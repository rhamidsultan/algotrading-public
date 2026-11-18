"""The Algo Lab Data tab's reading of stored data (algolab/datainfo.py)."""
import sqlite3

from algolab import datainfo


def _data(tmp_path):
    d = tmp_path / "data"
    (d / "spot").mkdir(parents=True)
    (d / "spot" / "TD_NIFTY_1minute.csv").write_text(
        "datetime,open,high,low,close,vix\n" + "".join(f"2026-10-12 09:{15 + i}:00,1,1,1,{25000 + i},12\n" for i in range(3))
        + "2026-10-13 09:15:00,1,1,1,25100,12.5\n")
    (d / "spot" / "TD_BANKNIFTY_1minute.csv").write_text("datetime,open,high,low,close\n2026-10-13 09:15:00,1,1,1,56000\n")
    e = d / "options" / "NIFTY" / "2026-10-20"
    e.mkdir(parents=True)
    (e / "25000CE.csv").write_text("time,last,bid,ask\n2026-10-12 09:20:00,120,,\n2026-10-14 15:29:00,140.5,,\n")
    (e / "25000PE.csv").write_text("time,last,bid,ask\n2026-10-13 09:20:00,90,,\n")
    (e / "26000CE.csv").write_text("time,last,bid,ask\n")                     # header only: ignored
    (d / "truedata").mkdir()
    c = sqlite3.connect(d / "truedata" / "progress.db")
    c.execute("create table items (key text, status text, rows int)")
    c.executemany("insert into items values (?,?,?)", [("NIFTY26102025000CE", "done", 375), ("NIFTYX", "empty", 0),
                                                        ("EQ:TCS:a:b", "done", 1000), ("SENSEX1", "pending", None)])
    c.commit()
    c.close()
    r = d / "review" / "20261015-1350"
    r.mkdir(parents=True)
    (r / "issues.csv").write_text("severity,check,dataset,key,detail,contract\nWARN,missing minutes,X,1,d,\nINFO,a,X,1,d,\n")
    (r / "redownload.txt").write_text("A\nB\n")
    return d


def test_contracts_coverage_and_index(tmp_path):
    d = _data(tmp_path)
    assert datainfo.underlyings(d) == ["NIFTY", "BANKNIFTY"]
    assert [f.stem for f in datainfo.index_files("NIFTY", d)] == ["TD_NIFTY_1minute"]      # not the Bank Nifty file
    cons = datainfo.contracts("NIFTY", d)
    assert len(cons) == 2 and set(cons.right) == {"CE", "PE"}
    ce = cons[cons.right == "CE"].iloc[0]
    assert (ce["from"], ce["to"], ce["last"], ce["rows"]) == ("2026-10-12 09:20", "2026-10-14 15:29", 140.5, 2)
    ex = datainfo.expiries(cons)
    assert ex.iloc[0].to_dict()["contracts"] == 2 and ex.iloc[0]["low"] == 25000
    days = datainfo.index_days(datainfo.index_files("NIFTY", d)[0])
    assert list(days.bars) == [3, 1] and days.close.iloc[0] == 25002 and days.vix.iloc[-1] == 12.5
    cov = datainfo.coverage(cons, days).set_index("day")
    assert cov.loc["2026-10-12"].tolist() == [3, 1] and cov.loc["2026-10-13"].tolist() == [1, 2]
    assert cov.loc["2026-10-14"].tolist() == [0, 1]
    weeks, z, text = datainfo.calendar_grid(cov.reset_index())
    assert weeks == ["2026-10-12"] and z[0] == [1.0] and z[3] == [None] and "nothing stored" in text[3][0]


def test_sizes_progress_and_review(tmp_path):
    d = _data(tmp_path)
    parts = dict(zip(*datainfo.folder_sizes(d).T.values))
    assert {"Options NIFTY", "Index bars", "TrueData raw: progress.db", "Data reviews"} <= set(parts)
    tp = datainfo.truedata_progress(d).set_index(["what", "status"])
    assert tp.loc[("NIFTY", "done"), "candles"] == 375 and tp.loc[("Stocks", "done"), "items"] == 1
    rv = datainfo.latest_review(d)
    assert (rv["verdict"], rv["warn"], rv["info"], rv["redownload"]) == ("WARN", 1, 1, 2)


def test_big_file_rows_are_estimated(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("time,last,bid,ask\n" + "2026-10-13 09:20:00,101.50,,\n" * 50000)
    info = datainfo.edges(f)
    assert abs(info["rows"] - 50000) < 50 and info["last"] == 101.5
