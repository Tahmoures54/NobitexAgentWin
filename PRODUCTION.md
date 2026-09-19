# Production readiness — CryptoScanner (Nobitex IRT)

This document is the go-live checklist for operators. Completing it does **not** guarantee profitability.

## Honest status (read this first)

| Capability | Code exists | Wired into main scan/order path |
|------------|-------------|----------------------------------|
| Paper default + SL/trailing | Yes | Yes (SignalTracker + config) |
| RegimeDetector | Yes | Yes (GUI path) |
| Structured logging / shutdown | Yes | Yes (`main.py`) |
| Watchdog | Yes | **Yes** (`main.py` starts it) |
| SQLite `core/database.py` | Yes (tx fixed) | Partial (plus actual-fill accounting ledger) |
| Rate limiter | Yes | Run `python tools/wire_nobitex_rate_limiter.py` |
| IdempotencyGuard | Yes | **Yes** (`TradingBot.place_order`) |
| AdaptivePipeline / Confidence / AutoRisk | Yes | **Not default GUI path** — integrate optionally |
| Event-driven historical backtester | **No** | Use performance analytics only |
| Cost gate (fee+spread+slip) | Yes | In `ConfidenceScorer` when pipeline used |
| Scan journal | Yes | `trading/scan_journal.py` (opt-in) |

**Do not market “full backtesting” or “AI learner” until those engines are real and on by default.**

## Release identity

| Field | Value |
|-------|--------|
| Product | CryptoScanner — Nobitex IRT Edition |
| Version | 7.0.0 |
| Default execution | **Paper** |
| Venue | Nobitex spot IRT only |


## v7.0 L2 order-flow microstructure gate\n\nThe live candidate path can now require visible Nobitex bid-side pressure before a BUY is allowed. The feature layer is deterministic and interpretable; it does not replace the existing strategy selector or risk engine.\n\nDefault controls: `order_flow_enabled=true`, `order_flow_levels=10`, `order_flow_min_score=58`, `order_flow_max_spread_pct=1.2`. These thresholds must be validated on real Nobitex data before changing live allocation.\n\n## v6.9 profitability-aware strategy switching

The automatic strategy layer now combines market regime with realized closed-trade expectancy. The system requires a minimum sample of 8 closed trades before realized strategy performance can veto a regime-selected strategy. A negative-expectancy candidate can be replaced by a sufficiently sampled positive-expectancy alternative. This is a control mechanism, not a profitability guarantee.

The strategy label is stored with new trades through `entry_indicators`, allowing performance to be attributed to the strategy that generated the entry. Strategy switching is deliberately conservative and uses hysteresis to avoid rapid scan-to-scan changes.

## v6.8 actual-fill accounting

The production branch now includes a separate exchange-execution accounting ledger for Nobitex spot:

- Actual matched quantity and execution price only; no estimated fills are booked.
- Reported execution fees are stored when the exchange provides them.
- Weighted-average cost basis with realized and unrealized P&L in IRT.
- Cumulative/partial fills are idempotent.
- Wallet-vs-ledger reconciliation is visible and flags discrepancies.
- Live dashboard exposes accounting completeness, P&L, fees, reconciliation state, and refresh time.

If Nobitex omits a required execution field such as a fee, the ledger remains explicitly incomplete rather than inventing a value.

## Pre-flight checklist

### Security
- [ ] No API keys in git
- [ ] READ + TRADE only (no WITHDRAW)
- [ ] Clock synced

### Config (pre-sample conservative)
- [ ] `execution_mode: paper`
- [ ] `risk_per_trade_pct` ≤ 0.5
- [ ] `max_total_exposure_pct` ≤ 30
- [ ] `max_open_positions` ≤ 2
- [ ] Fee model ≥ real IRT taker round-trip

### Hardening commands
```bat
pip install -r requirements.txt
python tools/wire_nobitex_rate_limiter.py
python -m pytest -q
python main.py
```

### Before any live capital
1. All tests green locally and on CI
2. Paper expectancy **after fees** positive
3. Rate limiter confirmed in client
4. Tiny live size only

## Priority roadmap

1. DB transaction fix — **done**
2. Rate limiter wire script — **done** (run once)
3. Watchdog on boot — **done**
4. Cost gate in ConfidenceScorer — **done**
5. CI workflow — **done**
6. Idempotency on all order mutations — pending
7. AdaptivePipeline as single decision path in GUI — pending
8. Real event-driven backtest + walk-forward — pending

## Disclaimer

Trading involves loss of capital. This software is an assistant, not a guarantee.


## Portfolio reconciliation — v6.7

The main Nobitex live cycle now performs exchange-to-ledger reconciliation instead of treating the internal trade database as the complete account state.

### Cycle behavior

1. Every configured number of live scans, refresh the Nobitex wallet, current asset valuations, and open orders.
2. Reuse that wallet snapshot when reconciling open live positions.
3. Detect phantom/partial internal positions without issuing a second wallet request.
4. After a BUY, fill/close, stop/trailing exit, or resize mutation, force an immediate portfolio reconciliation.
5. Preserve unpriced assets as valuation_complete=false; never convert missing market data to zero value.
6. Keep live execution gated when authentication, balance, exposure, or other safety checks fail.

### Default cadence

- portfolio_reconcile_every_scans = 3
- portfolio_reconcile_min_interval_seconds = 30

The cadence can be changed in bot_config.json.

### End-to-end chain

The intended live control chain is now:

Nobitex market scan → candidate → BUY → fill confirmation → protective stop → trailing management → SELL/exit → wallet refresh → ledger reconciliation

This is an execution-integrity feature, not a profitability guarantee. Live trading should remain on small allocation until the complete chain has been observed on the real account and the resulting ledger/portfolio snapshots have been checked.
