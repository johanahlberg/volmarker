"use strict";
// VolMarker front end - plain JS, no dependencies.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const clone = (x) => JSON.parse(JSON.stringify(x));
const STATE_KEYS = ["pair", "mode", "bases", "key", "skew_expiry", "weights", "atm_type", "lock_rr10f", "lock_bf10f",
  "follow_market", "alert_tol", "alerts_off", "group", "spread_ref", "spread_off", "cut", "dc_factor", "tenors"];
const MKT_FIELDS = { atm: "atm", rr25: "rr25", rr10: "rr10", bf25: "bf25", bf10: "bf10" };
const FOLLOW_FIELDS = ["atm", "rr25", "rr10f", "rr10", "bf25", "bf10f", "bf10"];   // owned by the feed on Follow FMD pairs
const MARK_LABEL = { atm: "ATM", rr25: "25D RR", rr10: "10D RR", bf25: "25D BF", bf10: "10D BF" };
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
  spread: null,      // Spread tab: reference pair data
  spreadView: {},    // reference picked in the Spread tab this session, per pair (the default is the pair's spread_ref)
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
async function selectPair(pair) {
  if (app.dirty && pair !== app.current && !confirmDiscard()) return;
  if (pair !== app.current) { app.defEvents = []; app.testRows = []; }
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
  await loadMarket(false);
  buildGrid(); renderAll();
  if (app.tab === "weights") renderWeights(true);
  if (app.tab === "history") renderHistory();
  if (app.tab === "setup") renderSetup();
  if (app.tab === "spread") { if (app.spread?.pair === app.current && app.spread.ref === spreadRef()) { renderSpread(); loadSpreadHistory(); } else loadSpreadRef(); }
}
function pick(rec) {
  const s = {};
  for (const k of STATE_KEYS) s[k] = clone(rec[k] ?? null);
  s.weights = normaliseWeights(s.weights);
  for (const k of ["lock_rr10f", "lock_bf10f"]) s[k] = s[k] === null ? true : !!s[k];
  s.atm_type = s.atm_type || "DNS";
  s.follow_market = !!s.follow_market; s.alerts_off = !!s.alerts_off;
  for (const r of Object.values(s.tenors)) {   // records from before the 10D values were stored
    if (r.rr10 == null) r.rr10 = r.rr25 * r.rr10f;
    if (r.bf10 == null) r.bf10 = r.bf25 * r.bf10f;
  }
  return s;
}

// ---------------------------------------------------------------- grid
const COLS = [
  { f: "corr", label: "Corr", dp: 3, derived: true, group: "" },
  { f: "pin", label: "Pin", derived: true, check: true },
  { f: "atm", label: "ATM", dp: 3, group: "ATM", sep: true },
  { f: "spread", label: "Spread", dp: 2 },
  { calc: (r) => r.quoted.atm_bid, label: "Bid", dp: 3, q: true, title: "Fenics ATM bid (incl. skew)" },
  { calc: (r) => r.quoted.atm_offer, label: "Offer", dp: 3, q: true, title: "Fenics ATM offer (incl. skew)" },
  { f: "rr25", label: "25D", dp: 3, group: "Risk reversal", sep: true },
  { f: "rr10f", label: "10D fac", dp: 3, lock: "lock_rr10f", role: "factor" },
  { f: "rr10", label: "10D", dp: 3, lock: "lock_rr10f", role: "tenD" },
  { f: "bf25", label: "25D", dp: 3, group: "Butterfly", sep: true },
  { f: "bf10f", label: "10D fac", dp: 3, lock: "lock_bf10f", role: "factor" },
  { f: "bf10", label: "10D", dp: 3, lock: "lock_bf10f", role: "tenD" },
  { f: "skew_atm", label: "ATM", dp: 2, group: "Skew → Fenics only", sep: true, skew: true },
  { f: "skew_rr", label: "RR", dp: 2, skew: true },
  { f: "skew_bf", label: "BF", dp: 2, skew: true },
];
function visibleCols() { return COLS.filter((c) => !c.derived || app.draft.mode === "derived"); }

function buildGrid() {
  const cols = visibleCols();
  const groups = [];
  for (const c of cols) {
    if (c.group !== undefined || !groups.length) groups.push({ label: c.group || "", n: 1, sep: c.sep });
    else groups[groups.length - 1].n++;
  }
  let h = `<thead><tr><th class="tn"></th>${groups.map((g) => `<th class="grp" colspan="${g.n}">${esc(g.label)}</th>`).join("")}</tr>`;
  const lockTitle = "Ticked: type the factor; 10D = 25D × factor (10D locked).\nUnticked: type the 10D quote; factor = 10D / 25D.";
  h += `<tr><th class="tn">Tenor</th>${cols.map((c) => `<th class="${c.sep ? "sep" : ""}" title="${esc(c.title || "")}">` +
    (c.role === "factor" ? `<label class="lock" title="${esc(lockTitle)}"><input type="checkbox" data-lock="${c.lock}"> lock</label>` : "") +
    `${c.label}</th>`).join("")}</tr></thead><tbody>`;
  for (const t of app.tenors) {
    h += `<tr data-t="${t}"><td class="tn">${t}</td>`;
    cols.forEach((c, i) => {
      const cls = c.sep ? "sep" : "";
      if (c.check) h += `<td class="pin ${cls}"><input type="checkbox" data-t="${t}" data-f="${c.f}" title="Keep the typed ATM fixed; ignore the bases"></td>`;
      else if (c.calc) h += `<td class="calc ${c.q ? "q" : ""} ${cls}" data-t="${t}" data-c="${i}"></td>`;
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
  for (const el of $$("#grid tbody input")) {
    const t = el.dataset.t, f = el.dataset.f;
    const v = app.draft.tenors[t][f];
    const changed = !same(v, publishedRow(t)[f]);
    if (el.type === "checkbox") { el.checked = !!v; el.parentElement.classList.toggle("dirty", changed); continue; }
    const col = cols.find((c) => c.f === f);
    if (document.activeElement !== el) el.value = col.skew && !v ? "" : fmt(v, col.dp);
    el.classList.toggle("dirty", changed);
    const fol = app.draft.follow_market && FOLLOW_FIELDS.includes(f);
    if (col.lock || fol || el.readOnly) {
      const lockRo = !!col.lock && (col.role === "factor") !== !!app.draft[col.lock];
      const ro = fol || lockRo;
      el.readOnly = ro; el.tabIndex = ro ? -1 : 0;
      el.classList.toggle("ro", ro);
      el.title = fol ? "Follows FMD: published by the feed (untick Follow FMD in Setup to mark by hand)" :
        lockRo ? (col.role === "factor" ? "Calibrated from the 10D quote (untick lock to edit)" : "25D × factor (untick lock to type the 10D)") : "";
    }
    el.classList.toggle("follow", fol);
    if (f === "atm") el.classList.toggle("derived", app.draft.mode === "derived" && !app.draft.tenors[t].pin);
    if (f === "atm" && app.draft.mode === "derived") {
      el.title = app.draft.tenors[t].pin ? "Pinned: kept as typed" : "Derived from bases; type a value to calibrate the correlation";
    }
  }
  for (const td of $$("#grid td.calc")) {
    const col = cols[+td.dataset.c];
    td.textContent = fmt(col.calc(resolved[td.dataset.t]), col.dp);
  }
}

function onCellInput(el) {
  const t = el.dataset.t, f = el.dataset.f, row = app.draft.tenors[t];
  if (el.type === "checkbox") { row[f] = el.checked; return changed(); }
  const txt = el.value.trim();
  const v = txt === "" ? null : Number(txt);
  if (txt !== "" && !Number.isFinite(v)) { el.classList.add("dirty"); return; }
  setDraftValue(t, f, v);
  changed();
}
function setDraftValue(t, f, v) {
  const row = app.draft.tenors[t];
  if (f === "atm" && app.draft.mode === "derived" && !row.pin) {
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
  $("#publish").disabled = !app.dirty;
  $("#discard").disabled = !app.dirty;
  $("#publish").textContent = app.dirty ? "Publish ●" : "Publish";
}

async function publish() {
  if (!app.dirty) return;
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
    (app.view.dependents.length ? ` · feeds ${app.view.dependents.join(", ")}` : "");
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
    const t = td.dataset.t, m = td.dataset.m, q = mktQuote(t, m), el = $(".mq", td);
    td.classList.remove("out");
    if (!q) { el.textContent = ""; el.title = ""; continue; }
    const two = (v) => (v == null ? "–" : (Math.abs(v) < 0.005 ? 0 : v).toFixed(2));
    el.textContent = q.bid != null || q.offer != null ? `${two(q.bid)} / ${two(q.offer)}` : `mid ${two(q.mid)}`;
    if (q.alt_atm) el.textContent += "*";
    el.classList.toggle("stale", !!q.stale);
    const v = res[t].mid[m];
    let side = "";
    if (v != null && q.bid != null && v < q.bid - 1e-9) side = `below bid by ${(q.bid - v).toFixed(3)}`;
    if (v != null && q.offer != null && v > q.offer + 1e-9) side = `above offer by ${(v - q.offer).toFixed(3)}`;
    td.classList.toggle("out", !!side);
    const al = app.alerts.find((a) => a.key === `${app.current}|${t}|${m}`);
    td.classList.toggle("alert", !!al);
    el.title = `${q.brand}${q.ptype ? " " + q.ptype : ""} · mid ${fmt(q.mid, 3)}` +
      (q.age_min != null ? ` · ${age(q.age_min)} old` : "") + (q.alt_atm ? ` · *other ATM convention than ${app.draft.atm_type}` : "") +
      (side ? `\nYour mark is ${side}` : "") +
      (al ? `\nALERT since ${localTime(al.since)}: published ${fmt(al.value, 2)} vs FMD ${fmt(al.mkt, 2)} (${al.dev > 0 ? "+" : ""}${fmt(al.dev, 2)}, tol ${fmt(al.tol, 2)})` : "") +
      (app.draft.follow_market ? "" : "\nClick to set the draft to the broker mid");
  }
}
function setFromMarket(t, m, v, force = false) {
  const row = app.draft.tenors[t];
  if (v == null || (app.draft.follow_market && !force)) return;
  if (m === "atm") { row.atm = v; if (app.draft.mode === "derived" && !row.pin) row.corr = null; }
  else if (m === "rr10" || m === "bf10") {
    const w = m.slice(0, 2);
    row[m] = v;                                     // the 10D quote itself (input when unlocked or following FMD)
    if (Math.abs(row[`${w}25`]) > 1e-12) row[`${w}10f`] = v / row[`${w}25`];   // locked: calibrate the factor
  } else row[m] = v;
}

function renderFollow() {
  const bar = $("#followbar"), d = app.draft, pub = app.published;
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
      html += `<br>Held: ${j.tenor} ${MARK_LABEL[j.mark]} ${fmt(j.from, 2)} → ${fmt(j.to, 2)}, taken in ${j.accept_in}s unless it reverts`;
    html += " · You keep spread, skew and event weights.";
  }
  bar.className = "followbar " + cls; bar.innerHTML = html;
}

function renderSpreadTab() {      // pairs set to "no spread" don't get the tab
  const off = !!app.draft?.spread_off, btn = $('.tabs button[data-tab="spread"]');
  btn.hidden = off;
  if (off && app.tab === "spread") $('.tabs button[data-tab="surface"]').click();
}
function renderAll() {
  renderSpreadTab();
  renderHeader(); renderFollow(); renderGrid(); renderMarket(); renderWarnings(); renderSkewExpiry(); renderTermChart(); updateButtons();
  if (app.tab === "s3d") render3D();
  if (app.tab === "spread") renderSpreadValues();
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

// ---------------------------------------------------------------- spread tab (this pair − a reference pair)
const SP_MARKS = ["atm", "rr25", "rr10", "bf25", "bf10"];
const havePair = (p) => app.pairs.some((x) => x.pair === p);
function spreadRef() {
  const v = app.spreadView[app.current];
  return v !== undefined && (v === "" || havePair(v)) ? v : spreadDefault();
}
function spreadDefault() {
  // the pair's setting (in the draft, so "Make default" shows at once) ...
  const set = app.draft?.pair === app.current ? app.draft.spread_ref : null;
  if (set && havePair(set)) return set;
  // ... else a pair from the same sidebar group, key and marked pairs first (EURUSD for USDCHF)
  const me = app.pairs.find((p) => p.pair === app.current);
  if (!me) return "";
  const rank = (p) => (p.key ? 0 : 2) + (p.mode === "marked" ? 0 : 1);
  const cands = orderedPairs(groupOf(me), app.pairs).filter((p) => p.pair !== app.current);
  return cands.sort((a, b) => rank(a) - rank(b))[0]?.pair || "";
}
function mktMid(market, t, m) {
  const q = market?.view?.[t]?.[m];
  return q && q.mid != null && !q.alt_atm && market.feed?.state !== "off" ? q : null;
}
async function loadSpreadRef() {
  const pair = app.current, ref = spreadRef();
  app.spread = { pair, ref, view: null, market: null, hist: null };
  if (ref) {
    const brand = encodeURIComponent($("#mkt-brand").value);
    const [v, m] = await Promise.all([api(`/api/pairs/${ref}`).catch(() => null),
      api(`/api/pairs/${ref}/market?brand=${brand}`).catch(() => null)]);
    if (app.spread.pair !== pair || app.spread.ref !== ref) return;
    Object.assign(app.spread, { view: v, market: m });
  }
  renderSpread();
  loadSpreadHistory();
}
function renderSpread() {
  if (app.tab !== "spread" || !app.view) return;
  const sp = app.spread || {}, ref = sp.ref;
  $("#sp-pair").textContent = app.current;
  const chips = relatedPairs();
  $("#sp-chips").innerHTML = chips.map((p) => `<button class="chip${p === ref ? " on" : ""}" data-ref="${p}"` +
    ` title="${p === spreadDefault() ? "Default reference for " + app.current : "Show " + app.current + " − " + p}">${p}${p === spreadDefault() ? " ★" : ""}</button>`).join("");
  $("#sp-ref").innerHTML = `<option value="">${chips.length ? "Other…" : "— choose —"}</option>` +
    app.pairs.filter((p) => p.pair !== app.current).map((p) => `<option>${p.pair}</option>`).join("");
  $("#sp-ref").value = ref && !chips.includes(ref) ? ref : "";
  const def = spreadDefault(), btn = $("#sp-default");
  btn.hidden = !ref || ref === def;
  btn.textContent = `Make ${ref} the default`;
  btn.title = `Set ${ref} as ${app.current}'s spread reference for the whole desk (into the draft; publish to save)`;
  if (!ref || !sp.view) {
    $("#sp-note").textContent = ref ? "Loading…" : "Pick a reference pair";
    $("#sp-grid").innerHTML = ""; $("#sp-chart").innerHTML = ""; $("#sp-hist").innerHTML = ""; $("#sp-legend").innerHTML = "";
    $("#sp-h-note").textContent = "";
    return;
  }
  const r = sp.view.record;
  $("#sp-note").textContent = `${ref} v${r.version} at ${localTime(r.ts)}`;
  let h = `<thead><tr><th class="tn"></th>${SP_MARKS.map((m) => `<th class="grp" colspan="3">${MARK_LABEL[m]}</th>`).join("")}</tr>`;
  h += `<tr><th class="tn">Tenor</th>${SP_MARKS.map(() => `<th class="sep">${ref}</th><th>Spread</th><th title="FMD ${app.current} − FMD ${ref}">FMD</th>`).join("")}</tr></thead><tbody>`;
  for (const t of app.tenors) {
    h += `<tr><td class="tn">${t}</td>` + SP_MARKS.map((m) =>
      `<td class="calc sep" data-t="${t}" data-m="${m}" data-k="ref"></td>` +
      `<td><input class="cell" data-t="${t}" data-f="${m}" inputmode="decimal" autocomplete="off"></td>` +
      `<td class="calc fmd" data-t="${t}" data-m="${m}" data-k="fmd"></td>`).join("") + `</tr>`;
  }
  $("#sp-grid").innerHTML = h + "</tbody>";
  renderSpreadValues();
}
// pairs sharing a currency with this one: bases and the default first, then the same group, then key pairs
function relatedPairs() {
  const [c1, c2] = [app.current.slice(0, 3), app.current.slice(3)], me = app.pairs.find((p) => p.pair === app.current);
  const bases = app.draft?.bases || [], def = spreadDefault();
  const score = (p) => (p.pair === def ? 0 : 10) + (bases.includes(p.pair) ? 0 : 5) + (me && groupOf(p) === groupOf(me) ? 0 : 2) + (p.key ? 0 : 1);
  const out = app.pairs.filter((p) => p.pair !== app.current && [c1, c2].some((c) => p.pair.includes(c)))
    .sort((a, b) => score(a) - score(b) || a.pair.localeCompare(b.pair)).slice(0, 8).map((p) => p.pair);
  if (def && !out.includes(def)) out.unshift(def);
  return out;
}
function spreadAt(t, m, resolved) {
  const a = resolved?.[t]?.mid?.[m], b = app.spread?.view?.resolved?.[t]?.mid?.[m];
  return a == null || b == null ? null : a - b;
}
function spreadTol(t, m) {
  const c = app.alertCfg; if (!c) return null;
  return ((app.draft.alert_tol || {})[m] || c.tolerance[m]) * ((c.tenor_scale || {})[t] || 1);
}
function renderSpreadValues() {
  const sp = app.spread;
  if (app.tab !== "spread" || !sp?.view || sp.pair !== app.current) return;
  const res = app.preview.resolved, rres = sp.view.resolved, d = app.draft;
  for (const el of $$("#sp-grid input")) {
    const t = el.dataset.t, m = el.dataset.f, v = spreadAt(t, m, res);
    if (document.activeElement !== el) el.value = fmt(v, 3);
    const pub = spreadAt(t, m, app.view.resolved);
    el.classList.toggle("dirty", !same(v, pub));
    const ro = d.follow_market || (m === "rr10" && d.lock_rr10f) || (m === "bf10" && d.lock_bf10f) || rres[t].mid[m] == null;
    el.readOnly = ro; el.tabIndex = ro ? -1 : 0; el.classList.toggle("ro", ro);
    el.title = `${app.current} ${fmt(res[t].mid[m], 3)} − ${sp.ref} ${fmt(rres[t].mid[m], 3)}` +
      (pub != null && !same(v, pub) ? ` · published spread ${fmt(pub, 3)}` : "") +
      (d.follow_market ? "\nFollows FMD: set by the feed" : ro && rres[t].mid[m] != null ? "\nUntick the 10D factor lock to type the 10D" : "");
  }
  for (const td of $$("#sp-grid td.calc")) {
    const t = td.dataset.t, m = td.dataset.m;
    if (td.dataset.k === "ref") { td.textContent = fmt(rres[t].mid[m], 3); continue; }
    const a = mktMid(app.market, t, m), b = mktMid(sp.market, t, m);
    const f = a && b ? a.mid - b.mid : null, ours = spreadAt(t, m, res), tol = spreadTol(t, m);
    td.textContent = fmt(f, 3);
    const off = f != null && ours != null && tol != null && Math.abs(ours - f) > tol;
    td.classList.toggle("off", off);
    td.classList.toggle("stale", !!(a?.stale || b?.stale));
    td.title = f == null ? "No FMD quote for one of the pairs" :
      `FMD ${app.current} ${fmt(a.mid, 3)} − ${sp.ref} ${fmt(b.mid, 3)}` + (ours != null ? ` · yours ${ours - f > 0 ? "+" : ""}${fmt(ours - f, 3)} vs FMD` : "") +
      (tol != null ? ` (tol ${fmt(tol, 2)})` : "") + (a.stale || b.stale ? " · stale quote" : "");
  }
  renderSpreadChart();
}
function renderSpreadChart() {
  const T = app.tenors, m = $("#sp-mark").value, sp = app.spread;
  $("#sp-ts-title").textContent = `${MARK_LABEL[m]} spread by tenor`;
  const series = [{ name: app.dirty ? "Draft" : "Spread", color: "var(--s1)", values: T.map((t) => spreadAt(t, m, app.preview.resolved)), dots: true }];
  if (app.dirty) series.push({ name: "Published", color: "var(--muted)", dash: true, values: T.map((t) => spreadAt(t, m, app.view.resolved)) });
  const fv = T.map((t) => { const a = mktMid(app.market, t, m), b = mktMid(sp.market, t, m); return a && b ? a.mid - b.mid : null; });
  if (fv.some((v) => v != null)) series.push({ name: "FMD", color: "var(--s5)", noLine: true, label: false, values: fv });
  $("#sp-legend").innerHTML = series.map((s) => `<span><i class="${s.dash ? "dash" : ""}" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("");
  lineChart($("#sp-chart"), { n: T.length, xLabel: (i) => `${T[i]} · ${app.current} − ${sp.ref}`, xTicks: T.map((t, i) => [i, t]), series, yLabel: "Spread by tenor" });
}
let spHistSeq = 0, spHistTimer = null;
function loadSpreadHistory(delay = 0) {
  clearTimeout(spHistTimer);
  spHistTimer = setTimeout(async () => {
    const sp = app.spread;
    if (app.tab !== "spread" || !sp?.ref || !sp.view) return;
    const seq = ++spHistSeq;
    let h;
    try {
      h = await api(`/api/pairs/${sp.pair}/spread?ref=${sp.ref}&tenor=${$("#sp-tenor").value}&days=${$("#sp-days").value}`);
    } catch (e) { h = { points: [], error: e.message }; }
    if (seq !== spHistSeq || app.spread !== sp) return;
    sp.hist = h; renderSpreadHistory();
  }, delay);
}
function renderSpreadHistory() {
  const sp = app.spread, h = sp?.hist, m = $("#sp-mark").value;
  if (!h) return;
  $("#sp-h-title").textContent = `${$("#sp-tenor").value} ${MARK_LABEL[m]} spread, published`;
  const pts = h.points || [];
  $("#sp-h-note").textContent = h.error ? h.error :
    `${app.current} − ${sp.ref} as published at each moment · ${h.versions} version(s) in the window` + (h.truncated ? " · older versions not read" : "");
  if (!pts.length) { $("#sp-hist").innerHTML = `<div class="muted" style="padding:20px">No history in this window</div>`; return; }
  const day = (iso) => new Date(iso).toDateString(), ticks = [];
  pts.forEach(([ts], i) => {
    if (i && day(ts) !== day(pts[i - 1][0])) ticks.push([i, new Date(ts).toLocaleDateString(undefined, { weekday: "short", day: "numeric" })]);
  });
  if (!ticks.length) ticks.push([0, new Date(pts[0][0]).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })],
    [pts.length - 1, new Date(pts[pts.length - 1][0]).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })]);
  lineChart($("#sp-hist"), {
    n: pts.length, xLabel: (i) => localTime(pts[i][0]), xTicks: ticks, yLabel: "Spread history",
    series: [{ name: "Spread", color: "var(--s1)", values: pts.map((p) => p[1][m]), label: false, dots: pts.length < 40 }],
  });
}
function wireSpread() {
  const pick = (ref) => { app.spreadView[app.current] = ref; loadSpreadRef(); };
  $("#sp-ref").onchange = () => { if ($("#sp-ref").value) pick($("#sp-ref").value); };
  $("#sp-chips").addEventListener("click", (e) => { const b = e.target.closest(".chip"); if (b) pick(b.dataset.ref); });
  $("#sp-hide").onclick = () => {
    app.draft.spread_off = true;
    changed(); renderSpreadTab();
    showBanner(`No Spread tab for ${app.current} in the draft. Publish to save it for the desk; switch it back under Setup → Spread reference.`, [["OK", () => hideBanner()]]);
  };
  $("#sp-default").onclick = () => {
    app.draft.spread_ref = app.spread.ref; delete app.spreadView[app.current];
    changed(); renderSpread();
    showBanner(`${app.spread.ref} is now ${app.current}'s spread reference in the draft. Publish to save it for the desk.`, [["OK", () => hideBanner()]]);
  };
  $("#sp-mark").onchange = () => { renderSpreadChart(); renderSpreadHistory(); };
  $("#sp-tenor").onchange = $("#sp-days").onchange = () => loadSpreadHistory();
  const g = $("#sp-grid");
  g.addEventListener("input", (e) => {
    const el = e.target; if (!el.matches("input.cell") || el.readOnly) return;
    const t = el.dataset.t, m = el.dataset.f, txt = el.value.trim(), v = Number(txt);
    const ref = app.spread?.view?.resolved?.[t]?.mid?.[m];
    if (txt === "" || !Number.isFinite(v) || ref == null) { el.classList.add("dirty"); return; }
    setDraftValue(t, m, Math.round((ref + v) * 1e6) / 1e6);
    changed();
  });
  g.addEventListener("focusin", (e) => { if (e.target.matches("input.cell")) e.target.select(); });
  g.addEventListener("focusout", () => setTimeout(renderSpreadValues, 0));
  g.addEventListener("keydown", (e) => {
    const el = e.target; if (!el.matches("input.cell")) return;
    const step = e.key === "ArrowDown" || e.key === "Enter" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (step) {
      e.preventDefault();
      const i = app.tenors.indexOf(el.dataset.t) + step;
      const next = i >= 0 && i < app.tenors.length && $(`#sp-grid input[data-t="${app.tenors[i]}"][data-f="${el.dataset.f}"]`);
      if (next) { next.focus(); next.select(); }
    } else if (e.key === "Escape") {
      revertDraftValue(el.dataset.t, el.dataset.f); el.blur(); changed();
    }
  });
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
  $("#cut-note").textContent = cut ? `Expiry cut ${app.draft.cut || "NYC"}: ${cut.time} ${cut.tz.split("/").pop().replace("_", " ")} (Setup)` : "";
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
function testDates() { return ((app.prefs.test_dates || {})[app.current] || []).filter((d) => d > isoToday()); }
function setTestDates(list) {
  (app.prefs.test_dates ||= {})[app.current] = [...new Set(list)].sort();
  savePrefs(); renderTestDates(); loadTestDates();
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
  const dirty = app.dirty;
  $("#td-table").innerHTML = `<thead><tr><th class="tn">Expiry</th><th class="tn">Day</th><th title="Calendar days">Days</th><th title="Weighted days from today">W. days</th><th title="Weight of the expiry day itself">Day wt</th>` +
    `<th>${dirty ? "Draft ATM" : "ATM"}</th>${dirty ? "<th>Published</th><th>Δ</th>" : ""}<th title="Forward vol from the row above">Fwd</th><th class="tn">Events</th><th></th></tr></thead><tbody>` +
    (rows.length ? rows.map((r) => {
      let d = r.atm != null && r.published != null ? r.atm - r.published : null;
      if (d != null && Math.abs(d) < 0.0005) d = 0;
      return `<tr${r.good_day ? "" : ' class="muted" title="Not a good business day for both currencies"'}><td class="tn">${r.date}</td><td class="tn">${r.weekday}</td><td>${r.days}</td>
        <td>${fmt(r.weighted_days, 2)}</td><td>${fmt(r.day_weight, 2)}</td><td><b>${fmt(r.atm, 3)}</b></td>
        ${dirty ? `<td>${fmt(r.published, 3)}</td><td class="${d && Math.abs(d) >= 0.0005 ? "chg" : "muted"}">${d == null ? "" : (d > 0 ? "+" : "") + fmt(d, 3)}</td>` : ""}
        <td>${fmt(r.fwd, 3)}</td><td class="tn small">${esc(r.events.join(", "))}</td>
        <td><button class="ghost td-del" data-date="${r.date}" title="Remove">✕</button></td></tr>`;
    }).join("") : `<tr><td colspan="11" class="muted tn">${esc(err || "Add an expiry date above, e.g. the day of and the day before an event")}</td></tr>`) + "</tbody>";
}
function addEventTestDates() {
  const today = isoToday(), lim = new Date(); lim.setMonth(lim.getMonth() + 3);
  const end = lim.toISOString().slice(0, 10), out = [...testDates()];
  for (const r of eventRows()) {
    if (r.date <= today || r.date > end || !(r.weight > 0)) continue;
    const d = new Date(r.date + "T12:00:00");
    do d.setDate(d.getDate() - 1); while ([0, 6].includes(d.getDay()));
    const before = d.toISOString().slice(0, 10);
    if (before > today) out.push(before);
    out.push(r.date);
  }
  setTestDates(out);
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
  $("#dl-table").innerHTML = `<thead><tr><th class="tn">Expiry</th><th class="tn">Day</th><th>Mkt ATM</th><th>Your ATM</th><th>Mkt fwd</th><th>Your fwd</th><th>RR25</th><th>BF25</th><th>Implied event</th><th>Your event</th><th class="tn">Events</th></tr></thead><tbody>` +
    rows.map((r) => {
      const diff = Math.abs(r.implied_event - r.current_event) >= 0.1;
      return `<tr><td class="tn">${r.n}D</td><td class="tn">${r.weekday} ${r.date}</td><td>${f2(r.mkt_atm)}</td><td>${f2(r.model_atm)}</td>
        <td>${f2(r.mkt_fwd)}</td><td>${f2(r.model_fwd)}</td><td>${f2(r.rr25)}</td><td>${f2(r.bf25)}</td>
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
function renderSetup() {
  const d = app.draft;
  $("#su-mode").value = d.mode;
  $("#su-b1").value = d.bases?.[0] || ""; $("#su-b2").value = d.bases?.[1] || "";
  $("#su-key").checked = !!d.key;
  if (document.activeElement !== $("#su-group")) $("#su-group").value = d.group || "";
  $("#su-spref").innerHTML = `<option value="">Automatic (same group)</option><option value="-">None (no Spread tab)</option>` +
    app.pairs.filter((p) => p.pair !== app.current).map((p) => `<option>${p.pair}</option>`).join("");
  $("#su-spref").value = d.spread_off ? "-" : d.spread_ref || "";
  const cuts = app.me.cuts || {};
  $("#su-cut").innerHTML = Object.entries(cuts).map(([k, c]) => `<option value="${k}">${k} (${c.time} ${c.tz.split("/").pop().replace("_", " ")})</option>`).join("");
  $("#su-cut").value = d.cut || "NYC";
  if (document.activeElement !== $("#su-dc")) $("#su-dc").value = d.dc_factor == null ? "" : fmt(d.dc_factor, 2);
  $("#su-atm").value = d.atm_type || "DNS";
  $$(".su-derived").forEach((el) => (el.hidden = d.mode !== "derived"));
  $("#su-follow").checked = !!d.follow_market;
  $("#su-follow").disabled = d.mode !== "marked";
  $("#su-follow").parentElement.title = d.mode !== "marked" ? "Only marked pairs can follow FMD" : "";
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
  const spv = $("#su-spref").value;
  d.spread_off = spv === "-";
  if (!d.spread_off) d.spread_ref = spv || null;      // switching it off keeps the reference for later
  d.atm_type = $("#su-atm").value;
  if (mode === "derived") {
    if (!bases[0] || !bases[1]) { $$(".su-derived").forEach((el) => (el.hidden = false)); return; }
    if (d.mode !== "derived" || String(d.bases) !== String(bases)) {
      // calibrate correlations to the current ATMs
      for (const t of app.tenors) { d.tenors[t].corr = null; d.tenors[t].pin = false; }
    }
    d.bases = bases;
  } else {
    d.bases = null;
    for (const t of app.tenors) { d.tenors[t].corr = null; d.tenors[t].pin = false; }
  }
  const rebuild = d.mode !== mode;
  d.mode = mode;
  const follow = mode === "marked" && $("#su-follow").checked;
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
  d.alerts_off = $("#su-alerts-off").checked;
  renderSetup();
  renderSpreadTab();
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
        mktBusy = true; setTimeout(async () => { await loadMarket(); loadDailies(); renderSpreadValues(); mktBusy = false; }, 300);
      }
      if (app.tab === "spread" && app.spread?.ref && ev.pairs.includes(app.spread.ref)) refreshSpreadRef();
      return;
    }
    flash = ev.pair;
    clearTimeout(t); t = setTimeout(() => loadPairs(flash), 150);
    if (!app.current) return;
    if (app.tab === "spread" && app.spread?.ref) {
      if (ev.pair === app.spread.ref) refreshSpreadRef();
      else if (ev.pair === app.current) loadSpreadHistory(1500);
    }
    const auto = (ev.source === "market" && app.published.follow_market) || ev.source === "propagation";
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
    } else if (app.view && (app.draft.bases || []).includes(ev.pair)) {
      const v = await api(`/api/pairs/${app.current}`);
      app.view.bases = v.bases;
      if (app.dirty) runPreview(); else renderTermChart();
    }
  };
}

let spRefBusy = false;
function refreshSpreadRef() {        // the reference pair was published or its market moved: keep the draft, update the spreads
  if (spRefBusy) return;
  spRefBusy = true;
  setTimeout(async () => {
    const sp = app.spread;
    try {
      const brand = encodeURIComponent($("#mkt-brand").value);
      const [v, m] = await Promise.all([api(`/api/pairs/${sp.ref}`), api(`/api/pairs/${sp.ref}/market?brand=${brand}`).catch(() => null)]);
      if (app.spread === sp) {
        const moved = sp.view?.record?.version !== v.record.version;
        Object.assign(sp, { view: v, market: m });
        if (!$("#sp-grid input")) renderSpread(); else { $("#sp-note").textContent = `${sp.ref} v${v.record.version} at ${localTime(v.record.ts)}`; renderSpreadValues(); }
        if (moved) loadSpreadHistory(1500);
      }
    } catch {}
    spRefBusy = false;
  }, 300);
}

async function quietUpdate(source) {
  const v = await api(`/api/pairs/${app.current}`);
  if (!app.dirty) {
    app.view = v; app.baseVersion = v.record.version; app.published = pick(v.record); app.draft = pick(v.record);
    app.preview = { state: v.record, resolved: v.resolved, warnings: v.record.warnings || [] };
    renderAll();
  } else if (source === "market") {     // take the feed's fields, keep the trader's spread/skew/weights edits
    const pub = pick(v.record);
    for (const t of app.tenors) for (const f of FOLLOW_FIELDS) app.draft.tenors[t][f] = pub.tenors[t][f];
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
  return `${a.tenor} ${MARK_LABEL[a.mark]}: ours ${fmt(a.value, 2)}, FMD ${fmt(a.mkt, 2)} (${a.dev > 0 ? "+" : ""}${fmt(a.dev, 2)}, tol ${fmt(a.tol, 2)})`;
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
    if (e.target.type === "checkbox" && e.target.dataset.t) onCellInput(e.target);
    else if (e.target.matches("input.cell")) renderGrid();
  });
  grid.addEventListener("change", (e) => {
    if (!e.target.dataset.lock) return;
    app.draft[e.target.dataset.lock] = e.target.checked;
    changed(); renderGrid();
  });
  grid.addEventListener("focusin", (e) => { if (e.target.matches("input.cell")) e.target.select(); });
  grid.addEventListener("focusout", () => setTimeout(renderGrid, 0));
  grid.addEventListener("keydown", (e) => {
    const el = e.target; if (!el.matches("input.cell")) return;
    if (e.key === "ArrowDown" || e.key === "Enter") { e.preventDefault(); moveFocus(el, 1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); moveFocus(el, -1); }
    else if (e.key === "Escape") {
      const t = el.dataset.t, f = el.dataset.f;
      revertDraftValue(t, f);
      el.value = fmt(app.draft.tenors[t][f], 3); changed();
    }
  });

  grid.addEventListener("click", (e) => {
    const el = e.target.closest(".mq"); if (!el || !el.textContent) return;
    const q = mktQuote(el.dataset.t, el.dataset.m); if (!q) return;
    setFromMarket(el.dataset.t, el.dataset.m, q.mid); changed(); renderGrid();
  });
  $("#mkt-show").onchange = () => { renderMarket(); renderTermChart(); };
  $("#mkt-brand").onchange = async () => { await loadMarket(); if (app.tab === "spread") refreshSpreadRef(); };
  const followBlock = () => {
    if (!app.draft.follow_market) return false;
    showBanner("This pair follows FMD: ATM, RR and BF come from the feed. Untick Follow FMD in Setup to mark by hand.", [["OK", () => hideBanner()]]);
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
      row[f] = Math.round((row[f] + amt) * 1e6) / 1e6;
      if (f === "atm" && app.draft.mode === "derived" && !row.pin) row.corr = null;
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
  $$(".tabs button").forEach((b) => (b.onclick = () => {
    app.tab = b.dataset.tab;
    $$(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
    $$("[data-panel]").forEach((p) => (p.hidden = p.dataset.panel !== app.tab));
    if (app.tab === "weights") renderWeights(true);
    if (app.tab === "history") renderHistory();
    if (app.tab === "setup") renderSetup();
    if (app.tab === "surface") renderTermChart();
    if (app.tab === "s3d") render3D();
    if (app.tab === "spread") loadSpreadRef();
  }));

  // setup
  ["#su-mode", "#su-b1", "#su-b2", "#su-key", "#su-group", "#su-spref", "#su-cut", "#su-dc", "#su-atm", "#su-follow", "#su-alerts-off"].forEach((id) => $(id).addEventListener("change", onSetupChange));
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
  wireSpread();
  wireWeights();
}

async function init() {
  app.me = await api("/api/me");
  app.tenors = app.me.tenors;
  $("#me").textContent = app.me.user;
  $("#datadir").textContent = app.me.data;
  const opts = app.tenors.map((t) => `<option>${t}</option>`).join("");
  $("#sh-from").innerHTML = opts; $("#sh-to").innerHTML = opts;
  $("#sh-from").value = "1M"; $("#sh-to").value = "1Y";
  $("#sp-tenor").innerHTML = opts; $("#sp-tenor").value = "1M";
  try { app.prefs = await api("/api/prefs"); } catch { app.prefs = {}; }
  wire();
  await loadPairs();
  loadAlerts();
  connect();
  const h = location.hash.slice(1);
  if (h && app.pairs.some((p) => p.pair === h)) selectPair(h);
}
init();
