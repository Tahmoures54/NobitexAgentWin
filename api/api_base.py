# api/api_base.py
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from typing import Any, Dict, Literal, Optional, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.config import (
    DEFAULT_USER_AGENT,
    MAX_RETRIES,
    REQUEST_TIMEOUT,
    RETRY_DELAY,
)

# ──────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────

class ApiException(Exception):
    """پایه‌ای‌ترین خطای کتابخانه."""


class ApiConnectionError(ApiException):
    """خطای اتصال به شبکه."""


class ApiTimeoutError(ApiException):
    """خطای timeout."""


class ApiHttpError(ApiException):
    """خطای HTTP با status code مشخص."""

    def __init__(
        self,
        status_code: int,
        message: str,
        response: Optional[requests.Response] = None,
    ):
        self.status_code = status_code
        self.response = response
        super().__init__(f"HTTP {status_code}: {message}")


class ApiRateLimitError(ApiHttpError):
    """خطای 429 - تعداد درخواست‌ها از حد مجاز گذشته."""

    def __init__(
        self,
        retry_after: int,
        response: Optional[requests.Response] = None,
    ):
        self.retry_after = retry_after
        super().__init__(
            429,
            f"Rate limited. Retry after {retry_after}s.",
            response,
        )


class ApiAuthError(ApiHttpError):
    """خطای 401/403 - احراز هویت یا مجوز نادرست."""

    def __init__(self, response: Optional[requests.Response] = None):
        super().__init__(
            response.status_code if response else 401,
            "Authentication failed. Check your API key.",
            response,
        )


# ──────────────────────────────────────────────────────────────
# Metrics
# ──────────────────────────────────────────────────────────────

class ClientMetrics:
    """
    ردیابی thread-safe آمار درخواست‌ها.

    از dataclass استفاده نمی‌کنیم چون Lock با pickle و copy مشکل دارد.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.total_requests: int = 0
        self.successful_requests: int = 0
        self.failed_requests: int = 0
        self.retried_requests: int = 0
        self.total_latency_ms: float = 0.0
        self.status_counts: Dict[int, int] = defaultdict(int)

    def record(
        self,
        *,
        success: bool,
        latency_ms: float,
        status: Optional[int] = None,
        retried: bool = False,
    ) -> None:
        """یک درخواست را ثبت می‌کند."""
        with self._lock:
            self.total_requests += 1

            if success:
                self.successful_requests += 1
            else:
                self.failed_requests += 1

            if retried:
                self.retried_requests += 1

            self.total_latency_ms += latency_ms

            if status is not None:
                self.status_counts[status] += 1

    @property
    def average_latency_ms(self) -> float:
        """میانگین latency تمام درخواست‌ها."""
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests

    @property
    def success_rate(self) -> float:
        """نرخ موفقیت بین ۰.۰ تا ۱.۰"""
        if self.total_requests == 0:
            return 0.0
        return self.successful_requests / self.total_requests

    def snapshot(self) -> Dict[str, Any]:
        """یک کپی thread-safe از تمام آمار."""
        with self._lock:
            return {
                "total_requests": self.total_requests,
                "successful_requests": self.successful_requests,
                "failed_requests": self.failed_requests,
                "retried_requests": self.retried_requests,
                "average_latency_ms": round(self.average_latency_ms, 2),
                "success_rate": round(self.success_rate, 4),
                "status_counts": dict(self.status_counts),
            }

    def reset(self) -> None:
        """بازنشانی تمام آمار."""
        with self._lock:
            self.total_requests = 0
            self.successful_requests = 0
            self.failed_requests = 0
            self.retried_requests = 0
            self.total_latency_ms = 0.0
            self.status_counts = defaultdict(int)

    def __repr__(self) -> str:
        return (
            f"ClientMetrics("
            f"total={self.total_requests}, "
            f"success={self.successful_requests}, "
            f"failed={self.failed_requests}, "
            f"avg_latency={self.average_latency_ms:.1f}ms"
            f")"
        )


# ──────────────────────────────────────────────────────────────
# HTTP Method type
# ──────────────────────────────────────────────────────────────

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


# ──────────────────────────────────────────────────────────────
# Base Client
# ──────────────────────────────────────────────────────────────

class ApiBaseClient:
    """
    کلاس پایه برای تمام کلاینت‌های API.

    ویژگی‌ها:
        - مدیریت retry با backoff خطی (با سقف ۱۵ ثانیه)
        - پشتیبانی از چند روش احراز هویت
        - ثبت آمار درخواست‌ها (اختیاری)
        - لاگ‌گذاری کامل با request_id
        - پشتیبانی از context manager
        - پشتیبانی از GET, POST, PUT, PATCH, DELETE
    """

    BASE_URL: Optional[str] = None

    def __init__(
        self,
        api_key: Optional[str] = None,
        auth_method: Literal["api_key", "bearer", "none"] = "api_key",
        auth_header: str = "X-API-KEY",
        timeout: float = REQUEST_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        backoff_factor: float = 2.0,
        enable_metrics: bool = False,
        user_agent: Optional[str] = None,
    ):
        """
        Parameters
        ----------
        api_key:
            کلید API (اختیاری برای برخی سرویس‌ها).
        auth_method:
            روش احراز هویت: 'api_key', 'bearer', یا 'none'.
        auth_header:
            نام هدر برای api_key (پیش‌فرض 'X-API-KEY').
        timeout:
            مدت‌زمان انتظار به ثانیه.
        max_retries:
            حداکثر تعداد تلاش مجدد.
        backoff_factor:
            ضریب backoff خطی: wait = backoff_factor * attempt
        enable_metrics:
            اگر True باشد، آمار درخواست‌ها ثبت می‌شود.
        user_agent:
            User-Agent سفارشی. اگر None باشد، از DEFAULT_USER_AGENT استفاده می‌شود.
        """
        self.api_key = api_key
        self.auth_method = auth_method
        self.auth_header = auth_header
        self.timeout = timeout
        self.max_retries = max(1, max_retries)
        self.backoff_factor = backoff_factor
        self.retry_delay = float(RETRY_DELAY)  # from config, used as cap
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.logger = logging.getLogger(self.__class__.__name__)

        self._metrics: Optional[ClientMetrics] = (
            ClientMetrics() if enable_metrics else None
        )

        self.session = self._build_session()

    # ──────────────────────────────────────────
    # Session
    # ──────────────────────────────────────────

    def _build_session(self) -> requests.Session:
        """
        یک Session با connection pooling بهینه می‌سازد.
        retry در سطح urllib3 غیرفعال است تا ما خودمان مدیریت کنیم.
        """
        session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=20,
            max_retries=Retry(total=0),  # retry را خودمان مدیریت می‌کنیم
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    # ──────────────────────────────────────────
    # Auth
    # ──────────────────────────────────────────

    def _apply_auth(self, headers: Dict[str, str]) -> None:
        """هدرهای احراز هویت را اضافه می‌کند."""
        if not self.api_key or self.auth_method == "none":
            return

        if self.auth_method == "api_key":
            headers[self.auth_header] = self.api_key
        elif self.auth_method == "bearer":
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            self.logger.warning(f"Unknown auth_method: '{self.auth_method}'")

    # ──────────────────────────────────────────
    # Core Request
    # ──────────────────────────────────────────

    def _request(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        method: HttpMethod = "GET",
        json_data: Any = None,
        timeout: Optional[float] = None,
        raise_on_error: bool = False,
    ) -> Any:
        """
        یک HTTP request ارسال می‌کند.

        Parameters
        ----------
        url:
            آدرس کامل endpoint.
        params:
            پارامترهای query string.
        headers:
            هدرهای اضافی (با هدرهای پیش‌فرض merge می‌شود).
        method:
            متد HTTP: GET, POST, PUT, PATCH, DELETE
        json_data:
            داده JSON برای body درخواست (برای POST/PUT/PATCH).
        timeout:
            override برای timeout کلاس.
        raise_on_error:
            اگر True باشد، به جای None، exception می‌اندازد.

        Returns
        -------
        dict/list یا None در صورت خطا (وقتی raise_on_error=False).
        """
        request_id = str(uuid.uuid4())[:8]
        effective_timeout = timeout if timeout is not None else self.timeout
        request_start = time.time()
        retried = False

        final_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if headers:
            final_headers.update(headers)
        self._apply_auth(final_headers)

        self.logger.debug(
            f"[{request_id}] {method} {url} | "
            f"params={params} | timeout={effective_timeout}s"
        )

        for attempt in range(1, self.max_retries + 1):
            attempt_start = time.time()

            try:
                resp = self._send(
                    method=method,
                    url=url,
                    headers=final_headers,
                    params=params,
                    json_data=json_data,
                    timeout=effective_timeout,
                )

            # ── شبکه / اتصال ─────────────────
            except requests.Timeout as e:
                wait = self._backoff(attempt)
                self.logger.warning(
                    f"[{request_id}] Timeout on attempt {attempt}/{self.max_retries}. "
                    f"Waiting {wait:.1f}s..."
                )
                if attempt < self.max_retries:
                    retried = True
                    time.sleep(wait)
                    continue

                total_latency = (time.time() - request_start) * 1000
                self._record_metrics(
                    success=False,
                    latency_ms=total_latency,
                    status=None,
                    retried=retried,
                )
                if raise_on_error:
                    raise ApiTimeoutError(
                        f"[{request_id}] Request timed out after {self.max_retries} attempts."
                    ) from e
                return None

            except requests.ConnectionError as e:
                wait = self._backoff(attempt)
                self.logger.warning(
                    f"[{request_id}] Connection error on attempt {attempt}/{self.max_retries}. "
                    f"Waiting {wait:.1f}s..."
                )
                if attempt < self.max_retries:
                    retried = True
                    time.sleep(wait)
                    continue

                total_latency = (time.time() - request_start) * 1000
                self._record_metrics(
                    success=False,
                    latency_ms=total_latency,
                    status=None,
                    retried=retried,
                )
                if raise_on_error:
                    raise ApiConnectionError(
                        f"[{request_id}] Connection failed: {e}"
                    ) from e
                return None

            except Exception as e:
                self.logger.error(
                    f"[{request_id}] Unexpected error on attempt {attempt}: {e}",
                    exc_info=True,
                )
                total_latency = (time.time() - request_start) * 1000
                self._record_metrics(
                    success=False,
                    latency_ms=total_latency,
                    status=None,
                    retried=retried,
                )
                if raise_on_error:
                    raise ApiException(
                        f"[{request_id}] Unexpected error: {e}"
                    ) from e
                return None

            # ── پاسخ HTTP ─────────────────────
            attempt_latency = (time.time() - attempt_start) * 1000
            status = resp.status_code

            # 401 / 403
            if status in (401, 403):
                self.logger.error(
                    f"[{request_id}] Auth error HTTP {status}."
                )
                total_latency = (time.time() - request_start) * 1000
                self._record_metrics(
                    success=False,
                    latency_ms=total_latency,
                    status=status,
                    retried=retried,
                )
                if raise_on_error:
                    raise ApiAuthError(resp)
                return None

            # 429 Rate limit
            if status == 429:
                retry_after = int(resp.headers.get("Retry-After", self._backoff(attempt)))
                self.logger.warning(
                    f"[{request_id}] Rate limited (429). "
                    f"Retry-After={retry_after}s. "
                    f"Attempt {attempt}/{self.max_retries}."
                )
                if attempt < self.max_retries:
                    retried = True
                    time.sleep(retry_after)
                    continue

                total_latency = (time.time() - request_start) * 1000
                self._record_metrics(
                    success=False,
                    latency_ms=total_latency,
                    status=429,
                    retried=retried,
                )
                if raise_on_error:
                    raise ApiRateLimitError(retry_after, resp)
                return None

            # 5xx Server errors
            if status >= 500:
                wait = self._backoff(attempt)
                self.logger.warning(
                    f"[{request_id}] Server error HTTP {status} on attempt "
                    f"{attempt}/{self.max_retries}. Waiting {wait:.1f}s..."
                )
                if attempt < self.max_retries:
                    retried = True
                    time.sleep(wait)
                    continue

                total_latency = (time.time() - request_start) * 1000
                self._record_metrics(
                    success=False,
                    latency_ms=total_latency,
                    status=status,
                    retried=retried,
                )
                if raise_on_error:
                    raise ApiHttpError(status, resp.reason, resp)
                return None

            # سایر خطاهای HTTP (4xx)
            if not resp.ok:
                self.logger.error(
                    f"[{request_id}] HTTP {status}: {resp.reason}"
                )
                total_latency = (time.time() - request_start) * 1000
                self._record_metrics(
                    success=False,
                    latency_ms=total_latency,
                    status=status,
                    retried=retried,
                )
                if raise_on_error:
                    raise ApiHttpError(status, resp.reason, resp)
                return None

            # ── موفق ──────────────────────────
            total_latency = (time.time() - request_start) * 1000
            self._record_metrics(
                success=True,
                latency_ms=total_latency,
                status=status,
                retried=retried,
            )
            self.logger.debug(
                f"[{request_id}] Success HTTP {status} | "
                f"latency={total_latency:.1f}ms | "
                f"attempts={attempt}"
            )

            try:
                return resp.json()
            except ValueError:
                self.logger.error(
                    f"[{request_id}] Response is not valid JSON. "
                    f"Content-Type: {resp.headers.get('Content-Type')}"
                )
                if raise_on_error:
                    raise ApiException(
                        f"[{request_id}] Invalid JSON response."
                    )
                return None

        return None

    # ──────────────────────────────────────────
    # HTTP Methods
    # ──────────────────────────────────────────

    def _send(
        self,
        method: HttpMethod,
        url: str,
        headers: Dict[str, str],
        params: Optional[Dict],
        json_data: Any,
        timeout: float,
    ) -> requests.Response:
        """
        درخواست HTTP را با متد مناسب ارسال می‌کند.
        """
        kwargs = dict(
            headers=headers,
            params=params,
            timeout=timeout,
        )

        if method in ("POST", "PUT", "PATCH"):
            kwargs["json"] = json_data

        return self.session.request(method, url, **kwargs)

    def get(self, url: str, **kwargs) -> Any:
        """درخواست GET."""
        return self._request(url, method="GET", **kwargs)

    def post(self, url: str, json_data: Any = None, **kwargs) -> Any:
        """درخواست POST."""
        return self._request(url, method="POST", json_data=json_data, **kwargs)

    def put(self, url: str, json_data: Any = None, **kwargs) -> Any:
        """درخواست PUT."""
        return self._request(url, method="PUT", json_data=json_data, **kwargs)

    def patch(self, url: str, json_data: Any = None, **kwargs) -> Any:
        """درخواست PATCH."""
        return self._request(url, method="PATCH", json_data=json_data, **kwargs)

    def delete(self, url: str, **kwargs) -> Any:
        """درخواست DELETE."""
        return self._request(url, method="DELETE", **kwargs)

    # ──────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────

    def _backoff(self, attempt: int) -> float:
        """
        زمان انتظار بین تلاش‌ها با backoff خطی.
        wait = backoff_factor * attempt, capped at 15s.

        با مقادیر پیش‌فرض:
        attempt 1 → 2s, 2 → 4s, 3 → 6s, 4 → 8s, 5 → 10s
        """
        wait = self.backoff_factor * attempt
        return min(wait, 15.0)

    def _record_metrics(
        self,
        *,
        success: bool,
        latency_ms: float,
        status: Optional[int],
        retried: bool,
    ) -> None:
        """اگر metrics فعال باشد، آمار را ثبت می‌کند."""
        if self._metrics is not None:
            self._metrics.record(
                success=success,
                latency_ms=latency_ms,
                status=status,
                retried=retried,
            )

    # ──────────────────────────────────────────
    # Metrics API
    # ──────────────────────────────────────────

    @property
    def metrics(self) -> Optional[ClientMetrics]:
        """دسترسی به آمار (None اگر غیرفعال باشد)."""
        return self._metrics

    def get_metrics_snapshot(self) -> Optional[Dict[str, Any]]:
        """یک کپی از آمار فعلی برمی‌گرداند."""
        if self._metrics is None:
            return None
        return self._metrics.snapshot()

    # ──────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────

    def close(self) -> None:
        """Session را می‌بندد."""
        self.session.close()
        self.logger.debug(f"{self.__class__.__name__} session closed.")

    def __enter__(self) -> "ApiBaseClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"auth_method='{self.auth_method}', "
            f"timeout={self.timeout}s, "
            f"max_retries={self.max_retries}"
            f")"
        )