# Zerodha portfolio dashboard

Live holdings, P&L, history and alerts for your Zerodha account. Read-only: it never places orders.

## One-time setup

1. Install Python 3.10 or newer.
2. In this folder, run: `pip install -r requirements.txt`
3. Copy `config.example.json` to `config.json`.
4. Open `config.json` and paste your Kite `api_key` and `api_secret`.
5. In the Kite developer console, check the app's redirect URL is exactly `http://127.0.0.1:5000/callback`.

To preview with fake data before connecting your account: `python app.py --demo`

## Signing in to the dashboard

The dashboard asks for a username and password, set by `dashboard_user` and `dashboard_password` in `config.json`. Change them there and restart the app. Tick "Keep me signed in" to stay signed in on that device for 30 days. After 5 wrong attempts, that device is locked out for 15 minutes.

## Day P&L range and charts

- **Day P&L range:** today's highest and lowest day P&L, with the time each happened, and how far the current figure is from each (in rupees and as a percentage of portfolio value).
- **Today:** day P&L through the day; the low is marked in red and the high in green.
- **Today by stock:** each holding's day P&L right now, best to worst.
- **Day P&L by day:** one bar per trading day, for the whole portfolio or any one stock (use the drop-down).

Total P&L high/low is no longer shown, but it is still recorded. Every trading day the app logs one row per holding (quantity, previous close, closing price, value, day P&L, total P&L). **Download history (CSV)** under the chart exports that log for analysis.

## Stock history page

Open **Stock history** from the top of the dashboard, or tap any stock name in the holdings table (address `/stock`). Pick a stock from the drop-down: current holdings are listed first, then stocks you have sold. For the chosen stock it shows money invested, current value, profit or loss and units held, with graphs of invested amount against value, profit or loss over time, and money added each month.

- Zerodha's API only gives the current day's trades, so the history comes from the Console tradebook. Use **Import tradebook** at the bottom of the page once; uploading the same file again is harmless.
- From then on the app records each day's trades by itself during sync hours.
- Invested amount uses first-in-first-out cost, the same way Zerodha works out your average price.
- The value and profit graphs each show three lines: at the day's high, at the close, and at the day's low. They use Zerodha's daily prices when your plan allows the historical data API. Otherwise they use the prices of your own trades and the highs and lows this app saw during the day, which is rougher.
- For a stock you hold, the four figures at the top come from Zerodha's holdings. The graphs are rebuilt from trades plus the corporate actions in `corporate_actions.json` (splits, bonuses, mergers, demergers, rights and IPO allotments). Each stock's page lists the actions applied to it and how each was checked.
- That file is written by hand and is replaced on every update. Put your own additions in `corporate_actions.local.json` beside it, in the same format, and restart the app.
- The page warns when trades and actions do not add up to your holding, which means an action or an off-market transfer is missing.

## Sync hours

The app only contacts Zerodha on weekdays between 8:45 am and 4:00 pm IST (30 minutes either side of market hours). Outside that window it shows the last figures and makes no requests, apart from one fetch after a restart or a new login so the page is not blank. Weekends are fully paused. The page in your browser also stops refreshing outside the window; it reloads once when you reopen it and starts again by itself when syncing resumes.

## Pausing sync by hand

**Pause sync** at the top of the dashboard stops all contact with Zerodha until you press **Resume sync**; the choice survives restarts. Before pausing, the dashboard explains what you would miss given the market's state. While paused during market hours the figures are stale, alerts are off, and if sync is still paused at 3:35 pm the day's closing figures and trades are not recorded.

## Port 5000 on a Mac

macOS uses port 5000 for AirPlay Receiver, so the app may report "Port 5000 is already in use". Fix it one of two ways:

- Turn off AirPlay Receiver in System Settings > General > AirDrop & Handoff, or
- Use another port: set `"port": 5010` in `config.json` and change the redirect URL in the Kite developer console to `http://127.0.0.1:5010/callback`.

## Every trading day

1. Run `python app.py` and leave the window open.
2. Open http://127.0.0.1:5000 on the laptop and click **Log in with Zerodha**. The login lasts until about 6 am the next day.
3. On your phone (same Wi-Fi), open the "phone" address printed in the window.

Prices refresh every 60 seconds during market hours (9:15 am to 3:30 pm IST). A snapshot is saved every minute; these build the Today and Daily history charts.

## Running on the cloud server

`setup.sh` prepares the server and `deploy.sh` installs this dashboard on it (as a service that restarts by itself). On the server:

- The Kite keys are read from `/opt/trader/.env`, so `config.json` there leaves them blank.
- `"token_file": "data/access_token.json"` makes the dashboard share the day's Zerodha login with the morning login page on port 8080. Logging in on either one is enough.
- Zerodha's redirect cannot reach the server, so after the Zerodha login copy the address from the browser and paste it into the box on the dashboard (or on the login page).
- Logs: `journalctl -u trader-app -f`

## Algo page: paper trading (Blaze Butterfly)

On the server a second service, `trader-paper`, runs `paper_engine.py`. It trades the **Blaze Butterfly (NIFTY zero-adjustment)** strategy on paper only: it reads live prices from Zerodha and records simulated fills, and never places orders. Open **Algo (paper)** at the top of the dashboard (address `/algo`) to see the open position, P&L against target and stop, every weekly run, and a log of what the engine did and why.

- **Strategy:** a NIFTY put broken-wing butterfly on next week's expiry: buy 1 lot at K1 = R−200, sell 2 lots at K2 = R−400, buy 1 lot at K3 = R−800 (R is spot rounded to 100). Strikes shift down in 100-point steps (up to 500) until the net premium is between −0.7% and +0.6% of `margin_per_unit`; otherwise the week is skipped.
- **Entry:** Monday 15:10–15:12 IST, into the expiry 7–9 days away. Not on the last trading day of a week.
- **Exits (shorts first):** loss of 1% or profit of 1% of `margin_per_unit` × `multiplier`; on Tuesday if NIFTY falls to K1 or below; 14:30 on the last trading day of the week (Friday, or earlier if Friday is a holiday); the long legs alone if the short is not placed by 15:14; and at once if the filled premium falls outside the band.
- **Fills:** buys at the best ask, sells at the best bid. P&L is gross (before brokerage and taxes).
- **Needs:** the day's Zerodha login (the same one the dashboard uses) and the paid Kite Connect plan, which provides quotes. Before the first Monday, run `sudo -u trader /opt/trader/venv/bin/python /opt/trader/paper_engine.py --check` on the server during market hours: it shows the expiry, spot, strike candidates with bid/ask and what the engine would do, without trading.
- **Settings:** `/opt/trader/paper_config.json` (the engine reloads it within seconds; no restart needed). Check `margin_per_unit` against your broker's actual margin for the 1/2/1 basket: the premium band, target and stop are all percentages of it. `permit_mode` 1 with `permit_date` set to a Monday's date allows only that week (to skip budget or election weeks, leave it at 1 and don't set the date). The NSE holiday list for 2026 is in `holidays`; add 2027's when NSE publishes it.
- **Buttons:** *Pause new entries* stops new weeks; an open position still follows its exits. *Square off now* closes the paper position at current prices (queued until the market opens if pressed after hours).
- **Alerts:** entries, exits and skipped weeks are emailed (and sent to Telegram) when those are on in `config.json`. Set `"notify": false` in `paper_config.json` to turn them off.
- **Logs:** `journalctl -u trader-paper -f`. Paper history lives in `data/paper.db`.

## Alerts

Edit the `alerts` section of `config.json`, then restart the app.

| Setting | Meaning |
|---|---|
| `holding_day_move_pct` | Alert when any holding moves this many percent in a day, up or down |
| `portfolio_day_move_pct` | Alert when the whole portfolio moves this many percent in a day |
| `gold_allocation_band_pct` | Alert when gold falls outside this `[low, high]` share of the portfolio |
| `price_levels` | Per-stock targets, e.g. `{"symbol": "INFY", "above": 1800, "below": 1450}` |

Each alert fires once per day. Set a value to `null` to switch that rule off. Alerts always appear on the dashboard.

**Email:** in Google Account > Security, turn on 2-Step Verification, create an App Password, then set `email.enabled` to `true` and fill in `from`, `app_password` and `to`. Use the **Send test alert** button to check.

**Telegram (optional):** create a bot with @BotFather, put its token and your chat id under `telegram`, and set `enabled` to `true`.

## Gold vs equity

Any symbol containing `GOLD` counts as gold. Add other gold ETFs to `gold_symbols`, and list any wrongly tagged stock in `non_gold_symbols`.

## Files

- `config.json`, `token.json`: your secrets. Keep them private.
- `portfolio.db`: your history. Back it up if you want to keep the charts.
- `data/paper.db`: the paper engine's trades and log. `paper_config.json`: its settings.
