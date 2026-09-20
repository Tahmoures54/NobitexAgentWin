## v7.0.0 — L2 order-flow microstructure gate

Entry-side confirmation only. The v6.9.1 profitability guards (cost guard, honest
paper fills, 360-minute time stop, expectancy guard) are unchanged and still bind.

### Order-flow gate
- `trading/order_flow.py`: deterministic, exchange-agnostic L2 feature extraction —
  visible bid/ask depth imbalance, spread and microprice bias, collapsed into an
  interpretable 0–100 score.
- Configurable BUY-side gate (`order_flow_enabled`, `order_flow_levels=10`,
  `order_flow_min_score=58`, `order_flow_max_spread_pct=1.2`,
  `order_flow_min_bid_depth_quote`) wired into the existing candidate execution
  path, and still evaluated when the ask-depth filter is disabled.

### Multi-scan confirmation
- A mover must persist before it is bought: `min_confirm_scans=3`,
  `confirmation_enabled=true`, `confirmation_max_minutes=10`, with the shipped
  entry thresholds raised to a 3% observed move (`pump_threshold_pct=3`,
  `min_observed_move_pct=3`).

### Online trade-outcome learner
- `trading/online_trade_learner.py`: online logistic regression with bounded
  weights and JSON persistence, estimating the probability a candidate reaches a
  positive net outcome. It abstains below `ml_min_samples=30` and only gates
  entries above `ml_min_probability=0.58`; it never touches sizing, stops or live
  execution.

### Tooling, tests, docs
- `tools/nobitex_paper_capture.py`: read-only Nobitex paper-data recorder (public
  market stats + L2 order book, no order placement/cancellation) for measuring the
  paper strategy against real IRT conditions.
- Tests for order-flow features and gate behaviour, the online learner, the
  three-scan confirmation path, and pump-threshold isolation in the prioritisation
  fixtures.
- `README.md` / `PRODUCTION.md` capture instructions and default controls.
- No profitability guarantee: the 100-day x 18-market study behind v6.9.1 found
  every realistic arm net negative, and these layers are filters, not an edge.
  Paper and walk-forward validation on captured Nobitex data remain required.

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
  setting). The shipped profile uses **360 minutes**, chosen by a three-level
  sensitivity run (120 / 360 / uncapped, 100 days x 18 markets) rather than by
  taste: uncapped, 62% of all swept trades are held longer than 2 hours (77% of
  the balanced geometry), so a 120-minute cap pre-empts the trailing stop and
  becomes the dominant exit - 46 of 61 exits on 1m data, 6 of 8 at the live 10s
  cadence. At 360 minutes the cap binds on ~10-38% of trades instead and still
  closes the multi-day positions the time stop exists for. No level is
  profitable; `PROFITABILITY_ANALYSIS.md` §11-6 has the table.
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
  spread/cost-guard report per configured pair. It now prints the configured
  time stop and fails (🔴) when `max_hold_minutes` is 0, because uncapped holds
  averaged 1.2-2.1 days in the study.
- `NOBITEX_TEST_READINESS.md`: Persian run book for the paper test.
- Research pipeline: s/ms/µs epoch normalisation, bar-step sanity warning,
  live-faithful exit model (previous-scan stop level, gap-aware fills,
  profit-gated ratchet), trailing (activation, distance) pairs in the sweep,
  no-trailing arms, and an out-of-sample half-split check for the best cells.
- Research pipeline, correctness pass — two defects that had been flattering the
  results: positions still open at a drawdown halt were marked at the last bar of
  the *whole window* instead of the halt bar (48 of 96 sweep cells halted, so a
  position opened on day 20 and halted on day 40 was booked at the day-100
  price), and the `max_hold_minutes` time stop this release adds to the bot was
  not modelled at all. Leftovers are now marked at the halt bar and reported as
  `Halted open`, runs report `traded_days` next to `window_days`, the engine
  market-sells once a position exceeds `max_hold_minutes` (an intrabar stop still
  wins the scan), and `--time-stop` applies it to every arm and the whole grid.
- Research pipeline, reporting: out-of-sample candidates are ranked among cells
  with >=20 trades that never tripped their halt (raw-expectancy ranking used to
  surface n=6 cells), each cell reports `robust` = positive in both halves with
  >=10 trades each, and every arm/cell carries a `hold_hist_min` histogram so
  "how often would a cap bind?" is measured instead of argued.
- Net effect on the published evidence: positive sweep cells with >=20 trades
  8 -> 4, and the balanced preset's zero-cost gross edge +0.27% -> +0.04% per
  trade (~1/20 of the 0.80% round trip). Numbers quoted before this pass should
  not be reused.
- Migration precedence is now tested as well as documented: a pre-v11 config
  adopts `PROFITABILITY_GUARD_DEFAULTS` only for keys it does not already set,
  so an operator's explicit `max_hold_minutes` (including `0` = no time stop)
  survives the v11 migration.
- Tests: 289 passed, including 5 engine regressions
  (`tests/test_research_backtest_engine.py`), 8 invariants on the shipped
  profile's economics - among them that `apply_to_tracker()` really carries all
  seven guards into the trading loop, the "dead config key" bug class this
  release fixes - and 3 on migration precedence
  (`tests/test_bot_config_regime.py`).

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
