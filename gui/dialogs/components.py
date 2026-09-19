# source/gui/dialogs/components.py
"""
GUI Components — اجزای قابل استفاده مجدد.

Components:
    - safe_clipboard_copy / paste : کلیپ‌بورد امن
    - style_button                : استایل دکمه با Theme
    - make_tree                   : Treeview با scrollbar
    - AutocompleteCombobox        : Combobox با autocomplete
    - CopyableLabel               : Label با دکمه copy
    - LabeledValue                : Label + مقدار رنگی
    - SectionFrame                : قاب با عنوان Theme
    - ProgressCard                : کارت progress با درصد
"""
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from gui.gui_helpers import ToolTip
from gui.ui_theme import Theme, Styles

logger = logging.getLogger(__name__)
T = Theme

# ── Optional pyperclip ───────────────────────────────────────
try:
    import pyperclip
    _HAS_PYPERCLIP = True
except ImportError:
    _HAS_PYPERCLIP = False


# ══════════════════════════════════════════════════════════════
# Clipboard
# ══════════════════════════════════════════════════════════════

def safe_clipboard_copy(
    text:   str,
    parent: Optional[tk.Widget] = None,
) -> bool:
    """
    متن را در کلیپ‌بورد کپی می‌کند.
    اول pyperclip امتحان می‌کند، بعد tkinter.

    Returns
    -------
    True اگر موفق بود.
    """
    if _HAS_PYPERCLIP:
        try:
            pyperclip.copy(text)
            return True
        except Exception as exc:
            logger.warning("pyperclip copy failed: %s", exc)

    # fallback: tkinter clipboard
    try:
        root = _get_root(parent)
        if root:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()   # اطمینان از flush
            return True
    except Exception as exc:
        logger.error("tk clipboard copy failed: %s", exc)
    return False


def safe_clipboard_paste(
    parent: Optional[tk.Widget] = None,
) -> str:
    """
    متن را از کلیپ‌بورد می‌خواند.

    Returns
    -------
    متن کلیپ‌بورد یا '' در صورت خطا.
    """
    if _HAS_PYPERCLIP:
        try:
            return pyperclip.paste() or ""
        except Exception as exc:
            logger.warning("pyperclip paste failed: %s", exc)

    try:
        root = _get_root(parent)
        if root:
            return root.clipboard_get()
    except Exception:
        pass
    return ""


def _get_root(widget: Optional[tk.Widget]) -> Optional[tk.Misc]:
    """root window را پیدا می‌کند."""
    if widget:
        try:
            return widget.winfo_toplevel()
        except Exception:
            pass
    # آخرین راه: iterate از winfo_children پایه
    try:
        import tkinter as _tk
        roots = [w for w in _tk.Misc._default_root.winfo_children()
                 if isinstance(w, _tk.Tk)] if hasattr(_tk.Misc, '_default_root') else []
        if roots:
            return roots[0]
        # Python 3.8+
        return _tk._get_temp_root()
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════
# Button Styling
# ══════════════════════════════════════════════════════════════

# رنگ‌ها از Theme — بدون hardcode
_BTN_VARIANTS: Dict[str, Tuple[str, str, str]] = {
    # variant: (bg, fg, hover_bg)
    "primary": (T.PRIMARY,      T.TEXT_ON_PRIMARY, T.PRIMARY_DARK),
    "success": (T.SUCCESS,      T.TEXT_ON_PRIMARY, T.SUCCESS_DARK),
    "danger":  (T.DANGER,       T.TEXT_ON_PRIMARY, T.DANGER_DARK),
    "warning": (T.WARNING,      T.TEXT_PRIMARY,    T.WARNING_DARK),
    "info":    (T.INFO,         T.TEXT_ON_PRIMARY, T.INFO_DARK),
    "ghost":   (T.BG_APP,       T.PRIMARY,         T.PRIMARY_GHOST),
    "neutral": (T.BG_DISABLED,  T.TEXT_SECONDARY,  T.BORDER),
}


def style_button(
    btn:       tk.Button,
    variant:   str = "primary",
    padx:      int = T.PAD_LG,
    pady:      int = T.PAD_SM,
    font_size: int = T.FONT_SM,
    bold:      bool = True,
) -> None:
    """
    استایل Theme را به یک tk.Button اعمال می‌کند.

    Parameters
    ----------
    btn:
        دکمه‌ای که باید استایل بگیرد.
    variant:
        'primary', 'success', 'danger', 'warning', 'info', 'ghost', 'neutral'
    """
    bg, fg, hover = _BTN_VARIANTS.get(variant, _BTN_VARIANTS["primary"])
    weight = "bold" if bold else "normal"

    btn.configure(
        bg=bg,
        fg=fg,
        font=T.font(size=font_size, weight=weight),
        relief="flat",
        cursor="hand2",
        padx=padx,
        pady=pady,
        bd=0,
        activebackground=hover,
        activeforeground=fg,
    )

    # hover با رنگ صحیح
    btn.bind("<Enter>", lambda _: btn.config(bg=hover))
    btn.bind("<Leave>", lambda _: btn.config(bg=bg))


# ══════════════════════════════════════════════════════════════
# Treeview Factory
# ══════════════════════════════════════════════════════════════

def make_tree(
    parent:  tk.Widget,
    columns: Tuple[str, ...],
    widths:  List[int],
    height:  int            = 14,
    anchors: Optional[Dict[str, str]] = None,
) -> ttk.Treeview:
    """
    Treeview با scrollbar عمودی و افقی و هدر فیروزه‌ای.

    Parameters
    ----------
    parent:
        widget والد.
    columns:
        نام ستون‌ها.
    widths:
        عرض هر ستون (به pixel).
    height:
        تعداد ردیف‌های نمایشی.
    anchors:
        dict اختیاری برای تراز هر ستون.
        مثلاً: {"Price": "e", "Symbol": "w"}
        پیش‌فرض: ستون‌های عددی → "center"، بقیه → "w"

    Returns
    -------
    ttk.Treeview

    Raises
    ------
    ValueError:
        اگر تعداد columns و widths برابر نباشد.
    """
    if len(columns) != len(widths):
        raise ValueError(
            f"columns ({len(columns)}) and widths ({len(widths)}) must have equal length."
        )

    frame = tk.Frame(parent, bg=T.BG_PANEL)
    frame.pack(fill="both", expand=True)

    vsb = ttk.Scrollbar(frame, orient="vertical")
    hsb = ttk.Scrollbar(frame, orient="horizontal")
    vsb.pack(side="right",  fill="y")
    hsb.pack(side="bottom", fill="x")

    tree = ttk.Treeview(
        frame,
        columns=columns,
        show="headings",
        height=height,
        style="Treeview",
        yscrollcommand=vsb.set,
        xscrollcommand=hsb.set,
    )
    vsb.config(command=tree.yview)
    hsb.config(command=tree.xview)

    _TEXT_COLS = {"Name", "Symbol", "Time", "Metric", "Description", "Note"}
    _anchors   = anchors or {}

    for col, w in zip(columns, widths):
        anchor = _anchors.get(col, "w" if col in _TEXT_COLS else "center")
        tree.heading(col, text=col, anchor=anchor)
        tree.column(col, width=max(w, 30), anchor=anchor,
                    minwidth=30, stretch=tk.NO)

    # ردیف‌های یک‌درمیان
    tree.tag_configure("alt",     background=T.BG_ROW_ALT)
    tree.tag_configure("profit",  background=T.SUCCESS_BG, foreground=T.SUCCESS_DARK)
    tree.tag_configure("loss",    background=T.DANGER_BG,  foreground=T.DANGER_DARK)
    tree.tag_configure("neutral", background=T.BG_PANEL,   foreground=T.TEXT_SECONDARY)

    tree.pack(fill="both", expand=True)
    return tree


# ══════════════════════════════════════════════════════════════
# AutocompleteCombobox
# ══════════════════════════════════════════════════════════════

class AutocompleteCombobox(ttk.Combobox):
    """
    Combobox با autocomplete.

    متن کاربر حفظ می‌شود و لیست dropdown فیلتر می‌شود.
    بر خلاف نسخه قبلی، متن کاربر جایگزین نمی‌شود.

    Examples
    --------
    >>> cb = AutocompleteCombobox(parent, width=20)
    >>> cb.set_completion_list(["BTC", "ETH", "LTC"])
    >>> cb.pack()
    """

    def set_completion_list(self, items: List[str]) -> None:
        """
        لیست autocomplete را تنظیم می‌کند.

        Parameters
        ----------
        items:
            لیست گزینه‌ها.
        """
        self._all_items: List[str] = sorted(str(i) for i in items if i)
        self["values"] = self._all_items
        self.bind("<KeyRelease>", self._on_key_release)
        self.bind("<FocusIn>",    self._on_focus_in)
        self.bind("<FocusOut>",   self._on_focus_out)

    def _on_focus_in(self, _=None) -> None:
        """هنگام focus، همه گزینه‌ها را نشان می‌دهد."""
        self._update_list(self.get())

    def _on_focus_out(self, _=None) -> None:
        """هنگام از دست دادن focus، dropdown را می‌بندد."""
        pass

    def _on_key_release(self, event) -> None:
        """لیست را بر اساس متن تایپ‌شده فیلتر می‌کند."""
        if event.keysym in (
            "Up", "Down", "Return", "Escape",
            "Tab", "Left", "Right",
        ):
            return

        typed = self.get()
        self._update_list(typed)

        # dropdown را باز می‌کند اگر بسته باشد
        try:
            self.event_generate("<Button-1>")
        except Exception:
            pass

    def _update_list(self, typed: str) -> None:
        """
        لیست dropdown را با توجه به متن تایپ‌شده فیلتر می‌کند.
        """
        items = getattr(self, "_all_items", [])
        if not typed:
            self["values"] = items
            return

        q = typed.lower()
        # ابتدا: شروع با typed
        starts = [i for i in items if i.lower().startswith(q)]
        # بعد: شامل typed (ولی در میان)
        contains = [i for i in items
                    if q in i.lower() and not i.lower().startswith(q)]
        filtered = starts + contains
        self["values"] = filtered if filtered else items


# ══════════════════════════════════════════════════════════════
# CopyableLabel
# ══════════════════════════════════════════════════════════════

class CopyableLabel(tk.Frame):
    """
    Label با دکمه copy کنار آن.

    Examples
    --------
    >>> lbl = CopyableLabel(parent, text="TXabc123...")
    >>> lbl.pack()
    >>> lbl.set_text("TX456def...")  # آپدیت متن
    """

    def __init__(
        self,
        parent:   tk.Widget,
        text:     str        = "",
        label:    str        = "",
        fg:       str        = T.TEXT_PRIMARY,
        font:     tuple      = None,
        truncate: int        = 0,
        **kwargs,
    ):
        """
        Parameters
        ----------
        text:
            متن نمایشی (و متن کپی‌شونده).
        label:
            عنوان اختیاری قبل از متن.
        truncate:
            اگر > 0 باشد، متن از وسط کوتاه می‌شود.
        """
        super().__init__(parent, bg=T.BG_PANEL, **kwargs)
        self._full_text = text
        self._truncate  = truncate

        if label:
            tk.Label(
                self,
                text=label,
                font=T.font(size=T.FONT_XS),
                bg=T.BG_PANEL,
                fg=T.TEXT_MUTED,
            ).pack(side="left", padx=(0, T.PAD_XS))

        self._text_lbl = tk.Label(
            self,
            text=self._display_text(text),
            font=font or T.font(size=T.FONT_SM, family=T.FONT_FAMILY_MONO),
            bg=T.BG_PANEL,
            fg=fg,
        )
        self._text_lbl.pack(side="left")

        self._copy_btn = tk.Button(
            self,
            text="📋",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL,
            fg=T.PRIMARY,
            activebackground=T.PRIMARY_GHOST,
            activeforeground=T.PRIMARY_DARK,
            relief="flat",
            cursor="hand2",
            bd=0,
            padx=T.PAD_XS,
            command=self._copy,
        )
        self._copy_btn.pack(side="left", padx=T.PAD_XS)
        ToolTip(self._copy_btn, "Copy to clipboard")

    def _display_text(self, text: str) -> str:
        """متن نمایشی (احتمالاً truncate‌شده)."""
        if self._truncate > 0 and len(text) > self._truncate:
            half = self._truncate // 2
            return f"{text[:half]}…{text[-half:]}"
        return text

    def set_text(self, text: str) -> None:
        """متن را به‌روز می‌کند."""
        self._full_text = text
        self._text_lbl.config(text=self._display_text(text))

    def _copy(self) -> None:
        if safe_clipboard_copy(self._full_text, self):
            original_text = self._copy_btn.cget("text")
            self._copy_btn.config(text="✅", fg=T.SUCCESS_DARK)
            self.after(1200, lambda: self._copy_btn.config(
                text=original_text, fg=T.PRIMARY
            ))


# ══════════════════════════════════════════════════════════════
# LabeledValue
# ══════════════════════════════════════════════════════════════

class LabeledValue(tk.Frame):
    """
    یک Label + مقدار رنگی — مناسب برای نمایش آمار.

    Examples
    --------
    >>> lv = LabeledValue(parent, label="Win Rate", value="62.5%", value_color=Theme.SUCCESS_DARK)
    >>> lv.pack(side="left", padx=10)
    >>> lv.set_value("70%", Theme.SUCCESS_DARK)
    """

    def __init__(
        self,
        parent:      tk.Widget,
        label:       str    = "",
        value:       str    = "—",
        value_color: str    = T.TEXT_PRIMARY,
        label_size:  int    = T.FONT_XS,
        value_size:  int    = T.FONT_XL,
        bg:          str    = T.BG_PANEL,
        **kwargs,
    ):
        super().__init__(parent, bg=bg, **kwargs)

        tk.Label(
            self,
            text=label,
            font=T.font(size=label_size),
            bg=bg,
            fg=T.TEXT_MUTED,
        ).pack(anchor="w")

        self._value_lbl = tk.Label(
            self,
            text=value,
            font=T.font(size=value_size, weight="bold"),
            bg=bg,
            fg=value_color,
        )
        self._value_lbl.pack(anchor="w")

    def set_value(
        self,
        value: str,
        color: Optional[str] = None,
    ) -> None:
        """مقدار را به‌روز می‌کند."""
        kw: Dict[str, Any] = {"text": value}
        if color:
            kw["fg"] = color
        self._value_lbl.config(**kw)


# ══════════════════════════════════════════════════════════════
# SectionFrame
# ══════════════════════════════════════════════════════════════

class SectionFrame(tk.Frame):
    """
    قاب با عنوان فیروزه‌ای — جایگزین بهتر ttk.LabelFrame.

    Examples
    --------
    >>> sf = SectionFrame(parent, title="⚙️ Settings")
    >>> sf.pack(fill="x")
    >>> tk.Label(sf.body, text="API Key:").pack()
    """

    def __init__(
        self,
        parent:       tk.Widget,
        title:        str   = "",
        title_icon:   str   = "",
        collapsible:  bool  = False,
        **kwargs,
    ):
        super().__init__(parent, bg=T.BG_PANEL, **kwargs)
        self._collapsed = False

        # هدر
        header = tk.Frame(self, bg=T.PRIMARY_GHOST)
        header.pack(fill="x")

        full_title = f"{title_icon}  {title}" if title_icon else title
        self._title_lbl = tk.Label(
            header,
            text=full_title,
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=T.PRIMARY_GHOST,
            fg=T.PRIMARY_DARK,
            padx=T.PAD_MD,
            pady=T.PAD_SM,
        )
        self._title_lbl.pack(side="left")

        if collapsible:
            self._toggle_btn = tk.Button(
                header,
                text="▲",
                font=T.font(size=T.FONT_XS),
                bg=T.PRIMARY_GHOST,
                fg=T.PRIMARY_DARK,
                activebackground=T.PRIMARY_GHOST,
                relief="flat",
                cursor="hand2",
                bd=0,
                command=self._toggle,
            )
            self._toggle_btn.pack(side="right", padx=T.PAD_SM)

        # border
        tk.Frame(self, bg=T.BORDER, height=1).pack(fill="x")

        # body
        self.body = tk.Frame(self, bg=T.BG_PANEL)
        self.body.pack(fill="both", expand=True,
                       padx=T.PAD_MD, pady=T.PAD_MD)

    def _toggle(self) -> None:
        """collapse/expand body."""
        self._collapsed = not self._collapsed
        if self._collapsed:
            self.body.pack_forget()
            self._toggle_btn.config(text="▼")
        else:
            self.body.pack(fill="both", expand=True,
                           padx=T.PAD_MD, pady=T.PAD_MD)
            self._toggle_btn.config(text="▲")

    def set_title(self, title: str) -> None:
        self._title_lbl.config(text=title)


# ══════════════════════════════════════════════════════════════
# ProgressCard
# ══════════════════════════════════════════════════════════════

class ProgressCard(tk.Frame):
    """
    کارت نمایش پیشرفت با درصد و نوار.

    Examples
    --------
    >>> card = ProgressCard(parent, label="Win Rate", color=Theme.SUCCESS)
    >>> card.pack(side="left", padx=5, expand=True, fill="x")
    >>> card.set_value(62.5)
    """

    def __init__(
        self,
        parent: tk.Widget,
        label:  str = "",
        color:  str = T.PRIMARY,
        max_v:  float = 100.0,
        **kwargs,
    ):
        super().__init__(
            parent,
            bg=T.BG_PANEL,
            highlightthickness=1,
            highlightbackground=T.BORDER,
            **kwargs,
        )
        self._color = color
        self._max   = max_v

        inner = tk.Frame(self, bg=T.BG_PANEL)
        inner.pack(fill="both", expand=True,
                   padx=T.PAD_MD, pady=T.PAD_MD)

        tk.Label(
            inner,
            text=label,
            font=T.font(size=T.FONT_XS),
            bg=T.BG_PANEL,
            fg=T.TEXT_MUTED,
        ).pack(anchor="w")

        self._value_lbl = tk.Label(
            inner,
            text="—",
            font=T.font(size=T.FONT_LG, weight="bold"),
            bg=T.BG_PANEL,
            fg=color,
        )
        self._value_lbl.pack(anchor="w")

        # نوار پیشرفت
        bar_bg = tk.Frame(inner, bg=T.BORDER, height=6)
        bar_bg.pack(fill="x", pady=(T.PAD_XS, 0))

        self._bar = tk.Frame(bar_bg, bg=color, height=6, width=0)
        self._bar.place(x=0, y=0, relheight=1, relwidth=0)

    def set_value(self, value: float, suffix: str = "%") -> None:
        """مقدار و نوار را به‌روز می‌کند."""
        pct = max(0.0, min(1.0, value / self._max))

        color = self._color
        if suffix == "%":
            if value >= 60:
                color = T.SUCCESS_DARK
            elif value >= 40:
                color = T.WARNING_DARK
            else:
                color = T.DANGER_DARK

        self._value_lbl.config(
            text=f"{value:.1f}{suffix}",
            fg=color,
        )
        self._bar.config(bg=color)
        self._bar.place(relwidth=pct)


# ══════════════════════════════════════════════════════════════
# Exports
# ══════════════════════════════════════════════════════════════

__all__ = [
    "safe_clipboard_copy",
    "safe_clipboard_paste",
    "style_button",
    "make_tree",
    "AutocompleteCombobox",
    "CopyableLabel",
    "LabeledValue",
    "SectionFrame",
    "ProgressCard",
]