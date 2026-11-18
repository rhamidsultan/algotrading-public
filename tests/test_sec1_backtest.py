"""Backtests stepped every second on the recorded 1-second bars (algolab/sec1_feed.py), with the audit fixes of 26 Oct:
same-day expiry settlement (C1), no fills on an earlier day's price (H1), partial days left out (H2), the exchange's
day open / high (H3), a second's row known only from the next second (M1), fills counted by price source (M4)."""
from datetime import date, datetime

import pytest

from algolab import engine, sec1
from algolab.config import load_config
from algolab.models import Contract
from algolab.sec1_feed import SEC1_DATASET, Sec1Feed, sec1_days
from algolab.strategy import Strategy

DAY = date(2026, 10, 26)          # Monday
EXPIRY = date(2026, 10, 27)       # Tuesday expiry


def _write(root, day, name, rows, extra=None):
    folder = root / str(day)
    folder.mkdir(parents=True, exist_ok=True)
    lines = [",".join(sec1.COLUMNS)]
    for r in rows:
        t, close = r[0], r[1]
        bid, ask = (r[2], r[3]) if len(r) > 3 else ("", "")
        dopen, dhigh = (r[4], r[5]) if len(r) > 5 else ("", "")
        lines.append(f"{t},{close},{close},{close},{close},{bid},{ask},,,1,{dopen},{dhigh}")
    (folder / f"{name}.csv").write_text("\n".join(lines) + "\n")


def _full_index(root, day, levels=((("09:15:00"), 25000),), close_at="15:29:59", close=None):
    """NIFTY printing once a minute (the real index prints every second) from the first level until close_at."""
    from datetime import timedelta
    lv = sorted((datetime.strptime(f"{day} {t}", "%Y-%m-%d %H:%M:%S"), px) for t, px in levels)
    end = datetime.strptime(f"{day} {close_at}", "%Y-%m-%d %H:%M:%S")
    rows, t, k = [], lv[0][0], 0
    while t < end:
        while k + 1 < len(lv) and lv[k + 1][0] <= t:
            k += 1
        rows.append((f"{t:%Y-%m-%d %H:%M:%S}", lv[k][1]))
        t += timedelta(minutes=1)
    rows.append((f"{day} {close_at}", close if close is not None else lv[-1][1]))
    _write(root, day, "NIFTY", rows)
    _write(root, day, "INDIAVIX", [(f"{day} 09:15:00", 12.5)])


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "sec1"
    monkeypatch.setattr(sec1, "ROOT", r)
    return r


def _c(k, right="CE", e=EXPIRY):
    return Contract("NIFTY", e, k, right)


class At(Strategy):
    """Sells the 25000 CE at one moment, buys it back at another."""
    name = "t_at"

    def __init__(self, sell="09:20:06", buy=None, expiry=EXPIRY, **kw):
        super().__init__(**kw)
        self.s, self.b, self.e = sell, buy, expiry

    def on_bar(self, ctx):
        hm = ctx.now.strftime("%H:%M:%S")
        c = ctx.option(self.e, 25000, "CE")
        if hm == self.s and not ctx.positions:
            ctx.sell(c, 1, "test")
        elif self.b and hm == self.b and ctx.positions:
            ctx.close(c, "test")


def test_fills_use_the_second_known_at_that_moment(root):
    _full_index(root, DAY)
    _write(root, DAY, "2026-10-27_25000CE", [(f"{DAY} 09:15:02", 80.0, 79.5, 80.5), (f"{DAY} 09:20:05", 82.0, 81.6, 82.4),
                                             (f"{DAY} 09:20:06", 90.0, 89.6, 90.4), (f"{DAY} 09:29:59", 70.0, 69.8, 70.3)])
    res = engine.backtest(load_config(), At(buy="09:30:00"), SEC1_DATASET, DAY, DAY)
    assert list(res.fills.side) == ["SELL", "BUY"]
    assert list(res.fills.price) == [81.6, 70.3]     # at 09:20:06 the 09:20:06 row is not known yet (M1)
    assert res.metrics["Fills on 1-second prices"] == "100%"
    assert str(res.equity.index[-1]) == f"{DAY} 15:30:00"     # stepped through 15:30 as paper


def test_expiring_legs_settle_on_expiry_day(root):
    _full_index(root, EXPIRY, levels=(("09:15:00", 25000),), close=25120.0)
    _write(root, EXPIRY, "2026-10-27_25000CE", [(f"{EXPIRY} 09:15:02", 80.0, 79.5, 80.5)])
    res = engine.backtest(load_config(), At(sell="09:20:00"), SEC1_DATASET, EXPIRY, EXPIRY)
    assert list(res.fills.source)[-1] == "settlement"
    assert str(res.fills.time.iloc[-1]) == f"{EXPIRY} 15:30:00" and res.fills.price.iloc[-1] == pytest.approx(120.0)
    assert len(res.trades) == 1 and str(res.trades.exit_time.iloc[0]).startswith(str(EXPIRY))   # booked that day (C1)


def test_no_fill_on_an_earlier_days_price(root, monkeypatch):
    prev, day = date(2026, 10, 23), DAY
    _full_index(root, prev)
    _write(root, prev, "2026-10-27_25000CE", [(f"{prev} 15:29:00", 60.0, 59.8, 60.2)])
    _full_index(root, day)
    _write(root, day, "2026-10-27_25000CE", [(f"{day} 09:15:40", 80.0, 79.6, 80.4)])
    feed = Sec1Feed(load_config(), sec1_days())
    it = iter(feed)
    for t in it:
        if t == datetime(2026, 10, 26, 9, 15, 1):
            break
    q = feed.quote(_c(25000))
    assert q.source == "previous_day" and q.last == 60.0          # a guide for decisions, never a "real" price
    f = feed.fill_quote(_c(25000))
    assert f.source == "market" and f.bid == 79.6                 # the order waits for today's first tick (H1)
    assert feed.fill_stats["waited_for_tick"] == 1


def test_partial_and_missing_days_are_left_out_and_expiry_settles_on_the_minute_close(root, monkeypatch):
    _full_index(root, DAY)
    _write(root, DAY, "2026-10-27_25000CE", [(f"{DAY} 09:15:02", 80.0, 79.5, 80.5)])
    _full_index(root, EXPIRY, close_at="11:00:00")                 # recording stopped at 11:00 (H2)
    _full_index(root, date(2026, 10, 28))
    import algolab.sec1_feed as sf
    monkeypatch.setattr(sf, "index_close_from_minute_set", lambda d: 25150.0 if d == EXPIRY else None)
    res = engine.backtest(load_config(), At(sell="09:20:00"), SEC1_DATASET, DAY, date(2026, 10, 28))
    assert "2026-10-27 (recording ended at 11:00)" in res.metrics["Recorded days left out (partial)"]
    assert res.metrics["Trading days without 1-second bars (left out)"] == "2026-10-27"
    settle = res.fills[res.fills.source == "settlement"]
    assert settle.price.iloc[0] == pytest.approx(150.0)           # 25150 close from the 1-minute set, not 11:00's 25000
    assert "2026-10-27 at 25,150.00" in res.metrics["Expiries settled on a day not replayed"]


def test_day_open_and_high_come_from_the_exchange(root):
    _full_index(root, DAY)
    _write(root, DAY, "2026-10-27_25000CE", [(f"{DAY} 12:00:00", 50.0, 49.8, 50.2, 95.0, 120.0)])   # first recorded mid-day
    feed = Sec1Feed(load_config(), sec1_days())
    for t in feed:
        if t == datetime(2026, 10, 26, 12, 0, 1):
            break
    q = feed.quote(_c(25000))
    assert (q.last, q.day_open, q.day_high) == (50.0, 95.0, 120.0)     # not 50 / 50 (H3)


def test_a_second_written_twice_is_merged(root):
    _full_index(root, DAY)
    folder = root / str(DAY)
    lines = [",".join(sec1.COLUMNS), f"{DAY} 09:20:00,80,81,80,81,80.8,81.2,,,2,,", f"{DAY} 09:20:00,81,82,79,79.5,79.3,79.7,,,1,,"]
    (folder / "2026-10-27_25000CE.csv").write_text("\n".join(lines) + "\n")
    feed = Sec1Feed(load_config(), sec1_days())
    s = feed._get(DAY, "2026-10-27_25000CE")
    assert len(s.secs) == 1 and s.last[0] == 79.5 and s.bid[0] == 79.3
    assert s.day_high[0] == 82.0                                    # the highest of both rows


def test_strikes_appear_only_once_traded(root):
    _full_index(root, DAY)
    _write(root, DAY, "2026-10-27_25000CE", [(f"{DAY} 09:15:02", 80.0, 79.5, 80.5)])
    _write(root, DAY, "2026-10-27_25050CE", [(f"{DAY} 09:40:00", 50.0, 49.0, 51.0)])
    feed = Sec1Feed(load_config(), sec1_days())
    it = iter(feed)
    assert next(it) == datetime(2026, 10, 26, 9, 15, 1)          # the 09:15:00 index row is known from 09:15:01
    assert feed.listed_strikes(EXPIRY, "CE") == set()             # nothing traded yet: no strike, not "any strike"
    for t in it:
        if t == datetime(2026, 10, 26, 9, 20, 0):
            break
    assert feed.listed_strikes(EXPIRY, "CE") == {25000}


def test_model_priced_fills_are_counted(root):
    _full_index(root, DAY)
    res = engine.backtest(load_config(), At(sell="09:20:00"), SEC1_DATASET, DAY, DAY)   # no option recorded at all
    assert res.metrics["Fills on 1-second prices"] == "0%" and "model 1" in res.metrics["Fills not at that second's price"]


def test_no_bars_at_all_is_refused(root):
    _full_index(root, DAY, close_at="10:00:00")
    with pytest.raises(ValueError, match="incomplete"):
        engine.backtest(load_config(), At(), SEC1_DATASET, DAY, DAY)
    with pytest.raises(ValueError):
        engine.backtest(load_config(), At(), SEC1_DATASET, date(2026, 10, 19), date(2026, 10, 23))
