"""
Tests for analysis/indicators.py v3.3 fixes.

Bugs being covered:
1. `calculate_ichimoku()` returned `closes.shift(-kijun)`, which pushes
   values FORWARD and produces NaN at the tail.  The `_last_valid()`
   helper then fell back to the current close, so the chikou span was
   effectively wrong.  Standard Ichimoku uses a POSITIVE shift.

2. `compute_trend_indicators()` assumed a literal `close` column and
   raised KeyError for DataFrames that use `Close` / `Price` / `C`.

NOTE on shift semantics:
    `closes.shift(kijun)` moves data FORWARD by `kijun` slots.
    The last valid value of the shifted series is `close[-(kijun+1)]`.
    An earlier draft of this test used `close[-kijun]`, which is off
    by one.  The implementation is correct; the expectation was not.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis.indicators import (
    calculate_ichimoku,
    compute_trend_indicators,
)


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def price_series():
    rng = np.random.default_rng(seed=7)
    n = 200
    close = 100.0 + np.cumsum(rng.standard_normal(n) * 0.8)
    close = np.clip(close, 1.0, None)
    high  = close + rng.uniform(0.2, 1.5, n)
    low   = np.maximum(close - rng.uniform(0.2, 1.5, n), 0.01)
    return pd.Series(close), pd.Series(high), pd.Series(low)


# ══════════════════════════════════════════════════════════════
# 1. Chikou span direction
# ══════════════════════════════════════════════════════════════

def test_chikou_is_the_historical_close(price_series):
    """chikou_span at the tail equals close[-(kijun+1)]."""
    close, high, low = price_series
    kijun = 26
    res = calculate_ichimoku(
        high, low, close,
        tenkan_period=9,
        kijun_period=kijun,
        senkou_period=52,
    )
    # shift(kijun) moves data forward; the LAST valid value is at
    # index -1 of the shifted series, which corresponds to close[-(kijun+1)].
    expected = float(close.iloc[-(kijun + 1)])
    assert res["chikou_span"] is not None
    assert abs(res["chikou_span"] - expected) < 1e-9, (
        f"chikou_span={res['chikou_span']}, expected={expected}"
    )


def test_chikou_is_not_the_current_close(price_series):
    """Regression check: the OLD (buggy) behaviour returned the current close."""
    close, high, low = price_series
    res = calculate_ichimoku(high, low, close, kijun_period=26)
    current_close = float(close.iloc[-1])
    # Ensure the two values would visibly differ if buggy.
    if abs(current_close - float(close.iloc[-27])) > 1e-6:
        assert abs(res["chikou_span"] - current_close) > 1e-6, (
            "chikou_span must not equal the current close"
        )


def test_ichimoku_dict_has_all_expected_keys(price_series):
    close, high, low = price_series
    res = calculate_ichimoku(high, low, close)
    for key in ("tenkan_sen", "kijun_sen", "senkou_a", "senkou_b", "chikou_span"):
        assert key in res, f"missing {key}"


def test_ichimoku_handles_short_series_gracefully():
    close = pd.Series([100.0, 101.0, 102.0])
    high  = close + 0.5
    low   = close - 0.5
    res = calculate_ichimoku(high, low, close)
    # All values should be None (insufficient data) but no exception.
    for key in ("tenkan_sen", "kijun_sen", "senkou_a", "senkou_b", "chikou_span"):
        assert res[key] is None


# ══════════════════════════════════════════════════════════════
# 2. compute_trend_indicators column resolution
# ══════════════════════════════════════════════════════════════

def _build_df(price_col_name: str, n: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(seed=3)
    close = 100.0 + np.cumsum(rng.standard_normal(n) * 0.5)
    close = np.clip(close, 1.0, None)
    return pd.DataFrame({price_col_name: close})


@pytest.mark.parametrize("col", ["close", "Close", "Price", "C"])
def test_trend_indicators_accepts_all_aliases(col):
    df = _build_df(col, n=80)
    res = compute_trend_indicators(df)
    assert isinstance(res, dict)
    assert "Trend" in res
    assert "Trend Score" in res
    assert "MA20" in res
    assert "MA50" in res


def test_trend_indicators_returns_empty_for_short_df():
    df = _build_df("close", n=30)   # < 50 rows
    res = compute_trend_indicators(df)
    assert res == {}


def test_trend_indicators_returns_empty_when_no_price_column():
    df = pd.DataFrame({"foo": range(80), "bar": range(80)})
    res = compute_trend_indicators(df)
    assert res == {}


def test_trend_indicators_returns_empty_for_none():
    assert compute_trend_indicators(None) == {}


def test_trend_indicators_with_uptrend_scores_high():
    n = 80
    close = np.linspace(100.0, 200.0, n)
    df = pd.DataFrame({"Close": close})
    res = compute_trend_indicators(df)
    assert res["Trend"] in ("Uptrend", "Weak Uptrend")
    assert res["Trend Score"] >= 60


def test_trend_indicators_with_downtrend_scores_low():
    n = 80
    close = np.linspace(200.0, 100.0, n)
    df = pd.DataFrame({"Price": close})
    res = compute_trend_indicators(df)
    assert res["Trend"] in ("Downtrend", "Weak Downtrend")
    assert res["Trend Score"] <= 40