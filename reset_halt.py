"""
reset_halt.py — Clear stale halt state and peak_equity from a live DB.

Usage
-----
    python reset_halt.py                       # default DB
    python reset_halt.py --db path/to/db       # custom DB
    python reset_halt.py --show                # only print state, do not modify
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional


def _default_db_path() -> Path:
    try:
        from core.config import APPDATA_DIR
        return Path(APPDATA_DIR) / "real_trades.db"
    except Exception:
        return Path("data") / "real_trades.db"


def _print_state(label: str, state: dict) -> None:
    print(f"{label}:")
    for k in sorted(state.keys()):
        print(f"  {k} = {state[k]}")
    if not state:
        print("  (empty)")


def _read_state(cur: sqlite3.Cursor) -> dict:
    try:
        cur.execute("SELECT key, value FROM bot_state")
        return dict(cur.fetchall())
    except sqlite3.Error:
        return {}


def reset(db_path: Path, show_only: bool = False) -> int:
    if not db_path.exists():
        print(f"[ERROR] DB not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        cur = conn.cursor()

        # Ensure table exists
        cur.execute(
            "CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)"
        )

        before = _read_state(cur)
        _print_state("BEFORE", before)

        if show_only:
            return 0

        cash_str = before.get("cash", "0")
        try:
            cash = float(cash_str)
        except (ValueError, TypeError):
            cash = 0.0

        if cash <= 0:
            print(
                "\n[WARN] Cash is 0 or invalid. Aborting reset to avoid corrupting baseline.",
                file=sys.stderr,
            )
            return 3

        upsert = (
            "INSERT INTO bot_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        cur.execute(upsert, ("peak_equity", f"{cash:.8f}"))
        cur.execute(upsert, ("initial_balance", f"{cash:.8f}"))
        cur.execute(upsert, ("trading_halted", "0"))
        cur.execute(upsert, ("halt_reason", ""))
        conn.commit()

        after = _read_state(cur)
        print()
        _print_state("AFTER", after)
    finally:
        conn.close()

    print("\n✅ Done. Restart the bot now.")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(prog="reset_halt")
    parser.add_argument(
        "--db", type=Path, default=_default_db_path(),
        help="Path to the SQLite database",
    )
    parser.add_argument(
        "--show", action="store_true",
        help="Print current state without modifying it",
    )
    args = parser.parse_args(argv)
    return reset(args.db, show_only=args.show)


if __name__ == "__main__":
    sys.exit(main())