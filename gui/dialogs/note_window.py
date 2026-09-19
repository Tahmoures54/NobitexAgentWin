# source/gui/dialogs/note_window.py
"""
NoteWindow — پنجره یادداشت‌برداری.

ویژگی‌ها:
    - نمایش یادداشت‌های قبلی با timestamp
    - نوشتن یادداشت جدید
    - جستجو در یادداشت‌ها
    - حذف یادداشت‌های قدیمی
    - پیش‌پر کردن با اطلاعات ارز انتخابی
    - ذخیره‌سازی atomic
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path
from tkinter import scrolledtext
from typing import List, Optional
import tkinter as tk
from tkinter import ttk

from core.config import APPDATA_DIR
from gui.dialogs.base_dialog import BaseDialog
from gui.dialogs.components import style_button
from gui.gui_helpers import ToolTip
from gui.ui_theme import Theme

logger = logging.getLogger(__name__)
T = Theme

# ── مسیر فایل یادداشت‌ها ─────────────────────────────────────
_NOTES_FILE = Path(APPDATA_DIR) / "crypto_notes.txt"
_SEPARATOR  = "─" * 50


# ══════════════════════════════════════════════════════════════
# File I/O
# ══════════════════════════════════════════════════════════════

def _ensure_notes_dir() -> None:
    """دایرکتوری یادداشت‌ها را می‌سازد اگر وجود نداشته باشد."""
    _NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_notes() -> str:
    """
    محتوای فایل یادداشت‌ها را می‌خواند.

    Returns
    -------
    محتوای فایل یا '' در صورت عدم وجود یا خطا.
    """
    try:
        if _NOTES_FILE.exists():
            return _NOTES_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        logger.error("Cannot read notes file: %s", exc)
    return ""


def _append_note(content: str) -> None:
    """
    یادداشت جدید را به فایل اضافه می‌کند (atomic write).

    Parameters
    ----------
    content:
        متن یادداشت.

    Raises
    ------
    OSError:
        اگر نوشتن به فایل ممکن نباشد.
    """
    _ensure_notes_dir()
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{ts}]\n{content}\n{_SEPARATOR}\n"

    tmp_path = _NOTES_FILE.with_suffix(".tmp")
    existing = _load_notes()

    try:
        tmp_path.write_text(existing + entry, encoding="utf-8")
        tmp_path.replace(_NOTES_FILE)
    except OSError:
        # پاکسازی tmp در صورت خطا
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _clear_notes() -> None:
    """تمام یادداشت‌ها را حذف می‌کند."""
    try:
        if _NOTES_FILE.exists():
            _NOTES_FILE.unlink()
    except OSError as exc:
        logger.error("Cannot clear notes: %s", exc)
        raise


# ══════════════════════════════════════════════════════════════
# NoteWindow
# ══════════════════════════════════════════════════════════════

class NoteWindow(BaseDialog):
    """
    پنجره یادداشت‌برداری Crypto Scanner.

    Features:
        - نمایش و جستجوی یادداشت‌های قبلی
        - نوشتن یادداشت جدید
        - پیش‌پر کردن با اطلاعات ارز انتخابی در جدول
        - حذف همه یادداشت‌ها
    """

    def __init__(self, parent: tk.Widget, app):
        super().__init__(
            parent,
            app,
            title="📝 Crypto Notes",
            width=560,
            height=640,
            resizable=(True, True),
            min_size=(420, 480),
        )

        self._selected_coin: str = self._get_selected_coin()
        self._search_var = tk.StringVar()

        self._build_previous_section()
        self._build_new_note_section()
        self._add_separator()
        self._build_footer_buttons()

        # بارگذاری یادداشت‌های قبلی
        self._load_and_display()

    # ══════════════════════════════════════════════════════════
    # Coin Detection
    # ══════════════════════════════════════════════════════════

    def _get_selected_coin(self) -> str:
        """
        اطلاعات ارز انتخابی در جدول اصلی را می‌خواند.

        Returns
        -------
        رشته آماده برای پیش‌پر کردن یادداشت یا ''.
        """
        try:
            tree = getattr(self.app, "tree", None)
            if not tree:
                return ""

            sel = tree.focus()
            if not sel:
                return ""

            vals = tree.item(sel, "values")
            cols = list(tree["columns"])

            parts: List[str] = []

            for col in ("Symbol", "Name", "Price", "24h %", "Signal"):
                if col in cols:
                    idx = cols.index(col)
                    if len(vals) > idx and vals[idx]:
                        parts.append(f"{col}: {vals[idx]}")

            return "\n".join(parts) + "\n" if parts else ""

        except Exception as exc:
            logger.debug("Could not get selected coin: %s", exc)
            return ""

    # ══════════════════════════════════════════════════════════
    # UI Sections
    # ══════════════════════════════════════════════════════════

    def _build_previous_section(self) -> None:
        """بخش نمایش یادداشت‌های قبلی با جستجو."""
        section = tk.Frame(self.body, bg=T.BG_APP)
        section.pack(fill="both", expand=True, pady=(0, T.PAD_MD))

        # ── هدر بخش ──────────────────────────────────────────
        hdr = tk.Frame(section, bg=T.BG_APP)
        hdr.pack(fill="x", pady=(0, T.PAD_SM))

        tk.Label(
            hdr,
            text="📖  Previous Notes",
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.BG_APP,
            fg=T.PRIMARY,
        ).pack(side="left")

        # دکمه refresh
        tk.Button(
            hdr,
            text="🔄",
            font=T.font(size=T.FONT_SM),
            bg=T.BG_APP,
            fg=T.TEXT_SECONDARY,
            activebackground=T.PRIMARY_GHOST,
            activeforeground=T.PRIMARY,
            relief="flat",
            cursor="hand2",
            bd=0,
            padx=T.PAD_SM,
            command=self._load_and_display,
        ).pack(side="right")

        ToolTip(hdr.winfo_children()[-1], "Reload notes")

        # ── جستجو ─────────────────────────────────────────────
        search_frame = tk.Frame(section, bg=T.BG_APP)
        search_frame.pack(fill="x", pady=(0, T.PAD_SM))

        tk.Label(
            search_frame,
            text="🔍",
            font=T.font(size=T.FONT_SM),
            bg=T.BG_APP,
            fg=T.TEXT_MUTED,
        ).pack(side="left", padx=(0, T.PAD_XS))

        search_entry = ttk.Entry(
            search_frame,
            textvariable=self._search_var,
            width=30,
        )
        search_entry.pack(side="left", fill="x", expand=True)
        self._search_var.trace_add("write", lambda *_: self._filter_notes())
        ToolTip(search_entry, "Search notes")

        tk.Button(
            search_frame,
            text="✕",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP,
            fg=T.TEXT_MUTED,
            activebackground=T.BG_APP,
            relief="flat",
            cursor="hand2",
            bd=0,
            padx=T.PAD_XS,
            command=lambda: self._search_var.set(""),
        ).pack(side="left")

        # ── متن یادداشت‌های قبلی ──────────────────────────────
        text_frame = tk.Frame(
            section,
            bg=T.BG_PANEL,
            highlightthickness=1,
            highlightbackground=T.BORDER,
        )
        text_frame.pack(fill="both", expand=True)

        self._prev_text = scrolledtext.ScrolledText(
            text_frame,
            wrap=tk.WORD,
            font=T.font(size=T.FONT_SM, family=T.FONT_FAMILY_MONO),
            state="disabled",
            bg=T.BG_PANEL,
            fg=T.TEXT_PRIMARY,
            insertbackground=T.PRIMARY,
            borderwidth=0,
            highlightthickness=0,
            height=10,
        )
        self._prev_text.pack(fill="both", expand=True, padx=2, pady=2)

        # tag های highlight برای جستجو
        self._prev_text.tag_config(
            "highlight",
            background=T.WARNING_BG,
            foreground=T.WARNING_DARK,
        )
        self._prev_text.tag_config(
            "timestamp",
            foreground=T.PRIMARY,
            font=T.font(size=T.FONT_XS, weight="bold"),
        )

        # counter
        self._notes_count_lbl = tk.Label(
            section,
            text="",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP,
            fg=T.TEXT_MUTED,
            anchor="e",
        )
        self._notes_count_lbl.pack(fill="x", pady=(T.PAD_XS, 0))

    def _build_new_note_section(self) -> None:
        """بخش نوشتن یادداشت جدید."""
        section = tk.Frame(self.body, bg=T.BG_APP)
        section.pack(fill="x")

        hdr = tk.Frame(section, bg=T.BG_APP)
        hdr.pack(fill="x", pady=(0, T.PAD_SM))

        tk.Label(
            hdr,
            text="✍️  New Note",
            font=T.font(size=T.FONT_MD, weight="bold"),
            bg=T.BG_APP,
            fg=T.PRIMARY,
        ).pack(side="left")

        # شمارنده کاراکتر
        self._char_count_lbl = tk.Label(
            hdr,
            text="0 chars",
            font=T.font(size=T.FONT_XS),
            bg=T.BG_APP,
            fg=T.TEXT_MUTED,
        )
        self._char_count_lbl.pack(side="right")

        # متن جدید
        text_frame = tk.Frame(
            section,
            bg=T.BG_PANEL,
            highlightthickness=1,
            highlightbackground=T.PRIMARY,
        )
        text_frame.pack(fill="x")

        self._note_text = scrolledtext.ScrolledText(
            text_frame,
            wrap=tk.WORD,
            font=T.font(size=T.FONT_BASE),
            bg=T.BG_PANEL,
            fg=T.TEXT_PRIMARY,
            insertbackground=T.PRIMARY,
            borderwidth=0,
            highlightthickness=0,
            height=7,
        )
        self._note_text.pack(fill="x", padx=2, pady=2)

        # پیش‌پر با اطلاعات ارز
        if self._selected_coin:
            self._note_text.insert("1.0", self._selected_coin)
            # cursor را به آخر می‌بریم تا کاربر ادامه بدهد
            self._note_text.mark_set("insert", "end")

        self._note_text.focus()
        self._note_text.bind(
            "<KeyRelease>",
            lambda _: self._update_char_count(),
        )
        self._update_char_count()

    def _build_footer_buttons(self) -> None:
        """دکمه‌های عملیاتی footer."""
        # دکمه حذف همه (سمت چپ)
        clear_btn = tk.Button(
            self.footer,
            text="🗑️ Clear All",
            font=T.font(size=T.FONT_SM),
            bg=T.BG_APP,
            fg=T.DANGER,
            activebackground=T.DANGER_BG,
            activeforeground=T.DANGER_DARK,
            relief="flat",
            cursor="hand2",
            bd=0,
            padx=T.PAD_MD,
            pady=T.PAD_SM,
            command=self._confirm_clear,
        )
        clear_btn.pack(side="left")
        ToolTip(clear_btn, "Delete all saved notes")

        # دکمه‌های Save و Cancel (سمت راست)
        self._add_ok_cancel(
            ok_text="💾 Save Note",
            cancel_text="Close",
            ok_command=self._save_note,
            ok_style="success",
        )

    # ══════════════════════════════════════════════════════════
    # Notes Logic
    # ══════════════════════════════════════════════════════════

    def _load_and_display(self) -> None:
        """یادداشت‌های قبلی را بارگذاری و نمایش می‌دهد."""
        content = _load_notes()
        self._all_notes_content = content
        self._display_notes(content)

    def _display_notes(self, content: str) -> None:
        """متن را در بخش previous نمایش می‌دهد."""
        self._prev_text.config(state="normal")
        self._prev_text.delete("1.0", tk.END)

        if not content.strip():
            self._prev_text.insert("1.0", "(No notes yet)\n", "timestamp")
            self._notes_count_lbl.config(text="")
        else:
            self._prev_text.insert("1.0", content)
            self._highlight_timestamps()

            # شمارش یادداشت‌ها
            count = content.count(_SEPARATOR)
            self._notes_count_lbl.config(
                text=f"{count} note{'s' if count != 1 else ''}"
            )

        self._prev_text.config(state="disabled")
        self._prev_text.see(tk.END)

    def _highlight_timestamps(self) -> None:
        """تاریخ‌ها را با رنگ فیروزه‌ای نشان می‌دهد."""
        content = self._prev_text.get("1.0", tk.END)
        lines   = content.split("\n")

        for i, line in enumerate(lines, 1):
            if line.startswith("[") and "]" in line:
                try:
                    start = f"{i}.0"
                    end   = f"{i}.{len(line)}"
                    self._prev_text.tag_add("timestamp", start, end)
                except Exception:
                    pass

    def _filter_notes(self) -> None:
        """یادداشت‌ها را بر اساس جستجو فیلتر می‌کند."""
        query   = self._search_var.get().strip().lower()
        content = getattr(self, "_all_notes_content", "")

        if not query:
            self._display_notes(content)
            return

        # فیلتر بر اساس بخش‌های جداشده
        blocks    = content.split(_SEPARATOR + "\n")
        matched   = [b for b in blocks if query in b.lower()]
        filtered  = (_SEPARATOR + "\n").join(matched)

        self._display_notes(filtered)

        # highlight متن جستجو
        self._prev_text.config(state="normal")
        start = "1.0"
        while True:
            pos = self._prev_text.search(
                query, start, tk.END, nocase=True
            )
            if not pos:
                break
            end = f"{pos}+{len(query)}c"
            self._prev_text.tag_add("highlight", pos, end)
            start = end
        self._prev_text.config(state="disabled")

    def _update_char_count(self) -> None:
        """شمارنده کاراکتر را به‌روز می‌کند."""
        count = len(self._note_text.get("1.0", "end-1c"))
        color = T.TEXT_MUTED if count < 500 else (T.WARNING if count < 1000 else T.DANGER)
        self._char_count_lbl.config(text=f"{count} chars", fg=color)

    def _save_note(self) -> None:
        """یادداشت را ذخیره می‌کند."""
        content = self._note_text.get("1.0", "end-1c").strip()

        if not content:
            self.show_warning("Empty Note", "Please write something before saving.")
            return

        try:
            _append_note(content)
            self.show_info("✅ Saved", "Note saved successfully.")
            self.close()
        except OSError as exc:
            self.show_error("Save Failed", f"Could not save note:\n{exc}")

    def _confirm_clear(self) -> None:
        """تأیید حذف همه یادداشت‌ها."""
        content = _load_notes()
        if not content.strip():
            self.show_info("No Notes", "There are no notes to clear.")
            return

        if self.ask_confirm(
            "Clear All Notes",
            "Delete ALL saved notes permanently?"
        ):
            try:
                _clear_notes()
                self._all_notes_content = ""
                self._display_notes("")
                self.show_info("Cleared", "All notes have been deleted.")
            except OSError as exc:
                self.show_error("Clear Failed", f"Could not clear notes:\n{exc}")

    # ══════════════════════════════════════════════════════════
    # Hooks
    # ══════════════════════════════════════════════════════════

    def on_close(self) -> bool:
        """بررسی یادداشت ذخیره‌نشده قبل از بستن."""
        content = self._note_text.get("1.0", "end-1c").strip()

        # اگر متن تایپ‌شده فقط اطلاعات ارز است، نیازی به هشدار نیست
        if not content or content == self._selected_coin.strip():
            return True

        return self.ask_confirm(
            "Unsaved Note",
            "You have an unsaved note. Close without saving?",
        )