"""Algo Studio: one home per strategy (Strategies, presets, backtests, sessions), a second app on the server.

It runs beside the portfolio dashboard (service algolab-studio, 127.0.0.1:8020, behind /studio/ on the same address)
and reads what the engine already keeps: the strategy list, sessions and their trades (runtime/), the backtest results
(the dashboard's data/backtests) and sweeps (runtime/backtest-jobs). Sign-in is the dashboard's: each request's cookie
is checked with the dashboard (/api/studio-auth). Pages are plain files in studio/static (no build step).
"""
