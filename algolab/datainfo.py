"""What market data is stored on this computer, read quickly enough for the dashboard's Data tab.

Option files are not read through: each contract's first and last price come from the first and last lines,
and its number of prices is estimated from the file size (exact for small files). Index files are read once.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .config import DATA

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CONTRACT = re.compile(r"^(\d+)(CE|PE)\.csv$")
UNDERLYINGS = ("NIFTY", "BANKNIFTY", "SENSEX")


def edges(path: Path) -> dict:
    """First and last time, last price and number of prices of a `time,last,bid,ask` file."""
    size = path.stat().st_size
    with path.open("rb") as f:
        header = f.readline().decode("utf-8", "replace").strip()
        first = f.readline().decode("utf-8", "replace").strip()
        head = f.tell()
        if size <= 1 << 20:
            rows = (1 if first else 0) + f.read().count(b"\n")
        else:
            rows = None
        f.seek(max(0, size - 4096))
        tail = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    last = tail[-1].strip() if tail else first
    if not first or last == header:
        return {"from": "", "to": "", "last": None, "rows": 0}
    if rows is None:
        avg = sum(len(x) + 1 for x in tail[1:]) / max(1, len(tail) - 1) if len(tail) > 1 else len(first) + 1
        rows = int(round((size - head) / max(avg, 1))) + 1
    p = last.split(",")
    try:
        lastp = float(p[1])
    except (IndexError, ValueError):
        lastp = None
    return {"from": first[:16], "to": last[:16], "last": lastp, "rows": rows}


def underlyings(root: Path = DATA) -> list[str]:
    have = {p.name for p in (root / "options").iterdir() if p.is_dir()} if (root / "options").is_dir() else set()
    have |= {u for u in UNDERLYINGS if index_files(u, root)}
    return [u for u in UNDERLYINGS if u in have] + sorted(have - set(UNDERLYINGS))


def index_files(underlying: str, root: Path = DATA) -> list[Path]:
    """Index bar files of an underlying, best first: TrueData 1-minute, Zerodha 1-minute, then 5-minute."""
    d = root / "spot"
    if not d.is_dir():
        return []
    u = underlying.upper()
    files = [f for f in d.glob("*.csv") if not f.name.startswith("SAMPLE")
             and re.search(rf"(^|_){u}_", f.stem.upper()) and not (u == "NIFTY" and "BANKNIFTY" in f.stem.upper())]
    return sorted(files, key=lambda f: (0 if f.stem.startswith("TD_") else 1 if "5minute" not in f.stem else 2, f.stem))


def index_days(path: Path) -> pd.DataFrame:
    """One row per day: bars, close (last bar) and India VIX if the file has it."""
    df = pd.read_csv(path, usecols=lambda c: c in ("datetime", "close", "vix"))
    df["day"] = df["datetime"].str[:10]
    g = df.groupby("day")
    out = pd.DataFrame({"bars": g.size(), "close": g["close"].last()})
    if "vix" in df:
        out["vix"] = g["vix"].last()
    return out.reset_index()


def index_bars(path: Path, days: int) -> pd.DataFrame:
    """The bars of the last `days` days (reads the whole file; use for short ranges)."""
    df = pd.read_csv(path, usecols=lambda c: c in ("datetime", "close", "vix"))
    keep = sorted(df["datetime"].str[:10].unique())[-days:]
    return df[df["datetime"].str[:10] >= keep[0]] if keep else df


def contracts(underlying: str, root: Path = DATA) -> pd.DataFrame:
    """Every stored option contract of an underlying: expiry, strike, right, first/last time, last price, prices."""
    base = root / "options" / underlying
    rows = []
    if base.is_dir():
        for e in os.scandir(base):
            if not (e.is_dir() and _DAY.match(e.name)):
                continue
            for f in os.scandir(e.path):
                m = _CONTRACT.match(f.name)
                if not m:
                    continue
                info = edges(Path(f.path))
                if info["rows"]:
                    rows.append({"expiry": e.name, "strike": int(m.group(1)), "right": m.group(2), **info})
    return pd.DataFrame(rows, columns=["expiry", "strike", "right", "from", "to", "last", "rows"])


def expiries(cons: pd.DataFrame) -> pd.DataFrame:
    if cons.empty:
        return pd.DataFrame(columns=["expiry", "contracts", "calls", "puts", "low", "high", "from", "to", "prices"])
    g = cons.groupby("expiry")
    return pd.DataFrame({
        "contracts": g.size(), "calls": g["right"].apply(lambda r: (r == "CE").sum()),
        "puts": g["right"].apply(lambda r: (r == "PE").sum()), "low": g["strike"].min(), "high": g["strike"].max(),
        "from": g["from"].min(), "to": g["to"].max(), "prices": g["rows"].sum()}).reset_index().sort_values("expiry")


def coverage(cons: pd.DataFrame, days: pd.DataFrame | None) -> pd.DataFrame:
    """Per weekday: index bars stored and option contracts with prices that day (every weekday between a
    contract's first and last price counts)."""
    count: dict[str, int] = {}
    for a, b in zip(cons["from"].str[:10], cons["to"].str[:10]):
        d, end = date.fromisoformat(a), date.fromisoformat(b)
        while d <= end:
            if d.weekday() < 5:
                k = d.isoformat()
                count[k] = count.get(k, 0) + 1
            d += timedelta(days=1)
    bars = dict(zip(days["day"], days["bars"])) if days is not None and len(days) else {}
    keys = sorted(set(count) | set(bars))
    return pd.DataFrame({"day": keys, "bars": [int(bars.get(k, 0)) for k in keys],
                         "contracts": [count.get(k, 0) for k in keys]})


def calendar_grid(cov: pd.DataFrame, value: str = "contracts"):
    """(weeks, z, text): one column per week (Monday), rows Monday..Friday, for a heatmap."""
    if cov.empty:
        return [], [], []
    by = dict(zip(cov["day"], cov.index))
    first = date.fromisoformat(cov["day"].iloc[0])
    last = date.fromisoformat(cov["day"].iloc[-1])
    monday = first - timedelta(days=first.weekday())
    weeks, z, text = [], [[] for _ in range(5)], [[] for _ in range(5)]
    while monday <= last:
        weeks.append(monday.isoformat())
        for i in range(5):
            d = (monday + timedelta(days=i)).isoformat()
            if d in by:
                r = cov.loc[by[d]]
                z[i].append(float(r[value]) if r[value] else (0.001 if r["bars"] else None))
                text[i].append(f"{d}: {int(r['bars']):,} index bars, {int(r['contracts']):,} option contracts")
            else:
                z[i].append(None)
                text[i].append(f"{d}: nothing stored")
        monday += timedelta(days=7)
    return weeks, z, text


def folder_sizes(root: Path = DATA) -> pd.DataFrame:
    """Space used under data/, by part (options per underlying, index bars, each TrueData folder, others)."""
    def size(p: Path) -> int:
        if p.is_file():
            return p.stat().st_size
        tot = 0
        for r, _, files in os.walk(p):
            for n in files:
                try:
                    tot += os.stat(os.path.join(r, n)).st_size
                except OSError:
                    pass
        return tot
    names = {"spot": "Index bars", "eod": "NSE closing prices", "review": "Data reviews"}
    parts = []
    if root.is_dir():
        for p in sorted(root.iterdir()):
            if p.name == "options" and p.is_dir():
                parts += [(f"Options {q.name}", size(q)) for q in sorted(p.iterdir()) if q.is_dir()]
            elif p.name == "truedata" and p.is_dir():
                parts += [(f"TrueData raw: {q.name}", size(q)) for q in sorted(p.iterdir())]
            elif not p.name.startswith("."):
                parts.append((names.get(p.name, p.name), size(p)))
    df = pd.DataFrame(parts, columns=["part", "bytes"])
    return df[df.bytes > 0].reset_index(drop=True)


def truedata_progress(root: Path = DATA) -> pd.DataFrame:
    """The TrueData download record: per underlying (and stocks / stock futures), how many are done, never
    traded, waiting or failed, and how many 1-minute candles."""
    db = root / "truedata" / "progress.db"
    if not db.exists():
        return pd.DataFrame(columns=["what", "status", "items", "candles"])
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        q = """select case when key like 'EQ:%' then 'Stocks' when key like 'FUT:%' then 'Stock futures'
                           when key like 'BANKNIFTY%' then 'BANKNIFTY' when key like 'SENSEX%' then 'SENSEX'
                           when key like 'NIFTY%' then 'NIFTY' else 'Other' end as what,
                      status, count(*) as items, coalesce(sum(rows), 0) as candles
               from items group by what, status"""
        return pd.read_sql_query(q, con)
    finally:
        con.close()


def latest_review(root: Path = DATA) -> dict | None:
    """The newest data review (python -m algolab.datacheck): when, verdict and counts by severity."""
    d = root / "review"
    runs = sorted((p for p in d.iterdir() if (p / "issues.csv").exists()), reverse=True) if d.is_dir() else []
    if not runs:
        return None
    run = runs[0]
    df = pd.read_csv(run / "issues.csv")
    counts = df["severity"].value_counts().to_dict() if len(df) else {}
    verdict = "FAIL" if counts.get("FAIL") else "WARN" if counts.get("WARN") else "OK"
    redo = (run / "redownload.txt").read_text().split() if (run / "redownload.txt").exists() else []
    return {"run": run.name, "folder": run, "verdict": verdict, "fail": counts.get("FAIL", 0), "warn": counts.get("WARN", 0),
            "info": counts.get("INFO", 0), "redownload": len(redo), "issues": df}
