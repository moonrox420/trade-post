"""Execution adapters (PRD M2). Select the venue adapter at runtime."""

from __future__ import annotations

from ...core.config import Settings
from ...market.service import MarketDataService
from .base import ExchangeAdapter, FetchOrderResult, PlaceOrderResult
from .live import LiveAdapter
from .paper import PaperAdapter

__all__ = [
    "ExchangeAdapter",
    "FetchOrderResult",
    "PlaceOrderResult",
    "build_adapter",
]


def build_adapter(settings: Settings, market: MarketDataService | None = None) -> ExchangeAdapter:
    """Return the venue adapter matching the configured trading mode.

    Mirrors the engine's decision: paper mode without exchange credentials uses
    the deterministic ``PaperAdapter``; anything that can reach a real venue
    uses the CCXT-backed ``LiveAdapter``.
    """
    if settings.is_paper and not settings.has_exchange_credentials:
        return PaperAdapter(settings)
    if market is None:
        raise ValueError("live execution requires a connected MarketDataService")
    return LiveAdapter(settings, market)
