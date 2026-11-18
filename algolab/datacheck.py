"""Data review: finds missing bars and abnormal prices in the stored market data.

    python -m algolab.datacheck                      # NIFTY and BANKNIFTY, everything stored
    python -m algolab.datacheck --underlying NIFTY --from 2025-01-01
    python -m algolab.datacheck --requeue data/review/<run>   # re-download the contracts it flagged

Writes data/review/<date-time>/report.md (read this), issues.csv (one row per finding) and
redownload.txt (contracts worth fetching again). Severity: FAIL = the data is wrong and a backtest
would use it; WARN = missing or suspicious, worth a look; INFO = real market events or expected gaps.

What is checked
- Index (TD_<UNDERLYING>_1minute.csv, from TrueData): trading days missing against the exchange's own
  contract lists (bhavcopies), regular-session bars per day (375 = 09:15-15:29), bars outside the
  session, duplicates, broken OHLC, one-minute spikes that reverse (bad ticks), frozen prices, VIX gaps
  and range, and the index against its future (basis).
- Options (TrueData raw candles): download status per expiry, at-the-money coverage for every day each
  expiry was traded (what backtests need most), broken OHLC, bars after expiry or outside the session,
  prices below intrinsic value, reversing spikes, call/put prices that do not fall/rise with strike, and
  put-call parity at the money (catches swapped or mislabelled contracts).
"""
from __future__ import annotations

import argparse
import gzip
import sqlite3
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from .config import DATA, load_config
except ImportError:  # run as a plain script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from algolab.config import DATA, load_config


RATE, DIV = 0.065, 0.012     # interest and dividend yield for option price bounds (config.yaml pricing)


def _carry() -> float:
    """Yearly cost of carry (interest minus dividend yield) for the forward in put-call parity."""
    try:
        p = load_config().get("pricing", {})
        return float(p.get("risk_free_rate", 0.065)) - float(p.get("dividend_yield", 0.012))
    except Exception:
        return 0.053

REG_START, REG_END = time(9, 15), time(15, 29)      # bar start times of the regular session
# From 3 Aug 2026 NSE runs a closing auction in the cash market (15:15-15:35) and F&O trades to 15:40:
# index values stop moving continuously at 15:15, so index bars after 15:14 are expected to be missing
# or frozen. https://5paisa.com/blog/nse-extends-fo-trading-by-10-minutes
AUCTION_FROM, AUCTION_START = date(2026, 8, 3), time(15, 15)
PRE_START, CLOSE_END = time(9, 0), time(15, 59)     # pre-open auction / closing session
STEP = {"NIFTY": 50, "BANKNIFTY": 100, "SENSEX": 100}
# faults a fresh download can fix; prices that are odd but real (thin trading) come back the same
REFETCHABLE = {"unreadable file", "duplicate bars", "bars after expiry", "broken OHLC", "negative open interest"}
SPOT_SYMBOL = {"NIFTY": "NIFTY_50", "BANKNIFTY": "NIFTY_BANK"}
FUT_SYMBOL = {"NIFTY": "NIFTY-I", "BANKNIFTY": "BANKNIFTY-I", "SENSEX": "SENSEX-I"}


@dataclass
class Issues:
    rows: list = field(default_factory=list)
    special_days: set = field(default_factory=set)   # Muhurat / Budget sessions seen in the index

    def add(self, severity, check, dataset, key, detail, contract=""):
        self.rows.append(dict(severity=severity, check=check, dataset=dataset, key=str(key),
                              detail=detail, contract=contract))

    def frame(self):
        return pd.DataFrame(self.rows, columns=["severity", "check", "dataset", "key", "detail", "contract"])


def _session(t: pd.Series) -> pd.Series:
    tt = t.dt.time
    return np.select([(tt >= REG_START) & (tt <= REG_END), (tt >= PRE_START) & (tt < REG_START),
                      (tt > REG_END) & (tt <= CLOSE_END)], ["regular", "pre-open", "closing"], "off-hours")


def trading_calendar(root: Path) -> set[date]:
    """Days the exchange traded F&O, from the bhavcopy days TrueData returned (empty if none stored)."""
    db = root / "truedata" / "progress.db"
    if not db.exists():
        return set()
    with sqlite3.connect(db) as c:
        try:
            return {date.fromisoformat(d) for (d,) in c.execute("SELECT day FROM days WHERE status='ok'")
                    if ":" not in d}   # NSE F&O days (BSE days are stored as 'BSEFO:<date>')
        except sqlite3.Error:
            return set()


# ---- index ------------------------------------------------------------------
def check_index(und: str, root: Path, cal: set, issues: Issues, start=None, end=None) -> pd.DataFrame | None:
    ds = f"TD_{und}_1minute"
    path = root / "spot" / f"{ds}.csv"
    if not path.exists():
        issues.add("WARN", "missing file", ds, path.name, "no TrueData index file")
        return None
    s = pd.read_csv(path, parse_dates=["datetime"])
    if start:
        s = s[s.datetime >= pd.Timestamp(start)]
    if end:
        s = s[s.datetime < pd.Timestamp(end) + pd.Timedelta(days=1)]
    s = s.sort_values("datetime")
    dup = s.datetime.duplicated().sum()
    if dup:
        issues.add("FAIL", "duplicate bars", ds, f"{dup} rows", "same timestamp more than once")
    s = s.drop_duplicates("datetime")
    s["session"] = _session(s.datetime)
    s["day"] = s.datetime.dt.date

    # bars outside the regular session
    for kind, sev in (("off-hours", "WARN"), ("closing", "INFO"), ("pre-open", "INFO")):
        part = s[s.session == kind]
        if part.empty:
            continue
        if kind == "off-hours":
            for d, g in part.groupby("day"):
                times = f"{g.datetime.dt.strftime('%H:%M').iloc[0]}-{g.datetime.dt.strftime('%H:%M').iloc[-1]}"
                if g.close.nunique() == 1 and (g.high == g.low).all():
                    issues.add("WARN", "filler bars outside market hours", ds, d,
                               f"{len(g)} bars {times} frozen at {g.close.iloc[0]}; not real trading "
                               "(backtests ignore them)")
                elif len(g) > 30:
                    issues.special_days.add(d)
                    issues.add("INFO", "special session", ds, d, f"{len(g)} bars {times} (e.g. Diwali Muhurat trading)")
                else:
                    issues.add(sev, "bars outside market hours", ds, d, f"{len(g)} bars {times}")
        else:
            issues.add(sev, f"{kind} bars", ds, f"{part.day.nunique()} days",
                       f"{len(part)} bars; ignored by backtests" if kind != "pre-open" else
                       f"{len(part)} pre-open auction bars (09:00-09:14)")

    today = date.today()
    if (s.day >= today).any():
        issues.add("INFO", "day in progress", ds, today, "today's bars skipped (the day is not over)")
        s = s[s.day < today]
    reg = s[s.session == "regular"].copy()
    if (reg.day >= AUCTION_FROM).any():
        issues.add("INFO", "closing auction", ds, f"from {AUCTION_FROM}",
                   "index does not move continuously after 15:15 (cash-market closing auction); use futures or "
                   "options for prices after 15:15; F&O trades until 15:40")
    days = set(reg.day)
    any_days = set(s.day)
    lo, hi = (min(days), max(days)) if days else (None, None)

    # missing trading days
    if cal and days:
        expected = {d for d in cal if lo <= d <= hi}
        for d in sorted(expected - any_days):
            issues.add("FAIL", "missing day", ds, d, "exchange traded (bhavcopy exists) but no index bars")
        for d in sorted(days - expected):
            weekend = " (weekend: a special trading day, e.g. Union Budget)" if d.weekday() >= 5 else ""
            issues.add("INFO", "day not in exchange calendar", ds, d,
                       f"{(reg.day == d).sum()} bars; no bhavcopy for this day{weekend}")

    # bars per day
    per_day = reg.groupby("day").size()
    gap_starts: dict = {}
    for d, n in per_day.items():
        auction = d >= AUCTION_FROM
        if auction:   # only 09:15-15:14 counts; the closing auction freezes the index after that
            n = int((reg[reg.day == d].datetime.dt.time < AUCTION_START).sum())
        expect = 360 if auction else 375
        if n >= expect:
            continue
        if n > 375:
            issues.add("FAIL", "too many bars", ds, d, f"{n} regular bars (max 375)")
        elif n < expect:
            g = reg[reg.day == d].datetime
            if auction:
                g = g[g.dt.time < AUCTION_START]
            span = (g.max() - g.min()) / pd.Timedelta(minutes=1) + 1
            if n < 120 and span <= 120:        # one short block of trading: a special session
                issues.special_days.add(d)
                issues.add("INFO", "special session", ds, d,
                           f"{n} bars {g.min():%H:%M}-{g.max():%H:%M} (e.g. Diwali Muhurat trading)")
                continue
            last = (datetime.combine(d, AUCTION_START) - timedelta(minutes=1)) if auction else datetime.combine(d, REG_END)
            full = pd.date_range(datetime.combine(d, REG_START), last, freq="1min")
            missing = full.difference(g)
            runs = (pd.Series(missing).diff() != pd.Timedelta(minutes=1)).cumsum()
            longest = pd.Series(missing).groupby(runs).size().max() if len(missing) else 0
            first = missing[0].strftime("%H:%M") if len(missing) else ""
            sev = "FAIL" if n < 300 else "WARN" if (expect - n) >= 5 or longest >= 3 else "INFO"
            issues.add(sev, "missing minutes", ds, d, f"{expect - n} of {expect} missing; longest gap {longest} min from {first}")
            gap_starts[first] = gap_starts.get(first, []) + [d]

    for start_t, ds_days in gap_starts.items():
        if len(ds_days) >= 5:
            issues.add("WARN", "recurring gap", ds, f"{len(ds_days)} days",
                       f"minutes missing from about {start_t} on {len(ds_days)} days, {min(ds_days)} to {max(ds_days)}")

    # OHLC sanity
    bad = reg[(reg.high < reg[["open", "close"]].max(axis=1) - 1e-9) |
              (reg.low > reg[["open", "close"]].min(axis=1) + 1e-9) | (reg.low <= 0)]
    if len(bad):
        issues.add("FAIL", "broken OHLC", ds, f"{len(bad)} bars",
                   f"high/low inconsistent with open/close, e.g. {bad.datetime.iloc[0]}")

    # spikes that reverse (bad ticks) vs real big moves
    reg["ret"] = reg.groupby("day").close.pct_change()
    nxt = reg.groupby("day").ret.shift(-1)
    spike = reg[(reg.ret.abs() > 0.008) & (nxt.abs() > 0.8 * reg.ret.abs()) & (np.sign(nxt) == -np.sign(reg.ret))]
    for _, r in spike.iterrows():
        issues.add("FAIL", "spike that reverses", ds, r.datetime, f"{r.ret:+.2%} then back: probable bad tick")
    big = reg[reg.ret.abs() > 0.008].drop(spike.index, errors="ignore")
    for _, r in big.loc[big.ret.abs().sort_values(ascending=False).index[:10]].iterrows():
        issues.add("INFO", "big one-minute move", ds, r.datetime, f"{r.ret:+.2%} (not reversed: likely real)")

    # opening gaps vs previous close (real events, for context)
    closes = reg.groupby("day").close.last()
    opens = reg.groupby("day").open.first()
    gap = (opens / closes.shift(1) - 1).dropna()
    for d, g in gap[gap.abs() > 0.015].items():
        issues.add("INFO", "opening gap", ds, d, f"{g:+.2%} from previous close")

    # frozen prices
    same = reg.close.eq(reg.close.shift()) & reg.day.eq(reg.day.shift())
    run_id = (~same).cumsum()
    runs = reg.groupby(run_id).agg(day=("day", "first"), start=("datetime", "first"), n=("close", "size"))
    runs = runs[~((runs.day >= AUCTION_FROM) & (runs.start.dt.time >= time(15, 10)))]   # closing auction
    for _, r in runs[runs.n >= 15].iterrows():
        issues.add("WARN", "frozen price", ds, r.day, f"close unchanged for {r.n} minutes from {r.start:%H:%M}")

    # VIX
    if "vix" in reg:
        nan_days = reg[reg.vix.isna()].groupby("day").size()
        for d, n in nan_days[nan_days > 5].items():
            issues.add("WARN", "VIX missing", ds, d, f"{n} regular bars without VIX")
        outr = reg[(reg.vix < 5) | (reg.vix > 90)]
        if len(outr):
            issues.add("FAIL", "VIX out of range", ds, f"{len(outr)} bars", f"e.g. {outr.datetime.iloc[0]}: {outr.vix.iloc[0]}")

    # index vs its future
    fut = root / "truedata" / "underlying" / f"{FUT_SYMBOL[und]}_1min.csv.gz"
    if fut.exists():
        f = pd.read_csv(fut, parse_dates=["timestamp"]).rename(columns={"timestamp": "datetime", "close": "fut"})
        m = reg.merge(f[["datetime", "fut"]], on="datetime")
        if len(m):
            m["basis"] = m.fut / m.close - 1
            daily = m.groupby("day").basis.median()
            for d, b in daily[(daily < -0.005) | (daily > 0.025)].items():
                issues.add("WARN", "index vs future", ds, d, f"median basis {b:+.2%} (expected about 0% to +1.5%)")
    cross_check_zerodha(und, root, reg, issues)
    return reg[["datetime", "day", "close", "high", "low"]]


ZERODHA_MINUTE = {"NIFTY": "NIFTY_minute.csv"}


def cross_check_zerodha(und, root, reg, issues) -> None:
    """Compare TrueData's index with Zerodha's 1-minute candles where both exist."""
    name = ZERODHA_MINUTE.get(und)
    path = root / "spot" / name if name else None
    if not path or not path.exists():
        return
    ds = f"TD_{und}_1minute"
    z = pd.read_csv(path, parse_dates=["datetime"])
    z = z[(z.datetime.dt.time >= REG_START) & (z.datetime.dt.time <= REG_END)]
    m = reg.merge(z[["datetime", "close"]], on="datetime", suffixes=("", "_z"))
    if len(m):
        diff = (m.close - m.close_z).abs()
        tol = 0.0005 * m.close + 1
        far = m[diff > tol]
        sev = "FAIL" if len(far) > 0.01 * len(m) else "WARN" if len(far) else "INFO"
        issues.add(sev, "TrueData vs Zerodha", ds, f"{len(m)} common minutes",
                   f"median difference {diff.median():.2f} pts, largest {diff.max():.2f}; "
                   f"{len(far)} minutes differ by more than 0.05%")
    lo, hi = reg.datetime.min(), reg.datetime.max()
    zin = z[(z.datetime >= lo) & (z.datetime <= hi) & z.datetime.dt.date.isin(set(reg.day))]
    fill = zin[~zin.datetime.isin(reg.datetime)]
    if len(fill):
        issues.add("WARN", "index gaps Zerodha can fill", ds, f"{len(fill)} minutes on {fill.datetime.dt.date.nunique()} days",
                   f"fill with: python -m algolab.datacheck --fill-index {und}")


def fill_index(und: str, root: Path) -> int:
    """Add Zerodha's 1-minute index bars where TrueData's are missing (never overwrites). Keeps a backup."""
    td_path, z_path = root / "spot" / f"TD_{und}_1minute.csv", root / "spot" / ZERODHA_MINUTE[und]
    td = pd.read_csv(td_path, parse_dates=["datetime"])
    z = pd.read_csv(z_path, parse_dates=["datetime"])
    z = z[(z.datetime.dt.time >= REG_START) & (z.datetime.dt.time <= REG_END) &
          z.datetime.dt.date.isin(set(td.datetime.dt.date)) & ~z.datetime.isin(td.datetime)]
    if z.empty:
        return 0
    backup = td_path.with_name(td_path.stem + f".before-fill-{datetime.now():%Y%m%d-%H%M}.csv")
    td.to_csv(backup, index=False)
    add = z[[c for c in td.columns if c in z.columns]]
    out = pd.concat([td, add]).sort_values("datetime").drop_duplicates("datetime", keep="first")
    if "vix" in out:   # VIX for added minutes: the nearest earlier value that day
        out["vix"] = out.groupby(out.datetime.dt.date).vix.ffill()
    out.to_csv(td_path, index=False)
    return len(add)


# ---- options ----------------------------------------------------------------
def _read(path: Path) -> pd.DataFrame:
    try:
        with gzip.open(path, "rt") as f:
            df = pd.read_csv(f, parse_dates=["timestamp"])
    except (OSError, ValueError, EOFError) as e:
        return pd.DataFrame({"error": [str(e)]})
    return df


def _check_expiry(args):
    """Per-contract checks for one expiry folder (runs in a worker process)."""
    und, exp_dir, spot_path, special = args
    exp = date.fromisoformat(exp_dir.name)
    spot = pd.read_pickle(spot_path)
    rows, closes = [], {}
    for p in sorted(exp_dir.glob("*.csv.gz")):
        k, right = float(p.name[:-9]), p.name[-9:-7]
        c = f"{und} {exp} {int(k)} {right}"
        df = _read(p)
        if "error" in df:
            rows.append(("FAIL", "unreadable file", c, p.name, df.error.iloc[0][:100]))
            continue
        if df.empty:
            continue
        if df.timestamp.duplicated().any():
            rows.append(("FAIL", "duplicate bars", c, p.name, f"{df.timestamp.duplicated().sum()} duplicate timestamps"))
            df = df.drop_duplicates("timestamp")
        after = df[df.timestamp.dt.date > exp]
        if len(after):
            rows.append(("FAIL", "bars after expiry", c, p.name, f"{len(after)} bars after {exp}: wrong contract?"))
        sess = _session(df.timestamp)
        offb = df[sess == "off-hours"]
        if len(offb):
            on_special = offb.timestamp.dt.date.isin(special)
            if (~on_special).any():
                rows.append(("WARN", "bars outside market hours", c, p.name,
                             f"{(~on_special).sum()} bars, e.g. {offb.timestamp[~on_special].iloc[0]}"))
        df = df[sess == "regular"]
        bad = df[(df.high < df[["open", "close"]].max(axis=1) - 1e-9) | (df.low > df[["open", "close"]].min(axis=1) + 1e-9)
                 | (df.close <= 0)]
        if len(bad):
            rows.append(("FAIL", "broken OHLC", c, p.name, f"{len(bad)} bars, e.g. {bad.timestamp.iloc[0]}"))
        if "oi" in df and (df.oi < 0).any():
            rows.append(("FAIL", "negative open interest", c, p.name, f"{(df.oi < 0).sum()} bars"))
        # reversing spikes on prices worth looking at (a day's first trade is often an odd print)
        first = df.timestamp.dt.date.ne(df.timestamp.dt.date.shift())
        r = df.close.pct_change().where(~first)
        nxt = r.shift(-1)
        sp = df[(df.close.shift() > 5) & (r.abs() > 0.5) & (nxt.abs() > 0.3) & (np.sign(nxt) == -np.sign(r))
                & (df.volume > 0)]
        if len(sp):
            rows.append(("WARN", "spike that reverses", c, p.name,
                         f"{len(sp)} bars, e.g. {sp.timestamp.iloc[0]} {r[sp.index[0]]:+.0%}: probable bad tick"))
        # below intrinsic value: near-the-money strikes, traded bars, after the opening minute, against the
        # most favourable index level of that minute (an option trade can happen at the minute's high or low)
        m = df[(df.volume > 0) & (df.timestamp.dt.time > REG_START)].merge(
            spot, left_on="timestamp", right_on="datetime", how="inner", suffixes=("", "_s"))
        m = m[(m.close_s - k).abs() <= 0.03 * m.close_s]
        # expiry day after 15:00: contracts settle on the last 30 minutes' average index, not the live price
        m = m[~((m.timestamp.dt.date == exp) & (m.timestamp.dt.time >= time(15, 0)))]
        if len(m):
            t = ((pd.Timestamp(exp) + pd.Timedelta(hours=15, minutes=30) - m.timestamp).dt.total_seconds()
                 / (365 * 86400)).clip(lower=0)
            df_r, df_q = np.exp(-RATE * t), np.exp(-DIV * t)
            # European lower bounds: call >= S.e^-qt - K.e^-rt, put >= K.e^-rt - S.e^-qt
            intrinsic = ((m.low_s * df_q - k * df_r) if right == "CE" else (k * df_r - m.high_s * df_q)).clip(lower=0)
            tol = 0.002 * m.close_s + 2
            below = m[m.close < intrinsic - tol]
            if len(below) > max(3, 0.02 * len(m)):
                rows.append(("FAIL", "below intrinsic value", c, p.name,
                             f"{len(below)} of {len(m)} traded near-the-money bars, e.g. {below.timestamp.iloc[0]}: "
                             f"{below.close.iloc[0]} vs intrinsic {intrinsic.loc[below.index[0]]:.1f}"))
        closes[(k, right)] = df.set_index("timestamp")[["close", "volume"]]
    return und, exp, rows, closes


def check_options(und: str, root: Path, spot: pd.DataFrame, cal: set, issues: Issues, work: Path,
                  start=None, end=None, workers: int = 4, log=print) -> list[str]:
    """Returns the TrueData symbols worth re-downloading."""
    base = root / "truedata" / "options" / und
    ds = f"{und} options"
    if not base.exists():
        issues.add("WARN", "missing folder", ds, str(base), "no TrueData option candles stored")
        return []
    exps = sorted(p for p in base.iterdir() if p.is_dir())
    if start:
        exps = [p for p in exps if p.name >= str(start)]
    if end:
        exps = [p for p in exps if p.name <= str(end)]
    redo: set[str] = set()

    # download status from progress.db
    db = root / "truedata" / "progress.db"
    windows = {}
    if db.exists():
        q = "SELECT key, expiry, strike, right, start, end, status FROM items WHERE kind='option' AND key GLOB ?"
        try:
            with sqlite3.connect(db) as c:
                prog = pd.read_sql_query(q, c, params=(f"{und}[0-9]*",))
        except (sqlite3.Error, pd.errors.DatabaseError):
            prog = pd.DataFrame(columns=["key", "expiry", "strike", "right", "start", "end", "status"])
        for e, g in prog.groupby("expiry"):
            st = g.status.value_counts().to_dict()
            if st.get("pending") or st.get("error"):
                issues.add("WARN", "download unfinished", ds, e,
                           ", ".join(f"{v} {k}" for k, v in sorted(st.items())))
            windows[e] = (date.fromisoformat(g.start.min()), date.fromisoformat(g.end.max()))
        for _, r in prog[prog.status == "error"].iterrows():
            redo.add(r.key)

    spot_path = work / f"spot_{und}.pkl"
    spot.to_pickle(spot_path)
    from .truedata import Meter
    meter = Meter(f"Reviewing {und} options", len(exps), log, " expiries")
    per_exp = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(_check_expiry, (und, p, spot_path, set(issues.special_days))) for p in exps]
        for i, f in enumerate(as_completed(futs), 1):
            u, exp, rows, closes = f.result()
            for sev, chk, cname, key, detail in rows:
                issues.add(sev, chk, ds, key, detail, contract=cname)
                if chk in REFETCHABLE:
                    k, right = cname.split()[2], cname.split()[3]
                    redo.add(f"{und}{exp:%y%m%d}{k}{right}")
            per_exp[exp] = closes
            meter.show(i)
    meter.finish()

    # at-the-money coverage, strike ordering and parity, day by day
    step = STEP.get(und, 50)
    daily_spot = spot.groupby("day").close.agg(["first", "last", "min", "max"])
    mid = spot[spot.datetime.dt.time == time(12, 30)].set_index("day").close
    full, total, near_full, near_total = 0, 0, 0, 0
    carry = _carry()
    for exp, closes in sorted(per_exp.items()):
        if not closes:
            continue
        a, b = windows.get(exp.isoformat(), (exp - timedelta(days=7), exp))
        days = [d for d in daily_spot.index if a <= d <= b and (not cal or d in cal)]
        for d in days:
            total += 1
            near_total += (exp - d).days <= 7
            s_mid = mid.get(d, daily_spot.loc[d, "last"])
            atm = round(s_mid / step) * step
            gaps = []
            for k in (atm - step, atm, atm + step):
                for right in ("CE", "PE"):
                    c = closes.get((float(k), right))
                    n = 0 if c is None else ((c.index.date == d) & (c.volume > 0)).sum()
                    need = 150 if (exp - d).days <= 7 else 40
                    if n < need:
                        gaps.append(f"{int(k)}{right}:{n}")
                        if c is None:
                            redo.add(f"{und}{exp:%y%m%d}{int(k)}{right}")
            if not gaps:
                full += 1
                near_full += (exp - d).days <= 7
            else:
                near = (exp - d).days <= 7
                issues.add("WARN" if near else "INFO", "thin at-the-money data", ds, f"{d} exp {exp}",
                           f"traded minutes (need {150 if near else 40}): " + ", ".join(gaps), contract=f"{und} {exp}")
            # ordering across strikes and put-call parity at 12:30
            ts = pd.Timestamp(datetime.combine(d, time(12, 30)))
            snap = {}
            for (k, right), c in closes.items():
                if abs(k - s_mid) <= 6 * step and ts in c.index and c.loc[ts, "volume"] > 0:
                    snap[(k, right)] = float(c.loc[ts, "close"])
            for right, sign in (("CE", -1), ("PE", 1)):
                ks = sorted(k for k, r in snap if r == right)
                for k1, k2 in zip(ks, ks[1:]):
                    p1, p2 = snap[(k1, right)], snap[(k2, right)]
                    if sign * (p2 - p1) < -max(2.0, 0.05 * max(p1, p2)):
                        issues.add("WARN", "price order across strikes", ds, f"{ts:%Y-%m-%d %H:%M} exp {exp}",
                                   f"{right} {int(k1)}={p1} vs {int(k2)}={p2}", contract=f"{und} {exp}")
            if (atm, "CE") in snap and (atm, "PE") in snap:
                lhs = snap[(atm, "CE")] - snap[(atm, "PE")]
                t = max((exp - d).days, 0) / 365
                fwd = s_mid * np.exp(carry * t)
                rhs = (fwd - atm) * np.exp(-0.065 * t)
                if abs(lhs - rhs) > 0.004 * s_mid + 5:
                    issues.add("FAIL", "put-call parity", ds, f"{ts:%Y-%m-%d %H:%M} exp {exp}",
                               f"CE-PE at {int(atm)} = {lhs:.1f}, forward-strike = {rhs:.1f}: check labels/expiry",
                               contract=f"{und} {exp}")
    if near_total:
        issues.add("INFO", "at-the-money coverage", ds, f"{near_full} of {near_total} expiry-days",
                   f"{near_full / near_total:.0%} within 7 days of expiry have the at-the-money and next strikes "
                   f"well traded ({full / total:.0%} over whole windows, where far expiries trade thinly)")
    return sorted(redo)


# ---- report ------------------------------------------------------------------
def write_report(issues: Issues, out: Path, redo: list[str], args) -> Path:
    df = issues.frame()
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "issues.csv", index=False)
    (out / "redownload.txt").write_text("\n".join(redo) + ("\n" if redo else ""))
    lines = [f"# Data review {datetime.now():%Y-%m-%d %H:%M}", "",
             f"Underlyings: {', '.join(args.underlying)}; period: {args.start or 'all'} to {args.end or 'all'}.", ""]
    counts = df.groupby(["severity"]).size().reindex(["FAIL", "WARN", "INFO"]).fillna(0).astype(int)
    verdict = "FAIL" if counts["FAIL"] else "WARN" if counts["WARN"] else "OK"
    lines += [f"**Verdict: {verdict}** — {counts['FAIL']} FAIL, {counts['WARN']} WARN, {counts['INFO']} INFO.", ""]
    if len(df):
        lines += ["## By check", "", "| Severity | Data set | Check | Findings |", "|---|---|---|---|"]
        g = df.groupby(["severity", "dataset", "check"]).size().reset_index(name="n")
        g["o"] = g.severity.map({"FAIL": 0, "WARN": 1, "INFO": 2})
        for _, r in g.sort_values(["o", "dataset", "n"], ascending=[True, True, False]).iterrows():
            lines.append(f"| {r.severity} | {r.dataset} | {r.check} | {r.n} |")
        for sev in ("FAIL", "WARN"):
            part = df[df.severity == sev]
            if len(part):
                lines += ["", f"## {sev} examples (first 5 per check)", ""]
                for (dsn, chk), grp in part.groupby(["dataset", "check"]):
                    lines.append(f"**{dsn} — {chk}** ({len(grp)})")
                    for _, r in grp.head(5).iterrows():
                        lines.append(f"- {r.key}: {r.detail}" + (f" ({r.contract})" if r.contract else ""))
                    lines.append("")
        info = df[(df.severity == "INFO") & df.check.isin(["at-the-money coverage", "special session",
                                                                 "opening gap", "big one-minute move"])]
        if len(info):
            lines += ["## Context (real market events and coverage)", ""]
            for _, r in info.head(30).iterrows():
                lines.append(f"- {r.dataset} — {r.check}: {r.key}: {r.detail}")
    lines += ["", f"{len(redo)} contracts listed in redownload.txt. To fetch them again:",
              f"`python -m algolab.datacheck --requeue {out}` then `python run.py truedata options` "
              "(add `--underlying BANKNIFTY` for Bank Nifty)."]
    path = out / "report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def requeue(run_dir: Path, root: Path) -> int:
    keys = [k.strip() for k in (run_dir / "redownload.txt").read_text().split() if k.strip()]
    db = root / "truedata" / "progress.db"
    with sqlite3.connect(db) as c:
        n = 0
        for k in keys:
            n += c.execute("UPDATE items SET status='pending' WHERE key=?", (k,)).rowcount
        c.commit()
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--underlying", nargs="+", default=["NIFTY", "BANKNIFTY", "SENSEX"])
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--data", default=str(DATA), help="data folder (default: this Algo Lab's data/)")
    ap.add_argument("--out", help="report folder (default: data/review/<date-time>)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--no-options", action="store_true", help="index checks only (fast)")
    ap.add_argument("--requeue", metavar="REVIEW_FOLDER", help="mark that review's redownload.txt contracts for download")
    ap.add_argument("--fill-index", metavar="UNDERLYING", help="fill TrueData index gaps from Zerodha 1-minute bars")
    args = ap.parse_args(argv)
    root = Path(args.data)
    if args.fill_index:
        n = fill_index(args.fill_index.upper(), root)
        print(f"Added {n} Zerodha minutes to TD_{args.fill_index.upper()}_1minute (a backup of the old file was kept).")
        return 0
    if args.requeue:
        print(f"{requeue(Path(args.requeue), root)} contracts marked for download. "
              "Now run: python run.py truedata options   (add --underlying BANKNIFTY for Bank Nifty)")
        return 0
    out = Path(args.out) if args.out else root / "review" / datetime.now().strftime("%Y%m%d-%H%M")
    work = out / ".work"
    work.mkdir(parents=True, exist_ok=True)
    cal = trading_calendar(root)
    issues, redo = Issues(), []
    for und in args.underlying:
        print(f"{und}: index")
        spot = check_index(und, root, cal, issues, args.start, args.end)
        if spot is not None and not args.no_options:
            redo += check_options(und, root, spot, cal, issues, work, args.start, args.end, args.workers)
    for p in work.glob("*"):
        p.unlink()
    work.rmdir()
    path = write_report(issues, out, redo, args)
    df = issues.frame()
    print(f"\nFAIL {(df.severity == 'FAIL').sum()}  WARN {(df.severity == 'WARN').sum()}  "
          f"INFO {(df.severity == 'INFO').sum()}\nReport: {path}")
    return 1 if (df.severity == "FAIL").any() else 0


if __name__ == "__main__":
    raise SystemExit(main())
