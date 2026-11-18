# Algo Trading — guide for every Claude session

Hamid's NIFTY / Bank Nifty options platform: backtests on the Mac, paper (and guarded live) trading
on a Lightsail server, all shown in one dashboard that also tracks his India and US portfolios.
This repo is the single source of truth. Read this file and `docs/STATUS.md` before changing anything.

## House rules

1. **Work on a branch, open a pull request, never push to `main`.** Hamid approves and merges.
   One change per pull request; say in plain words what it changes and how it was tested.
2. **Never commit secrets or data.** No `secrets.yaml`, `config.json`, `token.json`, `.secret_key`,
   `.env`, `*.pem`, databases, `data/`, `runs/`, `runtime/`. The test workflow fails if any appear.
3. **Tests must pass:** `python -m pytest -q` (about 4 minutes). Add a test for new behaviour.
4. **Update `docs/STATUS.md`** in the same pull request when what is live, data coverage or work in
   progress changes. Add a dated line to `docs/DECISIONS.md` for choices Hamid makes.
5. **Code updates may go to the server in market hours; data copies wait for the close** (weekdays
   9:00–15:40 IST). An update restarts the paper engine for a few seconds (sessions resume) and the
   dashboard (rolled back if a page fails). Deploys are Hamid's, by double-click; never ask him to
   paste code into the server.
6. **Never write into Hamid's folders directly.** The Mac and the server only take merged code from
   GitHub (`git pull` / deploy). Writing files straight into the Mac folder caused overwrites
   between chats before this repo existed.
7. Hamid reads replies on an iPhone during the day and uses the Mac in the evening. Keep
   instructions short, give exact commands to paste, and say what a command will do first.

## What is where

| Path | What it is | Runs on |
|---|---|---|
| `algolab/` | Engine: strategies (`algolab/strategies/`), backtest (`engine.py`), feeds and real option prices (`feeds.py`), paper/live trading (`paper.py`, `live.py`, `stream.py`), Zerodha (`kite.py`), TrueData history (`truedata.py`), margins, metrics | Mac and server |
| `run.py` | Command line: `backtest`, `sweep`, `paper`, `collect`, `sync-data`, `import-nse`, `truedata …`, `publish` | Mac and server |
| `dashboard.py` | Streamlit backtest dashboard (`start_dashboard.sh`) | Mac |
| `tests/` | pytest suite, run by GitHub Actions on every pull request | CI |
| `deploy/` | Server install/update scripts (`install_server.sh`, `update_engine.sh`), Mac double-click launchers (`*.command`) | both |
| `deploy/portfolio_page/` | Algo Lab pages added into the portfolio dashboard (Paper, Live, Summary, Users, phone app) | server |
| `server/portfolio/` | Portfolio dashboard (Flask app, Zerodha holdings, US/IBKR page, alerts) — runs as `/opt/trader` | server |
| `config.yaml` | Market settings: lot sizes by date, expiry weekdays, holidays, charges, collection | both |

## Machines

- **Mac** (Hamid's MacBook Air): today the code is in `~/AI - VS/algo-lab` with a `.venv`.
  Planned: `~/Trading/algotrading` (this repo), `~/Trading/data`, `~/Trading/runs`; secrets in
  `~/.algo-trading`. Commands: `.venv/bin/python run.py …`.
  The launchers in `deploy/` read the server address and SSH key from `~/.algo-trading/server.env`
  (`deploy/server_login.sh`); never write an address or repo name into the code.
- **Server** (AWS Lightsail, Ubuntu, 2 GB): engine in `/opt/algolab` (code, `venv`, `data`, `runtime`,
  `config.yaml`, `secrets.yaml`); services `algolab-paper` (paper/live sessions),
  `algolab-collect.timer` (records prices after the close, 15:45 IST), `algolab-margins`,
  `algolab-backtest.path` (backtests asked for on the dashboard, `algolab/backtest_queue.py`).
  Portfolio dashboard in `/opt/trader` (service `trader-app`). Planned: `/opt/algo-trading/{app,data,runtime,secrets}`.
- **Mac helper** (`algolab/lab_agent.py`, login item from `deploy/install-mac-agent.command`): runs the backtests and sweeps
  the dashboard sends to "Mac" (Algo Lab › Run on), signing in with `secrets.yaml` dashboard settings.
- **A new server** (anyone's): `docs/SERVER_SETUP.md`, `sudo bash deploy/new_server.sh`; IBKR optional.
- **Repos:** this public repo (`algotrading-public`, from 18 Nov 2026) is where work happens: free CI. The private
  `algotrading` keeps the history before that, and its `ibkr-snapshot` branch (Hamid's US account data, written by a
  scheduled task): never copy that branch or the private history here. Never commit the server's real address:
  placeholders only (`<your-ip-with-dashes>.sslip.io`, `203-0-113-10.sslip.io` in tests).
- **Deploy:** Hamid taps **Update now** on the dashboard (Profile › Server version) after merging: the server
  pulls `main` from this repo over https (no key; `deploy/update_source.sh` records it in `/opt/algo-trading/update_repo`,
  `deploy/self_update.sh` run by the `algolab-selfupdate` path unit) and runs `deploy/update_engine.sh`, which swaps
  the code in with a backup and rolls back if it does not start. The IBKR snapshot is still read from the private
  repo with the read-only deploy key (`/opt/algo-trading/repo`). The Mac route still works: `bash deploy/make_server_update.sh` then `deploy/update-server.command`.

## Market data

- Backtests use **real option prices only** by default (`--allow-model` turns the Black-Scholes model back on).
- Option prices: `data/options/<UNDERLYING>/<expiry>/<strike><CE|PE>.csv` with `time,last,bid,ask`
  (`time` = when the price was known: bar start + bar length).
- Spot data sets: `data/spot/<name>.csv` (`datetime,open,high,low,close,vix`): `NIFTY_5minute`
  (Zerodha), `TD_NIFTY_1minute`, `TD_BANKNIFTY_1minute` (TrueData).
- TrueData raw copies (OHLC, volume, OI), bhavcopies and `progress.db`: `data/truedata/`. TrueData has
  1-minute expired-option bars back to about 2 years (checked Oct 2026). Trial ends 22 Oct 2026. Real-time feed:
  push.truedata.in port 8086 for trial accounts, 8084 paid (8082 is Market Replay).
- Market facts used in code: NIFTY weekly expiry Tuesday since Sep 2025 (Thursday before); NIFTY lot
  65 from Jan 2026; Bank Nifty weekly contracts ended Nov 2024 (monthly only since).
  Sensex weekly expiry: Friday until 2024, Tuesday from Jan 2025, Thursday from Sep 2025. From 3 Aug
  2026 the cash market closes by auction (15:15–15:35) and F&O trades until 15:40: the index is frozen
  after 15:15.
- Ticks with best bid/ask (last 5 trading days before the trial ended, near the money):
  `data/truedata/ticks/<UNDERLYING>/<day>/<symbol>.csv.gz` (columns as TrueData sends them: price, volume, OI, bid/ask).
- Stocks: `data/truedata/equity/<SYMBOL>_1min.csv.gz`, futures `data/truedata/futures/<SYMBOL>-I_1min.csv.gz`
  (`&` written as `and`, e.g. `MandM`); the list used is in `data/truedata/equity/symbols.txt`.

## Running things

```
python -m pytest -q                                    # all tests
python run.py backtest --strategy short_straddle --dataset TD_NIFTY_1minute --from 2026-01-01
python run.py truedata status                          # TrueData download progress
python run.py strategies                               # strategies and their parameters
python -m algolab.datacheck                            # review stored data: missing bars, bad prices
```

**Data review:** before trusting new data for backtests, use the `data-reviewer` agent
(`.claude/agents/data-reviewer.md`). It runs `algolab/datacheck.py`, separates real market events
(Muhurat, Budget days, gaps) from data faults, and recommends the fix.
