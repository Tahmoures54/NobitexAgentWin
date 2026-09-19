"""
trading/retry_policy.py — Centralized Error Handling & Retry with Exponential Backoff.
"""
from __future__ import annotations

import functools
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, Type, TypeVar

from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    ExchangeClientError,
    NetworkExchangeError,
    RateLimitError,
    ServerExchangeError,
)

logger = logging.getLogger(__name__)
T = TypeVar("T")

NON_RETRYABLE_EXCEPTIONS: Tuple[Type[Exception], ...] = (
    AuthenticationError,
    AuthorizationError,
    ExchangeClientError,
    ValueError,
    KeyError,
)

@dataclass
class RetryConfig:
    max_attempts: int = 4
    initial_delay: float = 0.5
    max_delay: float = 8.0
    backoff_factor: float = 2.0
    jitter_pct: float = 0.25
    retry_on_server_errors: bool = True
    retry_on_network_errors: bool = True
    retry_on_rate_limits: bool = True

class RetryPolicy:
    """Centralized retry evaluator and backoff scheduler."""

    def __init__(self, config: Optional[RetryConfig] = None) -> None:
        self.config = config or RetryConfig()

    def should_retry(
        self,
        exc: Exception,
        attempt: int,
        method: str = "GET",
        is_idempotent: bool = True,
    ) -> bool:
        if attempt >= self.config.max_attempts - 1:
            return False
        if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
            return False
        if exc.__class__.__name__ == "SymbolNotFoundError":
            return False
        if not is_idempotent and isinstance(exc, (NetworkExchangeError, ServerExchangeError)):
            logger.warning("Suppressed blind retry for mutating operation (%s): %s", method, exc)
            return False
        if isinstance(exc, RateLimitError) and self.config.retry_on_rate_limits:
            return True
        if isinstance(exc, ServerExchangeError) and self.config.retry_on_server_errors:
            return True
        if isinstance(exc, NetworkExchangeError) and self.config.retry_on_network_errors:
            return is_idempotent
        return False

    def compute_delay(self, attempt: int, exc: Optional[Exception] = None) -> float:
        if isinstance(exc, RateLimitError) and getattr(exc, "retry_after", None) is not None:
            try:
                suggested = float(exc.retry_after)
                if suggested > 0:
                    return min(suggested + 0.1, self.config.max_delay * 2)
            except (ValueError, TypeError):
                pass

        raw_delay = self.config.initial_delay * (self.config.backoff_factor ** attempt)
        capped_delay = min(raw_delay, self.config.max_delay)
        if self.config.jitter_pct > 0:
            jitter = (random.random() * 2 - 1) * self.config.jitter_pct * capped_delay
            delay = max(0.1, capped_delay + jitter)
        else:
            delay = capped_delay
        return round(delay, 3)

    def execute(
        self,
        func: Callable[..., T],
        *args: Any,
        is_idempotent: bool = True,
        method: str = "GET",
        operation_name: str = "api_call",
        **kwargs: Any,
    ) -> T:
        last_exception: Optional[Exception] = None
        for attempt in range(self.config.max_attempts):
            try:
                return func(*args, **kwargs)
            except Exception as exc:
                last_exception = exc
                if not self.should_retry(exc, attempt, method=method, is_idempotent=is_idempotent):
                    raise exc
                delay = self.compute_delay(attempt, exc)
                time.sleep(delay)
        if last_exception:
            raise last_exception
        raise NetworkExchangeError(f"Operation {operation_name} failed after retries.")

def with_retry(
    is_idempotent: bool = True,
    method: str = "GET",
    config: Optional[RetryConfig] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    policy = RetryPolicy(config)
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return policy.execute(
                func,
                *args,
                is_idempotent=is_idempotent,
                method=method,
                operation_name=func.__name__,
                **kwargs,
            )
        return wrapper
    return decorator
