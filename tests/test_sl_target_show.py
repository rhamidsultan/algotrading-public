"""26 Nov 2026: each live leg's SL / target in the activity log (and every move of a trailing stop), on the position
row (with how close the price is), and as red / green lines on the P&L chart."""
import json
from datetime import date

from algolab import paper
from test_fresh_cycle import _env, PARAMS  # noqa: F401


def test_the_session_logs_shows_and_records_each_legs_sl(tmp_path, monkeypatch):
    fake, cfg, sid, want = _env(tmp_path, monkeypatch)
    paper.run_group(cfg, want, "kite", max_bars=3)
    s = paper.read_state(sid)
    legs = {p["Contract"][-2:]: p for p in s["positions"]}
    assert legs["CE"]["SL"] > legs["CE"]["Avg price"] and legs["CE"]["Zerodha SL"] > legs["CE"]["SL"]
    assert any(" CE: SL " in x and "Zerodha stop" in x for x in s["logs"])
    rows = (paper.Slot(sid).dir / "levels.csv").read_text().splitlines()
    assert rows[0] == "time,at_sl,at_target" and len(rows) >= 2
    eq = float((paper.Slot(sid).dir / "equity.csv").read_text().splitlines()[-1].split(",")[1])
    assert float(rows[-1].split(",")[1]) < eq                    # at the SLs the account is lower


def test_a_moved_stop_is_logged():
    s = paper.Session.__new__(paper.Session)
    s.logs = []
    s._log = s.logs.append
    s._log_levels({"NIFTY X CE": {"sl": 128.25, "zerodha": 281.55}})
    s._log_levels({"NIFTY X CE": {"sl": 93.85, "zerodha": 281.55}})
    assert s.logs == ["NIFTY X CE: SL 128.25 · Zerodha stop 281.55", "NIFTY X CE: SL moved 128.25 -> 93.85"]


def test_the_dashboard_sends_the_lines_and_shows_the_row(tmp_path, monkeypatch):
    import pytest
    pytest.importorskip("flask")
    from test_defined_risk import _panel
    mod = _panel(tmp_path, monkeypatch)
    d = mod.SESS / "is-1"
    d.mkdir(parents=True)
    (d / "levels.csv").write_text("time,at_sl,at_target\n2026-11-26 09:21:00,297000.00,\n2026-11-26 09:30:00,299500.00,301000\n")
    lv = mod._levels_series(d / "levels.csv", 300000)
    assert lv == [["2026-11-26 09:21", -3000.0, None], ["2026-11-26 09:30", -500.0, 1000.0]]
    import flask
    a = flask.Flask("t")
    a.register_blueprint(mod.panel)
    page = a.test_client().get("/live").get_data(as_text=True)
    assert "function levelsRow(p)" in page and 'label: "At SL"' in page and 'label: "At target"' in page
