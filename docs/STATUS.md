# Status

Update this in every pull request that changes what is live, data coverage or work in progress.
Newest information first within each section.


## Summary › Prices: no more false "No new price for 277778 h" (27 Nov 2026, pull request "prices-health")

- The check read the engine's `runtime/feed_status.json` (JSON) as a bare timestamp; that always failed and its
  "never" value (1e9 s) was shown as hours, every market day. Now (`algolab_panel._feed_health`): the age from the
  file's own `time`; a warning only when a running session (desired.json + fresh heartbeat) uses Zerodha prices;
  "No session needs Zerodha prices right now" when none does; "No price yet today" when nothing was saved.

## SL and target shown: log, position rows, P&L chart (26 Nov 2026, pull request "sl-target-show")

- Each open leg's SL / target: the strategy's own (`Strategy.leg_levels`, intraday_strangle: its leg stop, moved
  by trail-to-cost) and the session's risk-rule stops (the nearer SL), plus, live, the protective stop at Zerodha.
- Activity log: "<leg>: SL 128.25 · Zerodha stop 281.55" when a leg opens, "<leg>: SL moved 128.25 -> 93.85" at
  each move (trailing). Position rows: SL (red), target (green), Zerodha stop, how far away, and a bar from target
  (or entry) to SL with the price on it.
- P&L chart (Overall and the cycle view): red "At SL" and green "At target" step lines, the P&L if the open legs
  left at those levels at that moment, so a trail shows as a step. Kept per minute in the session's `levels.csv`.

## Positions page: trailing SL and % entry (23 Nov 2026, pull request "positions-trail-pct")

- SL / target can be entered as a price or as % of the average price ("₹ price / % of avg"; a short's SL is above
  the average, its target below; signed, so an SL already past the average keeps its side). Save never removes a
  GTT: only Remove GTT does (`remove: true` on /api/positions/protect).
- **Trail SL by x % or pts** (needs a saved SL): the GTT's SL follows the best bid/ask midpoint since it was set
  (lowest for a short, highest for a long), only in the position's favour, every 15 s from 09:15 to 15:40, from a
  fresh two-sided quote no older than 2 minutes; the target stays. One dashboard process moves them (file lock);
  never while a close runs or on a position a LIVE session holds. The trail ends (alert) when the position closes,
  changes size or average price, or its GTT changes. Kept in `server/portfolio/data/positions_trails.json`.

## Resume with nothing open starts a fresh cycle (23 Nov 2026, pull request "fresh-cycle-on-resume")

- Why (23 Nov, intraday_strangle-4): straddle sold 09:20, PE leg stop 09:24, Stop at 09:35 (CE bought back),
  Resume 09:36. The strategy still thought the CE was open (Stop squares off without telling it) and waited all day.
- Now Resume on the card marks desired.json `resumed`; the engine, finding nothing open, asks the strategy for a
  fresh cycle (`Strategy.fresh_cycle`, once per press: a restart after an update is not a resume).
  intraday_strangle forgets today's cycle and starts a new one at the next bar, before `last_entry_time`, with the
  entry checks; a day it closed itself (day loss / target / 15:15) stays closed. Other strategies: unchanged.
- Stop (and any halt or its close retries) now also tells the strategy which legs it squared off
  (`Strategy.on_squared_off`, by default as a leg closed in Kite): intraday_strangle marks them "(closed by Stop)",
  no re-entry, so its record never shows a closed leg as open.

## Re-check today and the session archive (20 Nov 2026, pull request "recheck-and-archive")

- **Re-check today** (a running session's card, when its strategy said "No trade today" / "No entry today" and
  nothing is open): the strategy forgets today's decision (`Strategy.recheck_today`, RECHECK file) and its entry
  filters run again at the next bar, within its entry window (intraday_strangle until `last_entry_time`).
- **Archive** (Live › Overview and Paper › Overview): Select, tick sessions, Move to archive. Running sessions and a
  LIVE one still holding legs are refused. Archived ones leave the lists, the chips and the totals, and are listed
  under "Archived (n)" with Restore; starting or resuming one brings it back. Marker file `archived` in its folder.

## Gap and VIX-spike filters use the exchange's previous close (20 Nov 2026, pull request "gap-prev-close")

- Why: 20 Nov, "No trade today: NIFTY +1.34% from yesterday's close (trend/gap day)" was wrong. The filter took the
  last NIFTY the session saw as yesterday's close; the session had stopped at 09:30 on 19 Nov, so it compared with
  09:30, not the close.
- Now live and paper on Zerodha prices read the previous close of NIFTY and VIX from Zerodha's quote (`ohlc.close`,
  one request a day, `KiteFeed._load_previous_close`) and the gap and VIX-spike filters use it; the strategy's daily
  record is corrected too. Backtests are unchanged (full days in the data).
- 20 Nov afternoon the filter said "+11.94%": a close that puts NIFTY more than 8% away (it halts at 10%) is now
  treated as a bad reading and not used (the session's own record is tried next), and the "No trade today" line
  shows the numbers and their source ("26,030.00 now, close 25,300.00 (Zerodha)") (pull request "gap-sanity").

## Protective stops below the circuit limit (19 Nov 2026, pull request "stop-circuit-cap")

- Why (19 Nov, intraday_strangle-10): the PE's stop (3 x 157.10 = 471.30) was above its upper circuit limit
  (442.15); Zerodha refused it, so the leg had no stop from 09:20. Each retry was taken for a lost answer, so at the
  PE's leg stop (09:29) the session could not rule out a live stop, halted and squared off the CE (in profit) too.
- The option's upper circuit limit is read from a quote before every protective stop (pull request
  "stop-band-first"): a 3x trigger (or its limit price) at or above it goes one tick below it, so Zerodha does not
  refuse the first order.
- Now a stop refused for the circuit limit is placed again at once one tick below it (limit price capped at the
  circuit): nearer, so it protects more. A refusal from Zerodha (InputException / OrderException, or a circuit
  message) is never taken for a lost answer: no "may still be at Zerodha" wait, so an exit closes only its own leg.
  A real lost answer (timeout, dropped connection) still waits, so a stop is never placed twice.

## Sign-in opens an open LIVE trade first (18 Nov 2026, pull request "login-to-live")

- When a LIVE session holds positions, opening the dashboard (after signing in, and the first time each day) goes
  to that session on the Live page (`/live#<id>`); with several, to the Live page. Otherwise the profile's start
  page, as before. Only for those who may see Live (`algolab_panel._live_holding_page`).

## Live page review (18 Nov 2026, pull request "live-page-review", needs "remove-shadow")

- Reviewed at phone width, light and dark (ui-review checklist). Changed on LIVE cards: Close all & stop, Resume and
  Sync carry words, not only an icon; a stopped LIVE session that still holds legs shows a banner (nothing watches
  them; Resume or square off in Kite and Sync) and STOPPED on the overview, and has no Delete / Reset there.
- Session chips show the session's name and fit two to a row on a phone; Stop all LIVE is a full-size button; the
  P&L Overall / Current cycle toggle is a real control (34 px, focus ring).
- Start a live session lists the strategies that can trade live first, marks the others "Paper only" and opens on a
  live-ready one.

## Shadow mode removed (18 Nov 2026, pull request "remove-shadow", needs "edit-exit-price")

- The Live pages offer LIVE only: the start form has no Shadow choice, Deploy (from a Paper card) goes to LIVE, the
  Live › Overview LIVE/SHADOW filter is gone, and `/api/paper/start` refuses mode "shadow". Paper stays as it is.
- Existing SHADOW sessions (they never sent an order) are stopped and their folders deleted by the dashboard
  (`algolab_panel._drop_shadow`). The engine still has its no-order path inside (`LiveSettings.mode == "shadow"`,
  used by tests); nothing can start one.

## Exit prices of closed LIVE legs can be edited (18 Nov 2026, pull request "edit-exit-price", needs "live-resume")

- Live › a session › Closed cycles › a cycle: each leg's exit price has an edit button (owner). The new price replaces
  the old one in the trade, its exit fill and the realised P&L (charges stay as booked); the leg shows "edited · was
  <first price>". Nothing is sent to Zerodha. For e.g. a square-off in Kite booked at an estimate.
- Running session: the dashboard writes `EDITS.json`, the engine applies it within seconds (`LiveBroker.edit_exit`);
  stopped: the dashboard edits its saved account the same way (`algolab_panel._edit_exit_saved`, tested to match).

## A stopped LIVE session resumes with its own account (18 Nov 2026, pull request "live-resume", needs "kite-sync")

- Why: Resume on a LIVE card only said "start it again from the form", which made a new, empty session: a strategy
  stopped with legs held (e.g. Blocked) could not continue the next day.
- Now Resume asks you to type LIVE and restarts the same session: positions, P&L, cycle and stops carry on; the
  first check reads Zerodha (and syncs legs closed in Kite) before anything is sent.
- Reset account is hidden (and refused) on a LIVE session that still holds legs: it would forget them.

## Legs squared off in Kite are synced (18 Nov 2026, pull request "kite-sync")

- Why: a live leg squared off by hand in Kite (or the Positions page) stopped the session ("Positions differ from
  Zerodha") and it could only be reset; after a restart the strategy also exited its other leg ("A leg is missing").
- Now (`algolab/live.py` `sync_outside`): at the minute check (and the morning check) a leg Zerodha holds less of
  (all or part) is booked at the price of Hamid's own Kite order: NRML, no Algo Lab tag, placed after the leg was
  opened, not booked before by any live session. Reason "Closed in Kite"; its stop is cancelled, the rest of a
  part-closed leg gets a new stop. Positions are read twice and must agree; an empty answer never counts as closed.
- No such order (closed on an earlier day, or Zerodha's order book is behind): nothing is booked by itself; the
  session stops as before ("press Sync"). **Sync** on the card then books it at the price now, "Closed in Kite (price
  estimated)", after reading positions twice. If Zerodha shows no positions at all (every leg closed on an earlier
  day, or a bad answer), the first press books nothing: press Sync again within 10 minutes.
- A strategy exit right after a square-off in Kite (before the next check) sends nothing: booked from the Kite order,
  or Blocked if none is found (a buy-back would open a long).
- Not synced (the position check stops the session): Zerodha holds more than Algo Lab, or the other way round; a
  contract another live session also holds; any order still working on it (also one of yours in Kite). A stop that
  filled stays a stop fill (the session halts and squares off, also if the stop was changed to MARKET in Kite).
- The strategy keeps its other legs: premium_strangle keeps the cycle, intraday_strangle does not re-enter that leg
  (`Strategy.on_outside_close`). Alert "Algo Lab: leg squared off in Kite".
- Live card: **Sync** button (owner only). Running: syncs at once (refused while it is still closing its legs).
  Stopped: started only to sync; it sends no trades and places no new stops (only a stop resized for the rest of a
  part-closed leg), never squares off, and stops again.

## Updates come from the public repo (18 Nov 2026, pull request "updates-from-public")

- GitHub Actions stopped running on the private repo (billing), so work moved to this public repo (main = snapshot
  of private main 77d1fae, no history). The server's Update now is switched to it once, by a one-line
  `deploy/UPDATE_FROM` in the private repo; from then on it pulls `main` from here over https, no key.
- `deploy/update_source.sh` (run by every update) records `update_repo` (public, https) and `repo` (deploy key: the
  IBKR snapshot, still from the private repo's `ibkr-snapshot` branch, and updates from a private repo). The server's
  copy follows a new update address (`git remote set-url`).

## Legs held overnight close the next day (17 Nov 2026, pull request "live-next-day-stops", needs 134)

- Why: protective stops are DAY orders. The morning after a short leg was held overnight (premium_strangle,
  put_credit_spread, iron_fly, double_calendar...), Zerodha no longer knows the old stop's order id ("Couldn't find
  that order_id"). Every buy-back of that leg first read the stop, failed on that error and gave up: every second, no
  square-off, no alert. The stop check every 30 s failed the same way. The session also lived across days without
  restarting (the feed adopts the morning login), so the start-of-session check never ran again.
- Now: each new day the start-of-day check runs again before any order (positions against Zerodha; stops not in
  today's order book are placed again, at the old trigger or 1.2x the price). An order Zerodha cannot find and that is
  not in today's order book counts as gone. Before a leg whose stop is gone is bought back, Zerodha must still hold
  exactly that leg (the stop may have filled): otherwise nothing is sent and the session stops for a check in Kite.
- A stop or position check that cannot read Zerodha at that moment is logged (once a minute) and no longer stops the
  strategy, and so its exits, from running.
- Orders in the restart journal from an earlier day no longer block the morning check (alert when one is dropped).
- A stop counts as gone only if Kite says it cannot find it, it was placed on an earlier day, and today's order book
  does not list it: a timeout or a lagging order book never leads to a second stop. A gone stop stays tracked until a
  new one is placed (tried at every 30 s check, one alert if it cannot be placed). A filled stop still stops the session
  first. A two-leg exit checks every gone stop before taking any stop off. Zerodha's positions are read twice before
  a difference blocks. A mismatch in the morning still renews the stops of the legs that match and names the legs
  left without one. Legs past their expiry are left to the settlement (no false mismatch the morning after expiry).
- Zerodha that cannot be read for 2 minutes in a row sends one alert ("Algo Lab cannot check Zerodha").
- Also fixed (found by the review): selling more of a short leg used to leave its first stop at Zerodha untracked next
  to a new one (two could fill); now the stop is replaced by one for the whole leg. A stop that filled in part and was
  cancelled at the close is booked.
- Each protective stop now has its own tag: if Zerodha's answer to a placement is lost, the stop is found and adopted,
  never placed a second time; while that cannot be told, no other stop is placed for the leg. A stop is placed again
  only on a leg Zerodha still holds. A "cannot check Zerodha" alert counts each kind of check on its own.
- Order-safety review (independent agent, 17 Nov, six rounds): PASS WITH FIXES (4 High, 4 Medium) → fixes → PASS WITH
  FIXES (1 Critical, 2 High, 1 Medium, in the new stop retry) → fixes → PASS WITH FIXES (1 High: a stop whose answer was
  lost) → fixes → PASS WITH FIXES (1 High) → fixes → PASS WITH FIXES (1 Medium) → fixes → PASS (3 Low, also fixed).

## Live closes never give up (10 Nov 2026, pull request "close-never-gives-up")

- Why: 9 Nov (expiry day) intraday_strangle-7's exit BUY 65 NIFTY 24900 CE did not fill in the 30 s chase (capped at
  5% from the first price), the square-off repeated the same chase, and the session stopped with "close these in Kite
  now", the leg's protective stop already taken off.
- Closing orders (`algolab/live.py`): after `chase_seconds`, what is left is re-sent further through the best price
  (2, 5, 10, 20, 30% in 10 s rounds; `close_max_through_pct` default 30, never past the circuit limit) for
  `close_retry_seconds` (default 60). A step Zerodha rejects is not tried again.
- Never two orders at once for a leg: each order has its own tag and is written to `runtime/sessions/<id>/working.json`
  before it is sent; it is confirmed finished before the next one. One that may still fill (cancel not confirmed, answer
  lost) is read and its fill booked before anything else is sent for that leg, also after a restart.
- Every order may follow the market at least 3 ticks (5% of a cheap option is less than a tick: it was never re-priced).
- A leg that still cannot be closed gets its protective stop back (old trigger, or 1.2x the price if higher), except
  while a buy may still fill. At start the engine also puts back any missing stop on a short leg.
- The session then goes "closing only" (`algolab/paper.py`): a short try every 30 s, also after a restart (same day),
  sending only what Zerodha still holds for it. Nothing is sent if another order works on the symbol (e.g. from Kite);
  a leg closed in Kite is booked as closed (alert); a position that does not fit (twice in a row) stops it untouched.
  Alert when closed and every 10 tries; at 15:25 IST, or on Stop, it ends and asks for Kite, the stop staying.
- Known limit: the first square-off still closes legs one after another (up to ~90 s each); other sessions wait.
- Not changed: the Positions page's manual close (30 s, 5%).
- Order-safety review (independent agent, 10 Nov): FAIL (2 Critical, 5 High) → fixes → PASS WITH FIXES (2 High) → fixes → PASS (2 Low, fixed).

## Positions page (29 Oct 2026, pull request "positions-page")

- Live › Positions (`/positions`, owner only, `server/portfolio/positions_panel.py`): open Zerodha positions with live
  prices, bid/ask and P&L (every second while open), closed-today P&L, and per position:
  - **SL / target** (F&O, NRML only in practice; MIS and equity can only be closed) as one GTT at Zerodha (OCO when
    both). SL limit 10% through the trigger, capped at the circuit; an SL beyond the circuit is refused.
  - **Close**: stops (GTTs, pending SL orders) taken off and saved, re-checked, then LIMIT at bid/ask re-priced every 5 s
    for 30 s (max 5% chase). What is open is taken from the order's own fill and must match Kite's positions before
    any stop goes back; anything unclear → nothing put back + "check Kite now" alert. Fully closed → leftover exit
    GTTs/orders on it cancelled. Restart during a close is recovered from `data/positions_jobs.json`.
  - Legs held by a LIVE Algo Lab session are read-only; positions above ~20 lots go to Kite. Log: `/opt/trader/data/positions_actions.log`.
  - Order-safety review (independent agent, 29 Oct): FAIL → fixes → PASS WITH FIXES → fixes → PASS.
- Deploy: the server update now installs `positions_panel.py` even though the server does not have it yet (`NEW_OK`
  in `deploy/update_engine.sh`); the page check includes `/positions`.
- **Before relying on it:** one 1-lot test on a cheap far-OTM option: set SL/target, check the GTT in Kite, close.

## One price set per index (26 Oct 2026, pull request "index-data-sets")

- `data/spot/NIFTY_minute.csv`, `BANKNIFTY_minute.csv`, `SENSEX_minute.csv`: TrueData 1-minute history (from 15 Oct 2024)
  then Zerodha's 1-minute bars, topped up for all three by the 15:45 collect. The Data and Backtests pages (and the Mac
  dashboard) offer only these, plus the 1-second days.
- `run.py data-organize [--dry-run]` (also run by collect and after sync-data): seeds each set from TrueData, moves
  `TD_*_1minute.csv` (once fully inside the set) and `*.before-fill-*.csv` to `data/archive/spot/`. Mac on 26 Oct:
  TD_NIFTY (in NIFTY_minute) and the 15 Oct backup will move; Bank Nifty and Sensex sets are created from TrueData.

## Paper vs backtest at 1-second steps (started 26 Oct 2026)

| Step | State |
|---|---|
| 1. Record 1-second bars from Zerodha's stream (`algolab/sec1.py`, `data/sec1/<day>/`) | merged (#108) |
| 2. Keep N trading days (`sec1.keep_trading_days`, default 5; pruned by the 15:45 collect, `run.py sec1 prune`) | merged (#108) |
| 3. Backtest engine stepping every second on these bars (`algolab/sec1_feed.py`, data set `NIFTY_1second`) | this pull request ("sec1-backtest") |
| 4. Daily compare job: paper / live vs 1-min vs 1-sec backtest, per session per day (`algolab/daily_compare.py`, `run.py compare`) | this pull request ("daily-compare") |
| 5. Algo Lab › Compare with paper page (`/backtests/compare`: dates, sessions, 1m/1s, report, CSV; owner setting for days kept) | this pull request |
| 6. Nightly run: the 15:45 collect queues the day's comparison (#113); Saturday report carries the week day by day in `paper_report.json` → `daily`; Get data from server brings `data/sec1` (mirrored) | this pull request ("weekly-daily-compare") |

Audit of the 1-second engine (26 Oct, independent reviewer): FAIL on 1 Critical + 3 High findings (expiry legs not settled that day, earlier-day prices used for fills, partial days replayed on frozen prices, day open/high from recorded seconds); all fixed with M1/M3/M4 in pull request "sec1-audit-fixes". Re-check on real recorded days once 2–3 exist.

Recording starts with the first market day after the update, and only while the paper engine runs on Zerodha prices
with the day's login. Check: `run.py sec1 status` (days, MB, free disk) and `runtime/sec1_status.json`.

## Moving to one repo (plan reviewed 15 Oct 2026)

| Phase | State |
|---|---|
| 1. Freeze; other chats write hand-off notes into `docs/handoff/` | done: 7 notes filed |
| 2. GitHub repo, history imported, secrets check, CLAUDE.md, tests on every pull request | in progress (this pull request) |
| 3. Mac folders: `~/Trading`, data and secrets moved out of the code folder, `data_dir` setting | not started |
| 4. Server switch to `/opt/algo-trading` from a tagged version (a Saturday, with fallback) | not started |
| 5. Routine: branch → pull request → tests → Hamid merges; Mac `git pull`; server deploy off-hours | not started |

## Own-server setup (17 Oct 2026)

- Sharing the code: `bash deploy/make-share-copy.command [their empty repo]` pushes today's `main` as one fresh
  commit (no history, no STATUS/DECISIONS/handoff notes, a general CLAUDE.md) or makes a `.bundle` file to send.
- `docs/SERVER_SETUP.md`: a new person goes from a Lightsail account to their own dashboard, engine and Mac.
  One command for a new server: `sudo bash deploy/new_server.sh`. Optional add-ons: `deploy/add_https.sh <domain>`
  (Caddy, for the phone app) and `deploy/add_us_stocks.sh` (IBKR; US hidden on the dashboard until installed).
- The Mac launchers now read the server and key from `~/.algo-trading/server.env`. **Hamid:** the first launcher
  after this change asks for the server address once (it offers the key in ~/Downloads); then it works as before.
- The Update button's repo comes from the server's own copy (`/opt/algo-trading/repo`), written by every update.

## Live on the server (as of 15 Oct 2026)

- Engine deployed 14 Oct 2026 23:36 IST from the Mac folder (paste-file update).
- Strategies available: short_straddle, blaze_butterfly, double_calendar, iron_fly, event_iron_fly,
  intraday_straddle, intraday_strangle, premium_strangle, put_credit_spread, straddle_trend_switch.
- Paper session on: straddle_trend_switch (Zerodha live prices, no real orders); other sessions as
  shown on the dashboard's Paper page.
- Old Blaze engine `trader-paper` disabled (Blaze runs as an Algo Lab strategy; `/algo` → `/paper`).
- Margins checked every 30 min in market hours (`algolab-margins.timer`); needs the day's Zerodha login.
- IBKR gateway and `ibkr-sync` stopped; `/us` page installed but never seen live.
- Daily IBKR snapshot (pull request "ibkr-daily-snapshot"): a Claude scheduled task reads the account through
  Claude's IBKR connection twice a day, 5 minutes after the US open and close (09:35 and 16:05 New York time, weekdays),
  and saves `ibkr_snapshot.json` on the repo's `ibkr-snapshot` branch; the server fetches it 10 minutes after each run
  and hourly (`algolab-ibkr-snapshot.timer`, `deploy/ibkr_snapshot_pull.sh`); the `/us` page keeps a history row per run. No IB Gateway needed; a running `ibkr-sync` (newer data) wins.
- Daily recording at 15:45 IST: NIFTY ±10 strikes, puts to 30 strikes below, two nearest expiries.
- The portfolio dashboard on the server (`/opt/trader`) was patched by scripts; its code may differ
  from `server/portfolio/`. Reconcile before phase 4.
- The server update also ships the dashboard's program files (pull request "ship-dashboard-app"): app.py and its
  panels from server/portfolio, backed up and put back if a page fails. Before, only pages and Algo Lab panels went.
- Update from GitHub (pull request "self-update"): Profile › Server version shows the running version, the latest
  merged one and what is waiting, with Check and Update now (owner only). The server pulls `main` with a read-only
  deploy key and runs the usual update (backup, checks, rollback). Set-up: one Mac update installs it; then add the
  key shown on that card to GitHub › Deploy keys (read-only) and tap Check.
- Reconciled 16 Oct: the server's templates matched `server/portfolio/templates/` exactly; `app.py` differed only by
  the Order test page, now in the repo (`order_test_panel.py`). The server update ships `server/portfolio/templates/`
  from now on (backed up, checked page by page, rolled back if a page fails). Next: restyle those pages.
- **One data set on the Mac (21 Oct 2026):** `~/Trading/algotrading/data` is the only copy used. The old
  `~/AI - VS/algo-lab/data` was merged into it (69 NIFTY 13-Oct-expiry option files had extra rows: 237,682 added),
  set aside as `algo-lab/data-old-copy-20261021`, and `algo-lab/data` is now a link to the repo's data folder.
  Last-day download finished: 56,032 option contracts (118 M candles), 215 stock/future/MCX series (41.5 M bars),
  1,846 tick symbol-days with bid/ask (14.8 M ticks; NIFTY, Bank Nifty, Sensex, 14–21 Oct), MCX near-month
  futures 2 years (`data/truedata/equity/<NAME>-I_1min.csv.gz`). Review from 11 Sep
  (`data/review/2026-10-21-last-day`): 3 FAIL (Sensex 1 Oct 75800–76000 PE below intrinsic on 25 Sep), 163 one-bar
  spikes, 43 thin Sensex contracts listed to re-download. The server copy is still the 16 Oct one.
- **TrueData last day (21 Oct 2026, pull request "truedata-last-day"):** `deploy/get-truedata-final.command` tops up
  NIFTY, Bank Nifty and Sensex for the last 40 days: index, VIX, near-month future, option candles for contracts that
  expired since 14 Oct **and** contracts still open (`--open`, their days so far), tick history with best bid/ask for the
  last 5 trading days near the money (`truedata ticks`, `data/truedata/ticks/<UND>/<day>/<symbol>.csv.gz`), and the
  NIFTY 50 stocks/futures. Not run yet when this was written; trial ends 22 Oct.
- NIFTY options, 15 Apr–14 Oct 2026: 4,994 contracts, last 35 days before each expiry, strikes within
  10% of spot, ~13.9 M one-minute candles.
- Bank Nifty options, same period: 1,479 contracts (monthly expiries), ~5.1 M candles.
- 15 Oct 2024–14 Apr 2026: NIFTY complete (all contracts with the 6 months above: 20,215 downloaded, 189
  never traded); Bank Nifty downloading (2,217 contracts left at 13:15 on 15 Oct).
- Data review, 15 Oct: TrueData's NIFTY 50 and NIFTY BANK index missed about 15:16–15:28 every day
  from 3 Aug 2026 (options complete). NIFTY filled from Zerodha the same day (51 days now complete,
  backup `TD_NIFTY_1minute.before-fill-20261015-1306.csv`); Bank Nifty still has the gap (no Zerodha
  1-minute file for it). TrueData and Zerodha NIFTY agree (median difference 0, largest 4.8 pts).
  Filler bars 18 Mar 2026 03:35–06:54 (ignored). Within 7 days of expiry, NIFTY at-the-money data is
  complete. Re-run the review once Bank Nifty finishes.
- Bank Nifty 15 Oct 2024–14 Apr 2026: complete (6,566 contracts downloaded, 177 never traded).
- Review after both finished (15 Oct 13:50): nothing to re-download; within 7 days of expiry the
  at-the-money strikes are well traded on 100% (NIFTY) and 99% (Bank Nifty) of expiry-days.
- **Closing auction from 3 Aug 2026:** NSE's cash market closes by auction (15:15–15:35) and F&O
  trades until 15:40. The NIFTY / NIFTY BANK index does not move continuously after 15:15 (TrueData has
  no bars; Zerodha shows it frozen, then a jump). Backtests should price exits after 15:15 from
  options or futures, not the index; `feeds.py` still trims bars at 15:30 (open item).
- Next downloads: Sensex options (same rules, 2 years), then NIFTY 50 stocks and their near-month
  futures (1-minute, 2 years).
- TrueData has 1-minute expired-option data back to about 24 months; at 36 months daily bars only.
- A second copy is on the server since 16 Oct (see below); the Mac copy is the only other one.
- Copied to the server on 16 Oct 13:12–13:21 Qatar (`deploy/send-data-to-server.command`, choice 2): all 11 parts
  (NIFTY, Bank Nifty and Sensex options, index bars, TrueData raw backup incl. stocks and futures), 4.9 GB; the server's
  data folder holds about 4.95 GB, about 44.5 GB free. The closing line of that run misread a warning and printed
  "0.00 GB; 4.95 GB free" (fixed in pull request "send-data-summary").

- **Algo Lab for a friend (17 Oct 2026):** `deploy/make-friend-bundle.command` on the Mac makes
  `~/Desktop/AlgoLab-<date>.tar.gz`: the backtest program and stored prices, without the server, deploy
  scripts, notes, tests, logins or results; stops if it finds a secret or the server address.

- **Algo Studio (19 Oct 2026, step 1 of 5):** a second web app on the server at `https://<address>/studio/`
  (`studio/`, service `algolab-studio` on 127.0.0.1:8020, Caddy `handle /studio*` added by `deploy/studio_caddy.sh`).
  One login: it asks the dashboard's `/api/studio-auth` with the browser's cookie. Pages: Strategies (stage, presets,
  last backtest, paper/shadow/live totals) and each strategy's Overview, Presets, Backtests, Sessions. Presets live in
  `runtime/presets.json`; backtests and sessions started with one record it. Run, Sweep and Start still use the old
  Algo Lab pages, prefilled from the chosen preset.
  Step 2 (19 Oct): Overview › Start runs a backtest or starts paper/shadow right there from a preset, lots chosen at
  start (calls the dashboard's own /api/backtest-runner and /api/paper/start); Backtests › Save as preset keeps a
  run's settings incl. Orders & stops. Live still starts from the old Live page.
  Step 3 (19 Oct): Analysis tab (same days in backtest / paper / shadow, what the gap is made of, the latest
  backtest's robustness and months; Compare queues a "compare" job: algolab/backtest_queue.py run_compare, kept in
  runtime/paper_compare.json) and stage gates on Overview (studio/analysis.py, advice only; limits in config.yaml
  studio.gates). Paper-vs-backtest now backtests with the session's Orders & stops.
  Step 4 (19 Oct): Share / Import (studio/share.py): a preset as a share code `ALGOSHARE1.…` or file, with an optional
  backtest summary and results snapshot; never logins, account, positions or server address. Import checks the strategy
  exists and the settings are valid; a later code with the same share id shows what changed (update or keep as new).
  Shares made are listed in runtime/shares.json.
  Step 5 (19 Oct): Strategy Developer (`#/build`, algolab/built.py): strategies made from blocks (entry days, times,
  condition; up to 6 legs; re-entry; exits) saved as specs in runtime/built_strategies/b_*.json and run by one engine
  strategy in backtests, sweeps, paper and shadow. Live is off for built strategies (live_ready False). Server only:
  the Mac helper does not have the specs (Studio offers Server only for them).
  Results (19 Oct): Studio › Results (studio/results.py) groups every backtest and sweep by strategy and preset, names runs
  in words (preset · period · data set · fills, Mac/Server and paper-vs-backtest badges), filters and sorts; a starred
  reference run per preset is what gates, Analysis and Share use; archive (runtime/results_meta.json) and batch delete
  (through the dashboard's delete). Strategy cards link to their results.
  Part B: a run page in Studio (#/run/<name>: figures, P&L and drawdown chart, months, settings used, worst/best
  cycles, Save as preset, Run again), Compare 2–3 runs (#/compare: overlaid P&L, figures, settings that differ) and a
  sweep / Find best page (#/sweep/<id>: picks with full settings, Save as preset, Save & backtest).
  TrueData test (19 Oct): the trial's real-time feed answers "User Subscription Expired" (subscription null, valid to
  22 Oct; segments NSE, FO, MCX); the test now shows TrueData's own reply. TrueData support (20 Oct): trial accounts connect on port 8086 (8082 is
  Market Replay); the test now tries 8086 first.

- **Simulated paper sessions (19 Oct):** made-up prices replayed fast (a 5-minute bar every 0.5 s: a trading day in
  ~40 s), so their date runs ahead (one reached Dec 2027). Now labelled "Simulated prices · fast replay", not counted
  as paper trading in Studio (stage, totals, gates, Analysis, Share), and stopped by themselves after a replayed year.
  Studio's Start defaults to Zerodha live prices for paper.

## Open items

- **Holiday clean-up (21 Oct 2026): undone.** The clean-up reset every session holding a position opened on 20 Oct,
  wiping their whole history including 21 Oct. The next update restores `runtime/backups/sessions-before-scrub-*.tgz`
  once (`run.py paper-restore`); updates no longer run `paper-scrub`. The 20 Oct recorded prices stay deleted.
  Restored 21 Oct. Then (approved) the next update removes only four cycles opened and closed on 20 Oct
  (intraday straddle-2 004, intraday strangle 005, intraday strangle-2 002, straddle grid trend-2 006); sessions
  holding positions entered on 20 Oct are left as they are.
- **Honest fills (18 Oct 2026):** backtests no longer flatter cheap options. Without a bid/ask a fill pays at least Rs 0.10
  per side under Rs 20 and Rs 0.20 under Rs 100 (else 0.5%); decisions never see a later trade (the strike list holds only
  strikes traded so far); a fill needs a trade at most 3 minutes old, else it waits for the next one (15 min). Run and
  Sweep have Fills: Conservative (spread x2, filled a minute after the decision). **Results before this change are
  more optimistic: re-run strategies you rely on.**
  Results also show Sortino, Calmar, best/worst day, expectancy, days in a trade, worst/best move per cycle and a
  breakdown by year/month/weekday/exit reason; yardsticks: P&L before charges and the index move over the period. Server
  runs record the deployed GitHub commit as their code version.
  Liquidity (needs TrueData raw candles in `data/truedata/options`): Run/Sweep can skip strikes with fewer contracts
  traded so far that day; orders over 10% of the last 5 minutes' volume are counted on the result.
  Inside a bar, options now follow their TrueData high/low (a call at its low when NIFTY is, a put at its high), so
  option stops and targets touched mid-bar are hit (`execution.option_intrabar`).
  Margin (estimate, `algolab/margin_est.py`, `account.margin`): peak and average margin, return on peak margin, and
  entries that needed more than the account had (`check: refuse` also skips them).
  Robustness: every result shows a Monte Carlo reshuffle of its cycles (drawdown 1 in 20 worse than, chance of a losing
  period) and the P&L with 50% higher charges; Sweep has Walk-forward (3–6 periods) and warns how much of the best
  result is luck when many combinations were tried.
  Weekly paper vs backtest (`algolab/paper_report.py`, timer `algolab-paper-report`, Saturdays 08:05 IST): each paper
  or shadow session on live prices is backtested over its week; day-by-day P&L and fill differences go on the
  Backtests page ("Paper vs backtest"), a summary to the phone, the detail to `runtime/paper_report.json`.
  Find best settings (Sweep › Mode, `algolab/optimise.py`): locks away the last months, walk-forward on the rest,
  drops fragile settings, ranks on steadiness, re-checks the top 3 with Conservative fills and on the locked-away
  months; each pick has reasons and a Start paper session button.

- **Server version at a glance (19 Oct 2026, pull request "update-status"):** the server looks at GitHub quietly every
  30 minutes (`algolab-selfupdate-check.timer`, `self_update.sh peek`). Owner only: a dot on the profile photo in the
  menu while an update waits (amber), runs (blue) or failed (red); a card at the top of Summary with what is waiting and
  Update now (two taps), or one green line when up to date; the Server version card is first on Profile with the same
  status in large type.

- **Run on Mac or Server (18 Oct 2026):** Algo Lab › Run and Sweep have a Run on choice. Mac jobs wait in the dashboard's job
  list until the Mac helper (`run.py lab-agent`, installed by `deploy/install-mac-agent.command`) collects them; it copies new
  prices from the server first, runs the job, publishes the result. The page shows the Mac online or asleep.

- **Algo Lab › Sweep (18 Oct 2026):** like the Mac app's: Vary per setting with from/to/step ranges (times in minutes) or
  choice sets, an out-of-sample check (rank on the earlier part, see the later), Run on Mac (up to 400 combinations) or
  server (60); results: rank by any metric or by robustness (average with neighbours), in vs out scatter, heatmap of
  two settings, line for one, CSV, and Run any row as a full backtest.

- **Algo Lab menu (18 Oct 2026):** backtests moved out of Paper into their own menu (Run, Results); Results has a P&L
  calendar (a square per trading day; tap one for the cycles that closed that day).
- **Zerodha login only on trading days (18 Oct 2026):** no banner, key icon or 08:50/09:05 reminder on weekends or NSE
  holidays (`config.yaml` market.holidays). **2027 holidays still to add** once NSE publishes them (usually December).

- **Moving to Mumbai, 4 GB (18 Oct 2026):** by Lightsail snapshot. The dashboard's address is an sslip.io name made from
  the IP, so it changes with the move; every update now checks it (`deploy/set_address.sh`) and switches Caddy to the
  new name (or to your own domain: `DASH_HOST=dash.example.com bash deploy/update-server.command`).

- **New strategy `opening_move_itm` (22 Oct 2026, paper only, not live-ready):** from 09:16 until 09:45 it buys a call 100 points in the money when NIFTY breaks
  the high, or a put 100 in the money when it breaks the low (first break only); stop 5 points below the entry,
  trailing 5 behind the option's best price; one trade a day, square-off 15:15. Options: a break buffer, a later start for counting breaks, stop on NIFTY instead of the option, a target. A paper session must be running at 09:15. Run a backtest on
  `TD_NIFTY_1minute` before a paper session: on model prices the 5-point stop was hit within about 2 minutes almost
  every day, with exits well below the stop.
- **New strategy `open_high_retest` (21 Oct 2026, not live-ready):** from 13:00, among the nearest-expiry calls and
  puts (ATM ±10 strikes at 13:00, opened at Rs 30+) whose open is still the day's high (open and high from Zerodha's
  quote or the stored candles, so a session can start any time), buys one that has fallen more
  than 15 below its open when it comes back within 15; target the open; stop 10 below the entry, trailing near the
  target at best - (open - best). Square-off 15:15. Needs a backtest on TrueData 1-minute prices before a paper session.
- **New strategy `expiry_doubler` (18 Oct 2026, not live-ready):** expiry days only; buys the call/put priced just under
  Rs 30 (from 11:00) once it doubles; 20-point stop rising 2 per point gained, then 10 behind the high; re-entry 10
  above a stop-out. Needs a backtest
  on real expiry-day prices (TrueData 1-minute) before any paper session.
- **Paper/Live positions closed in a cycle (19 Oct 2026, pull request "cycle-positions"):** under Positions, the legs
  already closed in the open cycle (a roll, a stopped side) at quantity 0 with entry → exit and what each booked; each
  row of Closed cycles opens to the positions it closed (`/api/paper/cycle`, asked for on tap, so the 3-second refresh
  stays small).
- **Backtests page (18 Oct 2026):** tap a cycle to see its trades (each buy/sell with times, prices, why it
  entered and exited, net). Runs now carry their orders (`fills.csv`); older runs show the legs without the entry reason.
- **Backtests on the server (18 Oct 2026):** Paper › Run a backtest on the dashboard queues a run; the engine runs it at
  low priority (`algolab-backtest.path`, nice 19, CPU 60 %, 800 MB) on the server's stored prices and the result shows
  under Backtests. Installed by the next update (Update now). Saved runs: `/opt/algolab/runs/`.
  A job the server stops (over 800 MB, over 3 hours, a restart) now shows as failed with the reason instead of its
  last progress (19 Oct 2026, systemd `ExecStopPost` → `run.py backtest-queue --stopped`).

Security and money first.

- **Change the portfolio dashboard password**: it appeared in a chat. Change it on the dashboard (rewrites
  `/opt/trader/config.json`), then update the Mac's `secrets.yaml` to match.
- Delete the old Lightsail instance "Claude-Zerodha" and the 8 Oct snapshot (they cost money).
- AWS Free plan ends 8 Apr 2027 (server suspended unless upgraded); confirm the server is in the billed
  account (billing showed $0.05). Reminder set for 1 Mar 2027.
- `data/truedata` now has a second copy on the server (16 Oct); an off-site backup is still open.

Trading.

- Straddle trend switch lost Rs 2.08 lakh over 6 months of real prices (profit factor 0.66); its paper
  session still runs. Decide: stop it, or keep it as a live check.
- `premium_strangle` had 3 modelled price look-ups in 6 months (no fills); flagged in results.
- Backtests after 3 Aug 2026: use options/futures prices after 15:15 (index frozen by the closing
  auction) and allow trading until 15:40 (`MARKET_CLOSE` in `algolab/feeds.py` is 15:30).
- NIFTY 50 stock data covers today's members only; past members (e.g. Wipro until 30 Sep 2026) are not
  downloaded, so stock backtests over 2 years have survivorship bias.
- Bank Nifty backtests need its own market settings (lot size, strike step 100) in `config.yaml`.
- Recheck charges and lot size (65) in `config.yaml` against Zerodha.

Platform.

- Paper/Live session card in the Overview's visual style (pull request "ui-sessions"): one header line with icon
  buttons (square off, stop, copy; resume, reset, delete), chips for changed settings, margin and limits; Total / Today
  / Open; positions and closed cycles as two-line rows that fit a phone; payoff figures in one row plus a line;
  shorter chart dates. Not deployed yet.
- Menu (pull request "ui-menu"): Paper and Live open on Overview, which is the first sub-page; Chart moved under Data
  (Data › Stored data / Chart).
- Summary page (pull request "summary-us-indices"): Include US switch (remembered per device; off hides the US card
  and shows net worth for India only); Market card shows NIFTY 50, BANK NIFTY, SENSEX and India VIX with the day's
  change (Zerodha quotes; falls back to the engine's NIFTY when Zerodha is not logged in).
- Summary page in the Overview's visual style (pull request "ui-summary"): net worth with an India/US bar; Market
  moved up; India and US as three numbers with icon chips; Paper/Live as the Overview's compact list; System and
  Server merged into one Health card of lights ("all OK", details on tap); alerts with short times. Not deployed yet.
- Portfolio (India) page in the Overview's visual style (pull request "ui-portfolio"): status as an icon and the time once;
  pause and CSV as icon buttons; Value / Today / Total P&L as three numbers with chips (holdings, gold share, up/down
  count); today's range as one track; chart tabs Today / Stocks / Days; holdings as two-line rows that fit a phone with a
  Day / Total switch (sort by tapping a heading); Alerts folded, with the test alert as an icon inside. Ships with the
  server update (needs pull request "dashboard-pages-route" first). Not deployed yet.
- Paper/Live split (pull request "paper-overview"): Overview (`/paper/overview`, `/live/overview`) has total and day
  P&L, the list of strategies, every session's payoff and the calendar for all sessions. Sessions shows the strategies
  as buttons (name and day P&L, three to a row) and the chosen one's details in collapsible sections (remembered per
  device, Expand all / Collapse all).
- Stocks page in the Overview's visual style (pull request "ui-stock"): stock picker, a feed icon and a live-chart button
  in one line; Value / P&L / Invested as three numbers with chips (units, average cost, booked profit, trade dates); one
  chart with Value / P&L / Added and 3M / 1Y / All switches, the period's high and low as ▲/▼; trades as two-line rows
  (B/S badge); corporate actions, notes and the tradebook import folded. Not deployed yet.
- UI review fixes (pull request "ui-review-fixes", 16 Oct, ui-review checklist): text colours pass WCAG AA contrast in
  light mode (darker green, red, grey, amber); white-on-blue buttons in dark mode use dark text; no text under 12px on the
  restyled pages and the Algo Lab labels; switches, folded-section headings, stock names and contract links have bigger
  tap areas; labels on the symbol, data set and tradebook inputs; a main landmark, a visible keyboard focus ring and
  calmer motion when the phone asks for it, on every page. Also brings the no-sign change to Portfolio and Stocks (it
  missed #22 and #23). Not deployed yet.
- Portfolio follow-ups (pull request "ui-portfolio-2"): India: Holdings folded by default (remembered per device) with the
  day's best and worst in its heading; holdings up / down counted for today and since bought. Stocks: a tap on the Buys or
  Sells chip lists only those trades (tap again for all).
- Portfolio and Summary keep the last trading day's day P&L outside market hours (pull request "portfolio-last-day"):
  evenings, weekends and before 9:15 IST show that day's figures (portfolio, each holding, the range and the day chart),
  labelled e.g. "Fri 16 Oct" with a note "Market closed · Fri 16 Oct shown until 9:15 IST". Portfolio's chart tabs and
  stock picker now fit one line on a phone.
- Paper/Live pages no longer jump up while you read the bottom on an iPhone (pull request "paper-scroll"): the
  3-second refresh holds the cards' height, keeps your place, pauses while you scroll and skips unchanged data.
- Algo Lab (Mac) UI review, part 1 (pull request "algolab-ui-1"): reds that keep white text readable (light #c81e1e,
  dark #d93636); NIFTY's day change and the paper account change shown with ▲/▼ in green/red and no sign; chart axis
  text and chart labels darker and at least 12px; strategy logs as plain wrapped text; sweep colours readable for
  colour-blind eyes; "Reset this strategy's account" asks to confirm. Mac only: take it with `git pull`.
- Algo Lab (Mac) UI review, part 2 (pull request "algolab-ui-2", after part 1): results open with Net P&L, Max drawdown
  and Return / drawdown side by side, chips for cycles, win rate, profit factor, return a year, worst week and charges,
  and the full list folded under "All figures"; cycles, legs and paper trades with plain column names, short times and ₹
  amounts; sweep table and heatmap with the settings' own names and ₹ amounts, results named "16 Oct 12:27 · strategy";
  Data tab: quiet calendar in dark mode, ₹ ticks on the price chart, disk labels that fit, "Get data from server" only
  when a server is filled in. Mac only.
- Algo Lab (Mac) Data tab redesigned (pull request "mac-data-tab"): per underlying, headline numbers, a day-by-day
  calendar of stored data, index and VIX chart, expiry timeline, option chain by strike and a contract's prices,
  the latest data review, disk use and the TrueData download record; the download forms are folded below.
- Parameter sweep tab in the Mac backtest dashboard (pull request "sweep-tab"): pick settings to vary and their
  ranges, run every combination in the background (all cores, in-sample / out-of-sample), then a ranked table,
  heatmap, in-vs-out scatter and a robustness score; backtest a chosen combination in one click. Mac only.
  Safer since 16 Oct (pull request "sweep-safer"): one sweep at a time; parallel runs sized to memory (~2.5 GB
  each); results saved as each combination finishes; the Mac kept awake. A full backtest of 2 years of 1-minute
  data takes minutes, so keep sweeps to tens of combinations, not hundreds.
- Time zone choice (pull request "ui-timezone"): Profile › "Show times in": India time (IST, the default) or this device's
  time. Applies to every time on every page (updates, alerts, trades, cycles, charts, recordings, backtests). In IST mode
  times with a zone are now shown in IST too (some were shown in the device's zone before).
- Refreshes keep what is being read (pull request "keep-details-open", 19 Oct): Summary's Details stay open (and scrolled)
  through the 15-second refresh; on Paper/Live the Activity log and tables keep their scroll position.
- Chart page and index strip (pull request "live-chart"): `/chart` shows any NSE/BSE stock, index or F&O
  contract with live price (every 2 s in market hours) and 1D–5Y line or candle charts from Zerodha; holdings
  names on Portfolio and contracts on Paper/Live open it. Portfolio shows NIFTY, SENSEX and BANK NIFTY with
  their change from the previous close. Needs the day's Zerodha login. Not deployed yet.
- No + / − on green and red figures (pull request "ui-no-signs"): Summary, Paper/Live (sessions, Overview, payoff,
  calendar, metrics), index strip, Chart and Data pages; highs and lows are coloured by gain or loss. Not deployed yet.
- Portfolio Total P&L chart (pull request "ui-pnl-chart"): a "Total" tab next to Today / Stocks / Days shows the total
  P&L for each day as the close (bold line) with the day's high and low (thin lines edging a band), the best high and
  worst low marked; for the whole portfolio or one stock (picker). Days recorded before this change have only one
  snapshot, so their high and low equal the close.
- Payoff charts on the Paper and Live pages (pull request "payoff-chart"): one chart per session with open
  legs (P&L at the first expiry and today, index marker, breakevens, max profit/loss, delta/theta/vega),
  plus days-ahead and volatility what-if sliders on the selected session. Not deployed yet; deploy after
  merge, outside market hours. Values come from each leg's implied volatility at its live price
  (India VIX when a price gives none); entry charges included, exit charges not.
- US page in the Overview's visual style (pull request "ui-us"): feed icon and time once, $ / ₹ switch; Value / Today /
  P&L as three numbers with chips (holdings, cash, account value, USD/INR, up/down count); chart tabs Today / Days /
  Account; holdings as two-line rows with a Day / Total switch; no + / − on coloured figures. Not deployed yet.
- Calendar of closed cycles on the Paper and Live pages (pull request "pnl-calendar"): each day shaded by the
  booked P&L (after charges) of cycles that closed that day; tap a day for its cycles; one calendar per session
  and one for all sessions together. Not deployed yet.
- Backtests page in the Overview's visual style (pull request "ui-backtests"): runs as two-line rows (net, max drawdown,
  cycles and win %; tick up to 3 to compare); the chosen run as three numbers (Net, Max DD, Ret/DD) with icon chips;
  legs, log and delete as icon buttons; cycles as two-line rows (first 10, then Show all); Settings and All figures
  folded; no + / − on coloured figures. Not deployed yet.
- Data page follow-ups (pull request "ui-data-2"): the four totals in one slim card; Expiries folded by default; the
  index and India VIX chart moved to the bottom; the contract's stored period in its heading; a tap on the put line of the
  chain chart opens the put (it always opened the call).
- Index and India VIX chart moved from Data to Chart (pull request "data-chart-recording"): the Chart page has a "stored"
  card under the live chart (NIFTY / BANK NIFTY / SENSEX, data set, 1D–All). The Data page's recording box now says why
  the last recording failed instead of only "Last run failed".
- 16 Oct: the 15:45 IST recording failed (shown as 13:16 in Qatar time); reason to be read from Data › Recording log.
  The next run (Mon 19 Oct) fetches the last 7 days again, so Friday's prices are filled then if it succeeds.
- Summary › Health, Recording light (pull request "recording-light"): amber after one failed run, with when and why and
  "the next run fills it in"; red only when two or more runs in a row failed (it was red for any log line with "fail").
  Lights that are not green say why under the chips, without opening Details.
- Daily recording survives a slow Zerodha (pull request "collect-retry"): 16 Oct 15:45 IST it failed on one 7-second
  read timeout. Now each request waits up to 30 s and is tried again up to 4 times; a contract that still fails is
  skipped and named in the log (the run fails only if most do); the whole run is retried twice, 2 minutes apart.
- Data page rewritten (pull request "data-page"): NIFTY / BANK NIFTY / SENSEX tabs; a day-by-day calendar of
  what is stored (index bars, option contracts, short days); index chart with data set and range; expiries as a
  timeline; option chain as a chart (calls and puts by strike) or table; disk use by folder. Big files are no
  longer read through (row counts estimated), so the page stays fast with the 2-year TrueData copy. Not deployed yet.
- 2 known test failures in `tests/test_summary.py` (marked xfail): one expects a different `/api/summary`
  than the deployed Summary page; one predates real-prices-only backtests. Decide which Summary API is wanted.
- Summary page shows "Live prices … (1000000000s old)": `_health()` in `deploy/portfolio_page/algolab_panel.py`
  reads `feed_status.json` as a timestamp; should use the file's modification time.
- Nightly recording is 5-minute; 1-minute would need `collect_daily` in `algolab/kite.py`.
- IBKR: automatic gateway login fails; weekly manual re-login from the Mac (`ibkr-login`, approve on phone,
  `ibkr-done`). Server-only IBKR settings are not in the repo (`~/ibkr/.env`, `docker-compose.yml` viewer
  password and port, `ibkr-sync.service` UMask).
- The algo engine ignores the dashboard's "Pause sync".
- Server address and key path are written into launchers (`deploy/*.command`); move to local settings.
- NSE holidays live in `algolab/nse_holidays.txt` (shipped with every update; `config.yaml` can add more). 2026 only; add 2027 when NSE publishes them.

Later.

- Friend replication bundle (see `docs/handoff/2026-10-15-friend-bundle-plan.md`).
- US investing: monthly-buy planner, then order placement with phone approval.
- **Live prices every second (19 Oct 2026):** the engine writes each session's latest prices and P&L to
  `runtime/sessions/<id>/tick.json` every step; Paper/Live pages update NIFTY, VIX, legs and P&L in place every second in
  market hours (`/api/paper/ticks`); the full page still refreshes every 3 s.
- **Orders & stops framework (19 Oct 2026, in progress):** `algolab/risk.py` applies one set of stop rules to any strategy
  in backtests, paper, shadow and live: per-leg stop and trailing, whole-position loss/target/profit lock, day loss/profit.
  Per session or job (`risk` settings): `stops` = own (as before) | framework (the strategy's own stops off: `STOP_OFF`)
  | both. Settings under "Orders & stops" on Start a session, Run and Sweep (sweepable), with per-strategy defaults
  (`runtime/risk_defaults.json`). Live/shadow: order type limit-and-chase or market (one limit through the market),
  chase seconds/%, protective stop x; a sold leg's trailing stop moves its protective stop at Zerodha (never loosens).
- 27 Oct 2026: Data page › Look at 1-second bars: pick a day and an instrument (NIFTY, India VIX, or any recorded call or put) and see 1-second candles of any length (1 s to 5 min), with a table of the last 300 candles (bid, ask, volume, OI, ticks).
- 27 Oct 2026: Backtests › Compare with paper: each day has a Trades button that shows the session's trades and the backtests' trades (contract, side, qty, entry and exit time and price, net P&L, exit reason).
- 27 Oct 2026: Compare with paper: on a day the session started late, both backtests start at the same time (its first price that day). The selected report in the list is highlighted.
- 28 Oct 2026: Paper and shadow session cards have Deploy: it opens Live › Start a session with every setting carried over (strategy, parameters, Orders & stops, live limits) and a list of what could make the new session behave unlike the source (orders above max lots with Raise / allow-larger choices, order type, protective stops, LIVE availability, daily loss limit, shadow history, simulated prices). Warnings never block. The source keeps running.
- 28 Oct 2026: Live / shadow sessions can allow orders above max lots (per session, off by default): placed with a warning in the log and an alert. Daily loss limit and orders per day still stop the session.
- 28 Oct 2026: Data › 1-second recordings redesigned: a card per day with how much of 09:15–15:30 has ticks (gaps of 5+ min shown), the day's options as a strike ladder (calls, strike, puts; coverage per option; a dot for options a session traded; ATM marked), and a chart with 1s / 1m / 5m / 30m candles, periods, zoom in / out and a whole-day window to drag. 1-second candles load for the chosen period (up to 6,000 candles).
- 27 Oct 2026: Compare with paper: each finished comparison has a Delete button (tap twice). It removes the report and its job record; a running one cannot be deleted. Same permission as running comparisons.
- 28 Oct 2026: Compare with paper redesigned: totals with how far each backtest is from the sessions, a line on why when a session stopped itself (from its log: max lots, daily loss, rejection...), each session with its badges and P&L bars, and its trades leg by leg side by side (or fill prices, with better/worse per fill). Delete for finished comparisons (#121) is included.
- 29 Oct 2026: Compare with paper counts only trades the session opened inside the period (a backtest starts flat). Trades opened before the period that closed in it, and positions still held from before, are shown in a "Not compared" box with their P&L, and are not in the Session figures.
- 30 Oct 2026: Compare with paper: trades opened before the period are no longer shown leg by leg; one "Not compared" line per session at the bottom gives the strategy and the reason.
- 30 Oct 2026: Live entries with two legs (Intraday strangle, Intraday straddle, Straddle trend switch) send both orders at once and wait for both fills, instead of selling the second leg only after the first has filled (about 5 s later). Exits, re-entries and backtests are unchanged. If one leg fails, the leg that filled is booked with its protective stop and the session stops and squares off, as before.
- 2 Nov 2026: Stop works outside market hours. A session with no open positions stops within a second of Stop (it used to wait for the next price update, 09:15 when pressed before the open, showing "Squaring off…"), including one that is not running yet (waiting for the day's login); any protective stop it still had at Zerodha is cancelled. With open positions the square-off still waits for live prices; the card says "Squaring off at the next market update…". The button reads "Stop (no open positions)" when the session holds nothing.
- 2 Nov 2026: Several LIVE sessions at once (pull request "multi-live", needs "stop-when-closed"). Up to a limit the owner sets on Live › Overview (default 3, 1–10; `runtime/live_settings.json`); shadow and paper never count. Sessions may hold the same contract: before its first order each live session checks that Zerodha's net NRML position equals its own legs plus the other running live sessions' legs (it still stops without trading on any other difference). Each session keeps its own lots, orders per day, daily loss limit and protective stops sized to its own quantity. Live › Overview shows how many LIVE sessions run and has Stop all LIVE (tap twice): every live session squares off its own legs and stops. Known limit: all Zerodha sessions run on one thread, so while one session re-prices an order (up to 30 s) the others wait; their protective stops at Zerodha keep working meanwhile.
- 2 Nov 2026: Live safety for several LIVE sessions. (1) A session may not open a contract another LIVE session holds the other way round (Zerodha nets the two, so an exit would open a new position there); it stops and squares off instead. (2) Buying a leg back waits until Zerodha confirms its protective stop is cancelled; a stop that filled meanwhile is booked instead of buying again, a part-filled stop leaves only the rest to buy, and if the cancel cannot be confirmed nothing is bought (the stop stays) and an alert goes out. (3) Every minute while trading, each live session checks Zerodha's net position in its contracts against its own legs plus the other live sessions'; a difference at two checks in a row stops it without sending anything. Positions at Zerodha that no live session holds are reported once a day (no stop: Positions page legs live there too).
- 2 Nov 2026: Live › Overview has a Both / LIVE / SHADOW filter above the session list. It filters the totals, the list, the payoffs and the calendar; the choice is remembered on that device. Live › Sessions and Paper are unchanged.
- 17 Nov 2026: Email (Gmail) and Telegram alerts set on Profile › Email and Telegram alerts (owner only): switch each on, Gmail app password, Telegram bot token, Find my chat id, Send a test. Saved in the dashboard's `config.json` (`email`, `telegram`), file readable by its owner only; used at once by the dashboard's price alerts and by live / shadow sessions (every order, protective stop, square-off and failed close). The engine now sends these from a background thread, so an alert never holds up an order.
- 28 Nov 2026: **1-second bars are the primary backtest source** (Hamid: more reliable than 1-minute). Algo Lab › Run a backtest, Sweep (plain, out-of-sample date, walk-forward, Find best settings) and Algo Studio's Backtest list **NIFTY 1-second** first and pick it by default, with From / To set to the first and last day that has 1-second bars (`data/sec1/<day>/NIFTY.csv`; the date pickers stop there). Days without 1-second bars inside the range are left out and named in the result. Runs on the server or the Mac (the Mac needs `deploy/get-server-data.command` first for the days). Walk-forward needs at least a week per period and Find best settings about 2 months, so they only fit once more days are kept (`sec1.keep_trading_days`, default 5, up to 30 on Compare with paper). 1-minute sets stay in the lists; `run.py backtest` still defaults to NIFTY_minute.
- 28 Nov 2026: 1-second backtests ~4x faster and much lighter: picking strikes reads only the top of each strike's file (was: every strike's whole file, ~300 files / 320 MB a day, again each day; on the server's 800 MB / 60% CPU runner a 22-day run crawled). Whole files load only for strikes a strategy prices. Run a backtest shows "day N of M" for 1-second runs, and Mac jobs show "copying new prices from the server" while the helper syncs (the first sync with 1-second bars copies every kept day, ~320 MB each).
