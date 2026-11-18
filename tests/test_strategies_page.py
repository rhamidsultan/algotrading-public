"""Algo Studio: the strategies, their presets, and jobs/sessions that remember their preset; sign-in through the dashboard."""
import json
import sys
import time
from pathlib import Path

import pytest

from test_risk_screens import _catalog
from test_defined_risk import _panel

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"user": "rosh", "owner": True, "access": {}, "live_use": True}
VIEWER = {"user": "ali", "owner": False, "access": {"backtests": "view"}, "live_use": False}
NOBODY = {"user": "sam", "owner": False, "access": {"portfolio": "view"}, "live_use": False}


def _studio(tmp_path, monkeypatch, who=OWNER):
    pytest.importorskip("flask")
    _catalog(tmp_path, monkeypatch)
    monkeypatch.setenv("STUDIO_RUNTIME", str(tmp_path / "runtime"))
    monkeypatch.setenv("BACKTESTS_STORE", str(tmp_path / "store"))
    from studio.app import create_app
    people = {"c-owner": OWNER, "c-viewer": VIEWER, "c-nobody": NOBODY}
    app = create_app(auth=lambda cookie: people.get(cookie.split("=", 1)[-1]))
    c = app.test_client()
    if who:
        key = next(k for k, v in people.items() if v is who)
        c.set_cookie("session", key, domain="localhost")
    return c


def test_studio_presets_and_strategy_pages(tmp_path, monkeypatch):
    c = _studio(tmp_path, monkeypatch)
    assert c.get("/studio/api/me").get_json() == {"user": "rosh", "owner": True, "can_use": True, "live_use": True}
    r = c.post("/studio/api/presets", json={"strategy": "short_straddle", "name": "Tight stop", "source": "Made by hand",
                                            "params": {"lots": "2", "stop_loss_pct": "25", "risk.stops": "framework",
                                                       "risk.leg_stop": "30", "bogus": 1}})
    assert r.status_code == 200, r.get_json()
    pid = r.get_json()["id"]
    assert pid == "tight-stop" and r.get_json()["preset"]["params"] == {"lots": 2, "stop_loss_pct": 25.0, "risk.stops": "framework",
                                                                        "risk.leg_stop": 30.0}
    assert r.get_json()["preset"]["by"] == "rosh"
    assert c.post("/studio/api/presets", json={"strategy": "short_straddle", "name": "tight STOP", "params": {}}).status_code == 400
    assert c.post("/studio/api/presets", json={"strategy": "short_straddle", "name": "Bad", "params": {"risk.stops": "maybe"}}).status_code == 400
    assert c.post("/studio/api/presets", json={"strategy": "nope", "name": "X", "params": {}}).status_code == 404
    r = c.post("/studio/api/presets", json={"strategy": "short_straddle", "id": pid, "name": "Tight stop", "params": {"lots": 3}})
    assert r.get_json()["preset"]["version"] == 2
    # a saved backtest made with the preset, and a paper session started with it
    run = tmp_path / "store" / "20261019-120000_short_straddle"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"strategy": "short_straddle", "saved": "2026-10-19T12:00:00", "note": "preset Tight stop",
                                              "preset": {"id": pid, "name": "Tight stop"}, "data_from": "2025-01-01", "data_to": "2026-10-16",
                                              "metrics": {"Net P&L": 12345, "Max drawdown": -4000, "Cycles": 50}}))
    sess = tmp_path / "runtime" / "sessions" / "short_straddle"
    sess.mkdir(parents=True)
    (sess / "desired.json").write_text(json.dumps({"strategy": "short_straddle", "params": {"lots": 3}, "feed": "kite",
                                                   "preset": {"id": pid, "name": "Tight stop", "version": 2}}))
    (sess / "heartbeat").write_text(str(time.time()))
    lst = {s["name"]: s for s in c.get("/studio/api/strategies").get_json()["strategies"]}
    ss = lst["short_straddle"]
    assert ss["stage"] == "Paper" and ss["presets"] == 1 and ss["backtest"]["net"] == 12345 and ss["paper"]["running"] == 1
    assert lst["iron_fly"]["stage"] == "Draft"
    d = c.get("/studio/api/strategies/short_straddle").get_json()
    assert d["presets"][0]["name"] == "Tight stop" and d["runs"][0]["preset"] == "Tight stop"
    assert d["sessions"][0]["preset"] == "Tight stop" and any(p["key"] == "risk.stops" for p in d["catalog"]["params"])
    assert c.get("/studio/api/strategies/nope").status_code == 404
    assert c.post("/studio/api/presets/delete", json={"strategy": "short_straddle", "id": pid}).status_code == 200
    assert c.post("/studio/api/presets/delete", json={"strategy": "short_straddle", "id": pid}).status_code == 404
    assert c.get("/studio/api/strategies/short_straddle").get_json()["presets"] == []
    # the pages
    assert c.get("/studio").status_code in (301, 302, 308)
    assert "/studio/static/app.js" in c.get("/studio/").get_data(as_text=True)
    js = c.get("/studio/static/app.js").get_data(as_text=True)
    assert "presets/delete" in js and "/backtests/run?" in js and "/paper/new?" in js
    assert c.get("/studio/api/me").headers["Cache-Control"] == "no-store"


def test_studio_saves_a_backtest_as_a_preset(tmp_path, monkeypatch):
    c = _studio(tmp_path, monkeypatch)
    run = tmp_path / "store" / "20261019-120000_short_straddle"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"strategy": "short_straddle", "dataset": "NIFTY_minute", "params": {"lots": 1, "stop_loss_pct": 30},
                                              "config": {"risk": {"stops": "framework", "leg_stop": 25.0}}, "metrics": {}}))
    r = c.post("/studio/api/presets/from-run", json={"run": run.name, "name": "From Oct"})
    assert r.status_code == 200, r.get_json()
    p = r.get_json()["preset"]
    assert p["params"] == {"lots": 1, "stop_loss_pct": 30.0, "risk.stops": "framework", "risk.leg_stop": 25.0}
    assert p["source"].startswith("From backtest") and r.get_json()["strategy"] == "short_straddle"
    for bad in ("../x", "nope", ""):
        assert c.post("/studio/api/presets/from-run", json={"run": bad, "name": "X"}).status_code == 404
    assert c.post("/studio/api/presets/from-run", json={"run": run.name, "name": "from oct"}).status_code == 400   # same name
    assert c.get("/studio/api/strategies/short_straddle").get_json()["runs"][0]["dataset"] == "NIFTY_minute"
    js = c.get("/studio/static/app.js").get_data(as_text=True)
    assert "/api/backtest-runner" in js and "/api/paper/start" in js and "presets/from-run" in js


def test_studio_sign_in_and_access(tmp_path, monkeypatch):
    anon = _studio(tmp_path, monkeypatch, who=None)
    assert anon.get("/studio/api/strategies").status_code == 401
    r = anon.get("/studio/")
    assert r.status_code == 302 and r.headers["Location"].startswith("/signin?next=/studio/")
    viewer = _studio(tmp_path, monkeypatch, who=VIEWER)
    assert viewer.get("/studio/api/strategies").status_code == 200
    assert viewer.get("/studio/api/me").get_json()["can_use"] is False
    assert viewer.post("/studio/api/presets", json={"strategy": "short_straddle", "name": "X", "params": {}}).status_code == 403
    nobody = _studio(tmp_path, monkeypatch, who=NOBODY)
    assert nobody.get("/studio/api/strategies").status_code == 403
    assert nobody.get("/studio/").status_code == 403


def test_studio_asks_the_dashboard_once_per_cookie(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    _catalog(tmp_path, monkeypatch)
    monkeypatch.setenv("STUDIO_RUNTIME", str(tmp_path / "runtime"))
    from studio.app import create_app
    calls = []
    app = create_app(auth=lambda cookie: calls.append(cookie) or OWNER)
    c = app.test_client()
    c.set_cookie("session", "abc", domain="localhost")
    for _ in range(3):
        assert c.get("/studio/api/me").status_code == 200
    assert len(calls) == 1


def test_old_pages_use_studio_presets(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    _catalog(tmp_path, monkeypatch)
    monkeypatch.setenv("BACKTESTS_STORE", str(tmp_path / "store"))
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    (tmp_path / "runtime" / "presets.json").write_text(json.dumps(
        {"short_straddle": {"tight-stop": {"name": "Tight stop", "params": {"lots": 2}, "version": 3}}}))
    r = c.post("/api/paper/start", json={"strategy": "short_straddle", "feed": "sim", "params": {"lots": 1}, "preset": {"id": "tight-stop"}})
    sid = r.get_json()["id"]
    want = json.loads((tmp_path / "runtime" / "sessions" / sid / "desired.json").read_text())
    assert want["preset"] == {"id": "tight-stop", "name": "Tight stop", "version": 3}
    spot = tmp_path / "data" / "spot"
    spot.mkdir(parents=True)
    (spot / "NIFTY_minute.csv").write_text("datetime,open,high,low,close,vix\n2025-01-01 09:15:00,1,1,1,1,1\n2026-10-16 15:29:00,1,1,1,1,1\n")
    body = {"strategy": "short_straddle", "dataset": "NIFTY_minute", "from": "2025-01-01", "to": "2025-03-01", "params": {}}
    assert c.post("/api/backtest-runner", json={**body, "preset": {"id": "tight-stop"}}).get_json()["job"]["preset"]["name"] == "Tight stop"
    assert "preset" not in c.post("/api/backtest-runner", json={**body, "preset": {"id": "ghost"}}).get_json()["job"]
    run = c.get("/backtests/run").get_data(as_text=True)
    assert "applyPreset" in run and "/studio/api/strategies/" in run
    assert "/studio/api/presets" in c.get("/backtests/sweep").get_data(as_text=True)
    assert 'href="/studio/"' in run
    assert c.get("/strategies").status_code == 404 and c.get("/api/strategies").status_code == 404


def test_engine_notes_the_preset_on_a_saved_run(tmp_path, monkeypatch):
    from algolab import backtest_queue as bq, runs
    from algolab.config import load_config
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    cfg = load_config()
    cfg["pricing"]["real_only"] = False
    cfg["pricing"]["smile"] = "off"
    job = {"strategy": "intraday_straddle", "dataset": "SAMPLE_NIFTY_5minute", "from": "2025-01-01", "to": "2025-01-15",
           "preset": {"id": "base-2", "name": "Base 2"}}
    out = bq.run_one(job, store=tmp_path / "store", cfg=cfg)
    meta = json.loads((tmp_path / "store" / out["run"] / "run.json").read_text())
    assert meta["preset"] == {"id": "base-2", "name": "Base 2"} and "preset Base 2" in meta["note"]


def test_dashboard_tells_studio_who_is_signed_in(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_users import OWNER as NAME, OWNER_PW, make_app, signin
    mod, app, c = make_app(tmp_path, monkeypatch)
    assert c.get("/api/studio-auth").status_code == 401
    signin(c, NAME, OWNER_PW)
    d = c.get("/api/studio-auth").get_json()
    assert d["user"] == NAME and d["owner"] is True
    import users_panel as up
    assert "backtests" in str(up.classify("GET", "/studio/"))


def test_update_installs_studio():
    up = (ROOT / "deploy" / "update_engine.sh").read_text()
    assert "for item in algolab studio " in up and "algolab-studio.service" in up and "studio_caddy.sh" in up
    assert "ExecStart=$PY -m studio" in up


def test_caddy_gets_the_studio_block(tmp_path):
    import shutil
    import subprocess
    if shutil.which("bash") is None or shutil.which("python3") is None:
        pytest.skip("needs bash and python3")
    f = tmp_path / "Caddyfile"
    f.write_text("# x\n1-2-3-4.sslip.io {\n\tencode gzip\n\treverse_proxy 127.0.0.1:5010\n}\n")
    script = (ROOT / "deploy" / "studio_caddy.sh").read_text().replace('[ "$(id -u)" = 0 ] || { echo "Run with sudo"; exit 1; }', "")
    s = tmp_path / "s.sh"
    s.write_text(script.replace("command -v caddy", "false && command -v caddy").replace("systemctl reload caddy", "true"))
    env = {"CADDYFILE": str(f), "PATH": "/usr/bin:/bin"}
    subprocess.run(["bash", str(s)], env=env, check=True, capture_output=True)
    out = f.read_text()
    assert out.index("handle /studio*") < out.index("reverse_proxy 127.0.0.1:5010") and "reverse_proxy 127.0.0.1:8020" in out
    subprocess.run(["bash", str(s)], env=env, check=True, capture_output=True)
    assert f.read_text().count("handle /studio*") == 1


def test_studio_analysis_and_stage_gates(tmp_path, monkeypatch):
    c = _studio(tmp_path, monkeypatch)
    rt = tmp_path / "runtime"
    # a backtest: 8 months, 40 cycles, positive, 90% real prices
    run = tmp_path / "store" / "20261019-120000_short_straddle"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"strategy": "short_straddle", "data_from": "2026-01-01", "data_to": "2026-09-01",
                                              "metrics": {"Net P&L": 50000, "Net with charges 50% higher": 30000, "Cycles": 40,
                                                          "Fills on real option prices": "90%", "Max drawdown": -9000}}))
    (run / "cycles.csv").write_text("group,entry_time,exit_time,net_pnl\na,2026-01-05,2026-01-06,1000\nb,2026-02-02,2026-02-03,-400\n")
    d = c.get("/studio/api/strategies/short_straddle").get_json()
    assert d["gates"]["next"] == "Paper" and d["gates"]["ready"] is True and d["gates"]["passed"] == 5
    a = c.get("/studio/api/strategies/short_straddle/analysis").get_json()
    assert a["robust"]["months"] == [{"month": "2026-01", "net": 1000}, {"month": "2026-02", "net": -400}]
    assert any(f["label"] == "Charges +50%" and f["value"] == 30000 for f in a["robust"]["figs"])
    # a paper session on Zerodha prices, 3 days, compared: paper 25% behind its backtest
    s = rt / "sessions" / "short_straddle"
    s.mkdir(parents=True)
    trades = [{"exit_time": f"2026-10-1{i}T15:10:00", "net_pnl": 1000} for i in (4, 5, 6)]
    (s / "state.json").write_text(json.dumps({"strategy": "short_straddle", "feed": "kite", "mode": "paper", "broker": {"trades": trades}}))
    (s / "desired.json").write_text(json.dumps({"strategy": "short_straddle", "feed": "kite", "mode": "paper"}))
    (s / "equity.csv").write_text("time,equity,spot\n2026-10-14T09:16:00,1,1\n2026-10-15T09:16:00,1,1\n2026-10-16T09:16:00,1,1\n")
    (rt / "paper_compare.json").write_text(json.dumps({"short_straddle": {
        "session": "short_straddle", "from": "2026-10-14", "to": "2026-10-16", "paper_net": 3000, "backtest_net": 4000,
        "days": [{"day": "2026-10-14", "backtest": 2000}], "fills": {"avg_diff": 0.5, "matched": 6, "paper_only": 0, "backtest_only": 1}}}))
    g = c.get("/studio/api/strategies/short_straddle").get_json()["gates"]
    by = {x["name"]: x for x in g["checks"]}
    assert g["next"] == "Shadow" and not g["ready"]
    assert by["10+ trading days in paper"]["ok"] is False and by["Paper on Zerodha live prices"]["ok"] is True
    gap = next(x for x in g["checks"] if "within" in x["name"])
    assert gap["ok"] is False and "25%" in gap["note"]
    a = c.get("/studio/api/strategies/short_straddle/analysis").get_json()
    assert a["days"][0] == {"day": "2026-10-14", "paper": 1000, "shadow": None, "backtest": 2000}
    assert a["totals"]["paper"] == 3000 and a["gap"][0]["avg_fill"] == 0.5
    # Compare: queued for the server's backtest runner; simulated sessions are refused
    r = c.post("/studio/api/compare", json={"strategy": "short_straddle", "session": "short_straddle", "days": 14})
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    assert job["kind"] == "compare" and (rt / "backtest-queue" / f"{job['id']}.json").exists()
    assert c.get("/studio/api/strategies/short_straddle/analysis").get_json()["jobs"][0]["status"] == "queued"
    assert c.post("/studio/api/compare", json={"strategy": "short_straddle", "session": "ghost"}).status_code == 404
    (s / "desired.json").write_text(json.dumps({"strategy": "short_straddle", "feed": "sim", "mode": "paper"}))
    assert c.post("/studio/api/compare", json={"strategy": "short_straddle", "session": "short_straddle"}).status_code == 400
    js = c.get("/studio/static/app.js").get_data(as_text=True)
    assert "Same days, three ways" in js and "gatesCard" in js


def test_studio_share_and_import(tmp_path, monkeypatch):
    from studio import share
    c = _studio(tmp_path, monkeypatch)
    r = c.post("/studio/api/presets", json={"strategy": "short_straddle", "name": "Trail 10",
                                            "params": {"lots": 2, "risk.stops": "framework", "risk.leg_stop": "20"}})
    pid = r.get_json()["id"]
    run = tmp_path / "store" / "20261019-120000_short_straddle"
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"strategy": "short_straddle", "preset": {"id": pid, "name": "Trail 10"}, "data_from": "2025-10-01",
                                              "data_to": "2026-10-01", "metrics": {"Net P&L": 42000, "Max drawdown": -9000, "Cycles": 48}}))
    j = c.post("/studio/api/share", json={"strategy": "short_straddle", "preset": pid, "backtest": True, "to": "Arun"}).get_json()
    code = j["code"]
    assert code.startswith("ALGOSHARE1.") and j["payload"]["backtest"]["net"] == 42000 and "results" not in j["payload"]
    assert set(j["payload"]) <= {"v", "kind", "id", "version", "strategy", "title", "from", "shared", "preset", "backtest", "results"}
    assert str(tmp_path) not in json.dumps(j["payload"])                          # no paths, accounts or addresses
    assert c.get("/studio/api/shares/short_straddle").get_json()["shares"][0]["to"] == ["Arun"]
    # import on a friend's server (here: the same one): checks, then a new preset
    pv = c.post("/studio/api/import/preview", json={"code": code}).get_json()
    assert pv["ok"] and "update_of" not in pv and all(x["ok"] for x in pv["checks"])
    r = c.post("/studio/api/import", json={"code": code, "how": "new"}).get_json()
    assert r["name"] == "Trail 10 (2)"
    mine = c.get("/studio/api/strategies/short_straddle").get_json()["presets"]
    got = next(p for p in mine if p["id"] == r["id"])
    assert got["params"]["risk.leg_stop"] == 20.0 and got["share"]["from"] == "rosh" and got["source"].startswith("From rosh")
    # the sharer changes the preset and sends a new code: the friend sees what changed and updates
    c.post("/studio/api/presets", json={"strategy": "short_straddle", "id": pid, "name": "Trail 10",
                                        "params": {"lots": 2, "risk.stops": "framework", "risk.leg_stop": "18"}})
    assert c.get("/studio/api/shares/short_straddle").get_json()["shares"][0]["changed"] is True
    code2 = c.post("/studio/api/share", json={"strategy": "short_straddle", "preset": pid}).get_json()["code"]
    pv = c.post("/studio/api/import/preview", json={"code": code2}).get_json()
    ch = pv["update_of"]["changes"]
    assert pv["update_of"]["id"] == r["id"] and ch[0]["before"] == 20.0 and ch[0]["after"] == 18.0
    c.post("/studio/api/import", json={"code": code2, "how": "update"})
    got = next(p for p in c.get("/studio/api/strategies/short_straddle").get_json()["presets"] if p["id"] == r["id"])
    assert got["params"]["risk.leg_stop"] == 18.0 and got["share"]["version"] == 2
    c.post("/studio/api/presets", json={"strategy": "short_straddle", "id": r["id"], "name": got["name"], "params": {"lots": 1}})
    got = next(p for p in c.get("/studio/api/strategies/short_straddle").get_json()["presets"] if p["id"] == r["id"])
    assert got["share"]["from"] == "rosh"                                          # editing keeps where it came from
    # broken, foreign or unknown shares are refused
    assert c.post("/studio/api/import/preview", json={"code": code[:-2] + "00"}).status_code == 400
    assert c.post("/studio/api/import/preview", json={"code": "hello"}).status_code == 400
    bad = share.encode({"v": 1, "kind": "preset", "id": "abcdefgh", "strategy": "made_up", "preset": {"name": "X", "params": {}}})
    pv = c.post("/studio/api/import/preview", json={"code": bad}).get_json()
    assert pv["ok"] is False and c.post("/studio/api/import", json={"code": bad}).status_code == 400
    odd = share.encode({"v": 1, "kind": "preset", "id": "abcdefgh", "strategy": "short_straddle",
                        "preset": {"name": "X", "params": {"lots": 2, "not_here": 5, "evil": {"x": 1}}}})
    pv = c.post("/studio/api/import/preview", json={"code": odd}).get_json()
    assert pv["ok"] and "not_here" in pv["checks"][-1]["note"] and pv["params"] == {"lots": 2}
    viewer = _studio(tmp_path, monkeypatch, who=VIEWER)
    assert viewer.post("/studio/api/import", json={"code": code}).status_code == 403
    assert viewer.post("/studio/api/share", json={"strategy": "short_straddle", "preset": pid}).status_code == 403


def test_studio_results_by_strategy_and_preset(tmp_path, monkeypatch):
    c = _studio(tmp_path, monkeypatch)
    store = tmp_path / "store"

    def run(name, strategy, net, note="", preset=None, stress=1, frm="2026-01-01", to="2026-09-30", saved="2026-10-10T10:00:00"):
        d = store / name
        d.mkdir(parents=True)
        m = {"strategy": strategy, "note": note, "dataset": "NIFTY_minute", "data_from": frm, "data_to": to, "saved": saved,
             "metrics": {"Net P&L": net, "Max drawdown": -5000, "Cycles": 40}, "config": {"execution": {"cost_stress": stress}}}
        if preset:
            m["preset"] = preset
        (d / "run.json").write_text(json.dumps(m))
    trail = {"id": "trail-10", "name": "Trail 10"}
    run("20261010-100000_short_straddle", "short_straddle", 12000, "first look · preset Trail 10 · run on the server for rosh", trail)
    run("20261011-100000_short_straddle", "short_straddle", 30000, "preset Trail 10 · run on the Mac for rosh", trail, stress=2,
        frm="2024-10-01", saved="2026-10-11T10:00:00")
    run("20261012-100000_short_straddle", "short_straddle", 900, "Paper vs backtest: session x, 06 Oct – 12 Oct", saved="2026-10-12T10:00:00")
    run("20261013-100000_gone", "gone", 100)
    d = c.get("/studio/api/results").get_json()
    g = {x["strategy"]: x for x in d["groups"]}
    assert g["short_straddle"]["count"] == 3 and g["short_straddle"]["best"]["net"] == 30000 and g["gone"]["known"] is False
    assert [x["title"] for x in d["groups"]][-1] == "gone"                                  # unsorted last
    p = {x["id"]: x for x in g["short_straddle"]["presets"]}
    assert p["trail-10"]["count"] == 2 and p["trail-10"]["best"]["net"] == 30000 and p["trail-10"]["latest"]["net"] == 30000
    rs = {r["name"]: r for r in c.get("/studio/api/results?strategy=short_straddle").get_json()["runs"]}
    a, b, cmp_ = rs["20261010-100000_short_straddle"], rs["20261011-100000_short_straddle"], rs["20261012-100000_short_straddle"]
    assert a["period"] == "Jan–Sep 26" and a["data"] == "NIFTY 1-min" and a["fills"] == "normal fills" and a["where"] == "server"
    assert a["user_note"] == "first look" and b["fills"] == "conservative fills" and b["where"] == "mac" and b["period"] == "Oct 24 – Sep 26"
    assert cmp_["kind"] == "compare" and cmp_["preset_name"] == "Defaults"
    assert [r["name"] for r in c.get("/studio/api/results?strategy=short_straddle&kind=compare").get_json()["runs"]] == [cmp_["name"]]
    assert len(c.get("/studio/api/results?strategy=short_straddle&preset=trail-10").get_json()["runs"]) == 2
    assert c.get("/studio/api/results?strategy=short_straddle&sort=net").get_json()["runs"][0]["net"] == 30000
    # the older run as reference: stage gates use it instead of the latest
    assert c.post("/studio/api/results/ref", json={"run": a["name"]}).status_code == 200
    assert c.get("/studio/api/results?strategy=short_straddle").get_json()["refs"]["short_straddle"]["trail-10"] == a["name"]
    g = c.get("/studio/api/strategies/short_straddle").get_json()["gates"]
    assert g["next"] == "Paper" and "Trail 10" in g["checks"][0]["name"] and any(x["note"] == "₹12,000" for x in g["checks"])
    assert c.post("/studio/api/results/ref", json={"run": "nope"}).status_code == 404
    # archive hides, unarchive shows
    assert c.post("/studio/api/results/archive", json={"runs": [b["name"], "ghost"]}).get_json()["changed"] == 1
    assert b["name"] not in {r["name"] for r in c.get("/studio/api/results?strategy=short_straddle").get_json()["runs"]}
    assert b["name"] in {r["name"] for r in c.get("/studio/api/results?strategy=short_straddle&archived=1").get_json()["runs"]}
    c.post("/studio/api/results/archive", json={"runs": [b["name"]], "on": False})
    assert b["name"] in {r["name"] for r in c.get("/studio/api/results?strategy=short_straddle").get_json()["runs"]}
    lst = {s["name"]: s for s in c.get("/studio/api/strategies").get_json()["strategies"]}
    assert lst["short_straddle"]["best"]["net"] == 30000
    viewer = _studio(tmp_path, monkeypatch, who=VIEWER)
    assert viewer.get("/studio/api/results").status_code == 200
    assert viewer.post("/studio/api/results/archive", json={"runs": [a["name"]]}).status_code == 403


def test_studio_run_page_compare_and_sweep(tmp_path, monkeypatch):
    c = _studio(tmp_path, monkeypatch)
    store = tmp_path / "store"
    for name, net, stop, risk in (("20261010-100000_short_straddle", 12000, 40.0, {}),
                                  ("20261011-100000_short_straddle", 30000, 25.0, {"stops": "framework", "leg_stop": 20.0})):
        d = store / name
        d.mkdir(parents=True)
        (d / "run.json").write_text(json.dumps({"strategy": "short_straddle", "params": {"lots": 1, "stop_loss_pct": stop},
                                                "config": {"risk": risk}, "data_from": "2026-01-01", "data_to": "2026-03-31",
                                                "dataset": "NIFTY_minute", "metrics": {"Net P&L": net, "Max drawdown": -4000,
                                                                                           "Charges paid": 900}}))
        (d / "equity_daily.csv").write_text("date,equity,low,spot\n2026-01-02,300000,0,1\n2026-01-03,305000,0,1\n2026-01-06,302000,0,1\n")
        (d / "cycles.csv").write_text("group,entry_time,exit_time,legs,gross_pnl,costs,net_pnl,exit_reason\n"
                                      "a,2026-01-02 09:20,2026-01-02 15:15,2,0,0,5000,Square-off\nb,2026-02-03 09:20,2026-02-03 15:15,2,0,0,-3000,Stop\n")
    a, b = "20261010-100000_short_straddle", "20261011-100000_short_straddle"
    d = c.get(f"/studio/api/run/{a}").get_json()
    assert d["curve"] == [["2026-01-02", 0, 0], ["2026-01-03", 5000, 0], ["2026-01-06", 2000, -3000]]
    assert d["months"] == [{"month": "2026-01", "net": 5000}, {"month": "2026-02", "net": -3000}]
    assert d["worst"][0]["net"] == -3000 and d["best"][0]["net"] == 5000 and d["cycles"] == 2
    st = {x["key"]: x for x in d["settings"]}
    assert st["stop_loss_pct"]["changed"] is False and st["lots"]["value"] == 1
    assert any(f["label"] == "Charges paid" and f["kind"] == "cost" for f in d["figs"])
    assert c.get("/studio/api/run/..%2Fx").status_code == 404 and c.get("/studio/api/run/nope").status_code == 404
    cmp_ = c.get(f"/studio/api/compare-runs?runs={a},{b}").get_json()
    diff = {x["key"]: x["values"] for x in cmp_["settings"] if x["differs"]}
    assert diff["stop_loss_pct"] == [40.0, 25.0] and diff["risk.leg_stop"] == [None, 20.0] and "lots" not in diff
    assert cmp_["same_strategy"] and [f["values"] for f in cmp_["figs"] if f["label"] == "Net P&L"][0] == [12000, 30000]
    assert len(cmp_["curves"]) == 2
    assert c.get(f"/studio/api/compare-runs?runs={a}").status_code == 400
    # a Find best job: its picks with full settings to save as presets
    jobs = tmp_path / "runtime" / "backtest-jobs"
    jobs.mkdir(parents=True)
    (jobs / "20261016-100000-bbbb.json").write_text(json.dumps({
        "id": "20261016-100000-bbbb", "kind": "sweep", "strategy": "short_straddle", "status": "done", "params": {"lots": 2},
        "grid": {"stop_loss_pct": ["20", "30"]}, "dataset": "NIFTY_minute", "from": "2025-01-01", "to": "2026-09-30",
        "optimise": {"verdict": "ok", "picks": [{"params": {"stop_loss_pct": "30"}, "total_net": 31000, "periods_up": 3, "periods": 4}]}}))
    s = c.get("/studio/api/sweep/20261016-100000-bbbb").get_json()
    assert s["find_best"] and s["picks"][0]["params"] == {"lots": 2, "stop_loss_pct": "30"} and s["verdict"] == "ok"
    r = c.post("/studio/api/presets", json={"strategy": "short_straddle", "name": "Pick 1", "params": s["picks"][0]["params"]})
    assert r.get_json()["preset"]["params"] == {"lots": 2, "stop_loss_pct": 30.0}
    assert c.get("/studio/api/sweep/../../x").status_code == 404
    js = c.get("/studio/static/app.js").get_data(as_text=True)
    assert "comparePage" in js and "runPage" in js and "sweepPage" in js


def test_simulated_sessions_are_a_check_not_paper_trading(tmp_path, monkeypatch):
    c = _studio(tmp_path, monkeypatch)
    d = tmp_path / "runtime" / "sessions" / "short_straddle"
    d.mkdir(parents=True)
    (d / "desired.json").write_text(json.dumps({"strategy": "short_straddle", "feed": "sim", "mode": "paper"}))
    (d / "state.json").write_text(json.dumps({"strategy": "short_straddle", "feed": "sim", "time": "2027-12-03T10:00:00",
                                              "equity": 350000, "broker": {"capital": 300000, "trades": [{"exit_time": "2027-12-02T15:15:00", "net_pnl": 50000}]}}))
    (d / "heartbeat").write_text(str(time.time()))
    s = {x["name"]: x for x in c.get("/studio/api/strategies").get_json()["strategies"]}["short_straddle"]
    assert s["stage"] == "Draft" and "paper" not in s and s["sim"] == {"sessions": 1, "running": 1}
    one = c.get("/studio/api/strategies/short_straddle").get_json()
    assert one["sessions"][0]["sim"] is True and one["sessions"][0]["sim_time"].startswith("2027-12-03")
    a = c.get("/studio/api/strategies/short_straddle/analysis").get_json()
    assert a["days"] == [] and a["sessions"] == []


def test_simulated_session_stops_after_a_replayed_year(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from algolab import paper
    from algolab.config import load_config
    slot = paper.Slot.__new__(paper.Slot)
    slot.desired, slot.status = tmp_path / "desired.json", tmp_path / "status.txt"
    slot.desired.write_text("{}")
    logs = []
    sess = SimpleNamespace(saved={"time": "2027-10-20T15:25:00", "spot": 25000, "sim_from": "2026-10-19"}, slot=slot,
                           _log=logs.append)
    assert paper._sim_feed(load_config(), sess, None) is None
    assert not slot.desired.exists() and "a year of simulated prices" in slot.status.read_text() and logs
    sess2 = SimpleNamespace(saved={"time": "2027-10-10T15:25:00", "spot": 25000, "sim_from": "2026-10-19"}, slot=slot, _log=logs.append)
    feed = paper._sim_feed(load_config(), sess2, None)
    assert feed is not None and sess2.sim_from == "2026-10-19"


def test_older_simulated_session_counts_from_its_first_point(tmp_path):
    from types import SimpleNamespace
    from algolab import paper
    from algolab.config import load_config
    slot = paper.Slot.__new__(paper.Slot)
    slot.desired, slot.status, slot.equity = tmp_path / "d.json", tmp_path / "s.txt", tmp_path / "equity.csv"
    slot.desired.write_text("{}")
    slot.equity.write_text("time,equity,spot\n2026-10-15T09:20:00,300000,24000\n2027-12-03T10:00:00,350000,25000\n")
    sess = SimpleNamespace(saved={"time": "2027-12-03T10:00:00", "spot": 25000}, slot=slot, _log=lambda m: None)
    assert paper._sim_feed(load_config(), sess, None) is None and not slot.desired.exists()
