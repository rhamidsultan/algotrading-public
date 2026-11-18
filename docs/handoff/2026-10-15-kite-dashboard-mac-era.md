# Hand-off: Kite portfolio dashboard, Mac version (15 Oct 2026) — partly superseded

Describes the dashboard as first run on the Mac. It now runs on the server (`/opt/trader`, service
`trader-app`, code in `server/portfolio/`), viewed in the browser and as a phone app.

## What it is

Read-only Zerodha portfolio dashboard (Flask): holdings, total and day P&L, equity/gold split, P&L
high/low ranges, history charts, alerts. No order code.

## Never commit

`config.json`, `token.json`, `.secret_key`, `portfolio.db` (API keys, dashboard password, daily Zerodha token,
P&L history). The repo's `.gitignore` and the test workflow enforce this.

## Running it on the Mac (old routine)

- `caffeinate -i python3 app.py`, sign in, then Log in with Zerodha. Token expires around 6 am IST.
- Port 5010 (macOS AirPlay uses 5000); Kite redirect `http://127.0.0.1:5010/callback`.
- Paid Kite Connect (Rs 500/month) gives live ticks; otherwise a 60-second refresh.
- Mac Python lacks root certificates, so the stream passes certifi's bundle explicitly. Keep that.

## Unfinished / unverified (as reported)

- Second-by-second browser push only tested on demo data.
- Email and Telegram alerts off; alert thresholds still the defaults.
- Stocks bought today only appear once they reach holdings (positions not read).
- Gold detection is "GOLD in the symbol name"; fix misclassified symbols in `gold_symbols` / `non_gold_symbols`.
- Total P&L high/low only covers the period since 8 Oct 2026.

## Next phase (as written then)

API orders need a static IP registered with Zerodha (SEBI), hence the cloud server. Build trading with
confirmations, limits, a dry-run mode and a kill switch. (Done since: shadow/live modes with guards,
limit orders, protective stops, daily loss limit and kill switch.)
