"""
Phase-1 Production Hardening unit tests.

Covers:
- Token Bucket rate limiter
- RetryPolicy / exponential backoff
- SQLite Database (WAL)
- IdempotencyGuard
- Watchdog heartbeats
- Structured logger helpers
"""
from __future__ import annotations

import os
import tempfile
import time
import threading
from pathlib import Path

import pytest

from trading.rate_limiter import NobitexRateLimiter, TokenBucket
from trading.retry_policy import RetryPolicy, RetryConfig
from trading.exceptions import (
    RateLimitError,
    NetworkExchangeError,
    AuthenticationError,
    ExchangeClientError,
)
from trading.idempotency import IdempotencyGuard
from trading.watchdog import Watchdog, HealthStatus
from core.database import Database
from core.logger import (
    get_logger,
    set_log_context,
    clear_log_context,
    get_log_context,
    setup_structured_logging,
)


# ── Rate Limiter ────────────────────────────────────────────────────────────

def test_token_bucket_acquire_and_refill():
    bucket = TokenBucket(capacity=2.0, refill_rate=10.0, category="test")
    assert bucket.try_acquire(1.0) is True
    assert bucket.try_acquire(1.0) is True
    assert bucket.try_acquire(1.0) is False  # empty
    time.sleep(0.15)  # allow refill
    assert bucket.try_acquire(1.0) is True


def test_nobitex_rate_limiter_categorization():
    rl = NobitexRateLimiter()
    assert rl.categorize_endpoint("/market/stats") == "public"
    assert rl.categorize_endpoint("/users/wallets/list") == "private_read"
    assert rl.categorize_endpoint("/market/orders/add") == "private_trade"
    assert rl.acquire("/market/stats", timeout=2.0) is True


def test_rate_limiter_diagnostics():
    rl = NobitexRateLimiter()
    rl.acquire("/market/stats")
    diag = rl.get_diagnostics()
    assert "public" in diag
    assert diag["public"]["total_requests"] >= 1


# ── Retry Policy ────────────────────────────────────────────────────────────

def test_retry_policy_does_not_retry_auth():
    policy = RetryPolicy(RetryConfig(max_attempts=3))
    assert policy.should_retry(AuthenticationError("fail"), attempt=0) is False
    assert policy.should_retry(ExchangeClientError("bad request"), attempt=0) is False


def test_retry_policy_retries_network_when_idempotent():
    policy = RetryPolicy(RetryConfig(max_attempts=4))
    assert policy.should_retry(NetworkExchangeError("timeout"), attempt=0, is_idempotent=True) is True
    assert policy.should_retry(NetworkExchangeError("timeout"), attempt=0, is_idempotent=False) is False


def test_retry_policy_execute_succeeds_after_transient():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise NetworkExchangeError("transient")
        return "ok"

    policy = RetryPolicy(RetryConfig(max_attempts=4, initial_delay=0.01, max_delay=0.05))
    result = policy.execute(flaky, is_idempotent=True)
    assert result == "ok"
    assert calls["n"] == 3


def test_retry_delay_has_jitter():
    policy = RetryPolicy(RetryConfig(initial_delay=1.0, backoff_factor=2.0, jitter_pct=0.5))
    delays = [policy.compute_delay(0) for _ in range(10)]
    assert min(delays) < 1.0 or max(delays) > 1.0  # some jitter present


# ── Database ────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "test.db"
    return Database(str(db_path))


def test_database_prepare_and_update_order(tmp_db):
    oid = tmp_db.prepare_order(
        client_order_id="cid-1",
        symbol="BTCIRT",
        side="buy",
        order_type="market",
        amount=0.01,
    )
    assert oid == "cid-1"
    order = tmp_db.get_order("cid-1")
    assert order is not None
    assert order["status"] == "prepared"

    tmp_db.update_order_status("cid-1", status="submitted", filled_amount=0.01)
    order = tmp_db.get_order("cid-1")
    assert order["status"] == "submitted"
    assert float(order["filled_amount"]) == 0.01


def test_database_record_trade_and_state(tmp_db):
    tid = tmp_db.record_trade("BTCIRT", "buy", 1_000_000.0, 0.01, mode="paper")
    assert tid >= 1
    trades = tmp_db.get_recent_trades(limit=5)
    assert len(trades) >= 1

    tmp_db.set_state("last_scan", {"ts": 123})
    assert tmp_db.get_state("last_scan")["ts"] == 123


# ── Idempotency ─────────────────────────────────────────────────────────────

def test_idempotency_blocks_duplicate(tmp_path):
    db = Database(str(tmp_path / "idemp.db"))
    guard = IdempotencyGuard(db)
    cid = guard.prepare("BTCIRT", "buy", "market", 0.01)
    assert guard.is_duplicate(cid) is True
    with pytest.raises(RuntimeError, match="Duplicate"):
        guard.prepare("BTCIRT", "buy", "market", 0.01, client_order_id=cid)


def test_idempotency_confirm_and_fail(tmp_path):
    db = Database(str(tmp_path / "idemp2.db"))
    guard = IdempotencyGuard(db)
    cid = guard.prepare("ETHIRT", "sell", "limit", 1.0, price=50_000_000)
    guard.confirm(cid, status="submitted")
    order = db.get_order(cid)
    assert order["status"] == "submitted"

    cid2 = guard.prepare("ETHIRT", "buy", "market", 0.5)
    guard.mark_failed(cid2, "InsufficientBalance")
    order2 = db.get_order(cid2)
    assert order2["status"] == "failed"


# ── Watchdog ────────────────────────────────────────────────────────────────

def test_watchdog_heartbeat_and_status():
    wd = Watchdog(check_interval=0.1)
    wd.register("scanner", max_silence_seconds=0.3)
    wd.heartbeat("scanner")
    assert wd.status == HealthStatus.HEALTHY
    diag = wd.get_diagnostics()
    assert "scanner" in diag
    assert diag["scanner"]["status"] == "healthy"


def test_watchdog_detects_silence():
    wd = Watchdog(check_interval=0.05)
    wd.register("worker", max_silence_seconds=0.1)
    wd.heartbeat("worker")
    wd.start()
    time.sleep(0.4)
    wd.stop()
    # After silence it should be degraded or critical
    assert wd.get_diagnostics()["worker"]["status"] in ("degraded", "critical")


# ── Logger ──────────────────────────────────────────────────────────────────

def test_log_context_thread_local():
    clear_log_context()
    set_log_context(symbol="BTCIRT", mode="paper")
    ctx = get_log_context()
    assert ctx["symbol"] == "BTCIRT"
    clear_log_context()
    assert get_log_context() == {}


def test_setup_structured_logging_smoke(tmp_path):
    log_file = tmp_path / "test.log"
    logger = setup_structured_logging(debug=True, log_file=str(log_file))
    logger.info("phase1 smoke test")
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "phase1 smoke test" in content
