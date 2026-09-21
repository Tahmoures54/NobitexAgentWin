# CryptoScanner 6.1.0 — Nobitex IRT raw-scan strategy

CryptoScanner is a Windows-friendly spot scanner and trading assistant for **Nobitex IRT**. The 6.1 strategy is deliberately based on real movement already observed by the scanner. It is not a prediction system and it is not a profit guarantee.

> Cryptocurrency trading can lose money. Profitability must be measured from paper results after fees and slippage; it cannot be guaranteed by this project.

## What 6.1 does

The strict Nobitex trend path uses only:

- the price observed on each completed scanner cycle;
- the scanner-owned per-symbol scan history; and
- a **simple average of previous scans** as a soft quality baseline.

A long entry is allowed when both of the following are true:

1. enough valid scans exist for the configured lookback; and
2. the current observed move is **strictly above** the user-configured `threshold_percent`.

Any symbol that grew more than that threshold is eligible. Consecutive positive scans, higher highs / higher lows, price-above-mean and a rising previous mean still participate, but only as a **small ranking bonus** (they never veto a threshold-qualified long). Negative, flat, below-threshold, or incomplete-history symbols remain non-tradable. A move exactly equal to the threshold is rejected.

Every fill is opened with a protective **stop loss**. When trailing is enabled the stop ratchets with a new observed high and never moves below the entry price.

The strategy does **not** add RSI, MACD, EMA, Bollinger Bands, Fibonacci, another technical indicator, price prediction, AI, or machine learning. The only trend baseline is the simple mean of prior scan prices. Existing spread, quote-volume, order-book-depth, and account-risk checks may still reject an otherwise confirmed candidate as an execution-safety measure; they never create a trend signal.

## Default 6.1 profile

The shipped `data/bot_config.json` starts in paper mode with these raw-trend defaults:

| Setting | Default |
|---|---:|
| Execution mode | **paper** |
| Quote / venue | Nobitex **IRT / Rial** spot |
| Raw trend enabled | yes |
| Movement threshold | 3% |
| Consecutive positive scans | 3 |
| Lookback | 6 scans |
| Stop loss | 3% |
| Trailing stop | enabled, 1% distance (can be disabled) |
| Risk per trade | 0.5% |
| Maximum open positions | 2 |
| Cooldown | 30 minutes |
| Scan interval | 10 seconds |
| Whitelist / blacklist | empty / empty |
| Max total exposure | 30% in the shipped profile |

The persistent scanner history is written to `data/scan_history.json` (ignored by Git). It is bounded per symbol and written atomically. A restart therefore does not turn an already-observed trend into a new single-tick signal.

## Exits and risk controls

- **Stop loss:** a long position is closed when the observed price reaches the configured stop.
- **Trend break:** an open position is closed when the shared raw assessment detects a broken lower structure or price below the previous-scan mean.
- **Optional trailing stop:** when enabled and activated, the stop follows a new observed high and never moves below the entry price.
- **Position limits:** maximum open positions, entries per cycle, per-position notional, total exposure, minimum order value, cooldowns, and drawdown halt are applied before an order.
- **Paper accounting:** paper fills use the configured fee and optional half-spread model. Review realized net P&L, win rate, drawdown, fees, and exit-reason breakdown rather than a theoretical signal count.

The strategy does not promise that any stop or filter will make the system profitable.

## Paper first, live only by explicit activation

1. Keep **Execution mode = Paper** and run enough scans to accumulate the configured lookback.
2. Measure closed-trade results after fees and realistic spread assumptions. Save the paper database/log for review.
3. Check that the confirmed-entry, rejected-entry, stop-loss, and trend-break behavior is understood.
4. Only after paper review, switch to **Live Nobitex** in Bot Settings.
5. Press **Start** on the Real tab. This is the explicit live activation step; connecting an API account alone does not arm orders.
6. Start with a small allocation, verify the first fill and protective stop, and keep withdrawals disabled on the API key.

Paper and live entries are mutually exclusive. Pausing new live entries does not discard existing positions: their stop, trailing, and trend-break exits must remain monitorable. The application does not bypass `emergency_halt.py` or any existing halt/safety gate.

### IRT / Rial accounting

Nobitex IRT order and risk amounts stay in Rial-compatible IRT units. Toman is display-only. **Do not divide an order amount or price by ten before sending it to Nobitex.** The UI labels quote amounts as IRT (Rial).

## Configuration names

Canonical raw-trend settings are:

```text
threshold_percent
min_consecutive_positive_scans
trend_lookback_scans
stop_loss_percent
trailing_stop_percent
risk_per_trade_percent
max_open_positions
cooldown_minutes
scan_interval_seconds
symbol_whitelist
symbol_blacklist
raw_scan_trend_enabled
scan_history_file
```

The GUI also keeps older profile aliases (`pump_threshold_pct`, `movement_lookback_scans`, `stop_loss_pct`, and similar) synchronized for compatibility. Lists accept comma-separated symbols such as `BTC, ETH, SOL`; an empty whitelist means all symbols except the blacklist.

Never put API keys in source control. Use the encrypted local credential store or the environment/configuration flow, with Nobitex permissions **READ + TRADE only** and no withdrawal permission.

## Installation and execution

Python 3.11+ is recommended (3.13 is supported).

```bat
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
py main.py
```

Before any live use, run the read-only preflight:

```bat
python tools\nobitex_preflight.py
```

The repository also includes a read-only paper-data recorder. It reads public Nobitex market data and writes a CSV; it never places or cancels orders:

```bat
py tools\nobitex_paper_capture.py --hours 8 --interval 15 --top 40
```

## Validation and tests

Run the full suite from the repository root:

```bat
python -m pytest -q
```

The raw-trend tests cover a threshold-qualified entry (including incomplete structure), ranking tilt between equal-move symbols, invalid/negative/below-threshold/insufficient-history rejection, stop-loss exit, trailing-stop ratchet, and trend-break exit. Static checks should also include Python compilation/import checks before a release.

## Measuring results

A result report should record the paper period, number of completed scans, confirmed and rejected candidates by reason, open/closed trades, net P&L after fees/spread, win rate, average win/loss, expectancy, maximum drawdown, exposure, and exits by stop/trend-break/trailing reason. Do not call a configuration profitable from a handful of signals, and do not promote it to live without measured paper evidence.

## Project layout

```text
analysis/raw_trend.py                 # shared raw-price assessment
analysis/signals.py                   # raw strategy routing and aliases
analysis/risk.py                      # stop, trailing, and IRT sizing helpers
core/irt_money.py                     # Decimal-safe IRT/Rial helpers
trading/nobitex_momentum_engine.py    # scanner entry gates and history
trading/regime_detector.py             # raw scan regime context
trading/scan_history.py                # atomic persistent scan history
trading/trader.py                      # guarded Nobitex execution adapter
signal_tracker.py                      # paper/live ledger, entries, exits
trading/bot_config.py                  # settings, migration, validation
trading/emergency_halt.py              # existing emergency halt path

gui/                                   # settings, paper, and live panels
tests/                                 # regression and strategy tests
data/bot_config.json                   # paper-first shipped profile
data/config.ini                        # portable raw-trend defaults
```

**Operational sequence:** **Paper → measure after costs → review → optionally enable Live explicitly → scale only with evidence.**

## Security and production references

See [PRODUCTION.md](PRODUCTION.md), [SECURITY.md](SECURITY.md), [NOBITEX_TEST_READINESS.md](NOBITEX_TEST_READINESS.md), and [CHANGELOG.md](CHANGELOG.md) for exchange setup, security, and operational details.
