"""Paper (and shadow, and live) against backtests on 1-minute and 1-second bars, day by day, for a chosen period.

For every session on Zerodha prices that traded in the period, its strategy is backtested with the session's own
settings over the same days twice: on the index's 1-minute set (NIFTY_minute) and stepped every second on the
recorded 1-second bars (NIFTY_1second, only for the days kept). Each backtest starts flat at 09:15 on the first day
and carries positions across days like any backtest. Then, per session and day:

  actual          net P&L the session booked that day (paper / shadow / live; live is never added to paper)
  bt_1m, bt_1s    net P&L the backtests booked that day (None when that kind could not run, e.g. no 1-second bars)
  gap_1m, gap_1s  backtest minus actual
  flag            a gap above max(Rs 500, 10% of |actual|)

and per session over the period: fills matched with each backtest (same contract and side, nearest in time within
15 minutes), the average price difference per unit (+ = the session did worse), the same for stop exits alone,
orders in one but not the other, and labels that explain gaps which are not errors:

  late start      the session's first price that day came after 09:20 (the backtest was watching from 09:15)
  carried in      the session already held positions when the period began (the backtest starts flat): those trades
                  are left out of "actual" and listed apart (rep["left_out"]), so both sides count only trades
                  opened inside the period
  holiday fills   the session traded on an NSE holiday (stale prices; run.py paper-scrub)

  python run.py compare --from 2026-10-27 --to 2026-10-31 [--bars 1m,1s] [--sessions id,id]
  dashboard: Backtests › Compare with paper (a "compare_period" job on the server's backtest queue)

The report is kept in runtime/compare/<id>.json and runtime/compare/latest.json.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from pathlib import Path

import pandas as pd

from .config import RUNTIME, load_config, to_date

OUT = RUNTIME / "compare"
BARS = {"1m": "NIFTY_minute", "1s": None}       # 1s: sec1_feed.SEC1_DATASET (imported lazily)
LATE_AFTER = time(9, 20)
FLAG_MIN, FLAG_SHARE, FILL_SHARE = 500.0, 0.10, 0.01


def sessions(ids=None) -> list[dict]:
    """Saved sessions on Zerodha prices (paper, shadow, live), with their id and equity file."""
    from .paper import SESSIONS
    out = []
    if not SESSIONS.is_dir():
        return out
    want = {str(i) for i in ids} if ids else None
    for d in sorted(SESSIONS.iterdir()):
        if want is not None and d.name not in want:
            continue
        try:
            st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if st.get("feed") != "kite":
            continue
        out.append({**st, "id": st.get("id") or d.name, "mode": st.get("mode", "paper"), "_equity": d / "equity.csv"})
    return out


def _first_price_by_day(path: Path) -> dict:
    """{day: "HH:MM:SS" of the session's first account point that day} from its equity file (to the second, so a
    1-second backtest starts when the session did, not at the start of that minute)."""
    out: dict = {}
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                t = line.split(",", 1)[0]
                if len(t) < 16 or not t[:4].isdigit():
                    continue
                d = t[:10]
                if d not in out:
                    hms = t[11:19].strip()
                    out[d] = hms if len(hms) == 8 else hms[:5] + ":00"
    except OSError:
        pass
    return out


def _labels(state: dict, start: date, end: date, cal) -> dict:
    """{"late": {day: "HH:MM"}, "carried": bool, "holiday": [days]}."""
    b = state.get("broker") or {}
    firsts = _first_price_by_day(state["_equity"]) if state.get("_equity") else {}
    late = {d: hms[:5] for d, hms in firsts.items()
            if start <= date.fromisoformat(d) <= end and hms > LATE_AFTER.strftime("%H:%M:%S")}
    carried = any(datetime.fromisoformat(str(p.get("opened_at"))).date() < start for p in b.get("positions") or []
                  if p.get("opened_at")) or any(
        datetime.fromisoformat(t["entry_time"]).date() < start <= datetime.fromisoformat(t["exit_time"]).date()
        for t in b.get("trades") or [])
    hol = sorted({datetime.fromisoformat(f["time"]).date().isoformat() for f in b.get("fills") or []
                  if start <= datetime.fromisoformat(f["time"]).date() <= end
                  and not cal.is_trading_day(datetime.fromisoformat(f["time"]).date())})
    return {"late": late, "carried": bool(carried), "holiday": hol}


_STOPPED = re.compile(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})\s+Session stopped: (.*)")


def _halt_kind(reason: str) -> str:
    low = reason.lower()
    for key, words in (("max lots", ("above the limit of",)), ("orders per day", ("orders to open positions today",)),
                       ("daily loss", ("daily loss limit",)), ("positions differ", ("positions differ",)),
                       ("dashboard", ("from the dashboard",)), ("rejected", ("refused", "rejected", "partial"))):
        if any(w in low for w in words):
            return key
    return "stopped"


def _halts(state: dict, start: date, end: date) -> dict:
    """{day: [{"time": "HH:MM", "reason", "kind"}]}: when the session stopped itself (a live limit, a rejection...),
    from its log. A stopped session trades no more that day, while the backtests carry on."""
    out: dict = {}
    for line in state.get("logs") or []:
        m = _STOPPED.match(str(line))
        if not m or not start <= date.fromisoformat(m.group(1)) <= end:
            continue
        reason = m.group(3).strip()
        out.setdefault(m.group(1), []).append({"time": m.group(2), "reason": reason, "kind": _halt_kind(reason)})
    return out


def _hms(t) -> str:
    return pd.Timestamp(t).strftime("%H:%M:%S")


def _trade_list(trades) -> list:
    """Closed trades as plain rows, for the Compare page: a session's saved trades (dicts) or a backtest's
    trades (DataFrame). Each row is filed under the day it exited (the day its P&L counts on)."""
    rows = trades.to_dict("records") if isinstance(trades, pd.DataFrame) else list(trades or [])
    out = []
    for t in rows:
        out.append({"day": pd.Timestamp(t["exit_time"]).date().isoformat(), "contract": str(t["contract"]),
                    "side": str(t["side"]), "qty": int(t["qty"]), "entry": _hms(t["entry_time"]),
                    "exit": _hms(t["exit_time"]), "entry_price": round(float(t["entry_price"]), 2),
                    "exit_price": round(float(t["exit_price"]), 2), "net": round(float(t["net_pnl"]), 0),
                    "reason": str(t.get("reason") or "")})
    return sorted(out, key=lambda r: (r["day"], r["entry"]))


def _trade_rows_safe(trades) -> list:
    """_trade_list, but a result that lacks the trade columns gives no trade rows rather than failing the session."""
    try:
        return _trade_list(trades)
    except (KeyError, TypeError, ValueError):
        return []


def _left_out(state: dict, trades: list, start: date) -> dict:
    """What the comparison does not count: the session's trades that closed in the period but were opened before it
    (a backtest starts flat), and positions opened before it that are still held."""
    rows = []
    for t in _trade_rows_safe(trades):
        full = next((x for x in trades if str(x["exit_time"])[11:19] == t["exit"] and
                     str(x["contract"]) == t["contract"] and str(x["exit_time"])[:10] == t["day"]), {})
        rows.append({**t, "entry_day": str(full.get("entry_time", ""))[:10]})
    held = []
    for p in (state.get("broker") or {}).get("positions") or []:
        try:
            opened = datetime.fromisoformat(str(p.get("opened_at")))
        except ValueError:
            continue
        if opened.date() < start:
            held.append({"contract": str(p.get("label") or p.get("contract") or ""), "side": str(p.get("side") or ""),
                         "qty": p.get("qty"), "opened": opened.strftime("%Y-%m-%d %H:%M:%S")})
    return {"trades": rows, "held": held, "net": round(sum(r["net"] for r in rows), 0)}


def _by_day(trades) -> dict:
    out: dict = {}
    if trades is None or not len(trades):
        return out
    for _, t in trades.iterrows():
        d = pd.Timestamp(t["exit_time"]).date().isoformat()
        out[d] = out.get(d, 0.0) + float(t["net_pnl"])
    return out


def _stops(fills: list) -> list:
    return [f for f in fills if "stop" in str(f.get("reason", "")).lower() or "sl " in str(f.get("reason", "")).lower()]


def _fill_check(paper_fills: list, res) -> dict:
    from .paper_report import match_fills
    m = match_fills(paper_fills, res.fills if res is not None else None)
    stops = _stops(paper_fills)
    m["stops"] = match_fills(stops, res.fills if res is not None else None) if stops else None
    prices = sorted(float(f["price"]) for f in paper_fills if f.get("price"))
    med = prices[len(prices) // 2] if prices else None
    m["avg_diff_pct"] = round(m["avg_diff"] / med * 100, 2) if m.get("avg_diff") is not None and med else None
    m["flag"] = bool(m["avg_diff_pct"] is not None and abs(m["avg_diff_pct"]) > FILL_SHARE * 100)
    return m


def _flag(actual: float, bt) -> bool:
    if bt is None:
        return False
    return abs(bt - actual) > max(FLAG_MIN, FLAG_SHARE * abs(actual))


def compare_session(state: dict, start: date, end: date, cfg: dict, bars=("1m", "1s"), log=print) -> dict:
    from .engine import backtest
    from .feeds import Feed
    from .paper_report import paper_days, session_config
    from .risk import split, strategy_params
    from .sec1_feed import SEC1_DATASET
    from .strategy import load_strategies
    cls = load_strategies()[state["strategy"]]
    own, rk = split(state.get("params") or {}, state.get("risk"))
    p_days, p_fills = paper_days(state, start, end, entered_from=start)
    actual = {d.isoformat(): v for d, v in p_days.items()}
    rep = {"session": state["id"], "strategy": state["strategy"], "label": state.get("label", ""),
           "mode": state["mode"], "labels": _labels(state, start, end, Feed(cfg)), "bars": {}, "days": []}
    bt_days: dict = {}
    # a day the session started late (its first price after 09:15): the backtest starts at that time too
    firsts = _first_price_by_day(state["_equity"]) if state.get("_equity") else {}
    not_before = {date.fromisoformat(d): time.fromisoformat(hm) for d, hm in firsts.items()
                  if start <= date.fromisoformat(d) <= end and hm > "09:15:00"}
    rep["starts"] = {d.isoformat(): t.strftime("%H:%M:%S") for d, t in not_before.items()}
    in_period = [t for t in (state.get("broker") or {}).get("trades", [])
                 if start <= datetime.fromisoformat(t["exit_time"]).date() <= end]
    saved = [t for t in in_period if datetime.fromisoformat(t["entry_time"]).date() >= start]
    rep["period"] = {"from": start.isoformat(), "to": end.isoformat()}
    rep["left_out"] = _left_out(state, [t for t in in_period if t not in saved], start)
    trade_rows = {"actual": _trade_rows_safe(saved)}
    for b in bars:
        dataset = SEC1_DATASET if b == "1s" else BARS[b]
        try:
            res = backtest(session_config(state, cfg), cls(**strategy_params(cls, own, rk)), dataset, start, end,
                           not_before=not_before)
        except Exception as e:  # noqa: BLE001 - e.g. no 1-second bars for those days
            rep["bars"][b] = {"error": f"{type(e).__name__}: {e}"[:300]}
            log(f"{state['id']} {b}: {e}")
            continue
        bt_days[b] = _by_day(res.trades)
        trade_rows["bt_" + b] = _trade_rows_safe(res.trades)
        rep["bars"][b] = {"net": round(sum(bt_days[b].values()), 0), "fills": _fill_check(p_fills, res),
                          "prices": {k: res.metrics[k] for k in res.metrics
                                     if k.startswith(("Price", "Fills on", "Fills not", "Recorded days", "Trading days without",
                                                               "Days without", "Expiries settled"))}}
        left = res.metrics.get("Trading days without 1-second bars (left out)" if b == "1s"
                               else "Days without real option prices (left out)", "")
        rep["bars"][b]["missing_days"] = [x.strip() for x in str(left).split(",") if x.strip()]
    halts = _halts(state, start, end)
    rep["halts"] = halts
    days = sorted(set(actual) | {d for v in bt_days.values() for d in v} | set(halts)
                  | {t["day"] for t in rep["left_out"]["trades"]})
    for d in days:
        a = round(actual.get(d, 0.0), 0)
        row = {"day": d, "actual": a}
        for b in bars:
            if b not in bt_days or d in rep["bars"].get(b, {}).get("missing_days", []):
                row[f"bt_{b}"] = row[f"gap_{b}"] = None
                continue
            v = round(bt_days[b].get(d, 0.0), 0)
            row[f"bt_{b}"], row[f"gap_{b}"] = v, round(v - a, 0)
        row["flag"] = any(_flag(a, row.get(f"bt_{b}")) for b in bars)
        notes = []
        if d in rep["labels"]["late"]:
            notes.append(f"late start {rep['labels']['late'][d]}")
        if d in rep["labels"]["holiday"]:
            notes.append("holiday fills")
        out_n = [t for t in rep["left_out"]["trades"] if t["day"] == d]
        if out_n:
            notes.append(f"{len(out_n)} trade{'s' if len(out_n) > 1 else ''} opened before {start:%d %b} left out "
                         f"({sum(t['net'] for t in out_n):+,.0f})")
        for h in halts.get(d, []):
            notes.append(f"session stopped {h['time']} ({h['kind']})")
        row["notes"] = notes
        rep["days"].append(row)
    rep["actual_net"] = round(sum(actual.values()), 0)
    rep["trades"] = {}          # day -> {"actual": [...], "bt_1m": [...], "bt_1s": [...]}: the trades behind each day
    for kind, rows in trade_rows.items():
        for r in rows:
            rep["trades"].setdefault(r.pop("day"), {}).setdefault(kind, []).append(r)
    return rep


def run(start, end, bars=("1m", "1s"), ids=None, cfg: dict | None = None, job_id: str | None = None, log=print,
        progress=None) -> dict:
    cfg = cfg or load_config()
    start, end = to_date(start), to_date(end)
    if end < start:
        raise ValueError("The period ends before it starts.")
    bars = tuple(b for b in ("1m", "1s") if b in set(bars)) or ("1m",)
    from .paper_report import paper_days
    todo = [s for s in sessions(ids) if any(paper_days(s, start, end))]
    reports = []
    for i, st in enumerate(todo):
        if progress:
            progress(i, len(todo), st["id"])
        try:
            reports.append(compare_session(st, start, end, cfg, bars, log))
        except Exception as e:  # noqa: BLE001 - one session failing must not stop the others
            reports.append({"session": st["id"], "strategy": st.get("strategy"), "mode": st.get("mode"),
                            "error": f"{type(e).__name__}: {e}"[:300]})
    ok = [r for r in reports if "error" not in r]
    total = {"paper": {}, "live": {}}
    for r in ok:
        side = "live" if r["mode"] == "live" else "paper"      # shadow sessions place no orders: with paper
        t = total[side]
        t["actual"] = t.get("actual", 0) + r["actual_net"]
        for b in bars:
            n = (r["bars"].get(b) or {}).get("net")
            if n is not None:
                t[f"bt_{b}"] = t.get(f"bt_{b}", 0) + n
    out = {"id": job_id, "made": datetime.now().isoformat(timespec="seconds"), "from": str(start), "to": str(end),
           "bars": list(bars), "sessions": reports, "totals": total,
           "flagged": sum(1 for r in ok for d in r["days"] if d["flag"]) + sum(
               1 for r in ok for b in bars if ((r["bars"].get(b) or {}).get("fills") or {}).get("flag"))}
    OUT.mkdir(parents=True, exist_ok=True)
    body = json.dumps(out, indent=1, default=str)
    if job_id:
        (OUT / f"{job_id}.json").write_text(body, encoding="utf-8")
    tmp = OUT / "latest.tmp"
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(OUT / "latest.json")
    return out


def to_csv(rep: dict) -> str:
    """One row per session and day, for the download button."""
    lines = ["session,strategy,mode,day,actual,bt_1m,gap_1m,bt_1s,gap_1s,flag,notes"]
    for r in rep.get("sessions", []):
        if "error" in r:
            lines.append(f"{r['session']},{r.get('strategy', '')},{r.get('mode', '')},,,,,,,,error: {r['error'].replace(',', ';')}")
            continue
        for d in r["days"]:
            cell = lambda k: "" if d.get(k) is None else f"{d[k]:.0f}"  # noqa: E731
            lines.append(",".join([r["session"], r["strategy"], r["mode"], d["day"], cell("actual"), cell("bt_1m"),
                                   cell("gap_1m"), cell("bt_1s"), cell("gap_1s"), "yes" if d["flag"] else "",
                                   "; ".join(d["notes"])]))
    return "\n".join(lines) + "\n"


def enqueue_day(day: date, cfg: dict | None = None) -> str | None:
    """Queue the day's comparison (all sessions, 1-minute and 1-second) on the server's backtest queue, so it runs at
    low priority after the close. Called by `run.py collect`. Returns the job id, or None when nothing traded."""
    import base64
    import os
    from .backtest_queue import JOBS, QUEUE
    from .paper_report import paper_days
    if not any(any(paper_days(st, day, day)) for st in sessions()):
        return None
    jid = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + "".join(
        c if c.isalnum() else "a" for c in base64.b32encode(os.urandom(5)).decode().lower()[:4])
    job = {"id": jid, "kind": "compare_period", "title": "Compare with paper (nightly)", "from": str(day), "to": str(day),
           "bars": ["1m", "1s"], "sessions": [], "target": "server", "user": "nightly",
           "queued": datetime.now().isoformat(timespec="seconds"), "status": "queued"}
    JOBS.mkdir(parents=True, exist_ok=True)
    QUEUE.mkdir(parents=True, exist_ok=True)
    (JOBS / f"{jid}.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
    tmp = JOBS / f".{jid}.request"
    tmp.write_text(json.dumps(job), encoding="utf-8")
    tmp.replace(QUEUE / f"{jid}.json")
    return jid
