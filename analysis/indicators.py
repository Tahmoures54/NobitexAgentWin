# analysis/indicators.py  (v3.3 — Chikou direction fix + trend indicator robustness)
"""
Technical Indicators Engine v3.3

CHANGES FROM v3.2
─────────────────
FIX 1 — Ichimoku chikou_span direction (was inverted in v3.2)
    Ichimoku's lagging span is the CLOSE price plotted `kijun_period`
    bars BEHIND the current bar:
        chikou[t] = close[t - kijun_period]
    In pandas that is `closes.shift(+kijun_period)` — a POSITIVE shift.
    v3.2 used `shift(-kijun)` which pushed values FORWARD and produced
    NaN at the tail, so `_last_valid(chikou)` was effectively returning
    the current close, not the lagging span.
    Both `calculate_ichimoku()` and `calculate_indicators_df()` are
    corrected here.

FIX 2 — `compute_trend_indicators()` no longer assumes a literal
    `close` column.  It now resolves the price column through the same
    alias table used everywhere else in this module, so DataFrames that
    use `Close` / `Price` / `C` work correctly instead of raising
    KeyError.

CHANGES FROM v3.1 (retained)
────────────────────────────
DEFECT 11 — EMA200 silent NaN now warns from calculate_indicators_df.
DEFECT 10 — VWAP and Ichimoku added.
DEFECT 9  — Pivot detection boundary corrected in find_support_resistance.
DEFECT 8  — rel_vol double-check redundant + buggy.
DEFECT 7  — ema_cross with 0.0 falsy bug fixed.
DEFECT 6  — Historical Volatility annualization corrected.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

try:
    import talib as _talib  # noqa: F401
    TA_LIB_AVAILABLE = True
    logger.debug("TA-Lib importable (unused — module is pure-pandas).")
except ImportError:
    TA_LIB_AVAILABLE = False

# ══════════════════════════════════════════════════════════════
#  CONFIGURATION
# ══════════════════════════════════════════════════════════════

INDICATOR_CONFIG: Dict[str, Any] = {
    "rsi_period":   14,
    "macd_fast":    12,
    "macd_slow":    26,
    "macd_signal":   9,
    "bb_period":    20,
    "bb_std":        2.0,
    "bb_ddof":       0,
    "stoch_k":      14,
    "stoch_d":       3,
    "adx_period":   14,
    "atr_period":   14,
    "ema_fast":     50,
    "ema_slow":    200,
    "volume_ma":    20,
    "ichi_tenkan":   9,
    "ichi_kijun":   26,
    "ichi_senkou":  52,
    "cci_period":   20,
    "willr_period": 14,
    "mom_period":   10,
    "hv_period":    30,
    "hv_annualize_factor": 8760,
    "obv_enabled":  True,
    "emit_prev":    True,
    "vwap_enabled": True,
}

_CONFIG_BOUNDS: Dict[str, Tuple[int, int]] = {
    "rsi_period":      (2,   100),
    "macd_fast":       (2,   100),
    "macd_slow":       (5,   300),
    "macd_signal":     (2,    50),
    "bb_period":       (5,   200),
    "stoch_k":         (2,   100),
    "stoch_d":         (1,    20),
    "adx_period":      (2,   100),
    "atr_period":      (2,   100),
    "ema_fast":        (5,   500),
    "ema_slow":       (20,  1000),
    "ichi_tenkan":     (2,    50),
    "ichi_kijun":      (5,   100),
    "ichi_senkou":    (10,   200),
    "cci_period":      (5,   200),
    "willr_period":    (2,   100),
    "mom_period":      (2,   100),
    "hv_period":       (5,   365),
}


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """Validate all config keys, return list of error strings."""
    errors: List[str] = []

    for key, (lo, hi) in _CONFIG_BOUNDS.items():
        val = cfg.get(key)
        if val is None:
            errors.append(f"Missing config key: '{key}'")
            continue
        try:
            v = int(val)
        except (TypeError, ValueError):
            errors.append(f"Config '{key}' must be integer, got {type(val)}")
            continue
        if not (lo <= v <= hi):
            errors.append(f"Config '{key}' = {v} out of range [{lo}, {hi}]")

    if cfg.get("macd_fast", 0) >= cfg.get("macd_slow", 999):
        errors.append("macd_fast must be less than macd_slow")
    if cfg.get("ema_fast", 0) >= cfg.get("ema_slow", 999):
        errors.append("ema_fast must be less than ema_slow")
    if cfg.get("ichi_tenkan", 0) >= cfg.get("ichi_kijun", 999):
        errors.append("ichi_tenkan must be less than ichi_kijun")
    if cfg.get("ichi_kijun", 0) >= cfg.get("ichi_senkou", 999):
        errors.append("ichi_kijun must be less than ichi_senkou")
    if int(cfg.get("bb_ddof", 0)) not in (0, 1):
        errors.append("bb_ddof must be 0 or 1")

    annualize = cfg.get("hv_annualize_factor", 8760)
    if not isinstance(annualize, (int, float)) or annualize <= 0:
        errors.append("hv_annualize_factor must be a positive number")

    return errors


for _e in validate_config(INDICATOR_CONFIG):
    logger.warning("INDICATOR_CONFIG WARNING: %s", _e)


# ══════════════════════════════════════════════════════════════
#  COLUMN NAME ALIASES
# ══════════════════════════════════════════════════════════════

_HIGH_COLS   = ("high",   "High",   "H")
_LOW_COLS    = ("low",    "Low",    "L")
_CLOSE_COLS  = ("close",  "Close",  "Price", "C")
_VOLUME_COLS = ("volume", "Volume", "V", "vol")
_OPEN_COLS   = ("open",   "Open",   "O")


# ══════════════════════════════════════════════════════════════
#  CORE HELPERS
# ══════════════════════════════════════════════════════════════

def _wilder_smooth(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA) — used for RSI, ATR, ADX."""
    if period <= 0:
        return pd.Series(np.nan, index=series.index, dtype=float)
    return series.ewm(
        alpha=1.0 / period,
        adjust=False,
        min_periods=period,
    ).mean()


def _last_valid(s: Optional[pd.Series]) -> Optional[float]:
    """Return last finite value, or None."""
    if s is None or len(s) == 0:
        return None
    dropped = s.dropna()
    if dropped.empty:
        return None
    v = float(dropped.iloc[-1])
    return v if np.isfinite(v) else None


def _safe_divide(
    num: pd.Series,
    den: pd.Series,
    fill: float = np.nan,
) -> pd.Series:
    """Element-wise division, replacing zeros in denominator."""
    with np.errstate(divide="ignore", invalid="ignore"):
        result = num / den.replace(0.0, np.nan)
    return result if np.isnan(fill) else result.fillna(fill)


def _require_series(
    *series: Optional[pd.Series],
    min_len: int = 2,
    caller: str = "",
) -> bool:
    """Check that all series exist and have enough valid values."""
    for i, s in enumerate(series):
        if s is None or not isinstance(s, pd.Series):
            logger.debug("%s: series[%d] is None or not a Series", caller, i)
            return False
        if len(s.dropna()) < min_len:
            logger.debug(
                "%s: series[%d] has < %d valid values", caller, i, min_len,
            )
            return False
    return True


def _get_column(df: pd.DataFrame, *keys: str) -> Optional[pd.Series]:
    """Find first non-all-NaN column by candidate names."""
    for key in keys:
        if key in df.columns:
            col = df[key]
            if not col.isna().all():
                return col
    return None


def _ema_cross_signal(
    ema_fast: Optional[float],
    ema_slow: Optional[float],
) -> int:
    """
    Return +1 (fast > slow), -1 (fast < slow), 0 (unknown/equal).
    FIX v3.0: explicit None check instead of truthy test (avoids 0.0 bug).
    """
    if ema_fast is None or ema_slow is None:
        return 0
    if ema_fast > ema_slow:
        return 1
    if ema_fast < ema_slow:
        return -1
    return 0


# ══════════════════════════════════════════════════════════════
#  INDIVIDUAL INDICATORS — Existing
# ══════════════════════════════════════════════════════════════

def calculate_sma(prices: pd.Series, period: int = 20) -> Optional[float]:
    """Simple Moving Average — last valid value."""
    if not _require_series(prices, min_len=period, caller="SMA"):
        return None
    return _last_valid(
        prices.rolling(window=period, min_periods=period).mean()
    )


def calculate_ema(prices: pd.Series, period: int = 20) -> Optional[float]:
    """Exponential Moving Average — last valid value."""
    if not _require_series(prices, min_len=period, caller="EMA"):
        return None
    return _last_valid(
        prices.ewm(span=period, adjust=False, min_periods=period).mean()
    )


def calculate_rsi(
    prices: pd.Series,
    period: int = 14,
) -> Optional[float]:
    """Wilder RSI — last valid value."""
    if not _require_series(prices, min_len=period + 1, caller="RSI"):
        return None
    delta = prices.diff()
    avg_gain = _wilder_smooth(delta.clip(lower=0.0), period)
    avg_loss = _wilder_smooth((-delta).clip(lower=0.0), period)
    rs = _safe_divide(avg_gain, avg_loss)
    return _last_valid(100.0 - (100.0 / (1.0 + rs)))


def calculate_macd(
    prices: pd.Series,
    fast: int = 12,
    slow: int = 26,
    sig: int = 9,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """MACD line, signal line, histogram — last valid values."""
    if not _require_series(prices, min_len=slow + sig, caller="MACD"):
        return None, None, None
    ema_f = prices.ewm(span=fast, adjust=False, min_periods=fast).mean()
    ema_s = prices.ewm(span=slow, adjust=False, min_periods=slow).mean()
    macd_line = ema_f - ema_s
    sig_line = macd_line.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return (
        _last_valid(macd_line),
        _last_valid(sig_line),
        _last_valid(macd_line - sig_line),
    )


def calculate_bollinger(
    prices: pd.Series,
    period: int = 20,
    nbdev: float = 2.0,
    ddof: int = 0,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Bollinger Bands (upper, middle, lower) — last valid values."""
    if not _require_series(prices, min_len=period, caller="BB"):
        return None, None, None
    mid = prices.rolling(period, min_periods=period).mean()
    std = prices.rolling(period, min_periods=period).std(ddof=ddof)
    return (
        _last_valid(mid + std * nbdev),
        _last_valid(mid),
        _last_valid(mid - std * nbdev),
    )


def _true_range(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
) -> pd.Series:
    """True Range — vectorized."""
    prev_c = closes.shift(1)
    return pd.concat(
        [
            (highs - lows).abs(),
            (highs - prev_c).abs(),
            (lows  - prev_c).abs(),
        ],
        axis=1,
    ).max(axis=1)


def calculate_atr(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 14,
) -> Tuple[Optional[float], Optional[float]]:
    """ATR and ATR% — last valid values."""
    if not _require_series(highs, lows, closes, min_len=period, caller="ATR"):
        return None, None
    atr = _wilder_smooth(_true_range(highs, lows, closes), period)
    return (
        _last_valid(atr),
        _last_valid(_safe_divide(atr, closes) * 100.0),
    )


def calculate_stoch(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    k_period: int = 14,
    d_period: int = 3,
) -> Tuple[Optional[float], Optional[float]]:
    """Stochastic %K and %D — last valid values."""
    if not _require_series(
        highs, lows, closes, min_len=k_period, caller="Stoch",
    ):
        return None, None
    ll = lows.rolling(k_period, min_periods=k_period).min()
    hh = highs.rolling(k_period, min_periods=k_period).max()
    k = 100.0 * _safe_divide(closes - ll, hh - ll)
    return (
        _last_valid(k),
        _last_valid(k.rolling(d_period, min_periods=d_period).mean()),
    )


def _directional_movement(
    highs: pd.Series,
    lows: pd.Series,
) -> Tuple[pd.Series, pd.Series]:
    """Raw +DM and -DM series."""
    up_move = highs.diff()
    dn_move = -lows.diff()
    plus_dm  = pd.Series(
        np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0),
        index=highs.index,
    )
    minus_dm = pd.Series(
        np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0),
        index=highs.index,
    )
    return plus_dm, minus_dm


def calculate_adx(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 14,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """ADX, +DI, -DI — last valid values."""
    if not _require_series(
        highs, lows, closes, min_len=period * 2, caller="ADX",
    ):
        return None, None, None
    atr_s = _wilder_smooth(_true_range(highs, lows, closes), period)
    plus_dm, minus_dm = _directional_movement(highs, lows)
    plus_di  = _safe_divide(
        100.0 * _wilder_smooth(plus_dm,  period), atr_s,
    )
    minus_di = _safe_divide(
        100.0 * _wilder_smooth(minus_dm, period), atr_s,
    )
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return (
        _last_valid(_wilder_smooth(dx, period)),
        _last_valid(plus_di),
        _last_valid(minus_di),
    )


# ══════════════════════════════════════════════════════════════
#  NEW INDICATORS v3.0
# ══════════════════════════════════════════════════════════════

def calculate_vwap(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    volumes: pd.Series,
) -> Optional[float]:
    """
    Volume Weighted Average Price (session VWAP).
    Uses typical price = (H + L + C) / 3.
    Returns last value of cumulative VWAP series.
    """
    if not _require_series(highs, lows, closes, volumes, min_len=2, caller="VWAP"):
        return None
    typical = (highs + lows + closes) / 3.0
    cum_tp_vol = (typical * volumes).cumsum()
    cum_vol    = volumes.cumsum()
    vwap_series = _safe_divide(cum_tp_vol, cum_vol)
    return _last_valid(vwap_series)


def calculate_ichimoku(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_period: int = 52,
) -> Dict[str, Optional[float]]:
    """
    Ichimoku Cloud components:
      tenkan_sen  — Conversion Line  (9-period midpoint)
      kijun_sen   — Base Line       (26-period midpoint)
      senkou_a    — Leading Span A  (avg of tenkan + kijun, shifted forward)
      senkou_b    — Leading Span B  (52-period midpoint, shifted forward)
      chikou_span — Lagging Span    (close shifted BACK by kijun_period)

    Returns dict of last valid values.

    FIX v3.3:
      `chikou_span` = `closes.shift(+kijun_period)`.
      The lagging span is plotted `kijun_period` bars BEHIND the current
      bar, i.e. `chikou[t] = close[t - kijun_period]`.  In pandas that is
      a POSITIVE shift.  v3.2 used a negative shift, which pushed values
      forward and produced NaN at the tail (so `_last_valid` returned the
      current close instead of the lagging value).
    """
    empty = {
        "tenkan_sen": None, "kijun_sen": None,
        "senkou_a": None,   "senkou_b": None,
        "chikou_span": None,
    }
    if not _require_series(
        highs, lows, closes, min_len=senkou_period, caller="Ichimoku",
    ):
        return empty

    def _midpoint(h: pd.Series, l: pd.Series, p: int) -> pd.Series:
        return (
            h.rolling(p, min_periods=p).max() +
            l.rolling(p, min_periods=p).min()
        ) / 2.0

    tenkan = _midpoint(highs, lows, tenkan_period)
    kijun  = _midpoint(highs, lows, kijun_period)
    senkou_a = ((tenkan + kijun) / 2.0).shift(kijun_period)
    senkou_b = _midpoint(highs, lows, senkou_period).shift(kijun_period)

    # FIX v3.3: chikou is the historical close, so a POSITIVE shift.
    chikou = closes.shift(kijun_period)

    return {
        "tenkan_sen":  _last_valid(tenkan),
        "kijun_sen":   _last_valid(kijun),
        "senkou_a":    _last_valid(senkou_a),
        "senkou_b":    _last_valid(senkou_b),
        "chikou_span": _last_valid(chikou),
    }


def calculate_cci(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 20,
) -> Optional[float]:
    """
    Commodity Channel Index.
    CCI = (Typical Price − SMA(TP)) / (0.015 × Mean Deviation)
    """
    if not _require_series(highs, lows, closes, min_len=period, caller="CCI"):
        return None
    typical  = (highs + lows + closes) / 3.0
    sma_tp   = typical.rolling(period, min_periods=period).mean()
    mean_dev = typical.rolling(period, min_periods=period).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True,
    )
    cci = _safe_divide(typical - sma_tp, 0.015 * mean_dev)
    return _last_valid(cci)


def calculate_williams_r(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    period: int = 14,
) -> Optional[float]:
    """
    Williams %R.
    %R = (Highest High − Close) / (Highest High − Lowest Low) × −100
    Range: −100 (oversold) to 0 (overbought).
    """
    if not _require_series(highs, lows, closes, min_len=period, caller="WillR"):
        return None
    hh = highs.rolling(period, min_periods=period).max()
    ll = lows.rolling(period, min_periods=period).min()
    willr = -100.0 * _safe_divide(hh - closes, hh - ll)
    return _last_valid(willr)


def calculate_momentum(
    prices: pd.Series,
    period: int = 10,
) -> Optional[float]:
    """
    Rate-of-Change Momentum: (Close − Close[n]) / Close[n] × 100
    """
    if not _require_series(prices, min_len=period + 1, caller="Momentum"):
        return None
    mom = _safe_divide(
        prices - prices.shift(period),
        prices.shift(period),
    ) * 100.0
    return _last_valid(mom)


def calculate_historical_volatility(
    prices: pd.Series,
    period_days: int = 30,
    annualize_factor: float = 8760.0,
    is_hourly: bool = True,
) -> Optional[float]:
    """
    Historical Volatility (annualized).

    FIX v3.0: correct annualization.
      - hourly data  → annualize_factor = 24 * 365 = 8760
      - daily data   → annualize_factor = 252  (trading days)

    Returns annualized stddev of log returns.
    """
    candles_needed = period_days * (24 if is_hourly else 1)
    if not _require_series(prices, min_len=candles_needed + 1, caller="HV"):
        return None

    log_ret = np.log(prices / prices.shift(1)).dropna()
    window  = log_ret.iloc[-candles_needed:]

    if len(window) < 5:
        return None

    std_per_candle = float(window.std(ddof=1))
    return round(std_per_candle * (annualize_factor ** 0.5), 6)


# ══════════════════════════════════════════════════════════════
#  PATTERN DETECTION
# ══════════════════════════════════════════════════════════════

def detect_divergence(
    prices: pd.Series,
    indicator: pd.Series,
    lookback: int = 14,
) -> str:
    """Detect bullish/bearish divergence between price and indicator."""
    if not _require_series(prices, indicator, min_len=5, caller="divergence"):
        return "none"

    combined = pd.concat([prices, indicator], axis=1).dropna()
    if len(combined) < 5:
        return "none"

    n   = min(lookback, len(combined))
    p   = combined.iloc[-n:, 0].to_numpy(dtype=float)
    ind = combined.iloc[-n:, 1].to_numpy(dtype=float)

    if len(p) < 5:
        return "none"

    def _peaks(a: np.ndarray) -> List[int]:
        return [
            i for i in range(1, len(a) - 1)
            if a[i] > a[i - 1] and a[i] > a[i + 1]
        ]

    def _troughs(a: np.ndarray) -> List[int]:
        return [
            i for i in range(1, len(a) - 1)
            if a[i] < a[i - 1] and a[i] < a[i + 1]
        ]

    p_pk, i_pk = _peaks(p), _peaks(ind)
    if (len(p_pk) >= 2 and len(i_pk) >= 2
            and p[p_pk[-1]]   > p[p_pk[-2]]
            and ind[i_pk[-1]] < ind[i_pk[-2]]):
        return "bearish"

    p_tr, i_tr = _troughs(p), _troughs(ind)
    if (len(p_tr) >= 2 and len(i_tr) >= 2
            and p[p_tr[-1]]   < p[p_tr[-2]]
            and ind[i_tr[-1]] > ind[i_tr[-2]]):
        return "bullish"

    return "none"


def find_support_resistance(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    lookback: int = 50,
    pivot_window: int = 2,
    max_levels: int = 3,
) -> Dict[str, List[float]]:
    """
    Identify support and resistance levels via pivot-point detection.
    """
    empty: Dict[str, List[float]] = {"support": [], "resistance": []}
    if not _require_series(highs, lows, closes, min_len=5, caller="SR"):
        return empty

    n  = min(lookback, len(closes))
    h  = highs.iloc[-n:].reset_index(drop=True).astype(float)
    lo = lows.iloc[-n:].reset_index(drop=True).astype(float)
    c  = closes.iloc[-n:].reset_index(drop=True).astype(float)

    curr_p = _last_valid(c)
    if curr_p is None:
        return empty

    length = len(h)
    pw     = pivot_window
    res_levels: List[float] = []
    sup_levels: List[float] = []

    for i in range(pw, length - pw):
        left_h  = h.iloc[i - pw : i]
        right_h = h.iloc[i + 1 : i + pw + 1]
        if not left_h.empty and not right_h.empty:
            if h.iloc[i] >= left_h.max() and h.iloc[i] >= right_h.max():
                v = float(h.iloc[i])
                if np.isfinite(v) and v > curr_p:
                    res_levels.append(round(v, 8))

        left_l  = lo.iloc[i - pw : i]
        right_l = lo.iloc[i + 1 : i + pw + 1]
        if not left_l.empty and not right_l.empty:
            if lo.iloc[i] <= left_l.min() and lo.iloc[i] <= right_l.min():
                v = float(lo.iloc[i])
                if np.isfinite(v) and v < curr_p:
                    sup_levels.append(round(v, 8))

    return {
        "support":    sorted(set(sup_levels), reverse=True)[:max_levels],
        "resistance": sorted(set(res_levels))[:max_levels],
    }


# ══════════════════════════════════════════════════════════════
#  BATCH CALCULATION — DataFrame path
# ══════════════════════════════════════════════════════════════

def calculate_indicators_df(
    df: pd.DataFrame,
    min_rows: int = 30,
    cfg: Optional[Dict[str, Any]] = None,
) -> pd.DataFrame:
    """
    Compute all indicators in-place on a copy of df.
    Returns enriched DataFrame with all indicator columns.

    v3.1: warns when the input has fewer rows than `ema_slow` so the
    operator knows EMA200 is NaN for this dataset.

    v3.3: Ichimoku chikou span now uses `close.shift(+kijun)` — a
    POSITIVE shift.  v3.2's negative shift produced NaN at the tail.
    """
    if df is None or not isinstance(df, pd.DataFrame):
        logger.warning("calculate_indicators_df: invalid input")
        return pd.DataFrame()

    if len(df) < min_rows:
        logger.warning(
            "calculate_indicators_df: %d rows < min_rows=%d",
            len(df), min_rows,
        )
        return df.copy()

    config = cfg or INDICATOR_CONFIG
    out = df.copy()

    c  = _get_column(out, *_CLOSE_COLS)
    h  = _get_column(out, *_HIGH_COLS)
    lo = _get_column(out, *_LOW_COLS)
    v  = _get_column(out, *_VOLUME_COLS)

    if c is None:
        logger.error(
            "calculate_indicators_df: 'close' not found. Columns: %s",
            list(out.columns),
        )
        return out

    if h is None:
        logger.warning("'high' missing — using 'close' as fallback.")
        h = c
    if lo is None:
        logger.warning("'low' missing — using 'close' as fallback.")
        lo = c

    emit_prev = bool(config.get("emit_prev", True))

    ef = int(config.get("ema_fast", 50))
    es = int(config.get("ema_slow", 200))

    if len(out) < es:
        logger.warning(
            "EMA%d will be all NaN: only %d rows available, need %d.",
            es, len(out), es,
        )
    if len(out) < ef:
        logger.warning(
            "EMA%d will be all NaN: only %d rows available, need %d.",
            ef, len(out), ef,
        )

    # ── RSI ──────────────────────────────────────────────────
    period_rsi = int(config.get("rsi_period", 14))
    delta      = c.diff()
    rs = _safe_divide(
        _wilder_smooth(delta.clip(lower=0.0), period_rsi),
        _wilder_smooth((-delta).clip(lower=0.0), period_rsi),
    )
    out["RSI"] = 100.0 - (100.0 / (1.0 + rs))
    if emit_prev:
        out["RSI_prev"] = out["RSI"].shift(1)

    # ── MACD ─────────────────────────────────────────────────
    mf, ms, mg = (
        int(config.get("macd_fast", 12)),
        int(config.get("macd_slow", 26)),
        int(config.get("macd_signal", 9)),
    )
    ema_f = c.ewm(span=mf, adjust=False, min_periods=mf).mean()
    ema_s = c.ewm(span=ms, adjust=False, min_periods=ms).mean()
    out["MACD"]           = ema_f - ema_s
    out["MACD Signal"]    = out["MACD"].ewm(span=mg, adjust=False, min_periods=mg).mean()
    out["MACD_Histogram"] = out["MACD"] - out["MACD Signal"]
    if emit_prev:
        out["MACD_prev"]        = out["MACD"].shift(1)
        out["MACD_Signal_prev"] = out["MACD Signal"].shift(1)

    # ── Bollinger Bands ───────────────────────────────────────
    bp    = int(config.get("bb_period", 20))
    bsd   = float(config.get("bb_std", 2.0))
    bddof = int(config.get("bb_ddof", 0))
    mid   = c.rolling(bp, min_periods=bp).mean()
    std   = c.rolling(bp, min_periods=bp).std(ddof=bddof)
    out["BB Upper"]    = mid + std * bsd
    out["BB Middle"]   = mid
    out["BB Lower"]    = mid - std * bsd
    out["BB Width %"]  = _safe_divide(out["BB Upper"] - out["BB Lower"], mid) * 100.0
    out["BB_PercentB"] = _safe_divide(c - out["BB Lower"],
                                      out["BB Upper"] - out["BB Lower"])

    # ── ATR ──────────────────────────────────────────────────
    atr_p      = int(config.get("atr_period", 14))
    tr         = _true_range(h, lo, c)
    out["ATR"] = _wilder_smooth(tr, atr_p)
    out["ATR %"] = _safe_divide(out["ATR"], c) * 100.0

    # ── Stochastic ────────────────────────────────────────────
    sk, sd_ = int(config.get("stoch_k", 14)), int(config.get("stoch_d", 3))
    ll_s = lo.rolling(sk, min_periods=sk).min()
    hh_s = h.rolling(sk, min_periods=sk).max()
    out["Stochastic_K"] = 100.0 * _safe_divide(c - ll_s, hh_s - ll_s)
    out["Stochastic_D"] = out["Stochastic_K"].rolling(sd_, min_periods=sd_).mean()
    out["Stochastic"]   = out["Stochastic_K"]

    # ── EMA ──────────────────────────────────────────────────
    out["EMA50"]    = c.ewm(span=ef, adjust=False, min_periods=ef).mean()
    out["EMA200"]   = c.ewm(span=es, adjust=False, min_periods=es).mean()
    ema_delta = out["EMA50"] - out["EMA200"]
    out["EMA_Cross"] = np.sign(ema_delta).fillna(0).astype(int)

    # ── ADX / DI ─────────────────────────────────────────────
    adx_p        = int(config.get("adx_period", 14))
    plus_dm, minus_dm = _directional_movement(h, lo)
    atr_adx      = _wilder_smooth(tr, adx_p)
    out["+DI"]   = _safe_divide(100.0 * _wilder_smooth(plus_dm,  adx_p), atr_adx)
    out["-DI"]   = _safe_divide(100.0 * _wilder_smooth(minus_dm, adx_p), atr_adx)
    di_sum       = (out["+DI"] + out["-DI"]).replace(0.0, np.nan)
    dx           = 100.0 * (out["+DI"] - out["-DI"]).abs() / di_sum
    out["ADX"]   = _wilder_smooth(dx, adx_p)
    if emit_prev:
        out["ADX_prev"] = out["ADX"].shift(1)

    # ── CCI ──────────────────────────────────────────────────
    cci_p = int(config.get("cci_period", 20))
    tp    = (h + lo + c) / 3.0
    sma_tp = tp.rolling(cci_p, min_periods=cci_p).mean()
    mean_dev = tp.rolling(cci_p, min_periods=cci_p).apply(
        lambda x: np.mean(np.abs(x - np.mean(x))), raw=True,
    )
    out["CCI"] = _safe_divide(tp - sma_tp, 0.015 * mean_dev)

    # ── Williams %R ──────────────────────────────────────────
    wr_p = int(config.get("willr_period", 14))
    hh_w = h.rolling(wr_p, min_periods=wr_p).max()
    ll_w = lo.rolling(wr_p, min_periods=wr_p).min()
    out["Williams_%R"] = -100.0 * _safe_divide(hh_w - c, hh_w - ll_w)

    # ── Momentum ─────────────────────────────────────────────
    mom_p = int(config.get("mom_period", 10))
    out["Momentum"] = _safe_divide(
        c - c.shift(mom_p), c.shift(mom_p),
    ) * 100.0

    # ── Volume indicators ────────────────────────────────────
    if v is not None:
        vol_ma = int(config.get("volume_ma", 20))
        out["Volume_MA"]       = v.rolling(vol_ma, min_periods=vol_ma).mean()
        out["Relative_Volume"] = _safe_divide(v, out["Volume_MA"])

        if config.get("obv_enabled", True):
            out["OBV"] = (
                np.sign(c.diff().fillna(0.0)) * v
            ).cumsum()

        if config.get("vwap_enabled", True):
            typical_p = (h + lo + c) / 3.0
            cum_pv    = (typical_p * v).cumsum()
            cum_v     = v.cumsum()
            out["VWAP"] = _safe_divide(cum_pv, cum_v)
    else:
        logger.debug("Volume column not found — skipping volume indicators.")

    # ── Ichimoku ─────────────────────────────────────────────
    ichi_t = int(config.get("ichi_tenkan", 9))
    ichi_k = int(config.get("ichi_kijun", 26))
    ichi_s = int(config.get("ichi_senkou", 52))

    def _mp(high_s, low_s, p):
        return (
            high_s.rolling(p, min_periods=p).max() +
            low_s.rolling(p, min_periods=p).min()
        ) / 2.0

    out["Ichi_Tenkan"] = _mp(h, lo, ichi_t)
    out["Ichi_Kijun"]  = _mp(h, lo, ichi_k)
    out["Ichi_SenkouA"] = (
        (out["Ichi_Tenkan"] + out["Ichi_Kijun"]) / 2.0
    ).shift(ichi_k)
    out["Ichi_SenkouB"] = _mp(h, lo, ichi_s).shift(ichi_k)
    # FIX v3.3: chikou span = historical close, POSITIVE shift.
    out["Ichi_Chikou"]  = c.shift(ichi_k)

    logger.debug(
        "calculate_indicators_df: %d columns for %d rows.",
        len(out.columns), len(out),
    )
    return out


# ══════════════════════════════════════════════════════════════
#  CALCULATE ALL — single-row dict
# ══════════════════════════════════════════════════════════════

def calculate_all_indicators(
    highs: pd.Series,
    lows: pd.Series,
    closes: pd.Series,
    volumes: Optional[pd.Series] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Optional[float]]:
    """
    Compute latest value of every indicator, keyed in lowercase.
    Suitable for single-symbol scoring in signals.py.
    """
    if not _require_series(closes, min_len=2, caller="calc_all"):
        return {}

    cfg  = config or INDICATOR_CONFIG
    h_s  = highs  if _require_series(highs,  min_len=2) else closes
    lo_s = lows   if _require_series(lows,   min_len=2) else closes

    rsi = calculate_rsi(closes, cfg.get("rsi_period", 14))

    macd_line, macd_sig, macd_hist = calculate_macd(
        closes,
        cfg.get("macd_fast",   12),
        cfg.get("macd_slow",   26),
        cfg.get("macd_signal",  9),
    )
    bb_up, bb_mid, bb_low = calculate_bollinger(
        closes,
        cfg.get("bb_period", 20),
        cfg.get("bb_std",     2.0),
        cfg.get("bb_ddof",    0),
    )
    atr_val, atr_pct = calculate_atr(
        h_s, lo_s, closes, cfg.get("atr_period", 14),
    )
    stoch_k, stoch_d = calculate_stoch(
        h_s, lo_s, closes,
        cfg.get("stoch_k", 14),
        cfg.get("stoch_d",  3),
    )
    adx_val, plus_di, minus_di = calculate_adx(
        h_s, lo_s, closes, cfg.get("adx_period", 14),
    )
    ema50  = calculate_ema(closes, cfg.get("ema_fast",  50))
    ema200 = calculate_ema(closes, cfg.get("ema_slow", 200))
    cci    = calculate_cci(h_s, lo_s, closes, cfg.get("cci_period", 20))
    willr  = calculate_williams_r(h_s, lo_s, closes, cfg.get("willr_period", 14))
    mom    = calculate_momentum(closes, cfg.get("mom_period", 10))
    hv     = calculate_historical_volatility(
        closes,
        period_days=int(cfg.get("hv_period", 30)),
        annualize_factor=float(cfg.get("hv_annualize_factor", 8760)),
    )

    bb_width_pct: Optional[float] = None
    percent_b:    Optional[float] = None
    if bb_up is not None and bb_low is not None:
        if bb_mid is not None and bb_mid != 0.0:
            bb_width_pct = round((bb_up - bb_low) / bb_mid * 100.0, 4)
        if bb_up != bb_low:
            last_close = _last_valid(closes)
            if last_close is not None:
                percent_b = round((last_close - bb_low) / (bb_up - bb_low), 4)

    obv:     Optional[float] = None
    rel_vol: Optional[float] = None
    vwap:    Optional[float] = None

    if volumes is not None and _require_series(volumes, min_len=2):
        obv = float(
            (np.sign(closes.diff().fillna(0)) * volumes).cumsum().iloc[-1]
        )
        vol_ma_period = int(cfg.get("volume_ma", 20))
        vol_ma_val    = _last_valid(
            volumes.rolling(vol_ma_period, min_periods=vol_ma_period).mean()
        )
        last_vol = _last_valid(volumes)
        if vol_ma_val is not None and vol_ma_val > 0 and last_vol is not None:
            rel_vol = round(last_vol / vol_ma_val, 4)

        if cfg.get("vwap_enabled", True):
            vwap = calculate_vwap(h_s, lo_s, closes, volumes)

    return {
        "rsi":          rsi,
        "macd":         macd_line,
        "macd_signal":  macd_sig,
        "macd_hist":    macd_hist,
        "bb_upper":     bb_up,
        "bb_middle":    bb_mid,
        "bb_lower":     bb_low,
        "bb_width_pct": bb_width_pct,
        "bb_percent_b": percent_b,
        "atr":          atr_val,
        "atr_pct":      atr_pct,
        "stoch_k":      stoch_k,
        "stoch_d":      stoch_d,
        "adx":          adx_val,
        "plus_di":      plus_di,
        "minus_di":     minus_di,
        "ema50":        ema50,
        "ema200":       ema200,
        "ema_cross":    _ema_cross_signal(ema50, ema200),
        "cci":          cci,
        "williams_r":   willr,
        "momentum":     mom,
        "obv":          obv,
        "relative_volume": rel_vol,
        "vwap":         vwap,
        "historical_volatility": hv,
    }


# ══════════════════════════════════════════════════════════════
#  STREAMING ENGINE — Real-time incremental calculation
# ══════════════════════════════════════════════════════════════

@dataclass
class RollingIndicatorEngine:
    """
    Maintains a sliding window of OHLCV data and computes indicators
    incrementally. Designed for WebSocket / real-time feed integration.
    """
    max_candles: int = 500
    config: Dict[str, Any] = field(default_factory=lambda: INDICATOR_CONFIG.copy())
    _data: List[Dict[str, float]] = field(default_factory=list, init=False)

    def push(
        self,
        open_:     float,
        high:      float,
        low:       float,
        close:     float,
        volume:    float,
        timestamp: Optional[Any] = None,
    ) -> None:
        """Append one candle. Drops oldest if over max_candles."""
        self._data.append({
            "open":      open_,
            "high":      high,
            "low":       low,
            "close":     close,
            "volume":    volume,
            "timestamp": timestamp or time.time(),
        })
        if len(self._data) > self.max_candles:
            self._data.pop(0)

    def push_candle(self, candle: Dict[str, float]) -> None:
        """Convenience: push from dict."""
        self.push(
            open_  = float(candle.get("open",   candle.get("o", 0))),
            high   = float(candle.get("high",   candle.get("h", 0))),
            low    = float(candle.get("low",    candle.get("l", 0))),
            close  = float(candle.get("close",  candle.get("c", 0))),
            volume = float(candle.get("volume", candle.get("v", 0))),
            timestamp = candle.get("timestamp", candle.get("t")),
        )

    def _to_df(self) -> pd.DataFrame:
        return pd.DataFrame(self._data)

    def compute(self) -> Dict[str, Optional[float]]:
        """Compute all indicators on current window. Returns single-row dict."""
        df = self._to_df()
        if df.empty or len(df) < 2:
            return {}
        return calculate_all_indicators(
            highs   = df["high"],
            lows    = df["low"],
            closes  = df["close"],
            volumes = df["volume"],
            config  = self.config,
        )

    def compute_df(self) -> pd.DataFrame:
        """Compute full indicator DataFrame on current window."""
        return calculate_indicators_df(self._to_df(), min_rows=30, cfg=self.config)

    @property
    def candle_count(self) -> int:
        return len(self._data)

    @property
    def latest_close(self) -> Optional[float]:
        return self._data[-1]["close"] if self._data else None

    def reset(self) -> None:
        self._data.clear()


# ══════════════════════════════════════════════════════════════
#  MULTI-TIMEFRAME BATCH
# ══════════════════════════════════════════════════════════════

def calculate_indicators_df_multi_tf(
    frames: Dict[str, pd.DataFrame],
    min_rows: int = 30,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, pd.DataFrame]:
    """
    Compute indicators for multiple timeframes simultaneously.
    """
    result: Dict[str, pd.DataFrame] = {}
    for label, df in frames.items():
        try:
            result[label] = calculate_indicators_df(df, min_rows=min_rows, cfg=cfg)
            logger.debug("Multi-TF: computed '%s' (%d rows)", label, len(df))
        except Exception as exc:
            logger.error("Multi-TF: failed for '%s': %s", label, exc)
            result[label] = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    return result


# ══════════════════════════════════════════════════════════════
#  EXPORT FORMATTER — signals.py compatibility
# ══════════════════════════════════════════════════════════════

def export_to_signal_format(
    indicators: Dict[str, Optional[float]],
    price: Optional[float] = None,
    change_1h: Optional[float] = None,
    change_24h: Optional[float] = None,
    support: Optional[float] = None,
    resistance: Optional[float] = None,
    market_cap: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Convert calculate_all_indicators() output to the exact key schema
    expected by signals.py.
    """
    def _g(key: str) -> Optional[float]:
        return indicators.get(key)

    return {
        "Price":               price,
        "1h Change (%)":       change_1h,
        "24h Change (%)":      change_24h,
        "Market Cap":          market_cap,
        "RSI":                 _g("rsi"),
        "MACD":                _g("macd"),
        "MACD Signal":         _g("macd_signal"),
        "MACD Histogram":      _g("macd_hist"),
        "MACD_prev":           _g("macd_prev"),
        "MACD_Signal_prev":    _g("macd_signal_prev"),
        "BB Upper":            _g("bb_upper"),
        "BB Lower":            _g("bb_lower"),
        "BB Width %":          _g("bb_width_pct"),
        "BB_PercentB":         _g("bb_percent_b"),
        "ATR %":               _g("atr_pct"),
        "ADX":                 _g("adx"),
        "+DI":                 _g("plus_di"),
        "-DI":                 _g("minus_di"),
        "EMA50":               _g("ema50"),
        "EMA200":              _g("ema200"),
        "EMA_Cross":           _g("ema_cross"),
        "Stochastic_K":        _g("stoch_k"),
        "Stochastic_D":        _g("stoch_d"),
        "CCI":                 _g("cci"),
        "Williams_%R":         _g("williams_r"),
        "Momentum":            _g("momentum"),
        "Relative Volume":     _g("relative_volume"),
        "OBV":                 _g("obv"),
        "VWAP":                _g("vwap"),
        "Historical_Volatility_30d": _g("historical_volatility"),
        "Support":             support,
        "Resistance":          resistance,
    }


# ══════════════════════════════════════════════════════════════
#  BENCHMARK UTILITY
# ══════════════════════════════════════════════════════════════

def benchmark_indicators(
    n_rows: int = 1000,
    n_runs: int = 5,
) -> Dict[str, float]:
    """
    Benchmark calculate_indicators_df on synthetic data.
    """
    rng = np.random.default_rng(42)
    prices  = 100 + np.cumsum(rng.standard_normal(n_rows)) * 0.5
    df_test = pd.DataFrame({
        "open":   prices * (1 + rng.uniform(-0.002, 0.002, n_rows)),
        "high":   prices * (1 + rng.uniform( 0.000, 0.005, n_rows)),
        "low":    prices * (1 - rng.uniform( 0.000, 0.005, n_rows)),
        "close":  prices,
        "volume": rng.integers(1_000, 1_000_000, n_rows).astype(float),
    })
    times: List[float] = []
    for _ in range(n_runs):
        t0 = time.perf_counter()
        calculate_indicators_df(df_test, min_rows=50)
        times.append(time.perf_counter() - t0)

    return {
        "mean_s": round(float(np.mean(times)), 4),
        "min_s":  round(float(np.min(times)),  4),
        "max_s":  round(float(np.max(times)),  4),
        "n_rows": n_rows,
        "n_runs": n_runs,
    }


# ══════════════════════════════════════════════════════════════
#  TREND INDICATORS
# ══════════════════════════════════════════════════════════════

def compute_trend_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """
    Compute MA20 / MA50 and a trend score.

    FIX v3.3: the price column is now resolved through `_get_column`
    so DataFrames that use `Close` / `Price` / `C` work correctly
    instead of raising KeyError on the literal `close` name.
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty or len(df) < 50:
        return {}

    df = df.copy()
    price_col = _get_column(df, *_CLOSE_COLS)
    if price_col is None:
        logger.warning(
            "compute_trend_indicators: no close/price column found "
            "(looked for %s); columns=%s",
            _CLOSE_COLS, list(df.columns),
        )
        return {}

    price_name = price_col.name if price_col.name is not None else "close"
    df["MA20"] = price_col.rolling(window=20).mean()
    df["MA50"] = price_col.rolling(window=50).mean()

    last_row = df.iloc[-1]
    ma20 = last_row["MA20"]
    ma50 = last_row["MA50"]
    price = last_row[price_name]

    if pd.isna(ma20) or pd.isna(ma50):
        return {}

    if ma20 > ma50 and price > ma20:
        trend = "Uptrend"
        score = 80
    elif ma20 > ma50:
        trend = "Weak Uptrend"
        score = 60
    elif ma20 < ma50 and price < ma20:
        trend = "Downtrend"
        score = 20
    elif ma20 < ma50:
        trend = "Weak Downtrend"
        score = 40
    else:
        trend = "Neutral"
        score = 50

    return {
        "Trend": trend,
        "Trend Score": score,
        "MA20": ma20,
        "MA50": ma50,
        "Close": price,
    }


# ══════════════════════════════════════════════════════════════
#  PUBLIC API
# ══════════════════════════════════════════════════════════════

__all__ = [
    "INDICATOR_CONFIG",
    "validate_config",
    "TA_LIB_AVAILABLE",
    "calculate_sma",
    "calculate_ema",
    "calculate_rsi",
    "calculate_macd",
    "calculate_bollinger",
    "calculate_atr",
    "calculate_stoch",
    "calculate_adx",
    "calculate_vwap",
    "calculate_ichimoku",
    "calculate_cci",
    "calculate_williams_r",
    "calculate_momentum",
    "calculate_historical_volatility",
    "detect_divergence",
    "find_support_resistance",
    "calculate_indicators_df",
    "calculate_indicators_df_multi_tf",
    "calculate_all_indicators",
    "export_to_signal_format",
    "RollingIndicatorEngine",
    "benchmark_indicators",
    "compute_trend_indicators",
]