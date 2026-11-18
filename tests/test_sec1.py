"""1-second bars from Zerodha's stream (algolab/sec1.py): folding ticks, writing, keeping N trading days."""
from datetime import date, datetime

from algolab import sec1
from algolab.config import IST, load_config


def _epoch(y, mo, d, h, mi, s):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST).timestamp()


def _rec(tmp_path, monkeypatch, now):
    monkeypatch.setattr(sec1, "RUNTIME", tmp_path / "runtime")
    monkeypatch.setattr(sec1, "STATUS", tmp_path / "runtime" / "sec1_status.json")
    clock = [now]
    r = sec1.SecondRecorder(root=tmp_path / "sec1", clock=lambda: clock[0])
    r.set_names({256265: "NIFTY", 111: "2026-10-27_25000CE"})
    return r, clock


def test_ticks_fold_into_one_bar_per_second_in_ist(tmp_path, monkeypatch):
    t0 = _epoch(2026, 10, 27, 10, 0, 5)
    r, clock = _rec(tmp_path, monkeypatch, t0 + 0.9)
    r.on_ticks([{"token": 111, "last": 100.0, "bid": 99.5, "ask": 100.5, "volume": 10, "rx": t0 + 0.1},
                {"token": 111, "last": 102.0, "rx": t0 + 0.4},
                {"token": 111, "last": 98.5, "bid": 98.0, "ask": 99.0, "volume": 25, "rx": t0 + 0.8},
                {"token": 999, "last": 5.0, "rx": t0 + 0.8},                 # not a recorded instrument
                {"token": 256265, "last": 25010.5, "ts": datetime.fromtimestamp(t0 + 1), "rx": t0 + 1.2}])
    assert r.flush() == 0                                   # too soon: the second may still get ticks
    clock[0] = t0 + 20
    assert r.flush() == 2
    rows = (tmp_path / "sec1" / "2026-10-27" / "2026-10-27_25000CE.csv").read_text().splitlines()
    assert rows[0] == ",".join(sec1.COLUMNS)
    assert rows[1] == "2026-10-27 10:00:05,100.0,102.0,98.5,98.5,98.0,99.0,25,,3,,"
    nifty = (tmp_path / "sec1" / "2026-10-27" / "NIFTY.csv").read_text().splitlines()
    assert nifty[1].startswith("2026-10-27 10:00:06,25010.5")    # exchange timestamp wins over receive time
    assert not (tmp_path / "sec1" / "2026-10-27" / "999.csv").exists()


def test_a_fault_switches_recording_off_without_raising(tmp_path, monkeypatch):
    r, _ = _rec(tmp_path, monkeypatch, 0)
    r.on_ticks([{"token": 111, "last": "bad", "rx": 1}, {"token": 111, "last": 5, "rx": 1}])
    assert not r.enabled and r.error
    r.on_ticks([{"token": 111, "last": 5.0, "rx": 1}])      # ignored from now on, still no exception


def test_stream_drops_a_listener_that_raises():
    from algolab.stream import KiteStream
    s = KiteStream("k", "t", log=lambda m: None)

    def boom(batch):
        raise RuntimeError("x")
    s.on_ticks = boom

    class WS:
        n = 0

        def recv_data(self):
            WS.n += 1
            if WS.n > 1:
                s._stop.set()
            return 2, bytes.fromhex("0001000800000101000003e8")   # one LTP packet: token 257, 10.00
    ws = WS()
    s._ws = ws
    s._sync_subscriptions = lambda w: None
    s._loop(ws)
    assert s.on_ticks is None and s.ticks[257]["last"] == 10.0


def test_prune_keeps_the_last_n_trading_days(tmp_path):
    cfg = load_config()
    cfg["sec1"] = {"keep_trading_days": 3}
    root = tmp_path / "sec1"
    for d in ("2026-10-16", "2026-10-19", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-26", "notes"):
        (root / d).mkdir(parents=True)
        (root / d / "NIFTY.csv").write_text("time\n")
    out = sec1.prune(cfg, today=date(2026, 10, 26), root=root, dry_run=True)
    assert out["keep_from"] == "2026-10-22" and out["removed"] == ["2026-10-16", "2026-10-19", "2026-10-21"]
    assert (root / "2026-10-16").exists()                    # dry run
    sec1.prune(cfg, today=date(2026, 10, 26), root=root)
    assert [str(d) for d in sec1.stored_days(root)] == ["2026-10-22", "2026-10-23", "2026-10-26"]
    assert (root / "notes").exists()                          # only dated folders are touched


def test_holiday_does_not_count_as_a_trading_day(tmp_path):
    cfg = load_config()                                       # 20 Oct 2026 is an NSE holiday
    cfg["sec1"] = {"keep_trading_days": 2}
    root = tmp_path / "sec1"
    for d in ("2026-10-16", "2026-10-19", "2026-10-21"):
        (root / d).mkdir(parents=True)
    out = sec1.prune(cfg, today=date(2026, 10, 21), root=root)
    assert out["removed"] == ["2026-10-16"] and out["keep_from"] == "2026-10-19"


def test_settings_defaults_and_floor():
    s = sec1.settings({"sec1": {"keep_trading_days": 0}})
    assert s["keep_trading_days"] == 1 and s["strikes_each_side"] == 30 and s["record"]
    assert sec1.settings(load_config())["keep_trading_days"] == 5


def test_live_feed_records_one_second_bars_from_the_stream(tmp_path, monkeypatch):
    from test_stream import _holiday_feed
    monkeypatch.setattr(sec1, "ROOT", tmp_path / "sec1")
    monkeypatch.setattr(sec1, "RUNTIME", tmp_path / "rt")
    monkeypatch.setattr(sec1, "STATUS", tmp_path / "rt" / "sec1_status.json")
    feed, st, clock = _holiday_feed(tmp_path, monkeypatch, datetime(2026, 10, 21, 10, 0))
    assert feed.sec1 is not None and st.on_ticks == feed.sec1.on_ticks
    feed._load_chain()
    st.ticks[256265] = {"last": 25100.0, "ts": datetime(2026, 10, 21, 9, 59, 59)}
    assert feed._stream_step(datetime(2026, 10, 21, 10, 0))
    names = feed.sec1.names
    assert names.get(256265) == "NIFTY"
    strikes = {int(n.split("_")[1][:-2]) for n in names.values() if "_" in n}
    assert len(strikes) > 21 or len(feed._band) >= len(strikes)    # band widened beyond the collect: ±10
    tok = next(t for t, n in names.items() if n.endswith("CE"))
    t0 = datetime(2026, 10, 21, 10, 0, 1, tzinfo=sec1.IST).timestamp()
    st.on_ticks([{"token": tok, "last": 50.0, "rx": t0}, {"token": tok, "last": 51.0, "rx": t0 + 0.5}])
    assert feed.sec1.flush(force=True) == 1
    day = tmp_path / "sec1" / "2026-10-21"
    (f,) = list(day.glob("*CE.csv"))
    assert f.read_text().splitlines()[1].startswith("2026-10-21 10:00:01,50.0,51.0,50.0,51.0")


def test_recording_can_be_switched_off(tmp_path, monkeypatch):
    from test_stream import _holiday_feed
    import algolab.sec1 as s1
    real = s1.settings
    monkeypatch.setattr(s1, "settings", lambda cfg: {**real(cfg), "record": False})
    feed, st, _ = _holiday_feed(tmp_path, monkeypatch, datetime(2026, 10, 21, 10, 0))
    assert feed.sec1 is None and getattr(st, "on_ticks", None) is None


def test_recorder_keeps_the_exchanges_day_open_and_high(tmp_path, monkeypatch):
    t0 = _epoch(2026, 10, 27, 12, 0, 1)
    r, clock = _rec(tmp_path, monkeypatch, t0 + 30)
    r.on_ticks([{"token": 111, "last": 50.0, "open": 95.0, "high": 120.0, "rx": t0}])
    r.flush(force=True)
    row = (tmp_path / "sec1" / "2026-10-27" / "2026-10-27_25000CE.csv").read_text().splitlines()[1]
    assert row.endswith(",95.0,120.0")
