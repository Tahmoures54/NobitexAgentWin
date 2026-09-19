"""
trading/nobitex_client.py — Nobitex adapter

Auth follows the official API-key guide:
https://apidocs.nobitex.ir/api_key/api-key-guide

- Nobitex-Key / Nobitex-Signature / Nobitex-Timestamp (never Authorization)
- Ed25519 over timestamp + METHOD + full_path + raw_body
- User-Agent TraderBot/<name-and-version> on every request
- Spot paths only: READ for wallets/orders, TRADE for add/cancel

v6.5.1 FIX (this file):
- `get_balances()` now short-circuits when no credentials are
  configured.  The previous version fired a real POST to
  `/users/wallets/list` on every call, and the resulting 401 error
  was logged as `Requesting Nobitex wallets list...` followed by
  `Exchange authentication failed`, on every scanner cycle.  With
  `check_interval_seconds = 10`, that filled the log with hundreds
  of identical lines per minute.  The new guard marks auth as failed
  once and raises `AuthenticationError` immediately without touching
  the network.

v6.5.0 FIX (retained):
- `_raise_api_error()` no longer maps every 4xx response to
  `SymbolNotFoundError`.  Only `404` and `InvalidCurrency` do.

v6.4.0 FIX (retained):
- Switched orderbook endpoint from deprecated /market/orderbook to
  /v2/orderbook/{SYMBOL}.
- Added negative cache for symbols without orderbook.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.config import APP_NAME, APP_VERSION
from .exchange_base import ExchangeBase
from .exceptions import (
    AuthenticationError,
    AuthorizationError,
    ExchangeClientError,
    NetworkExchangeError,
    RateLimitError,
    ServerExchangeError,
)
from .rate_limiter import NobitexRateLimiter

logger = logging.getLogger(__name__)

USER_AGENT = f"TraderBot/{APP_NAME}-{APP_VERSION}"

_QUOTE_SUFFIXES = ("USDT", "USDC", "IRT", "RLS", "BTC", "ETH")
_EXECUTION_MAP = {
    "market": "market",
    "limit": "limit",
    "stop_market": "stop_market",
    "stop-market": "stop_market",
    "stop": "stop_market",
    "stop_limit": "stop_limit",
    "stop-limit": "stop_limit",
}


class SymbolNotFoundError(Exception):
    """Raised when Nobitex returns 404 / InvalidCurrency for a symbol.

    This error is *never* retried and means the pair does not exist
    on Nobitex (or its orderbook is unavailable).  Any other 4xx
    response is NOT this exception.
    """


class ExchangeClientError(RuntimeError):
    """Raised for non-retryable 4xx errors that are NOT symbol-not-found.

    Examples: OverValueOrder, InsufficientBalance, InvalidAmount,
    ValidationFailed, OrderRejected, ...  The caller receives a clear
    message instead of a misleading `SymbolNotFoundError`.
    """


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        result = float(value)
        if result != result:
            return default
        return result
    except (ValueError, TypeError):
        return default


def _looks_like_symbol(value: Optional[str]) -> bool:
    if not value:
        return False
    cleaned = str(value).upper().replace("-", "").replace("/", "").replace("_", "")
    return any(cleaned.endswith(q) and len(cleaned) > len(q) for q in _QUOTE_SUFFIXES)


def _looks_like_order_id(value: Optional[str]) -> bool:
    if value is None or value == "":
        return False
    text = str(value)
    if text.isdigit():
        return True
    return not _looks_like_symbol(text)


def _coerce_order_ref(order_id: Optional[str], symbol: Optional[str]) -> Tuple[str, Optional[str]]:
    if order_id is None and symbol is None:
        raise ValueError("order_id is required")
    if _looks_like_symbol(order_id) and _looks_like_order_id(symbol):
        return str(symbol), str(order_id)
    if order_id is None:
        raise ValueError("order_id is required")
    return str(order_id), None if symbol is None else str(symbol)


def _wallet_total(wallet: Dict[str, Any]) -> float:
    return max(0.0, _safe_float(wallet.get("balance", wallet.get("available", 0))))


def _wallet_spendable(wallet: Dict[str, Any]) -> float:
    active = wallet.get("activeBalance")
    if active not in (None, ""):
        return _safe_float(active)
    total = _wallet_total(wallet)
    blocked = _safe_float(wallet.get("blockedBalance", 0))
    return max(0.0, total - blocked)


def _fmt_money(value: float) -> str:
    text = f"{float(value):.12f}".rstrip("0").rstrip(".")
    return text or "0"


def _urlsafe_b64decode(value: str) -> bytes:
    raw = (value or "").strip().encode("ascii")
    raw += b"=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(raw)


# Error codes that mean "this symbol/pair does not exist".
_SYMBOL_NOT_FOUND_CODES = frozenset({
    "InvalidCurrency",
    "MarketNotFound",
    "SymbolNotFound",
    "PairNotFound",
    "InvalidSymbol",
})


class NobitexClient(ExchangeBase):
    """Nobitex adapter using API-key authentication (Ed25519 signatures)."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
        quote_currency: str = "IRT",
        timeout: int = 15,
    ):
        super().__init__(api_key=api_key, api_secret=api_secret, testnet=testnet)
        self.quote_currency = (quote_currency or "IRT").upper()
        if self.quote_currency == "RLS":
            self.quote_currency = "IRT"
        self.timeout = int(timeout)

        self.auth_method = "anonymous"
        self.private_key = None
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        self._lock = threading.RLock()

        self._symbol_support_cache: Dict[str, bool] = {}
        self._orderbook_cache: Dict[str, bool] = {}
        self._balance_cache: Dict[str, float] = {}
        self._balance_total_cache: Dict[str, float] = {}
        self._balance_cache_timestamp: float = 0.0
        self._balance_cache_ttl: float = 300.0

        import os
        api_key = api_key or os.getenv("NOBITEX_API_KEY", "")
        api_secret = api_secret or os.getenv("NOBITEX_PRIVATE_KEY", "")
        key_present = bool(api_key and str(api_key).strip())
        secret_present = bool(api_secret and str(api_secret).strip())
        logger.info(
            "NobitexClient init | key_present=%s | private_key_present=%s | quote=%s | testnet=%s",
            key_present, secret_present, self.quote_currency, testnet,
        )

        if key_present and secret_present:
            try:
                private_bytes = _urlsafe_b64decode(api_secret)
                if len(private_bytes) != 32:
                    raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(private_bytes)}")
                self.private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
                self.auth_method = "api_key"
                logger.info("Nobitex API key/private key loaded successfully (Ed25519).")
            except Exception as exc:
                logger.error("Invalid Nobitex private key: %s", exc)
                self.mark_auth_failed("Invalid Nobitex private key format.")
                raise AuthenticationError("Invalid Nobitex private key format.") from exc
        elif key_present or secret_present:
            logger.error("Nobitex API key authentication requires both public key and private key.")
            self.mark_auth_failed("Nobitex API key/private key pair is incomplete.")
            raise AuthenticationError("Nobitex API key/private key pair is incomplete.")
        else:
            logger.warning("Nobitex client initialized without credentials (public data only).")

        self.BASE_URL = (
            "https://testnetapiv2.nobitex.ir" if testnet
            else "https://apiv2.nobitex.ir"
        )
        logger.info("Nobitex base URL set to: %s", self.BASE_URL)

    def _sign_request(self, timestamp: str, method: str, full_path: str, raw_body: str) -> str:
        if not self.private_key:
            raise AuthenticationError("Nobitex private key is not configured.")
        payload = f"{timestamp}{method}{full_path}{raw_body}".encode("utf-8")
        signature = self.private_key.sign(payload)
        return base64.urlsafe_b64encode(signature).decode("ascii")

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[Dict] = None,
        query_params: Optional[Dict] = None,
        signed: bool = True,
    ) -> Dict[str, Any]:
        """Perform a Nobitex request with bounded retry/backoff.

        Retries are limited to transient network/server/rate-limit failures.
        Authentication, authorization, validation failures and 4xx errors
        are never retried.

        FIX v6.5.0: The `except requests.exceptions.*` blocks now raise
        `ExchangeClientError` for a 4xx response that is NOT a 404 /
        SymbolNotFound.  This preserves the real error message for the
        caller instead of masking it as a missing symbol.
        """
        url = f"{self.BASE_URL}{path}"
        if query_params:
            query_string = urlencode(query_params, doseq=True)
            url += f"?{query_string}"
            full_path = f"{path}?{query_string}"
        else:
            full_path = path

        max_attempts = 4
        for attempt in range(max_attempts):
            timestamp = str(int(time.time()))
            raw_body = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
            headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
            if raw_body:
                headers["Content-Type"] = "application/json"
            if signed:
                if not self.private_key or not self.api_key:
                    raise AuthenticationError("Nobitex API key authentication is not configured.")
                signature = self._sign_request(timestamp, method.upper(), full_path, raw_body)
                headers.update({
                    "Nobitex-Key": self.api_key.strip(),
                    "Nobitex-Signature": signature,
                    "Nobitex-Timestamp": timestamp,
                })

            try:
                if method.upper() == "GET":
                    resp = self._session.get(url, headers=headers, timeout=self.timeout)
                else:
                    resp = self._session.request(
                        method, url, headers=headers, data=raw_body or None, timeout=self.timeout,
                    )
                try:
                    payload = resp.json() if resp.content else {}
                except Exception:
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {"raw": payload}

                if resp.status_code >= 400:
                    self._raise_api_error(resp, payload, http_error=True)
                if str(payload.get("status", "")).lower() == "failed":
                    self._raise_api_error(resp, payload, http_error=False)
                return payload

            # Never retry symbol-not-found or generic 4xx client errors.
            except SymbolNotFoundError:
                raise
            except ExchangeClientError:
                raise

            except RateLimitError as exc:
                if attempt >= max_attempts - 1:
                    raise
                retry_after = getattr(exc, "retry_after", None)
                if retry_after is None:
                    retry_after = min(8.0, 0.8 * (2 ** attempt))
                time.sleep(max(0.2, min(float(retry_after), 10.0)))

            except (ServerExchangeError, NetworkExchangeError):
                if attempt >= max_attempts - 1:
                    raise
                time.sleep(min(8.0, 0.6 * (2 ** attempt)))

            except (AuthenticationError, AuthorizationError):
                raise

            except requests.exceptions.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code is not None and 400 <= int(status_code) < 500:
                    if int(status_code) == 404:
                        logger.debug("Nobitex 404 (no retry): %s", exc)
                        raise SymbolNotFoundError(
                            f"Nobitex 404 for {full_path}"
                        ) from exc
                    logger.debug("Nobitex client error HTTP %s (no retry): %s", status_code, exc)
                    raise ExchangeClientError(
                        f"Nobitex HTTP {status_code} for {full_path}: {exc}"
                    ) from exc
                if attempt >= max_attempts - 1:
                    logger.error("Nobitex request failed: %s", exc)
                    raise NetworkExchangeError("Nobitex network error.") from exc
                time.sleep(min(8.0, 0.6 * (2 ** attempt)))

            except requests.exceptions.RequestException as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", None)
                if status_code is not None and 400 <= int(status_code) < 500:
                    if int(status_code) == 404:
                        logger.debug("Nobitex client 404 (no retry) for %s", full_path)
                        raise SymbolNotFoundError(
                            f"Nobitex HTTP 404 for {full_path}"
                        ) from exc
                    logger.debug("Nobitex client error HTTP %s (no retry) for %s", status_code, full_path)
                    raise ExchangeClientError(
                        f"Nobitex HTTP {status_code} for {full_path}"
                    ) from exc
                if attempt >= max_attempts - 1:
                    logger.error("Nobitex request failed: %s", exc)
                    raise NetworkExchangeError("Nobitex network error.") from exc
                time.sleep(min(8.0, 0.6 * (2 ** attempt)))

        raise NetworkExchangeError("Nobitex request failed after retries.")

    def _raise_api_error(self, resp: Any, error_data: Dict[str, Any], *, http_error: bool) -> None:
        """Convert an API error response into the appropriate exception."""
        error_code = str(error_data.get("code", "") or "")
        error_msg = str(error_data.get("message", "") or "")
        status_code = getattr(resp, "status_code", None)
        backoff = error_data.get("backOff")

        # ── Symbol/pair not found ──────────────────────────────
        if status_code == 404:
            logger.debug("Nobitex 404 (symbol/endpoint not found): %s", error_msg or error_code)
            raise SymbolNotFoundError(
                f"Nobitex 404: {error_code or error_msg or 'not found'}"
            )
        if error_code in _SYMBOL_NOT_FOUND_CODES:
            logger.debug("Nobitex symbol-not-found (%s): %s", error_code, error_msg or "unknown")
            raise SymbolNotFoundError(f"{error_code}: {error_msg or 'unknown'}")

        # ── Auth / authz ──────────────────────────────────────
        logger.error(
            "Nobitex API error: HTTP %s code=%s message=%s",
            status_code, error_code, error_msg,
        )
        if error_data:
            logger.error("Error response: %s", error_data)

        if status_code == 401 or error_code in ("Unauthorized", "AuthenticationFailed", "InvalidSignature"):
            hint = (
                "Nobitex authentication failed. Check public key (Nobitex-Key), "
                "private key, and that the PC clock is within 30 seconds of UTC."
            )
            self.mark_auth_failed(hint)
            raise AuthenticationError(hint)
        if status_code == 403:
            self.mark_authorization_failed("Nobitex authorization failed (HTTP 403). Missing READ or TRADE permission?")
            raise AuthorizationError("Nobitex authorization failed (HTTP 403).")

        # ── Rate limit ────────────────────────────────────────
        if status_code == 429 or error_code == "TooManyRequests":
            wait = f" Wait {backoff}s." if backoff not in (None, "") else ""
            err = RateLimitError(
                f"Nobitex rate limit exceeded.{wait}",
                retry_after=_safe_float(backoff, 0.0) if backoff not in (None, "") else None,
            )
            err.status_code = 429
            raise err

        # ── Server errors ─────────────────────────────────────
        if status_code is not None and 500 <= int(status_code) <= 599:
            raise ServerExchangeError(
                f"Nobitex server error (HTTP {status_code}).", status_code=status_code,
            )

        # ── Any other 4xx: client error, NOT symbol-not-found ─
        if status_code is not None and 400 <= int(status_code) < 500:
            message = (
                f"Nobitex HTTP {status_code}: "
                f"{error_code or ''} {error_msg or 'client error'}".strip()
            )
            logger.warning("Nobitex client error: %s", message)
            raise ExchangeClientError(message)

        # ── Fallback ──────────────────────────────────────────
        if http_error:
            resp.raise_for_status()
        raise ExchangeClientError(
            f"Nobitex request failed: {error_code or 'failed'} {error_msg}".strip()
        )

    def resolve_symbol(self, symbol: str) -> str:
        if not symbol:
            raise ValueError("Symbol is empty.")
        cleaned = (
            str(symbol).upper()
            .replace("-", "").replace("/", "").replace("_", "").replace(" ", "")
        )
        if self.quote_currency:
            base, _ = self._split_symbol(cleaned)
            quote = "IRT" if self.quote_currency in ("IRT", "RLS", "IRR") else self.quote_currency
            return f"{base}{quote}"
        return cleaned

    def is_symbol_supported(self, symbol: str) -> bool:
        symbol = self.resolve_symbol(symbol)
        with self._lock:
            if symbol in self._symbol_support_cache:
                return self._symbol_support_cache[symbol]
            if self._orderbook_cache.get(symbol) is False:
                return False
        try:
            base, quote = self._split_symbol(symbol)
            query = {"srcCurrency": base.lower(), "dstCurrency": self._map_quote(quote)}
            data = self._request("GET", "/market/stats", query_params=query, signed=False)
            stats = data.get("stats", {})
            expected_key = f"{base.lower()}-{self._map_quote(quote)}"
            pair_stats = stats.get(expected_key)
            supported = (
                data.get("status") == "ok"
                and pair_stats is not None
                and not pair_stats.get("isClosed", False)
            )
        except SymbolNotFoundError:
            supported = False
        except ExchangeClientError as exc:
            logger.debug("Symbol support check rejected for %s: %s", symbol, exc)
            return False
        except (NetworkExchangeError, ServerExchangeError, RateLimitError) as exc:
            logger.debug("Symbol support check transient failure for %s: %s", symbol, exc)
            return False
        except Exception:
            supported = False
        with self._lock:
            self._symbol_support_cache[symbol] = supported
        return supported

    def invalidate_symbol_cache(self) -> None:
        with self._lock:
            self._symbol_support_cache.clear()
            self._orderbook_cache.clear()

    def get_all_market_stats(self, quote: str = "IRT") -> List[Dict[str, Any]]:
        requested_quote = str(quote or self.quote_currency).upper()
        dst = self._map_quote(requested_quote)
        data = self._request(
            "GET", "/market/stats",
            query_params={"dstCurrency": dst}, signed=False,
        )
        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        api_quote = dst.upper()
        rows: List[Dict[str, Any]] = []
        for key, item in stats.items():
            if not isinstance(item, dict) or key == "global":
                continue
            key_clean = str(key).replace("-", "").upper()
            if not key_clean.endswith(api_quote):
                continue
            base = key_clean[:-len(api_quote)]
            market = base + requested_quote
            if not base or item.get("isClosed"):
                continue
            last = _safe_float(item.get("latest"))
            if last <= 0:
                continue
            rows.append({
                "Symbol": base,
                "AssetKey": f"nobitex:{base.lower()}",
                "Pair": market,
                "Price": last,
                "Bid": _safe_float(item.get("bestBuy")),
                "Ask": _safe_float(item.get("bestSell")),
                "Volume": _safe_float(item.get("volumeDst")),
                "24h Change (%)": _safe_float(item.get("dayChange")),
                "Day Open": _safe_float(item.get("dayOpen")),
                "Day High": _safe_float(item.get("dayHigh")),
                "Day Low": _safe_float(item.get("dayLow")),
                "Market": market,
                "timestamp": int(time.time() * 1000),
            })
        return rows

    def get_ticker(self, symbol: str) -> Dict:
        market_symbol = self.resolve_symbol(symbol)
        base, quote = self._split_symbol(market_symbol)
        query = {"srcCurrency": base.lower(), "dstCurrency": self._map_quote(quote)}
        data = self._request("GET", "/market/stats", query_params=query, signed=False)
        stats = data.get("stats", {}).get(f"{base.lower()}-{self._map_quote(quote)}", {})
        last = _safe_float(stats.get("latest"))
        return {
            "symbol": symbol,
            "last": last,
            "price": last,
            "bid": _safe_float(stats.get("bestBuy")),
            "ask": _safe_float(stats.get("bestSell")),
            "volume": _safe_float(stats.get("volumeDst")),
            "timestamp": int(time.time() * 1000),
        }

    def get_klines(self, symbol: str, interval: str = "1h", limit: int = 100) -> List[Dict]:
        market_symbol = self.resolve_symbol(symbol)
        resolution_map = {
            "1m": 60, "5m": 300, "15m": 900, "30m": 1800,
            "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800,
        }
        resolution = resolution_map.get(interval, 3600)
        now = int(time.time())
        from_time = now - (limit * resolution)
        query = {
            "symbol": market_symbol, "resolution": str(resolution),
            "from": str(from_time), "to": str(now),
        }
        try:
            data = self._request("GET", "/market/udf/history", query_params=query, signed=False)
            candles = data.get("candles", [])
            result = []
            for c in candles:
                if len(c) >= 6:
                    result.append({
                        "timestamp": int(c[0]) * 1000,
                        "open": _safe_float(c[1]),
                        "high": _safe_float(c[2]),
                        "low": _safe_float(c[3]),
                        "close": _safe_float(c[4]),
                        "volume": _safe_float(c[5]),
                    })
            return result
        except SymbolNotFoundError:
            logger.debug("Klines not available for %s (symbol not found)", symbol)
            return []
        except ExchangeClientError as exc:
            logger.warning("Klines request rejected for %s: %s", symbol, exc)
            return []
        except Exception as e:
            logger.warning("Failed to fetch klines for %s: %s", symbol, e)
            return []

    def get_order_book(self, symbol: str, limit: int = 100) -> Dict:
        """Fetch the order book for a symbol.

        Uses Nobitex's current endpoint: GET /v2/orderbook/{SYMBOL}.
        """
        market_symbol = self.resolve_symbol(symbol)

        with self._lock:
            cached = self._orderbook_cache.get(market_symbol)
        if cached is False:
            return {"bids": [], "asks": []}

        try:
            data = self._request(
                "GET", f"/v2/orderbook/{market_symbol}",
                signed=False,
            )
        except SymbolNotFoundError:
            logger.debug("Orderbook not available for %s (marking unsupported)", market_symbol)
            with self._lock:
                self._orderbook_cache[market_symbol] = False
            return {"bids": [], "asks": []}
        except ExchangeClientError as exc:
            logger.warning("Orderbook request rejected for %s: %s", market_symbol, exc)
            return {"bids": [], "asks": []}
        except (NetworkExchangeError, ServerExchangeError, RateLimitError) as exc:
            logger.warning("Orderbook fetch transient failure for %s: %s", market_symbol, exc)
            return {"bids": [], "asks": []}
        except Exception as exc:
            logger.warning("Orderbook fetch failed for %s: %s", market_symbol, exc)
            return {"bids": [], "asks": []}

        if not isinstance(data, dict) or str(data.get("status", "")).lower() not in ("ok", ""):
            logger.debug(
                "Orderbook status not ok for %s: %s",
                market_symbol, data.get("status") if isinstance(data, dict) else None,
            )
            with self._lock:
                self._orderbook_cache[market_symbol] = False
            return {"bids": [], "asks": []}

        bids = data.get("bids", [])[:limit]
        asks = data.get("asks", [])[:limit]
        with self._lock:
            self._orderbook_cache[market_symbol] = True
        return {"bids": bids, "asks": asks}

    def get_balances(self, force_refresh: bool = False) -> Dict[str, float]:
        """
        Fetch account balances.

        FIX v6.5.1:
            Short-circuit before the network call when no credentials are
            configured.  The previous version fired a real POST to
            `/users/wallets/list` on every call and logged two lines per
            attempt (`Requesting Nobitex wallets list...` + the 401 error).
            With the scanner running every 10 seconds and multiple
            balance probes per cycle, the log filled with hundreds of
            identical lines per minute.  We now raise immediately
            without touching the network and mark auth as failed so
            callers can short-circuit their own retry paths.
        """
        # FIX v6.5.1: never hit the network when authentication is not
        # configured.  This is the common case during Paper-mode runs.
        if (
            self.auth_method != "api_key"
            or not self.private_key
            or not self.api_key
        ):
            hint = "Nobitex API key authentication is not configured."
            # Only log the transition once — subsequent calls stay silent.
            if self.authentication_status != self.AUTH_FAILED:
                logger.warning(hint)
                self.mark_auth_failed(hint)
            raise AuthenticationError(hint)

        now = time.time()
        with self._lock:
            if (
                not force_refresh
                and self._balance_cache
                and (now - self._balance_cache_timestamp) < self._balance_cache_ttl
            ):
                logger.debug(
                    "Returning cached balances (age=%.1fs)",
                    now - self._balance_cache_timestamp,
                )
                return self._balance_cache.copy()

        logger.info("Requesting Nobitex wallets list (auth_method=%s)...", self.auth_method)

        attempt_bodies: List[Optional[Dict[str, Any]]] = [{"type": "spot"}, {}]
        last_exc: Optional[Exception] = None

        for idx, body in enumerate(attempt_bodies):
            try:
                data = self._request(
                    "POST", "/users/wallets/list", body=body, signed=True,
                )
                if str(data.get("status", "")).lower() not in ("ok", ""):
                    raise RuntimeError(
                        f"Nobitex wallets list failed: {data.get('message', data)}"
                    )

                wallets = data.get("wallets", [])
                balances: Dict[str, float] = {}
                totals: Dict[str, float] = {}
                for wallet in wallets:
                    if not isinstance(wallet, dict):
                        continue
                    asset = str(wallet.get("currency", "")).upper()
                    if not asset:
                        continue
                    value = _wallet_spendable(wallet)
                    total = _wallet_total(wallet)
                    balances[asset] = value
                    totals[asset] = total
                    if asset == "RLS":
                        balances["IRT"] = value
                        totals["IRT"] = total
                    elif asset == "IRT":
                        balances["RLS"] = value
                        totals["RLS"] = total

                self.mark_authenticated()
                self.balance_status = self.BALANCE_AVAILABLE
                self.last_balance_error = None
                self.last_balance_timestamp = time.time()
                with self._lock:
                    self._balance_cache = balances.copy()
                    self._balance_total_cache = totals.copy()
                    self._balance_cache_timestamp = time.time()
                logger.info("Nobitex balances loaded: %s", list(balances.keys())[:5])
                return balances

            except (AuthenticationError, AuthorizationError, RateLimitError):
                raise
            except ServerExchangeError as exc:
                last_exc = exc
                logger.warning(
                    "Nobitex wallets 5xx (attempt %d/%d): %s",
                    idx + 1, len(attempt_bodies), exc,
                )
                time.sleep(1.0)
                continue
            except ExchangeClientError as exc:
                last_exc = exc
                logger.warning(
                    "Nobitex wallets rejected (attempt %d/%d): %s — retrying with alternate body",
                    idx + 1, len(attempt_bodies), exc,
                )
                time.sleep(0.4)
                continue
            except Exception as exc:
                last_exc = exc
                err_text = str(exc)
                if "HTTP 400" in err_text or "UnexpectedError" in err_text:
                    logger.warning(
                        "Nobitex wallets 400/UnexpectedError (attempt %d/%d): %s — retrying with alternate body",
                        idx + 1, len(attempt_bodies), exc,
                    )
                    time.sleep(0.4)
                    continue
                logger.warning("Nobitex wallets unexpected error: %s", exc)
                break

        with self._lock:
            if self._balance_cache:
                logger.warning(
                    "Using stale balance cache (age=%.1fs) due to: %s",
                    now - self._balance_cache_timestamp,
                    last_exc,
                )
                return self._balance_cache.copy()

        if last_exc is not None:
            self.mark_balance_unavailable(str(last_exc))
            raise last_exc
        return {}

    def get_balance(self, asset: str) -> float:
        asset = (asset or "").upper()
        lookup_asset = "RLS" if asset in ("IRT", "IRR") else asset
        logger.debug("get_balance requested for %s (lookup as %s)", asset, lookup_asset)
        balances = self.get_balances()
        if lookup_asset in balances:
            value = float(balances[lookup_asset])
        elif asset in balances:
            value = float(balances[asset])
        else:
            value = 0.0
        if self._is_quote_asset(asset) or self._is_quote_asset(lookup_asset):
            self.mark_balance_available(value)
        return value

    def get_balance_total(self, asset: str, force_refresh: bool = False) -> float:
        self.get_balances(force_refresh=force_refresh)
        asset = (asset or "").upper()
        lookup_asset = "RLS" if asset in ("IRT", "IRR") else asset
        with self._lock:
            totals = self._balance_total_cache
        if lookup_asset in totals:
            return float(totals[lookup_asset])
        if asset in totals:
            return float(totals[asset])
        return self.get_balance(asset)

    def get_balance_fresh(self, asset: str) -> float:
        self.invalidate_balance_cache()
        asset = (asset or "").upper()
        lookup_asset = "RLS" if asset in ("IRT", "IRR") else asset
        balances = self.get_balances(force_refresh=True)
        if lookup_asset in balances:
            value = float(balances[lookup_asset])
        elif asset in balances:
            value = float(balances[asset])
        else:
            value = 0.0
        if self._is_quote_asset(asset) or self._is_quote_asset(lookup_asset):
            self.mark_balance_available(value)
        return value

    def get_balance_total_fresh(self, asset: str) -> float:
        """Bypass cache AND include funds locked in open orders."""
        self.invalidate_balance_cache()
        asset = (asset or "").upper()
        lookup_asset = "RLS" if asset in ("IRT", "IRR") else asset
        self.get_balances(force_refresh=True)
        with self._lock:
            totals = self._balance_total_cache
        if lookup_asset in totals:
            return float(totals[lookup_asset])
        if asset in totals:
            return float(totals[asset])
        return 0.0

    def invalidate_balance_cache(self) -> None:
        with self._lock:
            self._balance_cache = {}
            self._balance_total_cache = {}
            self._balance_cache_timestamp = 0.0

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "GTC",
        **kwargs,
    ) -> Dict:
        market_symbol = self.resolve_symbol(symbol)
        base, quote = self._split_symbol(market_symbol)
        side = side.lower().strip()
        order_type = order_type.lower().strip()
        execution = _EXECUTION_MAP.get(order_type, order_type)

        client_order_id = str(kwargs.pop("client_order_id", "") or "").strip() or f"cs{int(time.time() * 1000)}{random.randint(100, 999)}"

        body = {
            "type": side,
            "srcCurrency": base.lower(),
            "dstCurrency": self._map_quote(quote),
            "amount": f"{quantity:.8f}".rstrip("0").rstrip(".") or "0",
            "execution": execution,
            "clientOrderId": client_order_id,
        }

        if execution == "limit":
            if price is None:
                raise ValueError("Price is required for limit orders.")
            body["price"] = _fmt_money(price)
        elif execution == "market":
            if price is not None:
                body["price"] = _fmt_money(price)
        elif execution == "stop_market":
            if stop_price is None:
                raise ValueError("stopPrice is required for stop_market orders.")
            body["stopPrice"] = _fmt_money(stop_price)
        elif execution == "stop_limit":
            if price is None:
                raise ValueError("Price is required for stop_limit orders.")
            if stop_price is None:
                raise ValueError("stopPrice is required for stop_limit orders.")
            body["price"] = _fmt_money(price)
            body["stopPrice"] = _fmt_money(stop_price)
        else:
            if price is not None:
                body["price"] = _fmt_money(price)
            if stop_price is not None:
                body["stopPrice"] = _fmt_money(stop_price)

        logger.info("Placing order: %s %s %s, body=%s",
                    side, execution, market_symbol, body)

        try:
            data = self._request("POST", "/market/orders/add", body=body, signed=True)
        except SymbolNotFoundError:
            raise
        except ExchangeClientError:
            raise
        except (NetworkExchangeError, ServerExchangeError) as exc:
            logger.warning(
                "Order submission response was ambiguous for %s/%s; "
                "reconciling clientOrderId=%s.",
                market_symbol, side, client_order_id,
            )
            try:
                reconciled = self.get_order_status(
                    symbol=market_symbol,
                    client_order_id=client_order_id,
                )
                if reconciled.get("order_id"):
                    self.invalidate_balance_cache()
                    return reconciled
            except Exception as reconcile_exc:
                logger.error(
                    "Could not reconcile ambiguous order %s: %s",
                    client_order_id, reconcile_exc,
                )
            raise exc
        logger.info(
            "Order accepted: id=%s status=%s symbol=%s side=%s",
            (data.get("order") or data).get("id") if isinstance(data, dict) else None,
            (data.get("order") or data).get("status") if isinstance(data, dict) else None,
            market_symbol, side,
        )

        self.invalidate_balance_cache()
        return self._parse_order(data.get("order", data))

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        oid, _sym = _coerce_order_ref(order_id, symbol)
        body = {
            "order": int(oid) if str(oid).isdigit() else oid,
            "status": "canceled",
        }
        data = self._request("POST", "/market/orders/update-status", body=body, signed=True)
        self.invalidate_balance_cache()
        return {"order_id": oid, "status": "canceled", "raw": data}

    def _order_list_query(
        self,
        *,
        symbol: Optional[str] = None,
        status: str = "open",
        page_size: int = 100,
    ) -> Dict[str, Any]:
        query: Dict[str, Any] = {
            "status": status,
            "details": 2,
            "tradeType": "spot",
            "pageSize": max(1, min(int(page_size or 100), 1000)),
        }
        if symbol:
            market_symbol = self.resolve_symbol(symbol)
            base, quote = self._split_symbol(market_symbol)
            query["srcCurrency"] = base.lower()
            query["dstCurrency"] = self._map_quote(quote)
        return query

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        data = self._request(
            "GET", "/market/orders/list",
            query_params=self._order_list_query(symbol=symbol, status="open"),
            signed=True,
        )
        orders = data.get("orders", [])
        return [self._parse_order(o) for o in orders]

    def get_order_status(
        self,
        order_id: Optional[str] = None,
        symbol: Optional[str] = None,
        *,
        client_order_id: Optional[str] = None,
    ) -> Dict:
        """Return an order status by numeric/id reference or clientOrderId."""
        if client_order_id:
            body = {"clientOrderId": str(client_order_id)}
        else:
            oid, _sym = _coerce_order_ref(order_id, symbol)
            body = {"id": int(oid) if str(oid).isdigit() else oid}
        data = self._request("POST", "/market/orders/status", body=body, signed=True)
        order = data.get("order", data)
        return self._parse_order(order)

    def get_order_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        data = self._request(
            "GET", "/market/orders/list",
            query_params=self._order_list_query(symbol=symbol, status="all", page_size=limit),
            signed=True,
        )
        orders = data.get("orders", [])
        return [self._parse_order(o) for o in orders[:limit]]

    def get_positions(self) -> List[Dict]:
        return []

    def _split_symbol(self, market_symbol: str) -> tuple:
        known_quotes = ("USDT", "USDC", "IRT", "RLS", "BTC", "ETH")
        symbol = str(market_symbol or "").upper()
        for q in known_quotes:
            if symbol.endswith(q) and len(symbol) > len(q):
                return symbol[: -len(q)], q
        quote = "IRT" if self.quote_currency in ("IRT", "RLS", "IRR") else (self.quote_currency or "IRT")
        return symbol, quote

    def _map_quote(self, quote: str) -> str:
        if quote.upper() in ("IRT", "RLS"):
            return "rls"
        return quote.lower()

    def _normalize_market_symbol(self, market: str) -> str:
        text = str(market or "").upper().replace("_", "").replace("/", "").replace(" ", "")
        if "-" in text:
            base, quote = text.split("-", 1)
            if quote in ("RLS", "IRT", "IRR"):
                return f"{base}IRT"
            return f"{base}{quote}"
        if text:
            return self.resolve_symbol(text)
        return ""

    def _is_quote_asset(self, asset: str) -> bool:
        key = (asset or "").upper()
        if self.quote_currency in ("IRT", "RLS", "IRR"):
            return key in ("IRT", "RLS", "IRR")
        return key == self.quote_currency

    def _parse_order(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not raw or not isinstance(raw, dict):
            return {}

        order_id = raw.get("id") or raw.get("orderId") or raw.get("order_id")
        status_raw = (raw.get("status") or raw.get("state") or "").lower()
        order_type = str(raw.get("execution") or raw.get("order_type") or "").lower()

        matched_amount = _safe_float(
            raw.get("matchedAmount")
            or raw.get("matched_amount")
            or raw.get("matched")
        )
        unmatched_raw = (
            raw.get("unmatchedAmount")
            if "unmatchedAmount" in raw
            else raw.get("unmatched_amount")
            if "unmatched_amount" in raw
            else raw.get("unmatched")
            if "unmatched" in raw
            else None
        )
        unmatched_present = unmatched_raw is not None
        unmatched_amount = _safe_float(unmatched_raw) if unmatched_present else None

        if status_raw in ("done", "filled", "matched", "complete", "completed"):
            if matched_amount > 0 and (not unmatched_present or unmatched_amount == 0):
                status = "filled"
            elif matched_amount > 0:
                status = "partial"
            else:
                status = "open"
        elif status_raw in ("canceled", "cancelled", "rejected"):
            status = "canceled"
        elif status_raw in ("new", "active", "open", "pending", "inactive"):
            if matched_amount > 0 and unmatched_present and unmatched_amount == 0:
                status = "filled"
            elif matched_amount > 0:
                status = "partial"
            else:
                status = "open"
        else:
            status = status_raw or "unknown"
            if matched_amount <= 0 and status in ("filled", "closed", "complete", "completed", "done"):
                status = "open"

        price_value = raw.get("price")
        if price_value is None or str(price_value).lower() == "market":
            price = 0.0
        else:
            price = _safe_float(price_value)

        executed_qty = matched_amount
        avg_price = _safe_float(
            raw.get("averagePrice") or raw.get("avg_price") or raw.get("executedPrice")
        )

        if avg_price <= 0 and matched_amount > 0:
            symbol = raw.get("market") or raw.get("symbol") or ""
            if symbol:
                try:
                    ticker = self.get_ticker(symbol)
                    avg_price = _safe_float(ticker.get("last") or ticker.get("price"))
                except Exception:
                    avg_price = 0.0

        placed_qty = _safe_float(raw.get("amount") or raw.get("quantity"))

        return {
            "order_id": order_id,
            "symbol": self._normalize_market_symbol(
                raw.get("market") or raw.get("symbol") or ""
            ),
            "side": raw.get("type") or raw.get("side") or "",
            "type": order_type,
            "price": price,
            "quantity": placed_qty,
            "matched_amount": matched_amount,
            "unmatched_amount": unmatched_amount if unmatched_present else 0.0,
            "executed_qty": executed_qty,
            "executed_quantity": executed_qty,
            "executed_price": avg_price,
            "status": status,
            "time": raw.get("createdAt") or raw.get("created_at") or raw.get("time"),
            "update_time": raw.get("updatedAt") or raw.get("updated_at") or raw.get("update_time"),
            "raw": raw,
        }

    def get_diagnostics(self) -> Dict[str, Any]:
        with self._lock:
            cache_ts = self._balance_cache_timestamp
            symbol_cache_size = len(self._symbol_support_cache)
            orderbook_cache_size = len(self._orderbook_cache)
            orderbook_unsupported = sum(
                1 for v in self._orderbook_cache.values() if v is False
            )
        return {
            "base_url": self.BASE_URL,
            "testnet": bool(self.testnet),
            "auth_method": self.auth_method,
            "user_agent": USER_AGENT,
            "credentials_present": bool(self.api_key),
            "authentication_status": self.authentication_status,
            "balance_status": self.balance_status,
            "quote_currency": self.quote_currency,
            "last_balance_error": self.last_balance_error,
            "balance_cache_age_s": (
                time.time() - cache_ts if cache_ts else None
            ),
            "symbol_cache_size": symbol_cache_size,
            "orderbook_cache_size": orderbook_cache_size,
            "orderbook_unsupported_count": orderbook_unsupported,
        }

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
        super().close()
        logger.info("Nobitex client closed.")
