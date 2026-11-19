"""Resume on a LIVE card (type LIVE): the same session, so its positions, P&L and stops carry on the next day. It used
to say "start it again from the form", which made a new, empty session."""
import json

import pytest

from test_defined_risk import _panel


def test_a_stopped_live_session_resumes_with_its_own_account(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "strategies.json").write_text(json.dumps([{"name": "premium_strangle", "title": "PS", "live_ready": True,
                                                                     "params": [{"key": "lots", "label": "Lots", "default": 1, "type": "int"}]}]))
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    c = a.test_client()
    d = mod.SESS / "premium_strangle-3"
    d.mkdir(parents=True)
    state = {"strategy": "premium_strangle", "mode": "live", "feed": "kite", "params": {"lots": 1}, "positions": [{"Contract": "x"}]}
    (d / "state.json").write_text(json.dumps(state))
    body = {"id": "premium_strangle-3", "strategy": "premium_strangle", "feed": "kite", "params": {"lots": 1}, "mode": "live",
            "live": {"max_lots": 1, "daily_loss_limit": 3000, "mode": "live"}}
    assert c.post("/api/paper/start", json=body).status_code == 400                    # not without typing LIVE
    assert c.post("/api/paper/reset", json={"id": "premium_strangle-3"}).status_code == 400   # would forget the legs
    r = c.post("/api/paper/start", json={**body, "confirm": "LIVE"}).get_json()
    assert r["id"] == "premium_strangle-3" and json.loads((d / "state.json").read_text()) == state   # the same account
    assert json.loads((d / "desired.json").read_text())["mode"] == "live"
    page = c.get("/live").get_data(as_text=True)
    assert "Type LIVE to confirm" in page and "start it again from the form" not in page


def test_a_resumed_live_session_carries_its_legs_into_the_next_day(tmp_path, monkeypatch):
    """The engine side: the same session id loads its saved account (positions, stops), checks Zerodha and goes on."""
    from datetime import timedelta

    from algolab import paper
    from test_live_next_day import NextDay, _session_env
    from datetime import date
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)
    held = paper.read_state(sid)["positions"]
    paper.Slot(sid).desired.unlink()                            # stopped with its legs (e.g. Blocked)
    d = paper.read_desired(sid) or json.loads(json.dumps(paper.read_state(sid)))
    fake.new_day()
    clock[0] = clock[0] + timedelta(days=1)
    n = len([o for o in fake.sent() if o["type"] != "SL"])
    paper.Slot(sid).desired.write_text(json.dumps({"strategy": d["strategy"], "params": d["params"], "feed": "kite", "mode": "live",
                                                   "live": {"max_lots": 1, "daily_loss_limit": 3000}}))   # Resume
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert [p["Contract"] for p in s["positions"]] == [p["Contract"] for p in held], s["logs"][-6:]
    assert len([o for o in fake.sent() if o["type"] != "SL"]) == n   # no new entry: the cycle goes on
    assert paper.read_desired(sid) is not None
    assert any(o["status"] == "TRIGGER PENDING" for o in fake.sent("SL"))   # the day's stops placed again
