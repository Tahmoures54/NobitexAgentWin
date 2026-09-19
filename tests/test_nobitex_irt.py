import base64
import json
import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from trading.nobitex_client import USER_AGENT, NobitexClient, _urlsafe_b64decode


def _client():
    key = Ed25519PrivateKey.generate()
    private_b64 = base64.urlsafe_b64encode(key.private_bytes_raw()).decode()
    public_b64 = base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode()
    return NobitexClient(
        api_key=public_b64,
        api_secret=private_b64,
        testnet=False,
        quote_currency="IRT",
    )


class _Ok:
    status_code = 200
    content = b"{}"

    def json(self):
        return {"status": "ok"}


def test_nobitex_uses_production_apiv2_and_ed25519_headers(monkeypatch):
    client = _client()
    captured = {}

    class Response(_Ok):
        def json(self):
            return {"status": "ok", "orders": []}

    def fake_get(url, headers=None, timeout=None):
        captured.update(method="GET", url=url, headers=headers, data=None)
        return Response()

    monkeypatch.setattr(client._session, "get", fake_get)
    client._request("GET", "/market/orders/list", query_params={"status": "open"}, signed=True)

    assert client.BASE_URL == "https://apiv2.nobitex.ir"
    assert captured["url"].startswith("https://apiv2.nobitex.ir/market/orders/list")
    assert captured["headers"]["Nobitex-Key"]
    assert captured["headers"]["Nobitex-Signature"]
    assert captured["headers"]["Nobitex-Timestamp"]
    assert captured["headers"]["User-Agent"].startswith("TraderBot/CryptoScanner-")
    assert captured["headers"]["User-Agent"] == USER_AGENT
    assert "Authorization" not in captured["headers"]
    assert "Content-Type" not in captured["headers"]


def test_sign_request_matches_official_payload_formula():
    client = _client()
    timestamp = "1700000000"
    method = "POST"
    full_path = "/market/orders/cancel-old"
    body = json.dumps({"hours": 2.4}, separators=(",", ":"))
    signature_b64 = client._sign_request(timestamp, method, full_path, body)
    payload = f"{timestamp}{method}{full_path}{body}".encode()
    public = Ed25519PublicKey.from_public_bytes(_urlsafe_b64decode(client.api_key))
    public.verify(_urlsafe_b64decode(signature_b64), payload)


def test_private_key_without_padding_still_loads():
    key = Ed25519PrivateKey.generate()
    private_b64 = base64.urlsafe_b64encode(key.private_bytes_raw()).decode().rstrip("=")
    public_b64 = base64.urlsafe_b64encode(key.public_key().public_bytes_raw()).decode().rstrip("=")
    client = NobitexClient(
        api_key=public_b64,
        api_secret=private_b64,
        testnet=False,
        quote_currency="IRT",
    )
    assert client.auth_method == "api_key"
    assert client._sign_request("1", "GET", "/market/stats", "")


def test_nobitex_balance_uses_rial_wallet_and_exposes_irt_alias(monkeypatch):
    client = _client()
    captured = {}

    class Response(_Ok):
        def json(self):
            return {
                "status": "ok",
                "wallets": [
                    {
                        "currency": "rls",
                        "balance": "125000000",
                        "blockedBalance": "1000000",
                        "activeBalance": "124000000",
                    },
                    {"currency": "btc", "balance": "0.0123"},
                ],
            }

    def fake_request(method, url, headers=None, data=None, timeout=None):
        captured.update(url=url, headers=headers, method=method, data=data)
        return Response()

    monkeypatch.setattr(client._session, "request", fake_request)
    assert client.get_balance("IRT") == 124000000.0
    assert client.get_balance("RLS") == 124000000.0
    assert captured["url"] == "https://apiv2.nobitex.ir/users/wallets/list"
    assert captured["method"] == "POST"
    assert json.loads(captured["data"]) == {"type": "spot"}
    assert captured["headers"]["User-Agent"] == USER_AGENT
    assert "Authorization" not in captured["headers"]


def test_wallet_spendable_subtracts_blocked_when_active_missing(monkeypatch):
    client = _client()

    class Response(_Ok):
        def json(self):
            return {
                "status": "ok",
                "wallets": [
                    {"currency": "rls", "balance": "125000000", "blockedBalance": "1000000"},
                ],
            }

    monkeypatch.setattr(client._session, "request", lambda *a, **k: Response())
    assert client.get_balance("IRT") == 124000000.0


def test_all_irt_market_stats_are_normalized(monkeypatch):
    client = _client()
    def fake_get(url, headers=None, timeout=None):
        class Response(_Ok):
            def json(self):
                return {"status":"ok","stats":{
                    "vtho-rls":{"isClosed":False,"latest":"1200","bestBuy":"1199","bestSell":"1201","volumeDst":"50000000","dayChange":"8.2"},
                    "btc-usdt":{"isClosed":False,"latest":"100","dayChange":"2"},
                }}
        return Response()
    monkeypatch.setattr(client._session, "get", fake_get)
    rows = client.get_all_market_stats("IRT")
    assert len(rows) == 1
    assert rows[0]["Symbol"] == "VTHO"
    assert rows[0]["Pair"] == "VTHOIRT"
    assert rows[0]["24h Change (%)"] == 8.2


def test_low_price_stop_price_keeps_precision(monkeypatch):
    client = _client()
    captured = {}
    class Response(_Ok):
        def json(self):
            return {
                "status": "ok",
                "order": {
                    "id": 123,
                    "status": "Inactive",
                    "execution": "StopMarket",
                    "amount": "1000",
                    "matchedAmount": "0",
                    "price": "market",
                    "market": "VTHO-RLS",
                },
            }
    def fake_request(method, url, headers=None, data=None, timeout=None):
        captured["data"] = json.loads(data)
        captured["headers"] = headers
        return Response()
    monkeypatch.setattr(client._session, "request", fake_request)
    order = client.place_order("VTHOIRT", "sell", "stop_market", 1000, stop_price=0.00067225)
    assert captured["data"]["stopPrice"] == "0.00067225"
    assert "price" not in captured["data"]
    assert captured["data"]["execution"] == "stop_market"
    assert captured["data"]["dstCurrency"] == "rls"
    assert re.fullmatch(r"[A-Za-z0-9-]{1,32}", captured["data"]["clientOrderId"])
    assert order["status"] == "open"
    assert order["symbol"] == "VTHOIRT"


def test_open_orders_use_documented_query_params(monkeypatch):
    client = _client()
    captured = {}

    class Response(_Ok):
        def json(self):
            return {"status": "ok", "orders": []}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return Response()

    monkeypatch.setattr(client._session, "get", fake_get)
    client.get_open_orders("BTCIRT")
    assert "/market/orders/list?" in captured["url"]
    assert "status=open" in captured["url"]
    assert "details=2" in captured["url"]
    assert "tradeType=spot" in captured["url"]
    assert "srcCurrency=btc" in captured["url"]
    assert "dstCurrency=rls" in captured["url"]
    assert "market=" not in captured["url"]


def test_http_200_failed_status_raises():
    client = _client()

    class Response(_Ok):
        def json(self):
            return {"status": "failed", "code": "InvalidSignature", "message": "bad"}

    client._session.get = lambda *a, **k: Response()
    try:
        client._request("GET", "/market/orders/list", signed=True)
        assert False, "expected AuthenticationError"
    except Exception as exc:
        from trading.exceptions import AuthenticationError
        assert isinstance(exc, AuthenticationError)


def test_bare_base_symbols_resolve_to_irt_pairs():
    client = _client()
    assert client.resolve_symbol("PROM") == "PROMIRT"
    assert client.resolve_symbol("DOGE") == "DOGEIRT"
    assert client.resolve_symbol("BTC") == "BTCIRT"
    assert client.resolve_symbol("promirt") == "PROMIRT"
    assert client.resolve_symbol("BTC/USDT") == "BTCIRT"
    assert client._split_symbol("PROM") == ("PROM", "IRT")
    assert client._split_symbol("PROMIRT") == ("PROM", "IRT")
    assert client._split_symbol("DOGE") != ("D", "OGE")
    assert client._normalize_market_symbol("BTC-RLS") == "BTCIRT"
    assert client._normalize_market_symbol("BTC-USDT") == "BTCUSDT"


def test_is_symbol_supported_queries_full_base(monkeypatch):
    client = _client()
    captured = {}

    class Response(_Ok):
        def json(self):
            return {
                "status": "ok",
                "stats": {
                    "prom-rls": {"isClosed": False, "latest": "1"},
                },
            }

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        return Response()

    monkeypatch.setattr(client._session, "get", fake_get)
    assert client.is_symbol_supported("PROM") is True
    assert "srcCurrency=prom" in captured["url"]
    assert "p-rls" not in captured["url"]


def test_trader_appends_irt_to_bare_base():
    from trading.trader import TradingBot

    class Dummy:
        exchange_name = "nobitex"
        nobitex_market = "IRT"
        quote_currency = "IRT"

    bot = Dummy()
    assert TradingBot.normalize_symbol_for_execution(bot, "PROM") == "PROMIRT"
    assert TradingBot.normalize_symbol_for_execution(bot, "DOGE") == "DOGEIRT"
    assert TradingBot.normalize_symbol_for_execution(bot, "BTCIRT") == "BTCIRT"


def test_bot_settings_describe_nobitex_key_fields():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "gui/panels/real_trading_panel.py").read_text(encoding="utf-8")
    assert "Nobitex public key" in src
    assert "Nobitex private key" in src
    assert "READ,TRADE" in src
    assert "WITHDRAW" in src
    assert "تومان" not in src
    assert "execution_mode" in src
    assert "Switch to Live" in src


def test_parse_order_active_unmatched_is_open_not_filled():
    client = _client()
    parsed = client._parse_order({
        "status": "Active",
        "matchedAmount": "0",
        "unmatchedAmount": "15.34",
        "amount": "15.34",
        "id": 6212089660,
        "execution": "Market",
        "type": "buy",
        "market": "XTZ-RLS",
        "averagePrice": "0",
    })
    assert parsed["status"] == "open"
    assert parsed["matched_amount"] == 0.0
    assert parsed["order_id"] == 6212089660


def test_parse_order_done_with_zero_matched_is_not_filled():
    client = _client()
    parsed = client._parse_order({
        "status": "Done",
        "matchedAmount": "0",
        "unmatchedAmount": "15.34",
        "amount": "15.34",
        "id": 99,
        "execution": "Market",
        "type": "buy",
        "market": "XTZ-RLS",
    })
    assert parsed["status"] != "filled"
    assert parsed["matched_amount"] == 0.0


def test_wallet_total_includes_blocked_irt():
    from trading.nobitex_client import _wallet_spendable, _wallet_total

    wallet = {
        "balance": "38183865.13",
        "blockedBalance": "11108614.4",
        "activeBalance": "27075250.73",
    }
    assert abs(_wallet_total(wallet) - 38183865.13) < 0.01
    assert abs(_wallet_spendable(wallet) - 27075250.73) < 0.01


def test_get_balance_total_keeps_locked_irt(monkeypatch):
    client = _client()
    wallets = {
        "status": "ok",
        "wallets": [
            {
                "currency": "rls",
                "balance": "38183865.13",
                "blockedBalance": "11108614.4",
                "activeBalance": "27075250.73",
            }
        ],
    }

    monkeypatch.setattr(client, "_request", lambda *a, **k: wallets)
    client.invalidate_balance_cache()
    assert abs(client.get_balance("IRT") - 27075250.73) < 0.01
    assert abs(client.get_balance_total("IRT") - 38183865.13) < 0.01
