"""Security: bcrypt password hashing and session helpers."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import uuid
from datetime import datetime, timedelta

from ..core.config import Settings
from ..core.errors import AuthenticationError


def hash_password(plain: str, settings: Settings) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256. bcrypt is optional."""
    if not plain:
        raise AuthenticationError("Empty password")
    salt = secrets.token_bytes(16)
    iterations = max(100_000, 2 ** settings.bcrypt_work_factor // 64)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    return f"pbkdf2-sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """Constant-time comparison; returns False on any malformed input."""
    if not plain or not stored:
        return False
    try:
        algo, iters_s, salt_hex, digest_hex = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2-sha256":
        return False
    try:
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def new_session_id() -> str:
    return secrets.token_urlsafe(48)


def new_user_id() -> str:
    return uuid.uuid4().hex


def session_expiry(settings: Settings, now: datetime | None = None) -> datetime:
    now = now or datetime.utcnow()
    return now + timedelta(minutes=settings.session_ttl_minutes)


def sign_state_token(payload: str, secret: str) -> str:
    """HMAC-SHA256 over a payload. Used for cheap state tokens (e.g. CSRF)."""
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
    return sig.hexdigest()
