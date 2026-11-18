"""deploy/make-friend-bundle.command: one file with the backtest program and price data, nothing private."""
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "make-friend-bundle.command"


def _repo(tmp_path, extra=None):
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    shutil.copy(SCRIPT, repo / "deploy")
    for name, text in {"run.py": "print(1)\n", "config.yaml": "server:\n  host: ''\n", "README.md": "his\n",
                       "server/app.py": "x\n", "docs/STATUS.md": "x\n", "tests/test_x.py": "x\n",
                       "CLAUDE.md": "x\n", **(extra or {})}.items():
        f = repo / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    g("init", "-q"); g("add", "-A")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    data = tmp_path / "data"
    for rel in ("options/NIFTY/2026-01-06/26000CE.csv", "spot/TD_NIFTY_1minute.csv", "truedata/raw.csv"):
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_text("time,last\n")
    return repo, data


def _run(tmp_path, repo, data, choice):
    out = tmp_path / "out"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "ALGO_DATA": str(data), "ALGO_CHOICE": choice,
           "ALGO_OUT": str(out)}
    r = subprocess.run(["bash", str(repo / "deploy" / SCRIPT.name)], env=env, capture_output=True, text=True)
    files = list(out.glob("AlgoLab-*.tar.gz")) if out.exists() else []
    names = tarfile.open(files[0]).getnames() if files else []
    return r, names, out


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_bundle_has_code_and_data_but_nothing_private(tmp_path):
    repo, data = _repo(tmp_path)
    r, names, out = _run(tmp_path, repo, data, "1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "AlgoLab/run.py" in names and "AlgoLab/READ ME FIRST.txt" in names and "AlgoLab/README.md" in names
    assert "AlgoLab/data/options/NIFTY/2026-01-06/26000CE.csv" in names
    assert not [n for n in names if n.startswith(("AlgoLab/server", "AlgoLab/docs", "AlgoLab/deploy",
                                                  "AlgoLab/tests", "AlgoLab/data/truedata"))]
    assert "AlgoLab/CLAUDE.md" not in names
    assert list(out.glob("*.sha256"))


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_choice_two_adds_truedata_and_cancel_makes_nothing(tmp_path):
    repo, data = _repo(tmp_path)
    r, names, _ = _run(tmp_path, repo, data, "2")
    assert "AlgoLab/data/truedata/raw.csv" in names
    r, names, _ = _run(tmp_path / "b", *_repo(tmp_path / "b"), "x")
    assert "Nothing was made" in r.stdout and not names


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_stops_when_something_private_is_committed(tmp_path):
    repo, data = _repo(tmp_path, {"algolab/x.py": "HOST = '203.0.113.7'\n"})
    (tmp_path / ".algo-trading").mkdir()
    (tmp_path / ".algo-trading" / "server.env").write_text('ALGO_HOST="ubuntu@203.0.113.7"\n')
    r, names, _ = _run(tmp_path, repo, data, "1")
    assert r.returncode == 1 and "Stopped" in r.stdout and not names
