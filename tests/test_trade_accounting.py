import os
import tempfile
import unittest

from trading.trade_accounting import TradeAccounting


class TradeAccountingTests(unittest.TestCase):
    def make(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return TradeAccounting(path, quote="IRT")

    def test_buy_sell_with_quote_fees_and_realized_pnl(self):
        a = self.make()
        buy = a.ingest_order({
            "order_id": "1", "client_order_id": "c1", "symbol": "BTCIRT",
            "side": "buy", "executed_qty": 2, "executed_price": 100,
            "fee": 2, "fee_currency": "IRT",
        })
        self.assertTrue(buy["accounting_complete"])
        sell = a.ingest_order({
            "order_id": "2", "client_order_id": "c2", "symbol": "BTCIRT",
            "side": "sell", "executed_qty": 1, "executed_price": 120,
            "fee": 1, "fee_currency": "IRT",
        })
        self.assertTrue(sell["accounting_complete"])
        snap = a.snapshot({"BTC": 130})
        self.assertEqual(snap["assets"][0]["quantity"], 1)
        self.assertAlmostEqual(snap["realized_pnl_quote"], 18)
        self.assertAlmostEqual(snap["unrealized_pnl_quote"], 29)

    def test_base_fee_is_valued_and_duplicate_is_idempotent(self):
        a = self.make()
        order = {
            "order_id": "10", "symbol": "ETHIRT", "side": "buy",
            "executed_qty": 10, "executed_price": 50,
            "fee": 0.1, "fee_currency": "ETH",
        }
        first = a.ingest_order(order)
        second = a.ingest_order(order)
        self.assertTrue(first["accounting_complete"])
        self.assertEqual(second["status"], "duplicate")
        snap = a.snapshot({"ETH": 60})
        self.assertAlmostEqual(snap["assets"][0]["cost_basis_quote"], 505)
        self.assertAlmostEqual(snap["assets"][0]["unrealized_pnl_quote"], 95)

    def test_partial_sell_reduces_weighted_cost(self):
        a = self.make()
        a.ingest_order({
            "order_id": "20", "symbol": "ABCIRT", "side": "buy",
            "executed_qty": 4, "executed_price": 100,
            "fee": 0, "fee_currency": "IRT",
        })
        result = a.ingest_order({
            "order_id": "21", "symbol": "ABCIRT", "side": "sell",
            "executed_qty": 1, "executed_price": 110,
            "fee": 0, "fee_currency": "IRT",
        })
        self.assertTrue(result["accounting_complete"])
        snap = a.snapshot({"ABC": 110})
        self.assertAlmostEqual(snap["assets"][0]["quantity"], 3)
        self.assertAlmostEqual(snap["assets"][0]["cost_basis_quote"], 300)
        self.assertAlmostEqual(snap["realized_pnl_quote"], 10)

    def test_missing_fee_marks_accounting_incomplete(self):
        a = self.make()
        result = a.ingest_order({
            "order_id": "30", "symbol": "XYZIRT", "side": "buy",
            "executed_qty": 1, "executed_price": 100,
        })
        self.assertFalse(result["accounting_complete"])
        self.assertFalse(a.snapshot()["accounting_complete"])

    def test_cumulative_partial_fill_does_not_double_count_position(self):
        a = self.make()
        first = a.ingest_order({
            "order_id": "50", "symbol": "BTCIRT", "side": "buy",
            "executed_qty": 1, "executed_price": 100,
            "fee": 1, "fee_currency": "IRT",
        })
        second = a.ingest_order({
            "order_id": "50", "symbol": "BTCIRT", "side": "buy",
            "executed_qty": 2, "executed_price": 110,
            "fee": 2, "fee_currency": "IRT",
        })
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "recorded")
        snap = a.snapshot({"BTC": 110})
        self.assertAlmostEqual(snap["assets"][0]["quantity"], 2)
        self.assertAlmostEqual(snap["assets"][0]["cost_basis_quote"], 222)
        self.assertAlmostEqual(snap["unrealized_pnl_quote"], -2)

    def test_cumulative_partial_sell_uses_delta_quantity(self):
        a = self.make()
        a.ingest_order({
            "order_id": "70", "symbol": "BTCIRT", "side": "buy",
            "executed_qty": 20, "executed_price": 100,
            "fee": 0, "fee_currency": "IRT",
        })
        first = a.ingest_order({
            "order_id": "71", "symbol": "BTCIRT", "side": "sell",
            "executed_qty": 5, "executed_price": 110,
            "fee": 0, "fee_currency": "IRT",
        })
        second = a.ingest_order({
            "order_id": "71", "symbol": "BTCIRT", "side": "sell",
            "executed_qty": 10, "executed_price": 120,
            "fee": 0, "fee_currency": "IRT",
        })
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["status"], "recorded")
        snap = a.snapshot({"BTC": 120})
        self.assertAlmostEqual(snap["assets"][0]["quantity"], 10)
        self.assertAlmostEqual(snap["assets"][0]["cost_basis_quote"], 1000)
        self.assertAlmostEqual(snap["realized_pnl_quote"], 150)

    def test_zero_fee_is_a_known_fee(self):
        a = self.make()
        result = a.ingest_order({
            "order_id": "60", "symbol": "ETHIRT", "side": "buy",
            "executed_qty": 1, "executed_price": 100,
            "fee": 0, "fee_currency": "IRT",
        })
        self.assertTrue(result["accounting_complete"])
        self.assertAlmostEqual(a.snapshot()["fees_quote"], 0)

    def test_wallet_reconciliation_detects_external_or_missing_holdings(self):

        a = self.make()
        a.ingest_order({
            "order_id": "40", "symbol": "BTCIRT", "side": "buy",
            "executed_qty": 1, "executed_price": 100,
            "fee": 0, "fee_currency": "IRT",
        })
        report = a.reconcile_wallet({"BTC": 1.25})
        self.assertFalse(report["reconciled"])
        self.assertAlmostEqual(report["discrepancies"][0]["difference"], 0.25)


if __name__ == "__main__":
    unittest.main()
