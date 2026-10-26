/* LoTrace - Patient Cohort Builder: nested AND/OR/SEQUENTIAL groups of ICD / CPT / NDC. */
"use strict";

const CB = {
  token: 0, searchToken: 0, tree: null, preview: null, searchSys: null, searchHits: [],
  saved: [], focus: null, suggestTimer: 0, previewTimer: 0, chat: [], asking: false,
  studyPeriod: { from: "", to: "" },
  periodMode: "",
};


const jq = (s) => encodeURIComponent(s);
async function jget(url) {
  const r = await fetch(url);
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
}

const CB_SYS = [["ICD", "ICD-10"], ["CPT", "CPT"], ["NDC", "NDC"]];
const CB_WIN = [
  ["any", "Any time after"],
  ["30", "Within 30 days"],
  ["60", "Within 60 days"],
  ["90", "Within 90 days"],
];

function cbIsSeq(join) {
  return /^seq/i.test(String(join || "").replace(/[\s-]+/g, "_"));
}
function cbSeqDefaults() {
  return { window: "90", direction: "preceding", occurrence: 1 };
}
function cbEnsureSeq(node) {
  if (!node.sequence) node.sequence = cbSeqDefaults();
  return node.sequence;
}

function cbNewCrit(system, join) {
  const n = { kind: "criterion", id: cbUid(), system: system || "ICD", codes: [], code: "", label: "" };
  if (cbIsSeq(join)) {
    n.join = "SEQ";
    n.sequence = cbSeqDefaults();
  } else if (join) n.join = join;
  return n;
}
function cbNewGroup(join) {
  const n = { kind: "group", id: cbUid(), children: [cbNewCrit("ICD")] };
  if (join) n.join = join;
  return n;
}
function cbUid() { return "n" + Math.random().toString(36).slice(2, 9); }
function cbBindRemove(btn, fn) {
  btn.type = "button";
  btn.addEventListener("pointerdown", (e) => { e.preventDefault(); e.stopPropagation(); });
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    fn();
  });
}
function cbPlaceholder(system, hasCodes) {
  if (system === "ICD") return hasCodes ? "Add another ICD-10 code" : "Type an ICD-10 code";
  if (system === "NDC") return hasCodes ? "Add another NDC" : "Type a drug name or NDC, e.g. Keytruda";
  return hasCodes ? "Add another code" : "Type a CPT/HCPCS code";
}
function cbCodes(node) {
  if (!Array.isArray(node.codes)) node.codes = [];
  if (!node.codes.length && node.code) node.codes.push({ code: node.code, label: node.label || "" });
  return node.codes;
}
function cbSyncCode(node) {
  const cs = cbCodes(node);
  node.code = cs[0] ? cs[0].code : "";
  node.label = cs[0] ? cs[0].label : "";
}
function cbHasCodes(node) {
  return node.kind === "criterion" && cbCodes(node).length > 0;
}
function cbIcdCaption(code) {
  return String(code || "").trim().replace(/\*+$/, "");
}
function cbAddCode(node, code, label) {
  const key = String(code || "").trim();
  if (!key) return false;
  const cs = cbCodes(node);
  if (cs.some((c) => c.code.toUpperCase() === key.toUpperCase())) return false;
  cs.push({ code: key, label: label || "" });
  cbSyncCode(node);
  return true;
}
async function renderCohortBuilder() {
  const root = $("#cohort");
  CB.focus = CB.tree;
  root.classList.remove("loading");
  root.replaceChildren(cbHeader(), cbLayout());
  cbPaintSearch();
  cbPaintPeriod();
  cbPaintAsk();
  cbPaintEditor();
  cbRefreshPreview();
  cbLoadSaved();
}

function cbHeader() {
  const h = el("header", "jn-head");
  const text = el("div", "jn-head-text");
  text.append(
    el("span", "cc-kicker", "Patient Cohort Builder"),
    el("h1", "jn-title", "Build Patient cohort using ICD, NDC and CPT codes for Analysis"),
    el("p", "jn-sub", "Integrating diagnosis, treatment and procedure signals to identify and refine high-confidence patient cohorts"));
  h.appendChild(text);
  return h;
}

function cbLayout() {
  const wrap = el("div", "cb-layout");
  wrap.append(el("aside", "cb-search"), el("div", "cb-main"));
  wrap.querySelector(".cb-search").id = "cbSearch";
  const main = wrap.querySelector(".cb-main");
  main.append(el("section", "cb-period"), el("section", "cb-ask"), el("div", "cb-work"));
  wrap.querySelector(".cb-period").id = "cbPeriod";
  main.querySelector(".cb-ask").id = "cbAsk";
  main.querySelector(".cb-work").id = "cbWork";
  return wrap;
}

function cbPaintSearch() {
  const side = $("#cbSearch");
  if (!side) return;
  const box = el("div", "cb-search-box");
  const input = el("input");
  input.type = "search";
  input.placeholder = "Search codes or terms";
  input.setAttribute("aria-label", "Search codes or terms");
  input.addEventListener("input", () => {
    clearTimeout(CB.suggestTimer);
    CB.suggestTimer = setTimeout(() => cbRunSearch(input.value), 220);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); cbRunSearch(input.value); }
  });
  box.appendChild(input);
  const chips = el("div", "cb-chips");
  const all = el("button", "cb-chip" + (!CB.searchSys ? " on" : ""), "All");
  all.type = "button";
  all.addEventListener("click", () => { CB.searchSys = null; cbPaintSearch(); if (input.value) cbRunSearch(input.value); });
  chips.appendChild(all);
  for (const [id, lab] of CB_SYS) {
    const b = el("button", "cb-chip sys-" + id + (CB.searchSys === id ? " on" : ""), lab);
    b.type = "button";
    b.addEventListener("click", () => { CB.searchSys = id; cbPaintSearch(); if (input.value) cbRunSearch(input.value); });
    chips.appendChild(b);
  }
  const hits = el("div", "cb-hits");
  hits.id = "cbHits";
  if (!CB.searchHits.length) {
    hits.appendChild(el("p", "jn-note", "Search a disease, problem or code, then click a result to add it to the cohort."));
  } else {
    cbFillHits(hits, CB.searchHits);
  }
  side.replaceChildren(el("h2", "cb-side-title", "Code search"), box, chips, hits);
  input.value = side.dataset.q || "";
  if (input.value) input.focus();
}

function cbDrugName(h) {
  if (!h || h.system !== "NDC") return "";
  const lab = String(h.label || "").trim();
  if (!lab || lab.toUpperCase() === String(h.code || "").toUpperCase()) return "";
  return lab.split("·")[0].trim();
}

function cbNdcGroups(hits) {
  const groups = [];
  const by = new Map();
  for (const h of hits) {
    const name = cbDrugName(h);
    if (!name) continue;
    const key = name.toLowerCase();
    let g = by.get(key);
    if (!g) {
      g = { name, hits: [] };
      by.set(key, g);
      groups.push(g);
    }
    g.hits.push(h);
  }
  return groups.filter((g) => g.hits.length > 1);
}

function cbFillHits(box, hits) {
  box.replaceChildren();
  for (const g of cbNdcGroups(hits)) {
    const b = el("button", "cb-hit-all", `Add all ${g.name} NDCs (${g.hits.length})`);
    b.type = "button";
    b.addEventListener("click", () => cbAddNdcGroup(g.hits));
    box.appendChild(b);
  }
  for (const h of hits) box.appendChild(cbHitRow(h, true));
}

function cbHitRow(h, addable) {
  const row = el("button", "cb-hit");
  row.type = "button";
  row.append(el("span", "cb-sys sys-" + h.system, h.system === "ICD" ? "ICD-10" : h.system),
    el("span", "cb-hit-code", h.code),
    el("span", "cb-hit-lab", h.label || ""));
  if (addable) row.addEventListener("click", () => cbAddFromSearch(h));
  return row;
}

async function cbRunSearch(q) {
  const side = $("#cbSearch");
  if (side) side.dataset.q = q;
  const hits = $("#cbHits");
  const token = ++CB.searchToken;
  if (!(q || "").trim()) {
    CB.searchHits = [];
    if (hits) hits.replaceChildren(el("p", "jn-note", "Search a disease, problem or code, then click a result to add it to the cohort."));
    return;
  }
  let err = "";
  try {
    const sys = CB.searchSys ? `&system=${CB.searchSys}` : "";
    const found = await jget(`/api/codes/search?q=${jq(q)}${sys}`);
    if (token !== CB.searchToken) return;
    CB.searchHits = Array.isArray(found) ? found : [];
  } catch (e) {
    if (token !== CB.searchToken) return;
    CB.searchHits = [];
    err = e.message || String(e);
  }
  if (!hits) return;
  if (err) {
    hits.replaceChildren(el("p", "jn-note", "Code search failed: " + err));
    return;
  }
  if (!CB.searchHits.length) {
    hits.replaceChildren(el("p", "jn-note", "No matching codes in this warehouse."));
    return;
  }
  cbFillHits(hits, CB.searchHits);
}

function cbNewRoot() {
  return { kind: "group", id: cbUid(), root: true, children: [] };
}
function cbEnsureRoot() {
  if (!CB.tree) return;
  if (CB.tree.root) {
    if (!CB.tree.children.length) { CB.tree = null; CB.focus = null; }
    else if (CB.focus && CB.focus.root) CB.focus = CB.tree.children[0];
    return;
  }
  const wrap = cbNewRoot();
  wrap.children = [CB.tree];
  CB.tree = wrap;
  if (!CB.focus || CB.focus.root) CB.focus = wrap.children[0];
}
function cbFocusGroup() {
  let g = CB.focus;
  if (g && g.root) g = (g.children || [])[g.children.length - 1];
  if (g && g.kind === "group" && !g.root) return g;
  const kids = CB.tree && CB.tree.root ? CB.tree.children : [];
  return kids.length ? kids[kids.length - 1] : null;
}
function cbStartGroup() {
  CB.tree = cbNewRoot();
  const g = cbNewGroup();
  CB.tree.children = [g];
  CB.focus = g;
  cbPaintEditor();
  cbRefreshPreview();
}
function cbAddTopGroup(join) {
  cbEnsureRoot();
  if (!CB.tree) return cbStartGroup();
  const g = cbNewGroup(join || "AND");
  CB.tree.children.push(g);
  CB.focus = g;
  cbPaintEditor();
  cbRefreshPreview();
}

function cbInsertCrit(parent, crit) {
  const i = parent.children.findIndex((c) => c.kind === "group");
  if (i < 0) parent.children.push(crit);
  else parent.children.splice(i, 0, crit);
}

function cbPlaceSearchHit(h) {
  if (!cbFocusGroup()) {
    CB.tree = cbNewRoot();
    const g0 = cbNewGroup();
    CB.tree.children = [g0];
    CB.focus = g0;
  }
  const g = cbFocusGroup();
  if (!g || g.kind !== "group") return;
  const empty = g.children.find((c) => c.kind === "criterion" && !cbHasCodes(c));
  const same = [...g.children].reverse().find((c) => c.kind === "criterion" && c.system === h.system);
  let target = empty;
  if (!target && same) target = same;
  if (target) {
    target.system = h.system;
    cbAddCode(target, h.code, h.label);
  } else {
    const n = cbNewCrit(h.system, "AND");
    cbAddCode(n, h.code, h.label);
    cbInsertCrit(g, n);
  }
}

function cbAddFromSearch(h) {
  cbPlaceSearchHit(h);
  cbPaintEditor();
  cbRefreshPreview();
}

function cbAddNdcGroup(list) {
  for (const h of list) cbPlaceSearchHit(h);
  cbPaintEditor();
  cbRefreshPreview();
}

function cbCoverageBounds() {
  const c = (typeof state !== "undefined" && state.meta && state.meta.coverage) || {};
  return { min: (c.data_start || "").slice(0, 10), max: (c.data_end || "").slice(0, 10) };
}
function cbHasPeriod() {
  return !!(CB.studyPeriod.from || CB.studyPeriod.to);
}
function cbPeriodBody() {
  if (!cbHasPeriod()) return {};
  return { study_period: { from: CB.studyPeriod.from || null, to: CB.studyPeriod.to || null } };
}
function cbDateInput(which, label) {
  const wrap = el("label", "cb-date");
  wrap.appendChild(el("span", "cb-date-lab", label));
  const inp = el("input");
  inp.type = "date";
  inp.value = CB.studyPeriod[which] || "";
  const b = cbCoverageBounds();
  if (b.min) inp.min = b.min;
  if (b.max) inp.max = b.max;
  inp.setAttribute("aria-label", label);
  const apply = () => {
    CB.periodMode = "custom";
    CB.studyPeriod[which] = inp.value || "";
    if (CB.studyPeriod.from && CB.studyPeriod.to && CB.studyPeriod.from > CB.studyPeriod.to) {
      const swap = CB.studyPeriod.from;
      CB.studyPeriod.from = CB.studyPeriod.to;
      CB.studyPeriod.to = swap;
      cbPaintPeriod();
    }
    cbRefreshPreview();
  };
  inp.addEventListener("change", apply);
  inp.addEventListener("input", apply);
  wrap.appendChild(inp);
  return wrap;
}
function cbIsFullData() {
  if (CB.periodMode === "custom") return false;
  const b = cbCoverageBounds();
  return !!(b.min && b.max && CB.studyPeriod.from === b.min && CB.studyPeriod.to === b.max);
}
function cbApplyFullData() {
  const b = cbCoverageBounds();
  if (!b.min || !b.max) return;
  CB.periodMode = "full";
  CB.studyPeriod = { from: b.min, to: b.max };
  cbPaintPeriod();
  cbRefreshPreview();
}
function cbPaintPeriod() {
  const box = $("#cbPeriod");
  if (!box) return;
  const b = cbCoverageBounds();
  const head = el("header", "cb-ask-head");
  head.append(el("h2", null, "Study Period (Index Date)"));
  const seg = el("div", "cc-seg cb-period-seg");
  const full = el("button", "cc-seg-btn" + (cbIsFullData() ? " on" : ""), "Full data");
  full.type = "button";
  full.title = b.min && b.max ? `${b.min} to ${b.max}` : "Use the whole claims window";
  full.addEventListener("click", cbApplyFullData);
  const custom = el("button", "cc-seg-btn" + (!cbIsFullData() ? " on" : ""), "Custom range");
  custom.type = "button";
  custom.addEventListener("click", () => {
    CB.periodMode = "custom";
    if (!cbHasPeriod()) {
      const cov = cbCoverageBounds();
      CB.studyPeriod = { from: cov.min || "", to: cov.max || "" };
    }
    cbPaintPeriod();
    cbRefreshPreview();
    const from = $("#cbPeriod input[type=date]");
    if (from) from.focus();
  });
  seg.append(full, custom);
  const row = el("div", "cb-dates");
  row.append(seg, cbDateInput("from", "From"), cbDateInput("to", "To"));
  if (cbHasPeriod()) {
    const clr = el("button", "cb-add", "Clear dates");
    clr.type = "button";
    clr.addEventListener("click", () => {
      CB.periodMode = "";
      CB.studyPeriod = { from: "", to: "" };
      cbPaintPeriod();
      cbRefreshPreview();
    });
    row.appendChild(clr);
  }
  box.replaceChildren(head, row);
}

function cbPaintAsk() {
  const box = $("#cbAsk");
  if (!box) return;
  const head = el("header", "cb-ask-head");
  head.append(el("h2", null, "Ask in plain English"));
  const thread = el("div", "cb-ask-thread");
  if (!CB.chat.length) {
    thread.appendChild(el("p", "cb-ask-empty", "Try “How many patients have ICD C78?” or “C34 followed by C78 within 90 days”"));
  } else {
    for (const m of CB.chat) thread.appendChild(cbAskBubble(m));
  }
  const form = el("form", "cb-ask-form");
  const inp = el("textarea", "cb-ask-input");
  inp.rows = 2;
  inp.placeholder = "ICD codes with AND, OR, or followed by…";
  inp.setAttribute("aria-label", "Cohort question");
  const send = el("button", "cm-btn primary", CB.asking ? "Asking…" : "Ask");
  send.type = "submit";
  send.disabled = CB.asking;
  form.append(inp, send);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    cbAskSend(inp.value);
  });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); cbAskSend(inp.value); }
  });
  box.replaceChildren(head, thread, form);
  thread.scrollTop = thread.scrollHeight;
}

function cbAskBubble(m) {
  const row = el("div", "cb-ask-msg " + m.role);
  row.appendChild(el("div", "cb-ask-bubble", m.text));
  if (m.role === "assistant" && m.size != null) {
    const n = el("div", "cb-ask-count");
    n.append(el("span", "cb-n", fmtInt(m.size)), el("span", "cb-n-lab", "Patients"));
    row.appendChild(n);
  }
  if (m.tree) {
    const use = el("button", "cb-add", "Apply to criteria");
    use.type = "button";
    use.addEventListener("click", () => cbApplyAskTree(m.tree));
    row.appendChild(use);
  }
  return row;
}

function cbApplyAskTree(tree) {
  if (!tree) return;
  const g = tree.root ? tree : tree;
  CB.tree = { kind: "group", id: cbUid(), root: true, children: g.root ? g.children : [g] };
  CB.focus = cbFocusGroup();
  cbPaintEditor();
  cbRefreshPreview();
}

async function cbAskSend(raw) {
  const q = (raw || "").trim();
  if (!q || CB.asking) return;
  CB.asking = true;
  CB.chat.push({ role: "user", text: q });
  cbPaintAsk();
  let tree = null;
  try {
    const r = await fetch("/api/cohorts/ask", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q, ...cbPeriodBody() }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || r.statusText);
    tree = d.tree || null;
    CB.chat.push({
      role: "assistant",
      text: d.answer || (d.size != null ? `${fmtInt(d.size)} patients` : "No match"),
      size: d.empty ? 0 : d.size,
      tree,
    });
  } catch (e) {
    CB.chat.push({ role: "assistant", text: e.message || String(e), size: null, tree: null });
  }
  CB.asking = false;
  cbPaintAsk();
  if (tree) cbApplyAskTree(tree);
}

function cbPaintEditor() {
  const main = $("#cbWork") || $("#cbMain");
  if (!main) return;
  cbEnsureRoot();
  const editor = el("div", "cb-editor");
  if (CB.tree && CB.tree.children.length) {
    const stack = el("div", "cb-groups");
    CB.tree.children.forEach((g, i) => {
      if (i) stack.appendChild(cbJoinBar(g, false, null, null, false));
      stack.appendChild(cbRenderGroup(g, 0, String(i + 1), CB.tree, i === 0));
    });
    stack.appendChild(cbRenderTopAdd());
    editor.appendChild(stack);
  } else editor.appendChild(cbEmptyStart());
  const prev = el("section", "jn-panel cb-result");
  prev.id = "cbResult";
  editor.appendChild(prev);
  const saved = el("section", "jn-panel");
  saved.id = "cbSaved";
  editor.appendChild(saved);
  main.replaceChildren(editor);
  cbPaintPreview();
  cbPaintSaved();
}

function cbEmptyStart() {
  const box = el("div", "cb-group cb-empty");
  box.appendChild(el("p", "cb-empty-msg", "Create a patient cohort to get started with Analysis"));
  const add = el("button", "cb-add", "Add criteria");
  add.type = "button";
  add.addEventListener("click", cbStartGroup);
  box.appendChild(add);
  return box;
}

function cbRenderGroup(node, depth, label, parent, firstTop) {
  const box = el("div", "cb-group" + (depth ? " nested" : "") + (firstTop ? " cb-group-first" : ""));
  const head = el("div", "cb-ghead");
  head.appendChild(el("span", "cb-glabel", "Group " + label));
  const rm = el("button", "cb-x", "×");
  rm.title = "Remove group";
  rm.setAttribute("aria-label", "Remove group");
  cbBindRemove(rm, () => {
    if (parent) {
      parent.children = parent.children.filter((c) => c !== node);
      if (parent.root && !parent.children.length) {
        CB.tree = null;
        CB.focus = null;
      } else if (CB.focus === node) {
        CB.focus = parent.root ? (parent.children[0] || null) : parent;
      }
    } else {
      CB.tree = null;
      CB.focus = null;
    }
    cbPaintEditor();
    cbRefreshPreview();
  });
  head.appendChild(rm);
  box.appendChild(head);
  const body = el("div", "cb-gbody");
  let sub = 0;
  const criteria = node.children.filter((c) => c.kind !== "group");
  const nested = node.children.filter((c) => c.kind === "group");
  const cont = el("div", "cb-cont-wrap");
  let hasCont = false;
  criteria.forEach((ch, i) => {
    const target = i ? cont : body;
    if (i) {
      hasCont = true;
      const prev = criteria[i - 1];
      target.appendChild(cbJoinBar(ch, false, null, null, true));
      if (cbIsSeq(ch.join)) target.appendChild(cbSeqPanel(ch, prev));
    }
    const nxt = criteria[i + 1];
    const role = cbIsSeq(ch.join) ? "follow" : (nxt && cbIsSeq(nxt.join) ? "index" : "");
    const row = cbRenderCrit(ch, node, role);
    row.classList.add("cb-tint");
    target.appendChild(row);
  });
  if (hasCont) body.appendChild(cont);
  box.appendChild(body);
  const lastCrit = criteria[criteria.length - 1];
  const foot = el("div", "cb-gfoot");
  foot.append(cbJoinBar({ join: "AND" }, true, (join) => {
    CB.focus = node;
    cbInsertCrit(node, cbNewCrit("ICD", join));
    cbPaintEditor();
  }, null, !!lastCrit));
  box.appendChild(foot);
  const nestWrap = el("div", "cb-nested-compare");
  nested.forEach((g, i) => {
    const lab = i === 0 && criteria.length ? "Compare this combination with" : null;
    nestWrap.appendChild(cbJoinBar(g, false, null, lab, false));
    sub += 1;
    const nestedBox = cbRenderGroup(g, depth + 1, label + String.fromCharCode(96 + sub), node);
    nestedBox.classList.add("cb-tint");
    nestWrap.appendChild(nestedBox);
  });
  const addG = el("button", "cb-add", "+ Nested group");
  addG.type = "button";
  addG.addEventListener("click", () => {
    CB.focus = node;
    const g = cbNewGroup("AND");
    node.children.push(g);
    CB.focus = g;
    cbPaintEditor();
  });
  nestWrap.appendChild(addG);
  box.appendChild(nestWrap);
  box.addEventListener("click", (e) => {
    if (e.target.closest("button, input, select, textarea, a")) return;
    e.stopPropagation();
    CB.focus = node;
  });
  return box;
}

function cbRenderTopAdd() {
  const foot = el("div", "cb-gfoot cb-new-group");
  foot.append(
    cbJoinBar({ join: "AND" }, true, (join) => cbAddTopGroup(join), "Create a new group with", false),
    (() => {
      const add = el("button", "cb-add", "+ Create new group");
      add.type = "button";
      add.addEventListener("click", () => cbAddTopGroup("AND"));
      return add;
    })());
  return foot;
}

function cbTouchesSeq(node) {
  if (cbIsSeq(node.join)) return true;
  function walk(n) {
    const kids = n && n.children;
    if (!kids) return false;
    for (let i = 0; i < kids.length; i++) {
      if (kids[i] === node) return !!(kids[i + 1] && cbIsSeq(kids[i + 1].join));
      if (walk(kids[i])) return true;
    }
    return false;
  }
  return walk(CB.tree);
}

function cbJoinBar(node, adder, onAdd, lab, allowSeq) {
  const bar = el("div", "cb-join" + (adder ? " add" : ""));
  if (adder || lab) bar.appendChild(el("span", "cb-join-lab", lab || "Then compare with"));
  const ops = allowSeq === false ? ["AND", "OR"] : ["AND", "OR", "SEQ"];
  const labels = { AND: "AND", OR: "OR", SEQ: "SEQUENTIAL EVENT" };
  const cur = cbIsSeq(node.join) ? "SEQ" : (node.join || "AND");
  for (const j of ops) {
    const b = el("button", "cb-op " + j.toLowerCase() + (cur === j && !adder ? " on" : ""), labels[j]);
    b.type = "button";
    b.addEventListener("click", (e) => {
      e.stopPropagation();
      if (adder) onAdd(j);
      else {
        node.join = j;
        if (j === "SEQ") cbEnsureSeq(node);
        cbPaintEditor();
        cbRefreshPreview();
      }
    });
    bar.appendChild(b);
  }
  return bar;
}

function cbSeqPanel(node, prev) {
  const seq = cbEnsureSeq(node);
  const card = el("div", "cb-seq");
  const head = el("div", "cb-seq-head");
  head.append(
    el("strong", null, "Configure Follow-up Event"),
    el("span", "cb-seq-note", "not same-day — order matters"));
  card.appendChild(head);
  card.appendChild(el("p", "cb-seq-intro",
    "Define the conditions for the subsequent event that should occur after the initial event."));
  function row(lab, control, hint) {
    const r = el("div", "cb-seq-row");
    r.append(el("label", null, lab), control);
    if (hint) r.appendChild(el("span", "cb-seq-hint", hint));
    return r;
  }
  const type = el("select", "cb-sys-sel");
  for (const [id, lab] of CB_SYS) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = lab;
    if (id === node.system) o.selected = true;
    type.appendChild(o);
  }
  type.addEventListener("change", () => {
    node.system = type.value;
    node.codes = [];
    node.code = "";
    node.label = "";
    cbPaintEditor();
    cbRefreshPreview();
  });
  card.appendChild(row("Follow-up Event", type));
  const win = el("select", "cb-sys-sel");
  const curW = String(seq.window || "90");
  for (const [id, lab] of CB_WIN) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = lab;
    if (id === curW) o.selected = true;
    win.appendChild(o);
  }
  win.addEventListener("change", () => {
    seq.window = win.value;
    cbPaintEditor();
    cbRefreshPreview();
  });
  card.appendChild(row("Window", win));
  const dir = el("select", "cb-sys-sel");
  const dopt = document.createElement("option");
  dopt.value = "preceding";
  dopt.textContent = "Preceding";
  dopt.selected = true;
  dir.appendChild(dopt);
  card.appendChild(row("Direction", dir, "The initial event must occur before the follow-up (sequential) event."));
  const occ = el("select", "cb-sys-sel");
  for (const [id, lab] of [["1", "At least once"], ["2", "At least twice"]]) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = lab;
    if (String(seq.occurrence || 1) === id) o.selected = true;
    occ.appendChild(o);
  }
  occ.addEventListener("change", () => {
    seq.occurrence = Number(occ.value);
    cbPaintEditor();
    cbRefreshPreview();
  });
  card.appendChild(row("Occurrence", occ));
  const ic = cbCodes(prev || { codes: [] }).map((c) => c.code).join(" | ") || "…";
  const fc = cbCodes(node).map((c) => c.code).join(" | ") || "…";
  const wlab = (CB_WIN.find((w) => w[0] === String(seq.window || "90")) || CB_WIN[3])[1];
  const olab = Number(seq.occurrence) >= 2 ? "At least twice" : "At least once";
  const ex = el("div", "cb-seq-ex");
  ex.appendChild(el("div", "cb-seq-ex-flow",
    `${(prev && prev.system) || "ICD"} = %${ic}% → SEQUENTIAL EVENT → Follow-up Event = ${node.system} = %${fc}% → ${wlab} → Preceding → ${olab}`));
  const when = seq.window === "any" ? " any time after" : ` within ${seq.window || 90} days`;
  ex.appendChild(el("p", null,
    `This will identify patients with a ${ic} event followed by a ${fc} event${when}, occurring ${olab.toLowerCase()}.`));
  card.appendChild(ex);
  return card;
}

function cbPaintTags(tags, node, inp) {
  const chips = cbCodes(node);
  tags.replaceChildren();
  for (const item of chips) {
    const chip = el("span", "cb-tag");
    chip.appendChild(el("b", null, node.system === "ICD" ? cbIcdCaption(item.code) : item.code));
    const x = el("button", "cb-tag-x", "×");
    x.title = "Remove " + item.code;
    x.setAttribute("aria-label", "Remove " + item.code);
    cbBindRemove(x, () => {
      const key = String(item.code).toUpperCase();
      node.codes = cbCodes(node).filter((c) => String(c.code).toUpperCase() !== key);
      cbSyncCode(node);
      inp.placeholder = cbPlaceholder(node.system, cbHasCodes(node));
      cbPaintTags(tags, node, inp);
      inp.focus();
      if (cbTouchesSeq(node)) cbPaintEditor();
      cbRefreshPreview();
    });
    chip.appendChild(x);
    tags.appendChild(chip);
  }
  tags.appendChild(inp);
}

function cbCommitTyped(node, inp, tags, list) {
  const raw = inp.value.split("—")[0].trim();
  if (!raw) return;
  cbAddCode(node, raw, "");
  inp.value = "";
  list.hidden = true;
  inp.placeholder = cbPlaceholder(node.system, true);
  cbPaintTags(tags, node, inp);
  inp.focus();
  if (cbTouchesSeq(node)) cbPaintEditor();
  cbRefreshPreview();
}

function cbRenderCrit(node, parent, role) {
  const row = el("div", "cb-crit" + (role ? " cb-crit-" + role : ""));
  if (role === "index" || role === "follow") {
    const lab = el("div", "cb-event-lab");
    lab.textContent = role === "index" ? "Index event" : "Follow-up event";
    row.appendChild(lab);
  }
  const sel = el("select", "cb-sys-sel");
  for (const [id, lab] of CB_SYS) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = lab;
    if (id === node.system) o.selected = true;
    sel.appendChild(o);
  }
  const wrap = el("div", "cb-combo cb-multi");
  const tags = el("div", "cb-tags");
  const inp = el("input");
  inp.type = "text";
  inp.placeholder = cbPlaceholder(node.system, cbHasCodes(node));
  inp.setAttribute("autocomplete", "off");
  inp.setAttribute("aria-label", "Add codes");
  const list = el("div", "cb-sugg");
  list.hidden = true;
  sel.addEventListener("change", () => {
    node.system = sel.value;
    node.codes = [];
    node.code = "";
    node.label = "";
    inp.value = "";
    inp.placeholder = cbPlaceholder(sel.value, false);
    list.replaceChildren();
    cbPaintTags(tags, node, inp);
    cbRefreshPreview();
  });
  let t = 0;
  inp.addEventListener("input", () => {
    if (inp.value.endsWith(",")) {
      inp.value = inp.value.slice(0, -1);
      cbCommitTyped(node, inp, tags, list);
      return;
    }
    clearTimeout(t);
    t = setTimeout(() => cbSuggest(node, inp, list, tags), 180);
  });
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Tab" && inp.value.trim()) {
      e.preventDefault();
      cbCommitTyped(node, inp, tags, list);
    } else if (e.key === "Backspace" && !inp.value && cbCodes(node).length) {
      node.codes.pop();
      cbSyncCode(node);
      cbPaintTags(tags, node, inp);
      inp.placeholder = cbPlaceholder(node.system, cbHasCodes(node));
      inp.focus();
      if (cbTouchesSeq(node)) cbPaintEditor();
      cbRefreshPreview();
    }
  });
  inp.addEventListener("focus", () => { if (inp.value.trim()) cbSuggest(node, inp, list, tags); });
  inp.addEventListener("blur", () => setTimeout(() => { list.hidden = true; }, 180));
  cbPaintTags(tags, node, inp);
  wrap.append(tags, list);
  const rm = el("button", "cb-x", "×");
  rm.title = "Remove criterion";
  rm.setAttribute("aria-label", "Remove criterion");
  cbBindRemove(rm, () => {
    parent.children = parent.children.filter((c) => c !== node);
    if (!parent.children.length) parent.children.push(cbNewCrit("ICD"));
    cbPaintEditor();
    cbRefreshPreview();
  });
  row.append(sel, wrap, rm);
  return row;
}

async function cbSuggest(node, inp, list, tags) {
  const q = inp.value.split("—")[0].trim();
  if (!q) { list.hidden = true; return; }
  let hits = [];
  try { hits = await jget(`/api/codes/suggest?system=${node.system}&q=${jq(q)}`); }
  catch (e) { hits = []; }
  const taken = new Set(cbCodes(node).map((c) => c.code.toUpperCase()));
  hits = hits.filter((h) => !taken.has(String(h.code).toUpperCase()));
  list.replaceChildren();
  if (!hits.length) { list.hidden = true; return; }
  for (const h of hits) {
    const b = el("button", "cb-sugg-item");
    b.type = "button";
    b.append(el("b", null, h.code), document.createTextNode(" " + (h.label || "")));
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("click", () => {
      cbAddCode(node, h.code, h.label);
      inp.value = "";
      list.hidden = true;
      inp.placeholder = cbPlaceholder(node.system, true);
      cbPaintTags(tags, node, inp);
      inp.focus();
      if (cbTouchesSeq(node)) cbPaintEditor();
      cbRefreshPreview();
    });
    list.appendChild(b);
  }
  list.hidden = false;
}

async function cbRefreshPreview() {
  const token = ++CB.token;
  if (!CB.tree && !cbHasPeriod()) {
    CB.preview = { size: 0, empty: true };
    cbPaintPreview();
    return;
  }
  const body = JSON.stringify({ tree: CB.tree, ...cbPeriodBody() });
  try {
    CB.preview = await fetch("/api/cohorts/query", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body,
    }).then(async (r) => {
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || r.statusText);
      return d;
    });
  } catch (e) {
    CB.preview = { error: e.message || String(e), size: 0 };
  }
  if (token !== CB.token) return;
  cbPaintPreview();
}

function cbPaintPreview() {
  const box = $("#cbResult");
  if (!box) return;
  const p = CB.preview || {};
  const head = el("header");
  head.append(el("h2", null, "Matching patients"), el("span", null, p.label || p.error || (CB.tree ? "Add a code to preview" : "Add criteria to preview")));
  const n = el("div", "cb-preview");
  n.append(
    el("span", "cb-n", (p.empty && !p.size) || (p.error && !p.size) ? "0" : fmtInt(p.size || 0)),
    el("span", "cb-n-lab", "Patients"),
    el("span", "cb-n-name", p.universe ? (cbHasPeriod()
      ? `of ${fmtInt(p.universe)} after the index-date filter ${p.index_from || CB.studyPeriod.from || ""} to ${p.index_to || CB.studyPeriod.to || ""}`
      : `of ${fmtInt(p.universe)} in the warehouse cohort`) : ""));
  const form = el("form", "dg-save cb-save");
  const input = el("input");
  input.value = "Cohort";
  input.maxLength = 80;
  input.setAttribute("aria-label", "Cohort name");
  const save = el("button", "cm-btn primary", "Save cohort");
  save.type = "submit";
  save.disabled = !p.size;
  form.append(input, save);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!p.size) return;
    save.disabled = true;
    const r = await fetch("/api/cohorts/from-tree", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: input.value, tree: CB.tree, product: state.product, ...cbPeriodBody() }),
    });
    const c = await r.json();
    if (!r.ok) { save.disabled = false; alert(c.detail || r.statusText); return; }
    if (typeof cx !== "undefined") cx.opts = null;
    cbLoadSaved();
    save.disabled = false;
    save.textContent = "Saved";
    setTimeout(() => { save.textContent = "Save cohort"; }, 1600);
  });
  box.replaceChildren(head, n, form);
}

async function cbLoadSaved() {
  try {
    CB.saved = await jget("/api/cohorts");
  } catch (e) { CB.saved = []; }
  cbPaintSaved();
}

function cbPaintSaved() {
  const box = $("#cbSaved");
  if (!box) return;
  const saved = (CB.saved || []).slice().reverse();
  const head = el("header");
  head.append(el("h2", null, "Saved cohorts"), el("span", null, saved.length ? `${saved.length} saved` : "None yet"));
  box.replaceChildren(head);
  if (!saved.length) {
    box.appendChild(el("p", "jn-note", "Saved cohorts appear in the Compare panel."));
    return;
  }
  const ul = el("ul", "cb-saved");
  for (const c of saved) {
    const li = el("li");
    li.append(
      el("span", "cb-saved-name", c.name),
      el("span", "cb-saved-meta", `${c.kind === "hcp" ? "HCP" : "Patient"} · ${fmtInt(c.size)}`),
      el("span", "cb-saved-def", c.definition || ""));
    ul.appendChild(li);
  }
  box.appendChild(ul);
}

(function initCohort() {
  $("#btnCohort").addEventListener("click", () => showView("cohort"));
  $("#productSelect").addEventListener("change", () => { if (state.view === "cohort") renderCohortBuilder(); });
  if (new URLSearchParams(location.search).get("view") === "cohort") showView("cohort");
})();
