"""Performance statistics."""
from __future__ import annotations

import math

import pandas as pd


def cycles_table(trades: pd.DataFrame) -> pd.DataFrame:
    """One row per strategy cycle (all legs and rolls that share a group id)."""
    if trades.empty:
        return pd.DataFrame(columns=["group", "entry_time", "exit_time", "legs", "gross_pnl", "costs", "net_pnl", "exit_reason"])
    g = trades.groupby("group", sort=False)
    out = g.agg(entry_time=("entry_time", "min"), exit_time=("exit_time", "max"), legs=("contract", "count"),
                gross_pnl=("gross_pnl", "sum"), costs=("costs", "sum"), net_pnl=("net_pnl", "sum"),
                exit_reason=("reason", "last")).reset_index()
    return out.sort_values("entry_time").reset_index(drop=True)


def add_excursions(cycles: pd.DataFrame, equity: pd.DataFrame) -> pd.DataFrame:
    """For each cycle, how far the account went against it (worst, a negative number) and in its favour (best)
    while it was open, measured on the equity curve from the moment it was entered. Cycles that overlap share the
    curve, so these are approximate when several run at once."""
    if cycles.empty or equity.empty:
        return cycles.assign(worst_move=pd.Series(dtype=float), best_move=pd.Series(dtype=float))
    eq = equity["equity"]
    worst, best = [], []
    for r in cycles.itertuples():
        a, b = pd.Timestamp(r.entry_time), pd.Timestamp(r.exit_time)
        before = eq[eq.index <= a]
        base = float(before.iloc[-1]) if len(before) else float(eq.iloc[0])
        w = eq[(eq.index > a) & (eq.index <= b)] - base
        worst.append(round(min(float(w.min()), 0.0), 0) if len(w) else 0.0)
        best.append(round(max(float(w.max()), 0.0), 0) if len(w) else 0.0)
    return cycles.assign(worst_move=worst, best_move=best)


def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame, cycles: pd.DataFrame,
                    capital: float, total_costs: float) -> dict:
    m: dict = {}
    if equity.empty:
        return {"Net P&L": 0.0}
    eq = equity["equity"]
    net = float(eq.iloc[-1] - capital)
    peak = eq.cummax()
    dd = eq - peak
    daily = eq.groupby(eq.index.date).last()
    rets = daily.diff().fillna(daily.iloc[0] - capital) / capital
    days = max((eq.index[-1] - eq.index[0]).days, 1)
    m["Net P&L"] = round(net, 0)
    m["Return on capital %"] = round(net / capital * 100, 2)
    m["Annualised return %"] = round(net / capital * 365 / days * 100, 2)
    m["Max drawdown"] = round(float(dd.min()), 0)
    m["Max drawdown %"] = round(float((dd / peak).min()) * 100, 2)
    m["Return / drawdown"] = round(net / abs(float(dd.min())), 2) if dd.min() < 0 else 0.0
    weekly = eq.groupby(eq.index.to_period("W-FRI")).last()
    week_chg = weekly.diff().fillna(weekly.iloc[0] - capital)
    m["Worst week"] = round(float(week_chg.min()), 0)
    underwater, longest, since = dd < 0, 0, None
    for t, flag in zip(eq.index, underwater):
        if flag and since is None:
            since = t
        elif not flag and since is not None:
            longest, since = max(longest, (t - since).days), None
    if since is not None:
        longest = max(longest, (eq.index[-1] - since).days)
    m["Longest drawdown (days)"] = int(longest)
    sd = rets.std()
    m["Sharpe (daily)"] = round(float(rets.mean() / sd * math.sqrt(252)), 2) if sd and sd > 0 else 0.0
    down = rets[rets < 0]
    dsd = math.sqrt(float((down ** 2).sum() / len(rets))) if len(rets) else 0.0
    m["Sortino (daily)"] = round(float(rets.mean() / dsd * math.sqrt(252)), 2) if dsd > 0 else 0.0
    mdd_pct = abs(float((dd / peak).min()) * 100)
    m["Calmar (annual return / max drawdown %)"] = round(net / capital * 365 / days * 100 / mdd_pct, 2) if mdd_pct > 0 else 0.0
    day_chg = daily.diff().fillna(daily.iloc[0] - capital)
    m["Best day"] = round(float(day_chg.max()), 0)
    m["Worst day"] = round(float(day_chg.min()), 0)
    m["Charges paid"] = round(total_costs, 0)
    m["Trading days"] = int(len(daily))
    n = len(cycles)
    m["Cycles"] = n
    if n:
        wins = cycles[cycles.net_pnl > 0].net_pnl
        losses = cycles[cycles.net_pnl <= 0].net_pnl
        m["Win rate %"] = round(len(wins) / n * 100, 1)
        m["Avg win"] = round(float(wins.mean()), 0) if len(wins) else 0.0
        m["Avg loss"] = round(float(losses.mean()), 0) if len(losses) else 0.0
        m["Profit factor"] = round(float(wins.sum() / abs(losses.sum())), 2) if losses.sum() != 0 else float("inf")
        m["Best cycle"] = round(float(cycles.net_pnl.max()), 0)
        m["Worst cycle"] = round(float(cycles.net_pnl.min()), 0)
        m["Expectancy per cycle"] = round(float(cycles.net_pnl.mean()), 0)
        held = set()
        for r in cycles.itertuples():
            for d in pd.date_range(pd.Timestamp(r.entry_time).normalize(), pd.Timestamp(r.exit_time).normalize()):
                held.add(d.date())
        m["Days in a trade %"] = round(len(held & set(daily.index)) / max(len(daily), 1) * 100, 1)
        if "worst_move" in cycles:
            m["Avg worst move in a cycle"] = round(float(cycles.worst_move.mean()), 0)
            m["Avg best move in a cycle"] = round(float(cycles.best_move.mean()), 0)
    return m
