"""Nobitex-only trading package + Phase-1 hardening layers."""
from .nobitex_client import NobitexClient
from .execution_mode import LIVE, PAPER, normalize_execution_mode, cycle_plan
from .bot_config import apply_to_tracker

__all__ = [
    "NobitexClient",
    "LIVE",
    "PAPER",
    "normalize_execution_mode",
    "cycle_plan",
    "apply_to_tracker",
    # Phase-1 hardening
    "NobitexRateLimiter",
    "RetryPolicy",
    "RetryConfig",
    "IdempotencyGuard",
    "Watchdog",
    "get_watchdog",
]

try:
    from .rate_limiter import NobitexRateLimiter
    from .retry_policy import RetryPolicy, RetryConfig
    from .idempotency import IdempotencyGuard
    from .watchdog import Watchdog, get_watchdog
except ImportError:
    pass
