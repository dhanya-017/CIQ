"""Patient Journey: reconstruct each patient's observable NSCLC pathway from claims and compare it to a reference.

Each newly diagnosed patient is encoded as a pathway vector: the first observed date of every reference stage.
From that come the journey map (who reaches each stage), transitions (who moves on, and how fast), a data-derived
journey status, similarity to the reference pathway, recurring journey patterns, and the HCPs associated with each
transition. Everything is observed in claims; statuses are data definitions, not clinical judgements.
"""
from __future__ import annotations

import json
from functools import lru_cache

import numpy as np
import pandas as pd

from ciq import config
from ciq.tools import charts, compare
from ciq.tools.base import coverage, json_safe, pct, query, resolve_product

# ---------------------------------------------------------------------------- the reference pathway
IMAGING = ["71250", "71260", "71270", "78815", "78816", "G0235", "70551", "70552", "70553"]
CONSULT = ["99204", "99205"]
TISSUE = ["31628", "31629", "31652", "31653", "32405", "32408", "10005", "10009", "10012", "88305", "88173"]
BIOMARKER = ["88360", "88361", "81235", "81210", "81275", "81445", "81449", "81455", "81457", "81459", "81479",
             "88374", "88377", "0022U", "0037U", "0239U", "0242U"]
SURGERY = ["32480", "32482", "32484", "32486", "32488", "32491", "32505", "32663", "32666", "32667", "32668",
           "32669", "32440", "32442", "32445", "32670", "32671"]
RADIATION = ["77373", "77385", "77386", "77387", "77402", "77407", "77412", "77427", "77520", "77522", "77523",
             "77525"] + [f"G60{n:02d}" for n in range(3, 16)]

# (key, label, how it is observed)
STAGES = [
    ("suspicion", "Suspicion", "Abnormal lung finding coded (ICD-10 R91.x, e.g. lung nodule)"),
    ("imaging", "Imaging work-up", "CT chest, PET/CT or brain MRI (CPT 71250/71260/71270, 78815/78816, 70551-70553)"),
    ("consult", "Specialist consult", "New-patient consultation visit (CPT 99204/99205); specialty is not in the "
                                      "data, so this is a proxy"),
    ("tissue", "Tissue diagnosis", "Biopsy (bronchoscopic, EBUS, needle) or pathology (CPT 88305/88173)"),
    ("diagnosis", "Diagnosis confirmed", "First lung-cancer diagnosis code (ICD-10 C34)"),
    ("biomarker", "Biomarker testing", "PD-L1 IHC, EGFR/BRAF/ALK/ROS1 molecular tests or NGS panels"),
    ("treatment", "Treatment start", "First lung systemic therapy, lung resection surgery or radiation therapy"),
]
KEYS = [k for k, _, _ in STAGES]
LABEL = {k: l for k, l, _ in STAGES}
SHORT = {"suspicion": "Suspicion", "imaging": "Imaging", "consult": "Consult", "tissue": "Tissue",
         "diagnosis": "Diagnosis", "biomarker": "Biomarker", "treatment": "Treatment"}

CLEAN_DAYS = 60       # incident cohort: first diagnosis >= this many days after data start (work-up observable)
PRE_WINDOW = 90       # work-up stages searched from diagnosis - 90 days ...
PRE_AFTER = 30        # ... to diagnosis + 30 days
STALL_DAYS = 60       # no treatment 60+ days after diagnosis = stalled (if that much follow-up exists)
DELAY_DAYS = 30       # diagnosis -> treatment above 30 days = delayed
DIAG_DELAY_DAYS = 30  # first suspicion/imaging to diagnosis above 30 days = diagnostic delay
ORDER_TOL = 7         # stages within 7 days of each other are not treated as out of order
STATUSES = ["On pathway", "Delayed", "Deviated", "Stalled", "In progress"]
STATUS_DEF = {
    "On pathway": f"Treatment observed within {DELAY_DAYS} days of diagnosis, not before diagnosis or tissue "
                  "diagnosis.",
    "Delayed": f"Treatment observed, but more than {DELAY_DAYS} days after diagnosis.",
    "Deviated": f"Treatment observed before the diagnosis or tissue-diagnosis stage (by more than {ORDER_TOL} days).",
    "Stalled": f"No treatment observed {STALL_DAYS}+ days after diagnosis (with at least that much follow-up).",
    "In progress": f"No treatment yet, but diagnosed less than {STALL_DAYS} days before data end: too early to tell.",
}
MEDICARE = compare.MEDICARE
MEDICAID = compare.MEDICAID


def _codes(xs):
    return "(" + ", ".join(f"'{x}'" for x in xs) + ")"


# ---------------------------------------------------------------------------- pathway vectors
@lru_cache(maxsize=1)
def patient_stages() -> pd.DataFrame:
    """One row per incident patient: first date of every stage, status, similarity, attributes."""
    cov = coverage()
    start = pd.Timestamp(cov["data_start"]) + pd.Timedelta(days=CLEAN_DAYS)
    end = pd.Timestamp(cov["data_end"])
    ev = query(f"""
        with dx as (select patient_id, min(service_date) dx_date, arg_min(hcp_id, service_date) dx_hcp
                    from fact_dx_event where dx_code like 'C34%' group by 1),
        coh as (select * from dx where dx_date >= $start),
        ev as (
          select patient_id, service_date d, 'suspicion' st, hcp_id, null::varchar m from fact_dx_event where dx_code like 'R91%'
          union all select patient_id, service_date, 'imaging', hcp_id, null from fact_px_event where px_code in {_codes(IMAGING)}
          union all select patient_id, service_date, 'consult', hcp_id, null from fact_px_event where px_code in {_codes(CONSULT)}
          union all select patient_id, service_date, 'tissue', hcp_id, null from fact_px_event where px_code in {_codes(TISSUE)}
          union all select patient_id, service_date, 'biomarker', hcp_id, null from fact_px_event where px_code in {_codes(BIOMARKER)}
          union all select patient_id, service_date, 'treatment', hcp_id, 'Systemic therapy' from fact_drug_event where therapy_group = 'LUNG_SYSTEMIC'
          union all select patient_id, service_date, 'treatment', hcp_id, 'Surgery' from fact_px_event where px_code in {_codes(SURGERY)}
          union all select patient_id, service_date, 'treatment', hcp_id, 'Radiation' from fact_px_event where px_code in {_codes(RADIATION)})
        select c.patient_id, c.dx_date, c.dx_hcp, e.st, min(e.d) d, arg_min(e.hcp_id, e.d) hcp, arg_min(e.m, e.d) modality
        from coh c join ev e using (patient_id)
        where (e.st in ('suspicion', 'imaging', 'consult', 'tissue')
               and e.d between c.dx_date - {PRE_WINDOW} and c.dx_date + {PRE_AFTER})
           or (e.st in ('biomarker', 'treatment') and e.d >= c.dx_date - {PRE_AFTER})
        group by all""", {"start": start.date()})
    base = query("""
        with dx as (select patient_id, min(service_date) dx_date, arg_min(hcp_id, service_date) dx_hcp
                    from fact_dx_event where dx_code like 'C34%' group by 1)
        select dx.*, p.age, p.age_group, p.payment_type from dx join dim_patient p using (patient_id)
        where dx_date >= $start""", {"start": start.date()})
    wide = ev.pivot_table(index="patient_id", columns="st", values="d", aggfunc="min")
    hcps = ev.pivot_table(index="patient_id", columns="st", values="hcp", aggfunc="first")
    mod = ev[ev.st == "treatment"].set_index("patient_id")["modality"]
    df = base.set_index("patient_id")
    for k in KEYS:
        if k == "diagnosis":
            df["d_diagnosis"] = pd.to_datetime(df["dx_date"])
            continue
        df[f"d_{k}"] = pd.to_datetime(wide[k]) if k in wide else pd.NaT
        df[f"h_{k}"] = hcps[k] if k in hcps else None
    df["h_diagnosis"] = df["dx_hcp"]
    df["modality"] = mod
    first_line = query("""select patient_id, string_agg(distinct product, ' + ' order by product) regimen,
                                 list(distinct product) products
                          from lot_line_drug where line_number = 1 group by 1""").set_index("patient_id")
    df = df.join(first_line)
    df["products"] = df["products"].apply(lambda v: list(v) if isinstance(v, (list, np.ndarray)) else [])
    df["payer_group"] = df["payment_type"].map(compare._pay_group)
    df["followup_days"] = (end - df["d_diagnosis"]).dt.days

    dts = df[[f"d_{k}" for k in KEYS]]
    df["n_stages"] = dts.notna().sum(axis=1)
    # observed order: by date, ties broken by reference order
    ordered = []
    for row in dts.itertuples(index=False):
        seen = [(d, i, KEYS[i]) for i, d in enumerate(row) if pd.notna(d)]
        ordered.append([k for _, _, k in sorted(seen)])
    df["sequence"] = ordered
    df["similarity"] = [_lcs(seq, KEYS) / len(KEYS) for seq in ordered]
    df["dx_to_tx"] = (df["d_treatment"] - df["d_diagnosis"]).dt.days
    tx, dx, tis = df["d_treatment"], df["d_diagnosis"], df["d_tissue"]
    before_dx = tx < dx - pd.Timedelta(days=ORDER_TOL)
    before_tis = tis.notna() & (tx < tis - pd.Timedelta(days=ORDER_TOL))
    df["deviation"] = np.select([before_dx, before_tis],
                                ["Treatment before the diagnosis code", "Treatment before tissue diagnosis"], "")
    df["biomarker_before_tx"] = df["d_biomarker"].notna() & (df["d_biomarker"] <= tx + pd.Timedelta(days=ORDER_TOL))
    df["status"] = np.select(
        [tx.notna() & (df["deviation"] != ""), tx.notna() & (df["dx_to_tx"] > DELAY_DAYS), tx.notna(),
         df["followup_days"] >= STALL_DAYS],
        ["Deviated", "Delayed", "On pathway", "Stalled"], "In progress")
    df["last_stage"] = [LABEL[seq[-1]] if seq else "" for seq in ordered]
    df["similarity_band"] = pd.cut(df["similarity"], [-0.01, 4 / 7 - 1e-9, 6 / 7 - 1e-9, 1.0],
                                   labels=["Low", "Moderate", "High"]).astype(str)
    first_workup = df[["d_suspicion", "d_imaging"]].min(axis=1)
    df["workup_days"] = (df["d_diagnosis"] - first_workup).dt.days
    in_order = pd.Series([_lcs(q, KEYS) == len(q) for q in ordered], index=df.index)
    treated = df["d_treatment"].notna()
    df["archetype"] = np.select(
        [~treated & (df["status"] == "In progress"),
         (df["n_stages"] == len(KEYS)) & in_order,
         df["status"] == "Deviated",
         df["workup_days"] > DIAG_DELAY_DAYS,
         ~treated,
         treated & df["d_biomarker"].isna(),
         treated & df["d_tissue"].notna()],
        ["Too early to tell", "Complete reference pathway", "Alternative order", "Diagnostic delay",
         "Diagnosis without treatment", "Treated without an observed biomarker test", "Treated after tissue diagnosis"],
        "Treated, limited work-up observed")
    return df.reset_index()


def _lcs(a: list, b: list) -> int:
    """Longest common subsequence length: how many stages occur in reference order."""
    return len(_lcs_items(a, b))


def _lcs_items(a: list, b: list) -> list:
    m = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            m[i + 1][j + 1] = m[i][j] + 1 if a[i] == b[j] else max(m[i][j + 1], m[i + 1][j])
    out, i, j = [], len(a), len(b)
    while i and j:
        if a[i - 1] == b[j - 1]:
            out.append(a[i - 1]); i -= 1; j -= 1
        elif m[i - 1][j] >= m[i][j - 1]:
            i -= 1
        else:
            j -= 1
    return out[::-1]


def _days(n) -> str:
    return f"{int(n)} day" + ("" if int(n) == 1 else "s")


def _med(s: pd.Series):
    s = s.dropna()
    return float(s.median()) if len(s) else None


# ---------------------------------------------------------------------------- scope: market or one brand's patients
SMALL_N = 100   # below this, percentages move a lot with a few patients


def cohort(scope: str = "market", product: str | None = None) -> tuple[pd.DataFrame, str]:
    """market = every newly diagnosed patient; brand = those whose first line of therapy includes the brand."""
    df = patient_stages()
    if scope in (None, "", "market"):
        return df, "All newly diagnosed patients"
    if scope == "brand":
        p = resolve_product(product)
        s = df[df["products"].apply(lambda xs: p in xs)]
        if s.empty:
            raise ValueError(f"no newly diagnosed patients have {p} in their first line of therapy")
        return s, f"{p} patients (first line includes {p})"
    raise ValueError("scope must be 'market' or 'brand'")


# ---------------------------------------------------------------------------- segments (for compare / filters)
def segment_options(product: str) -> list[dict]:
    df = patient_stages()
    top = pd.Series([p for xs in df["products"] for p in xs]).value_counts()
    brands = [product] + [p for p in top.index if p != product][:9]
    opts = [{"id": "all", "label": "All newly diagnosed patients"},
            {"id": "treated:yes", "label": "Treatment observed"}, {"id": "treated:no", "label": "No treatment observed"}]
    opts += [{"id": f"brand:{p}", "label": f"First line includes {p}", "group": "brand"} for p in brands]
    opts += [{"id": f"status:{s}", "label": f"Status: {s}"} for s in STATUSES]
    opts += [{"id": f"sim:{b}", "label": f"{b} similarity to reference"} for b in ("High", "Moderate", "Low")]
    opts += [{"id": "payer:Medicare", "label": "Medicare (all forms)"}, {"id": "payer:Commercial", "label": "Commercial"},
             {"id": "payer:Medicaid", "label": "Medicaid"}, {"id": "age:lt65", "label": "Under 65"},
             {"id": "age:65plus", "label": "65+"}, {"id": "biomarker:yes", "label": "Biomarker test observed"},
             {"id": "biomarker:no", "label": "No biomarker test observed"}]
    opts += [{"id": f"saved:{c['id']}", "label": c["name"], "saved": True}
             for c in compare.list_cohorts() if c.get("kind") == "patient"]
    return opts


def _select(df: pd.DataFrame, spec: str) -> tuple[pd.DataFrame, str]:
    kind, _, val = spec.partition(":")
    if spec == "all":
        return df, "All newly diagnosed patients"
    if kind == "brand":
        p = resolve_product(val)
        return df[df["products"].apply(lambda xs: p in xs)], f"First line includes {p}"
    if kind == "treated":
        has = df["d_treatment"].notna()
        return (df[has], "Treatment observed") if val == "yes" else (df[~has], "No treatment observed")
    if kind == "status" and val in STATUSES:
        return df[df["status"] == val], val
    if kind == "sim" and val in ("High", "Moderate", "Low"):
        return df[df["similarity_band"] == val], f"{val} similarity"
    if kind == "payer":
        return df[df["payer_group"] == val], val
    if kind == "age":
        if not df["age"].notna().any():
            raise ValueError("Age segments need patient age, which is not available in this data "
                             "(age comes from mx_nsclc.csv). Choose a segment not based on age.")
        return (df[df["age"] < 65], "Under 65") if val == "lt65" else (df[df["age"] >= 65], "65+")
    if kind == "biomarker":
        has = df["d_biomarker"].notna()
        return (df[has], "Biomarker test observed") if val == "yes" else (df[~has], "No biomarker test observed")
    if kind == "saved":
        c = next((c for c in compare.list_cohorts() if c["id"] == val and c.get("kind") == "patient"), None)
        if not c:
            raise ValueError(f"saved patient cohort '{val}' not found")
        return df[df["patient_id"].astype(str).isin(set(c["patient_ids"]))], c["name"]
    raise ValueError(f"unknown journey segment '{spec}'")


# ---------------------------------------------------------------------------- overview
def overview(product: str | None = None, scope: str = "market") -> dict:
    product = resolve_product(product)
    df, scope_label = cohort(scope, product)
    n = len(df)
    cov = coverage()
    stages = [{"key": k, "label": LABEL[k], "short": SHORT[k], "definition": d,
               "patients": int(df[f"d_{k}"].notna().sum()), "pct": float(df[f"d_{k}"].notna().mean()),
               "median_days_from_dx": _med((df[f"d_{k}"] - df["d_diagnosis"]).dt.days)} for k, _, d in STAGES]
    trans = []
    for a, b in zip(KEYS, KEYS[1:]):
        t = _transition_counts(df, a, b)
        trans.append({"a": a, "b": b, "label": f"{SHORT[a]} → {SHORT[b]}", **t})
    trans.append({"a": "diagnosis", "b": "treatment", "label": "Diagnosis → Treatment", "milestone": True,
                  **_transition_counts(df, "diagnosis", "treatment")})
    status = [{"status": s, "patients": int((df["status"] == s).sum()), "pct": float((df["status"] == s).mean()),
               "definition": STATUS_DEF[s]} for s in STATUSES]
    sim = [{"band": b, "patients": int((df["similarity_band"] == b).sum()),
            "pct": float((df["similarity_band"] == b).mean())} for b in ("High", "Moderate", "Low")]
    treated = df[df["d_treatment"].notna()]
    judged = df[df["followup_days"] >= STALL_DAYS]
    kpis = {"patients": n, "treated": int(len(treated)),
            "median_dx_to_tx": _med(treated["dx_to_tx"].clip(lower=0)),
            "treated_within_60": float((judged["dx_to_tx"].between(-ORDER_TOL, STALL_DAYS)).mean()) if len(judged) else None,
            "biomarker_before_tx": float(treated["biomarker_before_tx"].mean()) if len(treated) else None,
            "mean_similarity": float(df["similarity"].mean()),
            "modality": treated["modality"].value_counts().to_dict()}
    biggest = max(trans[:-1], key=lambda t: t["not_reached"] / max(t["from"], 1))
    return json_safe({
        "context": {"indication": config.INDICATION_NAME, "product": product, "scope": scope or "market",
                    "scope_label": scope_label, "small": n < SMALL_N,
                    "cohort": f"Newly diagnosed: first lung-cancer diagnosis from "
                              f"{(pd.Timestamp(cov['data_start']) + pd.Timedelta(days=CLEAN_DAYS)).date()} to "
                              f"{cov['data_end']}", "patients": n, "data_end": cov["data_end"]},
        "stages": stages, "transitions": trans, "status": status, "similarity": sim, "kpis": kpis,
        "patterns": patterns(df), "sequences": _sequences(df, len(df), 10), "biggest_drop": biggest,
        "brand": brand_view(product),
        "rules": {"stall_days": STALL_DAYS, "delay_days": DELAY_DAYS, "order_tolerance": ORDER_TOL,
                  "clean_days": CLEAN_DAYS, "pre_window": PRE_WINDOW},
    })


def _transition_counts(df: pd.DataFrame, a: str, b: str) -> dict:
    has_a = df[df[f"d_{a}"].notna()]
    reached = has_a[f"d_{b}"].notna()
    # "not reached" only when there was enough time after stage A to observe stage B
    enough = (pd.Timestamp(coverage()["data_end"]) - has_a[f"d_{a}"]).dt.days >= STALL_DAYS
    days = (has_a[f"d_{b}"] - has_a[f"d_{a}"]).dt.days
    return {"from": int(len(has_a)), "reached": int(reached.sum()), "not_reached": int((~reached & enough).sum()),
            "too_early": int((~reached & ~enough).sum()), "median_days": _med(days[reached].clip(lower=0)),
            "pct_reached": float(reached.mean()) if len(has_a) else None}


ARCHETYPES = {
    "Complete reference pathway": "All seven stages observed, in reference order.",
    "Treated after tissue diagnosis": "Tissue diagnosis, biomarker test and treatment observed (some stages "
                                      "missing or in a different order).",
    "Treated without an observed biomarker test": "Treatment started, but no biomarker test is observed.",
    "Treated, limited work-up observed": "Treatment started after a biomarker test, without an observed tissue "
                                         "diagnosis.",
    "Alternative order": f"Treatment observed before the diagnosis or tissue-diagnosis claim (by more than {ORDER_TOL} "
                         "days).",
    "Diagnostic delay": f"More than {DIAG_DELAY_DAYS} days from the first suspicion or imaging claim to the "
                        "lung-cancer diagnosis.",
    "Diagnosis without treatment": f"Diagnosed, with no treatment observed {STALL_DAYS}+ days later.",
    "Too early to tell": f"Diagnosed less than {STALL_DAYS} days before the data end, not yet treated.",
}


def _sequences(g: pd.DataFrame, n: int, top: int) -> list[dict]:
    sig = g["sequence"].apply(lambda s: "|".join(s))
    out = []
    for key, h in g.groupby(sig):
        seq = key.split("|") if key else []
        out.append({"signature": key, "sequence": seq, "patients": int(len(h)), "pct": len(h) / n,
                    "in_order_stages": _lcs_items(seq, KEYS), "similarity": _lcs(seq, KEYS) / len(KEYS),
                    "missing": [LABEL[k] for k in KEYS if k not in seq],
                    "name": h["archetype"].mode().iloc[0]})
    return sorted(out, key=lambda p: -p["patients"])[:top]


def patterns(df: pd.DataFrame) -> list[dict]:
    """Journey archetypes (every patient in exactly one), each with its most common stage sequences."""
    out = []
    for name, desc in ARCHETYPES.items():
        g = df[df["archetype"] == name]
        if g.empty:
            continue
        out.append({"name": name, "description": desc, "patients": int(len(g)), "pct": len(g) / len(df),
                    "median_dx_to_tx": _med(g["dx_to_tx"].clip(lower=0)),
                    "median_workup_days": _med(g["workup_days"]),
                    "biomarker": float(g["d_biomarker"].notna().mean()),
                    "status_mix": g["status"].value_counts().to_dict(),
                    "sequences": _sequences(g, len(g), 3),
                    "patient_ids": g["patient_id"].astype(str).head(20000).tolist()})
    return out


# ---------------------------------------------------------------------------- the brand within the journey
def brand_view(product: str | None = None) -> dict:
    """Where the brand is chosen: time to brand start and its share of first-line starts by journey characteristics.
    Denominator: newly diagnosed patients with a first line of systemic therapy."""
    p = resolve_product(product)
    df = patient_stages()
    sys = df[df["products"].apply(len) > 0].copy()
    sys["has"] = sys["products"].apply(lambda xs: p in xs)
    bp = sys[sys["has"]]
    starts = query("select patient_id, min(service_date) d from fact_drug_event where product = $p group by 1",
                   {"p": p}).set_index("patient_id")["d"]
    days = (pd.to_datetime(bp["patient_id"].map(starts)) - bp["d_diagnosis"]).dt.days.clip(lower=0)
    rows = []

    def add(dim, name, g):
        if len(g):
            rows.append({"dimension": dim, "group": name, "patients": int(len(g)), "brand_patients": int(g["has"].sum()),
                         "share": float(g["has"].mean())})
    bm = sys["biomarker_before_tx"]
    add("Biomarker testing", "Biomarker test before treatment", sys[bm])
    add("Biomarker testing", "No biomarker test observed", sys[~bm])
    for st in ("On pathway", "Delayed", "Deviated"):
        add("Treatment timing", st, sys[sys["status"] == st])
    for name in ARCHETYPES:
        g = sys[sys["archetype"] == name]
        if len(g) >= 20:
            add("Journey pattern", name, g)
    for pay in ("Medicare", "Commercial", "Medicaid"):
        add("Payer", pay, sys[sys["payer_group"] == pay])
    bins = [(-1, 7, "0-7 days"), (7, 14, "8-14 days"), (14, 30, "15-30 days"), (30, 60, "31-60 days"),
            (60, 10_000, "60+ days")]
    dist = [{"bucket": lbl, "patients": int(((days > lo) & (days <= hi)).sum())} for lo, hi, lbl in bins]
    capture = float(sys["has"].mean()) if len(sys) else None
    ins = [f"{len(bp):,} of {len(sys):,} newly diagnosed patients who started systemic therapy ({pct(capture or 0)}) "
           f"have {p} in their first line."]
    if len(bp):
        ins.append(f"Median time from diagnosis to first {p} claim: {_med(days):.0f} days; "
                   f"{pct(float((days <= 30).mean()))} start within 30 days.")
    bmr = [r for r in rows if r["dimension"] == "Biomarker testing"]
    if len(bmr) == 2:
        ins.append(f"{p} share of first-line starts: {pct(bmr[0]['share'])} after a biomarker test vs "
                   f"{pct(bmr[1]['share'])} without one (an association, not a cause).")
    return {"product": p, "brand_patients": int(len(bp)), "systemic_treated": int(len(sys)), "capture": capture,
            "median_dx_to_brand": _med(days), "within30": float((days <= 30).mean()) if len(bp) else None,
            "distribution": dist, "rows": rows, "insights": ins, "small": len(bp) < SMALL_N}


# ---------------------------------------------------------------------------- transition drill-down
def transition(a: str, b: str, product: str | None = None, scope: str = "market") -> dict:
    if a not in KEYS or b not in KEYS or a == b:
        raise ValueError(f"transition must be two different stages from {KEYS}")
    product = resolve_product(product)
    df, _ = cohort(scope, product)
    end = pd.Timestamp(coverage()["data_end"])
    g = df[df[f"d_{a}"].notna()].copy()
    g["days"] = (g[f"d_{b}"] - g[f"d_{a}"]).dt.days
    g["enough"] = (end - g[f"d_{a}"]).dt.days >= STALL_DAYS
    g["outcome"] = np.select([g[f"d_{b}"].notna(), g["enough"]], ["Reached", "Not reached"], "Too early to tell")
    reached, missed = g[g.outcome == "Reached"], g[g.outcome == "Not reached"]
    bins = [(-10_000, 7, "0-7 days"), (7, 14, "8-14 days"), (14, 30, "15-30 days"), (30, 60, "31-60 days"),
            (60, 10_000, "60+ days")]
    d = reached["days"].clip(lower=0)
    dist = [{"bucket": lbl, "patients": int(((d > lo) & (d <= hi)).sum()) if lo > -10_000 else int((d <= hi).sum())}
            for lo, hi, lbl in bins]
    for x in dist:
        x["pct"] = x["patients"] / max(len(reached), 1)

    def profile(s: pd.DataFrame) -> dict:
        return {"patients": len(s), "mean_age": _med(s["age"]), "pct_65plus": float((s["age"].dropna() >= 65).mean()) if s["age"].notna().any() else None,
                "medicare": float((s["payer_group"] == "Medicare").mean()) if len(s) else None,
                "commercial": float((s["payer_group"] == "Commercial").mean()) if len(s) else None,
                "medicaid": float((s["payer_group"] == "Medicaid").mean()) if len(s) else None,
                "biomarker": float(s["d_biomarker"].notna().mean()) if len(s) else None,
                "similarity": float(s["similarity"].mean()) if len(s) else None}
    pa, pb = profile(reached), profile(missed)
    rows = [("Patients", "patients", "int"), ("Median age", "mean_age", "num"), ("Aged 65+", "pct_65plus", "pct"),
            ("Medicare (all forms)", "medicare", "pct"), ("Commercial", "commercial", "pct"),
            ("Medicaid", "medicaid", "pct")]
    if "biomarker" not in (a, b):   # similarity / the stage itself would be circular here
        rows.append(("Biomarker test observed", "biomarker", "pct"))
    comparison = [compare._row("Profile", lbl, pa[k], pb[k], kind, None) for lbl, k, kind in rows]
    # associated HCPs: the HCP on the stage-A event (e.g. the diagnosing HCP)
    h = g[g[f"h_{a}"].notna()].groupby(f"h_{a}").agg(
        patients=("patient_id", "count"), not_reached=("outcome", lambda s: int((s == "Not reached").sum())),
        median_days=("days", lambda s: _med(s.clip(lower=0)))).reset_index().rename(columns={f"h_{a}": "hcp_id"})
    h = h[h.patients >= 3]
    h["pct_not_reached"] = h.not_reached / h.patients
    ht = compare._hcp_table(product)
    ht["brand_share"] = ht.p_volume / ht.market_volume
    h = h.merge(ht[["hcp_id", "market_volume", "brand_share"]], on="hcp_id", how="left")
    h = h.sort_values(["not_reached", "patients"], ascending=False).head(25)
    ta, tb = LABEL[a], LABEL[b]
    ins = [f"{len(g):,} patients reached {ta.lower()}; {len(reached):,} ({pct(len(reached) / max(len(g), 1))}) have an "
           f"observed {tb.lower()}, {len(missed):,} do not after {STALL_DAYS}+ days of follow-up, and "
           f"{int((g.outcome == 'Too early to tell').sum()):,} are too recent to tell."]
    if len(reached):
        ins.append(f"Median {ta.lower()} → {tb.lower()}: {_med(d):.0f} days; "
                   f"{pct(float((d > 30).mean()))} take longer than 30 days.")
    big = max(comparison[1:], key=lambda r: abs(r["diff"] or 0) if r["kind"] == "pct" else 0)
    if big["diff"] is not None:
        ins.append(f"Largest profile difference: {big['metric'].lower()} ({big['a_fmt']} reached vs {big['b_fmt']} "
                   f"not reached). This is an observed association, not a cause.")
    return json_safe({"a": a, "b": b, "label": f"{ta} → {tb}", "product": product, "from": len(g),
                      "reached": len(reached), "not_reached": len(missed),
                      "too_early": int((g.outcome == "Too early to tell").sum()), "median_days": _med(d),
                      "distribution": dist, "comparison": comparison, "hcps": h.to_dict("records"),
                      "insights": ins,
                      "reached_ids": reached["patient_id"].astype(str).head(5000).tolist(),
                      "not_reached_ids": missed["patient_id"].astype(str).head(5000).tolist(),
                      "hcp_ids": h["hcp_id"].tolist()})


# ---------------------------------------------------------------------------- one patient
def patient(pid: str) -> dict:
    df = patient_stages()
    r = df[df["patient_id"].astype(str) == str(pid)]
    if r.empty:
        raise ValueError(f"patient {pid} is not in the newly diagnosed cohort")
    r = r.iloc[0]
    dx = r["d_diagnosis"]
    in_order = set(_lcs_items(r["sequence"], KEYS))
    stages = [{"key": k, "label": LABEL[k], "month": None if pd.isna(r[f"d_{k}"]) else r[f"d_{k}"].strftime("%b %Y"),
               "days_from_dx": None if pd.isna(r[f"d_{k}"]) else int((r[f"d_{k}"] - dx).days),
               "in_order": (k in in_order) if not pd.isna(r[f"d_{k}"]) else None}
              for k in KEYS]
    stage_of = {c: k for k, codes in (("imaging", IMAGING), ("consult", CONSULT), ("tissue", TISSUE),
                                      ("biomarker", BIOMARKER), ("treatment", SURGERY + RADIATION)) for c in codes}
    tl = query(f"""
        select service_date as d, 'treatment' as st, product as label from fact_drug_event
          where patient_id = $p and therapy_group = 'LUNG_SYSTEMIC'
        union select service_date, case when dx_code like 'C34%' then 'diagnosis' else 'suspicion' end,
                     dx_code || coalesce(' ' || dx_desc, '') from fact_dx_event
          where patient_id = $p and (dx_code like 'C34%' or dx_code like 'R91%')
        union select service_date, px_code, px_code from fact_px_event where patient_id = $p and px_code in
          {_codes(IMAGING + CONSULT + TISSUE + BIOMARKER + SURGERY + RADIATION)}
        order by 1""", {"p": r["patient_id"]})
    tl["st"] = tl["st"].map(lambda v: stage_of.get(v, v))
    tl["label"] = [f"{'Surgery' if c in SURGERY else 'Radiation' if c in RADIATION else LABEL[st]} (CPT {c})"
                   if c in stage_of else lab for c, st, lab in zip(tl["label"], tl["st"], tl["label"])]
    tl = tl.drop_duplicates(["d", "st", "label"])
    tl["d"] = pd.to_datetime(tl["d"])
    tl = tl[tl["d"] >= dx - pd.Timedelta(days=PRE_WINDOW)].head(120)
    days = None if pd.isna(r["dx_to_tx"]) else int(r["dx_to_tx"])
    reasons = {"Deviated": lambda: f"{r['deviation']} ({abs(days)} days before diagnosis)" if days < 0 else r["deviation"],
               "Delayed": lambda: f"Treatment started {days} days after diagnosis (reference: within {DELAY_DAYS})",
               "Stalled": lambda: f"No treatment observed {_days(r['followup_days'])} after diagnosis; last observed "
                                  f"stage: {r['last_stage']}",
               "In progress": lambda: f"Diagnosed {_days(r['followup_days'])} before the data end: too early to judge",
               "On pathway": lambda: f"Treatment started {max(days, 0)} days after diagnosis"}
    return json_safe({"patient_id": str(r["patient_id"]), "diagnosis_month": dx.strftime("%b %Y"),
                      "age_group": r["age_group"], "payer": r["payer_group"],
                      "status": r["status"], "reason": reasons[r["status"]](), "similarity": r["similarity"],
                      "similarity_band": r["similarity_band"], "regimen": r["regimen"], "modality": r["modality"],
                      "stages": stages, "sequence": [LABEL[k] for k in r["sequence"]],
                      "timeline": [{"month": pd.Timestamp(x.d).strftime("%b %Y"), "stage": x.st, "label": x.label,
                                    "days_from_dx": int((pd.Timestamp(x.d) - dx).days)} for x in tl.itertuples()]})


def patients(filter_spec: str = "all", limit: int = 300, scope: str = "market", product: str | None = None) -> dict:
    df, scope_label = cohort(scope, product)
    if filter_spec.startswith("pattern:"):
        sig = filter_spec.split(":", 1)[1]
        s, label = df[df["sequence"].apply(lambda q: "|".join(q)) == sig], "Journey pattern"
    elif filter_spec.startswith("archetype:"):
        name = filter_spec.split(":", 1)[1]
        if name not in ARCHETYPES:
            raise ValueError(f"unknown journey pattern '{name}'")
        s, label = df[df["archetype"] == name], name
    else:
        s, label = _select(df, filter_spec)
    if scope == "brand":
        label = scope_label if filter_spec == "all" else f"{label} · {scope_label}"
    cols = s.assign(sequence=s["sequence"].apply(lambda q: " → ".join(SHORT[k] for k in q)),
                    similarity=s["similarity"].round(3), diagnosis=s["d_diagnosis"].dt.strftime("%b %Y"))
    rows = cols[["patient_id", "status", "diagnosis", "dx_to_tx", "similarity", "sequence", "age_group",
                 "payer_group"]].head(limit)
    return json_safe({"label": label, "count": len(s), "rows": rows.to_dict("records"),
                      "patient_ids": s["patient_id"].astype(str).head(20000).tolist()})


# ---------------------------------------------------------------------------- compare two journey cohorts
def compare_journeys(a: str, b: str, product: str | None = None) -> dict:
    product = resolve_product(product)
    df = patient_stages()
    (sa, la), (sb, lb) = _select(df, a), _select(df, b)
    if la == lb:
        raise ValueError("pick two different cohorts")

    def m(s):
        t = s[s["d_treatment"].notna()]
        j = s[s["followup_days"] >= STALL_DAYS]
        return {"n": len(s), "dx_tx": _med(t["dx_to_tx"].clip(lower=0)),
                "treated": float(s["d_treatment"].notna().mean()) if len(s) else None,
                "within60": float(j["dx_to_tx"].between(-ORDER_TOL, STALL_DAYS).mean()) if len(j) else None,
                "biomarker": float(t["biomarker_before_tx"].mean()) if len(t) else None,
                "sim": float(s["similarity"].mean()) if len(s) else None,
                **{f"st_{x}": float((s["status"] == x).mean()) if len(s) else None for x in STATUSES},
                **{f"stage_{k}": float(s[f"d_{k}"].notna().mean()) if len(s) else None for k in KEYS}}
    ma, mb = m(sa), m(sb)
    R = compare._row
    rows = [R("Journey", "Patients", ma["n"], mb["n"], "int", None),
            R("Journey", "Median diagnosis → treatment", ma["dx_tx"], mb["dx_tx"], "days", False),
            R("Journey", "Treatment observed", ma["treated"], mb["treated"], "pct"),
            R("Journey", f"Treated within {STALL_DAYS} days (patients with enough follow-up)", ma["within60"],
              mb["within60"], "pct"),
            R("Journey", "Biomarker test before treatment", ma["biomarker"], mb["biomarker"], "pct"),
            R("Journey", "Mean similarity to reference pathway", ma["sim"], mb["sim"], "pct")]
    rows += [R("Journey status", s, ma[f"st_{s}"], mb[f"st_{s}"], "pct", s in ("On pathway",)) for s in STATUSES]
    rows += [R("Stage reached", LABEL[k], ma[f"stage_{k}"], mb[f"stage_{k}"], "pct") for k in KEYS]
    # skip rows that define the cohorts themselves (0% or 100% on one side, e.g. "treatment observed" for treated)
    big = max((r for r in rows[1:] if r["diff"] is not None and r["kind"] in ("pct", "days")
               and not (r["kind"] == "pct" and ({r["a"], r["b"]} & {0.0, 1.0}))),
              key=lambda r: abs(r["diff"]) / (1 if r["kind"] == "pct" else 3), default=None)
    ins = [f"{la}: {ma['n']:,} patients; {lb}: {mb['n']:,} patients."]
    if big:
        what = f"patients reaching {big['metric'].lower()}" if big["section"] == "Stage reached" else \
            f"{big['section'].lower()} {big['metric'].lower()}" if big["section"] == "Journey status" else big["metric"].lower()
        ins.append(f"Largest difference: {what} ({big['a_fmt']} vs {big['b_fmt']}, {big['diff_fmt']}).")
    return json_safe({"a": la, "b": lb, "rows": rows, "insights": ins,
                      "a_ids": sa["patient_id"].astype(str).head(20000).tolist(),
                      "b_ids": sb["patient_id"].astype(str).head(20000).tolist()})


def _table(rows: list[dict], a: str, b: str) -> pd.DataFrame:
    """Scorecard rows as a readable table (formatted values, sides named)."""
    return pd.DataFrame([{"section": r["section"], "metric": r["metric"], a: r["a_fmt"], b: r["b_fmt"],
                          "difference": r["diff_fmt"]} for r in rows])


# ---------------------------------------------------------------------------- agent tool
def patient_journey(view: str = "overview", a: str | None = None, b: str | None = None,
                    product: str | None = None, scope: str = "market"):
    """Chat tool: journey overview, a transition drill-down, or a comparison of two journey cohorts."""
    from ciq.tools.base import ToolResult, kpi
    product = resolve_product(product)
    if view == "transition":
        t = transition(a or "diagnosis", b or "treatment", product, scope)
        dist = pd.DataFrame(t["distribution"])
        return ToolResult(
            tool="patient_journey", title=f"Patient journey: {t['label']}",
            subtitle="Newly diagnosed NSCLC patients; how many move on to the next stage and how fast",
            params={"view": view, "a": t["a"], "b": t["b"], "product": product, "scope": scope},
            data={"time_to_transition": dist, "reached_vs_not": _table(t["comparison"], "Reached", "Not reached"),
                  "associated_hcps": pd.DataFrame(t["hcps"])},
            charts=[charts.bar(dist, "bucket", "patients", f"Time from {LABEL[t['a']].lower()} to {LABEL[t['b']].lower()}",
                               sort=[x["bucket"] for x in t["distribution"]], x_title="", y_title="Patients")],
            kpis=[kpi("Reached", t["reached"], note=f"of {t['from']:,}"), kpi("Not reached", t["not_reached"], lead=True,
                  note=f"{STALL_DAYS}+ days of follow-up"), kpi("Median days", t["median_days"], "num"),
                  kpi("Too early to tell", t["too_early"])],
            insights=t["insights"], observations=t["insights"][1:],
            caveats=[STATUS_DEF["Stalled"], "Associations between patient profile and outcome are observational."])
    if view == "compare":
        c = compare_journeys(a or "all", b or f"brand:{product}", product)
        score = _table(c["rows"], c["a"], c["b"])
        return ToolResult(
            tool="patient_journey", title=f"Patient journeys: {c['a']} vs {c['b']}",
            subtitle="Same newly diagnosed cohort and pathway definitions; difference = B minus A",
            params={"view": view, "a": a, "b": b, "product": product}, data={"scorecard": score},
            insights=c["insights"], caveats=[STATUS_DEF[s] for s in STATUSES])
    if view == "brand":
        bv = brand_view(product)
        rows = pd.DataFrame(bv["rows"])
        dist = pd.DataFrame(bv["distribution"])
        return ToolResult(
            tool="patient_journey", title=f"{bv['product']} in the patient journey",
            subtitle=f"Newly diagnosed patients who started systemic therapy: where {bv['product']} is chosen",
            params={"view": view, "product": product},
            data={"brand_share_by_group": rows, "time_to_brand_start": dist},
            charts=[charts.bar(rows.assign(label=rows["dimension"] + ": " + rows["group"]), "label", "share",
                               f"{bv['product']} share of first-line starts", horizontal=True, y_format=".0%",
                               sort=list(rows["dimension"] + ": " + rows["group"]), x_title="", y_title="Share"),
                    charts.bar(dist, "bucket", "patients", f"Days from diagnosis to first {bv['product']} claim",
                               sort=[x["bucket"] for x in bv["distribution"]], x_title="", y_title="Patients")],
            kpis=[kpi(f"{bv['product']} patients", bv["brand_patients"], note=f"of {bv['systemic_treated']:,} treated"),
                  kpi("Share of first-line starts", bv["capture"], "pct", lead=True),
                  kpi("Median diagnosis → brand start", bv["median_dx_to_brand"], "days"),
                  kpi("Started within 30 days", bv["within30"], "pct")],
            insights=bv["insights"], observations=bv["insights"][1:],
            caveats=["Share = patients whose first line includes the brand / newly diagnosed patients with a first "
                     "line of systemic therapy.", "Differences between groups are associations, not causes."]
                    + (["Fewer than 100 brand patients: small-sample percentages move a lot."] if bv["small"] else []))
    o = overview(product, scope)
    st = pd.DataFrame(o["stages"])
    status = pd.DataFrame(o["status"])
    trans = pd.DataFrame(o["transitions"])
    k = o["kpis"]
    worst = o["biggest_drop"]
    ins = [f"{k['patients']:,} newly diagnosed patients; {k['treated']:,} ({pct(k['treated'] / k['patients'])}) have an "
           f"observed treatment start, median {k['median_dx_to_tx']:.0f} days after diagnosis.",
           "Status: " + ", ".join(f"{r.status} {pct(r.pct)}" for r in status.itertuples()) + ".",
           f"Largest drop-off between consecutive stages: {worst['label']} ({worst['not_reached']:,} of "
           f"{worst['from']:,} not observed moving on).",
           f"Biomarker test observed before treatment for {pct(k['biomarker_before_tx'] or 0)} of treated patients."]
    return ToolResult(
        tool="patient_journey", title="Patient journey: " + (o["context"]["scope_label"] if scope == "brand"
                                                             else "newly diagnosed NSCLC"),
        subtitle="Observed pathway vs the reference: Suspicion → Imaging → Consult → Tissue → Diagnosis → Biomarker → Treatment",
        params={"view": "overview", "product": product, "scope": scope},
        data={"stages": st, "status": status, "transitions": trans.drop(columns=["milestone"], errors="ignore")},
        charts=[charts.bar(st.assign(stage=st["label"]), "stage", "patients", "Patients reaching each pathway stage",
                           sort=list(st["label"]), x_title="", y_title="Patients", horizontal=True),
                charts.donut(status, "status", "patients", "Journey status")],
        kpis=[kpi("Newly diagnosed", k["patients"]), kpi("Treatment observed", k["treated"] / k["patients"], "pct", lead=True),
              kpi("Median diagnosis → treatment", k["median_dx_to_tx"], "days"),
              kpi("Biomarker before treatment", k["biomarker_before_tx"], "pct")],
        insights=ins, observations=ins[1:],
        caveats=[o["context"]["cohort"] + ".", *[f"{s}: {d}" for s, d in STATUS_DEF.items()],
                 "Specialist consult is a proxy (new-patient visit codes); specialty is not in the data."])
