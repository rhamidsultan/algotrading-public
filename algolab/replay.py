"""Bar-by-bar replay of a backtest.

`record()` runs the normal backtest engine over a short window and keeps a snapshot after every bar:
the time, NIFTY, VIX, account value, each open leg with its price and P&L, the strategy's own status
(cycle P&L against target and stop, strikes...) and the fills and log lines of that bar. The Replay
tab then steps through those snapshots one bar at a time, forwards or back, so what you watch is
exactly what the backtest did.

A warm-up period before the window is run first (not shown), so a position opened before the window
starts is already in place when the replay begins.

Like backtests, replays use real option prices only (pricing.real_only): days without them are skipped.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

import pandas as pd

from .config import to_date
from .engine import make_broker, real_only, real_price_bars, run
from .feeds import HistoricalFeed, OptionStore, load_dataset

MAX_BARS = 8000  # about a month of 1-minute bars


@dataclass
class Replay:
    strategy: str
    params: dict
    dataset: str
    capital: float
    bars: pd.DataFrame                     # open/high/low/close/vix by bar-close time
    frames: list = field(default_factory=list)  # one dict per bar
    fills: pd.DataFrame | None = None      # every fill in the window, with the frame index it happened in
    start_equity: float = 0.0
    price_stats: dict = field(default_factory=dict)   # real-prices-only: how gaps were filled

    def __len__(self) -> int:
        return len(self.frames)

    def next_event(self, i: int, kinds=("fill",)) -> int | None:
        for j in range(i + 1, len(self.frames)):
            f = self.frames[j]
            if ("fill" in kinds and f["fills"]) or ("log" in kinds and f["logs"]):
                return j
        return None

    def prev_event(self, i: int) -> int | None:
        for j in range(i - 1, -1, -1):
            if self.frames[j]["fills"]:
                return j
        return None


def record(cfg: dict, strategy, dataset: str, start, end, warmup_days: int = 10) -> Replay:
    start_d, end_d = to_date(start), to_date(end)
    df = load_dataset(dataset, start_d - timedelta(days=warmup_days), end_d)
    store = OptionStore(cfg["market"]["underlying"])
    real = real_only(cfg)
    if real:   # real option prices only: days without them are left out (warm-up included)
        df = real_price_bars(df, store, cfg)
    show_from = pd.Timestamp(start_d)
    n_shown = int((df.index >= show_from).sum())
    if n_shown == 0:
        raise ValueError("No days with real option prices in that date range." if real else "No bars in that date range.")
    if n_shown > MAX_BARS:
        raise ValueError(f"That is {n_shown:,} bars; replay up to {MAX_BARS:,} at a time (about a month of "
                         "1-minute bars). Pick a shorter range.")
    feed = HistoricalFeed(df, cfg, store, real_only=real)
    broker = make_broker(cfg)
    logs: list = []
    frames: list = []
    seen = {"logs": 0, "fills": 0}
    fill_rows: list = []

    def after_bar(ctx, point):
        now = point[0]
        new_logs = logs[seen["logs"]:]
        new_fills = broker.fills[seen["fills"]:]
        seen["logs"], seen["fills"] = len(logs), len(broker.fills)
        if pd.Timestamp(now) < show_from:
            return
        idx = len(frames)
        for f in new_fills:
            fill_rows.append({"frame": idx, "time": f.time, "contract": f.contract, "side": f.side, "qty": f.qty,
                              "price": f.price, "charges": f.costs, "reason": f.reason, "priced": f.source})
        positions = []
        for c, p in broker.positions.items():
            ltp = ctx.price(c)
            positions.append({"Leg": c.label, "Strike": c.strike, "Type": c.right, "Qty": p.qty,
                              "Entry": round(p.avg_price, 2), "Now": round(ltp, 2),
                              "P&L": round((ltp - p.avg_price) * p.qty, 0)})
        try:
            status = strategy.status(ctx) if hasattr(strategy, "status") else None
        except Exception:  # a status display must never break the replay
            status = None
        frames.append({"time": now, "spot": point[2], "vix": ctx.vix, "equity": point[1],
                       "booked": broker.net_realized, "open_pnl": ctx.unrealized, "positions": positions,
                       "status": status, "logs": [l[18:] if len(l) > 18 else l for l in new_logs],
                       "fills": len(new_fills)})

    run(feed, strategy, broker, logs, after_bar, risk=cfg.get("risk") or None)
    bars = df[df.index >= show_from]
    if "open" not in bars:
        bars = bars.assign(open=bars.close, high=bars.close, low=bars.close)
    start_equity = frames[0]["equity"] if frames else broker.capital
    return Replay(strategy.name, strategy.params, dataset, broker.capital, bars, frames,
                  pd.DataFrame(fill_rows), start_equity, dict(feed.price_stats) if real else {})
