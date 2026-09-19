"""
trading/adaptive_pipeline.py — End-to-end Phase-2 adaptive scan pipeline.

Flow per scan:
1. RegimeDetector (existing) → AGGRESSIVE / BALANCED / CONSERVATIVE / CRISIS
2. StrategySelector → StrategyProfile (thresholds + aggression)
3. Configure NobitexMomentumEngine from profile
4. Evaluate candidates
5. ConfidenceScorer filters confluence
6. AutoRiskEngine sizes / gates new entries

Does not place orders; returns ranked, risk-annotated candidates for SignalTracker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from trading.strategy_selector import StrategySelector, StrategyProfile, DEFENSIVE
from trading.auto_risk import AutoRiskEngine, RiskDecision
from trading.confidence import ConfidenceScorer, ConfidenceResult

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    regime: str
    strategy: StrategyProfile
    risk: RiskDecision
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    rejected: List[Dict[str, Any]] = field(default_factory=list)
    stats: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "regime": self.regime,
            "strategy": self.strategy.to_dict(),
            "risk": self.risk.to_dict(),
            "candidates": self.candidates,
            "rejected_count": len(self.rejected),
            "stats": self.stats,
        }


class AdaptivePipeline:
    """Glue layer between regime, strategy, confidence, risk and momentum engine."""

    def __init__(
        self,
        *,
        momentum_engine: Any = None,
        regime_detector: Any = None,
        strategy_selector: Optional[StrategySelector] = None,
        confidence_scorer: Optional[ConfidenceScorer] = None,
        auto_risk: Optional[AutoRiskEngine] = None,
        min_confidence: float = 55.0,
    ) -> None:
        self.engine = momentum_engine
        self.regime_detector = regime_detector
        self.selector = strategy_selector or StrategySelector()
        self.scorer = confidence_scorer or ConfidenceScorer(min_confidence=min_confidence)
        self.risk_engine = auto_risk or AutoRiskEngine()
        self.last_result: Optional[PipelineResult] = None

    def _apply_profile_to_engine(self, profile: StrategyProfile) -> None:
        if self.engine is None:
            return
        cfg = {
            "min_observed_move_pct": profile.min_move_pct,
            "pump_threshold_pct": max(profile.min_move_pct, getattr(self.engine, "pump_threshold_pct", 1.5)),
            "max_spread_pct": profile.max_spread_pct,
            "min_volume_irt": profile.min_volume_irt,
            "btc_dump_exception_enabled": profile.allow_eagle,
        }
        if hasattr(self.engine, "configure"):
            self.engine.configure(**cfg)
        else:
            for k, v in cfg.items():
                if hasattr(self.engine, k):
                    setattr(self.engine, k, v)

    def _resolve_regime(
        self,
        local_rows: List[Dict[str, Any]],
        tracker: Any = None,
        regime_override: Optional[str] = None,
    ) -> str:
        if regime_override:
            return str(regime_override).upper()
        det = self.regime_detector
        if det is None:
            return "BALANCED"
        # Prefer update()/detect() APIs used by existing RegimeDetector
        for meth_name in ("update", "detect", "evaluate", "get_regime"):
            meth = getattr(det, meth_name, None)
            if not callable(meth):
                continue
            try:
                if meth_name in ("update", "detect", "evaluate"):
                    out = meth(local_rows, tracker=tracker) if tracker is not None else meth(local_rows)
                else:
                    out = meth()
                if isinstance(out, dict):
                    return str(out.get("regime") or out.get("current_regime") or "BALANCED").upper()
                if isinstance(out, str):
                    return out.upper()
            except TypeError:
                try:
                    out = meth(local_rows)
                    if isinstance(out, dict):
                        return str(out.get("regime") or "BALANCED").upper()
                    if isinstance(out, str):
                        return out.upper()
                except Exception:
                    continue
            except Exception as exc:
                logger.debug("regime detector %s failed: %s", meth_name, exc)
        return str(getattr(det, "current_regime", "BALANCED")).upper()

    def run(
        self,
        local_rows: List[Dict[str, Any]],
        *,
        equity_irt: float = 0.0,
        open_exposure_irt: float = 0.0,
        tracker: Any = None,
        regime_override: Optional[str] = None,
        atr_pct: Optional[float] = None,
        drawdown_pct: Optional[float] = None,
        win_rate: Optional[float] = None,
        require_depth: bool = True,
        max_candidates: int = 5,
    ) -> PipelineResult:
        regime = self._resolve_regime(local_rows, tracker=tracker, regime_override=regime_override)

        # Feature hints for selector
        breadth = None
        median_spread = None
        try:
            changes = []
            spreads = []
            for r in local_rows or []:
                ch = r.get("24h Change (%)")
                if ch is not None:
                    try:
                        changes.append(float(ch))
                    except (TypeError, ValueError):
                        pass
                bid = float(r.get("Bid") or 0) or 0.0
                ask = float(r.get("Ask") or 0) or 0.0
                if bid > 0 and ask >= bid:
                    spreads.append((ask - bid) / bid * 100.0)
            if changes:
                breadth = sum(1 for c in changes if c > 0) / len(changes)
            if spreads:
                spreads.sort()
                median_spread = spreads[len(spreads) // 2]
        except Exception:
            pass

        if drawdown_pct is None and tracker is not None:
            try:
                summary = getattr(tracker, "get_summary_stats", lambda: {})() or {}
                drawdown_pct = float(summary.get("drawdown_pct") or 0)
                if win_rate is None and summary.get("win_rate") is not None:
                    wr = float(summary["win_rate"])
                    win_rate = wr / 100.0 if wr > 1.0 else wr
            except Exception:
                pass

        strategy_performance: Dict[str, Dict[str, Any]] = {}
        if tracker is not None:
            try:
                get_perf = getattr(tracker, "get_strategy_performance", None)
                if callable(get_perf):
                    strategy_performance = get_perf(100) or {}
            except Exception as exc:
                logger.debug("strategy performance unavailable: %s", exc)

        profile = self.selector.select(
            regime,
            volatility_pct=atr_pct,
            median_spread_pct=median_spread,
            breadth_24h=breadth,
            drawdown_pct=drawdown_pct,
            performance=strategy_performance,
        )
        self._apply_profile_to_engine(profile)

        risk = self.risk_engine.compute(
            equity_irt=equity_irt,
            open_exposure_irt=open_exposure_irt,
            win_rate=win_rate,
            atr_pct=atr_pct,
            drawdown_pct=drawdown_pct,
            strategy_aggression=profile.entry_aggression,
        )

        raw_candidates: List[Dict[str, Any]] = []
        engine_stats: Dict[str, Any] = {}
        if self.engine is not None and profile.max_new_entries_per_scan > 0 and risk.allow_new_entries:
            try:
                raw_candidates = self.engine.evaluate(local_rows, require_depth=require_depth) or []
                engine_stats = dict(getattr(self.engine, "last_stats", {}) or {})
            except Exception as exc:
                logger.exception("momentum evaluate failed: %s", exc)
                raw_candidates = []
        elif profile.name == DEFENSIVE or not risk.allow_new_entries:
            engine_stats = {"skipped": "defensive_or_risk_halt"}

        btc_ch = None
        for r in local_rows or []:
            if str(r.get("Symbol") or "").upper() == "BTC":
                try:
                    btc_ch = float(r.get("24h Change (%)") or 0)
                except (TypeError, ValueError):
                    pass
                break

        accepted: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        limit = min(int(max_candidates), max(0, int(profile.max_new_entries_per_scan)))

        for cand in raw_candidates:
            move = cand.get("ObservedLocalMove (%)", cand.get("pump_pct"))
            vol = cand.get("Volume")
            spread = cand.get("Nobitex Spread (%)")
            try:
                move_f = float(move) if move is not None else None
            except (TypeError, ValueError):
                move_f = None
            try:
                vol_f = float(vol) if vol is not None else None
            except (TypeError, ValueError):
                vol_f = None
            try:
                spread_f = float(spread) if spread is not None else None
            except (TypeError, ValueError):
                spread_f = None

            conf: ConfidenceResult = self.scorer.score(
                move_pct=move_f,
                volume_irt=vol_f,
                spread_pct=spread_f,
                regime=regime,
                btc_change_pct=btc_ch,
                strategy_name=profile.name,
                strategy_min_move=profile.min_move_pct,
            )
            enriched = dict(cand)
            enriched["ConfidenceScore"] = conf.score
            enriched["ConfidencePassed"] = conf.passed
            enriched["ConfidenceReason"] = conf.reason
            enriched["Strategy"] = profile.name
            enriched["Regime"] = regime
            enriched["RiskNotionalIRT"] = risk.position_notional_irt
            enriched["RiskStopPct"] = risk.stop_loss_pct
            enriched["RiskTrailActPct"] = risk.trailing_activation_pct
            enriched["RiskTrailDistPct"] = risk.trailing_distance_pct
            enriched["RiskAllowEntry"] = risk.allow_new_entries

            if conf.passed and risk.allow_new_entries and len(accepted) < limit:
                accepted.append(enriched)
            else:
                rejected.append(enriched)

        stats = {
            "regime": regime,
            "strategy": profile.name,
            "strategy_reason": self.selector.last_reason,
            "strategy_performance": strategy_performance,
            "raw_candidates": len(raw_candidates),
            "accepted": len(accepted),
            "rejected": len(rejected),
            "risk_allow": risk.allow_new_entries,
            "engine": engine_stats,
        }
        result = PipelineResult(
            regime=regime,
            strategy=profile,
            risk=risk,
            candidates=accepted,
            rejected=rejected,
            stats=stats,
        )
        self.last_result = result
        logger.info(
            "AdaptivePipeline: regime=%s strategy=%s accepted=%d risk_notional=%.0f",
            regime, profile.name, len(accepted), risk.position_notional_irt,
        )
        return result
