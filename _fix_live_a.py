"""Throwaway: rewrite live.py (part A: header + retry helper)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

PATH = Path("trade_post/execution/adapters/live.py")

CONTENT_A = '''"""Live CCXT-backed execution adapter (PRD M2).

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
                delay = self._settings.exchange_retry_base_sec * (2 ** attempt)
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
'''

fd, tmp_path = tempfile.mkstemp(dir=str(PATH.parent), prefix=".live_tmp_", suffix=".py")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(CONTENT_A)
    os.replace(tmp_path, PATH)
except Exception:
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    raise

print("live.py part A written")
