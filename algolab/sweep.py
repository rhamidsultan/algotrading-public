"""Parameter sweeps with an in-sample / out-of-sample split.

Every combination is backtested on the in-sample period (where you choose settings) and, separately,
on the out-of-sample period (which you only look at afterwards). Settings whose good in-sample result
does not carry over, or whose neighbours do badly, are probably fitted to noise.
Runs in parallel on all CPU cores.
"""
from __future__ import annotations

import itertools
import json
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import load_config, to_date
from .runs import RUNS_DIR, code_version, data_fingerprint

KEYS = {"Net P&L": "net", "Max drawdown": "max_dd", "Cycles": "cycles", "Win rate %": "win_pct",
        "Worst week": "worst_week", "Return / drawdown": "ret_dd"}


def parse_grid(items: list[str]) -> dict[str, list[str]]:
    """["stop_loss_pct=20,30,40", "target_pct=50:80:10"] -> {name: [values]} (a:b:step is a range)."""
    grid = {}
    for item in items:
        name, values = item.split("=", 1)
        if ":" in values and "," not in values:
            a, b, step = (float(x) for x in values.split(":"))
            vals, v = [], a
            while v <= b + 1e-9:
                vals.append(f"{v:g}")
                v += step
        else:
            vals = [v.strip() for v in values.split(",") if v.strip()]
        grid[name.strip()] = vals
    return grid


def value_range(default, start, stop, step) -> list[str]:
    """Values from start to stop (inclusive) in steps, typed like the parameter's default. Times (HH:MM)
    step in minutes."""
    if isinstance(default, str) and ":" in default:
        def mins(t):
            h, m = str(t).strip().split(":")[:2]
            return int(h) * 60 + int(m)
        a, b, st = mins(start), mins(stop), int(float(step))
        if st <= 0 or b < a:
            raise ValueError("A time range needs From <= To and a step of at least 1 minute")
        return [f"{v // 60:02d}:{v % 60:02d}" for v in range(a, b + 1, st)]
    a, b, st = float(start), float(stop), float(step)
    if st <= 0 or b < a:
        raise ValueError("A range needs From <= To and a step above 0")
    n = int(round((b - a) / st + 1e-9)) + 1
    vals = [a + i * st for i in range(n)]
    if isinstance(default, int) and not isinstance(default, bool):
        return [str(int(round(v))) for v in vals]
    return [f"{round(v, 6):g}" for v in vals]


def grid_size(grid: dict) -> int:
    n = 1
    for v in grid.values():
        n *= len(v)
    return n


def neighbour_score(df: pd.DataFrame, grid: dict, col: str) -> pd.Series:
    """For each combination: the average of `col` over it and its neighbours (one step away on one setting).
    A good value whose neighbours are also good is more likely to hold up than a lone peak."""
    names = [n for n in grid if len(grid[n]) > 1]
    if col not in df or not names:
        return df.get(col, pd.Series(dtype=float))
    pos = {n: {str(v): i for i, v in enumerate(grid[n])} for n in names}
    keys = [tuple(pos[n].get(str(r[n]), -99) for n in names) for _, r in df.iterrows()]
    val = dict(zip(keys, pd.to_numeric(df[col], errors="coerce")))
    out = []
    for k in keys:
        around = [val[k]]
        for d in range(len(names)):
            for step in (-1, 1):
                nb = k[:d] + (k[d] + step,) + k[d + 1:]
                if nb in val:
                    around.append(val[nb])
        around = [v for v in around if pd.notna(v)]
        out.append(round(sum(around) / len(around), 2) if around else None)
    return pd.Series(out, index=df.index)


def heat_table(df: pd.DataFrame, x: str, y: str, col: str) -> pd.DataFrame:
    """Average of `col` for each pair of values of two settings (other settings averaged over)."""
    d = df[[x, y]].astype(str).assign(v=pd.to_numeric(df[col], errors="coerce"))
    t = d.pivot_table(index=y, columns=x, values="v", aggfunc="mean")

    def order(vals):
        try:
            return sorted(vals, key=lambda v: float(str(v).replace(":", ".")))
        except ValueError:
            return sorted(vals)
    return t.reindex(index=order(t.index), columns=order(t.columns))


def past_sweeps() -> list[Path]:
    """Saved sweeps, newest first."""
    if not RUNS_DIR.is_dir():
        return []
    return sorted((p for p in RUNS_DIR.iterdir() if p.is_dir() and ((p / "sweep.csv").exists() or (p / "sweep.partial.csv").exists())),
                  reverse=True)


def _one(job) -> dict:
    strategy_name, params, dataset, periods, cfg = job
    from .engine import backtest
    from .strategy import load_strategies
    row = dict(params)
    for label, (start, end) in periods.items():
        try:
            from .risk import split, strategy_params
            cls = load_strategies()[strategy_name]
            own, rk = split(params, cfg.get("risk"))                 # "risk.*" values can be varied like any setting
            c = {**cfg, "risk": rk} if rk else cfg
            m = backtest(c, cls(**strategy_params(cls, own, rk)), dataset, start, end).metrics
            for k, short in KEYS.items():
                row[f"{label}_{short}"] = m.get(k)
        except ValueError as e:  # e.g. no data in range
            row[f"{label}_error"] = str(e)
    return row


def memory_gb() -> float | None:
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1e9
    except (ValueError, OSError, AttributeError):
        return None


def safe_workers() -> int:
    """Parallel backtests that fit this computer: one per core (leaving one free) and about 2.5 GB of memory
    each, since every process loads the index and option prices on its own."""
    cores = max(1, (os.cpu_count() or 2) - 1)
    mem = memory_gb()
    return max(1, min(cores, int(mem // 2.5) if mem else cores))


def sweep(strategy_name: str, grid: dict, dataset: str, start=None, end=None, split=None,
          base_params: dict | None = None, cfg: dict | None = None, workers: int | None = None,
          progress=None, out: Path | None = None, periods: dict | None = None,
          combos: list | None = None) -> tuple[pd.DataFrame, Path]:
    """Every combination is saved to sweep.partial.csv as soon as it finishes, so a sweep that is stopped
    (or killed) keeps what it did; the ranked sweep.csv is written at the end. `periods` ({label: (from, to)})
    replaces the split, e.g. the folds of a walk-forward."""
    cfg = cfg or load_config()
    base_params = base_params or {}
    if periods:
        split = None
    elif split:
        split_d = to_date(split)
        periods = {"in": (start, split_d - timedelta(days=1)), "out": (split_d, end)}
    else:
        periods = {"all": (start, end)}
    names = list(grid)
    if combos is not None:          # chosen combinations (Find best settings), not the whole grid
        combos = [dict(base_params, **c) for c in combos]
    else:
        combos = [dict(base_params, **dict(zip(names, vals))) for vals in itertools.product(*grid.values())]
    jobs = [(strategy_name, c, dataset, periods, cfg) for c in combos]
    out = out or RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_sweep_{strategy_name}"
    out.mkdir(parents=True, exist_ok=True)
    (out / "sweep.json").write_text(json.dumps({
        "strategy": strategy_name, "grid": grid, "base_params": base_params, "dataset": dataset, "total": len(jobs),
        "periods": {k: [str(a) if a else None, str(b) if b else None] for k, (a, b) in periods.items()},
        "data": data_fingerprint(dataset), "code": code_version(), "config": cfg}, indent=2, default=str))
    partial = out / "sweep.partial.csv"
    partial.unlink(missing_ok=True)
    rows = []

    def keep(row):
        rows.append(row)
        pd.DataFrame([row]).to_csv(partial, mode="a", header=not partial.exists(), index=False)
        if progress:
            progress(len(rows), len(jobs))

    workers = workers or safe_workers()
    if workers == 1 or len(jobs) == 1:
        for j in jobs:
            keep(_one(j))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(_one, j) for j in jobs]):
                keep(fut.result())
    df = pd.DataFrame(rows)
    rank = f"{next(iter(periods))}_ret_dd"
    if rank in df:
        df = df.sort_values(rank, ascending=False, na_position="last").reset_index(drop=True)
    df.to_csv(out / "sweep.csv", index=False)
    partial.unlink(missing_ok=True)
    return df, out


def folds(start, end, n: int) -> dict:
    """`n` back-to-back periods of about equal length from `start` to `end`: {"f1": (from, to), ...}."""
    a, b = to_date(start), to_date(end)
    if n < 2 or (b - a).days < n * 7:
        raise ValueError(f"Walk-forward needs at least {n} weeks between From and To")
    span = (b - a).days + 1
    cuts = [a + timedelta(days=round(span * i / n)) for i in range(n + 1)]
    return {f"f{i + 1}": (cuts[i], cuts[i + 1] - timedelta(days=1)) for i in range(n)}


def walk_forward(df: pd.DataFrame, grid: dict, periods: dict, metric: str = "ret_dd") -> dict:
    """For each fold but the last: the settings that did best on it, and how they then did on the next fold
    (which they never saw). Added together, the next-fold results are what choosing by the past would have made."""
    labels = list(periods)
    steps, total = [], 0.0
    for i in range(len(labels) - 1):
        tr, te = labels[i], labels[i + 1]
        col = f"{tr}_{metric}"
        if col not in df or df[col].dropna().empty:
            continue
        best = df.loc[pd.to_numeric(df[col], errors="coerce").idxmax()]
        net = best.get(f"{te}_net")
        net = None if net is None or net != net else float(net)
        total += net or 0.0
        steps.append({"train": [str(d) for d in periods[tr]], "test": [str(d) for d in periods[te]],
                      "params": {k: str(best[k]) for k in grid}, "train_metric": _num(best.get(col)),
                      "test_net": net, "test_max_dd": _num(best.get(f"{te}_max_dd")),
                      "test_cycles": _num(best.get(f"{te}_cycles")),
                      "same_as_before": bool(steps) and steps[-1]["params"] == {k: str(best[k]) for k in grid}})
    return {"steps": steps, "net": round(total, 0), "metric": metric}


def luck_note(df: pd.DataFrame, total: int, periods: list) -> str:
    """A warning about choosing the best of many: with many combinations, the top one is partly luck."""
    if total < 10:
        return ""
    msg = (f"{total} combinations were tried, so the best {'in-sample ' if 'in' in periods else ''}result is partly "
           "luck: expect it to do worse from here.")
    if "out" in periods and "out_net" in df and "in_ret_dd" in df:
        out = pd.to_numeric(df["out_net"], errors="coerce").dropna()
        if len(out):
            msg += f" {int((out > 0).sum())} of {len(out)} made money out-of-sample."
            top = df.sort_values("in_ret_dd", ascending=False).head(max(1, len(df) // 10))
            top_out = pd.to_numeric(top["out_net"], errors="coerce").dropna()
            if len(top_out):
                msg += f" The top tenth in-sample made {top_out.mean():,.0f} on average out-of-sample, against " \
                       f"{out.mean():,.0f} for all."
    msg += " Prefer settings whose neighbours and later periods are also good."
    return msg


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else round(f, 2)
