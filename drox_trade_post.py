"""Drox Trade Post — High-Performance Local-First Algorithmic Trading System.

Consolidates all modular services, execution engines, and risk gates into a 
unified, type-safe, and non-blocking runtime. Underpinned entirely by local-first
architectures using SQLite and local or cloud Ollama API endpoints to eliminate cloud quota,
credentials, and permission bottlenecks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import sqlite3
import time
import urllib.error
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Sequence

# Manually parse local .env file natively to avoid unresolved package imports
env_file = Path(".env")
if env_file.exists():
    for env_line in env_file.read_text(encoding="utf-8").splitlines():
        env_line = env_line.strip()
        if not env_line or env_line.startswith("#"):
            continue
        if "=" in env_line:
            env_key, env_val = env_line.split("=", 1)
            os.environ[env_key.strip()] = env_val.strip()

import ccxt.async_support as ccxt
import pandas as pd
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger: logging.Logger = logging.getLogger("drox_trade_post")

# -----------------------------------------------------------------------------
# System Configurations
# -----------------------------------------------------------------------------
EXCHANGE_ID: str = os.getenv("EXCHANGE_ID", "kraken")
API_KEY: str = os.getenv("EXCHANGE_API_KEY", "")
API_SECRET: str = os.getenv("EXCHANGE_API_SECRET", "")
MODE: str = os.getenv("TRADING_MODE", "paper")  # "paper" | "live"
ENABLE_SANDBOX: bool = os.getenv("ENABLE_SANDBOX", "true").lower() == "true"

OLLAMA_URL: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_API_KEY: str = os.getenv("OLLAMA_API_KEY", "")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gpt-oss:120b-cloud")

MAX_POSITION_PCT: float = float(os.getenv("MAX_POSITION_PCT", 2.0))
MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", 1.0))
STALE_THRESHOLD_SEC: int = int(os.getenv("STALE_THRESHOLD_SEC", 30))
POSITION_SIZE_HARD_CAP: float = float(os.getenv("POSITION_SIZE_HARD_CAP", 1000.0))
SIMULATED_SLIPPAGE_BPS: float = float(os.getenv("SIMULATED_SLIPPAGE_BPS", 5.0))
ENABLE_CHAOS_TEST: bool = os.getenv("ENABLE_CHAOS_TEST", "false").lower() == "true"
ORPHAN_AGE_THRESHOLD_SEC: int = int(os.getenv("ORPHAN_AGE_THRESHOLD_SEC", 300))
RECOVERY_COOLOFF_SEC: int = int(os.getenv("RECOVERY_COOLOFF_SEC", 300))

DB_PATH: str = "trade_post.db"

# -----------------------------------------------------------------------------
# Local Database Initializer
# -----------------------------------------------------------------------------


def init_db() -> None:
    """Initialize SQLite tables for local SSoT state persistence."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS event_store (
            event_id TEXT PRIMARY KEY,
            session_id TEXT,
            event_type TEXT,
            data TEXT,
            severity TEXT,
            timestamp TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS execution_ledger (
            idempotency_key TEXT PRIMARY KEY,
            proposal_id TEXT,
            order_id TEXT,
            status TEXT,
            timestamp TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            total_equity REAL,
            available_margin REAL,
            positions TEXT,
            base_balances TEXT,
            risk_adjusted_equity REAL,
            margin_utilization REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS strategy_evaluations (
            proposal_id TEXT PRIMARY KEY,
            symbol TEXT,
            performance_bps REAL,
            qualitative_score INTEGER,
            critique TEXT,
            rationale TEXT,
            timestamp TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS performance_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report TEXT,
            timestamp TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS risk_engine_state (
            key TEXT PRIMARY KEY,
            starting_equity REAL,
            killed INTEGER,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Local SQLite database verified and initialized.")


# -----------------------------------------------------------------------------
# Custom Exceptions
# -----------------------------------------------------------------------------


class TradingPostError(Exception):
    """Base exception for the Drox Trade Post system."""


class ExchangeExecutionError(TradingPostError):
    """Exception raised when an exchange operation fails via CCXT."""

    def __init__(self, message: str, symbol: str | None = None, original_error: Exception | None = None) -> None:
        self.symbol: str | None = symbol
        self.original_error: Exception | None = original_error
        super().__init__(message)


# -----------------------------------------------------------------------------
# Canonical Pydantic Models (Normalization Layer)
# -----------------------------------------------------------------------------


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class SignalSide(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class MarketSnapshot(BaseModel):
    symbol: str
    timestamp: datetime
    last_price: float
    bid: float
    ask: float
    volume: float
    indicators: dict[str, Any] = {}


class OrderSnapshot(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    amount: float
    price: float | None
    status: str
    filled: float
    timestamp: datetime
    proposal_price: float | None = None
    slippage_adjusted_notional: float = 0.0

    @model_validator(mode="after")
    def validate_slippage(self) -> "OrderSnapshot":
        if self.price is not None and self.proposal_price is not None and self.proposal_price > 0:
            deviation = abs(self.price - self.proposal_price) / self.proposal_price
            if deviation > 0.05:
                raise ValueError(
                    f"Slippage violation: execution price {self.price} deviates more than 5% "
                    f"from proposal price {self.proposal_price}"
                )
        return self


class PositionSnapshot(BaseModel):
    symbol: str
    side: SignalSide
    entry_price: float
    notional: float
    unrealized_pnl: float
    leverage: float
    contracts: float
    timestamp: datetime


class PortfolioSnapshot(BaseModel):
    timestamp: datetime
    total_equity: float
    available_margin: float
    positions: list[PositionSnapshot]
    base_balances: dict[str, float]
    risk_adjusted_equity: float
    margin_utilization: float

    @field_validator("total_equity")
    @classmethod
    def validate_total_equity(cls, value: float) -> float:
        if value < 0:
            logger.error("CRITICAL: Negative total equity detected: %f", value)
            raise ValueError("total_equity must be non-negative")
        return value


class PerformanceReport(BaseModel):
    report_period: str
    total_evaluations: int
    average_score: float
    net_performance_bps: float
    executive_summary: str
    key_learnings: list[str]


class QualitativeEvaluation(BaseModel):
    score: int = Field(ge=1, le=10)
    critique: str


class SymbolPrioritization(BaseModel):
    prioritized_symbols: list[str]


class StrategyProposal(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    proposal_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    symbol: str = Field(..., pattern=r"^[A-Z0-9]+/[A-Z0-9_]+$")
    side: SignalSide = Field(..., alias="signal")
    amount: float = Field(gt=0)
    price: float | None = None
    order_type: str = Field(default="limit", pattern="^(market|limit)$")
    conviction: int = Field(ge=1, le=10)
    rationale: str
    trailing_stop_pct: float | None = Field(default=None, gt=0.1, le=5.0)
    market_snapshot_id: str | None = None

    @field_validator("amount")
    @classmethod
    def validate_amount_safety(cls, value: float) -> float:
        if value > POSITION_SIZE_HARD_CAP:
            raise ValueError(f"Amount {value} exceeds hard safety cap of {POSITION_SIZE_HARD_CAP}")
        return value

    def get_idempotency_key(self) -> str:
        payload = f"{self.symbol}:{self.side}:{self.amount}:{self.order_type}"
        return hashlib.sha256(payload.encode()).hexdigest()


# -----------------------------------------------------------------------------
# Managed Runtime registries
# -----------------------------------------------------------------------------


class TaskRegistry:
    """Manages thread-safe cancellation and shutdown properties of tasks."""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task[Any]] = {}

    def register(self, task_id: str, coro) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.tasks[task_id] = task
        task.add_done_callback(lambda t: self.tasks.pop(task_id, None))
        return task

    async def shutdown(self) -> None:
        logger.info("Shutting down %d managed tasks...", len(self.tasks))
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)


class ConnectionManager:
    """Manages WebSocket subscribers, subscriptions, and JSON serialization."""

    def __init__(self) -> None:
        self.active: dict[str, WebSocket] = {}
        self.subscriptions: dict[str, set[str]] = {}

    async def connect(self, websocket: WebSocket, sid: str) -> None:
        await websocket.accept()
        self.active[sid] = websocket
        self.subscriptions[sid] = set()

    def disconnect(self, sid: str) -> None:
        self.active.pop(sid, None)
        self.subscriptions.pop(sid, None)

    async def subscribe(self, sid: str, symbols: list[str]) -> None:
        if sid in self.subscriptions:
            self.subscriptions[sid].update(symbols)

    async def unsubscribe(self, sid: str, symbols: list[str]) -> None:
        if sid in self.subscriptions:
            for s in symbols:
                self.subscriptions[sid].discard(s)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Serialize data payloads containing datetimes safely to all subscribers."""
        def json_serial(object_to_serialize: Any) -> str:
            if isinstance(object_to_serialize, (datetime, pd.Timestamp)):
                return object_to_serialize.isoformat()
            raise TypeError(f"Type {type(object_to_serialize)} not serializable")

        payload = json.dumps(message, default=json_serial)
        msg_type = message.get("type")
        msg_symbol = message.get("data", {}).get("symbol")

        for sid, ws in list(self.active.items()):
            try:
                if msg_type == "market_snapshot" and msg_symbol:
                    if msg_symbol not in self.subscriptions.get(sid, set()):
                        continue
                await ws.send_text(payload)
            except Exception:
                pass


manager: ConnectionManager = ConnectionManager()


# -----------------------------------------------------------------------------
# CCXT Exchange Adapter
# -----------------------------------------------------------------------------


class CCXTAdapter:
    """Handles rate-limited and sandboxed exchange executions."""

    def __init__(self, events: EventStore) -> None:
        self.events: EventStore = events
        self.exchange = getattr(ccxt, EXCHANGE_ID)({
            'apiKey': API_KEY,
            'secret': API_SECRET,
            'enableRateLimit': True,
            'options': {'defaultType': 'future' if EXCHANGE_ID in ['binance', 'bybit'] else 'spot'}
        })
        if ENABLE_SANDBOX and hasattr(self.exchange, 'set_sandbox_mode'):
            self.exchange.set_sandbox_mode(True)
        self.side_map = {SignalSide.LONG: "buy", SignalSide.SHORT: "sell", SignalSide.FLAT: "sell"}

    async def _request(self, method_name: str, *args, **kwargs) -> Any:
        method = getattr(self.exchange, method_name)
        for i in range(3):
            try:
                return await method(*args, **kwargs)
            except (ccxt.NetworkError, ccxt.RequestTimeout) as e:
                if i == 2:
                    raise e
                await asyncio.sleep(2 ** (i + 1))

    async def initialize(self) -> None:
        await self._request('load_markets')

    async def fetch_balance(self) -> dict[str, Any]:
        return await self._request('fetch_balance')

    async def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return await self._request('fetch_ticker', symbol)

    async def fetch_positions(self) -> list[dict[str, Any]]:
        return await self._request('fetch_positions')

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._request('fetch_open_orders', symbol)

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        return await self._request('cancel_order', order_id, symbol)

    async def set_leverage(self, symbol: str, leverage: int) -> Any:
        return await self._request('set_leverage', leverage, symbol)

    async def fetch_ohlcv(self, symbol: str, timeframe: str = '1h', limit: int = 100) -> list[list[float]]:
        return await self._request('fetch_ohlcv', symbol, timeframe=timeframe, limit=limit)

    async def place_order(self, proposal: StrategyProposal) -> OrderSnapshot:
        """Place an order on the exchange or simulate a paper transaction."""
        if MODE == "paper":
            return OrderSnapshot(
                order_id=f"paper_{uuid.uuid4().hex[:8]}",
                symbol=proposal.symbol,
                side=OrderSide.BUY if proposal.side == SignalSide.LONG else OrderSide.SELL,
                amount=proposal.amount,
                price=proposal.price,
                status="closed",
                filled=proposal.amount,
                timestamp=datetime.utcnow(),
                proposal_price=proposal.price,
                slippage_adjusted_notional=proposal.amount * (proposal.price or 0.0)
            )
        
        side = self.side_map.get(proposal.side)
        raw_order = await self._request(
            'create_order',
            symbol=proposal.symbol,
            type=proposal.order_type,
            side=side,
            amount=proposal.amount,
            price=proposal.price
        )
        actual_price = float(raw_order.get('price') or raw_order.get('average', 0))
        return OrderSnapshot(
            order_id=str(raw_order['id']),
            symbol=raw_order['symbol'],
            side=OrderSide(raw_order['side']),
            amount=float(raw_order['amount']),
            price=actual_price,
            status=raw_order['status'],
            filled=float(raw_order.get('filled', 0)),
            timestamp=datetime.utcnow(),
            proposal_price=proposal.price,
            slippage_adjusted_notional=float(raw_order['amount']) * actual_price
        )

    async def close(self) -> None:
        await self.exchange.close()


# -----------------------------------------------------------------------------
# Market Data Service
# -----------------------------------------------------------------------------


class MarketDataService:
    """Computes technical indicator snapshots and handles websocket streaming."""

    def __init__(self, ccxt_adapter: CCXTAdapter) -> None:
        self.ccxt: CCXTAdapter = ccxt_adapter
        self.cache: dict[str, MarketSnapshot] = {}

    def _process_indicators(self, dataframe: pd.DataFrame) -> dict[str, Any]:
        """Compute RSI and Volatility parameters cleanly."""
        delta = dataframe['close'].diff()
        gain = (delta.where(delta > 0, 0.0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0.0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.00001)
        rsi = 100 - (100 / (1 + rs.iloc[-1]))
        return {"rsi": rsi, "volatility": dataframe['close'].pct_change().std()}

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        ticker = await self.ccxt.fetch_ticker(symbol)
        ohlcv = await self.ccxt.fetch_ohlcv(symbol)
        
        dataframe = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        indicators = self._process_indicators(dataframe)
        
        snapshot = MarketSnapshot(
            symbol=symbol,
            timestamp=datetime.utcnow(),
            last_price=ticker['last'],
            bid=ticker['bid'],
            ask=ticker['ask'],
            volume=ticker['baseVolume'],
            indicators=indicators
        )
        self.cache[symbol] = snapshot
        return snapshot

    async def backtest(self, symbol: str, ohlcv_sequence: list[list[float]]) -> list[MarketSnapshot]:
        """Reconstruct historical market states from OHLCV arrays."""
        snapshots = []
        dataframe = pd.DataFrame(ohlcv_sequence, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        for i in range(14, len(dataframe)):
            sub_df = dataframe.iloc[:i+1]
            last_row = sub_df.iloc[-1]
            snapshots.append(MarketSnapshot(
                symbol=symbol,
                timestamp=datetime.fromtimestamp(last_row['ts'] / 1000.0),
                last_price=last_row['close'],
                bid=last_row['close'] * 0.9999,
                ask=last_row['close'] * 1.0001,
                volume=last_row['vol'],
                indicators=self._process_indicators(sub_df)
            ))
        return snapshots

    async def stream_loop(self, symbols: list[str], interval: int = 15) -> None:
        """Broadcast live feeds to active client connections."""
        logger.info("Market Streamer active for %s", symbols)
        while True:
            try:
                for symbol in symbols:
                    snap = await self.get_snapshot(symbol)
                    await manager.broadcast({"type": "market_snapshot", "data": snap.model_dump()})
            except Exception as exception:
                logger.error("Market streaming error: %s", exception)
            await asyncio.sleep(interval)

    def is_stale(self, symbol: str) -> bool:
        if symbol not in self.cache: 
            return True
        age = (datetime.utcnow() - self.cache[symbol].timestamp).total_seconds()
        return age > STALE_THRESHOLD_SEC


# -----------------------------------------------------------------------------
# Portfolio Engine
# -----------------------------------------------------------------------------


class PortfolioEngine:
    """Manages active balances, positions, and margin metrics."""

    def __init__(self, ccxt_adapter: CCXTAdapter) -> None:
        self.ccxt: CCXTAdapter = ccxt_adapter
        self.state: PortfolioSnapshot | None = None
        self._paper_equity: float = 10000.0  # Simulated initial balance
        self._paper_positions: list[PositionSnapshot] = []

    async def persist_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Store portfolio history cleanly into local SQLite."""
        def save():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO portfolio_history (timestamp, total_equity, available_margin, positions, base_balances, risk_adjusted_equity, margin_utilization) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot.timestamp.isoformat(),
                    snapshot.total_equity,
                    snapshot.available_margin,
                    json.dumps([p.model_dump() for p in snapshot.positions], default=str),
                    json.dumps(snapshot.base_balances),
                    snapshot.risk_adjusted_equity,
                    snapshot.margin_utilization
                )
            )
            conn.commit()
            conn.close()
        await asyncio.to_thread(save)

    async def refresh_state(self) -> PortfolioSnapshot:
        # Keyless Paper Trading Fallback: simulate balances and positions in RAM
        if MODE == "paper" and (not API_KEY or not API_SECRET):
            total_equity = self._paper_equity
            available_margin = total_equity
            
            # Retrieve active paper positions we tracked
            positions = self._paper_positions
            total_notional = sum(p.notional for p in positions)
            
            margin_utilization = ((total_equity - available_margin) / total_equity * 100.0) if total_equity > 0 else 0.0
            risk_adjusted_equity = total_equity - (total_notional * 0.01)
            
            self.state = PortfolioSnapshot(
                timestamp=datetime.utcnow(),
                total_equity=total_equity,
                available_margin=available_margin,
                positions=positions,
                base_balances={"USDT": total_equity},
                risk_adjusted_equity=risk_adjusted_equity,
                margin_utilization=margin_utilization
            )
            return self.state

        # Otherwise, query real exchange
        balances = await self.ccxt.fetch_balance()
        raw_positions = await self.ccxt.fetch_positions()
        
        total_equity = balances.get('total', {}).get('USDT', 0.0)
        available_margin = balances.get('free', {}).get('USDT', 0.0)
        
        positions = []
        total_notional = 0.0
        for p in raw_positions:
            contracts = float(p.get('contracts', 0) or 0)
            if contracts == 0:
                continue

            notional = abs(float(p.get('notional', 0) or 0))
            total_notional += notional

            positions.append(PositionSnapshot(
                symbol=p['symbol'],
                side=SignalSide.LONG if p['side'] == 'long' else SignalSide.SHORT,
                entry_price=float(p['entryPrice']),
                notional=float(p['notional']),
                unrealized_pnl=float(p['unrealizedPnl']),
                leverage=float(p['leverage']),
                contracts=contracts,
                timestamp=datetime.utcnow()
            ))

        margin_utilization = ((total_equity - available_margin) / total_equity * 100) if total_equity > 0 else 0
        risk_adjusted_equity = total_equity - (total_notional * 0.01)

        self.state = PortfolioSnapshot(
            timestamp=datetime.utcnow(),
            total_equity=total_equity,
            available_margin=available_margin,
            positions=positions,
            base_balances={k: v for k, v in balances.get('total', {}).items() if v > 0},
            risk_adjusted_equity=risk_adjusted_equity,
            margin_utilization=margin_utilization
        )
        return self.state

    async def snapshot_loop(self, interval: int = 60) -> None:
        logger.info("Portfolio Snapshot Loop started.")
        while True:
            try:
                snap = await self.refresh_state()
                await self.persist_snapshot(snap)
                await manager.broadcast({"type": "portfolio_snapshot", "data": snap.model_dump()})
            except Exception as exception:
                logger.error("Portfolio snapshot loop error: %s", exception)
            await asyncio.sleep(interval)


# -----------------------------------------------------------------------------
# Event Store & Circuit Breaker Protection
# -----------------------------------------------------------------------------


class EventStore:
    """Monitors background process health and enforces circuit breakers."""

    def __init__(self) -> None:
        self.session_id: str = f"session_{int(time.time())}"
        self._failure_timestamps: list[float] = []
        self._failure_threshold: int = 3
        self._failure_window: int = 60
        self._circuit_open: bool = False
        self._last_failure_time: float | None = None

    async def emit(self, event_type: str, data: dict[str, Any], severity: str = "INFO") -> None:
        """Log structured events to SQLite with automated severity checks."""
        if event_type == "execution_failed":
            now = time.time()
            self._failure_timestamps.append(now)
            self._last_failure_time = now
        event_id = str(uuid.uuid4())
        
        def save():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO event_store VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, self.session_id, event_type, json.dumps(data), severity, datetime.utcnow().isoformat())
            )
            conn.commit()
            conn.close()
            
        try:
            await asyncio.to_thread(save)
            logger.info("EVENT [%s] %s", event_type, event_id)
        except Exception as exception:
            logger.error("Event emission failed: %s", exception)

    def is_circuit_broken(self) -> bool:
        """Monitor recent background thread crashes and trip recovery mode if required."""
        now = time.time()
        
        if self._circuit_open:
            if self._last_failure_time and (now - self._last_failure_time >= RECOVERY_COOLOFF_SEC):
                logger.info("Circuit Breaker: Cooling-off boundary met. Restoring system state.")
                self._circuit_open = False
                self._failure_timestamps = []
                return False
            return True

        self._failure_timestamps = [t for t in self._failure_timestamps if now - t < self._failure_window]
        if len(self._failure_timestamps) >= self._failure_threshold:
            logger.critical("Circuit Breaker TRIP: %d failures detected. Entering recovery.", len(self._failure_timestamps))
            self._circuit_open = True
            return True
        return False


# -----------------------------------------------------------------------------
# Risk Engine
# -----------------------------------------------------------------------------


class RiskEngine:
    """Enforces absolute margin limits and daily loss drawdown caps."""

    def __init__(self) -> None:
        self.killed: bool = False
        self.starting_equity: float | None = None
        self.portfolio: PortfolioEngine | None = None

    async def initialize(self, portfolio: PortfolioEngine) -> None:
        self.portfolio = portfolio
        
        def load():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT starting_equity, killed FROM risk_engine_state WHERE key = 'state'")
            result_row = cursor.fetchone()
            conn.close()
            return result_row
            
        try:
            row = await asyncio.to_thread(load)
            if row:
                self.starting_equity = row[0]
                self.killed = bool(row[1])
        except Exception as exception:
            logger.error("Failed to restore RiskEngine state: %s", exception)

    async def _persist_state(self) -> None:
        def save():
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO risk_engine_state VALUES ('state', ?, ?, ?)",
                (self.starting_equity, 1 if self.killed else 0, datetime.utcnow().isoformat())
            )
            conn.commit()
            conn.close()
        try:
            await asyncio.to_thread(save)
        except Exception as exception:
            logger.error("Failed to persist RiskEngine state: %s", exception)

    def calculate_dynamic_leverage(self, margin_utilization: float) -> int:
        """Calculate maximum allowed leverage dynamically based on account margin utilization."""
        if margin_utilization > 80: return 1
        if margin_utilization > 60: return 2
        if margin_utilization > 40: return 3
        if margin_utilization > 20: return 5
        return 10

    async def validate_proposal(
        self, 
        proposal: StrategyProposal, 
        market: MarketSnapshot, 
        portfolio_state: PortfolioSnapshot | None = None
    ) -> tuple[bool, str]:
        """Asynchronously validate a strategy proposal against risk constraints."""
        if self.killed:
            return False, "Kill switch active"
        
        if not self.portfolio:
            return False, "Risk Engine not initialized with portfolio"

        portfolio_state = portfolio_state or await self.portfolio.refresh_state()
        total_equity = portfolio_state.total_equity

        if total_equity <= 0:
            logger.error("Risk Engine: Insufficient USDT balance data")
            return False, "Insufficient balance data"

        if self.starting_equity is None:
            self.starting_equity = total_equity
            await self._persist_state()
            logger.info("Session starting equity initialized at %f USDT", self.starting_equity)

        # Drawdown checks
        if self.starting_equity <= 0:
            await self.kill_switch("Starting equity configuration error.")
            return False, "Equity config error"
            
        current_drawdown_pct = ((self.starting_equity - total_equity) / self.starting_equity) * 100
        if current_drawdown_pct >= MAX_DAILY_LOSS_PCT:
            await self.kill_switch(f"Daily drawdown limit reached: {current_drawdown_pct:.2f}%")
            return False, f"Drawdown limit: {current_drawdown_pct:.2f}%"

        # Margin/Notional Sizing Checks
        notional_value = proposal.amount * market.last_price
        if notional_value > (total_equity * (MAX_POSITION_PCT / 100.0)):
            return False, f"Size too large: {notional_value:.2f} USDT"

        return True, "Risk validation passed"

    async def kill_switch(self, reason: str) -> None:
        self.killed = True
        logger.critical("KILL SWITCH TRIGGERED: %s", reason)
        await self._persist_state()


# -----------------------------------------------------------------------------
# Local-First AI Integration Handler (Ollama JSON Engine)
# -----------------------------------------------------------------------------


async def query_ollama_json(prompt: str, system_prompt: str, model_name: str) -> dict[str, Any]:
    """Query a local or cloud Ollama API natively using standard libraries with strict JSON validation."""
    normalized_url = OLLAMA_URL.strip()
    
    # Dynamic Ollama Cloud router integration
    if OLLAMA_API_KEY and ("localhost" in normalized_url or "127.0.0.1" in normalized_url):
        normalized_url = "https://ollama.com"
        
    if "ollama.com" in normalized_url:
        api_endpoint = "https://ollama.com/api/chat"
    else:
        api_endpoint = f"{normalized_url.rstrip('/')}/api/chat"
    
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "format": "json",  # Force Ollama to produce syntactically valid JSON
        "options": {
            "temperature": 0.1
        },
        "stream": False
    }
    
    def call():
        request_headers = {"Content-Type": "application/json"}
        if OLLAMA_API_KEY:
            request_headers["Authorization"] = f"Bearer {OLLAMA_API_KEY}"
            
        encoded_data = json.dumps(payload).encode("utf-8")
        request_object = urllib.request.Request(
            api_endpoint,
            data=encoded_data,
            headers=request_headers,
            method="POST"
        )
        with urllib.request.urlopen(request_object, timeout=90) as response_stream:
            return json.loads(response_stream.read().decode("utf-8"))
            
    try:
        response_json = await asyncio.to_thread(call)
        content_string = response_json["message"]["content"]
        return json.loads(content_string)
    except urllib.error.HTTPError as exception:
        if exception.code == 404:
            raise ProcessError(
                f"Model '{model_name}' not found inside your Ollama server. "
                f"Please verify model installation or cloud availability."
            ) from exception
        raise ProcessError(f"Ollama server returned error code {exception.code}: {exception.reason}") from exception
    except Exception as exception:
        _LOGGER.error("Local Ollama request failed: %s", exception)
        raise ProcessError(f"Could not reach Ollama: {exception}") from exception


# -----------------------------------------------------------------------------
# Multi-Agent Strategy Brain (Ollama-Powered Local Interface)
# -----------------------------------------------------------------------------


class MultiAgentStrategyBrain:
    """Orchestrates market scans, prioritizes symbols, and queries local LLMs."""

    def __init__(
        self, 
        market_service: MarketDataService, 
        portfolio: PortfolioEngine, 
        events: EventStore,
        app_instance: FastAPI,
        exec_engine: Any = None
    ) -> None:
        self.market: MarketDataService = market_service
        self.portfolio: PortfolioEngine = portfolio
        self.events: EventStore = events
        self.app: FastAPI = app_instance
        self.exec_engine: Any = exec_engine

    async def autonomous_loop(self) -> None:
        """Monitors subscribed assets and coordinates non-blocking execution runs."""
        logger.info("Autonomous Brain Loop initialized.")
        while True:
            try:
                if not self.exec_engine:
                    logger.warning("Autonomous Brain Loop: No execution engine found. Standing by.")
                    await asyncio.sleep(10)
                    continue

                if getattr(self.app.state, "autonomous_trading", False) and not self.app.state.risk.killed:
                    all_subscribed = set()
                    for session_subs in manager.subscriptions.values():
                        all_subscribed.update(session_subs)
                    
                    if not_subscribed := not all_subscribed:
                        await asyncio.sleep(30)
                        continue

                    # Priority evaluation
                    volatility_data = []
                    for symbol in all_subscribed:
                        try:
                            snapshot = await self.market.get_snapshot(symbol)
                            volatility_data.append({"symbol": symbol, "volatility": snapshot.indicators.get("volatility", 0.0)})
                        except Exception: 
                            continue

                    analysis_order = list(all_subscribed)
                    if len(volatility_data) > 1:
                        try:
                            priority_prompt = f"Given these symbols and their recent volatility metrics: {json.dumps(volatility_data)}, return a JSON list of symbols prioritized for analysis (most important first) matching the SymbolPrioritization schema."
                            system_prompt = "You are a prioritization router. Analyze the volatility metrics and output a JSON object matching this schema: {'prioritized_symbols': ['BTC/USDT', ...]}"
                            
                            priority_result = await query_ollama_json(priority_prompt, system_prompt, OLLAMA_MODEL)
                            prioritized_model = SymbolPrioritization.model_validate(priority_result)
                            analysis_order = prioritized_model.prioritized_symbols
                        except Exception as exception:
                            logger.error("Symbol prioritization failed: %s. Falling back to default order.", exception)

                    # Cool-off logic: skip symbols with recent execution errors (15 min window)
                    cutoff_err_iso = (datetime.utcnow() - timedelta(minutes=15)).isoformat()
                    
                    def get_errors():
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute(
                            "SELECT data FROM event_store WHERE event_type = 'strategy_error' AND timestamp >= ?",
                            (cutoff_err_iso,)
                        )
                        rows = cursor.fetchall()
                        conn.close()
                        return rows
                        
                    err_rows = await asyncio.to_thread(get_errors)
                    skip_symbols = {json.loads(r[0]).get("symbol") for r in err_rows}
                    skip_symbols.discard(None)

                    for symbol in analysis_order:
                        if symbol in skip_symbols:
                            logger.info("Skipping %s due to recent strategy error cooling-off.", symbol)
                            continue
                        try:
                            proposal = await self.generate_proposal(symbol)
                            if proposal and proposal.side != SignalSide.FLAT:
                                await self.exec_engine.execute(proposal)
                        except Exception as exception:
                            logger.error("Autonomous cycle error for %s: %s", symbol, exception)
            except Exception as loop_error:
                logger.error("Critical error inside Autonomous loop: %s", loop_error)
            
            await asyncio.sleep(60)

    async def generate_proposal(
        self, 
        symbol: str, 
        market_snapshot: MarketSnapshot | None = None, 
        portfolio_snapshot: PortfolioSnapshot | None = None
    ) -> StrategyProposal | None:
        """Perform evaluation scanning, falling back gracefully if local Ollama times out."""
        if self.events.is_circuit_broken():
            logger.warning("Strategy Brain paused: Circuit breaker active.")
            return None

        try:
            snapshot = market_snapshot or await self.market.get_snapshot(symbol)
            portfolio_snap = portfolio_snapshot or await self.portfolio.refresh_state()

            # Incorporate past critiques to establish historical learning context
            hist_context = ""
            def get_past_evals():
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT rationale, qualitative_score, critique FROM strategy_evaluations WHERE symbol = ? ORDER BY timestamp DESC LIMIT 5",
                    (symbol,)
                )
                rows = cursor.fetchall()
                conn.close()
                return rows

            try:
                eval_rows = await asyncio.to_thread(get_past_evals)
                if eval_rows:
                    hist_context = "\nRecent historical evaluations for this symbol:\n"
                    for row in eval_rows:
                        hist_context += f"- Rationale: {row[0]} | Score: {row[1]}/10 | Critique: {row[2]}\n"
                    hist_context += "Use these past evaluations to avoid repeating logic that resulted in low scores or poor qualitative outcomes.\n"
            except Exception as db_error:
                logger.warning("Could not fetch historical evaluations: %s", db_error)

            try:
                prompt = f"""You are a professional trading team. 
Symbol: {symbol} | Price: {snapshot.last_price} | Balance: {portfolio_snap.total_equity} USDT
Indicators: RSI: {snapshot.indicators['rsi']:.2f}, Volatility: {snapshot.indicators['volatility']:.4f}
{hist_context}
Propose ONE StrategyProposal based on the provided schema. 
CRITICAL: Use 'volatility' to set 'trailing_stop_pct' (0.1 to 5.0)."""

                system_prompt = (
                    "You are a professional quantitative strategy agent. Analyze market variables and output a JSON "
                    "matching this schema precisely: {'symbol': 'BTC/USDT', 'signal': 'LONG', 'amount': 0.1, "
                    "'order_type': 'market', 'conviction': 8, 'rationale': 'some text', 'trailing_stop_pct': 1.5}"
                )

                raw_proposal = await query_ollama_json(prompt, system_prompt, OLLAMA_MODEL)
                proposal = StrategyProposal.model_validate(raw_proposal)
                proposal.market_snapshot_id = str(snapshot.timestamp.timestamp())
            except Exception as llm_error:
                logger.warning("Ollama Server Unavailable: %s. Switching to deterministic fallback.", llm_error)
                await self.events.emit("graceful_degradation_active", {
                    "error": str(llm_error),
                    "symbol": symbol,
                    "strategy": "rsi_trend_following"
                }, severity="WARNING")
                
                # Dynamic technical fallback
                rsi = snapshot.indicators.get("rsi", 50)
                side = SignalSide.FLAT
                if rsi < 30: side = SignalSide.LONG
                elif rsi > 70: side = SignalSide.SHORT

                proposal = StrategyProposal(
                    symbol=snapshot.symbol,
                    signal=side,
                    amount=POSITION_SIZE_HARD_CAP * 0.01 / snapshot.last_price,
                    order_type="market",
                    conviction=5,
                    rationale=f"Graceful Degradation: RSI is {rsi:.2f}",
                    trailing_stop_pct=2.0,
                    market_snapshot_id=str(snapshot.timestamp.timestamp())
                )
            
            await self.events.emit("strategy_proposal_generated", {
                "proposal": proposal.model_dump(),
                "snapshot": snapshot.model_dump(),
                "portfolio": portfolio_snap.model_dump()
            })
            return proposal
        except Exception as exception:
            await self.events.emit("strategy_error", {"symbol": symbol, "error": str(exception)}, severity="ERROR")
        return None

    async def generate_rebalance_proposals(self, target_weights: dict[str, float]) -> list[StrategyProposal]:
        """Construct market trades to align open assets with target weights."""
        proposals = []
        try:
            portfolio_snap = await self.portfolio.refresh_state()
            total_equity = portfolio_snap.total_equity
            
            if total_equity <= 0:
                logger.error("Rebalance failed: Insufficient USDT equity data.")
                return []

            for symbol, weight in target_weights.items():
                snapshot = await self.market.get_snapshot(symbol)
                current_price = snapshot.last_price
                
                base_currency = symbol.split('/')[0]
                current_qty = portfolio_snap.base_balances.get(base_currency, 0.0)
                current_notional = current_qty * current_price
                
                target_notional = total_equity * weight
                delta_notional = target_notional - current_notional
                
                if abs(delta_notional) < max(10, target_notional * 0.01):
                    continue

                proposals.append(StrategyProposal(
                    symbol=symbol,
                    signal=SignalSide.LONG if delta_notional > 0 else SignalSide.SHORT,
                    amount=abs(delta_notional) / current_price,
                    price=current_price,
                    order_type="market",
                    conviction=10,
                    rationale=f"Dynamic rebalance to {weight*100}% target weight."
                ))
        except Exception as exception:
            logger.error("Error generating rebalance proposals: %s", exception)
        return proposals


# -----------------------------------------------------------------------------
# Replay Audit Service
# -----------------------------------------------------------------------------


class ReplayService:
    """Simulates trading strategies over previous session database events."""

    def __init__(self, db_path: str, brain: MultiAgentStrategyBrain, risk: RiskEngine) -> None:
        self.db_path: str = db_path
        self.brain: MultiAgentStrategyBrain = brain
        self.risk: RiskEngine = risk

    async def simulate_decision_process(self, session_id: str) -> dict[str, Any]:
        """Re-runs risk rules and metrics over old database snapshots."""
        logger.info("Starting Replay Simulation for Session: %s", session_id)
        
        def load_events():
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT event_type, data, timestamp FROM event_store WHERE session_id = ? ORDER BY timestamp",
                (session_id,)
            )
            rows = cursor.fetchall()
            conn.close()
            return rows

        rows = await asyncio.to_thread(load_events)

        historical_outcomes = {}
        for row in rows:
            ev_type, ev_data_str, _ = row
            ev_data = json.loads(ev_data_str)
            if ev_type == "trade_executed":
                historical_outcomes[ev_data["proposal_id"]] = "executed"
            elif ev_type == "trade_rejected":
                historical_outcomes[ev_data["proposal_id"]] = "rejected"

        comparisons = []
        summary = {
            "total_events": len(rows), 
            "proposals_evaluated": 0, 
            "risk_passed": 0, 
            "risk_failed": 0,
            "discrepancies": 0
        }

        for row in rows:
            ev_type, ev_data_str, timestamp = row
            if ev_type == "strategy_proposal_generated":
                summary["proposals_evaluated"] += 1
                data = json.loads(ev_data_str)
                
                hist_proposal = StrategyProposal.model_validate(data["proposal"])
                hist_snapshot = MarketSnapshot.model_validate(data["snapshot"])
                hist_portfolio = PortfolioSnapshot.model_validate(data["portfolio"])
                
                risk_passed, _ = await self.risk.validate_proposal(
                    hist_proposal, 
                    hist_snapshot, 
                    portfolio_state=hist_portfolio
                )
                
                sim_outcome = "passed" if risk_passed else "failed"
                hist_outcome = historical_outcomes.get(hist_proposal.proposal_id, "lost")
                
                hist_logic_outcome = "passed" if hist_outcome == "executed" else "failed" if hist_outcome == "rejected" else "unknown"
                has_discrepancy = (sim_outcome != hist_logic_outcome)
                
                if risk_passed: summary["risk_passed"] += 1
                else: summary["risk_failed"] += 1
                if has_discrepancy: summary["discrepancies"] += 1

                comparisons.append({
                    "proposal_id": hist_proposal.proposal_id,
                    "timestamp": timestamp,
                    "symbol": hist_proposal.symbol,
                    "simulated_risk_outcome": sim_outcome,
                    "historical_outcome": hist_outcome,
                    "discrepancy": has_discrepancy,
                    "historical_rationale": hist_proposal.rationale
                })

        return {
            "session_id": session_id,
            "report_timestamp": datetime.utcnow().isoformat(),
            "summary": summary,
            "details": comparisons
        }


# -----------------------------------------------------------------------------
# Strategy Evaluator & Reporter
# -----------------------------------------------------------------------------


class StrategyEvaluator:
    """Evaluates proposal rationale accuracy and generates performance reports."""

    def __init__(self, db_path: str, market: MarketDataService, events: EventStore, risk: RiskEngine) -> None:
        self.db_path: str = db_path
        self.market: MarketDataService = market
        self.events: EventStore = events
        self.risk: RiskEngine = risk

    async def evaluation_loop(self, interval: int = 300, evaluation_window_sec: int = 900) -> None:
        """Routinely evaluate previous proposals after the target window threshold."""
        logger.info("Strategy Evaluator Loop started.")
        while True:
            try:
                cutoff_ts = datetime.utcnow().timestamp() - evaluation_window_sec
                cutoff_iso = datetime.fromtimestamp(cutoff_ts).isoformat()
                
                def load_targets():
                    conn = sqlite3.connect(self.db_path)
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT data FROM event_store WHERE event_type = 'strategy_proposal_generated' AND timestamp <= ? ORDER BY timestamp DESC LIMIT 20",
                        (cutoff_iso,)
                    )
                    rows = cursor.fetchall()
                    conn.close()
                    return rows

                rows = await asyncio.to_thread(load_targets)
                for row in rows:
                    data = json.loads(row[0])
                    proposal = data["proposal"]
                    pid = proposal["proposal_id"]
                    
                    # Deduplicate evaluations
                    def check_exists():
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute("SELECT 1 FROM strategy_evaluations WHERE proposal_id = ?", (pid,))
                        exists = cursor.fetchone() is not None
                        conn.close()
                        return exists

                    if await asyncio.to_thread(check_exists):
                        continue
                    
                    symbol = proposal["symbol"]
                    side = proposal["side"]
                    entry_price = data["snapshot"]["last_price"]
                    
                    current_snap = await self.market.get_snapshot(symbol)
                    exit_price = current_snap.last_price
                    
                    # Directional BPS Alpha calculations
                    perf_pct = (exit_price - entry_price) / entry_price
                    if side == "SHORT":
                        perf_pct = -perf_pct
                    
                    score = round(perf_pct * 10000, 2)
                    
                    intended_notional = proposal["amount"] * entry_price
                    mu = data["portfolio"].get("margin_utilization", 0)
                    leverage = self.risk.calculate_dynamic_leverage(mu)

                    prompt = f"""Evaluate this trading rationale:
                    Symbol: {symbol} | Side: {side} | Notional: {intended_notional:.2f} USDT | Leverage: {leverage}x
                    Historical Rationale: {proposal['rationale']}
                    Performance after {evaluation_window_sec}s: {score} bps
                    Compare the reasoning to the outcome and provide a score (1-10) and critique."""
                    
                    system_prompt = (
                        "You are an expert quantitative risk auditor. Analyze the rationale and the output "
                        "and return a JSON matching this schema: {'score': 7, 'critique': 'some critique'}"
                    )
                    
                    qual_json = await query_ollama_json(prompt, system_prompt, OLLAMA_MODEL)
                    qual_eval = QualitativeEvaluation.model_validate(qual_json)
                    
                    eval_doc = {
                        "proposal_id": pid,
                        "symbol": symbol,
                        "performance_bps": score,
                        "qualitative_score": qual_eval.score,
                        "critique": qual_eval.critique,
                        "rationale": proposal["rationale"],
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    
                    def save_eval():
                        conn = sqlite3.connect(self.db_path)
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT OR REPLACE INTO strategy_evaluations VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (pid, symbol, score, qual_eval.score, qual_eval.critique, proposal["rationale"], eval_doc["timestamp"])
                        )
                        conn.commit()
                        conn.close()

                    await asyncio.to_thread(save_eval)
                    await self.events.emit("strategy_evaluation", eval_doc)
                    logger.info("Evaluated rationale for %s: %f bps", pid, score)
            except Exception as exception:
                logger.error("Strategy Evaluator error: %s", exception)
            await asyncio.sleep(interval)

    async def report_loop(self, interval: int = 604800) -> None:
        """Compile weekly performance metrics and store structured JSON reports."""
        logger.info("Weekly Performance Report Loop started.")
        while True:
            try:
                lookback_ts = time.time() - (7 * 86400)
                lookback_iso = datetime.fromtimestamp(lookback_ts).isoformat()
                
                def load_evals():
                    conn = sqlite3.connect(self.db_path)
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT performance_bps, qualitative_score, critique, rationale, symbol FROM strategy_evaluations WHERE timestamp >= ?",
                        (lookback_iso,)
                    )
                    rows = cursor.fetchall()
                    conn.close()
                    return rows

                rows = await asyncio.to_thread(load_evals)
                
                if not rows:
                    logger.info("No evaluations found for weekly report period.")
                else:
                    evals = [
                        {
                            "performance_bps": r[0],
                            "qualitative_score": r[1],
                            "critique": r[2],
                            "rationale": r[3],
                            "symbol": r[4]
                        } for r in rows
                    ]
                    total_bps = sum(e["performance_bps"] for e in evals)
                    avg_score = sum(e["qualitative_score"] for e in evals) / len(evals)
                    
                    prompt = f"""Analyze the last week of trading performance:
                    Period: {lookback_iso} to {datetime.utcnow().isoformat()}
                    Total Trades Evaluated: {len(evals)}
                    Net Performance (BPS): {total_bps}
                    Average Qualitative Score: {avg_score:.2f}
                    
                    Full Evaluation Data: {json.dumps(evals)}
                    
                    Generate a structured performance report summarizing trade reasoning accuracy, 
                    overall profitability, and learnings."""

                    system_prompt = (
                        "You are a quantitative investment director. Generate a comprehensive weekly report "
                        "matching this schema: {'report_period': 'last week', 'total_evaluations': 5, "
                        "'average_score': 7.5, 'net_performance_bps': 120.0, 'executive_summary': 'summary', "
                        "'key_learnings': ['learning 1']}"
                    )
                    
                    report_json = await query_ollama_json(prompt, system_prompt, OLLAMA_MODEL)
                    report = PerformanceReport.model_validate(report_json)
                    
                    def save_report():
                        conn = sqlite3.connect(self.db_path)
                        cursor = conn.cursor()
                        cursor.execute(
                            "INSERT INTO performance_reports (report, timestamp) VALUES (?, ?)",
                            (json.dumps(report.model_dump()), datetime.utcnow().isoformat())
                        )
                        conn.commit()
                        conn.close()

                    await asyncio.to_thread(save_report)
                    await self.events.emit("weekly_report_generated", report.model_dump())
                    logger.info("Weekly performance report compiled successfully.")
                    
            except Exception as exception:
                logger.error("Weekly report generation failed: %s", exception)
            
            await asyncio.sleep(interval)


# -----------------------------------------------------------------------------
# Execution Engine
# -----------------------------------------------------------------------------


class ExecutionEngine:
    """Manages trade reconciliation, trailing stop monitors, and order execution."""

    def __init__(self, ccxt_adapter: CCXTAdapter, risk: RiskEngine, events: EventStore, registry: TaskRegistry) -> None:
        self.ccxt: CCXTAdapter = ccxt_adapter
        self.risk: RiskEngine = risk
        self.events: EventStore = events
        self.registry: TaskRegistry = registry
        self._lock: asyncio.Lock = asyncio.Lock()

    def _calculate_slippage(self, expected: float, actual: float, side: SignalSide) -> float:
        if not expected or not actual: 
            return 0.0
        diff = actual - expected
        return (diff / expected) * 100.0 if side == SignalSide.LONG else -(diff / expected) * 100.0

    async def reconciliation_loop(self, interval: int = 60) -> None:
        """Regularly audit active leverage parameters and cancel orphan orders."""
        logger.info("Reconciliation Loop started.")
        while True:
            try:
                # 1. Leverage Alignment
                portfolio_state = await self.risk.portfolio.refresh_state()
                target_leverage = self.risk.calculate_dynamic_leverage(portfolio_state.margin_utilization)
                
                if MODE != "paper":
                    for pos in portfolio_state.positions:
                        if int(pos.leverage) != target_leverage:
                            logger.info("Reconciliation: Aligning leverage for %s to %dx", pos.symbol, target_leverage)
                            try:
                                await self.ccxt.set_leverage(pos.symbol, target_leverage)
                            except Exception as exception:
                                logger.error("Failed to reconcile leverage for %s: %s", pos.symbol, exception)

                # 2. Orphan Order Checks
                open_orders = await self.ccxt.fetch_open_orders()
                for order in open_orders:
                    order_id = str(order['id'])
                    
                    def check_ledger():
                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute("SELECT 1 FROM execution_ledger WHERE order_id = ?", (order_id,))
                        exists = cursor.fetchone() is not None
                        conn.close()
                        return exists

                    exists = await asyncio.to_thread(check_ledger)
                    if not exists:
                        order_ts = order.get('timestamp')
                        if order_ts:
                            age_sec = (time.time() * 1000 - order_ts) / 1000
                            if age_sec > ORPHAN_AGE_THRESHOLD_SEC:
                                await self.ccxt.cancel_order(order_id, order['symbol'])
                                await self.events.emit("orphan_resolved", {
                                    "order_id": order_id,
                                    "symbol": order['symbol'],
                                    "age_sec": age_sec,
                                    "policy": "auto_cancel_on_timeout"
                                }, severity="WARNING")
                            else:
                                await self.events.emit("orphan_detected", {
                                    "order_id": order_id,
                                    "symbol": order['symbol'],
                                    "age_sec": age_sec
                                }, severity="CRITICAL")
            except Exception as exception:
                logger.error("Reconciliation Loop error: %s", exception)
            await asyncio.sleep(interval)

    async def execute(self, proposal: StrategyProposal, dry_run: bool = False) -> OrderSnapshot | None:
        """Perform centralized lock serialization, validate risk gates, and place order."""
        async with self._lock:
            ikey = proposal.get_idempotency_key()
            
            # Idempotency Verification
            def check_exists():
                conn = sqlite3.connect(DB_PATH)
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM execution_ledger WHERE idempotency_key = ?", (ikey,))
                exists = cursor.fetchone() is not None
                conn.close()
                return exists

            if not dry_run:
                if await asyncio.to_thread(check_exists):
                    logger.warning("Duplicate execution blocked: %s", ikey)
                    return None
            
            try:
                market = await self.ccxt.fetch_ticker(proposal.symbol)
                market_snapshot = MarketSnapshot(
                    symbol=proposal.symbol,
                    timestamp=datetime.utcnow(),
                    last_price=market['last'],
                    bid=market.get('bid', 0.0), 
                    ask=market.get('ask', 0.0), 
                    volume=market.get('baseVolume', 0.0)
                )
            except Exception as exception:
                logger.error("Execution failed: Could not fetch market data for %s: %s", proposal.symbol, exception)
                return None

            portfolio_state = await self.risk.portfolio.refresh_state()
            passed, reason = await self.risk.validate_proposal(proposal, market_snapshot, portfolio_state=portfolio_state)
            if not passed:
                if not dry_run:
                    await self.events.emit("trade_rejected", {"proposal_id": proposal.proposal_id})
                    await manager.broadcast({"type": "risk_rejection", "proposal_id": proposal.proposal_id, "reason": reason})
                return None

            target_leverage = self.risk.calculate_dynamic_leverage(portfolio_state.margin_utilization)

            if dry_run:
                logger.info("DRY RUN: Proposal %s passed risk check successfully.", proposal.proposal_id)
                return OrderSnapshot(
                    order_id=f"dry_{uuid.uuid4().hex[:8]}",
                    symbol=proposal.symbol,
                    side=OrderSide.BUY if proposal.side == SignalSide.LONG else OrderSide.SELL,
                    amount=proposal.amount,
                    price=market_snapshot.last_price,
                    status="dry_run",
                    filled=0.0,
                    timestamp=datetime.utcnow()
                )

            try:
                if ENABLE_CHAOS_TEST and random.random() < 0.1:
                    if random.random() < 0.5:
                        raise ccxt.RequestTimeout("Chaos Test: Simulated Network Timeout")
                    else:
                        raise Exception("Chaos Test: Simulated Exchange Rejection")

                if MODE != "paper":
                    try:
                        await self.ccxt.set_leverage(proposal.symbol, target_leverage)
                    except Exception as exception:
                        logger.warning("Non-fatal error setting leverage for %s: %s", proposal.symbol, exception)

                try:
                    order = await self.ccxt.place_order(proposal)
                except (ccxt.NetworkError, ccxt.ExchangeError) as exception:
                    raise ExchangeExecutionError(
                        f"Exchange execution failed for {proposal.symbol}: {str(exception)}", 
                        symbol=proposal.symbol, 
                        original_error=exception
                    ) from exception

                if MODE == "paper":
                    multiplier = 1.0 + (SIMULATED_SLIPPAGE_BPS / 10000.0)
                    order.price = (order.price or market_snapshot.last_price) * (multiplier if proposal.side == SignalSide.LONG else (2.0 - multiplier))
                
                if order.proposal_price is None:
                    order.proposal_price = market_snapshot.last_price
                
                order.slippage_adjusted_notional = order.amount * (order.price or 0.0)
                order = OrderSnapshot(**order.model_dump())
                
                # Persist trade details to ledger
                def save_ledger():
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute(
                        "INSERT INTO execution_ledger VALUES (?, ?, ?, ?, ?)",
                        (ikey, proposal.proposal_id, order.order_id, order.status, datetime.utcnow().isoformat())
                    )
                    conn.commit()
                    conn.close()

                await asyncio.to_thread(save_ledger)

                slippage_pct = self._calculate_slippage(market_snapshot.last_price, order.price, proposal.side)

                if proposal.trailing_stop_pct and order.status in ["open", "closed"]:
                    self.registry.register(
                        f"tsl_{order.order_id}", 
                        self.monitor_trailing_stop(proposal, order)
                    )
                
                # Update simulated paper positions in RAM for keyless mode
                if MODE == "paper" and (not API_KEY or not API_SECRET):
                    engine = self.risk.portfolio
                    current_positions = list(engine._paper_positions)
                    
                    if proposal.side == SignalSide.FLAT:
                        current_positions = [p for p in current_positions if p.symbol != proposal.symbol]
                    else:
                        position_exists = False
                        for pos in current_positions:
                            if pos.symbol == proposal.symbol:
                                pos.side = proposal.side
                                pos.entry_price = order.price or market_snapshot.last_price
                                pos.contracts = proposal.amount
                                pos.notional = proposal.amount * pos.entry_price
                                pos.timestamp = datetime.utcnow()
                                position_exists = True
                                break
                        if not position_exists:
                            current_positions.append(PositionSnapshot(
                                symbol=proposal.symbol,
                                side=proposal.side,
                                entry_price=order.price or market_snapshot.last_price,
                                contracts=proposal.amount,
                                notional=proposal.amount * (order.price or market_snapshot.last_price),
                                unrealized_pnl=0.0,
                                leverage=1.0,
                                timestamp=datetime.utcnow()
                            ))
                    engine._paper_positions = current_positions
                    
                # Fix: model_dump() before WS broadcast to prevent Pydantic serialization crash
                await self.events.emit("trade_executed", {
                    "order": order.model_dump(),
                    "slippage_pct": slippage_pct,
                    "expected_price": market_snapshot.last_price
                })
                return order
            except ExchangeExecutionError as exception:
                await self.events.emit("execution_failed", {
                    "symbol": exception.symbol or "unknown",
                    "error": str(exception.original_error)
                }, severity="CRITICAL")
                return None
            except Exception as exception:
                await self.events.emit("execution_failed", {"error": str(exception)}, severity="CRITICAL")
                return None

    async def monitor_trailing_stop(self, proposal: StrategyProposal, order: OrderSnapshot) -> None:
        """Asynchronously monitor trailing stop bounds, executing market close on trigger."""
        symbol = proposal.symbol
        side = proposal.side
        trail_pct = (proposal.trailing_stop_pct or 2.0) / 100.0
        
        try:
            entry_price = order.price or 0.0
            highest_price = lowest_price = entry_price
            current_price = entry_price
            
            while True:
                await asyncio.sleep(10) 
                
                if self.risk.killed:
                    logger.info("TSL Monitor for %s stopping due to system kill switch.", symbol)
                    return

                ticker = await self.ccxt.fetch_ticker(symbol)
                current_price = ticker["last"]
                
                if side == SignalSide.LONG:
                    highest_price = max(highest_price, current_price)
                    trigger_price = highest_price * (1.0 - trail_pct)
                    if current_price <= trigger_price:
                        break
                elif side == SignalSide.SHORT:
                    lowest_price = min(lowest_price, current_price)
                    trigger_price = lowest_price * (1.0 + trail_pct)
                    if current_price >= trigger_price:
                        break

            exit_proposal = StrategyProposal(
                symbol=symbol, 
                signal=SignalSide.FLAT, 
                amount=proposal.amount, 
                order_type="market",
                rationale="Trailing Stop Loss Triggered", 
                conviction=10
            )
            await self.ccxt.place_order(exit_proposal)
            await self.events.emit("tsl_exit", {"symbol": symbol, "exit_price": current_price})
        except Exception as exception:
            logger.error("TSL Monitoring failed for %s: %s", symbol, exception)


# ========================= LIFESPAN RESOURCES =========================

@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    # Initialize Local SSoT SQLite tables
    init_db()

    events_store = EventStore()
    registry = TaskRegistry()
    
    # Dependency injections
    ccxt_adapter = CCXTAdapter(events_store)
    await ccxt_adapter.initialize()
    
    market_service = MarketDataService(ccxt_adapter)
    portfolio_engine = PortfolioEngine(ccxt_adapter)
    risk_engine = RiskEngine()
    await risk_engine.initialize(portfolio_engine)
    
    # Inject FastAPI app reference into MultiAgentStrategyBrain to prevent NameError
    strategy_brain = MultiAgentStrategyBrain(market_service, portfolio_engine, events_store, app_instance)
    exec_engine = ExecutionEngine(ccxt_adapter, risk_engine, events_store, registry)
    
    # Bind back-references safely
    strategy_brain.exec_engine = exec_engine
    
    registry.register("reconciliation_loop", exec_engine.reconciliation_loop())
    app_instance.state.autonomous_trading = False
    registry.register("autonomous_trading", strategy_brain.autonomous_loop())
    registry.register("portfolio_snapshot", portfolio_engine.snapshot_loop())
    registry.register("market_stream", market_service.stream_loop(["BTC/USD", "ETH/USD"] if EXCHANGE_ID == "kraken" else ["BTC/USDT", "ETH/USDT"]))
    
    evaluator = StrategyEvaluator(DB_PATH, market_service, events_store, risk_engine)
    registry.register("strategy_evaluator", evaluator.evaluation_loop())
    registry.register("performance_report", evaluator.report_loop())
    replay_service = ReplayService(DB_PATH, strategy_brain, risk_engine)
    
    # Register global state objects
    app_instance.state.strategy_brain = strategy_brain
    app_instance.state.exec_engine = exec_engine
    app_instance.state.risk = risk_engine
    app_instance.state.events = events_store
    app_instance.state.registry = registry
    app_instance.state.replay = replay_service

    await events_store.emit("system_startup", {"mode": MODE, "exchange": EXCHANGE_ID})

    yield

    # System Shutdown Lifecycle
    await registry.shutdown()
    await ccxt_adapter.close()
    await events_store.emit("system_shutdown", {})


app: FastAPI = FastAPI(title="Drox Trade Post vNext", lifespan=lifespan)


# ========================= CONTROLLER HTTP & WS ROUTES =========================

@app.get("/", response_class=HTMLResponse)
async def root_index(request: Request):
    """Retrieve the index page content."""
    local_html = Path("index.html")
    if local_html.exists():
        return HTMLResponse(content=local_html.read_text(encoding="utf-8"))
    
    return HTMLResponse(content="""
    <!DOCTYPE html>
    <html>
    <head><title>Drox Trading Post</title>
    <style>body{font-family:system-ui;background:#0a0a0a;color:#e0e0e0;padding:20px;}</style>
    </head>
    <body>
        <h1>Drox Trading Post — Standalone Web Gateway</h1>
        <button onclick="sendCmd('KILL')">EMERGENCY SHUTDOWN</button>
        <button onclick="sendCmd('START_AUTO')">Start Autonomous Scanning</button>
        <button onclick="sendCmd('STOP_AUTO')">Stop Autonomous Scanning</button>
        <div id="log" style="height:60vh;overflow:auto;background:#111;padding:15px;margin-top:20px;font-family:monospace;border:1px solid #333;"></div>
        <script>
            const ws = new WebSocket(`ws://${window.location.host}/ws/control`);
            ws.onmessage = e => {
                const div = document.createElement('div');
                div.textContent = `${new Date().toLocaleTimeString()} ${e.data}`;
                const log = document.getElementById('log');
                log.appendChild(div);
                log.scrollTop = log.scrollHeight;
            };
            function sendCmd(cmd){ ws.send(JSON.stringify({cmd: cmd})); }
        </script>
    </body>
    </html>
    """)


@app.get("/healthz")
async def healthz():
    """Cloud-native status and health diagnostics."""
    diagnostics = {"ccxt": "healthy", "risk_killed": app.state.risk.killed}
    try:
        await app.state.ccxt.exchange.fetch_time()
        return diagnostics
    except Exception as exception:
        logger.error("Health diagnostics check failed: %s", exception)
        diagnostics["ccxt"] = "unhealthy"
        diagnostics["error"] = str(exception)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=diagnostics
        )


@app.get("/replay/{session_id}")
async def trigger_replay(session_id: str):
    """Trigger a historical session re-run audit by ID."""
    try:
        report = await app.state.replay.simulate_decision_process(session_id)
        return report
    except Exception as exception:
        logger.error("Replay failed for session %s: %s", session_id, exception)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": str(exception)}
        )


@app.websocket("/ws/control")
async def control_ws(websocket: WebSocket):
    """Central WebSocket routing loop supporting dynamic rebalances and prioritisations."""
    sid = str(uuid.uuid4())
    await manager.connect(websocket, sid)
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            cmd = msg.get("cmd")
            
            if cmd == "KILL":
                await app.state.risk.kill_switch("human_override")
                await manager.broadcast({"type": "kill_activated"})
            elif cmd == "START_AUTO":
                app.state.autonomous_trading = True
                await manager.broadcast({"type": "system_status", "autonomous": True})
            elif cmd == "STOP_AUTO":
                app.state.autonomous_trading = False
                await manager.broadcast({"type": "system_status", "autonomous": False})
            elif cmd == "ANALYZE":
                symbol = msg.get("symbol", "BTC/USDT")
                await manager.broadcast({"type": "brain_thinking", "symbol": symbol})
                proposal = await app.state.strategy_brain.generate_proposal(symbol)
                if proposal:
                    result = await app.state.exec_engine.execute(proposal)
                    await manager.broadcast({
                        "type": "execution", 
                        "proposal": proposal.model_dump(), 
                        "result": result.model_dump() if result else None
                    })
            elif cmd == "GET_REPORTS":
                def get_reports():
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("SELECT report, timestamp FROM performance_reports ORDER BY timestamp DESC LIMIT 5")
                    rows = cursor.fetchall()
                    conn.close()
                    return [{"report": json.loads(r[0]), "timestamp": r[1]} for r in rows]
                reports = await asyncio.to_thread(get_reports)
                await manager.broadcast({"type": "performance_reports", "data": reports})
            elif cmd == "GET_EVALS":
                def get_evals():
                    conn = sqlite3.connect(DB_PATH)
                    cursor = conn.cursor()
                    cursor.execute("SELECT proposal_id, symbol, performance_bps, qualitative_score, critique, rationale, timestamp FROM strategy_evaluations ORDER BY timestamp DESC LIMIT 10")
                    rows = cursor.fetchall()
                    conn.close()
                    return [
                        {
                            "proposal_id": r[0],
                            "symbol": r[1],
                            "performance_bps": r[2],
                            "qualitative_score": r[3],
                            "critique": r[4],
                            "rationale": r[5],
                            "timestamp": r[6]
                        } for r in rows
                    ]
                evals = await asyncio.to_thread(get_evals)
                await manager.broadcast({"type": "evaluations", "data": evals})
            elif cmd == "REPLAY":
                replay_sid = msg.get("session_id")
                report = await app.state.replay.simulate_decision_process(replay_sid)
                await manager.broadcast({"type": "replay_report", "data": report})
            elif cmd == "REBALANCE":
                allocations = msg.get("allocations", {"BTC/USDT": 0.5, "ETH/USDT": 0.5})
                proposals = await app.state.strategy_brain.generate_rebalance_proposals(allocations)
                for prop in proposals:
                    res = await app.state.exec_engine.execute(prop)
                    await manager.broadcast({
                        "type": "rebalance_step", 
                        "proposal": prop.model_dump(), 
                        "result": res.model_dump() if res else None
                    })
            elif cmd == "SUBSCRIBE":
                symbols = msg.get("symbols", [])
                await manager.subscribe(sid, symbols)
            elif cmd == "UNSUBSCRIBE":
                symbols = msg.get("symbols", [])
                await manager.unsubscribe(sid, symbols)
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(sid)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)