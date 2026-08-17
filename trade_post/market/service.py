"""Market data service. Owns the cache, fetches from CCXT, computes indicators."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal

import ccxt.async_support as ccxt

from ..core.config import Settings
from ..core.errors import ExchangeError
from ..domain.models import MarketSnapshot
from . import indicators as ind

log = logging.getLogger(__name__)


class MarketDataService:
    """Thread-safe async wrapper around CCXT. Owns the snapshot cache."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._exchange: ccxt.Exchange | None = None
        self._cache: dict[str, tuple[float, MarketSnapshot]] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._exchange is not None:
            return
        cls = getattr(ccxt, self._settings.exchange_id.value)
        cfg: dict = {
            "enableRateLimit": True,
            "timeout": 30_000,
        }
        if self._settings.has_exchange_credentials:
            cfg["apiKey"] = self._settings.exchange_api_key
            cfg["secret"] = self._settings.exchange_api_secret
        self._exchange = cls(cfg)
        if self._exchange is None:
            raise ExchangeError("Failed to instantiate exchange client")
        if self._settings.exchange_sandbox and hasattr(self._exchange, "setSandboxMode"):
            try:
                self._exchange.setSandboxMode(True)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.warning("sandbox mode unsupported on %s: %s", self._settings.exchange_id.value, exc)
        try:
            await self._exchange.load_markets()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError("Failed to load markets", original=exc) from exc
        log.info("market data service connected exchange=%s sandbox=%s",
                self._settings.exchange_id.value, self._settings.exchange_sandbox)

    async def disconnect(self) -> None:
        if self._exchange is not None:
            try:
                await self._exchange.close()
            finally:
                self._exchange = None
                log.info("market data service disconnected")

    async def get_snapshot(self, symbol: str, *, use_cache: bool = True,
                        max_age_sec: float | None = None) -> MarketSnapshot:
        max_age = max_age_sec if max_age_sec is not None else float(self._settings.max_stale_data_sec)
        if use_cache:
            cached = self._cache.get(symbol)
            if cached and (time.monotonic() - cached[0]) < max_age:
                return cached[1]
        if self._exchange is None:
            raise ExchangeError("Market service not connected")
        try:
            ticker = await self._exchange.fetch_ticker(symbol)
            ohlcv = await self._exchange.fetch_ohlcv(symbol, timeframe="1m", limit=self._settings.market_data_ohlcv_limit)
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError(f"Failed to fetch {symbol}", symbol=symbol, original=exc) from exc
        closes = [c[4] for c in ohlcv]
        highs = [c[2] for c in ohlcv]
        lows = [c[3] for c in ohlcv]
        vols = [c[5] for c in ohlcv]
        feats = ind.compute_all(closes, highs, lows, vols,
                                rsi_period=14, atr_period=self._settings.atr_period)
        spread_bps: Decimal | None = None
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        last = ticker.get("last")
        if bid and ask and last and float(last) > 0:
            spread_bps = Decimal(str(((float(ask) - float(bid)) / float(last)) * 10_000))
        snap = MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.now(timezone.utc),
            last_price=Decimal(str(last)) if last is not None else Decimal("0"),
            bid=Decimal(str(bid)) if bid else None,
            ask=Decimal(str(ask)) if ask else None,
            spread_bps=spread_bps,
            volume_24h=Decimal(str(ticker.get("quoteVolume") or 0)),
            indicators={k: v for k, v in feats.items() if v is not None},
            source="ccxt",
        )
        async with self._lock:
            self._cache[symbol] = (time.monotonic(), snap)
        return snap

    async def get_ohlcv(self, symbol: str, timeframe: str = "1h", limit: int = 200) -> list[list]:
        if self._exchange is None:
            raise ExchangeError("Market service not connected")
        try:
            return await self._exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception as exc:  # noqa: BLE001
            raise ExchangeError(f"Failed to fetch OHLCV {symbol}", symbol=symbol, original=exc) from exc

    def is_stale(self, symbol: str, max_age_sec: float | None = None) -> bool:
        max_age = max_age_sec if max_age_sec is not None else float(self._settings.max_stale_data_sec)
        cached = self._cache.get(symbol)
        if not cached:
            return True
        return (time.monotonic() - cached[0]) > max_age

    @property
    def exchange(self) -> ccxt.Exchange | None:
        return self._exchange
