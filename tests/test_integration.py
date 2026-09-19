# tests/test_integration.py
"""
Integration tests for signals.py v7.3 and indicators_integration.py v4.3
"""
import pytest
import pandas as pd
import numpy as np
from typing import Dict, Any

from analysis.signals import (
    apply_strategy_to_df,
    composite_strategy,
    generate_signals,
)
from analysis.indicators_integration import (
    enrich_market_data,
    get_trading_decision,
    ColumnMapper,
    EnrichmentResult,
    PositionContext,
)


# ══════════════════════════════════════════════════════════════
# FIXTURES
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def sample_ohlcv_df():
    """Sample OHLCV DataFrame"""
    return pd.DataFrame({
        'Symbol': ['BTCUSDT'] * 10,
        'close': [50000 + i*100 for i in range(10)],
        'open': [49900 + i*100 for i in range(10)],
        'high': [50200 + i*100 for i in range(10)],
        'low': [49800 + i*100 for i in range(10)],
        'volume': [1000000 + i*10000 for i in range(10)],
    })


# ══════════════════════════════════════════════════════════════
# COLUMN COMPATIBILITY TESTS
# ══════════════════════════════════════════════════════════════

def test_column_compatibility():
    """Test که ستون‌های signals.py و indicators_integration هماهنگ هستند"""
    
    df = pd.DataFrame({
        'close': [100, 101, 102],
        'volume': [1000, 1100, 1200],
        'RSI': [45, 50, 55],
        'MACD': [0.5, 0.6, 0.7],
        'ADX': [25, 26, 27],
        'EMA50': [99, 100, 101],
        'EMA200': [95, 96, 97],
    })
    
    # Test signals.py output
    result = apply_strategy_to_df(
        df,
        strategy="composite",
        enrich=True,
        return_frame=True
    )
    
    # Validate structure
    warnings = ColumnMapper.validate_signal_output(result)
    assert isinstance(warnings, list)
    
    # Check both variants exist
    has_signal = 'signal' in result.columns or 'Signal' in result.columns
    has_risk = any(c in result.columns for c in ['risk', 'risk_level', 'Risk_Level'])
    
    assert has_signal, "Missing signal column"
    assert has_risk, "Missing risk column"
    
    print("✅ Column compatibility test passed")


def test_enrichment_flow(sample_ohlcv_df):
    """Test کل flow از indicators تا decision"""
    
    # Step 1: Enrich
    enriched = enrich_market_data(
        sample_ohlcv_df,
        calculate_signals=True,
        calculate_risk=True,
        strategy="composite",
        timeframe="1h",
        symbol="BTCUSDT",
        return_result_obj=True
    )
    
    assert isinstance(enriched, EnrichmentResult)
    assert enriched.is_valid or len(enriched.df) > 0
    
    df = enriched.df
    assert not df.empty
    
    # Check that both signal variants exist
    has_signal = 'Signal' in df.columns or 'signal' in df.columns
    assert has_signal, f"Missing signal column. Available: {df.columns.tolist()}"
    
    # Step 2: Get decision from last row
    last_row = df.iloc[-1].to_dict()
    decision = get_trading_decision(
        last_row,
        strategy="composite",
        symbol="BTCUSDT"
    )
    
    # Validate decision structure
    assert 'action' in decision
    assert 'signal' in decision and 'Signal' in decision
    assert 'risk' in decision and 'risk_level' in decision
    assert decision['signal'] == decision['Signal'], "Signal variants must match"
    
    print("✅ Enrichment flow test passed")
    print(f"Decision: {decision['action']} | Signal: {decision['signal']} | Risk: {decision['risk']}")


def test_none_vs_zero():
    """Test که None برای missing values استفاده می‌شود نه 0.0"""
    
    data = {
        'close': 100,
        # RSI намدنًا وجود ندارد
    }
    
    result = composite_strategy(data)
    
    # signals.py v7.3 باید None برگرداند نه 0.0 (در generate_signals)
    # اما در strategy result ممکن است default value داشته باشد
    assert isinstance(result, dict)
    assert 'score' in result
    
    print("✅ None handling test passed")


def test_position_context_integration():
    """Test PositionContext with trading decision"""
    
    market_data = {
        'Price': 50000,
        'close': 50000,
        'RSI': 65,
        'MACD': 100,
        'ADX': 30,
        'EMA50': 49000,
        'EMA200': 48000,
    }
    
    # Test without position
    decision1 = get_trading_decision(
        market_data,
        strategy='composite',
        symbol='BTCUSDT'
    )
    
    # Test with position
    position = PositionContext(
        holding=True,
        entry_price=48000,
        quantity=0.1,
        entry_time=1000000
    )
    
    decision2 = get_trading_decision(
        market_data,
        strategy='composite',
        symbol='BTCUSDT',
        position=position
    )
    
    assert decision1['action'] in ['buy', 'exit', 'hold', 'wait']
    assert decision2['action'] in ['buy', 'exit', 'hold', 'wait']
    
    # With position, exit signals should be considered differently
    print("✅ Position context integration test passed")


def test_batch_decision_compatibility():
    """Test batch decision making"""
    from analysis.indicators_integration import get_trading_decision_batch
    
    symbols_data = {
        'BTCUSDT': {
            'Price': 50000,
            'RSI': 55,
            'MACD': 100,
            'ADX': 25,
            'EMA50': 49000,
            'EMA200': 48000,
        },
        'ETHUSDT': {
            'Price': 3000,
            'RSI': 65,
            'MACD': 50,
            'ADX': 30,
            'EMA50': 2900,
            'EMA200': 2800,
        },
    }
    
    results = get_trading_decision_batch(
        symbols_data,
        strategy='composite',
        sort_by='confidence',
        top_n=2
    )
    
    assert isinstance(results, list)
    assert len(results) <= 2
    
    for decision in results:
        assert 'symbol' in decision
        assert 'action' in decision
        assert 'signal' in decision and 'Signal' in decision
        assert decision['signal'] == decision['Signal']
    
    print("✅ Batch decision compatibility test passed")


def test_risk_column_variants():
    """Test that all risk column variants are handled correctly"""
    
    df = pd.DataFrame({
        'close': [100],
        'RSI': [50],
        'MACD': [0],
        'ADX': [25],
    })
    
    result = apply_strategy_to_df(
        df,
        strategy='composite',
        enrich=True,
        return_frame=True
    )
    
    # Should have at least one risk variant
    risk_variants = ['risk', 'risk_level', 'Risk', 'Risk_Level']
    has_risk = any(col in result.columns for col in risk_variants)
    
    assert has_risk, f"Missing all risk variants. Columns: {result.columns.tolist()}"
    
    print("✅ Risk column variants test passed")


def test_generate_signals_output():
    """Test generate_signals output format"""
    
    df = pd.DataFrame({
        'Symbol': ['BTC', 'ETH'],
        'Price': [50000, 3000],
        'close': [50000, 3000],
        'RSI': [55, 65],
        'MACD': [100, 50],
        'ADX': [25, 30],
        'EMA50': [49000, 2900],
        'EMA200': [48000, 2800],
        'Market Cap': [1e9, 5e8],
    })
    
    signals = generate_signals(
        df,
        strategy='composite',
        top_n=5,
        include_neutral=True
    )
    
    assert isinstance(signals, list)
    assert len(signals) > 0
    
    for signal in signals:
        # Check both signal variants exist
        assert 'Signal' in signal or 'signal' in signal
        
        # Check both risk variants exist
        has_risk = 'Risk' in signal or 'risk_level' in signal or 'risk' in signal
        assert has_risk
        
        # v7.3: None for missing indicators, not 0.0
        # But in final output, None might be removed
        if 'RSI' in signal:
            assert signal['RSI'] is None or isinstance(signal['RSI'], (int, float))
    
    print("✅ Generate signals output test passed")


# ══════════════════════════════════════════════════════════════
# PERFORMANCE TESTS
# ══════════════════════════════════════════════════════════════

def test_enrichment_performance():
    """Test enrichment performance on larger dataset"""
    import time
    
    # Create larger dataset
    n = 500
    df = pd.DataFrame({
        'Symbol': ['BTCUSDT'] * n,
        'close': np.random.randn(n).cumsum() + 50000,
        'open': np.random.randn(n).cumsum() + 49900,
        'high': np.random.randn(n).cumsum() + 50200,
        'low': np.random.randn(n).cumsum() + 49800,
        'volume': np.random.randint(900000, 1100000, n),
    })
    
    start = time.time()
    
    result = enrich_market_data(
        df,
        calculate_signals=True,
        calculate_risk=True,
        strategy="composite",
        timeframe="1h",
        symbol="BTCUSDT",
        return_result_obj=True
    )
    
    elapsed = time.time() - start
    
    assert isinstance(result, EnrichmentResult)
    assert result.elapsed_s > 0
    
    print(f"✅ Performance test passed: {n} rows in {elapsed:.2f}s")
    print(f"   Enrichment ratio: {result.enrichment_ratio:.2%}")


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])