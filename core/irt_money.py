"""IRT/Rial display helpers. Nobitex IRT balances are Rial."""


def is_irt_quote(quote: str) -> bool:
    return str(quote or "").upper() in {"IRT", "RLS", "IRR"}


def display_quote_label(quote: str) -> str:
    if is_irt_quote(quote):
        return "IRT (Rial)"
    text = str(quote or "USDT").strip().upper()
    return text or "USDT"


def parse_amount(text: str, default: float = 0.0) -> float:
    try:
        return float(str(text).replace(",", "").strip() or default)
    except (TypeError, ValueError):
        return float(default)
