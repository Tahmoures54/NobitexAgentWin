# CryptoScanner 6.9.0 — Nobitex IRT Edition

CryptoScanner is a Windows-friendly spot scanner and trading assistant built specifically for **Nobitex and the IRT market**.

## Important

This project is a trading system, **not a profit guarantee**. Cryptocurrency markets can move rapidly and losses can exceed expectations. The release improves execution discipline and operational safety; it does not promise profitability.

**Production docs:** [PRODUCTION.md](PRODUCTION.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md)

## What this release includes

### v6.9 — Profitability-aware strategy switching

The adaptive layer now uses realized closed-trade performance in addition to market regime. A strategy needs a minimum sample before its results can influence switching; negative realized expectancy can veto a regime-selected strategy in favor of a positive, sufficiently sampled alternative. Switch hysteresis prevents scan-to-scan flapping. The selected strategy is recorded with new trades so future performance is attributable to the strategy that actually generated the entry.

### Venue isolation
Nobitex only: market stats, order book, candles, balances, spot orders, status and cancel.

### Strategy behavior (default profile)
1. Enter on **real observed local move** (default ~1.5–2%), not micro-noise.
2. Reject wide spread, weak volume, and excessive chase.
3. Place a **hard stop** on entry (default ~3%).
4. **Trail the stop** when in profit (activate ~1.5%, distance ~1.2%).
5. Take-profit percent default **0** — primary exit is the trailing stop.
6. BTC dump guard + limited Eagle exception for strong liquid movers.
7. Optional adaptive path: `RegimeDetector` → `StrategySelector` → `ConfidenceScorer` → `AutoRiskEngine` via `AdaptivePipeline`.

### Phase-1 production hardening
Structured logging, rate limiter, retry policy, SQLite ledger, idempotency, graceful shutdown, watchdog.

### Phase-2 adaptive modules
`tech_regime`, `strategy_selector`, `auto_risk`, `confidence`, `adaptive_pipeline`.

## Default risk snapshot (`data/bot_config.json`)

| Setting | Default |
|---------|---------|
| Execution mode | **paper** |
| Position sizing | `risk_percent` (~1% risk/trade) |
| Stop loss | 3% |
| Trailing | on — act 1.5% / dist 1.2% |
| Take profit | 0 (trail-driven exits) |
| Max open positions | 4 |
| Max total exposure | 50% |
| Max new entries / cycle | 1 |
| Fee model (sim) | 0.25% |

Validate on your account size in paper before any live change.

**IRT note:** API amounts are rial-compatible. Do **not** divide order size by 10 before sending to Nobitex.

## Security

- Prefer `NOBITEX_API_KEY` / `NOBITEX_PRIVATE_KEY` or the encrypted local store
- API permissions: **READ + TRADE**, never WITHDRAW
- Keep PC clock in sync
- See [SECURITY.md](SECURITY.md)

## Installation

Python 3.11+ recommended (3.13 supported).

```bat
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run

```bat
py main.py
py main.py --debug --log logs\scanner.log
python -m pytest -q
```

Paper mode is the default. Follow [PRODUCTION.md](PRODUCTION.md) before enabling live.

## Live enable (short)

1. Paper sample with fees included  
2. Key = READ+TRADE only  
3. Confirm balance, stops, exposure caps  
4. Small allocation first  
5. Verify fill + protective stop on first live entry  

## Project layout (abbrev.)

```text
main.py
PRODUCTION.md / SECURITY.md / CHANGELOG.md
core/          # config, logger, database, shutdown
trading/       # nobitex client, momentum, regime, pipeline, risk
gui/
tests/
data/bot_config.json
```

## Operational sequence

**Paper → measure expectancy (after fees) → tune → small live → scale only with evidence**


## v6.8 — Actual-Fill Accounting & P&L

The live trading panel now surfaces accounting derived from actual Nobitex execution data:

- Realized and unrealized P&L in the configured quote currency.
- Reported trading fees, without inventing missing fee data.
- Weighted-average cost basis for spot holdings.
- Wallet-vs-ledger reconciliation and discrepancy count.
- A visible accounting completeness state and last-refresh timestamp.
- Manual refresh plus a background refresh every 30 seconds while the live panel is open.

Accounting is separate from the strategy journal and is intended to reflect exchange execution economics rather than planned order values.

## v6.7 Portfolio Reconciliation

The live Nobitex cycle now treats the exchange wallet as the account source of truth.

- Periodic portfolio reconciliation every few scans (configurable).
- Immediate reconciliation after BUY/close/resize mutations.
- Available and total wallet values are tracked separately.
- Non-quote assets are valued from current Nobitex tickers.
- Open orders are captured with the portfolio snapshot.
- Internal live positions are reconciled against the same wallet snapshot.
- External holdings remain visible as exchange assets rather than being invented as bot trades.
- Missing market prices leave an asset explicitly unpriced; the account is not falsely marked fully valued.
- A failed portfolio read never becomes a zero-balance signal.
- The total-wallet snapshot is reused so one reconciliation cycle does not make one wallet request per asset.

The default cadence is every 3 scans with a minimum 30-second interval. A trade mutation forces an immediate reconciliation.

Live execution remains opt-in and must still pass the existing safety gates.
