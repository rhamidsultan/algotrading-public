"""deploy/self_update.sh: the server updating itself from GitHub, run against a local stand-in for GitHub."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not all(shutil.which(x) for x in ("git", "flock")), reason="needs git and flock")


def _git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True,
                          env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                                   GIT_COMMITTER_EMAIL="t@t")).stdout.strip()


def _setup(tmp_path):
    work, bare = tmp_path / "work", tmp_path / "github.git"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    (work / "a.txt").write_text("1")
    _git(work, "add", "."); _git(work, "commit", "-qm", "first version")
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    _git(work, "remote", "add", "origin", str(bare))
    engine = tmp_path / "engine"
    (engine / "runtime").mkdir(parents=True)
    (engine / "run.py").write_text("")
    path = os.environ["PATH"]
    if not shutil.which("ssh-keygen"):          # a stand-in that writes a key pair the way ssh-keygen -f does
        b = tmp_path / "bin"; b.mkdir()
        (b / "ssh-keygen").write_text('#!/bin/bash\nf="${@: -1}"\necho PRIVATE > "$f"\necho "ssh-ed25519 AAAAtest algolab-server-read-only" > "$f.pub"\n')
        (b / "ssh-keygen").chmod(0o755)
        path = f"{b}:{path}"
    env = dict(os.environ, PATH=path, ENGINE_DIR=str(engine), SELF_UPDATE_HOME=str(tmp_path / "home"), SELF_UPDATE_REPO=str(bare),
               SELF_UPDATE_CMD=f"bash -c 'cat a.txt > {tmp_path}/deployed.txt'", HOME=str(tmp_path))
    return work, engine, env


def _run(env, *args):
    return subprocess.run(["bash", str(ROOT / "deploy" / "self_update.sh"), *args], env=env, capture_output=True, text=True, timeout=120)


def _state(engine):
    return json.loads((engine / "runtime" / "self-update.json").read_text())


def test_check_update_and_what_is_waiting(tmp_path):
    work, engine, env = _setup(tmp_path)
    sha1 = _git(work, "rev-parse", "HEAD")
    assert _run(env, "check").returncode == 0
    s = _state(engine)
    assert s["status"] == "checked" and s["latest"] == sha1 and s["public_key"].startswith("ssh-ed25519")

    (engine / "runtime" / "self-update.request").write_text("update")      # what the dashboard's button writes
    r = _run(env, "run")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not (engine / "runtime" / "self-update.request").exists()
    assert (engine / ".deployed_commit").read_text().strip() == sha1 and (tmp_path / "deployed.txt").read_text() == "1"
    assert _state(engine)["status"] == "updated" and _state(engine)["behind"] == 0

    (work / "a.txt").write_text("2"); _git(work, "commit", "-qam", "second version"); _git(work, "push", "-q", "origin", "main")
    _run(env, "check")
    s = _state(engine)
    assert s["behind"] == 1 and s["pending"][0].endswith("second version")

    bad = dict(env, SELF_UPDATE_CMD="bash -c 'echo rolled back; exit 3'")
    assert _run(bad, "update").returncode == 3
    s = _state(engine)
    assert s["status"] == "failed" and (engine / ".deployed_commit").read_text().strip() == sha1   # still the old version
    assert "rolled back" in (engine / "runtime" / "self-update.log").read_text()


def test_unknown_request_does_nothing_and_unreachable_github_says_so(tmp_path):
    work, engine, env = _setup(tmp_path)
    (engine / "runtime" / "self-update.request").write_text("rm -rf /")
    assert _run(env, "run").returncode == 0 and not (engine / "runtime" / "self-update.json").exists()
    r = _run(dict(env, SELF_UPDATE_REPO=str(tmp_path / "nowhere.git")), "check")
    assert r.returncode == 1 and _state(engine)["status"] == "failed"


def test_dashboard_asks_the_updater(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import importlib.util
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    monkeypatch.setenv("SELF_UPDATE_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("algolab_panel_su", ROOT / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    app = flask.Flask("t"); app.register_blueprint(mod.panel)
    c = app.test_client()
    s = c.get("/api/server-update").get_json()
    assert s["installed"] is False and s["requested"] is False and s["repo_keys_url"] is None   # no repo set yet
    (tmp_path / "home").mkdir()
    (tmp_path / "home" / "repo").write_text("git@github.com:someone/their-copy.git\n")
    assert c.get("/api/server-update").get_json()["repo_keys_url"] == "https://github.com/someone/their-copy/settings/keys/new"
    assert c.post("/api/server-update", json={"action": "reboot"}).status_code == 400
    s = c.post("/api/server-update", json={"action": "update"}).get_json()
    assert s["requested"] is True and (mod.RT / "self-update.request").read_text() == "update"
    (mod.RT / "self-update.json").write_text(json.dumps({"status": "updated", "deployed": "abc", "behind": 0}))
    assert c.get("/api/server-update").get_json()["status"] == "updated"
    assert 'id="srvCard"' in mod.PROFILE_PAGE and "srvLoad();" in mod.PROFILE_PAGE


def test_no_repo_set_says_how_to_set_it(tmp_path):
    work, engine, env = _setup(tmp_path)
    env = {k: v for k, v in env.items() if k != "SELF_UPDATE_REPO"}
    r = _run(env, "check")
    st = json.loads((engine / "runtime" / "self-update.json").read_text())
    assert r.returncode == 1 and st["status"] == "failed" and "No GitHub repo" in st["message"]
    (tmp_path / "home" / "repo").write_text(str(tmp_path / "github.git"))     # the file update_engine.sh writes
    assert _run(env, "check").returncode == 0
    assert json.loads((engine / "runtime" / "self-update.json").read_text())["status"] == "checked"


def test_quiet_check_records_what_is_waiting_and_keeps_the_last_message(tmp_path):
    work, engine, env = _setup(tmp_path)
    assert _run(env, "update").returncode == 0
    assert _state(engine)["status"] == "updated"
    msg = _state(engine)["message"]
    (work / "a.txt").write_text("2"); _git(work, "commit", "-qam", "second version"); _git(work, "push", "-q", "origin", "main")
    assert _run(env, "peek").returncode == 0                     # what the 30-minute timer runs
    s = _state(engine)
    assert s["behind"] == 1 and s["pending"][0].endswith("second version")
    assert s["status"] == "updated" and s["message"] == msg     # the last update's result is still what it says
    before = (engine / "runtime" / "self-update.json").read_text()
    (tmp_path / "github.git").rename(tmp_path / "away.git")      # GitHub out of reach: nothing written, no error shown
    assert _run(env, "peek").returncode == 0
    assert (engine / "runtime" / "self-update.json").read_text() == before


def test_update_status_for_the_menu_summary_and_profile(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    import importlib.util
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    monkeypatch.setenv("SELF_UPDATE_HOME", str(tmp_path / "home"))
    spec = importlib.util.spec_from_file_location("algolab_panel_us", ROOT / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    mod.RT.mkdir(parents=True, exist_ok=True)
    st = mod.RT / "self-update.json"
    assert mod._update_status()["state"] == "setup"                                  # updater not installed
    (tmp_path / "home").mkdir(); (tmp_path / "home" / "self_update.sh").write_text("")
    assert mod._update_status()["state"] == "unknown"
    st.write_text(json.dumps({"status": "updated", "behind": 0, "deployed": "abcdef1234"}))
    assert mod._update_status()["state"] == "current" and mod._update_status()["deployed"] == "abcdef1"
    st.write_text(json.dumps({"status": "checked", "behind": 2, "pending": ["a1 one", "b2 two"]}))
    u = mod._update_status()
    assert u["state"] == "waiting" and u["behind"] == 2 and u["label"] == "Update waiting"
    (mod.RT / "self-update.request").write_text("update")
    assert mod._update_status()["state"] == "updating"
    (mod.RT / "self-update.request").unlink()
    st.write_text(json.dumps({"status": "working", "message": "Updating to abc first"}))
    assert mod._update_status()["state"] == "updating"
    st.write_text(json.dumps({"status": "failed", "behind": 1, "message": "The update stopped"}))
    assert mod._update_status()["state"] == "failed"

    app = flask.Flask("t"); app.secret_key = "x"; app.register_blueprint(mod.panel)
    st.write_text(json.dumps({"status": "checked", "behind": 3}))
    with app.test_request_context("/"):
        nav = mod._nav_html("/summary", {"name": "Hamid"})
    assert 'class="sn-badge waiting"' in nav and "Update waiting: 3 changes" in nav and 'href="/profile#srvCard"' in nav
    st.write_text(json.dumps({"status": "updated", "behind": 0}))
    with app.test_request_context("/"):
        assert "sn-badge" not in mod._nav_html("/summary", {"name": "Hamid"})        # nothing to say when up to date
    c = app.test_client()
    assert c.get("/api/server-update").get_json()["summary"]["state"] == "current"
    assert 'id="srvState"' in mod.PROFILE_PAGE and mod.PROFILE_PAGE.index('id="srvCard"') < mod.PROFILE_PAGE.index("About you")
    assert "updateBar(d.server_update)" in mod.SUMMARY_PAGE and 'id="updNow"' in mod.SUMMARY_PAGE


def test_update_installs_the_30_minute_github_check():
    s = (ROOT / "deploy" / "update_engine.sh").read_text()
    assert "algolab-selfupdate-check.timer" in s and "ExecStart=$SELF/self_update.sh peek" in s and "OnUnitActiveSec=30min" in s


def test_updates_move_to_another_repo(tmp_path):
    """The server's copy follows a new update address (a public copy with its own history), not its old remote."""
    work, engine, env = _setup(tmp_path)
    env = {k: v for k, v in env.items() if k != "SELF_UPDATE_REPO"}
    home = tmp_path / "home"
    home.mkdir()
    (home / "repo").write_text(str(tmp_path / "github.git") + "\n")   # the private repo (also the IBKR snapshot's)
    assert _run(env, "update").returncode == 0
    assert (tmp_path / "deployed.txt").read_text() == "1"
    other = tmp_path / "public-work"
    other.mkdir()
    _git(other, "init", "-q", "-b", "main")
    (other / "a.txt").write_text("public copy")
    _git(other, "add", "."); _git(other, "commit", "-qm", "snapshot without history")
    _git(tmp_path, "clone", "-q", "--bare", str(other), str(tmp_path / "public.git"))
    (home / "update_repo").write_text(str(tmp_path / "public.git") + "\n")   # written by update_source.sh
    r = _run(env, "check")
    assert r.returncode == 0, r.stdout + r.stderr
    s = _state(engine)
    assert s["latest_subject"] == "snapshot without history" and s["behind"] == 1
    assert _run(env, "update").returncode == 0
    assert (tmp_path / "deployed.txt").read_text() == "public copy"
    assert _git(home / "src", "remote", "get-url", "origin") == str(tmp_path / "public.git")
    assert (home / "repo").read_text().strip() == str(tmp_path / "github.git")   # the snapshot's repo is untouched
