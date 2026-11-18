"""Summary › Health: CPU and memory history for the graphs, market hours for the price light, plain texts."""
import pytest

from test_defined_risk import _panel


def test_cpu_memory_history_and_health_fields(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    pts = [[86400 * 100 + i * 60, 10.0 + (i % 2) * 10, 50.0] for i in range(30)]       # 30 minutes, 10 % and 20 % CPU
    h = mod._history(pts)
    assert len(h) == 2 and 14 < h[0][1] < 16 and h[0][2] == 50.0                     # 15-minute averages
    assert mod._history([]) == []
    import pathlib
    if not pathlib.Path("/proc/stat").exists():
        pytest.skip("needs /proc")
    monkeypatch.setattr(mod, "_STATS", tmp_path / "runtime" / "server_stats.json")
    mod._stats.update(started=True, points=[], last=mod._cpu_times())
    for _ in range(50000):                       # let some CPU time pass
        pass
    pt = mod._sample_once()
    assert pt is None or (0 <= pt[1] <= 100 and 0 < pt[2] < 100)
    mod._stats["points"] = pts
    mod._server_cache.clear()
    srv = mod._server()
    assert srv["history"] and srv["cpu"] == pts[-1][1]
    hl = mod._health()
    assert "market_open" in hl and isinstance(hl["market_open"], bool)


def test_pages_say_things_plainly(tmp_path, monkeypatch):
    (tmp_path / "runtime").mkdir()
    mod = _panel(tmp_path, monkeypatch)
    page = mod.SUMMARY_PAGE
    assert "Market closed: live prices start at 9:15" in page and "Price saving" in page and "CPU busy" in page
    assert "RAM ${" not in page                                  # memory once, as a gauge, not also as a light
    prof = mod.PROFILE_PAGE if hasattr(mod, "PROFILE_PAGE") else ""
    assert not prof or ("Running <b>" not in prof and "drop commit codes" in prof)
    assert "u.deployed" not in page                               # Summary: "Up to date · checked 10:05", no code
