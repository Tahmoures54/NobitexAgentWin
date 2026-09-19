"""Live buy must not record a position until Nobitex actually fills."""
from __future__ import annotations

import tempfile
from unittest.mock import MagicMock, patch

import pytest

import signal_tracker as st_mod
from signal_tracker import SignalTracker


class UnfilledExecutor:
    def __init__(self, spendable=27_075_250.73, total=38_183_865.13):
        self.spendable = spendable
        self.total = total
        self.cancelled = []
        self.placed = []

    def get_balance(self, asset):
        key = str(asset or "").upper()
        if key in ("IRT", "RLS", "IRR"):
            return self.spendable
        return 0.0

    def get_balance_total(self, asset):
        key = str(asset or "").upper()
        if key in ("IRT", "RLS", "IRR"):
            return self.total
        return 0.0

    def get_balance_fresh(self, asset):
        return self.get_balance(asset)

    def invalidate_balance_cache(self):
        return None

    def get_ticker(self, symbol):
        return {"ask": 651810.0, "last": 651810.0, "bid": 650000.0}

    def place_order(self, **kwargs):
        self.placed.append(kwargs)
        if str(kwargs.get("side") or "").lower() != "buy":
            raise AssertionError(f"unexpected non-buy order: {kwargs}")
        return {
            "status": "open",
            "order_id": "6212089660",
            "matched_amount": 0.0,
            "quantity": kwargs.get("quantity"),
            "executed_price": 0.0,
        }

    def get_order_status(self, order_id, symbol=None):
        return {
            "status": "open",
            "order_id": order_id,
            "matched_amount": 0.0,
            "executed_price": 0.0,
        }

    def cancel_order(self, order_id, symbol=None):
        self.cancelled.append(order_id)
        return True

    def get_open_orders(self, symbol=None):
        return []

    def get_positions(self):
        return []

    def is_symbol_supported(self, symbol):
        return True


@pytest.fixture
def live_tracker(tmp_path, monkeypatch):
    monkeypatch.setattr(st_mod, "APPDATA_DIR", str(tmp_path))
    monkeypatch.setattr(st_mod, "FILL_POLL_TIMEOUT", 0.05)
    monkeypatch.setattr(st_mod.time, "sleep", lambda *a, **k: None)
    executor = UnfilledExecutor()
    tracker = SignalTracker(
        account_balance=38_183_865.13,
        max_open_trades=3,
        min_volume_24h=0.0,
        min_market_cap=0.0,
        executor=executor,
        db_filename="real_trades.db",
        learner=MagicMock(),
    )
    tracker.quote_currency = "IRT"
    tracker._last_balance_read_ts = 0.0
    tracker._refresh_cash_from_executor()
    tracker.account_balance = executor.total
    tracker.peak_equity = executor.total
    tracker.cash = executor.spendable
    tracker.ignore_signal_filters = True
    tracker.confirmation_enabled = False
    tracker.pump_threshold_pct = 0.0
    tracker.position_size_mode = "fixed"
    tracker.fixed_position_quote = 10_000_000.0
    tracker.max_notional_quote = 10_000_000.0
    tracker.min_notional_quote = 0.0
    tracker.max_total_exposure_pct = 90.0
    tracker.max_drawdown_percent = 20.0
    tracker.halt_on_max_drawdown = True
    tracker.auto_trading_enabled = True
    tracker.allow_new_entries = True
    tracker.trading_halted = False
    return tracker, executor


def _buy_row():
    return {
        "Symbol": "XTZ",
        "Price": 651810.0,
        "Signal": "Trend Buy +1.52%",
        "Score": 51.9,
        "Volume": 1_000_000,
        "Market Cap": 1_000_000,
        "1h Change (%)": 1.52,
        "pump_pct": 1.52,
        "Risk": "Low",
    }


def test_unfilled_buy_is_cancelled_and_not_recorded(live_tracker):
    tracker, executor = live_tracker
    stats = tracker.process_new_signals([_buy_row()])
    assert stats["opened"] == 0
    assert tracker.get_open_trades() == []
    assert executor.cancelled == ["6212089660"]
    assert len(executor.placed) == 1
    assert executor.placed[0]["side"] == "buy"


def test_monitor_only_does_not_submit_buys(live_tracker):
    tracker, executor = live_tracker
    tracker.allow_new_entries = False
    stats = tracker.process_new_signals([_buy_row()])
    assert stats["opened"] == 0
    assert executor.placed == []
    assert tracker.get_open_trades() == []


def test_locked_irt_does_not_trip_drawdown(live_tracker):
    tracker, executor = live_tracker
    tracker.allow_new_entries = False
    tracker.peak_equity = 38_183_865.13
    tracker.account_balance = 38_183_865.13
    stats = tracker.process_cycle([])
    assert stats["halted"] == 0
    assert tracker.trading_halted is False
    assert tracker.account_balance == pytest.approx(38_183_865.13, rel=1e-6)
