"""同期を別のプロセスで 1 つずつ動かし、結果を記録して、必要なら通知する。定期実行もここ"""

import asyncio
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .schedule import next_run

HISTORY_SIZE = 50
RUN_TIMEOUT = timedelta(hours=2)


class SyncRunner:
    """同期の実行と記録（runs.json と logs/<実行の ID>.log）"""

    def __init__(self, data_dir, notifier, command, tz):
        self.data_dir = Path(data_dir)
        self.history_path = self.data_dir / "runs.json"
        self.logs_dir = self.data_dir / "logs"
        self.notifier = notifier
        self.command = list(command)
        self.tz = tz
        self.current = None
        self._task = None

    # --- 記録 ---

    def history(self):
        """これまでの実行（新しい順）"""
        if not self.history_path.exists():
            return []
        try:
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        except ValueError:
            return []

    def _save_history(self, runs):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.history_path.with_name(f".{self.history_path.name}.tmp")
        tmp.write_text(json.dumps(runs[:HISTORY_SIZE], ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.history_path)
        keep = {f"{r['id']}.log" for r in runs[:HISTORY_SIZE]}
        if self.logs_dir.exists():
            for log in self.logs_dir.glob("*.log"):
                if log.name not in keep:
                    log.unlink(missing_ok=True)

    def last_started(self):
        if self.current:
            return datetime.fromisoformat(self.current["started_at"])
        runs = self.history()
        return datetime.fromisoformat(runs[0]["started_at"]) if runs else None

    def log_path(self, run_id):
        if not run_id.replace("-", "").isalnum():
            return None
        return self.logs_dir / f"{run_id}.log"

    # --- 実行 ---

    def is_running(self):
        return self._task is not None and not self._task.done()

    async def start(self, trigger):
        """同期を始める。すでに同期中なら何もしないで False"""
        if self.is_running():
            return False
        now = datetime.now(self.tz)
        self.current = {
            "id": now.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2),
            "trigger": trigger,
            "started_at": now.isoformat(timespec="seconds"),
            "status": "running",
        }
        self._task = asyncio.create_task(self._run(self.current))
        return True

    async def wait(self):
        if self._task:
            await self._task

    async def _run(self, run):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        summary_path = Path(tempfile.gettempdir()) / f"cds-summary-{run['id']}.json"
        timed_out = False
        with open(self.logs_dir / f"{run['id']}.log", "wb") as log:
            process = await asyncio.create_subprocess_exec(
                *self.command, "--summary", str(summary_path),
                stdout=log, stderr=asyncio.subprocess.STDOUT, env=os.environ.copy(),
            )
            try:
                returncode = await asyncio.wait_for(process.wait(), RUN_TIMEOUT.total_seconds())
            except TimeoutError:
                process.kill()
                await process.wait()
                returncode, timed_out = -1, True

        summary = {}
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text(encoding="utf-8"))
            except ValueError:
                pass
            summary_path.unlink(missing_ok=True)

        finished = datetime.now(self.tz)
        targets = summary.get("targets", [])
        failed = [f"{t['name']}: {line}" for t in targets for line in t.get("failed", [])]
        fallback = [f"{t['name']}: {line}" for t in targets for line in t.get("pdf_fallback", [])]
        fatal = summary.get("fatal")
        if timed_out:
            fatal = f"{int(RUN_TIMEOUT.total_seconds() // 3600)} 時間たっても終わらないため止めました"
        elif returncode != 0 and not fatal:
            fatal = f"同期が異常終了しました（終了コード {returncode}）。ログを確認してください"

        run.update({
            "finished_at": finished.isoformat(timespec="seconds"),
            "seconds": int((finished - datetime.fromisoformat(run["started_at"])).total_seconds()),
            "counts": {k: sum(t.get(k, 0) for t in targets) for k in ("new", "updated", "unchanged", "moved", "archived")},
            "fatal": fatal,
            "errors": summary.get("errors", []),
            "failed": failed[:20],
            "failed_count": len(failed),
            "pdf_fallback": fallback[:20],
        })
        run["status"] = "error" if fatal else ("warning" if (failed or fallback or run["errors"]) else "ok")

        previous = next((r for r in self.history() if r.get("status") != "running"), None)
        self._save_history([run] + self.history())
        self.current = None
        await self._notify(previous, run)

    async def _notify(self, previous, run):
        """失敗が始まったとき・直ったときだけ通知する（毎回は送らない）"""
        healthy_before = previous is None or previous["status"] == "ok"
        if run["status"] != "ok" and (healthy_before or previous["status"] != run["status"]):
            lines = [f"⚠️ CloudDrive-Sync: 同期で問題が起きました（{_status_label(run['status'])}）"]
            if run["fatal"]:
                lines.append(run["fatal"])
            lines += [f"・{line}" for line in (run["errors"] + run["failed"] + run["pdf_fallback"])[:5]]
            lines.append("詳しくは管理画面の「直近の記録」を確認してください。")
            await self.notifier.send("\n".join(lines))
        elif run["status"] == "ok" and previous is not None and previous["status"] != "ok":
            await self.notifier.send("✅ CloudDrive-Sync: 同期の問題が解消しました（正常に完了）")


def _status_label(status):
    return {"ok": "正常", "warning": "一部失敗", "error": "停止", "running": "同期中"}.get(status, status)


class Scheduler:
    """設定された同期のしかたに従って、時間になったら同期を始める"""

    def __init__(self, store, runner, tz, tick_seconds=15):
        self.store = store
        self.runner = runner
        self.tz = tz
        self.tick = tick_seconds
        self.next_due = None

    def reset(self):
        """同期のしかたを変えたとき、次の予定を計算し直す"""
        self.next_due = None

    def upcoming(self):
        schedule = self.store.load()["schedule"]
        if schedule.get("paused"):
            return None
        if self.next_due is None:
            self.next_due = next_run(schedule, datetime.now(self.tz), self.runner.last_started())
        return self.next_due

    async def check(self):
        due = self.upcoming()
        now = datetime.now(self.tz)
        if due is None or now < due or self.runner.is_running():
            return
        schedule = self.store.load()["schedule"]
        if schedule.get("mode") != "daily":
            # ○分ごと: 予定を決めたあとに手動の同期があれば、その開始から数え直す（続けて 2 回走らないように）
            fresh = next_run(schedule, now, self.runner.last_started())
            if fresh is not None and fresh > now:
                self.next_due = fresh
                return
        await self.runner.start("schedule")
        self.next_due = None

    async def loop(self):
        while True:
            try:
                await self.check()
            except Exception as e:  # noqa: BLE001 - 定期実行の見回りは止めない
                print(f"定期実行の確認でエラー: {type(e).__name__}")
            await asyncio.sleep(self.tick)
