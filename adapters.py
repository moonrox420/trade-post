import uuid
import asyncio
from datetime import datetime
from typing import List
import ccxt.async_support as ccxt
from models import SignalSide, StrategyProposal, OrderSnapshot, OrderSide
from config import EXCHANGE_ID, API_KEY, API_SECRET, ENABLE_SANDBOX, MODE


class CCXTAdapter:
    def __init__(self, events):
        self.events = events
        self.exchange = getattr(ccxt, EXCHANGE_ID)(
            {
                "apiKey": API_KEY,
                "secret": API_SECRET,
                "enableRateLimit": True,
                "options": {
                    "defaultType": "future"
                    if EXCHANGE_ID in ["binance", "bybit"]
                    else "spot"
                },
            }
        )
        if ENABLE_SANDBOX and hasattr(self.exchange, "set_sandbox_mode"):
            self.exchange.set_sandbox_mode(True)
        self.side_map = {
            SignalSide.LONG: "buy",
            SignalSide.SHORT: "sell",
            SignalSide.FLAT: "sell",
        }

    async def _request(self, method_name: str, *args, **kwargs):
        method = getattr(self.exchange, method_name)
        for i in range(3):
            try:
                return await method(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                if i == 2:
                    raise e
                await asyncio.sleep(2 ** (i + 1))

    async def initialize(self):
        await self._request("load_markets")

    async def fetch_balance(self) -> dict:
        return await self._request("fetch_balance")

    async def fetch_ticker(self, symbol: str) -> dict:
        return await self._request("fetch_ticker", symbol)

    async def fetch_positions(self) -> List[dict]:
        return await self._request("fetch_positions")

    async def fetch_open_orders(self, symbol: str = None) -> List[dict]:
        return await self._request("fetch_open_orders", symbol)

    async def set_leverage(self, symbol: str, leverage: int):
        return await self._request("set_leverage", leverage, symbol)

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 100
    ) -> List[List]:
        return await self._request(
            "fetch_ohlcv", symbol, timeframe=timeframe, limit=limit
        )

    async def place_order(self, proposal: StrategyProposal) -> OrderSnapshot:
        if MODE == "paper":
            return OrderSnapshot(
                order_id=f"paper_{uuid.uuid4().hex[:8]}",
                symbol=proposal.symbol,
                side=OrderSide.BUY
                if proposal.side == SignalSide.LONG
                else OrderSide.SELL,
                amount=proposal.amount,
                price=proposal.price,
                status="closed",
                filled=proposal.amount,
                timestamp=datetime.utcnow(),
                proposal_price=proposal.price,
                slippage_adjusted_notional=proposal.amount * (proposal.price or 0),
            )

        side = self.side_map.get(proposal.side)
        raw = await self._request(
            "create_order",
            symbol=proposal.symbol,
            type=proposal.order_type,
            side=side,
            amount=proposal.amount,
            price=proposal.price,
        )
        actual_price = float(raw.get("price") or raw.get("average", 0))
        return OrderSnapshot(
            order_id=str(raw["id"]),
            symbol=raw["symbol"],
            side=OrderSide(raw["side"]),
            amount=float(raw["amount"]),
            price=actual_price,
            status=raw["status"],
            filled=float(raw.get("filled", 0)),
            timestamp=datetime.utcnow(),
            proposal_price=proposal.price,
            slippage_adjusted_notional=float(raw["amount"]) * actual_price,
        )
