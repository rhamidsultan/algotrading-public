"""What Algo Studio reads and keeps: strategies, presets, backtests, sweeps and sessions."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from algolab.config import RUNTIME

STAGES = ("Draft", "Backtested", "Paper", "Shadow", "Live")


def runtime() -> Path:
    return Path(os.environ.get("STUDIO_RUNTIME") or RUNTIME)


def bt_store() -> Path:
    return Path(os.environ.get("BACKTESTS_STORE") or "/opt/trader/data/backtests")


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda c: None)
    except (OSError, ValueError):
        return default


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _age(path: Path) -> float:
    try:
        return datetime.now().timestamp() - float(path.read_text())
    except (OSError, ValueError):
        return 1e9


# ---- strategies -------------------------------------------------------------------------------------------
def catalog() -> list[dict]:
    """The strategies and their settings (the engine's list), with each one's saved Orders & stops defaults."""
    cat = _json(runtime() / "strategies.json")
    if not cat:
        from algolab.paper import catalog as build
        cat = build()
    saved = _json(runtime() / "risk_defaults.json", {}) or {}
    for c in cat:
        mine = saved.get(c.get("name")) or {}
        for p in c.get("params", []):
            k = str(p.get("key", ""))
            if k.startswith("risk.") and k[5:] in mine:
                p["default"] = mine[k[5:]]
    return cat


# ---- presets ----------------------------------------------------------------------------------------------
def presets_file() -> Path:
    return runtime() / "presets.json"


def presets(name: str | None = None) -> dict:
    allp = _json(presets_file(), {}) or {}
    return allp.get(name, {}) if name else allp


def clean_params(entry: dict, params: dict | None) -> dict:
    """A preset's settings typed like the strategy's list (its own settings and the "risk.*" Orders & stops)."""
    keys = {p["key"]: p for p in entry.get("params", [])}
    out = {}
    for k, v in (params or {}).items():
        p = keys.get(k)
        if p is None:
            continue
        t = p.get("type")
        try:
            if t == "bool":
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
            elif t == "int":
                v = int(float(v))
            elif t == "float":
                v = float(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            raise ValueError(f"'{p['label']}' needs a number.") from None
        if p.get("choices") and v not in p["choices"]:
            raise ValueError(f"'{p['label']}' must be one of {', '.join(map(str, p['choices']))}.")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and p.get("min") is not None and v < p["min"]:
            raise ValueError(f"'{p['label']}' must be at least {p['min']}.")
        out[k] = v
    return out


def save_preset(strategy: str, name: str, params: dict, pid: str = "", source: str = "", by: str = "") -> tuple[str, dict]:
    entry = next((c for c in catalog() if c["name"] == strategy), None)
    if entry is None:
        raise LookupError("Unknown strategy.")
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:40]
    if not name:
        raise ValueError("Give the preset a name.")
    clean = clean_params(entry, params)
    allp = presets()
    mine = allp.setdefault(strategy, {})
    if pid and pid not in mine:
        raise LookupError("No such preset.")
    if any(v.get("name", "").lower() == name.lower() for k, v in mine.items() if k != pid):
        raise ValueError("Another preset already has that name.")
    if not pid:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "preset"
        pid, n = base, 2
        while pid in mine:
            pid, n = f"{base}-{n}", n + 1
    now = datetime.now().isoformat(timespec="seconds")
    old = mine.get(pid, {})
    version = old.get("version", 0) + (1 if clean != old.get("params") else 0)
    mine[pid] = {"name": name, "params": clean, "source": str(source or old.get("source") or "Made by hand")[:120],
                 "created": old.get("created", now), "updated": now, "version": max(version, 1), "by": by,
                 **({"share": old["share"]} if old.get("share") else {})}
    _write(presets_file(), allp)
    return pid, mine[pid]


def delete_preset(strategy: str, pid: str) -> None:
    allp = presets()
    if allp.get(strategy, {}).pop(pid, None) is None:
        raise LookupError("No such preset.")
    _write(presets_file(), allp)


# ---- backtests, sweeps, sessions ------------------------------------------------------------------------------
def runs(strategy: str | None = None, limit: int = 300) -> list[dict]:
    """Saved backtests (the dashboard's Results), newest first."""
    out, store = [], bt_store()
    if not store.is_dir():
        return out
    for d in sorted(store.iterdir(), reverse=True):
        if len(out) >= limit:
            break
        m = _json(d / "run.json") if d.is_dir() else None
        if not m or (strategy and m.get("strategy") != strategy):
            continue
        mt = m.get("metrics") or {}
        pr = m.get("preset") or {}
        out.append({"name": d.name, "strategy": m.get("strategy"), "saved": m.get("saved"), "note": m.get("note", ""),
                    "dataset": m.get("dataset"),
                    "preset": pr.get("name"), "preset_id": pr.get("id"),
                    "from": m.get("data_from") or m.get("from"), "to": m.get("data_to") or m.get("to"),
                    "net": mt.get("Net P&L"), "max_dd": mt.get("Max drawdown"), "cycles": mt.get("Cycles"),
                    "win": mt.get("Win rate %"), "ret_dd": mt.get("Return / drawdown")})
    return out


def run_settings(run: str) -> tuple[str, dict]:
    """(strategy, settings) a saved backtest ran with: its own settings and its Orders & stops as "risk.*"."""
    store = bt_store()
    d = store / str(run or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(run or "")) or d.parent != store or not (d / "run.json").is_file():
        raise LookupError("No such backtest.")
    m = _json(d / "run.json", {}) or {}
    risk = ((m.get("config") or {}).get("risk")) or {}
    return str(m.get("strategy") or ""), {**(m.get("params") or {}), **{"risk." + k: v for k, v in risk.items()}}


def sweeps(strategy: str, limit: int = 30) -> list[dict]:
    out = []
    jobs = runtime() / "backtest-jobs"
    for f in sorted(jobs.glob("*.json"), reverse=True)[:400] if jobs.is_dir() else []:
        j = _json(f)
        if not j or j.get("strategy") != strategy or j.get("kind") != "sweep" or j.get("status") != "done":
            continue
        out.append({k: j.get(k) for k in ("id", "dataset", "from", "to", "total", "finished", "target", "folds")}
                   | {"optimise": bool(j.get("optimise"))})
        if len(out) >= limit:
            break
    return out


def sessions() -> list[dict]:
    out = []
    root = runtime() / "sessions"
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if not d.is_dir() or not ((d / "state.json").exists() or (d / "desired.json").exists()):
            continue
        s, w = _json(d / "state.json", {}) or {}, _json(d / "desired.json", {}) or {}
        info = w or s
        b = s.get("broker") or {}
        total = (s["equity"] - b["capital"]) if s.get("equity") is not None and b.get("capital") is not None else None
        days = sorted({str(t.get("exit_time", ""))[:10] for t in b.get("trades", []) if t.get("exit_time")})
        out.append({"id": d.name, "strategy": info.get("strategy", d.name), "mode": info.get("mode", "paper"),
                    "running": _age(d / "heartbeat") < 15, "wanted": bool(w), "label": info.get("label", ""),
                    "preset": (w.get("preset") or s.get("preset") or {}).get("name"), "total": total,
                    "preset_id": (w.get("preset") or s.get("preset") or {}).get("id"),
                    "open_legs": len(s.get("positions") or []), "days": len(days), "feed": info.get("feed", ""),
                    "sim": info.get("feed") == "sim", "sim_time": s.get("time") if info.get("feed") == "sim" else None})
    return out


def stage(mine_sessions: list, mine_runs: list) -> str:
    modes = {x["mode"] for x in mine_sessions if (x["wanted"] or x["running"]) and not x.get("sim")}   # simulated: a check only
    for m, st in (("live", "Live"), ("shadow", "Shadow"), ("paper", "Paper")):
        if m in modes:
            return st
    return "Backtested" if mine_runs else "Draft"


def summary(entry: dict, all_sessions: list, all_runs: list) -> dict:
    mine = [x for x in all_sessions if x["strategy"] == entry["name"]]
    rr = [r for r in all_runs if r["strategy"] == entry["name"]]
    out = {"name": entry["name"], "title": (entry.get("title") or entry["name"]).split(" (")[0],
           "description": entry.get("description", ""), "live_ready": entry.get("live_ready", False),
           "built": bool(entry.get("built")),
           "stage": stage(mine, rr), "presets": len(presets(entry["name"])), "backtest": rr[0] if rr else None,
           "best": max((r for r in rr if isinstance(r.get("net"), (int, float))), key=lambda r: r["net"], default=None),
           "backtests": len(rr)}
    sims = [x for x in mine if x.get("sim")]
    if sims:
        out["sim"] = {"sessions": len(sims), "running": sum(1 for x in sims if x["running"])}
    for m in ("paper", "shadow", "live"):
        ss = [x for x in mine if x["mode"] == m and not x.get("sim")]
        if ss:
            out[m] = {"total": round(sum(x["total"] or 0 for x in ss), 0), "running": sum(1 for x in ss if x["running"]),
                      "sessions": len(ss), "days": max(x["days"] for x in ss)}
    return out
