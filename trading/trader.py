"""
trader.py — TradingBot execution layer v6.4.0
=============================================

Drop-in replacement for the project's trader.py / TradingBot module.
Keep this file next to bot_config.py and exchange_base.py (same package).

Execution-only adapter used by SignalTracker.

FIXES vs v6.3.1
────────────────
FIX 1 — `_sync_exchange_state()` now mirrors the full balance state
    from the exchange.  Previously only `authentication_status` and
    `live_trading_available` were copied.  If the exchange had
    already probed the balance successfully during its own init,
    `TradingBot.start()` would still see `balance_status = UNKNOWN`
    and refuse to enable execution.

FIX 2 — `_extract_asset_amount()` no longer silently returns 0.0 when
    the asset key is missing from a successful `get_balances()`
    response.  A missing asset in a valid response means the account
    really has zero of it — but a MISSING response (None) means the
    whole lookup failed and must be treated as `None`, not `0.0`.

FIX 3 — `get_balance_fresh()` no longer falls back to a cached
    `get_balance()` when the exchange's dedicated fresh read is
    callable but returns None.  `None` is propagated so `SignalTracker`
    can distinguish "unknown" from "zero".  Only when the exchange
    has NO fresh API at all does the method fall back to a cached read,
    and it does so with a clear debug log.

FIX 4 — `_is_auth_error()` and `_is_authorization_error()` are
    tightened so a generic 4xx (OverValueOrder, InsufficientBalance,
    ExchangeClientError) is not mistaken for an auth failure.

FIX 5 — `_handle_balance_exception()` distinguishes a generic 4xx
    client error from network/server failures.

Retained from v6.3.1 (the get_balance_fresh patch):
- Fresh reads never turn a failed lookup into 0.0.
- get_balance(asset) no longer overwrites quote equity with a
  base-asset amount.
- A successful balance probe does not enable execution while the
  bot is stopped.
- Cache is invalidated after accepted orders and cancels.
- Post-fill refresh uses get_balance_fresh.
- IRT / RLS / IRR wallet keys are aliased.
- Exchange HTTP stays outside long-held locks.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .bot_config import BotConfig, load_config, validate_config
from .exchange_base import ExchangeBase
from .execution_mode import LIVE, PAPER, normalize_execution_mode
from .idempotency import IdempotencyGuard
from .portfolio_manager import NobitexPortfolioManager
from .trade_accounting import TradeAccounting


logger = logging.getLogger("TradingBot")


AUTH_UNKNOWN = "UNKNOWN"
AUTHENTICATED = "AUTHENTICATED"
AUTH_FAILED = "AUTH_FAILED"
AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"

BALANCE_UNKNOWN = "UNKNOWN"
BALANCE_AVAILABLE = "AVAILABLE"
BALANCE_UNAVAILABLE = "UNAVAILABLE"

_FILLED_STATUSES = ("filled", "closed", "complete", "completed")
_ACCEPTED_STATUSES = _FILLED_STATUSES + ("open", "partial", "inactive", "new", "accepted")
_QUOTE_ALIASES = {
    "IRT": ("IRT", "RLS", "IRR"),
    "RLS": ("IRT", "RLS", "IRR"),
    "IRR": ("IRT", "RLS", "IRR"),
}


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value is None:
        return default
    try:
        result = float(value)
        if result != result:  # NaN
            return default
        return result
    except (TypeError, ValueError):
        return default


def _status_code_from_exception(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "status", "http_status", "response_status", "code"):
        try:
            value = getattr(exc, attr, None)
            if value is not None:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

    try:
        response = getattr(exc, "response", None)
        if response is not None:
            status_code = getattr(response, "status_code", None)
            if status_code is not None:
                return int(status_code)
    except Exception:
        pass

    text = str(exc).lower()
    for code in (401, 403, 404, 409, 422, 429, 500, 502, 503, 504):
        if f" {code}" in f" {text}" or text.startswith(str(code)) or f"http {code}" in text:
            return code
    return None


# FIX v6.4.0: strict authentication classification.
# A generic 4xx (OverValueOrder, InsufficientBalance, ExchangeClientError)
# is NEVER classified as an auth error.
def _is_auth_error(exc: Exception) -> bool:
    # If the exception class name explicitly says "authentication" it
    # is an auth error regardless of the numeric code.
    name = exc.__class__.__name__.lower()
    if "authentication" in name or "autherror" in name or "unauthorized" in name:
        return True
    # Numeric 401 from any wrapper.
    if _status_code_from_exception(exc) == 401:
        return True
    # Textual hints — only if no other status code is present.
    text = str(exc).lower()
    if _status_code_from_exception(exc) is None:
        return (
            "invalid signature" in text
            or "invalid api key" in text
            or "authentication failed" in text
        )
    return False


def _is_authorization_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    if "authorization" in name or "permission" in name or "forbidden" in name:
        return True
    return _status_code_from_exception(exc) == 403


def _is_rate_limit_error(exc: Exception) -> bool:
    code = _status_code_from_exception(exc)
    if code == 429:
        return True
    name = exc.__class__.__name__.lower()
    return "ratelimit" in name or "rate_limit" in name or "too_many" in name


def _is_server_error(exc: Exception) -> bool:
    code = _status_code_from_exception(exc)
    return code is not None and 500 <= code <= 599


def _is_client_error(exc: Exception) -> bool:
    """Generic non-retryable 4xx (NOT auth, NOT authz, NOT rate limit)."""
    code = _status_code_from_exception(exc)
    if code is None:
        return False
    return 400 <= code < 500 and code not in (401, 403, 429)


def _is_network_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    text = str(exc).lower()
    if any(word in name for word in ("timeout", "connection", "network")):
        return True
    return any(
        word in text
        for word in (
            "timeout", "timed out", "connection reset", "connection refused",
            "network error", "dns", "socket", "temporarily unavailable",
        )
    )


def _asset_lookup_keys(asset: str) -> List[str]:
    key = str(asset or "").strip().upper()
    if not key:
        return []
    return list(_QUOTE_ALIASES.get(key, (key,)))


class TradingBot:
    _instance: Optional["TradingBot"] = None
    _instance_lock = threading.RLock()

    @classmethod
    def get_instance(
        cls,
        config: Optional[BotConfig] = None,
        auto_start: bool = False,
        force: bool = False,
        **kwargs: Any,
    ) -> "TradingBot":
        with cls._instance_lock:
            if force and cls._instance is not None:
                try:
                    cls._instance.close()
                except Exception:
                    logger.exception("Error while replacing TradingBot instance.")
                cls._instance = None
            if cls._instance is None:
                cls._instance = cls(config=config, auto_start=auto_start, **kwargs)
            elif auto_start and not cls._instance.running:
                cls._instance.start()
            elif config is not None:
                logger.debug("get_instance ignored a new config because an instance already exists.")
            return cls._instance

    @classmethod
    def release_instance(cls) -> None:
        with cls._instance_lock:
            instance = cls._instance
            if instance is not None:
                try:
                    instance.close()
                except Exception:
                    logger.exception("Error while releasing TradingBot instance.")
            cls._instance = None

    def __init__(
        self,
        config: Optional[BotConfig] = None,
        auto_start: bool = False,
        exchange: Optional[ExchangeBase] = None,
    ):
        self.config: BotConfig = config if config is not None else load_config()
        self.exchange: Optional[ExchangeBase] = None

        self.running = False
        self.closed = False
        self.lock = threading.RLock()
        self.idempotency = IdempotencyGuard()
        self.portfolio_manager: Optional[NobitexPortfolioManager] = None
        self.trade_accounting: Optional[TradeAccounting] = None

        self.exchange_name = (
            getattr(self.config, "exchange", "simulator") or "simulator"
        ).strip().lower()

        self.quote_currency = (
            getattr(
                self.config,
                "quote_currency",
                "IRT" if str(getattr(self.config, "exchange", "")).lower() == "nobitex" else "USDT",
            ) or "USDT"
        ).strip().upper()

        self.nobitex_market = (
            getattr(self.config, "nobitex_market", self.quote_currency)
            or self.quote_currency
        ).strip().upper()

        if self.nobitex_market == "RLS":
            self.nobitex_market = "IRT"

        self.authentication_status = AUTH_UNKNOWN
        self.authentication_error: Optional[str] = None

        self.starting_balance: Optional[float] = None
        self.current_balance: Optional[float] = None
        self.last_known_balance: Optional[float] = None

        self.balance_status = BALANCE_UNKNOWN
        self.last_balance_error: Optional[str] = None
        self.last_balance_timestamp: Optional[float] = None

        self.live_trading_available = False
        self.execution_enabled = False
        # A connected account is not an authorization to trade.  The GUI
        # arms this flag only after the user switches to LIVE and presses
        # Start; paper mode and a background connection can never submit a
        # Nobitex order.
        self.live_order_activation = False

        self.last_error: Optional[str] = None
        self.last_error_timestamp: Optional[float] = None

        self.last_order: Optional[Dict[str, Any]] = None
        self.last_order_timestamp: Optional[float] = None

        self._balance_retry_after = 0.0
        self._balance_retry_count = 0
        self._max_balance_retry_count = 6
        self._max_balance_retry_backoff = 60.0

        try:
            errors = validate_config(self.config)
        except Exception as exc:
            logger.exception("Configuration validation failed: %s", exc)
            raise ValueError(f"Unable to validate bot configuration: {exc}") from exc

        if errors:
            if isinstance(errors, str):
                errors = [errors]
            raise ValueError("Invalid bot configuration: " + "; ".join(str(x) for x in errors))

        if exchange is not None:
            self.exchange = exchange
            self.exchange_name = (
                getattr(self.config, "exchange", self.exchange_name) or self.exchange_name
            ).strip().lower()
        else:
            self._init_exchange()

        if self.exchange is None:
            raise RuntimeError("Exchange initialization failed")

        # FIX v6.4.0: pull the FULL balance state before any probe.
        self._sync_exchange_state()
        self._initialize_balance()
        if self.exchange_name == "nobitex":
            self.portfolio_manager = NobitexPortfolioManager(
                self.exchange, quote=self.quote_currency
            )
            self.trade_accounting = TradeAccounting(quote=self.quote_currency)

        if auto_start:
            self.start()

    def record_actual_fill(self, order: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Record a terminal/partial exchange fill without affecting execution."""
        if self.trade_accounting is None:
            return None
        try:
            result = self.trade_accounting.ingest_order(order)
            if result.get("status") == "recorded":
                logger.info(
                    "Actual fill reconciled into ledger: %s %s qty=%s price=%s",
                    order.get("side"), order.get("symbol"),
                    result.get("quantity"), result.get("price"),
                )
            return result
        except Exception as exc:
            logger.warning("Actual fill ledger reconciliation failed: %s", exc)
            return {"status": "error", "accounting_complete": False, "reason": str(exc)}

    def refresh_portfolio(self, *, force: bool = True, include_orders: bool = True) -> Optional[Dict[str, Any]]:
        """Refresh the real Nobitex spot portfolio and return its snapshot."""
        if self.exchange is None or self.exchange_name != "nobitex":
            return None
        if self.portfolio_manager is None:
            self.portfolio_manager = NobitexPortfolioManager(
                self.exchange, quote=self.quote_currency
            )
        snapshot = self.portfolio_manager.refresh(
            force=force, include_orders=include_orders
        )
        # Keep quote equity aligned with the exchange-valued account.
        portfolio_value = _safe_float(snapshot.get("portfolio_value_quote"), None)
        if portfolio_value is not None and portfolio_value >= 0:
            self.current_balance = portfolio_value
            self.last_known_balance = portfolio_value
        return snapshot


    def reconcile_portfolio(
        self,
        tracker: Any,
        *,
        force: bool = True,
        include_orders: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Refresh Nobitex and reconcile the supplied live SignalTracker."""
        if self.exchange is None or self.exchange_name != "nobitex":
            return None
        if self.portfolio_manager is None:
            self.portfolio_manager = NobitexPortfolioManager(
                self.exchange, quote=self.quote_currency
            )
        snapshot = self.portfolio_manager.refresh_and_reconcile(
            tracker,
            force=force,
            include_orders=include_orders,
        )
        if self.trade_accounting is not None:
            try:
                prices = {
                    str(item.get("asset") or "").upper(): _safe_float(item.get("price"), None)
                    for item in snapshot.get("assets", [])
                    if isinstance(item, dict) and item.get("asset")
                }
                wallet = {
                    str(item.get("asset") or "").upper(): _safe_float(item.get("available"), 0.0) or 0.0
                    for item in snapshot.get("assets", [])
                    if isinstance(item, dict) and item.get("asset")
                }
                snapshot["accounting"] = self.trade_accounting.snapshot(prices)
                snapshot["accounting_reconciliation"] = self.trade_accounting.reconcile_wallet(wallet)
            except Exception as accounting_exc:
                logger.warning("Portfolio accounting snapshot failed: %s", accounting_exc)
                snapshot["accounting"] = {"accounting_complete": False, "error": str(accounting_exc)}
        portfolio_value = _safe_float(snapshot.get("portfolio_value_quote"), None)
        if portfolio_value is not None and portfolio_value >= 0:
            self.current_balance = portfolio_value
            self.last_known_balance = portfolio_value
        return snapshot

    def _init_exchange(self) -> None:
        """Initialize the only supported live venue: Nobitex spot IRT."""
        exchange_id = str(getattr(self.config, "exchange", "nobitex") or "nobitex").strip().lower()
        if exchange_id != "nobitex":
            raise ValueError("CryptoScanner supports Nobitex only.")

        from .nobitex_client import NobitexClient
        self.nobitex_market = "IRT"
        self.quote_currency = "IRT"
        self.config.exchange = "nobitex"
        self.config.nobitex_market = "IRT"
        self.config.quote_currency = "IRT"
        self.config.quote_unit = "rial"

        self.exchange = NobitexClient(
            api_key=getattr(self.config, "api_key", ""),
            api_secret=getattr(self.config, "api_secret", ""),
            testnet=bool(getattr(self.config, "testnet", False)),
            quote_currency="IRT",
            timeout=15,
        )
        self.exchange_name = "nobitex"
        self._sync_exchange_state()
        logger.info("Nobitex exchange initialized (market=IRT, quote=IRT).")

    def _sync_exchange_state(self) -> None:
        """
        Mirror the exchange client's state into TradingBot.

        FIX v6.4.0: now copies the full balance state
        (`balance_status`, `last_balance`, `last_balance_error`,
        `last_balance_timestamp`) in addition to auth and live flags.
        """
        if self.exchange is None:
            return

        # Auth / live flags
        try:
            exchange_auth = getattr(self.exchange, "authentication_status", None)
            if exchange_auth:
                self.authentication_status = exchange_auth
        except Exception:
            pass

        try:
            exchange_live = getattr(self.exchange, "live_trading_available", None)
            if exchange_live is not None:
                self.live_trading_available = bool(exchange_live)
        except Exception:
            pass

        # Balance state — new in v6.4.0
        try:
            exchange_balance_status = getattr(self.exchange, "balance_status", None)
            if exchange_balance_status:
                self.balance_status = exchange_balance_status
        except Exception:
            pass

        try:
            exchange_last_balance = getattr(self.exchange, "last_balance", None)
            if exchange_last_balance is not None:
                parsed = _safe_float(exchange_last_balance, None)
                if parsed is not None and parsed >= 0:
                    self.current_balance = parsed
                    self.last_known_balance = parsed
        except Exception:
            pass

        try:
            exchange_last_error = getattr(self.exchange, "last_balance_error", None)
            if exchange_last_error:
                self.last_balance_error = str(exchange_last_error)
        except Exception:
            pass

        try:
            exchange_last_ts = getattr(self.exchange, "last_balance_timestamp", None)
            if exchange_last_ts:
                self.last_balance_timestamp = float(exchange_last_ts)
        except Exception:
            pass

    def _initialize_balance(self) -> None:
        # If the exchange already has a valid balance, skip the probe.
        if (
            self.balance_status == BALANCE_AVAILABLE
            and self.last_known_balance is not None
            and self.last_known_balance >= 0
        ):
            logger.info(
                "Starting balance (from exchange init): %.2f %s",
                self.last_known_balance, self.quote_currency,
            )
            return

        balance = self.get_balance(self.quote_currency, update_starting_balance=True)
        if balance is not None:
            logger.info("Starting balance: %.2f %s", balance, self.quote_currency)
        else:
            logger.warning(
                "Initial balance is unavailable. Live execution will remain "
                "disabled until a valid balance is obtained."
            )
            if self.exchange_name == "nobitex":
                logger.warning(
                    "Nobitex balance is unavailable. Check API authentication "
                    "and account permissions."
                )

    def start(self) -> None:
        with self.lock:
            if self.running:
                return
            self.running = True

            if self.exchange_name == "simulator":
                self.execution_enabled = True
            else:
                self.execution_enabled = bool(
                    self.live_trading_available
                    and self.balance_status == BALANCE_AVAILABLE
                )

            logger.info(
                "TradingBot execution layer started (mode=%s | environment=%s | execution=%s)",
                self.exchange_name.upper(),
                "TESTNET" if bool(getattr(self.config, "testnet", False)) else "PRODUCTION",
                "READY" if self.execution_enabled else "BLOCKED",
            )

            if self.exchange_name != "simulator" and not self.live_trading_available:
                logger.warning(
                    "TradingBot started, but live execution is currently unavailable."
                )

    def stop(self) -> None:
        with self.lock:
            self.running = False
            self.execution_enabled = False
            self.live_order_activation = False
            logger.info("TradingBot execution layer stopped")

    def get_status(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "exchange": self.exchange_name,
                "quote_currency": self.quote_currency,
                "current_balance": self.current_balance,
                "starting_balance": self.starting_balance,
                "last_known_balance": self.last_known_balance,
                "balance_status": self.balance_status,
                "last_balance_error": self.last_balance_error,
                "last_balance_timestamp": self.last_balance_timestamp,
                "authentication_status": self.authentication_status,
                "authentication_error": self.authentication_error,
                "live_trading_available": self.live_trading_available,
                "live_order_activation": bool(getattr(self, "live_order_activation", False)),
                "execution_enabled": self.execution_enabled,
                "last_error": self.last_error,
                "last_error_timestamp": self.last_error_timestamp,
                "last_order": self.last_order,
                "last_order_timestamp": self.last_order_timestamp,
            }

    def _mark_auth_failed(self, exc: Exception) -> None:
        self.authentication_status = AUTH_FAILED
        self.authentication_error = str(exc)
        self.live_trading_available = False
        self.execution_enabled = False
        logger.error("Exchange authentication failed: %s", exc)

    def _mark_authorization_failed(self, exc: Exception) -> None:
        self.authentication_status = AUTHORIZATION_FAILED
        self.authentication_error = str(exc)
        self.live_trading_available = False
        self.execution_enabled = False
        logger.error("Exchange authorization failed: %s", exc)

    def _mark_balance_unavailable(self, exc: Exception) -> None:
        self.balance_status = BALANCE_UNAVAILABLE
        self.last_balance_error = str(exc)
        self.last_balance_timestamp = time.time()
        logger.warning("Account balance unavailable: %s", exc)

    def _mark_balance_available(self, balance: float) -> None:
        self.balance_status = BALANCE_AVAILABLE
        self.last_balance_error = None
        self.last_balance_timestamp = time.time()
        self.current_balance = balance
        self.last_known_balance = balance
        self._balance_retry_count = 0
        self._balance_retry_after = 0.0

    def _is_quote_asset(self, asset: str) -> bool:
        requested = set(_asset_lookup_keys(asset))
        quote = set(_asset_lookup_keys(self.quote_currency))
        return bool(requested & quote)

    def _extract_asset_amount(self, balances: Any, asset: str) -> Optional[float]:
        """
        Extract an asset amount from a balances dict.

        FIX v6.4.0:
          - Return `None` if the balances payload itself is missing
            (lookup failed entirely) — MUST NOT be treated as 0.0.
          - Return 0.0 only when the payload is a valid non-empty dict
            that legitimately does not contain the asset key.
          - Return the parsed value when the key is present.
        """
        if balances is None:
            return None
        if not isinstance(balances, dict):
            return None
        if not balances:
            return 0.0

        upper = {str(k).upper(): v for k, v in balances.items()}
        for key in _asset_lookup_keys(asset):
            if key in upper:
                parsed = _safe_float(upper[key], None)
                if parsed is not None:
                    return parsed
                return None
        return 0.0

    def invalidate_balance_cache(self) -> None:
        if self.exchange is None:
            return
        invalidate = getattr(self.exchange, "invalidate_balance_cache", None)
        if callable(invalidate):
            try:
                invalidate()
            except Exception as exc:
                logger.debug("invalidate_balance_cache failed: %s", exc)

    def _record_successful_auth(self, asset: str, balance: float, update_starting_balance: bool) -> None:
        with self.lock:
            self.authentication_status = AUTHENTICATED
            if self.exchange_name != "simulator":
                self.live_trading_available = True
            if self._is_quote_asset(asset):
                self._mark_balance_available(balance)
                if update_starting_balance or self.starting_balance is None:
                    self.starting_balance = balance
                if self.running:
                    self.execution_enabled = True

    def _schedule_balance_retry(self, exc: Exception, label: str, status_code: Optional[int] = None) -> None:
        with self.lock:
            self._balance_retry_count = min(
                self._balance_retry_count + 1, self._max_balance_retry_count
            )
            backoff = min(2 ** self._balance_retry_count, self._max_balance_retry_backoff)
            self._balance_retry_after = time.time() + backoff
            self._mark_balance_unavailable(exc)
        extra = f" (HTTP {status_code})" if status_code else ""
        logger.warning("%s%s. Balance retry in %.1fs.", label, extra, backoff)

    def _handle_balance_exception(self, exc: Exception, asset: str) -> None:
        status_code = _status_code_from_exception(exc)
        if _is_auth_error(exc):
            with self.lock:
                self._mark_auth_failed(exc)
                self._mark_balance_unavailable(exc)
            logger.error("API authentication failed (HTTP 401). Live trading disabled.")
            return
        if _is_authorization_error(exc):
            with self.lock:
                self._mark_authorization_failed(exc)
                self._mark_balance_unavailable(exc)
            logger.error("Exchange authorization failed (HTTP 403). Live trading disabled.")
            return
        if _is_rate_limit_error(exc):
            self._schedule_balance_retry(exc, "Exchange rate limit reached", status_code or 429)
            return
        if _is_server_error(exc):
            self._schedule_balance_retry(exc, "Exchange server error", status_code)
            return
        if _is_client_error(exc):
            # FIX v6.4.0: a generic 4xx (bad body, invalid parameter)
            # is NOT retryable — report it clearly.
            with self.lock:
                self._mark_balance_unavailable(exc)
            logger.error(
                "Exchange rejected the balance request (HTTP %s): %s",
                status_code, exc,
            )
            return
        if _is_network_error(exc):
            self._schedule_balance_retry(exc, f"Network error while reading {asset} balance", status_code)
            return
        with self.lock:
            self._mark_balance_unavailable(exc)
        logger.error("Balance read failed for %s: %s", asset, exc)

    def get_balance(
        self,
        asset: str,
        update_starting_balance: bool = False,
    ) -> Optional[float]:
        if self.exchange is None:
            self._mark_balance_unavailable(RuntimeError("Exchange is not initialized"))
            return None

        asset = str(asset or "").strip().upper()
        if not asset:
            self._mark_balance_unavailable(ValueError("Asset is empty"))
            return None

        with self.lock:
            blocked = (
                time.time() < self._balance_retry_after
                and self.balance_status == BALANCE_UNAVAILABLE
            )
        if blocked:
            return None

        try:
            balance = _safe_float(self.exchange.get_balance(asset), None)
            if balance is None:
                raise ValueError("Exchange returned an invalid balance.")
            if balance < 0:
                raise ValueError(f"Exchange returned negative balance: {balance}")
            self._record_successful_auth(asset, balance, update_starting_balance)
            logger.debug("Balance: %.8f %s", balance, asset)
            return balance
        except Exception as exc:
            self._handle_balance_exception(exc, asset)
            return None

    def get_balance_total(self, asset: str, force_refresh: bool = False) -> Optional[float]:
        """Wallet total including funds reserved by unmatched open orders."""
        if self.exchange is None:
            return None
        key = str(asset or "").strip().upper()
        if not key:
            return None
        total_fn = getattr(self.exchange, "get_balance_total", None)
        if not callable(total_fn):
            return self.get_balance(key)
        try:
            parsed = _safe_float(total_fn(key, force_refresh=force_refresh), None)
        except TypeError:
            parsed = _safe_float(total_fn(key), None)
        except Exception as exc:
            logger.debug("get_balance_total failed for %s: %s", key, exc)
            return self.get_balance(key)
        if parsed is None or parsed < 0:
            return self.get_balance(key)
        return parsed

    def get_balance_fresh(self, asset: str) -> Optional[float]:
        """
        Force a cache-bypassing wallet read. Returns None when unknown.

        v6.4.0 contract (FIX 3):
          - If the exchange implements `get_balance_fresh` and it
            returns a real number, use it.
          - If `get_balance_fresh` is callable but returns None/invalid,
            **propagate None** — do NOT fall back to a stale cached
            `get_balance()` value.
          - Only when the exchange has NO `get_balance_fresh` API at all
            does this method fall back to `get_balances(force_refresh=True)`
            and then, as a last resort, to a direct `get_balance()` read.
        """
        if self.exchange is None:
            return None

        key = str(asset or "").strip().upper()
        if not key:
            return None

        try:
            self.invalidate_balance_cache()

            # ── Preferred path: exchange's dedicated fresh read ──
            fresh = getattr(self.exchange, "get_balance_fresh", None)
            if callable(fresh):
                try:
                    raw = fresh(key)
                except Exception as inner:
                    logger.debug(
                        "exchange.get_balance_fresh raised for %s: %s",
                        key, inner,
                    )
                    raw = None

                parsed = _safe_float(raw, None)
                if parsed is not None:
                    if parsed < 0:
                        raise ValueError(
                            f"Exchange returned negative balance: {parsed}"
                        )
                    self._record_successful_auth(
                        key, parsed, update_starting_balance=False
                    )
                    return parsed

                # FIX v6.4.0: fresh read implemented but unavailable.
                # Do NOT fall back to stale cache.
                logger.warning(
                    "get_balance_fresh: exchange fresh read returned no "
                    "value for %s; propagating None (not using cache).",
                    key,
                )
                return None

            # ── Secondary path (only when get_balance_fresh is NOT
            #    implemented): forced balances refresh ──
            get_balances = getattr(self.exchange, "get_balances", None)
            if callable(get_balances):
                balances = None
                try:
                    balances = get_balances(force_refresh=True)
                except TypeError:
                    try:
                        balances = get_balances()
                    except Exception:
                        balances = None
                except Exception as exc:
                    logger.debug("get_balances(force_refresh=True) failed: %s", exc)
                    balances = None

                if balances is not None:
                    parsed = self._extract_asset_amount(balances, key)
                    if parsed is not None:
                        if parsed < 0:
                            raise ValueError(
                                f"Exchange returned negative balance: {parsed}"
                            )
                        self._record_successful_auth(
                            key, parsed, update_starting_balance=False
                        )
                        return parsed

            # ── Last resort: cached read (exchange has no fresh API) ──
            logger.debug(
                "get_balance_fresh falling back to cached get_balance for %s",
                key,
            )
            parsed = _safe_float(self.exchange.get_balance(key), None)
            if parsed is None:
                return None
            if parsed < 0:
                raise ValueError(f"Exchange returned negative balance: {parsed}")
            self._record_successful_auth(
                key, parsed, update_starting_balance=False
            )
            return parsed

        except Exception as exc:
            logger.warning("get_balance_fresh failed for %s: %s", key, exc)
            self._handle_balance_exception(exc, key)
            return None

    def refresh_balance(self) -> Optional[float]:
        return self.get_balance_fresh(self.quote_currency)

    def _can_place_order(self, side: Optional[str] = None) -> bool:
        if not self.running:
            return False
        if not self.execution_enabled:
            return False
        if self.authentication_status in (AUTH_FAILED, AUTHORIZATION_FAILED):
            return False
        if self.exchange_name == "simulator":
            return True
        if self.exchange_name == "nobitex" and str(side or "buy").lower() != "sell":
            # A live session may be switched back to paper while existing
            # positions are still monitored.  The mode blocks new BUYs, but
            # an already authenticated session may still submit SELL exits.
            if not bool(getattr(self, "live_order_activation", False)):
                return False
        if not self.live_trading_available:
            return False
        if self.balance_status != BALANCE_AVAILABLE:
            return False
        return True

    def normalize_symbol_for_execution(self, symbol: str) -> str:
        raw = str(symbol or "").strip().upper()
        if not raw:
            return raw

        if self.exchange_name != "nobitex":
            return raw.replace("/", "") if "/" in raw else raw

        market = (
            self.nobitex_market or self.quote_currency or "IRT"
        ).upper()
        if market == "RLS":
            market = "IRT"

        if "/" in raw:
            base, old_quote = raw.split("/", 1)
            if old_quote in ("USDT", "USDC", "USD", "IRT", "RLS"):
                return f"{base}{market}"
            return raw.replace("/", "")

        for old_quote in ("USDT", "USDC", "USD", "IRT", "RLS"):
            if raw.endswith(old_quote):
                base = raw[:-len(old_quote)]
                if base:
                    return f"{base}{market}"

        return f"{raw}{market}"

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if not symbol:
            return {"status": "rejected", "order_id": None, "message": "Symbol is empty."}

        quantity_value = _safe_float(quantity, None)
        if quantity_value is None or quantity_value <= 0:
            return {"status": "rejected", "order_id": None, "message": "Invalid order quantity."}

        side = str(side or "").strip().lower()
        order_type = str(order_type or "").strip().lower()

        if side not in ("buy", "sell"):
            return {"status": "rejected", "order_id": None, "message": f"Invalid order side: {side}"}

        if not order_type:
            return {"status": "rejected", "order_id": None, "message": "Order type is empty."}

        with self.lock:
            if not self._can_place_order(side):
                if not self.running:
                    return {
                        "status": "rejected", "order_id": None,
                        "message": "Trading execution layer is not running.",
                    }
                if self.authentication_status == AUTH_FAILED:
                    return {
                        "status": "rejected", "order_id": None,
                        "message": "Live trading rejected: exchange authentication failed.",
                        "reason": "authentication_failed",
                    }
                if self.authentication_status == AUTHORIZATION_FAILED:
                    return {
                        "status": "rejected", "order_id": None,
                        "message": "Live trading rejected: exchange authorization failed.",
                        "reason": "authorization_failed",
                    }
                if (
                    self.exchange_name == "nobitex"
                    and side == "buy"
                    and normalize_execution_mode(getattr(self.config, "execution_mode", PAPER)) != LIVE
                ):
                    return {
                        "status": "rejected", "order_id": None,
                        "message": "Paper mode is active; switch to Live and press Start before sending Nobitex BUY orders.",
                        "reason": "paper_mode",
                    }
                if self.exchange_name == "nobitex" and not bool(
                    getattr(self, "live_order_activation", False)
                ):
                    return {
                        "status": "rejected", "order_id": None,
                        "message": "Live order activation is not armed. Switch to Live and press Start.",
                        "reason": "live_activation_required",
                    }
                if not self.execution_enabled or not self.live_trading_available:
                    return {
                        "status": "rejected", "order_id": None,
                        "message": "Live trading is currently unavailable.",
                        "reason": "live_trading_unavailable",
                    }
                if self.balance_status != BALANCE_AVAILABLE:
                    return {
                        "status": "rejected", "order_id": None,
                        "message": "Account balance is unavailable. Order execution is blocked safely.",
                        "reason": "balance_unavailable",
                    }
                return {
                    "status": "rejected", "order_id": None,
                    "message": "Trading execution is disabled.",
                }

        execution_symbol = self.normalize_symbol_for_execution(symbol)

        # v6.6 live BUY guard: validate estimated quote notional against
        # fresh balance and configured position caps before network mutation.
        client_order_id = kwargs.pop("client_order_id", None)
        if self.exchange_name == "nobitex" and side == "buy":
            ref_price = _safe_float(price, None)
            if ref_price is None or ref_price <= 0:
                try:
                    ticker = self.exchange.get_ticker(execution_symbol)
                    ref_price = _safe_float(ticker.get("ask") or ticker.get("Ask") or ticker.get("last") or ticker.get("price"), None)
                except Exception as exc:
                    return {"status":"rejected","order_id":None,"message":f"Live buy blocked: price unavailable ({exc}).","reason":"price_unavailable"}
            if ref_price is None or ref_price <= 0:
                return {"status":"rejected","order_id":None,"message":"Live buy blocked: invalid reference price.","reason":"invalid_reference_price"}
            notional = quantity_value * ref_price
            max_notional = _safe_float(getattr(self.config, "max_notional_quote", None), None)
            max_pct = _safe_float(getattr(self.config, "max_position_pct", None), None)
            min_notional = _safe_float(getattr(self.config, "min_notional_quote", 0.0), 0.0) or 0.0
            if min_notional > 0 and notional < min_notional:
                return {"status":"rejected","order_id":None,"message":f"Live buy blocked: notional {notional:.2f} below minimum {min_notional:.2f}.","reason":"below_min_notional"}
            if max_notional and max_notional > 0 and notional > max_notional:
                return {"status":"rejected","order_id":None,"message":f"Live buy blocked: notional {notional:.2f} exceeds cap {max_notional:.2f}.","reason":"max_notional_exceeded"}
            equity = self.get_balance_fresh(self.quote_currency)
            if equity is None or equity <= 0:
                return {"status":"rejected","order_id":None,"message":"Live buy blocked: fresh quote balance unavailable.","reason":"balance_unavailable"}
            if max_pct and max_pct > 0 and notional > equity * max_pct / 100.0:
                return {"status":"rejected","order_id":None,"message":f"Live buy blocked: notional {notional:.2f} exceeds {max_pct:.2f}% position cap.","reason":"max_position_pct_exceeded"}

            # The SignalTracker applies the authoritative managed-position
            # exposure calculation.  Keep a second, execution-layer guard
            # using the latest reconciled Nobitex snapshot when available so
            # a direct caller cannot exceed the configured total exposure
            # cap.  If no snapshot exists, the existing quote-balance and
            # per-order caps still fail closed; the tracker must size the
            # order before it reaches this method.
            max_total_pct = _safe_float(
                getattr(self.config, "max_total_exposure_pct", None), None
            )
            snapshot = getattr(getattr(self, "portfolio_manager", None), "last_snapshot", None)
            if max_total_pct and max_total_pct > 0 and isinstance(snapshot, dict):
                total_equity = _safe_float(
                    snapshot.get("portfolio_value_quote")
                    or snapshot.get("portfolio_total_value_quote"), equity
                ) or equity
                held_exposure = _safe_float(snapshot.get("valued_assets_quote"), 0.0) or 0.0
                exposure_cap = total_equity * max_total_pct / 100.0
                if held_exposure + notional > exposure_cap + 1e-9:
                    return {
                        "status": "rejected", "order_id": None,
                        "message": (
                            f"Live buy blocked: total exposure {held_exposure + notional:.2f} "
                            f"would exceed {max_total_pct:.2f}% cap ({exposure_cap:.2f})."
                        ),
                        "reason": "max_total_exposure_exceeded",
                    }
            fee_pct = max(0.0, _safe_float(getattr(self.config, "trading_fee_pct", 0.25), 0.25) or 0.25)
            if notional > equity * (1.0 - fee_pct / 100.0):
                return {"status":"rejected","order_id":None,"message":"Live buy blocked: order plus estimated fee exceeds available quote balance.","reason":"insufficient_quote_after_fee"}
        if execution_symbol != symbol:
            logger.info("Execution symbol normalized: %s -> %s", symbol, execution_symbol)

        if self.exchange is None:
            return {"status": "rejected", "order_id": None, "message": "Exchange is not initialized."}

        if self.exchange_name == "nobitex":
            try:
                if not self.exchange.is_symbol_supported(execution_symbol):
                    logger.warning("Order rejected: unsupported Nobitex market %s", execution_symbol)
                    return {
                        "status": "rejected", "order_id": None,
                        "message": f"Unsupported Nobitex market: {execution_symbol}",
                        "reason": "unsupported_market",
                    }
            except Exception as exc:
                logger.warning(
                    "Order rejected: could not validate Nobitex market %s: %s",
                    execution_symbol, exc,
                )
                return {
                    "status": "rejected", "order_id": None,
                    "message": "Could not validate Nobitex market availability.",
                    "reason": "market_validation_failed",
                }

        try:
            client_order_id = self.idempotency.prepare(
                symbol=execution_symbol, side=side, order_type=order_type,
                amount=quantity_value, price=price, client_order_id=client_order_id,
            )
            raw_order = self.exchange.place_order(
                symbol=execution_symbol,
                side=side,
                order_type=order_type,
                quantity=quantity_value,
                price=price,
                client_order_id=client_order_id,
                **kwargs,
            )

            if raw_order is None:
                raw_order = {
                    "status": "unknown", "order_id": None,
                    "message": "Exchange returned no order response.",
                }
            elif not isinstance(raw_order, dict):
                raw_order = {
                    "status": "unknown", "order_id": None,
                    "raw_response": raw_order,
                }

            status = str(raw_order.get("status", "") or "").strip().lower()
            if not status:
                status = str(raw_order.get("state", "") or "").strip().lower()
            status = status or "submitted"
            raw_order.setdefault("client_order_id", client_order_id)
            if self.trade_accounting is not None:
                try:
                    accounting_result = self.trade_accounting.ingest_order(raw_order)
                    raw_order["accounting"] = accounting_result
                    if accounting_result.get("status") == "recorded":
                        logger.info(
                            "Actual-fill accounting recorded: %s %s qty=%s price=%s fee=%s %s complete=%s",
                            side.upper(), execution_symbol,
                            accounting_result.get("quantity"), accounting_result.get("price"),
                            accounting_result.get("fee"), accounting_result.get("fee_currency"),
                            accounting_result.get("accounting_complete"),
                        )
                except Exception as accounting_exc:
                    logger.warning("Trade accounting did not block order execution: %s", accounting_exc)
                    raw_order["accounting"] = {
                        "status": "error", "accounting_complete": False,
                        "reason": str(accounting_exc),
                    }
            self.idempotency.confirm(
                client_order_id, exchange_order_id=str(raw_order.get("order_id") or "") or None,
                status=status,
                filled_amount=_safe_float(raw_order.get("executed_qty") or raw_order.get("matched_amount"), None),
                raw_response=raw_order,
            )
            with self.lock:
                self.last_order = raw_order
                self.last_order_timestamp = time.time()

            
            if not status:
                status = str(raw_order.get("state", "") or "").strip().lower()

            if status in _ACCEPTED_STATUSES:
                self.invalidate_balance_cache()

            if status in _FILLED_STATUSES:
                logger.info(
                    "Order executed successfully: %s %s %.8f",
                    side.upper(), execution_symbol, quantity_value,
                )
                refreshed_balance = self.get_balance_fresh(self.quote_currency)
                if refreshed_balance is None:
                    logger.warning(
                        "Order was successful, but post-order balance refresh failed. "
                        "Keeping order result intact."
                    )
                    raw_order["balance_refresh"] = "unavailable"
                else:
                    raw_order["balance_after"] = refreshed_balance

            return raw_order

        except Exception as exc:
            status_code = _status_code_from_exception(exc)
            with self.lock:
                self.last_error = str(exc)
                self.last_error_timestamp = time.time()

            if _is_auth_error(exc):
                try:
                    self.idempotency.mark_failed(client_order_id, str(exc))
                except Exception:
                    pass
                with self.lock:
                    self._mark_auth_failed(exc)
                return {
                    "status": "rejected", "order_id": None,
                    "message": f"Authentication failed (HTTP {status_code or 401}).",
                    "reason": "authentication_failed",
                }

            if _is_authorization_error(exc):
                try:
                    self.idempotency.mark_failed(client_order_id, str(exc))
                except Exception:
                    pass
                with self.lock:
                    self._mark_authorization_failed(exc)
                return {
                    "status": "rejected", "order_id": None,
                    "message": f"Authorization failed (HTTP {status_code or 403}).",
                    "reason": "authorization_failed",
                }

            if _is_rate_limit_error(exc):
                try:
                    self.idempotency.confirm(client_order_id, status="unknown", raw_response={"error": str(exc)})
                except Exception:
                    pass
                return {
                    "status": "rejected", "order_id": None,
                    "message": "Exchange rate limit reached.",
                    "reason": "rate_limit",
                }

            if _is_server_error(exc) or _is_network_error(exc):
                try:
                    self.idempotency.confirm(client_order_id, status="unknown", raw_response={"error": str(exc)})
                except Exception:
                    pass
                return {
                    "status": "unknown", "order_id": None, "client_order_id": client_order_id,
                    "message": str(exc), "reason": "exchange_unavailable_reconcile_required",
                }

            if _is_client_error(exc):
                try:
                    self.idempotency.mark_failed(client_order_id, str(exc))
                except Exception:
                    pass
                # FIX v6.4.0: surface the exchange's own 4xx message.
                return {
                    "status": "rejected", "order_id": None,
                    "message": str(exc),
                    "reason": "client_error",
                }

            logger.error("place_order failed for %s: %s", execution_symbol, exc)
            return {"status": "error", "order_id": None, "message": str(exc)}

    def get_usdt_irt_rate(self, rows: Optional[List[Dict[str, Any]]] = None) -> Optional[float]:
        if rows is None:
            rows = self.get_all_market_stats("IRT")
        for row in rows or []:
            if str(row.get("Symbol", "")).upper() == "USDT":
                ask = _safe_float(row.get("Ask"))
                if ask and ask > 0:
                    return ask
                price = _safe_float(row.get("Price"))
                if price and price > 0:
                    return price
        return None

    def get_all_market_stats(self, quote: Optional[str] = None):
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        if not hasattr(self.exchange, "get_all_market_stats"):
            return []
        return self.exchange.get_all_market_stats(quote or self.quote_currency)

    def get_ticker(self, symbol: str) -> Dict[str, Any]:
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        execution_symbol = self.normalize_symbol_for_execution(symbol)
        return self.exchange.get_ticker(execution_symbol)

    def get_klines(
        self,
        symbol: str,
        interval: Optional[str] = None,
        limit: Optional[int] = None,
    ):
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        execution_symbol = self.normalize_symbol_for_execution(symbol)

        if interval is None:
            interval = getattr(self.config, "candle_interval", "15m")
        if limit is None:
            limit = getattr(self.config, "kline_limit", 100)

        try:
            return self.exchange.get_klines(execution_symbol, interval=interval, limit=limit)
        except TypeError:
            return self.exchange.get_klines(execution_symbol, interval, limit)

    def get_order_book(self, symbol: str, limit: int = 20):
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        execution_symbol = self.normalize_symbol_for_execution(symbol)
        try:
            return self.exchange.get_order_book(execution_symbol, limit=limit)
        except TypeError:
            return self.exchange.get_order_book(execution_symbol, limit)

    def is_symbol_supported(self, symbol: str) -> bool:
        if self.exchange is None:
            return False
        execution_symbol = self.normalize_symbol_for_execution(symbol)
        try:
            return bool(self.exchange.is_symbol_supported(execution_symbol))
        except Exception as exc:
            logger.debug("Symbol support check failed for %s: %s", execution_symbol, exc)
            return False

    def cancel_order(self, order_id: str, symbol: Optional[str] = None):
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        if not hasattr(self.exchange, "cancel_order"):
            raise NotImplementedError("Exchange does not implement cancel_order.")

        execution_symbol = (
            self.normalize_symbol_for_execution(symbol) if symbol else symbol
        )
        try:
            result = self.exchange.cancel_order(order_id=order_id, symbol=execution_symbol)
        except TypeError:
            result = self.exchange.cancel_order(order_id, execution_symbol)
        self.invalidate_balance_cache()
        return result

    def get_open_orders(self, symbol: Optional[str] = None):
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        if not hasattr(self.exchange, "get_open_orders"):
            return []
        execution_symbol = (
            self.normalize_symbol_for_execution(symbol) if symbol else symbol
        )
        try:
            return self.exchange.get_open_orders(execution_symbol)
        except TypeError:
            return self.exchange.get_open_orders()

    def get_order_status(self, order_id: str, symbol: Optional[str] = None):
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        if not hasattr(self.exchange, "get_order_status"):
            raise NotImplementedError("Exchange does not implement get_order_status.")

        execution_symbol = (
            self.normalize_symbol_for_execution(symbol) if symbol else symbol
        )
        try:
            return self.exchange.get_order_status(order_id=order_id, symbol=execution_symbol)
        except TypeError:
            return self.exchange.get_order_status(order_id, execution_symbol)

    def get_order_history(self, symbol: Optional[str] = None):
        if self.exchange is None:
            raise RuntimeError("Exchange is not initialized.")
        if not hasattr(self.exchange, "get_order_history"):
            return []
        execution_symbol = (
            self.normalize_symbol_for_execution(symbol) if symbol else symbol
        )
        try:
            return self.exchange.get_order_history(execution_symbol)
        except TypeError:
            return self.exchange.get_order_history()

    def get_positions(self):
        if self.exchange is None:
            return []
        if not hasattr(self.exchange, "get_positions"):
            return []
        try:
            return self.exchange.get_positions()
        except Exception as exc:
            logger.warning("get_positions failed: %s", exc)
            return []

    def get_connection_status(self) -> Dict[str, Any]:
        status = self.get_status()
        try:
            exchange_status_method = getattr(self.exchange, "get_connection_status", None)
            if callable(exchange_status_method):
                exchange_status = exchange_status_method()
                if isinstance(exchange_status, dict):
                    status.update(exchange_status)
        except Exception as exc:
            logger.debug("Could not read exchange connection status: %s", exc)
        return status

    def disable_live_trading(self, reason: str = "Disabled by user.") -> None:
        with self.lock:
            self.live_order_activation = False
            self.live_trading_available = False
            self.execution_enabled = False
            self.last_error = reason
            self.last_error_timestamp = time.time()
            logger.warning("Live trading disabled: %s", reason)

    def enable_live_trading(self) -> bool:
        with self.lock:
            if self.exchange_name == "simulator":
                self.live_order_activation = True
                self.live_trading_available = True
                self.execution_enabled = bool(self.running)
                return True
            if normalize_execution_mode(getattr(self.config, "execution_mode", PAPER)) != LIVE:
                logger.warning("Live order activation refused while execution_mode=%s.", getattr(self.config, "execution_mode", PAPER))
                return False

        balance = self.get_balance_fresh(self.quote_currency)
        if balance is None:
            logger.warning(
                "Live trading cannot be enabled because account balance is unavailable."
            )
            return False

        with self.lock:
            if self.authentication_status in (AUTH_FAILED, AUTHORIZATION_FAILED):
                return False
            self.live_trading_available = True
            self.execution_enabled = bool(self.running)
            self.live_order_activation = True
            logger.info("Live trading execution enabled.")
            return True

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.running = False
            self.execution_enabled = False
            self.live_order_activation = False

            try:
                if self.exchange is not None:
                    close_method = getattr(self.exchange, "close", None)
                    if callable(close_method):
                        close_method()
            except Exception as exc:
                logger.warning("Exchange close failed: %s", exc)
            finally:
                self.closed = True
                logger.info("TradingBot closed.")

    def __enter__(self) -> "TradingBot":
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __del__(self):
        try:
            if not getattr(self, "closed", True):
                self.close()
        except Exception:
            pass