"""Algo Studio › Analysis and stage gates: how a strategy did in backtest, paper and shadow, and whether it is ready
for its next stage.

  Same days, three ways   each day's P&L in paper and shadow (closed trades, by exit day) and, where a comparison was
                          run (weekly report or Compare), the backtest of those same days with the same settings
  What the gap is made of  from the latest comparison: fills dearer or cheaper than the backtest's, orders only one made
  How sure                 the latest backtest's robustness figures (reshuffled drawdown, charges +50%, margin, months)
  Stage gates              checks for the next stage; advice only, nothing is blocked

The limits are in GATES and can be changed in config.yaml under `studio: gates:`.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime

from . import data

GATES = {
    "bt_months": 6,           # a backtest covering at least this many months
    "bt_cycles": 30,          # ... with at least this many cycles
    "bt_real_pct": 80,        # ... priced on real option prices for at least this share of fills
    "paper_days": 10,         # trading days in paper (and in shadow) before the next stage
    "gap_pct": 20,            # paper / shadow at most this far behind its backtest over the same days
}


def gates_limits() -> dict:
    lim = dict(GATES)
    try:
        from algolab.config import load_config
        lim.update(((load_config().get("studio") or {}).get("gates")) or {})
    except Exception:  # noqa: BLE001  settings unreadable: the defaults
        pass
    return lim


def _num(v):
    try:
        return float(str(v).rstrip("%"))
    except (TypeError, ValueError):
        return None


# ---- sessions: P&L by day --------------------------------------------------------------------------------------
def session_days(sid: str) -> dict:
    """{trading days the session ran, net P&L by exit day} from its saved account and its minute equity file."""
    d = data.runtime() / "sessions" / sid
    st = data._json(d / "state.json", {}) or {}
    by_day: dict = {}
    for t in (st.get("broker") or {}).get("trades", []):
        day = str(t.get("exit_time", ""))[:10]
        if day:
            by_day[day] = by_day.get(day, 0.0) + float(t.get("net_pnl") or 0)
    ran = set()
    try:
        with open(d / "equity.csv", newline="", encoding="utf-8") as f:
            for row in csv.reader(f):
                if row and row[0][:4].isdigit():
                    ran.add(row[0][:10])
    except OSError:
        pass
    return {"ran": sorted(ran | set(by_day)), "pnl": {k: round(v, 0) for k, v in sorted(by_day.items())},
            "feed": st.get("feed"), "risk": st.get("risk") or {}, "live": st.get("live") or {}}


def comparisons() -> dict:
    """Each session's latest paper-vs-backtest comparison (algolab/paper_report.py)."""
    out = {}
    rep = data._json(data.runtime() / "paper_report.json", {}) or {}
    for r in rep.get("sessions") or []:
        if "error" not in r:
            out[r["session"]] = {**r, "made": rep.get("made")}
    out.update(data._json(data.runtime() / "paper_compare.json", {}) or {})
    return out


def compare_jobs(strategy: str) -> list[dict]:
    out = []
    jobs = data.runtime() / "backtest-jobs"
    for f in sorted(jobs.glob("*.json"), reverse=True)[:200] if jobs.is_dir() else []:
        j = data._json(f) or {}
        if j.get("kind") == "compare" and j.get("strategy") == strategy:
            out.append({k: j.get(k) for k in ("id", "session", "days", "status", "message", "queued", "finished", "run")})
        if len(out) >= 10:
            break
    return out


# ---- the latest backtest: how sure ---------------------------------------------------------------------------
ROBUST = (("Net P&L", "Net P&L"), ("Net with charges 50% higher", "Charges +50%"),
          ("Max drawdown", "Max drawdown"), ("Monte Carlo: max drawdown, 1 in 20 worse than", "Drawdown, 1 in 20 reshuffles"),
          ("Monte Carlo: chance of a losing period %", "Chance of a losing period %"),
          ("Return on peak margin %", "Return on peak margin %"), ("Peak margin (estimated)", "Peak margin"),
          ("Profit factor", "Profit factor"), ("Win rate %", "Win rate %"), ("Cycles", "Cycles"),
          ("Worst day", "Worst day"), ("Longest drawdown (days)", "Longest drawdown (days)"),
          ("Fills on real option prices", "Fills on real option prices"))


def robust(run: dict | None) -> dict | None:
    if not run:
        return None
    d = data.bt_store() / run["name"]
    m = data._json(d / "run.json", {}) or {}
    mt = m.get("metrics") or {}
    figs = [{"label": label, "value": mt[k]} for k, label in ROBUST if k in mt]
    months: dict = {}
    try:
        with open(d / "cycles.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                k = str(row.get("exit_time", ""))[:7]
                if k:
                    months[k] = months.get(k, 0.0) + float(row.get("net_pnl") or 0)
    except (OSError, ValueError):
        pass
    return {"run": run["name"], "from": run.get("from"), "to": run.get("to"), "preset": run.get("preset"),
            "figs": figs, "months": [{"month": k, "net": round(v, 0)} for k, v in sorted(months.items())],
            "metrics": mt}


# ---- stage gates ---------------------------------------------------------------------------------------------
def _span_months(a, b) -> float:
    try:
        return (date.fromisoformat(str(b)[:10]) - date.fromisoformat(str(a)[:10])).days / 30.4
    except ValueError:
        return 0.0


def _gap_check(sessions: list, comps: dict, lim: dict, mode: str) -> dict:
    """The newest comparison of a session of this mode: is it within the allowed gap?"""
    reps = sorted((comps[s["id"]] for s in sessions if s["mode"] == mode and s["id"] in comps),
                  key=lambda r: str(r.get("made") or r.get("to") or ""), reverse=True)
    name = f"{mode.title()} within {lim['gap_pct']:g}% of its backtest, same days"
    if not reps:
        return {"name": name, "ok": None, "note": "Not compared yet: Analysis › Compare."}
    r = reps[0]
    bt, pp = float(r.get("backtest_net") or 0), float(r.get("paper_net") or 0)
    if bt <= 0:
        ok, note = pp >= bt, f"Backtest ₹{bt:,.0f}, {mode} ₹{pp:,.0f}"
    else:
        behind = (bt - pp) / bt * 100
        ok, note = behind <= lim["gap_pct"], f"{mode.title()} {'behind' if behind > 0 else 'ahead'} by {abs(behind):.0f}% (₹{pp:,.0f} vs ₹{bt:,.0f})"
    return {"name": name, "ok": ok, "note": note + f", {r.get('from')} – {r.get('to')}"}


def gates(entry: dict, stage: str, runs: list, sessions: list, comps: dict, preset: str | None = None) -> dict:
    """The checks for the strategy's next stage: {"next", "checks": [{name, ok (True/False/None = not known), note}]}."""
    lim = gates_limits()
    order = list(data.STAGES)
    nxt = order[order.index(stage) + 1] if stage in order and stage != "Live" else None
    checks: list = []
    from .results import reference_first
    pick = reference_first(entry["name"], [r for r in runs if not preset or r.get("preset_id") == preset], preset)
    if nxt == "Backtested":
        checks.append({"name": "A backtest", "ok": False, "note": "Run one from Overview › Start."})
    elif nxt == "Paper":
        r = pick[0] if pick else None
        mt = (data._json(data.bt_store() / r["name"] / "run.json", {}) or {}).get("metrics", {}) if r else {}
        months = _span_months(r.get("from"), r.get("to")) if r else 0
        net, net50, cyc, real = _num(mt.get("Net P&L")), _num(mt.get("Net with charges 50% higher")), _num(mt.get("Cycles")), \
            _num(mt.get("Fills on real option prices"))
        where = f" (latest backtest{', preset ' + r['preset'] if r and r.get('preset') else ''})" if r else ""
        checks += [
            {"name": f"Covers {lim['bt_months']:g}+ months{where}", "ok": months >= lim["bt_months"], "note": f"{months:.0f} months"},
            {"name": f"{lim['bt_cycles']:g}+ cycles", "ok": (cyc or 0) >= lim["bt_cycles"], "note": f"{cyc or 0:.0f} cycles"},
            {"name": "Made money", "ok": (net or 0) > 0, "note": f"₹{net or 0:,.0f}"},
            {"name": "Still makes money with charges 50% higher", "ok": None if net50 is None else net50 > 0,
             "note": "" if net50 is None else f"₹{net50:,.0f}"},
            {"name": f"{lim['bt_real_pct']:g}%+ of fills on real option prices", "ok": None if real is None else real >= lim["bt_real_pct"],
             "note": "" if real is None else f"{real:.0f}%"},
        ]
    elif nxt in ("Shadow", "Live"):
        mode = "paper" if nxt == "Shadow" else "shadow"
        mine = [s for s in sessions if s["mode"] == mode]
        days = max((len(session_days(s["id"])["ran"]) for s in mine), default=0)
        checks.append({"name": f"{lim['paper_days']:g}+ trading days in {mode}", "ok": days >= lim["paper_days"], "note": f"{days} days"})
        if mode == "paper":
            kite = any(s.get("feed") == "kite" for s in mine)
            checks.append({"name": "Paper on Zerodha live prices", "ok": kite,
                           "note": "" if kite else "Simulated prices cannot be compared with a backtest."})
        checks.append(_gap_check(mine, comps, lim, mode))
        if nxt == "Live":
            loss = any(float((session_days(s["id"])["risk"] or {}).get("day_loss") or 0) > 0 for s in mine)
            checks += [{"name": "A day loss limit in Orders & stops", "ok": loss, "note": "" if loss else "Set Day loss in the preset."},
                       {"name": "Strategy enabled for live orders", "ok": bool(entry.get("live_ready")), "note": ""}]
    passed = sum(1 for c in checks if c["ok"] is True)
    return {"next": nxt, "checks": checks, "passed": passed, "total": len(checks),
            "ready": bool(checks) and all(c["ok"] is True for c in checks)}


# ---- the whole Analysis tab ----------------------------------------------------------------------------------
def analysis(entry: dict, preset: str | None = None, days: int = 10) -> dict:
    name = entry["name"]
    allss = [s for s in data.sessions() if s["strategy"] == name and not s.get("sim")]    # simulated: not paper trading
    ss = [s for s in allss if not preset or (s.get("preset_id") == preset)]
    rr = data.runs(name)
    comps = comparisons()
    # same days, three ways: the last `days` days any session closed trades on
    table: dict = {}
    for s in ss:
        if s["mode"] not in ("paper", "shadow"):
            continue
        sd = session_days(s["id"])
        for d, v in sd["pnl"].items():
            row = table.setdefault(d, {"day": d, "paper": None, "shadow": None, "backtest": None})
            row[s["mode"]] = (row[s["mode"]] or 0) + v
        rep = comps.get(s["id"])
        for r in (rep or {}).get("days") or []:
            row = table.setdefault(r["day"], {"day": r["day"], "paper": None, "shadow": None, "backtest": None})
            if row["backtest"] is None:          # one backtest per day (paper and shadow run the same settings)
                row["backtest"] = r.get("backtest")
    rows = sorted(table.values(), key=lambda r: r["day"])[-days:]
    tot = {k: round(sum(r[k] or 0 for r in rows), 0) if any(r[k] is not None for r in rows) else None
           for k in ("backtest", "paper", "shadow")}
    gap = []
    for s in ss:
        rep = comps.get(s["id"])
        if rep:
            f = rep.get("fills") or {}
            gap.append({"session": s["id"], "label": s.get("label") or s["id"], "mode": s["mode"], "from": rep.get("from"),
                        "to": rep.get("to"), "paper": rep.get("paper_net"), "backtest": rep.get("backtest_net"),
                        "avg_fill": f.get("avg_diff"), "matched": f.get("matched"), "paper_only": f.get("paper_only"),
                        "backtest_only": f.get("backtest_only"), "run": rep.get("run"), "made": rep.get("made")})
    from .results import reference_first
    run = next(iter(reference_first(name, [r for r in rr if not preset or r.get("preset_id") == preset], preset)), None)
    stg = data.stage(allss, rr)
    return {"days": rows, "totals": tot, "gap": gap, "robust": robust(run),
            "gates": gates(entry, stg, rr, allss, comps, preset), "stage": stg,
            "sessions": [{"id": s["id"], "label": s.get("label") or s["id"], "mode": s["mode"], "feed": s.get("feed"),
                          "running": s["running"]} for s in allss if s["mode"] in ("paper", "shadow")],
            "jobs": compare_jobs(name), "limits": gates_limits()}


def queue_compare(strategy: str, sid: str, days: int, user: str = "") -> dict:
    """A comparison for the server's backtest runner (algolab/backtest_queue.py, kind "compare")."""
    import base64
    import re
    s = next((x for x in data.sessions() if x["id"] == sid and x["strategy"] == strategy), None)
    if s is None:
        raise LookupError("No such session of this strategy.")
    if s.get("feed") != "kite":
        raise ValueError("Only sessions on Zerodha live prices can be compared with a backtest.")
    days = max(3, min(90, int(days or 14)))
    rt = data.runtime()
    jobs, queue = rt / "backtest-jobs", rt / "backtest-queue"
    recent = [data._json(f) or {} for f in sorted(jobs.glob("*.json"), reverse=True)[:50]] if jobs.is_dir() else []
    busy = [j for j in recent if j.get("status") in ("queued", "running")]
    if len(busy) >= 3:
        raise ValueError(f"{len(busy)} backtests are already waiting; try again when one finishes.")
    jid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + re.sub(r"[^a-z0-9]", "a", base64.b32encode(os.urandom(5)).decode().lower()[:4])
    start = date.fromordinal(date.today().toordinal() - days + 1)
    job = {"id": jid, "kind": "compare", "strategy": strategy, "title": f"Paper vs backtest: {s.get('label') or sid}",
           "session": sid, "days": days, "params": {}, "target": "server", "dataset": "", "from": str(start), "to": str(date.today()),
           "note": f"Compare session {sid}, last {days} days", "user": user, "queued": datetime.now().isoformat(timespec="seconds"),
           "status": "queued"}
    jobs.mkdir(parents=True, exist_ok=True)
    queue.mkdir(parents=True, exist_ok=True)
    (jobs / f"{jid}.json").write_text(json.dumps(job, indent=1))
    tmp = jobs / f".{jid}.request"
    tmp.write_text(json.dumps(job))
    tmp.rename(queue / f"{jid}.json")
    return job

