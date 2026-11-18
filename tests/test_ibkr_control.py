"""IBKR start / stop from the dashboard: the request file, the root helper script, the status shown."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from test_defined_risk import _panel

ROOT = Path(__file__).resolve().parents[1]


def test_status_and_request(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    srv = {"services": [{"unit": "trader-app", "state": "active"}], "processes": []}
    assert mod._ibkr_status(srv) is None                                   # not installed: no IBKR box
    srv = {"services": [{"unit": "ibkr-sync", "state": "active"}],
           "processes": [{"name": "IB Gateway (Java)", "rss": 900_000_000, "count": 1}, {"name": "IBKR sync", "rss": 60_000_000, "count": 1}]}
    k = mod._ibkr_status(srv)
    assert k["gateway"] and k["sync"] and k["memory"] == 960_000_000 and not k["busy"]
    monkeypatch.setattr(mod, "_server", lambda: srv)
    app = flask.Flask("t")
    app.register_blueprint(mod.panel)
    c = app.test_client()
    assert c.post("/api/ibkr-control", json={"action": "reboot"}).status_code == 400
    r = c.post("/api/ibkr-control", json={"action": "stop"})
    assert r.status_code == 200 and (tmp_path / "runtime" / "ibkr.request").read_text() == "stop"
    assert r.get_json()["ibkr"]["busy"] is True
    page = mod.SUMMARY_PAGE
    assert "function ibkrBox" in page and "Tap again to" in page


def test_owner_only():
    import importlib.util
    spec = importlib.util.spec_from_file_location("up", ROOT / "deploy" / "portfolio_page" / "users_panel.py")
    up = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(up)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"users panel needs the dashboard: {e}")
    assert "/api/ibkr-control" in up.OWNER_EXACT


def _fake_bin(tmp_path, log):
    b = tmp_path / "bin"
    b.mkdir()
    for name in ("systemctl", "docker"):
        f = b / name
        if name == "systemctl":
            f.write_text(f'#!/bin/sh\necho "systemctl $*" >> {log}\n'
                         f'[ "$1" = show ] && echo "{tmp_path / "ibkr"}"\nexit 0\n')
        else:
            f.write_text(f'#!/bin/sh\necho "docker $* in $(pwd)" >> {log}\nexit 0\n')
        f.chmod(0o755)
    return b


@pytest.mark.parametrize("action,expect", [("stop", ["systemctl disable --now ibkr-sync", "docker compose down"]),
                                           ("start", ["docker compose up -d", "systemctl enable --now ibkr-sync"])])
def test_helper_script(tmp_path, action, expect):
    eng = tmp_path / "engine"
    (eng / "runtime").mkdir(parents=True)
    (eng / "run.py").write_text("")
    (tmp_path / "ibkr").mkdir()
    (tmp_path / "ibkr" / "docker-compose.yml").write_text("services: {}\n")
    log = tmp_path / "calls.log"
    b = _fake_bin(tmp_path, log)
    (eng / "runtime" / "ibkr.request").write_text(action)
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "ENGINE_DIR": str(eng)}
    subprocess.run(["bash", str(ROOT / "deploy" / "ibkr_control.sh")], env=env, check=True, timeout=30)
    calls = log.read_text()
    pos = [calls.index(x) for x in expect]
    assert pos == sorted(pos)                                              # in this order
    out = json.loads((eng / "runtime" / "ibkr-control.json").read_text())
    assert out["status"] == "done" and out["action"] == action
    assert not (eng / "runtime" / "ibkr.request").exists()


def test_helper_without_ibkr(tmp_path):
    eng = tmp_path / "engine"
    (eng / "runtime").mkdir(parents=True)
    (eng / "run.py").write_text("")
    (eng / "runtime" / "ibkr.request").write_text("start")
    b = tmp_path / "bin"
    b.mkdir()
    (b / "systemctl").write_text("#!/bin/sh\nexit 1\n")
    (b / "systemctl").chmod(0o755)
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "ENGINE_DIR": str(eng)}
    subprocess.run(["bash", str(ROOT / "deploy" / "ibkr_control.sh")], env=env, check=True, timeout=30)
    out = json.loads((eng / "runtime" / "ibkr-control.json").read_text())
    assert out["status"] == "failed" and "not installed" in out["message"]
