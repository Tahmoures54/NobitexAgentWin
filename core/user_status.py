# source/core/user_status.py
"""
Lightweight and robust user status manager.
Version: 2.4 — Secure admin activation (env/file only)

Changes vs 2.3:
  - REMOVED hardcoded ADMIN_SECRET_CODE from source.
  - Added _load_admin_code() that reads from:
      1. CRYPTOSCANNER_ADMIN_CODE environment variable
      2. <APPDATA_DIR>/admin_key.enc (Fernet-encrypted)
  - Admin code is now compared with hmac.compare_digest.
  - Added clear_admin_code() to invalidate local admin key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

from core.config import (
    AMOUNT_TOLERANCE,
    BOT_TRIAL_DAYS,
    DAILY_FREE_REFRESH_LIMIT,
    MONTHLY_PLAN_USDT,
    QUARTERLY_PLAN_USDT,
    TRIAL_DAYS,
    USDT_TOKEN_ID,
    USER_STATUS_FILE,
    WALLET_ADDRESS,
    YEARLY_PLAN_USDT,
)
from core.encryption import decrypt_user_status, encrypt_user_status, get_device_id

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Module-level lock
# ══════════════════════════════════════════════════════════════

_STATUS_LOCK = threading.RLock()

# ══════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════

VALID_PLAN_MONTHS: Tuple[int, ...] = (1, 3, 12)
MAX_USED_TX_HASHES: int = 500
MAX_TX_AGE_HOURS: int = 48
CLOCK_ROLLBACK_TOLERANCE_SEC: int = 3600
_REPLACE_RETRY_COUNT: int = 5
_REPLACE_RETRY_DELAY: float = 0.05  # seconds

_PLAN_PRICE_MAP: Dict[int, float] = {
    1:  float(MONTHLY_PLAN_USDT),
    3:  float(QUARTERLY_PLAN_USDT),
    12: float(YEARLY_PLAN_USDT),
}

LICENSE_KEY_FILE = Path("secrets/license_key.key")

# ── Admin code storage (env var OR encrypted file) ──
_ADMIN_KEY_FILE_ENV = "CRYPTOSCANNER_ADMIN_KEY_FILE"
_ADMIN_CODE_ENV = "CRYPTOSCANNER_ADMIN_CODE"
_ADMIN_FILE_NAME = "admin_key.enc"
_ADMIN_FILE_KEY_NAME = "admin_key.key"


# ══════════════════════════════════════════════════════════════
# Admin code loader (replaces hardcoded constant)
# ══════════════════════════════════════════════════════════════

def _admin_key_path() -> Path:
    override = os.environ.get(_ADMIN_KEY_FILE_ENV)
    if override:
        return Path(override)
    from core.config import APPDATA_DIR
    return Path(APPDATA_DIR) / _ADMIN_FILE_NAME


def _admin_fernet_key_path() -> Path:
    from core.config import APPDATA_DIR
    return Path(APPDATA_DIR) / _ADMIN_FILE_KEY_NAME


def _load_admin_fernet_key() -> Optional[bytes]:
    path = _admin_fernet_key_path()
    if not path.exists():
        return None
    try:
        key = path.read_bytes().strip()
        Fernet(key)  # validate
        return key
    except Exception as exc:
        logger.warning("Invalid admin Fernet key at %s: %s", path, exc)
        return None


def _load_admin_code() -> str:
    """
    Load the admin activation code from, in order:
      1. CRYPTOSCANNER_ADMIN_CODE environment variable
      2. <APPDATA_DIR>/admin_key.enc (Fernet-encrypted)

    Returns empty string when no admin code is configured.
    """
    env_code = os.environ.get(_ADMIN_CODE_ENV, "").strip()
    if env_code:
        return env_code

    path = _admin_key_path()
    if not path.exists():
        return ""

    fernet_key = _load_admin_fernet_key()
    if fernet_key is None:
        logger.warning(
            "Admin key file %s exists but Fernet key is missing.", path,
        )
        return ""

    try:
        plaintext = Fernet(fernet_key).decrypt(path.read_bytes())
        return plaintext.decode("utf-8").strip()
    except (InvalidToken, UnicodeDecodeError, OSError) as exc:
        logger.warning("Could not decrypt admin key: %s", exc)
        return ""


def set_admin_code(code: str, persist: bool = True) -> bool:
    """
    Store a new admin code.

    When persist=True, the code is encrypted with a locally generated
    Fernet key and stored at <APPDATA_DIR>/admin_key.enc.
    Otherwise it is only set in the current process environment.
    """
    code = (code or "").strip()
    if not code:
        return False

    os.environ[_ADMIN_CODE_ENV] = code

    if not persist:
        return True

    try:
        from core.config import APPDATA_DIR
        os.makedirs(APPDATA_DIR, exist_ok=True)

        key_path = _admin_fernet_key_path()
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass

        fernet_key = _load_admin_fernet_key()
        if fernet_key is None:
            return False

        encrypted = Fernet(fernet_key).encrypt(code.encode("utf-8"))
        tmp = _admin_key_path().with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        os.replace(tmp, _admin_key_path())
        return True
    except OSError as exc:
        logger.error("Failed to persist admin code: %s", exc)
        return False


def clear_admin_code() -> None:
    """Remove the locally stored admin code (keeps env var if set)."""
    try:
        path = _admin_key_path()
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning("Could not remove admin key file: %s", exc)


# ══════════════════════════════════════════════════════════════
# Date / Time Helpers
# ══════════════════════════════════════════════════════════════

def _utc_today() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _today_str() -> str:
    return _utc_today().strftime("%Y-%m-%d")


def _parse_date(d_str: Optional[str]) -> Optional[datetime]:
    if not d_str:
        return None
    try:
        return datetime.strptime(d_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _days_since(date_str: Optional[str], today: datetime) -> Optional[int]:
    dt = _parse_date(date_str)
    if dt is None:
        return None
    return (today - dt).days


# ══════════════════════════════════════════════════════════════
# License Key & Activation Code Helpers
# ══════════════════════════════════════════════════════════════

def load_license_secret_key() -> bytes:
    if not LICENSE_KEY_FILE.exists():
        raise FileNotFoundError(
            f"License key file not found at {LICENSE_KEY_FILE}. "
            "Please provide the secret key used by the activation code generator."
        )
    key = LICENSE_KEY_FILE.read_bytes()
    if len(key) < 32:
        raise ValueError("License key file is shorter than 32 bytes.")
    return key[:32]


def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def verify_activation_code_signature(
    code: str, secret_key: bytes
) -> Tuple[bool, str, dict]:
    try:
        payload_b64, signature_b64 = code.split(".", 1)
    except ValueError:
        return False, "Invalid code format", {}

    expected_sig = hmac.new(
        secret_key, payload_b64.encode("utf-8"), hashlib.sha256
    )
    expected_sig_b64 = base64url_encode(expected_sig.digest())
    if not hmac.compare_digest(signature_b64, expected_sig_b64):
        return False, "Invalid signature", {}

    try:
        payload_json = base64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_json)
    except Exception:
        return False, "Cannot read payload", {}

    return True, "", payload


# ══════════════════════════════════════════════════════════════
# Status Normalization
# ══════════════════════════════════════════════════════════════

def _detect_rollback(status: Dict[str, Any]) -> bool:
    now = time.time()
    last = status.get("last_system_time", 0)
    rolled = last > now + CLOCK_ROLLBACK_TOLERANCE_SEC
    if rolled:
        logger.warning(
            "Clock rollback detected: last=%.0f now=%.0f diff=%.0fs",
            last, now, last - now,
        )
    return rolled


def _normalize(
    s: Dict[str, Any],
    did: str,
    *,
    _now: Optional[float] = None,
) -> Dict[str, Any]:
    now_sys = _now if _now is not None else time.time()
    today = _utc_today()
    today_str = today.strftime("%Y-%m-%d")
    rolled_back = _detect_rollback(s)

    s.setdefault("device_id", did)
    s.setdefault("plan", "free")

    if "trial_active" not in s:
        s["trial_active"] = True
        s["trial_start"] = today_str
    else:
        s.setdefault("trial_start", None)

    if "bot_trial_active" not in s:
        s["bot_trial_active"] = True
        s["bot_trial_start"] = s.get("trial_start", today_str)
    else:
        s.setdefault("bot_trial_start", None)

    s.setdefault("refresh_count", DAILY_FREE_REFRESH_LIMIT)
    s.setdefault("last_refresh_date", today_str)
    s.setdefault("used_tx_hashes", [])
    s["last_system_time"] = now_sys

    if s.get("trial_active"):
        days = _days_since(s.get("trial_start"), today)
        if rolled_back or days is None or days >= TRIAL_DAYS:
            s["trial_active"] = False
            s["trial_start"] = None

    if s.get("bot_trial_active"):
        days = _days_since(s.get("bot_trial_start"), today)
        if rolled_back or days is None or days >= BOT_TRIAL_DAYS:
            s["bot_trial_active"] = False
            s["bot_trial_start"] = None

    if s.get("plan") == "admin":
        s["plan_expiry"] = None
    elif s.get("plan", "free") != "free":
        exp_dt = _parse_date(s.get("plan_expiry"))
        if rolled_back or exp_dt is None or today > exp_dt:
            s["plan"] = "free"
            s["plan_expiry"] = None

    if (
        s.get("plan") == "free"
        and not s.get("trial_active")
        and s.get("last_refresh_date") != today_str
    ):
        s["refresh_count"] = DAILY_FREE_REFRESH_LIMIT
        s["last_refresh_date"] = today_str

    tx_hashes: List[str] = s.get("used_tx_hashes", [])
    if len(tx_hashes) > MAX_USED_TX_HASHES:
        s["used_tx_hashes"] = tx_hashes[-MAX_USED_TX_HASHES:]

    return s


# ══════════════════════════════════════════════════════════════
# File I/O
# ══════════════════════════════════════════════════════════════

def _make_tmp_path() -> str:
    tid = threading.get_ident()
    uid = uuid.uuid4().hex[:8]
    return f"{USER_STATUS_FILE}.{tid}.{uid}.tmp"


def _safe_replace(src: str, dst: str) -> None:
    last_exc: Optional[Exception] = None
    for attempt in range(_REPLACE_RETRY_COUNT):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            last_exc = exc
            if attempt < _REPLACE_RETRY_COUNT - 1:
                time.sleep(_REPLACE_RETRY_DELAY * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def _cleanup_orphan_tmps() -> None:
    dir_path = os.path.dirname(USER_STATUS_FILE) or "."
    base = os.path.basename(USER_STATUS_FILE)
    try:
        for name in os.listdir(dir_path):
            if name.startswith(base) and name.endswith(".tmp"):
                try:
                    os.remove(os.path.join(dir_path, name))
                    logger.debug("Cleaned orphan tmp: %s", name)
                except OSError:
                    pass
    except OSError:
        pass


def _read_raw(did: str) -> Dict[str, Any]:
    if not os.path.exists(USER_STATUS_FILE):
        return {}
    try:
        with open(USER_STATUS_FILE, "rb") as f:
            raw = f.read()
        decrypted = decrypt_user_status(raw, did)
        if isinstance(decrypted, dict):
            return decrypted
        logger.warning("Decrypted status is not a dict — resetting.")
        return {}
    except Exception as exc:
        logger.warning("Failed to read user status (%s) — resetting.", exc)
        return {}


def _write_raw(st: Dict[str, Any], did: str) -> None:
    dir_path = os.path.dirname(USER_STATUS_FILE)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)

    tmp_path = _make_tmp_path()
    try:
        with open(tmp_path, "wb") as f:
            f.write(encrypt_user_status(st, did))
            f.flush()
            os.fsync(f.fileno())
        _safe_replace(tmp_path, USER_STATUS_FILE)
    except Exception as exc:
        logger.error("Failed to save user status: %s", exc)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


# ══════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════

def load_user_status(device_id: Optional[str] = None) -> Dict[str, Any]:
    did = device_id or get_device_id()

    with _STATUS_LOCK:
        _cleanup_orphan_tmps()
        st = _read_raw(did)

        if st.get("device_id") != did:
            st = {"device_id": did}

        before = str(st)
        st = _normalize(st, did)
        after = str(st)

        if before != after:
            try:
                _write_raw(st, did)
            except Exception:
                pass

    return st


def save_user_status(
    status: Dict[str, Any],
    device_id: Optional[str] = None,
) -> None:
    did = device_id or get_device_id()
    with _STATUS_LOCK:
        st = _normalize(status, did)
        _write_raw(st, did)


def _save_raw(st: Dict[str, Any], did: str) -> None:
    with _STATUS_LOCK:
        _write_raw(st, did)


# ══════════════════════════════════════════════════════════════
# Status Query API
# ══════════════════════════════════════════════════════════════

def is_premium_active(
    device_id: Optional[str] = None,
    status: Optional[Dict[str, Any]] = None,
) -> bool:
    st = status if status is not None else load_user_status(device_id)
    return st.get("plan", "free") != "free"


def has_bot_access(
    device_id: Optional[str] = None,
    status: Optional[Dict[str, Any]] = None,
) -> bool:
    st = status if status is not None else load_user_status(device_id)
    return is_premium_active(status=st) or bool(st.get("bot_trial_active"))


def can_refresh(
    device_id: Optional[str] = None,
    status: Optional[Dict[str, Any]] = None,
) -> bool:
    st = status if status is not None else load_user_status(device_id)
    return (
        bool(st.get("trial_active"))
        or is_premium_active(status=st)
        or st.get("refresh_count", 0) > 0
    )


def consume_refresh(device_id: Optional[str] = None) -> bool:
    did = device_id or get_device_id()
    with _STATUS_LOCK:
        st = _read_raw(did)
        if st.get("device_id") != did:
            st = {"device_id": did}
        st = _normalize(st, did)

        if st.get("trial_active") or is_premium_active(status=st):
            return True

        if st.get("refresh_count", 0) > 0:
            st["refresh_count"] -= 1
            try:
                _write_raw(st, did)
            except Exception:
                pass
            return True

        return False


def get_status_summary(
    device_id: Optional[str] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    st = status if status is not None else load_user_status(device_id)
    today = _utc_today()

    trial_days_left = None
    if st.get("trial_active"):
        days = _days_since(st.get("trial_start"), today)
        if days is not None:
            trial_days_left = max(0, TRIAL_DAYS - days)

    bot_trial_days_left = None
    if st.get("bot_trial_active"):
        days = _days_since(st.get("bot_trial_start"), today)
        if days is not None:
            bot_trial_days_left = max(0, BOT_TRIAL_DAYS - days)

    plan_days_left = None
    if st.get("plan") == "admin":
        plan_days_left = "∞"
    elif st.get("plan_expiry") and st.get("plan", "free") != "free":
        exp_dt = _parse_date(st.get("plan_expiry"))
        if exp_dt:
            plan_days_left = max(0, (exp_dt - today).days)

    return {
        "plan":               st.get("plan", "free"),
        "trial_active":       bool(st.get("trial_active")),
        "trial_days_left":    trial_days_left,
        "bot_trial_active":   bool(st.get("bot_trial_active")),
        "bot_trial_days_left": bot_trial_days_left,
        "refresh_count":      st.get("refresh_count", 0),
        "plan_expiry":        st.get("plan_expiry"),
        "plan_days_left":     plan_days_left,
        "can_refresh":        can_refresh(status=st),
        "has_bot_access":     has_bot_access(status=st),
        "is_premium":         is_premium_active(status=st),
    }


# ══════════════════════════════════════════════════════════════
# Plan Activation
# ══════════════════════════════════════════════════════════════

def activate_premium_plan(
    months: int,
    device_id: Optional[str] = None,
    *,
    tx_hash: Optional[str] = None,
) -> None:
    if months not in VALID_PLAN_MONTHS:
        raise ValueError(
            f"Invalid plan duration: {months}. Must be one of {VALID_PLAN_MONTHS}."
        )

    did = device_id or get_device_id()
    with _STATUS_LOCK:
        st = _read_raw(did)
        if st.get("device_id") != did:
            st = {"device_id": did}
        st = _normalize(st, did)

        expiry = (
            datetime.now(timezone.utc) + timedelta(days=months * 30)
        ).strftime("%Y-%m-%d")

        st["plan"] = f"{months}months"
        st["trial_active"] = False
        st["trial_start"] = None
        st["plan_expiry"] = expiry

        _write_raw(st, did)

    logger.info(
        "Premium plan activated: %d months, expiry=%s, tx_hash=%s",
        months, expiry, tx_hash,
    )


def activate_admin_plan(device_id: Optional[str] = None) -> None:
    """Activate permanent admin plan (no transaction required)."""
    did = device_id or get_device_id()
    with _STATUS_LOCK:
        st = _read_raw(did)
        if st.get("device_id") != did:
            st = {"device_id": did}
        st = _normalize(st, did)

        st["plan"] = "admin"
        st["trial_active"] = False
        st["trial_start"] = None
        st["plan_expiry"] = None

        _write_raw(st, did)

    logger.info("Admin plan activated for device %s", did)


# ══════════════════════════════════════════════════════════════
# Payment Verification (USDT via Tron)
# ══════════════════════════════════════════════════════════════

def _parse_tx_amount(trigger_info: Dict[str, Any]) -> Tuple[bool, str, float]:
    amount_str = trigger_info.get("parameter", {}).get("_value")
    if not amount_str:
        return False, "Amount not found in transaction.", 0.0
    try:
        amount = int(amount_str) / 1_000_000
        return True, "", amount
    except (ValueError, TypeError):
        return False, f"Invalid amount format: {amount_str!r}", 0.0


def _validate_tx_response(
    resp: Dict[str, Any],
    expected_amount: float,
    tx_hash: str,
) -> Tuple[bool, str]:
    if resp.get("contractRet") != "SUCCESS":
        return False, f"Transaction failed: {resp.get('contractRet', 'UNKNOWN')}"

    trigger_info = resp.get("trigger_info")
    if not trigger_info:
        return False, "No trigger_info — not a token transfer."

    if trigger_info.get("contract_address") != USDT_TOKEN_ID:
        return False, "Not a valid USDT TRC20 transaction."

    params = trigger_info.get("parameter", {})
    recipient = params.get("_to", "")
    if recipient != WALLET_ADDRESS:
        return False, f"Recipient mismatch. Expected {WALLET_ADDRESS}, got {recipient}."

    if not resp.get("owner_address"):
        return False, "Sender address missing."

    ok, err, amount = _parse_tx_amount(trigger_info)
    if not ok:
        return False, err

    lo = expected_amount - AMOUNT_TOLERANCE
    hi = expected_amount + AMOUNT_TOLERANCE
    if not (lo <= amount <= hi):
        return False, (
            f"Amount mismatch. Expected {expected_amount:.2f}±{AMOUNT_TOLERANCE} USDT, "
            f"got {amount:.2f} USDT."
        )

    ts = resp.get("timestamp", 0)
    if not ts:
        return False, "Transaction timestamp missing."

    tx_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    age = datetime.now(timezone.utc) - tx_time
    if age > timedelta(hours=MAX_TX_AGE_HOURS):
        return False, (
            f"Transaction too old ({age.days}d {age.seconds // 3600}h). "
            f"Must be within {MAX_TX_AGE_HOURS}h."
        )

    if resp.get("confirmed") is False:
        return False, "Transaction not yet confirmed on blockchain."

    if resp.get("confirmations", 1) < 1:
        return False, f"Insufficient confirmations: {resp.get('confirmations')}."

    return True, ""


def verify_usdt_transaction(
    tx_hash: str,
    months: int,
    fetch_func: Optional[Callable] = None,
    device_id: Optional[str] = None,
) -> Tuple[bool, str]:
    tx_hash = (tx_hash or "").strip()
    if not tx_hash:
        return False, "Transaction hash is empty."

    if months not in VALID_PLAN_MONTHS:
        return False, f"Invalid months: {months}."

    if fetch_func is None:
        return False, "No network function provided."

    expected_amount = _PLAN_PRICE_MAP[months]
    did = device_id or get_device_id()

    with _STATUS_LOCK:
        st = _read_raw(did)
        if st.get("device_id") != did:
            st = {"device_id": did}
        st = _normalize(st, did)

        if tx_hash in st.get("used_tx_hashes", []):
            return False, "This transaction hash has already been used on this device."

    api_url = f"https://apilist.tronscan.org/api/transaction-info?hash={tx_hash}"
    logger.info("Fetching transaction from blockchain: %s", api_url)

    resp = fetch_func(api_url, {}, {})
    if resp is None:
        return False, "Failed to fetch transaction data."

    if isinstance(resp, dict) and resp.get("error") == "NO_INTERNET":
        return False, "No internet connection."

    if not isinstance(resp, dict):
        return False, "Unexpected response format from TronScan API."

    valid, error_msg = _validate_tx_response(resp, expected_amount, tx_hash)
    if not valid:
        logger.warning("TX validation failed: %s (hash=%s)", error_msg, tx_hash)
        return False, error_msg

    with _STATUS_LOCK:
        st = _read_raw(did)
        if st.get("device_id") != did:
            st = {"device_id": did}
        st = _normalize(st, did)

        if tx_hash in st.get("used_tx_hashes", []):
            return False, "This transaction hash has already been used (concurrent check)."

        hashes = st.get("used_tx_hashes", []) + [tx_hash]
        st["used_tx_hashes"] = hashes[-MAX_USED_TX_HASHES:]

        expiry = (
            datetime.now(timezone.utc) + timedelta(days=months * 30)
        ).strftime("%Y-%m-%d")
        st["plan"] = f"{months}months"
        st["trial_active"] = False
        st["trial_start"] = None
        st["plan_expiry"] = expiry

        _write_raw(st, did)

    logger.info(
        "Premium activated: months=%d, expiry=%s, tx=%s",
        months, expiry, tx_hash,
    )
    return True, ""


def verify_usdt_transaction_gui(
    tx_hash: str,
    months: int,
    parent_widget=None,
    fetch_func: Optional[Callable] = None,
    device_id: Optional[str] = None,
) -> Tuple[bool, str]:
    return verify_usdt_transaction(tx_hash, months, fetch_func, device_id)


# ══════════════════════════════════════════════════════════════
# Activation Code Verification (secure admin path)
# ══════════════════════════════════════════════════════════════

def verify_and_activate_code(
    code: str,
    device_id: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Verify an activation code.

    Order of checks:
      1. Admin code (loaded from env or encrypted file) — constant-time compare
      2. Signed license code (HMAC-SHA256 with local secret key)
    """
    code = (code or "").strip()
    if not code:
        return False, "Activation code is empty."

    did = device_id or get_device_id()

    # ── Admin path (constant-time compare to avoid timing oracle) ──
    admin_code = _load_admin_code()
    if admin_code:
        if hmac.compare_digest(code, admin_code):
            try:
                activate_admin_plan(did)
                return True, "Admin plan activated successfully."
            except Exception as e:
                logger.error("Failed to activate admin plan: %s", e)
                return False, f"Admin activation failed: {e}"

    # ── Signed license path ──
    try:
        secret_key = load_license_secret_key()
    except Exception as e:
        logger.error("Failed to load license key: %s", e)
        return False, f"License key error: {e}"

    ok, err, payload = verify_activation_code_signature(code, secret_key)
    if not ok:
        return False, err

    expires_at = payload.get("expires_at")
    if expires_at is not None:
        try:
            if int(time.time()) > int(expires_at):
                return False, "Activation code has expired."
        except (ValueError, TypeError):
            return False, "Invalid expiry in code."

    bound_device = payload.get("device_id")
    if bound_device is not None and bound_device != did:
        return False, "Activation code is bound to another device."

    plan_str = payload.get("plan", "")
    try:
        months = int(plan_str.replace("months", ""))
    except (ValueError, AttributeError):
        return False, "Invalid plan format in code."

    if months not in VALID_PLAN_MONTHS:
        return False, f"Unsupported plan duration: {months}."

    try:
        activate_premium_plan(months, did)
    except Exception as e:
        logger.error("Failed to activate plan: %s", e)
        return False, f"Activation failed: {e}"

    logger.info("Activation code verified and plan activated: %d months", months)
    return True, f"Premium plan activated for {months} months."