"""Server-side access verification; no default token or bypass credential."""
from __future__ import annotations

from collections import OrderedDict, deque
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import time

COOKIE = 'grid_access'
ITERATIONS = 600_000
SESSION_SECONDS = 12 * 60 * 60


def token_hash(token: str) -> str:
    if not 8 <= len(token) <= 256:
        raise ValueError('访问 token 长度须为 8 至 256 个字符。')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', token.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest}'


def check_token(token: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt, digest = encoded.split('$')
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= int(iterations) <= 2_000_000:
            return False
        if len(salt) != 32 or len(digest) != 64 or not 8 <= len(token) <= 256:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', token.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, TypeError):
        return False


class AccessControl:
    def __init__(self, config_path: Path, *, clock=time.monotonic):
        self.clock = clock
        self.encoded = os.getenv('GRID_ACCESS_TOKEN_HASH', '')
        if not self.encoded and config_path.is_file():
            try:
                self.encoded = json.loads(config_path.read_text(encoding='utf-8'))['token_hash']
            except (OSError, ValueError, KeyError, TypeError):
                self.encoded = ''
        self.sessions: OrderedDict[str, float] = OrderedDict()
        self.attempts: OrderedDict[str, deque] = OrderedDict()
        self.global_attempts: deque = deque()

    @property
    def configured(self) -> bool:
        return bool(self.encoded)

    def valid_session(self, cookie: str | None) -> bool:
        if not cookie or len(cookie) > 100:
            return False
        key = hashlib.sha256(cookie.encode()).hexdigest()
        expiry = self.sessions.get(key)
        if expiry is None:
            return False
        if self.clock() >= expiry:
            self.sessions.pop(key, None)
            return False
        return True

    def allow_attempt(self, client: str) -> bool:
        now = self.clock()
        while self.global_attempts and self.global_attempts[0] <= now - 60:
            self.global_attempts.popleft()
        if len(self.global_attempts) >= 30:
            return False
        queue = self.attempts.setdefault(client, deque())
        self.attempts.move_to_end(client)
        while len(self.attempts) > 1024:
            self.attempts.popitem(last=False)
        while queue and queue[0] <= now - 300:
            queue.popleft()
        if len(queue) >= 5:
            return False
        # Reserve before slow hashing, so concurrent requests cannot bypass it.
        queue.append(now)
        self.global_attempts.append(now)
        return True

    def create_session(self, client: str, previous: str | None = None) -> str:
        self.logout(previous)
        self.attempts.pop(client, None)
        cookie = secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(cookie.encode()).hexdigest()] = self.clock() + SESSION_SECONDS
        while len(self.sessions) > 256:
            self.sessions.popitem(last=False)
        return cookie

    def logout(self, cookie: str | None):
        if cookie and len(cookie) <= 100:
            self.sessions.pop(hashlib.sha256(cookie.encode()).hexdigest(), None)
