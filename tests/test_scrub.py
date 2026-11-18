"""Undoing paper trading on an NSE holiday (20 Oct 2026, when the feed still ran on stale prices)."""
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algolab import paper, scrub  # noqa: E402

DAY = date(2026, 10, 20)


def _trade(entry, exit_, gross, costs):
    return {"contract": "NIFTY 27 Oct 26 25000 CE", "side": "SHORT", "qty": 65, "entry_time": entry,
            "exit_time": exit_, "entry_price": 100.0, "exit_price": 100.0, "gross_pnl": gross, "costs": costs,
            "net_pnl": gross - costs, "reason": "Square-off", "group": "g"}


def _fill(t, costs):
    return {"time": t, "contract": "x", "side": "SELL", "qty": 65, "price": 100.0, "costs": costs,
            "reason": "", "group": "g", "source": "market", "note": ""}


def _session(tmp, sid, trades, fills, positions=()):
    slot = paper.Slot(sid).mk()
    b = {"capital": 300000, "realized_gross": sum(t["gross_pnl"] for t in trades),
         "total_costs": sum(f["costs"] for f in fills), "positions": list(positions), "fills": fills, "trades": trades}
    slot.state.write_text(json.dumps({"broker": b, "equity": 300000 + b["realized_gross"] - b["total_costs"],
                                      "logs": ["2026-10-20 09:20 Entered", "2026-10-21 09:20 Entered"]}))
    slot.equity.write_text("time,equity,spot\n2026-10-19 15:29:00,301000,25000\n2026-10-20 15:15:00,300632,25000\n"
                           "2026-10-21 10:00:00,300832,25100\n")
    return slot


def test_same_day_trades_are_removed_and_later_equity_lifted(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(scrub, "RUNTIME", tmp_path)
    keep = _trade("2026-10-19T09:20:00", "2026-10-19T15:15:00", 1200.0, 0)
    bad = _trade("2026-10-20T09:20:00", "2026-10-20T15:15:00", -221.0, 0)
    slot = _session(tmp_path, "intraday_straddle", [keep, bad],
                    [_fill("2026-10-19T09:20:00", 100.0), _fill("2026-10-20T09:20:00", 73.4), _fill("2026-10-20T15:15:00", 73.41)])
    scrub.scrub_day(DAY, log=lambda m: None)
    st = json.loads(slot.state.read_text())
    assert st["broker"]["trades"] == [keep] and len(st["broker"]["fills"]) == 1
    assert round(st["broker"]["realized_gross"], 2) == 1200.0 and round(st["broker"]["total_costs"], 2) == 100.0
    assert st["logs"] == ["2026-10-21 09:20 Entered"]
    rows = slot.equity.read_text().splitlines()
    assert [r[:10] for r in rows[1:]] == ["2026-10-19", "2026-10-21"]
    assert rows[2].split(",")[1] == f"{300832 + 367.81:.2f}"
    assert (tmp_path / "backups" / "sessions-before-scrub-2026-10-20.tgz").exists()


def test_a_position_carried_on_from_the_holiday_resets_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(scrub, "RUNTIME", tmp_path)
    slot = _session(tmp_path, "premium_strangle", [_trade("2026-10-20T10:15:00", "2026-10-21T12:23:00", 4000.0, 0)],
                    [_fill("2026-10-20T10:15:00", 50.0), _fill("2026-10-21T12:23:00", 50.0)])
    assert scrub.scrub_session(slot, DAY).startswith("reset")
    assert not slot.state.exists() and not slot.equity.exists()


def test_recorded_holiday_prices_are_deleted(tmp_path):
    f = tmp_path / "options" / "NIFTY" / "2026-10-27" / "25000CE.csv"
    f.parent.mkdir(parents=True)
    f.write_text("time,last,bid,ask\n2026-10-19 15:29:00,100,99,101\n2026-10-20 09:16:00,100,99,101\n"
                 "2026-10-20 09:17:00,100,99,101\n2026-10-21 09:16:00,90,89,91\n")
    assert scrub.scrub_prices(DAY, data=tmp_path) == 2
    assert "2026-10-20" not in f.read_text() and f.read_text().count("\n") == 3


def test_only_listed_holidays_are_cleaned(tmp_path, monkeypatch):
    monkeypatch.setattr(scrub, "RUNTIME", tmp_path)
    cfg = {"market": {"holidays": ["2026-10-02", "2026-10-20", "2026-11-10"]}}
    assert scrub.holidays_to_scrub(cfg, today=date(2026, 10, 21)) == [date(2026, 10, 2), DAY]
    (tmp_path / "scrubbed-2026-10-20").write_text("x")
    assert scrub.holidays_to_scrub(cfg, today=date(2026, 10, 21)) == [date(2026, 10, 2)]


def test_shipped_holidays_apply_even_when_the_servers_config_lacks_them(tmp_path):
    from algolab.config import load_config
    old = tmp_path / "config.yaml"
    old.write_text("market:\n  underlying: NIFTY\n")          # a server config.yaml from before the holiday list
    cfg = load_config(old)
    assert "2026-10-20" in cfg["market"]["holidays"]
    assert scrub.holidays_to_scrub(cfg, today=date(2026, 10, 21))[-1] == DAY


def test_restore_puts_back_the_copy_saved_before_the_clean_up(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(scrub, "RUNTIME", tmp_path)
    slot = _session(tmp_path, "iron_fly", [_trade("2026-10-21T09:30:00", "2026-10-21T15:00:00", 900.0, 0)], [])
    before = slot.state.read_text()
    scrub.scrub_day(DAY, log=lambda m: None)          # writes the backup
    slot.state.write_text("{}")                        # the clean-up went wrong
    out = []
    scrub.restore(dry_run=True, log=out.append)
    assert out[0].startswith("Will restore") and slot.state.read_text() == "{}"
    scrub.restore(log=out.append)
    assert paper.Slot("iron_fly").state.read_text() == before
    assert list((tmp_path / "backups").glob("sessions-after-scrub-*.tgz"))
    out.clear()
    scrub.restore(log=out.append)                      # only once
    assert out == ["Nothing to restore."]


def test_only_the_named_holiday_cycle_is_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(paper, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(scrub, "RUNTIME", tmp_path)
    keep = {**_trade("2026-10-21T09:20:00", "2026-10-21T15:15:00", 900.0, 0), "group": "006 | 21 Oct 26"}
    bad = {**_trade("2026-10-20T09:20:00", "2026-10-20T15:15:00", -221.0, 0), "group": "005 | 20 Oct 26"}
    f20 = [{**_fill("2026-10-20T09:20:00", 73.4), "group": "005 | 20 Oct 26"},
           {**_fill("2026-10-20T15:15:00", 73.41), "group": "005 | 20 Oct 26"}]
    f21 = [{**_fill("2026-10-21T09:20:00", 50.0), "group": "006 | 21 Oct 26"}]
    slot = _session(tmp_path, "intraday_strangle", [keep, bad], f20 + f21)
    carried = {**_trade("2026-10-20T10:15:00", "2026-10-21T12:00:00", 4000.0, 0), "group": "003 | exp 27 Oct 26"}
    other = _session(tmp_path, "premium_strangle", [carried],
                     [{**_fill("2026-10-20T10:15:00", 50.0), "group": "003 | exp 27 Oct 26"}])
    before_other = other.state.read_text()
    cycles = {"intraday_strangle": "005 | 20 Oct 26", "premium_strangle": "003 | exp 27 Oct 26"}
    out = []
    scrub.remove_holiday_cycles(log=out.append, cycles=cycles)
    st = json.loads(slot.state.read_text())
    assert st["broker"]["trades"] == [keep] and st["broker"]["fills"] == f21
    assert round(st["broker"]["total_costs"], 2) == 50.0
    assert other.state.read_text() == before_other and any("not entirely" in o for o in out)
    rows = slot.equity.read_text().splitlines()
    assert [r[:10] for r in rows[1:]] == ["2026-10-19", "2026-10-21"]
    assert list((tmp_path / "backups").glob("sessions-before-cycle-removal-*.tgz"))
    out.clear()
    scrub.remove_holiday_cycles(log=out.append, cycles=cycles)
    assert out == ["Nothing to remove."]
