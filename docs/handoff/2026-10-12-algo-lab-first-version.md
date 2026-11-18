# Hand-off: Algo Lab first version (written 15 Oct, describes 12 Oct state) — mostly superseded

## What it was

Backtesting and paper trading for NIFTY options; first strategy the positional short ATM straddle. The Mac
runs backtests and never logs in to Zerodha. The server (`/opt/algolab`, service `algolab-paper`) stores
prices daily at 15:45 IST (`algolab-collect.timer`). Page `/paper` inside the portfolio app. The engine reuses
the portfolio app's morning Zerodha login (`/opt/trader/data/access_token.json`); Zerodha allows one login per key.

## Key files

`algolab/` (engine, broker/charges, pricing, `kite.py`, `paper.py`); `deploy/install_server.sh`,
`uninstall_server.sh`; `deploy/update_portfolio_dashboard.sh`; `deploy/portfolio_page/`;
`deploy/install.sh` (older standalone hosting, not used).

## Superseded since

- Tests: now 101 (99 pass, 2 known failures), not 19.
- Backtests no longer use Black-Scholes by default: real prices only (Zerodha + TrueData).
- Margins are now fetched from Zerodha (`algolab/margins.py`).
- The "not confirmed as applied" zip was superseded by later engine updates (last 14 Oct 23:36 IST).

## Still open

- **Change the portfolio dashboard password**: it appeared in a chat. Change it on the dashboard (which
  rewrites `/opt/trader/config.json`), then update the Mac's `secrets.yaml` to match.
- The engine ignores the dashboard's "Pause sync".
- Recheck charges and lot size (65) in `config.yaml` against Zerodha.
- Run dashboard-restarting scripts outside market hours. Backups: `*.before-algolab`, `dashboard.html.bak-*`
  in `/opt/trader`.
