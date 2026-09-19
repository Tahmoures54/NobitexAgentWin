"""
trading/scan_journal.py — Append-only JSONL journal of scan decisions.

Stores enough fields to rebuild expectancy analysis later:
timestamp, symbol, bid/ask, volume, regime, accept/reject reason,
confidence, estimated cost, edge ratio.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join("data", "scan_journal.jsonl")


class ScanJournal:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or _DEFAULT_PATH
        self._lock = threading.Lock()
        parent = os.path.dirname(os.path.abspath(self.path))
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)

    def record(self, event: Dict[str, Any]) -> None:
        row = dict(event)
        row.setdefault("ts", time.time())
        line = json.dumps(row, ensure_ascii=False, default=str)
        try:
            with self._lock:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError as exc:
            logger.warning("Scan journal write failed: %s", exc)
