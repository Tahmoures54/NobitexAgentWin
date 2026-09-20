# source/gui/ui_factory.py
"""
UIFactory: سازنده تمام اجزای رابط گرافیکی (v7.1 — Pure Price Action)
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import TYPE_CHECKING, Callable

from gui.ui_theme import Styles, Theme

if TYPE_CHECKING:
    from gui.gui_main import CryptoScannerApp

T = Theme


def _card(parent: tk.Widget, **kwargs) -> tuple:
    outer = tk.Frame(parent, bg=T.BG_APP, **kwargs)
    card = tk.Frame(parent, bg=T.BG_PANEL, highlightthickness=1,
                    highlightbackground=T.BORDER, relief="flat")
    card.pack(fill="x", padx=2, pady=2)
    inner = tk.Frame(card, bg=T.BG_PANEL)
    inner.pack(fill="x", padx=T.PAD_2XL, pady=T.PAD_LG)
    return outer, inner


def _sep_v(parent: tk.Widget) -> None:
    tk.Frame(parent, bg=T.BORDER, width=1).pack(side="left", fill="y", padx=T.PAD_MD)


def _field_label(parent: tk.Widget, text: str) -> tk.Label:
    lbl = tk.Label(parent, text=text,
                   font=T.font(size=T.FONT_SM, weight="bold"),
                   bg=T.BG_PANEL, fg=T.TEXT_SECONDARY)
    lbl.pack(side="left", padx=(0, T.PAD_SM))
    return lbl


def _add_hover(widget: tk.Widget, color_enter: str, color_leave: str,
               prop: str = "bg") -> None:
    widget.bind("<Enter>", lambda _: widget.config(**{prop: color_enter}))
    widget.bind("<Leave>", lambda _: widget.config(**{prop: color_leave}))


def _primary_button(parent: tk.Widget, text: str, command: Callable,
                    padx: int = T.PAD_XL, pady: int = T.PAD_MD,
                    font_size: int = T.FONT_BASE, bold: bool = True) -> tk.Button:
    weight = "bold" if bold else "normal"
    btn = tk.Button(parent, text=text,
                    font=T.font(size=font_size, weight=weight),
                    bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
                    activebackground=T.PRIMARY_DARK,
                    activeforeground=T.TEXT_ON_PRIMARY,
                    relief="flat", cursor="hand2",
                    padx=padx, pady=pady, bd=0, command=command)
    _add_hover(btn, T.PRIMARY_DARK, T.PRIMARY)
    return btn


def _colored_button(parent: tk.Widget, text: str, command: Callable,
                    bg: str = T.PRIMARY, fg: str = T.TEXT_ON_PRIMARY,
                    padx: int = T.PAD_LG, pady: int = T.PAD_SM,
                    bold: bool = False,
                    hover_bg: str | None = None) -> tk.Button:
    btn = tk.Button(parent, text=text,
                    font=T.font(size=T.FONT_SM, weight="bold" if bold else "normal"),
                    bg=bg, fg=fg,
                    activebackground=hover_bg or bg,
                    activeforeground=fg,
                    relief="flat", cursor="hand2",
                    padx=padx, pady=pady, bd=0, command=command)
    if hover_bg:
        _add_hover(btn, hover_bg, bg)
    return btn


class UIFactory:
    def __init__(self, app: "CryptoScannerApp"):
        self.app = app
        self.root = app.root

    def build_all(self) -> None:
        self._apply_global_styles()
        self._build_menubar()
        self._build_header()
        self._build_market_stats()
        self._build_control_panel()
        self._build_market_intelligence()
        self._build_treeview()
        self._build_footer()
        self._configure_treeview_tags()

    def _apply_global_styles(self) -> None:
        self.root.configure(bg=T.BG_APP)
        style = ttk.Style(self.root)
        Styles.apply(style)

    def _build_menubar(self) -> None:
        app = self.app
        menubar = tk.Menu(
            self.root, bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
            activebackground=T.PRIMARY_DARK, activeforeground=T.TEXT_ON_PRIMARY,
            font=T.font(size=T.FONT_SM), borderwidth=0, relief="flat",
        )
        self.root.config(menu=menubar)

        def _submenu(label: str) -> tk.Menu:
            m = tk.Menu(
                menubar, tearoff=0, bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
                activebackground=T.PRIMARY_GHOST, activeforeground=T.PRIMARY_DARK,
                font=T.font(size=T.FONT_SM), borderwidth=1, relief="solid",
            )
            menubar.add_cascade(label=label, menu=m)
            return m

        file_menu = _submenu("📁 File")
        file_menu.add_command(label="💾 Save to Excel", command=app.save_to_excel)
        file_menu.add_command(label="⚙️ Settings", command=app.open_settings)
        file_menu.add_separator()
        file_menu.add_command(label="🗑️ Clear Trading Database", command=app.clear_databases)
        file_menu.add_separator()
        file_menu.add_command(label="❌ Exit", command=app._on_closing)

        tools_menu = _submenu("🛠️ Tools")
        for label, cmd in [
            ("📝 Take Note", app.open_note_window),
            ("📈 Signal Performance", app.open_signal_performance),
            ("🔬 Backtest", app.open_signal_backtester),
            ("🔔 Set Alerts", app.set_alerts),
            ("🗑️ Clear Trading Database", app.clear_databases),
        ]:
            tools_menu.add_command(label=label, command=cmd)

        view_menu = _submenu("👁️ View")
        view_menu.add_checkbutton(label="🎯 Simple Mode",
                                  variable=app.simple_mode_var,
                                  command=app.toggle_simple_mode)
        view_menu.add_checkbutton(label="🔄 Auto Refresh",
                                  variable=app.auto_refresh_var)

        help_menu = _submenu("❓ Help")
        help_menu.add_command(label="📖 User Guide", command=app.open_user_guide)
        help_menu.add_command(label="⚠️ Disclaimer",
                              command=lambda: app.handle_disclaimer(show_always=True))
        help_menu.add_separator()
        help_menu.add_command(label="ℹ️ About", command=app.show_about)

    def _build_header(self) -> None:
        app = self.app
        hdr = tk.Frame(self.root, bg=T.PRIMARY, height=T.HEADER_HEIGHT)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        left = tk.Frame(hdr, bg=T.PRIMARY)
        left.pack(side="left", fill="y", padx=T.PAD_2XL)
        tk.Label(left, text="🚀 Advanced Crypto Scanner",
                 font=T.font(size=T.FONT_XL, weight="bold"),
                 bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY).pack(anchor="w", pady=(T.PAD_LG, 0))
        tk.Label(left, text=f"v{app.APP_VERSION}  –  Pump & Trailing Stop",
                 font=T.font(size=T.FONT_XS),
                 bg=T.PRIMARY, fg=T.PRIMARY_LIGHTER).pack(anchor="w")

        right = tk.Frame(hdr, bg=T.PRIMARY)
        right.pack(side="right", fill="y", padx=T.PAD_2XL)
        for text, bg, fg, cmd in [
            ("⭐ PREMIUM", T.WARNING, T.TEXT_PRIMARY, app.open_premium_window),
            ("🤖 BOT", T.INFO, T.TEXT_ON_PRIMARY, app.open_bot_panel),
            ("📊 PERF", T.SUCCESS, T.TEXT_ON_PRIMARY, app.open_signal_performance),
            ("⚙️ SETTINGS", T.PRIMARY_DARK, T.TEXT_ON_PRIMARY, app.open_settings),
        ]:
            btn = tk.Button(right, text=text,
                            font=T.font(size=T.FONT_SM, weight="bold"),
                            bg=bg, fg=fg,
                            activebackground=T.PRIMARY_DARKER,
                            activeforeground=T.TEXT_ON_PRIMARY,
                            relief="flat", cursor="hand2",
                            padx=T.PAD_XL, pady=T.PAD_SM, bd=0, command=cmd)
            btn.pack(side="right", padx=T.PAD_SM, pady=T.PAD_LG)

    def _build_market_stats(self) -> None:
        app = self.app
        outer, inner = _card(self.root)
        outer.pack(fill="x", padx=T.PAD_LG, pady=(T.PAD_LG, T.PAD_SM))

        def _metric(title, init_value, attr, color, value_size=T.FONT_XL):
            frame = tk.Frame(inner, bg=T.BG_PANEL)
            frame.pack(side="left", padx=T.PAD_LG)
            tk.Label(frame, text=title, font=T.font(size=T.FONT_XS),
                     bg=T.BG_PANEL, fg=T.TEXT_MUTED).pack(anchor="w")
            lbl = tk.Label(frame, text=init_value,
                           font=T.font(size=value_size, weight="bold"),
                           bg=T.BG_PANEL, fg=color)
            lbl.pack(anchor="w")
            setattr(app, attr, lbl)

        for title, init, attr, color in [
            ("₿ BTC Price", "$--,---", "price_label", T.PRIMARY),
            ("📊 Dominance", "--%", "dom_label", T.INFO),
            ("📈 Market Trend", "--", "trend_label", T.SUCCESS),
            ("👤 Plan", "Loading…", "plan_label", T.WARNING),
        ]:
            _metric(title, init, attr, color)
            _sep_v(inner)

        for attr, title, color in [
            ("gainers_label", "🔥 Top Gainer", T.SUCCESS),
            ("losers_label", "❄️ Top Loser", T.DANGER),
        ]:
            frame = tk.Frame(inner, bg=T.BG_PANEL)
            frame.pack(side="left", padx=T.PAD_LG)
            tk.Label(frame, text=title, font=T.font(size=T.FONT_XS),
                     bg=T.BG_PANEL, fg=T.TEXT_MUTED).pack(anchor="w")
            lbl = tk.Label(frame, text="--",
                           font=T.font(size=T.FONT_MD, weight="bold"),
                           bg=T.BG_PANEL, fg=color)
            lbl.pack(anchor="w")
            setattr(app, attr, lbl)
            _sep_v(inner)

    def _build_control_panel(self) -> None:
        app = self.app
        outer, inner = _card(self.root)
        outer.pack(fill="x", padx=T.PAD_LG, pady=(T.PAD_SM, T.PAD_MD))

        row1 = tk.Frame(inner, bg=T.BG_PANEL)
        row1.pack(fill="x", pady=(0, T.PAD_MD))

        _field_label(row1, "Fetch:")
        app.cnt_entry = ttk.Entry(row1, width=7)
        app.cnt_entry.insert(0, "500")
        app.cnt_entry.pack(side="left", padx=(0, T.PAD_XL))

        _field_label(row1, "Venue:")
        ttk.Label(row1, text="Nobitex • IRT").pack(side="left", padx=(0, T.PAD_XL))

        _field_label(row1, "Category:")
        app.cb_cat = ttk.Combobox(row1, values=list(app.CATEGORIES.keys()),
                                  state="readonly", width=12)
        app.cb_cat.current(0)
        app.cb_cat.pack(side="left", padx=(0, T.PAD_XL))
        app.cb_cat.bind("<<ComboboxSelected>>", app.apply_filter)

        _field_label(row1, "Signal:")
        app.cb_sig = ttk.Combobox(row1, values=["All", "5% Pump", "Neutral"],
                                  state="readonly", width=11)
        app.cb_sig.current(0)
        app.cb_sig.pack(side="left", padx=(0, T.PAD_XL))
        app.cb_sig.bind("<<ComboboxSelected>>", app.apply_filter)

        _field_label(row1, "Risk:")
        app.cb_risk = ttk.Combobox(row1, values=["All", "Medium", "High"],
                                   state="readonly", width=9)
        app.cb_risk.current(0)
        app.cb_risk.pack(side="left", padx=(0, T.PAD_XL))
        app.cb_risk.bind("<<ComboboxSelected>>", app.apply_filter)

        _field_label(row1, "Search:")
        app.search_var = tk.StringVar()
        app.search_var.trace_add("write", lambda *_: app.apply_filter())
        ttk.Entry(row1, textvariable=app.search_var, width=18).pack(
            side="left", padx=(0, T.PAD_XL)
        )

        row2 = tk.Frame(inner, bg=T.BG_PANEL)
        row2.pack(fill="x")

        app.refresh_button = _primary_button(
            row2, text="🔄 REFRESH DATA", command=app.refresh,
            padx=T.PAD_2XL, pady=T.PAD_MD, font_size=T.FONT_MD,
        )
        app.refresh_button.pack(side="left", padx=(0, T.PAD_MD))

        # ── Action buttons ─────────────────────────────────────
        # NOTE: "Clear DB" opens a confirmation dialog before deleting
        #       paper + real trading databases.
        for text, bg, fg, cmd, hover_bg, tip in [
            ("🧹 Clear Filters", T.BG_APP, T.TEXT_SECONDARY,
             app.clear_filters, None, "Reset category/signal/risk/search filters"),
            ("🗑️ Clear DB", T.DANGER, T.TEXT_ON_PRIMARY,
             app.clear_databases, T.DANGER_DARK,
             "Delete ALL trading history (paper + real)"),
            ("📝 Note", T.INFO, T.TEXT_ON_PRIMARY,
             app.open_note_window, None, "Open trading journal"),
            ("📊 Backtest", T.WARNING, T.TEXT_PRIMARY,
             app.open_signal_backtester, None, "Open backtest window"),
            ("📈 Perf", T.PRIMARY, T.TEXT_ON_PRIMARY,
             app.open_signal_performance, None, "Open performance tracker"),
        ]:
            btn = _colored_button(
                row2, text=text, command=cmd, bg=bg, fg=fg,
                padx=T.PAD_LG, pady=T.PAD_SM,
                bold=(text == "🗑️ Clear DB"),
                hover_bg=hover_bg,
            )
            btn.pack(side="left", padx=(0, T.PAD_SM))
            try:
                from gui.gui_helpers import ToolTip
                ToolTip(btn, tip)
            except Exception:
                pass

        tk.Frame(row2, bg=T.BG_PANEL).pack(side="left", fill="x", expand=True)
        timer_frame = tk.Frame(row2, bg=T.BG_PANEL)
        timer_frame.pack(side="right")
        tk.Checkbutton(timer_frame, text="Auto",
                       variable=app.auto_refresh_var,
                       font=T.font(size=T.FONT_SM),
                       bg=T.BG_PANEL, fg=T.TEXT_SECONDARY,
                       selectcolor=T.PRIMARY_GHOST,
                       activebackground=T.BG_PANEL,
                       cursor="hand2").pack(side="left", padx=(0, T.PAD_SM))
        tk.Label(timer_frame, text="Next:", font=T.font(size=T.FONT_XS),
                 bg=T.BG_PANEL, fg=T.TEXT_MUTED).pack(side="left", padx=(0, T.PAD_SM))
        app.timer_label = tk.Label(timer_frame, text="5:00",
                                   font=T.font(size=T.FONT_XL, weight="bold"),
                                   bg=T.BG_PANEL, fg=T.DANGER)
        app.timer_label.pack(side="left")

    def _build_market_intelligence(self) -> None:
        """Main-page read-only Nobitex market intelligence panel."""
        app = self.app
        outer = tk.Frame(self.root, bg=T.BG_APP)
        outer.pack(fill="x", padx=T.PAD_LG, pady=(0, T.PAD_MD))
        card = tk.Frame(
            outer, bg=T.BG_PANEL, highlightthickness=1,
            highlightbackground=T.BORDER, relief="flat"
        )
        card.pack(fill="x", padx=2, pady=2)

        header = tk.Frame(card, bg=T.BG_PANEL)
        header.pack(fill="x", padx=T.PAD_2XL, pady=(T.PAD_MD, T.PAD_SM))
        tk.Label(
            header, text="📡 Nobitex Market Intelligence",
            font=T.font(size=T.FONT_BASE, weight="bold"),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
        ).pack(side="left")
        tk.Label(
            header,
            text="Read-only deep data for the selected / active ≥3% pump candidate",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
        ).pack(side="left", padx=T.PAD_MD)

        body = tk.Frame(card, bg=T.BG_PANEL)
        body.pack(fill="x", padx=T.PAD_2XL, pady=(0, T.PAD_MD))
        app.market_intelligence_vars = {}

        groups = [
            ("Market", [
                ("symbol", "Symbol"), ("price", "Price"), ("bid", "Bid"), ("ask", "Ask"),
                ("volume", "24h Vol"), ("change24", "24h %"),
                ("day_high", "Day High"), ("day_low", "Day Low"), ("day_pos", "Day Pos"),
            ]),
            ("Flow", [
                ("buy_pressure", "Buy Pressure"), ("sell_pressure", "Sell Pressure"),
                ("ratio", "Buy/Sell"), ("imbalance", "OB Imbalance"),
                ("last_trade", "Last Trade"), ("trades", "Trades"),
            ]),
            ("Technical", [
                ("momentum1", "1m"), ("momentum5", "5m"), ("momentum15", "15m"),
                ("rsi", "RSI"), ("ema9", "EMA 9"), ("ema21", "EMA 21"),
                ("ema_trend", "EMA Trend"), ("macd", "MACD"), ("macd_hist", "MACD Hist"),
                ("volume_ratio", "Vol Ratio"),
            ]),
        ]

        for group_name, items in groups:
            group = tk.Frame(body, bg=T.BG_PANEL)
            group.pack(side="left", fill="x", expand=True, padx=(0, T.PAD_LG))
            tk.Label(
                group, text=group_name,
                font=T.font(size=T.FONT_XS, weight="bold"),
                bg=T.BG_PANEL, fg=T.PRIMARY,
            ).pack(anchor="w", pady=(0, T.PAD_XS))
            grid = tk.Frame(group, bg=T.BG_PANEL)
            grid.pack(fill="x")
            for index, (key, label) in enumerate(items):
                cell = tk.Frame(grid, bg=T.BG_PANEL)
                cell.grid(row=index // 5, column=index % 5, sticky="ew",
                          padx=(0, T.PAD_SM), pady=1)
                grid.grid_columnconfigure(index % 5, weight=1)
                tk.Label(
                    cell, text=label,
                    font=T.font(size=T.FONT_XS),
                    bg=T.BG_PANEL, fg=T.TEXT_MUTED,
                ).pack(anchor="w")
                var = tk.StringVar(value="--")
                app.market_intelligence_vars[key] = var
                tk.Label(
                    cell, textvariable=var,
                    font=T.font(size=T.FONT_SM, weight="bold"),
                    bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
                ).pack(anchor="w")

        tk.Label(
            card, text="Deep snapshot uses Nobitex trades + order book + 1m/5m/15m candles; "
                       "technical indicators are calculated locally. Live trading is not changed.",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED, anchor="w",
        ).pack(fill="x", padx=T.PAD_2XL, pady=(0, T.PAD_SM))

    def _build_treeview(self) -> None:
        outer = tk.Frame(self.root, bg=T.BG_APP)
        outer.pack(fill="both", expand=True, padx=T.PAD_LG, pady=(0, T.PAD_MD))
        card = tk.Frame(outer, bg=T.BG_PANEL, highlightthickness=1,
                        highlightbackground=T.BORDER, relief="flat")
        card.pack(fill="both", expand=True, padx=2, pady=2)
        frame = tk.Frame(card, bg=T.BG_PANEL)
        frame.pack(fill="both", expand=True, padx=T.PAD_MD, pady=T.PAD_MD)
        self._setup_tree_widget(frame)

    def _setup_tree_widget(self, frame: tk.Frame) -> None:
        app = self.app
        app.tree = ttk.Treeview(frame, columns=app._COLS_FULL,
                                show="headings", style="Treeview")
        vsb = ttk.Scrollbar(frame, orient="vertical", command=app.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=app.tree.xview)
        app.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        app.tree.pack(fill="both", expand=True)

        text_align_cols = {"Name", "Symbol"}
        for col in app._COLS_FULL:
            anchor = "w" if col in text_align_cols else "center"
            app.tree.heading(col, text=col,
                             command=lambda c=col: app._sort_column(c, False))
            app.tree.column(col, width=app._COL_WIDTHS.get(col, 80),
                            anchor=anchor, stretch=tk.NO)

        app.tree.bind("<ButtonRelease-1>", app._on_click)
        app.tree.bind("<Motion>", app._on_motion)

    def _build_footer(self) -> None:
        app = self.app
        footer = tk.Frame(self.root, bg=T.PRIMARY, height=36)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        app.status_label = tk.Label(footer, text="Ready to scan 🚀",
                                    font=T.font(size=T.FONT_SM),
                                    bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
                                    anchor="w")
        app.status_label.pack(side="left", padx=T.PAD_2XL, fill="x", expand=True)
        tk.Label(footer, text=f"v{app.APP_VERSION}",
                 font=T.font(size=T.FONT_XS),
                 bg=T.PRIMARY, fg=T.PRIMARY_LIGHTER).pack(side="right", padx=T.PAD_2XL)

    def _configure_treeview_tags(self) -> None:
        tree = self.app.tree
        for tag, bg, fg in [
            ("5% Pump", T.STRONG_BUY_BG, T.STRONG_BUY),
            ("Neutral", T.NEUTRAL_SIG_BG, T.NEUTRAL_SIG),
            ("Medium", T.WARNING_BG, T.WARNING_DARK),
            ("High", T.DANGER_BG, T.DANGER_DARK),
        ]:
            tree.tag_configure(tag, background=bg, foreground=fg)
        tree.tag_configure("link", foreground=T.PRIMARY)
        tree.tag_configure("TV_link", foreground=T.PRIMARY)
        tree.tag_configure("BOT_link", foreground=T.INFO)
        tree.tag_configure("VIP", foreground=T.GOLD,
                           font=T.font(size=T.FONT_SM, weight="bold"))