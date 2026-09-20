"""Fetch real market data for the Nobitex profitability study.

Sandbox/CI egress rules differ per environment, so this script tries several
public venues and records exactly which one supplied the data.

Outputs (all gzip CSV, UTC unix seconds in the first column):

    research_data/ohlc_1m_<SYM>.csv.gz     t,open,high,low,close,volume
    research_data/bars_10s_<SYM>.csv.gz    t,open,high,low,close,volume,trades
    research_data/nobitex_probe.json       raw status of every Nobitex attempt
    research_data/fetch_report.json        provenance / row counts / errors

Usage:
    python tools/research/fetch_market_data.py --days 120 --tick-days 14
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import io
import csv
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "research_data")
UA = {"User-Agent": "CryptoScanner-Research/1.0 (+profitability-study)"}
HTTP_TIMEOUT = 30.0
SLEEP_BETWEEN_CALLS = 0.7
DEADLINE = float("inf")


def budget_left() -> float:
    return DEADLINE - time.time()


def out_of_time() -> bool:
    return budget_left() <= 0


# ────────────────────────────── http helper ──────────────────────────────────

def http_json(url: str, *, timeout: float = HTTP_TIMEOUT, retries: int = 3) -> Any:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read()[:200].decode("utf-8", "replace")
            except Exception:
                pass
            last_exc = RuntimeError(f"HTTP {exc.code} for {url} :: {body}")
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(1.0 + attempt)
                continue
            raise last_exc
        except Exception as exc:  # noqa: BLE001 - network layer
            last_exc = exc
            time.sleep(0.8 + attempt)
    raise RuntimeError(f"request failed: {url} :: {last_exc}")


# ─────────────────────────── 1-minute OHLCV sources ──────────────────────────

def fetch_bitfinex_1m(base: str, days: int) -> Tuple[List[list], str]:
    """[MTS, OPEN, CLOSE, HIGH, LOW, VOLUME] — 10k candles per call."""
    symbol = f"t{base}USD"
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows: List[list] = []
    cursor = start
    for _ in range(400):
        url = (f"https://api.bitfinex.com/v2/candles/trade:1m:{symbol}/hist"
               f"?limit=10000&start={cursor}&end={end}&sort=1")
        data = http_json(url)
        if not isinstance(data, list) or not data:
            break
        chunk = [r for r in data if isinstance(r, list) and len(r) >= 6]
        if not chunk:
            break
        rows.extend([r[:6] for r in chunk])
        next_cursor = int(chunk[-1][0]) + 60_000
        if next_cursor <= cursor or len(chunk) < 2:
            break
        cursor = next_cursor
        if cursor >= end or out_of_time():
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    # normalise to t,o,h,l,c,v
    out = [[int(r[0] // 1000), float(r[1]), float(r[3]), float(r[4]), float(r[2]), float(r[5])] for r in rows]
    out.sort(key=lambda x: x[0])
    return out, f"bitfinex:{symbol}:1m"


def fetch_bitstamp_1m(base: str, days: int) -> Tuple[List[list], str]:
    pair = f"{base.lower()}usd"
    end = int(time.time())
    start = end - days * 86_400
    rows: List[list] = []
    cursor = start
    for _ in range(2000):
        url = (f"https://www.bitstamp.net/api/v2/ohlc/{pair}/"
               f"?step=60&limit=1000&start={cursor}&end={end}")
        data = http_json(url)
        items = ((data or {}).get("data") or {}).get("ohlc") or []
        if not items:
            break
        for it in items:
            rows.append([int(it["timestamp"]), float(it["open"]), float(it["high"]),
                         float(it["low"]), float(it["close"]), float(it["volume"])])
        next_cursor = int(items[-1]["timestamp"]) + 60
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if cursor >= end - 60 or out_of_time():
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    rows.sort(key=lambda x: x[0])
    return rows, f"bitstamp:{pair}:1m"


def fetch_bybit_1m(base: str, days: int) -> Tuple[List[list], str]:
    symbol = f"{base}USDT"
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    rows: List[list] = []
    cursor = start
    for _ in range(2000):
        url = (f"https://api.bybit.com/v5/market/kline?category=spot&symbol={symbol}"
               f"&interval=1&start={cursor}&end={end}&limit=1000")
        data = http_json(url)
        items = ((data or {}).get("result") or {}).get("list") or []
        if not items:
            break
        chunk = sorted(items, key=lambda x: int(x[0]))
        for it in chunk:
            rows.append([int(it[0]) // 1000, float(it[1]), float(it[2]),
                         float(it[3]), float(it[4]), float(it[5])])
        next_cursor = int(chunk[-1][0]) + 60_000
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if cursor >= end:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    rows.sort(key=lambda x: x[0])
    return rows, f"bybit:{symbol}:1m"


def fetch_kucoin_1m(base: str, days: int) -> Tuple[List[list], str]:
    symbol = f"{base}-USDT"
    end = int(time.time())
    start = end - days * 86_400
    rows: List[list] = []
    cursor = start
    for _ in range(2000):
        url = (f"https://api.kucoin.com/api/v1/market/candles?type=1min&symbol={symbol}"
               f"&startAt={cursor}&endAt={end}")
        data = http_json(url)
        items = ((data or {}).get("data") or [])
        if not items:
            break
        chunk = sorted(items, key=lambda x: int(x[0]))
        for it in chunk:
            rows.append([int(it[0]), float(it[1]), float(it[3]), float(it[4]), float(it[2]), float(it[5])])
        next_cursor = int(chunk[-1][0]) + 60
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if cursor >= end:
            break
        time.sleep(SLEEP_BETWEEN_CALLS)
    rows.sort(key=lambda x: x[0])
    return rows, f"kucoin:{symbol}:1m"


def _vision_days(symbol: str, interval: str, days: int) -> Iterable[List[list]]:
    """Yield per-day kline rows from the static Binance Vision CDN (no rate limits)."""
    today = datetime.now(timezone.utc).date()
    for offset in range(days, 0, -1):
        d = datetime.fromordinal(today.toordinal() - offset).date()
        url = (f"https://data.binance.vision/data/spot/daily/klines/{symbol}/{interval}/"
               f"{symbol}-{interval}-{d.isoformat()}.zip")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                name = zf.namelist()[0]
                with zf.open(name) as fh:
                    day_rows = []
                    for line in fh.read().decode("utf-8", "replace").splitlines():
                        parts = line.split(",")
                        if len(parts) < 6 or not parts[0].strip().isdigit():
                            continue
                        ts = int(parts[0])
                        if ts > 10_000_000_000:
                            ts //= 1000
                        day_rows.append([ts, float(parts[1]), float(parts[2]), float(parts[3]),
                                         float(parts[4]), float(parts[5])])
            yield day_rows
        except Exception as exc:  # noqa: BLE001
            print(f"    binance-vision {symbol} {interval} {d}: {str(exc)[:120]}")
            yield []


def fetch_binance_vision_1s_bars(base: str, days: int) -> Tuple[List[list], str]:
    """1-second klines aggregated to 10-second bars (the live scan cadence)."""
    symbol = f"{base}USDT"
    buckets: Dict[int, List[float]] = {}
    for day_rows in _vision_days(symbol, "1s", days):
        if out_of_time():
            break
        for r in day_rows:
            b = (r[0] // 10) * 10
            cur = buckets.get(b)
            if cur is None:
                buckets[b] = [r[1], r[2], r[3], r[4], r[5], 1.0]
            else:
                cur[1] = max(cur[1], r[2])
                cur[2] = min(cur[2], r[3])
                cur[3] = r[4]
                cur[4] += r[5]
                cur[5] += 1.0
        time.sleep(0.02)
    rows = [[b] + v for b, v in sorted(buckets.items())]
    return rows, f"binance-vision:{symbol}:1s->10s"


def fetch_binance_vision_1m(base: str, days: int) -> Tuple[List[list], str]:
    """Daily zips from data.binance.vision (works in some regions where API is 451)."""
    symbol = f"{base}USDT"
    rows: List[list] = []
    today = datetime.now(timezone.utc).date()
    for offset in range(days, 0, -1):
        day = today.toordinal() - offset
        d = datetime.fromordinal(day).date()
        url = (f"https://data.binance.vision/data/spot/daily/klines/{symbol}/1m/"
               f"{symbol}-1m-{d.isoformat()}.zip")
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                blob = resp.read()
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                name = zf.namelist()[0]
                with zf.open(name) as fh:
                    for line in fh.read().decode("utf-8", "replace").splitlines():
                        parts = line.split(",")
                        if len(parts) < 6 or not parts[0].strip().isdigit():
                            continue
                        ts = int(parts[0])
                        if ts > 10_000_000_000:
                            ts //= 1000
                        rows.append([ts, float(parts[1]), float(parts[2]), float(parts[3]),
                                     float(parts[4]), float(parts[5])])
        except Exception as exc:  # noqa: BLE001
            print(f"    binance-vision {symbol} {d}: {exc}")
            continue
        time.sleep(0.05)
    rows.sort(key=lambda x: x[0])
    return rows, f"binance-vision:{symbol}:1m"


OHLC_SOURCES: Sequence[Tuple[str, Callable[[str, int], Tuple[List[list], str]]]] = (
    ("binance_vision", fetch_binance_vision_1m),
    ("bitfinex", fetch_bitfinex_1m),
    ("bitstamp", fetch_bitstamp_1m),
    ("bybit", fetch_bybit_1m),
    ("kucoin", fetch_kucoin_1m),
)


# ───────────────────────── trade tape → 10s bars ─────────────────────────────

def _aggregate_trades(trades: Iterable[Tuple[int, float, float]]) -> Dict[int, List[float]]:
    """(ms, price, amount) → {bucket_start_s: [o,h,l,c,v,n]}"""
    buckets: Dict[int, List[float]] = {}
    for ms, price, amount in trades:
        if price <= 0 or amount <= 0:
            continue
        b = (ms // 10_000) * 10
        cur = buckets.get(b)
        if cur is None:
            buckets[b] = [price, price, price, price, amount, 1.0]
        else:
            cur[1] = max(cur[1], price)
            cur[2] = min(cur[2], price)
            cur[3] = price
            cur[4] += amount
            cur[5] += 1.0
    return buckets


def fetch_bitfinex_trades(base: str, days: int) -> Tuple[List[list], str]:
    symbol = f"t{base}USD"
    end = int(time.time() * 1000)
    start = end - days * 86_400_000
    buckets: Dict[int, List[float]] = {}
    cursor = start
    calls = 0
    while cursor < end and calls < 800 and not out_of_time():
        url = (f"https://api.bitfinex.com/v2/trades/{symbol}/hist"
               f"?limit=10000&start={cursor}&end={end}&sort=1")
        data = http_json(url)
        calls += 1
        if not isinstance(data, list) or not data:
            break
        trades = []
        last_ms = cursor
        for row in data:
            try:
                ms = int(float(row[1]))
                amount = float(row[2])
                price = float(row[3])
            except Exception:
                continue
            trades.append((ms, price, abs(amount)))
            last_ms = max(last_ms, ms)
        for b, agg in _aggregate_trades(trades).items():
            cur = buckets.get(b)
            if cur is None:
                buckets[b] = agg
            else:
                cur[1] = max(cur[1], agg[1])
                cur[2] = min(cur[2], agg[2])
                cur[3] = agg[3]
                cur[4] += agg[4]
                cur[5] += agg[5]
        if last_ms <= cursor or len(data) < 2:
            break
        cursor = last_ms + 1
        time.sleep(0.2)
    rows = [[b] + v for b, v in sorted(buckets.items())]
    return rows, f"bitfinex-trades:{symbol}:10s"


def fetch_coinbase_trades(base: str, days: int) -> Tuple[List[list], str]:
    product = f"{base}-USD"
    cutoff = int(time.time()) - days * 86_400
    buckets: Dict[int, List[float]] = {}
    url = f"https://api.exchange.coinbase.com/products/{product}/trades?limit=1000"
    calls = 0
    while url and calls < 4000 and not out_of_time():
        data = http_json(url)
        calls += 1
        if not isinstance(data, list) or not data:
            break
        trades = []
        for row in data:
            try:
                ts = int(datetime.fromisoformat(row["time"].replace("Z", "+00:00")).timestamp())
                trades.append((ts * 1000, float(row["price"]), float(row["size"])))
            except Exception:
                continue
        for b, agg in _aggregate_trades(trades).items():
            cur = buckets.get(b)
            if cur is None:
                buckets[b] = agg
            else:
                cur[1] = max(cur[1], agg[1])
                cur[2] = min(cur[2], agg[2])
                cur[3] = agg[3]
                cur[4] += agg[4]
                cur[5] += agg[5]
        oldest = min((t[0] // 1000 for t in trades), default=0)
        if oldest and oldest <= cutoff:
            break
        if not trades:
            break
        url = (f"https://api.exchange.coinbase.com/products/{product}/trades"
               f"?limit=1000&before={min(int(r['trade_id']) for r in data)}")
        time.sleep(0.2)
    rows = [[b] + v for b, v in sorted(buckets.items())]
    return rows, f"coinbase-trades:{product}:10s"


TRADE_SOURCES: Sequence[Tuple[str, Callable[[str, int], Tuple[List[list], str]]]] = (
    ("binance_vision_1s", fetch_binance_vision_1s_bars),
    ("bitfinex", fetch_bitfinex_trades),
    ("coinbase", fetch_coinbase_trades),
)


# ──────────────────────────────── Nobitex probe ──────────────────────────────

def probe_nobitex() -> Dict[str, Any]:
    report: Dict[str, Any] = {"attempts": []}
    now = int(time.time())
    for path, params in (
        ("/market/stats", {"dstCurrency": "rls"}),
        ("/market/udf/history", {"symbol": "BTCIRT", "resolution": "15",
                                 "from": str(now - 900 * 500), "to": str(now)}),
        ("/market/orderbook/BTCIRT", {"limit": "10"}),
    ):
        url = "https://api.nobitex.ir" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        entry: Dict[str, Any] = {"url": url}
        try:
            data = http_json(url)
            entry["ok"] = True
            if path == "/market/stats":
                stats = (data or {}).get("stats") or {}
                summary = {}
                for key, item in list(stats.items())[:400]:
                    if not isinstance(item, dict) or key == "global":
                        continue
                    try:
                        bid = float(item.get("bestBuy") or 0)
                        ask = float(item.get("bestSell") or 0)
                    except Exception:
                        continue
                    if bid > 0 and ask > 0:
                        summary[key] = {
                            "spread_pct": round((ask - bid) / bid * 100.0, 4),
                            "volume_dst": float(item.get("volumeDst") or 0),
                            "day_change_pct": float(item.get("dayChange") or 0),
                        }
                entry["markets"] = len(summary)
                entry["spread_sample"] = dict(sorted(
                    summary.items(), key=lambda kv: -kv[1]["volume_dst"])[:25])
                entry["spread_median_all"] = (
                    sorted(v["spread_pct"] for v in summary.values())[len(summary) // 2]
                    if summary else None
                )
            elif path == "/market/udf/history":
                candles = (data or {}).get("candles") or []
                entry["candles"] = len(candles)
                entry["last_candle"] = candles[-1] if candles else None
                if candles:
                    with gzip.open(os.path.join(OUT_DIR, "nobitex_BTCIRT_15m.csv.gz"), "wt",
                                   newline="") as fh:
                        w = csv.writer(fh)
                        w.writerow(["t", "open", "high", "low", "close", "volume"])
                        for c in candles:
                            if len(c) >= 6:
                                w.writerow([int(c[0]), c[1], c[2], c[3], c[4], c[5]])
            else:
                book = (data or {})
                asks = book.get("asks") or []
                bids = book.get("bids") or []
                entry["levels"] = {"asks": len(asks), "bids": len(bids)}
                entry["top"] = {"ask": asks[:3], "bid": bids[:3]}
        except Exception as exc:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = str(exc)[:300]
        report["attempts"].append(entry)
        report["reachable"] = any(a.get("ok") for a in report["attempts"])
        time.sleep(0.3)
    return report


# ───────────────────────────────── writing ───────────────────────────────────

def write_gz_csv(path: str, header: Sequence[str], rows: Iterable[Sequence[Any]]) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = 0
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(list(header))
        for r in rows:
            w.writerow(list(r))
            n += 1
    return n


def dedupe(rows: List[list]) -> List[list]:
    seen = set()
    out = []
    for r in sorted(rows, key=lambda x: x[0]):
        if r[0] in seen:
            continue
        seen.add(r[0])
        out.append(r)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120, help="days of 1m OHLC")
    ap.add_argument("--tick-days", type=int, default=14, help="days of trade tape → 10s bars")
    ap.add_argument("--symbols", default="BTC,ETH")
    ap.add_argument("--skip-ticks", action="store_true")
    ap.add_argument("--skip-nobitex", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=25.0,
                    help="wall-clock budget for the whole fetch phase")
    args = ap.parse_args()

    global DEADLINE
    DEADLINE = time.time() + args.max_minutes * 60.0

    os.makedirs(OUT_DIR, exist_ok=True)
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    report: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(),
                              "symbols": symbols, "ohlc": {}, "ticks": {}, "errors": []}

    for sym in symbols:
        path = os.path.join(OUT_DIR, f"ohlc_1m_{sym}.csv.gz")
        if os.path.exists(path) and os.path.getsize(path) > 10_000:
            print(f"[ohlc] {sym}: cached")
            report["ohlc"][sym] = {"cached": True, "rows": -1}
            continue
        for name, fn in OHLC_SOURCES:
            try:
                print(f"[ohlc] {sym}: trying {name} ({args.days}d)…")
                rows, provenance = fn(sym, args.days)
                rows = dedupe(rows)
                if len(rows) < 5_000:
                    print(f"[ohlc] {sym}: {name} returned only {len(rows)} rows; next source")
                    continue
                n = write_gz_csv(path, ["t", "open", "high", "low", "close", "volume"], rows)
                print(f"[ohlc] {sym}: OK via {provenance} rows={n} "
                      f"range={datetime.utcfromtimestamp(rows[0][0]).date()} → "
                      f"{datetime.utcfromtimestamp(rows[-1][0]).date()}")
                report["ohlc"][sym] = {"provenance": provenance, "rows": n,
                                       "first": rows[0][0], "last": rows[-1][0]}
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[ohlc] {sym}: {name} failed: {str(exc)[:200]}")
                report["errors"].append(f"ohlc:{sym}:{name}:{str(exc)[:200]}")
        else:
            print(f"[ohlc] {sym}: ALL SOURCES FAILED")

    if not args.skip_ticks:
        for sym in symbols:
            path = os.path.join(OUT_DIR, f"bars_10s_{sym}.csv.gz")
            if os.path.exists(path) and os.path.getsize(path) > 10_000:
                print(f"[ticks] {sym}: cached")
                report["ticks"][sym] = {"cached": True, "rows": -1}
                continue
            for name, fn in TRADE_SOURCES:
                try:
                    print(f"[ticks] {sym}: trying {name} ({args.tick_days}d)…")
                    rows, provenance = fn(sym, args.tick_days)
                    if len(rows) < 5_000:
                        print(f"[ticks] {sym}: {name} returned only {len(rows)} bars; next source")
                        continue
                    n = write_gz_csv(path, ["t", "open", "high", "low", "close", "volume", "trades"], rows)
                    print(f"[ticks] {sym}: OK via {provenance} bars={n}")
                    report["ticks"][sym] = {"provenance": provenance, "rows": n,
                                            "first": rows[0][0], "last": rows[-1][0]}
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"[ticks] {sym}: {name} failed: {str(exc)[:200]}")
                    report["errors"].append(f"ticks:{sym}:{name}:{str(exc)[:200]}")

    if not args.skip_nobitex:
        print("[nobitex] probing public endpoints…")
        try:
            nb = probe_nobitex()
            report["nobitex"] = nb
            print(f"[nobitex] reachable={nb.get('reachable')}")
            for a in nb.get("attempts", []):
                print(f"    {a['url'][:110]} ok={a.get('ok')} {a.get('error', '')}")
        except Exception as exc:  # noqa: BLE001
            report["nobitex"] = {"reachable": False, "error": str(exc)[:300]}
            print(f"[nobitex] failed: {exc}")

    with open(os.path.join(OUT_DIR, "fetch_report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k in ("ohlc", "ticks", "errors")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
