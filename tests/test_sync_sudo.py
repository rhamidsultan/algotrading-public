"""Copying prices from the server reads them through sudo when the engine's user owns them (after the 18 Oct move)."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from algolab import kite  # noqa: E402


def test_permission_denied_retries_with_sudo(tmp_path, monkeypatch):
    key = tmp_path / "k.pem"
    key.write_text("x")
    monkeypatch.setattr(kite, "DATA", tmp_path / "data")
    monkeypatch.setattr(kite, "SPOT_DIR", tmp_path / "data" / "spot")
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        if "--rsync-path" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=23, stdout="", stderr='rsync: change_dir "/opt/algolab/data/spot" failed: Permission denied (13)')
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rsync" if name == "rsync" else None)   # rsync installed
    kite.sync_from_server("ubuntu@203.0.113.5", str(key))
    sudo = [c for c in calls if "--rsync-path" in c]
    assert len(sudo) == 3 and all(c[c.index("--rsync-path") + 1] == "sudo -n rsync" for c in sudo)   # spot, options, sec1
    assert '--rsync-path="sudo -n rsync"' in (Path(__file__).resolve().parents[1] / "deploy" / "get-server-data.command").read_text()
