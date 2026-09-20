from trading.order_flow import analyze_order_book, entry_gate


def test_order_flow_detects_bid_pressure():
    book = {
        "bids": [[100, 100], [99, 50]],
        "asks": [[101, 20], [102, 20]],
    }
    features = analyze_order_book(book, levels=2)
    assert features["order_flow_valid"] is True
    assert features["order_flow_score"] > 70
    assert features["order_flow_imbalance"] > 0


def test_order_flow_rejects_wide_spread():
    book = {
        "bids": [[100, 10]],
        "asks": [[103, 10]],
    }
    features = analyze_order_book(book)
    allowed, reason = entry_gate(features, max_spread_pct=1.2)
    assert allowed is False
    assert reason == "wide_spread"


def test_order_flow_rejects_weak_buy_pressure():
    book = {
        "bids": [[100, 10]],
        "asks": [[101, 100]],
    }
    features = analyze_order_book(book)
    allowed, reason = entry_gate(features, min_score=58)
    assert allowed is False
    assert reason == "weak_buy_pressure"


def test_order_flow_gate_can_pass():
    book = {
        "bids": [[100, 100]],
        "asks": [[100.2, 10]],
    }
    features = analyze_order_book(book)
    allowed, reason = entry_gate(features, min_score=58, max_spread_pct=1.2)
    assert allowed is True
    assert reason == "ok"
