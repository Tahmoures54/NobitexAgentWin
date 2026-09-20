#!/usr/bin/env python3
"""Monte-Carlo of the bot's exit geometry alone (no market data needed).

Question: after the bot buys (because the price just jumped
+min_observed_move_pct over `movement_lookback_scans` scans), what does the
shipped exit rule earn on average - given only the *post-entry* path?

The rule (signal_tracker._evaluate_trade with the shipped options):
    hard stop      : -stop_loss_pct from entry
    trailing stop  : armed once profit >= trail_activation_pct, then
                     stop = max(extreme * (1 - trail_distance_pct/100), entry)
    take profit    : disabled (0) in bot_config.json
    costs          : taker fee both sides + half-spread both sides

The post-entry path is simulated as geometric Brownian motion whose per-step
volatility is calibrated to a daily volatility, plus an optional "continuation"
drift applied to the k steps after entry (i.e. how much of the spike keeps
going).  Scanning the drift term answers: how much trend-follow-through would
this strategy need to break even, and is that plausible?
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from typing import Dict, List

import numpy as np


def simulate(
    paths: int = 200_000,
    step_sec: float = 10.0,
    horizon_sec: float = 3600.0,
    daily_vol_pct: float = 6.0,
    stop_loss_pct: float = 3.0,
    trail_activation_pct: float = 1.5,
    trail_distance_pct: float = 1.2,
    take_profit_pct: float = 0.0,
    fee_pct_per_side: float = 0.25,
    half_spread_pct: float = 0.15,
    slippage_pct_per_side: float = 0.05,
    continuation_bps: float = 0.0,
    continuation_window_sec: float = 600.0,
    seed: int = 7,
) -> Dict[str, object]:
    rng = np.random.default_rng(seed)
    steps = int(round(horizon_sec / step_sec))
    sigma = (daily_vol_pct / 100.0) / math.sqrt(86_400.0 / step_sec)
    k_cont = max(1, int(round(continuation_window_sec / step_sec)))
    drift_per_step = (continuation_bps / 10_000.0) / k_cont

    entry = 1.0
    price = np.ones(paths)
    stop = np.full(paths, entry * (1 - stop_loss_pct / 100.0))
    extreme = np.full(paths, entry)
    armed = np.zeros(paths, dtype=bool)
    alive = np.ones(paths, dtype=bool)
    exit_ret = np.zeros(paths)
    reason = np.zeros(paths, dtype=np.int8)          # 0 open, 1 stop, 2 trail, 3 tp
    tp_level = entry * (1 + take_profit_pct / 100.0) if take_profit_pct > 0 else None

    for i in range(steps):
        shock = rng.normal(0.0, sigma, size=paths)
        if i < k_cont:
            shock = shock + drift_per_step
        price = price * np.exp(shock)
        hit = alive & (price <= stop)
        if hit.any():
            exit_ret[hit] = stop[hit] / entry - 1.0
            reason[hit] = np.where(armed[hit], 2, 1)
            alive &= ~hit
        if tp_level is not None:
            hit_tp = alive & (price >= tp_level)
            if hit_tp.any():
                exit_ret[hit_tp] = tp_level / entry - 1.0
                reason[hit_tp] = 3
                alive &= ~hit_tp
        extreme = np.maximum(extreme, price)
        profit_pct = (price - entry) / entry * 100.0
        arm_now = alive & (profit_pct > 0) & (profit_pct >= trail_activation_pct)
        armed |= arm_now
        trail = np.maximum(extreme * (1 - trail_distance_pct / 100.0), entry)
        stop = np.where(arm_now, np.maximum(stop, trail), stop)

    open_ret = price / entry - 1.0
    exit_ret = np.where(alive, open_ret, exit_ret)
    reason = np.where(alive, 0, reason)

    cost = 2 * (fee_pct_per_side + half_spread_pct + slippage_pct_per_side) / 100.0
    gross = exit_ret * 100.0
    net = gross - cost * 100.0

    return {
        "paths": paths,
        "horizon_sec": horizon_sec,
        "daily_vol_pct": daily_vol_pct,
        "continuation_bps_over_10min": continuation_bps,
        "step_sec": step_sec,
        "round_trip_cost_pct": round(cost * 100.0, 4),
        "mean_gross_pct": round(float(gross.mean()), 4),
        "mean_net_pct": round(float(net.mean()), 4),
        "median_net_pct": round(float(np.median(net)), 4),
        "std_net_pct": round(float(net.std()), 3),
        "pct_profitable": round(float((net > 0).mean() * 100.0), 2),
        "stop_exits_pct": round(float((reason == 1).mean() * 100.0), 2),
        "trail_exits_pct": round(float((reason == 2).mean() * 100.0), 2),
        "tp_exits_pct": round(float((reason == 3).mean() * 100.0), 2),
        "still_open_pct": round(float((reason == 0).mean() * 100.0), 2),
        "p05_net_pct": round(float(np.percentile(net, 5)), 3),
        "p95_net_pct": round(float(np.percentile(net, 95)), 3),
        "mean_net_armed_only_pct": round(float(net[armed].mean()), 4) if armed.any() else None,
        "armed_pct": round(float(armed.mean() * 100.0), 2),
    }


def long_run(paths: int = 20_000, horizon_hours: float = 24.0) -> None:
    """Both shipped profiles over a 24h horizon: the rule mainly decides WHEN to exit."""
    for name, kw in (
        ("file_defaults (trail 1.5/1.2, TP off)", dict()),
        ("balanced preset (trail 3.0/2.0, TP 50%)",
         dict(trail_activation_pct=3.0, trail_distance_pct=2.0, take_profit_pct=50.0)),
    ):
        print(f"\n== 24h horizon, {name}")
        for cont in (0.0, 25.0, 50.0, 100.0):
            res = simulate(paths=paths, daily_vol_pct=4.0, continuation_bps=cont,
                           horizon_sec=horizon_hours * 3600, **kw)
            print(f"  cont={cont:>6} bps -> mean_net={res['mean_net_pct']:>8}% "
                  f"median={res['median_net_pct']:>8}% stop={res['stop_exits_pct']}% "
                  f"trail={res['trail_exits_pct']}% open={res['still_open_pct']}% "
                  f"armed={res['armed_pct']}% win={res['pct_profitable']}%")


    # the deployed ('balanced' preset) geometry: trail 3.0 / 2.0
    print("\ndeployed balanced preset (trail 3.0/2.0, stop 3.0, TP 50%):")

    for cont in (0.0, 25.0, 50.0, 100.0, 200.0):
        res = simulate(paths=paths, daily_vol_pct=4.0, continuation_bps=cont,
                       trail_activation_pct=3.0, trail_distance_pct=2.0,
                       take_profit_pct=50.0)
        print(f"  cont={cont:>6} bps -> mean_net={res['mean_net_pct']:>8}% "
              f"armed={res['armed_pct']}% stop={res['stop_exits_pct']}% "
              f"trail={res['trail_exits_pct']}% tp={res['tp_exits_pct']}% "
              f"open={res['still_open_pct']}%")



def breakeven_continuation(**kw) -> float:
    """Smallest continuation drift (bps over the window) with net expectancy >= 0."""
    lo, hi = -200.0, 400.0
    for _ in range(20):
        mid = (lo + hi) / 2
        res = simulate(continuation_bps=mid, paths=20_000, **kw)
        if (res["mean_net_pct"] or 0.0) >= 0:
            hi = mid
        else:
            lo = mid
    return round(hi, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", type=int, default=60_000)
    ap.add_argument("--long", action="store_true", help="also run the 24h horizon study")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()

    out: Dict[str, object] = {"scenarios": [], "breakeven": {}}
    print("round-trip cost assumption: 2*(fee 0.25% + half-spread 0.15% + slip 0.05%) = 0.90%")
    print(f"{'vol/day':>7} {'cont(bps)':>10} {'mean_gross':>11} {'mean_net':>9} "
          f"{'armed%':>7} {'stop%':>6} {'trail%':>7} {'open%':>6} {'win%':>6}")
    for vol in (2.0, 4.0, 6.0, 10.0):
        for cont in (0.0, 25.0, 50.0, 100.0):
            res = simulate(paths=args.paths, daily_vol_pct=vol, continuation_bps=cont)
            out["scenarios"].append(res)
            print(f"{vol:>7} {cont:>10} {res['mean_gross_pct']:>11} {res['mean_net_pct']:>9} "
                  f"{res['armed_pct']:>7} {res['stop_exits_pct']:>6} {res['trail_exits_pct']:>7} "
                  f"{res['still_open_pct']:>6} {res['pct_profitable']:>6}")

    for vol in (2.0, 4.0, 6.0):
        out["breakeven"][f"vol_{vol}"] = breakeven_continuation(daily_vol_pct=vol)
        print(f"vol {vol}%/day -> breakeven continuation = "
              f"{out['breakeven'][f'vol_{vol}']} bps over 10 min")

    if args.long:
        long_run(paths=max(6000, args.paths / 4))

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(out, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
