"""Send saved backtest runs to the portfolio dashboard on the server (its Backtests page).

Settings in secrets.yaml:

    dashboard:
      url: "http://YOUR_SERVER_IP:5010"
      user: "admin"
      password: "the dashboard password"

Each run is sent as a small zip: run.json, one equity point per day (closing and lowest value),
cycles, legs, orders (fills) and the log. A run that was sent is marked with runs/<run>/.published.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import yaml

from .config import ROOT
from .runs import RUNS_DIR


class PublishError(RuntimeError):
    pass


def settings() -> dict:
    path = ROOT / "secrets.yaml"
    d = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("dashboard") if path.exists() else None
    d = d or {}
    missing = [k for k in ("url", "user", "password") if not d.get(k)]
    if missing:
        raise PublishError("Add the dashboard's address and sign-in to secrets.yaml first:\n\n"
                           "dashboard:\n  url: \"http://YOUR_SERVER_IP:5010\"\n  user: \"admin\"\n"
                           "  password: \"...\"\n\n(missing: " + ", ".join(missing) + ")")
    d["url"] = str(d["url"]).rstrip("/")
    return d


def daily_equity(run_dir: Path) -> pd.DataFrame:
    eq = pd.read_csv(run_dir / "equity.csv", parse_dates=["time"]).set_index("time")
    g = eq.groupby(eq.index.date)
    out = pd.DataFrame({"equity": g.equity.last(), "low": g.equity.min(), "spot": g.spot.last()})
    out.index.name = "date"
    return out.round(2)


def pack(run_dir: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(run_dir / "run.json", "run.json")
        z.writestr("equity_daily.csv", daily_equity(run_dir).to_csv())
        for name in ("cycles.csv", "trades.csv", "fills.csv", "log.txt"):
            if (run_dir / name).exists():
                z.write(run_dir / name, name)
    return buf.getvalue()


def signed_in_session(cfg: dict) -> requests.Session:
    s = requests.Session()
    try:
        r = s.post(f"{cfg['url']}/signin", data={"username": cfg["user"], "password": cfg["password"]},
                   timeout=20, allow_redirects=False)
    except requests.RequestException as e:
        raise PublishError(f"Could not reach the dashboard at {cfg['url']}: {e}") from None
    if r.status_code not in (302, 303):
        raise PublishError("The dashboard refused the sign-in: check user and password in secrets.yaml "
                           "(after 5 wrong tries it locks out for 15 minutes).")
    return s


def unpublished(root: Path = RUNS_DIR) -> list[Path]:
    return [d for d in sorted(root.glob("*")) if (d / "run.json").exists() and (d / "equity.csv").exists()
            and not (d / ".published").exists()]


def publish(run_dirs: list[Path], log=print) -> int:
    if not run_dirs:
        log("Nothing new to send.")
        return 0
    cfg = settings()
    s = signed_in_session(cfg)
    sent = 0
    for d in run_dirs:
        r = s.post(f"{cfg['url']}/api/backtests/upload", data={"name": d.name},
                   files={"file": ("run.zip", pack(d), "application/zip")}, timeout=60)
        if r.status_code == 404:
            raise PublishError("The server dashboard has no Backtests page yet. Install it first "
                               "(see kite-dashboard/server/backtests/README).")
        if not r.ok:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text[:200]
            raise PublishError(f"{d.name}: the server said {r.status_code} {msg}")
        (d / ".published").write_text(json.dumps({"url": cfg["url"], "at": datetime.now().isoformat(timespec="seconds")}))
        sent += 1
        log(f"Sent {d.name}")
    log(f"{sent} run(s) on {cfg['url']}/backtests")
    return sent
