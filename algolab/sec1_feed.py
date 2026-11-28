"""Backtests stepped every second on the 1-second bars recorded from Zerodha's stream (algolab/sec1.py).

This replays a day the way the paper engine lived it: strategies are stepped once a second from 09:15:00 to 15:30:00
inclusive (paper's `stream_step_seconds: 1`, stepping while the market is open up to and including 15:30), every
moment counts as a finished bar (as on the live feed), and orders fill at the best bid / ask recorded for that
second (as paper fills at Zerodha's best bid / ask). Legs expiring that day settle at 15:30 that day, as in paper.

  backtest(cfg, strategy, SEC1_DATASET, start, end)      # engine.backtest picks this feed for that name

Prices come from data/sec1/<day>/:
  NIFTY.csv, INDIAVIX.csv                 index and VIX
  <expiry>_<strike><CE|PE>.csv            options: last, bid, ask; day_open / day_high as the exchange reports them

What is known when: a row stamped second s holds the ticks of s.000–s.999, so at moment s the feed uses rows up to
s − 1 only (a price's time is when it was known, as for the 1-minute data).

When a contract has no 1-second price yet today, a decision uses today's 1-minute price, then its last price on an
earlier stored day (labelled "previous_day", never counted as a real price), then the model. An order never fills on
an earlier day's price: it waits for the contract's first tick of the day (up to 15 minutes), else today's 1-minute
price, else the model. Every fill's price source is counted in the result.

Days: a day without NIFTY.csv, or a partial day (recording ended before 15:25 or stopped for more than 10 minutes),
is left out and named in the result, as a holiday would be. If an expiry day is left out, legs expiring on it settle
at that day's close from the 1-minute index set (NIFTY_minute), and the result says so.
"""
from __future__ import annotations

import bisect
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .feeds import Feed, OptionStore, weekly_expiries
from .models import Contract, Quote

SEC1_DATASET = "NIFTY_1second"
OPEN_S, CLOSE_S = 9 * 3600 + 15 * 60, 15 * 3600 + 30 * 60      # 09:15:00 .. 15:30:00 inclusive (seconds of the day)
FULL_DAY_UNTIL = 15 * 3600 + 25 * 60                            # a day whose index rows end earlier is partial
MAX_GAP_S = 600                                                 # ...or that has no index row for this long
WAIT_FOR_TICK_S = 15 * 60                                       # an order waits this long for a first tick today


def _root() -> Path:
    from . import sec1
    return sec1.ROOT


def sec1_days(start=None, end=None, root: Path | None = None) -> list[date]:
    """Stored days with the index file, inside [start, end]."""
    from .config import to_date
    root = root or _root()
    s = to_date(start) if start is not None else None
    e = to_date(end) if end is not None else None
    out = []
    if root.is_dir():
        for p in sorted(root.iterdir()):
            try:
                d = date.fromisoformat(p.name)
            except ValueError:
                continue
            if (s is None or d >= s) and (e is None or d <= e) and (p / "NIFTY.csv").exists():
                out.append(d)
    return out


def _read(path: Path):
    """(seconds of the day, frame) of one 1-second file, one row per second. A second written twice (a late packet)
    is merged: first open, highest high, lowest low, last close / bid / ask / volume / OI / day open-high, ticks added."""
    try:
        df = pd.read_csv(path)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return None
    if df.empty or "time" not in df or "close" not in df:
        return None
    t = pd.to_datetime(df["time"], errors="coerce")
    df = df.assign(_s=(t.dt.hour * 3600 + t.dt.minute * 60 + t.dt.second)).dropna(subset=["_s", "close"])
    if df.empty:
        return None
    df["_s"] = df["_s"].astype(np.int64)
    for c in ("open", "high", "low"):
        if c not in df:
            df[c] = df["close"]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    for c in ("bid", "ask", "volume", "oi", "day_open", "day_high"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce")
            agg[c] = "last"
    if "ticks" in df:
        agg["ticks"] = "sum"
    df = df.sort_values("_s", kind="stable").groupby("_s", sort=True).agg(agg).reset_index()
    return df["_s"].to_numpy(np.int64), df


def _first_second(path: Path, rows: int = 20):
    """Second of the day of a file's first row (the earliest of its first few rows: a late packet can be written a
    second late), read from the top of the file only, or None. Enough to know whether a strike has traded yet
    without loading the whole day (a day is ~300 files, ~320 MB)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = f.readline().strip().split(",")
            if "time" not in head:
                return None
            k, best = head.index("time"), None
            for _ in range(rows):
                line = f.readline()
                if not line:
                    break
                t = line.split(",")[k].strip()
                try:
                    hh, mm, ss = t[-8:].split(":")
                    v = int(hh) * 3600 + int(mm) * 60 + int(float(ss))
                except ValueError:
                    continue
                best = v if best is None else min(best, v)
            return best
    except OSError:
        return None


def _col(df: pd.DataFrame, name: str) -> np.ndarray:
    return df[name].to_numpy(float) if name in df else np.full(len(df), np.nan)


class _Series:
    """One instrument on one day."""

    def __init__(self, secs: np.ndarray, df: pd.DataFrame):
        self.secs = secs
        self.last = df["close"].to_numpy(float)
        self.bid, self.ask = _col(df, "bid"), _col(df, "ask")
        rec_high = np.fmax.accumulate(np.nan_to_num(df["high"].to_numpy(float), nan=-np.inf))
        ex_open, ex_high = _col(df, "day_open"), _col(df, "day_high")
        first_open = float(df["open"].iloc[0])
        # the exchange's day open / high when the packets carried them (a strike first recorded mid-day still shows
        # its real open), else what was recorded
        self.day_open = np.where(ex_open > 0, ex_open, first_open)
        self.day_high = np.fmax(np.where(ex_high > 0, ex_high, -np.inf), rec_high)

    def known(self, s: int):
        """Index of the last row known at moment s (rows of seconds before s), or None."""
        i = bisect.bisect_left(self.secs, s) - 1
        return i if i >= 0 else None

    def next_row(self, s: int):
        """Index of the first row from second s on, or None."""
        i = bisect.bisect_left(self.secs, s)
        return i if i < len(self.secs) else None


def _num(x):
    return float(x) if x is not None and x == x and x > 0 else None


def day_problem(spot: _Series | None) -> str | None:
    """Why a recorded day cannot be replayed as a whole day, or None."""
    if spot is None or not len(spot.secs):
        return "no index bars"
    secs = spot.secs[(spot.secs >= OPEN_S) & (spot.secs <= CLOSE_S)]
    if not len(secs) or secs[-1] < FULL_DAY_UNTIL:
        last = int(secs[-1]) if len(secs) else None
        return f"recording ended at {last // 3600:02d}:{last % 3600 // 60:02d}" if last is not None else "no index bars in hours"
    edges = np.concatenate(([OPEN_S], secs))
    gaps = np.diff(edges)
    k = int(np.argmax(gaps))
    if gaps[k] > MAX_GAP_S:
        a = int(edges[k])
        return f"no index bars for {gaps[k] // 60} min after {a // 3600:02d}:{a % 3600 // 60:02d}"
    return None


class Sec1Feed(Feed):
    gives_day_ohlc = True

    def __init__(self, cfg: dict, days: list[date], root: Path | None = None, store: OptionStore | None = None,
                 index_close=None, progress=None):
        super().__init__(cfg)
        self._progress = progress                  # callable(day number, days): the job's "day N of M"
        self._first: dict = {}                     # (day, name) -> first second with a row (listed_strikes)
        self.root = root or _root()
        self.store = store
        self._index_close = index_close           # callable(day) -> close from the 1-minute set, for missing days
        self._series: dict = {}
        self._listing: dict = {}
        self.price_stats = {"sec1": 0, "minute": 0, "previous_day": 0, "model": 0}
        self.fill_stats = {"sec1": 0, "waited_for_tick": 0, "minute": 0, "model": 0}
        self.missing: list = []
        self.skipped_days: list = []               # "YYYY-MM-DD (why)"
        self.late_settled: list = []
        self.spot, self.vix, self.now = float("nan"), None, None
        self._cache: dict = {}
        self._sec = 0
        self.days = []
        for d in days:                              # partial days are left out like missing ones
            why = day_problem(self._get(d, "NIFTY"))
            if why:
                self.skipped_days.append(f"{d} ({why})")
            else:
                self.days.append(d)
        self._day_set = set(self.days)

    # ---- files ------------------------------------------------------------------------------
    def _get(self, day: date, name: str):
        key = (day, name)
        if key not in self._series:
            r = _read(self.root / f"{day:%Y-%m-%d}" / f"{name}.csv")
            self._series[key] = _Series(*r) if r else None
        return self._series[key]

    def _names(self, day: date) -> dict:
        if day not in self._listing:
            out: dict = {}
            folder = self.root / f"{day:%Y-%m-%d}"
            for p in folder.glob("*.csv") if folder.is_dir() else []:
                try:
                    e, rest = p.stem.split("_")
                    out.setdefault((date.fromisoformat(e), rest[-2:]), set()).add(int(rest[:-2]))
                except ValueError:
                    continue
            self._listing[day] = out
        return self._listing[day]

    @staticmethod
    def _name(c: Contract) -> str:
        return f"{c.expiry:%Y-%m-%d}_{int(c.strike)}{c.right}"

    # ---- stepping ---------------------------------------------------------------------------
    def _first_sec(self, day: date, name: str):
        key = (day, name)
        if key not in self._first:
            s = self._series.get(key)
            self._first[key] = (int(s.secs[0]) if s is not None and len(s.secs) else None) if key in self._series \
                else _first_second(self.root / f"{day:%Y-%m-%d}" / f"{name}.csv")
        return self._first[key]

    def __iter__(self):
        for n, day in enumerate(self.days, 1):
            if self._progress:
                try:
                    self._progress(n, len(self.days))
                except Exception:  # noqa: BLE001 - a progress note never stops the backtest
                    pass
            spot, vix = self._get(day, "NIFTY"), self._get(day, "INDIAVIX")
            if self.store is not None:
                self.store.forget_expired(day)
            base = datetime.combine(day, time(0))
            for s in range(OPEN_S, CLOSE_S + 1):
                i = spot.known(s)
                if i is None:
                    continue                       # the index has not printed yet today
                self._sec = s
                self.now = base + timedelta(seconds=s)
                self.spot = float(spot.last[i])
                if vix is not None:
                    j = vix.known(s)
                    self.vix = float(vix.last[j]) if j is not None else self.vix
                self._cache = {}
                yield self.now
            self._series = {k: v for k, v in self._series.items() if k[0] >= day}   # free earlier days
            self._first = {k: v for k, v in self._first.items() if k[0] >= day}

    def settle_spot(self, expiry: date):
        """The index close for legs expiring on a day that was not replayed (Stepper's late settlement)."""
        px = self._index_close(expiry) if self._index_close else None
        self.late_settled.append(f"{expiry} at " + (f"{px:,.2f} (1-minute close)" if px else "the last price seen (no close found)"))
        return px

    # ---- market facts -----------------------------------------------------------------------
    def is_trading_day(self, d: date) -> bool:
        if self.days and self.days[0] <= d <= self.days[-1]:
            return d in self._day_set
        return super().is_trading_day(d)

    def expiries(self) -> list[date]:
        have = sorted({e for e, _ in self._names(self.now.date()) if e >= self.now.date()})
        if have:
            return have
        return weekly_expiries(self.now.date(), self.now.date() + timedelta(days=45), self.cfg["market"]["expiry_rules"])

    def listed_strikes(self, expiry: date, right: str) -> set | None:
        """Strikes recorded that day that have traded by now (a strike not yet traded was not on the screen).
        An empty set (nothing traded yet) means no strike can be picked yet, not 'any strike'."""
        day = self.now.date()
        ks = self._names(day).get((expiry, right)) or set()
        seen = set()
        for k in ks:                       # top of each file only: whole files are loaded for strikes quoted
            f = self._first_sec(day, f"{expiry:%Y-%m-%d}_{int(k)}{right}")
            if f is not None and f < self._sec:          # a row of an earlier second: known now (as _Series.known)
                seen.add(k)
        return seen if ks else None

    # ---- prices -----------------------------------------------------------------------------
    def quote(self, c: Contract) -> Quote:
        q = self._cache.get(c)
        if q is None:
            q = self._quote(c)
            self._cache[c] = q
        return q

    def _today(self, c: Contract, i: int, s: _Series) -> Quote:
        return Quote(float(s.last[i]), _num(s.bid[i]), _num(s.ask[i]), "market",
                     float(s.day_open[i]), float(max(s.day_open[i], s.day_high[i])))

    def _quote(self, c: Contract) -> Quote:
        day = self.now.date()
        s = self._get(day, self._name(c))
        i = s.known(self._sec) if s is not None else None
        if i is not None:
            self.price_stats["sec1"] += 1
            return self._today(c, i, s)
        px = self.store.price(c, self.now, stale_ok=True) if self.store is not None else None
        if px is not None:                                                  # today's 1-minute price
            self.price_stats["minute"] += 1
            return Quote(float(px), None, None, "market")
        for d in reversed([x for x in self.days if x < day]):               # yesterday's last price: a guide only
            p = self._get(d, self._name(c))
            if p is not None and len(p.last):
                self.price_stats["previous_day"] += 1
                return Quote(float(p.last[-1]), None, None, "previous_day")
        self.price_stats["model"] += 1
        self._note(c)
        return self.model_quote(c)

    def fill_quote(self, c: Contract) -> Quote:
        """The price an order gets: that second's bid / ask; before the contract's first tick today, the first tick
        within 15 minutes (the order waits), else today's 1-minute price, else the model. Never an earlier day's."""
        day = self.now.date()
        s = self._get(day, self._name(c))
        if s is not None:
            i = s.known(self._sec)
            if i is not None:
                self.fill_stats["sec1"] += 1
                return self._today(c, i, s)
            j = s.next_row(self._sec)
            if j is not None and s.secs[j] - self._sec <= WAIT_FOR_TICK_S:
                self.fill_stats["waited_for_tick"] += 1
                return self._today(c, j, s)
        px = self.store.price(c, self.now, stale_ok=True) if self.store is not None else None
        if px is not None:
            self.fill_stats["minute"] += 1
            return Quote(float(px), None, None, "market")
        self.fill_stats["model"] += 1
        self._note(c)
        return self.model_quote(c)

    def _note(self, c: Contract) -> None:
        if len(self.missing) < 50:
            tag = f"{self.now:%Y-%m-%d %H:%M} {c.label}"
            if not self.missing or self.missing[-1] != tag:
                self.missing.append(tag)


def index_close_from_minute_set(day: date):
    """NIFTY's last 1-minute close at or before 15:30 on `day` (data/spot/NIFTY_minute.csv), or None."""
    from .config import SPOT_DIR
    path = SPOT_DIR / "NIFTY_minute.csv"
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, usecols=["datetime", "close"], parse_dates=["datetime"])
    except (OSError, ValueError):
        return None
    w = df[(df["datetime"].dt.date == day) & (df["datetime"].dt.time < time(15, 30))]
    return float(w["close"].iloc[-1]) if len(w) else None


def price_metrics(res, feed: Sec1Feed) -> None:
    fs = feed.fill_stats
    fills = sum(fs.values())
    res.metrics["Price data"] = "1-second bars recorded from Zerodha's stream"
    if fills:
        res.metrics["Fills on 1-second prices"] = f"{(fs['sec1'] + fs['waited_for_tick']) / fills * 100:.0f}%"
        other = {k: v for k, v in fs.items() if k != "sec1" and v}
        if other:
            label = {"waited_for_tick": "waited for first tick", "minute": "1-minute", "model": "model"}
            res.metrics["Fills not at that second's price"] = " · ".join(f"{label[k]} {v}" for k, v in other.items())
    ps = feed.price_stats
    if ps["minute"] or ps["previous_day"] or ps["model"]:
        res.metrics["Price lookups not from today's 1-second bars"] = (
            f"1-minute {ps['minute']:,} · earlier day {ps['previous_day']:,} · model {ps['model']:,}")
    if feed.skipped_days:
        res.metrics["Recorded days left out (partial)"] = "; ".join(feed.skipped_days)
    if feed.late_settled:
        res.metrics["Expiries settled on a day not replayed"] = "; ".join(feed.late_settled)
    if feed.missing:
        res.logs.append("Prices not in the 1-second bars (model used): " + "; ".join(feed.missing[:20]))
