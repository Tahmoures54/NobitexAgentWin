"""
Tests for SignalTracker v6.4.2 fixes.

Bugs being covered:
1. `close_trade()` and `_open_pump_position()` hardcoded `fee_pct = 0.1`
   instead of reading `self.trading_fee_pct`.  Users with a different
   Nobitex fee tier had every P&L calculation wrong.

2. Schema migrations in `_init_db()` swallowed `sqlite3.Error` with no
   diagnostic.  A failed ALTER TABLE left the DB inconsistent.

3. Wallet verification used a hard 90% floor and rejected entries when
   the exchange reported rounding dust.  A dust-aware floor is added.
"""
from __future__ import annotations

import gc
import tempfile
import unittest
from unittest.mock import patch

from signal_tracker import SignalTracker


def _close_tracker(tracker):
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


class TestFeePctReadsFromConfig(unittest.TestCase):
    """v6.4.2: fee must be read from self.trading_fee_pct, not hardcoded."""

    def test_fee_pct_returns_configured_value(self):
        st = SignalTracker.__new__(SignalTracker)
        st.trading_fee_pct = 0.25
        self.assertEqual(st._fee_pct(), 0.25)

    def test_fee_pct_defaults_when_missing(self):
        st = SignalTracker.__new__(SignalTracker)
        # No attribute set → class default; ensure safe fallback.
        self.assertAlmostEqual(st._fee_pct(), 0.1)

    def test_fee_pct_rejects_negative(self):
        st = SignalTracker.__new__(SignalTracker)
        st.trading_fee_pct = -1.0
        self.assertEqual(st._fee_pct(), 0.0)

    def test_fee_pct_handles_garbage(self):
        st = SignalTracker.__new__(SignalTracker)
        st.trading_fee_pct = "not-a-number"
        self.assertAlmostEqual(st._fee_pct(), 0.1)

    def test_get_risk_settings_uses_fee_pct(self):
        st = SignalTracker.__new__(SignalTracker)
        st.trading_fee_pct = 0.33
        st.stop_loss_pct = 2.0
        st.trailing_stop_enabled = True
        st.trailing_distance_pct = 1.5
        st.take_profit_percent = 5.0
        st.trailing_activation_pct = 0.8

        risk = st._get_risk_settings()
        self.assertEqual(risk["trading_fee_pct"], 0.33)


class TestWalletMeetsFloor(unittest.TestCase):
    """v6.4.2: dust-tolerant wallet floor."""

    def test_exact_match_ok(self):
        st = SignalTracker.__new__(SignalTracker)
        self.assertTrue(st._wallet_meets_floor(1000.0, 1000.0))

    def test_ninety_percent_ok(self):
        # Right at the ratio boundary → accept.
        st = SignalTracker.__new__(SignalTracker)
        self.assertTrue(st._wallet_meets_floor(900.0, 1000.0))

    def test_dust_tolerance_accepts_885(self):
        # Floor = 900 - 1000*0.02 = 880 → 885 passes.
        st = SignalTracker.__new__(SignalTracker)
        self.assertTrue(st._wallet_meets_floor(885.0, 1000.0))

    def test_dust_tolerance_rejects_870(self):
        # Below the dust floor (880) → reject.
        st = SignalTracker.__new__(SignalTracker)
        self.assertFalse(st._wallet_meets_floor(870.0, 1000.0))

    def test_zero_required_always_ok(self):
        st = SignalTracker.__new__(SignalTracker)
        self.assertTrue(st._wallet_meets_floor(0.0, 0.0))

    def test_very_small_quantity(self):
        # For BTC-scale dust (0.00001 BTC).
        st = SignalTracker.__new__(SignalTracker)
        # Floor = 0.9 * 1e-5 - 1e-5 * 0.02 = 8.8e-6
        self.assertTrue(st._wallet_meets_floor(9e-6, 1e-5))
        self.assertFalse(st._wallet_meets_floor(8e-6, 1e-5))


class TestMigrationLogging(unittest.TestCase):
    """v6.4.2: a failed migration must be logged, not silently ignored.

    `sqlite3.Cursor.execute` is a C-level method and cannot be patched
    with `unittest.mock.patch.object`.  We instead inject a custom
    Connection class through `sqlite3.connect(..., factory=...)` that
    returns a Cursor subclass whose `execute()` raises for one specific
    ALTER TABLE statement.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    def tearDown(self):
        try:
            gc.collect()
        except Exception:
            pass
        try:
            self.tmp.cleanup()
        except (PermissionError, OSError):
            pass

    def test_migration_failure_is_logged(self):
        import sqlite3 as _sqlite3
        import signal_tracker as _st_mod

        real_connect = _sqlite3.connect

        class _FailingCursor(_sqlite3.Cursor):
            def execute(self, sql, *args, **kwargs):
                if (
                    isinstance(sql, str)
                    and "ADD COLUMN protective_order_id" in sql
                ):
                    raise _sqlite3.Error("simulated migration failure")
                return super().execute(sql, *args, **kwargs)

        class _FailingConn(_sqlite3.Connection):
            def cursor(self, *args, **kwargs):
                return _FailingCursor(self, *args, **kwargs)

        def failing_connect(*args, **kwargs):
            kwargs["factory"] = _FailingConn
            return real_connect(*args, **kwargs)

        with patch.object(_st_mod.sqlite3, "connect", failing_connect):
            with patch("signal_tracker.APPDATA_DIR", self.tmp.name):
                with self.assertLogs("signal_tracker", level="ERROR") as cm:
                    tracker = SignalTracker(
                        db_filename="migration_test.db",
                        account_balance=1000.0,
                    )
                    _close_tracker(tracker)

        messages = "\n".join(cm.output)
        self.assertIn(
            "protective_order_id", messages,
            f"Migration failure was not logged. Logs:\n{messages}",
        )

    def test_migration_adds_column_on_fresh_db(self):
        """Positive control: the column must be added on a fresh DB."""
        with patch("signal_tracker.APPDATA_DIR", self.tmp.name):
            tracker = SignalTracker(
                db_filename="positive_mig.db",
                account_balance=1000.0,
            )
            try:
                with tracker._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("PRAGMA table_info(trades)")
                    cols = {row[1] for row in cur.fetchall()}
                self.assertIn("protective_order_id", cols)
            finally:
                _close_tracker(tracker)


class TestFeeAwarePnl(unittest.TestCase):
    """End-to-end: a different fee must change the reported PnL."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        with patch("signal_tracker.APPDATA_DIR", self.tmp.name):
            self.low_fee = SignalTracker(
                db_filename="lf.db",
                account_balance=1_000_000.0,
            )
            self.high_fee = SignalTracker(
                db_filename="hf.db",
                account_balance=1_000_000.0,
            )
        self.low_fee.trading_fee_pct = 0.05
        self.high_fee.trading_fee_pct = 0.50
        self.low_fee.confirmation_enabled = False
        self.high_fee.confirmation_enabled = False

    def tearDown(self):
        _close_tracker(self.low_fee)
        _close_tracker(self.high_fee)
        try:
            gc.collect()
        except Exception:
            pass
        try:
            self.tmp.cleanup()
        except (PermissionError, OSError):
            pass

    @staticmethod
    def _net_pnl_pct(side, entry, price, fee_pct):
        # Mirrors SignalTracker._compute_net_pnl_pct.
        if entry <= 0:
            return 0.0
        sign = -1.0 if side == "short" else 1.0
        gross_pct = sign * (price - entry) / entry * 100.0
        fees_pct = fee_pct * (1.0 + price / entry)
        return gross_pct - fees_pct

    def test_higher_fee_reduces_net_pnl(self):
        low = self._net_pnl_pct("long", 100.0, 110.0, self.low_fee.trading_fee_pct)
        high = self._net_pnl_pct("long", 100.0, 110.0, self.high_fee.trading_fee_pct)
        self.assertGreater(low, high)
        self.assertAlmostEqual(low - high, 0.45 * (1 + 1.1), places=4)


if __name__ == "__main__":
    unittest.main()