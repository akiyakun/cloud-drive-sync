"""管理画面の設定（admin.json）の読み書き

admin.json に置くもの:
- password: パスワードのハッシュ（パスワードそのものは保存しない）
- password_is_default: 初期パスワード（admin）のままか
- webhook_url: Discord の Webhook URL
- schedule: 同期のしかた（mode = interval / daily、interval_minutes、daily_times、paused）
- lockout: ログインのロック（max_failures 回、window_seconds 秒以内に失敗したら lock_seconds 秒ロック）
- archive: 退避の自動削除（enabled、max_gb = 合計の上限）
"""

import copy
import json
import os
import threading
from pathlib import Path

from .security import hash_password

DEFAULT_PASSWORD = "admin"

DEFAULTS = {
    "password": None,
    "password_is_default": True,
    "webhook_url": "",
    "schedule": {"mode": "interval", "interval_minutes": 60, "daily_times": ["09:00"], "paused": False},
    "lockout": {"max_failures": 3, "window_seconds": 180, "lock_seconds": 900},
    "session_hours": 12,
    "archive": {"enabled": False, "max_gb": 1.0},
}

MIN_ARCHIVE_GB = 0.1
MAX_ARCHIVE_GB = 10000


def parse_archive_gb(text):
    """退避の上限（GB）の入力を確かめる"""
    try:
        value = round(float(str(text).strip()), 1)
    except ValueError:
        raise ValueError("上限は数字で入れてください") from None
    if not MIN_ARCHIVE_GB <= value <= MAX_ARCHIVE_GB:
        raise ValueError(f"上限は {MIN_ARCHIVE_GB}〜{MAX_ARCHIVE_GB} GB にしてください")
    return value


def sync_options(settings):
    """設定から、sync.py に渡す追加の引数を作る"""
    archive = settings["archive"]
    if archive.get("enabled"):
        return ["--archive-max-mb", str(int(float(archive["max_gb"]) * 1024))]
    return []


class SettingsStore:
    """admin.json を読み書きする。書き込みは一時ファイルに書いてから差し替える"""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        settings = self.load()
        if not settings["password"]:
            # 初めて起動したとき: 初期パスワード（admin）を設定する
            settings["password"] = hash_password(DEFAULT_PASSWORD)
            settings["password_is_default"] = True
            self.save(settings)

    def load(self):
        with self._lock:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        settings = copy.deepcopy(DEFAULTS)
        for key, value in data.items():
            if isinstance(settings.get(key), dict) and isinstance(value, dict):
                settings[key].update(value)
            else:
                settings[key] = value
        return settings

    def save(self, settings):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.tmp")
            tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)

    def update(self, change):
        """change(settings) で書き換えて保存する。書き換えた設定を返す"""
        settings = self.load()
        change(settings)
        self.save(settings)
        return settings
