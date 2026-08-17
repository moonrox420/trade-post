"""Pure-Python technical indicators. NumPy/Pandas are not required."""

from __future__ import annotations

import math
from decimal import Decimal

Number = float | int | Decimal


def _to_floats(values):
    return [float(v) for v in values]


def sma(values, period):
    if period <= 0 or len(values) < period:
        return None
    series = _to_floats(values[-period:])
    return sum(series) / period


def ema(values, period):
    if period <= 0 or len(values) < period:
        return None
    series = _to_floats(values)
    k = 2.0 / (period + 1)
    e = series[0]
    for v in series[1:]:
        e = v * k + e * (1 - k)
    return e


def rsi(values, period=14):
    """Wilder RSI in [0, 100]."""
    if period <= 0 or len(values) <= period:
        return None
    series = _to_floats(values)
    gains, losses = [], []
    for i in range(1, len(series)):
        diff = series[i] - series[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values, fast=12, slow=26, signal=9):
    if len(values) < slow + signal:
        return None
    series = _to_floats(values)
    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    fast_e = series[0]
    slow_e = series[0]
    fast_e_list, slow_e_list = [], []
    for v in series[1:]:
        fast_e = v * k_fast + fast_e * (1 - k_fast)
        slow_e = v * k_slow + slow_e * (1 - k_slow)
        fast_e_list.append(fast_e)
        slow_e_list.append(slow_e)
    macd_line = [f - s for f, s in zip(fast_e_list, slow_e_list, strict=True)]
    k_sig = 2.0 / (signal + 1)
    sig = macd_line[0]
    for m in macd_line[1:]:
        sig = m * k_sig + sig * (1 - k_sig)
    return {"macd": macd_line[-1], "signal": sig, "histogram": macd_line[-1] - sig}


def atr(highs, lows, closes, period=14):
    if period <= 0 or len(highs) < period + 1:
        return None
    h = _to_floats(highs)
    low_prices = _to_floats(lows)
    c = _to_floats(closes)
    trs = []
    for i in range(1, len(c)):
        tr = max(h[i] - low_prices[i], abs(h[i] - c[i - 1]), abs(low_prices[i] - c[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    prev = sum(trs[:period]) / period
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
    return prev


def bollinger(values, period=20, num_std=2.0):
    if period <= 1 or len(values) < period:
        return None
    series = _to_floats(values[-period:])
    mean = sum(series) / period
    var = sum((x - mean) ** 2 for x in series) / period
    std = math.sqrt(var)
    return {"middle": mean, "upper": mean + num_std * std, "lower": mean - num_std * std}


def vwap(highs, lows, closes, volumes):
    if not highs or len(highs) != len(lows) or len(highs) != len(closes) or len(highs) != len(volumes):
        return None
    if sum(float(v) for v in volumes) == 0:
        return None
    typical = [
        (float(high) + float(low) + float(close)) / 3.0
        for high, low, close in zip(highs, lows, closes, strict=True)
    ]
    pv = sum(t * float(v) for t, v in zip(typical, volumes, strict=True))
    v = sum(float(x) for x in volumes)
    return pv / v


def realized_volatility(returns, annualization=365.0 * 24.0):
    series = _to_floats(returns)
    n = len(series)
    if n < 2:
        return None
    mean = sum(series) / n
    var = sum((x - mean) ** 2 for x in series) / (n - 1)
    return math.sqrt(var) * math.sqrt(annualization)


def compute_all(closes, highs=None, lows=None, volumes=None, rsi_period=14, atr_period=14, bb_period=20):
    out = {
        "rsi": rsi(closes, rsi_period),
        "macd": macd(closes),
        "ema_fast": ema(closes, 12),
        "ema_slow": ema(closes, 26),
        "sma": sma(closes, 20),
        "bollinger": bollinger(closes, bb_period),
    }
    if highs is not None and lows is not None and len(highs) == len(lows) == len(closes):
        out["atr"] = atr(highs, lows, closes, atr_period)
        if volumes is not None and len(volumes) == len(closes):
            out["vwap"] = vwap(highs, lows, closes, volumes)
    if len(closes) >= 2:
        rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
        out["volatility"] = realized_volatility(rets)
    else:
        out["volatility"] = None
    return out
