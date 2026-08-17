"""Integration tests for the authentication/authorization subsystem.

These tests exercise the real FastAPI application and its lifespan against a
temporary SQLite database. ``full_startup=False`` runs only the
database/migration/admin-bootstrap core so no network or background work runs.

Run with:  python -m pytest tests/test_auth_integration.py -q
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import trade_post.persistence.database as dbmodule
from trade_post.api.server import create_app
from trade_post.core.config import Settings

ADMIN_PASSWORD = "test-admin-password-123"


def _reset_db() -> None:
    """Drop any cached database singleton and dispose its engine."""
    existing = dbmodule._singleton
    if existing is not None:
        try:
            asyncio.run(existing.disconnect())
        except Exception:
            pass
    dbmodule._singleton = None


@pytest.fixture
def client():
    """A fresh app+DB isolated from the real trade_post.db for each test."""
    _reset_db()
    handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db_path = handle.name
    handle.close()
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{db_path}",
        drox_admin_password=ADMIN_PASSWORD,
        login_max_attempts_per_ip=3,
        login_lockout_minutes=15,
        session_ttl_minutes=480,
        app_env="development",
    )
    app = create_app(settings, full_startup=False)
    with TestClient(app, raise_server_exceptions=True) as test_client:
        yield test_client
    _reset_db()
    try:
        os.unlink(db_path)
    except OSError:
        pass


def _login(client, username: str = "admin", password: str = ADMIN_PASSWORD):
    return client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )


def _csrf_headers(client) -> dict:
    return {"x-csrf-token": client.cookies.get("drox_csrf") or ""}


def _revoke_session(client) -> None:
    """Revoke the client's active session directly in the DB."""
    sid = client.cookies.get("drox_session")
    db = dbmodule._singleton
    assert db is not None

    async def _run() -> None:
        from trade_post.persistence.repository import Repository

        async with db.session() as s:
            await Repository(s).revoke_session(sid)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# HTTP authentication / authorization
# ---------------------------------------------------------------------------


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] in ("ok", "degraded")


def test_valid_login_establishes_session(client):
    r = _login(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    # Session token must not appear in the JSON body.
    assert "session" not in body and "token" not in body and "password" not in body
    assert client.cookies.get("drox_session")
    assert client.cookies.get("drox_csrf")


def test_invalid_login_is_generic(client):
    # Neither unknown username nor wrong password reveals which was wrong.
    for creds in ({"username": "nobody", "password": "x"}, {"username": "admin", "password": "wrong-pass"}):
        r = client.post("/api/v1/auth/login", json=creds)
        assert r.status_code == 401
        assert "Invalid credentials" in r.text
        assert "nobody" not in r.text or "admin" not in r.text  # no username echo leak


def test_logout_revokes_server_session(client):
    assert _login(client).status_code == 200
    assert client.get("/api/v1/me").status_code == 200
    assert client.post("/api/v1/auth/logout").status_code == 200
    # Server-side session revoked -> /me now 401 even with the stale cookie.
    assert client.get("/api/v1/me").status_code == 401


def test_me_and_reads_require_auth(client):
    assert client.get("/api/v1/me").status_code == 401
    for path in (
        "/api/v1/portfolio",
        "/api/v1/orders",
        "/api/v1/events",
        "/api/v1/risk",
        "/api/v1/ai-decisions",
    ):
        assert client.get(path).status_code == 401, path


def test_authenticated_read_access(client):
    assert _login(client).status_code == 200
    assert client.get("/api/v1/me").status_code == 200
    for path in (
        "/api/v1/portfolio",
        "/api/v1/orders",
        "/api/v1/events",
        "/api/v1/risk",
        "/api/v1/ai-decisions",
    ):
        assert client.get(path).status_code == 200, path


def test_expired_session_rejected(client):
    assert _login(client).status_code == 200
    _revoke_session(client)  # simulate expiry/revocation server-side
    assert client.get("/api/v1/me").status_code == 401


def test_csrf_required_for_state_changes(client):
    assert _login(client).status_code == 200
    # Missing CSRF header -> 403.
    assert client.post("/api/v1/start", json={}).status_code == 403
    # Present CSRF header -> allowed.
    assert client.post("/api/v1/start", json={}, headers=_csrf_headers(client)).status_code == 200


def test_brute_force_rate_limit(client):
    # login_max_attempts_per_ip=3: 3 failures allowed, the 4th is throttled.
    for _ in range(3):
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "bad-pass"})
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 429


def test_role_authorization(client):
    assert _login(client).status_code == 200  # admin
    csrf = _csrf_headers(client)
    for name, role in (("viewer1", "viewer"), ("op1", "operator")):
        r = client.post(
            "/api/v1/admin/users",
            json={"username": name, "password": "pass-1234-5678", "role": role},
            headers=csrf,
        )
        assert r.status_code == 200, r.text
    assert client.get("/api/v1/admin/users").status_code == 200

    # Viewer: read-only.
    client.post("/api/v1/auth/logout")
    assert _login(client, "viewer1", "pass-1234-5678").status_code == 200
    viewer_csrf = _csrf_headers(client)
    assert client.get("/api/v1/me").json()["role"] == "viewer"
    assert client.get("/api/v1/portfolio").status_code == 200
    assert client.post("/api/v1/start", json={}, headers=viewer_csrf).status_code == 403
    assert client.post("/api/v1/kill", headers=viewer_csrf).status_code == 403
    assert client.get("/api/v1/admin/users").status_code == 403

    # Operator: controls allowed, admin still denied.
    client.post("/api/v1/auth/logout")
    assert _login(client, "op1", "pass-1234-5678").status_code == 200
    operator_csrf = _csrf_headers(client)
    assert client.post("/api/v1/start", json={}, headers=operator_csrf).status_code == 200
    assert client.post("/api/v1/stop", json={}, headers=operator_csrf).status_code == 200
    assert client.get("/api/v1/admin/users").status_code == 403


def test_admin_list_users_never_exposes_password(client):
    assert _login(client).status_code == 200
    r = client.get("/api/v1/admin/users")
    assert r.status_code == 200
    assert r.json()["users"]
    for u in r.json()["users"]:
        assert "password" not in u and "hash" not in u


# ---------------------------------------------------------------------------
# WebSocket authentication + AI model/provider privacy
# ---------------------------------------------------------------------------


def test_websocket_rejects_unauthenticated(client):
    # Server closes the un-accepted handshake with code 4001 -> WebSocketDisconnect.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/control") as ws:
            ws.send_json({"cmd": "PING"})
            ws.receive_json()


def test_websocket_authorized_ping(client):
    assert _login(client).status_code == 200
    with client.websocket_connect("/ws/control") as ws:
        ws.send_json({"cmd": "PING"})
        msg = ws.receive_json()
        assert msg["type"] == "pong"


def test_websocket_enforces_role(client):
    assert _login(client).status_code == 200  # admin
    csrf = _csrf_headers(client)
    client.post(
        "/api/v1/admin/users",
        json={"username": "viewer1", "password": "pass-1234-5678", "role": "viewer"},
        headers=csrf,
    )
    client.post("/api/v1/auth/logout")
    assert _login(client, "viewer1", "pass-1234-5678").status_code == 200
    with client.websocket_connect("/ws/control") as ws:
        ws.send_json({"cmd": "KILL"})
        msg = ws.receive_json()
        assert msg["type"] == "error"
        assert "role" in msg.get("error", "")


def test_websocket_operator_can_kill(client):
    assert _login(client).status_code == 200  # admin (operator class)
    with client.websocket_connect("/ws/control") as ws:
        ws.send_json({"cmd": "KILL"})
        msg = ws.receive_json()
        assert msg["type"] == "ack"
        assert msg["cmd"] == "KILL"


def test_ai_decisions_redact_model_fields(client):
    assert _login(client).status_code == 200
    db = dbmodule._singleton
    assert db is not None

    async def _insert() -> None:
        from trade_post.persistence.repository import Repository

        async with db.session() as s:
            await Repository(s).insert_ai_decision(
                id="fake-1",
                symbol="BTC/USDT",
                signal="LONG",
                conviction=7,
                confidence="0.8",
                rationale="test",
                raw_output="secret",
                model="gpt-oss:stealth-model",
                prompt_version="v42",
                validated=1,
                validation_errors="[]",
                timestamp=datetime.now(timezone.utc).isoformat(),
                trace_id="trc",
            )

    asyncio.run(_insert())
    r = client.get("/api/v1/ai-decisions")
    assert r.status_code == 200
    assert r.json()["decisions"]
    for d in r.json()["decisions"]:
        assert "model" not in d
        assert "raw_output" not in d
        assert "prompt_version" not in d
