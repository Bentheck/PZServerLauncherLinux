from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Deque


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
    return f"{salt.hex()}:{derived.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        salt_hex, hash_hex = encoded.split(":", 1)
    except ValueError:
        return False

    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(hash_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600_000)
    return hmac.compare_digest(actual, expected)


def ensure_password_policy(password: str) -> None:
    if len(password) < 12:
        raise ValueError("Passwords must be at least 12 characters long.")


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or "server"


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


class InMemoryRateLimiter:
    def __init__(self, limit: int = 5, window_seconds: int = 900) -> None:
        self.limit = limit
        self.window = timedelta(seconds=window_seconds)
        self._events: dict[str, Deque[datetime]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = datetime.now(timezone.utc)
        events = self._events[key]

        while events and now - events[0] > self.window:
            events.popleft()

        if len(events) >= self.limit:
            raise ValueError("Too many attempts. Please wait a bit before trying again.")

        events.append(now)
