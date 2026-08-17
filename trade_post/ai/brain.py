"""AI decision layer. Combines deterministic signal with Ollama, validates, persists."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ..core.config import Settings
from ..core.errors import AIProviderError, AIResponseInvalid
from ..domain.models import (
    AIDecision,
    OrderIntent,
    OrderSide,
    OrderType,
    SignalSide,
    StrategySignal,
    TimeInForce,
    idempotency_key_for,
)
from .ollama import OllamaClient, build_user_prompt
from .prompts import PROMPT_VERSION
from .schemas import SCHEMA_VERSION, validate_decision

log = logging.getLogger(__name__)


class AIBrain:
    def __init__(self, settings: Settings, client: OllamaClient) -> None:
        self._settings = settings
        self._client = client

    async def decide(
        self,
        *,
        signal: StrategySignal,
        last_price: Decimal,
        spread_bps,
        equity: Decimal,
        drawdown_pct: Decimal,
        available_margin: Decimal,
        recent_evaluations: list,
        trace_id: str | None = None,
        strategy_id: str = "ai-default",
    ) -> AIDecision:
        baseline_payload = _baseline_from_signal(signal, last_price, equity)
        prompt = build_user_prompt(
            symbol=signal.symbol,
            last_price=float(last_price),
            indicators=signal.features,
            spread_bps=float(spread_bps) if spread_bps is not None else None,
            equity=float(equity),
            drawdown_pct=float(drawdown_pct),
            available_margin=float(available_margin),
            max_position_pct=self._settings.max_position_pct,
            recent_evaluations=recent_evaluations,
        )
        raw: dict = dict(baseline_payload)
        ai_used = False
        if not self._client._breaker.is_open:
            try:
                parsed = await self._client.chat_json(prompt)
                if isinstance(parsed, dict):
                    for k in (
                        "action",
                        "price",
                        "quantity",
                        "confidence",
                        "stop_loss",
                        "take_profit",
                        "rationale",
                        "symbol",
                    ):
                        if k in parsed and parsed[k] is not None:
                            raw[k] = parsed[k]
                    ai_used = True
            except (AIProviderError, AIResponseInvalid) as exc:
                log.warning("AI unavailable, using deterministic fallback: %s", exc)

        # PRD M4: the raw output must strictly conform to the versioned schema.
        validated, validation_errors = validate_decision(raw)
        action = str(raw.get("action", "HOLD")).upper()
        decision_signal = (
            SignalSide.LONG if action == "BUY" else SignalSide.SHORT if action == "SELL" else SignalSide.FLAT
        )
        # Invalid outputs are rejected and must never place a live order.
        if not validated:
            decision_signal = SignalSide.FLAT
        try:
            confidence_raw = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence_raw = 0.0
        confidence = Decimal(str(min(1.0, max(0.0, confidence_raw))))
        decision = AIDecision(
            id=uuid.uuid4().hex,
            symbol=str(raw.get("symbol") or signal.symbol),
            signal=decision_signal,
            conviction=max(1, min(10, int(round(float(confidence) * 10)))),
            confidence=confidence,
            rationale=str(raw.get("rationale") or signal.rationale)[:240],
            raw_output=raw,
            model=self._settings.ollama_model if ai_used else "deterministic-fallback",
            prompt_version=PROMPT_VERSION,
            schema_version=SCHEMA_VERSION,
            validated=validated,
            validation_errors=validation_errors,
            timestamp=datetime.now(timezone.utc),
            trace_id=trace_id,
        )
        if not validated:
            log.error(
                "AI output rejected by schema %s: %s",
                SCHEMA_VERSION,
                "; ".join(validation_errors) or "unknown",
            )
        return decision

    def to_intent(
        self,
        decision: AIDecision,
        *,
        last_price: Decimal,
        quantity: Decimal,
    ) -> OrderIntent:
        side = (
            OrderSide.BUY
            if decision.signal is SignalSide.LONG
            else (OrderSide.SELL if decision.signal is SignalSide.SHORT else OrderSide.SELL)
        )
        order_type = OrderType.MARKET
        amount = max(Decimal("0"), Decimal(str(quantity)))
        # The PRD schema's stop_loss/take_profit are absolute price levels, not
        # percents; mapping them into OrderIntent.stop_loss_pct (percent domain)
        # would be unsafe, so a price-level stop is not inferred here.
        return OrderIntent(
            symbol=decision.symbol,
            side=side,
            type=order_type,
            quantity=amount,
            stop_loss_pct=None,
            time_in_force=TimeInForce.GTC,
            idempotency_key=idempotency_key_for(decision.symbol, side, order_type, amount, decision.id[:8]),
            strategy_id=decision.id,
            signal=decision.signal,
            conviction=decision.conviction,
            rationale=decision.rationale,
        )


def _baseline_from_signal(signal: StrategySignal, last_price: Decimal, equity: Decimal) -> dict:
    """Produce the deterministic fallback decision payload (PRD schema v1)."""
    if signal.signal is SignalSide.FLAT or signal.strength < Decimal("0.15"):
        return {
            "action": "HOLD",
            "symbol": signal.symbol,
            "price": None,
            "quantity": None,
            "confidence": 0.0,
            "stop_loss": None,
            "take_profit": None,
            "rationale": "No edge detected",
        }
    notional = float(equity) * 0.005
    quantity = notional / float(last_price) if float(last_price) > 0 else 0.0
    return {
        "action": "BUY" if signal.signal is SignalSide.LONG else "SELL",
        "symbol": signal.symbol,
        "price": round(float(last_price), 8),
        "quantity": round(quantity, 8),
        "confidence": float(signal.strength),
        "stop_loss": None,
        "take_profit": None,
        "rationale": signal.rationale,
    }
