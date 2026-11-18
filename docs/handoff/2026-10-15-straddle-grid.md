# Hand-off: Algo Lab straddle-grid work (14–15 Oct 2026)

From the chat that built real-prices-only backtests and the straddle trend-switch strategy.
Its note as written, then a check against the code in this repo.

## Built / changed

- Backtests and replays now use real option prices only (default: `pricing.real_only: true` in
  `config.yaml`; `--allow-model` on `run.py backtest` switches it off). Zerodha recordings, Zerodha
  1-minute candles and TrueData are read together. Days without real at-the-money prices are skipped.
  Strategies see only traded expiries and strikes. Short gaps are filled from same-day trades, the
  next trade within 15 min, or the previous day. Coverage is cached in `data/options/.coverage.json`.
  Bars are trimmed to 09:15–15:30.
  Files: `algolab/feeds.py`, `engine.py`, `replay.py`, `dashboard.py`, `run.py`, `config.yaml`,
  `README.md`, `tests/test_platform.py` (new test), `tests/test_defined_risk.py`.
- New strategy: `algolab/strategies/straddle_trend_switch.py`. Its paper session
  "Trend switch (backtest Oct 2026)" runs on the server.
- `deploy/`: `get-minute-data.command` + `fetch_minute_data.py` (Zerodha 1-min via the server),
  `get-truedata.command`, `stop-ibkr.command`, `start-ibkr.command`.
- `straddle_grid_backtest.py` (repo root): standalone research script.

## Unfinished / known broken (as reported)

- Nothing was committed to git.
- `run.py truedata` calls `truedata.use_underlying` and `history_depth` (Bank Nifty work), which
  were missing from `truedata.py`, so the command errored and 3 tests in `tests/test_truedata.py` failed.
- `premium_strangle` still had 3 modelled price look-ups in 6 months (none were fills); flagged in results.

## Remember

- Over 6 months of real prices the trend switch lost Rs 2.08 lakh (profit factor 0.66). Stop its
  paper session or keep it only as a live check.
- IBKR is stopped on the server (Docker and `ibkr-sync` disabled). "IBKR needs a login" reminders
  continue until `start-ibkr.command` is run.
- Back up `data/truedata` before the TrueData trial ends (22 Oct 2026).
- Zerodha and TrueData timestamps match exactly, so mixing the two is safe.
- Nightly server recording is still 5-minute. Switching `collect_daily` (`algolab/kite.py`) to
  1-minute would capture every expiry at 1-minute.

## Checked against this repo (15 Oct 2026)

- All of this work is now committed (pull request #1).
- The `truedata` breakage was a stale copy on the Mac, since fixed: `use_underlying` and
  `history_depth` are in `algolab/truedata.py` and the full test suite passes (78 passed, 8 skipped).
