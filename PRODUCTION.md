# Production readiness — CryptoScanner (Nobitex IRT)

This document is the go-live checklist for operators. Completing it does **not** guarantee profitability; it reduces operational and security failures.

## Release identity

| Field | Value |
|-------|--------|
| Product | CryptoScanner — Nobitex IRT Edition |
| Recommended tag | `v6.2.0` |
| Default execution | **Paper** (`data/bot_config.json` → `execution_mode`) |
| Venue | Nobitex spot IRT only |

## Pre-flight checklist

### Security

- [ ] Never commit API keys, private keys, or `.env` files
- [ ] Nobitex API key permissions: **READ + TRADE only** (no WITHDRAW)
- [ ] Private key stored only in encrypted local store or environment variables
- [ ] PC clock synchronized (Ed25519 signing is time-sensitive)
- [ ] Confirm `.gitignore` excludes `data/*.db`, `data/*.enc`, `.env`, keys

### Configuration

- [ ] Review `data/bot_config.json` before first run
- [ ] Keep `execution_mode: "paper"` until at least several dozen closed paper trades
- [ ] Confirm `trading_fee_pct` reflects real IRT costs (default tuned to ~0.25%)
- [ ] Confirm stop / trailing: SL on entry, trail when in profit
- [ ] `max_total_exposure_pct` and `max_open_positions` match account size
- [ ] `halt_on_max_drawdown: true` remains enabled

### Runtime

- [ ] Python 3.11+ (3.13 supported)
- [ ] `pip install -r requirements.txt` in a clean venv
- [ ] `python -m pytest -q` passes on the target machine
- [ ] Log directory writable; optional: `python main.py --log logs/app.log`
- [ ] Network access to `https://apiv2.nobitex.ir`

### Operational discipline

- [ ] Paper → review expectancy (fees included) → small live allocation
- [ ] Do not raise size after a short lucky streak alone
- [ ] Watch rate-limit and auth errors in logs
- [ ] After live enable: verify first fill, stop placement, and wallet reconciliation

## Architecture (production layers)

```
GUI / main.py
    → AdaptivePipeline (optional)
        → RegimeDetector → StrategySelector
        → NobitexMomentumEngine
        → ConfidenceScorer → AutoRiskEngine
    → SignalTracker (entries, SL, trailing, cooldowns, halt)
    → NobitexClient (sign, rate-limit, retry)
    → core: logger, database, shutdown, watchdog
```

### Phase-1 hardening (shipped)

| Module | Path |
|--------|------|
| Structured logging | `core/logger.py` |
| SQLite ledger | `core/database.py` |
| Graceful shutdown | `core/shutdown.py` |
| Rate limiter | `trading/rate_limiter.py` |
| Retry policy | `trading/retry_policy.py` |
| Idempotency | `trading/idempotency.py` |
| Watchdog | `trading/watchdog.py` |

### Phase-2 strategy (shipped)

| Module | Path |
|--------|------|
| Tech features | `trading/tech_regime.py` |
| Strategy selector | `trading/strategy_selector.py` |
| Auto risk | `trading/auto_risk.py` |
| Confidence | `trading/confidence.py` |
| Pipeline glue | `trading/adaptive_pipeline.py` |

## Safe live enable procedure

1. Run paper with current `bot_config.json` for a meaningful sample.
2. Export / review closed trades (win rate, average win/loss, fees).
3. If expectancy after fees is acceptable, set `execution_mode` to `live`.
4. Start with reduced `risk_per_trade_pct` and `max_total_exposure_pct`.
5. Confirm one live entry: fill, protective stop, balance update.
6. Keep daily loss awareness; halt remains tied to max drawdown.

## Known operator notes

- **IRT units:** API amounts are rial-compatible; do not divide by 10 before sending orders.
- **Fee model:** Config `trading_fee_pct` is for sizing/simulation realism; exchange bills actual tier fees.
- **Large client file:** If `nobitex_client` rate-limiter wiring was applied manually, re-verify after upgrades.
- **Local paths:** `trade_log_file` should stay project-relative (`data/...`) for portability.

## Support commands

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q
python main.py
python main.py --debug --log logs\scanner.log
```

## Disclaimer

This software is a trading assistant. Markets can gap, APIs can fail, and losses can exceed expectations. Operators are responsible for keys, capital, and compliance with local rules.
