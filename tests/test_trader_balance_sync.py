"""
Tests for TradingBot balance-state mirroring (v6.4.0 fix).

Bugs being covered:
1. `_sync_exchange_state()` only mirrored `authentication_status` and
   `live_trading_available`.  If the exchange client had already probed
   the balance successfully during its own init, TradingBot.start()
   still saw `balance_status == UNKNOWN` and refused to enable
   execution.

2. `_extract_asset_amount()` collapsed None (lookup failed) and 0.0
   (asset absent from a valid response) into the same value.  This
   could produce false phantom entries or ignore real holdings.

3. `_is_auth_error()` was too permissive: a generic 4xx with a text
   hint like "invalid" was misclassified as an auth failure.

4. `get_balance_fresh()` silently fell back to a stale cached
   `get_balance()` value when the exchange's dedicated fresh API was
   callable but returned None.  It now propagates None so callers can
   distinguish "unknown" from "zero".
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from trading.trader import (
    TradingBot,
    AUTH_UNKNOWN,
    AUTHENTICATED,
    BALANCE_UNKNOWN,
    BALANCE_AVAILABLE,
    BALANCE_UNAVAILABLE,
    _is_auth_error,
    _is_authorization_error,
    _is_client_error,
    _is_server_error,
    _is_rate_limit_error,
)


# ══════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════

class _FakeExchange:
    """Minimal exchange-shaped object for state mirroring tests."""

    def __init__(
        self,
        auth="AUTHENTICATED",
        live=True,
        balance_status="AVAILABLE",
        last_balance=5_000_000.0,
        last_balance_error=None,
        last_balance_timestamp=1_700_000_000.0,
    ):
        self.authentication_status = auth
        self.live_trading_available = live
        self.balance_status = balance_status
        self.last_balance = last_balance
        self.last_balance_error = last_balance_error
        self.last_balance_timestamp = last_balance_timestamp

    def get_balance(self, asset):
        return self.last_balance

    def get_balances(self, force_refresh=False):
        return {"IRT": self.last_balance}


def _make_bot_with_exchange(exchange):
    """Build a TradingBot shell without running __init__ (which would
    try to initialize a real Nobitex client).

    NOTE: `self.lock` must be set explicitly because the real
    `TradingBot.__init__` is skipped; `_record_successful_auth` and
    `_handle_balance_exception` both use `with self.lock`.
    """
    bot = TradingBot.__new__(TradingBot)
    bot.exchange = exchange
    bot.exchange_name = "nobitex"
    bot.quote_currency = "IRT"
    bot.nobitex_market = "IRT"

    # Required by most methods for thread safety.
    bot.lock = threading.RLock()

    # Defaults used by state-mirroring helpers.
    bot.authentication_status = AUTH_UNKNOWN
    bot.authentication_error = None
    bot.live_trading_available = False
    bot.balance_status = BALANCE_UNKNOWN
    bot.last_balance_error = None
    bot.last_balance_timestamp = None
    bot.current_balance = None
    bot.starting_balance = None
    bot.last_known_balance = None

    # Defaults used by retry / execution helpers.
    bot.running = False
    bot.closed = False
    bot.execution_enabled = False
    bot.last_error = None
    bot.last_error_timestamp = None
    bot.last_order = None
    bot.last_order_timestamp = None
    bot._balance_retry_after = 0.0
    bot._balance_retry_count = 0
    bot._max_balance_retry_count = 6
    bot._max_balance_retry_backoff = 60.0

    return bot


# ══════════════════════════════════════════════════════════════
# 1. _sync_exchange_state mirrors balance status
# ══════════════════════════════════════════════════════════════

def test_sync_mirrors_balance_status_and_value():
    ex = _FakeExchange(
        auth="AUTHENTICATED",
        live=True,
        balance_status="AVAILABLE",
        last_balance=7_500_000.0,
        last_balance_timestamp=1_700_000_123.0,
    )
    bot = _make_bot_with_exchange(ex)
    bot._sync_exchange_state()

    assert bot.authentication_status == "AUTHENTICATED"
    assert bot.live_trading_available is True
    assert bot.balance_status == "AVAILABLE"
    assert bot.current_balance == 7_500_000.0
    assert bot.last_known_balance == 7_500_000.0
    assert bot.last_balance_timestamp == 1_700_000_123.0


def test_sync_keeps_unknown_if_exchange_unknown():
    ex = _FakeExchange(
        auth="UNKNOWN",
        live=False,
        balance_status="UNKNOWN",
        last_balance=None,
        last_balance_timestamp=None,
    )
    bot = _make_bot_with_exchange(ex)
    bot._sync_exchange_state()

    assert bot.balance_status == "UNKNOWN"
    assert bot.current_balance is None
    assert bot.last_known_balance is None


def test_sync_with_null_balance_does_not_crash():
    """A missing last_balance attribute must not break mirroring."""
    ex = _FakeExchange()
    delattr(ex, "last_balance")
    delattr(ex, "last_balance_timestamp")
    bot = _make_bot_with_exchange(ex)
    bot._sync_exchange_state()
    # Balance status is still mirrored; the missing attrs are ignored.
    assert bot.balance_status == "AVAILABLE"


# ══════════════════════════════════════════════════════════════
# 2. _extract_asset_amount: None vs 0.0 vs value
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def bot_for_extract():
    bot = TradingBot.__new__(TradingBot)
    bot.quote_currency = "IRT"
    return bot


def test_extract_none_payload_returns_none(bot_for_extract):
    """The whole lookup failed — must NOT be treated as zero."""
    assert bot_for_extract._extract_asset_amount(None, "IRT") is None


def test_extract_non_dict_payload_returns_none(bot_for_extract):
    assert bot_for_extract._extract_asset_amount(["not", "a", "dict"], "IRT") is None


def test_extract_empty_dict_returns_zero(bot_for_extract):
    """A valid but empty payload means the asset really is absent."""
    assert bot_for_extract._extract_asset_amount({}, "IRT") == 0.0


def test_extract_present_key_returns_value(bot_for_extract):
    assert bot_for_extract._extract_asset_amount({"IRT": "123.5"}, "IRT") == 123.5
    assert bot_for_extract._extract_asset_amount({"IRT": 42}, "IRT") == 42.0


def test_extract_absent_key_from_valid_payload_returns_zero(bot_for_extract):
    """Dict is non-empty but does not contain the asset → 0.0."""
    assert bot_for_extract._extract_asset_amount({"BTC": 0.5}, "IRT") == 0.0


def test_extract_quote_aliases(bot_for_extract):
    """IRT / RLS / IRR are interchangeable for the quote side."""
    assert bot_for_extract._extract_asset_amount({"RLS": 100.0}, "IRT") == 100.0
    assert bot_for_extract._extract_asset_amount({"IRT": 200.0}, "RLS") == 200.0


# ══════════════════════════════════════════════════════════════
# 3. Error classification is strict
# ══════════════════════════════════════════════════════════════

class _FakeAuthError(Exception):
    """Simulates AuthenticationError by class name."""
    pass
_FakeAuthError.__name__ = "AuthenticationError"


class _FakeOvervalueError(Exception):
    """Simulates the v6.5 ExchangeClientError from nobitex_client."""
    pass
_FakeOvervalueError.__name__ = "ExchangeClientError"


def test_is_auth_error_true_for_401():
    exc = Exception("HTTP 401 Unauthorized")
    exc.status_code = 401
    assert _is_auth_error(exc) is True


def test_is_auth_error_true_for_named_class():
    assert _is_auth_error(_FakeAuthError("whatever")) is True


def test_is_auth_error_false_for_overvalue():
    """A generic 4xx must not be treated as an auth failure."""
    exc = Exception("Nobitex HTTP 400: OverValueOrder order value too high")
    exc.status_code = 400
    assert _is_auth_error(exc) is False
    assert _is_client_error(exc) is True


def test_is_auth_error_false_for_insufficient_balance():
    exc = Exception("Nobitex HTTP 400: InsufficientBalance not enough balance")
    exc.status_code = 400
    assert _is_auth_error(exc) is False
    assert _is_client_error(exc) is True


def test_is_authorization_error_true_for_403():
    exc = Exception("HTTP 403 Forbidden")
    exc.status_code = 403
    assert _is_authorization_error(exc) is True
    assert _is_auth_error(exc) is False


def test_is_rate_limit_error_true_for_429():
    exc = Exception("HTTP 429 TooManyRequests")
    exc.status_code = 429
    assert _is_rate_limit_error(exc) is True
    assert _is_client_error(exc) is False


def test_is_server_error_true_for_5xx():
    exc = Exception("HTTP 503 Service Unavailable")
    exc.status_code = 503
    assert _is_server_error(exc) is True


# ══════════════════════════════════════════════════════════════
# 4. get_balance_fresh propagates None when fresh read fails
# ══════════════════════════════════════════════════════════════

def test_get_balance_fresh_propagates_none_on_failed_fresh():
    """If the exchange's fresh read returns None, do NOT fall back to
    a stale cached value silently."""

    class _FailingExchange:
        authentication_status = "AUTHENTICATED"
        live_trading_available = True
        balance_status = "AVAILABLE"
        last_balance = 5_000_000.0
        last_balance_error = None
        last_balance_timestamp = 0.0

        def invalidate_balance_cache(self):
            pass

        def get_balance_fresh(self, asset):
            return None     # ← deliberately unavailable

        def get_balances(self, force_refresh=False):
            return None     # ← also unavailable

        def get_balance(self, asset):
            # This is the STALE path — must NOT be used silently when
            # the exchange implements get_balance_fresh.
            return 5_000_000.0

    bot = _make_bot_with_exchange(_FailingExchange())

    # The contract is strict: fresh read was implemented and returned
    # None → propagate None, do not silently substitute the stale value.
    result = bot.get_balance_fresh("IRT")
    assert result is None, (
        f"expected None when the exchange's fresh read returns None, "
        f"got {result!r}"
    )


def test_get_balance_fresh_returns_value_on_success():
    """Sanity check: a real fresh read returns the value."""

    class _HealthyExchange:
        authentication_status = "AUTHENTICATED"
        live_trading_available = True
        balance_status = "AVAILABLE"
        last_balance = 5_000_000.0
        last_balance_error = None
        last_balance_timestamp = 0.0

        def invalidate_balance_cache(self):
            pass

        def get_balance_fresh(self, asset):
            return 1_234_567.89

    bot = _make_bot_with_exchange(_HealthyExchange())
    result = bot.get_balance_fresh("IRT")
    assert result == 1_234_567.89
    # After a successful read, auth/balance state must be updated.
    assert bot.balance_status == "AVAILABLE"
    assert bot.current_balance == 1_234_567.89


def test_get_balance_fresh_uses_balances_when_no_fresh_api():
    """When the exchange has no get_balance_fresh, fall back to
    get_balances(force_refresh=True)."""

    class _LegacyExchange:
        authentication_status = "AUTHENTICATED"
        live_trading_available = True
        balance_status = "AVAILABLE"
        last_balance = 5_000_000.0
        last_balance_error = None
        last_balance_timestamp = 0.0

        def invalidate_balance_cache(self):
            pass

        # NOTE: no get_balance_fresh attribute.

        def get_balances(self, force_refresh=False):
            assert force_refresh is True, "must force a refresh"
            return {"IRT": 777_000.0}

        def get_balance(self, asset):
            return 5_000_000.0  # stale

    bot = _make_bot_with_exchange(_LegacyExchange())
    result = bot.get_balance_fresh("IRT")
    # Should use get_balances value, not the stale get_balance.
    assert result == 777_000.0