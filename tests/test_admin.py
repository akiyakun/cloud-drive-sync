"""管理画面のテスト（Google や Discord にはつながない）

実行: .venv/bin/python -m unittest discover -s tests -v
"""

import asyncio
import json
import re
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cds_admin.app import create_app  # noqa: E402
from cds_admin.notify import is_valid_webhook, mask_webhook  # noqa: E402
from cds_admin.runner import Scheduler, SyncRunner  # noqa: E402
from cds_admin.schedule import next_run, parse_daily_times, parse_interval  # noqa: E402
from cds_admin.security import LoginGuard  # noqa: E402

TZ = ZoneInfo("Asia/Tokyo")


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(text)
        return True, "送信しました"


def csrf_of(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


class AdminTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notifier = FakeNotifier()
        self.app = create_app(data_dir=self.tmp.name, command=[sys.executable, "-c", "pass"],
                              allowed_hosts=["testserver"], run_scheduler=False, notifier=self.notifier)
        self.client = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def login(self, password="admin"):
        page = self.client.get("/login")
        return self.client.post("/login", data={"csrf": csrf_of(page.text), "password": password})

    def csrf(self):
        return csrf_of(self.client.get("/").text)


class LoginTests(AdminTestCase):
    def test_redirects_to_login_when_not_logged_in(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], "/login")

    def test_default_password_logs_in_and_shows_warning(self):
        self.assertEqual(self.login().headers["location"], "/")
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("初期値（admin）のまま", page.text)

    def test_wrong_password_is_rejected(self):
        response = self.login("wrong")
        self.assertIn("error=password", response.headers["location"])
        self.assertEqual(self.client.get("/").status_code, 303)

    def test_three_failures_lock_and_notify(self):
        for _ in range(3):
            self.login("wrong")
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("ログインを止めました", self.notifier.sent[0])
        # ロック中は正しいパスワードでも入れない
        self.assertIn("error=locked", self.login("admin").headers["location"])

    def test_login_page_opened_twice_still_works(self):
        first = self.client.get("/login")
        self.client.get("/login")  # 2 つ目のタブ・再読み込み
        response = self.client.post("/login", data={"csrf": csrf_of(first.text), "password": "admin"})
        self.assertEqual(response.headers["location"], "/")

    def test_login_without_csrf_is_rejected(self):
        self.client.get("/login")
        response = self.client.post("/login", data={"password": "admin"})
        self.assertIn("error=expired", response.headers["location"])


class SecurityTests(AdminTestCase):
    def test_post_without_csrf_is_forbidden(self):
        self.login()
        self.assertEqual(self.client.post("/sync", data={}).status_code, 403)

    def test_cross_origin_post_is_forbidden(self):
        self.login()
        response = self.client.post("/sync", data={"csrf": self.csrf()}, headers={"origin": "http://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_unknown_host_is_rejected(self):
        self.assertEqual(self.client.get("/login", headers={"host": "evil.example"}).status_code, 400)

    def test_null_origin_is_rejected(self):
        page = self.client.get("/login")
        response = self.client.post("/login", data={"csrf": csrf_of(page.text), "password": "admin"},
                                    headers={"origin": "null"})
        self.assertIn("error=expired", response.headers["location"])

    def test_security_headers(self):
        headers = self.client.get("/login").headers
        # no-referrer だとブラウザが Origin を "null" にしてしまうので same-origin
        self.assertEqual(headers["referrer-policy"], "same-origin")
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])
        self.assertEqual(headers["x-frame-options"], "DENY")

    def test_session_cookie_flags(self):
        cookie = self.login().headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)

    def test_api_requires_login(self):
        self.assertEqual(self.client.get("/api/status").status_code, 401)

    def test_log_path_rejects_traversal(self):
        self.login()
        self.assertEqual(self.client.get("/runs/..%2F..%2Fadmin/log").status_code, 404)


class PasswordTests(AdminTestCase):
    def change(self, current, new, confirm=None):
        return self.client.post("/password", data={"csrf": self.csrf(), "current": current, "new": new,
                                                   "confirm": new if confirm is None else confirm})

    def test_rules(self):
        self.login()
        for current, new, confirm, expected in [
            ("wrong", "goodpassword", None, "今のパスワードが違います"),
            ("admin", "short", None, "8 文字以上"),
            ("admin", "admin", None, "8 文字以上"),
            ("admin", "goodpassword", "different!", "一致しません"),
        ]:
            self.change(current, new, confirm)
            self.assertIn(expected, self.client.get("/").text)

    def test_wrong_current_password_locks_like_login(self):
        self.login()
        for _ in range(2):
            self.change("wrong", "goodpassword")
        response = self.change("wrong", "goodpassword")  # 3 回目でロックし、ログアウトする
        self.assertIn("error=locked", response.headers["location"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIn("パスワードの変更", self.notifier.sent[0])
        self.assertEqual(self.client.get("/").status_code, 303)
        self.assertIn("error=locked", self.login("admin").headers["location"])  # ロック中は正しくても入れない

    def test_change_and_login_with_new_password(self):
        self.login()
        self.change("admin", "new-password-123")
        page = self.client.get("/").text
        self.assertIn("パスワードを変更しました", page)
        self.assertNotIn("初期値（admin）のまま", page)
        self.client.cookies.clear()
        self.assertIn("error=password", self.login("admin").headers["location"])
        self.assertEqual(self.login("new-password-123").headers["location"], "/")
        saved = json.loads((Path(self.tmp.name) / "admin.json").read_text())
        self.assertNotIn("new-password-123", json.dumps(saved))  # パスワードそのものは保存しない


class ScheduleAndWebhookTests(AdminTestCase):
    def test_save_interval_and_daily(self):
        self.login()
        self.client.post("/schedule", data={"csrf": self.csrf(), "mode": "interval", "interval_minutes": "30"})
        self.assertEqual(self.app.state.store.load()["schedule"]["interval_minutes"], 30)
        self.client.post("/schedule", data={"csrf": self.csrf(), "mode": "daily", "daily_times": "18:00, 9:00"})
        schedule = self.app.state.store.load()["schedule"]
        self.assertEqual((schedule["mode"], schedule["daily_times"]), ("daily", ["09:00", "18:00"]))

    def test_invalid_schedule_is_rejected(self):
        self.login()
        self.client.post("/schedule", data={"csrf": self.csrf(), "mode": "interval", "interval_minutes": "1"})
        self.assertIn("10〜1440 分", self.client.get("/").text)
        self.assertEqual(self.app.state.store.load()["schedule"]["interval_minutes"], 60)

    def test_pause_and_resume(self):
        self.login()
        self.client.post("/pause", data={"csrf": self.csrf(), "paused": "1"})
        self.assertTrue(self.app.state.store.load()["schedule"]["paused"])
        self.client.post("/pause", data={"csrf": self.csrf(), "paused": "0"})
        self.assertFalse(self.app.state.store.load()["schedule"]["paused"])

    def test_archive_setting(self):
        self.login()
        self.assertEqual(self.app.state.store.load()["archive"], {"enabled": False, "max_gb": 1.0})  # 既定は OFF
        self.client.post("/archive", data={"csrf": self.csrf(), "enabled": "1", "max_gb": "2.5"})
        self.assertEqual(self.app.state.store.load()["archive"], {"enabled": True, "max_gb": 2.5})
        for bad in ("0", "-1", "abc", "99999"):
            self.client.post("/archive", data={"csrf": self.csrf(), "enabled": "1", "max_gb": bad})
            self.assertEqual(self.app.state.store.load()["archive"]["max_gb"], 2.5)
        self.client.post("/archive", data={"csrf": self.csrf(), "max_gb": "2.5"})  # チェックを外す
        self.assertFalse(self.app.state.store.load()["archive"]["enabled"])

    def test_webhook_only_discord(self):
        self.login()
        self.client.post("/webhook", data={"csrf": self.csrf(), "webhook_url": "http://192.168.0.100/hook"})
        self.assertEqual(self.app.state.store.load()["webhook_url"], "")
        url = "https://discord.com/api/webhooks/123/abcdefg"
        self.client.post("/webhook", data={"csrf": self.csrf(), "webhook_url": url})
        self.assertEqual(self.app.state.store.load()["webhook_url"], url)
        self.assertNotIn("abcdefg", self.client.get("/").text.replace(mask_webhook(url), ""))


class ScheduleCalcTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_daily_times("9:00、13:30 9:00"), ["09:00", "13:30"])
        for bad in ("", "25:00", "abc"):
            with self.assertRaises(ValueError):
                parse_daily_times(bad)
        self.assertEqual(parse_interval("45"), 45)
        for bad in ("5", "2000", "x"):
            with self.assertRaises(ValueError):
                parse_interval(bad)

    def test_next_run(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=TZ)
        interval = {"mode": "interval", "interval_minutes": 30}
        self.assertEqual(next_run(interval, now, None), now + timedelta(minutes=1))
        self.assertEqual(next_run(interval, now, now - timedelta(minutes=10)), now + timedelta(minutes=20))
        self.assertEqual(next_run(interval, now, now - timedelta(hours=3)), now)
        daily = {"mode": "daily", "daily_times": ["09:00", "18:00"]}
        self.assertEqual(next_run(daily, now), datetime(2026, 10, 12, 18, 0, tzinfo=TZ))
        late = datetime(2026, 10, 12, 19, 0, tzinfo=TZ)
        self.assertEqual(next_run(daily, late), datetime(2026, 10, 13, 9, 0, tzinfo=TZ))
        self.assertIsNone(next_run({**daily, "paused": True}, now))

    def test_login_guard_window(self):
        clock = [0.0]
        guard = LoginGuard(3, 180, 900, clock=lambda: clock[0])
        guard.record_failure(); clock[0] = 200  # 1 回目は 3 分を過ぎたので数えない
        self.assertFalse(guard.record_failure())
        self.assertTrue(guard.record_failure() or guard.record_failure())
        self.assertGreater(guard.locked_seconds_left(), 0)

    def test_webhook_validation(self):
        self.assertTrue(is_valid_webhook("https://discord.com/api/webhooks/1/x"))
        self.assertFalse(is_valid_webhook("https://example.com/api/webhooks/1/x"))
        self.assertFalse(is_valid_webhook("http://discord.com/api/webhooks/1/x"))


class FakeStore:
    def __init__(self, schedule):
        self.schedule = schedule

    def load(self):
        return {"schedule": self.schedule}


class FakeRunner:
    def __init__(self):
        self.started, self.last, self.running = [], None, False

    def is_running(self):
        return self.running

    def last_started(self):
        return self.last

    async def start(self, trigger):
        self.started.append(trigger)
        self.last = datetime.now(TZ)
        return True


class SchedulerTests(unittest.TestCase):
    def test_manual_run_pushes_interval_back(self):
        runner = FakeRunner()
        scheduler = Scheduler(FakeStore({"mode": "interval", "interval_minutes": 60}), runner, TZ)
        scheduler.next_due = datetime.now(TZ) - timedelta(seconds=1)  # 予定の時刻になった
        runner.last = datetime.now(TZ) - timedelta(minutes=1)          # でも 1 分前に手動で同期した
        asyncio.run(scheduler.check())
        self.assertEqual(runner.started, [])
        self.assertGreater(scheduler.next_due, datetime.now(TZ) + timedelta(minutes=58))

    def test_due_interval_starts(self):
        runner = FakeRunner()
        scheduler = Scheduler(FakeStore({"mode": "interval", "interval_minutes": 60}), runner, TZ)
        runner.last = datetime.now(TZ) - timedelta(hours=2)
        scheduler.next_due = datetime.now(TZ) - timedelta(seconds=1)
        asyncio.run(scheduler.check())
        self.assertEqual(runner.started, ["schedule"])

    def test_daily_starts_at_time_even_after_manual(self):
        runner = FakeRunner()
        scheduler = Scheduler(FakeStore({"mode": "daily", "daily_times": ["09:00"]}), runner, TZ)
        runner.last = datetime.now(TZ) - timedelta(minutes=1)
        scheduler.next_due = datetime.now(TZ) - timedelta(seconds=1)
        asyncio.run(scheduler.check())
        self.assertEqual(runner.started, ["schedule"])

    def test_daily_slot_during_running_sync_is_skipped(self):
        runner = FakeRunner()
        scheduler = Scheduler(FakeStore({"mode": "daily", "daily_times": ["09:00"]}), runner, TZ)
        scheduler.next_due = datetime.now(TZ) - timedelta(seconds=1)  # 予定の時刻になったが、手動の同期の最中
        runner.running = True
        asyncio.run(scheduler.check())
        runner.running = False                                         # 手動の同期が終わった
        asyncio.run(scheduler.check())
        self.assertEqual(runner.started, [])                           # 続けてもう 1 回は走らない
        self.assertGreater(scheduler.next_due, datetime.now(TZ))       # 次は明日の 09:00

    def test_paused_never_starts(self):
        runner = FakeRunner()
        scheduler = Scheduler(FakeStore({"mode": "interval", "interval_minutes": 10, "paused": True}), runner, TZ)
        asyncio.run(scheduler.check())
        self.assertEqual((runner.started, scheduler.upcoming()), ([], None))


class RunnerTests(unittest.TestCase):
    """偽物の同期（--summary に結果を書く小さなスクリプト）で、記録と通知の送り分けを確かめる"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.script = Path(self.tmp.name) / "fake_sync.py"
        self.script.write_text(textwrap.dedent("""
            import json, os, sys
            path = sys.argv[sys.argv.index("--summary") + 1]
            mode = os.environ.get("FAKE_MODE", "ok")
            limit = sys.argv[sys.argv.index("--archive-max-mb") + 1] if "--archive-max-mb" in sys.argv else None
            target = {"name": "t", "kind": "folder", "new": 1, "updated": 0, "unchanged": 5, "moved": 0,
                      "archived": 0, "skipped": 0, "failed": ["a.md (500 err)"] if mode == "warning" else [],
                      "pdf_fallback": []}
            summary = {"targets": [] if mode == "error" else [target], "errors": [],
                       "fatal": "Google の鍵（token.json）が無効です" if mode == "error" else None,
                       "archive": {"size": 10, "removed": 1 if limit else 0, "removed_bytes": 5, "limit": limit}}
            open(path, "w").write(json.dumps(summary))
            print("sync output")
            sys.exit(1 if mode == "error" else 0)
        """))
        self.notifier = FakeNotifier()
        self.options = []
        self.runner = SyncRunner(self.tmp.name, self.notifier, [sys.executable, str(self.script)], TZ,
                                 options=lambda: self.options)

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, mode):
        import os
        os.environ["FAKE_MODE"] = mode

        async def go():
            self.assertTrue(await self.runner.start("manual"))
            self.assertFalse(await self.runner.start("manual"))  # 同期中は 2 つ目を始めない
            await self.runner.wait()
        asyncio.run(go())
        return self.runner.history()[0]

    def test_records_and_notifies_only_on_change(self):
        run = self.run_once("ok")
        self.assertEqual((run["status"], run["counts"]["new"]), ("ok", 1))
        self.assertEqual(self.notifier.sent, [])
        self.assertIn("sync output", self.runner.log_path(run["id"]).read_text())

        self.assertEqual(self.run_once("warning")["status"], "warning")
        self.assertEqual(len(self.notifier.sent), 1)  # 失敗が始まった
        self.run_once("warning")
        self.assertEqual(len(self.notifier.sent), 1)  # 同じ失敗が続いても送らない
        error = self.run_once("error")
        self.assertEqual(error["status"], "error")
        self.assertIn("鍵", error["fatal"])
        self.assertEqual(len(self.notifier.sent), 2)  # 失敗の種類が変わった
        self.run_once("ok")
        self.assertEqual(len(self.notifier.sent), 3)
        self.assertIn("解消", self.notifier.sent[-1])  # 直った

    def test_failure_before_sync_starts_is_recorded_and_notified(self):
        self.runner.logs_dir.parent.mkdir(parents=True, exist_ok=True)
        self.runner.logs_dir.write_text("ログのフォルダの場所にファイルがある")  # ログを書けない状態
        run = self.run_once("ok")
        self.assertEqual(run["status"], "error")
        self.assertIn("同期を始められませんでした", run["fatal"])
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertIsNone(self.runner.current)

    def test_passes_archive_limit_from_settings(self):
        from cds_admin.settings import sync_options
        self.assertEqual(sync_options({"archive": {"enabled": False, "max_gb": 1.0}}), [])
        self.options = sync_options({"archive": {"enabled": True, "max_gb": 1.5}})
        run = self.run_once("ok")
        self.assertEqual((run["archive"]["limit"], run["archive"]["removed"]), ("1536", 1))


if __name__ == "__main__":
    unittest.main()
