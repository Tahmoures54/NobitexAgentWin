import pandas as pd

from trading.nobitex_client import NobitexClient


def _client_for_snapshot():
    client = NobitexClient(api_key="", api_secret="")
    client.get_order_book = lambda symbol, limit=50: {
        "bids": [["100", "10"], ["99", "5"]],
        "asks": [["101", "2"], ["102", "3"]],
    }
    client.get_buy_sell_pressure = lambda symbol, limit=50: {
        "trades_count": 10,
        "buy_volume": 70.0,
        "sell_volume": 30.0,
        "buy_pressure_pct": 70.0,
        "sell_pressure_pct": 30.0,
        "buy_sell_ratio": 70.0 / 30.0,
        "last_trade_type": "buy",
        "last_trade_price": 100.5,
    }
    candles = []
    price = 100.0
    for i in range(60):
        close = price + i * 0.5
        candles.append({
            "timestamp": i,
            "open": close - 0.2,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 100.0 + i,
        })
    client.get_klines = lambda symbol, interval="5m", limit=100: candles[:limit]
    client._request = lambda *args, **kwargs: {
        "stats": {
            "btc-rls": {
                "latest": "120",
                "bestBuy": "119",
                "bestSell": "121",
                "volumeDst": "5000000",
                "dayChange": "4.2",
                "dayOpen": "115",
                "dayHigh": "125",
                "dayLow": "110",
            }
        }
    }
    return client


def test_market_intelligence_is_read_only_and_calculates_core_metrics():
    client = _client_for_snapshot()
    snapshot = client.get_market_intelligence("BTC", candle_limit=60)

    assert snapshot["symbol"] == "BTC"
    assert snapshot["price"] == 120.0
    assert snapshot["buy_pressure_pct"] == 70.0
    assert snapshot["sell_pressure_pct"] == 30.0
    assert snapshot["orderbook_imbalance_pct"] > 0
    assert snapshot["rsi"] is not None
    assert snapshot["ema9"] is not None
    assert snapshot["ema21"] is not None
    assert snapshot["macd"] is not None
    assert snapshot["volume_ratio"] is not None
    assert snapshot["momentum_5m_pct"] > 0
