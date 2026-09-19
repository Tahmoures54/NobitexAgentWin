"""
Tests for BotConfig auto-regime persistence (v7.2.0 fix).

Bug being covered:
    Previously `SettingsWindow._save_and_close()` set
    `cfg.auto_regime_strategy` on the dataclass instance, but since
    that field was NOT declared on BotConfig, `asdict()` in to_dict()
    silently dropped it.  A restart of the app reset the toggle to
    False and the whole auto-regime feature was effectively dead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from trading import bot_config as bc
from trading.bot_config import BotConfig, load_config, save_config


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def temp_config_dir(tmp_path, monkeypatch):
    """Redirect all BotConfig file paths to a temp directory."""
    config_path = str(tmp_path / "bot_config.json")
    creds_path = str(tmp_path / "nobitex_credentials.enc")
    key_path = str(tmp_path / "nobitex_credentials.key")

    monkeypatch.setattr(bc, "DEFAULT_CONFIG_FILE", config_path)
    monkeypatch.setattr(bc, "_CREDENTIALS_FILE", creds_path)
    monkeypatch.setattr(bc, "_CREDENTIAL_KEY_FILE", key_path)

    return tmp_path, config_path


# ══════════════════════════════════════════════════════════════
# 1. Field declarations exist on the dataclass
# ══════════════════════════════════════════════════════════════

def test_new_fields_are_declared_on_botconfig():
    """All auto-regime fields must exist as dataclass fields."""
    fields = set(BotConfig.__dataclass_fields__.keys())

    for required in (
        "auto_regime_strategy",
        "regime_strategy_map",
        "regime_controlled_keys",
        "max_spread_pct",
        "max_market_data_age_sec",
    ):
        assert required in fields, f"Missing dataclass field: {required}"


# ══════════════════════════════════════════════════════════════
# 2. Round-trip: to_dict → from_dict
# ══════════════════════════════════════════════════════════════

def test_auto_regime_strategy_round_trips_through_dict():
    cfg = BotConfig()
    cfg.auto_regime_strategy = True
    cfg.regime_strategy_map = {
        "AGGRESSIVE":   "aggressive",
        "BALANCED":     "balanced",
        "CONSERVATIVE": "conservative",
        "CRISIS":       "crisis",
    }
    cfg.regime_controlled_keys = ["max_open_positions", "max_new_entries_per_cycle"]

    data = cfg.to_dict()
    assert data["auto_regime_strategy"] is True
    assert data["regime_strategy_map"]["CRISIS"] == "crisis"
    assert "max_new_entries_per_cycle" in data["regime_controlled_keys"]

    cfg2 = BotConfig.from_dict(data)
    assert cfg2.auto_regime_strategy is True
    assert cfg2.regime_strategy_map["CRISIS"] == "crisis"
    assert "max_new_entries_per_cycle" in cfg2.regime_controlled_keys


def test_max_spread_and_age_round_trip():
    cfg = BotConfig()
    cfg.max_spread_pct = 1.75
    cfg.max_market_data_age_sec = 420.0

    data = cfg.to_dict()
    assert data["max_spread_pct"] == 1.75
    assert data["max_market_data_age_sec"] == 420.0

    cfg2 = BotConfig.from_dict(data)
    assert cfg2.max_spread_pct == 1.75
    assert cfg2.max_market_data_age_sec == 420.0


# ══════════════════════════════════════════════════════════════
# 3. Type coercion in from_dict
# ══════════════════════════════════════════════════════════════

def test_bool_coercion_from_string():
    cfg = BotConfig.from_dict({"auto_regime_strategy": "true"})
    assert cfg.auto_regime_strategy is True

    cfg = BotConfig.from_dict({"auto_regime_strategy": "0"})
    assert cfg.auto_regime_strategy is False

    cfg = BotConfig.from_dict({"auto_regime_strategy": 1})
    assert cfg.auto_regime_strategy is True


def test_list_coercion_from_comma_string():
    """regime_controlled_keys may arrive as a comma-separated string."""
    cfg = BotConfig.from_dict({
        "regime_controlled_keys": "max_open_positions,max_new_entries_per_cycle"
    })
    assert isinstance(cfg.regime_controlled_keys, list)
    assert "max_open_positions" in cfg.regime_controlled_keys
    assert "max_new_entries_per_cycle" in cfg.regime_controlled_keys


def test_dict_field_coercion_falls_back_to_default():
    """A non-dict regime_strategy_map is replaced with the default."""
    cfg = BotConfig.from_dict({"regime_strategy_map": "garbage"})
    assert isinstance(cfg.regime_strategy_map, dict)
    assert cfg.regime_strategy_map["AGGRESSIVE"] == "aggressive"


# ══════════════════════════════════════════════════════════════
# 4. Real file save → load round-trip
# ══════════════════════════════════════════════════════════════

def test_save_then_load_preserves_auto_regime(temp_config_dir):
    """The exact bug: user toggles auto-regime, saves, restarts, toggle is gone."""
    _, config_path = temp_config_dir

    cfg = BotConfig()
    cfg.auto_regime_strategy = True
    cfg.regime_controlled_keys = ["max_open_positions"]
    cfg.regime_strategy_map = {
        "AGGRESSIVE": "aggressive",
        "BALANCED":   "balanced",
        "CONSERVATIVE": "conservative",
        "CRISIS":     "crisis",
    }

    assert save_config(cfg, config_path) is True

    # Verify the file itself contains the keys.
    with open(config_path, "r", encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk["auto_regime_strategy"] is True
    assert "regime_strategy_map" in on_disk
    assert "regime_controlled_keys" in on_disk

    # Reload and confirm the flag survived.
    reloaded = load_config(config_path)
    assert reloaded.auto_regime_strategy is True
    assert reloaded.regime_strategy_map["CRISIS"] == "crisis"


def test_toggle_off_persists_as_off(temp_config_dir):
    _, config_path = temp_config_dir

    cfg = BotConfig()
    cfg.auto_regime_strategy = True
    save_config(cfg, config_path)

    cfg2 = load_config(config_path)
    assert cfg2.auto_regime_strategy is True
    cfg2.auto_regime_strategy = False
    save_config(cfg2, config_path)

    cfg3 = load_config(config_path)
    assert cfg3.auto_regime_strategy is False


# ══════════════════════════════════════════════════════════════
# 5. Missing keys in JSON fall back to defaults
# ══════════════════════════════════════════════════════════════

def test_missing_auto_regime_keys_get_defaults(temp_config_dir):
    _, config_path = temp_config_dir

    # Write a JSON without any of the new fields.
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump({
            "exchange": "nobitex",
            "quote_currency": "IRT",
        }, f)

    cfg = load_config(config_path)
    assert cfg.auto_regime_strategy is False
    assert isinstance(cfg.regime_strategy_map, dict)
    assert cfg.regime_strategy_map["BALANCED"] == "balanced"
    assert "max_open_positions" in cfg.regime_controlled_keys