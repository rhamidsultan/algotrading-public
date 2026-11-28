"""Backtests run on the server: the dashboard queues them, the engine runs them and puts them on the Backtests page."""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab import backtest_queue as bq, runs  # noqa: E402
from algolab.config import load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setattr(bq, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(bq, "QUEUE", tmp_path / "runtime" / "backtest-queue")
    monkeypatch.setattr(bq, "JOBS", tmp_path / "runtime" / "backtest-jobs")
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    (tmp_path / "runtime").mkdir()
    cfg = load_config()
    cfg["pricing"]["smile"] = "off"
    cfg["pricing"]["real_only"] = False            # the sample data has no stored option prices
    return tmp_path, cfg


def _queue(tmp_path, jid, **job):
    q = tmp_path / "runtime" / "backtest-queue"
    q.mkdir(parents=True, exist_ok=True)
    (q / f"{jid}.json").write_text(json.dumps({"id": jid, **job}))


def test_runs_the_queue_and_publishes_to_the_backtests_page(engine):
    tmp_path, cfg = engine
    _queue(tmp_path, "20261018-120000-abcd", strategy="short_straddle", dataset="SAMPLE_NIFTY_5minute",
           params={"lots": 1}, user="hamid", **{"from": "2025-01-01", "to": "2025-03-31"})
    _queue(tmp_path, "20261018-120001-efgh", strategy="no_such", dataset="SAMPLE_NIFTY_5minute")
    _queue(tmp_path, "not-an-id", strategy="short_straddle", dataset="SAMPLE_NIFTY_5minute")
    store = tmp_path / "trader" / "data" / "backtests"
    assert bq.process(store, cfg=cfg) == 2
    assert not list((tmp_path / "runtime" / "backtest-queue").glob("*.json"))        # all taken off the queue
    ok = json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261018-120000-abcd.json").read_text())
    assert ok["status"] == "done" and ok["run"].endswith("_short_straddle") and "Net P&L" in ok["metrics"]
    assert (store / ok["run"] / "run.json").exists() and (store / ok["run"] / "equity_daily.csv").exists()
    meta = json.loads((store / ok["run"] / "run.json").read_text())
    assert meta["params"]["lots"] == 1 and "run on the server for hamid" in meta["note"]
    bad = json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261018-120001-efgh.json").read_text())
    assert bad["status"] == "failed" and "no_such" in bad["message"]


def test_one_runner_at_a_time(engine):
    import fcntl
    tmp_path, cfg = engine
    with open(tmp_path / "runtime" / "backtest-queue.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        _queue(tmp_path, "20261018-120000-abcd", strategy="short_straddle", dataset="SAMPLE_NIFTY_5minute")
        assert bq.process(tmp_path / "store", cfg=cfg) == 0
    assert list((tmp_path / "runtime" / "backtest-queue").glob("*.json"))             # left for the running one


def _panel(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    rt = tmp_path / "runtime"
    rt.mkdir(exist_ok=True)
    (rt / "strategies.json").write_text(json.dumps([{"name": "expiry_doubler", "title": "Expiry doubler", "params": [
        {"key": "lots", "label": "Lots", "default": 1, "type": "int", "min": 1, "choices": None},
        {"key": "reference", "label": "From", "default": "start", "type": "str", "min": None, "choices": ["start", "low"]}]}]))
    spot = tmp_path / "data" / "spot"
    spot.mkdir(parents=True)
    (spot / "NIFTY_minute.csv").write_text("datetime,open,high,low,close,vix\n2024-10-01 09:15:00,1,1,1,1,1\n"
                                               "2026-10-16 15:29:00,1,1,1,1,1\n")
    (spot / "SAMPLE_NIFTY_5minute.csv").write_text("datetime,close\n")
    spec = importlib.util.spec_from_file_location("algolab_panel_bt", ROOT / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    return mod, app.test_client()


def test_dashboard_queues_a_backtest(tmp_path, monkeypatch):
    mod, c = _panel(tmp_path, monkeypatch)
    d = c.get("/api/backtest-runner").get_json()
    assert d["datasets"] == [{"name": "NIFTY_minute", "from": "2024-10-01", "to": "2026-10-16"}]   # no SAMPLE
    assert d["strategies"][0]["name"] == "expiry_doubler" and d["jobs"] == [] and d["may_run"]
    body = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "from": "2025-10-01", "to": "2026-10-16",
            "params": {"lots": 2, "reference": "start"}, "note": "first look"}
    r = c.post("/api/backtest-runner", json=body)
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    req = json.loads((tmp_path / "runtime" / "backtest-queue" / f"{job['id']}.json").read_text())
    assert req["params"] == {"lots": 2} and req["dataset"] == "NIFTY_minute" and req["status"] == "queued"
    assert bq.ID_OK.match(job["id"])
    assert not list((tmp_path / "runtime" / "backtest-jobs").glob(".*"))               # no half-written files left
    assert c.get("/api/backtest-runner").get_json()["jobs"][0]["id"] == job["id"]
    for bad, why in (({"dataset": "SAMPLE_NIFTY_5minute"}, 400), ({"strategy": "x"}, 400), ({"from": "2026-13"}, 400),
                     ({"params": {"nope": 1}}, 400), ({"params": {"reference": "high"}}, 400),
                     ({"from": "2026-10-10", "to": "2026-10-01"}, 400)):
        assert c.post("/api/backtest-runner", json={**body, **bad}).status_code == why, bad
    c.post("/api/backtest-runner", json=body)
    c.post("/api/backtest-runner", json=body)
    assert c.post("/api/backtest-runner", json=body).status_code == 429                  # at most 3 waiting
    assert 'id="run"' in c.get("/backtests/run").get_data(as_text=True)


def test_page_is_in_the_paper_menu_and_needs_backtests_use():
    users = (ROOT / "deploy" / "portfolio_page" / "users_panel.py").read_text()
    assert '"/backtests/run", "/backtests/sweep", "/api/backtest-runner",' in users and '"/api/backtest-runner/finished", "/api/td-live-test",' in users and '{"/api/backtests/upload", "/api/backtest-runner", "/api/lab-agent/claim", "/api/lab-agent/status", "/api/td-live-test",' in users
    panel = (ROOT / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()
    assert '("Algo Lab", "/backtests/run", ("/backtests", "/backtests/run", "/backtests/sweep", "/backtests/compare"))' in panel and '("Results", "/backtests")' in panel


def test_update_installs_the_low_priority_runner():
    s = (ROOT / "deploy" / "update_engine.sh").read_text()
    assert "algolab-backtest.path" in s and "DirectoryNotEmpty=$ENGINE/runtime/backtest-queue" in s
    for want in ("Nice=19", "IOSchedulingClass=idle", "CPUQuota=", "MemoryMax=", "run.py backtest-queue"):
        assert want in s


def test_sweep_runs_every_combination_and_ranks_them(engine):
    tmp_path, cfg = engine
    _queue(tmp_path, "20261018-130000-swep", kind="sweep", strategy="intraday_straddle", dataset="SAMPLE_NIFTY_5minute",
           grid={"leg_stop_pct": ["20", "40"], "exit_time": ["15:00", "15:15"]}, params={"lots": 1},
           **{"from": "2025-01-01", "to": "2025-02-28"})
    assert bq.process(tmp_path / "store", cfg=cfg) == 1
    j = json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261018-130000-swep.json").read_text())
    assert j["status"] == "done", j.get("message") and j["total"] == 4 and j["progress"] == [4, 4]
    assert len(j["rows"]) == 4 and {"leg_stop_pct", "exit_time", "all_net", "all_ret_dd"} <= set(j["columns"])
    rd = [r["all_ret_dd"] for r in j["rows"] if r["all_ret_dd"] is not None]
    assert rd == sorted(rd, reverse=True)                                              # best first
    _queue(tmp_path, "20261018-130001-big1", kind="sweep", strategy="intraday_straddle", dataset="SAMPLE_NIFTY_5minute",
           grid={"leg_stop_pct": [str(x) for x in range(61)]})
    bq.process(tmp_path / "store", cfg=cfg)
    big = json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261018-130001-big1.json").read_text())
    assert big["status"] == "failed" and "at most 60" in big["message"]


def test_dashboard_queues_a_sweep(tmp_path, monkeypatch):
    mod, c = _panel(tmp_path, monkeypatch)
    base = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "from": "2025-10-01", "to": "2026-10-16"}
    r = c.post("/api/backtest-runner", json={**base, "kind": "sweep", "grid": {"lots": "1, 2", "reference": "start,low"}})
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    assert job["kind"] == "sweep" and job["grid"] == {"lots": ["1", "2"], "reference": ["start", "low"]}
    assert c.post("/api/backtest-runner", json={**base, "kind": "sweep", "grid": {"lots": "1"}}).status_code == 400   # one value: no sweep
    assert c.post("/api/backtest-runner", json={**base, "kind": "sweep", "grid": {"reference": "start,high"}}).status_code == 400
    assert c.post("/api/backtest-runner", json={**base, "kind": "sweep", "grid": {"lots": ",".join(map(str, range(1, 62)))}}).status_code == 400
    assert 'id="useSplit"' in c.get("/backtests/sweep").get_data(as_text=True)


class _Client:
    """requests-like wrapper around a Flask test client, for the Mac helper."""
    def __init__(self, c):
        self.c = c

    def post(self, url, json=None, timeout=None):
        resp = self.c.post(url.replace("http://dash", ""), json=json)

        class R:
            status_code = resp.status_code
            ok = resp.status_code < 400

            def json(self):
                return resp.get_json()

            def raise_for_status(self):
                if not self.ok:
                    raise RuntimeError(self.status_code)
        return R()


def test_mac_jobs_wait_for_the_mac_helper_and_report_back(engine, monkeypatch):
    tmp_path, cfg = engine
    (tmp_path / "dash").mkdir()
    mod, c = _panel(tmp_path / "dash", monkeypatch)
    from algolab import lab_agent
    from algolab.feeds import list_datasets
    spot = tmp_path / "dash" / "data" / "spot"
    import shutil
    shutil.copy(ROOT / "data" / "spot" / "SAMPLE_NIFTY_5minute.csv", spot / "SAMPLE_NIFTY_5minute.csv")
    assert c.get("/api/backtest-runner").get_json()["mac"]["online"] is False
    assert c.post("/api/lab-agent/claim", json={"name": "Mac", "beat_only": True}).get_json() == {"job": None, "stop": []}
    assert c.get("/api/backtest-runner").get_json()["mac"]["online"] is True       # a beat alone marks it online
    body = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "target": "mac", "from": "2025-01-01", "to": "2025-02-28"}
    job = c.post("/api/backtest-runner", json=body).get_json()["job"]
    assert job["target"] == "mac" and not list((tmp_path / "dash" / "runtime" / "backtest-queue").glob("*.json"))  # not the server's
    # the Mac runs it: on the Mac the data set exists (sample here), its result is published
    monkeypatch.setattr("algolab.backtest_queue.load_config", lambda: cfg)
    import algolab.feeds
    monkeypatch.setattr(algolab.feeds, "list_datasets", lambda: list_datasets() + ["NIFTY_minute"])
    orig = algolab.engine.backtest if hasattr(algolab, "engine") else None
    import algolab.engine as eng
    real = eng.backtest
    monkeypatch.setattr(eng, "backtest", lambda cfg_, s, ds, a, b: real(cfg_, s, "SAMPLE_NIFTY_5minute", a, b))
    published = []
    agent = lab_agent.Agent(session=_Client(c), cfg={"url": "http://dash"}, log=lambda m: None, sync=lambda log: "prices up to date")
    agent.publish = lambda d: (published.append(d.name), d.name)[1]
    assert agent.once() is True and agent.once() is False                               # one job, then nothing waiting
    d = c.get("/api/backtest-runner").get_json()
    done = d["jobs"][0]
    assert d["mac"]["online"] and done["status"] == "done", done.get("message")
    assert done["run"] == published[0] and "Net P&L" in done["metrics"] and done["runner"]
    assert c.post("/api/lab-agent/status", json={"id": job["id"].replace("-", "_")}).status_code == 404   # bad ids refused


def test_mac_helper_installer_and_page():
    s = (ROOT / "deploy" / "install-mac-agent.command").read_text()
    assert "run.py</string><string>lab-agent" in s and "KeepAlive" in s and "PYTHONUNBUFFERED" in s and "signed_in_session(cfg)" in s
    panel = (ROOT / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()
    assert 'id="target"' in panel and "target: TARGET" in panel and "waits for the Mac" in panel


def test_sweep_with_out_of_sample_check_and_robustness(engine):
    tmp_path, cfg = engine
    _queue(tmp_path, "20261018-140000-oosx", kind="sweep", strategy="intraday_straddle", dataset="SAMPLE_NIFTY_5minute",
           grid={"leg_stop_pct": ["20", "30", "40"]}, split="2025-03-01", **{"from": "2025-01-01", "to": "2025-03-31"})
    assert bq.process(tmp_path / "store", cfg=cfg) == 1
    j = json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261018-140000-oosx.json").read_text())
    assert j["status"] == "done", j.get("message")
    assert j["periods"] == ["in", "out"] and {"in_net", "out_net", "in_ret_dd", "out_ret_dd", "robust_ret_dd", "robust_net"} <= set(j["columns"])
    assert len(j["rows"]) == 3 and all(r["leg_stop_pct"] in ("20", "30", "40") for r in j["rows"])


def test_sweep_page_api_takes_ranges_split_and_mac_limit(tmp_path, monkeypatch):
    mod, c = _panel(tmp_path, monkeypatch)
    base = {"kind": "sweep", "strategy": "expiry_doubler", "dataset": "NIFTY_minute", "from": "2025-10-01", "to": "2026-10-16"}
    r = c.post("/api/backtest-runner", json={**base, "grid": {"lots": ["1", "2", "3"]}, "split": "2026-06-01"})
    assert r.status_code == 200 and r.get_json()["job"]["split"] == "2026-06-01"
    assert c.post("/api/backtest-runner", json={**base, "grid": {"lots": ["1", "2"]}, "split": "2027-01-01"}).status_code == 400
    many = {"lots": [str(i) for i in range(1, 101)]}
    assert c.post("/api/backtest-runner", json={**base, "grid": many}).status_code == 400             # 100 > 60 on the server
    assert c.post("/api/backtest-runner", json={**base, "grid": many, "target": "mac"}).status_code == 200   # the Mac takes 400
    jid = r.get_json()["job"]["id"]
    full = c.get(f"/api/backtest-runner/job/{jid}").get_json()["job"]
    assert full["id"] == jid and c.get("/api/backtest-runner/job/nope").status_code == 404
    page = c.get("/backtests/sweep").get_data(as_text=True)
    for el in ('id="useSplit"', 'id="scatter"', 'id="heat"', 'class="vary"', "robust_", "Download CSV"):
        assert el in page


def test_walk_forward_sweep(engine, tmp_path, monkeypatch):
    tmp, cfg = engine
    _queue(tmp, "20261018-150000-walk", kind="sweep", strategy="intraday_straddle", dataset="SAMPLE_NIFTY_5minute",
           grid={"leg_stop_pct": ["20", "40"]}, folds=3, **{"from": "2025-01-01", "to": "2025-03-31"})
    assert bq.process(tmp / "store", cfg=cfg) == 1
    j = json.loads((tmp / "runtime" / "backtest-jobs" / "20261018-150000-walk.json").read_text())
    assert j["status"] == "done", j.get("message")
    assert j["periods"] == ["f1", "f2", "f3"] and {"f1_net", "f3_net"} <= set(j["columns"])
    assert len(j["walk"]["steps"]) == 2 and j["fold_dates"]["f1"][0] == "2025-01-01"
    (tmp_path / "panel").mkdir()
    mod, c = _panel(tmp_path / "panel", monkeypatch)
    base = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "from": "2025-10-01", "to": "2026-10-16",
            "kind": "sweep", "grid": {"lots": "1, 2"}}
    r = c.post("/api/backtest-runner", json={**base, "folds": 4, "split": "2026-06-01"})
    assert r.status_code == 200 and r.get_json()["job"]["folds"] == 4 and r.get_json()["job"]["split"] is None
    assert c.post("/api/backtest-runner", json={**base, "folds": 12}).status_code == 400


def test_find_best_settings(engine):
    tmp, cfg = engine
    _queue(tmp, "20261018-160000-best", kind="sweep", optimise=True, strategy="intraday_straddle",
           dataset="SAMPLE_NIFTY_5minute", grid={"leg_stop_pct": ["20", "30", "40", "50"], "exit_time": ["15:00", "15:15"]},
           folds=3, lock_days=30, min_cycles=5, **{"from": "2025-01-01", "to": "2025-06-30"})
    assert bq.process(tmp / "store", cfg=cfg) == 1
    j = json.loads((tmp / "runtime" / "backtest-jobs" / "20261018-160000-best.json").read_text())
    assert j["status"] == "done", j.get("message")
    o = j["optimise"]
    assert o["tested"] == 8 and o["lock"] == ["2025-06-01", "2025-06-30"] and list(o["fold_dates"]) == ["f1", "f2", "f3"]
    assert o["fold_dates"]["f3"][1] == "2025-05-31"                  # the locked-away month is never used to choose
    assert len(o["picks"]) <= 3 and o["verdict"]
    for p in o["picks"]:
        assert p["conservative_net"] > 0 and p["reasons"] and set(p["params"]) == {"leg_stop_pct", "exit_time"}
    assert {"score", "why_out", "f1_net"} <= set(j["columns"])


def test_dashboard_queues_find_best_settings(tmp_path, monkeypatch):
    mod, c = _panel(tmp_path, monkeypatch)
    base = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "from": "2025-10-01", "to": "2026-10-16", "kind": "sweep"}
    big = {"lots": ",".join(map(str, range(1, 101))), "reference": "start,low"}           # 200: over the sweep limit
    assert c.post("/api/backtest-runner", json={**base, "grid": big}).status_code == 400
    r = c.post("/api/backtest-runner", json={**base, "grid": big, "optimise": True, "max_dd": 40000, "split": "2026-06-01"})
    assert r.status_code == 200, r.get_json()
    job = r.get_json()["job"]
    assert job["optimise"] is True and job["folds"] == 4 and job["lock_days"] == 75 and job["max_dd"] == 40000
    assert job["split"] is None
    assert c.post("/api/backtest-runner", json={**base, "grid": big, "optimise": True, "lock_days": 5}).status_code == 400
    page = c.get("/backtests/sweep").get_data(as_text=True)
    assert 'id="bestBox"' in page and "Start paper session" in page


def test_a_job_the_mac_lost_is_marked_and_sweep_extras_are_kept(tmp_path, monkeypatch):
    mod, c = _panel(tmp_path, monkeypatch)
    body = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "target": "mac", "from": "2025-10-01", "to": "2026-10-16",
            "kind": "sweep", "grid": {"lots": "1, 2"}}
    first = c.post("/api/backtest-runner", json=body).get_json()["job"]
    second = c.post("/api/backtest-runner", json=body).get_json()["job"]
    got = c.post("/api/lab-agent/claim", json={"name": "Air"}).get_json()["job"]
    if got["id"] != first["id"]:                    # queued in the same second: either may come first
        first, second = second, first
    c.post("/api/lab-agent/claim", json={"name": "Air", "beat_only": True})              # busy: nothing changes
    jobs = {j["id"]: j for j in c.get("/api/backtest-runner").get_json()["jobs"]}
    assert jobs[first["id"]]["status"] == "running"
    # the helper restarted (idle again) without finishing: the first job is marked, the next one handed out
    assert c.post("/api/lab-agent/claim", json={"name": "Air"}).get_json()["job"]["id"] == second["id"]
    jobs = {j["id"]: j for j in c.get("/api/backtest-runner").get_json()["jobs"]}
    assert jobs[first["id"]]["status"] == "failed" and "Send it again" in jobs[first["id"]]["message"]
    assert jobs[second["id"]]["status"] == "running"
    c.post("/api/lab-agent/status", json={"id": second["id"], "status": "done", "rows": [], "periods": ["f1", "f2"],
                                          "walk": {"net": 5}, "optimise": {"picks": []}, "luck": "x", "evil": 1})
    j = c.get(f"/api/backtest-runner/job/{second['id']}").get_json()["job"]
    assert j["periods"] == ["f1", "f2"] and j["walk"] == {"net": 5} and j["optimise"] == {"picks": []} and "evil" not in j


def _job(tmp_path, jid, **job):
    d = tmp_path / "runtime" / "backtest-jobs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{jid}.json").write_text(json.dumps({"id": jid, **job}))
    return d / f"{jid}.json"


def test_a_killed_runner_leaves_no_job_stuck_at_running(engine):
    tmp_path, cfg = engine
    srv = _job(tmp_path, "20261019-030000-abcd", kind="sweep", status="running", progress=[50, 66], target="server")
    old = _job(tmp_path, "20261019-030001-efgh", status="running")                 # from before runners said where
    mac = _job(tmp_path, "20261019-030002-ijkl", status="running", target="mac", runner="Mac")
    done = _job(tmp_path, "20261019-030003-mnop", status="done", target="server")
    assert bq.mark_stopped("oom-kill") == 2
    s = json.loads(srv.read_text())
    assert s["status"] == "failed" and "800 MB" in s["message"] and s["finished"] and s["progress"] == [50, 66]
    assert json.loads(old.read_text())["status"] == "failed"
    assert json.loads(mac.read_text())["status"] == "running"                       # the Mac helper's own
    assert json.loads(done.read_text())["status"] == "done"
    _job(tmp_path, "20261019-030004-qrst", status="running")
    assert bq.mark_stopped("timeout") == 1
    assert "3 hours" in json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261019-030004-qrst.json").read_text())["message"]
    _job(tmp_path, "20261019-030005-uvwx", status="running")
    assert bq.mark_stopped(None) == 1 and bq.mark_stopped("success") == 0


def test_stopped_check_leaves_a_working_runner_alone(engine):
    import fcntl
    tmp_path, cfg = engine
    job = _job(tmp_path, "20261019-030000-abcd", status="running", target="server")
    with open(tmp_path / "runtime" / "backtest-queue.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)                                               # a runner is at work
        assert bq.mark_stopped("oom-kill") == 0
    assert json.loads(job.read_text())["status"] == "running"


def test_next_runner_closes_what_a_killed_one_left(engine):
    tmp_path, cfg = engine
    job = _job(tmp_path, "20261019-030000-abcd", status="running", target="server")
    _queue(tmp_path, "20261019-040000-efgh", strategy="short_straddle", dataset="SAMPLE_NIFTY_5minute",
           params={"lots": 1}, **{"from": "2025-01-01", "to": "2025-01-31"})
    assert bq.process(tmp_path / "store", cfg=cfg) == 1
    assert json.loads(job.read_text())["status"] == "failed"
    new = json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261019-040000-efgh.json").read_text())
    assert new["status"] == "done"


def test_service_reports_why_it_stopped():
    s = (ROOT / "deploy" / "update_engine.sh").read_text()
    unit = s.split("algolab-backtest.service <<UNIT", 1)[1].split("UNIT", 1)[0]
    assert "ExecStopPost=$PY run.py backtest-queue --stopped" in unit and "SERVICE_RESULT" not in unit
    assert "run.py backtest-queue --stopped" in s.split("algolab-backtest.path ||", 1)[1]


def test_results_list_finished_jobs_and_delete(tmp_path, monkeypatch):
    mod, c = _panel(tmp_path, monkeypatch)
    jobs = tmp_path / "runtime" / "backtest-jobs"
    jobs.mkdir(parents=True, exist_ok=True)
    base = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "from": "2025-01-01", "to": "2025-06-30"}
    rows = [{"lots": "1", "f1_net": 500.0}]
    for jid, extra in {"20261019-100000-aaaa": {"kind": "sweep", "status": "done", "rows": rows, "periods": ["f1"], "folds": 3},
                       "20261019-100001-bbbb": {"kind": "sweep", "status": "done", "optimise": {"picks": [{"total_net": 900}]}},
                       "20261019-100002-cccc": {"status": "failed", "message": "No data"},
                       "20261019-100003-dddd": {"status": "done", "run": "x"},                      # a saved run: in Runs
                       "20261019-100004-eeee": {"kind": "sweep", "status": "running"}}.items():
        (jobs / f"{jid}.json").write_text(json.dumps({"id": jid, **base, **extra}))
    got = {j["id"]: j for j in c.get("/api/backtest-runner/finished").get_json()["jobs"]}
    assert set(got) == {"20261019-100000-aaaa", "20261019-100001-bbbb", "20261019-100002-cccc"}
    assert got["20261019-100000-aaaa"]["best"] == 500.0 and got["20261019-100001-bbbb"]["best"] == 900
    assert got["20261019-100001-bbbb"]["picks"] == 1 and "rows" not in got["20261019-100000-aaaa"]
    assert c.post("/api/backtest-runner/job/20261019-100004-eeee/delete").status_code == 409     # still running
    assert c.post("/api/backtest-runner/job/20261019-100000-aaaa/delete").status_code == 200
    assert not (jobs / "20261019-100000-aaaa.json").exists()
    assert c.post("/api/backtest-runner/job/../x/delete").status_code == 404
    run = c.get("/backtests/run").get_data(as_text=True)
    assert "In progress" in run and "Finished backtests are in" in run
    assert "Finished sweeps are in" in c.get("/backtests/sweep").get_data(as_text=True)


def test_run_page_offers_1_second_bars_first_with_their_days(tmp_path, monkeypatch):
    mod, c = _panel(tmp_path, monkeypatch)
    for day in ("2026-11-24", "2026-11-26", "2026-11-27"):
        (tmp_path / "data" / "sec1" / day).mkdir(parents=True)
        (tmp_path / "data" / "sec1" / day / "NIFTY.csv").write_text("time\n")
    (tmp_path / "data" / "sec1" / "2026-11-25").mkdir()            # no index file: not usable, not offered
    d = c.get("/api/backtest-runner").get_json()
    assert d["datasets"][0] == {"name": "NIFTY_1second", "from": "2026-11-24", "to": "2026-11-27", "sec1": True,
                                "days": ["2026-11-24", "2026-11-26", "2026-11-27"]}
    assert d["datasets"][1]["name"] == "NIFTY_minute"
    body = {"strategy": "expiry_doubler", "dataset": "NIFTY_1second", "from": "2026-11-24", "to": "2026-11-27"}
    r = c.post("/api/backtest-runner", json=body)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["job"]["dataset"] == "NIFTY_1second"
    r = c.post("/api/backtest-runner", json={**body, "kind": "sweep", "grid": {"lots": "1,2"}})   # sweeps too
    assert r.status_code == 200 and r.get_json()["job"]["dataset"] == "NIFTY_1second", r.get_json()
    assert "D.datasets.find(d => d.sec1) ||" in c.get("/backtests/sweep").get_data(as_text=True)    # sweep page: 1-second first
    page = c.get("/backtests/run").get_data(as_text=True)
    assert "D.datasets.find(d => d.sec1) ||" in page and "NIFTY 1-second" in page    # chosen first, labelled


def test_queue_runs_1_second_backtests(engine, monkeypatch):
    tmp_path, cfg = engine
    import algolab.engine as eng
    seen = {}

    def fake(cfg, strat, dataset, start, end, *a, **k):
        seen["dataset"] = dataset
        raise ValueError("stepped on 1-second bars")
    monkeypatch.setattr(eng, "backtest", fake)
    _queue(tmp_path, "20261128-010000-sec1", strategy="short_straddle", dataset="NIFTY_1second",
           **{"from": "2026-11-24", "to": "2026-11-27"})
    bq.process(tmp_path / "store", cfg=cfg)
    j = json.loads((tmp_path / "runtime" / "backtest-jobs" / "20261128-010000-sec1.json").read_text())
    assert seen["dataset"] == "NIFTY_1second" and "stepped on 1-second bars" in j["message"]   # not "No data set"


def test_sweeps_and_walk_forward_use_the_1_second_days(engine, monkeypatch, tmp_path):
    from datetime import date
    from algolab import sec1
    import algolab.sweep as sw
    root = tmp_path / "sec1"
    for day in ("2026-11-02", "2026-11-09", "2026-11-16", "2026-11-23", "2026-11-27"):
        (root / day).mkdir(parents=True)
        (root / day / "NIFTY.csv").write_text("time\n")
    monkeypatch.setattr(sec1, "ROOT", root)
    assert bq.known_dataset("NIFTY_1second") and not bq.known_dataset("NIFTY_2second")
    assert bq.data_span("NIFTY_1second", date(2026, 11, 5), None) == (date(2026, 11, 9), date(2026, 11, 27))
    with pytest.raises(ValueError, match="No 1-second bars"):
        bq.data_span("NIFTY_1second", date(2026, 12, 1), None)
    seen = {}

    def fake_sweep(strategy, grid, dataset, start, end, split=None, **k):
        seen.update(dataset=dataset, periods=k.get("periods"))
        raise ValueError("swept")
    monkeypatch.setattr(sw, "sweep", fake_sweep)
    with pytest.raises(ValueError, match="swept"):
        bq.run_sweep({"strategy": "short_straddle", "dataset": "NIFTY_1second", "grid": {"lots": ["1", "2"]}, "folds": 3},
                     cfg=engine[1])
    assert seen["dataset"] == "NIFTY_1second"
    assert seen["periods"]["f1"][0] == date(2026, 11, 2) and seen["periods"]["f3"][1] == date(2026, 11, 27)


def test_running_jobs_show_time_running_time_left_and_what_waits_behind(tmp_path, monkeypatch):
    """Tell slow from stuck: elapsed, last report, time left from this step's pace; queued jobs say what they wait for."""
    from datetime import datetime
    mod, c = _panel(tmp_path, monkeypatch)
    now = datetime(2026, 11, 28, 13, 30, 0)
    jobs = [{"id": "a", "kind": "sweep", "status": "running", "target": "mac", "strategy": "intraday_strangle",
             "started": "2026-11-28T13:00:00", "updated": "2026-11-28T13:28:00", "step": "combo", "progress": [11, 41],
             "p0": {"step": "combo", "at": "2026-11-28T13:08:00", "n": 1}},
            {"id": "b", "status": "queued", "target": "mac", "strategy": "iron_fly"},
            {"id": "c", "kind": "compare_period", "status": "running", "target": "server", "started": "2026-11-28T13:29:00",
             "updated": "2026-11-28T13:29:30", "step": "session", "progress": [7, 19]},
            {"id": "d", "status": "queued", "target": "server", "strategy": "iron_fly"}]
    mod._job_clock(jobs, now=now)
    a = jobs[0]
    assert (a["elapsed_s"], a["since_update_s"]) == (1800, 120)
    assert a["eta_s"] == 20 * 60 / 10 * 30                               # 10 done in 20 min since 13:08, 30 left
    assert jobs[1]["behind"] == "a sweep (intraday_strangle), combination 11 of 41"
    assert jobs[3]["behind"] == "Compare with paper, session 7 of 19"
    # the page's line, run by node on the same job
    import shutil
    import subprocess
    if shutil.which("node"):
        js = mod.JOB_PROGRESS_JS + "\nconst esc = s => String(s);\n" + \
            f"console.log(jobProgress({json.dumps(a)})); console.log(jobProgress({json.dumps({**a, 'since_update_s': 1200})}));" \
            f"console.log(jobProgress({json.dumps(jobs[1])}));"
        out = subprocess.run(["node", "-e", js], capture_output=True, text=True).stdout.splitlines()
        assert out[0].startswith(" · combination 11 of 41 · running 30 min · about 1 h left · last update 2 min ago")
        assert "no update for 20 min: may be stuck" in out[1]
        assert out[2].startswith(" · waits behind a sweep (intraday_strangle), combination 11 of 41 <button")   # + Stop


def test_job_reports_are_time_stamped(tmp_path, monkeypatch):
    monkeypatch.setattr(bq, "JOBS", tmp_path)
    job = {"id": "20261128-130000-abcd"}
    bq._status(job, status="running", step="day", progress=[1, 22])
    bq._status(job, progress=[2, 22])
    assert job["updated"] and job["p0"]["step"] == "day" and job["p0"]["n"] == 1     # pace from the first report
    bq._status(job, step="combo", progress=[1, 5])
    assert job["p0"]["step"] == "combo"


def test_stop_a_server_backtest_waiting_or_running(engine, monkeypatch):
    """Stop: a waiting job is dropped; a running one ends as "stopped" within a trading day of prices."""
    tmp_path, cfg = engine
    monkeypatch.setattr(bq, "STOPS", tmp_path / "runtime" / "backtest-stop")
    from algolab import stopping
    jobs = tmp_path / "runtime" / "backtest-jobs"
    # waiting: its stop file is there when the runner picks it up
    _queue(tmp_path, "20261128-120000-wait", strategy="short_straddle", dataset="SAMPLE_NIFTY_5minute")
    (tmp_path / "runtime" / "backtest-stop").mkdir(parents=True)
    (tmp_path / "runtime" / "backtest-stop" / "20261128-120000-wait").write_text("x")
    # running: Stop pressed on the third trading day (the engine looks once a day)
    real_check, days = stopping.check, []

    def check():
        days.append(1)
        if len(days) == 3:
            (bq.STOPS / "20261128-120001-runs").write_text("x")
        real_check()
    monkeypatch.setattr(stopping, "check", check)
    _queue(tmp_path, "20261128-120001-runs", strategy="short_straddle", dataset="SAMPLE_NIFTY_5minute",
           **{"from": "2025-01-01", "to": "2025-03-31"})
    assert bq.process(tmp_path / "store", cfg=cfg) == 1                 # the stopped-while-waiting one is not run
    w = json.loads((jobs / "20261128-120000-wait.json").read_text())
    assert w["status"] == "stopped" and "before it started" in w["message"]
    r = json.loads((jobs / "20261128-120001-runs.json").read_text())
    assert r["status"] == "stopped" and r["message"].startswith("Stopped on the page"), r["message"]
    assert len(days) == 3 and not list((tmp_path / "runtime" / "backtest-stop").iterdir())   # ended that day; cleaned up
    assert not stopping.asked() and stopping.ENV not in __import__("os").environ


def test_stop_buttons_and_the_mac_helper(engine, monkeypatch):
    tmp_path, cfg = engine
    (tmp_path / "dash").mkdir()
    mod, c = _panel(tmp_path / "dash", monkeypatch)
    rt = tmp_path / "dash" / "runtime"
    body = {"strategy": "expiry_doubler", "dataset": "NIFTY_minute", "from": "2025-01-01", "to": "2025-02-28"}
    # server, waiting: off the queue at once
    j = c.post("/api/backtest-runner", json=body).get_json()["job"]
    r = c.post(f"/api/backtest-runner/job/{j['id']}/stop")
    assert r.status_code == 200 and r.get_json()["job"]["status"] == "stopped"
    assert not list((rt / "backtest-queue").glob("*.json"))
    assert c.post(f"/api/backtest-runner/job/{j['id']}/stop").status_code == 409            # already ended
    # server, running: a stop file, shown as stopping
    j2 = c.post("/api/backtest-runner", json=body).get_json()["job"]
    (rt / "backtest-queue" / f"{j2['id']}.json").unlink()
    f = rt / "backtest-jobs" / f"{j2['id']}.json"
    f.write_text(json.dumps({**json.loads(f.read_text()), "status": "running", "started": "2026-11-28T12:00:00"}))
    assert c.post(f"/api/backtest-runner/job/{j2['id']}/stop").get_json()["job"]["stopping"] is True
    assert (rt / "backtest-stop" / j2["id"]).exists()
    assert next(x for x in c.get("/api/backtest-runner").get_json()["jobs"] if x["id"] == j2["id"])["stopping"] is True
    # Mac, running and online: told through the beat and the status reply; the helper ends it as stopped
    c.post("/api/lab-agent/claim", json={"name": "Mac", "beat_only": True})
    j3 = c.post("/api/backtest-runner", json={**body, "target": "mac"}).get_json()["job"]
    claimed = c.post("/api/lab-agent/claim", json={"name": "Mac"}).get_json()["job"]
    assert claimed["id"] == j3["id"]
    c.post(f"/api/backtest-runner/job/{j3['id']}/stop")
    assert c.post("/api/lab-agent/claim", json={"name": "Mac", "beat_only": True, "job": j3["id"]}).get_json()["stop"] == [j3["id"]]
    assert c.post("/api/lab-agent/status", json={"id": j3["id"], "status": "running", "progress": [3, 22], "step": "day"}).get_json()["stop"] is True
    from algolab import lab_agent, stopping
    agent = lab_agent.Agent(session=_Client(c), cfg={"url": "http://dash"}, log=lambda m: None, sync=lambda log: "")
    agent._stop_file = tmp_path / "stopflag"
    stopping.watch(agent._stop_file)
    try:
        agent.status(claimed, status="running", progress=[4, 22], step="day")      # the reply says stop: flag made
        assert stopping.asked()
        with pytest.raises(stopping.Stopped):
            stopping.check()
    finally:
        stopping.watch(None)
    agent.status(claimed, status="stopped", message="Stopped on the page after 4 of 22")
    done = json.loads((rt / "backtest-jobs" / f"{j3['id']}.json").read_text())
    assert done["status"] == "stopped" and done["finished"] and not (rt / "backtest-stop" / j3["id"]).exists()
    # Mac asleep: stopped here at once; when it wakes and reports, it is told to drop it
    j4 = c.post("/api/backtest-runner", json={**body, "target": "mac"}).get_json()["job"]
    c.post("/api/lab-agent/claim", json={"name": "Mac"})
    monkeypatch.setattr(mod, "_mac_status", lambda: {"online": False})
    assert c.post(f"/api/backtest-runner/job/{j4['id']}/stop").get_json()["job"]["status"] == "stopped"
    assert c.post("/api/lab-agent/status", json={"id": j4["id"], "status": "running", "progress": [9, 22]}).get_json()["stop"] is True
    assert json.loads((rt / "backtest-jobs" / f"{j4['id']}.json").read_text())["status"] == "stopped"   # not reopened
    page = c.get("/backtests/run").get_data(as_text=True)
    assert "data-stop=" in page and "Tap again to stop" in page and "tdCard" not in page
    assert "data-stop=" in c.get("/backtests/sweep").get_data(as_text=True)
