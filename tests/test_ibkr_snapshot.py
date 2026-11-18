"""Daily IBKR snapshot: the importer and the server's pull from the ibkr-snapshot branch."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server/portfolio/server/ibkr"))
import snapshot_import as si  # noqa: E402


def _snap(ts="2026-10-20T20:30:00+00:00", net=455.17):
    return {"timestamp_utc": ts, "usd_inr": 88.0,
            "summary_usd": {"net_liquidation": net, "cash": 16.78, "positions_value": 438.39,
                            "unrealized_pnl": 139.01, "realized_pnl": 0, "daily_pnl": 1.84},
            "positions": [{"symbol": "BAC", "con_id": 10098, "currency": "USD", "qty": 1, "avg_cost": 33.37,
                           "price": 50.54, "market_value": 50.54, "unrealized_pnl": 17.17, "daily_pnl": 0.15},
                          {"symbol": "TSLA", "con_id": 76792991, "currency": "USD", "qty": 0.3, "avg_cost": 190.87,
                           "price": 375.26, "market_value": 112.58, "unrealized_pnl": 55.32, "daily_pnl": 2.16}]}


def test_import_writes_page_file_and_history(tmp_path):
    msg = si.import_snapshot(_snap(), tmp_path)
    assert msg.startswith("imported")
    page = json.loads((tmp_path / "ibkr_snapshot.json").read_text())
    assert page["positions"][0]["symbol"] == "TSLA"            # biggest first, as the page expects
    assert page["summary_inr"]["net_liquidation"] == round(455.17 * 88, 2)
    assert page["timestamp_ist"] == "21 Oct 2026 02:00 IST"
    con = sqlite3.connect(tmp_path / "ibkr.db")
    assert con.execute("SELECT count(*) FROM account_history").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM positions_history").fetchone()[0] == 2


def test_older_or_same_snapshot_never_replaces_newer(tmp_path):
    si.import_snapshot(_snap(ts="2026-10-21T14:00:00+00:00", net=500), tmp_path)   # e.g. the live sync
    assert si.import_snapshot(_snap(), tmp_path).startswith("skipped")
    assert si.import_snapshot(_snap(ts="2026-10-21T14:00:00+00:00"), tmp_path).startswith("skipped")
    assert json.loads((tmp_path / "ibkr_snapshot.json").read_text())["summary_usd"]["net_liquidation"] == 500


def test_missing_rate_taken_from_saved_rate(tmp_path):
    (tmp_path / "usd_inr.json").write_text(json.dumps({"rate": 90.0}))
    s = _snap(); s["usd_inr"] = None
    si.import_snapshot(s, tmp_path)
    assert json.loads((tmp_path / "ibkr_snapshot.json").read_text())["usd_inr"] == 90.0


@pytest.mark.parametrize("bad", [{}, {"timestamp_utc": "x"}, {**_snap(), "positions": None},
                                 {**_snap(), "summary_usd": {}}, {**_snap(), "positions": [{"qty": 1}]}])
def test_bad_snapshot_refused(tmp_path, bad):
    with pytest.raises(ValueError):
        si.import_snapshot(bad, tmp_path)
    assert not (tmp_path / "ibkr_snapshot.json").exists()


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_pull_script_fetches_branch_and_imports(tmp_path):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t", HOME=str(tmp_path))
    git = lambda cwd, *a: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, env=env)  # noqa: E731
    work, bare, data = tmp_path / "work", tmp_path / "gh.git", tmp_path / "data"
    work.mkdir()
    git(work, "init", "-q", "-b", "ibkr-snapshot")
    (work / "ibkr_snapshot.json").write_text(json.dumps(_snap()))
    git(work, "add", "."); git(work, "commit", "-qm", "snapshot")
    git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    run_env = dict(env, SELF_UPDATE_HOME=str(tmp_path / "home"), SNAPSHOT_REPO=f"file://{bare}",
                   SNAPSHOT_IMPORTER=str(ROOT / "server/portfolio/server/ibkr/snapshot_import.py"),
                   IBKR_DATA_DIR=str(data))
    (tmp_path / "home").mkdir()
    out = subprocess.run(["bash", str(ROOT / "deploy/ibkr_snapshot_pull.sh")], env=run_env,
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "imported" in out.stdout
    assert (data / "ibkr_snapshot.json").exists()
    out = subprocess.run(["bash", str(ROOT / "deploy/ibkr_snapshot_pull.sh")], env=run_env,
                         capture_output=True, text=True)
    assert "skipped" in out.stdout                                   # hourly reruns change nothing


def test_update_installs_the_timer():
    s = (ROOT / "deploy/update_engine.sh").read_text()
    assert "algolab-ibkr-snapshot.timer" in s and "ibkr_snapshot_pull.sh" in s and "snapshot_import.py" in s
    # pulled 10 minutes after each scheduled snapshot (09:35 and 16:05 New York time), with the hourly fallback kept
    assert "OnCalendar=Mon..Fri 09:45 America/New_York" in s and "OnCalendar=Mon..Fri 16:15 America/New_York" in s
    assert "OnUnitActiveSec=1h" in s
