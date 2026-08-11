"""Order execution engine. Handles lifecycle, idempotency, slippage, reconciliation."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ..core.config import Settings
from ..core.errors import ExchangeError, OrderNotFound, OrderRejected
from ..domain.models import (
    Fill,
    MarketSnapshot,
    Money,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    SignalSide,
)
from ..market.service import MarketDataService
from ..persistence.repository import Repository
from ..risk.engine import RiskEngine

log = logging.getLogger(__name__)


class ExecutionEngine:
    def __init__(self, settings: Settings, market: MarketDataService, risk: RiskEngine, repo: Repository) -> None:
        self._settings = settings
        self._market = market
        self._risk = risk
        self._repo = repo
        self._lock = asyncio.Lock()
        self._paper_positions: dict = {}
        self._paper_equity: Decimal = Decimal(str(settings.paper_initial_equity))
        self._paper_balances: dict = {"USDT": self._paper_equity}

    @property
    def paper_equity(self) -> Decimal:
        return self._paper_equity

    @property
    def paper_balances(self) -> dict:
        return dict(self._paper_balances)

    @property
    def paper_positions(self) -> dict:
        return dict(self._paper_positions)


    async def submit(self, intent: OrderIntent, portfolio: PortfolioSnapshot,
                     trace_id: str | None = None) -> Order | None:
        async with self._lock:
            existing = await self._repo.get_order_by_idempotency(intent.idempotency_key)
            if existing is not None:
                log.info("Idempotent hit for key %s -> order %s", intent.idempotency_key, existing.id)
                return existing
            order = Order(
                id=intent.id, intent_id=intent.id, symbol=intent.symbol, side=intent.side,
                type=intent.type, quantity=intent.quantity, status=OrderStatus.PENDING,
                limit_price=intent.limit_price, stop_loss_pct=intent.stop_loss_pct,
                take_profit_pct=intent.take_profit_pct, idempotency_key=intent.idempotency_key,
                strategy_id=intent.strategy_id, signal=intent.signal, conviction=intent.conviction,
                rationale=intent.rationale, created_at=datetime.now(timezone.utc), trace_id=trace_id,
            )
            await self._repo.insert_order(order)
            await self._repo.update_order_status(order.id, OrderStatus.SUBMITTED)
            order.status = OrderStatus.SUBMITTED
            order.submitted_at = datetime.now(timezone.utc)
            try:
                snapshot = await self._market.get_snapshot(intent.symbol, use_cache=False)
            except ExchangeError as exc:
                log.error("Market fetch failed: %s", exc)
                await self._repo.update_order_status(order.id, OrderStatus.REJECTED, last_error=str(exc))
                await self._risk.record_failure()
                return None
            fill_price = self._estimate_fill_price(snapshot, intent)
            slippage_pct = self._calculate_slippage_pct(snapshot, intent, fill_price)
            if abs(float(fill_price - snapshot.last_price) / float(snapshot.last_price)) > 0.05:
                msg = f"Slippage violation: {slippage_pct:.2%}"
                await self._repo.update_order_status(order.id, OrderStatus.REJECTED, last_error=msg)
                await self._risk.record_failure()
                log.warning("REJECTED %s: %s", intent.symbol, msg)
                return None
            if self._settings.is_paper and not self._settings.has_exchange_credentials:
                order.exchange_order_id = f"paper_{uuid.uuid4().hex[:8]}"
                order.average_price = fill_price
                order.filled_quantity = intent.quantity
                order.status = OrderStatus.FILLED
                order.completed_at = datetime.now(timezone.utc)
                self._apply_paper_fill(order)
            else:
                try:
                    ex_id = await self._place_via_ccxt(intent, fill_price)
                    order.exchange_order_id = ex_id
                    order.average_price = fill_price
                    order.filled_quantity = intent.quantity
                    order.status = OrderStatus.FILLED
                    order.completed_at = datetime.now(timezone.utc)
                except (ExchangeError, OrderRejected) as exc:
                    await self._repo.update_order_status(order.id, OrderStatus.REJECTED, last_error=str(exc))
                    await self._risk.record_failure()
                    return None
            fill = Fill(
                order_id=order.id, exchange_order_id=order.exchange_order_id or "",
                symbol=order.symbol, side=order.side, quantity=order.filled_quantity,
                price=order.average_price or fill_price,
                fee=Money(amount=Decimal("0"), currency="USDT"), liquidity="taker",
            )
            await self._repo.insert_fill(fill)
            await self._repo.update_order_status(
                order.id, OrderStatus.FILLED, exchange_order_id=order.exchange_order_id,
                average_price=order.average_price, filled_quantity=order.filled_quantity,
            )
            await self._risk.record_success()
            log.info("EXECUTED %s %s qty=%s @ %s slip=%.3f%%",
                     order.side.value, order.symbol, order.filled_quantity, order.average_price, slippage_pct)
            return order

    def _estimate_fill_price(self, snap: MarketSnapshot, intent: OrderIntent) -> Decimal:
        if intent.type is OrderType.MARKET:
            if intent.side is OrderSide.BUY and snap.ask is not None:
                return snap.ask
            if intent.side is OrderSide.SELL and snap.bid is not None:
                return snap.bid
            return snap.last_price
        return intent.limit_price or snap.last_price

    def _calculate_slippage_pct(self, snap: MarketSnapshot, intent: OrderIntent,
                                 fill_price: Decimal) -> float:
        expected = snap.last_price
        if expected <= 0:
            return 0.0
        diff = float(fill_price - expected)
        if intent.side is OrderSide.BUY:
            return diff / float(expected) * 100.0
        return -diff / float(expected) * 100.0


    def _apply_paper_fill(self, order: Order) -> None:
        symbol = order.symbol
        if order.side is OrderSide.BUY:
            self._paper_balances["USDT"] = self._paper_balances.get("USDT", Decimal("0")) - (order.filled_quantity * (order.average_price or Decimal("0")))
            base = symbol.split("/")[0]
            self._paper_balances[base] = self._paper_balances.get(base, Decimal("0")) + order.filled_quantity
        else:
            self._paper_balances["USDT"] = self._paper_balances.get("USDT", Decimal("0")) + (order.filled_quantity * (order.average_price or Decimal("0")))
            base = symbol.split("/")[0]
            self._paper_balances[base] = self._paper_balances.get(base, Decimal("0")) - order.filled_quantity
        if order.signal is SignalSide.FLAT:
            self._paper_positions.pop(symbol, None)
        else:
            self._paper_positions[symbol] = {
                "side": order.signal.value,
                "quantity": order.filled_quantity,
                "entry_price": order.average_price,
                "leverage": Decimal("1"),
            }
        equity = self._paper_balances.get("USDT", Decimal("0"))
        for _, pos in self._paper_positions.items():
            equity += pos["quantity"] * pos["entry_price"]
        self._paper_equity = equity

    async def _place_via_ccxt(self, intent: OrderIntent, fill_price: Decimal) -> str:
        exch = self._market.exchange
        if exch is None:
            raise ExchangeError("Exchange not connected")
        try:
            params: dict = {}
            r = await exch.create_order(
                intent.symbol, intent.type.value, intent.side.value,
                float(intent.quantity), float(intent.limit_price) if intent.limit_price else None,
                params,
            )
            return str(r.get("id", ""))
        except Exception as exc:  # noqa: BLE001
            raise OrderRejected(f"CCXT create_order failed: {exc}") from exc

    async def cancel(self, order_id: str) -> None:
        async with self._lock:
            recent = await self._repo.list_recent_orders(200)
            target = next((o for o in recent if o.id == order_id), None)
            if target is None or target.is_terminal:
                raise OrderNotFound(f"Order {order_id} not found or terminal")
            if self._settings.is_paper and not self._settings.has_exchange_credentials:
                await self._repo.update_order_status(order_id, OrderStatus.CANCELLED)
                return
            exch = self._market.exchange
            if exch is None or not target.exchange_order_id:
                raise ExchangeError("Exchange not connected or missing exchange id")
            try:
                await exch.cancel_order(target.exchange_order_id, target.symbol)
                await self._repo.update_order_status(order_id, OrderStatus.CANCELLED)
            except Exception as exc:  # noqa: BLE001
                raise ExchangeError(f"Cancel failed: {exc}") from exc

    async def reconcile_orphans(self) -> list:
        if self._settings.is_paper and not self._settings.has_exchange_credentials:
            return []
        exch = self._market.exchange
        if exch is None:
            return []
        actions: list = []
        try:
            ex_orders = await exch.fetch_open_orders()
        except Exception as exc:  # noqa: BLE001
            log.warning("Reconcile fetch_open_orders failed: %s", exc)
            return []
        for eo in ex_orders:
            ex_id = str(eo.get("id"))
            recent = await self._repo.list_recent_orders(500)
            if not any(o.exchange_order_id == ex_id for o in recent):
                ts = eo.get("timestamp")
                if ts:
                    age = (time.time() * 1000 - float(ts)) / 1000.0
                    if age > self._settings.orphan_age_threshold_sec:
                        try:
                            await exch.cancel_order(ex_id, eo.get("symbol"))
                            actions.append({"order_id": ex_id, "action": "cancelled", "age_sec": age})
                        except Exception as exc:  # noqa: BLE001
                            log.warning("Failed to cancel orphan %s: %s", ex_id, exc)
        return actions
