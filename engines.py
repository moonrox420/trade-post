import asyncio
import logging
import random
import time
import uuid
from datetime import datetime
from typing import Optional
from google.cloud.firestore import AsyncClient
import ccxt.async_support as ccxt

from exceptions import ExchangeExecutionError
from models import (
    StrategyProposal,
    MarketSnapshot,
    PortfolioSnapshot,
    OrderSnapshot,
    SignalSide,
    OrderSide,
)
from config import (
    MODE,
    MAX_DAILY_LOSS_PCT,
    MAX_POSITION_PCT,
    ORPHAN_AGE_THRESHOLD_SEC,
    ENABLE_CHAOS_TEST,
    SIMULATED_SLIPPAGE_BPS,
)
from runtime import manager
from services import PortfolioEngine

logger = logging.getLogger(__name__)


class RiskEngine:
    def __init__(self) -> None:
        self.killed: bool = False
        self.starting_equity: float | None = None
        self.db: AsyncClient | None = None
        self.portfolio: PortfolioEngine | None = None

    async def initialize(self, db: AsyncClient, portfolio: PortfolioEngine) -> None:
        self.db = db
        self.portfolio = portfolio
        try:
            document_snapshot = await (
                db.collection("system_state")
                .document("risk_engine_state")
                .get()
            )
            if document_snapshot.exists:
                restored_state = document_snapshot.to_dict() or {}
                self.starting_equity = restored_state.get("starting_equity")
                self.killed = restored_state.get("killed", False)
        except Exception as exception:
            logger.error("Failed to restore RiskEngine state: %s", exception)

    async def _persist_state(self) -> None:
        db = self.db
        if db is None:
            return
        try:
            await (
                db.collection("system_state")
                .document("risk_engine_state")
                .set(
                    {
                        "starting_equity": self.starting_equity,
                        "killed": self.killed,
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                )
            )
        except Exception as exception:
            logger.error("Failed to persist RiskEngine state: %s", exception)

    def calculate_dynamic_leverage(self, margin_utilization: float) -> int:
        if margin_utilization > 80:
            return 1
        if margin_utilization > 60:
            return 2
        if margin_utilization > 40:
            return 3
        if margin_utilization > 20:
            return 5
        return 10

    async def validate_proposal(
        self,
        proposal: StrategyProposal,
        market: MarketSnapshot,
        portfolio_state: Optional[PortfolioSnapshot] = None,
    ) -> tuple[bool, str]:
        if self.killed:
            return False, "Kill switch active"
        if self.portfolio is None:
            return False, "Risk Engine not initialized with portfolio"
        resolved_portfolio_state = portfolio_state
        if resolved_portfolio_state is None:
            resolved_portfolio_state = await self.portfolio.refresh_state()
        if resolved_portfolio_state is None:
            return False, "Portfolio state unavailable"
        total_equity = resolved_portfolio_state.total_equity
        if total_equity <= 0:
            logger.error("Risk Engine: Insufficient USDT balance data")
            return False, "Insufficient balance data"
        if self.starting_equity is None:
            self.starting_equity = total_equity
            await self._persist_state()
            logger.info(
                f"Session starting equity initialized at {self.starting_equity} USDT"
            )
        if self.starting_equity <= 0:
            await self.kill_switch("Starting equity configuration error.")
            return False, "Equity config error"
        current_drawdown_pct = (
            (self.starting_equity - total_equity) / self.starting_equity
        ) * 100
        if current_drawdown_pct >= MAX_DAILY_LOSS_PCT:
            await self.kill_switch(
                f"Daily drawdown limit reached: {current_drawdown_pct:.2f}%"
            )
            return False, f"Drawdown limit: {current_drawdown_pct:.2f}%"
        notional_value = proposal.amount * market.last_price
        if notional_value > (total_equity * (MAX_POSITION_PCT / 100.0)):
            return False, f"Size too large: {notional_value:.2f} USDT"
        return True, "Risk validation passed"

    async def kill_switch(self, reason: str):
        self.killed = True
        logger.critical(f"KILL SWITCH: {reason}")
        await self._persist_state()


class ExecutionEngine:
    def __init__(
        self, ccxt_adapter, risk: RiskEngine, events, registry, db: AsyncClient
    ):
        self.ccxt = ccxt_adapter
        self.risk = risk
        self.events = events
        self.registry = registry
        self.db = db
        self._lock = asyncio.Lock()

    def _calculate_slippage(
        self, expected: float | None, actual: float | None, side: SignalSide
    ) -> float:
        if not expected or not actual:
            return 0.0
        diff = actual - expected
        return (
            (diff / expected) * 100
            if side == SignalSide.LONG
            else -(diff / expected) * 100
        )

    async def reconciliation_loop(self, interval: int = 60):
        logger.info("Reconciliation Loop started.")
        while True:
            try:
                if self.risk.portfolio is None:
                    logger.warning(
                        "Reconciliation Loop: Risk Engine missing portfolio. Skipping cycle."
                    )
                    await asyncio.sleep(interval)
                    continue
                portfolio_state = await self.risk.portfolio.refresh_state()
                target_leverage = self.risk.calculate_dynamic_leverage(
                    portfolio_state.margin_utilization
                )
                if MODE != "paper":
                    for pos in portfolio_state.positions:
                        if int(pos.leverage) != target_leverage:
                            logger.info(
                                f"Reconciliation: Correcting leverage for {pos.symbol} from {pos.leverage}x to {target_leverage}x"
                            )
                            try:
                                await self.ccxt.set_leverage(
                                    pos.symbol, target_leverage
                                )
                            except Exception as e:
                                logger.error(
                                    f"Failed to reconcile leverage for {pos.symbol}: {e}"
                                )
                open_orders = await self.ccxt.fetch_open_orders()
                for order in open_orders:
                    order_id = str(order["id"])
                    ledger_query = self.db.collection("execution_ledger").where(
                        "order_id", "==", order_id
                    )
                    docs = await ledger_query.get()
                    if not docs:
                        order_ts = order.get("timestamp")
                        if order_ts:
                            age_sec = (time.time() * 1000 - order_ts) / 1000
                            if age_sec > ORPHAN_AGE_THRESHOLD_SEC:
                                await self.ccxt.cancel_order(order_id, order["symbol"])
                                await self.events.emit(
                                    "orphan_resolved",
                                    {
                                        "order_id": order_id,
                                        "symbol": order["symbol"],
                                        "age_sec": age_sec,
                                        "policy": "auto_cancel_on_timeout",
                                    },
                                    severity="WARNING",
                                )
                            else:
                                await self.events.emit(
                                    "orphan_detected",
                                    {
                                        "order_id": order_id,
                                        "symbol": order["symbol"],
                                        "age_sec": age_sec,
                                    },
                                    severity="CRITICAL",
                                )
            except Exception as e:
                logger.error(f"Reconciliation Loop error: {e}")
            await asyncio.sleep(interval)

    async def execute(
        self, proposal: StrategyProposal, dry_run: bool = False
    ) -> Optional[OrderSnapshot]:
        async with self._lock:
            ikey = proposal.get_idempotency_key()
            ledger_ref = self.db.collection("execution_ledger").document(ikey)
            if not dry_run:
                ledger_doc = await ledger_ref.get()
                if ledger_doc.exists:
                    logger.warning(f"Duplicate execution blocked: {ikey}")
                    return None
            try:
                market = await self.ccxt.fetch_ticker(proposal.symbol)
                market_snapshot = MarketSnapshot(
                    symbol=proposal.symbol,
                    timestamp=datetime.utcnow(),
                    last_price=market["last"],
                    bid=market.get("bid", 0),
                    ask=market.get("ask", 0),
                    volume=market.get("baseVolume", 0),
                )
            except Exception as e:
                logger.error(
                    f"Execution failed: Could not fetch market data for {proposal.symbol}: {e}"
                )
                return None
            if self.risk.portfolio is None:
                logger.error("Execution failed: Risk Engine missing portfolio.")
                return None
            portfolio_state = await self.risk.portfolio.refresh_state()
            passed, reason = await self.risk.validate_proposal(
                proposal, market_snapshot, portfolio_state=portfolio_state
            )
            if not passed:
                if not dry_run:
                    await self.events.emit(
                        "trade_rejected", {"proposal_id": proposal.proposal_id}
                    )
                    await manager.broadcast(
                        {
                            "type": "risk_rejection",
                            "proposal_id": proposal.proposal_id,
                            "reason": reason,
                        }
                    )
                return None
            target_leverage = self.risk.calculate_dynamic_leverage(
                portfolio_state.margin_utilization
            )
            if dry_run:
                logger.info(
                    f"DRY RUN: Proposal {proposal.proposal_id} passed risk check."
                )
                return OrderSnapshot(
                    order_id=f"dry_{uuid.uuid4().hex[:8]}",
                    symbol=proposal.symbol,
                    side=OrderSide.BUY
                    if proposal.side == SignalSide.LONG
                    else OrderSide.SELL,
                    amount=proposal.amount,
                    price=market_snapshot.last_price,
                    status="dry_run",
                    filled=0,
                    timestamp=datetime.utcnow(),
                )
            try:
                if ENABLE_CHAOS_TEST and random.random() < 0.1:
                    if random.random() < 0.5:
                        raise ccxt.RequestTimeout(
                            "Chaos Test: Simulated Network Timeout"
                        )
                    else:
                        raise Exception("Chaos Test: Simulated Exchange Rejection")
                if MODE != "paper":
                    try:
                        await self.ccxt.set_leverage(proposal.symbol, target_leverage)
                    except Exception as e:
                        logger.warning(
                            f"Non-fatal error setting leverage to {target_leverage}x for {proposal.symbol}: {e}"
                        )
                try:
                    order = await self.ccxt.place_order(proposal)
                except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                    raise ExchangeExecutionError(
                        f"Exchange execution failed for {proposal.symbol}: {str(e)}",
                        symbol=proposal.symbol,
                        original_error=e,
                    )
                if MODE == "paper":
                    multiplier = 1 + (SIMULATED_SLIPPAGE_BPS / 10000)
                    order.price = (order.price or market_snapshot.last_price) * (
                        multiplier
                        if proposal.side == SignalSide.LONG
                        else (2 - multiplier)
                    )
                if order.proposal_price is None:
                    order.proposal_price = market_snapshot.last_price
                order.slippage_adjusted_notional = order.amount * (order.price or 0)
                order = OrderSnapshot(**order.model_dump())
                await ledger_ref.set(
                    {
                        "proposal_id": proposal.proposal_id,
                        "order_id": order.order_id,
                        "status": order.status,
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )
                slippage_pct = self._calculate_slippage(
                    market_snapshot.last_price, order.price, proposal.side
                )
                if proposal.trailing_stop_pct and order.status in ["open", "closed"]:
                    self.registry.register(
                        f"tsl_{order.order_id}",
                        self.monitor_trailing_stop(proposal, order),
                    )
                await self.events.emit(
                    "trade_executed",
                    {
                        "order": order.model_dump(),
                        "slippage_pct": slippage_pct,
                        "expected_price": market_snapshot.last_price,
                    },
                )
                return order
            except ExchangeExecutionError as e:
                await self.events.emit(
                    "execution_failed",
                    {"symbol": e.symbol, "error": str(e.original_error)},
                    severity="CRITICAL",
                )
                return None
            except Exception as e:
                await self.events.emit(
                    "execution_failed", {"error": str(e)}, severity="CRITICAL"
                )
                return None

    async def monitor_trailing_stop(
        self, proposal: StrategyProposal, order: OrderSnapshot
    ) -> None:
        """Auto-exit when the trailing stop trigger price is breached."""
        if proposal.trailing_stop_pct is None:
            logger.warning(
                "TSL Monitor: missing trailing stop for %s. Skipping.", proposal.symbol
            )
            return
        if order.price is None:
            logger.warning(
                "TSL Monitor: no fill price for order %s. Skipping.", order.order_id
            )
            return
        symbol = proposal.symbol
        side = proposal.side
        trail_pct = proposal.trailing_stop_pct / 100.0
        entry_price: float = order.price
        highest_price: float = entry_price
        lowest_price: float = entry_price
        current_price: float = entry_price
        try:
            while True:
                await asyncio.sleep(10)
                if self.risk.killed:
                    logger.info(
                        "TSL Monitor for %s stopping due to system kill switch.", symbol
                    )
                    return
                ticker = await self.ccxt.fetch_ticker(symbol)
                ticker_last = ticker.get("last")
                if ticker_last is None:
                    logger.warning(
                        "TSL Monitor: no last price for %s. Retrying.", symbol
                    )
                    continue
                current_price = float(ticker_last)
                if side == SignalSide.LONG:
                    highest_price = max(highest_price, current_price)
                    trigger_price = highest_price * (1 - trail_pct)
                    if current_price <= trigger_price:
                        break
                elif side == SignalSide.SHORT:
                    lowest_price = min(lowest_price, current_price)
                    trigger_price = lowest_price * (1 + trail_pct)
                    if current_price >= trigger_price:
                        break
            exit_proposal = StrategyProposal(
                symbol=symbol,
                signal=SignalSide.FLAT,
                amount=proposal.amount,
                order_type="market",
                rationale="Trailing Stop Loss Triggered",
                conviction=10,
                trailing_stop_pct=None,
            )
            await self.ccxt.place_order(exit_proposal)
            await self.events.emit(
                "tsl_exit", {"symbol": symbol, "exit_price": current_price}
            )
        except Exception as exception:
            logger.error("TSL Monitoring failed for %s: %s", symbol, exception)
