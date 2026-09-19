# gui/panels/real_trading_panel.py
"""
SmartEagle live trading panel.

Version: 7.5.1 — Symbol-negative-cache TTL

Changes vs 7.5.0:
  - FIX: `_is_symbol_unsupported()` now expires its negative cache
    after `_UNSUPPORTED_SYMBOL_TTL_SEC` (default 1 hour).  The previous
    behaviour cached "unsupported" permanently, so a single transient
    network failure inside `TradingBot.is_symbol_supported()` (which
    swallows every exception and returns False) poisoned the cache and
    the symbol was never re-checked for the rest of the session.  A
    positive result is still cached for the session lifetime because
    markets rarely delist mid-session.
  - Retained v7.5.0 centralized tracker sync (`apply_to_tracker`).

Retained from v7.4.6:
  - Start/Stop actually control live auto entries; flush UI queue.
"""
from __future__ import annotations

import csv
import logging
import os
import platform
import queue
import re
import threading
import time
from datetime import datetime
from tkinter import filedialog, messagebox, scrolledtext
from typing import Any, Callable, Dict, List, Optional, Tuple, TYPE_CHECKING
import tkinter as tk
from tkinter import ttk

if platform.system() == "Windows":
    try:
        import winsound
        SOUND_AVAILABLE = True
    except ImportError:
        SOUND_AVAILABLE = False
else:
    SOUND_AVAILABLE = False

from gui.dialogs.base_dialog import BaseDialog
from gui.gui_helpers import ToolTip, center_window, ConfirmDialog
from gui.ui_theme import Theme, Styles
from gui.trading_ui_helpers import (
    AutocompleteCombobox,
    make_tree,
    primary_btn,
    get_nobitex_exchanges,
)
from trading.bot_config import BotConfig, load_config, save_config
from signal_tracker import SignalTracker
from trading.trader import TradingBot
from trading.execution_mode import LIVE, PAPER, normalize_execution_mode
from core.irt_money import display_quote_label, parse_amount

if TYPE_CHECKING:
    from gui.gui_main import CryptoScannerApp

logger = logging.getLogger(__name__)
T = Theme

_LIVE_DB_FILENAME = "real_trades.db"
_SIM_DB_FILENAME = "signal_log_real.db"

# FIX v7.5.1: negative-cache TTL for "unsupported" symbols.
# A single transient network error inside
# TradingBot.is_symbol_supported() previously poisoned the negative
# cache for the whole session.  A 1-hour TTL keeps the cache useful
# while letting transient failures self-heal.
_UNSUPPORTED_SYMBOL_TTL_SEC = 3600.0


class RealTradingPanel(tk.Frame):
    _PUMP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:Pump|Movement)", re.IGNORECASE)
    REFRESH_INTERVAL_MS = 30_000
    TIMER_UPDATE_MS = 1_000

    def __init__(self, parent: tk.Widget, app: "CryptoScannerApp"):
        super().__init__(parent, bg=T.BG_APP)
        self.parent = parent
        self.app = app

        from core.config import APPDATA_DIR
        self._config_path = os.path.join(APPDATA_DIR, "bot_config.json")
        os.makedirs(os.path.dirname(self._config_path), exist_ok=True)

        self._closing = False
        self._signal_lock = threading.Lock()
        self._bot_ready = False
        self._bot_init_error = None
        self._using_app_live_tracker = False

        self._ui_callback_queue: queue.Queue = queue.Queue()
        self._log_queue: queue.Queue = queue.Queue()
        self._signal_queue: queue.Queue = queue.Queue()
        self._processed_signal_keys: set = set()

        self.bot: Optional[TradingBot] = None
        self.tracker: Optional[SignalTracker] = None
        self.config: BotConfig = self._load_cfg()
        self.pump_threshold_pct = float(getattr(self.config, "pump_threshold_pct", 5.0))

        self.auto_trade_enabled = False
        self._last_closed_count = 0
        self._bot_start_time: Optional[datetime] = None
        self._accounting_refresh_job: Optional[str] = None
        self._refresh_job: Optional[str] = None
        self._timer_job: Optional[str] = None
        self._updating = False

        # FIX v7.5.1: parallel timestamp map for the negative cache.
        # `_unsupported_symbols` keeps the set of symbols we've seen fail;
        # `_unsupported_symbol_ts` records when each was added so we can
        # expire stale entries.
        self._unsupported_symbols: set = set()
        self._unsupported_symbol_ts: Dict[str, float] = {}
        self._supported_symbols: set = set()

        self._apply_styles()
        self._build_ui()
        # Accounting is populated from actual Nobitex fills; it never estimates P&L.

        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="disabled")
        self._status_bar.config(text="⏳ Initializing bot...", bg=T.WARNING)

        threading.Thread(target=self._init_bot_async, daemon=True).start()
        self._signal_worker = threading.Thread(target=self._signal_processor_loop, daemon=True)
        self._signal_worker.start()

        self._schedule_ui_pump()
        self._schedule_refresh()
        self._schedule_timer()
        self._accounting_refresh_job = self.after(5000, self._schedule_accounting_refresh)

    def _load_cfg(self) -> BotConfig:
        try:
            return load_config(self._config_path)
        except Exception:
            return BotConfig()

    def _save_cfg(self) -> None:
        try:
            save_config(self.config, self._config_path)
        except Exception as e:
            logger.error("_save_cfg failed: %s", e)

    def _apply_styles(self) -> None:
        Styles.apply(ttk.Style(self))

    def _is_live_exchange(self) -> bool:
        name = str(getattr(self.bot, "exchange_name", "") or "").strip().lower()
        return bool(self.bot) and name not in ("simulator", "paper", "simulation", "")

    def _app_owns_auto_entries(self) -> bool:
        if self._using_app_live_tracker:
            return True
        strategy = str(getattr(self.app, "_real_strategy_name", "") or "").strip().lower()
        if "global" in strategy:
            return True
        return bool(getattr(self.app, "real_auto_enabled", False))

    # ══════════════════════════════════════════════════════════════
    # TRACKER SYNC — v7.5 delegates to bot_config.apply_to_tracker
    # ══════════════════════════════════════════════════════════════

    def _apply_config_to_tracker(self) -> None:
        """
        Sync the current BotConfig into the tracker.

        v7.5: now delegates to `trading.bot_config.apply_to_tracker()` —
        the single source of truth for this operation.  Previously the
        field list was duplicated here and could drift out of sync.
        """
        if not self.tracker:
            return
        try:
            from trading.bot_config import apply_to_tracker

            is_live = self._is_live_exchange()
            apply_to_tracker(self.config, self.tracker, is_live_exchange=is_live)

            # Live-only post-processing: ensure fixed lot is at least the
            # minimum notional for the exchange.
            if is_live and self.tracker.position_size_mode == "fixed":
                self.tracker.fixed_position_quote = max(
                    self.tracker.fixed_position_quote,
                    self.tracker.min_notional_quote,
                )

            self.tracker._save_state()
            logger.debug("Tracker parameters updated via apply_to_tracker().")
        except Exception as e:
            logger.error("Failed to apply config to tracker: %s", e)

    def _clear_cooldowns(self) -> None:
        if not self.tracker:
            return
        try:
            with self.tracker._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM cooldowns")
                conn.commit()
            self.log("Cooldowns cleared.", "info")
        except Exception as e:
            logger.error("Failed to clear cooldowns: %s", e)

    def _init_bot_async(self) -> None:
        try:
            self._attach_bot()
            self._bot_ready = True
            self._bot_init_error = None
            self._post_ui_callback(self._on_bot_ready)
        except Exception as e:
            self._bot_ready = False
            self._bot_init_error = str(e)
            logger.error("RealTradingPanel async init failed: %s", e)
            self._post_ui_callback(self._on_bot_init_failed)

    def _attach_bot(self) -> None:
        exchange_name = (getattr(self.config, "exchange", "") or "simulator").strip().lower()

        if exchange_name != "simulator" and not (self.config.api_key and self.config.api_secret):
            error_msg = (
                f"API key/secret are missing for exchange '{exchange_name}'.\n"
                "Please set them in Settings or check the encryption key."
            )
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        existing = getattr(self.app, "trading_bot", None)
        if existing is not None:
            self.bot = existing
            self.app.trading_bot = existing
        else:
            self.bot = TradingBot.get_instance(config=self.config, auto_start=False)
            self.app.trading_bot = self.bot

        if not self.bot:
            raise RuntimeError("Trading bot is not available.")

        self.config.exchange = self.bot.exchange_name
        shared = getattr(self.app, "real_signal_tracker", None)

        if self.bot.exchange_name == "simulator":
            self._using_app_live_tracker = False
            self.tracker = SignalTracker(executor=None, db_filename=_SIM_DB_FILENAME)
            self.tracker.mode = "paper"
            self.tracker.auto_trading_enabled = False
            self.log("Simulator mode: using independent paper logic (signal_log_real.db).", "warning")
        elif shared is not None:
            self._using_app_live_tracker = True
            self.tracker = shared
            self.log(
                "Live mode: attached to the main scanner tracker (real_trades.db). "
                "Automatic entries stay on the real-movement trend cycle.",
                "info",
            )
        else:
            self._using_app_live_tracker = False
            self.tracker = SignalTracker(
                executor=self.bot,
                db_filename=_LIVE_DB_FILENAME,
            )
            self.app.real_signal_tracker = self.tracker
            self.tracker.auto_trading_enabled = True
            self.tracker.ignore_signal_filters = True
            self.log("Live mode: using real trading tracker (real_trades.db).", "info")

        self._apply_config_to_tracker()
        if self._is_live_exchange():
            self._sync_tracker_balance_from_exchange()
            try:
                result = self.tracker.reconcile_open_positions()
                if result.get("closed_phantom", 0) > 0:
                    self.log(
                        f"⚠️  Reconciled {result['closed_phantom']} phantom position(s). "
                        f"These were in the DB but not on the exchange.",
                        "warning",
                    )
                else:
                    self.log(
                        f"✅ Position reconciliation OK: {result.get('kept', 0)} position(s) verified.",
                        "info",
                    )
            except Exception as e:
                logger.error("Reconcile failed: %s", e)
                self.log(f"Reconcile failed: {e}", "error")
        else:
            self._reset_simulator_balance_if_zero()
        self._post_ui_callback(self._update_balance_display)

    def _read_quote_balance(self, fresh: bool = False) -> Optional[float]:
        if not self.bot:
            return None
        quote = str(getattr(self.config, "quote_currency", None) or getattr(self.bot, "quote_currency", "IRT") or "IRT")
        try:
            if fresh:
                getter = getattr(self.bot, "get_balance_fresh", None)
                if callable(getter):
                    return getter(quote)
            return self.bot.get_balance(quote)
        except Exception as e:
            logger.error("Failed to read quote balance: %s", e)
            return None

    def _sync_tracker_balance_from_exchange(self) -> None:
        if not self.tracker or not self.bot:
            return
        try:
            quote = str(getattr(self.config, "quote_currency", "IRT") or "IRT").upper()
            balance = self._read_quote_balance(fresh=True)
            if balance is None:
                return
            cash = float(balance)
            self.tracker.cash = cash
            self.tracker.account_balance = cash
            if not self.tracker.initial_balance or self.tracker.initial_balance <= 0:
                self.tracker.initial_balance = cash
            self.tracker.peak_equity = max(float(self.tracker.peak_equity or 0.0), cash)
            self.tracker._save_state()
            if quote in ("IRT", "RLS", "IRR"):
                self.log(
                    f"Tracker balance synced to {cash:,.2f} IRT (Rial)",
                    "info",
                )
            else:
                self.log(f"Tracker balance synced to {cash:.2f} {quote}", "info")
        except Exception as e:
            logger.error("Failed to sync tracker balance: %s", e)

    def _reset_simulator_balance_if_zero(self) -> None:
        if not self.bot or not self.bot.exchange:
            return
        try:
            quote = str(getattr(self.config, "quote_currency", "IRT") or "IRT").upper()
            current_balance = self.bot.exchange.get_balance(quote)
            if current_balance <= 0:
                initial_capital = float(getattr(self.config, "account_balance", 1000.0) or 1000.0)
                self.bot.exchange.balances[quote] = initial_capital
                self.bot.starting_balance = initial_capital
                self.bot.current_balance = initial_capital
                if self.tracker:
                    self.tracker.cash = initial_capital
                    self.tracker.account_balance = initial_capital
                    self.tracker.initial_balance = initial_capital
                    self.tracker.peak_equity = initial_capital
                    self.tracker._save_state()
                self.log(
                    f"Simulator balance was 0 and has been reset to ${initial_capital:.2f}.",
                    "warning",
                )
        except Exception as e:
            logger.error("Failed to reset simulator balance: %s", e)

    def _post_ui_callback(self, callback: Callable) -> None:
        self._ui_callback_queue.put(callback)

    def _process_ui_callbacks(self) -> None:
        try:
            while True:
                callback = self._ui_callback_queue.get_nowait()
                callback()
        except queue.Empty:
            pass

    def _schedule_ui_pump(self) -> None:
        if self._closing:
            return
        self._process_ui_callbacks()
        try:
            self.after(150, self._schedule_ui_pump)
        except Exception:
            pass

    def _sync_live_auto_entries(self, enabled: bool) -> bool:
        setter = getattr(self.app, "set_real_auto_entries", None)
        if callable(setter):
            return bool(setter(enabled))
        self.app.real_auto_enabled = bool(enabled)
        if enabled:
            ensure = getattr(self.app, "ensure_real_auto_cycle", None)
            if callable(ensure):
                ensure()
        if self.tracker:
            self.tracker.auto_trading_enabled = True
            self.tracker.allow_new_entries = bool(enabled)
        return True

    def refresh_execution_mode(self) -> None:
        mode = normalize_execution_mode(getattr(self.app, "execution_mode", PAPER))
        live_on = bool(getattr(self.app, "real_auto_enabled", False))
        if hasattr(self, "_mode_hdr") and self._mode_hdr.winfo_exists():
            if mode == LIVE:
                self._mode_hdr.config(
                    text="Mode: LIVE" + (" | entries ON" if live_on else " | press Start"),
                )
            else:
                self._mode_hdr.config(text="Mode: PAPER | live entries off")

    def _schedule_accounting_refresh(self) -> None:
        if self._closing:
            return
        try:
            self._refresh_accounting_now()
        finally:
            self._accounting_refresh_job = self.after(30000, self._schedule_accounting_refresh)

    def _on_bot_ready(self) -> None:
        if self._closing:
            return
        already_running = bool(self.bot and getattr(self.bot, "running", False))
        auto_on = bool(getattr(self.app, "real_auto_enabled", False))
        if already_running:
            self._bot_start_time = self._bot_start_time or datetime.now()
            self._set_running_ui(True)
            self._status_bar.config(
                text="🟢  Bot: Running" + ("  |  Auto entries ON" if auto_on else "  |  Auto entries PAUSED"),
                bg=T.SUCCESS_DARK,
            )
            self.log(
                "Nobitex is connected. Paper is the default. Switch to Live, then press Start for real orders.",
                "success",
            )
            if auto_on and self._is_live_exchange():
                self._auto_var.set(True)
                self._auto_status_lbl.config(text="✅ Global Lead Active", fg=T.SUCCESS_DARK)
                self.auto_trade_enabled = False
        else:
            self._set_running_ui(False)
            self._status_bar.config(
                text="🟢  Bot initialized. Paper is active. Switch to Live, then Start.",
                bg=T.SUCCESS_DARK,
            )
            self.log(
                "Bot initialized. Default is Paper (simulated). Switch to Live, then press Start for Nobitex orders.",
                "success",
            )
        self._update_balance_display()

    def _on_bot_init_failed(self) -> None:
        if self._closing:
            return
        self._start_btn.config(state="disabled")
        self._stop_btn.config(state="disabled")
        self._status_bar.config(
            text="❌  Bot init failed: " + (self._bot_init_error or "unknown error"),
            bg=T.DANGER,
        )
        self.log(f"Bot initialization failed: {self._bot_init_error}", "error")

    def _base_symbol(self, symbol: str) -> str:
        text = (
            str(symbol or "").upper()
            .replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
        )
        quote = str(
            getattr(self.config, "quote_currency", None)
            or getattr(self.bot, "quote_currency", "IRT")
            or "IRT"
        ).upper()
        if quote == "RLS":
            quote = "IRT"
        for suffix in (quote, "IRT", "RLS", "IRR", "USDT"):
            if text.endswith(suffix) and len(text) > len(suffix):
                return text[: -len(suffix)]
        return text

    def _lookup_nobitex_market(self, symbol: str) -> Optional[Dict[str, Any]]:
        if not self.bot or not symbol:
            return None
        base = self._base_symbol(symbol)
        try:
            quote = str(getattr(self.config, "quote_currency", "IRT") or "IRT")
            rows = self.bot.get_all_market_stats(quote) or []
        except Exception as exc:
            logger.warning("Could not load live markets for %s: %s", symbol, exc)
            return None
        for row in rows:
            if str(row.get("Symbol") or "").upper() == base:
                return dict(row)
        return None

    # ══════════════════════════════════════════════════════════════
    # FIX v7.5.1: TTL-aware symbol support check
    # ══════════════════════════════════════════════════════════════
    def _is_symbol_unsupported(self, symbol: str) -> bool:
        """
        Check whether `symbol` is known to be unsupported on the live
        exchange.

        FIX v7.5.1:
            The previous version cached "unsupported" forever.  Because
            `TradingBot.is_symbol_supported()` swallows every exception
            and returns False, a single transient network failure
            permanently poisoned the negative cache and the symbol was
            never re-checked for the rest of the session.

            We now store a timestamp alongside each negative entry and
            expire it after `_UNSUPPORTED_SYMBOL_TTL_SEC`.  Positive
            results are still cached for the session (markets rarely
            delist mid-session), so a symbol that has been verified
            supported never re-triggers a network call.
        """
        if not symbol or self.bot is None:
            return False
        if not self._is_live_exchange():
            return False

        base = self._base_symbol(symbol)
        if base in self._supported_symbols:
            return False

        now = time.time()
        if base in self._unsupported_symbols:
            added_at = self._unsupported_symbol_ts.get(base, 0.0)
            age = now - added_at
            if age < _UNSUPPORTED_SYMBOL_TTL_SEC:
                return True
            # TTL expired — drop the stale negative entry and re-check.
            self._unsupported_symbols.discard(base)
            self._unsupported_symbol_ts.pop(base, None)
            logger.debug(
                "Symbol %s negative-cache expired (age=%.0fs); re-checking support.",
                base, age,
            )

        try:
            supported = bool(self.bot.is_symbol_supported(base))
        except Exception as exc:
            # A hard exception means we genuinely could not determine
            # the answer.  Do NOT cache this as unsupported.
            logger.debug(
                "Symbol support check raised for %s: %s — not caching.",
                base, exc,
            )
            return False

        if supported:
            self._supported_symbols.add(base)
            return False

        self._unsupported_symbols.add(base)
        self._unsupported_symbol_ts[base] = now
        logger.debug(
            "Symbol %s is not supported on %s (cached, TTL=%ds).",
            base, self.bot.exchange_name, int(_UNSUPPORTED_SYMBOL_TTL_SEC),
        )
        return True

    def _build_accounting_section(self, parent) -> None:
        frame = tk.LabelFrame(
            parent, text="📒  Actual Trading Accounting",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY, padx=T.PAD_MD, pady=T.PAD_MD,
        )
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        self._accounting_vars = {
            "realized": tk.StringVar(value="—"),
            "unrealized": tk.StringVar(value="—"),
            "fees": tk.StringVar(value="—"),
            "status": tk.StringVar(value="Not synced"),
            "reconciliation": tk.StringVar(value="—"),
            "updated": tk.StringVar(value="—"),
        }
        rows = [
            ("Realized P&L:", "realized"),
            ("Unrealized P&L:", "unrealized"),
            ("Fees:", "fees"),
            ("Ledger:", "status"),
            ("Wallet reconciliation:", "reconciliation"),
            ("Last update:", "updated"),
        ]
        for row, (label, key) in enumerate(rows):
            tk.Label(frame, text=label, font=T.font(size=T.FONT_SM),
                     bg=T.BG_APP, fg=T.TEXT_SECONDARY).grid(
                row=row, column=0, sticky="w", pady=T.PAD_XS)
            tk.Label(frame, textvariable=self._accounting_vars[key],
                     font=T.font(size=T.FONT_SM, weight="bold"),
                     bg=T.BG_APP, fg=T.TEXT_PRIMARY).grid(
                row=row, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)

        tk.Button(
            frame, text="Refresh Accounting", command=self._refresh_accounting_now,
            relief="flat", cursor="hand2", padx=T.PAD_MD, pady=4,
        ).grid(row=0, column=2, rowspan=2, padx=(T.PAD_MD, 0), sticky="e")

        frame.columnconfigure(1, weight=1)

    def _refresh_accounting_now(self) -> None:
        if not self.bot or not self._is_live_exchange():
            return
        threading.Thread(target=self._refresh_accounting_worker, daemon=True).start()

    def _refresh_accounting_worker(self) -> None:
        try:
            sync = getattr(self.bot, "refresh_portfolio", None)
            if not callable(sync):
                raise RuntimeError("Portfolio accounting is not available.")
            snapshot = sync(force=True, include_orders=True) or {}
            accounting = snapshot.get("accounting") or {}
            recon = snapshot.get("accounting_reconciliation") or {}
            self._post_ui_callback(lambda: self._apply_accounting_snapshot(accounting, recon))
        except Exception as exc:
            logger.warning("Accounting dashboard refresh failed: %s", exc)
            self._post_ui_callback(
                lambda: self._accounting_vars["status"].set(f"Error: {exc}")
            )

    def _apply_accounting_snapshot(self, accounting: Dict[str, Any], recon: Dict[str, Any]) -> None:
        unit = str(getattr(self.config, "quote_currency", "IRT") or "IRT").upper()
        self._accounting_vars["realized"].set(
            f"{float(accounting.get('realized_pnl_quote', 0.0) or 0.0):,.2f} {unit}"
        )
        self._accounting_vars["unrealized"].set(
            f"{float(accounting.get('unrealized_pnl_quote', 0.0) or 0.0):,.2f} {unit}"
        )
        self._accounting_vars["fees"].set(
            f"{float(accounting.get('fees_quote', 0.0) or 0.0):,.2f} {unit}"
        )
        complete = bool(accounting.get("accounting_complete", False))
        self._accounting_vars["status"].set("Complete" if complete else "Incomplete — exchange data missing")
        self._accounting_vars["updated"].set(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        if recon.get("reconciled"):
            self._accounting_vars["reconciliation"].set("OK")
        else:
            count = len(recon.get("discrepancies") or [])
            self._accounting_vars["reconciliation"].set(f"{count} discrepancy(s)")

    def _build_ui(self) -> None:
        self._build_top_bar()
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=T.PAD_LG, pady=T.PAD_MD)
        self._build_signals_tab()
        self._build_open_trades_tab()
        self._build_history_tab()
        self._build_status_tab()
        self._build_accounting_section(self)
        self._build_status_bar()

    def _build_top_bar(self) -> None:
        bar = tk.Frame(self, bg=T.PRIMARY, height=T.HEADER_HEIGHT)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        tk.Label(
            bar, text="🦅  SmartEagle Bot (Live)",
            font=T.font(size=T.FONT_LG, weight="bold"),
            bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
        ).pack(side="left", padx=T.PAD_2XL)
        self._mode_hdr = tk.Label(
            bar, text="Mode: PAPER",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
        )
        self._mode_hdr.pack(side="left", padx=T.PAD_LG)

        self._balance_label = tk.Label(
            bar, text="Balance: $0.00",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
        )
        self._balance_label.pack(side="left", padx=T.PAD_LG)

        api_status = "🔑 API: " + ("Set" if (self.config.api_key and self.config.api_secret) else "Not Set")
        self._api_status_lbl = tk.Label(
            bar, text=api_status, font=T.font(size=T.FONT_XS),
            bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
        )
        self._api_status_lbl.pack(side="left", padx=T.PAD_SM)

        refresh_btn = tk.Button(
            bar, text="🔄 Refresh", font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.INFO, fg=T.TEXT_ON_PRIMARY, activebackground=T.PRIMARY_DARK,
            activeforeground=T.TEXT_ON_PRIMARY, relief="flat", cursor="hand2",
            padx=T.PAD_LG, pady=T.PAD_SM, bd=0, command=self._manual_refresh,
        )
        refresh_btn.pack(side="right", padx=T.PAD_SM, pady=T.PAD_MD)
        ToolTip(refresh_btn, "Refresh all data now")

        for text, bg, fg, cmd, tip in [
            ("⚙️ Settings", T.WARNING, T.TEXT_PRIMARY, self.open_bot_settings, "Open bot configuration"),
            ("⏹ Stop", T.DANGER, T.TEXT_ON_PRIMARY, self.stop_bot, "Stop trading bot"),
            ("▶ Start", T.SUCCESS, T.TEXT_ON_PRIMARY, self.start_bot, "Start trading bot"),
        ]:
            btn = tk.Button(
                bar, text=text, font=T.font(size=T.FONT_SM, weight="bold"),
                bg=bg, fg=fg, activebackground=T.PRIMARY_DARK,
                activeforeground=T.TEXT_ON_PRIMARY, relief="flat", cursor="hand2",
                padx=T.PAD_LG, pady=T.PAD_SM, bd=0, command=cmd,
            )
            btn.pack(side="right", padx=T.PAD_SM, pady=T.PAD_MD)
            ToolTip(btn, tip)
            if text == "▶ Start":
                self._start_btn = btn
            elif text == "⏹ Stop":
                self._stop_btn = btn
                self._stop_btn.config(state="disabled")

    def _build_status_tab(self) -> None:
        tab = tk.Frame(self._nb, bg=T.BG_APP)
        self._nb.add(tab, text="📊 Status")

        cards_frame = tk.Frame(tab, bg=T.BG_APP)
        cards_frame.pack(fill="x", padx=T.PAD_MD, pady=T.PAD_MD)

        self._metric_labels: Dict[str, tk.Label] = {}
        for label, init_value, color in [
            ("Balance", "0.00 USDT", T.PRIMARY),
            ("Open P&L", "0.00 USDT", T.SUCCESS_DARK),
            ("Positions", "0 / 5", T.INFO),
            ("Win Rate", "0%", T.WARNING),
        ]:
            card = tk.Frame(
                cards_frame, bg=T.BG_PANEL, highlightthickness=1, highlightbackground=T.BORDER,
            )
            card.pack(side="left", expand=True, fill="both", padx=T.PAD_SM, pady=T.PAD_SM)
            tk.Label(
                card, text=label, font=T.font(size=T.FONT_XS),
                bg=T.BG_PANEL, fg=T.TEXT_MUTED,
            ).pack(pady=(T.PAD_SM, 0))
            lbl = tk.Label(
                card, text=init_value, font=T.font(size=T.FONT_XL, weight="bold"),
                bg=T.BG_PANEL, fg=color,
            )
            lbl.pack(pady=(0, T.PAD_SM))
            self._metric_labels[label] = lbl

        timer_frame = tk.Frame(tab, bg=T.BG_PANEL, highlightthickness=1, highlightbackground=T.BORDER)
        timer_frame.pack(fill="x", padx=T.PAD_MD, pady=(0, T.PAD_MD))
        tk.Label(
            timer_frame, text="⏱ Running Time:", font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
        ).pack(side="left", padx=T.PAD_SM, pady=T.PAD_SM)
        self._timer_label = tk.Label(
            timer_frame, text="Not started", font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
        )
        self._timer_label.pack(side="left", padx=T.PAD_SM)

        log_frame = tk.Frame(tab, bg="#0A1628", highlightthickness=1, highlightbackground=T.PRIMARY)
        log_frame.pack(fill="both", expand=True, padx=T.PAD_MD, pady=(0, T.PAD_MD))
        tk.Label(
            log_frame, text="📋  Bot Log", font=T.font(size=T.FONT_SM, weight="bold"),
            bg="#0D1F3C", fg=T.PRIMARY_LIGHT, anchor="w", padx=T.PAD_MD, pady=T.PAD_SM,
        ).pack(fill="x")
        self._log_text = scrolledtext.ScrolledText(
            log_frame, height=10, wrap=tk.WORD,
            font=T.font(size=T.FONT_SM, family=T.FONT_FAMILY_MONO),
            state="disabled", bg="#0A1628", fg=T.PRIMARY_LIGHT,
            insertbackground=T.PRIMARY_LIGHT, borderwidth=0,
        )
        self._log_text.pack(fill="both", expand=True, padx=2, pady=2)
        for tag, fg in [("info", T.PRIMARY_LIGHT), ("success", T.SUCCESS),
                        ("warning", T.WARNING), ("error", T.DANGER)]:
            self._log_text.tag_config(tag, foreground=fg)

    def _build_open_trades_tab(self) -> None:
        tab = tk.Frame(self._nb, bg=T.BG_APP)
        self._nb.add(tab, text="📈 Open Trades")
        frame, self._open_tree = make_tree(
            tab,
            columns=("Symbol", "Side", "Entry", "Current", "P&L", "Trailing SL", "Age"),
            widths=[90, 65, 90, 90, 85, 90, 70],
            height=12,
            return_frame=True,
        )
        frame.pack(fill="both", expand=True)
        btn_bar = tk.Frame(tab, bg=T.BG_APP)
        btn_bar.pack(pady=T.PAD_MD)
        primary_btn(btn_bar, "❌ Close Selected", self._close_selected, bg=T.DANGER).pack(side="left", padx=T.PAD_SM)
        primary_btn(btn_bar, "⚠ Close All", self._close_all, bg=T.DANGER).pack(side="left", padx=T.PAD_SM)

    def _build_history_tab(self) -> None:
        tab = tk.Frame(self._nb, bg=T.BG_APP)
        self._nb.add(tab, text="📜 History")
        frame, self._history_tree = make_tree(
            tab,
            columns=("Time", "Symbol", "Side", "Entry", "Exit", "P&L", "ROI %"),
            widths=[130, 85, 65, 90, 90, 85, 75],
            height=14,
            return_frame=True,
        )
        frame.pack(fill="both", expand=True)
        self._history_tree.tag_configure(
            "profit", background="#D4EDDA", foreground="#155724", font=("Segoe UI", 10, "bold"),
        )
        self._history_tree.tag_configure(
            "loss", background="#F8D7DA", foreground="#721C24", font=("Segoe UI", 10),
        )
        btn_bar = tk.Frame(tab, bg=T.BG_APP)
        btn_bar.pack(pady=T.PAD_MD)
        primary_btn(btn_bar, "💾 Export CSV", self._export_history, bg=T.SUCCESS).pack(side="left", padx=T.PAD_SM)

    def _build_signals_tab(self) -> None:
        tab = tk.Frame(self._nb, bg=T.BG_APP)
        self._nb.add(tab, text="📡 Signals")
        toggle_bar = tk.Frame(tab, bg=T.BG_PANEL, highlightthickness=1, highlightbackground=T.BORDER)
        toggle_bar.pack(fill="x", padx=T.PAD_MD, pady=T.PAD_MD)
        inner = tk.Frame(toggle_bar, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=T.PAD_XL, pady=T.PAD_SM)
        self._auto_var = tk.BooleanVar(value=False)
        cb = tk.Checkbutton(
            inner, text="🤖  Enable Auto-Trade (Pump Entries)",
            variable=self._auto_var, command=self._toggle_auto_trade,
            font=T.font(size=T.FONT_MD, weight="bold"), bg=T.BG_PANEL,
            fg=T.TEXT_PRIMARY, selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL, cursor="hand2",
        )
        cb.pack(side="left")
        ToolTip(cb, "Queue scanner pump signals (simulator only). Live Nobitex entries stay on Global Lead.")
        self._auto_status_lbl = tk.Label(
            inner, text="(Disabled)", font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
        )
        self._auto_status_lbl.pack(side="left", padx=T.PAD_MD)

        frame, self._signals_tree = make_tree(
            tab,
            columns=("Time", "Symbol", "Signal", "Risk", "Price", "Status"),
            widths=[130, 90, 90, 70, 90, 90],
            height=14,
            return_frame=True,
        )
        frame.pack(fill="both", expand=True)
        self._signals_tree.tag_configure("buy", background=T.SUCCESS_BG, foreground=T.SUCCESS_DARK)

        btn_bar = tk.Frame(tab, bg=T.BG_APP)
        btn_bar.pack(pady=T.PAD_MD)
        primary_btn(btn_bar, "🗑 Clear", self.clear_signals, bg=T.DANGER).pack(side="left", padx=T.PAD_SM)
        primary_btn(btn_bar, "📤 Trade Selected", self._manual_trade, bg=T.PRIMARY).pack(side="left", padx=T.PAD_SM)

    def _build_status_bar(self) -> None:
        self._status_bar = tk.Label(
            self, text="🔴  Bot: Stopped", anchor="w",
            bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
            font=T.font(size=T.FONT_SM), padx=T.PAD_XL,
        )
        self._status_bar.pack(side="bottom", fill="x")

    def start_bot(self) -> None:
        if not self.bot or not self._bot_ready:
            self.log("Start ignored: bot is still initializing.", "warning")
            return
        try:
            self._apply_config_to_tracker()
            if self._is_live_exchange():
                current = normalize_execution_mode(getattr(self.app, "execution_mode", PAPER))
                if current != LIVE:
                    ok = ConfirmDialog.ask(
                        self,
                        title="Switch to Live trading?",
                        message="Paper entries will stop. Start will send real Nobitex orders.",
                        detail="Only continue after paper results look acceptable.",
                        yes_text="Switch to Live and Start",
                        no_text="Stay on Paper",
                        danger=True,
                    )
                    if not ok:
                        self.log("Start cancelled: still in paper mode.", "warning")
                        return
                    setter = getattr(self.app, "set_execution_mode", None)
                    if callable(setter):
                        setter(LIVE, persist=True, reason="real_tab_start")
                self._sync_tracker_balance_from_exchange()
                try:
                    self.tracker.reconcile_open_positions()
                except Exception as e:
                    logger.warning("Reconcile on start failed: %s", e)
            else:
                self._reset_simulator_balance_if_zero()
            if self.tracker:
                self.tracker.trading_halted = False
                self.tracker.halt_reason = ""
                self.tracker.auto_trading_enabled = True
            if not self._using_app_live_tracker:
                self._clear_cooldowns()
            if not self.bot.running:
                self.bot.start()
            if self._is_live_exchange():
                armed = self._sync_live_auto_entries(True)
                if not armed:
                    self.log("Live entries were not armed. Switch to Live first.", "warning")
                    return
                self._auto_var.set(True)
                self._auto_status_lbl.config(text="✅ Global Lead Active", fg=T.SUCCESS_DARK)
                self.auto_trade_enabled = False
            self._bot_start_time = datetime.now()
            self._set_running_ui(True)
            actual_mode = self.bot.exchange_name.upper() if self.bot.exchange_name else "UNKNOWN"
            self.log(f"✅ Bot started. Mode: {actual_mode} | auto entries ON", "success")
            self.refresh_execution_mode()
            if actual_mode == "SIMULATOR" and str(self.config.exchange).lower() != "simulator":
                self.log(
                    "⚠️  WARNING: Bot is running in SIMULATOR mode, not live trading. Check API keys.",
                    "warning",
                )
            self._update_balance_display()
            self._manual_refresh()
        except Exception as exc:
            messagebox.showerror("Start Error", str(exc), parent=self)

    def stop_bot(self) -> None:
        if not self.bot:
            return
        try:
            if self._is_live_exchange():
                self._sync_live_auto_entries(False)
                self.auto_trade_enabled = False
                self._auto_var.set(False)
                self._auto_status_lbl.config(text="(Paused)", fg=T.TEXT_MUTED)
                self._bot_start_time = None
                self._set_running_ui(False)
                self._status_bar.config(
                    text="🔴  Auto entries paused — open positions still monitored",
                    bg=T.PRIMARY,
                )
                self.log("⏹ New live entries paused. Open positions and SL/trail still run.", "warning")
            else:
                if self.bot.running:
                    self.bot.stop()
                self._bot_start_time = None
                self._set_running_ui(False)
                self.log("⏹ Bot stopped.", "warning")
            self._manual_refresh()
        except Exception as exc:
            messagebox.showerror("Stop Error", str(exc), parent=self)

    def _set_running_ui(self, running: bool) -> None:
        if not self._bot_ready:
            self._start_btn.config(state="disabled")
            self._stop_btn.config(state="disabled")
            return
        if running:
            self._start_btn.config(state="disabled")
            self._stop_btn.config(state="normal")
            auto_on = bool(getattr(self.app, "real_auto_enabled", True))
            self._status_bar.config(
                text="🟢  Bot: Running" + ("  |  Auto entries ON" if auto_on else "  |  Auto entries PAUSED"),
                bg=T.SUCCESS_DARK,
            )
        else:
            self._start_btn.config(state="normal")
            self._stop_btn.config(state="disabled")
            self._status_bar.config(text="🔴  Bot: Stopped", bg=T.PRIMARY)

    def _update_balance_display(self) -> None:
        if not self.bot:
            return
        try:
            balance = self._read_quote_balance(fresh=False)
            if balance is None:
                balance = 0.0
            quote = str(getattr(self.config, "quote_currency", "IRT") or "IRT")
            self._balance_label.config(text=f"Balance: {balance:,.2f} {display_quote_label(quote)}")
        except Exception as e:
            logger.error("Failed to update balance display: %s", e)

    def _schedule_refresh(self) -> None:
        if self._closing:
            return
        self._refresh_job = self.after(self.REFRESH_INTERVAL_MS, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self._closing:
            return
        self._process_ui_callbacks()
        self.refresh_signals()
        self._queue_signals_for_processing()
        if not self._updating:
            self._updating = True
            threading.Thread(target=self._background_data_update, daemon=True).start()
        self._update_balance_display()
        self._schedule_refresh()

    def _queue_signals_for_processing(self) -> None:
        if not self.auto_trade_enabled:
            return
        if not self.bot or not self.bot.running:
            return
        if self._app_owns_auto_entries():
            return

        signals = getattr(self.app, "latest_signals", []) or []
        pump_signals = [
            dict(s) for s in signals
            if isinstance(s, dict) and self._is_pump_signal(s.get("Signal", ""))
        ]
        if not pump_signals:
            return

        supported_signals = []
        for sig in pump_signals:
            symbol = str(sig.get("Pair") or sig.get("Symbol") or "")
            if not symbol:
                continue
            if self._is_symbol_unsupported(symbol):
                continue
            payload = dict(sig)
            payload["Symbol"] = self._base_symbol(symbol) or symbol
            supported_signals.append(payload)

        if not supported_signals:
            return

        for sig in supported_signals:
            symbol = str(sig.get("Symbol") or "")
            key = symbol + "_" + str(sig.get("Price") or "")
            if key not in self._processed_signal_keys:
                self._signal_queue.put([sig])
                self._processed_signal_keys.add(key)
                self.log(f"Queued signal for {key}", "success")

    def _background_data_update(self) -> None:
        try:
            if self.tracker:
                status = dict(self.tracker.get_status() or {})
                trades = self.tracker.get_open_trades()
                rows = []
                open_pnl = 0.0
                for t in trades:
                    symbol = t.get("symbol", "")
                    entry = float(t.get("entry_price", 0) or 0)
                    size = float(t.get("position_size", 0) or 0)
                    cur_price = entry
                    if self.bot and str(symbol).upper() not in self._unsupported_symbols:
                        try:
                            ticker = self.bot.get_ticker(symbol) or {}
                            cur_price = float(ticker.get("last") or ticker.get("price") or entry)
                        except Exception:
                            cur_price = entry
                    pnl_pct = ((cur_price - entry) / entry * 100) if entry > 0 else 0.0
                    open_pnl += (cur_price - entry) * size
                    rows.append((
                        symbol, "LONG", f"{entry:.4f}", f"{cur_price:.4f}",
                        f"{pnl_pct:+.2f}%", f"{float(t.get('current_stop_loss', 0) or 0):.4f}",
                        str(t.get("entry_time", "—"))[:16],
                    ))
                status["total_pnl_amount"] = open_pnl
                self._ui_callback_queue.put(lambda s=status: self._update_status_ui(s))
                self._ui_callback_queue.put(lambda r=rows: self._update_open_trades_ui(r))

                history = self.tracker.get_enriched_trades()
                closed_trades = [t for t in history if t.get("status") == "closed"]
                closed_trades.sort(
                    key=lambda x: x.get("exit_time") or x.get("entry_time") or "",
                    reverse=True,
                )
                hist_rows = []
                for t in closed_trades[-100:]:
                    pnl = float(t.get("_pnl_amount", 0) or 0)
                    hist_rows.append((
                        t.get("entry_time", "—"), t.get("symbol", "—"),
                        "LONG", f"{float(t.get('_entry', 0) or 0):.4f}",
                        f"{float(t.get('_dp', 0) or 0):.4f}", f"{pnl:+.2f}",
                        f"{float(t.get('_pnl', 0) or 0):+.2f}%", pnl,
                    ))
                self._ui_callback_queue.put(lambda h=hist_rows: self._update_history_ui(h))
        except Exception as e:
            logger.error("Background data update failed: %s", e)
        finally:
            self._updating = False

    def _schedule_timer(self) -> None:
        if self._closing:
            return
        self._update_timer_label()
        self._timer_job = self.after(self.TIMER_UPDATE_MS, self._schedule_timer)

    def _update_timer_label(self) -> None:
        if self._bot_start_time and self.bot and self.bot.running:
            elapsed = datetime.now() - self._bot_start_time
            hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
            minutes, seconds = divmod(remainder, 60)
            self._timer_label.config(text=f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        else:
            self._timer_label.config(text="Not started")

    def _update_status_ui(self, status: Dict[str, Any]) -> None:
        try:
            equity = status.get("equity", 0) or 0.0
            open_pnl = status.get("total_pnl_amount", 0) or 0.0
            pos_count = status.get("open_trades", 0) or 0
            max_pos = getattr(self.config, "max_open_positions", 5)
            win_rate = status.get("win_rate_pct", 0) or 0.0
            quote = display_quote_label(getattr(self.config, "quote_currency", "IRT"))
            self._metric_labels["Balance"].config(text=f"{equity:.2f} {quote}")
            pnl_color = T.SUCCESS_DARK if open_pnl >= 0 else T.DANGER_DARK
            self._metric_labels["Open P&L"].config(
                text=f"{open_pnl:+.2f} {quote}", fg=pnl_color,
            )
            self._metric_labels["Positions"].config(text=f"{pos_count} / {max_pos}")
            wr_color = T.SUCCESS_DARK if win_rate >= 50 else T.WARNING
            self._metric_labels["Win Rate"].config(text=f"{win_rate:.2f}%", fg=wr_color)
            self._balance_label.config(text=f"Balance: {equity:,.2f} {quote}")
        except Exception as e:
            logger.error("Update status UI failed: %s", e)

    def _update_open_trades_ui(self, rows: list) -> None:
        try:
            self._open_tree.delete(*self._open_tree.get_children())
            for row in rows:
                pnl_str = str(row[4]).replace("%", "").replace("+", "")
                try:
                    pnl_val = float(pnl_str)
                except (ValueError, TypeError):
                    pnl_val = 0.0
                tag = "profit" if pnl_val >= 0 else "loss"
                self._open_tree.insert("", "end", values=row, tags=(tag,))
            self._open_tree.tag_configure("profit", foreground=T.SUCCESS_DARK)
            self._open_tree.tag_configure("loss", foreground=T.DANGER_DARK)
        except Exception as e:
            logger.error("Update open trades UI failed: %s", e)

    def _update_history_ui(self, rows: list) -> None:
        try:
            self._history_tree.delete(*self._history_tree.get_children())
            for row in rows:
                pnl = row[-1]
                tag = "profit" if pnl > 0 else "loss"
                self._history_tree.insert("", "end", values=row[:-1], tags=(tag,))
        except Exception as e:
            logger.error("Update history UI failed: %s", e)

    def log(self, message: str, level: str = "info") -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_queue.put((f"[{ts}] {message}\n", level))
        if self.winfo_exists():
            self.after(0, self._flush_log_queue)

    def _flush_log_queue(self) -> None:
        try:
            while True:
                msg, level = self._log_queue.get_nowait()
                self._log_text.config(state="normal")
                self._log_text.insert(tk.END, msg, level)
                self._log_text.see(tk.END)
                self._log_text.config(state="disabled")
        except queue.Empty:
            pass
        except Exception:
            pass

    def _is_pump_signal(self, signal_name: str) -> bool:
        match = self._PUMP_RE.search(str(signal_name or ""))
        if not match:
            return False
        try:
            pct = float(match.group(1))
            return pct >= self.pump_threshold_pct
        except ValueError:
            return False

    def refresh_signals(self) -> None:
        try:
            signals = getattr(self.app, "latest_signals", []) or []
            self._signals_tree.delete(*self._signals_tree.get_children())
            for sig in signals:
                if not isinstance(sig, dict):
                    continue
                signal_name = sig.get("Signal", "")
                if not self._is_pump_signal(signal_name):
                    continue
                display_symbol = sig.get("Pair") or sig.get("Symbol", "—")
                base = self._base_symbol(str(display_symbol))
                if base in self._unsupported_symbols:
                    status_txt = "⛔ Unsupported"
                else:
                    status_txt = "✅ Ready"
                price = sig.get("Price", 0)
                self._signals_tree.insert("", "end", values=(
                    datetime.now().strftime("%H:%M:%S"),
                    display_symbol,
                    signal_name,
                    sig.get("Risk", "—"),
                    f"{float(price):.4f}" if price else "—",
                    status_txt,
                ), tags=("buy",))
        except Exception as e:
            logger.error("refresh_signals error: %s", e)

    def clear_signals(self) -> None:
        self._signals_tree.delete(*self._signals_tree.get_children())

    def _manual_trade(self) -> None:
        selection = self._signals_tree.selection()
        if not selection:
            return
        try:
            values = self._signals_tree.item(selection[0]).get("values", [])
            if len(values) < 5:
                return
            symbol = str(values[1])
            signal = str(values[2])
            try:
                table_price = float(values[4])
            except (TypeError, ValueError):
                table_price = 0.0
            self._execute_manual_trade(symbol, signal, table_price)
        except Exception as e:
            self.log(f"Manual trade error: {e}", "error")

    def _execute_manual_trade(self, symbol: str, signal: str, price: float) -> None:
        if not self.tracker or not self.bot:
            self.log("Cannot trade: tracker or bot not ready.", "error")
            return
        if self._is_live_exchange():
            mode = normalize_execution_mode(getattr(self.app, "execution_mode", PAPER))
            if mode != LIVE or not bool(getattr(self.app, "real_auto_enabled", False)):
                messagebox.showwarning(
                    "Live entries off",
                    "Paper and live cannot run together.\n"
                    "Switch to Live, then press Start, then send a real order.",
                    parent=self,
                )
                return
        elif not self.bot.running:
            messagebox.showwarning(
                "Bot Stopped",
                "Start the bot before sending a live order.",
                parent=self,
            )
            return
        data = self._build_manual_payload(symbol, signal, price)
        if data is None:
            return
        key = str(data.get("Symbol")) + "_" + str(data.get("Price")) + "_manual"
        if key in self._processed_signal_keys:
            self.log(f"Manual trade already queued for {data.get('Symbol')}.", "warning")
            return
        self._processed_signal_keys.add(key)
        self._signal_queue.put([data])
        self.log(
            f"Queued MANUAL BUY {data.get('Symbol')} @ {float(data.get('Price') or 0):,.4f}",
            "success",
        )

    def _build_manual_payload(
        self, symbol: str, signal: str, table_price: float,
    ) -> Optional[Dict[str, Any]]:
        if self._is_live_exchange():
            live = self._lookup_nobitex_market(symbol)
            if not live:
                messagebox.showwarning(
                    "Not on exchange",
                    f"{symbol} is not available on the live {self.bot.exchange_name} market.\n"
                    "Scanner prices are informational; live orders use exchange quotes only.",
                    parent=self,
                )
                return None
            ask = float(live.get("Ask") or live.get("Price") or 0)
            if ask <= 0:
                messagebox.showwarning(
                    "No Live Price",
                    f"Could not read a live ask/last for {symbol}.",
                    parent=self,
                )
                return None
            payload = dict(live)
            payload.update({
                "Symbol": str(live.get("Symbol") or self._base_symbol(symbol)).upper(),
                "symbol": str(live.get("Symbol") or self._base_symbol(symbol)).upper(),
                "AssetKey": live.get("AssetKey") or f"nobitex:{self._base_symbol(symbol).lower()}",
                "Pair": live.get("Pair") or symbol,
                "Signal": "buy",
                "signal": "buy",
                "Price": ask,
                "price": ask,
                "source": "Manual",
                "_manual": True,
            })
            return payload
        return {
            "Symbol": symbol,
            "Pair": symbol,
            "Signal": signal or "buy",
            "Price": table_price,
            "source": "Manual",
            "_manual": True,
        }

    def _signal_processor_loop(self) -> None:
        while not self._closing:
            try:
                item = self._signal_queue.get(timeout=0.5)
                if item is None:
                    break
                self._process_signal(item)
                for sig in item:
                    key = str(sig.get("Symbol") or sig.get("Pair") or "") + "_" + str(sig.get("Price") or "")
                    self._processed_signal_keys.discard(key)
                    self._processed_signal_keys.discard(key + "_manual")
            except queue.Empty:
                continue
            except Exception as e:
                self.log(f"Signal processor error: {e}", "error")

    def _process_signal(self, signals: List[Dict]) -> None:
        if not self.tracker or not self.bot or not self.bot.running:
            self.log("Cannot process signal: tracker or bot not ready.", "error")
            return
        manual = any(bool(s.get("_manual")) for s in signals if isinstance(s, dict))
        if self._using_app_live_tracker:
            mode = normalize_execution_mode(getattr(self.app, "execution_mode", PAPER))
            if mode != LIVE or not getattr(self.tracker, "allow_new_entries", False):
                self.log("Ignored live signal: Paper is active or live entries are paused.", "warning")
                return
        elif not manual and not self.auto_trade_enabled:
            self.log("Auto-trade is disabled, ignoring scanner signal.", "warning")
            return
        with self._signal_lock:
            was = self.tracker.auto_trading_enabled
            self.tracker.auto_trading_enabled = True
            try:
                self.log(
                    f"Processing {len(signals)} signal(s)... | halted={self.tracker.trading_halted} | manual={manual}",
                    "info",
                )
                result = self.tracker.process_new_signals(signals)
                self.log(f"Process result: {result} | halted={self.tracker.trading_halted}", "info")
                if result.get("opened", 0) == 0 and signals:
                    self.log(f"No trades opened from {len(signals)} signal(s).", "info")
            except Exception as e:
                logger.error("process_new_signals failed: %s", e, exc_info=True)
                self.log(f"Signal processing failed: {e}", "error")
            finally:
                if self._using_app_live_tracker:
                    self.tracker.auto_trading_enabled = True
                else:
                    self.tracker.auto_trading_enabled = was

    def _toggle_auto_trade(self) -> None:
        wanted = bool(self._auto_var.get())
        if wanted and (not self.bot or not self.bot.running or not self._bot_ready):
            self._auto_var.set(False)
            self.auto_trade_enabled = False
            messagebox.showwarning(
                "Auto-Trade", "Press Start first, then enable auto-trade.", parent=self,
            )
            return
        if self._is_live_exchange():
            self.auto_trade_enabled = False
            self._sync_live_auto_entries(wanted)
            if wanted:
                self._auto_status_lbl.config(text="✅ Global Lead Active", fg=T.SUCCESS_DARK)
                self._set_running_ui(True)
                self.log("Auto entries ON (Nobitex local momentum).", "success")
            else:
                self._auto_status_lbl.config(text="(Paused)", fg=T.TEXT_MUTED)
                self.log("Auto entries paused. Open positions still monitored.", "info")
            return
        self.auto_trade_enabled = wanted
        if wanted:
            self._auto_status_lbl.config(text="✅ Active", fg=T.SUCCESS_DARK)
            self.log("Auto-trade enabled", "success")
        else:
            self._auto_status_lbl.config(text="(Disabled)", fg=T.TEXT_MUTED)
            self.log("Auto-trade disabled", "info")
        if self.tracker:
            self.tracker.auto_trading_enabled = wanted

    def _close_selected(self) -> None:
        selection = self._open_tree.selection()
        if not selection:
            return
        item = self._open_tree.item(selection[0])
        values = item.get("values", [])
        if not values:
            return
        symbol = values[0]
        if ConfirmDialog.ask(self, "Close Trade", f"Close trade for {symbol}?", danger=True):
            if self.tracker and self.bot:
                for t in self.tracker.get_open_trades():
                    if t.get("symbol") == symbol:
                        trade_id = t.get("id")
                        if trade_id is None:
                            continue
                        try:
                            ticker = self.bot.get_ticker(symbol) or {}
                            price = float(ticker.get("last") or ticker.get("price") or 0)
                        except Exception:
                            price = 0.0
                        threading.Thread(
                            target=self._close_trade_worker,
                            args=(trade_id, price, "Manual Close"),
                            daemon=True,
                        ).start()
                        break
        self.after(1500, self._manual_refresh)

    def _close_all(self) -> None:
        if not ConfirmDialog.ask(self, "Close All", "Close ALL open positions?", danger=True):
            return
        if not self.tracker or not self.bot:
            return
        for t in self.tracker.get_open_trades():
            symbol = t.get("symbol", "")
            trade_id = t.get("id")
            if trade_id is None:
                continue
            try:
                ticker = self.bot.get_ticker(symbol) or {}
                price = float(ticker.get("last") or ticker.get("price") or 0)
            except Exception:
                price = 0.0
            threading.Thread(
                target=self._close_trade_worker,
                args=(trade_id, price, "Manual Close All"),
                daemon=True,
            ).start()
        self.after(2000, self._manual_refresh)

    def _close_trade_worker(self, trade_id: int, price: float, reason: str) -> None:
        try:
            ok = self.tracker.close_trade(trade_id, current_price=price, reason=reason)
            self.log(
                f"{'Closed' if ok else 'Close failed'} trade id={trade_id} ({reason})",
                "success" if ok else "error",
            )
        except Exception as exc:
            logger.error("close_trade failed: %s", exc, exc_info=True)
            self.log(f"Close failed: {exc}", "error")

    def _export_history(self) -> None:
        items = self._history_tree.get_children()
        if not items:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV files", "*.csv")],
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Time", "Symbol", "Side", "Entry", "Exit", "P&L", "ROI %"])
                for item in items:
                    writer.writerow(self._history_tree.item(item)["values"])
        except Exception as e:
            logger.error("Export failed: %s", e)
            messagebox.showerror("Export Error", str(e), parent=self)

    def open_bot_settings(self) -> None:
        BotSettingsWindow(self, self)

    def on_close(self):
        self._closing = True
        self.auto_trade_enabled = False
        self._signal_queue.put(None)
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
        if self._timer_job:
            try:
                self.after_cancel(self._timer_job)
            except Exception:
                pass
        if self._accounting_refresh_job:
            try:
                self.after_cancel(self._accounting_refresh_job)
            except Exception:
                pass

    def _manual_refresh(self):
        self._process_ui_callbacks()
        self.refresh_signals()
        self._queue_signals_for_processing()
        self._update_balance_display()
        if not self._updating:
            self._updating = True
            threading.Thread(target=self._background_data_update, daemon=True).start()


# ══════════════════════════════════════════════════════════════
# Bot Settings Window  (v7.5 — no hardcoded DEFAULT_SETTINGS)
# ══════════════════════════════════════════════════════════════

class BotSettingsWindow(BaseDialog):
    """
    Full bot configuration dialog.

    v7.5:
      - Removed the local `DEFAULT_SETTINGS` dict.  Default values now
        come from a fresh `BotConfig()` instance, so there is exactly
        ONE source of truth for defaults (the dataclass itself).
    """

    _RISK_FIELDS: List[Tuple[str, str, float]] = [
        ("Risk per trade %", "risk_per_trade_pct", 1.0),
        ("Max positions", "max_open_positions", 5),
        ("Stop Loss %", "stop_loss_pct", 1.8),
        ("Trailing Distance %", "trailing_distance_pct", 0.5),
        ("Trail activation %", "trailing_activation_pct", 0.8),
        ("Max daily loss %", "max_drawdown_percent", 10.0),
    ]

    def __init__(self, parent: tk.Widget, panel: RealTradingPanel):
        super().__init__(
            parent, panel.app, title="⚙️ Bot Settings", width=720, height=780,
            resizable=(True, True),
        )
        self.panel = panel
        self.config = panel.config

        self.canvas = tk.Canvas(self.body, bg=T.BG_APP, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.body, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=T.BG_APP)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )

        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._build_content()
        self._add_separator()
        self._add_ok_cancel(ok_text="💾 Save", cancel_text="Cancel", ok_command=self._save)

        if hasattr(self, "button_frame"):
            reset_btn = tk.Button(
                self.button_frame, text="🔄 Default Settings",
                font=T.font(size=T.FONT_SM, weight="bold"),
                bg=T.INFO, fg=T.TEXT_ON_PRIMARY, relief="flat", cursor="hand2",
                padx=T.PAD_LG, pady=T.PAD_SM, bd=0, command=self._reset_to_defaults,
            )
            reset_btn.pack(side="left", padx=T.PAD_SM, pady=T.PAD_MD)

    def _build_content(self) -> None:
        self._build_execution_mode_section(self.scrollable_frame)
        self._build_exchange_section(self.scrollable_frame)
        self._build_capital_section(self.scrollable_frame)
        self._build_strategy_section(self.scrollable_frame)
        self._build_risk_section(self.scrollable_frame)
        self._build_extra_section(self.scrollable_frame)

    def _build_execution_mode_section(self, parent) -> None:
        mf = tk.LabelFrame(
            parent, text="🧪  Execution mode (paper XOR live)",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY, padx=T.PAD_MD, pady=T.PAD_MD,
        )
        mf.pack(fill="x", pady=(0, T.PAD_MD))
        current = normalize_execution_mode(getattr(self.config, "execution_mode", PAPER))
        self._execution_mode_var = tk.StringVar(value=current)
        tk.Label(
            mf,
            text="Paper and live never open together. Run paper first. When satisfied, switch to Live, then press Start.",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP, fg=T.TEXT_MUTED, wraplength=620, justify="left",
        ).pack(anchor="w", pady=(0, T.PAD_SM))
        row = tk.Frame(mf, bg=T.BG_APP)
        row.pack(anchor="w")
        ttk.Radiobutton(
            row, text="Paper only (no Nobitex orders)",
            variable=self._execution_mode_var, value=PAPER,
        ).pack(side="left", padx=(0, T.PAD_LG))
        ttk.Radiobutton(
            row, text="Live Nobitex (stops paper entries)",
            variable=self._execution_mode_var, value=LIVE,
        ).pack(side="left")

    def _quote_label(self) -> str:
        quote = ""
        if hasattr(self, "_quote_currency_var"):
            quote = str(self._quote_currency_var.get() or "")
        if not quote:
            quote = str(getattr(self.config, "quote_currency", "IRT") or "IRT")
        return display_quote_label(quote)

    def _money_to_display(self, value: float) -> str:
        amount = float(value or 0.0)
        if abs(amount) >= 100:
            return f"{amount:,.0f}"
        return f"{amount:.4f}".rstrip("0").rstrip(".")

    def _money_from_display(self, text: str, default: float) -> float:
        return parse_amount(text, default)

    def _build_strategy_section(self, parent) -> None:
        sf = tk.LabelFrame(
            parent, text="🎯  Real movement — ride forming trends",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY, padx=T.PAD_MD, pady=T.PAD_MD,
        )
        sf.pack(fill="x", pady=(0, T.PAD_MD))
        sf.columnconfigure(1, weight=1)

        tk.Label(
            sf,
            text="Enter forming moves early. Strong 1h pumps skip extra confirm. Tight trail locks small winners.",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP, fg=T.TEXT_MUTED,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, T.PAD_SM))

        self._strategy_vars: Dict[str, tk.Variable] = {}
        row = 1
        float_fields = [
            ("pump_threshold_pct", "Nobitex momentum threshold (%)", 1.2),
            ("min_observed_move_pct", "Min observed Nobitex move (%)", 0.7),
            ("max_nobitex_spread_pct", "Max Nobitex spread (%)", 1.2),
            ("min_volume_irt", "Min Nobitex 24h volume (IRT)", 300000000.0),
            ("max_market_data_age_sec", "Max Nobitex market-data age (sec)", 30.0),
            ("btc_max_dump_pct", "Skip alts if BTC dumps more than (%)", 1.0),
        ]
        int_fields = [
            ("check_interval_seconds", "Live scan interval (sec)", 15),
            ("movement_lookback_scans", "Lookback scans for observed move", 4),
            ("min_confirm_scans", "Trend confirm scans (hold the move)", 1),
            ("max_new_entries_per_cycle", "Max new entries per scan", 1),
        ]
        for key, label, default in float_fields:
            tk.Label(sf, text=label + ":", font=T.font(size=T.FONT_SM),
                     bg=T.BG_APP, fg=T.TEXT_SECONDARY).grid(
                row=row, column=0, sticky="w", pady=T.PAD_XS)
            var = tk.DoubleVar(value=float(getattr(self.config, key, default)))
            ttk.Entry(sf, textvariable=var, width=14).grid(
                row=row, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
            self._strategy_vars[key] = var
            row += 1
        for key, label, default in int_fields:
            tk.Label(sf, text=label + ":", font=T.font(size=T.FONT_SM),
                     bg=T.BG_APP, fg=T.TEXT_SECONDARY).grid(
                row=row, column=0, sticky="w", pady=T.PAD_XS)
            var = tk.IntVar(value=int(getattr(self.config, key, default)))
            ttk.Entry(sf, textvariable=var, width=14).grid(
                row=row, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)
            self._strategy_vars[key] = var
            row += 1

        tk.Label(
            sf,
            text="Live Start on the Real tab sends Nobitex orders. Saving Live here only switches mode; entries stay paused until Start.",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP, fg=T.TEXT_MUTED, wraplength=620, justify="left",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(T.PAD_SM, 0))

    def _build_exchange_section(self, parent) -> None:
        ef = tk.LabelFrame(
            parent, text="💱  Exchange", font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY, padx=T.PAD_MD, pady=T.PAD_MD,
        )
        ef.pack(fill="x", pady=(0, T.PAD_MD))
        ef.columnconfigure(1, weight=1)

        tk.Label(ef, text="Exchange:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_APP, fg=T.TEXT_SECONDARY).grid(row=0, column=0, sticky="w", pady=T.PAD_XS)
        self._exchange_var = tk.StringVar(value="nobitex")
        self._exchange_cb = AutocompleteCombobox(ef, textvariable=self._exchange_var, width=22, state="readonly")
        self._exchange_cb.set_completion_list(get_nobitex_exchanges())
        self._exchange_cb.grid(row=0, column=1, sticky="ew", padx=T.PAD_SM, pady=T.PAD_XS)

        tk.Label(ef, text="Nobitex public key:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_APP, fg=T.TEXT_SECONDARY).grid(row=1, column=0, sticky="w", pady=T.PAD_XS)
        self._api_key_var = tk.StringVar(value=self.config.api_key or "")
        ttk.Entry(ef, textvariable=self._api_key_var, width=32).grid(
            row=1, column=1, sticky="ew", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(ef, text="Nobitex private key:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_APP, fg=T.TEXT_SECONDARY).grid(row=2, column=0, sticky="w", pady=T.PAD_XS)
        self._api_secret_var = tk.StringVar(value=self.config.api_secret or "")
        ttk.Entry(ef, textvariable=self._api_secret_var, width=32, show="*").grid(
            row=2, column=1, sticky="ew", padx=T.PAD_SM, pady=T.PAD_XS
        )
        tk.Label(
            ef,
            text="Create the key in Nobitex with permissions READ,TRADE only (no WITHDRAW). "
            "Paste field key as the public key and the one-time privateKey. "
            "PC clock must stay within 30 seconds of UTC.",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP, fg=T.TEXT_MUTED, wraplength=520, justify="left",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=T.PAD_SM, pady=(0, T.PAD_SM))

        self._testnet_var = tk.BooleanVar(value=self.config.testnet)
        ttk.Checkbutton(ef, text="Use Testnet", variable=self._testnet_var).grid(
            row=4, column=1, sticky="w", pady=2
        )

        if str(self.config.exchange).lower() == "nobitex":
            tk.Label(ef, text="Market:", font=T.font(size=T.FONT_SM),
                     bg=T.BG_APP, fg=T.TEXT_SECONDARY).grid(row=5, column=0, sticky="w", pady=T.PAD_XS)
            self._nobitex_market_var = tk.StringVar(value=getattr(self.config, "nobitex_market", "IRT"))
            ttk.Combobox(
                ef, textvariable=self._nobitex_market_var, values=["IRT", "USDT"],
                state="readonly", width=10,
            ).grid(row=5, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)

    def _build_capital_section(self, parent) -> None:
        cf = tk.LabelFrame(
            parent, text="💰  Capital & Currency",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY, padx=T.PAD_MD, pady=T.PAD_MD,
        )
        cf.pack(fill="x", pady=(0, T.PAD_MD))
        cf.columnconfigure(1, weight=1)

        tk.Label(
            cf, text=f"Account balance ({self._quote_label()}):", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=0, column=0, sticky="w", pady=T.PAD_XS)
        self._account_balance_var = tk.StringVar(
            value=self._money_to_display(float(getattr(self.config, "account_balance", 10000000.0)))
        )
        ttk.Entry(cf, textvariable=self._account_balance_var, width=18).grid(
            row=0, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(
            cf, text="Quote Currency:", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=1, column=0, sticky="w", pady=T.PAD_XS)
        self._quote_currency_var = tk.StringVar(value=self.config.quote_currency or "IRT")
        ttk.Entry(cf, textvariable=self._quote_currency_var, width=10).grid(
            row=1, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

    def _build_risk_section(self, parent) -> None:
        rf = tk.LabelFrame(
            parent, text="🛡  Risk & Strategy",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY, padx=T.PAD_MD, pady=T.PAD_MD,
        )
        rf.pack(fill="x", pady=(0, T.PAD_MD))
        rf.columnconfigure(1, weight=1)

        self._risk_vars: Dict[str, tk.Variable] = {}
        row = 0
        for label, key, default in self._RISK_FIELDS:
            tk.Label(
                rf, text=label + ":", font=T.font(size=T.FONT_SM),
                bg=T.BG_APP, fg=T.TEXT_SECONDARY,
            ).grid(row=row, column=0, sticky="w", pady=T.PAD_XS)
            val = float(getattr(self.config, key, default))
            var = tk.DoubleVar(value=val)
            ttk.Entry(rf, textvariable=var, width=12).grid(
                row=row, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
            )
            self._risk_vars[key] = var
            row += 1

        tk.Label(
            rf, text="Pump Threshold %:", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=row, column=0, sticky="w", pady=T.PAD_XS)
        self._pump_var = tk.DoubleVar(value=float(getattr(self.config, "pump_threshold_pct", 5.0)))
        ttk.Entry(rf, textvariable=self._pump_var, width=12).grid(
            row=row, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )
        row += 1

        tk.Label(
            rf, text="Take Profit %:", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=row, column=0, sticky="w", pady=T.PAD_XS)
        self._take_profit_var = tk.DoubleVar(value=float(getattr(self.config, "take_profit_percent", 0.0)))
        ttk.Entry(rf, textvariable=self._take_profit_var, width=12).grid(
            row=row, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

    def _build_extra_section(self, parent) -> None:
        ef = tk.LabelFrame(
            parent, text="🔧  Extra Settings",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY, padx=T.PAD_MD, pady=T.PAD_MD,
        )
        ef.pack(fill="x", pady=(0, T.PAD_MD))
        ef.columnconfigure(1, weight=1)

        unit = self._quote_label()
        tk.Label(
            ef, text="Position size mode:", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=0, column=0, sticky="w", pady=T.PAD_XS)
        self._position_size_mode_var = tk.StringVar(
            value=str(getattr(self.config, "position_size_mode", "fixed") or "fixed")
        )
        ttk.Combobox(
            ef, textvariable=self._position_size_mode_var,
            values=["fixed", "risk_percent"], state="readonly", width=16,
        ).grid(row=0, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS)

        tk.Label(
            ef, text=f"Fixed amount per trade ({unit}):", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=1, column=0, sticky="w", pady=T.PAD_XS)
        self._fixed_position_var = tk.StringVar(
            value=self._money_to_display(float(getattr(self.config, "fixed_position_quote", 10000000.0)))
        )
        ttk.Entry(ef, textvariable=self._fixed_position_var, width=18).grid(
            row=1, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(
            ef, text=f"Min order value ({unit}):", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=2, column=0, sticky="w", pady=T.PAD_XS)
        self._min_notional_var = tk.StringVar(
            value=self._money_to_display(float(getattr(self.config, "min_notional_quote", 300000.0)))
        )
        ttk.Entry(ef, textvariable=self._min_notional_var, width=18).grid(
            row=2, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(
            ef, text="Max Position %:", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=3, column=0, sticky="w", pady=T.PAD_XS)
        self._max_position_pct_var = tk.DoubleVar(
            value=float(getattr(self.config, "max_position_pct", 30.0))
        )
        ttk.Entry(ef, textvariable=self._max_position_pct_var, width=15).grid(
            row=3, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(
            ef, text="Min Volume 24h:", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=4, column=0, sticky="w", pady=T.PAD_XS)
        self._min_volume_var = tk.DoubleVar(
            value=float(getattr(self.config, "min_volume_24h", 100000.0))
        )
        ttk.Entry(ef, textvariable=self._min_volume_var, width=15).grid(
            row=4, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(
            ef, text="Cooldown After Loss (min):", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=5, column=0, sticky="w", pady=T.PAD_XS)
        self._cooldown_loss_var = tk.IntVar(
            value=int(getattr(self.config, "cooldown_after_loss_min", 15))
        )
        ttk.Entry(ef, textvariable=self._cooldown_loss_var, width=15).grid(
            row=5, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(
            ef, text="Cooldown After Win (min):", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=6, column=0, sticky="w", pady=T.PAD_XS)
        self._cooldown_win_var = tk.IntVar(
            value=int(getattr(self.config, "cooldown_after_win_min", 15))
        )
        ttk.Entry(ef, textvariable=self._cooldown_win_var, width=15).grid(
            row=6, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        tk.Label(
            ef, text="Confirmation %:", font=T.font(size=T.FONT_SM),
            bg=T.BG_APP, fg=T.TEXT_SECONDARY,
        ).grid(row=7, column=0, sticky="w", pady=T.PAD_XS)
        self._confirmation_pct_var = tk.DoubleVar(
            value=float(getattr(self.config, "confirmation_pct", 0.35))
        )
        ttk.Entry(ef, textvariable=self._confirmation_pct_var, width=15).grid(
            row=7, column=1, sticky="w", padx=T.PAD_SM, pady=T.PAD_XS
        )

        self._confirmation_enabled_var = tk.BooleanVar(
            value=bool(getattr(self.config, "confirmation_enabled", False))
        )
        ttk.Checkbutton(
            ef, text="Wait for an extra price tick before entry (off = don't miss the trend)",
            variable=self._confirmation_enabled_var,
        ).grid(row=8, column=1, sticky="w", pady=T.PAD_XS)

    # ══════════════════════════════════════════════════════════════
    # v7.5: reset uses BotConfig defaults, not a hardcoded dict
    # ══════════════════════════════════════════════════════════════

    def _reset_to_defaults(self) -> None:
        """
        Reset every widget to BotConfig's built-in defaults.

        v7.5: pulls values from a fresh BotConfig() instance instead of
        a hardcoded dict, so there is exactly ONE source of truth for
        default values.
        """
        from trading.bot_config import BotConfig, EAGLE_DEFAULTS

        defaults = BotConfig()

        # ── Exchange / credentials ──
        self._exchange_var.set(defaults.exchange)
        self._api_key_var.set(defaults.api_key)
        self._api_secret_var.set(defaults.api_secret)
        self._testnet_var.set(defaults.testnet)

        # ── Capital ──
        self._account_balance_var.set(
            self._money_to_display(float(defaults.account_balance))
        )
        self._quote_currency_var.set(defaults.quote_currency)

        # ── Risk ──
        self._risk_vars["risk_per_trade_pct"].set(defaults.risk_per_trade_pct)
        self._risk_vars["max_open_positions"].set(defaults.max_open_positions)
        self._risk_vars["stop_loss_pct"].set(defaults.stop_loss_pct)
        self._risk_vars["trailing_distance_pct"].set(defaults.trailing_distance_pct)
        if "trailing_activation_pct" in self._risk_vars:
            self._risk_vars["trailing_activation_pct"].set(defaults.trailing_activation_pct)
        self._risk_vars["max_drawdown_percent"].set(defaults.max_drawdown_percent)

        # ── Strategy thresholds ──
        self._pump_var.set(defaults.pump_threshold_pct)
        self._take_profit_var.set(defaults.take_profit_percent)

        # ── Position sizing ──
        self._fixed_position_var.set(
            self._money_to_display(float(defaults.fixed_position_quote))
        )
        self._max_position_pct_var.set(defaults.max_position_pct)
        self._min_volume_var.set(defaults.min_volume_24h)

        # ── Cooldowns ──
        self._cooldown_loss_var.set(defaults.cooldown_after_loss_min)
        self._cooldown_win_var.set(defaults.cooldown_after_win_min)

        # ── Nobitex market ──
        if hasattr(self, "_nobitex_market_var"):
            self._nobitex_market_var.set(defaults.nobitex_market)

        # ── Real-movement / Eagle strategy vars ──
        # The eagle/momentum engine consumes these keys directly, so we
        # reset them from the canonical defaults (BotConfig + EAGLE_DEFAULTS).
        if hasattr(self, "_strategy_vars"):
            defaults_map = {
                "pump_threshold_pct":        defaults.pump_threshold_pct,
                "min_observed_move_pct":     defaults.min_observed_move_pct,
                "max_nobitex_spread_pct":    defaults.max_nobitex_spread_pct,
                "min_volume_irt":            defaults.min_volume_irt,
                "max_market_data_age_sec":   30.0,
                "btc_max_dump_pct":          defaults.btc_max_dump_pct,
                "check_interval_seconds":    defaults.check_interval_seconds,
                "movement_lookback_scans":   defaults.movement_lookback_scans,
                "min_confirm_scans":         defaults.min_confirm_scans,
                "max_new_entries_per_cycle": defaults.max_new_entries_per_cycle,
                # Eagle exception knobs
                "eagle_min_observed_move_pct": EAGLE_DEFAULTS["eagle_min_observed_move_pct"],
                "eagle_min_1h_pct":           EAGLE_DEFAULTS["eagle_min_1h_pct"],
                "eagle_min_volume_irt":       EAGLE_DEFAULTS["eagle_min_volume_irt"],
                "eagle_max_spread_pct":       EAGLE_DEFAULTS["eagle_max_spread_pct"],
            }
            for key, value in defaults_map.items():
                if key in self._strategy_vars:
                    self._strategy_vars[key].set(value)

        # ── Execution mode / sizing mode ──
        if hasattr(self, "_execution_mode_var"):
            self._execution_mode_var.set(PAPER)
        if hasattr(self, "_position_size_mode_var"):
            self._position_size_mode_var.set(defaults.position_size_mode)

        # ── Notional / confirmation ──
        if hasattr(self, "_min_notional_var"):
            self._min_notional_var.set(
                self._money_to_display(defaults.min_notional_quote)
            )
        if hasattr(self, "_confirmation_enabled_var"):
            self._confirmation_enabled_var.set(defaults.confirmation_enabled)
        if hasattr(self, "_confirmation_pct_var"):
            self._confirmation_pct_var.set(defaults.confirmation_pct)

    @staticmethod
    def _get_float_or(var, default: float) -> float:
        try:
            val = var.get()
            return float(val) if val not in (None, "") else default
        except (ValueError, tk.TclError):
            return default

    @staticmethod
    def _get_int_or(var, default: int) -> int:
        try:
            val = var.get()
            return int(val) if val not in (None, "") else default
        except (ValueError, tk.TclError):
            return default

    def _save(self) -> None:
        self.config.exchange = self._exchange_var.get().strip()
        self.config.api_key = self._api_key_var.get().strip()
        self.config.api_secret = self._api_secret_var.get().strip()
        self.config.testnet = self._testnet_var.get()
        self.config.account_balance = self._money_from_display(
            self._account_balance_var.get(), 10000000.0
        )
        self.config.quote_currency = self._quote_currency_var.get().strip().upper() or "IRT"

        for key, var in self._risk_vars.items():
            raw = var.get()
            try:
                val = int(raw) if key == "max_open_positions" else float(raw)
                setattr(self.config, key, val)
            except (ValueError, tk.TclError):
                logger.warning("Invalid value for %s, using default.", key)

        self.config.pump_threshold_pct = self._get_float_or(self._pump_var, 5.0)
        self.config.take_profit_percent = self._get_float_or(self._take_profit_var, 0.0)
        self.config.fixed_position_quote = self._money_from_display(
            self._fixed_position_var.get(), 10000000.0
        )
        self.config.min_notional_quote = self._money_from_display(
            self._min_notional_var.get() if hasattr(self, "_min_notional_var") else "300000",
            300000.0,
        )
        if hasattr(self, "_position_size_mode_var"):
            self.config.position_size_mode = (
                str(self._position_size_mode_var.get() or "fixed").strip().lower()
            )
        if hasattr(self, "_confirmation_enabled_var"):
            self.config.confirmation_enabled = bool(self._confirmation_enabled_var.get())
        if hasattr(self, "_confirmation_pct_var"):
            self.config.confirmation_pct = self._get_float_or(self._confirmation_pct_var, 0.35)
        self.config.max_position_pct = self._get_float_or(self._max_position_pct_var, 30.0)
        self.config.max_notional_quote = max(
            float(getattr(self.config, "max_notional_quote", 10_000_000.0) or 0.0),
            float(self.config.fixed_position_quote or 0.0),
        )
        self.config.min_volume_24h = self._get_float_or(self._min_volume_var, 100000.0)
        self.config.cooldown_after_loss_min = self._get_int_or(self._cooldown_loss_var, 15)
        self.config.cooldown_after_win_min = self._get_int_or(self._cooldown_win_var, 15)

        if hasattr(self, "_nobitex_market_var"):
            self.config.nobitex_market = self._nobitex_market_var.get()
            if self.config.nobitex_market.upper() == "IRT":
                self.config.quote_currency = "IRT"

        self.config.strategy = "nobitex_momentum"
        self.config.global_signal_source = "Nobitex"
        self.config.quote_unit = "rial"
        mode = normalize_execution_mode(
            self._execution_mode_var.get() if hasattr(self, "_execution_mode_var") else PAPER
        )
        prev_mode = normalize_execution_mode(getattr(self.config, "execution_mode", PAPER))
        if mode == LIVE and prev_mode != LIVE:
            ok = ConfirmDialog.ask(
                self.panel,
                title="Save Live mode?",
                message="Paper entries will stop. Live orders still wait for Start on the Real tab.",
                detail="Do not enable Live until paper results look acceptable.",
                yes_text="Save Live mode",
                no_text="Keep Paper",
                danger=True,
            )
            if not ok:
                mode = PAPER
                if hasattr(self, "_execution_mode_var"):
                    self._execution_mode_var.set(PAPER)
        self.config.execution_mode = mode
        self.config.enable_auto_trading = True
        int_keys = {
            "check_interval_seconds", "movement_lookback_scans",
            "max_new_entries_per_cycle", "min_confirm_scans",
        }
        for key, var in getattr(self, "_strategy_vars", {}).items():
            if key in int_keys:
                setattr(self.config, key, self._get_int_or(var, int(getattr(self.config, key, 1))))
            else:
                setattr(self.config, key, self._get_float_or(var, float(getattr(self.config, key, 0.0))))

        from trading.bot_config import STRATEGY_DEFAULTS_VERSION
        self.config.strategy_defaults_version = max(
            int(getattr(self.config, "strategy_defaults_version", STRATEGY_DEFAULTS_VERSION) or 0),
            STRATEGY_DEFAULTS_VERSION,
        )

        self.panel._save_cfg()
        self.panel.pump_threshold_pct = self.config.pump_threshold_pct
        # v7.5: delegate to centralized sync helper
        self.panel._apply_config_to_tracker()
        app = self.panel.app
        app._bot_cfg = self.config
        app._real_strategy_name = "nobitex_momentum"
        if hasattr(app, "_configure_momentum_engine"):
            try:
                app._configure_momentum_engine(self.config)
            except Exception as exc:
                logger.warning("Could not apply Nobitex momentum settings: %s", exc)
        if hasattr(app, "set_execution_mode"):
            try:
                app.set_execution_mode(mode, persist=False, reason="settings")
            except Exception as exc:
                logger.warning("Could not apply execution mode: %s", exc)
        if hasattr(app, "real_signal_tracker") and app.real_signal_tracker is not None:
            app.real_signal_tracker.max_new_entries_per_cycle = int(
                getattr(self.config, "max_new_entries_per_cycle", 1) or 1
            )
            app.real_signal_tracker.confirmation_enabled = bool(
                getattr(self.config, "confirmation_enabled", False)
            )
            app.real_signal_tracker.confirmation_pct = float(
                getattr(self.config, "confirmation_pct", 0.35)
            )
            app.real_signal_tracker.stop_loss_pct = float(
                getattr(self.config, "stop_loss_pct", 1.8)
            )
            app.real_signal_tracker.trailing_distance_pct = float(
                getattr(self.config, "trailing_distance_pct", 0.5)
            )
            app.real_signal_tracker.trailing_activation_pct = float(
                getattr(self.config, "trailing_activation_pct", 0.8)
            )
            app.real_signal_tracker.take_profit_percent = float(
                getattr(self.config, "take_profit_percent", 0.0) or 0.0
            )
        self.panel.refresh_signals()
        self.panel.refresh_execution_mode()
        messagebox.showinfo(
            "Saved",
            "Bot configuration saved.\n"
            "Paper and live never run together. Live Start sends Nobitex orders.\n"
            "IRT amounts are Rial, same as Nobitex.\n"
            "Restart the bot to apply exchange key changes.",
            parent=self.panel,
        )
        self.close()