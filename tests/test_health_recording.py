"""Summary › Health: the daily price recording's light says when it last ran, why it failed, and when it catches up."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OK = "2026-10-15 15:45  NIFTY bars stored: 75; option contracts updated: 80"
FAIL = "2026-10-16 15:45  FAILED: Read timed out. (read timeout=7)"


def _panel(tmp_path, monkeypatch):
    pytest.importorskip("flask")
    monkeypatch.setenv("ALGOLAB_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("algolab_panel_rec", ROOT / "deploy" / "portfolio_page" / "algolab_panel.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["algolab_panel_rec"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_one_failed_run_says_why_and_two_in_a_row_count(tmp_path, monkeypatch):
    mod = _panel(tmp_path, monkeypatch)
    r = mod._recording_status([OK, FAIL, ""])
    assert r["failed"] and r["failed_in_row"] == 1 and r["when"] == "2026-10-16 15:45"
    assert r["why"] == "Read timed out. (read timeout=7)" and r["next"].endswith("15:45 IST")
    assert mod._recording_status([OK, FAIL, FAIL.replace("16", "19")])["failed_in_row"] == 2
    ok = mod._recording_status([FAIL, OK])
    assert not ok["failed"] and ok["why"] == "" and ok["when"] == "2026-10-15 15:45"
    assert mod._recording_status([]) == {"last": "", "when": "", "failed": False, "why": "", "failed_in_row": 0,
                                         "next": ok["next"]}
    # a successful run that merely skipped a few contracts is not a failure
    assert not mod._recording_status([OK + " (skipped after retries: 25000CE 2026-10-20)"])["failed"]


def test_health_reads_the_recording_log(tmp_path, monkeypatch):
    mod = _panel(tmp_path, monkeypatch)
    mod.DATA.mkdir(parents=True, exist_ok=True)
    (mod.DATA / "collect_log.txt").write_text(OK + "\n" + FAIL + "\n")
    monkeypatch.setattr(mod, "_server", lambda: {})
    rec = mod._health()["recording"]
    assert rec["failed"] and rec["why"].startswith("Read timed out")


def test_summary_light_is_amber_for_one_failure_and_shows_the_reason():
    page = (ROOT / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()
    assert "function recLight(r)" in page and "recLight(h.recording)" in page
    assert 'r.failed_in_row > 1 ? "bad" : "warn"' in page and "fetches the last 7 days again" in page
    assert "/fail/i.test(h.recording.last" not in page          # the old check: any line with "fail" was red
