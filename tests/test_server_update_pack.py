"""deploy/make_server_update.sh packs the dashboard's page templates with the engine and panels."""
import base64
import io
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not shutil.which("tar"), reason="needs tar")
def test_update_pack_contains_templates(tmp_path):
    work = tmp_path / "repo"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "data", "runs", "runtime", "__pycache__", ".venv"))
    subprocess.run(["bash", "deploy/make_server_update.sh"], cwd=work, check=True, capture_output=True)
    line = (work / "deploy" / "paste-engine-update.txt").read_text()
    b64 = re.search(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d", line).group(1)
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz").getnames()
    for page in ("dashboard.html", "stock.html", "us.html", "backtests.html", "signin.html"):
        assert f"server/portfolio/templates/{page}" in names
    assert "deploy/portfolio_page/algolab_panel.py" in names and "deploy/update_engine.sh" in names
    assert not any(n.endswith((".json", ".db", ".secret_key")) for n in names)


@pytest.mark.skipif(not shutil.which("tar"), reason="needs tar")
def test_update_pack_contains_the_dashboard_program(tmp_path):
    """16 Oct: app.py changes (e.g. the Total P&L chart's data) never reached the server; now they ship too."""
    work = tmp_path / "repo"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "data", "runs", "runtime", "__pycache__", ".venv"))
    subprocess.run(["bash", "deploy/make_server_update.sh"], cwd=work, check=True, capture_output=True)
    line = (work / "deploy" / "paste-engine-update.txt").read_text()
    b64 = re.search(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d", line).group(1)
    names = tarfile.open(fileobj=io.BytesIO(base64.b64decode(b64)), mode="r:gz").getnames()
    for f in ("app.py", "backtests_panel.py", "ibkr_panel.py", "order_test_panel.py", "positions_panel.py", "paper_engine.py",
              "server/auth_server.py", "server/kite_session.py"):
        assert f"server/portfolio/{f}" in names, f
    src = (ROOT / "deploy" / "update_engine.sh").read_text()
    assert "APP_CHANGED" in src and 'cp -p "$TRADER_DIR/$n.bak-$STAMP" "$TRADER_DIR/$n"; else rm -f "$TRADER_DIR/$n"; fi; done' in src   # backed up, put back
    # on failure; a file new to the server (positions_panel.py) has no backup and is removed instead
