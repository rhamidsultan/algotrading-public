"""Backtests run on the server, asked for from the dashboard (Paper › Run a backtest).

The dashboard writes a request into runtime/backtest-queue/<id>.json and a status into runtime/backtest-jobs/<id>.json.
A systemd path unit (algolab-backtest.path, installed by deploy/update_engine.sh) sees the queue is not empty and
runs `python run.py backtest-queue` at low priority (nice 19, idle disk, capped CPU and memory), so paper and live
sessions are not slowed. Each request is taken off the queue first (so a crash never repeats it), run exactly like
`run.py backtest` on the Mac, saved under runs/, and put on the dashboard's Backtests page (the same files a Mac
`run.py publish` uploads).

Request: {"id", "strategy", "params": {...}, "dataset", "from": "YYYY-MM-DD", "to": "YYYY-MM-DD", "note", "user"}
Status:  the request plus "status" (queued / running / done / failed), "queued", "started", "finished", "run"
         (the saved run's name, which the Backtests page opens), "metrics" (headline figures) and "message".
"""
from __future__ import annotations

import fcntl
import io
import json
import os
import re
import shutil
import zipfile
from datetime import date, datetime
from pathlib import Path

from .config import RUNTIME, load_config

QUEUE = RUNTIME / "backtest-queue"
JOBS = RUNTIME / "backtest-jobs"
ID_OK = re.compile(r"^[0-9]{8}-[0-9]{6}-[a-z0-9]{4}$")
HEADLINE = ("Net P&L", "Return on capital %", "Max drawdown", "Cycles", "Win rate %", "Profit factor",
            "Fills on real option prices")


def store_dir() -> Path:
    """The dashboard's Backtests page folder (same user as the engine on the server)."""
    return Path(os.environ.get("BACKTESTS_STORE", "/opt/trader/data/backtests"))


def _write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _status(job: dict, **changes) -> dict:
    job.update(changes)
    JOBS.mkdir(parents=True, exist_ok=True)
    _write(JOBS / f"{job['id']}.json", job)
    return job


def publish_local(run_dir: Path, store: Path | None = None) -> str:
    """Put a saved run on this server's Backtests page, the files `run.py publish` would upload."""
    from .publish import pack
    store = store or store_dir()
    store.mkdir(parents=True, exist_ok=True)
    name = run_dir.name
    tmp = store / f".incoming-{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    with zipfile.ZipFile(io.BytesIO(pack(run_dir))) as z:
        z.extractall(tmp)
    final = store / name
    if final.exists():
        shutil.rmtree(final)
    tmp.rename(final)
    (run_dir / ".published").write_text(datetime.now().isoformat(timespec="seconds"))
    return name


def _day(text) -> date | None:
    return date.fromisoformat(str(text)) if text else None


SWEEP_MAX = 60          # combinations per sweep on the server (the Mac takes more: SWEEP_MAX_MAC)
SWEEP_MAX_MAC = 400
SWEEP_KEEP = 1000       # result rows kept on the job for the page (table, charts, heatmap)
METRICS = ("net", "ret_dd", "max_dd", "win_pct", "cycles", "worst_week")


def job_config(job: dict, cfg: dict | None = None) -> dict:
    """The settings for a job: "Fills: conservative" doubles the spread and fills a minute after the decision;
    "min_volume" leaves out strikes with fewer contracts traded so far that day."""
    import copy
    cfg = copy.deepcopy(cfg or load_config())
    if job.get("fills") == "conservative":
        ex = cfg.setdefault("execution", {})
        ex["cost_stress"] = max(2.0, float(ex.get("cost_stress", 1.0) or 1.0))
        ex["fill_delay_seconds"] = max(60, int(ex.get("fill_delay_seconds", 0) or 0))
    if job.get("risk"):                      # Orders & stops (algolab/risk.py): backtests and sweeps read cfg["risk"]
        from .risk import settings
        cfg["risk"] = settings(job["risk"])
    if job.get("min_volume"):
        liq = cfg.setdefault("execution", {}).setdefault("liquidity", {})
        liq["min_volume_today"] = max(0, int(float(job["min_volume"])))
    return cfg


def run_sweep(job: dict, cfg: dict | None = None, workers: int = 1, progress=None) -> dict:
    """Every combination of the job's grid ({setting: [values]}); with job["split"] each one is backtested on the
    in-sample period (before the split date) and the out-of-sample period (from it). Rows carry the in/out (or
    whole-period) figures and a robustness score (average with the neighbours one step away) for each metric."""
    from .feeds import list_datasets
    from .runs import _clean
    from .strategy import load_strategies
    from .sweep import grid_size, neighbour_score, sweep
    if job.get("strategy") not in load_strategies():
        raise ValueError(f"No strategy called '{job.get('strategy')}'")
    if job.get("dataset") not in list_datasets():
        raise ValueError(f"No data set called '{job.get('dataset')}' here")
    grid = {k: [str(v) for v in vals] for k, vals in (job.get("grid") or {}).items() if vals}
    if not grid:
        raise ValueError("Vary at least one setting")
    total = grid_size(grid)
    limit = SWEEP_MAX_MAC if job.get("target") == "mac" else SWEEP_MAX
    if job.get("optimise"):
        return run_optimise(job, grid, limit, cfg, workers, progress)
    if total > limit:
        raise ValueError(f"{total} combinations; at most {limit}")
    cfg = job_config(job, cfg)
    base = {k: v for k, v in (job.get("params") or {}).items() if k not in grid}
    from .sweep import folds, luck_note, walk_forward
    n_folds = int(job.get("folds") or 0)
    fold_periods = None
    if n_folds:
        from .feeds import load_dataset
        if not 3 <= n_folds <= 8:
            raise ValueError("Walk-forward takes 3 to 8 periods")
        idx = load_dataset(job["dataset"], _day(job.get("from")), _day(job.get("to"))).index
        if not len(idx):
            raise ValueError("No index prices in that range")
        fold_periods = folds(idx[0].date(), idx[-1].date(), n_folds)
    df, out = sweep(job["strategy"], grid, job["dataset"], _day(job.get("from")), _day(job.get("to")),
                    job.get("split") or None, base_params=base, cfg=cfg, workers=workers,
                    progress=progress or (lambda n, t: _status(job, progress=[n, t])), periods=fold_periods)
    per = list(fold_periods) if fold_periods else ["in", "out"] if job.get("split") else ["all"]
    for m in METRICS:
        col = f"{per[0]}_{m}"
        if col in df:
            df[f"robust_{m}"] = neighbour_score(df, grid, col)
    cols = list(grid) + [f"{p}_{m}" for p in per for m in METRICS if f"{p}_{m}" in df] \
        + [f"robust_{m}" for m in METRICS if f"robust_{m}" in df] + [c for c in df if c.endswith("_error")]
    rows = [{c: _clean(r[c].item() if hasattr(r[c], "item") else r[c]) for c in cols} for _, r in df.head(SWEEP_KEEP).iterrows()]
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and v != v:
                r[k] = None
    res = {"sweep": out.name, "columns": cols, "rows": rows, "total": total, "periods": per,
           "luck": luck_note(df, total, per)}
    if fold_periods:
        res["walk"] = walk_forward(df, grid, fold_periods)
        res["fold_dates"] = {k: [str(a), str(b)] for k, (a, b) in fold_periods.items()}
    return res


OPTIMISE_GRID_MAX = 200_000    # Find best settings samples a grid this large; it tests at most the sweep limit


def run_optimise(job: dict, grid: dict, limit: int, cfg: dict | None = None, workers: int = 1, progress=None) -> dict:
    """Find best settings (algolab/optimise.py): walk-forward on all but the locked-away months, filters, a
    steadiness score, Conservative-fill and locked-away checks of the top picks."""
    import copy
    from .engine import backtest
    from .feeds import load_dataset
    from .optimise import run
    from .runs import _clean
    from .strategy import load_strategies
    from .sweep import grid_size, sweep
    if grid_size(grid) > OPTIMISE_GRID_MAX:
        raise ValueError(f"{grid_size(grid):,} combinations to choose from; at most {OPTIMISE_GRID_MAX:,}")
    cfg = job_config(job, cfg)
    base = {k: v for k, v in (job.get("params") or {}).items() if k not in grid}
    done = [0]
    budget = limit + 6                    # the sweep, then up to 3 picks x 2 checks

    report = progress or (lambda n, t: _status(job, progress=[n, t]))

    def tick(_n=None, _t=None):
        done[0] += 1
        report(min(done[0], budget), budget)

    def run_sweep_(combos, periods):
        df, _ = sweep(job["strategy"], grid, job["dataset"], _day(job.get("from")), _day(job.get("to")), None,
                      base_params=base, cfg=cfg, workers=workers, periods=periods, combos=combos,
                      progress=lambda n, t: tick())
        return df

    def run_one_(params, start, end, conservative):
        c = job_config({**job, "fills": "conservative" if conservative else "normal"}, copy.deepcopy(cfg))
        from .risk import split, strategy_params
        cls = load_strategies()[job["strategy"]]
        own, rk = split(params, c.get("risk"))
        if rk:
            c["risk"] = rk
        strat = cls(**strategy_params(cls, own, rk))
        try:
            m = backtest(c, strat, job["dataset"], start, end).metrics
        except ValueError:          # e.g. no days with real prices in the locked-away months
            m = {}
        tick()
        return m

    def data_range():
        idx = load_dataset(job["dataset"], _day(job.get("from")), _day(job.get("to"))).index
        if not len(idx):
            raise ValueError("No index prices in that range")
        return idx[0].date(), idx[-1].date()

    out = run(job, cfg, limit, run_sweep_, run_one_, data_range)
    scored, labels = out.pop("scored"), out.pop("labels")
    names = list(grid)
    cols = names + [f"{p}_{m}" for p in labels for m in METRICS if f"{p}_{m}" in scored] + \
        ["total_net", "periods_up", "worst_dd", "cycles", "score", "why_out"]
    rows = [{c: _clean(r[c].item() if hasattr(r[c], "item") else r[c]) for c in cols if c in scored}
            for _, r in scored.head(SWEEP_KEEP).iterrows()]
    for r in rows:
        for k, v in list(r.items()):
            if isinstance(v, float) and v != v:
                r[k] = None
    return {"optimise": out, "columns": [c for c in cols if c in scored], "rows": rows, "total": grid_size(grid),
            "periods": labels, "fold_dates": out["fold_dates"], "walk": out["walk"]}


def run_one(job: dict, store: Path | None = None, cfg: dict | None = None, publish=None, where: str = "the server") -> dict:
    from .engine import backtest
    from .feeds import list_datasets
    from . import runs
    from .strategy import load_strategies
    strategies = load_strategies()
    if job.get("strategy") not in strategies:
        raise ValueError(f"No strategy called '{job.get('strategy')}'")
    from .sec1_feed import SEC1_DATASET
    if job.get("dataset") not in list_datasets() and job.get("dataset") != SEC1_DATASET:   # 1-second: data/sec1, not data/spot
        raise ValueError(f"No data set called '{job.get('dataset')}' on the server")
    start, end = _day(job.get("from")), _day(job.get("to"))
    if start and end and end < start:
        raise ValueError("The end date is before the start date")
    from .risk import split, strategy_params
    own, rk = split(job.get("params"), job.get("risk"))
    if rk:
        job = {**job, "risk": rk}
    strat = strategies[job["strategy"]](**strategy_params(strategies[job["strategy"]], own, rk))
    cfg = job_config(job, cfg)
    res = backtest(cfg, strat, job["dataset"], start, end)
    note = (job.get("note") or "").strip()
    preset = job.get("preset") if isinstance(job.get("preset"), dict) else None
    if preset and preset.get("name"):
        note = (note + " · " if note else "") + f"preset {preset['name']}"
    note = (note + " · " if note else "") + f"run on {where} for {job.get('user') or 'the dashboard'}"
    run_dir = runs.save_run(res, strat, job["dataset"], start, end, cfg, note, root=runs.RUNS_DIR,
                            extra={"preset": {"id": str(preset.get("id", "")), "name": str(preset.get("name", ""))}} if preset else None)
    name = publish(run_dir) if publish else publish_local(run_dir, store)
    from .runs import _clean
    return {"run": name, "metrics": {k: _clean(res.metrics[k]) for k in HEADLINE if k in res.metrics}}


def run_compare(job: dict, store: Path | None = None, cfg: dict | None = None) -> dict:
    """Algo Studio › Analysis › Compare: backtest a paper or shadow session's last days with its own settings on the
    prices the server recorded, compare day by day and fill by fill (algolab/paper_report.py), and keep the result."""
    from datetime import timedelta
    from . import paper_report as pr, runs
    from .config import ist_now
    from .feeds import list_datasets
    from .paper import SESSIONS
    sid = str(job.get("session") or "")
    try:
        state = json.loads((SESSIONS / sid / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"No session called '{sid}' with saved trades") from None
    state["id"] = sid
    days = max(1, min(90, int(job.get("days") or 14)))
    end = ist_now().date()
    start = end - timedelta(days=days - 1)
    cfg = cfg or load_config()
    from .feeds import preferred_dataset
    dataset = job.get("dataset") or (cfg.get("paper_report") or {}).get("dataset") or preferred_dataset(start, end)
    if dataset not in list_datasets():
        raise ValueError(f"No data set called '{dataset}' on the server")
    res, strat, rep = pr.compare(state, start, end, cfg, dataset)
    pre = state.get("preset") if isinstance(state.get("preset"), dict) else None
    run_dir = runs.save_run(res, strat, dataset, start, end, pr.session_config(state, cfg),
                            f"Paper vs backtest: session {sid}, {start:%d %b} – {end:%d %b}", root=runs.RUNS_DIR,
                            extra={"preset": {"id": str(pre.get("id", "")), "name": str(pre.get("name", ""))}} if pre else None)
    rep["run"] = publish_local(run_dir, store)
    pr.remember(rep)
    from .runs import _clean
    return {"run": rep["run"], "metrics": {k: _clean(res.metrics[k]) for k in HEADLINE if k in res.metrics},
            "message": f"Paper {rep['paper_net']:,.0f}, backtest {rep['backtest_net']:,.0f} over the same days"}


def run_compare_period(job: dict, cfg: dict | None = None) -> dict:
    """Backtests › Compare with paper: every session (or those picked) against 1-minute and 1-second backtests over
    the chosen days, day by day (algolab/daily_compare.py). The report is runtime/compare/<job id>.json."""
    from . import daily_compare as dc
    start, end = _day(job.get("from")), _day(job.get("to"))
    if not start or not end:
        raise ValueError("Pick the first and last day.")
    rep = dc.run(start, end, job.get("bars") or ["1m", "1s"], job.get("sessions") or None, cfg or load_config(),
                 job_id=job["id"], log=lambda m: None, progress=lambda n, t, sid: _status(job, progress=[n, t], message=sid))
    ok = [r for r in rep["sessions"] if "error" not in r]
    p, lv = rep["totals"]["paper"], rep["totals"]["live"]
    bits = [f"{len(ok)} session(s)"]
    for name, t in (("paper", p), ("live", lv)):
        if t:
            bits.append(f"{name} {t.get('actual', 0):,.0f} vs 1m {t['bt_1m']:,.0f}" if "bt_1m" in t else f"{name} {t.get('actual', 0):,.0f}")
    bits.append(f"{rep['flagged']} flag(s)")
    return {"report": job["id"], "flagged": rep["flagged"], "message": " · ".join(bits), "progress": None}


def run_kite_ticks(job: dict) -> dict:
    """Algo Lab › Run › TrueData test, MCX: count Zerodha's ticks here while the Mac counts TrueData's."""
    from . import kite_tick_test as kt
    if not kt.mcx_open() and job.get("set") == "mcx":
        raise ValueError("MCX is closed now: try between 9:00 and 23:25 IST on a trading day.")
    try:
        report = kt.run(float(job.get("seconds") or 120), job.get("names") or None, start_at=job.get("start_at"))
    except SystemExit as e:
        raise ValueError(str(e)) from None
    return {"kite": report}


STOPPED = {      # systemd's $SERVICE_RESULT when it ended the runner -> what the page says
    "oom-kill": "Stopped by the server: it needed more than the 800 MB of memory backtests may use there. "
                "Try a shorter period or fewer combinations, or Run on Mac.",
    "timeout": "Stopped by the server: backtests may run for 3 hours at most there. "
               "Try fewer combinations or a shorter period, or Run on Mac.",
}
STOPPED_OTHER = ("Stopped before it finished (the server ran short of memory, reached the 3-hour limit or restarted). "
                 "Try again, or Run on Mac.")


def _close_stale(reason: str | None) -> int:
    """Marks server jobs still shown as running as failed, with why. Only call while holding the runner's lock: then
    no runner is alive, so a 'running' server job is one whose runner was killed (out of memory, time limit, restart)
    and would otherwise show its last progress forever. Mac jobs are the Mac helper's and are left alone."""
    n = 0
    for p in sorted(JOBS.glob("*.json")) if JOBS.is_dir() else []:
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if job.get("status") != "running" or job.get("target") == "mac" or not ID_OK.match(p.stem):
            continue
        _status({**job, "id": p.stem}, status="failed", finished=datetime.now().isoformat(timespec="seconds"),
                message=STOPPED.get(reason or "", STOPPED_OTHER))
        n += 1
    return n


def mark_stopped(reason: str | None = None) -> int:
    """After the runner ends (systemd ExecStopPost, with $SERVICE_RESULT) or at an update: close the jobs it left
    'running'. Does nothing while a runner is at work. Returns how many were closed."""
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME / "backtest-queue.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        return _close_stale(reason)


def process(store: Path | None = None, limit: int | None = None, cfg: dict | None = None) -> int:
    """Run every queued request, oldest first. One runner at a time; returns how many were run."""
    QUEUE.mkdir(parents=True, exist_ok=True)
    JOBS.mkdir(parents=True, exist_ok=True)
    with open(RUNTIME / "backtest-queue.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        _close_stale(None)                                # left by a runner that was killed before
        done = 0
        while limit is None or done < limit:
            reqs = sorted(QUEUE.glob("*.json"))
            if not reqs:
                break
            path = reqs[0]
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                job = {}
            path.unlink(missing_ok=True)                  # off the queue first: a crash never repeats it
            jid = path.stem
            if not ID_OK.match(jid):
                continue
            job = {**job, "id": jid}
            _status(job, status="running", started=datetime.now().isoformat(timespec="seconds"), message="")
            try:
                kind = job.get("kind")
                out = run_sweep(job, cfg) if kind == "sweep" else run_compare(job, store, cfg) if kind == "compare" \
                    else run_compare_period(job, cfg) if kind == "compare_period" \
                    else run_kite_ticks(job) if kind == "kite_tick_test" else run_one(job, store, cfg)
                _status(job, status="done", finished=datetime.now().isoformat(timespec="seconds"), **out)
            except Exception as e:  # report every failure on the page, never stop the queue
                _status(job, status="failed", finished=datetime.now().isoformat(timespec="seconds"),
                        message=f"{type(e).__name__}: {e}"[:400])
            done += 1
        return done
