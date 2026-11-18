"""deploy/get-dashboard-pages.command against a stand-in server folder (no network): copies code and pages only."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(not shutil.which("tar"), reason="needs tar")


def test_copies_code_and_pages_never_secrets(tmp_path):
    srv = tmp_path / "trader"
    (srv / "templates").mkdir(parents=True)
    (srv / "static").mkdir()
    (srv / "app.py").write_text("print('app')\n")
    (srv / "algolab_panel.py").write_text("x = 1\n")
    (srv / "algolab_panel.py.bak-20261014").write_text("old\n")
    (srv / "templates" / "dashboard.html").write_text("<html>live page</html>\n")
    (srv / "templates" / "dashboard.html.before-algolab").write_text("old page\n")
    (srv / "static" / "sw.js").write_text("// sw\n")
    for secret in ("config.json", "token.json", ".secret_key", "portfolio.db"):
        (srv / secret).write_text("SECRET\n")
    fake = tmp_path / "fake_ssh"
    fake.write_text('#!/bin/bash\nshift\nexec bash -c "$1"\n')
    fake.chmod(0o755)
    mac = tmp_path / "mac"
    (mac / "deploy").mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "get-dashboard-pages.command", mac / "deploy" / "get.command")
    shutil.copy(ROOT / "deploy" / "server_login.sh", mac / "deploy")
    out = tmp_path / "copies"
    env = dict(os.environ, ALGO_SSH=str(fake), ALGO_HOST="server", ALGO_TRADER=str(srv), ALGO_COPY_TO=str(out))
    r = subprocess.run(["bash", str(mac / "deploy" / "get.command")], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    got = next(out.iterdir())
    files = sorted(str(p.relative_to(got)) for p in got.rglob("*") if p.is_file())
    assert files == ["algolab_panel.py", "app.py", "listing.txt", "static/sw.js", "templates/dashboard.html"]
    assert "SECRET" not in "".join(p.read_text() for p in got.rglob("*") if p.is_file() and p.name != "listing.txt")
    assert "app.py" in (got / "listing.txt").read_text()                     # names and checksums for comparison
