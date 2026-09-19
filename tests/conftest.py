# tests/conftest.py
"""Pytest configuration and environment fixtures for CryptoScanner test suite.

Ensures that headless CI/Linux environments without X11 or python3-tk
can run unit tests safely by providing dummy Tkinter stubs if Tkinter
is not installed on the host system.
"""
from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

_TK_MODULES = [
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.filedialog",
    "tkinter.font",
    "tkinter.simpledialog",
    "tkinter.constants",
    "tkinter.colorchooser",
]

class _MockWidgetMeta(type):
    """Metaclass allowing class-level attribute access."""
    def __getattr__(cls, name: str) -> Any:
        if name in ("__all__", "__iter__"):
            raise AttributeError(name)
        return MagicMock()

    def __iter__(cls):
        return iter([])

class _MockWidget(metaclass=_MockWidgetMeta):
    """A mock widget class that supports subclassing, instantiation, and attribute access."""
    def __init__(self, *args, **kwargs) -> None:
        pass

    def __getattr__(self, name: str) -> Any:
        return MagicMock()

class _MockTkModule(types.ModuleType):
    """A module mock that returns mockable classes for any attribute."""
    def __getattr__(self, name: str) -> Any:
        if name in ("__path__", "__spec__", "__all__"):
            raise AttributeError(name)
        return _MockWidget

for _mod_name in _TK_MODULES:
    if _mod_name not in sys.modules:
        try:
            __import__(_mod_name)
        except ImportError:
            sys.modules[_mod_name] = _MockTkModule(_mod_name)
