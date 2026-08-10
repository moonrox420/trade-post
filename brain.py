import asyncio
import logging
from datetime import datetime
from typing import List, Optional
from langchain_google_vertexai import ChatVertexAI
from google.cloud.firestore import AsyncClient

from models import (
    StrategyProposal,
    MarketSnapshot,
    PortfolioSnapshot,
    SignalSide,
    SymbolPrioritization,
    QualitativeEvaluation,
    PerformanceReport,
)
from config import POSITION_SIZE_HARD_CAP

logger = logging.getLogger(__name__)


class MultiAgentStrategyBrain:
    def __init__(
        self, project_id: str, market_service, portfolio, events, exec_engine=None
    ):
        self.base_llm = ChatVertexAI(
            model_name="xai/grok-4.20-reasoning",
            project=project_id,
            temperature=0.1,
            max_output_tokens=2048,
        )
        self.llm = self.base_llm.with_structured_output(StrategyProposal)
        self.priority_llm = self.base_llm.with_structured_output(SymbolPrioritization)
        self.market = market_service
        self.portfolio = portfolio
        self.events = events
        self.exec_engine = exec_engine

    async def autonomous_loop(self):
        logger.info("Autonomous Brain Loop initialized.")
        while True:
            try:
                if not self.exec_engine:
                    await asyncio.sleep(10)
                    continue
                # The main app state check is handled in drox_trade_post.py
                await asyncio.sleep(60)
            except Exception as loop_err:
                logger.error(f"Critical error in Autonomous Loop: {loop_err}")
                await asyncio.sleep(60)

    async def generate_proposal(
        self, symbol: str, market_snapshot=None, portfolio_snapshot=None
    ) -> Optional[StrategyProposal]:
        if self.events.is_circuit_broken():
            logger.warning("Strategy Brain paused: Circuit breaker active.")
            return None
        try:
            snapshot = market_snapshot or await self.market.get_snapshot(symbol)
            portfolio_snap = portfolio_snapshot or await self.portfolio.refresh_state()
            hist_context = ""
            try:
                evals_ref = self.events.db.collection("strategy_evaluations")
                evals_query = (
                    evals_ref.where("symbol", "==", symbol)
                    .order_by("timestamp", direction="DESCENDING")
                    .limit(5)
                )
                eval_docs = await evals_query.get()
                if eval_docs:
                    hist_context = "\nRecent historical evaluations:\n"
                    for doc in eval_docs:
                        d = doc.to_dict()
                        hist_context += f"- Rationale: {d.get('rationale')} | Score: {d.get('qualitative_score')}/10\n"
            except Exception as db_err:
                logger.warning(f"Could not fetch history: {db_err}")
            try:
                prompt = f"""You are a professional trading team.
Symbol: {symbol} | Price: {snapshot.last_price} | Balance: {portfolio_snap.total_equity} USDT
Indicators: RSI: {snapshot.indicators["rsi"]:.2f}, Volatility: {snapshot.indicators["volatility"]:.4f}
{hist_context}
Propose ONE StrategyProposal."""
                proposal = await self.llm.ainvoke(prompt)
                proposal.market_snapshot_id = str(snapshot.timestamp.timestamp())
            except Exception as llm_err:
                logger.warning(f"LLM Provider Unavailable: {llm_err}.")
                rsi = snapshot.indicators.get("rsi", 50)
                side = SignalSide.FLAT
                if rsi < 30:
                    side = SignalSide.LONG
                elif rsi > 70:
                    side = SignalSide.SHORT
                proposal = StrategyProposal(
                    symbol=snapshot.symbol,
                    signal=side,
                    amount=POSITION_SIZE_HARD_CAP * 0.01 / snapshot.last_price,
                    order_type="market",
                    conviction=5,
                    rationale=f"Graceful Degradation: RSI is {rsi:.2f}",
                    trailing_stop_pct=2.0,
                    market_snapshot_id=str(snapshot.timestamp.timestamp()),
                )
            await self.events.emit(
                "strategy_proposal_generated",
                {
                    "proposal": proposal.model_dump(),
                    "snapshot": snapshot.model_dump(),
                    "portfolio": portfolio_snap.model_dump(),
                },
            )
            return proposal
        except Exception as e:
            await self.events.emit(
                "strategy_error", {"symbol": symbol, "error": str(e)}, severity="ERROR"
            )
        return None

    async def generate_rebalance_proposals(
        self, target_weights: dict[str, float]
    ) -> List[StrategyProposal]:
        proposals = []
        try:
            portfolio_snap = await self.portfolio.refresh_state()
            total_equity = portfolio_snap.total_equity
            if total_equity <= 0:
                return []
            for symbol, weight in target_weights.items():
                snapshot = await self.market.get_snapshot(symbol)
                current_price = snapshot.last_price
                base_currency = symbol.split("/")[0]
                current_qty = portfolio_snap.base_balances.get(base_currency, 0.0)
                current_notional = current_qty * current_price
                target_notional = total_equity * weight
                delta_notional = target_notional - current_notional
                if abs(delta_notional) < max(10, target_notional * 0.01):
                    continue
                proposals.append(
                    StrategyProposal(
                        symbol=symbol,
                        signal=SignalSide.LONG
                        if delta_notional > 0
                        else SignalSide.SHORT,
                        amount=abs(delta_notional) / current_price,
                        price=current_price,
                        order_type="market",
                        conviction=10,
                        rationale=f"Dynamic rebalance to {weight * 100}% target weight.",
                    )
                )
        except Exception as e:
            logger.error(f"Error generating rebalance proposals: {e}")
        return proposals


class ReplayService:
    def __init__(self, db: AsyncClient, brain: MultiAgentStrategyBrain, risk):
        self.db = db
        self.brain = brain
        self.risk = risk

    async def simulate_decision_process(self, session_id: str):
        logger.info(f"Starting Replay Simulation for Session: {session_id}")
        events_ref = self.db.collection("event_store")
        query = events_ref.where("session_id", "==", session_id).order_by("timestamp")
        docs = await query.get()
        historical_outcomes = {}
        for doc in docs:
            ev = doc.to_dict()
            if ev["event_type"] == "trade_executed":
                historical_outcomes[ev["data"]["proposal_id"]] = "executed"
            elif ev["event_type"] == "trade_rejected":
                historical_outcomes[ev["data"]["proposal_id"]] = "rejected"
        comparisons = []
        summary = {
            "total_events": len(docs),
            "proposals_evaluated": 0,
            "risk_passed": 0,
            "risk_failed": 0,
            "discrepancies": 0,
        }
        for doc in docs:
            event = doc.to_dict()
            if event["event_type"] == "strategy_proposal_generated":
                summary["proposals_evaluated"] += 1
                data = event["data"]
                hist_proposal = StrategyProposal.model_validate(data["proposal"])
                hist_snapshot = MarketSnapshot.model_validate(data["snapshot"])
                hist_portfolio = PortfolioSnapshot.model_validate(data["portfolio"])
                risk_passed, _ = await self.risk.validate_proposal(
                    hist_proposal, hist_snapshot, portfolio_state=hist_portfolio
                )
                sim_outcome = "passed" if risk_passed else "failed"
                hist_outcome = historical_outcomes.get(
                    hist_proposal.proposal_id, "lost"
                )
                hist_logic_outcome = (
                    "passed"
                    if hist_outcome == "executed"
                    else "failed"
                    if hist_outcome == "rejected"
                    else "unknown"
                )
                has_discrepancy = sim_outcome != hist_logic_outcome
                if risk_passed:
                    summary["risk_passed"] += 1
                else:
                    summary["risk_failed"] += 1
                if has_discrepancy:
                    summary["discrepancies"] += 1
                comparisons.append(
                    {
                        "proposal_id": hist_proposal.proposal_id,
                        "timestamp": event["timestamp"],
                        "symbol": hist_proposal.symbol,
                        "simulated_risk_outcome": sim_outcome,
                        "historical_outcome": hist_outcome,
                        "discrepancy": has_discrepancy,
                    }
                )
        return {
            "session_id": session_id,
            "report_timestamp": datetime.utcnow().isoformat(),
            "summary": summary,
            "details": comparisons,
        }


class StrategyEvaluator:
    def __init__(self, db: AsyncClient, market, events, project_id: str, risk):
        self.db = db
        self.market = market
        self.events = events
        self.risk = risk
        base_llm = ChatVertexAI(
            model_name="xai/grok-4.20-reasoning", project=project_id, temperature=0.1
        )
        self.eval_llm = base_llm.with_structured_output(QualitativeEvaluation)
        self.report_llm = base_llm.with_structured_output(PerformanceReport)

    async def evaluation_loop(
        self, interval: int = 300, evaluation_window_sec: int = 900
    ):
        while True:
            try:
                cutoff_ts = datetime.utcnow().timestamp() - evaluation_window_sec
                cutoff_iso = datetime.fromtimestamp(cutoff_ts).isoformat()
                events_ref = self.db.collection("event_store")
                query = (
                    events_ref.where("event_type", "==", "strategy_proposal_generated")
                    .where("timestamp", "<=", cutoff_iso)
                    .order_by("timestamp", direction="DESCENDING")
                    .limit(20)
                )
                docs = await query.get()
                for doc in docs:
                    event = doc.to_dict()
                    data = event["data"]
                    proposal = data["proposal"]
                    pid = proposal["proposal_id"]
                    eval_ref = self.db.collection("strategy_evaluations").document(pid)
                    if (await eval_ref.get()).exists:
                        continue
                    symbol = proposal["symbol"]
                    side = proposal["side"]
                    entry_price = data["snapshot"]["last_price"]
                    current_snap = await self.market.get_snapshot(symbol)
                    exit_price = current_snap.last_price
                    perf_pct = (exit_price - entry_price) / entry_price
                    if side == "SHORT":
                        perf_pct = -perf_pct
                    score = round(perf_pct * 10000, 2)
                    proposal["amount"] * entry_price
                    mu = data["portfolio"].get("margin_utilization", 0)
                    self.risk.calculate_dynamic_leverage(mu)
                    prompt = f"Evaluate rationale: {proposal['rationale']} | Performance: {score} bps"
                    qual_eval = await self.eval_llm.ainvoke(prompt)
                    eval_doc = {
                        "proposal_id": pid,
                        "symbol": symbol,
                        "performance_bps": score,
                        "qualitative_score": qual_eval.score,
                        "critique": qual_eval.critique,
                        "rationale": proposal["rationale"],
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                    await eval_ref.set(eval_doc)
                    await self.events.emit("strategy_evaluation", eval_doc)
            except Exception as e:
                logger.error(f"Strategy Evaluator error: {e}")
            await asyncio.sleep(interval)

    async def report_loop(self, interval: int = 604800):
        while True:
            try:
                import time

                lookback_ts = time.time() - (7 * 86400)
                lookback_iso = datetime.fromtimestamp(lookback_ts).isoformat()
                evals_ref = self.db.collection("strategy_evaluations")
                query = evals_ref.where("timestamp", ">=", lookback_iso).order_by(
                    "timestamp"
                )
                docs = await query.get()
                if docs:
                    evals = [d.to_dict() for d in docs]
                    total_bps = sum(e.get("performance_bps", 0) for e in evals)
                    avg_score = sum(e.get("qualitative_score", 0) for e in evals) / len(
                        evals
                    )
                    import json

                    prompt = f"Analyze week: {total_bps} BPS, {avg_score} score. Data: {json.dumps(evals)}"
                    report = await self.report_llm.ainvoke(prompt)
                    await self.db.collection("performance_reports").add(
                        {
                            "report": report.model_dump(),
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )
                    await self.events.emit(
                        "weekly_report_generated", report.model_dump()
                    )
            except Exception as e:
                logger.error(f"Weekly report generation failed: {e}")
            await asyncio.sleep(interval)
