/* CIQ Data Intelligence - frontend (no build step). */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const fmtInt = (v) => (v == null ? "—" : Number(v).toLocaleString());
const fmtPct = (v) => (v == null ? "—" : (v * 100).toFixed(1) + "%");
const fmtMonth = (iso) => new Date(iso + "T00:00:00").toLocaleDateString(undefined, { month: "short", year: "numeric" });
const fmtPeriod = (cov) => (cov ? `${fmtMonth(cov.data_start)} – ${fmtMonth(cov.data_end)}` : "");
function fmtKpi(k) {
  if (k.value == null) return "—";
  switch (k.format) {
    case "pct": return fmtPct(k.value);
    case "days": return `${fmtInt(k.value)} days`;
    case "num": return Number(k.value).toLocaleString(undefined, { maximumFractionDigits: 1 });
    case "text": return String(k.value);
    default: return fmtInt(k.value);
  }
}
const svgIcon = (d) => `<svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true"><path d="${d}" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
// Only ~~double tildes~~ strike through. Analysts write "~29%" for "approximately"; marked's default GFM rule
// also accepts single tildes, which struck out everything between two approximate figures.
marked.use({ tokenizer: { del(src) {
  const m = /^~~(?=[^\s~])([\s\S]*?[^\s~])~~(?!~)/.exec(src);
  if (m) return { type: "del", raw: m[0], text: m[1], tokens: this.lexer.inlineTokens(m[1]) };
  // undefined (not false) = no strikethrough here; false would fall back to the single-tilde rule
} } });

const ICONS = {
  download: "M12 4v11M7 10l5 5 5-5M5 20h14",
  image: "M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4",
  expand: "M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5",
  collapse: "M9 4v5H4M15 4v5h5M9 20v-5H4M15 20v-5h5",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  check: "M5 12.5l4.5 4.5L19 7.5",
  retry: "M20 11a8 8 0 1 0-2.4 5.7M20 4v7h-7",
  info: "M12 11v5M12 7.5v.5M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
};

let topPicker = null;   // header brand picker (mirrors the hidden #productSelect)
function setProductUI(v) {
  $("#productSelect").value = v;
  topPicker?.set(v);
}

const state = { meta: null, product: null, convId: null, busy: false, controller: null, history: [], ask: null };

// ---------------------------------------------------------------- init
async function init() {
  bindUI();
  try {
    const meta = await (await fetch("/api/meta")).json();
    state.meta = meta;
    state.product = localStorage.getItem("ciq.product") || meta.default_product;
    const sel = $("#productSelect");
    for (const p of meta.products) {
      const o = el("option", null, `${p.product}`);
      o.value = p.product;
      o.title = `${p.drug_class} · ${p.patients} patients`;
      sel.appendChild(o);
    }
    if (!meta.products.some((p) => p.product === state.product)) state.product = meta.default_product;
    sel.value = state.product;
    topPicker = brandPicker(state.product, (v) => {
      sel.value = v;
      sel.dispatchEvent(new Event("change"));
    }, { variant: "dark", label: "Brand" });
    $("#brandPickerSlot").replaceChildren(topPicker.node);
    const c = meta.coverage;
    $("#footModel").textContent = `${meta.model} · Bedrock`;
    setUser(meta.user);
    $("#hintModel").textContent = meta.model;
    $("#footData").textContent = `${c.n_patients.toLocaleString()} patients · ${fmtPeriod(c)}`;
    $("#srcFacts").textContent = `${meta.indication} · ${c.n_patients.toLocaleString()} patients · ${fmtPeriod(c)}`;
    $("#srcPill").title = `Connected: IQVIA claims, ${meta.indication}, ${fmtPeriod(c)}`;
  } catch (e) {
    $("#footData").textContent = "Backend unavailable";
    setUser(null);
  }
  renderWelcome();
  loadHistory();
  const cid = new URLSearchParams(location.search).get("c");
  if (cid) openConversation(cid);
}

function bindUI() {
  $("#composer").addEventListener("submit", (e) => { e.preventDefault(); submit(); });
  const input = $("#input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
  });
  input.addEventListener("input", autosize);
  $("#btnStop").addEventListener("click", () => state.controller && state.controller.abort());
  $("#btnNew").addEventListener("click", newConversation);
  $("#btnCompare").addEventListener("click", () => openCompare());
  initCompare();
  $("#btnMenu").addEventListener("click", toggleSidebar);
  $("#btnSideToggle").addEventListener("click", toggleSidebar);
  $("#btnRecents").addEventListener("click", () => setSidebarCollapsed(false, true));
  // in the rail, the search icon opens the sidebar and focuses the box
  $("#searchRow").addEventListener("click", () => {
    if ($("#app").classList.contains("nav-collapsed") && !narrowScreen.matches) focusSearch();
  });
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  $("#searchKbd").textContent = mac ? "⌘K" : "Ctrl K";
  $("#searchRow").dataset.tip = `Search (${mac ? "⌘K" : "Ctrl+K"})`;
  $("#historySearch").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.target.value = ""; renderHistory(); e.target.blur(); }
    if (e.key === "Enter") $("#historyList li:not(.empty)")?.click();
  });
  let collapsed = false;
  try { collapsed = localStorage.getItem("ciq.navCollapsed") === "1"; } catch (e) { /* storage blocked */ }
  setSidebarCollapsed(collapsed);
  $("#historySearch").addEventListener("input", renderHistory);
  initDataSources();
  initAccountMenu();
  $("#askSkip").addEventListener("click", () => answerAsk(null));
  $("#askNext").addEventListener("click", () => askAdvance());
  document.addEventListener("keydown", (e) => {
    const open = document.querySelector(".card.expanded");
    if (e.key === "Escape" && open) open.dispatchEvent(new Event("collapse"));
    const mod = (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey;
    if (mod && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSidebar(); }
    if (mod && e.key.toLowerCase() === "k") { e.preventDefault(); focusSearch(); }
    // number keys pick an option in the question panel (unless the user is typing an answer)
    if (state.ask && !mod && /^[1-9]$/.test(e.key) && !(e.target.closest("textarea, input") && e.target.value)) {
      const btn = $("#askOptions").children[Number(e.key) - 1];
      if (btn) { e.preventDefault(); btn.click(); }
    }
  });
  $("#productSelect").addEventListener("change", (e) => {
    state.product = e.target.value;
    localStorage.setItem("ciq.product", state.product);
    if (!state.convId) renderWelcome();
  });
}

// phones: the sidebar is an off-canvas drawer; wider screens: it collapses to an icon rail (remembered)
const narrowScreen = window.matchMedia("(max-width: 900px)");
function toggleSidebar() {
  if (narrowScreen.matches) $("#app").classList.toggle("nav-open");
  else setSidebarCollapsed(!$("#app").classList.contains("nav-collapsed"), true);
}
function focusSearch() {
  if (narrowScreen.matches) $("#app").classList.add("nav-open");
  else setSidebarCollapsed(false, true);
  $("#historySearch").focus();
}
function setSidebarCollapsed(on, remember) {
  $("#app").classList.toggle("nav-collapsed", on);
  const label = on ? "Expand sidebar" : "Collapse sidebar";
  for (const btn of [$("#btnMenu"), $("#btnSideToggle")]) {
    btn.setAttribute("aria-label", label);
    btn.setAttribute("aria-expanded", String(!on));
  }
  $("#btnSideToggle").dataset.tip = label;
  if (remember) try { localStorage.setItem("ciq.navCollapsed", on ? "1" : "0"); } catch (e) { /* storage blocked */ }
  // charts size to their container; let them re-measure once the grid transition ends
  setTimeout(() => window.dispatchEvent(new Event("resize")), 220);
}

function autosize() {
  const t = $("#input");
  t.style.height = "auto";
  t.style.height = Math.min(t.scrollHeight, 180) + "px";
}

// ---------------------------------------------------------------- welcome
async function renderWelcome() {
  const p = state.product || "your brand";
  $("#heroBrand").textContent = p;
  $("#input").placeholder = `Ask a question, e.g. “What is the ${p} patient share in each line?”`;
  const sg = $("#suggest");
  sg.replaceChildren();
  for (const g of (state.meta?.suggestions || [])) {
    const box = el("div", "s-group");
    box.appendChild(el("h3", null, g.group));
    for (const q of g.questions) {
      const text = q.replaceAll("{p}", p);
      const b = el("button", "s-q", text);
      b.type = "button";
      b.addEventListener("click", () => submit(text));
      box.appendChild(b);
    }
    sg.appendChild(box);
  }
  if (state.product) renderCommandCenter();
}

// ---------------------------------------------------------------- searchable brand picker
// Type to filter by brand, molecule or drug class; grouped by class; arrows + Enter to pick, Esc to close.
function brandPicker(value, onPick, { variant = "", label = "Brand", wide = false } = {}) {
  const products = state.meta?.products || [];
  const root = el("div", `bp ${variant}` + (wide ? " wide" : ""));
  const btn = el("button", "bp-btn");
  btn.type = "button";
  btn.setAttribute("aria-haspopup", "listbox");
  btn.setAttribute("aria-expanded", "false");
  btn.setAttribute("aria-label", `${label}: ${value}`);
  const btnLabel = el("span", "bp-value", value);
  const chev = el("span", "bp-chev");
  chev.innerHTML = svgIcon("M6 9l6 6 6-6");
  btn.append(btnLabel, chev);
  const pop = el("div", "bp-pop");
  pop.hidden = true;
  const search = el("input", "bp-search");
  search.type = "search";
  search.placeholder = "Search brand, molecule or class";
  search.setAttribute("aria-label", "Search brands");
  const list = el("div", "bp-list");
  list.setAttribute("role", "listbox");
  pop.append(search, list);
  root.append(btn, pop);
  let current = value, rows = [], active = 0;

  const niceClass = (c) => c.split(" ").map((w) => (/\d/.test(w) || w.length <= 4 ? w : w.toLowerCase())).join(" ");
  const paint = () => {
    const terms = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const hay = (p) => `${p.product} ${p.molecules || ""} ${p.drug_class}`.toLowerCase();
    const hit = products.filter((p) => terms.every((t) => hay(p).includes(t)));
    const groups = new Map();
    for (const p of hit) {
      if (!groups.has(p.drug_class)) groups.set(p.drug_class, []);
      groups.get(p.drug_class).push(p);
    }
    const ordered = [...groups.entries()].sort((a, b) =>
      b[1].reduce((s, p) => s + p.patients, 0) - a[1].reduce((s, p) => s + p.patients, 0));
    list.replaceChildren();
    rows = [];
    for (const [cls, ps] of ordered) {
      list.appendChild(el("div", "bp-group", niceClass(cls)));
      for (const p of ps.sort((a, b) => b.patients - a.patients)) {
        const row = el("div", "bp-row" + (p.product === current ? " selected" : ""));
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(p.product === current));
        const txt = el("span", "bp-row-text");
        txt.append(el("span", "bp-name", p.product));
        if (p.molecules && p.molecules.toLowerCase() !== p.product.toLowerCase()) txt.append(el("span", "bp-mol", p.molecules));
        row.append(el("span", "bp-check", p.product === current ? "✓" : ""), txt, el("span", "bp-pts", `${p.patients.toLocaleString()} pts`));
        const idx = rows.length;
        row.addEventListener("mousemove", () => { if (active !== idx) { active = idx; highlight(); } });
        row.addEventListener("mousedown", (e) => e.preventDefault());   // keep focus in the search box
        row.addEventListener("click", () => pick(p.product));
        rows.push({ node: row, product: p.product });
        list.appendChild(row);
      }
    }
    if (!rows.length) list.appendChild(el("div", "bp-empty", `No brand matches “${search.value.trim()}”`));
    active = Math.max(0, rows.findIndex((r) => r.product === current));
    if (terms.length) active = 0;
    highlight();
  };
  const highlight = () => {
    rows.forEach((r, i) => r.node.classList.toggle("active", i === active));
    rows[active]?.node.scrollIntoView({ block: "nearest" });
  };
  const open = (on) => {
    pop.hidden = !on;
    btn.setAttribute("aria-expanded", String(on));
    root.classList.toggle("open", on);
    if (on) { search.value = ""; paint(); search.focus(); }
  };
  const pick = (v) => {
    open(false);
    btn.focus();
    if (v !== current) { current = v; btnLabel.textContent = v; btn.setAttribute("aria-label", `${label}: ${v}`); onPick(v); }
  };
  btn.addEventListener("click", (e) => { e.stopPropagation(); open(pop.hidden); });
  search.addEventListener("input", paint);
  search.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(rows.length - 1, active + 1); highlight(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(0, active - 1); highlight(); }
    else if (e.key === "Enter") { e.preventDefault(); if (rows[active]) pick(rows[active].product); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); open(false); btn.focus(); }
  });
  document.addEventListener("click", (e) => { if (!pop.hidden && !root.contains(e.target)) open(false); });
  return {
    node: root,
    set(v) { current = v; btnLabel.textContent = v; btn.setAttribute("aria-label", `${label}: ${v}`); },
  };
}

// ---------------------------------------------------------------- market command center (home)
let ccWindow = 3;
try { ccWindow = Number(localStorage.getItem("ciq.ccWindow")) || 3; } catch (e) { /* storage blocked */ }
let ccLens = "market";
let ccToken = 0;

async function renderCommandCenter() {
  const root = $("#cc");
  const token = ++ccToken;
  root.classList.add("loading");
  if (!root.firstChild) root.appendChild(ccSkeleton());
  let d;
  try {
    const r = await fetch(`/api/command-center?product=${encodeURIComponent(state.product)}&window=${ccWindow}`);
    d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
  } catch (e) {
    root.replaceChildren(el("div", "error-box", "Could not load the Command Center: " + (e.message || e)));
    return;
  }
  if (token !== ccToken) return;   // a newer render started (brand/window changed)
  root.classList.remove("loading");
  root.replaceChildren(ccContext(d), ccHeadline(d), ccPanels(d), ccSignals(d), ccLenses(d), ccNotes(d));
}

function ccSkeleton() {
  const s = el("div", "cc-skel");
  for (let i = 0; i < 3; i++) s.appendChild(el("div", "cc-skel-row"));
  return s;
}

function ccSelect(label, options, value, onChange) {
  const wrap = el("label", "cc-field");
  wrap.appendChild(el("span", "cc-field-label", label));
  const sel = el("select", "cc-select");
  for (const o of options) {
    const op = el("option", null, o.label);
    op.value = o.value;
    op.disabled = Boolean(o.disabled);
    sel.appendChild(op);
  }
  sel.value = value;
  sel.addEventListener("change", () => onChange(sel.value));
  wrap.appendChild(sel);
  return wrap;
}

function ccBrandField(value) {
  const wrap = el("div", "cc-field");
  wrap.appendChild(el("span", "cc-field-label", "Brand"));
  wrap.appendChild(brandPicker(value, (v) => {
    state.product = v;
    setProductUI(v);
    try { localStorage.setItem("ciq.product", v); } catch (e) { /* storage blocked */ }
    renderWelcome();
  }, { label: "Brand" }).node);
  return wrap;
}

function ccContext(d) {
  const c = d.context;
  const bar = el("div", "cc-context");
  bar.appendChild(el("span", "cc-kicker", "Market Command Center"));
  const fields = el("div", "cc-fields");
  fields.append(
    ccSelect("Indication", c.indications.map((x, i) => ({ value: x, label: i ? `${x} · connect data` : x, disabled: i > 0 })), c.indication, () => {}),
    ccBrandField(c.product),
    ccSelect("Geography", [{ value: "US", label: c.geography }, ...["Northeast", "South", "Midwest", "West"].map((r) => ({ value: r, label: `${r} · needs geography data`, disabled: true }))], "US", () => {}),
  );
  const win = el("div", "cc-field");
  win.appendChild(el("span", "cc-field-label", "Period"));
  const seg = el("div", "cc-seg");
  for (const w of c.windows) {
    const b = el("button", "cc-seg-btn" + (w === c.window ? " on" : ""), w === 1 ? "Last month" : `Last ${w} months`);
    b.type = "button";
    b.addEventListener("click", () => {
      ccWindow = w;
      try { localStorage.setItem("ciq.ccWindow", String(w)); } catch (e) { /* storage blocked */ }
      renderCommandCenter();
    });
    seg.appendChild(b);
  }
  win.appendChild(seg);
  fields.appendChild(win);
  bar.appendChild(fields);
  return bar;
}

function ccHeadline(d) {
  const c = d.context;
  const h = el("div", "cc-headline");
  const t = el("h1", "cc-title");
  t.append(el("span", null, "NSCLC"), el("i", null, "·"), el("span", "accent", c.product), el("i", null, "·"),
    el("span", null, "US"), el("i", null, "·"), el("span", null, c.current.label));
  h.append(t, el("p", "cc-sub", `Compared with ${c.prior.label}, the prior window of equal length. Every number and signal below is observed in IQVIA claims.`));
  return h;
}

function ccTile(k) {
  const t = el("div", "cc-tile" + (k.lead ? " lead" : "") + (k.unavailable ? " off" : ""));
  const val = k.unavailable ? "—" : k.format === "pct" ? fmtPct(k.value) : fmtInt(k.value);
  t.append(el("span", "cc-tile-val", val), el("span", "cc-tile-lab", k.label));
  if (k.delta) {
    const v = k.delta.value;
    // market growth is partly data coverage, so it is shown neutrally rather than as good/bad
    const tone = k.delta.coverage || v == null ? "" : v > 0.0005 ? " up" : v < -0.0005 ? " down" : "";
    const pill = el("span", "cc-delta" + tone, `${k.delta.fmt} vs prior`);
    if (k.delta.coverage) pill.title = "Includes claims-capture ramp-up early in the data window";
    t.appendChild(pill);
  }
  if (k.note) t.appendChild(el("span", "cc-tile-note", k.note));
  if (k.unavailable) {
    const link = el("button", "cc-tile-link", k.unavailable);
    link.type = "button";
    link.addEventListener("click", () => openConnect("custom"));
    t.appendChild(link);
  }
  return t;
}

function ccPanels(d) {
  const row = el("div", "cc-panels");
  const panel = (title, q, tiles, cls, note) => {
    const p = el("section", "cc-panel " + cls);
    const h = el("header");
    h.append(el("h2", null, title), el("span", null, q));
    const g = el("div", "cc-tiles");
    for (const k of tiles) g.appendChild(ccTile(k));
    p.append(h, g);
    if (note) p.appendChild(el("p", "cc-panel-note", note));
    return p;
  };
  row.append(panel("The market", "How big is it?", d.market, "cc-market", d.market_note),
    panel(`${d.context.product} performance`, "How is the brand doing?", d.brand, "cc-brand"));
  return row;
}

const SIGNAL_ICONS = { up: "M7 17L17 7M9 7h8v8", down: "M7 7l10 10M17 9v8H9", flat: "M5 12h14",
  opportunity: "M12 3v4M12 17v4M3 12h4M17 12h4M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z", watch: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z" };

function ccSignals(d) {
  const sec = el("section", "cc-signals");
  const h = el("header");
  h.append(el("h2", null, "What matters now"), el("span", "cc-evidence", "Observed in claims · no model inference"));
  sec.appendChild(h);
  const list = el("div", "cc-signal-list");
  for (const s of d.signals) {
    const card = el("article", `cc-signal ${s.direction}`);
    const icon = el("span", "cc-signal-icon");
    icon.innerHTML = svgIcon(SIGNAL_ICONS[s.direction] || SIGNAL_ICONS.flat);
    const text = el("div", "cc-signal-text");
    text.append(el("h3", null, s.headline), el("p", null, s.evidence));
    const acts = el("div", "cc-signal-actions");
    if (s.action) {
      const go = el("button", "cc-go", `${s.action.label} →`);
      go.type = "button";
      go.addEventListener("click", () => compareNow(s.action.kind, s.action.a, s.action.b));
      acts.appendChild(go);
    }
    if (s.hcps?.hcp_ids?.length) {
      const v = el("button", "cc-go ghost", `View ${s.hcps.hcp_ids.length.toLocaleString()} HCPs`);
      v.type = "button";
      v.addEventListener("click", () => openHcpList(s.hcps, d.context.product));
      acts.appendChild(v);
    }
    text.appendChild(acts);
    card.append(icon, text);
    list.appendChild(card);
  }
  sec.appendChild(list);
  return sec;
}

const LENSES = [["market", "Market"], ["hcps", "HCPs"], ["patients", "Patients"], ["competitors", "Competitors"]];

function ccLenses(d) {
  const sec = el("section", "cc-lenses");
  const tabs = el("div", "cc-lens-tabs");
  tabs.setAttribute("role", "tablist");
  const body = el("div", "cc-lens-body");
  const show = (id) => {
    ccLens = id;
    for (const b of tabs.children) b.classList.toggle("on", b.dataset.id === id);
    const L = d.lenses[id];
    const q = el("p", "cc-lens-q", L.question);
    const grid = el("div", "charts");
    for (const spec of L.charts) {
      const c = el("div", "chart");
      grid.appendChild(c);
      requestAnimationFrame(() => embed(c, spec));
    }
    const asks = el("div", "cc-lens-asks");
    asks.appendChild(el("span", null, "Ask CIQ"));
    for (const text of L.questions) {
      const b = el("button", "fu", text);
      b.type = "button";
      b.addEventListener("click", () => submit(text));
      asks.appendChild(b);
    }
    body.replaceChildren(q, grid, asks);
  };
  for (const [id, label] of LENSES) {
    const b = el("button", "cc-lens-tab", label);
    b.type = "button";
    b.dataset.id = id;
    b.setAttribute("role", "tab");
    b.addEventListener("click", () => show(id));
    tabs.appendChild(b);
  }
  sec.append(tabs, body);
  show(ccLens);
  return sec;
}

function ccNotes(d) {
  const n = el("p", "cc-notes");
  n.textContent = d.notes.join(" ");
  return n;
}

// run a comparison straight from a Command Center signal
async function compareNow(kind, a, b) {
  if (state.busy || !kind || !a || !b) return;
  try {
    const r = await fetch("/api/compare", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind, a, b, product: state.product, conversation_id: state.convId }) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    showPanelResult(data);
  } catch (e) {
    alert("Could not run the comparison: " + (e.message || e));
  }
}

// A table with a search box. Matching is on the values as displayed (so "20.3%" finds a 0.203 share);
// several words must all match. onFilter(rows, filtering) lets callers export/save just the matches.
function searchableTable(rows, { total = rows.length, noun = "HCPs", onFilter } = {}) {
  const box = el("div", "st");
  const bar = el("label", "st-bar");
  const icon = el("span", "st-icon");
  icon.innerHTML = svgIcon("M11 18a7 7 0 1 0 0-14 7 7 0 0 0 0 14zM20 20l-3.5-3.5");
  const input = el("input", "st-input");
  input.type = "search";
  input.placeholder = `Search ${noun} by id or any value`;
  input.setAttribute("aria-label", `Search ${noun}`);
  const count = el("span", "st-count");
  bar.append(icon, input, count);
  const wrap = el("div", "table-wrap st-wrap");
  box.append(bar, wrap);
  const full = renderTable(rows);
  const text = [...full.querySelectorAll("tbody tr")].map((tr) => [...tr.cells].map((c) => c.textContent).join(" ").toLowerCase());
  const apply = () => {
    const terms = input.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const hit = terms.length ? rows.filter((_, i) => terms.every((t) => text[i].includes(t))) : rows;
    wrap.replaceChildren(!terms.length ? full : hit.length ? renderTable(hit) : el("p", "st-empty", `No ${noun} match “${input.value.trim()}”`));
    count.textContent = terms.length
      ? `${hit.length.toLocaleString()} of ${rows.length.toLocaleString()}`
      : `${total.toLocaleString()} ${noun}` + (rows.length < total ? ` · searching the first ${rows.length.toLocaleString()}` : "");
    onFilter?.(hit, terms.length > 0);
  };
  input.addEventListener("input", apply);
  input.addEventListener("keydown", (e) => { if (e.key === "Escape" && input.value) { e.stopPropagation(); input.value = ""; apply(); } });
  apply();
  return { node: box, input };
}

// HCP list drawer: searchable table, export, save as cohort, compare the cohort
function openHcpList(list, product) {
  const back = el("div", "modal-back");
  const m = el("div", "modal hcpm");
  m.setAttribute("role", "dialog");
  m.setAttribute("aria-modal", "true");
  const head = el("header", "cm-head");
  const ht = el("div", "cm-head-text");
  ht.append(el("span", "cm-kicker", `${list.hcp_ids.length.toLocaleString()} HCPs`), el("h2", null, list.label));
  const x = el("button", "cm-x");
  x.type = "button"; x.setAttribute("aria-label", "Close");
  x.innerHTML = svgIcon("M6 6l12 12M18 6L6 18");
  head.append(ht, x);
  const body = el("div", "cm-body");
  let shown = list.rows, filtering = false;
  const acts = el("div", "cm-actions");
  const exp = el("button", "cm-btn", "Export CSV");
  exp.type = "button";
  exp.addEventListener("click", () => downloadCSV({ tool: "hcps", data: { list: shown } }));
  const form = el("form", "dg-save");
  const name = el("input"); name.value = list.label; name.setAttribute("aria-label", "Cohort name");
  const save = el("button", "cm-btn primary", "Save as cohort"); save.type = "submit";
  form.append(name, save);
  const table = searchableTable(list.rows, { total: list.hcp_ids.length, onFilter: (rows, f) => {
    shown = rows; filtering = f;
    exp.textContent = f ? `Export ${rows.length.toLocaleString()} (CSV)` : "Export CSV";
    save.textContent = f ? `Save ${rows.length.toLocaleString()} as cohort` : "Save as cohort";
    save.disabled = f && !rows.length;
  } });
  table.node.querySelector(".st-wrap").classList.add("hcp-table");
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    save.disabled = true;
    const r = await fetch("/api/cohorts", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.value, product,
        hcp_ids: filtering ? shown.map((r) => r.hcp_id) : list.hcp_ids,
        definition: filtering ? `${list.label}, filtered by “${table.input.value.trim()}”` : list.label }) });
    const saved = await r.json();
    if (!r.ok) { save.disabled = false; alert(saved.detail || r.statusText); return; }
    cx.opts = null;
    const cmp = el("button", "cm-btn primary", "Compare this cohort vs all HCPs");
    cmp.type = "button";
    cmp.addEventListener("click", () => { close(); compareNow("hcp", `saved:${saved.id}`, "all"); });
    form.replaceChildren(el("span", "dg-saved", `✓ Saved “${saved.name}”`), cmp);
  });
  acts.append(form, exp);
  body.append(table.node, acts);
  m.append(head, body);
  back.appendChild(m);
  document.body.appendChild(back);
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  x.addEventListener("click", close);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  table.input.focus();
}

// ---------------------------------------------------------------- history
async function loadHistory() {
  try { state.history = await (await fetch("/api/conversations")).json(); } catch (e) { /* offline */ }
  renderHistory();
}

function renderHistory() {
  const ul = $("#historyList");
  const items = state.history;
  const q = $("#historySearch").value.trim().toLowerCase();
  const shown = q ? items.filter((c) => `${c.title} ${c.product || ""}`.toLowerCase().includes(q)) : items;
  ul.replaceChildren();
  if (!items.length) { ul.appendChild(el("li", "empty", "No analyses yet")); return; }
  if (!shown.length) { ul.appendChild(el("li", "empty", "No matches")); return; }
  for (const c of shown) {
    const li = el("li");
    if (c.id === state.convId) li.classList.add("active");
    const txt = el("span", "h-text");
    txt.append(el("span", "h-title", c.title));
    if (c.product) txt.append(el("span", "h-meta", c.product));
    li.appendChild(txt);
    li.title = `${c.title}\n${c.product || ""} · ${new Date(c.updated_at * 1000).toLocaleString()}`;
    const del = el("button", "h-del", "×");
    del.type = "button";
    del.title = "Delete";
    del.setAttribute("aria-label", `Delete “${c.title}”`);
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this analysis?")) return;
      await fetch(`/api/conversations/${c.id}`, { method: "DELETE" });
      if (state.convId === c.id) newConversation();
      loadHistory();
    });
    li.appendChild(del);
    li.addEventListener("click", () => { openConversation(c.id); $("#app").classList.remove("nav-open"); });
    ul.appendChild(li);
  }
}

function newConversation() {
  if (state.busy) state.controller?.abort();
  state.convId = null;
  history.replaceState(null, "", "/");
  $("#messages").replaceChildren();
  setFollowups([]);
  closeAsk();
  $("#welcome").hidden = false;
  $("#convTitle").textContent = "New analysis";
  renderWelcome();
  loadHistory();
  $("#input").focus();
}

async function openConversation(id) {
  if (state.busy) return;
  const res = await fetch(`/api/conversations/${id}`);
  if (!res.ok) return;
  const conv = await res.json();
  state.convId = conv.id;
  history.replaceState(null, "", `/?c=${conv.id}`);
  $("#convTitle").textContent = conv.title;
  $("#welcome").hidden = true;
  const box = $("#messages");
  box.replaceChildren();
  let lastTools = [];
  if (conv.product) { state.product = conv.product; setProductUI(conv.product); }
  for (const t of conv.transcript) {
    if (t.role === "user") addUserMsg(t.text, t.product, Boolean(t.answer_to));
    else {
      const m = createAssistantMsg();
      m.restore(t);
      lastTools = (t.results || []).map((r) => r.tool);
    }
  }
  setFollowups(lastTools);
  const answered = new Set(conv.transcript.filter((t) => t.answer_to).map((t) => t.answer_to));
  for (const rec of document.querySelectorAll(".ask-record[data-id]")) {
    if (!answered.has(rec.dataset.id)) rec.classList.add("waiting");
  }
  closeAsk();
  if (conv.pending) openAsk(conv.pending);
  loadHistory();
}

// ---------------------------------------------------------------- chat
function addUserMsg(text, product, isAnswer) {
  const m = el("div", "msg user");
  const b = el("div", "bubble");
  if (isAnswer) b.appendChild(el("span", "b-brand", "Your answer"));
  else if (product) b.appendChild(el("span", "b-brand", `Brand: ${product}`));
  b.appendChild(document.createTextNode(text));
  m.appendChild(b);
  $("#messages").appendChild(m);
  scrollDown(true);
}

async function submit(preset) {
  const input = $("#input");
  const text = (preset ?? input.value).trim();
  if (!text || state.busy) return;
  input.value = "";
  autosize();
  setFollowups([]);
  const isAnswer = Boolean(state.ask);
  let sent = text;
  if (isAnswer && !preset) sent = askTypedAnswer(text);   // typed "Other" answer
  closeAsk();
  document.querySelectorAll(".ask-record.waiting").forEach((r) => r.classList.remove("waiting"));
  $("#welcome").hidden = true;
  addUserMsg(sent, state.product, isAnswer);
  await streamChat({ message: sent, conversation_id: state.convId, product: state.product }, createAssistantMsg());
}

// Regenerate the latest reply: the server rewinds the conversation to the last user message and runs it again.
async function retryLast(root) {
  if (state.busy || !state.convId) return;
  root.remove();
  closeAsk();
  setFollowups([]);
  await streamChat({ retry: true, conversation_id: state.convId }, createAssistantMsg());
}

async function streamChat(payload, m) {
  setBusy(true);
  state.controller = new AbortController();
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal: state.controller.signal,
    });
    if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        if (chunk.startsWith("data: ")) handleEvent(JSON.parse(chunk.slice(6)), m);
      }
    }
  } catch (e) {
    if (e.name === "AbortError") m.error("Stopped.");
    else m.error(String(e.message || e));
  } finally {
    m.finish();
    setBusy(false);
    loadHistory();
  }
}

function handleEvent(ev, m) {
  switch (ev.type) {
    case "conversation":
      state.convId = ev.id;
      history.replaceState(null, "", `/?c=${ev.id}`);
      break;
    case "thinking": m.thinking(ev.text); break;
    case "text": m.text(ev.text); break;
    case "status": m.status(ev.text); break;
    case "tool_start": m.toolStart(ev); break;
    case "tool_result": m.toolResult(ev); break;
    case "ask": m.ask(ev); break;
    case "error": m.error(ev.message); break;
    case "done":
      if (ev.conversation) $("#convTitle").textContent = ev.conversation.title;
      break;
  }
}

function setBusy(b) {
  state.busy = b;
  $("#app").classList.toggle("busy", b);
  $("#btnSend").hidden = b;
  $("#btnStop").hidden = !b;
}

let userScrolledUp = false;
$("#thread")?.addEventListener("scroll", () => {
  const t = $("#thread");
  userScrolledUp = t.scrollHeight - t.scrollTop - t.clientHeight > 120;
});
function scrollDown(force) {
  const t = $("#thread");
  if (force || !userScrolledUp) t.scrollTop = t.scrollHeight;
}

// ---------------------------------------------------------------- assistant message
function createAssistantMsg() {
  const root = el("div", "msg assistant");
  const av = el("div", "avatar");
  const img = el("img"); img.src = "/static/assets/chryselys-mark.png"; img.alt = "";
  av.appendChild(img);
  const body = el("div", "body");
  root.append(av, body);

  const trace = el("details", "trace");
  trace.open = true;
  const sum = el("summary");
  const chev = el("span", "chev", "›");
  const status = el("span", "t-status", "Thinking");
  const count = el("span", null, "");
  const dots = el("span", "t-dots");
  dots.append(el("i"), el("i"), el("i"));
  sum.append(chev, status, dots, count);
  trace.classList.add("live");
  root.classList.add("working");
  const tbody = el("div", "trace-body");
  const think = el("div", "thinking");
  const steps = el("ol", "steps");
  tbody.append(think, steps);
  trace.append(sum, tbody);

  const cards = el("div", "cards");
  const answerBox = el("section", "answer-box");
  answerBox.hidden = true;
  const ahead = el("header", "answer-head");
  ahead.append(el("span", "answer-label", "CIQ read-out"), el("span", "answer-sub", "Interpretation of the analyses above, written by the model"));
  const answer = el("div", "answer streaming");
  answerBox.append(ahead, answer);
  const actions = el("div", "msg-actions");
  actions.hidden = true;
  const copyBtn = msgAction("copy", "Copy", async () => {
    await copyText(md.trim());
    copyBtn.innerHTML = svgIcon(ICONS.check);
    copyBtn.classList.add("done");
    copyBtn.title = "Copied";
    setTimeout(() => { copyBtn.innerHTML = svgIcon(ICONS.copy); copyBtn.classList.remove("done"); copyBtn.title = "Copy"; }, 1500);
  });
  const retryBtn = msgAction("retry", "Retry", () => retryLast(root));
  retryBtn.classList.add("ma-retry");
  actions.append(copyBtn, retryBtn);
  body.append(trace, cards, answerBox, actions);
  const tools = [];
  $("#messages").appendChild(root);
  scrollDown(true);

  let md = "";
  let raf = 0;
  let asked = null;
  const stepEls = {};
  const render = () => {
    raf = 0;
    answerBox.hidden = !md;
    answer.innerHTML = DOMPurify.sanitize(marked.parse(md));
    for (const t of answer.querySelectorAll("table")) {
      const w = el("div", "md-table");
      t.replaceWith(w);
      w.appendChild(t);
    }
    scrollDown();
  };
  const updateCount = () => {
    const n = steps.children.length;
    count.textContent = n ? `· ${n} ${n === 1 ? "analysis" : "analyses"}` : "";
  };

  const api = {
    thinking(t) { think.textContent += t; think.scrollTop = think.scrollHeight; },
    text(t) {
      md += t;
      status.textContent = "Writing the read-out";
      if (!raf) raf = requestAnimationFrame(render);
    },
    status(t) { status.textContent = t; },
    toolStart(ev) {
      status.textContent = ev.label + "…";
      const li = el("li", "step running");
      li.append(el("span", "s-ic"), el("span", null, ev.label));
      steps.appendChild(li);
      stepEls[ev.id] = li;
      updateCount();
      scrollDown();
    },
    toolResult(ev) {
      const li = stepEls[ev.id];
      if (li) {
        li.className = "step " + (ev.ok ? "ok" : "err");
        if (ev.ok && ev.result) li.appendChild(el("code", null, paramText(ev.result.params)));
        if (!ev.ok) li.appendChild(el("span", "s-err", ev.error));
      }
      if (ev.ok && !ev.result.meta?.internal) { cards.appendChild(renderCard(ev.result)); tools.push(ev.result.tool); }
      scrollDown();
    },
    error(msg) {
      body.appendChild(el("div", "error-box", msg));
    },
    ask(ev) {
      asked = ev;
      const rec = renderAskRecord(ev);
      if (!state.restoring) rec.classList.add("waiting");
      body.insertBefore(rec, answerBox);
      if (!state.restoring) openAsk(ev);
    },
    finish() {
      if (raf) { cancelAnimationFrame(raf); render(); }
      answer.classList.remove("streaming");
      actions.hidden = false;
      copyBtn.hidden = !md.trim();
      trace.classList.remove("live");
      root.classList.remove("working");
      const n = steps.children.length;
      status.textContent = asked ? "Waiting for your answer" : n ? "Analysis complete" : "Answered";
      trace.classList.toggle("asking", Boolean(asked));
      if (md && !state.restoring && !asked) setFollowups(tools);
      if (!n && !think.textContent && !asked) trace.remove();
      else trace.open = false;
    },
    restore(turn) {
      state.restoring = true;
      think.textContent = turn.thinking || "";
      for (const s of turn.steps || []) {
        const li = el("li", "step " + (s.ok === false ? "err" : "ok"));
        li.append(el("span", "s-ic"), el("span", null, s.label));
        li.appendChild(s.ok === false ? el("span", "s-err", s.error || "failed") : el("code", null, paramText(s.input)));
        steps.appendChild(li);
      }
      updateCount();
      for (const r of turn.results || []) if (!r.meta?.internal) cards.appendChild(renderCard(r));
      if (turn.ask) api.ask(turn.ask);
      md = turn.text || "";
      render();
      api.finish();
      state.restoring = false;
    },
  };
  return api;
}

function paramText(params) {
  if (!params) return "";
  return Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join("  ");
}

// ---------------------------------------------------------------- brand comparison
const CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]; // same slots as the chart specs
const COMPARE_TABS = [
  ["scorecard", "Scorecard"], ["trend", "Share trend"], ["patients", "Patients"], ["payer", "Payer mix"],
  ["lines", "Lines & regimens"], ["persistency", "Persistency"], ["prescribers", "Prescribers"],
];

function defaultCompetitors(focus, n = 2) {
  const prods = state.meta?.products || [];
  const cls = prods.find((p) => p.product === focus)?.drug_class;
  const same = prods.filter((p) => p.product !== focus && p.drug_class === cls);
  const rest = prods.filter((p) => p.product !== focus && p.drug_class !== cls);
  return [...same, ...rest].slice(0, n).map((p) => p.product);
}


function renderCompare(initial) {
  let res = initial;
  let tab = "scorecard";
  let dirty = false;
  const card = el("article", "card compare");

  const head = el("header", "card-head");
  const title = el("div", "card-title", "Brand comparison");
  const tag = el("span", "card-tool", "side by side");
  const interpret = el("button", "card-act primary", "Ask CIQ to interpret");
  interpret.type = "button";
  interpret.hidden = true;
  interpret.addEventListener("click", () => {
    submit(`Interpret this brand comparison: ${res.params.products.join(" vs ")}. What are the key differences and what do they mean for ${res.params.products[0]}?`);
    interpret.hidden = true;
  });
  const dl = el("button", "card-act", "CSV");
  dl.type = "button";
  dl.addEventListener("click", () => downloadCSV(res));
  const method = methodPanel(res);
  head.append(title, tag, interpret, ...(method ? [method.button] : []), dl);

  const brandBar = el("div", "cmp-brands");
  const tabs = el("div", "cmp-tabs");
  const body = el("div", "card-body cmp-body");
  const overlay = el("div", "cmp-loading");
  overlay.append(el("span", "cmp-spinner"), el("span", null, "Updating comparison…"));
  overlay.hidden = true;
  card.append(head, ...(method ? [method.panel] : []), brandBar, tabs, body, overlay);

  for (const [key, label] of COMPARE_TABS) {
    const b = el("button", "tab", label);
    b.type = "button";
    b.dataset.key = key;
    b.addEventListener("click", () => { tab = key; paint(); });
    tabs.appendChild(b);
  }

  async function rerun(products) {
    overlay.hidden = false;
    try {
      const r = await fetch("/api/tools/compare_products", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ products }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.statusText);
      res = data;
      dirty = true;
      paint();
    } catch (e) {
      alert("Could not update comparison: " + e.message);
    } finally {
      overlay.hidden = true;
    }
  }

  function paintBrands() {
    brandBar.replaceChildren();
    const prods = res.params.products;
    prods.forEach((p, i) => {
      const pill = el("div", "cmp-brand");
      pill.style.setProperty("--brand", CATEGORICAL[i]);
      const sw = el("span", "cmp-swatch");
      const sel = el("select");
      for (const o of state.meta?.products || []) {
        const opt = el("option", null, o.product);
        opt.value = o.product;
        opt.disabled = prods.includes(o.product) && o.product !== p;
        sel.appendChild(opt);
      }
      sel.value = p;
      sel.title = "Swap brand";
      sel.addEventListener("change", () => rerun(prods.map((x, j) => (j === i ? sel.value : x))));
      pill.append(sw, sel);
      if (prods.length > 2) {
        const x = el("button", "cmp-remove", "×");
        x.type = "button";
        x.title = `Remove ${p}`;
        x.addEventListener("click", () => rerun(prods.filter((_, j) => j !== i)));
        pill.appendChild(x);
      }
      brandBar.appendChild(pill);
    });
    if (prods.length < 4) {
      const add = el("button", "cmp-add", "+ Add brand");
      add.type = "button";
      add.addEventListener("click", () => {
        const next = (state.meta?.products || []).map((o) => o.product).find((q) => !prods.includes(q));
        if (next) rerun([...prods, next]);
      });
      brandBar.appendChild(add);
    }
  }

  function paint() {
    title.textContent = res.title;
    interpret.hidden = !dirty;
    paintBrands();
    tabs.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.key === tab));
    body.replaceChildren();
    if (tab === "scorecard") {
      body.appendChild(renderScorecard(res));
    } else {
      const grid = el("div", "charts");
      body.appendChild(grid);
      for (const spec of res.charts.filter((s) => s.usermeta?.section === tab)) {
        const c = el("div", "chart");
        grid.appendChild(c);
        requestAnimationFrame(() => embed(c, spec));
      }
    }
    if (res.caveats?.length) {
      const d = el("details", "cmp-notes");
      d.appendChild(el("summary", null, "Method & caveats"));
      const ul = el("ul");
      for (const c of res.caveats) ul.appendChild(el("li", null, c));
      d.appendChild(ul);
      body.appendChild(d);
    }
  }

  paint();
  return card;
}

function renderScorecard(res) {
  const prods = res.params.products;
  const rows = Array.isArray(res.data) ? res.data : res.data.scorecard;
  const wrap = el("div", "table-wrap cmp-table-wrap");
  const t = el("table", "data scorecard");
  const thead = el("thead");
  const hr = el("tr");
  hr.appendChild(el("th", null, "Metric"));
  prods.forEach((p, i) => {
    const th = el("th", "brand-col", p);
    th.style.setProperty("--brand", CATEGORICAL[i]);
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  const tb = el("tbody");
  let section = null;
  for (const r of rows) {
    if (r.section !== section) {
      section = r.section;
      const sr = el("tr", "section-row");
      const td = el("td", null, section);
      td.colSpan = prods.length + 1;
      sr.appendChild(td);
      tb.appendChild(sr);
    }
    const tr = el("tr");
    tr.appendChild(el("td", "metric", r.metric));
    for (const p of prods) {
      const td = el("td", "val", r[p] ?? "—");
      if (r.leader === p) {
        td.classList.add("leader");
        td.title = "Leads on this metric";
      }
      tr.appendChild(td);
    }
    tb.appendChild(tr);
  }
  t.append(thead, tb);
  wrap.appendChild(t);
  return wrap;
}

function msgAction(icon, label, onClick) {
  const b = el("button", "ma-btn");
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = svgIcon(ICONS[icon]);
  b.addEventListener("click", onClick);
  return b;
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (e) {   // clipboard API unavailable (e.g. non-secure origin): fall back to a hidden textarea
    const t = el("textarea");
    t.value = text;
    t.style.position = "fixed"; t.style.opacity = "0";
    document.body.appendChild(t);
    t.select();
    document.execCommand("copy");
    t.remove();
  }
}

// ---------------------------------------------------------------- clarifying questions (ask_user)
// The agent pauses on a question; the panel above the composer collects the answer and the next message sends it.
function renderAskRecord(ask) {
  const rec = el("div", "ask-record");
  rec.dataset.id = ask.id;
  for (const q of ask.questions) {
    const row = el("div", "ar-row");
    row.append(el("span", "ar-ic", "?"), el("span", "ar-q", q.question));
    const opts = el("div", "ar-opts");
    for (const o of q.options) opts.appendChild(el("span", "ar-opt", o.label));
    row.appendChild(opts);
    rec.appendChild(row);
  }
  return rec;
}

function openAsk(ask) {
  state.ask = { ...ask, step: 0, answers: ask.questions.map(() => null) };
  setFollowups([]);
  $("#askPanel").hidden = false;
  $("#input").placeholder = "…or type your own answer";
  paintAsk();
}

function closeAsk() {
  state.ask = null;
  $("#askPanel").hidden = true;
  $("#input").placeholder = `Ask a question, e.g. “What is the ${state.product || "brand"} patient share in each line?”`;
}

function paintAsk() {
  const a = state.ask;
  const q = a.questions[a.step];
  const tabs = $("#askTabs");
  tabs.replaceChildren();
  if (a.questions.length > 1) {
    a.questions.forEach((qq, i) => {
      const t = el("button", "ask-tab" + (i === a.step ? " active" : "") + (a.answers[i] ? " done" : ""),
        qq.header || `Question ${i + 1}`);
      t.type = "button";
      t.addEventListener("click", () => { a.step = i; paintAsk(); });
      tabs.appendChild(t);
    });
  }
  $("#askQuestion").textContent = q.question;
  const box = $("#askOptions");
  box.replaceChildren();
  const chosen = new Set(a.answers[a.step] || []);
  q.options.forEach((o, i) => {
    const b = el("button", "ask-opt" + (chosen.has(o.label) ? " selected" : ""));
    b.type = "button";
    b.setAttribute("role", "option");
    b.setAttribute("aria-selected", chosen.has(o.label));
    const num = el("span", "ao-num", q.multi_select ? (chosen.has(o.label) ? "✓" : "") : String(i + 1));
    if (q.multi_select) num.classList.add("check");
    const txt = el("span", "ao-text");
    txt.appendChild(el("span", "ao-label", o.label));
    if (o.description) txt.appendChild(el("span", "ao-desc", o.description));
    b.append(num, txt);
    b.addEventListener("click", () => {
      if (q.multi_select) {
        chosen.has(o.label) ? chosen.delete(o.label) : chosen.add(o.label);
        a.answers[a.step] = q.options.map((x) => x.label).filter((l) => chosen.has(l));
        paintAsk();
      } else {
        a.answers[a.step] = [o.label];
        askAdvance();
      }
    });
    box.appendChild(b);
  });
  const next = $("#askNext");
  const last = a.step === a.questions.length - 1;
  next.hidden = !q.multi_select && !(a.questions.length > 1 && a.answers[a.step]);
  next.textContent = last ? "Send answer" : "Next";
  next.disabled = q.multi_select && !(a.answers[a.step] || []).length;
  $("#askHint").textContent = q.multi_select
    ? "Select all that apply, or type your own answer below"
    : "Pick an option, press its number, or type your own answer below";
}

function askAdvance() {
  const a = state.ask;
  const nextOpen = a.answers.findIndex((x, i) => i > a.step && !x);
  const anyOpen = a.answers.findIndex((x) => !x);
  if (nextOpen >= 0 || (anyOpen >= 0 && anyOpen !== a.step)) {
    a.step = nextOpen >= 0 ? nextOpen : anyOpen;
    paintAsk();
    return;
  }
  answerAsk(a.answers);
}

// answers: list per question (null = skipped); null overall = skip everything
function answerAsk(answers) {
  const a = state.ask;
  if (!a) return;
  submit(formatAnswer(a, answers));
}

function formatAnswer(a, answers) {
  if (!answers) return "No preference - use your best judgement.";
  const parts = a.questions.map((q, i) => {
    const v = answers[i]?.length ? answers[i].join(", ") : "no preference";
    return a.questions.length > 1 ? `${q.header || q.question}: ${v}` : v;
  });
  return parts.join("\n");
}

// a typed answer fills the current question; anything already picked for the others is kept
function askTypedAnswer(text) {
  const a = state.ask;
  const answers = a.answers.slice();
  answers[a.step] = [text];
  return formatAnswer(a, answers);
}

// ---------------------------------------------------------------- result cards
function actionButton(icon, label, onClick) {
  const b = el("button", "card-act icon");
  b.type = "button";
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = svgIcon(ICONS[icon]);
  b.addEventListener("click", onClick);
  return b;
}

function renderKpis(kpis) {
  const strip = el("div", "kpi-strip");
  for (const k of kpis) {
    const t = el("div", "kpi-tile" + (k.lead ? " lead" : ""));
    t.append(el("div", "kt-val", fmtKpi(k)), el("div", "kt-lab", k.label));
    if (k.note) t.appendChild(el("div", "kt-note", k.note));
    strip.appendChild(t);
  }
  return strip;
}

function renderCard(res) {
  if (res.tool === "compare_products") return renderCompare(res);
  if (res.tool === "compare_segments") return renderDiagnostic(res);
  const card = el("article", "card");
  const head = el("header", "card-head");
  const titles = el("div", "card-titles");
  titles.appendChild(el("h3", "card-title", res.title));
  if (res.subtitle) titles.appendChild(el("p", "card-sub", res.subtitle));
  const meta = el("div", "card-meta");
  const period = fmtPeriod(res.meta?.coverage);
  if (period) meta.appendChild(el("span", null, `Claims ${period}`));
  meta.appendChild(el("span", "card-tool", res.tool));
  titles.appendChild(meta);

  const controls = el("div", "card-controls");
  const tabs = el("div", "tabs");
  tabs.setAttribute("role", "tablist");
  const views = {};
  const body = el("div", "card-body");
  const names = res.charts?.length ? ["Chart", "Data", "Notes"] : ["Data", "Notes"];
  for (const n of names) {
    const b = el("button", "tab", n);
    b.type = "button";
    b.setAttribute("role", "tab");
    b.addEventListener("click", () => show(n));
    tabs.appendChild(b);
    views[n] = { btn: b, pane: null };
  }
  const chartViews = [];
  const actions = el("div", "card-actions");
  const method = methodPanel(res);
  if (method) actions.appendChild(method.button);
  actions.appendChild(actionButton("download", "Download data (CSV)", () => downloadCSV(res)));
  const png = actionButton("image", "Download charts (PNG)", () => downloadPNG(res, chartViews));
  png.hidden = !res.charts?.length;
  const expand = actionButton("expand", "Expand", () => setExpanded(!card.classList.contains("expanded")));
  actions.append(png, expand);
  controls.append(tabs, actions);
  head.append(titles, controls);
  card.append(head);
  if (method) card.append(method.panel);
  card.append(body);

  function setExpanded(on) {
    card.classList.toggle("expanded", on);
    document.body.classList.toggle("has-expanded", on);
    expand.innerHTML = svgIcon(ICONS[on ? "collapse" : "expand"]);
    expand.title = on ? "Collapse (Esc)" : "Expand";
    // vega-embed's container width follows window resizes
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  }
  card.addEventListener("collapse", () => setExpanded(false));

  function build(n) {
    const pane = el("div", "pane");
    if (n === "Chart") {
      if (res.kpis?.length) pane.appendChild(renderKpis(res.kpis));
      const grid = el("div", "charts");
      // a bar chart + donut pair: give the bar chart more room
      if (res.charts.length === 2 && res.charts[1].layer?.[0]?.mark?.type === "arc") grid.classList.add("split");
      pane.appendChild(grid);
      for (const spec of res.charts) {
        const c = el("div", "chart");
        grid.appendChild(c);
        requestAnimationFrame(() => embed(c, spec).then((v) => v && chartViews.push(v)));
      }
      // older saved results predate `observations`
      const observations = res.observations ?? res.insights ?? [];
      if (observations.length) {
        const box = el("div", "card-insights");
        box.appendChild(el("h4", null, observations.length === 1 ? "Key observation" : "Key observations"));
        const ul = el("ul");
        for (const i of observations) ul.appendChild(el("li", null, i));
        box.appendChild(ul);
        pane.appendChild(box);
      }
    } else if (n === "Data") {
      pane.appendChild(renderData(res.data));
    } else {
      const notes = el("div", "notes");
      const figures = [...new Set([...(res.insights || []), ...(res.observations || [])])];
      if (figures.length) {
        notes.appendChild(el("h4", null, "Key figures"));
        const ul = el("ul");
        for (const i of figures) ul.appendChild(el("li", null, i));
        notes.appendChild(ul);
      }
      if (res.caveats?.length) {
        notes.appendChild(el("h4", null, "Method & caveats"));
        const ul = el("ul");
        for (const c of res.caveats) ul.appendChild(el("li", null, c));
        notes.appendChild(ul);
      }
      notes.appendChild(el("h4", null, "Parameters"));
      notes.appendChild(el("div", "params", paramText(res.params) || "defaults"));
      pane.appendChild(notes);
    }
    return pane;
  }
  function show(n) {
    for (const [k, v] of Object.entries(views)) {
      v.btn.classList.toggle("active", k === n);
      v.btn.setAttribute("aria-selected", k === n);
      if (v.pane) v.pane.hidden = k !== n;
    }
    if (!views[n].pane) { views[n].pane = build(n); body.appendChild(views[n].pane); }
  }
  show(names[0]);
  return card;
}

// ---------------------------------------------------------------- "how this was calculated" (i) panel
function methodPanel(res) {
  const m = res.method;
  if (!m || !(m.steps?.length || m.question)) return null;
  const panel = el("section", "method");
  panel.hidden = true;
  const button = actionButton("info", "How this was calculated", () => {
    panel.hidden = !panel.hidden;
    button.classList.toggle("on", !panel.hidden);
    button.setAttribute("aria-expanded", String(!panel.hidden));
  });
  button.setAttribute("aria-expanded", "false");

  const head = el("div", "m-head");
  head.append(el("span", "m-kicker", "How this was calculated"));
  if (m.question) head.append(el("p", "m-question", m.question));
  panel.appendChild(head);
  const grid = el("div", "m-grid");
  panel.appendChild(grid);

  const section = (title, node) => { const s = el("div", "m-sec"); s.append(el("h5", null, title), node); grid.appendChild(s); return s; };
  const list = (items, tag = "ul") => { const l = el(tag); for (const i of items) l.appendChild(el("li", null, i)); return l; };

  if (m.steps?.length) section("Calculation", list(m.steps, "ol"));
  if (m.reasoning) section("Why it matters", el("p", null, m.reasoning));
  if (m.definitions?.length) section("Definitions", list(m.definitions));
  if (m.sql) {
    const pre = el("pre", "m-sql");
    pre.appendChild(el("code", null, m.sql));
    section("Query", pre).classList.add("wide");
  }
  if (m.lot_rules?.length) {
    const ol = el("ol", "m-rules");
    for (const r of m.lot_rules) {
      const li = el("li");
      li.append(el("span", "m-rule", r.rule), el("span", "m-applied", r.applied));
      ol.appendChild(li);
    }
    const s = section("Line-of-therapy business rules", ol);
    s.classList.add("wide");
    if (m.lot_scope) s.appendChild(el("p", "m-note", m.lot_scope));
  }
  const facts = el("dl", "m-facts");
  const fact = (k, v) => { if (v) facts.append(el("dt", null, k), el("dd", null, v)); };
  fact("Data", res.meta?.coverage ? `${res.meta.coverage.n_patients.toLocaleString()} patients, claims ${fmtPeriod(res.meta.coverage)}` : "");
  fact("Parameters", paramText(m.params) || "defaults");
  fact("Source", m.source);
  const src = section("Inputs", facts);
  src.classList.add("wide");
  if (res.caveats?.length) section("Caveats", list(res.caveats)).classList.add("wide");
  return { button, panel };
}

// ---------------------------------------------------------------- compare: dialog (diagnostic compare)
const CX_MODES = [
  { id: "brands", label: "Brands" }, { id: "hcp", label: "HCP cohorts" }, { id: "time", label: "Time periods" },
  { id: "patient", label: "Patient cohorts" }, { id: "geo", label: "Geographies", disabled: true },
];
const cx = { mode: "brands", opts: null, a: null, b: null, extra: [] };

function initCompare() {
  const modes = $("#cxModes");
  for (const m of CX_MODES) {
    const b = el("button", "cx-mode", m.label);
    b.type = "button";
    b.dataset.mode = m.id;
    b.setAttribute("role", "tab");
    if (m.disabled) { b.classList.add("soon"); b.appendChild(el("span", "cx-soon", "needs data")); }
    b.addEventListener("click", () => setCompareMode(m.id));
    modes.appendChild(b);
  }
  $("#cxClose").addEventListener("click", closeCompare);
  $("#cxCancel").addEventListener("click", closeCompare);
  $("#compareModal").addEventListener("click", (e) => { if (e.target.id === "compareModal") closeCompare(); });
  $("#cxSwap").addEventListener("click", () => { [cx.a, cx.b] = [cx.b, cx.a]; paintCompare(); });
  $("#cxGo").addEventListener("click", runCompare);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#compareModal").hidden) closeCompare(); });
}

async function openCompare(mode, preset) {
  if (state.busy) return;
  $("#compareModal").hidden = false;
  $("#cxErr").textContent = "";
  try {
    if (!cx.opts || cx.opts.product !== state.product) {
      cx.opts = await (await fetch(`/api/compare/options?product=${encodeURIComponent(state.product)}`)).json();
    }
  } catch (e) { $("#cxErr").textContent = "Could not load comparison options."; return; }
  setCompareMode(mode || cx.mode, preset);
}

function closeCompare() { $("#compareModal").hidden = true; $("#btnCompare").focus(); }

function cxDefaults(mode) {
  const months = cx.opts.months.map((m) => m.id);
  const split = Math.min(3, months.length - 1);
  return {
    brands: [state.product, defaultCompetitors(state.product, 1)[0]],
    hcp: ["high_high", "high_low"],
    time: [`${months[0]}..${months[split - 1]}`, `${months[split]}..${months[months.length - 1]}`],
    patient: ["age_lt65", "age_65plus"],
    geo: [null, null],
  }[mode];
}

function cxPresets(mode) {
  const p = state.product;
  const months = cx.opts.months.map((m) => m.id);
  const last = months[months.length - 1];
  return {
    brands: defaultCompetitors(p, 3).map((c) => ({ label: `${p} vs ${c}`, a: p, b: c })),
    hcp: [{ label: "Decile 1 vs Decile 7", a: "decile:1", b: "decile:7" },
      { label: `High vs low ${p} share HCPs`, a: "high_high", b: "high_low" },
      { label: "Low-share HCPs vs all HCPs (benchmark)", a: "high_low", b: "all" },
      ...cx.opts.hcp.filter((o) => o.saved).slice(-2).map((o) => ({ label: `${o.label} vs all HCPs`, a: o.id, b: "all" }))],
    time: [{ label: `${cx.opts.months[0].label.split(" ")[0]}–${cx.opts.months[2].label} vs later`, ...(() => { const d = cxDefaults("time"); return { a: d[0], b: d[1] }; })() },
      { label: "First vs last month", a: `${months[0]}..${months[0]}`, b: `${last}..${last}` },
      { label: "Last 2 months vs prior 2", a: `${months[months.length - 4]}..${months[months.length - 3]}`, b: `${months[months.length - 2]}..${last}` }],
    patient: [{ label: "Under 65 vs 65+", a: "age_lt65", b: "age_65plus" },
      { label: "Medicare vs Commercial", a: "medicare", b: "commercial" },
      { label: "Line 1 vs reached line 2+", a: "line1", b: "line2plus" }],
    geo: [],
  }[mode];
}

function setCompareMode(mode, preset) {
  if (mode === "geo") {
    $("#cxGeo").hidden = false;
    $("#cxGeo").textContent = cx.opts.geography.reason + " Connect a geography source from Data sources to compare regions.";
    return;
  }
  $("#cxGeo").hidden = true;
  cx.mode = mode;
  [cx.a, cx.b] = preset ? [preset.a, preset.b] : cxDefaults(mode);
  cx.extra = [];
  paintCompare();
}

function cxSelect(options, value, onChange) {
  const sel = el("select", "cx-select");
  for (const o of options) {
    const opt = el("option", null, o.label);
    opt.value = o.id;
    if (o.detail) opt.title = o.detail;
    sel.appendChild(opt);
  }
  sel.value = value;
  sel.addEventListener("change", () => onChange(sel.value));
  return sel;
}

function cxPicker(side) {
  const box = el("div", "cx-fields");
  const val = cx[side];
  const set = (v) => { cx[side] = v; paintCompare(); };
  if (cx.mode === "brands") {
    box.appendChild(brandPicker(val, set, { label: `Side ${side.toUpperCase()} brand`, wide: true }).node);
    const p = state.meta?.products.find((x) => x.product === val);
    const nice = (c) => c.split(" ").map((w) => (/\d/.test(w) || w.length <= 4 ? w : w.toLowerCase())).join(" ");
    if (p) box.appendChild(el("span", "cx-hint", `${nice(p.drug_class)} · ${p.molecules} · ${p.patients.toLocaleString()} patients`));
  } else if (cx.mode === "hcp") {
    const groups = [["Segments", cx.opts.hcp.filter((o) => !o.id.startsWith("decile:") && !o.saved)],
      [`Deciles (${state.product} volume)`, cx.opts.hcp.filter((o) => o.id.startsWith("decile:"))],
      ["Saved cohorts", cx.opts.hcp.filter((o) => o.saved)]];
    const sel = el("select", "cx-select");
    for (const [g, list] of groups) {
      if (!list.length) continue;
      const og = el("optgroup"); og.label = g;
      for (const o of list) { const op = el("option", null, o.label); op.value = o.id; og.appendChild(op); }
      sel.appendChild(og);
    }
    sel.value = val;
    sel.addEventListener("change", () => set(sel.value));
    box.appendChild(sel);
    const o = cx.opts.hcp.find((x) => x.id === val);
    if (o?.detail) box.appendChild(el("span", "cx-hint", o.detail));
  } else if (cx.mode === "time") {
    const [from, to] = val.split("..");
    const row = el("div", "cx-range");
    row.append(cxSelect(cx.opts.months, from, (v) => set(`${v}..${to < v ? v : to}`)), el("span", null, "to"),
      cxSelect(cx.opts.months, to, (v) => set(`${from > v ? v : from}..${v}`)));
    box.appendChild(row);
  } else if (cx.mode === "patient") {
    box.appendChild(cxSelect(cx.opts.patient, val, set));
  }
  return box;
}

function paintCompare() {
  for (const b of $("#cxModes").children) b.classList.toggle("active", b.dataset.mode === cx.mode);
  $("#cxA").replaceChildren(cxPicker("a"));
  $("#cxB").replaceChildren(cxPicker("b"));
  const presets = $("#cxPresets");
  presets.replaceChildren(el("span", "cx-presets-label", "Quick compare"));
  for (const pr of cxPresets(cx.mode)) {
    const b = el("button", "cx-preset" + (pr.a === cx.a && pr.b === cx.b ? " on" : ""), pr.label);
    b.type = "button";
    b.addEventListener("click", () => { cx.a = pr.a; cx.b = pr.b; cx.extra = []; paintCompare(); });
    presets.appendChild(b);
  }
  // brands: optional 3rd/4th brand switches to the side-by-side panel
  const more = $("#cxMore");
  more.replaceChildren();
  if (cx.mode === "brands") {
    cx.extra.forEach((v, i) => {
      const row = el("div", "cx-extra");
      row.append(el("span", "cx-tag c", String.fromCharCode(67 + i)),
        cxSelect((state.meta?.products || []).map((p) => ({ id: p.product, label: p.product })), v, (nv) => { cx.extra[i] = nv; }));
      const x = el("button", "cx-x", "×");
      x.type = "button"; x.title = "Remove";
      x.addEventListener("click", () => { cx.extra.splice(i, 1); paintCompare(); });
      row.appendChild(x);
      more.appendChild(row);
    });
    if (cx.extra.length < 2) {
      const add = el("button", "cx-add", "+ Add brand (3–4 brands open the side-by-side panel)");
      add.type = "button";
      add.addEventListener("click", () => {
        const used = [cx.a, cx.b, ...cx.extra];
        const next = defaultCompetitors(state.product, 6).find((c) => !used.includes(c));
        if (next) { cx.extra.push(next); paintCompare(); }
      });
      more.appendChild(add);
    }
  }
  const c = cx.opts.context;
  const ctx = $("#cxContext");
  ctx.replaceChildren();
  const item = (label, value) => { const s = el("span", "cx-ctx"); s.append(el("b", null, "✓ " + label), document.createTextNode(" " + value)); ctx.appendChild(s); };
  item("Same market", c.market.replace(/\s*\(.*\)\s*/, " (NSCLC)"));
  item("Same period", `${fmtMonth(c.start)} – ${fmtMonth(c.end)}`);
  item("Same definitions", "metrics, lines of therapy, HCP attribution");
  if (cx.mode !== "brands") item("Brand", state.product);
  $("#cxErr").textContent = cx.a === cx.b ? "Pick two different sides." : "";
  $("#cxGo").disabled = cx.a === cx.b;
}

async function runCompare() {
  if (state.busy || cx.a === cx.b) return;
  const multi = cx.mode === "brands" && cx.extra.length;
  const payload = multi
    ? { kind: "multi", products: [...new Set([cx.a, cx.b, ...cx.extra])], product: state.product, conversation_id: state.convId }
    : { kind: cx.mode, a: cx.a, b: cx.b, product: state.product, conversation_id: state.convId };
  $("#cxGo").disabled = true;
  $("#cxErr").textContent = "";
  try {
    const r = await fetch("/api/compare", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await r.json();
    if (!r.ok) throw new Error(data.detail || r.statusText);
    closeCompare();
    showPanelResult(data);
  } catch (e) {
    $("#cxErr").textContent = String(e.message || e);
  } finally {
    $("#cxGo").disabled = false;
  }
}

// a result run from a panel (not by the model) joins the conversation like a normal exchange
function showPanelResult(data) {
  setFollowups([]);
  closeAsk();
  $("#welcome").hidden = true;
  state.convId = data.conversation.id;
  history.replaceState(null, "", `/?c=${state.convId}`);
  $("#convTitle").textContent = data.conversation.title;
  addUserMsg(data.text, state.product);
  const m = createAssistantMsg();
  m.restore({ results: [data.result], text: "", steps: [{ label: "Compared side A and side B", input: {}, ok: true }] });
  setFollowups([data.result.tool]);
  loadHistory();
  scrollDown(true);
}

// ---------------------------------------------------------------- compare: diagnostic card
function renderDiagnostic(res) {
  const c = res.meta.compare;
  const [A, B] = [c.a, c.b];
  const card = el("article", "card diag");
  const head = el("header", "card-head");
  const titles = el("div", "card-titles");
  const t = el("h3", "card-title dg-title");
  t.append(el("span", "dg-chip a", A), el("span", "dg-vs", "vs"), el("span", "dg-chip b", B));
  titles.appendChild(t);
  titles.appendChild(el("p", "card-sub", res.subtitle || ""));
  const meta = el("div", "card-meta");
  meta.append(el("span", "dg-ok", "✓ Same market"), el("span", "dg-ok", "✓ Same period"), el("span", "dg-ok", "✓ Same definitions"),
    el("span", null, `Claims ${fmtPeriod(res.meta.coverage)}`), el("span", "card-tool", res.tool));
  titles.appendChild(meta);
  const actions = el("div", "card-actions");
  const method = methodPanel(res);
  if (method) actions.appendChild(method.button);
  actions.appendChild(actionButton("download", "Download data (CSV)", () => downloadCSV(res)));
  const expand = actionButton("expand", "Expand", () => {
    const on = !card.classList.contains("expanded");
    card.classList.toggle("expanded", on);
    document.body.classList.toggle("has-expanded", on);
    requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
  });
  card.addEventListener("collapse", () => expand.click());
  actions.appendChild(expand);
  head.append(titles, actions);
  card.appendChild(head);
  if (method) card.appendChild(method.panel);
  const body = el("div", "card-body");
  card.appendChild(body);

  // 1. the duel: headline metric A | gap | B
  const k = res.kpis || [];
  if (k.length >= 3) {
    const duel = el("div", "dg-duel");
    const side = (cls, kp, label) => {
      const d = el("div", `dg-side ${cls}` + (kp.lead ? " lead" : ""));
      d.append(el("span", "dg-side-label", label), el("span", "dg-side-val", fmtKpi(kp)), el("span", "dg-side-metric", c.headline));
      return d;
    };
    const gap = el("div", "dg-gap");
    const leader = k[0].value >= k[1].value ? A : B;
    gap.append(el("span", "dg-gap-val", `${k[2].value.toFixed(1)}pp`), el("span", "dg-gap-lab", `gap · ${leader} ahead`));
    duel.append(side("a", k[0], A), gap, side("b", k[1], B));
    body.appendChild(duel);
  }

  // 2. what is different
  const sec = (title, sub) => { const s = el("section", "dg-sec"); const h = el("h4", null, title); s.appendChild(h); if (sub) s.appendChild(el("p", "dg-sec-sub", sub)); body.appendChild(s); return s; };
  const score = Array.isArray(res.data) ? res.data : res.data.scorecard;
  const key = new Set([...score].filter((r) => r.diff != null && (r.kind === "pct" || r.kind === "pp"))
    .sort((x, y) => Math.abs(y.diff) - Math.abs(x.diff)).slice(0, 3).map((r) => r.metric));
  const s1 = sec("What is different", "Difference = B minus A. Shares in percentage points; counts as change and %.");
  const wrap = el("div", "table-wrap dg-table-wrap");
  const tbl = el("table", "data dg-score");
  const hr = el("tr");
  for (const [txt, cls] of [["Metric", ""], [A, "a"], [B, "b"], ["Difference", ""]]) hr.appendChild(el("th", cls, txt));
  const thead = el("thead"); thead.appendChild(hr);
  const tb = el("tbody");
  let section = null;
  for (const r of score) {
    if (r.section !== section) {
      section = r.section;
      const tr = el("tr", "section-row"); const td = el("td", null, section); td.colSpan = 4; tr.appendChild(td); tb.appendChild(tr);
    }
    const tr = el("tr", key.has(r.metric) ? "key" : "");
    const m = el("td", "metric", r.metric);
    if (key.has(r.metric)) m.appendChild(el("span", "dg-key", "key gap"));
    const diff = el("td", "num");
    const pill = el("span", "dg-diff" + (r.diff == null || Math.abs(r.diff) < 1e-9 ? "" : r.diff > 0 ? " b" : " a"), r.diff_fmt);
    diff.appendChild(pill);
    tr.append(m, el("td", "num", r.a_fmt), el("td", "num", r.b_fmt), diff);
    tb.appendChild(tr);
  }
  tbl.append(thead, tb);
  wrap.appendChild(tbl);
  s1.appendChild(wrap);

  // 3. where it is different
  const s2 = sec("Where it is different");
  if (res.observations?.length) {
    const ul = el("ul", "dg-obs");
    for (const o of res.observations) ul.appendChild(el("li", null, o));
    s2.appendChild(ul);
  }
  const grid = el("div", "charts");
  s2.appendChild(grid);
  for (const spec of res.charts || []) {
    const ch = el("div", "chart");
    grid.appendChild(ch);
    requestAnimationFrame(() => embed(ch, spec));
  }

  // 4. who: the opportunity list, export, save as cohort
  const opp = c.opportunity;
  if (opp?.count) {
    const s3 = sec("Who is behind it");
    const box = el("div", "dg-opp");
    const head2 = el("div", "dg-opp-head");
    const big = el("span", "dg-opp-n", opp.count.toLocaleString());
    const txt = el("span", "dg-opp-text");
    txt.append(el("b", null, opp.label), el("span", null, opp.concentration != null
      ? `The top 20% of them account for ${fmtPct(opp.concentration)} of the gap.` : "Ready to export or save as a cohort."));
    head2.append(big, txt);
    const btns = el("div", "dg-opp-actions");
    const frame = res.data.opportunity_hcps;
    const tableBox = el("div", "dg-opp-table");
    tableBox.hidden = true;
    let shown = frame || [], filtering = false, search = null;
    const save = el("button", "cm-btn primary", "Save as cohort");
    const exp = el("button", "cm-btn", "Export CSV");
    if (frame?.length) {
      const view = el("button", "cm-btn", "View HCPs");
      view.type = "button";
      view.addEventListener("click", () => {
        tableBox.hidden = !tableBox.hidden;
        view.textContent = tableBox.hidden ? "View HCPs" : "Hide HCPs";
        if (!search) {
          search = searchableTable(frame, { total: opp.count, onFilter: (rows, f) => {
            shown = rows; filtering = f;
            exp.textContent = f ? `Export ${rows.length.toLocaleString()} (CSV)` : "Export CSV";
            save.textContent = f ? `Save ${rows.length.toLocaleString()} as cohort` : "Save as cohort";
          } });
          tableBox.appendChild(search.node);
        }
        if (!tableBox.hidden) search.input.focus();
      });
      exp.type = "button";
      exp.addEventListener("click", () => downloadCSV({ tool: "opportunity_hcps", data: { hcps: shown } }));
      btns.append(view, exp);
    }
    save.type = "button";
    const form = el("form", "dg-save");
    form.hidden = true;
    const name = el("input");
    name.value = opp.label;
    name.setAttribute("aria-label", "Cohort name");
    const ok = el("button", "cm-btn primary", "Save");
    ok.type = "submit";
    form.append(name, ok);
    save.addEventListener("click", () => { form.hidden = false; save.hidden = true; name.select(); });
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      ok.disabled = true;
      try {
        const r = await fetch("/api/cohorts", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.value, product: opp.product,
            hcp_ids: filtering ? shown.map((r) => r.hcp_id) : opp.hcp_ids,
            definition: `${opp.label} (${A} vs ${B})` + (filtering ? `, filtered by “${search.input.value.trim()}”` : "") }) });
        const saved = await r.json();
        if (!r.ok) throw new Error(saved.detail || r.statusText);
        cx.opts = null;   // refresh the dialog's cohort list
        form.replaceChildren(el("span", "dg-saved", `✓ Saved “${saved.name}” (${saved.size.toLocaleString()} HCPs)`));
        const cmp = el("button", "cm-btn", "Compare this cohort vs all HCPs");
        cmp.type = "button";
        cmp.addEventListener("click", async () => {
          await openCompare("hcp", { a: `saved:${saved.id}`, b: "all" });
          runCompare();
        });
        form.appendChild(cmp);
      } catch (err) { ok.disabled = false; name.setCustomValidity(String(err.message || err)); name.reportValidity(); }
    });
    btns.append(save);
    box.append(head2, btns, form, tableBox);
    s3.appendChild(box);
  }

  // 5. why
  const why = el("div", "dg-why");
  const ask = el("button", "dg-why-btn");
  ask.type = "button";
  ask.innerHTML = svgIcon("M12 3l1.9 5.8H20l-4.9 3.6 1.9 5.8-5-3.6-5 3.6 1.9-5.8L4 8.8h6.1z");
  ask.appendChild(document.createTextNode("Why are they different?"));
  ask.addEventListener("click", () => submit(`Why are ${A} and ${B} different? Explain what drives the gap in this comparison ` +
    `(what, where, the likely drivers) and what the ${c.product || A} team could do about it.`));
  why.append(ask, el("span", "dg-why-hint", "CIQ digs into the drivers above and explains the gap."));
  if (c.kind === "hcp" && B !== "All HCPs") {
    const bench = el("button", "cm-btn", `Compare ${A} vs all HCPs`);
    bench.type = "button";
    bench.addEventListener("click", async () => { await openCompare("hcp", { a: res.params.a, b: "all" }); runCompare(); });
    why.appendChild(bench);
  }
  body.appendChild(why);

  const det = el("details", "dg-data");
  det.appendChild(el("summary", null, "All data tables"));
  det.addEventListener("toggle", () => { if (det.open && det.children.length === 1) det.appendChild(renderData(res.data)); }, { once: true });
  body.appendChild(det);
  return card;
}

// ---------------------------------------------------------------- account (profile row + menu, like Claude)
function setUser(u) {
  const name = (u?.name || "Analyst").trim();
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "A";
  $("#acctAvatar").textContent = initials;
  $("#acctName").textContent = name;
  $("#acctRole").textContent = u?.role || "";
  $("#amName").textContent = name;
  $("#amEmail").textContent = u?.email || u?.role || "";
  $("#footStatus").dataset.tip = name;
}

function initAccountMenu() {
  const btn = $("#footStatus"), menu = $("#acctMenu");
  const setOpen = (open) => {
    if (open && $("#app").classList.contains("nav-collapsed") && !narrowScreen.matches) setSidebarCollapsed(false, true);
    menu.hidden = !open;
    btn.setAttribute("aria-expanded", String(open));
  };
  btn.addEventListener("click", (e) => { e.stopPropagation(); setOpen(menu.hidden); });
  document.addEventListener("click", (e) => { if (!menu.hidden && !e.target.closest(".account")) setOpen(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !menu.hidden) setOpen(false); });
  $("#amSources").addEventListener("click", (e) => {
    e.stopPropagation();
    setOpen(false);
    $("#srcPill").click();
  });
  $("#amKeys").addEventListener("click", () => { $("#amKeyList").hidden = !$("#amKeyList").hidden; });
  const mac = /Mac|iPhone|iPad/.test(navigator.platform);
  for (const k of document.querySelectorAll(".am-keys kbd.mk")) k.textContent = (mac ? "⌘" : "Ctrl ") + k.textContent;
}

// ---------------------------------------------------------------- data sources (IQVIA connected; others preview only)
const KIND_ICONS = {
  payer: "M12 3l7 3v5c0 4.5-3 8.3-7 10-4-1.7-7-5.5-7-10V6zM9 12l2 2 4-4",
  crm: "M16 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM8 13a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM2 20c0-3 2.7-5 6-5s6 2 6 5M14 15.5c.6-.3 1.3-.5 2-.5 3.3 0 6 2 6 5",
  specialty: "M10.5 20.5l10-10a4.95 4.95 0 1 0-7-7l-10 10a4.95 4.95 0 1 0 7 7zM8.5 8.5l7 7",
  research: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  custom: "M3 5h18v14H3zM3 10h18M9 10v9",
};
const SOURCE_KINDS = [
  { id: "payer", name: "Payer & formulary", desc: "Formulary status, covered lives, PA and step edits by plan", join: "Payer plan id" },
  { id: "crm", name: "Promotional / CRM", desc: "Rep calls, samples, speaker programs, HCP engagement", join: "HCP (NPI)" },
  { id: "specialty", name: "Specialty pharmacy & hub", desc: "Dispense, adherence and patient-support program data", join: "Patient token" },
  { id: "research", name: "Market research", desc: "ATU, chart audits and survey data", join: "HCP (NPI)" },
  { id: "custom", name: "Custom table", desc: "Any other table you want CIQ to use", join: "" },
];
const PLATFORMS = [
  { id: "snowflake", logo: "snowflake.svg", name: "Snowflake", sub: "Data Cloud", auth: ["Key pair", "OAuth", "PrivateLink"],
    fields: [["Account URL", "xy12345.us-east-1.snowflakecomputing.com"], ["Warehouse", "ANALYTICS_WH"], ["Database", "COMMERCIAL"],
      ["Schema", "PAYER"], ["Table", "FORMULARY_STATUS"], ["Role", "CIQ_READER"]] },
  { id: "databricks", logo: "databricks.svg", name: "Databricks", sub: "Unity Catalog · SQL warehouse", auth: ["Service principal", "OAuth"],
    fields: [["Workspace URL", "https://adb-123.azuredatabricks.net"], ["HTTP path", "/sql/1.0/warehouses/abc123"], ["Catalog", "commercial"],
      ["Schema", "crm"], ["Table", "calls"]] },
  { id: "aws", logo: "aws.svg", wide: true, name: "Amazon Web Services", sub: "Redshift · S3 · Athena", auth: ["IAM role", "PrivateLink"],
    fields: [["Service", "Redshift, S3 or Athena"], ["Region", "us-east-1"], ["Endpoint or bucket", "s3://commercial-data/payer/"],
      ["Database or prefix", "payer"], ["Table or file pattern", "formulary_*.parquet"], ["IAM role ARN", "arn:aws:iam::123456789012:role/ciq-reader"]] },
  { id: "azure", logo: "azure.svg", name: "Microsoft Azure", sub: "Synapse · Data Lake · SQL", auth: ["Entra ID", "Managed identity", "Private Link"],
    fields: [["Service", "Synapse, Data Lake or Azure SQL"], ["Server or account", "commercial.sql.azuresynapse.net"],
      ["Database or container", "commercial"], ["Schema or path", "crm"], ["Table", "rep_calls"]] },
  { id: "bigquery", logo: "bigquery.svg", name: "Google BigQuery", sub: "Google Cloud", auth: ["Service account", "Workload identity"],
    fields: [["Project", "commercial-analytics"], ["Dataset", "payer"], ["Table", "formulary_status"]] },
  { id: "upload", icon: "M12 16V4M7 9l5-5 5 5M4 20h16", name: "Upload a file", sub: "CSV · Parquet · Excel", auth: ["Up to 1 GB"],
    fields: [["File", ""], ["Table name in CIQ", "payer_formulary"]] },
];
const JOIN_KEYS = ["HCP (NPI)", "Payer plan id", "Patient token", "Product (NDC / HCPCS)", "None (reference table)"];

function platformLogo(pl, cls) {
  const box = el("span", cls + (pl.wide ? " wide" : ""));
  if (pl.logo) {
    const img = el("img");
    img.src = `/static/assets/logos/${pl.logo}`;
    img.alt = "";
    box.appendChild(img);
  } else {
    box.innerHTML = svgIcon(pl.icon);
    box.classList.add("glyph");
  }
  return box;
}

function initDataSources() {
  const pill = $("#srcPill"), pop = $("#srcPop");
  const avail = $("#srcAvailable");
  for (const k of SOURCE_KINDS.filter((x) => x.id !== "custom")) {
    const row = el("div", "sp-item");
    const logo = el("span", "sp-logo kind");
    logo.innerHTML = svgIcon(KIND_ICONS[k.id]);
    const text = el("span", "sp-text");
    text.append(el("span", "sp-name", k.name), el("span", "sp-desc", k.desc));
    const btn = el("button", "sp-btn", "Connect");
    btn.type = "button";
    btn.addEventListener("click", () => openConnect(k.id));
    row.append(logo, text, btn);
    avail.appendChild(row);
  }
  const setPop = (open) => { pop.hidden = !open; pill.setAttribute("aria-expanded", String(open)); };
  pill.addEventListener("click", (e) => { e.stopPropagation(); setPop(pop.hidden); });
  document.addEventListener("click", (e) => { if (!pop.hidden && !e.target.closest("#sources")) setPop(false); });
  $("#srcAdd").addEventListener("click", () => openConnect());
  $("#srcConnectMore").addEventListener("click", () => openConnect());
  $("#cmClose").addEventListener("click", closeConnect);
  $("#cmBack").addEventListener("click", () => connectStep(1));
  $("#connectModal").addEventListener("click", (e) => { if (e.target.id === "connectModal") closeConnect(); });
  $("#cmStep2").addEventListener("submit", (e) => e.preventDefault());   // preview: never submits anywhere
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("#connectModal").hidden) closeConnect();
    else if (!pop.hidden) setPop(false);
  });

  const kinds = $("#cmKinds");
  for (const k of SOURCE_KINDS) {
    const b = el("button", "cm-kind");
    b.type = "button";
    b.dataset.kind = k.id;
    const icon = el("span", "ck-icon");
    icon.innerHTML = svgIcon(KIND_ICONS[k.id]);
    const text = el("span", "ck-text");
    text.append(el("span", "ck-name", k.name), el("span", "ck-desc", k.desc));
    b.append(icon, text);
    b.addEventListener("click", () => selectKind(k.id));
    kinds.appendChild(b);
  }
  const plats = $("#cmPlatforms");
  for (const pl of PLATFORMS) {
    const b = el("button", "cm-platform");
    b.type = "button";
    const auth = el("span", "cp-auth");
    for (const a of pl.auth) auth.appendChild(el("span", null, a));
    b.append(platformLogo(pl, "cp-logo"), el("span", "cp-name", pl.name), el("span", "cp-sub", pl.sub), auth);
    b.addEventListener("click", () => showConnectForm(pl));
    plats.appendChild(b);
  }
}

function connectStep(n) {
  $("#cmStep1").hidden = n !== 1;
  $("#cmStep2").hidden = n !== 2;
  const kindChosen = Boolean(document.querySelector(".cm-kind.selected"));
  [...$("#cmSteps").children].forEach((li, i) => {
    const step = i + 1;
    const done = n === 2 ? step < 3 : step === 1 && kindChosen;
    li.classList.toggle("done", done);
    li.classList.toggle("active", n === 2 ? step === 3 : step === (kindChosen ? 2 : 1));
  });
}

function selectKind(id) {
  for (const b of document.querySelectorAll(".cm-kind")) b.classList.toggle("selected", b.dataset.kind === id);
  if (!$("#cmStep1").hidden) connectStep(1);
}

function openConnect(kind) {
  $("#srcPop").hidden = true;
  $("#srcPill").setAttribute("aria-expanded", "false");
  $("#connectModal").hidden = false;
  selectKind(kind || "");
  connectStep(1);
  $("#cmClose").focus();
}

function closeConnect() {
  $("#connectModal").hidden = true;
  $("#srcAdd").focus();
}

function showConnectForm(pl) {
  const kind = SOURCE_KINDS.find((k) => document.querySelector(`.cm-kind.selected[data-kind="${k.id}"]`));
  $("#cmFormLogo").replaceChildren(platformLogo(pl, "cm-form-logo-inner"));
  $("#cmFormTitle").textContent = pl.name;
  $("#cmFormSub").textContent = kind ? `${kind.name} · ${pl.sub}` : pl.sub;
  const box = $("#cmFields");
  box.replaceChildren();
  const field = (label, control, wide) => {
    const f = el("label", "cm-field" + (wide ? " wide" : ""));
    f.append(el("span", null, label), control);
    box.appendChild(f);
  };
  for (const [label, hint] of pl.fields) {
    const input = el("input");
    input.type = label === "File" ? "file" : "text";
    if (hint) input.placeholder = hint;
    field(label, input, label === "File");
  }
  const select = (opts, chosen) => {
    const sel = el("select");
    for (const o of opts) { const op = el("option", null, o); op.selected = o === chosen; sel.appendChild(op); }
    return sel;
  };
  if (pl.id !== "upload") field("Authentication", select(pl.auth.filter((a) => !/link/i.test(a))));
  field("Join key to IQVIA claims", select(JOIN_KEYS, kind?.join || JOIN_KEYS[0]));
  connectStep(2);
  box.querySelector("input")?.focus();
}

// ---------------------------------------------------------------- follow-up suggestions
const FOLLOWUPS = {
  compare_segments: ["Why are they different?", "Which HCPs should we target to close the gap?",
    "Compare {p} with its top competitor"],
  hcp_product_share_deciles: ["Which HCP deciles have the biggest {p} share opportunity?",
    "Is the market more concentrated across HCPs after first {p}?", "Compare {p} with its top competitors"],
  hcp_market_concentration: ["Decile the HCPs based on {p} market share", "What is the source of business for {p}?",
    "Compare {p} with its top competitors"],
  payer_mix: ["What is the source of business for {p}?", "How persistent are {p} patients?",
    "Compare {p} with its top competitors"],
  persistency: ["What share of {p} patients progress to line 3+?", "What is the {p} patient share in each line?",
    "Compare {p} persistency with its top competitors"],
  product_share_by_line: ["What are the most common treatments in each line?", "How persistent are {p} patients?",
    "Compare {p} with its top competitors"],
  line_progression: ["What is the {p} patient share in each line?", "What are the most common treatments in each line?",
    "How persistent are {p} patients?"],
  compare_products: ["Decile the HCPs based on {p} market share", "What is the payer mix for {p}?",
    "How persistent are {p} patients?"],
};

function setFollowups(tools) {
  const box = $("#followups");
  const p = state.product;
  let qs = [];
  for (const t of tools) qs.push(...(FOLLOWUPS[t] || []));
  if (tools.length && !qs.length) {
    for (const g of state.meta?.suggestions || []) qs.push(g.questions[0]);
  }
  qs = [...new Set(qs.map((q) => q.replaceAll("{p}", p)))].slice(0, 3);
  box.replaceChildren(...qs.map((q) => {
    const b = el("button", "fu", q);
    b.type = "button";
    b.addEventListener("click", () => submit(q));
    return b;
  }));
  box.hidden = !qs.length || !p;
}

function renderData(data) {
  const wrap = el("div");
  const frames = Array.isArray(data) ? { data } : data || {};
  const keys = Object.keys(frames);
  const tableBox = el("div", "table-wrap");
  if (keys.length > 1) {
    const pick = el("div", "table-pick");
    keys.forEach((k, i) => {
      const b = el("button", i === 0 ? "active" : "", k.replaceAll("_", " "));
      b.type = "button";
      b.addEventListener("click", () => {
        pick.querySelectorAll("button").forEach((x) => x.classList.remove("active"));
        b.classList.add("active");
        tableBox.replaceChildren(renderTable(frames[k]));
      });
      pick.appendChild(b);
    });
    wrap.appendChild(pick);
  }
  tableBox.appendChild(renderTable(frames[keys[0]] || []));
  wrap.appendChild(tableBox);
  return wrap;
}

function renderTable(rows) {
  const t = el("table", "data");
  if (!rows.length) { t.appendChild(el("caption", null, "No rows")); return t; }
  const cols = Object.keys(rows[0]).filter((c) => !c.startsWith("_"));
  const thead = el("thead"); const tr = el("tr");
  for (const c of cols) tr.appendChild(el("th", null, c.replaceAll("_", " ")));
  thead.appendChild(tr);
  const tb = el("tbody");
  for (const r of rows) {
    const row = el("tr");
    for (const c of cols) {
      const v = r[c];
      const isNum = typeof v === "number";
      const shown = v == null ? "" : isNum
        ? (/(pct|share)/.test(c) && Math.abs(v) <= 1 ? (v * 100).toFixed(1) + "%" : v.toLocaleString(undefined, { maximumFractionDigits: 2 }))
        : String(v);
      row.appendChild(el("td", isNum ? "num" : "", shown));
    }
    tb.appendChild(row);
  }
  t.append(thead, tb);
  return t;
}

function downloadCSV(res) {
  const frames = Array.isArray(res.data) ? { data: res.data } : res.data;
  for (const [name, rows] of Object.entries(frames)) {
    if (!rows.length) continue;
    const cols = Object.keys(rows[0]);
    const esc = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replaceAll('"', '""')}"` : String(v));
    const csv = [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
    const a = el("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = `${res.tool}_${name}.csv`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
}

function embed(node, spec, height) {
  const s = structuredClone(spec);
  if (height) s.height = height;
  // chart actions live in the card header (Download · Expand), not vega-embed's "…" menu
  return vegaEmbed(node, s, { actions: false, renderer: "svg" })
    .then((r) => r.view)
    .catch((e) => { node.textContent = "Chart failed to render: " + e.message; });
}

async function downloadPNG(res, views) {
  for (const [i, v] of views.entries()) {
    const a = el("a");
    a.href = await v.toImageURL("png", 2);
    a.download = `${res.tool}${views.length > 1 ? "_" + (i + 1) : ""}.png`;
    a.click();
  }
}

init();
