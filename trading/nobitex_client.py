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

Phase-1 Hardening:
- Client-side Token Bucket rate limiter (NobitexRateLimiter)
- Centralized RetryPolicy with exponential backoff + full jitter
- No blind retry on mutating (non-idempotent) order operations
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
from .retry_policy import RetryPolicy, RetryConfig

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

        # Phase-1 hardening: client-side rate limiter + centralized retry policy
        self._rate_limiter = NobitexRateLimiter()
        self._retry_policy = RetryPolicy(RetryConfig(
            max_attempts=4,
            initial_delay=0.5,
            max_delay=8.0,
            backoff_factor=2.0,
            jitter_pct=0.25,
        ))

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
        """Perform a Nobitex request with proactive rate-limiting and centralized retry.

        - Client-side Token Bucket prevents 429s proactively.
        - RetryPolicy applies exponential backoff + full jitter only for
          transient / idempotent failures.
        - Mutating (non-idempotent) calls are never blindly retried.
        """
        url = f"{self.BASE_URL}{path}"
        if query_params:
            query_string = urlencode(query_params, doseq=True)
            url += f"?{query_string}"
            full_path = f"{path}?{query_string}"
        else:
            full_path = path

        is_idempotent = method.upper() in ("GET", "HEAD", "OPTIONS")
        # Order placement / cancel are mutating
        if any(p in path for p in ("/market/orders/add", "/market/orders/update-status", "/market/orders/cancel")):
            is_idempotent = False

        def _do_once() -> Dict[str, Any]:
            # Proactive rate limiting
            if not self._rate_limiter.acquire(path, method=method, timeout=15.0):
                raise RateLimitError(
                    "Client-side rate limiter timeout — request not sent.",
                    retry_after=1.0,
                )

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
            except requests.exceptions.Timeout as exc:
                raise NetworkExchangeError(f"Nobitex request timeout: {exc}") from exc
            except requests.exceptions.ConnectionError as exc:
                raise NetworkExchangeError(f"Nobitex connection error: {exc}") from exc
            except requests.exceptions.RequestException as exc:
                raise NetworkExchangeError(f"Nobitex network error: {exc}") from exc

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

        return self._retry_policy.execute(
            _do_once,
            is_idempotent=is_idempotent,
            method=method.upper(),
            operation_name=f"nobitex_{method.upper()}_{path}",
        )

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
            raise ExchangeClientError(message, status_code=status_code)

        # Fallback
        raise ServerExchangeError(
            f"Nobitex unexpected error: {error_code or error_msg or 'unknown'}",
            status_code=status_code,
        )
