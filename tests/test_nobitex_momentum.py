from trading.nobitex_momentum_engine import NobitexMomentumEngine

def row(symbol="ABC", price=100.0, bid=99.8, ask=100.0, volume=500_000_000.0, ch24=2.0):
    return {
        "Symbol": symbol, "Pair": symbol + "IRT", "Price": price,
        "Bid": bid, "Ask": ask, "Volume": volume, "24h Change (%)": ch24,
        "asks": [[ask, 10_000_000.0]],
    }

def test_engine_waits_for_observed_history():
    e=NobitexMomentumEngine(min_observed_move_pct=0.5, pump_threshold_pct=0.5,
                             min_volume_irt=1.0, min_confirm_scans=1)
    assert e.evaluate([row()], now=1.0) == []
    hits=e.evaluate([row(price=101.0, bid=100.8, ask=101.0)], now=11.0)
    assert hits and hits[0]["ExecutionVenue"]=="Nobitex"

def test_engine_confirms_discovered_mover_before_entry_gates():
    e=NobitexMomentumEngine(min_observed_move_pct=3.0, pump_threshold_pct=3.0,
                             min_volume_irt=400_000_000.0, min_confirm_scans=3)
    # Discovery must survive pre-entry liquidity failures while the mover is
    # being confirmed across scans.  Only the third confirming scan reaches
    # the executable entry gates.
    e.evaluate([row(volume=1.0)], now=1.0)
    e.evaluate([row(price=104.0, bid=103.8, ask=104.0, volume=1.0)], now=2.0)
    e.evaluate([row(price=105.0, bid=104.8, ask=105.0, volume=500_000_000.0)], now=3.0)
    hits=e.evaluate([row(price=106.0, bid=105.8, ask=106.0, volume=500_000_000.0)], now=4.0)
    assert hits and hits[0]["TrendConfirmScans"] == 3
    assert e.last_stats["confirm"] == 0


def test_engine_rejects_wide_spread():
    e=NobitexMomentumEngine(min_observed_move_pct=0.1, pump_threshold_pct=0.1,
                             min_volume_irt=1.0, max_spread_pct=0.5)
    e.evaluate([row()], now=1.0)
    assert e.evaluate([row(price=102.0, bid=100.0, ask=102.0)], now=2.0) == []

def test_engine_rejects_missing_depth_when_depth_filter_is_enabled():
    e=NobitexMomentumEngine(min_observed_move_pct=0.1, pump_threshold_pct=0.1,
                             min_volume_irt=1.0, min_ask_depth_quote=1_000_000)
    e.evaluate([row()], now=1.0)
    missing_depth = row(price=102.0, bid=101.8, ask=102.0)
    missing_depth.pop("asks")
    assert e.evaluate([missing_depth], now=2.0) == []


def test_engine_can_defer_depth_until_execution_layer():
    e=NobitexMomentumEngine(min_observed_move_pct=0.1, pump_threshold_pct=0.1,
                             min_volume_irt=1.0, min_ask_depth_quote=1_000_000)
    e.evaluate([row()], now=1.0)
    missing_depth = row(price=102.0, bid=101.8, ask=102.0)
    missing_depth.pop("asks")
    hits=e.evaluate([missing_depth], now=2.0, require_depth=False)
    assert hits and hits[0]["Pair"] == "ABCIRT"
