import pytest


@pytest.fixture(autouse=True)
def _runtime_files_in_tmp(tmp_path, monkeypatch):
    """Notifications and the live feed's status file written during tests go to a temporary folder."""
    monkeypatch.setenv("ALGOLAB_PUSH_DIR", str(tmp_path / "push_outbox"))
    from algolab import kite
    if not str(kite.RUNTIME).startswith(str(tmp_path.parent.parent)):
        monkeypatch.setattr(kite, "RUNTIME", tmp_path / "runtime")
