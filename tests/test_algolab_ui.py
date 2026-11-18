"""Algo Lab (Mac dashboard) after the UI review: it still loads, uses readable reds, and shows changes without signs."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_dashboard_loads_without_errors():
    testing = pytest.importorskip("streamlit.testing.v1")
    at = testing.AppTest.from_file(str(ROOT / "dashboard.py"), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.tabs) == 6


def test_theme_reds_keep_white_text_readable():
    cfg = (ROOT / ".streamlit" / "config.toml").read_text()
    assert '[theme.light]\nprimaryColor = "#c81e1e"' in cfg and '[theme.dark]\nprimaryColor = "#d93636"' in cfg


def test_changes_shown_without_signs_and_reset_needs_confirming():
    src = (ROOT / "dashboard.py").read_text()
    assert ':+,.2f} today"' not in src                       # NIFTY's day change goes through move()
    assert '**move(s["equity"] - capital)' in src
    assert '"Yes, erase it"' in src
    assert src.count("st.plotly_chart(") == 1                # every chart goes through chart() for readable text


def test_results_show_headline_numbers_and_readable_tables():
    src = (ROOT / "dashboard.py").read_text()
    assert src.count("headline(res.metrics)") == 2 and 'st.expander("All figures")' in src
    assert "nice_table(res.cycles)" in src and "nice_table(trades.iloc[::-1])" in src
    assert "format_func=sweep_name" in src


def test_sweep_name_is_readable():
    import ast
    src = (ROOT / "dashboard.py").read_text()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "sweep_name")
    import pandas as pd
    ns = {"pd": pd, "STRATS": {"short_straddle": type("S", (), {"title": "Positional short ATM straddle"})}}
    exec(compile(ast.Module([fn], []), "x", "exec"), ns)
    assert ns["sweep_name"]("20261016-122727_sweep_short_straddle") == "16 Oct 12:27 · Positional short ATM straddle"
