"""
Tests for Nobitex client error taxonomy (v6.5.0 fix).

Bug being covered:
    Previously `_raise_api_error()` mapped EVERY 4xx response to
    `SymbolNotFoundError`.  That meant OverValueOrder, InsufficientBalance,
    InvalidAmount, ValidationFailed, etc. all looked like missing symbols.
    Consequences:
        - the symbol was added to the negative orderbook cache
          and never queried again
        - the caller never saw the real exchange message
        - TradingBot.place_order() returned "unsupported_market"

    v6.5.0 introduces `ExchangeClientError` for these cases and keeps
    `SymbolNotFoundError` ONLY for 404 / InvalidCurrency / known
    symbol-missing codes.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from trading.nobitex_client import (
    NobitexClient,
    SymbolNotFoundError,
    ExchangeClientError,
)


# ══════════════════════════════════════════════════════════════
# Fixture: a client with a fake session
# ══════════════════════════════════════════════════════════════

def _make_client() -> NobitexClient:
    # No credentials → public-only client.  That's enough for the
    # error-path tests below (they never touch a signed endpoint).
    return NobitexClient(api_key="", api_secret="", testnet=False, quote_currency="IRT")


class _Resp:
    def __init__(self, status_code: int, payload: dict, reason: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.reason = reason
        self.content = json.dumps(payload).encode()
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# ══════════════════════════════════════════════════════════════
# 1. SymbolNotFoundError — only for symbol-missing
# ══════════════════════════════════════════════════════════════

def test_404_raises_symbol_not_found():
    client = _make_client()
    client._session.get = lambda *a, **k: _Resp(404, {"status": "failed", "code": "NotFound"})

    with pytest.raises(SymbolNotFoundError):
        client._request("GET", "/v2/orderbook/FAKECOINIRT", signed=False)


def test_invalid_currency_raises_symbol_not_found():
    client = _make_client()
    client._session.get = lambda *a, **k: _Resp(
        400,
        {"status": "failed", "code": "InvalidCurrency", "message": "invalid currency"},
    )

    with pytest.raises(SymbolNotFoundError):
        client._request("GET", "/market/stats", signed=False)


def test_market_not_found_raises_symbol_not_found():
    client = _make_client()
    client._session.request = lambda *a, **k: _Resp(
        400,
        {"status": "failed", "code": "MarketNotFound", "message": "market not found"},
    )

    with pytest.raises(SymbolNotFoundError):
        client._request("POST", "/market/orders/add", body={}, signed=False)


# ══════════════════════════════════════════════════════════════
# 2. ExchangeClientError — for other 4xx
# ══════════════════════════════════════════════════════════════

def test_overvalue_order_raises_exchange_client_error():
    client = _make_client()
    client._session.request = lambda *a, **k: _Resp(
        400,
        {"status": "failed", "code": "OverValueOrder", "message": "order value too high"},
    )

    with pytest.raises(ExchangeClientError) as excinfo:
        client._request("POST", "/market/orders/add", body={}, signed=False)

    assert "OverValueOrder" in str(excinfo.value) or "too high" in str(excinfo.value)


def test_insufficient_balance_raises_exchange_client_error():
    client = _make_client()
    client._session.request = lambda *a, **k: _Resp(
        400,
        {"status": "failed", "code": "InsufficientBalance", "message": "not enough balance"},
    )

    with pytest.raises(ExchangeClientError):
        client._request("POST", "/market/orders/add", body={}, signed=False)


def test_invalid_amount_raises_exchange_client_error():
    client = _make_client()
    client._session.request = lambda *a, **k: _Resp(
        400,
        {"status": "failed", "code": "InvalidAmount", "message": "amount below minimum"},
    )

    with pytest.raises(ExchangeClientError):
        client._request("POST", "/market/orders/add", body={}, signed=False)


def test_generic_4xx_raises_exchange_client_error_not_symbol_not_found():
    client = _make_client()
    client._session.get = lambda *a, **k: _Resp(
        422,
        {"status": "failed", "code": "ValidationFailed", "message": "bad request"},
    )

    # MUST be ExchangeClientError, MUST NOT be SymbolNotFoundError
    with pytest.raises(ExchangeClientError):
        client._request("GET", "/market/stats", signed=False)

    # Extra assertion: ensure it is NOT the symbol-not-found class.
    try:
        client._request("GET", "/market/stats", signed=False)
    except SymbolNotFoundError:
        pytest.fail("generic 4xx was incorrectly mapped to SymbolNotFoundError")
    except ExchangeClientError:
        pass


# ══════════════════════════════════════════════════════════════
# 3. No negative-cache poisoning for generic 4xx
# ══════════════════════════════════════════════════════════════

def test_generic_4xx_does_not_poison_orderbook_cache():
    """A 400 on the orderbook endpoint must NOT blacklist the symbol."""
    client = _make_client()

    calls = {"n": 0}

    def fake_get(url, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            # First call: generic client error
            return _Resp(400, {"status": "failed", "code": "OverValueOrder"})
        # Second call: valid empty book
        return _Resp(200, {"status": "ok", "bids": [], "asks": []})

    client._session.get = fake_get

    # First call — generic 4xx, empty book, must not cache as unsupported
    book1 = client.get_order_book("BTCIRT")
    assert book1 == {"bids": [], "asks": []}
    assert client._orderbook_cache.get("BTCIRT") is None, \
        "generic 4xx must not poison the negative cache"

    # Second call — same symbol, should attempt a real request again
    book2 = client.get_order_book("BTCIRT")
    assert calls["n"] == 2, "second call should reach the exchange"
    assert book2 == {"bids": [], "asks": []}
    assert client._orderbook_cache.get("BTCIRT") is True


def test_symbol_not_found_does_poison_orderbook_cache():
    """A real 404 SHOULD be cached so we don't hammer the endpoint."""
    client = _make_client()
    client._session.get = lambda *a, **k: _Resp(
        404, {"status": "failed", "code": "NotFound"}
    )

    book = client.get_order_book("GHOSTCOINIRT")
    assert book == {"bids": [], "asks": []}
    assert client._orderbook_cache.get("GHOSTCOINIRT") is False, \
        "404 must poison the negative cache"


# ══════════════════════════════════════════════════════════════
# 4. is_symbol_supported reacts correctly
# ══════════════════════════════════════════════════════════════

def test_is_symbol_supported_returns_false_on_invalid_currency():
    client = _make_client()
    client._session.get = lambda *a, **k: _Resp(
        400, {"status": "failed", "code": "InvalidCurrency"}
    )
    assert client.is_symbol_supported("GHOSTIRT") is False


def test_is_symbol_supported_returns_false_on_generic_4xx():
    """A generic 4xx must not raise; safe `False` is returned."""
    client = _make_client()
    client._session.get = lambda *a, **k: _Resp(
        400, {"status": "failed", "code": "OverValueOrder"}
    )
    assert client.is_symbol_supported("BTCIRT") is False