# gui/panels/paper_trading_panel.py
from __future__ import annotations

import csv
import json
import logging
import os
import queue
from datetime import datetime
from typing import Any, Dict, List, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from gui.gui_helpers import center_window
from gui.ui_theme import Theme, Styles
from gui.trading_ui_helpers import (
    LIVE_COLS,
    LIVE_WIDTHS,
    safe_pnl,
    safe_stat,
    fmt_price,
    fmt_pnl,
    fmt_dollar,
    make_tree,
)
from core.utils import safe_float
from trading.execution_mode import LIVE, PAPER, normalize_execution_mode

logger = logging.getLogger(__name__)
T = Theme


class QueueLogHandler(logging.Handler):
    """Logging handler that sends records to a queue for GUI display."""

    def __init__(self, log_queue: queue.Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


class PaperTradingPanel(tk.Frame):
    """
    Panel for paper trading: displays trades, stats, and logs.
    All signals from scanner are entered automatically.
    No manual settings or checkboxes.
    """

    AUTO_REFRESH_INTERVAL_MS = 5_000   # ۵ ثانیه
    LOG_FLUSH_INTERVAL_MS = 1_000      # هر ثانیه لاگ‌ها بررسی شوند

    # Default values for paper trade settings (not shown in UI)
    DEFAULT_PAPER_SETTINGS = {
        "size": "10000000",
        "tp": "0",
        "sl": "1.8",
        "max_open": "3",
        "capital": "38000000",
        "trailing_enabled": True,
        "trailing_distance": "0.5",
        "trailing_activation": "0.8",
    }

    def __init__(self, parent: tk.Widget, main_app: Any) -> None:
        super().__init__(parent, bg=T.BG_APP)
        self.main_app = main_app
        self.tracker = getattr(main_app, "signal_tracker", None)

        # مسیر فایل تنظیمات کاغذی در پوشه‌ی data پروژه
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.paper_settings_path = os.path.join(project_root, "data", "paper_trade_settings.json")
        os.makedirs(os.path.dirname(self.paper_settings_path), exist_ok=True)

        self._sort_state: Dict[str, bool] = {}
        self._live_rows: List[Dict[str, Any]] = []

        # صف لاگ برای نمایش در تب Log
        self.log_queue: queue.Queue = queue.Queue()
        self._log_handler: Optional[QueueLogHandler] = None

        # Timer for auto-refresh and log flush
        self._refresh_job: Optional[str] = None
        self._log_flush_job: Optional[str] = None
        self._auto_refresh_active = False

        # Load settings from file (used to update tracker once)
        saved = self._load_paper_settings()
        self._apply_settings_to_tracker(saved)

        # Build UI
        self._build_ui()

        # Attach log handler to capture signals from signal_tracker
        self._setup_log_handler()

        # Initial data load
        if self.tracker:
            self.load_live_data()

        # Start timers
        self._schedule_auto_refresh()
        self._schedule_log_flush()

    # -------------------------------------------------------------------------
    # تنظیمات (بدون نمایش در UI)
    # -------------------------------------------------------------------------
    def _load_paper_settings(self) -> Dict[str, Any]:
        if os.path.exists(self.paper_settings_path):
            try:
                with open(self.paper_settings_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error("Failed to load paper trade settings: %s", e)
        return self.DEFAULT_PAPER_SETTINGS.copy()

    def _apply_settings_to_tracker(self, settings: Dict[str, Any]) -> None:
        if not self.tracker:
            return
        try:
            cfg = getattr(self.main_app, "_bot_cfg", None)
            if cfg is not None:
                obs_need = float(getattr(cfg, "min_observed_move_pct", 0.7) or 0.7)
                self.tracker.pump_threshold_pct = obs_need
                self.tracker.ignore_signal_filters = True
                self.tracker.confirmation_enabled = False
                self.tracker.stop_loss_pct = float(getattr(cfg, "stop_loss_pct", 1.8) or 1.8)
                self.tracker.trailing_distance_pct = float(
                    getattr(cfg, "trailing_distance_pct", 0.5) or 0.5
                )
                self.tracker.trailing_activation_pct = float(
                    getattr(cfg, "trailing_activation_pct", 0.8) or 0.8
                )
                self.tracker.trailing_stop_enabled = True
                self.tracker.take_profit_percent = float(
                    getattr(cfg, "take_profit_percent", 0.0) or 0.0
                )
                self.tracker.max_open_trades = int(
                    getattr(cfg, "max_open_positions", 3) or 3
                )
                self.tracker.position_size_mode = "fixed"
                self.tracker.fixed_position_quote = float(
                    getattr(cfg, "fixed_position_quote", 10000000.0) or 10000000.0
                )
                return
            size = float(settings.get("size", 10000000))
            tp = float(settings.get("tp", 0.0))
            sl = float(settings.get("sl", 1.8))
            max_open = int(settings.get("max_open", 3))
            capital = float(settings.get("capital", 10_000_000))
            trailing_distance = float(settings.get("trailing_distance", 0.5))
            trailing_enabled = bool(settings.get("trailing_enabled", True))

            self.tracker.pump_threshold_pct = float(
                getattr(getattr(self.main_app, "_bot_cfg", None), "min_observed_move_pct", 0.7) or 0.7
            )
            self.tracker.trailing_distance_pct = trailing_distance
            self.tracker.trailing_activation_pct = float(
                settings.get("trailing_activation", 0.8)
            )
            self.tracker.trailing_stop_enabled = trailing_enabled
            self.tracker.ignore_signal_filters = True
            self.tracker.min_volume_24h = 0.0
            self.tracker.min_market_cap = 0.0
            self.tracker.set_paper_trade_params(
                size=size, tp=tp, sl=sl, capital=capital, max_open=max_open
            )
        except Exception as e:
            logger.warning("Failed to apply paper settings to tracker: %s", e)

    # -------------------------------------------------------------------------
    # Log handler setup
    # -------------------------------------------------------------------------
    def _setup_log_handler(self) -> None:
        self._log_handler = QueueLogHandler(self.log_queue)
        formatter = logging.Formatter("%(asctime)s | %(levelname)-8s | %(name)s | %(message)s", "%H:%M:%S")
        self._log_handler.setFormatter(formatter)
        logging.getLogger().addHandler(self._log_handler)

    def _remove_log_handler(self) -> None:
        if self._log_handler:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

    # -------------------------------------------------------------------------
    # Auto-refresh scheduling
    # -------------------------------------------------------------------------
    def _schedule_auto_refresh(self) -> None:
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
        self._refresh_job = self.after(self.AUTO_REFRESH_INTERVAL_MS, self._auto_refresh)

    def _auto_refresh(self) -> None:
        if self._auto_refresh_active:
            self._schedule_auto_refresh()
            return
        self._auto_refresh_active = True
        try:
            self.load_live_data()
        finally:
            self._auto_refresh_active = False
            self._schedule_auto_refresh()

    def _schedule_log_flush(self) -> None:
        if self._log_flush_job:
            try:
                self.after_cancel(self._log_flush_job)
            except Exception:
                pass
        self._log_flush_job = self.after(self.LOG_FLUSH_INTERVAL_MS, self._flush_logs)

    def _flush_logs(self) -> None:
        if hasattr(self, 'log_text'):
            while True:
                try:
                    msg = self.log_queue.get_nowait()
                except queue.Empty:
                    break
                self.log_text.configure(state='normal')
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state='disabled')
        self._schedule_log_flush()

    # -------------------------------------------------------------------------
    # UI Construction
    # -------------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._notebook = ttk.Notebook(self)
        self._notebook.pack(fill="both", expand=True)

        # Tab 1: Trades
        self._trades_tab = tk.Frame(self._notebook, bg=T.BG_APP)
        self._notebook.add(self._trades_tab, text="📈 Trades")

        # Tab 2: Log
        self._log_tab = tk.Frame(self._notebook, bg=T.BG_APP)
        self._notebook.add(self._log_tab, text="📋 Log")

        self._build_trades_tab()
        self._build_log_tab()

    def _build_trades_tab(self) -> None:
        main = tk.Frame(self._trades_tab, bg=T.BG_APP, padx=T.PAD_MD, pady=T.PAD_MD)
        main.pack(fill="both", expand=True)

        self._mode_banner = tk.Label(
            main,
            text="PAPER mode — simulated trades only. No Nobitex orders.",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.SUCCESS_BG,
            fg=T.SUCCESS_DARK,
            anchor="w",
            padx=T.PAD_MD,
            pady=T.PAD_SM,
        )
        self._mode_banner.pack(fill="x", pady=(0, T.PAD_MD))
        self.refresh_execution_mode()

        self._build_stats_cards(main)
        self._build_action_bar(main)
        self._build_tree(main)

    def refresh_execution_mode(self) -> None:
        if not hasattr(self, "_mode_banner"):
            return
        try:
            if not self._mode_banner.winfo_exists():
                return
        except Exception:
            return
        mode = normalize_execution_mode(getattr(self.main_app, "execution_mode", PAPER))
        if mode == LIVE:
            self._mode_banner.config(
                text="Paper entries STOPPED — app is in Live mode. Numbers below are simulated leftover/history only.",
                bg=T.WARNING_BG if hasattr(T, "WARNING_BG") else T.BG_PANEL,
                fg=T.DANGER,
            )
        else:
            self._mode_banner.config(
                text="PAPER mode — simulated trades only. No Nobitex orders. Switch to Live after you are satisfied, then press Start.",
                bg=T.SUCCESS_BG if hasattr(T, "SUCCESS_BG") else T.BG_PANEL,
                fg=T.SUCCESS_DARK,
            )

    def _build_log_tab(self) -> None:
        log_frame = tk.Frame(self._log_tab, bg="#0A1628", highlightthickness=1, highlightbackground=T.PRIMARY)
        log_frame.pack(fill="both", expand=True, padx=T.PAD_MD, pady=T.PAD_MD)

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            font=("Consolas", 10),
            bg="#0A1628",
            fg="#E0E0E0",
            insertbackground="#E0E0E0",
            borderwidth=0,
            height=20,
            state="disabled"
        )
        self.log_text.pack(fill="both", expand=True, padx=2, pady=2)

        # Configure tags for log levels
        self.log_text.tag_config("INFO", foreground="#4FC3F7")
        self.log_text.tag_config("WARNING", foreground="#FFB74D")
        self.log_text.tag_config("ERROR", foreground="#E57373")
        self.log_text.tag_config("DEBUG", foreground="#90A4AE")
        self.log_text.tag_config("CRITICAL", foreground="#EF5350", font=("Consolas", 10, "bold"))

    # -------------------------------------------------------------------------
    # Stats cards
    # -------------------------------------------------------------------------
    def _build_stats_cards(self, parent: tk.Widget) -> None:
        card = tk.Frame(parent, bg=T.BG_PANEL,
                        highlightthickness=1, highlightbackground=T.BORDER)
        card.pack(fill="x", pady=(0, T.PAD_MD))
        inner = tk.Frame(card, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=T.PAD_2XL, pady=T.PAD_MD)

        def _stat(label: str, attr: str, color: str) -> None:
            f = tk.Frame(inner, bg=T.BG_PANEL)
            f.pack(side="left", padx=T.PAD_XL)
            tk.Label(f, text=label,
                     font=T.font(size=T.FONT_XS),
                     bg=T.BG_PANEL, fg=T.TEXT_MUTED).pack(anchor="w")
            lbl = tk.Label(f, text="—",
                           font=T.font(size=T.FONT_LG, weight="bold"),
                           bg=T.BG_PANEL, fg=color)
            lbl.pack(anchor="w")
            setattr(self, attr, lbl)

        stats_defs = [
            ("💰 Equity",      "_lbl_equity",      T.SUCCESS_DARK),
            ("📈 Net P&L",     "_lbl_net_pnl",     T.PRIMARY),
            ("📊 Return %",    "_lbl_return_pct",  T.INFO),
            ("🏆 Win Rate",   "_lbl_win_rate",    T.SUCCESS_DARK),
            ("📊 Avg PnL %",  "_lbl_avg_pnl",     T.PRIMARY),
            ("✅ Closed",     "_lbl_total_closed", T.TEXT_PRIMARY),
            ("🔓 Open",       "_lbl_open",        T.INFO),
        ]
        for label, attr, color in stats_defs:
            _stat(label, attr, color)

    # -------------------------------------------------------------------------
    # Action bar (refresh, export, clear)
    # -------------------------------------------------------------------------
    def _build_action_bar(self, parent: tk.Widget) -> None:
        bar = tk.Frame(parent, bg=T.BG_APP)
        bar.pack(fill="x", pady=(0, T.PAD_SM))

        tk.Button(bar, text="🔄 Refresh Data",
                  font=T.font(size=T.FONT_SM, weight="bold"),
                  bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
                  relief="flat", cursor="hand2",
                  padx=T.PAD_LG, pady=T.PAD_SM,
                  command=self.load_live_data).pack(side="left", padx=(0, T.PAD_SM))
        tk.Button(bar, text="📤 Export CSV",
                  font=T.font(size=T.FONT_SM),
                  bg=T.SUCCESS, fg=T.TEXT_ON_PRIMARY,
                  relief="flat", cursor="hand2",
                  padx=T.PAD_LG, pady=T.PAD_SM,
                  command=self._export_live_csv).pack(side="left")
        tk.Button(bar, text="🗑️ Clear DB",
                  font=T.font(size=T.FONT_SM),
                  bg=T.DANGER, fg=T.TEXT_ON_PRIMARY,
                  relief="flat", cursor="hand2",
                  padx=T.PAD_LG, pady=T.PAD_SM,
                  command=self._clear_live_db).pack(side="left", padx=(T.PAD_SM, 0))

        tk.Label(bar, text="Filter:",
                 bg=T.BG_APP, fg=T.TEXT_SECONDARY).pack(side="left", padx=(T.PAD_LG, T.PAD_SM))
        self._filter_var = tk.StringVar(value="All")
        filter_cb = ttk.Combobox(bar, textvariable=self._filter_var,
                                 values=["All", "open", "closed"],
                                 state="readonly", width=9)
        filter_cb.pack(side="left")
        filter_cb.bind("<<ComboboxSelected>>", lambda _: self._refresh_live_tree())

    # -------------------------------------------------------------------------
    # Trade Tree
    # -------------------------------------------------------------------------
    def _build_tree(self, parent: tk.Widget) -> None:
        tree_frame, self.live_tree = make_tree(
            parent,
            columns=LIVE_COLS,
            widths=[LIVE_WIDTHS.get(col, 100) for col in LIVE_COLS],
            height=10,
            return_frame=True
        )
        tree_frame.pack(fill="both", expand=True)

        for col in LIVE_COLS:
            self.live_tree.heading(col, text=col,
                                   command=lambda c=col: self._sort_live_by(c))
            anchor = "w" if col in {"Symbol", "Signal", "Entry Time"} else "center"
            self.live_tree.column(col, width=LIVE_WIDTHS.get(col, 100), anchor=anchor)

        self.live_tree.tag_configure("profit", foreground=T.SUCCESS_DARK, background=T.SUCCESS_BG)
        self.live_tree.tag_configure("loss",   foreground=T.DANGER_DARK,  background=T.DANGER_BG)
        self.live_tree.tag_configure("open",   foreground=T.INFO,          background=T.INFO_BG)

    # -------------------------------------------------------------------------
    # Data loading and display
    # -------------------------------------------------------------------------
    def load_live_data(self) -> None:
        """Load live trades from tracker and update stats and table."""
        if not self.tracker:
            return

        try:
            data_df = getattr(self.main_app, "data_df", None)

            raw_rows = self.tracker.get_enriched_trades(
                current_data=data_df,
                custom_tp=None,
                custom_sl=None
            ) or []

            self._live_rows = []
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                row["_pnl"] = safe_pnl(row.get("_pnl"))
                row["_entry"] = safe_pnl(row.get("_entry"))
                row["_dp"] = safe_pnl(row.get("_dp"))
                row["_pnl_amount"] = safe_pnl(row.get("_pnl_amount"))
                self._live_rows.append(row)

            stats = self.tracker.get_summary_stats() or {}
            win_rate = safe_stat(stats, "win_rate")
            avg_pnl = safe_stat(stats, "avg_pnl")
            total_closed = int(safe_stat(stats, "total_trades"))

            open_count = sum(
                1 for r in self._live_rows
                if isinstance(r.get("status"), str) and r["status"].lower() == "open"
            )

            initial_cap = self.tracker.initial_balance if hasattr(self.tracker, 'initial_balance') else 1000.0

            total_pnl_amount = sum(
                r.get("_pnl_amount", 0.0) or 0.0 for r in self._live_rows
            )
            current_equity = initial_cap + total_pnl_amount
            return_pct = (total_pnl_amount / initial_cap * 100.0) if initial_cap > 0 else 0.0

            net_color = T.SUCCESS_DARK if total_pnl_amount >= 0 else T.DANGER_DARK
            self._lbl_equity.config(text=f"${current_equity:,.2f}", fg=net_color)
            self._lbl_net_pnl.config(text=f"${total_pnl_amount:+,.2f}", fg=net_color)
            self._lbl_return_pct.config(text=f"{return_pct:+.2f}%", fg=net_color)
            self._lbl_win_rate.config(text=f"{win_rate:.2f}%")
            self._lbl_avg_pnl.config(text=f"{avg_pnl:.2f}%")
            self._lbl_total_closed.config(text=str(total_closed))
            self._lbl_open.config(text=str(open_count))

            self._refresh_live_tree()

        except Exception as exc:
            logger.error("Error loading live data in UI: %s", exc, exc_info=True)

    def _refresh_live_tree(self) -> None:
        self.live_tree.delete(*self.live_tree.get_children())
        fv = self._filter_var.get()
        rows = [
            r for r in self._live_rows
            if fv == "All" or r.get("status", "").lower() == fv.lower()
        ]

        for r in rows:
            pnl = r.get("_pnl")
            pnl_amt = r.get("_pnl_amount")
            status = r.get("status", "").lower()

            if status == "open":
                tag = "open"
            elif pnl is not None:
                tag = "profit" if pnl >= 0 else "loss"
            else:
                tag = ""

            vals = (
                r.get("symbol", ""),
                status.upper(),
                r.get("entry_signal", ""),
                fmt_price(r.get("_entry")),
                fmt_price(r.get("_dp")),
                fmt_pnl(pnl),
                fmt_dollar(pnl_amt),
                r.get("exit_reason") or "—",
                r.get("entry_time") or "",
            )
            self.live_tree.insert("", "end", values=vals, tags=(tag,) if tag else ())

    # -------------------------------------------------------------------------
    # Sorting
    # -------------------------------------------------------------------------
    def _sort_live_by(self, col: str) -> None:
        asc = not self._sort_state.get(col, False)
        self._sort_state[col] = asc

        key_map = {
            "Symbol": lambda r: str(r.get("symbol") or "").lower(),
            "Status": lambda r: str(r.get("status") or "").lower(),
            "Signal": lambda r: str(r.get("entry_signal") or "").lower(),
            "Entry": lambda r: safe_pnl(r.get("_entry")) or 0.0,
            "Exit / Cur": lambda r: safe_pnl(r.get("_dp")) or 0.0,
            "PnL %": lambda r: safe_pnl(r.get("_pnl")) or 0.0,
            "PnL $": lambda r: safe_pnl(r.get("_pnl_amount")) or 0.0,
            "Reason": lambda r: str(r.get("exit_reason") or "").lower(),
            "Entry Time": lambda r: str(r.get("entry_time") or "").lower(),
        }

        key_func = key_map.get(col, lambda r: str(r.get(col, "")))
        self._live_rows.sort(key=key_func, reverse=not asc)
        self._refresh_live_tree()

    # -------------------------------------------------------------------------
    # Export and clear
    # -------------------------------------------------------------------------
    def _export_live_csv(self) -> None:
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            initialfile=f"live_trades_{datetime.now():%Y%m%d}.csv",
            parent=self
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.writer(fh)
                w.writerow(LIVE_COLS)
                for child in self.live_tree.get_children():
                    w.writerow(self.live_tree.item(child)["values"])
            messagebox.showinfo("Export", "Saved successfully.", parent=self)
        except Exception as exc:
            logger.error("CSV export failed: %s", exc)
            messagebox.showerror("Export Error", str(exc), parent=self)

    def _clear_live_db(self) -> None:
        if not self.tracker:
            return
        if not callable(getattr(self.tracker, "clear_all_trades", None)):
            messagebox.showerror(
                "Feature Missing",
                "The 'clear_all_trades' method is missing from SignalTracker.",
                parent=self
            )
            return
        if not messagebox.askyesno(
            "Clear Database",
            "Delete ALL paper trades history?\n\nThis cannot be undone.",
            parent=self
        ):
            return
        if self.tracker.clear_all_trades():
            self.load_live_data()
            messagebox.showinfo("Success", "Paper trading database cleared.", parent=self)
        else:
            messagebox.showerror("Error", "Failed to clear the database.", parent=self)

    # -------------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------------
    def on_close(self) -> None:
        if self._refresh_job:
            try:
                self.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None
        if self._log_flush_job:
            try:
                self.after_cancel(self._log_flush_job)
            except Exception:
                pass
            self._log_flush_job = None
        self._remove_log_handler()