"""1-second bars recorded from Zerodha's live price stream, for paper-vs-backtest checks at 1-second steps.

Zerodha keeps no history finer than 1 minute, so these bars exist only for days the server recorded them:
a day it missed (engine stopped, no Zerodha login, stream down) cannot be downloaded later.

Every tick the stream receives (stream.KiteStream `on_ticks`) is folded into a bar for its second:

  data/sec1/<YYYY-MM-DD>/NIFTY.csv, INDIAVIX.csv, <expiry>_<strike><CE|PE>.csv
  columns: time (IST, start of the second), open, high, low, close (last traded price),
           bid, ask (best, at the end of the second), volume (day's traded quantity so far), oi, ticks,
           day_open, day_high (the exchange's open and high so far today, as the live feed reports them)

A second with no tick has no row (the price did not change). The time is Zerodha's exchange timestamp
when the packet carries one, else the moment it was received.

What is recorded (config `sec1:`): NIFTY, India VIX, calls and puts `strikes_each_side` around the money
on the `expiries` nearest weekly expiries (the band follows the index), and every option a session uses.

Days are kept for `keep_trading_days` NSE trading days, then deleted (`prune`, run after the close by
`run.py collect` and by `run.py sec1 prune`).

The recorder runs inside the paper engine. It must never disturb trading: every call is guarded, and a
fault only switches the recorder off for the day with a note in runtime/sec1_status.json.
"""
from __future__ import annotations

import json
import shutil
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import DATA, IST, RUNTIME

ROOT = DATA / "sec1"
STATUS = RUNTIME / "sec1_status.json"
COLUMNS = ["time", "open", "high", "low", "close", "bid", "ask", "volume", "oi", "ticks", "day_open", "day_high"]
DEFAULTS = {"record": True, "strikes_each_side": 30, "expiries": 2, "keep_trading_days": 5}


OVERRIDE = RUNTIME / "sec1_settings.json"     # set from the dashboard (Backtests › Compare with paper), wins over config


def settings(cfg: dict) -> dict:
    over = {}
    try:
        over = json.loads(OVERRIDE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        pass
    s = {**DEFAULTS, **(cfg.get("sec1") or {}), **{k: v for k, v in over.items() if k in DEFAULTS}}
    s["keep_trading_days"] = max(1, int(s.get("keep_trading_days") or 1))
    s["strikes_each_side"] = max(0, int(s.get("strikes_each_side") or 0))
    s["expiries"] = max(1, int(s.get("expiries") or 1))
    s["record"] = bool(s.get("record"))
    return s


def contract_name(expiry: date, strike: int, right: str) -> str:
    return f"{expiry:%Y-%m-%d}_{int(strike)}{right}"


def _epoch(ts) -> float | None:
    """Epoch seconds from the stream's timestamp (a naive local datetime made by datetime.fromtimestamp)."""
    if ts is None:
        return None
    try:
        return ts.timestamp()
    except (AttributeError, OverflowError, OSError, ValueError):
        return None


class SecondRecorder:
    """Folds ticks into 1-second bars and appends finished seconds to the day's files.

    on_ticks() runs on the stream's thread; flush() on the engine's thread (each step). `names` maps an
    instrument token to its file name; tokens without a name are ignored."""

    FLUSH_EVERY = 15.0     # seconds between writes to disk
    SETTLE = 2             # a second is finished this many seconds after it ends (late packets)

    def __init__(self, root: Path | None = None, clock=None):
        import time as _t
        self.root = Path(root) if root is not None else ROOT
        self.clock = clock or _t.time
        self.names: dict[int, str] = {}
        self._open: dict[tuple, dict] = {}       # (token, epoch second) -> bar
        self._lock = threading.Lock()
        self._last_write = 0.0
        self.enabled = True
        self.error = ""
        self.rows_today = 0
        self.day = None
        self.files: set = set()

    # ---- stream thread ----------------------------------------------------------------------
    def on_ticks(self, ticks: list) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                for t in ticks:
                    tok = t.get("token")
                    if tok not in self.names:
                        continue
                    last = t.get("last")
                    if not last:
                        continue
                    sec = int(_epoch(t.get("ts")) or t.get("rx") or self.clock())
                    key = (tok, sec)
                    b = self._open.get(key)
                    if b is None:
                        b = {"open": last, "high": last, "low": last, "close": last, "ticks": 0}
                        self._open[key] = b
                    else:
                        b["high"] = max(b["high"], last)
                        b["low"] = min(b["low"], last)
                        b["close"] = last
                    b["ticks"] += 1
                    for k in ("bid", "ask", "volume", "oi"):
                        if t.get(k) is not None:
                            b[k] = t[k]
                    for src, k in (("open", "day_open"), ("high", "day_high")):   # the exchange's own day open / high
                        if t.get(src):
                            b[k] = t[src]
        except Exception as e:  # noqa: BLE001 - never let recording disturb the stream
            self._fail(e)

    # ---- engine thread ----------------------------------------------------------------------
    def set_names(self, names: dict) -> None:
        with self._lock:
            self.names = {int(k): v for k, v in names.items() if k and v}

    def flush(self, force: bool = False) -> int:
        """Write finished seconds to disk (at most every FLUSH_EVERY seconds unless forced). Rows written."""
        if not self.enabled and not force:
            return 0
        now = self.clock()
        if not force and now - self._last_write < self.FLUSH_EVERY:
            return 0
        self._last_write = now
        try:
            cutoff = int(now) - self.SETTLE
            with self._lock:
                done = [k for k in self._open if force or k[1] < cutoff]
                bars = [(k, self._open.pop(k)) for k in done]
                names = dict(self.names)
            return self._write(bars, names)
        except Exception as e:  # noqa: BLE001
            self._fail(e)
            return 0

    def _write(self, bars, names) -> int:
        if not bars:
            return 0
        by_file: dict[tuple, list] = {}
        for (tok, sec), b in sorted(bars, key=lambda kb: kb[0][1]):
            name = names.get(tok)
            if not name:
                continue
            t = datetime.fromtimestamp(sec, IST).replace(tzinfo=None)
            row = [t.isoformat(sep=" "), b["open"], b["high"], b["low"], b["close"], b.get("bid", ""),
                   b.get("ask", ""), b.get("volume", ""), b.get("oi", ""), b["ticks"], b.get("day_open", ""), b.get("day_high", "")]
            by_file.setdefault((t.date(), name), []).append(row)
        n = 0
        for (day, name), rows in by_file.items():
            folder = self.root / f"{day:%Y-%m-%d}"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{name}.csv"
            new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if new:
                    f.write(",".join(COLUMNS) + "\n")
                for r in rows:
                    f.write(",".join("" if v is None else str(v) for v in r) + "\n")
            if self.day != day:
                self.day, self.rows_today, self.files = day, 0, set()
            self.files.add(name)
            self.rows_today += len(rows)
            n += len(rows)
        self._status()
        return n

    def _fail(self, e: Exception) -> None:
        self.enabled = False
        self.error = f"{type(e).__name__}: {e}"[:300]
        self._status()

    def _status(self) -> None:
        try:
            RUNTIME.mkdir(parents=True, exist_ok=True)
            out = {"time": datetime.now(IST).isoformat(timespec="seconds"), "enabled": self.enabled,
                   "error": self.error, "day": str(self.day) if self.day else None, "rows_today": self.rows_today,
                   "instruments_today": len(self.files), "instruments_named": len(self.names)}
            tmp = STATUS.with_suffix(".tmp")
            tmp.write_text(json.dumps(out), encoding="utf-8")
            tmp.replace(STATUS)
        except OSError:
            pass


# ---- stored days ---------------------------------------------------------------------------
def stored_days(root: Path = ROOT) -> list[date]:
    out = []
    if root.is_dir():
        for p in root.iterdir():
            try:
                out.append(date.fromisoformat(p.name))
            except ValueError:
                continue
    return sorted(out)


def keep_from(today: date, keep: int, is_trading_day) -> date:
    """The oldest day kept: the `keep`-th most recent trading day up to and including today."""
    d, seen = today, 0
    for _ in range(keep * 3 + 30):
        if is_trading_day(d):
            seen += 1
            if seen >= keep:
                return d
        d -= timedelta(days=1)
    return d


def _dir_bytes(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def prune(cfg: dict, today: date | None = None, root: Path = ROOT, dry_run: bool = False) -> dict:
    """Delete stored days older than the last `keep_trading_days` trading days."""
    from .feeds import Feed
    s = settings(cfg)
    today = today or datetime.now(IST).date()
    feed = Feed(cfg)
    first = keep_from(today, s["keep_trading_days"], feed.is_trading_day)
    removed = []
    for d in stored_days(root):
        if d < first:
            removed.append(str(d))
            if not dry_run:
                shutil.rmtree(root / f"{d:%Y-%m-%d}", ignore_errors=True)
    return {"keep_trading_days": s["keep_trading_days"], "keep_from": str(first), "removed": removed,
            **status(root)}


def status(root: Path = ROOT) -> dict:
    days = []
    for d in stored_days(root):
        p = root / f"{d:%Y-%m-%d}"
        files = list(p.glob("*.csv"))
        days.append({"day": str(d), "instruments": len(files), "mb": round(_dir_bytes(p) / 1e6, 1)})
    free = None
    try:
        free = round(shutil.disk_usage(root if root.exists() else root.parent).free / 1e9, 1)
    except OSError:
        pass
    return {"days": days, "total_mb": round(sum(x["mb"] for x in days), 1), "disk_free_gb": free}
