# source/core/config.py
"""
Constants and configuration settings for CryptoScanner.

Storage:
    All persistent state files are stored under the PROJECT folder:
    - <project_root>/data/

    This makes the app fully portable: copy the folder, everything moves.

    Environment override is still supported for testing / CI:
    - CRYPTOSCANNER_APPDATA

Obfuscation Note:
    Sensitive strings (wallet address, USDT contract ID) are obfuscated
    with XOR + base64. This is OBFUSCATION ONLY, not cryptographic security.
    It raises the bar against trivial string extraction from binaries,
    but a determined reverse-engineer can always recover the values.
    Do NOT rely this for security-critical operations.

Environment Overrides (useful for testing / CI):
    CRYPTOSCANNER_APPDATA   - override APPDATA_DIR

Version: 6.1.1
    - Documented obfuscation limits (no functional change).
    - Version sync with signal_tracker v6.4.1 and gui_main v7.7.2.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# Version
# ══════════════════════════════════════════════════════════════

APP_VERSION: str = "6.1.2"
APP_NAME:    str = "CryptoScanner"

# ══════════════════════════════════════════════════════════════
# Obfuscation
# ══════════════════════════════════════════════════════════════
#
# ⚠️  SECURITY WARNING:
#     The XOR+base64 helper below is OBFUSCATION, not encryption.
#     It only raises the cost of trivial `strings` extraction.
#     Never use it for private keys, API secrets, or any value
#     where confidentiality is required.

_K = ("Cry", "pto", "Scan", "ner")
_OBFUS_KEY: str = "".join(_K)


def _xo(key: str, enc: str) -> str:
    """
    XOR 'decryption' of a base64-encoded obfuscated string.

    Warning:
        This is obfuscation, not cryptography.
    """
    try:
        raw = base64.b64decode(enc.encode("ascii"))
        k   = key.encode("utf-8")
        if not k:
            raise ValueError("XOR key cannot be empty.")
        return "".join(chr(b ^ k[i % len(k)]) for i, b in enumerate(raw))
    except Exception as exc:
        logger.error("Obfuscation decode failed: %s", exc)
        return ""


def _safe_decode(key: str, enc: str, name: str) -> str:
    """Decode an obfuscated value, warning if it fails."""
    result = _xo(key, enc)
    if not result:
        logger.warning(
            "Failed to decode obfuscated value for '%s'. "
            "Payment verification will not work correctly.",
            name,
        )
    return result


# ══════════════════════════════════════════════════════════════
# Project-local data directory
# ══════════════════════════════════════════════════════════════

def _resolve_appdata_dir() -> str:
    """
    Resolve the application data directory.

    Priority:
        1. CRYPTOSCANNER_APPDATA (env var) — for tests / CI
        2. <project_root>/data                — default (portable)
    """
    env_override = os.environ.get("CRYPTOSCANNER_APPDATA")
    if env_override:
        logger.debug("Using CRYPTOSCANNER_APPDATA override: %s", env_override)
        return env_override

    this_file = os.path.abspath(__file__)          # .../core/config.py
    core_dir  = os.path.dirname(this_file)         # .../core
    proj_root = os.path.dirname(core_dir)          # .../  (project root)

    local_data = os.path.join(proj_root, "data")
    logger.debug("Using project-local data dir: %s", local_data)
    return local_data


def ensure_appdata_dir() -> str:
    """
    Create the application data directory if it does not exist.

    Returns
    -------
    str
        The directory path.
    """
    try:
        os.makedirs(APPDATA_DIR, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not create data directory '%s': %s",
            APPDATA_DIR, exc,
        )
    return APPDATA_DIR


APPDATA_DIR: str = _resolve_appdata_dir()
try:
    os.makedirs(APPDATA_DIR, exist_ok=True)
except OSError as exc:
    logger.warning("Could not create data directory '%s': %s", APPDATA_DIR, exc)

# ══════════════════════════════════════════════════════════════
# File Paths
# ══════════════════════════════════════════════════════════════

API_KEY_FILE:         str = os.path.join(APPDATA_DIR, "api_key.txt")
CONFIG_FILE:          str = os.path.join(APPDATA_DIR, "config.ini")
USER_STATUS_FILE:     str = os.path.join(APPDATA_DIR, "user_status.enc")
ENCRYPTED_LIMIT_FILE: str = os.path.join(APPDATA_DIR, "scan_limit.enc")
CACHE_FILE:           str = os.path.join(APPDATA_DIR, "last_data.json")
BACKUP_MARKER_FILE:   str = os.path.join(APPDATA_DIR, "trial_marker.enc")

# ══════════════════════════════════════════════════════════════
# Bot / Trading Paths
# ══════════════════════════════════════════════════════════════

BOT_CONFIG_FILE:      str = os.path.join(APPDATA_DIR, "bot_config.json")
REAL_DB_FILE:         str = os.path.join(APPDATA_DIR, "real_trades.db")
PAPER_DB_FILE:        str = os.path.join(APPDATA_DIR, "signal_log_real.db")

# ══════════════════════════════════════════════════════════════
# Wallet & Payment
# ══════════════════════════════════════════════════════════════

# Wallet address for USDT TRC20 payments.
WALLET_ADDRESS: str = "TYnrKkYasGJ3ZpqX7TCzr2jtdf8gjgks76"

# USDT TRC20 contract ID (public, XOR-obfuscated only as a speed bump).
_USDT_ENC:   str = "QBMADgZJHx4YHBYMHA0TGBQ="
USDT_TOKEN_ID:  str = _safe_decode(_OBFUS_KEY, _USDT_ENC,   "USDT_TOKEN_ID")

# ══════════════════════════════════════════════════════════════
# Subscription Plans
# ══════════════════════════════════════════════════════════════

MONTHLY_PLAN_USDT:   int = 10
QUARTERLY_PLAN_USDT: int = 28
YEARLY_PLAN_USDT:    int = 100

VALID_PLAN_MONTHS: Tuple[int, ...] = (1, 3, 12)

PLAN_PRICE_MAP: Dict[int, float] = {
    1:  float(MONTHLY_PLAN_USDT),
    3:  float(QUARTERLY_PLAN_USDT),
    12: float(YEARLY_PLAN_USDT),
}

AMOUNT_TOLERANCE: float = 1.0

# ══════════════════════════════════════════════════════════════
# Trial & Refresh Limits
# ══════════════════════════════════════════════════════════════

TRIAL_DAYS:              int = 7
BOT_TRIAL_DAYS:          int = 7
DAILY_FREE_REFRESH_LIMIT: int = 5

# ══════════════════════════════════════════════════════════════
# Cache
# ══════════════════════════════════════════════════════════════

CACHE_EXPIRY_MIN: int = 5   # minutes

# ══════════════════════════════════════════════════════════════
# API Keys
# ══════════════════════════════════════════════════════════════

def _load_api_keys() -> Dict[str, str]:
    """No third-party market-data API keys are used."""
    return {}


# ══════════════════════════════════════════════════════════════
# HTTP / API Request Settings
# ══════════════════════════════════════════════════════════════

MAX_RETRIES:     int   = 5
REQUEST_TIMEOUT: float = 30.0
RETRY_DELAY:     float = 2.0

DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ══════════════════════════════════════════════════════════════
# GUI / Refresh Settings
# ══════════════════════════════════════════════════════════════

REFRESH_INTERVAL_MS: int = CACHE_EXPIRY_MIN * 60 * 1000

DEFAULT_ADV_LIMIT: int = 50

RISK_LEVELS: Dict[str, str] = {
    "Low":     "#d4edda",
    "Medium":  "#fff3cd",
    "High":    "#f8d7da",
    "Extreme": "#f5c6cb",
}

# ══════════════════════════════════════════════════════════════
# Market Categories
# ══════════════════════════════════════════════════════════════

CATEGORIES: Dict[str, List[str]] = {
    "All":         [],
    "DeFi":        ["defi"],
    "AI":          ["artificial-intelligence", "ai"],
    "Gaming":      ["gaming", "gamefi"],
    "NFT":         ["nft"],
    "Layer 2":     ["layer-2"],
    "Meme":        ["meme-token"],
    "Stablecoins": ["stablecoin"],
    "Privacy":     ["privacy"],
}

# ══════════════════════════════════════════════════════════════
# Signal Settings
# ══════════════════════════════════════════════════════════════

SIGNAL_OPTIONS: List[str] = [
    "All",
    "Strong Buy",
    "Buy Signal",
    "Neutral",
    "Sell Signal",
    "Strong Sell",
]

SIGNAL_ORDER: Dict[str, int] = {
    "Strong Buy":  0,
    "Buy Signal":  1,
    "Neutral":     2,
    "Sell Signal": 3,
    "Strong Sell": 4,
}

# ══════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════

def validate_config() -> List[str]:
    """Validate config values. Empty list means all good."""
    issues: List[str] = []

    _check_positive(issues, "MONTHLY_PLAN_USDT",   MONTHLY_PLAN_USDT)
    _check_positive(issues, "QUARTERLY_PLAN_USDT", QUARTERLY_PLAN_USDT)
    _check_positive(issues, "YEARLY_PLAN_USDT",    YEARLY_PLAN_USDT)

    if not (0.0 <= AMOUNT_TOLERANCE <= 10.0):
        issues.append(
            f"AMOUNT_TOLERANCE={AMOUNT_TOLERANCE} is suspicious "
            f"(expected 0–10 USDT)."
        )

    if TRIAL_DAYS < 0:
        issues.append(f"TRIAL_DAYS must be >= 0, got {TRIAL_DAYS}.")
    if BOT_TRIAL_DAYS < 0:
        issues.append(f"BOT_TRIAL_DAYS must be >= 0, got {BOT_TRIAL_DAYS}.")
    if DAILY_FREE_REFRESH_LIMIT < 0:
        issues.append(
            f"DAILY_FREE_REFRESH_LIMIT must be >= 0, "
            f"got {DAILY_FREE_REFRESH_LIMIT}."
        )
    if MAX_RETRIES < 1:
        issues.append(f"MAX_RETRIES must be >= 1, got {MAX_RETRIES}.")
    if REQUEST_TIMEOUT <= 0:
        issues.append(
            f"REQUEST_TIMEOUT must be positive, got {REQUEST_TIMEOUT}."
        )
    if CACHE_EXPIRY_MIN <= 0:
        issues.append(
            f"CACHE_EXPIRY_MIN must be positive, got {CACHE_EXPIRY_MIN}."
        )

    for m in VALID_PLAN_MONTHS:
        if m not in PLAN_PRICE_MAP:
            issues.append(
                f"VALID_PLAN_MONTHS contains {m} "
                f"but PLAN_PRICE_MAP has no entry for it."
            )

    if not WALLET_ADDRESS:
        issues.append("WALLET_ADDRESS is empty. Payment verification will fail.")
    if not USDT_TOKEN_ID:
        issues.append(
            "USDT_TOKEN_ID could not be decoded. Payment verification will fail."
        )

    expected_ms = CACHE_EXPIRY_MIN * 60 * 1000
    if REFRESH_INTERVAL_MS != expected_ms:
        issues.append(
            f"REFRESH_INTERVAL_MS ({REFRESH_INTERVAL_MS}ms) is not equal to "
            f"CACHE_EXPIRY_MIN × 60 × 1000 ({expected_ms}ms). "
            f"This may cause unnecessary API calls."
        )

    for sig in SIGNAL_ORDER:
        if sig not in SIGNAL_OPTIONS and sig != "All":
            issues.append(
                f"SIGNAL_ORDER contains '{sig}' "
                f"which is not in SIGNAL_OPTIONS."
            )

    return issues


def _check_positive(issues: List[str], name: str, value: float) -> None:
    if value <= 0:
        issues.append(f"{name} must be positive, got {value}.")


# ══════════════════════════════════════════════════════════════
# Developer Utility (offline use only)
# ══════════════════════════════════════════════════════════════

def _make_obfuscated(plaintext: str, key: str = _OBFUS_KEY) -> str:
    """Obfuscate a string for use in config. Developer utility only."""
    k     = key.encode("utf-8")
    raw   = plaintext.encode("utf-8")
    xored = bytes(b ^ k[i % len(k)] for i, b in enumerate(raw))
    return base64.urlsafe_b64encode(xored).decode("ascii")