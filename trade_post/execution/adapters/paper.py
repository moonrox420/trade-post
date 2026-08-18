"""Deterministic paper adapter (PRD M2).

Simulates fills from market snapshots with a configurable slippage model. The
adapter is stateless and reproducible: given the same snapshot, intent and
settings it produces the same fill price and exchange order id, so historical
replay tests are deterministic.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal

from ...core.config import Settings
from ...core.errors import ExchangeError
from ...domain.models import MarketSnapshot, OrderSide, OrderStatus, OrderType
from .base import ExchangeAdapter, FetchOrderResult, PlaceOrderResult


class PaperAdapter(ExchangeAdapter):
    """Stateless venue that fills deterministically against market snapshots."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def send_order(
        self,
        *,
        client_order_id: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        price: Decimal | None,
        quantity: Decimal,
        idempotency_key: str,
        snapshot: MarketSnapshot | None = None,
    ) -> PlaceOrderResult:
        if snapshot is None:
            raise ExchangeError("paper send_order requires a market snapshot")
        base = self._estimate_fill_price(snapshot, side, order_type)
        slippage = Decimal(str(self._settings.slippage_bps)) / Decimal("10000")
        if side is OrderSide.BUY:
            fill = base * (Decimal("1") + slippage)
        else:
            fill = base * (Decimal("1") - slippage)
        filled = self.round_quantity(symbol, quantity, side)
        order_id = "paper_" + hashlib.sha256(client_order_id.encode()).hexdigest()[:16]
        return PlaceOrderResult(
            exchange_order_id=order_id,
            status=OrderStatus.FILLED,
            filled_quantity=filled,
            average_price=fill,
            raw={"venue": "paper", "slippage_bps": self._settings.slippage_bps},
        )

    @staticmethod
    def _estimate_fill_price(snapshot: MarketSnapshot, side: OrderSide, order_type: OrderType) -> Decimal:
        if order_type is OrderType.MARKET:
            if side is OrderSide.BUY and snapshot.ask is not None:
                return snapshot.ask
            if side is OrderSide.SELL and snapshot.bid is not None:
                return snapshot.bid
            return snapshot.last_price
        return snapshot.last_price

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> None:
        return None

    async def fetch_order(self, exchange_order_id: str) -> FetchOrderResult:
        raise ExchangeError("paper orders are not queryable after instant fill")

    async def fetch_balance(self) -> dict[str, Decimal]:
        return {"USDT": Decimal(str(self._settings.paper_initial_equity))}

    async def fetch_open_orders(self) -> list[dict]:
        return []
