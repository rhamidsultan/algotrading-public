"""TrueData real-time check: ticks per second counted per symbol (with a stand-in for TrueData's package)."""
import sys
import types
from pathlib import Path
from pathlib import Path
from datetime import date

import pytest

from algolab import td_live_test


def test_summarise_counts_seconds_with_several_ticks():
    rows = td_live_test.summarise({"A": [0.1, 0.2, 0.9, 1.5, 3.0], "B": [0.5, 1.5]}, {"A": 3}, 4)
    a = next(r for r in rows if r["symbol"] == "A")
    assert a["ticks"] == 5 and a["most_in_one_second"] == 3 and a["seconds_with_2_or_more"] == 1
    assert a["share_2_or_more_pct"] == round(1 / 3 * 100, 1) and a["trades"] == 3 and a["per_second"] == 1.25


def test_run_with_a_fake_feed(tmp_path, monkeypatch):
    class Tick:
        def __init__(self, symbol, ltp, ttq):
            self.symbol, self.ltp, self.ttq = symbol, ltp, ttq

    class FakeTD:
        def __init__(self, user, pw):
            self.cb = None

        def trade_callback(self, f):
            self.cb = f

        def start_live_data(self, symbols):
            for i in range(3):
                for s in symbols:
                    self.cb(Tick(s, 25010.0, 100 + i))

        def disconnect(self):
            pass
    mod = types.ModuleType("truedata")
    mod.TD_live = FakeTD
    monkeypatch.setitem(sys.modules, "truedata", mod)
    monkeypatch.setattr("algolab.truedata.credentials", lambda: ("u", "p"))
    monkeypatch.setattr(td_live_test, "ROOT", tmp_path)
    monkeypatch.setattr(td_live_test, "_refusal", lambda *a, **k: None)          # no network in tests
    rep = td_live_test.run(0.2, log=lambda *_: None)
    assert rep["spot"] == 25010.0 and len(rep["symbols"]) == 4 and rep["symbols"][2].startswith("NIFTY")
    assert list((tmp_path / "runs").glob("td_live_test_*.json"))


def test_button_sends_a_mac_job_and_the_helper_reports(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    from datetime import datetime, timedelta, timezone
    from test_backtest_queue import _Client, _panel
    mod, c = _panel(tmp_path, monkeypatch)
    ist = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(mod, "_ist_now", lambda: datetime(2026, 10, 18, 11, 0, tzinfo=ist))          # a Sunday
    assert c.post("/api/td-live-test").status_code == 400
    monkeypatch.setattr(mod, "_ist_now", lambda: datetime(2026, 10, 20, 11, 0, tzinfo=ist))
    r = c.post("/api/td-live-test")
    assert r.status_code == 200 and r.get_json()["test"]["status"] == "queued"
    assert c.post("/api/td-live-test").status_code == 409                                         # one at a time
    from algolab import lab_agent
    agent = lab_agent.Agent(session=_Client(c), cfg={"url": "http://dash"}, log=lambda m: None, sync=lambda log: "")
    report = {"results": [{"symbol": "NIFTY261020 25000CE", "per_second": 3.2, "most_in_one_second": 7,
                           "share_2_or_more_pct": 64.0, "trades": 180}]}
    monkeypatch.setattr(agent, "_td_test", lambda job: {"td": report})
    assert agent.once() is True
    t = c.get("/api/td-live-test").get_json()["test"]
    assert t["status"] == "done" and t["td"]["results"][0]["per_second"] == 3.2
    assert all(j["kind"] != "td_live_test" for j in c.get("/api/backtest-runner/finished").get_json()["jobs"])
    assert "Test TrueData live" not in c.get("/backtests/run").get_data(as_text=True)   # removed from Run (28 Nov)


def test_helper_refuses_outside_market_hours(monkeypatch):
    from algolab import lab_agent
    agent = lab_agent.Agent(session=object(), cfg={"url": "http://dash"}, log=lambda m: None, sync=lambda log: "")
    import datetime as real
    class Fake(real.datetime):
        @classmethod
        def now(cls, tz=None):
            return real.datetime(2026, 10, 20, 16, 0, tzinfo=tz)
    monkeypatch.setattr(real, "datetime", Fake)
    with pytest.raises(RuntimeError, match="market is closed"):
        agent._td_test({"seconds": 1})


def test_older_helper_is_not_given_the_test(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from datetime import datetime, timedelta, timezone
    from test_backtest_queue import _panel
    mod, c = _panel(tmp_path, monkeypatch)
    monkeypatch.setattr(mod, "_ist_now", lambda: datetime(2026, 10, 20, 11, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))))
    assert c.post("/api/td-live-test").status_code == 200
    assert c.post("/api/lab-agent/claim", json={"name": "Air"}).get_json()["job"] is None          # old helper: no "can"
    st = c.get("/api/td-live-test").get_json()
    assert st["test"]["status"] == "queued" and st["mac"]["can"] == []
    got = c.post("/api/lab-agent/claim", json={"name": "Air", "can": ["backtest", "sweep", "td_live_test"]}).get_json()["job"]
    assert got["kind"] == "td_live_test"
    assert "Test TrueData live" not in c.get("/backtests/run").get_data(as_text=True)   # the test left Run on 28 Nov


def test_truedata_side_watches_given_symbols(tmp_path, monkeypatch):
    class Tick:
        def __init__(self, symbol, ltp, ttq):
            self.symbol, self.ltp, self.ttq = symbol, ltp, ttq

    class FakeTD:
        def __init__(self, user, pw):
            self.cb = None

        def trade_callback(self, f):
            self.cb = f

        def start_live_data(self, symbols):
            self.symbols = symbols

        def disconnect(self):
            pass
    mod = types.ModuleType("truedata")
    mod.TD_live = FakeTD
    monkeypatch.setitem(sys.modules, "truedata", mod)
    monkeypatch.setattr("algolab.truedata.credentials", lambda: ("u", "p"))
    monkeypatch.setattr(td_live_test, "ROOT", tmp_path)
    monkeypatch.setattr(td_live_test, "_refusal", lambda *a, **k: None)          # no network in tests
    monkeypatch.setattr(td_live_test.time, "sleep", lambda s: None)
    rep = td_live_test.run(0.2, log=lambda *_: None, symbols=["GOLD-I", "SILVER-I"])
    assert rep["symbols"] == ["GOLD-I", "SILVER-I"] and {r["symbol"] for r in rep["results"]} == {"GOLD-I", "SILVER-I"}
    assert rep["source"] == "truedata"


def test_zerodha_side_counts_ticks_after_the_snapshot():
    import struct
    from algolab import kite_tick_test as kt

    def pkt(token, last, vol):
        body = struct.pack(">Ii", token, int(last * 100)) + struct.pack(">iiiiiiiii", 0, 0, vol, 0, 0, 0, 0, 0, 0)
        return struct.pack(">H", len(body)) + body

    def msg(*pkts):
        return struct.pack(">H", len(pkts)) + b"".join(pkts)

    class WS:
        def __init__(self):
            self.sent, self.q = [], [msg(pkt(1, 100, 10), pkt(2, 50, 5)),          # snapshot on subscribing
                                     msg(pkt(1, 101, 11)), msg(pkt(1, 101, 11)), "{}", msg(pkt(2, 51, 9))]

        def send(self, m):
            self.sent.append(m)

        def recv(self):
            if not self.q:
                raise TimeoutError("timed out")
            return self.q.pop(0)
    clock = iter([0.0] + [i * 0.1 for i in range(1, 50)])
    ws = WS()
    arr, tr = kt.count(ws, {"GOLD-I": 1, "SILVER-I": 2}, 2.0, clock=lambda: next(clock))
    assert len(arr["GOLD-I"]) == 2 and tr["GOLD-I"] == 1 and len(arr["SILVER-I"]) == 1 and tr["SILVER-I"] == 1
    assert '"mode"' in ws.sent[1] and "full" in ws.sent[1]

    class Kite:
        def instruments(self, ex):   # dates relative to today: the nearest unexpired contract is token 10
            from datetime import date, timedelta
            today = date.today()
            return [{"instrument_type": "FUT", "name": "GOLD", "expiry": today + timedelta(days=60), "instrument_token": 11},
                    {"instrument_type": "FUT", "name": "GOLD", "expiry": today + timedelta(days=20), "instrument_token": 10},
                    {"instrument_type": "FUT", "name": "GOLD", "expiry": today - timedelta(days=10), "instrument_token": 9}]
    assert kt.tokens_for(Kite(), ["GOLD", "SILVER"]) == {"GOLD-I": 10}


def test_mcx_button_sends_both_sides(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    from datetime import datetime, timedelta, timezone
    from test_backtest_queue import _panel
    mod, c = _panel(tmp_path, monkeypatch)
    ist = timezone(timedelta(hours=5, minutes=30))
    monkeypatch.setattr(mod, "_ist_now", lambda: datetime(2026, 10, 19, 20, 30, tzinfo=ist))     # evening: MCX open, NSE shut
    assert c.post("/api/td-live-test").status_code == 400
    r = c.post("/api/td-live-test", json={"set": "mcx"})
    assert r.status_code == 200, r.get_json()
    t, k = r.get_json()["test"], r.get_json()["pair"]
    assert t["symbols"] == ["GOLD-I", "SILVER-I", "CRUDEOIL-I", "NATURALGAS-I"] and t["pair"] == k["id"] and k["kind"] == "kite_tick_test"
    assert k["start_at"] == t["start_at"] and (mod.RT / "tick-test-queue" / f"{k['id']}.json").exists()
    assert not (mod.BT_QUEUE / f"{k['id']}.json").exists()                                       # not behind backtests
    assert c.get("/api/td-live-test").get_json()["pair"]["id"] == k["id"]
    assert all(j["kind"] != "kite_tick_test" for j in c.get("/api/backtest-runner/finished").get_json()["jobs"])
    monkeypatch.setattr(mod, "_ist_now", lambda: datetime(2026, 10, 19, 23, 40, tzinfo=ist))
    (tmp_path / "x").mkdir()


def test_server_runs_the_zerodha_side(monkeypatch):
    from algolab import backtest_queue as bq, kite_tick_test as kt
    monkeypatch.setattr(kt, "mcx_open", lambda now=None: True)
    monkeypatch.setattr(kt, "run", lambda seconds, names, start_at=None: {"results": [{"symbol": "GOLD-I", "per_second": 1.0}], "names": names})
    assert bq.run_kite_ticks({"set": "mcx", "names": ["GOLD"], "seconds": 5})["kite"]["names"] == ["GOLD"]
    monkeypatch.setattr(kt, "mcx_open", lambda now=None: False)
    with pytest.raises(ValueError, match="MCX is closed"):
        bq.run_kite_ticks({"set": "mcx"})


def test_tick_test_runner_runs_its_own_queue(tmp_path, monkeypatch):
    import json as _json
    from algolab import backtest_queue as bq, config, kite_tick_test as kt
    monkeypatch.setattr(config, "RUNTIME", tmp_path)
    monkeypatch.setattr(bq, "JOBS", tmp_path / "backtest-jobs")
    q = tmp_path / "tick-test-queue"
    q.mkdir()
    (q / "20261019-203000-abcd.json").write_text(_json.dumps({"kind": "kite_tick_test", "set": "mcx", "names": ["GOLD"]}))
    monkeypatch.setattr(bq, "run_kite_ticks", lambda job: {"kite": {"results": [{"symbol": "GOLD-I", "per_second": 1.1}]}})
    assert kt.process() == 1 and not list(q.glob("*.json"))
    j = _json.loads((tmp_path / "backtest-jobs" / "20261019-203000-abcd.json").read_text())
    assert j["status"] == "done" and j["kite"]["results"][0]["per_second"] == 1.1
    assert "algolab-ticktest.path" in (Path(__file__).resolve().parents[1] / "deploy" / "update_engine.sh").read_text()


def test_connect_reports_what_truedata_said():
    import logging
    import threading as th
    tried = []

    class Refused:
        def __init__(self, user, pw, live_port=None, log_handler=None):
            tried.append(live_port)
            lg = logging.getLogger(f"td-fake-{live_port}")
            lg.addHandler(log_handler)
            lg.setLevel(logging.INFO)
            lg.error("\x1b[31mThe request encountered an error - User not subscribed to real time data\x1b[0m")
            th.Event().wait(5)                       # TrueData's package waits for a connection that never comes
    monkeypatch_refusal = td_live_test._refusal
    td_live_test._refusal = lambda *a, **k: None
    try:
        td, said = td_live_test._connect(Refused, "u", "p", lambda m: None, wait=0.3)
    finally:
        td_live_test._refusal = monkeypatch_refusal
    assert td is None and tried == [8086, 8084]
    assert said[0] == "port 8086: The request encountered an error - User not subscribed to real time data"


def test_refusal_is_read_from_truedata_first_reply():
    class WS:
        def __init__(self, text):
            self.text = text

        def recv(self):
            return self.text

        def close(self):
            pass
    no = '{"success":false,"message":"User Subscription Expired","subscription":null,"validity":"2026-10-22T00:00:00"}'
    assert td_live_test._refusal("u", "p", 8084, connect=lambda url: WS(no)) == "User Subscription Expired (valid until 2026-10-22)"
    ok = '{"success":true,"message":"TrueData Real Time Data Service","subscription":"tick"}'
    assert td_live_test._refusal("u", "p", 8084, connect=lambda url: WS(ok)) is None

    def boom(url):
        raise OSError("down")
    assert td_live_test._refusal("u", "p", 8084, connect=boom) is None
