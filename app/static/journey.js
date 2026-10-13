/* CIQ - Patient Journey view (loaded after app.js; uses its helpers). */
"use strict";

const JN = { data: null, tab: "overview", token: 0, trans: ["diagnosis", "treatment"], filter: "all", segs: null,
  cmp: { a: "treated:yes", b: "treated:no" }, scope: "market", pathways: {} };
try { JN.scope = localStorage.getItem("ciq.journeyScope") === "brand" ? "brand" : "market"; } catch (e) { /* storage blocked */ }
const jscope = () => `scope=${JN.scope}&product=${jq(state.product)}`;
const JTABS = [["overview", "Overview"], ["map", "Journey map"], ["transitions", "Transitions"], ["patterns", "Patterns"],
  ["compare", "Compare"], ["explore", "Explore"]];
const JSTATUS = { "On pathway": "ok", "Delayed": "delay", "Deviated": "dev", "Stalled": "stall", "In progress": "prog" };
const JPRESETS = [
  ["Treated vs untreated", "treated:yes", "treated:no"],
  ["On pathway vs deviated", "status:On pathway", "status:Deviated"],
  ["High vs low similarity", "sim:High", "sim:Low"],
  ["Medicare vs commercial", "payer:Medicare", "payer:Commercial"],
];

async function jget(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
}
const jq = (s) => encodeURIComponent(s);
const jdays = (v) => (v == null ? "—" : `${Math.round(v)} day${Math.round(v) === 1 ? "" : "s"}`);
const jday = (v) => (v == null ? "" : v === 0 ? "day 0" : `day ${v > 0 ? "+" : "−"}${Math.abs(v)}`);
function jbtn(cls, text, onClick) {
  const b = el("button", cls, text);
  b.type = "button";
  if (onClick) b.addEventListener("click", onClick);
  return b;
}

// ---------------------------------------------------------------- view switching
function showView(view) {
  state.view = view;
  const journey = view === "journey";
  const cohort = view === "cohort";
  const playground = view === "playground";
  $("#journey").hidden = !journey;
  $("#cohort").hidden = !cohort;
  const pg = $("#playground");
  if (pg) pg.hidden = !playground;
  const hideChat = journey || cohort || playground;
  $("#thread").hidden = hideChat;
  $(".composer-wrap").hidden = hideChat;
  $("#btnJourney").classList.toggle("active", journey);
  $("#btnJourney").setAttribute("aria-current", journey ? "page" : "false");
  $("#btnCohort").classList.toggle("active", cohort);
  $("#btnCohort").setAttribute("aria-current", cohort ? "page" : "false");
  const btnPg = $("#btnPlayground");
  if (btnPg) {
    btnPg.classList.toggle("active", playground);
    btnPg.setAttribute("aria-current", playground ? "page" : "false");
  }
  if (journey) {
    closeAsk();
    $("#convTitle").textContent = "Patient Journey";
    history.replaceState(null, "", "/?view=journey");
    renderJourney();
    closeNavDrawer();
  }
  if (cohort) {
    closeAsk();
    $("#convTitle").textContent = "Patient Cohort Builder";
    history.replaceState(null, "", "/?view=cohort");
    renderCohortBuilder();
    closeNavDrawer();
  }
  if (playground) {
    closeAsk();
    $("#convTitle").textContent = "Playground Tab";
    history.replaceState(null, "", "/?view=playground");
    renderPlayground();
    closeNavDrawer();
  }
}

// Hand a question to the agent: a fresh analysis in the chat view
function askJourney(text) {
  newConversation();
  submit(text);
}

// ---------------------------------------------------------------- render
async function renderJourney() {
  const root = $("#journey");
  const token = ++JN.token;
  root.classList.add("loading");
  if (!root.firstChild) root.appendChild(ccSkeleton());
  let d;
  try {
    d = await jget(`/api/journey?${jscope()}`);
  } catch (e) {
    root.replaceChildren(el("div", "error-box", "Could not load patient journeys: " + (e.message || e)));
    return;
  }
  if (token !== JN.token) return;
  JN.data = d;
  JN.segs = null;
  root.classList.remove("loading");
  const body = el("div", "jn-body");
  root.replaceChildren(jHeader(d), jTabs(body), body);
  jPaint(body);
}

function jHeader(d) {
  const h = el("header", "jn-head");
  const text = el("div", "jn-head-text");
  const brand = d.context.scope === "brand";
  text.append(el("span", "cc-kicker", `Patient Journey · ${d.context.indication}`),
    el("h1", "jn-title", brand ? `How do ${d.context.product} patients move through the pathway?`
      : "Where are patients falling out of the expected pathway?"),
    el("p", "jn-sub", `${d.context.cohort} · ${brand ? d.context.scope_label + " · " : ""}${fmtInt(d.context.patients)} patients`));
  const seg = el("div", "jn-scope");
  seg.setAttribute("role", "radiogroup");
  seg.setAttribute("aria-label", "Patients shown");
  for (const [id, label, sub] of [["market", "Market", "all newly diagnosed"], ["brand", `${d.context.product} patients`, "first line includes the brand"]]) {
    const b = jbtn("jn-scope-btn" + (d.context.scope === id ? " on" : ""), "", () => {
      if (JN.scope === id) return;
      JN.scope = id;
      try { localStorage.setItem("ciq.journeyScope", id); } catch (e) { /* storage blocked */ }
      if (JN.tab === "explore") JN.filter = "all";
      renderJourney();
    });
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(d.context.scope === id));
    b.append(el("b", null, label), el("small", null, sub));
    seg.appendChild(b);
  }
  text.appendChild(seg);
  if (d.context.small) {
    text.appendChild(el("p", "jn-small", `Small group: only ${fmtInt(d.context.patients)} patients. Percentages can move a lot with a few patients, so read them as directional.`));
  }
  const m = methodPanel(d);
  const btn = jbtn("jn-method-btn", "View methodology");
  btn.insertAdjacentHTML("afterbegin", svgIcon(ICONS.info));
  btn.setAttribute("aria-expanded", "false");
  h.append(text, btn);
  if (m) {
    m.panel.classList.add("jn-method");
    btn.addEventListener("click", () => {
      m.panel.hidden = !m.panel.hidden;
      btn.setAttribute("aria-expanded", String(!m.panel.hidden));
      btn.classList.toggle("on", !m.panel.hidden);
    });
    h.append(m.panel);
  }
  return h;
}

function jTabs(body) {
  const nav = el("nav", "jn-tabs");
  nav.setAttribute("role", "tablist");
  for (const [id, label] of JTABS) {
    const b = jbtn("jn-tab" + (JN.tab === id ? " on" : ""), label, () => jGo(id));
    b.dataset.tab = id;
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(JN.tab === id));
    nav.appendChild(b);
  }
  return nav;
}

function jGo(tab, opts = {}) {
  JN.tab = tab;
  Object.assign(JN, opts);
  for (const b of document.querySelectorAll(".jn-tab")) {
    b.classList.toggle("on", b.dataset.tab === tab);
    b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  }
  jPaint($("#journey .jn-body"));
  $("#journey").scrollTop = 0;
}

function jPaint(body) {
  if (!body || !JN.data) return;
  const d = JN.data;
  body.replaceChildren();
  ({ overview: jOverview, map: jMap, transitions: jTransitions, patterns: jPatterns, compare: jCompare,
     explore: jExplore })[JN.tab](body, d);
}

function jPanel(title, sub) {
  const p = el("section", "jn-panel");
  const h = el("header");
  h.append(el("h2", null, title));
  if (sub) h.append(el("span", null, sub));
  p.appendChild(h);
  return p;
}

function jAsk(questions) {
  const box = el("section", "jn-ask");
  const h = el("div", "jn-ask-head");
  h.innerHTML = svgIcon("M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z");
  h.append(el("span", null, "Ask about this journey"),
    el("small", null, "CIQ answers with evidence, the point in the pathway and the cohort to act on"));
  const list = el("div", "jn-ask-list");
  for (const q of questions) list.appendChild(jbtn("jn-ask-q", q, () => askJourney(q)));
  box.append(h, list);
  return box;
}

// ---------------------------------------------------------------- overview
function jOverview(body, d) {
  const st = Object.fromEntries(d.status.map((s) => [s.status, s]));
  const judged = d.status.filter((s) => s.status !== "In progress").reduce((a, s) => a + s.patients, 0);
  const hero = el("section", "jn-hero");
  const lead = el("div", "jn-hero-lead");
  const stalled = st["Stalled"];
  const bv = d.brand;
  if (d.context.scope === "brand") {
    lead.append(el("span", "jn-hero-n brand", jdays(bv.median_dx_to_brand)),
      el("p", null, `median from diagnosis to the first ${bv.product} claim; ${fmtPct(bv.within30)} start within 30 days. ` +
        `${fmtInt(bv.brand_patients)} newly diagnosed patients have ${bv.product} in their first line: ` +
        `${fmtPct(bv.capture)} of those who started systemic therapy.`));
  } else {
    lead.append(el("span", "jn-hero-n", fmtPct(stalled.patients / Math.max(judged, 1))),
      el("p", null, `of newly diagnosed patients with ${d.rules.stall_days}+ days of follow-up have no observed treatment ` +
        `start (${fmtInt(stalled.patients)} of ${fmtInt(judged)}). Another ${fmtInt(st["Delayed"].patients)} started ` +
        `treatment more than ${d.rules.delay_days} days after diagnosis.`));
  }
  const bar = el("div", "jn-statusbar");
  const tiles = el("div", "jn-status");
  for (const s of d.status) {
    const seg = el("span", `jn-sb ${JSTATUS[s.status]}`);
    seg.style.flexGrow = s.patients;
    seg.title = `${s.status}: ${fmtInt(s.patients)} (${fmtPct(s.pct)})`;
    bar.appendChild(seg);
    const t = jbtn(`jn-st ${JSTATUS[s.status]}`, "", () => jGo("explore", { filter: `status:${s.status}` }));
    t.title = `${s.definition} Click to see these patients.`;
    t.append(el("span", "jn-st-lab", s.status), el("span", "jn-st-val", fmtPct(s.pct)),
      el("span", "jn-st-n", `${fmtInt(s.patients)} patients`), el("span", "jn-st-def", s.definition));
    tiles.appendChild(t);
  }
  hero.append(lead, bar, tiles);
  body.appendChild(hero);

  const grid = el("div", "jn-grid");
  const fun = jPanel("Patient journey overview", "Patients reaching each stage of the reference pathway");
  fun.appendChild(jFunnel(d));
  const side = el("div", "jn-side");
  const k = d.kpis;
  const kp = jPanel("Journey health");
  const tiles2 = el("div", "cc-tiles");
  const tile = (label, val, note, lead) => {
    const t = el("div", "cc-tile" + (lead ? " lead" : ""));
    t.append(el("span", "cc-tile-val", val), el("span", "cc-tile-lab", label));
    if (note) t.append(el("span", "cc-tile-note", note));
    tiles2.appendChild(t);
  };
  tile("Median diagnosis → treatment", jdays(k.median_dx_to_tx), `${fmtInt(k.treated)} treated patients`, true);
  tile(`Treated within ${d.rules.stall_days} days`, fmtPct(k.treated_within_60), "patients with enough follow-up");
  tile("Biomarker test before treatment", fmtPct(k.biomarker_before_tx), "of treated patients");
  const high = d.similarity.find((s) => s.band === "High");
  tile("High similarity to reference", fmtPct(high?.pct), "6-7 of 7 stages in order");
  kp.appendChild(tiles2);
  const drop = d.biggest_drop;
  const dp = el("section", "jn-panel jn-drop");
  dp.append(el("span", "cc-kicker", "Biggest drop-off"), el("h3", null, drop.label),
    el("p", null, `${fmtInt(drop.not_reached)} of ${fmtInt(drop.from)} patients (${fmtPct(drop.not_reached / drop.from)}) ` +
      `are not observed moving on after ${d.rules.stall_days}+ days.`),
    jbtn("cc-go", "Analyse this transition", () => jGo("transitions", { trans: [drop.a, drop.b] })));
  side.append(dp, kp);
  grid.append(fun, side);
  body.appendChild(grid);
  body.appendChild(jBrand(d.brand));
  body.appendChild(jAsk(d.context.scope === "brand" ? [
    `How do ${d.context.product} patients move through the pathway compared with the market?`,
    `Are ${d.context.product} patients more likely to have a biomarker test before treatment?`,
    `Which journey patterns lead to ${d.context.product} most often?`,
    `Which diagnosing HCPs send the most patients to ${d.context.product}?`,
  ] : [
    "Where are newly diagnosed patients falling out of the expected pathway?",
    "Why do patients stall between diagnosis and treatment?",
    `How does the journey of ${d.context.product} first-line patients compare with all treated patients?`,
    "Which HCPs have the most diagnosed patients who never start treatment?",
  ]));
}

function jFunnel(d) {
  const wrap = el("div", "jn-funnel");
  const trans = Object.fromEntries(d.transitions.filter((t) => !t.milestone).map((t) => [t.a, t]));
  d.stages.forEach((s, i) => {
    const row = el("div", "jn-f-row" + (s.key === "diagnosis" ? " anchor" : ""));
    row.title = s.definition;
    const bar = el("div", "jn-f-bar");
    const fill = el("span", "jn-f-fill");
    fill.style.width = `${Math.max(s.pct * 100, 1.5)}%`;
    bar.appendChild(fill);
    row.append(el("span", "jn-f-idx", String(i + 1)), el("span", "jn-f-lab", s.label), bar,
      el("span", "jn-f-val", fmtInt(s.patients)), el("span", "jn-f-pct", fmtPct(s.pct)));
    wrap.appendChild(row);
    const t = trans[s.key];
    if (t) {
      const b = jbtn("jn-f-drop", "", () => jGo("transitions", { trans: [t.a, t.b] }));
      const moved = t.from ? t.reached / t.from : 0;
      b.append(el("span", "jn-f-arrow", "↓"), el("span", null, `${fmtPct(moved)} move on`),
        el("span", "jn-f-lost", t.not_reached ? `${fmtInt(t.not_reached)} not observed` : "none lost"),
        el("span", "jn-f-med", t.median_days == null ? "" : `median ${jdays(t.median_days)}`));
      b.title = `${t.label}: of ${fmtInt(t.from)} patients, ${fmtInt(t.reached)} reached the next stage, ` +
        `${fmtInt(t.not_reached)} did not after ${d.rules.stall_days}+ days, ${fmtInt(t.too_early)} too recent to tell. Click to analyse.`;
      if (t.not_reached / Math.max(t.from, 1) > 0.3) b.classList.add("hot");
      wrap.appendChild(b);
    }
  });
  wrap.appendChild(el("p", "cc-panel-note", "Stages are observed in claims; a stage billed elsewhere (e.g. an outside lab) looks missing. Diagnosis is the anchor every patient has."));
  return wrap;
}

// the brand within the journey: time to start and share of first-line starts by journey characteristics
function jBrand(bv) {
  const p = jPanel(`${bv.product} in the journey`, "Among newly diagnosed patients who started systemic therapy");
  if (bv.small) p.appendChild(el("p", "jn-small", `Only ${fmtInt(bv.brand_patients)} ${bv.product} patients: treat the splits below as directional.`));
  const tiles = el("div", "jn-ttiles");
  const tile = (lab, val, note, lead) => {
    const x = el("div", "cc-tile" + (lead ? " lead" : ""));
    x.append(el("span", "cc-tile-val", val), el("span", "cc-tile-lab", lab));
    if (note) x.append(el("span", "cc-tile-note", note));
    tiles.appendChild(x);
  };
  tile("Share of first-line starts", fmtPct(bv.capture), `${fmtInt(bv.brand_patients)} of ${fmtInt(bv.systemic_treated)} patients`, true);
  tile(`Diagnosis → first ${bv.product}`, jdays(bv.median_dx_to_brand), "median");
  tile("Started within 30 days", fmtPct(bv.within30), `of ${bv.product} patients`);
  const max = Math.max(...bv.distribution.map((x) => x.patients), 1);
  const spark = el("div", "jn-spark");
  for (const x of bv.distribution) {
    const b = el("span"); b.style.height = `${(x.patients / max) * 100}%`; b.title = `${x.bucket}: ${fmtInt(x.patients)} patients`;
    spark.appendChild(b);
  }
  const sp = el("div", "cc-tile");
  sp.append(spark, el("span", "cc-tile-lab", "Days to brand start"), el("span", "cc-tile-note", "0-7 · 8-14 · 15-30 · 31-60 · 60+"));
  tiles.appendChild(sp);
  p.appendChild(tiles);
  const grid = el("div", "jn-bshare");
  const dims = [...new Set(bv.rows.map((r) => r.dimension))];
  const top = Math.max(...bv.rows.map((r) => r.share), bv.capture || 0) * 1.15 || 1;
  for (const dim of dims) {
    const col = el("div", "jn-bs-col");
    col.appendChild(el("h3", "jn-mini", dim));
    for (const r of bv.rows.filter((x) => x.dimension === dim)) {
      const row = el("div", "jn-bs-row");
      row.title = `${r.group}: ${fmtInt(r.brand_patients)} of ${fmtInt(r.patients)} patients started ${bv.product} in first line`;
      const bar = el("span", "jn-bs-bar");
      const f = el("i"); f.style.width = `${(r.share / top) * 100}%`;
      if (bv.capture != null) {
        const avg = el("b"); avg.style.left = `${(bv.capture / top) * 100}%`; bar.appendChild(avg);
        f.classList.toggle("above", r.share > bv.capture);
      }
      bar.appendChild(f);
      row.append(el("span", "jn-bs-lab", r.group), bar, el("span", "jn-bs-val", fmtPct(r.share)), el("span", "jn-bs-n", `n=${fmtInt(r.patients)}`));
      col.appendChild(row);
    }
    grid.appendChild(col);
  }
  p.appendChild(grid);
  const note = el("p", "cc-panel-note", `Bars: ${bv.product} share of first-line starts in each group; the marker is the overall ${fmtPct(bv.capture)}. Groups are associations, not causes.`);
  p.appendChild(note);
  const ul = el("ul", "dg-obs");
  for (const i of bv.insights) ul.appendChild(el("li", null, i));
  p.appendChild(ul);
  return p;
}

// ---------------------------------------------------------------- journey map: reference vs observed
function jMap(body, d) {
  const def = jPanel("Pathway definition", "The claims codes behind each stage, and how many patients move on");
  const slot = el("div");
  slot.appendChild(ccSkeleton());
  const edit = el("div", "jn-actions pw-edit");
  edit.append(el("span", "jn-note", "Disagree with a definition? Ask CIQ to change it, e.g. “remove 88305 from tissue diagnosis” or “add a brain MRI stage”. The numbers are recalculated and marked as a custom pathway."),
    jbtn("cc-go", "Edit this pathway with CIQ", () => { newConversation(); submit("Show me the patient pathway diagram with the codes that define each stage."); }));
  def.append(slot, edit);
  body.appendChild(def);
  const pkey = jscope();
  (JN.pathways[pkey] ? Promise.resolve(JN.pathways[pkey]) : jget(`/api/journey/pathway?${pkey}`)).then((v) => {
    JN.pathways[pkey] = v;
    slot.replaceChildren(renderPathway(v));
  }).catch((e) => slot.replaceChildren(el("div", "error-box", e.message || String(e))));
  const ref = jPanel("Reference pathway", "Share of patients observed at each stage and typical timing vs diagnosis");
  const chain = el("ol", "jn-chain");
  for (const s of d.stages) {
    const li = el("li", "jn-node" + (s.key === "diagnosis" ? " anchor" : ""));
    li.style.setProperty("--p", s.pct);
    li.title = s.definition;
    li.append(el("span", "jn-node-lab", s.short), el("span", "jn-node-pct", fmtPct(s.pct)),
      el("span", "jn-node-day", s.median_days_from_dx == null ? "" : `median ${jday(Math.round(s.median_days_from_dx))}`));
    chain.appendChild(li);
  }
  ref.appendChild(chain);
  body.appendChild(ref);

  const mat = jPanel("Reference vs observed journeys", "Each row is a journey pattern; numbers give the order in which stages were observed");
  mat.appendChild(el("p", "jn-explain", "Every patient's journey is recorded as the date each stage was first observed. " +
    "Comparing that order with the reference shows where journeys skip a stage or happen out of order."));
  const wrapT = el("div", "table-wrap");
  const t = el("table", "jn-matrix");
  const head = el("tr");
  head.append(el("th", null, "Journey"));
  for (const s of d.stages) head.append(el("th", null, s.short));
  head.append(el("th", "num", "Patients"), el("th", "num", "Similarity"));
  const thead = el("thead"); thead.appendChild(head);
  const tb = el("tbody");
  const refRow = el("tr", "ref");
  refRow.append(el("td", null, "Reference pathway"));
  d.stages.forEach((_, i) => refRow.append(el("td", "c on", String(i + 1))));
  refRow.append(el("td", "num", "—"), el("td", "num", "100%"));
  tb.appendChild(refRow);
  for (const p of d.sequences) {
    const tr = el("tr");
    tr.append(el("td", null, p.name));
    for (const s of d.stages) {
      const i = p.sequence.indexOf(s.key);
      const ok = p.in_order_stages.includes(s.key);
      const c = el("td", "c" + (i < 0 ? " off" : ok ? " on" : " swap"), i < 0 ? "" : String(i + 1));
      c.title = i < 0 ? `${s.label}: not observed` : ok ? `${s.label}: observed, in reference order` : `${s.label}: observed out of order`;
      tr.appendChild(c);
    }
    tr.append(el("td", "num", `${fmtInt(p.patients)} · ${fmtPct(p.pct)}`), el("td", "num", fmtPct(p.similarity)));
    tb.appendChild(tr);
  }
  t.append(thead, tb);
  wrapT.appendChild(t);
  const legend = el("div", "jn-legend");
  legend.innerHTML = `<span><i class="on"></i>Observed in reference order</span><span><i class="swap"></i>Observed out of order</span><span><i class="off"></i>Not observed</span>`;
  mat.append(wrapT, legend);
  body.appendChild(mat);

  const sim = jPanel("Pathway similarity", "How closely observed journeys follow the reference");
  const bars = el("div", "jn-sim");
  const band = { High: "6-7 of 7 stages in reference order", Moderate: "4-5 of 7", Low: "3 or fewer" };
  for (const s of d.similarity) {
    const b = jbtn(`jn-sim-row ${s.band.toLowerCase()}`, "", () => jGo("explore", { filter: `sim:${s.band}` }));
    const bar = el("span", "jn-sim-bar");
    const f = el("span"); f.style.width = `${s.pct * 100}%`; bar.appendChild(f);
    b.append(el("span", "jn-sim-lab", s.band), el("span", "jn-sim-def", band[s.band]), bar,
      el("span", "jn-sim-val", `${fmtPct(s.pct)} · ${fmtInt(s.patients)}`));
    bars.appendChild(b);
  }
  sim.appendChild(bars);
  const note = el("p", "cc-panel-note");
  note.append("Similarity = reference stages observed in reference order ÷ 7. ");
  const how = jbtn("cc-tile-link", "View methodology", () => $(".jn-method-btn")?.click());
  note.appendChild(how);
  sim.appendChild(note);
  body.appendChild(sim);
}

// ---------------------------------------------------------------- transitions
function jTransitions(body, d) {
  const grid = el("div", "jn-tgrid");
  const list = el("div", "jn-tlist");
  list.appendChild(el("h2", "jn-mini", "Transitions"));
  const detail = el("div", "jn-tdetail");
  for (const t of d.transitions) {
    const on = JN.trans[0] === t.a && JN.trans[1] === t.b;
    const b = jbtn("jn-titem" + (on ? " on" : "") + (t.milestone ? " milestone" : ""), "", () => {
      JN.trans = [t.a, t.b];
      jPaint(body);
    });
    const rate = t.from ? t.reached / t.from : 0;
    const bar = el("span", "jn-tbar"); const f = el("span"); f.style.width = `${rate * 100}%`; bar.appendChild(f);
    b.append(el("span", "jn-tlab", t.milestone ? `★ ${t.label}` : t.label), bar,
      el("span", "jn-tnums", `${fmtPct(rate)} reached · ${fmtInt(t.not_reached)} not`));
    list.appendChild(b);
  }
  grid.append(list, detail);
  body.appendChild(grid);
  detail.appendChild(ccSkeleton());
  const [a, b] = JN.trans;
  jget(`/api/journey/transition?a=${a}&b=${b}&${jscope()}`).then((t) => {
    if (JN.tab !== "transitions" || JN.trans[0] !== a || JN.trans[1] !== b) return;
    detail.replaceChildren(...jTransitionDetail(t, d));
  }).catch((e) => detail.replaceChildren(el("div", "error-box", e.message || String(e))));
}

function jTransitionDetail(t, d) {
  const out = [];
  const head = el("section", "jn-panel");
  const h = el("header");
  h.append(el("h2", null, t.label), el("span", null, `${fmtInt(t.from)} patients reached ${t.label.split(" → ")[0].toLowerCase()}`));
  head.appendChild(h);
  const tiles = el("div", "jn-ttiles");
  const tile = (lab, val, note, cls = "") => {
    const x = el("div", `cc-tile ${cls}`);
    x.append(el("span", "cc-tile-val", val), el("span", "cc-tile-lab", lab));
    if (note) x.append(el("span", "cc-tile-note", note));
    tiles.appendChild(x);
  };
  tile("Reached", fmtInt(t.reached), fmtPct(t.reached / Math.max(t.from, 1)));
  tile("Not reached", fmtInt(t.not_reached), `after ${d.rules.stall_days}+ days of follow-up`, "lead");
  tile("Too early to tell", fmtInt(t.too_early), "recent patients");
  tile("Median time", jdays(t.median_days), "for patients who reached it");
  head.appendChild(tiles);
  const ins = el("ul", "dg-obs");
  for (const i of t.insights) ins.appendChild(el("li", null, i));
  head.appendChild(ins);
  out.push(head);

  const two = el("div", "jn-grid even");
  const time = jPanel("Time to transition", "Patients who reached the next stage");
  const bars = el("div", "jn-buckets");
  const max = Math.max(...t.distribution.map((x) => x.patients), 1);
  for (const x of t.distribution) {
    const col = el("div", "jn-bk");
    const bar = el("span", "jn-bk-bar"); bar.style.height = `${(x.patients / max) * 100}%`;
    const slow = /31|60\+/.test(x.bucket);
    if (slow) col.classList.add("slow");
    col.append(el("span", "jn-bk-val", fmtPct(x.pct)), el("span", "jn-bk-track", ""), el("span", "jn-bk-lab", x.bucket));
    col.querySelector(".jn-bk-track").appendChild(bar);
    col.title = `${x.bucket}: ${fmtInt(x.patients)} patients`;
    bars.appendChild(col);
  }
  time.appendChild(bars);
  const why = jPanel("What explains the difference?", "Reached vs not reached");
  const tbl = el("table", "data jn-cmp");
  tbl.innerHTML = "<thead><tr><th></th><th class='num'>Reached</th><th class='num'>Not reached</th><th class='num'>Difference</th></tr></thead>";
  const tb = el("tbody");
  for (const r of t.comparison) {
    const tr = el("tr");
    tr.append(el("td", null, r.metric), el("td", "num", r.a_fmt), el("td", "num", r.b_fmt), el("td", "num", r.diff_fmt));
    tb.appendChild(tr);
  }
  tbl.appendChild(tb);
  const tw = el("div", "table-wrap"); tw.appendChild(tbl);
  why.append(tw, el("p", "cc-panel-note", "Observed associations, not causes: patients differ in many ways the claims do not record (stage, performance status, care outside this data)."));
  two.append(time, why);
  out.push(two);

  const act = jPanel("Act on it");
  const row = el("div", "jn-actions");
  const stageA = t.label.split(" → ")[0];
  row.appendChild(jbtn("cc-go", `View associated HCPs (${fmtInt(t.hcps.length)})`, () => openHcpList({
    label: `HCPs on the ${stageA.toLowerCase()} claim of patients: ${t.label}`,
    hcp_ids: t.hcp_ids,
    rows: t.hcps.map((h) => ({ hcp_id: h.hcp_id, patients: h.patients, not_reached: h.not_reached,
      pct_not_reached: h.pct_not_reached, median_days: h.median_days, market_volume: h.market_volume,
      [`${state.product}_share`]: h.brand_share })),
  }, state.product)));
  row.appendChild(jbtn("cc-go ghost", "Ask CIQ why", () =>
    askJourney(`Why do patients who reach ${stageA.toLowerCase()} not move on to ${t.label.split(" → ")[1].toLowerCase()}?`)));
  act.appendChild(row);
  act.appendChild(el("p", "jn-note", "HCP = the provider on the patient's first claim for the earlier stage (e.g. the diagnosing HCP); market volume and brand share are that HCP's lung-cancer systemic therapy claims. Only HCPs with 3+ patients are listed."));
  act.appendChild(jCohortForm(t.not_reached_ids, `Not reached: ${t.label}`,
    `Newly diagnosed patients who reached ${stageA.toLowerCase()} but not the next stage after ${d.rules.stall_days}+ days`));
  out.push(act);
  return out;
}

// ---------------------------------------------------------------- patterns
function jSeq(q, d) {
  const seq = el("div", "jn-seq");
  if (!q.sequence.length) seq.appendChild(el("span", "jn-seq-chip", "no stages"));
  q.sequence.forEach((k, i) => {
    if (i) seq.appendChild(el("span", "jn-seq-arrow", "→"));
    const s = d.stages.find((x) => x.key === k);
    seq.appendChild(el("span", "jn-seq-chip" + (q.in_order_stages.includes(k) ? "" : " swap") + (k === "diagnosis" ? " anchor" : ""), s.short));
  });
  return seq;
}

function jPatterns(body, d) {
  body.appendChild(el("p", "jn-explain", "Every newly diagnosed patient falls into one journey pattern. Patterns are data-derived archetypes built from the observed stages and their timing, not clinical categories."));
  const grid = el("div", "jn-patterns");
  for (const p of d.patterns) {
    const c = el("article", "jn-pat");
    const h = el("header");
    h.append(el("h3", null, p.name), el("span", "jn-pat-n", `${fmtInt(p.patients)} · ${fmtPct(p.pct)}`));
    const facts = el("p", "jn-pat-facts", p.description);
    const kv = el("div", "jn-pat-kv");
    const kvi = (lab, val) => { const x = el("span"); x.append(el("b", null, val), ` ${lab}`); kv.appendChild(x); };
    if (p.median_dx_to_tx != null) kvi("median diagnosis → treatment", jdays(p.median_dx_to_tx));
    if (p.median_workup_days != null) kvi("median work-up → diagnosis", jdays(p.median_workup_days));
    kvi("with a biomarker test", fmtPct(p.biomarker));
    const mix = el("div", "jn-statusbar small");
    for (const [s, n] of Object.entries(p.status_mix)) {
      const seg = el("span", `jn-sb ${JSTATUS[s]}`); seg.style.flexGrow = n; seg.title = `${s}: ${fmtInt(n)}`;
      mix.appendChild(seg);
    }
    const seqs = el("div", "jn-pat-seqs");
    seqs.appendChild(el("span", "jn-mini", "Most common sequences"));
    for (const q of p.sequences) {
      const r = el("div", "jn-pat-seq");
      r.append(jSeq(q, d), el("span", "jn-pat-seq-n", fmtPct(q.pct)));
      seqs.appendChild(r);
    }
    const acts = el("div", "jn-actions");
    acts.append(jbtn("cc-go ghost", "View patients", () => jGo("explore", { filter: `archetype:${p.name}` })),
      jCohortForm(p.patient_ids, p.name, `Journey pattern: ${p.name} (${p.description})`, true));
    c.append(h, facts, kv, mix, seqs, acts);
    grid.appendChild(c);
  }
  body.appendChild(grid);
  body.appendChild(jAsk(["Which journey patterns are most common, and which ones end without treatment?",
    "Do patients without an observed biomarker test start treatment faster or slower?",
    "Which HCPs see the most patients with a diagnostic delay?"]));
}

// ---------------------------------------------------------------- compare two journey cohorts
async function jSegments() {
  if (!JN.segs) JN.segs = await jget(`/api/journey/segments?product=${jq(state.product)}`);
  return JN.segs;
}

function jCompare(body, d) {
  const panel = jPanel("Journey comparison", "Same newly diagnosed cohort, same pathway definitions; difference = B minus A");
  if (d.context.scope === "brand") panel.appendChild(el("p", "jn-note", `Comparisons use all newly diagnosed patients; pick “First line includes ${d.context.product}” as a side to compare the brand's patients.`));
  const presets = el("div", "jn-presets");
  presets.appendChild(el("span", "cc-field-label", "Quick picks"));
  const pick = el("div", "jn-pick");
  const out = el("div", "jn-cmp-out");
  panel.append(presets, pick, out);
  body.appendChild(panel);
  jSegments().then((segs) => {
    const brandOpts = segs.filter((s) => s.group === "brand");
    const list = [...JPRESETS];
    if (brandOpts.length > 1) list.unshift([`${brandOpts[0].id.slice(6)} vs ${brandOpts[1].id.slice(6)}`, brandOpts[0].id, brandOpts[1].id]);
    for (const [label, a, b] of list) {
      const btn = jbtn("jn-chip" + (JN.cmp.a === a && JN.cmp.b === b ? " on" : ""), label, () => { JN.cmp = { a, b }; jPaint(body); });
      presets.appendChild(btn);
    }
    const sel = (side) => {
      const s = el("select", "cc-select");
      s.setAttribute("aria-label", `Cohort ${side.toUpperCase()}`);
      for (const o of segs) { const op = el("option", null, o.label); op.value = o.id; s.appendChild(op); }
      s.value = JN.cmp[side];
      s.addEventListener("change", () => { JN.cmp[side] = s.value; run(); });
      const wrap = el("label", `jn-cside ${side}`);
      wrap.append(el("span", "cc-field-label", `Cohort ${side.toUpperCase()}`), s);
      return wrap;
    };
    pick.append(sel("a"), el("span", "dg-vs", "vs"), sel("b"));
    const run = async () => {
      out.replaceChildren(ccSkeleton());
      try {
        const c = await jget(`/api/journey/compare?a=${jq(JN.cmp.a)}&b=${jq(JN.cmp.b)}&product=${jq(state.product)}`);
        out.replaceChildren(...jCompareResult(c));
      } catch (e) { out.replaceChildren(el("div", "error-box", e.message || String(e))); }
    };
    run();
  }).catch((e) => out.replaceChildren(el("div", "error-box", e.message || String(e))));
}

function jCompareResult(c) {
  const title = el("div", "dg-title");
  title.append(el("span", "dg-chip a", c.a), el("span", "dg-vs", "vs"), el("span", "dg-chip b", c.b));
  const ins = el("ul", "dg-obs");
  for (const i of c.insights) ins.appendChild(el("li", null, i));
  const wrap = el("div", "table-wrap");
  const t = el("table", "data jn-cmp");
  t.innerHTML = `<thead><tr><th></th><th class="num">A</th><th class="num">B</th><th class="num">Difference</th></tr></thead>`;
  const tb = el("tbody");
  let sec = null;
  for (const r of c.rows) {
    if (r.section !== sec) {
      sec = r.section;
      const s = el("tr", "sec"); const td = el("td", null, sec); td.colSpan = 4; s.appendChild(td); tb.appendChild(s);
    }
    const tr = el("tr");
    const diff = el("td", "num");
    const big = r.diff != null && (r.kind === "pct" ? Math.abs(r.diff) >= 5 : r.kind !== "int" && r.a && Math.abs(r.diff / r.a) >= 0.2);
    diff.appendChild(el("span", "dg-diff" + (big ? (r.diff > 0 ? " b" : " a") : ""), r.diff_fmt));
    tr.append(el("td", null, r.metric), el("td", "num", r.a_fmt), el("td", "num", r.b_fmt), diff);
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  wrap.appendChild(t);
  const why = el("div", "dg-why");
  const btn = jbtn("dg-why-btn", "Explore differences", () =>
    askJourney(`Compare the patient journeys of "${c.a}" vs "${c.b}" newly diagnosed patients: where do they differ in the pathway, and what explains it?`));
  btn.insertAdjacentHTML("afterbegin", svgIcon("M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"));
  why.append(el("span", "dg-why-hint", "CIQ digs into the drivers and answers with evidence, the point in the pathway and a cohort."), btn);
  const cohorts = el("div", "jn-actions");
  cohorts.append(jCohortForm(c.a_ids, c.a, `Journey cohort: ${c.a}`, true, null, "Save A as cohort"),
    jCohortForm(c.b_ids, c.b, `Journey cohort: ${c.b}`, true, null, "Save B as cohort"));
  return [title, ins, wrap, cohorts, why];
}

// ---------------------------------------------------------------- explore: patient-level drill-down
function jExplore(body, d) {
  const panel = jPanel("Explore patients", "De-identified patient tokens; dates shown as month and days from diagnosis");
  const bar = el("div", "jn-pick");
  const out = el("div");
  panel.append(bar, out);
  body.appendChild(panel);
  jSegments().then((segs) => {
    const s = el("select", "cc-select");
    s.setAttribute("aria-label", "Patients to show");
    const opts = [...segs];
    if (JN.filter.startsWith("archetype:")) opts.unshift({ id: JN.filter, label: `Pattern: ${JN.filter.slice(10)}` });
    if (JN.filter.startsWith("pattern:")) opts.unshift({ id: JN.filter, label: `Pattern: ${JN.filter.slice(8).split("|").join(" → ") || "no stages"}` });
    for (const o of opts) { const op = el("option", null, o.label); op.value = o.id; s.appendChild(op); }
    s.value = JN.filter;
    s.addEventListener("change", () => { JN.filter = s.value; load(); });
    const lab = el("label", "cc-field");
    lab.append(el("span", "cc-field-label", "Show"), s);
    bar.appendChild(lab);
    load();
  });
  const load = async () => {
    out.replaceChildren(ccSkeleton());
    let r;
    try { r = await jget(`/api/journey/patients?filter=${jq(JN.filter)}&${jscope()}`); } catch (e) { out.replaceChildren(el("div", "error-box", e.message)); return; }
    const rows = r.rows.map((x) => ({ patient: x.patient_id, status: x.status, diagnosed: x.diagnosis,
      days_to_treatment: x.dx_to_tx, similarity_pct: x.similarity, journey: x.sequence, age_group: x.age_group, payer: x.payer_group }));
    let shown = rows, filtering = false;
    const form = el("div");
    const table = searchableTable(rows, { total: r.count, noun: "patients", onFilter: (h, f) => { shown = h; filtering = f; } });
    table.node.querySelector(".st-wrap").classList.add("jn-ptable");
    table.node.addEventListener("click", (e) => {
      const tr = e.target.closest("tbody tr");
      if (tr) openPatient(tr.cells[0].textContent);
    });
    const hint = el("p", "jn-note", "Click a patient to see their journey. ");
    form.appendChild(jCohortForm(null, r.label, `Patient journey filter: ${r.label}`, false, () =>
      filtering ? shown.map((x) => x.patient) : r.patient_ids));
    out.replaceChildren(table.node, hint, form);
  };
}

async function openPatient(pid) {
  let p;
  try { p = await jget(`/api/journey/patient/${jq(pid)}`); } catch (e) { alert(e.message); return; }
  const back = el("div", "modal-back");
  const m = el("div", "modal jn-pm");
  m.setAttribute("role", "dialog");
  m.setAttribute("aria-modal", "true");
  const head = el("header", "cm-head");
  const ht = el("div", "cm-head-text");
  ht.append(el("span", "cm-kicker", `Patient journey · diagnosed ${p.diagnosis_month}`), el("h2", null, `Patient ${p.patient_id}`),
    el("p", null, [p.age_group && `Age ${p.age_group}`, p.payer, p.regimen && `First line: ${p.regimen}`, p.modality && `Treatment: ${p.modality}`].filter(Boolean).join(" · ")));
  const x = jbtn("cm-x", "");
  x.setAttribute("aria-label", "Close");
  x.innerHTML = svgIcon("M6 6l12 12M18 6L6 18");
  head.append(ht, x);
  const bodyN = el("div", "cm-body");
  const top = el("div", "jn-pm-top");
  const st = el("div", `jn-pm-status ${JSTATUS[p.status]}`);
  st.append(el("span", "jn-st-lab", "Journey status"), el("b", null, p.status), el("span", null, p.reason));
  const sim = el("div", "jn-pm-status");
  sim.append(el("span", "jn-st-lab", "Similarity to reference"), el("b", null, `${p.similarity_band} · ${fmtPct(p.similarity)}`),
    el("span", null, `${Math.round(p.similarity * 7)} of 7 stages in reference order`));
  top.append(st, sim);
  const check = el("ol", "jn-check");
  for (const s of p.stages) {
    const li = el("li", s.month ? (s.in_order ? "on" : "swap") : "off");
    li.append(el("span", "jn-check-mark", s.month ? (s.in_order ? "✓" : "↺") : "–"), el("span", "jn-check-lab", s.label),
      el("span", "jn-check-when", s.month ? `${jday(s.days_from_dx)} · ${s.month}` : "not observed"));
    if (s.month && !s.in_order) li.title = "Observed out of reference order";
    check.appendChild(li);
  }
  const tl = el("ol", "jn-timeline");
  for (const e of p.timeline) {
    const li = el("li", `st-${e.stage}`);
    li.append(el("span", "jn-tl-day", jday(e.days_from_dx)), el("span", "jn-tl-lab", e.label), el("span", "jn-tl-m", e.month));
    tl.appendChild(li);
  }
  const cols = el("div", "jn-pm-cols");
  const c1 = el("div"); c1.append(el("h3", "jn-mini", "Pathway checklist"), check);
  const c2 = el("div"); c2.append(el("h3", "jn-mini", "Claims timeline (from 90 days before diagnosis)"), tl);
  cols.append(c1, c2);
  bodyN.append(top, cols, el("p", "jn-note", "Observed claims only; care billed outside this data will not appear. Status is a data definition, not a clinical assessment."));
  m.append(head, bodyN);
  back.appendChild(m);
  document.body.appendChild(back);
  const close = () => { back.remove(); document.removeEventListener("keydown", onKey); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  x.addEventListener("click", close);
  back.addEventListener("click", (e) => { if (e.target === back) close(); });
  x.focus();
}

// ---------------------------------------------------------------- create cohort
// ids: patient ids (or getIds() at save time). compact: a single button that expands into the form.
function jCohortForm(ids, name, definition, compact = false, getIds = null, label = "Create cohort") {
  const box = el("div", "jn-cohort");
  const count = () => (getIds ? getIds() : ids || []).length;
  const openForm = () => {
    const form = el("form", "dg-save");
    const input = el("input");
    input.value = name.slice(0, 80);
    input.setAttribute("aria-label", "Cohort name");
    const save = el("button", "cm-btn primary", `Save ${fmtInt(count())} patients as cohort`);
    save.type = "submit";
    form.append(input, save);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const pids = getIds ? getIds() : ids;
      if (!pids?.length) return;
      save.disabled = true;
      const r = await fetch("/api/cohorts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: input.value, patient_ids: pids, definition, product: state.product }) });
      const c = await r.json();
      if (!r.ok) { save.disabled = false; alert(c.detail || r.statusText); return; }
      JN.segs = null;
      if (typeof cx !== "undefined") cx.opts = null;
      box.replaceChildren(el("span", "dg-saved", `✓ Saved “${c.name}” (${fmtInt(c.size)} patients)`),
        jbtn("cc-go ghost", "Compare journeys", () => jGo("compare", { cmp: { a: `saved:${c.id}`, b: "all" } })),
        jbtn("cc-go ghost", "Compare vs all treated patients", () => { newConversation(); compareNow("patient", `saved:${c.id}`, "all"); }));
    });
    box.replaceChildren(form);
    input.focus();
  };
  if (compact) box.appendChild(jbtn("cc-go ghost", label, openForm));
  else openForm();
  return box;
}

// ---------------------------------------------------------------- pathway diagram (code boxes)
// v: {stages:[{key,label,window,patients,pct,median_day,codes:[{code,desc,patients}],more_codes,vs_standard,new,changed}],
//     transitions:[{a,b,from,reached,not_reached,too_early,pct,median_days}], n, cohort, custom, diff, highlight, rules}
function renderPathway(v) {
  const root = el("div", "pw");
  if (v.custom) {
    const c = el("div", "pw-custom");
    c.append(el("span", "pw-badge", "Custom pathway"),
      el("span", "pw-custom-note", "Numbers differ from the standard pathway because the definition changed:"));
    const ul = el("ul");
    for (const d of v.diff || []) ul.appendChild(el("li", null, d));
    c.appendChild(ul);
    root.appendChild(c);
  }
  const hl = v.highlight || {};
  const hlStage = hl.stage, hlT = hl.transition;
  const dim = (on) => (hlStage || hlT) && !on ? " dim" : "";
  const scroll = el("div", "pw-scroll");
  const row = el("div", "pw-row");
  const trans = Object.fromEntries((v.transitions || []).map((t) => [`${t.a}>${t.b}`, t]));
  v.stages.forEach((s, i) => {
    if (i) {
      const prev = v.stages[i - 1];
      const t = trans[`${prev.key}>${s.key}`];
      const on = hlT && hlT[0] === prev.key && hlT[1] === s.key;
      const conn = el("div", "pw-conn" + (on ? " hl" : "") + dim(on));
      if (t) {
        if (t.from && t.not_reached / t.from > 0.3) conn.classList.add("hot");
        conn.append(el("span", "pw-pct", t.pct == null ? "—" : fmtPct(t.pct)), el("span", "pw-arrow", ""),
          el("span", "pw-lost", t.not_reached ? `${fmtInt(t.not_reached)} not observed` : "none lost"),
          el("span", "pw-med", t.median_days == null ? "" : `median ${jdays(t.median_days)}`));
        conn.title = `${t.label}: ${fmtInt(t.reached)} of ${fmtInt(t.from)} moved on; ${fmtInt(t.not_reached)} not observed after ` +
          `${v.rules?.stall_days ?? 60}+ days; ${fmtInt(t.too_early)} too recent to tell.`;
      }
      row.appendChild(conn);
    }
    const on = hlStage === s.key;
    const box = el("div", "pw-box" + (s.window === "anchor" ? " anchor" : "") + (on ? " hl" : "") +
      (s.new ? " new" : s.changed ? " changed" : "") + dim(on));
    const head = el("div", "pw-head");
    head.append(el("span", "pw-idx", String(i + 1)), el("span", "pw-name", s.label));
    if (s.new) head.append(el("span", "pw-tag", "New"));
    else if (s.changed) head.append(el("span", "pw-tag", "Edited"));
    const n = el("div", "pw-n");
    n.append(fmtInt(s.patients), el("small", null, fmtPct(s.pct)));
    box.append(head, n);
    if (s.vs_standard) box.appendChild(el("span", "pw-delta" + (s.vs_standard > 0 ? " up" : " down"),
      `${s.vs_standard > 0 ? "+" : "−"}${fmtInt(Math.abs(s.vs_standard))} vs standard`));
    const ul = el("ul", "pw-codes");
    const max = Math.max(...s.codes.map((c) => c.patients), 1);
    for (const c of s.codes) {
      const li = el("li");
      li.title = `${c.code}${c.desc ? ` · ${c.desc}` : ""}: ${fmtInt(c.patients)} patients`;
      const bar = el("span", "pw-cbar");
      const f = el("i"); f.style.width = `${(c.patients / max) * 100}%`; bar.appendChild(f);
      li.append(el("code", null, c.code), el("span", "pw-cdesc", c.desc || ""), bar, el("b", null, fmtInt(c.patients)));
      ul.appendChild(li);
    }
    box.appendChild(ul);
    if (s.more_codes) box.appendChild(el("span", "pw-more", `+${s.more_codes} more code${s.more_codes === 1 ? "" : "s"}`));
    const when = s.window === "anchor" ? "anchor · defines the cohort"
      : `${s.window === "workup" ? "work-up window" : "from diagnosis on"}${s.median_day == null ? "" : ` · median ${jday(Math.round(s.median_day))}`}`;
    box.appendChild(el("span", "pw-when", when));
    row.appendChild(box);
  });
  scroll.appendChild(row);
  const frame = el("div", "pw-frame");
  frame.appendChild(scroll);
  root.appendChild(frame);
  // show the highlighted stage / drop-off, and fade the edge while more stages are off-screen
  const edges = () => {
    frame.classList.toggle("more-right", scroll.scrollLeft + scroll.clientWidth < scroll.scrollWidth - 4);
    frame.classList.toggle("more-left", scroll.scrollLeft > 4);
  };
  scroll.addEventListener("scroll", edges, { passive: true });
  new ResizeObserver(edges).observe(scroll);
  requestAnimationFrame(() => {
    const target = row.querySelector(".hl");
    if (target) scroll.scrollLeft = Math.max(0, target.offsetLeft - (scroll.clientWidth - target.offsetWidth) / 2);
    edges();
  });
  const r = v.rules || {};
  root.appendChild(el("p", "pw-foot", `${fmtInt(v.n)} patients · ${v.cohort} · work-up window: ${r.workup_before} days before to ` +
    `${r.workup_after} days after diagnosis · "not observed" = no next stage after ${r.stall_days}+ days · ` +
    `${fmtInt(v.complete)} patients have every stage in order. Hover a code for its description.`));
  return root;
}

// ---------------------------------------------------------------- wiring
(function initJourney() {
  $("#btnJourney").addEventListener("click", () => showView("journey"));
  $("#productSelect").addEventListener("change", () => { if (state.view === "journey") renderJourney(); });
})();
