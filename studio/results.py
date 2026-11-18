"""Algo Studio › Results: every saved backtest and sweep, by strategy and preset, with clear names.

Each run is described the same way everywhere: strategy · preset · data period · data set · fills, with badges for where
it ran (server or Mac) and for the automatic paper-vs-backtest runs. A run can be starred as its preset's reference
(stage gates, Analysis and Share then use it instead of the latest) and archived (hidden unless asked for).

  runtime/results_meta.json   {"refs": {strategy: {preset id or "": run}}, "archived": [run, ...]}
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import data

META = "results_meta.json"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def meta() -> dict:
    m = data._json(data.runtime() / META, {}) or {}
    return {"refs": m.get("refs") or {}, "archived": list(m.get("archived") or [])}


def _save(m: dict) -> None:
    data._write(data.runtime() / META, m)


def dataset_label(name: str | None) -> str:
    """NIFTY_minute / TD_NIFTY_1minute -> NIFTY 1-min; NIFTY_5minute -> NIFTY 5-min."""
    if not name:
        return ""
    m = re.match(r"^(?:TD_|SAMPLE_)?([A-Z]+)_(\d*)minute$", name)
    return f"{m.group(1)} {m.group(2) or '1'}-min" if m else name


def period_label(a, b) -> str:
    try:
        x, y = date.fromisoformat(str(a)[:10]), date.fromisoformat(str(b)[:10])
    except ValueError:
        return ""
    if x.year == y.year:
        return f"{MONTHS[x.month - 1]}–{MONTHS[y.month - 1]} {y:%y}" if x.month != y.month else f"{MONTHS[x.month - 1]} {y:%y}"
    return f"{MONTHS[x.month - 1]} {x:%y} – {MONTHS[y.month - 1]} {y:%y}"


def _where(note: str) -> str:
    n = (note or "").lower()
    return "mac" if "run on the mac" in n or "on mac" in n else "server" if "run on the server" in n else ""


def runs(strategy: str | None = None) -> list[dict]:
    """Saved backtests with their name in words, badges, reference and archive flags; newest first."""
    m = meta()
    arch = set(m["archived"])
    titles = {c["name"]: (c.get("title") or c["name"]).split(" (")[0] for c in data.catalog()}
    out = []
    for r in data.runs(strategy, limit=2000):
        full = data._json(data.bt_store() / r["name"] / "run.json", {}) or {}
        ex = (full.get("config") or {}).get("execution") or {}
        conservative = float(ex.get("cost_stress") or 1) >= 2
        note = r.get("note") or ""
        kind = "compare" if note.startswith("Paper vs backtest") else "backtest"
        pid = r.get("preset_id") or ""
        ref = (m["refs"].get(r["strategy"] or "") or {}).get(pid) == r["name"]
        user_note = " · ".join(x.strip() for x in note.split("·") if x.strip() and not re.match(r"(preset |run on )", x.strip())) \
            if kind == "backtest" else ""
        out.append({**r, "kind": kind, "title": titles.get(r["strategy"], r["strategy"] or "?"), "known": r["strategy"] in titles,
                    "preset_name": r.get("preset") or "Defaults", "preset_id": pid,
                    "period": period_label(r.get("from"), r.get("to")), "data": dataset_label(r.get("dataset")),
                    "fills": "conservative fills" if conservative else "normal fills", "where": _where(note),
                    "user_note": user_note, "ref": ref, "archived": r["name"] in arch})
    return out


def sweep_items(strategy: str | None = None) -> list[dict]:
    names = [strategy] if strategy else [c["name"] for c in data.catalog()]
    titles = {c["name"]: (c.get("title") or c["name"]).split(" (")[0] for c in data.catalog()}
    out = []
    for n in names:
        for j in data.sweeps(n):
            out.append({**j, "strategy": n, "title": titles.get(n, n), "kind": "find_best" if j.get("optimise") else "sweep",
                        "period": period_label(j.get("from"), j.get("to")), "data": dataset_label(j.get("dataset")),
                        "saved": j.get("finished")})
    return out


def _best(rs: list) -> dict | None:
    xs = [r for r in rs if isinstance(r.get("net"), (int, float))]
    return max(xs, key=lambda r: r["net"]) if xs else None


def overview(strategy: str | None = None, kind: str = "all", days: int = 0, sort: str = "new", archived: bool = False,
             preset: str | None = None) -> dict:
    """Groups by strategy (each with its presets: count, latest, best, reference) and the filtered runs."""
    allruns = runs(strategy)
    since = (datetime.now() - timedelta(days=days)).isoformat() if days else ""
    sw = sweep_items(strategy) if kind in ("all", "sweep") else []

    def keep(r):
        return ((archived or not r["archived"]) and (not since or str(r.get("saved") or "") >= since)
                and (kind == "all" or r["kind"] == kind) and (preset is None or r["preset_id"] == preset))
    shown = [r for r in allruns if keep(r)]
    if since:
        sw = [s for s in sw if str(s.get("saved") or "") >= since]
    key = {"net": lambda r: -(r.get("net") or -1e18), "retdd": lambda r: -(r.get("ret_dd") or -1e18),
           "new": lambda r: "" if not r.get("saved") else "~" + str(r["saved"])}
    shown.sort(key=key.get(sort, key["new"]), reverse=sort == "new")
    stages = {}
    if not strategy:
        ss = data.sessions()
        allr = data.runs(limit=2000)
        stages = {c["name"]: data.summary(c, ss, allr)["stage"] for c in data.catalog()}
    groups: dict = {}
    for r in allruns:
        if r["archived"] and not archived:
            continue
        g = groups.setdefault(r["strategy"] or "?", {"strategy": r["strategy"], "title": r["title"], "known": r["known"],
                                                     "stage": stages.get(r["strategy"]), "presets": {}, "count": 0})
        g["count"] += 1
        p = g["presets"].setdefault(r["preset_id"], {"id": r["preset_id"], "name": r["preset_name"], "runs": []})
        p["runs"].append(r)
    out_groups = []
    for g in groups.values():
        pres = []
        for p in g["presets"].values():
            rs = p.pop("runs")
            own = [r for r in rs if r["kind"] == "backtest"] or rs
            pres.append({**p, "count": len(rs), "latest": own[0], "best": _best(own), "ref": next((r for r in rs if r["ref"]), None)})
        pres.sort(key=lambda p: (p["id"] == "", p["name"].lower()))
        b = _best([p["best"] for p in pres if p["best"]])
        out_groups.append({**g, "presets": pres, "best": b})
    out_groups.sort(key=lambda g: (not g["known"], g["title"].lower()))
    return {"groups": out_groups, "runs": shown, "sweeps": sw, "refs": meta()["refs"]}


def set_ref(run: str, on: bool = True) -> dict:
    r = next((x for x in data.runs(limit=5000) if x["name"] == run), None)
    if r is None:
        raise LookupError("No such backtest.")
    m = meta()
    mine = m["refs"].setdefault(r["strategy"], {})
    pid = r.get("preset_id") or ""
    if on:
        mine[pid] = run
    elif mine.get(pid) == run:
        mine.pop(pid)
    _save(m)
    return {"strategy": r["strategy"], "preset": pid, "ref": run if on else None}


def set_archived(names: list, on: bool = True) -> int:
    known = {x["name"] for x in data.runs(limit=5000)}
    names = [n for n in names if n in known]
    m = meta()
    cur = set(m["archived"])
    cur = (cur | set(names)) if on else (cur - set(names))
    m["archived"] = sorted(cur)
    _save(m)
    return len(names)


def reference_first(strategy: str, rows: list, preset: str | None = None) -> list:
    """Runs with the starred reference of the preset (or of the defaults) first: what gates and Analysis use."""
    refs = meta()["refs"].get(strategy) or {}
    ref = refs.get(preset or "") if preset is not None else None
    if ref is None and preset is None and len(refs) == 1:
        ref = next(iter(refs.values()))
    if not ref:
        return rows
    return sorted(rows, key=lambda r: r.get("name") != ref)


# ---- one run, several runs side by side, a sweep's picks (part B) ----------------------------------------------------
KEY_FIGS = (("Net P&L", "Net P&L", "money"), ("Max drawdown", "Max drawdown", "money"), ("Return / drawdown", "Return ÷ drawdown", "num"),
            ("Cycles", "Cycles", "num"), ("Win rate %", "Win rate %", "num"), ("Profit factor", "Profit factor", "num"),
            ("Net with charges 50% higher", "Charges +50%", "money"),
            ("Monte Carlo: max drawdown, 1 in 20 worse than", "Drawdown, 1 in 20 reshuffles", "money"),
            ("Return on peak margin %", "Return on peak margin %", "num"), ("Worst day", "Worst day", "money"),
            ("Charges paid", "Charges paid", "cost"), ("Fills on real option prices", "Real option prices", "text"))


def _run_dir(name: str):
    store = data.bt_store()
    d = store / str(name or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(name or "")) or d.parent != store or not (d / "run.json").is_file():
        raise LookupError("No such backtest.")
    return d


def _csv(path, limit=None) -> list[dict]:
    import csv
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError:
        return []
    return rows if limit is None else rows[:limit]


def _num(v):
    try:
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def curve(d) -> list:
    """[[date, P&L since the start, drawdown from the peak]] from the daily equity, at most ~400 points."""
    rows = _csv(d / "equity_daily.csv")
    pts = [(r.get("date") or r.get("") or "", _num(r.get("equity"))) for r in rows]
    pts = [(a, b) for a, b in pts if a and b is not None]
    if not pts:
        return []
    start, peak, out = pts[0][1], pts[0][1], []
    for a, b in pts:
        peak = max(peak, b)
        out.append([a[:10], round(b - start), round(b - peak)])
    step = max(1, len(out) // 400)
    return out[::step] + ([out[-1]] if (len(out) - 1) % step else [])


def settings_of(m: dict) -> dict:
    """The settings a run used: its own and its Orders & stops as "risk.*"."""
    risk = ((m.get("config") or {}).get("risk")) or {}
    return {**(m.get("params") or {}), **{"risk." + k: v for k, v in risk.items()}}


def _labels(strategy: str) -> dict:
    entry = next((c for c in data.catalog() if c["name"] == strategy), None) or {}
    return {p["key"]: {"label": re.sub(r"\s*\([^()]*\)\s*$", "", p["label"]), "default": p.get("default"),
                       "group": p.get("group", "")} for p in entry.get("params", [])}


def run_detail(name: str) -> dict:
    d = _run_dir(name)
    m = data._json(d / "run.json", {}) or {}
    head = next((r for r in runs(m.get("strategy")) if r["name"] == name), None) or {"name": name}
    mt = m.get("metrics") or {}
    lab = _labels(m.get("strategy"))
    sets = [{"key": k, "label": (lab.get(k) or {}).get("label", k), "value": v,
             "changed": k in lab and lab[k].get("default") != v, "risk": k.startswith("risk.")}
            for k, v in settings_of(m).items()]
    cyc = []
    for r in _csv(d / "cycles.csv"):
        cyc.append({"group": r.get("group"), "entry": (r.get("entry_time") or "")[:16], "exit": (r.get("exit_time") or "")[:16],
                    "legs": r.get("legs"), "net": _num(r.get("net_pnl")), "reason": r.get("exit_reason")})
    months: dict = {}
    for c in cyc:
        if c["exit"] and c["net"] is not None:
            months[c["exit"][:7]] = months.get(c["exit"][:7], 0.0) + c["net"]
    ranked = sorted((c for c in cyc if c["net"] is not None), key=lambda c: c["net"])
    return {"run": head, "figs": [{"label": l, "value": mt.get(k), "kind": t} for k, l, t in KEY_FIGS if k in mt],
            "metrics": mt, "settings": sets, "curve": curve(d), "months": [{"month": k, "net": round(v)} for k, v in sorted(months.items())],
            "worst": ranked[:5], "best": ranked[-5:][::-1], "recent": cyc[-20:][::-1], "cycles": len(cyc)}


def compare_runs(names: list) -> dict:
    names = list(dict.fromkeys(str(n) for n in names))[:3]
    if len(names) < 2:
        raise ValueError("Pick two or three runs to compare.")
    cols = []
    for n in names:
        d = _run_dir(n)
        m = data._json(d / "run.json", {}) or {}
        head = next((r for r in runs(m.get("strategy")) if r["name"] == n), None) or {"name": n}
        cols.append({"run": head, "metrics": m.get("metrics") or {}, "settings": settings_of(m), "curve": curve(d),
                     "strategy": m.get("strategy")})
    lab = {}
    for c in cols:
        lab.update(_labels(c["strategy"]))
    keys = list(dict.fromkeys(k for c in cols for k in c["settings"]))
    rows = [{"key": k, "label": (lab.get(k) or {}).get("label", k), "values": [c["settings"].get(k) for c in cols],
             "differs": len({json_key(c["settings"].get(k)) for c in cols}) > 1} for k in keys]
    figs = [{"label": l, "kind": t, "values": [c["metrics"].get(k) for c in cols]} for k, l, t in KEY_FIGS]
    return {"runs": [c["run"] for c in cols], "figs": [f for f in figs if any(v is not None for v in f["values"])],
            "settings": rows, "curves": [c["curve"] for c in cols],
            "same_strategy": len({c["strategy"] for c in cols}) == 1}


def json_key(v) -> str:
    import json
    return json.dumps(v, sort_keys=True, default=str)


def sweep_detail(jid: str) -> dict:
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-z0-9]{4}", str(jid or "")):
        raise LookupError("No such sweep.")
    j = data._json(data.runtime() / "backtest-jobs" / f"{jid}.json")
    if not j or j.get("kind") != "sweep":
        raise LookupError("No such sweep.")
    base = dict(j.get("params") or {})
    grid = list((j.get("grid") or {}).keys())
    lab = _labels(j.get("strategy"))
    out = {"id": jid, "strategy": j.get("strategy"), "title": j.get("title"), "status": j.get("status"),
           "period": period_label(j.get("from"), j.get("to")), "data": dataset_label(j.get("dataset")),
           "total": j.get("total"), "find_best": bool(j.get("optimise")), "grid": [{"key": k, "label": (lab.get(k) or {}).get("label", k)} for k in grid],
           "message": j.get("message")}
    opt = j.get("optimise") if isinstance(j.get("optimise"), dict) else None
    if opt:
        out["verdict"] = opt.get("verdict")
        out["picks"] = [{"n": i + 1, "params": {**base, **(p.get("params") or {})}, "varied": p.get("params") or {},
                         "total_net": p.get("total_net"), "worst_dd": p.get("worst_dd"), "periods_up": p.get("periods_up"),
                         "periods": p.get("periods"), "cycles": p.get("cycles"), "conservative_net": p.get("conservative_net"),
                         "lock_net": p.get("lock_net"), "reasons": p.get("reasons") or []}
                        for i, p in enumerate((opt.get("picks") or [])[:5])]
    else:
        per = (j.get("periods") or ["all"])[0]
        rows = []
        for i, r in enumerate((j.get("rows") or [])[:10]):
            varied = {k: r.get(k) for k in grid}
            rows.append({"n": i + 1, "params": {**base, **varied}, "varied": varied, "net": r.get(f"{per}_net"),
                         "max_dd": r.get(f"{per}_max_dd"), "ret_dd": r.get(f"{per}_ret_dd"), "cycles": r.get(f"{per}_cycles"),
                         "robust_net": r.get("robust_net")})
        out["rows"] = rows
        out["period_name"] = per
    return out
