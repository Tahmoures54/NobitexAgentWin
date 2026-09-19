# ===== trading/exchange_base.py =====
"""
Base exchange interface.

The interface deliberately preserves the original public API while adding
safe account-state handling. In particular, an unavailable balance must
never silently become zero.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from .exceptions import BalanceUnavailableError


class ExchangeBase(ABC):
    """
    Abstract base class for cryptocurrency exchange clients.
    """

    AUTH_UNKNOWN = "UNKNOWN"
    AUTHENTICATED = "AUTHENTICATED"
    AUTH_FAILED = "AUTH_FAILED"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"

    BALANCE_UNKNOWN = "UNKNOWN"
    BALANCE_AVAILABLE = "AVAILABLE"
    BALANCE_UNAVAILABLE = "UNAVAILABLE"

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = True,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self._testnet = bool(testnet)

        self.session = None
        self._logger = logging.getLogger(self.__class__.__name__)

        self.authentication_status = self.AUTH_UNKNOWN
        self.balance_status = self.BALANCE_UNKNOWN
        self.last_balance: Optional[float] = None
        self.last_balance_error: Optional[str] = None
        self.last_balance_timestamp: Optional[float] = None

        self.live_trading_available = True
        self.last_error: Optional[str] = None

    @property
    def testnet(self) -> bool:
        return self._testnet

    @testnet.setter
    def testnet(self, value: bool) -> None:
        self._testnet = bool(value)

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def mark_authenticated(self) -> None:
        self.authentication_status = self.AUTHENTICATED
        self.live_trading_available = True
        self.last_error = None

    def mark_auth_failed(self, message: str) -> None:
        self.authentication_status = self.AUTH_FAILED
        self.live_trading_available = False
        self.last_error = message

    def mark_authorization_failed(self, message: str) -> None:
        self.authentication_status = self.AUTHORIZATION_FAILED
        self.live_trading_available = False
        self.last_error = message

    def mark_balance_available(self, value: float) -> None:
        value = float(value)

        if value < 0:
            raise ValueError("Balance cannot be negative.")

        self.balance_status = self.BALANCE_AVAILABLE
        self.last_balance = value
        self.last_balance_error = None
        self.last_balance_timestamp = time.time()

    def mark_balance_unavailable(self, message: str) -> None:
        self.balance_status = self.BALANCE_UNAVAILABLE
        self.last_balance_error = message
        self.last_balance_timestamp = time.time()

    def get_connection_status(self) -> Dict[str, Any]:
        return {
            "authentication_status": self.authentication_status,
            "balance_status": self.balance_status,
            "last_balance": self.last_balance,
            "last_balance_error": self.last_balance_error,
            "last_balance_timestamp": self.last_balance_timestamp,
            "live_trading_available": self.live_trading_available,
            "last_error": self.last_error,
            "testnet": self.testnet,
        }

    # ------------------------------------------------------------------
    # Low-level exchange methods
    # ------------------------------------------------------------------

    @abstractmethod
    def _sign_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
    ) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        signed: bool = False,
    ) -> Any:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Symbols
    # ------------------------------------------------------------------

    @abstractmethod
    def resolve_symbol(self, symbol: str) -> str:
        raise NotImplementedError

    @abstractmethod
    def is_symbol_supported(self, symbol: str) -> bool:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    @abstractmethod
    def get_ticker(self, symbol: str) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 500,
    ) -> List[Dict]:
        raise NotImplementedError

    @abstractmethod
    def get_order_book(
        self,
        symbol: str,
        limit: int = 100,
    ) -> Dict:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Balances
    # ------------------------------------------------------------------

    @abstractmethod
    def get_balances(self) -> Dict[str, float]:
        raise NotImplementedError

    def get_balance(self, asset: str) -> float:
        """
        Return a valid balance.

        Missing asset is legitimately zero.
        Exchange/API failure is NOT zero.
        """
        balances = self.get_balances()

        if balances is None:
            raise BalanceUnavailableError(
                "Exchange returned no balance data."
            )

        key = (asset or "").upper()

        try:
            value = float(balances.get(key, 0.0))
        except (TypeError, ValueError) as exc:
            raise BalanceUnavailableError(
                f"Invalid balance returned for {key}."
            ) from exc

        if value < 0:
            raise BalanceUnavailableError(
                f"Exchange returned a negative balance for {key}."
            )

        self.mark_balance_available(value)
        return value

    # ------------------------------------------------------------------
    # Trading
    # ------------------------------------------------------------------

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: float = None,
        stop_price: float = None,
        time_in_force: str = "GTC",
        **kwargs,
    ) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(
        self,
        symbol: str,
        order_id: str,
    ) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def get_open_orders(
        self,
        symbol: Optional[str] = None,
    ) -> List[Dict]:
        raise NotImplementedError

    @abstractmethod
    def get_order_status(
        self,
        symbol: str,
        order_id: str,
    ) -> Dict:
        raise NotImplementedError

    @abstractmethod
    def get_order_history(
        self,
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    @abstractmethod
    def get_positions(self) -> List[Dict]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _timestamp(self) -> int:
        return int(time.time() * 1000)

    def _generate_signature(self, query_string: str) -> str:
        return hmac.new(
            (self.api_secret or "").encode("utf-8"),
            (query_string or "").encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _format_symbol(self, symbol: str) -> str:
        return (symbol or "").upper()

    def _parse_order(self, raw_order: Dict) -> Dict:
        return raw_order

    def close(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception as exc:
                self._logger.debug(
                    "Error closing session: %s",
                    exc,
                )

        self._logger.info("Exchange client closed.")

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"api_key={'***' if self.api_key else ''}, "
            f"testnet={self.testnet})"
        )