"""TrueData historical data -> Algo Lab.

Downloads 1-minute candles (with open interest) for expired NIFTY option contracts, plus NIFTY spot
and India VIX, and stores them where the backtester already looks:

  data/options/NIFTY/<expiry>/<strike><CE|PE>.csv     time,last,bid,ask   (used by backtests)
  data/spot/TD_NIFTY_1minute.csv                      datetime,open,high,low,close,vix
  data/truedata/...                                   full raw copies (OHLC, volume, OI, bhavcopies)

Built for a short trial: every finished contract is recorded in data/truedata/progress.db, so the
download can be stopped and restarted at any time, and contracts nearest the money are fetched
first, so an unfinished run still holds the most useful data.

Login: secrets.yaml -> truedata: {user: ..., password: ...}  (or TRUEDATA_USER / TRUEDATA_PASSWORD).
"""
from __future__ import annotations

import gzip
import os
import re
import sqlite3
import struct
import threading
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

from .config import DATA, OPTION_DIR, ROOT, SPOT_DIR, to_date
from .feeds import OptionStore, weekly_expiries
from .models import Contract

AUTH_URL = "https://auth.truedata.in/token"
HIST_URL = "https://history.truedata.in"
TD_DIR = DATA / "truedata"
DEFAULTS = {
    "spot_symbol": "NIFTY 50",
    "vix_symbol": "INDIA VIX",
    "future_symbol": "NIFTY-I",
    "interval": "1min",
    "days_before_expiry": 35,   # candles from this many calendar days before each expiry
    "band_pct": 10,             # strikes within this % of NIFTY (during the contract's window)
    "chunk_days": 10,           # calendar days per request (halved automatically if refused)
    "workers": 4,               # parallel requests
    "min_gap_seconds": 0.25,    # between any two requests: 240/min, under TrueData's 600/min and 18,000/hour
}


# underlying -> (TrueData spot symbol, continuous future, strike step, monthly expiries only)
UNDERLYINGS = {
    "NIFTY": ("NIFTY 50", "NIFTY-I", 50, False),
    "BANKNIFTY": ("NIFTY BANK", "BANKNIFTY-I", 100, True),
    "SENSEX": ("SENSEX", "SENSEX-I", 100, False),
}
# F&O bhavcopy segments to try (BSE contracts are not in NSE's list; TrueData's BSE name is probed)
BHAV_SEGMENTS = {"SENSEX": ["BSEFO", "BFO", "BSE_FO", "BSE"]}
# weekly expiry weekday by date, where it differs from config.yaml's NIFTY rules (0 = Monday)
EXPIRY_RULES = {
    "SENSEX": [{"from": "2000-01-01", "weekday": 4},    # Friday
               {"from": "2025-01-01", "weekday": 1},    # Tuesday from Jan 2025
               {"from": "2025-09-01", "weekday": 3}],   # Thursday from Sep 2025 (SEBI: NSE Tue, BSE Thu)
}


def use_underlying(cfg: dict, name: str) -> dict:
    """Point the downloader at another index (strike step and TrueData symbols)."""
    name = name.upper()
    if name not in UNDERLYINGS:
        raise TrueDataError(f"Unknown underlying {name}; known: {', '.join(UNDERLYINGS)}")
    spot, fut, step, _monthly = UNDERLYINGS[name]
    if name != cfg["market"]["underlying"]:
        cfg["market"].update(underlying=name, strike_step=step)
        if name in EXPIRY_RULES:
            cfg["market"]["expiry_rules"] = EXPIRY_RULES[name]
        cfg.setdefault("truedata", {}).update(spot_symbol=spot, future_symbol=fut)
    return cfg


class TrueDataError(Exception):
    pass


class NoData(TrueDataError):
    """TrueData has nothing for that symbol and period (HTTP 404 / 'No Data Exists')."""


# ---- settings and login ---------------------------------------------------
def settings(cfg: dict | None) -> dict:
    return {**DEFAULTS, **((cfg or {}).get("truedata") or {})}


def credentials() -> tuple[str, str]:
    user, pw = os.environ.get("TRUEDATA_USER"), os.environ.get("TRUEDATA_PASSWORD")
    path = ROOT / "secrets.yaml"
    if not (user and pw) and path.exists():
        import yaml
        td = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("truedata") or {}
        user, pw = user or td.get("user"), pw or td.get("password")
    if not (user and pw):
        raise TrueDataError("TrueData login not set. Add to secrets.yaml:\n"
                            "truedata:\n  user: \"your_trial_user\"\n  password: \"your_password\"")
    return str(user), str(pw)


# ---- REST client ----------------------------------------------------------
def _decode(content: bytes) -> str:
    """TrueData sends 'comp=true' bodies as <uncompressed len><compressed len><lz4 block>."""
    if len(content) >= 8:
        raw_len, comp_len = struct.unpack("<II", content[:8])
        if comp_len == len(content) - 8 and raw_len < 2_000_000_000:
            if raw_len == comp_len:
                return content[8:].decode()
            import lz4.block
            return lz4.block.decompress(content[8:], uncompressed_size=raw_len).decode()
    return content.decode()


def _frame(text: str) -> pd.DataFrame:
    text = text.strip()
    if not text or text.lower().startswith("no data"):
        return pd.DataFrame()
    df = pd.read_csv(StringIO(text))
    df.columns = [str(c).strip().lower() for c in df.columns]
    for alt in ("time", "datetime", "date", "ts"):
        if "timestamp" not in df and alt in df:
            df = df.rename(columns={alt: "timestamp"})
    if "timestamp" in df:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
    return df


class Client:
    """Thread-safe TrueData history client with token refresh, pacing and retries."""

    def __init__(self, user: str, password: str, min_gap: float = 0.25, session=None, log=print):
        self.user, self.password, self.min_gap, self.log = user, password, min_gap, log
        self.http = session or requests.Session()
        self._token, self._expires = None, datetime.min
        self._lock, self._pace = threading.Lock(), threading.Lock()
        self._last = 0.0
        self.requests = 0

    def token(self, force: bool = False) -> str:
        with self._lock:
            if force or not self._token or datetime.now() >= self._expires:
                r = self.http.post(AUTH_URL, data={"username": self.user, "password": self.password,
                                                   "grant_type": "password"}, timeout=30)
                try:
                    js = r.json()
                except ValueError:
                    js = {}
                if not js.get("access_token"):
                    raise TrueDataError(f"TrueData login failed: {js.get('error_description') or r.text[:200]}")
                self._token = js["access_token"]
                self._expires = datetime.now() + timedelta(seconds=int(js.get("expires_in", 3600)) - 60)
            return self._token

    def _wait_turn(self) -> None:
        with self._pace:
            wait = self._last + self.min_gap - _time.monotonic()
            if wait > 0:
                _time.sleep(wait)
            self._last = _time.monotonic()

    def get(self, endpoint: str, params: dict, tries: int = 6) -> str:
        delay = 2.0
        for attempt in range(1, tries + 1):
            self._wait_turn()
            try:
                r = self.http.get(f"{HIST_URL}/{endpoint}", params=params, timeout=90,
                                  headers={"Authorization": f"Bearer {self.token()}"})
                self.requests += 1
            except requests.RequestException as e:
                if attempt == tries:
                    raise TrueDataError(f"network error: {e}") from e
                _time.sleep(delay)
                delay = min(delay * 2, 120)
                continue
            if r.status_code == 200:
                text = _decode(r.content)
                if text.strip().lower().startswith("no data"):
                    raise NoData(text.strip()[:120])
                return text
            if r.status_code == 404:
                raise NoData(r.text[:120])
            if r.status_code == 401:
                self.token(force=True)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == tries:
                    raise TrueDataError(f"HTTP {r.status_code} after {tries} tries")
                self.log(f"  TrueData said {r.status_code}; waiting {delay:.0f}s")
                _time.sleep(delay)
                delay = min(delay * 2, 300)
                continue
            raise TrueDataError(f"HTTP {r.status_code}: {r.text[:200]}")
        raise TrueDataError("unreachable")

    def bars(self, symbol: str, start: datetime, end: datetime, interval: str = "1min") -> pd.DataFrame:
        params = {"symbol": symbol, "from": start.strftime("%y%m%dT%H:%M:%S"),
                  "to": end.strftime("%y%m%dT%H:%M:%S"), "interval": interval,
                  "response": "csv", "comp": "true"}
        return _frame(self.get("getbars", params))

    def ticks(self, symbol: str, day: date, bidask: bool = True) -> pd.DataFrame:
        """Every trade (and, with bidask, the best bid/ask) for one day. TrueData keeps about 5 days."""
        params = {"symbol": symbol, "from": datetime.combine(day, datetime.min.time()).strftime("%y%m%dT09:00:00"),
                  "to": datetime.combine(day, datetime.min.time()).strftime("%y%m%dT16:00:00"),
                  "bidask": 1 if bidask else 0, "response": "csv", "comp": "true"}
        return _frame(self.get("getticks", params))

    def bhavcopy(self, segment: str, day: date) -> pd.DataFrame:
        return _frame(self.get("getbhavcopy", {"segment": segment, "date": day.isoformat(),
                                               "response": "csv", "comp": "true"}))


def connect(cfg: dict | None = None, log=print) -> Client:
    user, pw = credentials()
    c = Client(user, pw, float(settings(cfg)["min_gap_seconds"]), log=log)
    c.token()
    return c


# ---- symbols --------------------------------------------------------------
def option_symbol(c: Contract) -> str:
    """TrueData option symbol: NIFTY + YYMMDD + strike + CE/PE, e.g. NIFTY26042124000CE."""
    return f"{c.underlying}{c.expiry:%y%m%d}{c.strike}{c.right}"


_SYM = re.compile(r"^([A-Z&]+?)(\d{2})(\d{2})(\d{2})(\d+(?:\.\d+)?)(CE|PE)$")


def parse_symbol(s: str) -> Contract | None:
    m = _SYM.match(str(s).strip().upper())
    if not m:
        return None
    und, yy, mm, dd, strike, right = m.groups()
    try:
        exp = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    k = float(strike)
    return Contract(und, exp, int(k) if k.is_integer() else k, right)


def contracts_in(df: pd.DataFrame, underlying: str) -> set[Contract]:
    """Option contracts of `underlying` named anywhere in a bhavcopy (column names vary)."""
    found = set()
    for col in df.columns:
        if pd.api.types.is_numeric_dtype(df[col]) or pd.api.types.is_datetime64_any_dtype(df[col]):
            continue
        vals = df[col].dropna().astype(str)
        vals = vals[vals.str.upper().str.startswith(underlying)]
        for v in vals.unique():
            c = parse_symbol(v)
            if c and c.underlying == underlying:
                found.add(c)
    return found


# ---- storage --------------------------------------------------------------
class Progress:
    """data/truedata/progress.db: one row per contract or series and how far it got."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS items(
            key TEXT PRIMARY KEY, kind TEXT, expiry TEXT, strike REAL, right TEXT,
            start TEXT, end TEXT, priority REAL, status TEXT DEFAULT 'pending',
            rows INTEGER DEFAULT 0, note TEXT DEFAULT '', updated TEXT)""")
        self.db.execute("CREATE TABLE IF NOT EXISTS days(day TEXT PRIMARY KEY, status TEXT, contracts INTEGER)")
        self.db.commit()

    def add(self, key, kind, start, end, priority=0.0, contract: Contract | None = None):
        with self.lock:
            self.db.execute("""INSERT INTO items(key,kind,expiry,strike,right,start,end,priority,updated)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                start=MIN(start, excluded.start), end=MAX(end, excluded.end), priority=excluded.priority,
                status=CASE WHEN excluded.end > end OR excluded.start < start THEN 'pending' ELSE status END""",
                            (key, kind, contract.expiry.isoformat() if contract else None,
                             contract.strike if contract else None, contract.right if contract else None,
                             start.isoformat(), end.isoformat(), priority, _now()))
            self.db.commit()

    def set(self, key, status, rows=0, note=""):
        with self.lock:
            self.db.execute("UPDATE items SET status=?, rows=?, note=?, updated=? WHERE key=?",
                            (status, rows, note[:300], _now(), key))
            self.db.commit()

    def pending(self, kind, retry_errors=False, underlying: str | None = None):
        states = ("pending", "error") if retry_errors else ("pending",)
        q = f"SELECT key,expiry,strike,right,start,end FROM items WHERE kind=? AND status IN ({','.join('?' * len(states))})"
        args = [kind, *states]
        if underlying:   # NIFTY26... but not BANKNIFTY26...
            q += " AND key GLOB ?"
            args.append(f"{underlying}[0-9]*")
        with self.lock:
            return self.db.execute(q + " ORDER BY priority, expiry DESC", args).fetchall()

    def summary(self):
        """(underlying, status, contracts, candles) rows."""
        with self.lock:
            rows = self.db.execute("SELECT key,status,rows FROM items").fetchall()
        out: dict = {}
        for key, status, n in rows:
            und = re.match(r"[A-Z&]+?(?=\d{6})", key)
            name = ("STOCKS" if key.startswith("EQ:") else "STK FUTURE" if key.startswith("FUT:")
                    else "TICKS " + key.split(":")[1] if key.startswith("TICK:")
                    else und.group(0) if und else key)
            k = (name, status)
            c, r = out.get(k, (0, 0))
            out[k] = (c + 1, r + (n or 0))
        return [(u, st, c, r) for (u, st), (c, r) in sorted(out.items())]

    def day_done(self, day, segment: str = "FO"):
        with self.lock:
            r = self.db.execute("SELECT status FROM days WHERE day=?", (_day_key(day, segment),)).fetchone()
        return r[0] if r else None

    def set_day(self, day, status: str, n: int = 0, segment: str = "FO"):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO days VALUES(?,?,?)", (_day_key(day, segment), status, n))
            self.db.commit()

    def get(self, key):
        with self.lock:
            return self.db.execute("SELECT status, rows FROM items WHERE key=?", (key,)).fetchone()


def _day_key(day, segment: str) -> str:
    """NSE F&O days keep their plain date (as before); other segments are prefixed: 'BSEFO:2025-01-02'."""
    d = day.isoformat() if hasattr(day, "isoformat") else str(day)
    return d if segment == "FO" else f"{segment}:{d}"


class Meter:
    """One-line progress in the terminal: label, percent, counts and time left."""

    def __init__(self, label: str, total: int, log=print, unit: str = ""):
        self.label, self.total, self.log, self.unit = label, max(int(total), 0), log, unit
        self.t0, self.last = _time.monotonic(), -1.0

    def show(self, done: int, extra: str = "", force: bool = False) -> None:
        now = _time.monotonic()
        if not force and now - self.last < 1.0 and done < self.total:
            return
        self.last = now
        pct = 100.0 * done / self.total if self.total else 100.0
        left = ""
        if 0 < done < self.total:
            secs = (now - self.t0) / done * (self.total - done)
            left = f"  ~{secs / 60:.0f} min left" if secs >= 60 else f"  ~{secs:.0f} s left"
        self.log(f"\r  {self.label}: {pct:5.1f}%  {done:,}/{self.total:,}{self.unit}{left}{extra}      ", end="")

    def finish(self, extra: str = "") -> None:
        self.show(self.total, extra, force=True)
        self.log("")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _read_csv_safe(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_csv(path)
    except (OSError, ValueError, EOFError):
        return None


def _read_saved(path: Path) -> pd.DataFrame | None:
    """A saved candle file, or None when it is missing, empty or damaged (e.g. cut off by Ctrl+C)."""
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path, parse_dates=["timestamp"])
    except (OSError, ValueError, EOFError, KeyError):
        return None
    return df if "timestamp" in df and len(df) else None


def _save_raw(path: Path, df: pd.DataFrame) -> int:
    """Merge new candles into the saved file. Written to a temporary file first and then swapped in,
    so a stop part-way through can never leave a damaged file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    old = _read_saved(path)
    if old is not None:
        df = pd.concat([old, df])
    df = df.drop_duplicates("timestamp", keep="last").sort_values("timestamp")
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        df.to_csv(f, index=False)
    os.replace(tmp, path)
    return len(df)


def _have(path: Path):
    """(first day, last day, candles) already saved for a contract, or None."""
    df = _read_saved(path)
    if df is None:
        return None
    ts = df["timestamp"]
    return (ts.min().date(), ts.max().date(), len(ts))


def _bar_step(df: pd.DataFrame) -> pd.Timedelta:
    gaps = df["timestamp"].sort_values().diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(0)]
    return gaps.min() if len(gaps) else pd.Timedelta(minutes=1)


# ---- the downloader -------------------------------------------------------
class Downloader:
    def __init__(self, cfg: dict, start, end, client: Client | None = None, log=print, root: Path = TD_DIR,
                 option_root: Path = OPTION_DIR, nearest: bool = False, open_contracts: bool = False):
        self.cfg, self.s, self.log = cfg, settings(cfg), log
        self.start, self.end = to_date(start), to_date(end)
        self.und = cfg["market"]["underlying"]
        self.step = int(cfg["market"]["strike_step"])
        self.root, self.option_root = root, option_root
        self.nearest = nearest   # only each expiry's own week (while it is the nearest expiry)
        self.open = open_contracts   # also contracts not expired yet (their days so far, up to `end`)
        self.progress = Progress(root / "progress.db")
        self._client = client
        self.holidays = {to_date(h) for h in cfg["market"].get("holidays") or []}

    @property
    def client(self) -> Client:
        if self._client is None:
            self._client = connect(self.cfg, self.log)
        return self._client

    def weekdays(self):
        d = self.start
        while d <= self.end:
            if d.weekday() < 5 and d not in self.holidays:
                yield d
            d += timedelta(days=1)

    # -- fetch with chunking ------------------------------------------------
    def fetch(self, symbol: str, start: date, end: date, chunk_days: int | None = None,
              on_chunk=None) -> pd.DataFrame:
        chunk = int(chunk_days or self.s["chunk_days"])
        parts, d = [], start
        while d <= end:
            if on_chunk:
                on_chunk((d - start).days, (end - start).days + 1)
            e = min(d + timedelta(days=chunk - 1), end)
            try:
                parts.append(self.client.bars(symbol, datetime.combine(d, datetime.min.time()),
                                              datetime.combine(e, datetime.max.time().replace(microsecond=0)),
                                              self.s["interval"]))
            except NoData:
                pass
            except TrueDataError as err:
                if chunk > 1 and "HTTP 4" in str(err):   # range too long for one request: split it
                    self.s["chunk_days"] = max(1, chunk // 2)
                    self.log(f"  request refused ({err}); trying {self.s['chunk_days']}-day pieces")
                    parts.append(self.fetch(symbol, d, e, self.s["chunk_days"]))
                else:
                    raise
            d = e + timedelta(days=1)
        parts = [p for p in parts if not p.empty]
        return pd.concat(parts).drop_duplicates("timestamp").sort_values("timestamp") if parts else pd.DataFrame()

    # -- step 1: spot, VIX and futures ----------------------------------------
    def spot(self) -> Path | None:
        series = {"spot": self.s["spot_symbol"], "vix": self.s["vix_symbol"], "fut": self.s["future_symbol"]}
        frames = {}
        for i, (name, sym) in enumerate(series.items(), 1):
            meter = Meter(f"{sym} ({i}/{len(series)}) {self.start} to {self.end}", (self.end - self.start).days + 1,
                          self.log, " days")
            try:
                df = self.fetch(sym, self.start, self.end, on_chunk=lambda done, tot: meter.show(done))
                meter.finish()
            except TrueDataError as e:
                self.log(f"  failed: {e}")
                continue
            if df.empty:
                self.log("  no data")
                continue
            _save_raw(self.root / "underlying" / f"{sym.replace(' ', '_')}_{self.s['interval']}.csv.gz", df)
            frames[name] = df
            self.log(f"  {len(df):,} bars")
        if "spot" not in frames:
            return None
        spot = frames["spot"][["timestamp", "open", "high", "low", "close"]].rename(columns={"timestamp": "datetime"})
        if "vix" in frames:
            v = frames["vix"][["timestamp", "close"]].rename(columns={"timestamp": "datetime", "close": "vix"})
            spot = pd.merge_asof(spot.sort_values("datetime"), v.sort_values("datetime"), on="datetime",
                                 direction="backward", tolerance=pd.Timedelta(minutes=30))
        else:
            spot["vix"] = float("nan")
        SPOT_DIR.mkdir(parents=True, exist_ok=True)
        path = SPOT_DIR / f"TD_{self.und}_1minute.csv"
        if path.exists():
            spot = pd.concat([pd.read_csv(path, parse_dates=["datetime"]), spot])
        spot = spot.drop_duplicates("datetime", keep="last").sort_values("datetime")
        spot.to_csv(path, index=False)
        self.log(f"Backtest data set: {path.stem}  ({len(spot):,} bars)")
        return path

    def spot_closes(self) -> pd.Series:
        """Daily NIFTY close (from the saved spot file) for choosing strikes."""
        path = SPOT_DIR / f"TD_{self.und}_1minute.csv"
        if not path.exists():
            alt = sorted(p for p in SPOT_DIR.glob(f"{self.und}_*.csv"))
            if not alt:
                raise TrueDataError("No NIFTY prices yet - run the 'spot' step first.")
            path = alt[0]
        df = pd.read_csv(path, parse_dates=["datetime"])
        return df.groupby(df["datetime"].dt.date)["close"].agg(["min", "max"])

    # -- step 2: which contracts existed (F&O bhavcopy) ---------------------
    def _segment(self, days: list) -> str | None:
        """The bhavcopy segment that lists this underlying's contracts (probed on recent days)."""
        cands = BHAV_SEGMENTS.get(self.und, ["FO"])
        if cands == ["FO"]:
            return "FO"
        for seg in cands:
            for d in reversed(days[-6:]):
                cached = self.root / "bhavcopy" / f"{seg}_{d}.csv.gz"
                try:
                    df = pd.read_csv(cached) if cached.exists() else self.client.bhavcopy(seg, d)
                except (TrueDataError, OSError, ValueError):
                    continue
                if not df.empty and contracts_in(df, self.und):
                    self.log(f"  {self.und} contracts are in TrueData's '{seg}' bhavcopy")
                    return seg
        return None

    def contracts(self) -> int:
        seen: dict[Contract, list[date]] = {}
        raw = self.root / "bhavcopy"
        raw.mkdir(parents=True, exist_ok=True)
        ok = 0
        days = list(self.weekdays())
        seg = self._segment(days)
        if seg is None:
            self.log(f"No bhavcopy lists {self.und} contracts - working out expiries and strikes from the calendar.")
            return self.contracts_from_calendar()
        meter = Meter(f"Contract lists (F&O bhavcopy) {self.und}", len(days), self.log, " days")
        for i, d in enumerate(days, 1):
            meter.show(i - 1)
            path = raw / f"{seg}_{d}.csv.gz"
            if path.exists():
                df = _read_csv_safe(path)
                if df is None:
                    path.unlink()
                    continue
            elif self.progress.day_done(d, seg) == "none":
                continue
            else:
                try:
                    df = self.client.bhavcopy(seg, d)
                except NoData:
                    self.progress.set_day(d, "none", segment=seg)
                    continue
                except TrueDataError as e:
                    self.log(f"  bhavcopy {d}: {e}")
                    continue
                if df.empty:
                    self.progress.set_day(d, "none", segment=seg)
                    continue
                with gzip.open(path, "wt", encoding="utf-8") as f:
                    df.to_csv(f, index=False)
            cs = contracts_in(df, self.und)
            self.progress.set_day(d, "ok", len(cs), segment=seg)
            ok += 1
            for c in cs:
                seen.setdefault(c, []).append(d)
        meter.finish(f"  ({ok} days with a list)")
        if ok < len(days) // 2:
            self.log(f"  Note: contract lists exist for only {ok} of {len(days)} days in this period.")
        if not ok:
            self.log("No bhavcopy available - working out expiries and strikes from the calendar instead.")
            return self.contracts_from_calendar()
        return self._queue({c: (min(ds), max(ds)) for c, ds in seen.items()})

    def contracts_from_calendar(self) -> int:
        """Fallback when bhavcopies are not available: weekly expiries + every strike in the band."""
        traded = set(self.spot_closes().index)
        exps = weekly_expiries(self.start, self.end, self.cfg["market"]["expiry_rules"],
                               {d for d in traded if self.start <= d <= self.end} or set(self.weekdays()) or None)
        if UNDERLYINGS.get(self.und, (0, 0, 0, False))[3]:   # monthly contracts only: last expiry of each month
            exps = sorted({(e.year, e.month): e for e in exps}.values())
        found = {}
        for e in exps:
            for k in self._strike_range(e - timedelta(days=int(self.s["days_before_expiry"])), e):
                for r in ("CE", "PE"):
                    found[Contract(self.und, e, k, r)] = (e - timedelta(days=int(self.s["days_before_expiry"])), e)
        return self._queue(found)

    def _strike_range(self, a: date, b: date):
        closes = self.spot_closes()
        w = closes[(closes.index >= a) & (closes.index <= b)]
        if w.empty:
            return []
        band = float(self.s["band_pct"]) / 100
        lo = int(w["min"].min() * (1 - band)) // self.step * self.step
        hi = -(-int(w["max"].max() * (1 + band)) // self.step) * self.step
        return range(lo, hi + self.step, self.step)

    def _queue(self, found: dict) -> int:
        closes = self.spot_closes()
        band = float(self.s["band_pct"]) / 100
        dbe = int(self.s["days_before_expiry"])
        expiries = sorted({c.expiry for c in found})
        previous = {e: (expiries[i - 1] if i else None) for i, e in enumerate(expiries)}
        n = 0
        for c, (first, last) in found.items():
            if c.expiry < self.start or (c.expiry > self.end and not self.open):
                continue   # expired contracts in the period only (plus open ones with --open)
            if self.nearest:   # only the days after the previous expiry: while this is the nearest one
                prev = previous[c.expiry]
                a = max(self.start, first, prev + timedelta(days=1) if prev else c.expiry - timedelta(days=7))
            else:
                a = max(self.start, first, c.expiry - timedelta(days=dbe))
            b = min(self.end, c.expiry)
            if a > b:
                continue
            w = closes[(closes.index >= a) & (closes.index <= b)]
            if w.empty:
                continue
            lo, hi = w["min"].min(), w["max"].max()
            if not (lo * (1 - band) <= c.strike <= hi * (1 + band)):
                continue
            mid = (lo + hi) / 2
            # nearest the money first; recent expiries before older ones within each 1% band
            priority = round(abs(c.strike - mid) / mid * 100)
            self.progress.add(option_symbol(c), "option", a, b, priority, c)
            n += 1
        span = "each expiry only while it is the nearest one" if self.nearest else f"last {dbe} days before each expiry"
        if self.open:
            span += "; open contracts up to " + self.end.isoformat()
        self.log(f"{n:,} {self.und} contracts queued ({self.start} to {self.end}; strikes within "
                 f"{self.s['band_pct']}% of the index; {span})")
        return n

    # -- step 3: option candles ------------------------------------------------
    def plan(self) -> dict:
        rows = self.progress.pending("option", retry_errors=True, underlying=self.und)
        req = 0
        for _k, _e, _s, _r, a, b in rows:
            days = (date.fromisoformat(b) - date.fromisoformat(a)).days + 1
            req += -(-days // int(self.s["chunk_days"]))
        secs = req * max(float(self.s["min_gap_seconds"]), 1.0 / max(int(self.s["workers"]), 1))
        return {"contracts": len(rows), "requests": req,
                "estimate": f"about {secs / 3600:.1f} h if TrueData answers in under a second"}

    def options(self, retry_errors=False, limit: int | None = None, stop=None) -> int:
        rows = self.progress.pending("option", retry_errors, self.und)[:limit]
        store = OptionStore(self.und, root=self.option_root)
        total, done, lock = len(rows), [0], threading.Lock()
        meter = Meter(f"{self.und} option candles", total, self.log, " contracts")
        self.log(f"{total:,} {self.und} contracts to download")

        def one(row):
            try:
                return work(row)
            except Exception as e:   # one bad contract must never stop the whole download
                self.progress.set(row[0], "error", note=f"{type(e).__name__}: {e}")
                return f"{row[0]}: {type(e).__name__}: {e}"

        def work(row):
            key, exp, strike, right, a, b = row
            if stop and stop():
                return
            c = Contract(self.und, date.fromisoformat(exp), int(strike) if float(strike).is_integer() else strike, right)
            raw = self.root / "options" / self.und / exp / f"{strike:g}{right}.csv.gz"
            a, b = date.fromisoformat(a), date.fromisoformat(b)
            have = _have(raw)
            if have and have[1] >= b - timedelta(days=4):   # the later days are saved already (e.g. --nearest)
                b = have[0] - timedelta(days=1)             # fetch only the earlier days still missing
            try:
                df = self.fetch(key, a, b) if a <= b else pd.DataFrame()
            except TrueDataError as e:
                self.progress.set(key, "error", note=str(e))
                return f"{key}: {e}"
            if df.empty:
                self.progress.set(key, "done", rows=have[2]) if have else self.progress.set(key, "empty")
            else:
                rows_saved = _save_raw(raw, df)
                step = _bar_step(df)
                frame = pd.DataFrame({"time": df["timestamp"] + step, "last": df["close"], "bid": "", "ask": ""})
                store.save_frame(c, frame)
                self.progress.set(key, "done", rows=rows_saved)
            with lock:
                done[0] += 1
                meter.show(done[0])
            return None

        errors = []
        with ThreadPoolExecutor(max_workers=int(self.s["workers"])) as pool:
            for f in as_completed([pool.submit(one, r) for r in rows]):
                if f.result():
                    errors.append(f.result())
        meter.finish()
        for e in errors[:10]:
            self.log("  error " + e)
        if errors:
            self.log(f"{len(errors)} failed; run again with --retry-errors")
        return done[0]

    def status(self) -> list[str]:
        out = []
        for und, status, n, rows in self.progress.summary():
            out.append(f"{und:<11}{status:<9}{n:>7,} contracts{(rows or 0):>13,} candles")
        return out or ["Nothing queued yet."]


def test_login(cfg: dict, log=print) -> None:
    """Log in and fetch one day of NIFTY and one expired option, printing what comes back."""
    c = connect(cfg, log)
    s = settings(cfg)
    log("Login OK.")
    day = date.today() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    df = c.bars(s["spot_symbol"], datetime.combine(day, datetime.min.time()),
                datetime.combine(day, datetime.max.time().replace(microsecond=0)), s["interval"])
    log(f"{s['spot_symbol']} on {day}: {len(df)} bars, columns {list(df.columns)}")
    if df.empty:
        return
    print(df.head(3).to_string(index=False))
    spot = float(df["close"].iloc[-1])
    # an expired weekly contract about 3 months back, at the money then
    exp = date.today() - timedelta(days=90)
    while exp.weekday() != 1:
        exp -= timedelta(days=1)
    ref = c.bars(s["spot_symbol"], datetime.combine(exp - timedelta(days=4), datetime.min.time()),
                 datetime.combine(exp, datetime.max.time().replace(microsecond=0)), "eod")
    atm = round(float(ref["close"].iloc[-1] if not ref.empty else spot) / 50) * 50
    for e in (exp, exp - timedelta(days=1)):   # holiday expiries move a day earlier
        sym = option_symbol(Contract(cfg["market"]["underlying"], e, atm, "CE"))
        try:
            o = c.bars(sym, datetime.combine(e - timedelta(days=2), datetime.min.time()),
                       datetime.combine(e, datetime.max.time().replace(microsecond=0)), s["interval"])
        except NoData:
            log(f"{sym}: no data")
            continue
        log(f"{sym} (expired): {len(o)} bars, columns {list(o.columns)}")
        print(o.head(3).to_string(index=False))
        break


def history_depth(cfg: dict, months=(6, 7, 8, 9, 10, 12, 15, 18, 24, 36), log=print, root: Path = TD_DIR,
                  client: Client | None = None) -> Path:
    """How far back TrueData has data: NIFTY 1-minute bars, and an expired at-the-money option's
    1-minute and daily bars, at several ages. Saves data/truedata/history_depth.csv."""
    c = client or connect(cfg, log)
    s = settings(cfg)
    und = cfg["market"]["underlying"]
    today = date.today()

    def count(sym, a, b, interval):
        try:
            return len(c.bars(sym, datetime.combine(a, datetime.min.time()),
                              datetime.combine(b, datetime.max.time().replace(microsecond=0)), interval)), ""
        except NoData:
            return 0, ""
        except TrueDataError as e:
            return 0, str(e)[:80]

    rows = []
    for m in months:
        day = (pd.Timestamp(today) - pd.DateOffset(months=m)).date()
        row = {"months_back": m, "date": day.isoformat()}
        # NIFTY 1-minute: the 5 weekdays ending on that date (covers holidays)
        row["spot_1min_bars"], note = count(s["spot_symbol"], day - timedelta(days=6), day, s["interval"])
        # an expired weekly contract that expired on/after that date, at the money then
        exps = [e for e in weekly_expiries(day, day + timedelta(days=70), cfg["market"]["expiry_rules"]) if e >= day]
        if UNDERLYINGS.get(und, (0, 0, 0, False))[3]:   # monthly contracts only
            exps = sorted({(e.year, e.month): e for e in exps}.values())
        exps = exps[:1]
        row.update(option="", option_1min_bars=0, option_eod_bars=0)
        if exps:
            e = exps[0]
            try:
                ref = c.bars(s["spot_symbol"], datetime.combine(e - timedelta(days=10), datetime.min.time()),
                             datetime.combine(e, datetime.max.time().replace(microsecond=0)), "eod")
            except TrueDataError:
                ref = pd.DataFrame()
            if not ref.empty:
                atm = int(round(float(ref["close"].iloc[-1]) / 100) * 100)
                for exp in (e, e - timedelta(days=1), e - timedelta(days=2)):   # holiday expiries move earlier
                    sym = option_symbol(Contract(und, exp, atm, "CE"))
                    n1, note1 = count(sym, exp - timedelta(days=4), exp, s["interval"])
                    ne, _ = count(sym, exp - timedelta(days=30), exp, "eod")
                    if n1 or ne:
                        row.update(option=sym, option_1min_bars=n1, option_eod_bars=ne)
                        break
                    note = note or note1
                else:
                    row["option"] = option_symbol(Contract(und, e, atm, "CE"))
        row["note"] = note
        rows.append(row)
        log(f"  {m:>2} months back ({day}): NIFTY 1-min {row['spot_1min_bars']:>5}  "
            f"{row['option'] or '-':<22} 1-min {row['option_1min_bars']:>5}  daily {row['option_eod_bars']:>3}")
    df = pd.DataFrame(rows)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "history_depth.csv"
    df.to_csv(path, index=False)
    ok1 = df.loc[df["option_1min_bars"] > 0, "months_back"]
    oke = df.loc[df["option_eod_bars"] > 0, "months_back"]
    log(f"Expired options, 1-minute: data found up to {ok1.max() if len(ok1) else 0} months back "
        f"(checked up to {max(months)}).")
    log(f"Expired options, daily: data found up to {oke.max() if len(oke) else 0} months back.")
    log(f"Saved {path}")
    return path


# ---- NIFTY 50 stocks: cash equity and near-month futures ---------------------
NIFTY50_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv"
# Used only when the official list cannot be fetched. Best knowledge in Oct 2026; check before relying on it.
NIFTY50_FALLBACK = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV",
    "BEL", "BHARTIARTL", "BSE", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH",
    "HDFCBANK", "HDFCLIFE", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC", "JIOFIN",
    "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TMPV", "TRENT", "ULTRACEMCO",
]


def nifty50_symbols(log=print, session=None) -> tuple[list[str], str]:
    """Current NIFTY 50 symbols from NSE's index site; the built-in list if that fails."""
    try:
        http = session or requests.Session()
        r = http.get(NIFTY50_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0 (Macintosh) AlgoLab"})
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        col = next(c for c in df.columns if c.strip().lower() == "symbol")
        syms = [str(x).strip() for x in df[col].dropna()]
        if 45 <= len(syms) <= 55:
            return syms, "NSE (niftyindices.com, today)"
    except Exception as e:  # network, format
        log(f"  Could not fetch the official NIFTY 50 list ({type(e).__name__}); using the built-in list.")
    return list(NIFTY50_FALLBACK), "built-in list (Oct 2026)"


def download_stocks(dl: "Downloader", symbols: list[str], futures: bool = True, log=print) -> int:
    """1-minute candles for each stock and (optionally) its near-month continuous future (<SYMBOL>-I).
    Saved to data/truedata/equity/<SYMBOL>_1min.csv.gz and data/truedata/futures/<SYMBOL>-I_1min.csv.gz.
    Resumable: finished symbols are recorded in progress.db."""
    jobs = []
    for sym in symbols:
        jobs.append(("EQ", sym, dl.root / "equity" / f"{_safe(sym)}_1min.csv.gz"))
        if futures:
            jobs.append(("FUT", f"{sym}-I", dl.root / "futures" / f"{_safe(sym)}-I_1min.csv.gz"))
    todo = []
    for kind, sym, path in jobs:
        key = f"{kind}:{sym}:{dl.start}:{dl.end}"
        st = dl.progress.get(key)
        if st and st[0] in ("done", "empty"):
            continue
        dl.progress.add(key, "stock", dl.start, dl.end, 0)
        todo.append((key, sym, path))
    total_days = (dl.end - dl.start).days + 1
    meter = Meter("NIFTY 50 stocks and futures", len(todo) * total_days, log, " symbol-days")
    log(f"{len(todo)} of {len(jobs)} series to download ({len(symbols)} stocks"
        + (" + their futures" if futures else "") + f", {dl.start} to {dl.end})")
    done = 0
    for i, (key, sym, path) in enumerate(todo):
        base = i * total_days
        try:
            df = dl.fetch(sym, dl.start, dl.end, on_chunk=lambda d, t: meter.show(base + d, f"  {sym}"))
        except TrueDataError as e:
            dl.progress.set(key, "error", note=str(e))
            log(f"\n  {sym}: {e}")
            continue
        if df.empty:
            dl.progress.set(key, "empty")
            continue
        n = _save_raw(path, df)
        dl.progress.set(key, "done", rows=n)
        done += 1
    meter.finish()
    return done


def download_ticks(dl: "Downloader", strikes: int = 10, days: int = 5, expiries: int = 2, log=print) -> int:
    """Tick history with best bid/ask for the last `days` trading days up to dl.end (TrueData keeps about 5):
    the index, its near-month future and each day's `expiries` nearest expiries, strikes within `strikes` steps
    of the day's range. Saved as data/truedata/ticks/<UNDERLYING>/<day>/<symbol>.csv.gz, one file per symbol-day.
    Resumable: finished symbol-days are recorded in progress.db."""
    und, step = dl.und, dl.step
    recent = [d for d in dl.weekdays()][-days:]
    if not recent:
        log("No trading days in that period.")
        return 0
    try:
        closes = dl.spot_closes()
    except TrueDataError:
        closes = pd.DataFrame(columns=["min", "max"])
    jobs = []
    for d in recent:
        syms = [dl.s["spot_symbol"], dl.s["future_symbol"]]
        cs: set = set()
        for s in BHAV_SEGMENTS.get(und, ["FO"]):    # contracts listed that day (bhavcopy saved by the contracts step)
            for day in (d, *[x for x in reversed(recent) if x < d]):
                df = _read_csv_safe(dl.root / "bhavcopy" / f"{s}_{day}.csv.gz")
                if df is not None and not df.empty:
                    cs = {c for c in contracts_in(df, und) if c.expiry >= d}
                    if cs:
                        break
            if cs:
                break
        known = closes[closes.index <= d]
        if not known.empty:
            row = known.iloc[-1]
            lo = int(row["min"]) // step * step - strikes * step
            hi = -(-int(row["max"]) // step) * step + strikes * step
            if cs:
                exps = sorted({c.expiry for c in cs})[:expiries]
                opts = sorted((c for c in cs if c.expiry in exps and lo <= c.strike <= hi),
                              key=lambda c: (c.expiry, abs(c.strike - (row["min"] + row["max"]) / 2)))
            else:   # no contract list: weekly calendar
                exps = weekly_expiries(d, d + timedelta(days=70), dl.cfg["market"]["expiry_rules"], None)
                if UNDERLYINGS.get(und, (0, 0, 0, False))[3]:
                    exps = sorted({(e.year, e.month): e for e in exps}.values())
                exps = [e for e in exps if e >= d][:expiries]
                opts = [Contract(und, e, k, r) for e in exps for k in range(lo, hi + step, step) for r in ("CE", "PE")]
            syms += [option_symbol(c) for c in opts]
        else:
            log(f"  {d}: no {und} price saved for that day - run the spot step first; index and future only")
        for sym in syms:
            jobs.append((f"TICK:{und}:{sym}:{d}", sym, d))
    todo = []
    for key, sym, d in jobs:
        st = dl.progress.get(key)
        if st and st[0] in ("done", "empty"):
            continue
        dl.progress.add(key, "tick", d, d, 0)
        todo.append((key, sym, d))
    log(f"{len(todo)} of {len(jobs)} {und} symbol-days of ticks to download ({recent[0]} to {recent[-1]})")
    meter = Meter(f"{und} ticks with bid/ask", len(todo), log, " symbol-days")
    done, failed, lock = [0], [], threading.Lock()

    def work(job):
        key, sym, d = job
        try:
            df = dl.client.ticks(sym, d)
        except NoData:
            dl.progress.set(key, "empty")
            df = None
        except Exception as e:   # one bad symbol must never stop the rest
            dl.progress.set(key, "error", note=f"{type(e).__name__}: {e}")
            with lock:
                failed.append(f"{sym} {d}: {e}")
            df = None
        if df is not None:
            if df.empty:
                dl.progress.set(key, "empty")
            else:
                path = dl.root / "ticks" / und / d.isoformat() / f"{_safe(sym)}.csv.gz"
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                with gzip.open(tmp, "wt", encoding="utf-8") as f:   # every tick kept (same-second ticks too)
                    df.to_csv(f, index=False)
                os.replace(tmp, path)
                dl.progress.set(key, "done", rows=len(df))
        with lock:
            done[0] += 1
            meter.show(done[0])

    # a quick check first: if TrueData refuses tick history altogether, say so instead of 1,000 errors
    if todo:
        work(todo[0])
        if failed and dl.progress.get(todo[0][0])[0] == "error":
            meter.finish()
            log(f"  TrueData refused the first tick request: {failed[0]}")
            return 0
    with ThreadPoolExecutor(max_workers=int(dl.s["workers"])) as pool:
        list(pool.map(work, todo[1:]))
    meter.finish()
    for e in failed[:10]:
        log("  error " + e)
    if failed:
        log(f"{len(failed)} failed; run again to retry them")
    got = sum(1 for k, _s, _d in jobs if (dl.progress.get(k) or ("",))[0] == "done")
    log(f"{und}: ticks saved for {got} of {len(jobs)} symbol-days")
    return got


def _safe(sym: str) -> str:
    return sym.replace("&", "and").replace(" ", "_")
