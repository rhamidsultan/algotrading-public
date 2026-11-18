"""Weekly paper-vs-backtest report: does the backtest do what the paper session really did?

For every paper or shadow session on Zerodha live prices that traded in the week, the same strategy with the same
settings is backtested over the same days on the prices the server recorded. The two are compared day by day, and
each paper fill is matched with the backtest's fill of the same contract and side nearest in time:

  P&L per day      paper, backtest, difference
  fills            how much dearer (or cheaper) paper fills were per unit than the backtest's, on average
  missing          trades one made and the other did not

The backtest is saved and put on the Backtests page (note "Paper vs backtest"), with these figures added to its
results; a short summary is sent to the phone; the full report is kept in runtime/paper_report.json. Its "daily" part
is the same week day by day for every session including live, against 1-minute and 1-second backtests
(algolab/daily_compare.py: actual, bt_1m, bt_1s, gaps, flags, fill and stop-exit differences, labels).

  python run.py paper-report [--days 7]     (server: algolab-paper-report.timer, Saturdays 08:05 IST)

A large gap means the backtest flatters (or misjudges) the strategy: fills, missing prices, or timing.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pandas as pd

from .config import RUNTIME, ist_now

OUT = RUNTIME / "paper_report.json"
LATEST = RUNTIME / "paper_compare.json"   # each session's latest comparison (weekly report or Algo Studio › Compare)
MATCH_MINUTES = 15            # a backtest fill this close in time to a paper fill counts as the same order


def _sessions() -> list[dict]:
    from .paper import SESSIONS
    out = []
    if not SESSIONS.is_dir():
        return out
    for d in sorted(SESSIONS.iterdir()):
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if st.get("feed") == "kite" and st.get("mode", "paper") in ("paper", "shadow"):
            out.append({**st, "id": st.get("id") or d.name})
    return out


def paper_days(state: dict, start: date, end: date, entered_from: date | None = None) -> tuple[dict, list]:
    """(net P&L by exit day, fills) of a session's saved account within [start, end].

    `entered_from`: count only trades opened on or after that day (a backtest starts flat, so a trade carried in
    from before the period has nothing to be compared with); their closing fills are left out too."""
    b = state.get("broker") or {}
    by_day: dict = {}
    skipped: set = set()
    for t in b.get("trades", []):
        d = datetime.fromisoformat(t["exit_time"]).date()
        if not start <= d <= end:
            continue
        if entered_from is not None and datetime.fromisoformat(t["entry_time"]).date() < entered_from:
            skipped.add((str(t.get("contract")), str(t["exit_time"])))
            continue
        by_day[d] = by_day.get(d, 0.0) + float(t["net_pnl"])
    fills = [f for f in b.get("fills", []) if start <= datetime.fromisoformat(f["time"]).date() <= end
             and (str(f.get("contract")), str(f["time"])) not in skipped]
    return by_day, fills


def match_fills(paper: list, backtest: pd.DataFrame) -> dict:
    """Pairs each paper fill with the backtest fill of the same contract and side nearest in time (within
    MATCH_MINUTES). Returns the average price difference per unit (positive = paper paid more / got less)."""
    if backtest is None or not len(backtest):
        return {"matched": 0, "paper_only": len(paper), "backtest_only": 0, "avg_diff": None}
    bt = backtest.copy()
    bt["time"] = pd.to_datetime(bt["time"])
    used, diffs, unmatched = set(), [], 0
    for f in paper:
        t = pd.Timestamp(f["time"])
        cand = bt[(bt.contract == f["contract"]) & (bt.side == f["side"])]
        cand = cand[~cand.index.isin(used)]
        if not len(cand):
            unmatched += 1
            continue
        gap = (cand["time"] - t).abs()
        i = gap.idxmin()
        if gap[i] > pd.Timedelta(minutes=MATCH_MINUTES):
            unmatched += 1
            continue
        used.add(i)
        d = float(f["price"]) - float(cand.loc[i, "price"])
        diffs.append(d if f["side"] == "BUY" else -d)          # dearer buys and cheaper sells both count against paper
    bt_only = int((~bt.index.isin(used)).sum()) if len(bt) else 0
    if "source" in bt:
        bt_only = int(((~bt.index.isin(used)) & (bt.source != "settlement")).sum())
    return {"matched": len(diffs), "paper_only": unmatched, "backtest_only": bt_only,
            "avg_diff": round(sum(diffs) / len(diffs), 2) if diffs else None}


def session_config(state: dict, cfg: dict) -> dict:
    """The settings to backtest a session with: its Orders & stops on top of `cfg`."""
    from .backtest_queue import job_config
    from .risk import split
    _, rk = split(state.get("params") or {}, state.get("risk"))
    return job_config({"risk": rk}, cfg) if rk else cfg


def compare(state: dict, start: date, end: date, cfg: dict, dataset: str):
    """Backtest the session's strategy and settings over [start, end]; returns (result, report dict)."""
    from .engine import backtest
    from .risk import split, strategy_params
    from .strategy import load_strategies
    cls = load_strategies()[state["strategy"]]
    own, rk = split(state.get("params") or {}, state.get("risk"))     # with the session's Orders & stops
    strat = cls(**strategy_params(cls, own, rk))
    res = backtest(session_config(state, cfg), strat, dataset, start, end)
    p_days, p_fills = paper_days(state, start, end)
    b_days: dict = {}
    if len(res.trades):
        for _, t in res.trades.iterrows():
            d = pd.Timestamp(t["exit_time"]).date()
            b_days[d] = b_days.get(d, 0.0) + float(t["net_pnl"])
    days = sorted(set(p_days) | set(b_days))
    rows = [{"day": str(d), "paper": round(p_days.get(d, 0.0), 0), "backtest": round(b_days.get(d, 0.0), 0),
             "diff": round(b_days.get(d, 0.0) - p_days.get(d, 0.0), 0)} for d in days]
    fills = match_fills(p_fills, res.fills)
    paper_net, bt_net = sum(p_days.values()), sum(b_days.values())
    rep = {"session": state["id"], "strategy": state["strategy"], "label": state.get("label", ""),
           "from": str(start), "to": str(end), "paper_net": round(paper_net, 0), "backtest_net": round(bt_net, 0),
           "diff": round(bt_net - paper_net, 0), "days": rows, "fills": fills}
    m = res.metrics
    m["Paper P&L (same days)"] = rep["paper_net"]
    m["Backtest minus paper"] = rep["diff"]
    m["Fills matched with paper"] = fills["matched"]
    if fills["avg_diff"] is not None:
        m["Paper fill vs backtest, per unit (+ = paper worse)"] = fills["avg_diff"]
    if fills["paper_only"] or fills["backtest_only"]:
        m["Orders in paper only / backtest only"] = f"{fills['paper_only']} / {fills['backtest_only']}"
    return res, strat, rep


def remember(rep: dict) -> None:
    """Keep a session's latest comparison (Algo Studio › Analysis reads it)."""
    try:
        allr = json.loads(LATEST.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        allr = {}
    allr[rep["session"]] = {**rep, "made": datetime.now().isoformat(timespec="seconds")}
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    tmp = LATEST.with_suffix(".tmp")
    tmp.write_text(json.dumps(allr, indent=1, default=str), encoding="utf-8")
    tmp.replace(LATEST)


def weekly(cfg: dict, days: int = 7, publish: bool = True, log=print) -> dict:
    from . import runs
    from .backtest_queue import publish_local
    end = ist_now().date()
    start = end - timedelta(days=days - 1)
    from .feeds import preferred_dataset
    dataset = (cfg.get("paper_report") or {}).get("dataset") or preferred_dataset(start, end)
    reports = []
    for st in _sessions():
        p_days, p_fills = paper_days(st, start, end)
        if not p_days and not p_fills:
            continue
        try:
            res, strat, rep = compare(st, start, end, cfg, dataset)
        except Exception as e:  # one session failing must not stop the others
            reports.append({"session": st["id"], "strategy": st.get("strategy"), "error": f"{type(e).__name__}: {e}"[:300]})
            log(f"{st['id']}: {e}")
            continue
        if publish:
            run_dir = runs.save_run(res, strat, dataset, start, end, session_config(st, cfg),
                                    f"Paper vs backtest: session {st['id']}, {start:%d %b} – {end:%d %b}", root=runs.RUNS_DIR)
            rep["run"] = publish_local(run_dir)
        reports.append(rep)
        remember(rep)
        log(f"{st['id']}: paper {rep['paper_net']:,.0f}, backtest {rep['backtest_net']:,.0f}")
    out = {"made": datetime.now().isoformat(timespec="seconds"), "from": str(start), "to": str(end),
           "dataset": dataset, "sessions": reports}
    try:   # the same week day by day, paper / shadow / live vs 1-minute and 1-second backtests (algolab/daily_compare.py)
        from . import daily_compare
        out["daily"] = daily_compare.run(start, end, ("1m", "1s"), cfg=cfg, log=log)
    except Exception as e:  # noqa: BLE001 - the weekly report stands without it
        out["daily"] = {"error": f"{type(e).__name__}: {e}"[:300]}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1, default=str), encoding="utf-8")
    ok = [r for r in reports if "error" not in r]
    if ok and publish:
        from .pushbox import push
        body = "; ".join(f"{r['session']}: paper Rs {r['paper_net']:,.0f}, backtest Rs {r['backtest_net']:,.0f}" for r in ok[:4])
        d = out.get("daily") or {}
        if "flagged" in d:
            body += f". Day by day (1-minute and 1-second): {d['flagged']} flag(s)"
        push("Paper vs backtest this week", body, "/backtests", "daily")
    return out
