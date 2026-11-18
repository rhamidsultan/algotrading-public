"""deploy/send-data-to-server.command, run against a stand-in server folder (no network)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not (shutil.which("rsync") and shutil.which("tar")), reason="needs rsync and tar")


def _setup(tmp_path):
    mac = tmp_path / "mac"
    (mac / "deploy").mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "send-data-to-server.command", mac / "deploy" / "send.command")
    shutil.copy(ROOT / "deploy" / "server_login.sh", mac / "deploy")
    d = mac / "data"
    for u, k in (("NIFTY", 25000), ("SENSEX", 81000)):
        e = d / "options" / u / "2026-10-20"
        e.mkdir(parents=True)
        (e / f"{k}CE.csv").write_text("time,last,bid,ask\n2026-10-13 09:20:00,101,,\n")
    (d / "spot").mkdir()
    (d / "spot" / "TD_NIFTY_1minute.csv").write_text("datetime,open,high,low,close,vix\n2026-10-13 09:15:00,1,1,1,25000,12\n")
    (d / "truedata" / "bhavcopy").mkdir(parents=True)
    (d / "truedata" / "bhavcopy" / "FO_2026-10-13.csv.gz").write_bytes(b"\x1f\x8b raw")
    (d / "truedata" / "progress.db").write_bytes(b"sqlite")
    server = tmp_path / "server"
    (server / "data" / "options" / "NIFTY" / "2026-10-20").mkdir(parents=True)
    (server / "run.py").write_text("")
    # a price file the server recorded itself: must not be replaced
    (server / "data" / "options" / "NIFTY" / "2026-10-20" / "25000CE.csv").write_text("server's own recording\n")
    fake_ssh = tmp_path / "fake_ssh"
    fake_ssh.write_text('#!/bin/bash\nshift\nexec bash -c "$1"\n')
    fake_ssh.chmod(0o755)
    env = dict(os.environ, ALGO_SSH=str(fake_ssh), ALGO_HOST="server", ALGO_REMOTE=str(server), ALGO_IGNORE_HOURS="1")
    return mac, server, env


def _run(mac, env, choice):
    return subprocess.run(["bash", str(mac / "deploy" / "send.command")], env=dict(env, ALGO_CHOICE=choice),
                          capture_output=True, text=True, timeout=120)


def test_backtest_data_only_and_never_overwrites(tmp_path):
    mac, server, env = _setup(tmp_path)
    r = _run(mac, env, "1")
    assert r.returncode == 0, r.stdout + r.stderr
    sd = server / "data"
    assert (sd / "options" / "NIFTY" / "2026-10-20" / "25000CE.csv").read_text() == "server's own recording\n"
    assert (sd / "options" / "SENSEX" / "2026-10-20" / "81000CE.csv").read_text().endswith("101,,\n")
    assert (sd / "spot" / "TD_NIFTY_1minute.csv").exists()
    assert not (sd / "truedata").exists()                      # raw backup only with choice 2
    assert not (server / "incoming").exists()                  # holding folder cleaned up
    assert "3 of 3 parts copied" in r.stdout

    r = _run(mac, env, "2")                                     # again, with everything: safe to repeat
    assert r.returncode == 0, r.stdout + r.stderr
    assert (sd / "truedata" / "bhavcopy" / "FO_2026-10-13.csv.gz").read_bytes() == b"\x1f\x8b raw"
    assert (sd / "truedata" / "progress.db").exists()
    assert (sd / "options" / "NIFTY" / "2026-10-20" / "25000CE.csv").read_text() == "server's own recording\n"


def test_cancel_and_market_hours(tmp_path):
    mac, server, env = _setup(tmp_path)
    r = _run(mac, env, "no")
    assert r.returncode == 0 and "Nothing was copied" in r.stdout
    assert not (server / "data" / "spot").exists()
    out = _run(mac, dict(env, ALGO_IGNORE_HOURS=""), "1").stdout
    assert "market hours" in out or "Backtest data" in out    # depends on the time the test runs


def test_summary_ignores_warning_lines_from_the_server(tmp_path):
    """16 Oct: a warning line from ssh came first, and the summary said the data folder held 0.00 GB."""
    mac, server, env = _setup(tmp_path)
    noisy = tmp_path / "noisy_ssh"
    noisy.write_text('#!/bin/bash\nshift\necho "Warning: Permanently added the server to the list of known hosts." >&2\nexec bash -c "$1"\n')
    noisy.chmod(0o755)
    r = _run(mac, dict(env, ALGO_SSH=str(noisy)), "1")
    assert r.returncode == 0, r.stdout + r.stderr
    last = [x for x in r.stdout.splitlines() if "now holds" in x][-1]
    assert "0.00 GB" not in last and " MB" in last and "free" in last
