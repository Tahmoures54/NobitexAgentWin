# Production readiness — CryptoScanner (Nobitex IRT)

This document is the go-live checklist for operators. Completing it does **not** guarantee profitability.

## Honest status (read this first)

| Capability | Code exists | Wired into main scan/order path |
|------------|-------------|----------------------------------|
| Paper default + SL/trailing | Yes | Yes (SignalTracker + config) |
| RegimeDetector | Yes | Yes (GUI path) |
| Structured logging / shutdown | Yes | Yes (`main.py`) |
| Watchdog | Yes | **Yes** (`main.py` starts it) |
| SQLite `core/database.py` | Yes (tx fixed) | Partial (ledger API; SignalTracker has own DB) |
| Rate limiter | Yes | Run `python tools/wire_nobitex_rate_limiter.py` |
| IdempotencyGuard | Yes | **Not yet** on place_order path |
| AdaptivePipeline / Confidence / AutoRisk | Yes | **Not default GUI path** — integrate optionally |
| Event-driven historical backtester | **No** | Use performance analytics only |
| Cost gate (fee+spread+slip) | Yes | In `ConfidenceScorer` when pipeline used |
| Scan journal | Yes | `trading/scan_journal.py` (opt-in) |

**Do not market “full backtesting” or “AI learner” until those engines are real and on by default.**

## Release identity

| Field | Value |
|-------|--------|
| Product | CryptoScanner — Nobitex IRT Edition |
| Version | 6.2.x |
| Default execution | **Paper** |
| Venue | Nobitex spot IRT only |

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
