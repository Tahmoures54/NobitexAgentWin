# source/gui/dialogs/base_dialog.py
from __future__ import annotations

import logging
import tkinter as tk
from tkinter import ttk, messagebox
from typing import Any, Callable, Optional, Tuple

from gui.gui_helpers import center_window
from gui.ui_theme import Theme, Styles

logger = logging.getLogger(__name__)
T = Theme


class BaseDialog:
    def __init__(
        self,
        parent: tk.Widget,
        app: Any,
        title: str,
        width: int = 600,
        height: int = 450,
        resizable: Tuple[bool, bool] = (False, False),
        min_size: Optional[Tuple[int, int]] = None,
        modal: bool = True,
    ):
        self.parent = parent
        self.app = app
        self._modal = modal

        self.dlg = tk.Toplevel(parent)
        self.dlg.title(title)
        self.dlg.configure(bg=T.BG_APP)
        self.dlg.transient(parent)
        self.dlg.resizable(*resizable)

        if min_size:
            self.dlg.minsize(*min_size)
        elif any(resizable):
            self.dlg.minsize(width, height)

        if modal:
            self.dlg.grab_set()

        self.header = tk.Frame(self.dlg, bg=T.PRIMARY, height=T.HEADER_HEIGHT)
        self.header.pack(fill="x")
        self.header.pack_propagate(False)

        self._title_lbl = tk.Label(
            self.header,
            text=title,
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.PRIMARY,
            fg=T.TEXT_ON_PRIMARY,
        )
        self._title_lbl.pack(side="left", padx=T.PAD_XL, pady=T.PAD_SM)

        tk.Button(
            self.header,
            text="✕",
            font=T.font(size=T.FONT_SM),
            bg=T.PRIMARY,
            fg=T.TEXT_ON_PRIMARY,
            activebackground=T.PRIMARY_DARK,
            activeforeground=T.TEXT_ON_PRIMARY,
            relief="flat",
            cursor="hand2",
            bd=0,
            padx=T.PAD_MD,
            pady=T.PAD_XS,
            command=self.close,
        ).pack(side="right", padx=T.PAD_SM)

        self.body = tk.Frame(self.dlg, bg=T.BG_APP)
        self.body.pack(fill="both", expand=True, padx=T.PAD_XL, pady=T.PAD_XL)

        self.footer = tk.Frame(self.dlg, bg=T.BG_APP)
        self.footer.pack(fill="x", padx=T.PAD_XL, pady=(0, T.PAD_LG))

        try:
            style = ttk.Style(self.dlg)
            Styles.apply(style)
        except Exception as exc:
            logger.debug("ttk.Style failed: %s", exc)

        center_window(self.dlg, width, height)
        self.dlg.protocol("WM_DELETE_WINDOW", self.close)
        self.dlg.after(50, self.on_open)

    def on_open(self) -> None:
        pass

    def on_close(self) -> bool:
        return True

    def close(self) -> None:
        if not self._dlg_exists():
            return
        try:
            if not self.on_close():
                return
            if self._modal:
                try:
                    self.dlg.grab_release()
                except Exception:
                    pass
            self.dlg.destroy()
            logger.debug("%s closed.", self.__class__.__name__)
        except Exception as exc:
            logger.debug("Error closing dialog: %s", exc)

    def wait(self) -> None:
        if self._dlg_exists():
            self.dlg.wait_window(self.dlg)

    def set_title(self, title: str) -> None:
        try:
            self.dlg.title(title)
            self._title_lbl.config(text=title)
        except Exception:
            pass

    def _safe_ui_call(self, callback: Callable, delay_ms: int = 0) -> None:
        if self._dlg_exists():
            try:
                self.dlg.after(delay_ms, callback)
            except Exception as exc:
                logger.debug("_safe_ui_call failed: %s", exc)

    def _safe_ui_call_from_thread(self, callback: Callable, delay_ms: int = 0) -> None:
        self._safe_ui_call(callback, delay_ms)

    def _dlg_exists(self) -> bool:
        try:
            return bool(self.dlg and self.dlg.winfo_exists())
        except Exception:
            return False

    def show_error(self, title: str, message: str) -> None:
        if self._dlg_exists():
            messagebox.showerror(title, message, parent=self.dlg)

    def show_info(self, title: str, message: str) -> None:
        if self._dlg_exists():
            messagebox.showinfo(title, message, parent=self.dlg)

    def show_warning(self, title: str, message: str) -> None:
        if self._dlg_exists():
            messagebox.showwarning(title, message, parent=self.dlg)

    def ask_confirm(self, title: str, message: str) -> bool:
        if not self._dlg_exists():
            return False
        return messagebox.askyesno(title, message, parent=self.dlg)

    def _add_ok_cancel(
        self,
        ok_text: str = "OK",
        cancel_text: str = "Cancel",
        ok_command: Optional[Callable] = None,
        ok_style: str = "primary",
    ) -> Tuple[tk.Button, tk.Button]:
        style_map = {
            "primary": (T.PRIMARY, T.PRIMARY_DARK),
            "danger": (T.DANGER, T.DANGER_DARK),
            "success": (T.SUCCESS, T.SUCCESS_DARK),
            "warning": (T.WARNING, T.WARNING_DARK),
        }
        bg, hv = style_map.get(ok_style, (T.PRIMARY, T.PRIMARY_DARK))

        def _ok():
            if ok_command:
                ok_command()
            else:
                self.close()

        btn_frame = tk.Frame(self.footer, bg=T.BG_APP)
        btn_frame.pack(side="right")

        cancel_btn = tk.Button(
            btn_frame,
            text=cancel_text,
            font=T.font(size=T.FONT_SM),
            bg=T.BG_DISABLED,
            fg=T.TEXT_SECONDARY,
            activebackground=T.BORDER,
            activeforeground=T.TEXT_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_XL,
            pady=T.PAD_SM,
            bd=0,
            command=self.close,
        )
        cancel_btn.pack(side="right", padx=(T.PAD_SM, 0))

        ok_btn = tk.Button(
            btn_frame,
            text=ok_text,
            font=T.font(size=T.FONT_SM, weight="bold"),
            bg=bg,
            fg=T.TEXT_ON_PRIMARY,
            activebackground=hv,
            activeforeground=T.TEXT_ON_PRIMARY,
            relief="flat",
            cursor="hand2",
            padx=T.PAD_XL,
            pady=T.PAD_SM,
            bd=0,
            command=_ok,
        )
        ok_btn.pack(side="right", padx=(0, T.PAD_SM))

        self.dlg.bind("<Return>", lambda _: ok_btn.invoke())
        self.dlg.bind("<Escape>", lambda _: self.close())

        return ok_btn, cancel_btn

    def _add_separator(self) -> None:
        tk.Frame(self.dlg, bg=T.BORDER, height=1).pack(fill="x")

    def __enter__(self) -> "BaseDialog":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    def __repr__(self) -> str:
        exists = self._dlg_exists()
        return (
            f"{self.__class__.__name__}("
            f"title='{self.dlg.title() if exists else '?'}', "
            f"exists={exists}"
            f")"
        )