"""Execution adapter contract (PRD M2).

``ExchangeAdapter`` is the venue-facing boundary for order placement. Every
venue (paper simulation or a live CCXT-backed exchange) implements the same
interface so the execution engine is venue-agnostic. Adapters are responsible
for deterministic (paper) or venue-accurate (live) fill simulation, quantity
rounding, and persisting the raw exchange response.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from ...domain.models import (
    MarketSnapshot,
    OrderIntent,
    OrderSide,
    OrderStatus,
    OrderType,
)


class PlaceOrderResult(BaseModel):
    """Normalised outcome of an order placement, independent of the venue."""

    model_config = ConfigDict(frozen=True)

    exchange_order_id: str
    status: OrderStatus
    filled_quantity: Decimal
    average_price: Decimal | None = None
    raw: dict = {}  # noqa: RUF012


class FetchOrderResult(BaseModel):
    """Normalised venual order state (used during reconciliation)."""

    model_config = ConfigDict(frozen=True)

    exchange_order_id: str
    status: OrderStatus
    filled_quantity: Decimal
    average_price: Decimal | None = None


class ExchangeAdapter(ABC):
    """Abstract venue boundary. All money is ``Decimal``; never binary float."""

    @abstractmethod
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
        """Submit one order and return its normalised fill state.

        ``snapshot`` is optionally supplied by the engine so deterministic
        (paper) venues can simulate fills; live venues ignore it.
        """

    @abstractmethod
    async def cancel_order(self, exchange_order_id: str, symbol: str) -> None:
        """Cancel an open order at the venue."""

    @abstractmethod
    async def fetch_order(self, exchange_order_id: str) -> FetchOrderResult:
        """Fetch the current state of a single order at the venue."""

    @abstractmethod
    async def fetch_balance(self) -> dict[str, Decimal]:
        """Return quote/base currency free balances keyed by symbol."""

    @abstractmethod
    async def fetch_open_orders(self) -> list[dict]:
        """Return open orders as a list of raw venue records."""

    def round_quantity(self, symbol: str, quantity: Decimal, side: OrderSide) -> Decimal:
        """Round a quantity to the venue's lot precision. Default: 8 decimals."""
        return quantity.quantize(Decimal("0.00000001"))


def quantity_from_intent(intent: OrderIntent) -> Decimal:
    """Convenience: the quantity an adapter should send for an intent."""
    return intent.quantity
