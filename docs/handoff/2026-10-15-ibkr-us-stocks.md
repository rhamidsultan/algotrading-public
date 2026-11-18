# Hand-off: IBKR US stocks (15 Oct 2026)

## Built

- IB Gateway in Docker on Lightsail (`~/ibkr`), logged in to the live account. The API is read-only and only
  reachable from the server itself.
- `ibkr-sync` service: every 60 s from 09:00 to 16:30 New York time, writes holdings to
  `/opt/trader/data/ibkr/` (a snapshot file plus `ibkr.db` history).
- Dashboard page `/us` ("US stocks"): holdings, day P&L, charts, USD/INR switch, a warning when data goes stale.

## Files (now `server/portfolio/`)

- New: `ibkr_panel.py`, `templates/us.html`, `server/ibkr/` (installer, page files, `sync/` setup files).
- Changed: `app.py` (registers the page), `templates/dashboard.html` (link), `server/deploy.sh` (copies the page).
- Server-only changes, not in the repo: `~/ibkr/.env` (DATA_DIR, SYNC_INTERVAL_SEC=60), the screen-viewer
  password and port 5900 in the server's `docker-compose.yml`, UMask in `ibkr-sync.service`, and user
  `ubuntu` added to the `trader` group.

## Known broken / unfinished (as reported)

- Automatic gateway login fails: the new login screen is a web page the login tool can't fill in. Weekly
  manual re-login from the Mac: run `ibkr-login`, log in, approve on the phone, then run `ibkr-done`.
  Check with `ibkr-status`.
- Memory was tight on the 1 GB server; plan was to move to the 2 GB plan and keep the static IP.
- The `/us` page was installed but never seen running live. Check it once.
- AWS billing showed only $0.05 this month. Confirm the Stockholm server is in the same AWS account.

## Remember

- The AWS Free plan ends 8 Apr 2027; the server is suspended unless upgraded first. Reminder set for 1 Mar 2027.
- `srv-health` checks memory, CPU, disk and services.
- Next: a monthly-buy planner, then order placement with phone approval.
- Investing plan: 1–2 year horizon, $200/month, about half in a Treasury fund and half in an S&P 500 fund.
  No adding to single stocks.

## Checked (15 Oct 2026)

- The server moved to a 2 GB instance with the same static IP on 13 Oct (see `deploy/update-server.command`).
- IBKR is currently stopped on the server (Docker and `ibkr-sync` disabled), per the straddle-grid hand-off;
  `deploy/start-ibkr.command` starts it again.
