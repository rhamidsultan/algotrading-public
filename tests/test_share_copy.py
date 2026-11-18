"""deploy/make-share-copy.command: today's code as one fresh commit, without this setup's notes or anything private."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True).stdout


def _repo(tmp_path, extra=None):
    repo = tmp_path / "repo"
    files = {"run.py": "print(1)\n", "docs/SERVER_SETUP.md": "guide\n", "docs/STATUS.md": "my notes\n",
             "docs/DECISIONS.md": "my choices\n", "docs/handoff/a.md": "x\n", "CLAUDE.md": "about me\n", **(extra or {})}
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    (repo / "deploy").mkdir(exist_ok=True)
    shutil.copy(ROOT / "deploy" / "make-share-copy.command", repo / "deploy")
    _git(repo.parent, "init", "-q", "-b", "main", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=me@example.org", "-c", "user.name=me", "commit", "-qm", "old history")
    _git(repo, "remote", "add", "origin", "https://github.com/someone/private-trading.git")
    return repo


def _run(tmp_path, repo, *args):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "SHARE_NO_FETCH": "1", "SHARE_BRANCH": "HEAD",
           "SHARE_OUT": str(tmp_path / "out")}
    return subprocess.run(["bash", str(repo / "deploy" / "make-share-copy.command"), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def test_bundle_is_one_commit_without_the_notes(tmp_path):
    repo = _repo(tmp_path)
    r = _run(tmp_path, repo)
    assert r.returncode == 0, r.stdout + r.stderr
    bundle = next((tmp_path / "out").glob("AlgoLab-code-*.bundle"))
    copy = tmp_path / "copy"
    _git(tmp_path, "clone", "-q", str(bundle), str(copy))
    assert _git(copy, "log", "--format=%s").splitlines() == [_git(copy, "log", "-1", "--format=%s").strip()]
    assert "old history" not in _git(copy, "log", "--all", "--format=%s")
    assert (copy / "docs/SERVER_SETUP.md").exists() and (copy / "run.py").exists()
    for gone in ("docs/STATUS.md", "docs/DECISIONS.md", "docs/handoff"):
        assert not (copy / gone).exists()
    assert "about me" not in (copy / "CLAUDE.md").read_text() and "SERVER_SETUP" in (copy / "CLAUDE.md").read_text()
    assert "me@example.org" not in _git(copy, "log", "--format=%ae %ce")


def test_pushes_to_their_empty_repo(tmp_path):
    repo = _repo(tmp_path)
    theirs = tmp_path / "theirs.git"
    _git(tmp_path, "init", "-q", "--bare", str(theirs))
    r = _run(tmp_path, repo, str(theirs))
    assert r.returncode == 0 and "Done" in r.stdout, r.stdout + r.stderr
    assert len(_git(theirs, "log", "--format=%h", "main").splitlines()) == 1
    assert _run(tmp_path, repo, str(theirs)).returncode == 1          # never overwrites a repo that has commits


def test_stops_on_the_repo_name_or_saved_server(tmp_path):
    repo = _repo(tmp_path, {"algolab/x.py": "URL = 'someone/private-trading'\n"})
    r = _run(tmp_path, repo)
    assert r.returncode == 1 and "Stopped" in r.stdout and "algolab/x.py" in r.stdout
    repo2 = _repo(tmp_path / "b", {"algolab/y.py": "HOST = '203.0.113.9'\n"})
    (tmp_path / "b" / ".algo-trading").mkdir(parents=True)
    (tmp_path / "b" / ".algo-trading" / "server.env").write_text('ALGO_HOST="ubuntu@203.0.113.9"\n')
    r = _run(tmp_path / "b", repo2)
    assert r.returncode == 1 and "algolab/y.py" in r.stdout and not list((tmp_path / "b" / "out").glob("*"))
