"""管理画面（Starlette）

安全のための決まり:
- ログイン必須（パスワードは scrypt のハッシュで確認）。window 秒以内に max_failures 回まちがえたら lock 秒ロックし、通知する
- ログインの状態は HttpOnly・SameSite=Strict の Cookie。POST には CSRF 対策の合言葉が必要で、Origin も確かめる
- 受け付けるホスト名を CDS_ALLOWED_HOSTS に限る（DNS リバインディング対策）
- 画面の外部読み込み・埋め込みを禁止する（Content-Security-Policy など）
"""

import asyncio
import contextlib
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from . import __version__, targets as target_ops
from .notify import Notifier, is_valid_webhook, mask_webhook
from .runner import Scheduler, SyncRunner, _status_label
from .schedule import MAX_INTERVAL, MIN_INTERVAL, parse_daily_times, parse_interval
from .security import LoginGuard, Sessions, hash_password, same_token, verify_password
from .settings import (DEFAULT_PASSWORD, MAX_ARCHIVE_GB, MIN_ARCHIVE_GB, SettingsStore, parse_archive_gb,
                       sync_options)

HERE = Path(__file__).parent
APP_ROOT = HERE.parent
SESSION_COOKIE = "cds_session"
LOGIN_CSRF_COOKIE = "cds_login_csrf"
MIN_PASSWORD_LENGTH = 8
LOG_TAIL_BYTES = 200_000


class SecurityHeaders:
    """すべての応答に、安全のためのヘッダーを付ける"""

    HEADERS = [
        (b"content-security-policy",
         b"default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
         b"frame-ancestors 'none'; form-action 'self'; base-uri 'none'"),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        # same-origin: よそのサイトにはリファラーを送らない。no-referrer にすると、ブラウザがフォーム送信の
        # Origin まで "null" にしてしまい、Origin の確認でログインできなくなる
        (b"referrer-policy", b"same-origin"),
        (b"cache-control", b"no-store"),
    ]

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [h for h in message["headers"] if h[0] not in dict(self.HEADERS)] + self.HEADERS
            await send(message)

        await self.app(scope, receive, send_with_headers)


def _fmt(value):
    """日時を「10/12 17:13」の形にする（画面用）"""
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%m/%d %H:%M")


def _size(value):
    """バイト数を「12.3 MB」の形にする（画面用）"""
    if value is None:
        return "—"
    for unit in ("B", "KB", "MB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.2f} GB"


def create_app(data_dir=None, command=None, allowed_hosts=None, run_scheduler=True, notifier=None,
               config_path=None, drive_lookup=None):
    tz = ZoneInfo(os.environ.get("TZ") or "Asia/Tokyo")
    config_file = target_ops.ConfigFile(config_path or os.environ.get("CDS_CONFIG", "data/config.toml"))
    drive_lookup = drive_lookup or target_ops.lookup_drive
    data_dir = Path(data_dir or os.environ.get("CDS_ADMIN_DIR", "data/dev/admin"))
    store = SettingsStore(data_dir / "admin.json")
    settings = store.load()
    sessions = Sessions(lifetime_seconds=int(settings["session_hours"]) * 3600)
    guard = LoginGuard(**settings["lockout"])
    notifier = notifier or Notifier(store)
    runner = SyncRunner(data_dir, notifier, command or [sys.executable, str(APP_ROOT / "sync.py")], tz,
                        options=lambda: sync_options(store.load()))
    scheduler = Scheduler(store, runner, tz)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["fmt"] = _fmt
    templates.env.filters["status_label"] = _status_label
    templates.env.filters["size"] = _size

    # --- 共通の処理 ---

    def current_session(request):
        token = request.cookies.get(SESSION_COOKIE)
        return token, sessions.get(token)

    def same_origin(request):
        origin = request.headers.get("origin")
        return origin is None or origin == f"{request.url.scheme}://{request.headers.get('host', '')}"

    def flash(session, kind, text):
        session["flash"].append({"kind": kind, "text": text})

    def login_required(handler):
        """ログインしていなければログイン画面へ。POST は CSRF の合言葉と Origin を確かめる"""
        async def wrapper(request):
            token, session = current_session(request)
            if session is None:
                if request.method == "GET" and not request.url.path.startswith("/api/"):
                    return RedirectResponse("/login", status_code=303)
                return PlainTextResponse("ログインしてください", status_code=401)
            form = None
            if request.method == "POST":
                form = await request.form()
                if not same_origin(request) or not same_token(form.get("csrf"), session["csrf"]):
                    return PlainTextResponse("不正なリクエストです（画面を開き直してください）", status_code=403)
            return await handler(request, token, session, form)
        return wrapper

    def back(path="/"):
        return RedirectResponse(path, status_code=303)

    async def notify_lock(reason):
        lock = store.load()["lockout"]
        await notifier.send(
            f"🔒 CloudDrive-Sync: {reason}に {lock['max_failures']} 回失敗したため、"
            f"{lock['lock_seconds'] // 60} 分間ログインを止めました。心当たりがなければ、パスワードを変更してください。"
        )

    # --- ログイン ---

    async def login_page(request):
        _, session = current_session(request)
        if session:
            return back()
        # 合言葉が Cookie にすでにあれば使い回す（タブを 2 つ開いた・戻るで戻ったときに、ずれないように）
        csrf = request.cookies.get(LOGIN_CSRF_COOKIE, "")
        if not re.fullmatch(r"[0-9a-f]{48}", csrf):
            csrf = os.urandom(24).hex()
        response = templates.TemplateResponse(request, "login.html", {
            "csrf": csrf, "error": request.query_params.get("error"),
            "locked": guard.locked_seconds_left(), "version": __version__,
        })
        response.set_cookie(LOGIN_CSRF_COOKIE, csrf, httponly=True, samesite="strict", max_age=600)
        return response

    async def login_post(request):
        form = await request.form()
        if not same_origin(request) or not same_token(form.get("csrf"), request.cookies.get(LOGIN_CSRF_COOKIE)):
            return RedirectResponse("/login?error=expired", status_code=303)
        if guard.locked_seconds_left():
            return RedirectResponse("/login?error=locked", status_code=303)
        if not verify_password(form.get("password", ""), store.load()["password"]):
            if guard.record_failure():
                await notify_lock("管理画面のログイン")
                return RedirectResponse("/login?error=locked", status_code=303)
            await asyncio.sleep(1)  # 総当たりを遅くする
            return RedirectResponse("/login?error=password", status_code=303)
        guard.record_success()
        token = sessions.create()
        response = back()
        response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="strict", max_age=sessions.lifetime)
        response.delete_cookie(LOGIN_CSRF_COOKIE)
        return response

    @login_required
    async def logout(request, token, session, form):
        sessions.delete(token)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # --- 同期先 ---

    def shown_path(config, path):
        """出力先を、利用者が分かる場所（NAS の中の場所。CDS_OUTPUT_LABEL）で表す"""
        if not path:
            return None
        root = Path(config["output_root"]).resolve()
        label = os.environ.get("CDS_OUTPUT_LABEL") or str(root)
        try:
            rel = Path(path).relative_to(root).as_posix()
        except ValueError:
            return str(path)
        return label if rel == "." else f"{label}/{rel}"

    def output_label():
        try:
            config = config_file.effective()
            return shown_path(config, Path(config["output_root"]))
        except (OSError, ValueError, KeyError):
            return "出力の土台"

    def targets_view():
        """一覧に出す同期先を、グループごとにまとめる。[(グループ名か None, [同期先, ...]), ...]。読めなければ (None, 理由)"""
        try:
            data, config = config_file.raw(), config_file.effective()
        except (OSError, ValueError) as e:
            return None, f"設定ファイル（{config_file.path.name}）を読めません: {type(e).__name__}: {e}"
        groups = {}
        for kind, target in target_ops.list_targets(data):
            info = target_ops.summarize(config, kind, target)
            groups.setdefault(target.get("group") or None, []).append({
                "kind": kind, "kind_label": target_ops.KIND_LABELS[kind], "name": target["name"],
                "output": target.get("output") or target["name"], "recursive": target.get("recursive", True),
                "active": target_ops.is_active(target), "environments": target.get("environments"),
                "last_out": shown_path(config, info["out_dir"]), "files": info["files"],
            })
        # グループは設定に書いた順。グループなしは最後
        return sorted(groups.items(), key=lambda g: g[0] is None), None

    def group_names():
        try:
            return target_ops.group_names(config_file.raw())
        except (OSError, ValueError):
            return []

    def is_popup(request, form=None):
        """別ウィンドウ（同期先の追加・編集・削除）で開いているか"""
        return (form.get("popup") if form is not None else request.query_params.get("popup")) == "1"

    def finish(request, session, popup, kind, text):
        """操作の結果を伝える。別ウィンドウなら結果の画面（元の画面を更新して閉じる）、そうでなければ一覧へ戻る"""
        if popup:
            return templates.TemplateResponse(request, "target_done.html", {
                "kind": kind, "text": text, "version": __version__,
            })
        flash(session, kind, text)
        return back()

    def form_page(request, session, mode, kind, values, popup, error=None, status_code=200):
        return templates.TemplateResponse(request, "target_form.html", {
            "csrf": session["csrf"], "mode": mode, "kind": kind, "kind_label": target_ops.KIND_LABELS[kind],
            "values": values, "error": error, "output_label": output_label(), "groups": group_names(),
            "popup": popup, "version": __version__,
        }, status_code=status_code)

    @login_required
    async def target_new_page(request, token, session, form):
        kind = request.query_params.get("kind")
        if kind not in target_ops.KINDS:
            return back()
        group = (request.query_params.get("group") or "").strip()
        # グループから追加したときは、出力先の頭にグループ名を入れておく
        return form_page(request, session, "new", kind, {
            "recursive": True, "group": group, "output": f"{group}/" if group else "",
        }, is_popup(request))

    @login_required
    async def target_edit_page(request, token, session, form):
        popup = is_popup(request)
        try:
            kind, target = target_ops.find_target(config_file.raw(), request.query_params.get("name"))
        except (OSError, ValueError):
            kind = None
        if kind is None:
            return finish(request, session, popup, "error", "その同期先は見つかりません。")
        return form_page(request, session, "edit", kind, {
            "original": target["name"], "name": target["name"], "group": target.get("group", ""),
            "drive": target.get("folder_id" if kind == "folder" else "file_id", ""),
            "output": target.get("output", ""), "recursive": target.get("recursive", True),
        }, popup)

    @login_required
    async def target_save(request, token, session, form):
        original = form.get("original") or None
        kind = form.get("kind")
        popup = is_popup(request, form)
        values = {"original": original, "name": form.get("name", ""), "group": form.get("group", ""),
                  "drive": form.get("drive", ""), "output": form.get("output", ""),
                  "recursive": form.get("recursive") == "1"}
        mode = "edit" if original else "new"
        if kind not in target_ops.KINDS:
            return back()

        def fail(message):
            return form_page(request, session, mode, kind, values, popup, message, status_code=400)

        if runner.is_running():
            return fail("同期中は変更できません。同期が終わってから、もう一度保存してください。")
        try:
            data, config = config_file.raw(), config_file.effective()
            current = None
            if original:
                found_kind, current = target_ops.find_target(data, original)
                if current is None or found_kind != kind:
                    return fail("編集していた同期先が見つかりません（ほかの画面で変更・削除されたかもしれません）。")
            name = target_ops.parse_name(values["name"])
            group = target_ops.parse_group(values["group"])
            drive_id = target_ops.parse_drive_id(values["drive"])
            output = target_ops.parse_output(values["output"])
            target_ops.check_unique(data, name, ignore=current)
            item = await asyncio.to_thread(drive_lookup, config, drive_id)
            target_ops.check_drive_kind(kind, item)

            id_key = "folder_id" if kind == "folder" else "file_id"
            target = current if current is not None else {}
            if current is None:
                data.setdefault(target_ops.KINDS[kind], []).append(target)
            target["name"] = name
            for key, value in (("group", group), ("output", output)):
                if value is None:
                    target.pop(key, None)
                else:
                    target[key] = value
            target[id_key] = drive_id
            if kind == "folder":
                target["recursive"] = values["recursive"]
            drive_names = {name: item["name"]}
            for other, other_id in target_ops.missing_drive_names(config, data, drive_names):
                try:
                    drive_names[other] = (await asyncio.to_thread(drive_lookup, config, other_id))["name"]
                except ValueError:
                    pass  # 読めない同期先は、同期のときに確かめる
            target_ops.check_out_dirs(config, data, drive_names)

            # Drive に問い合わせている間に、同期が始まったかもしれない
            if runner.is_running():
                return fail("同期が始まったため、保存しませんでした。同期が終わってから、もう一度保存してください。")
            undo = target_ops.rename_records(config, original, name) if original and original != name else None
            try:
                config_file.save(data)
            except Exception:
                if undo:
                    undo()
                raise
        except ValueError as e:
            return fail(str(e))
        except OSError as e:
            return fail(f"保存できませんでした（{type(e).__name__}: {e}）")
        return finish(request, session, popup, "ok",
                      f"同期先「{name}」を保存しました（Drive 上の名前: {item.get('name')}）。次の同期から反映されます。")

    @login_required
    async def target_delete_page(request, token, session, form):
        popup = is_popup(request)
        try:
            config = config_file.effective()
            kind, target = target_ops.find_target(config_file.raw(), request.query_params.get("name"))
        except (OSError, ValueError):
            kind = None
        if kind is None:
            return finish(request, session, popup, "error", "その同期先は見つかりません。")
        info = target_ops.summarize(config, kind, target)
        return templates.TemplateResponse(request, "target_delete.html", {
            "csrf": session["csrf"], "name": target["name"], "kind_label": target_ops.KIND_LABELS[kind],
            "out_dir": shown_path(config, info["out_dir"]), "files": info["files"], "popup": popup,
            "version": __version__,
        })

    @login_required
    async def target_delete(request, token, session, form):
        name = form.get("name")
        popup = is_popup(request, form)
        if runner.is_running():
            return finish(request, session, popup, "error", "同期中は削除できません。同期が終わってから、もう一度操作してください。")
        try:
            data, config = config_file.raw(), config_file.effective()
            kind, target = target_ops.find_target(data, name)
            if kind is None:
                return finish(request, session, popup, "error", "その同期先は見つかりません。")
            data[target_ops.KINDS[kind]].remove(target)
            config_file.save(data)
        except (OSError, ValueError) as e:
            return finish(request, session, popup, "error", f"削除できませんでした（{type(e).__name__}: {e}）")
        try:
            archived = target_ops.retire_outputs(config, kind, name)
        except OSError as e:
            return finish(request, session, popup, "warn", f"同期先「{name}」を削除しましたが、出力の退避の途中で失敗しました"
                          f"（{type(e).__name__}）。残った出力は手で整理してください。")
        return finish(request, session, popup, "ok", f"同期先「{name}」を削除しました。出力 {archived} 個を退避へ移しました。")

    # --- 画面 ---

    def status_data():
        history = runner.history()
        last = next((r for r in history if r.get("status") != "running"), None)
        upcoming = scheduler.upcoming()
        return {
            "running": runner.is_running(),
            "current": runner.current,
            "last": last,
            "next_run": upcoming.isoformat(timespec="seconds") if upcoming else None,
            "paused": store.load()["schedule"].get("paused", False),
        }

    @login_required
    async def index(request, token, session, form):
        settings = store.load()
        targets_rows, targets_error = targets_view()
        messages, session["flash"] = session["flash"], []
        return templates.TemplateResponse(request, "index.html", {
            "csrf": session["csrf"], "flash": messages, "settings": settings,
            "schedule": settings["schedule"], "status": status_data(), "history": runner.history()[:20],
            "webhook_masked": mask_webhook(settings["webhook_url"]),
            "default_password": settings["password_is_default"],
            "min_interval": MIN_INTERVAL, "max_interval": MAX_INTERVAL,
            "min_password": MIN_PASSWORD_LENGTH, "version": __version__,
            "archive": settings["archive"], "min_archive_gb": MIN_ARCHIVE_GB, "max_archive_gb": MAX_ARCHIVE_GB,
            "archive_info": next((r for r in runner.history() if r.get("archive")), None),
            "target_groups": targets_rows, "targets_error": targets_error, "output_label": output_label(),
        })

    @login_required
    async def api_status(request, token, session, form):
        return JSONResponse(status_data())

    @login_required
    async def run_log(request, token, session, form):
        path = runner.log_path(request.path_params["run_id"])
        if path is None or not path.exists():
            return PlainTextResponse("記録が見つかりません", status_code=404)
        data = path.read_bytes()[-LOG_TAIL_BYTES:]
        return templates.TemplateResponse(request, "log.html", {
            "run_id": request.path_params["run_id"], "text": data.decode("utf-8", errors="replace"),
            "truncated": path.stat().st_size > LOG_TAIL_BYTES, "version": __version__,
        })

    # --- 操作 ---

    @login_required
    async def sync_now(request, token, session, form):
        if await runner.start("manual"):
            scheduler.reset()  # ○分ごとの次の予定は、この同期の開始から数える
            flash(session, "ok", "同期を始めました。終わると「直近の記録」に結果が出ます。")
        else:
            flash(session, "warn", "すでに同期中です。終わってからもう一度押してください。")
        return back()

    @login_required
    async def toggle_pause(request, token, session, form):
        paused = form.get("paused") == "1"
        store.update(lambda s: s["schedule"].update({"paused": paused}))
        scheduler.reset()
        flash(session, "ok", "自動同期を止めました。" if paused else "自動同期を再開しました。")
        return back()

    @login_required
    async def save_schedule(request, token, session, form):
        try:
            mode = form.get("mode")
            if mode == "interval":
                change = {"mode": "interval", "interval_minutes": parse_interval(form.get("interval_minutes"))}
            elif mode == "daily":
                change = {"mode": "daily", "daily_times": parse_daily_times(form.get("daily_times", ""))}
            else:
                raise ValueError("同期のしかたを選んでください")
        except ValueError as e:
            flash(session, "error", f"同期のしかたを保存できませんでした: {e}")
            return back()
        store.update(lambda s: s["schedule"].update(change))
        scheduler.reset()
        flash(session, "ok", "同期のしかたを保存しました。")
        return back()

    @login_required
    async def save_archive(request, token, session, form):
        enabled = form.get("enabled") == "1"
        try:
            max_gb = parse_archive_gb(form.get("max_gb", ""))
        except ValueError as e:
            flash(session, "error", f"退避の自動削除を保存できませんでした: {e}")
            return back()
        store.update(lambda s: s["archive"].update({"enabled": enabled, "max_gb": max_gb}))
        flash(session, "ok", f"退避の自動削除を ON にしました（上限 {max_gb:g} GB）。次の同期から削除します。"
              if enabled else "退避の自動削除を OFF にしました。")
        return back()

    @login_required
    async def save_webhook(request, token, session, form):
        url = (form.get("webhook_url") or "").strip()
        if form.get("clear") == "1":
            url = ""
        elif not is_valid_webhook(url):
            flash(session, "error", "Discord の Webhook URL（https://discord.com/api/webhooks/…）を入れてください。")
            return back()
        store.update(lambda s: s.update({"webhook_url": url}))
        flash(session, "ok", "通知先を削除しました。" if not url else "通知先を保存しました。「テスト送信」で確かめられます。")
        return back()

    @login_required
    async def test_webhook(request, token, session, form):
        ok, message = await notifier.send("🔔 CloudDrive-Sync: テスト送信です。この通知が届いていれば設定は正しいです。")
        flash(session, "ok" if ok else "error", f"テスト送信: {message}")
        return back()

    @login_required
    async def change_password(request, token, session, form):
        current, new, confirm = form.get("current", ""), form.get("new", ""), form.get("confirm", "")
        error = None
        if guard.locked_seconds_left():
            error = "ロック中です。しばらくしてから、もう一度試してください。"
        elif not verify_password(current, store.load()["password"]):
            # ログインと同じ回数で数える（ログインしたままの画面を使った総当たりを防ぐ）
            if guard.record_failure():
                await notify_lock("パスワードの変更（今のパスワードの確認）")
                sessions.delete(token)
                response = RedirectResponse("/login?error=locked", status_code=303)
                response.delete_cookie(SESSION_COOKIE)
                return response
            await asyncio.sleep(1)
            error = "今のパスワードが違います。"
        elif len(new) < MIN_PASSWORD_LENGTH:
            error = f"新しいパスワードは {MIN_PASSWORD_LENGTH} 文字以上にしてください。"
        elif new == DEFAULT_PASSWORD:
            error = "初期パスワードと同じものは使えません。"
        elif new != confirm:
            error = "新しいパスワード（確認）が一致しません。"
        if error:
            flash(session, "error", f"パスワードを変更できませんでした: {error}")
            return back()
        record = await asyncio.to_thread(hash_password, new)
        store.update(lambda s: s.update({"password": record, "password_is_default": False}))
        sessions.delete_others(token)
        flash(session, "ok", "パスワードを変更しました。ほかのブラウザのログインは切れました。")
        return back()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(scheduler.loop()) if run_scheduler else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    hosts = allowed_hosts or [h.strip() for h in os.environ.get("CDS_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
    app = Starlette(
        routes=[
            Route("/", index),
            Route("/login", login_page, methods=["GET"]),
            Route("/login", login_post, methods=["POST"]),
            Route("/logout", logout, methods=["POST"]),
            Route("/sync", sync_now, methods=["POST"]),
            Route("/pause", toggle_pause, methods=["POST"]),
            Route("/schedule", save_schedule, methods=["POST"]),
            Route("/archive", save_archive, methods=["POST"]),
            Route("/targets/new", target_new_page, methods=["GET"]),
            Route("/targets/edit", target_edit_page, methods=["GET"]),
            Route("/targets/save", target_save, methods=["POST"]),
            Route("/targets/delete", target_delete_page, methods=["GET"]),
            Route("/targets/delete", target_delete, methods=["POST"]),
            Route("/webhook", save_webhook, methods=["POST"]),
            Route("/webhook/test", test_webhook, methods=["POST"]),
            Route("/password", change_password, methods=["POST"]),
            Route("/api/status", api_status),
            Route("/runs/{run_id}/log", run_log),
            Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
        ],
        middleware=[Middleware(TrustedHostMiddleware, allowed_hosts=hosts), Middleware(SecurityHeaders)],
        lifespan=lifespan,
    )
    app.state.runner = runner
    app.state.scheduler = scheduler
    app.state.store = store
    app.state.guard = guard
    return app
