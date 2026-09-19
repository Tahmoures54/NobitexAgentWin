"""
trading/auto_risk.py — Adaptive risk sizing and stop geometry.

Provides:
- Fixed-fractional and Kelly-lite position sizing
- ATR-based stop distance
- Chandelier-style trailing activation
- Max drawdown circuit breaker (halt new entries)
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    position_notional_irt: float
    stop_loss_pct: float
    trailing_activation_pct: float
    trailing_distance_pct: float
    take_profit_pct: float
    allow_new_entries: bool
    reason: str
    method: str  # fixed | fractional | kelly_lite

    def to_dict(self) -> Dict[str, Any]:
        return {
            "position_notional_irt": self.position_notional_irt,
            "stop_loss_pct": self.stop_loss_pct,
            "trailing_activation_pct": self.trailing_activation_pct,
            "trailing_distance_pct": self.trailing_distance_pct,
            "take_profit_pct": self.take_profit_pct,
            "allow_new_entries": self.allow_new_entries,
            "reason": self.reason,
            "method": self.method,
        }


class AutoRiskEngine:
    """Compute position size and stop geometry from account + market state."""

    def __init__(
        self,
        *,
        # Sizing
        risk_fraction: float = 0.01,          # risk ~1% of equity per trade
        kelly_fraction: float = 0.25,         # fractional Kelly (quarter)
        min_notional_irt: float = 500_000.0,
        max_notional_irt: float = 5_000_000.0,
        max_exposure_pct: float = 0.90,
        # Default stops (pct)
        base_stop_pct: float = 3.0,
        base_trail_act_pct: float = 3.0,
        base_trail_dist_pct: float = 2.0,
        base_tp_pct: float = 50.0,
        # ATR scaling
        atr_stop_mult: float = 1.5,
        atr_trail_mult: float = 2.0,
        # Circuit breaker
        max_drawdown_pct: float = 15.0,
        halt_on_breaker: bool = True,
    ) -> None:
        self.risk_fraction = float(risk_fraction)
        self.kelly_fraction = float(kelly_fraction)
        self.min_notional_irt = float(min_notional_irt)
        self.max_notional_irt = float(max_notional_irt)
        self.max_exposure_pct = float(max_exposure_pct)
        self.base_stop_pct = float(base_stop_pct)
        self.base_trail_act_pct = float(base_trail_act_pct)
        self.base_trail_dist_pct = float(base_trail_dist_pct)
        self.base_tp_pct = float(base_tp_pct)
        self.atr_stop_mult = float(atr_stop_mult)
        self.atr_trail_mult = float(atr_trail_mult)
        self.max_drawdown_pct = float(max_drawdown_pct)
        self.halt_on_breaker = bool(halt_on_breaker)

    def compute(
        self,
        *,
        equity_irt: float,
        open_exposure_irt: float = 0.0,
        win_rate: Optional[float] = None,       # 0..1
        avg_win_pct: Optional[float] = None,
        avg_loss_pct: Optional[float] = None,
        atr_pct: Optional[float] = None,        # ATR as % of price
        drawdown_pct: Optional[float] = None,
        strategy_aggression: float = 0.5,
        method: str = "fractional",
    ) -> RiskDecision:
        reasons = []
        allow = True

        # ── Circuit breaker ────────────────────────────────────
        if drawdown_pct is not None and drawdown_pct >= self.max_drawdown_pct:
            allow = not self.halt_on_breaker
            reasons.append(f"max_dd_breaker={drawdown_pct:.1f}%")
            if not allow:
                return RiskDecision(
                    position_notional_irt=0.0,
                    stop_loss_pct=self.base_stop_pct,
                    trailing_activation_pct=self.base_trail_act_pct,
                    trailing_distance_pct=self.base_trail_dist_pct,
                    take_profit_pct=self.base_tp_pct,
                    allow_new_entries=False,
                    reason="; ".join(reasons),
                    method=method,
                )

        equity = max(0.0, float(equity_irt))
        exposure = max(0.0, float(open_exposure_irt))
        remaining = max(0.0, equity * self.max_exposure_pct - exposure)

        # ── Stop geometry (ATR-aware) ──────────────────────────
        stop_pct = self.base_stop_pct
        trail_act = self.base_trail_act_pct
        trail_dist = self.base_trail_dist_pct
        if atr_pct is not None and atr_pct > 0:
            stop_pct = max(1.0, min(12.0, atr_pct * self.atr_stop_mult))
            trail_dist = max(0.8, min(8.0, atr_pct * self.atr_trail_mult))
            trail_act = max(stop_pct, trail_dist)
            reasons.append(f"atr_pct={atr_pct:.2f}")

        # ── Position notional ──────────────────────────────────
        method_l = (method or "fractional").lower()
        notional = self.min_notional_irt

        if method_l == "kelly_lite" and win_rate and avg_win_pct and avg_loss_pct:
            # Kelly f* = W - (1-W)/(avg_win/avg_loss)  for net odds
            w = max(0.05, min(0.95, float(win_rate)))
            aw = max(0.1, float(avg_win_pct))
            al = max(0.1, float(avg_loss_pct))
            b = aw / al
            kelly = w - (1.0 - w) / b
            kelly = max(0.0, kelly) * self.kelly_fraction
            # Convert Kelly fraction of equity into notional capped by stop
            risk_budget = equity * kelly
            notional = risk_budget / (stop_pct / 100.0) if stop_pct > 0 else risk_budget
            reasons.append(f"kelly_lite={kelly:.3f}")
            method_l = "kelly_lite"
        else:
            # Fixed fractional: risk_fraction of equity / stop distance
            risk_budget = equity * self.risk_fraction * (0.5 + strategy_aggression)
            notional = risk_budget / (stop_pct / 100.0) if stop_pct > 0 else risk_budget
            reasons.append(f"fractional_risk={self.risk_fraction}")
            method_l = "fractional"

        notional = max(self.min_notional_irt, min(self.max_notional_irt, notional))
        notional = min(notional, remaining) if remaining > 0 else 0.0
        if notional < self.min_notional_irt * 0.5:
            allow = False
            reasons.append("insufficient_room")

        # Slightly tighter TP when defensive aggression is low
        tp = self.base_tp_pct
        if strategy_aggression < 0.3:
            tp = min(tp, 25.0)

        return RiskDecision(
            position_notional_irt=round(notional, 0),
            stop_loss_pct=round(stop_pct, 2),
            trailing_activation_pct=round(trail_act, 2),
            trailing_distance_pct=round(trail_dist, 2),
            take_profit_pct=round(tp, 2),
            allow_new_entries=allow,
            reason="; ".join(reasons) or "ok",
            method=method_l,
        )
