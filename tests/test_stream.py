"""Zerodha's live price stream: decoding, the connection, and the live feed using it with fallback."""
import json
import struct
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from algolab import feeds, kite  # noqa: E402
from algolab.feeds import OptionStore  # noqa: E402
from algolab.models import Contract  # noqa: E402
from algolab.stream import KiteStream, parse_binary  # noqa: E402
from test_platform import CFG, FakeKite  # noqa: E402


def index_packet(token, ltp, ts=1791950000):
    body = struct.pack(">IiiiiiiI", token, int(ltp * 100), int(ltp * 100) + 500, int(ltp * 100) - 500,
                       int(ltp * 100) - 100, int(ltp * 100) - 200, 0, ts)
    return body


def full_packet(token, ltp, bid, ask, ts=1791950000):
    head = struct.pack(">Iiiiiiiiiii", token, int(ltp * 100), 65, int(ltp * 100), 1000, 5000, 6000,
                       int(ltp * 100), int(ltp * 100) + 1000, int(ltp * 100) - 1000, int(ltp * 100) - 300)
    head += struct.pack(">IiiiI", ts, 12345, 0, 0, ts)
    depth = b""
    for i in range(5):
        depth += struct.pack(">iihh", 65 if i == 0 else 0, int((bid - i * 0.05) * 100), 3, 0)
    for i in range(5):
        depth += struct.pack(">iihh", 130, int((ask + i * 0.05) * 100), 2, 0)
    pkt = head + depth
    assert len(pkt) == 184
    return pkt


def message(*packets):
    out = struct.pack(">H", len(packets))
    for p in packets:
        out += struct.pack(">H", len(p)) + p
    return out


def test_parse_binary_index_and_full_packets():
    ticks = parse_binary(message(index_packet(256265, 24500.10), full_packet(240000 * 256 + 2, 101.0, 100.5, 101.5)))
    idx, opt = ticks
    assert idx["token"] == 256265 and idx["last"] == pytest.approx(24500.10) and idx["ts"] is not None
    assert opt["last"] == 101.0 and opt["bid"] == 100.5 and opt["ask"] == 101.5 and opt["oi"] == 12345
    assert parse_binary(b"\x00") == []                       # heartbeat
    ltp_only = parse_binary(message(struct.pack(">Ii", 264969, 1320)))
    assert ltp_only[0]["last"] == pytest.approx(13.2) and ltp_only[0]["bid"] is None


class Timeout(Exception):
    pass


Timeout.__name__ = "WebSocketTimeoutException"


class FakeWS:
    def __init__(self):
        self.sent, self.inbox, self.closed = [], [], False

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv_data(self):
        if self.closed:
            raise ConnectionError("closed")
        if not self.inbox:
            time.sleep(0.01)
            raise Timeout()
        return self.inbox.pop(0)

    def close(self):
        self.closed = True


def wait_for(cond, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_stream_subscribes_decodes_and_reconnects_with_a_new_login():
    conns, urls = [], []

    def connect(url):
        urls.append(url)
        ws = FakeWS()
        conns.append(ws)
        return ws
    logs = []
    st = KiteStream("key", "tok1", log=logs.append, connect=connect, stale_after=2).start()
    try:
        st.want({256265, 61440002})
        assert wait_for(lambda: conns and len(conns[0].sent) >= 2)
        sub, mode = conns[0].sent[:2]
        assert sub == {"a": "subscribe", "v": [256265, 61440002]} and mode == {"a": "mode", "v": ["full", [256265, 61440002]]}
        conns[0].inbox.append((2, message(index_packet(256265, 24510.0), full_packet(61440002, 99.5, 99.0, 100.0))))
        assert wait_for(lambda: st.tick(61440002) is not None)
        assert st.healthy() and st.tick(256265)["last"] == 24510.0 and st.tick(61440002)["bid"] == 99.0
        conns[0].inbox.append((1, json.dumps({"type": "order", "data": {"order_id": "7", "status": "COMPLETE",
                                                                         "filled_quantity": 65, "average_price": 99.1}}).encode()))
        upd = st.wait_order("7", 2.0)
        assert upd["status"] == "COMPLETE" and st.order("7")["average_price"] == 99.1
        st.want({256265})
        assert wait_for(lambda: {"a": "unsubscribe", "v": [61440002]} in conns[0].sent)
        assert st.tick(61440002) is None
        st.set_token("tok2")                                   # next morning's login
        assert wait_for(lambda: len(conns) == 2)
        assert "access_token=tok2" in urls[1] and st.reconnects == 1
        assert wait_for(lambda: {"a": "subscribe", "v": [256265]} in conns[1].sent)
    finally:
        st.stop()


def test_stream_is_unhealthy_when_silent():
    clock = [1000.0]
    st = KiteStream("k", "t", clock=lambda: clock[0], stale_after=5)
    st.connected, st.last_msg = True, 1000.0
    st.ticks[256265] = {"last": 1.0}
    assert st.healthy() and st.tick(256265)
    clock[0] += 6
    assert not st.healthy() and st.tick(256265) is None


class FakeStream:
    """Stands in for KiteStream inside the feed."""
    def __init__(self):
        self.ticks, self.wanted, self.ok, self.started, self.tokens = {}, set(), True, 0, []

    def start(self):
        self.started += 1

    def running(self):
        return self.started > 0

    def healthy(self):
        return self.ok

    def tick(self, token):
        return self.ticks.get(token) if self.ok else None

    def want(self, tokens):
        self.wanted = set(tokens)

    def set_token(self, t):
        self.tokens.append(t)

    def status(self):
        return {"healthy": self.ok, "subscribed": len(self.wanted)}


def test_feed_uses_the_stream_and_falls_back_to_polling(tmp_path, monkeypatch):
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(kite, "RUNTIME", tmp_path / "rt")
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [datetime(2026, 10, 7, 10, 0, 0)]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    fake = FakeKite(date(2026, 10, 13))
    calls = []
    real = fake.quote
    fake.quote = lambda syms: (calls.append(list(syms)), real(syms))[1]
    st = FakeStream()
    st.ticks[256265] = {"last": 24005.0}
    st.ticks[264969] = {"last": 12.9}

    class Tick:
        def sleep(self, s):
            clock[0] += timedelta(seconds=s)
            return True
    cfg = json.loads(json.dumps(CFG, default=str))
    feed = kite.KiteFeed(fake, cfg, control=Tick(), log=lambda m: None, stream=st)
    it = iter(feed)
    assert next(it) == datetime(2026, 10, 7, 10, 0) and feed.source == "stream"
    assert feed.spot == 24005.0 and feed.vix == 12.9
    assert calls == [["NSE:NIFTY 50", "NSE:INDIA VIX"]]    # only the day's previous close (once a day)
    calls.clear()
    c = Contract("NIFTY", date(2026, 10, 13), 24000, "CE")
    assert feed.quote(c).last == 101.0 and len(calls) == 1                  # first use: not streamed yet
    st.ticks[240000] = {"last": 103.0, "bid": 102.5, "ask": 103.5}
    calls.clear()
    next(it)
    assert clock[0] == datetime(2026, 10, 7, 10, 0, 1)                       # stepped every second
    q = feed.quote(c)
    assert (q.last, q.bid, q.ask) == (103.0, 102.5, 103.5) and calls == []
    assert 240000 in st.wanted and 256265 in st.wanted                      # kept subscribed
    assert any(t for t in st.wanted if t % 10 == 1)                         # puts near the money (recording band)
    rows = (tmp_path / "options" / "NIFTY" / "2026-10-13" / "24000CE.csv").read_text().strip().splitlines()
    assert len(rows) == 2                                                   # one recorded price a minute
    # the stream goes quiet: polling every 2 s, one request with the index, VIX and the option
    st.ok = False
    calls.clear()
    t0 = clock[0]
    next(it)
    assert feed.source == "poll" and feed.stats["fallbacks"] == 1 and len(calls) == 1
    assert {"NSE:NIFTY 50", "NSE:INDIA VIX", "NFO:NIFTYX24000CE"} <= set(calls[0])
    next(it)
    assert clock[0] - t0 >= timedelta(seconds=2)
    st.ok = True                                                            # and back
    next(it)
    assert feed.source == "stream"
    status = json.loads((tmp_path / "rt" / "feed_status.json").read_text())
    assert status["source"] in ("stream", "poll") and "stream" in status


def _holiday_feed(tmp_path, monkeypatch, start):
    monkeypatch.setattr(kite, "DATA", tmp_path)
    monkeypatch.setattr(kite, "RUNTIME", tmp_path / "rt")
    monkeypatch.setattr(OptionStore.__init__, "__defaults__", (tmp_path / "options", 20))
    clock = [start]
    monkeypatch.setattr(kite, "ist_now", lambda: clock[0])
    fake = FakeKite(date(2026, 10, 27))
    st = FakeStream()

    class Tick:
        def sleep(self, s):
            clock[0] += timedelta(seconds=s)
            return True
    cfg = json.loads(json.dumps(CFG, default=str))
    cfg["market"]["holidays"] = ["2026-10-20"]
    return kite.KiteFeed(fake, cfg, control=Tick(), log=lambda m: None, stream=st), st, clock


def test_feed_does_not_trade_on_an_nse_holiday(tmp_path, monkeypatch):
    """20 Oct 2026 (Dussehra) is a weekday holiday: Zerodha still serves the last day's prices, so
    the feed must stay closed all day and first step at the next open."""
    feed, st, clock = _holiday_feed(tmp_path, monkeypatch, datetime(2026, 10, 20, 9, 0))
    st.ticks[256265] = {"last": 25100.0, "ts": datetime(2026, 10, 19, 15, 29, 59)}
    st.ticks[264969] = {"last": 11.5}
    assert not feed.market_open(datetime(2026, 10, 20, 10, 0))
    assert feed.market_open(datetime(2026, 10, 21, 10, 0))
    first = next(iter(feed))
    assert first.date() == date(2026, 10, 21) and first.time() >= kite.MARKET_OPEN


def test_feed_ignores_a_stream_replaying_an_earlier_day(tmp_path, monkeypatch):
    """A holiday missing from config.yaml: the stream's index tick is stamped with an earlier day,
    so the feed does not take it as today's price."""
    feed, st, clock = _holiday_feed(tmp_path, monkeypatch, datetime(2026, 10, 21, 10, 0))
    feed._load_chain()
    st.ticks[256265] = {"last": 25100.0, "ts": datetime(2026, 10, 19, 15, 29, 59)}
    assert not feed._stream_step(datetime(2026, 10, 21, 10, 0))
    st.ticks[256265] = {"last": 25100.0, "ts": datetime(2026, 10, 21, 9, 59, 59)}
    assert feed._stream_step(datetime(2026, 10, 21, 10, 0))


def test_live_order_fill_arrives_on_the_stream(monkeypatch):
    from algolab.broker import CostModel
    from algolab.live import LiveBroker, LiveSettings
    from algolab.models import Quote

    class K:
        def __init__(self):
            self.history_calls = 0

        def place_order(self, **k):
            return "55"

        def order_history(self, oid):
            self.history_calls += 1
            return [{"status": "OPEN", "filled_quantity": 0}]

    class S:
        def healthy(self):
            return True

        def wait_order(self, oid, timeout):
            return {"order_id": oid, "status": "COMPLETE", "filled_quantity": 65, "average_price": 100.25}

        def order(self, oid):
            return None
    b = LiveBroker(300000, CostModel(), 0.5, 0.05, None, settings=LiveSettings.from_dict({"mode": "live", "daily_loss_limit": 1000}),
                   sleep=lambda s: None)
    b.kite, b.stream = K(), S()
    filled, avg, oid = b._place_and_wait("NIFTYX24000CE", "SELL", 65, Quote(100.0, 100.0, 100.5))
    assert (filled, avg, oid) == (65, 100.25, "55") and b.kite.history_calls == 0
