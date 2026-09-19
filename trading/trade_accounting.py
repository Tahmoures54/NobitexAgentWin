"""Actual-fill accounting for Nobitex spot execution.

This module deliberately refuses to invent execution prices or fees.  It records
only quantities/prices/fees present in exchange order data and marks accounting
incomplete when a required execution field is missing.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        value = float(value)
        return value if value == value else None
    except (TypeError, ValueError):
        return None


def _first_num(mapping: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _num(mapping.get(key))
        if value is not None:
            return value
    return None


def _fee_from_order(order: Dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
    raw = order.get("raw") if isinstance(order.get("raw"), dict) else {}
    candidates = [order, raw]
    for obj in candidates:
        fee = _first_num(obj, "fee", "feeAmount", "fee_amount", "commission", "commissionAmount")
        if fee is not None:
            currency = (
                obj.get("feeCurrency") or obj.get("fee_currency")
                or obj.get("commissionAsset") or obj.get("commissionCurrency")
            )
            return max(0.0, fee), str(currency).upper() if currency else None
    trades = raw.get("trades") if isinstance(raw.get("trades"), list) else []
    total = 0.0
    currency = None
    found = False
    for fill in trades:
        if not isinstance(fill, dict):
            continue
        fee = _first_num(fill, "fee", "feeAmount", "fee_amount", "commission", "commissionAmount")
        if fee is None:
            continue
        found = True
        total += max(0.0, fee)
        currency = (
            fill.get("feeCurrency") or fill.get("fee_currency")
            or fill.get("commissionAsset") or fill.get("commissionCurrency")
            or currency
        )
    if found:
        return total, str(currency).upper() if currency else None
    return None, None


class TradeAccounting:
    """Weighted-average spot ledger backed by SQLite.

    The ledger is intentionally separate from SignalTracker's strategy journal:
    it represents actual exchange execution economics.
    """

    def __init__(self, db_path: str = "data/cryptoscanner.db", quote: str = "IRT"):
        self.db_path = db_path
        self.quote = "IRT" if str(quote).upper() in ("IRT", "RLS", "IRR") else str(quote).upper()
        self._lock = threading.RLock()
        self._init_schema()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounting_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fill_key TEXT NOT NULL UNIQUE,
                order_id TEXT,
                client_order_id TEXT,
                symbol TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                quantity REAL NOT NULL,
                price REAL NOT NULL,
                gross_quote REAL NOT NULL,
                fee REAL,
                fee_currency TEXT,
                executed_at TEXT NOT NULL,
                accounting_complete INTEGER NOT NULL DEFAULT 0,
                raw_response TEXT
            );
            CREATE TABLE IF NOT EXISTS accounting_positions (
                asset TEXT PRIMARY KEY,
                quantity REAL NOT NULL DEFAULT 0,
                cost_basis_quote REAL NOT NULL DEFAULT 0,
                realized_pnl_quote REAL NOT NULL DEFAULT 0,
                fees_quote REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_accounting_fills_asset ON accounting_fills(asset);
            """)
            conn.commit()

    @staticmethod
    def _split_symbol(symbol: str, quote: str) -> tuple[str, str]:
        s = str(symbol or "").upper().replace("/", "").replace("-", "")
        for q in (quote, "RLS" if quote == "IRT" else "", "USDT", "USDC", "BTC", "ETH"):
            if q and s.endswith(q) and len(s) > len(q):
                return s[:-len(q)], q
        return s, quote

    def ingest_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Book the actual matched fill in an idempotent transaction."""
        if not isinstance(order, dict):
            return {"status": "ignored", "reason": "invalid_order"}

        qty = _first_num(order, "executed_qty", "executed_quantity", "matched_amount")
        price = _first_num(order, "executed_price", "average_price", "averagePrice")
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").lower()
        order_id = order.get("order_id")
        client_id = order.get("client_order_id")
        if not symbol or side not in ("buy", "sell") or not qty or qty <= 0:
            return {"status": "pending", "accounting_complete": False, "reason": "no_actual_fill"}
        if not price or price <= 0:
            return {"status": "pending", "accounting_complete": False, "reason": "execution_price_missing"}

        asset, _ = self._split_symbol(symbol, self.quote)
        fee, fee_currency = _fee_from_order(order)
        fee_known = fee is not None

        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as conn:
            prior = None
            if order_id or client_id:
                prior = conn.execute(
                    """SELECT COALESCE(SUM(quantity),0) qty, COALESCE(SUM(gross_quote),0) gross,
                              COALESCE(SUM(fee),0) fee, COUNT(*) count
                       FROM accounting_fills
                       WHERE (order_id=? AND order_id IS NOT NULL)
                          OR (client_order_id=? AND client_order_id IS NOT NULL)""",
                    (str(order_id) if order_id else None, str(client_id) if client_id else None),
                ).fetchone()
            prior_qty = float(prior["qty"] or 0.0) if prior else 0.0
            prior_gross = float(prior["gross"] or 0.0) if prior else 0.0
            prior_fee = float(prior["fee"] or 0.0) if prior else 0.0
            if qty <= prior_qty + 1e-12:
                return {"status": "duplicate", "accounting_complete": fee_known}
            delta_qty = qty - prior_qty
            delta_gross = max(0.0, qty * price - prior_gross)
            price = delta_gross / delta_qty if delta_qty > 0 else price
            if fee_known and prior and prior["count"]:
                fee = max(0.0, fee - prior_fee)
            gross = delta_qty * price
            fill_key = f"{order_id or client_id or symbol}:{side}:{qty:.16g}"
            fee_value_quote = 0.0
            if fee_known:
                fee_currency_u = (fee_currency or "").upper()
                if fee_currency_u in (self.quote, "RLS", "IRR"):
                    fee_value_quote = fee
                elif fee_currency_u == asset:
                    fee_value_quote = fee * price
                else:
                    # Fee currency cannot be valued without another market.
                    fee_known = False

            conn.execute(
                """INSERT INTO accounting_fills
                (fill_key,order_id,client_order_id,symbol,asset,side,quantity,price,
                 gross_quote,fee,fee_currency,executed_at,accounting_complete,raw_response)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fill_key, str(order_id) if order_id else None,
                 str(client_id) if client_id else None, symbol, asset, side, delta_qty,
                 price, gross, fee, fee_currency, str(order.get("time") or now),
                 int(fee_known), str(order.get("raw") or order)),
            )

            pos = conn.execute("SELECT * FROM accounting_positions WHERE asset=?", (asset,)).fetchone()
            old_qty = float(pos["quantity"]) if pos else 0.0
            old_cost = float(pos["cost_basis_quote"]) if pos else 0.0
            old_realized = float(pos["realized_pnl_quote"]) if pos else 0.0
            old_fees = float(pos["fees_quote"]) if pos else 0.0

            if side == "buy":
                new_qty = old_qty + qty
                new_cost = old_cost + gross + fee_value_quote
                realized_delta = 0.0
            else:
                sell_qty = min(qty, old_qty) if old_qty > 0 else 0.0
                avg_cost = old_cost / old_qty if old_qty > 0 else 0.0
                cost_removed = avg_cost * sell_qty
                realized_delta = (gross - fee_value_quote - cost_removed) if sell_qty > 0 else 0.0
                new_qty = max(0.0, old_qty - qty)
                new_cost = max(0.0, old_cost - cost_removed)
                if old_qty <= 0:
                    fee_known = False

            new_fees = old_fees + fee_value_quote
            conn.execute(
                """INSERT INTO accounting_positions(asset,quantity,cost_basis_quote,
                   realized_pnl_quote,fees_quote,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(asset) DO UPDATE SET
                   quantity=excluded.quantity,cost_basis_quote=excluded.cost_basis_quote,
                   realized_pnl_quote=excluded.realized_pnl_quote,fees_quote=excluded.fees_quote,
                   updated_at=excluded.updated_at""",
                (asset, new_qty, new_cost, old_realized + realized_delta, new_fees, now),
            )
            conn.commit()

        return {
            "status": "recorded",
            "asset": asset,
            "quantity": qty,
            "price": price,
            "fee": fee,
            "fee_currency": fee_currency,
            "fee_known": fee_known,
            "accounting_complete": fee_known and (side == "buy" or old_qty > 0),
            "realized_pnl_quote": realized_delta,
        }

    def snapshot(self, prices: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
        prices = prices or {}
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM accounting_positions ORDER BY asset").fetchall()
            incomplete_fills = conn.execute("SELECT COUNT(*) FROM accounting_fills WHERE accounting_complete=0").fetchone()[0]
        assets = []
        realized = 0.0
        fees = 0.0
        unrealized = 0.0
        complete = incomplete_fills == 0
        for row in rows:
            asset = row["asset"]
            qty = float(row["quantity"])
            cost = float(row["cost_basis_quote"])
            price = _num(prices.get(asset))
            upnl = (qty * price - cost) if price is not None and price > 0 else None
            if upnl is None:
                complete = False if qty > 0 else complete
            else:
                unrealized += upnl
            realized += float(row["realized_pnl_quote"])
            fees += float(row["fees_quote"])
            assets.append({
                "asset": asset, "quantity": qty, "cost_basis_quote": cost,
                "average_cost": (cost / qty if qty > 0 else 0.0),
                "price": price, "unrealized_pnl_quote": upnl,
                "realized_pnl_quote": float(row["realized_pnl_quote"]),
                "fees_quote": float(row["fees_quote"]),
            })
        return {
            "assets": assets,
            "realized_pnl_quote": realized,
            "unrealized_pnl_quote": unrealized,
            "fees_quote": fees,
            "accounting_complete": complete,
        }

    def reconcile_wallet(self, wallet: Dict[str, float], tolerance: float = 1e-10) -> Dict[str, Any]:
        snap = self.snapshot()
        ledger = {a["asset"]: a["quantity"] for a in snap["assets"]}
        discrepancies = []
        for asset in sorted(set(wallet) | set(ledger)):
            if asset in (self.quote, "RLS", "IRR"):
                continue
            exchange_qty = float(wallet.get(asset, 0.0) or 0.0)
            ledger_qty = float(ledger.get(asset, 0.0) or 0.0)
            if abs(exchange_qty - ledger_qty) > tolerance:
                discrepancies.append({
                    "asset": asset, "exchange_quantity": exchange_qty,
                    "ledger_quantity": ledger_qty,
                    "difference": exchange_qty - ledger_qty,
                })
        return {
            "accounting_complete": snap["accounting_complete"],
            "discrepancies": discrepancies,
            "reconciled": not discrepancies,
        }
