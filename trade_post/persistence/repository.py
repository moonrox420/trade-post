"""Repository pattern. All persistence goes through here."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.models import (
    Event,
    EventSeverity,
    Fill,
    MarketSnapshot,
    Money,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    RiskState,
    SignalSide,
)

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _order_dict(o: Order) -> dict:
    return {"id": o.id, "intent_id": o.intent_id, "eoid": o.exchange_order_id,
            "symbol": o.symbol, "side": o.side.value, "type": o.type.value,
            "q": str(o.quantity), "fq": str(o.filled_quantity),
            "ap": str(o.average_price) if o.average_price else None,
            "status": o.status.value,
            "lp": str(o.limit_price) if o.limit_price else None,
            "sl": str(o.stop_loss_pct) if o.stop_loss_pct else None,
            "tp": str(o.take_profit_pct) if o.take_profit_pct else None,
            "ikey": o.idempotency_key, "sid": o.strategy_id,
            "sig": o.signal.value, "conv": o.conviction, "r": o.rationale,
            "ca": o.created_at.isoformat(),
            "sa": o.submitted_at.isoformat() if o.submitted_at else None,
            "ct": o.completed_at.isoformat() if o.completed_at else None,
            "le": o.last_error, "trc": o.trace_id}


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_order(self, order: Order) -> None:
        await self._s.execute(text(
            "INSERT INTO orders (id, intent_id, exchange_order_id, symbol, side, type,"
            " quantity, filled_quantity, average_price, status, limit_price, stop_loss_pct,"
            " take_profit_pct, idempotency_key, strategy_id, signal, conviction, rationale,"
            " created_at, submitted_at, completed_at, last_error, trace_id)"
            " VALUES (:id, :intent_id, :eoid, :symbol, :side, :type, :q, :fq, :ap, :status,"
            " :lp, :sl, :tp, :ikey, :sid, :sig, :conv, :r, :ca, :sa, :ct, :le, :trc)"),
            _order_dict(order))

    async def update_order_status(self, order_id, status, *, exchange_order_id=None,
                                average_price=None, filled_quantity=None, last_error=None) -> None:
        sets = ["status = :status"]
        params = {"id": order_id, "status": status.value}
        if exchange_order_id is not None:
            sets.append("exchange_order_id = :eid")
            params["eid"] = exchange_order_id
        if average_price is not None:
            sets.append("average_price = :ap")
            params["ap"] = str(average_price)
        if filled_quantity is not None:
            sets.append("filled_quantity = :fq")
            params["fq"] = str(filled_quantity)
        if last_error is not None:
            sets.append("last_error = :le")
            params["le"] = last_error
        if status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.EXPIRED):
            sets.append("completed_at = :ts")
            params["ts"] = _now_iso()
        await self._s.execute(text(f"UPDATE orders SET {', '.join(sets)} WHERE id = :id"), params)

    async def get_order_by_id(self, order_id):
        row = (await self._s.execute(text("SELECT * FROM orders WHERE id = :id"), {"id": order_id})).first()
        return _order_from_row(row) if row else None

    async def get_order_by_idempotency(self, key):
        row = (await self._s.execute(
            text("SELECT * FROM orders WHERE idempotency_key = :k"),
            {"k": key},
        )).first()
        return _order_from_row(row) if row else None

    async def list_open_orders(self):
        rows = (await self._s.execute(text(
            "SELECT * FROM orders WHERE status IN ('pending','submitted','partially_filled')"
            " ORDER BY created_at DESC"))).fetchall()
        return [o for o in (_order_from_row(r) for r in rows) if o is not None]


    async def insert_fill(self, fill: Fill) -> None:
        await self._s.execute(text(
            "INSERT INTO fills (id, order_id, exchange_order_id, symbol, side, quantity,"
            " price, fee_amount, fee_currency, liquidity, timestamp)"
            " VALUES (:id, :oid, :eoid, :sym, :side, :q, :p, :fa, :fc, :liq, :ts)"),
            {"id": fill.id, "oid": fill.order_id, "eoid": fill.exchange_order_id,
            "sym": fill.symbol, "side": fill.side.value,
            "q": str(fill.quantity), "p": str(fill.price),
            "fa": str(fill.fee.amount), "fc": fill.fee.currency,
            "liq": fill.liquidity, "ts": fill.timestamp.isoformat()})

    async def insert_market_snapshot(self, snap: MarketSnapshot) -> None:
        # INSERT OR REPLACE keeps this idempotent: get_snapshot() may return a
        # still-fresh *cached* snapshot whose second-resolution id was already
        # written, which would otherwise trip the primary key.
        await self._s.execute(text(
            "INSERT OR REPLACE INTO market_snapshots (id, symbol, timestamp, last_price, bid, ask,"
            " spread_bps, volume_24h, indicators, source)"
            " VALUES (:id, :sym, :ts, :lp, :bid, :ask, :sb, :v, :ind, :src)"),
            {"id": f"mkt_{snap.symbol}_{int(snap.timestamp.timestamp())}",
            "sym": snap.symbol, "ts": snap.timestamp.isoformat(),
            "lp": str(snap.last_price),
            "bid": str(snap.bid) if snap.bid else None,
            "ask": str(snap.ask) if snap.ask else None,
            "sb": str(snap.spread_bps) if snap.spread_bps else None,
            "v": str(snap.volume_24h) if snap.volume_24h else None,
            "ind": json.dumps(snap.indicators or {}), "src": snap.source})

    async def latest_market_snapshot(self, symbol):
        row = (await self._s.execute(
            text("SELECT * FROM market_snapshots WHERE symbol = :s ORDER BY timestamp DESC LIMIT 1"),
            {"s": symbol})).first()
        return _market_from_row(row) if row else None

    async def list_recent_equity(self, limit=200):
        rows = (await self._s.execute(
            text("SELECT timestamp, total_equity FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT :n"),
            {"n": limit})).fetchall()
        return list(reversed([{"ts": r.timestamp, "equity": float(r.total_equity)} for r in rows]))

    async def insert_portfolio_snapshot(self, snap: PortfolioSnapshot) -> None:
        await self._s.execute(text(
            "INSERT INTO portfolio_snapshots (id, timestamp, total_equity, available_margin,"
            " positions, base_balances, risk_adjusted_equity, margin_utilization, drawdown_pct)"
            " VALUES (:id, :ts, :te, :am, :pos, :bb, :rae, :mu, :dd)"),
            {"id": f"pf_{int(snap.timestamp.timestamp() * 1e6)}",
            "ts": snap.timestamp.isoformat(),
            "te": str(snap.total_equity.amount),
            "am": str(snap.available_margin.amount),
            "pos": json.dumps([{"symbol": p.symbol, "side": p.side.value,
                                "quantity": str(p.quantity), "entry_price": str(p.entry_price),
                                "mark_price": str(p.mark_price),
                                "unrealized_pnl": str(p.unrealized_pnl.amount),
                                "leverage": str(p.leverage)} for p in snap.positions]),
            "bb": json.dumps({k: str(v.amount) for k, v in snap.base_balances.items()}),
            "rae": str(snap.risk_adjusted_equity.amount),
            "mu": str(snap.margin_utilization),
            "dd": str(snap.drawdown_pct)})

    async def insert_ai_decision(self, *, id, symbol, signal, conviction, confidence,
                                rationale, raw_output, model, prompt_version, schema_version,
                                validated, validation_errors, timestamp, trace_id) -> None:
        ts = timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp)
        await self._s.execute(text(
            "INSERT INTO ai_decisions (id, symbol, signal, conviction, confidence, rationale,"
            " raw_output, model, prompt_version, schema_version, validated,"
            " validation_errors, timestamp, trace_id)"
            " VALUES (:id, :sym, :sig, :conv, :conf, :r, :ro, :m, :pv, :sv, :v, :ve, :ts, :trc)"),
            {"id": id, "sym": symbol, "sig": signal, "conv": conviction,
            "conf": str(confidence), "r": rationale, "ro": json.dumps(raw_output),
            "m": model, "pv": prompt_version, "sv": schema_version, "v": 1 if validated else 0,
            "ve": json.dumps(validation_errors), "ts": ts, "trc": trace_id})


    async def list_recent_ai_decisions(self, limit=50):
        rows = (await self._s.execute(
            text("SELECT * FROM ai_decisions ORDER BY timestamp DESC LIMIT :n"),
            {"n": limit},
        )).fetchall()
        return [dict(r._mapping) for r in rows]

    async def list_recent_orders(self, limit=50):
        rows = (await self._s.execute(
            text("SELECT * FROM orders ORDER BY created_at DESC LIMIT :n"),
            {"n": limit},
        )).fetchall()
        return [o for o in (_order_from_row(r) for r in rows) if o is not None]

    async def get_risk_state(self):
        row = (await self._s.execute(text("SELECT * FROM risk_state WHERE id = 1"))).first()
        if not row:
            return None
        return RiskState(
            killed=bool(row.killed), kill_reason=row.kill_reason,
            killed_at=datetime.fromisoformat(row.killed_at) if row.killed_at else None,
            starting_equity=Money(amount=Decimal(row.starting_equity)) if row.starting_equity else None,
            daily_realized_pnl=Money(amount=Decimal(row.daily_realized_pnl or "0")),
            session_id=row.session_id, failures_in_window=row.failures_in_window,
            circuit_open=bool(row.circuit_open),
            last_failure_at=datetime.fromisoformat(row.last_failure_at) if row.last_failure_at else None,
        )

    async def upsert_risk_state(self, state: RiskState) -> None:
        await self._s.execute(text(
            "INSERT OR REPLACE INTO risk_state (id, killed, kill_reason, killed_at,"
            " starting_equity, daily_realized_pnl, session_id, failures_in_window,"
            " circuit_open, last_failure_at) VALUES (1, :k, :kr, :ka, :se, :dp, :sid, :fw, :co, :lfa)"),
            {"k": 1 if state.killed else 0, "kr": state.kill_reason,
            "ka": state.killed_at.isoformat() if state.killed_at else None,
            "se": str(state.starting_equity.amount) if state.starting_equity else None,
            "dp": str(state.daily_realized_pnl.amount), "sid": state.session_id,
            "fw": state.failures_in_window, "co": 1 if state.circuit_open else 0,
            "lfa": state.last_failure_at.isoformat() if state.last_failure_at else None})

    async def insert_event(self, event: Event) -> None:
        await self._s.execute(text(
            "INSERT INTO events (id, timestamp, type, severity, actor, payload, trace_id, session_id)"
            " VALUES (:id, :ts, :t, :s, :a, :p, :trc, :sid)"),
            {"id": event.id, "ts": event.timestamp.isoformat(),
            "t": event.type, "s": event.severity.value, "a": event.actor,
            "p": json.dumps(event.payload), "trc": event.trace_id, "sid": event.session_id})

    async def list_recent_events(self, limit=100):
        rows = (await self._s.execute(
            text("SELECT * FROM events ORDER BY timestamp DESC LIMIT :n"), {"n": limit})).fetchall()
        return [Event(id=r.id, timestamp=datetime.fromisoformat(r.timestamp),
                    type=r.type, severity=EventSeverity(r.severity), actor=r.actor,
                    payload=json.loads(r.payload or "{}"), trace_id=r.trace_id, session_id=r.session_id)
                for r in rows]

    async def get_user_by_username(self, username):
        row = (await self._s.execute(
            text("SELECT * FROM users WHERE username = :u"), {"u": username})).first()
        return dict(row._mapping) if row else None

    async def get_user_by_id(self, user_id):
        row = (await self._s.execute(
            text("SELECT * FROM users WHERE id = :id"), {"id": user_id})).first()
        return dict(row._mapping) if row else None

    async def insert_user(self, *, id, username, email, password_hash, role, created_at) -> None:
        await self._s.execute(text(
            "INSERT INTO users (id, username, email, password_hash, role, created_at)"
            " VALUES (:id, :u, :e, :ph, :r, :ca)"),
            {"id": id, "u": username, "e": email, "ph": password_hash, "r": role, "ca": created_at})

    async def update_user_login(self, user_id, when) -> None:
        await self._s.execute(
            text("UPDATE users SET last_login_at = :w WHERE id = :id"),
            {"w": when, "id": user_id})

    async def update_user_password(self, user_id, password_hash) -> None:
        await self._s.execute(
            text("UPDATE users SET password_hash = :ph WHERE id = :id"),
            {"ph": password_hash, "id": user_id})

    async def insert_session(self, *, id, user_id, issued_at, expires_at, ip, user_agent) -> None:
        await self._s.execute(text(
            "INSERT INTO sessions (id, user_id, issued_at, expires_at, ip, user_agent)"
            " VALUES (:id, :uid, :ia, :ea, :ip, :ua)"),
            {"id": id, "uid": user_id, "ia": issued_at, "ea": expires_at, "ip": ip, "ua": user_agent})

    async def get_active_session(self, session_id):
        row = (await self._s.execute(
            text("SELECT * FROM sessions WHERE id = :id AND revoked = 0"),
            {"id": session_id})).first()
        if not row:
            return None
        if datetime.fromisoformat(row.expires_at) < datetime.now(UTC):
            return None
        return dict(row._mapping)

    async def revoke_session(self, session_id) -> None:
        await self._s.execute(
            text("UPDATE sessions SET revoked = 1 WHERE id = :id"), {"id": session_id})

    async def record_login_attempt(self, ip, username, success) -> None:
        await self._s.execute(text(
            "INSERT INTO login_attempts (ip, username, success, attempted_at)"
            " VALUES (:ip, :u, :s, :ts)"),
            {"ip": ip, "u": username, "s": 1 if success else 0, "ts": _now_iso()})



    async def count_failed_attempts_by_ip(self, ip: str, since: datetime) -> int:
        row = (await self._s.execute(
            text(
                "SELECT COUNT(*) AS c FROM login_attempts"
                " WHERE ip = :ip AND success = 0 AND attempted_at >= :since"
            ),
            {"ip": ip, "since": since.isoformat()},
        )).first()
        return int(row.c) if row else 0

    async def increment_failed_login(self, user_id: str) -> None:
        await self._s.execute(
            text(
                "UPDATE users SET failed_login_count = failed_login_count + 1"
                " WHERE id = :id"
            ),
            {"id": user_id},
        )

    async def reset_failed_login(self, user_id: str) -> None:
        await self._s.execute(
            text(
                "UPDATE users SET failed_login_count = 0, locked_until = NULL"
                " WHERE id = :id"
            ),
            {"id": user_id},
        )

    async def lock_user(self, user_id: str, until: datetime) -> None:
        await self._s.execute(
            text("UPDATE users SET locked_until = :until WHERE id = :id"),
            {"until": until.isoformat(), "id": user_id},
        )

    async def get_user_lock_status(self, user_id: str) -> dict | None:
        row = (await self._s.execute(
            text(
                "SELECT failed_login_count, locked_until FROM users WHERE id = :id"
            ),
            {"id": user_id},
        )).first()
        if not row:
            return None
        return {"failed_login_count": row.failed_login_count, "locked_until": row.locked_until}

    async def list_users(self) -> list[dict]:
        """Return all users without password hashes (for the admin management UI)."""
        rows = (await self._s.execute(text(
            "SELECT id, username, email, role, account_status, created_at,"
            " last_login_at, updated_at, failed_login_count, locked_until"
            " FROM users ORDER BY created_at ASC"
        ))).fetchall()
        return [dict(r._mapping) for r in rows]

    async def count_users_by_role(self, role: str) -> int:
        row = (await self._s.execute(
            text("SELECT COUNT(*) AS c FROM users WHERE role = :r"),
            {"r": role},
        )).first()
        return int(row.c) if row else 0

    async def update_user_role_status(self, user_id: str, *, role: str | None = None,
                                      account_status: str | None = None) -> None:
        """Update a user's role and/or account status (administrator operation)."""
        sets: list[str] = []
        params: dict = {"id": user_id}
        if role is not None:
            sets.append("role = :role")
            params["role"] = role
        if account_status is not None:
            sets.append("account_status = :status")
            params["status"] = account_status
        if not sets:
            return
        sets.append("updated_at = :ua")
        params["ua"] = _now_iso()
        await self._s.execute(
            text(f"UPDATE users SET {', '.join(sets)} WHERE id = :id"),
            params,
        )

    async def revoke_all_sessions_for_user(self, user_id: str) -> int:
        """Revoke every active session for a user. Returns the number revoked."""
        result = await self._s.execute(
            text("UPDATE sessions SET revoked = 1, revoked_at = :ts"
                 " WHERE user_id = :uid AND revoked = 0"),
            {"uid": user_id, "ts": _now_iso()},
        )
        return int(cast(CursorResult, result).rowcount)

    async def delete_user(self, user_id: str) -> None:
        """Permanently remove a user. Sessions cascade-delete via the schema."""
        await self._s.execute(
            text("DELETE FROM users WHERE id = :id"),
            {"id": user_id},
        )


def _order_from_row(row):
    if row is None:
        return None
    return Order(
        id=row.id, intent_id=row.intent_id, exchange_order_id=row.exchange_order_id,
        symbol=row.symbol, side=OrderSide(row.side), type=OrderType(row.type),
        quantity=Decimal(row.quantity), filled_quantity=Decimal(row.filled_quantity),
        average_price=Decimal(row.average_price) if row.average_price else None,
        status=OrderStatus(row.status),
        limit_price=Decimal(row.limit_price) if row.limit_price else None,
        stop_loss_pct=Decimal(row.stop_loss_pct) if row.stop_loss_pct else None,
        take_profit_pct=Decimal(row.take_profit_pct) if row.take_profit_pct else None,
        idempotency_key=row.idempotency_key, strategy_id=row.strategy_id,
        signal=SignalSide(row.signal), conviction=row.conviction, rationale=row.rationale,
        created_at=datetime.fromisoformat(row.created_at),
        submitted_at=datetime.fromisoformat(row.submitted_at) if row.submitted_at else None,
        completed_at=datetime.fromisoformat(row.completed_at) if row.completed_at else None,
        last_error=row.last_error, trace_id=row.trace_id,
    )


def _market_from_row(row) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=row.symbol, timestamp=datetime.fromisoformat(row.timestamp),
        last_price=Decimal(row.last_price),
        bid=Decimal(row.bid) if row.bid else None,
        ask=Decimal(row.ask) if row.ask else None,
        spread_bps=Decimal(row.spread_bps) if row.spread_bps else None,
        volume_24h=Decimal(row.volume_24h) if row.volume_24h else None,
        indicators=json.loads(row.indicators or "{}"),
        source=row.source,
    )




