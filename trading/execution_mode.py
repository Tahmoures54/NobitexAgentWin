"""Exclusive paper vs live execution. The two modes must never open together."""
from __future__ import annotations

from typing import Any, Dict

PAPER = "paper"
LIVE = "live"

_PAPER_ALIASES = frozenset({"paper", "sim", "simulator", "shadow", "demo"})
_LIVE_ALIASES = frozenset({"live", "real", "nobitex", "production"})


def normalize_execution_mode(value: Any, default: str = PAPER) -> str:
    text = str(value if value not in (None, "") else default or PAPER).strip().lower()
    if text in _LIVE_ALIASES:
        return LIVE
    if text in _PAPER_ALIASES:
        return PAPER
    return PAPER if str(default or PAPER).strip().lower() not in _LIVE_ALIASES else LIVE


def cycle_plan(mode: Any, live_entries_enabled: bool = False) -> Dict[str, bool]:
    """What one scan cycle is allowed to do.

    Paper and live entries are mutually exclusive. Live leftover positions
    are still monitored after switching back to paper.
    """
    normalized = normalize_execution_mode(mode)
    paper = normalized == PAPER
    live = normalized == LIVE
    live_entries = live and bool(live_entries_enabled)
    return {
        "mode": normalized,
        "evaluate_signals": paper or live_entries,
        "open_paper": paper,
        "open_live": live_entries,
        "monitor_live": True,
        "scan_tag": "[PAPER]" if paper else "[REAL]",
    }


def should_run_live_tracker(plan: Dict[str, Any], has_live_positions: bool) -> bool:
    """Call the live SignalTracker (and therefore Nobitex wallets) only when needed.

    Paper scans must not poll wallets. Leftover live positions are still
    managed after switching back to paper. Live Start still processes every
    scan so new entries can open.
    """
    if not isinstance(plan, dict):
        return bool(has_live_positions)
    if plan.get("open_live"):
        return True
    return bool(has_live_positions)
