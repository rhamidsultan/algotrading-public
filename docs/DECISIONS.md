# Decisions

Dated, newest first. One line each; link the pull request when there is one.

- 2026-10-26 — One 1-minute price set per index (NIFTY, Bank Nifty, Sensex): `data/spot/<INDEX>_minute.csv` =
  TrueData history + Zerodha's daily bars. With the 1-second bars of the last few trading days, these alone show on
  the Data and Backtests screens. Old copies (TrueData-only 1-minute files, pre-fill backups) move to `data/archive/`;
  nothing is deleted. NIFTY_5minute stays in place (nightly job, straddle_trend_switch) but is hidden; the sample
  file ships with the code and stays (pull request "index-data-sets").
- 2026-10-26 — Session cards: "Unrealized" instead of "Open"; P&L chart switch Overall | Current cycle (#109, #110).
- 2026-10-26 — Daily paper (and live) vs backtest on 1-minute and 1-second bars. No TrueData API, so the 1-second
  bars are recorded on the server from Zerodha's live stream (a missed day cannot be fetched later); NIFTY, VIX and
  ±30 strikes on the two nearest expiries plus every option in use. Kept `sec1.keep_trading_days` (default 5,
  configurable) NSE trading days. The 1-second backtest steps the whole strategy every second; each comparison
  backtest starts flat at 09:15 on the first day; live sessions are shown apart from paper; comparisons run on the
  server outside market hours, started by a button on the Backtests page (pull request "sec1-recorder" and follow-ups).
- 2026-10-21 — One data set: `~/Trading/algotrading/data` on the Mac (old algo-lab folder links to it); the server
  holds a copy sent from it.
- 2026-10-21 — Before the TrueData trial ends, keep what no other source will give later: expired and open
  option candles with OI, near-month futures, and the last 5 days of ticks with bid/ask (pull request "truedata-last-day").
- 2026-10-16 — Code updates (engine, dashboard pages) may be deployed in market hours; only data copies to the
  server wait until after the close (15:40 IST).
- 2026-10-16 — Restyle Portfolio, Stocks, US and Backtests only after copying the live server versions into the
  repo; then ship those pages with the normal server update (backup and rollback), not by patching from outside.
- 2026-10-16 — Dashboard figures shown in green or red carry no + / − sign: the colour says which way. Signs stay
  where there is no colour (chart axes, tooltips, inputs).
- 2026-10-16 — A profile setting chooses IST or the device's own time zone for every time shown on the dashboard.
- 2026-10-16 — The server updates itself from GitHub when Hamid taps Update on the dashboard (read-only deploy key);
  no automatic deploys and no GitHub access to the server.
- 2026-10-16 — Parameter sweeps run from the Mac dashboard (not the server): choose on in-sample, check
  out-of-sample, prefer robust plateaus.
- 2026-10-15 — Live chart page for any symbol; a tap on a holding or position opens it. Portfolio shows the
  day's change of NIFTY, SENSEX and BANK NIFTY.
- 2026-10-15 — Paper and Live pages show a live payoff chart for each running strategy's open legs.
- 2026-10-15 — Also download Sensex options (same rules as NIFTY) and NIFTY 50 stocks with their
  near-month futures (1-minute, 2 years); no stock options.

- 2026-10-15 — Data is reviewed (`data-reviewer` agent, `algolab/datacheck.py`) before it is trusted for
  backtests; fixes only fill gaps or re-download, never overwrite existing bars.

- 2026-10-15 — One private GitHub repo (`rhamidsultan/algotrading`) for all trading code, including
  the portfolio dashboard (`server/portfolio`). Chats change code only through pull requests that
  Hamid approves; the Mac and the server take merged versions only.
- 2026-10-16 — Paper/Live pages on the phone: words become icons (state dot, flask for simulated prices, a clock
  when a session lags, one bar per open leg); the time and the live-feed sign shown once in the header, not on
  every row; Overview list fits the phone screen without sideways scrolling; short strategy names in lists.
- 2026-10-15 — Backtests and replays use real option prices only (Zerodha recordings or TrueData);
  the pricing model is opt-in (`--allow-model`).
- 2026-10-15 — TrueData trial: download 2 years of 1-minute NIFTY and Bank Nifty option data
  (last 35 days before each expiry, strikes within 10% of the index).
- 2026-10-15 — Paper and Live pages get a calendar of closed-cycle P&L (per session and all sessions).
- 2026-10-14 — Dashboard becomes an installable phone app (Face ID / fingerprint, push notifications).
- Earlier — Positional short straddle as the first strategy; backtests on the Mac, paper and live
  trading on the Lightsail server; prices from Zerodha (Kite Connect).
- 2026-10-17 — Anyone with access to the repo can set up their own server from it (`docs/SERVER_SETUP.md`,
  `deploy/new_server.sh`). No server address or GitHub repo is written into the code: the Mac keeps them in
  `~/.algo-trading/server.env`, and the server keeps its repo in `/opt/algo-trading/repo`. US stocks (IBKR) is
  optional (`deploy/add_us_stocks.sh`); the dashboard hides US until it is installed.
- 2026-10-18 — New strategy idea (expiry-day option doubling, trailing stop, re-entry) built as `expiry_doubler`.
  Hamid's rules: entries only from 11:00; stop 20 below the entry, raised 2 points for every point gained until it
  trails 10 points behind the high. Defaults where the idea left room: doubling measured from the 11:00 price (or the
  day's low, a setting), at most 3 entries per side, options Rs 5-30, re-entry 10 above the stop-out price.
- 2026-10-18 — Backtests can be run from the dashboard on the server (no Mac needed), at low priority so trading
  sessions are not slowed; at most 3 waiting at a time.
- 2026-10-18 — One server for everything: move to a 4 GB Lightsail instance in Mumbai by snapshot (not a separate
  lab server). Backtesting moves into the dashboard (Algo Lab menu: Run, Sweep, Results, Replay; run on the Mac or the
  server); the Data menu stays the only data page; the Streamlit app is retired once Replay is in the dashboard.
- 2026-10-18 — Backtest fills made honest (review of Algo Lab against best practice): spread floor by premium, no later
  prices in decisions, fills wait for a recent trade, optional conservative fills. Next candidates: option highs/lows within
  bars, margin and return on margin, more figures, robustness tools, liquidity filter, paper-vs-backtest report.
- 2026-10-18: "Best settings" means steady profit with limited drawdown (return / drawdown x periods profitable), not the biggest past profit; Hamid agreed ("go").
- 2026-10-19: Orders & stops framework: per strategy, choose own stops, framework stops or both; framework covers per-leg stop + trailing, whole-position stops and day limits; default orders limit-and-chase.
- 2026-10-20: US page data from a daily snapshot made by a Claude scheduled task through Claude's IBKR connection (not live), carried to the server on a separate `ibkr-snapshot` branch of the private repo; Hamid accepted holdings figures sitting in GitHub on that branch (never on `main`).
- 2026-10-21 — New strategy idea (buy options back toward an open that is still the day's high) built as `open_high_retest`.
  Hamid's rules: from 13:00, options with open = high; buy within 15 points of the open; stop 10; target the open; near the
  target the stop is best - (open - best) (target 220, best 216 -> stop 212; best 218 -> 216). Defaults where the idea left
  room: nearest expiry, ATM ±10 strikes at 13:00, calls and puts, open at least Rs 30, open = high exactly, the price must
  first be more than 15 below the open after 13:00, one trade per option, 2 open / 4 a day, entries until 15:00, out 15:15.
- 2026-10-21 — The day's open and high come from the feed (Zerodha's quote ohlc / tick stream; stored candles in
  backtests), not from watching since 09:15: a session started at any time of day trades (Hamid).
- 2026-10-21 — IBKR snapshot twice a day, 5 minutes after the US open and close (09:35 and 16:05 New York time), and the server pulls it 10 minutes after each run (Hamid).
- 2026-10-21: remove the paper trades made on the 20 Oct holiday; reset the premium strangle session (its cycle was opened on that day's stale prices) rather than keep it.
- 2026-10-21: after the clean-up was undone, remove only the four 20 Oct cycles that opened and closed that day; keep positions entered on 20 Oct (premium strangle, double calendar, iron fly, put credit spread, short straddle-2) as they are.
- 2026-10-22 — New strategy idea (buy an ITM call or put one minute after the open in the direction of the move, 5-point
  SL followed closely; paper trade) built as `opening_move_itm`. Defaults where the idea left room: direction = NIFTY at
  09:16 vs its 09:15 open, at least 5 points; 100 points in the money; nearest expiry; stop 5 points on the option,
  trailing 5 behind its best price; one trade a day; no entry after 09:20; square-off 15:15; no target.
- 2026-10-22 — `opening_move_itm` entry changed (Hamid): buy only when NIFTY breaks the day's high (call) or low (put)
  within the first 30 minutes, instead of on the 09:16 direction. Day's high and low = the first minute's; the first
  break decides; no break by 09:45 = no trade.
- 2026-10-27 — 1-second recording stays at 30 strikes each side (PR #117, the 15-strike change, closed unmerged). Data page gets a 1-second bars viewer (day, instrument, candle length) to inspect what is stored.
- 2026-10-27 — Compare with paper shows the trades behind each day (session and both backtests), with times and prices, on demand.
- 2026-10-27 — Compare with paper: backtests start at the session's first price on a late-start day (not 09:15); the selected report is highlighted.
- 2026-10-27 — `straddle_trend_switch` enabled for LIVE orders (Hamid asked to remove the block): `live_ready = True`. Still required before a LIVE session starts: a daily loss limit and typing LIVE. Not yet reviewed by the order-execution reviewer; the dashboard's Live option is now selectable.
- 2026-10-28 — Compare with paper: a late-start day's backtests start at the session's first second (e.g. 09:55:41), not the start of that minute. Found on 28 Oct: the 1-second backtest sold 41 s before paper at other prices, so its stop-to-cost moved 11 minutes early and stopped the re-entered PE leg.
- 2026-10-28 — Deploy (Hamid): from any paper or shadow session to Shadow or LIVE with the same settings; the source keeps running; the max-lots override is per session; daily loss and orders per day stay hard limits.
- 2026-10-28 — Live / shadow: max lots can be overridden per session (Hamid): `live.allow_over_max_lots`, chosen when deploying. An order above max lots is then placed with a warning and an alert instead of stopping the session. Daily loss limit and orders per day stay hard limits. Off by default.
- 2026-10-28 — Data page 1-second recordings redesign approved (Hamid, prototype screen 2).
- 2026-10-27 — Compare with paper: finished comparisons can be deleted from the list (report and job record), by users who may run comparisons.
- 2026-10-28 — Compare with paper redesign approved (Hamid, prototype 'Algo Lab UI proposals'): leg-by-leg trades, fill-price view, sessions that stopped themselves shown with the reason.
- 2026-10-29 — Positions page (Hamid): in the Algo Lab dashboard; SL/target held at Zerodha as a GTT OCO; legs of live Algo Lab sessions read-only; manual close = LIMIT at bid/ask re-priced for 30 s. Claude's addition: the GTT is removed before the close and put back if it does not fill, so the two can never both fill.
- 2026-10-29 — Compare with paper (Hamid): only trades opened inside the period are compared; earlier ones are listed apart as information.
- 2026-10-30 — Live entry legs are placed together (Hamid: fix the 5 s second-leg delay). Order-execution code: no automated reviewer exists in the repo; Hamid to review the PR.
- 2026-11-02 — Stop on a flat session stops it at once, also outside market hours (Hamid). Multi-live next (Hamid): same contract may be held by several live sessions, per-session daily loss limits only, max live sessions a setting (default 3); multi-live before the 24 Oct live-safety fixes.
- 2026-11-17 — Gmail and Telegram alerts for live trading (Hamid): set from Profile, not by editing the server.
- 2026-11-18 — Algo Lab work moves to the public repo algotrading-public (free CI); the server updates from it. The private repo keeps the history and the IBKR snapshot branch (Hamid).
- 2026-11-18 — Sign-in opens the LIVE session only when a live session holds positions; with several, the Live page; otherwise the usual start page (Hamid).
- 2026-11-18 — A live leg squared off in Kite is synced, not a stop (Hamid, 11 Nov): automatic plus a Sync button (also on a stopped session); the other legs carry on; the closed leg is not re-entered in that trade.
- 2026-11-18 — A stopped LIVE session is resumed from its card (type LIVE) with its own account, so a strategy continues the next day (Hamid).
- 2026-11-18 — Exit prices of any closed leg of a LIVE session can be edited; P&L is recalculated and the leg shows "edited" with the first price kept (Hamid).
- 2026-11-18 — Shadow mode removed (Hamid): no Shadow when starting or deploying, the Live page shows LIVE only, existing shadow sessions are deleted; Paper stays.
- 2026-11-19 — Protective stops: read the option's upper circuit limit first and keep the stop below it (Hamid).
- 2026-11-20 — Re-check today button on a session whose strategy skipped the day; archive for old sessions, several at once (Hamid).
- 2026-11-23 — A LIVE session stopped and resumed on the same day with nothing open starts a fresh cycle (Hamid).
- 2026-11-23 — Positions page: trailing SL (by % or points from the best price) and SL / target as % of the average price (Hamid).
- 2026-11-26 — Live: SL / target of each leg in the activity log (with trailing moves), on the position rows and as red / green lines on the P&L chart (Hamid).
- 2026-11-28 — Run a backtest: 1-second bars offered and chosen by default, dates = the days that have them (Hamid).
