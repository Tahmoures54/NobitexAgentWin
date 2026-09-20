"""
Main application class for the Advanced Crypto Scanner.
Drop-in for gui/gui_main.py
Version: 7.8.5 — Gate live tracker calls with should_run_live_tracker

Changes in 7.8.5
────────────────
FIX 1 — `_live_auto_scan` now gates the `real_signal_tracker.process_new_signals`
    call with `should_run_live_tracker(plan, has_live_positions)`.  Previously
    the live tracker was called on every cycle regardless of execution mode,
    which caused:
        * a real network request to `/users/wallets/list` every cycle
        * a 401 error + warning logged every cycle when the API key was
          missing (the common case during Paper-mode runs)
        * hundreds of identical log lines per minute
    With the gate, the live tracker is:
        * always called when in LIVE mode (any state)
        * always called when there are open live positions to monitor
        * never called in PAPER mode with no live positions

Retained from v7.8.4
────────────────────
FIX 1 — `_apply_global_lead_hit` no longer writes the engine's
    short-term observed move into `1h Change (%)`.  That field must
    keep its real one-hour value for reporting, filters, and any
    downstream code (including RegimeDetector) that reads it.
    The observed short move is now published under
    `ObservedLocalMove (%)` only.  `1h Change (%)` is only filled
    when the row genuinely does not already carry a value.

Retained from v7.8.3
────────────────────
FIX 1 — `_apply_global_lead_hit` reads every key the Nobitex
    momentum engine actually writes:
      * `Nobitex Ask`  (was missing — the engine's real ask key)
      * `ObservedLocalMove (%)`, `LocalMomentum (%)`, `pump_pct`
        (fallbacks when `ObservedGlobalMove (%)` is absent)
      * `MomentumScore` (was missing — the engine's real score key)

Retained from v7.8.2:
- `open_bot_panel`, `open_bot_settings`, `open_signal_performance`
  use `UnifiedTradingWindow.get_or_create(...)`.
- `_configure_paper_tracker` resets stale peak_equity / halt when no
  open paper trades exist.
- `_on_closing` closes the singleton trading window first.

Retained from v7.8.1:
- `_apply_live_sizing(cfg)` is called after
  `_apply_auto_regime_strategy()` in the auto-regime branch.
- `_last_applied_regime_strategy` reset when auto mode is OFF.
- `_apply_auto_regime_strategy()` validates every preset field
  against `BotConfig.__dataclass_fields__` before setattr.
- `_update_regime()` captures partial info even when `decide()`
  raises.

Retained from v7.8.0:
- `_apply_auto_regime_strategy()` applied in memory only
  (bot_config.json is never overwritten by a regime switch).
- `_apply_regime_preset()` only overwrites `regime_controlled_keys`.
- `[REGIME] max_open_trades → N (regime=...)` change-only log line.
"""
from __future__ import annotations
import matplotlib
import os as _os
try:
    matplotlib.use("TkAgg")
except ImportError:
    matplotlib.use("Agg")

import configparser
import json
import logging
import os
import threading
import time
import webbrowser
from datetime import datetime
from tkinter.filedialog import asksaveasfilename
from typing import Any, Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import tkinter as tk
from PIL import Image, ImageTk
from tkinter import messagebox, ttk

from core.config import (
    APP_VERSION, CATEGORIES, SIGNAL_OPTIONS, DEFAULT_ADV_LIMIT,
    API_KEY_FILE, CONFIG_FILE, CACHE_FILE, CACHE_EXPIRY_MIN,
    TRIAL_DAYS, DAILY_FREE_REFRESH_LIMIT, REFRESH_INTERVAL_MS,
    APPDATA_DIR,
)
from core.encryption import get_device_id
from core import user_status
from core.utils import (
    resource_path, LRUCache, fmt, fmt_mcap, fmt_volume, safe_float, make_pair,
)

from api.api_tronscan import TronscanClient

from gui.ui_theme import ModernTheme
from gui.ui_factory import UIFactory
from gui.gui_helpers import center_window

from gui.dialogs.note_window import NoteWindow
from gui.dialogs.settings_window import SettingsWindow
from gui.dialogs.premium_window import PremiumWindow

from signal_tracker import SignalTracker
from trading.trader import TradingBot
from trading.bot_config import (
    DEFAULT_FIXED_POSITION_QUOTE,
    DEFAULT_MAX_NOTIONAL_QUOTE,
    DEFAULT_MAX_POSITION_PCT,
    DEFAULT_MAX_TOTAL_EXPOSURE_PCT,
    DEFAULT_MIN_NOTIONAL_QUOTE,
    load_config,
    save_config,
)
from trading.execution_mode import (
    LIVE, PAPER, cycle_plan, normalize_execution_mode,
    should_run_live_tracker,
)
from trading.nobitex_momentum_engine import NobitexMomentumEngine
from trading.regime_detector import (
    RegimeDetector,
    REGIME_PRESETS,
    AGGRESSIVE,
    BALANCED,
    CONSERVATIVE,
    CRISIS,
)


logger = logging.getLogger(__name__)

_REAL_DB_FILENAMES = ("real_trades.db", "signal_log_real.db")


class CryptoScannerApp:
    APP_VERSION = APP_VERSION
    CATEGORIES = CATEGORIES
    SIGNAL_OPTIONS = SIGNAL_OPTIONS

    _COLS_FULL = [
        "#", "Rank", "Name", "Symbol", "Price", "1h %", "24h %", "7d %",
        "Pullback %", "RSI", "MACD", "BB Width %", "Stoch %K", "Stoch %D",
        "ADX", "Tx Volume", "Active Addr", "Turnover %", "Market Cap",
        "Signal", "Risk", "AI Win %", "BOT", "LINK", "TV",
    ]
    _COLS_SIMPLE = [
        "#", "Rank", "Name", "Symbol", "Price", "24h %",
        "Market Cap", "Signal", "Risk", "AI Win %", "BOT", "LINK", "TV",
    ]
    _COL_WIDTHS = {
        "#": 50, "Rank": 60, "Name": 140, "Symbol": 80, "Price": 100,
        "1h %": 70, "24h %": 70, "7d %": 70, "Pullback %": 80,
        "RSI": 60, "MACD": 80, "BB Width %": 80, "Stoch %K": 70,
        "Stoch %D": 70, "ADX": 60, "Tx Volume": 90, "Active Addr": 90,
        "Turnover %": 80, "Market Cap": 110, "Signal": 100, "Risk": 80,
        "AI Win %": 85, "BOT": 70, "LINK": 60, "TV": 50,
    }

    # ────────────────────────────────────────────────────────────
    # Regime override policy (manual mode only)
    #
    # By default, ONLY `max_open_positions` changes with market regime.
    # Everything else the user sets in bot_config.json wins.
    #
    # When `auto_regime_strategy` is True in bot_config.json, the whole
    # strategy is dictated by the regime (see _apply_auto_regime_strategy).
    # ────────────────────────────────────────────────────────────
    _REGIME_CONTROLLED_KEYS_DEFAULT = ("max_open_positions",)

    # Fallback mapping if the user removes regime_strategy_map from cfg.
    _DEFAULT_REGIME_STRATEGY_MAP = {
        "AGGRESSIVE":   "aggressive",
        "BALANCED":     "balanced",
        "CONSERVATIVE": "conservative",
        "CRISIS":       "crisis",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.theme = ModernTheme()
        self.ui_factory = UIFactory(self)

        self.api_key: str = self._load_api_key()
        self.data_df: pd.DataFrame = pd.DataFrame()
        self.filtered_df: pd.DataFrame = pd.DataFrame()
        self.latest_signals: List[Dict[str, Any]] = []
        self.last_global_metrics: Optional[Dict] = None

        self.trading_bot = None
        self.trading_bot_lock = threading.RLock()
        self._bot_quote_cache: Optional[str] = None
        self._bot_cfg = None

        self._bot_config_path = os.path.join(APPDATA_DIR, "bot_config.json")

        self._refresh_lock = threading.Lock()
        self._refresh_in_progress: bool = False
        self._pending_refresh: bool = False
        self._real_scan_lock = threading.Lock()
        self._real_auto_job: Optional[str] = None

        # Portfolio reconciliation is deliberately slower than the market
        # scan. Order mutations still trigger an immediate reconciliation.
        self._portfolio_scan_count = 0
        self._last_portfolio_sync = 0.0
        self._portfolio_sync_status = "never"
        self._last_portfolio_snapshot: Optional[Dict[str, Any]] = None

        self.enable_advanced_var = tk.BooleanVar(value=False)
        self.enable_risk_var = tk.BooleanVar(value=True)
        self.simple_mode_var = tk.BooleanVar(value=False)
        self.auto_refresh_var = tk.BooleanVar(value=True)
        self.api_source_var = tk.StringVar(value="Nobitex")
        self.adv_limit_var = tk.IntVar(value=DEFAULT_ADV_LIMIT)
        self.email_alert_var = tk.StringVar(value="")

        self._load_settings()

        self.history_cache = LRUCache(maxsize=200)
        self.device_id = get_device_id()
        self.user_status = self._load_user_status()

        from core.user_manager import UserManager
        self.user_manager = UserManager(device_id=self.device_id)

        self.signal_tracker = SignalTracker()
        self.signal_tracker.auto_trading_enabled = False
        self.real_signal_tracker = None
        self.real_auto_enabled = False
        self.execution_mode = PAPER
        self._local_history = {}
        self._local_last_scan = 0.0
        self.momentum_engine = None
        self._local_momentum_history = {}

        self.regime_detector = RegimeDetector()
        self.current_regime = BALANCED
        self._last_regime_info: Dict[str, Any] = {}

        # Cached effective max_open_trades, used only for change-detection logging.
        self._last_applied_max_open: Optional[int] = None
        self._last_applied_max_open_regime: Optional[str] = None

        # Cached effective auto-regime strategy, used for change detection.
        self._last_applied_regime_strategy: Optional[str] = None

        self._init_real_auto_trading()

        self.tron_client = TronscanClient()

        self.remaining_time = 0
        self.ticker_running = True
        self.gainers_index, self.losers_index = 0, 0
        self._fade_image_orig: Optional[Image.Image] = None
        self._fade_label: Optional[tk.Label] = None
        self._fade_photo: Optional[ImageTk.PhotoImage] = None
        self._ticker_job: Optional[str] = None
        self._timer_job: Optional[str] = None
        self._fade_job: Optional[str] = None

        self._status_messages = [
            "💡 Tip: Use filters to find hidden gems!",
            "🚀 Try the CryptoBootEn bot for automated trading.",
            "📊 Check the Paper Trading tab to practice without risk.",
            "🔔 Enable email alerts to never miss a pump.",
            "⭐ Upgrade to Premium for exclusive signals.",
            "🦅 CryptoBootEn bot: Eagle mode active — hunting strong movers.",
            "📈 Follow the market trend with our advanced indicators.",
            "💬 Need help? Open User Guide from menu.",
        ]
        self._status_index = 0
        self._status_job: Optional[str] = None

        self._setup_main_window()
        self.ui_factory.build_all()

        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.root.after(100, self._post_init)
        if self.trading_bot is not None:
            self._real_auto_job = self.root.after(5000, self._real_auto_cycle)

    def _init_real_auto_trading(self):
        """Wire the automatic signal path to the live executor."""
        try:
            cfg = load_config(self._bot_config_path)
            self._bot_cfg = cfg
            self._real_strategy_name = str(getattr(cfg, "strategy", "") or "").strip().lower()
            self.trading_bot = TradingBot.get_instance(config=cfg, auto_start=True)
            exchange_name = str(getattr(self.trading_bot, "exchange_name", "") or "").strip().lower()
            live_ready = (
                exchange_name not in ("", "simulator", "paper", "simulation")
                and bool(getattr(self.trading_bot, "execution_enabled", False))
            )
            self.execution_mode = normalize_execution_mode(getattr(cfg, "execution_mode", PAPER))
            self.real_auto_enabled = False
            self._configure_momentum_engine(cfg)
            # Apply regime-driven max_open at startup so the GUI reflects
            # the correct cap before the first scan.
            self._apply_regime_preset(cfg, self.current_regime)
            if live_ready:
                live_cash = max(float(self.trading_bot.current_balance or 0.0), 0.0)
                self.real_signal_tracker = SignalTracker(
                    account_balance=live_cash,
                    max_open_trades=int(getattr(cfg, "max_open_trades", 3)),
                    min_volume_24h=float(getattr(cfg, "min_volume_24h", 0.0)),
                    min_market_cap=0.0,
                    executor=self.trading_bot,
                    db_filename="real_trades.db",
                )
                self.real_signal_tracker.quote_currency = str(
                    getattr(self.trading_bot, "quote_currency", None)
                    or getattr(cfg, "quote_currency", "USDT")
                    or "USDT"
                ).upper()
                self.real_signal_tracker.pump_threshold_pct = 0.0
                self.real_signal_tracker.stop_loss_pct = float(getattr(cfg, "stop_loss_pct", 1.8))
                self.real_signal_tracker.trailing_distance_pct = float(getattr(cfg, "trailing_distance_pct", 1.6))
                self.real_signal_tracker.trailing_activation_pct = float(
                    getattr(cfg, "trailing_activation_pct", 0.8)
                )
                self.real_signal_tracker.trading_fee_pct = float(getattr(cfg, "trading_fee_pct", 0.1))
                self.real_signal_tracker.max_open_trades = int(getattr(cfg, "max_open_positions", 3))
                self.real_signal_tracker.max_new_entries_per_cycle = int(getattr(cfg, "max_new_entries_per_cycle", 1))
                self._apply_live_sizing(cfg, live_cash=live_cash)
                self._apply_strategy_settings(cfg)
                self.real_signal_tracker.auto_trading_enabled = True
                self.real_signal_tracker.ignore_signal_filters = True
                self.real_signal_tracker.confirmation_enabled = bool(getattr(cfg, "confirmation_enabled", False))
                self.real_signal_tracker.confirmation_pct = float(getattr(cfg, "confirmation_pct", 0.35))
                self.real_signal_tracker.confirmation_max_minutes = int(getattr(cfg, "confirmation_max_minutes", 8))
                self.real_signal_tracker.invalidation_pct = float(getattr(cfg, "invalidation_pct", 1.0))
                self.real_signal_tracker.max_chase_pct = float(getattr(cfg, "max_chase_pct", 0.55))
                self.real_signal_tracker.entry_cooldown_seconds = int(getattr(cfg, "entry_cooldown_seconds", 480))
                self.real_signal_tracker.trailing_stop_enabled = bool(getattr(cfg, "trailing_stop_enabled", True))
                self.real_signal_tracker.take_profit_percent = float(getattr(cfg, "take_profit_percent", 0.0))
                self.real_signal_tracker._sync_balance_from_executor()
                logger.info(
                    "[REAL] Live tracker ready | size=%.2f | mode=%s | entries=off until Live+Start",
                    self.real_signal_tracker.fixed_position_quote,
                    self.real_signal_tracker.position_size_mode,
                )
            else:
                logger.warning("[REAL] Automatic live trading is not ready; paper-only mode remains available.")
            self.set_execution_mode(self.execution_mode, persist=False, reason="startup")
        except Exception as exc:
            logger.error("[REAL] Auto-trading initialization failed: %s", exc, exc_info=True)
            self.real_signal_tracker = None
            self.real_auto_enabled = False
            self.execution_mode = PAPER
            try:
                self._configure_paper_tracker()
                if self.signal_tracker is not None:
                    self.signal_tracker.auto_trading_enabled = True
                    self.signal_tracker.allow_new_entries = True
            except Exception:
                pass

    def _apply_live_sizing(self, cfg, live_cash: Optional[float] = None) -> None:
        """Keep paper and real trackers on the configured quote lot size."""
        if cfg is None:
            return
        size = float(getattr(cfg, "fixed_position_quote", DEFAULT_FIXED_POSITION_QUOTE) or DEFAULT_FIXED_POSITION_QUOTE)
        min_n = float(getattr(cfg, "min_notional_quote", DEFAULT_MIN_NOTIONAL_QUOTE) or 0.0)
        max_n = max(
            float(getattr(cfg, "max_notional_quote", DEFAULT_MAX_NOTIONAL_QUOTE) or 0.0),
            size,
        )
        mode = str(getattr(cfg, "position_size_mode", "fixed") or "fixed").lower()
        max_pct = float(getattr(cfg, "max_position_pct", DEFAULT_MAX_POSITION_PCT) or DEFAULT_MAX_POSITION_PCT)
        exposure = float(
            getattr(cfg, "max_total_exposure_pct", DEFAULT_MAX_TOTAL_EXPOSURE_PCT)
            or DEFAULT_MAX_TOTAL_EXPOSURE_PCT
        )
        trackers = [self.real_signal_tracker, self.signal_tracker]
        for st in trackers:
            if st is None:
                continue
            st.position_size_mode = mode
            st.fixed_position_quote = size
            st.min_notional_quote = min_n
            st.max_notional_quote = max_n
            st.max_position_pct = max_pct
            st.max_total_exposure_pct = exposure
            if mode == "fixed":
                st.fixed_position_quote = max(st.fixed_position_quote, st.min_notional_quote)
        if live_cash is not None and self.real_signal_tracker is not None:
            self.real_signal_tracker.max_notional_quote = max(
                self.real_signal_tracker.max_notional_quote,
                self.real_signal_tracker.fixed_position_quote,
            )

    # ────────────────────────────────────────────────────────────
    # User strategy settings → both trackers (manual mode)
    # ────────────────────────────────────────────────────────────
    def _apply_strategy_settings(self, cfg) -> None:
        """Re-apply user strategy settings from cfg to both trackers.

        Called from `_live_auto_scan` in manual mode (auto_regime_strategy
        is False). Ensures changes made in Bot Settings take effect on
        the next scan without restarting the app.

        Fields handled by _apply_live_sizing and _apply_regime_preset
        are intentionally NOT touched here.
        """
        if cfg is None:
            return

        for st, is_live in (
            (self.real_signal_tracker, True),
            (self.signal_tracker, False),
        ):
            if st is None:
                continue
            try:
                # ── Stops / targets ──
                st.stop_loss_pct = float(getattr(cfg, "stop_loss_pct", st.stop_loss_pct))
                st.trailing_distance_pct = float(
                    getattr(cfg, "trailing_distance_pct", st.trailing_distance_pct)
                )
                st.trailing_activation_pct = float(
                    getattr(cfg, "trailing_activation_pct", st.trailing_activation_pct)
                )
                st.trailing_stop_enabled = bool(
                    getattr(cfg, "trailing_stop_enabled", st.trailing_stop_enabled)
                )
                st.take_profit_percent = float(
                    getattr(cfg, "take_profit_percent", st.take_profit_percent)
                )
                st.trading_fee_pct = float(
                    getattr(cfg, "trading_fee_pct", st.trading_fee_pct)
                )
                # ── Entry thresholds ──
                if is_live:
                    st.pump_threshold_pct = float(
                        getattr(cfg, "min_observed_move_pct", st.pump_threshold_pct)
                    )
                else:
                    st.pump_threshold_pct = float(
                        getattr(cfg, "pump_threshold_pct", st.pump_threshold_pct)
                    )
                # ── Risk ──
                st.risk_per_trade_pct = float(
                    getattr(cfg, "risk_per_trade_pct", st.risk_per_trade_pct)
                )
                st.max_drawdown_percent = float(
                    getattr(cfg, "max_drawdown_percent", st.max_drawdown_percent)
                )
                # ── Filters ──
                st.min_volume_24h = float(
                    getattr(cfg, "min_volume_24h", st.min_volume_24h)
                )
                st.min_market_cap = float(
                    getattr(cfg, "min_market_cap", st.min_market_cap)
                )
                st.use_risk_filter = bool(
                    getattr(cfg, "use_risk_filter", st.use_risk_filter)
                )
                blocked = getattr(cfg, "blocked_risk_levels", None)
                if isinstance(blocked, list):
                    st.blocked_risk_levels = list(blocked)
                st.min_quality = float(getattr(cfg, "min_quality", st.min_quality))
                # ── Cooldowns ──
                st.max_new_entries_per_cycle = int(
                    getattr(cfg, "max_new_entries_per_cycle", st.max_new_entries_per_cycle)
                )
                st.cooldown_after_loss_min = int(
                    getattr(cfg, "cooldown_after_loss_min", st.cooldown_after_loss_min)
                )
                st.cooldown_after_win_min = int(
                    getattr(cfg, "cooldown_after_win_min", st.cooldown_after_win_min)
                )
                st.entry_cooldown_seconds = int(
                    getattr(cfg, "entry_cooldown_seconds", st.entry_cooldown_seconds)
                )
                # ── Confirmation ──
                st.confirmation_enabled = bool(
                    getattr(cfg, "confirmation_enabled", st.confirmation_enabled)
                )
                st.confirmation_pct = float(
                    getattr(cfg, "confirmation_pct", st.confirmation_pct)
                )
                st.confirmation_max_minutes = int(
                    getattr(cfg, "confirmation_max_minutes", st.confirmation_max_minutes)
                )
                st.invalidation_pct = float(
                    getattr(cfg, "invalidation_pct", st.invalidation_pct)
                )
                st.max_chase_pct = float(
                    getattr(cfg, "max_chase_pct", st.max_chase_pct)
                )
            except Exception as exc:
                logger.debug(
                    "[SETTINGS] Could not apply strategy to %s tracker: %s",
                    "real" if is_live else "paper", exc,
                )

    # ────────────────────────────────────────────────────────────
    # Auto-regime strategy switching
    # ────────────────────────────────────────────────────────────
    def _apply_auto_regime_strategy(self, regime: str, cfg) -> None:
        """Apply the regime-mapped strategy preset to cfg and both trackers.

        No-op when `auto_regime_strategy` is False or the strategy is
        not found. Rate-limited by change detection: only logs when the
        effective strategy actually changes.

        FIX v7.8.1: validates every preset field against the dataclass
        before setattr; unknown field names are logged once per switch.
        """
        if cfg is None:
            return
        if not bool(getattr(cfg, "auto_regime_strategy", False)):
            return

        mapping = getattr(cfg, "regime_strategy_map", None)
        if not isinstance(mapping, dict) or not mapping:
            mapping = dict(self._DEFAULT_REGIME_STRATEGY_MAP)

        strategy_key = str(mapping.get(regime, "balanced")).strip().lower()

        # Profitability governor: regime still determines the initial
        # strategy, but a strategy with enough realized trades and negative
        # expectancy can be vetoed in favor of a proven positive alternative.
        strategy_alias = {
            "aggressive": "TREND_FOLLOWING",
            "trend": "TREND_FOLLOWING",
            "balanced": "MOMENTUM",
            "conservative": "MEAN_REVERSION",
            "scalping": "SCALPING",
            "crisis": "DEFENSIVE",
        }
        perf_by_tracker: Dict[str, Dict[str, Any]] = {}
        for tracker in (self.real_signal_tracker, self.signal_tracker):
            if tracker is None:
                continue
            try:
                getter = getattr(tracker, "get_strategy_performance", None)
                if callable(getter):
                    perf_by_tracker = getter(100) or {}
                    break
            except Exception as exc:
                logger.debug("[REGIME] Strategy performance unavailable: %s", exc)

        canonical = strategy_alias.get(strategy_key)
        candidate_perf = perf_by_tracker.get(canonical, {}) if canonical else {}
        candidate_n = int(candidate_perf.get("trades", 0) or 0)
        candidate_exp = float(candidate_perf.get("expectancy_pct", 0.0) or 0.0)
        min_sample = 8
        if candidate_n >= min_sample and candidate_exp < 0:
            alternatives = []
            for preset_key, canonical_name in strategy_alias.items():
                stats = perf_by_tracker.get(canonical_name, {}) or {}
                n = int(stats.get("trades", 0) or 0)
                exp = float(stats.get("expectancy_pct", 0.0) or 0.0)
                if n >= min_sample and exp > 0:
                    alternatives.append((exp, preset_key, canonical_name))
            if alternatives:
                alternatives.sort(reverse=True)
                best_exp, best_key, _ = alternatives[0]
                if best_key != strategy_key:
                    logger.warning(
                        "[REGIME] Profitability veto: %s expectancy=%.3f%%; "
                        "switching to %s expectancy=%.3f%%",
                        strategy_key, candidate_exp, best_key, best_exp,
                    )
                    strategy_key = best_key

        try:
            from gui.dialogs.settings_window import STRATEGY_PRESETS
        except Exception:
            logger.debug("[REGIME] STRATEGY_PRESETS unavailable; skipping auto-apply.")
            return

        strategy = STRATEGY_PRESETS.get(strategy_key)
        if not strategy:
            logger.warning(
                "[REGIME] Strategy '%s' not found for regime %s; skipping.",
                strategy_key, regime,
            )
            return

        valid_fields = set(getattr(type(cfg), "__dataclass_fields__", {}).keys())
        unknown_fields: List[str] = []
        known_settings: Dict[str, Any] = {}
        for field_name, value in strategy["settings"].items():
            if field_name in valid_fields:
                known_settings[field_name] = value
            else:
                unknown_fields.append(field_name)

        # Only the keys the user opted into may be rewritten.  Applying a whole
        # preset used to silently replace risk_per_trade_pct / exposure limits
        # with much more aggressive values (see PROFITABILITY_ANALYSIS.md §5).
        if not bool(getattr(cfg, "regime_auto_apply_all", False)):
            controlled = getattr(cfg, "regime_controlled_keys", None)
            if not isinstance(controlled, (list, tuple, set)) or not controlled:
                controlled = self._REGIME_CONTROLLED_KEYS_DEFAULT
            controlled = {str(k).strip() for k in controlled if k}
            kept = sorted(k for k in known_settings if k not in controlled)
            if kept:
                logger.info(
                    "[REGIME] Strategy '%s': applying %s; keeping user values for %s",
                    strategy_key, sorted(controlled), kept)
            known_settings = {k: v for k, v in known_settings.items() if k in controlled}
        if unknown_fields:
            logger.warning(
                "[REGIME] Strategy '%s' contains %d field(s) not defined on "
                "BotConfig: %s. These values will NOT be applied.",
                strategy_key, len(unknown_fields), ", ".join(sorted(unknown_fields)),
            )

        applied = 0
        for field_name, value in known_settings.items():
            try:
                setattr(cfg, field_name, value)
                applied += 1
            except Exception as exc:
                logger.debug("[REGIME] Could not set %s on cfg: %s", field_name, exc)

        for st in (self.real_signal_tracker, self.signal_tracker):
            if st is None:
                continue
            for field_name, value in known_settings.items():
                try:
                    setattr(st, field_name, value)
                except Exception as exc:
                    logger.debug(
                        "[REGIME] Could not set %s on tracker: %s", field_name, exc,
                    )
            if st is self.real_signal_tracker:
                try:
                    st.pump_threshold_pct = float(
                        getattr(cfg, "min_observed_move_pct", st.pump_threshold_pct)
                    )
                except Exception:
                    pass

        prev = getattr(self, "_last_applied_regime_strategy", None)
        if prev != strategy_key:
            logger.info(
                "[REGIME] Strategy switched to '%s' | regime=%s | %d fields",
                strategy_key, regime, applied,
            )
            self._last_applied_regime_strategy = strategy_key

    def _apply_regime_preset(self, cfg, regime: str) -> None:
        """Apply regime preset to only the keys the user opted in."""
        if cfg is None:
            return

        if bool(getattr(cfg, "auto_regime_strategy", False)):
            self._last_applied_max_open = None
            self._last_applied_max_open_regime = None
            return

        controlled = getattr(cfg, "regime_controlled_keys", None)
        if not isinstance(controlled, (list, tuple, set)) or not controlled:
            controlled = self._REGIME_CONTROLLED_KEYS_DEFAULT
        controlled = {str(k).strip() for k in controlled if k}

        preset = REGIME_PRESETS.get(regime, REGIME_PRESETS[BALANCED])

        for key, value in preset.items():
            if key not in controlled:
                logger.debug(
                    "[REGIME] Skipping %s=%s (not regime-controlled; user value=%s)",
                    key, value, getattr(cfg, key, None),
                )
                continue
            try:
                current = getattr(cfg, key, None)
                if current is None:
                    setattr(cfg, key, value)
                else:
                    setattr(cfg, key, type(current)(value))
            except (TypeError, ValueError) as exc:
                logger.debug(
                    "[REGIME] Coercion failed for %s=%s (%s); "
                    "applying preset value directly.",
                    key, value, exc,
                )
                setattr(cfg, key, value)

        if "max_open_positions" in controlled:
            try:
                max_open = max(1, int(preset.get("max_open_positions", 3)))
            except (TypeError, ValueError):
                max_open = 3

            for st in (self.real_signal_tracker, self.signal_tracker):
                if st is None:
                    continue
                try:
                    st.max_open_trades = max_open
                except Exception:
                    pass

            prev_value = getattr(self, "_last_applied_max_open", None)
            prev_regime = getattr(self, "_last_applied_max_open_regime", None)
            if max_open != prev_value or regime != prev_regime:
                logger.info(
                    "[REGIME] max_open_trades → %d (regime=%s)",
                    max_open, regime,
                )
                self._last_applied_max_open = max_open
                self._last_applied_max_open_regime = regime

        if "max_new_entries_per_cycle" in controlled and self.real_signal_tracker is not None:
            try:
                self.real_signal_tracker.max_new_entries_per_cycle = int(
                    getattr(cfg, "max_new_entries_per_cycle", 1) or 1
                )
            except (TypeError, ValueError):
                pass

    def _update_regime(self, live_rows: List[Dict[str, Any]]) -> str:
        """Score market conditions and switch regime. Returns the regime name.

        FIX v7.8.1: capture partial info when `decide()` raises so that
        `_last_regime_info` does not go stale and mislead the logs.
        """
        info: Dict[str, Any] = {}
        try:
            info = self.regime_detector.score(
                live_rows,
                tracker=self.real_signal_tracker or self.signal_tracker,
            )
        except Exception as exc:
            logger.warning("[REGIME] Detection failed, staying BALANCED: %s", exc)
            return BALANCED

        try:
            regime = self.regime_detector.decide(info)
        except Exception as exc:
            logger.warning("[REGIME] decide() failed, staying BALANCED: %s", exc)
            self._last_regime_info = info
            return self.current_regime or BALANCED

        if regime != self.current_regime:
            logger.info(
                "[REGIME] %s -> %s | score=%.1f breadth=%.2f btc24h=%.2f%% "
                "strong_movers=%d top_move=%.2f%% "
                "winrate=%.2f trades=%d dd=%.2f%%",
                self.current_regime, regime,
                info.get("score", 0.0),
                info.get("breadth", 0.0),
                info.get("btc_24h", 0.0),
                int(info.get("strong_movers_count", 0) or 0),
                info.get("top_mover_move", 0.0),
                info.get("win_rate", 0.5),
                int(info.get("trade_count", 0)),
                info.get("drawdown_pct", 0.0),
            )
        self.current_regime = regime
        self._last_regime_info = info
        return regime

    def set_execution_mode(self, mode, persist: bool = True, reason: str = "") -> str:
        """Paper XOR live. Switching to live stops paper entries; Start still required."""
        mode = normalize_execution_mode(mode)
        prev = normalize_execution_mode(getattr(self, "execution_mode", PAPER))
        self.execution_mode = mode
        if self._bot_cfg is not None:
            self._bot_cfg.execution_mode = mode
        if persist and self._bot_cfg is not None:
            try:
                save_config(self._bot_cfg, self._bot_config_path)
            except Exception as exc:
                logger.warning("Could not persist execution_mode: %s", exc)

        if mode == LIVE:
            if self.signal_tracker is not None:
                self.signal_tracker.auto_trading_enabled = False
                self.signal_tracker.allow_new_entries = False
            if self.real_signal_tracker is not None:
                self.real_signal_tracker.auto_trading_enabled = True
                self.real_signal_tracker.allow_new_entries = False
            self.real_auto_enabled = False
            logger.info(
                "[LIVE] Paper entries stopped. Live entries stay paused until Start. %s",
                reason or "",
            )
        else:
            self.real_auto_enabled = False
            if self.real_signal_tracker is not None:
                self.real_signal_tracker.allow_new_entries = False
                self.real_signal_tracker.auto_trading_enabled = True
            self._configure_paper_tracker()
            if self.signal_tracker is not None:
                self.signal_tracker.auto_trading_enabled = True
                self.signal_tracker.allow_new_entries = True
            logger.info(
                "[PAPER] Live entries stopped. Paper trading is active. %s",
                reason or "",
            )

        if prev != mode:
            logger.info("Execution mode %s -> %s | %s", prev, mode, reason or "switch")
        self._notify_execution_mode()
        self.ensure_real_auto_cycle()
        return mode

    def _notify_execution_mode(self) -> None:
        root = getattr(self, "root", None)
        if root is None:
            return
        try:
            children = list(root.winfo_children())
        except Exception:
            return
        for widget in children:
            refresh = getattr(widget, "refresh_execution_mode", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:
                    pass

    def current_cycle_plan(self) -> Dict[str, Any]:
        return cycle_plan(getattr(self, "execution_mode", PAPER), bool(self.real_auto_enabled))

    def _configure_paper_tracker(self) -> None:
        """Paper tracker shadows the engine with simulated fills and isolated cash.

        FIX v7.8.2:
          When there are no open paper trades, we now also reset
          `peak_equity`, `trading_halted`, and `halt_reason` before
          persisting.  Without this, a `peak_equity` carried over from
          a previous run (when the config had a different
          `account_balance`) produced a spurious
          `TRADING HALTED: drawdown X% >= limit Y%` on the first scan
          of the new session.
        """
        st = self.signal_tracker
        if st is None:
            return

        cfg = self._bot_cfg
        size = float(
            getattr(cfg, "fixed_position_quote", DEFAULT_FIXED_POSITION_QUOTE) or DEFAULT_FIXED_POSITION_QUOTE
        ) if cfg is not None else DEFAULT_FIXED_POSITION_QUOTE
        cash = float(getattr(cfg, "account_balance", 0.0) or 0.0) if cfg is not None else 0.0
        cash = max(cash, 10_000.0)

        st.quote_currency = str(getattr(cfg, "quote_currency", "USDT") or "USDT").upper() if cfg is not None else "USDT"
        st.executor = None
        st.mode = "paper"
        st.ignore_signal_filters = True
        st.confirmation_enabled = False
        st.pump_threshold_pct = 0.0
        st.min_volume_24h = 0.0
        st.min_market_cap = 0.0

        try:
            has_open_paper = bool(st.get_open_trades())
        except Exception:
            has_open_paper = False

        if not has_open_paper:
            st.account_balance = cash
            st.initial_balance = cash
            st.cash = cash
            st.peak_equity = cash
            st.trading_halted = False
            st.halt_reason = ""
            try:
                st._save_state()
            except Exception as exc:
                logger.debug("[PAPER] Could not persist fresh session state: %s", exc)
            logger.info(
                "[PAPER] Balance initialized | cash=%.2f %s "
                "(peak_equity reset, halt cleared)",
                cash, st.quote_currency,
            )
        else:
            logger.info(
                "[PAPER] Balance preserved (open paper trades present) | "
                "cash=%.2f %s peak=%.2f %s",
                st.cash, st.quote_currency,
                st.peak_equity, st.quote_currency,
            )

        if cfg is not None:
            st.max_open_trades = int(
                getattr(cfg, "max_open_positions", getattr(cfg, "max_open_trades", 3)) or 3
            )
            self._apply_strategy_settings(cfg)
            self._apply_live_sizing(cfg)

        if normalize_execution_mode(getattr(self, "execution_mode", PAPER)) == PAPER:
            logger.info(
                "[PAPER] Trend shadow ready | size=%.0f SL=%.2f trail=%.2f act=%.2f TP=%.2f",
                st.fixed_position_quote, st.stop_loss_pct, st.trailing_distance_pct,
                getattr(st, "trailing_activation_pct", 0.0), st.take_profit_percent,
            )

    def _paper_shadow_global_lead(self, rows: List[Dict[str, Any]]) -> None:
        plan = self.current_cycle_plan()
        if not plan.get("open_paper") or not rows or self.signal_tracker is None:
            return
        st = self.signal_tracker
        if not st.auto_trading_enabled:
            return
        st.ignore_signal_filters = True
        st.confirmation_enabled = False
        st.pump_threshold_pct = 0.0
        try:
            result = st.process_new_signals(rows)
            if result.get("opened") or result.get("pending") or result.get("closed"):
                logger.info(
                    "[PAPER][GLOBAL] opened=%s pending=%s closed=%s",
                    result.get("opened"), result.get("pending"), result.get("closed"),
                )
        except Exception as exc:
            logger.warning("[PAPER][GLOBAL] Paper shadow failed: %s", exc)

    def _configure_momentum_engine(self, cfg) -> None:
        """Nobitex-only configuration with Eagle Exception enabled."""
        raw_spread = float(getattr(cfg, "max_spread_pct", 1.2) or 1.2)
        raw_age = float(getattr(cfg, "max_market_data_age_sec", 300.0) or 300.0)
        min_vol_irt = float(getattr(cfg, "min_volume_24h", 500_000_000.0) or 500_000_000.0)

        eagle_enabled = bool(getattr(cfg, "btc_dump_exception_enabled", True))
        eagle_min_obs = float(getattr(cfg, "eagle_min_observed_move_pct", 2.5) or 2.5)
        eagle_min_1h = float(getattr(cfg, "eagle_min_1h_pct", 2.0) or 2.0)
        eagle_min_vol = float(getattr(cfg, "eagle_min_volume_irt", 300_000_000.0) or 300_000_000.0)
        eagle_max_spread = float(getattr(cfg, "eagle_max_spread_pct", 0.9) or 0.9)

        kwargs = dict(
            pump_threshold_pct=float(getattr(cfg, "pump_threshold_pct", 1.2)),
            max_spread_pct=max(0.2, raw_spread),
            min_volume_irt=min_vol_irt,
            max_market_data_age_sec=max(raw_age, 180.0),
            min_local_volume_irt=min_vol_irt,
            max_local_fall_pct=float(getattr(cfg, "max_local_fall_pct", 0.8)),
            max_chase_pct=float(getattr(cfg, "max_chase_pct", 0.55)),
            movement_lookback_scans=int(getattr(cfg, "movement_lookback_scans", 4) or 4),
            min_confirm_scans=int(getattr(cfg, "min_confirm_scans", 1) or 1),
            min_observed_move_pct=float(getattr(cfg, "min_observed_move_pct", 0.7)),
            max_local_24h_pct=float(getattr(cfg, "max_local_24h_pct", 15.0)),
            btc_max_dump_pct=float(getattr(cfg, "btc_max_dump_pct", 1.5)),
            btc_dump_exception_enabled=eagle_enabled,
            eagle_min_observed_move_pct=eagle_min_obs,
            eagle_min_1h_pct=eagle_min_1h,
            eagle_min_volume_irt=eagle_min_vol,
            eagle_max_spread_pct=eagle_max_spread,
        )
        if self.momentum_engine is None:
            self.momentum_engine = NobitexMomentumEngine(**kwargs)
        else:
            self.momentum_engine.configure(**kwargs)
        tag = self.current_cycle_plan().get("scan_tag", "[REAL]")
        auto_mode = "AUTO-REGIME" if bool(getattr(cfg, "auto_regime_strategy", False)) else "MANUAL"
        logger.info(
            "%s[GLOBAL] Nobitex momentum | %s | regime=%s | pump=%.2f obs=%.2f spread=%.2f "
            "lookback=%d confirm=%d age=%.0fs vol=%.0f IRT | "
            "eagle=%s obs>=%.2f 1h>=%.2f vol>=%.0f spread<=%.2f",
            tag, auto_mode, self.current_regime,
            kwargs["pump_threshold_pct"], kwargs["min_observed_move_pct"],
            kwargs["max_spread_pct"], kwargs["movement_lookback_scans"],
            kwargs["min_confirm_scans"],
            kwargs["max_market_data_age_sec"], kwargs["min_volume_irt"],
            "ON" if eagle_enabled else "OFF",
            eagle_min_obs, eagle_min_1h, eagle_min_vol, eagle_max_spread,
        )

    def _real_scan_interval_ms(self) -> int:
        cfg = self._bot_cfg
        seconds = 20
        if cfg is not None:
            seconds = int(getattr(cfg, "check_interval_seconds", 20) or 20)
        return max(10, min(seconds, 120)) * 1000

    @staticmethod
    def _orderbook_quote_depth(levels: Any, n: int = 5) -> float:
        total = 0.0
        if not isinstance(levels, list):
            return 0.0
        for level in levels[:n]:
            try:
                if isinstance(level, (list, tuple)) and len(level) >= 2:
                    total += float(level[0]) * float(level[1])
                elif isinstance(level, dict):
                    px = float(level.get("price") or level.get("p") or 0.0)
                    qty = float(level.get("amount") or level.get("quantity") or level.get("q") or 0.0)
                    total += px * qty
            except (TypeError, ValueError):
                continue
        return total

    def _has_executable_depth(self, candidate: Dict[str, Any], min_quote: float) -> bool:
        if min_quote <= 0 or not self.trading_bot:
            return True
        symbol = str(candidate.get("Pair") or candidate.get("Symbol") or "")
        if not symbol:
            return True
        try:
            book = self.trading_bot.get_order_book(symbol, limit=8)
        except Exception as exc:
            logger.debug("[REAL][LIVE] Order book unavailable for %s: %s", symbol, exc)
            return True
        depth = self._orderbook_quote_depth((book or {}).get("asks") or [])
        if depth > 0 and depth < min_quote:
            logger.info(
                "[REAL][LIVE] Skip %s: ask depth %.0f < min %.0f",
                symbol, depth, min_quote,
            )
            return False
        return True

    def _portfolio_reconcile_interval(self) -> int:
        cfg = self._bot_cfg
        scans = 3
        if cfg is not None:
            scans = int(getattr(cfg, "portfolio_reconcile_every_scans", 3) or 3)
        return max(1, min(scans, 20))

    def _portfolio_reconcile_min_interval(self) -> float:
        cfg = self._bot_cfg
        seconds = 30.0
        if cfg is not None:
            try:
                seconds = float(getattr(cfg, "portfolio_reconcile_min_interval_seconds", 30) or 30)
            except (TypeError, ValueError):
                seconds = 30.0
        return max(10.0, min(seconds, 300.0))

    def _reconcile_portfolio_cycle(self, *, force: bool = False, reason: str = "") -> Optional[Dict[str, Any]]:
        """Synchronize Nobitex wallet and the internal live trade ledger."""
        bot = self.trading_bot
        tracker = self.real_signal_tracker
        if bot is None or str(getattr(bot, "exchange_name", "")).lower() != "nobitex":
            return None
        if tracker is None:
            return None

        now = time.time()
        if not force and (now - self._last_portfolio_sync) < self._portfolio_reconcile_min_interval():
            return self._last_portfolio_snapshot

        try:
            snapshot = bot.reconcile_portfolio(
                tracker,
                force=True,
                include_orders=True,
            )
            if not snapshot:
                self._portfolio_sync_status = "error"
                logger.warning("[PORTFOLIO] Empty Nobitex portfolio snapshot | reason=%s", reason or "cycle")
                return None

            reconciliation = snapshot.get("reconciliation") or {}
            self._last_portfolio_snapshot = snapshot
            self._last_portfolio_sync = now
            self._portfolio_sync_status = (
                "synced" if bool(snapshot.get("valuation_complete", False)) else "partial"
            )

            logger.info(
                "[PORTFOLIO] Reconciled | reason=%s | value=%.0f %s | total=%.0f %s | "
                "assets=%d | open_orders=%d | valuation=%s | unpriced=%s | "
                "ledger_checked=%d kept=%d resized=%d phantom_closed=%d",
                reason or "cycle",
                float(snapshot.get("portfolio_value_quote", 0.0) or 0.0),
                snapshot.get("quote_currency", "IRT"),
                float(snapshot.get("portfolio_total_value_quote", 0.0) or 0.0),
                snapshot.get("quote_currency", "IRT"),
                int(snapshot.get("asset_count", 0) or 0),
                int(snapshot.get("open_order_count", 0) or 0),
                self._portfolio_sync_status,
                ",".join(snapshot.get("unpriced_assets", []) or []) or "-",
                int(reconciliation.get("checked", 0) or 0),
                int(reconciliation.get("kept", 0) or 0),
                int(reconciliation.get("resized", 0) or 0),
                int(reconciliation.get("closed_phantom", 0) or 0),
            )
            return snapshot
        except Exception as exc:
            self._portfolio_sync_status = "error"
            logger.error(
                "[PORTFOLIO] Reconciliation failed | reason=%s | %s",
                reason or "cycle", exc, exc_info=True,
            )
            return None

    def _live_auto_scan(self):
        """Nobitex-only momentum scan with adaptive regime; paper/live exclusive.

        FIX v7.8.5: the live tracker is now gated by
        `should_run_live_tracker(plan, has_live_positions)`.  In paper mode
        with no leftover live positions, the tracker is not called at all,
        which stops the per-cycle `/users/wallets/list` request (and the
        resulting 401 log spam) during Paper-mode runs.
        """
        if not self.trading_bot:
            return
        plan = self.current_cycle_plan()
        tag = plan.get("scan_tag", "[REAL]")
        self._portfolio_scan_count += 1
        if self.real_signal_tracker is not None:
            self.real_signal_tracker.allow_new_entries = bool(plan.get("open_live"))
        if not plan.get("open_paper") and self.real_signal_tracker is None:
            return
        try:
            local_rows = self.trading_bot.get_all_market_stats(self._scan_quote())
            if not local_rows:
                logger.warning("%s[LIVE] No local market data; skipping cycle.", tag)
                return

            now = time.time()
            live_rows = []
            live_syms = set()
            universe_rejected_no_price = 0
            universe_rejected_invalid = 0
            for r in local_rows:
                symbol = str(r.get("Symbol") or "").upper()
                price = safe_float(r.get("Price")) or 0.0
                if not symbol:
                    universe_rejected_invalid += 1
                    continue
                if price <= 0:
                    universe_rejected_no_price += 1
                    continue
                live_syms.add(symbol)
                prev = self._local_history.get(symbol)
                scan_change = ((price - prev[0]) / prev[0] * 100.0) if prev and prev[0] > 0 else 0.0
                self._local_history[symbol] = (price, now)
                bid = safe_float(r.get("Bid")) or 0.0
                ask = safe_float(r.get("Ask")) or 0.0
                spread = ((ask - bid) / bid * 100.0) if bid > 0 and ask >= bid else 99.0
                row = dict(r)
                row.update({
                    "Signal": "Neutral",
                    "Local 30s Change (%)": scan_change,
                    "1h Change (%)": safe_float(r.get("1h Change (%)")),
                    "Score": 0.0,
                    "AssetKey": f"spot:{symbol.lower()}",
                    "Pair": symbol + self._scan_quote(),
                    "DataSource": "NobitexBook",
                    "Local Spread (%)": spread,
                })
                live_rows.append(row)

            self._local_history = {
                k: v for k, v in self._local_history.items() if k in live_syms
            }
            self._local_last_scan = now

            has_managed_live_positions = False
            if self.real_signal_tracker is not None:
                try:
                    has_managed_live_positions = bool(self.real_signal_tracker.get_open_trades())
                except Exception:
                    has_managed_live_positions = False
            if (
                self.real_signal_tracker is not None
                and (plan.get("mode") == LIVE or has_managed_live_positions)
                and self._portfolio_scan_count % self._portfolio_reconcile_interval() == 0
            ):
                self._reconcile_portfolio_cycle(reason=f"scan-{self._portfolio_scan_count}")
            logger.info(
                "%s[UNIVERSE] source=%d accepted=%d rejected_no_price=%d rejected_invalid=%d quote=%s",
                tag, len(local_rows), len(live_rows), universe_rejected_no_price,
                universe_rejected_invalid, self._scan_quote(),
            )

            regime = self._update_regime(live_rows)

            candidates = []
            try:
                cfg = load_config(self._bot_config_path)
                self._bot_cfg = cfg
            except Exception:
                cfg = self._bot_cfg

            if cfg is not None:
                auto_regime = bool(getattr(cfg, "auto_regime_strategy", False))
                if auto_regime:
                    self._apply_auto_regime_strategy(regime, cfg)
                    self._apply_live_sizing(cfg)
                    if self.real_signal_tracker is not None:
                        try:
                            self.real_signal_tracker.pump_threshold_pct = float(
                                getattr(cfg, "min_observed_move_pct",
                                        self.real_signal_tracker.pump_threshold_pct)
                            )
                        except Exception:
                            pass
                else:
                    self._last_applied_regime_strategy = None
                    self._apply_strategy_settings(cfg)
                    self._apply_live_sizing(cfg)
                    self._apply_regime_preset(cfg, regime)
                self._configure_momentum_engine(cfg)

            if not plan.get("evaluate_signals"):
                logger.debug("%s Auto entries disabled; monitoring open live positions only.", tag)
            else:
                try:
                    if self.momentum_engine is None:
                        logger.warning("%s[NOBITEX] Momentum engine missing; no new entries this cycle.", tag)
                    else:
                        candidates = self.momentum_engine.evaluate(
                            live_rows, now=now, require_depth=False
                        )
                        min_depth = float(getattr(cfg, "min_ask_depth_quote", 0.0) or 0.0) if cfg else 0.0
                        depth_checked = 0
                        depth_rejected = 0
                        if min_depth > 0 and candidates:
                            enriched = []
                            for hit in candidates:
                                symbol = str(hit.get("Pair") or hit.get("Symbol") or "").upper()
                                try:
                                    book = self.trading_bot.get_order_book(symbol, limit=8)
                                except Exception as exc:
                                    logger.warning("%s[DEPTH] %s unavailable: %s", tag, symbol, exc)
                                    depth_rejected += 1
                                    continue
                                asks = (book or {}).get("asks") or []
                                depth = self._orderbook_quote_depth(asks, n=5)
                                depth_checked += 1
                                hit["asks"] = asks
                                hit["AskDepthQuote"] = depth
                                if depth < min_depth:
                                    depth_rejected += 1
                                    continue
                                enriched.append(hit)
                            candidates = enriched
                        logger.info(
                            "%s[NOBITEX] Filters | regime=%s | %s | execution_depth=%d rejected=%d threshold=%.0f IRT",
                            tag, regime, self.momentum_engine.stats_line(), depth_checked, depth_rejected, min_depth,
                        )
                except Exception as exc:
                    logger.warning(
                        "%s[GLOBAL] Nobitex momentum data unavailable: %s",
                        tag, exc, exc_info=True,
                    )

            candidate_map = {str(x.get("Symbol", "")).upper(): x for x in candidates}
            execution_rows = live_rows
            if plan.get("evaluate_signals") and candidate_map:
                for row in live_rows:
                    hit = candidate_map.get(str(row.get("Symbol", "")).upper())
                    if hit:
                        self._apply_global_lead_hit(row, hit)
                        active_strategy = getattr(self, "_last_applied_regime_strategy", None)
                        if active_strategy:
                            row["Strategy"] = active_strategy
                        is_eagle = bool(hit.get("EagleException"))
                        marker = "🦅" if is_eagle else "🎯"
                        logger.info(
                            "%s[GLOBAL→LIVE] %s %s | mom=%.2f%% | obs=%.2f%% | ask=%s | spread=%.2f%% | local_obs=%.2f%% | score=%.1f | signal=%s",
                            tag,
                            marker,
                            row.get("Symbol"),
                            float(hit.get("Global1hPct", 0)),
                            float(hit.get("ObservedGlobalMove (%)", 0)),
                            f"{float(hit.get('Local Ask', 0) or hit.get('Ask') or row.get('Price') or 0):.8f}",
                            float(hit.get("Local Spread (%)", 0)),
                            float(hit.get("ObservedLocalMove (%)", 0)),
                            float(hit.get("GlobalLeadScore", 0)),
                            row.get("Signal"),
                        )

            info = self._last_regime_info or {}
            logger.info(
                "%s Scan complete | Live markets=%d | opportunities=%d | "
                "regime=%s | score=%.1f breadth=%.2f btc24h=%.2f%% | "
                "strong_movers=%d top_move=%.2f%% | interval=%ss",
                tag, len(live_rows), len(candidates), regime,
                float(info.get("score", 0.0)),
                float(info.get("breadth", 0.0)),
                float(info.get("btc_24h", 0.0)),
                int(info.get("strong_movers_count", 0) or 0),
                float(info.get("top_mover_move", 0.0)),
                self._real_scan_interval_ms() // 1000,
            )
            if plan.get("open_paper"):
                self._paper_shadow_global_lead(list(live_rows))

            # FIX v7.8.5: only run the live tracker when the cycle plan
            # actually requires it.  In paper mode with no leftover live
            # positions, this call is a no-op that spams balance-read
            # warnings every cycle.
            has_live_positions = False
            if self.real_signal_tracker is not None:
                try:
                    has_live_positions = bool(self.real_signal_tracker.get_open_trades())
                except Exception:
                    has_live_positions = False

            if (
                self.real_signal_tracker is not None
                and should_run_live_tracker(plan, has_live_positions)
            ):
                try:
                    allow = bool(getattr(self.real_signal_tracker, "allow_new_entries", False))
                    auto = bool(getattr(self.real_signal_tracker, "auto_trading_enabled", False))
                    quote = str(getattr(self.real_signal_tracker, "quote_currency", "?"))
                    cash = float(getattr(self.real_signal_tracker, "cash", 0.0) or 0.0)
                    size = float(getattr(self.real_signal_tracker, "fixed_position_quote", 0.0) or 0.0)
                    min_n = float(getattr(self.real_signal_tracker, "min_notional_quote", 0.0) or 0.0)
                    try:
                        open_count = len(self.real_signal_tracker.get_open_trades() or [])
                    except Exception:
                        open_count = -1
                    max_open = int(getattr(self.real_signal_tracker, "max_open_trades", 0) or 0)
                    halted = bool(getattr(self.real_signal_tracker, "trading_halted", False))
                    logger.info(
                        "[REAL][LIVE] Tracker state | allow=%s auto=%s cash=%.2f %s "
                        "size=%.2f min_notional=%.2f open=%d/%d halted=%s regime=%s",
                        allow, auto, cash, quote, size, min_n,
                        open_count, max_open, halted, regime,
                    )
                    result = self.real_signal_tracker.process_new_signals(execution_rows)
                    logger.info(
                        "[REAL][LIVE] process_new_signals | opened=%s closed=%s pending=%s resized=%s candidates=%d",
                        result.get("opened"), result.get("closed"),
                        result.get("pending"), result.get("resized"), len(candidates),
                    )
                    if any(result.get(key) for key in ("opened", "closed", "resized")):
                        # BUY/fill/stop/trailing/SELL mutations are followed
                        # by an immediate exchange-vs-ledger reconciliation.
                        self._reconcile_portfolio_cycle(force=True, reason="order-mutation")
                    if not result.get("opened") and live_rows:
                        if not allow and plan.get("mode") == LIVE:
                            logger.warning(
                                "[REAL][LIVE] Entry gate CLOSED: Live mode is active but "
                                "Live+Start/Auto Entries is not armed. No real order will be sent."
                            )
                        elif allow and not candidates:
                            logger.info(
                                "[REAL][LIVE] Entry gate OPEN but no executable momentum candidate "
                                "passed the scanner filters this cycle."
                            )
                        logger.info(
                            "[REAL][LIVE] No trade opened this cycle. "
                            "Check: allow=%s auto=%s cash=%.2f size=%.2f halted=%s "
                            "candidates=%d open=%d/%d regime=%s",
                            allow, auto, cash, size, halted,
                            len(candidates), open_count, max_open, regime,
                        )
                except Exception as exc:
                    logger.error("[REAL][LIVE] process_new_signals crashed: %s", exc, exc_info=True)
        except Exception as exc:
            logger.error("%s Nobitex momentum scan failed: %s", tag, exc, exc_info=True)

    @staticmethod
    def _is_entry_signal(sig: Any) -> bool:
        text = str(sig or "").strip().lower()
        return any(token in text for token in ("buy", "movement", "pump", "lead", "trend", "momentum", "eagle"))

    def _apply_global_lead_hit(self, row: Dict[str, Any], hit: Dict[str, Any]) -> None:
        """Tag a local book row so SignalTracker will actually enter.

        FIX v7.8.3: now reads every key the Nobitex momentum engine
        actually writes.

        FIX v7.8.4: the engine's observed short move is written only to
        `ObservedLocalMove (%)` and `pump_pct`, NOT to `1h Change (%)`.
        """
        row.update(hit)

        ask = (
            safe_float(hit.get("Nobitex Ask"))
            or safe_float(hit.get("Local Ask"))
            or safe_float(hit.get("Ask"))
            or safe_float(row.get("Ask"))
            or safe_float(row.get("Price"))
        )
        if ask and ask > 0:
            row["Price"] = ask
            row["price"] = ask

        observed = safe_float(
            hit.get("ObservedGlobalMove (%)")
            or hit.get("ObservedLocalMove (%)")
            or hit.get("LocalMomentum (%)")
            or hit.get("LiveLeadMove (%)")
            or hit.get("pump_pct")
        )
        global_1h = safe_float(hit.get("Global1hPct")) or 0.0

        if observed:
            row["ObservedLocalMove (%)"] = observed
            row["pump_pct"] = observed

        if global_1h:
            row["Global1hPct"] = global_1h
            if not safe_float(row.get("1h Change (%)")):
                row["1h Change (%)"] = global_1h

        volume = safe_float(
            hit.get("GlobalVolumeUSD")
            or hit.get("Volume")
            or hit.get("24h Volume")
        )
        if volume:
            row["Volume"] = volume
            row["24h Volume"] = volume

        mcap = safe_float(hit.get("GlobalMarketCapUSD") or hit.get("Market Cap"))
        if mcap:
            row["Market Cap"] = mcap

        score = safe_float(
            hit.get("MomentumScore")
            or hit.get("GlobalLeadScore")
            or hit.get("Score")
        )
        if score is not None:
            row["Score"] = score

        engine_signal = hit.get("Signal") or hit.get("signal")
        if engine_signal and self._is_entry_signal(engine_signal):
            row["Signal"] = str(engine_signal)
            row["signal"] = str(engine_signal)
        elif not self._is_entry_signal(row.get("Signal") or row.get("signal")):
            row["Signal"] = "Trend Buy"
            row["signal"] = "Trend Buy"

    def _lookup_live_market(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.trading_bot or not symbol:
            return None
        quote = self._scan_quote()
        base = str(symbol).upper().replace("/", "").replace("-", "")
        if base.endswith(quote):
            base = base[: -len(quote)]
        try:
            rows = self.trading_bot.get_all_market_stats(self._scan_quote()) or []
        except Exception as exc:
            logger.warning("[REAL] Could not load live markets for SEND: %s", exc)
            return None
        for row in rows:
            if str(row.get("Symbol") or "").upper() == base:
                return dict(row)
        return None

    def _setup_main_window(self):
        self.root.title(f"CryptoBootEn  v{self.APP_VERSION}")
        self.root.geometry("1850x950")
        self.root.minsize(1400, 700)
        try:
            ico = resource_path("app_icon.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(ico)
        except Exception:
            pass

    def _post_init(self):
        if self.handle_disclaimer():
            self._add_pump_threshold_label()
            self.update_plan_display()
            self.refresh()
            self._start_tickers()
            self._start_status_rotation()
        else:
            self.root.destroy()

    def _add_pump_threshold_label(self):
        try:
            parent = self.dom_label.master
            self.pump_threshold_label = tk.Label(
                parent,
                text="Pump ≥ 5.0%",
                font=("Segoe UI", 9, "bold"),
                bg=parent.cget("bg"),
                fg="#FFA500",
            )
            self.pump_threshold_label.pack(side="left", padx=(10, 0))
        except Exception as e:
            logger.error("Could not add pump threshold label: %s", e)

    def _start_status_rotation(self):
        if not self.ticker_running:
            return
        msg = self._status_messages[self._status_index % len(self._status_messages)]
        self._status_index += 1
        if hasattr(self, "status_label"):
            self.status_label.config(text=msg)
        self._status_job = self.root.after(8000, self._start_status_rotation)

    def _shutdown_trading(self) -> None:
        self.real_auto_enabled = False
        if self._real_auto_job:
            try:
                self.root.after_cancel(self._real_auto_job)
            except Exception:
                pass
            self._real_auto_job = None
        try:
            with self.trading_bot_lock:
                bot = self.trading_bot
            if bot is not None:
                try:
                    bot.stop()
                except Exception:
                    pass
                try:
                    bot.close()
                except Exception:
                    pass
            TradingBot.release_instance()
        except Exception:
            logger.exception("Error shutting down trading bot")

    def _on_closing(self):
        """Shutdown sequence for the application."""
        self.ticker_running = False
        self._shutdown_trading()

        try:
            from gui.unified_trading_window import UnifiedTradingWindow
            UnifiedTradingWindow.close_if_open()
        except Exception as exc:
            logger.debug("Could not close trading window on shutdown: %s", exc)

        for job in (self._ticker_job, self._timer_job, self._fade_job, self._status_job, self._real_auto_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        self.root.destroy()

    def _load_settings(self):
        cfg = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE):
            cfg.read(CONFIG_FILE)
            if cfg.has_section("Settings"):
                self.api_source_var.set(cfg.get("Settings", "api_source", fallback="Nobitex"))
                self.enable_advanced_var.set(cfg.getboolean("Settings", "enable_advanced", fallback=False))
                self.enable_risk_var.set(cfg.getboolean("Settings", "enable_risk", fallback=True))
                self.adv_limit_var.set(cfg.getint("Settings", "adv_limit", fallback=DEFAULT_ADV_LIMIT))
                self.auto_refresh_var.set(cfg.getboolean("Settings", "auto_refresh", fallback=True))
                self.email_alert_var.set(cfg.get("Settings", "email_alert", fallback=""))
        self.api_source_var.set("Nobitex")

    def _load_api_key(self) -> str:
        try:
            with open(API_KEY_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except FileNotFoundError:
            return ""

    def save_api_key(self, key: str):
        self.api_key = key.strip()
        try:
            with open(API_KEY_FILE, "w", encoding="utf-8") as f:
                f.write(self.api_key)
        except OSError as exc:
            logger.error("Cannot save API key: %s", exc)

    def save_settings(self):
        cfg = configparser.ConfigParser()
        if os.path.exists(CONFIG_FILE):
            cfg.read(CONFIG_FILE)
        if not cfg.has_section("Settings"):
            cfg.add_section("Settings")
        cfg.set("Settings", "api_source", self.api_source_var.get())
        cfg.set("Settings", "enable_advanced", str(self.enable_advanced_var.get()))
        cfg.set("Settings", "enable_risk", str(self.enable_risk_var.get()))
        cfg.set("Settings", "adv_limit", str(self.adv_limit_var.get()))
        cfg.set("Settings", "auto_refresh", str(self.auto_refresh_var.get()))
        cfg.set("Settings", "email_alert", self.email_alert_var.get())
        try:
            with open(CONFIG_FILE, "w") as f:
                cfg.write(f)
        except OSError as exc:
            logger.error("Failed to save settings: %s", exc)

    def _load_user_status(self) -> dict:
        for args in [(self.device_id,), ()]:
            try:
                return user_status.load_user_status(*args)
            except (TypeError, AttributeError):
                continue
            except Exception:
                break
        return {"plan": "free", "trial_active": False, "refresh_count": 0}

    def _save_user_status(self, status: dict):
        for args in [(status, self.device_id), (status,)]:
            try:
                return user_status.save_user_status(*args)
            except (TypeError, AttributeError):
                continue
            except Exception as exc:
                logger.error("save_user_status failed: %s", exc)

    def _can_refresh(self) -> bool:
        for args in [(self.device_id,), ()]:
            try:
                return bool(user_status.can_refresh(*args))
            except (TypeError, AttributeError):
                continue
            except Exception:
                return False
        return False

    def _consume_refresh(self):
        for args in [(self.device_id,), ()]:
            try:
                return user_status.consume_refresh(*args)
            except (TypeError, AttributeError):
                continue
            except Exception:
                return

    def _show_refresh_blocked(self):
        messagebox.showwarning(
            "Limit Reached",
            "Daily free refresh limit reached.\nPlease upgrade to Premium to continue.",
        )
        self.open_premium_window()

    def _is_paid_plan_active(self, status: Optional[dict] = None) -> bool:
        status = status or self.user_status or {}
        plan = (status.get("plan") or "free").strip().lower()
        if plan in ("free", ""):
            return False
        exp = status.get("plan_expiry")
        if not exp:
            return True
        try:
            return datetime.now().date() <= datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            return False

    def _has_bot_access(self) -> bool:
        for args in [(self.device_id,), ()]:
            try:
                return bool(user_status.has_bot_access(*args))
            except AttributeError:
                return self._is_paid_plan_active(self._load_user_status())
            except TypeError:
                continue
            except Exception:
                return False
        return False

    def _bot_trial_days_left(self) -> Optional[int]:
        st = self._load_user_status()
        if self._is_paid_plan_active(st):
            return None
        if not st.get("bot_trial_active"):
            return 0
        start_str = st.get("bot_trial_start")
        if not start_str:
            return None
        try:
            start = datetime.strptime(start_str, "%Y-%m-%d").date()
            return max(0, 7 - (datetime.now().date() - start).days)
        except ValueError:
            return None

    def refresh(self):
        if self._refresh_in_progress:
            self._pending_refresh = True
            self._ui_status("⏳ Refresh already in progress — queued for next cycle…")
            return

        self.api_source_var.set("Nobitex")

        if not self._can_refresh():
            self._show_refresh_blocked()
            return

        with self._refresh_lock:
            if self._refresh_in_progress:
                self._pending_refresh = True
                self._ui_status("⏳ Refresh already in progress — queued…")
                return
            self._refresh_in_progress = True

        self._ui_status("Refreshing…")
        try:
            self.refresh_button.config(state="disabled")
        except Exception:
            pass
        threading.Thread(
            target=self._do_refresh, daemon=True, name="refresh-worker"
        ).start()

    def _refresh_unlock(self, enable_button: bool = True) -> None:
        with self._refresh_lock:
            self._refresh_in_progress = False
        if enable_button:
            try:
                self.root.after(0, lambda: self.refresh_button.config(state="normal"))
            except Exception:
                pass

    def _do_refresh(self):
        finished_on_ui = False
        try:
            api_source = self.api_source_var.get()

            if api_source == "Nobitex":
                self._ui_status("Fetching coins from Nobitex…")
                try:
                    if self.trading_bot is None:
                        self._ensure_bot()
                    rows = self.trading_bot.get_all_market_stats("IRT") if self.trading_bot else None
                except Exception as exc:
                    logger.exception("Nobitex refresh failed")
                    self.root.after(0, lambda msg=str(exc): messagebox.showerror(
                        "Error", f"Nobitex refresh failed:\n{msg}"
                    ))
                    return
                if not rows:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "No Data", "No coins retrieved from Nobitex."
                    ))
                    return

                df = self._build_nobitex_dataframe(rows)
                if df.empty:
                    finished_on_ui = True
                    self.root.after(0, lambda: self._finish_refresh(None, {}, df))
                    return

                btc_row = df[df["Symbol"] == "BTC"]
                btc_price = float(btc_row.iloc[0]["Price"]) if not btc_row.empty else None

                self._consume_refresh()
                self.user_status = self._load_user_status()
                finished_on_ui = True
                self.root.after(0, lambda: self._finish_refresh(btc_price, {}, df))
                return

        except Exception as exc:
            logger.exception("Unexpected error in _do_refresh")
            err = str(exc)
            self.root.after(0, lambda msg=err: messagebox.showerror(
                "Error", f"Unexpected error during refresh:\n{msg}"
            ))
            self._pending_refresh = False
        finally:
            self._refresh_unlock(enable_button=not finished_on_ui)

    def _build_nobitex_dataframe(self, rows: List[Dict[str, Any]]) -> pd.DataFrame:
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        df["Name"] = df["Symbol"]
        df["Slug"] = df["Symbol"].str.lower()
        df["Tags"] = [[] for _ in range(len(df))]
        df["AssetKey"] = df["Symbol"].str.lower().apply(lambda s: f"nobitex:{s}")
        df = df.sort_values(by="Volume", ascending=False, na_position="last").reset_index(drop=True)
        df["Rank"] = range(1, len(df) + 1)
        if "1h Change (%)" not in df.columns:
            df["1h Change (%)"] = df.get("24h Change (%)", 0.0)
        if "7d Change (%)" not in df.columns:
            df["7d Change (%)"] = 0.0
        if "Market Cap" not in df.columns:
            df["Market Cap"] = 0.0
        df["Risk"] = "Medium"
        df["Turnover Ratio (%)"] = np.where(
            df["Market Cap"].gt(0) & df["Volume"].notna(),
            df["Volume"] / df["Market Cap"] * 100,
            np.nan,
        )
        return df

    def _parse_listings(self, api: str, data: Any) -> pd.DataFrame:
        return self._build_nobitex_dataframe(data if isinstance(data, list) else [])

    def _finish_refresh(self, btc_price, g_data, df):
        if not isinstance(df, pd.DataFrame):
            logger.error("Data is not a DataFrame in _finish_refresh!")
            return

        self.last_global_metrics = g_data
        if btc_price:
            try:
                self.price_label.config(text=f"{btc_price:,.0f} IRT")
            except Exception:
                pass
        cond = self._get_market_condition(g_data)
        try:
            self.trend_label.config(text=cond)
            self.dom_label.config(text="N/A")
        except Exception:
            pass

        self.data_df = df

        try:
            cfg = load_config(self._bot_config_path)
            self._bot_cfg = cfg
        except Exception:
            cfg = self._bot_cfg

        pump_threshold = float(getattr(cfg, "pump_threshold_pct", 5.0) if cfg is not None else 5.0)
        min_volume_24h = float(getattr(cfg, "min_volume_24h", 100000.0) if cfg is not None else 100000.0)

        if hasattr(self, "pump_threshold_label"):
            try:
                self.pump_threshold_label.config(text=f"Pump ≥ {pump_threshold:.1f}%")
            except Exception:
                pass

        if hasattr(self, "cb_sig"):
            current_sig_filter = self.cb_sig.get()
            signal_values = ["All", f"{pump_threshold:.1f}% Pump"]
            if not df.empty and "Signal" in df.columns:
                unique_signals = df["Signal"].dropna().unique().tolist()
                for s in unique_signals:
                    if s != "Neutral" and s not in signal_values:
                        signal_values.append(s)
            self.cb_sig["values"] = signal_values
            if current_sig_filter not in signal_values:
                self.cb_sig.current(0)

        price_history = self.signal_tracker.get_price_history()
        current_prices: Dict[str, float] = {}
        df["Signal"] = "Neutral"

        movement_lookback = int(getattr(cfg, "movement_lookback_scans", 4) or 4) if cfg is not None else 4
        movement_lookback = max(1, min(movement_lookback, 60))
        market_condition = cond

        if not df.empty and "Symbol" in df.columns and "Price" in df.columns:
            for idx, row in df.iterrows():
                sym = str(row.get("Symbol", "")).upper()
                current_price = safe_float(row.get("Price"))
                if current_price is None or current_price <= 0:
                    continue
                current_prices[sym] = current_price
                prices = price_history.get(sym, [])
                if len(prices) < movement_lookback:
                    continue
                baseline = float(prices[-movement_lookback])
                if baseline <= 0:
                    continue
                movement_pct = ((current_price - baseline) / baseline) * 100.0
                local_1h = safe_float(row.get("1h Change (%)"))
                volume = safe_float(row.get("Volume", 0)) or 0.0

                if movement_pct >= pump_threshold:
                    if volume < min_volume_24h:
                        continue
                    if local_1h is not None and local_1h < 0:
                        continue
                    if market_condition in ("Bearish", "Strong Bear"):
                        continue
                    if market_condition == "Neutral" and volume < 2 * min_volume_24h:
                        continue
                    df.at[idx, "Signal"] = f"{movement_pct:.2f}% Pump"

        self.signal_tracker.update_price_history(current_prices)

        if not df.empty and "Symbol" in df.columns:
            quote = self._get_bot_quote(default="IRT")
            self.latest_signals = df.apply(
                lambda r: {
                    "Symbol": r.get("Symbol", "").upper(),
                    "AssetKey": r.get("AssetKey", f"unknown:{r.get('Symbol', '')}"),
                    "Slug": r.get("Slug", ""),
                    "Pair": make_pair(r.get("Symbol", ""), quote),
                    "Price": safe_float(r.get("Price")),
                    "Signal": r.get("Signal", "Neutral"),
                    "Risk": r.get("Risk", "N/A"),
                    "24h Change (%)": safe_float(r.get("24h Change (%)")),
                    "1h Change (%)": safe_float(r.get("1h Change (%)")),
                    "Market Cap": safe_float(r.get("Market Cap")),
                    "Volume": safe_float(r.get("Volume")),
                },
                axis=1,
            ).tolist()

            logger.debug(
                "[PAPER] Public scanner refreshed (source=%s); paper entries stay on the live-book cycle.",
                self.api_source_var.get(),
            )
        else:
            self.latest_signals = []

        self.apply_filter()
        try:
            auto_label = "🤖 AUTO" if bool(getattr(cfg, "auto_regime_strategy", False)) else "✋ MANUAL"
            self.status_label.config(
                text=f"✅ Last refreshed: {datetime.now():%H:%M:%S} | "
                     f"Pump ≥ {pump_threshold:.1f}% | Regime: {self.current_regime} | "
                     f"{auto_label} | 🦅 Eagle mode ON"
            )
        except Exception:
            pass

        if self._pending_refresh:
            self._pending_refresh = False
            self.root.after(100, self.refresh)
        else:
            try:
                self.refresh_button.config(state="normal")
            except Exception:
                pass

        if self._timer_job:
            try:
                self.root.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None

        self.remaining_time = REFRESH_INTERVAL_MS // 1000
        self._update_timer()
        self._update_tickers()
        self.update_plan_display()
        self.root.after(100, self._show_fade_image)

    def _get_market_condition(self, g_data: Optional[Dict]) -> str:
        df = getattr(self, "data_df", pd.DataFrame())
        if df.empty or "24h Change (%)" not in df.columns:
            return "Neutral"
        changes = pd.to_numeric(df["24h Change (%)"], errors="coerce").dropna()
        if changes.empty:
            return "Neutral"
        positive = float((changes > 0).mean())
        negative = float((changes < 0).mean())
        if positive >= 0.70: return "Strong Bull"
        if positive >= 0.55: return "Bullish"
        if negative >= 0.70: return "Strong Bear"
        if negative >= 0.55: return "Bearish"
        return "Neutral"

    def _is_pump_signal(self, sig: str) -> bool:
        return bool(sig and isinstance(sig, str) and sig.endswith("% Pump"))

    def apply_filter(self, _=None):
        if self.data_df.empty:
            self._update_tree(pd.DataFrame())
            return
        mask = pd.Series(True, index=self.data_df.index)
        if self.cb_cat.get() != "All":
            tags = self.CATEGORIES.get(self.cb_cat.get(), [])
            mask &= self.data_df["Tags"].apply(
                lambda x: any(t in x for t in tags) if isinstance(x, list) else False
            )
        if self.cb_sig.get() != "All":
            mask &= self.data_df.get("Signal", pd.Series(dtype=str)) == self.cb_sig.get()
        if self.cb_risk.get() != "All":
            mask &= self.data_df.get("Risk", pd.Series(dtype=str)) == self.cb_risk.get()
        if self.search_var.get():
            mask &= (
                self.data_df["Symbol"].str.lower().str.contains(
                    self.search_var.get().lower(),
                )
                | self.data_df["Name"].str.lower().str.contains(
                    self.search_var.get().lower(),
                )
            )
        self.filtered_df = self.data_df[mask]
        self._update_tree(self.filtered_df)

    def _update_tree(self, df: pd.DataFrame):
        self.tree.delete(*self.tree.get_children())
        if df.empty:
            return
        self.user_status = self._load_user_status()
        bot_ok = self._has_bot_access()
        is_vip = self._is_paid_plan_active(self.user_status)
        try:
            df_sorted = df.sort_values(
                by=["1h Change (%)", "Rank"], ascending=[False, True], na_position="last",
            )
        except Exception:
            df_sorted = df
        for i, (_, row) in enumerate(df_sorted.iterrows(), 1):
            sig = str(row.get("Signal", "Neutral"))
            rsk = str(row.get("Risk", "N/A"))
            bot_cell = "SEND" if bot_ok and self._is_pump_signal(sig) else ""
            ai_cell = self._compute_ai_cell(sig, safe_float(row.get("RSI")), is_vip)
            values = (
                i, row.get("Rank"), row.get("Name"), row.get("Symbol"),
                fmt(row.get("Price"), ".4f"),
                fmt(row.get("1h Change (%)"), ".2f"),
                fmt(row.get("24h Change (%)"), ".2f"),
                fmt(row.get("7d Change (%)"), ".2f"),
                fmt(row.get("pullback_from_high"), ".1f"),
                fmt(row.get("RSI"), ".1f"),
                fmt(row.get("MACD"), ".4f"),
                fmt(row.get("BB Width"), ".1f"),
                fmt(row.get("Stoch %K"), ".1f"),
                fmt(row.get("Stoch %D"), ".1f"),
                fmt(row.get("ADX"), ".1f"),
                fmt_volume(row.get("Tx Volume (24h)")),
                row.get("Active Addresses", "--") or "--",
                fmt(row.get("Turnover Ratio (%)"), ".2f"),
                fmt_mcap(row.get("Market Cap")),
                sig, rsk, ai_cell, bot_cell, "🔗", "📈",
            )
            tags = (
                [sig, rsk, "link", "TV_link"]
                + (["BOT_link"] if bot_cell else [])
                + (["VIP"] if "VIP" in ai_cell else [])
            )
            self.tree.insert("", "end", values=values, tags=tuple(tags))

    def _get_btc_change(self) -> Optional[float]:
        if self.data_df.empty:
            return None
        row = self.data_df[self.data_df["Symbol"] == "BTC"]
        if row.empty:
            return None
        return safe_float(row.iloc[0].get("24h Change (%)"))

    def _compute_ai_cell(self, sig: str, rsi: Optional[float], is_vip: bool) -> str:
        if not self._is_pump_signal(sig):
            return "--"
        if not is_vip:
            return "🔒 VIP"
        return "🚀 Pump"

    def _sort_column(self, col: str, reverse: bool):
        def key_func(k):
            v = self.tree.set(k, col)
            if v in ("--", "N/A", "", "🔒 VIP"):
                return float("-inf")
            for icon in ("🔥 ", "🟢 ", "📊 ", "🚀 "):
                v = v.replace(icon, "")
            v = (v.replace("%", "").replace("B", "e9")
                 .replace("M", "e6").replace("K", "e3"))
            return safe_float(v) or v
        items = sorted(
            [(key_func(k), k) for k in self.tree.get_children()], reverse=reverse,
        )
        for i, (_, k) in enumerate(items):
            self.tree.move(k, "", i)
        self.tree.heading(col, command=lambda: self._sort_column(col, not reverse))

    def _on_click(self, event):
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col_id = self.tree.identify_column(event.x)
        item = self.tree.identify_row(event.y)
        if not item or not col_id:
            return
        self.tree.selection_set(item)
        col_idx = int(col_id.replace("#", "")) - 1
        col_name = self._COLS_FULL[col_idx]
        values = self.tree.item(item)["values"]
        if col_name == "BOT":
            self._handle_bot_click(values)
        elif col_name in ("LINK", "TV"):
            symbol = (
                str(values[self._COLS_FULL.index("Symbol")])
                if "Symbol" in self._COLS_FULL else ""
            )
            slug_s = self.filtered_df.loc[self.filtered_df["Symbol"] == symbol, "Slug"]
            slug = slug_s.iloc[0] if not slug_s.empty else symbol.lower()
            url = ""
            if col_name == "LINK":
                url = f"https://nobitex.ir/markets/view/{symbol}-IRT"
            elif col_name == "TV":
                url = f"https://www.tradingview.com/symbols/{symbol}USDT"
            if url:
                webbrowser.open_new_tab(url)

    def _on_motion(self, event):
        col_id = self.tree.identify_column(event.x)
        if not col_id:
            self.root.config(cursor="")
            return
        col_name = self._COLS_FULL[int(col_id.replace("#", "")) - 1]
        self.root.config(cursor="hand2" if col_name in ("LINK", "TV", "BOT") else "")

    def _handle_bot_click(self, values):
        bot_val = (
            str(values[self._COLS_FULL.index("BOT")])
            if "BOT" in self._COLS_FULL else ""
        )
        if bot_val == "SEND":
            if self._has_bot_access():
                self._send_to_bot(values)
            else:
                self._show_bot_locked()

    def _send_to_bot(self, values):
        def _v(col: str) -> Any:
            try:
                return values[self._COLS_FULL.index(col)]
            except (ValueError, IndexError):
                return None
        sym = str(_v("Symbol") or "").upper()
        if not self.real_signal_tracker or not self.trading_bot:
            messagebox.showwarning(
                "Bot Not Ready",
                "Live trading is not connected.\n"
                "Check API keys and exchange in bot settings.",
                parent=self.root,
            )
            return
        if normalize_execution_mode(getattr(self, "execution_mode", PAPER)) != LIVE:
            messagebox.showwarning(
                "Paper mode",
                "Paper and live cannot run together.\n"
                "Switch to Live, press Start on the Real tab, then SEND.",
                parent=self.root,
            )
            return
        if not bool(getattr(self, "real_auto_enabled", False)):
            messagebox.showwarning(
                "Live entries paused",
                "Switch to Live and press Start on the Real tab before sending a live order.",
                parent=self.root,
            )
            return
        live = self._lookup_live_market(sym)
        if not live:
            messagebox.showwarning(
                "Not on this exchange",
                f"{sym} is not available on this exchange.\n"
                "The scanner table is informational; live orders use the selected exchange book only.",
                parent=self.root,
            )
            return
        ask = safe_float(live.get("Ask")) or safe_float(live.get("Price"))
        if not ask or ask <= 0:
            messagebox.showwarning(
                "No Live Price",
                f"Could not read a live ask/last price for {sym}.",
                parent=self.root,
            )
            return
        if not messagebox.askyesno(
            "Confirm Bot Trade",
            f"Send {sym} to live trading as a BUY?\n\n"
            f"Ask ≈ {ask:.8f} {self._scan_quote()}\n\n"
            "⚠️ May place a real order.",
            parent=self.root,
        ):
            return
        data = dict(live)
        data.update({
            "Symbol": str(live.get("Symbol") or sym).upper(),
            "symbol": str(live.get("Symbol") or sym).upper(),
            "AssetKey": live.get("AssetKey") or f"spot:{sym.lower()}",
            "Pair": live.get("Pair") or f"{sym}{self._scan_quote()}",
            "Signal": "buy",
            "signal": "buy",
            "Price": ask,
            "price": ask,
            "source": "GUI_TABLE",
        })

        def worker():
            self._ui_status(f"Bot: sending {data['Symbol']} on the live exchange…")
            self._ensure_bot()
            try:
                res = self.real_signal_tracker.process_new_signals([data])
                ok = bool(res.get("opened")) if isinstance(res, dict) else False
            except Exception as exc:
                logger.error("SEND to bot failed: %s", exc, exc_info=True)
                ok = False
            self._ui_status(
                f"✅ Bot: trade for {data['Symbol']} sent"
                if ok else f"❌ Bot: failed for {data['Symbol']}"
            )
        threading.Thread(target=worker, daemon=True).start()

    def _ensure_bot(self):
        with self.trading_bot_lock:
            if self.trading_bot is not None:
                return
            from trading.trader import TradingBot as _TradingBot
            from trading.bot_config import load_config as _load_config
            try:
                cfg = _load_config(self._bot_config_path)
            except TypeError:
                cfg = _load_config()
            self._bot_cfg = cfg
            self.trading_bot = _TradingBot.get_instance(config=cfg, auto_start=True)

    def _scan_quote(self) -> str:
        cfg = getattr(self, "_bot_cfg", None)
        quote = "IRT"
        if cfg is not None:
            quote = str(getattr(cfg, "quote_currency", "IRT") or "IRT")
        elif self.trading_bot is not None:
            quote = str(getattr(self.trading_bot, "quote_currency", "IRT") or "IRT")
        return quote.strip().upper() or "IRT"

    def _get_bot_quote(self, default: str = "IRT") -> str:
        if self._bot_quote_cache:
            return self._bot_quote_cache
        try:
            cfg = load_config(self._bot_config_path)
            self._bot_quote_cache = (getattr(cfg, "quote_currency", None) or default).upper()
            return self._bot_quote_cache
        except Exception:
            return default.upper()

    def _show_bot_locked(self):
        left = self._bot_trial_days_left()
        msg = (
            f"Bot Trial active ({left} day(s) left)."
            if left and left > 0
            else "Trading Bot requires Premium."
        )
        messagebox.showinfo("Bot Locked", msg, parent=self.root)
        self.open_premium_window()

    def _update_timer(self):
        if self._timer_job:
            try:
                self.root.after_cancel(self._timer_job)
            except Exception:
                pass
            self._timer_job = None
        if self.remaining_time > 0:
            m, s = divmod(self.remaining_time, 60)
            try:
                if self.timer_label.winfo_exists():
                    self.timer_label.config(text=f"{m}:{s:02d}")
            except Exception:
                pass
            self.remaining_time -= 1
            self._timer_job = self.root.after(1000, self._update_timer)
        else:
            try:
                if self.timer_label.winfo_exists():
                    self.timer_label.config(text="0:00")
            except Exception:
                pass
            if self.auto_refresh_var.get():
                self.refresh()

    def ensure_real_auto_cycle(self) -> None:
        if not getattr(self, "ticker_running", False) or self.trading_bot is None:
            return
        if self._real_auto_job:
            return
        try:
            self._real_auto_job = self.root.after(500, self._real_auto_cycle)
        except tk.TclError:
            self._real_auto_job = None

    def set_real_auto_entries(self, enabled: bool) -> bool:
        if enabled and normalize_execution_mode(getattr(self, "execution_mode", PAPER)) != LIVE:
            logger.warning("[REAL] Start ignored: switch to Live first (paper and live cannot run together).")
            self.real_auto_enabled = False
            if self.real_signal_tracker is not None:
                self.real_signal_tracker.allow_new_entries = False
            return False
        ready = self.real_signal_tracker is not None and self.trading_bot is not None
        self.real_auto_enabled = bool(enabled) and ready
        if self.real_signal_tracker is not None:
            self.real_signal_tracker.auto_trading_enabled = True
            self.real_signal_tracker.allow_new_entries = bool(self.real_auto_enabled)
        if self.real_auto_enabled:
            self.ensure_real_auto_cycle()
            logger.info(
                "[REAL] Auto entries ENABLED (adaptive regime, current=%s, eagle ON).",
                self.current_regime,
            )
        else:
            logger.info("[REAL] Auto entries PAUSED (open positions still monitored).")
        self._notify_execution_mode()
        return bool(self.real_auto_enabled) if enabled else True

    def _real_auto_cycle(self):
        if not self.ticker_running or not self.trading_bot:
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return

        if self._real_scan_lock.acquire(blocking=False):
            def _run():
                try:
                    self._live_auto_scan()
                finally:
                    try:
                        self._real_scan_lock.release()
                    except Exception:
                        pass
            threading.Thread(target=_run, daemon=True, name="live-auto-scan").start()
        else:
            logger.debug("[REAL] Previous live scan still running; skipping this tick.")

        try:
            self._real_auto_job = self.root.after(self._real_scan_interval_ms(), self._real_auto_cycle)
        except tk.TclError:
            self._real_auto_job = None

    def _start_tickers(self):
        self._update_tickers()
        self._ticker_job = self.root.after(3000, self._cycle_tickers)

    def _cycle_tickers(self):
        if not self.ticker_running:
            return
        self.gainers_index += 1
        self.losers_index += 1
        self._update_tickers()
        self._ticker_job = self.root.after(3000, self._cycle_tickers)

    def _update_tickers(self):
        if self.filtered_df.empty or "1h Change (%)" not in self.filtered_df.columns:
            return
        try:
            gainers = self.filtered_df.nlargest(10, "1h Change (%)")[
                ["Symbol", "1h Change (%)"]
            ].dropna()
            losers = self.filtered_df.nsmallest(10, "1h Change (%)")[
                ["Symbol", "1h Change (%)"]
            ].dropna()
            if not gainers.empty:
                r = gainers.iloc[self.gainers_index % len(gainers)]
                self.gainers_label.config(
                    text=f"{r['Symbol']} +{r['1h Change (%)']:.2f}%",
                    fg=self.theme.SUCCESS,
                )
            if not losers.empty:
                r = losers.iloc[self.losers_index % len(losers)]
                self.losers_label.config(
                    text=f"{r['Symbol']} {r['1h Change (%)']:.2f}%",
                    fg=self.theme.DANGER,
                )
        except Exception:
            pass

    def plot_chart(self):
        messagebox.showinfo(
            "Chart",
            "Advanced charts are disabled in the Pure Price Action version.",
        )

    def _ui_status(self, text: str):
        try:
            self.root.after(0, lambda: self.status_label.config(text=text))
        except Exception:
            pass

    def toggle_simple_mode(self):
        cols = self._COLS_SIMPLE if self.simple_mode_var.get() else self._COLS_FULL
        for c in self._COLS_FULL:
            self.tree.column(
                c, width=self._COL_WIDTHS.get(c, 0) if c in cols else 0,
            )
        self.apply_filter()

    def open_user_guide(self):
        webbrowser.open(f"file://{resource_path('UserGuide.html')}")

    def open_note_window(self):
        NoteWindow(self.root, self)

    def open_settings(self):
        SettingsWindow(self.root, self)

    def open_premium_window(self):
        PremiumWindow(self.root, self)

    def open_bot_panel(self):
        if self._has_bot_access():
            from gui.unified_trading_window import UnifiedTradingWindow
            UnifiedTradingWindow.get_or_create(self.root, self, initial_tab=1)
        else:
            self._show_bot_locked()

    def open_bot_settings(self):
        """Open CryptoBootEn Bot on the live tab, then Bot Settings."""
        if not self._has_bot_access():
            self._show_bot_locked()
            return
        from gui.unified_trading_window import UnifiedTradingWindow
        UnifiedTradingWindow.get_or_create(
            self.root, self, initial_tab=1, open_settings=True,
        )

    def open_signal_performance(self):
        try:
            from gui.unified_trading_window import UnifiedTradingWindow
            UnifiedTradingWindow.get_or_create(self.root, self, initial_tab=0)
        except Exception as e:
            logger.error("Error opening unified trading window: %s", e)

    def open_signal_backtester(self):
        self.open_signal_performance()

    def clear_filters(self):
        self.cb_cat.current(0)
        self.cb_sig.current(0)
        self.cb_risk.current(0)
        self.search_var.set("")
        self.apply_filter()

    def _live_activity_blocking_db_clear(self) -> Optional[str]:
        opens: List[Any] = []
        if self.real_signal_tracker is not None:
            try:
                opens = self.real_signal_tracker.get_open_trades() or []
            except Exception:
                opens = []
        exchange_orders: List[Any] = []
        if self.trading_bot is not None:
            try:
                exchange_orders = self.trading_bot.get_open_orders() or []
            except Exception as exc:
                logger.warning("Could not list open orders before DB clear: %s", exc)
                return (
                    "Could not confirm open orders. "
                    "Cancel any live orders on the exchange first, then retry."
                )
        if opens or exchange_orders:
            return (
                "Cannot clear databases while live activity exists: "
                f"{len(opens)} tracked position(s), {len(exchange_orders)} exchange order(s). "
                "Close or cancel them on the exchange first."
            )
        return None

    def clear_databases(self) -> None:
        blocked = self._live_activity_blocking_db_clear()
        if blocked:
            messagebox.showerror("Clear Database blocked", blocked, parent=self.root)
            logger.error("clear_databases refused: %s", blocked)
            return
        if not messagebox.askyesno(
            "🗑️ Clear Database — Step 1/2",
            "This will PERMANENTLY delete ALL trading data:\n\n"
            "  • Paper trading history\n"
            "  • Real trading history\n"
            "  • Open positions\n"
            "  • Pending signals\n"
            "  • Cooldown timers\n\n"
            "⚠️  This CANNOT be undone.\n\n"
            "Do you want to continue?",
            icon="warning",
            parent=self.root,
        ):
            return

        if not messagebox.askyesno(
            "🗑️ Clear Database — Step 2/2",
            "FINAL CONFIRMATION\n\n"
            "All trade history will be destroyed.\n\n"
            "Click YES to proceed.",
            icon="warning",
            parent=self.root,
        ):
            return

        results: List[Tuple[str, str]] = []

        try:
            closed_count = self._close_open_trading_windows()
            if closed_count > 0:
                results.append(("Trading Window", f"✅ Closed {closed_count} window(s)"))
                time.sleep(0.6)
        except Exception as exc:
            logger.warning("Could not close trading windows: %s", exc)
            results.append(("Trading Window", f"⚠️  {exc}"))

        try:
            ok = self.signal_tracker.clear_all_trades()
            results.append(("Paper Trading DB", "✅ Cleared" if ok else "⚠️  Failed"))
        except Exception as exc:
            logger.error("clear_databases paper failed: %s", exc, exc_info=True)
            results.append(("Paper Trading DB", f"❌ {exc}"))

        try:
            if self.real_signal_tracker is not None:
                ok = self.real_signal_tracker.clear_all_trades()
                results.append(("Real Trading DB", "✅ Cleared" if ok else "⚠️  Failed"))
            leftover_locked = False
            leftover_deleted = False
            leftover_found = False
            for name in _REAL_DB_FILENAMES:
                real_db = os.path.join(APPDATA_DIR, name)
                if self.real_signal_tracker is not None and os.path.abspath(real_db) == os.path.abspath(
                    getattr(self.real_signal_tracker, "db_path", "")
                ):
                    continue
                if not os.path.exists(real_db):
                    continue
                leftover_found = True
                for suffix in ("", "-wal", "-shm"):
                    path = real_db + suffix
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                            leftover_deleted = True
                        except PermissionError:
                            leftover_locked = True
            if self.real_signal_tracker is None:
                if not leftover_found:
                    results.append(("Real Trading DB", "ℹ️  No file (already empty)"))
                elif leftover_locked and not leftover_deleted:
                    results.append((
                        "Real Trading DB",
                        "❌ File is locked — close Trading window and retry",
                    ))
                elif leftover_locked:
                    results.append(("Real Trading DB", "⚠️  Partially deleted (some files locked)"))
                else:
                    results.append(("Real Trading DB", "✅ Cleared"))
            elif leftover_found and leftover_deleted:
                results.append(("Legacy Real DB files", "✅ Removed"))
        except Exception as exc:
            logger.error("clear_databases real failed: %s", exc, exc_info=True)
            results.append(("Real Trading DB", f"❌ {exc}"))

        try:
            self.signal_tracker.trading_halted = False
            self.signal_tracker.halt_reason = ""
            self.signal_tracker.cash = self.signal_tracker.initial_balance
            self.signal_tracker.account_balance = self.signal_tracker.initial_balance
            self.signal_tracker.peak_equity = self.signal_tracker.initial_balance
            self.signal_tracker._save_state()

            if self.real_signal_tracker is not None:
                self.real_signal_tracker.trading_halted = False
                self.real_signal_tracker.halt_reason = ""
                try:
                    self.real_signal_tracker._sync_balance_from_executor()
                except Exception:
                    pass
                live_cash = float(getattr(self.real_signal_tracker, "cash", 0.0) or 0.0)
                if live_cash > 0:
                    self.real_signal_tracker.peak_equity = live_cash
                    self.real_signal_tracker.initial_balance = live_cash
                self.real_signal_tracker._save_state()
                logger.info(
                    "[REAL] Halt reset | halted=False | peak_equity=%.2f | cash=%.2f",
                    self.real_signal_tracker.peak_equity,
                    self.real_signal_tracker.cash,
                )
            self._local_history = {}
            results.append(("Bot State", "✅ Reset"))
        except Exception as exc:
            results.append(("Bot State", f"⚠️  {exc}"))

        try:
            if hasattr(self, "history_cache"):
                self.history_cache.clear()
        except Exception:
            pass

        summary = "\n".join(f"  • {name}: {status}" for name, status in results)
        messagebox.showinfo(
            "🗑️ Database Cleared",
            f"Results:\n\n{summary}\n\n"
            "You can now safely continue trading.",
            parent=self.root,
        )
        logger.info("clear_databases finished: %s", results)
        self._ui_status("🗑️ Databases cleared — ready for fresh trades.")

    def _close_open_trading_windows(self) -> int:
        """Close any live trading Toplevels, then clear the singleton ref."""
        closed = 0
        for widget in list(self.root.winfo_children()):
            if not isinstance(widget, tk.Toplevel):
                continue
            try:
                if not widget.winfo_exists():
                    continue
                title = str(widget.title() or "")
                if "CryptoBootEn" not in title and "Trading" not in title:
                    continue

                handled = False
                on_close = getattr(widget, "_on_close", None)
                if callable(on_close):
                    try:
                        on_close()
                        closed += 1
                        handled = True
                    except Exception as exc:
                        logger.debug("_on_close failed for %s: %s", title, exc)

                if not handled:
                    try:
                        widget.destroy()
                        closed += 1
                    except Exception as exc:
                        logger.debug("Failed to destroy %s: %s", title, exc)
            except Exception as exc:
                logger.debug("Error while closing a Toplevel: %s", exc)

        try:
            from gui.unified_trading_window import UnifiedTradingWindow
            if not UnifiedTradingWindow.has_live_instance():
                UnifiedTradingWindow._instance = None
        except Exception:
            pass

        if closed > 0:
            try:
                import gc
                gc.collect()
            except Exception:
                pass

        return closed

    def save_to_excel(self):
        if self.filtered_df.empty:
            return messagebox.showwarning("No Data", "Nothing to export.")
        path = asksaveasfilename(
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        self.filtered_df.drop(columns=["closes"], errors="ignore").to_excel(
            path, index=False,
        )
        messagebox.showinfo("Saved", f"✅ Exported to {path}")

    def handle_disclaimer(self, show_always: bool = False):
        cfg = configparser.ConfigParser()
        cfg.read(CONFIG_FILE)
        if (
            not show_always
            and cfg.has_section("Settings")
            and cfg.getboolean("Settings", "disclaimer_accepted", fallback=False)
        ):
            return True
        accepted = messagebox.askokcancel(
            "Disclaimer",
            "⚠️ This tool is for informational purposes only.\n"
            "Cryptocurrency trading involves significant risk.",
        )
        if accepted:
            if not cfg.has_section("Settings"):
                cfg.add_section("Settings")
            cfg.set("Settings", "disclaimer_accepted", "True")
            with open(CONFIG_FILE, "w") as f:
                cfg.write(f)
        return accepted

    def update_plan_display(self):
        self.user_status = self._load_user_status()
        st = self.user_status
        if st.get("trial_active") and st.get("trial_start"):
            try:
                start = datetime.strptime(st["trial_start"], "%Y-%m-%d").date()
                left = max(0, TRIAL_DAYS - (datetime.now().date() - start).days)
            except ValueError:
                left = 0
            text, color = f"Trial ({left}d)", self.theme.SUCCESS
        elif self._is_paid_plan_active(st):
            exp = st.get("plan_expiry")
            try:
                left = (
                    max(0, (datetime.strptime(exp, "%Y-%m-%d").date()
                            - datetime.now().date()).days)
                    if exp else 0
                )
            except ValueError:
                left = 0
            name = (st.get("plan") or "").replace("months", "M")
            text, color = f"Premium {name} ({left}d)", self.theme.PRIMARY
        else:
            cnt = st.get("refresh_count", 0)
            text, color = f"Free ({cnt}/{DAILY_FREE_REFRESH_LIMIT})", self.theme.TEXT_GRAY
        try:
            self.plan_label.config(text=text, fg=color)
        except Exception:
            pass

    def show_about(self):
        messagebox.showinfo(
            "About",
            f"🚀 Advanced Crypto Scanner v{self.APP_VERSION}\n\n"
            "Nobitex-only momentum strategy with adaptive regime,\n"
            "Eagle Exception, and optional auto-regime strategy switching.",
            parent=self.root,
        )

    def set_alerts(self):
        pass

    def on_api_source_change(self, _=None):
        self.api_source_var.set("Nobitex")
        self.cnt_entry.delete(0, tk.END)
        self.cnt_entry.insert(0, "300")

    def _handle_offline(self):
        self.root.after(0, lambda: messagebox.showwarning(
            "No Internet", "No internet connection. Loading cached data.",
        ))
        btc, g, df = self._load_offline()
        if not df.empty:
            self.root.after(0, lambda: self._finish_refresh(btc, g, df))
        self.root.after(0, lambda: self.refresh_button.config(state="normal"))
        with self._refresh_lock:
            self._refresh_in_progress = False

    def _load_offline(self) -> Tuple[Optional[float], dict, pd.DataFrame]:
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            return (
                data.get("btc_price"),
                data.get("global_metrics", {}),
                pd.DataFrame(data.get("data_df", [])),
            )
        except Exception:
            return None, {}, pd.DataFrame()

    def _save_offline(self, btc_price, g_data, df: pd.DataFrame):
        try:
            if isinstance(df, pd.DataFrame):
                with open(CACHE_FILE, "w") as f:
                    json.dump({
                        "data_df": df.drop(
                            columns=["closes", "AssetKey"], errors="ignore",
                        ).to_dict("records"),
                        "global_metrics": g_data,
                        "btc_price": btc_price,
                        "timestamp": str(datetime.now()),
                    }, f, default=str)
        except Exception as e:
            logger.error("Could not save offline cache: %s", e)

    def _show_fade_image(self):
        if self._fade_job:
            try:
                self.root.after_cancel(self._fade_job)
            except Exception:
                pass
            self._fade_job = None
        try:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            img_path = os.path.join(base_dir, "eagle.png")
            if not os.path.exists(img_path):
                img_path = resource_path("eagle.png")
            if not os.path.exists(img_path):
                logger.error("Eagle image NOT FOUND: %s", img_path)
                return
            self._fade_image_orig = Image.open(img_path).convert("RGBA")
            win_w = self.root.winfo_width()
            win_h = self.root.winfo_height()
            if win_w < 100 or win_h < 100:
                win_w, win_h = 1850, 950
            self._fade_image_orig = self._fade_image_orig.resize(
                (win_w, win_h), Image.Resampling.LANCZOS,
            )
            bg_color_hex = self.root.cget("bg") or "#1e1e1e"
            if not self._fade_label or not self._fade_label.winfo_exists():
                self._fade_label = tk.Label(self.root, bg=bg_color_hex)
                self._fade_label.place(x=0, y=0, relwidth=1, relheight=1)
            bg_rgb = self.root.winfo_rgb(bg_color_hex)
            bg_color = tuple(x >> 8 for x in bg_rgb) + (255,)
            bg_img = Image.new("RGBA", self._fade_image_orig.size, bg_color)
            blended = Image.alpha_composite(bg_img, self._fade_image_orig)
            self._fade_photo = ImageTk.PhotoImage(blended)
            self._fade_label.config(image=self._fade_photo)
            self._fade_label.lift()
            self._fade_job = self.root.after(2000, lambda: self._fade_step(255))
        except Exception as e:
            logger.error("Error loading eagle image: %s", e)

    def _fade_step(self, alpha: int):
        if alpha <= 0 or not self._fade_image_orig:
            self._cleanup_fade()
            return
        try:
            img_copy = self._fade_image_orig.copy()
            r, g, b, a = img_copy.split()
            a = a.point(lambda p: int(p * (alpha / 255.0)))
            img_copy.putalpha(a)
            bg_color_hex = self.root.cget("bg") or "#1e1e1e"
            bg_rgb = self.root.winfo_rgb(bg_color_hex)
            bg_color = tuple(x >> 8 for x in bg_rgb) + (255,)
            bg_img = Image.new("RGBA", img_copy.size, bg_color)
            blended = Image.alpha_composite(bg_img, img_copy)
            self._fade_photo = ImageTk.PhotoImage(blended)
            if self._fade_label and self._fade_label.winfo_exists():
                self._fade_label.config(image=self._fade_photo)
                self._fade_label.lift()
            self._fade_job = self.root.after(40, lambda: self._fade_step(alpha - 20))
        except Exception as e:
            logger.error("Fade animation error: %s", e)
            self._cleanup_fade()

    def _cleanup_fade(self):
        if self._fade_label and self._fade_label.winfo_exists():
            self._fade_label.destroy()
        self._fade_label = None
        self._fade_photo = None

    def _extract_btc_price(self, df):
        if not isinstance(df, pd.DataFrame) or "Symbol" not in df.columns:
            return None
        row = df[df["Symbol"] == "BTC"]
        return float(row.iloc[0]["Price"]) if not row.empty else None

    def get_latest_signals(self) -> List[Dict[str, Any]]:
        return self.latest_signals


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )
    root = tk.Tk()
    app = CryptoScannerApp(root)
    root.mainloop()