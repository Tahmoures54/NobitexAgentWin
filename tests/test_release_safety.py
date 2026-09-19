
from __future__ import annotations

from pathlib import Path

import pytest

from signal_tracker import SignalTracker
from trading.nobitex_client import NobitexClient


class _StopExecutor:
    def __init__(self, result=None):
        self.calls = []
        self.result = result or {"status": "inactive", "order_id": "123"}

    def place_order(self, **kwargs):
        self.calls.append(kwargs)
        return dict(self.result)

    def cancel_order(self, *args, **kwargs):
        return {"status": "canceled"}


def test_protective_stop_never_reduces_quantity(tmp_path):
    ex = _StopExecutor()
    tracker = SignalTracker(
        account_balance=100_000_000,
        db_filename="release_safety.db",
        executor=ex,
    )
    tracker.mode = "real"
    tracker.max_notional_quote = 1_000_000.0

    order_id = tracker._place_exchange_stop("BTCIRT", 1.0, 100_000_000.0)

    assert order_id == "123"
    assert ex.calls
    assert ex.calls[0]["quantity"] == pytest.approx(1.0)


class _PartialExecutor:
    def __init__(self):
        self.cancelled = False
        self.status_calls = 0

    def get_order_status(self, order_id, symbol):
        self.status_calls += 1
        if self.cancelled:
            return {
                "status": "canceled",
                "matched_amount": 0.25,
                "unmatched_amount": 0.0,
            }
        return {
            "status": "partial",
            "matched_amount": 0.25,
            "unmatched_amount": 0.75,
        }

    def cancel_order(self, order_id, symbol):
        self.cancelled = True
        return {"status": "canceled"}


def test_partial_fill_is_cancelled_and_reconciled():
    ex = _PartialExecutor()
    tracker = SignalTracker(account_balance=1000.0, executor=ex)
    result = tracker._wait_for_fill("42", "BTCIRT", timeout=0.01, initial_delay=0)

    assert result is not None
    assert ex.cancelled is True
    assert tracker._order_matched_qty(result) == pytest.approx(0.25)


def test_market_order_does_not_send_synthetic_price(monkeypatch):
    client = NobitexClient(api_key="", api_secret="")
    captured = {}

    def fake_request(method, path, body=None, query_params=None, signed=True):
        captured.update(body or {})
        return {
            "status": "ok",
            "order": {
                "id": 1,
                "market": "BTC-RLS",
                "type": "buy",
                "execution": "Market",
                "status": "Done",
                "amount": "0.01",
                "matchedAmount": "0.01",
                "unmatchedAmount": "0",
                "averagePrice": "1000000000",
            },
        }

    monkeypatch.setattr(client, "_request", fake_request)
    client.place_order("BTCIRT", "buy", "market", 0.01, price=None)

    assert "price" not in captured
    assert captured["execution"] == "market"


def test_eagle_uses_one_hour_change_not_last_tick():
    from trading.nobitex_momentum_engine import NobitexMomentumEngine

    engine = NobitexMomentumEngine(
        btc_dump_exception_enabled=True,
        eagle_min_observed_move_pct=2.5,
        eagle_min_1h_pct=2.0,
        eagle_min_volume_irt=100.0,
        eagle_max_spread_pct=1.0,
        max_chase_pct=1.0,
    )

    # The last tick is only +0.1%, but the actual 1h field is +2.4%.
    # The eagle exception should therefore be allowed.
    assert engine._eagle(3.0, 0.1, 2.4, 1_000.0, 0.2, 0.1, 5.0)
