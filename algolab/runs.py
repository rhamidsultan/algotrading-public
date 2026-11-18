"""Saved backtest runs: every result kept with exactly what produced it, so it can be reproduced.

runs/<YYYYmmdd-HHMMSS>_<strategy>/
    run.json     strategy, parameters, dataset and date range, data file fingerprint, code version,
                 the full settings used, and the headline metrics
    equity.csv  trades.csv  cycles.csv  fills.csv  log.txt
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import ROOT, SPOT_DIR

RUNS_DIR = ROOT / "runs"


def code_version() -> str:
    """Git commit of the code, with '+changes' when there are uncommitted edits."""
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "algolab", "config.yaml"], cwd=ROOT,
                               capture_output=True, text=True, timeout=5).stdout.strip()
        if sha:
            return sha + ("+changes" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        pass
    return deployed_commit() or "not in git"


def deployed_commit() -> str:
    """On the server the code is a copy without git: the update stamps the GitHub commit in .deployed_commit."""
    try:
        sha = (ROOT / ".deployed_commit").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return sha[:7] if re.fullmatch(r"[0-9a-f]{7,40}", sha) else ""


def data_fingerprint(dataset: str) -> dict:
    path = SPOT_DIR / f"{dataset}.csv"
    if not path.exists():
        return {"file": path.name}
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"file": path.name, "sha256": h.hexdigest()[:16], "bytes": path.stat().st_size}


def _clean(v):
    """JSON-safe metric value: NaN becomes null, infinity (e.g. profit factor with no losses) 'n/a'."""
    if isinstance(v, float) and v != v:
        return None
    if isinstance(v, float) and v in (float("inf"), float("-inf")):
        return "n/a"
    return v


def save_run(res, strategy, dataset: str, start, end, cfg: dict, note: str = "", root: Path = RUNS_DIR,
             extra: dict | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = root / f"{stamp}_{strategy.name}"
    n = 1
    while out.exists():
        n += 1
        out = root / f"{stamp}-{n}_{strategy.name}"
    out.mkdir(parents=True)
    meta = {
        "saved": datetime.now().isoformat(timespec="seconds"),
        "strategy": strategy.name, "params": strategy.params, "note": note,
        "dataset": dataset, "from": str(start) if start else None, "to": str(end) if end else None,
        "data_from": str(res.equity.index[0].date()) if len(res.equity) else None,
        "data_to": str(res.equity.index[-1].date()) if len(res.equity) else None,
        "data": data_fingerprint(dataset), "code": code_version(),
        "metrics": {k: _clean(v) for k, v in res.metrics.items()},
        "config": cfg,
        **(extra or {}),                     # e.g. {"preset": {"id", "name"}}: Algo Lab › Strategies
    }
    (out / "run.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
    res.equity.to_csv(out / "equity.csv")
    for name in ("trades", "cycles", "fills"):
        getattr(res, name).to_csv(out / f"{name}.csv", index=False)
    (out / "log.txt").write_text("\n".join(res.logs), encoding="utf-8")
    return out


def list_runs(root: Path = RUNS_DIR) -> pd.DataFrame:
    rows = []
    for f in sorted(root.glob("*/run.json"), reverse=True):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mt = m.get("metrics", {})
        changed = {k: v for k, v in m.get("params", {}).items()}
        rows.append({"run": f.parent.name, "strategy": m.get("strategy"), "dataset": m.get("dataset"),
                     "from": m.get("from"), "to": m.get("to"), "net_pnl": mt.get("Net P&L"),
                     "max_dd": mt.get("Max drawdown"), "cycles": mt.get("Cycles"),
                     "real_prices": mt.get("Fills on real option prices"), "code": m.get("code"),
                     "sent": (f.parent / ".published").exists(),
                     "note": m.get("note", ""), "params": json.dumps(changed)})
    return pd.DataFrame(rows)
