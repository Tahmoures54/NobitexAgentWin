"""Exact quote-currency helpers for Nobitex IRT/Rial.

Nobitex's IRT market API amounts are Rial amounts.  The application displays
some values as تومان for humans, but order quantities and all risk/notional
math stay in Rial/IRT.  In particular, never divide an IRT amount by ten
before sending it to Nobitex.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional, Union


DecimalLike = Union[str, int, float, Decimal]
IRT_QUOTE = "IRT"
RIAL_QUOTE_ALIASES = frozenset({"IRT", "RLS", "IRR"})


def is_irt_quote(quote: str) -> bool:
    return str(quote or "").strip().upper() in RIAL_QUOTE_ALIASES


def display_quote_label(quote: str) -> str:
    if is_irt_quote(quote):
        return "IRT (Rial)"
    text = str(quote or "USDT").strip().upper()
    return text or "USDT"


def to_irt(value: DecimalLike, *, default: Optional[DecimalLike] = None) -> Decimal:
    """Parse a quote amount without binary-float arithmetic."""
    candidate: Any = value
    if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
        candidate = default
    try:
        amount = Decimal(str(candidate).replace(",", "").strip())
    except (InvalidOperation, TypeError, ValueError):
        if default is None:
            raise ValueError(f"Invalid IRT amount: {value!r}") from None
        amount = Decimal(str(default))
    if not amount.is_finite():
        raise ValueError("IRT amount must be finite")
    return amount


def quantize_irt(value: DecimalLike, *, rounding=ROUND_HALF_UP) -> Decimal:
    """Quantize a Rial amount to the smallest IRT unit (one Rial)."""
    return to_irt(value).quantize(Decimal("1"), rounding=rounding)


def format_irt(value: DecimalLike, *, decimals: int = 0, grouping: bool = True) -> str:
    """Format an IRT amount for UI/logs; this is not an order conversion."""
    amount = to_irt(value).quantize(Decimal(1).scaleb(-max(0, int(decimals))), rounding=ROUND_HALF_UP)
    if grouping:
        return f"{amount:,.{max(0, int(decimals))}f}"
    return f"{amount:.{max(0, int(decimals))}f}"


def quote_value(quantity: DecimalLike, price_irt: DecimalLike) -> Decimal:
    """Return quantity × IRT price as an exact quote amount."""
    quantity_d = to_irt(quantity)
    price_d = to_irt(price_irt)
    if quantity_d < 0 or price_d < 0:
        raise ValueError("quantity and price cannot be negative")
    return quantity_d * price_d


def quantity_for_quote(quote_amount_irt: DecimalLike, price_irt: DecimalLike) -> Decimal:
    """Convert an IRT notional to base quantity; no تومان/Rial scaling."""
    quote = to_irt(quote_amount_irt)
    price = to_irt(price_irt)
    if quote < 0 or price <= 0:
        raise ValueError("quote amount must be non-negative and price positive")
    return quote / price


def parse_amount(text: str, default: float = 0.0) -> float:
    """Backward-compatible float parser for Tkinter entry fields."""
    try:
        return float(to_irt(text, default=default))
    except (ValueError, InvalidOperation, TypeError):
        return float(default)


__all__ = [
    "IRT_QUOTE",
    "RIAL_QUOTE_ALIASES",
    "is_irt_quote",
    "display_quote_label",
    "to_irt",
    "quantize_irt",
    "format_irt",
    "quote_value",
    "quantity_for_quote",
    "parse_amount",
]
