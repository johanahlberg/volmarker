"use strict";
// VolMarker front end - plain JS, no dependencies.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const clone = (x) => JSON.parse(JSON.stringify(x));
const STATE_KEYS = ["pair", "mode", "bases", "key", "skew_expiry", "weights", "atm_type", "lock_rr10f", "lock_bf10f",
  "follow_market", "alert_tol", "alerts_off", "group", "spread_ref", "follow_parent", "cut", "dc_factor", "excel", "rr_put", "follow_bases", "tenors"];
// pair settings the Setup tab saves on its own (service.SETTING_KEYS)
const SETUP_KEYS = ["mode", "bases", "key", "group", "spread_ref", "follow_parent", "follow_bases", "cut", "dc_factor",
  "atm_type", "follow_market", "excel", "alert_tol", "alerts_off"];
const MKT_FIELDS = { atm: "atm", rr25: "rr25", rr10: "rr10", bf25: "bf25", bf10: "bf10" };
const FOLLOW_FIELDS = ["atm", "rr25", "rr10f", "rr10", "bf25", "bf10f", "bf10"];   // owned by the feed on Follow FMD pairs
// marks from outside VolMarker (surface.external); a derived pair's correlation is then implied by them
const external = (d = app.draft) => !!(d.excel || d.follow_market);
// a derived pair whose ATMs move with the bases at a fixed correlation (else the correlation follows)
const movesWithBases = (d = app.draft) => d.mode === "derived" && d.follow_bases && !external(d);
// "15:00, Fri 12:00 Jerusalem": a cut with its weekday exceptions (config cuts.<name>.times)
const cutLabel = (c) => [c.time, ...Object.entries(c.times || {}).map(([d, t]) => `${d} ${t}`)].join(", ") +
  " " + c.tz.split("/").pop().replace("_", " ");
const MARK_LABEL = { atm: "ATM", rr25: "25D RR", rr10: "10D RR", bf25: "25D BF", bf10: "10D BF" };
// favour puts (pair setting rr_put): the GUI shows and takes RRs as put − call; stored and exported RRs stay call − put
const RR_FIELDS = ["rr25", "rr10", "skew_rr"];
function rrSign(f, put = app.draft?.rr_put) { return put && RR_FIELDS.includes(f) ? -1 : 1; }
function rrFlip(q) {               // a call − put quote in put − call terms: bid and offer swap
  const neg = (v) => (v == null ? v : -v);
  return { ...q, bid: neg(q.offer), offer: neg(q.bid), mid: neg(q.mid) };
}
const DEFAULT_WEIGHTS = { weekdays: [1, 1, 1, 1, 1, 0, 0], holiday1: 0.4, holiday2: 0.4, events: [] };
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
function normaliseWeights(w) {     // as calendar.normalise_weights: old weekend1/weekend2 -> weekday weights
  const out = { ...clone(DEFAULT_WEIGHTS), ...(w || {}) };
  if (!(w?.weekdays?.length === 7) && w && ("weekend1" in w || "weekend2" in w)) {
    const we = Math.min(+w.weekend1 || 0, +w.weekend2 || 0);
    out.weekdays = [1, 1, 1, 1, 1, we, we];
  }
  delete out.weekend1; delete out.weekend2;
  return out;
}

const app = {
  me: null, tenors: [], pairs: [], current: null,
  view: null,        // server view of the published version
  baseVersion: null, // version the draft is based on
  draft: null, preview: null, dirty: false, tab: "surface",
  alerts: [], alertCfg: null,
  prefs: {},         // this user's own settings (sidebar layout, spread references), saved on the shared drive
};

// ---------------------------------------------------------------- api
async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const data = await r.json();
  if (!r.ok) { const e = new Error(data.error || r.statusText); e.status = r.status; e.data = data; throw e; }
  return data;
}

// ---------------------------------------------------------------- formatting
function fmt(v, dp = 3) {
  if (v === null || v === undefined || v === "") return "";
  return Number(v).toFixed(dp);
}
function age(min) {
  if (min < 1) return "now";
  if (min < 60) return Math.round(min) + "m";
  if (min < 60 * 48) return Math.round(min / 60) + "h";
  return Math.round(min / 1440) + "d";
}
function localTime(iso) {
  const d = new Date(iso);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function esc(s) { return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

// ---------------------------------------------------------------- user preferences
let prefsTimer = null;
function savePrefs() {
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(() => api("/api/prefs", app.prefs).catch(() => {}), 400);
}

// ---------------------------------------------------------------- sidebar
// The group is a pair setting (shared); the order of groups and pairs and which groups are folded are per user.
const UNGROUPED = "Ungrouped";
const groupOf = (p) => p.group || UNGROUPED;
function rankBy(list) { return (x) => { const i = (list || []).indexOf(x); return i < 0 ? 1e9 : i; }; }
function orderedGroups(rows) {
  const r = rankBy(app.prefs.group_order);
  return [...new Set(rows.map(groupOf))].sort((a, b) =>
    r(a) - r(b) || (a === UNGROUPED) - (b === UNGROUPED) || a.localeCompare(b));
}
function orderedPairs(group, rows) {
  const r = rankBy((app.prefs.pair_order || {})[group]);
  return rows.filter((p) => groupOf(p) === group).sort((a, b) => r(a.pair) - r(b.pair) || a.pair.localeCompare(b.pair));
}
let sbDrag = null, sbPending = false;
function renderPairs(flashPair) {
  if (sbDrag) { sbPending = true; return; }       // don't rebuild under the mouse while dragging
  const q = $("#filter").value.trim().toUpperCase();
  const rows = app.pairs.filter((p) => !q || p.pair.includes(q));
  const folded = app.prefs.collapsed || {};
  const html = orderedGroups(rows).map((g) => {
    const ps = orderedPairs(g, rows), fold = !q && folded[g] && !ps.some((p) => p.pair === flashPair);
    const nStale = ps.filter((p) => p.stale).length, nAl = ps.reduce((n, p) => n + p.alerts, 0);
    const nNew = ps.reduce((n, p) => n + p.alerts_new, 0);
    const sum = fold ? (nStale ? `<span class="tag stale" title="${nStale} stale">${nStale} stale</span>` : "") +
      (nAl ? `<span class="tag alert${nNew ? "" : " acked"}" title="${nAl} market alert(s)">⚠${nAl}</span>` : "") +
      (ps.some((p) => p.pair === app.current) ? `<span class="tag" title="The selected pair is in this group">●</span>` : "") : "";
    return `<div class="gblock" data-group="${esc(g)}"><div class="group${fold ? " folded" : ""}" draggable="true" data-group="${esc(g)}" title="Click to ${fold ? "unfold" : "fold"} · drag to move">` +
      `<span class="caret">▾</span>${esc(g)} <span class="cnt">${ps.length}</span>${sum}</div>` +
      (fold ? "" : ps.map((p) => pairRow(p, flashPair)).join("")) + `</div>`;
  }).join("");
  $("#pairlist").innerHTML = html || `<div class="muted" style="padding:8px">No pairs</div>`;
}
function pairRow(p, flashPair) {
  const tags = [
    p.key ? `<span class="tag key" title="Key pair">K</span>` : "",
    p.mode === "derived" ? `<span class="tag" title="Derived from ${esc((p.bases || []).join(" × "))}">D</span>` : "",
    p.skew ? `<span class="tag" title="Skew active in Fenics">skew</span>` : "",
    p.stale ? `<span class="tag stale" title="Not marked for ${age(p.age_minutes)}">stale</span>` : "",
    p.warnings ? `<span class="tag" title="${p.warnings} check(s)">!${p.warnings}</span>` : "",
    p.follow ? `<span class="tag follow ${p.follow}" title="Follows FMD (${p.follow})">FMD</span>` : "",
    p.follow_parent ? `<span class="tag" title="Moves with ${esc(p.spread_ref)}">P</span>` : "",
    p.excel ? `<span class="tag xl${p.excel_error ? " bad" : ""}" title="${esc(p.excel_error ? "Excel file not readable: " + p.excel_error : "Marked in Excel, read from its export file")}">XL</span>` : "",
    p.excel_foreign ? `<span class="tag xl bad" title="Its export file was changed outside VolMarker: is it still marked in Excel?">XL?</span>` : "",
    p.alerts ? `<span class="tag alert${p.alerts_new ? "" : " acked"}" title="${p.alerts} market alert(s)${p.alerts_new ? "" : ", acknowledged"}">⚠${p.alerts}</span>` : "",
  ].join("");
  const ageTitle = `Marked ${localTime(p.marked_ts)} · last update ${localTime(p.ts)} by ${p.user}`;
  return `<div class="prow${p.pair === app.current ? " sel" : ""}${p.pair === flashPair ? " flash" : ""}" data-pair="${p.pair}" draggable="true">
    <span><span class="name">${p.pair}</span>${tags}</span>
    <span class="atm" title="1M ATM">${fmt(p.atm_1m, 2)}</span>
    <span class="age" title="${esc(ageTitle)}">${age(p.age_minutes)}</span></div>`;
}
function wireSidebar() {
  const list = $("#pairlist");
  const clearMarks = () => $$(".drop-before, .drop-after", list).forEach((el) => el.classList.remove("drop-before", "drop-after"));
  const target = (e) => {   // element to drop next to, and on which side
    if (!sbDrag) return null;
    const el = sbDrag.type === "group" ? e.target.closest(".gblock")
      : e.target.closest(`.gblock[data-group="${CSS.escape(sbDrag.group)}"] .prow`);
    if (!el || el.dataset[sbDrag.type === "group" ? "group" : "pair"] === sbDrag.id) return null;
    const r = el.getBoundingClientRect();
    const mid = sbDrag.type === "group" ? r.top + Math.min(r.height / 2, 14) : r.top + r.height / 2;
    return { el, after: e.clientY > mid };
  };
  list.addEventListener("dragstart", (e) => {
    const g = e.target.closest?.(".group"), r = e.target.closest?.(".prow");
    if (g) sbDrag = { type: "group", id: g.dataset.group };
    else if (r) sbDrag = { type: "pair", id: r.dataset.pair, group: r.closest(".gblock").dataset.group };
    else return;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", sbDrag.id);
    (g || r).classList.add("dragging");
  });
  list.addEventListener("dragover", (e) => {
    const t = target(e); clearMarks();
    if (!t) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    t.el.classList.add(t.after ? "drop-after" : "drop-before");
  });
  list.addEventListener("drop", (e) => {
    const t = target(e); clearMarks();
    if (!t) return;
    e.preventDefault();
    const q = $("#filter").value.trim().toUpperCase();
    if (sbDrag.type === "group") {
      const order = orderedGroups(app.pairs).filter((g) => g !== sbDrag.id);
      order.splice(order.indexOf(t.el.dataset.group) + (t.after ? 1 : 0), 0, sbDrag.id);
      app.prefs.group_order = order;
    } else {
      const g = sbDrag.group;
      const order = orderedPairs(g, app.pairs).map((p) => p.pair).filter((p) => p !== sbDrag.id);
      order.splice(order.indexOf(t.el.dataset.pair) + (t.after ? 1 : 0), 0, sbDrag.id);
      (app.prefs.pair_order ||= {})[g] = order;
    }
    savePrefs();
  });
  list.addEventListener("dragend", () => {
    clearMarks(); sbDrag = null;
    sbPending = false; renderPairs();
  });
  list.addEventListener("click", (e) => {
    const g = e.target.closest(".group");
    if (g) {
      if ($("#filter").value.trim()) return;      // all groups are open while filtering
      const c = (app.prefs.collapsed ||= {});
      if (c[g.dataset.group]) delete c[g.dataset.group]; else c[g.dataset.group] = true;
      savePrefs(); renderPairs(); return;
    }
    const r = e.target.closest(".prow"); if (r) selectPair(r.dataset.pair);
  });
  $("#layout-reset").onclick = () => {
    if (!window.confirm("Reset your sidebar order and unfold all groups?")) return;
    delete app.prefs.group_order; delete app.prefs.pair_order; delete app.prefs.collapsed;
    savePrefs(); renderPairs();
  };
}
async function loadPairs(flashPair) {
  app.pairs = await api("/api/pairs");
  renderPairs(flashPair);
  fillPairSelects();
  if (app.view) renderFollow();      // the changed-outside-VolMarker bar
}
function fillPairSelects() {
  $("#group-list").innerHTML = [...new Set(app.pairs.map((p) => p.group).filter(Boolean))].sort()
    .map((g) => `<option value="${esc(g)}"></option>`).join("");
  const opts = app.pairs.map((p) => `<option>${p.pair}</option>`).join("");
  for (const id of ["#np-b1", "#np-b2", "#su-b1", "#su-b2"]) {
    const el = $(id), v = el.value; el.innerHTML = `<option value="">—</option>` + opts; el.value = v;
  }
}

// ---------------------------------------------------------------- pair loading
function showTab(tab, render = true) {
  app.tab = tab;
  $$(".tabs button").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  $$("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== tab));
  if (!render || !app.view) return;
  if (tab === "weights") renderWeights(true);
  if (tab === "history") renderHistory();
  if (tab === "setup") renderSetup();
  if (tab === "surface") renderTermChart();
  if (tab === "s3d") render3D();
}
async function selectPair(pair) {
  if (app.dirty && pair !== app.current && !confirmDiscard()) return;
  if (pair !== app.current) { app.defEvents = []; app.testRows = []; showTab("surface", false); }
  app.current = pair;
  history.replaceState(null, "", "#" + pair);
  await reloadView();
  renderPairs();
}
function confirmDiscard() {
  return window.confirm(`Discard unpublished edits on ${app.current}?`);
}
async function reloadView() {
  const v = await api(`/api/pairs/${app.current}`);
  app.view = v;
  app.baseVersion = v.record.version;
  app.published = pick(v.record);
  app.draft = pick(v.record);
  app.preview = { state: v.record, resolved: v.resolved, warnings: v.record.warnings || [] };
  app.dirty = false;
  hideBanner();
  $("#empty").hidden = true; $("#pane").hidden = false;
  await Promise.all([loadMarket(false), loadParent()]);
  buildGrid(); renderAll();
  if (app.tab === "weights") renderWeights(true);
  if (app.tab === "history") renderHistory();
  if (app.tab === "setup") renderSetup();
}
// spread parent: the grid shows this pair's ATM minus the parent's published ATM
async function loadParent() {
  const ref = app.draft?.spread_ref;
  if (!ref) { app.parent = null; return; }
  try {
    const v = await api(`/api/pairs/${ref}`);
    app.parent = { pair: ref, version: v.record.version, ts: v.record.ts,
      atm: Object.fromEntries(app.tenors.map((t) => [t, v.resolved[t].mid.atm])) };
  } catch { app.parent = { pair: ref, atm: {}, missing: true }; }
}
function pick(rec) {
  const s = {};
  for (const k of STATE_KEYS) s[k] = clone(rec[k] ?? null);
  s.weights = normaliseWeights(s.weights);
  for (const k of ["lock_rr10f", "lock_bf10f"]) s[k] = s[k] === null ? true : !!s[k];
  s.atm_type = s.atm_type || "DNS";
  s.follow_market = !!s.follow_market; s.alerts_off = !!s.alerts_off; s.rr_put = !!s.rr_put;
  s.follow_bases = s.follow_bases !== false;
  for (const r of Object.values(s.tenors)) {   // records from before the 10D values were stored
    if (r.rr10 == null) r.rr10 = r.rr25 * r.rr10f;
    if (r.bf10 == null) r.bf10 = r.bf25 * r.bf10f;
  }
  return s;
}

// ---------------------------------------------------------------- grid
const COLS = [
  { f: "corr", label: "Corr", dp: 3, derived: true, group: "" },
  { f: "atm", label: "ATM", dp: 3, group: "ATM", sep: true },
  { f: "psprd", label: "vs", dp: 3, parentCol: true },
  { f: "spread", label: "Spread", dp: 2 },
  { calc: (r) => r.quoted.atm_bid, label: "Bid", dp: 3, q: true, title: "Fenics ATM bid (incl. skew)" },
  { calc: (r) => r.quoted.atm_offer, label: "Offer", dp: 3, q: true, title: "Fenics ATM offer (incl. skew)" },
  { f: "rr25", label: "25D", dp: 3, group: "Risk reversal", sep: true, groupToggle: true },
  { f: "rr10", label: "10D", dp: 3, lock: "lock_rr10f", role: "tenD" },
  { f: "rr10f", label: "10D fac", dp: 3, lock: "lock_rr10f", role: "factor" },
  { f: "bf25", label: "25D", dp: 3, group: "Butterfly", sep: true },
  { f: "bf10", label: "10D", dp: 3, lock: "lock_bf10f", role: "tenD" },
  { f: "bf10f", label: "10D fac", dp: 3, lock: "lock_bf10f", role: "factor" },
  { f: "skew_atm", label: "ATM", dp: 2, group: "Skew → Fenics only", sep: true, skew: true },
  { f: "skew_rr", label: "RR", dp: 2, skew: true },
  { f: "skew_bf", label: "BF", dp: 2, skew: true },
];
function visibleCols() {
  return COLS.filter((c) => (!c.derived || app.draft.mode === "derived") && (!c.parentCol || app.draft.spread_ref));
}

function buildGrid() {
  const cols = visibleCols();
  const groups = [];
  for (const c of cols) {
    if (c.group !== undefined || !groups.length) groups.push({ label: c.group || "", n: 1, sep: c.sep, toggle: c.groupToggle });
    else groups[groups.length - 1].n++;
  }
  const putTitle = "Favour puts (this pair, for everyone): RRs are shown and typed as put − call.\nExports and FMD stay call − put.";
  let h = `<thead><tr><th class="tn"></th>${groups.map((g) => `<th class="grp" colspan="${g.n}">` + (g.toggle ?
    `<span id="rr-head">${esc(g.label)}</span><label class="lock put" title="${esc(putTitle)}"><input type="checkbox" data-lock="rr_put"> favour puts</label>` :
    esc(g.label)) + `</th>`).join("")}</tr>`;
  const lockTitle = "Ticked: type the factor; 10D = 25D × factor (10D locked).\nUnticked: type the 10D quote; factor = 10D / 25D.";
  h += `<tr><th class="tn">Tenor</th>${cols.map((c) => `<th class="${c.sep ? "sep" : ""}" title="${esc(c.title || "")}">` +
    (c.role === "factor" ? `<label class="lock" title="${esc(lockTitle)}"><input type="checkbox" data-lock="${c.lock}"> lock</label>` : "") +
    `${c.parentCol ? `vs ${esc(app.draft.spread_ref)}` : c.label}</th>`).join("")}</tr></thead><tbody>`;
  for (const t of app.tenors) {
    h += `<tr data-t="${t}"><td class="tn">${t}</td>`;
    cols.forEach((c, i) => {
      const cls = c.sep ? "sep" : "";
      if (c.calc) h += `<td class="calc ${c.q ? "q" : ""} ${cls}" data-t="${t}" data-c="${i}"></td>`;
      else {
        const mk = MKT_FIELDS[c.f];
        h += `<td class="${cls}${mk ? " mk" : ""}"${mk ? ` data-t="${t}" data-m="${mk}"` : ""}><input class="cell${c.skew ? " skew" : ""}" data-t="${t}" data-f="${c.f}" inputmode="decimal" autocomplete="off" ${c.skew ? 'placeholder=" "' : ""}>` +
          (mk ? `<div class="mq" data-t="${t}" data-m="${mk}"></div>` : "") + `</td>`;
      }
    });
    h += `</tr>`;
  }
  $("#grid").innerHTML = h + "</tbody>";
}

function publishedRow(t) { return app.published.tenors[t]; }
function same(a, b) {
  if ((a === null || a === undefined) && (b === null || b === undefined)) return true;
  if (typeof a === "boolean" || typeof b === "boolean") return !!a === !!b;
  return a !== null && b !== null && Math.abs(a - b) < 1e-9;
}

function renderGrid() {
  const cols = visibleCols();
  const resolved = app.preview.resolved;
  for (const el of $$("#grid thead input[data-lock]")) {
    const k = el.dataset.lock;
    el.checked = !!app.draft[k];
    el.parentElement.classList.toggle("dirty", app.draft[k] !== app.published[k]);
  }
  $("#rr-head").textContent = app.draft.rr_put ? "Risk reversal (put − call)" : "Risk reversal";
  for (const el of $$("#grid tbody input")) {
    const t = el.dataset.t, f = el.dataset.f;
    if (f === "psprd") { renderParentCell(el, t); continue; }
    const v = app.draft.tenors[t][f], pub = publishedRow(t)[f];
    const changed = !same(v, pub);
    const col = cols.find((c) => c.f === f);
    if (document.activeElement !== el) el.value = col.skew && !v ? "" : fmt(v == null ? v : rrSign(f) * v, col.dp);
    el.classList.toggle("dirty", changed);
    // ATM, RR and BF: green above the published value, red below (as shown, so in put − call on favour-puts pairs)
    const dir = changed && MKT_FIELDS[f] && v != null && pub != null ? Math.sign(rrSign(f) * (v - pub)) : 0;
    el.classList.toggle("up", dir > 0); el.classList.toggle("down", dir < 0);
    const xl = !!app.draft.excel && (FOLLOW_FIELDS.includes(f) || f === "spread");
    const fol = (app.draft.follow_market && FOLLOW_FIELDS.includes(f)) || xl;
    const held = f === "corr" && !movesWithBases();
    if (col.lock || fol || held || el.readOnly) {
      const lockRo = !!col.lock && (col.role === "factor") !== !!app.draft[col.lock];
      const ro = fol || lockRo || held;
      el.readOnly = ro; el.tabIndex = ro ? -1 : 0;
      el.classList.toggle("ro", ro);
      el.title = xl ? "Marked in Excel: read from its export file (untick Marked in Excel in Setup to mark here)" :
        held ? (external() ? `Implied by the ATM ${app.draft.excel ? "from Excel" : "from FMD"} and the bases; it moves when either is published`
          : "Implied by the ATM and the bases (tick Move with bases in Setup to keep the correlation instead)") :
        fol ? "Follows FMD: published by the feed (untick Follow FMD in Setup to mark by hand)" :
        lockRo ? (col.role === "factor" ? "Calibrated from the 10D quote (untick lock to edit)" : "25D × factor (untick lock to type the 10D)") : "";
    }
    el.classList.toggle("follow", fol);
    if (f === "atm") el.classList.toggle("derived", movesWithBases() || !!app.draft.follow_parent);
    if (f === "atm" && app.draft.follow_parent) el.title = `Moves with ${app.draft.spread_ref}: ATM = ${app.draft.spread_ref} + spread. Type an ATM to recalibrate the spread`;
    if (f === "atm" && app.draft.mode === "derived" && !external()) {
      el.title = app.draft.follow_bases ? "Moves with the bases; type a value to calibrate the correlation" : "Kept as typed while the bases move; the correlation follows";
    }
  }
  for (const td of $$("#grid td.calc")) {
    const col = cols[+td.dataset.c];
    td.textContent = fmt(col.calc(resolved[td.dataset.t]), col.dp);
  }
}

function parentSpread(t, atm) {
  const pa = app.parent?.atm?.[t];
  return pa == null || atm == null ? null : atm - pa;
}
function renderParentCell(el, t) {
  const p = app.parent, pa = p?.atm?.[t], row = app.draft.tenors[t], fp = !!app.draft.follow_parent;
  const v = fp && row.psprd != null ? row.psprd : parentSpread(t, row.atm);
  if (document.activeElement !== el) el.value = fmt(v, 3);
  const pub = fp && publishedRow(t).psprd != null ? publishedRow(t).psprd : parentSpread(t, app.view.resolved[t].mid.atm);
  el.classList.toggle("dirty", !same(v, pub));
  const ro = app.draft.follow_market || !!app.draft.excel || pa == null;
  el.readOnly = ro; el.tabIndex = ro ? -1 : 0; el.classList.toggle("ro", ro);
  el.title = pa == null ? `${app.draft.spread_ref} has no ${t} ATM` :
    `${app.current} ATM − ${p.pair} ATM ${fmt(pa, 3)} (published v${p.version})\nType a spread to set the ATM to ${p.pair} + spread` +
    (fp ? `\nMoves with ${p.pair}: the spread is kept when ${p.pair} is published` : "");
}
function onCellInput(el) {
  const t = el.dataset.t, f = el.dataset.f, row = app.draft.tenors[t];
  const txt = el.value.trim();
  const v = txt === "" ? null : Number(txt);
  if (txt !== "" && !Number.isFinite(v)) { el.classList.add("dirty"); return; }
  if (f === "psprd") {                    // ATM = parent ATM + spread
    const pa = app.parent?.atm?.[t];
    if (v === null || pa == null) return;
    setDraftValue(t, "atm", Math.round((pa + v) * 1e6) / 1e6);
    if (app.draft.follow_parent) app.draft.tenors[t].psprd = v;      // kept when the parent moves
    return changed();
  }
  setDraftValue(t, f, v === null ? v : rrSign(f) * v);
  changed();
}
function setDraftValue(t, f, v) {
  const row = app.draft.tenors[t];
  if (f === "atm" && app.draft.follow_parent) {
    row.atm = v; row.psprd = null;         // calibrate the spread to the parent to this ATM
  } else if (f === "atm" && movesWithBases()) {
    row.atm = v; row.corr = null;          // calibrate correlation to this ATM
  } else if (f === "corr") {
    row.corr = v;
  } else {
    row[f] = v === null && !["atm", "rr10", "bf10"].includes(f) ? 0 : v;
  }
}
function revertDraftValue(t, f) {
  app.draft.tenors[t][f] = clone(publishedRow(t)[f]);
  if (f === "atm" && app.draft.mode === "derived") app.draft.tenors[t].corr = clone(publishedRow(t).corr);
  if (f === "atm" && app.draft.follow_parent) app.draft.tenors[t].psprd = clone(publishedRow(t).psprd);
}

function moveFocus(el, dRow) {
  const f = el.dataset.f;
  const i = app.tenors.indexOf(el.dataset.t) + dRow;
  if (i < 0 || i >= app.tenors.length) return;
  const next = $(`#grid input[data-t="${app.tenors[i]}"][data-f="${f}"]`);
  if (next) { next.focus(); next.select(); }
}

// ---------------------------------------------------------------- preview / dirty
let previewTimer = null, previewSeq = 0;
function changed() {
  app.dirty = true;
  updateButtons();
  if (app.tab === "weights") weightsCalc();
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, 120);
}
async function runPreview() {
  const seq = ++previewSeq;
  try {
    const p = await api(`/api/pairs/${app.current}/preview`, { state: app.draft });
    if (seq !== previewSeq) return;
    app.preview = p;
    for (const t of app.tenors) {                 // sync the calculated side of each 10D pair
      if (document.activeElement?.dataset?.t === t) continue;
      for (const f of ["rr10", "rr10f", "bf10", "bf10f"]) app.draft.tenors[t][f] = p.state.tenors[t][f];
    }
    if (app.draft.follow_parent) {               // sync ATM/spread computed from the parent
      for (const t of app.tenors) {
        if (document.activeElement?.dataset?.t === t) continue;
        app.draft.tenors[t].atm = p.state.tenors[t].atm;
        app.draft.tenors[t].psprd = p.state.tenors[t].psprd;
      }
    }
    if (app.draft.mode === "derived") {          // sync derived ATM/corr into the draft
      for (const t of app.tenors) {
        const focused = document.activeElement?.dataset?.t === t;
        if (!focused) {
          app.draft.tenors[t].atm = p.state.tenors[t].atm;
          app.draft.tenors[t].corr = p.state.tenors[t].corr;
        }
      }
    }
    hideBanner("error");
  } catch (e) {
    showBanner(`Draft is invalid: ${esc(e.message)}`, [], "error");
  }
  renderAll();
}
function updateButtons() {
  const sd = setupDirty();
  $("#su-save").disabled = !sd;
  $("#su-save").textContent = sd ? "Save ●" : "Save";
  $("#su-status").textContent = sd ? "Unsaved settings" : "";
  const oe = app.dirty && app.draft ? otherEdits() : null, pd = !!oe && (oe.keys.length + oe.cells.length > 0);
  $("#publish").disabled = !pd;
  $("#discard").disabled = !app.dirty;
  $("#publish").textContent = pd ? "Publish ●" : "Publish";
  $("#publish").title = pd && sd ? "Publishes your edits and the unsaved settings (Ctrl/Cmd+S)" : "Ctrl/Cmd+S";
}

async function publish() {
  if (!app.dirty || $("#publish").disabled) return;      // settings alone are saved in Setup
  clearTimeout(previewTimer);
  $("#publish").disabled = true;
  try {
    await api(`/api/pairs/${app.current}/publish`, {
      state: app.draft, expected_version: app.baseVersion, comment: $("#comment").value,
    });
    $("#comment").value = "";
    await reloadView();
    await loadPairs();
  } catch (e) {
    if (e.status === 409) {
      const l = e.data.latest;
      showBanner(`<b>${esc(l.user)}</b> published ${l.pair} v${l.version} at ${localTime(l.ts)} while you were editing. Nothing was saved.`, [
        ["Load theirs (discard mine)", () => reloadView()],
        ["Publish mine over it", () => rebase(l)],
      ]);
    } else {
      showBanner(`Publish failed: ${esc(e.message)}`, [], "error");
    }
    updateButtons();
  }
}
async function rebase(latest) {
  // keep the draft, but compare against / build on the latest version
  const v = await api(`/api/pairs/${app.current}`);
  app.view = v; app.baseVersion = latest.version; app.published = pick(v.record);
  hideBanner(); renderAll(); changed();
}

async function loadExportFile() {
  if (app.dirty && !confirmDiscard()) return;
  let r;
  try { r = await api(`/api/pairs/${app.current}/load-export`, { state: app.draft }); }
  catch (e) { showBanner(`Last export: ${esc(e.message)}`, [["OK", () => hideBanner()]], "error"); return; }
  app.draft = pick(r.state);
  const at = localTime(new Date(r.mtime * 1000).toISOString());
  $("#comment").value = `From ${r.file} (${at})`;
  changed(); buildGrid(); renderAll();
  showBanner(`Loaded ${esc(r.file)}, written ${at}` +
    (r.ours ? " <span class=\"muted\">(VolMarker's own last export)</span>" : "") +
    (r.missing.length ? `. <span class="warn">Not in the file, kept as they were: ${r.missing.join(", ")}</span>` : "") +
    ". Check the draft and Publish to take it over.", [["OK", () => hideBanner()]]);
}

// ---------------------------------------------------------------- banner
function showBanner(html, actions = [], kind = "") {
  const b = $("#banner");
  b.className = "banner " + kind; b.dataset.kind = kind;
  b.innerHTML = `<span>${html}</span>`;
  for (const [label, fn] of actions) {
    const btn = document.createElement("button"); btn.textContent = label; btn.onclick = fn; b.appendChild(btn);
  }
  b.hidden = false;
}
function hideBanner(kind) {
  const b = $("#banner");
  if (kind === undefined || b.dataset.kind === kind) b.hidden = true;
}

// ---------------------------------------------------------------- header / checks
function renderHeader() {
  const r = app.view.record, d = app.draft;
  $("#ph-pair").textContent = r.pair;
  const mode = d.mode === "derived" ? `Derived from ${d.bases.join(" × ")}` : "Marked";
  const by = r.source === "propagation" ? `auto-updated (${esc(r.comment)})` : `by ${esc(r.user)}`;
  $("#ph-sub").innerHTML = `${d.group ? esc(d.group) + " · " : ""}${mode}${d.key ? " · key pair" : ""} · v${r.version} ${by} at ${localTime(r.ts)}` +
    (r.source === "propagation" ? ` · last marked ${localTime(r.marked_ts)}` : "") +
    (app.view.dependents.length ? ` · feeds ${app.view.dependents.join(", ")}` : "") +
    (d.spread_ref ? (d.follow_parent ? ` · moves with ${esc(d.spread_ref)}` : ` · spread vs ${esc(d.spread_ref)}`) : "");
}
function renderWarnings() {
  const w = app.preview.warnings || [];
  $("#warnings").innerHTML = w.length ? w.map((x) => `<li>${esc(x)}</li>`).join("") : `<li class="ok">✓ No issues</li>`;
}
function renderSkewExpiry() {
  const v = app.draft.skew_expiry;
  $("#skew-exp").value = v ? toLocalInput(new Date(v)) : "";
}
function toLocalInput(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------------------------------------------------------------- broker market (FMD)
async function loadMarket(render = true) {
  if (!app.current) return;
  try {
    app.market = await api(`/api/pairs/${app.current}/market?brand=${encodeURIComponent($("#mkt-brand").value)}`);
  } catch { app.market = null; }
  renderFeed(app.market && app.market.feed);
  const sel = $("#mkt-brand"), cur = sel.value;
  const brands = (app.market && app.market.brands) || [];
  sel.innerHTML = `<option value="auto">Auto (FMD first)</option><option value="best">Best of brands</option>` +
    brands.map((b) => `<option value="${esc(b)}">${esc(b)}</option>`).join("");
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : "auto";
  if (render && app.view) { renderMarket(); renderFollow(); renderTermChart(); }
}
function renderFeed(st) {
  const el = $("#fmd"); if (!st) return;
  el.classList.toggle("off", st.state === "off");
  el.classList.toggle("warn", st.state === "down");
  el.textContent = st.state === "live" ? "● FMD live" : st.state === "down" ? "● FMD down" : "FMD off";
  el.title = st.state === "off" ? "No market data feed running" :
    `Fenics Market Data ${st.host || ""} · last message ${st.last_msg ? localTime(st.last_msg) : "–"}` + (st.last_error ? ` · ${st.last_error}` : "");
}
function mktQuote(t, m) {
  if (!app.market || !$("#mkt-show").checked || app.market.feed.state === "off") return null;
  return app.market.view?.[t]?.[m] || null;
}
function renderMarket() {
  const res = app.preview.resolved;
  for (const td of $$("#grid td.mk")) {
    const t = td.dataset.t, m = td.dataset.m, sg = rrSign(m), mq = mktQuote(t, m), el = $(".mq", td);
    td.classList.remove("out");
    if (!mq) { el.textContent = ""; el.title = ""; continue; }
    const q = sg < 0 ? rrFlip(mq) : mq;
    const two = (v) => (v == null ? "–" : (Math.abs(v) < 0.005 ? 0 : v).toFixed(2));
    el.textContent = q.bid != null || q.offer != null ? `${two(q.bid)} / ${two(q.offer)}` : `mid ${two(q.mid)}`;
    if (q.alt_atm) el.textContent += "*";
    el.classList.toggle("stale", !!q.stale);
    const v = res[t].mid[m] == null ? null : sg * res[t].mid[m];
    let side = "";
    if (v != null && q.bid != null && v < q.bid - 1e-9) side = `below bid by ${(q.bid - v).toFixed(3)}`;
    if (v != null && q.offer != null && v > q.offer + 1e-9) side = `above offer by ${(v - q.offer).toFixed(3)}`;
    td.classList.toggle("out", !!side);
    const al = app.alerts.find((a) => a.key === `${app.current}|${t}|${m}`);
    td.classList.toggle("alert", !!al);
    el.title = `${q.brand}${q.ptype ? " " + q.ptype : ""} · mid ${fmt(q.mid, 3)}` +
      (q.age_min != null ? ` · ${age(q.age_min)} old` : "") + (q.alt_atm ? ` · *other ATM convention than ${app.draft.atm_type}` : "") +
      (side ? `\nYour mark is ${side}` : "") +
      (al ? `\nALERT since ${localTime(al.since)}: published ${fmt(sg * al.value, 2)} vs FMD ${fmt(sg * al.mkt, 2)} (${sg * al.dev > 0 ? "+" : ""}${fmt(sg * al.dev, 2)}, tol ${fmt(al.tol, 2)})` : "") +
      (app.draft.follow_market ? "" : "\nClick to set the draft to the broker mid");
  }
}
function setFromMarket(t, m, v, force = false) {
  const row = app.draft.tenors[t];
  if (v == null || ((app.draft.follow_market || app.draft.excel) && !force)) return;
  if (m === "atm") { row.atm = v; if (movesWithBases()) row.corr = null; }
  else if (m === "rr10" || m === "bf10") {
    const w = m.slice(0, 2);
    row[m] = v;                                     // the 10D quote itself (input when unlocked or following FMD)
    if (Math.abs(row[`${w}25`]) > 1e-12) row[`${w}10f`] = v / row[`${w}25`];   // locked: calibrate the factor
  } else row[m] = v;
}

function renderFollow() {
  const bar = $("#followbar"), d = app.draft, pub = app.published;
  if (d.excel || pub.excel) {
    const r = app.view.record, sum = app.pairs.find((p) => p.pair === app.current) || {};
    let html = !pub.excel ? "<b>Marked in Excel</b> is switched on in the draft. Publish to hand the marks to Excel: from then on VolMarker reads the pair's export file and stops writing it."
      : !d.excel ? "<b>Marked in Excel</b> switched off in the draft. Publish to mark the pair here again; VolMarker then writes its export files."
      : `<b>Marked in Excel</b> · ` + (r.excel_file ? `last export read ${localTime(new Date(r.excel_mtime * 1000).toISOString())}`
        : `waiting for Excel's next export of ${app.current.slice(0, 3)}${app.current.slice(3)}volsurface.txt`) +
        (r.excel_file ? ` <span class="muted">(${esc(r.excel_file.split(/[\\/]/).pop())})</span>` : "") +
        ` · VolMarker doesn't write this pair's files.` + (sum.excel_error ? ` <span class="warn">Last file not readable: ${esc(sum.excel_error)}</span>` : "");
    bar.className = "followbar" + (sum.excel_error ? " down" : ""); bar.innerHTML = html; bar.hidden = false;
    return;
  }
  const sum = app.pairs.find((p) => p.pair === app.current) || {};
  if (sum.excel_foreign && !d.follow_market && !pub.follow_market) {
    bar.className = "followbar held";
    bar.innerHTML = "<b>The export file was changed outside VolMarker</b> (Excel?). Load it into the draft to take those marks over, " +
      "or tick Marked in Excel in Setup if Excel keeps marking this pair. ";
    const btn = document.createElement("button"); btn.textContent = "Last export"; btn.onclick = loadExportFile;
    bar.appendChild(btn); bar.hidden = false;
    return;
  }
  if (!d.follow_market && !pub.follow_market) { bar.hidden = true; return; }
  bar.hidden = false;
  const f = (app.market && app.market.follow) || {};
  let cls = "", html;
  if (d.follow_market && !pub.follow_market) html = "<b>Follow FMD</b> is switched on in the draft. Publish to hand ATM, RR and BF to the feed.";
  else if (!d.follow_market) html = "<b>Follow FMD</b> switched off in the draft. Publish to take the surface back; the current values stay as your marks.";
  else {
    const h = f.health || "down";
    cls = h === "ok" || h === "starting" ? "" : h;
    const v = app.view.record;
    html = h === "down" ? `<b>Follows FMD, but it is not updating</b> (${app.market?.feed?.state !== "live" ? "feed down" : "follower not running"}). Fenics keeps v${v.version} from ${localTime(v.ts)}.`
      : h === "starting" ? "<b>Follows FMD.</b> Waiting for the follower in the FMD feed process…"
      : `<b>Follows FMD</b> · last FMD publish ${f.last_publish ? localTime(f.last_publish) : "–"} · checked ${f.last_check ? localTime(f.last_check) : "–"}` +
        (f.waiting ? " · next publish due (throttled)" : "");
    if (h !== "down" && f.missing) html += ` · <span title="${esc((f.missing_list || []).join(", "))}">${f.missing} mark(s) without a fresh FMD quote keep their last value</span>`;
    for (const j of (h !== "down" && f.held) || [])
      html += `<br>Held: ${j.tenor} ${MARK_LABEL[j.mark]}${rrSign(j.mark) < 0 ? " (P−C)" : ""} ${fmt(rrSign(j.mark) * j.from, 2)} → ${fmt(rrSign(j.mark) * j.to, 2)}, taken in ${j.accept_in}s unless it reverts`;
    html += " · You keep spread, skew and event weights." +
      (d.mode === "derived" ? " The correlation is implied from the FMD ATMs and the bases." : "");
  }
  bar.className = "followbar " + cls; bar.innerHTML = html;
}

function renderAll() {
  renderHeader(); renderFollow(); renderGrid(); renderMarket(); renderWarnings(); renderSkewExpiry(); renderTermChart(); updateButtons();
  if (app.tab === "s3d") render3D();
}

// ---------------------------------------------------------------- charts
const tip = $("#tooltip");
function lineChart(el, { n, xLabel, xTicks, series, yLabel, markers = [] }) {
  const W = el.clientWidth || 600, H = el.clientHeight || 240;
  const m = { l: 44, r: 64, t: 10, b: 24 };
  const all = series.flatMap((s) => s.values.concat((s.whiskers || []).flat()).filter((v) => v !== null && v !== undefined));
  if (!all.length) { el.innerHTML = `<div class="muted" style="padding:20px">No data</div>`; return; }
  let lo = Math.min(...all), hi = Math.max(...all);
  const pad = Math.max((hi - lo) * 0.1, 0.1); lo -= pad; hi += pad;
  const step = niceStep((hi - lo) / 5);
  const x = (i) => m.l + (n === 1 ? 0 : (i * (W - m.l - m.r)) / (n - 1));
  const y = (v) => m.t + ((hi - v) * (H - m.t - m.b)) / (hi - lo);
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(yLabel || "chart")}">`;
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    s += `<line x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--grid)" stroke-width="1"/>`;
    s += `<text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v.toFixed(step < 0.1 ? 2 : 1)}</text>`;
  }
  s += `<line x1="${m.l}" x2="${W - m.r}" y1="${H - m.b}" y2="${H - m.b}" stroke="var(--axis)"/>`;
  const rowEnd = [];   // stack event labels in rows so close events don't collide
  for (const mk of [...markers].sort((a, b) => a.i - b.i)) {
    const w = mk.label.length * 6.5 + 14;
    let row = rowEnd.findIndex((end) => x(mk.i) > end);
    if (row < 0) { row = rowEnd.length; rowEnd.push(0); }
    rowEnd[row] = x(mk.i) + w;
    const ly = m.t + 8 + row * 13;
    s += `<line x1="${x(mk.i)}" x2="${x(mk.i)}" y1="${ly + 4}" y2="${H - m.b}" stroke="var(--muted)" stroke-dasharray="2 3"/>`;
    s += `<text x="${x(mk.i) + 3}" y="${ly}">${esc(mk.label)}</text>`;
  }
  for (const [i, label] of xTicks) s += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${esc(label)}</text>`;
  for (const se of [...series].reverse()) {   // first series drawn on top
    let d = "", pen = false;
    se.values.forEach((v, i) => {
      if (v === null || v === undefined) { pen = false; return; }
      d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; pen = true;
    });
    if (se.whiskers) se.whiskers.forEach((w, i) => {
      if (w) s += `<line x1="${x(i)}" x2="${x(i)}" y1="${y(w[0])}" y2="${y(w[1])}" stroke="${se.color}" stroke-width="2" stroke-linecap="round"/>`;
    });
    if (se.noLine) { se.values.forEach((v, i) => { if (v != null) s += `<circle cx="${x(i)}" cy="${y(v)}" r="4" fill="${se.color}" stroke="var(--surface)" stroke-width="2"/>`; }); continue; }
    s += `<path d="${d}" fill="none" stroke="${se.color}" stroke-width="2" stroke-linejoin="round" ${se.dash ? 'stroke-dasharray="5 4"' : ""}/>`;
    if (se.dots) se.values.forEach((v, i) => { if (v != null) s += `<circle cx="${x(i)}" cy="${y(v)}" r="3.5" fill="${se.color}" stroke="var(--surface)" stroke-width="2"/>`; });
  }
  // direct labels at the last point, nudged apart so they never overlap
  const labels = series.filter((se) => se.label !== false).map((se) => {
    const li = se.values.map((v, i) => (v == null ? -1 : i)).filter((i) => i >= 0).pop();
    return li === undefined ? null : { name: se.name, x: x(li) + 8, y: y(se.values[li]) + 4 };
  }).filter(Boolean).sort((a, b) => a.y - b.y);
  for (let k = 1; k < labels.length; k++) labels[k].y = Math.max(labels[k].y, labels[k - 1].y + 13);
  for (const l of labels) s += `<text x="${l.x}" y="${l.y}" style="fill:var(--ink-2)">${esc(l.name)}</text>`;
  s += `<line class="xh" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--axis)" visibility="hidden"/>`;
  s += `<rect x="${m.l}" y="0" width="${W - m.l - m.r}" height="${H}" fill="transparent"/></svg>`;
  el.innerHTML = s;
  const svg = el.firstChild, xh = $(".xh", svg);
  svg.addEventListener("mousemove", (ev) => {
    const r = svg.getBoundingClientRect();
    const px = ((ev.clientX - r.left) * W) / r.width;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) * (n - 1)) / (W - m.l - m.r))));
    xh.setAttribute("x1", x(i)); xh.setAttribute("x2", x(i)); xh.setAttribute("visibility", "visible");
    tip.innerHTML = `<b>${esc(xLabel(i))}</b>` + series.map((se) =>
      `<div class="row"><span class="sw" style="background:${se.color}"></span>${esc(se.name)}<span class="v">${fmt(se.values[i], 3)}</span></div>`).join("");
    tip.hidden = false;
    tip.style.left = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = ev.clientY + 14 + "px";
  });
  svg.addEventListener("mouseleave", () => { tip.hidden = true; xh.setAttribute("visibility", "hidden"); });
}
function niceStep(raw) {
  const p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * p;
}

function renderTermChart() {
  const T = app.tenors, res = app.preview.resolved;
  const series = [{ name: app.dirty ? "Draft" : "Mid", color: "var(--s1)", values: T.map((t) => res[t].mid.atm), dots: true }];
  if (app.dirty) series.push({ name: "Published", color: "var(--muted)", dash: true, values: T.map((t) => app.view.resolved[t].mid.atm) });
  if (T.some((t) => Math.abs(res[t].quoted.atm - res[t].mid.atm) > 1e-9))
    series.push({ name: "Fenics", color: "var(--s2)", values: T.map((t) => res[t].quoted.atm) });
  if (app.draft.mode === "derived") {
    ["var(--s3)", "var(--s4)"].forEach((color, k) => {
      const b = app.draft.bases[k], bv = app.view.bases[b];
      if (bv) series.push({ name: b, color, values: T.map((t) => bv.atm[t]) });
    });
  }
  if (app.parent && app.draft.mode !== "derived" && !app.parent.missing)
    series.push({ name: app.parent.pair, color: "var(--s3)", values: T.map((t) => app.parent.atm[t] ?? null) });
  if (T.some((t) => mktQuote(t, "atm")))
    series.push({ name: "Market", color: "var(--s5)", noLine: true, label: false,
      values: T.map((t) => mktQuote(t, "atm")?.mid ?? null),
      whiskers: T.map((t) => { const q = mktQuote(t, "atm"); return q && q.bid != null && q.offer != null ? [q.bid, q.offer] : null; }) });
  $("#legend").innerHTML = series.length > 1 ? series.map((s) =>
    `<span><i class="${s.dash ? "dash" : ""}" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("") : "";
  lineChart($("#chart"), {
    n: T.length, xLabel: (i) => T[i], xTicks: T.map((t, i) => [i, t]), series, yLabel: "ATM vol by tenor",
  });
}

// ---------------------------------------------------------------- 3D surface
const TENOR_DAYS = { ON: 1, "1W": 7, "2W": 14, "1M": 30, "2M": 61, "3M": 91, "6M": 182, "9M": 273, "1Y": 365, "2Y": 730, "3Y": 1095, "4Y": 1461, "5Y": 1826 };
const DELTAS = ["10D P", "25D P", "ATM", "25D C", "10D C"];
const SEQ = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];
const view3d = { yaw: -0.65, pitch: 0.55 };

function smile(r) {   // pillar vols left to right: 10P 25P ATM 25C 10C
  if (r.atm == null) return null;
  return [r.atm + r.bf10 - r.rr10 / 2, r.atm + r.bf25 - r.rr25 / 2, r.atm,
          r.atm + r.bf25 + r.rr25 / 2, r.atm + r.bf10 + r.rr10 / 2];
}
function hexRgb(h) { return [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)); }
function mix(a, b, t) { const A = hexRgb(a), B = hexRgb(b); return `rgb(${A.map((v, i) => Math.round(v + (B[i] - v) * t)).join(",")})`; }
function seqColor(t) {
  t = Math.max(0, Math.min(1, t)) * (SEQ.length - 1);
  const i = Math.min(SEQ.length - 2, Math.floor(t));
  return mix(SEQ[i], SEQ[i + 1], t - i);
}
function isDark() {
  const th = document.documentElement.dataset.theme;
  return th ? th === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
}
function divColor(t) {   // t in [-1, 1]: blue (down) - gray - red (up)
  const mid = isDark() ? "#383835" : "#f0efec";
  return t < 0 ? mix(mid, "#1c5cab", Math.min(1, -t)) : mix(mid, "#c53a39", Math.min(1, t));
}

function surfaceData() {
  const src = $('input[name="s3-src"]:checked').value;
  const res = app.preview.resolved;
  const T = app.tenors.filter((t) => res[t].mid.atm != null);
  let grid;
  if (src === "diff") {
    grid = T.map((t) => {
      const a = smile(res[t].mid), b = smile(app.view.resolved[t].mid);
      return a && b ? a.map((v, j) => v - b[j]) : [0, 0, 0, 0, 0];
    });
  } else {
    grid = T.map((t) => smile(src === "quoted" ? res[t].quoted : res[t].mid));
  }
  return { T, grid, diff: src === "diff" };
}

function render3D() {
  const el = $("#surf3d");
  const { T, grid, diff } = surfaceData();
  if (T.length < 2) { el.innerHTML = `<div class="muted" style="padding:20px">Need at least two tenors with an ATM</div>`; return; }
  const W = el.clientWidth || 900, H = el.clientHeight || 560;
  const flat = grid.flat();
  let lo = Math.min(...flat), hi = Math.max(...flat);
  if (diff) { const m = Math.max(Math.abs(lo), Math.abs(hi), 0.01); lo = -m; hi = m; }
  else if (hi - lo < 0.2) { const c = (hi + lo) / 2; lo = c - 0.1; hi = c + 0.1; }
  const color = (v) => (diff ? divColor(v / hi) : seqColor((v - lo) / (hi - lo)));

  // model coordinates: x = delta (-1..1), y = tenor (-1 short .. 1 long), z = vol (-0.42..0.42)
  const ax = $("#s3-axis").value;
  const tpos = T.map((t) => (ax === "sqrt" ? Math.sqrt(TENOR_DAYS[t]) : T.indexOf(t)));
  const ty = (i) => -1 + (2 * (tpos[i] - tpos[0])) / (tpos[tpos.length - 1] - tpos[0]);
  const dx = (j) => -1 + j / 2;
  const vz = (v) => -0.42 + (0.84 * (v - lo)) / (hi - lo);

  const { yaw, pitch } = view3d, cy = Math.cos(yaw), sy = Math.sin(yaw), cp = Math.cos(pitch), sp = Math.sin(pitch);
  const scale = Math.min(W, H * 1.3) * 0.27, ox = W / 2, oy = H / 2 - 10;
  const P = (x, y, z) => {
    const x1 = x * cy - y * sy, y1 = x * sy + y * cy;
    const depth = y1 * cp - z * sp;          // larger = farther from the viewer (camera above, in front)
    const up = z * cp + y1 * sp;
    return [ox + x1 * scale, oy - up * scale, depth];
  };

  let s = `<svg viewBox="0 0 ${W} ${H}">`;
  // floor grid + back walls
  const zf = -0.42;
  const line = (a, b, attrs) => `<line x1="${a[0]}" y1="${a[1]}" x2="${b[0]}" y2="${b[1]}" ${attrs}/>`;
  for (let j = 0; j < 5; j++) s += line(P(dx(j), -1, zf), P(dx(j), 1, zf), `stroke="var(--grid)"`);
  T.forEach((_, i) => (s += line(P(-1, ty(i), zf), P(1, ty(i), zf), `stroke="var(--grid)"`)));
  // vertical axis on the back corner (the one farthest from the viewer)
  const corners = [[-1, -1], [1, -1], [-1, 1], [1, 1]];
  const back = corners.reduce((b, c) => (P(c[0], c[1], 0)[2] > P(b[0], b[1], 0)[2] ? c : b));
  const step = niceStep((hi - lo) / 5);
  let vlabels = "";   // drawn after the surface so they stay readable
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) {
    const a = P(back[0], back[1], vz(v));
    s += line(a, P(back[0] === 1 ? -1 : 1, back[1], vz(v)), `stroke="var(--grid)" stroke-dasharray="2 3"`);
    vlabels += `<text x="${a[0] - 6}" y="${a[1] + 4}" text-anchor="end">${(diff && v > 0 ? "+" : "") + v.toFixed(step < 0.1 ? 2 : 1)}</text>`;
  }
  s += line(P(back[0], back[1], zf), P(back[0], back[1], 0.42), `stroke="var(--axis)"`);

  // quads, painted back to front
  const quads = [];
  for (let i = 0; i < T.length - 1; i++) for (let j = 0; j < 4; j++) {
    const pts = [[i, j], [i, j + 1], [i + 1, j + 1], [i + 1, j]].map(([a, b]) => P(dx(b), ty(a), vz(grid[a][b])));
    const avg = (grid[i][j] + grid[i][j + 1] + grid[i + 1][j] + grid[i + 1][j + 1]) / 4;
    quads.push({ pts, depth: pts.reduce((m, p) => m + p[2], 0) / 4, fill: color(avg) });
  }
  quads.sort((a, b) => b.depth - a.depth);
  for (const q of quads) s += `<polygon points="${q.pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" ")}" fill="${q.fill}" stroke="var(--surface)" stroke-width="1" stroke-linejoin="round"/>`;

  // axis labels on the front floor edges
  const front = corners.reduce((b, c) => (P(c[0], c[1], 0)[2] < P(b[0], b[1], 0)[2] ? c : b));
  const yEdge = front[1], xEdge = front[0];
  DELTAS.forEach((d, j) => { const a = P(dx(j), yEdge * 1.12, zf); s += `<text x="${a[0]}" y="${a[1] + 12}" text-anchor="middle">${d}</text>`; });
  T.forEach((t, i) => {
    if (ax === "sqrt" && ["1W", "2W", "2M", "9M", "4Y"].includes(t)) return;   // crowded at the short end
    const a = P(xEdge * 1.12, ty(i), zf); s += `<text x="${a[0]}" y="${a[1] + 12}" text-anchor="middle">${t}</text>`;
  });
  const da = P(0, yEdge * 1.32, zf), ta = P(xEdge * 1.36, 0, zf);
  s += `<text class="axis" x="${da[0]}" y="${da[1] + 14}" text-anchor="middle">Delta</text>`;
  s += `<text class="axis" x="${ta[0]}" y="${ta[1] + 14}" text-anchor="middle">Tenor</text>`;
  s += vlabels;
  s += `<circle class="hov" r="5" fill="none" stroke="var(--ink)" stroke-width="2" visibility="hidden"/></svg>`;
  el.innerHTML = s;

  // hover: nearest vertex
  const verts = [];
  T.forEach((t, i) => grid[i].forEach((v, j) => { const p = P(dx(j), ty(i), vz(v)); verts.push({ t, d: DELTAS[j], v, x: p[0], y: p[1], depth: p[2] }); }));
  const svg = el.firstChild, hov = $(".hov", svg);
  svg.onmousemove = (ev) => {
    if (drag3d) return;
    const r = svg.getBoundingClientRect(), mx = ((ev.clientX - r.left) * W) / r.width, my = ((ev.clientY - r.top) * H) / r.height;
    let best = null, bd = 18 * 18;
    for (const v of verts) { const d = (v.x - mx) ** 2 + (v.y - my) ** 2; if (d < bd || (best && d === bd && v.depth < best.depth)) { bd = d; best = v; } }
    if (!best) { tip.hidden = true; hov.setAttribute("visibility", "hidden"); return; }
    hov.setAttribute("cx", best.x); hov.setAttribute("cy", best.y); hov.setAttribute("visibility", "visible");
    tip.innerHTML = `<b>${best.t} · ${best.d}</b><div class="row">${diff ? "Change" : "Vol"}<span class="v">${diff && best.v > 0 ? "+" : ""}${fmt(best.v, 3)}</span></div>`;
    tip.hidden = false;
    tip.style.left = Math.min(ev.clientX + 14, window.innerWidth - tip.offsetWidth - 8) + "px";
    tip.style.top = ev.clientY + 14 + "px";
  };
  svg.onmouseleave = () => { tip.hidden = true; hov.setAttribute("visibility", "hidden"); };

  // colour scale
  const stops = diff ? [-1, -0.5, 0, 0.5, 1].map((t) => divColor(t)) : SEQ;
  $("#s3-scale").innerHTML = `<span>${diff ? "−" + fmt(hi, 2) : fmt(lo, 2)}</span><span class="bar" style="background:linear-gradient(90deg,${stops.join(",")})"></span><span>${diff ? "+" + fmt(hi, 2) : fmt(hi, 2)}</span><span class="muted">${diff ? "vol change vs published" : "vol"}</span>`;
}

let drag3d = null;
function wire3d() {
  const el = $("#surf3d");
  el.addEventListener("pointerdown", (e) => { drag3d = { x: e.clientX, y: e.clientY, ...view3d }; el.classList.add("drag"); el.setPointerCapture(e.pointerId); tip.hidden = true; });
  el.addEventListener("pointermove", (e) => {
    if (!drag3d) return;
    view3d.yaw = drag3d.yaw + (e.clientX - drag3d.x) * 0.008;
    view3d.pitch = Math.max(0.05, Math.min(1.5, drag3d.pitch + (e.clientY - drag3d.y) * 0.006));
    render3D();
  });
  const end = () => { drag3d = null; el.classList.remove("drag"); };
  el.addEventListener("pointerup", end); el.addEventListener("pointercancel", end);
  $$('input[name="s3-src"]').forEach((r) => (r.onchange = render3D));
  $("#s3-axis").onchange = render3D;
  $("#s3-reset").onclick = () => { view3d.yaw = -0.65; view3d.pitch = 0.55; render3D(); };
}

// ---------------------------------------------------------------- weights tab
const isoToday = () => { const d = new Date(), p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };
const useDefaults = () => app.draft.weights.use_defaults !== false;
function renderWeights(reload) {
  const w = app.draft.weights, [c1, c2] = [app.current.slice(0, 3), app.current.slice(3)];
  $("#factors").innerHTML = WEEKDAYS.map((d, i) =>
    `<label>${d}<input class="num wf" data-wd="${i}" value="${fmt(w.weekdays[i], 2)}"></label>`).join("") +
    `<span class="fsep"></span>` + [["holiday1", `${c1} holiday`], ["holiday2", `${c2} holiday`]].map(([k, l]) =>
    `<label title="Multiplies the weekday weight on a ${l}">${l}<input class="num wf" data-k="${k}" value="${fmt(w[k], 2)}"></label>`).join("");
  const cut = (app.me.cuts || {})[app.draft.cut || "NYC"];
  $("#cut-note").textContent = cut ? `Expiry cut ${app.draft.cut || "NYC"}: ${cutLabel(cut)} (Setup)` : "";
  $("#ev-usedef").checked = useDefaults();
  renderEvents();
  renderTestDates();
  if (reload) { loadDefEvents(); loadDefaults(); }
  loadWeightsExport();
  weightsCalc(0);
}
// one row per date: desk default, pair override of a default, or the pair's own event
function eventRows() {
  const w = app.draft.weights, own = {}, defs = useDefaults() ? app.defEvents || [] : [];
  (w.events || []).forEach((e, i) => (own[e.date] ||= []).push({ ...e, i }));
  const rows = defs.filter((d) => !own[d.date]).map((d) => ({ date: d.date, desc: d.desc, def: d.weight, weight: d.weight, kind: "default", parts: d.parts, ccy: d.ccy }));
  for (const [date, es] of Object.entries(own)) {
    const d = defs.find((x) => x.date === date);
    rows.push({ date, desc: es.map((e) => e.desc).filter(Boolean).join(", "), def: d ? d.weight : null, defDesc: d?.desc, parts: d?.parts,
      weight: es.reduce((s, e) => s + (Number(e.weight) || 0), 0), kind: d ? "override" : "pair", idx: es.map((e) => e.i),
      ccy: es[0].ccy || d?.ccy || app.current.slice(0, 3) });
  }
  return rows.sort((a, b) => a.date.localeCompare(b.date));
}
function renderEvents() {
  const today = isoToday(), all = eventRows(), rows = all.filter((r) => r.date >= today);
  const past = all.filter((r) => r.date < today && r.kind !== "default").length;
  const wd = (d) => new Date(d + "T12:00:00").toLocaleDateString(undefined, { weekday: "short" });
  const ccys = [app.current.slice(0, 3), app.current.slice(3)];
  $("#events").innerHTML = `<thead><tr><th class="tn" title="Option expiry the event weighs on">Expiry</th><th class="tn">Day</th><th class="tn" title="Release time (${esc(app.me.calendar_tz || "")})">Release</th><th class="tn">Ccy</th><th class="tn">Description</th><th title="Desk default">Default</th><th>Weight</th><th></th></tr></thead><tbody>` +
    (rows.length ? rows.map((r) => {
      const i = r.idx ? r.idx[0] : "", extra = r.idx ? r.idx.slice(1).join(",") : "";
      const attrs = r.kind === "default" ? `data-date="${r.date}" data-desc="${esc(r.desc)}"` : `data-i="${i}" data-extra="${extra}"`;
      // plain text in the same yyyy-mm-dd as the other rows (a date picker would show the PC's regional format)
      const date = r.kind === "pair" ? `<input class="ev evdate" ${attrs} data-k="date" value="${r.date}" placeholder="yyyy-mm-dd" maxlength="10" size="10" title="Expiry date, yyyy-mm-dd">` : r.date;
      const breakdown = (r.parts || []).length > 1 ? r.parts.map((x) => `${x.desc}  ${fmt(x.weight, 2)}`).join("\n") + `\n= ${fmt(r.def, 2)}` : "";
      const shown = r.ccy && (r.desc || "").startsWith(r.ccy + " ") ? r.desc.slice(4) : r.desc;
      const desc = r.kind === "default" ? `<span class="muted" title="${esc(breakdown || "Desk default")}">${esc(shown)}</span>`
        : `<input class="ev" ${attrs} data-k="desc" value="${esc(r.desc)}" placeholder="${esc(r.defDesc || "")}">`;
      const act = r.kind === "pair" ? `<button class="ghost ev-del" data-idx="${r.idx.join(",")}" title="Remove">✕</button>`
        : r.kind === "override" ? `<button class="ghost ev-del" data-idx="${r.idx.join(",")}" title="Back to the desk default (${fmt(r.def, 2)})">↺</button>` : "";
      const rel = (r.parts || [])[0]?.release || "";
      const relTxt = rel.slice(0, 10) === r.date ? rel.slice(11) : rel ? `${wd(rel.slice(0, 10))} ${rel.slice(8, 10)}. ${rel.slice(11)}` : "";
      const relTitle = (r.parts || []).map((x) => `${x.desc}: ${x.release}`).join("\n");
      const ccy = r.kind === "pair" ? `<select class="ev" ${attrs} data-k="ccy">${ccys.map((c) => `<option${c === r.ccy ? " selected" : ""}>${c}</option>`).join("")}</select>` : esc(r.ccy || "");
      return `<tr class="ev-${r.kind}"><td class="tn">${date}</td><td class="tn">${wd(r.date)}</td><td class="tn small${rel && rel.slice(0, 10) !== r.date ? " moved" : ""}" title="${esc(relTitle)}">${esc(relTxt)}</td><td class="tn">${ccy}</td><td class="tn">${desc}</td>
        <td class="muted"${breakdown ? ` title="${esc(breakdown)}"` : ""}>${r.def == null ? "" : fmt(r.def, 2) + (breakdown ? " Σ" : "")}</td>
        <td><input class="ev num${r.kind === "override" ? " ovr" : ""}" ${attrs} data-k="weight" value="${fmt(r.weight, 2)}" title="${r.kind === "default" ? "Desk default; type to override for this pair" : r.kind === "override" ? "Overrides the desk default for this pair" : "This pair's own event"}"></td>
        <td>${act}</td></tr>`;
    }).join("") : `<tr><td colspan="8" class="muted tn">No upcoming events</td></tr>`) + "</tbody>";
  const nDef = rows.filter((r) => r.kind === "default").length, nOvr = rows.filter((r) => r.kind === "override").length;
  $("#ev-note").innerHTML = (useDefaults() ? `${nDef} from desk defaults, ${nOvr} overridden. ` : "Desk defaults off for this pair. ") +
    (past ? `${past} past event(s) hidden <button class="ghost" id="ev-prune">Remove past</button>` : "");
}
async function loadDefEvents() {
  const pair = app.current;
  try {
    const d = await api(`/api/pairs/${pair}/default-events?cut=${encodeURIComponent(app.draft.cut || "NYC")}`);
    if (pair === app.current) app.defEvents = d;
  } catch { app.defEvents = []; }
  if (app.tab === "weights") renderEvents();
}
let wcTimer = null;
function weightsCalc(delay = 200) {    // everything on the weights tab that depends on the draft
  clearTimeout(wcTimer);
  wcTimer = setTimeout(() => { if (app.tab !== "weights" || !app.current) return; loadTestDates(); renderCurve(); loadDailies(); renderWeightsExport(); }, delay);
}

// ---------------------------------------------------------------- test dates
const MAX_TEST_DATES = 120;    // service.MAX_TEST_DATES
function addTestDateRange() {
  const from = $("#td-from").value, to = $("#td-to").value, today = isoToday();
  if (!from || !to) return;
  const [a, b] = from <= to ? [from, to] : [to, from], out = [];
  const p = (n) => String(n).padStart(2, "0");
  for (const d = new Date(a + "T12:00:00"); ; d.setDate(d.getDate() + 1)) {
    const iso = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    if (iso > b) break;
    if (![0, 6].includes(d.getDay()) && iso > today) out.push(iso);
  }
  const all = [...new Set([...testDates(), ...out])].sort();
  if (all.length > MAX_TEST_DATES) {
    showBanner(`That would make ${all.length} test dates; the limit is ${MAX_TEST_DATES}. Pick a shorter range or clear some dates.`, [["OK", () => hideBanner()]], "error");
    return;
  }
  setTestDates(all);
}
function testDates() { return ((app.prefs.test_dates || {})[app.current] || []).filter((d) => d > isoToday()); }
function setTestDates(list) {
  const ds = [...new Set(list)].sort();
  (app.prefs.test_dates ||= {})[app.current] = ds;
  const q = testQuotes();                       // drop the quotes of removed dates
  for (const d of Object.keys(q)) if (!ds.includes(d)) delete q[d];
  savePrefs(); renderTestDates(); loadTestDates();
}
// external bid/offer per test date (e.g. from a bank's single-dealer platform), saved for you per pair
function testQuotes() { return ((app.prefs.test_quotes ||= {})[app.current] ||= {}); }
function quoteMid(q) {
  const vs = [q?.bid, q?.offer].filter((v) => v != null);
  return vs.length ? vs.reduce((a, b) => a + b, 0) / vs.length : null;
}
let tdSeq = 0;
async function loadTestDates() {
  const dates = testDates(), seq = ++tdSeq;
  if (!dates.length) { app.testRows = []; return renderTestDates(); }
  try {
    const r = await api(`/api/pairs/${app.current}/testdates`, { dates, state: app.dirty ? app.draft : null });
    if (seq === tdSeq) { app.testRows = r.rows; renderTestDates(); }
  } catch (e) { if (seq === tdSeq) { app.testRows = []; renderTestDates(e.message); } }
}
function renderTestDates(err) {
  const rows = (app.testRows || []).filter((r) => testDates().includes(r.date));
  const dirty = app.dirty, qs = testQuotes();
  const act = document.activeElement, focus = act?.matches?.("#td-table input.tq") ? [act.dataset.date, act.dataset.k, act.selectionStart, act.value] : null;
  // forward vol between two consecutive quoted rows, comparable with Fwd when both rows are quoted
  const extFwd = rows.map((r, i) => {
    const m = quoteMid(qs[r.date]), p = rows[i - 1], pm = p ? quoteMid(qs[p.date]) : null;
    if (m == null || pm == null || r.days <= p.days) return null;
    const fv = (m * m * r.days - pm * pm * p.days) / (r.days - p.days);
    return fv > 0 ? Math.sqrt(fv) : NaN;
  });
  const nq = rows.filter((r, i) => extFwd[i] != null && !Number.isNaN(extFwd[i])).length;
  $("#td-calib").disabled = !nq;
  $("#td-floor").checked = app.prefs.td_no_negative !== false;
  $("#td-calib").title = nq ? `Set the event weights so the forward vol between each two consecutive quoted dates matches the external quotes (${nq} interval${nq > 1 ? "s" : ""})`
    : "Type bid/offer on at least two consecutive test dates";
  $("#td-table").innerHTML = `<thead><tr><th class="tn">Expiry</th><th class="tn">Day</th><th title="Calendar days">Days</th><th title="Weighted days from today">W. days</th><th title="Weight of the expiry day itself">Day wt</th>` +
    `<th>${dirty ? "Draft ATM" : "ATM"}</th>${dirty ? "<th>Published</th><th>Δ</th>" : ""}<th title="Forward vol from the row above">Fwd</th>` +
    `<th class="sep" title="External bid (typed)">Ext bid</th><th title="External offer (typed)">Ext offer</th><th title="ATM minus the external mid">vs mid</th>` +
    `<th title="External forward vol from the row above (both rows quoted)">Ext fwd</th><th class="tn">Events</th><th></th></tr></thead><tbody>` +
    (rows.length ? rows.map((r, i) => {
      const q = qs[r.date] || {}, m = quoteMid(q), vm = m != null && r.atm != null ? r.atm - m : null, ef = extFwd[i];
      const ext = `<td class="sep"><input class="tq num" data-date="${r.date}" data-k="bid" value="${q.bid ?? ""}" inputmode="decimal" autocomplete="off"></td>` +
        `<td><input class="tq num" data-date="${r.date}" data-k="offer" value="${q.offer ?? ""}" inputmode="decimal" autocomplete="off"></td>` +
        `<td class="${vm == null ? "muted" : ""}">${vm == null ? "" : (vm > 0 ? "+" : "") + fmt(vm, 2)}</td>` +
        `<td${ef != null && r.fwd != null && Math.abs(ef - r.fwd) >= 0.05 ? ' class="chg"' : ""}>${Number.isNaN(ef) ? '<span class="warn" title="The quotes imply negative forward variance">neg.</span>' : fmt(ef, 3)}</td>`;
      let d = r.atm != null && r.published != null ? r.atm - r.published : null;
      if (d != null && Math.abs(d) < 0.0005) d = 0;
      return `<tr${r.good_day ? "" : ' class="muted" title="Not a good business day for both currencies"'}><td class="tn">${r.date}${r.pillar ? ` <span class="tag" title="Expiry of the ${r.pillar} mark: quote it to set the shift to the external quotes from your mark">${r.pillar}</span>` : ""}</td><td class="tn">${r.weekday}</td><td>${r.days}</td>
        <td>${fmt(r.weighted_days, 2)}</td><td>${fmt(r.day_weight, 2)}</td><td><b>${fmt(r.atm, 3)}</b></td>
        ${dirty ? `<td>${fmt(r.published, 3)}</td><td class="${d && Math.abs(d) >= 0.0005 ? "chg" : "muted"}">${d == null ? "" : (d > 0 ? "+" : "") + fmt(d, 3)}</td>` : ""}
        <td>${fmt(r.fwd, 3)}</td>${ext}<td class="tn small">${esc(r.events.join(", "))}</td>
        <td><button class="ghost td-del" data-date="${r.date}" title="Remove" tabindex="-1">✕</button></td></tr>`;
    }).join("") : `<tr><td colspan="15" class="muted tn">${esc(err || "Add expiry dates above, e.g. + Event dates for each event and the weekday before and after it")}</td></tr>`) + "</tbody>";
  if (focus) {
    const el = $(`#td-table input.tq[data-date="${focus[0]}"][data-k="${focus[1]}"]`);
    if (el) { el.value = focus[3]; el.focus(); try { el.setSelectionRange(focus[2], focus[2]); } catch {} }
  }
}
function onTestQuote(el) {
  const txt = el.value.trim(), v = Number(txt), qs = testQuotes();
  if (txt !== "" && !(v > 0)) return;
  const q = { ...(qs[el.dataset.date] || {}) };
  if (txt === "") delete q[el.dataset.k]; else q[el.dataset.k] = v;
  if (q.bid == null && q.offer == null) delete qs[el.dataset.date]; else qs[el.dataset.date] = q;
  savePrefs(); renderTestDates();
}
async function calibrateTestQuotes() {
  const qs = testQuotes(), quotes = testDates().filter((d) => qs[d]).map((d) => ({ date: d, ...qs[d] }));
  let r;
  const floor = app.prefs.td_no_negative !== false;
  try { r = await api(`/api/pairs/${app.current}/testdates-calibrate`, { quotes, state: app.draft, floor }); }
  catch (e) { showBanner(`Calibrate to quotes: ${esc(e.message)}`, [["OK", () => hideBanner()]], "error"); return; }
  if (!r.events.length) { showBanner(esc((r.warnings || []).join(" · ") || "Nothing to calibrate"), [["OK", () => hideBanner()]], "error"); return; }
  const w = app.draft.weights, dates = new Set(r.events.map((e) => e.date));
  w.events = (w.events || []).filter((e) => !dates.has(e.date)).concat(r.events.map((e) => ({ ...e })));
  changed(); renderWeights(false);
  // list the weights that moved (a daily fit touches every day)
  const moved = r.intervals.filter((iv) => Math.abs(iv.weight - iv.weight_before) >= 0.05);
  const lines = moved.slice(0, 12).map((iv) => `${iv.date} ${esc(iv.desc)}: ${fmt(iv.weight_before, 2)} → <b>${fmt(iv.weight, 2)}</b>`);
  if (moved.length > 12) lines.push(`… and ${moved.length - 12} more`);
  const sh = (r.anchors || []).map((a) => `${(a.shift > 0 ? "+" : "") + fmt(a.shift, 3)} at ${a.tenor || a.date}`).join(", ");
  const fit = r.fit ? ` ${r.fit.floor ? "Best fit, no negative weights" : "Exact fit"}: within ${fmt(r.fit.max_gap, 3)} vol of every quote (${r.fit.worst}), rms ${fmt(r.fit.rms, 3)}.` : "";
  showBanner(`Event weights set from the external quotes${sh ? ` (our level vs theirs: ${sh})` : ""}.${fit} ${moved.length} weight(s) changed by 0.05 or more. Review and publish.` +
    (lines.length ? `<br>${lines.join("<br>")}` : "") +
    (r.warnings.length ? `<br><span class="warn">${r.warnings.map(esc).join("<br>")}</span>` : ""), [["OK", () => hideBanner()]]);
}
// the expiry of each upcoming event (next 3 months) and the weekday before and after it (holidays count)
function addEventTestDates() {
  const today = isoToday(), lim = new Date(); lim.setMonth(lim.getMonth() + 3);
  const end = lim.toISOString().slice(0, 10);
  const evs = [...new Set(eventRows().filter((r) => r.date > today && r.date <= end && r.weight > 0).map((r) => r.date))];
  if (!evs.length) return;
  const p = (n) => String(n).padStart(2, "0");
  const weekday = (iso, step) => {
    const d = new Date(iso + "T12:00:00");
    do d.setDate(d.getDate() + step); while ([0, 6].includes(d.getDay()));
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  };
  const out = [...testDates()];
  for (const d of evs) out.push(weekday(d, -1), d, weekday(d, 1));
  const all = [...new Set(out.filter((d) => d > today))].sort();
  if (all.length > MAX_TEST_DATES) {
    showBanner(`That would make ${all.length} test dates; the limit is ${MAX_TEST_DATES}. Clear some dates first.`, [["OK", () => hideBanner()]], "error");
    return;
  }
  setTestDates(all);
}

// ---------------------------------------------------------------- desk defaults for recurring events
async function loadDefaults() {
  if (app.dfDirty) return;
  try { app.defaults = await api("/api/event-defaults"); } catch { return; }
  app.dfRows = clone(app.defaults.rows);
  renderDefaults();
}
function renderDefaults() {
  const d = app.defaults || {}, q = ($("#df-filter").value || "").trim().toLowerCase();
  const rows = (app.dfRows || []).map((r, i) => ({ ...r, i }))
    .filter((r) => !q || `${r.ccy} ${r.event} ${r.ticker || ""}`.toLowerCase().includes(q))
    .sort((a, b) => (a.ccy || "").localeCompare(b.ccy || "") || (b.weight - a.weight) || a.i - b.i);
  $("#df-table").innerHTML = `<thead><tr><th class="tn">Ccy</th><th class="tn">Event</th><th class="tn">Ticker</th><th>Weight</th><th title="Upcoming calendar events this one matches">Upcoming</th><th class="tn">Next release</th><th></th></tr></thead><tbody>` +
    (rows.length ? rows.map((r) => `<tr><td class="tn"><input class="df" data-i="${r.i}" data-k="ccy" value="${esc(r.ccy)}" maxlength="3" size="4" placeholder="any"></td>
      <td class="tn"><input class="df" data-i="${r.i}" data-k="event" value="${esc(r.event)}" placeholder="e.g. FOMC Rate Decision (Upper Bound)" size="34"></td>
      <td class="tn"><input class="df" data-i="${r.i}" data-k="ticker" value="${esc(r.ticker || "")}" placeholder="optional" size="14"></td>
      <td><input class="df num" data-i="${r.i}" data-k="weight" value="${fmt(r.weight, 2)}"></td>
      <td${r.matches === 0 ? ' class="warn" title="Not in the calendar: check the name, or import the Bloomberg file"' : ""}>${r.matches ?? ""}</td><td class="tn muted small">${esc(r.next || "")}</td>
      <td><button class="ghost df-del" data-i="${r.i}" title="Remove">✕</button></td></tr>`).join("")
      : `<tr><td colspan="7" class="muted tn">${q ? "No match" : "No relevant events yet"}</td></tr>`) + "</tbody>";
  $("#df-save").disabled = $("#df-revert").disabled = !app.dfDirty;
  $("#df-meta").textContent = app.dfDirty ? "Unsaved changes" : d.ts ? `Saved by ${d.user} at ${localTime(d.ts)}` : "";
  const imp = d.imports || {};
  $("#cal-imports").textContent = Object.keys(imp).length ? "Bloomberg: " + Object.entries(imp).map(([c, x]) =>
    `${c} ${localTime(x.ts)} by ${x.user} (${x.events})`).join(" · ") : "No Bloomberg import yet";
}

// ---------------------------------------------------------------- Bloomberg calendar import
async function previewImport(file) {
  const buf = new Uint8Array(await file.arrayBuffer());
  let bin = ""; for (let i = 0; i < buf.length; i += 0x8000) bin += String.fromCharCode(...buf.subarray(i, i + 0x8000));
  const payload = { name: file.name, data: btoa(bin) };
  try {
    const p = await api("/api/calendar/preview", payload);
    app.imp = { payload, p, rows: p.events.map((x) => ({ ...x, on: x.relevant, weight: x.weight ?? 0 })) };
    $("#defaults-card").open = true; $("#cal-preview").hidden = false;
    renderImport();
  } catch (e) { showBanner(`Import failed: ${esc(e.message)}`, [], "error"); }
}
function renderImport() {
  const { p, rows } = app.imp, q = $("#cal-p-filter").value.trim().toLowerCase(), only = $("#cal-p-only").checked;
  $("#cal-p-title").textContent = `${p.file}: ${p.ccys.join(", ")}`;
  const on = rows.filter((r) => r.on).length;
  $("#cal-p-sub").textContent = `${p.count} releases ${p.from} → ${p.to}, ${rows.length} different events, ${on} relevant · times in ${p.source_tz}` +
    (p.downloaded ? ` · downloaded ${p.downloaded}` : "");
  const shown = rows.map((r, i) => ({ ...r, i })).filter((r) => (!only || r.on) && (!q || `${r.name} ${r.ticker}`.toLowerCase().includes(q)));
  $("#cal-p-table").innerHTML = `<thead><tr><th></th><th class="tn">Event</th><th class="tn">Ticker</th><th title="Bloomberg relevance">Rel.</th><th>Count</th><th class="tn">Next</th><th class="tn">Times</th><th>Weight</th></tr></thead><tbody>` +
    shown.map((r) => `<tr class="${r.on ? "" : "muted"}"><td><input type="checkbox" class="ip" data-i="${r.i}" data-k="on"${r.on ? " checked" : ""}></td>
      <td class="tn">${esc(r.name)}</td><td class="tn small">${esc(r.ticker)}</td><td>${r.relevance ?? ""}</td><td>${r.count}</td>
      <td class="tn small">${esc(r.next || "")}</td><td class="tn small">${esc(r.times.join(" "))}</td>
      <td><input class="ip num" data-i="${r.i}" data-k="weight" value="${fmt(r.weight, 2)}"${r.on ? "" : " disabled"}></td></tr>`).join("") + "</tbody>";
  const notes = [];
  if (p.missing.length) notes.push(`On the list but not in this file (kept): ${p.missing.map((r) => r.event).join(", ")}.`);
  if (p.skipped) notes.push(`${p.skipped} release(s) without a fixed day yet are left out (e.g. ${p.skipped_list.slice(0, 2).map((x) => x.event).join(", ")}).`);
  if (Object.keys(p.assumed).length) notes.push(`Country codes ${Object.keys(p.assumed).join(", ")} counted as ${[...new Set(Object.values(p.assumed))].join(", ")}.`);
  notes.push("Ticked events become the list of relevant events for these currencies, with these weights. The whole file is kept, so ticking another event later needs no new import.");
  $("#cal-p-note").textContent = notes.join(" ");
}
async function saveImport() {
  if (app.dfDirty && !window.confirm("Your unsaved changes to the relevant events list will be replaced. Continue?")) return;
  const { payload, rows } = app.imp;
  try {
    app.defaults = await api("/api/calendar/import", { ...payload, expected_version: app.defaults?.version ?? null,
      selection: rows.filter((r) => r.on).map((r) => ({ ccy: r.ccy, name: r.name, ticker: r.ticker, weight: r.weight })) });
    app.dfRows = clone(app.defaults.rows); app.dfDirty = false; app.imp = null;
    $("#cal-preview").hidden = true; renderDefaults();
    if (app.current) { await loadDefEvents(); weightsCalc(0); }
    showBanner("Bloomberg calendar imported. Every pair now uses it.", [["OK", () => hideBanner()]]);
  } catch (e) {
    showBanner(e.status === 409 ? `${esc(e.message)}. Nothing was imported; load the list again and retry.` : `Import failed: ${esc(e.message)}`, [], "error");
  }
}

// ---------------------------------------------------------------- send weights (Fenics XML poster, Front Arena files)
async function loadWeightsExport() {
  const pair = app.current;
  try { const x = await api(`/api/pairs/${pair}/weights-export`); if (pair === app.current) { app.wx = x; renderWeightsExport(); } } catch {}
}
function renderWeightsExport() {
  const x = app.wx; if (!x) return;
  const l = x.last;
  $("#wx-state").innerHTML = (l ? `Last sent v${l.version} by ${esc(l.user)} at ${localTime(l.ts)}${l.ok ? "" : ` <span class="warn">(${esc(l.error || "failed")})</span>`}` : "Not sent from VolMarker yet") +
    (x.changed ? ` · <b class="warn">weights changed since</b>` : " · up to date");
  if (!$("#wx-user").value) $("#wx-user").value = app.prefs.fenics_user || "";
  $("#wx-send").disabled = app.dirty;
  $("#wx-send").title = app.dirty ? "Publish first: the published version is sent" : `POST to ${x.url}/xmlposter`;
  $("#wx-table").innerHTML = `<thead><tr><th class="tn">Expiry</th><th class="tn">Ccy</th><th class="tn">Type</th><th class="tn">Event</th><th>Weight</th><th>Sent value</th></tr></thead><tbody>` +
    `<tr><td class="tn" colspan="4">Weekdays Mon–Sun · holiday ${app.current.slice(0, 3)} / ${app.current.slice(3)}</td><td colspan="2">${x.weekdays.map((v) => fmt(v, 2)).join(" ")} · ${fmt(x.holiday1, 2)} / ${fmt(x.holiday2, 2)}</td></tr>` +
    x.fenics.map((e) => `<tr><td class="tn">${e.date}</td><td class="tn">${e.ccy}</td><td class="tn">${e.type}</td><td class="tn">${esc(e.name)}</td><td>${fmt(e.weight, 3)}</td><td>${fmt(e.value, 4)}</td></tr>`).join("") + "</tbody>";
}
async function sendWeights() {
  const username = $("#wx-user").value.trim(), password = $("#wx-pass").value;
  if (!username || !password) { $("#wx-msg").textContent = "User and password needed"; return; }
  if (app.prefs.fenics_user !== username) { app.prefs.fenics_user = username; savePrefs(); }
  $("#wx-send").disabled = true; $("#wx-msg").textContent = "Sending…";
  try {
    const r = await api(`/api/pairs/${app.current}/weights-export`, { username, password });
    $("#wx-msg").textContent = r.ok ? "Sent: Fenics accepted, Front Arena files written" : `Front Arena files written; Fenics: ${r.error}`;
    $("#wx-msg").className = r.ok ? "ok" : "warn";
    if (!r.ok && r.response) $("#wx-msg").title = r.response;
  } catch (e) { $("#wx-msg").textContent = `Failed: ${e.message}`; $("#wx-msg").className = "warn"; }
  $("#wx-pass").value = "";
  loadWeightsExport();
}

async function saveDefaults() {
  try {
    app.defaults = await api("/api/event-defaults", { rows: app.dfRows, expected_version: app.defaults?.version ?? null });
    app.dfRows = clone(app.defaults.rows); app.dfDirty = false;
    renderDefaults(); await loadDefEvents(); weightsCalc(0);
  } catch (e) {
    if (e.status === 409) showBanner(`${esc(e.message)}. Nothing was saved.`, [
      ["Load theirs (discard mine)", () => { app.dfDirty = false; hideBanner(); loadDefaults(); }],
      ["Keep editing mine", () => { app.defaults = e.data.latest; hideBanner(); }]]);
    else showBanner(`Saving the defaults failed: ${esc(e.message)}`, [], "error");
  }
}
function wireWeights() {
  $("#factors").addEventListener("input", (e) => {
    const v = Number(e.target.value); if (!Number.isFinite(v)) return;
    if (e.target.dataset.wd !== undefined) app.draft.weights.weekdays[+e.target.dataset.wd] = v;
    else app.draft.weights[e.target.dataset.k] = v;
    changed();
  });
  $("#ev-usedef").onchange = (e) => { app.draft.weights.use_defaults = e.target.checked; changed(); renderEvents(); };
  $("#events").addEventListener("change", (e) => {
    const el = e.target; if (!el.matches(".ev")) return;
    const evs = (app.draft.weights.events ||= []), k = el.dataset.k;
    let val = k === "weight" ? Number(el.value) || 0 : el.value;
    if (k === "date") {
      val = val.trim();
      const d = new Date(val + "T12:00:00");
      if (!/^\d{4}-\d{2}-\d{2}$/.test(val) || isNaN(d) || d.toISOString().slice(0, 10) !== val) {
        el.classList.add("bad"); el.title = "Not a date: use yyyy-mm-dd, e.g. 2026-11-04"; return;
      }
    }
    if (el.dataset.date) {                 // a desk default: typing creates this pair's override
      const def = (app.defEvents || []).find((x) => x.date === el.dataset.date);
      evs.push({ date: el.dataset.date, desc: k === "desc" ? val : el.dataset.desc, weight: k === "weight" ? val : def?.weight ?? 0, ccy: def?.ccy });
    } else {
      evs[+el.dataset.i][k] = val;
      if (k === "weight" && el.dataset.extra)     // several events on one date: keep one carrying the total
        el.dataset.extra.split(",").map(Number).sort((a, b) => b - a).forEach((j) => evs.splice(j, 1));
    }
    changed(); renderEvents();
  });
  $("#events").addEventListener("click", (e) => {
    if (!e.target.matches(".ev-del")) return;
    e.target.dataset.idx.split(",").map(Number).sort((a, b) => b - a).forEach((j) => app.draft.weights.events.splice(j, 1));
    changed(); renderEvents();
  });
  $("#ev-note").addEventListener("click", (e) => {
    if (e.target.id !== "ev-prune") return;
    const today = isoToday();
    app.draft.weights.events = (app.draft.weights.events || []).filter((x) => x.date >= today);
    changed(); renderEvents();
  });
  $("#ev-add").onclick = () => {
    const used = new Set(eventRows().map((r) => r.date)), d = new Date();
    let iso; do { d.setDate(d.getDate() + 1); iso = d.toISOString().slice(0, 10); } while (used.has(iso));
    (app.draft.weights.events ||= []).push({ date: iso, desc: "", weight: 0.5 });
    changed(); renderEvents();
  };
  // test dates
  $("#td-add").onclick = () => { const v = $("#td-date").value; if (v && v > isoToday()) setTestDates([...testDates(), v]); };
  $("#td-date").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#td-add").click(); });
  $("#td-events").onclick = addEventTestDates;
  $("#td-clear").onclick = () => setTestDates([]);
  $("#td-range").onclick = addTestDateRange;
  $("#td-from").addEventListener("change", () => { if (!$("#td-to").value || $("#td-to").value < $("#td-from").value) $("#td-to").value = $("#td-from").value; });
  $("#td-calib").onclick = calibrateTestQuotes;
  $("#td-floor").addEventListener("change", (e) => { app.prefs.td_no_negative = e.target.checked; savePrefs(); });
  $("#td-table").addEventListener("input", (e) => { if (e.target.matches("input.tq")) onTestQuote(e.target); });
  $("#td-table").addEventListener("click", (e) => {
    if (e.target.matches(".td-del")) setTestDates(testDates().filter((d) => d !== e.target.dataset.date));
  });
  // desk defaults
  $("#defaults-card").addEventListener("toggle", () => { if ($("#defaults-card").open) loadDefaults(); });
  $("#df-table").addEventListener("change", (e) => {
    const el = e.target; if (!el.matches(".df")) return;
    const r = app.dfRows[+el.dataset.i], k = el.dataset.k;
    r[k] = k === "weight" ? Number(el.value) || 0 : k === "ccy" ? el.value.trim().toUpperCase() : el.value.trim();
    delete r.matches; delete r.next;
    app.dfDirty = true; renderDefaults();
  });
  $("#df-table").addEventListener("click", (e) => {
    if (!e.target.matches(".df-del")) return;
    app.dfRows.splice(+e.target.dataset.i, 1); app.dfDirty = true; renderDefaults();
  });
  $("#df-add").onclick = () => { (app.dfRows ||= []).push({ ccy: app.current?.slice(0, 3) || "", event: "", ticker: "", weight: 0.5 }); app.dfDirty = true; $("#df-filter").value = ""; renderDefaults(); };
  $("#df-filter").addEventListener("input", renderDefaults);
  // Bloomberg import
  $("#cal-file").onchange = (e) => { const f = e.target.files[0]; e.target.value = ""; if (f) previewImport(f); };
  $("#cal-p-filter").addEventListener("input", renderImport);
  $("#cal-p-only").onchange = renderImport;
  $("#cal-p-table").addEventListener("change", (e) => {
    const el = e.target; if (!el.matches(".ip")) return;
    const r = app.imp.rows[+el.dataset.i];
    if (el.dataset.k === "on") r.on = el.checked; else r.weight = Number(el.value) || 0;
    renderImport();
  });
  $("#cal-p-save").onclick = saveImport;
  $("#cal-p-cancel").onclick = () => { app.imp = null; $("#cal-preview").hidden = true; };
  // send weights
  $("#wx-send").onclick = sendWeights;
  $("#wx-pass").addEventListener("keydown", (e) => { if (e.key === "Enter") sendWeights(); });
  $("#wx-show").onclick = () => { $("#wx-detail").hidden = !$("#wx-detail").hidden; };
  $("#df-save").onclick = saveDefaults;
  $("#df-revert").onclick = () => { app.dfDirty = false; loadDefaults(); };
}

// ---------------------------------------------------------------- market dailies -> weights
let dlTimer = null;
function loadDailies() {
  clearTimeout(dlTimer);
  dlTimer = setTimeout(async () => {
    if (app.tab !== "weights" || !app.current) return;
    try {
      app.dailies = await api(`/api/pairs/${app.current}/dailies`, { state: app.draft, brand: $("#mkt-brand").value });
    } catch (e) { app.dailies = { rows: [], warnings: [e.message] }; }
    renderDailies();
  }, 150);
}
function renderDailies() {
  const d = app.dailies, rows = (d && d.rows) || [];
  $("#dl-calib").disabled = !(d && d.events !== undefined && rows.length);
  if (!rows.length) {
    $("#dl-note").textContent = d && d.feed && d.feed.state !== "live" ? "Feed not live" : "No daily quotes for this pair";
    $("#dl-chart").innerHTML = ""; $("#dl-table").innerHTML = ""; $("#dl-warn").innerHTML = "";
    return;
  }
  $("#dl-note").textContent = `Forward vol per daily expiry (${d.basis} days): market vs. your current weights`;
  lineChart($("#dl-chart"), {
    n: rows.length, xLabel: (i) => `${rows[i].n}D · ${rows[i].weekday} ${rows[i].date}` + (rows[i].labels.length ? ` · ${rows[i].labels.join(", ")}` : ""),
    xTicks: rows.map((r, i) => [i, `${r.weekday.slice(0, 2)} ${r.date.slice(8)}`]),
    series: [
      { name: "Market fwd", color: "var(--s5)", values: rows.map((r) => r.mkt_fwd), dots: true },
      { name: "Your weights", color: "var(--s1)", values: rows.map((r) => r.model_fwd), dots: true },
    ],
    yLabel: "Forward vol per daily expiry",
  });
  const f2 = (v) => (v == null ? "" : fmt(Math.abs(v) < 0.005 ? 0 : v, 2));
  $("#dl-table").innerHTML = `<thead><tr><th class="tn">Expiry</th><th class="tn">Day</th><th>Mkt ATM</th><th>Your ATM</th><th>Mkt fwd</th><th>Your fwd</th><th>RR25${rrSign("rr25") < 0 ? " P−C" : ""}</th><th>BF25</th><th>Implied event</th><th>Your event</th><th class="tn">Events</th></tr></thead><tbody>` +
    rows.map((r) => {
      const diff = Math.abs(r.implied_event - r.current_event) >= 0.1;
      return `<tr><td class="tn">${r.n}D</td><td class="tn">${r.weekday} ${r.date}</td><td>${f2(r.mkt_atm)}</td><td>${f2(r.model_atm)}</td>
        <td>${f2(r.mkt_fwd)}</td><td>${f2(r.model_fwd)}</td><td>${f2(r.rr25 == null ? null : rrSign("rr25") * r.rr25)}</td><td>${f2(r.bf25)}</td>
        <td${diff ? ' style="color:var(--critical);font-weight:600"' : ""}>${f2(r.implied_event)}</td><td>${f2(r.current_event)}</td>
        <td class="tn">${esc(r.labels.join(", "))}</td></tr>`;
    }).join("") + "</tbody>";
  $("#dl-warn").innerHTML = (d.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
}
function calibrateDailies() {
  const d = app.dailies; if (!d || !d.window_end) return;
  const today = new Date().toISOString().slice(0, 10);
  const w = app.draft.weights;
  const kept = (w.events || []).filter((e) => !(e.date > today && e.date <= d.window_end));
  w.events = kept.concat(d.events.map((e) => ({ ...e })));
  changed(); renderWeights(false);
  showBanner(`Event weights up to ${d.window_end} set from the market dailies (${d.events.length} events). Review and publish.`, [["OK", () => hideBanner()]]);
}
let curveSeq = 0;
async function renderCurve() {
  const seq = ++curveSeq;
  let c;
  try { c = await api(`/api/pairs/${app.current}/curve`, { state: app.dirty ? app.draft : null }); } catch { return; }
  if (seq !== curveSeq) return;
  const pts = c.points;
  if (!pts.length) { $("#curve").innerHTML = `<div class="muted" style="padding:20px">No ATM marks</div>`; return; }
  const ticks = [];
  pts.forEach(([d], i) => {
    if (i && d.slice(0, 7) !== pts[i - 1][0].slice(0, 7)) ticks.push([i, new Date(d).toLocaleString(undefined, { month: "short" })]);
  });
  const draft = pts.some((p) => p[2] != null && Math.abs(p[2] - p[1]) > 1e-6);
  const series = draft
    ? [{ name: "Draft", color: "var(--s1)", values: pts.map((p) => p[2]), label: false },
       { name: "Published", color: "var(--muted)", dash: true, values: pts.map((p) => p[1]), label: false }]
    : [{ name: "ATM", color: "var(--s1)", values: pts.map((p) => p[1]), label: false }];
  $("#curve-legend").innerHTML = draft ? series.map((s) => `<span><i class="${s.dash ? "dash" : ""}" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("") : "";
  const evs = c.events.filter((e) => e.weight);
  lineChart($("#curve"), {
    n: pts.length, xLabel: (i) => {
      const d = pts[i][0], ev = evs.filter((e) => e.date === d).map((e) => `${e.desc} ${e.weight > 0 ? "+" : ""}${fmt(e.weight, 2)}`).join(", ");
      return d + (ev ? ` · ${ev}` : "");
    },
    xTicks: ticks, series,
    // label only the biggest days (all events are in the hover text)
    markers: [...evs].sort((a, b) => b.weight - a.weight).slice(0, 10).filter((e) => e.weight >= 0.3)
      .map((e) => ({ i: pts.findIndex((p) => p[0] >= e.date), label: e.desc.replace(/^[A-Z]{3} /, "").replace(/ \+\d+$/, "").slice(0, 24) })).filter((mk) => mk.i >= 0),
    yLabel: "Interpolated ATM vol by expiry date",
  });
}

// ---------------------------------------------------------------- setup tab
const setting = (v) => (v && typeof v === "object" && !Array.isArray(v) && !Object.keys(v).length ? null : v ?? null);
// draft edits that aren't settings, leaving out what switching a setting resets itself
function otherEdits(d = app.draft, p = app.published) {
  const ck = (k) => JSON.stringify(setting(d[k])) !== JSON.stringify(setting(p[k]));
  const skip = new Set([...(ck("mode") || ck("bases") || (d.mode === "derived" && external(d) !== external(p)) ? ["corr"] : []), ...(ck("spread_ref") || ck("follow_parent") ? ["psprd"] : []),
    ...(!p.follow_market && d.follow_market ? FOLLOW_FIELDS : [])]);
  const keys = STATE_KEYS.filter((k) => !SETUP_KEYS.includes(k) && k !== "pair" && k !== "tenors" && JSON.stringify(d[k]) !== JSON.stringify(p[k]));
  const cells = [];
  for (const t of app.tenors) for (const [f, v] of Object.entries(d.tenors[t]))
    if (!skip.has(f) && !same(v, p.tenors[t]?.[f])) cells.push([t, f]);
  return { keys, cells };
}
function setupDirty() {
  return !!app.draft && SETUP_KEYS.some((k) => JSON.stringify(setting(app.draft[k])) !== JSON.stringify(setting(app.published[k])));
}
// Save publishes the settings on the latest marks; the trader's other draft edits stay in the draft
async function saveSettings() {
  if (!setupDirty()) return;
  clearTimeout(previewTimer);
  $("#su-save").disabled = true;
  const oldDraft = app.draft, edits = otherEdits();
  try {
    await api(`/api/pairs/${app.current}/settings`, { state: oldDraft, expected_version: app.baseVersion });
  } catch (e) {
    if (e.status === 409) {
      const l = e.data.latest;
      showBanner(`<b>${esc(l.user)}</b> changed the settings of ${l.pair} (v${l.version}, ${localTime(l.ts)}) while you were editing. Nothing was saved.`, [
        ["Load theirs (discard mine)", () => reloadView()], ["Keep editing mine", () => hideBanner()]]);
    } else showBanner(`Save failed: ${esc(e.message)}`, [["OK", () => hideBanner()]], "error");
    updateButtons();
    return;
  }
  await reloadView();
  // put back the trader's other edits (marks, weights, locks) on the saved version
  let kept = 0;
  for (const k of edits.keys) if (JSON.stringify(oldDraft[k]) !== JSON.stringify(app.draft[k])) { app.draft[k] = clone(oldDraft[k]); kept++; }
  for (const [t, f] of edits.cells) if (!same(oldDraft.tenors[t][f], app.draft.tenors[t][f])) { app.draft.tenors[t][f] = oldDraft.tenors[t][f]; kept++; }
  if (kept) { changed(); renderGrid(); }
  if (app.tab === "setup") renderSetup();
  await loadPairs();
  showBanner(`Settings saved${kept ? ". Your other edits are still in the draft; publish them when ready." : "."}`, [["OK", () => hideBanner()]]);
}
function renderSetup() {
  const d = app.draft;
  $("#su-mode").value = d.mode;
  $("#su-b1").value = d.bases?.[0] || ""; $("#su-b2").value = d.bases?.[1] || "";
  $("#su-key").checked = !!d.key;
  if (document.activeElement !== $("#su-group")) $("#su-group").value = d.group || "";
  $("#su-spref").innerHTML = `<option value="">None</option>` +
    app.pairs.filter((p) => p.pair !== app.current).map((p) => `<option>${p.pair}</option>`).join("");
  $("#su-spref").value = d.spread_ref || "";
  $("#su-follow-parent").checked = !!d.follow_parent;
  $("#su-follow-parent").disabled = !d.spread_ref || d.mode !== "marked" || !!d.follow_market || !!d.excel;
  $("#su-follow-parent").parentElement.title = $("#su-follow-parent").disabled ? "Needs a spread parent, on a marked pair that doesn't follow FMD and isn't marked in Excel" : "";
  const cuts = app.me.cuts || {};
  $("#su-cut").innerHTML = Object.entries(cuts).map(([k, c]) => `<option value="${k}">${k} (${cutLabel(c)})</option>`).join("");
  $("#su-cut").value = d.cut || "NYC";
  if (document.activeElement !== $("#su-dc")) $("#su-dc").value = d.dc_factor == null ? "" : fmt(d.dc_factor, 2);
  $("#su-atm").value = d.atm_type || "DNS";
  $$(".su-derived").forEach((el) => (el.hidden = d.mode !== "derived"));
  $("#su-follow-bases").checked = d.follow_bases !== false;
  $("#su-follow-bases").disabled = external(d);
  $("#su-follow-bases").parentElement.title = external(d) ? `The ATMs come from ${d.excel ? "Excel" : "FMD"}, so the correlation always follows the bases` : "";
  $("#su-follow").checked = !!d.follow_market;
  $("#su-follow").disabled = !!d.excel;
  $("#su-excel").checked = !!d.excel;
  $("#su-excel").disabled = !!d.follow_market;
  const def = (app.alertCfg && app.alertCfg.tolerance) || {};
  const own = d.alert_tol || {};
  $("#su-tol").innerHTML = Object.entries(MARK_LABEL).map(([m, l]) =>
    `<label>${l}<input class="num" data-m="${m}" value="${own[m] != null ? fmt(own[m], 2) : ""}" placeholder="${fmt(def[m], 2)}"></label>`).join("");
  $("#su-alerts-off").checked = !!d.alerts_off;
  $(".su-tol").hidden = !!d.follow_market;
}
function onSetupChange() {
  const d = app.draft, mode = $("#su-mode").value;
  const bases = [$("#su-b1").value, $("#su-b2").value];
  d.key = $("#su-key").checked;
  d.group = $("#su-group").value.trim().replace(/\s+/g, " ") || null;
  if ($("#su-cut").value && $("#su-cut").value !== (d.cut || "NYC")) { d.cut = $("#su-cut").value; app.defEvents = []; loadDefEvents(); }
  const dcTxt = $("#su-dc").value.trim(), dcv = Number(dcTxt);
  d.dc_factor = dcTxt === "" || !Number.isFinite(dcv) ? null : dcv;
  const parentChanged = ($("#su-spref").value || null) !== (d.spread_ref || null);
  d.spread_ref = $("#su-spref").value || null;
  const fp = !!d.spread_ref && mode === "marked" && !$("#su-follow").checked && !$("#su-excel").checked && $("#su-follow-parent").checked;
  if (fp !== !!d.follow_parent || parentChanged) for (const t of app.tenors) d.tenors[t].psprd = null;   // (re)calibrate to the current ATMs
  d.follow_parent = fp;
  d.atm_type = $("#su-atm").value;
  if (mode === "derived") {
    if (!bases[0] || !bases[1]) { $$(".su-derived").forEach((el) => (el.hidden = false)); return; }
    if (d.mode !== "derived" || String(d.bases) !== String(bases)) {
      // calibrate correlations to the current ATMs
      for (const t of app.tenors) d.tenors[t].corr = null;
    }
    d.bases = bases;
  } else {
    d.bases = null;
    for (const t of app.tenors) d.tenors[t].corr = null;
  }
  d.follow_bases = mode !== "derived" || $("#su-follow-bases").checked;
  const rebuild = d.mode !== mode;
  d.mode = mode;
  const follow = $("#su-follow").checked;
  if (follow && !d.follow_market) {
    // start from the market so the publish that switches it on already shows FMD levels
    let n = 0;
    for (const t of app.tenors) for (const m of ["atm", "rr25", "bf25", "rr10", "bf10"]) {
      const q = app.market?.view?.[t]?.[m];
      if (q && q.mid != null && !q.stale && !q.alt_atm) { setFromMarket(t, m, Math.round(q.mid * 1e4) / 1e4, true); n++; }
    }
    showBanner(`Follow FMD: ${n} marks set to FMD mids in the draft. Publish to hand the surface to the feed.`, [["OK", () => hideBanner()]]);
  }
  if (!follow && d.follow_market) {
    // FMD 10D quotes can give factors a locked factor can't hold (25D near zero, opposite signs)
    for (const w of ["rr", "bf"]) {
      if (d[`lock_${w}10f`] && app.tenors.some((t) => !(d.tenors[t][`${w}10f`] > 0))) {
        d[`lock_${w}10f`] = false;
        showBanner(`Follow FMD off: the ${w.toUpperCase()} 10D factor lock was unticked because some FMD factors are zero or negative. The 10D quotes stay as they are.`, [["OK", () => hideBanner()]]);
      }
    }
  }
  d.follow_market = follow;
  const wasExcel = !!d.excel;
  d.excel = !follow && $("#su-excel").checked;
  if (wasExcel && !d.excel) {
    // Excel's 10D quotes can give factors a locked factor can't hold
    for (const w of ["rr", "bf"]) {
      if (d[`lock_${w}10f`] && app.tenors.some((t) => !(d.tenors[t][`${w}10f`] > 0))) d[`lock_${w}10f`] = false;
    }
  }
  d.alerts_off = $("#su-alerts-off").checked;
  renderSetup();
  if (parentChanged) loadParent().then(() => { buildGrid(); renderAll(); });
  if (rebuild) buildGrid();
  changed();
}

// ---------------------------------------------------------------- history tab
async function renderHistory() {
  const h = await api(`/api/pairs/${app.current}/history?limit=100`);
  app.historyRows = h;
  $("#history").innerHTML = `<thead><tr><th>v</th><th class="tn">Time</th><th class="tn">User</th><th class="tn">Source</th><th class="tn">Comment</th><th>1M ATM</th><th>1Y ATM</th><th></th></tr></thead><tbody>` +
    h.map((r, i) => `<tr><td>${r.version}</td><td class="tn">${localTime(r.ts)}</td><td class="tn">${esc(r.user)}</td>
      <td class="tn">${esc(r.source)}</td><td class="tn">${esc(r.comment)}</td>
      <td>${fmt(r.tenors["1M"].atm, 3)}</td><td>${fmt(r.tenors["1Y"].atm, 3)}</td>
      <td>${i ? `<button class="ghost restore" data-i="${i}" title="Copy this version into the draft">Restore</button>` : ""}</td></tr>`).join("") + "</tbody>";
}

// ---------------------------------------------------------------- live updates
function connect() {
  const es = new EventSource("/api/stream");
  es.onopen = () => $("#conn").classList.remove("down");
  es.onerror = () => $("#conn").classList.add("down");
  let t = null, flash = null, mktBusy = false;
  es.onmessage = async (m) => {
    const ev = JSON.parse(m.data);
    if (ev.type === "alerts") {
      loadAlerts(ev.new);
      clearTimeout(t); t = setTimeout(() => loadPairs(), 300);
      return;
    }
    if (ev.type === "event_defaults") {
      if (ev.user !== app.me.user || !app.dfDirty) loadDefaults();
      if (app.current && app.tab === "weights") { await loadDefEvents(); weightsCalc(0); }
      return;
    }
    if (ev.type === "market") {
      clearTimeout(t); t = setTimeout(() => loadPairs(), 400);
      if (app.current && (ev.pairs.includes(app.current) || ev.follow) && !mktBusy) {
        mktBusy = true; setTimeout(async () => { await loadMarket(); loadDailies(); mktBusy = false; }, 300);
      }
      return;
    }
    flash = ev.pair;
    clearTimeout(t); t = setTimeout(() => loadPairs(flash), 150);
    if (!app.current) return;
    const auto = (ev.source === "market" && app.published.follow_market) || ev.source === "propagation" ||
      (ev.source === "excel" && app.published.excel);
    if (ev.pair === app.current && ev.version > app.baseVersion && auto) {
      await quietUpdate(ev.source);   // no banner; keeps focus and the trader's edits (server rebases on publish)
    } else if (ev.pair === app.current && ev.version > app.baseVersion) {
      if (!app.dirty) {
        await reloadView();
        if (ev.user !== app.me.user || ev.source !== "manual")
          showBanner(`Updated to v${ev.version} by ${esc(ev.user)}${ev.source === "propagation" ? " (a base pair moved)" : ""}.`, [["OK", () => hideBanner()]]);
      } else {
        showBanner(`<b>${esc(ev.user)}</b> published ${ev.pair} v${ev.version} while you are editing.`, [
          ["Load theirs (discard mine)", () => reloadView()],
          ["Keep editing mine", () => hideBanner()],
        ]);
      }
    } else if (app.parent && ev.pair === app.parent.pair) {      // keep the marks, update the spreads
      await loadParent(); renderGrid(); renderTermChart();
    } else if (app.view && (app.draft.bases || []).includes(ev.pair)) {
      const v = await api(`/api/pairs/${app.current}`);
      app.view.bases = v.bases;
      if (app.dirty) runPreview(); else renderTermChart();
    }
  };
}

async function quietUpdate(source) {
  const v = await api(`/api/pairs/${app.current}`);
  if (!app.dirty) {
    app.view = v; app.baseVersion = v.record.version; app.published = pick(v.record); app.draft = pick(v.record);
    app.preview = { state: v.record, resolved: v.resolved, warnings: v.record.warnings || [] };
    renderAll();
  } else if (source === "market" || source === "excel") {   // take the feed's / Excel's fields, keep the trader's other edits
    const pub = pick(v.record);
    const fields = source === "excel" ? FOLLOW_FIELDS.concat("spread") : FOLLOW_FIELDS;
    for (const t of app.tenors) for (const f of fields) app.draft.tenors[t][f] = pub.tenors[t][f];
    app.view = v; app.baseVersion = v.record.version; app.published = pub;
    runPreview();
  } else {                              // propagation: re-derive the draft on the new bases
    app.view.bases = v.bases;
    runPreview();
  }
  if (app.tab === "history") renderHistory();
}

// ---------------------------------------------------------------- alerts
async function loadAlerts(fresh) {
  try { const r = await api("/api/alerts"); app.alerts = r.alerts; app.alertCfg = r.config; } catch { return; }
  renderAlerts();
  if (app.view) renderMarket();
  if (fresh && fresh.length) notifyAlerts(fresh);
}
function alertSort(a, b) {
  return a.pair.localeCompare(b.pair) || (a.kind === "follow" ? -1 : b.kind === "follow" ? 1 : 0) ||
    app.tenors.indexOf(a.tenor) - app.tenors.indexOf(b.tenor) || Object.keys(MARK_LABEL).indexOf(a.mark) - Object.keys(MARK_LABEL).indexOf(b.mark);
}
function alertText(a) {
  if (a.kind !== "market") return a.msg;
  const put = a.pair === app.current ? app.draft?.rr_put : app.pairs.find((p) => p.pair === a.pair)?.rr_put, sg = rrSign(a.mark, put);
  return `${a.tenor} ${MARK_LABEL[a.mark]}${sg < 0 ? " (P−C)" : ""}: ours ${fmt(sg * a.value, 2)}, FMD ${fmt(sg * a.mkt, 2)} (${sg * a.dev > 0 ? "+" : ""}${fmt(sg * a.dev, 2)}, tol ${fmt(a.tol, 2)})`;
}
function renderAlerts() {
  const list = [...app.alerts].sort(alertSort);
  const fresh = list.filter((a) => !a.acked).length;
  const btn = $("#alerts-btn");
  btn.textContent = `⚠ ${list.length}`;
  btn.classList.toggle("on", list.length > 0); btn.classList.toggle("new", fresh > 0);
  btn.title = list.length ? `${list.length} market alert(s), ${fresh} not acknowledged` : "No market alerts";
  document.title = fresh ? `(${fresh}) VolMarker` : "VolMarker";
  const basis = app.alertCfg?.basis === "range" ? "outside FMD bid/offer" : "vs FMD mid";
  $("#al-sub").textContent = list.length ? `${list.length} · ${basis}` : basis;
  const pairs = [...new Set(list.map((a) => a.pair))];
  $("#al-list").innerHTML = pairs.length ? pairs.map((p) => {
    const rows = list.filter((a) => a.pair === p);
    return `<div class="al-pair" data-pair="${p}">${p}<span class="muted">${rows.length}</span><span class="spacer"></span>` +
      (rows.some((a) => !a.acked) ? `<button class="ghost" data-ackpair="${p}">Ack ${p}</button>` : "") + `</div>` +
      rows.map((a) => `<div class="al-row ${a.acked ? "acked" : ""} ${a.kind}"><span>${esc(alertText(a))}</span>` +
        `<span class="when" title="since ${esc(localTime(a.since))}">${age((Date.now() - new Date(a.since)) / 60000)}</span>` +
        (a.acked ? "" : `<button class="ghost" data-ack="${esc(a.key)}">Ack</button>`) + `</div>`).join("");
  }).join("") : `<div class="muted">No alerts. Published surfaces are within tolerance of FMD.</div>`;
}
function notifyOn() { try { return localStorage.getItem("vm-notify") === "1"; } catch { return false; } }
function notifyAlerts(list) {
  if (!notifyOn() || !("Notification" in window) || Notification.permission !== "granted") return;
  const pairs = [...new Set(list.map((a) => a.pair))];
  for (const p of pairs.slice(0, 4)) {
    const rows = list.filter((a) => a.pair === p).sort(alertSort);
    const n = new Notification(`VolMarker · ${p}`, {
      body: rows.slice(0, 4).map(alertText).join("\n") + (rows.length > 4 ? `\n+${rows.length - 4} more` : ""), tag: `vm-${p}`,
    });
    n.onclick = () => { window.focus(); selectPair(p); n.close(); };
  }
}
async function ackAlerts(body) {
  try { await api("/api/alerts/ack", body); } catch {}
  loadAlerts(); loadPairs();
}
function wireAlerts() {
  const panel = $("#alerts-panel");
  $("#alerts-btn").onclick = () => { panel.hidden = !panel.hidden; if (!panel.hidden) renderAlerts(); };
  $("#al-close").onclick = () => (panel.hidden = true);
  $("#al-ackall").onclick = () => ackAlerts({ keys: app.alerts.map((a) => a.key) });
  $("#al-list").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b && b.dataset.ack) return ackAlerts({ keys: [b.dataset.ack] });
    if (b && b.dataset.ackpair) return ackAlerts({ pair: b.dataset.ackpair });
    const h = e.target.closest(".al-pair"); if (h) selectPair(h.dataset.pair);
  });
  const cb = $("#al-notify");
  cb.checked = notifyOn() && "Notification" in window && Notification.permission === "granted";
  cb.onchange = async () => {
    let on = cb.checked;
    if (on && "Notification" in window && Notification.permission !== "granted")
      on = (await Notification.requestPermission()) === "granted";
    cb.checked = on;
    try { localStorage.setItem("vm-notify", on ? "1" : "0"); } catch {}
  };
  setInterval(() => { if (!panel.hidden) renderAlerts(); }, 30000);   // refresh the ages
}

// ---------------------------------------------------------------- wiring
function wire() {
  wireSidebar();
  $("#filter").addEventListener("input", () => renderPairs());
  $("#filter").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { const first = $(".prow", $("#pairlist")); if (first) selectPair(first.dataset.pair); }
  });

  const grid = $("#grid");
  grid.addEventListener("input", (e) => { if (e.target.matches("input.cell")) onCellInput(e.target); });
  grid.addEventListener("change", (e) => {
    if (e.target.matches("input.cell")) renderGrid();
  });
  grid.addEventListener("change", (e) => {
    if (!e.target.dataset.lock) return;
    app.draft[e.target.dataset.lock] = e.target.checked;
    changed(); renderGrid(); renderMarket();
  });
  grid.addEventListener("focusin", (e) => { if (e.target.matches("input.cell")) e.target.select(); });
  grid.addEventListener("focusout", () => setTimeout(renderGrid, 0));
  grid.addEventListener("keydown", (e) => {
    const el = e.target; if (!el.matches("input.cell")) return;
    if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); moveFocus(el, 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveFocus(el, -1); }
    else if (e.key === "Escape") {
      const t = el.dataset.t, f = el.dataset.f;
      revertDraftValue(t, f === "psprd" ? "atm" : f);
      el.value = fmt(f === "psprd" ? parentSpread(t, app.draft.tenors[t].atm) : app.draft.tenors[t][f], 3); changed();
    }
  });

  grid.addEventListener("click", (e) => {
    const el = e.target.closest(".mq"); if (!el || !el.textContent) return;
    const q = mktQuote(el.dataset.t, el.dataset.m); if (!q) return;
    setFromMarket(el.dataset.t, el.dataset.m, q.mid); changed(); renderGrid();
  });
  $("#mkt-show").onchange = () => { renderMarket(); renderTermChart(); };
  $("#mkt-brand").onchange = () => loadMarket();
  const followBlock = () => {
    if (!app.draft.follow_market && !app.draft.excel) return false;
    showBanner(app.draft.excel ? "This pair is marked in Excel: the marks come from its export file. Untick Marked in Excel in Setup to mark here."
      : "This pair follows FMD: ATM, RR and BF come from the feed. Untick Follow FMD in Setup to mark by hand.", [["OK", () => hideBanner()]]);
    return true;
  };
  $("#sh-mkt").onclick = () => {
    if (followBlock()) return;
    const f = $("#sh-field").value, m = MKT_FIELDS[f];
    if (!m) { showBanner("Market levels exist for ATM, 25D/10D RR and 25D/10D BF only.", [["OK", () => hideBanner()]]); return; }
    const i0 = app.tenors.indexOf($("#sh-from").value), i1 = app.tenors.indexOf($("#sh-to").value);
    let n = 0;
    for (const t of app.tenors.slice(Math.min(i0, i1), Math.max(i0, i1) + 1)) {
      const q = mktQuote(t, m); if (q && q.mid != null && !q.stale) { setFromMarket(t, m, q.mid); n++; }
    }
    if (n) { changed(); renderGrid(); }
    else showBanner("No fresh market quotes for that field and range.", [["OK", () => hideBanner()]]);
  };
  $("#dl-calib").onclick = calibrateDailies;
  $("#sh-mktall").onclick = () => {
    if (followBlock()) return;
    const i0 = app.tenors.indexOf($("#sh-from").value), i1 = app.tenors.indexOf($("#sh-to").value);
    let n = 0;
    for (const t of app.tenors.slice(Math.min(i0, i1), Math.max(i0, i1) + 1))
      for (const m of ["atm", "rr25", "bf25", "rr10", "bf10"]) {      // 25D before 10D (factor calibration)
        const q = mktQuote(t, m); if (q && q.mid != null && !q.stale) { setFromMarket(t, m, q.mid); n++; }
      }
    if (n) { changed(); renderGrid(); }
    else showBanner("No fresh market quotes in that range.", [["OK", () => hideBanner()]]);
  };
  setInterval(async () => { try { renderFeed(await api("/api/market/status")); } catch {} }, 10000);
  $("#publish").onclick = publish;
  $("#discard").onclick = () => reloadView();
  $("#load-fa").onclick = loadExportFile;
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); publish(); }
    if (e.key === "/" && !e.target.matches("input, select, textarea")) { e.preventDefault(); $("#filter").focus(); }
  });
  window.addEventListener("beforeunload", (e) => { if (app.dirty) { e.preventDefault(); e.returnValue = ""; } });

  // shift toolbar
  const shiftable = [["atm", "ATM"], ["spread", "ATM spread"], ["rr25", "25D RR"], ["rr10", "10D RR (unlocked)"],
    ["bf25", "25D BF"], ["bf10", "10D BF (unlocked)"],
    ["corr", "Correlation"], ["skew_atm", "Skew ATM"], ["skew_rr", "Skew RR"], ["skew_bf", "Skew BF"]];
  $("#sh-field").innerHTML = shiftable.map(([f, l]) => `<option value="${f}">${l}</option>`).join("");
  $("#sh-apply").onclick = () => {
    const f = $("#sh-field").value, amt = Number($("#sh-amt").value);
    const i0 = app.tenors.indexOf($("#sh-from").value), i1 = app.tenors.indexOf($("#sh-to").value);
    if (!Number.isFinite(amt)) return;
    if (FOLLOW_FIELDS.includes(f) && followBlock()) return;
    for (const t of app.tenors.slice(Math.min(i0, i1), Math.max(i0, i1) + 1)) {
      const row = app.draft.tenors[t];
      if (f === "corr") { if (row.corr !== null) row.corr = Math.max(-1, Math.min(1, row.corr + amt)); continue; }
      if (row[f] === null) continue;
      if ((f === "rr10" && app.draft.lock_rr10f) || (f === "bf10" && app.draft.lock_bf10f)) continue;
      row[f] = Math.round((row[f] + rrSign(f) * amt) * 1e6) / 1e6;      // amounts in the RR convention shown
      if (f === "atm" && movesWithBases()) row.corr = null;
    }
    changed(); renderGrid();
  };

  // skew expiry
  $("#skew-exp").addEventListener("change", (e) => {
    app.draft.skew_expiry = e.target.value ? new Date(e.target.value).toISOString().replace(/\.\d+Z$/, "+00:00") : null;
    changed();
  });
  $("#skew-eod").onclick = () => {
    const d = new Date(); d.setHours(17, 0, 0, 0);
    app.draft.skew_expiry = d.toISOString().replace(/\.\d+Z$/, "+00:00"); renderSkewExpiry(); changed();
  };
  $("#skew-clear").onclick = () => {
    for (const t of app.tenors) Object.assign(app.draft.tenors[t], { skew_atm: 0, skew_rr: 0, skew_bf: 0 });
    app.draft.skew_expiry = null; renderSkewExpiry(); renderGrid(); changed();
  };

  // tabs
  $$(".tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
  $("#su-save").onclick = saveSettings;

  // setup
  ["#su-mode", "#su-b1", "#su-b2", "#su-key", "#su-group", "#su-spref", "#su-cut", "#su-dc", "#su-atm", "#su-follow", "#su-excel", "#su-follow-parent", "#su-follow-bases", "#su-alerts-off"].forEach((id) => $(id).addEventListener("change", onSetupChange));
  $("#su-tol").addEventListener("change", (e) => {
    const m = e.target.dataset.m; if (!m) return;
    const txt = e.target.value.trim(), v = Number(txt);
    const tol = { ...(app.draft.alert_tol || {}) };
    if (txt === "" || !(v > 0)) delete tol[m]; else tol[m] = v;
    app.draft.alert_tol = Object.keys(tol).length ? tol : null;
    renderSetup(); changed();
  });

  // history restore
  $("#history").addEventListener("click", (e) => {
    if (!e.target.matches(".restore")) return;
    const r = app.historyRows[+e.target.dataset.i];
    app.draft = pick(r); buildGrid(); changed();
    $("#comment").value = `restore v${r.version}`;
    $$(".tabs button")[0].click();
  });

  // new pair
  const dlg = $("#dlg-new");
  $("#newpair").onclick = () => { $("#np-err").textContent = ""; $("#form-new").reset(); npMode(); dlg.showModal(); $("#np-pair").focus(); };
  const npMode = () => $$(".np-derived").forEach((el) => (el.hidden = $("#np-mode").value !== "derived"));
  $("#np-mode").onchange = npMode;
  $("#np-ok").onclick = async (e) => {
    e.preventDefault();
    const mode = $("#np-mode").value;
    try {
      const rec = await api("/api/pairs", {
        pair: $("#np-pair").value, mode, key: $("#np-key").checked, group: $("#np-group").value.trim() || null,
        bases: mode === "derived" ? [$("#np-b1").value, $("#np-b2").value] : null,
      });
      dlg.close(); await loadPairs(); selectPair(rec.pair);
    } catch (err) { $("#np-err").textContent = err.message; }
  };

  // theme
  const saved = (() => { try { return localStorage.getItem("vm-theme"); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  $("#theme").onclick = () => {
    const cur = document.documentElement.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    const next = cur === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("vm-theme", next); } catch {}
    if (app.view) { renderTermChart(); if (app.tab === "weights") renderCurve(); if (app.tab === "s3d") render3D(); }
  };
  let rt = null;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (!app.view) return; renderTermChart(); if (app.tab === "s3d") render3D(); }, 100); });
  wire3d();
  wireAlerts();
  wireWeights();
}

async function init() {
  app.me = await api("/api/me");
  app.tenors = app.me.tenors;
  $("#me").textContent = app.me.user;
  $("#datadir").textContent = app.me.data;
  $("#datadir").title = "Exports: " + Object.entries(app.me.exports || {}).map(([k, v]) => `${k} ${v}`).join(" · ");
  if (app.me.config_error) {
    const w = document.createElement("span");
    w.className = "warn"; w.textContent = " · config.json problem (hover)"; w.title = app.me.config_error;
    $("#datadir").after(w);
  }
  const opts = app.tenors.map((t) => `<option>${t}</option>`).join("");
  $("#sh-from").innerHTML = opts; $("#sh-to").innerHTML = opts;
  $("#sh-from").value = "1M"; $("#sh-to").value = "1Y";
  try { app.prefs = await api("/api/prefs"); } catch { app.prefs = {}; }
  wire();
  await loadPairs();
  loadAlerts();
  connect();
  const h = location.hash.slice(1);
  if (h && app.pairs.some((p) => p.pair === h)) selectPair(h);
}
init();
