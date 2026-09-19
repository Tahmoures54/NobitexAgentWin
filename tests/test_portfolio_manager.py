from trading.portfolio_manager import NobitexPortfolioManager


class FakeExchange:
    total_snapshot_calls = 0
    def get_balances(self, force_refresh=False):
        return {"IRT": 1000000, "BTC": 0.01, "RLS": 1000000}

    def get_balances_total_snapshot(self):
        type(self).total_snapshot_calls += 1
        return {"IRT": 1200000, "BTC": 0.01, "RLS": 1200000}

    def get_balance_total_fresh(self, asset):
        return {"IRT": 1200000, "BTC": 0.01, "RLS": 1200000}.get(asset, 0)

    def get_ticker(self, symbol):
        return {"BTCIRT": {"last": 50000000}}[symbol]

    def get_open_orders(self):
        return [{"order_id": "1", "symbol": "BTCIRT", "status": "open"}]


def test_portfolio_uses_nobitex_wallet_as_source_of_truth(tmp_path):
    from core.database import Database
    manager = NobitexPortfolioManager(FakeExchange(), Database(str(tmp_path / "db.sqlite")))
    snapshot = manager.refresh()

    assert snapshot["source"] == "nobitex"
    assert snapshot["quote_available"] == 1000000
    assert snapshot["portfolio_value_quote"] == 1500000
    assert snapshot["portfolio_total_value_quote"] == 1700000
    assert snapshot["asset_count"] == 1
    assert snapshot["open_order_count"] == 1
    assert manager.exposure_pct("BTC") > 0
    assert FakeExchange.total_snapshot_calls == 1


class BrokenExchange(FakeExchange):
    def get_balances(self, force_refresh=False):
        return None


def test_portfolio_does_not_treat_missing_wallet_as_zero(tmp_path):
    from core.database import Database
    manager = NobitexPortfolioManager(BrokenExchange(), Database(str(tmp_path / "db.sqlite")))
    try:
        manager.refresh()
    except RuntimeError as exc:
        assert "no wallet data" in str(exc).lower()
    else:
        raise AssertionError("Expected wallet failure")


class PartiallyPricedExchange(FakeExchange):
    def get_balances(self, force_refresh=False):
        return {"IRT": 1000000, "BTC": 0.01, "XYZ": 25.0}

    def get_balances_total_snapshot(self):
        return {"IRT": 1000000, "BTC": 0.01, "XYZ": 25.0}

    def get_ticker(self, symbol):
        if symbol == "BTCIRT":
            return {"last": 50000000}
        raise RuntimeError("ticker unavailable")


def test_unpriced_asset_marks_portfolio_incomplete(tmp_path):
    from core.database import Database
    manager = NobitexPortfolioManager(
        PartiallyPricedExchange(), Database(str(tmp_path / "db.sqlite"))
    )
    snapshot = manager.refresh()

    assert snapshot["valuation_complete"] is False
    assert snapshot["unpriced_assets"] == ["XYZ"]
    assert snapshot["portfolio_value_quote"] == 1500000
    assert snapshot["assets"][1]["valuation_status"] == "unpriced"
