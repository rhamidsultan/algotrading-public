"""Parameter sweep helpers used by the dashboard's Parameter sweep tab."""
import json

import pandas as pd
import pytest

from algolab.sweep import grid_size, heat_table, neighbour_score, value_range


def test_value_ranges_follow_the_setting_type():
    assert value_range(40.0, "20", "60", "10") == ["20", "30", "40", "50", "60"]
    assert value_range(300, 100, 400, 100) == ["100", "200", "300", "400"]
    assert value_range(1.25, 1, 1.5, 0.25) == ["1", "1.25", "1.5"]
    assert value_range("09:30", "09:20", "10:00", 10) == ["09:20", "09:30", "09:40", "09:50", "10:00"]
    with pytest.raises(ValueError):
        value_range(40.0, 60, 20, 10)
    with pytest.raises(ValueError):
        value_range("09:30", "09:20", "10:00", 0)
    assert grid_size({"a": ["1", "2", "3"], "b": ["x", "y"]}) == 6


def test_robustness_and_heatmap():
    grid = {"a": ["1", "2", "3"], "b": ["10", "20"], "fixed": ["5"]}
    df = pd.DataFrame([{"a": a, "b": b, "fixed": "5", "in_net": int(a) * int(b)} for a in grid["a"] for b in grid["b"]])
    score = neighbour_score(df, grid, "in_net").tolist()
    assert score[0] == round((10 + 20 + 20) / 3, 2)          # a=1,b=10 with neighbours a=2,b=10 and a=1,b=20
    assert score[-1] == round((60 + 30 + 40) / 3, 2)         # a=3,b=20 with a=2,b=20 and a=3,b=10
    t = heat_table(df, "a", "b", "in_net")
    assert list(t.columns) == ["1", "2", "3"] and list(t.index) == ["10", "20"] and t.loc["20", "3"] == 60


def test_finished_combinations_are_kept_if_a_sweep_stops(tmp_path, monkeypatch):
    import algolab.sweep as sw
    calls = []

    def one(job):
        calls.append(job[1])
        if len(calls) == 3:
            raise KeyboardInterrupt   # stands in for the sweep being stopped part-way
        return dict(job[1], all_net=len(calls) * 100.0)
    monkeypatch.setattr(sw, "_one", one)
    monkeypatch.setattr(sw, "data_fingerprint", lambda d: {})
    with pytest.raises(KeyboardInterrupt):
        sw.sweep("short_straddle", {"stop_loss_pct": ["20", "30", "40", "50"]}, "X", workers=1, cfg={"a": 1},
                 out=tmp_path / "s")
    part = pd.read_csv(tmp_path / "s" / "sweep.partial.csv")
    assert list(part["all_net"]) == [100.0, 200.0] and not (tmp_path / "s" / "sweep.csv").exists()
    assert json.loads((tmp_path / "s" / "sweep.json").read_text())["total"] == 4
    monkeypatch.setattr(sw, "RUNS_DIR", tmp_path)
    assert sw.past_sweeps() == [tmp_path / "s"]                 # a stopped sweep can still be opened

    monkeypatch.setattr(sw, "_one", lambda job: dict(job[1], all_ret_dd=float(job[1]["stop_loss_pct"])))
    df, out = sw.sweep("short_straddle", {"stop_loss_pct": ["20", "30"]}, "X", workers=1, cfg={"a": 1}, out=tmp_path / "t")
    assert (out / "sweep.csv").exists() and not (out / "sweep.partial.csv").exists()
    assert list(df["stop_loss_pct"].astype(str)) == ["30", "20"]   # ranked at the end


def test_safe_workers_fit_memory(monkeypatch):
    import algolab.sweep as sw
    monkeypatch.setattr(sw.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(sw, "memory_gb", lambda: 8.0)
    assert sw.safe_workers() == 3                                # 8 GB: 3 runs of ~2.5 GB, not 7
    monkeypatch.setattr(sw, "memory_gb", lambda: 64.0)
    assert sw.safe_workers() == 7                                # plenty of memory: one per core, one spare


def _stop_one(job):
    """A combination for the Stop test: notes itself, presses Stop, takes a moment (module level: sent to workers)."""
    import os
    import time
    from pathlib import Path
    (Path(os.environ["SWEEP_TEST_SEEN"]) / str(job[1]["stop_loss_pct"])).write_text("x")
    Path(os.environ["ALGOLAB_STOP_FILE"]).write_text("x")
    time.sleep(0.3)
    return dict(job[1], all_net=1.0)


def test_stop_drops_the_combinations_not_started(tmp_path, monkeypatch):
    """Stop on the page during a sweep run by several processes: the ones not started are dropped."""
    import multiprocessing
    import algolab.sweep as sw
    from algolab import stopping
    if multiprocessing.get_start_method() != "fork":
        pytest.skip("the stand-in combination is patched in, which needs forked workers")
    seen = tmp_path / "seen"
    seen.mkdir()
    monkeypatch.setenv("SWEEP_TEST_SEEN", str(seen))
    monkeypatch.setattr(sw, "_one", _stop_one)
    monkeypatch.setattr(sw, "data_fingerprint", lambda d: {})
    stopping.watch(tmp_path / "stop")
    try:
        with pytest.raises(stopping.Stopped):
            sw.sweep("short_straddle", {"stop_loss_pct": [str(v) for v in range(10, 90, 2)]}, "X", workers=2,
                     cfg={"a": 1}, out=tmp_path / "s")
    finally:
        stopping.watch(None)
    assert len(list(seen.iterdir())) < 10                     # of 40: the rest never started
