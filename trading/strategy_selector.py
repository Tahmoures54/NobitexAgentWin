"""
trading/strategy_selector.py — Auto strategy selection from market regime.

Maps RegimeDetector labels (and optional technical features) to a concrete
strategy profile used by the entry engine and risk layer.

Strategies:
- TREND_FOLLOWING  — ride sustained directional moves
- MOMENTUM         — short-horizon continuation (default Nobitex engine)
- MEAN_REVERSION   — fade stretched moves in range-bound tape
- SCALPING         — tight spread, high liquidity, quick exits
- DEFENSIVE        — minimal new risk (crisis / deep drawdown)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

TREND_FOLLOWING = "TREND_FOLLOWING"
MOMENTUM = "MOMENTUM"
MEAN_REVERSION = "MEAN_REVERSION"
SCALPING = "SCALPING"
DEFENSIVE = "DEFENSIVE"

ALL_STRATEGIES = (TREND_FOLLOWING, MOMENTUM, MEAN_REVERSION, SCALPING, DEFENSIVE)


@dataclass
class StrategyProfile:
    name: str
    # Entry aggressiveness 0..1
    entry_aggression: float = 0.5
    # Prefer fewer but larger moves vs many small ones
    min_move_pct: float = 1.0
    max_spread_pct: float = 1.5
    min_volume_irt: float = 50_000_000.0
    # Hold / exit bias
    hold_bias: float = 0.5          # 0 = quick exit, 1 = trail longer
    use_mean_reversion: bool = False
    allow_eagle: bool = True
    max_new_entries_per_scan: int = 1
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "entry_aggression": self.entry_aggression,
            "min_move_pct": self.min_move_pct,
            "max_spread_pct": self.max_spread_pct,
            "min_volume_irt": self.min_volume_irt,
            "hold_bias": self.hold_bias,
            "use_mean_reversion": self.use_mean_reversion,
            "allow_eagle": self.allow_eagle,
            "max_new_entries_per_scan": self.max_new_entries_per_scan,
            "notes": self.notes,
        }


# Default profiles (tunable via AutoParameter later)
_PROFILES: Dict[str, StrategyProfile] = {
    TREND_FOLLOWING: StrategyProfile(
        name=TREND_FOLLOWING,
        entry_aggression=0.65,
        min_move_pct=1.5,
        max_spread_pct=1.8,
        min_volume_irt=80_000_000.0,
        hold_bias=0.75,
        allow_eagle=True,
        notes="Sustained direction; wider trail",
    ),
    MOMENTUM: StrategyProfile(
        name=MOMENTUM,
        entry_aggression=0.55,
        min_move_pct=1.0,
        max_spread_pct=1.5,
        min_volume_irt=50_000_000.0,
        hold_bias=0.5,
        allow_eagle=True,
        notes="Short-horizon continuation (Nobitex default style)",
    ),
    MEAN_REVERSION: StrategyProfile(
        name=MEAN_REVERSION,
        entry_aggression=0.4,
        min_move_pct=2.0,
        max_spread_pct=1.2,
        min_volume_irt=60_000_000.0,
        hold_bias=0.35,
        use_mean_reversion=True,
        allow_eagle=False,
        notes="Fade stretched moves in range tape",
    ),
    SCALPING: StrategyProfile(
        name=SCALPING,
        entry_aggression=0.7,
        min_move_pct=0.4,
        max_spread_pct=0.6,
        min_volume_irt=150_000_000.0,
        hold_bias=0.2,
        allow_eagle=False,
        max_new_entries_per_scan=2,
        notes="Tight spread, high liquidity only",
    ),
    DEFENSIVE: StrategyProfile(
        name=DEFENSIVE,
        entry_aggression=0.15,
        min_move_pct=3.0,
        max_spread_pct=1.0,
        min_volume_irt=200_000_000.0,
        hold_bias=0.3,
        allow_eagle=True,
        max_new_entries_per_scan=0,
        notes="Crisis / deep DD — block most new risk",
    ),
}


class StrategySelector:
    """Pick strategy profile from regime label + optional features."""

    def __init__(self, profiles: Optional[Dict[str, StrategyProfile]] = None) -> None:
        self.profiles = dict(profiles or _PROFILES)
        self.current: StrategyProfile = self.profiles[MOMENTUM]
        self.last_reason: str = "init"

    def select(
        self,
        regime: str,
        *,
        volatility_pct: Optional[float] = None,
        median_spread_pct: Optional[float] = None,
        breadth_24h: Optional[float] = None,
        drawdown_pct: Optional[float] = None,
    ) -> StrategyProfile:
        """
        Map regime → strategy, with soft overrides from live features.

        regime: AGGRESSIVE | BALANCED | CONSERVATIVE | CRISIS
        """
        regime_u = (regime or "BALANCED").upper()
        reasons: List[str] = [f"regime={regime_u}"]

        # Hard defensive gates
        if regime_u == "CRISIS":
            choice = DEFENSIVE
            reasons.append("crisis→defensive")
        elif drawdown_pct is not None and drawdown_pct >= 12.0:
            choice = DEFENSIVE
            reasons.append(f"dd={drawdown_pct:.1f}%→defensive")
        elif regime_u == "AGGRESSIVE":
            # High vol + tight spread → scalping; else trend
            if (
                median_spread_pct is not None
                and median_spread_pct <= 0.7
                and volatility_pct is not None
                and 0.8 <= volatility_pct <= 4.0
            ):
                choice = SCALPING
                reasons.append("tight_spread+mod_vol→scalping")
            else:
                choice = TREND_FOLLOWING
                reasons.append("aggressive→trend")
        elif regime_u == "CONSERVATIVE":
            if breadth_24h is not None and breadth_24h < 0.4:
                choice = MEAN_REVERSION
                reasons.append("low_breadth→mean_reversion")
            else:
                choice = MOMENTUM
                reasons.append("conservative→momentum")
        else:  # BALANCED
            choice = MOMENTUM
            reasons.append("balanced→momentum")

        profile = self.profiles.get(choice) or self.profiles[MOMENTUM]
        self.current = profile
        self.last_reason = "; ".join(reasons)
        logger.info("Strategy selected: %s (%s)", profile.name, self.last_reason)
        return profile

    def get_profile(self, name: str) -> StrategyProfile:
        return self.profiles.get(name.upper(), self.profiles[MOMENTUM])
