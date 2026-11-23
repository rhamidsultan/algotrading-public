"""23 Nov 2026 (intraday_strangle-4): sold the straddle at 09:20, the PE's leg stop at 09:24, Stop at 09:35 (CE
bought back), Resume at 09:36. The strategy still thought the CE was open and waited all day. Hamid: a session
resumed on the same day with nothing open starts a fresh cycle (before last_entry_time, entry checks applied)."""
import json
from datetime import date

from algolab import paper
from test_live_next_day import NextDay, _session_env

PARAMS = {"min_dte": 0, "gap_pct": 0, "vix_min": 0, "vix_max": 0, "vix_spike_pct": 0, "events": "off", "lots": 1}


def _env(tmp_path, monkeypatch):
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, _, _ = _session_env(tmp_path, monkeypatch, fake)
    sid = paper.set_desired("intraday_strangle", PARAMS, "kite", "live", {"max_lots": 1, "daily_loss_limit": 3000})
    want = lambda: {sid: paper.read_desired(sid)} if paper.read_desired(sid) else {}  # noqa: E731
    return fake, cfg, sid, want


def _sells(fake):
    return [o for o in fake.sent() if o["side"] == "SELL" and o["type"] != "SL"]


def test_stop_then_resume_the_same_day_starts_a_fresh_cycle(tmp_path, monkeypatch):
    fake, cfg, sid, want = _env(tmp_path, monkeypatch)
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert len(_sells(fake)) == 2                                # the day's straddle
    paper.kill(sid)                                              # Stop: squared off
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert paper.read_desired(sid) is None and not paper.read_state(sid)["positions"]
    d = paper.read_state(sid)
    resume = {"strategy": "intraday_strangle", "params": d["params"], "feed": "kite", "mode": "live",
              "live": {"max_lots": 1, "daily_loss_limit": 3000}, "resumed": "2026-10-07 10:05:00"}
    paper.Slot(sid).desired.write_text(json.dumps(resume))      # Resume on the card
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    assert len(_sells(fake)) == 4 and len(s["positions"]) == 2, s["logs"][-6:]
    assert any("Resumed with nothing open" in x for x in s["logs"])


def test_a_restart_is_not_a_resume(tmp_path, monkeypatch):
    """An update restarts the engine with the same desired.json: no second fresh cycle."""
    fake, cfg, sid, want = _env(tmp_path, monkeypatch)
    paper.run_group(cfg, want, "kite", max_bars=3)
    paper.kill(sid)
    paper.run_group(cfg, want, "kite", max_bars=3)
    d = paper.read_state(sid)
    resume = {"strategy": "intraday_strangle", "params": d["params"], "feed": "kite", "mode": "live",
              "live": {"max_lots": 1, "daily_loss_limit": 3000}, "resumed": "2026-10-07 10:05:00"}
    paper.Slot(sid).desired.write_text(json.dumps(resume))
    paper.run_group(cfg, want, "kite", max_bars=3)
    paper.kill(sid)                                             # flat again...
    paper.run_group(cfg, want, "kite", max_bars=3)
    paper.Slot(sid).desired.write_text(json.dumps(resume))      # ...and started with the same press (a restart)
    n = len(_sells(fake))
    paper.run_group(cfg, want, "kite", max_bars=3)
    assert len(_sells(fake)) == n                               # the day's cycle stays done


def test_the_dashboard_marks_a_resume(tmp_path, monkeypatch):
    import pytest
    flask = pytest.importorskip("flask")
    from test_defined_risk import _panel
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "strategies.json").write_text(json.dumps([{"name": "intraday_strangle", "title": "IS", "live_ready": True,
                                                                     "params": [{"key": "lots", "label": "Lots", "default": 1, "type": "int"}]}]))
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    c = a.test_client()
    body = {"strategy": "intraday_strangle", "feed": "kite", "params": {"lots": 1}, "mode": "live", "confirm": "LIVE",
            "live": {"max_lots": 1, "daily_loss_limit": 3000}}
    sid = c.post("/api/paper/start", json=body).get_json()["id"]
    assert "resumed" not in json.loads((mod.SESS / sid / "desired.json").read_text())       # a first start
    (mod.SESS / sid / "state.json").write_text(json.dumps({"strategy": "intraday_strangle", "mode": "live", "positions": []}))
    (mod.SESS / sid / "desired.json").unlink()
    c.post("/api/paper/start", json={**body, "id": sid})
    assert json.loads((mod.SESS / sid / "desired.json").read_text())["resumed"]


def test_a_day_the_strategy_closed_itself_stays_closed():
    """Its day loss limit, day target or the 15:15 square-off: a Resume does not start another cycle."""
    from algolab.strategies.intraday_strangle import IntradayStrangle
    st = IntradayStrangle()
    st.day = {"date": "2026-11-23", "group": "002", "closed": True, "legs": {}}
    assert st.fresh_cycle("2026-11-23") is None and st.day["closed"]
    st.day = {"date": "2026-11-23", "group": "002", "legs": {"CE": {"open": True}}}
    assert "002" in st.fresh_cycle("2026-11-23") and st.day is None
