"""
GUI Package — Advanced Crypto Scanner
======================================

Usage
-----
    # Start the application
    from gui import launch_app
    launch_app()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

__version__  = "2.1.0" # Version updated for refactor
__author__   = "Crypto Scanner Team"

# ── Always-safe imports (no Tk, no matplotlib) ────────────────────
from .gui_helpers import (
    ToolTip,
    center_window,
    ScrollableFrame,
    ThemeManager,
    LoadingOverlay,
    StatusBar,
    ConfirmDialog,
    KeyboardShortcuts,
    format_number,
    make_labeled_entry,
    make_labeled_combobox,
)

# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
# CORRECTED IMPORTS: All dialogs now come from the 'dialogs' package
# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
from .dialogs.settings_window import SettingsWindow
from .dialogs.premium_window import PremiumWindow
from .dialogs.note_window import NoteWindow
from .ui_theme import ModernTheme as WindowsTheme

# ── Submodule references ─────────────────────────────────────────
from . import gui_helpers
# ++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

# ── gui_main is NOT imported here — it calls matplotlib.use('TkAgg')
#    and imports heavy dependencies. Use launch_app() or import explicitly.
# ─────────────────────────────────────────────────────────────────────

if TYPE_CHECKING:
    # Only for type checkers — never executed at runtime
    from .gui_main import CryptoScannerApp


def launch_app() -> None:
    """
    Create the Tk root and launch CryptoScannerApp.
    """
    import tkinter as tk
    from .gui_main import CryptoScannerApp   # noqa: PLC0415

    root = tk.Tk()
    _app = CryptoScannerApp(root)
    root.mainloop()


# ── Public API ────────────────────────────────────────────────────
__all__ = [
    # Package metadata
    "__version__",

    # Launch helper
    "launch_app",

    # Submodule objects
    "gui_helpers",

    # gui_helpers exports
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

    # Dialogs exports (from the new structure)
    "SettingsWindow",
    "PremiumWindow",
    "NoteWindow",
    "WindowsTheme",
]