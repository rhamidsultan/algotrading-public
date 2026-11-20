"""Zerodha Kite Connect: daily login, history download, option-chain lookup and the live quote feed.

Market data needs the paid Kite Connect plan. Zerodha does not serve history for expired option
contracts, so backtests model option prices unless you have recorded or bought real ones."""
from __future__ import annotations

import json
import os
import re
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pandas as pd
import yaml

from .config import DATA, IST, ROOT, RUNTIME, SPOT_DIR, ist_now
from .feeds import MARKET_CLOSE, MARKET_OPEN, Feed, OptionStore
from .models import Contract, Quote

SESSION_FILE = RUNTIME / "kite_session.json"
INTERVALS = {  # name: (minutes, max days per request)
    "minute": (1, 60), "3minute": (3, 100), "5minute": (5, 100), "10minute": (10, 100),
    "15minute": (15, 200), "30minute": (30, 200), "60minute": (60, 400), "day": (1440, 2000),
}
SPOT_SYMBOL, VIX_SYMBOL = "NSE:NIFTY 50", "NSE:INDIA VIX"
FALLBACK_TOKENS = {"NIFTY 50": 256265, "INDIA VIX": 264969}


class KiteError(RuntimeError):
    pass


# ---- login -------------------------------------------------------------
def _read_env_file(path: Path) -> dict:
    out = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_secrets() -> dict:
    key, secret = os.environ.get("KITE_API_KEY"), os.environ.get("KITE_API_SECRET")
    path = ROOT / "secrets.yaml"
    if path.exists():
        k = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kite", {})
        key, secret = key or k.get("api_key"), secret or k.get("api_secret")
        if k.get("env_file"):   # e.g. another app's .env holding KITE_API_KEY / KITE_API_SECRET
            env = _read_env_file(Path(str(k["env_file"])).expanduser())
            key, secret = key or env.get("KITE_API_KEY"), secret or env.get("KITE_API_SECRET")
    if key and not key.startswith("your_") and shared_token_file():
        return {"api_key": key, "api_secret": secret or ""}   # the other app does the logging in
    if not key or not secret or key.startswith("your_"):
        raise KiteError("Zerodha API key/secret not set. Copy secrets.example.yaml to secrets.yaml and fill it in.")
    return {"api_key": key, "api_secret": secret}


def shared_token_file() -> Path | None:
    """Path of another app's saved Zerodha login to reuse (secrets.yaml: kite.token_file), if set.
    Zerodha allows one live login per API key, so two apps on one key must share it."""
    path = ROOT / "secrets.yaml"
    if not path.exists():
        return None
    f = ((yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("kite") or {}).get("token_file")
    return Path(str(f)).expanduser() if f else None


def read_shared_token(path: Path) -> str | None:
    """Find the access token in a JSON, .env-style or plain-text file."""
    text = path.read_text(encoding="utf-8", errors="ignore").strip()
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, dict):
        stack = [obj]
        while stack:
            for k, v in stack.pop().items():
                if isinstance(v, dict):
                    stack.append(v)
                elif isinstance(v, str) and "access_token" in k.lower() and v:
                    return v
    m = re.search(r"access_token[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9]{16,})", text, re.I)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9]{16,}", text) else None


def _new(api_key: str, timeout: int | None = None):
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=api_key, timeout=timeout) if timeout else KiteConnect(api_key=api_key)


def _transient(e: Exception) -> bool:
    """A network hiccup worth trying again (timeouts, dropped connections, Zerodha's 'too many requests')."""
    name = type(e).__name__
    text = str(e).lower()
    return (name in ("ReadTimeout", "ConnectTimeout", "Timeout", "ConnectionError", "NetworkException", "DataException")
            or "timed out" in text or "too many requests" in text or "connection aborted" in text
            or "temporarily unavailable" in text)


def _retry(call, tries: int = 4, waits=(3, 10, 30), log=None):
    """call() again after a short wait when Zerodha is slow (16 Oct: one 7-second read timeout stopped the whole
    daily recording)."""
    for n in range(tries):
        try:
            return call()
        except Exception as e:  # noqa: BLE001
            if n == tries - 1 or not _transient(e):
                raise
            if log:
                log(f"Zerodha slow ({e}); trying again in {waits[min(n, len(waits) - 1)]} s")
            _time.sleep(waits[min(n, len(waits) - 1)])


def login_url() -> str:
    return _new(load_secrets()["api_key"]).login_url()


def complete_login(text: str) -> dict:
    """Accepts the request_token, or the whole address you were redirected to after logging in."""
    if shared_token_file():
        raise KiteError("This platform reuses your other app's Zerodha login; log in there instead.")
    text = text.strip()
    token = parse_qs(urlparse(text).query).get("request_token", [text])[0] if "request_token=" in text else text
    s = load_secrets()
    data = _new(s["api_key"]).generate_session(token, api_secret=s["api_secret"])
    info = {"access_token": data["access_token"], "date": ist_now().date().isoformat(),
            "user": data.get("user_name") or data.get("user_id", "")}
    RUNTIME.mkdir(exist_ok=True)
    SESSION_FILE.write_text(json.dumps(info), encoding="utf-8")
    return info


def last_flush() -> datetime:
    """Zerodha clears every login at about 6 am IST."""
    now = ist_now()
    cutoff = datetime.combine(now.date(), datetime.min.time()).replace(hour=6)
    return cutoff if now >= cutoff else cutoff - timedelta(days=1)


def _issued_since_flush(path: Path) -> bool:
    created = None
    try:
        stamp = json.loads(path.read_text(encoding="utf-8")).get("created")
        if stamp:
            created = datetime.fromisoformat(stamp)
            created = created.astimezone(IST).replace(tzinfo=None) if created.tzinfo else created
    except (ValueError, AttributeError, TypeError):
        pass
    if created is None:
        created = datetime.fromtimestamp(path.stat().st_mtime, IST).replace(tzinfo=None)
    return created >= last_flush()


def session_info() -> dict | None:
    """Today's saved login, or None. Zerodha tokens expire early each morning."""
    shared = shared_token_file()
    if shared:
        try:
            token = read_shared_token(shared) if _issued_since_flush(shared) else None
        except OSError:
            token = None
        return {"access_token": token, "date": ist_now().date().isoformat(), "user": "", "shared": True} if token else None
    if not SESSION_FILE.exists():
        return None
    info = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    return info if info.get("date") == ist_now().date().isoformat() else None


def client(timeout: int | None = None):
    info = session_info()
    if info is None and shared_token_file():
        raise KiteError("Waiting for today's Zerodha login. Do the morning login for your portfolio dashboard; "
                        "this starts by itself a few seconds later.")
    if info is None:
        raise KiteError("Not logged in to Zerodha today. Log in from the dashboard's Data & broker tab "
                        "(or run: python run.py login).")
    kite = _new(load_secrets()["api_key"], timeout)
    kite.set_access_token(info["access_token"])
    return kite


# ---- history -----------------------------------------------------------
def fetch_candles(kite, token: int, start: date, end: date, interval: str) -> pd.DataFrame:
    if interval not in INTERVALS:
        raise KiteError(f"Unknown interval '{interval}'. Use one of: {', '.join(INTERVALS)}")
    span = timedelta(days=INTERVALS[interval][1] - 1)
    rows, cur = [], start
    while cur <= end:
        to = min(cur + span, end)
        a, b = datetime.combine(cur, datetime.min.time()), datetime.combine(to, datetime.max.time().replace(microsecond=0))
        rows += _retry(lambda: kite.historical_data(token, a, b, interval))
        cur = to + timedelta(days=1)
        _time.sleep(0.35)  # stay under 3 requests/second
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])
    df["datetime"] = [d.replace(tzinfo=None) if hasattr(d, "tzinfo") else d for d in df["date"]]
    return df[["datetime", "open", "high", "low", "close"]].drop_duplicates("datetime")


def index_tokens(kite) -> dict:
    tokens = dict(FALLBACK_TOKENS)
    try:
        for i in kite.instruments("NSE"):
            if i["tradingsymbol"] in tokens and i.get("segment") == "INDICES":
                tokens[i["tradingsymbol"]] = i["instrument_token"]
    except Exception:
        pass
    return tokens


def download_spot(start: date, end: date, interval: str = "5minute", kite=None):
    """Download NIFTY 50 and India VIX bars into data/spot/NIFTY_<interval>.csv (merged with any older file)."""
    kite = kite or client()
    tok = index_tokens(kite)
    spot = fetch_candles(kite, tok["NIFTY 50"], start, end, interval)
    if spot.empty:
        raise KiteError("Zerodha returned no NIFTY data for that range.")
    vix = fetch_candles(kite, tok["INDIA VIX"], start, end, interval)
    return save_spot(spot, vix, interval)


def download_spot_via_server(start: date, end: date, interval: str = "5minute", log=print):
    """Same as download_spot, but the server dashboard fetches the bars with its own Zerodha login,
    so this Mac never logs in (a login here would sign the server out). Needs the dashboard section
    in secrets.yaml and the Backtests page installed on the server."""
    import io as _io
    from .publish import PublishError, settings, signed_in_session
    cfg = settings()
    s = signed_in_session(cfg)
    frames = {}
    for sym in ("NIFTY 50", "INDIA VIX"):
        log(f"Fetching {sym} {interval} bars {start} to {end} through the server...")
        r = s.get(f"{cfg['url']}/api/backtests/history", params={"symbol": sym, "from": start.isoformat(),
                  "to": end.isoformat(), "interval": interval}, timeout=600)
        if r.status_code == 404:
            raise PublishError("The server dashboard does not have the history service yet: "
                               "install (or re-install) the Backtests page on the server.")
        if not r.ok:
            try:
                msg = r.json().get("error", r.text)
            except ValueError:
                msg = r.text[:200]
            raise PublishError(f"The server could not fetch {sym}: {msg}")
        frames[sym] = pd.read_csv(_io.StringIO(r.text), parse_dates=["datetime"])
    if frames["NIFTY 50"].empty:
        raise KiteError("Zerodha returned no NIFTY data for that range.")
    return save_spot(frames["NIFTY 50"], frames["INDIA VIX"], interval)


def save_spot(spot: pd.DataFrame, vix: pd.DataFrame, interval: str, name: str | None = None):
    vix = vix[["datetime", "close"]].rename(columns={"close": "vix"})
    df = pd.merge_asof(spot.sort_values("datetime"), vix.sort_values("datetime"), on="datetime") if len(vix) else spot.assign(vix=float("nan"))
    SPOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SPOT_DIR / f"{name or 'NIFTY_' + interval}.csv"
    if path.exists():
        df = pd.concat([pd.read_csv(path, parse_dates=["datetime"]), df]).drop_duplicates("datetime", keep="last")
    df.sort_values("datetime").to_csv(path, index=False)
    return path, len(df)


def option_chain(kite, underlying: str = "NIFTY") -> pd.DataFrame:
    """Today's listed option contracts for the underlying (cached for the day)."""
    cache = DATA / f"chain_{underlying}_{ist_now().date().isoformat()}.csv"
    if cache.exists():
        return pd.read_csv(cache, parse_dates=["expiry"]).assign(expiry=lambda d: d.expiry.dt.date)
    df = None
    try:  # read Zerodha's CSV straight into a table: far less memory than kite.instruments()
        import io
        import requests
        root = getattr(kite, "root", None) or "https://api.kite.trade"
        r = requests.get(f"{root}/instruments/NFO", timeout=60, headers={
            "X-Kite-Version": "3", "Authorization": f"token {kite.api_key}:{kite.access_token}"})
        r.raise_for_status()
        raw = pd.read_csv(io.StringIO(r.text), usecols=["instrument_token", "tradingsymbol", "name", "expiry",
                                                        "strike", "lot_size", "instrument_type"])
        raw = raw[(raw["name"] == underlying) & raw["instrument_type"].isin(["CE", "PE"])]
        df = pd.DataFrame({"expiry": raw["expiry"].astype(str), "strike": raw["strike"].astype(float).astype(int),
                           "right": raw["instrument_type"], "tradingsymbol": raw["tradingsymbol"],
                           "token": raw["instrument_token"], "lot_size": raw["lot_size"]})
    except Exception:
        df = None
    if df is None or df.empty:
        rows = [{"expiry": i["expiry"], "strike": int(i["strike"]), "right": i["instrument_type"],
                 "tradingsymbol": i["tradingsymbol"], "token": i["instrument_token"], "lot_size": i["lot_size"]}
                for i in kite.instruments("NFO")
                if i.get("name") == underlying and i.get("instrument_type") in ("CE", "PE")]
        df = pd.DataFrame(rows)
    if df.empty:
        raise KiteError(f"No {underlying} options found in Zerodha's instrument list.")
    df["expiry"] = [e if isinstance(e, date) else date.fromisoformat(str(e)[:10]) for e in df["expiry"]]
    DATA.mkdir(exist_ok=True)
    for old in DATA.glob(f"chain_{underlying}_*.csv"):
        old.unlink()
    df.to_csv(cache, index=False)
    return df


def download_options(strikes_each_side: int = 10, expiries: int = 2, interval: str = "5minute",
                     days: int = 45, kite=None, cfg: dict | None = None, progress=None,
                     puts_below: int | None = None, plan_only: bool = False):
    """Save real candles for currently listed NIFTY options near the money into the option store.
    Calls: `strikes_each_side` strikes either side of ATM. Puts: the same above ATM and `puts_below`
    strikes below it (default: same as above), so far out-of-the-money puts such as the Blaze
    Butterfly's lower wing are recorded too. Run it daily and real option history accumulates."""
    from .config import load_config
    cfg = cfg or load_config()
    kite = kite or client()
    und, step = cfg["market"]["underlying"], cfg["market"]["strike_step"]
    chain = _retry(lambda: option_chain(kite, und))
    spot = _retry(lambda: kite.quote([SPOT_SYMBOL]))[SPOT_SYMBOL]["last_price"]
    atm = round(spot / step) * step
    today = ist_now().date()
    below = strikes_each_side if puts_below is None else max(int(puts_below), 0)
    near = chain.expiry.isin(sorted(e for e in chain.expiry.unique() if e >= today)[:expiries])
    up = (chain.strike - atm <= strikes_each_side * step)
    down = (atm - chain.strike <= strikes_each_side * step) | ((chain.right == "PE") & (atm - chain.strike <= below * step))
    wanted = chain[near & up & down]
    if plan_only:
        out = []
        for e, g in wanted.groupby("expiry"):
            for r_ in ("CE", "PE"):
                k = g[g.right == r_].strike
                if len(k):
                    out.append(f"  expiry {e}: {len(k)} {r_} strikes {int(k.min())} to {int(k.max())}")
        return f"NIFTY {spot:,.2f} (ATM {atm})\n" + "\n".join(out) + f"\n  {len(wanted)} contracts in all"
    store, minutes = OptionStore(und), INTERVALS[interval][0]
    failed = []
    for n, row in enumerate(wanted.itertuples(), 1):
        try:
            df = fetch_candles(kite, int(row.token), today - timedelta(days=days), today, interval)
        except Exception as e:  # noqa: BLE001 - one contract that keeps failing does not stop the others
            if not _transient(e):
                raise
            failed.append(f"{int(row.strike)}{row.right} {row.expiry}")
            continue
        if len(df):
            frame = pd.DataFrame({"time": df["datetime"] + pd.Timedelta(minutes=minutes), "last": df["close"], "bid": "", "ask": ""})
            store.save_frame(Contract(und, row.expiry, int(row.strike), row.right), frame)
        if progress:
            progress(n, len(wanted))
    if failed and len(failed) > len(wanted) // 2:
        raise KiteError(f"Zerodha kept timing out: {len(failed)} of {len(wanted)} contracts not fetched")
    return len(wanted) - len(failed) if not failed else f"{len(wanted) - len(failed)} (skipped after retries: {', '.join(failed[:5])}{'…' if len(failed) > 5 else ''})"


# ---- live feed ---------------------------------------------------------
class KiteFeed(Feed):
    """Live Zerodha prices during market hours. Fills use the real best bid / ask.

    With a live price stream (stream.KiteStream, the server's default) prices arrive tick by tick and
    the strategies are stepped every `paper.stream_step_seconds` (1 s); only options the stream has
    not delivered yet are fetched with a quote request. Whenever the stream is not healthy the feed
    polls quotes every `paper.live_poll_seconds` (2 s) as before, so a dropped connection never
    stops trading. Once a minute the stream is cross-checked against a quote request."""
    gives_day_ohlc = True    # every quote carries the contract's open and high so far today

    def __init__(self, kite, cfg: dict, control=None, record: bool = True, log=print, stream=None):
        super().__init__(cfg)
        self.kite, self.control, self.log = kite, control, log
        p = cfg["paper"]
        self.poll = float(p.get("live_poll_seconds", 2))
        self.stream = stream
        self.stream_step = float(p.get("stream_step_seconds", 1))
        self.keep_seconds = float(p.get("stream_keep_seconds", 300))
        self.band = bool(p.get("record_band", True))
        self._token = None
        self._refresh_login()
        self._chain_day = None      # contract list is fetched at the first poll in market hours
        self._symbols, self._expiries, self._tokens = {}, [], {}
        self._idx_tokens = dict(FALLBACK_TOKENS)
        self.store = OptionStore(self.underlying) if record else None
        self._cache: dict = {}
        self._watch: dict = {}      # contracts in use -> ticks since last asked for; fetched with the index
        self._asked: dict = {}      # stream mode: contract -> time it was last asked for
        self._band: set = set()     # stream mode: contracts near the money, recorded every minute
        self._band_at = 0.0
        self._recorded: dict = {}   # contract -> minute last written to the option store
        self.source = "poll"
        self.stats = {"steps_stream": 0, "steps_poll": 0, "rest_calls": 0, "fallbacks": 0, "check": None}
        self._was_stream = False
        self._status_at = 0.0
        self._check_at = _time.time()   # first cross-check a minute after starting
        self.sec1 = None                # 1-second bars from the stream (algolab/sec1.py), when recording
        self._sec1_cfg = None
        if stream is not None and record:
            try:
                from . import sec1
                self._sec1_cfg = sec1.settings(cfg)
                if self._sec1_cfg["record"]:
                    self.sec1 = sec1.SecondRecorder()
                    stream.on_ticks = self.sec1.on_ticks
            except Exception as e:  # noqa: BLE001 - recording is optional; trading is not
                self.log(f"1-second recording off: {e}")
                self.sec1 = None

    def _take_prev_close(self, idx: dict | None, vix: dict | None) -> None:
        """Zerodha's quote carries the previous day's close in ohlc.close (for the index and VIX)."""
        c = float(((idx or {}).get("ohlc") or {}).get("close") or 0)
        v = float(((vix or {}).get("ohlc") or {}).get("close") or 0)
        if c > 0:
            self.prev_close = c
        if v > 0:
            self.prev_vix_close = v

    def _load_previous_close(self) -> None:
        """Once a day (also in stream mode, where the index comes from ticks): the previous close of NIFTY and VIX."""
        try:
            q = self._rest([SPOT_SYMBOL, VIX_SYMBOL])
            self.prev_close = self.prev_vix_close = None
            self._take_prev_close(q.get(SPOT_SYMBOL), q.get(VIX_SYMBOL))
        except Exception as e:  # noqa: BLE001 - the strategy then uses its own daily record
            self.log(f"Previous close not read: {e}")

    def _load_chain(self) -> None:
        chain = option_chain(self.kite, self.underlying)
        self._symbols = {(r.expiry, int(r.strike), r.right): r.tradingsymbol for r in chain.itertuples()}
        self._tokens = ({(r.expiry, int(r.strike), r.right): int(r.token) for r in chain.itertuples()}
                        if "token" in chain else {})
        self._expiries = sorted(set(chain.expiry))
        self.lot_size = int(chain.lot_size.mode().iloc[0])
        self._chain_day = ist_now().date()
        self._load_previous_close()

    def _refresh_login(self) -> None:
        """Zerodha logins expire every morning; adopt the new one as soon as it is saved."""
        try:
            info = session_info()
            if info and info["access_token"] != self._token:
                if self._token is not None:
                    self.kite.set_access_token(info["access_token"])
                    self.log("Picked up today's Zerodha login")
                self._token = info["access_token"]
                if self.stream is not None:
                    self.stream.set_token(info["access_token"])
        except Exception:
            pass

    def _sleep(self, seconds: float) -> bool:
        if self.control:
            return self.control.sleep(seconds)
        _time.sleep(seconds)
        return True

    def _rest(self, symbols):
        self.stats["rest_calls"] += 1
        return self.kite.quote(symbols)

    # ---- the live price stream ---------------------------------------------------------------
    def _tok(self, c: Contract):
        return self._tokens.get((c.expiry, c.strike, c.right))

    def _stream_quote(self, c: Contract) -> Quote | None:
        if self.stream is None:
            return None
        t = self.stream.tick(self._tok(c))
        if not t or not t.get("last"):
            return None
        return Quote(float(t["last"]), t.get("bid"), t.get("ask"), "market",
                     t.get("open") or None, t.get("high") or None)

    def _update_band(self, spot: float) -> None:
        """Calls and puts near the money on the nearest expiries (config `collect:`), kept subscribed
        so their prices are recorded every minute for backtests."""
        if not self.band or _time.time() - self._band_at < 60:
            return
        self._band_at = _time.time()
        col = self.cfg.get("collect", {})
        n, below, k = int(col.get("strikes_each_side", 10)), int(col.get("puts_below", 30)), int(col.get("expiries", 2))
        if self.sec1 is not None and self._sec1_cfg:   # the 1-second recorder wants a wider band
            n = max(n, self._sec1_cfg["strikes_each_side"])
            k = max(k, self._sec1_cfg["expiries"])
        step = self.strike_step
        atm = int(round(spot / step) * step)
        band = set()
        for e in self.expiries()[:k]:
            for i in range(-n, n + 1):
                band.add(Contract(self.underlying, e, atm + i * step, "CE"))
            for i in range(-max(n, below), n + 1):
                band.add(Contract(self.underlying, e, atm + i * step, "PE"))
        self._band = {c for c in band if self._tok(c)}
        self._sec1_names()

    def _sec1_names(self) -> None:
        """Tell the 1-second recorder which instrument each token is (index, VIX, band, options in use)."""
        if self.sec1 is None:
            return
        try:
            from .sec1 import contract_name
            names = {self._idx_tokens.get("NIFTY 50"): "NIFTY", self._idx_tokens.get("INDIA VIX"): "INDIAVIX"}
            for c in set(self._band) | set(self._asked):
                tok = self._tok(c)
                if tok:
                    names[tok] = contract_name(c.expiry, c.strike, c.right)
            self.sec1.set_names(names)
        except Exception as e:  # noqa: BLE001
            self.log(f"1-second recording off: {e}")
            self.sec1 = None

    def _subscribe(self) -> None:
        now = _time.time()
        self._asked = {c: t for c, t in self._asked.items() if now - t <= self.keep_seconds}
        tokens = {self._idx_tokens.get("NIFTY 50"), self._idx_tokens.get("INDIA VIX")}
        tokens |= {self._tok(c) for c in list(self._asked) + list(self._band)}
        self.stream.want({t for t in tokens if t})

    def _stream_step(self, now) -> bool:
        """Prices for this moment from the stream. False when the stream cannot provide them."""
        st = self.stream
        spot_t = st.tick(self._idx_tokens.get("NIFTY 50"))
        if not spot_t or not spot_t.get("last"):
            return False
        stamp = spot_t.get("ts")
        if hasattr(stamp, "date") and stamp.date() != now.date():
            return False   # a holiday missing from config.yaml: the stream replays the last day's prices
        vix_t = st.tick(self._idx_tokens.get("INDIA VIX"))
        self.now, self.spot = now, float(spot_t["last"])
        self.vix = vix_t["last"] if vix_t else self.vix
        self._cache = {}
        missing = []
        for c in list(self._asked):
            q = self._stream_quote(c)
            if q:
                self._cache[c] = q
                self._record(c, q)
            else:
                missing.append(c)
        if missing:   # just subscribed (no tick yet) or not trading: one quote request for those
            syms = {c: self._symbols.get((c.expiry, c.strike, c.right)) for c in missing}
            try:
                got = self._rest([f"NFO:{s}" for s in syms.values() if s]) if any(syms.values()) else {}
            except Exception as e:  # noqa: BLE001
                self.log(f"Quote failed: {e}")
                got = {}
            for c, sym in syms.items():
                q = self._parse(c, got.get(f"NFO:{sym}")) if sym else None
                if q:
                    self._cache[c] = q
        for c in self._band:
            if c not in self._cache:
                q = self._stream_quote(c)
                if q:
                    self._record(c, q)
        self._update_band(self.spot)
        self._subscribe()
        if self.sec1 is not None:
            if any(self._tok(c) and self._tok(c) not in self.sec1.names for c in self._asked):
                self._sec1_names()          # a session started using a new option
            self.sec1.flush()
        self._watch = {c: 0 for c in self._asked}   # what a fallback to polling would fetch
        return True

    def _cross_check(self) -> None:
        """Once a minute: compare the stream with a quote request (written to runtime/stream_check.csv)."""
        if self.stream is None or _time.time() - self._check_at < 60 or not self.stream.healthy():
            return
        self._check_at = _time.time()
        sample = [c for c in self._asked if self._stream_quote(c)][:20]
        syms = {c: self._symbols.get((c.expiry, c.strike, c.right)) for c in sample}
        try:
            got = self._rest([SPOT_SYMBOL] + [f"NFO:{s}" for s in syms.values() if s])
        except Exception:  # noqa: BLE001
            return
        st = self.stream.tick(self._idx_tokens.get("NIFTY 50")) or {}
        d_spot = abs(float(st.get("last") or 0) - float((got.get(SPOT_SYMBOL) or {}).get("last_price") or 0))
        diffs = []
        for c, sym in syms.items():
            r = (got.get(f"NFO:{sym}") or {}).get("last_price")
            q = self._stream_quote(c)
            if r and q and q.last:
                diffs.append(abs(q.last - float(r)) / float(r) * 100)
        ages = [_time.time() - (self.stream.ticks.get(self._tok(c)) or {}).get("rx", _time.time()) for c in sample]
        check = {"time": f"{ist_now():%H:%M:%S}", "spot_diff": round(d_spot, 2), "options": len(diffs),
                 "max_option_diff_pct": round(max(diffs), 2) if diffs else None,
                 "median_tick_age_s": round(sorted(ages)[len(ages) // 2], 1) if ages else None}
        self.stats["check"] = check
        try:
            path = RUNTIME / "stream_check.csv"
            new = not path.exists()
            with path.open("a", encoding="utf-8") as f:
                if new:
                    f.write("time,spot_diff,options,max_option_diff_pct,median_tick_age_s\n")
                f.write(f"{ist_now():%Y-%m-%d} {check['time']},{check['spot_diff']},{check['options']},"
                        f"{check['max_option_diff_pct']},{check['median_tick_age_s']}\n")
        except OSError:
            pass

    def _write_status(self) -> None:
        if _time.time() - self._status_at < 5:
            return
        self._status_at = _time.time()
        out = {"time": ist_now().isoformat(timespec="seconds"), "source": self.source,
               "poll_seconds": self.poll, "stream_step_seconds": self.stream_step,
               "stream": self.stream.status() if self.stream is not None else None,
               "band": len(self._band), "in_use": len(self._asked) if self.stream is not None else len(self._watch),
               **self.stats}
        try:
            RUNTIME.mkdir(exist_ok=True)
            tmp = RUNTIME / "feed_status.tmp"
            tmp.write_text(json.dumps(out, default=str), encoding="utf-8")
            tmp.replace(RUNTIME / "feed_status.json")
        except OSError:
            pass

    # ---- stepping ----------------------------------------------------------------------------
    def market_open(self, now) -> bool:
        """Weekdays 9:15-15:30 IST, except the NSE holidays in config.yaml (market: holidays). On a
        holiday Zerodha still answers with the last day's prices, so the feed must not step at all."""
        return self.is_trading_day(now.date()) and MARKET_OPEN <= now.time() <= MARKET_CLOSE

    def __iter__(self):
        while True:
            now = ist_now()
            if not self.market_open(now):
                self.source = "closed"
                if self.sec1 is not None and self.sec1._open:
                    self.sec1.flush(force=True)  # the day's last seconds
                if self.stream is not None and self.stream.running():
                    self.stream.stop()          # reconnects at the next open (with that day's login)
                self._write_status()
                if not self._sleep(20):
                    return
                continue
            self._refresh_login()
            try:
                if self._chain_day != now.date():
                    self._load_chain()
            except Exception as e:  # noqa: BLE001
                self.log(f"Quote failed: {e}")
                if not self._sleep(15):
                    return
                continue
            if self.stream is not None:
                self.stream.start()
                if not self._asked:
                    self._asked = {c: _time.time() for c in self._watch}
                self._subscribe()
                if self.stream.healthy() and self._stream_step(now):
                    if not self._was_stream:
                        self.log("Prices: Zerodha live stream")
                    self._was_stream, self.source = True, "stream"
                    self.stats["steps_stream"] += 1
                    self._cross_check()
                    self._write_status()
                    yield now
                    if not self._sleep(self.stream_step):
                        return
                    continue
                if self._was_stream:
                    self.stats["fallbacks"] += 1
                    self.log("Price stream not available: polling quotes every "
                             f"{self.poll:g} s until it is back")
                self._was_stream = False
            try:
                # one request per update: the index, VIX and every option the strategy is using
                used = set(self._watch) | set(self._asked)
                watched = {c: self._symbols.get((c.expiry, c.strike, c.right)) for c in used}
                q = self._rest([SPOT_SYMBOL, VIX_SYMBOL] + [f"NFO:{s}" for s in watched.values() if s])
                idx = q[SPOT_SYMBOL]
            except Exception as e:  # network blip, expired login...
                self.log(f"Quote failed: {e}")
                if not self._sleep(15):
                    return
                continue
            stamp = idx.get("timestamp")
            if hasattr(stamp, "date") and stamp.date() != now.date():  # market holiday: stale prices
                if not self._sleep(60):
                    return
                continue
            self.now, self.spot = now, float(idx["last_price"])
            self.vix = (q.get(VIX_SYMBOL) or {}).get("last_price")
            self._take_prev_close(idx, q.get(VIX_SYMBOL))
            self._cache = {}
            for c, sym in watched.items():
                quote = self._parse(c, q.get(f"NFO:{sym}")) if sym else None
                if quote:
                    self._cache[c] = quote
            self._watch = {c: n + 1 for c, n in self._watch.items() if n < 3}   # drop options no longer asked for
            self.source = "poll"
            self.stats["steps_poll"] += 1
            self._write_status()
            yield now
            if not self._sleep(self.poll):
                return

    def expiries(self) -> list[date]:
        return [e for e in self._expiries if e >= self.now.date()]

    def lot_size_for(self, expiry: date) -> int:
        return self.lot_size  # what Zerodha reports today

    def _mark(self, c: Contract) -> None:
        self._watch[c] = 0          # still in use: keep fetching it with every update
        if self.stream is not None:
            self._asked[c] = _time.time()

    def quote(self, c: Contract) -> Quote:
        self._mark(c)
        if c in self._cache:
            return self._cache[c]
        quote = self._stream_quote(c)
        sym = self._symbols.get((c.expiry, c.strike, c.right))
        if quote is None and sym:   # first time this option is needed: fetch it now
            try:
                quote = self._parse(c, self._rest([f"NFO:{sym}"]).get(f"NFO:{sym}"))
            except Exception as e:
                self.log(f"Option quote failed for {sym}: {e}")
        if quote is None:
            quote = self.model_quote(c)
        self._cache[c] = quote
        return quote

    def listed_strikes(self, expiry: date, right: str) -> set | None:
        return {k for (e, k, r) in self._symbols if e == expiry and r == right}

    def quotes(self, contracts) -> dict:
        """Several options at once: from the stream where it has them, the rest in one request
        (Zerodha allows about one quote request a second)."""
        contracts = list(dict.fromkeys(contracts))
        for c in contracts:
            if c not in self._cache:
                q = self._stream_quote(c)
                if q:
                    self._cache[c] = q
        need = [c for c in contracts if c not in self._cache]
        syms = {c: self._symbols.get((c.expiry, c.strike, c.right)) for c in need}
        got = {}
        if any(syms.values()):
            try:
                got = self._rest([f"NFO:{s}" for s in syms.values() if s])
            except Exception as e:
                self.log(f"Option quotes failed: {e}")
        for c in need:
            sym = syms[c]
            quote = self._parse(c, got.get(f"NFO:{sym}")) if sym else None
            self._cache[c] = quote or self.model_quote(c)
        for c in contracts:
            self._mark(c)
        return {c: self._cache[c] for c in contracts}

    def _record(self, c: Contract, quote: Quote) -> None:
        minute = self.now.replace(second=0, microsecond=0)
        if self.store and self._recorded.get(c) != minute:   # keep one price a minute for backtests
            self._recorded[c] = minute
            self.store.append(c, self.now, quote.last, quote.bid, quote.ask)

    def _parse(self, c: Contract, d: dict | None) -> Quote | None:
        if not d:
            return None
        depth = d.get("depth") or {}
        bid = ((depth.get("buy") or [{}])[0].get("price")) or None
        ask = ((depth.get("sell") or [{}])[0].get("price")) or None
        last = d.get("last_price") or ((bid + ask) / 2 if bid and ask else None)
        if not last:
            return None
        ohlc = d.get("ohlc") or {}
        quote = Quote(float(last), bid, ask, "market", ohlc.get("open") or None, ohlc.get("high") or None)
        self._record(c, quote)
        return quote


def make_stream(kite, cfg: dict, log=print):
    """The live price stream for the server's sessions, or None (config `paper.price_source: poll`,
    or websocket-client not installed)."""
    if str(cfg.get("paper", {}).get("price_source", "stream")).lower() != "stream":
        return None
    try:
        import websocket  # noqa: F401  (websocket-client)
    except ImportError:
        log("Price stream unavailable (websocket-client is not installed): polling quotes")
        return None
    from .stream import KiteStream
    return KiteStream(kite.api_key, kite.access_token, log=log,
                      stale_after=float(cfg["paper"].get("stream_stale_seconds", 5)))


# ---- daily collection (server) and copying to another computer (Mac) ------------------------
def collect_daily(cfg: dict | None = None, kite=None, plan_only: bool = False) -> str:
    """After the close: add the latest NIFTY / India VIX bars and real candles for the listed options
    near the money. Run once each trading day on the server; backtests anywhere can then use them.
    plan_only: just say which contracts would be stored (nothing is downloaded)."""
    kite = kite or client(timeout=30)        # the recording can wait for a slow answer
    if plan_only:
        from .config import load_config
        col = (cfg or load_config()).get("collect", {})
        return download_options(int(col.get("strikes_each_side", 10)), int(col.get("expiries", 2)), kite=kite,
                                cfg=cfg, puts_below=int(col.get("puts_below", 30)), plan_only=True)
    today = ist_now().date()
    path = SPOT_DIR / "NIFTY_5minute.csv"
    start = today - timedelta(days=365)
    if path.exists():
        last = pd.read_csv(path, usecols=["datetime"], parse_dates=["datetime"])["datetime"].max().date()
        start = max(start, last - timedelta(days=1))
    from .config import load_config
    col = (cfg or load_config()).get("collect", {})
    _, bars = download_spot(start, today, "5minute", kite=kite)
    minute = collect_minute(today, kite)
    contracts = download_options(int(col.get("strikes_each_side", 10)), int(col.get("expiries", 2)), "5minute",
                                 days=int(col.get("days", 7)), kite=kite, cfg=cfg,
                                 puts_below=int(col.get("puts_below", 30)))
    return f"{ist_now():%Y-%m-%d %H:%M}  NIFTY bars stored: {bars:,}; {minute}; option contracts updated: {contracts}"


MINUTE_DAYS = 59    # Zerodha keeps about 60 days of 1-minute history


def collect_minute(today: date, kite) -> str:
    """1-minute bars of NIFTY, Bank Nifty and Sensex (each with India VIX) into data/spot/<INDEX>_minute.csv: the one
    price set per index the screens and backtests use (algolab/datasets.py). Picks up from the last stored bar, so a
    missed day is filled on the next run (up to MINUTE_DAYS back). The TrueData 1-minute history is put in front
    the first time. Never raises: the 5-minute bars and option prices are stored even if this fails."""
    from . import datasets
    parts = []
    try:
        vix_tok = index_tokens(kite).get("INDIA VIX", FALLBACK_TOKENS["INDIA VIX"])
    except Exception:  # noqa: BLE001
        vix_tok = FALLBACK_TOKENS["INDIA VIX"]
    vix_cache: dict = {}
    for u in datasets.INDICES:
        name = datasets.canonical(u)
        path = SPOT_DIR / f"{name}.csv"
        try:
            seeded = datasets.seed_from_truedata(u, SPOT_DIR)
            note = f" (+{seeded:,} TrueData)" if seeded else ""
            start = today - timedelta(days=MINUTE_DAYS)
            if path.exists():
                last = pd.read_csv(path, usecols=["datetime"], parse_dates=["datetime"])["datetime"].max().date()
                if last < start:
                    note += f" (gap: bars after {last} are older than Zerodha keeps and stay missing)"
                start = max(start, last - timedelta(days=1))
            spot = fetch_candles(kite, datasets.KITE_INDEX[u][1], start, today, "minute")
            if spot.empty:
                parts.append(f"{u} none from Zerodha{note}")
                continue
            if start not in vix_cache:
                vix_cache[start] = fetch_candles(kite, vix_tok, start, today, "minute")
            _, rows = save_spot(spot, vix_cache[start], "minute", name=name)
            parts.append(f"{u} {rows:,}{note}")
        except Exception as e:  # noqa: BLE001 - one index failing must not stop the others
            parts.append(f"{u} NOT stored ({type(e).__name__}: {str(e)[:100]})")
    return "1-minute bars stored: " + ", ".join(parts)


def sync_from_server(host: str, key_file: str, remote: str = "/opt/algolab/data") -> str:
    """Copy the price data collected on the server into this computer's data folder."""
    import shlex
    import shutil
    import subprocess
    if not host or not key_file:
        raise KiteError("Enter the server address (like ubuntu@YOUR_SERVER_IP) and the key file first.")
    key = str(Path(key_file).expanduser())
    if not Path(key).exists():
        raise KiteError(f"Key file not found: {key}")
    opts = ["-i", key, "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]
    done = []
    incoming = DATA / ".server_spot"   # merged below, so a longer history already here is kept
    if incoming.exists():
        shutil.rmtree(incoming)
    for sub, local in (("spot", incoming), ("options", DATA / "options"), ("sec1", DATA / "sec1")):
        local.mkdir(parents=True, exist_ok=True)
        if shutil.which("rsync"):
            cmd = ["rsync", "-az", "--exclude", "SAMPLE_*"] + (["--delete"] if sub == "sec1" else []) + [   # 1-second
                   "-e", " ".join(shlex.quote(x) for x in ["ssh"] + opts), f"{host}:{remote}/{sub}/", f"{local}/"]  # bars: same days
        else:
            cmd = ["scp", "-r", "-q"] + opts + [f"{host}:{remote}/{sub}/.", str(local)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if r.returncode and "Permission denied" in (r.stderr or "") and cmd[0] == "rsync":
            # the data belongs to the engine's user on the server: read it through sudo (read-only copy)
            r = subprocess.run(cmd[:2] + ["--rsync-path", "sudo -n rsync"] + cmd[2:], capture_output=True, text=True, timeout=900)
        if r.returncode and sub == "sec1":
            continue                     # none recorded on the server yet (or an older server): the rest still counts
        if r.returncode:
            raise KiteError(f"Copying {sub} failed: {(r.stderr or r.stdout).strip()[-400:]}")
        done.append(sub)
    SPOT_DIR.mkdir(parents=True, exist_ok=True)
    for f in incoming.glob("*.csv"):
        if f.name.startswith("SAMPLE") or f.name.startswith("TD_") or ".before-fill-" in f.name:
            continue        # only the index sets (and the 5-minute file); old copies live in data/archive
        new = pd.read_csv(f, parse_dates=["datetime"])
        mine = SPOT_DIR / f.name
        if mine.exists():
            new = pd.concat([pd.read_csv(mine, parse_dates=["datetime"]), new]).drop_duplicates("datetime", keep="last")
        new.sort_values("datetime").to_csv(mine, index=False)
    shutil.rmtree(incoming, ignore_errors=True)
    try:                                   # one 1-minute set per index; old copies to data/archive (datasets.py)
        from .datasets import organize
        organize(SPOT_DIR)
    except Exception:  # noqa: BLE001
        pass
    files = sum(1 for _ in (DATA / "options").rglob("*.csv"))
    return f"Copied from the server: NIFTY history and {files:,} option price files."
