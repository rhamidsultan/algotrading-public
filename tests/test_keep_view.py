"""Pages that redraw themselves keep what is being read: Summary's Details stay open, the Paper/Live Activity log keeps
its scroll position."""
from pathlib import Path

PANEL = (Path(__file__).resolve().parents[1] / "deploy" / "portfolio_page" / "algolab_panel.py").read_text()


def test_summary_refresh_keeps_details_open_and_scroll():
    summary = PANEL.split("SUMMARY_PAGE", 1)[1]
    assert "function keepView(box, draw)" in summary
    assert 'keepView($("grid"), () => { $("grid").innerHTML = html' in summary          # the 15 s refresh goes through it
    body = summary.split("function keepView(box, draw)", 1)[1].split("\n}\n", 1)[0]
    assert "d.open = opened.get(k)" in body and "e.scrollTop = s[0]" in body


def test_paper_refresh_keeps_the_activity_log_scroll():
    detail = PANEL.split("function renderDetail()", 1)[1].split("\n}\n", 1)[0]
    assert 'querySelectorAll(".log, .scroll")' in detail and "sameSession" in detail
    # restored after the sections' open state is set, so a table inside a folded section can be scrolled back too
    assert detail.index("e.scrollTop = s[0]") > detail.index("d.open = !!open[id + d.dataset.k]")
