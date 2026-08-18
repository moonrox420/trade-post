"""Live CCXT-backed execution adapter (PRD M2).

Wraps a CCXT async exchange with a retry policy (exponential backoff), venue
rate-limit awareness, error normalisation and order-status mapping. Each
placement captures its raw venue response for audit persistence.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

import ccxt.async_support as ccxt

from ...core.config import Settings
from ...core.errors import ExchangeError, OrderRejected
from ...domain.models import OrderSide, OrderStatus, OrderType
from ...market.service import MarketDataService
from .base import ExchangeAdapter, FetchOrderResult, PlaceOrderResult

log = logging.getLogger(__name__)

_TRANSIENT = (
    ccxt.NetworkError,
    ccxt.ExchangeNotAvailable,
    ccxt.RateLimitExceeded,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
)

_STATUS_MAP: dict[str, OrderStatus] = {
    "open": OrderStatus.SUBMITTED,
    "closed": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
    "expired": OrderStatus.EXPIRED,
    "partial": OrderStatus.PARTIALLY_FILLED,
}


def map_order_status(raw_status: str | None) -> OrderStatus:
    """Map a CCXT order status to the local status enum."""
    if not raw_status:
        return OrderStatus.SUBMITTED
    return _STATUS_MAP.get(raw_status.lower(), OrderStatus.PARTIALLY_FILLED)


class LiveAdapter(ExchangeAdapter):
    """CCXT-backed venue for real order placement."""

    def __init__(self, settings: Settings, market: MarketDataService) -> None:
        self._settings = settings
        self._market = market

    def _exchange(self) -> ccxt.Exchange:
        exch = self._market.exchange
        if exch is None:
            raise ExchangeError("Live exchange not connected")
        return exch

    async def _run(self, label: str, call):
        """Execute ``call`` with exponential-backoff retry on transient errors."""
        last: Exception | None = None
        for attempt in range(self._settings.exchange_max_retries + 1):
            try:
                return await call()
            except _TRANSIENT as exc:  # type: ignore[attr-defined]
                last = exc
                if attempt >= self._settings.exchange_max_retries:
                    raise ExchangeError(f"{label} failed after retries: {exc}") from exc
                delay = self._settings.exchange_retry_base_sec * (2**attempt)
                log.warning(
                    "%s transient error (attempt %d), retrying in %.1fs: %s",
                    label,
                    attempt + 1,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        if last is not None:
            raise ExchangeError(f"{label} failed: {last}") from last
        raise ExchangeError(f"{label} failed with no retries available")

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
        snapshot: object | None = None,
    ) -> PlaceOrderResult:
        exch = self._exchange()

        async def _place() -> dict:
            return await exch.create_order(
                symbol,
                order_type.value,
                side.value,
                float(quantity),
                float(price) if price is not None else None,
                {"clientOrderId": client_order_id},
            )

        try:
            raw = await self._run("create_order", _place)
        except ccxt.InsufficientFunds as exc:
            raise OrderRejected(f"insufficient funds on {symbol}: {exc}") from exc
        except ccxt.InvalidOrder as exc:
            raise OrderRejected(f"invalid order on {symbol}: {exc}") from exc

        status = map_order_status(str(raw.get("status")))
        filled = Decimal(str(raw.get("filled") or 0))
        avg = raw.get("average") or raw.get("price")
        return PlaceOrderResult(
            exchange_order_id=str(raw.get("id", "")),
            status=status,
            filled_quantity=filled,
            average_price=Decimal(str(avg)) if avg is not None else None,
            raw=dict(raw),
        )

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> None:
        exch = self._exchange()

        async def _cancel() -> None:
            await exch.cancel_order(exchange_order_id, symbol)

        try:
            await self._run("cancel_order", _cancel)
        except ccxt.OrderNotFound as exc:
            raise ExchangeError(f"order not found to cancel: {exc}") from exc

    async def fetch_order(self, exchange_order_id: str) -> FetchOrderResult:
        exch = self._exchange()

        async def _fetch() -> dict:
            return await exch.fetch_order(exchange_order_id)

        raw = await self._run("fetch_order", _fetch)
        return FetchOrderResult(
            exchange_order_id=exchange_order_id,
            status=map_order_status(str(raw.get("status"))),
            filled_quantity=Decimal(str(raw.get("filled") or 0)),
            average_price=(Decimal(str(raw["average"])) if raw.get("average") is not None else None),
        )

    async def fetch_balance(self) -> dict[str, Decimal]:
        exch = self._exchange()

        async def _fetch() -> dict:
            return await exch.fetch_balance()

        raw = await self._run("fetch_balance", _fetch)
        free = raw.get("free") or {}
        return {key: Decimal(str(value)) for key, value in free.items() if value is not None}

    async def fetch_open_orders(self) -> list[dict]:
        exch = self._exchange()

        async def _fetch() -> list:
            return await exch.fetch_open_orders()

        return await self._run("fetch_open_orders", _fetch)
