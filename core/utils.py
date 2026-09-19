# core/utils.py
from __future__ import annotations

import math              # ← اضافه شد
import os
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Optional, Tuple, Union

import numpy as np

# ══════════════════════════════════════════════════════════════
# Path Utilities
# ══════════════════════════════════════════════════════════════

def resource_path(relative: str) -> str:
    """
    مسیر مطلق یک فایل را برمی‌گرداند.
    هم در حالت توسعه و هم در PyInstaller کار می‌کند.

    Parameters
    ----------
    relative:
        مسیر نسبی فایل.

    Examples
    --------
    >>> resource_path("assets/logo.png")
    '/home/user/project/assets/logo.png'
    """
    base = getattr(sys, "_MEIPASS", os.path.abspath("."))
    return os.path.join(base, relative)


# ══════════════════════════════════════════════════════════════
# LRU Cache
# ══════════════════════════════════════════════════════════════

class LRUCache:
    """
    Cache thread-safe با اندازه محدود و پشتیبانی از TTL.

    Features:
        - حذف خودکار قدیمی‌ترین آیتم وقتی پر می‌شود (LRU policy)
        - TTL اختیاری: آیتم‌های منقضی‌شده به‌صورت lazy حذف می‌شوند
        - آمار hit/miss
        - thread-safe با Lock

    Examples
    --------
    >>> cache = LRUCache(maxsize=100, ttl=300)
    >>> cache.set("key", {"data": 42})
    >>> cache.get("key")
    {'data': 42}
    >>> len(cache)
    1
    >>> cache.stats()
    {'hits': 1, 'misses': 0, 'size': 1, 'maxsize': 100}
    """

    _MISSING = object()  # sentinel برای تشخیص "نبود" از None

    def __init__(
        self,
        maxsize: int = 200,
        ttl: Optional[float] = None,
    ):
        """
        Parameters
        ----------
        maxsize:
            حداکثر تعداد آیتم‌ها.
        ttl:
            مدت‌زمان زندگی هر آیتم به ثانیه. None یعنی بدون انقضا.
        """
        if maxsize <= 0:
            raise ValueError("maxsize must be a positive integer.")
        if ttl is not None and ttl <= 0:
            raise ValueError("ttl must be a positive number.")

        self._maxsize = maxsize
        self._ttl = ttl

        # مقدار: (value, expire_at_or_None)
        self._store: OrderedDict[str, Tuple[Any, Optional[float]]] = OrderedDict()
        self._lock = threading.Lock()

        self._hits = 0
        self._misses = 0

    # ──────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """
        مقدار کلید را برمی‌گرداند.
        اگر منقضی شده یا وجود نداشته باشد، default برمی‌گرداند.
        """
        with self._lock:
            entry = self._store.get(key, self._MISSING)

            if entry is self._MISSING:
                self._misses += 1
                return default

            value, expire_at = entry

            if expire_at is not None and time.monotonic() > expire_at:
                del self._store[key]
                self._misses += 1
                return default

            self._store.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """
        یک کلید-مقدار را ذخیره می‌کند.

        Parameters
        ----------
        key:
            کلید.
        value:
            مقدار.
        ttl:
            TTL اختیاری برای این آیتم خاص.
            اگر None باشد، از TTL کلاس استفاده می‌شود.
        """
        effective_ttl = ttl if ttl is not None else self._ttl
        expire_at = (
            time.monotonic() + effective_ttl
            if effective_ttl is not None
            else None
        )

        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, expire_at)

            if len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def delete(self, key: str) -> bool:
        """
        یک کلید را حذف می‌کند.

        Returns
        -------
        True اگر کلید وجود داشت و حذف شد، False در غیر این صورت.
        """
        with self._lock:
            if key in self._store:
                del self._store[key]
                return True
            return False

    def clear(self) -> None:
        """تمام آیتم‌ها را پاک می‌کند."""
        with self._lock:
            self._store.clear()

    def invalidate_expired(self) -> int:
        """
        تمام آیتم‌های منقضی‌شده را پاک می‌کند.

        Returns
        -------
        تعداد آیتم‌های حذف‌شده.
        """
        now = time.monotonic()
        removed = 0
        with self._lock:
            expired_keys = [
                k for k, (_, exp) in self._store.items()
                if exp is not None and now > exp
            ]
            for k in expired_keys:
                del self._store[k]
                removed += 1
        return removed

    def stats(self) -> dict:
        """آمار hit/miss و اندازه کش."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total > 0 else 0.0,
                "size": len(self._store),
                "maxsize": self._maxsize,
                "ttl": self._ttl,
            }

    def reset_stats(self) -> None:
        """آمار hit/miss را بازنشانی می‌کند (بدون پاک کردن کش)."""
        with self._lock:
            self._hits = 0
            self._misses = 0

    # ──────────────────────────────────────────
    # Dunder methods
    # ──────────────────────────────────────────

    def __contains__(self, key: str) -> bool:
        """پشتیبانی از `key in cache`."""
        return self.get(key, self._MISSING) is not self._MISSING

    def __len__(self) -> int:
        """تعداد آیتم‌های فعلی (شامل منقضی‌شده‌های هنوز پاک‌نشده)."""
        with self._lock:
            return len(self._store)

    def __repr__(self) -> str:
        with self._lock:
            return (
                f"LRUCache("
                f"size={len(self._store)}/{self._maxsize}, "
                f"ttl={self._ttl}, "
                f"hits={self._hits}, "
                f"misses={self._misses}"
                f")"
            )


# ══════════════════════════════════════════════════════════════
# Type Conversion
# ══════════════════════════════════════════════════════════════

def safe_float(val: Any) -> Optional[float]:
    """
    تبدیل امن هر مقداری به float.

    - اعداد با کاما (مثل '1,234.56') پشتیبانی می‌شوند.
    - NaN و Inf → None
    - None یا مقدار ناقابل تبدیل → None

    Examples
    --------
    >>> safe_float("1,234.56")
    1234.56
    >>> safe_float(float('nan'))
    None
    >>> safe_float("abc")
    None
    """
    if val is None:
        return None
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        f = float(val)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    """
    تبدیل امن به int.

    Examples
    --------
    >>> safe_int("42")
    42
    >>> safe_int("3.7")
    3
    >>> safe_int("abc")
    None
    """
    f = safe_float(val)
    if f is None:
        return default
    return int(f)


def clamp(
    value: Union[int, float],
    min_val: Union[int, float],
    max_val: Union[int, float],
) -> Union[int, float]:
    """
    محدود کردن مقدار بین min و max.

    Examples
    --------
    >>> clamp(150, 0, 100)
    100
    >>> clamp(-5, 0, 100)
    0
    """
    return max(min_val, min(max_val, value))


# ══════════════════════════════════════════════════════════════
# Formatting Utilities
# ══════════════════════════════════════════════════════════════

def fmt(val: Any, fmt_str: str) -> str:
    """
    فرمت‌بندی یک مقدار با format string مشخص.
    در صورت None، NaN، Inf یا خطا، '--' برمی‌گرداند.

    Examples
    --------
    >>> fmt(3.14159, ".2f")
    '3.14'
    >>> fmt(None, ".2f")
    '--'
    >>> fmt(float('inf'), ".2f")
    '--'
    """
    if val is None or val == "--":
        return "--"
    try:
        f = float(val)
        if not np.isfinite(f):
            return "--"
        return format(f, fmt_str)
    except (TypeError, ValueError):
        return str(val)


def fmt_price(val: Any) -> str:
    """
    فرمت‌بندی هوشمند قیمت بر اساس اندازه مقدار.

    - >= 1000   → بدون اعشار    (42,000)
    - >= 1      → 2 رقم اعشار   (1.23)
    - >= 0.01   → 4 رقم اعشار   (0.0045)
    - < 0.01    → 8 رقم اعشار   (0.00001234)

    Examples
    --------
    >>> fmt_price(42000)
    '42,000'
    >>> fmt_price(1.2345)
    '1.23'
    >>> fmt_price(0.000123)
    '0.00012300'
    """
    f = safe_float(val)
    if f is None:
        return "--"

    abs_f = abs(f)

    if abs_f == 0:
        return "0"
    if abs_f >= 1_000:
        return f"{f:,.0f}"
    if abs_f >= 1:
        return f"{f:.2f}"
    if abs_f >= 0.01:
        return f"{f:.4f}"
    return f"{f:.8f}"


def fmt_compact(
    val: Any,
    *,
    decimals: int = 2,
    suffix_B: str = "B",
    suffix_M: str = "M",
    suffix_K: str = "K",
) -> str:
    """
    فرمت‌بندی فشرده اعداد بزرگ (Billion, Million, Thousand).

    Parameters
    ----------
    val:
        مقدار ورودی.
    decimals:
        تعداد ارقام اعشار.
    suffix_B / suffix_M / suffix_K:
        پسوند قابل تنظیم.

    Examples
    --------
    >>> fmt_compact(1_500_000_000)
    '1.50B'
    >>> fmt_compact(2_500_000)
    '2.50M'
    >>> fmt_compact(750_000, decimals=1)
    '750.0K'
    >>> fmt_compact(-1_200_000)
    '-1.20M'
    """
    f = safe_float(val)
    if f is None:
        return "--"

    abs_f = abs(f)
    fmt_str = f".{decimals}f"

    if abs_f >= 1e12:
        return f"{f / 1e12:{fmt_str}}T"
    if abs_f >= 1e9:
        return f"{f / 1e9:{fmt_str}}{suffix_B}"
    if abs_f >= 1e6:
        return f"{f / 1e6:{fmt_str}}{suffix_M}"
    if abs_f >= 1e3:
        return f"{f / 1e3:{fmt_str}}{suffix_K}"
    return f"{f:{fmt_str}}"


def fmt_mcap(cap: Any) -> str:
    """
    فرمت‌بندی Market Cap.

    Examples
    --------
    >>> fmt_mcap(1_200_000_000)
    '1.20B'
    >>> fmt_mcap(None)
    '--'
    """
    return fmt_compact(cap, decimals=2)


def fmt_volume(vol: Any) -> str:
    """
    فرمت‌بندی Volume.
    مشابه market cap ولی مستقل - در آینده می‌توان تنظیمات جدا داشت.

    Examples
    --------
    >>> fmt_volume(850_000_000)
    '850.00M'
    """
    return fmt_compact(vol, decimals=2)


def fmt_percent(
    val: Any,
    decimals: int = 2,
    show_sign: bool = True,
) -> str:
    """
    فرمت‌بندی درصد با علامت.

    Parameters
    ----------
    val:
        مقدار درصد (مثلاً 3.45 برای 3.45%).
    decimals:
        تعداد ارقام اعشار.
    show_sign:
        اگر True باشد، برای اعداد مثبت '+' نشان داده می‌شود.

    Examples
    --------
    >>> fmt_percent(3.45)
    '+3.45%'
    >>> fmt_percent(-2.1)
    '-2.10%'
    >>> fmt_percent(0.0)
    '0.00%'
    >>> fmt_percent(None)
    '--'
    """
    f = safe_float(val)
    if f is None:
        return "--"

    prefix = "+" if show_sign and f > 0 else ""
    return f"{prefix}{f:.{decimals}f}%"


def fmt_timestamp(
    ts: Any,
    *,
    fmt_str: str = "%Y-%m-%d %H:%M",
    utc: bool = True,
) -> str:
    """
    تبدیل Unix timestamp به رشته تاریخ/ساعت خوانا.

    Parameters
    ----------
    ts:
        Unix timestamp به ثانیه یا میلی‌ثانیه.
    fmt_str:
        قالب datetime (پیش‌فرض: '2024-01-15 09:30').
    utc:
        اگر True باشد، در timezone UTC نمایش می‌دهد.

    Examples
    --------
    >>> fmt_timestamp(1700000000)
    '2023-11-14 22:13'
    >>> fmt_timestamp(None)
    '--'
    """
    if ts is None:
        return "--"

    try:
        ts_float = float(ts)
        # تشخیص میلی‌ثانیه
        if ts_float > 1e12:
            ts_float /= 1000.0

        tz = timezone.utc if utc else None
        dt = datetime.fromtimestamp(ts_float, tz=tz)
        return dt.strftime(fmt_str)
    except (TypeError, ValueError, OSError):
        return "--"


# ══════════════════════════════════════════════════════════════
# Trading Pair Utilities
# ══════════════════════════════════════════════════════════════

# جفت‌ارزهای رایج quote
_KNOWN_QUOTES = (
    "USDT", "USDC", "BUSD", "TUSD", "USDP",
    "BTC", "ETH", "BNB",
    "EUR", "GBP", "USD",
)


def make_pair(base: str, quote: str = "USDT") -> str:
    """
    یک جفت معاملاتی استاندارد می‌سازد.

    - اگر base خالی باشد، ValueError می‌اندازد.
    - اگر base قبلاً با quote تمام شده باشد، همان را برمی‌گرداند.
    - اگر base شامل '/' باشد (مثلاً 'BTC/USDT')، نرمال می‌کند.

    Examples
    --------
    >>> make_pair("BTC", "USDT")
    'BTCUSDT'
    >>> make_pair("BTC/USDT")
    'BTCUSDT'
    >>> make_pair("BTCUSDT", "USDT")
    'BTCUSDT'
    >>> make_pair("ETH", "BTC")
    'ETHBTC'
    """
    base = (base or "").strip().upper()
    quote = (quote or "USDT").strip().upper()

    if not base:
        raise ValueError("base cannot be empty.")

    if not quote:
        quote = "USDT"

    # اگر قبلاً slash دارد: 'BTC/USDT' → 'BTCUSDT'
    if "/" in base:
        parts = base.split("/", 1)
        base = parts[0].strip()
        if len(parts) == 2 and parts[1].strip():
            quote = parts[1].strip()

    # اگر base قبلاً شامل quote است
    if base.endswith(quote):
        return base

    # بررسی اینکه آیا base خودش با یک quote شناخته‌شده تمام می‌شود
    for known_q in _KNOWN_QUOTES:
        if base.endswith(known_q) and base != known_q:
            return base

    return f"{base}{quote}"


def split_pair(pair: str) -> Tuple[str, str]:
    """
    یک جفت معاملاتی را به base و quote تقسیم می‌کند.

    Examples
    --------
    >>> split_pair("BTC/USDT")
    ('BTC', 'USDT')
    >>> split_pair("BTCUSDT")
    ('BTC', 'USDT')
    >>> split_pair("ETHBTC")
    ('ETH', 'BTC')
    """
    pair = pair.strip().upper()

    if "/" in pair:
        parts = pair.split("/", 1)
        return parts[0].strip(), parts[1].strip()

    for quote in _KNOWN_QUOTES:
        if pair.endswith(quote) and pair != quote:
            base = pair[: -len(quote)]
            if base:
                return base, quote

    return pair, ""


# ══════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════

def is_valid_number(x: Any) -> bool:
    """
    بررسی می‌کند که x یک عدد متناهی (finite) باشد.
    None, NaN, Inf, رشته‌های غیرعددی → False

    Examples
    --------
    >>> is_valid_number(3.14)
    True
    >>> is_valid_number(float('nan'))
    False
    >>> is_valid_number("abc")
    False
    """
    if x is None:
        return False
    try:
        return math.isfinite(float(x))
    except (ValueError, TypeError):
        return False