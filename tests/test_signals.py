# tests/test_signals.py
"""
Test suite for signals module v7.3
"""
import pytest
import pandas as pd
import numpy as np
from typing import Dict, Any

from analysis.signals import (
    # Core functions
    composite_strategy,
    advanced_signal_strategy,
    basic_signal_strategy,
    momentum_strategy,
    mean_reversion_strategy,
    volume_profile_strategy,
    trend_pullback_strategy,
    breakout_strategy,
    
    # DataFrame functions
    apply_strategy_to_df,
    rank_long_candidates,
    generate_signals,
    combine_timeframe_signals,
    
    # Classes
    PositionSizer,
    MarketRegimeFilter,
    SignalExplainer,
    BacktestMetrics,
    StrategyRegistry,
    
    # Registry
    strategy_registry,
    get_strategy,
    
    # Config
    CONFIG,
    update_config,
    validate_config,
    
    # Utilities
    is_valid_number,
    
    # REMOVED: _sigmoid_confidence - این تابع در v7.3 وجود ندارد
)


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def sample_market_data() -> Dict[str, Any]:
    """Sample market data for testing"""
    return {
        'Price': 50000.0,
        'close': 50000.0,
        'open': 49500.0,
        'high': 51000.0,
        'low': 49000.0,
        'volume': 1000000.0,
        'RSI': 55.0,
        'MACD': 100.0,
        'MACD Signal': 95.0,
        'MACD_Histogram': 5.0,
        'ADX': 25.0,
        '+DI': 22.0,
        '-DI': 18.0,
        'EMA50': 49000.0,
        'EMA200': 48000.0,
        'BB Upper': 52000.0,
        'BB Lower': 48000.0,
        'BB Width %': 8.0,
        'ATR %': 3.5,
        'Stochastic_K': 60.0,
        'Stochastic_D': 55.0,
        'Relative_Volume': 1.2,
        '24h Change (%)': 2.5,
        '1h Change (%)': 0.5,
        'Market Cap': 1000000000.0,
        'Spread %': 0.1,
        'Turnover': 0.8,
    }


@pytest.fixture
def sample_dataframe() -> pd.DataFrame:
    """Sample DataFrame for batch testing"""
    return pd.DataFrame({
        'Symbol': ['BTC', 'ETH', 'BNB'],
        'Price': [50000, 3000, 400],
        'close': [50000, 3000, 400],
        'volume': [1000000, 500000, 200000],
        'RSI': [55, 65, 45],
        'MACD': [100, 50, -20],
        'MACD Signal': [95, 55, -15],
        'ADX': [25, 30, 20],
        '+DI': [22, 25, 15],
        '-DI': [18, 15, 20],
        'EMA50': [49000, 2900, 390],
        'EMA200': [48000, 2800, 380],
        'BB Upper': [52000, 3200, 420],
        'BB Lower': [48000, 2800, 380],
        'ATR %': [3.5, 4.0, 5.0],
        'Relative_Volume': [1.2, 1.5, 0.8],
        '24h Change (%)': [2.5, 3.0, -1.0],
        'Market Cap': [1e9, 5e8, 1e8],
    })


# ══════════════════════════════════════════════════════════════
# BASIC TESTS
# ══════════════════════════════════════════════════════════════

def test_is_valid_number():
    """Test number validation utility"""
    assert is_valid_number(42.0) == True
    assert is_valid_number(0) == True
    assert is_valid_number(-10.5) == True
    assert is_valid_number(None) == False
    assert is_valid_number(float('nan')) == False
    assert is_valid_number(float('inf')) == False
    assert is_valid_number("invalid") == False
    assert is_valid_number(True) == False  # booleans should be rejected


def test_config_validation():
    """Test configuration validation"""
    errors = validate_config(CONFIG)
    assert isinstance(errors, list)
    # Should have no errors with default config
    assert len(errors) == 0, f"Default config has errors: {errors}"


def test_config_update():
    """Test configuration update"""
    original_value = CONFIG.get("strong_buy_threshold")
    
    # Update config
    new_config = {"strong_buy_threshold": 45.0}
    errors = update_config(new_config)
    
    assert len(errors) == 0
    assert CONFIG["strong_buy_threshold"] == 45.0
    
    # Restore original
    update_config({"strong_buy_threshold": original_value})


# ══════════════════════════════════════════════════════════════
# STRATEGY TESTS
# ══════════════════════════════════════════════════════════════

def test_basic_strategy(sample_market_data):
    """Test basic signal strategy"""
    result = basic_signal_strategy(sample_market_data)
    
    assert isinstance(result, dict)
    assert 'signal' in result or 'Signal' in result
    assert 'score' in result
    assert 'confidence' in result
    assert 'risk' in result or 'risk_level' in result
    assert 'quality' in result
    assert 'tradable_long' in result
    
    # Validate value ranges
    assert -100 <= result['score'] <= 100
    assert 0 <= result['confidence'] <= 100
    assert 0 <= result['quality'] <= 1


def test_advanced_strategy(sample_market_data):
    """Test advanced signal strategy"""
    result = advanced_signal_strategy(sample_market_data)
    
    assert isinstance(result, dict)
    assert result['signal'] in ('Strong Buy', 'Buy Signal', 'Neutral', 
                                  'Sell Signal', 'Strong Sell')
    assert 'components' in result
    assert isinstance(result['components'], dict)


def test_composite_strategy(sample_market_data):
    """Test composite strategy"""
    result = composite_strategy(sample_market_data)
    
    assert isinstance(result, dict)
    assert 'signal' in result or 'Signal' in result
    assert 'score' in result
    
    # Composite should have reasons mentioning ensemble
    reasons = result.get('reasons', result.get('Reasons', ''))
    if isinstance(reasons, str):
        assert 'Ensemble' in reasons or 'ensemble' in reasons.lower()


def test_all_strategies(sample_market_data):
    """Test all registered strategies"""
    strategies = strategy_registry.list_all()
    
    for strategy_name in strategies:
        strategy_fn = strategy_registry.get(strategy_name)
        assert strategy_fn is not None, f"Strategy {strategy_name} not found"
        
        result = strategy_fn(sample_market_data)
        assert isinstance(result, dict)
        assert 'signal' in result or 'Signal' in result
        assert 'score' in result


# ══════════════════════════════════════════════════════════════
# DATAFRAME TESTS
# ══════════════════════════════════════════════════════════════

def test_apply_strategy_to_df(sample_dataframe):
    """Test strategy application to DataFrame"""
    result = apply_strategy_to_df(
        sample_dataframe,
        strategy='composite',
        enrich=True,
        return_frame=True
    )
    
    assert isinstance(result, pd.DataFrame)
    assert len(result) == len(sample_dataframe)
    assert 'signal' in result.columns or 'Signal' in result.columns
    assert 'score' in result.columns
    assert 'confidence' in result.columns


def test_apply_strategy_enrichment(sample_dataframe):
    """Test that enrichment adds columns to original DataFrame"""
    df = sample_dataframe.copy()
    original_cols = set(df.columns)
    
    apply_strategy_to_df(df, strategy='advanced', enrich=True, return_frame=False)
    
    new_cols = set(df.columns) - original_cols
    assert len(new_cols) > 0  # Should have added columns
    assert 'Signal' in df.columns or 'signal' in df.columns


def test_rank_long_candidates(sample_dataframe):
    """Test ranking of long candidates"""
    ranked = rank_long_candidates(
        sample_dataframe,
        strategy='composite',
        top_n=2
    )
    
    assert isinstance(ranked, pd.DataFrame)
    assert len(ranked) <= 2
    
    if not ranked.empty:
        assert 'tradable_long' in ranked.columns
        assert all(ranked['tradable_long'])  # All should be tradable
        
        # Should be sorted by long_score
        if 'long_score' in ranked.columns and len(ranked) > 1:
            scores = ranked['long_score'].values
            assert all(scores[i] >= scores[i+1] for i in range(len(scores)-1))


def test_generate_signals(sample_dataframe):
    """Test signal generation"""
    signals = generate_signals(
        sample_dataframe,
        strategy='composite',
        top_n=5,
        include_neutral=True
    )
    
    assert isinstance(signals, list)
    assert len(signals) > 0
    
    for signal in signals:
        assert isinstance(signal, dict)
        assert 'Symbol' in signal
        assert 'Signal' in signal or 'signal' in signal
        assert 'Score' in signal or 'score' in signal


# ══════════════════════════════════════════════════════════════
# POSITION SIZER TESTS
# ══════════════════════════════════════════════════════════════

def test_position_sizer(sample_market_data):
    """Test position size calculation"""
    sizer = PositionSizer(portfolio_value=10000.0)
    
    # Get a signal first
    signal_result = composite_strategy(sample_market_data)
    
    position = sizer.calculate(
        row=sample_market_data,
        signal_result=signal_result,
        entry_price=50000.0
    )
    
    assert isinstance(position, dict)
    assert 'position_pct' in position
    assert 'position_value' in position
    assert 'stop_price' in position
    assert 'target_price' in position
    assert 'risk_value' in position
    
    # Position should be within limits
    assert 0 <= position['position_pct'] <= CONFIG['position_max_pct']
    assert position['stop_price'] < 50000.0  # Stop should be below entry


# ══════════════════════════════════════════════════════════════
# MARKET REGIME TESTS
# ══════════════════════════════════════════════════════════════

def test_market_regime_detection(sample_dataframe):
    """Test market regime detection"""
    filter_obj = MarketRegimeFilter()
    
    # Add scores to dataframe
    df_with_signals = apply_strategy_to_df(
        sample_dataframe,
        strategy='composite',
        enrich=True,
        return_frame=True
    )
    
    regime = filter_obj.detect(df_with_signals)
    
    assert regime.label in ('Bull', 'Bear', 'Neutral', 'Unknown')
    assert isinstance(regime.avg_score, float)
    assert isinstance(regime.confidence, float)
    assert 0 <= regime.confidence <= 1


def test_regime_filter_longs(sample_dataframe):
    """Test regime-based filtering"""
    filter_obj = MarketRegimeFilter()
    
    df_with_signals = apply_strategy_to_df(
        sample_dataframe,
        strategy='composite',
        enrich=True,
        return_frame=True
    )
    
    # Create a bearish regime
    from analysis.signals import MarketRegime
    bear_regime = MarketRegime(
        label='Bear',
        avg_score=-20.0,
        confidence=0.8,
        sample_size=len(df_with_signals)
    )
    
    filtered = filter_obj.filter_longs(df_with_signals, bear_regime)
    
    # In bear market, should be more selective
    assert len(filtered) <= len(df_with_signals)


# ══════════════════════════════════════════════════════════════
# SIGNAL EXPLAINER TESTS
# ══════════════════════════════════════════════════════════════

def test_signal_explainer(sample_market_data):
    """Test signal explanation"""
    explainer = SignalExplainer()
    result = composite_strategy(sample_market_data)
    
    explanation = explainer.explain(result, symbol='BTCUSDT', verbose=False)
    
    assert isinstance(explanation, str)
    assert len(explanation) > 0
    assert 'BTCUSDT' in explanation


def test_signal_comparison():
    """Test signal comparison"""
    explainer = SignalExplainer()
    
    results = [
        ('BTC', {'signal': 'Strong Buy', 'score': 50, 'confidence': 80, 
                 'risk': 'Low', 'quality': 0.9}),
        ('ETH', {'signal': 'Buy Signal', 'score': 30, 'confidence': 65, 
                 'risk': 'Medium', 'quality': 0.7}),
    ]
    
    comparison = explainer.compare(results)
    
    assert isinstance(comparison, str)
    assert 'BTC' in comparison
    assert 'ETH' in comparison


# ══════════════════════════════════════════════════════════════
# BACKTEST METRICS TESTS
# ══════════════════════════════════════════════════════════════

def test_backtest_metrics(sample_dataframe):
    """Test backtest metrics computation"""
    df_with_signals = apply_strategy_to_df(
        sample_dataframe,
        strategy='composite',
        enrich=True,
        return_frame=True
    )
    
    metrics = BacktestMetrics.compute(df_with_signals)
    
    assert isinstance(metrics.total_signals, int)
    assert metrics.total_signals > 0
    assert isinstance(metrics.avg_score, float)
    assert isinstance(metrics.avg_confidence, float)


def test_score_distribution(sample_dataframe):
    """Test score distribution analysis"""
    df_with_signals = apply_strategy_to_df(
        sample_dataframe,
        strategy='composite',
        enrich=True,
        return_frame=True
    )
    
    dist = BacktestMetrics.score_distribution(df_with_signals, bins=5)
    
    assert isinstance(dist, dict)
    assert len(dist) > 0


# ══════════════════════════════════════════════════════════════
# MULTI-TIMEFRAME TESTS
# ══════════════════════════════════════════════════════════════

def test_combine_timeframe_signals(sample_market_data):
    """Test multi-timeframe signal combination"""
    signals = {
        '1h': composite_strategy(sample_market_data),
        '4h': composite_strategy(sample_market_data),
        '1d': composite_strategy(sample_market_data),
    }
    
    combined = combine_timeframe_signals(signals)
    
    assert isinstance(combined, dict)
    assert 'signal' in combined or 'Signal' in combined
    assert 'score' in combined
    assert 'reasons' in combined or 'Reasons' in combined
    
    # Should mention MTF in reasons
    reasons = combined.get('reasons', combined.get('Reasons', ''))
    if isinstance(reasons, str):
        assert 'MTF' in reasons or '[1h]' in reasons


# ══════════════════════════════════════════════════════════════
# STRATEGY REGISTRY TESTS
# ══════════════════════════════════════════════════════════════

def test_strategy_registry():
    """Test strategy registry functionality"""
    registry = StrategyRegistry()
    
    # Test registration
    def custom_strategy(row):
        return {'signal': 'Neutral', 'score': 0.0}
    
    registry.register('custom', custom_strategy, alias='my_custom')
    
    assert 'custom' in registry
    assert registry.get('my_custom') is not None
    assert 'custom' in registry.list_all()


def test_get_strategy():
    """Test getting strategies by name"""
    assert get_strategy('composite') is not None
    assert get_strategy('advanced') is not None
    assert get_strategy('nonexistent') is None
    
    # Test alias
    assert get_strategy('basic') == get_strategy('basic_signal_strategy')


# ══════════════════════════════════════════════════════════════
# EDGE CASES
# ══════════════════════════════════════════════════════════════

def test_empty_dataframe():
    """Test handling of empty DataFrame"""
    empty_df = pd.DataFrame()
    
    result = apply_strategy_to_df(empty_df, strategy='composite')
    
    assert isinstance(result, (pd.DataFrame, pd.Series))
    # Should handle gracefully without error


def test_missing_data(sample_market_data):
    """Test handling of missing indicators"""
    incomplete_data = {
        'Price': 50000,
        'close': 50000,
        # Missing most indicators
    }
    
    result = composite_strategy(incomplete_data)
    
    assert isinstance(result, dict)
    assert result['signal'] == 'Neutral'  # Should default to neutral
    assert result.get('completeness', 1.0) < 1.0  # Should flag low completeness


def test_extreme_values(sample_market_data):
    """Test handling of extreme values"""
    extreme_data = sample_market_data.copy()
    extreme_data['RSI'] = 95  # Extremely overbought
    extreme_data['24h Change (%)'] = 50  # Extreme pump
    
    result = composite_strategy(extreme_data)
    
    assert isinstance(result, dict)
    # Should still produce valid output
    assert -100 <= result['score'] <= 100


def test_parallel_processing(sample_dataframe):
    """Test parallel processing mode"""
    # Create larger DataFrame for parallel test
    large_df = pd.concat([sample_dataframe] * 50, ignore_index=True)
    
    result = apply_strategy_to_df(
        large_df,
        strategy='composite',
        parallel=True,
        batch_size=50
    )
    
    assert isinstance(result, (pd.DataFrame, pd.Series))
    assert len(result) == len(large_df)


# ══════════════════════════════════════════════════════════════
# COMPATIBILITY TESTS (for indicators_integration.py)
# ══════════════════════════════════════════════════════════════

def test_signals_v7_3_compatibility(sample_market_data):
    """Test that v7.3 returns both lowercase and Title-Case columns"""
    result = composite_strategy(sample_market_data)
    
    # v7.3 should return both variants
    assert 'signal' in result
    assert 'Signal' in result
    assert result['signal'] == result['Signal']  # Must match
    
    assert 'risk' in result
    assert 'risk_level' in result


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])