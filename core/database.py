"""
core/database.py — Persistent SQLite storage with WAL mode for CryptoScanner.

Provides atomic transactions for:
- Order lifecycle tracking
- Trade history
- Balance snapshots
- System state / health flags

FIX: isolation_level + BEGIN/COMMIT interaction caused
"cannot commit/rollback - no transaction is active" under executescript
and some read paths. Transactions are now explicit only when needed and
commit/rollback are guarded with in_transaction.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.path.join("data", "cryptoscanner.db")


class Database:
    """Thread-safe SQLite database with WAL mode and timeout."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
        self._lock = threading.RLock()
        parent = os.path.dirname(os.path.abspath(self.db_path))
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        # Default isolation (DEFERRED) — Python opens a transaction on DML.
        conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Write transaction; safe commit/rollback even if SQLite auto-finished."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                if conn.in_transaction:
                    conn.execute("COMMIT")
            except Exception:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error as rb_exc:
                        logger.debug("Rollback skipped: %s", rb_exc)
                raise
            finally:
                conn.close()

    @contextmanager
    def _reader(self) -> Generator[sqlite3.Connection, None, None]:
        """Read path without forcing a write transaction."""
        with self._lock:
            conn = self._connect()
            try:
                yield conn
            finally:
                conn.close()

    def _init_schema(self) -> None:
        # executescript issues implicit commits; do not wrap in BEGIN/COMMIT.
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS orders (
                        id              TEXT PRIMARY KEY,
                        client_order_id TEXT UNIQUE,
                        symbol          TEXT NOT NULL,
                        side            TEXT NOT NULL,
                        order_type      TEXT NOT NULL,
                        price           REAL,
                        amount          REAL NOT NULL,
                        filled_amount   REAL DEFAULT 0,
                        status          TEXT NOT NULL DEFAULT 'prepared',
                        created_at      TEXT NOT NULL,
                        updated_at      TEXT NOT NULL,
                        raw_response    TEXT,
                        error_message   TEXT
                    );

                    CREATE TABLE IF NOT EXISTS trades (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id        TEXT,
                        symbol          TEXT NOT NULL,
                        side            TEXT NOT NULL,
                        price           REAL NOT NULL,
                        amount          REAL NOT NULL,
                        fee             REAL DEFAULT 0,
                        fee_currency    TEXT,
                        executed_at     TEXT NOT NULL,
                        mode            TEXT DEFAULT 'paper',
                        pnl             REAL,
                        FOREIGN KEY (order_id) REFERENCES orders(id)
                    );

                    CREATE TABLE IF NOT EXISTS balance_history (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        currency        TEXT NOT NULL,
                        available       REAL NOT NULL,
                        total           REAL NOT NULL,
                        recorded_at     TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS system_state (
                        key             TEXT PRIMARY KEY,
                        value           TEXT NOT NULL,
                        updated_at      TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
                    CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
                    CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
                    CREATE INDEX IF NOT EXISTS idx_balance_currency ON balance_history(currency);
                    """
                )
                conn.commit()
            finally:
                conn.close()
        logger.info("Database schema initialized at %s (WAL mode)", self.db_path)

    # ── Orders ──────────────────────────────────────────────────────────────

    def prepare_order(
        self,
        client_order_id: str,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
    ) -> str:
        """Insert a prepared order (pre-network). Returns internal id."""
        now = datetime.now(timezone.utc).isoformat()
        order_id = client_order_id
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO orders
                (id, client_order_id, symbol, side, order_type, price, amount,
                 status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (order_id, client_order_id, symbol, side, order_type, price,
                 amount, now, now),
            )
        return order_id

    def update_order_status(
        self,
        order_id: str,
        status: str,
        filled_amount: Optional[float] = None,
        raw_response: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE orders
                SET status = ?,
                    filled_amount = COALESCE(?, filled_amount),
                    raw_response = COALESCE(?, raw_response),
                    error_message = COALESCE(?, error_message),
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    filled_amount,
                    json.dumps(raw_response) if raw_response else None,
                    error_message,
                    now,
                    order_id,
                ),
            )

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._reader() as conn:
            if symbol:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE status IN ('prepared','submitted','partial') AND symbol = ?",
                    (symbol,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM orders WHERE status IN ('prepared','submitted','partial')"
                ).fetchall()
            return [dict(r) for r in rows]

    # ── Trades ──────────────────────────────────────────────────────────────

    def record_trade(
        self,
        symbol: str,
        side: str,
        price: float,
        amount: float,
        *,
        order_id: Optional[str] = None,
        fee: float = 0.0,
        fee_currency: Optional[str] = None,
        mode: str = "paper",
        pnl: Optional[float] = None,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO trades
                (order_id, symbol, side, price, amount, fee, fee_currency,
                 executed_at, mode, pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, symbol, side, price, amount, fee, fee_currency,
                 now, mode, pnl),
            )
            return int(cur.lastrowid)

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY executed_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ── Balance history ─────────────────────────────────────────────────────

    def record_balance(self, currency: str, available: float, total: float) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO balance_history (currency, available, total, recorded_at)
                VALUES (?, ?, ?, ?)
                """,
                (currency, available, total, now),
            )

    # ── System state ────────────────────────────────────────────────────────

    def set_state(self, key: str, value: Any) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(value) if not isinstance(value, str) else value
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO system_state (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, payload, now),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self._reader() as conn:
            row = conn.execute(
                "SELECT value FROM system_state WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                return row["value"]

    def close(self) -> None:
        """WAL checkpoint for cleanliness (no long-lived connection)."""
        try:
            conn = self._connect()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("WAL checkpoint failed: %s", exc)
