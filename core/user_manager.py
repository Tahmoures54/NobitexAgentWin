# source/core/user_manager.py
"""
UserManager: لایه میانی بین UI و user_status.

مسئولیت‌ها:
    - نگه‌داشتن وضعیت کاربر در حافظه
    - ارائه API ساده برای UI
    - delegate کردن منطق اصلی به user_status
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core import user_status as _us
from core.encryption import get_device_id

logger = logging.getLogger(__name__)


class UserManager:
    """
    مدیریت وضعیت کاربر با cache داخلی.

    Notes:
        - وضعیت یکبار هنگام ساخت بارگذاری می‌شود.
        - بعد از هر عملیات نوشتن، cache به‌روز می‌شود.
        - برای دریافت وضعیت تازه از دیسک، از reload() استفاده کنید.

    Examples
    --------
    >>> manager = UserManager()
    >>> manager.can_refresh()
    True
    >>> manager.consume_refresh()
    True
    >>> manager.get_summary()
    {'plan': 'free', 'trial_active': True, ...}
    """

    def __init__(self, device_id: Optional[str] = None):
        """
        Parameters
        ----------
        device_id:
            شناسه دستگاه. اگر None باشد، از get_device_id() خوانده می‌شود.
        """
        self.device_id: str = device_id or get_device_id()
        self._status: Dict[str, Any] = {}
        self._load()

    # ══════════════════════════════════════════════════════
    # Internal Load / Save
    # ══════════════════════════════════════════════════════

    def _load(self) -> None:
        """وضعیت را از دیسک بارگذاری و در cache ذخیره می‌کند."""
        try:
            self._status = _us.load_user_status(self.device_id)
            logger.debug(
                "UserManager loaded status for device=%s, plan=%s",
                self.device_id,
                self._status.get("plan", "free"),
            )
        except Exception as exc:
            logger.error(
                "Failed to load user status for device=%s: %s. Using defaults.",
                self.device_id,
                exc,
                exc_info=True,
            )
            self._status = {
                "device_id": self.device_id,
                "plan": "free",
                "trial_active": False,
                "bot_trial_active": False,
                "refresh_count": 0,
            }

    def _save(self) -> bool:
        """
        وضعیت فعلی cache را روی دیسک ذخیره می‌کند.

        Returns
        -------
        True اگر موفق، False در صورت خطا.
        """
        try:
            _us.save_user_status(self._status, self.device_id)
            logger.debug("UserManager saved status for device=%s", self.device_id)
            return True
        except Exception as exc:
            logger.error(
                "Failed to save user status for device=%s: %s",
                self.device_id,
                exc,
                exc_info=True,
            )
            return False

    # ══════════════════════════════════════════════════════
    # Public: Cache Management
    # ══════════════════════════════════════════════════════

    def reload(self) -> "UserManager":
        """
        وضعیت را از دیسک مجدداً بارگذاری می‌کند.
        برای زمانی که از جای دیگری وضعیت تغییر کرده استفاده کنید.

        Returns
        -------
        self (برای method chaining).

        Examples
        --------
        >>> manager.reload().get_summary()
        """
        self._load()
        return self

    @property
    def status(self) -> Dict[str, Any]:
        """کپی read-only از وضعیت فعلی."""
        return dict(self._status)

    # ══════════════════════════════════════════════════════
    # Public: Query Methods
    # ══════════════════════════════════════════════════════

    def is_premium_active(self) -> bool:
        """
        آیا پلن premium فعال است؟

        Examples
        --------
        >>> manager.is_premium_active()
        False
        """
        return _us.is_premium_active(status=self._status)

    # backward-compatible alias
    def is_paid_plan_active(self) -> bool:
        """Alias برای is_premium_active."""
        return self.is_premium_active()

    def has_bot_access(self) -> bool:
        """
        آیا کاربر به ربات دسترسی دارد؟
        (premium یا bot_trial فعال)

        Examples
        --------
        >>> manager.has_bot_access()
        True
        """
        return _us.has_bot_access(status=self._status)

    def can_refresh(self) -> bool:
        """
        آیا کاربر می‌تواند داده‌ها را refresh کند؟

        Examples
        --------
        >>> manager.can_refresh()
        True
        """
        return _us.can_refresh(status=self._status)

    def get_plan(self) -> str:
        """
        نام پلن فعلی کاربر.

        Returns
        -------
        مثلاً: 'free', '1months', '3months', '12months'
        """
        return self._status.get("plan", "free")

    def get_refresh_count(self) -> int:
        """تعداد refresh باقی‌مانده برای کاربران free."""
        return int(self._status.get("refresh_count", 0))

    def is_trial_active(self) -> bool:
        """آیا trial اسکنر فعال است؟"""
        return bool(self._status.get("trial_active", False))

    def is_bot_trial_active(self) -> bool:
        """آیا trial ربات فعال است؟"""
        return bool(self._status.get("bot_trial_active", False))

    def get_plan_expiry(self) -> Optional[str]:
        """
        تاریخ انقضای پلن premium.

        Returns
        -------
        رشته به‌صورت 'YYYY-MM-DD' یا None اگر premium نباشد.
        """
        if not self.is_premium_active():
            return None
        return self._status.get("plan_expiry")

    def get_summary(self) -> Dict[str, Any]:
        """
        خلاصه کامل وضعیت کاربر برای نمایش در UI.

        Returns
        -------
        دیکشنری با کلیدهای:
            plan, trial_active, trial_days_left, bot_trial_active,
            bot_trial_days_left, refresh_count, plan_expiry,
            plan_days_left, can_refresh, has_bot_access, is_premium

        Examples
        --------
        >>> summary = manager.get_summary()
        >>> print(summary['trial_days_left'])
        7
        """
        return _us.get_status_summary(status=self._status)

    def get_bot_trial_days_left(self) -> Optional[int]:
        """
        روزهای باقی‌مانده از trial ربات.

        Returns
        -------
        عدد صحیح یا None اگر trial غیرفعال باشد.
        """
        summary = self.get_summary()
        return summary.get("bot_trial_days_left")

    def get_trial_days_left(self) -> Optional[int]:
        """
        روزهای باقی‌مانده از trial اسکنر.

        Returns
        -------
        عدد صحیح یا None اگر trial غیرفعال باشد.
        """
        summary = self.get_summary()
        return summary.get("trial_days_left")

    def get_plan_days_left(self) -> Optional[int]:
        """
        روزهای باقی‌مانده از پلن premium.

        Returns
        -------
        عدد صحیح یا None اگر premium نباشد.
        """
        summary = self.get_summary()
        return summary.get("plan_days_left")

    # ══════════════════════════════════════════════════════
    # Public: Write Operations
    # ══════════════════════════════════════════════════════

    def consume_refresh(self) -> bool:
        """
        یک refresh مصرف می‌کند.

        Returns
        -------
        True اگر refresh موفق بود یا نیازی به کسر نبود.
        False اگر موجودی refresh نداشت.

        Notes:
            برای کاربران premium و trial، همیشه True برمی‌گرداند
            بدون اینکه چیزی کسر شود.

        Examples
        --------
        >>> if manager.consume_refresh():
        ...     fetch_data()
        ... else:
        ...     show_upgrade_dialog()
        """
        result = _us.consume_refresh(device_id=self.device_id)
        # cache را refresh کن چون فایل تغییر کرده
        self._load()
        return result

    def activate_premium(self, months: int, tx_hash: Optional[str] = None) -> None:
        """
        پلن premium را فعال می‌کند و cache را به‌روز می‌کند.

        Parameters
        ----------
        months:
            تعداد ماه: 1، 3 یا 12.
        tx_hash:
            هش تراکنش برای لاگ (اختیاری).

        Raises
        ------
        ValueError:
            اگر months نامعتبر باشد.

        Examples
        --------
        >>> manager.activate_premium(months=3, tx_hash="abc123")
        >>> manager.is_premium_active()
        True
        """
        _us.activate_premium_plan(
            months=months,
            device_id=self.device_id,
            tx_hash=tx_hash,
        )
        # cache را sync کن
        self._load()
        logger.info(
            "Premium activated via UserManager: months=%d, device=%s",
            months, self.device_id,
        )

    def verify_and_activate(
        self,
        tx_hash: str,
        months: int,
        fetch_func=None,
    ) -> tuple[bool, str]:
        """
        تراکنش USDT را بررسی و در صورت موفقیت پلن را فعال می‌کند.

        Parameters
        ----------
        tx_hash:
            هش تراکنش.
        months:
            تعداد ماه پلن.
        fetch_func:
            تابع HTTP برای دریافت اطلاعات تراکنش.

        Returns
        -------
        (success, error_message)

        Examples
        --------
        >>> ok, msg = manager.verify_and_activate("0xabc...", 1, fetch)
        >>> if ok:
        ...     show_success()
        """
        ok, msg = _us.verify_usdt_transaction(
            tx_hash=tx_hash,
            months=months,
            fetch_func=fetch_func,
            device_id=self.device_id,
        )
        if ok:
            # وضعیت جدید را از دیسک بارگذاری کن
            self._load()
        return ok, msg

    # ══════════════════════════════════════════════════════
    # Dunder Methods
    # ══════════════════════════════════════════════════════

    def __repr__(self) -> str:
        return (
            f"UserManager("
            f"device_id='{self.device_id[:8]}...', "
            f"plan='{self.get_plan()}', "
            f"premium={self.is_premium_active()}, "
            f"trial={self.is_trial_active()}"
            f")"
        )

    def __enter__(self) -> "UserManager":
        return self

    def __exit__(self, *_) -> None:
        """در صورت استفاده به‌عنوان context manager، وضعیت را ذخیره می‌کند."""
        self._save()