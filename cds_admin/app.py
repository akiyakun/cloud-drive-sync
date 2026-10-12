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

from . import __version__
from .notify import Notifier, is_valid_webhook, mask_webhook
from .runner import Scheduler, SyncRunner, _status_label
from .schedule import MAX_INTERVAL, MIN_INTERVAL, parse_daily_times, parse_interval
from .security import LoginGuard, Sessions, hash_password, same_token, verify_password
from .settings import DEFAULT_PASSWORD, SettingsStore

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


def create_app(data_dir=None, command=None, allowed_hosts=None, run_scheduler=True, notifier=None):
    tz = ZoneInfo(os.environ.get("TZ") or "Asia/Tokyo")
    data_dir = Path(data_dir or os.environ.get("CDS_ADMIN_DIR", "data/dev/admin"))
    store = SettingsStore(data_dir / "admin.json")
    settings = store.load()
    sessions = Sessions(lifetime_seconds=int(settings["session_hours"]) * 3600)
    guard = LoginGuard(**settings["lockout"])
    notifier = notifier or Notifier(store)
    runner = SyncRunner(data_dir, notifier, command or [sys.executable, str(APP_ROOT / "sync.py")], tz)
    scheduler = Scheduler(store, runner, tz)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["fmt"] = _fmt
    templates.env.filters["status_label"] = _status_label

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
                lock = store.load()["lockout"]
                await notifier.send(
                    f"🔒 CloudDrive-Sync: 管理画面のログインに {lock['max_failures']} 回失敗したため、"
                    f"{lock['lock_seconds'] // 60} 分間ログインを止めました。心当たりがなければ、パスワードを変更してください。"
                )
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
        messages, session["flash"] = session["flash"], []
        return templates.TemplateResponse(request, "index.html", {
            "csrf": session["csrf"], "flash": messages, "settings": settings,
            "schedule": settings["schedule"], "status": status_data(), "history": runner.history()[:20],
            "webhook_masked": mask_webhook(settings["webhook_url"]),
            "default_password": settings["password_is_default"],
            "min_interval": MIN_INTERVAL, "max_interval": MAX_INTERVAL,
            "min_password": MIN_PASSWORD_LENGTH, "version": __version__,
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
        if not verify_password(current, store.load()["password"]):
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
