"""Market data: stored history, recorded option prices, modelled option prices and sample data."""
from __future__ import annotations

import bisect
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import OPTION_DIR, SPOT_DIR, as_of, to_date
from .models import Contract, Quote
from .pricing import bs_price, year_fraction

MARKET_OPEN, MARKET_CLOSE = time(9, 15), time(15, 30)


# ---- expiry calendar ---------------------------------------------------
def weekly_expiries(start: date, end: date, rules: list, trading_days: set | None = None) -> list[date]:
    """Weekly expiry dates. A holiday expiry moves to the previous trading day (known days only)."""
    parsed = sorted((to_date(r["from"]), int(r["weekday"])) for r in rules)
    known = (min(trading_days), max(trading_days)) if trading_days else None
    out = set()
    d = start
    while d <= end:
        weekday = [w for f, w in parsed if f <= d][-1] if parsed[0][0] <= d else parsed[0][1]
        if d.weekday() == weekday:
            e = d
            if known:
                for _ in range(6):
                    if not (known[0] <= e <= known[1]) or e in trading_days:
                        break
                    e -= timedelta(days=1)
            out.add(e)
        d += timedelta(days=1)
    return sorted(out)


# ---- stored data -------------------------------------------------------
def list_datasets() -> list[str]:
    """Every price set in data/spot: the index 1-minute sets first (datasets.screen_datasets, what the screens
    offer), then any other file (the 5-minute bars, older copies), samples last."""
    from .datasets import screen_datasets
    names = sorted(p.stem for p in SPOT_DIR.glob("*.csv"))
    main = [n for n in screen_datasets(SPOT_DIR) if n in names]
    rest = [n for n in names if n not in main]
    return main + [n for n in rest if not n.startswith("SAMPLE")] + [n for n in rest if n.startswith("SAMPLE")]


def _span(name: str):
    """First and last date in a spot file, read from its ends only."""
    path = SPOT_DIR / f"{name}.csv"
    with path.open("rb") as f:
        f.readline()
        first = f.readline().decode()[:10]
        f.seek(max(0, path.stat().st_size - 4096))
        last = f.read().decode("utf-8", "replace").strip().splitlines()[-1][:10]
    return date.fromisoformat(first), date.fromisoformat(last)


def preferred_dataset(start=None, end=None) -> str:
    """The data set backtests should use for a date range: the 1-minute NIFTY bars when they cover it (stops and
    rolls are then checked every minute, as paper does), else the 5-minute bars."""
    names = list_datasets()
    fallback = "NIFTY_5minute" if "NIFTY_5minute" in names else (names[0] if names else "NIFTY_5minute")
    if "NIFTY_minute" not in names:
        return fallback
    try:
        first, last = _span("NIFTY_minute")
        _, last5 = _span(fallback) if fallback in names else (None, last)
    except (OSError, ValueError, IndexError):
        return fallback
    s = to_date(start) if start is not None else None
    e = to_date(end) if end is not None else None
    if (s is None or first <= s) and last >= min(e or last5, last5):
        return "NIFTY_minute"
    return fallback


def load_dataset(name: str, start=None, end=None) -> pd.DataFrame:
    """Load spot bars. Index is the moment each bar's close became known (bar start + interval).
    Keeps open/high/low when the file has them, so stops can be checked against the bar's range."""
    df = pd.read_csv(SPOT_DIR / f"{name}.csv", parse_dates=["datetime"]).sort_values("datetime")
    df = df.drop_duplicates("datetime", keep="last")
    if "vix" not in df:
        df["vix"] = np.nan
    idx = pd.DatetimeIndex(df["datetime"])
    gaps = pd.Series(idx).diff().dropna()
    step = gaps[gaps > pd.Timedelta(0)].min() if len(gaps) else pd.Timedelta(minutes=5)
    if step < pd.Timedelta(days=1):   # intraday bars: keep the session only (TrueData adds pre-open bars)
        t = df["datetime"].dt.time
        keep = ((t >= MARKET_OPEN) & (t < MARKET_CLOSE)).to_numpy()
        df, idx = df[keep], idx[keep]
    if step >= pd.Timedelta(days=1):
        idx = idx.normalize() + pd.Timedelta(hours=15, minutes=30)
    else:
        idx = idx + step
    df.index = idx
    df.index.name = "time"
    if start is not None:
        df = df[df.index >= pd.Timestamp(to_date(start))]
    if end is not None:
        df = df[df.index < pd.Timestamp(to_date(end)) + pd.Timedelta(days=1)]
    if df.empty:
        raise ValueError("No data in the selected date range.")
    cols = [c for c in ("open", "high", "low") if c in df] + ["close", "vix"]
    return df[cols]


class OptionStore:
    """Recorded option prices: data/options/<UNDERLYING>/<expiry>/<strike><CE|PE>.csv (time, last, bid, ask).
    `time` is when the price was observed. Used in preference to the pricing model when present.

    Zerodha 1-minute candles in data/options_1min/<UNDERLYING>/<expiry>/<strike><CE|PE>.csv (time = bar
    start, open, high, low, close; deploy/get-minute-data.command) are read alongside the recordings:
    each candle's close counts as observed at the end of its minute, and wins over a recording at the
    same moment."""

    MIN_CONTRACTS_PER_DAY = 20   # an expiry counts as priced on a day with at least this many contracts...
    MIN_PRICES_PER_DAY = 50      # ...each with at least this many stored prices that day (steady trading)

    def __init__(self, underlying: str, root: Path = OPTION_DIR, max_age_minutes: int = 20):
        self.root = root / underlying
        self.minute_root = (root.parent / "options_1min" / underlying) if root == OPTION_DIR else None
        self.max_age = pd.Timedelta(minutes=max_age_minutes)
        self._cache: dict[Contract, pd.Series | None] = {}
        self._days: set | None = None
        self._exp_days: dict = {}
        self.coverage_file = (root.parent / "options" / ".coverage.json") if root == OPTION_DIR else None
        # TrueData raw candles (OHLC, volume, OI): data/truedata/options/<UNDERLYING>/<expiry>/<strike><CE|PE>.csv.gz
        self.raw_root = (root.parent / "truedata" / "options" / underlying) if root == OPTION_DIR else None
        self._bars_cache: dict = {}
        self._range_cache: dict = {}

    def _roots(self):
        return [r for r in (self.root, self.minute_root) if r is not None and r.exists()]

    def forget_expired(self, day: date) -> int:
        """Lets go of the prices and candles held in memory for contracts that expired before `day` (read from disk
        again if ever asked for). A backtest then holds only the expiries still open, not every contract it touched
        since its first day, so a 2-year run needs about the memory of a few weeks. Returns how many were dropped."""
        dropped = 0
        for cache in (self._cache, self._bars_cache):
            old = [c for c in cache if c.expiry < day]
            for c in old:
                del cache[c]
            dropped += len(old)
        for key in [k for k in self._range_cache if k[0].expiry < day]:
            del self._range_cache[key]
        return dropped

    def _series(self, c: Contract):
        if c not in self._cache:
            parts = []
            p = self.path(c)
            if p.exists():
                parts.append(pd.read_csv(p, parse_dates=["time"]).set_index("time")["last"])
            if self.minute_root is not None:
                m = self.minute_root / c.expiry.isoformat() / f"{c.strike}{c.right}.csv"
                if m.exists():
                    d = pd.read_csv(m, usecols=["time", "close"], parse_dates=["time"])
                    parts.append(pd.Series(d["close"].to_numpy(float), index=d["time"] + pd.Timedelta(minutes=1)))
            if parts:
                s = pd.concat(parts).dropna()
                s = s[~s.index.duplicated(keep="last")].sort_index()
                self._cache[c] = s
            else:
                self._cache[c] = None
        return self._cache[c]

    def _file_days(self) -> dict:
        """{file: {expiry, days: [dates with enough prices]}} for every stored option file. Kept in
        data/options/.coverage.json so only new or changed files are read again."""
        import json
        cache = {}
        if self.coverage_file is not None and self.coverage_file.exists():
            try:
                cache = json.loads(self.coverage_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cache = {}
        out, changed = {}, False
        for r in self._roots():
            for f in r.glob("*/*.csv"):
                key = str(f.relative_to(r.parent.parent))
                try:
                    st = f.stat()
                    exp = date.fromisoformat(f.parent.name)
                except (OSError, ValueError):
                    continue
                stamp = [st.st_size, st.st_mtime_ns]
                hit = cache.get(key)
                if hit and hit.get("stamp") == stamp:
                    out[key] = hit
                    continue
                try:
                    t = pd.to_datetime(pd.read_csv(f, usecols=["time"])["time"])
                except (ValueError, KeyError, OSError):
                    continue
                per_day = t.dt.date.value_counts()
                out[key] = {"stamp": stamp, "expiry": exp.isoformat(),
                            "days": sorted(d.isoformat() for d in per_day[per_day >= self.MIN_PRICES_PER_DAY].index)}
                changed = True
        if self.coverage_file is not None and (changed or len(out) != len(cache)):
            try:
                self.coverage_file.parent.mkdir(parents=True, exist_ok=True)
                self.coverage_file.write_text(json.dumps(out), encoding="utf-8")
            except OSError:
                pass
        return out

    def real_days(self) -> set:
        """Trading days on which some expiry has real prices stored for enough of its contracts."""
        if self._days is None:
            counts: dict = {}
            for v in self._file_days().values():
                exp = date.fromisoformat(v["expiry"])
                for d in v["days"]:
                    k = (exp, date.fromisoformat(d))
                    counts[k] = counts.get(k, 0) + 1
            self._exp_days = {}
            for (exp, d), n in counts.items():
                if n >= self.MIN_CONTRACTS_PER_DAY:
                    self._exp_days.setdefault(exp, set()).add(d)
            self._days = set().union(*self._exp_days.values()) if self._exp_days else set()
        return self._days

    def strikes_on(self, expiry: date, right: str, day: date) -> set:
        """Strikes of `expiry` / `right` that traded steadily on `day` (real prices stored)."""
        if not hasattr(self, "_strike_days"):
            import re
            self._strike_days = {}
            for key, v in self._file_days().items():
                m = re.match(r"(\d+)(CE|PE)\.csv$", key.replace("\\", "/").rsplit("/", 1)[-1])
                if not m:
                    continue
                k = (date.fromisoformat(v["expiry"]), m.group(2))
                for d in v["days"]:
                    self._strike_days.setdefault((k[0], k[1], date.fromisoformat(d)), set()).add(int(m.group(1)))
        return self._strike_days.get((expiry, right, day), set())

    def expiries_on(self, day: date) -> list[date]:
        """Expiries (on or after `day`) with real prices stored on `day` for enough contracts."""
        self.real_days()
        return sorted(e for e, ds in self._exp_days.items() if e >= day and day in ds)

    def stored_expiries(self) -> list[date]:
        out = set()
        for r in self._roots():
            for d in r.iterdir():
                try:
                    out.add(date.fromisoformat(d.name))
                except ValueError:
                    continue
        return sorted(out)

    def path(self, c: Contract) -> Path:
        return self.root / c.expiry.isoformat() / f"{c.strike}{c.right}.csv"

    def price(self, c: Contract, now: datetime, stale_ok: bool = False) -> float | None:
        """Last real price observed at or before `now` (within max_age; with stale_ok, any earlier
        price from the same day)."""
        s = self._series(c)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(now)
        i = s.index.searchsorted(ts, side="right") - 1
        if i < 0:
            return None
        if stale_ok:
            if s.index[i].date() != ts.date():
                return None
        elif ts - s.index[i] > self.max_age:
            return None
        return float(s.iloc[i])

    def last_trade(self, c: Contract, now: datetime):
        """(price, its time) of the last real trade at or before `now` on the same day, or None."""
        s = self._series(c)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(now)
        i = s.index.searchsorted(ts, side="right") - 1
        if i < 0 or s.index[i].date() != ts.date():
            return None
        return float(s.iloc[i]), s.index[i]

    def bars(self, c: Contract):
        """TrueData 1-minute candles for a contract, indexed by when each was complete (bar start + its length),
        with open, high, low, close, volume (contracts); None when no raw copy is stored."""
        if c not in self._bars_cache:
            out = None
            if self.raw_root is not None:
                strike = f"{c.strike:g}" if isinstance(c.strike, (int, float)) else str(c.strike)
                p = self.raw_root / c.expiry.isoformat() / f"{strike}{c.right}.csv.gz"
                if p.exists():
                    try:
                        d = pd.read_csv(p, parse_dates=["timestamp"])
                        d = d.drop_duplicates("timestamp").sort_values("timestamp")
                        gaps = d["timestamp"].diff().dropna()
                        step = gaps[gaps > pd.Timedelta(0)].min() if len(gaps) else pd.Timedelta(minutes=1)
                        step = step if isinstance(step, pd.Timedelta) and step <= pd.Timedelta(minutes=15) \
                            else pd.Timedelta(minutes=1)
                        cols = [k for k in ("open", "high", "low", "close", "volume") if k in d]
                        out = d.set_index(d["timestamp"] + step)[cols].astype(float)
                    except (OSError, ValueError, KeyError, EOFError):
                        out = None
            self._bars_cache[c] = out
        return self._bars_cache[c]

    def day_range(self, c: Contract, now: datetime):
        """(open, high so far) of the contract on `now`'s day, from what was known by `now`: TrueData candles
        (completed bars) when stored, else the recorded prices. None when it has not traded yet today."""
        ts = pd.Timestamp(now)
        key = (c, ts.date())
        e = self._range_cache.get(key)
        if e is None:
            e = False
            start, end = ts.normalize(), ts.normalize() + pd.Timedelta(days=1)
            b = self.bars(c)
            if b is not None and {"open", "high"} <= set(b.columns):
                w = b[(b.index >= start) & (b.index < end)].dropna(subset=["open", "high"])
                if len(w):
                    e = (w.index, float(w["open"].iloc[0]), np.fmax.accumulate(w["high"].to_numpy(float)))
            if e is False:
                s = self._series(c)
                if s is not None and len(s):
                    w = s[(s.index >= start) & (s.index < end)]
                    if len(w):
                        e = (w.index, float(w.iloc[0]), np.fmax.accumulate(w.to_numpy(float)))
            self._range_cache[key] = e
        if e is False:
            return None
        idx, op, hi = e
        i = idx.searchsorted(ts, side="right") - 1
        if i < 0:
            return None
        return op, float(max(op, hi[i]))

    def volume(self, c: Contract, start, end) -> float | None:
        """Contracts traded in bars completed after `start` up to `end`; None when volume is not stored."""
        b = self.bars(c)
        if b is None or "volume" not in b:
            return None
        v = b["volume"]
        i, j = v.index.searchsorted(pd.Timestamp(start), side="right"), v.index.searchsorted(pd.Timestamp(end), side="right")
        return float(v.iloc[i:j].sum())

    def traded_by(self, c: Contract, now: datetime) -> bool:
        """The contract has traded today at or before `now` (a strike a trader could have seen quoted)."""
        return self.last_trade(c, now) is not None

    def next_price(self, c: Contract, now: datetime, within_minutes: float = 15) -> float | None:
        """The first real price after `now` the same day, within `within_minutes` (an order on a strike
        that has not traded yet fills on its next trade)."""
        s = self._series(c)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(now)
        i = s.index.searchsorted(ts, side="right")
        if i >= len(s) or s.index[i].date() != ts.date() or s.index[i] - ts > pd.Timedelta(minutes=within_minutes):
            return None
        return float(s.iloc[i])

    def prev_day_price(self, c: Contract, now: datetime) -> float | None:
        """The last real price before today (values a position carried overnight until it trades)."""
        s = self._series(c)
        if s is None or s.empty:
            return None
        ts = pd.Timestamp(now).normalize()
        i = s.index.searchsorted(ts, side="left") - 1
        return float(s.iloc[i]) if i >= 0 else None

    def append(self, c: Contract, now: datetime, last: float, bid=None, ask=None) -> None:
        p = self.path(c)
        p.parent.mkdir(parents=True, exist_ok=True)
        new = not p.exists()
        with p.open("a", encoding="utf-8") as f:
            if new:
                f.write("time,last,bid,ask\n")
            f.write(f"{now.isoformat(sep=' ')},{last},{bid or ''},{ask or ''}\n")

    def save_frame(self, c: Contract, frame: pd.DataFrame) -> None:
        p = self.path(c)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            frame = pd.concat([pd.read_csv(p, parse_dates=["time"]), frame]).drop_duplicates("time", keep="last")
        frame.sort_values("time").to_csv(p, index=False)


# ---- feeds -------------------------------------------------------------
_SMILE_CACHE: dict = {}


def _load_smile():
    from .eod import SMILE_FILE, Smile
    try:
        mtime = SMILE_FILE.stat().st_mtime
    except OSError:
        return None
    if _SMILE_CACHE.get("mtime") != mtime:
        _SMILE_CACHE.update(mtime=mtime, smile=Smile.load(SMILE_FILE))
    return _SMILE_CACHE["smile"]


class Feed:
    """Common shape: iterate to advance time; read .now/.spot/.vix; ask quote() and expiries()."""
    now: datetime
    spot: float
    vix: float | None = None

    def __init__(self, cfg: dict):
        m = cfg["market"]
        self.cfg = cfg
        self.underlying = m["underlying"]
        self.lot_size = int(m["lot_size"])
        self.strike_step = int(m["strike_step"])
        self._pricing = cfg["pricing"]
        self._lot_schedule = m.get("lot_size_schedule") or []
        self._holidays = {to_date(h) for h in m.get("holidays") or []}

    bar_closed = True  # False while a historical feed is walking through the inside of a bar

    def listed_strikes(self, expiry: date, right: str) -> set | None:
        """Strikes actually listed for an expiry; None when unknown (history: any strike can be priced)."""
        return None

    def quotes(self, contracts) -> dict:
        """Quotes for several contracts at once (a live feed fetches them in one request)."""
        return {c: self.quote(c) for c in contracts}

    def lot_size_for(self, expiry: date) -> int:
        """Lot size of a contract. NSE has changed the NIFTY lot several times; history uses the
        schedule in config.yaml (by contract expiry), live feeds the size Zerodha reports."""
        return int(as_of(self._lot_schedule, expiry, "size", self.lot_size))

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._holidays

    def next_trading_day(self, d: date) -> date:
        n = d + timedelta(days=1)
        while not self.is_trading_day(n):
            n += timedelta(days=1)
        return n

    def model_quote(self, c: Contract) -> Quote:
        p = self._pricing
        vix = self.vix
        sigma = (vix / 100.0 if vix and vix == vix else p["fallback_iv"]) * p["iv_scale"]
        t = year_fraction(self.now, c.expiry, p.get("overnight_variance_share", 0.3))
        source = "model"
        smile = self.smile
        if smile is not None and vix and vix == vix:
            ratio = smile.ratio(self.now, c.expiry, c.strike, self.spot, sigma, t)
            if ratio is not None:
                sigma, source = sigma * ratio, "model+smile"
        px = bs_price(self.spot, c.strike, t, sigma, c.right,
                      p["risk_free_rate"], p["dividend_yield"])
        return Quote(last=max(px, 0.05), source=source)

    @property
    def smile(self):
        """Volatility smile from NSE closing prices (run.py import-nse), when present and enabled."""
        if str(self._pricing.get("smile", "auto")).lower() == "off":
            return None
        return _load_smile()


class HistoricalFeed(Feed):
    """Replays stored spot bars. `control.sleep(delay)` paces it for simulated paper trading.

    With `execution.intrabar: ohlc` (the default) and bars that carry open/high/low, each bar is
    walked as open -> nearer extreme -> farther extreme -> close, at evenly spaced moments inside
    the bar, so a stop or a strike crossing that happened inside the bar is caught at that price
    instead of being missed when only the close is looked at."""

    def __init__(self, df: pd.DataFrame, cfg: dict, store: OptionStore | None = None,
                 delay: float = 0.0, control=None, real_only: bool = False):
        super().__init__(cfg)
        self._times = [t.to_pydatetime() for t in df.index]
        self._close = df["close"].to_numpy(float)
        self._vix = df["vix"].to_numpy(float)
        has_range = all(c in df for c in ("open", "high", "low"))
        self._ohlc = df[["open", "high", "low"]].to_numpy(float) if has_range else None
        gaps = np.diff(np.array(self._times, dtype="datetime64[s]")).astype("timedelta64[s]").astype(float)
        positive = gaps[gaps > 0]
        self._step = timedelta(seconds=float(positive.min())) if len(positive) else timedelta(minutes=5)
        self.intrabar = (cfg.get("execution", {}).get("intrabar", "ohlc") == "ohlc" and self._ohlc is not None
                         and self._step < timedelta(hours=1))
        self.store, self.delay, self.control = store, delay, control
        self._days = {t.date() for t in self._times}
        self._first_day, self._last_day = min(self._days), max(self._days)
        self._expiries = weekly_expiries(self._first_day, self._last_day + timedelta(days=45),
                                         cfg["market"]["expiry_rules"], self._days)
        # real_only: trade only expiries with stored real prices, and never price a contract with the
        # model while it has a real price earlier that day (see engine.backtest)
        self.real_only = bool(real_only and store is not None)
        self.price_stats = {"next_trade": 0, "old_fill": 0, "previous_day": 0, "missing": 0}
        self.liquidity = dict(cfg.get("execution", {}).get("liquidity") or {})
        # inside a bar, options follow their own TrueData high/low (calls with NIFTY, puts against it)
        self.option_intrabar = bool(self.intrabar and cfg.get("execution", {}).get("option_intrabar", True))
        self.bar_point, self._bar_end = "close", None
        self.missing: list = []
        if self.real_only:
            have = [e for e in store.stored_expiries() if e >= self._first_day]
            if have:
                self._expiries = have
        self._cache: dict = {}

    def _points(self, i: int, t: datetime):
        """(time, price, is_close) moments to visit for bar i."""
        close = float(self._close[i])
        if not self.intrabar:
            return [(t, close, True, "close")]
        o, h, lo = (float(x) for x in self._ohlc[i])
        if not (o == o and h == h and lo == lo):  # missing values
            return [(t, close, True, "close")]
        low_first = abs(o - lo) <= abs(h - o)
        path = [o] + ([lo, h] if low_first else [h, lo]) + [close]
        labels = ["open"] + (["low", "high"] if low_first else ["high", "low"]) + ["close"]
        start = t - self._step
        pts = []
        for k, px in enumerate(path, 1):
            if k < 4 and pts and px == pts[-1][1]:
                continue
            pts.append((start + self._step * k / 4, px, k == 4, labels[k - 1]))
        return pts

    def __iter__(self):
        day = None
        for i, t in enumerate(self._times):
            if self.store is not None and t.date() != day:     # a new day: expired contracts leave memory
                day = t.date()
                self.store.forget_expired(day)
            self.vix = float(self._vix[i])
            self._bar_end = t
            for when, px, closed, label in self._points(i, t):
                self.now, self.spot, self.bar_closed, self.bar_point = when, px, closed, label
                self._cache = {}
                yield when
            if self.delay and self.control and not self.control.sleep(self.delay):
                return

    def is_trading_day(self, d: date) -> bool:
        if self._first_day <= d <= self._last_day:
            return d in self._days
        return super().is_trading_day(d)

    def listed_strikes(self, expiry: date, right: str) -> set | None:
        """Real-prices-only: the strikes that traded that day, so a strategy scanning strikes (premium
        targets, wings) only picks ones with real prices - as a live feed offers only listed strikes."""
        if self.real_only:
            have = self.store.strikes_on(expiry, right, self.now.date())
            # only strikes that have traded by now: a strike that first trades later today was not on the screen
            seen = {k for k in have if self.store.traded_by(Contract(self.underlying, expiry, k, right), self.now)}
            floor = float(self.liquidity.get("min_volume_today", 0) or 0)
            if floor > 0 and seen:   # thin strikes are left off the screen when TrueData volume is stored
                day0 = datetime.combine(self.now.date(), time(0))
                vols = {k: self.store.volume(Contract(self.underlying, expiry, k, right), day0, self.now) for k in seen}
                thin = {k for k, v in vols.items() if v is not None and v < floor}
                if thin:
                    self.price_stats["thin_strikes"] = self.price_stats.get("thin_strikes", 0) + len(thin)
                    seen -= thin
            return seen or None
        return None

    def _real_fallback(self, c: Contract) -> float | None:
        """Real-prices-only: the contract has no price in the last few minutes. For a decision, use only what
        was known then: its last trade earlier today, or yesterday's last trade (an overnight position before
        it first trades). Never a later trade: that would show the strategy the future. (Fills may wait for the
        next trade: see fill_quote.) Only when nothing is known is the model used, and that is counted."""
        st = self.store
        px = st.price(c, self.now, stale_ok=True)
        if px is not None:
            return px
        px = st.prev_day_price(c, self.now)
        if px is not None and pd.Timestamp(self.now).date() != c.expiry:
            self.price_stats["previous_day"] += 1
            return px
        self.price_stats["missing"] += 1
        if len(self.missing) < 50:
            self.missing.append(f"{self.now:%Y-%m-%d %H:%M} {c.label}")
        return None

    @property
    def gives_day_ohlc(self) -> bool:
        """Real-prices backtests know each option's open and high so far (day_range); model-only ones do not."""
        return self.real_only

    def day_range(self, c: Contract):
        return self.store.day_range(c, self.now) if self.real_only else None

    def check_size(self, c: Contract, qty: int) -> None:
        """Counts orders larger than `max_share_of_volume` of the contracts traded in the last `window_minutes`
        (TrueData volume): such an order would move the price, so the result is too kind to it."""
        share = float(self.liquidity.get("max_share_of_volume", 0) or 0)
        if not (share > 0 and self.store and qty):
            return
        win = float(self.liquidity.get("window_minutes", 5) or 5)
        v = self.store.volume(c, self.now - timedelta(minutes=win), self.now)
        if v is None:
            return
        if abs(qty) > share * v:
            self.price_stats["large_orders"] = self.price_stats.get("large_orders", 0) + 1

    def fill_quote(self, c: Contract) -> Quote:
        """The price an order gets (real-prices-only backtests): the last trade `fill_delay_seconds` after the
        decision if it is at most `fill_max_age_minutes` old; otherwise the next trade within 15 minutes (the order
        waits for it); otherwise the decision's price (counted as an old price). Without stored prices, as quote."""
        if not (self.real_only and self.store):
            return self.quote(c)
        ex = self.cfg.get("execution", {})
        delay = float(ex.get("fill_delay_seconds", 0) or 0)
        if not delay:
            px = self._inside_bar_price(c)
            if px is not None:                               # an order inside a bar fills where the option was then
                self.price_stats["inside_bar"] = self.price_stats.get("inside_bar", 0) + 1
                return Quote(last=px, source="market")
        at = self.now + timedelta(seconds=delay)
        max_age = pd.Timedelta(minutes=float(ex.get("fill_max_age_minutes", 3) or 3))
        lt = self.store.last_trade(c, at)
        if lt is not None and pd.Timestamp(at) - lt[1] <= max_age:
            return Quote(last=lt[0], source="market")
        px = self.store.next_price(c, at)
        if px is not None:
            self.price_stats["next_trade"] += 1
            return Quote(last=px, source="market")
        q = self.quote(c)
        if q.source == "market":
            self.price_stats["old_fill"] = self.price_stats.get("old_fill", 0) + 1
        return q

    def expiries(self) -> list[date]:
        if self.real_only:
            have = self.store.expiries_on(self.now.date())
            if have:
                return have
        return self._expiries[bisect.bisect_left(self._expiries, self.now.date()):]

    def _inside_bar_price(self, c: Contract) -> float | None:
        """At the open / low / high point of a NIFTY bar, the option's open, low or high over the same minutes
        (TrueData candles): a call is at its low when NIFTY is, a put at its high. So a stop or target the option
        touched inside the bar is seen, as NIFTY's are. None at the close, or without candles."""
        if not (self.option_intrabar and self.store and not self.bar_closed and self._bar_end is not None):
            return None
        b = self.store.bars(c)
        if b is None or not {"open", "high", "low"} <= set(b.columns):
            return None
        end = pd.Timestamp(self._bar_end)
        i = b.index.searchsorted(end - pd.Timedelta(self._step), side="right")
        j = b.index.searchsorted(end, side="right")
        if i >= j:
            return None
        w = b.iloc[i:j]
        label = self.bar_point
        if c.right == "PE" and label in ("low", "high"):
            label = "high" if label == "low" else "low"
        px = {"open": w["open"].iloc[0], "low": w["low"].min(), "high": w["high"].max()}.get(label)
        if px is None or not px == px or px <= 0:
            return None
        return float(px)

    def quote(self, c: Contract) -> Quote:
        q = self._cache.get(c)
        if q is None:
            px = self._inside_bar_price(c)
            if px is None:
                px = self.store.price(c, self.now) if self.store else None
            if px is None and self.real_only:
                px = self._real_fallback(c)
            q = Quote(last=px, source="market") if px is not None else self.model_quote(c)
            self._cache[c] = q
        return q


# ---- sample data -------------------------------------------------------
def make_sample(start, end, interval: int = 5, seed: int = 7, s0: float = 24000.0) -> pd.DataFrame:
    """Synthetic NIFTY-like bars with a VIX-like series. For trying the platform only - not real prices."""
    rng = np.random.default_rng(seed)
    n = 375 // interval
    rows = []
    price, logvol, base = s0, np.log(0.13), np.log(0.13)
    for day in pd.bdate_range(to_date(start), to_date(end)):
        vol = float(np.exp(logvol))
        gap = rng.normal(0, vol / np.sqrt(252) * 0.4)
        if rng.random() < 0.03:
            gap += rng.choice([-1, 1]) * rng.uniform(0.008, 0.02)
        price *= float(np.exp(gap))
        logvol += -4.0 * gap
        for i in range(n):
            vol = float(np.clip(np.exp(logvol), 0.08, 0.5))
            r = rng.normal(0.10 / 252 / n, vol * np.sqrt(interval / 375 / 252) * 0.75)
            o, price = price, price * float(np.exp(r))
            wig = abs(rng.normal(0, 0.0003)) * price
            rows.append((day + pd.Timedelta(minutes=555 + i * interval), round(o, 2), round(max(o, price) + wig, 2),
                         round(min(o, price) - wig, 2), round(price, 2), round(vol * 100, 2)))
            logvol += 0.04 / n * (base - logvol) + rng.normal(0, 0.06 / np.sqrt(n)) - 4.0 * r
    return pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "vix"])


def write_sample(start="2025-01-01", end=None, interval: int = 5) -> Path:
    end = end or date.today()
    SPOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SPOT_DIR / f"SAMPLE_NIFTY_{interval}minute.csv"
    make_sample(start, end, interval).to_csv(path, index=False)
    return path
