"""Several logins for the dashboard, each with access to chosen pages.

  Owner      the dashboard's own login (config.json dashboard_user / dashboard_password). Sees and
             does everything, manages the other logins on the Users page (/users), and is the only
             one who can log in to Zerodha, place live orders or change settings that affect the
             whole server. Changing the owner's password here also updates config.json.
  Others     a username and password (stored hashed in data/users.json), and for each area of the
             dashboard: no access, view, or use (view plus its buttons). New logins must choose their
             own password at their first sign-in. "Hide amounts" shows the portfolio as percentages:
             rupee and dollar amounts are scaled on the server (the real figures never leave it) and
             masked on the page.

Any page or address not listed in the rules below is owner-only, so new pages stay private until
they are added here. Every change (start, stop, upload, sign-in...) is written to data/activity.log
with the login that made it; the Users page shows the latest.

Loaded by algolab_panel.py (attach), after pwa_panel.py. If this file is missing or fails to load,
the dashboard keeps its single owner login and refuses every other login's session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Response, g, has_request_context, jsonify, redirect, render_template, request, session

IST = timezone(timedelta(hours=5, minutes=30))

# ---- areas and what each address needs -------------------------------------------------------------
# key, label, what "view" gives, what "use" adds (None: view only for other logins)
AREAS = [
    ("portfolio", "Portfolio (India)", "Holdings, stock pages, gold, history", "Import a stock's trades"),
    ("us", "US stocks (IBKR)", "US holdings and history", None),
    ("market_data", "Market data", "Stored NIFTY, VIX and option prices, downloads, live charts", None),
    ("paper", "Paper trading", "Sessions, P&L, charts, metrics, margins", "Start, stop, reset and delete paper sessions"),
    ("backtests", "Backtests", "Results", "Run, upload and delete runs"),
    ("live", "Live trading", "Shadow and live sessions and their P&L", None),
]
AREA_KEYS = [a[0] for a in AREAS]
USE_AREAS = {a[0] for a in AREAS if a[3]}
RANK = {"": 0, "none": 0, "view": 1, "use": 2}
PRESETS = {
    "family": {"label": "Family: portfolio", "access": {"portfolio": "view", "us": "view"}, "hide_amounts": True},
    "friend": {"label": "Friend: paper trading", "hide_amounts": True,
               "access": {"paper": "use", "market_data": "view", "backtests": "view"}},
}
FIRST_PAGES = ["/summary", "/", "/us", "/paper", "/live", "/market-data", "/backtests"]

GET_LIKE = ("GET", "HEAD", "OPTIONS")
PUBLIC_EXACT = {"/signin", "/signout", "/manifest.webmanifest", "/sw.js", "/offline", "/favicon.ico",
                "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/apple-touch-icon-180x180.png"}
PUBLIC_PREFIX = ("/static/", "/icons/", "/passkey/")
ALWAYS_EXACT = {"/summary", "/api/summary", "/algo", "/profile", "/profile/photo", "/api/profile", "/api/profile/photo", "/account/password",
                "/api/account", "/api/account/password", "/api/passkeys", "/api/passkey/register/options",
                "/api/passkey/register", "/api/push", "/api/push/subscribe", "/api/push/prefs", "/api/push/remove",
                "/api/push/test", "/api/chart/indices", "/api/studio-auth"}
ALWAYS_PREFIX = ("/api/passkeys/",)
OWNER_EXACT = {"/login", "/callback", "/login/paste", "/api/test-alert", "/api/sync", "/users", "/api/users",
               "/api/activity", "/live/new", "/api/algo", "/api/algo/command",   # /api/algo*: the retired Blaze engine
               "/api/server-update",                                             # update the server from GitHub
               "/api/compare-period/keep",                                       # days of 1-second bars kept
               "/api/ibkr-control", "/api/risk-defaults",                        # start / stop IBKR
               "/api/live/stop-all"}                                             # square off every LIVE session
OWNER_PREFIX = ("/api/users/", "/order-test", "/api/order-test", "/positions", "/api/positions")   # positions: real orders
VIEW = {   # GET addresses: exact paths, path prefixes
    "portfolio": ({"/", "/stock", "/gold", "/export.csv", "/api/data", "/api/live", "/api/history", "/api/stock",
                   "/api/gold"}, ()),
    "us": ({"/us", "/api/us"}, ("/api/us/",)),
    "market_data": ({"/market-data", "/chart"}, ("/api/market-data/", "/api/chart/")),
    "paper": ({"/paper", "/paper/overview"}, ()),
    "live": ({"/live", "/live/overview", "/api/live/settings"}, ()),   # POST /api/live/settings: owner
    "backtests": ({"/backtests", "/api/backtests", "/backtests/run", "/backtests/sweep", "/api/backtest-runner", "/studio/",
                   "/api/backtest-runner/finished", "/api/td-live-test", "/backtests/compare", "/api/compare-period"},
                  ("/api/backtests/", "/api/backtest-runner/job/", "/api/compare-period/report/")),
}
USE = {    # other methods (POST, DELETE): exact paths, regexes
    "portfolio": ({"/stock/import"}, ()),
    "paper": ({"/api/paper/margins"}, ()),
    "backtests": ({"/api/backtests/upload", "/api/backtest-runner", "/api/lab-agent/claim", "/api/lab-agent/status", "/api/td-live-test",
                   "/api/compare-period"},
                  (re.compile(r"/api/backtests/[^/]+/delete"), re.compile(r"/api/backtest-runner/job/[0-9a-z-]+/delete"),
                   re.compile(r"/api/compare-period/[0-9a-z-]+/delete"))),
}
PAPER_ACTIONS = {"/api/paper/start", "/api/paper/stop", "/api/paper/kill", "/api/paper/reset", "/api/paper/delete"}
PW_PATHS = {"/account/password", "/api/account/password", "/api/account", "/signout"}
HIDE_JSON = {"/api/data", "/api/live", "/api/history", "/api/stock", "/api/gold", "/api/us"}
HIDE_PAGES = {"/", "/stock", "/gold", "/us"}
NAME_OK = re.compile(r"[a-z0-9][a-z0-9._-]{2,29}")
LOG_KEYS = ("id", "strategy", "mode", "feed", "label", "cmd", "name", "paused", "symbol", "theme")


def classify(method, path, body=None, mode_of=None):
    """What an address needs: ("public",), ("always",), ("owner", why) or ("area", (areas...), level)."""
    get = method.upper() in GET_LIKE
    if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIX):
        return ("public",)
    if path in ALWAYS_EXACT or path.startswith(ALWAYS_PREFIX):
        return ("always",)
    if path in OWNER_EXACT or path.startswith(OWNER_PREFIX):
        return ("owner", "listed")
    if path == "/paper/new" and get:
        return ("area", ("paper",), "use")
    if path in ("/api/paper", "/api/paper/metrics.csv", "/api/paper/cycle", "/api/paper/ticks") and get:
        return ("area", ("paper", "live"), "view")       # the page itself lists only what may be seen
    if path == "/api/live/settings" and not get:
        return ("owner", "listed")                       # the LIVE session limit
    if path in PAPER_ACTIONS and not get:
        mode = mode_of(path, body or {}) if mode_of else "paper"
        return ("area", ("paper",), "use") if mode == "paper" else ("owner", "live orders")
    if get:
        for area, (exact, prefixes) in VIEW.items():
            if path in exact or (prefixes and path.startswith(prefixes)):
                return ("area", (area,), "view")
    else:
        for area, (exact, patterns) in USE.items():
            if path in exact or any(p.fullmatch(path) for p in patterns):
                return ("area", (area,), "use")
    return ("owner", "unlisted")


# ---- passwords ----------------------------------------------------------------------------------------
def hash_password(pw: str, iterations: int = 260_000) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = str(stored).split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", str(pw).encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(got, base64.b64decode(dk))
    except (ValueError, TypeError):
        return False


def new_password() -> str:
    words = ("river", "maple", "cedar", "lotus", "amber", "coral", "delta", "ember", "falcon", "harbor",
             "indigo", "jasmine", "kestrel", "lagoon", "meadow", "nectar", "orchid", "pebble", "quartz", "saffron")
    return f"{secrets.choice(words)}-{secrets.choice(words)}-{secrets.randbelow(9000) + 1000}"


# ---- hiding amounts ----------------------------------------------------------------------------------
SAFE_KEY = re.compile(r"(pct|percent|weight|price|avg_cost|usd_inr|fx|rate|age_min|count|seconds|strike|ver)$", re.I)
CURRENCY = re.compile(r"(₹|Rs\.?\s?|INR\s?|US\$|\$|USD\s?)\s?([−+-]?)\d[\d,]*(?:\.\d+)?"
                      r"(?:\s?(?:k|K|L|lakhs?|Cr|cr|crores?|M|mn|bn)\b)?")


def mask_text(s: str) -> str:
    return CURRENCY.sub(lambda m: m.group(1) + m.group(2) + "•••", s)


def scale_amounts(o, k, key=""):
    """Every amount multiplied by k (percentages, prices, rates and counts kept); amounts in text masked."""
    if isinstance(o, bool) or o is None:
        return o
    if isinstance(o, (int, float)):
        return o if SAFE_KEY.search(key or "") else round(o * k, 2)
    if isinstance(o, str):
        return mask_text(o)
    if isinstance(o, list):
        return [scale_amounts(v, k, key) for v in o]
    if isinstance(o, dict):
        return {kk: scale_amounts(v, k, str(kk)) for kk, v in o.items()}
    return o


MASK_JS = r"""<script id="hide-amounts">(function(){
var RE=/(₹|Rs\.?\s?|US\$|\$)\s?([−+-]?)\d[\d,]*(\.\d+)?(\s?(k|K|L|Cr|cr|M)\b)?/g, NUM=/^\s*[−+-]?[\d,]+(\.\d+)?\s*(k|K|L|Cr|cr|M)?\s*$/;
var U=/\d[\d,]*(\.\d+)?(?=\s*(units?|shares?)\b)/g;
function m(s){return s.replace(RE,"$1$2•••").replace(U,"•••").replace(/(shows\s)\d[\d,]*(\.\d+)?/g,"$1•••");}
function mc(t){t=m(String(t));return NUM.test(t)?"•••":t;}
var C=window.CanvasRenderingContext2D&&CanvasRenderingContext2D.prototype;
if(C){var f=C.fillText,k=C.strokeText;C.fillText=function(t,x,y,w){return w===undefined?f.call(this,mc(t),x,y):f.call(this,mc(t),x,y,w);};
C.strokeText=function(t,x,y,w){return w===undefined?k.call(this,mc(t),x,y):k.call(this,mc(t),x,y,w);};}
function walk(n){if(!n)return;if(n.nodeType===3){var v=n.nodeValue;if(/\d/.test(v)){var w=m(v);if(w!==v)n.nodeValue=w;}return;}
if(n.nodeType!==1||n.tagName==="SCRIPT"||n.tagName==="STYLE")return;if(n.title)n.title=m(n.title);for(var c=n.firstChild;c;c=c.nextSibling)walk(c);}
function qty(){document.querySelectorAll("table").forEach(function(t){var cols=[];t.querySelectorAll("tr").forEach(function(tr){var ths=tr.querySelectorAll("th");
if(ths.length){ths.forEach(function(th,i){if(/^\s*(qty|quantity|units|shares)\s*$/i.test(th.textContent))cols.push(i);});return;}
cols.forEach(function(i){var td=tr.children[i];if(td&&td.textContent!=="•••"&&/\d/.test(td.textContent))td.textContent="•••";});});});}
function ids(){document.querySelectorAll("#qty,#units,#shares").forEach(function(e){if(/\d/.test(e.textContent))e.textContent="•••";});}
var busy=false;function all(){if(busy)return;busy=true;try{walk(document.body);qty();ids();}finally{busy=false;}}
document.addEventListener("DOMContentLoaded",function(){all();new MutationObserver(function(){all();}).observe(document.body,{childList:true,subtree:true,characterData:true});});
})();</script>"""
NOTE_CSS = ('<style>.acc-note{max-width:1100px;margin:8px auto 0;padding:7px 12px;border-radius:8px;font:13px/1.35 system-ui,'
            '-apple-system,"Segoe UI",Roboto,sans-serif;background:rgba(47,111,237,.10);color:var(--text,#16181d)}'
            '@media (max-width:1124px){.acc-note{margin:8px 12px 0}}</style>')


def attach(P) -> None:
    """Add logins and access rules to the dashboard panel module P (algolab_panel)."""
    panel = P.panel
    base = lambda: P._profile_dir()          # noqa: E731 - data folder beside the dashboard
    cache = {"mtime": None, "users": []}
    fails_ip, fails_user = {}, {}
    process_k = random.uniform(0.3, 3.0)      # amounts scale when nothing better is known (kept secret)

    # ---- the users file -------------------------------------------------------------------------------
    def users_path():
        return base() / "users.json"

    def load_users():
        p = users_path()
        try:
            mtime = p.stat().st_mtime
        except OSError:
            return []
        if cache["mtime"] != mtime:
            data = P._json(p, {}) or {}
            cache.update(mtime=mtime, users=[u for u in data.get("users", []) if isinstance(u, dict) and u.get("name")])
        return [dict(u) for u in cache["users"]]

    def save_users(users):
        d = base()
        d.mkdir(parents=True, exist_ok=True)
        P._write(users_path(), {"users": users})
        try:
            os.chmod(users_path(), 0o600)
        except OSError:
            pass
        cache["mtime"] = None

    def owner():
        return P.owner_name()

    def is_owner(name):
        return bool(name) and name == owner()

    def find(name):
        n = str(name or "").strip().lower()
        return next((u for u in load_users() if u["name"] == n), None)

    def get_user(name):
        if is_owner(name):
            return {"name": owner(), "owner": True, "ver": 0}
        return find(name)

    def access_of(u):
        if not u:
            return {}
        if u.get("owner"):
            return {a: ("use" if a in USE_AREAS or a == "live" else "view") for a in AREA_KEYS}
        acc = u.get("access") or {}
        out = {}
        for a in AREA_KEYS:
            lvl = acc.get(a, "")
            if lvl == "use" and a not in USE_AREAS:
                lvl = "view"
            if lvl in ("view", "use"):
                out[a] = lvl
        return out

    def can_user(name, area, level="view"):
        u = get_user(name)
        if not u:
            return False
        if u.get("owner"):
            return True
        if area == "live" and level == "use":
            return False
        return RANK.get(access_of(u).get(area, ""), 0) >= RANK.get(level, 1)

    def mode_of(path, body):
        if path == "/api/paper/start":
            return body.get("mode") or "paper"
        x = P._slot(str(body.get("id", "")))
        return P._mode(x) if x else "paper"

    def allowed(name, method, path, body=None):
        c = classify(method, path, body, mode_of)
        if c[0] in ("public", "always"):
            return True
        u = get_user(name)
        if not u:
            return False
        if u.get("owner"):
            return True
        if c[0] == "owner":
            return False
        if path == "/export.csv" and u.get("hide_amounts"):
            return False
        return any(can_user(name, a, c[2]) for a in c[1])

    def me():
        if has_request_context():
            return g.get("user") or session.get("user") or ""
        return ""

    # ---- what the rest of the dashboard asks ------------------------------------------------------
    def current_user():
        return me()

    def user_dir(user=None):
        u = user or me() or owner()
        if is_owner(u):
            return base()
        return base() / "users" / re.sub(r"[^a-z0-9._-]", "_", str(u).lower())

    def path_allowed(path, method="GET"):
        if not has_request_context() or not me():
            return True
        return allowed(me(), method, path)

    def can(area, level="view"):
        if not has_request_context() or not me():
            return True
        return can_user(me(), area, level)

    def user_active(name):
        u = get_user(name)
        return bool(u) and not u.get("disabled")

    def user_version(name):
        u = get_user(name)
        return int((u or {}).get("ver", 0))

    def push_allowed(name, msg):
        if is_owner(name):
            return True
        u = get_user(name)
        if not u or u.get("disabled"):
            return False
        cat, url = msg.get("category", "trading"), str(msg.get("url") or "")
        if cat == "test":
            return True
        if cat == "logins":
            return False
        if cat == "daily":
            return can_user(name, "paper")
        if cat == "trading":
            return can_user(name, "live") if url.startswith("/live") else can_user(name, "paper")
        if cat == "alerts":
            return can_user(name, "portfolio") and not u.get("hide_amounts")
        return False

    # ---- activity log -------------------------------------------------------------------------------
    def client_ip():
        ip = request.remote_addr or ""
        if ip in ("127.0.0.1", "::1", "") and request.headers.get("X-Forwarded-For"):
            ip = request.headers["X-Forwarded-For"].split(",")[0].strip()
        return ip

    def log_activity(event, user=None, **detail):
        try:
            entry = {"ts": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"), "user": user or me() or "",
                     "event": str(event)[:120]}
            if has_request_context():
                entry["ip"] = client_ip()
            detail = {k: str(v)[:80] for k, v in detail.items() if v not in (None, "")}
            if detail:
                entry["detail"] = detail
            p = base() / "activity.log"
            p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and p.stat().st_size > 2_000_000:
                p.replace(p.with_suffix(".log.1"))
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:  # noqa: BLE001 - logging must never break a request
            print(f"activity log: {e}")

    def read_activity(limit=300, user=""):
        out = []
        for name in ("activity.log.1", "activity.log"):
            try:
                for line in (base() / name).read_text(encoding="utf-8").splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if not user or e.get("user") == user:
                        out.append(e)
            except OSError:
                continue
        return out[-limit:][::-1]

    P.current_user, P.user_dir, P.path_allowed, P.can = current_user, user_dir, path_allowed, can
    P.user_active, P.user_version, P.push_allowed, P.log_activity = user_active, user_version, push_allowed, log_activity
    P.amounts_hidden = lambda: hidden_user()
    P._USERS_LOADED = True

    # ---- sign-in --------------------------------------------------------------------------------------
    def owner_password():
        main = sys.modules.get("__main__")
        cfg = getattr(main, "CFG", None) or P._json(Path(P.__file__).resolve().parent / "config.json", {}) or {}
        return str(cfg.get("dashboard_password", ""))

    def safe_next(nxt):
        nxt = str(nxt or "/")
        return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"

    def too_many(store, key, limit):
        n, since = store.get(key, (0, 0.0))
        if time.time() - since > 900:
            store.pop(key, None)
            return False
        return n >= limit

    def failed(store, key):
        n, since = store.get(key, (0, 0.0))
        if time.time() - since > 900:
            n, since = 0, time.time()
        store[key] = (n + 1, since)

    def signin_post():
        ip = client_ip()
        uname = request.form.get("username", "").strip()
        pw = request.form.get("password", "")
        key = uname.lower()
        error = None
        if too_many(fails_ip, ip, 20) or too_many(fails_user, key, 5):
            error = "Too many wrong attempts. Try again in 15 minutes."
        else:
            rec, ok = None, False
            if uname and hmac.compare_digest(uname.encode(), owner().encode()):
                pw_owner = owner_password()
                ok = bool(pw_owner) and hmac.compare_digest(pw.encode(), pw_owner.encode())
                name = owner()
            else:
                rec = find(uname)
                ok = bool(rec) and check_password(pw, rec.get("password", ""))
                name = rec["name"] if rec else key
            if ok and rec and rec.get("disabled"):
                error = "This login is switched off. Ask the owner."
                log_activity("signin refused (switched off)", user=name)
            elif ok:
                fails_ip.pop(ip, None)
                fails_user.pop(key, None)
                session.clear()
                session["user"] = name
                session["uv"] = int((rec or {}).get("ver", 0))
                session.permanent = bool(request.form.get("remember"))
                if rec:
                    users = load_users()
                    for u in users:
                        if u["name"] == name:
                            u["last_signin"] = datetime.now(IST).strftime("%Y-%m-%d %H:%M")
                    save_users(users)
                log_activity("signin", user=name)
                nxt = safe_next(request.args.get("next"))
                if rec and rec.get("must_change"):
                    return redirect("/account/password?next=" + nxt)
                return redirect(nxt)
            else:
                failed(fails_ip, ip)
                failed(fails_user, key)
                log_activity("signin failed", user=key[:30])
                time.sleep(1)
                error = "Wrong username or password."
        return render_template("signin.html", error=error), 401

    # ---- the check on every request ---------------------------------------------------------------
    def wants_json():
        return request.path.startswith("/api/") or request.method not in GET_LIKE

    def signed_out():
        session.clear()
        if wants_json():
            return jsonify(error="not signed in"), 401
        return redirect("/signin?next=" + request.path)

    def first_page(name):
        for p in FIRST_PAGES:
            if allowed(name, "GET", p):
                return p
        return "/profile"

    def no_access(name, c):
        if wants_json():
            if c[0] == "area" and c[2] == "use" and any(can_user(name, a, "view") for a in c[1]):
                msg = f"Your login can view this but not change it. Ask {owner()} for 'use' access."
            elif c[0] == "owner" and c[1] == "live orders":
                msg = "Only the owner can start or stop shadow and live sessions."
            else:
                msg = f"Your login does not have access to this. Ask {owner()}."
            return jsonify(error=msg), 403
        if request.path == "/":
            return redirect(first_page(name))
        prof = P.load_profile()
        html = NO_ACCESS.replace("__FIRST__", first_page(name)).replace("__OWNER__", P._esc(owner()))
        html = P.apply_theme(P.add_menu(html, request.path, prof), prof)
        return Response(html, status=403, mimetype="text/html")

    def check():
        path = request.path
        if path == "/signin" and request.method == "POST":
            return signin_post()
        name = session.get("user")
        if not name:
            return None                       # the dashboard's own check sends this to the sign-in page
        u = get_user(name)
        valid = u is not None and not u.get("disabled") and int(session.get("uv", 0)) == int(u.get("ver", 0))
        if classify(request.method, path)[0] == "public":
            if not valid:
                session.clear()
            else:
                g.user = u["name"]
            return None
        if not valid:
            return signed_out()
        g.user = u["name"]
        if u.get("must_change") and path not in PW_PATHS:
            if wants_json():
                return jsonify(error="Choose your own password first (Profile, Change password).",
                               change_password=True), 403
            return redirect("/account/password?next=" + safe_next(request.full_path.rstrip("?")))
        body = request.get_json(silent=True) if path in PAPER_ACTIONS else None
        if allowed(u["name"], request.method, path, body):
            return None
        c = classify(request.method, path, body, mode_of)
        if c[0] != "area" or not request.path.startswith("/api/"):
            log_activity(f"no access: {request.method} {path}", user=u["name"])
        return no_access(u["name"], c)

    @panel.record_once
    def _install(state):
        funcs = state.app.before_request_funcs.setdefault(None, [])
        at = 1 if funcs and getattr(funcs[0], "__name__", "") == "public" else 0   # after pwa_panel's public files
        funcs.insert(at, check)

    # ---- after each request: hide amounts, view-only notes, activity ---------------------------------
    def india_k(data=None):
        main = sys.modules.get("__main__")
        try:
            inv = float(((getattr(main, "STATE", None) or {}).get("summary") or {}).get("invested") or 0)
        except (TypeError, ValueError):
            inv = 0.0
        if not inv and isinstance(data, dict):
            inv = float(((data.get("summary") or {}).get("invested")) or 0)
        return 100_000.0 / inv if inv > 0 else process_k

    def us_k(data):
        inv = 0.0
        if isinstance(data, dict):
            for p in data.get("positions") or []:
                try:
                    inv += float(p.get("qty") or 0) * float(p.get("avg_cost") or 0)
                except (TypeError, ValueError, AttributeError):
                    pass
        return 10_000.0 / inv if inv > 0 else process_k

    def hidden_user():
        u = get_user(me()) if me() else None
        return bool(u and not u.get("owner") and u.get("hide_amounts"))

    def hide_response(resp):
        path = request.path
        if resp.mimetype == "text/event-stream":
            def wrapped(chunks):
                for chunk in chunks:
                    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
                    lines = []
                    for line in text.split("\n"):
                        if line.startswith("data: "):
                            try:
                                obj = json.loads(line[6:])
                                line = "data: " + json.dumps(scale_amounts(obj, india_k(obj)))
                            except ValueError:
                                line = "data: {}"
                        lines.append(line)
                    yield "\n".join(lines).encode("utf-8")
            resp.response = wrapped(resp.response)
            return resp
        if resp.is_json:
            data = resp.get_json(silent=True)
            if data is not None:
                k = us_k(data) if path.startswith("/api/us") else india_k(data)
                resp.set_data(json.dumps(scale_amounts(data, k)))
        return resp

    @panel.after_app_request
    def _after(resp):
        try:
            if me() and request.path in HIDE_JSON and hidden_user():
                resp = hide_response(resp)
            if me() and request.method not in GET_LIKE and resp.status_code < 400 and not request.path.startswith(
                    ("/signin", "/api/users", "/api/account/password", "/passkey/", "/api/push/prefs")):
                detail = {}
                body = request.get_json(silent=True) if request.is_json else None
                src = body if isinstance(body, dict) else request.form
                for k in LOG_KEYS:
                    if k in src and not isinstance(src.get(k), (dict, list)):
                        detail[k] = src.get(k)
                log_activity(f"{request.method} {request.path}", **detail)
        except Exception as e:  # noqa: BLE001 - never break a page
            print(f"users_panel after-request: {e}")
        return resp

    def page_notes(html, path):
        if not me() or is_owner(me()):
            return html
        note, css = "", ""
        if path in HIDE_PAGES:      # the owner's controls on the portfolio pages
            css = "#pauseBtn,#pauseBox,#testBtn,#loginBox{display:none !important}"
            if not path_allowed("/export.csv"):
                css += 'a[href="/export.csv"]{display:none !important}'
            if not can("portfolio", "use"):
                css += '.card:has(form[action="/stock/import"]){display:none !important}'
        if path in HIDE_PAGES and hidden_user():
            html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + MASK_JS, html, count=1)
            note = "Amounts are hidden for your login: figures show as ••• and percentages are real."
        elif path in ("/paper", "/live") and not can(path[1:], "use"):
            html = html.replace("</head>", "<style>.sess button[data-act]{display:none !important}</style></head>", 1)
            note = "View only: you can follow these sessions but not start or stop them."
        if css:
            html = html.replace("</head>", f"<style>{css}</style></head>", 1)
        if note and "</nav>" in html:
            html = html.replace("</head>", NOTE_CSS + "</head>", 1)
            html = html.replace("</nav>", f'</nav><div class="acc-note">{note}</div>', 1)
        return html

    P.HTML_FILTERS.append(page_notes)

    # ---- Algo Studio (studio/, a second app on this server) asks who is signed in -------------------------
    @panel.route("/api/studio-auth")
    def api_studio_auth():
        """The signed-in person and what they may see or use: Algo Studio forwards the browser's cookie here."""
        u = get_user(me())
        if not u:
            return jsonify(error="not signed in"), 401
        return jsonify(user=u["name"], owner=bool(u.get("owner")), access=access_of(u),
                       live_use=can_user(u["name"], "live", "use"))

    # ---- the signed-in person's account ---------------------------------------------------------------
    @panel.route("/api/account")
    def api_account():
        u = get_user(me()) or {}
        return jsonify(user=me(), owner=is_owner(me()), hide_amounts=bool(u.get("hide_amounts")) and not is_owner(me()),
                       must_change=bool(u.get("must_change")), access=access_of(u))

    @panel.route("/account/password")
    def account_password_page():
        return Response(PASSWORD_PAGE, mimetype="text/html")

    @panel.route("/api/account/password", methods=["POST"])
    def api_account_password():
        body = request.get_json(silent=True) or {}
        cur, new = str(body.get("current", "")), str(body.get("new", ""))
        name = me()
        if len(new) < 8:
            return jsonify(error="Use at least 8 characters."), 400
        if new == cur:
            return jsonify(error="Choose a different password from the current one."), 400
        if is_owner(name):
            if not hmac.compare_digest(cur.encode(), owner_password().encode()):
                return jsonify(error="The current password is not right."), 400
            cfg_path = Path(P.__file__).resolve().parent / "config.json"
            try:
                cfg = json.loads(cfg_path.read_text())
                cfg["dashboard_password"] = new
                tmp = cfg_path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(cfg, indent=2))
                os.chmod(tmp, cfg_path.stat().st_mode & 0o777)
                tmp.replace(cfg_path)
            except (OSError, ValueError) as e:
                return jsonify(error=f"Could not save it in config.json: {e}"), 500
            main = sys.modules.get("__main__")
            if isinstance(getattr(main, "CFG", None), dict):
                main.CFG["dashboard_password"] = new
            log_activity("password changed")
            return jsonify(ok=True, note="Also use the new password in the Mac's secrets.yaml (backtest uploads).")
        users = load_users()
        rec = next((u for u in users if u["name"] == name), None)
        if not rec or not check_password(cur, rec.get("password", "")):
            return jsonify(error="The current password is not right."), 400
        rec.update(password=hash_password(new), must_change=False, ver=int(rec.get("ver", 0)) + 1)
        save_users(users)
        session["uv"] = rec["ver"]            # this device stays signed in; other devices sign in again
        log_activity("password changed")
        return jsonify(ok=True)

    # ---- the owner's Users page ---------------------------------------------------------------------
    def public_user(u):
        return {"name": u["name"], "label": u.get("label", ""), "access": access_of(u),
                "hide_amounts": bool(u.get("hide_amounts")), "disabled": bool(u.get("disabled")),
                "must_change": bool(u.get("must_change")), "created": u.get("created", ""),
                "last_signin": u.get("last_signin", ""), "passkeys": P.passkey_count(u["name"])
                if hasattr(P, "passkey_count") else 0}

    @panel.route("/users")
    def users_page():
        return Response(USERS_PAGE, mimetype="text/html")

    @panel.route("/api/users", methods=["GET", "POST"])
    def api_users():
        if request.method == "GET":
            return jsonify(owner=owner(), users=[public_user(u) for u in load_users()], presets=PRESETS,
                           areas=[{"key": a, "label": l, "view": v, "use": x} for a, l, v, x in AREAS])
        body = request.get_json(silent=True) or {}
        name = str(body.get("name", "")).strip().lower()
        users = load_users()
        rec = next((u for u in users if u["name"] == name), None)
        creating = bool(body.get("new"))
        if creating:
            if not NAME_OK.fullmatch(name):
                return jsonify(error="Username: 3 to 30 characters, lower-case letters, digits, dot, dash or underscore."), 400
            if rec or name == owner().lower():
                return jsonify(error="That username is taken."), 400
            rec = {"name": name, "created": datetime.now(IST).strftime("%Y-%m-%d %H:%M"), "ver": 0}
            users.append(rec)
        elif not rec:
            return jsonify(error="No such login."), 404
        access = body.get("access") or {}
        rec["access"] = {a: lvl for a, lvl in access.items()
                         if a in AREA_KEYS and lvl in ("view", "use") and (lvl == "view" or a in USE_AREAS)}
        rec["label"] = re.sub(r"\s+", " ", str(body.get("label") or "")).strip()[:60]
        rec["hide_amounts"] = bool(body.get("hide_amounts"))
        was_disabled = bool(rec.get("disabled"))
        rec["disabled"] = bool(body.get("disabled"))
        pw = str(body.get("password") or "")
        if creating and len(pw) < 8:
            return jsonify(error="Give a temporary password of at least 8 characters."), 400
        if pw:
            if len(pw) < 8:
                return jsonify(error="The password needs at least 8 characters."), 400
            rec["password"] = hash_password(pw)
            rec["must_change"] = bool(body.get("must_change", True))
            if not creating:
                rec["ver"] = int(rec.get("ver", 0)) + 1          # signs the login out everywhere
        if rec["disabled"] and not was_disabled:
            rec["ver"] = int(rec.get("ver", 0)) + 1
        save_users(users)
        what = "login created" if creating else ("password reset" if pw else "login updated")
        log_activity(what, login=name, access=",".join(f"{a}:{l}" for a, l in rec["access"].items()) or "none",
                     hidden="yes" if rec["hide_amounts"] else "no", off="yes" if rec["disabled"] else "")
        return jsonify(ok=True, user=public_user(rec))

    @panel.route("/api/users/<name>/delete", methods=["POST"])
    def api_user_delete(name):
        users = load_users()
        keep = [u for u in users if u["name"] != name]
        if len(keep) == len(users):
            return jsonify(error="No such login."), 404
        save_users(keep)
        if hasattr(P, "remove_user_keys"):
            P.remove_user_keys(name)
        shutil.rmtree(user_dir(name), ignore_errors=True)
        log_activity("login deleted", login=name)
        return jsonify(ok=True)

    @panel.route("/api/users/<name>/passkeys/remove", methods=["POST"])
    def api_user_passkeys_remove(name):
        if not find(name):
            return jsonify(error="No such login."), 404
        n = P.remove_user_keys(name) if hasattr(P, "remove_user_keys") else 0
        log_activity("Face ID keys removed", login=name, count=n)
        return jsonify(ok=True, removed=n)

    @panel.route("/api/users/password")
    def api_users_new_password():
        return jsonify(password=new_password())

    @panel.route("/api/activity")
    def api_activity():
        return jsonify(events=read_activity(int(request.args.get("limit", 300) or 300), request.args.get("user", "")))


# ---- pages ---------------------------------------------------------------------------------------------
_CSS = r"""<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#16181d;--muted:#646b78;--line:#e5e7eb;--up:#087a4c;--down:#c8323a;--accent:#2f6fed}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--text:#e8eaee;--muted:#8b93a1;--line:#272b35;--up:#34c98a;--down:#f0626a;--accent:#6c9bff}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 14px}
h2{font-size:15px;margin:0 0 12px;color:var(--muted);font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:12px}
.sub{color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:12px 0 4px}
input[type=text],input[type=password],select{width:100%;padding:9px 11px;border:1px solid var(--line);border-radius:8px;background:transparent;color:var(--text);font:inherit}
.btn{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:9px 16px;font:inherit;font-size:14px;cursor:pointer;text-decoration:none;display:inline-block}
.btn.ghost{background:transparent;color:var(--accent);border:1px solid var(--line)}
.btn.stop{background:transparent;color:var(--down);border:1px solid var(--line)}
.btn:disabled{opacity:.5}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
.msg{font-size:13px}.msg.ok{color:var(--up)}.msg.err{color:var(--down)}
.hide{display:none}
.chk{display:flex;gap:10px;align-items:flex-start;padding:7px 0;font-size:14px;cursor:pointer;margin:0;color:var(--text)}
.chk input{margin-top:3px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px}
.scroll{overflow-x:auto}
.pill{display:inline-block;font-size:11px;padding:2px 7px;border-radius:999px;background:var(--bg);border:1px solid var(--line);margin:1px 2px 1px 0;white-space:nowrap}
.pill.use{border-color:var(--accent);color:var(--accent)}
.pill.off{color:var(--down);border-color:var(--down)}
.areas{display:grid;grid-template-columns:1fr auto;gap:6px 12px;align-items:center}
.areas .seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.areas .seg button{border:0;background:transparent;color:var(--text);padding:7px 11px;font:inherit;font-size:13px;cursor:pointer}
.areas .seg button.on{background:var(--accent);color:#fff}
.areas .seg button:disabled{opacity:.35;cursor:default}
.areas .nm b{font-weight:600;font-size:14px}.areas .nm div{font-size:12px;color:var(--muted)}
.pw{font:15px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--bg);border:1px dashed var(--line);border-radius:8px;padding:8px 10px;word-break:break-all}
.ev{font-size:13px}.ev td{padding:6px}
@media (max-width:560px){.areas{grid-template-columns:1fr}.areas .seg{justify-self:start;margin-bottom:6px}}
</style>"""

NO_ACCESS = ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>No access · Portfolio</title>""" + _CSS + """
</head><body><div class="wrap"><div class="card"><h1>No access</h1>
<p class="sub">Your login does not have access to this page. Ask __OWNER__ if you need it.</p>
<div class="row"><a class="btn" href="__FIRST__">Go to your pages</a><a class="btn ghost" href="/profile">Profile</a></div>
</div></div></body></html>""")

PASSWORD_PAGE = ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Password · Portfolio</title>""" + _CSS + """
</head><body><div class="wrap" style="max-width:520px">
<h1>Change password</h1>
<div class="card">
  <div class="sub" id="why">Choose a password only you know (at least 8 characters).</div>
  <form id="f" autocomplete="on">
    <input type="text" name="username" id="username" autocomplete="username" class="hide" readonly>
    <label for="cur">Current password</label><input type="password" id="cur" autocomplete="current-password" required>
    <label for="new">New password</label><input type="password" id="new" autocomplete="new-password" minlength="8" required>
    <label for="new2">New password again</label><input type="password" id="new2" autocomplete="new-password" minlength="8" required>
    <div class="row"><button class="btn" type="submit" id="go">Save password</button><span class="msg" id="msg"></span></div>
  </form>
</div>
<div class="row"><a class="btn ghost" href="/profile" id="back">Back to Profile</a><a class="btn stop" href="/signout">Sign out</a></div>
</div>
<script>
const $ = id => document.getElementById(id);
const next = new URLSearchParams(location.search).get("next") || "/";
fetch("/api/account", {cache: "no-store"}).then(r => r.json()).then(a => {
  $("username").value = a.user || "";
  if (a.must_change) { $("why").textContent = "Welcome! Before you start, replace the temporary password you were given with one only you know (at least 8 characters)."; $("back").classList.add("hide"); }
}).catch(() => {});
$("f").onsubmit = async e => {
  e.preventDefault();
  const msg = $("msg");
  if ($("new").value !== $("new2").value) { msg.className = "msg err"; msg.textContent = "The two new passwords are different."; return; }
  $("go").disabled = true;
  try {
    const r = await fetch("/api/account/password", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({current: $("cur").value, new: $("new").value})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    msg.className = "msg ok"; msg.textContent = "Saved." + (j.note ? " " + j.note : "");
    setTimeout(() => { location.href = next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/account") ? next : "/"; }, j.note ? 3000 : 900);
  } catch (err) { msg.className = "msg err"; msg.textContent = err.message; }
  $("go").disabled = false;
};
</script></body></html>""")

USERS_PAGE = ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Users · Portfolio</title>""" + _CSS + """
</head><body><div class="wrap">
<h1>Users and access</h1>
<div class="card">
  <div class="sub">You (<b id="ownerName"></b>) are the owner: full access, Zerodha login and live trading. Other logins see only
  the areas you give them. <b>View</b> lets them look; <b>Use</b> also lets them press that area's buttons.</div>
  <div class="scroll" style="margin-top:10px"><table id="list"></table></div>
  <div class="row"><button class="btn" id="add" type="button">Add a login</button></div>
</div>

<div class="card hide" id="ed">
  <h2 id="edTitle">New login</h2>
  <div class="row" style="margin-top:0" id="presetRow"></div>
  <label for="uname">Username</label><input type="text" id="uname" autocapitalize="none" autocomplete="off" spellcheck="false" placeholder="e.g. asha">
  <label for="ulabel">Who is this? (for you)</label><input type="text" id="ulabel" maxlength="60" placeholder="e.g. Asha (sister)">
  <label>Access</label>
  <div class="areas" id="areas"></div>
  <label class="chk"><input type="checkbox" id="hide"> <span>Hide amounts on the portfolio pages<br><span class="sub">Shows percentages and charts' shape; rupee and dollar figures show as •••.</span></span></label>
  <label class="chk"><input type="checkbox" id="off"> <span>Switched off<br><span class="sub">Cannot sign in; signed out everywhere at once.</span></span></label>
  <label id="pwLabel">Temporary password</label>
  <div class="row" style="margin-top:0"><input type="text" id="upw" autocomplete="off" spellcheck="false" style="flex:1;min-width:180px" placeholder="leave empty to keep the current one">
    <button class="btn ghost" id="gen" type="button">Make one</button></div>
  <div class="sub">They choose their own password at the first sign-in. Send the temporary one privately.</div>
  <div class="row"><button class="btn" id="save" type="button">Save</button><button class="btn ghost" id="cancel" type="button">Cancel</button><span class="msg" id="msg"></span></div>
  <div class="row hide" id="danger">
    <button class="btn stop" id="rmKeys" type="button">Remove their Face ID keys and notifications</button>
    <button class="btn stop" id="del" type="button">Delete this login</button></div>
  <div class="hide" id="shareBox" style="margin-top:12px"><div class="sub">Send this to them (once):</div><div class="pw" id="share"></div></div>
</div>

<div class="card">
  <h2>Activity</h2>
  <div class="row" style="margin-top:0"><select id="who" style="max-width:240px"><option value="">Everyone</option></select></div>
  <div class="scroll" style="margin-top:8px"><table class="ev" id="events"></table></div>
</div>
</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
let D = null, cur = null, acc = {}, armed = 0;
async function api(url, opts) {
  const r = await fetch(url, Object.assign({cache: "no-store", headers: {"Content-Type": "application/json"}}, opts || {}));
  if (r.status === 401) { location.href = "/signin?next=/users"; throw new Error("sign in"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
const LBL = a => (D.areas.find(x => x.key === a) || {label: a}).label;
function summary(u) {
  const a = Object.entries(u.access);
  return (a.length ? a.map(([k, l]) => `<span class="pill ${l === "use" ? "use" : ""}">${esc(LBL(k).split(" (")[0])}${l === "use" ? " · use" : ""}</span>`).join("") : '<span class="sub">no pages</span>')
    + (u.hide_amounts ? '<span class="pill">amounts hidden</span>' : "") + (u.disabled ? '<span class="pill off">switched off</span>' : "");
}
async function load() {
  D = await api("/api/users");
  $("ownerName").textContent = D.owner;
  $("list").innerHTML = "<tr><th>Login</th><th>Access</th><th>Last sign-in</th><th></th></tr>" +
    (D.users.length ? D.users.map(u => `<tr><td><b>${esc(u.name)}</b><div class="sub">${esc(u.label)}</div></td><td>${summary(u)}</td>
      <td class="sub">${esc(u.last_signin || (u.must_change ? "not yet" : "–"))}${u.passkeys ? "<br>Face ID on " + u.passkeys + " device(s)" : ""}</td>
      <td><button class="btn ghost" data-edit="${esc(u.name)}" type="button">Edit</button></td></tr>`).join("")
      : '<tr><td colspan="4" class="sub">No other logins yet.</td></tr>');
  document.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => edit(D.users.find(u => u.name === b.dataset.edit)));
  const who = $("who"), keep = who.value;
  who.innerHTML = '<option value="">Everyone</option>' + [D.owner].concat(D.users.map(u => u.name)).map(n => `<option>${esc(n)}</option>`).join("");
  who.value = keep;
  loadEvents();
}
function drawAreas() {
  $("areas").innerHTML = D.areas.map(a => {
    const lv = acc[a.key] || "";
    const b = (v, t, dis) => `<button type="button" data-a="${a.key}" data-v="${v}" class="${lv === v ? "on" : ""}" ${dis ? "disabled" : ""}>${t}</button>`;
    return `<div class="nm"><b>${esc(a.label)}</b><div>${esc(a.view)}${a.use ? " · Use: " + esc(a.use) : ""}${a.key === "live" ? " · placing live orders stays yours" : ""}</div></div>
      <div class="seg">${b("", "None")}${b("view", "View")}${b("use", "Use", !a.use)}</div>`;
  }).join("");
  document.querySelectorAll("#areas button").forEach(x => x.onclick = () => { acc[x.dataset.a] = x.dataset.v; if (!x.dataset.v) delete acc[x.dataset.a]; drawAreas(); });
}
function edit(u) {
  cur = u || null; acc = Object.assign({}, u ? u.access : {});
  $("ed").classList.remove("hide"); $("shareBox").classList.add("hide"); $("msg").textContent = "";
  $("edTitle").textContent = u ? "Edit " + u.name : "New login";
  $("uname").value = u ? u.name : ""; $("uname").disabled = !!u;
  $("ulabel").value = u ? u.label : ""; $("hide").checked = u ? u.hide_amounts : true; $("off").checked = u ? u.disabled : false;
  $("upw").value = ""; $("pwLabel").textContent = u ? "Reset password (optional)" : "Temporary password";
  $("upw").placeholder = u ? "leave empty to keep their password" : "at least 8 characters";
  $("danger").classList.toggle("hide", !u);
  $("presetRow").innerHTML = Object.entries(D.presets).map(([k, p]) => `<button class="btn ghost" type="button" data-p="${k}">${esc(p.label)}</button>`).join("");
  document.querySelectorAll("[data-p]").forEach(b => b.onclick = () => { const p = D.presets[b.dataset.p]; acc = Object.assign({}, p.access); $("hide").checked = !!p.hide_amounts; drawAreas(); });
  drawAreas();
  if (!u) gen();
  $("ed").scrollIntoView({behavior: "smooth", block: "start"});
}
async function gen() { $("upw").value = (await api("/api/users/password")).password; }
$("gen").onclick = gen;
$("add").onclick = () => edit(null);
$("cancel").onclick = () => { $("ed").classList.add("hide"); };
$("save").onclick = async () => {
  const msg = $("msg"), pw = $("upw").value.trim();
  try {
    const j = await api("/api/users", {method: "POST", body: JSON.stringify({new: !cur, name: cur ? cur.name : $("uname").value.trim().toLowerCase(),
      label: $("ulabel").value, access: acc, hide_amounts: $("hide").checked, disabled: $("off").checked, password: pw})});
    msg.className = "msg ok"; msg.textContent = "Saved.";
    if (pw) { $("share").textContent = `${location.origin}  ·  username: ${j.user.name}  ·  temporary password: ${pw}`; $("shareBox").classList.remove("hide"); }
    cur = j.user; $("uname").disabled = true; $("danger").classList.remove("hide"); $("upw").value = "";
    await load();
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; }
};
$("rmKeys").onclick = async () => {
  try { const j = await api(`/api/users/${encodeURIComponent(cur.name)}/passkeys/remove`, {method: "POST"});
        $("msg").className = "msg ok"; $("msg").textContent = `Removed ${j.removed} Face ID key(s) and their notifications.`; load(); }
  catch (e) { $("msg").className = "msg err"; $("msg").textContent = e.message; }
};
$("del").onclick = async () => {
  if (Date.now() - armed > 5000) { armed = Date.now(); $("del").textContent = "Tap again to delete " + cur.name; return; }
  try { await api(`/api/users/${encodeURIComponent(cur.name)}/delete`, {method: "POST"}); $("ed").classList.add("hide"); $("del").textContent = "Delete this login"; load(); }
  catch (e) { $("msg").className = "msg err"; $("msg").textContent = e.message; }
};
async function loadEvents() {
  const j = await api("/api/activity?limit=200&user=" + encodeURIComponent($("who").value));
  $("events").innerHTML = "<tr><th>When (IST)</th><th>Who</th><th>What</th></tr>" + (j.events.length ? j.events.map(e =>
    `<tr><td class="sub" style="white-space:nowrap">${esc(e.ts)}</td><td>${esc(e.user)}</td><td>${esc(e.event)}${e.detail ? ' <span class="sub">' +
      esc(Object.entries(e.detail).map(([k, v]) => k + "=" + v).join(" · ")) + "</span>" : ""}${e.ip ? ' <span class="sub">· ' + esc(e.ip) + "</span>" : ""}</td></tr>`).join("")
    : '<tr><td colspan="3" class="sub">Nothing yet.</td></tr>');
}
$("who").onchange = loadEvents;
load().catch(e => { document.querySelector(".wrap").insertAdjacentHTML("afterbegin", `<div class="card msg err">${esc(e.message)}</div>`); });
</script></body></html>""")
