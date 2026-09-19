# Changelog

## [6.8.0] — Actual-fill accounting + portfolio reconciliation

### Execution accounting
- Actual matched quantity and execution price are recorded; intended order values are not treated as fills.
- Reported Nobitex fees are captured when available; missing fee data keeps accounting explicitly incomplete.
- Weighted-average spot cost basis with realized and unrealized P&L in IRT.
- Idempotent handling of cumulative/partial order fills.
- Wallet-vs-ledger reconciliation detects external, missing, or phantom holdings.
- Accounting is refreshed after order mutations and periodically from the live Nobitex wallet.
- Live panel exposes realized P&L, unrealized P&L, fees, completeness, reconciliation state, and refresh time.

### Production safety
- Live execution remains opt-in and retains the existing balance, exposure, idempotency, and authentication gates.
- Paper remains the default execution mode.


## [6.2.0] — Production pack (Phase-1 + Phase-2)

### Production hardening (Phase-1)
- Structured JSON / colored logging (`core/logger.py`)
- Client rate limiter Token Bucket (`trading/rate_limiter.py`)
- Retry policy with jitter (`trading/retry_policy.py`)
- SQLite WAL ledger (`core/database.py`)
- Order idempotency guard (`trading/idempotency.py`)
- Graceful shutdown hooks (`core/shutdown.py`)
- Thread health watchdog (`trading/watchdog.py`)
- Unit tests: `tests/test_phase1_hardening.py`

### Strategy & auto risk (Phase-2)
- Technical features: ATR%%, EMA slope, Hurst proxy (`trading/tech_regime.py`)
- Auto strategy selector (`trading/strategy_selector.py`)
- Auto risk: fractional / Kelly-lite, ATR stops, max-DD breaker (`trading/auto_risk.py`)
- Confidence + confluence gate (`trading/confidence.py`)
- Adaptive pipeline wiring (`trading/adaptive_pipeline.py`)
- Tests: `tests/test_phase2_strategy.py`, `tests/test_adaptive_pipeline.py`

### Default trading profile
- Entry on real observed move (~1.5–2%), not micro-noise
- Hard stop on entry (~3%)
- Trailing enabled (activate ~1.5%, distance ~1.2%)
- Take-profit disabled (0) — exit via trailing stop
- Paper mode default; exposure and position caps tightened
- Fee model set closer to IRT taker costs

### Docs
- `PRODUCTION.md` release checklist
- README production notes

## [6.1.x] — Prior Nobitex IRT edition
- Nobitex-only venue isolation
- Momentum engine + SignalTracker safety fixes
- Regime detector v2.2
