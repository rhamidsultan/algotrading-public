# Hand-off: Blaze Butterfly paper engine (12 Oct 2026) — superseded

Superseded on 14 Oct: the Blaze Butterfly now runs as an Algo Lab strategy, `trader-paper` is disabled and
`/algo` redirects to `/paper` (dashboard hand-off). Kept for its history and the incident lesson.

## Built

- `paper_engine.py` (now `server/portfolio/paper_engine.py`): paper-only Blaze Butterfly (NIFTY put
  broken-wing butterfly, Tradetron spec). Never places orders. Zerodha quotes, the dashboard's daily login;
  buys at ask, sells at bid; state in `data/paper.db`.
- `paper_config.example.json` (copied to `paper_config.json` on the server).
- `templates/algo.html`, routes `/algo`, `/api/algo`, `/api/algo/command` in `app.py`.
- `server/deploy.sh` installs the `trader-paper` service and backs up `app.py` and pages first.
- `server/fix/`: one-off repair scripts (already run; reference only).

## Incident (fixed)

The 12 Oct deploy was built from a stale Mac copy and overwrote the server's `app.py` and pages, removing the
Algo Lab `/paper` page and that morning's dashboard and Stock history changes. Repaired with
`server/fix/fix_dashboard.sh`; pre-fix files in `/opt/trader/backup-fix-*`. This is the overwrite problem
the single repo is meant to end.

## Unfinished (as reported)

- Never confirmed against live prices (`paper_engine.py --check` in market hours).
- `margin_per_unit = 87300` was the spec's placeholder.
- NSE holidays listed for 2026 only; add 2027 when published.
- P&L is gross (no brokerage or taxes).
- `server/paste-into-server.txt` is stale; do not use (not in the repo).

## Remember

- Until the server switch (phase 4), the server is the source of truth for `/opt/trader/app.py` and
  templates; reconcile them with `server/portfolio/` before deploying from the repo.
