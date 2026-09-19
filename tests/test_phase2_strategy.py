"""Phase-2 Strategy & Auto Risk unit tests."""
from __future__ import annotations

import pytest

from trading.strategy_selector import (
    StrategySelector,
    MOMENTUM,
    TREND_FOLLOWING,
    DEFENSIVE,
    SCALPING,
    MEAN_REVERSION,
)
from trading.auto_risk import AutoRiskEngine
from trading.confidence import ConfidenceScorer
from trading.tech_regime import atr_pct, ema_slope_pct, hurst_proxy, summarize_series


def test_strategy_crisis_is_defensive():
    sel = StrategySelector()
    p = sel.select("CRISIS")
    assert p.name == DEFENSIVE
    assert p.max_new_entries_per_scan == 0


def test_strategy_aggressive_trend_or_scalp():
    sel = StrategySelector()
    p = sel.select("AGGRESSIVE", median_spread_pct=1.5, volatility_pct=2.0)
    assert p.name == TREND_FOLLOWING
    p2 = sel.select("AGGRESSIVE", median_spread_pct=0.4, volatility_pct=1.5)
    assert p2.name == SCALPING


def test_strategy_conservative_mean_reversion():
    sel = StrategySelector()
    p = sel.select("CONSERVATIVE", breadth_24h=0.3)
    assert p.name == MEAN_REVERSION


def test_auto_risk_fractional_basic():
    eng = AutoRiskEngine(risk_fraction=0.01, min_notional_irt=500_000, max_notional_irt=5_000_000)
    d = eng.compute(equity_irt=50_000_000, open_exposure_irt=0, strategy_aggression=0.5)
    assert d.allow_new_entries is True
    assert d.position_notional_irt >= 500_000
    assert d.stop_loss_pct > 0


def test_auto_risk_max_dd_breaker():
    eng = AutoRiskEngine(max_drawdown_pct=10.0, halt_on_breaker=True)
    d = eng.compute(equity_irt=10_000_000, drawdown_pct=15.0)
    assert d.allow_new_entries is False
    assert d.position_notional_irt == 0.0


def test_auto_risk_atr_widens_stop():
    eng = AutoRiskEngine(base_stop_pct=3.0, atr_stop_mult=2.0)
    d = eng.compute(equity_irt=20_000_000, atr_pct=2.5)
    assert d.stop_loss_pct >= 3.0  # 2.5 * 2 = 5


def test_confidence_passes_strong_candidate():
    sc = ConfidenceScorer(min_confidence=50.0)
    r = sc.score(
        move_pct=3.0,
        volume_irt=200_000_000,
        spread_pct=0.5,
        regime="AGGRESSIVE",
        btc_change_pct=1.0,
        strategy_min_move=1.0,
    )
    assert r.passed is True
    assert r.score >= 50.0


def test_confidence_fails_crisis_filter():
    sc = ConfidenceScorer(min_confidence=40.0, block_in_crisis=True)
    r = sc.score(move_pct=5.0, volume_irt=500_000_000, spread_pct=0.3, regime="CRISIS")
    assert r.passed is False
    assert "CRISIS" in r.failed_filters[0]


def test_tech_regime_helpers():
    closes = [100 + i * 0.5 for i in range(40)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    a = atr_pct(highs, lows, closes, period=10)
    assert a is not None and a > 0
    s = ema_slope_pct(closes, period=10, lookback=3)
    assert s is not None
    h = hurst_proxy(closes, max_lag=5)
    assert h is not None and 0 <= h <= 1
    summary = summarize_series(closes, highs, lows)
    assert "atr_pct" in summary
