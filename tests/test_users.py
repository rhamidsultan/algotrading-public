"""Several logins on the dashboard: sign-in, access per area, hidden amounts, the Users page, activity."""
import json
import re
import sys
from pathlib import Path

import pytest

flask = pytest.importorskip("flask")
pytest.importorskip("cryptography")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_app_pwa import HTTPS, Authenticator  # noqa: E402

HERE = Path(__file__).resolve().parent.parent / "deploy" / "portfolio_page"
OWNER, OWNER_PW = "rosh", "owner-pass-1"

# Routes of the dashboard's own app.py, the Backtests page and the US stocks page (not loaded in tests).
APP_ROUTES = [("GET", "/"), ("GET", "/signin"), ("POST", "/signin"), ("GET", "/signout"), ("GET", "/login"),
              ("GET", "/callback"), ("POST", "/login/paste"), ("GET", "/api/data"), ("POST", "/api/sync"),
              ("GET", "/stock"), ("GET", "/gold"), ("GET", "/api/stock"), ("POST", "/stock/import"),
              ("GET", "/api/history"), ("GET", "/export.csv"), ("GET", "/api/live"), ("POST", "/api/test-alert"),
              ("GET", "/algo"), ("GET", "/api/algo"), ("POST", "/api/algo/command"), ("GET", "/backtests"),
              ("GET", "/api/backtests"), ("GET", "/api/backtests/run1"), ("POST", "/api/backtests/upload"),
              ("POST", "/api/backtests/run1/delete"), ("GET", "/api/backtests/history"), ("GET", "/us"),
              ("GET", "/api/us")]
OWNER_ONLY = {"/login", "/callback", "/login/paste", "/api/sync", "/api/test-alert", "/users", "/api/users",
              "/api/users/<name>/delete", "/api/users/<name>/passkeys/remove", "/api/users/password", "/api/activity",
              "/live/new", "/api/algo", "/api/algo/command", "/api/server-update"}

ROWS = [{"symbol": "INFY", "qty": 40, "avg_price": 1500.0, "last_price": 1600.0, "invested": 60000.0, "value": 64000.0,
         "pnl": 4000.0, "pnl_pct": 6.6667, "day_pnl": 800.0, "day_pct": 1.27, "weight": 64.0}]
SUMMARY = {"value": 100000.0, "invested": 95000.0, "pnl": 5000.0, "pnl_pct": 5.263, "day_pnl": 900.0, "day_pct": 0.91,
           "count": 2, "gold_value": 0.0, "gold_pct": 0.0}


def make_app(tmp_path, monkeypatch, users=True):
    import importlib.util
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    rt = tmp_path / "runtime"
    rt.mkdir(exist_ok=True)
    (rt / "strategies.json").write_text(json.dumps([{"name": "iron_fly", "title": "Iron fly", "params": [], "live_ready": True}]))
    trader = tmp_path / "trader"
    trader.mkdir(exist_ok=True)
    (trader / "config.json").write_text(json.dumps({"dashboard_user": OWNER, "dashboard_password": OWNER_PW, "port": 5010}))
    (trader / "templates").mkdir(exist_ok=True)
    (trader / "templates" / "signin.html").write_text(
        "<html><head></head><body><p class=err>{{ error or '' }}</p><form method=post></form></body></html>")
    for m in ("pwa_panel", "users_panel"):
        sys.modules.pop(m, None)
    if str(HERE) not in sys.path:                                    # on the server they sit beside algolab_panel.py
        sys.path.insert(0, str(HERE))
    if not users:
        monkeypatch.setitem(sys.modules, "users_panel", None)       # as if the file were missing
    spec = importlib.util.spec_from_file_location("algolab_panel_users", HERE / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(trader / "algolab_panel.py")                  # config.json beside it, like on the server
    spec.loader.exec_module(mod)
    mod.__file__ = str(trader / "algolab_panel.py")
    monkeypatch.setattr(mod, "_profile_dir", lambda: trader / "data")
    app = flask.Flask("t", template_folder=str(trader / "templates"))
    app.secret_key = "s"

    @app.before_request
    def require_signin():               # the dashboard's own check (app.py)
        if flask.request.endpoint in ("signin", "static") or flask.session.get("user"):
            return None
        if flask.request.path.startswith("/api/"):
            return flask.jsonify(error="not signed in"), 401
        return flask.redirect("/signin?next=" + flask.request.path)

    page = lambda title: (lambda: f"<html><head></head><body><div class=\"status\">x</div><h1>{title}</h1>"  # noqa: E731
                                  "<table><tr><th>Symbol</th><th>Qty</th><th>Value</th></tr><tr><td>INFY</td><td>40</td>"
                                  "<td>₹64,000</td></tr></table></body></html>")
    app.add_url_rule("/signin", "signin", lambda: flask.render_template("signin.html"), methods=["GET", "POST"])
    app.add_url_rule("/signout", "signout", lambda: (flask.session.clear(), flask.redirect("/signin"))[1])
    app.add_url_rule("/", "home", page("Portfolio"))
    app.add_url_rule("/us", "us", page("US"))
    app.add_url_rule("/login", "login", lambda: "zerodha")
    app.add_url_rule("/export.csv", "export", lambda: "day,value\n2026-10-14,64000\n")
    app.add_url_rule("/api/data", "data", lambda: flask.jsonify(
        holdings=ROWS, summary=SUMMARY, alerts=[{"ts": "10:00", "message": "INFY up 3%: day P&L +₹1,234.50"}],
        daily=[{"day": "2026-10-13", "value": 99000.0, "invested": 95000.0, "pnl": 4000.0, "day_pnl": -200.0}],
        updated="14 Oct, 10:00", market_open=True, next_sync_seconds=30, extremes={"day": {"high": 1200.0, "low": -50.0,
                                                                                            "from_high_pct": -0.3}}))
    app.add_url_rule("/api/live", "live_sse", lambda: flask.Response(
        iter(["retry: 3000\n\n", "data: " + json.dumps({"summary": SUMMARY, "holdings": ROWS}) + "\n\n"]),
        mimetype="text/event-stream"))
    app.add_url_rule("/api/us", "api_us", lambda: flask.jsonify(ok=True, invested=5000.0, value=5500.0, usd_inr=88.2,
                     positions=[{"symbol": "AAPL", "qty": 20, "avg_cost": 250.0, "market_value": 5500.0}]))
    app.add_url_rule("/api/sync", "sync", lambda: flask.jsonify(ok=True), methods=["POST"])
    app.register_blueprint(mod.panel)
    return mod, app, app.test_client()


def signin(c, user, pw, nxt=None):
    return c.post("/signin" + (f"?next={nxt}" if nxt else ""), data={"username": user, "password": pw})


def add_user(c, name, access, pw="temp-pass-1", hide=True, **extra):
    r = c.post("/api/users", json={"new": True, "name": name, "access": access, "password": pw, "hide_amounts": hide, **extra})
    assert r.status_code == 200, r.get_json()
    return r.get_json()["user"]


def first_password(c, user, temp="temp-pass-1", new="my-own-pass-9"):
    with c.session_transaction() as s:
        s.clear()
    r = signin(c, user, temp)
    assert r.status_code == 302 and r.headers["Location"].startswith("/account/password")
    assert c.post("/api/account/password", json={"current": temp, "new": new}).get_json() == {"ok": True}


def session_dir(tmp_path, sid, mode):
    d = tmp_path / "runtime" / "sessions" / sid
    d.mkdir(parents=True)
    (d / "desired.json").write_text(json.dumps({"strategy": "iron_fly", "params": {}, "feed": "kite", "mode": mode}))
    return d


def test_every_page_is_classified():
    sys.modules.pop("users_panel", None)
    sys.path.insert(0, str(HERE))
    import users_panel as up
    import importlib.util
    spec = importlib.util.spec_from_file_location("algolab_panel_routes", HERE / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = flask.Flask("routes")
    app.register_blueprint(mod.panel)
    routes = list(APP_ROUTES)
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        for m in rule.methods - {"HEAD", "OPTIONS"}:
            routes.append((m, rule.rule))
    unlisted = []
    for method, rule in routes:
        path = re.sub(r"<[^>]+>", "x", rule)
        c = up.classify(method, path)
        if c == ("owner", "unlisted") and rule not in OWNER_ONLY:
            unlisted.append(f"{method} {rule}")
        if rule in OWNER_ONLY:
            assert c[0] == "owner", rule
    assert unlisted == [], "add these to users_panel's rules: " + ", ".join(unlisted)
    assert up.classify("GET", "/something-new") == ("owner", "unlisted")      # new pages: owner only until listed


def test_owner_signs_in_with_the_config_password(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    assert mod._USERS_LOADED
    r = signin(c, OWNER, "wrong")
    assert r.status_code == 401 and b"Wrong username or password" in r.data
    r = signin(c, OWNER, OWNER_PW, "/paper")
    assert r.status_code == 302 and r.headers["Location"] == "/paper"
    assert c.get("/users").status_code == 200 and c.get("/login").status_code == 200
    acct = c.get("/api/account").get_json()
    assert acct["owner"] and acct["user"] == OWNER
    # five wrong passwords for one username lock it for 15 minutes
    with c.session_transaction() as s:
        s.clear()
    for _ in range(5):
        signin(c, "asha", "nope")
    assert b"Too many" in signin(c, "asha", "whatever").data
    assert signin(c, OWNER, OWNER_PW).status_code == 302                   # other logins unaffected


def test_family_login_sees_the_portfolio_with_amounts_hidden(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    (tmp_path / "trader" / "data" / "ibkr").mkdir(parents=True)          # the IBKR sync is installed
    signin(c, OWNER, OWNER_PW)
    u = add_user(c, "Asha", {"portfolio": "view", "us": "view", "live": "use"}, label="Sister")
    assert u["name"] == "asha" and u["access"] == {"portfolio": "view", "us": "view"}     # live: never 'use'
    assert c.post("/api/users", json={"new": True, "name": OWNER, "password": "x" * 9}).status_code == 400
    # first sign-in: own password before anything else
    with c.session_transaction() as s:
        s.clear()
    r = signin(c, "ASHA", "temp-pass-1", "/us")
    assert r.headers["Location"] == "/account/password?next=/us"
    assert c.get("/").headers["Location"].startswith("/account/password")
    assert c.get("/api/data").get_json()["change_password"]
    assert c.post("/api/account/password", json={"current": "temp-pass-1", "new": "short"}).status_code == 400
    assert c.post("/api/account/password", json={"current": "wrong-one", "new": "my-own-pass-9"}).status_code == 400
    assert c.post("/api/account/password", json={"current": "temp-pass-1", "new": "my-own-pass-9"}).get_json() == {"ok": True}
    # pages: portfolio and US only
    home = c.get("/").get_data(as_text=True)
    assert 'id="hide-amounts"' in home and "Amounts are hidden" in home
    nav = re.search(r'<nav class="sitenav">.*?</nav>', home, re.S).group(0)
    assert 'href="/"' in nav and 'href="/us"' in nav and 'href="/paper"' not in nav and 'href="/live"' not in nav
    r = c.get("/paper")
    assert r.status_code == 403 and b"No access" in r.data
    for path in ("/api/paper", "/users", "/api/users", "/api/activity", "/market-data"):
        assert c.get(path).status_code == 403, path
    assert c.get("/login").status_code == 403 and c.post("/api/sync", json={}).status_code == 403
    assert c.get("/export.csv").status_code == 403                         # the raw figures: not with hidden amounts
    # amounts: scaled on the server, percentages exact, amounts in text masked
    d = c.get("/api/data").get_json()
    h, s = d["holdings"][0], d["summary"]
    assert s["invested"] == pytest.approx(100000.0) and s["value"] != SUMMARY["value"]
    assert s["pnl_pct"] == SUMMARY["pnl_pct"] and h["pnl_pct"] == ROWS[0]["pnl_pct"] and h["weight"] == 64.0
    assert h["last_price"] == 1600.0 and h["qty"] != 40 and s["count"] == 2
    assert s["value"] / s["invested"] == pytest.approx(SUMMARY["value"] / SUMMARY["invested"])
    assert d["extremes"]["day"]["high"] != 1200.0 and d["extremes"]["day"]["from_high_pct"] == -0.3
    assert d["alerts"][0]["message"] == "INFY up 3%: day P&L +₹•••" and d["next_sync_seconds"] == 30
    sse = c.get("/api/live").get_data(as_text=True)
    ev = json.loads(sse.split("data: ")[1].split("\n")[0])
    assert ev["summary"]["invested"] == pytest.approx(100000.0) and ev["holdings"][0]["qty"] != 40
    us = c.get("/api/us").get_json()
    assert us["usd_inr"] == 88.2 and us["positions"][0]["avg_cost"] == 250.0 and us["value"] == pytest.approx(11000.0)
    # the owner still sees real figures
    with c.session_transaction() as s:
        s.clear()
    signin(c, OWNER, OWNER_PW)
    assert c.get("/api/data").get_json()["summary"]["value"] == 100000.0
    assert 'id="hide-amounts"' not in c.get("/").get_data(as_text=True)


def test_friend_runs_paper_sessions_but_never_live(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    session_dir(tmp_path, "iron_fly-live", "live")
    session_dir(tmp_path, "iron_fly-paper", "paper")
    signin(c, OWNER, OWNER_PW)
    add_user(c, "vik", {"paper": "use", "market_data": "view"})
    add_user(c, "meera", {"paper": "view"})
    first_password(c, "vik")
    r = c.get("/")
    assert r.status_code == 302 and r.headers["Location"] == "/summary"        # first page they may see
    ids = [s["id"] for s in c.get("/api/paper").get_json()["sessions"]]
    assert ids == ["iron_fly-paper"]                                         # live sessions are not shown
    assert c.get("/live").status_code == 403
    r = c.post("/api/paper/start", json={"strategy": "iron_fly", "feed": "sim", "label": "Vik's test"})
    assert r.status_code == 200
    sid = r.get_json()["id"]
    assert json.loads((tmp_path / "runtime" / "sessions" / sid / "started_by.json").read_text())["user"] == "vik"
    r = c.post("/api/paper/start", json={"strategy": "iron_fly", "feed": "kite", "mode": "live", "confirm": "LIVE",
                                         "live": {"daily_loss_limit": 1000}})
    assert r.status_code == 403 and "owner" in r.get_json()["error"]
    assert c.post("/api/paper/stop", json={"id": "iron_fly-live"}).status_code == 403
    assert c.post("/api/paper/kill", json={"id": "iron_fly-live"}).status_code == 403
    assert (tmp_path / "runtime" / "sessions" / "iron_fly-live" / "desired.json").exists()
    assert c.get("/api/paper/metrics.csv?kind=live").status_code == 200 and c.get("/paper/new").status_code == 200
    # view only: sees sessions, the buttons are hidden and refused
    first_password(c, "meera")
    page = c.get("/paper").get_data(as_text=True)
    assert "View only" in page and "button[data-act]{display:none" in page
    assert 'href="/paper/new"' not in page and c.get("/paper/new").status_code == 403
    r = c.post("/api/paper/stop", json={"id": "iron_fly-paper"})
    assert r.status_code == 403 and "view this but not change it" in r.get_json()["error"]
    with c.session_transaction() as s:
        s.clear()
    signin(c, "vik", "my-own-pass-9")
    assert c.post("/api/paper/stop", json={"id": "iron_fly-paper"}).status_code == 200
    # the owner sees who started what
    with c.session_transaction() as s:
        s.clear()
    signin(c, OWNER, OWNER_PW)
    views = {s["id"]: s for s in c.get("/api/paper").get_json()["sessions"]}
    assert set(views) == {"iron_fly-live", sid} and views[sid]["started_by"] == "vik"
    ev = c.get("/api/activity?user=vik").get_json()["events"]
    what = [e["event"] for e in ev]
    assert "POST /api/paper/start" in what and "POST /api/paper/stop" in what and "signin" in what
    start = next(e for e in ev if e["event"] == "POST /api/paper/start")
    assert start["detail"]["strategy"] == "iron_fly" and "my-own-pass" not in json.dumps(ev) and "temp-pass" not in json.dumps(ev)


def test_switching_off_or_resetting_signs_a_login_out(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    owner_c = app.test_client()
    signin(owner_c, OWNER, OWNER_PW)
    add_user(owner_c, "asha", {"portfolio": "view"})
    first_password(c, "asha")
    assert c.get("/").status_code == 200
    r = owner_c.post("/api/users", json={"name": "asha", "access": {"portfolio": "view"}, "disabled": True})
    assert r.get_json()["user"]["disabled"]
    assert c.get("/").status_code == 302 and c.get("/api/data").status_code == 401
    assert b"switched off" in signin(c, "asha", "my-own-pass-9").data
    owner_c.post("/api/users", json={"name": "asha", "access": {"portfolio": "view"}, "disabled": False})
    assert signin(c, "asha", "my-own-pass-9").status_code == 302 and c.get("/").status_code == 200
    owner_c.post("/api/users", json={"name": "asha", "access": {"portfolio": "view"}, "password": "reset-pass-2"})
    assert c.get("/api/data").status_code == 401                              # signed out by the reset
    assert signin(c, "asha", "reset-pass-2").headers["Location"].startswith("/account/password")
    assert owner_c.post("/api/users/asha/delete").get_json() == {"ok": True}
    assert signin(c, "asha", "reset-pass-2").status_code == 401
    events = [e["event"] for e in owner_c.get("/api/activity").get_json()["events"]]
    assert {"login created", "password reset", "login deleted", "signin failed"} <= set(events)


def test_each_login_has_its_own_profile_face_id_and_notifications(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    owner_c = app.test_client()
    signin(owner_c, OWNER, OWNER_PW)
    owner_c.post("/api/profile", json={"name": "Rosh", "theme": "dark"})
    add_user(owner_c, "vik", {"paper": "use"})
    first_password(c, "vik")
    c.post("/api/profile", json={"name": "Vikram", "start_page": "/"})
    assert c.get("/api/profile").get_json()["profile"]["name"] == "Vikram"
    assert owner_c.get("/api/profile").get_json()["profile"]["name"] == "Rosh"
    assert set(c.get("/api/profile").get_json()["start_pages"]) == {"/summary", "/paper"}
    assert c.get("/api/profile").get_json()["profile"]["start_page"] == "/"    # not allowed: not saved
    # Face ID for vik signs in as vik
    a = Authenticator()
    opts = c.post("/api/passkey/register/options", headers=HTTPS).get_json()["publicKey"]
    assert c.post("/api/passkey/register", json=a.create(opts), headers=HTTPS).get_json() == {"ok": True}
    assert owner_c.get("/api/passkeys", headers=HTTPS).get_json()["passkeys"] == []
    with c.session_transaction() as s:
        s.clear()
    lo = c.post("/passkey/login/options", headers=HTTPS).get_json()["publicKey"]
    assert c.post("/passkey/login", json=a.get(lo), headers=HTTPS).get_json()["ok"]
    assert c.get("/api/account").get_json()["user"] == "vik"
    # notifications: categories by access; the owner removes their keys
    sub = {"endpoint": "https://web.push.apple.com/VIK", "keys": {"p256dh": "x", "auth": "y"}}
    assert c.post("/api/push/subscribe", json={"subscription": sub, "name": "iPhone"}).status_code == 200
    assert set(c.get("/api/push").get_json()["categories"]) == {"trading", "daily"}
    assert owner_c.get("/api/push").get_json()["devices"] == []
    assert mod.push_allowed("vik", {"category": "trading", "url": "/paper"})
    assert not mod.push_allowed("vik", {"category": "trading", "url": "/live"})
    assert not mod.push_allowed("vik", {"category": "logins"}) and mod.push_allowed(OWNER, {"category": "logins"})
    assert owner_c.post("/api/users/vik/passkeys/remove").get_json()["removed"] == 1
    assert c.get("/api/push").get_json()["devices"] == []
    with c.session_transaction() as s:
        s.clear()
    lo = c.post("/passkey/login/options", headers=HTTPS).get_json()
    assert "error" in lo


def test_owner_password_change_updates_config(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    signin(c, OWNER, OWNER_PW)
    assert c.post("/api/account/password", json={"current": "nope", "new": "brand-new-pass"}).status_code == 400
    r = c.post("/api/account/password", json={"current": OWNER_PW, "new": "brand-new-pass"})
    assert r.get_json()["ok"]
    cfg = json.loads((tmp_path / "trader" / "config.json").read_text())
    assert cfg["dashboard_password"] == "brand-new-pass" and cfg["port"] == 5010
    with c.session_transaction() as s:
        s.clear()
    assert signin(c, OWNER, OWNER_PW).status_code == 401 and signin(c, OWNER, "brand-new-pass").status_code == 302


def test_without_the_logins_add_on_only_the_owner_gets_in(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch, users=False)
    assert not mod._USERS_LOADED
    with c.session_transaction() as s:
        s["user"] = "asha"
    assert c.get("/api/paper").status_code == 401
    with c.session_transaction() as s:
        s["user"] = OWNER
    assert c.get("/api/paper").status_code == 200


def test_us_is_optional_until_the_ibkr_sync_is_installed(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    signin(c, OWNER, OWNER_PW)
    nav = re.search(r'<nav class="sitenav">.*?</nav>', c.get("/").get_data(as_text=True), re.S).group(0)
    assert ">US<" not in nav and 'href="/stock"' in nav
    assert "us" not in c.get("/api/summary").get_json()


def test_portfolio_menu_holds_stocks_and_us(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    (tmp_path / "trader" / "data" / "ibkr").mkdir(parents=True)
    signin(c, OWNER, OWNER_PW)
    nav = re.search(r'<nav class="sitenav">.*?</nav>', c.get("/us").get_data(as_text=True), re.S).group(0)
    top = re.search(r'<div class="sn-in">(.*?)</div>', nav, re.S).group(1)
    sub = re.search(r'<div class="sn-sub">(.*?)</div>', nav, re.S).group(1)
    assert ">Stocks<" not in top and ">US<" not in top and 'class=on>Portfolio' in top
    assert re.findall(r">(\w+)</a>", sub) == ["India", "Stocks", "US"] and '<a href="/us" class=on>US' in sub
    add_user(c, "nri", {"us": "view"})
    first_password(c, "nri")
    nav = re.search(r'<nav class="sitenav">.*?</nav>', c.get("/us").get_data(as_text=True), re.S).group(0)
    assert '<a href="/us" class=on>Portfolio</a>' in nav and 'href="/stock"' not in nav   # only what they may open


def test_summary_page_shows_what_each_login_may_see(tmp_path, monkeypatch):
    main = sys.modules["__main__"]
    monkeypatch.setattr(main, "STATE", {"summary": SUMMARY, "holdings": ROWS + [dict(ROWS[0], symbol="TCS", day_pct=-2.1)],
                                        "updated": "14 Oct, 13:00", "logged_in": True}, raising=False)
    mod, app, c = make_app(tmp_path, monkeypatch)
    d = tmp_path / "trader" / "data" / "ibkr"
    d.mkdir(parents=True)
    (d / "ibkr_snapshot.json").write_text(json.dumps({
        "timestamp_utc": "2026-10-14T09:00:00+00:00", "timestamp_ist": "14 Oct 14:30", "usd_inr": 88.0,
        "summary_usd": {"net_liquidation": 5600.0, "daily_pnl": 56.0},
        "positions": [{"symbol": "AAPL", "qty": 20, "avg_cost": 250.0, "market_value": 5500.0}]}))
    session_dir(tmp_path, "iron_fly-paper", "paper")
    signin(c, OWNER, OWNER_PW)
    s = c.get("/api/summary").get_json()
    assert s["owner"] and s["india"]["value"] == 100000.0 and s["india"]["worst"][0]["symbol"] == "TCS"
    assert s["us"]["net_liq"] == 5600.0 and s["us"]["day_pct"] == pytest.approx(1.01)
    assert s["networth"]["total"] == pytest.approx(100000 + 5600 * 88) and "health" in s and s["paper"]["total"] == 1
    page = c.get("/summary").get_data(as_text=True)
    assert 'id="grid"' in page and '<a href="/summary" class=on>Summary</a>' in page
    add_user(c, "asha", {"portfolio": "view", "us": "view"})
    add_user(c, "vik", {"paper": "use"})
    first_password(c, "asha")
    s = c.get("/api/summary").get_json()
    assert s["hidden"] and s["india"]["value"] is None and s["india"]["pnl_pct"] == SUMMARY["pnl_pct"]
    assert "alerts" not in s["india"] and s["us"]["net_liq"] is None and s["us"]["pnl_pct"] == 10.0
    assert "networth" not in s and "paper" not in s and "health" not in s and not s["owner"]
    first_password(c, "vik")
    s = c.get("/api/summary").get_json()
    assert set(s) >= {"paper", "market"} and "india" not in s and "us" not in s and "live" not in s


def test_margins_shown_and_refreshed_from_the_paper_page(tmp_path, monkeypatch):
    mod, app, c = make_app(tmp_path, monkeypatch)
    rt = tmp_path / "runtime"
    session_dir(tmp_path, "iron_fly-paper", "paper")
    session_dir(tmp_path, "iron_fly-live", "live")
    (rt / "margins.json").write_text(json.dumps({
        "time": "2026-10-14 10:30", "lot_size": 65,
        "strategies": {"iron_fly": {"margin": 58500.0, "unit": "1 lot per leg", "legs": [{"text": "SELL 24000 CE 20 Oct", "qty": 65}]},
                       "blaze_butterfly": {"error": "would not enter at today's prices"}},
        "sessions": {"iron_fly-paper": {"margin": 60000.0}, "iron_fly-live": {"margin": 61000.0}}}))
    signin(c, OWNER, OWNER_PW)
    m = c.get("/api/paper").get_json()["margins"]
    assert m["strategies"]["iron_fly"]["margin"] == 58500.0 and m["sessions"] == {"iron_fly-paper": 60000.0, "iron_fly-live": 61000.0}
    page = c.get("/paper/new").get_data(as_text=True)
    assert 'id="marginNote"' in page and "refreshMargins" in page
    s = c.get("/api/summary").get_json()
    assert "server" in s["health"] and "memory" in s["health"]["server"]
    add_user(c, "vik", {"paper": "use"})
    add_user(c, "meera", {"paper": "view"})
    first_password(c, "meera")
    assert c.get("/api/paper").get_json()["margins"]["sessions"] == {"iron_fly-paper": 60000.0}   # not the live one
    assert c.post("/api/paper/margins").status_code == 403 and not (rt / "margins_request").exists()
    first_password(c, "vik")
    assert c.post("/api/paper/margins").get_json() == {"ok": True} and (rt / "margins_request").exists()
    assert c.get("/api/paper").get_json()["margins"]["requested"]
