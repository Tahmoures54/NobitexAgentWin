"""Small persistent store for raw Nobitex scan history.

The trend strategy needs the prices observed by *this* scanner, not a candle
indicator calculated from another feed.  ``ScanHistoryStore`` keeps a bounded
per-symbol list and writes it atomically as JSON.  It is optional: unit tests
and callers that do not pass a path remain in-memory only.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from analysis.raw_trend import price_from_scan


class ScanHistoryStore:
    """Bounded, thread-safe, atomic JSON persistence for scan prices."""

    def __init__(self, path: Optional[str] = None, *, max_scans: int = 120) -> None:
        self.path = str(path) if path else None
        self.max_scans = max(4, int(max_scans))
        self._lock = threading.RLock()
        self._data: Dict[str, List[Dict[str, float]]] = {}
        if self.path:
            self._load()

    @staticmethod
    def _symbol(value: Any) -> str:
        return str(value or "").strip().upper()

    def _load(self) -> None:
        try:
            raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
            symbols = raw.get("symbols", raw) if isinstance(raw, dict) else {}
            if not isinstance(symbols, dict):
                return
            loaded: Dict[str, List[Dict[str, float]]] = {}
            for symbol, values in symbols.items():
                key = self._symbol(symbol)
                if not key or not isinstance(values, list):
                    continue
                points = []
                for value in values[-self.max_scans :]:
                    if isinstance(value, dict):
                        price = price_from_scan(value)
                        timestamp = value.get("timestamp", 0.0)
                    else:
                        price = price_from_scan(value)
                        timestamp = 0.0
                    if price is not None:
                        try:
                            ts = float(timestamp or 0.0)
                        except (TypeError, ValueError):
                            ts = 0.0
                        points.append({"timestamp": ts, "price": float(price)})
                if points:
                    loaded[key] = points
            self._data = loaded
        except (OSError, ValueError, TypeError):
            # A corrupt/partial history must never prevent paper mode from
            # starting.  The next successful scan will replace it.
            self._data = {}

    def _save(self) -> None:
        if not self.path:
            return
        target = Path(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "symbols": self._data}
        tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, target)

    def record(self, symbol: str, price: Any, *, timestamp: Optional[float] = None) -> bool:
        key = self._symbol(symbol)
        parsed = price_from_scan(price)
        if not key or parsed is None:
            return False
        point = {"timestamp": float(time.time() if timestamp is None else timestamp), "price": float(parsed)}
        with self._lock:
            self._data.setdefault(key, []).append(point)
            self._data[key] = self._data[key][-self.max_scans :]
            self._save()
        return True

    def record_scan(self, rows: Iterable[Dict[str, Any]], *, timestamp: Optional[float] = None) -> int:
        now = time.time() if timestamp is None else float(timestamp)
        changed = 0
        with self._lock:
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                symbol = row.get("Symbol") or row.get("symbol")
                price = price_from_scan(row)
                key = self._symbol(symbol)
                if not key or price is None:
                    continue
                self._data.setdefault(key, []).append({"timestamp": now, "price": float(price)})
                self._data[key] = self._data[key][-self.max_scans :]
                changed += 1
            if changed:
                self._save()
        return changed

    def prices(self, symbol: str) -> List[float]:
        key = self._symbol(symbol)
        with self._lock:
            return [float(point["price"]) for point in self._data.get(key, [])]

    def points(self, symbol: str) -> List[Dict[str, float]]:
        key = self._symbol(symbol)
        with self._lock:
            return [dict(point) for point in self._data.get(key, [])]

    def snapshot(self) -> Dict[str, List[Dict[str, float]]]:
        with self._lock:
            return {symbol: [dict(point) for point in points] for symbol, points in self._data.items()}

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            if self.path:
                try:
                    Path(self.path).unlink(missing_ok=True)
                except OSError:
                    pass


__all__ = ["ScanHistoryStore"]
