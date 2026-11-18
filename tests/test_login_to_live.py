"""Sign-in opens the LIVE session first when one holds positions (one: that session; several: the Live page),
otherwise the profile's start page as before."""
import json

import pytest

from test_defined_risk import PAGE, _panel


def _app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.secret_key = "x"
    app.register_blueprint(mod.panel)
    app.add_url_rule("/", "home", lambda: PAGE)
    return mod, app.test_client()


def _session(mod, sid, mode, positions):
    d = mod.SESS / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps({"strategy": "premium_strangle", "mode": mode, "positions": positions}))


def _sign_in(c):
    with c.session_transaction() as s:
        s.clear()
        s["user"] = "admin"
    return c.get("/")


def test_sign_in_opens_the_live_session_that_holds_positions(tmp_path, monkeypatch):
    mod, c = _app(tmp_path, monkeypatch)
    c.post("/api/profile", json={"start_page": "/paper"})
    _session(mod, "paper-1", "paper", [{"Contract": "x"}])           # paper positions: not a reason
    _session(mod, "ps-2", "live", [])                                 # a live session with nothing open: not either
    assert _sign_in(c).headers["Location"].endswith("/paper")        # the start page as before
    _session(mod, "ps-3", "live", [{"Contract": "NIFTY 24300 CE"}])
    r = _sign_in(c)
    assert r.status_code == 302 and r.headers["Location"].endswith("/live#ps-3")
    assert c.get("/").status_code == 200                              # once: then the page itself
    _session(mod, "ps-4", "live", [{"Contract": "NIFTY 23700 PE"}])
    assert _sign_in(c).headers["Location"].endswith("/live")          # several: the Live page
