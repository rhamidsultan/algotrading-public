"""Algo Studio › Share and Import: a strategy preset passed between friends as a share code (or a small file).

A share code carries the strategy's name and one preset's settings (its own settings and Orders & stops), who shared
it, and, if chosen, a summary of the sharer's backtest and a snapshot of their paper/shadow/live results. It never
carries code, logins, keys, account details, positions or any server address. The friend imports it on their own
server: it becomes a preset of the same built-in strategy there, and runs with their own Zerodha login, lots and limits.

Updates: each share has an id. When the sharer changes the preset they send a new code with the same id; importing it
shows what changed and lets the friend keep it as a new preset or update the one they have.

  ALGOSHARE1.<base64url(zlib(json))>.<check>
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import zlib
from datetime import datetime

from . import data

PREFIX = "ALGOSHARE1"
MAX_CODE = 20_000                 # characters a share code may have
SHARES = "shares.json"            # runtime/shares.json: what this server shared (for "changed since shared")


def _check(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:8]


def encode(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    body = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")
    return f"{PREFIX}.{body}.{_check(body)}"


def decode(code: str) -> dict:
    text = re.sub(r"\s+", "", str(code or ""))
    if text.startswith("{"):                                    # a share file: the payload as JSON
        try:
            out = json.loads(code)
        except ValueError:
            raise ValueError("That file is not a strategy share.") from None
        return _shape(out)
    m = re.search(PREFIX + r"\.([A-Za-z0-9_-]+)\.([0-9a-f]{8})", text)
    if not m or len(text) > MAX_CODE:
        raise ValueError("That is not a share code: it starts with ALGOSHARE1.")
    body, chk = m.groups()
    if _check(body) != chk:
        raise ValueError("The share code is incomplete or changed: copy it again in full.")
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)), bufsize=MAX_CODE * 4)
        return _shape(json.loads(raw.decode("utf-8")))
    except (ValueError, zlib.error, UnicodeDecodeError):
        raise ValueError("The share code could not be read.") from None


def _shape(p) -> dict:
    """Only the fields a share may have, as plain values."""
    if not isinstance(p, dict) or p.get("v") != 1 or p.get("kind") != "preset":
        raise ValueError("That is not a strategy share (or it is from a newer version).")
    pre = p.get("preset") if isinstance(p.get("preset"), dict) else {}
    s = lambda v, n=80: re.sub(r"\s+", " ", str(v or "")).strip()[:n]          # noqa: E731
    params = pre.get("params") if isinstance(pre.get("params"), dict) else {}
    params = {str(k)[:60]: v for k, v in list(params.items())[:200] if isinstance(v, (bool, int, float, str))}
    out = {"v": 1, "kind": "preset", "id": s(p.get("id"), 40), "version": int(p.get("version") or 1) if str(p.get("version") or "1").isdigit() else 1,
           "strategy": s(p.get("strategy"), 60), "title": s(p.get("title")), "from": s(p.get("from"), 40), "shared": s(p.get("shared"), 20),
           "preset": {"name": s(pre.get("name"), 40), "params": params}}
    if not re.fullmatch(r"[a-z0-9]{6,40}", out["id"]):
        raise ValueError("The share has no valid id.")
    for key in ("backtest", "results"):
        v = p.get(key)
        if isinstance(v, dict):
            out[key] = {str(k)[:40]: (x if isinstance(x, (int, float, bool)) or x is None else s(x, 60)) for k, x in list(v.items())[:30]}
        elif isinstance(v, list):
            out[key] = [{str(k)[:40]: (x if isinstance(x, (int, float, bool)) or x is None else s(x, 60)) for k, x in list(r.items())[:10]}
                        for r in v[:6] if isinstance(r, dict)]
    return out


# ---- sharing ---------------------------------------------------------------------------------------------------------
def _shares() -> dict:
    return data._json(data.runtime() / SHARES, {}) or {}


def backtest_summary(strategy: str, pid: str) -> dict | None:
    from .results import reference_first
    run = next(iter(reference_first(strategy, [r for r in data.runs(strategy) if r.get("preset_id") == pid], pid)), None)
    if not run:
        return None
    mt = (data._json(data.bt_store() / run["name"] / "run.json", {}) or {}).get("metrics") or {}
    keep = {"Net P&L": "net", "Max drawdown": "max_dd", "Cycles": "cycles", "Win rate %": "win",
            "Net with charges 50% higher": "net_charges50", "Monte Carlo: max drawdown, 1 in 20 worse than": "dd_1in20",
            "Fills on real option prices": "real", "Return on peak margin %": "ret_margin"}
    out = {"from": run.get("from"), "to": run.get("to"), "dataset": run.get("dataset")}
    out.update({v: mt.get(k) for k, v in keep.items() if k in mt})
    return out


def results_summary(strategy: str, pid: str) -> list:
    out = []
    from .analysis import session_days
    for s in data.sessions():
        if s["strategy"] == strategy and s.get("preset_id") == pid and not s.get("sim"):
            sd = session_days(s["id"])
            out.append({"mode": s["mode"], "net": round(sum(sd["pnl"].values()), 0), "days": len(sd["ran"]),
                        "feed": s.get("feed")})
    return out


def make(strategy: str, pid: str, by: str, backtest: bool = True, results: bool = False, to: str = "") -> dict:
    entry = next((c for c in data.catalog() if c["name"] == strategy), None)
    if entry is None:
        raise LookupError("Unknown strategy.")
    pre = data.presets(strategy).get(pid)
    if not pre:
        raise LookupError("No such preset.")
    allsh = _shares()
    sid = next(
        (k for k, v in allsh.items() if v.get("strategy") == strategy and v.get("preset") == pid), None) \
        or base64.b32encode(os.urandom(10)).decode().lower()
    payload = {"v": 1, "kind": "preset", "id": sid, "version": int(pre.get("version", 1)), "strategy": strategy,
               "title": (entry.get("title") or strategy).split(" (")[0], "from": str(by or "")[:40],
               "shared": datetime.now().strftime("%Y-%m-%d"),
               "preset": {"name": pre["name"], "params": data.clean_params(entry, pre.get("params") or {})}}
    if backtest:
        bt = backtest_summary(strategy, pid)
        if bt:
            payload["backtest"] = bt
    if results:
        rs = results_summary(strategy, pid)
        if rs:
            payload["results"] = rs
    code = encode(payload)
    rec = allsh.get(sid, {"strategy": strategy, "preset": pid, "made": datetime.now().isoformat(timespec="seconds"), "to": []})
    rec.update(version=payload["version"], name=pre["name"], last=datetime.now().isoformat(timespec="seconds"),
               backtest=bool(payload.get("backtest")), results=bool(payload.get("results")))
    to = re.sub(r"\s+", " ", str(to or "")).strip()[:40]
    if to and to not in rec["to"]:
        rec["to"] = (rec.get("to") or [])[-19:] + [to]
    allsh[sid] = rec
    data._write(data.runtime() / SHARES, allsh)
    name = re.sub(r"[^A-Za-z0-9]+", "-", f"{payload['title']}-{pre['name']}").strip("-")[:60]
    return {"code": code, "file": f"{name}.algoshare.txt", "payload": payload, "share": {"id": sid, **rec}}


def shares(strategy: str) -> list:
    pres = data.presets(strategy)
    out = []
    for sid, r in sorted(_shares().items(), key=lambda kv: kv[1].get("last", ""), reverse=True):
        if r.get("strategy") != strategy:
            continue
        p = pres.get(r.get("preset"))
        out.append({"id": sid, **r, "exists": bool(p), "changed": bool(p) and int(p.get("version", 1)) > int(r.get("version", 1)),
                    "now_version": (p or {}).get("version")})
    return out


def forget(sid: str) -> None:
    allsh = _shares()
    if allsh.pop(sid, None) is None:
        raise LookupError("No such share.")
    data._write(data.runtime() / SHARES, allsh)


# ---- importing -------------------------------------------------------------------------------------------------------
def _label(entry: dict, key: str) -> str:
    p = next((x for x in entry.get("params", []) if x["key"] == key), None)
    return re.sub(r"\s*\([^()]*\)\s*$", "", p["label"]) if p else key


def preview(code: str) -> dict:
    """What importing this code would do: checks, and if this share was imported before, what changed."""
    p = decode(code)
    entry = next((c for c in data.catalog() if c["name"] == p["strategy"]), None)
    checks = [{"name": "No program code: settings only", "ok": True}]
    if entry is None:
        checks.append({"name": f"Strategy '{p['strategy']}' is on this server", "ok": False,
                       "note": "Update this server (Profile › Server version) or ask your friend which version they use."})
        return {"share": p, "checks": checks, "ok": False}
    checks.append({"name": "A built-in strategy on this server", "ok": True, "note": entry.get("title", "")})
    known = {x["key"] for x in entry.get("params", [])}
    unknown = sorted(k for k in p["preset"]["params"] if k not in known)
    try:
        clean = data.clean_params(entry, {k: v for k, v in p["preset"]["params"].items() if k in known})
        checks.append({"name": "Settings valid", "ok": True,
                       "note": f"Left out (not on this server): {', '.join(unknown)}" if unknown else ""})
    except ValueError as e:
        checks.append({"name": "Settings valid", "ok": False, "note": str(e)})
        return {"share": p, "checks": checks, "ok": False}
    mine = data.presets(p["strategy"])
    have = next(((k, v) for k, v in mine.items() if (v.get("share") or {}).get("id") == p["id"]), None)
    out = {"share": p, "checks": checks, "ok": True, "params": clean,
           "described": [{"label": _label(entry, k), "value": v} for k, v in clean.items()
                         if v != next((x.get("default") for x in entry["params"] if x["key"] == k), None)]}
    if have:
        old = have[1].get("params") or {}
        out["update_of"] = {"id": have[0], "name": have[1]["name"], "version": (have[1].get("share") or {}).get("version"),
                            "changes": [{"label": _label(entry, k), "before": old.get(k), "after": clean.get(k)}
                                        for k in sorted(set(old) | set(clean)) if old.get(k) != clean.get(k)]}
    return out


def take(code: str, how: str = "new", name: str = "", by: str = "") -> tuple[str, dict]:
    """Import: a new preset ("new"), or update the preset this share was imported as before ("update")."""
    pv = preview(code)
    if not pv["ok"]:
        bad = next((c for c in pv["checks"] if not c["ok"]), {})
        raise ValueError(bad.get("name", "This share cannot be imported.") + (": " + bad["note"] if bad.get("note") else ""))
    p = pv["share"]
    meta = {"id": p["id"], "from": p["from"], "version": p["version"], "shared": p["shared"], "imported": datetime.now().isoformat(timespec="seconds"),
            **({"backtest": p["backtest"]} if p.get("backtest") else {}), **({"results": p["results"]} if p.get("results") else {})}
    source = f"From {p['from'] or 'a friend'}, shared {p['shared']}"
    if how == "update":
        if not pv.get("update_of"):
            raise LookupError("This share has not been imported here before.")
        pid = pv["update_of"]["id"]
        cur = data.presets(p["strategy"])[pid]
        pid, pre = data.save_preset(p["strategy"], cur["name"], pv["params"], pid, source, by)
    else:
        mine = data.presets(p["strategy"])
        base = re.sub(r"\s+", " ", str(name or p["preset"]["name"] or "Shared")).strip()[:34]
        nm, n = base, 2
        while any(v.get("name", "").lower() == nm.lower() for v in mine.values()):
            nm, n = f"{base} ({n})", n + 1
        pid, pre = data.save_preset(p["strategy"], nm, pv["params"], "", source, by)
    allp = data.presets()
    allp[p["strategy"]][pid]["share"] = meta
    data._write(data.presets_file(), allp)
    return pid, {**allp[p["strategy"]][pid], "strategy": p["strategy"]}
