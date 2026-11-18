"""deploy/update_source.sh: which GitHub repo the server updates from (update_repo, public, no key) and which one it
reads with its deploy key (repo: the IBKR snapshot, and updates from a private repo)."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


def _code(tmp_path, origin=None, stamp=None, update_from=None):
    src = tmp_path / "src"
    (src / "deploy").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(src), "remote", "add", "origin", origin], check=True)
    if stamp:
        (src / "deploy" / "REPO").write_text(stamp + "\n")
    if update_from:
        (src / "deploy" / "UPDATE_FROM").write_text(update_from + "\n")
    return src


def _run(tmp_path, src, public):
    self_dir = tmp_path / "self"
    r = subprocess.run(["bash", str(ROOT / "deploy" / "update_source.sh"), str(src), str(self_dir)],
                       env=dict(os.environ, UPDATE_SOURCE_PUBLIC=public), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    read = lambda n: (self_dir / n).read_text().strip() if (self_dir / n).exists() else None  # noqa: E731
    return read("repo"), read("update_repo")


def test_a_private_repo_is_read_with_the_deploy_key(tmp_path):
    src = _code(tmp_path, stamp="https://github.com/hamid/algotrading.git")      # the Mac's pack
    assert _run(tmp_path, src, "no") == ("git@github.com:hamid/algotrading.git", None)
    src2 = _code(tmp_path / "b", origin="git@github.com:hamid/algotrading.git")  # the server's own copy
    assert _run(tmp_path / "b", src2, "no") == ("git@github.com:hamid/algotrading.git", None)


def test_a_private_repo_can_move_updates_to_a_public_one(tmp_path):
    """The one-time switch: the private repo's deploy/UPDATE_FROM names the public copy; the deploy-key repo stays
    the private one (the IBKR snapshot comes from there)."""
    src = _code(tmp_path, origin="git@github.com:hamid/algotrading.git",
                update_from="https://github.com/hamid/algotrading-public")
    assert _run(tmp_path, src, "no") == ("git@github.com:hamid/algotrading.git", "https://github.com/hamid/algotrading-public")


def test_updates_from_the_public_repo_keep_the_snapshot_repo(tmp_path):
    """After the switch the code comes from the public repo itself: it stays the update repo, and `repo` is never
    overwritten with it, even if GitHub cannot be reached at that moment."""
    self_dir = tmp_path / "self"
    self_dir.mkdir()
    (self_dir / "repo").write_text("git@github.com:hamid/algotrading.git\n")
    (self_dir / "update_repo").write_text("https://github.com/hamid/algotrading-public\n")
    src = _code(tmp_path, origin="https://github.com/hamid/algotrading-public")
    assert _run(tmp_path, src, "yes") == ("git@github.com:hamid/algotrading.git", "https://github.com/hamid/algotrading-public")
    assert _run(tmp_path, src, "no") == ("git@github.com:hamid/algotrading.git", "https://github.com/hamid/algotrading-public")


def test_someone_elses_public_copy_updates_without_a_key(tmp_path):
    src = _code(tmp_path, stamp="https://github.com/friend/algotrading-public.git")
    assert _run(tmp_path, src, "yes") == (None, "https://github.com/friend/algotrading-public")


def test_nothing_known_writes_nothing(tmp_path):
    assert _run(tmp_path, _code(tmp_path), "no") == (None, None)
