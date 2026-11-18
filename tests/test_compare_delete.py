"""Backtests › Compare with paper: deleting a finished comparison (its report and job record)."""
import json

import pytest

from test_defined_risk import _panel

JID = "20261027-213000-abcd"


@pytest.fixture
def app(tmp_path, monkeypatch):
    flask = pytest.importorskip("flask")
    mod = _panel(tmp_path, monkeypatch)
    mod.BT_JOBS.mkdir(parents=True, exist_ok=True)
    mod.CMP_DIR.mkdir(parents=True, exist_ok=True)
    a = flask.Flask("t")
    a.secret_key = "x"
    a.register_blueprint(mod.panel)
    return a.test_client(), mod


def _job(mod, jid, status, kind="compare_period"):
    (mod.BT_JOBS / f"{jid}.json").write_text(json.dumps({"id": jid, "kind": kind, "status": status}))
    (mod.CMP_DIR / f"{jid}.json").write_text("{}")


def test_a_finished_comparison_is_deleted_with_its_report(app):
    c, mod = app
    _job(mod, JID, "done")
    r = c.post(f"/api/compare-period/{JID}/delete")
    assert r.status_code == 200 and len(r.get_json()["removed"]) == 2       # the report and the job record
    assert not (mod.BT_JOBS / f"{JID}.json").exists() and not (mod.CMP_DIR / f"{JID}.json").exists()
    assert c.get("/api/compare-period").get_json()["jobs"] == []


def test_a_failed_comparison_can_be_deleted_too(app):
    c, mod = app
    _job(mod, JID, "failed")
    assert c.post(f"/api/compare-period/{JID}/delete").status_code == 200


def test_a_running_comparison_is_kept(app):
    c, mod = app
    _job(mod, JID, "running")
    assert c.post(f"/api/compare-period/{JID}/delete").status_code == 409
    assert (mod.BT_JOBS / f"{JID}.json").exists() and (mod.CMP_DIR / f"{JID}.json").exists()


def test_only_comparison_jobs_and_well_formed_ids_are_accepted(app):
    c, mod = app
    _job(mod, JID, "done", kind="sweep")
    assert c.post(f"/api/compare-period/{JID}/delete").status_code == 404      # a sweep is not deleted here
    assert c.post("/api/compare-period/../../etc/delete").status_code == 404
    assert c.post("/api/compare-period/nope/delete").status_code == 404
    assert (mod.BT_JOBS / f"{JID}.json").exists()


def test_live_overview_has_no_shadow_filter(app):
    c, mod = app                                                     # Shadow mode was removed (18 Nov 2026)
    html = c.get("/live/overview").get_data(as_text=True)
    assert 'id="modeFilt"' not in html and 'data-m="shadow"' not in html and "Start shadow session" not in html
