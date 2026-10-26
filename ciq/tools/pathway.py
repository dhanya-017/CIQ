"""Pathway diagrams: the patient journey drawn as stage boxes with the codes that define each stage inside.

A pathway is a definition (stages, each with the codes that mark it and a time window around the diagnosis). The
diagram is computed from it for the newly diagnosed cohort: patients reaching each stage, the codes behind each box,
and who moves on between consecutive boxes. The agent edits a pathway the way it edits charts, except that an edit
to codes, stages or rules changes the numbers, so every edit re-runs the analysis and is labelled as a custom pathway
with its differences from the standard one.

Code syntax: dx:<ICD-10> (a trailing * is a prefix, e.g. dx:R91*), px:<CPT/HCPCS> (trailing * allowed),
drug:<therapy group or product>, e.g. drug:LUNG_SYSTEMIC or drug:KEYTRUDA.
"""
from __future__ import annotations

import copy
import re

import pandas as pd

from ciq.tools import journey as J
from ciq.tools.base import ToolResult, coverage, kpi, pct, query, resolve_product

WINDOWS = {"workup": "work-up window (diagnosis - {workup_before} to + {workup_after} days)",
           "after": "from {workup_after} days before diagnosis onward",
           "anchor": "first lung-cancer diagnosis (defines the cohort)"}
MAX_STAGES = 10
SHOW_CODES = 4

# short descriptions for the standard codes (procedure codes carry no description in this data)
CODE_DESC = {
    "71250": "CT chest without contrast", "71260": "CT chest with contrast", "71270": "CT chest without & with contrast",
    "78815": "PET/CT, skull base to mid-thigh", "78816": "PET/CT, whole body", "G0235": "PET imaging (other)",
    "70551": "MRI brain without contrast", "70552": "MRI brain with contrast", "70553": "MRI brain without & with",
    "99204": "New patient visit (moderate)", "99205": "New patient visit (high complexity)",
    "31628": "Bronchoscopy with lung biopsy", "31629": "Bronchoscopy with needle aspiration",
    "31652": "EBUS needle sampling, 1-2 nodes", "31653": "EBUS needle sampling, 3+ nodes",
    "32405": "Percutaneous lung biopsy", "32408": "Image-guided lung core biopsy",
    "10005": "Fine-needle aspiration, ultrasound", "10009": "Fine-needle aspiration, CT", "10012": "Fine-needle aspiration, imaging",
    "88305": "Surgical pathology, tissue exam", "88173": "Cytopathology, FNA evaluation",
    "88360": "IHC, quantitative (e.g. PD-L1)", "88361": "IHC, computer-assisted", "81235": "EGFR gene analysis",
    "81210": "BRAF gene analysis", "81275": "KRAS gene analysis", "81445": "Solid tumour NGS panel, 5-50 genes",
    "81449": "Solid tumour NGS panel (RNA)", "81455": "Solid tumour NGS panel, 51+ genes",
    "81457": "Solid tumour molecular analysis", "81459": "Solid tumour molecular analysis",
    "81479": "Unlisted molecular pathology", "88374": "FISH, computer-assisted", "88377": "FISH, manual multiplex",
    "0022U": "NGS lung panel (companion diagnostic)", "0037U": "Comprehensive genomic profiling",
    "0239U": "Liquid biopsy genomic profiling", "0242U": "Liquid biopsy genomic profiling",
    "32480": "Lobectomy", "32482": "Bilobectomy", "32484": "Segmentectomy", "32486": "Sleeve lobectomy",
    "32488": "Completion pneumonectomy", "32505": "Wedge resection", "32663": "VATS lobectomy",
    "32666": "VATS wedge resection", "32669": "VATS segmentectomy", "32440": "Pneumonectomy", "32670": "VATS bilobectomy",
    "32671": "VATS pneumonectomy", "77373": "Stereotactic body radiation (SBRT)", "77385": "IMRT delivery",
    "77386": "IMRT delivery, complex", "77387": "Radiation guidance and tracking",
    "77402": "Radiation treatment delivery", "77407": "Radiation treatment delivery", "77412": "Radiation treatment delivery", "77427": "Radiation treatment management",
}


def standard() -> dict:
    """The reference pathway used by pathway diagrams and journey analytics."""
    s = lambda k, codes, window: {"key": k, "label": J.LABEL[k], "codes": codes, "window": window}
    px = lambda xs: [f"px:{c}" for c in xs]
    return {"name": "Standard NSCLC pathway", "filter": "all",
            "rules": {"workup_before": J.PRE_WINDOW, "workup_after": J.PRE_AFTER, "stall_days": J.STALL_DAYS},
            "stages": [s("suspicion", ["dx:R91*"], "workup"), s("imaging", px(J.IMAGING), "workup"),
                       s("consult", px(J.CONSULT), "workup"), s("tissue", px(J.TISSUE), "workup"),
                       s("diagnosis", ["dx:C34*"], "anchor"), s("biomarker", px(J.BIOMARKER), "after"),
                       s("treatment", ["drug:LUNG_SYSTEMIC"] + px(J.SURGERY + J.RADIATION), "after")]}


# ---------------------------------------------------------------------------- codes
def _parse(code: str) -> tuple[str, str, bool]:
    m = re.fullmatch(r"\s*(dx|px|drug):\s*([A-Za-z0-9._\- ]+?)(\*?)\s*", str(code))
    if not m:
        raise ValueError(f"bad code '{code}': use dx:<ICD-10> (dx:R91* for a prefix), px:<CPT/HCPCS> or "
                         "drug:<product or LUNG_SYSTEMIC>")
    kind, val, star = m.group(1), m.group(2).strip(), bool(m.group(3))
    return kind, (val.upper() if kind != "drug" else val), star


def _norm(code: str) -> str:
    kind, val, star = _parse(code)
    if kind == "drug" and val.upper() != "LUNG_SYSTEMIC":
        val = resolve_product(val)
    elif kind == "drug":
        val = "LUNG_SYSTEMIC"
    return f"{kind}:{val}{'*' if star else ''}"


def _cond(code: str) -> tuple[str, str, str]:
    """-> (event table, SQL condition, column naming the matched code)"""
    kind, val, star = _parse(code)
    v = val.replace("'", "''")
    if kind == "dx":
        return "fact_dx_event", (f"dx_code like '{v}%'" if star else f"dx_code = '{v}'"), "dx_code"
    if kind == "px":
        return "fact_px_event", (f"px_code like '{v}%'" if star else f"px_code = '{v}'"), "px_code"
    if val == "LUNG_SYSTEMIC":
        return "fact_drug_event", "therapy_group = 'LUNG_SYSTEMIC'", "product"
    return "fact_drug_event", f"product = '{v}'", "product"


def validate_codes(codes: list[str]) -> list[str]:
    """Normalise codes and check each one occurs in the claims; raise with the ones that don't."""
    out, missing = [], []
    for c in codes:
        n = _norm(c)
        table, cond, _ = _cond(n)
        if query(f"select count(*) n from {table} where {cond}").iloc[0]["n"] == 0:
            missing.append(n)
        out.append(n)
    if missing:
        raise ValueError(f"no claims match {', '.join(missing)}. Check the code (ICD-10 codes include the dot, e.g. "
                         "dx:C34.90; use a trailing * for a prefix, e.g. dx:R91*; CPT codes are 5 characters)")
    return out


# ---------------------------------------------------------------------------- compute a diagram
def compute(pw: dict, top_codes: int = SHOW_CODES) -> dict:
    df, cohort = J._select(J.patient_stages(), pw.get("filter") or "all")
    if df.empty:
        raise ValueError(f"the cohort '{cohort}' is empty")
    r = pw["rules"]
    end = pd.Timestamp(coverage()["data_end"])
    stages = pw["stages"]
    parts = []
    for s in stages:
        if s["window"] == "anchor":
            continue
        for c in s["codes"]:
            table, cond, col = _cond(c)
            parts.append(f"select patient_id, service_date d, '{s['key']}' st, {col}::varchar code "
                         f"from {table} where {cond}")
    win = " or ".join(
        f"(e.st = '{s['key']}' and e.d between c.dx - {r['workup_before']} and c.dx + {r['workup_after']})"
        if s["window"] == "workup" else f"(e.st = '{s['key']}' and e.d >= c.dx - {r['workup_after']})"
        for s in stages if s["window"] != "anchor") or "false"
    ev = query(f"""
        with c as (select * from (values {', '.join(f"('{p}', DATE '{d.date()}')" for p, d in
                                              zip(df['patient_id'], df['d_diagnosis']))}) t(patient_id, dx)),
        e as ({' union all '.join(parts) if parts else "select null patient_id, null::date d, null st, null code where false"})
        select e.patient_id, e.st, e.code, e.d from e join c using (patient_id)
        where {win}""")
    anchor = next((s for s in stages if s["window"] == "anchor"), None)
    n = len(df)
    first = ev.groupby(["patient_id", "st"])["d"].min().unstack() if len(ev) else pd.DataFrame()
    dates = pd.DataFrame(index=df["patient_id"].values)
    for s in stages:
        if s["window"] == "anchor":
            dates[s["key"]] = df["d_diagnosis"].values
        else:
            dates[s["key"]] = pd.to_datetime(first[s["key"]]).reindex(dates.index) if s["key"] in first else pd.NaT
    dx = pd.Series(df["d_diagnosis"].values, index=dates.index)
    out_stages = []
    for s in stages:
        d = dates[s["key"]]
        codes = []
        if s["window"] == "anchor":
            codes = [{"code": c.split(":", 1)[1], "desc": "First lung-cancer diagnosis", "patients": n}
                     for c in s["codes"]]
            more = 0
        else:
            g = ev[ev.st == s["key"]].groupby("code")["patient_id"].nunique().sort_values(ascending=False)
            codes = [{"code": str(c), "desc": _desc(str(c)), "patients": int(v)} for c, v in g.head(top_codes).items()]
            more = max(len(g) - top_codes, 0)
        out_stages.append({"key": s["key"], "label": s["label"], "window": s["window"], "rules": s["codes"],
                           "patients": int(d.notna().sum()), "pct": float(d.notna().mean()),
                           "median_day": J._med((d - dx).dt.days), "codes": codes, "more_codes": more})
    trans = []
    for a, b in zip(stages, stages[1:]):
        da, db = dates[a["key"]], dates[b["key"]]
        has = da.notna()
        reached = has & db.notna()
        enough = (end - da).dt.days >= r["stall_days"]
        days = (db - da).dt.days[reached].clip(lower=0)
        trans.append({"a": a["key"], "b": b["key"], "label": f"{a['label']} → {b['label']}", "from": int(has.sum()),
                      "reached": int(reached.sum()), "not_reached": int((has & ~reached & enough).sum()),
                      "too_early": int((has & ~reached & ~enough).sum()),
                      "pct": float(reached.sum() / has.sum()) if has.sum() else None, "median_days": J._med(days)})
    keys = [s["key"] for s in stages]
    ordered = dates.apply(lambda row: [k for _, i, k in sorted((v, i, k) for i, (k, v) in enumerate(row.items())
                                                                if pd.notna(v))], axis=1)
    complete = sum(1 for q in ordered if len(q) == len(keys) and J._lcs(q, keys) == len(keys))
    return {"cohort": cohort, "n": n, "stages": out_stages, "transitions": trans, "complete": complete,
            "anchor": anchor["key"] if anchor else None}


def _desc(code: str) -> str:
    if code in CODE_DESC:
        return CODE_DESC[code]
    if not re.search(r"\d", code):   # a product name
        return "Systemic therapy"
    if re.fullmatch(r"[A-Z]\d{2}(\.\w+)?", code):
        d = query("select any_value(dx_desc) d from fact_dx_event where dx_code = $c", {"c": code}).iloc[0]["d"]
        return d or ""
    return ""


# ---------------------------------------------------------------------------- edits
def _stage(pw: dict, ref: str) -> dict:
    q = str(ref).strip().lower()
    for s in pw["stages"]:
        if q in (s["key"].lower(), s["label"].lower()):
            return s
    hits = [s for s in pw["stages"] if q in s["label"].lower()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"no stage '{ref}'. Stages: " + ", ".join(f"{s['key']} ({s['label']})" for s in pw["stages"]))


def _slug(label: str, pw: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:24] or "stage"
    k, i = base, 2
    while any(s["key"] == k for s in pw["stages"]):
        k, i = f"{base}_{i}", i + 1
    return k


def _no_anchor(s: dict, what: str):
    if s["window"] == "anchor":
        raise ValueError(f"the diagnosis stage anchors the cohort (first C34 diagnosis) and cannot be {what}")


def apply(pw: dict, ops: list[dict]) -> tuple[dict, list[str], dict]:
    """Apply edits -> (new pathway, change descriptions, presentation settings)."""
    pw = copy.deepcopy(pw)
    changes, view = [], {}
    if not isinstance(ops, list):
        raise ValueError("ops must be a list of edit objects")
    for op in ops:
        if not isinstance(op, dict) or "op" not in op:
            raise ValueError(f"each edit needs an 'op': {op}")
        o = op["op"]
        if o in ("add_codes", "remove_codes"):
            s = _stage(pw, op.get("stage", ""))
            if o == "add_codes":
                _no_anchor(s, "given other codes")
                codes = validate_codes(op.get("codes") or [])
                new = [c for c in codes if c not in s["codes"]]
                s["codes"] += new
                changes.append(f"added {', '.join(new) or 'no new codes'} to {s['label']}")
            else:
                _no_anchor(s, "given other codes")
                drop = {_norm(c) for c in op.get("codes") or []}
                unknown = drop - set(s["codes"])
                if unknown:
                    raise ValueError(f"{s['label']} does not contain {', '.join(sorted(unknown))}. Its codes: "
                                     f"{', '.join(s['codes'])}")
                s["codes"] = [c for c in s["codes"] if c not in drop]
                if not s["codes"]:
                    raise ValueError(f"removing these leaves {s['label']} with no codes: remove the stage instead")
                changes.append(f"removed {', '.join(sorted(drop))} from {s['label']}")
        elif o == "add_stage":
            label = str(op.get("label") or "").strip()
            if not label:
                raise ValueError("add_stage needs a label")
            if len(pw["stages"]) >= MAX_STAGES:
                raise ValueError(f"a pathway can have at most {MAX_STAGES} stages")
            s = {"key": _slug(label, pw), "label": label[:40], "codes": validate_codes(op.get("codes") or []),
                 "window": op.get("window")}
            if not s["codes"]:
                raise ValueError("add_stage needs at least one code")
            after = op.get("after")
            if op.get("before"):
                idx = pw["stages"].index(_stage(pw, op["before"]))
            elif after == "start":
                idx = 0
            elif after:
                idx = pw["stages"].index(_stage(pw, after)) + 1
            else:
                idx = len(pw["stages"])
            pw["stages"].insert(idx, s)
            if s["window"] is None:   # default: before the diagnosis -> work-up window, after it -> diagnosis onward
                anchor = next((i for i, x in enumerate(pw["stages"]) if x["window"] == "anchor"), len(pw["stages"]))
                s["window"] = "workup" if idx < anchor else "after"
            if s["window"] not in ("workup", "after"):
                raise ValueError("window must be 'workup' (around diagnosis) or 'after' (diagnosis onward)")
            changes.append(f"added stage {s['label']} ({', '.join(s['codes'])})")
        elif o == "remove_stage":
            s = _stage(pw, op.get("stage", ""))
            _no_anchor(s, "removed")
            if len(pw["stages"]) <= 2:
                raise ValueError("a pathway needs at least two stages")
            pw["stages"].remove(s)
            changes.append(f"removed stage {s['label']}")
        elif o == "rename_stage":
            s = _stage(pw, op.get("stage", ""))
            old, s["label"] = s["label"], str(op.get("label") or s["label"])[:40]
            changes.append(f"renamed {old} to {s['label']}")
        elif o == "move_stage":
            s = _stage(pw, op.get("stage", ""))
            pw["stages"].remove(s)
            if op.get("before"):
                pw["stages"].insert(pw["stages"].index(_stage(pw, op["before"])), s)
                where = f"before {op['before']}"
            else:
                after = op.get("after", "start")
                pw["stages"].insert(0 if after == "start" else pw["stages"].index(_stage(pw, after)) + 1, s)
                where = f"after {after}"
            changes.append(f"moved {s['label']} {where}")
        elif o == "merge_stages":
            ss = [_stage(pw, x) for x in op.get("stages") or []]
            if len(ss) < 2:
                raise ValueError("merge_stages needs two or more stages")
            for s in ss:
                _no_anchor(s, "merged")
            keep = ss[0]
            keep["codes"] = list(dict.fromkeys(c for s in ss for c in s["codes"]))
            keep["window"] = "workup" if all(s["window"] == "workup" for s in ss) else "after"
            old = [s["label"] for s in ss]
            keep["label"] = str(op.get("label") or " / ".join(old))[:40]
            for s in ss[1:]:
                pw["stages"].remove(s)
            changes.append(f"merged {', '.join(old)} into {keep['label']}")
        elif o == "set_window":
            s = _stage(pw, op.get("stage", ""))
            _no_anchor(s, "given another window")
            if op.get("window") not in ("workup", "after"):
                raise ValueError("window must be 'workup' or 'after'")
            s["window"] = op["window"]
            changes.append(f"{s['label']} now counted {WINDOWS[s['window']].format(**pw['rules'])}")
        elif o == "set_rule":
            rule, val = op.get("rule"), op.get("value")
            if rule not in pw["rules"]:
                raise ValueError(f"rule must be one of {list(pw['rules'])}")
            if not isinstance(val, (int, float)) or not 0 <= val <= 365:
                raise ValueError("value must be a number of days between 0 and 365")
            pw["rules"][rule] = int(val)
            changes.append(f"{rule.replace('_', ' ')} set to {int(val)} days")
        elif o == "filter":
            spec = op.get("cohort") or "all"
            _, label = J._select(J.patient_stages(), spec)
            pw["filter"] = spec
            changes.append(f"cohort: {label}")
        elif o == "highlight":
            if op.get("transition"):
                a, _, b = str(op["transition"]).replace("→", ">").partition(">")
                view["highlight"] = {"transition": [_stage(pw, a)["key"], _stage(pw, b)["key"]]}
                changes.append(f"highlighted {a.strip()} → {b.strip()}")
            else:
                s = _stage(pw, op.get("stage", ""))
                view["highlight"] = {"stage": s["key"]}
                changes.append(f"highlighted {s['label']}")
        elif o == "show_codes":
            view["top_codes"] = max(1, min(int(op.get("n", SHOW_CODES)), 12))
            changes.append(f"showing up to {view['top_codes']} codes per stage")
        else:
            raise ValueError(f"unknown op '{o}'. Ops: add_codes, remove_codes, add_stage, remove_stage, rename_stage, "
                             "move_stage, merge_stages, set_window, set_rule, filter, highlight, show_codes")
    return pw, changes, view


def diff(pw: dict) -> list[str]:
    """How a pathway differs from the standard one (definition only)."""
    std = {s["key"]: s for s in standard()["stages"]}
    out = []
    for s in pw["stages"]:
        o = std.get(s["key"])
        if not o:
            out.append(f"New stage {s['label']}: {', '.join(s['codes'])}")
            continue
        add, rem = [c for c in s["codes"] if c not in o["codes"]], [c for c in o["codes"] if c not in s["codes"]]
        if add or rem or s["window"] != o["window"]:
            out.append(f"{s['label']}: " + "; ".join(x for x in [add and f"+ {', '.join(add)}",
                                                                   rem and f"- {', '.join(rem)}",
                                                                   s["window"] != o["window"] and f"window {s['window']}"] if x))
    keys = {s["key"] for s in pw["stages"]}
    out += [f"Stage removed: {o['label']}" for k, o in std.items() if k not in keys]
    order = [s["key"] for s in pw["stages"] if s["key"] in std]
    if order != [k for k in std if k in keys]:
        out.append("Stage order changed")
    for k, v in pw["rules"].items():
        if v != standard()["rules"][k]:
            out.append(f"{k.replace('_', ' ')}: {v} days (standard {standard()['rules'][k]})")
    if (pw.get("filter") or "all") != "all":
        out.append(f"Cohort filter: {J._select(J.patient_stages(), pw['filter'])[1]}")
    return out


# ---------------------------------------------------------------------------- the agent tool
def pathway_diagram(source: str | None = None, ops: list[dict] | None = None, title: str | None = None, *,
                    history: list[dict]) -> ToolResult:
    """Draw the pathway as code boxes, or edit a pathway diagram shown earlier (every edit re-computes)."""
    base, earlier, view = standard(), [], {}
    if source and source.lower() not in ("standard", "default"):
        shown = [r for r in history if r.get("tool") == "pathway_diagram" and not r.get("withdrawn")]
        src = [r for r in shown if r.get("call_id") == source] or \
              [r for r in shown if source.strip().lower() in r.get("title", "").lower()]
        if not src:
            known = "; ".join(f"{r.get('call_id')}: {r['title']}" for r in shown[-6:]) or "none yet"
            raise ValueError(f"no pathway diagram matches '{source}'. Pathway diagrams shown (id: title): {known}. "
                             "Omit source to start from the standard pathway.")
        m = src[-1].get("meta", {})
        base, earlier, view = m["pathway"], m.get("changes", []), m.get("view", {})
    pw, changes, v = apply(base, ops or [])
    view = {**view, **v}
    if any(c.startswith("highlighted") for c in changes):   # a new highlight replaces the old one
        earlier = [c for c in earlier if not c.startswith("highlighted")]
    all_changes = earlier + changes
    d = compute(pw, view.get("top_codes", SHOW_CODES))
    ref = compute(standard()) if diff(pw) else None
    if ref:
        rs = {s["key"]: s["patients"] for s in ref["stages"]}
        std = {s["key"]: s for s in standard()["stages"]}
        for s in d["stages"]:
            o = std.get(s["key"])
            s["changed"] = bool(o) and (set(o["codes"]) != set(s["rules"]) or o["window"] != s["window"]
                                        or o["label"] != s["label"])
            s["vs_standard"] = s["patients"] - rs[s["key"]] if s["key"] in rs else None
            s["new"] = s["key"] not in rs
    dif = diff(pw)
    custom = bool(dif)
    name = title or ("Custom patient pathway" if custom else "Patient pathway: standard definition")
    visual = {"type": "pathway", **d, "custom": custom, "diff": dif, "highlight": view.get("highlight"),
              "rules": pw["rules"]}
    st = pd.DataFrame([{"stage": s["label"], "patients": s["patients"], "pct_of_cohort": s["pct"],
                        "median_days_from_diagnosis": s["median_day"], "codes": ", ".join(s["rules"]),
                        **({"vs_standard": s.get("vs_standard")} if custom else {})} for s in d["stages"]])
    codes = pd.DataFrame([{"stage": s["label"], "code": c["code"], "description": c["desc"], "patients": c["patients"]}
                          for s in d["stages"] for c in s["codes"]])
    tr = pd.DataFrame([{k: t[k] for k in ("label", "from", "reached", "not_reached", "too_early", "pct", "median_days")}
                       for t in d["transitions"]]).rename(columns={"label": "transition", "pct": "pct_moved_on"})
    worst = max(d["transitions"], key=lambda t: t["not_reached"] / max(t["from"], 1))
    ins = [f"{d['n']:,} newly diagnosed patients ({d['cohort']}); {d['complete']:,} ({pct(d['complete'] / d['n'])}) "
           f"have every stage, in order.",
           f"Largest drop-off: {worst['label']} ({worst['not_reached']:,} of {worst['from']:,} not observed moving on "
           f"after {pw['rules']['stall_days']}+ days)."]
    if custom:
        moved = [s for s in d["stages"] if s.get("vs_standard")]
        if moved:
            ins.append("Versus the standard pathway: " + "; ".join(
                f"{s['label']} {s['vs_standard']:+,} patients" for s in moved[:4]) + ".")
    summary = " → ".join(f"{s['label']} [{', '.join(s['codes'][:6])}{', …' if len(s['codes']) > 6 else ''}; "
                         f"{s['window']}]" for s in pw["stages"])
    return ToolResult(
        tool="pathway_diagram", title=name,
        subtitle=("Custom definition: " + "; ".join(all_changes)) if all_changes else
                 "Each box is a stage, with the claims codes that define it and the patients observed there",
        params={"source": source, "ops": ops or []},
        data={"stages": st, "codes": codes, "transitions": tr},
        kpis=[kpi("Patients", d["n"], note=d["cohort"]), kpi("Complete pathway", d["complete"] / d["n"], "pct"),
              kpi("Largest drop-off", worst["not_reached"], lead=True, note=worst["label"])],
        insights=ins, observations=ins,
        caveats=["Stages are what the claims show; care billed elsewhere looks missing.",
                 *([f"Custom pathway: numbers differ from the standard pathway ({'; '.join(dif)})."]
                   if custom else [])],
        meta={"pathway": pw, "changes": all_changes, "view": view, "visual": visual,
              "chart_summary": f"Pathway now: {summary}. Rules: {pw['rules']}. Cohort: {d['cohort']}."
                               + (f" Differences from standard: {'; '.join(dif)}." if dif else " (standard pathway)")})
