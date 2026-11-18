"""Dashboard.  Start with:  python -m streamlit run dashboard.py"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
import time
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from algolab import paper
from algolab.config import ROOT, load_config
from algolab.engine import backtest
from algolab.feeds import SPOT_DIR, list_datasets, write_sample
from algolab.runs import save_run
from algolab.strategy import load_strategies

st.set_page_config(page_title="Algo Lab", page_icon="📈", layout="wide")
cfg = load_config()
STRATS = load_strategies()
_ver = tuple(int(x) for x in st.__version__.split(".")[:2])
WIDE = {"width": "stretch"} if _ver >= (1, 50) else {"use_container_width": True}   # name changed in Streamlit 1.50
MONEY = {"Net P&L", "Max drawdown", "Charges paid", "Avg win", "Avg loss", "Best cycle", "Worst cycle", "Worst week"}


def rupees(x) -> str:
    return f"-₹{-x:,.0f}" if x < 0 else f"₹{x:,.0f}"


try:                                                       # newer Streamlit: own arrow and named colours for deltas
    import streamlit.elements.metric as _stm
    _DELTA_ARROW = "delta_arrow" in inspect.signature(st.metric).parameters and "red" in str(getattr(_stm, "DeltaColor", ""))
except Exception:  # noqa: BLE001
    _DELTA_ARROW = False


def move(x, fmt=rupees) -> dict:
    """A metric's change in green or red with an arrow and no + / − (the colour says which way, as on the server)."""
    if x is None or x != x:
        return {}
    if not _DELTA_ARROW:                                   # older Streamlit: its own arrow needs the sign
        return {"delta": fmt(x)}
    return {"delta": fmt(abs(x)), "delta_arrow": "up" if x > 0 else "down" if x < 0 else "off",
            "delta_color": "green" if x > 0 else "red" if x < 0 else "gray"}


def _theme() -> str:
    try:
        return st.context.theme.type or "light"
    except AttributeError:                                 # older Streamlit
        return "light"


def chart(fig, **kw):
    """st.plotly_chart with readable axis text: 13px and dark enough to read (Streamlit's default grey is 3.7:1)."""
    tick = "#c5cad3" if _theme() == "dark" else "#4b5563"
    fig.update_layout(font=dict(size=13))
    fig.update_xaxes(tickfont=dict(color=tick, size=12.5), title_font=dict(color=tick))
    fig.update_yaxes(tickfont=dict(color=tick, size=12.5), title_font=dict(color=tick))
    for an in fig.layout.annotations or ():               # labels on the chart (target, stop, strikes): readable too
        if not (an.font and an.font.color):
            an.font = dict(color=tick, size=max(12, (an.font.size if an.font and an.font.size else 12)))
        elif an.font.size and an.font.size < 12:
            an.font.size = 12
    for tr in fig.data:                                    # colour bars of heatmaps and scatter markers
        if getattr(tr, "colorbar", None) is not None:
            tr.colorbar.tickfont = dict(color=tick)
        mk = getattr(tr, "marker", None)
        if mk is not None and getattr(mk, "colorbar", None) is not None and mk.showscale:
            mk.colorbar.tickfont = dict(color=tick)
    if fig.layout.title.text and fig.layout.showlegend is not False and len(fig.data) > 1:
        fig.update_layout(legend=dict(orientation="h", y=-0.12, yanchor="top", x=0))   # under the plot, clear of the title
    return st.plotly_chart(fig, **kw)


def strategy_form(prefix: str):
    """Strategy picker plus an input for each of its parameters."""
    name = st.selectbox("Strategy", list(STRATS), format_func=lambda n: STRATS[n].title, key=f"{prefix}_strategy")
    cls = STRATS[name]
    st.caption(cls.description)
    params = {}
    cols = st.columns(3)
    for i, (k, p) in enumerate(cls.PARAMS.items()):
        box, key = cols[i % 3], f"{prefix}_{name}_{k}"
        if isinstance(p.default, bool):
            params[k] = box.checkbox(p.label, p.default, key=key)
        elif p.choices:
            params[k] = box.selectbox(p.label, p.choices, p.choices.index(p.default), key=key)
        elif isinstance(p.default, int):
            params[k] = box.number_input(p.label, value=p.default, step=1, key=key,
                                         min_value=None if p.min is None else int(p.min))
        elif isinstance(p.default, float):
            params[k] = box.number_input(p.label, value=p.default, step=5.0, key=key,
                                         min_value=None if p.min is None else float(p.min))
        else:
            params[k] = box.text_input(p.label, p.default, key=key)
    return name, params


def line_chart(x, y, title, color="#2563eb", fill=False, height=300):
    fig = go.Figure(go.Scatter(x=x, y=y, mode="lines", line=dict(color=color, width=1.6),
                               fill="tozeroy" if fill else None))
    fig.update_layout(title=title, height=height, margin=dict(l=10, r=10, t=40, b=10),
                      xaxis=dict(rangebreaks=[dict(bounds=["sat", "mon"])]), yaxis=dict(tickprefix="₹", tickformat=","))
    return fig


HEAD = ("Net P&L", "Max drawdown", "Return / drawdown")
CHIPS = [("Cycles", "cycles", "{:,}"), ("Win rate %", "won", "{}%"), ("Profit factor", "profit factor", "{}"),
         ("Annualised return %", "a year", "%"), ("Worst week", "worst week", None), ("Charges paid", "charges", None)]


def chips(items) -> None:
    """Small rounded labels for secondary figures, in one line (the server pages' chips)."""
    dark = _theme() == "dark"
    line, txt, mut = ("#3a3f4b", "#e8eaee", "#aab1bd") if dark else ("#d9dce2", "#16181d", "#5f6673")
    html = "".join(f'<span style="display:inline-flex;gap:4px;align-items:baseline;border:1px solid {line};border-radius:999px;'
                   f'padding:2px 10px;margin:0 6px 6px 0;font-size:13px;color:{mut}"><b style="color:{c or txt};font-weight:600">{v}</b>{k}</span>'
                   for v, k, c in items)
    st.markdown(f'<div style="display:flex;flex-wrap:wrap;margin:-6px 0 4px">{html}</div>', unsafe_allow_html=True)


def headline(metrics: dict) -> None:
    """Net P&L, max drawdown and return / drawdown side by side, the rest as chips, all figures folded."""
    m = metrics
    a, b, c, _ = st.columns([1.2, 1, 1, 1.3])
    net, cap = m.get("Net P&L"), m.get("Return on capital %")
    a.metric("Net P&L", rupees(net) if net is not None else "–",               # value is not coloured: keeps its sign
             **(move(cap, lambda v: f"{v}% on capital") if cap is not None else move(net)))
    dd = m.get("Max drawdown")
    b.metric("Max drawdown", rupees(abs(dd)) if dd is not None else "–",
             **({"delta": f"{abs(m['Max drawdown %'])}%", "delta_color": "red", "delta_arrow": "down"} if _DELTA_ARROW and m.get("Max drawdown %") is not None else {}))
    c.metric("Return / drawdown", m.get("Return / drawdown", "–"))
    red, green = ("#f0626a", "#34c98a") if _theme() == "dark" else ("#c8323a", "#087a4c")
    out = []
    for key, label, fmt in CHIPS:
        v = m.get(key)
        if v is None or v == "":
            continue
        if fmt in (None, "%"):                            # money and returns: no sign, colour says which way
            col = None if key == "Charges paid" else (green if v > 0 else red if v < 0 else None)
            out.append((rupees(abs(v)) if fmt is None else f"{abs(v)}%", label, col))
        else:
            out.append((fmt.format(v), label, None))
    chips(out)
    with st.expander("All figures"):
        metric_grid({k: v for k, v in m.items() if k not in HEAD})


COLS = {"group": "Cycle", "entry_time": "Entered", "exit_time": "Exited", "legs": "Legs", "gross_pnl": "Gross",
        "costs": "Charges", "net_pnl": "Net", "exit_reason": "Why it closed", "reason": "Reason", "contract": "Contract",
        "side": "Side", "qty": "Qty", "entry_price": "Entry", "exit_price": "Exit", "symbol": "Contract"}
_MONEY_COLS = {"gross_pnl", "costs", "net_pnl", "pnl"}


def nice_table(df: pd.DataFrame, **kw) -> None:
    """A trades / cycles table with plain column names, short times and ₹ amounts."""
    d = df.copy()
    for c in d.columns:
        if c.endswith("_time") or c == "time":
            d[c] = pd.to_datetime(d[c], errors="coerce").dt.strftime("%d %b %y %H:%M")
    conf = {c: st.column_config.Column(COLS[c]) for c in d.columns if c in COLS}
    money = [c for c in d.columns if c in _MONEY_COLS]
    fmt = {c: rupees for c in money}
    fmt.update({c: "{:,.2f}" for c in ("entry_price", "exit_price", "price") if c in d.columns})
    st.dataframe(d.style.format(fmt, na_rep="–"), column_config=conf, hide_index=True, **WIDE, **kw)


def metric_grid(metrics: dict, per_row: int = 5) -> None:
    items = list(metrics.items())
    for i in range(0, len(items), per_row):
        for col, (k, v) in zip(st.columns(per_row), items[i:i + per_row]):
            col.metric(k, rupees(v) if k in MONEY else (f"{v:,}" if isinstance(v, (int, float)) else v))


st.title("Algo Lab")
st.caption("Backtest and paper-trade option strategies on NIFTY. No real orders are ever placed.")
@st.cache_data(ttl=300, show_spinner=False)
def _real_days() -> list:
    from algolab.feeds import OptionStore
    return sorted(OptionStore(cfg["market"]["underlying"]).real_days())


REAL_ONLY = bool(cfg.get("pricing", {}).get("real_only", True))
def real_datasets() -> list:
    """Price data offered for backtests and replays: one 1-minute set per index (algolab/datasets.py), NIFTY first.
    Falls back to whatever is stored (the sample included) on a computer that has no index set yet."""
    from algolab.datasets import screen_datasets
    names = screen_datasets(SPOT_DIR)
    if not names:
        names = list_datasets()
        if REAL_ONLY:
            names = [n for n in names if not n.startswith("SAMPLE")] or names
    return names


def real_span(dataset: str):
    """(first, last, default_from, default_to) for date pickers: the dataset's span, narrowed to the
    days that have real option prices."""
    span = pd.read_csv(SPOT_DIR / f"{dataset}.csv", usecols=["datetime"], parse_dates=["datetime"])["datetime"]
    first, last = span.min().date(), span.max().date()
    rdays = [d for d in _real_days() if first <= d <= last] if REAL_ONLY else []
    lo, hi = (rdays[0], rdays[-1]) if rdays else (first, last)
    return first, last, lo, hi


def real_note(start, end) -> None:
    if not REAL_ONLY:
        return
    rdays = _real_days()
    if not rdays:
        st.warning("No real option prices are stored yet. Get them first: Zerodha (deploy/get-server-data.command, "
                   "get-minute-data.command) or TrueData (deploy/get-truedata.command).")
        return
    n = sum(1 for d in rdays if start <= d <= end)
    st.caption(f"Real option prices only (Zerodha and TrueData). Stored for {len(rdays)} trading days, "
               f"{rdays[0]:%d %b %Y} to {rdays[-1]:%d %b %Y}; {n} of them fall in these dates. Days without real "
               "prices for the at-the-money options are skipped.")


def price_note(metrics: dict) -> None:
    n = metrics.get("Prices with no real trade (modelled)")
    if n:
        st.warning(f"{n} price look-ups found no real trade nearby and were modelled (a strike that did not trade "
                   "that day). The strategy log lists them.")
    elif not REAL_ONLY and metrics.get("Fills on real option prices") != "100%":
        st.caption("Option prices without a recorded quote are modelled (Black-Scholes on India VIX). "
                   "Treat results as indicative until you have real option data.")


tab_bt, tab_sw, tab_rp, tab_paper, tab_data, tab_help = st.tabs(
    ["Backtest", "Parameter sweep", "Replay", "Paper trading", "Data", "Help"])

# ---------------------------------------------------------------- backtest
with tab_bt:
    datasets = real_datasets()
    if not datasets:
        st.info("No price data yet. Get real NIFTY and option prices in the Data tab "
                "(Zerodha, or TrueData with deploy/get-truedata.command).")
    else:
        name, params = strategy_form("bt")
        c1, c2, c3, c4, c5 = st.columns(5)
        dataset = c1.selectbox("Price data", datasets, help="One 1-minute set per index: TrueData history (Oct 2024 on) "
                               "followed by Zerodha's daily bars.")
        first, last, lo, hi = real_span(dataset)
        start = c2.date_input("From", lo, min_value=first, max_value=last, key=f"bt_from_{dataset}")
        end = c3.date_input("To", hi, min_value=first, max_value=last, key=f"bt_to_{dataset}")
        capital = c4.number_input("Capital (₹)", value=int(cfg["account"]["capital"]), step=50000, min_value=10000)
        slip = c5.number_input("Slippage (% of premium)", value=float(cfg["execution"]["slippage_pct"]), step=0.25, min_value=0.0)
        if dataset.startswith("SAMPLE"):
            st.warning("This is synthetic sample data, for trying the platform. Results say nothing about the real market.")
        real_note(start, end)
        if st.button("Run backtest", type="primary"):
            run_cfg = load_config()
            run_cfg["account"]["capital"], run_cfg["execution"]["slippage_pct"] = capital, slip
            try:
                with st.spinner("Running..."):
                    strat = STRATS[name](**params)
                    st.session_state["bt"] = backtest(run_cfg, strat, dataset, start, end)
                    saved = save_run(st.session_state["bt"], strat, dataset, start, end, run_cfg)
                    st.session_state["bt_saved"] = saved.name
            except Exception as e:
                st.error(f"Backtest failed: {e}")
        res = st.session_state.get("bt")
        if res is not None:
            st.divider()
            headline(res.metrics)
            if st.session_state.get("bt_saved"):
                saved_name = st.session_state["bt_saved"]
                s1, s2 = st.columns([4, 1])
                s1.caption(f"Saved as runs/{saved_name} (settings, data fingerprint and code version included).")
                if s2.button("Send to dashboard", help="Show this run on the server dashboard's Backtests page"):
                    from algolab.publish import PublishError, publish
                    from algolab.runs import RUNS_DIR
                    try:
                        msgs = []
                        publish([RUNS_DIR / saved_name], log=msgs.append)
                        st.success(msgs[-1])
                    except PublishError as e:
                        st.error(str(e))
                    except Exception as e:  # network trouble etc.
                        st.error(f"Could not send: {e}")
            price_note(res.metrics)
            eq = res.equity
            chart(line_chart(eq.index, eq.equity, "Account value"), **WIDE)
            dd = eq.equity - eq.equity.cummax()
            chart(line_chart(eq.index, dd, "Drawdown", "#dc2626", True, 220), **WIDE)
            a, b = st.tabs([f"Cycles ({len(res.cycles)})", "Individual legs"])
            with a:
                nice_table(res.cycles)
            with b:
                nice_table(res.trades)
            d1, d2, _ = st.columns([1, 1, 4])
            d1.download_button("Download trades", res.trades.to_csv(index=False), "trades.csv")
            d2.download_button("Download equity", res.equity.to_csv(), "equity.csv")
            with st.expander("Strategy log"):
                st.code("\n".join(res.logs[-500:]) or "No activity", language=None, wrap_lines=True)

# ---------------------------------------------------------------- parameter sweep
# Choose which settings to vary and over what range; every combination is backtested (in a separate process,
# all CPU cores), on an in-sample period and, separately, an out-of-sample period. Results: ranked table,
# heatmap of two settings, and a robustness score (the average of a combination and its neighbours).
SWEEP_METRICS = {"ret_dd": "Return / drawdown", "net": "Net P&L", "max_dd": "Max drawdown", "win_pct": "Win rate %",
                 "worst_week": "Worst week", "cycles": "Cycles"}
SWEEP_DIR = ROOT / "runs" / ".sweeps"


def _pid_alive(pid) -> bool:
    try:
        import os
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _sweep_jobs():
    """Every sweep started from this tab (newest first): (progress file, progress dict, state), where state is
    running, done, failed or stopped. Read from runs/.sweeps, so it survives a page refresh or a restart."""
    out = []
    if not SWEEP_DIR.is_dir():
        return out
    for pf in sorted(SWEEP_DIR.glob("*.json"), reverse=True):
        if pf.name.endswith("_grid.json") or pf.name == "speed.json":
            continue
        try:
            prog = json.loads(pf.read_text())
        except (OSError, ValueError):
            continue
        state = ("done" if prog.get("out") else "failed" if prog.get("error")
                 else "running" if _pid_alive(prog.get("pid")) else "stopped")
        out.append((pf, prog, state))
    return out


def _speed(dataset, prog=None, workers=None):
    """Seconds one combination takes on one core, learnt from earlier sweeps (per data set)."""
    f = SWEEP_DIR / "speed.json"
    try:
        known = json.loads(f.read_text())
    except (OSError, ValueError):
        known = {}
    if prog and workers and prog.get("done"):
        el = prog.get("updated", 0) - prog.get("started", 0)
        if el > 0:
            known[dataset] = round(el * min(workers, prog["done"]) / prog["done"], 1)
            try:
                f.write_text(json.dumps(known))
            except OSError:
                pass
    return known.get(dataset)


def _hm(sec):
    sec = int(sec)
    return f"{sec // 3600} h {sec % 3600 // 60:02d} min" if sec >= 3600 else f"{max(1, sec // 60)} min"


@st.fragment(run_every=5)
def sweep_status():
    """Progress of running sweeps (refreshes itself every 5 s) and notes about ones that ended."""
    jobs = _sweep_jobs()
    now_running = {pf.name for pf, _, state in jobs if state == "running"}
    before = st.session_state.get("sw_running", set())
    st.session_state["sw_running"] = now_running
    if before - now_running:   # a sweep just ended: refresh the whole tab (results list, Start button)
        st.rerun(scope="app")
    for pf, prog, state in jobs[:5]:
        seen = st.session_state.setdefault("sw_seen", set())
        if state == "running":
            done, total = prog.get("done", 0), max(1, prog.get("total", 1))
            el = time.time() - prog.get("started", time.time())
            eta = el / done * (total - done) if done else None
            st.progress(done / total, text=f"Sweep running ({pf.stem}): {done} of {total} combinations"
                        + (f" · about {_hm(eta)} left" if eta else " · the first results take a few minutes"))
            meta = SWEEP_DIR / f"{pf.stem}_meta.json"
            try:
                m = json.loads(meta.read_text())
                _speed(m.get("dataset"), prog, m.get("workers"))
            except (OSError, ValueError):
                pass
            if st.button("Stop this sweep", key=f"stop_{pf.stem}"):
                import os
                import signal
                try:
                    os.killpg(os.getpgid(int(prog["pid"])), signal.SIGTERM)   # the sweep and its parallel runs
                except (OSError, KeyError, ValueError):
                    try:
                        os.kill(int(prog["pid"]), signal.SIGTERM)
                    except (OSError, KeyError, ValueError):
                        pass
                time.sleep(1)
                st.rerun()
        elif pf.name in seen:
            continue
        elif state == "done":
            seen.add(pf.name)
            if time.time() - prog.get("updated", 0) < 3600:
                st.success(f"Sweep finished: {prog.get('total')} combinations in {_hm(prog.get('updated', 0) - prog.get('started', 0))}.")
                st.session_state.setdefault("sw_open", prog["out"])
        elif time.time() - prog.get("updated", 0) < 6 * 3600:
            seen.add(pf.name)
            why = prog.get("error") or ("it was stopped, the Mac went to sleep, or macOS closed it for lack of memory "
                                        "(several sweeps at once, or too many parallel runs)")
            part = f" Results of the {prog.get('done', 0)} finished combinations are kept below." if prog.get("folder") and prog.get("done") else ""
            if part:
                st.session_state.setdefault("sw_open", prog["folder"])
            st.warning(f"Sweep {pf.stem} stopped after {prog.get('done', 0)} of {prog.get('total', '?')} combinations: {why}.{part}")
    if now_running:
        st.caption("A sweep runs on its own: you can close this page and come back. One sweep at a time.")


def sweep_setup():
    from algolab.sweep import grid_size, value_range
    name = st.selectbox("Strategy", list(STRATS), format_func=lambda n: STRATS[n].title, key="sw_strategy")
    cls = STRATS[name]
    st.caption("Tick **Vary** for each setting to test. Numbers and times take a range (From, To, Step; times step in "
               "minutes); other settings take a list. Settings you do not vary keep the value shown.")
    grid, fixed = {}, {}
    head = st.columns([3, 1, 2, 2, 2])
    for h, t in zip(head, ["Setting", "Vary", "From / value", "To", "Step"]):
        h.markdown(f"**{t}**")
    for k, p in cls.PARAMS.items():
        key = f"sw_{name}_{k}"
        c = st.columns([3, 1, 2, 2, 2])
        c[0].markdown(p.label)
        vary = c[1].checkbox("Vary", key=key + "_v", label_visibility="collapsed")
        d = p.default
        is_time = isinstance(d, str) and ":" in d
        numeric = isinstance(d, (int, float)) and not isinstance(d, bool)
        if not vary:
            if isinstance(d, bool):
                fixed[k] = c[2].checkbox("on", d, key=key + "_f")
            elif p.choices:
                fixed[k] = c[2].selectbox("value", p.choices, p.choices.index(d), key=key + "_f", label_visibility="collapsed")
            else:
                fixed[k] = c[2].text_input("value", str(d), key=key + "_f", label_visibility="collapsed")
            continue
        try:
            if isinstance(d, bool):
                grid[k] = ["True", "False"]
                c[2].caption("on and off")
            elif p.choices:
                grid[k] = [str(x) for x in c[2].multiselect("values", p.choices, p.choices, key=key + "_l",
                                                            label_visibility="collapsed")]
            elif numeric or is_time:
                lo = c[2].text_input("from", str(d), key=key + "_a", label_visibility="collapsed")
                hi = c[3].text_input("to", str(d), key=key + "_b", label_visibility="collapsed")
                stp = c[4].text_input("step", "10" if is_time else ("1" if isinstance(d, int) else "5"), key=key + "_s",
                                      label_visibility="collapsed")
                grid[k] = value_range(d, lo, hi, stp)
            else:
                txt = c[2].text_input("values, comma-separated", str(d), key=key + "_l", label_visibility="collapsed")
                grid[k] = [x.strip() for x in txt.split(",") if x.strip()]
            if numeric and p.min is not None and grid[k] and min(float(x) for x in grid[k]) < p.min:
                c[4].error(f"below {p.min:g}")
                grid[k] = []
            if k in grid and (numeric or is_time):
                c[0].caption(", ".join(grid[k][:8]) + (" …" if len(grid[k]) > 8 else ""))
        except ValueError as e:
            c[4].error(str(e))
            grid[k] = []
    return name, grid, fixed, grid_size(grid) if grid else 0


def sweep_name(n: str) -> str:
    """'20261016-122727_sweep_short_straddle' -> '16 Oct 12:27 · Positional short ATM straddle'."""
    stamp, _, strat = n.partition("_sweep_")
    try:
        when = pd.to_datetime(stamp, format="%Y%m%d-%H%M%S").strftime("%d %b %H:%M")
    except ValueError:
        when = stamp
    return f"{when} · {STRATS[strat].title if strat in STRATS else strat.replace('_', ' ')}"


def sweep_results(folder):
    from algolab.sweep import heat_table, neighbour_score
    final = folder / "sweep.csv"
    df = pd.read_csv(final if final.exists() else folder / "sweep.partial.csv", dtype=str)
    meta = json.loads((folder / "sweep.json").read_text())
    if not final.exists():
        st.info(f"Partial results: {len(df)} of {meta.get('total', '?')} combinations finished before the sweep "
                "stopped (or it is still running).")
    grid = meta["grid"]
    for col in df.columns:
        if col.split("_", 1)[0] in ("in", "out", "all") and not col.endswith("_error"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    split = "in_ret_dd" in df.columns or "in_net" in df.columns
    per = ["in", "out"] if split else ["all"]
    varied = [k for k, v in grid.items() if len(v) > 1]
    st.markdown(f"**{STRATS[meta['strategy']].title if meta['strategy'] in STRATS else meta['strategy']}** · "
                f"{len(df)} combinations · {meta['dataset']} · " +
                " · ".join(f"{'in-sample' if k == 'in' else 'out-of-sample' if k == 'out' else 'period'} "
                           f"{a or 'start'} → {b or 'end'}" for k, (a, b) in meta["periods"].items()))
    errs = [c for c in df.columns if c.endswith("_error")]
    if errs and df[errs].notna().any().any():
        st.warning(f"{int(df[errs].notna().any(axis=1).sum())} combinations failed (no data in range, or an invalid "
                   "setting). They are at the bottom of the table.")

    c1, c2 = st.columns(2)
    mkey = c1.selectbox("Rank by", list(SWEEP_METRICS), format_func=SWEEP_METRICS.get, key=f"swr_m_{folder.name}")
    base = per[0]
    col = f"{base}_{mkey}"
    df["robust"] = neighbour_score(df, grid, col)
    order = c2.radio("Order", ["Best " + ("in-sample" if split else "result"), "Most robust (with neighbours)"],
                     horizontal=True, key=f"swr_o_{folder.name}")
    df = df.sort_values("robust" if order.startswith("Most") else col, ascending=False, na_position="last")
    show = varied + [f"{p}_{m}" for p in per for m in SWEEP_METRICS if f"{p}_{m}" in df.columns] + ["robust"]
    names = {f"{p}_{m}": f"{'In' if p == 'in' else 'Out' if p == 'out' else ''} {SWEEP_METRICS[m]}".strip()
             for p in per for m in SWEEP_METRICS}
    names["robust"] = f"Robustness ({SWEEP_METRICS[mkey]}, avg with neighbours)"
    plabels = STRATS[meta["strategy"]].PARAMS if meta["strategy"] in STRATS else {}
    names.update({k: plabels[k].label for k in varied if k in plabels})
    table = df[show].rename(columns=names).reset_index(drop=True)
    table.index = table.index + 1
    money = [names[f"{p}_{m}"] for p in per for m in ("net", "max_dd", "worst_week") if f"{p}_{m}" in df.columns]
    fmt = {c: (lambda v: "–" if pd.isna(v) else rupees(v)) for c in money}
    fmt.update({c: "{:.2f}" for c in table.columns if c not in money and table[c].dtype.kind == "f"})
    st.dataframe(table.head(200).style.format(fmt, na_rep="–"), **WIDE, height=min(420, 38 + 35 * min(len(table), 12)))
    st.download_button("Download all results (CSV)", df.to_csv(index=False), f"{folder.name}.csv", key=f"swr_dl_{folder.name}")

    if split and f"in_{mkey}" in df and f"out_{mkey}" in df:
        fig = go.Figure(go.Scatter(x=df[f"in_{mkey}"], y=df[f"out_{mkey}"], mode="markers",
                                   text=[", ".join(f"{k}={r[k]}" for k in varied) for _, r in df.iterrows()],
                                   hovertemplate="%{text}<br>in %{x}<br>out %{y}<extra></extra>",
                                   marker=dict(size=8, color=df["robust"], colorscale="Viridis", showscale=True,
                                               colorbar=dict(title="Robust"))))
        fig.update_layout(title=f"In-sample vs out-of-sample: {SWEEP_METRICS[mkey]}", height=380,
                          margin=dict(l=10, r=10, t=40, b=10), xaxis_title="In-sample", yaxis_title="Out-of-sample")
        chart(fig, **WIDE)
        st.caption("Points along a rising line mean the in-sample ranking carried over. A cloud with no slope means "
                   "the in-sample winners were mostly luck.")

    if len(varied) >= 2:
        h1, h2, h3 = st.columns(3)
        lbl = lambda k: plabels[k].label if k in plabels else k
        x = h1.selectbox("Across", varied, 0, format_func=lbl, key=f"swr_x_{folder.name}")
        y = h2.selectbox("Down", [v for v in varied if v != x], 0, format_func=lbl, key=f"swr_y_{folder.name}")
        hp = h3.selectbox("Period", per, format_func=lambda p: {"in": "In-sample", "out": "Out-of-sample", "all": "Whole period"}[p],
                          key=f"swr_p_{folder.name}")
        t = heat_table(df, x, y, f"{hp}_{mkey}")
        fig = go.Figure(go.Heatmap(z=t.values, x=[str(c) for c in t.columns], y=[str(i) for i in t.index], colorscale="RdBu", zmid=0,
                                   text=[[("" if pd.isna(v) else f"{v:,.2f}" if abs(v) < 100 else f"{v:,.0f}") for v in row] for row in t.values],
                                   texttemplate="%{text}", hovertemplate=f"{x}=%{{x}}<br>{y}=%{{y}}<br>%{{z}}<extra></extra>"))
        fig.update_layout(title=f"{SWEEP_METRICS[mkey]} by {lbl(x).split(',')[0]} and {lbl(y).split(',')[0]}" + (" (other settings averaged)" if len(varied) > 2 else ""),
                          height=420, margin=dict(l=10, r=10, t=40, b=10), xaxis_title=lbl(x), yaxis_title=lbl(y),
                          xaxis=dict(type="category"), yaxis=dict(type="category"))
        chart(fig, **WIDE)
    elif len(varied) == 1:
        v = varied[0]
        g = df.groupby(v)[[f"{p}_{mkey}" for p in per]].mean()
        g = g.reindex(sorted(g.index, key=lambda s: float(str(s).replace(":", ".")) if str(s).replace(":", "").replace(".", "").lstrip("-").isdigit() else 0))
        fig = go.Figure([go.Scatter(x=[str(i) for i in g.index], y=g[c], mode="lines+markers",
                                    name={"in": "In-sample", "out": "Out-of-sample", "all": "Whole period"}[c.split("_")[0]]) for c in g.columns])
        fig.update_layout(title=f"{SWEEP_METRICS[mkey]} by {v}", height=360, margin=dict(l=10, r=10, t=40, b=10),
                          xaxis=dict(type="category", title=v))
        chart(fig, **WIDE)

    st.markdown("**Backtest one combination over the whole period**")
    pick = st.selectbox("Combination (rank in the table above)", list(range(1, min(len(table), 200) + 1)),
                        format_func=lambda i: f"{i}. " + ", ".join(f"{names.get(k, k).split(',')[0]} {table.iloc[i - 1][names.get(k, k)]}" for k in varied),
                        key=f"swr_pick_{folder.name}")
    if st.button("Run this backtest", key=f"swr_run_{folder.name}"):
        row = df.reset_index(drop=True).iloc[pick - 1]
        params = dict(meta.get("base_params") or {})
        params.update({k: row[k] for k in grid})
        periods = list(meta["periods"].values())
        start, end = periods[0][0], periods[-1][1]
        run_cfg = load_config()
        if meta.get("config"):
            run_cfg["account"]["capital"] = meta["config"]["account"]["capital"]
            run_cfg["execution"]["slippage_pct"] = meta["config"]["execution"]["slippage_pct"]
        try:
            with st.spinner("Running..."):
                strat = STRATS[meta["strategy"]](**params)
                res = backtest(run_cfg, strat, meta["dataset"], start, end)
                saved = save_run(res, strat, meta["dataset"], start, end, run_cfg, note=f"from sweep {folder.name}")
            st.session_state["bt"], st.session_state["bt_saved"] = res, saved.name
            st.success(f"Done. The full result is on the Backtest tab (saved as runs/{saved.name}).")
            headline(res.metrics)
            eq = res.equity
            chart(line_chart(eq.index, eq.equity, "Account value"), **WIDE)
        except Exception as e:
            st.error(f"Backtest failed: {e}")


with tab_sw:
    datasets = real_datasets()
    if not datasets:
        st.info("No price data yet.")
    else:
        from algolab.sweep import memory_gb, safe_workers
        running = any(j[2] == "running" for j in _sweep_jobs())
        sweep_status()

        with st.expander("New sweep", expanded=not running and not st.session_state.get("sw_open")):
            name, grid, fixed, n = sweep_setup()
            c1, c2, c3, c4 = st.columns(4)
            dataset = c1.selectbox("Price data", datasets, key="sw_dataset")
            first, last, lo, hi = real_span(dataset)
            start = c2.date_input("From", lo, min_value=first, max_value=last, key=f"sw_from_{dataset}")
            end = c3.date_input("To", hi, min_value=first, max_value=last, key=f"sw_to_{dataset}")
            span = max(1, (end - start).days)
            use_split = c4.checkbox("Out-of-sample check", True, help="Rank on the earlier part (in-sample), then see how "
                                    "the same settings did on the later part they were not chosen on.")
            c5, c6, c7, c8 = st.columns(4)
            split = c5.date_input("Out-of-sample from", start + timedelta(days=int(span * 0.7)), min_value=start,
                                  max_value=end, key=f"sw_split_{dataset}", disabled=not use_split)
            capital = c6.number_input("Capital (₹)", value=int(cfg["account"]["capital"]), step=50000, min_value=10000, key="sw_cap")
            slip = c7.number_input("Slippage (% of premium)", value=float(cfg["execution"]["slippage_pct"]), step=0.25,
                                   min_value=0.0, key="sw_slip")
            import os as _os
            cores, safe, mem = max(1, (_os.cpu_count() or 2) - 1), safe_workers(), memory_gb()
            workers = c8.number_input("Parallel runs", value=safe, min_value=1, max_value=max(cores, 1), key="sw_workers",
                                      help=f"Backtests run side by side. Each needs about 2.5 GB of memory; this Mac has "
                                           f"{mem:.0f} GB and {cores + 1} cores, so {safe} is the safe number." if mem else "Backtests run side by side.")
            real_note(start, end)
            per = _speed(dataset)
            if not grid:
                st.info("Tick Vary for at least one setting.")
            else:
                secs = n * per / max(1, int(workers)) if per else None
                est = (f" · about **{_hm(secs)}** with {int(workers)} parallel runs (from your earlier sweeps)" if secs
                       else " · time unknown until your first sweep: start small (10–30 combinations) to learn it")
                big = n > 400 or (secs and secs > 3 * 3600)
                (st.warning if big else st.caption)(f"**{n} combinations**{est}" + (" — a lot: narrow the ranges or use bigger steps, and "
                                                                                    "remember that more combinations make a lucky winner more likely." if big else ""))
            if running:
                st.info("A sweep is already running. Wait for it, or stop it above, before starting another.")
            if st.button("Start sweep", type="primary", disabled=not grid or n == 0 or running):
                SWEEP_DIR.mkdir(parents=True, exist_ok=True)
                tag = time.strftime("%Y%m%d-%H%M%S")
                gfile, pfile = SWEEP_DIR / f"{tag}_grid.json", SWEEP_DIR / f"{tag}.json"
                gfile.write_text(json.dumps(grid))
                import shutil as _sh
                (SWEEP_DIR / f"{tag}_meta.json").write_text(json.dumps({"dataset": dataset, "workers": int(workers)}))
                awake = ["caffeinate", "-i"] if _sh.which("caffeinate") else []   # keep the Mac awake while it runs
                cmd = awake + [sys.executable, str(ROOT / "run.py"), "sweep", "--strategy", name, "--grid-json", str(gfile),
                       "--progress-file", str(pfile), "--dataset", dataset, "--from", str(start), "--to", str(end),
                       "--capital", str(capital), "--slippage", str(slip), "--workers", str(int(workers)),
                       "--params", json.dumps({k: v for k, v in fixed.items() if k not in grid})]
                if use_split:
                    cmd += ["--split", str(split)]
                subprocess.Popen(cmd, cwd=str(ROOT), stdout=open(SWEEP_DIR / f"{tag}.log", "w"), stderr=subprocess.STDOUT,
                                 start_new_session=True)
                st.session_state.pop("sw_open", None)
                time.sleep(1)
                st.rerun()

        from algolab.sweep import past_sweeps
        past = past_sweeps()
        if past:
            names = [p.name for p in past]
            want = st.session_state.get("sw_open")
            sel = st.selectbox("Results", names, names.index(want) if want in names else 0, key="sw_pick_past",
                               format_func=sweep_name)
            folder = next(p for p in past if p.name == sel)
            try:
                sweep_results(folder)
            except Exception as e:
                st.error(f"Cannot show this sweep: {e}")
        else:
            st.caption("No sweeps yet.")

        with st.expander("How to use a sweep without fooling yourself"):
            st.markdown(
                "- **Vary two or three settings at a time**, with a handful of values each. Hundreds of combinations "
                "almost guarantee one that looks great by chance.\n"
                "- **Choose on the in-sample columns only.** Then look at out-of-sample: if the winners fall apart there, "
                "the edge was not real.\n"
                "- **Prefer a plateau to a peak.** Sort by *Most robust*: a combination whose neighbours (one step away on "
                "any setting) are also good is much more likely to keep working than a lone best cell on the heatmap.\n"
                "- **Check enough cycles.** A result built on 15 cycles says little; aim for 50 or more.\n"
                "- **Last step:** backtest the chosen combination over the whole period, then paper-trade it before going live.")


# ---------------------------------------------------------------- replay
SPEEDS = {"1 bar / second": (1, 1.0), "3 bars / second": (1, 0.34), "10 bars / second": (5, 0.5),
          "30 bars / second": (15, 0.5)}


def _rp_set(i):
    rp = st.session_state.get("rp")
    if rp is not None and i is not None:
        st.session_state["rp_i"] = max(0, min(len(rp) - 1, int(i)))


def _rp_step(n):
    _rp_set(st.session_state.get("rp_i", 0) + n)


def _rp_next_trade():
    rp = st.session_state["rp"]
    j = rp.next_event(st.session_state.get("rp_i", 0))
    if j is None:
        st.session_state["rp_msg"] = "No more trades in this window."
    _rp_set(j if j is not None else len(rp) - 1)


def _rp_prev_trade():
    rp = st.session_state["rp"]
    j = rp.prev_event(st.session_state.get("rp_i", 0))
    _rp_set(j if j is not None else 0)


def _rp_toggle_play():
    rp = st.session_state.get("rp")
    if rp is not None and st.session_state.get("rp_i", 0) >= len(rp) - 1:
        st.session_state["rp_i"] = 0
    st.session_state["rp_play"] = not st.session_state.get("rp_play", False)


def replay_chart(rp, i, span):
    f = rp.frames[i]
    now = pd.Timestamp(f["time"])
    bars = rp.bars[rp.bars.index <= now]
    if span == "Today":
        bars = bars[bars.index.date == now.date()]
    elif span == "Last 3 days":
        days = sorted(set(bars.index.date))[-3:]
        bars = bars[pd.Index(bars.index.date).isin(days)]
    fig = go.Figure(go.Candlestick(x=bars.index, open=bars.open, high=bars.high, low=bars.low, close=bars.close,
                                   name="NIFTY", increasing_line_color="#0a8f5b", decreasing_line_color="#d1373f",
                                   increasing_fillcolor="#0a8f5b", decreasing_fillcolor="#d1373f", line_width=1))
    if rp.fills is not None and len(rp.fills):
        shown = rp.fills[(rp.fills.frame <= i) & (pd.to_datetime(rp.fills.time) >= bars.index.min() - pd.Timedelta(minutes=5))]
        shown = shown.assign(line=[f"{sd} {q} {c} @ {px:.2f}" for sd, q, c, px in
                                   zip(shown.side, shown.qty, shown.contract, shown.price)],
                             entry=shown.reason.str.contains("Entry"))
        for is_entry, g in shown.groupby("entry"):
            pts = g.groupby("frame").agg(text=("line", "<br>".join), reason=("reason", "first"))
            xs = [pd.Timestamp(rp.frames[j]["time"]) for j in pts.index]
            ys = [rp.frames[j]["spot"] for j in pts.index]
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="markers", name="Entries" if is_entry else "Exits",
                                     marker=dict(size=13, symbol="triangle-up" if is_entry else "x",
                                                 color="#2f6fed" if is_entry else "#b7791f",
                                                 line=dict(width=1.5, color="white")),
                                     text=[f"<b>{r}</b><br>{t}" for r, t in zip(pts.reason, pts.text)],
                                     hovertemplate="%{text}<extra></extra>"))
    st_ = f.get("status") or {}
    for name, level in (st_.get("levels") or {}).items():
        fig.add_hline(y=level, line=dict(color="#6b7280", width=1, dash="dot"),
                      annotation_text=f"{name} {level:,.0f}", annotation_position="top left",
                      annotation_font=dict(size=11, color="#6b7280"))
    fig.add_vline(x=now, line=dict(color="#6b7280", width=1))
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10), showlegend=False,
                      xaxis=dict(rangeslider=dict(visible=False),
                                 rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.6, 9.2], pattern="hour")]),
                      yaxis=dict(tickformat=",", side="right"), hovermode="x unified")
    return fig


def pnl_chart(rp, i):
    rows = [(pd.Timestamp(fr["time"]), (fr.get("status") or {}).get("pnl")) for fr in rp.frames[: i + 1]]
    df = pd.DataFrame(rows, columns=["time", "pnl"])
    acct = [fr["equity"] - rp.start_equity for fr in rp.frames[: i + 1]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df.time, y=acct, mode="lines", name="Account P&L since replay start",
                             line=dict(color="#9ca3af", width=1.5)))
    fig.add_trace(go.Scatter(x=df.time, y=df.pnl, mode="lines", name="Open cycle P&L",
                             line=dict(color="#2f6fed", width=2), connectgaps=False))
    st_ = rp.frames[i].get("status") or {}
    if st_.get("target") is not None:
        fig.add_hline(y=st_["target"], line=dict(color="#0a8f5b", dash="dash", width=1),
                      annotation_text=f"Target {rupees(st_['target'])}", annotation_position="top left")
    if st_.get("stop") is not None:
        fig.add_hline(y=st_["stop"], line=dict(color="#d1373f", dash="dash", width=1),
                      annotation_text=f"Stop {rupees(st_['stop'])}", annotation_position="bottom left")
    fig.update_layout(height=240, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
                      legend=dict(orientation="h", y=1.12, x=0),
                      xaxis=dict(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.6, 9.2], pattern="hour")]),
                      yaxis=dict(tickprefix="₹", tickformat=",", side="right"))
    return fig


def replay_view():
    rp = st.session_state.get("rp")
    if rp is None:
        return
    n = len(rp)
    playing = st.session_state.get("rp_play", False)
    if playing:
        step = SPEEDS[st.session_state.get("rp_speed", "3 bars / second")][0]
        if st.session_state.get("rp_i", 0) >= n - 1:
            st.session_state["rp_play"] = False
            st.rerun(scope="app")
        _rp_step(step)
        # stop on a trade so it can be looked at
        rng = range(st.session_state["rp_i"] - step + 1, st.session_state["rp_i"] + 1)
        hit = next((j for j in rng if 0 <= j < n and rp.frames[j]["fills"]), None)
        if hit is not None and st.session_state.get("rp_pause_trades", True):
            st.session_state["rp_i"] = hit
            st.session_state["rp_play"] = False
            st.session_state["rp_msg"] = "Paused at a trade. Press Play to carry on."
            st.rerun(scope="app")
    i = st.session_state.get("rp_i", 0)

    c = st.columns([1, 1, 1, 1, 1, 1, 3])
    c[0].button("⏮", help="Back to the first bar", on_click=_rp_set, args=(0,), key="rp_b0", **WIDE)
    c[1].button("◀ trade", help="Previous trade", on_click=_rp_prev_trade, key="rp_bpt", **WIDE)
    c[2].button("◀ 1", help="One bar back", on_click=_rp_step, args=(-1,), key="rp_bm1", **WIDE)
    c[3].button("1 ▶", help="One bar forward", on_click=_rp_step, args=(1,), key="rp_bp1", **WIDE)
    c[4].button("5 ▶▶", help="Five bars forward", on_click=_rp_step, args=(5,), key="rp_bp5", **WIDE)
    c[5].button("trade ⏭", help="Jump to the next entry or exit", on_click=_rp_next_trade, key="rp_bnt", **WIDE)
    c[6].slider("Bar", 0, n - 1, key="rp_i", label_visibility="collapsed")
    if st.session_state.get("rp_msg"):
        st.caption(st.session_state.pop("rp_msg"))

    f = rp.frames[i]
    now = pd.Timestamp(f["time"])
    day_bars = rp.bars[(rp.bars.index.date == now.date()) & (rp.bars.index <= now)]
    day_open = float(day_bars.open.iloc[0]) if len(day_bars) else f["spot"]
    status = f.get("status") or {}
    m = st.columns(6)
    m[0].metric("Time (IST)", now.strftime("%d %b %H:%M"))
    m[0].caption(f"{now:%A} · bar {i + 1} of {n}")
    m[1].metric("NIFTY", f"{f['spot']:,.2f}", **move(f["spot"] - day_open, lambda v: f"{v:,.2f} today"))
    m[2].metric("India VIX", f"{f['vix']:.2f}" if f["vix"] == f["vix"] else "-")
    if status.get("pnl") is not None:
        bits = [f"target {rupees(status['target'])}" if status.get("target") is not None else "",
                f"stop {rupees(status['stop'])}" if status.get("stop") is not None else ""]
        m[3].metric("Open cycle P&L", rupees(status["pnl"]))
        if any(bits):
            m[3].caption(" · ".join(b for b in bits if b))
    else:
        m[3].metric("Open cycle P&L", "Flat")
    m[4].metric("Account P&L (replay)", rupees(f["equity"] - rp.start_equity))
    m[5].metric("Booked since start", rupees(f["booked"] - rp.frames[0]["booked"]))
    if status.get("note"):
        st.info(status["note"])
    if f["logs"]:
        st.success("**This bar:**  \n" + "  \n".join(f["logs"]))

    span = st.session_state.get("rp_span", "Last 3 days")
    chart(replay_chart(rp, i, span), **WIDE, key="rp_chart")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Open legs**")
        if f["positions"]:
            st.dataframe(pd.DataFrame(f["positions"]).drop(columns=["Strike", "Type"]), **WIDE, hide_index=True)
        else:
            st.caption("No open position.")
        chart(pnl_chart(rp, i), **WIDE, key="rp_pnl")
    with right:
        st.markdown("**What the strategy did**")
        recent = [(fr["time"], line) for fr in rp.frames[: i + 1] for line in fr["logs"]][-25:][::-1]
        st.code("\n".join(f"{pd.Timestamp(t):%d %b %H:%M}  {line}" for t, line in recent) or "Nothing yet",
                language=None, wrap_lines=True)


with tab_rp:
    st.caption("Step through a backtest one bar at a time and watch each decision as it happens. "
               "Same engine as the Backtest tab; a warm-up before the window carries in any open position.")
    datasets = real_datasets()
    if not datasets:
        st.info("No price data yet. Get real NIFTY and option prices in the Data tab.")
    else:
        _loaded = st.session_state.get("rp")
        setup = st.expander("Replay settings" + (" (change and load again)" if _loaded is not None else ""),
                            expanded=_loaded is None)
        with setup:
            name, params = strategy_form("rp")
            c1, c2, c3, c4 = st.columns(4)
            dataset = c1.selectbox("Price data", datasets, key="rp_dataset",
                                   help="The index's 1-minute set replays minute by minute.")
            first, last, lo, hi = real_span(dataset)
            r_from = c2.date_input("From", max(lo, hi - timedelta(days=6)), min_value=first, max_value=last,
                                   key=f"rp_from_{dataset}")
            r_to = c3.date_input("To", hi, min_value=first, max_value=last, key=f"rp_to_{dataset}")
            warm = c4.number_input("Warm-up days", value=10, min_value=0, max_value=60, key="rp_warm",
                                   help="Run this many days before the window first, so a position already open is shown.")
            if dataset.startswith("SAMPLE"):
                st.warning("Synthetic sample data: fine for learning the controls, says nothing about the real market.")
            real_note(r_from, r_to)
            if st.button("Load replay", type="primary"):
                from algolab.replay import record
                try:
                    with st.spinner("Running the backtest for this window..."):
                        st.session_state["rp"] = record(load_config(), STRATS[name](**params), dataset, r_from, r_to, int(warm))
                    st.session_state["rp_i"] = 0
                    st.session_state["rp_play"] = False
                except ValueError as e:
                    st.error(str(e))
        rp = st.session_state.get("rp")
        if rp is not None:
            st.divider()
            trades = 0 if rp.fills is None or not len(rp.fills) else rp.fills.frame.nunique()
            st.markdown(f"**{STRATS[rp.strategy].title if rp.strategy in STRATS else rp.strategy}** on {rp.dataset}: "
                        f"{len(rp):,} bars, {trades} moments with trades"
                        + (" · real option prices only" if getattr(rp, "price_stats", None) is not None and REAL_ONLY else ""))
            if (getattr(rp, "price_stats", None) or {}).get("missing"):
                st.warning(f"{rp.price_stats['missing']} price look-ups found no real trade nearby and were modelled.")
            p1, p2, p3, p4 = st.columns([1, 2, 2, 2])
            p1.button("⏸ Pause" if st.session_state.get("rp_play") else "▶ Play", on_click=_rp_toggle_play,
                      type="primary", key="rp_playbtn", **WIDE)
            p2.selectbox("Speed", list(SPEEDS), index=1, key="rp_speed", label_visibility="collapsed")
            p3.selectbox("Chart", ["Today", "Last 3 days", "Whole window"], index=1, key="rp_span",
                         label_visibility="collapsed")
            p4.checkbox("Pause at each trade", value=True, key="rp_pause_trades")
            every = SPEEDS[st.session_state.get("rp_speed", "3 bars / second")][1] if st.session_state.get("rp_play") else None
            st.fragment(run_every=every)(replay_view)()

# ---------------------------------------------------------------- paper
with tab_paper:
    hosted = paper.service_alive()          # true on a server running the paper-trading service
    ids = paper.sessions()
    st.caption("Each strategy runs in its own paper session with its own account; several can run side by side. "
               "On your server, start them from the portfolio dashboard's Paper trading page instead.")
    st.markdown("**Start a strategy**")
    name, params = strategy_form("pp")
    feed = st.radio("Market feed", ["sim", "kite"], horizontal=True,
                    format_func=lambda f: {"sim": "Simulated market (fast, no broker needed)",
                                           "kite": "Zerodha live quotes (market hours)"}[f])
    state = paper.read_state(name) if name in ids else None
    resume = bool(state and state.get("feed") == feed)
    running_now = paper.is_running(name) or (hosted and paper.read_desired(name))
    c1, c2, _ = st.columns([1, 1, 4])
    if running_now:
        c1.button("Running", disabled=True)
    elif c1.button("Resume session" if resume else "Start session", type="primary"):
        ok = True
        if feed == "kite":
            from algolab import kite
            try:
                kite.load_secrets()
                if not hosted and kite.session_info() is None:
                    ok = False
                    st.error("No Zerodha login on this computer. Live paper trading runs on your server instead.")
            except kite.KiteError as e:
                ok = False
                st.error(str(e))
        if ok:
            if hosted:
                paper.set_desired(name, params, feed)
            else:
                paper.RUNTIME.mkdir(exist_ok=True)
                log = open(paper.RUNTIME / "paper_process.log", "a")
                subprocess.Popen([sys.executable, str(ROOT / "run.py"), "paper", "--strategy", name, "--feed", feed,
                                  "--params", json.dumps(params)], cwd=ROOT, stdout=log, stderr=log)
            time.sleep(3)
            st.rerun()
    if state and not running_now:
        if not st.session_state.get(f"pp_reset_ask_{name}"):
            if c2.button("Reset this strategy's account"):
                st.session_state[f"pp_reset_ask_{name}"] = True
                st.rerun()
        else:
            c2.warning("This erases the saved paper account: positions, trades and P&L.")
            y, n = c2.columns(2)
            if y.button("Yes, erase it", type="primary", key=f"pp_reset_yes_{name}"):
                st.session_state.pop(f"pp_reset_ask_{name}", None)
                paper.reset(name)
                st.rerun()
            if n.button("Cancel", key=f"pp_reset_no_{name}"):
                st.session_state.pop(f"pp_reset_ask_{name}", None)
                st.rerun()
    if state:
        st.caption("This strategy has a saved paper account. Resume continues it; another feed starts a new one.")

    @st.fragment(run_every="3s")
    def live_view():
        ids_now = paper.sessions()
        if not ids_now:
            st.info("No paper session yet.")
            return
        totals = []
        for sid in ids_now:
            s = paper.read_state(sid)
            if s:
                totals.append((sid, s["equity"] - s["broker"]["capital"]))
        if len(totals) > 1:
            cols = st.columns(len(totals) + 1)
            cols[0].metric("All strategies", rupees(sum(t for _, t in totals)))
            for col, (sid, t) in zip(cols[1:], totals):
                col.metric(STRATS[sid].title.split(" (")[0] if sid in STRATS else sid, rupees(t))
        sid = st.radio("Session", ids_now, horizontal=True, key="pp_view",
                       format_func=lambda i: (STRATS[i].title.split(" (")[0] if i in STRATS else i)
                       + (" (running)" if paper.is_running(i) else ""))
        s = paper.read_state(sid)
        live = paper.is_running(sid)
        if live or paper.read_desired(sid):
            if st.button("Stop this session", key=f"stop_{sid}"):
                paper.request_stop(sid)
                time.sleep(2.5)
                st.rerun(scope="app")
            why = paper.session_status(sid)
            if not live and why:
                st.warning(f"Waiting to start: {why}")
        if not s:
            st.caption("No updates yet.")
            return
        capital = s["broker"]["capital"]
        c = st.columns(6)
        c[0].metric("Market time (IST)", pd.Timestamp(s["time"]).strftime("%d %b %H:%M"))
        c[1].metric("NIFTY", f"{s['spot']:,.2f}")
        c[2].metric("India VIX", f"{s['vix']:.2f}" if s.get("vix") else "-")
        c[3].metric("Account value", rupees(s["equity"]), **move(s["equity"] - capital))
        c[4].metric("Booked P&L (after charges)", rupees(s["net_realized"]))
        c[5].metric("Open P&L", rupees(s["unrealized"]))
        st.markdown("**Open positions**")
        if s["positions"]:
            st.dataframe(pd.DataFrame(s["positions"]), **WIDE, hide_index=True)
        else:
            st.caption("None")
        eqf = paper.equity_file(sid)
        if eqf and eqf.exists():
            eq = pd.read_csv(eqf, parse_dates=["time"])
            if len(eq) > 1:
                chart(line_chart(eq.time, eq.equity, "Account value"), **WIDE, key=f"paper_equity_{sid}")
        trades = pd.DataFrame(s["broker"]["trades"])
        a, b = st.tabs([f"Closed trades ({len(trades)})", "Log"])
        if len(trades):
            with a:
                nice_table(trades.iloc[::-1])
        b.code("\n".join(s["logs"][-60:][::-1]) or "No activity yet", language=None, wrap_lines=True)

    st.divider()
    live_view()

# ---------------------------------------------------------------- data
@st.cache_data(ttl=600, show_spinner=False)
def _di_contracts(u, stamp):
    from algolab import datainfo
    return datainfo.contracts(u)


@st.cache_data(ttl=600, show_spinner=False)
def _di_index_days(path, stamp):
    from algolab import datainfo
    return datainfo.index_days(path)


@st.cache_data(ttl=600, show_spinner=False)
def _di_sizes(stamp):
    from algolab import datainfo
    return datainfo.folder_sizes()


def _stamp(p):
    """Changes when files are added or updated under p (cache key)."""
    try:
        return int(max([p.stat().st_mtime] + [c.stat().st_mtime for c in p.iterdir()]))
    except (OSError, ValueError):
        return 0


def _gb(b):
    return f"{b / 1e9:.2f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def data_overview():
    from algolab import datainfo
    from algolab.config import DATA
    unds = datainfo.underlyings()
    if not unds:
        st.info("No market data stored yet. Use the sections under *Get more data* below.")
        return
    names = {"NIFTY": "NIFTY", "BANKNIFTY": "Bank Nifty", "SENSEX": "Sensex"}
    u = unds[0] if len(unds) == 1 else st.radio("Underlying", unds, horizontal=True, format_func=lambda x: names.get(x, x),
                                                 key="di_u", label_visibility="collapsed")
    with st.spinner("Reading the stored data (first time only)..."):
        cons = _di_contracts(u, _stamp(DATA / "options" / u))
        exps = datainfo.expiries(cons)
        files = datainfo.index_files(u)
        days = _di_index_days(files[0], int(files[0].stat().st_mtime)) if files else None
        cov = datainfo.coverage(cons, days)

    c1, c2, c3, c4 = st.columns(4)
    if days is not None and len(days):
        c1.metric("Index days", f"{len(days):,}", help=f"{files[0].stem}: {int(days.bars.sum()):,} bars")
        c1.caption(f"{days.day.iloc[0]} → {days.day.iloc[-1]}")
    else:
        c1.metric("Index days", "none")
    c2.metric("Option contracts", f"{len(cons):,}")
    c2.caption(f"{len(exps)} expiries" + (f", {exps.expiry.iloc[0]} → {exps.expiry.iloc[-1]}" if len(exps) else ""))
    c3.metric("Option prices", f"{int(cons.rows.sum()) / 1e6:.1f} M" if len(cons) else "0")
    c3.caption("1-minute candles and recorded prices")
    full = int(days.bars.max()) if days is not None and len(days) else 0
    short = int((days.bars < 0.9 * full).sum()) if full else 0
    c4.metric("Short index days", f"{short}", help="Days with fewer bars than a full day: half sessions (Muhurat, "
              "Budget Saturday), the closing auction after 3 Aug 2026, or gaps. The data review tells them apart.")
    c4.caption(f"a full day has {full} bars" if full else "")

    # what is stored, day by day
    if len(cov):
        weeks, z, text = datainfo.calendar_grid(cov)
        top = max(1.0, float(cov.contracts.max()))
        fig = go.Figure(go.Heatmap(
            x=weeks, y=["Mon", "Tue", "Wed", "Thu", "Fri"], z=z, text=text, hoverinfo="text", xgap=2, ygap=2,
            zmin=0, zmax=top, colorscale=[[0, "#2f3542" if _theme() == "dark" else "#d7dbe2"], [0.002, "#c6dbfb"], [0.35, "#8db6f6"], [0.7, "#4f8bef"], [1, "#1e4fc2"]],
            colorbar=dict(title="Contracts", thickness=10)))
        fig.update_layout(title="What is stored, day by day (grey: index only · blue: option prices, darker = more contracts)",
                          height=230, margin=dict(l=10, r=10, t=40, b=10), yaxis=dict(autorange="reversed"),
                          xaxis=dict(type="date", tickformat="%b %y"), plot_bgcolor="rgba(0,0,0,0)")
        chart(fig, **WIDE)

    # index and VIX
    if files:
        r1, r2 = st.columns([3, 1])
        span = r2.selectbox("Range", ["1 day", "5 days", "1 month", "6 months", "1 year", "All"], index=4, key="di_range")
        ds = r1.selectbox("Index data set", [f.stem for f in files], key=f"di_ds_{u}", label_visibility="collapsed") if len(files) > 1 else files[0].stem
        path = next(f for f in files if f.stem == ds)
        nd = {"1 day": 1, "5 days": 5, "1 month": 22, "6 months": 130, "1 year": 260, "All": 100000}[span]
        if nd <= 5:
            bars = datainfo.index_bars(path, nd)
            x, y, v = bars["datetime"], bars["close"], bars.get("vix")
        else:
            dd = _di_index_days(path, int(path.stat().st_mtime)).tail(nd)
            x, y, v = dd["day"], dd["close"], dd.get("vix")
        fig = go.Figure(go.Scatter(x=x, y=y, mode="lines", name=names.get(u, u), line=dict(color="#2563eb", width=1.6),
                                   fill="tozeroy", fillcolor="rgba(37,99,235,0.06)"))
        if v is not None and v.notna().any():
            fig.add_trace(go.Scatter(x=x, y=v, mode="lines", name="India VIX", yaxis="y2", line=dict(color="#b7791f", width=1.2)))
        lo, hi = float(y.min()), float(y.max())
        fig.update_layout(title=f"{names.get(u, u)}" + (" and India VIX" if v is not None and v.notna().any() else ""), height=320,
                          margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h", y=1.12),
                          yaxis=dict(range=[lo - (hi - lo) * 0.05, hi + (hi - lo) * 0.05], tickformat=","),
                          yaxis2=dict(overlaying="y", side="right", showgrid=False),
                          xaxis=dict(rangebreaks=[dict(bounds=["sat", "mon"])] + ([dict(bounds=[15.6, 9.2], pattern="hour")] if nd <= 5 else [])))
        chart(fig, **WIDE)

    # expiries and the chain
    if len(exps):
        e = exps.copy()
        e["start"], e["end"] = pd.to_datetime(e["from"].str[:10]), pd.to_datetime(e["to"].str[:10]) + pd.Timedelta(hours=18)
        fig = go.Figure(go.Bar(y=e.expiry, x=(e.end - e.start).dt.total_seconds() * 1000, base=e.start, orientation="h",
                               marker=dict(color=e.contracts, colorscale=[[0, "#c6dbfb"], [1, "#1e4fc2"]]),
                               customdata=e[["contracts", "calls", "puts", "low", "high", "prices"]],
                               hovertemplate="expiry %{y}<br>%{customdata[0]} contracts (%{customdata[1]} calls, %{customdata[2]} puts)"
                                             "<br>strikes %{customdata[3]:,}–%{customdata[4]:,}<br>%{customdata[5]:,} prices<extra></extra>"))
        fig.update_layout(title=f"Expiries: each bar runs from the first stored price to the last ({len(e)})",
                          height=min(900, 120 + 14 * len(e)), margin=dict(l=10, r=10, t=40, b=10),
                          xaxis=dict(type="date"), yaxis=dict(type="category", autorange="reversed", tickfont=dict(size=10)))
        chart(fig, **WIDE)

        today = date.today().isoformat()
        upcoming = [x for x in exps.expiry if x >= today]
        pick = st.selectbox("Option chain of expiry", list(exps.expiry)[::-1],
                            index=(list(exps.expiry)[::-1].index(upcoming[0]) if upcoming else 0), key=f"di_exp_{u}")
        ch = cons[cons.expiry == pick].pivot_table(index="strike", columns="right", values="last", aggfunc="last")
        last_day = cons[cons.expiry == pick]["to"].max()[:10]
        spot = None
        if days is not None and len(days):
            hit = days[days.day <= last_day]
            spot = float(hit.close.iloc[-1]) if len(hit) else None
        fig = go.Figure()
        for r, col, nm in (("CE", "#16a34a", "Calls"), ("PE", "#dc2626", "Puts")):
            if r in ch:
                s = ch[r].dropna()
                fig.add_trace(go.Scatter(x=s.index, y=s.values, mode="lines+markers", name=nm, line=dict(color=col, width=1.6),
                                         marker=dict(size=4)))
        if spot:
            fig.add_vline(x=spot, line_dash="dot", line_color="#6b7280", annotation_text=f"index {spot:,.0f}")
        fig.update_layout(title=f"Last stored price by strike, expiry {pick} (prices up to {last_day})", height=340,
                          margin=dict(l=10, r=10, t=40, b=10), yaxis=dict(type="log", title="price", dtick=1, tickprefix="₹"), xaxis=dict(title="strike", tickformat=","),
                          legend=dict(orientation="h", y=1.12))
        chart(fig, **WIDE)

        k1, k2 = st.columns([1, 3])
        strikes = sorted(cons[cons.expiry == pick].strike.unique())
        near = min(strikes, key=lambda k: abs(k - spot)) if spot else strikes[len(strikes) // 2]
        k = k1.selectbox("Contract", strikes, index=strikes.index(near), key=f"di_k_{u}_{pick}", format_func=lambda v: f"{v:,}")
        rt = k1.radio("Type", ["CE", "PE"], horizontal=True, key=f"di_r_{u}_{pick}")
        cf = DATA / "options" / u / pick / f"{k}{rt}.csv"
        if cf.exists():
            px = pd.read_csv(cf)
            fig = go.Figure(go.Scatter(x=px["time"], y=px["last"], mode="lines", name="last", line=dict(color="#2563eb", width=1.4)))
            fig.update_layout(title=f"{u} {pick} {k:,} {rt}: {len(px):,} prices", height=300, margin=dict(l=10, r=10, t=40, b=10),
                              xaxis=dict(rangebreaks=[dict(bounds=["sat", "mon"]), dict(bounds=[15.6, 9.2], pattern="hour")]))
            with k2:
                chart(fig, **WIDE)
        else:
            k2.info("No prices stored for this contract.")

    # data review, TrueData record, disk
    a, b = st.columns(2)
    rv = datainfo.latest_review()
    with a:
        st.markdown("**Latest data review**")
        if rv:
            (st.success if rv["verdict"] == "OK" else st.warning if rv["verdict"] == "WARN" else st.error)(
                f"{rv['verdict']} · {rv['fail']} fail, {rv['warn']} warn, {rv['info']} info · {rv['redownload']} to download again "
                f"· run {rv['run']}")
            if len(rv["issues"]):
                g = rv["issues"].groupby(["severity", "dataset", "check"]).size().reset_index(name="findings")
                g["o"] = g.severity.map({"FAIL": 0, "WARN": 1, "INFO": 2})
                st.dataframe(g.sort_values(["o", "findings"], ascending=[True, False]).drop(columns="o"), **WIDE, hide_index=True,
                             height=min(250, 38 + 35 * len(g)))
            st.caption(f"Full report: {rv['folder'] / 'report.md'}")
        else:
            st.caption("No review yet.")
        st.caption("Run a new one in Terminal: `.venv/bin/python -m algolab.datacheck` (or ask Claude to use the data-reviewer).")
    with b:
        st.markdown("**Disk used by the data**")
        sz = _di_sizes(_stamp(DATA) + _stamp(DATA / "options") + _stamp(DATA / "truedata"))
        if len(sz):
            sz = sz.sort_values("bytes")
            fig = go.Figure(go.Bar(x=sz.bytes / 1e9, y=sz.part, orientation="h", marker_color="#2563eb",
                                   text=[_gb(v) for v in sz.bytes], textposition="outside"))
            fig.update_layout(height=60 + 24 * len(sz), margin=dict(l=10, r=70, t=10, b=10), xaxis=dict(title="GB"), uniformtext_minsize=12)
            fig.update_traces(cliponaxis=False)
            chart(fig, **WIDE)
            st.caption(f"Total {_gb(int(sz.bytes.sum()))}")
    tp = datainfo.truedata_progress()
    if len(tp):
        with st.expander("TrueData download record (trial ends 22 Oct 2026)"):
            t = tp.pivot_table(index="what", columns="status", values="items", aggfunc="sum").fillna(0).astype(int)
            t["1-minute candles"] = tp.groupby("what")["candles"].sum().map(lambda v: f"{v / 1e6:.1f} M")
            st.dataframe(t, **WIDE)
            st.caption("done: downloaded · empty: the contract never traded · pending / error: not fetched yet "
                       "(run `python run.py truedata all --underlying …` again).")


with tab_data:
    data_overview()
    st.divider()
    st.subheader("Get more data")
    from algolab import kite
    from algolab.publish import PublishError, settings as dash_settings
    with st.expander("Index bars from Zerodha, through your server"):
        st.caption("Recommended. Your server dashboard fetches NIFTY and India VIX bars with its own Zerodha login "
                   "and sends them here, so this Mac never logs in. (Zerodha keeps one login per API key: a login "
                   "here would sign the server out, and your Kite app sends the login back to the server anyway.)")
        try:
            _dash = dash_settings()
        except PublishError:
            _dash = None
            st.info("Add the dashboard section (url, user, password) to secrets.yaml to use this. "
                    "The server's install script prints the values.")
        if _dash:
            c1, c2, c3 = st.columns(3)
            sv_from = c1.date_input("From", date(2023, 1, 1), key="sv_from")
            sv_to = c2.date_input("To", date.today(), key="sv_to")
            sv_int = c3.selectbox("Bar size", list(kite.INTERVALS), index=2, key="sv_int",
                                  help="minute = 1-minute bars, for minute-by-minute replay")
            if st.button("Download through the server", type="primary"):
                msgs = st.empty()
                try:
                    with st.spinner("The server is fetching from Zerodha (a minute or two for several years)..."):
                        path, n = kite.download_spot_via_server(sv_from, sv_to, sv_int, log=lambda m: msgs.caption(m))
                    st.success(f"Saved {n:,} bars to {path.name}")
                except (PublishError, kite.KiteError) as e:
                    st.error(str(e))
                except Exception as e:
                    st.error(f"Download failed: {e}")
    with st.expander("Option prices recorded by your server"):
        st.caption("Your server records real NIFTY and option prices every trading day. Copy them here to backtest on them.")
        srv_saved = paper.RUNTIME / "server.json"
        try:
            srv = {**cfg.get("server", {}), **json.loads(srv_saved.read_text())}
        except Exception:
            srv = dict(cfg.get("server", {}))
        s1, s2 = st.columns(2)
        host = s1.text_input("Server", srv.get("host", ""), placeholder="ubuntu@YOUR_SERVER_IP")
        keyf = s2.text_input("Key file", srv.get("key_file", ""), placeholder="~/.ssh/LightsailDefaultKey.pem")
        if not host.strip():
            st.caption("Fill in the server (user@address) and the key file to copy the server's recordings here.")
        if st.button("Get data from server", type="primary", disabled=not host.strip()):
            try:
                with st.spinner("Copying..."):
                    msg = kite.sync_from_server(host.strip(), keyf.strip(), srv.get("data_path", "/opt/algolab/data"))
                paper.RUNTIME.mkdir(exist_ok=True)
                srv_saved.write_text(json.dumps({"host": host.strip(), "key_file": keyf.strip()}))
                st.success(msg)
            except Exception as ex:
                st.error(str(ex))
    with st.expander("NSE closing prices of past options (free)"):
        st.caption("Zerodha keeps no history for expired options, but NSE publishes every option's closing price "
                   "each day. Imported here, they correct the estimated option prices in backtests for the real "
                   "skew (far puts cost more than India VIX implies). Each moment is corrected with the previous "
                   "day's closes, so there is no look-ahead. Needs the NIFTY price history above.")
        from algolab import eod as _eod
        _have = sorted(_eod.DAYS_DIR.glob("*.csv.gz")) if _eod.DAYS_DIR.exists() else []
        if _have:
            st.caption(f"Imported: {len(_have)} days, {_have[0].name[:10]} to {_have[-1].name[:10]}."
                       + (" Smile built." if _eod.SMILE_FILE.exists() else " Smile not built yet."))
        n1, n2, n3 = st.columns([1, 1, 2])
        nse_from = n1.date_input("From", date(2023, 1, 1), key="nse_from")
        nse_to = n2.date_input("To", date.today(), key="nse_to")
        b1, b2, _ = st.columns([1, 1, 2])
        go_import = b1.button("Import from NSE", type="primary",
                              help="About 1 second per trading day the first time; already imported days are skipped.")
        go_rebuild = b2.button("Rebuild correction only", disabled=not _have)
        if go_import or go_rebuild:
            msgs = st.empty()
            try:
                if go_import:
                    with st.spinner("Downloading NSE closing prices..."):
                        res = _eod.import_bhavcopy(nse_from, nse_to, log=lambda m: msgs.caption(m))
                    st.success(f"Imported {res['saved']} days" + (f"; {len(res['missing'])} days not available "
                               "(holidays or not published)" if res["missing"] else ""))
                with st.spinner("Building the correction..."):
                    table = _eod.build_smile(load_config(), log=lambda m: msgs.caption(m))
                    st.session_state["nse_report"] = _eod.validate(load_config(), table)
            except _eod.EodError as e:
                st.error(str(e))
            except Exception as e:
                st.error(f"Import failed: {e}")
        if st.session_state.get("nse_report") is not None and len(st.session_state["nse_report"]):
            st.markdown("**How close estimates are to real closes** (median error %, each day priced from the day before)")
            st.dataframe(st.session_state["nse_report"].rename(columns={
                "options": "Options", "plain_vix_error_pct": "Plain VIX: error %",
                "plain_vix_bias_pct": "Plain VIX: bias %", "with_smile_error_pct": "Corrected: error %"}), **WIDE)
            st.caption("Bias below zero means plain-VIX estimates are too cheap. Backtests use the correction "
                       "automatically; set pricing.smile: off in config.yaml to compare without it.")

    with st.expander("Zerodha login on this computer (not needed when your server collects the data)"):
        st.caption("Your portfolio server already logs in to Zerodha each morning. Logging in here with the same "
                   "API key would sign the server out, so leave this alone unless you use a separate key.")
        try:
            kite.load_secrets()
            have_keys = True
        except kite.KiteError as e:
            have_keys = False
            st.info(str(e))
        if have_keys:
            info = kite.session_info()
            shared = kite.shared_token_file()
            if shared and info:
                st.success("Using today's Zerodha login from your other app.")
            elif shared:
                st.warning(f"Waiting for today's Zerodha login in your other app (reading {shared}). "
                           "Log in there; this platform does not log in separately, because a second login "
                           "on the same API key would sign the other app out.")
            elif info:
                st.success(f"Logged in today as {info['user'] or 'your account'}.")
            else:
                st.markdown(f"**Daily login** - Zerodha needs this once each morning.\n\n"
                            f"1. [Open the Zerodha login page]({kite.login_url()}) and sign in.\n"
                            f"2. You land on a page that may fail to load. Copy its full address and paste it here.")
                st.caption("Zerodha keeps one live login per API key. If another app of yours (for example on "
                           "a server) uses the same key, logging in here signs that app out.")
                pasted = st.text_input("Redirected address or request_token")
                if st.button("Complete login") and pasted:
                    try:
                        kite.complete_login(pasted)
                        st.rerun()
                    except Exception as e:
                        st.error(f"Login failed: {e}")
            if info:
                st.markdown("**Download NIFTY and India VIX history**")
                c1, c2, c3 = st.columns(3)
                d_from = c1.date_input("From", date(2023, 1, 1), key="dl_from")
                d_to = c2.date_input("To", date.today(), key="dl_to")
                interval = c3.selectbox("Bar size", list(kite.INTERVALS), index=2)
                if st.button("Download history"):
                    try:
                        with st.spinner("Downloading..."):
                            path, n = kite.download_spot(d_from, d_to, interval)
                        st.success(f"Saved {n:,} bars to {path.name}")
                    except Exception as e:
                        st.error(f"Download failed: {e}")
                st.markdown("**Save real option prices** for the contracts listed right now (Zerodha keeps no "
                            "history once a contract expires, so run this weekly to build your own).")
                c1, c2 = st.columns(2)
                n_strikes = c1.number_input("Strikes each side of ATM", value=10, min_value=1, max_value=40)
                n_exp = c2.number_input("Nearest expiries", value=2, min_value=1, max_value=6)
                if st.button("Download option candles"):
                    try:
                        bar = st.progress(0.0)
                        n = kite.download_options(int(n_strikes), int(n_exp), cfg=cfg, progress=lambda i, t: bar.progress(i / t))
                        st.success(f"Saved candles for {n} contracts")
                    except Exception as e:
                        st.error(f"Download failed: {e}")
    with st.expander("TrueData history (trial until 22 Oct 2026)"):
        st.caption("Two years of 1-minute option, index, stock and futures bars, downloaded with your TrueData trial login "
                   "(secrets.yaml). Run these in Terminal, in this folder; they carry on where they stopped.")
        st.code(".venv/bin/python run.py truedata status                         # what is done\n"
                ".venv/bin/python run.py truedata all --underlying NIFTY         # or BANKNIFTY, SENSEX\n"
                ".venv/bin/python run.py truedata stocks                         # NIFTY 50 stocks and futures\n"
                ".venv/bin/python -m algolab.datacheck                           # review what was downloaded", language="bash")
    with st.expander("Index data sets and sample data"):
        rows = []
        for d in list_datasets():
            s = pd.read_csv(SPOT_DIR / f"{d}.csv", usecols=["datetime"], parse_dates=["datetime"])["datetime"]
            rows.append({"Dataset": d, "From": s.min().date(), "To": s.max().date(), "Bars": len(s),
                         "Kind": "Synthetic sample" if d.startswith("SAMPLE") else "Real (TrueData)" if d.startswith("TD_") else "Real (Zerodha)"})
        st.dataframe(pd.DataFrame(rows), **WIDE, hide_index=True)
        if st.button("Create / refresh sample data"):
            write_sample()
            st.rerun()


# ---------------------------------------------------------------- help
with tab_help:
    st.markdown((ROOT / "README.md").read_text(encoding="utf-8") if (ROOT / "README.md").exists() else "See README.md")
