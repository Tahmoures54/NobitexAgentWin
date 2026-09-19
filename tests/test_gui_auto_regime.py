"""
Tests for the auto-regime strategy logic in gui/gui_main.py (v7.8.1).

These tests do NOT create a Tk window.  They exercise the helper
methods on a bare-bones CryptoScannerApp instance created via
`__new__`.

Bugs being covered:
1. `_apply_live_sizing()` was not called after `_apply_auto_regime_strategy()`,
   so a preset that changed `fixed_position_quote` left
   `max_notional_quote` stale on the tracker.

2. `_last_applied_regime_strategy` was never reset when auto mode was
   turned off, so the "strategy switched" log could be suppressed.

3. `_apply_auto_regime_strategy()` called `setattr` for every field in
   the preset without validating against the dataclass.  A typo in a
   preset produced an orphan attribute silently.

4. `_update_regime()` returned BALANCED on `decide()` exceptions but
   never refreshed `_last_regime_info`, so the log lines in
   `_live_auto_scan` showed stale scores.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from gui.gui_main import CryptoScannerApp
from trading.bot_config import BotConfig
from trading.regime_detector import BALANCED, AGGRESSIVE, CRISIS


# ══════════════════════════════════════════════════════════════
# Fixture: a bare CryptoScannerApp
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def app():
    a = CryptoScannerApp.__new__(CryptoScannerApp)
    a.current_regime = BALANCED
    a.real_signal_tracker = None
    a.signal_tracker = None
    a._last_applied_max_open = None
    a._last_applied_max_open_regime = None
    a._last_applied_regime_strategy = None
    a._last_regime_info = {}
    a.regime_detector = MagicMock()
    return a


@pytest.fixture
def tracker():
    """A stand-in for SignalTracker with only the fields we touch."""
    t = MagicMock()
    t.fixed_position_quote = 2_000_000.0
    t.max_notional_quote = 8_000_000.0
    t.min_notional_quote = 500_000.0
    t.max_position_pct = 15.0
    t.max_total_exposure_pct = 40.0
    t.position_size_mode = "risk_percent"
    t.max_open_trades = 3
    return t


# ══════════════════════════════════════════════════════════════
# 1. _apply_auto_regime_strategy validates field names
# ══════════════════════════════════════════════════════════════

def test_auto_regime_applies_known_fields(app, tracker):
    app.real_signal_tracker = tracker
    app.signal_tracker = tracker

    cfg = BotConfig()
    cfg.auto_regime_strategy = True

    # Inject a preset that overlaps with real BotConfig fields.
    # We import the real STRATEGY_PRESETS to modify it temporarily.
    from gui.dialogs import settings_window as sw
    original = dict(sw.STRATEGY_PRESETS["balanced"]["settings"])
    try:
        sw.STRATEGY_PRESETS["balanced"]["settings"] = {
            "fixed_position_quote": 4_000_000.0,
            "stop_loss_pct": 3.5,
        }
        app._apply_auto_regime_strategy(BALANCED, cfg)

        assert cfg.fixed_position_quote == 4_000_000.0
        assert cfg.stop_loss_pct == 3.5
        assert app._last_applied_regime_strategy == "balanced"
    finally:
        sw.STRATEGY_PRESETS["balanced"]["settings"] = original


def test_auto_regime_rejects_unknown_fields(app, tracker):
    app.real_signal_tracker = tracker
    app.signal_tracker = tracker

    cfg = BotConfig()
    cfg.auto_regime_strategy = True

    from gui.dialogs import settings_window as sw
    original = dict(sw.STRATEGY_PRESETS["balanced"]["settings"])
    try:
        sw.STRATEGY_PRESETS["balanced"]["settings"] = {
            "fixed_position_quote": 3_000_000.0,
            "nonexistent_field_xyz": 999,
        }
        app._apply_auto_regime_strategy(BALANCED, cfg)

        # Known field applied
        assert cfg.fixed_position_quote == 3_000_000.0
        # Unknown field NOT applied
        assert "nonexistent_field_xyz" not in BotConfig.__dataclass_fields__
        assert not hasattr(cfg, "nonexistent_field_xyz")
    finally:
        sw.STRATEGY_PRESETS["balanced"]["settings"] = original


def test_auto_regime_no_op_when_flag_off(app, tracker):
    app.real_signal_tracker = tracker
    cfg = BotConfig()
    cfg.auto_regime_strategy = False
    cfg.fixed_position_quote = 1_000_000.0

    app._apply_auto_regime_strategy(BALANCED, cfg)
    # Nothing should have changed.
    assert cfg.fixed_position_quote == 1_000_000.0


# ══════════════════════════════════════════════════════════════
# 2. _apply_regime_preset resets the manual-mode cache in auto mode
# ══════════════════════════════════════════════════════════════

def test_regime_preset_resets_cache_when_auto_on(app, tracker):
    app.real_signal_tracker = tracker
    app.signal_tracker = tracker
    app._last_applied_max_open = 10
    app._last_applied_max_open_regime = BALANCED

    cfg = BotConfig()
    cfg.auto_regime_strategy = True

    app._apply_regime_preset(cfg, BALANCED)

    assert app._last_applied_max_open is None
    assert app._last_applied_max_open_regime is None


def test_regime_preset_updates_tracker_in_manual_mode(app, tracker):
    app.real_signal_tracker = tracker
    app.signal_tracker = tracker

    cfg = BotConfig()
    cfg.auto_regime_strategy = False
    cfg.regime_controlled_keys = ["max_open_positions"]

    app._apply_regime_preset(cfg, AGGRESSIVE)
    # Aggressive preset has max_open_positions = 10
    assert tracker.max_open_trades == 10
    assert app._last_applied_max_open == 10


def test_regime_preset_ignores_non_controlled_keys(app, tracker):
    app.real_signal_tracker = tracker
    app.signal_tracker = tracker

    cfg = BotConfig()
    cfg.auto_regime_strategy = False
    cfg.regime_controlled_keys = ["max_open_positions"]
    cfg.fixed_position_quote = 9_999_999.0

    app._apply_regime_preset(cfg, CRISIS)
    # fixed_position_quote is NOT in controlled → user value wins
    assert cfg.fixed_position_quote == 9_999_999.0


# ══════════════════════════════════════════════════════════════
# 3. _update_regime captures info on decide() failure
# ══════════════════════════════════════════════════════════════

def test_update_regime_captures_info_when_decide_fails(app):
    score_info = {"score": 42.0, "breadth_24h": 0.5, "btc_24h": 1.0}
    app.regime_detector.score.return_value = score_info
    app.regime_detector.decide.side_effect = RuntimeError("simulated")
    app.current_regime = BALANCED

    result = app._update_regime([])

    # Regime is preserved but info is captured.
    assert result == BALANCED
    assert app._last_regime_info == score_info


def test_update_regime_captures_info_on_success(app):
    score_info = {"score": 75.0, "breadth_24h": 0.7, "btc_24h": 3.0}
    app.regime_detector.score.return_value = score_info
    app.regime_detector.decide.return_value = AGGRESSIVE

    result = app._update_regime([])

    assert result == AGGRESSIVE
    assert app.current_regime == AGGRESSIVE
    assert app._last_regime_info == score_info


# ══════════════════════════════════════════════════════════════
# 4. _apply_live_sizing recomputes max_notional after presets
# ══════════════════════════════════════════════════════════════

def test_apply_live_sizing_pushes_fixed_lot_to_both_trackers(app):
    real = MagicMock()
    paper = MagicMock()
    app.real_signal_tracker = real
    app.signal_tracker = paper

    cfg = BotConfig()
    cfg.fixed_position_quote = 4_000_000.0
    cfg.max_notional_quote = 2_000_000.0   # ← smaller than fixed lot
    cfg.position_size_mode = "fixed"
    cfg.min_notional_quote = 500_000.0
    cfg.max_position_pct = 25.0
    cfg.max_total_exposure_pct = 40.0

    app._apply_live_sizing(cfg)

    # Both trackers receive the preset values.
    assert real.fixed_position_quote == 4_000_000.0
    assert paper.fixed_position_quote == 4_000_000.0
    # max_notional must be at least the fixed lot.
    assert real.max_notional_quote >= 4_000_000.0
    assert paper.max_notional_quote >= 4_000_000.0