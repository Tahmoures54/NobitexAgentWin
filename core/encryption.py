# source/core/encryption.py
"""
Encryption utilities and device ID generation.

Provides:
    - Device-bound encryption using PBKDF2-HMAC-SHA256
    - HMAC-SHA256 integrity protection
    - Stable device ID (persisted + hardware fallback)
    - Legacy key-file based encryption (backward compatible)
    - Derived key caching برای جلوگیری از PBKDF2 تکراری

Format of device-bound ciphertext:
    [1 byte version] [16 bytes salt] [32 bytes HMAC] [N bytes Fernet ciphertext]
"""
from __future__ import annotations

import base64
import hashlib
import hmac as _hmac_mod          # نام واضح برای جلوگیری از تداخل
import json
import logging
import os
import platform
import socket
import subprocess
import uuid
from functools import lru_cache
from typing import Any, Dict, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from core.config import (
    APPDATA_DIR,
    DAILY_FREE_REFRESH_LIMIT,
    ENCRYPTED_LIMIT_FILE,
)

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

# نسخه فرمت فایل — اگر فرمت تغییر کند، این را افزایش دهید
_FORMAT_VERSION: int = 1
_VERSION_BYTE: bytes = _FORMAT_VERSION.to_bytes(1, "big")

# Pepper values — آنتروپی اضافه، مقاوم در برابر آنالیز باینری
_STATUS_PEPPER  = b"CrScaN::StatusPepper::2024x"
_INTEGRITY_PEPPER = b"CrScaN::IntegrityHmac::v2"

# اندازه‌های ثابت فرمت (به بایت)
_SALT_SIZE   = 16
_HMAC_SIZE   = 32
_HEADER_SIZE = 1 + _SALT_SIZE + _HMAC_SIZE   # version + salt + hmac = 49

# تعداد iteration برای PBKDF2
_PBKDF2_ITERATIONS = 200_000

# ویژگی‌های فایل Windows
_WIN_ATTR_HIDDEN    = 0x02
_WIN_ATTR_READONLY  = 0x01
_WIN_ATTR_PROTECTED = _WIN_ATTR_HIDDEN | _WIN_ATTR_READONLY


# ══════════════════════════════════════════════════════════════
# File Attribute Helpers (Windows)
# ══════════════════════════════════════════════════════════════

def _set_win_protected(path: str) -> None:
    """فایل را روی Windows به حالت hidden + read-only تنظیم می‌کند."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(path, _WIN_ATTR_PROTECTED)
    except Exception as exc:
        logger.debug("Could not set file attributes for %s: %s", path, exc)


def _unset_win_readonly(path: str) -> None:
    """
    قبل از نوشتن، read-only را برمی‌دارد.
    بعد از نوشتن دوباره set کنید.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(path, _WIN_ATTR_HIDDEN)
    except Exception as exc:
        logger.debug("Could not unset readonly for %s: %s", path, exc)


# ══════════════════════════════════════════════════════════════
# Key File Helper (DRY)
# ══════════════════════════════════════════════════════════════

def _load_or_create_key_file(key_path: str) -> bytes:
    """
    کلید Fernet را از فایل می‌خواند.
    اگر فایل وجود نداشته باشد، یک کلید جدید می‌سازد و ذخیره می‌کند.

    Parameters
    ----------
    key_path:
        مسیر کامل فایل کلید.

    Returns
    -------
    bytes کلید Fernet (44 بایت base64-urlsafe).
    """
    os.makedirs(os.path.dirname(key_path), exist_ok=True)

    if os.path.exists(key_path):
        _unset_win_readonly(key_path)
        try:
            with open(key_path, "rb") as f:
                key = f.read().strip()
            if len(key) >= 44:     # حداقل اندازه کلید Fernet
                return key
            logger.warning("Key file %s is corrupted. Regenerating.", key_path)
        except OSError as exc:
            logger.error("Cannot read key file %s: %s", key_path, exc)

    # ساخت کلید جدید
    key = Fernet.generate_key()
    try:
        with open(key_path, "wb") as f:
            f.write(key)
        _set_win_protected(key_path)
        logger.debug("Created new key file: %s", key_path)
    except OSError as exc:
        logger.error("Cannot write key file %s: %s", key_path, exc)

    return key


# ══════════════════════════════════════════════════════════════
# Device ID
# ══════════════════════════════════════════════════════════════

def _stable_device_id_path() -> str:
    """مسیر فایل device_id."""
    return os.path.join(APPDATA_DIR, "device_id.hash")


def _read_stored_device_id() -> Optional[str]:
    """device_id ذخیره‌شده را می‌خواند."""
    path = _stable_device_id_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="ascii") as f:
            stored = f.read().strip()
        # اعتبارسنجی: باید یک hex string 64 کاراکتری باشد (SHA-256)
        if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored):
            return stored
        logger.warning("Stored device_id is invalid, regenerating.")
    except OSError as exc:
        logger.debug("Cannot read device_id file: %s", exc)
    return None


def _save_device_id(device_id: str) -> None:
    """device_id را روی دیسک ذخیره می‌کند."""
    path = _stable_device_id_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _unset_win_readonly(path)
        with open(path, "w", encoding="ascii") as f:
            f.write(device_id)
        _set_win_protected(path)
        logger.debug("Device ID saved to %s", path)
    except OSError as exc:
        logger.warning("Cannot save device_id: %s", exc)


def _collect_hardware_parts() -> list[str]:
    """
    اطلاعات سخت‌افزاری سیستم را جمع‌آوری می‌کند.
    هر مورد با try/except جداگانه حفاظت شده.
    """
    parts: list[str] = []

    # MAC Address
    try:
        mac_int = uuid.getnode()
        # بررسی که آدرس MAC واقعی باشد (multicast bit = fake)
        if mac_int & (1 << 40) == 0:
            parts.append(mac_int.to_bytes(6, "big").hex(":"))
        else:
            parts.append("mac_random")
    except Exception:
        parts.append("mac_unknown")

    # CPU
    try:
        cpu = platform.processor() or platform.machine() or "unknown_cpu"
        parts.append(cpu)
    except Exception:
        parts.append("cpu_unknown")

    # Hostname
    try:
        parts.append(socket.gethostname())
    except Exception:
        parts.append("host_unknown")

    # Platform
    try:
        parts.append(platform.system() + platform.release())
    except Exception:
        parts.append("os_unknown")

    # Disk Serial — Windows
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["wmic", "diskdrive", "get", "SerialNumber"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip() and "SerialNumber" not in line
            ]
            if lines:
                parts.append(lines[0])
        except Exception:
            pass

    # Machine ID — Linux
    elif os.name == "posix":
        for mid_path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(mid_path, "r") as f:
                    mid = f.read().strip()
                if mid:
                    parts.append(mid)
                    break
            except OSError:
                continue

    return parts


def _compute_hardware_id() -> str:
    """device_id را از اطلاعات سخت‌افزاری محاسبه می‌کند."""
    parts = _collect_hardware_parts()
    raw = "|".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha256(raw).hexdigest()


def get_device_id() -> str:
    """
    شناسه منحصربه‌فرد و پایدار دستگاه را برمی‌گرداند.

    الگوریتم:
        1. اگر device_id روی دیسک ذخیره شده، همان را برمی‌گرداند.
        2. در غیر این صورت، از اطلاعات سخت‌افزاری محاسبه می‌کند.
        3. نتیجه را برای استفاده بعدی ذخیره می‌کند.

    Returns
    -------
    رشته hex 64 کاراکتری (SHA-256).
    """
    stored = _read_stored_device_id()
    if stored:
        return stored

    hw_id = _compute_hardware_id()
    _save_device_id(hw_id)
    logger.info("New device ID computed and saved.")
    return hw_id


# ══════════════════════════════════════════════════════════════
# Key Derivation (با cache برای جلوگیری از PBKDF2 تکراری)
# ══════════════════════════════════════════════════════════════

@lru_cache(maxsize=8)
def _derive_key_cached(device_id: str, salt_hex: str) -> bytes:
    """
    کلید Fernet را از device_id و salt مشتق می‌کند.
    نتیجه cache می‌شود تا PBKDF2 تکراری اجرا نشود.

    Parameters
    ----------
    device_id:
        شناسه دستگاه.
    salt_hex:
        salt به‌صورت hex string (برای hashability در lru_cache).

    Returns
    -------
    کلید base64-urlsafe برای Fernet.
    """
    salt = bytes.fromhex(salt_hex)
    combined = device_id.encode() + _STATUS_PEPPER
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(combined))


def _derive_key(device_id: str, salt: bytes) -> bytes:
    """wrapper عمومی که salt را به hex تبدیل می‌کند."""
    return _derive_key_cached(device_id, salt.hex())


def _derive_hmac_key(device_id: str) -> bytes:
    """
    کلید HMAC جداگانه برای integrity verification.
    از pepper متفاوت استفاده می‌کند.
    """
    mixed = device_id.encode() + _INTEGRITY_PEPPER
    return hashlib.sha256(mixed).digest()


# ══════════════════════════════════════════════════════════════
# Device-Bound Encryption
# ══════════════════════════════════════════════════════════════

def encrypt_user_status(data: Dict[str, Any], device_id: str) -> bytes:
    """
    دیکشنری وضعیت کاربر را با کلید مرتبط به دستگاه رمزنگاری می‌کند.

    فرمت خروجی:
        [1B version] [16B salt] [32B HMAC-SHA256] [NB Fernet ciphertext]

    Parameters
    ----------
    data:
        دیکشنری قابل JSON serialize.
    device_id:
        شناسه دستگاه.

    Returns
    -------
    bytes رمزنگاری‌شده.

    Raises
    ------
    TypeError:
        اگر data قابل JSON serialize نباشد.
    """
    try:
        json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Data is not JSON serializable: {exc}") from exc

    salt = os.urandom(_SALT_SIZE)
    key = _derive_key(device_id, salt)
    ciphertext = Fernet(key).encrypt(json_bytes)

    # HMAC روی: version + salt + ciphertext
    hmac_key = _derive_hmac_key(device_id)
    payload_for_mac = _VERSION_BYTE + salt + ciphertext
    mac = _hmac_mod.new(hmac_key, payload_for_mac, hashlib.sha256).digest()

    return _VERSION_BYTE + salt + mac + ciphertext


def decrypt_user_status(encrypted_data: bytes, device_id: str) -> Dict[str, Any]:
    """
    داده‌های رمزنگاری‌شده را رمزگشایی می‌کند.

    ابتدا integrity را با HMAC بررسی می‌کند، سپس رمزگشایی می‌کند.

    Parameters
    ----------
    encrypted_data:
        خروجی encrypt_user_status.
    device_id:
        شناسه دستگاه.

    Returns
    -------
    دیکشنری وضعیت یا {} در صورت هر نوع خطا.
    """
    if len(encrypted_data) < _HEADER_SIZE + 1:
        logger.debug("Encrypted data too short (%d bytes).", len(encrypted_data))
        return {}

    # خواندن version
    version = encrypted_data[0]
    if version != _FORMAT_VERSION:
        logger.warning(
            "Unknown encryption format version: %d (expected %d).",
            version, _FORMAT_VERSION,
        )
        return {}

    # جداسازی اجزا
    salt       = encrypted_data[1 : 1 + _SALT_SIZE]
    mac        = encrypted_data[1 + _SALT_SIZE : _HEADER_SIZE]
    ciphertext = encrypted_data[_HEADER_SIZE:]

    # بررسی HMAC
    hmac_key = _derive_hmac_key(device_id)
    payload_for_mac = _VERSION_BYTE + salt + ciphertext
    expected_mac = _hmac_mod.new(
        hmac_key, payload_for_mac, hashlib.sha256
    ).digest()

    if not _hmac_mod.compare_digest(mac, expected_mac):
        logger.warning(
            "HMAC verification failed. "
            "Data may be tampered or from a different device."
        )
        return {}

    # رمزگشایی
    try:
        key = _derive_key(device_id, salt)
        decrypted_bytes = Fernet(key).decrypt(ciphertext)
        result = json.loads(decrypted_bytes.decode("utf-8"))

        if not isinstance(result, dict):
            logger.warning("Decrypted data is not a dict (type=%s).", type(result))
            return {}

        return result

    except InvalidToken:
        logger.warning("Fernet decryption failed: invalid token.")
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Failed to parse decrypted JSON: %s", exc)
        return {}
    except Exception as exc:
        logger.error("Unexpected decryption error: %s", exc, exc_info=True)
        return {}


# ══════════════════════════════════════════════════════════════
# Legacy Key-File Encryption
# ══════════════════════════════════════════════════════════════

def get_encryption_key() -> bytes:
    """
    کلید رمزنگاری legacy را از فایل می‌خواند یا می‌سازد.
    فایل: APPDATA_DIR/status_key.key
    """
    key_path = os.path.join(APPDATA_DIR, "status_key.key")
    return _load_or_create_key_file(key_path)


def generate_encryption_key() -> bytes:
    """یک کلید Fernet جدید می‌سازد (ذخیره نمی‌کند)."""
    return Fernet.generate_key()


def encrypt_data(data: Dict[str, Any], key: bytes) -> bytes:
    """
    دیکشنری را با کلید Fernet رمزنگاری می‌کند (legacy).

    Raises
    ------
    TypeError:
        اگر data قابل JSON serialize نباشد.
    """
    try:
        json_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TypeError(f"Data is not JSON serializable: {exc}") from exc
    return Fernet(key).encrypt(json_bytes)


def decrypt_data(encrypted_data: bytes, key: bytes) -> Dict[str, Any]:
    """
    bytes را به دیکشنری رمزگشایی می‌کند (legacy).
    در صورت خطا {} برمی‌گرداند.
    """
    try:
        decrypted = Fernet(key).decrypt(encrypted_data)
        result = json.loads(decrypted.decode("utf-8"))
        return result if isinstance(result, dict) else {}
    except InvalidToken:
        logger.debug("decrypt_data: invalid token.")
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("decrypt_data: JSON parse error: %s", exc)
        return {}
    except Exception as exc:
        logger.error("decrypt_data: unexpected error: %s", exc)
        return {}


# ══════════════════════════════════════════════════════════════
# Scan Limit (Legacy)
# ══════════════════════════════════════════════════════════════

def encrypt_scan_limit(limit: int, key: bytes) -> bytes:
    """مقدار limit را رمزنگاری می‌کند."""
    return Fernet(key).encrypt(str(limit).encode())


def decrypt_scan_limit(encrypted_data: bytes, key: bytes) -> int:
    """
    مقدار limit رمزنگاری‌شده را رمزگشایی می‌کند.
    در صورت خطا ۰ برمی‌گرداند.
    """
    try:
        return int(Fernet(key).decrypt(encrypted_data).decode())
    except (InvalidToken, ValueError) as exc:
        logger.debug("decrypt_scan_limit failed: %s", exc)
        return 0


def init_scan_limit() -> int:
    """
    فایل scan limit رمزنگاری‌شده را مقداردهی اولیه می‌کند.

    Returns
    -------
    مقدار فعلی limit.
    """
    key_path = os.path.join(APPDATA_DIR, "limit_key.key")
    key = _load_or_create_key_file(key_path)

    if not os.path.exists(ENCRYPTED_LIMIT_FILE):
        encrypted = encrypt_scan_limit(DAILY_FREE_REFRESH_LIMIT, key)
        os.makedirs(os.path.dirname(ENCRYPTED_LIMIT_FILE), exist_ok=True)
        with open(ENCRYPTED_LIMIT_FILE, "wb") as f:
            f.write(encrypted)
        logger.debug("Initialized scan limit file: %s", ENCRYPTED_LIMIT_FILE)
        return DAILY_FREE_REFRESH_LIMIT

    try:
        with open(ENCRYPTED_LIMIT_FILE, "rb") as f:
            encrypted_data = f.read()
        limit = decrypt_scan_limit(encrypted_data, key)
        if limit <= 0:
            logger.warning("Scan limit is invalid (%d), resetting to default.", limit)
            return DAILY_FREE_REFRESH_LIMIT
        return limit
    except OSError as exc:
        logger.error("Cannot read scan limit file: %s", exc)
        return DAILY_FREE_REFRESH_LIMIT