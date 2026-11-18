"""Find best settings: the settings most likely to keep working, not the ones with the biggest past profit.

  1. The last `lock_days` (about 2-3 months) are locked away: never used to choose, only to report once at the end.
  2. The rest is cut into `folds` back-to-back periods (walk-forward). Each combination is backtested on every period.
  3. The search: a random sample of the grid first, then the untried neighbours of the best ones (coarse, then fine).
  4. Fragile combinations are dropped: too few cycles, a loss overall, a period with a heavy loss, a drawdown beyond
     the limit set.
  5. The rest are scored on steadiness: return / worst drawdown x the share of periods that made money, averaged with
     their neighbours (one step away on one setting), so a lone lucky peak scores low.
  6. The top picks are run again with Conservative fills over the whole choosing period (dropped if they lose), for the
     reshuffle (Monte Carlo) drawdown and return on margin, and once on the locked-away months.

Result: up to three picks, each with its reasons, plus whether choosing by the past worked for this strategy at all
(the walk-forward total).
"""
from __future__ import annotations

import itertools
import random
from datetime import timedelta

import pandas as pd

from .sweep import folds as make_folds, grid_size, neighbour_score, walk_forward

PICKS = 3


def _combo_key(row: dict, names: list) -> tuple:
    return tuple(str(row[n]) for n in names)


def sample_grid(grid: dict, n: int, seed: int = 7) -> list[dict]:
    """Up to `n` distinct combinations: all of them when the grid is small, else a random spread in which every
    value of every setting appears at least once (when n allows)."""
    names = list(grid)
    total = grid_size(grid)
    if total <= n:
        return [dict(zip(names, v)) for v in itertools.product(*grid.values())]
    rng = random.Random(seed)
    seen, out = set(), []
    longest = max(len(v) for v in grid.values())
    for i in range(min(longest, n)):               # every value at least once
        c = {k: v[i] if i < len(v) else rng.choice(v) for k, v in grid.items()}
        key = _combo_key(c, names)
        if key not in seen:
            seen.add(key)
            out.append(c)
    while len(out) < n:
        c = {k: rng.choice(v) for k, v in grid.items()}
        key = _combo_key(c, names)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def neighbours(combo: dict, grid: dict) -> list[dict]:
    out = []
    for k, vals in grid.items():
        i = vals.index(str(combo[k])) if str(combo[k]) in vals else -1
        for j in (i - 1, i + 1):
            if i >= 0 and 0 <= j < len(vals):
                out.append({**combo, k: vals[j]})
    return out


def score_rows(df: pd.DataFrame, grid: dict, labels: list, min_cycles: int, max_dd: float | None) -> pd.DataFrame:
    """Adds per-combination totals, the filters' verdict ("why_out") and the steadiness score."""
    df = df.copy()
    nets = df[[f"{p}_net" for p in labels]].apply(pd.to_numeric, errors="coerce")
    dds = df[[f"{p}_max_dd" for p in labels]].apply(pd.to_numeric, errors="coerce")
    cyc = df[[f"{p}_cycles" for p in labels]].apply(pd.to_numeric, errors="coerce")
    df["total_net"] = nets.sum(axis=1).round(0)
    df["periods_up"] = (nets > 0).sum(axis=1)
    df["worst_period"] = nets.min(axis=1).round(0)
    df["worst_dd"] = dds.min(axis=1).round(0)                     # drawdowns are negative
    df["cycles"] = cyc.sum(axis=1)
    avg_period = df["total_net"] / len(labels)

    def why(r):
        if r["cycles"] < min_cycles:
            return f"only {int(r['cycles'])} cycles (needs {min_cycles})"
        if r["total_net"] <= 0:
            return "lost money overall"
        if max_dd and r["worst_dd"] < -abs(max_dd):
            return f"drawdown {abs(r['worst_dd']):,.0f} beyond the limit"
        if r["worst_period"] < 0 and abs(r["worst_period"]) > 2 * max(avg_period[r.name], 1):
            return "one period lost more than two average periods made"
        return ""
    df["why_out"] = df.apply(why, axis=1)
    raw = df["total_net"] / df["worst_dd"].abs().clip(lower=1) * (df["periods_up"] / len(labels))
    df["raw_score"] = raw.where(df["why_out"] == "", other=-1.0).round(3)
    df["score"] = neighbour_score(df, grid, "raw_score")
    df.loc[df["why_out"] != "", "score"] = -1.0
    return df


def working_ranges(df: pd.DataFrame, grid: dict, pick: pd.Series, labels: list) -> dict:
    """For each setting: the values (others as in the pick) that also passed and made money in most periods."""
    out = {}
    need = max(1, len(labels) - 1)
    for k, vals in grid.items():
        if len(vals) < 2:
            continue
        others = [n for n in grid if n != k]
        same = df
        for n in others:
            same = same[same[n].astype(str) == str(pick[n])]
        good = [str(v) for v in vals
                if ((same[k].astype(str) == str(v)) & (same["why_out"] == "") & (same["periods_up"] >= need)).any()]
        if good:
            out[k] = [good[0], good[-1]] if len(good) > 1 else [good[0], good[0]]
    return out


def run(job: dict, cfg: dict, limit: int, run_sweep, run_one_backtest, data_range, progress=None) -> dict:
    """run_sweep(combos, periods) -> DataFrame of rows; run_one_backtest(params, start, end, conservative) -> metrics;
    data_range() -> (first day, last day) of the index data in the job's range."""
    grid = {k: [str(v) for v in vals] for k, vals in (job.get("grid") or {}).items() if vals}
    names = list(grid)
    n_folds = int(job.get("folds") or 4)
    lock_days = int(job.get("lock_days") or 75)
    min_cycles = int(job.get("min_cycles") or 30)
    max_dd = float(job["max_dd"]) if job.get("max_dd") not in (None, "", 0, "0") else None
    first, last = data_range()
    lock_from = last - timedelta(days=lock_days - 1)
    if (lock_from - first).days < n_folds * 14:
        raise ValueError(f"Too short: after locking away {lock_days} days, each of the {n_folds} periods needs 2 weeks")
    periods = make_folds(first, lock_from - timedelta(days=1), n_folds)
    labels = list(periods)

    stage1 = sample_grid(grid, max(1, int(limit * 0.7)))
    df = run_sweep(stage1, periods)
    tried = {_combo_key(r, names) for _, r in df.iterrows()}
    scored = score_rows(df, grid, labels, min_cycles, max_dd)
    extra = []
    for _, r in scored.sort_values("raw_score", ascending=False).head(5).iterrows():
        for nb in neighbours({n: str(r[n]) for n in names}, grid):
            key = _combo_key(nb, names)
            if key not in tried and len(extra) < limit - len(stage1):
                tried.add(key)
                extra.append(nb)
    if extra:
        df = pd.concat([df, run_sweep(extra, periods)], ignore_index=True)
    scored = score_rows(df, grid, labels, min_cycles, max_dd).sort_values("score", ascending=False).reset_index(drop=True)

    base = {k: v for k, v in (job.get("params") or {}).items() if k not in grid}
    picks, checked = [], 0
    for _, r in scored[scored["why_out"] == ""].head(PICKS * 3).iterrows():
        if len(picks) >= PICKS:
            break
        params = {**base, **{n: r[n] for n in names}}
        if progress:
            progress(f"checking pick {len(picks) + 1}")
        checked += 1
        cons = run_one_backtest(params, first, lock_from - timedelta(days=1), True)
        cons_net = cons.get("Net P&L")
        if not isinstance(cons_net, (int, float)) or cons_net <= 0:
            continue
        lock = run_one_backtest(params, lock_from, last, False)
        reasons = [f"made money in {int(r['periods_up'])} of {len(labels)} periods",
                   f"still made Rs {cons_net:,.0f} with Conservative fills"]
        ranges = working_ranges(scored, grid, r, labels)
        wide = [k for k, (a, b) in ranges.items() if a != b]
        if wide:
            reasons.append("nearby values also work (" + ", ".join(f"{k} {ranges[k][0]}–{ranges[k][1]}" for k in wide) + ")")
        lnet = lock.get("Net P&L")
        if isinstance(lnet, (int, float)):
            reasons.append(f"locked-away months: Rs {lnet:,.0f}" + (" (held up)" if lnet > 0 else " (did not hold up)"))
        picks.append({
            "params": {n: str(r[n]) for n in names}, "score": _f(r["score"]),
            "periods_up": int(r["periods_up"]), "periods": len(labels),
            "period_nets": [_f(r[f"{p}_net"]) for p in labels], "total_net": _f(r["total_net"]),
            "worst_dd": _f(r["worst_dd"]), "cycles": int(r["cycles"]),
            "conservative_net": _f(cons_net), "mc_dd": _f(cons.get("Monte Carlo: max drawdown, 1 in 20 worse than")),
            "ret_margin": _f(cons.get("Return on peak margin %")), "peak_margin": _f(cons.get("Peak margin (estimated)")),
            "lock_net": _f(lnet), "lock_dd": _f(lock.get("Max drawdown")), "lock_cycles": _f(lock.get("Cycles")),
            "ranges": ranges, "reasons": reasons})

    walk = walk_forward(scored, grid, periods, metric="ret_dd")
    passed = int((scored["why_out"] == "").sum())
    held = [p for p in picks if (p["lock_net"] or 0) > 0]
    if not picks:
        verdict = (f"No settings passed all the checks ({passed} of {len(scored)} passed the first filters). "
                   "Widen the ranges, use a longer period, or rethink the strategy.")
    elif walk["steps"] and walk["net"] <= 0:
        verdict = ("Careful: choosing each period's best settings and trading them in the next period lost money "
                   f"(Rs {walk['net']:,.0f}). For this strategy the past has not been a good guide to settings.")
    elif not held:
        verdict = "The picks did well when chosen but none made money in the locked-away months: treat them with doubt."
    else:
        verdict = (f"{len(held)} of {len(picks)} picks also made money in the locked-away months. Start with pick 1 "
                   "in paper trading and check the Saturday paper-vs-backtest report before risking money.")
    reasons_out = scored["why_out"].value_counts().drop("", errors="ignore").head(4).to_dict()
    return {"picks": picks, "tested": len(scored), "passed": passed, "checked": checked, "walk": walk,
            "fold_dates": {k: [str(a), str(b)] for k, (a, b) in periods.items()},
            "lock": [str(lock_from), str(last)], "verdict": verdict, "dropped": reasons_out,
            "scored": scored, "labels": labels}


def _f(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 2)
