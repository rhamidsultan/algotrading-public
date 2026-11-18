"""Changes to the portfolio dashboard's main page (templates/dashboard.html).

1. The Current value / Invested / Total P&L / Day P&L cards become two lines: the labelled figures,
   and one bar in which invested plus P&L make up the current value. (Day P&L stays in the range
   card and the "Today" chart below.)
2. The "Today" day-P&L chart starts at the market open instead of at the pre-open zero.

A page that has the earlier version (value-over-time chart, dashboard_v1.py) is moved to this one.
Each change looks for exact pieces of the page; if any is missing nothing is written.
Usage: python3 dashboard_changes.py path/to/dashboard.html [more files...]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dashboard_v1 import CHANGES as V1, MARK as MARK1  # noqa: E402

# pieces both versions share: no day card, and the Today chart starting at the open
COMMON = [V1[2], V1[5], V1[6]]
V1_ONLY = [V1[0], V1[1], V1[3], V1[4]]

MARK2 = 'id="valueBar"'
V2_ONLY = [
    ("@media (max-width:760px){.cards{",
     ".vrow{display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 20px;font-size:13px;color:var(--muted);font-variant-numeric:tabular-nums}\n"
     ".vrow b{font-size:18px;color:var(--text);font-weight:650;margin-left:5px}\n"
     ".vrow b.up{color:var(--up)}.vrow b.down{color:var(--down)}\n"
     ".vrow small{margin-left:4px}.vrow .tot{margin-left:auto}\n"
     ".vrow .sw{width:10px;height:10px;border-radius:2px;margin-right:5px;display:inline-block}\n"
     ".vbar{display:flex;height:14px;border-radius:7px;overflow:hidden;background:var(--line);margin-top:10px}\n"
     ".vbar i{height:100%;transition:width .4s}\n"
     "#invBar{background:var(--accent)}#pnlBar.up{background:var(--up)}\n"
     "#pnlBar.down{background:repeating-linear-gradient(45deg,var(--down) 0 4px,color-mix(in srgb,var(--down) 45%,transparent) 4px 8px)}\n"
     "@media (max-width:760px){.vrow{gap:2px 14px}.vrow b{font-size:15px;margin-left:4px}.vrow .tot{margin-left:0}}\n"
     "@media (max-width:760px){.cards{"),
    ('''  <div class="cards">
    <div class="card"><div class="label">Current value</div><div class="big" id="value">–</div><div class="sub" id="count"></div></div>
    <div class="card"><div class="label">Invested</div><div class="big" id="invested">–</div></div>
    <div class="card"><div class="label">Total P&amp;L</div><div class="big" id="pnl">–</div><div class="sub" id="pnlPct"></div></div>
    <div class="card"><div class="label">Day P&amp;L</div><div class="big" id="day">–</div><div class="sub" id="dayPct"></div></div>
  </div>''',
     '''  <div class="card" id="valueCard">
    <div class="vrow">
      <span><span class="sw" style="background:var(--accent)"></span>Invested<b id="invested">–</b></span>
      <span><span class="sw" id="pnlSw" style="background:var(--up)"></span>P&amp;L<b id="pnl">–</b><small id="pnlPct"></small></span>
      <span class="tot">Current value<b id="value">–</b><small id="count" hidden></small></span>
    </div>
    <div class="vbar" id="valueBar"><i id="invBar" style="width:0"></i><i id="pnlBar" style="width:0"></i></div>
  </div>'''),
    ('''  drawChart(d);
}

// ---- manual pause''', '''  drawChart(d);
  drawValueBar(d);
}

// ---- manual pause'''),
    ('''async function loadHist() {''', '''function drawValueBar(d) {
  // One bar for the current value: the invested part plus the P&L part. With a loss the bar spans
  // what was invested, and the hatched red part is the amount lost.
  const s = d.summary || {};
  if (s.value === undefined) return;
  const gain = s.pnl >= 0, whole = Math.max(s.value, s.invested) || 1;
  $('invBar').style.width = ((gain ? s.invested : s.value) / whole * 100).toFixed(2) + '%';
  $('pnlBar').style.width = (Math.abs(s.pnl) / whole * 100).toFixed(2) + '%';
  $('pnlBar').className = gain ? 'up' : 'down';
  $('pnlSw').style.background = gain ? 'var(--up)' : 'var(--down)';
  $('valueBar').title = gain ? `Invested ${inr(s.invested)} + profit ${inr(s.pnl)} = ${inr(s.value)}`
                             : `Current value ${inr(s.value)} + loss ${inr(-s.pnl)} = invested ${inr(s.invested)}`;
}

async function loadHist() {'''),
]


def _swap(text, pieces, reverse=False):
    for old, new in pieces:
        a, b = (new, old) if reverse else (old, new)
        if text.count(a) != 1:
            raise ValueError(f"expected piece not found: {a[:60]!r}")
        text = text.replace(a, b, 1)
    return text


def apply(text: str) -> str:
    if MARK2 in text:
        return text
    if MARK1 in text:                      # earlier version: take its chart out first
        text = _swap(text, list(reversed(V1_ONLY)), reverse=True)
        return _swap(text, V2_ONLY)
    return _swap(text, COMMON + V2_ONLY)


if __name__ == "__main__":
    results = {}
    try:
        for name in sys.argv[1:]:
            results[name] = apply(Path(name).read_text(encoding="utf-8"))
    except ValueError as e:
        sys.exit(f"The dashboard page is not the version this was written for ({e}); nothing was changed.")
    for name, text in results.items():
        with open(name, "w", encoding="utf-8") as f:   # in place: keeps owner and permissions
            f.write(text)
    print("ok")
