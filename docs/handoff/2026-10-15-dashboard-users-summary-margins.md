# Hand-off: dashboard and Algo Lab work (14–15 Oct 2026)

## Built or changed (deployed; last server update 14 Oct 18:15 IST)

- Multiple logins with access per area (None / View / Use), "hide amounts" for the portfolio, per-login
  profile, Face ID and push, activity log, owner-only Users page. Files: `deploy/portfolio_page/users_panel.py`
  (new), `algolab_panel.py`, `pwa_panel.py`.
- Summary page (`/summary`): net worth, India and US, paper and live, market, system and server health.
- Menu: Stocks and US under Portfolio; Backtests under Paper.
- Blaze Butterfly runs as a normal Algo Lab strategy. The old engine (`trader-paper`) is disabled and `/algo`
  redirects to `/paper`. Its data is kept; re-enable with `sudo systemctl enable --now trader-paper`.
- Zerodha margins per strategy and per session: `algolab/margins.py`, run by `algolab-margins.timer` (every
  30 min in market hours). Shown on strategy cards, the start form, sessions and Summary.
- iPhone icon fix (`pwa_panel.py`).
- `deploy/update_engine.sh` and `make_server_update.sh` install all of the above. `deploy/get-server-data.command`
  copies server data to the Mac.
- Tests: `test_users.py`, `test_margins.py`, `test_defined_risk.py`, `test_app_pwa.py`.

## Unfinished or known broken (as reported)

- `tests/test_summary.py` (from another chat) fails: it expects a different `/api/summary` (owner-only,
  "groups", closed legs, trade activity).
- The other chat's engine changes (`engine.py`, `models.py`, `paper.py`, `run.py`, `truedata.py`) were
  uncommitted but already on the server.
- Margins need that day's Zerodha login.
- Recorded option prices only go back to 6 Oct.

## Remember

- To deploy (today): run `make_server_update.sh`, then double-click `update-server.command`.
- Pages not listed in `users_panel.py` rules are owner-only. Add new pages there.
- Changing the owner password on the dashboard rewrites `config.json`. Update the Mac's `secrets.yaml` to match.
- Logins and activity live in `/opt/trader/data` (`users.json`, `activity.log`).
- Delete the old instance Claude-Zerodha and the 8 Oct snapshot from Lightsail.

## Checked against this repo (15 Oct 2026)

- Confirmed: 2 tests in `tests/test_summary.py` fail (one expects the other Summary API; one predates
  real-prices-only backtests). Marked as known failures so they show in every run; GitHub now runs the
  dashboard tests too (99 others pass).
- The uncommitted engine changes are now committed (pull request #1).
- Option prices before 6 Oct now come from TrueData (2 years).
