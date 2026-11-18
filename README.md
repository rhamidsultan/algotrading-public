# Algo Lab

A platform for backtesting, paper-trading and live-trading option strategies on NIFTY. Paper and backtests never send orders; only a session you start in LIVE mode does (see "Real orders" below).

## Set up (once)

Setting up your own server as well? Follow [docs/SERVER_SETUP.md](docs/SERVER_SETUP.md).

**Mac:** open Terminal and run:

    cd ~/"AI - VS"/algo-lab
    bash setup_mac.sh
    bash start_dashboard.sh

The dashboard opens in your browser at http://localhost:8501. Leave the Terminal window open while
you use it. Next time only the last line is needed.

**Windows:** install Python 3.10+ from python.org (tick "Add Python to PATH"), double-click
`setup.bat` once, then `start_dashboard.bat`.

You can try everything straight away on the sample data and the simulated market.

## How the pieces fit

- **Your Mac** runs backtests (this dashboard). It never logs in to Zerodha.
- **Your server** (the one running the portfolio dashboard) runs paper trading around the clock and,
  after each close, stores the day's real NIFTY and option prices. Paper trading has its own page
  inside the portfolio dashboard and uses that dashboard's morning Zerodha login.
- **Get data from server** (Data tab here) copies those stored prices to the Mac for backtests.

Zerodha keeps one live login per API key, which is why only the server logs in.

## Backtesting on the Mac: the routine

This folder is a git repository. Every change to strategies or settings is a commit, so any result
can be traced to the exact code that produced it (`git log` lists them).

1. **Data.** Run `python run.py download` (or use **Download through the server** in the Data & broker
   tab). Your server dashboard fetches 5-minute NIFTY and India VIX bars from 2023 to today with its own
   Zerodha login and sends them here into `data/spot/NIFTY_5minute.csv`, so this Mac never logs in
   (Zerodha keeps one login per API key, and the Kite app sends logins back to the server anyway).
   Needs the `dashboard:` section in `secrets.yaml` and the Backtests page installed on the server.
   Add `--interval minute` for 1-minute bars. Re-run it now and then to top up. Back up the `data/`
   folder: it is not in git.
2. **Run.** `python run.py backtest --strategy blaze_butterfly --capital 90000 --note "why I ran it"`.
   Every run is saved under `runs/` with its parameters, settings, a fingerprint of the data file and
   the code version. `python run.py runs` lists them. The dashboard saves its runs there too.
3. **Choose settings without fooling yourself.** Hold back the most recent months:

       python run.py sweep --strategy short_straddle --grid stop_loss_pct=20:50:10 \
           --grid target_pct=40,60,80 --split 2026-04-01

   Pick settings by the `in_` columns only, then check the `out_` columns still hold up and that the
   neighbouring values are also good. Judge by worst week and drawdown, not win rate.
4. **Check against reality.** Each Friday compare the server's paper fills with a backtest of the
   same week. The gap is how far the backtest's option prices are off.

**Replay a backtest bar by bar.** The dashboard's **Replay** tab runs a strategy over a short window
(up to about a month of 1-minute bars) and lets you step through it: one bar or five forward or back,
jump to the next or previous trade, or play at 1-30 bars a second, pausing at each trade. Each step
shows NIFTY's candles so far with the strikes and the strategy's trigger levels, the open legs and
their prices, the cycle P&L against its target and stop, and the strategy's log at that moment. It is
the same engine as the Backtest tab; a warm-up before the window brings in a position already open.
For minute-by-minute replay, download 1-minute bars (`python run.py download --interval minute`)
and pick `NIFTY_minute`.

**Correct estimated option prices with NSE closing prices (free).** Zerodha keeps no history for
expired options, but NSE publishes every option's closing price each day (the F&O "bhavcopy").
`python run.py import-nse` (or **Import from NSE** in the Data & broker tab) downloads them from
2023 (about a second per trading day the first time; already imported days are skipped), keeps the
NIFTY rows in `data/eod/`, and builds a correction: how much richer each option traded than India
VIX implies, by distance from spot. Backtests then price each option with VIX x that ratio taken
from the **previous** trading day's closes (no look-ahead), and report the share of fills corrected
this way. It prints how far plain-VIX and corrected estimates are from the real next-day closes,
so you can see the effect. If NSE's site refuses automated downloads, save the daily F&O bhavcopy
files from nseindia.com/all-reports-derivatives into `data/eod/raw/` and run it again.

What the engine models, and where it is still approximate:

- **Intrabar checks.** Bars are walked open -> high/low -> close (`execution.intrabar: ohlc`), so a
  stop or a strike crossing inside a bar is caught. Set it to `close` to compare.
- **Lot sizes** follow NSE's changes by contract expiry (`market.lot_size_schedule`): 50, 25 from
  Apr 2024, 75 from early 2025, 65 from Jan 2026.
- **Charges** follow STT and exchange-fee changes by trade date (`cost_history`).
- **Option prices** are real only (Zerodha and TrueData): days without them are not tested (see
  "How option prices are obtained").
- Margin is not modelled: pass `--capital` as the margin you would block.

## Update the engine on the server

The server's daily price recording (3:45 pm IST) stores calls and puts 10 strikes either side of
the money and puts down to 30 strikes (1,500 points) below it, for the two nearest weekly expiries,
so the Blaze Butterfly's far put is recorded too (`collect:` in config.yaml). After changing code on
the Mac, update the server's engine without touching its data or the portfolio dashboard:

    bash deploy/make_server_update.sh

then open `deploy/paste-engine-update.txt`, copy all of it and paste it into the server's terminal.
It saves the old code first, restarts the paper engine, puts the old code back if anything fails,
and shows which option contracts the next recording will store.

## Add paper trading to the portfolio server (once)

On the Mac, from the folder holding the zip and your key:

    scp -i YOUR_KEY.pem algo-trading-platform.zip ubuntu@YOUR_SERVER_IP:~
    ssh -i YOUR_KEY.pem ubuntu@YOUR_SERVER_IP

On the server:

    unzip -o algo-trading-platform.zip
    cd algo-trading-platform
    sudo bash deploy/install_server.sh

It installs the engine in `/opt/algolab`, adds a **Paper trading** link to the portfolio dashboard,
and restarts that dashboard for a few seconds. It backs up the dashboard's files first and puts
them back by itself if anything goes wrong. To remove everything later:
`sudo bash deploy/uninstall_server.sh`. To update, copy a newer zip and run the installer again.

On the page, pick the strategy, its settings and the market feed, then Start:

- *Zerodha live prices*: real quotes in market hours; sells fill at the bid, buys at the ask.
  Before the morning login it shows "Waiting to start" and starts by itself once you log in.
- *Simulated market*: made-up prices running fast, for checking how a strategy behaves.

A started session keeps running with the page closed and resumes after a server restart.
No order is ever sent to Zerodha.

## Backtest on real prices (Mac)

In the Data tab enter the server (`ubuntu@YOUR_SERVER_IP`) and the path of your key file, then
**Get data from server**. The data set `NIFTY_5minute` then appears in the Backtest tab. The
server starts collecting on the day you install it, and option prices build up from then on.

(`deploy/install.sh` is an older alternative that hosts this whole dashboard on a server
instead; it is not needed with the setup above.)

## What each part does

**Backtest** replays stored NIFTY bars through a strategy and reports P&L after all charges,
drawdown, win rate, and every cycle and leg.

**Paper trading** runs the same strategy code on a live feed with simulated orders. It runs as a
separate process, saves its account after every update, and resumes where it left off, so a
positional trade survives closing the PC overnight. Two feeds:

- *Simulated market* - a made-up market running fast, for checking a strategy behaves as intended.
- *Zerodha live quotes* - real prices in market hours; sells fill at the real bid, buys at the ask.
  The PC must be on and the session running for exits to trigger.

## How option prices are obtained

**Backtests and replays use real option prices only** (`pricing.real_only: true`, the default).
Real prices come from three places, read together:

- `data/options/` - the server's nightly recordings, paper-trading quotes and TrueData history;
- `data/options_1min/` - Zerodha 1-minute candles (`deploy/get-minute-data.command`).

Zerodha and TrueData agree to the paisa at the same minute, so they can be mixed. Only days whose
at-the-money call and put of the nearest priced expiry have real prices are tested (other days are
skipped, warm-up days of a replay included), strategies are offered only the expiries and strikes
that traded that day, and when a contract has not traded in the last few minutes its last trade
earlier that day is used, else its next trade within 15 minutes (as an order would fill), else its
last trade the day before (a position carried overnight). Each result shows "Option prices: real
only", the days tested and how often those gaps were filled. In the rare case a contract did not
trade at all around then, the model is used and the result warns how many times (the strategy log
lists them). Days covered are cached in `data/options/.coverage.json`.

To use the model for days without real prices (the platform's original behaviour), set
`pricing.real_only: false` in `config.yaml` or pass `--allow-model` to `run.py backtest`. The model
is Black-Scholes on India VIX: there is no volatility skew, and weekly at-the-money volatility is
not exactly VIX (`pricing.iv_scale` in `config.yaml` adjusts it). Real prices accumulate in two ways: live paper trading records
every quote it sees, and "Download option candles" saves the contracts listed that day. Bought
historical option data can be dropped into the same folder layout:

    data/options/NIFTY/<expiry YYYY-MM-DD>/<strike><CE|PE>.csv     columns: time,last,bid,ask

## Expired option prices from TrueData (trial)

TrueData's history API keeps 1-minute candles for expired NIFTY options (about the last 6 months).
`run.py truedata` downloads them into `data/options/`, so backtests use real option prices
instead of modelled ones. It also saves NIFTY spot + India VIX as the data set `TD_NIFTY_1minute`.

1. Put the trial login in `secrets.yaml`:

       truedata:
         user: "your_trial_user"
         password: "your_password"

2. `pip install lz4` (once), then check the login and the data format:

       python run.py truedata test

3. Download everything for the last 6 months:

       python run.py truedata all

   Contracts nearest the money are fetched first. Stop it any time (Ctrl+C) and run the same
   command again to carry on. `python run.py truedata status` shows progress;
   `--retry-errors` retries failed contracts. Defaults: strikes within 10% of NIFTY (`--band`),
   the last 35 days before each expiry (`--days`), 4 parallel requests (`--workers`), paced at 240 a minute to stay inside TrueData's limits.

   Bank Nifty the same way: `python run.py truedata all --underlying BANKNIFTY` (monthly
   contracts, strikes in steps of 100; data set `TD_BANKNIFTY_1minute`).
   Older periods, nearest expiry only (each contract only during its own expiry week):
   `python run.py truedata all --from 2024-10-15 --to 2026-04-14 --nearest` (add
   `--underlying BANKNIFTY` for Bank Nifty). TrueData had 1-minute option data back to about
   2 years when checked in Oct 2026. Each step shows a percentage and the time left.
   `python run.py truedata depth` checks how many months back TrueData has expired options
   (1-minute and daily) and saves the answer in `data/truedata/history_depth.csv`.

4. Backtest on it: `python run.py backtest --dataset TD_NIFTY_1minute --strategy short_straddle`.
   The result's "Fills on real option prices" shows how much used TrueData prices.

Full copies (open/high/low/close, volume, open interest) and every day's F&O bhavcopy are kept in
`data/truedata/` - back this folder up before the trial ends. Settings can also go in `config.yaml`
under `truedata:` (`band_pct`, `days_before_expiry`, `chunk_days`, `workers`, `min_gap_seconds`).

## The short straddle

Sells the ATM call and put of the nearest weekly expiry and holds them.

| Setting | Meaning | Default |
|---|---|---|
| Lots per leg | 1 lot = 65 units | 1 |
| Entry time | First bar at or after this time | 09:30 |
| Min / max days to expiry | Which expiry is eligible at entry | 1 / 7 |
| Stop-loss | Exit both legs when the cycle loses this % of the premium collected | 40% |
| Profit target | Exit when the cycle gains this % of the premium collected | 60% |
| Roll trigger | Close and re-sell at the new ATM after this many index points | 300 |
| Maximum rolls | Per cycle | 2 |
| Expiry-day exit | 15:30 lets the options settle | 15:15 |

One cycle is traded per expiry; after a stop-out the next entry is for the following expiry.
Stops are checked once per bar (or per live poll), so a fast move can exit beyond the stop level.

## The Blaze Butterfly

`blaze_butterfly` follows the same rules as the server's paper engine (`kite-dashboard/paper_engine.py`):
a NIFTY put broken-wing butterfly (buy 1 lot K1 = R-200, sell 2 lots K2 = R-400, buy 1 lot K3 = R-800)
on next week's expiry, entered Monday 15:10-15:12 (Wednesday before 29 Aug 2025), strikes shifted down
in 100s until the net premium is inside -0.7% / +0.6% of margin, exits at ±1% of margin, on a Tuesday
(Thursday) fall to K1, or 14:30 on the week's last trading day. Percentages here are in percent
(0.6 = 0.6%), where `paper_config.json` uses fractions (0.006).

`margin_per_unit` (Rs 87,300 for a 65 lot) is scaled to past lot sizes by default
(`margin_scaling = lot`); choose `fixed` to use the figure as is.

## The premium-matched short strangle

`premium_strangle` sells an out-of-the-money call and put whose prices are closest to Rs 20
(`target_premium`) on the nearest weekly expiry 1-7 days away, at 09:20. Whenever one leg's last
price is more than twice the other's (`ratio`), it buys back the cheaper leg and sells a new one on
the same side, at the strike whose price is closest to the dearer leg's, moving toward the market
but never past the other leg's strike. When that roll would put both legs on the same strike, it
closes everything; otherwise it closes at 15:15 on expiry day. No stop-loss. The next cycle starts
the next trading day. Live, only strikes Zerodha actually lists are considered.

## Defined-risk strategies (lower drawdown)

Five strategies aimed at smaller drawdowns than the naked straddle. Each works everywhere the others
do: Mac backtests, Replay and sweeps, and on the server's Paper and Live pages (Shadow first).

| Strategy | What it does | Main settings |
|---|---|---|
| `iron_fly` | ATM straddle with bought wings (short offset > 0 = iron condor); worst loss known | target 50% / stop 100% of credit, wings auto (credit x 1.25) |
| `double_calendar` | Sells this week's call + put, buys the same strikes next week (term-structure decay) | target 30% / stop 35% of debit, exits the day before the near expiry |
| `put_credit_spread` | Bull put spread 1.5% below NIFTY, only above the 20-day average (put skew + drift) | width 200, target 60% / stop 150% of credit |
| `intraday_strangle` | Intraday straddle/strangle: stop per leg, other leg to cost, one re-entry, day limits | leg stop 30%, flat by 15:15 |
| `event_iron_fly` | Iron fly the day before RBI policy / Budget / election results, closed on the event day | entry 14:45, exit 11:30 next day |

**Regime filters** (all except the event strategy): enter only when India VIX is inside a band, has
not jumped on the day, NIFTY is not far from yesterday's close, VIX is above realised volatility,
and no scheduled event falls before expiry. Set any of them to 0 (or `events: ignore`) to switch it
off. A new paper session has no daily history yet, so the checks that need it pass until it has
seen enough days (the log says so). Event dates are in `algolab/events.py`; add more in
`events.yaml` beside run.py, e.g. `- {date: 2027-02-01, name: Union Budget}`.

Orders: wings are bought before short legs are sold, and short legs are bought back before wings
are sold, which keeps live margin low and never leaves a short leg without its hedge.

All five are enabled for live orders. Run each in **Shadow** for at least a week first, keep
`Max lots per order` at the lots you mean to trade, and set a daily loss limit.

First results on 5-minute NIFTY bars, Jan 2023 - Oct 2026, 1 lot, Rs 3 lakh capital, option prices
estimated from India VIX (no real option prices, no skew, one volatility for all expiries), so
indicative only:

| Strategy | Net P&L | Max drawdown | Worst week | Cycles |
|---|---|---|---|---|
| short_straddle (for comparison) | Rs 2,88,542 | -20.0% | Rs -75,065 | 198 |
| iron_fly | Rs 1,53,047 | -11.4% | Rs -23,821 | 156 |
| double_calendar | Rs 34,126 | -6.6% | Rs -7,870 | 178 |
| put_credit_spread | Rs -376 | -6.7% | Rs -5,547 | 123 |
| intraday_strangle | Rs -93,549 | -39.1% | Rs -25,082 | 771 |
| event_iron_fly | Rs -16,878 | -5.8% | Rs -8,607 | 28 |

The put spread's skew edge and the calendar's term-structure edge are exactly what estimated prices
leave out; judge those two (and the event fly's volatility crush) on real prices from the server
(`Get data from server`, `import-nse`) before deciding.

## Paper trading several strategies at once

Each strategy runs in its own paper session with its own account (`runtime/sessions/<strategy>/`).
On the server, start and stop them on the portfolio dashboard's **Paper trading** page; each running
strategy has its own card, plus a combined total. Sessions on live prices share one feed: each
update fetches the index, VIX and every option any session uses in a single Zerodha request.
A session started on a different feed replaces that strategy's account; Reset clears it.

## The dashboard's menu

Every page of the server dashboard now has the same menu at the top: **Summary · Portfolio · Stocks · US ·
Paper · Live · Backtests · Data** and your profile picture, which stays put while scrolling and scrolls sideways on a phone. Paper
has two tabs: the Algo Lab sessions and the **Blaze Butterfly (original engine)** page; the Paper
page also shows the original Blaze engine as a card (P&L, open legs, next entry) linking to it. Live
has the shadow/live sessions and the order test.

**Paper and Live pages.** The top card is a summary of every session: running or stopped, open
legs, open P&L and total P&L after charges (the original Blaze engine is a row too, before
charges). Tap a row to see that session's details below: open positions, P&L chart, closed cycles,
activity, and its Stop / Resume / Reset / Delete buttons. **Start a session** (`/paper/new`,
`/live/new`) is a separate page: pick a strategy from the cards (risk type shown on each), adjust
its settings and Start; you land on the new session. "Copy to a new session" on a session opens
that page with its settings filled in. The menu is added by `algolab_panel.py` (installed
with the engine update), so the dashboard's own page files are not edited.

## Summary page

**Summary** (`/summary`, first in the menu) shows what changes during the day, on one screen: today's
real-money P&L (India portfolio + US stocks in rupees + live sessions), then a tile each for the
Portfolio (day P&L and %, value, total P&L), US stocks (day P&L in $ and Rs, net value), Paper trading
(today, open and total P&L of every paper session plus the original Blaze engine), Live trading (live
and shadow) and the market (NIFTY, India VIX). Below are today's trades across all sessions, each with
its reason, and every session with its today / open / total P&L. Tap a tile or row to open that page.
It refreshes every 15 s. Only the owner's login sees it. To open it after signing in, choose it as the
start page in Profile.

**Positions and activity on the Paper and Live pages.** A session's Positions table lists the open legs
and, below them, legs closed today or in the cycle still open, with quantity 0, their entry and exit
prices and P&L (before charges). **Activity** lists only the orders placed, newest first, each with
why: the strategy's reason (Entry, Roll, Stop-loss, Target...) and the numbers at that moment - for a
new leg the strike's distance from NIFTY and days to expiry, for a closing leg the cycle P&L against
the target and stop (or the roll levels), and NIFTY and VIX. The engine's raw messages are under
**Engine log**. Orders placed before this update show their reason only.

## Market data page

**Data** in the dashboard menu shows what the server has stored for backtests: the NIFTY and India
VIX bars (range, days, last close, days with missing bars), NSE closing-price days, the option
prices recorded per expiry (contracts, strikes, number of prices, from/to), and the daily
recording's log with the next run. Tap an expiry for its option chain (last recorded price of every
strike, ATM highlighted), and a price for that contract's history chart (last, plus bid/ask where a
live paper session recorded them) with a CSV download. The chart at the top plots NIFTY with India
VIX over 1 day to all history. The recording writes its result to `data/collect_log.txt`; the engine update fills it once with the earlier runs found in the server's system journal.

## Profile and theme

The round picture at the right end of the menu opens **Profile** (`/profile`): your name, email,
a note and a photo (cut to a square in the browser before upload), the theme (same as the device,
light or dark), an accent colour used across every page, and which page opens after signing in.
Sign out is there too. The profile and photo are kept on the server in the dashboard's data folder
(`/opt/trader/data/profile.json`, `profile_photo.*`), so engine updates do not touch them.

## The dashboard as a phone app

Open **https://<your-ip-with-dashes>.sslip.io** (the plain-HTTP ports 5010 and 8080 are closed to the
internet; Zerodha's login comes back to the HTTPS address).

- **Install:** iPhone: Safari, Share, **Add to Home Screen**. Android: Chrome menu, **Install app**.
  It opens full screen with its own icon, and shows an offline page without a connection.
- **Face ID / fingerprint sign-in (passkeys):** Profile, **Set up on this device**. The sign-in page
  then offers "Sign in with Face ID / fingerprint"; the password keeps working. Passkeys are stored in
  `/opt/trader/data/passkeys.json` (public keys only).
- **Notifications:** Profile, **Turn on for this device** (on iPhone from the home-screen app, iOS
  16.4 or later), then choose: trading (sessions stopping by themselves, live fills and rejections,
  protective stops), the end-of-day P&L at 3:35 pm, login reminders (Zerodha at 8:50/9:05 am if not
  logged in, IBKR when US holdings stop updating) and portfolio price alerts. **Send a test** checks it.
  The engine queues messages in `runtime/push_outbox/`; the dashboard sends them (Web Push, its own
  VAPID key in `/opt/trader/data/vapid.json`).

## Live prices: Zerodha's price stream

On the server, sessions on Zerodha live prices share one connection to Zerodha's live price stream
(WebSocket, full mode: last price plus best bid and ask). The strategies are stepped every second
(`paper.stream_step_seconds`), and only an option the stream has not delivered yet is fetched with a
quote request. The stream also keeps the strikes near the money of the two nearest expiries
subscribed (`collect:` settings) and records their bid/ask once a minute for backtests.

If the stream goes quiet for 5 s (`paper.stream_stale_seconds`) the feed polls quotes every 2 s as
before, and switches back when the stream returns; it reconnects by itself and with each morning's
login. Once a minute it compares the stream with a quote request and writes the difference to
`runtime/stream_check.csv`. The Paper and Live pages show where prices come from ("prices: live
stream (N instruments)" or "polling"). Live orders: fills, rejections and protective-stop fills
arrive on the same stream, so they are acted on at once (Zerodha is still asked directly every few
seconds as a check). `paper.price_source: poll` in config.yaml turns the stream off.

## Real orders: shadow and live mode

A session on Zerodha live prices can run in one of three modes (chosen on the dashboard's Paper
trading page when starting a strategy):

- **Paper** - simulated orders (the default).
- **Shadow** - everything a live session does (limits, the exact orders with prices) but nothing is
  sent; fills are simulated and every order is logged as "SHADOW (not sent)". Run this for a week.
- **Live** - real orders from the server. Only strategies marked ready for it (so far the
  premium-matched strangle) can be started live, and you must type LIVE to confirm.

Live orders are LIMIT orders at the best bid/ask, product NRML, re-priced every 5 s for up to 30 s.
After every option sold, a protective SL buy order is left at Zerodha (default 3x the sold price),
so a server outage or a missed login cannot leave a short option unprotected; it is cancelled before
the leg is bought back. Before trading, the session's positions are compared with Zerodha's; any
difference stops it without sending anything. Limits: lots per order, orders a day, and a daily
loss limit (required) that squares off and stops the session. A rejection, a partial fill or a
protective stop filling squares off, stops the session and sends an alert (email/Telegram, using
the portfolio dashboard's alert settings). **Stop & square off** on the page closes everything.

Requirements: Zerodha accepts API orders only from the static IP whitelisted in your Kite Connect
developer profile (Profile > IP Whitelist) - the server's static IP. The daily Zerodha login on the
dashboard is still needed every morning. Margin must be available in your account (a short strangle
blocks roughly Rs 1-1.5 lakh per lot).

## Settings

`config.yaml` holds lot size, capital, slippage, every charge rate, pricing inputs and the live
polling interval. Charges are set to Zerodha's option rates as of October 2026 (brokerage Rs 20 per
order, STT 0.15% on sell premium, exchange, SEBI, stamp duty, GST). Check them against
zerodha.com/charges from time to time. Margin is not modelled: a 1-lot NIFTY short straddle needs
very roughly Rs 2-2.5 lakh, so set capital accordingly.

## Adding a strategy

Create a file in `algolab/strategies/`. It appears in the dashboard and command line automatically,
with an input for each parameter. `intraday_straddle.py` is a 50-line example.

    from ..strategy import Param, Strategy

    class MyStrategy(Strategy):
        name = "my_strategy"
        title = "My strategy"
        PARAMS = {"lots": Param(1, "Lots per leg", min=1)}

        def on_bar(self, ctx):
            if not ctx.positions and ctx.now.hour == 10:
                expiry = ctx.pick_expiry(min_dte=1)
                ctx.sell(ctx.option(expiry, ctx.atm_strike() + 200, "CE"), self.p.lots, "Entry", "my-group")

`ctx` gives `now`, `spot`, `vix`, `atm_strike()`, `expiries()`, `pick_expiry()`, `option()`,
`price()`, `quote()`, `buy()`, `sell()`, `close()`, `close_all()`, `positions`, `net_realized`,
`unrealized`, `equity` and `log()`. Anything that must survive a paper-trading restart goes in
`get_state()` / `set_state()`.

## Command line

    python run.py strategies
    python run.py backtest --strategy short_straddle --set stop_loss_pct=30 --from 2026-01-01
    python run.py backtest --strategy blaze_butterfly --capital 90000 --note "first real-data run"
    python run.py runs
    python run.py sweep --strategy short_straddle --grid stop_loss_pct=20,30,40 --split 2026-04-01
    python run.py paper --feed sim
    python run.py login
    python run.py download                   (NIFTY + VIX from 2023-01-01; --from to change)
    python run.py download-options
    python run.py paper --feed kite
    python run.py truedata test | all | status | depth   (TrueData expired options; --underlying BANKNIFTY)
    python -m pytest tests

Past or simulated results do not predict future results. Short straddles carry unlimited risk.
