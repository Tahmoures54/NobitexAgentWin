"""Tests for AdaptivePipeline integration layer."""
from __future__ import annotations

from trading.adaptive_pipeline import AdaptivePipeline
from trading.nobitex_momentum_engine import NobitexMomentumEngine
from trading.strategy_selector import DEFENSIVE, MOMENTUM


def _row(symbol, price, bid, ask, volume, ch24=1.0, ch1h=1.0):
    return {
        "Symbol": symbol,
        "Price": price,
        "Bid": bid,
        "Ask": ask,
        "Volume": volume,
        "24h Change (%)": ch24,
        "1h Change (%)": ch1h,
        "Pair": f"{symbol}IRT",
    }


def test_pipeline_defensive_on_crisis():
    eng = NobitexMomentumEngine(min_volume_irt=1_000_000, min_observed_move_pct=0.1)
    pipe = AdaptivePipeline(momentum_engine=eng)
    rows = [_row("AAA", 100, 99, 100, 500_000_000, ch24=5)]
    # Seed history so momentum can see a move
    for _ in range(5):
        eng.evaluate(rows, require_depth=False)
        rows[0]["Price"] *= 1.02
        rows[0]["Ask"] = rows[0]["Price"]
        rows[0]["Bid"] = rows[0]["Price"] * 0.999

    result = pipe.run(rows, equity_irt=50_000_000, regime_override="CRISIS", require_depth=False)
    assert result.regime == "CRISIS"
    assert result.strategy.name == DEFENSIVE
    assert result.candidates == [] or result.risk.allow_new_entries is False or result.strategy.max_new_entries_per_scan == 0


def test_pipeline_balanced_runs_engine():
    eng = NobitexMomentumEngine(
        min_volume_irt=1_000_000,
        min_observed_move_pct=0.5,
        pump_threshold_pct=0.5,
        min_confirm_scans=1,
        max_spread_pct=2.0,
    )
    pipe = AdaptivePipeline(momentum_engine=eng, min_confidence=30.0)
    price = 1000.0
    rows = [_row("BBB", price, price * 0.999, price, 500_000_000)]
    for i in range(6):
        price *= 1.015
        rows = [_row("BBB", price, price * 0.999, price, 500_000_000, ch24=3.0)]
        eng.evaluate(rows, require_depth=False)

    result = pipe.run(
        rows,
        equity_irt=100_000_000,
        regime_override="BALANCED",
        require_depth=False,
        max_candidates=3,
    )
    assert result.regime == "BALANCED"
    assert result.strategy.name in (MOMENTUM, "MOMENTUM")
    assert result.risk.position_notional_irt > 0
    assert "accepted" in result.stats
