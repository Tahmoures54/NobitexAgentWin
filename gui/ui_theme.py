# source/gui/ui_theme.py
"""
CryptoScanner — تم بصری یکپارچه (فیروزه‌ای)

استفاده:
    from gui.ui_theme import Theme, Styles

    # رنگ مستقیم
    color = Theme.PRIMARY

    # اعمال ttk styles
    Styles.apply(ttk_style_obj)

Backward Compatibility:
    ModernTheme = Theme   (alias برای کدهای قدیمی)

Changes v2.1:
    - اضافه شدن GOLD / GOLD_DARK / GOLD_BG برای Premium و Badge
    - اضافه شدن TEXT_ON_DARK به عنوان alias صریح
    - اضافه شدن BG_CARD alias
    - اضافه شدن PRIMARY_CARD برای کارت‌های Premium
    - اضافه شدن HEADER_HEIGHT_SM برای dialog های کوچک
    - تمام ثابت‌های استفاده‌شده در premium_window پوشش داده شدند
"""
from __future__ import annotations

from typing import Optional


# ══════════════════════════════════════════════════════════════
# Color Palette — فیروزه‌ای
# ══════════════════════════════════════════════════════════════

class Theme:
    """
    تمام رنگ‌ها، فونت‌ها و اندازه‌های استاندارد برنامه.
    رنگ اصلی: فیروزه‌ای (Teal / Cyan)
    """

    # ── Primary ───────────────────────────────────────────────
    PRIMARY         = "#00A8A8"
    PRIMARY_DARK    = "#007A7A"
    PRIMARY_DARKER  = "#005F5F"
    PRIMARY_LIGHT   = "#00CED1"
    PRIMARY_LIGHTER = "#B2EBEB"
    PRIMARY_GHOST   = "#E0F7F7"

    # ── Gold (Premium / Badge / Highlight) ───────────────────
    GOLD            = "#D4AF37"
    GOLD_DARK       = "#B5952F"
    GOLD_BG         = "#FFF8E1"
    GOLD_LIGHT      = "#FFF3C4"

    # ── Background ────────────────────────────────────────────
    BG_APP          = "#F0F8F8"
    BG_PANEL        = "#FFFFFF"
    BG_CARD         = "#FFFFFF"   # alias for BG_PANEL
    BG_SIDEBAR      = "#004F4F"
    BG_HEADER       = "#006868"
    BG_ROW_ALT      = "#F4FAFA"
    BG_ROW_HOVER    = "#E0F4F4"
    BG_INPUT        = "#FFFFFF"
    BG_DISABLED     = "#F0F0F0"

    # ── Backward-compatible aliases ───────────────────────────
    BG_LIGHT        = "#F0F8F8"   # = BG_APP
    BG_WHITE        = "#FFFFFF"   # = BG_PANEL
    BG_DARK         = "#004F4F"   # = BG_SIDEBAR

    # ── Text ──────────────────────────────────────────────────
    TEXT_PRIMARY    = "#1A2E35"
    TEXT_SECONDARY  = "#4A6670"
    TEXT_MUTED      = "#8AABAF"
    TEXT_ON_PRIMARY = "#FFFFFF"
    TEXT_ON_DARK    = "#E8F5F5"   # برای متن روی پس‌زمینه تیره
    TEXT_LINK       = "#00A8A8"
    TEXT_DISABLED   = "#BEBEBE"

    # ── Backward-compatible aliases ───────────────────────────
    TEXT_DARK       = "#1A2E35"   # = TEXT_PRIMARY
    TEXT_LIGHT      = "#FFFFFF"   # = TEXT_ON_PRIMARY
    TEXT_GRAY       = "#4A6670"   # = TEXT_SECONDARY

    # ── Border ────────────────────────────────────────────────
    BORDER          = "#C8E0E0"
    BORDER_STRONG   = "#00A8A8"
    BORDER_FOCUS    = "#007A7A"

    # ── Backward-compatible alias ─────────────────────────────
    BORDER_LIGHT    = "#C8E0E0"   # = BORDER

    # ── Status — Success ──────────────────────────────────────
    SUCCESS         = "#10B981"
    SUCCESS_BG      = "#D1FAE5"
    SUCCESS_DARK    = "#059669"
    SUCCESS_LIGHT   = "#D1FAE5"   # = SUCCESS_BG

    # ── Status — Warning ──────────────────────────────────────
    WARNING         = "#F59E0B"
    WARNING_BG      = "#FEF3C7"
    WARNING_DARK    = "#D97706"
    WARNING_LIGHT   = "#FEF3C7"   # = WARNING_BG

    # ── Status — Danger ───────────────────────────────────────
    DANGER          = "#EF4444"
    DANGER_BG       = "#FEE2E2"
    DANGER_DARK     = "#DC2626"
    DANGER_LIGHT    = "#FEE2E2"   # = DANGER_BG

    # ── Status — Info ─────────────────────────────────────────
    INFO            = "#0EA5E9"
    INFO_BG         = "#E0F2FE"
    INFO_DARK       = "#0284C7"
    INFO_LIGHT      = "#E0F2FE"   # = INFO_BG

    # ── Signal Colors ─────────────────────────────────────────
    STRONG_BUY      = "#059669"
    STRONG_BUY_BG   = "#D1FAE5"

    BUY             = "#34D399"
    BUY_BG          = "#ECFDF5"

    NEUTRAL_SIG     = "#6B7280"
    NEUTRAL_SIG_BG  = "#F3F4F6"

    SELL            = "#FBBF24"
    SELL_BG         = "#FFFBEB"

    STRONG_SELL     = "#DC2626"
    STRONG_SELL_BG  = "#FEE2E2"

    # ── Backward-compatible aliases ───────────────────────────
    NEUTRAL         = "#F3F4F6"   # = NEUTRAL_SIG_BG
    PRIMARY_HOVER   = "#007A7A"   # = PRIMARY_DARK

    # ── Shadow ────────────────────────────────────────────────
    SHADOW          = "#D0E8E8"

    # ══════════════════════════════════════════════════════════
    # Typography
    # ══════════════════════════════════════════════════════════

    FONT_FAMILY      = "Segoe UI"
    FONT_FAMILY_MONO = "Consolas"
    FONT_FAMILY_FB   = "Arial"

    FONT_XS   = 8
    FONT_SM   = 9
    FONT_BASE = 10
    FONT_MD   = 11
    FONT_LG   = 13
    FONT_XL   = 16
    FONT_2XL  = 20
    FONT_3XL  = 26

    # ══════════════════════════════════════════════════════════
    # Spacing & Sizing
    # ══════════════════════════════════════════════════════════

    PAD_XS  = 2
    PAD_SM  = 4
    PAD_MD  = 8
    PAD_LG  = 12
    PAD_XL  = 16
    PAD_2XL = 24

    RADIUS_SM = 4
    RADIUS_MD = 8
    RADIUS_LG = 12
    RADIUS_XL = 16

    BTN_HEIGHT      = 32
    BTN_HEIGHT_LG   = 40
    INPUT_HEIGHT    = 32
    ROW_HEIGHT      = 28
    HEADER_HEIGHT   = 52
    HEADER_HEIGHT_SM = 40   # برای dialog های کوچک
    SIDEBAR_WIDTH   = 200

    # ══════════════════════════════════════════════════════════
    # Helper Methods
    # ══════════════════════════════════════════════════════════

    @classmethod
    def font(
        cls,
        size:   Optional[int] = None,
        weight: str           = "normal",
        family: Optional[str] = None,
    ) -> tuple:
        """
        tuple فونت برای tkinter برمی‌گرداند.

        Examples
        --------
        >>> Theme.font()
        ('Segoe UI', 10, 'normal')
        >>> Theme.font(size=Theme.FONT_LG, weight='bold')
        ('Segoe UI', 13, 'bold')
        """
        return (
            family or cls.FONT_FAMILY,
            size   or cls.FONT_BASE,
            weight,
        )

    @classmethod
    def signal_color(cls, signal: str) -> tuple:
        """
        رنگ foreground و background یک سیگنال را برمی‌گرداند.

        Returns
        -------
        (fg_color, bg_color)

        Examples
        --------
        >>> Theme.signal_color("Strong Buy")
        ('#059669', '#D1FAE5')
        """
        mapping = {
            "Strong Buy":  (cls.STRONG_BUY,  cls.STRONG_BUY_BG),
            "Buy Signal":  (cls.BUY,          cls.BUY_BG),
            "Neutral":     (cls.NEUTRAL_SIG,  cls.NEUTRAL_SIG_BG),
            "Sell Signal": (cls.SELL,         cls.SELL_BG),
            "Strong Sell": (cls.STRONG_SELL,  cls.STRONG_SELL_BG),
        }
        return mapping.get(signal, (cls.TEXT_SECONDARY, cls.BG_ROW_ALT))

    @classmethod
    def change_color(cls, pct: float) -> str:
        """
        رنگ مناسب برای نمایش درصد تغییر قیمت.

        Examples
        --------
        >>> Theme.change_color(5.2)   # SUCCESS_DARK
        >>> Theme.change_color(-3.1)  # DANGER_DARK
        >>> Theme.change_color(0.0)   # NEUTRAL_SIG
        """
        if pct > 0:
            return cls.SUCCESS_DARK
        if pct < 0:
            return cls.DANGER_DARK
        return cls.NEUTRAL_SIG

    @classmethod
    def risk_color(cls, level: str) -> tuple:
        """
        رنگ foreground و background سطح ریسک.

        Returns
        -------
        (fg_color, bg_color)

        Examples
        --------
        >>> Theme.risk_color("Low")
        ('#059669', '#D1FAE5')
        """
        mapping = {
            "Low":    (cls.SUCCESS_DARK, cls.SUCCESS_BG),
            "Medium": (cls.WARNING_DARK, cls.WARNING_BG),
            "High":   (cls.DANGER_DARK,  cls.DANGER_BG),
        }
        return mapping.get(level, (cls.TEXT_SECONDARY, cls.BG_ROW_ALT))

    @classmethod
    def plan_color(cls, plan: str) -> tuple:
        """
        رنگ foreground و background پلن کاربر.

        Returns
        -------
        (fg_color, bg_color)

        Examples
        --------
        >>> Theme.plan_color("3months")
        ('#B5952F', '#FFF8E1')
        """
        plan = (plan or "").strip().lower()
        if plan == "free":
            return cls.TEXT_SECONDARY, cls.BG_ROW_ALT
        if "12" in plan:
            return cls.PRIMARY_DARK, cls.PRIMARY_GHOST
        # هر پلن premium دیگری
        return cls.GOLD_DARK, cls.GOLD_BG

    @classmethod
    def badge(
        cls,
        text: str,
        style: str = "primary",
    ) -> tuple:
        """
        رنگ متن و پس‌زمینه برای badge ها.

        Parameters
        ----------
        style:
            'primary' | 'gold' | 'success' | 'danger' | 'warning' | 'info'

        Returns
        -------
        (fg_color, bg_color)
        """
        mapping = {
            "primary": (cls.TEXT_ON_PRIMARY, cls.PRIMARY),
            "gold":    ("#FFFFFF",           cls.GOLD),
            "success": (cls.TEXT_ON_PRIMARY, cls.SUCCESS),
            "danger":  (cls.TEXT_ON_PRIMARY, cls.DANGER),
            "warning": (cls.TEXT_PRIMARY,    cls.WARNING),
            "info":    (cls.TEXT_ON_PRIMARY, cls.INFO),
        }
        return mapping.get(style, (cls.TEXT_ON_PRIMARY, cls.PRIMARY))


# ══════════════════════════════════════════════════════════════
# Backward-Compatible Alias
# ══════════════════════════════════════════════════════════════

#: alias برای کدهایی که هنوز از ModernTheme استفاده می‌کنند
ModernTheme = Theme


# ══════════════════════════════════════════════════════════════
# TTK Style Builder
# ══════════════════════════════════════════════════════════════

class Styles:
    """
    اعمال ttk.Style برای کل برنامه.

    Examples
    --------
    >>> import tkinter as tk
    >>> from tkinter import ttk
    >>> root = tk.Tk()
    >>> style = ttk.Style(root)
    >>> Styles.apply(style)
    """

    @staticmethod
    def apply(style) -> None:
        """تمام ttk style های برنامه را یکجا اعمال می‌کند."""
        T = Theme

        try:
            style.theme_use("clam")
        except Exception:
            pass

        # ── TFrame ───────────────────────────────────────────
        style.configure("TFrame",         background=T.BG_APP)
        style.configure("Card.TFrame",    background=T.BG_PANEL,  relief="flat")
        style.configure("Sidebar.TFrame", background=T.BG_SIDEBAR)
        style.configure("Header.TFrame",  background=T.BG_HEADER)
        style.configure("Gold.TFrame",    background=T.GOLD_BG)

        # ── TLabel ───────────────────────────────────────────
        style.configure(
            "TLabel",
            background=T.BG_APP,
            foreground=T.TEXT_PRIMARY,
            font=T.font(),
        )
        style.configure(
            "Title.TLabel",
            background=T.BG_APP,
            foreground=T.PRIMARY,
            font=T.font(size=T.FONT_2XL, weight="bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=T.BG_APP,
            foreground=T.TEXT_SECONDARY,
            font=T.font(size=T.FONT_MD),
        )
        style.configure(
            "Header.TLabel",
            background=T.BG_HEADER,
            foreground=T.TEXT_ON_DARK,
            font=T.font(size=T.FONT_LG, weight="bold"),
        )
        style.configure(
            "Sidebar.TLabel",
            background=T.BG_SIDEBAR,
            foreground=T.TEXT_ON_DARK,
            font=T.font(size=T.FONT_BASE),
        )
        style.configure(
            "Muted.TLabel",
            background=T.BG_APP,
            foreground=T.TEXT_MUTED,
            font=T.font(size=T.FONT_SM),
        )
        style.configure(
            "Card.TLabel",
            background=T.BG_PANEL,
            foreground=T.TEXT_PRIMARY,
            font=T.font(),
        )
        style.configure(
            "Gold.TLabel",
            background=T.GOLD_BG,
            foreground=T.GOLD_DARK,
            font=T.font(weight="bold"),
        )
        style.configure(
            "Premium.TLabel",
            background=T.PRIMARY_GHOST,
            foreground=T.PRIMARY_DARK,
            font=T.font(size=T.FONT_SM, weight="bold"),
        )

        # ── TButton ──────────────────────────────────────────
        style.configure(
            "TButton",
            background=T.PRIMARY,
            foreground=T.TEXT_ON_PRIMARY,
            font=T.font(weight="bold"),
            borderwidth=0,
            focusthickness=0,
            padding=(T.PAD_LG, T.PAD_SM),
        )
        style.map(
            "TButton",
            background=[
                ("active",   T.PRIMARY_DARK),
                ("pressed",  T.PRIMARY_DARKER),
                ("disabled", T.BG_DISABLED),
            ],
            foreground=[
                ("disabled", T.TEXT_DISABLED),
            ],
        )

        style.configure(
            "Secondary.TButton",
            background=T.BG_PANEL,
            foreground=T.PRIMARY,
            font=T.font(),
            borderwidth=1,
            relief="solid",
            padding=(T.PAD_LG, T.PAD_SM),
        )
        style.map(
            "Secondary.TButton",
            background=[
                ("active",  T.PRIMARY_GHOST),
                ("pressed", T.PRIMARY_LIGHTER),
            ],
        )

        style.configure(
            "Danger.TButton",
            background=T.DANGER,
            foreground=T.TEXT_ON_PRIMARY,
            font=T.font(weight="bold"),
            borderwidth=0,
            padding=(T.PAD_LG, T.PAD_SM),
        )
        style.map(
            "Danger.TButton",
            background=[
                ("active",  T.DANGER_DARK),
                ("pressed", T.DANGER_DARK),
            ],
        )

        style.configure(
            "Success.TButton",
            background=T.SUCCESS,
            foreground=T.TEXT_ON_PRIMARY,
            font=T.font(size=T.FONT_SM, weight="bold"),
            borderwidth=0,
            relief="flat",
            padding=(T.PAD_LG, T.PAD_SM),
        )
        style.map(
            "Success.TButton",
            background=[
                ("active",  T.SUCCESS_DARK),
                ("pressed", "#047857"),
            ],
        )

        style.configure(
            "Gold.TButton",
            background=T.GOLD,
            foreground="#FFFFFF",
            font=T.font(size=T.FONT_SM, weight="bold"),
            borderwidth=0,
            relief="flat",
            padding=(T.PAD_LG, T.PAD_SM),
        )
        style.map(
            "Gold.TButton",
            background=[
                ("active",  T.GOLD_DARK),
                ("pressed", T.GOLD_DARK),
            ],
        )

        # ── Premium.TButton ──────────────────────────────────
        style.configure(
            "Premium.TButton",
            background=T.PRIMARY,
            foreground=T.TEXT_ON_PRIMARY,
            borderwidth=0,
            relief="flat",
            font=T.font(size=T.FONT_SM, weight="bold"),
            padding=(T.PAD_XL, T.PAD_MD),
        )
        style.map(
            "Premium.TButton",
            background=[
                ("active",   T.PRIMARY_DARK),
                ("pressed",  T.PRIMARY_DARKER),
                ("disabled", T.TEXT_GRAY),
            ],
        )

        # ── TEntry ───────────────────────────────────────────
        style.configure(
            "TEntry",
            fieldbackground=T.BG_INPUT,
            foreground=T.TEXT_PRIMARY,
            bordercolor=T.BORDER,
            insertcolor=T.PRIMARY,
            font=T.font(),
            padding=T.PAD_SM,
        )
        style.map(
            "TEntry",
            bordercolor=[
                ("focus",    T.BORDER_FOCUS),
                ("hover",    T.BORDER_STRONG),
                ("disabled", T.BORDER),
            ],
            fieldbackground=[
                ("disabled", T.BG_DISABLED),
            ],
        )

        # ── TCombobox ────────────────────────────────────────
        style.configure(
            "TCombobox",
            fieldbackground=T.BG_INPUT,
            foreground=T.TEXT_PRIMARY,
            background=T.BG_INPUT,
            bordercolor=T.BORDER,
            arrowcolor=T.PRIMARY,
            font=T.font(),
            padding=T.PAD_SM,
        )
        style.map(
            "TCombobox",
            bordercolor=[("focus", T.BORDER_FOCUS)],
            fieldbackground=[("readonly", T.BG_INPUT)],
        )

        # ── Treeview ─────────────────────────────────────────
        style.configure(
            "Treeview",
            background=T.BG_PANEL,
            foreground=T.TEXT_PRIMARY,
            rowheight=T.ROW_HEIGHT,
            fieldbackground=T.BG_PANEL,
            bordercolor=T.BORDER,
            font=T.font(size=T.FONT_SM),
        )
        style.configure(
            "Treeview.Heading",
            background=T.PRIMARY,
            foreground=T.TEXT_ON_PRIMARY,
            font=T.font(size=T.FONT_SM, weight="bold"),
            relief="flat",
            padding=(T.PAD_SM, T.PAD_SM),
        )
        style.map(
            "Treeview",
            background=[("selected", T.PRIMARY_LIGHTER)],
            foreground=[("selected", T.PRIMARY_DARKER)],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", T.PRIMARY_DARK)],
        )

        # ── TScrollbar ───────────────────────────────────────
        style.configure(
            "TScrollbar",
            background=T.BG_APP,
            troughcolor=T.BG_APP,
            arrowcolor=T.PRIMARY,
            bordercolor=T.BG_APP,
        )
        style.map(
            "TScrollbar",
            background=[
                ("active",  T.PRIMARY),
                ("pressed", T.PRIMARY_DARK),
            ],
        )

        # ── TNotebook ────────────────────────────────────────
        style.configure(
            "TNotebook",
            background=T.BG_APP,
            bordercolor=T.BORDER,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "TNotebook.Tab",
            background=T.BG_APP,
            foreground=T.TEXT_SECONDARY,
            font=T.font(size=T.FONT_SM),
            padding=(T.PAD_XL, T.PAD_SM),
        )
        style.map(
            "TNotebook.Tab",
            background=[
                ("selected", T.BG_PANEL),
                ("active",   T.PRIMARY_GHOST),
            ],
            foreground=[
                ("selected", T.PRIMARY),
            ],
        )

        # ── TProgressbar ─────────────────────────────────────
        style.configure(
            "TProgressbar",
            background=T.PRIMARY,
            troughcolor=T.PRIMARY_GHOST,
            bordercolor=T.BORDER,
            lightcolor=T.PRIMARY_LIGHT,
            darkcolor=T.PRIMARY_DARK,
        )

        style.configure(
            "Gold.TProgressbar",
            background=T.GOLD,
            troughcolor=T.GOLD_LIGHT,
            bordercolor=T.BORDER,
        )

        # ── TCheckbutton ─────────────────────────────────────
        style.configure(
            "TCheckbutton",
            background=T.BG_APP,
            foreground=T.TEXT_PRIMARY,
            font=T.font(),
            indicatorcolor=T.PRIMARY,
        )
        style.map(
            "TCheckbutton",
            indicatorcolor=[
                ("selected",  T.PRIMARY),
                ("!selected", T.BG_INPUT),
            ],
            background=[
                ("active", T.PRIMARY_GHOST),
            ],
        )

        # ── TRadiobutton ─────────────────────────────────────
        style.configure(
            "TRadiobutton",
            background=T.BG_APP,
            foreground=T.TEXT_PRIMARY,
            font=T.font(),
        )
        style.map(
            "TRadiobutton",
            background=[("active", T.PRIMARY_GHOST)],
        )

        # ── TSeparator ───────────────────────────────────────
        style.configure("TSeparator", background=T.BORDER)

        # ── TLabelframe ──────────────────────────────────────
        style.configure(
            "TLabelframe",
            background=T.BG_PANEL,
            bordercolor=T.BORDER,
            relief="solid",
        )
        style.configure(
            "TLabelframe.Label",
            background=T.BG_PANEL,
            foreground=T.PRIMARY,
            font=T.font(weight="bold"),
        )

        style.configure(
            "Gold.TLabelframe",
            background=T.GOLD_BG,
            bordercolor=T.GOLD,
            relief="solid",
        )
        style.configure(
            "Gold.TLabelframe.Label",
            background=T.GOLD_BG,
            foreground=T.GOLD_DARK,
            font=T.font(weight="bold"),
        )

        # ── TSpinbox ─────────────────────────────────────────
        style.configure(
            "TSpinbox",
            fieldbackground=T.BG_INPUT,
            foreground=T.TEXT_PRIMARY,
            bordercolor=T.BORDER,
            arrowcolor=T.PRIMARY,
            font=T.font(),
        )

        # ── Scale ─────────────────────────────────────────────
        style.configure(
            "TScale",
            background=T.BG_APP,
            troughcolor=T.PRIMARY_LIGHTER,
            sliderlength=20,
        )
        style.map(
            "TScale",
            background=[("active", T.PRIMARY_GHOST)],
        )