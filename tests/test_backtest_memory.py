"""Long backtests keep only the open expiries in memory: Find best settings' whole-period checks were killed by the
server's 800 MB cap because every contract touched since the first day stayed loaded."""
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from algolab.config import load_config  # noqa: E402
from algolab.feeds import HistoricalFeed, OptionStore  # noqa: E402
from algolab.models import Contract  # noqa: E402

OLD, NEW = date(2026, 10, 6), date(2026, 10, 13)


def _store(tmp_path):
    for exp, day in ((OLD, "2026-10-06"), (NEW, "2026-10-07")):
        d = tmp_path / "options" / "NIFTY" / exp.isoformat()
        d.mkdir(parents=True)
        (d / "25000CE.csv").write_text(f"time,last,bid,ask\n{day} 09:30:00,40.0,,\n{day} 10:00:00,42.0,,\n")
    return OptionStore("NIFTY", root=tmp_path / "options")


def test_expired_contracts_leave_memory_and_come_back_from_disk(tmp_path):
    store = _store(tmp_path)
    old, new = Contract("NIFTY", OLD, 25000, "CE"), Contract("NIFTY", NEW, 25000, "CE")
    assert store.price(old, pd.Timestamp("2026-10-06 10:05")) == 42.0
    assert store.price(new, pd.Timestamp("2026-10-07 10:05")) == 42.0
    store._bars_cache[old] = None
    assert store.forget_expired(OLD) == 0                     # on its expiry day it is still open
    assert store.forget_expired(date(2026, 10, 7)) == 2       # the day after: its prices and candles go
    assert old not in store._cache and old not in store._bars_cache and new in store._cache
    assert store.price(old, pd.Timestamp("2026-10-06 10:05")) == 42.0   # asked again: read from disk, same answer


def test_a_backtest_feed_lets_go_of_expiries_as_days_pass(tmp_path):
    store = _store(tmp_path)
    old = Contract("NIFTY", OLD, 25000, "CE")
    idx = pd.DatetimeIndex(["2026-10-06 10:05", "2026-10-06 15:30", "2026-10-07 09:16", "2026-10-07 10:05"])
    df = pd.DataFrame({"open": 25000.0, "high": 25000.0, "low": 25000.0, "close": 25000.0, "vix": 12.0}, index=idx)
    feed = HistoricalFeed(df, load_config(), store, real_only=True)
    seen = []
    for now in feed:
        if now.date() == OLD:
            feed.quote(old)                                   # a position in the expiring contract is priced
        seen.append((now.date(), old in store._cache))
    assert (OLD, True) in seen
    assert all(not held for d, held in seen if d > OLD)       # gone from the next morning
