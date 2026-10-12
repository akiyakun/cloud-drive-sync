"""Discord への通知（Webhook）"""

import asyncio
import json
import urllib.error
import urllib.request

from . import __version__

# Discord の Webhook の URL だけを受け付ける（ほかの場所へ送らせないため）
ALLOWED_PREFIXES = (
    "https://discord.com/api/webhooks/",
    "https://discordapp.com/api/webhooks/",
    "https://ptb.discord.com/api/webhooks/",
    "https://canary.discord.com/api/webhooks/",
)


def is_valid_webhook(url):
    return isinstance(url, str) and url.startswith(ALLOWED_PREFIXES) and len(url) < 300 and " " not in url


def mask_webhook(url):
    """画面に出すときは、最後の数文字以外を伏せる"""
    if not url:
        return ""
    return url[:40] + "…" + "*" * 8 + url[-4:]


class Notifier:
    def __init__(self, store):
        self.store = store

    async def send(self, text):
        """通知を送る。戻り値は (送れたか, 利用者向けの結果の説明)。URL はログや画面に出さない"""
        url = self.store.load().get("webhook_url", "")
        if not url:
            return False, "通知先（Discord の Webhook URL）が設定されていません"
        if not is_valid_webhook(url):
            return False, "通知先の URL が Discord の Webhook ではありません"
        return await asyncio.to_thread(_post, url, text)


def _post(url, text):
    body = json.dumps({"username": "CloudDrive-Sync", "content": text[:1900]}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": f"CloudDrive-Sync/{__version__} (+https://github.com/akiyakun/cloud-drive-sync)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300, "送信しました"
    except urllib.error.HTTPError as e:
        return False, f"送信に失敗しました（Discord の応答 {e.code}）"
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, "送信に失敗しました（Discord につながりません）"
