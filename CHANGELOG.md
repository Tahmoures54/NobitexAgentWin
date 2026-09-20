## v6.9.1 — Profitability guards + honest paper economics

Implements the fix list from `PROFITABILITY_ANALYSIS.md` (study re-run on 100 days
of 1-minute data for 18 markets; every realistic arm is still net negative).

### Entry economics
- Cost guard rejects an entry when the observed move is below `min_edge_multiple`
  × the round-trip cost (2 × fee + live spread), or when the trailing gap
  (activation − distance) is smaller than the round-trip cost, i.e. every
  trailing exit would be net negative. Rejections are logged as `cost_guard`.
- `round_trip_cost_pct()` prices the round trip from the live order book
  (Bid/Ask), falling back to the configured half-spread, then to the spread cap.

### Paper-mode honesty
- Simulated fills cross `paper_half_spread_pct` on entry and on every exit
  (take-profit, trailing, hard stop, time stop), so paper P&L no longer assumes
  mid-price fills on both legs.

### Exits
- `max_hold_minutes` time stop (per-trade column honoured, else the tracker
  setting); the shipped profile uses 120 minutes because the study showed
  positions otherwise sitting open for days.
- Expectancy guard: when the mean `pnl_pct_net` of the last
  `expectancy_guard_trades` closed trades is below
  `expectancy_guard_min_expectancy_pct`, new entries stop and `halt_reason`
  records the reason (monitor-only mode).

### Configuration precedence
- Auto-regime switching now rewrites only `regime_controlled_keys`
  (default `max_open_positions`) instead of the whole preset; the legacy
  behaviour is available via `regime_auto_apply_all: true`. Previously a 20 s
  scan silently replaced the user's risk-per-trade, exposure cap, stop and
  trailing settings with the preset's.
- Seven guard fields added to `BotConfig`, synced in `apply_to_tracker()`, with
  a v11 migration block (`PROFITABILITY_GUARD_DEFAULTS`) so existing configs
  adopt the guards while explicit user values win.

### Accounting
- Partial fills of a cumulative-quantity order are priced by difference, since
  Nobitex reports `executed_price` as the running average price of the order;
  callers reporting per-fill prices set `price_is_incremental`. This fixes the
  failing `test_cumulative_partial_sell_uses_delta_quantity`.

### Test tooling
- `tools/nobitex_preflight.py`: offline cost geometry check + live Nobitex
  spread/cost-guard report per configured pair.
- `NOBITEX_TEST_READINESS.md`: Persian run book for the paper test.
- Research pipeline: s/ms/µs epoch normalisation, bar-step sanity warning,
  live-faithful exit model (previous-scan stop level, gap-aware fills,
  profit-gated ratchet), trailing (activation, distance) pairs in the sweep,
  no-trailing arms, and an out-of-sample half-split check for the best cells.
- Tests: 273 passed.

## v6.9.0 — Profitability-aware adaptive strategy switching

- Added realized strategy performance statistics from closed trades.
- Added minimum-sample and hysteresis controls to strategy selection.
- Added profitability veto for sufficiently sampled negative-expectancy strategies.
- Wired strategy attribution into live execution rows.
- Added GUI auto-regime profitability veto and adaptive-pipeline performance input.
- No profitability guarantee; paper validation remains required before live capital.

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
