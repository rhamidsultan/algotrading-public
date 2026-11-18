"""Paper/Live pages: each session's latest prices every second (runtime/sessions/<id>/tick.json)."""
import json
import time

import pytest

from test_defined_risk import _panel


def test_ticks_endpoint(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    rt = tmp_path / "runtime"
    rt.mkdir()
    mod = _panel(tmp_path, monkeypatch)
    sess = rt / "sessions"
    for sid, mode, alive in (("straddle", "paper", True), ("fly", "live", True), ("old", "paper", False)):
        d = sess / sid
        d.mkdir(parents=True)
        (d / "desired.json").write_text(json.dumps({"strategy": "short_straddle", "mode": mode, "feed": "kite"}))
        (d / "state.json").write_text(json.dumps({"strategy": "short_straddle", "mode": mode}))
        (d / "tick.json").write_text(json.dumps({"time": "2026-10-19T11:20:05", "spot": 25010.5, "equity": 301000,
                                                 "unrealized": 1200, "positions": [], "entry_charges": 40}))
        (d / "heartbeat").write_text(str(time.time() - (0 if alive else 600)))
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    paper = c.get("/api/paper/ticks?kind=paper").get_json()["sessions"]
    assert set(paper) == {"straddle"} and paper["straddle"]["spot"] == 25010.5     # stopped sessions left out
    assert set(c.get("/api/paper/ticks?kind=live").get_json()["sessions"]) == {"fly"}
    page = c.get("/paper").get_data(as_text=True)
    assert "/api/paper/ticks" in page and 'data-tk="plist"' in page


def test_ticks_route_needs_paper_or_live():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("up", Path(__file__).resolve().parents[1] / "deploy" / "portfolio_page" / "users_panel.py")
    up = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(up)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"users panel needs the dashboard: {e}")
    assert up.classify("GET", "/api/paper/ticks")[1] == ("paper", "live")


def test_today_uses_the_live_total(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    page = mod.SESSIONS_PAGE if hasattr(mod, "SESSIONS_PAGE") else open(mod.__file__, encoding="utf-8").read()
    assert "s.total - before" in page            # Today = account now - previous day's close (not the minute-old point)
    assert "last1.day += delta" not in page
