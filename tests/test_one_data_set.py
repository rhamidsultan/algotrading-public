"""One 1-minute price set per index (algolab/datasets.py), old copies moved to data/archive, screens show only those."""
import pandas as pd

from algolab import datasets


def _bars(path, start, end, close=100.0):
    idx = pd.date_range(start, end, freq="1min")
    pd.DataFrame({"datetime": idx, "open": close, "high": close, "low": close, "close": close, "vix": 12.0}).to_csv(path, index=False)


def test_truedata_history_goes_in_front_and_old_copies_move_to_archive(tmp_path):
    spot = tmp_path / "data" / "spot"
    spot.mkdir(parents=True)
    _bars(spot / "TD_NIFTY_1minute.csv", "2024-10-15 09:15", "2024-10-15 09:20", 1.0)
    _bars(spot / "NIFTY_minute.csv", "2024-10-15 09:18", "2026-10-26 09:20", 2.0)     # own bars win on overlap
    _bars(spot / "TD_BANKNIFTY_1minute.csv", "2024-10-15 09:15", "2024-10-15 09:17")  # no set yet: seeded whole
    _bars(spot / "TD_SENSEX_1minute.csv", "2024-10-15 09:15", "2026-10-21 09:16")
    _bars(spot / "SENSEX_minute.csv", "2026-10-01 09:15", "2026-10-01 09:16")         # stops before TrueData ends
    (spot / "TD_NIFTY_1minute.before-fill-20261015-1306.csv").write_text("datetime,close\n")
    (spot / "SAMPLE_NIFTY_5minute.csv").write_text("datetime,close\n")
    (spot / "NIFTY_5minute.csv").write_text("datetime,close\n")

    plan = datasets.organize(spot, dry_run=True)
    assert (spot / "TD_NIFTY_1minute.csv").exists() and not plan["seeded"]          # dry run touches nothing

    out = datasets.organize(spot)
    nifty = pd.read_csv(spot / "NIFTY_minute.csv", parse_dates=["datetime"])
    assert str(nifty.datetime.min()) == "2024-10-15 09:15:00" and len(nifty[nifty.close == 1.0]) == 3
    assert out["seeded"]["NIFTY_minute"] == 3 and out["seeded"]["BANKNIFTY_minute"] == 3
    arch = tmp_path / "data" / "archive" / "spot"
    assert (arch / "TD_NIFTY_1minute.csv").exists() and (arch / "TD_BANKNIFTY_1minute.csv").exists()
    assert (arch / "TD_NIFTY_1minute.before-fill-20261015-1306.csv").exists()
    assert (spot / "TD_SENSEX_1minute.csv").exists()                                 # its last days aren't in SENSEX_minute
    assert any("TD_SENSEX_1minute.csv" in k for k in out["kept"])
    assert (spot / "SAMPLE_NIFTY_5minute.csv").exists() and (spot / "NIFTY_5minute.csv").exists()
    assert out["sets"] == ["NIFTY_minute", "BANKNIFTY_minute", "SENSEX_minute"]
    assert datasets.seed_from_truedata("NIFTY", spot) == 0                           # archived copy already in
    datasets.organize(spot)                                                           # a second run is harmless
    assert not (arch / "TD_NIFTY_1minute.1.csv").exists()


def test_screens_offer_only_the_index_sets(tmp_path, monkeypatch):
    from test_defined_risk import _panel
    spot = tmp_path / "data" / "spot"
    spot.mkdir(parents=True)
    for n in ("NIFTY_minute", "SENSEX_minute", "NIFTY_5minute", "TD_NIFTY_1minute", "SAMPLE_NIFTY_5minute"):
        _bars(spot / f"{n}.csv", "2026-10-26 09:15", "2026-10-26 09:16")
    day = tmp_path / "data" / "sec1" / "2026-10-27"
    day.mkdir(parents=True)
    (day / "NIFTY.csv").write_text("time\n")
    mod = _panel(tmp_path, monkeypatch)
    assert [f.stem for f in mod._spot_files()] == ["NIFTY_minute", "SENSEX_minute"]
    assert [d["name"] for d in mod._bt_datasets()] == ["NIFTY_1second", "NIFTY_minute", "SENSEX_minute"]   # 1-second first
    assert mod._sec1_days() == [{"day": "2026-10-27", "instruments": 1, "mb": 0.0,
                                 "coverage": 0.0, "gaps": [["09:15", "15:30"]]}]     # an empty file: no ticks all day
    assert datasets.screen_datasets(spot) == ["NIFTY_minute", "SENSEX_minute"]


def test_collect_tops_up_every_index(tmp_path, monkeypatch):
    from datetime import date
    from algolab import kite
    spot = tmp_path / "spot"
    spot.mkdir()
    monkeypatch.setattr(kite, "SPOT_DIR", spot)
    asked = []

    def fake_candles(k, token, start, end, interval):
        asked.append(token)
        return pd.DataFrame({"datetime": pd.date_range("2026-10-26 09:15", periods=2, freq="1min"),
                             "open": 1.0, "high": 1.0, "low": 1.0, "close": float(token)})
    monkeypatch.setattr(kite, "fetch_candles", fake_candles)
    monkeypatch.setattr(kite, "index_tokens", lambda k: {"INDIA VIX": 264969})
    msg = kite.collect_minute(date(2026, 10, 26), kite=None)
    assert "NIFTY 2" in msg and "BANKNIFTY 2" in msg and "SENSEX 2" in msg
    assert {256265, 260105, 265} <= set(asked)
    assert pd.read_csv(spot / "SENSEX_minute.csv").close.iloc[0] == 265.0
