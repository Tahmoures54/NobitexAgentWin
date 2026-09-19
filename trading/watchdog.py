"""
trading/watchdog.py — Background thread health monitor.

Threads (scanner, executor, GUI) send periodic heartbeats.
If a heartbeat is missed beyond the allowed window the system
enters 'degraded' mode and logs a critical warning.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


@dataclass
class MonitoredComponent:
    name: str
    max_silence_seconds: float
    last_heartbeat: float = field(default_factory=time.monotonic)
    status: HealthStatus = HealthStatus.HEALTHY
    miss_count: int = 0


class Watchdog:
    """Central health monitor for background threads."""

    def __init__(
        self,
        check_interval: float = 5.0,
        on_degraded: Optional[Callable[[str, HealthStatus], None]] = None,
    ) -> None:
        self.check_interval = check_interval
        self.on_degraded = on_degraded
        self._components: Dict[str, MonitoredComponent] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._global_status = HealthStatus.HEALTHY

    def register(self, name: str, max_silence_seconds: float = 30.0) -> None:
        """Register a component to be monitored."""
        with self._lock:
            self._components[name] = MonitoredComponent(
                name=name,
                max_silence_seconds=max_silence_seconds,
            )
        logger.debug("Watchdog: registered component '%s' (max_silence=%.1fs)", name, max_silence_seconds)

    def heartbeat(self, name: str) -> None:
        """Call from the monitored thread to signal liveness."""
        with self._lock:
            comp = self._components.get(name)
            if comp is None:
                return
            comp.last_heartbeat = time.monotonic()
            if comp.status != HealthStatus.HEALTHY:
                logger.info("Watchdog: component '%s' recovered", name)
                comp.status = HealthStatus.HEALTHY
                comp.miss_count = 0

    def start(self) -> None:
        """Start the background monitoring loop."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="Watchdog",
            daemon=True,
        )
        self._thread.start()
        logger.info("Watchdog started (interval=%.1fs)", self.check_interval)

    def stop(self) -> None:
        """Stop the monitoring loop."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.check_interval + 1)
        logger.info("Watchdog stopped")

    def _loop(self) -> None:
        while not self._stop_event.wait(self.check_interval):
            self._check_all()

    def _check_all(self) -> None:
        now = time.monotonic()
        worst = HealthStatus.HEALTHY

        with self._lock:
            for name, comp in self._components.items():
                silence = now - comp.last_heartbeat
                if silence > comp.max_silence_seconds:
                    comp.miss_count += 1
                    if silence > comp.max_silence_seconds * 3:
                        new_status = HealthStatus.CRITICAL
                    else:
                        new_status = HealthStatus.DEGRADED

                    if new_status != comp.status:
                        logger.critical(
                            "Watchdog: component '%s' is %s (silent for %.1fs, misses=%d)",
                            name, new_status.value, silence, comp.miss_count,
                        )
                        if self.on_degraded:
                            try:
                                self.on_degraded(name, new_status)
                            except Exception as exc:
                                logger.error("on_degraded callback failed: %s", exc)

                    comp.status = new_status

                if comp.status == HealthStatus.CRITICAL:
                    worst = HealthStatus.CRITICAL
                elif comp.status == HealthStatus.DEGRADED and worst != HealthStatus.CRITICAL:
                    worst = HealthStatus.DEGRADED

            self._global_status = worst

    @property
    def status(self) -> HealthStatus:
        return self._global_status

    def get_diagnostics(self) -> Dict[str, Dict]:
        now = time.monotonic()
        with self._lock:
            return {
                name: {
                    "status": comp.status.value,
                    "last_heartbeat_ago": round(now - comp.last_heartbeat, 2),
                    "max_silence": comp.max_silence_seconds,
                    "miss_count": comp.miss_count,
                }
                for name, comp in self._components.items()
            }


# Convenience global
_watchdog: Optional[Watchdog] = None
_watchdog_lock = threading.Lock()


def get_watchdog() -> Watchdog:
    global _watchdog
    with _watchdog_lock:
        if _watchdog is None:
            _watchdog = Watchdog()
        return _watchdog
