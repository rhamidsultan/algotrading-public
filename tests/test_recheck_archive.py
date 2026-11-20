"""20 Nov 2026: Re-check today (a running session forgets today's "no trade today" decision) and the archive
(stopped sessions moved out of the lists, several at once, and back)."""
import json
import time
from datetime import datetime

import pytest

from test_defined_risk import _panel


def test_intraday_strangle_forgets_todays_skip_and_checks_again():
    from algolab.strategies.intraday_strangle import IntradayStrangle
    st = IntradayStrangle()
    st.day = {"date": "2026-11-20", "skip": "NIFTY +1.34% from yesterday's close (trend/gap day)"}
    assert st.recheck_today("2026-11-19") is None and st.day           # another day: nothing
    assert "1.34" in st.recheck_today("2026-11-20") and st.day is None
    assert st.recheck_today("2026-11-20") is None


def test_the_other_filtered_strategies_forget_it_too():
    from algolab.strategies.iron_fly import IronFly
    st = IronFly()
    st.skipped = "2026-11-20|VIX 25 above 22"
    assert st.recheck_today("2026-11-20") == "VIX 25 above 22" and st.skipped == ""


def _state(mode="live", logs=(), positions=()):
    return {"strategy": "intraday_strangle", "mode": mode, "positions": list(positions), "feed": "kite", "params": {},
            "time": "2026-11-20T11:40:00", "spot": 26000, "equity": 300000, "unrealized": 0, "net_realized": 0,
            "logs": list(logs), "broker": {"capital": 300000, "realized_gross": 0, "total_costs": 0, "positions": [],
                                           "fills": [], "trades": []}}


def _app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return mod, a.test_client()


def _sess(mod, sid, running, **kw):
    d = mod.SESS / sid
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps(_state(**kw)))
    if running:
        (d / "desired.json").write_text(json.dumps({"strategy": "intraday_strangle", "mode": kw.get("mode", "live")}))
        (d / "heartbeat").write_text(str(time.time()))
    return d


def test_recheck_on_the_card(tmp_path, monkeypatch):
    mod, c = _app(tmp_path, monkeypatch)
    _sess(mod, "is-1", True, logs=["2026-11-20 09:20  No trade today: NIFTY +1.34% from yesterday's close (trend/gap day)"])
    _sess(mod, "is-2", False)
    s = {x["id"]: x for x in c.get("/api/paper?kind=live").get_json()["sessions"]}
    assert s["is-1"]["skipped_today"].startswith("NIFTY +1.34%") and not s["is-2"]["skipped_today"]
    assert c.post("/api/paper/recheck", json={"id": "is-2"}).status_code == 400      # not running
    assert c.post("/api/paper/recheck", json={"id": "is-1"}).get_json()["ok"]
    assert (mod.SESS / "is-1" / "RECHECK").exists()
    assert "Re-check today" in c.get("/live").get_data(as_text=True)


def test_the_session_applies_a_recheck(tmp_path):
    from algolab import paper

    class Strat:
        def recheck_today(self, today):
            return "gap" if today == "2026-11-20" else None
    s = paper.Session.__new__(paper.Session)
    s.strategy, s.slot, s.logs = Strat(), type("S", (), {"dir": tmp_path})(), []
    s._log = s.logs.append
    s._save_now = lambda now: None
    (tmp_path / "RECHECK").write_text("{}")
    assert s._recheck_requested(datetime(2026, 11, 20, 11, 40)) and not (tmp_path / "RECHECK").exists()
    assert "today's skip (gap) cleared" in s.logs[-1]


def test_archive_several_and_restore(tmp_path, monkeypatch):
    mod, c = _app(tmp_path, monkeypatch)
    _sess(mod, "a-1", False)
    _sess(mod, "a-2", False, mode="paper")
    _sess(mod, "a-3", True)                                                 # running
    _sess(mod, "a-4", False, positions=[{"Contract": "x"}])                # LIVE, legs held
    j = c.post("/api/paper/archive", json={"ids": ["a-1", "a-2", "a-3", "a-4"], "archive": True}).get_json()
    assert j["done"] == ["a-1", "a-2"] and {r["id"] for r in j["refused"]} == {"a-3", "a-4"}
    s = {x["id"]: x for x in c.get("/api/paper?kind=live").get_json()["sessions"]}
    assert s["a-1"]["archived"] and not s["a-3"]["archived"]
    assert c.post("/api/paper/archive", json={"ids": ["a-1"], "archive": False}).get_json()["done"] == ["a-1"]
    assert not (mod.SESS / "a-1" / "archived").exists()
    page = c.get("/live/overview").get_data(as_text=True)
    assert 'id="archBar"' in page and 'id="archived"' in page and "Move ${n" in page
