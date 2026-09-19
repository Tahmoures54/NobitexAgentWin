"""
trading/tech_regime.py — Lightweight technical features for strategy/risk.

Computes from OHLCV / price series without heavy deps beyond math:
- ATR percentage
- EMA slope
- Simple Hurst-like persistence proxy (variance ratio)

Designed to feed StrategySelector and AutoRiskEngine.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence


def _ema(values: Sequence[float], period: int) -> List[float]:
    if not values or period < 1:
        return []
    alpha = 2.0 / (period + 1)
    out: List[float] = []
    e = float(values[0])
    out.append(e)
    for v in values[1:]:
        e = alpha * float(v) + (1 - alpha) * e
        out.append(e)
    return out


def atr_pct(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Optional[float]:
    """Average True Range as percent of last close."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs: List[float] = []
    for i in range(1, n):
        h, l, c_prev = float(highs[i]), float(lows[i]), float(closes[i - 1])
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / period
    last_close = float(closes[-1])
    if last_close <= 0:
        return None
    return (atr / last_close) * 100.0


def ema_slope_pct(closes: Sequence[float], period: int = 20, lookback: int = 5) -> Optional[float]:
    """Percent change of EMA over `lookback` bars."""
    if len(closes) < period + lookback:
        return None
    series = _ema([float(c) for c in closes], period)
    a, b = series[-1], series[-1 - lookback]
    if b <= 0:
        return None
    return (a - b) / b * 100.0


def hurst_proxy(closes: Sequence[float], max_lag: int = 10) -> Optional[float]:
    """
    Rough persistence proxy via variance ratio.

    Returns value around:
    - ~0.5  random walk
    - >0.55 trending / persistent
    - <0.45 mean-reverting

    Not a full R/S Hurst estimator; cheap and stable for regime hints.
    """
    n = len(closes)
    if n < max_lag * 3:
        return None
    rets = []
    for i in range(1, n):
        if closes[i - 1] > 0:
            rets.append(math.log(float(closes[i]) / float(closes[i - 1])))
    if len(rets) < max_lag * 2:
        return None

    def _var(xs: List[float]) -> float:
        if len(xs) < 2:
            return 0.0
        m = sum(xs) / len(xs)
        return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)

    v1 = _var(rets)
    if v1 <= 1e-18:
        return 0.5

    # lag-k aggregated returns variance / (k * v1)
    ratios = []
    for k in range(2, min(max_lag, len(rets) // 3) + 1):
        agg = [sum(rets[i : i + k]) for i in range(0, len(rets) - k + 1, k)]
        vk = _var(agg)
        if vk > 0:
            ratios.append(vk / (k * v1))
    if not ratios:
        return 0.5
    # H ≈ 0.5 * log2(variance_ratio) + 0.5  (rough)
    mean_r = sum(ratios) / len(ratios)
    if mean_r <= 0:
        return 0.5
    h = 0.5 + 0.5 * (math.log(mean_r) / math.log(2))
    return max(0.0, min(1.0, h))


def summarize_series(
    closes: Sequence[float],
    highs: Optional[Sequence[float]] = None,
    lows: Optional[Sequence[float]] = None,
) -> dict:
    """Convenience bundle for selector / risk."""
    highs = highs or closes
    lows = lows or closes
    return {
        "atr_pct": atr_pct(highs, lows, closes),
        "ema_slope_pct": ema_slope_pct(closes),
        "hurst_proxy": hurst_proxy(closes),
    }
