"""Security: password hashing, session verification, CSRF tokens, and auth dependencies."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic

from ..core.config import Settings
from ..core.errors import AuthenticationError


class SessionStore(Protocol):
    """Minimal interface required by auth helpers from a repository."""

    async def get_active_session(self, session_id: str) -> dict | None: ...
    async def get_user_by_id(self, user_id: str) -> dict | None: ...


def hash_password(plain: str, settings: Settings) -> str:
    """Hash a password using PBKDF2-HMAC-SHA256. bcrypt is optional."""
    if not plain:
        raise AuthenticationError("Empty password")
    salt = secrets.token_bytes(16)
    iterations = max(100_000, 2**settings.bcrypt_work_factor // 64)
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


def new_csrf_token() -> str:
    """Return a high-entropy token suitable for double-submit CSRF protection."""
    return secrets.token_urlsafe(32)


def session_expiry(settings: Settings, now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    return now + timedelta(minutes=settings.session_ttl_minutes)


def sign_state_token(payload: str, secret: str) -> str:
    """HMAC-SHA256 over a payload. Used for cheap state tokens (e.g. CSRF)."""
    sig = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256)
    return sig.hexdigest()


def _client_ip(request: Request) -> str:
    """Extract the most trustworthy client IP without trusting spoofed headers blindly."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        candidate = parts[-1] if request.app.state.settings.app_env == "production" else parts[0]
        try:
            ipaddress.ip_address(candidate)
            return candidate
        except ValueError:
            pass
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        try:
            ipaddress.ip_address(real_ip.strip())
            return real_ip.strip()
        except ValueError:
            pass
    return request.client.host if request.client else "unknown"


async def resolve_session(sid: str) -> dict | None:
    """Resolve a session id to a validated user identity, or ``None``.

    Centralizes every session-validation rule so the HTTP dependency path and
    the WebSocket handshake share one source of truth. Returns ``None`` (never
    raises) for missing, expired, revoked, locked, inactive, or orphaned
    sessions so each caller can map the failure to the appropriate status code
    without leaking which specific condition occurred.
    """
    if not sid:
        return None
    from ..persistence.database import get_database
    from ..persistence.repository import Repository

    db = get_database()
    async with db.session() as session:
        store: SessionStore = Repository(session)
        session_row = await store.get_active_session(sid)
        if not session_row:
            return None
        user = await store.get_user_by_id(session_row["user_id"])
        if not user:
            return None
        locked_until = user.get("locked_until")
        if locked_until:
            try:
                if datetime.fromisoformat(locked_until) > datetime.now(UTC):
                    return None
            except ValueError:
                return None
        if (user.get("account_status") or "active") != "active":
            return None
        return {
            "user_id": user["id"],
            "username": user["username"],
            "role": user["role"],
            "session_id": sid,
            "ip": session_row.get("ip"),
        }


class CurrentUser:
    """Dependency factory exposing the authenticated user from a session cookie."""

    def __init__(self, required: bool = True) -> None:
        self._required = required

    async def __call__(self, request: Request) -> dict | None:
        sid = request.cookies.get("drox_session")
        if not sid:
            if self._required:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
            return None
        resolved = await resolve_session(sid)
        if resolved is None:
            if self._required:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED, detail="Session invalid or expired"
                )
            return None
        return resolved


def require_role(*roles: str):
    """Dependency factory that enforces role-based authorization."""

    async def _check(user: dict = Depends(CurrentUser(required=True))) -> dict:
        if user["role"] not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient privileges",
            )
        return user

    return _check


# Convenience role dependencies. Operator capabilities are available to
# operators and administrators; administrator capabilities are admin-only.
require_operator = require_role("operator", "admin")
require_admin = require_role("admin")


async def require_csrf(request: Request) -> None:
    """Validate the double-submit CSRF token for state-changing browser requests.

    Reads settings from ``request.app.state.settings`` (set by the server
    lifespan) and enforces that the ``x-csrf-token`` header matches the
    ``drox_csrf`` cookie using a constant-time comparison.
    """
    settings: Settings = request.app.state.settings
    validate_csrf_token(request, settings)


def public_identity(user: dict) -> dict:
    """Return the non-sensitive public identity for API responses.

    Never includes password hashes, session tokens, emails, or any internal
    identifier beyond the opaque user id. The dashboard only needs the
    operator username and role.
    """
    return {"id": user["user_id"], "username": user["username"], "role": user["role"]}


def csrf_cookie_name(settings: Settings) -> str:
    return "drox_csrf"


def csrf_header_name() -> str:
    return "x-csrf-token"


def set_csrf_cookie(response, settings: Settings, token: str) -> None:
    secure = settings.app_env == "production"
    response.set_cookie(
        key=csrf_cookie_name(settings),
        value=token,
        httponly=False,
        secure=secure,
        samesite="Strict" if secure else "Lax",
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )


def validate_csrf_token(request: Request, settings: Settings) -> None:
    """Validate double-submit CSRF token from cookie + header for state-changing requests."""
    cookie_token = request.cookies.get(csrf_cookie_name(settings))
    header_token = request.headers.get(csrf_header_name())
    if not cookie_token or not header_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token missing")
    if not secrets.compare_digest(cookie_token, header_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF token mismatch")


http_basic = HTTPBasic(auto_error=False)
