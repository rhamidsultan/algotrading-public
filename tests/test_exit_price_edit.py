"""LIVE: the exit price of a closed leg can be edited (e.g. a square-off in Kite booked at an estimate): P&L is
recalculated, the first price is kept and shown as "edited". Running sessions apply it in the engine; a stopped
session's saved account is edited by the dashboard, the same way."""
import json

import pytest

from test_kite_sync import SYM, _by_hand, _short
from test_platform import NOW


def _closed():
    k, b, c = _short()
    _by_hand(k, SYM, "BUY", 65, 14.0)
    b.check_positions(NOW)
    return k, b, b.trades[-1]


def test_the_broker_edits_one_closed_leg_and_keeps_the_first_price():
    k, b, t = _closed()
    net, real = t.net_pnl, b.net_realized
    key = b.trade_key(t.contract, t.exit_time, t.qty)
    n = len(k.orders)
    line = b.edit_exit(key, 15.0, "hamid", "2026-10-07 15:00")
    assert t.exit_price == 15.0 and t.net_pnl == pytest.approx(net - 65) and b.net_realized == pytest.approx(real - 65)
    assert b.fills[-1].price == 15.0 and b.exit_edits[key]["was"] == 14.0 and "14.00 -> 15.00" in line
    b.edit_exit(key, 13.0)
    assert b.exit_edits[key]["was"] == 14.0 and t.net_pnl == pytest.approx(net + 65)   # the first price stays
    assert len(k.orders) == n                                                       # nothing sent
    with pytest.raises(ValueError):
        b.edit_exit(key, 0)
    with pytest.raises(ValueError):
        b.edit_exit("nope", 10)


def _app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    from test_defined_risk import _panel
    mod = _panel(tmp_path, monkeypatch)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return mod, a.test_client()


def test_a_stopped_session_is_edited_like_the_engine_does(tmp_path, monkeypatch):
    mod, c = _app(tmp_path, monkeypatch)
    k, b, t = _closed()
    d = mod.SESS / "premium_strangle-5"
    d.mkdir(parents=True)
    state = {"strategy": "premium_strangle", "mode": "live", "broker": b.to_dict(), "net_realized": b.net_realized,
             "equity": 300000 + b.net_realized, "positions": []}
    (d / "state.json").write_text(json.dumps(state))
    key = b.trade_key(t.contract, t.exit_time, t.qty)
    legs = c.get(f"/api/paper/cycle?id=premium_strangle-5&cycle={t.group}").get_json()["legs"]
    assert legs[0]["key"] == key and "edited_from" not in legs[0]
    assert c.post("/api/paper/exit_price", json={"id": "premium_strangle-5", "key": key, "price": "x"}).status_code == 400
    r = c.post("/api/paper/exit_price", json={"id": "premium_strangle-5", "key": key, "price": 15}).get_json()
    assert r["applied"] and r["delta"] == -65
    saved = json.loads((d / "state.json").read_text())
    b.edit_exit(key, 15.0)                                      # the engine's way gives the same account
    assert saved["broker"]["trades"] == json.loads(json.dumps(b.to_dict()))["trades"]
    assert saved["broker"]["realized_gross"] == pytest.approx(b.realized_gross)
    assert saved["net_realized"] == pytest.approx(state["net_realized"] - 65)
    legs = c.get(f"/api/paper/cycle?id=premium_strangle-5&cycle={t.group}").get_json()["legs"]
    assert legs[0]["exit_price"] == 15 and legs[0]["edited_from"] == 14.0
    assert "exitEdit" in c.get("/live").get_data(as_text=True)


def test_a_running_session_gets_the_edit_through_its_engine(tmp_path, monkeypatch):
    from datetime import date

    from algolab import paper
    from test_live_next_day import NextDay, _session_env
    mod, c = _app(tmp_path, monkeypatch)
    fake = NextDay(date(2026, 10, 13))
    fake.ask = None
    cfg, clock, sid, want = _session_env(tmp_path, monkeypatch, fake)
    paper.run_group(cfg, want, "kite", max_bars=3)
    sym = next(o["sym"] for o in fake.sent() if o["side"] == "SELL" and o["sym"].endswith("CE"))
    _by_hand(fake, sym, "BUY", 65, 5.0)
    paper.run_group(cfg, want, "kite", max_bars=3)
    t = paper.read_state(sid)["broker"]["trades"][-1]
    key = mod._trade_key(t)
    if mod.SESS != paper.SESSIONS:                              # the dashboard and the engine share the sessions folder
        monkeypatch.setattr(mod, "SESS", paper.SESSIONS)
    r = c.post("/api/paper/exit_price", json={"id": sid, "key": key, "price": 6}).get_json()
    assert r == {"ok": True, "applied": False}
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    t2 = next(x for x in s["broker"]["trades"] if mod._trade_key(x) == key)
    assert t2["exit_price"] == 6 and t2["net_pnl"] == pytest.approx(t["net_pnl"] - 65)
    assert s["broker"]["live"]["exit_edits"][key]["was"] == 5.0 and any("edited: 5.00 -> 6.00" in x for x in s["logs"])
