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
};

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
    const c = meta.coverage;
    $("#footModel").textContent = `${meta.model} · Bedrock`;
    $("#footStatus").title = `${meta.model} on Amazon Bedrock`;
    $("#footStatus").dataset.tip = `${meta.model} · connected`;
    $("#hintModel").textContent = meta.model;
    $("#footData").textContent = `${c.n_patients.toLocaleString()} patients · ${fmtPeriod(c)}`;
    $("#scopeMarket").textContent = meta.indication.replace(/\s*\(.*\)\s*/, "") || meta.indication;
    $("#scopePeriod").textContent = fmtPeriod(c);
    $("#chipData").title = `${meta.indication} · ${c.months} months of claims (${c.data_start} to ${c.data_end})`;
  } catch (e) {
    $("#footData").textContent = "Backend unavailable";
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
  $("#btnCompare").addEventListener("click", startCompare);
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
  const kpis = $("#kpis");
  kpis.replaceChildren(...Array.from({ length: 5 }, () => {
    const k = el("div", "kpi skeleton"); k.append(el("div", "k-val"), el("div", "k-lab", " ")); return k;
  }));
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
  if (!state.product) return;
  try {
    const ov = await (await fetch(`/api/overview?product=${encodeURIComponent(state.product)}`)).json();
    kpis.replaceChildren(...ov.kpis.map((k) => {
      const d = el("div", "kpi");
      d.append(el("div", "k-val", k.format === "pct" ? fmtPct(k.value) : fmtInt(k.value)), el("div", "k-lab", k.label));
      return d;
    }));
    const tc = $("#trendCard");
    tc.replaceChildren();
    if (ov.trend_chart) {
      const c = el("div", "chart");
      tc.appendChild(c);
      embed(c, ov.trend_chart, 200);
    }
  } catch (e) { /* overview is decorative; ignore */ }
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
  if (conv.product) { state.product = conv.product; $("#productSelect").value = conv.product; }
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
  else if (product) b.appendChild(el("span", "b-brand", `Focus: ${product}`));
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

function startCompare() {
  const focus = state.product;
  const brands = [focus, ...defaultCompetitors(focus)];
  submit(`Compare ${brands.join(" vs ")} side by side`);
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
  head.append(title, tag, interpret, dl);

  const brandBar = el("div", "cmp-brands");
  const tabs = el("div", "cmp-tabs");
  const body = el("div", "card-body cmp-body");
  const overlay = el("div", "cmp-loading");
  overlay.append(el("span", "cmp-spinner"), el("span", null, "Updating comparison…"));
  overlay.hidden = true;
  card.append(head, brandBar, tabs, body, overlay);

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
  actions.appendChild(actionButton("download", "Download data (CSV)", () => downloadCSV(res)));
  const png = actionButton("image", "Download charts (PNG)", () => downloadPNG(res, chartViews));
  png.hidden = !res.charts?.length;
  const expand = actionButton("expand", "Expand", () => setExpanded(!card.classList.contains("expanded")));
  actions.append(png, expand);
  controls.append(tabs, actions);
  head.append(titles, controls);
  card.append(head, body);

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

// ---------------------------------------------------------------- follow-up suggestions
const FOLLOWUPS = {
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
