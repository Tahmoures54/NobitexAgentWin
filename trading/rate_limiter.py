"""
trading/rate_limiter.py — Client-side Token Bucket Rate Limiter for Nobitex API.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

@dataclass
class BucketStats:
    category: str
    capacity: float
    refill_rate: float
    current_tokens: float
    total_acquired: int = 0
    total_delayed: int = 0
    total_wait_seconds: float = 0.0

class TokenBucket:
    """Thread-safe Token Bucket implementation."""

    def __init__(self, capacity: float, refill_rate: float, category: str = "default") -> None:
        self.capacity = float(capacity)
        self.refill_rate = float(refill_rate)
        self.category = category
        self.tokens = float(capacity)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._total_acquired = 0
        self._total_delayed = 0
        self._total_wait_seconds = 0.0

    def _refill(self, now: float) -> None:
        elapsed = now - self.last_refill
        if elapsed > 0:
            added = elapsed * self.refill_rate
            self.tokens = min(self.capacity, self.tokens + added)
            self.last_refill = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            if self.tokens >= tokens:
                self.tokens -= tokens
                self._total_acquired += 1
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        start_time = time.monotonic()
        deadline = (start_time + timeout) if timeout is not None else None

        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    self._total_acquired += 1
                    wait_time = now - start_time
                    if wait_time > 0.01:
                        self._total_delayed += 1
                        self._total_wait_seconds += wait_time
                    return True

                needed = tokens - self.tokens
                sleep_sec = needed / self.refill_rate

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Rate limiter timeout for category '%s' (waited %.2fs)",
                        self.category, time.monotonic() - start_time,
                    )
                    return False
                sleep_sec = min(sleep_sec, remaining)

            time.sleep(min(max(0.01, sleep_sec), 0.5))

    def get_stats(self) -> BucketStats:
        with self._lock:
            now = time.monotonic()
            self._refill(now)
            return BucketStats(
                category=self.category,
                capacity=self.capacity,
                refill_rate=self.refill_rate,
                current_tokens=round(self.tokens, 2),
                total_acquired=self._total_acquired,
                total_delayed=self._total_delayed,
                total_wait_seconds=round(self._total_wait_seconds, 3),
            )

class NobitexRateLimiter:
    """Centralized multi-bucket rate limiter for Nobitex endpoints."""

    def __init__(
        self,
        public_rate: float = 2.0,        # 120 req/min
        public_capacity: float = 10.0,
        private_read_rate: float = 1.0,  # 60 req/min
        private_read_capacity: float = 6.0,
        private_trade_rate: float = 0.5, # 30 req/min
        private_trade_capacity: float = 3.0,
    ) -> None:
        self.buckets: Dict[str, TokenBucket] = {
            "public": TokenBucket(public_capacity, public_rate, "public"),
            "private_read": TokenBucket(private_read_capacity, private_read_rate, "private_read"),
            "private_trade": TokenBucket(private_trade_capacity, private_trade_rate, "private_trade"),
        }

    def categorize_endpoint(self, path: str, method: str = "GET") -> str:
        clean_path = path.split("?")[0].strip().lower()
        if any(clean_path.startswith(p) for p in ("/market/orders/add", "/market/orders/update-status")):
            return "private_trade"
        if any(clean_path.startswith(p) for p in ("/users/wallets", "/market/orders/list", "/market/orders/status")):
            return "private_read"
        return "public"

    def acquire(self, path: str, method: str = "GET", timeout: Optional[float] = 15.0) -> bool:
        category = self.categorize_endpoint(path, method)
        bucket = self.buckets.get(category, self.buckets["public"])
        return bucket.acquire(1.0, timeout=timeout)

    def get_diagnostics(self) -> Dict[str, Any]:
        return {
            category: {
                "capacity": stats.capacity,
                "refill_rate_sec": stats.refill_rate,
                "available_tokens": stats.current_tokens,
                "total_requests": stats.total_acquired,
                "throttled_requests": stats.total_delayed,
                "total_wait_sec": stats.total_wait_seconds,
            }
            for category, bucket in self.buckets.items()
            for stats in [bucket.get_stats()]
        }
