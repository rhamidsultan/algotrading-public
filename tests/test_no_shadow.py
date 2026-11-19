"""Shadow mode was removed (18 Nov 2026): no Shadow on the Live pages, the start form or Deploy; old SHADOW sessions
(they never sent an order) are stopped and deleted. Paper stays."""
import json
import time

import pytest

from test_defined_risk import _panel


def _app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return mod, a.test_client()


def _sess(mod, sid, mode, running):
    d = mod.SESS / sid
    d.mkdir(parents=True)
    (d / "state.json").write_text(json.dumps({"strategy": "iron_fly", "mode": mode, "positions": [], "feed": "kite", "params": {}, "time": "2026-11-18T10:00:00", "spot": 24000, "equity": 300000, "unrealized": 0, "net_realized": 0,
                                              "broker": {"capital": 300000, "realized_gross": 0, "total_costs": 0,
                                                         "positions": [], "fills": [], "trades": []}}))
    if running:
        (d / "desired.json").write_text(json.dumps({"strategy": "iron_fly", "mode": mode, "feed": "kite"}))
        (d / "heartbeat").write_text(str(time.time()))
    return d


def test_old_shadow_sessions_are_stopped_then_deleted(tmp_path, monkeypatch):
    mod, c = _app(tmp_path, monkeypatch)
    old = _sess(mod, "fly-1", "shadow", False)
    run = _sess(mod, "fly-2", "shadow", True)
    live = _sess(mod, "fly-3", "live", True)
    paper = _sess(mod, "fly-4", "paper", False)
    ids = {x["id"] for x in c.get("/api/paper?kind=live").get_json()["sessions"]}
    assert ids == {"fly-3"}
    assert not old.exists()                                     # stopped: deleted
    assert run.exists() and not (run / "desired.json").exists()  # running: stopped first, deleted once let go
    (run / "heartbeat").write_text(str(time.time() - 60))
    c.get("/api/paper?kind=live")
    assert not run.exists() and live.exists() and paper.exists()


def test_the_live_pages_offer_no_shadow(tmp_path, monkeypatch):
    mod, c = _app(tmp_path, monkeypatch)
    for path in ("/live", "/live/new", "/live/overview"):
        page = c.get(path).get_data(as_text=True)
        assert "Start shadow session" not in page and "Deploy to Shadow" not in page and 'value="shadow"' not in page
    assert "Start a live session" in c.get("/live/new").get_data(as_text=True)
