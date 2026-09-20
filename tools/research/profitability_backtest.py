#!/usr/bin/env python3
"""Replay NobitexAgentWin's live rule set on public market data.

The engine mirrors the shipped defaults in data/bot_config.json and the code in
signal_tracker.py / trading/nobitex_momentum_engine.py:

  entry   : price >= +min_observed_move_pct over `movement_lookback_scans` scans,
            24h change <= max_local_24h_pct, no BTC dump, spread <= max_spread_pct
  exits   : hard stop at stop_loss_pct, trailing stop armed at
            trailing_activation_pct and trailing_distance_pct behind the extreme,
            take_profit_percent = 0 (disabled)
  costs   : taker fee on both sides + half-spread on both sides + slippage
  sizing  : risk_per_trade_pct of equity / stop distance, capped by
            max_position_pct, max_notional_quote and max_total_exposure_pct

Everything is parameterised so the same file can also answer "would different
settings help?" and "does the entry signal carry any edge at all?" (forward
return study, random-entry control).
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import os
import random
import statistics
import sys
from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional


# ── live defaults (data/bot_config.json, 2026-09) ────────────────────────────

@dataclass
class Params:
    scan_seconds: int = 10
    lookback_scans: int = 4
    min_observed_move_pct: float = 1.5
    max_local_24h_pct: float = 12.0
    max_local_fall_pct: float = 0.5
    stop_loss_pct: float = 3.0
    trail_activation_pct: float = 1.5
    trail_distance_pct: float = 1.2
    take_profit_pct: float = 0.0
    fee_pct_per_side: float = 0.25
    half_spread_pct: float = 0.15
    slippage_pct_per_side: float = 0.05
    risk_per_trade_pct: float = 0.5
    max_open_positions: int = 2
    max_total_exposure_pct: float = 30.0
    max_position_pct: float = 20.0
    max_notional_quote: float = 3_000_000.0
    min_notional_quote: float = 500_000.0
    equity: float = 10_000_000.0
    cooldown_after_loss_min: int = 25
    cooldown_after_win_min: int = 5
    entry_cooldown_sec: int = 90
    start_equity: Optional[float] = None   # internal: equity after drawdown halts
    max_drawdown_pct: float = 12.0
    halt_on_max_drawdown: bool = True
    intrabar_stops: bool = True            # real mode places stop_market on the book
    random_entries: bool = False
    random_seed: int = 11
    random_rate: float = 0.0               # per symbol-scan entry probability
    btc_dump_guard: bool = True
    btc_dump_pct: float = 3.0

    def scaled(self, **kw) -> "Params":
        return replace(self, **kw)


BALANCED_PRESET = Params(  # what auto_regime_strategy writes on top of the file
    lookback_scans=6, min_observed_move_pct=0.8, max_open_positions=10,
    risk_per_trade_pct=0.75, max_total_exposure_pct=90.0, max_position_pct=25.0,
    max_notional_quote=5_000_000.0, trail_activation_pct=3.0, trail_distance_pct=2.0,
    cooldown_after_loss_min=60, cooldown_after_win_min=15, entry_cooldown_sec=180,
    max_drawdown_pct=10.0,
)

CONSERVATIVE_PRESET = Params(
    lookback_scans=6, min_observed_move_pct=1.5, max_open_positions=4,
    risk_per_trade_pct=0.5, max_total_exposure_pct=30.0, max_position_pct=20.0,
    stop_loss_pct=2.0, trail_activation_pct=1.5, trail_distance_pct=1.0,
    take_profit_pct=5.0,
)


# ── data ─────────────────────────────────────────────────────────────────────

def _norm_ts(value) -> int:
    """Public archives mix second, millisecond and microsecond epochs."""
    ts = int(float(value))
    while ts > 100_000_000_000:
        ts //= 1000
    return ts


class Bars:
    __slots__ = ("symbol", "t", "o", "h", "l", "c", "v")

    def __init__(self, symbol: str, rows: List[List[float]]):
        rows = sorted(rows, key=lambda r: float(r[0]))
        self.symbol = symbol
        self.t = [_norm_ts(r[0]) for r in rows]
        self.o = [float(r[1]) for r in rows]
        self.h = [float(r[2]) for r in rows]
        self.l = [float(r[3]) for r in rows]
        self.c = [float(r[4]) for r in rows]
        self.v = [float(r[5]) for r in rows]

    def __len__(self) -> int:
        return len(self.t)

    def step(self) -> float:
        if len(self.t) < 2:
            return 60.0
        diffs = [self.t[i + 1] - self.t[i] for i in range(min(50, len(self.t) - 1))]
        return float(statistics.median(diffs))


def load_bars(data_dir: str, kind: str) -> Dict[str, Bars]:
    out: Dict[str, Bars] = {}
    for name in sorted(os.listdir(data_dir)):
        if not name.endswith(".csv.gz"):
            continue
        if kind == "10s" and not name.startswith("bars_10s_"):
            continue
        if kind == "1m" and not name.startswith("ohlc_1m_"):
            continue
        symbol = name.rsplit("_", 1)[-1].replace(".csv.gz", "")
        rows = [r for r in csv.reader(gzip.open(os.path.join(data_dir, name), "rt")) if len(r) >= 6]
        if rows:
            out[symbol] = Bars(symbol, rows)
            print(f"  loaded {symbol}: {len(out[symbol])} bars ({kind})", flush=True)
    return out


# ── engine ───────────────────────────────────────────────────────────────────

@dataclass
class Position:
    symbol: str
    entry_ts: int
    entry_price: float
    size: float
    notional: float
    stop: float
    initial_stop: float
    extreme: float
    fees: float = 0.0
    entry_fee: float = 0.0


class Engine:
    def __init__(self, data: Dict[str, Bars], params: Params, label: str = ""):
        self.data = data
        self.p = params
        self.label = label
        self.rng = random.Random(params.random_seed)

    def run(self) -> dict:
        p = self.p
        cash = p.equity
        equity = p.equity
        peak = equity
        positions: Dict[str, Position] = {}
        cooldowns: Dict[str, int] = {}
        history: Dict[str, List[float]] = {s: [] for s in self.data}
        prices_24h: Dict[str, List] = {s: [] for s in self.data}
        trades: List[dict] = []
        equity_curve: List[tuple] = []
        signals = 0
        blocked = {"max_open": 0, "exposure": 0, "cooldown": 0, "extended": 0,
                   "duplicate": 0, "min_notional": 0, "halted": 0, "no_cash": 0}
        halted = False
        min_notional = max(0.0, p.min_notional_quote)

        # merge all scan timestamps of all symbols
        index = {s: {t: i for i, t in enumerate(b.t)} for s, b in self.data.items()}
        timeline = sorted({t for b in self.data.values() for t in b.t})
        step_s = {s: max(b.step(), 1e-9) for s, b in self.data.items()}
        for s, st in step_s.items():
            if not (0.5 <= st <= 3600.0):
                print(f"  !! WARNING {s}: bar step {st}s looks wrong - timestamps "
                      f"may be mis-scaled", flush=True)
        window_24h = {s: max(2, int(round(86400.0 / st))) for s, st in step_s.items()}

        def btc_dump_blocked(ts: int) -> bool:
            if not p.btc_dump_guard or "BTC" not in self.data:
                return False
            b = self.data["BTC"]
            i = index["BTC"].get(ts)
            if i is None:
                return False
            if i > 0:
                move = (b.c[i] - b.c[i - 1]) / b.c[i - 1] * 100.0
                if move <= -p.btc_dump_pct:
                    return True
            st = step_s.get("BTC", 60.0)
            j = max(0, i - max(1, int(round(240.0 / st))))   # 4 minutes of bars
            if b.c[j] > 0:
                move = (b.c[i] - b.c[j]) / b.c[j] * 100.0
                if move <= -p.btc_dump_pct:
                    return True
            return False

        for ts in timeline:
            if halted:
                break
            dumped = btc_dump_blocked(ts)
            for symbol, bars in self.data.items():
                i = index[symbol].get(ts)
                if i is None:
                    continue
                price = bars.c[i]
                if price <= 0:
                    continue

                # ── exits first (mirrors _update_open_trades) ──
                pos = positions.get(symbol)
                if pos is not None:
                    # The exchange stop order in place during this bar is the
                    # level computed at the *previous* scan, exactly as the live
                    # loop leaves it (see _update_open_trades / _evaluate_trade).
                    exit_price: Optional[float] = None
                    reason = ""
                    if p.take_profit_pct > 0:
                        tp = pos.entry_price * (1 + p.take_profit_pct / 100.0)
                        if (p.intrabar_stops and bars.h[i] >= tp) or price >= tp:
                            exit_price, reason = max(tp, bars.o[i]) if p.intrabar_stops else tp, \
                                "Take Profit"
                    if exit_price is None:
                        if p.intrabar_stops:
                            if bars.l[i] <= pos.stop:
                                # a gap through the stop fills worse than the level
                                exit_price = min(pos.stop, bars.o[i])
                        elif price <= pos.stop:
                            exit_price = pos.stop
                        if exit_price is not None:
                            reason = ("Stop Loss" if pos.stop == pos.initial_stop
                                      else "Trailing Stop")

                    if exit_price is None:
                        # trail ratchet on the scan sample, as the live loop does
                        pos.extreme = max(pos.extreme, price)
                        profit = (price - pos.entry_price) / pos.entry_price * 100.0
                        if profit > 0 and profit >= p.trail_activation_pct:
                            trail = max(pos.extreme * (1 - p.trail_distance_pct / 100.0),
                                        pos.entry_price)
                            pos.stop = max(pos.stop, trail)
                    else:
                        fill = exit_price * (1 - p.slippage_pct_per_side / 100.0)
                        proceeds = pos.size * fill * (1 - p.fee_pct_per_side / 100.0)
                        exit_fee = pos.size * fill * p.fee_pct_per_side / 100.0
                        cash += proceeds
                        net_pnl = proceeds - pos.notional - pos.entry_fee
                        gross_pct = (fill - pos.entry_price) / pos.entry_price * 100.0
                        cost_pct = (pos.entry_fee + exit_fee) / pos.notional * 100.0
                        trades.append({
                            "symbol": symbol, "entry_ts": pos.entry_ts, "exit_ts": ts,
                            "hold_sec": ts - pos.entry_ts, "reason": reason,
                            "entry": pos.entry_price, "exit": fill,
                            "gross_pct": round(gross_pct, 4),
                            "net_pct": round(gross_pct - cost_pct, 4),
                            "net_amount": round(net_pnl, 2),
                            "notional": round(pos.notional, 2),
                        })
                        if net_pnl >= 0:
                            cooldowns[symbol] = ts + p.cooldown_after_win_min * 60
                        else:
                            cooldowns[symbol] = ts + p.cooldown_after_loss_min * 60
                        del positions[symbol]

                # ── entry logic (mirrors evaluate() + _check_5_percent_pump_entry) ──
                hist = history[symbol]
                observed = None
                recent = None
                if len(hist) >= 1:
                    base = hist[-p.lookback_scans] if len(hist) >= p.lookback_scans else hist[0]
                    observed = (price - base) / base * 100.0 if base > 0 else None
                    recent = (price - hist[-1]) / hist[-1] * 100.0 if hist[-1] > 0 else None
                hist.append(price)
                if len(hist) > 600:
                    del hist[0]

                win = window_24h[symbol]
                prices_24h[symbol].append(price)
                if len(prices_24h[symbol]) > win + 2:
                    del prices_24h[symbol][0]
                change_24h = 0.0
                if len(prices_24h[symbol]) > 1:
                    base24 = prices_24h[symbol][0]
                    if base24 > 0:
                        change_24h = (price - base24) / base24 * 100.0

                if symbol in positions:
                    blocked["duplicate"] += 1
                    continue
                if ts < cooldowns.get(symbol, 0):
                    blocked["cooldown"] += 1
                    continue
                if len(positions) >= p.max_open_positions:
                    blocked["max_open"] += 1
                    continue

                if p.random_entries:
                    if p.random_rate <= 0 or self.rng.random() >= p.random_rate:
                        continue
                    trigger = True
                else:
                    if dumped:
                        blocked["extended"] += 1
                        continue
                    if observed is None or observed < p.min_observed_move_pct:
                        continue
                    if recent is not None and recent < -p.max_local_fall_pct:
                        blocked["extended"] += 1
                        continue
                    if p.max_local_24h_pct > 0 and change_24h > p.max_local_24h_pct:
                        blocked["extended"] += 1
                        continue
                    trigger = True

                if trigger:
                    signals += 1
                    entry_price = price * (1 + p.half_spread_pct / 100.0)
                    entry_price *= (1 + p.slippage_pct_per_side / 100.0)
                    risk_amount = equity * p.risk_per_trade_pct / 100.0
                    notional = risk_amount / (p.stop_loss_pct / 100.0)
                    notional = min(notional, equity * p.max_position_pct / 100.0)
                    notional = min(notional, p.max_notional_quote)
                    open_notional = sum(x.notional for x in positions.values())
                    exposure_cap = equity * p.max_total_exposure_pct / 100.0
                    if open_notional + notional > exposure_cap:
                        notional = exposure_cap - open_notional
                        if notional < min_notional:
                            blocked["exposure"] += 1
                            continue
                    if notional > cash * 0.995:
                        notional = cash * 0.995
                        if notional < min_notional:
                            blocked["no_cash"] += 1
                            continue
                    size = notional / entry_price
                    entry_fee = notional * p.fee_pct_per_side / 100.0
                    cash -= (notional + entry_fee)
                    positions[symbol] = Position(
                        symbol=symbol, entry_ts=ts, entry_price=entry_price, size=size,
                        notional=notional, stop=entry_price * (1 - p.stop_loss_pct / 100.0),
                        initial_stop=entry_price * (1 - p.stop_loss_pct / 100.0),
                        extreme=entry_price, entry_fee=entry_fee)
                    cooldowns[symbol] = ts + p.entry_cooldown_sec

            # ── mark to market & drawdown halt ──
            unreal = 0.0
            for sym, pos in positions.items():
                b = self.data[sym]
                idx = index[sym].get(ts)
                mark = b.c[idx] if idx is not None else pos.entry_price
                unreal += pos.size * mark
            equity = cash + unreal
            peak = max(peak, equity)
            equity_curve.append((ts, equity))
            if (p.halt_on_max_drawdown
                    and (peak - equity) / peak * 100.0 >= p.max_drawdown_pct):
                halted = True
                blocked["halted"] = 1
                equity_curve.append((ts, equity))
                break

        # close leftovers at the last known price (mark-out)
        open_left = 0
        for sym, pos in list(positions.items()):
            b = self.data[sym]
            fill = b.c[-1] * (1 - p.slippage_pct_per_side / 100.0)
            proceeds = pos.size * fill * (1 - p.fee_pct_per_side / 100.0)
            exit_fee = pos.size * fill * p.fee_pct_per_side / 100.0
            cash += proceeds
            net_pnl = proceeds - pos.notional - pos.entry_fee
            gross_pct = (fill - pos.entry_price) / pos.entry_price * 100.0
            cost_pct = (pos.entry_fee + exit_fee) / pos.notional * 100.0
            trades.append({
                "symbol": sym, "entry_ts": pos.entry_ts, "exit_ts": b.t[-1],
                "hold_sec": b.t[-1] - pos.entry_ts, "reason": "Open at end",
                "entry": pos.entry_price, "exit": fill,
                "gross_pct": round(gross_pct, 4), "net_pct": round(gross_pct - cost_pct, 4),
                "net_amount": round(net_pnl, 2), "notional": round(pos.notional, 2),
            })
            open_left += 1
            del positions[sym]
        equity = cash

        wins = [t for t in trades if t["net_amount"] > 0]
        losses = [t for t in trades if t["net_amount"] <= 0]
        gross_profit = sum(t["net_amount"] for t in wins)
        gross_loss = -sum(t["net_amount"] for t in losses)
        expectancy = statistics.fmean([t["net_pct"] for t in trades]) if trades else 0.0
        dd = 0.0
        pk = p.equity
        for _, eq in equity_curve:
            pk = max(pk, eq)
            dd = max(dd, (pk - eq) / pk * 100.0)
        days = ((timeline[-1] - timeline[0]) / 86400.0) if len(timeline) > 1 else 0.0
        return {
            "label": self.label,
            "window_days": round(days, 2),
            "trades": len(trades),
            "trades_per_day": round(len(trades) / days, 2) if days else 0.0,
            "signals": signals,
            "win_rate_pct": round(100.0 * len(wins) / len(trades), 2) if trades else None,
            "avg_win_pct": round(statistics.fmean([t["net_pct"] for t in wins]), 4) if wins else None,
            "avg_loss_pct": round(statistics.fmean([t["net_pct"] for t in losses]), 4) if losses else None,
            "expectancy_pct": round(expectancy, 4),
            "expectancy_quote": round(statistics.fmean([t["net_amount"] for t in trades]), 2) if trades else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
            "net_pnl_quote": round(sum(t["net_amount"] for t in trades), 2),
            "return_pct": round((equity - p.equity) / p.equity * 100.0, 3),
            "final_equity": round(equity, 2),
            "max_drawdown_pct": round(dd, 3),
            "costs_quote": round(sum(t["notional"] * (p.fee_pct_per_side * 2 + p.half_spread_pct * 2
                                                      + p.slippage_pct_per_side * 2) / 100.0
                                     for t in trades), 2),
            "exits": {r: sum(1 for t in trades if t["reason"] == r)
                      for r in sorted({t["reason"] for t in trades})},
            "blocked": blocked,
            "halted": halted,
            "open_at_end": open_left,
            "avg_hold_min": round(statistics.fmean([t["hold_sec"] for t in trades]) / 60.0, 1)
            if trades else None,
            "trades_detail": trades[:400],
        }


def buy_and_hold(data: Dict[str, Bars]) -> Dict[str, dict]:
    out = {}
    for sym, b in data.items():
        if len(b) < 2 or b.c[0] <= 0:
            continue
        ret = (b.c[-1] - b.c[0]) / b.c[0] * 100.0
        peak = b.c[0]
        dd = 0.0
        for x in b.c:
            peak = max(peak, x)
            dd = max(dd, (peak - x) / peak * 100.0)
        out[sym] = {"return_pct": round(ret, 2), "max_drawdown_pct": round(dd, 2)}
    return out


def forward_study(data: Dict[str, Bars], p: Params, horizons_min=(1, 5, 15, 30, 60)) -> dict:
    """Conditional forward returns after the entry signal, per symbol."""
    rows = []
    for sym, b in data.items():
        step = max(b.step(), 1e-9)
        look = max(1, int(round(p.lookback_scans)))
        for i in range(look, len(b) - 2):
            base = b.c[i - look]
            if base <= 0:
                continue
            move = (b.c[i] - base) / base * 100.0
            if move < p.min_observed_move_pct:
                continue
            entry = b.c[i]
            row = {"symbol": sym, "ts": b.t[i], "move_pct": round(move, 3)}
            ok = True
            for h in horizons_min:
                j = i + max(1, int(round(h * 60.0 / step)))
                if j >= len(b):
                    ok = False
                    break
                seg_hi = max(b.h[i + 1:j + 1]) if j > i else entry
                seg_lo = min(b.l[i + 1:j + 1]) if j > i else entry
                row[f"ret_{h}m"] = round((b.c[j] - entry) / entry * 100.0, 4)
                row[f"mfe_{h}m"] = round((seg_hi - entry) / entry * 100.0, 4)
                row[f"mae_{h}m"] = round((seg_lo - entry) / entry * 100.0, 4)
            if ok:
                rows.append(row)
    summary = {"events": len(rows)}
    for h in horizons_min:
        rets = [r[f"ret_{h}m"] for r in rows]
        if not rets:
            continue
        summary[f"h{h}m"] = {
            "mean_pct": round(statistics.fmean(rets), 4),
            "median_pct": round(statistics.median(rets), 4),
            "pct_positive": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 2),
            "mean_mfe_pct": round(statistics.fmean([r[f"mfe_{h}m"] for r in rows]), 4),
            "mean_mae_pct": round(statistics.fmean([r[f"mae_{h}m"] for r in rows]), 4),
            "prob_mfe_ge_stop_before_mae": round(100.0 * sum(
                1 for r in rows if r[f"mfe_{h}m"] >= p.trail_activation_pct
                and abs(r[f"mae_{h}m"]) < p.stop_loss_pct) / len(rows), 2),
        }
    summary["sample"] = rows[:60]
    return summary


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="research_data")
    ap.add_argument("--bars", default="10s", choices=["10s", "1m"])
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--sweep-cells", default="fast", choices=["fast", "full"])
    ap.add_argument("--symbols", default="", help="comma list filter, e.g. BTC,ETH")
    args = ap.parse_args()

    print(f"== loading {args.bars} bars from {args.data_dir}", flush=True)
    data = load_bars(args.data_dir, args.bars)
    if args.symbols:
        keep = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        data = {k: v for k, v in data.items() if k in keep}
        print(f"  filtered to {sorted(data)}", flush=True)
    if not data:
        print("no data", file=sys.stderr)
        return 2

    base = Params()
    report: dict = {"bars": args.bars, "symbols": sorted(data),
                    "bars_loaded": {k: len(v) for k, v in data.items()}}

    arms = {
        "file_defaults": base,
        "balanced_preset": BALANCED_PRESET,
        "conservative_preset": CONSERVATIVE_PRESET,
        "file_defaults_zero_cost": base.scaled(fee_pct_per_side=0.0, half_spread_pct=0.0,
                                               slippage_pct_per_side=0.0),
        "balanced_preset_zero_cost": BALANCED_PRESET.scaled(
            fee_pct_per_side=0.0, half_spread_pct=0.0, slippage_pct_per_side=0.0),
        "balanced_preset_paper_stops": BALANCED_PRESET.scaled(intrabar_stops=False),
        # no trailing stop at all: exits are the hard stop (or the end of the
        # window).  This isolates "does the signal have drift?" from "does the
        # exit geometry pay for the round trip?".
        "file_defaults_no_trailing": base.scaled(trail_activation_pct=1e9),
        "balanced_preset_no_trailing": BALANCED_PRESET.scaled(trail_activation_pct=1e9),
    }
    for name, params in arms.items():
        res = Engine(data, params, label=name).run()
        report[name] = res
        print(f"\n== {name}: trades={res['trades']} win={res['win_rate_pct']} "
              f"expectancy={res['expectancy_pct']}% return={res['return_pct']}% "
              f"dd={res['max_drawdown_pct']}% pf={res['profit_factor']} "
              f"halted={res['halted']}", flush=True)

    # random-entry control with matched trade count
    ref = report["file_defaults"]
    days = max(ref["window_days"], 1e-9)
    scans = int(round(days * 86400 / base.scan_seconds)) * max(1, len(data))
    rate = min(0.5, max(1e-7, (ref["trades"] or 1) / max(scans, 1) * 3.0))
    ctrl = Engine(data, base.scaled(random_entries=True, random_rate=rate),
                  label="random_entries").run()
    report["random_entries"] = ctrl
    print(f"\n== random_entries (rate={rate:.3e}, target~{ref['trades']}): "
          f"trades={ctrl['trades']} "
          f"win={ctrl['win_rate_pct']} expectancy={ctrl['expectancy_pct']}% "
          f"return={ctrl['return_pct']}%", flush=True)

    report["buy_and_hold"] = buy_and_hold(data)
    print(f"\n== buy & hold: {report['buy_and_hold']}", flush=True)

    report["forward_study"] = forward_study(data, base)
    fs = report["forward_study"]
    print(f"\n== forward study: events={fs['events']}")
    for key, val in fs.items():
        if key.startswith("h"):
            print(f"   {key}: {val}")

    if args.sweep:
        # The trailing stop only does something when activation > distance: with
        # activation == distance the armed level is floored at the entry price, so
        # every trailed exit lands on ~-cost, and with activation >> distance the
        # trail never arms at all.  Sweep the pairs the live presets actually use.
        trail_pairs = ((1.5, 1.2), (3.0, 2.0), (1.5, 0.6), (3.0, 1.0))
        if args.sweep_cells == "fast":
            thresholds, lookbacks, stops = (0.8, 1.5), (4, 6), (3.0,)
        else:
            thresholds, lookbacks, stops = (0.5, 0.8, 1.5, 2.5), (2, 4, 6), (2.0, 3.0)
        grid = []
        for threshold in thresholds:
            for lookback in lookbacks:
                for act, dist in trail_pairs:
                    for stop in stops:
                        params = base.scaled(min_observed_move_pct=threshold,
                                             lookback_scans=lookback,
                                             trail_activation_pct=act,
                                             trail_distance_pct=dist,
                                             stop_loss_pct=stop)
                        res = Engine(data, params,
                                     label=f"T{threshold}/L{lookback}/{act}-{dist}/stop{stop}").run()
                        grid.append({"threshold": threshold, "lookback": lookback,
                                     "trail_activation": act, "trail_distance": dist,
                                     "stop": stop,
                                     "trades": res["trades"], "win_rate": res["win_rate_pct"],
                                     "expectancy_pct": res["expectancy_pct"],
                                     "return_pct": res["return_pct"],
                                     "max_dd_pct": res["max_drawdown_pct"],
                                     "halted": res["halted"],
                                     "avg_hold_min": res["avg_hold_min"],
                                     "exits": res["exits"]})
        report["sweep"] = grid
        best = sorted(grid, key=lambda r: -(r["expectancy_pct"] or -999))[:5]
        print("\n== sweep best by expectancy:")
        for row in best:
            print("   " + ", ".join(f"{k}={v}" for k, v in row.items()
                                    if k not in ("exits", "halted")))
        positive = [r for r in grid if (r["trades"] or 0) >= 20
                    and (r["expectancy_pct"] or 0) > 0]
        report["sweep_positive_cells"] = positive
        print(f"   positive cells (>=20 trades): {len(positive)}/{len(grid)}")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(report, fh, indent=1, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
