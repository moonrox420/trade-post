"""AI decision layer. Combines deterministic signal with Ollama, validates, persists."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

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
        validation_errors: list = []
        if not self._client._breaker.is_open:
            try:
                parsed = await self._client.chat_json(prompt)
                if isinstance(parsed, dict):
                    for k in ("conviction", "rationale", "trailing_stop_pct", "confidence"):
                        if k in parsed and parsed[k] is not None:
                            raw[k] = parsed[k]
                    ai_used = True
            except (AIProviderError, AIResponseInvalid) as exc:
                log.warning("AI unavailable, using deterministic fallback: %s", exc)
        decision_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc)
        try:
            decision = AIDecision(
                id=decision_id,
                symbol=signal.symbol,
                signal=SignalSide(raw.get("signal", "FLAT")),
                conviction=max(1, min(10, int(raw.get("conviction", 5)))),
                confidence=Decimal(str(min(1.0, max(0.0, float(raw.get("confidence", 0.5)))))),
                rationale=str(raw.get("rationale", signal.rationale))[:240],
                raw_output=raw,
                model=self._settings.ollama_model if ai_used else "deterministic-fallback",
                prompt_version=PROMPT_VERSION,
                validated=True,
                validation_errors=validation_errors,
                timestamp=timestamp,
                trace_id=trace_id,
            )
        except ValidationError as exc:
            errs = exc.errors()
            first_msg = errs[0]["msg"] if errs else "validation"
            log.error("AI decision failed schema validation: %s", first_msg)
            decision = AIDecision(
                id=decision_id,
                symbol=signal.symbol,
                signal=SignalSide.FLAT,
                conviction=1,
                confidence=Decimal("0"),
                rationale=f"Validation failure: {first_msg}",
                raw_output=raw,
                model="validation-failure",
                prompt_version=PROMPT_VERSION,
                validated=False,
                validation_errors=[str(e) for e in errs],
                timestamp=timestamp,
                trace_id=trace_id,
            )
        return decision


    def to_intent(
        self,
        decision: AIDecision,
        *,
        last_price: Decimal,
        quantity: Decimal,
    ) -> OrderIntent:
        side = OrderSide.BUY if decision.signal is SignalSide.LONG else (
            OrderSide.SELL if decision.signal is SignalSide.SHORT else OrderSide.SELL
        )
        order_type = OrderType.MARKET
        amount = max(Decimal("0"), Decimal(str(quantity)))
        tsl = decision.raw_output.get("trailing_stop_pct")
        trailing_stop_pct = Decimal(str(tsl)) if tsl is not None else None
        return OrderIntent(
            symbol=decision.symbol,
            side=side,
            type=order_type,
            quantity=amount,
            stop_loss_pct=trailing_stop_pct,
            time_in_force=TimeInForce.GTC,
            idempotency_key=idempotency_key_for(
                decision.symbol, side, order_type, amount, decision.id[:8]
            ),
            strategy_id=decision.id,
            signal=decision.signal,
            conviction=decision.conviction,
            rationale=decision.rationale,
        )


def _baseline_from_signal(signal: StrategySignal, last_price: Decimal, equity: Decimal) -> dict:
    if signal.signal is SignalSide.FLAT or signal.strength < Decimal("0.15"):
        return {
            "symbol": signal.symbol,
            "signal": "FLAT",
            "amount": 0.0,
            "order_type": "market",
            "conviction": 1,
            "rationale": "No edge detected",
            "trailing_stop_pct": None,
            "confidence": 0.0,
        }
    notional = float(equity) * 0.005
    amount = notional / float(last_price) if float(last_price) > 0 else 0.0
    return {
        "symbol": signal.symbol,
        "signal": signal.signal.value,
        "amount": round(amount, 8),
        "order_type": "market",
        "conviction": int(min(10, max(1, round(float(signal.strength) * 10)))),
        "rationale": signal.rationale,
        "trailing_stop_pct": 1.5,
        "confidence": float(signal.strength),
    }
