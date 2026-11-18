"""Orders & stops on the screens: the catalog rows, starting and resuming sessions, saved defaults, backtests and sweeps."""
import json
from datetime import date

import pytest

from test_defined_risk import _panel


def _catalog(tmp_path, monkeypatch):
    from algolab import paper
    monkeypatch.setattr(paper, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(paper, "CATALOG", tmp_path / "runtime" / "strategies.json")
    paper.write_catalog()
    return json.loads((tmp_path / "runtime" / "strategies.json").read_text())


def test_catalog_has_the_orders_and_stops_rows(tmp_path, monkeypatch):
    cat = {c["name"]: c for c in _catalog(tmp_path, monkeypatch)}
    ss = cat["short_straddle"]
    rows = {p["key"]: p for p in ss["params"] if p.get("group") == "risk"}
    assert rows["risk.stops"]["choices"] == ["own", "framework", "both"] and rows["risk.leg_stop"]["default"] == 0.0
    assert "risk.day_loss" in rows and "risk.order_type" in rows
    assert any("Stop-loss" in x for x in ss["own_stops"])


def test_start_resume_defaults_and_jobs(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    _catalog(tmp_path, monkeypatch)
    mod = _panel(tmp_path, monkeypatch)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    body = {"strategy": "short_straddle", "feed": "sim", "params": {"lots": 1, "risk.stops": "framework", "risk.leg_stop": "25",
                                                                    "risk.day_loss": "8000", "risk.reenter_after_stop": False}}
    r = c.post("/api/paper/start", json=body)
    assert r.status_code == 200, r.get_json()
    want = json.loads((tmp_path / "runtime" / "sessions" / r.get_json()["id"] / "desired.json").read_text())
    assert want["params"] == {"lots": 1} and want["risk"]["stops"] == "framework" and want["risk"]["leg_stop"] == 25.0
    assert c.post("/api/paper/start", json={**body, "params": {"risk.stops": "sometimes"}}).status_code == 400
    # resume: the page sends the saved settings back
    r2 = c.post("/api/paper/start", json={"id": r.get_json()["id"], "strategy": "short_straddle", "feed": "sim", "params": {"lots": 1},
                                          "risk": {"stops": "both", "cycle_loss": 5000}})
    want = json.loads((tmp_path / "runtime" / "sessions" / r2.get_json()["id"] / "desired.json").read_text())
    assert want["risk"] == {"stops": "both", "cycle_loss": 5000}
    # a strategy's saved defaults
    assert c.post("/api/risk-defaults", json={"strategy": "short_straddle", "risk": {"stops": "framework", "leg_stop": "30"}}).status_code == 200
    rows = {p["key"]: p for p in next(x for x in c.get("/api/paper").get_json()["catalog"] if x["name"] == "short_straddle")["params"]}
    assert rows["risk.stops"]["default"] == "framework" and rows["risk.leg_stop"]["default"] == 30.0
    assert c.post("/api/risk-defaults", json={"strategy": "short_straddle", "risk": {"leg_stop": "-1"}}).status_code == 400
    # a backtest job keeps the risk values even when they equal the (saved) defaults
    spot = tmp_path / "data" / "spot"
    spot.mkdir(parents=True)
    (spot / "NIFTY_minute.csv").write_text("datetime,open,high,low,close,vix\n2025-01-01 09:15:00,1,1,1,1,1\n2026-10-16 15:29:00,1,1,1,1,1\n")
    j = c.post("/api/backtest-runner", json={"strategy": "short_straddle", "dataset": "NIFTY_minute", "from": "2025-01-01", "to": "2025-03-01",
                                             "params": {"risk.stops": "framework", "risk.leg_stop": 30}}).get_json()["job"]
    assert j["params"]["risk.stops"] == "framework" and j["params"]["risk.leg_stop"] == 30
    page = c.get("/paper/new").get_data(as_text=True)
    assert "function riskBox" in page and "Save as this strategy" in page


def test_engine_runs_risk_settings_from_jobs_and_sweeps(tmp_path, monkeypatch):
    from algolab import backtest_queue as bq, runs
    from algolab.config import load_config
    from algolab.sweep import sweep
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    cfg = load_config()
    cfg["pricing"]["real_only"] = False
    cfg["pricing"]["smile"] = "off"
    job = {"strategy": "intraday_straddle", "dataset": "SAMPLE_NIFTY_5minute", "from": "2025-01-01", "to": "2025-01-31",
           "params": {"risk.stops": "framework", "risk.leg_stop": "10"}}
    out = bq.run_one(job, store=tmp_path / "store", cfg=cfg)
    trades = (tmp_path / "store" / out["run"] / "trades.csv").read_text()
    assert "Leg stop" in trades
    df, _ = sweep("intraday_straddle", {"risk.leg_stop": ["0", "10"]}, "SAMPLE_NIFTY_5minute", date(2025, 1, 1), date(2025, 1, 31),
                  base_params={"risk.stops": "framework"}, cfg=cfg, workers=1, out=tmp_path / "sw")
    nets = dict(zip(df["risk.leg_stop"].astype(str), df["all_net"]))
    assert nets["0"] != nets["10"]                                      # the stop changes the result
