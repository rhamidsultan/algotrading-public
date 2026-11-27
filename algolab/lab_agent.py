"""The Mac helper for Algo Lab: runs the backtests and sweeps that the dashboard sends to "Mac".

The dashboard keeps jobs meant for the Mac in its job list (they never enter the server's own queue). This
helper, started at login by deploy/install-mac-agent.command, signs in to the dashboard the way `run.py publish`
does (secrets.yaml: dashboard url / user / password), and every few seconds asks for the next Mac job; the
dashboard counts that as the Mac being online. For each job it first copies any new prices from the server
(read-only, the same as get-server-data), runs it here, sends a backtest's result to the Backtests page and
reports the outcome. Nothing on the Mac is reachable from outside: the Mac only calls the dashboard.

  python run.py lab-agent            (what the login item runs; Ctrl+C stops it)
  python run.py lab-agent --once     (one look for a job, then stop: for a test)
"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

POLL_SECONDS = 8
CAN = ("backtest", "sweep", "td_live_test")   # job kinds this helper runs: the dashboard sends only these
BEAT_SECONDS = 20     # while a job runs, tell the dashboard the Mac is still there


def _log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def _server_env() -> dict:
    """ALGO_HOST / ALGO_KEY from ~/.algo-trading/server.env (written by the Mac launchers)."""
    path = Path.home() / ".algo-trading" / "server.env"
    out = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def sync_prices(log=print) -> str:
    """New prices from the server (only new or changed files); never fatal for the job."""
    from . import kite
    from .config import load_config
    env, srv = _server_env(), load_config().get("server", {})
    host, key = env.get("ALGO_HOST") or srv.get("host", ""), env.get("ALGO_KEY") or srv.get("key_file", "")
    if not host:
        return "prices not refreshed (no server set in ~/.algo-trading/server.env)"
    try:
        return str(kite.sync_from_server(host, key, srv.get("data_path", "/opt/algolab/data")))[-200:]
    except Exception as e:  # noqa: BLE001
        log(f"Price copy failed: {e}")
        return f"prices not refreshed ({e})"


class Agent:
    def __init__(self, session=None, cfg=None, log=_log, sync=sync_prices):
        from .publish import settings, signed_in_session
        self.cfg = cfg or settings()
        self.s = session or signed_in_session(self.cfg)
        self.log, self.sync = log, sync
        self.name = socket.gethostname().split(".")[0][:40]

    def _post(self, path, body):
        r = self.s.post(f"{self.cfg['url']}{path}", json=body, timeout=30)
        if r.status_code == 401:            # signed out (e.g. dashboard restarted): sign in again once
            from .publish import signed_in_session
            self.s = signed_in_session(self.cfg)
            r = self.s.post(f"{self.cfg['url']}{path}", json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def status(self, job, **fields):
        return self._post("/api/lab-agent/status", {"id": job["id"], **fields})

    def publish(self, run_dir: Path) -> str:
        from .publish import publish
        publish([run_dir], log=self.log)
        return run_dir.name

    def once(self) -> bool:
        """Ask for one job and run it. True when a job was run."""
        job = self._post("/api/lab-agent/claim", {"name": self.name, "can": list(CAN)}).get("job")
        if not job:
            return False
        self.log(f"Job {job['id']}: {job.get('kind', 'backtest')} of {job['strategy']}")
        busy = threading.Event()

        def beat():
            while not busy.wait(BEAT_SECONDS):
                try:
                    self._post("/api/lab-agent/claim", {"name": self.name, "beat_only": True, "can": list(CAN)})
                except Exception:  # noqa: BLE001
                    pass
        threading.Thread(target=beat, daemon=True).start()
        try:
            return self._run_job(job)
        finally:
            busy.set()

    def _td_test(self, job) -> dict:
        """Algo Lab › Run › TrueData real-time test: count TrueData's ticks per second here (the login is on this Mac)."""
        import subprocess
        import sys
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        if job.get("set") == "mcx":
            if now.weekday() >= 5 or not ((9, 0) <= (now.hour, now.minute) < (23, 25)):
                raise RuntimeError("MCX is closed now: send it again between 9:00 and 23:25 IST on a trading day.")
        elif now.weekday() >= 5 or not ((9, 15) <= (now.hour, now.minute) < (15, 28)):
            raise RuntimeError("The market is closed now: send it again between 9:15 and 15:28 IST on a trading day.")
        try:
            import truedata  # noqa: F401
        except ImportError:
            self.log("Installing TrueData's package")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "truedata"], capture_output=True, text=True, timeout=600)
            if r.returncode:
                raise RuntimeError(f"Could not install TrueData's package: {(r.stderr or r.stdout)[-300:]}")
        import json
        import tempfile
        from pathlib import Path
        seconds = float(job.get("seconds") or 120)
        out = Path(tempfile.mkdtemp()) / "td.json"
        cmd = [sys.executable, "-m", "algolab.td_live_test", "--seconds", str(seconds), "--out", str(out)]
        if job.get("symbols"):
            cmd += ["--symbols", ",".join(job["symbols"])]
        if job.get("start_at"):
            cmd += ["--start-at", str(job["start_at"])]
        try:                                     # its own program: TrueData's package can leave threads running
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 240,
                               cwd=str(Path(__file__).resolve().parents[1]))
        except subprocess.TimeoutExpired:
            raise RuntimeError("The TrueData test did not finish in time.") from None
        for line in (r.stdout or "").splitlines()[-12:]:
            self.log(line)
        try:
            rep = json.loads(out.read_text())
        except (OSError, ValueError):
            raise RuntimeError(f"The TrueData test stopped: {(r.stderr or r.stdout or '').strip()[-300:]}") from None
        if rep.get("error"):
            raise RuntimeError(rep["error"])
        return {"td": rep}

    def _run_job(self, job) -> bool:
        from .backtest_queue import run_one, run_sweep
        from .sweep import safe_workers
        if job.get("kind") == "td_live_test":
            try:
                self.status(job, status="done", **self._td_test(job))
                self.log(f"Job {job['id']} done")
            except Exception as e:  # noqa: BLE001
                self.status(job, status="failed", message=f"{e}"[:400])
                self.log(f"Job {job['id']} failed: {e}")
            return True
        self.log("Copying new prices from the server")
        self.status(job, status="running", step="sync")       # the page: "copying new prices from the server"
        note = self.sync(self.log)
        try:
            if job.get("kind") == "sweep":
                last = [0.0]

                def progress(n, t):
                    if time.time() - last[0] > 3 or n == t:
                        last[0] = time.time()
                        self.status(job, status="running", progress=[n, t])
                out = run_sweep(job, workers=safe_workers(), progress=progress)
            else:
                def days(n, t):
                    self.status(job, status="running", progress=[n, t], step="day")
                out = run_one(job, publish=self.publish, where=f"the Mac ({self.name})", progress=days)
            self.status(job, status="done", message=note, **out)
            self.log(f"Job {job['id']} done")
        except Exception as e:  # noqa: BLE001 - every failure is reported on the page
            self.status(job, status="failed", message=f"{type(e).__name__}: {e}"[:400])
            self.log(f"Job {job['id']} failed: {e}")
        return True

    def run(self, once: bool = False):
        self.log(f"Algo Lab helper for {self.cfg['url']} on {self.name}")
        while True:
            try:
                ran = self.once()
            except Exception as e:  # noqa: BLE001 - the dashboard may be restarting; try again
                self.log(f"Could not reach the dashboard: {e}")
                ran = False
            if once:
                return
            if not ran:
                time.sleep(POLL_SECONDS)
