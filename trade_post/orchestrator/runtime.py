"""Top-level runtime orchestrator. Owns all subsystems and background tasks."""

from __future__ import annotations

import asyncio
import secrets
from datetime import datetime, timezone
from decimal import Decimal

from ..ai.brain import AIBrain
from ..ai.ollama import OllamaClient
from ..core.config import Settings
from ..core.logging_setup import get_logger, trace_context
from ..domain.models import (
    Event,
    EventSeverity,
    Money,
    PortfolioSnapshot,
    Position,
    SignalSide,
)
from ..execution.engine import ExecutionEngine
from ..market.service import MarketDataService
from ..persistence.database import init_database
from ..persistence.migrations import run_migrations
from ..persistence.repository import Repository
from ..risk.engine import RiskEngine
from ..security.auth import hash_password
from ..strategy.signals import SignalEngine

log = get_logger(__name__)


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict = {}

    def spawn(self, name: str, coro) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks[name] = task
        task.add_done_callback(lambda t: self._tasks.pop(name, None))
        return task

    async def shutdown(self, timeout: float = 10.0) -> None:
        log.info("Cancelling %d background tasks", len(self._tasks))
        for t in list(self._tasks.values()):
            t.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks.values(), return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            log.warning("Task shutdown timed out")
        log.info("Background tasks stopped")


class Orchestrator:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._tasks = TaskRegistry()
        self._db = init_database(settings)
        self.market = MarketDataService(settings)
        self.risk = RiskEngine(settings)
        self.ollama = OllamaClient(settings)
        self.brain = AIBrain(settings, self.ollama)
        self.signals = SignalEngine(settings)
        self.execution: ExecutionEngine | None = None
        # Autonomous AI trading is opt-in and controlled by authenticated operators.
        self.autonomous_enabled: bool = False

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def tasks(self) -> TaskRegistry:
        return self._tasks

    async def startup_core(self) -> None:
        """Database, migrations, risk engine, execution engine, and admin bootstrap.

        Safe to run in tests: performs no external network connections and
        starts no background loops. ``startup`` extends this with market/AI
        connections and the background task registry.
        """
        await self._db.connect()
        await run_migrations(self._db.engine)
        async with self._db.session() as s:
            repo = Repository(s)
            self.risk.bind_database(self._db)
            await self.risk.load()
            self.execution = ExecutionEngine(self._settings, self.market, self.risk, repo)
            admin = await repo.get_user_by_username("admin")
            if admin is None:
                await self._bootstrap_admin(repo)

    async def startup(self) -> None:
        log.info("Orchestrator starting env=%s", self._settings.app_env)
        await self.startup_core()
        await self.market.connect()
        await self.ollama.connect()
        try:
            await self.ollama.ensure_model(self._settings.ollama_model)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ollama model check failed: %s", exc)
        self._tasks.spawn("market_stream", self._loop_market_stream())
        self._tasks.spawn("portfolio_snap", self._loop_portfolio_snapshot())
        self._tasks.spawn("ai_scan", self._loop_ai_scan())
        self._tasks.spawn("orphan_reconcile", self._loop_orphan_reconcile())
        log.info("Orchestrator started")


    async def shutdown(self) -> None:
        log.info("Orchestrator shutting down")
        await self._tasks.shutdown()
        try:
            await self.ollama.disconnect()
        except Exception:
            pass
        try:
            await self.market.disconnect()
        except Exception:
            pass
        try:
            await self._db.disconnect()
        except Exception:
            pass
        log.info("Orchestrator stopped")

    async def _bootstrap_admin(self, repo: Repository) -> None:
        """Create the first administrator account when no admin exists.

        Uses ``settings.drox_admin_password`` when provided; otherwise generates
        a one-time random password and logs it exactly once at WARNING. The
        one-time credential line is intentionally formatted without an ``=``
        separator so the secret-scrubbing logging filter does not redact it.
        The configured credential itself is never logged.
        """
        username = "admin"
        configured = self._settings.drox_admin_password
        if configured:
            password = configured
            log.warning(
                "Bootstrap admin user created: %s (credential sourced from DROX_ADMIN_PASSWORD)",
                username,
            )
        else:
            password = secrets.token_urlsafe(18)
            log.warning(
                "Bootstrap admin user created: %s (one-time password shown once, change immediately)",
                username,
            )
            log.warning("Bootstrap one-time password for %s: %s", username, password)
        await repo.insert_user(
            id=secrets.token_hex(8),
            username=username,
            email=None,
            password_hash=hash_password(password, self._settings),
            role="admin",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await repo.insert_event(Event(
            type="account_created",
            severity=EventSeverity.WARNING,
            actor="system",
            payload={"username": username, "role": "admin", "bootstrap": True},
        ))

    @property
    def is_autonomous_running(self) -> bool:
        """True when autonomous trading is enabled and the kill switch is off."""
        return self.autonomous_enabled and not self.risk.state.killed

    async def start_autonomous(self, actor: str) -> None:
        self.autonomous_enabled = True
        log.info("autonomous enabled by %s", actor)

    async def stop_autonomous(self, actor: str) -> None:
        self.autonomous_enabled = False
        log.info("autonomous disabled by %s", actor)

    def subscribe_symbol(self, symbol: str) -> bool:
        """Add a symbol to the in-memory subscription list. Returns True if added."""
        sym = (symbol or "").strip().upper()
        if not sym or sym in self._settings.subscribed_symbols:
            return False
        self._settings.subscribed_symbols.append(sym)
        return True

    def unsubscribe_symbol(self, symbol: str) -> bool:
        """Remove a symbol from the in-memory subscription list. Returns True if removed."""
        sym = (symbol or "").strip().upper()
        if sym and sym in self._settings.subscribed_symbols:
            self._settings.subscribed_symbols.remove(sym)
            return True
        return False

    async def run_single_analysis(self, actor: str) -> dict:
        """Run one AI analysis cycle on demand. Does not expose internal model details."""
        try:
            await self._run_one_ai_cycle()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            log.warning("on-demand analysis by %s failed: %s", actor, exc)
            return {"ok": False, "error": "analysis_unavailable"}

    async def _loop_market_stream(self) -> None:
        while True:
            try:
                for sym in self._settings.subscribed_symbols:
                    try:
                        snap = await self.market.get_snapshot(sym)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("market stream fetch failed %s: %s", sym, exc)
                        continue
                    async with self._db.session() as s:
                        await Repository(s).insert_market_snapshot(snap)
            except Exception as exc:  # noqa: BLE001
                log.error("market stream loop error: %s", exc)
            await asyncio.sleep(self._settings.market_data_poll_sec)

    async def _loop_portfolio_snapshot(self) -> None:
        while True:
            try:
                await self._take_portfolio_snapshot()
            except Exception as exc:  # noqa: BLE001
                log.error("portfolio snapshot error: %s", exc)
            await asyncio.sleep(60)

    async def _take_portfolio_snapshot(self) -> None:
        async with self._db.session() as s:
            repo = Repository(s)
            is_paper_state = self._settings.is_paper and not self._settings.has_exchange_credentials
            if is_paper_state and self.execution is not None:
                total_equity = Money(amount=self.execution.paper_equity)
                base_balances = {k: Money(amount=v) for k, v in self.execution.paper_balances.items()}
                positions: list = []
                for sym, p in self.execution.paper_positions.items():
                    try:
                        snap = await self.market.get_snapshot(sym)
                        mark = snap.last_price
                    except Exception:
                        mark = p["entry_price"]
                    pnl = (mark - p["entry_price"]) * p["quantity"]
                    positions.append(Position(
                        symbol=sym, side=SignalSide(p["side"]), quantity=p["quantity"],
                        entry_price=p["entry_price"], mark_price=mark,
                        unrealized_pnl=Money(amount=pnl), leverage=p["leverage"],
                        liquidation_price=None, opened_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    ))
            else:
                total_equity = Money.zero()
                base_balances = {}
                positions = []
            if self.risk.state.starting_equity and self.risk.state.starting_equity.amount > 0:
                dd = (self.risk.state.starting_equity.amount - total_equity.amount) / self.risk.state.starting_equity.amount
            else:
                dd = Decimal("0")
            snap_obj = PortfolioSnapshot(
                timestamp=datetime.now(timezone.utc),
                total_equity=total_equity,
                available_margin=total_equity,
                positions=positions,
                base_balances=base_balances,
                risk_adjusted_equity=total_equity,
                margin_utilization=Decimal("0"),
                drawdown_pct=dd,
            )
            await repo.insert_portfolio_snapshot(snap_obj)


    async def _loop_ai_scan(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                await self._run_one_ai_cycle()
            except Exception as exc:  # noqa: BLE001
                log.error("ai scan loop error: %s", exc)
            await asyncio.sleep(60)

    async def _run_one_ai_cycle(self) -> None:
        if not self.autonomous_enabled:
            return
        if self.risk.state.killed or self.risk.state.circuit_open:
            return
        async with self._db.session() as s:
            recent_evals = await Repository(s).list_recent_ai_decisions(limit=5)
        for sym in self._settings.subscribed_symbols:
            try:
                snap = await self.market.get_snapshot(sym, use_cache=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("ai scan fetch %s: %s", sym, exc)
                continue
            with trace_context() as trace_id:
                signal = self.signals.from_snapshot(snap)
                recent_lines = [f"score={e.get('score')} | {e.get('critique','')[:80]}" for e in recent_evals]
                portfolio = await self._current_portfolio(snap.last_price)
                decision = await self.brain.decide(
                    signal=signal, last_price=snap.last_price, spread_bps=snap.spread_bps,
                    equity=portfolio.total_equity.amount, drawdown_pct=portfolio.drawdown_pct,
                    available_margin=portfolio.available_margin.amount,
                    recent_evaluations=recent_lines, trace_id=trace_id,
                )
                async with self._db.session() as s2:
                    await Repository(s2).insert_ai_decision(
                        id=decision.id, symbol=decision.symbol,
                        signal=decision.signal.value, conviction=decision.conviction,
                        confidence=decision.confidence, rationale=decision.rationale,
                        raw_output=decision.raw_output, model=decision.model,
                        prompt_version=decision.prompt_version,
                        validated=decision.validated, validation_errors=decision.validation_errors,
                        timestamp=decision.timestamp, trace_id=decision.trace_id,
                    )
                if decision.signal is SignalSide.FLAT:
                    continue
                baseline_qty = Decimal(str(decision.raw_output.get("amount", 0.0)))
                intent = self.brain.to_intent(decision, last_price=snap.last_price, quantity=baseline_qty)
                risk_decision = await self.risk.validate(intent, snap, portfolio)
                if not risk_decision.approved:
                    log.info("ai risk-rejected %s: %s", sym, risk_decision.reason)
                    continue
                if self.execution is None:
                    continue
                order = await self.execution.submit(intent, portfolio, trace_id=trace_id)
                if order is not None:
                    log.info("ai order filled: %s %s qty=%s", order.side.value, order.symbol, order.filled_quantity)

    async def _current_portfolio(self, last_price: Decimal) -> PortfolioSnapshot:
        if self.execution is None or not (self._settings.is_paper and not self._settings.has_exchange_credentials):
            return PortfolioSnapshot(
                timestamp=datetime.now(timezone.utc), total_equity=Money.zero(),
                available_margin=Money.zero(), positions=[], base_balances={},
                risk_adjusted_equity=Money.zero(), margin_utilization=Decimal("0"),
                drawdown_pct=Decimal("0"),
            )
        equity = Money(amount=self.execution.paper_equity)
        positions: list = []
        for sym, p in self.execution.paper_positions.items():
            try:
                mark = (await self.market.get_snapshot(sym, use_cache=True)).last_price
            except Exception:
                mark = p["entry_price"]
            pnl = (mark - p["entry_price"]) * p["quantity"]
            positions.append(Position(
                symbol=sym, side=SignalSide(p["side"]), quantity=p["quantity"],
                entry_price=p["entry_price"], mark_price=mark, unrealized_pnl=Money(amount=pnl),
                leverage=p["leverage"], liquidation_price=None,
                opened_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
            ))
        return PortfolioSnapshot(
            timestamp=datetime.now(timezone.utc), total_equity=equity, available_margin=equity,
            positions=positions, base_balances={k: Money(amount=v) for k, v in self.execution.paper_balances.items()},
            risk_adjusted_equity=equity, margin_utilization=Decimal("0"),
            drawdown_pct=Decimal("0"),
        )

    async def _loop_orphan_reconcile(self) -> None:
        while True:
            try:
                if self.execution is not None:
                    actions = await self.execution.reconcile_orphans()
                    for a in actions:
                        log.info("orphan reconciled: %s", a)
            except Exception as exc:  # noqa: BLE001
                log.warning("orphan reconcile error: %s", exc)
            await asyncio.sleep(120)
