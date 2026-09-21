"""Acceptance tests for the CryptoScanner raw-scan strategy.

The live contract is simple: any symbol whose observed move is strictly
above the user threshold may enter.  Secondary structure flags only
nudge ranking.  Every fill must carry a stop-loss, and the trailing
stop must ratchet once activated.
"""
from __future__ import annotations

from analysis.raw_trend import assess_trend
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
        "stop_loss_percent": 3.0,
        "trailing_stop_percent": 1.0,
        "min_volume_irt": 1.0,
        "max_spread_pct": 2.0,
        "min_ask_depth_quote": 0.0,
    }
    settings.update(overrides)
    return NobitexMomentumEngine(**settings)


def scan_until(engine: NobitexMomentumEngine, prices, *, symbol: str = "ABC") -> list:
    candidates = []
    for timestamp, price in enumerate(prices, start=1):
        candidates = engine.evaluate(
            [market_row(float(price), symbol=symbol)], now=float(timestamp), require_depth=False
        )
    return candidates


def confirmed_candidate(engine: NobitexMomentumEngine, prices=(100, 101, 102, 103, 104, 105)) -> dict:
    candidates = scan_until(engine, prices)
    assert candidates
    return candidates[0]


def test_scan_history_is_bounded_and_persistent(tmp_path):
    path = tmp_path / "scan_history.json"
    store = ScanHistoryStore(str(path), max_scans=6)
    for price in range(100, 110):
        assert store.record("ABC", price, timestamp=float(price))

    restored = ScanHistoryStore(str(path), max_scans=6)
    assert restored.prices("ABC") == [104.0, 105.0, 106.0, 107.0, 108.0, 109.0]


def test_threshold_is_the_hard_entry_gate():
    engine = strict_engine()
    candidate = confirmed_candidate(engine)

    assert candidate["Signal"] == "Trend Buy"
    assert candidate["TrendConfirmed"] is True
    assert candidate["ObservedLocalMove (%)"] > 3.0
    assert candidate["StopLossPrice"] == candidate["Ask"] * 0.97
    assert candidate["TrailingStopPercent"] == 1.0
    assert candidate["ConsecutivePositiveScans"] >= 3
    assert candidate["HigherHighs"] is True
    assert candidate["HigherLows"] is True


def test_above_threshold_enters_even_without_full_structure():
    # A single 4% jump over a flat window has almost no structure, but it
    # is still strictly above the user's 3% threshold so it must enter.
    engine = strict_engine()
    candidates = scan_until(engine, (100, 100, 100, 100, 100, 104))

    assert candidates
    row = candidates[0]
    assert row["Signal"] == "Trend Buy"
    assert row["ObservedLocalMove (%)"] > 3.0
    assert row["ConsecutivePositiveScans"] < 3
    assert row["StopLossPrice"] < row["Ask"]
    assert row["TrailingStopPercent"] == 1.0
    assessment = engine.last_assessments["ABC"]
    assert assessment.entry_allowed
    assert assessment.structure_score < 1.0
    assert "positive_streak_not_confirmed" in assessment.soft_reason_codes


def test_structure_only_tilts_ranking_between_threshold_qualified_symbols():
    engine = strict_engine()
    rows = []
    weak = (100, 100, 100, 100, 100, 104)
    strong = (100, 100.8, 101.6, 102.4, 103.2, 104)
    for timestamp, (weak_px, strong_px) in enumerate(zip(weak, strong), start=1):
        rows = engine.evaluate(
            [
                market_row(float(weak_px), symbol="WEAK"),
                market_row(float(strong_px), symbol="STRONG"),
            ],
            now=float(timestamp),
            require_depth=False,
        )
    assert {row["Symbol"] for row in rows} == {"WEAK", "STRONG"}
    by_symbol = {row["Symbol"]: row for row in rows}
    assert by_symbol["STRONG"]["MomentumScore"] > by_symbol["WEAK"]["MomentumScore"]
    # The move is identical (4%), so ranking must come from the small
    # structure bonus rather than from a hard veto.
    assert abs(by_symbol["STRONG"]["ObservedLocalMove (%)"] - by_symbol["WEAK"]["ObservedLocalMove (%)"]) < 1e-9


def test_negative_subthreshold_and_unconfirmed_entries_are_rejected():
    # Not enough completed scans: a single positive tick is never an entry.
    insufficient = strict_engine()
    assert not insufficient.evaluate([market_row(100.0)], now=1.0, require_depth=False)

    # The complete move is positive but below the configured 3% threshold.
    below_threshold = strict_engine()
    candidates = scan_until(below_threshold, (100, 100.1, 100.2, 100.3, 100.4, 100.5))
    assert candidates == []
    assert "below_threshold" in below_threshold.last_assessments["ABC"].reason_codes
    assert not below_threshold.last_assessments["ABC"].entry_allowed

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
    assert not negative.last_assessments["ABC"].entry_allowed


def test_assess_trend_treats_structure_as_soft_quality():
    hard = assess_trend(
        (100, 101, 102, 103, 104, 105),
        threshold_percent=3.0,
        min_consecutive_positive_scans=3,
        trend_lookback_scans=6,
    )
    soft = assess_trend(
        (100, 100, 100, 100, 100, 104),
        threshold_percent=3.0,
        min_consecutive_positive_scans=3,
        trend_lookback_scans=6,
    )
    assert hard.entry_allowed and soft.entry_allowed
    assert hard.structure_score > soft.structure_score
    assert hard.ranking_score > soft.ranking_score
    blocked = assess_trend(
        (100, 100, 100, 100, 100, 102),
        threshold_percent=3.0,
        min_consecutive_positive_scans=3,
        trend_lookback_scans=6,
    )
    assert not blocked.entry_allowed
    assert "below_threshold" in blocked.reason_codes


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
    tracker.trailing_distance_pct = 1.0
    tracker.trailing_activation_pct = 2.0
    tracker.quiet_skips = False
    tracker.paper_half_spread_pct = 0.0
    return tracker


def test_stop_loss_exit_has_priority_over_raw_trend_exit(tmp_path):
    candidate = confirmed_candidate(strict_engine())
    tracker = configured_tracker(tmp_path, "stop")
    assert tracker.process_new_signals([candidate])["opened"] == 1
    opened = tracker.get_open_trades()[0]
    assert float(opened["current_stop_loss"]) > 0
    assert float(opened["sl_pct"]) == 3.0

    stop_tick = market_row(100.0)
    stop_tick["Signal"] = "Neutral"
    result = tracker.process_new_signals([stop_tick])

    assert result["closed"] == 1
    closed = tracker.get_all_trades()[0]
    assert closed["exit_reason"] == "Stop Loss"


def test_trailing_stop_ratchets_and_exits(tmp_path):
    candidate = confirmed_candidate(strict_engine())
    tracker = configured_tracker(tmp_path, "trail")
    tracker.trailing_stop_enabled = True
    tracker.trailing_distance_pct = 1.0
    tracker.trailing_activation_pct = 2.0
    assert tracker.process_new_signals([candidate])["opened"] == 1

    opened = tracker.get_open_trades()[0]
    entry = float(opened["entry_price"])
    initial_stop = float(opened["current_stop_loss"])
    assert initial_stop == entry * 0.97

    up = market_row(entry * 1.025)
    up["Signal"] = "Neutral"
    result = tracker.process_new_signals([up])
    assert result["closed"] == 0
    ratcheted = tracker.get_open_trades()[0]
    new_stop = float(ratcheted["current_stop_loss"])
    assert new_stop > initial_stop
    assert new_stop >= entry

    down = market_row(new_stop * 0.999)
    down["Signal"] = "Neutral"
    result = tracker.process_new_signals([down])
    assert result["closed"] == 1
    closed = tracker.get_all_trades()[0]
    assert closed["exit_reason"] == "Trailing Stop"


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


def test_incomplete_structure_still_opens_a_protected_trade(tmp_path):
    candidate = confirmed_candidate(strict_engine(), prices=(100, 100, 100, 100, 100, 104))
    tracker = configured_tracker(tmp_path, "soft_entry")
    tracker.trailing_stop_enabled = True
    assert tracker.process_new_signals([candidate])["opened"] == 1
    opened = tracker.get_open_trades()[0]
    assert float(opened["current_stop_loss"]) == float(opened["entry_price"]) * 0.97
    assert float(opened["trail_distance_pct"]) == 1.0
    assert float(opened["sl_pct"]) == 3.0
