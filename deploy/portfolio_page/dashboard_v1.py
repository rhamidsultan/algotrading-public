"""Earlier version of the dashboard changes (value-over-time chart). Kept so a page that has it can be moved to the current version; see dashboard_changes.py.

1. The Current value / Invested / Total P&L / Day P&L cards become one chart of value, invested
   and total P&L over time, with today's figures above it. (Day P&L stays in the range card and
   the "Today" chart below.)
2. The "Today" day-P&L chart starts at the market open instead of at the pre-open zero.

Each change looks for exact pieces of the page; if any is missing nothing is written.
Usage: python3 dashboard_changes.py path/to/dashboard.html [more files...]
"""
import sys
from pathlib import Path

MARK = 'id="growthChart"'

CHANGES = [
    # ---- styles for the new block
    ("@media (max-width:760px){.cards{",
     ".kv{display:flex;gap:6px 22px;flex-wrap:wrap;align-items:baseline;margin:2px 0 10px;font-variant-numeric:tabular-nums}\n"
     ".kv div{font-size:13px;color:var(--muted)}\n"
     ".kv b{display:block;font-size:20px;color:var(--text);font-weight:650}\n"
     ".kv b.up{color:var(--up)}.kv b.down{color:var(--down)}\n"
     ".kv small{font-size:13px;margin-left:4px}\n"
     ".kv .sw{width:10px;height:3px;border-radius:2px;vertical-align:middle;margin-right:6px}\n"
     ".kv .sw.dash{background:repeating-linear-gradient(90deg,var(--muted) 0 4px,transparent 4px 7px)!important}\n"
     "@media (max-width:760px){.kv b{font-size:17px}}\n"
     "@media (max-width:760px){.cards{"),
    # ---- the four cards -> one chart
    ('''  <div class="cards">
    <div class="card"><div class="label">Current value</div><div class="big" id="value">–</div><div class="sub" id="count"></div></div>
    <div class="card"><div class="label">Invested</div><div class="big" id="invested">–</div></div>
    <div class="card"><div class="label">Total P&amp;L</div><div class="big" id="pnl">–</div><div class="sub" id="pnlPct"></div></div>
    <div class="card"><div class="label">Day P&amp;L</div><div class="big" id="day">–</div><div class="sub" id="dayPct"></div></div>
  </div>''',
     '''  <div class="card" id="growthCard">
    <div class="kv">
      <div><span class="sw" style="background:var(--accent)"></span>Current value<b id="value">–</b></div>
      <div><span class="sw dash"></span>Invested<b id="invested">–</b></div>
      <div><span class="sw" id="pnlSw" style="background:var(--up)"></span>Total P&amp;L<b id="pnl">–</b><small id="pnlPct"></small></div>
      <div style="margin-left:auto;align-self:center" id="count"></div>
    </div>
    <div class="chartbox"><canvas id="growthChart"></canvas></div>
    <div class="note">Value and invested on the left scale, total P&amp;L on the right. One point per trading day at the close; the last point is now.</div>
  </div>'''),
    # ---- render(): no day card any more; the P&L figure keeps its colour
    ('''    setNum('day', signed(s.day_pnl), s.day_pnl); setNum('dayPct', pct(s.day_pct), s.day_pnl);
''', ''),
    ('''  drawChart(d);
}

// ---- manual pause''', '''  drawChart(d);
  drawGrowth(d);
}

// ---- manual pause'''),
    # ---- the new chart
    ('''async function loadHist() {''', '''let growth = null;
function drawGrowth(d) {
  if (!window.Chart) return;
  const css = getComputedStyle(document.documentElement), v = n => css.getPropertyValue(n).trim();
  const s = d.summary || {};
  const rows = (d.daily || []).filter(r => r.value).map(r => ({...r}));
  const today = (d.intraday || []).length ? d.intraday[d.intraday.length - 1].ts.slice(0, 10) : null;
  if (s.value !== undefined && rows.length && rows[rows.length - 1].day === today)
    Object.assign(rows[rows.length - 1], {value: s.value, invested: s.invested, pnl: s.pnl});  // live figures for today
  const pnlColor = (s.pnl !== undefined ? s.pnl : 0) >= 0 ? v('--up') : v('--down');
  $('pnlSw').style.background = pnlColor;
  const labels = rows.map(r => new Date(r.day + 'T00:00:00').toLocaleDateString('en-IN', {day: 'numeric', month: 'short'}));
  const sets = [
    {label: 'Current value', data: rows.map(r => r.value), yAxisID: 'y', borderColor: v('--accent'), borderWidth: 2},
    {label: 'Invested', data: rows.map(r => r.invested), yAxisID: 'y', borderColor: v('--muted'), borderWidth: 1.5, borderDash: [5, 4]},
    {label: 'Total P&L', data: rows.map(r => r.pnl), yAxisID: 'y1', borderColor: pnlColor, borderWidth: 2},
  ].map(x => ({...x, pointRadius: rows.length < 30 ? 2.5 : 0, pointHoverRadius: 4, pointBackgroundColor: x.borderColor, tension: .2, fill: false}));
  if (growth) {
    growth.data.labels = labels;
    sets.forEach((x, i) => Object.assign(growth.data.datasets[i], x));
    growth.update('none');
    return;
  }
  const lakh = x => '₹' + (Math.abs(x) >= 1e5 ? (x / 1e5).toFixed(1) + 'L' : Number(x).toLocaleString('en-IN'));
  growth = new Chart($('growthChart'), {
    type: 'line', data: {labels, datasets: sets},
    options: {responsive: true, maintainAspectRatio: false, animation: false,
      interaction: {mode: 'index', intersect: false},
      plugins: {legend: {display: false},
        tooltip: {callbacks: {label: c => c.dataset.label + ': ' + (c.dataset.yAxisID === 'y1' ? signed(c.parsed.y) : inr(c.parsed.y))}}},
      scales: {
        x: {ticks: {color: v('--muted'), maxTicksLimit: 7}, grid: {display: false}},
        y: {position: 'left', ticks: {color: v('--muted'), callback: lakh, maxTicksLimit: 6}, grid: {color: v('--line')}},
        y1: {position: 'right', ticks: {color: v('--muted'), callback: x => (x >= 0 ? '+' : '−') + lakh(Math.abs(x)), maxTicksLimit: 6}, grid: {display: false}}
      }}
  });
}

async function loadHist() {'''),
    # ---- the Today chart begins at the open
    ('''    const src = d.intraday || [];''',
     '''    const src = (d.intraday || []).filter(p => p.ts.slice(11, 16) >= '09:15');  // from the market open'''),
    ("  today: 'Day P&L through today. Red dot marks the low, green the high.',",
     "  today: 'Day P&L from the 9:15 open. Red dot marks the low, green the high.',"),
]


def apply(text: str) -> str:
    if MARK in text:
        return text
    for old, new in CHANGES:
        if text.count(old) != 1:
            raise ValueError(f"expected piece not found: {old[:60]!r}")
        text = text.replace(old, new, 1)
    return text


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
