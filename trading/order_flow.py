"""Order-book microstructure features for Nobitex spot execution.

This module is deliberately deterministic and exchange-agnostic. It converts
top-of-book levels into interpretable features that can be used as an entry
filter without replacing the existing strategy/risk engine.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Sequence, Tuple


def _level(level: Any) -> Tuple[float, float]:
    if isinstance(level, dict):
        price = level.get("price", level.get("p", level.get("rate", 0.0)))
        amount = level.get("amount", level.get("a", level.get("volume", 0.0)))
    elif isinstance(level, (list, tuple)) and len(level) >= 2:
        price, amount = level[0], level[1]
    else:
        return 0.0, 0.0
    try:
        return max(0.0, float(price)), max(0.0, float(amount))
    except (TypeError, ValueError):
        return 0.0, 0.0


def _notional(levels: Iterable[Any], n: int) -> float:
    total = 0.0
    for level in list(levels)[: max(1, int(n))]:
        price, amount = _level(level)
        total += price * amount
    return total


def analyze_order_book(book: Dict[str, Any], levels: int = 10) -> Dict[str, float | bool]:
    """Return interpretable L2 pressure/liquidity features.

    imbalance is normalized to [-1, 1]:
        +1 = all visible notional is on bids
        -1 = all visible notional is on asks

    microprice_bias_pct is the microprice displacement from the mid price.
    A positive value means visible bid pressure is stronger.
    """
    bids = (book or {}).get("bids") or []
    asks = (book or {}).get("asks") or []
    bid_levels = [_level(x) for x in list(bids)[: max(1, int(levels))]]
    ask_levels = [_level(x) for x in list(asks)[: max(1, int(levels))]]
    bid_levels = [(p, a) for p, a in bid_levels if p > 0 and a > 0]
    ask_levels = [(p, a) for p, a in ask_levels if p > 0 and a > 0]

    best_bid = max((p for p, _ in bid_levels), default=0.0)
    best_ask = min((p for p, _ in ask_levels), default=0.0)
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return {
            "order_flow_valid": False,
            "order_flow_imbalance": 0.0,
            "order_flow_score": 50.0,
            "spread_pct": 0.0,
            "bid_depth_quote": 0.0,
            "ask_depth_quote": 0.0,
            "depth_imbalance": 0.0,
            "microprice_bias_pct": 0.0,
        }

    bid_depth = sum(p * a for p, a in bid_levels)
    ask_depth = sum(p * a for p, a in ask_levels)
    total_depth = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth > 0 else 0.0

    mid = (best_bid + best_ask) / 2.0
    spread_pct = ((best_ask - best_bid) / mid * 100.0) if mid > 0 else 0.0

    # Microprice weights the opposite side by displayed size.
    bid_size = bid_levels[0][1]
    ask_size = ask_levels[0][1]
    size_total = bid_size + ask_size
    microprice = (
        (best_ask * bid_size + best_bid * ask_size) / size_total
        if size_total > 0 else mid
    )
    micro_bias = (microprice / mid - 1.0) * 100.0 if mid > 0 else 0.0

    # 0..100 interpretable pressure score.
    score = max(0.0, min(100.0, 50.0 + 50.0 * imbalance))

    return {
        "order_flow_valid": True,
        "order_flow_imbalance": float(imbalance),
        "order_flow_score": float(score),
        "spread_pct": float(spread_pct),
        "bid_depth_quote": float(bid_depth),
        "ask_depth_quote": float(ask_depth),
        "depth_imbalance": float(imbalance),
        "microprice_bias_pct": float(micro_bias),
    }


def entry_gate(
    features: Dict[str, Any],
    *,
    min_score: float = 58.0,
    max_spread_pct: float = 1.2,
    min_depth_quote: float = 0.0,
) -> Tuple[bool, str]:
    """Apply a conservative BUY-side microstructure gate."""
    if not bool(features.get("order_flow_valid")):
        return False, "invalid_orderbook"
    score = float(features.get("order_flow_score", 50.0) or 0.0)
    spread = float(features.get("spread_pct", 0.0) or 0.0)
    bid_depth = float(features.get("bid_depth_quote", 0.0) or 0.0)
    if spread > max_spread_pct:
        return False, "wide_spread"
    if bid_depth < min_depth_quote:
        return False, "thin_bid_depth"
    if score < min_score:
        return False, "weak_buy_pressure"
    return True, "ok"
