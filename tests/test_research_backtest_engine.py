# tests/test_research_backtest_engine.py
"""Regression tests for the research replay engine (tools/research).

The engine is the evidence behind PROFITABILITY_ANALYSIS.md, so its accounting
has to be right even though nothing in the shipped bot imports it.  Two defects
silently flattered the results before:

* leftovers open at a drawdown halt were marked at the *last bar of the whole
  window* instead of the bar where the scan loop stopped - free look-ahead that
  rewarded exactly the cells that blew their drawdown limit;
* the live bot's ``max_hold_minutes`` time stop was not modelled at all, so a
  cell could look profitable by holding for days.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE = (Path(__file__).resolve().parents[1]
           / "tools" / "research" / "profitability_backtest.py")


def _load():
    spec = importlib.util.spec_from_file_location("profitability_backtest", _MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the module
    # has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pb = _load()

T0 = 1_700_000_000  # second-scale epoch, 1-minute bars


def _rows(prices, start=T0):
    """Flat OHLC bars from a close-price path (high/low nudged off the close)."""
    rows = []
    for i, px in enumerate(prices):
        rows.append([start + i * 60, px, px * 1.0005, px * 0.9995, px, 10.0])
    return rows


def _halt_data():
    """Pump, drift down until the (tight) drawdown halt fires, then moon.

    The rally after the halt is the trap: marking the halted position at the
    final bar would book a large fake win.
    """
    prices = [100.0] * 5            # flat run-up
    prices += [102.0]               # +2% over 2 scans -> entry signal
    prices += [100.5, 99.8, 99.4]   # drift down: equity DD trips the halt here
    prices += [150.0, 220.0, 300.0]         # post-halt rally (must be ignored)
    prices += [300.0] * 300                 # ...for five hours
    return {"ALT": pb.Bars("ALT", _rows(prices))}


def test_halted_leftover_is_marked_at_the_halt_bar_not_the_window_end():
    data = _halt_data()
    params = pb.Params(min_observed_move_pct=1.5, lookback_scans=2,
                       stop_loss_pct=3.0, trail_activation_pct=1e9,  # no trail
                       max_drawdown_pct=0.3, halt_on_max_drawdown=True)
    res = pb.Engine(data, params, label="halt").run()

    assert res["halted"] is True, "the fixture must actually trip the DD halt"
    halted_trades = [t for t in res["trades_detail"] if t["reason"] == "Halted open"]
    assert len(halted_trades) == 1, res["exits"]

    trade = halted_trades[0]
    last_bar_ts = data["ALT"].t[-1]
    last_bar_close = data["ALT"].c[-1]
    # marked where the loop stopped, not 4 minutes (and +200%) later
    assert trade["exit_ts"] < last_bar_ts
    assert trade["exit"] < 110.0, trade
    assert last_bar_close == pytest.approx(300.0)
    # the exit price is the halt-bar close minus the modelled slippage
    halt_close = data["ALT"].c[data["ALT"].t.index(trade["exit_ts"])]
    assert trade["exit"] == pytest.approx(halt_close * (1 - params.slippage_pct_per_side / 100),
                                          rel=1e-6)
    # and the run only claims the days it actually traded
    assert res["traded_days"] < res["window_days"]


def test_same_path_without_the_halt_marks_out_at_the_window_end():
    data = _halt_data()
    params = pb.Params(min_observed_move_pct=1.5, lookback_scans=2,
                       stop_loss_pct=3.0, trail_activation_pct=1e9,
                       halt_on_max_drawdown=False)
    res = pb.Engine(data, params, label="no-halt").run()

    assert res["halted"] is False
    open_trades = [t for t in res["trades_detail"] if t["reason"] == "Open at end"]
    assert len(open_trades) == 1
    assert open_trades[0]["exit_ts"] == data["ALT"].t[-1]
    assert open_trades[0]["exit"] > 200.0   # the rally is real when we hold


def test_time_stop_caps_the_hold_and_is_reported_as_its_own_exit():
    prices = [100.0] * 5 + [102.0] + [102.0] * 200   # pump, then nothing happens
    data = {"ALT": pb.Bars("ALT", _rows(prices))}
    params = pb.Params(min_observed_move_pct=1.5, lookback_scans=2,
                       stop_loss_pct=25.0,           # never reached
                       trail_activation_pct=1e9,     # never arms
                       max_hold_minutes=120.0,
                       halt_on_max_drawdown=False)
    res = pb.Engine(data, params, label="time-stop").run()

    assert res["trades"] == 1
    assert res["max_hold_minutes"] == 120.0
    assert res["exits"].get("Time Stop") == 1, res["exits"]
    assert res["avg_hold_min"] == pytest.approx(120.0, abs=1.0)
    assert res["trades_detail"][0]["hold_sec"] == pytest.approx(120 * 60, abs=60)


def test_without_the_time_stop_the_same_trade_still_open():
    prices = [100.0] * 5 + [102.0] + [102.0] * 200
    data = {"ALT": pb.Bars("ALT", _rows(prices))}
    params = pb.Params(min_observed_move_pct=1.5, lookback_scans=2,
                       stop_loss_pct=25.0, trail_activation_pct=1e9,
                       max_hold_minutes=0.0, halt_on_max_drawdown=False)
    res = pb.Engine(data, params, label="no-time-stop").run()

    assert res["exits"].get("Time Stop") is None
    assert res["exits"].get("Open at end") == 1
    assert res["avg_hold_min"] > 120.0


def test_time_stop_does_not_preempt_a_stop_that_was_hit_intrabar():
    """The exchange stop order sits on the book all bar, so it fills first."""
    prices = [100.0] * 5 + [102.0] + [101.0] * 119 + [90.0]   # crash on the last bar
    rows = _rows(prices)
    rows[-1][3] = 88.0        # low well through the stop
    data = {"ALT": pb.Bars("ALT", rows)}
    params = pb.Params(min_observed_move_pct=1.5, lookback_scans=2,
                       stop_loss_pct=3.0, trail_activation_pct=1e9,
                       max_hold_minutes=120.0, halt_on_max_drawdown=False)
    res = pb.Engine(data, params, label="stop-vs-time").run()

    assert res["trades"] == 1
    reasons = set(res["exits"])
    assert reasons == {"Stop Loss"}, res["exits"]
