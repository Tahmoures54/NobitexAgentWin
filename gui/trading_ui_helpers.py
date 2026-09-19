# gui/trading_ui_helpers.py
"""
Shared helper functions and classes for trading-related UI panels.

Contains:
- Treeview creation with scrollbar
- Styled button creation
- Price/PnL formatting utilities
- Safe conversion functions
- Autocomplete combobox for exchange selection
- Common column definitions for trade tables
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple, Union

import tkinter as tk
from tkinter import ttk

from gui.ui_theme import Theme

logger = logging.getLogger(__name__)
T = Theme

# ─────────────────────────────────────────────────────────────────────────────
# Column constants (used by paper trading panel)
# ─────────────────────────────────────────────────────────────────────────────
LIVE_COLS = (
    "Symbol", "Status", "Signal", "Entry",
    "Exit / Cur", "PnL %", "PnL $", "Reason", "Entry Time"
)
LIVE_WIDTHS = {
    "Symbol": 80, "Status": 70, "Signal": 100, "Entry": 90,
    "Exit / Cur": 90, "PnL %": 70, "PnL $": 75, "Reason": 90, "Entry Time": 140,
}

# ─────────────────────────────────────────────────────────────────────────────
# Safe conversion and formatting utilities
# ─────────────────────────────────────────────────────────────────────────────
def safe_pnl(value: Any) -> Optional[float]:
    """
    Convert value to float safely, returning None on failure.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

def safe_stat(stats: Dict[str, Any], key: str, default: float = 0.0) -> float:
    """
    Safely extract a numeric statistic from a dictionary.
    """
    val = stats.get(key, default)
    numeric = safe_pnl(val)
    return numeric if numeric is not None else default

def fmt_price(value: Any, decimals: int = 4) -> str:
    """
    Format a price value as a string with dollar sign.
    """
    if value is None:
        return "—"
    try:
        return f"${float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "—"

def fmt_pnl(value: Any) -> str:
    """
    Format a PnL percentage value.
    """
    pnl = safe_pnl(value)
    if pnl is None:
        return "—"
    return f"{pnl:+.2f}%"

def fmt_dollar(value: Any) -> str:
    """
    Format a dollar amount value.
    """
    val = safe_pnl(value)
    if val is None:
        return "—"
    return f"${val:+.2f}"

# ─────────────────────────────────────────────────────────────────────────────
# UI component builders
# ─────────────────────────────────────────────────────────────────────────────
def make_tree(
    parent: tk.Widget,
    columns: Tuple[str, ...],
    widths: List[int],
    height: int = 10,
    return_frame: bool = False
) -> Union[ttk.Treeview, Tuple[tk.Frame, ttk.Treeview]]:
    """
    Create a Treeview with vertical scrollbar inside a frame.

    Args:
        parent: Parent widget.
        columns: Tuple of column identifiers (strings).
        widths: List of column widths in pixels (must match columns length).
        height: Number of visible rows.
        return_frame: If True, returns a tuple (frame, tree) instead of just tree.
                      Useful when the caller needs to manage layout manually.

    Returns:
        ttk.Treeview instance by default, or (frame, tree) if return_frame is True.
        The frame is packed into parent automatically only if return_frame is False.
    """
    frame = tk.Frame(parent, bg=T.BG_PANEL)
    if not return_frame:
        frame.pack(fill="both", expand=True)

    tree = ttk.Treeview(
        frame,
        columns=columns,
        show="headings",
        style="Treeview",
        height=height
    )
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=vsb.set)

    vsb.pack(side="right", fill="y")
    tree.pack(fill="both", expand=True)

    # Configure columns
    for col, w in zip(columns, widths):
        anchor = "w" if col in ("Symbol", "Time", "Metric", "Entry Time") else "center"
        tree.heading(col, text=col)
        tree.column(col, width=w, anchor=anchor, stretch=tk.NO)

    if return_frame:
        return frame, tree
    return tree

def primary_btn(
    parent: tk.Widget,
    text: str,
    command: Any,
    bg: str = T.PRIMARY,
    padx: int = T.PAD_LG,
    pady: int = T.PAD_SM
) -> tk.Button:
    """
    Create a styled button with hover effect.
    """
    btn = tk.Button(
        parent,
        text=text,
        font=T.font(size=T.FONT_SM, weight="bold"),
        bg=bg,
        fg=T.TEXT_ON_PRIMARY,
        activebackground=T.PRIMARY_DARK,
        activeforeground=T.TEXT_ON_PRIMARY,
        relief="flat",
        cursor="hand2",
        padx=padx,
        pady=pady,
        bd=0,
        command=command
    )

    def on_enter(_e: tk.Event) -> None:
        btn.config(bg=T.PRIMARY_DARK)

    def on_leave(_e: tk.Event) -> None:
        btn.config(bg=bg)

    btn.bind("<Enter>", on_enter)
    btn.bind("<Leave>", on_leave)
    return btn

# ─────────────────────────────────────────────────────────────────────────────
# Exchange autocomplete combobox
# ─────────────────────────────────────────────────────────────────────────────
class AutocompleteCombobox(ttk.Combobox):
    """
    Combobox with autocomplete and debounced filtering.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._choices: List[str] = []
        self._filter_after_id: Optional[str] = None

    def set_completion_list(self, choices: List[str]) -> None:
        """Set the list of possible choices and enable autocomplete."""
        self._choices = sorted(str(c) for c in choices)
        self["values"] = self._choices
        self.bind("<KeyRelease>", self._on_key)

    def _on_key(self, _event: Optional[tk.Event] = None) -> None:
        """Debounce key releases to avoid excessive filtering."""
        if self._filter_after_id:
            self.after_cancel(self._filter_after_id)
        self._filter_after_id = self.after(200, self._apply_filter)

    def _apply_filter(self) -> None:
        """Filter the dropdown list based on typed text (case-insensitive)."""
        typed = self.get().lower()
        if not typed:
            self["values"] = self._choices
            return

        filtered = [c for c in self._choices if typed in c.lower()]
        # If no matches, show an empty list instead of all choices
        self["values"] = filtered

@lru_cache(maxsize=1)
def get_nobitex_exchanges() -> List[str]:
    return ["nobitex"]
