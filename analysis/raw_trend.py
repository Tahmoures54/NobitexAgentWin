"""Deterministic price-action trend rules.

This module is intentionally small and dependency free.  It does not calculate
technical indicators and it does not make a forecast.  A signal is only an
observation about the prices that have already arrived in consecutive scans.

The live Nobitex path uses :func:`assess_trend` as its entry contract:

* the current price must be above the reference price by the configured
  threshold;
* the latest scans must contain a positive consecutive streak;
* the recent structure must contain higher highs and higher lows;
* the current price must be above the mean of previous scans and that mean
  must be rising.

The same assessment is also used for trend-break exits.  Keeping these rules
in one module prevents the scanner, paper tracker and live tracker from
silently using different definitions of "trend".
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple


_PRICE_KEYS: Tuple[str, ...] = (
    "price",
    "Price",
    "current_price",
    "close",
    "Close",
    "ask",
    "Ask",
)


def _finite_positive(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def price_from_scan(scan: Any) -> Optional[float]:
    """Return a positive finite price from a scan point or ``None``.

    A scan can be a number, a mapping returned by Nobitex, or an object with
    one of the common price attributes.  Invalid observations are ignored by
    design; trading must fail closed when there is not enough valid history.
    """
    if isinstance(scan, Mapping):
        for key in _PRICE_KEYS:
            if key in scan:
                price = _finite_positive(scan.get(key))
                if price is not None:
                    return price
        return None
    if isinstance(scan, (int, float)) and not isinstance(scan, bool):
        return _finite_positive(scan)
    for key in _PRICE_KEYS:
        price = _finite_positive(getattr(scan, key, None))
        if price is not None:
            return price
    return None


def clean_prices(history: Iterable[Any]) -> List[float]:
    """Extract valid prices while preserving scan order."""
    if history is None:
        return []
    return [price for item in history if (price := price_from_scan(item)) is not None]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _window_means(values: Sequence[float]) -> Tuple[float, float]:
    """Return earlier/recent means for a sequence with at least two values."""
    if len(values) < 2:
        return 0.0, 0.0
    split = max(1, len(values) // 2)
    # For an odd length, let the recent half contain the extra observation.
    earlier = values[:split]
    recent = values[split:]
    if not recent:
        recent = values[-1:]
    return _mean(earlier), _mean(recent)


def _local_extremes(values: Sequence[float], kind: str) -> List[float]:
    """Find simple three-scan swing highs/lows, not an indicator.

    The endpoint is deliberately not treated as a swing point.  If a market
    has moved almost monotonically and therefore has too few local turns, the
    caller uses two adjacent scan blocks as structural observations.  This is
    still raw price action and avoids inventing a smoothing rule.
    """
    result: List[float] = []
    for index in range(1, len(values) - 1):
        left, current, right = values[index - 1 : index + 2]
        if kind == "high" and current > left and current >= right:
            result.append(current)
        elif kind == "low" and current < left and current <= right:
            result.append(current)
    return result


def _structural_extremes(values: Sequence[float], kind: str) -> List[float]:
    swings = _local_extremes(values, kind)
    if len(swings) >= 2:
        return swings
    if len(values) < 4:
        return swings
    split = len(values) // 2
    blocks = (values[:split], values[split:])
    if kind == "high":
        return [_mean([max(block)]) for block in blocks if block]
    return [_mean([min(block)]) for block in blocks if block]


def _positive_streak(values: Sequence[float]) -> int:
    """Number of consecutive positive scan-to-scan changes at the end."""
    streak = 0
    for index in range(len(values) - 1, 0, -1):
        if values[index] > values[index - 1]:
            streak += 1
        else:
            break
    return streak


@dataclass(frozen=True)
class TrendAssessment:
    """Auditable result of the raw scan rules."""

    current_price: float = 0.0
    reference_price: float = 0.0
    cumulative_change_pct: float = 0.0
    positive_streak: int = 0
    previous_mean: float = 0.0
    previous_mean_change_pct: float = 0.0
    last_high: float = 0.0
    previous_high: float = 0.0
    last_low: float = 0.0
    previous_low: float = 0.0
    current_above_previous_mean: bool = False
    previous_mean_rising: bool = False
    higher_highs: bool = False
    higher_lows: bool = False
    trend_confirmed: bool = False
    trend_break: bool = False
    sufficient_history: bool = False
    reason_codes: Tuple[str, ...] = ()

    @property
    def entry_allowed(self) -> bool:
        return self.trend_confirmed and not self.trend_break

    @property
    def is_negative(self) -> bool:
        return self.cumulative_change_pct <= 0.0

    def to_dict(self) -> dict:
        result = asdict(self)
        result["reason_codes"] = list(self.reason_codes)
        result["entry_allowed"] = self.entry_allowed
        result["is_negative"] = self.is_negative
        return result


def assess_trend(
    history: Iterable[Any],
    *,
    threshold_percent: float = 3.0,
    min_consecutive_positive_scans: int = 3,
    trend_lookback_scans: int = 6,
    reference_price: Optional[float] = None,
) -> TrendAssessment:
    """Evaluate a long-only trend using only completed scan prices.

    ``history`` must include the current scan as its final observation.  A
    minimum of ``trend_lookback_scans`` valid observations is required; using
    an older first observation when the requested window is unavailable would
    make a new symbol appear confirmed too early, so the function fails closed.
    """
    prices = clean_prices(history)
    try:
        threshold = float(threshold_percent)
    except (TypeError, ValueError):
        threshold = 3.0
    try:
        min_streak = max(1, int(min_consecutive_positive_scans))
    except (TypeError, ValueError):
        min_streak = 3
    try:
        lookback = max(4, int(trend_lookback_scans))
    except (TypeError, ValueError):
        lookback = 6

    if not prices:
        return TrendAssessment(reason_codes=("no_valid_price",))

    current = prices[-1]
    if len(prices) < lookback:
        return TrendAssessment(
            current_price=current,
            reference_price=prices[0],
            cumulative_change_pct=((current - prices[0]) / prices[0] * 100.0),
            positive_streak=_positive_streak(prices),
            sufficient_history=False,
            reason_codes=("insufficient_history",),
        )

    window = prices[-lookback:]
    previous = window[:-1]
    reference = _finite_positive(reference_price) or window[0]
    cumulative = (current - reference) / reference * 100.0
    streak = _positive_streak(window)
    previous_mean = _mean(previous)
    early_mean, recent_mean = _window_means(previous)
    mean_change = ((recent_mean - early_mean) / early_mean * 100.0) if early_mean > 0 else 0.0

    highs = _structural_extremes(window, "high")
    lows = _structural_extremes(window, "low")
    previous_high, last_high = (highs[-2], highs[-1]) if len(highs) >= 2 else (0.0, 0.0)
    previous_low, last_low = (lows[-2], lows[-1]) if len(lows) >= 2 else (0.0, 0.0)
    higher_highs = len(highs) >= 2 and last_high > previous_high
    higher_lows = len(lows) >= 2 and last_low > previous_low
    above_mean = current > previous_mean
    mean_rising = recent_mean > early_mean

    reasons: List[str] = []
    if cumulative <= 0:
        reasons.append("negative_or_flat_move")
    elif cumulative <= threshold:
        reasons.append("below_threshold")
    if streak < min_streak:
        reasons.append("positive_streak_not_confirmed")
    if not higher_highs:
        reasons.append("higher_highs_not_confirmed")
    if not higher_lows:
        reasons.append("higher_lows_not_confirmed")
    if not above_mean:
        reasons.append("price_below_previous_mean")
    if not mean_rising:
        reasons.append("previous_mean_not_rising")

    # A break is meaningful only after enough history exists to evaluate the
    # structure.  It is intentionally independent of entry confirmation so an
    # open trade can be exited even when a fresh trend has not formed.
    trend_break = not above_mean or (len(lows) >= 2 and last_low < previous_low)
    confirmed = not reasons
    return TrendAssessment(
        current_price=current,
        reference_price=reference,
        cumulative_change_pct=cumulative,
        positive_streak=streak,
        previous_mean=previous_mean,
        previous_mean_change_pct=mean_change,
        last_high=last_high,
        previous_high=previous_high,
        last_low=last_low,
        previous_low=previous_low,
        current_above_previous_mean=above_mean,
        previous_mean_rising=mean_rising,
        higher_highs=higher_highs,
        higher_lows=higher_lows,
        trend_confirmed=confirmed,
        trend_break=trend_break,
        sufficient_history=True,
        reason_codes=tuple(reasons),
    )


def trend_signal(
    history: Iterable[Any],
    *,
    symbol: str = "",
    threshold_percent: float = 3.0,
    min_consecutive_positive_scans: int = 3,
    trend_lookback_scans: int = 6,
    stop_loss_percent: float = 3.0,
    trailing_stop_percent: float = 0.0,
) -> dict:
    """Return the stable row-shaped signal consumed by GUI and tracker."""
    assessment = assess_trend(
        history,
        threshold_percent=threshold_percent,
        min_consecutive_positive_scans=min_consecutive_positive_scans,
        trend_lookback_scans=trend_lookback_scans,
    )
    if assessment.entry_allowed:
        label = "Trend Buy"
    elif assessment.trend_break:
        label = "Trend Break"
    else:
        label = "Neutral"
    result = assessment.to_dict()
    result.update(
        {
            "Symbol": str(symbol or "").upper(),
            "signal": label,
            "Signal": label,
            "trend_signal": label,
            "entry_allowed": assessment.entry_allowed,
            "tradable_long": assessment.entry_allowed,
            "threshold_percent": float(threshold_percent),
            "min_consecutive_positive_scans": int(min_consecutive_positive_scans),
            "trend_lookback_scans": int(trend_lookback_scans),
            "stop_loss_percent": float(stop_loss_percent),
            "trailing_stop_percent": float(trailing_stop_percent),
            "reasons": ",".join(assessment.reason_codes) or "trend_confirmed",
        }
    )
    return result


__all__ = [
    "TrendAssessment",
    "assess_trend",
    "clean_prices",
    "price_from_scan",
    "trend_signal",
]
