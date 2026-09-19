"""Nobitex public-market paper-test data recorder.

This tool is intentionally read-only. It never places, cancels, or modifies orders.
It records IRT market statistics and L2 order-book snapshots so the v7 paper
strategy can be evaluated against real Nobitex conditions.

Usage:
    py tools\\nobitex_paper_capture.py --hours 8
    py tools\\nobitex_paper_capture.py --hours 8 --interval 10 --top 30
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

BASE_URL = "https://apiv2.nobitex.ir"
STATS_URL = f"{BASE_URL}/market/stats"
ORDERBOOK_URL = f"{BASE_URL}/v2/orderbook/{{symbol}}"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fetch_stats(session: requests.Session) -> dict[str, dict[str, Any]]:
    response = session.get(
        STATS_URL,
        params={"srcCurrency": "all", "dstCurrency": "rls"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    stats = payload.get("stats", {})
    return stats if isinstance(stats, dict) else {}


def fetch_orderbook(session: requests.Session, symbol: str) -> dict[str, Any]:
    response = session.get(ORDERBOOK_URL.format(symbol=symbol), timeout=10)
    response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


def level(row: Any) -> tuple[float, float]:
    if isinstance(row, dict):
        price = _float(row.get("price") or row.get("Price"))
        amount = _float(
            row.get("amount")
            or row.get("quantity")
            or row.get("volume")
            or row.get("Amount")
        )
        return price, amount
    if isinstance(row, (list, tuple)) and len(row) >= 2:
        return _float(row[0]), _float(row[1])
    return 0.0, 0.0


def orderflow(book: dict[str, Any], levels: int = 10) -> dict[str, float]:
    bids = book.get("bids") or book.get("Bids") or []
    asks = book.get("asks") or book.get("Asks") or []

    bid_rows = [level(x) for x in bids[:levels]]
    ask_rows = [level(x) for x in asks[:levels]]

    bid_depth = sum(price * amount for price, amount in bid_rows if price > 0 and amount > 0)
    ask_depth = sum(price * amount for price, amount in ask_rows if price > 0 and amount > 0)

    best_bid = next((p for p, a in bid_rows if p > 0 and a > 0), 0.0)
    best_ask = next((p for p, a in ask_rows if p > 0 and a > 0), 0.0)

    if best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / mid * 100.0 if mid else 0.0
        micro_bias = (
            ((best_ask * bid_depth) + (best_bid * ask_depth))
            / (bid_depth + ask_depth)
            / mid
            - 1.0
        ) * 100.0 if (bid_depth + ask_depth) > 0 and mid else 0.0
    else:
        spread_pct = 0.0
        micro_bias = 0.0

    total = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total if total > 0 else 0.0
    score = max(0.0, min(100.0, 50.0 + imbalance * 50.0))

    return {
        "spread_pct": spread_pct,
        "bid_depth_quote": bid_depth,
        "ask_depth_quote": ask_depth,
        "order_flow_imbalance": imbalance,
        "order_flow_score": score,
        "microprice_bias_pct": micro_bias,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Nobitex IRT paper-test recorder")
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--interval", type=float, default=15.0)
    parser.add_argument("--top", type=int, default=40)
    parser.add_argument("--min-change", type=float, default=3.0)
    parser.add_argument("--output", default="data/nobitex_paper_capture.csv")
    args = parser.parse_args()

    if args.hours <= 0 or args.interval <= 0:
        parser.error("--hours and --interval must be positive")

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)

    fields = [
        "timestamp_utc",
        "symbol",
        "last",
        "open",
        "change_1h_pct",
        "volume_24h",
        "best_bid",
        "best_ask",
        "spread_pct",
        "bid_depth_quote",
        "ask_depth_quote",
        "order_flow_imbalance",
        "order_flow_score",
        "microprice_bias_pct",
    ]

    file_exists = os.path.exists(args.output) and os.path.getsize(args.output) > 0
    session = requests.Session()
    session.headers.update({"User-Agent": "NobitexAgent-PaperCapture/7.0"})

    started = time.monotonic()
    deadline = started + args.hours * 3600.0
    scans = 0
    rows_written = 0

    with open(args.output, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if not file_exists:
            writer.writeheader()

        while time.monotonic() < deadline:
            scans += 1
            timestamp = datetime.now(timezone.utc).isoformat()
            try:
                stats = fetch_stats(session)
                candidates = []

                for key, raw in stats.items():
                    if not isinstance(raw, dict):
                        continue
                    change = _float(
                        raw.get("change")
                        or raw.get("change1h")
                        or raw.get("change_1h")
                    )
                    last = _float(raw.get("last") or raw.get("latest") or raw.get("price"))
                    if last <= 0 or change < args.min_change:
                        continue
                    candidates.append((key, raw, change))

                candidates.sort(key=lambda item: item[2], reverse=True)
                candidates = candidates[: max(1, args.top)]

                for key, raw, change in candidates:
                    symbol = str(key).replace("-", "").replace("/", "").upper()
                    if symbol.endswith("RLS"):
                        symbol = symbol[:-3] + "IRT"
                    if not symbol.endswith("IRT"):
                        continue

                    try:
                        book = fetch_orderbook(session, symbol)
                        flow = orderflow(book)
                        bids = book.get("bids") or []
                        asks = book.get("asks") or []
                        best_bid = level(bids[0])[0] if bids else 0.0
                        best_ask = level(asks[0])[0] if asks else 0.0
                    except Exception:
                        flow = {
                            "spread_pct": 0.0,
                            "bid_depth_quote": 0.0,
                            "ask_depth_quote": 0.0,
                            "order_flow_imbalance": 0.0,
                            "order_flow_score": 0.0,
                            "microprice_bias_pct": 0.0,
                        }
                        best_bid = best_ask = 0.0

                    writer.writerow({
                        "timestamp_utc": timestamp,
                        "symbol": symbol,
                        "last": _float(raw.get("last") or raw.get("latest") or raw.get("price")),
                        "open": _float(raw.get("open")),
                        "change_1h_pct": change,
                        "volume_24h": _float(raw.get("volumeDst") or raw.get("volume")),
                        "best_bid": best_bid,
                        "best_ask": best_ask,
                        **flow,
                    })
                    rows_written += 1

                handle.flush()
                print(
                    f"[{timestamp}] scan={scans} candidates={len(candidates)} "
                    f"rows={rows_written}",
                    flush=True,
                )
            except Exception as exc:
                print(f"[{timestamp}] capture error: {exc}", file=sys.stderr, flush=True)

            time.sleep(max(0.5, args.interval))

    print(f"Finished: {rows_written} rows written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
