"""FastAPI HTTP/WS server: real authentication, real data, no fakes.

Every sensitive endpoint enforces server-side authentication and authorization.
The browser is never trusted to determine auth state. Sessions live in
HttpOnly cookies; CSRF is enforced via double-submit tokens on state-changing
requests; the WebSocket handshake validates the session cookie before accept.
AI model/provider details are stripped from every API response.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import (
    Depends, FastAPI, HTTPException, Request, Response, WebSocket,
    WebSocketDisconnect, status,
)
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..core.config import Settings, load_settings
from ..core.logging_setup import get_logger, new_trace_id, trace_context
from ..domain.models import Event, EventSeverity
from ..persistence.database import get_database
from ..persistence.repository import Repository
from ..security.auth import (
    CurrentUser, _client_ip, csrf_cookie_name, hash_password,
    new_csrf_token, new_session_id, new_user_id, public_identity,
    require_admin, require_csrf, require_operator, resolve_session,
    session_expiry, set_csrf_cookie, verify_password,
)

log = get_logger(__name__)

COOKIE_NAME = "drox_session"

_AI_REDACT_FIELDS = frozenset({"model", "raw_output", "prompt_version"})


class LoginRequest(BaseModel):
    """Credential payload for the login endpoint."""

    username: str = Field(..., min_length=1, max_length=120)
    password: str = Field(..., min_length=1, max_length=512)


class LoginResponse(BaseModel):
    """Public user identity returned after successful authentication.

    Never includes password hashes, session tokens, or emails. The session
    token is delivered exclusively via the HttpOnly ``drox_session`` cookie.
    """

    username: str
    role: str


class AuthStateResponse(BaseModel):
    """Lightweight auth state for the frontend bootstrap."""

    authenticated: bool
    username: str | None = None
    role: str | None = None


class ChangePasswordRequest(BaseModel):
    """Self-service password change. Requires proof of current password."""

    current_password: str = Field(..., min_length=1, max_length=512)
    new_password: str = Field(..., min_length=8, max_length=512)


class CreateUserRequest(BaseModel):
    """Administrator-only user creation. No public self-registration."""

    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=512)
    role: str = Field(..., pattern="^(viewer|operator|admin)$")
    email: str | None = None


class UpdateUserRequest(BaseModel):
    """Administrator-only user role/status update."""

    role: str | None = Field(default=None, pattern="^(viewer|operator|admin)$")
    account_status: str | None = Field(default=None, pattern="^(active|disabled)$")


class SubscribeRequest(BaseModel):
    """Operator symbol subscription update."""

    symbols: list[str] = Field(..., min_length=1, max_length=20)


class AnalyzeRequest(BaseModel):
    """Operator on-demand analysis trigger."""

    symbol: str = Field(..., min_length=1, max_length=32)


def _dashboard_html() -> str:
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "index.html",
        Path.cwd() / "index.html",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return "<!doctype html><html><body><h1>Drox Trade Post</h1><p>Dashboard not found.</p></body></html>"


def _set_session_cookie(response: Response, settings: Settings, sid: str) -> None:
    secure = settings.app_env == "production"
    response.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        httponly=True,
        secure=secure,
        samesite="Strict" if secure else "Lax",
        max_age=settings.session_ttl_minutes * 60,
        path="/",
    )


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=settings.app_env == "production",
        samesite="Strict" if settings.app_env == "production" else "Lax",
        path="/",
    )


def _clear_csrf_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=csrf_cookie_name(settings),
        secure=settings.app_env == "production",
        samesite="Strict" if settings.app_env == "production" else "Lax",
        path="/",
    )


async def _record_auth_event(
    event_type: str,
    actor: str,
    payload: dict,
    severity: EventSeverity = EventSeverity.INFO,
) -> None:
    """Persist an audit event with a trace id. Never logs secrets."""
    with trace_context() as trace_id:
        db = get_database()
        async with db.session() as session:
            await Repository(session).insert_event(Event(
                type=event_type,
                severity=severity,
                actor=actor,
                payload=payload,
                trace_id=trace_id,
            ))


def _security_headers(response: Response, settings: Settings) -> None:
    """Apply security headers including a CSP compatible with the dashboard.

    The CSP allows Chart.js from jsdelivr, Google Fonts, inline scripts/styles
    (required by the existing HUD), and WebSocket connections to the same
    origin. ``frame-ancestors 'none'`` prevents clickjacking.
    """
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'"
    )


async def _check_login_rate_limit(repo: Repository, ip: str, settings: Settings) -> None:
    """IP-based brute-force throttle using existing settings. Never permanent."""
    window = datetime.utcnow() - timedelta(minutes=settings.login_lockout_minutes)
    attempts = await repo.count_failed_attempts_by_ip(ip, window)
    if attempts >= settings.login_max_attempts_per_ip:
        log.warning("Login rate limit hit for ip=%s", ip)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed login attempts. Please try again later.",
        )


def _strip_ai_fields(rows: list[dict]) -> list[dict]:
    """Remove AI model/provider fields from decision rows before API response."""
    return [
        {k: v for k, v in row.items() if k not in _AI_REDACT_FIELDS}
        for row in rows
    ]

# --- Application factory -------------------------------------------------

def create_app(settings: Settings | None = None, *, full_startup: bool = True) -> FastAPI:
    """Build the FastAPI application with complete authentication/authorization.

    The lifespan owns the Orchestrator (database, migrations, risk engine,
    admin bootstrap, market/AI connections, background loops). Pass
    ``full_startup=False`` to run only the database/migration/bootstrap core
    for fast, isolated integration tests.
    """
    from contextlib import asynccontextmanager

    from ..orchestrator.runtime import Orchestrator

    if settings is None:
        settings = load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        orch = Orchestrator(settings)
        app.state.orchestrator = orch
        app.state.settings = settings
        if full_startup:
            await orch.startup()
        else:
            await orch.startup_core()
        log.info("Drox Trade Post ready on port %d", settings.port)
        try:
            yield
        finally:
            await orch.shutdown()
            log.info("Drox Trade Post stopped")

    app = FastAPI(
        title="Drox Trade Post",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next) -> Response:
        response = await call_next(request)
        _security_headers(response, settings)
        return response

    # ---- PUBLIC: liveness ----
    @app.get("/health")
    async def health() -> dict:
        db = get_database()
        healthy = await db.healthcheck()
        return {"status": "ok" if healthy else "degraded"}

    # ---- PUBLIC: dashboard SPA ----
    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _dashboard_html()

    # ---- PUBLIC: auth-state probe ----
    @app.get("/api/v1/auth/state", response_model=AuthStateResponse)
    async def auth_state(request: Request) -> AuthStateResponse:
        sid = request.cookies.get(COOKIE_NAME)
        resolved = await resolve_session(sid) if sid else None
        if resolved is None:
            return AuthStateResponse(authenticated=False)
        return AuthStateResponse(
            authenticated=True,
            username=resolved["username"],
            role=resolved["role"],
        )
    # ---- LOGIN ----
    @app.post("/api/v1/auth/login", response_model=LoginResponse)
    async def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
        """Authenticate credentials and establish a server-side session.

        Sets the HttpOnly ``drox_session`` cookie and the ``drox_csrf`` cookie.
        The response body contains only the public identity — never the session
        token. Uses generic failure messaging to avoid leaking which credential
        was incorrect, and enforces IP-based brute-force throttling.

        Failed-attempt counters and failure audit records are written within the
        same transaction and committed even on rejection, so brute-force
        throttling and the audit trail survive failed logins. No nested DB
        session is opened (which would deadlock SQLite's single writer).
        """
        ip = _client_ip(request)
        db = get_database()
        failure = False
        sid = ""
        identity: LoginResponse | None = None
        async with db.session() as session:
            repo = Repository(session)
            await _check_login_rate_limit(repo, ip, settings)
            user = await repo.get_user_by_username(body.username.strip())
            ok = bool(user) and verify_password(body.password, user["password_hash"])
            await repo.record_login_attempt(ip, body.username, ok)
            reason = None
            if not ok:
                reason = "credentials"
            elif (user.get("account_status") or "active") != "active":
                reason = "disabled"
            else:
                locked_until = user.get("locked_until")
                if locked_until:
                    try:
                        if datetime.fromisoformat(locked_until) > datetime.utcnow():
                            reason = "locked"
                    except ValueError:
                        reason = None
            if reason is not None:
                failure = True
                await Repository(session).insert_event(Event(
                    type="login_failure",
                    severity=EventSeverity.WARNING,
                    actor=body.username,
                    payload={"ip": ip, "reason": reason},
                ))
            else:
                sid = new_session_id()
                now = datetime.utcnow()
                await repo.insert_session(
                    id=sid, user_id=user["id"], issued_at=now.isoformat(),
                    expires_at=session_expiry(settings, now).isoformat(),
                    ip=ip, user_agent=(request.headers.get("user-agent") or "")[:512],
                )
                await repo.reset_failed_login(user["id"])
                await repo.update_user_login(user["id"], now.isoformat())
                identity = LoginResponse(username=user["username"], role=user["role"])
        if failure:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials.")
        _set_session_cookie(response, settings, sid)
        set_csrf_cookie(response, settings, new_csrf_token())
        await _record_auth_event(
            "login_success", identity.username, {"ip": ip}, EventSeverity.INFO,
        )
        return identity
    # ---- LOGOUT ----
    @app.post("/api/v1/auth/logout")
    async def logout(request: Request, response: Response) -> dict:
        """Revoke the server-side session and clear auth cookies."""
        sid = request.cookies.get(COOKIE_NAME)
        actor = "unknown"
        if sid:
            resolved = await resolve_session(sid)
            actor = resolved["username"] if resolved else "unknown"
            db = get_database()
            async with db.session() as session:
                await Repository(session).revoke_session(sid)
            await _record_auth_event("logout", actor, {}, EventSeverity.INFO)
        _clear_session_cookie(response, settings)
        _clear_csrf_cookie(response, settings)
        return {"ok": True}

    # ---- CHANGE PASSWORD ----
    @app.post("/api/v1/auth/change-password")
    async def change_password(
        body: ChangePasswordRequest,
        request: Request,
        user: dict = Depends(CurrentUser(required=True)),
    ) -> dict:
        """Self-service password change requiring proof of the current password."""
        await require_csrf(request)
        if len(body.new_password) < settings.password_min_length:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                detail=f"Password must be at least {settings.password_min_length} characters.",
            )
        db = get_database()
        async with db.session() as session:
            repo = Repository(session)
            full = await repo.get_user_by_id(user["user_id"])
            if not full or not verify_password(body.current_password, full["password_hash"]):
                await _record_auth_event(
                    "password_change_failure", user["username"], {}, EventSeverity.WARNING,
                )
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Current password is incorrect.")
            await repo.update_user_password(user["user_id"], hash_password(body.new_password, settings))
        await _record_auth_event("password_changed", user["username"], {}, EventSeverity.INFO)
        return {"ok": True}

    # ---- ME ----
    @app.get("/api/v1/me")
    async def me(user: dict = Depends(CurrentUser(required=True))) -> dict:
        """Return the authenticated user's public identity (no sensitive data)."""
        return public_identity(user)

    # ---- AUTHENTICATED READ ENDPOINTS ----
    @app.get("/api/v1/portfolio")
    async def portfolio(user: dict = Depends(CurrentUser(required=True))) -> dict:
        db = get_database()
        async with db.session() as session:
            repo = Repository(session)
            equity = await repo.list_recent_equity(limit=200)
            orders = await repo.list_recent_orders(limit=25)
        latest = equity[-1]["equity"] if equity else "0"
        ts = equity[-1]["ts"] if equity else None
        return {
            "total_equity": latest,
            "timestamp": ts,
            "positions": [],
            "orders": [
                {"id": o.id, "symbol": o.symbol, "side": o.side.value,
                 "quantity": str(o.quantity), "filled": str(o.filled_quantity),
                 "status": o.status.value, "created_at": o.created_at.isoformat()}
                for o in orders
            ],
        }

    @app.get("/api/v1/orders")
    async def orders_endpoint(user: dict = Depends(CurrentUser(required=True))) -> dict:
        db = get_database()
        async with db.session() as session:
            orders = await Repository(session).list_recent_orders(50)
        return {"orders": [
            {"id": o.id, "symbol": o.symbol, "side": o.side.value,
             "quantity": str(o.quantity), "filled": str(o.filled_quantity),
             "status": o.status.value, "created_at": o.created_at.isoformat()}
            for o in orders
        ]}

    @app.get("/api/v1/events")
    async def events_endpoint(user: dict = Depends(CurrentUser(required=True))) -> dict:
        db = get_database()
        async with db.session() as session:
            events = await Repository(session).list_recent_events(100)
        return {"events": [
            {"id": e.id, "timestamp": e.timestamp.isoformat(), "type": e.type,
             "severity": e.severity.value, "actor": e.actor, "payload": e.payload}
            for e in events
        ]}

    @app.get("/api/v1/ai-decisions")
    async def ai_decisions(user: dict = Depends(CurrentUser(required=True))) -> dict:
        """Return recent decisions with AI model/provider fields stripped."""
        db = get_database()
        async with db.session() as session:
            rows = await Repository(session).list_recent_ai_decisions(50)
        return {"decisions": _strip_ai_fields(rows)}

    @app.get("/api/v1/risk")
    async def risk_state(user: dict = Depends(CurrentUser(required=True))) -> dict:
        db = get_database()
        async with db.session() as session:
            state = await Repository(session).get_risk_state()
        if not state:
            return {"killed": False, "circuit_open": False}
        return {
            "killed": state.killed, "kill_reason": state.kill_reason,
            "circuit_open": state.circuit_open,
            "failures_in_window": state.failures_in_window,
            "starting_equity": str(state.starting_equity.amount) if state.starting_equity else None,
        }
    # ---- OPERATOR CONTROLS (CSRF + operator/admin role) ----
    @app.post("/api/v1/kill")
    async def kill_switch(request: Request, user: dict = Depends(require_operator)) -> dict:
        """Emergency kill switch. Operator/admin only. CSRF required."""
        await require_csrf(request)
        orch = request.app.state.orchestrator
        await orch.risk.kill(f"Requested by {user['username']}")
        await _record_auth_event(
            "kill_switch_requested", user["username"],
            {"trace_id": new_trace_id()}, EventSeverity.CRITICAL,
        )
        return {"ok": True, "message": "Kill switch activated."}

    @app.post("/api/v1/start")
    async def start_autonomous(request: Request, user: dict = Depends(require_operator)) -> dict:
        """Start autonomous AI trading. Operator/admin only."""
        await require_csrf(request)
        await request.app.state.orchestrator.start_autonomous(user["username"])
        return {"ok": True, "autonomous": True}

    @app.post("/api/v1/stop")
    async def stop_autonomous(request: Request, user: dict = Depends(require_operator)) -> dict:
        """Stop autonomous AI trading. Operator/admin only."""
        await require_csrf(request)
        await request.app.state.orchestrator.stop_autonomous(user["username"])
        return {"ok": True, "autonomous": False}

    @app.post("/api/v1/subscribe")
    async def subscribe(body: SubscribeRequest, request: Request, user: dict = Depends(require_operator)) -> dict:
        """Subscribe to market data feeds. Operator/admin only."""
        await require_csrf(request)
        orch = request.app.state.orchestrator
        added = [s for s in body.symbols if orch.subscribe_symbol(s)]
        return {"ok": True, "subscribed": orch.settings.subscribed_symbols, "added": added}

    @app.post("/api/v1/unsubscribe")
    async def unsubscribe(body: SubscribeRequest, request: Request, user: dict = Depends(require_operator)) -> dict:
        """Unsubscribe from market data feeds. Operator/admin only."""
        await require_csrf(request)
        orch = request.app.state.orchestrator
        removed = [s for s in body.symbols if orch.unsubscribe_symbol(s)]
        return {"ok": True, "subscribed": orch.settings.subscribed_symbols, "removed": removed}

    @app.post("/api/v1/analyze")
    async def analyze(body: AnalyzeRequest, request: Request, user: dict = Depends(require_operator)) -> dict:
        """Trigger on-demand AI analysis. Operator/admin only. No AI details exposed."""
        await require_csrf(request)
        return await request.app.state.orchestrator.run_single_analysis(user["username"])
    # ---- ADMIN USER MANAGEMENT (CSRF + admin only) ----
    @app.get("/api/v1/admin/users")
    async def list_users(user: dict = Depends(require_admin)) -> dict:
        """List all users. Admin only. Password hashes are never returned."""
        db = get_database()
        async with db.session() as session:
            users = await Repository(session).list_users()
        return {"users": users}

    @app.post("/api/v1/admin/users")
    async def create_user(body: CreateUserRequest, request: Request, user: dict = Depends(require_admin)) -> dict:
        """Create a user. Admin only. No public self-registration."""
        await require_csrf(request)
        db = get_database()
        async with db.session() as session:
            repo = Repository(session)
            if await repo.get_user_by_username(body.username):
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Username already exists.")
            uid = new_user_id()
            await repo.insert_user(
                id=uid, username=body.username, email=body.email,
                password_hash=hash_password(body.password, settings),
                role=body.role, created_at=datetime.utcnow().isoformat(),
            )
        await _record_auth_event(
            "account_created", user["username"],
            {"new_user": body.username, "role": body.role}, EventSeverity.INFO,
        )
        return {"ok": True, "id": uid, "username": body.username, "role": body.role}

    @app.patch("/api/v1/admin/users/{user_id}")
    async def update_user(user_id: str, body: UpdateUserRequest, request: Request, user: dict = Depends(require_admin)) -> dict:
        """Update a user's role/status. Admin only."""
        await require_csrf(request)
        db = get_database()
        async with db.session() as session:
            repo = Repository(session)
            if not await repo.get_user_by_id(user_id):
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
            await repo.update_user_role_status(
                user_id, role=body.role, account_status=body.account_status,
            )
        await _record_auth_event(
            "administrative_action", user["username"],
            {"target": user_id, "role": body.role, "status": body.account_status},
            EventSeverity.INFO,
        )
        return {"ok": True}

    @app.delete("/api/v1/admin/users/{user_id}")
    async def delete_user(user_id: str, request: Request, user: dict = Depends(require_admin)) -> dict:
        """Delete a user and revoke sessions. Admin only. Cannot self-delete."""
        await require_csrf(request)
        if user_id == user["user_id"]:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Cannot delete your own account while logged in.")
        db = get_database()
        async with db.session() as session:
            repo = Repository(session)
            target = await repo.get_user_by_id(user_id)
            if not target:
                raise HTTPException(status.HTTP_404_NOT_FOUND, detail="User not found.")
            await repo.revoke_all_sessions_for_user(user_id)
            await repo.delete_user(user_id)
        await _record_auth_event(
            "account_deleted", user["username"],
            {"target": target["username"]}, EventSeverity.WARNING,
        )
        return {"ok": True}
    # ---- AUTHENTICATED WEBSOCKET: /ws/control ----
    @app.websocket("/ws/control")
    async def ws_control(websocket: WebSocket) -> None:
        """Cookie-authenticated control WebSocket.

        Validates the ``drox_session`` cookie before accepting the connection.
        Unauthenticated connections are rejected with close code 4001. Each
        command re-validates the session and enforces role-based authorization.
        If the session expires mid-connection, the socket is closed cleanly.
        """
        sid = websocket.cookies.get(COOKIE_NAME)
        resolved = await resolve_session(sid) if sid else None
        if resolved is None:
            await websocket.close(code=4001, reason="Not authenticated")
            return
        await websocket.accept()
        role = resolved["role"]
        username = resolved["username"]
        log.info("ws_control accepted user=%s role=%s", username, role)
        try:
            while True:
                data = await websocket.receive_text()
                # Re-validate the session on every message to catch expiry.
                if (await resolve_session(sid)) is None:
                    await websocket.send_json({"type": "error", "error": "session_expired"})
                    await websocket.close(code=4001, reason="Session expired")
                    return
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    await websocket.send_json({"type": "error", "error": "invalid_json"})
                    continue
                cmd = msg.get("cmd")
                orch = websocket.app.state.orchestrator
                with trace_context() as trace_id:
                    if cmd == "PING":
                        await websocket.send_json({"type": "pong", "trace": trace_id})
                    elif cmd == "GET_STATE":
                        db = get_database()
                        async with db.session() as session:
                            equity = await Repository(session).list_recent_equity(limit=1)
                        await websocket.send_json({
                            "type": "state", "equity": equity,
                            "autonomous": orch.is_autonomous_running,
                            "trace": trace_id,
                        })
                    elif cmd == "KILL":
                        if role not in ("admin", "operator"):
                            await websocket.send_json({"type": "error", "error": "insufficient_role"})
                            continue
                        await orch.risk.kill(f"Requested by {username}")
                        await _record_auth_event(
                            "kill_switch_requested", username,
                            {"trace_id": trace_id}, EventSeverity.CRITICAL,
                        )
                        await websocket.send_json({"type": "ack", "cmd": "KILL", "trace": trace_id})
                    elif cmd == "START_AUTO":
                        if role not in ("admin", "operator"):
                            await websocket.send_json({"type": "error", "error": "insufficient_role"})
                            continue
                        await orch.start_autonomous(username)
                        await websocket.send_json({"type": "system_status", "autonomous": True, "trace": trace_id})
                    elif cmd == "STOP_AUTO":
                        if role not in ("admin", "operator"):
                            await websocket.send_json({"type": "error", "error": "insufficient_role"})
                            continue
                        await orch.stop_autonomous(username)
                        await websocket.send_json({"type": "system_status", "autonomous": False, "trace": trace_id})
                    elif cmd == "ANALYZE":
                        if role not in ("admin", "operator"):
                            await websocket.send_json({"type": "error", "error": "insufficient_role"})
                            continue
                        result = await orch.run_single_analysis(username)
                        await websocket.send_json({"type": "ack", "cmd": "ANALYZE", "result": result, "trace": trace_id})
                    elif cmd in ("SUBSCRIBE", "UNSUBSCRIBE"):
                        if role not in ("admin", "operator"):
                            await websocket.send_json({"type": "error", "error": "insufficient_role"})
                            continue
                        for sym in (msg.get("symbols") or []):
                            (orch.subscribe_symbol if cmd == "SUBSCRIBE" else orch.unsubscribe_symbol)(sym)
                        await websocket.send_json({
                            "type": "ack", "cmd": cmd,
                            "subscribed": orch.settings.subscribed_symbols,
                            "trace": trace_id,
                        })
                    else:
                        await websocket.send_json({"type": "error", "error": f"unknown_cmd: {cmd}", "trace": trace_id})
        except WebSocketDisconnect:
            log.info("ws_control disconnected user=%s", username)
        except Exception as exc:  # noqa: BLE001
            log.warning("ws_control error user=%s: %s", username, exc)

    return app
