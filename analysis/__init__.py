# analysis/__init__.py
"""
Technical Analysis & Risk Management Module
Optimized for Free/Premium Data Integration

v3.0: exports expanded to cover all public indicators (VWAP, Ichimoku, CCI,
Williams %R, Momentum, Historical Volatility, Trend score).
"""
from .indicators import (
    TA_LIB_AVAILABLE,
    INDICATOR_CONFIG,
    validate_config,
    # Moving averages
    calculate_sma,
    calculate_ema,
    # Momentum
    calculate_rsi,
    calculate_momentum,
    calculate_williams_r,
    # Trend
    calculate_macd,
    calculate_adx,
    calculate_stoch,
    calculate_cci,
    # Volatility / bands
    calculate_bollinger,
    calculate_atr,
    calculate_historical_volatility,
    # Volume / composite
    calculate_vwap,
    calculate_ichimoku,
    # Pattern detection
    detect_divergence,
    find_support_resistance,
    # Batch / bridges
    calculate_indicators_df,
    calculate_indicators_df_multi_tf,
    calculate_all_indicators,
    export_to_signal_format,
    compute_trend_indicators,
    # Streaming
    RollingIndicatorEngine,
    # Benchmark
    benchmark_indicators,
)

from .signals import (
    CONFIG as SIGNALS_CONFIG,
    is_valid_number,
    update_config,
    validate_config as validate_signals_config,
    advanced_signal_strategy,
    basic_signal_strategy,
    momentum_strategy,
    mean_reversion_strategy,
    volume_profile_strategy,
    trend_pullback_strategy,
    breakout_strategy,
    composite_strategy,
    apply_strategy_to_df,
    rank_long_candidates,
    generate_signals,
    combine_timeframe_signals,
    get_strategy,
    strategy_registry,
    StrategyRegistry,
    PositionSizer,
    MarketRegime,
    MarketRegimeFilter,
    SignalExplainer,
    BacktestMetrics,
)

from .risk import (
    CONFIG as RISK_CONFIG,
    assess_risk,
    assess_risk_batch,
    assess_risk_details,
    compute_risk_score,
    is_valid_number as risk_is_valid_number,
    # New public helpers (v4.3)
    RiskCache,
    RiskExplainer,
    RiskComparator,
    StressTest,
    DynamicThresholds,
    RiskTimeSeries,
    EngineResult,
    FactorSpec,
    RiskSummary,
)

from .indicators_integration import (
    enrich_market_data,
    enrich_market_data_async,
    get_trading_decision,
    get_trading_decision_batch,
    required_warmup,
    EnrichmentResult,
    PositionContext,
    ConfidenceBreakdown,
    DecisionMatrix,
    DEFAULT_MATRIX,
    EnrichmentMetrics,
    METRICS,
    Action,
    RiskLevel,
    ColumnMapper,
    ValidationError,
    validate_dataframe,
    validate_market_data,
    apply_signals,
    apply_risk,
    add_momentum_columns,
    get_signal_info,
    get_risk_info,
    decide_action,
    build_decision_reasons,
    extract_indicators,
    create_error_decision,
)

__all__ = [
    # ── indicators.py ────────────────────────────────────────
    "TA_LIB_AVAILABLE",
    "INDICATOR_CONFIG",
    "validate_config",

    "calculate_sma",
    "calculate_ema",
    "calculate_rsi",
    "calculate_momentum",
    "calculate_williams_r",
    "calculate_macd",
    "calculate_adx",
    "calculate_stoch",
    "calculate_cci",
    "calculate_bollinger",
    "calculate_atr",
    "calculate_historical_volatility",
    "calculate_vwap",
    "calculate_ichimoku",

    "detect_divergence",
    "find_support_resistance",

    "calculate_indicators_df",
    "calculate_indicators_df_multi_tf",
    "calculate_all_indicators",
    "export_to_signal_format",
    "compute_trend_indicators",

    "RollingIndicatorEngine",
    "benchmark_indicators",

    # ── signals.py ──────────────────────────────────────────
    "SIGNALS_CONFIG",
    "is_valid_number",
    "update_config",
    "validate_signals_config",

    "basic_signal_strategy",
    "advanced_signal_strategy",
    "momentum_strategy",
    "mean_reversion_strategy",
    "volume_profile_strategy",
    "trend_pullback_strategy",
    "breakout_strategy",
    "composite_strategy",

    "apply_strategy_to_df",
    "rank_long_candidates",
    "generate_signals",
    "combine_timeframe_signals",
    "get_strategy",
    "strategy_registry",
    "StrategyRegistry",

    "PositionSizer",
    "MarketRegime",
    "MarketRegimeFilter",
    "SignalExplainer",
    "BacktestMetrics",

    # ── risk.py ─────────────────────────────────────────────
    "RISK_CONFIG",
    "assess_risk",
    "assess_risk_batch",
    "assess_risk_details",
    "compute_risk_score",
    "risk_is_valid_number",

    "RiskCache",
    "RiskExplainer",
    "RiskComparator",
    "StressTest",
    "DynamicThresholds",
    "RiskTimeSeries",
    "EngineResult",
    "FactorSpec",
    "RiskSummary",

    # ── indicators_integration.py ──────────────────────────
    "enrich_market_data",
    "enrich_market_data_async",
    "get_trading_decision",
    "get_trading_decision_batch",
    "required_warmup",

    "EnrichmentResult",
    "PositionContext",
    "ConfidenceBreakdown",
    "DecisionMatrix",
    "DEFAULT_MATRIX",
    "EnrichmentMetrics",
    "METRICS",

    "Action",
    "RiskLevel",
    "ColumnMapper",
    "ValidationError",
    "validate_dataframe",
    "validate_market_data",
    "apply_signals",
    "apply_risk",
    "add_momentum_columns",
    "get_signal_info",
    "get_risk_info",
    "decide_action",
    "build_decision_reasons",
    "extract_indicators",
    "create_error_decision",
]