"use strict";
// VolMarker front end - plain JS, no dependencies.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const clone = (x) => JSON.parse(JSON.stringify(x));
const STATE_KEYS = ["pair", "mode", "bases", "key", "skew_expiry", "weights", "atm_type", "lock_rr10f", "lock_bf10f",
  "follow_market", "alert_tol", "alerts_off", "tenors"];
const MKT_FIELDS = { atm: "atm", rr25: "rr25", rr10: "rr10", bf25: "bf25", bf10: "bf10" };
const FOLLOW_FIELDS = ["atm", "rr25", "rr10f", "rr10", "bf25", "bf10f", "bf10"];   // owned by the feed on Follow FMD pairs
const MARK_LABEL = { atm: "ATM", rr25: "25D RR", rr10: "10D RR", bf25: "25D BF", bf10: "10D BF" };
const DEFAULT_WEIGHTS = { weekend1: 0, weekend2: 0, holiday1: 0.3, holiday2: 0.3, events: [] };

const app = {
  me: null, tenors: [], pairs: [], current: null,
  view: null,        // server view of the published version
  baseVersion: null, // version the draft is based on
  draft: null, preview: null, dirty: false, tab: "surface",
  alerts: [], alertCfg: null,
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

// ---------------------------------------------------------------- sidebar
function renderPairs(flashPair) {
  const q = $("#filter").value.trim().toUpperCase();
  const rows = app.pairs.filter((p) => !q || p.pair.includes(q));
  const groups = [["Key pairs", rows.filter((p) => p.key)], ["Other pairs", rows.filter((p) => !p.key)]];
  const html = groups.filter(([, g]) => g.length).map(([title, g]) =>
    `<div class="group">${title} (${g.length})</div>` + g.map((p) => {
      const tags = [
        p.mode === "derived" ? `<span class="tag" title="Derived from ${esc((p.bases || []).join(" × "))}">D</span>` : "",
        p.skew ? `<span class="tag" title="Skew active in Fenics">skew</span>` : "",
        p.stale ? `<span class="tag stale" title="Not marked for ${age(p.age_minutes)}">stale</span>` : "",
        p.warnings ? `<span class="tag" title="${p.warnings} check(s)">!${p.warnings}</span>` : "",
        p.follow ? `<span class="tag follow ${p.follow}" title="Follows FMD (${p.follow})">FMD</span>` : "",
        p.alerts ? `<span class="tag alert${p.alerts_new ? "" : " acked"}" title="${p.alerts} market alert(s)${p.alerts_new ? "" : ", acknowledged"}">⚠${p.alerts}</span>` : "",
      ].join("");
      const ageTitle = `Marked ${localTime(p.marked_ts)} · last update ${localTime(p.ts)} by ${p.user}`;
      return `<div class="prow${p.pair === app.current ? " sel" : ""}${p.pair === flashPair ? " flash" : ""}" data-pair="${p.pair}">
        <span><span class="name">${p.pair}</span>${tags}</span>
        <span class="atm" title="1M ATM">${fmt(p.atm_1m, 2)}</span>
        <span class="age" title="${esc(ageTitle)}">${age(p.age_minutes)}</span></div>`;
    }).join("")).join("");
  $("#pairlist").innerHTML = html || `<div class="muted" style="padding:8px">No pairs</div>`;
}
async function loadPairs(flashPair) {
  app.pairs = await api("/api/pairs");
  renderPairs(flashPair);
  fillPairSelects();
}
function fillPairSelects() {
  const opts = app.pairs.map((p) => `<option>${p.pair}</option>`).join("");
  for (const id of ["#np-b1", "#np-b2", "#su-b1", "#su-b2"]) {
    const el = $(id), v = el.value; el.innerHTML = `<option value="">—</option>` + opts; el.value = v;
  }
}

// ---------------------------------------------------------------- pair loading
async function selectPair(pair) {
  if (app.dirty && pair !== app.current && !confirmDiscard()) return;
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
}
function pick(rec) {
  const s = {};
  for (const k of STATE_KEYS) s[k] = clone(rec[k] ?? null);
  s.weights = s.weights || clone(DEFAULT_WEIGHTS);
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
  if (f === "atm" && app.draft.mode === "derived" && !row.pin) {
    row.atm = v; row.corr = null;          // calibrate correlation to this ATM
  } else if (f === "corr") {
    row.corr = v;
  } else {
    row[f] = v === null && !["atm", "rr10", "bf10"].includes(f) ? 0 : v;
  }
  changed();
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
  $("#ph-sub").innerHTML = `${mode}${d.key ? " · key pair" : ""} · v${r.version} ${by} at ${localTime(r.ts)}` +
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
function renderWeights(reloadCurve) {
  const w = app.draft.weights, [c1, c2] = [app.current.slice(0, 3), app.current.slice(3)];
  const f = [["weekend1", `Weekend ${c1}`], ["weekend2", `Weekend ${c2}`], ["holiday1", `Holiday ${c1}`], ["holiday2", `Holiday ${c2}`]];
  $("#factors").innerHTML = f.map(([k, l]) =>
    `<label>${l}<input class="num wf" data-k="${k}" value="${fmt(w[k], 2)}"></label>`).join("");
  const evs = (w.events || []).slice().map((e, i) => ({ ...e, i })).sort((a, b) => a.date.localeCompare(b.date));
  $("#events").innerHTML = `<thead><tr><th class="tn">Date</th><th class="tn">Description</th><th>Weight</th><th></th></tr></thead><tbody>` +
    (evs.length ? evs.map((e) => `<tr><td class="tn"><input type="date" class="ev" data-i="${e.i}" data-k="date" value="${e.date}"></td>
      <td class="tn"><input class="ev" data-i="${e.i}" data-k="desc" value="${esc(e.desc)}"></td>
      <td><input class="ev num" data-i="${e.i}" data-k="weight" value="${fmt(e.weight, 2)}"></td>
      <td><button class="ghost ev-del" data-i="${e.i}" title="Remove">✕</button></td></tr>`).join("")
      : `<tr><td colspan="4" class="muted tn">No events</td></tr>`) + "</tbody>";
  if (reloadCurve) renderCurve();
  loadDailies();
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
async function renderCurve() {
  const c = await api(`/api/pairs/${app.current}/curve`);
  const pts = c.points;
  if (!pts.length) { $("#curve").innerHTML = `<div class="muted" style="padding:20px">No ATM marks</div>`; return; }
  const ticks = [];
  pts.forEach(([d], i) => {
    if (i && d.slice(0, 7) !== pts[i - 1][0].slice(0, 7)) ticks.push([i, new Date(d).toLocaleString(undefined, { month: "short" })]);
  });
  lineChart($("#curve"), {
    n: pts.length, xLabel: (i) => {
      const d = pts[i][0], ev = c.events.filter((e) => e.date === d).map((e) => e.desc).join(", ");
      return d + (ev ? ` · ${ev}` : "");
    },
    xTicks: ticks, series: [{ name: "ATM", color: "var(--s1)", values: pts.map((p) => p[1]), label: false }],
    markers: c.events.map((e) => ({ i: pts.findIndex((p) => p[0] >= e.date), label: e.desc })).filter((mk) => mk.i >= 0),
    yLabel: "Interpolated ATM vol by expiry date",
  });
}

// ---------------------------------------------------------------- setup tab
function renderSetup() {
  const d = app.draft;
  $("#su-mode").value = d.mode;
  $("#su-b1").value = d.bases?.[0] || ""; $("#su-b2").value = d.bases?.[1] || "";
  $("#su-key").checked = !!d.key;
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
  $("#pairlist").addEventListener("click", (e) => {
    const r = e.target.closest(".prow"); if (r) selectPair(r.dataset.pair);
  });
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
      app.draft.tenors[t][f] = clone(publishedRow(t)[f]);
      if (f === "atm" && app.draft.mode === "derived") app.draft.tenors[t].corr = clone(publishedRow(t).corr);
      el.value = fmt(app.draft.tenors[t][f], 3); changed();
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
  }));

  // weights
  $("#factors").addEventListener("input", (e) => {
    const v = Number(e.target.value); if (!Number.isFinite(v)) return;
    app.draft.weights[e.target.dataset.k] = v; changed(); loadDailies();
  });
  $("#events").addEventListener("change", (e) => {
    if (!e.target.matches(".ev")) return;
    const ev = app.draft.weights.events[+e.target.dataset.i], k = e.target.dataset.k;
    ev[k] = k === "weight" ? Number(e.target.value) || 0 : e.target.value;
    changed(); renderWeights(false);
  });
  $("#events").addEventListener("click", (e) => {
    if (!e.target.matches(".ev-del")) return;
    app.draft.weights.events.splice(+e.target.dataset.i, 1); changed(); renderWeights(false);
  });
  $("#ev-add").onclick = () => {
    (app.draft.weights.events ||= []).push({ date: new Date().toISOString().slice(0, 10), desc: "", weight: 0.5 });
    changed(); renderWeights(false);
  };

  // setup
  ["#su-mode", "#su-b1", "#su-b2", "#su-key", "#su-atm", "#su-follow", "#su-alerts-off"].forEach((id) => $(id).addEventListener("change", onSetupChange));
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
        pair: $("#np-pair").value, mode, key: $("#np-key").checked,
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
}

async function init() {
  app.me = await api("/api/me");
  app.tenors = app.me.tenors;
  $("#me").textContent = app.me.user;
  $("#datadir").textContent = app.me.data;
  const opts = app.tenors.map((t) => `<option>${t}</option>`).join("");
  $("#sh-from").innerHTML = opts; $("#sh-to").innerHTML = opts;
  $("#sh-from").value = "1M"; $("#sh-to").value = "1Y";
  wire();
  await loadPairs();
  loadAlerts();
  connect();
  const h = location.hash.slice(1);
  if (h && app.pairs.some((p) => p.pair === h)) selectPair(h);
}
init();
