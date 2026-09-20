"""Profitability study for the Nobitex momentum bot (CryptoScanner 6.9 / v7).

The engine replays the *live* rule set from `data/bot_config.json`:

  ENTRY (every scan = check_interval_seconds, default 10 s)
    observed move over `movement_lookback_scans` scans >= min_observed_move_pct
    (the GUI mirrors min_observed_move_pct into the tracker pump gate for live)
    chase cap, 24 h change cap, BTC dump guard, per-asset cooldowns,
    max_open_trades, max_new_entries_per_cycle, exposure cap, risk sizing.

  EXIT (same scan cadence)
    hard stop_loss_pct, trailing activation + distance, take-profit disabled,
    exits only via stop / trailing stop (reverse-signal exits never fire,
    because the momentum flow only produces BUY-tagged rows).

Costs are explicit: taker fee per side + half-spread per side + slippage.
Bar data is 10 s (preferred, matches the live scan cadence) or 1 m (fallback).

Usage
-----
    python tools/research/profitability_backtest.py --data-dir research_data \
        --bars 10s --sweep --json-out research_data/results_10s.json
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import random
import statistics
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(ROOT, "data", "bot_config.json")


# ────────────────────────────────── config ───────────────────────────────────

@dataclass
class Params:
    """Live defaults from data/bot_config.json (see README risk snapshot)."""
    account_balance: float = 10_000_000.0     # IRT
    scan_interval_sec: float = 10.0
    lookback_scans: int = 4
    min_observed_move_pct: float = 1.5
    max_chase_pct: float = 0.8
    max_spread_pct: float = 0.9               # gate on the live book
    half_spread_pct: float = 0.15             # what a market order actually pays
    slippage_pct: float = 0.05                # per side, market/stop orders
    fee_pct: float = 0.25                     # taker fee per side
    stop_loss_pct: float = 3.0
    trailing_activation_pct: float = 1.5
    trailing_distance_pct: float = 1.2
    take_profit_pct: float = 0.0              # 0 == disabled
    risk_per_trade_pct: float = 0.5
    max_open_positions: int = 2
    max_new_entries_per_cycle: int = 1
    max_position_pct: float = 20.0
    max_total_exposure_pct: float = 30.0
    min_notional_quote: float = 500_000.0     # IRT
    max_notional_quote: float = 3_000_000.0   # IRT
    cooldown_after_loss_min: float = 25.0
    cooldown_after_win_min: float = 5.0
    entry_cooldown_sec: float = 90.0
    max_local_24h_pct: float = 12.0
    btc_max_dump_pct: float = 3.0
    min_volume_24h_irt: float = 400_000_000.0
    usd_irt: float = 1_000_000.0              # only used for IRT notional caps
    require_spread_gate: bool = False         # gate needs a real book; modelled as cost
    random_entry: bool = False                # control arm
    random_seed: int = 7
    random_prob: float = 1e-4                   # entry probability per scan in the control arm
    # entry confirmation queue (STRATEGY_PRESETS -> confirmation_enabled)
    confirmation_enabled: bool = False
    confirmation_pct: float = 0.3
    confirmation_max_minutes: float = 4.0
    invalidation_pct: float = 1.2
    # drawdown circuit breaker (halt_on_max_drawdown)
    max_drawdown_percent: float = 12.0
    halt_on_max_drawdown: bool = True
    intrabar_stops: bool = True               # exchange-side stop_market order exists

    def scaled(self, **kw) -> "Params":
        return replace(self, **kw)


# The GUI applies the regime-mapped preset on top of bot_config.json on every
# scan when `auto_regime_strategy` is true (shipped default).  In BALANCED —
# the regime the detector holds most of the time — the effective live profile
# is therefore this one, not the raw file.
BALANCED_PRESET: Dict[str, Any] = {
    "max_open_positions": 10,
    "risk_per_trade_pct": 0.75,
    "max_drawdown_percent": 10.0,
    "pump_threshold_pct": 1.8,
    "movement_lookback_scans": 6,
    "stop_loss_pct": 3.0,
    "trailing_distance_pct": 2.0,
    "trailing_activation_pct": 3.0,
    "trailing_stop_enabled": True,
    "take_profit_percent": 50.0,
    "max_position_pct": 25.0,
    "max_notional_quote": 5_000_000.0,
    "max_total_exposure_pct": 90.0,
    "min_volume_24h": 500_000_000.0,
    "cooldown_after_loss_min": 60,
    "cooldown_after_win_min": 15,
    "entry_cooldown_seconds": 180,
    "max_new_entries_per_cycle": 1,
    "confirmation_enabled": True,
    "confirmation_pct": 0.4,
    "confirmation_max_minutes": 5,
    "invalidation_pct": 0.8,
    "max_chase_pct": 0.8,
    "min_observed_move_pct": 0.8,
    "btc_max_dump_pct": 2.0,
    "min_ask_depth_quote": 1_000_000.0,
    "btc_dump_exception_enabled": True,
    "min_quality": 0.4,
}


def apply_balanced_preset(p: Params) -> Params:
    """What the running GUI actually configures in the BALANCED regime."""
    q = replace(
        p,
        max_open_positions=10,
        risk_per_trade_pct=0.75,
        max_drawdown_percent=10.0,
        lookback_scans=6,
        min_observed_move_pct=0.8,
        stop_loss_pct=3.0,
        trailing_activation_pct=3.0,
        trailing_distance_pct=2.0,
        take_profit_pct=50.0,
        max_position_pct=25.0,
        max_notional_quote=5_000_000.0,
        max_total_exposure_pct=90.0,
        min_volume_24h_irt=500_000_000.0,
        cooldown_after_loss_min=60,
        cooldown_after_win_min=15,
        entry_cooldown_sec=180,
        max_new_entries_per_cycle=1,
        confirmation_enabled=True,
        confirmation_pct=0.4,
        confirmation_max_minutes=5.0,
        invalidation_pct=0.8,
        max_chase_pct=0.8,
        btc_max_dump_pct=2.0,
    )
    return q


def load_config(path: str = CONFIG_PATH) -> Params:
    p = Params()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return p
    mapping = {
        "account_balance": "account_balance",
        "check_interval_seconds": "scan_interval_sec",
        "movement_lookback_scans": "lookback_scans",
        "min_observed_move_pct": "min_observed_move_pct",
        "max_chase_pct": "max_chase_pct",
        "max_spread_pct": "max_spread_pct",
        "trading_fee_pct": "fee_pct",
        "stop_loss_pct": "stop_loss_pct",
        "trailing_activation_pct": "trailing_activation_pct",
        "trailing_distance_pct": "trailing_distance_pct",
        "take_profit_percent": "take_profit_pct",
        "risk_per_trade_pct": "risk_per_trade_pct",
        "max_open_positions": "max_open_positions",
        "max_new_entries_per_cycle": "max_new_entries_per_cycle",
        "max_position_pct": "max_position_pct",
        "max_total_exposure_pct": "max_total_exposure_pct",
        "min_notional_quote": "min_notional_quote",
        "max_notional_quote": "max_notional_quote",
        "cooldown_after_loss_min": "cooldown_after_loss_min",
        "cooldown_after_win_min": "cooldown_after_win_min",
        "entry_cooldown_seconds": "entry_cooldown_sec",
        "max_local_24h_pct": "max_local_24h_pct",
        "btc_max_dump_pct": "btc_max_dump_pct",
        "min_volume_irt": "min_volume_24h_irt",
        "confirmation_enabled": "confirmation_enabled",
        "confirmation_pct": "confirmation_pct",
        "confirmation_max_minutes": "confirmation_max_minutes",
        "invalidation_pct": "invalidation_pct",
        "max_drawdown_percent": "max_drawdown_percent",
        "halt_on_max_drawdown": "halt_on_max_drawdown",
    }
    for cfg_key, attr in mapping.items():
        if cfg_key in cfg and cfg[cfg_key] is not None:
            cur = getattr(p, attr)
            try:
                setattr(p, attr, type(cur)(cfg[cfg_key]))
            except Exception:
                pass
    return p


# ───────────────────────────────── data loading ──────────────────────────────

class Bars:
    """Sorted bar table for one symbol: t, o, h, l, c, v (unix seconds)."""

    __slots__ = ("symbol", "t", "o", "h", "l", "c", "v", "step")

    def __init__(self, symbol: str, rows: Sequence[Sequence[float]]):
        arr = np.asarray(rows, dtype=np.float64)
        order = np.argsort(arr[:, 0], kind="stable")
        arr = arr[order]
        # guard against millisecond/microsecond epochs from any data source:
        # in seconds a unix epoch is ~1.8e9, so anything above 1e11 is scaled
        if len(arr) > 2 and float(arr[0, 0]) > 1e11:
            while float(arr[0, 0]) > 1e11:
                arr[:, 0] /= 1000.0
        self.symbol = symbol
        self.t = arr[:, 0].astype(np.int64)
        self.o = arr[:, 1]
        self.h = arr[:, 2]
        self.l = arr[:, 3]
        self.c = arr[:, 4]
        self.v = arr[:, 5]
        self.step = float(np.median(np.diff(self.t))) if len(self.t) > 1 else 10.0

    def __len__(self) -> int:
        return len(self.t)

    def lookback_pct(self, idx: int, scans: int) -> Optional[float]:
        j = idx - scans
        if j < 0:
            return None
        base = self.c[j]
        if base <= 0:
            return None
        return (self.c[idx] - base) / base * 100.0

    def change_24h_pct(self, idx: int) -> float:
        window = int(round(86_400.0 / max(self.step, 1e-9)))
        j = max(0, idx - window)
        base = self.c[j]
        return (self.c[idx] - base) / base * 100.0 if base > 0 else 0.0

    def volume_24h(self, idx: int) -> float:
        window = int(round(86_400.0 / max(self.step, 1e-9)))
        j = max(0, idx - window)
        return float(np.sum(self.c[j:idx + 1] * self.v[j:idx + 1]))


def load_bars_file(path: str, symbol: str) -> Optional[Bars]:
    if not os.path.exists(path):
        return None
    rows: List[Sequence[float]] = []
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="", encoding="utf-8") as fh:
        header = fh.readline()
        cols = [c.strip() for c in header.split(",")]
        idx = {name: i for i, name in enumerate(cols)}
        need = ("t", "open", "high", "low", "close", "volume")
        if not all(k in idx for k in need):
            return None
        for line in fh:
            parts = line.rstrip("\n").split(",")
            if len(parts) < len(need):
                continue
            try:
                rows.append([float(parts[idx[k]]) for k in need])
            except ValueError:
                continue
    if len(rows) < 100:
        return None
    return Bars(symbol, rows)


def discover(data_dir: str, kind: str, symbols: Optional[Sequence[str]] = None) -> Dict[str, Bars]:
    out: Dict[str, Bars] = {}
    prefix = "bars_10s_" if kind == "10s" else "ohlc_1m_"
    for name in sorted(os.listdir(data_dir)):
        if not name.startswith(prefix) or not name.endswith(".csv.gz"):
            continue
        sym = name[len(prefix):-len(".csv.gz")].upper()
        if symbols and sym not in symbols:
            continue
        bars = load_bars_file(os.path.join(data_dir, name), sym)
        if bars is not None:
            out[sym] = bars
    return out


# ───────────────────────────────── engine ────────────────────────────────────

@dataclass
class Trade:
    symbol: str
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    notional: float
    net_pct: float
    gross_pct: float
    pnl_quote: float
    reason: str
    hold_scans: int
    mfe_pct: float
    mae_pct: float
    observed_pct: float


@dataclass
class SimResult:
    label: str
    symbols: List[str]
    bars_kind: str
    days: float
    start_equity: float
    end_equity: float
    trades: List[Trade] = field(default_factory=list)
    equity_curve: List[Tuple[int, float]] = field(default_factory=list)
    signals_seen: int = 0
    entries_blocked: Dict[str, int] = field(default_factory=dict)
    cost_paid: float = 0.0
    fee_paid: float = 0.0
    spread_paid: float = 0.0
    halted_at: Optional[int] = None
    pending_expired: int = 0
    pending_cancelled: int = 0

    # ── derived statistics ──
    def stats(self) -> Dict[str, Any]:
        n = len(self.trades)
        pnl = [t.pnl_quote for t in self.trades]
        nets = [t.net_pct for t in self.trades]
        wins = [x for x in nets if x > 0]
        losses = [x for x in nets if x <= 0]
        gross_win = sum(t.pnl_quote for t in self.trades if t.pnl_quote > 0)
        gross_loss = -sum(t.pnl_quote for t in self.trades if t.pnl_quote <= 0)

        eq = [e for _, e in self.equity_curve]
        peak = -1e30
        max_dd = 0.0
        for v in eq:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak * 100.0)

        assert self.days >= 0
        def _pct(vals: Sequence[float], q: float) -> Optional[float]:
            if not vals:
                return None
            s = sorted(vals)
            k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
            return round(s[k], 4)

        hold = [t.hold_scans * 10.0 / 60.0 for t in self.trades]  # minutes (10 s scans)
        reasons: Dict[str, int] = {}
        for t in self.trades:
            reasons[t.reason] = reasons.get(t.reason, 0) + 1

        return {
            "trades": n,
            "trades_per_day": round(n / self.days, 3) if self.days > 0 else None,
            "win_rate_pct": round(len(wins) / n * 100.0, 2) if n else None,
            "avg_win_pct": round(statistics.fmean(wins), 4) if wins else None,
            "avg_loss_pct": round(statistics.fmean(losses), 4) if losses else None,
            "median_pnl_pct": _pct(nets, 0.5),
            "expectancy_pct": round(statistics.fmean(nets), 4) if n else None,
            "expectancy_quote": round(statistics.fmean(pnl), 1) if n else None,
            "total_pnl_quote": round(sum(pnl), 1),
            "total_return_pct": round((self.end_equity / self.start_equity - 1) * 100.0, 3),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
            "max_drawdown_pct": round(max_dd, 3),
            "avg_hold_min": round(statistics.fmean(hold), 1) if hold else None,
            "median_hold_min": round(statistics.median(hold), 1) if hold else None,
            "mfe_p50": _pct([t.mfe_pct for t in self.trades], 0.5),
            "mae_p50": _pct([t.mae_pct for t in self.trades], 0.5),
            "exit_reasons": reasons,
            "cost_paid_quote": round(self.cost_paid, 1),
            "fee_paid_quote": round(self.fee_paid, 1),
            "spread_paid_quote": round(self.spread_paid, 1),
            "gross_before_costs_quote": round(sum(pnl) + self.cost_paid, 1),
            "signals_seen": self.signals_seen,
            "entries_blocked": dict(self.entries_blocked),
            "pending_expired": self.pending_expired,
            "pending_cancelled": self.pending_cancelled,
            "halted_at": self.halted_at,
            "halted_pct_of_window": (
                round((self.equity_curve[-1][0] - self.halted_at) /
                      max(1, self.equity_curve[-1][0] - self.equity_curve[0][0]) * 100.0, 1)
                if self.halted_at else 0.0
            ),
        }


def _half_spread(p: Params) -> float:
    """What a market order pays relative to the mid/last price, per side."""
    if p.require_spread_gate:
        return p.max_spread_pct / 2.0
    return p.half_spread_pct


class Engine:
    """Replay of the live momentum rule set across several symbols."""

    def __init__(self, data: Dict[str, Bars], params: Params, label: str = "",
                 bars_kind: str = "10s", usd_irt: Optional[float] = None):
        self.data = data
        self.p = params
        self.label = label
        self.bars_kind = bars_kind
        if usd_irt:
            self.p = replace(params, usd_irt=usd_irt)
        self.rng = random.Random(params.random_seed)

    # ── helpers ──
    def _dedupe_entry(self, ts: int) -> float:
        return 0.0

    def run(self) -> SimResult:
        p = self.p
        data = self.data
        if not data:
            raise SystemExit("no bars loaded")

        # global timeline: union of all scan timestamps
        all_t = np.unique(np.concatenate([b.t for b in data.values()]))
        steps = {sym: {int(t): i for i, t in enumerate(b.t)} for sym, b in data.items()}
        ptr = {sym: 0 for sym in data}
        sym_order = list(data.keys())

        cash = p.account_balance
        equity = p.account_balance
        peak_equity = p.account_balance
        open_pos: List[Dict[str, Any]] = []
        trades: List[Trade] = []
        curve: List[Tuple[int, float]] = []
        blocked: Dict[str, int] = {}
        signals = 0
        fee_paid = 0.0
        spread_paid = 0.0
        cooldown_until: Dict[str, float] = {}
        last_entry_ts: Dict[str, float] = {}
        price_hist: Dict[str, List[float]] = {s: [] for s in data}
        btc_series = data.get("BTC")
        pending_confirm: Dict[str, Dict[str, float]] = {}
        halted = False
        halted_at: Optional[int] = None
        self.expired = 0
        self.cancelled = 0

        def block(reason: str, n: int = 1) -> None:
            blocked[reason] = blocked.get(reason, 0) + n

        hs = _half_spread(p)
        slip = p.slippage_pct
        entry_cost_pct = hs + slip            # paid on top of last price
        exit_cost_pct = hs + slip             # received below level

        bars10 = (self.bars_kind == "10s")
        scan_step = p.scan_interval_sec if not bars10 else data[sym_order[0]].step
        prev_price: Dict[str, float] = {}

        for ts in all_t:
            # ── 1. advance per-symbol view ──
            present: Dict[str, int] = {}
            for sym in sym_order:
                b = data[sym]
                i = ptr[sym]
                if i < len(b) and b.t[i] == ts:
                    present[sym] = i
                    ptr[sym] = i + 1
                elif i < len(b) and b.t[i] < ts:
                    # carry forward last known price for sparse symbols
                    present[sym] = i

            # ── 2. exits (tracker updates open trades first) ──
            still_open: List[Dict[str, Any]] = []
            for pos in open_pos:
                sym = pos["symbol"]
                i = present.get(sym)
                if i is None:
                    still_open.append(pos)
                    continue
                b = data[sym]
                high, low, close = b.h[i], b.l[i], b.c[i]
                exit_price: Optional[float] = None
                reason = ""

                # trailing arm/ratchet uses the favourable extreme first
                if high > pos["extreme"]:
                    pos["extreme"] = high
                profit_pct = (pos["extreme"] - pos["entry"]) / pos["entry"] * 100.0
                if p.trailing_distance_pct > 0 and profit_pct >= p.trailing_activation_pct:
                    trail = pos["extreme"] * (1.0 - p.trailing_distance_pct / 100.0)
                    if trail > pos["stop"]:
                        pos["stop"] = trail
                if p.take_profit_pct > 0:
                    tp = pos["entry"] * (1.0 + p.take_profit_pct / 100.0)
                    if high >= tp:
                        exit_price, reason = tp, "Take Profit"
                if exit_price is None and low <= pos["stop"]:
                    # stop-market fill: level, or the bar open if it gapped through
                    fill = pos["stop"]
                    if b.o[i] < fill:
                        fill = b.o[i]
                    exit_price, reason = fill, ("Trailing Stop" if pos["stop"] > pos["initial_stop"] else "Stop Loss")
                if exit_price is None and pos["hard_deadline"] and ts >= pos["hard_deadline"]:
                    exit_price, reason = close, "Time Exit"

                if exit_price is None:
                    still_open.append(pos)
                    continue

                eff_exit = exit_price * (1.0 - exit_cost_pct / 100.0)
                qty = pos["qty"]
                gross = (eff_exit - pos["entry_fill"]) * qty
                exit_fee = eff_exit * qty * p.fee_pct / 100.0
                entry_fee = pos["entry_fill"] * qty * p.fee_pct / 100.0
                net = gross - exit_fee - entry_fee
                cash += qty * eff_exit - exit_fee
                fee_paid += exit_fee + entry_fee
                spread_paid += (pos["entry_fill"] - pos["ref_price"]) * qty + (exit_price - eff_exit) * qty
                trades.append(Trade(
                    symbol=sym, entry_time=int(pos["entry_ts"]), exit_time=int(ts),
                    entry_price=round(pos["entry_fill"], 8), exit_price=round(eff_exit, 8),
                    notional=round(pos["notional"], 2),
                    net_pct=round(net / pos["notional"] * 100.0, 4),
                    gross_pct=round(gross / pos["notional"] * 100.0, 4),
                    pnl_quote=round(net, 2), reason=reason,
                    hold_scans=int((ts - pos["entry_ts"]) / max(scan_step, 1e-9)),
                    mfe_pct=round((pos["extreme"] - pos["entry"]) / pos["entry"] * 100.0, 3),
                    mae_pct=round((pos["worst"] - pos["entry"]) / pos["entry"] * 100.0, 3),
                    observed_pct=round(pos["observed"], 3),
                ))
                if net >= 0:
                    cooldown_until[sym] = ts + p.cooldown_after_win_min * 60.0
                else:
                    cooldown_until[sym] = ts + p.cooldown_after_loss_min * 60.0
            open_pos = still_open

            # update worst excursion for surviving positions + mark to market
            for pos in open_pos:
                i = present.get(pos["symbol"])
                if i is None:
                    continue
                b = data[pos["symbol"]]
                pos["worst"] = min(pos["worst"], b.l[i])

            # ── 3. equity mark & curve (sampled) ──
            mtm = 0.0
            for pos in open_pos:
                i = present.get(pos["symbol"])
                px = data[pos["symbol"]].c[i] if i is not None else pos["entry"]
                mtm += pos["qty"] * px
            equity = cash + mtm
            peak_equity = max(peak_equity, equity)
            if len(curve) == 0 or ts - curve[-1][0] >= 60:
                curve.append((int(ts), round(equity, 2)))
            if (p.halt_on_max_drawdown and not halted and peak_equity > 0
                    and (1.0 - equity / peak_equity) * 100.0 >= p.max_drawdown_percent):
                halted = True
                halted_at = int(ts)

            # ── 4. entries (one per cycle max) ──
            if halted:
                block("halted_drawdown")
                continue
            if len(open_pos) >= p.max_open_positions:
                block("max_open")
                continue

            # BTC dump guard (engine-level)
            btc_dump = False
            if btc_series is not None and "BTC" in present:
                bi = present["BTC"]
                btc_move = btc_series.lookback_pct(bi, p.lookback_scans)
                btc_24h = btc_series.change_24h_pct(bi)
                btc_dump = min(btc_24h, btc_move if btc_move is not None else btc_24h) <= -p.btc_max_dump_pct

            candidates: List[Tuple[float, str, int, float]] = []
            for sym in sym_order:
                i = present.get(sym)
                if i is None:
                    continue
                b = data[sym]
                price = b.c[i]
                hist = price_hist[sym]
                # history is a rolling deque of the last 60 scans
                if not hist or hist[-1] != price:
                    hist.append(price)
                if len(hist) > 61:
                    del hist[:-61]

                if p.random_entry:
                    # control arm: same exit rules, random entry timing
                    if len(hist) > p.lookback_scans and self.rng.random() < p.random_prob:
                        observed = 0.0
                        candidates.append((observed, sym, i, price))
                    continue

                observed = b.lookback_pct(i, p.lookback_scans)
                if observed is None or observed < p.min_observed_move_pct:
                    continue
                if observed > 60.0:  # data glitch guard
                    continue
                signals += 1
                pv = prev_price.get(sym)
                if pv and pv > 0:
                    chase = (price - pv) / pv * 100.0
                    if chase > p.max_chase_pct:
                        block("chase")
                        continue
                if p.max_local_24h_pct > 0 and b.change_24h_pct(i) > p.max_local_24h_pct:
                    block("24h_move")
                    continue
                if btc_dump and sym != "BTC":
                    block("btc_dump")
                    continue
                if p.min_volume_24h_irt > 0:
                    vol_irt = b.volume_24h(i) * price * p.usd_irt  # proxy volumes are USD
                    if vol_irt < p.min_volume_24h_irt:
                        block("low_volume")
                        continue
                if ts < cooldown_until.get(sym, 0):
                    block("cooldown")
                    continue
                if ts - last_entry_ts.get(sym, -1e18) < p.entry_cooldown_sec:
                    block("entry_cooldown")
                    continue
                if any(pos["symbol"] == sym for pos in open_pos):
                    block("already_open")
                    continue
                candidates.append((observed, sym, i, price))

            for sym in sym_order:
                i = present.get(sym)
                if i is not None:
                    prev_price[sym] = data[sym].c[i]

            if not candidates:
                continue
            candidates.sort(key=lambda x: -x[0])

            for observed, sym, i, price in candidates[:p.max_new_entries_per_cycle]:
                if len(open_pos) >= p.max_open_positions:
                    block("max_open")
                    break
                b = data[sym]

                # tracker-level gates that still apply in the live path
                if ts < cooldown_until.get(sym, 0):
                    block("cooldown")
                    continue
                if any(pos["symbol"] == sym for pos in open_pos):
                    block("already_open")
                    continue

                # confirmation queue (pending → confirm → buy), live order of checks
                if p.confirmation_enabled:
                    pend = pending_confirm.get(sym)
                    if pend is None:
                        pending_confirm[sym] = {
                            "price": price,
                            "expires": ts + p.confirmation_max_minutes * 60.0,
                        }
                        block("pending_created")
                        continue
                    if ts >= pend["expires"]:
                        pending_confirm.pop(sym, None)
                        self.expired += 1
                        block("pending_expired")
                        continue
                    move = ((price - pend["price"]) / pend["price"] * 100.0
                            if pend["price"] > 0 else 0.0)
                    if move >= p.confirmation_pct and move <= p.max_chase_pct:
                        pending_confirm.pop(sym, None)      # confirmed → buy now
                    elif move < -abs(p.invalidation_pct):
                        pending_confirm.pop(sym, None)
                        self.cancelled += 1
                        block("pending_invalidated")
                        continue
                    else:
                        block("pending_wait")
                        continue

                stop = p.stop_loss_pct
                entry_fill = price * (1.0 + entry_cost_pct / 100.0)
                # risk sizing (risk_percent mode)
                risk_amount = equity * (p.risk_per_trade_pct / 100.0)
                notional = risk_amount / (stop / 100.0) if stop > 0 else 0.0
                notional = min(notional, cash * 0.90)
                notional = min(notional, equity * max(1.0, p.max_position_pct) / 100.0)
                if p.max_notional_quote > 0:
                    notional = min(notional, p.max_notional_quote)
                min_notional = p.min_notional_quote
                if min_notional > 0 and notional < min_notional:
                    block("min_notional")
                    continue
                exposure = sum(pos["qty"] * pos["entry_fill"] for pos in open_pos)
                cap = equity * p.max_total_exposure_pct / 100.0
                if exposure + notional > cap:
                    remaining = cap - exposure
                    if remaining <= 0:
                        block("exposure_cap")
                        continue
                    shrunk = remaining * 0.99
                    if min_notional > 0 and shrunk < min_notional:
                        block("exposure_cap_min")
                        continue
                    notional = shrunk
                qty = notional / entry_fill
                if qty <= 0:
                    block("zero_size")
                    continue

                entry_fee = entry_fill * qty * p.fee_pct / 100.0
                cash -= qty * entry_fill + entry_fee
                fee_paid += entry_fee
                spread_paid += (entry_fill - price) * qty
                open_pos.append({
                    "symbol": sym, "entry": price, "entry_fill": entry_fill,
                    "qty": qty, "notional": qty * entry_fill,
                    "stop": entry_fill * (1.0 - stop / 100.0),
                    "initial_stop": entry_fill * (1.0 - stop / 100.0),
                    "extreme": max(entry_fill, b.h[i]), "worst": min(entry_fill, b.l[i]),
                    "entry_ts": ts, "ref_price": price, "observed": observed,
                    "hard_deadline": None,
                })
                last_entry_ts[sym] = ts

        # ── close out at the end for accounting ──
        last_ts = int(all_t[-1])
        for pos in open_pos:
            sym = pos["symbol"]
            b = data[sym]
            px = b.c[-1]
            eff = px * (1.0 - exit_cost_pct / 100.0)
            qty = pos["qty"]
            gross = (eff - pos["entry_fill"]) * qty
            fees = (pos["entry_fill"] + eff) * qty * p.fee_pct / 100.0
            net = gross - fees
            cash += qty * eff - eff * qty * p.fee_pct / 100.0
            fee_paid += eff * qty * p.fee_pct / 100.0
            trades.append(Trade(
                symbol=sym, entry_time=int(pos["entry_ts"]), exit_time=last_ts,
                entry_price=round(pos["entry_fill"], 8), exit_price=round(eff, 8),
                notional=round(pos["notional"], 2),
                net_pct=round(net / pos["notional"] * 100.0, 4),
                gross_pct=round(gross / pos["notional"] * 100.0, 4),
                pnl_quote=round(net, 2), reason="Open at end",
                hold_scans=int((last_ts - pos["entry_ts"]) / max(scan_step, 1e-9)),
                mfe_pct=round((pos["extreme"] - pos["entry"]) / pos["entry"] * 100.0, 3),
                mae_pct=round((pos["worst"] - pos["entry"]) / pos["entry"] * 100.0, 3),
                observed_pct=round(pos["observed"], 3),
            ))
        final_equity = cash
        curve.append((last_ts, round(final_equity, 2)))
        span_days = (all_t[-1] - all_t[0]) / 86_400.0 if len(all_t) > 1 else 0.0
        return SimResult(
            label=self.label, symbols=sorted(data.keys()), bars_kind=self.bars_kind,
            days=round(span_days, 3), start_equity=p.account_balance,
            end_equity=round(final_equity, 2), trades=trades, equity_curve=curve,
            signals_seen=signals, entries_blocked=blocked,
            cost_paid=round(fee_paid + spread_paid, 2), fee_paid=round(fee_paid, 2),
            spread_paid=round(spread_paid, 2), halted_at=halted_at,
            pending_expired=self.expired, pending_cancelled=self.cancelled,
        )


# ─────────────────────── signal event / forward-return study ─────────────────

def forward_return_study(data: Dict[str, Bars], params: Params, kind: str,
                         lookbacks: Sequence[int] = (1, 2, 4, 6, 10),
                         thresholds: Sequence[float] = (0.5, 1.0, 1.5, 2.0, 3.0),
                         horizons_sec: Sequence[int] = (30, 60, 300, 900, 1800, 3600),
                         out_path: Optional[str] = None) -> Dict[str, Any]:
    """Conditional expectation: after a spike >= T over L scans, what happens next?

    Reports MFE / MAE / terminal return per horizon so downstream analysis can
    re-price any stop/trail combination without re-reading the raw tape.
    """
    events: List[Dict[str, Any]] = []
    for sym, b in data.items():
        step = b.step
        for L in lookbacks:
            look = np.full(len(b), np.nan)
            if L < len(b):
                base = b.c[:-L] if L else b.c
                look[L:] = (b.c[L:] - base) / base * 100.0
            for T in thresholds:
                # event = the first scan where the move crosses T (edge triggered)
                trigger = (look >= T)
                idxs = np.flatnonzero(trigger)
                if len(idxs) == 0:
                    continue
                # de-cluster: keep at most one event per 30 minutes
                keep = []
                last = -10 ** 9
                for i in idxs:
                    if b.t[i] - last >= 1800:
                        keep.append(i)
                        last = b.t[i]
                for i in keep:
                    entry = float(b.c[i])
                    row = {"symbol": sym, "t": int(b.t[i]), "lookback_scans": L,
                           "threshold_pct": T, "observed_pct": round(float(look[i]), 4),
                           "entry": entry}
                    for h in horizons_sec:
                        j = min(len(b) - 1, i + int(round(h / step)))
                        seg_hi = float(np.max(b.h[i + 1:j + 1])) if j > i else entry
                        seg_lo = float(np.min(b.l[i + 1:j + 1])) if j > i else entry
                        row[f"h{h}_mfe_pct"] = round((seg_hi - entry) / entry * 100.0, 4)
                        row[f"h{h}_mae_pct"] = round((seg_lo - entry) / entry * 100.0, 4)
                        row[f"h{h}_ret_pct"] = round((float(b.c[j]) - entry) / entry * 100.0, 4)
                    events.append(row)

    # unconditional baseline: identical statistics at random (de-clustered) times.
    # Without this control a positive MFE number means nothing.
    for sym, b in data.items():
        step = b.step
        for i in range(0, len(b) - 1, max(1, int(round(1800 / step)))):
            entry = float(b.c[i])
            row = {"symbol": sym, "t": int(b.t[i]), "lookback_scans": 0,
                   "threshold_pct": 0.0, "observed_pct": 0.0, "entry": entry}
            for h in horizons_sec:
                j = min(len(b) - 1, i + int(round(h / step)))
                seg_hi = float(np.max(b.h[i + 1:j + 1])) if j > i else entry
                seg_lo = float(np.min(b.l[i + 1:j + 1])) if j > i else entry
                row[f"h{h}_mfe_pct"] = round((seg_hi - entry) / entry * 100.0, 4)
                row[f"h{h}_mae_pct"] = round((seg_lo - entry) / entry * 100.0, 4)
                row[f"h{h}_ret_pct"] = round((float(b.c[j]) - entry) / entry * 100.0, 4)
            events.append(row)

    summary: Dict[str, Any] = {"dataset": kind, "symbols": sorted(data.keys()),
                               "events": len(events), "cells": {}}
    keys = list(events[0].keys()) if events else []
    combos = [(0, 0.0)] + [(L, T) for L in lookbacks for T in thresholds]

    def _cell(sel: List[Dict[str, Any]]) -> Dict[str, Any]:
        cell: Dict[str, Any] = {"n": len(sel)}
        for h in horizons_sec:
            rets = np.array([e[f"h{h}_ret_pct"] for e in sel])
            mfes = np.array([e[f"h{h}_mfe_pct"] for e in sel])
            maes = np.array([e[f"h{h}_mae_pct"] for e in sel])
            sd = float(rets.std(ddof=1)) if len(rets) > 2 else 0.0
            cell[f"h{h}"] = {
                "mean_ret": round(float(rets.mean()), 4),
                "median_ret": round(float(np.median(rets)), 4),
                "std_ret": round(sd, 4),
                "t_stat": round(float(rets.mean() / (sd / math.sqrt(len(rets)))), 2)
                if sd > 0 else None,
                "pct_positive": round(float((rets > 0).mean() * 100.0), 1),
                "mean_mfe": round(float(mfes.mean()), 4),
                "mean_mae": round(float(maes.mean()), 4),
                "pct_mfe_ge_1.5_and_mae_lt_1.5": round(float(
                    ((mfes >= 1.5) & (np.abs(maes) < 1.5)).mean() * 100.0), 1),
            }
        return cell

    for L, T in combos:
        sel = [e for e in events if e["lookback_scans"] == L and e["threshold_pct"] == T]
        if len(sel) < 10:
            continue
        label = "UNCONDITIONAL" if L == 0 else f"L{L}_T{T}"
        cell = _cell(sel)
        # edge = signal-cell mean forward return minus the unconditional mean
        uncond = next((c for k, c in summary["cells"].items() if k == "UNCONDITIONAL"), None)
        if uncond and label != "UNCONDITIONAL":
            for h in horizons_sec:
                base = uncond.get(f"h{h}", {}).get("mean_ret")
                if base is not None:
                    cell[f"h{h}"]["edge_vs_unconditional"] = round(cell[f"h{h}"]["mean_ret"] - base, 4)
        summary["cells"][label] = cell

    if out_path and events:
        import csv as _csv
        opener = gzip.open if out_path.endswith(".gz") else open
        with opener(out_path, "wt", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for e in events:
                w.writerow(e)
    return summary


# ───────────────────────────── risk-of-ruin / MC ─────────────────────────────

def monte_carlo(trades: Sequence[Trade], p: Params, paths: int = 2000,
                trades_per_path: int = 200, seed: int = 11) -> Dict[str, Any]:
    """Resample realised trade P&L (block bootstrap) to price sequence risk."""
    if not trades:
        return {}
    rng = random.Random(seed)
    pnls = [t.pnl_quote for t in trades]
    finals, dds, ruins = [], [], 0
    for _ in range(paths):
        eq = p.account_balance
        peak = eq
        dd = 0.0
        ruined = False
        for _ in range(trades_per_path):
            # block bootstrap of 5-trade blocks to keep some autocorrelation
            k = rng.randrange(len(pnls))
            block = pnls[k:k + 5] or [pnls[k]]
            for x in block:
                scale = eq / p.account_balance
                eq += x * scale
                peak = max(peak, eq)
                dd = max(dd, (peak - eq) / peak * 100.0)
                if eq <= p.account_balance * 0.5:
                    ruined = True
                    break
            if ruined:
                break
        finals.append(eq / p.account_balance)
        dds.append(dd)
        ruins += int(ruined)
    finals_sorted = sorted(finals)
    return {
        "paths": paths, "trades_per_path": trades_per_path,
        "median_final_multiple": round(finals_sorted[len(finals_sorted) // 2], 3),
        "p05_final_multiple": round(finals_sorted[int(0.05 * len(finals_sorted))], 3),
        "p95_final_multiple": round(finals_sorted[int(0.95 * len(finals_sorted))], 3),
        "prob_loss": round(sum(1 for f in finals if f < 1.0) / len(finals) * 100.0, 1),
        "prob_half_loss": round(ruins / len(finals) * 100.0, 1),
        "median_max_dd_pct": round(sorted(dds)[len(dds) // 2], 2),
        "p95_max_dd_pct": round(sorted(dds)[int(0.95 * len(dds))], 2),
    }


def bootstrap_expectancy(trades: Sequence[Trade], iters: int = 4000,
                         seed: int = 5) -> Dict[str, Any]:
    if len(trades) < 5:
        return {}
    rng = random.Random(seed)
    nets = [t.net_pct for t in trades]
    means = []
    n = len(nets)
    for _ in range(iters):
        means.append(statistics.fmean(rng.choices(nets, k=n)))
    means.sort()
    return {
        "n": n,
        "mean": round(statistics.fmean(nets), 4),
        "ci95_low": round(means[int(0.025 * iters)], 4),
        "ci95_high": round(means[int(0.975 * iters)], 4),
        "prob_negative_expectancy": round(sum(1 for m in means if m <= 0) / iters * 100.0, 1),
    }


# ─────────────────────────────── cost geometry ───────────────────────────────

def cost_geometry(p: Params, exit_reasons: Dict[str, int],
                  avg_trail_win: Optional[float]) -> Dict[str, Any]:
    """Static economics of the payoff structure under the live defaults."""
    hs = _half_spread(p)
    round_trip_cost = 2 * (hs + p.slippage_pct) + 2 * p.fee_pct
    trail_exit_gross = max(0.0, p.trailing_activation_pct - p.trailing_distance_pct)
    return {
        "half_spread_pct_assumed": hs,
        "fee_pct_per_side": p.fee_pct,
        "slippage_pct_per_side": p.slippage_pct,
        "round_trip_cost_pct_of_notional": round(round_trip_cost, 4),
        "cost_as_pct_of_stop_distance": round(round_trip_cost / p.stop_loss_pct * 100.0, 2),
        "smallest_possible_trailing_win_gross_pct": round(trail_exit_gross, 4),
        "smallest_possible_trailing_win_net_pct": round(trail_exit_gross - round_trip_cost, 4),
        "min_gain_pct_to_break_even": round(round_trip_cost, 4),
        "ideal_trail_win_pct_needed": round(p.trailing_distance_pct + round_trip_cost, 4),
        "break_even_win_rate_pct_for_3pct_stop": round(
            (p.stop_loss_pct + round_trip_cost) /
            ((p.stop_loss_pct + round_trip_cost) + max(0.0, (p.trailing_activation_pct))) * 100.0, 2),
    }


def buy_and_hold(data: Dict[str, Bars], p: Params) -> Dict[str, Any]:
    out = {}
    for sym, b in data.items():
        if len(b) < 2:
            continue
        out[sym] = {
            "return_pct": round((b.c[-1] / b.c[0] - 1) * 100.0, 3),
            "max_dd_pct": round(_max_dd(b.c) * 100.0, 3),
            "ann_vol_pct": round(float(np.diff(np.log(b.c)).std() * math.sqrt(365 * 86400 / b.step) * 100), 2),
        }
    return out


def _max_dd(series: np.ndarray) -> float:
    peak = -1e30
    dd = 0.0
    for v in series:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak)
    return dd


# ─────────────────────────────────── report ──────────────────────────────────

def fmt(x: Any) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.4f}".rstrip("0").rstrip(".")
    return str(x)


def print_result(res: SimResult, title: str) -> None:
    s = res.stats()
    print(f"\n=== {title} ===")
    print(f"  window            : {res.days:.2f} days | bars={res.bars_kind} | symbols={','.join(res.symbols)}")
    print(f"  equity            : {res.start_equity:,.0f} → {res.end_equity:,.0f} IRT "
          f"({s['total_return_pct']:+.3f}%)")
    print(f"  trades            : {s['trades']} ({s['trades_per_day']}/day) | signals seen={s['signals_seen']}")
    print(f"  win rate          : {s['win_rate_pct']}% | avg win {s['avg_win_pct']}% | avg loss {s['avg_loss_pct']}%")
    print(f"  expectancy/trade  : {s['expectancy_pct']}% ({s['expectancy_quote']} IRT) | median {s['median_pnl_pct']}%")
    print(f"  profit factor     : {s['profit_factor']} | max DD {s['max_drawdown_pct']}%")
    print(f"  costs paid        : {s['cost_paid_quote']:,} IRT (fees {s['fee_paid_quote']:,} / "
          f"spread+slip {s['spread_paid_quote']:,})")
    print(f"  P&L before costs  : {s['gross_before_costs_quote']:,} IRT")
    print(f"  exits             : {s['exit_reasons']}")
    print(f"  blocked entries   : {s['entries_blocked']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "research_data"))
    ap.add_argument("--bars", choices=("10s", "1m"), default="10s")
    ap.add_argument("--symbols", default=None, help="comma list (default: all found)")
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--sweep", action="store_true")
    ap.add_argument("--forward-study", action="store_true")
    ap.add_argument("--study-kind", default=None, help="bars kind for the forward study")
    ap.add_argument("--horizon-min", type=float, default=0.0,
                    help="force-close positions after N minutes (diagnostic, not live behaviour)")
    args = ap.parse_args()

    p = load_config()
    if args.horizon_min > 0:
        p = p.scaled(horizon_minutes=args.horizon_min) if False else p
    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None

    data = discover(args.data_dir, args.bars, symbols)
    if not data:
        print(f"no {args.bars} bars in {args.data_dir}")
        return 2
    print(f"loaded {args.bars} bars: " + ", ".join(
        f"{s}={len(b):,} bars ({b.step:.0f}s)" for s, b in sorted(data.items())))

    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "params": asdict(p),
        "dataset": {s: {"bars": len(b), "step_sec": b.step,
                        "first": int(b.t[0]), "last": int(b.t[-1])} for s, b in data.items()},
    }

    # 1a. what the config *file* says
    base = Engine(data, p, label="file-defaults", bars_kind=args.bars).run()
    print_result(base, "A) bot_config.json FILE DEFAULTS (fees 0.25%/side, half-spread 0.15%/side, slip 0.05%/side)")
    report["file_defaults"] = base.stats()

    # 1b. what the running GUI actually applies (regime preset overrides the file)
    preset_params = apply_balanced_preset(p)
    preset = Engine(data, preset_params, label="balanced-preset", bars_kind=args.bars).run()
    print_result(preset, "B) BALANCED REGIME PRESET — what auto_regime_strategy really applies "
                         "(min move 0.8%, trail 3.0/2.0, TP 50%, confirmation on, 10 slots)")
    report["balanced_preset"] = preset.stats()
    report["balanced_preset_params"] = asdict(preset_params)

    base = preset   # the deployed behaviour is the primary arm for the deep-dive blocks
    print("\n(primary arm for the remaining blocks: balanced-preset = deployed behaviour)")
    report["baseline"] = base.stats()
    report["baseline_bootstrap"] = bootstrap_expectancy(base.trades)
    report["monte_carlo"] = monte_carlo(base.trades, p)
    report["cost_geometry"] = cost_geometry(p, base.stats()["exit_reasons"], base.stats()["avg_win_pct"])
    report["buy_and_hold"] = buy_and_hold(data, p)
    report["forward_returns"] = forward_return_study(
        data, p, args.bars,
        out_path=os.path.join(args.data_dir, f"signal_events_{args.bars}.csv.gz"))
    print(f"  bootstrap CI      : {report['baseline_bootstrap']}")
    print(f"  monte carlo       : {report['monte_carlo']}")
    print(f"  cost geometry     : {report['cost_geometry']}")
    print(f"  buy & hold        : {report['buy_and_hold']}")

    # 2. zero-cost control: is the *entry signal* itself predictive?
    zero = Engine(data, preset_params.scaled(fee_pct=0.0, half_spread_pct=0.0, slippage_pct=0.0),
                  label="zero-costs", bars_kind=args.bars).run()
    print_result(zero, "SIGNAL-ONLY (zero fees / zero spread: is the entry edge real?)")
    report["zero_cost"] = zero.stats()

    # 3. venue-realism ladder: exchange taker fees and typical books
    ladder = []
    for label, fp, hsp, slp in (
        ("taker 0.10% / tight book 0.02%", 0.10, 0.01, 0.02),
        ("taker 0.15% / Nobitex BTC-IRT 0.15%", 0.15, 0.075, 0.05),
        ("taker 0.25% / Nobitex BTC-IRT 0.30%", 0.25, 0.15, 0.05),
        ("taker 0.25% / allowed max spread 0.90%", 0.25, 0.45, 0.10),
    ):
        r = Engine(data, p.scaled(fee_pct=fp, half_spread_pct=hsp, slippage_pct=slp),
                   label=label, bars_kind=args.bars).run()
        print_result(r, f"COST SCENARIO — {label}")
        ladder.append({"scenario": label, **r.stats()})
    report["cost_ladder"] = ladder

    # 4. random-entry control (same exits, random timing)
    rnd = Engine(data, preset_params.scaled(random_entry=True, random_seed=3,
                                            confirmation_enabled=False, random_prob=1e-3),
                 label="random-entry", bars_kind=args.bars).run()
    print_result(rnd, "CONTROL — random entries, identical exit rules")
    report["random_entry_control"] = rnd.stats()

    # 5. parameter sweep (entry threshold, trail distance, stop)
    # 6. how would this scale to the real Nobitex universe (~150 IRT markets)?
    #    The live scan iterates every IRT market, not just the tested basket.
    def _scaling(res: SimResult, n_markets: int = 150) -> Dict[str, Any]:
        st = res.stats()
        n_sym = max(1, len(res.symbols))
        if not st["trades"] or res.days <= 0:
            return {"note": "no trades in this arm"}
        trades_per_day_per_symbol = st["trades"] / res.days / n_sym
        avg_notional_frac = statistics.fmean(
            [t.notional / res.start_equity for t in res.trades]) if res.trades else 0.0
        exp_frac = (st["expectancy_pct"] or 0.0) / 100.0 * avg_notional_frac
        implied = trades_per_day_per_symbol * n_markets
        return {
            "markets_assumed": n_markets,
            "tested_symbols": n_sym,
            "trades_per_day_per_symbol": round(trades_per_day_per_symbol, 4),
            "signals_per_day_per_symbol": round(res.signals_seen / max(res.days, 1e-9) / n_sym, 3),
            "implied_trades_per_day": round(implied, 2),
            "avg_notional_pct_of_equity": round(avg_notional_frac * 100.0, 2),
            "expectancy_pct_of_notional": st["expectancy_pct"],
            "implied_daily_equity_drag_pct": round(implied * exp_frac * 100.0, 3),
            "implied_monthly_equity_drag_pct": round(implied * exp_frac * 100.0 * 30, 2),
        }

    report["universe_scaling"] = {
        "file_defaults": _scaling(base if base.label == "file-defaults" else
                                  Engine(data, p, label="file-defaults", bars_kind=args.bars).run()),
        "balanced_preset": _scaling(preset),
    }
    print("\nuniverse scaling (extrapolated to the full Nobitex IRT market list):")
    print(json.dumps(report["universe_scaling"], indent=2, default=str))

    if args.sweep:
        grid = []
        for thr in (0.5, 1.0, 1.5, 2.0, 3.0):
            for trail in (0.6, 1.2, 2.0, 99.0):   # 99 == trailing disabled (pure 3% stop)
                for stop in (2.0, 3.0, 5.0):
                    r = Engine(data, preset_params.scaled(min_observed_move_pct=thr,
                                                          trailing_distance_pct=trail,
                                                          stop_loss_pct=stop),
                               label=f"T{thr}/trail{trail}/stop{stop}", bars_kind=args.bars).run()
                    st = r.stats()
                    grid.append({"threshold": thr, "trail": trail, "stop": stop,
                                 "trades": st["trades"], "win_rate": st["win_rate_pct"],
                                 "expectancy_pct": st["expectancy_pct"],
                                 "total_return_pct": st["total_return_pct"],
                                 "max_dd": st["max_drawdown_pct"],
                                 "profit_factor": st["profit_factor"]})
                    print(f"  [sweep] thr={thr:<4} trail={trail:<5} stop={stop:<4} "
                          f"trades={st['trades']:<4} win={fmt(st['win_rate_pct']):<6} "
                          f"exp={fmt(st['expectancy_pct']):<9} ret={fmt(st['total_return_pct']):<9} "
                          f"dd={fmt(st['max_drawdown_pct'])}")
        report["sweep"] = grid

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
        print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
