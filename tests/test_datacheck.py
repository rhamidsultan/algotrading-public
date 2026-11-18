"""Data review finds planted faults and leaves real market events alone."""
import gzip
import sqlite3
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from algolab import datacheck


def _minutes(d, start="09:15", end="15:29"):
    return pd.date_range(f"{d} {start}", f"{d} {end}", freq="1min")


def _make(root):
    (root / "spot").mkdir(parents=True)
    (root / "truedata" / "options" / "NIFTY" / "2026-03-10").mkdir(parents=True)
    days = [date(2026, 3, 2) + timedelta(days=i) for i in range(10)]
    days = [d for d in days if d.weekday() < 5]
    cal = list(days)
    days.remove(date(2026, 3, 5))                    # exchange traded 5 Mar: we have no bars (missing day)
    rows = []
    px = 22000.0
    rng = np.random.default_rng(3)
    for d in days:
        for t in _minutes(d):
            if d == days[2] and t.strftime("%H:%M") >= "15:16":
                continue                               # end-of-day gap
            px *= 1 + rng.normal(0, 0.0003)
            rows.append([t, px, px + 3, px - 3, px, 15.0])
    df = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "vix"])
    i = df.index[df.datetime == pd.Timestamp(f"{days[1]} 11:00")][0]
    df.loc[i, ["close", "high"]] = df.loc[i, "close"] * 1.03   # bad tick that reverses next minute
    j = df.index[df.datetime == pd.Timestamp(f"{days[3]} 12:00")][0]
    df.loc[j:j + 20, ["open", "high", "low", "close"]] = 22100.0  # frozen 21 minutes
    filler = pd.DataFrame({"datetime": _minutes(days[4], "03:35", "04:00")})
    for c in ("open", "high", "low", "close"):
        filler[c] = 21900.0
    filler["vix"] = 15.0
    pd.concat([df, filler]).sort_values("datetime").to_csv(root / "spot" / "TD_NIFTY_1minute.csv", index=False)
    with sqlite3.connect(root / "truedata" / "progress.db") as c:
        c.execute("CREATE TABLE days(day TEXT PRIMARY KEY, status TEXT, contracts INTEGER)")
        c.executemany("INSERT INTO days VALUES(?, 'ok', 1)", [(d.isoformat(),) for d in cal])
    # options for the 10 Mar expiry: a call priced below intrinsic, a put with bars after expiry, an empty file
    exp = root / "truedata" / "options" / "NIFTY" / "2026-03-10"
    spot = df.set_index("datetime").close
    for k in range(21800, 22300, 50):
        for right in ("CE", "PE"):
            ts = [t for t in spot.index if t.date() <= date(2026, 3, 10)]
            s = spot.loc[ts]
            fair = (s - k).clip(lower=0) + 60 if right == "CE" else (k - s).clip(lower=0) + 60
            if (k, right) == (21800, "CE"):
                fair = (s - k).clip(lower=0) - 120          # below intrinsic
            o = pd.DataFrame({"timestamp": ts, "open": fair.values, "high": fair.values + 1,
                              "low": fair.values - 1, "close": fair.values, "volume": 100, "oi": 1000})
            if (k, right) == (22250, "PE"):
                o = pd.concat([o, o.tail(5).assign(timestamp=o.timestamp.tail(5) + pd.Timedelta(days=3))])
            with gzip.open(exp / f"{k}{right}.csv.gz", "wt") as f:
                o.to_csv(f, index=False)
    (exp / "22300CE.csv.gz").write_bytes(b"")
    return days


def test_review_finds_planted_faults(tmp_path):
    root = tmp_path / "data"
    days = _make(root)
    out = tmp_path / "review"
    datacheck.main(["--underlying", "NIFTY", "--data", str(root), "--out", str(out), "--workers", "1"])
    df = pd.read_csv(out / "issues.csv")
    found = set(zip(df.severity, df.check))
    assert ("FAIL", "missing day") in found
    assert ("FAIL", "spike that reverses") in found
    assert ("WARN", "frozen price") in found
    assert ("WARN", "filler bars outside market hours") in found
    assert ("WARN", "missing minutes") in found
    assert ("FAIL", "below intrinsic value") in found
    assert ("FAIL", "bars after expiry") in found
    assert ("FAIL", "unreadable file") in found
    redo = (out / "redownload.txt").read_text().split()
    assert "NIFTY26031022250PE" in redo and "NIFTY26031022300CE" in redo   # fixable by downloading again
    assert "NIFTY26031021800CE" not in redo     # a real but odd price comes back the same: not re-downloaded
    assert ("FAIL", "put-call parity") not in found   # consistent calls and puts raise no parity flag
    assert "Verdict: FAIL" in (out / "report.md").read_text()


def test_fill_index_from_zerodha(tmp_path):
    root = tmp_path / "data"
    days = _make(root)
    td = pd.read_csv(root / "spot" / "TD_NIFTY_1minute.csv", parse_dates=["datetime"])
    z = pd.DataFrame({"datetime": _minutes(days[2])})
    z["open"] = z["high"] = z["low"] = z["close"] = 22000.0
    z["vix"] = 15.0
    z.to_csv(root / "spot" / "NIFTY_minute.csv", index=False)
    n = datacheck.fill_index("NIFTY", root)
    assert n == 14                                     # 15:16-15:29 added, nothing overwritten
    after = pd.read_csv(root / "spot" / "TD_NIFTY_1minute.csv", parse_dates=["datetime"])
    assert len(after) == len(td) + 14
    kept = after.merge(td, on="datetime", suffixes=("", "_old"))
    assert np.allclose(kept.close, kept.close_old)
    assert list((root / "spot").glob("TD_NIFTY_1minute.before-fill-*.csv"))
