"""One price set per index: the 1-minute bars backtests, the Data page and the Backtests page use.

  data/spot/NIFTY_minute.csv, BANKNIFTY_minute.csv, SENSEX_minute.csv     datetime,open,high,low,close,vix

Each is the TrueData 1-minute history (Oct 2024 on) followed by Zerodha's 1-minute bars, topped up after every
close by the server (`run.py collect`, kite.collect_minute) and copied to the Mac with the server's data.
Next to them, 1-second bars of the last few trading days (algolab/sec1.py, data/sec1/).

Older copies are moved out of the way into data/archive/spot/ (`organize`, run by `run.py collect` and
`run.py data-organize`): the TrueData-only 1-minute files once their bars are in the index's set and backups made
before a fill (`*.before-fill-*.csv`). Nothing is deleted. The sample file ships with the code and stays, hidden. NIFTY_5minute.csv stays where it
is because the nightly job and straddle_trend_switch still read it, but it is no longer offered on the screens.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from .config import SPOT_DIR

INDICES = ("NIFTY", "BANKNIFTY", "SENSEX")
# Zerodha index for each: (exchange:tradingsymbol for quotes, instrument token for history)
KITE_INDEX = {"NIFTY": ("NSE:NIFTY 50", 256265), "BANKNIFTY": ("NSE:NIFTY BANK", 260105), "SENSEX": ("BSE:SENSEX", 265)}


def canonical(underlying: str) -> str:
    return f"{underlying.upper()}_minute"


def archive_dir(spot_dir: Path = SPOT_DIR) -> Path:
    return spot_dir.parent / "archive" / "spot"


def screen_datasets(spot_dir: Path = SPOT_DIR) -> list[str]:
    """The price sets the Data and Backtests screens offer: one 1-minute set per index that has one."""
    return [canonical(u) for u in INDICES if (spot_dir / f"{canonical(u)}.csv").exists()]


def _truedata_file(underlying: str, spot_dir: Path) -> Path | None:
    name = f"TD_{underlying.upper()}_1minute.csv"
    for p in (spot_dir / name, archive_dir(spot_dir) / name):
        if p.exists():
            return p
    return None


def seed_from_truedata(underlying: str, spot_dir: Path = SPOT_DIR) -> int:
    """Put the TrueData 1-minute history in front of the index's set (bars older than its first bar, or all of
    them when the set does not exist yet). Bars already there win. Returns the number of bars added."""
    td = _truedata_file(underlying, spot_dir)
    if td is None:
        return 0
    path = spot_dir / f"{canonical(underlying)}.csv"
    old = pd.read_csv(td, parse_dates=["datetime"])
    if "vix" not in old:
        old["vix"] = float("nan")
    if path.exists():
        mine = pd.read_csv(path, parse_dates=["datetime"])
        old = old[old["datetime"] < mine["datetime"].min()]
        if old.empty:
            return 0
        df = pd.concat([old[[c for c in mine.columns if c in old.columns]], mine])
    else:
        df = old
    spot_dir.mkdir(parents=True, exist_ok=True)
    df.drop_duplicates("datetime", keep="last").sort_values("datetime").to_csv(path, index=False)
    return len(old)


def _edges(path: Path):
    s = pd.read_csv(path, usecols=["datetime"], parse_dates=["datetime"])["datetime"]
    return s.min(), s.max()


def _move(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    k = 1
    while dest.exists():
        dest = dest_dir / f"{src.stem}.{k}{src.suffix}"
        k += 1
    shutil.move(str(src), str(dest))
    return dest


def organize(spot_dir: Path = SPOT_DIR, dry_run: bool = False) -> dict:
    """Seed each index's 1-minute set from TrueData, then move the old copies to data/archive/spot/.
    A TrueData file is moved only when the index's set covers all its days."""
    arch = archive_dir(spot_dir)
    out = {"seeded": {}, "moved": [], "kept": [], "sets": []}
    if not spot_dir.is_dir():
        return out
    for u in INDICES:
        if not dry_run:
            n = seed_from_truedata(u, spot_dir)
            if n:
                out["seeded"][canonical(u)] = n
    for f in sorted(spot_dir.glob("*.csv")):
        why = None
        if f.name.startswith("SAMPLE_"):
            continue                       # part of the code (tests, run.py sample-data); never offered on screens
        if ".before-fill-" in f.name:
            why = "backup made before a fill"
        elif f.stem.startswith("TD_") and f.stem.endswith("_1minute"):
            u = f.stem[3:-len("_1minute")]
            mine = spot_dir / f"{canonical(u)}.csv"
            if u in INDICES and mine.exists():
                try:
                    (a0, a1), (b0, b1) = _edges(f), _edges(mine)
                except (OSError, ValueError, KeyError):
                    a0 = b0 = None
                if a0 is not None and b0 <= a0 and b1 >= a1:
                    why = f"all its bars are in {mine.name}"
            if why is None:
                out["kept"].append(f"{f.name} (not yet fully in {canonical(u)}.csv)")
        if why:
            out["moved"].append(f"{f.name}: {why}")
            if not dry_run:
                _move(f, arch)
    out["sets"] = screen_datasets(spot_dir)
    return out
