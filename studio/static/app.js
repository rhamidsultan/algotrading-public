// Algo Studio: Strategies and each strategy's page (Overview, Presets, Backtests, Sessions).
// Routes: #/  ·  #/s/<strategy>/<tab>. Data from /studio/api/ (studio/app.py).

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const inr = v => v == null ? "–" : (v < 0 ? "−₹" : "₹") + Math.abs(Math.round(v)).toLocaleString("en-IN");
const cls = v => v == null ? "" : v < 0 ? "down" : "up";
const day = t => { if (!t) return ""; const d = new Date(String(t).slice(0, 10) + "T00:00:00"); return isNaN(d) ? "" : d.toLocaleDateString("en-IN", {day: "numeric", month: "short", year: "2-digit"}); };
const STAGES = ["Draft", "Backtested", "Paper", "Shadow", "Live"];
const NEXT = {Draft: "Next: run a backtest", Backtested: "Next: paper trade it", Paper: "Next: shadow on live prices",
              Shadow: "Next: live, with a small size", Live: "Live"};

let SHARE = null, SHARES = null, IMP = {code: "", pv: null};
let AN = null, AN_PRESET = "", AN_DAYS = 10, CMP = {days: 14};
let ME = {}, FILTER = "All", EDIT = null, ARM = {}, S = null, KEEP = null, BT = null;
let QS = {kind: "backtest", preset: "", lots: "", dataset: "", period: "365", target: "server", feed: "kite", label: ""};

async function api(url, opt) {
  const r = await fetch("/studio/api/" + url, Object.assign({cache: "no-store", headers: {"Content-Type": "application/json"}}, opt || {}));
  if (r.status === 401) { location.href = "/signin?next=/studio/"; throw new Error("Signed out"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
const post = (url, body) => api(url, {method: "POST", body: JSON.stringify(body)});

function route() {
  if (location.hash.startsWith("#/import")) return {page: "import"};
  let m2 = location.hash.match(/^#\/run\/([A-Za-z0-9_.-]+)/);
  if (m2) return {page: "run", name: m2[1]};
  m2 = location.hash.match(/^#\/sweep\/([0-9a-z-]+)/);
  if (m2) return {page: "sweep", id: m2[1]};
  if (location.hash.startsWith("#/compare")) return {page: "compare", runs: (new URLSearchParams(location.hash.split("?")[1] || "").get("runs") || "").split(",").filter(Boolean)};
  if (location.hash.startsWith("#/results")) {
    const q = new URLSearchParams(location.hash.split("?")[1] || "");
    return {page: "results", s: q.get("s") || "", p: q.has("p") ? q.get("p") : null};
  }
  const b = location.hash.match(/^#\/build(?:\/([a-z0-9_]+))?/);
  if (b) return {page: "build", name: b[1] || ""};
  const m = location.hash.match(/^#\/s\/([^/]+)(?:\/([a-z]+))?/);
  return m ? {name: decodeURIComponent(m[1]), tab: m[2] || "overview"} : null;
}

// ---- Strategies ------------------------------------------------------------------------------------------
function figs(s) {
  const b = s.backtest || {};
  const f = (k, label) => s[k]
    ? `<div>${label}<b class="${cls(s[k].total)}">${inr(s[k].total)}</b>${s[k].running ? s[k].running + " running" : s[k].days + " days"}</div>`
    : `<div>${label}<b>–</b></div>`;
  return `<div class="figs"><div>Backtest<b class="${cls(b.net)}">${inr(b.net)}</b>${b.max_dd != null ? "DD " + inr(b.max_dd) : "not run"}</div>${f("paper", "Paper")}${f("shadow", "Shadow")}${f("live", "Live")}</div>`;
}
async function list() {
  const d = await api("strategies");
  const ss = d.strategies, count = st => ss.filter(s => s.stage === st).length;
  const shown = ss.filter(s => FILTER === "All" || s.stage === FILTER).sort((a, b) => STAGES.indexOf(b.stage) - STAGES.indexOf(a.stage) || a.title.localeCompare(b.title));
  $("app").innerHTML = `<div class="top"><h1>Strategies</h1>${ME.can_use ? '<span class="acts2"><a class="btn ghost" href="#/import">Import</a><a class="btn" href="#/build">New strategy</a></span>' : ""}</div>
    <div class="chips" role="group" aria-label="Filter by stage">${["All", ...STAGES.slice().reverse()].filter(st => st === "All" || count(st)).map(st =>
      `<button type="button" class="chip${FILTER === st ? " on" : ""}" data-f="${st}" aria-pressed="${FILTER === st}">${st} ${st === "All" ? ss.length : count(st)}</button>`).join("")}</div>
    ${shown.map(s => `<div class="scard"><a class="cardmain" href="#/s/${encodeURIComponent(s.name)}/overview">
      <div class="row"><span class="nm">${esc(s.title)}</span><span class="pill ${s.stage}">${s.stage}</span></div>
      <div class="muted">${s.presets} preset${s.presets === 1 ? "" : "s"} · ${esc(NEXT[s.stage])}</div>${s.backtest || s.paper || s.shadow || s.live ? figs(s) : ""}</a>
      ${s.backtests ? `<a class="cardfoot" href="#/results?s=${encodeURIComponent(s.name)}">${s.backtests} result${s.backtests === 1 ? "" : "s"}${s.best ? ` · best <b class="${cls(s.best.net)}">${inr(s.best.net)}</b>` : ""}<span>Results ›</span></a>` : ""}</div>`).join("")
      || '<div class="card muted">No strategies at this stage.</div>'}`;
  document.querySelectorAll("[data-f]").forEach(b => b.onclick = () => { FILTER = b.dataset.f; list(); });
}

// ---- one strategy -----------------------------------------------------------------------------------------
function go(kind, pid) {
  const q = `strategy=${encodeURIComponent(S.strategy.name)}` + (pid ? `&preset=${encodeURIComponent(pid)}` : "");
  return {backtest: "/backtests/run?" + q, sweep: "/backtests/sweep?" + q, best: "/backtests/sweep?" + q + "&mode=best",
          paper: "/paper/new?" + q, shadow: "/live/new?" + q}[kind];
}
function described(p) {   // what differs from the strategy's defaults, in words
  const out = [];
  for (const [k, v] of Object.entries(p.params || {})) {
    const d = S.catalog.params.find(x => x.key === k);
    if (!d || d.default == v) continue;
    out.push(`${d.label.replace(/\s*\((?:[^()]*)\)\s*$/, "").replace(/…$/, "").replace(/^…/, "")}: ${v === true ? "on" : v === false ? "off" : v}`);
  }
  return out.length ? out.slice(0, 6).join(" · ") + (out.length > 6 ? ` · +${out.length - 6} more` : "") : "The strategy's defaults";
}
function sessLine(x) {
  if (x.sim) return `<div class="line"><span><span class="badge">Simulated</span> ${esc(x.label || x.id)}<div class="muted">Made-up prices, fast replay${x.sim_time ? " · at " + day(x.sim_time) : ""}: a check that it runs, not paper trading${x.running ? "" : " · stopped"}</div></span>
    <a href="/paper#${encodeURIComponent(x.id)}">Open ›</a></div>`;
  const pill = x.mode === "live" ? "Live" : x.mode === "shadow" ? "Shadow" : "Paper";
  return `<div class="line"><span><span class="pill ${pill}">${pill}</span> ${esc(x.label || x.id)}${x.preset ? ` <span class="muted">· ${esc(x.preset)}</span>` : ""}${x.running ? "" : ' <span class="muted">· stopped</span>'}</span>
    <a class="${cls(x.total)}" href="/${x.mode === "paper" ? "paper" : "live"}#${encodeURIComponent(x.id)}">${inr(x.total)} ›</a></div>`;
}
// ---- Start from a preset: backtest, paper or shadow, lots chosen here ------------------------------------------
const PERIODS = [["182", "Last 6 months"], ["365", "Last year"], ["730", "Last 2 years"], ["", "All the data"]];
const hasLots = () => S.catalog.params.some(p => p.key === "lots");
function settingsOf(pid) {        // the strategy's defaults, then the preset's settings
  const out = {};
  for (const p of S.catalog.params) out[p.key] = p.default;
  const pr = S.presets.find(p => p.id === pid);
  return Object.assign(out, pr ? pr.params : {});
}
function startCard() {
  if (QS.strategy !== S.strategy.name) {      // new strategy: fresh choices; its last backtest's data set
    const last = S.runs.find(r => r.dataset) || {};
    QS = {...QS, strategy: S.strategy.name, preset: (S.presets[0] || {}).id || "", dataset: last.dataset || "", lots: "", label: "", done: ""};
  }
  if (PENDING) { QS = {...QS, ...PENDING, lots: ""}; PENDING = null; }
  if (QS.lots === "") QS.lots = settingsOf(QS.preset).lots ?? 1;
  const shadowOk = ME.owner || ME.live_use, ds = (BT || {}).datasets || [], mac = ((BT || {}).mac || {}).online && !S.strategy.built;
  if (!mac) QS.target = "server";                         // built strategies live on the server only
  if (!QS.dataset || !ds.some(d => d.name === QS.dataset)) QS.dataset = (ds.find(d => d.name === "TD_NIFTY_1minute") || ds[0] || {}).name || "";
  const kinds = [["backtest", "Backtest"], ["paper", "Paper"], ...(shadowOk ? [["shadow", "Shadow"]] : [])];
  if (!kinds.some(k => k[0] === QS.kind)) QS.kind = "backtest";
  const opt = (v, cur, t) => `<option value="${esc(v)}"${String(v) === String(cur) ? " selected" : ""}>${esc(t)}</option>`;
  let form = "";
  if (QS.kind === "backtest") {
    form = `<div class="form"><div><label for="qDs">Data set</label><select id="qDs">${ds.map(d => opt(d.name, QS.dataset, d.name)).join("") || "<option>Loading…</option>"}</select></div>
      <div><label for="qPer">Period</label><select id="qPer">${PERIODS.map(([v, t]) => opt(v, QS.period, t)).join("")}</select></div>
      ${mac ? `<div><label for="qTgt">Run on</label><select id="qTgt">${opt("server", QS.target, "Server")}${opt("mac", QS.target, "Mac (" + esc(BT.mac.name || "helper") + ")")}</select></div>` : ""}</div>`;
  } else {
    form = `<div class="form">${QS.kind === "paper" ? `<div><label for="qFeed">Prices</label><select id="qFeed">${opt("kite", QS.feed, "Zerodha live prices")}${opt("sim", QS.feed, "Simulated (fast replay, a check only)")}</select></div>` : ""}
      <div><label for="qLabel">Name (optional)</label><input id="qLabel" maxlength="40" value="${esc(QS.label)}" placeholder="${esc(S.strategy.title)}"></div></div>
      ${QS.kind === "shadow" ? '<p class="muted" style="margin:8px 0 0">Shadow follows Zerodha live prices exactly as live would, without sending orders.</p>' : ""}`;
  }
  const verb = {backtest: "Run backtest", paper: "Start paper", shadow: "Start shadow"}[QS.kind];
  return `<div class="card"><p class="h2">Start</p>
    <div class="chips" role="group" aria-label="What to start">${kinds.map(([k, t]) => `<button type="button" class="chip${QS.kind === k ? " on" : ""}" data-kind="${k}" aria-pressed="${QS.kind === k}">${t}</button>`).join("")}</div>
    <div class="form"><div><label for="usePreset" style="margin-top:0">Preset</label><select id="usePreset">${opt("", QS.preset, "The strategy's defaults")}${S.presets.map(p => opt(p.id, QS.preset, p.name)).join("")}</select></div>
      ${hasLots() ? `<div><label for="qLots" style="margin-top:0">${S.strategy.built ? "Lots × (each leg's lots)" : "Lots per leg"}</label><input id="qLots" inputmode="numeric" value="${esc(QS.lots)}"></div>` : ""}</div>
    ${form}
    <div class="err" id="qErr" role="alert"></div>
    ${QS.done ? `<div class="ok" role="status">${QS.done}</div>` : ""}
    ${ME.can_use ? `<button type="button" class="btn" data-quick="1" style="width:100%;margin-top:12px">${verb}</button>` : '<p class="muted">Your login can look but not start things.</p>'}
    <div class="more"><a href="${go(QS.kind === "shadow" ? "shadow" : QS.kind, QS.preset)}">More options ›</a><a href="${go("sweep", QS.preset)}">Sweep ›</a><a href="${go("best", QS.preset)}">Find best ›</a></div></div>`;
}
function readQuick() {
  const v = id => $(id) ? $(id).value : undefined;
  QS.preset = v("usePreset") ?? QS.preset;
  if ($("qLots")) QS.lots = v("qLots");
  if ($("qDs")) QS.dataset = v("qDs");
  if ($("qPer")) QS.period = v("qPer");
  if ($("qTgt")) QS.target = v("qTgt");
  if ($("qFeed")) QS.feed = v("qFeed");
  if ($("qLabel")) QS.label = v("qLabel");
}
const ago = days => { const d = new Date(Date.now() - days * 864e5); return d.toISOString().slice(0, 10); };
async function dash(url, body) {   // the dashboard's own API (same site, same login)
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), cache: "no-store"});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
  return j;
}
async function quick(r) {
  readQuick();
  const params = settingsOf(QS.preset);
  if (hasLots()) {
    const n = parseInt(QS.lots, 10);
    if (!(n >= 1)) { $("qErr").textContent = "Lots per leg: 1 or more."; return; }
    params.lots = n;
  }
  const preset = QS.preset ? {id: QS.preset} : null, name = S.strategy.name;
  $("qErr").textContent = "";
  try {
    if (QS.kind === "backtest") {
      const d = (BT.datasets || []).find(x => x.name === QS.dataset) || {};
      const to = d.to || "", from = QS.period && to ? new Date(new Date(to).getTime() - QS.period * 864e5).toISOString().slice(0, 10) : "";
      const j = await dash("/api/backtest-runner", {strategy: name, dataset: QS.dataset, from: from && d.from && from < d.from ? d.from : from,
        to, params, preset, target: QS.target, note: ""});
      QS.done = `Backtest queued${QS.target === "mac" ? " for the Mac" : ""}. <a href="/backtests/run">Follow it ›</a>`;
      void j;
    } else {
      const feed = QS.kind === "shadow" ? "kite" : QS.feed;
      const body = {strategy: name, feed, params, mode: QS.kind, label: QS.label, preset};
      if (QS.kind === "shadow") body.live = {max_lots: Math.max(1, params.lots || 1), daily_loss_limit: 0, max_orders_per_day: 40,
                                             protect_mult: params["risk.protect_mult"] || 3};
      const j = await dash("/api/paper/start", body);
      QS.done = `${QS.kind === "shadow" ? "Shadow" : "Paper"} session started. <a href="/${QS.kind === "paper" ? "paper" : "live"}#${encodeURIComponent(j.id)}">Open it ›</a>`;
      QS.label = "";
    }
    await strategy(r);
  } catch (e) { $("qErr").textContent = e.message; }
}

// ---- Share with a friend, and Import a friend's share -----------------------------------------------------------
function sharedNote(p) {
  const sh = p.share;
  if (!sh) return "";
  const b = sh.backtest;
  return `<div class="set">Shared by ${esc(sh.from || "a friend")} (v${esc(sh.version)})${b ? ` · their backtest ${day(b.from)} – ${day(b.to)}: ${inr(b.net)}, DD ${inr(b.max_dd)}` : ""}</div>`;
}
function sharePanel() {
  const p = S.presets.find(x => x.id === SHARE.pid) || {};
  const mine = (SHARES || []).filter(x => x.preset === p.id);
  return `<div class="card"><p class="h2">Share ${esc(p.name)}</p>
      <p style="margin:0 0 10px">Friends get the strategy's settings, not your trades. It runs on their own server, with their own Zerodha login, lots and limits.</p>
      <label><input type="checkbox" id="shBt"${SHARE.backtest ? " checked" : ""}>Backtest summary (this preset's latest backtest)</label>
      <label><input type="checkbox" id="shRes"${SHARE.results ? " checked" : ""}>My paper, shadow and live results so far</label>
      <label for="shTo">Friend's name (for your list, optional)</label><input id="shTo" maxlength="40" value="${esc(SHARE.to || "")}">
      <p class="muted">Always included: the settings and Orders &amp; stops. Never included: your account, logins, keys, server address or positions.</p>
      <div class="err" id="shErr" role="alert"></div>
      ${SHARE.nobt ? '<p class="warn">This preset has no backtest yet, so no backtest summary is included.</p>' : ""}
      ${SHARE.code ? `<label for="shCode">Share code</label><textarea id="shCode" class="code" readonly rows="5">${esc(SHARE.code)}</textarea>
        <div class="acts" style="margin-top:8px"><button type="button" class="btn" data-shcopy="1">${SHARE.copied ? "Copied" : "Copy code"}</button>
          ${navigator.share ? '<button type="button" class="btn ghost" data-shsend="1">Send…</button>' : ""}<button type="button" class="btn ghost" data-shfile="1">Download file</button></div>
        <p class="muted">Your friend opens Algo Studio › Strategies › Import on their dashboard and pastes it.</p>`
        : `<button type="button" class="btn" data-shmake="1" style="width:100%;margin-top:6px">Make share code</button>`}
      <button type="button" class="btn ghost" data-shclose="1" style="width:100%;margin-top:8px">Done</button></div>
    ${mine.length ? `<div class="card"><p class="h2">Shared before</p>${mine.map(x => `<div class="line"><span>v${esc(x.version)} · ${day(x.last)}${(x.to || []).length ? " · " + esc(x.to.join(", ")) : ""}
        ${x.changed ? `<div class="warn">Changed since (now v${esc(x.now_version)}): make a new code and send it; your friend sees what changed.</div>` : '<div class="muted">Up to date</div>'}</span>
        <button type="button" class="linkbtn" data-shforget="${esc(x.id)}">Forget</button></div>`).join("")}</div>` : ""}`;
}
async function loadShares() {
  try { SHARES = (await api("shares/" + encodeURIComponent(S.strategy.name))).shares; } catch (e) { SHARES = []; }
}
function importPage() {
  const pv = IMP.pv;
  const sh = pv && pv.share, b = sh && sh.backtest;
  $("app").innerHTML = `<a href="#/">‹ Strategies</a><div class="top" style="margin-top:6px"><h1>Import a strategy</h1></div>
    <div class="card"><label for="impCode" style="margin-top:0">Share code or file</label>
      <textarea id="impCode" class="code" rows="4" placeholder="ALGOSHARE1.…">${esc(IMP.code)}</textarea>
      <div class="acts" style="grid-template-columns:1fr 1fr;margin-top:8px"><label class="btn ghost" for="impFile" style="margin:0">Choose file</label><button type="button" class="btn" data-impcheck="1">Check</button></div>
      <input type="file" id="impFile" accept=".json,.algoshare,application/json,text/plain" hidden>
      <div class="err" id="impErr" role="alert"></div></div>
    ${pv ? `<div class="card"><div class="row"><span class="nm">${esc(sh.title || sh.strategy)} · ${esc(sh.preset.name)}</span><span class="pill Draft">${pv.update_of ? "Update" : "New"}</span></div>
      <div class="muted">from ${esc(sh.from || "a friend")} · shared ${day(sh.shared)} · v${esc(sh.version)}</div>
      ${(pv.described || []).length ? `<div class="set">${pv.described.slice(0, 10).map(d => esc(d.label + ": " + (d.value === true ? "on" : d.value === false ? "off" : d.value))).join(" · ")}</div>` : '<div class="set">The strategy\'s defaults</div>'}
      ${b ? `<div class="set">Their backtest ${day(b.from)} – ${day(b.to)}${b.dataset ? " (" + esc(b.dataset) + ")" : ""}: <b class="${cls(b.net)}">${inr(b.net)}</b>, DD ${inr(b.max_dd)}${b.cycles != null ? ", " + esc(b.cycles) + " cycles" : ""}${b.real ? ", " + esc(b.real) + " real prices" : ""}</div>` : ""}
      ${(sh.results || []).map(x => `<div class="set">Their ${esc(x.mode)}: ${inr(x.net)} over ${esc(x.days)} days</div>`).join("")}
      <div style="margin-top:10px">${pv.checks.map(c => `<div class="gate">${mark(c.ok)}<span>${esc(c.name)}${c.note ? `<div class="muted">${esc(c.note)}</div>` : ""}</span></div>`).join("")}</div>
      ${pv.ok ? (pv.update_of ? `<p class="h2" style="margin-top:12px">Changes from your “${esc(pv.update_of.name)}”</p>
          ${pv.update_of.changes.length ? pv.update_of.changes.map(c => `<div class="line"><span>${esc(c.label)}</span><span>${esc(c.before ?? "–")} → <b>${esc(c.after ?? "–")}</b></span></div>`).join("") : '<p class="muted">No changes in the settings.</p>'}
          <p class="muted">Running sessions keep their settings until you start them again with the preset.</p>
          <div class="acts" style="grid-template-columns:1fr 1fr"><button type="button" class="btn ghost" data-imp="new">Keep as new preset</button><button type="button" class="btn" data-imp="update">Update my preset</button></div>`
        : `<p class="muted">It arrives as a preset of ${esc(sh.title || sh.strategy)}. Backtest it on your own data first; paper, shadow and live follow the same checks as your own strategies.</p>
          <label for="impName">Preset name</label><input id="impName" maxlength="34" value="${esc(sh.preset.name)}">
          <div class="acts" style="grid-template-columns:1fr 1fr;margin-top:10px"><button type="button" class="btn ghost" data-imp="new">Import</button><button type="button" class="btn" data-imp="new" data-then="backtest">Import &amp; backtest</button></div>`) : ""}
      <div class="err" id="impErr2" role="alert"></div></div>` : ""}`;
  const err = (id, e) => { $(id).textContent = e.message; };
  $("impFile").onchange = async () => { const f = $("impFile").files[0]; if (!f) return; if (f.size > 40000) { $("impErr").textContent = "That file is too big for a share."; return; }
    IMP.code = await f.text(); $("impCode").value = IMP.code; check(); };
  const check = async () => { IMP.code = $("impCode").value.trim(); IMP.pv = null;
    try { IMP.pv = await post("import/preview", {code: IMP.code}); importPage(); } catch (e) { importPage(); err("impErr", e); } };
  document.querySelector("[data-impcheck]").onclick = check;
  document.querySelectorAll("[data-imp]").forEach(btn => btn.onclick = async () => {
    btn.disabled = true;
    try {
      const j = await post("import", {code: IMP.code, how: btn.dataset.imp, name: $("impName") ? $("impName").value : ""});
      IMP = {code: "", pv: null};
      if (btn.dataset.then === "backtest") { QS = {...QS, strategy: undefined}; PENDING = {preset: j.id, kind: "backtest"}; location.hash = `#/s/${encodeURIComponent(j.strategy)}/overview`; }
      else location.hash = `#/s/${encodeURIComponent(j.strategy)}/presets`;
    } catch (e) { err("impErr2", e); btn.disabled = false; }
  });
}
let PENDING = null;

// ---- Results: every backtest and sweep by strategy and preset (studio/results.py) -----------------------------------
let RS = {s: "", p: null, kind: "all", days: 0, sort: "new", arch: false}, RES = null, DELARM = false;
const SEL = new Set();
async function loadResults() {
  const q = new URLSearchParams({kind: RS.kind, days: RS.days, sort: RS.sort, archived: RS.arch ? "1" : "0"});
  if (RS.s) q.set("strategy", RS.s);
  if (RS.p !== null && RS.s) q.set("preset", RS.p);
  RES = await api("results?" + q);
}
async function resultsPage(r) {
  if (RS.s !== r.s || RS.p !== r.p) SEL.clear();
  RS = {...RS, s: r.s, p: r.p};
  await loadResults();
  const all = RS.s ? null : RES;
  $("app").innerHTML = `<div class="top"><h1>Results</h1><a class="muted" href="/backtests">Old results page ›</a></div>
    <div class="form" style="margin-bottom:6px"><div><label for="rsS" style="margin-top:0">Strategy</label><select id="rsS">
      <option value="">All strategies</option>${(RESALL || RES).groups.map(g => `<option value="${esc(g.strategy)}"${g.strategy === RS.s ? " selected" : ""}>${esc(g.title)}</option>`).join("")}</select></div></div>
    ${RS.s ? resultsBody(null) : groupsView()}`;
  if (!RS.s) RESALL = RES;
  $("rsS").onchange = () => { location.hash = $("rsS").value ? `#/results?s=${encodeURIComponent($("rsS").value)}` : "#/results"; };
  wireResults(() => resultsPage(route()));
}
let RESALL = null;
function groupsView() {
  const gs = RES.groups;
  if (!gs.length) return '<div class="card muted">No backtests yet. Run one from a strategy: Overview › Start.</div>';
  return filtersHtml(false) + gs.map(g => `<div class="card"><div class="row"><span class="nm">${g.known ? esc(g.title) : "Unsorted: " + esc(g.title || "?")}</span>
      ${g.stage ? `<span class="pill ${g.stage}">${g.stage}</span>` : ""}</div>
    <div class="muted">${g.count} result${g.count === 1 ? "" : "s"}${g.best ? ` · best <b class="${cls(g.best.net)}">${inr(g.best.net)}</b>` : ""}${g.known ? ` · <a href="#/s/${encodeURIComponent(g.strategy)}/overview">Strategy ›</a>` : " · the strategy is no longer on this server"}</div>
    ${g.presets.map(p => `<a class="prow" href="#/results?s=${encodeURIComponent(g.strategy)}&p=${encodeURIComponent(p.id)}">
      <span><b>${esc(p.name)}</b>${p.ref ? ' <span class="star" title="Reference">★</span>' : ""}<span class="muted"> · ${p.count} run${p.count === 1 ? "" : "s"}</span></span>
      <span class="muted">latest <span class="${cls(p.latest.net)}">${inr(p.latest.net)}</span> · best <span class="${cls((p.best || {}).net)}">${inr((p.best || {}).net)}</span> ›</span></a>`).join("")}</div>`).join("");
}
function filtersHtml(withPresets) {
  const chip = (k, l) => `<button type="button" class="chip${RS.kind === k ? " on" : ""}" data-rkind="${k}" aria-pressed="${RS.kind === k}">${l}</button>`;
  const g = withPresets && RES.groups[0];
  return `<div class="chips" role="group" aria-label="Type">${chip("all", "All")}${chip("backtest", "Backtests")}${chip("sweep", "Sweeps & Find best")}${chip("compare", "Paper vs backtest")}</div>
    ${g ? `<div class="chips" role="group" aria-label="Preset"><button type="button" class="chip${RS.p === null ? " on" : ""}" data-rpre="*">All presets</button>${g.presets.map(p =>
      `<button type="button" class="chip${RS.p === p.id ? " on" : ""}" data-rpre="${esc(p.id)}">${esc(p.name)}${p.ref ? " ★" : ""}</button>`).join("")}</div>` : ""}
    <div class="form2"><div><label for="rsDays" style="margin-top:0">Saved</label><select id="rsDays">${[[0, "Any time"], [7, "Last 7 days"], [30, "Last 30 days"], [90, "Last 90 days"]].map(([v, l]) => `<option value="${v}"${+RS.days === v ? " selected" : ""}>${l}</option>`).join("")}</select></div>
      <div><label for="rsSort" style="margin-top:0">Sort</label><select id="rsSort">${[["new", "Newest first"], ["net", "Net P&L"], ["retdd", "Return ÷ drawdown"]].map(([v, l]) => `<option value="${v}"${RS.sort === v ? " selected" : ""}>${l}</option>`).join("")}</select></div></div>
    <label style="margin:-4px 0 10px"><input type="checkbox" id="rsArch"${RS.arch ? " checked" : ""}>Show archived</label>`;
}
function runLine(r) {
  const badges = [r.ref ? '<span class="badge ref">★ Reference</span>' : "", r.kind === "compare" ? '<span class="badge">Paper vs backtest</span>' : "",
    r.where ? `<span class="badge">${r.where === "mac" ? "Mac" : "Server"}</span>` : "", r.archived ? '<span class="badge">Archived</span>' : ""].join("");
  return `<div class="run${SEL.has(r.name) ? " sel" : ""}">${ME.can_use ? `<input type="checkbox" class="rchk" data-sel="${esc(r.name)}" aria-label="Select"${SEL.has(r.name) ? " checked" : ""}>` : ""}
    <div class="rb"><a class="row rlink" href="#/run/${encodeURIComponent(r.name)}"><span><b>${esc(r.preset_name)}</b> · ${esc(r.period || day(r.from) + " – " + day(r.to))}</span><span class="${cls(r.net)}"><b>${inr(r.net)}</b> ›</span></a>
      <div class="muted">${esc([r.data, r.fills].filter(Boolean).join(" · "))} · DD ${inr(r.max_dd)}${r.ret_dd != null ? " · ret÷DD " + esc(r.ret_dd) : ""}${r.cycles != null ? " · " + esc(r.cycles) + " cycles" : ""} · saved ${day(r.saved)}</div>
      ${badges ? `<div style="margin-top:4px">${badges}</div>` : ""}${r.user_note ? `<div class="set">${esc(r.user_note)}</div>` : ""}
      ${ME.can_use ? `<div class="racts"><button type="button" class="linkbtn" data-ref="${esc(r.name)}" data-on="${r.ref ? 0 : 1}">${r.ref ? "Unstar" : "★ Make reference"}</button>
        <button type="button" class="linkbtn" data-keep="${esc(r.name)}">Save as preset</button></div>` : ""}
      ${KEEP === r.name ? `<div class="keep"><label for="keepName">Preset name</label><input id="keepName" maxlength="40" placeholder="e.g. Best Oct 2026">
        <div class="err" id="keepErr" role="alert"></div><div class="acts" style="grid-template-columns:1fr 1fr;margin-top:8px">
        <button type="button" class="btn ghost" data-keepno="1">Cancel</button><button type="button" class="btn" data-keepok="${esc(r.name)}">Save preset</button></div></div>` : ""}</div></div>`;
}
function resultsBody(fixed) {
  if (!RES) return '<p class="muted">Loading…</p>';
  const sw = RES.sweeps.filter(j => RS.kind === "all" || RS.kind === "sweep");
  const rs = RS.kind === "sweep" ? [] : RES.runs;
  return filtersHtml(true)
    + (SEL.size ? `<div class="batch"><span>${SEL.size} selected</span>${SEL.size >= 2 && SEL.size <= 3 ? `<a class="btn" href="#/compare?runs=${[...SEL].map(encodeURIComponent).join(",")}">Compare</a>` : ""}<button type="button" class="btn ghost" data-arch="1">Archive</button>${RS.arch ? '<button type="button" class="btn ghost" data-arch="0">Unarchive</button>' : ""}
        <button type="button" class="btn danger${DELARM ? " armed" : ""}" data-rdel="1">${DELARM ? "Tap again" : "Delete"}</button><button type="button" class="linkbtn" data-selnone="1">Clear</button></div><div class="err" id="batchErr" role="alert"></div>` : "")
    + `<div class="card">${rs.length ? rs.map(runLine).join("") : `<p class="muted" style="margin:0">No ${RS.kind === "compare" ? "paper-vs-backtest runs" : "backtests"} here${RS.arch ? "" : " (archived ones are hidden)"}.</p>`}</div>`
    + (sw.length ? `<div class="card"><p class="h2">Sweeps &amp; Find best</p>${sw.map(j => `<div class="line"><span>${j.kind === "find_best" ? "Find best settings" : j.folds ? "Walk-forward sweep" : "Sweep"} · ${esc(j.period)}
        <div class="muted">${esc(j.data)} · ${j.total || ""} combinations · ${day(j.saved)}</div></span><a href="#/sweep/${encodeURIComponent(j.id)}">Open ›</a></div>`).join("")}</div>` : "");
}
function wireResults(redraw) {
  const on = (sel, fn) => document.querySelectorAll(sel).forEach(el => el.onclick = () => fn(el));
  const reload = async () => { await loadResults(); redraw(); };
  on("[data-rkind]", async b => { RS.kind = b.dataset.rkind; await reload(); });
  on("[data-rpre]", async b => {
    RS.p = b.dataset.rpre === "*" ? null : b.dataset.rpre;
    if (location.hash.startsWith("#/results")) location.hash = `#/results?s=${encodeURIComponent(RS.s)}` + (RS.p !== null ? `&p=${encodeURIComponent(RS.p)}` : "");
    else await reload();
  });
  if ($("rsDays")) $("rsDays").onchange = async () => { RS.days = +$("rsDays").value; await reload(); };
  if ($("rsSort")) $("rsSort").onchange = async () => { RS.sort = $("rsSort").value; await reload(); };
  if ($("rsArch")) $("rsArch").onchange = async () => { RS.arch = $("rsArch").checked; SEL.clear(); await reload(); };
  document.querySelectorAll("[data-sel]").forEach(el => el.onchange = () => { el.checked ? SEL.add(el.dataset.sel) : SEL.delete(el.dataset.sel); DELARM = false; redraw(); });
  on("[data-selnone]", () => { SEL.clear(); redraw(); });
  on("[data-ref]", async b => { try { await post("results/ref", {run: b.dataset.ref, on: b.dataset.on === "1"}); } catch (e) { alert(e.message); } await reload(); });
  on("[data-arch]", async b => { await post("results/archive", {runs: [...SEL], on: b.dataset.arch === "1"}); SEL.clear(); await reload(); });
  on("[data-rdel]", async () => {
    if (!DELARM) { DELARM = true; redraw(); return; }
    DELARM = false;
    const fails = [];
    for (const n of [...SEL]) {                 // the dashboard deletes a saved run (its own login checks)
      const r = await fetch(`/api/backtests/${encodeURIComponent(n)}/delete`, {method: "POST"}).catch(() => null);
      if (r && r.ok) SEL.delete(n); else fails.push(n);
    }
    await reload();
    if (fails.length && $("batchErr")) $("batchErr").textContent = `${fails.length} could not be deleted (your login may not delete results).`;
  });
  on("[data-keep]", b => { KEEP = b.dataset.keep; redraw(); if ($("keepName")) $("keepName").focus(); });
  on("[data-keepno]", () => { KEEP = null; redraw(); });
  on("[data-keepok]", async b => {
    try { const j = await post("presets/from-run", {run: b.dataset.keepok, name: $("keepName").value}); KEEP = null;
          location.hash = `#/s/${encodeURIComponent(j.strategy)}/presets`; }
    catch (e) { $("keepErr").textContent = e.message; }
  });
}

// ---- one result, results side by side, a sweep's picks -------------------------------------------------------------
const CC = ["var(--accent)", "#d97706", "#8b5cf6"];
function chart(curves, opts = {}) {     // P&L since the start (lines) and, for one run, its drawdown (area below)
  const W = 640, H = opts.dd ? 230 : 180, top = 10, mid = opts.dd ? 160 : 170;
  const all = curves.flat();
  if (!all.length) return '<p class="muted">No daily equity saved with this result.</p>';
  const days = [...new Set(all.map(p => p[0]))].sort(), ix = Object.fromEntries(days.map((d, i) => [d, i]));
  const vals = all.map(p => p[1]), lo = Math.min(0, ...vals), hi = Math.max(0, ...vals), span = hi - lo || 1;
  const X = d => 40 + ix[d] / Math.max(1, days.length - 1) * (W - 50), Y = v => top + (hi - v) / span * (mid - top);
  const lines = curves.map((c, i) => c.length ? `<polyline fill="none" stroke="${CC[i]}" stroke-width="2" points="${c.map(p => X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join(" ")}"/>` : "").join("");
  let dd = "";
  if (opts.dd && curves[0].length) {
    const c = curves[0], dlo = Math.min(-1, ...c.map(p => p[2])), Yd = v => mid + 12 + (v / dlo) * (H - mid - 16);
    dd = `<polygon fill="var(--down)" fill-opacity=".25" points="${X(c[0][0])},${Yd(0)} ${c.map(p => X(p[0]).toFixed(1) + "," + Yd(p[2]).toFixed(1)).join(" ")} ${X(c[c.length - 1][0])},${Yd(0)}"/>
      <text x="4" y="${mid + 24}" class="ax">DD</text><text x="4" y="${H - 2}" class="ax">${esc(inr(dlo))}</text>`;
  }
  return `<svg viewBox="0 0 ${W} ${H}" class="chart" role="img" aria-label="${esc(opts.label || "P&L since the start")}">
    <line x1="40" x2="${W - 10}" y1="${Y(0)}" y2="${Y(0)}" class="zero"/>
    <text x="4" y="${Y(hi) + 4}" class="ax">${esc(inr(hi))}</text><text x="4" y="${Y(lo)}" class="ax">${esc(inr(lo))}</text>
    ${lines}${dd}
    <text x="40" y="${H - 1}" class="ax">${esc(day(days[0]))}</text><text x="${W - 10}" y="${H - 1}" class="ax" text-anchor="end">${esc(day(days[days.length - 1]))}</text></svg>`;
}
const figv = (v, kind) => v == null ? "–" : kind === "cost" ? inr(v) : kind === "money" ? `<span class="${cls(v)}">${inr(v)}</span>` : esc(typeof v === "number" ? v.toLocaleString("en-IN") : v);
const showv = v => v === true ? "on" : v === false ? "off" : v == null ? "–" : String(v);
async function runPage(name) {
  const d = await api("run/" + encodeURIComponent(name)), r = d.run;
  const badges = [r.ref ? '<span class="badge ref">★ Reference</span>' : "", r.kind === "compare" ? '<span class="badge">Paper vs backtest</span>' : "",
    r.where ? `<span class="badge">${r.where === "mac" ? "Mac" : "Server"}</span>` : "", r.archived ? '<span class="badge">Archived</span>' : ""].join("");
  const cyc = (title, rows) => rows.length ? `${title ? `<p class="h2" style="margin-top:12px">${title}</p>` : ""}<table class="tbl"><tbody>${rows.map(c =>
      `<tr><td>${esc(c.exit.slice(0, 10))}<div class="muted">${esc(c.reason || "")}</div></td><td>${num(c.net)}</td></tr>`).join("")}</tbody></table>` : "";
  $("app").innerHTML = `<a href="#/results?s=${encodeURIComponent(r.strategy || "")}">‹ ${esc(r.title || "Results")}</a>
    <div class="top" style="margin-top:6px"><h1>${esc(r.preset_name || "")} · ${esc(r.period || "")}</h1></div>
    <p class="muted" style="margin:0 0 6px">${esc([r.data, r.fills].filter(Boolean).join(" · "))} · saved ${day(r.saved)}</p>
    ${badges ? `<div style="margin-bottom:6px">${badges}</div>` : ""}${r.user_note ? `<p class="set" style="margin-top:0">${esc(r.user_note)}</p>` : ""}
    ${ME.can_use ? `<div class="acts" style="margin:10px 0 12px"><button type="button" class="btn ghost" data-ref="${esc(r.name)}" data-on="${r.ref ? 0 : 1}">${r.ref ? "Unstar" : "★ Reference"}</button>
      <button type="button" class="btn ghost" data-keep="${esc(r.name)}">Save as preset</button>
      ${r.known ? `<button type="button" class="btn" data-again="1">Run again</button>` : ""}</div>` : ""}
    ${KEEP === r.name ? `<div class="card"><label for="keepName" style="margin-top:0">Preset name</label><input id="keepName" maxlength="40">
      <div class="err" id="keepErr" role="alert"></div><div class="acts" style="grid-template-columns:1fr 1fr;margin-top:8px">
      <button type="button" class="btn ghost" data-keepno="1">Cancel</button><button type="button" class="btn" data-keepok="${esc(r.name)}">Save preset</button></div></div>` : ""}
    <div class="tiles">${d.figs.map(f => `<div class="tile"><span class="muted">${esc(f.label)}</span><b>${figv(f.value, f.kind)}</b></div>`).join("")}</div>
    <div class="card"><p class="h2">P&amp;L since the start, and drawdown</p>${chart([d.curve], {dd: true})}${monthsChart(d.months)}</div>
    <div class="card"><p class="h2">Settings used</p><table class="tbl"><tbody>${d.settings.map(x => `<tr${x.changed ? ' class="hl"' : ""}><td>${esc(x.label)}${x.risk ? ' <span class="muted">(Orders &amp; stops)</span>' : ""}</td><td>${esc(showv(x.value))}</td></tr>`).join("")}</tbody></table>
      <p class="muted" style="margin:6px 0 0">Highlighted: different from the strategy's defaults.</p></div>
    <div class="card"><p class="h2">Cycles (${d.cycles})</p>${cyc("Worst 5", d.worst)}${cyc("Best 5", d.best)}<details style="margin-top:10px"><summary>Latest 20</summary>${cyc("", d.recent)}</details></div>
    <details class="card"><summary>All figures</summary><table class="tbl"><tbody>${Object.entries(d.metrics).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(showv(v))}</td></tr>`).join("")}</tbody></table></details>
    <p><a href="/backtests?run=${encodeURIComponent(r.name)}">Open on the old results page (trades, fills, log) ›</a></p>`;
  wireResults(() => runPage(name));
  const again = document.querySelector("[data-again]");
  if (again) again.onclick = () => { PENDING = {kind: "backtest", preset: r.preset_id || ""}; QS = {...QS, strategy: undefined};
    location.hash = `#/s/${encodeURIComponent(r.strategy)}/overview`; };
}
async function comparePage(names) {
  let d;
  try { d = await api("compare-runs?runs=" + names.map(encodeURIComponent).join(",")); }
  catch (e) { $("app").innerHTML = `<a href="#/results">‹ Results</a><div class="card down">${esc(e.message)}</div>`; return; }
  const head = d.runs.map((r, i) => `<th scope="col"><span class="dot2" style="background:${CC[i]}"></span><a href="#/run/${encodeURIComponent(r.name)}">${esc(r.preset_name)}</a><div class="muted">${esc(r.period || "")}</div></th>`).join("");
  const diff = d.settings.filter(x => x.differs);
  $("app").innerHTML = `<a href="#/results${d.same_strategy ? "?s=" + encodeURIComponent(d.runs[0].strategy) : ""}">‹ Results</a>
    <div class="top" style="margin-top:6px"><h1>Compare</h1></div>
    ${d.same_strategy ? "" : '<p class="warn">These are different strategies.</p>'}
    <div class="card"><p class="h2">P&amp;L since the start</p>${chart(d.curves)}</div>
    <div class="card" style="overflow-x:auto"><table class="tbl cmp"><thead><tr><th></th>${head}</tr></thead><tbody>
      ${d.runs.map(() => "").join("")}<tr><td>Data, fills</td>${d.runs.map(r => `<td>${esc(r.data)}<div class="muted">${esc(r.fills)}</div></td>`).join("")}</tr>
      ${d.figs.map(f => `<tr><td>${esc(f.label)}</td>${f.values.map(v => `<td>${figv(v, f.kind)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
    <div class="card" style="overflow-x:auto"><p class="h2">Settings that differ (${diff.length})</p>${diff.length ? `<table class="tbl cmp"><tbody>${diff.map(x =>
      `<tr class="hl"><td>${esc(x.label)}</td>${x.values.map(v => `<td>${esc(showv(v))}</td>`).join("")}</tr>`).join("")}</tbody></table>` : '<p class="muted" style="margin:0">Same settings: the difference is the period, data or fills.</p>'}
      <details style="margin-top:8px"><summary>All settings</summary><table class="tbl cmp"><tbody>${d.settings.map(x =>
      `<tr${x.differs ? ' class="hl"' : ""}><td>${esc(x.label)}</td>${x.values.map(v => `<td>${esc(showv(v))}</td>`).join("")}</tr>`).join("")}</tbody></table></details></div>`;
}
let PICKSAVE = null;
async function sweepPage(id) {
  const d = await api("sweep/" + encodeURIComponent(id));
  const items = d.find_best ? (d.picks || []) : (d.rows || []);
  const lbl = Object.fromEntries(d.grid.map(g => [g.key, g.label]));
  const card = it => `<div class="card"><div class="row"><b>${d.find_best ? "Pick " + it.n : "#" + it.n}</b><b class="${cls(d.find_best ? it.total_net : it.net)}">${inr(d.find_best ? it.total_net : it.net)}</b></div>
      <div class="set">${Object.entries(it.varied).map(([k, v]) => esc((lbl[k] || k) + ": " + showv(v))).join(" · ")}</div>
      <div class="muted">${d.find_best ? `worst DD ${inr(it.worst_dd)} · ${it.periods_up} of ${it.periods} periods up · ${it.cycles} cycles${it.lock_net != null ? " · locked-away months " + inr(it.lock_net) : ""}${it.conservative_net != null ? " · conservative fills " + inr(it.conservative_net) : ""}`
        : `DD ${inr(it.max_dd)}${it.ret_dd != null ? " · ret÷DD " + esc(it.ret_dd) : ""}${it.cycles != null ? " · " + esc(it.cycles) + " cycles" : ""}${it.robust_net != null ? " · neighbours " + inr(it.robust_net) : ""}`}</div>
      ${(it.reasons || []).length ? `<div class="set">${it.reasons.map(esc).join("; ")}</div>` : ""}
      ${ME.can_use ? (PICKSAVE === it.n ? `<label for="pkName">Preset name</label><input id="pkName" maxlength="40" value="${esc((d.find_best ? "Pick " : "Sweep #") + it.n + " " + new Date().toLocaleDateString("en-IN", {day: "numeric", month: "short"}))}">
        <div class="err" id="pkErr" role="alert"></div><div class="acts" style="grid-template-columns:1fr 1fr 1fr;margin-top:8px"><button type="button" class="btn ghost" data-pkno="1">Cancel</button>
        <button type="button" class="btn ghost" data-pkok="${it.n}">Save</button><button type="button" class="btn" data-pkok="${it.n}" data-then="1">Save &amp; backtest</button></div>`
        : `<div class="racts"><button type="button" class="linkbtn" data-pk="${it.n}">Save as preset…</button></div>`) : ""}</div>`;
  $("app").innerHTML = `<a href="#/results?s=${encodeURIComponent(d.strategy || "")}">‹ Results</a>
    <div class="top" style="margin-top:6px"><h1>${d.find_best ? "Find best settings" : "Sweep"}</h1></div>
    <p class="muted" style="margin:0 0 8px">${esc(d.title || d.strategy)} · ${esc(d.period)} · ${esc(d.data)} · ${esc(d.total || "")} combinations · varied: ${d.grid.map(g => esc(g.label)).join(", ")}</p>
    ${d.verdict ? `<div class="card"><p style="margin:0">${esc(d.verdict)}</p></div>` : ""}
    ${d.status !== "done" ? `<div class="card muted">${esc(d.status)}${d.message ? ": " + esc(d.message) : ""}</div>` : ""}
    ${items.map(card).join("") || '<div class="card muted">No results kept.</div>'}
    <p><a href="/backtests/sweep?job=${encodeURIComponent(id)}">Open on the old sweep page (full table, walk-forward) ›</a></p>`;
  document.querySelectorAll("[data-pk]").forEach(b => b.onclick = () => { PICKSAVE = +b.dataset.pk; sweepPage(id); });
  document.querySelectorAll("[data-pkno]").forEach(b => b.onclick = () => { PICKSAVE = null; sweepPage(id); });
  document.querySelectorAll("[data-pkok]").forEach(b => b.onclick = async () => {
    const it = items.find(x => x.n === +b.dataset.pkok);
    try {
      const j = await post("presets", {strategy: d.strategy, name: $("pkName").value, params: it.params, source: `From ${d.find_best ? "Find best" : "sweep"} ${id}, #${it.n}`});
      PICKSAVE = null;
      if (b.dataset.then) { PENDING = {kind: "backtest", preset: j.id}; QS = {...QS, strategy: undefined}; location.hash = `#/s/${encodeURIComponent(d.strategy)}/overview`; }
      else location.hash = `#/s/${encodeURIComponent(d.strategy)}/presets`;
    } catch (e) { $("pkErr").textContent = e.message; }
  });
}

// ---- Strategy Developer: build a strategy from blocks (algolab/built.py runs it) -----------------------------------
let BLD = null, BLDX = null, BLDNAME = "", BLDARM = false;
const T = (v, l) => [v, l];
const optsHtml = (cur, list) => list.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(cur) ? " selected" : ""}>${esc(l)}</option>`).join("");
const fsel = (id, cur, list, label) => `<div><label for="${id}">${label}</label><select id="${id}" data-b="1">${optsHtml(cur, list)}</select></div>`;
const finp = (id, val, label, mode) => `<div><label for="${id}">${label}</label><input id="${id}" data-b="1" value="${esc(val ?? "")}"${mode ? ` inputmode="${mode}"` : ""}></div>`;
async function buildPage(name) {
  if (!BLDX) BLDX = await api("builder");
  if (BLD === null || BLDNAME !== name) {
    BLDNAME = name;
    BLD = name ? (await api("built/" + encodeURIComponent(name))).spec : JSON.parse(JSON.stringify({...BLDX.templates.straddle, title: ""}));
    BLD.entry = {...BLDX.entry, ...(BLD.entry || {}), condition: {...BLDX.entry.condition, ...((BLD.entry || {}).condition || {})}};
    BLD.exit = {...BLDX.exit, ...(BLD.exit || {})};
    BLD.reentry = {on: false, max_entries: 2, ...(BLD.reentry || {})};
    BLD.legs = (BLD.legs || []).map(l => ({...BLDX.leg, ...l}));
    BLD.tpl = name ? "" : "straddle";
  }
  drawBuild();
}
function drawBuild() {
  const b = BLD, e = b.entry, c = e.condition, x = b.exit;
  const tpls = [["blank", "Blank"], ["straddle", "Straddle"], ["strangle", "Strangle"], ["iron_fly", "Iron fly"], ["spread", "Spread"], ["buy_move", "Buy on a move"]];
  $("app").innerHTML = `<a href="${BLDNAME ? "#/s/" + encodeURIComponent(BLDNAME) + "/overview" : "#/"}">‹ ${BLDNAME ? esc(b.title) : "Strategies"}</a>
    <div class="top" style="margin-top:6px"><h1>${BLDNAME ? "Edit strategy" : "New strategy"}</h1></div>
    <div class="card"><label for="bTitle" style="margin-top:0">Name</label><input id="bTitle" data-b="1" maxlength="60" value="${esc(b.title)}" placeholder="e.g. Expiry straddle 10% stop">
      ${BLDNAME ? "" : `<label>Start from</label><div class="chips" role="group" aria-label="Start from">${tpls.map(([k, l]) => `<button type="button" class="chip${b.tpl === k ? " on" : ""}" data-tpl="${k}" aria-pressed="${b.tpl === k}">${l}</button>`).join("")}</div>`}
      <label for="bDesc">Notes (optional)</label><input id="bDesc" data-b="1" maxlength="400" value="${esc(b.description || "")}" placeholder="What it is for"></div>
    <div class="card"><p class="h2"><span class="num">1</span> When to enter</p><div class="form">
      ${fsel("eDays", e.days, [T("every", "Every trading day"), T("expiry", "Expiry day only"), T("dte", "Days before expiry…")], "Days")}
      ${e.days === "dte" ? finp("eDte", e.dte, "Days before the nearest expiry", "numeric") : "<div></div>"}
      ${finp("eFrom", e.from, "From (IST)")}${finp("eUntil", e.until, "No new entries after")}
      ${fsel("cKind", c.kind, [T("none", "No condition: enter at From"), T("leg_multiple", "A leg's price reaches X× its price at From"), T("spot_move", "NIFTY moves X points from From"), T("vix", "Only if VIX is in a range")], "Condition")}
      ${c.kind === "leg_multiple" ? finp("cX", c.x, "X (2 = doubles, 0.5 = halves)", "decimal") : c.kind === "spot_move" ? finp("cPts", c.points, "Points", "numeric") + fsel("cDir", c.dir, [T("either", "Either way"), T("up", "Up"), T("down", "Down")], "Direction")
        : c.kind === "vix" ? finp("cVmin", c.vix_min, "VIX at least (0 = any)", "decimal") + finp("cVmax", c.vix_max, "VIX at most (0 = any)", "decimal") : ""}</div></div>
    <div class="card"><p class="h2"><span class="num">2</span> Legs</p>${b.legs.map((l, i) => `<div class="legc"><div class="row"><b>Leg ${i + 1}</b><button type="button" class="linkbtn" data-legdel="${i}">Remove</button></div><div class="form">
        ${fsel(`l${i}side`, l.side, [T("sell", "Sell"), T("buy", "Buy")], "Buy or sell")}${fsel(`l${i}right`, l.right, [T("CE", "Call (CE)"), T("PE", "Put (PE)")], "Option")}
        ${fsel(`l${i}exp`, l.expiry, [T("nearest", "Nearest"), T("next", "Next"), T("monthly", "Monthly")], "Expiry")}${finp(`l${i}lots`, l.lots, "Lots", "numeric")}
        ${fsel(`l${i}by`, l.strike_by, [T("atm", "ATM ± steps"), T("premium", "Premium near ₹"), T("points", "Points from spot")], "Strike by")}
        ${l.strike_by === "atm" ? finp(`l${i}steps`, l.steps, "Steps (+ = out of the money)", "numeric") : l.strike_by === "premium" ? finp(`l${i}prem`, l.premium, "Premium ₹", "decimal") : finp(`l${i}pts`, l.points, "Points (+ = out of the money)", "numeric")}
      </div></div>`).join("") || '<p class="muted">No legs yet.</p>'}
      ${b.legs.length < BLDX.max_legs ? '<button type="button" class="btn ghost" data-legadd="1" style="width:100%">+ Add leg</button>' : ""}</div>
    <div class="card"><p class="h2"><span class="num">3</span> Re-entry</p>
      <label><input type="checkbox" id="rOn" data-b="1"${b.reentry.on ? " checked" : ""}>After a leg's stop, enter it again when its price is back at its first entry price</label>
      ${b.reentry.on ? `<div class="form">${finp("rMax", b.reentry.max_entries, "Entries per leg (first + re-entries)", "numeric")}</div>` : ""}</div>
    <div class="card"><p class="h2"><span class="num">4</span> Exits</p><div class="form">
      ${fsel("xHold", x.hold, [T("intraday", "Intraday: square off every day"), T("expiry", "Hold to the expiry day")], "Hold")}${finp("xTime", x.time, "Square-off (IST)")}
      ${finp("xStop", x.leg_stop, "Stop per leg (0 = off)", "decimal")}${fsel("xUnit", x.leg_stop_unit, [T("pct", "% against entry"), T("pts", "points against entry")], "Stop in")}
      ${finp("xDayStop", x.day_stop, "Loss limit ₹ (0 = off)", "numeric")}${finp("xDayTgt", x.day_target, "Profit target ₹ (0 = off)", "numeric")}</div>
      <p class="muted" style="margin:8px 0 0">Trailing stops, whole-position limits and order types: Orders &amp; stops in a preset, as for any strategy.</p></div>
    <div class="card"><p class="h2">In words</p><p id="bDescribe" style="margin:0">…</p><div class="err" id="bErr" role="alert"></div>
      <p class="muted" style="margin:8px 0 0">Saved as a strategy spec, not code: the same engine runs it in backtests, paper and shadow. Live orders stay off for built strategies.</p></div>
    <div class="acts" style="grid-template-columns:1fr 1fr"><button type="button" class="btn ghost" data-bsave="draft">Save</button><button type="button" class="btn" data-bsave="backtest">Save &amp; backtest</button></div>
    ${BLDNAME ? `<button type="button" class="btn danger${BLDARM ? " armed" : ""}" data-bdel="1" style="width:100%;margin-top:10px">${BLDARM ? "Tap again to delete" : "Delete strategy"}</button>` : ""}`;
  wireBuild();
  checkBuild();
}
function readBuild() {
  const b = BLD, v = id => $(id) ? $(id).value : undefined, set = (o, k, id) => { const x = v(id); if (x !== undefined) o[k] = x; };
  set(b, "title", "bTitle"); set(b, "description", "bDesc");
  set(b.entry, "days", "eDays"); set(b.entry, "dte", "eDte"); set(b.entry, "from", "eFrom"); set(b.entry, "until", "eUntil");
  const c = b.entry.condition; set(c, "kind", "cKind"); set(c, "x", "cX"); set(c, "points", "cPts"); set(c, "dir", "cDir"); set(c, "vix_min", "cVmin"); set(c, "vix_max", "cVmax");
  b.legs.forEach((l, i) => { set(l, "side", `l${i}side`); set(l, "right", `l${i}right`); set(l, "expiry", `l${i}exp`); set(l, "lots", `l${i}lots`);
    set(l, "strike_by", `l${i}by`); set(l, "steps", `l${i}steps`); set(l, "premium", `l${i}prem`); set(l, "points", `l${i}pts`); });
  if ($("rOn")) b.reentry.on = $("rOn").checked; set(b.reentry, "max_entries", "rMax");
  const x = b.exit; set(x, "hold", "xHold"); set(x, "time", "xTime"); set(x, "leg_stop", "xStop"); set(x, "leg_stop_unit", "xUnit"); set(x, "day_stop", "xDayStop"); set(x, "day_target", "xDayTgt");
}
let _chk = 0;
async function checkBuild() {
  const n = ++_chk, j = await post("built/check", {spec: BLD}).catch(e => ({ok: false, error: e.message}));
  if (n !== _chk || !$("bDescribe")) return;
  $("bDescribe").textContent = j.ok ? j.describe : "";
  $("bErr").textContent = j.ok ? "" : j.error;
}
function wireBuild() {
  document.querySelectorAll("[data-b]").forEach(el => {
    el.oninput = () => { readBuild(); checkBuild(); };
    el.onchange = () => { readBuild(); if (el.tagName === "SELECT" || el.type === "checkbox") drawBuild(); else checkBuild(); };
  });
  document.querySelectorAll("[data-tpl]").forEach(el => el.onclick = () => {
    readBuild();
    const t = JSON.parse(JSON.stringify(BLDX.templates[el.dataset.tpl]));
    BLD = {title: BLD.title, description: BLD.description, tpl: el.dataset.tpl,
           entry: {...BLDX.entry, ...(t.entry || {}), condition: {...BLDX.entry.condition, ...((t.entry || {}).condition || {})}},
           exit: {...BLDX.exit, ...(t.exit || {})}, reentry: {on: false, max_entries: 2, ...(t.reentry || {})},
           legs: (t.legs || []).map(l => ({...BLDX.leg, ...l}))};
    drawBuild();
  });
  document.querySelectorAll("[data-legdel]").forEach(el => el.onclick = () => { readBuild(); BLD.legs.splice(+el.dataset.legdel, 1); drawBuild(); });
  const add = document.querySelector("[data-legadd]");
  if (add) add.onclick = () => { readBuild(); BLD.legs.push({...BLDX.leg, ...(BLD.legs.length ? {right: BLD.legs[BLD.legs.length - 1].right === "CE" ? "PE" : "CE"} : {})}); drawBuild(); };
  document.querySelectorAll("[data-bsave]").forEach(el => el.onclick = async () => {
    readBuild(); el.disabled = true;
    try {
      const j = await post("built", {spec: BLD, name: BLDNAME});
      const then = el.dataset.bsave;
      BLD = null; BLDNAME = ""; S = null;
      if (then === "backtest") { PENDING = {kind: "backtest", preset: ""}; QS = {...QS, strategy: undefined}; }
      location.hash = `#/s/${encodeURIComponent(j.name)}/overview`;
    } catch (e) { $("bErr").textContent = e.message; el.disabled = false; }
  });
  const del = document.querySelector("[data-bdel]");
  if (del) del.onclick = async () => {
    if (!BLDARM) { BLDARM = true; drawBuild(); setTimeout(() => { if (BLDARM) { BLDARM = false; if ($("bTitle")) drawBuild(); } }, 4000); return; }
    BLDARM = false;
    try { await post("built/delete", {name: BLDNAME}); BLD = null; BLDNAME = ""; location.hash = "#/"; }
    catch (e) { $("bErr").textContent = e.message; }
  };
}

// ---- Analysis and stage gates -------------------------------------------------------------------------------------
const mark = ok => ok === true ? '<span class="gk ok" aria-label="passed">✓</span>' : ok === false ? '<span class="gk no" aria-label="not yet">✕</span>'
  : '<span class="gk unk" aria-label="not known yet">?</span>';
function gatesCard(g, short) {
  if (!g || !g.next) return g && !g.next ? '<div class="card"><p class="h2">Stage gates</p><p class="muted" style="margin:0">Live: the last stage.</p></div>' : "";
  return `<div class="card"><div class="row"><p class="h2" style="margin:0">Before ${esc(g.next)}</p>
      <span class="pill ${g.ready ? "Paper" : "Draft"}">${g.ready ? "Ready" : g.passed + " of " + g.total}</span></div>
    ${g.checks.map(c => `<div class="gate">${mark(c.ok)}<span>${esc(c.name)}${c.note ? `<div class="muted">${esc(c.note)}</div>` : ""}</span></div>`).join("")}
    <p class="muted" style="margin:8px 0 0">${short ? `Advice only; nothing is blocked. <a href="#/s/${encodeURIComponent(S.strategy.name)}/analysis">Analysis ›</a>` : "Advice only; nothing is blocked. Limits: config.yaml › studio › gates."}</p></div>`;
}
const num = v => v == null ? '<span class="muted">–</span>' : `<span class="${cls(v)}">${inr(v)}</span>`;
function fig(f) {
  const v = f.value, money = typeof v === "number" && /P&L|Charges|drawdown$|reshuffles|margin$|Worst day/.test(f.label);
  return `<div class="tile"><span class="muted">${esc(f.label)}</span><b class="${money ? cls(v) : ""}">${money ? inr(v) : esc(typeof v === "number" ? v.toLocaleString("en-IN") : v)}</b></div>`;
}
function monthsChart(ms) {
  if (!ms.length) return "";
  const top = Math.max(...ms.map(m => Math.abs(m.net)), 1);
  return `<p class="h2" style="margin-top:14px">By month</p><div class="months" role="img" aria-label="Net P&L by month">${ms.map(m => {
    const h = Math.max(2, Math.round(Math.abs(m.net) / top * 48));
    return `<div class="mo" title="${esc(m.month)}: ${esc(inr(m.net))}"><div class="half">${m.net >= 0 ? `<i class="mbar up" style="height:${h}px"></i>` : ""}</div>
      <div class="half dn">${m.net < 0 ? `<i class="mbar down" style="height:${h}px"></i>` : ""}</div></div>`; }).join("")}</div>
    <div class="row muted" style="font-size:12px"><span>${esc(ms[0].month)}</span><span>${ms.filter(m => m.net > 0).length} of ${ms.length} months made money</span><span>${esc(ms[ms.length - 1].month)}</span></div>`;
}
const TABS_EXTRA = {};
TABS_EXTRA.analysis = function () {
  const a = AN || {}, t = a.totals || {}, rb = a.robust;
  const kite = (a.sessions || []).filter(x => x.feed === "kite");
  const sel = (id, cur, opts) => `<select id="${id}">${opts.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(cur) ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  return `<div class="form" style="margin-bottom:12px"><div><label for="anPreset" style="margin-top:0">Preset</label>${sel("anPreset", AN_PRESET, [["", "All presets"], ...S.presets.map(p => [p.id, p.name])])}</div>
      <div><label for="anDays" style="margin-top:0">Days</label>${sel("anDays", AN_DAYS, [[5, "Last 5 trading days"], [10, "Last 10"], [20, "Last 20"], [60, "Last 60"]])}</div></div>
    ${gatesCard(a.gates, false)}
    <div class="card"><p class="h2">Same days, three ways</p>${(a.days || []).length ? `<table class="tbl"><thead><tr><th scope="col">Day</th><th scope="col">Backtest</th><th scope="col">Paper</th><th scope="col">Shadow</th></tr></thead>
      <tbody>${a.days.map(d => `<tr><td>${day(d.day)}</td><td>${num(d.backtest)}</td><td>${num(d.paper)}</td><td>${num(d.shadow)}</td></tr>`).join("")}</tbody>
      <tfoot><tr><th scope="row">Total</th><td>${num(t.backtest)}</td><td>${num(t.paper)}</td><td>${num(t.shadow)}</td></tr></tfoot></table>
      <p class="muted" style="margin:8px 0 0">Closed trades by exit day. The backtest column fills in once a session is compared (below, or the Saturday report).</p>`
      : '<p class="muted" style="margin:0">No paper or shadow trades yet.</p>'}</div>
    <div class="card"><p class="h2">What the gap is made of</p>${(a.gap || []).map(g => {
        const behind = g.backtest ? Math.round((g.backtest - g.paper) / Math.abs(g.backtest) * 100) : null;
        return `<div class="gapc"><div class="row"><b>${esc(g.label)}</b><span class="pill ${g.mode === "shadow" ? "Shadow" : "Paper"}">${g.mode === "shadow" ? "Shadow" : "Paper"}</span></div>
        <div class="muted">${day(g.from)} – ${day(g.to)}${g.run ? ` · <a href="/backtests?run=${encodeURIComponent(g.run)}">the backtest ›</a>` : ""}</div>
        <div class="line"><span>${g.mode === "shadow" ? "Shadow" : "Paper"} vs backtest</span><span>${num(g.paper)} vs ${num(g.backtest)}${behind != null ? ` <span class="muted">(${behind > 0 ? behind + "% behind" : -behind + "% ahead"})</span>` : ""}</span></div>
        <div class="line"><span>Fills vs backtest, per unit</span><span>${g.avg_fill == null ? "–" : (g.avg_fill > 0 ? "₹" + g.avg_fill.toFixed(2) + " dearer" : "₹" + (-g.avg_fill).toFixed(2) + " cheaper")} <span class="muted">(${g.matched || 0} matched)</span></span></div>
        <div class="line"><span>Orders only in ${g.mode} / only in backtest</span><span>${g.paper_only ?? 0} / ${g.backtest_only ?? 0}</span></div></div>`; }).join("")
      || '<p class="muted" style="margin:0 0 8px">No comparison yet.</p>'}
      ${(a.jobs || []).filter(j => j.status === "queued" || j.status === "running" || j.status === "failed").slice(0, 3).map(j =>
        `<p class="muted" style="margin:6px 0">${j.status === "failed" ? "✕ Compare failed: " + esc(j.message || "") : "⏳ Comparing " + esc(j.session) + " (" + esc(j.status) + ")…"}</p>`).join("")}
      ${ME.can_use ? (kite.length ? `<div class="form"><div><label for="cmpS">Compare a session</label>${sel("cmpS", CMP.session || kite[0].id, kite.map(x => [x.id, x.label + " · " + x.mode]))}</div>
        <div><label for="cmpD">Over</label>${sel("cmpD", CMP.days, [[7, "Last 7 days"], [14, "Last 14 days"], [30, "Last 30 days"]])}</div></div>
        <div class="err" id="cmpErr" role="alert"></div><button type="button" class="btn ghost" data-cmp="1" style="width:100%;margin-top:10px">Compare with a backtest</button>
        <p class="muted" style="margin:6px 0 0">Backtests the same days with the session's settings on the prices the server recorded; takes a few minutes.</p>`
        : '<p class="muted" style="margin:0">Start a paper session on Zerodha live prices to compare it with a backtest.</p>') : ""}</div>
    <div class="card"><p class="h2">How sure: latest backtest${rb && rb.preset ? " · " + esc(rb.preset) : ""}</p>${rb ? `<p class="muted" style="margin:0 0 10px">${day(rb.from)} – ${day(rb.to)} · <a href="/backtests?run=${encodeURIComponent(rb.run)}">open ›</a></p>
      <div class="tiles">${rb.figs.map(fig).join("")}</div>${monthsChart(rb.months || [])}` : '<p class="muted" style="margin:0">No backtest yet.</p>'}</div>`;
};

const TABS = {
  overview() {
    const s = S.strategy, cur = STAGES.indexOf(s.stage), running = S.sessions.filter(x => x.running);
    return `<div class="card"><p class="h2">Stage</p><div class="steps">${STAGES.map((st, i) =>
        `<div><span class="dot ${i < cur ? "done" : i === cur ? "now" : ""}" aria-hidden="true">${i < cur ? "✓" : i + 1}</span><div style="margin-top:4px;${i === cur ? "font-weight:700" : "color:var(--muted)"}">${st}</div></div>`).join("")}</div>
        <p class="muted" style="margin:10px 0 0">${esc(NEXT[s.stage])}.</p></div>
      ${gatesCard(S.gates, true)}
      ${startCard()}
      <div class="tiles">
        <div class="tile"><span class="muted">Last backtest</span><b class="${cls((s.backtest || {}).net)}">${inr((s.backtest || {}).net)}</b><span class="muted">${s.backtest ? "DD " + inr(s.backtest.max_dd) + " · " + (s.backtest.cycles ?? "–") + " cycles" : "none yet"}</span></div>
        ${["paper", "shadow", "live"].map(m => `<div class="tile"><span class="muted">${m[0].toUpperCase() + m.slice(1)}</span><b class="${cls((s[m] || {}).total)}">${s[m] ? inr(s[m].total) : "–"}</b><span class="muted">${s[m] ? s[m].sessions + " session" + (s[m].sessions === 1 ? "" : "s") + " · " + s[m].days + " days" : "not run"}</span></div>`).join("")}
      </div>
      <div class="card"><p class="h2">Running now</p>${running.length ? running.map(sessLine).join("") : '<p class="muted" style="margin:0">Nothing running.</p>'}</div>`;
  },
  presets() {
    if (EDIT) return editor();
    if (SHARE) return sharePanel();
    return S.presets.map(p => `<div class="card"><div class="row"><span class="nm">${esc(p.name)}</span><span class="muted">v${p.version}</span></div>
        <div class="muted">${esc(p.source)} · ${day(p.updated)}${p.by ? " · " + esc(p.by) : ""}</div><div class="set">${esc(described(p))}</div>${sharedNote(p)}
        <div class="acts" style="margin-top:10px">${[["backtest", "Backtest"], ["paper", "Paper"], ...(ME.owner || ME.live_use ? [["shadow", "Shadow"]] : [])].map(([k, t]) =>
            `<button type="button" class="btn ghost" data-with="${esc(p.id)}" data-as="${k}">${t}</button>`).join("")}
          ${ME.can_use ? `<button type="button" class="btn ghost" data-edit="${esc(p.id)}">Edit</button><button type="button" class="btn ghost" data-dup="${esc(p.id)}">Duplicate</button><button type="button" class="btn ghost" data-share="${esc(p.id)}">Share</button>
          <button type="button" class="btn danger${ARM[p.id] ? " armed" : ""}" data-del="${esc(p.id)}">${ARM[p.id] ? "Tap again" : "Delete"}</button>` : ""}</div></div>`).join("")
      + (ME.can_use ? `<button type="button" class="btn" data-new="1" style="width:100%">New preset</button>` : "")
      + `<p class="muted">A preset is a named set of this strategy's settings and its Orders &amp; stops. Backtests and sessions started with one show its name.</p>`;
  },
  backtests() {
    return resultsBody(S.strategy.name) + `<button type="button" class="btn ghost" data-with="" data-as="backtest" style="width:100%;margin-top:4px">Run a backtest</button>`;
  },
  sessions() {
    return `<div class="card"><p class="h2">Sessions</p>${S.sessions.length ? S.sessions.map(sessLine).join("") : '<p class="muted" style="margin:0">No paper, shadow or live sessions yet.</p>'}</div>`;
  },
};
function editor() {
  const p = EDIT, val = k => (p.params || {})[k] ?? (S.catalog.params.find(x => x.key === k) || {}).default;
  const row = d => {
    const id = "e_" + d.key, v = val(d.key);
    if (d.type === "bool") return `<div><label><input type="checkbox" id="${esc(id)}"${v ? " checked" : ""}>${esc(d.label)}</label></div>`;
    if (d.choices) return `<div><label for="${esc(id)}">${esc(d.label)}</label><select id="${esc(id)}">${d.choices.map(c => `<option${c == v ? " selected" : ""}>${esc(c)}</option>`).join("")}</select></div>`;
    return `<div><label for="${esc(id)}">${esc(d.label)}</label><input id="${esc(id)}" value="${esc(v)}"${d.type === "int" || d.type === "float" ? ' inputmode="decimal"' : ""}></div>`;
  };
  const own = S.catalog.params.filter(d => d.group !== "risk"), risk = S.catalog.params.filter(d => d.group === "risk");
  return `<div class="card"><p class="h2">${p.id ? "Edit preset" : "New preset"}</p>
      <label for="pname" style="margin-top:0">Name</label><input id="pname" value="${esc(p.name || "")}" maxlength="40" placeholder="e.g. Trail 10">
      <div class="form">${own.map(row).join("")}</div>
      <details style="margin-top:12px"><summary>Orders &amp; stops</summary><div class="form">${risk.map(row).join("")}</div></details>
      <div class="err" id="perr" role="alert"></div>
      <div class="acts" style="grid-template-columns:1fr 1fr;margin-top:12px"><button type="button" class="btn ghost" data-cancel="1">Cancel</button><button type="button" class="btn" data-save="1">Save preset</button></div></div>`;
}
function draw(r) {
  const s = S.strategy;
  $("app").innerHTML = `<a href="#/">‹ Strategies</a>
    <div class="top" style="margin:6px 0 2px"><h1>${esc(s.title)}</h1><span class="pill ${s.stage}">${s.stage}</span></div>
    <p class="muted" style="margin:0">${esc(s.description)}</p>
    ${s.built ? `<p style="margin:6px 0 0"><span class="pill Draft">Built in Studio</span> ${ME.can_use ? `<a href="#/build/${encodeURIComponent(s.name)}">Edit in Strategy Developer ›</a>` : ""}</p>` : ""}
    <nav class="tabs" aria-label="${esc(s.title)}">${[["overview", "Overview"], ["presets", "Presets"], ["backtests", "Backtests"], ["sessions", "Sessions"], ["analysis", "Analysis"]]
      .map(([k, t]) => `<a href="#/s/${encodeURIComponent(s.name)}/${k}" class="${r.tab === k ? "on" : ""}"${r.tab === k ? ' aria-current="page"' : ""}>${t}</a>`).join("")}</nav>
    <div>${(TABS[r.tab] || TABS_EXTRA[r.tab] || TABS.overview)()}</div>`;
  wire(r);
}
function collect() {
  const params = {};
  for (const d of S.catalog.params) { const el = $("e_" + d.key); if (el) params[d.key] = el.type === "checkbox" ? el.checked : el.value; }
  return params;
}
function wire(r) {
  const on = (sel, fn) => document.querySelectorAll(sel).forEach(el => el.onclick = () => fn(el));
  on("[data-share]", async b => { SHARE = {pid: b.dataset.share, backtest: true, results: false}; await loadShares(); draw(r); window.scrollTo(0, 0); });
  on("[data-shclose]", () => { SHARE = null; draw(r); });
  on("[data-shmake]", async b => {
    SHARE = {...SHARE, backtest: $("shBt").checked, results: $("shRes").checked, to: $("shTo").value};
    b.disabled = true;
    try { const j = await post("share", {strategy: S.strategy.name, preset: SHARE.pid, backtest: SHARE.backtest, results: SHARE.results, to: SHARE.to});
          SHARE.code = j.code; SHARE.file = j.file; SHARE.nobt = SHARE.backtest && !j.payload.backtest; await loadShares(); draw(r); }
    catch (e) { $("shErr").textContent = e.message; b.disabled = false; }
  });
  on("[data-shcopy]", async () => {
    try { await navigator.clipboard.writeText(SHARE.code); } catch (e) { $("shCode").select(); document.execCommand("copy"); }
    SHARE.copied = true; draw(r);
  });
  on("[data-shsend]", () => { navigator.share({title: "Algo Studio strategy", text: SHARE.code}).catch(() => {}); });
  on("[data-shfile]", () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([SHARE.code], {type: "text/plain"}));
    a.download = SHARE.file || "strategy.algoshare.txt"; document.body.appendChild(a); a.click(); a.remove();
  });
  on("[data-shforget]", async b => { try { await post("shares/delete", {id: b.dataset.shforget}); } catch (e) { /* gone already */ } await loadShares(); draw(r); });
  on("[data-with]", b => {
    if (QS.strategy !== S.strategy.name) startCard();          // this strategy's starting choices first
    QS = {...QS, preset: b.dataset.with || QS.preset, kind: b.dataset.as, lots: "", done: ""};
    location.hash = `#/s/${encodeURIComponent(S.strategy.name)}/overview`; });
  if ($("anPreset")) $("anPreset").onchange = () => { AN_PRESET = $("anPreset").value; strategy(r); };
  if ($("anDays")) $("anDays").onchange = () => { AN_DAYS = +$("anDays").value; strategy(r); };
  on("[data-cmp]", async b => {
    CMP = {session: $("cmpS").value, days: +$("cmpD").value};
    b.disabled = true;
    try { await post("compare", {strategy: S.strategy.name, session: CMP.session, days: CMP.days}); await strategy(r); }
    catch (e) { $("cmpErr").textContent = e.message; b.disabled = false; }
  });
  on("[data-kind]", b => { readQuick(); QS.kind = b.dataset.kind; QS.done = ""; draw(r); });
  on("[data-quick]", b => { b.disabled = true; quick(r).finally(() => { b.disabled = false; }); });
  if ($("usePreset")) $("usePreset").onchange = () => { readQuick(); QS.lots = ""; QS.done = ""; draw(r); };
  on("[data-keep]", b => { KEEP = b.dataset.keep; draw(r); $("keepName").focus(); });
  on("[data-keepno]", () => { KEEP = null; draw(r); });
  on("[data-keepok]", async b => {
    try { await post("presets/from-run", {run: b.dataset.keepok, name: $("keepName").value}); KEEP = null; location.hash = `#/s/${encodeURIComponent(S.strategy.name)}/presets`; }
    catch (e) { $("keepErr").textContent = e.message; }
  });
  on("[data-new]", () => { EDIT = {name: "", params: {}}; draw(r); });
  on("[data-edit]", b => { EDIT = JSON.parse(JSON.stringify(S.presets.find(p => p.id === b.dataset.edit))); draw(r); });
  on("[data-dup]", b => { const p = S.presets.find(x => x.id === b.dataset.dup); EDIT = {name: p.name + " copy", params: {...p.params}, source: "Copy of " + p.name}; draw(r); });
  on("[data-cancel]", () => { EDIT = null; draw(r); });
  on("[data-del]", async b => {
    const id = b.dataset.del;
    if (!ARM[id]) { ARM[id] = 1; draw(r); setTimeout(() => { if (ARM[id]) { delete ARM[id]; draw(route() || r); } }, 4000); return; }
    delete ARM[id];
    try { await post("presets/delete", {strategy: S.strategy.name, id}); } catch (e) { alert(e.message); }
    show();
  });
  on("[data-save]", async () => {
    try {
      await post("presets", {strategy: S.strategy.name, id: EDIT.id || "", name: $("pname").value, params: collect(), source: EDIT.source});
      EDIT = null; await show();
    } catch (e) { $("perr").textContent = e.message; }
  });
  if (r.tab === "backtests") wireResults(() => draw(r));      // the grouped results (overrides the older handlers above)
}
async function strategy(r) {
  S = await api("strategies/" + encodeURIComponent(r.name));
  if (r.tab === "backtests") { if (RS.s !== r.name) { RS = {...RS, s: r.name, p: null}; SEL.clear(); } await loadResults(); }
  if (r.tab === "analysis") {
    if (AN_PRESET && !S.presets.some(p => p.id === AN_PRESET)) AN_PRESET = "";
    AN = await api(`strategies/${encodeURIComponent(r.name)}/analysis?days=${AN_DAYS}` + (AN_PRESET ? `&preset=${encodeURIComponent(AN_PRESET)}` : ""));
  }
  if (!BT) BT = await fetch("/api/backtest-runner", {cache: "no-store"}).then(x => x.ok ? x.json() : {}).catch(() => ({}));
  document.title = S.strategy.title + " · Algo Studio";
  draw(r);
}

async function show() {
  const r = route();
  document.querySelectorAll(".barlinks a").forEach(a => a.classList.toggle("on", a.getAttribute("href") === (r && ["results", "run", "compare", "sweep"].includes(r.page) ? "#/results" : "#/")));
  try {
    if (r && r.page === "run") { document.title = "Result · Algo Studio"; await runPage(r.name); }
    else if (r && r.page === "compare") { document.title = "Compare · Algo Studio"; await comparePage(r.runs); }
    else if (r && r.page === "sweep") { document.title = "Sweep · Algo Studio"; await sweepPage(r.id); }
    else if (r && r.page === "results") { document.title = "Results · Algo Studio"; await resultsPage(r); }
    else if (r && r.page === "import") { document.title = "Import · Algo Studio"; importPage(); }
    else if (r && r.page === "build") { document.title = "Strategy Developer · Algo Studio"; await buildPage(r.name); }
    else if (r) await strategy(r); else { document.title = "Algo Studio"; await list(); }
  } catch (e) { $("app").innerHTML = `<div class="card down">${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", () => { EDIT = null; ARM = {}; KEEP = null; SHARE = null; show(); window.scrollTo(0, 0); });
api("me").then(m => { ME = m; show(); }).catch(e => { $("app").innerHTML = `<div class="card down">${esc(e.message)}</div>`; });
