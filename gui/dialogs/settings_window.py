"""Nobitex-only application settings dialog — Persian UI (v4.1).

v4.1 changes:
- FIX v7.2.2-companion: added a live "capacity summary" panel showing
  how many positions actually fit at the current fixed lot within the
  exposure cap.  The numbers come from `BotConfig.capacity_summary()`
  so the GUI and the load_config() warning share one source of truth.
- FIX: `_apply_strategy()` now validates every preset field name
  against `BotConfig.__dataclass_fields__` before calling setattr.
  Unknown field names (typos in a preset) are logged and skipped
  instead of silently creating orphan attributes that asdict() drops.

v4.0 changes:
- Added "crisis" strategy preset (used when auto_regime_strategy is on
  and the regime detector reports CRISIS).
- Added a toggle for auto_regime_strategy: when enabled, the market
  regime automatically applies the mapped strategy on every scan.
- When auto is on, strategy cards become read-only preview; the mapping
  table is shown instead.
- The mapping is editable through bot_config.json (regime_strategy_map).

v3.0 changes:
- Added 7 trading strategy presets applied with one click.
"""
from __future__ import annotations
import logging
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Dict

import requests

from gui.dialogs.base_dialog import BaseDialog
from gui.dialogs.components import SectionFrame, safe_clipboard_paste, style_button
from gui.gui_helpers import ToolTip
from gui.ui_theme import Theme
from trading.bot_config import load_config, save_config, BotConfig

logger = logging.getLogger(__name__)
T = Theme
NOBITEX_STATS_URL = "https://apiv2.nobitex.ir/market/stats?dstCurrency=rls"


# ══════════════════════════════════════════════════════════════
# Trading strategy presets
# ══════════════════════════════════════════════════════════════

STRATEGY_PRESETS: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "name": "محافظه‌کارانه",
        "icon": "🛡️",
        "color": "#4CAF50",
        "description": "پوزیشن کوچک، حد ضرر نزدیک، تعداد کم، فقط سیگنال‌های قوی",
        "settings": {
            "max_open_positions": 3,
            "risk_per_trade_pct": 0.5,
            "max_drawdown_percent": 5.0,
            "pump_threshold_pct": 2.5,
            "movement_lookback_scans": 8,
            "stop_loss_pct": 2.0,
            "trailing_distance_pct": 1.5,
            "trailing_activation_pct": 1.0,
            "trailing_stop_enabled": True,
            "take_profit_percent": 5.0,
            "fixed_position_quote": 1000000.0,
            "max_position_pct": 10.0,
            "max_notional_quote": 2000000.0,
            "max_total_exposure_pct": 30.0,
            "min_volume_24h": 1000000000.0,
            "cooldown_after_loss_min": 120,
            "cooldown_after_win_min": 30,
            "entry_cooldown_seconds": 600,
            "max_new_entries_per_cycle": 1,
            "confirmation_enabled": True,
            "confirmation_pct": 0.5,
            "confirmation_max_minutes": 8,
            "invalidation_pct": 0.6,
            "max_chase_pct": 0.5,
            "min_observed_move_pct": 1.5,
            "max_nobitex_spread_pct": 0.7,
            "min_confirm_scans": 2,
            "btc_max_dump_pct": 1.0,
            "min_ask_depth_quote": 5000000.0,
            "btc_dump_exception_enabled": False,
            "use_risk_filter": True,
            "blocked_risk_levels": ["High", "Extreme"],
            "min_quality": 0.6,
        },
    },
    "balanced": {
        "name": "متعادل (پیش‌فرض)",
        "icon": "⚖️",
        "color": "#2196F3",
        "description": "تعادل بین ریسک و بازده — مناسب اکثر کاربران",
        "settings": {
            "max_open_positions": 10,
            "risk_per_trade_pct": 0.75,
            "max_drawdown_percent": 10.0,
            "pump_threshold_pct": 1.8,
            "movement_lookback_scans": 6,
            "stop_loss_pct": 3.0,
            "trailing_distance_pct": 2.0,
            "trailing_activation_pct": 3.0,
            "trailing_stop_enabled": True,
            "take_profit_percent": 50.0,
            "fixed_position_quote": 2500000.0,
            "max_position_pct": 25.0,
            "max_notional_quote": 5000000.0,
            "max_total_exposure_pct": 90.0,
            "min_volume_24h": 500000000.0,
            "cooldown_after_loss_min": 60,
            "cooldown_after_win_min": 15,
            "entry_cooldown_seconds": 180,
            "max_new_entries_per_cycle": 1,
            "confirmation_enabled": True,
            "confirmation_pct": 0.4,
            "confirmation_max_minutes": 5,
            "invalidation_pct": 0.8,
            "max_chase_pct": 0.8,
            "min_observed_move_pct": 0.8,
            "max_nobitex_spread_pct": 0.9,
            "min_confirm_scans": 1,
            "btc_max_dump_pct": 2.0,
            "min_ask_depth_quote": 1000000.0,
            "btc_dump_exception_enabled": True,
            "use_risk_filter": True,
            "blocked_risk_levels": ["High", "Extreme"],
            "min_quality": 0.4,
        },
    },
    "aggressive": {
        "name": "تهاجمی",
        "icon": "🔥",
        "color": "#FF5722",
        "description": "پوزیشن بزرگ، فرصت‌های بیشتر، پذیرش ریسک بالاتر",
        "settings": {
            "max_open_positions": 10,
            "risk_per_trade_pct": 2.0,
            "max_drawdown_percent": 20.0,
            "pump_threshold_pct": 1.2,
            "movement_lookback_scans": 4,
            "stop_loss_pct": 4.0,
            "trailing_distance_pct": 2.5,
            "trailing_activation_pct": 2.0,
            "trailing_stop_enabled": True,
            "take_profit_percent": 0.0,
            "fixed_position_quote": 4000000.0,
            "max_position_pct": 40.0,
            "max_notional_quote": 8000000.0,
            "max_total_exposure_pct": 95.0,
            "min_volume_24h": 300000000.0,
            "cooldown_after_loss_min": 30,
            "cooldown_after_win_min": 10,
            "entry_cooldown_seconds": 120,
            "max_new_entries_per_cycle": 2,
            "confirmation_enabled": False,
            "confirmation_pct": 0.3,
            "confirmation_max_minutes": 4,
            "invalidation_pct": 1.0,
            "max_chase_pct": 1.0,
            "min_observed_move_pct": 0.5,
            "max_nobitex_spread_pct": 1.5,
            "min_confirm_scans": 1,
            "btc_max_dump_pct": 3.0,
            "min_ask_depth_quote": 500000.0,
            "btc_dump_exception_enabled": True,
            "use_risk_filter": False,
            "blocked_risk_levels": ["Extreme"],
            "min_quality": 0.2,
        },
    },
    "scalping": {
        "name": "اسکالپینگ",
        "icon": "⚡",
        "color": "#FFC107",
        "description": "ورود و خروج سریع، حد ضرر بسیار نزدیک، سود کوچک",
        "settings": {
            "max_open_positions": 10,
            "risk_per_trade_pct": 1.0,
            "max_drawdown_percent": 8.0,
            "pump_threshold_pct": 0.8,
            "movement_lookback_scans": 3,
            "stop_loss_pct": 1.2,
            "trailing_distance_pct": 0.8,
            "trailing_activation_pct": 0.5,
            "trailing_stop_enabled": True,
            "take_profit_percent": 2.0,
            "fixed_position_quote": 1500000.0,
            "max_position_pct": 15.0,
            "max_notional_quote": 3000000.0,
            "max_total_exposure_pct": 80.0,
            "min_volume_24h": 800000000.0,
            "cooldown_after_loss_min": 5,
            "cooldown_after_win_min": 3,
            "entry_cooldown_seconds": 60,
            "max_new_entries_per_cycle": 3,
            "confirmation_enabled": False,
            "confirmation_pct": 0.2,
            "confirmation_max_minutes": 3,
            "invalidation_pct": 0.4,
            "max_chase_pct": 0.5,
            "min_observed_move_pct": 0.4,
            "max_nobitex_spread_pct": 0.5,
            "min_confirm_scans": 1,
            "btc_max_dump_pct": 1.0,
            "min_ask_depth_quote": 3000000.0,
            "btc_dump_exception_enabled": True,
            "use_risk_filter": False,
            "blocked_risk_levels": ["Extreme"],
            "min_quality": 0.3,
        },
    },
    "trend": {
        "name": "روندی",
        "icon": "📈",
        "color": "#9C27B0",
        "description": "نگه‌داری طولانی، حد ضرر باز، اجازه دادن به سود بزرگ",
        "settings": {
            "max_open_positions": 5,
            "risk_per_trade_pct": 1.5,
            "max_drawdown_percent": 15.0,
            "pump_threshold_pct": 2.0,
            "movement_lookback_scans": 10,
            "stop_loss_pct": 5.0,
            "trailing_distance_pct": 3.0,
            "trailing_activation_pct": 4.0,
            "trailing_stop_enabled": True,
            "take_profit_percent": 0.0,
            "fixed_position_quote": 3000000.0,
            "max_position_pct": 25.0,
            "max_notional_quote": 6000000.0,
            "max_total_exposure_pct": 80.0,
            "min_volume_24h": 500000000.0,
            "cooldown_after_loss_min": 120,
            "cooldown_after_win_min": 60,
            "entry_cooldown_seconds": 600,
            "max_new_entries_per_cycle": 1,
            "confirmation_enabled": True,
            "confirmation_pct": 0.5,
            "confirmation_max_minutes": 10,
            "invalidation_pct": 1.2,
            "max_chase_pct": 0.6,
            "min_observed_move_pct": 1.2,
            "max_nobitex_spread_pct": 1.0,
            "min_confirm_scans": 2,
            "btc_max_dump_pct": 2.5,
            "min_ask_depth_quote": 2000000.0,
            "btc_dump_exception_enabled": False,
            "use_risk_filter": True,
            "blocked_risk_levels": ["High", "Extreme"],
            "min_quality": 0.5,
        },
    },
    "eagle": {
        "name": "شکارچی عقاب",
        "icon": "🦅",
        "color": "#795548",
        "description": "تمرکز روی محرک‌های قوی حتی در بازار نزولی (Eagle Exception)",
        "settings": {
            "max_open_positions": 8,
            "risk_per_trade_pct": 1.2,
            "max_drawdown_percent": 12.0,
            "pump_threshold_pct": 2.0,
            "movement_lookback_scans": 5,
            "stop_loss_pct": 3.5,
            "trailing_distance_pct": 2.0,
            "trailing_activation_pct": 2.5,
            "trailing_stop_enabled": True,
            "take_profit_percent": 30.0,
            "fixed_position_quote": 2500000.0,
            "max_position_pct": 25.0,
            "max_notional_quote": 5000000.0,
            "max_total_exposure_pct": 90.0,
            "min_volume_24h": 400000000.0,
            "cooldown_after_loss_min": 45,
            "cooldown_after_win_min": 15,
            "entry_cooldown_seconds": 240,
            "max_new_entries_per_cycle": 2,
            "confirmation_enabled": False,
            "confirmation_pct": 0.4,
            "confirmation_max_minutes": 5,
            "invalidation_pct": 1.0,
            "max_chase_pct": 0.8,
            "min_observed_move_pct": 1.0,
            "max_nobitex_spread_pct": 0.9,
            "min_confirm_scans": 1,
            "btc_max_dump_pct": 3.0,
            "min_ask_depth_quote": 1500000.0,
            "btc_dump_exception_enabled": True,
            "eagle_min_observed_move_pct": 2.0,
            "eagle_min_1h_pct": 1.5,
            "eagle_min_volume_irt": 250000000.0,
            "eagle_max_spread_pct": 1.0,
            "use_risk_filter": False,
            "blocked_risk_levels": ["Extreme"],
            "min_quality": 0.3,
        },
    },
    "swing": {
        "name": "نوسانی",
        "icon": "🌊",
        "color": "#00BCD4",
        "description": "نگه‌داری میان‌مدت، تعادل بین روند و اسکالپ",
        "settings": {
            "max_open_positions": 6,
            "risk_per_trade_pct": 1.0,
            "max_drawdown_percent": 12.0,
            "pump_threshold_pct": 1.5,
            "movement_lookback_scans": 12,
            "stop_loss_pct": 4.0,
            "trailing_distance_pct": 2.5,
            "trailing_activation_pct": 3.0,
            "trailing_stop_enabled": True,
            "take_profit_percent": 15.0,
            "fixed_position_quote": 3000000.0,
            "max_position_pct": 20.0,
            "max_notional_quote": 6000000.0,
            "max_total_exposure_pct": 80.0,
            "min_volume_24h": 700000000.0,
            "cooldown_after_loss_min": 90,
            "cooldown_after_win_min": 30,
            "entry_cooldown_seconds": 480,
            "max_new_entries_per_cycle": 1,
            "confirmation_enabled": True,
            "confirmation_pct": 0.5,
            "confirmation_max_minutes": 6,
            "invalidation_pct": 1.0,
            "max_chase_pct": 0.7,
            "min_observed_move_pct": 0.9,
            "max_nobitex_spread_pct": 1.0,
            "min_confirm_scans": 2,
            "btc_max_dump_pct": 2.0,
            "min_ask_depth_quote": 2000000.0,
            "btc_dump_exception_enabled": True,
            "use_risk_filter": True,
            "blocked_risk_levels": ["High", "Extreme"],
            "min_quality": 0.4,
        },
    },
    # ── crisis preset used by auto_regime_strategy ──
    "crisis": {
        "name": "بحران",
        "icon": "🚨",
        "color": "#D32F2F",
        "description": "حالت دفاعی — فقط سیگنال‌های استثنایی Eagle، حد ضرر بسیار نزدیک",
        "settings": {
            "max_open_positions": 1,
            "risk_per_trade_pct": 0.25,
            "max_drawdown_percent": 3.0,
            "pump_threshold_pct": 4.0,
            "movement_lookback_scans": 12,
            "stop_loss_pct": 1.5,
            "trailing_distance_pct": 1.0,
            "trailing_activation_pct": 0.5,
            "trailing_stop_enabled": True,
            "take_profit_percent": 3.0,
            "fixed_position_quote": 500000.0,
            "max_position_pct": 5.0,
            "max_notional_quote": 1000000.0,
            "max_total_exposure_pct": 15.0,
            "min_volume_24h": 2000000000.0,
            "cooldown_after_loss_min": 240,
            "cooldown_after_win_min": 120,
            "entry_cooldown_seconds": 1800,
            "max_new_entries_per_cycle": 1,
            "confirmation_enabled": True,
            "confirmation_pct": 0.8,
            "confirmation_max_minutes": 15,
            "invalidation_pct": 0.4,
            "max_chase_pct": 0.3,
            "min_observed_move_pct": 3.0,
            "max_nobitex_spread_pct": 0.5,
            "min_confirm_scans": 3,
            "btc_max_dump_pct": 0.5,
            "min_ask_depth_quote": 10000000.0,
            "btc_dump_exception_enabled": True,
            "eagle_min_observed_move_pct": 3.5,
            "eagle_min_1h_pct": 3.0,
            "eagle_min_volume_irt": 500000000.0,
            "eagle_max_spread_pct": 0.5,
            "use_risk_filter": True,
            "blocked_risk_levels": ["Medium", "High", "Extreme"],
            "min_quality": 0.7,
        },
    },
}


# Default mapping used when auto_regime_strategy is enabled and the
# user has not provided a `regime_strategy_map` in bot_config.json.
DEFAULT_REGIME_STRATEGY_MAP = {
    "AGGRESSIVE":   "aggressive",
    "BALANCED":     "balanced",
    "CONSERVATIVE": "conservative",
    "CRISIS":       "crisis",
}


class SettingsWindow(BaseDialog):
    def __init__(self, parent: tk.Widget, app):
        super().__init__(parent, app, title="⚙️ تنظیمات", width=700, height=760,
                         resizable=(True, True), min_size=(540, 600))
        self._testing = False
        self._build_scrollable_body()
        self._build_api_section()
        self._build_strategy_section()
        self._build_capacity_section()          # ← NEW (fix 7)
        self._build_bot_pointer_section()
        self._build_display_section()
        self._build_notifications_section()
        self._add_separator()
        self._add_ok_cancel(ok_text="💾 ذخیره و اعمال", cancel_text="انصراف",
                            ok_command=self._save_and_close)

    def _build_scrollable_body(self):
        self.canvas = tk.Canvas(self.body, bg=T.BG_APP, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self.body, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = tk.Frame(self.canvas, bg=T.BG_APP)
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass

    # ══════════════════════════════════════════════════════════
    # API section
    # ══════════════════════════════════════════════════════════
    def _build_api_section(self):
        frame = SectionFrame(self.scrollable_frame, title="اتصال به نوبیتکس",
                             title_icon="🔌")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)

        tk.Label(grid, text="صرافی:", font=T.font(size=T.FONT_SM),
                 bg=T.BG_PANEL, fg=T.TEXT_SECONDARY).grid(
            row=0, column=0, sticky="w", pady=T.PAD_XS)
        tk.Label(grid, text="نوبیتکس / تومان (IRT)",
                 font=T.font(size=T.FONT_SM, weight="bold"),
                 bg=T.BG_PANEL, fg=T.TEXT_PRIMARY).grid(
            row=0, column=1, sticky="w", padx=T.PAD_SM)

        tk.Label(grid, text="کلید عمومی:",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL,
                 fg=T.TEXT_SECONDARY).grid(
            row=1, column=0, sticky="w", pady=T.PAD_XS)
        self._api_key_entry = ttk.Entry(grid, width=40)
        self._api_key_entry.grid(row=1, column=1, sticky="ew",
                                 padx=T.PAD_SM, pady=T.PAD_XS)

        tk.Label(grid, text="کلید خصوصی:",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL,
                 fg=T.TEXT_SECONDARY).grid(
            row=2, column=0, sticky="w", pady=T.PAD_XS)
        self._api_secret_entry = ttk.Entry(grid, width=40, show="*")
        self._api_secret_entry.grid(row=2, column=1, sticky="ew",
                                    padx=T.PAD_SM, pady=T.PAD_XS)

        cfg = getattr(self.app, "_bot_cfg", None)
        if cfg is not None:
            self._api_key_entry.insert(0, str(getattr(cfg, "api_key", "") or ""))
            self._api_secret_entry.insert(0, str(getattr(cfg, "api_secret", "") or ""))

        tk.Label(
            grid,
            text="⚠️ فقط دسترسی READ + TRADE را فعال کنید؛ هرگز WITHDRAW را فعال نکنید.",
            font=T.font(size=T.FONT_XS), bg=T.BG_PANEL, fg=T.TEXT_MUTED,
            wraplength=540, justify="right", anchor="e",
        ).grid(row=3, column=0, columnspan=2, sticky="e",
               padx=T.PAD_SM, pady=T.PAD_XS)

        row = tk.Frame(grid, bg=T.BG_PANEL)
        row.grid(row=4, column=1, sticky="e", padx=T.PAD_SM, pady=T.PAD_SM)
        for text, variant, cmd in [
            ("📋 چسباندن کلید عمومی",  "primary", lambda: self._paste(self._api_key_entry)),
            ("📋 چسباندن کلید خصوصی", "primary", lambda: self._paste(self._api_secret_entry)),
            ("🔍 تست اتصال",           "ghost",   self._test_api),
        ]:
            b = tk.Button(row, text=text, command=cmd)
            style_button(b, variant, padx=T.PAD_MD, pady=T.PAD_XS, bold=False)
            b.pack(side="right", padx=(T.PAD_XS, 0))
        self._test_btn = row.winfo_children()[-1]

        self._test_result_lbl = tk.Label(
            grid, text="", font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
        )
        self._test_result_lbl.grid(row=5, column=1, sticky="e", padx=T.PAD_SM)

    def _paste(self, entry):
        text = safe_clipboard_paste(self.dlg)
        if text:
            entry.delete(0, tk.END)
            entry.insert(0, text.strip())

    def _test_api(self):
        self._test_btn.config(state="disabled")
        self._test_result_lbl.config(text="در حال تست اتصال به نوبیتکس…", fg=T.INFO)

        def worker():
            try:
                response = requests.get(NOBITEX_STATS_URL, timeout=8)
                if response.status_code == 200:
                    text = "✅ نوبیتکس در دسترس است"
                    color = T.SUCCESS_DARK
                else:
                    text = f"❌ خطای HTTP {response.status_code}"
                    color = T.DANGER
            except Exception as exc:
                text, color = f"❌ {str(exc)[:60]}", T.DANGER
            self._safe_ui_call_from_thread(lambda: self._finish_test(text, color))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_test(self, text, color):
        self._test_result_lbl.config(text=text, fg=color)
        self._test_btn.config(state="normal")

    # ══════════════════════════════════════════════════════════
    # Strategy section — with auto-regime toggle
    # ══════════════════════════════════════════════════════════
    def _build_strategy_section(self):
        frame = SectionFrame(self.scrollable_frame, title="استراتژی معاملاتی",
                             title_icon="🎯")
        frame.pack(fill="x", pady=(0, T.PAD_MD))

        # ── Auto-regime toggle ────────────────────────────────────
        cfg = getattr(self.app, "_bot_cfg", None)
        initial_auto = bool(getattr(cfg, "auto_regime_strategy", False)) if cfg else False
        self._auto_regime_var = tk.BooleanVar(value=initial_auto)

        toggle_row = tk.Frame(frame.body, bg=T.BG_PANEL)
        toggle_row.pack(fill="x", pady=(0, T.PAD_SM))
        chk = tk.Checkbutton(
            toggle_row,
            text="🤖 اعمال خودکار استراتژی بر اساس رژیم بازار",
            variable=self._auto_regime_var,
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_PANEL, fg=T.PRIMARY,
            selectcolor=T.PRIMARY_GHOST,
            activebackground=T.BG_PANEL,
            cursor="hand2", anchor="e",
            command=self._on_auto_regime_toggle,
        )
        chk.pack(side="right")
        ToolTip(
            chk,
            "وقتی فعال باشد، رژیم بازار (تهاجمی/متعادل/محافظه‌کارانه/بحران) "
            "خودش استراتژی مناسب را در هر اسکن اعمال می‌کند.",
        )

        tk.Label(
            frame.body,
            text=(
                "در حالت خودکار، تنظیمات زیر با تغییر رژیم بازار به‌طور خودکار "
                "روی هر دو tracker اعمال می‌شوند. فایل bot_config.json "
                "دست‌نخورده می‌ماند — با خاموش کردن، تنظیمات دستی شما برمی‌گردد."
            ),
            font=T.font(size=T.FONT_XS), bg=T.BG_PANEL, fg=T.TEXT_MUTED,
            anchor="e", justify="right", wraplength=560,
        ).pack(fill="x", anchor="e", pady=(0, T.PAD_SM))

        # ── Mapping table ─────────────────────────────────────────
        self._mapping_frame = tk.Frame(frame.body, bg=T.BG_PANEL)
        self._mapping_frame.pack(fill="x", pady=(0, T.PAD_SM))
        self._build_mapping_table()

        # ── Strategy cards container ──────────────────────────────
        self._cards_container = tk.Frame(frame.body, bg=T.BG_PANEL)
        self._cards_container.pack(fill="x")

        tk.Label(
            self._cards_container,
            text="📋 انتخاب دستی استراتژی:",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            anchor="e", justify="right",
        ).pack(fill="x", anchor="e", pady=(0, T.PAD_XS))

        for key, strategy in STRATEGY_PRESETS.items():
            self._make_strategy_row(self._cards_container, key, strategy)

        # Status label
        self._strategy_status = tk.Label(
            frame.body, text="", font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
            anchor="e", justify="right", wraplength=560,
        )
        self._strategy_status.pack(fill="x", anchor="e", pady=(T.PAD_XS, 0))

        # Apply initial enabled/disabled state
        self._refresh_cards_state()

    def _build_mapping_table(self):
        """Build the regime → strategy mapping table."""
        for widget in self._mapping_frame.winfo_children():
            widget.destroy()

        cfg = getattr(self.app, "_bot_cfg", None)
        mapping = getattr(cfg, "regime_strategy_map", None) if cfg else None
        if not isinstance(mapping, dict):
            mapping = dict(DEFAULT_REGIME_STRATEGY_MAP)

        tk.Label(
            self._mapping_frame,
            text="🗺️  نگاشت رژیم → استراتژی:",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            anchor="e", justify="right",
        ).pack(fill="x", anchor="e", pady=(0, T.PAD_XS))

        regime_labels = {
            "AGGRESSIVE":   "تهاجمی",
            "BALANCED":     "متعادل",
            "CONSERVATIVE": "محافظه‌کارانه",
            "CRISIS":       "بحران",
        }
        regime_icons = {
            "AGGRESSIVE":   "🚀",
            "BALANCED":     "⚖️",
            "CONSERVATIVE": "🛡️",
            "CRISIS":       "🚨",
        }

        table = tk.Frame(self._mapping_frame, bg=T.BG_PANEL)
        table.pack(fill="x")

        for regime, strategy_key in mapping.items():
            strategy = STRATEGY_PRESETS.get(strategy_key, {})
            strategy_name = strategy.get("name", strategy_key)
            strategy_icon = strategy.get("icon", "❓")
            strategy_color = strategy.get("color", T.TEXT_SECONDARY)
            regime_icon = regime_icons.get(regime, "❓")
            regime_label = regime_labels.get(regime, regime)

            row = tk.Frame(table, bg=T.BG_PANEL)
            row.pack(fill="x", pady=1)

            tk.Label(
                row,
                text=f"{strategy_icon} {strategy_name}",
                font=T.font(size=T.FONT_SM, weight="bold"),
                bg=T.BG_PANEL, fg=strategy_color,
                anchor="e", width=20,
            ).pack(side="right")

            tk.Label(
                row,
                text="→",
                font=T.font(size=T.FONT_SM),
                bg=T.BG_PANEL, fg=T.TEXT_MUTED,
                width=3,
            ).pack(side="right")

            tk.Label(
                row,
                text=f"{regime_icon} {regime_label}",
                font=T.font(size=T.FONT_SM),
                bg=T.BG_PANEL, fg=T.TEXT_SECONDARY,
                anchor="e", width=15,
            ).pack(side="right")

    def _on_auto_regime_toggle(self):
        """Called when user toggles auto_regime_strategy checkbox."""
        self._refresh_cards_state()
        enabled = self._auto_regime_var.get()
        status = (
            "✅ حالت خودکار فعال شد — با تغییر رژیم، استراتژی به‌طور خودکار اعمال می‌شود."
            if enabled else
            "ℹ️ حالت دستی فعال شد — استراتژی را خودتان انتخاب کنید."
        )
        self._strategy_status.config(text=status, fg=T.INFO)
        try:
            self.dlg.after(5000, lambda: self._strategy_status.config(text=""))
        except Exception:
            pass

    def _refresh_cards_state(self):
        """Grey out strategy cards when auto-regime is enabled."""
        auto_on = self._auto_regime_var.get()
        state = "disabled" if auto_on else "normal"
        cursor = "" if auto_on else "hand2"

        for widget in self._cards_container.winfo_children():
            try:
                if isinstance(widget, tk.Frame):
                    for sub in widget.winfo_children():
                        if isinstance(sub, tk.Frame):
                            for inner in sub.winfo_children():
                                if isinstance(inner, tk.Label):
                                    inner.config(
                                        cursor=cursor,
                                        fg=T.TEXT_MUTED if auto_on
                                        else inner.cget("fg"),
                                    )
            except Exception:
                pass

    def _make_strategy_row(self, parent, key: str, strategy: Dict[str, Any]):
        """Create a clickable card for one strategy."""
        color = strategy.get("color", T.PRIMARY)

        row = tk.Frame(parent, bg=T.BG_PANEL, cursor="hand2",
                       highlightthickness=1, highlightbackground=T.BORDER)
        row.pack(fill="x", pady=(0, T.PAD_XS))

        inner = tk.Frame(row, bg=T.BG_PANEL)
        inner.pack(side="right", fill="both", expand=True,
                   padx=T.PAD_SM, pady=T.PAD_XS)

        title = tk.Label(
            inner,
            text=f"{strategy['icon']}  {strategy['name']}",
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.BG_PANEL, fg=color,
            anchor="e", justify="right",
        )
        title.pack(fill="x", anchor="e")

        desc = tk.Label(
            inner,
            text=strategy["description"],
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
            anchor="e", justify="right", wraplength=520,
        )
        desc.pack(fill="x", anchor="e")

        for widget in (row, inner, title, desc):
            widget.bind("<Button-1>", lambda e, k=key: self._on_card_click(k))
            widget.bind("<Enter>", lambda e, r=row, c=color: self._on_card_hover(r, c))
            widget.bind("<Leave>", lambda e, r=row: self._on_card_unhover(r))

    def _on_card_hover(self, row, color):
        if self._auto_regime_var.get():
            return
        try:
            row.config(highlightbackground=color)
        except Exception:
            pass

    def _on_card_unhover(self, row):
        try:
            row.config(highlightbackground=T.BORDER)
        except Exception:
            pass

    def _on_card_click(self, key: str):
        if self._auto_regime_var.get():
            messagebox.showinfo(
                "حالت خودکار فعال است",
                "برای انتخاب دستی استراتژی، ابتدا تیک «اعمال خودکار بر اساس رژیم» "
                "را بردارید.",
                parent=self.dlg,
            )
            return
        self._apply_strategy(key)

    # ══════════════════════════════════════════════════════════
    # FIX: validate preset fields before setattr
    # ══════════════════════════════════════════════════════════
    def _apply_strategy(self, key: str) -> None:
        """Apply a strategy preset to bot_config and persist it.

        FIX v4.1:
            Before calling `setattr(cfg, field, value)`, validate that
            `field` is a declared dataclass field on BotConfig.  The
            previous version blindly applied every key, so a typo in a
            preset ("stop_less_pct" instead of "stop_loss_pct") created
            an orphan attribute that `asdict()` silently dropped on the
            next save — the user saw a "17 fields applied" message while
            one of them was silently lost.
        """
        strategy = STRATEGY_PRESETS.get(key)
        if not strategy:
            return

        confirm_text = (
            f"استراتژی «{strategy['name']}»\n\n"
            f"{strategy['description']}\n\n"
            "تنظیمات زیر بازنویسی می‌شوند:\n"
            "• اندازه پوزیشن و حد ضرر\n"
            "• آستانه‌های ورود و فیلترها\n"
            "• کول‌داون‌ها و تأییدها\n\n"
            "کلیدهای API، مسیرها و حالت اجرا (کاغذی/واقعی) دست‌نخورده می‌مانند.\n\n"
            "ادامه می‌دهید؟"
        )
        if not messagebox.askyesno("اعمال استراتژی", confirm_text, parent=self.dlg):
            return

        cfg = getattr(self.app, "_bot_cfg", None)
        if cfg is None:
            try:
                cfg = load_config(self.app._bot_config_path)
            except Exception as exc:
                logger.error("Could not load config to apply strategy: %s", exc)
                messagebox.showerror(
                    "خطا",
                    f"بارگذاری تنظیمات فعلی ناموفق بود:\n{exc}",
                    parent=self.dlg,
                )
                return

        # FIX v4.1: validate every preset field against the dataclass.
        valid_fields = set(BotConfig.__dataclass_fields__.keys())
        unknown: list[str] = []
        applied_count = 0
        for field_name, value in strategy["settings"].items():
            if field_name not in valid_fields:
                unknown.append(field_name)
                continue
            try:
                setattr(cfg, field_name, value)
                applied_count += 1
            except Exception as exc:
                logger.warning(
                    "Could not set %s=%s on config: %s", field_name, value, exc,
                )

        if unknown:
            logger.warning(
                "Strategy '%s' contained %d unknown field(s) that were NOT "
                "applied: %s",
                key, len(unknown), ", ".join(sorted(unknown)),
            )

        try:
            self.app._bot_cfg = cfg
            save_config(cfg, self.app._bot_config_path)
            logger.info(
                "Strategy '%s' applied and saved (%d fields, %d skipped).",
                key, applied_count, len(unknown),
            )
        except Exception as exc:
            logger.error("Could not save strategy: %s", exc, exc_info=True)
            messagebox.showerror(
                "خطای ذخیره‌سازی",
                f"ذخیره استراتژی ناموفق بود:\n{exc}",
                parent=self.dlg,
            )
            return

        try:
            if hasattr(self.app, "_apply_strategy_settings"):
                self.app._apply_strategy_settings(cfg)
            if hasattr(self.app, "_apply_live_sizing"):
                self.app._apply_live_sizing(cfg)
        except Exception as exc:
            logger.debug("Could not sync strategy to trackers: %s", exc)

        # FIX v4.1: refresh the live capacity summary after a preset swap.
        try:
            self._refresh_capacity_summary()
        except Exception:
            pass

        status_msg = (
            f"✅ استراتژی «{strategy['name']}» اعمال و ذخیره شد "
            f"({applied_count} فیلد)."
        )
        if unknown:
            status_msg += f"  ⚠️ {len(unknown)} فیلد نامعتبر نادیده گرفته شد."
        self._strategy_status.config(text=status_msg, fg=T.SUCCESS_DARK)
        try:
            self.dlg.after(6000, lambda: self._strategy_status.config(text=""))
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    # FIX 7 (v4.1): live capacity summary panel
    # ══════════════════════════════════════════════════════════
    def _build_capacity_section(self):
        """
        Show the operator how many positions actually fit at the
        current fixed lot within the exposure cap.  The number comes
        from `BotConfig.capacity_summary()` so the GUI and the
        load_config() warning share one source of truth.
        """
        frame = SectionFrame(
            self.scrollable_frame,
            title="ظرفیت پوزیشن (Capacity)",
            title_icon="📐",
        )
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(0, weight=1)

        self._capacity_lbl = tk.Label(
            grid,
            text="",
            font=T.font(size=T.FONT_SM),
            bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
            anchor="e", justify="right",
            wraplength=560,
        )
        self._capacity_lbl.pack(fill="x", anchor="e", pady=(0, T.PAD_XS))

        self._capacity_hint = tk.Label(
            grid,
            text="",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
            anchor="e", justify="right",
            wraplength=560,
        )
        self._capacity_hint.pack(fill="x", anchor="e")

        # Bind the relevant fields to auto-refresh on change.
        # (These vars live on BotSettingsWindow's UI in real_trading_panel,
        # but the top-level SettingsWindow has no such vars — so we only
        # recompute on open and on strategy change.  The BotSettingsWindow
        # in real_trading_panel.py has its own copy of this label wired
        # to trace callbacks on every keystroke.)
        self._refresh_capacity_summary()

    def _refresh_capacity_summary(self):
        """Recompute and display the capacity summary."""
        cfg = getattr(self.app, "_bot_cfg", None)
        if cfg is None:
            try:
                cfg = load_config(self.app._bot_config_path)
            except Exception:
                cfg = BotConfig()

        try:
            summary = cfg.capacity_summary()
        except Exception as exc:
            logger.debug("capacity_summary() failed: %s", exc)
            self._capacity_lbl.config(text="محاسبه ظرفیت ناموفق بود.", fg=T.TEXT_MUTED)
            self._capacity_hint.config(text="")
            return

        fits = summary["fits"]
        effective = summary["effective_max"]
        configured = summary["configured_max"]
        cap = summary["exposure_cap"]
        per_trade = summary["per_trade_notional"]
        mode = summary["mode"]

        if mode != "fixed":
            self._capacity_lbl.config(
                text=(
                    f"حالت {mode}: ظرفیت بر اساس حد ضرر هر نماد محاسبه می‌شود. "
                    f"حداکثر مجاز: {configured} پوزیشن."
                ),
                fg=T.TEXT_SECONDARY,
            )
            self._capacity_hint.config(
                text="برای دیدن تعداد دقیق، حالت position_size_mode را به fixed تغییر دهید.",
                fg=T.TEXT_MUTED,
            )
            return

        if summary["is_mismatch"]:
            self._capacity_lbl.config(
                text=(
                    f"⚠️ فقط ~{fits} از {configured} پوزیشن در سقف مواجهه "
                    f"({cap:,.0f}) جای می‌گیرد."
                ),
                fg=T.WARNING_DARK,
            )
            self._capacity_hint.config(
                text=(
                    f"هر معامله {per_trade:,.0f} — سیگنال‌های بعد از "
                    f"پوزیشن {fits} خودکار shrink می‌شوند یا رد می‌شوند."
                ),
                fg=T.WARNING_DARK,
            )
        else:
            self._capacity_lbl.config(
                text=(
                    f"✅ {configured} پوزیشن با اندازه {per_trade:,.0f} در "
                    f"سقف مواجهه {cap:,.0f} جای می‌گیرد."
                ),
                fg=T.SUCCESS_DARK,
            )
            self._capacity_hint.config(
                text=f"مجاز مؤثر: {effective} پوزیشن — بدون shrink خودکار.",
                fg=T.SUCCESS_DARK,
            )

    # ══════════════════════════════════════════════════════════
    # Advanced bot settings pointer
    # ══════════════════════════════════════════════════════════
    def _build_bot_pointer_section(self):
        frame = SectionFrame(self.scrollable_frame, title="تنظیمات پیشرفته ربات",
                             title_icon="🤖")
        frame.pack(fill="x", pady=(0, T.PAD_MD))

        tk.Label(
            frame.body,
            text="برای ویرایش دقیق تک‌تک پارامترها (اندازه پوزیشن، حد ضرر، اسکن زنده، حالت اجرا) وارد بخش SmartEagle Bot شوید.",
            font=T.font(size=T.FONT_SM), bg=T.BG_PANEL, fg=T.TEXT_SECONDARY,
            justify="right", wraplength=560, anchor="e",
        ).pack(anchor="e", pady=T.PAD_SM)

        b = tk.Button(frame.body, text="🤖 SmartEagle Bot → ⚙️ Settings",
                      command=self._open_bot_settings)
        style_button(b, "primary", padx=T.PAD_LG, pady=T.PAD_SM, bold=True)
        b.pack(anchor="e", pady=(0, T.PAD_SM))

    def _build_display_section(self):
        frame = SectionFrame(self.scrollable_frame, title="نمایش و رفتار",
                             title_icon="🎨")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        for text, var, tip in [
            ("🔄 به‌روزرسانی خودکار",
             self.app.auto_refresh_var,
             "دریافت خودکار داده‌های بازار نوبیتکس"),
            ("🎯 حالت ساده به‌صورت پیش‌فرض",
             self.app.simple_mode_var,
             "نمایش ستون‌های کمتر در جدول"),
        ]:
            row = tk.Frame(frame.body, bg=T.BG_PANEL)
            row.pack(fill="x", pady=T.PAD_XS)
            chk = tk.Checkbutton(
                row, text=text, variable=var,
                font=T.font(size=T.FONT_SM),
                bg=T.BG_PANEL, fg=T.TEXT_PRIMARY, selectcolor=T.PRIMARY_GHOST,
                activebackground=T.BG_PANEL, cursor="hand2",
                anchor="e",
            )
            chk.pack(side="right")
            ToolTip(chk, tip)

    def _build_notifications_section(self):
        frame = SectionFrame(self.scrollable_frame, title="اعلان‌ها",
                             title_icon="🔔")
        frame.pack(fill="x", pady=(0, T.PAD_MD))
        grid = frame.body
        grid.columnconfigure(1, weight=1)
        tk.Label(grid, text="ایمیل برای هشدارها:",
                 font=T.font(size=T.FONT_SM), bg=T.BG_PANEL,
                 fg=T.TEXT_SECONDARY).grid(
            row=0, column=0, sticky="w", pady=T.PAD_XS)
        self._email_entry = ttk.Entry(
            grid, textvariable=self.app.email_alert_var, width=30,
        )
        self._email_entry.grid(row=0, column=1, sticky="ew",
                               padx=T.PAD_SM, pady=T.PAD_XS)

    def _open_bot_settings(self):
        opener = getattr(self.app, "open_bot_settings", None)
        self.close()
        if callable(opener):
            opener()

    # ══════════════════════════════════════════════════════════
    # Save
    # ══════════════════════════════════════════════════════════
    def _save_and_close(self):
        cfg = getattr(self.app, "_bot_cfg", None)
        if cfg is not None:
            cfg.api_key = self._api_key_entry.get().strip()
            cfg.api_secret = self._api_secret_entry.get().strip()
            cfg.exchange = "nobitex"
            cfg.nobitex_market = "IRT"
            cfg.quote_currency = "IRT"
            cfg.quote_unit = "rial"
            # Auto-regime toggle
            cfg.auto_regime_strategy = bool(self._auto_regime_var.get())

            try:
                self.app._bot_cfg = cfg
                save_config(cfg, self.app._bot_config_path)
                logger.info(
                    "[SETTINGS] Saved. auto_regime_strategy=%s",
                    cfg.auto_regime_strategy,
                )
            except Exception as exc:
                logger.error("Could not save settings: %s", exc, exc_info=True)
                try:
                    messagebox.showerror(
                        "خطای ذخیره‌سازی",
                        f"ذخیره تغییرات با خطا مواجه شد:\n{exc}",
                        parent=self.dlg,
                    )
                except Exception:
                    pass
                return

        self.app.save_settings()
        try:
            self.app.refresh()
        except Exception as exc:
            logger.warning("Refresh after settings save failed: %s", exc)

        try:
            if cfg is not None:
                if hasattr(self.app, "_apply_strategy_settings"):
                    self.app._apply_strategy_settings(cfg)
                if hasattr(self.app, "_apply_live_sizing"):
                    self.app._apply_live_sizing(cfg)
        except Exception as exc:
            logger.debug("Could not re-apply settings after save: %s", exc)

        self.close()

    def on_close(self) -> bool:
        try:
            self.canvas.unbind("<MouseWheel>")
        except Exception:
            pass
        return True