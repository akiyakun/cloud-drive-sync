"""パスワードの確認、ログインの状態（セッション）、ログインのロック"""

import hashlib
import hmac
import secrets
import time
from collections import deque

# scrypt: Python に最初から入っている、パスワード用の計算に時間がかかるハッシュ（総当たりを遅くする）
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return {"scheme": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P, "salt": salt.hex(), "hash": digest.hex()}


def verify_password(password, record):
    if not record or record.get("scheme") != "scrypt":
        return False
    digest = _scrypt(password, bytes.fromhex(record["salt"]), record["n"], record["r"], record["p"])
    return hmac.compare_digest(digest.hex(), record["hash"])


def same_token(a, b):
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


class Sessions:
    """ログインの状態。トークン（ブラウザの Cookie に入れる）ごとに、期限と CSRF 対策の合言葉を持つ

    サーバーのメモリにだけ置く（コンテナを再起動するとログアウトになる）
    """

    def __init__(self, lifetime_seconds):
        self.lifetime = lifetime_seconds
        self._sessions = {}

    def create(self):
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {"expires": time.time() + self.lifetime, "csrf": secrets.token_urlsafe(32), "flash": []}
        return token

    def get(self, token):
        session = self._sessions.get(token) if token else None
        if session and session["expires"] < time.time():
            del self._sessions[token]
            return None
        return session

    def delete(self, token):
        self._sessions.pop(token, None)

    def delete_others(self, keep_token):
        """パスワードを変えたとき、ほかのブラウザのログインを切る"""
        for token in [t for t in self._sessions if t != keep_token]:
            del self._sessions[token]


class LoginGuard:
    """window 秒以内に max_failures 回まちがえたら、lock 秒のあいだログインを受け付けない"""

    def __init__(self, max_failures, window_seconds, lock_seconds, clock=time.time):
        self.max_failures = max_failures
        self.window = window_seconds
        self.lock = lock_seconds
        self.clock = clock
        self.failures = deque()
        self.locked_until = 0.0

    def locked_seconds_left(self):
        return max(0, int(self.locked_until - self.clock()) + 1) if self.locked_until > self.clock() else 0

    def record_failure(self):
        """失敗を記録する。この失敗でロックしたら True"""
        now = self.clock()
        self.failures.append(now)
        while self.failures and self.failures[0] < now - self.window:
            self.failures.popleft()
        if len(self.failures) >= self.max_failures:
            self.failures.clear()
            self.locked_until = now + self.lock
            return True
        return False

    def record_success(self):
        self.failures.clear()
