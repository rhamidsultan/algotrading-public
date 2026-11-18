"""Paper trading: several sessions side by side, each with its own simulated account. One strategy
can run in several sessions at once with different settings (e.g. premium_strangle, premium_strangle-2).

Every session lives in runtime/sessions/<id>/ (the first session of a strategy uses the strategy's
name as its id, later ones add -2, -3, ...), with
    state.json     account, positions, strategy state and recent log (saved after every update)
    equity.csv     account value, one point a minute
    desired.json   present while the session should run (written by a dashboard)
    heartbeat      updated while it runs
    STOP           a stop request (for sessions started from the command line)
    status.txt     why it is waiting, when it cannot run yet

Sessions on Zerodha live prices share one feed: every update fetches the index, VIX and all the
options any session uses in a single request (Zerodha allows about one quote request a second).
Simulated-market sessions each get their own made-up market. A session resumes where it left off
after a restart. No order is ever sent to Zerodha.
"""
from __future__ import annotations

import json
import shutil
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .config import RUNTIME, ist_now
from .engine import Stepper, make_broker
from .strategy import load_strategies

SESSIONS = RUNTIME / "sessions"
SERVICE_BEAT = RUNTIME / "service_heartbeat"
CATALOG = RUNTIME / "strategies.json"
_LEGACY = ("paper_state.json", "paper_equity.csv", "paper_desired.json", "STOP", "heartbeat", "service_status.txt")
_service = False


# ---- files of one session ---------------------------------------------------------------
class Slot:
    def __init__(self, sid: str):
        self.id = sid
        self.dir = SESSIONS / sid
        self.state, self.equity = self.dir / "state.json", self.dir / "equity.csv"
        self.desired, self.stop = self.dir / "desired.json", self.dir / "STOP"
        self.heartbeat, self.status = self.dir / "heartbeat", self.dir / "status.txt"
        self.tick = self.dir / "tick.json"      # the latest prices and P&L only, for the page's 1-second refresh

    def mk(self) -> "Slot":
        self.dir.mkdir(parents=True, exist_ok=True)
        return self


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")   # writers never share a temp file
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def migrate_legacy() -> None:
    """Move a session saved by the one-session version (runtime/paper_*.json) into sessions/<id>/."""
    state, desired = _read_json(RUNTIME / "paper_state.json"), _read_json(RUNTIME / "paper_desired.json")
    sid = (state or desired or {}).get("strategy")
    if not sid:
        return
    slot = Slot(sid).mk()
    for old, new in (("paper_state.json", slot.state), ("paper_equity.csv", slot.equity),
                     ("paper_desired.json", slot.desired)):
        src = RUNTIME / old
        if src.exists() and not new.exists():
            shutil.move(str(src), str(new))
    for name in _LEGACY:
        (RUNTIME / name).unlink(missing_ok=True)


def sessions() -> list[str]:
    """Ids of sessions that have a saved account or should be running, most recently active first."""
    if not SESSIONS.exists():
        return []
    out = [d for d in SESSIONS.iterdir() if d.is_dir() and ((d / "state.json").exists() or (d / "desired.json").exists())]

    def last_change(d: Path) -> float:
        t = 0.0
        for name in ("state.json", "desired.json", "heartbeat"):
            try:
                t = max(t, (d / name).stat().st_mtime)
            except OSError:
                pass
        return t
    return [d.name for d in sorted(out, key=last_change, reverse=True)]


def _sid(sid: str | None) -> str | None:
    if sid:
        return sid
    found = sessions()
    return found[0] if found else None


def read_state(sid: str | None = None) -> dict | None:
    sid = _sid(sid)
    return _read_json(Slot(sid).state) if sid else None


def read_desired(sid: str | None = None) -> dict | None:
    sid = _sid(sid)
    return _read_json(Slot(sid).desired) if sid else None


def equity_file(sid: str | None = None) -> Path | None:
    sid = _sid(sid)
    return Slot(sid).equity if sid else None


def is_running(sid: str | None = None) -> bool:
    ids = [sid] if sid else sessions()
    for s in ids:
        try:
            if _time.time() - float(Slot(s).heartbeat.read_text()) < 10:
                return True
        except Exception:
            pass
    return False


def session_status(sid: str) -> str:
    try:
        return Slot(sid).status.read_text(encoding="utf-8")
    except Exception:
        return ""


def new_id(strategy: str) -> str:
    """A free session id for another session of this strategy."""
    sid, n = strategy, 1
    while (SESSIONS / sid).exists():
        n += 1
        sid = f"{strategy}-{n}"
    return sid


def set_desired(strategy: str, params: dict, feed: str, mode: str = "paper", live: dict | None = None,
                sid: str | None = None, label: str = "", risk: dict | None = None) -> str:
    """Ask the service to run a session. Without `sid` the strategy's own session is used (created if
    needed); pass sid=new_id(strategy) for an additional one with other settings."""
    slot = Slot(sid or strategy).mk()
    slot.stop.unlink(missing_ok=True)
    want = {"strategy": strategy, "params": params, "feed": feed}
    if label:
        want["label"] = label
    if risk:
        from .risk import settings
        want["risk"] = settings(risk)
    if mode != "paper":
        want.update(mode=mode, live=live or {})
    _write_json(slot.desired, want)
    return slot.id


def kill(sid: str) -> None:
    """Square off and stop (the running session does it at its next update)."""
    Slot(sid).mk()
    (Slot(sid).dir / "KILL").write_text("kill")


def request_stop(sid: str | None = None) -> None:
    for s in ([sid] if sid else sessions()):
        slot = Slot(s).mk()
        slot.desired.unlink(missing_ok=True)
        slot.stop.write_text("stop")


def reset(sid: str | None = None) -> None:
    sid = _sid(sid)
    if sid:
        slot = Slot(sid)
        for f in (slot.state, slot.equity, slot.stop, slot.status):
            f.unlink(missing_ok=True)


def service_alive() -> bool:
    try:
        return _time.time() - float(SERVICE_BEAT.read_text()) < 45
    except Exception:
        return False


def write_catalog() -> None:
    """Strategies and their settings, for pages that start sessions (e.g. the portfolio dashboard)."""
    RUNTIME.mkdir(exist_ok=True)
    CATALOG.write_text(json.dumps(catalog()), encoding="utf-8")


def catalog() -> list[dict]:
    """Every strategy with its settings (and the Orders & stops rows), as the pages show them."""
    out = []
    for name, cls in load_strategies().items():
        from .risk import catalog_params
        own = [cls.PARAMS[k].label for k in getattr(cls, "STOP_OFF", {}) if k in cls.PARAMS]
        out.append({"name": name, "title": cls.title, "description": cls.description, "live_ready": cls.live_ready,
                    **({"built": True} if getattr(cls, "built", False) else {}),
                    "own_stops": own,
                    "params": [{"key": k, "label": p.label, "default": p.default, "type": type(p.default).__name__,
                                "min": p.min, "choices": p.choices} for k, p in cls.PARAMS.items()]
                    + catalog_params(getattr(cls, "RISK_DEFAULTS", None))})
    return out


# ---- one running session ---------------------------------------------------------------
class Session:
    """One strategy with its own account. mode: paper (simulated orders), shadow (live checks and the
    exact orders it would send, but simulated fills) or live (real orders through Zerodha)."""

    def __init__(self, cfg: dict, want: dict, fresh: bool = False, sid: str | None = None):
        self.cfg, self.want = cfg, want
        self.slot = Slot(sid or want["strategy"]).mk()
        if fresh:
            reset(self.slot.id)
        self.slot.stop.unlink(missing_ok=True)
        self.slot.status.unlink(missing_ok=True)
        self.name, self.feed_kind = want["strategy"], want["feed"]
        self.mode = want.get("mode", "paper")
        from .risk import RiskManager, split, strategy_params
        cls = load_strategies()[self.name]
        own, rk = split(want.get("params", {}), want.get("risk"))          # "risk.*" settings from the pages
        self.strategy = cls(**strategy_params(cls, own, rk))
        self.risk_settings = rk
        self.risk = RiskManager(rk) if rk else None                          # Orders & stops
        self.logs: list = []
        self.notify = lambda subj, body: self._log(f"{subj}: {body}")
        if self.mode in ("shadow", "live"):
            from .broker import CostModel
            from .live import LiveBroker, LiveSettings, make_notifier
            if self.feed_kind != "kite" and self.mode == "live":
                raise ValueError("Live orders need the Zerodha live feed.")
            if self.mode == "live" and not type(self.strategy).live_ready:
                raise ValueError(f"{self.name} is not enabled for live orders yet (shadow mode works).")
            from .risk import ORDER_KEYS
            orders = {k: rk[k] for k in ORDER_KEYS} if rk else {}            # Orders & stops: order settings
            self.live = LiveSettings.from_dict({**(want.get("live") or {}), **orders, "mode": self.mode})
            base_notify = make_notifier(cfg.get("alerts", {}).get("trader_config"), self._log)

            def notify(subject, body, base=base_notify, mode=self.mode):
                base(subject, body)
                from .pushbox import push
                push(subject, body, "/live", "trading")
            self.notify = notify
            self.broker = LiveBroker(cfg["account"]["capital"], CostModel(**cfg["costs"]), cfg["execution"]["slippage_pct"],
                                     cfg["market"]["tick_size"], cfg.get("cost_history"), settings=self.live,
                                     notify=self.notify, log=self._log)
            self.broker.journal = self.slot.dir / "working.json"     # orders in flight, written the moment they change
        else:
            self.live = None
            self.broker = make_broker(cfg)
        saved = _read_json(self.slot.state)
        if (saved and saved.get("strategy") == self.name and saved.get("feed") == self.feed_kind
                and saved.get("mode", "paper") == self.mode):
            self.broker.load_dict(saved["broker"])
            self.strategy.set_state(saved.get("strategy_state", {}))
            if self.risk is not None:
                self.risk.set_state(saved.get("risk_state"))
            self.logs[:0] = saved.get("logs", [])
            self.day = saved.get("day") or {}
            self.saved = saved
        else:
            if saved:   # a different feed or mode: start a new account
                reset(self.slot.id)
            self.day = {}
            self.saved = None
        if not self.slot.equity.exists():
            self.slot.equity.write_text("time,equity,spot\n")
        self._minute = None
        self.bars = 0
        self.stepper = None
        self.halted = ""
        self.closing_only = False        # live: stopped, but a leg it could not close is still retried (CLOSE_RETRY_EVERY)
        self._close_tried = 0.0
        self.close_tries = 0
        if (self.saved and self.saved.get("closing_only") and self.mode == "live" and self.broker.positions
                and str(self.saved.get("time", ""))[:10] == ist_now().date().isoformat()):   # not on a later day
            self.halted = self.saved.get("halted") or "Stopped"
            self.closing_only = True
            self.close_tries = int(self.saved.get("close_tries") or 0)
        self._kill_noted = False
        self._stops_checked = 0.0
        self._positions_checked = _time.time()   # the start-of-session reconcile covers the first minute
        self._failing: dict = {}         # kind of Zerodha check -> (failing since, alert sent)
        if self.mode != "paper":
            self._log(f"Session in {self.mode.upper()} mode: max {self.live.max_lots} lot(s) per order, "
                      f"daily loss limit Rs {self.live.daily_loss_limit:,.0f}, protective stops "
                      f"{'at ' + format(self.live.protect_mult, 'g') + 'x' if self.live.protect_mult > 0 else 'off'}; orders "
                      f"{'market (one limit ' + format(self.live.max_chase_pct, 'g') + '% through)' if self.live.order_type == 'market' else 'limit, re-priced for up to ' + str(self.live.chase_seconds) + ' s'}")

    def _log(self, msg: str) -> None:
        self.logs.append(f"{ist_now():%Y-%m-%d %H:%M}  {msg}")

    def bind(self, feed) -> None:
        self.feed = feed
        self.stepper = Stepper(feed, self.strategy, self.broker, self.logs, self._after_bar, keep_curve=False, risk=self.risk)
        if hasattr(self.broker, "attach"):
            self.broker.attach(feed)

    def beat(self) -> None:
        self.slot.heartbeat.write_text(str(_time.time()))

    CLOSE_RETRY_EVERY = 30          # seconds between tries to close what a live square-off could not
    CLOSE_RETRY_UNTIL = time(15, 25)  # IST: after this the retries stop and a person is asked (the market closes)

    def step(self, now) -> None:
        if self.closing_only:
            self._retry_close(now)
            return
        if self.halted:
            return
        from .live import Blocked, Halt
        kill = self.slot.dir / "KILL"
        try:
            if kill.exists():
                kill.unlink(missing_ok=True)
                raise Halt("Stopped from the dashboard")
            if self.mode == "live":
                try:      # cannot check Zerodha (positions, stops) just now: nothing is sent this moment
                    self.broker.reconcile(now)
                    self._zerodha_ok("reconcile")
                except Halt:
                    raise
                except Exception as e:  # noqa: BLE001
                    self._zerodha_failing("reconcile", f"positions not checked: {type(e).__name__}: {e}")
                    return
                # a check that cannot read Zerodha just now must not stop the strategy (its exits) this moment
                if _time.time() - self._stops_checked >= self.STOPS_EVERY:
                    self._stops_checked = _time.time()
                    try:
                        self.broker.check_protective_stops(now)
                        if getattr(self.broker, "stop_reads_failed", 0):
                            self._zerodha_failing("stops", f"{self.broker.stop_reads_failed} protective stop(s) not read")
                        else:
                            self._zerodha_ok("stops")
                    except Halt:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self._zerodha_failing("stops", f"protective stops not checked: {type(e).__name__}: {e}")
                else:   # between checks: any stop fill already pushed on the price stream
                    try:
                        self.broker.check_protective_stops(now, from_stream=True)
                    except Halt:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self.broker._not_read(f"Stop updates not read: {type(e).__name__}: {e}", "stream")
                if _time.time() - self._positions_checked >= self.broker.POSITIONS_EVERY:
                    self._positions_checked = _time.time()
                    try:
                        self.broker.check_positions(now)
                        self._zerodha_ok("positions")
                    except Halt:
                        raise
                    except Exception as e:  # noqa: BLE001
                        self._zerodha_failing("positions", f"positions not checked: {type(e).__name__}: {e}")
            self.stepper.step(now)
            if self.live is not None and self.live.daily_loss_limit > 0 and self.feed.bar_closed:
                eq = self.stepper.ctx.equity
                today = now.date().isoformat()
                if self.day.get("date") != today:
                    self.day = {"date": today, "start": eq}
                elif eq - self.day["start"] <= -self.live.daily_loss_limit:
                    raise Halt(f"Daily loss limit reached (Rs {eq - self.day['start']:,.0f})")
        except Blocked as e:
            self.halt(str(e), square_off=False, now=now)
        except Halt as e:
            self.halt(str(e), square_off=True, now=now)

    FAILING_ALERT_AFTER = 120       # seconds of Zerodha checks failing in a row before an alert
    STOPS_EVERY = 30                # seconds between full reads of the protective stops at Zerodha

    def _zerodha_ok(self, kind: str) -> None:
        self._failing.pop(kind, None)

    def _zerodha_failing(self, kind: str, msg: str) -> None:
        """A check of one kind (reconcile, stops, positions) failed: logged (once a minute for each kind); failing for
        FAILING_ALERT_AFTER s with no success of that kind in between sends one alert, so a session that cannot see
        Zerodha is never silent."""
        self.broker._not_read(f"Zerodha: {msg}", "fail " + kind)
        since, told = self._failing.get(kind, (None, False))
        if since is None:
            self._failing[kind] = (_time.time(), False)
        elif not told and _time.time() - since >= self.FAILING_ALERT_AFTER:
            self._failing[kind] = (since, True)
            self.notify(f"Algo Lab cannot check Zerodha: {self.slot.id}",
                        f"For {int(_time.time() - since)} s: {msg}. "
                        + ("Orders and exits wait until it works again; " if kind == "reconcile" else "")
                        + "protective stops already at Zerodha still work. Check Kite.")

    def kill_between_updates(self, now) -> None:
        """A Stop from the dashboard while no update is coming (market closed, before the open, between updates).
        With no open positions there is nothing to square off: stop now (cancelling any protective stop left at
        Zerodha). With positions, the square-off needs live prices: it happens at the next update (step())."""
        if self.halted or not (self.slot.dir / "KILL").exists():
            return
        if self.broker.positions:
            if not self._kill_noted:
                self._kill_noted = True
                self._log("Stop requested: open positions will be squared off at the next market update")
                self.slot.status.write_text("Stop requested: open positions will be squared off at the next market "
                                            "update (09:15 IST if the market is closed).", encoding="utf-8")
            return
        cancel = getattr(self.broker, "cancel_all_protective", None)
        problems = cancel(now) if cancel is not None else []
        self.halt("Stopped from the dashboard (no open positions)." + (
            " CHECK KITE NOW: " + "; ".join(problems) + "." if problems else ""), square_off=False, now=now)
        (self.slot.dir / "KILL").unlink(missing_ok=True)     # only once the session has stopped

    def halt(self, reason: str, square_off: bool, now) -> None:
        """Square off (if asked and possible), stop for good and tell the user."""
        problems = []
        self.halted = reason                                 # first: whatever fails below, it trades no more
        live = self.mode == "live" and hasattr(self.broker, "settle_working")

        def settle():
            try:
                return self.broker.settle_working(now)
            except Exception as e:  # noqa: BLE001
                return [f"orders not read: {e}"]
        if live and square_off:
            problems += settle()                             # first: a late opening fill is a leg to close too
        if square_off and self.broker.positions:
            retry = getattr(self.broker, "retry_close", None) if self.mode == "live" else None
            for c, pos in sorted(list(self.broker.positions.items()), key=lambda kv: kv[1].qty > 0):
                try:
                    if retry is not None:   # live: only what Zerodha still holds, never over an order that may fill
                        f = retry(now, c, self.feed.quote(c), "Square-off")
                    else:
                        f = self.broker.execute(now, c, "BUY" if pos.qty < 0 else "SELL", abs(pos.qty),
                                                self.feed.quote(c), "Square-off", pos.group)
                    if f is not None and not f.note:
                        f.note = reason
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{c.label}: {e}")
        if live and square_off and self.broker.working:
            problems += settle()                             # e.g. an opening order whose cancel was not confirmed
        if live and square_off and not problems and self.broker.positions:
            problems.append("a leg filled late: " + ", ".join(c.label for c in self.broker.positions))
        msg = reason + (" Positions were squared off." if square_off and not problems else "")
        if (live and square_off and problems and (self.broker.positions or self.broker.working)
                and self._may_retry(now)):
            # keep the session (closing only): it retries every CLOSE_RETRY_EVERY s, also after a restart
            self.closing_only = True
            self._close_tried = _time.time()
            msg += (" NOT CLOSED YET: " + "; ".join(problems) + f". Algo Lab keeps trying every {self.CLOSE_RETRY_EVERY} s"
                    f" until {self.CLOSE_RETRY_UNTIL:%H:%M} IST, with its protective stop back at Zerodha (unless an order "
                    "may still fill); you will get an alert when it is closed.")
            self._log(f"Session stopped, closing: {msg}")
            self.slot.status.write_text(f"Stopped, closing: {msg}", encoding="utf-8")
            self.notify(f"Algo Lab live session stopped, still closing: {self.slot.id}", msg)
            self._save_now(now)
            return
        if problems:
            msg += " COULD NOT CLOSE: " + "; ".join(problems) + " - close these in Kite now."
        self._finish(msg, now)

    def _retry_close(self, now) -> None:
        """Closing only: try again to close what the square-off could not. Each try sends only what Zerodha still holds
        for this session (LiveBroker.retry_close), so a leg closed in Kite meanwhile is not bought again. Each try is
        short (one round at the furthest step), so the other sessions are not held up for long."""
        from .live import Blocked
        kill = self.slot.dir / "KILL"
        late = not self._may_retry(now)   # checked before anything is sent
        if kill.exists() or late:          # Stop while closing, or the close: a person takes over in Kite
            kill.unlink(missing_ok=True)
            try:
                left = self.broker.settle_working(now)       # books fills; sends nothing
            except Exception as e:  # noqa: BLE001
                left = [f"orders not read: {e}"]
            self.closing_only = False
            why = (f"Retries stopped at {self.CLOSE_RETRY_UNTIL:%H:%M} IST." if late
                   else "Retries stopped from the dashboard.")
            legs = [f"{c.label} ({pos.qty:+d}, " + ("protective stop at Zerodha" if c.label in self.broker.protect
                                                    else "NO stop at Zerodha") + ")"
                    for c, pos in self.broker.positions.items()]
            if not legs and not left:
                self._finish(self.halted + f" {why} Positions were squared off.", now)
                return
            self._finish(self.halted + f" {why} NOT CLOSED: " + "; ".join(legs + left) + " - close these in Kite now.",
                         now)
            return
        if _time.time() - self._close_tried < self.CLOSE_RETRY_EVERY:
            return
        self.close_tries += 1
        problems, blocked = [], False
        try:
            problems += self.broker.settle_working(now)     # every order that may still fill, opening ones too
        except Exception as e:  # noqa: BLE001
            problems.append(f"orders not read: {e}")
        for c, pos in sorted(list(self.broker.positions.items()), key=lambda kv: kv[1].qty > 0):
            try:
                self.broker.retry_close(now, c, self.feed.quote(c), "Square-off (retry)", quick=True)
            except Blocked as e:
                blocked = True
                problems.append(f"{c.label}: {e}")
            except Exception as e:  # noqa: BLE001
                problems.append(f"{c.label}: {e}")
        self._close_tried = _time.time()            # counted from the end of the try
        if not self.broker.positions and not getattr(self.broker, "working", None):
            cancel = getattr(self.broker, "cancel_all_protective", None)
            left = cancel(now) if cancel is not None else []
            self.closing_only = False
            self._finish(self.halted + f" Positions were squared off (on try {self.close_tries + 1})."
                         + (" CHECK KITE NOW: " + "; ".join(left) + "." if left else ""), now)
            return
        if blocked:
            self.closing_only = False
            self._finish(self.halted + " COULD NOT CLOSE: " + "; ".join(problems) + " - close these in Kite now.", now)
            return
        self._log(f"Still closing (try {self.close_tries + 1}): " + "; ".join(problems))
        if self.close_tries % 10 == 0:
            self.notify(f"Algo Lab still closing: {self.slot.id}", f"Try {self.close_tries + 1}: " + "; ".join(problems))
        self._save_now(now)

    def _may_retry(self, now) -> bool:
        t = now.time() if hasattr(now, "time") else None
        day = getattr(self, "_closing_day", None)
        if day is None:
            self._closing_day = day = now.date() if hasattr(now, "date") else None
        same_day = day is None or not hasattr(now, "date") or now.date() == day
        return same_day and (t is None or t < self.CLOSE_RETRY_UNTIL)

    def _finish(self, msg: str, now) -> None:
        """Stop for good and tell the user."""
        self._log(f"Session stopped: {msg}")
        self.slot.desired.unlink(missing_ok=True)
        self.slot.status.write_text(f"Stopped: {msg}", encoding="utf-8")
        if self.mode != "paper":
            self.notify(f"Algo Lab {self.mode} session stopped: {self.slot.id}", msg)
        else:
            from .pushbox import push
            push(f"Paper session stopped: {self.want.get('label') or self.slot.id}", msg, "/paper", "trading")
        self._save_now(now)

    def _save_now(self, now) -> None:
        try:   # record the state now (also when no bar closes again)
            self._after_bar(self.stepper.ctx, (now, self.stepper.ctx.equity, self.feed.spot))
        except Exception:  # noqa: BLE001 - e.g. before the open: no prices yet. Still save the account as it is now
            try:
                state = _read_json(self.slot.state) or {}
                state.update(status="stopped", halted=self.halted, closing_only=self.closing_only,
                             close_tries=self.close_tries, broker=self.broker.to_dict(), logs=self.logs[-300:])
                _write_json(self.slot.state, state)
            except Exception:  # noqa: BLE001
                pass

    def _after_bar(self, ctx, point) -> None:
        minute = point[0].replace(second=0, microsecond=0)
        if minute != self._minute:   # the account-value history keeps one point a minute
            self._minute = minute
            with self.slot.equity.open("a") as f:
                f.write(f"{point[0].isoformat(sep=' ')},{point[1]:.2f},{point[2]:.2f}\n")
        b = self.broker
        positions = [{"Contract": c.label, "Qty": p.qty, "Avg price": round(p.avg_price, 2),
                      "LTP": round(ctx.price(c), 2), "P&L": round((ctx.price(c) - p.avg_price) * p.qty, 0)}
                     for c, p in b.positions.items()]
        _write_json(self.slot.tick, {"time": ctx.now.isoformat(), "spot": ctx.spot, "vix": ctx.vix, "equity": point[1],
                                     "unrealized": ctx.unrealized, "positions": positions,
                                     "entry_charges": sum(p.entry_costs for p in b.positions.values())})
        _write_json(self.slot.state, {
            "status": "stopped" if self.halted else "running", "halted": self.halted,
            "closing_only": self.closing_only, "close_tries": self.close_tries,
            "id": self.slot.id, "label": self.want.get("label", ""),
            "strategy": self.name, "params": self.strategy.params, "feed": self.feed_kind, "mode": self.mode,
            "live": None if self.live is None else {k: getattr(self.live, k) for k in self.live.__dataclass_fields__},
            "day": self.day, **({"sim_from": self.sim_from} if getattr(self, "sim_from", None) else {}),
            "time": ctx.now.isoformat(), "spot": ctx.spot, "vix": ctx.vix, "lot_size": ctx.lot_size,
            "equity": point[1], "net_realized": b.net_realized, "unrealized": ctx.unrealized,
            "positions": positions,
            "broker": b.to_dict(), "strategy_state": self.strategy.get_state(),
            "preset": self.want.get("preset"), "risk": self.risk_settings, "risk_state": self.risk.get_state() if self.risk is not None else None, "logs": self.logs[-300:],
        })
        self.bars += 1
        self.beat()

    def contracts(self) -> list:
        return list(self.broker.positions)

    def close(self) -> None:
        state = _read_json(self.slot.state)
        if state:
            state["status"] = "stopped"
            _write_json(self.slot.state, state)
        self.slot.heartbeat.unlink(missing_ok=True)


# ---- a group of sessions on one feed ----------------------------------------------------
class Group:
    """Runs sessions that share one feed. `wanted()` says which should be running (id -> settings);
    it is checked every second, so sessions join and leave without stopping the others."""

    def __init__(self, cfg: dict, wanted, max_bars: int | None = None, fresh: bool = False):
        self.cfg, self.wanted, self.max_bars, self.fresh = cfg, wanted, max_bars, fresh
        self.members: dict[str, Session] = {}
        self.feed = None
        self.done = False
        self._last_sync = 0.0

    def log(self, msg: str) -> None:
        for s in self.members.values():
            s.logs.append(f"{ist_now():%Y-%m-%d %H:%M}  {msg}")

    def sync(self) -> None:
        want = self.wanted()
        for sid in [s for s in self.members if s not in want]:
            self.members.pop(sid).close()
        for sid, w in want.items():
            m = self.members.get(sid)
            if m is not None and any(m.want.get(k) != w.get(k) for k in ("params", "feed", "mode", "live", "risk")):
                self.members.pop(sid).close()
                m = None
            if m is None:
                try:
                    s = Session(self.cfg, w, fresh=self.fresh, sid=sid)
                except Exception as e:  # noqa: BLE001 - e.g. a strategy removed in an update
                    Slot(sid).mk().status.write_text(f"Cannot start: {e}", encoding="utf-8")
                    continue
                if self.feed is not None:
                    s.bind(self.feed)
                if s.mode == "live":   # one Zerodha account: reconciliation counts the other live sessions' legs
                    s.broker.others = lambda me=sid: self.live_qty(exclude=me)
                self.members[sid] = s
        if self.max_bars and self.members and all(s.bars >= self.max_bars for s in self.members.values()):
            self.done = True
        self._last_sync = _time.time()

    def live_qty(self, exclude: str | None = None) -> dict:
        """Zerodha symbol -> net quantity the group's live sessions hold (all but `exclude`), from their own books.
        Sessions run one after another on this thread, so no book changes while this is read."""
        out: dict = {}
        for sid, s in self.members.items():
            if sid == exclude or s.mode != "live":
                continue
            for c, pos in s.broker.positions.items():
                sym = s.broker.symbol_of(c)
                if sym:
                    out[sym] = out.get(sym, 0) + pos.qty
        return out

    def sleep(self, seconds: float) -> bool:
        """Control for the feed: beats, notices sessions joining/leaving; False when none is left."""
        end = _time.time() + seconds
        while True:
            if _service:
                SERVICE_BEAT.write_text(str(_time.time()))
            for s in self.members.values():
                s.beat()
            if _time.time() - self._last_sync >= 1.0:
                for s in list(self.members.values()):
                    try:
                        s.kill_between_updates(ist_now())
                    except Exception as e:  # noqa: BLE001 - one session's problem must not stop the others
                        s.logs.append(f"{ist_now():%Y-%m-%d %H:%M}  Error on Stop: {type(e).__name__}: {e}")
                self.sync()
            if not self.members or self.done:
                return False
            left = end - _time.time()
            if left <= 0:
                return True
            _time.sleep(min(1.0, left))

    def run(self, feed) -> None:
        self.feed = feed
        for s in self.members.values():
            s.bind(feed)
        try:
            for now in feed:
                for s in list(self.members.values()):
                    if s.stepper is None:
                        s.bind(feed)
                    try:
                        s.step(now)
                    except Exception as e:  # noqa: BLE001 - one strategy's bug must not stop the others
                        s.logs.append(f"{ist_now():%Y-%m-%d %H:%M}  Error: {type(e).__name__}: {e}")
                if self.max_bars and all(s.bars >= self.max_bars for s in self.members.values()):
                    break
                if _time.time() - self._last_sync >= 1.0:
                    self.sync()
                if not self.members:
                    break
        finally:
            for s in self.members.values():
                s.close()
            self.members = {}
            st = getattr(feed, "stream", None)
            if st is not None:
                st.stop()


SIM_DAYS = 365          # a simulated session replays at most this many days of made-up prices, then stops


def _sim_feed(cfg: dict, session: Session, group: Group):
    """Made-up prices replayed fast (cfg paper.sim_seconds_per_bar): for checking that a strategy runs, not how it
    does. Stops the session once it has replayed SIM_DAYS (None then: nothing to run)."""
    from .feeds import HistoricalFeed, make_sample
    saved = session.saved
    start = (datetime.fromisoformat(saved["time"]).date() + timedelta(days=1)) if saved else ist_now().date()
    first = (saved or {}).get("sim_from")
    if saved and not first:                       # a session from before sim_from was kept: its first equity point
        try:
            with open(session.slot.equity, encoding="utf-8") as f:
                f.readline()
                first = (f.readline().split(",")[0] or "")[:10] or None
        except (OSError, AttributeError):
            first = None
    try:
        sim_from = date.fromisoformat(first or start.isoformat())
    except ValueError:
        sim_from = start
    session.sim_from = sim_from.isoformat()
    left = SIM_DAYS - (start - sim_from).days
    if left <= 0:
        msg = f"Stopped by itself: a year of simulated prices replayed ({sim_from:%d %b %Y} – {start:%d %b %Y})."
        session.slot.desired.unlink(missing_ok=True)
        session.slot.status.write_text(msg, encoding="utf-8")
        session._log(msg)
        return None
    df = make_sample(start, start + timedelta(days=min(400, left)), seed=int(_time.time()) % 100000,
                     s0=saved["spot"] if saved else 24000.0)
    df.index = df["datetime"] + timedelta(minutes=5)
    return HistoricalFeed(df, cfg, None, cfg["paper"]["sim_seconds_per_bar"], group)


def _kite_feed(cfg: dict, group: Group):
    from .kite import KiteFeed, client, make_stream
    k = client()
    return KiteFeed(k, cfg, group, log=group.log, stream=make_stream(k, cfg, log=group.log))


def run_group(cfg: dict, wanted, feed_kind: str, max_bars: int | None = None, fresh: bool = False) -> None:
    group = Group(cfg, wanted, max_bars, fresh)
    group.sync()
    if not group.members:
        return
    if feed_kind == "kite":
        feed = _kite_feed(cfg, group)
    else:
        feed = _sim_feed(cfg, next(iter(group.members.values())), group)
        if feed is None:
            return
    group.run(feed)


def run_paper(cfg: dict, strategy_name: str, params: dict, feed_kind: str = "sim", fresh: bool = False,
              max_bars: int | None = None) -> None:
    """One session in this process (command line / the Mac dashboard). Stops on a STOP request."""
    RUNTIME.mkdir(exist_ok=True)
    if is_running(strategy_name):
        raise SystemExit("This strategy's paper session is already running. Stop it first.")
    slot = Slot(strategy_name).mk()
    slot.stop.unlink(missing_ok=True)
    want = {"strategy": strategy_name, "params": params, "feed": feed_kind}
    wanted = lambda: {} if slot.stop.exists() else {strategy_name: want}  # noqa: E731
    print(f"Paper trading '{strategy_name}' on the {'Zerodha live' if feed_kind == 'kite' else 'simulated'} feed. Ctrl+C to stop.")
    try:
        run_group(cfg, wanted, feed_kind, max_bars=max_bars, fresh=fresh)
    except KeyboardInterrupt:
        pass
    finally:
        slot.stop.unlink(missing_ok=True)
    print("Paper session stopped. Positions and history are saved; start again to resume.")


# ---- server mode ---------------------------------------------------------------------------
def _desired_all() -> dict:
    out = {}
    for sid in sessions():
        d = _read_json(Slot(sid).desired)
        if d:
            out[sid] = d
    return out


def stop_idle_kills(desired: dict, alive: set) -> list:
    """Stop requests for sessions whose group is not running (for example waiting for the day's Zerodha login):
    nothing can be trading, so a session with no open positions and no protective stop left at Zerodha stops now.
    Anything else (positions, a leftover stop, a session file that cannot be read) waits until the session runs.
    A running group handles its own sessions (Group.sleep), never this."""
    done = []
    for sid, d in desired.items():
        slot = Slot(sid)
        if not (slot.dir / "KILL").exists():
            continue
        if ("kite" if d.get("feed") == "kite" else "sim:" + sid) in alive:
            continue
        state = {}
        if slot.state.exists():
            state = _read_json(slot.state)
            if not isinstance(state, dict):
                continue                       # cannot be read just now: never guess "no positions"
        broker = state.get("broker") or {}
        if broker.get("positions") or state.get("positions") or (broker.get("live") or {}).get("protect"):
            continue
        msg = "Stopped from the dashboard (no open positions)."
        slot.desired.unlink(missing_ok=True)
        slot.status.write_text("Stopped: " + msg, encoding="utf-8")
        if state:
            try:
                state.update(status="stopped", halted=msg,
                             logs=(state.get("logs") or [])[-299:] + [f"{ist_now():%Y-%m-%d %H:%M}  Session stopped: {msg}"])
                _write_json(slot.state, state)
            except Exception:  # noqa: BLE001
                pass
        if d.get("mode", "paper") != "paper":
            try:
                from .pushbox import push
                push(f"Algo Lab {d.get('mode')} session stopped: {sid}", msg, "/live", "trading")
            except Exception:  # noqa: BLE001
                pass
        (slot.dir / "KILL").unlink(missing_ok=True)
        done.append(sid)
    return done


def run_service(idle: float = 1.0, retry: float = 20.0, max_loops: int | None = None) -> None:
    """Background service for a server: keeps every session chosen in the dashboard running, side by
    side, restarts them after a crash or reboot, and waits (retrying) when they cannot start yet -
    for example until the day's Zerodha login has been done."""
    global _service
    from .config import load_config
    _service = True
    RUNTIME.mkdir(exist_ok=True)
    migrate_legacy()
    write_catalog()
    print("Paper-trading service running. Start and stop sessions from the dashboard.")
    threads: dict[str, threading.Thread] = {}
    retry_at: dict[str, float] = {}
    loops = 0

    def launch(key: str, wanted, kind: str, ids) -> None:
        def body():
            try:
                run_group(load_config(), wanted, kind)
            except (Exception, SystemExit) as e:  # noqa: BLE001
                msg = str(e) or type(e).__name__
                print(f"Sessions {', '.join(ids())} could not run: {msg}")
                for sid in ids():
                    Slot(sid).mk().status.write_text(msg, encoding="utf-8")
                retry_at[key] = _time.time() + retry
        t = threading.Thread(target=body, name=key, daemon=True)
        threads[key] = t
        t.start()

    while max_loops is None or loops < max_loops:
        loops += 1
        SERVICE_BEAT.write_text(str(_time.time()))
        desired = _desired_all()
        for sid in sessions():
            if sid not in desired:
                Slot(sid).status.unlink(missing_ok=True)
        alive = {k for k, t in threads.items() if t.is_alive()}
        for sid in stop_idle_kills(desired, alive):
            desired.pop(sid, None)
        live = {k: v for k, v in desired.items() if v.get("feed") == "kite"}
        if live and not (threads.get("kite") and threads["kite"].is_alive()) and _time.time() >= retry_at.get("kite", 0):
            def wanted_kite():
                return {k: v for k, v in _desired_all().items() if v.get("feed") == "kite"}
            launch("kite", wanted_kite, "kite", lambda: list(wanted_kite()))
        for sid, d in desired.items():
            if d.get("feed") != "sim":
                continue
            key = "sim:" + sid
            if not (threads.get(key) and threads[key].is_alive()) and _time.time() >= retry_at.get(key, 0):
                def wanted_one(sid=sid):
                    d2 = _read_json(Slot(sid).desired)
                    return {sid: d2} if d2 and d2.get("feed") == "sim" else {}
                launch(key, wanted_one, "sim", lambda sid=sid: [sid])
        _time.sleep(idle)
    for t in threads.values():   # tests: let finished groups write their final state
        t.join(timeout=60)
