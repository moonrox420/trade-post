"""Deterministic signal generation. AI augments, never replaces these."""

from __future__ import annotations

from decimal import Decimal

from ..core.config import Settings
from ..domain.models import MarketSnapshot, SignalSide, StrategySignal


def _f(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strength(value: float, lo: float, hi: float) -> Decimal:
    """Map [lo,hi] -> [0,1] with simple clipping."""
    if hi == lo:
        return Decimal("0.5")
    pct = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    return Decimal(str(round(pct, 4)))


class SignalEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def from_snapshot(self, snap: MarketSnapshot) -> StrategySignal:
        ind = snap.indicators or {}
        rsi = _f(ind.get("rsi")) or 50.0
        macd = ind.get("macd") or {}
        macd_hist = _f(macd.get("histogram")) if isinstance(macd, dict) else None
        ema_fast = _f(ind.get("ema_fast"))
        ema_slow = _f(ind.get("ema_slow"))
        vol = _f(ind.get("volatility")) or 0.0

        # Trend: EMA cross
        trend = 0.0
        if ema_fast is not None and ema_slow is not None and ema_slow != 0:
            trend = (ema_fast - ema_slow) / ema_slow
        # Mean reversion: RSI extremes
        mean_rev = 0.0
        if rsi <= 30:
            mean_rev = (30 - rsi) / 30  # bullish bias when oversold
        elif rsi >= 70:
            mean_rev = -(rsi - 70) / 30  # bearish bias when overbought
        # Momentum: MACD histogram
        momentum = macd_hist if macd_hist is not None else 0.0
        # Score
        score = 0.5 * trend + 0.3 * mean_rev + 0.2 * max(min(momentum / max(1.0, vol), 1.0), -1.0)
        # Volatility-adjusted threshold
        threshold = max(0.02, min(0.2, vol))
        if score > threshold:
            signal = SignalSide.LONG
            strength = _strength(score, threshold, max(0.5, threshold * 4))
        elif score < -threshold:
            signal = SignalSide.SHORT
            strength = _strength(-score, threshold, max(0.5, threshold * 4))
        else:
            signal = SignalSide.FLAT
            strength = Decimal("0")
        rationale = (
            f"trend={trend:.4f} mean_rev={mean_rev:.4f} momentum={momentum:.4f} "
            f"rsi={rsi:.2f} vol={vol:.4f} threshold={threshold:.4f}"
        )
        return StrategySignal(
            symbol=snap.symbol,
            signal=signal,
            strength=strength,
            features={
                "rsi": rsi, "trend": trend, "mean_rev": mean_rev, "momentum": momentum,
                "volatility": vol, "ema_fast": ema_fast or 0, "ema_slow": ema_slow or 0,
                "macd_histogram": macd_hist or 0,
            },
            rationale=rationale,
        )
