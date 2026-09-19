# trading/emergency_halt.py
"""
Emergency halt switch for CryptoScanner live trading.

Usage
-----
    # Show current state without modifying anything:
    python -m trading.emergency_halt --show

    # Halt using the default data directory:
    python -m trading.emergency_halt

    # Halt a specific database:
    python -m trading.emergency_halt --db path/to/real_trades.db

    # Cancel all open live orders on Nobitex (in addition to halting):
    python -m trading.emergency_halt --cancel-orders

    # Resume trading (clears halt + resets peak_equity to cash):
    python -m trading.emergency_halt --resume

This script writes directly to the SQLite bot_state table, so it works
even when the GUI is unresponsive.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("emergency_halt")


def _default_db_path() -> Path:
    try:
        from core.config import APPDATA_DIR
        return Path(APPDATA_DIR) / "real_trades.db"
    except Exception:
        return Path("data") / "real_trades.db"


def _ensure_state_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bot_state ("
        "  key TEXT PRIMARY KEY, value TEXT"
        ")"
    )


def _upsert(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO bot_state (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _read_state(conn: sqlite3.Connection) -> dict:
    try:
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM bot_state")
        return dict(cur.fetchall())
    except sqlite3.Error:
        return {}


def _print_state(label: str, state: dict) -> None:
    print(f"{label}:")
    for k in sorted(state.keys()):
        print(f"  {k} = {state[k]}")
    if not state:
        print("  (empty)")


def show(db_path: Path) -> int:
    """Print current state without modifying anything."""
    if not db_path.exists():
        print(f"[ERROR] Database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        _ensure_state_table(conn)
        _print_state(f"State of {db_path}", _read_state(conn))
    finally:
        conn.close()
    return 0


def halt(db_path: Path, reason: str = "Manual emergency halt") -> int:
    if not db_path.exists():
        print(f"[ERROR] Database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        _ensure_state_table(conn)
        print("Before:", _read_state(conn))

        _upsert(conn, "trading_halted", "1")
        _upsert(conn, "halt_reason", reason)

        # Optionally also disable new entries flag if the column exists
        try:
            conn.execute("SELECT allow_new_entries FROM bot_state LIMIT 1")
            _upsert(conn, "allow_new_entries", "0")
        except sqlite3.Error:
            pass

        conn.commit()
        print("After :", _read_state(conn))
    finally:
        conn.close()

    print(f"\n[OK] Trading HALTED for {db_path}")
    return 0


def resume(db_path: Path) -> int:
    if not db_path.exists():
        print(f"[ERROR] Database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        _ensure_state_table(conn)
        print("Before:", _read_state(conn))

        # Reset baseline to current cash to avoid immediate re-halt
        cash = 0.0
        try:
            cur = conn.cursor()
            cur.execute("SELECT value FROM bot_state WHERE key='cash'")
            row = cur.fetchone()
            if row and row[0]:
                cash = float(row[0])
        except (sqlite3.Error, ValueError, TypeError):
            cash = 0.0

        if cash > 0:
            _upsert(conn, "peak_equity", f"{cash:.8f}")
            _upsert(conn, "initial_balance", f"{cash:.8f}")

        _upsert(conn, "trading_halted", "0")
        _upsert(conn, "halt_reason", "")

        conn.commit()
        print("After :", _read_state(conn))
    finally:
        conn.close()

    print(f"\n[OK] Trading RESUMED for {db_path}")
    return 0


def cancel_live_orders(config_path: Optional[Path] = None) -> int:
    """Best-effort cancellation of all open live orders on Nobitex."""
    try:
        from trading.bot_config import load_config
        from trading.trader import TradingBot
    except Exception as exc:
        print(f"[ERROR] Could not import trading modules: {exc}", file=sys.stderr)
        return 2

    try:
        cfg = load_config()
        bot = TradingBot.get_instance(config=cfg, auto_start=False)
    except Exception as exc:
        print(f"[ERROR] Could not initialize bot: {exc}", file=sys.stderr)
        return 2

    try:
        orders = bot.get_open_orders() or []
    except Exception as exc:
        print(f"[ERROR] Could not fetch open orders: {exc}", file=sys.stderr)
        return 2

    if not orders:
        print("No open orders to cancel.")
        return 0

    cancelled = 0
    for order in orders:
        oid = order.get("order_id") or order.get("id")
        sym = order.get("symbol") or order.get("market")
        if not oid:
            continue
        try:
            bot.cancel_order(oid, sym)
            print(f"  [OK] Cancelled {sym} #{oid}")
            cancelled += 1
        except Exception as exc:
            print(f"  [FAIL] Could not cancel {sym} #{oid}: {exc}")

    print(f"\n[OK] Cancelled {cancelled}/{len(orders)} orders.")
    return 0 if cancelled == len(orders) else 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emergency_halt",
        description="Emergency halt/resume switch for CryptoScanner live trading.",
    )
    parser.add_argument(
        "--db", type=Path, default=_default_db_path(),
        help="Path to the SQLite database (default: <APPDATA_DIR>/real_trades.db)",
    )
    parser.add_argument(
        "--reason", type=str, default="Manual emergency halt",
        help="Reason string recorded in bot_state",
    )
    parser.add_argument(
        "--cancel-orders", action="store_true",
        help="Also attempt to cancel all open live orders on Nobitex",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="Clear the halt flag instead of setting it",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print current state without modifying anything",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
    )

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    )

    if args.show:
        return show(args.db)

    if args.resume:
        return resume(args.db)

    rc = halt(args.db, args.reason)
    if rc != 0:
        return rc

    if args.cancel_orders:
        rc2 = cancel_live_orders()
        if rc2 != 0:
            return rc2

    return 0


if __name__ == "__main__":
    sys.exit(main())