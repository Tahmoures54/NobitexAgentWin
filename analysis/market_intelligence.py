"""Read-only scoring utilities for Nobitex market intelligence.

This module is deliberately independent from the trading/execution path.
It converts the already-fetched market-intelligence snapshot into a
transparent 0-100 diagnostic score. The score is informational only.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        result = float(value)
        if result != result:
            return None
        return result
    except (TypeError, ValueError):
        return None


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _scale(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return _clip((value - low) / (high - low) * 100.0)


def _parse_pump_pct(row: Optional[Dict[str, Any]]) -> Optional[float]:
    if not row:
        return None
    direct = _num(row.get("pump_pct"))
    if direct is not None:
        return direct
    signal = str(row.get("Signal", "") or "")
    match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*%?\s*Pump", signal, re.I)
    return float(match.group(1)) if match else None


def _component_pump(row: Dict[str, Any]) -> Optional[float]:
    pump = _parse_pump_pct(row)
    if pump is None:
        return None
    # 3% is the existing candidate threshold.  10%+ receives full weight.
    return _scale(pump, 3.0, 10.0)


def _component_momentum(snapshot: Dict[str, Any]) -> Optional[float]:
    values = [_num(snapshot.get(k)) for k in (
        "momentum_1m_pct", "momentum_5m_pct", "momentum_15m_pct"
    )]
    values = [v for v in values if v is not None]
    if not values:
        return None
    # Shorter timeframes receive slightly more weight.
    weighted = []
    for key, weight in (
        ("momentum_1m_pct", 0.25),
        ("momentum_5m_pct", 0.35),
        ("momentum_15m_pct", 0.40),
    ):
        value = _num(snapshot.get(key))
        if value is not None:
            weighted.append((_clip(50.0 + value * 12.5), weight))
    if not weighted:
        return None
    return sum(score * weight for score, weight in weighted) / sum(w for _, w in weighted)


def _component_pressure(snapshot: Dict[str, Any]) -> Optional[float]:
    value = _num(snapshot.get("buy_pressure_pct"))
    if value is None:
        return None
    return _clip(value)


def _component_imbalance(snapshot: Dict[str, Any]) -> Optional[float]:
    value = _num(snapshot.get("orderbook_imbalance_pct"))
    if value is None:
        return None
    return _clip(50.0 + value * 0.5)


def _component_volume(snapshot: Dict[str, Any]) -> Optional[float]:
    value = _num(snapshot.get("volume_ratio"))
    if value is None:
        return None
    # 1x = neutral, 2x = strong confirmation, 3x+ = full score.
    return _scale(value, 0.5, 3.0)


def _component_ema(snapshot: Dict[str, Any]) -> Optional[float]:
    trend = str(snapshot.get("ema_trend", "") or "").lower()
    if trend == "bullish":
        return 100.0
    if trend == "bearish":
        return 0.0
    if trend:
        return 50.0
    return None


def _component_macd(snapshot: Dict[str, Any]) -> Optional[float]:
    hist = _num(snapshot.get("macd_hist"))
    if hist is None:
        return None
    macd = _num(snapshot.get("macd"))
    signal = _num(snapshot.get("macd_signal"))
    magnitude = abs(macd or 0.0) + abs(signal or 0.0)
    if magnitude <= 0:
        return 50.0
    # Relative histogram direction is more useful than absolute price scale.
    return _clip(50.0 + (hist / magnitude) * 100.0)


def _component_rsi(snapshot: Dict[str, Any]) -> Optional[float]:
    rsi = _num(snapshot.get("rsi"))
    if rsi is None:
        return None
    # Favor bullish momentum without rewarding extreme overbought readings.
    if 50.0 <= rsi <= 65.0:
        return 100.0
    if 65.0 < rsi <= 75.0:
        return _scale(75.0 - rsi, 0.0, 10.0)
    if 40.0 <= rsi < 50.0:
        return _scale(rsi, 40.0, 50.0)
    if rsi < 40.0:
        return _scale(rsi, 20.0, 40.0)
    return 20.0


def _component_adx(snapshot: Dict[str, Any]) -> Optional[float]:
    adx = _num(snapshot.get("adx"))
    if adx is None:
        return None
    return _scale(adx, 15.0, 40.0)


_COMPONENTS = (
    ("Pump", 20.0, _component_pump),
    ("Momentum", 20.0, _component_momentum),
    ("Buy Pressure", 15.0, _component_pressure),
    ("Order Book", 10.0, _component_imbalance),
    ("Volume", 10.0, _component_volume),
    ("EMA", 10.0, _component_ema),
    ("MACD", 5.0, _component_macd),
    ("RSI", 5.0, _component_rsi),
    ("ADX", 5.0, _component_adx),
)


def calculate_signal_intelligence_score(
    row: Optional[Dict[str, Any]],
    snapshot: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Return a transparent, availability-aware 0-100 diagnostic score.

    The score is normalized over available components, so missing candle
    or flow data does not silently become a zero. It never triggers orders.
    """
    row = row or {}
    snapshot = snapshot or {}

    contributions = []
    factors: Dict[str, float] = {}
    available_weight = 0.0
    weighted_total = 0.0

    for name, weight, calculator in _COMPONENTS:
        try:
            component = calculator(row) if name == "Pump" else calculator(snapshot)
        except Exception:
            component = None
        if component is None:
            continue
        component = _clip(component)
        factors[name] = round(component, 2)
        available_weight += weight
        weighted_total += component * weight
        contributions.append((name, component, weight))

    if available_weight <= 0:
        return {
            "score": None,
            "coverage_pct": 0.0,
            "grade": "Insufficient data",
            "factors": {},
            "available_weight": 0.0,
        }

    score = weighted_total / available_weight
    coverage = available_weight / 100.0 * 100.0
    if coverage < 50.0:
        grade = "Insufficient data"
    elif score >= 75.0:
        grade = "Strong alignment"
    elif score >= 60.0:
        grade = "Positive alignment"
    elif score >= 45.0:
        grade = "Mixed alignment"
    else:
        grade = "Weak alignment"

    return {
        "score": round(score, 1),
        "coverage_pct": round(coverage, 1),
        "grade": grade,
        "factors": factors,
        "available_weight": round(available_weight, 1),
    }


__all__ = ["calculate_signal_intelligence_score"]
