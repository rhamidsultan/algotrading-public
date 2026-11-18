"""Zerodha slow for a moment (16 Oct: 'Read timed out. (read timeout=7)' stopped the daily recording)."""
from datetime import date

import pytest

from algolab import kite


class ReadTimeout(Exception):
    pass


class SlowKite:
    def __init__(self, fail_times):
        self.fail_times, self.calls = fail_times, 0

    def historical_data(self, token, a, b, interval):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ReadTimeout("HTTPSConnectionPool(host='api.kite.trade', port=443): Read timed out. (read timeout=7)")
        return [{"date": a, "open": 1, "high": 2, "low": 0.5, "close": 1.5}]


def test_fetch_candles_tries_again_after_a_timeout(monkeypatch):
    monkeypatch.setattr(kite._time, "sleep", lambda s: None)
    k = SlowKite(fail_times=2)
    df = kite.fetch_candles(k, 1, date(2026, 10, 16), date(2026, 10, 16), "5minute")
    assert len(df) == 1 and k.calls == 3


def test_gives_up_after_four_tries_and_not_on_other_errors(monkeypatch):
    monkeypatch.setattr(kite._time, "sleep", lambda s: None)
    with pytest.raises(ReadTimeout):
        kite.fetch_candles(SlowKite(fail_times=9), 1, date(2026, 10, 16), date(2026, 10, 16), "5minute")
    calls = []
    def bad():
        calls.append(1)
        raise ValueError("Invalid token")
    with pytest.raises(ValueError):
        kite._retry(bad)
    assert len(calls) == 1                        # a real error is not retried
    assert kite._transient(ReadTimeout("x")) and not kite._transient(ValueError("Invalid token"))
