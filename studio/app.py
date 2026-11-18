"""Algo Studio's web app: the pages (studio/static) and their small API under /studio/api/.

Every request is checked with the dashboard's sign-in (/api/studio-auth, the browser's cookie forwarded): not signed in
-> the dashboard's sign-in page; no Algo Lab access -> 403. Reading needs Algo Lab "view", saving presets "use".

    python -m studio            (service algolab-studio: 127.0.0.1:8020, behind https://<address>/studio/)
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

from flask import Flask, jsonify, redirect, request, send_from_directory

from . import analysis, data, results, share

STATIC = Path(__file__).resolve().parent / "static"
AUTH_TTL = 20.0                 # seconds a cookie's check is remembered


def dashboard_auth(cookie: str) -> dict | None:
    """Ask the dashboard who this cookie belongs to: {user, owner, access, live_use} or None."""
    import requests
    base = os.environ.get("STUDIO_DASHBOARD", "http://127.0.0.1:5010").rstrip("/")
    try:
        r = requests.get(base + "/api/studio-auth", headers={"Cookie": cookie}, timeout=5, allow_redirects=False)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except ValueError:
        return None


def create_app(auth=None) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["AUTH"] = auth or dashboard_auth
    cache: dict = {}

    def who() -> dict | None:
        cookie = request.headers.get("Cookie", "")
        if not cookie:
            return None
        key = hashlib.sha256(cookie.encode()).hexdigest()
        hit = cache.get(key)
        if hit and time.time() - hit[0] < AUTH_TTL:
            return hit[1]
        me = app.config["AUTH"](cookie)
        cache[key] = (time.time(), me)
        if len(cache) > 500:
            cache.clear()
        return me

    def can(me: dict, level: str = "view") -> bool:
        if not me:
            return False
        if me.get("owner"):
            return True
        lvl = (me.get("access") or {}).get("backtests", "")
        return lvl == "use" or (level == "view" and lvl == "view")

    @app.before_request
    def gate():
        me = who()
        api = request.path.startswith("/studio/api/")
        if not me:
            return (jsonify(error="not signed in"), 401) if api else redirect("/signin?next=" + request.path)
        if not can(me, "use" if request.method == "POST" else "view"):
            msg = "Your login does not have Algo Lab access." if request.method != "POST" else \
                "Your login can look at Algo Studio but not change it."
            return (jsonify(error=msg), 403) if api else (msg, 403)
        request.me = me
        return None

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/studio/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---- pages ----------------------------------------------------------------------------------------------
    @app.route("/studio")
    def studio_root():
        return redirect("/studio/")

    @app.route("/studio/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.route("/studio/static/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name)

    # ---- API -----------------------------------------------------------------------------------------------
    @app.route("/studio/api/me")
    def api_me():
        me = request.me
        return jsonify(user=me.get("user"), owner=bool(me.get("owner")), can_use=can(me, "use"),
                       live_use=bool(me.get("live_use")))

    @app.route("/studio/api/strategies")
    def api_strategies():
        ss, rr = data.sessions(), data.runs()
        return jsonify(strategies=[data.summary(c, ss, rr) for c in data.catalog()])

    @app.route("/studio/api/strategies/<name>")
    def api_strategy(name):
        entry = next((c for c in data.catalog() if c["name"] == name), None)
        if entry is None:
            return jsonify(error="No such strategy."), 404
        ss, rr = data.sessions(), data.runs(name)
        pre = sorted(data.presets(name).items(), key=lambda kv: kv[1].get("created", ""))
        mine = [x for x in ss if x["strategy"] == name]
        summ = data.summary(entry, ss, rr)
        return jsonify(strategy=summ, catalog=entry, presets=[{"id": k, **v} for k, v in pre],
                       runs=rr[:50], sweeps=data.sweeps(name), sessions=mine,
                       gates=analysis.gates(entry, summ["stage"], rr, [x for x in mine if not x.get("sim")], analysis.comparisons()))

    @app.route("/studio/api/strategies/<name>/analysis")
    def api_analysis(name):
        entry = next((c for c in data.catalog() if c["name"] == name), None)
        if entry is None:
            return jsonify(error="No such strategy."), 404
        try:
            days = max(3, min(60, int(request.args.get("days") or 10)))
        except ValueError:
            days = 10
        return jsonify(analysis.analysis(entry, request.args.get("preset") or None, days))

    @app.route("/studio/api/compare", methods=["POST"])
    def api_compare():
        """Analysis › Compare: backtest a paper or shadow session's last days and compare (server's backtest runner)."""
        b = request.get_json(silent=True) or {}
        try:
            job = analysis.queue_compare(str(b.get("strategy") or ""), str(b.get("session") or ""), b.get("days") or 14,
                                         request.me.get("user") or "")
        except LookupError as e:
            return jsonify(error=str(e)), 404
        except (ValueError, TypeError) as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, job=job)

    @app.route("/studio/api/presets", methods=["POST"])
    def api_presets():
        b = request.get_json(silent=True) or {}
        try:
            pid, p = data.save_preset(str(b.get("strategy") or ""), b.get("name"), b.get("params") or {},
                                      str(b.get("id") or ""), str(b.get("source") or ""), request.me.get("user") or "")
        except LookupError as e:
            return jsonify(error=str(e)), 404
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, id=pid, preset=p)

    @app.route("/studio/api/presets/from-run", methods=["POST"])
    def api_presets_from_run():
        """Save the settings a backtest ran with as a preset (Backtests tab › Save as preset)."""
        b = request.get_json(silent=True) or {}
        try:
            strategy, params = data.run_settings(str(b.get("run") or ""))
            pid, p = data.save_preset(strategy, b.get("name"), params, "", "From backtest " + str(b.get("run"))[:40],
                                      request.me.get("user") or "")
        except LookupError as e:
            return jsonify(error=str(e)), 404
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, id=pid, preset=p, strategy=strategy)

    # ---- Share and Import (studio/share.py) -------------------------------------------------------------
    def _err(fn):
        try:
            return jsonify({"ok": True, **fn()})
        except LookupError as e:
            return jsonify(error=str(e)), 404
        except ValueError as e:
            return jsonify(error=str(e)), 400

    @app.route("/studio/api/share", methods=["POST"])
    def api_share():
        b = request.get_json(silent=True) or {}
        return _err(lambda: share.make(str(b.get("strategy") or ""), str(b.get("preset") or ""), request.me.get("user") or "",
                                       bool(b.get("backtest", True)), bool(b.get("results")), str(b.get("to") or "")))

    @app.route("/studio/api/shares/<name>")
    def api_shares(name):
        return jsonify(shares=share.shares(name))

    @app.route("/studio/api/shares/delete", methods=["POST"])
    def api_shares_delete():
        b = request.get_json(silent=True) or {}
        return _err(lambda: (share.forget(str(b.get("id") or "")), {})[1])

    @app.route("/studio/api/import/preview", methods=["POST"])
    def api_import_preview():
        b = request.get_json(silent=True) or {}
        return _err(lambda: share.preview(str(b.get("code") or "")))

    @app.route("/studio/api/import", methods=["POST"])
    def api_import():
        b = request.get_json(silent=True) or {}

        def run():
            pid, p = share.take(str(b.get("code") or ""), "update" if b.get("how") == "update" else "new",
                                str(b.get("name") or ""), request.me.get("user") or "")
            return {"id": pid, "strategy": p["strategy"], "name": p["name"]}
        return _err(run)

    # ---- Strategy Developer (algolab/built.py) ---------------------------------------------------------
    @app.route("/studio/api/builder")
    def api_builder():
        from algolab import built
        return jsonify(templates=built.TEMPLATES, entry=built.ENTRY_DEFAULT, exit=built.EXIT_DEFAULT, leg=built.LEG_DEFAULT,
                       max_legs=built.MAX_LEGS)

    @app.route("/studio/api/built/<name>")
    def api_built_get(name):
        from algolab import built
        s = built.load_specs().get(name)
        return jsonify(spec=s) if s else (jsonify(error="No such strategy."), 404)

    @app.route("/studio/api/built/check", methods=["POST"])
    def api_built_check():
        from algolab import built
        b = request.get_json(silent=True) or {}
        try:
            s = built.validate(b.get("spec") or {})
        except ValueError as e:
            return jsonify(ok=False, error=str(e))
        return jsonify(ok=True, describe=built.describe(s))

    @app.route("/studio/api/built", methods=["POST"])
    def api_built_save():
        from algolab import built
        from algolab.paper import write_catalog
        b = request.get_json(silent=True) or {}
        name = str(b.get("name") or "")
        if name and name not in built.load_specs():
            return jsonify(error="No such strategy."), 404
        try:
            s = built.save_spec(b.get("spec") or {}, name or None)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        write_catalog()                               # the dashboard's pages and Studio list it from now on
        return jsonify(ok=True, name=s["name"], spec=s)

    @app.route("/studio/api/built/delete", methods=["POST"])
    def api_built_delete():
        from algolab import built
        from algolab.paper import write_catalog
        name = str((request.get_json(silent=True) or {}).get("name") or "")
        if any(x["strategy"] == name and (x["running"] or x["wanted"]) for x in data.sessions()):
            return jsonify(error="A session of this strategy is running: stop it first."), 400
        try:
            built.delete_spec(name)
        except LookupError as e:
            return jsonify(error=str(e)), 404
        write_catalog()
        return jsonify(ok=True)

    # ---- Results (studio/results.py) -----------------------------------------------------------------------
    @app.route("/studio/api/results")
    def api_results():
        a = request.args
        try:
            days = max(0, min(3650, int(a.get("days") or 0)))
        except ValueError:
            days = 0
        kind = a.get("kind") if a.get("kind") in ("all", "backtest", "compare", "sweep") else "all"
        sort = a.get("sort") if a.get("sort") in ("new", "net", "retdd") else "new"
        return jsonify(results.overview(a.get("strategy") or None, kind, days, sort, a.get("archived") == "1",
                                        a.get("preset") if "preset" in a else None))

    @app.route("/studio/api/run/<name>")
    def api_run(name):
        try:
            return jsonify(results.run_detail(name))
        except LookupError as e:
            return jsonify(error=str(e)), 404

    @app.route("/studio/api/compare-runs")
    def api_compare_runs():
        try:
            return jsonify(results.compare_runs((request.args.get("runs") or "").split(",")))
        except LookupError as e:
            return jsonify(error=str(e)), 404
        except ValueError as e:
            return jsonify(error=str(e)), 400

    @app.route("/studio/api/sweep/<jid>")
    def api_sweep(jid):
        try:
            return jsonify(results.sweep_detail(jid))
        except LookupError as e:
            return jsonify(error=str(e)), 404

    @app.route("/studio/api/results/ref", methods=["POST"])
    def api_results_ref():
        b = request.get_json(silent=True) or {}
        try:
            return jsonify(ok=True, **results.set_ref(str(b.get("run") or ""), bool(b.get("on", True))))
        except LookupError as e:
            return jsonify(error=str(e)), 404

    @app.route("/studio/api/results/archive", methods=["POST"])
    def api_results_archive():
        b = request.get_json(silent=True) or {}
        names = [str(x) for x in (b.get("runs") or [])][:500]
        return jsonify(ok=True, changed=results.set_archived(names, bool(b.get("on", True))))

    @app.route("/studio/api/presets/delete", methods=["POST"])
    def api_presets_delete():
        b = request.get_json(silent=True) or {}
        try:
            data.delete_preset(str(b.get("strategy") or ""), str(b.get("id") or ""))
        except LookupError as e:
            return jsonify(error=str(e)), 404
        return jsonify(ok=True)

    return app


def main() -> None:
    app = create_app()
    port = int(os.environ.get("STUDIO_PORT", "8020"))
    try:
        from waitress import serve
    except ImportError:
        app.run(host="127.0.0.1", port=port)
    else:
        serve(app, host="127.0.0.1", port=port, threads=8)
