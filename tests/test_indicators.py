# tests/test_indicators.py
"""
Unit tests for analysis/indicators.py  (compatible with v2.0)

Coverage (only functions that actually exist):
    - Moving Averages : SMA, EMA
    - Momentum        : RSI
    - Trend           : MACD, ADX, Stochastic
    - Volatility      : Bollinger Bands (3 values), ATR
    - Advanced        : Divergence, Support/Resistance
    - Batch           : calculate_all_indicators, calculate_indicators_df
    - Edge Cases
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from analysis.indicators import (
    calculate_adx,
    calculate_all_indicators,
    calculate_atr,
    calculate_bollinger,
    calculate_ema,
    calculate_indicators_df,
    calculate_macd,
    calculate_rsi,
    calculate_sma,
    calculate_stoch,
    detect_divergence,
    find_support_resistance,
)

SAMPLE_SIZE = 120
_MIN_INDICATORS = 10


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def rng() -> np.random.Generator:
    return np.random.default_rng(seed=42)


@pytest.fixture(scope="module")
def price_data(rng) -> dict:
    close = 100.0 + np.cumsum(rng.standard_normal(SAMPLE_SIZE) * 2)
    close = np.clip(close, 1.0, None)
    spread = rng.uniform(0.5, 2.5, SAMPLE_SIZE)
    high = close + spread
    low = np.maximum(close - spread, 0.01)
    volume = rng.integers(1_000_000, 10_000_000, SAMPLE_SIZE).astype(float)

    return {
        "close": pd.Series(close),
        "high": pd.Series(high),
        "low": pd.Series(low),
        "volume": pd.Series(volume),
    }


@pytest.fixture(scope="module")
def ohlcv_df(price_data) -> pd.DataFrame:
    return pd.DataFrame({
        "open": price_data["close"] * 0.999,
        "high": price_data["high"],
        "low": price_data["low"],
        "close": price_data["close"],
        "volume": price_data["volume"],
    })


@pytest.fixture
def tiny_prices() -> pd.Series:
    return pd.Series([10.0, 11.0, 10.5, 12.0, 11.5])


@pytest.fixture
def constant_prices() -> pd.Series:
    return pd.Series(np.full(30, 100.0))


@pytest.fixture
def nan_prices() -> pd.Series:
    arr = 100.0 + np.cumsum(np.random.randn(50) * 2)
    arr[[5, 15, 30]] = np.nan
    return pd.Series(arr)


def _is_valid_float(val) -> bool:
    if val is None:
        return False
    try:
        f = float(val)
        return np.isfinite(f)
    except (TypeError, ValueError):
        return False


# ══════════════════════════════════════════════════════════════
# Moving Averages
# ══════════════════════════════════════════════════════════════

class TestMovingAverages:

    def test_sma_basic(self, price_data):
        sma = calculate_sma(price_data["close"], period=20)
        assert _is_valid_float(sma)
        assert float(sma) > 0

    def test_sma_equals_manual(self, price_data):
        period = 10
        sma = calculate_sma(price_data["close"], period=period)
        expected = float(price_data["close"].iloc[-period:].mean())
        assert abs(float(sma) - expected) < 0.01

    @pytest.mark.parametrize("period", [5, 10, 20, 50])
    def test_sma_periods(self, price_data, period):
        sma = calculate_sma(price_data["close"], period=period)
        assert _is_valid_float(sma)

    def test_sma_insufficient_data(self, tiny_prices):
        sma = calculate_sma(tiny_prices, period=50)
        assert sma is None or (isinstance(sma, float) and np.isnan(sma))

    def test_ema_basic(self, price_data):
        ema = calculate_ema(price_data["close"], period=20)
        assert _is_valid_float(ema)

    @pytest.mark.parametrize("period", [5, 12, 26])
    def test_ema_periods(self, price_data, period):
        ema = calculate_ema(price_data["close"], period=period)
        assert _is_valid_float(ema)


# ══════════════════════════════════════════════════════════════
# Momentum
# ══════════════════════════════════════════════════════════════

class TestMomentum:

    def test_rsi_range(self, price_data):
        rsi = calculate_rsi(price_data["close"], period=14)
        assert _is_valid_float(rsi)
        assert 0 <= float(rsi) <= 100

    def test_rsi_constant_prices(self, constant_prices):
        rsi = calculate_rsi(constant_prices, period=14)
        if rsi is not None and _is_valid_float(rsi):
            assert 40 <= float(rsi) <= 60 or float(rsi) in (50.0, 100.0)

    def test_rsi_uptrend_is_high(self):
        uptrend = pd.Series(np.linspace(50, 120, 50))
        rsi = calculate_rsi(uptrend, period=14)
        if _is_valid_float(rsi):
            assert float(rsi) > 60

    def test_rsi_downtrend_is_low(self):
        downtrend = pd.Series(np.linspace(120, 50, 50))
        rsi = calculate_rsi(downtrend, period=14)
        if _is_valid_float(rsi):
            assert float(rsi) < 40

    @pytest.mark.parametrize("period", [7, 14, 21])
    def test_rsi_periods(self, price_data, period):
        rsi = calculate_rsi(price_data["close"], period=period)
        if rsi is not None:
            assert 0 <= float(rsi) <= 100


# ══════════════════════════════════════════════════════════════
# Trend
# ══════════════════════════════════════════════════════════════

class TestTrend:

    def test_macd_returns_three_values(self, price_data):
        result = calculate_macd(price_data["close"])
        assert len(result) == 3

    def test_macd_basic(self, price_data):
        macd, signal, hist = calculate_macd(price_data["close"])
        assert _is_valid_float(macd)
        assert _is_valid_float(signal)
        assert _is_valid_float(hist)

    def test_macd_histogram_equals_diff(self, price_data):
        macd, signal, hist = calculate_macd(price_data["close"])
        if all(_is_valid_float(x) for x in [macd, signal, hist]):
            expected = float(macd) - float(signal)
            assert abs(float(hist) - expected) < 1e-6

    @pytest.mark.parametrize("fast,slow,sig", [(12, 26, 9), (8, 17, 9), (5, 13, 5)])
    def test_macd_params(self, price_data, fast, slow, sig):
        macd, signal, hist = calculate_macd(
            price_data["close"], fast=fast, slow=slow, sig=sig
        )
        assert _is_valid_float(macd)

    def test_adx_range(self, price_data):
        adx, plus_di, minus_di = calculate_adx(
            price_data["high"], price_data["low"], price_data["close"]
        )
        assert _is_valid_float(adx)
        assert 0 <= float(adx) <= 100

    def test_adx_strong_trend(self):
        n = 60
        c = pd.Series(np.linspace(100, 160, n))
        h = c + 1.5
        lo = c - 1.5
        adx, _, _ = calculate_adx(h, lo, c)
        if _is_valid_float(adx):
            assert float(adx) > 20

    def test_stoch_basic(self, price_data):
        k, d = calculate_stoch(
            price_data["high"], price_data["low"], price_data["close"]
        )
        assert _is_valid_float(k)
        assert 0 <= float(k) <= 100


# ══════════════════════════════════════════════════════════════
# Volatility
# ══════════════════════════════════════════════════════════════

class TestVolatility:

    def test_bollinger_returns_three_values(self, price_data):
        result = calculate_bollinger(price_data["close"])
        assert len(result) == 3

    def test_bollinger_ordering(self, price_data):
        upper, mid, lower = calculate_bollinger(price_data["close"])
        assert all(_is_valid_float(x) for x in [upper, mid, lower])
        assert float(upper) > float(mid) > float(lower)

    def test_bollinger_constant_prices(self, constant_prices):
        upper, mid, lower = calculate_bollinger(constant_prices)
        if all(_is_valid_float(x) for x in [upper, mid, lower]):
            assert abs(float(upper) - float(lower)) < 1.0

    @pytest.mark.parametrize("period,nbdev", [(10, 1.5), (20, 2.0), (20, 2.5)])
    def test_bollinger_params(self, price_data, period, nbdev):
        upper, mid, lower = calculate_bollinger(
            price_data["close"], period=period, nbdev=nbdev
        )
        assert _is_valid_float(upper)

    def test_atr_basic(self, price_data):
        atr, atr_pct = calculate_atr(
            price_data["high"], price_data["low"], price_data["close"]
        )
        assert _is_valid_float(atr)
        assert float(atr) >= 0

    def test_atr_pct_reasonable(self, price_data):
        _, atr_pct = calculate_atr(
            price_data["high"], price_data["low"], price_data["close"]
        )
        if _is_valid_float(atr_pct):
            assert 0 <= float(atr_pct) <= 50


# ══════════════════════════════════════════════════════════════
# Advanced
# ══════════════════════════════════════════════════════════════

class TestAdvanced:

    def test_divergence_valid_output(self, price_data):
        rsi_vals = []
        closes = price_data["close"]
        for i in range(20, len(closes) + 1):
            r = calculate_rsi(closes.iloc[:i], 14)
            rsi_vals.append(r if r is not None else np.nan)

        rsi_series = pd.Series(rsi_vals, index=closes.index[-len(rsi_vals):])
        prices_aligned = closes.iloc[-len(rsi_vals):]

        result = detect_divergence(prices_aligned, rsi_series)
        assert result in ("bullish", "bearish", "none")

    def test_divergence_empty_input(self):
        result = detect_divergence(pd.Series(dtype=float), pd.Series(dtype=float))
        assert result in ("none", None)

    def test_support_resistance_structure(self, price_data):
        sr = find_support_resistance(
            price_data["high"], price_data["low"], price_data["close"]
        )
        assert isinstance(sr, dict)
        assert "support" in sr
        assert "resistance" in sr
        assert isinstance(sr["support"], list)
        assert isinstance(sr["resistance"], list)


# ══════════════════════════════════════════════════════════════
# Batch Calculations
# ══════════════════════════════════════════════════════════════

class TestBatch:

    def test_all_indicators_returns_dict(self, price_data):
        result = calculate_all_indicators(
            price_data["high"],
            price_data["low"],
            price_data["close"],
            price_data["volume"],
        )
        assert isinstance(result, dict)

    def test_all_indicators_has_minimum_keys(self, price_data):
        result = calculate_all_indicators(
            price_data["high"],
            price_data["low"],
            price_data["close"],
            price_data["volume"],
        )
        assert len(result) >= _MIN_INDICATORS

    def test_all_indicators_key_presence(self, price_data):
        result = calculate_all_indicators(
            price_data["high"],
            price_data["low"],
            price_data["close"],
            price_data["volume"],
        )
        for key in ("rsi", "macd", "adx"):
            assert key in result

    def test_all_indicators_rsi_valid(self, price_data):
        result = calculate_all_indicators(
            price_data["high"],
            price_data["low"],
            price_data["close"],
            price_data["volume"],
        )
        rsi = result.get("rsi")
        if rsi is not None:
            assert 0 <= float(rsi) <= 100

    def test_indicators_df_returns_dataframe(self, ohlcv_df):
        result = calculate_indicators_df(ohlcv_df)
        assert isinstance(result, pd.DataFrame)

    def test_indicators_df_required_columns(self, ohlcv_df):
        result = calculate_indicators_df(ohlcv_df)
        for col in ("RSI", "MACD"):
            assert col in result.columns

    def test_indicators_df_preserves_length(self, ohlcv_df):
        result = calculate_indicators_df(ohlcv_df)
        assert len(result) == len(ohlcv_df)


# ══════════════════════════════════════════════════════════════
# Edge Cases
# ══════════════════════════════════════════════════════════════

class TestEdgeCases:

    def test_sma_empty_array(self):
        result = calculate_sma(pd.Series(dtype=float), period=20)
        assert result is None or (isinstance(result, float) and np.isnan(result))

    def test_rsi_single_value(self):
        result = calculate_rsi(pd.Series([100.0]), period=14)
        assert result is None or (isinstance(result, float) and np.isnan(result))

    def test_rsi_with_nan_values(self, nan_prices):
        try:
            result = calculate_rsi(nan_prices, period=14)
            if result is not None:
                assert np.isnan(result) or (0 <= float(result) <= 100)
        except Exception as exc:
            pytest.fail(f"calculate_rsi with NaN raised {type(exc).__name__}: {exc}")

    def test_bollinger_single_value(self):
        try:
            result = calculate_bollinger(pd.Series([100.0]))
            assert len(result) == 3
        except Exception as exc:
            pytest.fail(f"calculate_bollinger raised {type(exc).__name__}: {exc}")

    def test_macd_insufficient_data(self):
        small = pd.Series([100.0, 101.0, 102.0])
        try:
            result = calculate_macd(small)
            assert len(result) == 3
        except Exception as exc:
            pytest.fail(f"calculate_macd raised {type(exc).__name__}: {exc}")

    def test_all_indicators_empty(self):
        empty = pd.Series(dtype=float)
        try:
            result = calculate_all_indicators(empty, empty, empty, empty)
            assert isinstance(result, dict)
        except Exception as exc:
            pytest.fail(f"calculate_all_indicators raised {type(exc).__name__}: {exc}")