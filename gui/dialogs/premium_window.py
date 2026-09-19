# source/gui/dialogs/premium_window.py
"""
PremiumWindow v2.5 — Activation Code + Fixed USDT Verification

Changes vs v2.4:
  - FIX (v2.5): `_verify_worker()` now passes `fetch_func=self.tron_client._request`
    to `verify_usdt_transaction()`.  In v2.4 the call did NOT provide a
    network function, so `verify_usdt_transaction` returned immediately
    with ("No network function provided") — every USDT payment was
    silently rejected even after the blockchain confirmed it.  The fix
    wires the existing TronscanClient (already constructed in __init__)
    into the verification path.
  - Retained v2.4 activation-code section.

Changes vs v2.3 (retained):
  - Added activation code section for admin secret or signed codes.
  - Integrated verify_and_activate_code from core.user_status.
  - Kept all v2.3 pricing and layout changes.
"""
from __future__ import annotations

import logging
import threading
from typing import Dict, List, Optional, Tuple
import tkinter as tk
from tkinter import ttk

from api.api_tronscan import TronscanClient
from core.config import WALLET_ADDRESS, TRIAL_DAYS, DAILY_FREE_REFRESH_LIMIT
from core.user_status import (
    verify_usdt_transaction,
    load_user_status,
    get_status_summary,
    verify_and_activate_code,          # ← v2.4
)
from gui.dialogs.base_dialog import BaseDialog
from gui.dialogs.components import safe_clipboard_paste
from gui.gui_helpers import ToolTip
from gui.ui_theme import Theme

logger = logging.getLogger(__name__)
T = Theme

try:
    import qrcode
    from PIL import Image, ImageTk
    _HAS_QR = True
except ImportError:
    _HAS_QR = False


# ══════════════════════════════════════════════════════════════
# ✅ FIX: تبدیل ایمن مقادیر padding به int
# ══════════════════════════════════════════════════════════════

def _px(value) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        logger.warning("_px: could not convert %r to int, using 0", value)
        return 0


def _pad(a, b=None):
    if b is None:
        return _px(a)
    return (_px(a), _px(b))


# ══════════════════════════════════════════════════════════════
# Plan Definitions (UPDATED PRICING)
# ══════════════════════════════════════════════════════════════

_PLANS: List[Tuple[str, int, float, str]] = [
    ("1 Month  — 39 USDT",              1,  39.0, ""),
    ("3 Months — 99 USDT  (15% off)",   3,  99.0, "🔥 Popular"),
    ("12 Months — 299 USDT (36% off)", 12, 299.0, "⭐ Best Value"),
]

_PLAN_MAP: Dict[str, Tuple[int, float]] = {
    label: (months, price)
    for label, months, price, _ in _PLANS
}

# ── مقایسه Free vs Premium (unchanged) ───────────────────────
_COMPARE_ROWS: List[Tuple[str, str, str]] = [
    ("Daily Scans",          f"{DAILY_FREE_REFRESH_LIMIT} / day", "Unlimited"),
    ("Coins per Scan",       "250",                               "500+"),
    ("All 6 Strategies",     "✅",                                "✅"),
    ("20+ Indicators",       "✅",                                "✅"),
    ("Risk Assessment",      "✅",                                "✅"),
    ("Paper Trading",        "✅",                                "✅"),
    ("Backtesting",          "Limited",                           "✅ Full"),
    ("AI Trade Learner",     "✅",                                "✅"),
    ("AI Win Rate column",   "🔒 Locked",                         "✅ Unlocked"),
    ("Trading Bot",          "7-day trial",                       "✅ Unlimited"),
    ("Auto-Trade Mode",      "🔒 Locked",                         "✅ Unlocked"),
    ("AI-filtered Bot",      "🔒 Locked",                         "✅ Unlocked"),
    ("Priority Support",     "❌",                                "✅ WhatsApp"),
    ("Early Feature Access", "❌",                                "✅ v7.0"),
]

# ── Testimonials (unchanged) ─────────────────────────────────
_TESTIMONIALS: List[Tuple[str, str, str]] = [
    (
        "The Paper Trading tab showed my real win rate was 38%, not 60%. "
        "Fixed it in 2 weeks with the AI learner.",
        "Ahmad R.", "Full-time Trader • Dubai",
    ),
    (
        "Bot ran overnight, hit +6% TP on ETH. Risk engine blocked 2 High-Risk "
        "signals. Woke up profitable.",
        "Maria S.", "Software Engineer & Trader • London",
    ),
    (
        "Learning Dashboard showed I always lost when RSI > 72. "
        "That one insight saved me thousands.",
        "Layla M.", "Quant Trader • Beirut",
    ),
]

# ── Features (unchanged) ─────────────────────────────────────
_FEATURES: List[str] = [
    "✅  Unlimited market scans",
    "✅  500+ coins simultaneously",
    "✅  AI Win Rate predictions",
    "✅  Trading Bot — unlimited",
    "✅  Auto-Trade mode",
    "✅  Full historical backtesting",
    "✅  AI-filtered signals",
    "✅  Priority WhatsApp support",
    "✅  Early access to v7.0",
]

# ── ROI Rows (UPDATED with new pricing) ──────────────────────
_ROI_ROWS: List[Tuple[str, str, str]] = [
    ("Average losing trade (no system)",    "−$85",      "DANGER"),
    ("Bad trades per month (typical)",      "×4",        "NEUTRAL"),
    ("Annual loss from avoidable mistakes", "−$1,020",   "DANGER"),
    ("AI blocks ~40% of bad trades",        "+$408 saved","SUCCESS"),
    ("Backtest win rate improvement",       "+$612/yr",  "SUCCESS"),
    ("Annual Premium cost (12‑month plan)", "−$299/yr",  "MUTED"),
    ("Net annual benefit",                  "≈ +$721",   "SUCCESS"),
]


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════

def _call_safe(obj, method: str) -> None:
    try:
        getattr(obj, method)()
    except Exception as exc:
        logger.debug("Could not call %s: %s", method, exc)


def _get_user_summary(app) -> dict:
    device_id = getattr(app, "device_id", None)
    try:
        return get_status_summary(device_id=device_id)
    except Exception as exc:
        logger.error("_get_user_summary failed: %s", exc)
        return {}


def _roi_color(tag: str) -> str:
    return {
        "DANGER":  T.DANGER_DARK,
        "SUCCESS": T.SUCCESS_DARK,
        "MUTED":   T.TEXT_MUTED,
        "NEUTRAL": T.TEXT_PRIMARY,
    }.get(tag, T.TEXT_SECONDARY)


# ══════════════════════════════════════════════════════════════
# PremiumWindow
# ══════════════════════════════════════════════════════════════

class PremiumWindow(BaseDialog):
    def __init__(self, parent: tk.Widget, app) -> None:
        super().__init__(
            parent, app,
            title="⭐ Go Premium — Unlock Your Full Edge",
            width=680, height=820,
            resizable=(False, True),
            min_size=(600, 780),
        )

        self.tron_client  = TronscanClient()
        self._qr_photo:   Optional[ImageTk.PhotoImage] = None
        self._verifying   = False
        self._plan_var    = tk.StringVar(value=_PLANS[1][0])  # default 3 months

        self._nb = ttk.Notebook(self.body)
        self._nb.pack(fill="both", expand=True)

        self._tab_upgrade = tk.Frame(self._nb, bg=T.BG_APP)
        self._tab_compare = tk.Frame(self._nb, bg=T.BG_APP)
        self._tab_payment = tk.Frame(self._nb, bg=T.BG_APP)

        self._nb.add(self._tab_upgrade, text="  🚀 Why Upgrade?  ")
        self._nb.add(self._tab_compare, text="  📊 Compare Plans  ")
        self._nb.add(self._tab_payment, text="  💳 Pay & Activate  ")

        self._build_upgrade_tab()
        self._build_compare_tab()
        self._build_payment_tab()

        self._add_separator()
        self._build_footer_buttons()

        self._plan_var.trace_add("write", lambda *_: self._update_price())
        self._update_price()

    @property
    def _win(self) -> tk.Widget:
        for attr in ("dlg", "window", "root"):
            w = getattr(self, attr, None)
            if w is not None:
                return w
        return self.body

    # ══════════════════════════════════════════════════════════
    # TAB 1 — Why Upgrade?
    # ══════════════════════════════════════════════════════════

    def _build_upgrade_tab(self) -> None:
        canvas    = tk.Canvas(self._tab_upgrade, bg=T.BG_APP, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self._tab_upgrade, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=T.BG_APP)

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.bind(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._build_status_banner(scroll_frame)
        self._build_urgency_banner(scroll_frame)
        self._build_features_section(scroll_frame)
        self._build_roi_section(scroll_frame)
        self._build_testimonials(scroll_frame)
        self._build_upgrade_cta(scroll_frame)

    def _build_status_banner(self, parent: tk.Widget) -> None:
        summary = _get_user_summary(self.app)

        if summary.get("is_premium"):
            days  = summary.get("plan_days_left", 0)
            plan  = summary.get("plan", "Premium")
            text  = f"✅  Current Plan: {plan.upper()}  |  {days} days remaining"
            color, bg = T.SUCCESS_DARK, T.SUCCESS_BG
        elif summary.get("trial_active"):
            days  = summary.get("trial_days_left", 0)
            text  = f"🎁  Trial Active: {days} day(s) remaining — Don't lose access!"
            color, bg = T.INFO_DARK, T.INFO_BG
        else:
            used  = summary.get("refresh_count", 0)
            text  = f"🆓  Free Plan: {used}/{DAILY_FREE_REFRESH_LIMIT} scans used today"
            color, bg = T.TEXT_SECONDARY, T.BG_PANEL

        bar = tk.Frame(parent, bg=bg, pady=_px(T.PAD_SM))
        bar.pack(fill="x", pady=_pad(0, T.PAD_SM))

        tk.Label(
            bar,
            text=text,
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=bg, fg=color,
        ).pack(padx=_px(T.PAD_XL))

    def _build_urgency_banner(self, parent: tk.Widget) -> None:
        summary = _get_user_summary(self.app)
        if summary.get("is_premium"):
            return

        if summary.get("trial_active"):
            days = summary.get("trial_days_left", 0)
            msg  = (
                f"⏰  Your FREE trial expires in {days} day(s). "
                "Upgrade now to keep full access."
            )
        else:
            msg = (
                "⚡  95% of retail traders lose because they lack a system. "
                "Don't be one of them."
            )

        banner = tk.Frame(parent, bg="#B91C1C", pady=_px(T.PAD_SM))
        banner.pack(fill="x", pady=_pad(0, T.PAD_MD))

        tk.Label(
            banner,
            text=msg,
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg="#B91C1C", fg="#FFFFFF",
            wraplength=580, justify="center",
        ).pack(padx=_px(T.PAD_XL))

    def _build_features_section(self, parent: tk.Widget) -> None:
        card = tk.Frame(
            parent, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        card.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(0, T.PAD_SM))

        tk.Label(
            card,
            text="🚀  What You Unlock with Premium",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK,
            padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
            anchor="w",
        ).pack(fill="x")

        inner = tk.Frame(card, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_MD))

        half = len(_FEATURES) // 2 + len(_FEATURES) % 2
        col1 = tk.Frame(inner, bg=T.BG_PANEL)
        col2 = tk.Frame(inner, bg=T.BG_PANEL)
        col1.pack(side="left", fill="x", expand=True)
        col2.pack(side="left", fill="x", expand=True)

        for i, feat in enumerate(_FEATURES):
            col = col1 if i < half else col2
            tk.Label(
                col,
                text=feat,
                font=T.font(size=T.FONT_XS),
                bg=T.BG_PANEL, fg=T.TEXT_SECONDARY,
                anchor="w", justify="left",
            ).pack(anchor="w", pady=1)

    def _build_roi_section(self, parent: tk.Widget) -> None:
        card = tk.Frame(
            parent, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        card.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(0, T.PAD_SM))

        tk.Label(
            card,
            text="💰  The Real Cost of NOT Going Premium",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg="#FFF8E1", fg="#92400E",
            padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
            anchor="w",
        ).pack(fill="x")

        inner = tk.Frame(card, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_MD))

        for i, (label, value, color_tag) in enumerate(_ROI_ROWS):
            row_bg = T.BG_PANEL if i % 2 == 0 else T.BG_ROW_ALT
            row    = tk.Frame(inner, bg=row_bg)
            row.pack(fill="x")

            tk.Label(
                row,
                text=label,
                font=T.font(size=T.FONT_XS),
                bg=row_bg, fg=T.TEXT_SECONDARY,
                anchor="w", width=38,
            ).pack(side="left", padx=_pad(0, T.PAD_SM))

            tk.Label(
                row,
                text=value,
                font=T.font(size=T.FONT_XS, weight="bold"),
                bg=row_bg, fg=_roi_color(color_tag),
                anchor="e",
            ).pack(side="right", padx=_px(T.PAD_SM))

    def _build_testimonials(self, parent: tk.Widget) -> None:
        card = tk.Frame(
            parent, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        card.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(0, T.PAD_SM))

        tk.Label(
            card,
            text="💬  What Traders Are Saying",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK,
            padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
            anchor="w",
        ).pack(fill="x")

        inner = tk.Frame(card, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_MD))

        for quote, name, role in _TESTIMONIALS:
            t_frame = tk.Frame(
                inner, bg=T.BG_APP,
                highlightthickness=1, highlightbackground=T.BORDER,
            )
            t_frame.pack(fill="x", pady=_px(T.PAD_XS))

            tk.Label(
                t_frame,
                text=f'"{quote}"',
                font=T.font(size=T.FONT_XS),
                bg=T.BG_APP, fg=T.TEXT_SECONDARY,
                wraplength=560, justify="left",
                padx=_px(T.PAD_MD),
                pady=_px(T.PAD_SM),
            ).pack(anchor="w")

            tk.Label(
                t_frame,
                text=f"— {name}  |  {role}",
                font=T.font(size=T.FONT_XS, weight="bold"),
                bg=T.BG_APP, fg=T.TEXT_MUTED,
                padx=_px(T.PAD_MD),
                pady=_px(T.PAD_SM),
            ).pack(anchor="e")

    def _build_upgrade_cta(self, parent: tk.Widget) -> None:
        cta = tk.Frame(parent, bg=T.BG_APP)
        cta.pack(fill="x", padx=_px(T.PAD_MD), pady=_px(T.PAD_MD))

        tk.Button(
            cta,
            text="💎  Upgrade Now — Start in 60 Seconds",
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
            activebackground=T.PRIMARY_DARK,
            activeforeground=T.TEXT_ON_PRIMARY,
            relief="flat", cursor="hand2",
            padx=_px(T.PAD_2XL), pady=_px(T.PAD_MD), bd=0,
            command=lambda: self._nb.select(2),
        ).pack(fill="x")

        tk.Label(
            cta,
            text=(
                "No credit card. No account. "
                "Pay with USDT (TRC20). Instant activation."
            ),
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP, fg=T.TEXT_MUTED,
        ).pack(pady=_pad(T.PAD_XS, 0))

    # ══════════════════════════════════════════════════════════
    # TAB 2 — Compare Plans
    # ══════════════════════════════════════════════════════════

    def _build_compare_tab(self) -> None:
        outer = tk.Frame(self._tab_compare, bg=T.BG_APP)
        outer.pack(fill="both", expand=True, padx=_px(T.PAD_MD), pady=_px(T.PAD_MD))

        tk.Label(
            outer,
            text="Free vs Premium — Every Feature Compared",
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.BG_APP, fg=T.TEXT_PRIMARY,
        ).pack(pady=_pad(0, T.PAD_MD))

        table_frame = tk.Frame(
            outer, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        table_frame.pack(fill="both", expand=True)

        header = tk.Frame(table_frame, bg=T.PRIMARY_GHOST)
        header.pack(fill="x")

        for text, width in [("Feature", 25), ("Free", 12), ("⭐ Premium", 14)]:
            tk.Label(
                header,
                text=text,
                font=T.font(size=T.FONT_SM, weight="bold"),
                bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK,
                width=width, anchor="center",
                pady=_px(T.PAD_SM),
            ).pack(side="left")

        for i, (feature, free_val, prem_val) in enumerate(_COMPARE_ROWS):
            row_bg = T.BG_PANEL if i % 2 == 0 else T.BG_ROW_ALT
            row    = tk.Frame(table_frame, bg=row_bg)
            row.pack(fill="x")

            tk.Label(
                row, text=feature,
                font=T.font(size=T.FONT_XS),
                bg=row_bg, fg=T.TEXT_SECONDARY,
                width=25, anchor="w",
                padx=_px(T.PAD_SM),
            ).pack(side="left")

            free_color = (
                T.SUCCESS_DARK if free_val == "✅"
                else T.DANGER_DARK if ("🔒" in free_val or free_val == "❌")
                else T.TEXT_SECONDARY
            )
            tk.Label(
                row, text=free_val,
                font=T.font(size=T.FONT_XS),
                bg=row_bg, fg=free_color,
                width=12, anchor="center",
            ).pack(side="left")

            prem_color = T.SUCCESS_DARK if "✅" in prem_val else T.PRIMARY
            tk.Label(
                row, text=prem_val,
                font=T.font(size=T.FONT_XS, weight="bold"),
                bg=row_bg, fg=prem_color,
                width=14, anchor="center",
            ).pack(side="left")

        self._build_price_cards(outer)

    def _build_price_cards(self, parent: tk.Widget) -> None:
        tk.Label(
            parent,
            text="Choose Your Plan",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.TEXT_PRIMARY,
        ).pack(pady=_pad(T.PAD_LG, T.PAD_SM))

        cards_row = tk.Frame(parent, bg=T.BG_APP)
        cards_row.pack(fill="x")

        for label, months, price, badge in _PLANS:
            is_popular = (months == 3)
            card_bg    = T.PRIMARY_GHOST if is_popular else T.BG_PANEL
            card       = tk.Frame(
                cards_row,
                bg=card_bg,
                highlightthickness=2,
                highlightbackground=T.GOLD if is_popular else T.BORDER,
            )
            card.pack(side="left", expand=True, fill="x", padx=_px(T.PAD_SM))

            if badge:
                tk.Label(
                    card, text=badge,
                    font=T.font(size=T.FONT_XS, weight="bold"),
                    bg=T.GOLD if is_popular else T.PRIMARY,
                    fg="#FFFFFF",
                    padx=_px(T.PAD_SM), pady=2,
                ).pack(fill="x")

            tk.Label(
                card,
                text=f"{months} Month{'s' if months > 1 else ''}",
                font=T.font(size=T.FONT_SM, weight="bold"),
                bg=card_bg, fg=T.TEXT_PRIMARY,
            ).pack(pady=_pad(T.PAD_SM, 0))

            tk.Label(
                card,
                text=f"{price:.0f} USDT",
                font=T.font(size=T.FONT_LG, weight="bold"),
                bg=card_bg, fg=T.PRIMARY,
            ).pack()

            if months > 1:
                per_month = price / months
                tk.Label(
                    card,
                    text=f"${per_month:.2f}/mo",
                    font=T.font(size=T.FONT_XS),
                    bg=card_bg, fg=T.SUCCESS_DARK,
                ).pack()

            tk.Button(
                card,
                text="Select →",
                font=T.font(size=T.FONT_XS, weight="bold"),
                bg=T.PRIMARY        if is_popular else T.BG_APP,
                fg=T.TEXT_ON_PRIMARY if is_popular else T.PRIMARY,
                relief="flat", cursor="hand2",
                padx=_px(T.PAD_MD), pady=_px(T.PAD_XS), bd=0,
                command=lambda lbl=label: self._select_plan_and_go(lbl),
            ).pack(pady=_px(T.PAD_SM))

    def _select_plan_and_go(self, label: str) -> None:
        self._plan_var.set(label)
        self._nb.select(2)

    # ══════════════════════════════════════════════════════════
    # TAB 3 — Pay & Activate
    # ══════════════════════════════════════════════════════════

    def _build_payment_tab(self) -> None:
        canvas    = tk.Canvas(self._tab_payment, bg=T.BG_APP, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self._tab_payment, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=T.BG_APP)

        scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.bind(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"),
        )

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        # Original sections
        self._build_plan_section(scroll_frame)
        self._build_wallet_section(scroll_frame)
        self._build_steps_section(scroll_frame)
        self._build_tx_section(scroll_frame)
        # New section for activation code
        self._build_code_section(scroll_frame)

    # ══════════════════════════════════════════════════════════
    # Original methods (plan, wallet, steps, tx)
    # ══════════════════════════════════════════════════════════

    def _build_plan_section(self, parent: tk.Widget) -> None:
        section = tk.Frame(
            parent, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        section.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(T.PAD_MD, T.PAD_SM))

        tk.Label(
            section,
            text="💎  Select Plan",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK,
            padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
            anchor="w",
        ).pack(fill="x")

        inner = tk.Frame(section, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_SM))

        for label, months, price, badge in _PLANS:
            row = tk.Frame(inner, bg=T.BG_PANEL)
            row.pack(fill="x", pady=2)

            tk.Radiobutton(
                row,
                text=label,
                variable=self._plan_var,
                value=label,
                font=T.font(size=T.FONT_SM),
                bg=T.BG_PANEL, fg=T.TEXT_PRIMARY,
                selectcolor=T.PRIMARY_GHOST,
                activebackground=T.BG_PANEL,
                cursor="hand2",
            ).pack(side="left")

            if badge:
                tk.Label(
                    row, text=badge,
                    font=T.font(size=T.FONT_XS, weight="bold"),
                    bg=T.PRIMARY_LIGHTER, fg=T.PRIMARY_DARK,
                    padx=_px(T.PAD_SM), pady=1,
                ).pack(side="left", padx=_px(T.PAD_SM))

        self._price_lbl = tk.Label(
            inner, text="",
            font=T.font(size=T.FONT_LG, weight="bold"),
            bg=T.BG_PANEL, fg=T.PRIMARY,
        )
        self._price_lbl.pack(pady=_pad(T.PAD_SM, 0))

    def _build_steps_section(self, parent: tk.Widget) -> None:
        card = tk.Frame(
            parent, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        card.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(0, T.PAD_SM))

        tk.Label(
            card,
            text="📋  How to Pay — 4 Steps",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK,
            padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
            anchor="w",
        ).pack(fill="x")

        inner = tk.Frame(card, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_MD))

        steps = [
            ("1", "Copy the wallet address below"),
            ("2", "Send exact USDT amount (TRC20 network only)"),
            ("3", "Copy the TX hash from your wallet or TronScan"),
            ("4", "Paste hash below and click Verify & Activate"),
        ]
        for num, text in steps:
            row = tk.Frame(inner, bg=T.BG_PANEL)
            row.pack(fill="x", pady=2)

            tk.Label(
                row, text=num,
                font=T.font(size=T.FONT_SM, weight="bold"),
                bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY,
                width=2,
            ).pack(side="left", padx=_pad(0, T.PAD_SM))

            tk.Label(
                row, text=text,
                font=T.font(size=T.FONT_XS),
                bg=T.BG_PANEL, fg=T.TEXT_SECONDARY,
            ).pack(side="left", anchor="w")

    def _build_wallet_section(self, parent: tk.Widget) -> None:
        section = tk.Frame(
            parent, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        section.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(0, T.PAD_SM))

        tk.Label(
            section,
            text="💳  Send USDT (TRC20)",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK,
            padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
            anchor="w",
        ).pack(fill="x")

        inner = tk.Frame(section, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_SM))

        qr_frame = tk.Frame(inner, bg=T.BG_PANEL)
        qr_frame.pack(side="left", padx=_pad(0, T.PAD_XL))
        self._build_qr(qr_frame)

        info = tk.Frame(inner, bg=T.BG_PANEL)
        info.pack(side="left", fill="x", expand=True)

        tk.Label(
            info,
            text="Send exactly the plan amount to:",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
        ).pack(anchor="w", pady=_pad(0, 2))

        display_address = WALLET_ADDRESS or "ADDRESS_NOT_CONFIGURED"

        addr_row = tk.Frame(info, bg=T.BG_PANEL)
        addr_row.pack(anchor="w", fill="x", pady=_pad(2, 5))

        addr_entry = tk.Entry(
            addr_row,
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.BG_APP, fg=T.PRIMARY,
            relief="flat", width=34,
        )
        addr_entry.insert(0, display_address)
        addr_entry.config(state="readonly")
        addr_entry.pack(side="left", padx=_pad(0, T.PAD_SM))

        def _copy_address() -> None:
            if not WALLET_ADDRESS:
                return
            try:
                win = self._win
                win.clipboard_clear()
                win.clipboard_append(display_address)
                win.update()
                copy_btn.config(text="✔ Copied!", fg=T.SUCCESS_DARK)
                win.after(2000, lambda: copy_btn.config(text="📋 Copy", fg=T.PRIMARY))
            except Exception as exc:
                logger.warning("Clipboard copy failed: %s", exc)

        copy_btn = tk.Button(
            addr_row,
            text="📋 Copy",
            font=T.font(size=T.FONT_XS, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY,
            activebackground=T.PRIMARY_LIGHTER,
            relief="flat",
            cursor="hand2" if WALLET_ADDRESS else "arrow",
            state="normal" if WALLET_ADDRESS else "disabled",
            command=_copy_address,
        )
        copy_btn.pack(side="left")

        tk.Label(
            info,
            text=(
                "⚠  TRC20 network only\n"
                "⚠  Send exact amount (±1 USDT tolerance)\n"
                "⚠  Transaction must be within 48 hours"
            ),
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.WARNING_DARK,
            justify="left",
        ).pack(anchor="w", pady=_pad(T.PAD_SM, 0))

    def _build_qr(self, parent: tk.Frame) -> None:
        if not _HAS_QR or not WALLET_ADDRESS:
            tk.Label(
                parent,
                text="QR\nUnavailable",
                font=T.font(size=T.FONT_XS),
                bg=T.PRIMARY_GHOST, fg=T.TEXT_MUTED,
                width=10, height=5,
            ).pack()
            return

        try:
            resample = getattr(getattr(Image, "Resampling", None), "LANCZOS", getattr(Image, "LANCZOS", 1))
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=4,
                border=2,
            )
            qr.add_data(WALLET_ADDRESS)
            qr.make(fit=True)
            img = qr.make_image(fill_color=T.PRIMARY_DARKER, back_color="white")
            img = img.resize((100, 100), resample)
            self._qr_photo = ImageTk.PhotoImage(img)

            tk.Label(parent, image=self._qr_photo, bg=T.BG_PANEL, relief="solid", bd=1).pack()
            tk.Label(parent, text="Scan to copy", font=T.font(size=T.FONT_XS),
                     bg=T.BG_PANEL, fg=T.TEXT_MUTED).pack(pady=_pad(2, 0))

        except Exception as exc:
            logger.warning("QR generation failed: %s", exc)
            tk.Label(parent, text="QR\nUnavailable", font=T.font(size=T.FONT_XS),
                     bg=T.PRIMARY_GHOST, fg=T.TEXT_MUTED, width=10, height=5).pack()

    def _build_tx_section(self, parent: tk.Widget) -> None:
        section = tk.Frame(parent, bg=T.BG_PANEL, highlightthickness=1, highlightbackground=T.BORDER)
        section.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(0, T.PAD_MD))

        tk.Label(section, text="📝  Paste Transaction Hash", font=T.font(size=T.FONT_SM, weight="bold"),
                 bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK, padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
                 anchor="w").pack(fill="x")

        inner = tk.Frame(section, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_SM))

        tk.Label(inner, text="After sending, paste the TronScan TX hash here and click Verify:",
                 font=T.font(size=T.FONT_XS), bg=T.BG_PANEL, fg=T.TEXT_MUTED,
                 wraplength=500, justify="left").pack(anchor="w", pady=_pad(0, T.PAD_SM))

        row = tk.Frame(inner, bg=T.BG_PANEL)
        row.pack(fill="x")

        self._tx_entry = ttk.Entry(row, width=44, font=T.font(size=T.FONT_SM, family=T.FONT_FAMILY_MONO))
        self._tx_entry.pack(side="left", fill="x", expand=True, padx=_pad(0, T.PAD_SM))
        ToolTip(self._tx_entry, "Paste your TronScan transaction hash here")

        tk.Button(row, text="📋 Paste", font=T.font(size=T.FONT_SM, weight="bold"),
                  bg=T.PRIMARY, fg=T.TEXT_ON_PRIMARY, activebackground=T.PRIMARY_DARK,
                  activeforeground=T.TEXT_ON_PRIMARY, relief="flat", cursor="hand2",
                  padx=_px(T.PAD_LG), pady=_px(T.PAD_SM), bd=0,
                  command=self._paste_tx).pack(side="left")

        self._tx_preview_lbl = tk.Label(inner, text="", font=T.font(size=T.FONT_XS, family=T.FONT_FAMILY_MONO),
                                        bg=T.BG_PANEL, fg=T.TEXT_MUTED, anchor="w")
        self._tx_preview_lbl.pack(fill="x", pady=_pad(T.PAD_XS, 0))

        self._tx_entry.bind("<KeyRelease>", self._on_tx_change)
        self._tx_entry.bind("<<Paste>>",    self._on_tx_change)

    def _paste_tx(self) -> None:
        text = safe_clipboard_paste(self._win)
        if text:
            self._tx_entry.delete(0, tk.END)
            self._tx_entry.insert(0, text.strip())
            self._on_tx_change()

    def _on_tx_change(self, _=None) -> None:
        tx = self._tx_entry.get().strip()
        if len(tx) > 16:
            preview = f"{tx[:12]}...{tx[-8:]}"
            length  = len(tx)
            self._tx_preview_lbl.config(
                text=f"Hash: {preview}  ({length} chars)",
                fg=T.SUCCESS_DARK if length >= 64 else T.WARNING_DARK,
            )
        else:
            self._tx_preview_lbl.config(text="")

    def _update_price(self) -> None:
        months, price = _PLAN_MAP.get(self._plan_var.get(), (0, 0.0))
        if months and hasattr(self, "_price_lbl"):
            self._price_lbl.config(text=f"💰  Total: {price:.1f} USDT  (TRC20)")

    def _build_footer_buttons(self) -> None:
        action = tk.Frame(self.footer, bg=T.BG_APP)
        action.pack(fill="x", expand=True, pady=_px(T.PAD_SM))

        self._verify_btn = tk.Button(action, text="✅  Verify & Activate",
                                     font=T.font(size=T.FONT_MD, weight="bold"),
                                     bg=T.SUCCESS, fg=T.TEXT_ON_PRIMARY,
                                     activebackground=T.SUCCESS_DARK,
                                     activeforeground=T.TEXT_ON_PRIMARY,
                                     relief="flat", cursor="hand2",
                                     padx=_px(T.PAD_2XL), pady=_px(T.PAD_MD), bd=0,
                                     command=self._verify_and_activate)
        self._verify_btn.pack(side="top", fill="x")

        tk.Label(action, text="🔒  Privacy-first: No email, no account, no KYC. Instant on-chain activation.",
                 font=T.font(size=T.FONT_XS), bg=T.BG_APP, fg=T.TEXT_MUTED).pack(pady=_pad(T.PAD_XS, 0))

        self._progress = ttk.Progressbar(action, mode="indeterminate", length=300)

    def _verify_and_activate(self) -> None:
        if self._verifying:
            return
        if not WALLET_ADDRESS:
            self.show_error("Configuration Error", "Wallet address is not configured.\nPayments are currently disabled.")
            return
        tx_hash = self._tx_entry.get().strip()
        if not tx_hash:
            self.show_warning("Missing TX Hash", "Please paste the transaction hash.")
            return
        if len(tx_hash) < 60:
            self.show_warning("Invalid TX Hash", f"Transaction hash is too short ({len(tx_hash)} chars).\nA valid TronScan hash has 64 characters.")
            return
        months, _ = _PLAN_MAP.get(self._plan_var.get(), (0, 0.0))
        if not months:
            self.show_error("Invalid Plan", "Please select a plan first.")
            return

        self._verifying = True
        self._verify_btn.config(state="disabled", text="⏳  Verifying on blockchain…")
        self._progress.pack(side="top", pady=_pad(T.PAD_SM, 0))
        self._progress.start(10)

        device_id = getattr(self.app, "device_id", None)
        threading.Thread(target=self._verify_worker, args=(tx_hash, months, device_id), daemon=True, name="premium-verify").start()

    def _verify_worker(self, tx_hash: str, months: int, device_id: Optional[str]) -> None:
        """
        FIX (v2.5):
            The pre-fix version called `verify_usdt_transaction(tx_hash,
            months, device_id)` without a `fetch_func`.  The core
            function immediately returned
                (False, "No network function provided.")
            for every attempt, so no USDT payment could ever activate a
            plan even after the blockchain confirmed it.

            The fix wires the `TronscanClient._request` method (already
            constructed as `self.tron_client` in __init__) into the
            verification call.  Its signature
                `_request(url, params=None, headers=None, method="GET", ...)`
            matches exactly what `verify_usdt_transaction` calls:
                `resp = fetch_func(api_url, {}, {})`
        """
        try:
            ok, error_msg = verify_usdt_transaction(
                tx_hash=tx_hash,
                months=months,
                fetch_func=self.tron_client._request,   # ← FIX v2.5
                device_id=device_id,
            )
            if not ok:
                self._safe_ui_call_from_thread(
                    lambda: self.show_error("Verification Failed",
                                            f"❌  {error_msg or 'Transaction could not be verified.'}\n\n"
                                            "Please check:\n"
                                            "• Hash is correct\n"
                                            "• You sent to the correct wallet\n"
                                            "• Amount matches the selected plan\n"
                                            "• Transaction is less than 48 hours old")
                )
                return
            try:
                status = load_user_status(device_id=device_id)
                expiry = status.get("plan_expiry", "—")
            except Exception:
                status, expiry = {}, "—"
            for method_name in ("update_plan_display", "apply_filter", "refresh_ui"):
                if hasattr(self.app, method_name):
                    self._safe_ui_call_from_thread(lambda m=method_name: _call_safe(self.app, m))
            if status:
                try:
                    self.app.user_status = status
                except Exception:
                    pass
            def _show_success() -> None:
                self.show_info("✅  Activated!",
                               f"🎉  {months}-month Premium plan is now active!\n\n"
                               f"Expires: {expiry}\n\n"
                               "You now have unlimited access to all features.\n"
                               "Welcome to the elite. Trade with precision.")
                self.close()
            self._safe_ui_call_from_thread(_show_success)
        except Exception as exc:
            logger.error("Premium activation error: %s", exc, exc_info=True)
            self._safe_ui_call_from_thread(
                lambda: self.show_error("Activation Error", f"An unexpected error occurred:\n{exc}\n\nCheck your internet connection and try again.")
            )
        finally:
            self._safe_ui_call_from_thread(self._reset_verify_ui)

    def _reset_verify_ui(self) -> None:
        self._verifying = False
        try:
            self._progress.stop()
            self._progress.pack_forget()
        except Exception:
            pass
        if self._dlg_exists():
            try:
                self._verify_btn.config(state="normal", text="✅  Verify & Activate")
            except Exception:
                pass

    # ══════════════════════════════════════════════════════════
    # Activation Code Section (v2.4)
    # ══════════════════════════════════════════════════════════

    def _build_code_section(self, parent: tk.Widget) -> None:
        section = tk.Frame(
            parent, bg=T.BG_PANEL,
            highlightthickness=1, highlightbackground=T.BORDER,
        )
        section.pack(fill="x", padx=_px(T.PAD_MD), pady=_pad(T.PAD_MD, T.PAD_SM))

        tk.Label(
            section,
            text="🔑  Or Activate with Code",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST, fg=T.PRIMARY_DARK,
            padx=_px(T.PAD_MD), pady=_px(T.PAD_SM),
            anchor="w",
        ).pack(fill="x")

        inner = tk.Frame(section, bg=T.BG_PANEL)
        inner.pack(fill="x", padx=_px(T.PAD_XL), pady=_px(T.PAD_SM))

        tk.Label(
            inner,
            text="Enter the activation code you received (admin or license):",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
        ).pack(anchor="w", pady=_pad(0, T.PAD_SM))

        row = tk.Frame(inner, bg=T.BG_PANEL)
        row.pack(fill="x")

        self._code_entry = ttk.Entry(row, width=44, font=T.font(size=T.FONT_SM, family=T.FONT_FAMILY_MONO))
        self._code_entry.pack(side="left", fill="x", expand=True, padx=_pad(0, T.PAD_SM))
        ToolTip(self._code_entry, "Paste your activation code here")

        self._code_btn = tk.Button(
            row, text="🔓 Activate",
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.SUCCESS, fg=T.TEXT_ON_PRIMARY,
            activebackground=T.SUCCESS_DARK,
            activeforeground=T.TEXT_ON_PRIMARY,
            relief="flat", cursor="hand2",
            padx=_px(T.PAD_LG), pady=_px(T.PAD_SM), bd=0,
            command=self._activate_with_code,
        )
        self._code_btn.pack(side="left")

        self._code_status = tk.Label(
            inner, text="",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL, fg=T.TEXT_MUTED,
            anchor="w",
        )
        self._code_status.pack(fill="x", pady=_pad(T.PAD_XS, 0))

        self._code_entry.bind("<KeyRelease>", self._on_code_change)

    def _on_code_change(self, _=None) -> None:
        code = self._code_entry.get().strip()
        if code:
            self._code_status.config(text=f"Code length: {len(code)} characters", fg=T.TEXT_SECONDARY)
        else:
            self._code_status.config(text="")

    def _activate_with_code(self) -> None:
        if self._verifying:
            return
        code = self._code_entry.get().strip()
        if not code:
            self.show_warning("Missing Code", "Please enter an activation code.")
            return

        self._verifying = True
        self._code_btn.config(state="disabled", text="⏳  Activating…")
        device_id = getattr(self.app, "device_id", None)
        threading.Thread(
            target=self._activate_code_worker,
            args=(code, device_id),
            daemon=True,
            name="activation-code-verify"
        ).start()

    def _activate_code_worker(self, code: str, device_id: Optional[str]) -> None:
        try:
            ok, message = verify_and_activate_code(code, device_id=device_id)
            if not ok:
                self._safe_ui_call_from_thread(
                    lambda: self.show_error("Activation Failed", f"❌  {message}")
                )
                return
            # Success
            try:
                status = load_user_status(device_id=device_id)
                expiry = status.get("plan_expiry", "—")
                plan = status.get("plan", "premium")
            except Exception:
                status, expiry, plan = {}, "—", "premium"

            for method_name in ("update_plan_display", "apply_filter", "refresh_ui"):
                if hasattr(self.app, method_name):
                    self._safe_ui_call_from_thread(lambda m=method_name: _call_safe(self.app, m))
            if status:
                try:
                    self.app.user_status = status
                except Exception:
                    pass

            def _show_success() -> None:
                if plan == "admin":
                    msg = (f"🎉  Admin plan activated successfully!\n\n"
                           f"Plan: ADMIN (unlimited)\n"
                           "You now have full access to all features forever.")
                else:
                    msg = (f"🎉  {plan} plan activated successfully!\n\n"
                           f"Expires: {expiry}\n"
                           "Welcome to the elite. Trade with precision.")
                self.show_info("✅  Activated!", msg)
                self.close()

            self._safe_ui_call_from_thread(_show_success)
        except Exception as exc:
            logger.error("Activation code error: %s", exc, exc_info=True)
            self._safe_ui_call_from_thread(
                lambda: self.show_error("Activation Error", f"An unexpected error occurred:\n{exc}")
            )
        finally:
            self._safe_ui_call_from_thread(self._reset_code_ui)

    def _reset_code_ui(self) -> None:
        self._verifying = False
        if self._dlg_exists():
            try:
                self._code_btn.config(state="normal", text="🔓 Activate")
            except Exception:
                pass