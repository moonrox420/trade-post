"""Unit tests for the execution adapters (PRD M2)."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import ccxt.async_support as ccxt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trade_post.core.config import Settings
from trade_post.domain.models import (
    MarketSnapshot,
    OrderSide,
    OrderStatus,
    OrderType,
)
from trade_post.execution.adapters.live import LiveAdapter, map_order_status
from trade_post.execution.adapters.paper import PaperAdapter


def _snapshot(last="60000", bid="59990", ask="60010") -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTC/USDT",
        timestamp=datetime.now(timezone.utc),
        last_price=Decimal(last),
        bid=Decimal(bid),
        ask=Decimal(ask),
        spread_bps=Decimal("33.33"),
    )


def test_paper_adapter_is_deterministic():
    settings = Settings(slippage_bps=5.0)
    adapter = PaperAdapter(settings)

    async def run() -> tuple[Decimal, str]:
        result = await adapter.send_order(
            client_order_id="c1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            price=None,
            quantity=Decimal("0.5"),
            idempotency_key="ik-1",
            snapshot=_snapshot(),
        )
        return result.average_price or Decimal("0"), result.exchange_order_id

    first = asyncio.run(run())
    second = asyncio.run(run())
    assert first == second


def test_paper_adapter_applies_slippage():
    settings = Settings(slippage_bps=5.0)
    adapter = PaperAdapter(settings)

    buy = asyncio.run(
        adapter.send_order(
            client_order_id="c1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            price=None,
            quantity=Decimal("0.5"),
            idempotency_key="ik-1",
            snapshot=_snapshot(),
        )
    )
    # BUY fills at ask * (1 + 5bps) = 60010 * 1.0005 = 60040.005
    assert buy.average_price == Decimal("60040.005")
    assert buy.status is OrderStatus.FILLED

    sell = asyncio.run(
        adapter.send_order(
            client_order_id="c2",
            symbol="BTC/USDT",
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            price=None,
            quantity=Decimal("0.5"),
            idempotency_key="ik-2",
            snapshot=_snapshot(),
        )
    )
    # SELL fills at bid * (1 - 5bps) = 59990 * 0.9995 = 59960.0005
    assert sell.average_price == Decimal("59960.0005")


def test_map_order_status():
    assert map_order_status("closed") is OrderStatus.FILLED
    assert map_order_status("open") is OrderStatus.SUBMITTED
    assert map_order_status("canceled") is OrderStatus.CANCELLED
    assert map_order_status("rejected") is OrderStatus.REJECTED
    assert map_order_status(None) is OrderStatus.SUBMITTED


def test_live_adapter_retries_transient_then_succeeds():
    settings = Settings(
        trading_mode="live",
        exchange_api_key="k",
        exchange_api_secret="s",
        exchange_max_retries=3,
        exchange_retry_base_sec=0.01,
    )
    calls = {"n": 0}

    class FakeExchange:
        async def create_order(self, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ccxt.NetworkError("transient")
            return {"id": "ex-1", "status": "closed", "filled": 0.5, "average": 60000.0}

    class FakeMarket:
        exchange = FakeExchange()

    adapter = LiveAdapter(settings, FakeMarket())
    result = asyncio.run(
        adapter.send_order(
            client_order_id="c1",
            symbol="BTC/USDT",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            price=None,
            quantity=Decimal("0.5"),
            idempotency_key="ik-1",
        )
    )
    assert calls["n"] == 3
    assert result.exchange_order_id == "ex-1"
    assert result.status is OrderStatus.FILLED
    assert result.filled_quantity == Decimal("0.5")
