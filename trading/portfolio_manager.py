"""
Portfolio reconciliation and valuation for Nobitex spot accounts.

Nobitex is the source of truth for wallet balances and open orders.
The bot's trade ledger remains useful for strategy history, but it must
not invent account holdings that are absent from the exchange.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from core.database import Database

logger = logging.getLogger(__name__)


def _f(value: Any, default: float = 0.0) -> float:
    try:
        x = float(value)
        return x if x == x else default
    except (TypeError, ValueError):
        return default


class NobitexPortfolioManager:
    """Build a reconciled spot portfolio snapshot from Nobitex."""

    def __init__(self, exchange: Any, db: Optional[Database] = None, quote: str = "IRT"):
        self.exchange = exchange
        self.db = db or Database()
        self.quote = "IRT" if str(quote or "IRT").upper() in ("IRT", "RLS", "IRR") else str(quote).upper()
        self.last_snapshot: Optional[Dict[str, Any]] = None

    def refresh(self, *, force: bool = True, include_orders: bool = True) -> Dict[str, Any]:
        """Fetch wallets, value non-quote assets, and reconcile open orders."""
        balances = self.exchange.get_balances(force_refresh=force)
        if balances is None:
            raise RuntimeError("Nobitex returned no wallet data.")

        # Preserve both available and total where the adapter exposes totals.
        totals: Dict[str, float] = {}
        total_reader = getattr(self.exchange, "get_balance_total_fresh", None)
        if callable(total_reader):
            for asset in list(balances):
                try:
                    totals[asset] = _f(total_reader(asset), balances.get(asset, 0.0))
                except Exception:
                    totals[asset] = _f(balances.get(asset, 0.0))

        assets: List[Dict[str, Any]] = []
        quote_available = 0.0
        quote_total = 0.0

        seen = set()
        for raw_asset, raw_available in balances.items():
            asset = str(raw_asset or "").upper()
            if not asset or asset in seen or asset in ("RLS", "IRR") and "IRT" in balances:
                continue
            if asset == "RLS":
                asset = "IRT"
            if asset in seen:
                continue
            seen.add(asset)

            available = _f(raw_available)
            total = _f(totals.get(raw_asset, available), available)
            if asset == self.quote:
                quote_available = available
                quote_total = total
                continue
            if available <= 0 and total <= 0:
                continue

            symbol = f"{asset}{self.quote}"
            price = 0.0
            try:
                ticker = self.exchange.get_ticker(symbol)
                price = _f(ticker.get("last") or ticker.get("price"))
            except Exception as exc:
                logger.debug("Portfolio price unavailable for %s: %s", symbol, exc)

            assets.append({
                "asset": asset,
                "available": available,
                "total": total,
                "price_quote": price,
                "value_quote": available * price,
                "total_value_quote": total * price,
                "symbol": symbol,
                "valuation_status": "valued" if price > 0 else "unpriced",
            })

        open_orders: List[Dict[str, Any]] = []
        if include_orders:
            try:
                open_orders = self.exchange.get_open_orders()
            except Exception as exc:
                logger.warning("Could not load Nobitex open orders: %s", exc)

        valued_assets = sum(a["value_quote"] for a in assets)
        total_portfolio = quote_available + valued_assets
        total_portfolio_including_locked = quote_total + sum(a["total_value_quote"] for a in assets)
        unpriced_assets = [a["asset"] for a in assets if a["valuation_status"] != "valued"]

        snapshot = {
            "timestamp": time.time(),
            "quote_currency": self.quote,
            "quote_available": quote_available,
            "quote_total": quote_total,
            "assets": assets,
            "open_orders": open_orders,
            "asset_count": len(assets),
            "open_order_count": len(open_orders),
            "valued_assets_quote": valued_assets,
            "portfolio_value_quote": total_portfolio,
            "portfolio_total_value_quote": total_portfolio_including_locked,
            "unpriced_assets": unpriced_assets,
            "valuation_complete": not unpriced_assets,
            "source": "nobitex",
        }
        self.last_snapshot = snapshot
        self._persist(snapshot)
        return snapshot

    def _persist(self, snapshot: Dict[str, Any]) -> None:
        try:
            self.db.set_state("nobitex_portfolio_snapshot", snapshot)
            for asset in snapshot["assets"]:
                self.db.record_balance(
                    asset["asset"],
                    asset["available"],
                    asset["total"],
                )
            self.db.record_balance(
                snapshot["quote_currency"],
                snapshot["quote_available"],
                snapshot["quote_total"],
            )
        except Exception as exc:
            # Persistence failure must not turn a successful exchange read
            # into a false account failure.
            logger.warning("Portfolio snapshot persistence failed: %s", exc)

    def get_snapshot(self) -> Optional[Dict[str, Any]]:
        if self.last_snapshot is not None:
            return self.last_snapshot
        value = self.db.get_state("nobitex_portfolio_snapshot")
        return value if isinstance(value, dict) else None

    def get_asset(self, asset: str) -> Optional[Dict[str, Any]]:
        key = str(asset or "").upper()
        snapshot = self.get_snapshot() or {}
        for item in snapshot.get("assets", []):
            if item.get("asset") == key:
                return item
        return None

    def exposure_pct(self, asset: str) -> Optional[float]:
        snapshot = self.get_snapshot()
        if not snapshot:
            return None
        value = self.get_asset(asset)
        if not value:
            return 0.0
        total = _f(snapshot.get("portfolio_value_quote"))
        if total <= 0:
            return 0.0
        return value["value_quote"] / total * 100.0
