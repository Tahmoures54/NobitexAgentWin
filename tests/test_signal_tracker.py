# tests/test_signal_tracker.py
"""
Tests for SignalTracker v3.7 (with Pending Confirmation)

Windows fix (v7.6.1):
- TemporaryDirectory now uses ignore_cleanup_errors=True so a still-open
  SQLite handle cannot fail the test in tearDown.
- tearDown closes the tracker DB connection and forces GC before cleanup.
"""
from __future__ import annotations

import gc
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from signal_tracker import SignalTracker


def _close_tracker(tracker) -> None:
    """Best-effort close of the tracker's SQLite handle on any Python version."""
    if tracker is None:
        return
    for name in ("close", "shutdown", "disconnect"):
        fn = getattr(tracker, name, None)
        if callable(fn):
            try:
                fn()
                return
            except Exception:
                pass
    for attr in ("_conn", "conn", "_connection", "connection", "_db", "db"):
        obj = getattr(tracker, attr, None)
        if obj is None:
            continue
        for method in ("close", "disconnect"):
            fn = getattr(obj, method, None)
            if callable(fn):
                try:
                    fn()
                    return
                except Exception:
                    pass


def _safe_cleanup(tmp) -> None:
    """Force GC and swallow Windows file-lock errors on temp cleanup."""
    try:
        gc.collect()
    except Exception:
        pass
    try:
        tmp.cleanup()
    except (PermissionError, OSError):
        # On Windows, SQLite may still hold a lock for a moment.
        # ignore_cleanup_errors=True on the TemporaryDirectory already
        # handles this, but we defend anyway.
        pass


class TestSignalTrackerCoreLogic(unittest.TestCase):
    def setUp(self):
        self.risk = {
            "take_profit_pct": 10.0,
            "stop_loss_pct": 5.0,
            "trailing_stop_enabled": True,
            "trailing_activation_pct": 5.0,
            "trailing_distance_pct": 2.0,
            "reverse_signal_exit_enabled": True,
        }

    def test_asset_key_priority(self):
        row1 = {"Slug": "bitcoin", "Symbol": "BTC"}
        row2 = {"Symbol": "ETH"}
        self.assertEqual(SignalTracker._get_asset_key(row1), "cg:bitcoin")
        self.assertEqual(SignalTracker._get_asset_key(row2), "sym:ETH")

    def test_safe_float_or_zero(self):
        self.assertEqual(SignalTracker._safe_float_or_zero(None), 0.0)
        self.assertEqual(SignalTracker._safe_float_or_zero("12.5"), 12.5)

    def test_long_fixed_tp(self):
        risk = self.risk.copy()
        risk["trailing_stop_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "long", 100.0, 111.0, None, "", risk, 100.0
        )
        self.assertTrue(res["should_close"])
        self.assertEqual(res["exit_reason"], "Take Profit")
        self.assertAlmostEqual(res["pnl_pct"], 10.0, places=1)

    def test_long_fixed_sl(self):
        risk = self.risk.copy()
        risk["trailing_stop_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "long", 100.0, 94.0, None, "", risk, 100.0
        )
        self.assertTrue(res["should_close"])
        self.assertEqual(res["exit_reason"], "Stop Loss")
        self.assertEqual(res["pnl_pct"], -5.0)

    def test_short_fixed_tp(self):
        risk = self.risk.copy()
        risk["trailing_stop_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "short", 100.0, 89.0, None, "", risk, 100.0
        )
        self.assertTrue(res["should_close"])
        self.assertEqual(res["exit_reason"], "Take Profit")
        self.assertAlmostEqual(res["pnl_pct"], 10.0, places=1)

    def test_short_fixed_sl(self):
        risk = self.risk.copy()
        risk["trailing_stop_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "short", 100.0, 106.0, None, "", risk, 100.0
        )
        self.assertTrue(res["should_close"])
        self.assertEqual(res["exit_reason"], "Stop Loss")
        self.assertEqual(res["pnl_pct"], -5.0)

    def test_fallback_sl_before_trailing_activation(self):
        risk = self.risk.copy()
        risk["trailing_activation_pct"] = 5.0
        risk["stop_loss_pct"] = 3.0
        res = SignalTracker._evaluate_open_trade(
            "short", 100.0, 103.5, None, "", risk, 100.0
        )
        self.assertTrue(res["should_close"])
        self.assertEqual(res["exit_reason"], "Stop Loss")

    def test_long_trailing_activation(self):
        risk = self.risk.copy()
        res = SignalTracker._evaluate_open_trade(
            "long", 100.0, 105.0, None, "", risk, 100.0
        )
        self.assertFalse(res["should_close"])
        self.assertAlmostEqual(res["new_sl"], 102.9, places=1)

    def test_long_trailing_update_and_hit(self):
        risk = self.risk.copy()
        res = SignalTracker._evaluate_open_trade(
            "long", 100.0, 105.0, None, "", risk, 100.0
        )
        self.assertFalse(res["should_close"])
        self.assertAlmostEqual(res["new_sl"], 102.9, places=1)

        res_update = SignalTracker._evaluate_open_trade(
            "long", 100.0, 108.0, 102.9, "", risk, 100.0
        )
        self.assertFalse(res_update["should_close"])
        self.assertAlmostEqual(res_update["new_sl"], 105.84, places=2)

        res_hit = SignalTracker._evaluate_open_trade(
            "long", 100.0, 105.0, 105.84, "", risk, 100.0
        )
        self.assertTrue(res_hit["should_close"])
        self.assertEqual(res_hit["exit_reason"], "Trailing Stop")

    def test_short_trailing_activation(self):
        risk = self.risk.copy()
        res = SignalTracker._evaluate_open_trade(
            "short", 100.0, 94.0, None, "", risk, 100.0
        )
        self.assertFalse(res["should_close"])
        self.assertAlmostEqual(res["new_sl"], 95.88, places=2)

    def test_short_trailing_update_and_hit(self):
        risk = self.risk.copy()
        res = SignalTracker._evaluate_open_trade(
            "short", 100.0, 94.0, None, "", risk, 100.0
        )
        self.assertFalse(res["should_close"])
        self.assertAlmostEqual(res["new_sl"], 95.88, places=2)

        res_hit = SignalTracker._evaluate_open_trade(
            "short", 100.0, 96.0, 95.88, "", risk, 100.0
        )
        self.assertTrue(res_hit["should_close"])
        self.assertEqual(res_hit["exit_reason"], "Trailing Stop")

    def test_long_trail_does_not_lock_below_entry(self):
        risk = self.risk.copy()
        risk["stop_loss_pct"] = 1.8
        risk["trailing_activation_pct"] = 0.80
        risk["trailing_distance_pct"] = 1.60
        risk["reverse_signal_exit_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "long", 100.0, 100.81, None, "", risk, 100.0
        )
        self.assertFalse(res["should_close"])
        self.assertGreaterEqual(res["new_sl"], 100.0)

        res_hit = SignalTracker._evaluate_open_trade(
            "long", 100.0, 99.90, res["new_sl"], "", risk, 100.0
        )
        self.assertTrue(res_hit["should_close"])
        self.assertEqual(res_hit["exit_reason"], "Trailing Stop")
        self.assertGreaterEqual(res_hit["pnl_pct"], -0.05)

    def test_extract_pump_prefers_observed_even_if_flat(self):
        row = {
            "ObservedGlobalMove (%)": 0.0,
            "LiveLeadMove (%)": 1.36,
            "Global1hPct": 1.36,
            "1h Change (%)": 1.36,
        }
        pump = SignalTracker._extract_pump_percentage(
            object(), row, "Trend Buy +1.36%"
        )
        self.assertEqual(pump, 0.0)

    def test_closed_pnl_uses_irt_not_dollar(self):
        tracker = SignalTracker.__new__(SignalTracker)
        tracker.quote_currency = "IRT"
        text = SignalTracker._format_closed_pnl(tracker, -0.80, -70393.26)
        self.assertIn("IRT", text)
        self.assertNotIn("$", text)

    def test_reverse_signal_disabled(self):
        risk = self.risk.copy()
        risk["reverse_signal_exit_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "long", 100.0, 102.0, None, "Sell Signal", risk, 100.0
        )
        self.assertFalse(res["should_close"])

    def test_reverse_signal_long_closes(self):
        risk = self.risk.copy()
        risk["trailing_stop_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "long", 100.0, 102.0, None, "Strong Sell", risk, 100.0
        )
        self.assertTrue(res["should_close"])
        self.assertEqual(res["exit_reason"], "Signal Exit")

    def test_reverse_signal_short_closes(self):
        risk = self.risk.copy()
        risk["trailing_stop_enabled"] = False
        res = SignalTracker._evaluate_open_trade(
            "short", 100.0, 98.0, None, "Strong Buy", risk, 100.0
        )
        self.assertTrue(res["should_close"])
        self.assertEqual(res["exit_reason"], "Signal Exit")


class TestSignalTrackerIntegration(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors handles Windows file-lock races on temp dirs.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        mock_learner = MagicMock()
        mock_learner.validate_signal.return_value = {
            "allowed": True,
            "confidence": 80,
        }

        with patch("signal_tracker.APPDATA_DIR", self.tmp.name):
            self.tracker = SignalTracker(
                db_filename="test_signal.db",
                learner=mock_learner,
                max_open_trades=3,
                min_volume_24h=1000.0,
                min_market_cap=100_000.0,
            )
            # ورود فوری برای اکثر تست‌های integration
            self.tracker.confirmation_enabled = False
            # Integration tests use explicit pump-threshold tests where needed;
            # keep legacy entry/limit tests focused on their intended behavior.
            self.tracker.pump_threshold_pct = 0.0

    def tearDown(self):
        _close_tracker(self.tracker)
        _safe_cleanup(self.tmp)

    def _make_row(
        self,
        symbol,
        price,
        signal,
        score=20,
        volume=500_000,
        mcap=50_000_000,
    ):
        return {
            "Symbol": symbol,
            "Price": price,
            "Signal": signal,
            "Score": score,
            "Volume": volume,
            "Market Cap": mcap,
            "Risk": "Low",
            "RSI": 45.0,
            "24h Change (%)": 2.0,
        }

    def test_empty_tracker_has_no_managed_positions(self):
        self.assertFalse(self.tracker.has_managed_positions())
        self.tracker.process_new_signals([self._make_row("BTC", 100, "Buy Signal")])
        self.assertTrue(self.tracker.has_managed_positions())

    def test_trend_entry_respects_observed_threshold(self):
        self.tracker.pump_threshold_pct = 0.7
        row = self._make_row("W", 23680, "Trend Buy +0.17%")
        row["ObservedGlobalMove (%)"] = 0.17
        row["Global1hPct"] = 1.32
        stats = self.tracker.process_new_signals([row])
        self.assertEqual(stats["opened"], 0)

    def test_generic_buy_signal_requires_pump_threshold(self):
        self.tracker.pump_threshold_pct = 5.0
        stats = self.tracker.process_new_signals(
            [self._make_row("BTC", 100, "Buy Signal")]
        )
        self.assertEqual(stats["opened"], 0)

        row = self._make_row("ETH", 100, "Buy Signal")
        row["1h Change (%)"] = 6.0
        stats = self.tracker.process_new_signals([row])
        self.assertEqual(stats["opened"], 1)

    def test_open_and_close_long(self):
        data = [self._make_row("BTC", 100, "Buy Signal", score=25)]
        stats = self.tracker.process_new_signals(data)
        self.assertEqual(stats["opened"], 1)

        trades = self.tracker.get_all_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["status"], "open")

        ok = self.tracker.close_trade_manually(trades[0]["id"], 110)
        self.assertTrue(ok)
        trades2 = self.tracker.get_all_trades()
        self.assertEqual(trades2[0]["status"], "closed")

    def test_max_open_trades_limit(self):
        self.tracker.max_long_trades = None
        self.tracker.max_short_trades = None
        data = [
            self._make_row("BTC", 50000, "Strong Buy", score=30),
            self._make_row("ETH", 3000, "Strong Buy", score=25),
            self._make_row("SOL", 150, "Strong Buy", score=20),
        ]
        self.tracker.max_open_trades = 2
        stats = self.tracker.process_new_signals(data)
        self.assertEqual(stats["opened"], 2)

    def test_max_long_short_limits(self):
        self.tracker.max_open_trades = 5
        self.tracker.max_long_trades = 1
        self.tracker.max_short_trades = 1
        data = [
            self._make_row("BTC", 50000, "Strong Buy", score=30),
            self._make_row("ETH", 3000, "Strong Buy", score=25),
            self._make_row("XRP", 0.5, "Strong Sell", score=20),
        ]
        stats = self.tracker.process_new_signals(data)
        self.assertEqual(stats["opened"], 2)

    def test_quality_filters_skip(self):
        data = [self._make_row("LOW", 10, "Strong Buy", volume=100, mcap=1000)]
        stats = self.tracker.process_new_signals(data)
        self.assertEqual(stats["opened"], 0)

    def test_manual_close(self):
        data = [self._make_row("BTC", 100, "Buy Signal")]
        self.tracker.process_new_signals(data)
        trades = self.tracker.get_all_trades()
        self.assertTrue(len(trades) >= 1)
        self.assertTrue(self.tracker.close_trade_manually(trades[0]["id"], 110))

    def test_summary_stats(self):
        data = [self._make_row("BTC", 100, "Buy Signal")]
        self.tracker.process_new_signals(data)
        trades = self.tracker.get_all_trades()
        self.tracker.close_trade_manually(trades[0]["id"], 110)
        stats = self.tracker.get_summary_stats()
        self.assertEqual(stats["total_trades"], 1)
        self.assertGreater(stats["win_rate"], 0)

    def test_pending_mode_creates_pending_not_open(self):
        """با confirmation روشن، باید pending شود نه open."""
        self.tracker.confirmation_enabled = True
        self.tracker.confirmation_pct = 0.8
        data = [self._make_row("BTC", 100, "Strong Buy", score=30)]
        stats = self.tracker.process_new_signals(data)
        self.assertEqual(stats["opened"], 0)
        self.assertGreaterEqual(stats.get("pending", 0), 1)
        pendings = self.tracker.get_pending_signals()
        self.assertGreaterEqual(len(pendings), 1)

    def test_pending_confirms_on_favorable_move(self):
        """حرکت هم‌جهت → ورود تأیید می‌شود."""
        self.tracker.confirmation_enabled = True
        self.tracker.confirmation_pct = 0.5
        self.tracker.invalidation_pct = 2.0

        data1 = [self._make_row("BTC", 100, "Strong Buy", score=30)]
        stats1 = self.tracker.process_new_signals(data1)
        self.assertEqual(stats1["opened"], 0)
        self.assertGreaterEqual(stats1.get("pending", 0), 1)

        data2 = [self._make_row("BTC", 101, "Strong Buy", score=30)]
        stats2 = self.tracker.process_new_signals(data2)
        self.assertEqual(stats2["opened"], 0)
        self.assertGreaterEqual(stats2.get("pending", 0), 1)

        data3 = [self._make_row("BTC", 101.6, "Strong Buy", score=30)]
        stats3 = self.tracker.process_new_signals(data3)
        self.assertEqual(stats3["opened"], 1)
        trades = self.tracker.get_all_trades()
        self.assertEqual(len([t for t in trades if t["status"] == "open"]), 1)


class TestPrioritization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        mock_learner = MagicMock()
        mock_learner.validate_signal.return_value = {
            "allowed": True,
            "confidence": 50,
        }
        with patch("signal_tracker.APPDATA_DIR", self.tmp.name):
            self.tracker = SignalTracker(
                db_filename="prio.db",
                learner=mock_learner,
                max_open_trades=2,
                min_volume_24h=1000,
                min_market_cap=100_000,
            )
            self.tracker.confirmation_enabled = False
            self.tracker.pump_threshold_pct = 0.0

    def tearDown(self):
        _close_tracker(self.tracker)
        _safe_cleanup(self.tmp)

    def test_sorting_order(self):
        data = [
            {
                "Symbol": "LOW",
                "Price": 10,
                "Signal": "Buy Signal",
                "Score": 5,
                "Volume": 500000,
                "Market Cap": 50_000_000,
                "Risk": "Low",
            },
            {
                "Symbol": "HIGH",
                "Price": 20,
                "Signal": "Strong Buy",
                "Score": 30,
                "Volume": 500000,
                "Market Cap": 50_000_000,
                "Risk": "Low",
            },
        ]
        stats = self.tracker.process_new_signals(data)
        self.assertEqual(stats["opened"], 2)
        trades = self.tracker.get_all_trades()
        symbols = {t["symbol"] for t in trades if t["status"] == "open"}
        self.assertIn("HIGH", symbols)

    def test_strong_signal_boost(self):
        data = [
            {
                "Symbol": "A",
                "Price": 10,
                "Signal": "Buy Signal",
                "Score": 10,
                "Volume": 500000,
                "Market Cap": 50_000_000,
                "Risk": "Low",
            },
            {
                "Symbol": "B",
                "Price": 20,
                "Signal": "Strong Buy",
                "Score": 10,
                "Volume": 500000,
                "Market Cap": 50_000_000,
                "Risk": "Low",
            },
        ]
        self.tracker.max_open_trades = 1
        stats = self.tracker.process_new_signals(data)
        self.assertEqual(stats["opened"], 1)
        trades = self.tracker.get_all_trades()
        self.assertEqual(trades[0]["symbol"], "B")

    def test_fixed_ten_million_rial_not_clipped_by_stale_caps(self):
        t = self.tracker
        t.position_size_mode = "fixed"
        t.fixed_position_quote = 10_000_000.0
        t.max_notional_quote = 1_000_000.0
        t.max_position_pct = 25.0
        t.min_notional_quote = 300_000.0
        t.account_balance = 38_000_000.0
        t.cash = 38_000_000.0
        price = 12_680_530.0
        qty = t._position_size_for(price, 2.2)
        self.assertGreater(qty, 0)
        self.assertAlmostEqual(qty * price, 10_000_000.0, delta=1.0)

    def test_fixed_lot_needs_enough_cash(self):
        t = self.tracker
        t.position_size_mode = "fixed"
        t.fixed_position_quote = 10_000_000.0
        t.min_notional_quote = 300_000.0
        t.account_balance = 1_000_000.0
        t.cash = 1_000_000.0
        qty = t._position_size_for(12_680_530.0, 2.2)
        self.assertEqual(qty, 0.0)


if __name__ == "__main__":
    unittest.main()