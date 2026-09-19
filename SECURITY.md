# Security policy

## API credentials

- Store Nobitex public/private keys in environment variables or the app encrypted store.
- Recommended env vars: `NOBITEX_API_KEY`, `NOBITEX_PRIVATE_KEY`.
- Create exchange keys with **READ** and **TRADE** only — never **WITHDRAW**.
- Never commit keys, seeds, `.env`, or `data/*.enc` files.

## Operational security

- Keep the system clock accurate (signature timestamps).
- Prefer paper mode until behavior is validated.
- Limit capital at risk via `max_total_exposure_pct` and `risk_per_trade_pct`.
- Review logs for repeated 401/403/429 responses.

## Reporting

If you discover a vulnerability in this repository, do not open a public issue with exploit details. Contact the repository owner privately.

## Scope note

Obfuscation helpers in `core/config.py` are **not** cryptographic protection for secrets. Real secrets must use OS/env/encrypted store practices.
