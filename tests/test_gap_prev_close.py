"""20 Nov 2026: "No trade today: NIFTY +1.34% from yesterday's close (trend/gap day)" was wrong. The session had
stopped at 09:30 the day before, so the last NIFTY it saw (09:30) was taken for yesterday's close. Live, the filter
now uses the exchange's previous close from Zerodha's quote (ohlc.close)."""
from datetime import datetime

from algolab.strategies._common import RegimeFilter


class P:
    gap_pct, vix_min, vix_max, vix_spike_pct, iv_rv_min, rv_days, events = 1.0, 0, 0, 0, 0, 10, "off"


class Ctx:
    def __init__(self, now, spot, prev_close=None, vix=None, prev_vix_close=None):
        self.now, self.spot, self.prev_close, self.vix, self.prev_vix_close = now, spot, prev_close, vix, prev_vix_close
        self.logs = []

    def log(self, m):
        self.logs.append(m)


def test_a_session_stopped_early_yesterday_uses_the_real_close():
    f = RegimeFilter()
    f.observe(Ctx(datetime(2026, 11, 19, 9, 30), 25000.0))     # stopped at 09:30: its last NIFTY was 25,000
    today = Ctx(datetime(2026, 11, 20, 9, 20), 25335.0, prev_close=25300.0)   # the real close was 25,300
    f.observe(today)
    assert f.check(today, P()) is None                         # +0.14%, not +1.34%
    assert f.days[-1][1] == 25300.0                            # the daily record is corrected too


def test_without_the_exchange_close_the_own_record_is_used_as_before():
    f = RegimeFilter()
    f.observe(Ctx(datetime(2026, 11, 19, 15, 29), 25000.0))
    today = Ctx(datetime(2026, 11, 20, 9, 20), 25335.0)
    f.observe(today)
    assert "+1.34%" in f.check(today, P())


def test_a_real_gap_still_skips():
    f = RegimeFilter()
    today = Ctx(datetime(2026, 11, 20, 9, 20), 25600.0, prev_close=25300.0)
    f.observe(today)
    assert "+1.19%" in f.check(today, P())


def test_the_kite_feed_reads_the_previous_close():
    from algolab.kite import KiteFeed, SPOT_SYMBOL, VIX_SYMBOL
    feed = KiteFeed.__new__(KiteFeed)
    feed.prev_close = feed.prev_vix_close = None
    feed.stats, feed.log = {"rest_calls": 0}, print

    class K:
        def quote(self, syms):
            return {SPOT_SYMBOL: {"last_price": 25335, "ohlc": {"close": 25300.0}},
                    VIX_SYMBOL: {"last_price": 12.1, "ohlc": {"close": 12.4}}}
    feed.kite = K()
    feed._load_previous_close()
    assert feed.prev_close == 25300.0 and feed.prev_vix_close == 12.4
