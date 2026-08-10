import time
import uuid
import logging
import asyncio
import pandas as pd
from datetime import datetime
from google.cloud.firestore import AsyncClient
from typing import Any, List, Optional
from models import MarketSnapshot, PortfolioSnapshot, PositionSnapshot, SignalSide
from config import MODE, STALE_THRESHOLD_SEC, RECOVERY_COOLOFF_SEC
from runtime import manager

logger = logging.getLogger(__name__)


class MarketDataService:
    def __init__(self, ccxt_adapter):
        self.ccxt = ccxt_adapter
        self.cache: dict[str, MarketSnapshot] = {}

    def _process_indicators(self, df: pd.DataFrame) -> dict[str, Any]:
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.00001)
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        return {"rsi": rsi, "volatility": df["close"].pct_change().std()}

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        ticker = await self.ccxt.fetch_ticker(symbol)
        ohlcv = await self.ccxt.fetch_ohlcv(symbol)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "vol"])
        indicators = self._process_indicators(df)
        snapshot = MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.utcnow(),
            last_price=ticker["last"],
            bid=ticker["bid"],
            ask=ticker["ask"],
            volume=ticker["baseVolume"],
            indicators=indicators,
        )
        self.cache[symbol] = snapshot
        return snapshot

    async def stream_loop(self, symbols: List[str], interval: int = 15):
        logger.info(f"Market Streamer active for {symbols}")
        while True:
            try:
                for symbol in symbols:
                    snap = await self.get_snapshot(symbol)
                    await manager.broadcast(
                        {"type": "market_snapshot", "data": snap.model_dump()}
                    )
            except Exception as e:
                logger.error(f"Market streaming error: {e}")
            await asyncio.sleep(interval)

    def is_stale(self, symbol: str) -> bool:
        if symbol not in self.cache:
            return True
        age = (datetime.utcnow() - self.cache[symbol].timestamp).total_seconds()
        return age > STALE_THRESHOLD_SEC


class PortfolioEngine:
    def __init__(self, ccxt_adapter, db: AsyncClient):
        self.ccxt = ccxt_adapter
        self.db = db
        self.state: Optional[PortfolioSnapshot] = None

    async def persist_snapshot(self, snapshot: PortfolioSnapshot):
        try:
            await self.db.collection("portfolio_history").add(snapshot.model_dump())
        except Exception as e:
            logger.error(f"Failed to persist portfolio snapshot: {e}")

    async def refresh_state(self) -> PortfolioSnapshot:
        balances = await self.ccxt.fetch_balance()
        raw_positions = await self.ccxt.fetch_positions()
        total_equity = balances.get("total", {}).get("USDT", 0.0)
        available_margin = balances.get("free", {}).get("USDT", 0.0)
        positions = []
        total_notional = 0.0
        for p in raw_positions:
            contracts = float(p.get("contracts", 0) or 0)
            if contracts == 0:
                continue
            notional = abs(float(p.get("notional", 0) or 0))
            total_notional += notional
            positions.append(
                PositionSnapshot(
                    symbol=p["symbol"],
                    side=SignalSide.LONG if p["side"] == "long" else SignalSide.SHORT,
                    entry_price=float(p["entryPrice"]),
                    notional=float(p["notional"]),
                    unrealized_pnl=float(p["unrealizedPnl"]),
                    leverage=float(p["leverage"]),
                    contracts=contracts,
                    timestamp=datetime.utcnow(),
                )
            )
        margin_utilization = (
            ((total_equity - available_margin) / total_equity * 100)
            if total_equity > 0
            else 0
        )
        risk_adjusted_equity = total_equity - (total_notional * 0.01)
        self.state = PortfolioSnapshot(
            timestamp=datetime.utcnow(),
            total_equity=total_equity,
            available_margin=available_margin,
            positions=positions,
            base_balances={k: v for k, v in balances.get("total", {}).items() if v > 0},
            risk_adjusted_equity=risk_adjusted_equity,
            margin_utilization=margin_utilization,
        )
        return self.state

    async def snapshot_loop(self, interval: int = 60):
        logger.info("Portfolio Snapshot Loop started.")
        while True:
            try:
                snap = await self.refresh_state()
                await self.persist_snapshot(snap)
                await manager.broadcast(
                    {"type": "portfolio_snapshot", "data": snap.model_dump()}
                )
            except Exception as e:
                logger.error(f"Portfolio snapshot loop error: {e}")
            await asyncio.sleep(interval)


class EventStore:
    def __init__(self, db: AsyncClient):
        self.db = db
        self.session_id = f"session_{int(time.time())}"
        self._failure_timestamps: List[float] = []
        self._failure_threshold = 3
        self._failure_window = 60
        self._circuit_open = False
        self._last_failure_time: Optional[float] = None

    async def emit(self, event_type: str, data: dict, severity: str = "INFO"):
        if event_type == "execution_failed":
            now = time.time()
            self._failure_timestamps.append(now)
            self._last_failure_time = now
        event_id = str(uuid.uuid4())
        entry = {
            "event_id": event_id,
            "session_id": self.session_id,
            "event_type": event_type,
            "data": data,
            "severity": severity,
            "timestamp": datetime.utcnow().isoformat(),
            "version": "1.0",
            "mode": MODE,
        }
        try:
            await self.db.collection("event_store").document(event_id).set(entry)
            logger.info(f"EVENT [{event_type}] {event_id}")
        except Exception as e:
            logger.error(f"Event emission failed: {e}")

    def is_circuit_broken(self) -> bool:
        now = time.time()
        if self._circuit_open:
            if self._last_failure_time and (
                now - self._last_failure_time >= RECOVERY_COOLOFF_SEC
            ):
                logger.info(
                    f"Circuit Breaker: Cooling-off period met ({RECOVERY_COOLOFF_SEC}s). Recovering system."
                )
                self._circuit_open = False
                self._failure_timestamps = []
                return False
            return True
        self._failure_timestamps = [
            t for t in self._failure_timestamps if now - t < self._failure_window
        ]
        if len(self._failure_timestamps) >= self._failure_threshold:
            logger.critical(
                f"Circuit Breaker TRIP: {len(self._failure_timestamps)} failures detected. System entered recovery mode."
            )
            self._circuit_open = True
            return True
        return False
