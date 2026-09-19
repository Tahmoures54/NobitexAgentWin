"""
Trading / Exchange exceptions and runtime state definitions.

This module provides normalized exceptions for all exchange integrations.
The main goal is to prevent authentication/API failures from being
mistakenly interpreted as zero account balance or trading losses.
"""

from __future__ import annotations

from typing import Optional


class ExchangeError(Exception):
    """Base exception for exchange-related failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.fatal = fatal

    def __str__(self) -> str:
        return self.message


class AuthenticationError(ExchangeError):
    """HTTP 401 / invalid API credentials."""

    def __init__(self, message: str = "Exchange authentication failed.") -> None:
        super().__init__(
            message,
            status_code=401,
            retryable=False,
            fatal=True,
        )


class AuthorizationError(ExchangeError):
    """HTTP 403 / insufficient API permissions."""

    def __init__(self, message: str = "Exchange authorization failed.") -> None:
        super().__init__(
            message,
            status_code=403,
            retryable=False,
            fatal=True,
        )


class RateLimitError(ExchangeError):
    """HTTP 429 / rate limit."""

    def __init__(
        self,
        message: str = "Exchange rate limit exceeded.",
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(
            message,
            status_code=429,
            retryable=True,
            fatal=False,
        )
        self.retry_after = retry_after


class NetworkExchangeError(ExchangeError):
    """Timeout/network connectivity problem."""

    def __init__(self, message: str = "Exchange network error.") -> None:
        super().__init__(
            message,
            retryable=True,
            fatal=False,
        )


class ServerExchangeError(ExchangeError):
    """Exchange-side 5xx error."""

    def __init__(
        self,
        message: str = "Exchange server error.",
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            retryable=True,
            fatal=False,
        )


class BalanceUnavailableError(ExchangeError):
    """
    Account balance could not be obtained.

    IMPORTANT:
    This must never be converted into 0.0 by risk/execution code.
    """

    def __init__(
        self,
        message: str = "Account balance is currently unavailable.",
        *,
        cause: Optional[BaseException] = None,
    ) -> None:
        super().__init__(
            message,
            retryable=True,
            fatal=False,
        )
        self.cause = cause


class ConfigurationError(ExchangeError):
    """Invalid trading/exchange configuration."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            retryable=False,
            fatal=True,
        )


class OrderExecutionError(ExchangeError):
    """Order submission/execution failure."""

    def __init__(
        self,
        message: str,
        *,
        order_id: Optional[str] = None,
        possibly_filled: bool = False,
    ) -> None:
        super().__init__(
            message,
            retryable=False,
            fatal=False,
        )
        self.order_id = order_id
        self.possibly_filled = possibly_filled