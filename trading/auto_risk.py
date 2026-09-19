"""
trading/auto_risk.py — Adaptive risk sizing and stop geometry.

Provides:
- Fixed-fractional and Kelly-lite position sizing
- ATR-based stop distance
- Chandelier-style trailing activation
- Max drawdown circuit breaker (halt new entries)

Defaults aligned with data/bot_config.json conservative pre-sample profile.
"""
from __future__ import annotations

import logging
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
        risk_fraction: float = 0.005,         # 0.5% equity risk per trade
        kelly_fraction: float = 0.25,
        min_notional_irt: float = 500_000.0,
        max_notional_irt: float = 3_000_000.0,
        max_exposure_pct: float = 0.30,
        base_stop_pct: float = 3.0,
        base_trail_act_pct: float = 1.5,
        base_trail_dist_pct: float = 1.2,
        base_tp_pct: float = 0.0,             # trail-driven exits by default
        atr_stop_mult: float = 1.5,
        atr_trail_mult: float = 2.0,
        max_drawdown_pct: float = 12.0,
        halt_on_breaker: bool = True,
        kelly_enabled: bool = False,         # off until sample proven
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
        self.kelly_enabled = bool(kelly_enabled)

    def compute(
        self,
        *,
        equity_irt: float,
        open_exposure_irt: float = 0.0,
        win_rate: Optional[float] = None,
        avg_win_pct: Optional[float] = None,
        avg_loss_pct: Optional[float] = None,
        atr_pct: Optional[float] = None,
        drawdown_pct: Optional[float] = None,
        strategy_aggression: float = 0.5,
        method: str = "fractional",
    ) -> RiskDecision:
        reasons = []
        allow = True

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

        stop_pct = self.base_stop_pct
        trail_act = self.base_trail_act_pct
        trail_dist = self.base_trail_dist_pct
        if atr_pct is not None and atr_pct > 0:
            stop_pct = max(1.0, min(12.0, atr_pct * self.atr_stop_mult))
            trail_dist = max(0.8, min(8.0, atr_pct * self.atr_trail_mult))
            trail_act = max(stop_pct * 0.5, min(trail_act, trail_dist))
            reasons.append(f"atr_pct={atr_pct:.2f}")

        method_l = (method or "fractional").lower()
        if method_l == "kelly_lite" and not self.kelly_enabled:
            method_l = "fractional"
            reasons.append("kelly_disabled")

        notional = self.min_notional_irt

        if (
            method_l == "kelly_lite"
            and self.kelly_enabled
            and win_rate
            and avg_win_pct
            and avg_loss_pct
        ):
            w = max(0.05, min(0.95, float(win_rate)))
            aw = max(0.1, float(avg_win_pct))
            al = max(0.1, float(avg_loss_pct))
            b = aw / al
            kelly = w - (1.0 - w) / b
            kelly = max(0.0, kelly) * self.kelly_fraction
            risk_budget = equity * kelly
            notional = risk_budget / (stop_pct / 100.0) if stop_pct > 0 else risk_budget
            reasons.append(f"kelly_lite={kelly:.3f}")
            method_l = "kelly_lite"
        else:
            risk_budget = equity * self.risk_fraction * (0.5 + strategy_aggression)
            notional = risk_budget / (stop_pct / 100.0) if stop_pct > 0 else risk_budget
            reasons.append(f"fractional_risk={self.risk_fraction}")
            method_l = "fractional"

        notional = max(self.min_notional_irt, min(self.max_notional_irt, notional))
        notional = min(notional, remaining) if remaining > 0 else 0.0
        if notional < self.min_notional_irt * 0.5:
            allow = False
            reasons.append("insufficient_room")

        tp = self.base_tp_pct

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
