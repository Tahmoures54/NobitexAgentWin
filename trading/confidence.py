"""
trading/confidence.py — Signal confidence scoring and confluence filters.

Combines multiple independent confirmations into a 0..100 score.
A candidate must clear min_confidence, hard filters, and an optional
cost gate (expected move vs fee+spread+slippage) before execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceResult:
    score: float                    # 0..100
    passed: bool
    components: Dict[str, float] = field(default_factory=dict)
    failed_filters: List[str] = field(default_factory=list)
    reason: str = ""
    estimated_cost_pct: Optional[float] = None
    edge_ratio: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": self.score,
            "passed": self.passed,
            "components": self.components,
            "failed_filters": self.failed_filters,
            "reason": self.reason,
            "estimated_cost_pct": self.estimated_cost_pct,
            "edge_ratio": self.edge_ratio,
        }


def estimate_roundtrip_cost_pct(
    *,
    fee_pct: float = 0.25,
    spread_pct: Optional[float] = None,
    slippage_pct: float = 0.15,
    safety_margin_pct: float = 0.2,
) -> float:
    """
    Rough round-trip cost in percent of notional.

    fee applied twice (entry+exit), plus effective spread, slippage, margin.
    """
    fee = max(0.0, float(fee_pct)) * 2.0
    spread = max(0.0, float(spread_pct or 0.0))
    slip = max(0.0, float(slippage_pct))
    margin = max(0.0, float(safety_margin_pct))
    return fee + spread + slip + margin


class ConfidenceScorer:
    """Weighted confluence scorer for entry candidates."""

    def __init__(
        self,
        *,
        min_confidence: float = 55.0,
        w_momentum: float = 25.0,
        w_volume: float = 15.0,
        w_spread: float = 15.0,
        w_regime: float = 15.0,
        w_btc_align: float = 10.0,
        w_depth: float = 10.0,
        w_strategy_fit: float = 10.0,
        max_spread_pct: float = 2.0,
        min_volume_irt: float = 30_000_000.0,
        block_in_crisis: bool = True,
        # Cost gate
        enable_cost_gate: bool = True,
        fee_pct: float = 0.25,
        slippage_pct: float = 0.15,
        safety_margin_pct: float = 0.2,
        min_edge_ratio: float = 2.0,
    ) -> None:
        self.min_confidence = float(min_confidence)
        self.weights = {
            "momentum": float(w_momentum),
            "volume": float(w_volume),
            "spread": float(w_spread),
            "regime": float(w_regime),
            "btc_align": float(w_btc_align),
            "depth": float(w_depth),
            "strategy_fit": float(w_strategy_fit),
        }
        self.max_spread_pct = float(max_spread_pct)
        self.min_volume_irt = float(min_volume_irt)
        self.block_in_crisis = bool(block_in_crisis)
        self.enable_cost_gate = bool(enable_cost_gate)
        self.fee_pct = float(fee_pct)
        self.slippage_pct = float(slippage_pct)
        self.safety_margin_pct = float(safety_margin_pct)
        self.min_edge_ratio = float(min_edge_ratio)

    def score(
        self,
        *,
        move_pct: Optional[float] = None,
        volume_irt: Optional[float] = None,
        spread_pct: Optional[float] = None,
        regime: Optional[str] = None,
        btc_change_pct: Optional[float] = None,
        ask_depth_irt: Optional[float] = None,
        strategy_name: Optional[str] = None,
        strategy_min_move: Optional[float] = None,
        fee_pct: Optional[float] = None,
    ) -> ConfidenceResult:
        failed: List[str] = []
        comps: Dict[str, float] = {}
        cost_pct: Optional[float] = None
        edge_ratio: Optional[float] = None

        # ── Hard filters ───────────────────────────────────────
        if spread_pct is not None and spread_pct > self.max_spread_pct:
            failed.append(f"spread>{self.max_spread_pct}")
        if volume_irt is not None and volume_irt < self.min_volume_irt:
            failed.append(f"volume<{self.min_volume_irt:.0f}")
        if self.block_in_crisis and (regime or "").upper() == "CRISIS":
            failed.append("regime=CRISIS")

        # ── Cost gate: expected move must clear round-trip costs ─
        if self.enable_cost_gate and move_pct is not None:
            fee = self.fee_pct if fee_pct is None else float(fee_pct)
            cost_pct = estimate_roundtrip_cost_pct(
                fee_pct=fee,
                spread_pct=spread_pct,
                slippage_pct=self.slippage_pct,
                safety_margin_pct=self.safety_margin_pct,
            )
            if cost_pct > 0:
                edge_ratio = float(move_pct) / cost_pct
                if edge_ratio < self.min_edge_ratio:
                    failed.append(
                        f"cost_gate edge={edge_ratio:.2f}<{self.min_edge_ratio}"
                    )

        # ── Component scores 0..100 ────────────────────────────
        if move_pct is not None:
            comps["momentum"] = max(0.0, min(100.0, (move_pct / 5.0) * 100.0))
        else:
            comps["momentum"] = 50.0

        if volume_irt is not None and volume_irt > 0:
            ratio = volume_irt / max(self.min_volume_irt, 1.0)
            comps["volume"] = max(0.0, min(100.0, 40.0 + 30.0 * min(ratio, 3.0)))
        else:
            comps["volume"] = 40.0

        if spread_pct is not None:
            comps["spread"] = max(
                0.0, min(100.0, 100.0 - (spread_pct / max(self.max_spread_pct, 0.01)) * 100.0)
            )
        else:
            comps["spread"] = 50.0

        reg = (regime or "BALANCED").upper()
        regime_map = {"AGGRESSIVE": 85.0, "BALANCED": 65.0, "CONSERVATIVE": 40.0, "CRISIS": 10.0}
        comps["regime"] = regime_map.get(reg, 50.0)

        if btc_change_pct is not None:
            if btc_change_pct >= 0:
                comps["btc_align"] = min(100.0, 60.0 + btc_change_pct * 5.0)
            else:
                comps["btc_align"] = max(0.0, 50.0 + btc_change_pct * 8.0)
        else:
            comps["btc_align"] = 50.0

        if ask_depth_irt is not None and ask_depth_irt > 0:
            comps["depth"] = max(0.0, min(100.0, 30.0 + ask_depth_irt / 10_000_000.0))
        else:
            comps["depth"] = 50.0

        if strategy_min_move is not None and move_pct is not None:
            if move_pct >= strategy_min_move:
                comps["strategy_fit"] = min(
                    100.0, 60.0 + (move_pct / max(strategy_min_move, 0.1)) * 20.0
                )
            else:
                comps["strategy_fit"] = max(
                    0.0, 40.0 * (move_pct / max(strategy_min_move, 0.1))
                )
        else:
            comps["strategy_fit"] = 55.0

        total_w = sum(self.weights.values()) or 1.0
        score = 0.0
        for k, w in self.weights.items():
            score += comps.get(k, 50.0) * (w / total_w)

        passed = (score >= self.min_confidence) and not failed
        reason = f"score={score:.1f} min={self.min_confidence}"
        if cost_pct is not None:
            reason += f" cost={cost_pct:.2f}%"
        if edge_ratio is not None:
            reason += f" edge={edge_ratio:.2f}"
        if failed:
            reason += f" filters={failed}"

        return ConfidenceResult(
            score=round(score, 2),
            passed=passed,
            components={k: round(v, 2) for k, v in comps.items()},
            failed_filters=failed,
            reason=reason,
            estimated_cost_pct=None if cost_pct is None else round(cost_pct, 3),
            edge_ratio=None if edge_ratio is None else round(edge_ratio, 3),
        )
