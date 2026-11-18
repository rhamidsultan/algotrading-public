"""Command line for the platform.  Examples:

  python run.py sample-data
  python run.py backtest --strategy short_straddle --set stop_loss_pct=30 --from 2026-01-01
  python run.py backtest --strategy blaze_butterfly --capital 90000 --note "first real-data run"
  python run.py runs                        (saved backtests, newest first)
  python run.py publish                     (send new runs to the server dashboard's Backtests page)
  python run.py sweep --strategy short_straddle --grid stop_loss_pct=20,30,40 --grid target_pct=40:80:20 --split 2026-01-01
  python run.py paper --feed sim            (simulated market, no broker needed)
  python run.py login                       (Zerodha, once each morning)
  python run.py download                    (NIFTY + India VIX from 2023, through the server dashboard's login)
  python run.py download-options            (real candles for currently listed options)
  python run.py import-nse                  (NSE closing prices of every NIFTY option since 2023 -> skew correction)
  python run.py paper --feed kite           (live Zerodha quotes, simulated orders)
  python run.py truedata test               (TrueData trial: check the login and data format)
  python run.py truedata all                (TrueData: NIFTY, VIX and expired NIFTY options, last 6 months)
  python run.py truedata all --underlying BANKNIFTY
  python run.py truedata depth              (TrueData: how many months back expired options go)
  python run.py truedata ticks              (TrueData: last 5 days of ticks with bid/ask, near the money)
  python run.py truedata all --open         (also contracts not expired yet, up to today)
  python run.py dashboard
"""
from __future__ import annotations

import argparse
import json
import os
import time
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

from algolab.config import ROOT, load_config
from algolab.strategy import load_strategies


def _params(args) -> dict:
    params = json.loads(args.params) if args.params else {}
    for item in args.set or []:
        k, v = item.split("=", 1)
        params[k] = v
    return params


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sample-data")
    sub.add_parser("strategies")
    sub.add_parser("login")
    sub.add_parser("dashboard")
    sub.add_parser("paper-service", help="server mode: keep the dashboard-selected session running")
    sub.add_parser("catalog", help="write the strategy list used by the portfolio dashboard page")
    rc = sub.add_parser("paper-remove-holiday-cycles", help="server: remove the four 20 Oct holiday cycles (once)")
    rc.add_argument("--dry-run", action="store_true")
    rs = sub.add_parser("paper-restore", help="server: put paper sessions back to the copy saved before a clean-up")
    rs.add_argument("--dry-run", action="store_true")
    sc = sub.add_parser("paper-scrub", help="server: undo paper trading on a past NSE holiday (engine stopped)")
    sc.add_argument("--day", help="one holiday, e.g. 2026-10-20 (default: every recent one not cleaned yet)")
    sc.add_argument("--dry-run", action="store_true", help="only report what would change")
    bq = sub.add_parser("backtest-queue", help="run the backtests asked for on the dashboard (server)")
    bq.add_argument("--stopped", action="store_true",
                    help="only mark jobs the runner left 'running' as stopped (run by systemd after each run)")
    sub.add_parser("tick-test-queue", help="server: count Zerodha ticks for the dashboard's TrueData vs Zerodha test")
    pr = sub.add_parser("paper-report", help="server: backtest each paper session's week and compare (weekly timer)")
    pr.add_argument("--days", type=int, default=7)
    la = sub.add_parser("lab-agent", help="Mac helper: run the backtests the dashboard sends to the Mac")
    la.add_argument("--once", action="store_true")
    co = sub.add_parser("collect", help="server: store today's NIFTY and option prices (run after the close)")
    co.add_argument("--plan", action="store_true", help="only show which option contracts would be stored")
    cp = sub.add_parser("compare", help="paper / shadow / live vs 1-minute and 1-second backtests, day by day")
    cp.add_argument("--from", dest="start", required=True)
    cp.add_argument("--to", dest="end", required=True)
    cp.add_argument("--bars", default="1m,1s", help="1m, 1s or 1m,1s")
    cp.add_argument("--sessions", default="", help="session ids, comma separated (default: all that traded)")
    dz = sub.add_parser("data-organize", help="one 1-minute set per index (data/spot/<INDEX>_minute.csv); old copies to data/archive")
    dz.add_argument("--dry-run", action="store_true", help="only say what would be seeded and moved")
    s1 = sub.add_parser("sec1", help="server: the 1-second bars recorded from Zerodha's stream (status / prune)")
    s1.add_argument("action", choices=["status", "prune"], nargs="?", default="status")
    s1.add_argument("--dry-run", action="store_true", help="prune: only say which days would be deleted")
    sy = sub.add_parser("sync-data", help="copy the prices collected on your server to this computer")
    sy.add_argument("--host", help="e.g. ubuntu@YOUR_SERVER_IP (default: config.yaml server.host)")
    sy.add_argument("--key", help="SSH key file (default: config.yaml server.key_file)")
    ns = sub.add_parser("import-nse", help="NSE closing prices of all NIFTY options (bhavcopy) -> smile correction")
    ns.add_argument("--from", dest="start", default="2023-01-01")
    ns.add_argument("--to", dest="end", default=date.today().isoformat())
    ns.add_argument("--rebuild", action="store_true", help="only rebuild the smile from files already imported")
    bt = sub.add_parser("backtest")
    bt.add_argument("--dataset", help="index price set (default NIFTY_minute); NIFTY_1second = step every second on the recorded 1-second bars")
    bt.add_argument("--from", dest="start")
    bt.add_argument("--to", dest="end")
    bt.add_argument("--out", help="also copy equity/trades/cycles CSVs to this folder")
    bt.add_argument("--capital", type=float, help="capital for returns and drawdown %% (default: config.yaml)")
    bt.add_argument("--note", default="", help="a note saved with the run")
    bt.add_argument("--no-save", action="store_true", help="do not save the run under runs/")
    bt.add_argument("--real-only", action="store_true", help="(default) real option prices only")
    bt.add_argument("--allow-model", action="store_true",
                    help="also test days without real option prices, pricing options with the model")
    sw = sub.add_parser("sweep", help="try every combination of parameter values")
    sw.add_argument("--grid", action="append", default=[], metavar="NAME=V1,V2,.. or NAME=FROM:TO:STEP")
    sw.add_argument("--grid-json", help="file with {name: [values]} (the dashboard's Parameter sweep tab writes it)")
    sw.add_argument("--progress-file", help="keep {done, total, out} in this JSON file while running")
    sw.add_argument("--slippage", type=float, help="slippage, %% of premium (default: config.yaml)")
    sw.add_argument("--split", help="out-of-sample period starts on this date (YYYY-MM-DD)")
    sw.add_argument("--dataset")
    sw.add_argument("--from", dest="start")
    sw.add_argument("--to", dest="end")
    sw.add_argument("--capital", type=float)
    sw.add_argument("--workers", type=int, help="parallel processes (default: CPU cores - 1)")
    sw.add_argument("--top", type=int, default=20, help="rows to print")
    pu = sub.add_parser("publish", help="send saved runs to the server dashboard's Backtests page")
    pu.add_argument("runs", nargs="*", help="run folder names (default: every run not sent yet)")
    pu.add_argument("--again", action="store_true", help="also re-send runs that were sent before")
    ru = sub.add_parser("runs", help="list saved backtest runs")
    ru.add_argument("--last", type=int, default=20)
    pp = sub.add_parser("paper")
    pp.add_argument("--feed", choices=["sim", "kite"], default="sim")
    pp.add_argument("--fresh", action="store_true", help="discard the saved paper account and start over")
    for p in (bt, pp, sw):
        p.add_argument("--strategy", default="short_straddle")
        p.add_argument("--params", help="JSON of strategy parameters")
        p.add_argument("--set", action="append", metavar="NAME=VALUE")
    dl = sub.add_parser("download")
    dl.add_argument("--from", dest="start", default="2023-01-01")
    dl.add_argument("--to", dest="end", default=date.today().isoformat())
    dl.add_argument("--interval", default="5minute")
    dl.add_argument("--here", action="store_true", help="use this Mac's own Zerodha login instead of the server's")
    do = sub.add_parser("download-options")
    do.add_argument("--strikes", type=int, default=10)
    do.add_argument("--expiries", type=int, default=2)
    do.add_argument("--interval", default="5minute")
    td = sub.add_parser("truedata", help="TrueData history: NIFTY, VIX and expired option candles")
    td.add_argument("step", choices=["test", "spot", "contracts", "plan", "options", "all", "status", "depth", "stocks", "ticks", "live-test"],
                    help="test = check login; all = spot + contracts + options; depth = how far back data goes; "
                         "live-test = count real-time ticks per second (market hours); "
                         "ticks = tick history with bid/ask, last 5 trading days")
    td.add_argument("--seconds", type=float, default=120, help="live-test: how long to count (default 120)")
    td.add_argument("--underlying", default="NIFTY", help="NIFTY (default), BANKNIFTY or SENSEX")
    td.add_argument("--symbols", nargs="+", help="stocks: these NSE symbols instead of the NIFTY 50")
    td.add_argument("--no-futures", action="store_true", help="stocks: cash equity only")
    td.add_argument("--nearest", action="store_true",
                    help="each expiry only during its own week, while it is the nearest expiry")
    td.add_argument("--open", action="store_true",
                    help="also contracts not expired yet (their days so far), not only expired ones")
    td.add_argument("--strikes", type=int, default=10, help="ticks: strikes either side of the day's range (default 10)")
    td.add_argument("--tick-days", type=int, default=5, help="ticks: trading days back (default 5, TrueData's limit)")
    td.add_argument("--from", dest="start", default=(date.today() - timedelta(days=183)).isoformat())
    td.add_argument("--to", dest="end", default=date.today().isoformat())
    td.add_argument("--band", type=float, help="strikes within this %% of NIFTY (default 10)")
    td.add_argument("--days", type=int, help="candles from this many days before each expiry (default 35)")
    td.add_argument("--workers", type=int, help="parallel requests (default 4)")
    td.add_argument("--limit", type=int, help="only this many contracts (for a trial run)")
    td.add_argument("--retry-errors", action="store_true", help="also retry contracts that failed before")
    args = ap.parse_args()
    cfg = load_config()

    if args.cmd == "sample-data":
        from algolab.feeds import write_sample
        print("Wrote", write_sample())
    elif args.cmd == "strategies":
        for name, cls in load_strategies().items():
            print(f"{name}: {cls.title}")
            for k, p in cls.PARAMS.items():
                print(f"    {k} = {p.default!r}   ({p.label})")
    elif args.cmd == "backtest":
        from algolab.engine import backtest
        from algolab.feeds import list_datasets
        dataset = args.dataset or (list_datasets() or [None])[0]
        if not dataset:
            sys.exit("No data yet. Run: python run.py sample-data   (or download from Zerodha)")
        if args.capital:
            cfg["account"]["capital"] = args.capital
        if args.allow_model:
            cfg.setdefault("pricing", {})["real_only"] = False
        strat = load_strategies()[args.strategy](**_params(args))
        res = backtest(cfg, strat, dataset, args.start, args.end)
        print(f"\nBacktest of {args.strategy} on {dataset}\n" + "-" * 44)
        for k, v in res.metrics.items():
            print(f"{k:<30}{v:>14,}" if isinstance(v, (int, float)) else f"{k:<30}{v:>14}")
        if args.out:
            out = Path(args.out)
            out.mkdir(parents=True, exist_ok=True)
            res.equity.to_csv(out / "equity.csv")
            res.trades.to_csv(out / "trades.csv", index=False)
            res.cycles.to_csv(out / "cycles.csv", index=False)
            print("Saved CSVs to", out)
        if dataset.startswith("SAMPLE"):
            print("\nNote: SAMPLE data is synthetic. These numbers say nothing about the real market.")
        if not args.no_save:
            from algolab.runs import save_run
            print("Run saved to", save_run(res, strat, dataset, args.start, args.end, cfg, args.note))
    elif args.cmd == "sweep":
        from algolab.feeds import list_datasets
        from algolab.sweep import parse_grid, sweep
        import pandas as pd
        if args.capital:
            cfg["account"]["capital"] = args.capital
        dataset = args.dataset or (list_datasets() or [None])[0]
        if not dataset:
            sys.exit("No data yet. Run: python run.py download --from 2023-01-01")
        grid = parse_grid(args.grid)
        if args.grid_json:
            grid.update({k: [str(x) for x in v] for k, v in json.loads(Path(args.grid_json).read_text()).items()})
        if not grid:
            sys.exit("Give at least one --grid NAME=VALUES (or --grid-json)")
        if args.slippage is not None:
            cfg["execution"]["slippage_pct"] = args.slippage
        n = 1
        for v in grid.values():
            n *= len(v)
        prog = Path(args.progress_file) if args.progress_file else None
        t0 = time.time()
        from algolab.runs import RUNS_DIR
        folder = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_sweep_{args.strategy}"

        def note(**kw):
            if prog:
                tmp = prog.with_suffix(".tmp")
                tmp.write_text(json.dumps(dict(kw, total=n, started=t0, pid=os.getpid(), updated=time.time(),
                                               folder=folder.name)))
                os.replace(tmp, prog)

        def step(i, t):
            print(f"\r{i}/{t}", end="", flush=True)
            note(done=i)
        note(done=0)
        print(f"Sweeping {n} combinations of {', '.join(grid)} on {dataset}"
              + (f", in-sample before {args.split}, out-of-sample from it" if args.split else ""))
        try:
            df, out = sweep(args.strategy, grid, dataset, args.start, args.end, args.split, _params(args), cfg,
                            args.workers, progress=step, out=folder)
        except Exception as e:
            note(done=0, error=f"{type(e).__name__}: {e}")
            raise
        note(done=n, out=out.name)
        print()
        with pd.option_context("display.width", 200, "display.max_columns", 30):
            print(df.head(args.top).to_string(index=False))
        print("\nSaved to", out / "sweep.csv")
        if args.split:
            print("Choose settings on the in_ columns only; then check that out_ holds up and that neighbouring"
                  " values are also good. A lone best combination is usually luck.")
    elif args.cmd == "publish":
        from algolab.publish import PublishError, publish, unpublished
        from algolab.runs import RUNS_DIR
        if args.runs:
            dirs = [RUNS_DIR / n.rstrip("/").split("/")[-1] for n in args.runs]
            missing = [d.name for d in dirs if not (d / "run.json").exists()]
            if missing:
                sys.exit("No such run: " + ", ".join(missing))
        elif args.again:
            dirs = [d for d in sorted(RUNS_DIR.glob("*")) if (d / "run.json").exists() and (d / "equity.csv").exists()]
        else:
            dirs = unpublished()
        try:
            publish(dirs)
        except PublishError as e:
            sys.exit(str(e))
    elif args.cmd == "runs":
        from algolab.runs import list_runs
        import pandas as pd
        df = list_runs()
        if df.empty:
            print("No saved runs yet.")
        else:
            with pd.option_context("display.width", 220, "display.max_colwidth", 60):
                print(df.head(args.last).drop(columns=["params"]).to_string(index=False))
    elif args.cmd == "paper":
        from algolab.paper import run_paper
        run_paper(cfg, args.strategy, _params(args), args.feed, args.fresh)
    elif args.cmd == "lab-agent":
        from algolab.lab_agent import Agent
        from algolab.publish import PublishError
        try:
            Agent().run(once=args.once)
        except PublishError as e:
            sys.exit(str(e))
    elif args.cmd == "backtest-queue":
        from algolab.backtest_queue import mark_stopped, process
        if args.stopped:
            n = mark_stopped(os.environ.get("SERVICE_RESULT"))
            print(f"Marked {n} job(s) as stopped." if n else "No job was left running.")
        else:
            print(f"Ran {process()} backtest(s) from the dashboard's queue.")
    elif args.cmd == "tick-test-queue":
        from algolab.kite_tick_test import process as tick_process
        print(f"Ran {tick_process()} Zerodha tick count(s).")
    elif args.cmd == "paper-remove-holiday-cycles":
        from algolab.scrub import remove_holiday_cycles
        remove_holiday_cycles(args.dry_run)
    elif args.cmd == "paper-restore":
        from algolab.scrub import restore
        restore(args.dry_run)
    elif args.cmd == "paper-scrub":
        from algolab.scrub import run as scrub
        scrub(cfg, args.day, args.dry_run)
    elif args.cmd == "catalog":
        from algolab.paper import write_catalog
        write_catalog()
    elif args.cmd == "import-nse":
        from algolab import eod
        import pandas as pd
        try:
            if not args.rebuild:
                res = eod.import_bhavcopy(args.start, args.end)
                print(f"Imported {res['saved']} days." + (f" Not available: {len(res['missing'])} days "
                      f"(holidays or not published)." if res["missing"] else ""))
            table = eod.build_smile(cfg)
            report = eod.validate(cfg, table)
        except eod.EodError as e:
            sys.exit(str(e))
        if len(report):
            print("\nHow close estimates are to real NSE closes (median error %, next-day test):")
            with pd.option_context("display.width", 160):
                print(report.to_string())
            print("\nBacktests now use these corrections automatically (config.yaml pricing.smile: auto).")
    elif args.cmd == "paper-report":
        from algolab import paper_report
        out = paper_report.weekly(cfg, days=args.days)
        print(f"{len(out['sessions'])} session(s) compared, {out['from']} to {out['to']}: {paper_report.OUT}")
    elif args.cmd == "collect":
        from algolab import kite
        from algolab.config import DATA
        log = DATA / "collect_log.txt"     # shown on the dashboard's Market data page

        def note(line):
            if not args.plan:
                DATA.mkdir(exist_ok=True)
                with log.open("a", encoding="utf-8") as f:
                    f.write(line.strip() + "\n")
        if not args.plan:                     # 1-second bars: keep the last N trading days (config sec1:)
            try:
                from algolab import sec1
                pr = sec1.prune(cfg)
                note(f"{kite.ist_now():%Y-%m-%d %H:%M}  1-second bars: {len(pr['days'])} day(s) kept "
                     f"({pr['total_mb']} MB), removed {', '.join(pr['removed']) or 'none'}")
            except Exception as e:  # noqa: BLE001 - never stop the day's collection
                print(f"1-second bars: prune failed ({e})")
        import time as _t
        done_ok = False
        for attempt in range(1, 4):           # Zerodha slow or down for a moment: wait and run it again (16 Oct)
            try:
                msg = kite.collect_daily(cfg, plan_only=args.plan)
                print(msg)
                note(msg + (f" (attempt {attempt})" if attempt > 1 else ""))
                done_ok = True
                break
            except Exception as e:  # noqa: BLE001
                if attempt < 3 and kite._transient(e) and not args.plan:
                    print(f"Attempt {attempt} failed ({e}); trying again in 2 minutes")
                    _t.sleep(120)
                    continue
                note(f"{kite.ist_now():%Y-%m-%d %H:%M}  FAILED: {e}" + (f" (after {attempt} attempts)" if attempt > 1 else ""))
                sys.exit(str(e))
        if done_ok and not args.plan:         # one 1-minute set per index; old copies to data/archive
            try:
                from algolab import datasets
                org = datasets.organize()
                if org["moved"]:
                    note(f"{kite.ist_now():%Y-%m-%d %H:%M}  Data sets: moved to data/archive: " + "; ".join(org["moved"]))
            except Exception as e:  # noqa: BLE001
                print(f"Data sets: organize failed ({e})")
            try:                              # the day's paper / live vs 1-minute and 1-second backtests, queued
                from algolab import daily_compare
                jid = daily_compare.enqueue_day(kite.ist_now().date(), cfg)
                if jid:
                    note(f"{kite.ist_now():%Y-%m-%d %H:%M}  Compare with paper queued for today ({jid})")
            except Exception as e:  # noqa: BLE001
                print(f"Compare with paper: not queued ({e})")
    elif args.cmd == "compare":
        from algolab import daily_compare as dc
        rep = dc.run(args.start, args.end, args.bars.split(","), [x for x in args.sessions.split(",") if x] or None, cfg)
        print(dc.to_csv(rep))
        print(f"{rep['flagged']} flag(s). Report: {dc.OUT / 'latest.json'}")
    elif args.cmd == "data-organize":
        from algolab import datasets
        print(json.dumps(datasets.organize(dry_run=args.dry_run), indent=1))
    elif args.cmd == "sec1":
        from algolab import sec1
        out = sec1.prune(cfg, dry_run=args.dry_run) if args.action == "prune" else sec1.status()
        print(json.dumps(out, indent=1))
    elif args.cmd == "sync-data":
        from algolab import kite
        srv = cfg.get("server", {})
        print(kite.sync_from_server(args.host or srv.get("host", ""), args.key or srv.get("key_file", ""),
                                    srv.get("data_path", "/opt/algolab/data")))
    elif args.cmd == "paper-service":
        from algolab.paper import run_service
        run_service()
    elif args.cmd == "login":
        from algolab import kite
        from algolab.config import ist_now
        now = ist_now()
        if now.weekday() < 5 and (8, 45) <= (now.hour, now.minute) <= (15, 45) and not kite.shared_token_file():
            print("Careful: it is market hours in India. Zerodha keeps one login per API key, so logging in\n"
                  "here signs out the server (dashboard and paper engines) until someone logs in there again.\n"
                  "Downloading history works just as well after 3:30 pm IST.")
            if input("Type yes to log in anyway: ").strip().lower() != "yes":
                sys.exit("Not logged in.")
        print("1. Open this address and log in to Zerodha:\n\n   " + kite.login_url())
        text = input("\n2. Paste the address you were redirected to (or just the request_token): ")
        print("Logged in as", kite.complete_login(text)["user"])
    elif args.cmd == "download":
        from algolab import kite
        from algolab.publish import PublishError
        a, b = date.fromisoformat(args.start), date.fromisoformat(args.end)
        try:
            if args.here or kite.session_info() is not None:
                path, n = kite.download_spot(a, b, args.interval)
            else:   # no login on this Mac: let the server dashboard fetch it with its login
                path, n = kite.download_spot_via_server(a, b, args.interval)
        except (PublishError, kite.KiteError) as e:
            sys.exit(str(e))
        print(f"Saved {n:,} bars to {path}")
    elif args.cmd == "download-options":
        from algolab import kite
        n = kite.download_options(args.strikes, args.expiries, args.interval, cfg=cfg,
                                  progress=lambda i, t: print(f"\r{i}/{t}", end=""))
        print(f"\nSaved candles for {n} contracts")
    elif args.cmd == "truedata":
        from algolab import truedata
        tcfg = cfg.setdefault("truedata", {})
        for key, val in (("band_pct", args.band), ("days_before_expiry", args.days), ("workers", args.workers)):
            if val is not None:
                tcfg[key] = val
        try:
            if args.step == "live-test":
                from algolab import td_live_test
                td_live_test.run(args.seconds, cfg)
                return
            truedata.use_underlying(cfg, args.underlying)
            if args.step == "test":
                truedata.test_login(cfg)
                return
            if args.step == "depth":
                truedata.history_depth(cfg)
                return
            dl = truedata.Downloader(cfg, args.start, args.end, nearest=args.nearest, open_contracts=args.open)
            if args.step == "ticks":
                truedata.download_ticks(dl, strikes=args.strikes, days=args.tick_days)
                return
            if args.step == "stocks":
                syms, source = (args.symbols, "your list") if args.symbols else truedata.nifty50_symbols()
                print(f"{len(syms)} stocks from {source}")
                (dl.root / "equity").mkdir(parents=True, exist_ok=True)
                (dl.root / "equity" / "symbols.txt").write_text("\n".join(syms) + "\n")
                truedata.download_stocks(dl, syms, futures=not args.no_futures)
                print("\n".join(dl.status()))
                return
            if args.step in ("spot", "all"):
                if not dl.spot():
                    sys.exit("No NIFTY prices came back - run: python run.py truedata test")
            if args.step in ("contracts", "all"):
                dl.contracts()
            if args.step in ("plan", "all"):
                p = dl.plan()
                print(f"To download: {p['contracts']:,} contracts, ~{p['requests']:,} requests, {p['estimate']}")
            if args.step in ("options", "all"):
                dl.options(retry_errors=args.retry_errors, limit=args.limit)
            print("\n".join(dl.status()))
        except truedata.TrueDataError as e:
            sys.exit(str(e))
        except KeyboardInterrupt:
            sys.exit("\nStopped. Run the same command again to carry on where it left off.")
    elif args.cmd == "dashboard":
        subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "dashboard.py")])


if __name__ == "__main__":
    main()
