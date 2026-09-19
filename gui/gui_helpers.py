# source/gui/gui_helpers.py
"""
GUI Helper Components — v4.0.0

کامپوننت‌های کمکی یکپارچه با Theme فیروزه‌ای (gui.ui_theme.Theme).

Components:
    - ToolTip              : tooltip با تأخیر و throttle
    - center_window        : مرکز کردن Toplevel
    - ScrollableFrame      : قاب قابل scroll (mousewheel محلی)
    - ThemeManager         : مدیریت تم تاریک/روشن
    - LoadingOverlay       : overlay بارگذاری با spinner
    - StatusBar            : نوار وضعیت پایین
    - ConfirmDialog        : dialog تأیید Yes/No
    - KeyboardShortcuts    : مدیریت میانبرهای کیبورد
    - format_number        : فرمت‌بندی هوشمند عدد
    - make_labeled_entry   : Label + Entry در grid
    - make_labeled_combobox: Label + Combobox در grid
"""
from __future__ import annotations

import math
import logging
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from gui.ui_theme import Theme, Styles

logger = logging.getLogger(__name__)
T = Theme


# ══════════════════════════════════════════════════════════════
# ToolTip
# ══════════════════════════════════════════════════════════════

class ToolTip:
    """
    Tooltip با تأخیر و throttle برای هر widget.

    Examples
    --------
    >>> ToolTip(widget, "متن ثابت")
    >>> ToolTip(widget, lambda: f"مقدار: {val}")
    >>> ToolTip(widget, "توضیح", delay=600, wraplength=300)
    """

    _MOTION_THROTTLE_MS = 80

    def __init__(
        self,
        widget:     tk.Widget,
        text:       Union[str, Callable[[], str]],
        delay:      int = 400,
        wraplength: int = 380,
    ):
        self.widget     = widget
        self.text       = text
        self.delay      = max(0, int(delay))
        self.wraplength = int(wraplength)

        self.tip_window:   Optional[tk.Toplevel] = None
        self.after_id:     Optional[str]         = None
        self._motion_id:   Optional[str]         = None
        self._motion_job:  Optional[str]         = None
        self._motion_bound = False

        self._bind_events()

    # ── Binding ───────────────────────────────────────────────

    def _bind_events(self) -> None:
        try:
            self.widget.bind("<Enter>",       self._on_enter,   add="+")
            self.widget.bind("<Leave>",       self._on_leave,   add="+")
            self.widget.bind("<ButtonPress>", self._on_leave,   add="+")
            self.widget.bind("<Destroy>",     self._on_destroy, add="+")
        except tk.TclError:
            try:
                self.widget.bind("<Enter>",       self._on_enter)
                self.widget.bind("<Leave>",       self._on_leave)
                self.widget.bind("<ButtonPress>", self._on_leave)
                self.widget.bind("<Destroy>",     self._on_destroy)
            except Exception:
                pass

    # ── Events ────────────────────────────────────────────────

    def _on_enter(self, _=None):    self._schedule_show()
    def _on_leave(self, _=None):    self.hide_tip()
    def _on_destroy(self, _=None):
        self._cancel_scheduled()
        self._cancel_motion_job()
        self.hide_tip()

    def _on_motion(self, _=None):
        if not self.tip_window:
            return
        self._cancel_motion_job()
        try:
            self._motion_job = self.widget.after(
                self._MOTION_THROTTLE_MS, self._position_tip
            )
        except Exception:
            pass

    # ── Scheduling ────────────────────────────────────────────

    def _schedule_show(self) -> None:
        self._cancel_scheduled()
        if not self._widget_ok():
            return
        try:
            self.after_id = self.widget.after(self.delay, self.show_tip)
        except Exception:
            pass

    def _cancel_scheduled(self) -> None:
        if self.after_id and self._widget_ok():
            try:
                self.widget.after_cancel(self.after_id)
            except Exception:
                pass
        self.after_id = None

    def _cancel_motion_job(self) -> None:
        if self._motion_job and self._widget_ok():
            try:
                self.widget.after_cancel(self._motion_job)
            except Exception:
                pass
        self._motion_job = None

    # ── Helpers ───────────────────────────────────────────────

    def _widget_ok(self) -> bool:
        try:
            return bool(self.widget) and bool(self.widget.winfo_exists())
        except Exception:
            return False

    def _get_text(self) -> str:
        try:
            return str(self.text() if callable(self.text) else self.text or "")
        except Exception:
            return ""

    # ── Show / Hide ───────────────────────────────────────────

    def show_tip(self) -> None:
        if self.tip_window or not self._widget_ok():
            return
        tip_text = self._get_text().strip()
        if not tip_text:
            return

        try:
            tw = tk.Toplevel(self.widget)
        except Exception:
            return

        self.tip_window = tw
        tw.wm_overrideredirect(True)
        try:
            tw.attributes("-topmost", True)
        except Exception:
            pass

        tk.Label(
            tw,
            text=tip_text,
            justify=tk.LEFT,
            background=T.PRIMARY_GHOST,
            foreground=T.TEXT_PRIMARY,
            relief=tk.SOLID,
            borderwidth=1,
            font=T.font(size=T.FONT_XS),
            padx=T.PAD_SM,
            pady=T.PAD_XS,
            wraplength=self.wraplength,
        ).pack()

        tw.update_idletasks()
        self._position_tip()

        if not self._motion_bound and self._widget_ok():
            try:
                self._motion_id   = self.widget.bind("<Motion>", self._on_motion, add="+")
                self._motion_bound = True
            except Exception:
                pass

    def _position_tip(self) -> None:
        if not self.tip_window or not self._widget_ok():
            return
        try:
            x  = self.widget.winfo_pointerx() + 14
            y  = self.widget.winfo_pointery() + 14
            self.tip_window.update_idletasks()
            tw = self.tip_window.winfo_width()
            th = self.tip_window.winfo_height()
            sw = self.tip_window.winfo_screenwidth()
            sh = self.tip_window.winfo_screenheight()
            x  = min(max(x, 4), sw - tw - 4)
            y  = min(max(y, 4), sh - th - 4)
            self.tip_window.wm_geometry(f"+{x}+{y}")
        except Exception:
            self.hide_tip()

    def hide_tip(self) -> None:
        self._cancel_scheduled()
        self._cancel_motion_job()

        if self._motion_id and self._widget_ok():
            try:
                self.widget.unbind("<Motion>", self._motion_id)
            except Exception:
                pass
        self._motion_id    = None
        self._motion_bound = False

        if self.tip_window:
            try:
                self.tip_window.destroy()
            except Exception:
                pass
            self.tip_window = None


# ══════════════════════════════════════════════════════════════
# center_window
# ══════════════════════════════════════════════════════════════

def center_window(
    window: tk.Toplevel,
    width:  Optional[int] = None,
    height: Optional[int] = None,
) -> None:
    """Toplevel را روی صفحه مرکز می‌کند."""
    try:
        window.update_idletasks()
    except Exception:
        return

    def _safe(fn, fallback: int) -> int:
        try:
            v = fn()
            return v if v and v > 1 else fallback
        except Exception:
            return fallback

    if width is None:
        width = _safe(window.winfo_width, 0) or _safe(window.winfo_reqwidth, 600)
    if height is None:
        height = _safe(window.winfo_height, 0) or _safe(window.winfo_reqheight, 400)

    sw = _safe(window.winfo_screenwidth,  1920)
    sh = _safe(window.winfo_screenheight, 1080)
    x  = max(0, (sw - width)  // 2)
    y  = max(0, (sh - height) // 2)

    try:
        window.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════
# ScrollableFrame
# ══════════════════════════════════════════════════════════════

class ScrollableFrame(ttk.Frame):
    """
    قاب قابل scroll با Canvas.
    محتوا داخل self.inner قرار می‌گیرد.

    Examples
    --------
    >>> sf = ScrollableFrame(parent)
    >>> sf.pack(fill="both", expand=True)
    >>> ttk.Label(sf.inner, text="سلام").pack()
    """

    def __init__(
        self,
        parent,
        scroll_direction: str = "vertical",
        **kwargs,
    ):
        super().__init__(parent, **kwargs)
        self._direction = scroll_direction

        canvas_bg = T.BG_APP
        self.canvas = tk.Canvas(
            self,
            borderwidth=0,
            highlightthickness=0,
            background=canvas_bg,
        )
        self.inner = ttk.Frame(self.canvas)

        self._vsb: Optional[ttk.Scrollbar] = None
        self._hsb: Optional[ttk.Scrollbar] = None

        if scroll_direction in ("vertical", "both"):
            self._vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self._vsb.set)
            self._vsb.pack(side="right", fill="y")

        if scroll_direction in ("horizontal", "both"):
            self._hsb = ttk.Scrollbar(self, orient="horizontal", command=self.canvas.xview)
            self.canvas.configure(xscrollcommand=self._hsb.set)
            self._hsb.pack(side="bottom", fill="x")

        self.canvas.pack(side="left", fill="both", expand=True)
        self._win_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind("<Configure>",  self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)

        # mousewheel فقط وقتی ماوس داخل canvas است
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)

        self._mw_id: Optional[str] = None
        self._b4_id: Optional[str] = None
        self._b5_id: Optional[str] = None

    def _on_inner_configure(self, _=None):
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except Exception:
            pass

    def _on_canvas_configure(self, event=None):
        try:
            if event:
                self.canvas.itemconfig(self._win_id, width=event.width)
        except Exception:
            pass

    def _bind_mousewheel(self, _=None):
        try:
            self._mw_id = self.canvas.bind("<MouseWheel>", self._on_mousewheel)
            self._b4_id = self.canvas.bind("<Button-4>",   self._on_mousewheel)
            self._b5_id = self.canvas.bind("<Button-5>",   self._on_mousewheel)
        except Exception:
            pass

    def _unbind_mousewheel(self, _=None):
        for attr, seq in (("_mw_id", "<MouseWheel>"),
                          ("_b4_id", "<Button-4>"),
                          ("_b5_id", "<Button-5>")):
            bid = getattr(self, attr, None)
            if bid:
                try:
                    self.canvas.unbind(seq, bid)
                except Exception:
                    pass
            setattr(self, attr, None)

    def _on_mousewheel(self, event):
        if self._direction not in ("vertical", "both"):
            return
        try:
            if event.num == 4:
                self.canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self.canvas.yview_scroll(1, "units")
            else:
                self.canvas.yview_scroll(-int(event.delta / 120), "units")
        except Exception:
            pass

    def scroll_to_top(self):
        try:
            self.canvas.yview_moveto(0)
        except Exception:
            pass

    def scroll_to_bottom(self):
        try:
            self.canvas.yview_moveto(1)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════
# ThemeManager — یکپارچه با ui_theme.Theme
# ══════════════════════════════════════════════════════════════

class ThemeManager:
    """
    مدیریت تم تاریک/روشن.
    رنگ‌های Accent از Theme فیروزه‌ای گرفته می‌شوند.

    Examples
    --------
    >>> tm = ThemeManager(root)
    >>> tm.apply_dark()
    >>> tm.toggle()
    >>> color = tm.get_color("accent")
    """

    DARK: Dict[str, str] = {
        "bg":           "#0F1923",
        "fg":           "#E8F5F5",
        "accent":       T.PRIMARY,
        "accent_hover": T.PRIMARY_DARK,
        "entry_bg":     "#1A2A2A",
        "entry_fg":     T.TEXT_ON_DARK,
        "frame_bg":     "#152020",
        "tree_bg":      "#1A2A2A",
        "tree_fg":      T.TEXT_ON_DARK,
        "tree_sel_bg":  T.PRIMARY_DARK,
        "btn_bg":       "#1E3030",
        "btn_fg":       T.TEXT_ON_DARK,
        "border":       T.PRIMARY_DARKER,
        "success":      T.SUCCESS,
        "warning":      T.WARNING,
        "danger":       T.DANGER,
    }

    LIGHT: Dict[str, str] = {
        "bg":           T.BG_APP,
        "fg":           T.TEXT_PRIMARY,
        "accent":       T.PRIMARY,
        "accent_hover": T.PRIMARY_DARK,
        "entry_bg":     T.BG_INPUT,
        "entry_fg":     T.TEXT_PRIMARY,
        "frame_bg":     T.BG_PANEL,
        "tree_bg":      T.BG_PANEL,
        "tree_fg":      T.TEXT_PRIMARY,
        "tree_sel_bg":  T.PRIMARY_LIGHTER,
        "btn_bg":       T.BG_APP,
        "btn_fg":       T.TEXT_PRIMARY,
        "border":       T.BORDER,
        "success":      T.SUCCESS,
        "warning":      T.WARNING,
        "danger":       T.DANGER,
    }

    def __init__(self, root: tk.Tk):
        self.root    = root
        self._dark   = False
        self._colors = dict(self.LIGHT)

    @property
    def is_dark(self) -> bool:
        return self._dark

    @property
    def colors(self) -> Dict[str, str]:
        return dict(self._colors)

    def apply_dark(self)  -> None:
        self._dark   = True
        self._colors = dict(self.DARK)
        self._apply()

    def apply_light(self) -> None:
        self._dark   = False
        self._colors = dict(self.LIGHT)
        self._apply()

    def toggle(self) -> None:
        self.apply_light() if self._dark else self.apply_dark()

    def _apply(self) -> None:
        c = self._colors

        try:
            self.root.configure(background=c["bg"])
        except Exception:
            pass

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style_map: Dict[str, Dict] = {
            ".": {
                "configure": {"background": c["bg"], "foreground": c["fg"]},
            },
            "TFrame": {
                "configure": {"background": c["bg"]},
            },
            "TLabelframe": {
                "configure": {"background": c["bg"], "foreground": c["fg"]},
            },
            "TLabelframe.Label": {
                "configure": {"background": c["bg"], "foreground": c["accent"]},
            },
            "TLabel": {
                "configure": {"background": c["bg"], "foreground": c["fg"]},
            },
            "TButton": {
                "configure": {"background": c["btn_bg"], "foreground": c["btn_fg"]},
                "map": {
                    "background": [("active", c["accent"]), ("pressed", c["accent_hover"])],
                    "foreground": [("active", T.TEXT_ON_PRIMARY)],
                },
            },
            "TEntry": {
                "configure": {
                    "fieldbackground": c["entry_bg"],
                    "foreground":      c["entry_fg"],
                    "insertcolor":     c["accent"],
                },
            },
            "TCombobox": {
                "configure": {
                    "fieldbackground": c["entry_bg"],
                    "foreground":      c["entry_fg"],
                    "background":      c["btn_bg"],
                    "arrowcolor":      c["accent"],
                },
            },
            "TCheckbutton": {
                "configure": {"background": c["bg"], "foreground": c["fg"]},
                "map": {
                    "indicatorcolor": [("selected", c["accent"])],
                },
            },
            "TNotebook": {
                "configure": {"background": c["bg"]},
            },
            "TNotebook.Tab": {
                "configure": {
                    "background": c["btn_bg"],
                    "foreground": c["fg"],
                    "padding":    [T.PAD_LG, T.PAD_SM],
                },
                "map": {
                    "background": [("selected", c["accent"])],
                    "foreground": [("selected", T.TEXT_ON_PRIMARY)],
                },
            },
            "Treeview": {
                "configure": {
                    "background":      c["tree_bg"],
                    "foreground":      c["tree_fg"],
                    "fieldbackground": c["tree_bg"],
                    "rowheight":       T.ROW_HEIGHT,
                },
                "map": {
                    "background": [("selected", c["tree_sel_bg"])],
                    "foreground": [("selected", T.TEXT_ON_PRIMARY)],
                },
            },
            "Treeview.Heading": {
                "configure": {
                    "background": c["accent"],
                    "foreground": T.TEXT_ON_PRIMARY,
                    "relief":     "flat",
                },
                "map": {
                    "background": [("active", c["accent_hover"])],
                },
            },
            "TScrollbar": {
                "configure": {"background": c["btn_bg"], "troughcolor": c["bg"]},
                "map":       {"background": [("active", c["accent"])]},
            },
            "TProgressbar": {
                "configure": {"background": c["accent"], "troughcolor": c["bg"]},
            },
        }

        for widget_style, config in style_map.items():
            try:
                if "configure" in config:
                    style.configure(widget_style, **config["configure"])
                if "map" in config:
                    style.map(widget_style, **config["map"])
            except Exception as exc:
                logger.debug("Style error [%s]: %s", widget_style, exc)

        self._propagate_bg(self.root, c["bg"], c["fg"])

    def _propagate_bg(self, widget: tk.Widget, bg: str, fg: str) -> None:
        """bg را به تمام widget های tk (نه ttk) به‌صورت بازگشتی اعمال می‌کند."""
        try:
            cls = widget.__class__.__name__
            if cls in ("Frame", "Label", "Canvas", "Text",
                       "Listbox", "Button", "Checkbutton", "Radiobutton"):
                try:
                    widget.configure(background=bg)
                    if cls not in ("Frame", "Canvas"):
                        widget.configure(foreground=fg)
                except Exception:
                    pass
            for child in widget.winfo_children():
                self._propagate_bg(child, bg, fg)
        except Exception:
            pass

    def get_color(self, key: str, fallback: str = "#000000") -> str:
        return self._colors.get(key, fallback)


# ══════════════════════════════════════════════════════════════
# LoadingOverlay
# ══════════════════════════════════════════════════════════════

class LoadingOverlay:
    """
    Overlay نیمه‌شفاف بارگذاری با spinner فیروزه‌ای.
    بلافاصله هنگام ساخت نشان داده می‌شود.

    Examples
    --------
    >>> overlay = LoadingOverlay(parent, text="در حال پردازش…")
    >>> # کار پس‌زمینه
    >>> parent.after(0, overlay.destroy)

    >>> with LoadingOverlay(parent, "صبر کنید…"):
    ...     time.sleep(2)
    """

    _SPINNER = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(
        self,
        parent: tk.Widget,
        text:   str   = "Please wait…",
        alpha:  float = 0.65,
    ):
        self.parent = parent
        self.text   = text
        self.alpha  = alpha

        self._frame:       Optional[tk.Toplevel] = None
        self._text_lbl:    Optional[tk.Label]    = None
        self._spinner_lbl: Optional[tk.Label]    = None
        self._spinner_idx  = 0
        self._anim_id:     Optional[str]         = None

        self._show()

    def _show(self) -> None:
        if self._frame:
            return
        try:
            self.parent.update_idletasks()
            x = self.parent.winfo_rootx()
            y = self.parent.winfo_rooty()
            w = self.parent.winfo_width()
            h = self.parent.winfo_height()
        except Exception:
            x, y, w, h = 0, 0, 400, 300

        try:
            tw = tk.Toplevel(self.parent)
            tw.overrideredirect(True)
            tw.attributes("-topmost", True)
            try:
                tw.attributes("-alpha", float(self.alpha))
            except Exception:
                pass
            tw.geometry(f"{w}x{h}+{x}+{y}")

            bg_frame = tk.Frame(tw, bg=T.PRIMARY_DARKER)
            bg_frame.pack(fill="both", expand=True)

            # Spinner
            self._spinner_lbl = tk.Label(
                bg_frame,
                text=self._SPINNER[0],
                font=T.font(size=T.FONT_3XL),
                fg=T.PRIMARY_LIGHT,
                bg=T.PRIMARY_DARKER,
            )
            self._spinner_lbl.pack(expand=True, pady=(80, T.PAD_SM))

            # Text
            self._text_lbl = tk.Label(
                bg_frame,
                text=self.text,
                font=T.font(size=T.FONT_MD),
                fg=T.TEXT_ON_DARK,
                bg=T.PRIMARY_DARKER,
            )
            self._text_lbl.pack(pady=(0, 80))

            self._frame = tw
            self._animate()

        except Exception as exc:
            logger.debug("LoadingOverlay._show error: %s", exc)
            self._frame = None

    def _animate(self) -> None:
        if not self._frame:
            return
        try:
            self._spinner_idx = (self._spinner_idx + 1) % len(self._SPINNER)
            if self._spinner_lbl:
                self._spinner_lbl.config(text=self._SPINNER[self._spinner_idx])
            self._anim_id = self._frame.after(80, self._animate)
        except Exception:
            pass

    def update_message(self, message: str) -> None:
        """متن overlay را به‌روز می‌کند (از main thread فراخوانی کنید)."""
        self.text = message
        if self._text_lbl:
            try:
                self._text_lbl.config(text=message)
            except Exception:
                pass

    def hide(self) -> None:
        """alias برای backward compatibility."""
        self.destroy()

    def destroy(self) -> None:
        """overlay را حذف می‌کند (از main thread فراخوانی کنید)."""
        if self._anim_id and self._frame:
            try:
                self._frame.after_cancel(self._anim_id)
            except Exception:
                pass
        self._anim_id = None

        if self._frame:
            try:
                self._frame.destroy()
            except Exception:
                pass
            self._frame = None

    def __enter__(self) -> "LoadingOverlay":
        return self

    def __exit__(self, *_) -> None:
        self.destroy()


# ══════════════════════════════════════════════════════════════
# StatusBar
# ══════════════════════════════════════════════════════════════

class StatusBar(tk.Frame):
    """
    نوار وضعیت پایین با Theme فیروزه‌ای.

    Examples
    --------
    >>> sb = StatusBar(root)
    >>> sb.pack(side="bottom", fill="x")
    >>> sb.set_status("آماده", color=Theme.SUCCESS_DARK)
    >>> sb.set_right("v4.0 | Premium")
    >>> sb.show_progress()
    >>> sb.hide_progress()
    """

    def __init__(self, parent: tk.Widget, **kwargs):
        super().__init__(parent, bg=T.PRIMARY, height=32, **kwargs)
        self.pack_propagate(False)

        self._left_var  = tk.StringVar(value="Ready 🚀")
        self._right_var = tk.StringVar(value="")

        self._left_lbl = tk.Label(
            self,
            textvariable=self._left_var,
            font=T.font(size=T.FONT_SM),
            bg=T.PRIMARY,
            fg=T.TEXT_ON_PRIMARY,
            anchor="w",
        )
        self._left_lbl.pack(side="left", padx=T.PAD_XL, fill="x", expand=True)

        self._right_lbl = tk.Label(
            self,
            textvariable=self._right_var,
            font=T.font(size=T.FONT_XS),
            bg=T.PRIMARY,
            fg=T.PRIMARY_LIGHTER,
            anchor="e",
        )
        self._right_lbl.pack(side="right", padx=T.PAD_XL)

        # Progress bar — ساخته می‌شود ولی پک نمی‌شود تا show_progress
        self._progress = ttk.Progressbar(self, mode="indeterminate", length=120)
        self._progress_visible = False

    def set_status(
        self,
        message:    str,
        color:      str = "",
        timeout_ms: int = 0,
    ) -> None:
        try:
            self._left_var.set(message)
            self._left_lbl.configure(fg=color or T.TEXT_ON_PRIMARY)
        except Exception:
            pass
        if timeout_ms > 0:
            try:
                self.after(timeout_ms, lambda: self.set_status("Ready 🚀"))
            except Exception:
                pass

    def set_right(self, text: str, color: str = "") -> None:
        try:
            self._right_var.set(text)
            if color:
                self._right_lbl.configure(fg=color)
        except Exception:
            pass

    def show_progress(self) -> None:
        if not self._progress_visible:
            try:
                self._progress.pack(side="left", padx=T.PAD_MD, pady=4)
                self._progress.start(10)
                self._progress_visible = True
            except Exception:
                pass

    def hide_progress(self) -> None:
        if self._progress_visible:
            try:
                self._progress.stop()
                self._progress.pack_forget()
                self._progress_visible = False
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════
# ConfirmDialog
# ══════════════════════════════════════════════════════════════

class ConfirmDialog:
    """
    Dialog تأیید Yes/No با Theme فیروزه‌ای.

    Examples
    --------
    >>> ok = ConfirmDialog.ask(
    ...     parent,
    ...     title="حذف؟",
    ...     message="آیا مطمئن هستید؟",
    ...     detail="این عمل قابل بازگشت نیست.",
    ...     danger=True,
    ... )
    """

    @staticmethod
    def ask(
        parent,
        title:    str  = "Confirm",
        message:  str  = "Are you sure?",
        detail:   str  = "",
        yes_text: str  = "Yes",
        no_text:  str  = "No",
        icon:     str  = "❓",
        danger:   bool = False,
    ) -> bool:
        result = [False]

        dlg = tk.Toplevel(parent)
        dlg.title(title)
        dlg.configure(bg=T.BG_APP)
        dlg.transient(parent)
        dlg.grab_set()
        dlg.resizable(False, False)

        msg_len = len(message) + len(detail)
        w = min(520, max(380, msg_len * 4))
        h = 230 if not detail else 280
        center_window(dlg, w, h)

        main = tk.Frame(dlg, bg=T.BG_APP, padx=T.PAD_2XL, pady=T.PAD_2XL)
        main.pack(fill="both", expand=True)

        # Icon + message
        top_row = tk.Frame(main, bg=T.BG_APP)
        top_row.pack(fill="x", pady=(0, T.PAD_LG))

        tk.Label(
            top_row,
            text=icon,
            font=T.font(size=T.FONT_2XL),
            bg=T.BG_APP,
        ).pack(side="left", padx=(0, T.PAD_LG))

        msg_frame = tk.Frame(top_row, bg=T.BG_APP)
        msg_frame.pack(side="left", fill="x", expand=True)

        tk.Label(
            msg_frame,
            text=message,
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.BG_APP,
            fg=T.TEXT_PRIMARY,
            wraplength=max(300, w - 120),
            justify="left",
        ).pack(anchor="w")

        if detail:
            tk.Label(
                msg_frame,
                text=detail,
                font=T.font(size=T.FONT_SM),
                bg=T.BG_APP,
                fg=T.TEXT_SECONDARY,
                wraplength=max(300, w - 120),
                justify="left",
            ).pack(anchor="w", pady=(T.PAD_SM, 0))

        # جداکننده
        tk.Frame(main, bg=T.BORDER, height=1).pack(fill="x", pady=T.PAD_MD)

        # دکمه‌ها
        btn_frame = tk.Frame(main, bg=T.BG_APP)
        btn_frame.pack()

        yes_bg = T.DANGER if danger else T.PRIMARY
        yes_hv = T.DANGER_DARK if danger else T.PRIMARY_DARK

        yes_btn = tk.Button(
            btn_frame,
            text=yes_text,
            font=T.font(size=T.FONT_BASE, weight="bold"),
            bg=yes_bg,
            fg=T.TEXT_ON_PRIMARY,
            activebackground=yes_hv,
            activeforeground=T.TEXT_ON_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_2XL,
            pady=T.PAD_SM,
            bd=0,
            command=lambda: [result.__setitem__(0, True), dlg.destroy()],
        )
        yes_btn.pack(side="left", padx=(0, T.PAD_MD))

        tk.Button(
            btn_frame,
            text=no_text,
            font=T.font(size=T.FONT_BASE),
            bg=T.BG_DISABLED,
            fg=T.TEXT_SECONDARY,
            activebackground=T.BORDER,
            activeforeground=T.TEXT_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_2XL,
            pady=T.PAD_SM,
            bd=0,
            command=dlg.destroy,
        ).pack(side="left")

        dlg.bind("<Return>", lambda _: yes_btn.invoke())
        dlg.bind("<Escape>", lambda _: dlg.destroy())
        dlg.wait_window()
        return result[0]


# ══════════════════════════════════════════════════════════════
# KeyboardShortcuts
# ══════════════════════════════════════════════════════════════

class KeyboardShortcuts:
    """
    مدیریت میانبرهای کیبورد در سطح برنامه.

    Examples
    --------
    >>> ks = KeyboardShortcuts(root)
    >>> ks.register("<Control-r>", app.refresh, "Refresh data")
    >>> ks.show_help_dialog(root)
    """

    def __init__(self, root: tk.Widget):
        self.root = root
        self._bindings: Dict[str, Tuple[Callable, str, str]] = {}

    def register(
        self,
        key:         str,
        callback:    Callable,
        description: str = "",
    ) -> None:
        self.unregister(key)
        try:
            bid = self.root.bind(key, lambda _: callback(), add="+")
            self._bindings[key] = (callback, description, bid)
            logger.debug("Shortcut: %s → %s", key, description)
        except Exception as exc:
            logger.warning("Failed to register shortcut %s: %s", key, exc)

    def unregister(self, key: str) -> None:
        entry = self._bindings.pop(key, None)
        if entry and len(entry) == 3:
            try:
                self.root.unbind(key, entry[2])
            except Exception:
                pass

    def show_help_dialog(self, parent: tk.Widget) -> None:
        if not self._bindings:
            return

        dlg = tk.Toplevel(parent)
        dlg.title("⌨ Keyboard Shortcuts")
        dlg.configure(bg=T.BG_APP)
        dlg.transient(parent)
        dlg.grab_set()
        dlg.resizable(False, False)
        center_window(dlg, 440, min(80 + len(self._bindings) * 36, 520))

        tk.Label(
            dlg,
            text="⌨  Keyboard Shortcuts",
            font=T.font(size=T.FONT_LG, weight="bold"),
            bg=T.PRIMARY,
            fg=T.TEXT_ON_PRIMARY,
        ).pack(fill="x", padx=0, pady=0, ipady=T.PAD_MD)

        frame = tk.Frame(dlg, bg=T.BG_APP, padx=T.PAD_LG, pady=T.PAD_MD)
        frame.pack(fill="both", expand=True)

        for i, (key, (_, desc, _bid)) in enumerate(self._bindings.items()):
            bg = T.BG_ROW_ALT if i % 2 == 0 else T.BG_PANEL
            row = tk.Frame(frame, bg=bg)
            row.pack(fill="x")

            tk.Label(
                row,
                text=key,
                font=T.font(size=T.FONT_SM, family=T.FONT_FAMILY_MONO),
                bg=bg,
                fg=T.PRIMARY,
                width=20,
                anchor="w",
            ).pack(side="left", padx=T.PAD_MD, pady=T.PAD_SM)

            tk.Label(
                row,
                text=desc,
                font=T.font(size=T.FONT_SM),
                bg=bg,
                fg=T.TEXT_SECONDARY,
                anchor="w",
            ).pack(side="left", padx=T.PAD_SM)

        tk.Button(
            dlg,
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
            command=dlg.destroy,
        ).pack(pady=T.PAD_MD)

        dlg.bind("<Escape>", lambda _: dlg.destroy())


# ══════════════════════════════════════════════════════════════
# Utility Functions
# ══════════════════════════════════════════════════════════════

def format_number(
    value,
    decimals: int = 2,
    prefix:   str = "",
    suffix:   str = "",
    fallback: str = "--",
) -> str:
    """
    فرمت‌بندی هوشمند عدد.

    Examples
    --------
    >>> format_number(1_234_567.89)         # '1,234,567.89'
    >>> format_number(0.00012345, decimals=6) # '0.000123'
    >>> format_number(None)                  # '--'
    """
    if value is None:
        return fallback
    try:
        v = float(value)
        if not math.isfinite(v):
            return fallback

        d = decimals
        if d == 2 and 0 < abs(v) < 0.01:
            d = 6
        elif d == 2 and 0 < abs(v) < 1:
            d = 4

        return f"{prefix}{v:,.{d}f}{suffix}"
    except (ValueError, TypeError):
        return fallback


def make_labeled_entry(
    parent,
    label:       str,
    variable:    Optional[tk.Variable] = None,
    default:     str  = "",
    width:       int  = 20,
    row:         int  = 0,
    col:         int  = 0,
    label_width: int  = 0,
    tooltip:     str  = "",
    read_only:   bool = False,
    password:    bool = False,
) -> ttk.Entry:
    """Label + Entry در grid layout."""
    lbl_kw: Dict[str, Any] = {"text": label}
    if label_width:
        lbl_kw["width"] = label_width

    lbl = ttk.Label(parent, **lbl_kw)
    lbl.grid(row=row, column=col, sticky="w", padx=(0, T.PAD_SM), pady=T.PAD_XS)

    entry_kw: Dict[str, Any] = {"width": width}
    if variable:
        entry_kw["textvariable"] = variable
    if password:
        entry_kw["show"] = "*"
    if read_only:
        entry_kw["state"] = "readonly"

    entry = ttk.Entry(parent, **entry_kw)
    if default and not variable:
        entry.insert(0, default)
    entry.grid(row=row, column=col + 1, sticky="ew", pady=T.PAD_XS)

    if tooltip:
        ToolTip(entry, tooltip)
        ToolTip(lbl,   tooltip)

    return entry


def make_labeled_combobox(
    parent,
    label:    str,
    values:   List[str],
    variable: Optional[tk.StringVar] = None,
    default:  str  = "",
    width:    int  = 15,
    row:      int  = 0,
    col:      int  = 0,
    state:    str  = "readonly",
    tooltip:  str  = "",
) -> ttk.Combobox:
    """Label + Combobox در grid layout."""
    ttk.Label(parent, text=label).grid(
        row=row, column=col, sticky="w",
        padx=(0, T.PAD_SM), pady=T.PAD_XS,
    )

    var = variable or tk.StringVar(value=default)
    cb  = ttk.Combobox(
        parent, textvariable=var,
        values=values, state=state, width=width,
    )
    cb.grid(row=row, column=col + 1, sticky="w", pady=T.PAD_XS)

    if tooltip:
        ToolTip(cb, tooltip)

    return cb


# ══════════════════════════════════════════════════════════════
# Exports
# ══════════════════════════════════════════════════════════════

__all__ = [
    "ToolTip",
    "center_window",
    "ScrollableFrame",
    "ThemeManager",
    "LoadingOverlay",
    "StatusBar",
    "ConfirmDialog",
    "KeyboardShortcuts",
    "format_number",
    "make_labeled_entry",
    "make_labeled_combobox",
]