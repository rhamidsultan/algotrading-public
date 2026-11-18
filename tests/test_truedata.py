"""TrueData downloader against a stand-in TrueData server (no network)."""
import struct
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import lz4.block
import numpy as np
import pandas as pd
import pytest

from algolab import truedata
from algolab.config import load_config
from algolab.feeds import OptionStore, load_dataset
from algolab.models import Contract
from algolab.pricing import bs_price

START, END = date(2026, 4, 1), date(2026, 5, 15)
HOLIDAYS = {date(2026, 4, 3), date(2026, 4, 14), date(2026, 5, 1)}


def _days():
    d = START
    while d <= END:
        if d.weekday() < 5 and d not in HOLIDAYS:
            yield d
        d += timedelta(days=1)


def _spot():
    rng = np.random.default_rng(1)
    rows, px = [], 24000.0
    for d in _days():
        for i in range(375):
            px *= float(np.exp(rng.normal(0, 0.0004)))
            rows.append((datetime.combine(d, datetime.min.time()) + timedelta(minutes=555 + i), px))
    return pd.DataFrame(rows, columns=["timestamp", "close"])


SPOT = _spot()


def _expiries():
    out = []
    d = START
    while d <= END + timedelta(days=30):
        if d.weekday() == 1:
            e = d
            while e in HOLIDAYS:
                e -= timedelta(days=1)
            out.append(e)
        d += timedelta(days=1)
    return out


BANK = 2.3
SENSEX = 3.3
STOCKS = {"RELIANCE", "M&M", "BAJAJ-AUTO"}


def _sensex_expiries():
    """Thursdays from Sep 2025 (the period tested is 2026), holiday-shifted like NSE's."""
    out, d = [], START
    while d <= END + timedelta(days=30):
        if d.weekday() == 3:
            e = d
            while e in HOLIDAYS:
                e -= timedelta(days=1)
            out.append(e)
        d += timedelta(days=1)
    return out


def _monthly():
    return sorted({(e.year, e.month): e for e in _expiries()}.values())


def _pack(text: str) -> bytes:
    raw = text.encode()
    comp = lz4.block.compress(raw, store_size=False)
    return struct.pack("<II", len(raw), len(comp)) + comp


class Resp:
    def __init__(self, code, content=b"", js=None):
        self.status_code, self.content, self._js = code, content, js
        self.text = content.decode(errors="ignore") if isinstance(content, bytes) else content

    def json(self):
        return self._js


class FakeTD:
    """Mimics auth.truedata.in + history.truedata.in closely enough to exercise the downloader."""

    def __init__(self, max_days=12, throttle_every=0):
        self.max_days, self.throttle_every, self.calls = max_days, throttle_every, 0

    def post(self, url, data=None, timeout=None):
        assert url == truedata.AUTH_URL
        if data["password"] != "pw":
            return Resp(200, b"", {"error": "invalid_grant", "error_description": "bad password"})
        return Resp(200, b"", {"access_token": "tok", "expires_in": 3600})

    def get(self, url, params=None, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok"
        self.calls += 1
        if self.throttle_every and self.calls % self.throttle_every == 0:
            return Resp(429, b"")
        ep = urlparse(url).path.strip("/")
        if ep == "getbhavcopy":
            d = date.fromisoformat(params["date"])
            if params["segment"] not in ("FO", "BSEFO"):
                return Resp(400, b"Invalid segment")
            if d not in set(_days()):
                return Resp(404, b"No Data Exists")
            if params["segment"] == "BSEFO":
                spot = SPOT[SPOT.timestamp.dt.date == d].close.iloc[-1] * SENSEX
                rows = [(f"SENSEX{e:%y%m%d}{k}{r}", 1.0) for e in _sensex_expiries() if d <= e <= d + timedelta(days=20)
                        for k in range(int(spot * 0.97) // 100 * 100, int(spot * 1.03), 100) for r in ("CE", "PE")]
                return Resp(200, _pack(pd.DataFrame(rows, columns=["Symbol", "Close"]).to_csv(index=False)))
            rows = [("NIFTY-I", 24000)]
            spot = SPOT[SPOT.timestamp.dt.date == d].close.iloc[-1]
            for e in _expiries():
                if d <= e <= d + timedelta(days=50):
                    for k in range(int(spot * 0.85) // 50 * 50, int(spot * 1.15), 50):
                        for r in ("CE", "PE"):
                            rows.append((f"NIFTY{e:%y%m%d}{k}{r}", 1.0))
            for e in _monthly():
                if d <= e <= d + timedelta(days=80):
                    bank = spot * BANK
                    for k in range(int(bank * 0.85) // 100 * 100, int(bank * 1.15), 100):
                        for r in ("CE", "PE"):
                            rows.append((f"BANKNIFTY{e:%y%m%d}{k}{r}", 1.0))
            df = pd.DataFrame(rows, columns=["Symbol", "Close"])
            return Resp(200, _pack(df.to_csv(index=False)))
        if ep == "getticks":
            self.tick_calls = getattr(self, "tick_calls", 0) + 1
            if getattr(self, "refuse_ticks", False):
                return Resp(403, b"Tick history not in your plan")
            a = datetime.strptime(params["from"], "%y%m%dT%H:%M:%S")
            if params["bidask"] != 1 or a.date() < END - timedelta(days=7):   # about 5 trading days kept
                return Resp(404, b"No Data Exists")
            w = SPOT[SPOT.timestamp.dt.date == a.date()].head(30)
            if w.empty or (truedata.parse_symbol(params["symbol"]) is None
                           and params["symbol"] not in ("NIFTY 50", "NIFTY-I")):
                return Resp(404, b"No Data Exists")
            ts = list(w.timestamp) + list(w.timestamp)   # two ticks in the same second must both be kept
            out = pd.DataFrame({"timestamp": ts, "ltp": 100.0, "volume": 75, "oi": 1000,
                                "bid": 99.9, "bidqty": 150, "ask": 100.1, "askqty": 75})
            return Resp(200, _pack(out.to_csv(index=False, date_format="%Y-%m-%dT%H:%M:%S")))
        assert ep == "getbars"
        a = datetime.strptime(params["from"], "%y%m%dT%H:%M:%S")
        b = datetime.strptime(params["to"], "%y%m%dT%H:%M:%S")
        if (b - a).days + 1 > self.max_days:
            return Resp(400, b"Duration too long")
        sym = params["symbol"]
        w = SPOT[(SPOT.timestamp >= a) & (SPOT.timestamp <= b)]
        if params["interval"] == "eod":
            w = w.groupby(w.timestamp.dt.date).tail(1)
        if truedata.parse_symbol(sym) and truedata.parse_symbol(sym).underlying == "BANKNIFTY" or sym in ("NIFTY BANK", "BANKNIFTY-I"):
            w = w.assign(close=w.close * BANK)
        if truedata.parse_symbol(sym) and truedata.parse_symbol(sym).underlying == "SENSEX" or sym in ("SENSEX", "SENSEX-I"):
            w = w.assign(close=w.close * SENSEX)
        if sym in STOCKS or sym[:-2] in STOCKS:
            w = w.assign(close=w.close / 10)
            out = w.assign(open=w.close, high=w.close, low=w.close, volume=100, oi=0 if sym in STOCKS else 500)
            return Resp(200, _pack(out[["timestamp", "open", "high", "low", "close", "volume", "oi"]]
                                   .to_csv(index=False, date_format="%Y-%m-%dT%H:%M:%S")))
        if sym in ("NIFTY 50", "NIFTY BANK", "SENSEX"):
            out = w.assign(open=w.close, high=w.close + 2, low=w.close - 2, volume=0, oi=0)
        elif sym == "INDIA VIX":
            out = w.assign(close=13.0, open=13.0, high=13.1, low=12.9, volume=0, oi=0)
        elif sym == "NIFTY-I":
            out = w.assign(close=w.close + 60, open=w.close, high=w.close, low=w.close, volume=10, oi=100)
        else:
            c = truedata.parse_symbol(sym)
            if c is None:
                return Resp(404, b"No Data Exists")
            w = w[w.timestamp.dt.date <= c.expiry]
            t = ((pd.Timestamp(c.expiry) + pd.Timedelta(hours=15, minutes=30) - w.timestamp)
                 .dt.total_seconds() / (365 * 86400)).clip(lower=1e-6)
            px = [max(bs_price(s, c.strike, tt, 0.13, c.right, 0.065, 0.012), 0.05) for s, tt in zip(w.close, t)]
            out = w.assign(close=px, open=px, high=px, low=px, volume=50, oi=1000)
        if out.empty:
            return Resp(404, b"No Data Exists")
        out = out[["timestamp", "open", "high", "low", "close", "volume", "oi"]]
        return Resp(200, _pack(out.to_csv(index=False, date_format="%Y-%m-%dT%H:%M:%S")))


@pytest.fixture
def dl(tmp_path, monkeypatch):
    monkeypatch.setattr(truedata, "SPOT_DIR", tmp_path / "spot")
    monkeypatch.setattr(truedata._time, "sleep", lambda s: None)
    cfg = load_config()
    cfg["market"]["holidays"] = [d.isoformat() for d in HOLIDAYS]
    cfg["truedata"] = {"min_gap_seconds": 0, "workers": 3, "band_pct": 3, "days_before_expiry": 14}
    fake = FakeTD(throttle_every=17)
    client = truedata.Client("u", "pw", 0, session=fake, log=lambda *a, **k: None)

    def make():
        return truedata.Downloader(cfg, START, END, client=client, log=lambda *a, **k: None,
                                   root=tmp_path / "td", option_root=tmp_path / "options")
    return make, fake, tmp_path


def test_symbols_round_trip():
    c = Contract("NIFTY", date(2026, 4, 21), 24050, "PE")
    assert truedata.option_symbol(c) == "NIFTY26042124050PE"
    assert truedata.parse_symbol("NIFTY26042124050PE") == c
    assert truedata.parse_symbol("NIFTY-I") is None
    assert truedata.parse_symbol("BANKNIFTY26042852000CE").underlying == "BANKNIFTY"


def test_bad_login_is_reported():
    with pytest.raises(truedata.TrueDataError, match="bad password"):
        truedata.Client("u", "nope", 0, session=FakeTD()).token()


def test_full_download_feeds_the_backtester(dl, monkeypatch):
    make, fake, tmp = dl
    d = make()
    path = d.spot()
    assert path.name == "TD_NIFTY_1minute.csv"
    df = pd.read_csv(path)
    assert len(df) == 375 * len(list(_days())) and df["vix"].eq(13.0).all()
    # the spot file loads as a normal Algo Lab data set
    monkeypatch.setattr("algolab.feeds.SPOT_DIR", tmp / "spot")
    assert load_dataset("TD_NIFTY_1minute").index[0] == pd.Timestamp("2026-04-01 09:16")

    n = d.contracts()
    assert n > 0
    # only expired contracts inside the period; holiday expiries are named by the real date
    exps = {r[0] for r in d.progress.db.execute("SELECT DISTINCT expiry FROM items WHERE kind='option'")}
    assert "2026-04-14" not in exps and "2026-04-13" in exps
    assert max(exps) <= END.isoformat()
    # nearest the money first
    pend = d.progress.pending("option")
    assert pend[0][0] != pend[-1][0]

    before = fake.calls
    d.options(limit=20)
    assert d.status()[0].startswith("NIFTY      done")
    # stop / restart: the second run only picks up what is left
    d2 = make()
    left = len(d2.progress.pending("option"))
    assert left == n - 20
    d2.options()
    assert not d2.progress.pending("option") and not d2.progress.pending("option", True)
    assert fake.calls > before

    # the backtester's option store now returns market prices
    row = d2.progress.db.execute("SELECT expiry,strike,right FROM items WHERE status='done' ORDER BY priority LIMIT 1").fetchone()
    c = Contract("NIFTY", date.fromisoformat(row[0]), int(row[1]), row[2])
    store = OptionStore("NIFTY", root=tmp / "options")
    when = datetime.combine(c.expiry, datetime.min.time()) + timedelta(hours=11)   # expiry day
    assert store.price(c, when) is not None
    raw = pd.read_csv(tmp / "td" / "options" / "NIFTY" / row[0] / f"{int(row[1])}{row[2]}.csv.gz")
    assert {"open", "high", "low", "close", "volume", "oi"} <= set(raw.columns)


def test_long_ranges_are_split(dl):
    make, fake, _ = dl
    fake.max_days = 4
    d = make()
    assert d.spot() is not None
    assert d.s["chunk_days"] <= 4


def test_bank_nifty_alongside_nifty(dl, monkeypatch):
    make, fake, tmp = dl
    d = make()
    d.spot()
    n_nifty = d.contracts()
    d.options(limit=5)
    cfg = d.cfg
    truedata.use_underlying(cfg, "BANKNIFTY")
    assert cfg["market"]["strike_step"] == 100 and cfg["truedata"]["spot_symbol"] == "NIFTY BANK"
    b = make()
    assert b.spot().name == "TD_BANKNIFTY_1minute.csv"
    calls = fake.calls
    n_bank = b.contracts()
    assert fake.calls == calls            # reuses the bhavcopies already saved
    exps = {r[0] for r in b.progress.db.execute("SELECT DISTINCT expiry FROM items WHERE key GLOB 'BANKNIFTY*'")}
    assert exps and all(date.fromisoformat(e) in _monthly() for e in exps)
    # each index only works through its own contracts
    assert len(b.progress.pending("option", underlying="BANKNIFTY")) == n_bank
    assert len(b.progress.pending("option", underlying="NIFTY")) == n_nifty - 5
    b.options()
    assert not b.progress.pending("option", underlying="BANKNIFTY")
    assert len(b.progress.pending("option", underlying="NIFTY")) == n_nifty - 5
    files = list((tmp / "options" / "BANKNIFTY").rglob("*.csv"))
    assert files and all(int(f.stem[:-2]) % 100 == 0 for f in files)
    status = "\n".join(b.status())
    assert "BANKNIFTY  done" in status and "NIFTY      pending" in status


def test_history_depth_report(dl, monkeypatch):
    make, fake, tmp = dl
    d = make()
    monkeypatch.setattr(truedata, "date", type("D", (date,), {"today": staticmethod(lambda: date(2026, 10, 14))}))
    path = truedata.history_depth(d.cfg, months=(6, 7), log=lambda *a, **k: None, root=tmp / "td", client=d.client)
    df = pd.read_csv(path)
    assert list(df["months_back"]) == [6, 7]
    assert df.loc[0, "option_1min_bars"] > 0 and df.loc[0, "option"].startswith("NIFTY26")
    assert df.loc[1, "spot_1min_bars"] == 0       # the stand-in server has nothing before April


def test_nearest_expiry_only(dl, capsys):
    make, fake, tmp = dl
    d = make()
    d.log = print
    d.nearest = True
    d.spot()
    n = d.contracts()
    rows = d.progress.db.execute("SELECT DISTINCT expiry,start,end FROM items WHERE key GLOB 'NIFTY[0-9]*'").fetchall()
    exps = sorted({r[0] for r in rows})
    for exp, a, b in rows:
        i = exps.index(exp)
        assert b == exp
        if i:   # starts the day after the previous expiry: only its own week
            assert a == (date.fromisoformat(exps[i - 1]) + timedelta(days=1)).isoformat()
        assert (date.fromisoformat(b) - date.fromisoformat(a)).days <= 8
    d.options(limit=30)
    out = capsys.readouterr().out
    assert "100.0%" in out and "contracts queued" in out and "nearest one" in out
    assert "Contract lists (F&O bhavcopy) NIFTY" in out


def test_full_after_nearest_fetches_only_missing_days(dl):
    make, fake, tmp = dl
    d = make()
    d.nearest = True
    d.spot()
    d.contracts()
    d.options()
    near = dict(d.progress.db.execute("SELECT key, rows FROM items WHERE status='done'").fetchall())
    calls = fake.calls
    full = make()            # same period, full windows (last 14 days before each expiry here)
    full.contracts()
    pend = full.progress.pending("option")
    assert pend                             # widened windows went back to pending
    full.options()
    assert not full.progress.pending("option")
    after = dict(full.progress.db.execute("SELECT key, rows FROM items WHERE status='done'").fetchall())
    grew = [k for k in near if after.get(k, 0) > near[k]]
    assert grew                             # earlier days were added
    assert all(after[k] >= near[k] for k in near)   # nothing lost
    # one request per widened contract at most (only the missing earlier days), not a full refetch
    assert fake.calls - calls <= len(pend) * 1.2 + 60


def test_damaged_files_and_failures_do_not_stop_the_download(dl, monkeypatch):
    make, fake, tmp = dl
    d = make()
    d.spot()
    d.contracts()
    rows = d.progress.pending("option")
    # a file left empty by an interrupted run (what stopped the 15 Oct download)
    key, exp, strike, right, a, b = rows[0]
    raw = tmp / "td" / "options" / "NIFTY" / exp / f"{strike:g}{right}.csv.gz"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"")
    # and one contract whose download fails with an unexpected error
    bad_key = rows[1][0]
    real_fetch = d.fetch
    monkeypatch.setattr(d, "fetch", lambda sym, *x, **k: (_ for _ in ()).throw(RuntimeError("boom"))
                        if sym == bad_key else real_fetch(sym, *x, **k))
    d.options(limit=10)
    status = dict(d.progress.db.execute("SELECT key, status FROM items").fetchall())
    assert status[key] == "done"                       # the empty file was replaced with good data
    assert status[bad_key] == "error"                  # recorded, not fatal
    assert sum(v == "done" for v in status.values()) == 9
    assert not list(raw.parent.glob("*.tmp"))          # no half-written files left behind


def test_sensex_uses_the_bse_contract_list(dl):
    make, fake, tmp = dl
    cfg = make().cfg
    truedata.use_underlying(cfg, "SENSEX")
    assert cfg["market"]["strike_step"] == 100 and cfg["market"]["expiry_rules"][-1]["weekday"] == 3
    d = make()
    assert d.spot().name == "TD_SENSEX_1minute.csv"
    n = d.contracts()
    assert n > 0
    exps = {date.fromisoformat(r[0]) for r in d.progress.db.execute("SELECT DISTINCT expiry FROM items WHERE key GLOB 'SENSEX*'")}
    assert exps and exps <= set(_sensex_expiries())
    days = {r[0] for r in d.progress.db.execute("SELECT day FROM days")}
    assert any(k.startswith("BSEFO:") for k in days)          # BSE days kept apart from NSE days
    d.options(limit=10)
    assert "SENSEX     done" in "\n".join(d.status())
    assert list((tmp / "options" / "SENSEX").rglob("*.csv"))


def test_nifty50_stocks_and_futures(dl, monkeypatch):
    make, fake, tmp = dl
    d = make()
    got = truedata.download_stocks(d, ["RELIANCE", "M&M", "BAJAJ-AUTO", "NOSUCH"], log=lambda *a, **k: None)
    assert got == 6                                              # 3 stocks x (equity + future); NOSUCH empty
    eq = pd.read_csv(tmp / "td" / "equity" / "MandM_1min.csv.gz")
    fut = pd.read_csv(tmp / "td" / "futures" / "BAJAJ-AUTO-I_1min.csv.gz")
    assert len(eq) > 1000 and len(fut) == len(eq)
    status = "\n".join(d.status())
    assert "STOCKS     done" in status and "STK FUTURE done" in status and "STOCKS     empty" in status
    calls = fake.calls
    assert truedata.download_stocks(d, ["RELIANCE"], log=lambda *a, **k: None) == 0   # already done: no requests
    assert fake.calls == calls


def test_nifty50_list_falls_back(monkeypatch):
    class Boom:
        def get(self, *a, **k):
            raise OSError("offline")
    syms, source = truedata.nifty50_symbols(log=lambda *a: None, session=Boom())
    assert len(syms) == 50 and "built-in" in source and "BSE" in syms and "WIPRO" not in syms

    class Ok:
        def get(self, *a, **k):
            class R:
                text = "Company Name,Industry,Symbol,Series,ISIN Code\n" + "\n".join(f"C{i},X,S{i},EQ,I{i}" for i in range(50))
                def raise_for_status(self):
                    pass
            return R()
    syms, source = truedata.nifty50_symbols(session=Ok())
    assert syms[0] == "S0" and "NSE" in source


def test_sensex_without_a_bse_list_uses_the_calendar(dl, monkeypatch):
    make, fake, tmp = dl
    monkeypatch.setitem(truedata.BHAV_SEGMENTS, "SENSEX", ["NOPE"])      # TrueData offers no BSE list
    cfg = make().cfg
    truedata.use_underlying(cfg, "SENSEX")
    d = make()
    d.spot()
    assert d.contracts() > 0
    exps = {date.fromisoformat(r[0]) for r in d.progress.db.execute("SELECT DISTINCT expiry FROM items WHERE key GLOB 'SENSEX*'")}
    assert exps and all(e.weekday() == 3 or e + timedelta(days=1) in T_HOLIDAYS_PLUS_THURSDAYS() for e in exps)


def T_HOLIDAYS_PLUS_THURSDAYS():
    return {e for e in HOLIDAYS if e.weekday() == 3}


def test_open_contracts_are_queued_up_to_the_end_date(dl):
    make, fake, tmp = dl
    d = make()
    d.end = date(2026, 5, 6)      # a Wednesday: the 12 May expiry is still open
    d.spot()
    d.contracts()
    exps = {r[0] for r in d.progress.db.execute("SELECT DISTINCT expiry FROM items WHERE key GLOB 'NIFTY[0-9]*'")}
    assert max(exps) <= "2026-05-06"                     # without --open: expired contracts only
    o = make()
    o.end, o.open = date(2026, 5, 6), True
    o.contracts()
    rows = o.progress.db.execute("SELECT expiry, end FROM items WHERE key GLOB 'NIFTY[0-9]*' "
                                 "AND expiry > '2026-05-06'").fetchall()
    assert rows and all(e == "2026-05-06" for _x, e in rows)   # open ones: their days so far
    o.options()
    assert not o.progress.pending("option", underlying="NIFTY")


def test_ticks_with_bid_ask_for_recent_days(dl):
    make, fake, tmp = dl
    d = make()
    d.spot()
    d.contracts()
    logs = []
    got = truedata.download_ticks(d, strikes=2, days=5, log=lambda *a, **k: logs.append(str(a[0])))
    assert got > 20
    days = sorted(p.name for p in (tmp / "td" / "ticks" / "NIFTY").iterdir())
    assert len(days) == 5 and days[-1] == "2026-05-15"
    f = next((tmp / "td" / "ticks" / "NIFTY" / days[-1]).glob("NIFTY2605*CE.csv.gz"))
    t = pd.read_csv(f)
    assert {"bid", "ask", "ltp", "oi"} <= set(t.columns) and len(t) == 60   # same-second ticks kept
    assert (tmp / "td" / "ticks" / "NIFTY" / days[-1] / "NIFTY-I.csv.gz").exists()
    assert any(st.startswith("TICKS NIFTY") for st in d.status())
    calls = fake.tick_calls
    assert truedata.download_ticks(d, strikes=2, days=5, log=lambda *a, **k: logs.append(str(a[0]))) == got   # all done: no requests
    assert fake.tick_calls == calls


def test_ticks_stop_early_when_refused(dl):
    make, fake, tmp = dl
    d = make()
    d.spot()
    d.contracts()
    fake.refuse_ticks = True
    logs = []
    assert truedata.download_ticks(d, strikes=2, days=5, log=lambda *a, **k: logs.append(str(a[0]))) == 0
    assert fake.tick_calls == 1 and any("refused" in m for m in logs)
