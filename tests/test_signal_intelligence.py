from analysis.market_intelligence import calculate_signal_intelligence_score


def test_signal_intelligence_score_is_transparent_and_bounded():
    row = {"Signal": "6.00% Pump"}
    snapshot = {
        "momentum_1m_pct": 0.8,
        "momentum_5m_pct": 1.5,
        "momentum_15m_pct": 3.0,
        "buy_pressure_pct": 72.0,
        "orderbook_imbalance_pct": 24.0,
        "volume_ratio": 2.2,
        "ema_trend": "Bullish",
        "macd": 2.0,
        "macd_signal": 1.5,
        "macd_hist": 0.5,
        "rsi": 61.0,
        "adx": 28.0,
    }
    result = calculate_signal_intelligence_score(row, snapshot)

    assert 0.0 <= result["score"] <= 100.0
    assert result["coverage_pct"] == 100.0
    assert result["grade"] == "Strong alignment"
    assert set(result["factors"]) == {
        "Pump", "Momentum", "Buy Pressure", "Order Book", "Volume",
        "EMA", "MACD", "RSI", "ADX",
    }


def test_signal_intelligence_score_does_not_treat_missing_data_as_zero():
    result = calculate_signal_intelligence_score(
        {"Signal": "3.50% Pump"},
        {"buy_pressure_pct": 65.0},
    )

    assert result["score"] is not None
    assert result["coverage_pct"] == 35.0
    assert result["factors"]["Pump"] > 0
    assert result["factors"]["Buy Pressure"] == 65.0


def test_signal_intelligence_score_has_no_execution_side_effects():
    row = {"Signal": "8.00% Pump"}
    snapshot = {"buy_pressure_pct": 80.0}
    before = dict(snapshot)
    result = calculate_signal_intelligence_score(row, snapshot)

    assert snapshot == before
    assert "score" in result
