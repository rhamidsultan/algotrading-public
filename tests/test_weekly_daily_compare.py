"""The Saturday report carries the week day by day (1-minute and 1-second); Get data from server brings 1-second bars."""
import subprocess
from pathlib import Path


def test_weekly_report_includes_the_day_by_day_comparison(tmp_path, monkeypatch):
    from algolab import daily_compare, paper_report
    monkeypatch.setattr(paper_report, "OUT", tmp_path / "paper_report.json")
    monkeypatch.setattr(paper_report, "_sessions", lambda: [])
    monkeypatch.setattr("algolab.feeds.preferred_dataset", lambda *a: "NIFTY_minute")
    seen = {}
    monkeypatch.setattr(daily_compare, "run", lambda s, e, bars, **k: seen.update(s=s, e=e, bars=bars) or {"flagged": 2, "sessions": []})
    out = paper_report.weekly({}, days=7, publish=False, log=lambda m: None)
    assert out["daily"] == {"flagged": 2, "sessions": []} and seen["bars"] == ("1m", "1s")
    assert (seen["e"] - seen["s"]).days == 6
    monkeypatch.setattr(daily_compare, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no bars")))
    assert "no bars" in paper_report.weekly({}, days=7, publish=False, log=lambda m: None)["daily"]["error"]


def test_get_data_from_server_mirrors_1_second_bars_and_tolerates_none(tmp_path, monkeypatch):
    from algolab import kite as k
    monkeypatch.setattr(k, "DATA", tmp_path)
    monkeypatch.setattr(k, "SPOT_DIR", tmp_path / "spot")
    (tmp_path / "spot").mkdir()
    key = tmp_path / "key.pem"
    key.write_text("x")
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if "sec1/" in " ".join(cmd):
            return subprocess.CompletedProcess(cmd, 23, "", "rsync: change_dir failed: No such file or directory")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/rsync")
    k.sync_from_server("ubuntu@x", str(key))                         # no 1-second bars on the server: still fine
    sec = [c for c in cmds if any(str(x).endswith("/sec1/") for x in c)]
    assert sec and "--delete" in sec[0]                              # the Mac keeps the server's days, no more
    assert not any("--delete" in c for c in cmds if c not in sec)    # spot and options are never trimmed
