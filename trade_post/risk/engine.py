"""Risk engine. Pre-trade validation, kill switch, circuit breaker, sizing."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ..core.config import Settings
from ..domain.models import (
    MarketSnapshot,
    Money,
    OrderIntent,
    PortfolioSnapshot,
    RiskDecision,
    RiskState,
)
from ..persistence.database import Database
from ..persistence.repository import Repository

log = logging.getLogger(__name__)


class RiskEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = RiskState(session_id=uuid.uuid4().hex)
        self._db: Database | None = None

    def bind_database(self, db: Database) -> None:
        """Bind the persistence database used for risk-state reads and writes.

        Each call opens a short-lived, committed session internally, so late
        mutations (for example the kill switch) never touch a stale, closed
        session captured during startup.
        """
        self._db = db

    async def load(self) -> None:
        if self._db is None:
            return
        async with self._db.session() as session:
            loaded = await Repository(session).get_risk_state()
        if loaded is not None:
            self._state = loaded

    @property
    def state(self) -> RiskState:
        return self._state

    async def kill(self, reason: str) -> None:
        self._state.killed = True
        self._state.kill_reason = reason
        self._state.killed_at = datetime.now(timezone.utc)
        log.critical("KILL SWITCH: %s", reason)
        await self._persist()

    async def record_failure(self) -> None:
        self._state.failures_in_window += 1
        self._state.last_failure_at = datetime.now(timezone.utc)
        if self._state.failures_in_window >= self._settings.circuit_breaker_failure_threshold:
            self._state.circuit_open = True
            log.critical("Circuit breaker TRIP after %d failures", self._state.failures_in_window)
        await self._persist()

    async def record_success(self) -> None:
        self._state.failures_in_window = 0
        if self._state.circuit_open and self._state.last_failure_at is not None:
            elapsed = (datetime.now(timezone.utc) - self._state.last_failure_at).total_seconds()
            if elapsed >= self._settings.recovery_cooloff_sec:
                self._state.circuit_open = False
                log.info("Circuit breaker REARMED after cooloff")
        await self._persist()

    async def check_market(self, snap: MarketSnapshot) -> tuple:
        age = (datetime.now(timezone.utc) - snap.timestamp).total_seconds()
        if age > self._settings.max_stale_data_sec:
            return False, f"Stale market data: {age:.1f}s old"
        if snap.spread_bps is not None and snap.spread_bps > Decimal(str(self._settings.max_spread_pct * 100)):
            return False, f"Spread too wide: {snap.spread_bps}bps"
        return True, "market-ok"

    def check_state(self) -> tuple:
        if self._state.killed:
            return False, f"Kill switch active: {self._state.kill_reason}"
        if self._state.circuit_open:
            return False, "Circuit breaker open"
        return True, "state-ok"


    async def validate(
        self,
        intent: OrderIntent,
        snapshot: MarketSnapshot,
        portfolio: PortfolioSnapshot,
    ) -> RiskDecision:
        requested_notional = Money(
            amount=intent.quantity * snapshot.last_price, currency="USDT"
        )
        checks: dict = {}

        state_ok, state_reason = self.check_state()
        checks["state"] = state_ok
        if not state_ok:
            return self._reject(intent, requested_notional, state_reason, "STATE_BLOCKED", checks)

        market_ok, market_reason = await self.check_market(snapshot)
        checks["market"] = market_ok
        if not market_ok:
            return self._reject(intent, requested_notional, market_reason, "MARKET_BLOCKED", checks)

        if portfolio.total_equity.amount <= 0:
            return self._reject(intent, requested_notional, "No equity", "EQUITY_BLOCKED", checks)

        if self._state.starting_equity is None:
            self._state.starting_equity = portfolio.total_equity
            await self._persist()

        if self._state.starting_equity.amount > 0:
            dd = (self._state.starting_equity.amount - portfolio.total_equity.amount) / self._state.starting_equity.amount
            if dd * 100 >= Decimal(str(self._settings.max_daily_loss_pct)):
                await self.kill(f"Daily drawdown {float(dd*100):.2f}% exceeded")
                return self._reject(intent, requested_notional, "Daily drawdown exceeded", "DRAWDOWN_BLOCKED", checks)

        cap = portfolio.total_equity.amount * Decimal(str(self._settings.max_position_pct)) / Decimal("100")
        if requested_notional.amount > cap:
            return self._reject(intent, requested_notional,
                                f"Notional {requested_notional.amount} exceeds {self._settings.max_position_pct}% cap",
                                "SIZE_BLOCKED", checks)

        if requested_notional.amount > Decimal(str(self._settings.position_size_hard_cap_usd)):
            return self._reject(intent, requested_notional,
                                f"Notional exceeds hard cap ${self._settings.position_size_hard_cap_usd}",
                                "HARD_CAP", checks)

        atr = snapshot.indicators.get("atr") if snapshot.indicators else None
        if atr and float(atr) > 0 and self._settings.atr_stop_multiplier > 0:
            risk_per_unit = float(atr) * self._settings.atr_stop_multiplier
            dollar_risk = float(portfolio.total_equity.amount) * (self._settings.per_trade_risk_pct / 100.0)
            max_qty_by_risk = dollar_risk / risk_per_unit
            if float(intent.quantity) > max_qty_by_risk:
                new_qty = Decimal(str(round(max_qty_by_risk, 8)))
                if new_qty <= 0:
                    return self._reject(intent, requested_notional,
                                        "ATR-based risk sizing yielded zero quantity",
                                        "RISK_ZERO_QTY", checks)
                intent.quantity = new_qty
                requested_notional = Money(
                    amount=new_qty * snapshot.last_price, currency="USDT"
                )

        open_count = sum(1 for p in portfolio.positions if p.symbol == intent.symbol and p.quantity > 0)
        if open_count >= self._settings.max_open_orders:
            return self._reject(intent, requested_notional, "Max open orders reached", "ORDERS_BLOCKED", checks)

        if intent.limit_price and snapshot.last_price:
            deviation = abs(float(snapshot.last_price - intent.limit_price) / float(snapshot.last_price)) * 100.0
            if deviation > self._settings.max_price_deviation_pct:
                return self._reject(intent, requested_notional,
                                    f"Price deviation {deviation:.2f}% exceeds limit",
                                    "PRICE_DEVIATION", checks)

        return RiskDecision(
            approved=True,
            reason="All risk checks passed",
            code="APPROVED",
            requested_size_usd=requested_notional,
            capped_size_usd=requested_notional,
            checks=checks,
        )

    def _reject(self, intent, requested, reason, code, checks) -> RiskDecision:
        log.warning("RISK REJECT [%s] %s: %s", code, intent.symbol, reason)
        return RiskDecision(
            approved=False, reason=reason, code=code,
            requested_size_usd=requested, capped_size_usd=None, checks=checks,
        )

    async def _persist(self) -> None:
        if self._db is None:
            return
        try:
            async with self._db.session() as session:
                await Repository(session).upsert_risk_state(self._state)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to persist risk state: %s", exc)
