# gui/signal_performance_window.py
"""
Signal Performance Tracker Window.
نمایش عملکرد سیگنال‌ها، آمار کلی، و مدیریت معاملات.
"""
from __future__ import annotations

import csv
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from gui.gui_helpers import center_window
from gui.ui_theme import Theme, Styles
from core.utils import fmt_price, fmt_percent, safe_float

T = Theme


# ══════════════════════════════════════════════════════════════
# Signal Performance Window
# ══════════════════════════════════════════════════════════════

class SignalPerformanceWindow(tk.Toplevel):
    """
    پنجره نمایش عملکرد سیگنال‌های معاملاتی.

    ویژگی‌ها:
        - نمایش معاملات باز و بسته
        - آمار کلی (Win Rate, Avg PnL, ...)
        - مرتب‌سازی با کلیک روی هدر
        - Export به CSV
        - رنگ‌بندی با Theme یکپارچه
    """

    _COLS: Tuple[str, ...] = (
        "Symbol",
        "Status",
        "Signal",
        "Entry Price",
        "Current / Exit",
        "PnL %",
        "Entry Time",
    )

    _COL_WIDTHS: Dict[str, int] = {
        "Symbol":         90,
        "Status":         75,
        "Signal":        110,
        "Entry Price":   100,
        "Current / Exit": 115,
        "PnL %":          80,
        "Entry Time":    155,
    }

    _WIN_W = 920
    _WIN_H = 620

    def __init__(self, parent: tk.Widget, main_app: Any):
        super().__init__(parent)
        self.main_app = main_app
        self.tracker  = main_app.signal_tracker

        self.title("📈 Signal Performance Tracker")
        self.resizable(True, True)
        self.minsize(700, 450)
        center_window(self, self._WIN_W, self._WIN_H)
        self.transient(parent)
        self.grab_set()

        # وضعیت مرتب‌سازی: {col_name: ascending}
        self._sort_state: Dict[str, bool] = {}

        # آخرین داده‌های لود شده (برای sort بدون بارگذاری مجدد)
        self._rows: List[Dict[str, Any]] = []

        self._apply_style()
        self._build_ui()
        self.load_data()

    # ══════════════════════════════════════════════════════════
    # Style
    # ══════════════════════════════════════════════════════════

    def _apply_style(self) -> None:
        self.configure(bg=T.BG_APP)
        style = ttk.Style(self)
        Styles.apply(style)

    # ══════════════════════════════════════════════════════════
    # UI Builder
    # ══════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        main = tk.Frame(self, bg=T.BG_APP)
        main.pack(fill="both", expand=True, padx=T.PAD_LG, pady=T.PAD_LG)

        self._build_stats_bar(main)
        self._build_toolbar(main)
        self._build_tree(main)
        self._build_footer(main)

    def _build_stats_bar(self, parent: tk.Frame) -> None:
        """نوار آمار کلی بالای پنجره."""
        frame = tk.Frame(parent, bg=T.BG_PANEL, pady=T.PAD_MD)
        frame.pack(fill="x", pady=(0, T.PAD_MD))

        # کارت آمار
        card = tk.Frame(
            frame,
            bg=T.BG_PANEL,
            highlightthickness=1,
            highlightbackground=T.BORDER,
        )
        card.pack(fill="x", padx=2, pady=2)
        inner = tk.Frame(card, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=T.PAD_2XL, pady=T.PAD_MD)

        def _stat(label: str, attr: str, color: str) -> None:
            frame = tk.Frame(inner, bg=T.BG_PANEL)
            frame.pack(side="left", padx=T.PAD_XL)

            tk.Label(
                frame,
                text=label,
                font=T.font(size=T.FONT_XS),
                bg=T.BG_PANEL,
                fg=T.TEXT_MUTED,
            ).pack(anchor="w")

            lbl = tk.Label(
                frame,
                text="—",
                font=T.font(size=T.FONT_LG, weight="bold"),
                bg=T.BG_PANEL,
                fg=color,
            )
            lbl.pack(anchor="w")
            setattr(self, attr, lbl)

        _stat("🏆 Win Rate",    "_lbl_win_rate",     T.SUCCESS_DARK)
        _stat("📊 Avg PnL",     "_lbl_avg_pnl",      T.PRIMARY)
        _stat("✅ Total Closed","_lbl_total_closed",  T.TEXT_PRIMARY)
        _stat("🔓 Open",        "_lbl_open",          T.INFO)
        _stat("💰 Best Trade",  "_lbl_best",          T.SUCCESS_DARK)
        _stat("📉 Worst Trade", "_lbl_worst",         T.DANGER_DARK)

    def _build_toolbar(self, parent: tk.Frame) -> None:
        """نوار دکمه‌های عملیاتی."""
        bar = tk.Frame(parent, bg=T.BG_APP)
        bar.pack(fill="x", pady=(0, T.PAD_SM))

        # دکمه Refresh
        tk.Button(
            bar,
            text="🔄 Refresh",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY,
            fg=T.TEXT_ON_PRIMARY,
            activebackground=T.PRIMARY_DARK,
            activeforeground=T.TEXT_ON_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_LG,
            pady=T.PAD_SM,
            bd=0,
            command=self.load_data,
        ).pack(side="left", padx=(0, T.PAD_SM))

        # دکمه Export CSV
        tk.Button(
            bar,
            text="📤 Export CSV",
            font=T.font(size=T.FONT_SM),
            bg=T.SUCCESS,
            fg=T.TEXT_ON_PRIMARY,
            activebackground=T.SUCCESS_DARK,
            activeforeground=T.TEXT_ON_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_LG,
            pady=T.PAD_SM,
            bd=0,
            command=self._export_csv,
        ).pack(side="left", padx=(0, T.PAD_SM))

        # فیلتر Status
        tk.Label(
            bar,
            text="Filter:",
            font=T.font(size=T.FONT_SM),
            bg=T.BG_APP,
            fg=T.TEXT_SECONDARY,
        ).pack(side="left", padx=(T.PAD_LG, T.PAD_SM))

        self._filter_var = tk.StringVar(value="All")
        filter_cb = ttk.Combobox(
            bar,
            textvariable=self._filter_var,
            values=["All", "open", "closed"],
            state="readonly",
            width=9,
        )
        filter_cb.pack(side="left")
        filter_cb.bind("<<ComboboxSelected>>", lambda _: self._refresh_tree())

    def _build_tree(self, parent: tk.Frame) -> None:
        """Treeview اصلی."""
        frame = tk.Frame(
            parent,
            bg=T.BG_PANEL,
            highlightthickness=1,
            highlightbackground=T.BORDER,
        )
        frame.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            frame,
            columns=self._COLS,
            show="headings",
            style="Treeview",
        )

        vsb = ttk.Scrollbar(frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True, padx=T.PAD_SM, pady=T.PAD_SM)

        # تنظیم ستون‌ها
        text_cols = {"Symbol", "Signal", "Entry Time"}
        for col in self._COLS:
            anchor = "w" if col in text_cols else "center"
            self.tree.heading(
                col,
                text=col,
                command=lambda c=col: self._sort_by(c),
            )
            self.tree.column(
                col,
                width=self._COL_WIDTHS.get(col, 100),
                anchor=anchor,
                stretch=tk.NO,
            )

        # Tag های رنگ‌بندی با Theme
        self.tree.tag_configure(
            "profit",
            foreground=T.SUCCESS_DARK,
            background=T.SUCCESS_BG,
        )
        self.tree.tag_configure(
            "loss",
            foreground=T.DANGER_DARK,
            background=T.DANGER_BG,
        )
        self.tree.tag_configure(
            "open",
            foreground=T.INFO,
            background=T.INFO_BG,
        )
        self.tree.tag_configure(
            "neutral",
            foreground=T.TEXT_SECONDARY,
            background=T.BG_PANEL,
        )
        self.tree.tag_configure(
            "alt",
            background=T.BG_ROW_ALT,
        )

        # double-click برای جزئیات
        self.tree.bind("<Double-1>", self._on_row_double_click)

    def _build_footer(self, parent: tk.Frame) -> None:
        """نوار پایین با تعداد ردیف‌های نمایش داده شده."""
        footer = tk.Frame(parent, bg=T.BG_APP)
        footer.pack(fill="x", pady=(T.PAD_SM, 0))

        self._lbl_row_count = tk.Label(
            footer,
            text="",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP,
            fg=T.TEXT_MUTED,
        )
        self._lbl_row_count.pack(side="left")

    # ══════════════════════════════════════════════════════════
    # Data Loading
    # ══════════════════════════════════════════════════════════

    def load_data(self) -> None:
        """داده‌های معاملات را بارگذاری و پردازش می‌کند."""
        try:
            all_trades: List[Dict[str, Any]] = self.tracker.get_all_trades() or []
            stats: Dict[str, Any] = self.tracker.get_summary_stats() or {}

            # داده‌های فعلی بازار
            data_df = getattr(self.main_app, "data_df", None)

            # پردازش هر معامله
            self._rows = []
            for trade in all_trades:
                self._rows.append(
                    self._process_trade(trade, data_df)
                )

            self._update_stats(stats, self._rows)
            self._refresh_tree()

        except Exception as exc:
            messagebox.showerror(
                "Load Error",
                f"Failed to load performance data:\n{exc}",
                parent=self,
            )

    def _process_trade(
        self,
        trade: Dict[str, Any],
        data_df: Any,
    ) -> Dict[str, Any]:
        """
        یک معامله را پردازش می‌کند و مقادیر نمایشی را آماده می‌کند.
        برای معاملات باز، PnL لحظه‌ای محاسبه می‌شود.
        """
        result = dict(trade)

        entry_price = safe_float(trade.get("entry_price"))
        pnl_percent = safe_float(trade.get("pnl_percent"))
        display_price = safe_float(trade.get("exit_price"))

        if trade.get("status") == "open" and data_df is not None:
            try:
                symbol = trade.get("symbol", "")
                rows = data_df[data_df["Symbol"] == symbol]
                if not rows.empty:
                    current_price = safe_float(rows.iloc[0].get("Price"))
                    if current_price is not None and entry_price is not None and entry_price != 0:
                        pnl_percent  = ((current_price - entry_price) / entry_price) * 100
                        display_price = current_price
            except Exception:
                pass

        result["_entry_price"]   = entry_price
        result["_display_price"] = display_price
        result["_pnl_percent"]   = pnl_percent

        return result

    # ══════════════════════════════════════════════════════════
    # Stats Update
    # ══════════════════════════════════════════════════════════

    def _update_stats(
        self,
        stats: Dict[str, Any],
        rows: List[Dict[str, Any]],
    ) -> None:
        """برچسب‌های آمار را به‌روز می‌کند."""
        def _safe(key: str, fallback: str = "—") -> str:
            v = safe_float(stats.get(key))
            return fallback if v is None else f"{v:.2f}%"

        self._lbl_win_rate.config(text=_safe("win_rate"))
        self._lbl_avg_pnl.config(text=_safe("avg_pnl"))

        total_closed = stats.get("total_trades", 0)
        self._lbl_total_closed.config(text=str(total_closed))

        open_count = sum(1 for r in rows if r.get("status") == "open")
        self._lbl_open.config(text=str(open_count))

        # بهترین و بدترین معامله
        pnls = [r["_pnl_percent"] for r in rows if r.get("_pnl_percent") is not None]
        if pnls:
            best  = max(pnls)
            worst = min(pnls)
            self._lbl_best.config(
                text=f"{best:+.2f}%",
                fg=T.SUCCESS_DARK if best >= 0 else T.DANGER_DARK,
            )
            self._lbl_worst.config(
                text=f"{worst:+.2f}%",
                fg=T.DANGER_DARK if worst < 0 else T.SUCCESS_DARK,
            )
        else:
            self._lbl_best.config(text="—")
            self._lbl_worst.config(text="—")

    # ══════════════════════════════════════════════════════════
    # Tree Rendering
    # ══════════════════════════════════════════════════════════

    def _refresh_tree(self) -> None:
        """جدول را با داده‌های فعلی و فیلتر پر می‌کند."""
        self.tree.delete(*self.tree.get_children())

        filter_val = self._filter_var.get()
        rows = self._rows

        # فیلتر
        if filter_val != "All":
            rows = [r for r in rows if r.get("status") == filter_val]

        for i, trade in enumerate(rows):
            tag  = self._row_tag(trade)
            alt  = "alt" if (i % 2 == 1 and tag == "neutral") else tag
            vals = self._row_values(trade)
            self.tree.insert("", "end", values=vals, tags=(alt,))

        count = len(rows)
        total = len(self._rows)
        self._lbl_row_count.config(
            text=f"Showing {count} of {total} trades"
        )

    def _row_tag(self, trade: Dict[str, Any]) -> str:
        """tag رنگ‌بندی ردیف را تعیین می‌کند."""
        if trade.get("status") == "open":
            return "open"
        pnl = trade.get("_pnl_percent")
        if pnl is None:
            return "neutral"
        return "profit" if pnl >= 0 else "loss"

    def _row_values(self, trade: Dict[str, Any]) -> tuple:
        """مقادیر نمایشی یک ردیف."""
        entry  = trade.get("_entry_price")
        dp     = trade.get("_display_price")
        pnl    = trade.get("_pnl_percent")
        sign   = "+" if (pnl or 0) >= 0 else ""

        return (
            trade.get("symbol",       "—"),
            trade.get("status",       "—").upper(),
            trade.get("entry_signal", "—"),
            f"${entry:.4f}"       if entry is not None else "—",
            f"${dp:.4f}"          if dp    is not None else "—",
            f"{sign}{pnl:.2f}%"   if pnl   is not None else "—",
            trade.get("entry_time", "—"),
        )

    # ══════════════════════════════════════════════════════════
    # Sorting
    # ══════════════════════════════════════════════════════════

    def _sort_by(self, col: str) -> None:
        """ردیف‌ها را بر اساس ستون مرتب می‌کند."""
        asc = not self._sort_state.get(col, False)
        self._sort_state[col] = asc

        col_map = {
            "Symbol":         lambda r: r.get("symbol", ""),
            "Status":         lambda r: r.get("status", ""),
            "Signal":         lambda r: r.get("entry_signal", ""),
            "Entry Price":    lambda r: r.get("_entry_price")   or 0,
            "Current / Exit": lambda r: r.get("_display_price") or 0,
            "PnL %":          lambda r: r.get("_pnl_percent")   or 0,
            "Entry Time":     lambda r: r.get("entry_time", ""),
        }

        key_fn = col_map.get(col, lambda r: "")
        self._rows.sort(key=key_fn, reverse=not asc)
        self._refresh_tree()

        # آپدیت هدر برای نشان دادن جهت
        for c in self._COLS:
            label = c + (" ▲" if c == col and asc else " ▼" if c == col else "")
            self.tree.heading(c, text=label)

    # ══════════════════════════════════════════════════════════
    # Export
    # ══════════════════════════════════════════════════════════

    def _export_csv(self) -> None:
        """داده‌های جدول را به فایل CSV صادر می‌کند."""
        if not self._rows:
            messagebox.showinfo(
                "No Data",
                "No trades to export.",
                parent=self,
            )
            return

        path = filedialog.asksaveasfilename(
            parent=self,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialfile=f"signal_performance_{datetime.now():%Y%m%d_%H%M}.csv",
        )
        if not path:
            return

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(self._COLS)
                for trade in self._rows:
                    writer.writerow(self._row_values(trade))

            messagebox.showinfo(
                "Export Complete",
                f"Saved to:\n{path}",
                parent=self,
            )
        except OSError as exc:
            messagebox.showerror(
                "Export Failed",
                f"Could not save file:\n{exc}",
                parent=self,
            )

    # ══════════════════════════════════════════════════════════
    # Row Details
    # ══════════════════════════════════════════════════════════

    def _on_row_double_click(self, event: tk.Event) -> None:
        """با double-click روی ردیف، جزئیات معامله را نشان می‌دهد."""
        item = self.tree.selection()
        if not item:
            return

        idx  = self.tree.index(item[0])
        rows = self._rows

        # اعمال فیلتر
        fv = self._filter_var.get()
        if fv != "All":
            rows = [r for r in rows if r.get("status") == fv]

        if idx >= len(rows):
            return

        trade = rows[idx]
        self._show_trade_detail(trade)

    def _show_trade_detail(self, trade: Dict[str, Any]) -> None:
        """پنجره کوچکی برای جزئیات کامل یک معامله."""
        win = tk.Toplevel(self)
        win.title(f"Trade Detail — {trade.get('symbol', '')}")
        win.configure(bg=T.BG_APP)
        win.resizable(False, False)
        center_window(win, 420, 340)
        win.transient(self)
        win.grab_set()

        frame = tk.Frame(win, bg=T.BG_PANEL,
                         highlightthickness=1, highlightbackground=T.BORDER)
        frame.pack(fill="both", expand=True, padx=T.PAD_LG, pady=T.PAD_LG)

        inner = tk.Frame(frame, bg=T.BG_PANEL)
        inner.pack(fill="both", expand=True,
                   padx=T.PAD_XL, pady=T.PAD_XL)

        pnl   = trade.get("_pnl_percent")
        color = T.SUCCESS_DARK if (pnl or 0) >= 0 else T.DANGER_DARK

        fields = [
            ("Symbol",       trade.get("symbol",       "—")),
            ("Status",       trade.get("status",       "—").upper()),
            ("Signal",       trade.get("entry_signal", "—")),
            ("Entry Price",  f"${trade['_entry_price']:.6f}"
                             if trade.get("_entry_price") is not None else "—"),
            ("Exit / Current", f"${trade['_display_price']:.6f}"
                                if trade.get("_display_price") is not None else "—"),
            ("PnL",          f"{'+' if (pnl or 0) >= 0 else ''}{pnl:.2f}%"
                             if pnl is not None else "—"),
            ("Entry Time",   trade.get("entry_time",  "—")),
            ("Exit Time",    trade.get("exit_time",   "—")),
            ("Notes",        trade.get("notes",       "—")),
        ]

        for label, value in fields:
            row = tk.Frame(inner, bg=T.BG_PANEL)
            row.pack(fill="x", pady=2)

            tk.Label(
                row,
                text=f"{label}:",
                font=T.font(size=T.FONT_SM, weight="bold"),
                bg=T.BG_PANEL,
                fg=T.TEXT_SECONDARY,
                width=14,
                anchor="e",
            ).pack(side="left", padx=(0, T.PAD_SM))

            fg = color if label == "PnL" else T.TEXT_PRIMARY
            tk.Label(
                row,
                text=value,
                font=T.font(size=T.FONT_SM),
                bg=T.BG_PANEL,
                fg=fg,
                anchor="w",
            ).pack(side="left")

        tk.Button(
            win,
            text="Close",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY,
            fg=T.TEXT_ON_PRIMARY,
            activebackground=T.PRIMARY_DARK,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_XL,
            pady=T.PAD_SM,
            bd=0,
            command=win.destroy,
        ).pack(pady=T.PAD_MD)