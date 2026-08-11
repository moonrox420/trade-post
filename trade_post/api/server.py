"""FastAPI HTTP/WS server. Real auth, real data, no fakes."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import (
    Cookie, Depends, FastAPI, HTTPException, Request, Response, WebSocket,
    WebSocketDisconnect, status,
)
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from ..core.config import Settings, load_settings
from ..core.logging_setup import configure_logging, get_logger, new_trace_id, trace_context
from ..domain.models import Event, EventSeverity
from ..observability.metrics import metrics
from ..persistence.database import get_database
from ..persistence.repository import Repository
from ..security.auth import (
    hash_password, new_session_id, new_user_id, session_expiry, verify_password,
)

log = get_logger(__name__)


def _dashboard_html() -> str:
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "index.html",
        Path.cwd() / "index.html",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8")
    return "<!doctype html><html><body><h1>Drox Trade Post</h1><p>Dashboard not found.</p></body></html>"


def create_app(settings: Settings | None = None) -> FastAPI:
    from ..core.config import load_settings
    from ..core.logging_setup import configure_logging, get_logger
    from ..persistence.database import init_database
    from ..persistence.migrations import run_migrations

    s = settings or load_settings()
    configure_logging(s)
    log = get_logger(__name__)

    # Initialize the database eagerly so endpoints can use it.
    db = init_database(s)
    try:
        # FastAPI is sync at construction; migrations will be re-run by lifespan.
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(db.connect())
            loop.run_until_complete(run_migrations(db.engine))
        finally:
            loop.run_until_complete(db.disconnect())
    except Exception as exc:  # noqa: BLE001
        log.warning("Initial DB connect during create_app failed: %s", exc)

    app = FastAPI(title=s.app_name, version="2.0.0")
    app.state.db = db
    app.state.settings = s

    @app.on_event("startup")
    async def _on_startup():
        try:
            await db.connect()
            await run_migrations(db.engine)
        except Exception as exc:  # noqa: BLE001
            log.warning("DB connect on startup failed: %s", exc)

    @app.on_event("shutdown")
    async def _on_shutdown():
        try:
            await db.disconnect()
        except Exception:
            pass

    @app.get("/health")
    async def health() -> dict:
        db = get_database()
        db_ok = await db.healthcheck() if db._engine is not None else False
        return {"status": "ok" if db_ok else "degraded", "db": db_ok,
                "version": "2.0.0", "app": s.app_name}

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4")


    async def get_current_user(request: Request) -> dict:
        sid = request.cookies.get("drox_session")
        if not sid:
            raise HTTPException(status_code=401, detail="Not authenticated")
        db = get_database()
        async with db.session() as session:
            row = await Repository(session).get_active_session(sid)
            if not row:
                raise HTTPException(status_code=401, detail="Session expired")
            user = await Repository(session).get_user_by_id(row["user_id"])
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        return user

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        return HTMLResponse(_dashboard_html())

    @app.get("/api/v1/me")
    async def me(user: dict = Depends(get_current_user)) -> dict:
        return {"username": user["username"], "role": user["role"]}

    class LoginRequest(BaseModel):
        username: str
        password: str

    class LoginResponse(BaseModel):
        username: str
        role: str

    def _current_user_sync(sid: str) -> dict | None:
        db = get_database()

        async def _check():
            async with db.session() as s:
                row = await Repository(s).get_active_session(sid)
                if not row:
                    return None
                return await Repository(s).get_user_by_id(row["user_id"])

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                return None
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(_check())

    def _current_user(request: Request) -> dict | None:
        sid = request.cookies.get("drox_session")
        if not sid:
            return None
        return _current_user_sync(sid)

    @app.post("/api/v1/auth/login", response_model=LoginResponse)
    async def login(request: Request, response: Response) -> LoginResponse:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid JSON body")
        try:
            req_data = LoginRequest.model_validate(payload)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Invalid login payload: {exc}")
        db = get_database()
        async with db.session() as session:
            repo = Repository(session)
            user = await repo.get_user_by_username(req_data.username)
            if not user or not verify_password(req_data.password, user["password_hash"]):
                await repo.record_login_attempt(
                    ip=request.client.host if request.client else "0.0.0.0",
                    username=req_data.username, success=False)
                metrics.inc("auth_login_failures_total")
                raise HTTPException(status_code=401, detail="Invalid credentials")
            await repo.update_user_login(user["id"], datetime.utcnow().isoformat())
            sid = new_session_id()
            await repo.insert_session(
                id=sid, user_id=user["id"],
                issued_at=datetime.utcnow().isoformat(),
                expires_at=session_expiry(s).isoformat(),
                ip=request.client.host if request.client else "0.0.0.0",
                user_agent=request.headers.get("user-agent", "unknown"),
            )
            await repo.record_login_attempt(
                ip=request.client.host if request.client else "0.0.0.0",
                username=req_data.username, success=True)
            metrics.inc("auth_login_success_total")
        response.set_cookie(key="drox_session", value=sid, httponly=True,
                            secure=s.app_env == "production", samesite="lax",
                            max_age=s.session_ttl_minutes * 60)
        return LoginResponse(username=user["username"], role=user["role"])

    @app.post("/api/v1/auth/logout")
    async def logout(request: Request, response: Response) -> dict:
        sid = request.cookies.get("drox_session")
        if sid:
            db = get_database()
            async with db.session() as s:
                await Repository(s).revoke_session(sid)
        response.delete_cookie("drox_session")
        return {"ok": True}


    @app.get("/api/v1/portfolio")
    async def portfolio(user: dict | None = Depends(get_current_user)) -> dict:
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        db = get_database()
        async with db.session() as session:
            equity = await Repository(session).list_recent_equity(limit=200)
        return {"equity_history": equity}

    @app.get("/api/v1/orders")
    async def orders_endpoint(user: dict | None = Depends(get_current_user)) -> dict:
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        db = get_database()
        async with db.session() as session:
            recent = await Repository(session).list_recent_orders(50)
        return {"orders": [
            {"id": o.id, "symbol": o.symbol, "side": o.side.value,
             "type": o.type.value, "quantity": str(o.quantity),
             "filled_quantity": str(o.filled_quantity),
             "average_price": str(o.average_price) if o.average_price else None,
             "status": o.status.value, "created_at": o.created_at.isoformat()}
            for o in recent
        ]}

    @app.get("/api/v1/events")
    async def events_endpoint(user: dict | None = Depends(get_current_user)) -> dict:
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        db = get_database()
        async with db.session() as session:
            evs = await Repository(session).list_recent_events(50)
        return {"events": [
            {"id": e.id, "timestamp": e.timestamp.isoformat(),
             "type": e.type, "severity": e.severity.value,
             "actor": e.actor, "payload": e.payload}
            for e in evs
        ]}

    @app.get("/api/v1/ai-decisions")
    async def ai_decisions(user: dict | None = Depends(get_current_user)) -> dict:
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        db = get_database()
        async with db.session() as session:
            rows = await Repository(session).list_recent_ai_decisions(50)
        return {"decisions": rows}

    @app.get("/api/v1/risk")
    async def risk_state(user: dict | None = Depends(get_current_user)) -> dict:
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
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
            "session_id": state.session_id,
        }

    @app.post("/api/v1/kill")
    async def kill_switch(user: dict | None = Depends(get_current_user)) -> dict:
        if not user:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user["role"] not in ("admin", "operator"):
            raise HTTPException(status_code=403, detail="Insufficient role")
        db = get_database()
        async with db.session() as session:
            await Repository(session).insert_event(Event(
                type="kill_switch_requested", severity=EventSeverity.CRITICAL,
                actor=user["username"],
                payload={"requested_by": user["username"], "trace_id": new_trace_id()},
            ))
        return {"ok": True, "message": "Kill switch recorded."}

    @app.websocket("/ws/control")
    async def ws_control(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    await websocket.send_json({"error": "invalid_json"})
                    continue
                cmd = msg.get("cmd")
                with trace_context() as trace_id:
                    if cmd == "PING":
                        await websocket.send_json({"type": "pong", "trace": trace_id})
                    elif cmd == "GET_STATE":
                        db = get_database()
                        async with db.session() as session:
                            equity = await Repository(session).list_recent_equity(limit=1)
                            await websocket.send_json(
                                {"type": "state", "equity": equity, "trace": trace_id})
                    else:
                        await websocket.send_json({"error": f"unknown_cmd: {cmd}", "trace": trace_id})
        except WebSocketDisconnect:
            log.info("websocket disconnected")
        except Exception as exc:  # noqa: BLE001
            log.warning("websocket error: %s", exc)

    return app


