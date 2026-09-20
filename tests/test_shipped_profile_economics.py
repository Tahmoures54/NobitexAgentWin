# tests/test_shipped_profile_economics.py
"""The shipped test profile must not contradict its own cost guard.

``data/bot_config.json`` is the profile an operator runs the Nobitex paper test
with (see NOBITEX_TEST_READINESS.md).  Its numbers have to satisfy the same
geometry the cost guard enforces at runtime - otherwise the guard silently
rejects every entry, or the profile looks coherent while being unable to pay
for a single round trip.  These are invariants, not tuned values: they hold for
any fee/spread/geometry an operator picks.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading.bot_config import PROFITABILITY_GUARD_DEFAULTS

CONFIG = Path(__file__).resolve().parents[1] / "data" / "bot_config.json"


@pytest.fixture(scope="module")
def cfg() -> dict:
    return json.loads(CONFIG.read_text())


def round_trip_pct(cfg: dict) -> float:
    """Fee + half-spread, both sides - what one trade costs before it moves."""
    return 2.0 * (float(cfg["trading_fee_pct"]) + float(cfg["paper_half_spread_pct"]))


def test_ships_in_paper_mode(cfg):
    assert cfg["execution_mode"] == "paper"


def test_trailing_gap_pays_for_the_round_trip(cfg):
    gap = float(cfg["trailing_activation_pct"]) - float(cfg["trailing_distance_pct"])
    assert gap >= round_trip_pct(cfg), (
        f"the smallest trailing win ({gap:.2f}%) cannot cover the round trip "
        f"({round_trip_pct(cfg):.2f}%) - every trailed exit would be net negative"
    )


def test_entry_threshold_clears_the_cost_guard_floor(cfg):
    floor = float(cfg["min_edge_multiple"]) * round_trip_pct(cfg)
    assert float(cfg["min_observed_move_pct"]) >= floor, (
        f"min_observed_move_pct is below the cost guard's own floor ({floor:.2f}%), "
        "so the guard would reject every entry the signal produces"
    )


def test_hard_stop_is_wider_than_the_round_trip(cfg):
    assert float(cfg["stop_loss_pct"]) > round_trip_pct(cfg)


def test_guards_are_on_and_auto_regime_cannot_rewrite_risk(cfg):
    assert cfg["cost_guard_enabled"] is True
    assert cfg["expectancy_guard_enabled"] is True
    assert float(cfg["expectancy_guard_min_expectancy_pct"]) >= 0.0
    assert cfg["regime_auto_apply_all"] is False
    assert "max_open_positions" in cfg["regime_controlled_keys"]


def test_time_stop_is_enabled_and_matches_the_migration_default(cfg):
    """A migrated config and the shipped profile must run the same time stop."""
    assert cfg["max_hold_minutes"] == PROFITABILITY_GUARD_DEFAULTS["max_hold_minutes"]
    assert cfg["max_hold_minutes"] > 0


def test_no_risk_key_is_left_at_a_dead_value(cfg):
    """Keys the analysis found dead in v6.9.0 must carry real values now."""
    assert float(cfg["risk_per_trade_pct"]) > 0
    assert int(cfg["expectancy_guard_trades"]) >= 5
    assert float(cfg["max_local_premium_pct"]) >= 0.0


def test_the_profile_actually_reaches_the_tracker(tmp_path, monkeypatch):
    """`apply_to_tracker()` is the only link between the config file and the
    trading loop - if it drops a guard, that guard does not exist at runtime no
    matter what the JSON says.  This is the "dead config key" class of bug the
    analysis found in v6.9.0 (`max_hold_minutes` was stored and never read).
    """
    import signal_tracker as st
    from trading.bot_config import apply_to_tracker, load_config

    monkeypatch.setattr(st, "APPDATA_DIR", str(tmp_path))
    cfg = load_config(str(CONFIG))
    tracker = st.SignalTracker(db_filename="profile_sync.db", account_balance=1000.0)
    try:
        apply_to_tracker(cfg, tracker, is_live_exchange=False)

        assert tracker.max_hold_minutes == int(cfg.max_hold_minutes) > 0
        assert tracker.cost_guard_enabled is bool(cfg.cost_guard_enabled) is True
        assert tracker.min_edge_multiple == pytest.approx(float(cfg.min_edge_multiple))
        assert tracker.paper_half_spread_pct == pytest.approx(
            float(cfg.paper_half_spread_pct))
        assert tracker.expectancy_guard_enabled is True
        assert tracker.expectancy_guard_trades == int(cfg.expectancy_guard_trades)
        assert tracker.expectancy_guard_min_expectancy_pct == pytest.approx(
            float(cfg.expectancy_guard_min_expectancy_pct))
        assert tracker.trailing_activation_pct == pytest.approx(
            float(cfg.trailing_activation_pct))
        assert tracker.trailing_distance_pct == pytest.approx(
            float(cfg.trailing_distance_pct))
        assert tracker.stop_loss_pct == pytest.approx(float(cfg.stop_loss_pct))
        assert tracker.take_profit_percent == pytest.approx(
            float(cfg.take_profit_percent))
        assert tracker.trading_fee_pct == pytest.approx(float(cfg.trading_fee_pct))
    finally:
        for name in ("close", "shutdown", "disconnect"):
            fn = getattr(tracker, name, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001 - best effort teardown
                    pass
                break
