"""Transparent, read-only Market Intelligence scoring for Nobitex.

The score is diagnostic only. It is never used to place, size, or approve
orders. Execution thresholds and gates remain unchanged in the trading path.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _clip(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _positive_scale(value: float, neutral: float, strong: float) -> float:
    if value <= neutral:
        return 0.0
    if strong <= neutral:
        return 100.0
    return _clip((value - neutral) / (strong - neutral) * 100.0)


def calculate_early_mover_score(
    row: Dict[str, Any],
    scan_change_pct: float = 0.0,
) -> Dict[str, Any]:
    """Calculate a pre-pump score from cheap data already in the market row.

    This intentionally does not require the 3% pump threshold. A market can
    therefore become an early/pre-pump candidate while remaining below the
    existing execution threshold.
    """
    move = max(0.0, _num(row.get("ObservedLocalMove (%)"), scan_change_pct) or 0.0)
    one_hour = _num(row.get("1h Change (%)"), 0.0) or 0.0
    day_change = _num(row.get("24h Change (%)"), 0.0) or 0.0
    volume = max(0.0, _num(row.get("Volume"), 0.0) or 0.0)

    bid = _num(row.get("Bid"), 0.0) or 0.0
    ask = _num(row.get("Ask"), 0.0) or 0.0
    spread = ((ask - bid) / bid * 100.0) if bid > 0 and ask >= bid else 99.0

    price = _num(row.get("Price"), 0.0) or 0.0
    high = _num(row.get("Day High"), 0.0) or 0.0
    low = _num(row.get("Day Low"), 0.0) or 0.0
    day_position = (
        (price - low) / (high - low) * 100.0
        if high > low > 0 and price > 0 else 50.0
    )

    # Explicit weights sum to 100.
    momentum = _positive_scale(move, 0.15, 3.0) * 0.35
    hourly = _positive_scale(one_hour, 0.25, 3.0) * 0.20
    range_position = _clip((day_position - 35.0) / 65.0 * 100.0) * 0.15
    liquidity = _clip(
        math.log10(max(volume, 1.0) / 1_000_000.0 + 1.0) / 4.0 * 100.0
    ) * 0.10
    spread_score = _clip((1.5 - spread) / 1.5 * 100.0) * 0.10
    daily = _positive_scale(day_change, 0.0, 5.0) * 0.10

    score = _clip(momentum + hourly + range_position + liquidity + spread_score + daily)

    if score >= 75:
        state = "Strong Early"
    elif score >= 60:
        state = "Early Mover"
    elif score >= 45:
        state = "Pre-Pump"
    else:
        state = "Normal"

    reasons = []
    if move >= 0.15:
        reasons.append(f"local {move:+.2f}%")
    if one_hour >= 0.5:
        reasons.append(f"1h +{one_hour:.2f}%")
    if day_position >= 70:
        reasons.append(f"day-high proximity {day_position:.0f}%")
    if spread <= 0.6:
        reasons.append(f"spread {spread:.2f}%")
    if volume >= 100_000_000:
        reasons.append("liquid")
    if day_change < 0:
        reasons.append(f"24h {day_change:+.2f}%")
    if not reasons:
        reasons.append("no strong early evidence")

    return {
        "score": round(score, 1),
        "state": state,
        "reasons": reasons,
        "is_candidate": score >= 45.0,
        "move_pct": round(move, 4),
        "spread_pct": round(spread, 4),
        "day_position_pct": round(day_position, 2),
    }


def _component_momentum(snapshot: Dict[str, Any]) -> Optional[float]:
    values = []
    for key, weight in (
        ("momentum_1m_pct", 0.25),
        ("momentum_5m_pct", 0.35),
        ("momentum_15m_pct", 0.40),
    ):
        value = _num(snapshot.get(key))
        if value is not None:
            values.append((_clip(50.0 + value * 12.5), weight))
    if not values:
        return None
    return sum(score * weight for score, weight in values) / sum(w for _, w in values)


def _component_pressure(snapshot: Dict[str, Any]) -> Optional[float]:
    return _num(snapshot.get("buy_pressure_pct"))


def _component_imbalance(snapshot: Dict[str, Any]) -> Optional[float]:
    value = _num(snapshot.get("orderbook_imbalance_pct"))
    return None if value is None else _clip(50.0 + value * 0.5)


def _component_volume(snapshot: Dict[str, Any]) -> Optional[float]:
    value = _num(snapshot.get("volume_ratio"))
    if value is None:
        return None
    return _clip((value - 0.5) / 2.5 * 100.0)


def _component_ema(snapshot: Dict[str, Any]) -> Optional[float]:
    trend = str(snapshot.get("ema_trend", "") or "").lower()
    if "bull" in trend or "up" in trend:
        return 100.0
    if "bear" in trend or "down" in trend:
        return 0.0
    return 50.0 if trend else None


def _component_macd(snapshot: Dict[str, Any]) -> Optional[float]:
    hist = _num(snapshot.get("macd_hist"))
    if hist is None:
        return None
    macd = abs(_num(snapshot.get("macd"), 0.0) or 0.0)
    signal = abs(_num(snapshot.get("macd_signal"), 0.0) or 0.0)
    scale = macd + signal
    if scale <= 0:
        return 50.0
    return _clip(50.0 + hist / scale * 100.0)


def _component_rsi(snapshot: Dict[str, Any]) -> Optional[float]:
    rsi = _num(snapshot.get("rsi"))
    if rsi is None:
        return None
    if 50 <= rsi <= 65:
        return 100.0
    if 65 < rsi <= 75:
        return 100.0 - (rsi - 65.0) * 6.0
    if 40 <= rsi < 50:
        return (rsi - 40.0) * 5.0
    return 20.0 if rsi < 40 else 20.0


_COMPONENTS = (
    ("Momentum", 25.0, _component_momentum),
    ("Buy Pressure", 20.0, _component_pressure),
    ("Order Book", 15.0, _component_imbalance),
    ("Volume", 10.0, _component_volume),
    ("EMA", 10.0, _component_ema),
    ("MACD", 8.0, _component_macd),
    ("RSI", 7.0, _component_rsi),
)


def calculate_signal_intelligence_score(
    row: Optional[Dict[str, Any]],
    snapshot: Optional[Dict[str, Any]],
    *,
    early: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a transparent 0-100 score using available deep evidence.

    The early score is included at 20% when supplied. Missing deep components
    are excluded from the denominator rather than silently becoming zero.
    """
    row = row or {}
    snapshot = snapshot or {}
    early = early or calculate_early_mover_score(row)

    contributions = []
    factors: Dict[str, float] = {}
    available_weight = 0.0
    weighted_total = 0.0

    early_score = _num(early.get("score"))
    if early_score is not None:
        factors["Early Mover"] = round(early_score, 2)
        contributions.append(("Early Mover", early_score, 20.0))
        available_weight += 20.0
        weighted_total += early_score * 20.0

    for name, weight, calculator in _COMPONENTS:
        try:
            component = calculator(snapshot)
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
            "score": None, "coverage_pct": 0.0, "grade": "Insufficient data",
            "factors": {}, "reasons": ["no intelligence data"],
        }

    score = weighted_total / available_weight
    coverage = available_weight / 100.0 * 100.0
    if coverage < 45.0:
        grade = "Insufficient data"
    elif score >= 75.0:
        grade = "Strong"
    elif score >= 60.0:
        grade = "Constructive"
    elif score >= 45.0:
        grade = "Mixed"
    else:
        grade = "Weak"

    reasons = []
    buy = _num(snapshot.get("buy_pressure_pct"))
    imbalance = _num(snapshot.get("orderbook_imbalance_pct"))
    m5 = _num(snapshot.get("momentum_5m_pct"))
    m15 = _num(snapshot.get("momentum_15m_pct"))
    vr = _num(snapshot.get("volume_ratio"))

    if buy is not None:
        reasons.append(f"buy pressure {buy:.0f}%")
    if imbalance is not None and abs(imbalance) >= 5:
        reasons.append(f"OB imbalance {imbalance:+.1f}%")
    if m5 is not None and abs(m5) >= 0.25:
        reasons.append(f"5m {m5:+.2f}%")
    if m15 is not None and abs(m15) >= 0.5:
        reasons.append(f"15m {m15:+.2f}%")
    if vr is not None and vr >= 1.5:
        reasons.append(f"volume {vr:.1f}x")
    reasons.extend(list(early.get("reasons") or [])[:2])

    return {
        "score": round(score, 1),
        "coverage_pct": round(coverage, 1),
        "grade": grade,
        "factors": factors,
        "reasons": reasons[:7] or ["mixed deep evidence"],
        "available_weight": round(available_weight, 1),
    }


__all__ = ["calculate_early_mover_score", "calculate_signal_intelligence_score"]
