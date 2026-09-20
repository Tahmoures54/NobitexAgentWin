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
