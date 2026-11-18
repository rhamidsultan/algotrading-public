"""Remove what the paper engine did on an NSE holiday it should not have traded on.

Before the holiday fix (pull request #100) the live feed ran on weekday holidays with the last
day's prices (Tue 20 Oct 2026, Dussehra). For every paper session this:

  * drops trades and fills made entirely on that day, and gives their P&L and charges back;
  * drops that day's equity points and moves later points by the same amount;
  * resets a session whose position from that day was carried on (or that closed an older
    position at that day's stale prices): its later trades rest on a fake price;

and deletes the minute prices the feed recorded that day (data/options, data/spot).

Run with the paper engine stopped: `python run.py paper-scrub` does every past holiday in
config.yaml from the last 30 days, once each (a marker in runtime/ remembers it); a backup of
runtime/sessions is written first. `--day 2026-10-20` does one day; `--dry-run` only reports.
"""
from __future__ import annotations

import csv
import tarfile
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DATA, RUNTIME, ist_now
from . import paper


def _day(value) -> date | None:
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def scrub_session(slot: "paper.Slot", day: date, dry_run: bool = False) -> str:
    """Clean one session. Returns what happened, in a few words."""
    state = paper._read_json(slot.state)
    if not state or "broker" not in state:
        return "no saved account"
    b = state["broker"]
    trades, fills = b.get("trades", []), b.get("fills", [])
    same_day = [t for t in trades if _day(t["entry_time"]) == day and _day(t["exit_time"]) == day]
    carried = [t for t in trades if (_day(t["entry_time"]) == day) != (_day(t["exit_time"]) == day)]
    open_from_day = [p for p in b.get("positions", []) if _day(p.get("opened_at")) == day]
    day_fills = [f for f in fills if _day(f["time"]) == day]
    if carried or open_from_day:
        if not dry_run:
            paper.reset(slot.id)
        return "reset (a position from that day was carried on)"
    if not same_day and not day_fills and not _has_points(slot, day):
        return "nothing on that day"
    gross = sum(float(t["gross_pnl"]) for t in same_day)
    costs = sum(float(f["costs"]) for f in day_fills)
    delta = gross - costs                       # what that day added to the account (usually negative)
    if not dry_run:
        b["trades"] = [t for t in trades if t not in same_day]
        b["fills"] = [f for f in fills if f not in day_fills]
        b["realized_gross"] = float(b["realized_gross"]) - gross
        b["total_costs"] = float(b["total_costs"]) - costs
        state["net_realized"] = b["realized_gross"] - b["total_costs"]
        if isinstance(state.get("equity"), (int, float)):
            state["equity"] -= delta
        state["logs"] = [ln for ln in state.get("logs", []) if day.isoformat() not in str(ln)[:40]]
        paper._write_json(slot.state, state)
        _shift_equity(slot.equity, day, delta)
    n = len(same_day)
    return f"removed {n} trade{'s' * (n != 1)}, P&L {delta:+,.2f} given back" if n or day_fills else "removed flat equity points"


def _has_points(slot, day: date) -> bool:
    try:
        with open(slot.equity, encoding="utf-8") as f:
            return any(line.startswith(day.isoformat()) for line in f)
    except OSError:
        return False


def _shift_equity(path: Path, day: date, delta: float) -> None:
    """Drop the day's points; later points lose what the day added."""
    if not path.exists():
        return
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        return
    head, out = rows[0], [rows[0]]
    i = head.index("equity") if "equity" in head else 1
    for r in rows[1:]:
        d = _day(r[0]) if r else None
        if d == day:
            continue
        if d and d > day and delta:
            try:
                r[i] = f"{float(r[i]) - delta:.2f}"
            except (ValueError, IndexError):
                pass
        out.append(r)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(out)
    tmp.replace(path)


def scrub_prices(day: date, data: Path = DATA, dry_run: bool = False) -> int:
    """Delete the rows stamped with `day` from the recorded option and index prices. Returns rows removed."""
    removed = 0
    key = day.isoformat()
    for folder in (data / "options", data / "spot"):
        if not folder.exists():
            continue
        since = datetime.combine(day, datetime.min.time()).timestamp() - 86400
        for path in folder.rglob("*.csv"):
            try:
                if path.stat().st_mtime < since:      # untouched since before that day: nothing recorded then
                    continue
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            except (OSError, UnicodeDecodeError):
                continue
            keep = [ln for i, ln in enumerate(lines) if i == 0 or not ln.startswith(key)]
            if len(keep) != len(lines):
                removed += len(lines) - len(keep)
                if not dry_run:
                    if len(keep) <= 1 and path.parent.name == key:
                        path.unlink()
                    else:
                        path.write_text("".join(keep), encoding="utf-8")
    return removed


def scrub_day(day: date, log=print, dry_run: bool = False) -> None:
    if not dry_run and paper.SESSIONS.exists():
        bk = RUNTIME / "backups"
        bk.mkdir(parents=True, exist_ok=True)
        with tarfile.open(bk / f"sessions-before-scrub-{day}.tgz", "w:gz") as tar:
            tar.add(paper.SESSIONS, arcname="sessions")
    log(f"Holiday {day}:")
    for sid in paper.sessions():
        log(f"  {sid}: {scrub_session(paper.Slot(sid), day, dry_run)}")
    log(f"  recorded prices: {scrub_prices(day, dry_run=dry_run)} rows removed")


def holidays_to_scrub(cfg: dict, today: date | None = None) -> list[date]:
    today = today or ist_now().date()
    days = sorted({_day(h) for h in cfg["market"].get("holidays") or []} - {None})
    return [d for d in days if today - timedelta(days=30) <= d < today and d.weekday() < 5
            and not (RUNTIME / f"scrubbed-{d}").exists()]


def run(cfg: dict, day: str | None = None, dry_run: bool = False, log=print) -> None:
    if day:
        d = _day(day)
        holidays = {_day(h) for h in cfg["market"].get("holidays") or []}
        if d not in holidays:
            raise SystemExit(f"{day} is not an NSE holiday in config.yaml; nothing changed.")
        days = [d]
    else:
        days = holidays_to_scrub(cfg)
    if not days:
        log("No holiday to clean.")
    for d in days:
        scrub_day(d, log, dry_run)
        if not dry_run:
            RUNTIME.mkdir(exist_ok=True)
            (RUNTIME / f"scrubbed-{d}").write_text(ist_now().isoformat())


def restore(dry_run: bool = False, log=print) -> None:
    """Put runtime/sessions back to the earliest copy saved before a clean-up (once; marker in runtime/).
    The current sessions are kept next to the backups as sessions-after-scrub-<time>.tgz."""
    marker = RUNTIME / "restored-before-scrub"
    bk = RUNTIME / "backups"
    copies = sorted(bk.glob("sessions-before-scrub-*.tgz"), key=lambda p: p.stat().st_mtime) if bk.exists() else []
    if marker.exists() or not copies:
        log("Nothing to restore.")
        return
    src = copies[0]
    log(f"Will restore {src.name} (saved {datetime.fromtimestamp(src.stat().st_mtime):%d %b %H:%M})")
    if dry_run:
        return
    stamp = ist_now().strftime("%Y%m%d-%H%M%S")
    if paper.SESSIONS.exists():
        with tarfile.open(bk / f"sessions-after-scrub-{stamp}.tgz", "w:gz") as tar:
            tar.add(paper.SESSIONS, arcname="sessions")
        old = paper.SESSIONS.with_name(f"sessions-replaced-{stamp}")
        paper.SESSIONS.rename(old)
    with tarfile.open(src) as tar:
        tar.extractall(paper.SESSIONS.parent)
    if 'old' in locals():
        import shutil
        shutil.rmtree(old, ignore_errors=True)
    marker.write_text(f"{src.name} {ist_now().isoformat()}")
    log(f"Restored {len(paper.sessions())} sessions.")


# The four cycles Hamid approved removing on 21 Oct 2026: opened and closed on the 20 Oct holiday.
HOLIDAY_CYCLES = {
    "intraday_straddle-2": "004 | 20 Oct 26",
    "intraday_strangle": "005 | 20 Oct 26",
    "intraday_strangle-2": "002 | 20 Oct 26",
    "straddle_trend_switch-2": "006 | 20 Oct 26",
}


def remove_cycle(slot: "paper.Slot", group: str, day: date, dry_run: bool = False) -> str:
    """Remove one closed cycle whose trades and fills are all on `day`; give back its P&L and drop
    that day's equity points (later points move by the same amount). Anything else: left alone."""
    state = paper._read_json(slot.state)
    if not state or "broker" not in state:
        return "skipped: no saved account"
    b = state["broker"]
    trades = [t for t in b.get("trades", []) if t.get("group") == group]
    fills = [f for f in b.get("fills", []) if f.get("group") == group]
    if not trades:
        return "skipped: cycle not found (already removed?)"
    if any(_day(t["entry_time"]) != day or _day(t["exit_time"]) != day for t in trades) \
            or any(_day(f["time"]) != day for f in fills) \
            or any(p.get("group") == group for p in b.get("positions", [])):
        return "skipped: the cycle is not entirely on that day"
    others = [f for f in b.get("fills", []) if _day(f["time"]) == day and f.get("group") != group]
    if others:
        return "skipped: other trading on that day"
    gross = sum(float(t["gross_pnl"]) for t in trades)
    costs = sum(float(f["costs"]) for f in fills)
    delta = gross - costs
    if not dry_run:
        b["trades"] = [t for t in b["trades"] if t.get("group") != group]
        b["fills"] = [f for f in b["fills"] if f.get("group") != group]
        b["realized_gross"] = float(b["realized_gross"]) - gross
        b["total_costs"] = float(b["total_costs"]) - costs
        state["net_realized"] = b["realized_gross"] - b["total_costs"]
        if isinstance(state.get("equity"), (int, float)):
            state["equity"] -= delta
        paper._write_json(slot.state, state)
        _shift_equity(slot.equity, day, delta)
    return f"removed {group} ({len(trades)} legs, net {delta:+,.2f} given back)"


def remove_holiday_cycles(dry_run: bool = False, log=print, cycles: dict = HOLIDAY_CYCLES,
                          day: date = date(2026, 10, 20)) -> None:
    """Once (marker in runtime/): back up the sessions, then remove the approved cycles."""
    marker = RUNTIME / "removed-holiday-cycles-2026-10-20"
    if marker.exists():
        log("Nothing to remove.")
        return
    log(f"Will remove {len(cycles)} cycles from {day}:")
    for sid, group in cycles.items():
        log(f"  {sid}: {remove_cycle(paper.Slot(sid), group, day, dry_run=True)}")
    if dry_run:
        return
    bk = RUNTIME / "backups"
    bk.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bk / f"sessions-before-cycle-removal-{ist_now():%Y%m%d-%H%M%S}.tgz", "w:gz") as tar:
        tar.add(paper.SESSIONS, arcname="sessions")
    for sid, group in cycles.items():
        log(f"  {sid}: {remove_cycle(paper.Slot(sid), group, day)}")
    marker.write_text(ist_now().isoformat())
