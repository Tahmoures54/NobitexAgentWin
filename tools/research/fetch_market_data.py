#!/usr/bin/env python3
"""Fetch public market data for the NobitexAgentWin profitability study.

Sources (all public, no credentials):
  * Binance Vision daily/monthly ZIP archives  -> 1m and 1s OHLCV (BTC/ETH vs USDT)
  * Binance / Bybit / Kraken REST as fallbacks -> 1m OHLCV when archives are unreachable
  * Nobitex public REST (probed, mostly blocked outside Iran) -> IRT-market stats/OHLC

Outputs (gzip CSV, column order: ts,open,high,low,close,volume):
  research_data/ohlc_1m_<SYM>.csv.gz    1-minute bars
  research_data/bars_10s_<SYM>.csv.gz   10-second bars aggregated from 1s klines
  research_data/fetch_report.json       per-source status, row counts, date ranges
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import sys
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone

UA = {"User-Agent": "Mozilla/5.0 (compatible; research-bot/1.0)"}
VISION = "https://data.binance.vision/data/spot"
TIMEOUT = 40


# ── http helpers ─────────────────────────────────────────────────────────────

def _get(url: str, timeout: int = TIMEOUT, retries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 - report and retry
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed {url}: {last}")


def _get_json(url: str, timeout: int = TIMEOUT, retries: int = 2):
    return json.loads(_get(url, timeout=timeout, retries=retries).decode("utf-8", "replace"))


def _write_gz(path: str, rows) -> int:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n = 0
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.writer(fh)
        for r in rows:
            w.writerow(r)
            n += 1
    return n


def _read_gz(path: str):
    with gzip.open(path, "rt", newline="") as fh:
        for row in csv.reader(fh):
            if row:
                yield row


def _norm_ts(value) -> int:
    """Seconds, milliseconds and microseconds all appear in public archives."""
    ts = int(float(value))
    while ts > 100_000_000_000:      # > year 5138 -> ms/us
        ts //= 1000
    return ts


def _fmt_ts(ts: int) -> str:
    try:
        return f"{datetime.fromtimestamp(int(ts), timezone.utc):%Y-%m-%d}"
    except (ValueError, OSError, OverflowError):
        return f"ts={ts}"


# ── Binance Vision archives ──────────────────────────────────────────────────

def _vision_zip(kind: str, symbol: str, interval: str, period: str, daily: bool) -> list:
    """Download one Binance Vision archive and return its kline rows."""
    if daily:
        name = f"{symbol}-{interval}-{period}.zip"
        url = f"{VISION}/daily/{kind}/{symbol}/{interval}/{name}"
    else:
        name = f"{symbol}-{interval}-{period}.zip"
        url = f"{VISION}/monthly/{kind}/{symbol}/{interval}/{name}"
    blob = _get(url, timeout=90, retries=2)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        inner = zf.namelist()[0]
        with zf.open(inner) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8")
            rows = []
            for row in csv.reader(text):
                if not row or not row[0] or not row[0].replace(".", "").isdigit():
                    continue
                # klines: open_time, o, h, l, c, v, close_time, ...
                rows.append((int(float(row[0])), row[1], row[2], row[3], row[4], row[5]))
    for r in rows:
        yield (_norm_ts(r[0]), *r[1:])


def fetch_vision_1m(symbol: str, days: int) -> list:
    """1m klines from monthly archives (falling back to daily files)."""
    out = []
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days)
    months = []
    cur = start.replace(day=1)
    while cur <= today:
        months.append(cur.strftime("%Y-%m"))
        cur = (cur.replace(day=28) + timedelta(days=5)).replace(day=1)
    got_monthly = False
    for m in months:
        try:
            rows = list(_vision_zip("klines", symbol, "1m", m, daily=False))
            if rows:
                got_monthly = True
                out.extend(rows)
                print(f"  [vision] {symbol} 1m month {m}: {len(rows)} rows", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [vision] {symbol} 1m month {m} unavailable ({exc})", flush=True)
    if not got_monthly:
        day = start
        while day <= today:
            try:
                rows = list(_vision_zip("klines", symbol, "1m", day.isoformat(), daily=True))
                out.extend(rows)
                print(f"  [vision] {symbol} 1m day {day}: {len(rows)} rows", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  [vision] {symbol} 1m day {day} unavailable ({exc})", flush=True)
            day += timedelta(days=1)
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    out = [r for r in out if r[0] >= cutoff]
    out.sort(key=lambda r: r[0])
    return out


def fetch_vision_10s(symbol: str, days: int) -> list:
    """1s klines from daily archives, aggregated into 10-second bars."""
    day = datetime.now(timezone.utc).date() - timedelta(days=days)
    today = datetime.now(timezone.utc).date()
    sec_rows: list = []
    while day < today:
        try:
            rows = list(_vision_zip("klines", symbol, "1s", day.isoformat(), daily=True))
            sec_rows.extend(rows)
            print(f"  [vision] {symbol} 1s day {day}: {len(rows)} rows", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  [vision] {symbol} 1s day {day} unavailable ({exc})", flush=True)
        day += timedelta(days=1)
    sec_rows.sort(key=lambda r: r[0])
    bars: list = []
    cur_bucket = None
    o = h = l = c = v = 0.0
    for ts, *ohlcv in sec_rows:
        t = _norm_ts(ts)
        bucket = t - (t % 10)
        op, hi, lo, cl, vol = (float(x) for x in ohlcv[:5])
        if bucket != cur_bucket:
            if cur_bucket is not None:
                bars.append((cur_bucket, o, h, l, c, v))
            cur_bucket, o, h, l, c, v = bucket, op, hi, lo, cl, vol
        else:
            h = max(h, hi)
            l = min(l, lo)
            c = cl
            v += vol
    if cur_bucket is not None:
        bars.append((cur_bucket, o, h, l, c, v))
    return bars


# ── REST fallbacks ───────────────────────────────────────────────────────────

def fetch_binance_rest_1m(symbol: str, days: int) -> list:
    out: list = []
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    base_urls = ["https://data-api.binance.vision/api/v3/klines",
                 "https://api.binance.com/api/v3/klines"]
    cur = start
    while cur < end:
        data = None
        for base in base_urls:
            try:
                data = _get_json(f"{base}?symbol={symbol}&interval=1m&limit=1000&startTime={cur}")
                if isinstance(data, list) and data:
                    break
                data = None
            except Exception:  # noqa: BLE001
                data = None
        if not data:
            raise RuntimeError("binance REST unavailable")
        for k in data:
            out.append((_norm_ts(k[0]), k[1], k[2], k[3], k[4], k[5]))
        cur = int(data[-1][0]) + 60_000
    out.sort(key=lambda r: r[0])
    return out


def fetch_bybit_rest_1m(symbol: str, days: int) -> list:
    out: list = []
    end = int(time.time() * 1000)
    cur = end - days * 86_400_000
    while cur < end:
        data = _get_json("https://api.bybit.com/v5/market/kline?category=spot"
                         f"&symbol={symbol}&interval=1&start={cur}&end={end}&limit=1000")
        rows = ((data or {}).get("result") or {}).get("list") or []
        if not rows:
            break
        for k in rows:
            out.append((_norm_ts(k[0]), k[1], k[2], k[3], k[4], k[5]))
        cur = int(rows[0][0]) + 60_000
    out.sort(key=lambda r: r[0])
    return out


def fetch_kraken_1m(pair: str, days: int) -> list:
    out: list = []
    since = int(time.time()) - days * 86_400
    cursor = since
    for _ in range(400):
        data = _get_json(f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=1&since={cursor}")
        res = (data or {}).get("result") or {}
        rows = next((v for k, v in res.items() if k != "last"), [])
        if not rows:
            break
        for k in rows:
            out.append((int(k[0]), k[1], k[2], k[3], k[4], k[6]))
        cursor = int(res.get("last") or (rows[-1][0] + 60))
        if cursor <= int(rows[-1][0]):
            cursor = int(rows[-1][0]) + 60
        if cursor >= int(time.time()) - 60:
            break
    out.sort(key=lambda r: r[0])
    return out


# ── Nobitex (informational) ──────────────────────────────────────────────────

def probe_nobitex() -> dict:
    report = {"reachable": False}
    try:
        stats = _get_json("https://api.nobitex.ir/market/stats", timeout=20, retries=1)
        report["reachable"] = True
        rows = []
        for key, val in (stats.get("stats") or {}).items():
            if not key.endswith("rls"):
                continue
            rows.append({"market": key, "last": val.get("latest"),
                         "day_change_pct": val.get("dayChange"),
                         "volume_irt": val.get("volume")})
        rows.sort(key=lambda r: -(float(r["volume_irt"] or 0)))
        report["markets"] = len(rows)
        report["top"] = rows[:20]
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)[:200]
    return report


# ── main ─────────────────────────────────────────────────────────────────────

KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD",
                "ADA": "ADAUSD", "DOT": "DOTUSD", "LINK": "LINKUSD", "LTC": "LTCUSD"}


def symbol_spec(sym: str) -> dict:
    pair = f"{sym}USDT"
    return {"vision": pair, "binance": pair, "bybit": pair,
            "kraken": KRAKEN_PAIRS.get(sym, pair)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="research_data")
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--tick-days", type=int, default=5)
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--tick-symbols", default="", help="symbols for 1s archives (10s bars)")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    report: dict = {"generated_at": datetime.now(timezone.utc).isoformat(),
                    "days": args.days, "tick_days": args.tick_days, "symbols": {}}

    for sym in [s.strip().upper() for s in args.symbols.split(",") if s.strip()]:
      try:
        spec = symbol_spec(sym)
        entry: dict = {"sources": {}}
        rows: list = []
        for name, fn in (
            ("binance_vision_monthly", lambda: fetch_vision_1m(spec["vision"], args.days)),
            ("binance_rest", lambda: fetch_binance_rest_1m(spec["binance"], args.days)),
            ("bybit_rest", lambda: fetch_bybit_rest_1m(spec["bybit"], args.days)),
            ("kraken_rest", lambda: fetch_kraken_1m(spec["kraken"], args.days)),
        ):
            try:
                rows = fn()
            except Exception as exc:  # noqa: BLE001
                entry["sources"][name] = f"error: {exc}"[:200]
                continue
            entry["sources"][name] = f"ok rows={len(rows)}"
            if len(rows) > 1000:
                entry["source"] = name
                break
            rows = []
        if rows:
            path = os.path.join(args.out, f"ohlc_1m_{sym}.csv.gz")
            n = _write_gz(path, rows)
            entry.update(rows_1m=n, first_bar=rows[0][0], last_bar=rows[-1][0],
                         path=os.path.basename(path))
            print(f"[{sym}] 1m bars={n} via {entry.get('source')} "
                  f"{_fmt_ts(rows[0][0])} -> {_fmt_ts(rows[-1][0])}", flush=True)
        else:
            entry["rows_1m"] = 0
            print(f"[{sym}] 1m data unavailable", flush=True)

        tick_wanted = [x.strip().upper() for x in
                       (args.tick_symbols or args.symbols).split(",") if x.strip()]
        if args.tick_days > 0 and sym in tick_wanted:
            try:
                bars = fetch_vision_10s(spec["vision"], args.tick_days)
            except Exception as exc:  # noqa: BLE001
                bars = []
                entry["sources"]["vision_1s"] = f"error: {exc}"[:200]
            if bars:
                path = os.path.join(args.out, f"bars_10s_{sym}.csv.gz")
                n = _write_gz(path, bars)
                entry.update(rows_10s=n, first_10s=bars[0][0], last_10s=bars[-1][0])
                print(f"[{sym}] 10s bars={n}", flush=True)
        report["symbols"][sym] = entry
      except Exception as exc:  # noqa: BLE001 - one symbol must not kill the run
        print(f"[{sym}] FAILED: {exc}", flush=True)
        report["symbols"][sym] = {"error": str(exc)[:200]}

    try:
        report["nobitex"] = probe_nobitex()
    except Exception as exc:  # noqa: BLE001
        report["nobitex"] = {"reachable": False, "error": str(exc)[:200]}
    print(f"[nobitex] reachable={report['nobitex'].get('reachable')}", flush=True)

    with open(os.path.join(args.out, "fetch_report.json"), "w") as fh:
        json.dump(report, fh, indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
