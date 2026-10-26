/* View switching (cohort / playground / chat) and pathway diagrams used in chat cards. */
"use strict";

function showView(view) {
  state.view = view;
  const cohort = view === "cohort";
  const playground = view === "playground";
  $("#cohort").hidden = !cohort;
  const pg = $("#playground");
  if (pg) pg.hidden = !playground;
  const hideChat = cohort || playground;
  $("#thread").hidden = hideChat;
  $(".composer-wrap").hidden = hideChat;
  $("#btnCohort").classList.toggle("active", cohort);
  $("#btnCohort").setAttribute("aria-current", cohort ? "page" : "false");
  const btnPg = $("#btnPlayground");
  if (btnPg) {
    btnPg.classList.toggle("active", playground);
    btnPg.setAttribute("aria-current", playground ? "page" : "false");
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

const jdays = (v) => (v == null ? "—" : `${Math.round(v)} day${Math.round(v) === 1 ? "" : "s"}`);
const jday = (v) => (v == null ? "" : v === 0 ? "day 0" : `day ${v > 0 ? "+" : "−"}${Math.abs(v)}`);

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
