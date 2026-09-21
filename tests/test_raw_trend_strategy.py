"""Acceptance tests for the CryptoScanner 6.1 raw-scan strategy."""
from __future__ import annotations

from trading.nobitex_momentum_engine import NobitexMomentumEngine
from trading.scan_history import ScanHistoryStore
from signal_tracker import SignalTracker


def market_row(price: float, *, symbol: str = "ABC", change_24h: float = 2.0) -> dict:
    return {
        "Symbol": symbol,
        "Pair": f"{symbol}IRT",
        "AssetKey": f"nobitex:{symbol.lower()}",
        "Price": price,
        "Bid": price * 0.999,
        "Ask": price,
        "Volume": 1_000_000_000.0,
        "24h Change (%)": change_24h,
        "asks": [[price, 100_000_000.0]],
    }


def strict_engine(**overrides) -> NobitexMomentumEngine:
    settings = {
        "raw_scan_trend_enabled": True,
        "threshold_percent": 3.0,
        "min_consecutive_positive_scans": 3,
        "trend_lookback_scans": 6,
        "min_volume_irt": 1.0,
        "max_spread_pct": 2.0,
        "min_ask_depth_quote": 0.0,
    }
    settings.update(overrides)
    return NobitexMomentumEngine(**settings)


def confirmed_candidate(engine: NobitexMomentumEngine, prices=(100, 101, 102, 103, 104, 105)) -> dict:
    candidates = []
    for timestamp, price in enumerate(prices, start=1):
        candidates = engine.evaluate(
            [market_row(float(price))], now=float(timestamp), require_depth=False
        )
    assert candidates
    return candidates[0]


def test_scan_history_is_bounded_and_persistent(tmp_path):
    path = tmp_path / "scan_history.json"
    store = ScanHistoryStore(str(path), max_scans=6)
    for price in range(100, 110):
        assert store.record("ABC", price, timestamp=float(price))

    restored = ScanHistoryStore(str(path), max_scans=6)
    assert restored.prices("ABC") == [104.0, 105.0, 106.0, 107.0, 108.0, 109.0]


def test_confirmed_entry_requires_all_raw_structure_rules():
    engine = strict_engine()
    candidate = confirmed_candidate(engine)

    assert candidate["Signal"] == "Trend Buy"
    assert candidate["TrendConfirmed"] is True
    assert candidate["ConsecutivePositiveScans"] >= 3
    assert candidate["HigherHighs"] is True
    assert candidate["HigherLows"] is True
    assert candidate["CurrentAbovePreviousMean"] is True
    assert candidate["PreviousMeanRising"] is True
    assert candidate["ObservedLocalMove (%)"] > 3.0


def test_negative_subthreshold_and_unconfirmed_entries_are_rejected():
    # Not enough completed scans: a single positive tick is never an entry.
    insufficient = strict_engine()
    assert not insufficient.evaluate([market_row(100.0)], now=1.0, require_depth=False)

    # The complete move is positive but below the configured 3% threshold.
    below_threshold = strict_engine()
    candidates = []
    for timestamp, price in enumerate((100, 100.1, 100.2, 100.3, 100.4, 100.5), start=1):
        candidates = below_threshold.evaluate(
            [market_row(float(price))], now=float(timestamp), require_depth=False
        )
    assert candidates == []
    assert "below_threshold" in below_threshold.last_assessments["ABC"].reason_codes

    # A negative path is rejected even if it has enough scans.
    negative = strict_engine()
    candidates = []
    for timestamp, price in enumerate((100, 99, 98, 97, 96, 95), start=1):
        candidates = negative.evaluate(
            [market_row(float(price), change_24h=-2.0)],
            now=float(timestamp),
            require_depth=False,
        )
    assert candidates == []
    assert negative.last_assessments["ABC"].is_negative


def configured_tracker(tmp_path, name: str) -> SignalTracker:
    tracker = SignalTracker(
        account_balance=1_000_000.0,
        db_filename=str(tmp_path / f"{name}.db"),
    )
    tracker.raw_trend_only = True
    tracker.threshold_percent = 3.0
    tracker.min_consecutive_positive_scans = 3
    tracker.trend_lookback_scans = 6
    tracker.ignore_signal_filters = True
    tracker.position_size_mode = "fixed"
    tracker.fixed_position_quote = 10_000.0
    tracker.min_notional_quote = 0.0
    tracker.max_open_trades = 2
    tracker.stop_loss_pct = 3.0
    tracker.trailing_stop_enabled = False
    tracker.quiet_skips = False
    return tracker


def test_stop_loss_exit_has_priority_over_raw_trend_exit(tmp_path):
    candidate = confirmed_candidate(strict_engine())
    tracker = configured_tracker(tmp_path, "stop")
    assert tracker.process_new_signals([candidate])["opened"] == 1

    stop_tick = market_row(100.0)
    stop_tick["Signal"] = "Neutral"
    result = tracker.process_new_signals([stop_tick])

    assert result["closed"] == 1
    closed = tracker.get_all_trades()[0]
    assert closed["exit_reason"] == "Stop Loss"


def test_trend_break_exit_closes_before_a_stop_is_reached(tmp_path):
    candidate = confirmed_candidate(strict_engine())
    tracker = configured_tracker(tmp_path, "trend_break")
    assert tracker.process_new_signals([candidate])["opened"] == 1

    # Paper mode may include a small configured half-spread, so use a
    # price safely above the resulting stop.  It is still below the
    # previous-scan mean and therefore breaks the raw trend.
    break_tick = market_row(102.5)
    break_tick["Signal"] = "Neutral"
    result = tracker.process_new_signals([break_tick])

    assert result["closed"] == 1
    closed = tracker.get_all_trades()[0]
    assert closed["exit_reason"] == "Trend Break"
