/* CIQ - Playground Tab: default vs user-defined LOT rules, side by side. */
"use strict";

const PG = { token: 0, defaults: null, user: null };
const PG_FIELDS = [
  ["cycle_window_days", "Cycle Window (Days)", 1, 60],
  ["line_gap_days", "Allowable Gap between Therapy (Days)", 1, 730],
  ["min_cycles", "Minimum #Cycles for Line Advancement", 1, 30],
  ["min_days", "Minimum Treatment Period for Line Advancement (Days)", 1, 730],
];

function pgRulesFromForm(form) {
  const out = {};
  for (const [key] of PG_FIELDS) {
    const inp = form.querySelector(`[name="${key}"]`);
    out[key] = inp && inp.value !== "" ? Number(inp.value) : null;
  }
  return out;
}

function pgFillRules(form, rules) {
  for (const [key] of PG_FIELDS) {
    const inp = form.querySelector(`[name="${key}"]`);
    if (inp) inp.value = rules && rules[key] != null ? String(rules[key]) : "";
  }
}

function pgEmptyUserCharts(host) {
  host.replaceChildren(el("p", "pg-note", "Summaries appear here after you build."));
}

function pgRuleGrid(rules, editable) {
  const grid = el("div", "pg-rules");
  for (const [key, label, lo, hi] of PG_FIELDS) {
    const wrap = el("label", "pg-field");
    wrap.appendChild(el("span", "pg-field-lab", label));
    const inp = el("input");
    inp.type = "number";
    inp.name = key;
    inp.min = String(lo);
    inp.max = String(hi);
    inp.step = "1";
    inp.value = rules && rules[key] != null ? String(rules[key]) : "";
    inp.readOnly = !editable;
    inp.disabled = !editable;
    if (editable) inp.required = true;
    wrap.appendChild(inp);
    grid.appendChild(wrap);
  }
  return grid;
}

function pgPaintCharts(host, pack) {
  host.replaceChildren();
  if (!pack) return;
  host.appendChild(el("h3", "pg-lot-h", "LoT Summaries"));
  const add = (spec) => {
    if (!spec) return;
    const box = el("section", "cc-snap-chart");
    const node = el("div", "chart");
    box.appendChild(node);
    host.appendChild(box);
    requestAnimationFrame(() => embed(node, spec));
  };
  add(pack.lot_reach_chart);
  if (pack.lot_regimen_charts && pack.lot_regimen_charts.length) {
    host.appendChild(el("p", "cc-summary-kicker", "Top regimens by line"));
    const row = el("div", "cc-summary-row");
    for (const spec of pack.lot_regimen_charts) {
      const box = el("section", "cc-snap-chart");
      const node = el("div", "chart");
      box.appendChild(node);
      row.appendChild(box);
      requestAnimationFrame(() => embed(node, spec));
    }
    host.appendChild(row);
  }
  add(pack.lot_dot_chart);
  add(pack.lot_molecule_chart);
}

async function pgFetchLot(rules) {
  const r = await fetch("/api/playground/lot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(rules || {}),
  });
  const d = await r.json();
  if (!r.ok) {
    const det = d.detail;
    const msg = typeof det === "string" ? det
      : Array.isArray(det) ? det.map((x) => x.msg || JSON.stringify(x)).join("; ")
      : r.statusText;
    throw new Error(msg);
  }
  return d;
}

async function renderPlayground() {
  const root = $("#playground");
  if (!root) return;
  const token = ++PG.token;
  let defaults = PG.defaults;
  if (!defaults) {
    try {
      defaults = await (await fetch("/api/playground/defaults")).json();
      PG.defaults = defaults;
    } catch (e) {
      root.replaceChildren(el("div", "error-box", "Could not load playground defaults: " + (e.message || e)));
      return;
    }
  }
  if (token !== PG.token) return;

  const shell = el("div", "pg-shell");
  const head = el("header", "pg-head");
  head.append(
    el("h1", "jn-title", "Playground Tab"),
    el("p", "jn-sub", "Compare LoT summaries under the current business rules with a set you define."));
  const cols = el("div", "pg-cols");

  const defCol = el("section", "pg-col");
  defCol.append(
    el("h2", null, "Default rules"),
    el("p", "pg-note", "Cycle window, allowable gap, minimum cycles, and minimum days currently used to build lines."),
    pgRuleGrid(defaults, false),
    el("div", "pg-actions"));
  const defCharts = el("div", "pg-charts");
  defCharts.appendChild(el("p", "pg-note", "Building LoT summaries…"));
  defCol.appendChild(defCharts);

  const userCol = el("section", "pg-col");
  userCol.append(
    el("h2", null, "User defined rules"),
    el("p", "pg-note", "Set the four line-building parameters, then build summaries on this extract."));
  const form = el("form", "pg-form");
  form.appendChild(pgRuleGrid(PG.user && PG.user.rules ? PG.user.rules : {}, true));
  const actions = el("div", "pg-actions");
  const go = el("button", "cm-btn primary", "Build summaries");
  go.type = "submit";
  const clear = el("button", "cm-btn", "Clear");
  clear.type = "button";
  actions.append(go, clear);
  form.appendChild(actions);
  const userCharts = el("div", "pg-charts");
  if (PG.user) pgPaintCharts(userCharts, PG.user);
  else pgEmptyUserCharts(userCharts);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    go.disabled = true;
    clear.disabled = true;
    go.textContent = "Building…";
    userCharts.replaceChildren(el("p", "pg-note", "Building LoT summaries…"));
    try {
      const pack = await pgFetchLot(pgRulesFromForm(form));
      PG.user = pack;
      if (pack.rules) pgFillRules(form, pack.rules);
      pgPaintCharts(userCharts, pack);
    } catch (err) {
      userCharts.replaceChildren(el("div", "error-box", err.message || String(err)));
    }
    go.disabled = false;
    clear.disabled = false;
    go.textContent = "Build summaries";
  });
  clear.addEventListener("click", () => {
    pgFillRules(form, {});
  });
  userCol.append(form, userCharts);

  cols.append(defCol, userCol);
  shell.append(head, cols);
  root.replaceChildren(shell);

  try {
    const pack = await pgFetchLot(defaults);
    if (token !== PG.token) return;
    pgPaintCharts(defCharts, pack);
  } catch (err) {
    if (token !== PG.token) return;
    defCharts.replaceChildren(el("div", "error-box", err.message || String(err)));
  }
}

(function initPlayground() {
  const btn = $("#btnPlayground");
  if (btn) btn.addEventListener("click", () => showView("playground"));
  if (new URLSearchParams(location.search).get("view") === "playground") showView("playground");
})();
