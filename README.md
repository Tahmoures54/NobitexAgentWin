# CryptoScanner 6.1.1 — Nobitex IRT Edition

CryptoScanner is a Windows-friendly spot scanner and trading assistant built specifically for **Nobitex and the IRT market**.

## Important

This project is a trading system, not a profit guarantee. Cryptocurrency markets can move rapidly and losses can exceed expectations. The release is designed to improve execution discipline, reduce avoidable risk, and avoid unnecessary dependencies; it does not promise profitability.

## What changed in this release

### Venue isolation
The trading stack now supports **Nobitex only**:
- Nobitex REST market statistics
- Nobitex order book
- Nobitex UDF candle history
- Nobitex balances
- Nobitex spot orders
- Nobitex order status/history/cancellation

All alternative exchange adapters and third-party market-data adapters were removed.

### Strategy
The entry engine is now `NobitexMomentumEngine`:
1. Observe the real Nobitex IRT price path.
2. Require measurable upward movement over multiple scans.
3. Reject excessive spread and excessive chase.
4. Require meaningful local volume.
5. Optionally inspect executable ask depth for shortlisted candidates.
6. Block most new alt entries during a broad BTC sell-off.
7. Allow a tightly controlled Eagle exception only for unusually strong, liquid local movers.
8. Hand the final candidate to `SignalTracker`, which applies account, exposure, cooldown and risk controls.

The engine does not use a foreign price feed to manufacture an entry signal.

## Risk and position sizing

The shipped configuration uses explicit risk/exposure controls:
- Position sizing mode: `fixed` in the supplied configuration snapshot
- Fixed position notional: 2,500,000 IRT
- Stop loss: 3%
- Trailing activation: 3%
- Trailing distance: 2%
- Take profit: 50% (the trailing stop remains the primary exit once active)
- Maximum total exposure: 90%
- Maximum open positions: 10
- One new entry per scan
- 10-second live scan interval, subject to API limits

These values are configuration choices, not recommendations or guarantees. For a different account size, validate sizing and exposure in paper/forward testing before changing live parameters.

**IRT note:** Nobitex exposes the local currency balance/order values in Rial-compatible units. The application keeps calculations in the API unit and converts to Toman only for human-facing display. Never divide an order amount by 10 before sending it to Nobitex.

## Security

Do not distribute live credentials with the project.

Preferred options:
- `NOBITEX_API_KEY`
- `NOBITEX_PRIVATE_KEY`

The application also supports its encrypted local credential store for normal desktop use. Runtime credential files are intentionally excluded from the release archive and Git.

Create API keys with:
- READ
- TRADE
- no WITHDRAW

Keep the private key secret and keep the PC clock synchronized.

## Installation

Python 3.11+ is recommended; Python 3.13 is supported.

```bat
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Configure credentials

### Environment variables

PowerShell:

```powershell
$env:NOBITEX_API_KEY="YOUR_PUBLIC_KEY"
$env:NOBITEX_PRIVATE_KEY="YOUR_PRIVATE_KEY"
```

Command Prompt:

```bat
set NOBITEX_API_KEY=YOUR_PUBLIC_KEY
set NOBITEX_PRIVATE_KEY=YOUR_PRIVATE_KEY
```

Alternatively, open the application Settings dialog and enter the Nobitex public/private key pair. The private key is never written into normal logs.

## Run

```bat
py main.py
```

Paper mode is the default. Use paper mode to validate the strategy before enabling live execution.

## Live trading safety

Before enabling live trading:
1. Verify the Nobitex key has READ and TRADE only.
2. Verify the displayed IRT balance.
3. Confirm position sizing is `risk_percent`.
4. Confirm the maximum position and total exposure limits.
5. Start with a small account allocation.
6. Watch the order/fill logs.
7. Verify protective stop placement after every live entry.

The live execution layer refuses to treat an unfilled order as a completed position and performs wallet reconciliation after fills.

## Project structure

```text
CryptoScanner-6.1.1-Nobitex-IRT/
│
├── main.py
├── README.md
├── UserGuide.html
├── requirements.txt
├── pytest.ini
│
├── analysis/
│   ├── __init__.py
│   ├── indicators.py
│   ├── indicators_integration.py
│   ├── risk.py
│   └── signals.py
│
├── api/
│   ├── __init__.py
│   ├── api_base.py
│   └── api_tronscan.py
│
├── core/
│   ├── config.py
│   ├── encryption.py
│   ├── irt_money.py
│   ├── user_manager.py
│   ├── user_status.py
│   └── utils.py
│
├── trading/
│   ├── __init__.py
│   ├── bot_config.py
│   ├── exceptions.py
│   ├── exchange_base.py
│   ├── execution_mode.py
│   ├── nobitex_client.py
│   ├── nobitex_momentum_engine.py
│   ├── regime_detector.py
│   ├── trader.py
│   └── utils.py
│
├── gui/
│   ├── gui_main.py
│   ├── unified_trading_window.py
│   ├── trading_ui_helpers.py
│   ├── ui_factory.py
│   ├── ui_theme.py
│   ├── dialogs/
│   └── panels/
│
├── tests/
│   ├── test_nobitex_irt.py
│   ├── test_nobitex_momentum.py
│   ├── test_live_fill_safety.py
│   ├── test_signal_tracker.py
│   └── ...
│
└── data/
    ├── bot_config.json
    ├── config.ini
    └── .gitkeep
```

## Main components

### `trading/nobitex_client.py`
Nobitex REST adapter with:
- Ed25519 API-key signing
- IRT/RLS normalization
- market statistics
- order book
- candles
- balances
- order placement
- order status
- cancellation
- bounded retry/backoff
- rate-limit handling
- authentication/authorization error separation

### `trading/nobitex_momentum_engine.py`
Local momentum detector. It maintains a short in-memory price history and scores candidates using:
- observed movement
- local tick momentum
- spread
- volume
- confirmation count
- chase distance
- BTC regime protection

### `signal_tracker.py`
Execution and risk state:
- paper/live separation
- position sizing
- exposure cap
- stop/trailing logic
- cooldowns
- drawdown halt
- fill verification
- wallet reconciliation
- SQLite trade history

## Testing

Run:

```bat
python -m pytest -q
```

The final source tree contains no alternative exchange adapter or foreign exchange market-data provider.

## Operational recommendation

For a new installation, keep this sequence:

**Paper → observe fills/signals → tune thresholds → small live allocation → gradual increase**

Do not increase size merely because a strategy had a short profitable period. Evaluate it over a meaningful sample of trades, including fees, slippage, rejected orders and missed fills.
