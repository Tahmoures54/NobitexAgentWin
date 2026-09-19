# ===== trading/utils.py =====
# Utility functions for position sizing, trade logging, formatting, and validation.

import json
import math          # <-- اضافه شده برای is_valid_number
import os
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

from core.config import APPDATA_DIR

logger = logging.getLogger(__name__)

# Default trade history file stored in AppData
_DEFAULT_LOG_FILE = os.path.join(APPDATA_DIR, "trade_history.json")


def calculate_position_size(
    symbol: str,
    entry_price: float,
    balance: float,
    risk_percent: float,
    stop_loss_percent: float,
    mode: str = "risk_percent",
    fixed_amount: float = 50.0,
    kelly_fraction: float = 0.25,
    win_rate: float = 0.5,
    avg_win: float = 0.1,
    avg_loss: float = 0.05,
    max_balance_allocation: float = 0.95,
) -> float:
    """ ... (بدون تغییر) ... """
    if entry_price is None or entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if balance is None or balance <= 0:
        return 0.0

    max_alloc_quote = max(0.0, float(balance) * float(max_balance_allocation))
    mode = (mode or "").lower().strip()

    if mode == "fixed":
        amount_quote = float(fixed_amount)
        if amount_quote <= 0:
            raise ValueError("Fixed amount must be positive")
        amount_quote = min(amount_quote, max_alloc_quote)
        return amount_quote / float(entry_price)

    elif mode == "risk_percent":
        if risk_percent <= 0 or risk_percent > 100:
            raise ValueError("risk_percent must be between 0 and 100")
        if stop_loss_percent is None or stop_loss_percent <= 0:
            raise ValueError("stop_loss_percent must be positive")

        risk_amount = float(balance) * (float(risk_percent) / 100.0)
        position_size_quote = risk_amount / (float(stop_loss_percent) / 100.0)
        position_size_quote = min(position_size_quote, max_alloc_quote)
        return position_size_quote / float(entry_price)

    elif mode == "kelly":
        if avg_loss == 0:
            raise ValueError("avg_loss cannot be zero for Kelly criterion")

        b = float(avg_win) / float(avg_loss)
        p = float(win_rate)
        q = 1.0 - p
        kelly_f = (p * b - q) / b
        if kelly_f < 0:
            kelly_f = 0.0

        kelly_f *= float(kelly_fraction)
        kelly_f = max(0.0, min(1.0, kelly_f))

        amount_quote = float(balance) * kelly_f
        amount_quote = min(amount_quote, max_alloc_quote)
        return amount_quote / float(entry_price)

    raise ValueError(f"Unsupported position size mode: {mode}")


def calculate_stop_loss(
    entry_price: float, stop_loss_percent: float, side: str = "long"
) -> float:
    """ ... (بدون تغییر) ... """
    factor = float(stop_loss_percent) / 100.0
    side = (side or "long").lower()
    if side == "long":
        return float(entry_price) * (1 - factor)
    elif side == "short":
        return float(entry_price) * (1 + factor)
    raise ValueError("side must be 'long' or 'short'")


def calculate_take_profit(
    entry_price: float, take_profit_percent: float, side: str = "long"
) -> float:
    """ ... (بدون تغییر) ... """
    factor = float(take_profit_percent) / 100.0
    side = (side or "long").lower()
    if side == "long":
        return float(entry_price) * (1 + factor)
    elif side == "short":
        return float(entry_price) * (1 - factor)
    raise ValueError("side must be 'long' or 'short'")


def log_trade(
    trade_data: Dict[str, Any], log_file: Optional[str] = None
) -> bool:
    """ ... (بدون تغییر) ... """
    path = log_file or _DEFAULT_LOG_FILE
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)

        history = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                history = json.load(f)

        history.append(trade_data)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

        logger.debug("Trade logged to %s", path)
        return True
    except Exception as e:
        logger.error("Failed to log trade: %s", e)
        return False


def load_trade_history(log_file: Optional[str] = None) -> List[Dict[str, Any]]:
    """ ... (بدون تغییر) ... """
    path = log_file or _DEFAULT_LOG_FILE
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load trade history: %s", e)
        return []


def format_currency(
    value: float, currency: str = "USD", decimals: int = 2
) -> str:
    """ ... (بدون تغییر) ... """
    value = float(value or 0.0)
    sign = "-" if value < 0 else ""
    abs_val = abs(value)

    if abs_val >= 1e9:
        return f"{sign}{currency}{abs_val/1e9:.{decimals}f}B"
    if abs_val >= 1e6:
        return f"{sign}{currency}{abs_val/1e6:.{decimals}f}M"
    if abs_val >= 1e3:
        return f"{sign}{currency}{abs_val/1e3:.{decimals}f}K"
    return f"{sign}{currency}{abs_val:.{decimals}f}"


def validate_order_params(
    symbol: str,
    side: str,
    order_type: str,
    quantity: float,
    price: Optional[float] = None,
    stop_price: Optional[float] = None,
) -> List[str]:
    """ ... (بدون تغییر) ... """
    errors = []

    if not symbol or not isinstance(symbol, str):
        errors.append("Symbol must be a non-empty string")

    side_l = (side or "").lower()
    if side_l not in ("buy", "sell"):
        errors.append("Side must be 'buy' or 'sell'")

    type_l = (order_type or "").lower()
    allowed_types = (
        "market", "limit", "stop_loss", "stop_loss_limit",
        "take_profit", "take_profit_limit"
    )
    if type_l not in allowed_types:
        errors.append(f"Unsupported order type: '{order_type}'")

    try:
        q = float(quantity)
    except (TypeError, ValueError):
        q = -1
    if q <= 0:
        errors.append(f"Quantity must be positive (got {quantity})")

    if type_l in ("limit", "stop_loss_limit", "take_profit_limit"):
        if price is None:
            errors.append("Price is required for limit orders")
        else:
            try:
                if float(price) <= 0:
                    errors.append("Price must be positive")
            except (TypeError, ValueError):
                errors.append(f"Invalid price value: {price}")

    if type_l in ("stop_loss", "stop_loss_limit", "take_profit", "take_profit_limit"):
        if stop_price is None:
            errors.append("Stop price is required for stop orders")
        else:
            try:
                if float(stop_price) <= 0:
                    errors.append("Stop price must be positive")
            except (TypeError, ValueError):
                errors.append(f"Invalid stop price value: {stop_price}")

    return errors


# ══════════════════════════════════════════════════════════════
# تابع جدید برای رفع خطای BotPanel
# ══════════════════════════════════════════════════════════════
def is_valid_number(x):
    """
    Check if x is a valid finite number (not NaN, Inf, or None).
    """
    if x is None:
        return False
    try:
        v = float(x)
        return math.isfinite(v)
    except (ValueError, TypeError):
        return False