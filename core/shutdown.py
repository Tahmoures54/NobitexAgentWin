"""
core/shutdown.py — Graceful application shutdown manager.

Handles SIGINT / SIGTERM and executes registered cleanup hooks in priority order:
1. Stop accepting new orders
2. Cancel open limit orders (optional)
3. Flush database / WAL checkpoint
4. Close network sessions
"""
from __future__ import annotations

import atexit
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)


@dataclass(order=True)
class ShutdownHook:
    priority: int
    name: str
    callback: Callable[[], None] = field(compare=False)
    timeout: float = field(default=5.0, compare=False)


class ShutdownManager:
    """Singleton-style graceful shutdown coordinator."""

    def __init__(self) -> None:
        self._hooks: List[ShutdownHook] = []
        self._lock = threading.Lock()
        self._shutting_down = False
        self._original_sigint = None
        self._original_sigterm = None

    @property
    def is_shutting_down(self) -> bool:
        return self._shutting_down

    def register(
        self,
        name: str,
        callback: Callable[[], None],
        *,
        priority: int = 100,
        timeout: float = 5.0,
    ) -> None:
        """Register a cleanup hook. Lower priority number runs first."""
        with self._lock:
            self._hooks.append(ShutdownHook(priority=priority, name=name, callback=callback, timeout=timeout))
            self._hooks.sort()
        logger.debug("Shutdown hook registered: %s (priority=%d)", name, priority)

    def install_handlers(self) -> None:
        """Install signal handlers for SIGINT and SIGTERM."""
        self._original_sigint = signal.getsignal(signal.SIGINT)
        self._original_sigterm = signal.getsignal(signal.SIGTERM)

        def _handler(signum: int, frame: object) -> None:
            sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
            logger.info("Received %s — initiating graceful shutdown", sig_name)
            self.shutdown(reason=sig_name)

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
            logger.debug("Shutdown signal handlers installed")
        except ValueError:
            # Not in main thread
            logger.warning("Could not install signal handlers (not main thread)")

        atexit.register(self._atexit_handler)

    def _atexit_handler(self) -> None:
        if not self._shutting_down:
            self.shutdown(reason="atexit")

    def shutdown(self, reason: str = "manual") -> None:
        """Execute all hooks in priority order and exit."""
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True

        logger.info("Graceful shutdown started (reason=%s)", reason)
        start = time.monotonic()

        for hook in list(self._hooks):
            logger.info("Running shutdown hook: %s", hook.name)
            try:
                # Run with a soft timeout using a thread
                done = threading.Event()
                error: List[BaseException] = []

                def _run() -> None:
                    try:
                        hook.callback()
                    except BaseException as exc:
                        error.append(exc)
                    finally:
                        done.set()

                t = threading.Thread(target=_run, name=f"shutdown-{hook.name}", daemon=True)
                t.start()
                if not done.wait(timeout=hook.timeout):
                    logger.error("Shutdown hook '%s' timed out after %.1fs", hook.name, hook.timeout)
                elif error:
                    logger.error("Shutdown hook '%s' failed: %s", hook.name, error[0])
                else:
                    logger.debug("Shutdown hook '%s' completed", hook.name)
            except Exception as exc:
                logger.error("Unexpected error in shutdown hook '%s': %s", hook.name, exc)

        elapsed = time.monotonic() - start
        logger.info("Graceful shutdown finished in %.2fs", elapsed)

    def request_shutdown(self, reason: str = "requested") -> None:
        """Public API for components to request shutdown."""
        self.shutdown(reason=reason)


# Global instance
_manager: Optional[ShutdownManager] = None
_manager_lock = threading.Lock()


def get_shutdown_manager() -> ShutdownManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ShutdownManager()
        return _manager
