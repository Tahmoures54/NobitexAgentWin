"""
trading/idempotency.py — Two-phase order protection against duplicate submissions.

Flow:
1. prepare()  → record client_order_id as 'prepared' in DB before network call
2. confirm()  → mark as 'submitted' / 'filled' after successful response
3. On crash/restart, any leftover 'prepared' orders are treated as unknown
   and can be reconciled or cancelled safely.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict, Optional

from core.database import Database

logger = logging.getLogger(__name__)


class IdempotencyGuard:
    """Prevents accidental double-submission of the same logical order."""

    def __init__(self, db: Optional[Database] = None) -> None:
        self.db = db or Database()

    def generate_client_order_id(self, prefix: str = "cs") -> str:
        """Generate a unique client order id."""
        return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"

    def prepare(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> str:
        """
        Phase 1: Persist the intent before touching the network.

        Returns the client_order_id that must be sent to the exchange.
        """
        cid = client_order_id or self.generate_client_order_id()
        existing = self.db.get_order(cid)
        if existing and existing["status"] not in ("failed", "cancelled"):
            logger.warning(
                "Idempotency: order %s already exists with status=%s — refusing duplicate",
                cid, existing["status"],
            )
            raise RuntimeError(
                f"Duplicate order blocked: client_order_id={cid} already in status={existing['status']}"
            )

        self.db.prepare_order(
            client_order_id=cid,
            symbol=symbol,
            side=side,
            order_type=order_type,
            amount=amount,
            price=price,
        )
        logger.debug("Idempotency: prepared order %s %s %s %s", cid, side, amount, symbol)
        return cid

    def confirm(
        self,
        client_order_id: str,
        *,
        exchange_order_id: Optional[str] = None,
        status: str = "submitted",
        filled_amount: Optional[float] = None,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Phase 2: Mark the order as accepted by the exchange."""
        self.db.update_order_status(
            order_id=client_order_id,
            status=status,
            filled_amount=filled_amount,
            raw_response=raw_response,
        )
        logger.debug(
            "Idempotency: confirmed order %s → status=%s exchange_id=%s",
            client_order_id, status, exchange_order_id,
        )

    def mark_failed(
        self,
        client_order_id: str,
        error_message: str,
        raw_response: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record that the submission failed after prepare."""
        self.db.update_order_status(
            order_id=client_order_id,
            status="failed",
            error_message=error_message,
            raw_response=raw_response,
        )
        logger.warning("Idempotency: order %s marked failed: %s", client_order_id, error_message)

    def is_duplicate(self, client_order_id: str) -> bool:
        """Return True if this client_order_id is already known and not failed/cancelled."""
        order = self.db.get_order(client_order_id)
        if order is None:
            return False
        return order["status"] not in ("failed", "cancelled")

    def get_stale_prepared(self, max_age_seconds: float = 120.0) -> list:
        """Return prepared orders older than max_age (possible crash leftovers)."""
        open_orders = self.db.get_open_orders()
        now = time.time()
        stale = []
        for o in open_orders:
            if o["status"] != "prepared":
                continue
            try:
                created = time.mktime(time.strptime(o["created_at"][:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            if now - created > max_age_seconds:
                stale.append(o)
        return stale
