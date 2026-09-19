# trading/bot_config.py
"""
BotConfig v7.2.2 — Public capacity summary helper.

Changes vs v7.2.1:
- FIX v7.2.2: exposes the capacity sanity check as a PUBLIC method
  `BotConfig.capacity_summary()`.  Previously the "capacity mismatch"
  diagnostic lived only inside `load_config()` as a `logger.warning()`
  call, so the operator never saw it in the UI — a config with
  `max_open_positions=10` but capacity for only 3 positions silently
  shrank every signal past position 3.  The GUI (BotSettingsWindow)
  can now call `cfg.capacity_summary()` on every keystroke and
  display the result live.  `load_config()` was refactored to use
  the same method for its warning so there is exactly one source of
  truth for the computation.

Retained from v7.2.1:
- `load_config()` emits a warning when the configured
  `max_open_positions` cannot physically fit within the exposure cap at
  the configured `fixed_position_quote`.

Retained from v7.2.0:
- added the three auto-regime fields that were being written
  by SettingsWindow but silently dropped by asdict()/from_dict():
    * auto_regime_strategy: bool
    * regime_strategy_map: Dict[str, str]
    * regime_controlled_keys: List[str]
- added `max_spread_pct` and `max_market_data_age_sec` fields
  so they survive a round-trip through load_config/save_config instead of
  silently disappearing from the JSON.
"""
from __future__ import annotations
import os
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from dataclasses import dataclass, field, asdict
from cryptography.fernet import Fernet, InvalidToken

from core.config import APPDATA_DIR
from trading.execution_mode import PAPER, normalize_execution_mode

if TYPE_CHECKING:
    from signal_tracker import SignalTracker

logger = logging.getLogger(__name__)

_SOURCE_DIR = Path(__file__).resolve().parent.parent

DEFAULT_FIXED_POSITION_QUOTE = 2_000_000.0
DEFAULT_MAX_NOTIONAL_QUOTE = 8_000_000.0
DEFAULT_MIN_NOTIONAL_QUOTE = 500_000.0
DEFAULT_MAX_POSITION_PCT = 15.0
DEFAULT_MAX_TOTAL_EXPOSURE_PCT = 40.0
STRATEGY_DEFAULTS_VERSION = 9

DEFAULT_CONFIG_FILE = os.path.join(APPDATA_DIR, "bot_config.json")
_CREDENTIALS_FILE = os.path.join(APPDATA_DIR, "nobitex_credentials.enc")
_CREDENTIAL_KEY_FILE = os.path.join(APPDATA_DIR, "nobitex_credentials.key")
_CONFIG_LOCK = threading.Lock()


def _resolve_config_path(path: str) -> str:
    if not path:
        return DEFAULT_CONFIG_FILE
    if os.path.isabs(path):
        return path
    return os.path.join(APPDATA_DIR, path)


def _credential_key() -> bytes:
    os.makedirs(APPDATA_DIR, exist_ok=True)
    if os.path.exists(_CREDENTIAL_KEY_FILE):
        try:
            key = Path(_CREDENTIAL_KEY_FILE).read_bytes().strip()
            Fernet(key)
            return key
        except Exception:
            logger.warning("Nobitex credential key is invalid; generating a new one.")
    key = Fernet.generate_key()
    Path(_CREDENTIAL_KEY_FILE).write_bytes(key)
    return key


def _load_credentials() -> tuple[str, str]:
    try:
        if not os.path.exists(_CREDENTIALS_FILE):
            return "", ""
        payload = Fernet(_credential_key()).decrypt(Path(_CREDENTIALS_FILE).read_bytes())
        data = json.loads(payload.decode("utf-8"))
        return str(data.get("api_key", "") or ""), str(data.get("api_secret", "") or "")
    except (InvalidToken, ValueError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load stored exchange credentials: %s", exc)
        return "", ""


def _save_credentials(api_key: str, api_secret: str) -> None:
    os.makedirs(APPDATA_DIR, exist_ok=True)
    if not api_key and not api_secret:
        try:
            if os.path.exists(_CREDENTIALS_FILE):
                os.remove(_CREDENTIALS_FILE)
        except OSError:
            pass
        return
    payload = json.dumps({"api_key": api_key or "", "api_secret": api_secret or ""}).encode("utf-8")
    encrypted = Fernet(_credential_key()).encrypt(payload)
    tmp = f"{_CREDENTIALS_FILE}.{uuid.uuid4().hex[:8]}.tmp"
    Path(tmp).write_bytes(encrypted)
    os.replace(tmp, _CREDENTIALS_FILE)


@dataclass
class BotConfig:
    # ── Exchange ─────────────────────────────────────────────
    exchange: str = "nobitex"
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False
    spot_mode: bool = True

    # ── Nobitex Specific ────────────────────────────────────
    nobitex_market: str = "IRT"

    # ── Trading Pairs / Strategy ─────────────────────────────
    trading_pairs: List[str] = field(default_factory=lambda: ["BTCIRT", "ETHIRT"])
    quote_currency: str = "IRT"
    quote_unit: str = "rial"
    candle_interval: str = "15m"
    kline_limit: int = 120

    # ── Account / Risk ───────────────────────────────────────
    account_balance: float = 10_000_000.0
    risk_per_trade_pct: float = 0.75
    max_open_positions: int = 3
    max_drawdown_percent: float = 10.0
    halt_on_max_drawdown: bool = True

    # ── Pure Price Action / Real Movement Strategy ──────────
    pump_threshold_pct: float = 1.5
    movement_lookback_scans: int = 4
    stop_loss_pct: float = 2.2
    trailing_distance_pct: float = 1.2
    trailing_activation_pct: float = 0.8
    trailing_stop_enabled: bool = True
    take_profit_percent: float = 0.0

    # ── Position Sizing ──────────────────────────────────────
    position_size_mode: str = "risk_percent"
    fixed_position_quote: float = DEFAULT_FIXED_POSITION_QUOTE
    max_position_pct: float = DEFAULT_MAX_POSITION_PCT
    min_notional_quote: float = DEFAULT_MIN_NOTIONAL_QUOTE
    max_notional_quote: float = DEFAULT_MAX_NOTIONAL_QUOTE
    max_total_exposure_pct: float = DEFAULT_MAX_TOTAL_EXPOSURE_PCT

    # ── Filters ─────────────────────────────────────────────
    min_volume_24h: float = 300_000_000.0
    min_market_cap: float = 0.0

    # ── Cooldowns ────────────────────────────────────────────
    cooldown_after_loss_min: int = 60
    cooldown_after_win_min: int = 15
    entry_cooldown_seconds: int = 180

    # ── Automation / Notifications ───────────────────────────
    check_interval_seconds: int = 10
    enable_auto_trading: bool = True
    execution_mode: str = PAPER
    trading_fee_pct: float = 0.1
    max_new_entries_per_cycle: int = 1

    # ── Portfolio reconciliation ─────────────────────────────
    # Exchange wallet is reconciled periodically; order mutations also
    # trigger an immediate post-trade refresh.
    portfolio_reconcile_every_scans: int = 3
    portfolio_reconcile_min_interval_seconds: int = 30

    # ── Entry confirmation ───────────────────────────────────
    confirmation_enabled: bool = False
    confirmation_pct: float = 0.4
    confirmation_max_minutes: int = 5
    invalidation_pct: float = 0.8
    max_chase_pct: float = 0.6
    min_quality: float = 0.4
    blocked_risk_levels: List[str] = field(default_factory=lambda: ["High", "Extreme"])
    reverse_signal_exit_enabled: bool = True
    use_risk_filter: bool = True

    # ── Real-movement trend follow (Nobitex-only) ────────────
    strategy: str = "nobitex_momentum"
    global_signal_source: str = "Nobitex"
    min_volume_irt: float = 300_000_000.0
    max_nobitex_spread_pct: float = 0.9
    max_spread_pct: float = 1.2
    max_local_fall_pct: float = 0.5
    min_confirm_scans: int = 1
    min_observed_move_pct: float = 1.0
    max_local_24h_pct: float = 15.0
    btc_max_dump_pct: float = 2.5
    min_ask_depth_quote: float = 3_000_000.0
    max_local_premium_pct: float = 0.0
    max_market_data_age_sec: float = 300.0

    # ── Eagle Exception (BTC dump bypass) ────────────────────
    btc_dump_exception_enabled: bool = True
    eagle_min_observed_move_pct: float = 2.5
    eagle_min_1h_pct: float = 2.0
    eagle_min_volume_irt: float = 300_000_000.0
    eagle_max_spread_pct: float = 0.9

    # ── Auto-regime strategy ─────────────────────────────────
    auto_regime_strategy: bool = False
    regime_strategy_map: Dict[str, str] = field(
        default_factory=lambda: {
            "AGGRESSIVE":   "aggressive",
            "BALANCED":     "balanced",
            "CONSERVATIVE": "conservative",
            "CRISIS":       "crisis",
        }
    )
    regime_controlled_keys: List[str] = field(
        default_factory=lambda: ["max_open_positions"]
    )

    strategy_defaults_version: int = STRATEGY_DEFAULTS_VERSION

    # ── File Paths ───────────────────────────────────────────
    trade_log_file: str = field(default_factory=lambda: os.path.join(APPDATA_DIR, "trade_history.json"))
    config_file: str = field(default_factory=lambda: DEFAULT_CONFIG_FILE)

    # ── Alias Properties ─────────────────────────────────────
    @property
    def stop_loss_percent(self): return self.stop_loss_pct
    @stop_loss_percent.setter
    def stop_loss_percent(self, v): self.stop_loss_pct = float(v)

    @property
    def risk_per_trade(self): return self.risk_per_trade_pct
    @risk_per_trade.setter
    def risk_per_trade(self, v): self.risk_per_trade_pct = float(v)

    @property
    def max_positions(self): return self.max_open_positions
    @max_positions.setter
    def max_positions(self, v): self.max_open_positions = int(v)

    @property
    def take_profit_pct(self): return self.take_profit_percent
    @take_profit_pct.setter
    def take_profit_pct(self, v): self.take_profit_percent = float(v)

    @property
    def max_daily_loss(self): return self.max_drawdown_percent
    @max_daily_loss.setter
    def max_daily_loss(self, v): self.max_drawdown_percent = float(v)

    # ══════════════════════════════════════════════════════════════
    # FIX v7.2.2: capacity sanity helper
    # ══════════════════════════════════════════════════════════════
    def capacity_summary(self) -> Dict[str, Any]:
        """
        Compute whether the configured `max_open_positions` can actually
        fit within the exposure cap at the configured lot size.

        With `position_size_mode == "fixed"`, the tracker opens each
        entry at exactly `fixed_position_quote`.  The total exposure is
        capped at `account_balance * max_total_exposure_pct / 100`.  If
        `max_open_positions * fixed_position_quote` exceeds the cap,
        every signal past the computed `fits` position is either
        auto-shrunk (down to `min_notional_quote`) or rejected — which
        is confusing for the operator if it happens silently.

        This method is safe to call repeatedly (it only reads the
        current dataclass attributes) and is intended to be wired into
        a live label in the Bot Settings window.

        Returns
        -------
        dict with keys:
            fits                 : int   — how many fixed lots fit in the cap
            effective_max        : int   — min(max_open_positions, fits) or 0
            configured_max       : int   — the raw max_open_positions value
            per_trade_notional   : float — fixed_position_quote (or estimate)
            exposure_cap         : float — account_balance * pct / 100
            min_notional         : float — the configured minimum
            is_mismatch          : bool  — True when fits < configured_max
            can_fit_min_notional : bool  — True when at least one min lot fits
            mode                 : str   — "fixed" or "risk_percent"
            explanation          : str   — human-readable one-liner (empty when OK)
        """
        try:
            account = float(self.account_balance or 0.0)
        except (TypeError, ValueError):
            account = 0.0
        try:
            exposure_pct = float(self.max_total_exposure_pct or 0.0)
        except (TypeError, ValueError):
            exposure_pct = 0.0
        try:
            per_trade = float(self.fixed_position_quote or 0.0)
        except (TypeError, ValueError):
            per_trade = 0.0
        try:
            min_notional = float(self.min_notional_quote or 0.0)
        except (TypeError, ValueError):
            min_notional = 0.0
        try:
            configured_max = int(self.max_open_positions or 0)
        except (TypeError, ValueError):
            configured_max = 0

        mode = str(self.position_size_mode or "fixed").strip().lower()

        exposure_cap = max(0.0, account * exposure_pct / 100.0)

        # For risk_percent mode we cannot know the true per-trade size
        # without knowing each symbol's stop distance, so we only
        # report a rough upper bound using the fixed lot as a proxy.
        # The mismatch check is only meaningful in fixed mode.
        is_mismatch = False
        fits = 0
        explanation = ""

        if mode == "fixed" and per_trade > 0 and exposure_cap > 0:
            fits = int(exposure_cap // per_trade)
        elif exposure_cap <= 0:
            fits = 0
        else:
            # risk_percent or unknown mode — no meaningful per-trade
            # estimate available.  Report "fits = configured_max" so
            # the UI does not flag a false mismatch.
            fits = configured_max

        effective_max = min(configured_max, fits) if fits > 0 else 0
        can_fit_min = (
            min_notional <= 0
            or exposure_cap >= min_notional
        )

        if mode == "fixed" and per_trade > 0 and exposure_cap > 0:
            if configured_max > fits:
                is_mismatch = True
                if fits <= 0:
                    explanation = (
                        f"No position can fit: lot {per_trade:,.0f} > "
                        f"exposure cap {exposure_cap:,.0f} "
                        f"({exposure_pct:.0f}% of {account:,.0f})."
                    )
                else:
                    explanation = (
                        f"Only ~{fits} position(s) fit at "
                        f"{per_trade:,.0f} within {exposure_pct:.0f}% of "
                        f"{account:,.0f}. Signals past position {fits} "
                        f"will be auto-shrunk or rejected "
                        f"(min_notional={min_notional:,.0f})."
                    )

        return {
            "fits":                 int(fits),
            "effective_max":        int(effective_max),
            "configured_max":       int(configured_max),
            "per_trade_notional":   float(per_trade),
            "exposure_cap":         float(exposure_cap),
            "min_notional":         float(min_notional),
            "is_mismatch":          bool(is_mismatch),
            "can_fit_min_notional": bool(can_fit_min),
            "mode":                 mode,
            "explanation":          explanation,
        }

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.pop("config_file", None)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotConfig":
        if not data:
            return cls()
        d = dict(data)
        _ALIASES = {
            "stop_loss_percent": "stop_loss_pct",
            "risk_per_trade": "risk_per_trade_pct",
            "max_positions": "max_open_positions",
            "max_open_trades": "max_open_positions",
            "take_profit_pct": "take_profit_percent",
            "max_daily_loss": "max_drawdown_percent",
        }
        for alias, real in _ALIASES.items():
            if alias in d:
                if real in d:
                    d.pop(alias, None)
                else:
                    d[real] = d.pop(alias)

        valid = {f for f in cls.__dataclass_fields__}
        filtered = {k: v for k, v in d.items() if k in valid}

        _int_fields = {
            "max_open_positions", "kline_limit", "movement_lookback_scans",
            "check_interval_seconds", "entry_cooldown_seconds",
            "cooldown_after_loss_min", "cooldown_after_win_min",
            "max_new_entries_per_cycle", "confirmation_max_minutes",
            "market_scan_limit", "min_confirm_scans", "strategy_defaults_version",
        }
        _float_fields = {
            "account_balance", "risk_per_trade_pct", "stop_loss_pct",
            "max_drawdown_percent", "fixed_position_quote", "max_position_pct",
            "min_notional_quote", "max_notional_quote", "max_total_exposure_pct",
            "min_volume_24h", "min_market_cap", "pump_threshold_pct",
            "trailing_distance_pct", "trailing_activation_pct",
            "take_profit_percent", "trading_fee_pct", "confirmation_pct",
            "invalidation_pct", "max_chase_pct", "min_quality",
            "min_nobitex_discount_pct", "max_nobitex_discount_pct",
            "max_nobitex_spread_pct", "max_spread_pct",
            "min_volume_irt", "max_market_data_age_sec",
            "eagle_min_observed_move_pct", "eagle_min_1h_pct",
            "eagle_min_volume_irt", "eagle_max_spread_pct",
            "min_observed_move_pct", "max_local_24h_pct", "btc_max_dump_pct",
            "min_ask_depth_quote", "max_local_premium_pct", "max_local_fall_pct",
        }
        _bool_fields = {
            "testnet", "spot_mode", "halt_on_max_drawdown", "enable_auto_trading",
            "trailing_stop_enabled", "confirmation_enabled",
            "reverse_signal_exit_enabled", "use_risk_filter",
            "btc_dump_exception_enabled",
            "auto_regime_strategy",
        }

        for k in _int_fields:
            if k in filtered:
                try:
                    filtered[k] = int(filtered[k])
                except Exception:
                    pass
        for k in _float_fields:
            if k in filtered:
                try:
                    filtered[k] = float(filtered[k])
                except Exception:
                    pass
        for k in _bool_fields:
            if k in filtered:
                v = filtered[k]
                if isinstance(v, str):
                    filtered[k] = v.strip().lower() in ("1", "true", "yes", "on")
                else:
                    filtered[k] = bool(v)

        # ── List-typed fields (comma-separated fallback) ──────
        _list_fields = {
            "blocked_risk_levels": ("High", "Extreme"),
            "regime_controlled_keys": ("max_open_positions",),
            "trading_pairs": ("BTCIRT", "ETHIRT"),
        }
        for key, default in _list_fields.items():
            if key in filtered and not isinstance(filtered[key], list):
                raw = filtered.get(key)
                if raw in (None, ""):
                    filtered[key] = list(default)
                else:
                    filtered[key] = [
                        x.strip() for x in str(raw).split(",") if x.strip()
                    ]

        # ── Dict-typed field: regime_strategy_map ─────────────
        if "regime_strategy_map" in filtered:
            raw = filtered["regime_strategy_map"]
            if not isinstance(raw, dict):
                filtered["regime_strategy_map"] = {
                    "AGGRESSIVE":   "aggressive",
                    "BALANCED":     "balanced",
                    "CONSERVATIVE": "conservative",
                    "CRISIS":       "crisis",
                }

        if "execution_mode" in filtered:
            filtered["execution_mode"] = normalize_execution_mode(filtered["execution_mode"])

        return cls(**filtered)


_BOTCONFIG_INIT = BotConfig.__init__
_BOTCONFIG_INIT_ALIASES = {
    "risk_per_trade": "risk_per_trade_pct",
    "stop_loss_percent": "stop_loss_pct",
    "take_profit_pct": "take_profit_percent",
    "max_positions": "max_open_positions",
    "max_daily_loss": "max_drawdown_percent",
    "max_open_trades": "max_open_positions",
}


def _botconfig_init(self, *args, **kwargs):
    for alias, real in _BOTCONFIG_INIT_ALIASES.items():
        if alias in kwargs:
            if real not in kwargs:
                kwargs[real] = kwargs.pop(alias)
            else:
                kwargs.pop(alias, None)
    _BOTCONFIG_INIT(self, *args, **kwargs)


BotConfig.__init__ = _botconfig_init  # type: ignore[method-assign]


TREND_EV_DEFAULTS: Dict[str, Any] = {
    "strategy_defaults_version": 2,
    "pump_threshold_pct": 1.5,
    "min_observed_move_pct": 1.0,
    "max_nobitex_spread_pct": 1.0,
    "min_volume_irt": 500_000.0,
    "min_volume_24h": 500_000.0,
    "max_chase_pct": 0.7,
    "btc_max_dump_pct": 0.7,
    "max_local_24h_pct": 8.0,
    "movement_lookback_scans": 4,
    "min_confirm_scans": 1,
    "stop_loss_pct": 2.0,
    "trailing_distance_pct": 1.5,
    "trailing_activation_pct": 0.5,
    "take_profit_percent": 6.0,
    "confirmation_enabled": True,
    "max_open_positions": 3,
    "max_new_entries_per_cycle": 1,
    "cooldown_after_win_min": 15,
}

POSITION_SIZE_DEFAULTS: Dict[str, Any] = {
    "strategy_defaults_version": 3,
    "position_size_mode": "risk_percent",
    "fixed_position_quote": DEFAULT_FIXED_POSITION_QUOTE,
    "max_notional_quote": DEFAULT_MAX_NOTIONAL_QUOTE,
    "min_notional_quote": DEFAULT_MIN_NOTIONAL_QUOTE,
    "max_position_pct": DEFAULT_MAX_POSITION_PCT,
    "max_total_exposure_pct": DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
}

EARLY_TREND_DEFAULTS: Dict[str, Any] = {
    "strategy_defaults_version": 4,
    "pump_threshold_pct": 1.5,
    "min_observed_move_pct": 1.0,
    "max_nobitex_spread_pct": 1.0,
    "movement_lookback_scans": 4,
    "min_confirm_scans": 1,
    "stop_loss_pct": 2.0,
    "trailing_distance_pct": 1.5,
    "trailing_activation_pct": 0.5,
    "take_profit_percent": 6.0,
    "max_chase_pct": 0.7,
    "max_local_24h_pct": 8.0,
    "confirmation_enabled": True,
    "entry_cooldown_seconds": 900,
    "check_interval_seconds": 20,
}

WINNER_LOCK_DEFAULTS: Dict[str, Any] = {
    "strategy_defaults_version": 5,
    "trailing_distance_pct": 1.5,
    "trailing_activation_pct": 0.5,
}

PROFITABILITY_DEFAULTS: Dict[str, Any] = {
    "strategy_defaults_version": 6,
    "btc_max_dump_pct": 0.7,
    "risk_per_trade_pct": 1.0,
    "max_drawdown_percent": 15.0,
    "max_open_positions": 3,
    "min_notional_quote": DEFAULT_MIN_NOTIONAL_QUOTE,
    "max_notional_quote": DEFAULT_MAX_NOTIONAL_QUOTE,
    "use_risk_filter": True,
    "min_ask_depth_quote": 5_000_000.0,
    "cooldown_after_loss_min": 60,
}

NOBITEX_ONLY_DEFAULTS: Dict[str, Any] = {
    "strategy_defaults_version": 7,
    "strategy": "nobitex_momentum",
    "global_signal_source": "Nobitex",
}

EAGLE_DEFAULTS: Dict[str, Any] = {
    "strategy_defaults_version": 9,
    "btc_dump_exception_enabled": True,
    "eagle_min_observed_move_pct": 2.5,
    "eagle_min_1h_pct": 2.0,
    "eagle_min_volume_irt": 300_000_000.0,
    "eagle_max_spread_pct": 0.9,
    "btc_max_dump_pct": 2.5,
}


# ══════════════════════════════════════════════════════════════
# Tracker sync helper
# ══════════════════════════════════════════════════════════════

def apply_to_tracker(
    cfg: "BotConfig",
    tracker: "SignalTracker",
    *,
    is_live_exchange: bool = False,
) -> None:
    """Copy every relevant field from BotConfig into a SignalTracker."""
    if cfg is None or tracker is None:
        return

    tracker.pump_threshold_pct = float(
        cfg.min_observed_move_pct if is_live_exchange else cfg.pump_threshold_pct
    )
    tracker.stop_loss_pct = float(cfg.stop_loss_pct)
    tracker.trailing_distance_pct = float(cfg.trailing_distance_pct)
    tracker.trailing_activation_pct = float(cfg.trailing_activation_pct)
    tracker.trailing_stop_enabled = bool(cfg.trailing_stop_enabled)
    tracker.take_profit_percent = float(cfg.take_profit_percent)
    tracker.trading_fee_pct = float(cfg.trading_fee_pct)

    tracker.risk_per_trade_pct = float(cfg.risk_per_trade_pct)
    tracker.max_open_trades = int(cfg.max_open_positions)
    tracker.max_drawdown_percent = float(cfg.max_drawdown_percent)
    tracker.max_total_exposure_pct = float(cfg.max_total_exposure_pct)
    tracker.max_position_pct = float(cfg.max_position_pct)
    tracker.max_notional_quote = max(
        float(cfg.max_notional_quote),
        float(cfg.fixed_position_quote),
    )
    tracker.min_notional_quote = float(cfg.min_notional_quote)
    tracker.fixed_position_quote = float(cfg.fixed_position_quote)
    tracker.position_size_mode = str(cfg.position_size_mode or "fixed").lower()

    tracker.min_volume_24h = float(cfg.min_volume_24h)
    tracker.min_market_cap = float(cfg.min_market_cap)
    tracker.use_risk_filter = bool(cfg.use_risk_filter)
    tracker.blocked_risk_levels = list(cfg.blocked_risk_levels or ["High", "Extreme"])
    tracker.min_quality = float(cfg.min_quality)

    tracker.max_new_entries_per_cycle = int(cfg.max_new_entries_per_cycle)
    tracker.cooldown_after_loss_min = int(cfg.cooldown_after_loss_min)
    tracker.cooldown_after_win_min = int(cfg.cooldown_after_win_min)
    tracker.entry_cooldown_seconds = int(cfg.entry_cooldown_seconds)

    tracker.confirmation_enabled = bool(cfg.confirmation_enabled)
    tracker.confirmation_pct = float(cfg.confirmation_pct)
    tracker.confirmation_max_minutes = int(cfg.confirmation_max_minutes)
    tracker.invalidation_pct = float(cfg.invalidation_pct)
    tracker.max_chase_pct = float(cfg.max_chase_pct)

    if hasattr(tracker, "quote_currency"):
        tracker.quote_currency = str(cfg.quote_currency or "IRT").upper()

    if not is_live_exchange and hasattr(tracker, "account_balance"):
        tracker.account_balance = float(cfg.account_balance)
        tracker.initial_balance = float(cfg.account_balance)
        tracker.cash = float(cfg.account_balance)
        tracker.peak_equity = float(cfg.account_balance)


# ══════════════════════════════════════════════════════════════
# Load / Save
# ══════════════════════════════════════════════════════════════

def load_config(file_path: str = DEFAULT_CONFIG_FILE) -> BotConfig:
    path = _resolve_config_path(file_path)
    if not os.path.exists(path):
        cfg = BotConfig()
        cfg.config_file = path
        return cfg

    with _CONFIG_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}

            stored_key, stored_secret = _load_credentials()
            if stored_key or stored_secret:
                data["api_key"] = stored_key
                data["api_secret"] = stored_secret
            else:
                data["api_key"] = ""
                data["api_secret"] = ""

            data.pop("api_key_encrypted", None)
            data.pop("api_secret_encrypted", None)

            version = int(data.get("strategy_defaults_version") or 0)
            if version < 2:
                data.update(TREND_EV_DEFAULTS)
            if version < 3:
                data.update(POSITION_SIZE_DEFAULTS)
            if version < 4:
                data.update(EARLY_TREND_DEFAULTS)
            if version < 5:
                data.update(WINNER_LOCK_DEFAULTS)
            if version < 6:
                data.update(PROFITABILITY_DEFAULTS)
            if version < 7:
                data.update(NOBITEX_ONLY_DEFAULTS)
            if version < 8:
                for k, v in EAGLE_DEFAULTS.items():
                    data.setdefault(k, v)

            cfg = BotConfig.from_dict(data)
            cfg.execution_mode = normalize_execution_mode(
                getattr(cfg, "execution_mode", PAPER)
            )
            if str(cfg.exchange).strip().lower() == "nobitex":
                market = str(getattr(cfg, "nobitex_market", "") or "").strip().upper()
                quote = str(getattr(cfg, "quote_currency", "") or "").strip().upper()
                if market in ("", "RLS"):
                    market = "IRT"
                if market == "USDT" and quote == "USDT":
                    market = "IRT"
                    quote = "IRT"
                elif market == "IRT":
                    quote = "IRT"
                cfg.nobitex_market = market
                cfg.quote_currency = quote

            # ── FIX v7.2.2: capacity sanity warning ──────────────
            # The computation lives on BotConfig.capacity_summary() so
            # the GUI can display the same numbers live.  Here we only
            # log it once when the config is loaded.
            try:
                capacity = cfg.capacity_summary()
                if capacity.get("is_mismatch"):
                    logger.warning(
                        "Config capacity mismatch: max_open_positions=%d but "
                        "only ~%d positions fit at fixed_position_quote=%.0f "
                        "within %.1f%% of account_balance=%.0f. "
                        "Signals past position %d will be auto-shrunk or "
                        "rejected (min_notional=%.0f).",
                        cfg.max_open_positions,
                        capacity["fits"],
                        cfg.fixed_position_quote,
                        cfg.max_total_exposure_pct,
                        cfg.account_balance,
                        capacity["fits"],
                        cfg.min_notional_quote,
                    )
            except (TypeError, ValueError, ZeroDivisionError):
                # Never let the diagnostic break config loading.
                pass

            cfg.config_file = path
            return cfg
        except Exception as e:
            logger.error("Error loading bot config (%s): %s — using defaults.", path, e)
            return BotConfig()


def save_config(config: BotConfig, file_path: str = DEFAULT_CONFIG_FILE) -> bool:
    cfg = config or BotConfig()
    path = _resolve_config_path(file_path)

    with _CONFIG_LOCK:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = cfg.to_dict()
            _save_credentials(
                str(data.pop("api_key", "") or ""),
                str(data.pop("api_secret", "") or ""),
            )

            tmp_name = f"{path}.{threading.get_ident()}.{uuid.uuid4().hex[:6]}.tmp"
            with open(tmp_name, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
            return True
        except Exception as e:
            logger.error("Error saving bot config (%s): %s", path, e)
            return False


def validate_config(config: BotConfig) -> List[str]:
    errors: List[str] = []
    c = config or BotConfig()

    if not (0 < c.risk_per_trade_pct <= 100):
        errors.append("risk_per_trade_pct must be between 0 and 100.")
    if c.portfolio_reconcile_every_scans < 1:
        errors.append("portfolio_reconcile_every_scans must be >= 1.")
    if c.portfolio_reconcile_min_interval_seconds < 10:
        errors.append("portfolio_reconcile_min_interval_seconds must be >= 10.")
    if c.max_open_positions < 1:
        errors.append("max_open_positions must be >= 1.")
    if c.stop_loss_pct <= 0:
        errors.append("stop_loss_pct must be > 0.")
    if c.max_drawdown_percent <= 0:
        errors.append("max_drawdown_percent must be > 0.")
    if c.min_notional_quote > c.fixed_position_quote and c.position_size_mode == "fixed":
        errors.append("min_notional_quote must not exceed fixed_position_quote.")
    if c.eagle_min_observed_move_pct <= 0:
        errors.append("eagle_min_observed_move_pct must be > 0.")
    if c.eagle_min_1h_pct < 0:
        errors.append("eagle_min_1h_pct must be >= 0.")
    if c.eagle_min_volume_irt < 0:
        errors.append("eagle_min_volume_irt must be >= 0.")
    if c.eagle_max_spread_pct <= 0:
        errors.append("eagle_max_spread_pct must be > 0.")
    return errors


__all__ = [
    "BotConfig",
    "load_config",
    "save_config",
    "validate_config",
    "apply_to_tracker",
    "DEFAULT_CONFIG_FILE",
    "DEFAULT_FIXED_POSITION_QUOTE",
    "DEFAULT_MAX_NOTIONAL_QUOTE",
    "DEFAULT_MIN_NOTIONAL_QUOTE",
    "DEFAULT_MAX_POSITION_PCT",
    "DEFAULT_MAX_TOTAL_EXPOSURE_PCT",
    "STRATEGY_DEFAULTS_VERSION",
    "TREND_EV_DEFAULTS",
    "POSITION_SIZE_DEFAULTS",
    "EARLY_TREND_DEFAULTS",
    "WINNER_LOCK_DEFAULTS",
    "PROFITABILITY_DEFAULTS",
    "NOBITEX_ONLY_DEFAULTS",
    "EAGLE_DEFAULTS",
]