# Hand-off: friend replication bundle (chat of 12–15 Oct 2026) — not started

Nothing was built in that chat (the project folder was never connected). Recorded as a plan.

## Goal

A clean copy of the full setup (portfolio dashboard, paper-trading engine with all strategies, Backtests
page, Mac backtest code) for a friend to run on his own Mac and AWS with his own Zerodha account.

## To do

- Strip all secrets and personal data: API key/secret, access tokens, passwords, Zerodha client ID, server
  IP/hostname, holdings, trade logs, P&L history. Provide a `.env.example` template.
- Setup guide: Mac (Python, dependencies, backtests); AWS (Lightsail Ubuntu in Mumbai, static IP, firewall,
  deploy, services, HTTPS and dashboard login); Zerodha (his own Kite Connect app, his static IP whitelisted).

## Remember

- Package from what actually runs on the server; edits made only on the server must come back first.
- Grep the bundle for secrets (configs, logs, SQLite/CSV data, git history). Hand over a fresh export, not a clone.
- Live order placement ships disabled by default, so the friend starts in paper mode.
- With this repo the export is simpler: `git archive` of a tagged version, minus `docs/handoff/` and
  personal files, plus a scan.
