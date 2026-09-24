"""Analytics tools: one function per business question.

Each tool calls a stored function (fn_* macro in the warehouse), then adds a
chart spec, headline insights and data caveats, and returns a ToolResult.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

from ciq import config
from ciq.tools import charts, customize
from ciq.tools.base import ToolResult, call_fn, coverage, kpi, pct, query, resolve_product

LINE_BUCKET_NOTE = ("Lines of therapy are derived from observed claims only (regimen window "
                    f"{config.LOT_REGIMEN_WINDOW_DAYS}d, discontinuation gap {config.LOT_GAP_DAYS}d); "
                    "line 1 = first line observed in the data window, which may not be the patient's true first line.")


def _window_caveat(requested_days: int, available: pd.Series, direction: str) -> list[str]:
    """Explain how much of a requested look-back/forward window the data can actually support."""
    if available.empty:
        return []
    full = (available >= requested_days).mean()
    med = int(available.median())
    cov = coverage()
    if full >= 0.99:
        return []
    return [f"Requested {direction} window is {requested_days} days but data covers only "
            f"{cov['data_start']} to {cov['data_end']} ({cov['months']} months): median {direction} available is "
            f"{med} days and only {pct(full)} of patients have the full window. Results are truncated accordingly."]


def _cohort(product: str) -> pd.DataFrame:
    return call_fn("fn_cohort_patients", product)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
def list_products(therapy_group: str | None = None) -> ToolResult:
    df = call_fn("fn_product_catalog", therapy_group=therapy_group)
    top = df.head(20)
    return ToolResult(
        tool="list_products", title="Products in the data", params={"therapy_group": therapy_group}, data=df,
        charts=[charts.bar(top, "product", "patients", "Patients by product (top 20)", horizontal=True,
                           x_title="", y_title="Patients")],
        insights=[f"{len(df)} products found; largest is {df.iloc[0]['product']} ({df.iloc[0]['patients']:,} patients)."],
        caveats=["therapy_group LUNG_SYSTEMIC = lung-cancer systemic therapies (define the market and lines of therapy); "
                 "OTHER_ONCOLOGY = other-tumour agents; SUPPORTIVE = G-CSF, bone agents, ESAs etc."],
    )


def data_overview() -> ToolResult:
    cov = coverage()
    tables = query("""select table_name, estimated_size as rows from duckdb_tables() order by table_name""")
    params = query("select * from ref_build_params")
    pats = query("""select count(*) patients, count(*) filter (where is_treated) treated,
                    count(*) filter (where has_rx) with_pharmacy_claims,
                    count(*) filter (where gender is not null) with_gender from dim_patient""").iloc[0]
    return ToolResult(
        tool="data_overview", title="Data coverage and model", params={},
        data={"tables": tables, "build_params": params},
        insights=[f"{cov['n_patients']:,} lung-cancer patients, {cov['data_start']} to {cov['data_end']} ({cov['months']} months).",
                  f"{int(pats['treated']):,} patients received lung systemic therapy; "
                  f"{int(pats['with_pharmacy_claims']):,} have pharmacy (RX) claims."],
        caveats=["Gender is not present in the source data.",
                 "Payer names are not available; payers are identified by plan id and payment type."],
    )


# ---------------------------------------------------------------------------
# 1. Market overview
# ---------------------------------------------------------------------------
def market_incidence_prevalence(grain: str = "month", washout_days: int = 90) -> ToolResult:
    df = call_fn("fn_incidence_prevalence", grain=grain, washout_days=washout_days)
    cov = coverage()
    period_end = pd.to_datetime(df["period_start"]) + pd.tseries.frequencies.to_offset(
        {"month": "MS", "quarter": "QS", "year": "YS"}.get(grain, "MS"))
    df["is_partial_period"] = (period_end - pd.Timedelta(days=1) > pd.Timestamp(cov["data_end"])) | \
                              (pd.to_datetime(df["period_start"]) < pd.Timestamp(cov["data_start"]))
    long = df.melt(id_vars=["period"], value_vars=["prevalent_patients", "incident_patients"],
                   var_name="measure", value_name="patients")
    long = long.merge(df[["period", "incidence_valid"]], on="period")
    long = long[(long["measure"] == "prevalent_patients") | long["incidence_valid"]]
    long["measure"] = long["measure"].map({"prevalent_patients": "Prevalence", "incident_patients": "Incidence"})
    valid = df[df["incidence_valid"]]
    ins = [f"Prevalent (diagnosed) patients per {grain}: {df['prevalent_patients'].min():,}-{df['prevalent_patients'].max():,}."]
    if len(valid):
        ins.append(f"Incidence (new diagnoses after a {washout_days}-day washout) ranges "
                   f"{valid['incident_patients'].min():,}-{valid['incident_patients'].max():,} per {grain}; "
                   f"trend from {valid.iloc[0]['period']} to {valid.iloc[-1]['period']}: "
                   f"{valid.iloc[0]['incident_patients']:,} -> {valid.iloc[-1]['incident_patients']:,}.")
    if df["is_partial_period"].any():
        ins.append("Partial periods (not full coverage): " + ", ".join(df.loc[df["is_partial_period"], "period"]) + ".")
    return ToolResult(
        tool="market_incidence_prevalence", title=f"Incidence and prevalence by {grain}",
        params={"grain": grain, "washout_days": washout_days}, data=df,
        charts=[charts.line(long, "period", "patients", f"Lung cancer incidence & prevalence by {grain}",
                            color="measure", x_title="", y_title="Patients")],
        insights=ins,
        caveats=[f"Data spans {cov['months']} months ({cov['data_start']} to {cov['data_end']}); yearly trends cannot be "
                 "measured, so the default grain is month.",
                 f"Incidence is only reported once {washout_days} days of look-back exist; early periods are inflated by "
                 "prevalent patients seen for the first time (left-censoring).",
                 "Prevalence = patients with a C34 claim in the period (claims-observed, not population prevalence)."],
    )


def market_share(top_n: int = 10) -> ToolResult:
    df = call_fn("fn_market_share", top_n=top_n)
    return ToolResult(
        tool="market_share", title="Lung systemic therapy market share (patients)", params={"top_n": top_n}, data=df,
        charts=[charts.bar(df, "product", "patient_share", "Share of treated lung-cancer patients by product",
                           horizontal=True, y_format=".0%", x_title="", y_title="Share of treated patients")],
        insights=[f"{r.product}: {pct(r.patient_share)} of treated patients ({r.patients:,})" for r in df.head(3).itertuples()],
        caveats=["Patient share: a patient on a combination counts toward every product in it, so shares sum > 100%."],
    )


def product_trend(product: str | None = None, grain: str = "month") -> ToolResult:
    product = resolve_product(product)
    df = call_fn("fn_product_trend", product, grain=grain)
    return ToolResult(
        tool="product_trend", title=f"{product} patients and share over time", params={"product": product, "grain": grain},
        data=df,
        charts=[charts.bar(df, "period", "product_patients", f"{product} patients by {grain}", sort=None,
                           x_title="", y_title="Patients", tooltip_extra=[("new_patients", "quantitative", None)]),
                charts.line(df, "period", "patient_share", f"{product} share of treated lung patients",
                            y_format=".0%", x_title="", y_title="Patient share")],
        insights=[f"{product} treated {df['product_patients'].iloc[-1]:,} patients in {df['period'].iloc[-1]} "
                  f"({pct(df['patient_share'].iloc[-1])} of the treated market); "
                  f"{int(df['new_patients'].sum()):,} patients started {product} in the window."],
    )


def patient_demographics(product: str | None = None) -> ToolResult:
    product = resolve_product(product)
    ages = call_fn("fn_age_distribution", product)
    coh = _cohort(product)
    known = ages[ages["age_group"] != "Unknown"]
    peak = known.sort_values("patients", ascending=False).iloc[0] if len(known) else None
    pay = coh.groupby("payment_type", as_index=False).size().rename(columns={"size": "patients"})
    ins = [f"{len(coh):,} {product} patients; mean age {coh['age'].mean():.1f}, median {coh['age'].median():.0f}."]
    if peak is not None:
        ins.append(f"Peak age group {peak['age_group']} ({pct(peak['pct_patients'])} of patients).")
    ins.append(f"{pct((coh['age'] >= 65).mean())} are 65+.")
    return ToolResult(
        tool="patient_demographics", title=f"{product} patient demographics", params={"product": product},
        data={"age_distribution": ages, "payment_type": pay.sort_values("patients", ascending=False)},
        charts=[charts.bar(ages, "age_group", "patients", f"{product} patients by age group", sort=list(ages["age_group"]),
                           x_title="Age group", y_title="Patients",
                           tooltip_extra=[("pct_patients", "quantitative", ".1%")]),
                charts.donut(pay, "payment_type", "patients", f"{product} patients by payment type")],
        insights=ins,
        caveats=["Gender is not available in this data source, so gender split cannot be reported.",
                 "Age is the age recorded on medical claims (one value per patient)."],
    )


# ---------------------------------------------------------------------------
# 2. Patient level
# ---------------------------------------------------------------------------
def pre_index_treatments(product: str | None = None, lookback_days: int = 730, level: str = "drug_class",
                         include_supportive: bool = False) -> ToolResult:
    product = resolve_product(product)
    df = call_fn("fn_pre_index_treatments", product, lookback_days=lookback_days, level=level,
                 include_supportive=include_supportive)
    coh = _cohort(product)
    any_prior = query(f"""
        select count(distinct i.patient_id) from fn_product_index(?) i join fact_drug_event f
          on f.patient_id = i.patient_id and f.service_date < i.index_date
         and f.service_date >= i.index_date - cast(? as integer)
        where f.product <> ? and ({str(include_supportive).lower()} or f.therapy_group <> 'SUPPORTIVE')""",
                      [product, lookback_days, product]).iloc[0, 0]
    top = df.head(15)
    ins = [f"{pct(any_prior / len(coh))} of {product} patients ({any_prior:,}/{len(coh):,}) had another treatment "
           f"in the {lookback_days} days before their first {product} claim."]
    if len(df):
        ins.append(f"Most common prior {level.replace('_', ' ')}: {df.iloc[0]['treatment']} ({pct(df.iloc[0]['pct_of_cohort'])}).")
    return ToolResult(
        tool="pre_index_treatments", title=f"Treatments before first {product} ({lookback_days}-day look-back)",
        params={"product": product, "lookback_days": lookback_days, "level": level, "include_supportive": include_supportive},
        data=df,
        charts=[charts.bar(top, "treatment", "patients", f"Treatments in the {lookback_days} days before first {product}",
                           horizontal=True, x_title="", y_title="Patients",
                           tooltip_extra=[("pct_of_cohort", "quantitative", ".1%")])],
        insights=ins,
        caveats=_window_caveat(lookback_days, coh["lookback_days_available"], "look-back"),
    )


def source_of_business(product: str | None = None, lookback_days: int = 180, grace_days: int = 90,
                       level: str = "event", full_lookback_only: bool = False) -> ToolResult:
    product = resolve_product(product)
    df = call_fn("fn_source_of_business", product, lookback_days=lookback_days, grace_days=grace_days,
                 level=level, full_lookback_only=full_lookback_only)
    ins = [f"{r.sob_category}: {pct(r.pct)}" for r in df.itertuples()]
    cav = [f"Definitions: Continuing = {product} within {grace_days} days before; Restart = prior {product} in "
           f"{lookback_days}-day look-back but gap > {grace_days} days; New to Brand + Switch/Add-on = no prior {product} "
           "but another lung therapy in look-back (Add-on if that therapy continues alongside); New to Market = no lung "
           "therapy in look-back."]
    if not full_lookback_only:
        cav.append("Claims early in the data window have a truncated look-back and skew toward 'New to Market'; "
                   "set full_lookback_only=true for a cleaner (smaller) sample.")
    return ToolResult(
        tool="source_of_business", title=f"{product} source of business",
        params={"product": product, "lookback_days": lookback_days, "grace_days": grace_days, "level": level,
                "full_lookback_only": full_lookback_only},
        data=df,
        charts=[charts.bar(df, "sob_category", "pct", f"{product} source of business ({level} level)", horizontal=True,
                           y_format=".1%", x_title="", y_title=f"Share of {level}s",
                           tooltip_extra=[("events", "quantitative", ","), ("patients", "quantitative", ",")])],
        insights=ins, caveats=cav,
    )


def payer_mix(product: str | None = None, top_n: int = 10, level: str = "event") -> ToolResult:
    product = resolve_product(product)
    mix = call_fn("fn_payment_type_mix", product, level=level)
    payers = call_fn("fn_top_payers", product, top_n=top_n)
    payers["payer"] = "Plan " + payers["payer_plan_id"].astype(str) + " (" + payers["payment_type"].fillna("?") + ")"
    medicare = mix[mix["payment_type"].str.contains("Medicare", na=False)]["pct"].sum()
    return ToolResult(
        tool="payer_mix", title=f"{product} payer mix", params={"product": product, "top_n": top_n, "level": level},
        subtitle=f"How {product} claims are paid, by payment type and by payer plan",
        kpis=[kpi(f"{mix.iloc[0]['payment_type']} (largest)", mix.iloc[0]["pct"], "pct", lead=True),
              kpi("Medicare, all forms", medicare, "pct"),
              kpi("Top payer plan", payers.iloc[0]["pct_events"], "pct", note=f"plan {payers.iloc[0]['payer_plan_id']}")],
        observations=[],
        data={"payment_type_mix": mix, "top_payers": payers},
        charts=[charts.donut(mix, "payment_type", "events" if level == "event" else "patients",
                             f"{product} claims by payment type"),
                charts.bar(payers, "payer", "pct_events", f"Top {top_n} payers for {product} (share of claims)",
                           horizontal=True, y_format=".1%", x_title="", y_title="Share of claims",
                           tooltip_extra=[("events", "quantitative", ","), ("patients", "quantitative", ",")])],
        insights=[f"Largest payment type: {mix.iloc[0]['payment_type']} ({pct(mix.iloc[0]['pct'])}).",
                  f"Medicare (all forms incl. Advantage) accounts for {pct(medicare)}.",
                  f"Top payer plan {payers.iloc[0]['payer_plan_id']} holds {pct(payers.iloc[0]['pct_events'])} of claims."],
        caveats=["Payer names are not in the source; payers are shown as plan ids with their dominant payment type.",
                 "Pharmacy claims inherit the patient's most frequent medical payment type."],
    )


# ---------------------------------------------------------------------------
# 3. Therapies / lines
# ---------------------------------------------------------------------------
def combination_therapy(product: str | None = None, top_n: int = 10) -> ToolResult:
    product = resolve_product(product) if product else None
    combo = call_fn("fn_combination_type", product=product)
    combo_agg = combo.groupby("combination_type", as_index=False, sort=False)[["lines", "patients"]].sum()
    combo_agg["pct_lines"] = combo_agg["lines"] / combo_agg["lines"].sum()
    regs = call_fn("fn_regimen_distribution", product=product, top_n=top_n)
    scope = product or "all lung systemic therapy"
    multi = combo_agg.loc[combo_agg["combination_type"] != "Monotherapy", "pct_lines"].sum()
    return ToolResult(
        tool="combination_therapy", title=f"Combination therapy - {scope}", params={"product": product, "top_n": top_n},
        data={"combination_type": combo_agg, "regimens": regs},
        charts=[charts.donut(combo_agg, "combination_type", "lines", f"Regimen type share - {scope}"),
                charts.bar(regs, "regimen", "patients", f"Most common regimens - {scope}", horizontal=True,
                           x_title="", y_title="Patients", tooltip_extra=[("pct_patients", "quantitative", ".1%")])],
        insights=[f"{pct(multi)} of lines are combinations; {pct(1 - multi)} monotherapy.",
                  f"Top regimen: {regs.iloc[0]['regimen']} ({regs.iloc[0]['patients']:,} patients)."],
        caveats=[LINE_BUCKET_NOTE],
    )


def line_progression(product: str | None = None, min_line: int = 3, clean_start_only: bool = False) -> ToolResult:
    product = resolve_product(product) if product else None
    df = call_fn("fn_line_progression", product=product, clean_start_only=clean_start_only)
    cohort = int(df.loc[df["line_number"] == 1, "patients_reaching"].iloc[0]) if len(df) else 0
    later = df.loc[df["line_number"] >= min_line, "patients_ending_here"].sum()
    scope = f"{product} patients" if product else "treated patients"
    df["line"] = "Line " + df["line_number"].astype(str)
    return ToolResult(
        tool="line_progression", title=f"Progression through lines - {scope}",
        params={"product": product, "min_line": min_line, "clean_start_only": clean_start_only}, data=df,
        subtitle=f"How many {scope} move on to each later line of therapy",
        kpis=[kpi(f"Reached line {min_line}+", later / cohort if cohort else 0, "pct", note=f"{later:,} of {cohort:,}",
                  lead=True),
              kpi("Patients in line 1", cohort)],
        observations=[],
        charts=[charts.bar(df, "line", "patients_reaching", f"Patients reaching each line - {scope}", sort=None,
                           x_title="", y_title="Patients", tooltip_extra=[("pct_of_cohort", "quantitative", ".1%")])],
        insights=[f"{pct(later / cohort if cohort else 0)} of {scope} ({later:,}/{cohort:,}) progressed to line {min_line}+.",
                  *[f"Line {r.line_number}: {r.patients_reaching:,} patients ({pct(r.pct_of_cohort)})" for r in df.itertuples()]],
        caveats=[LINE_BUCKET_NOTE, f"With {coverage()['months']} months of data, few patients can be observed through "
                 "multiple lines; later-line progression is understated."],
    )


def product_share_by_line(product: str | None = None, max_line_bucket: int = 4) -> ToolResult:
    product = resolve_product(product)
    df = call_fn("fn_product_share_by_line", product, max_line_bucket=max_line_bucket)
    df["line"] = "Line " + df["line_bucket"]
    return ToolResult(
        tool="product_share_by_line", title=f"{product} patient share by line",
        params={"product": product, "max_line_bucket": max_line_bucket}, data=df,
        subtitle=f"Share of patients in each line of therapy who receive {product}",
        kpis=[kpi(f"Line {r.line_bucket} share", r.product_share, "pct", note=f"{r.product_patients:,} patients",
                  lead=i == 0) for i, r in enumerate(df.itertuples())][:4],
        observations=[] if len(df) <= 4 else None,
        charts=[charts.bar(df, "line", "product_share", f"{product} share of patients in each line", sort=None,
                           y_format=".0%", x_title="", y_title="Patient share",
                           tooltip_extra=[("product_patients", "quantitative", ","), ("line_patients", "quantitative", ",")])],
        insights=[f"Line {r.line_bucket}: {pct(r.product_share)} ({r.product_patients:,} of {r.line_patients:,} patients)"
                  for r in df.itertuples()],
        caveats=[LINE_BUCKET_NOTE],
    )


def treatments_by_line(product: str | None = None, top_n: int = 5, max_line_bucket: int = 4) -> ToolResult:
    product = resolve_product(product) if product else None
    df = call_fn("fn_top_regimens_by_line", top_n=top_n, max_line_bucket=max_line_bucket, product=product)
    df["line"] = "Line " + df["line_bucket"]
    order = list(dict.fromkeys(df.sort_values("line_sort")["line"]))
    scope = f"{product} patients" if product else "all treated patients"
    # colour identity: the 7 biggest regimens overall get fixed slots, everything else folds to "All other regimens"
    top_regs = df.groupby("regimen")["patients"].sum().sort_values(ascending=False).index[:7]
    plot = df.assign(regimen_group=df["regimen"].where(df["regimen"].isin(top_regs), "All other regimens"))
    plot = plot.groupby(["line", "regimen_group"], as_index=False)["pct_of_line"].sum()
    rest = 1 - plot.groupby("line")["pct_of_line"].sum()
    plot = pd.concat([plot, pd.DataFrame({"line": rest.index, "regimen_group": "All other regimens",
                                          "pct_of_line": rest.values})])
    plot = plot.groupby(["line", "regimen_group"], as_index=False)["pct_of_line"].sum()
    return ToolResult(
        tool="treatments_by_line", title=f"Most common treatments by line - {scope}",
        params={"product": product, "top_n": top_n, "max_line_bucket": max_line_bucket}, data=df,
        charts=[charts.stacked_bar(plot, "line", "pct_of_line", "regimen_group",
                                   f"Regimen mix by line - {scope}", x_sort=order, x_title="",
                                   y_title="Share of patients in line", horizontal=True,
                                   neutral_label="All other regimens")],
        insights=[f"{line}: " + ", ".join(f"{r.regimen} ({pct(r.pct_of_line)})" for r in g.head(3).itertuples())
                  for line, g in df.groupby("line", sort=False)],
        caveats=[LINE_BUCKET_NOTE],
    )


# ---------------------------------------------------------------------------
# 4. Diagnosis
# ---------------------------------------------------------------------------
def dx_around_index(product: str | None = None, window_days: int = 540, period: str = "both", top_n: int = 10,
                    code_level: str = "code", exclude_indication: bool = False) -> ToolResult:
    product = resolve_product(product)
    periods = ["pre", "post"] if period == "both" else [period]
    frames, chart_list, ins = {}, [], []
    coh = _cohort(product)
    cav = []
    for p in periods:
        df = call_fn("fn_dx_around_index", product, period=p, window_days=window_days, top_n=top_n,
                     code_level=code_level, exclude_indication=exclude_indication)
        df["label"] = df["code"] + " " + df["description"].fillna("").str.slice(0, 45)
        frames[p] = df
        label = "before" if p == "pre" else "after"
        chart_list.append(charts.bar(df, "label", "pct_of_cohort",
                                     f"Top dx codes {window_days} days {label} first {product}", horizontal=True,
                                     y_format=".0%", x_title="", y_title="Share of patients",
                                     tooltip_extra=[("patients", "quantitative", ",")]))
        if len(df):
            ins.append(f"{label.title()} first {product}: " +
                       ", ".join(f"{r.code} ({pct(r.pct_of_cohort, 0)})" for r in df.head(4).itertuples()))
        avail = coh["lookback_days_available"] if p == "pre" else coh["followup_days_available"]
        cav += _window_caveat(window_days, avail, "look-back" if p == "pre" else "follow-up")
    return ToolResult(
        tool="dx_around_index", title=f"Common diagnoses around first {product}",
        params={"product": product, "window_days": window_days, "period": period, "top_n": top_n,
                "code_level": code_level, "exclude_indication": exclude_indication},
        data=frames, charts=chart_list, insights=ins,
        caveats=cav + ["pct_of_cohort uses all cohort patients as denominator; pct_of_observed uses patients with any "
                       "dx claim in the window."],
    )


# ---------------------------------------------------------------------------
# 5. Adherence
# ---------------------------------------------------------------------------
def _kaplan_meier(durations: np.ndarray, events: np.ndarray, grid: np.ndarray) -> np.ndarray:
    order = np.argsort(durations)
    d, e = durations[order], events[order]
    times = np.unique(d[e == 1])
    surv, s = [], 1.0
    at = {}
    for t in times:
        n_risk = (d >= t).sum()
        n_evt = ((d == t) & (e == 1)).sum()
        s *= 1 - n_evt / n_risk
        at[t] = s
    for g in grid:
        prior = [v for t, v in at.items() if t <= g]
        surv.append(prior[-1] if prior else 1.0)
    return np.array(surv)


def _persistency_curve(product: str, grace_days: int, step_days: int):
    """First-episode Kaplan-Meier curve. Returns (curve df, first-episode df, discontinuation flags)."""
    ep = call_fn("fn_therapy_episodes", product, grace_days=grace_days)
    first = ep[ep["episode_number"] == 1].copy()
    dur = first["duration_days"].to_numpy(dtype=float)
    evt = (~first["censored"].astype(bool)).to_numpy(dtype=int)
    max_follow = int(dur.max()) if len(dur) else 0
    grid = np.arange(0, max_follow + step_days, step_days)
    s = _kaplan_meier(dur, evt, grid)
    at_risk = [(dur >= g).sum() for g in grid]
    curve = pd.DataFrame({"day": grid, "pct_persistent": np.round(s, 4), "patients_at_risk": at_risk})
    return curve[curve["patients_at_risk"] >= 10], first, evt  # suppress unstable tail


def persistency(product: str | None = None, grace_days: int = 60, step_days: int = 30) -> ToolResult:
    product = resolve_product(product)
    curve, first, evt = _persistency_curve(product, grace_days, step_days)
    below = curve[curve["pct_persistent"] <= 0.5]
    median = int(below["day"].iloc[0]) if len(below) else None
    def at(day):
        r = curve[curve["day"] <= day]
        return r["pct_persistent"].iloc[-1] if len(r) else None
    ins = [f"{len(first):,} patients started {product}; {pct(evt.mean())} discontinued within the window "
           f"(gap > {grace_days} days after supply)."]
    for d in (90, 180):
        if at(d) is not None and curve["day"].max() >= d:
            ins.append(f"Persistent at {d} days: {pct(at(d))}.")
    ins.append(f"Median time on therapy: {median} days." if median else
               "Median time on therapy not reached within the observed window.")
    summary = first.agg(patients=("patient_id", "count"), mean_duration=("duration_days", "mean"),
                        median_duration=("duration_days", "median")).T if len(first) else pd.DataFrame()
    return ToolResult(
        tool="persistency", title=f"{product} treatment persistency",
        params={"product": product, "grace_days": grace_days, "step_days": step_days},
        subtitle=f"Share of new {product} patients still on therapy over time",
        kpis=[kpi("Patients started", len(first)),
              *[kpi(f"Persistent at {d} days", at(d), "pct", lead=d == 180)
                for d in (90, 180) if at(d) is not None and curve["day"].max() >= d],
              kpi("Median time on therapy", median, "days") if median else
              kpi("Median time on therapy", "not reached", "text")],
        observations=ins[:1],
        data={"curve": curve, "episodes": first.drop(columns=["episode_number"])},
        charts=[charts.line(curve, "day", "pct_persistent", f"{product} persistency (Kaplan-Meier)", x_type="quantitative",
                            y_format=".0%", x_title="Days since first claim", y_title="Patients persistent", step=True)],
        insights=ins,
        caveats=[f"Episode = consecutive claims where each starts within days-supply + {grace_days} days of the previous "
                 "coverage; infused drugs use a standard cycle length as days supply (see ref_drug).",
                 "Patients still on therapy at data end are censored (Kaplan-Meier), not counted as discontinued.",
                 f"Maximum observable follow-up is ~{coverage()['months']} months."],
        meta={"summary": summary.to_dict() if len(summary) else {}},
    )


# ---------------------------------------------------------------------------
# 6. Physician level
# ---------------------------------------------------------------------------
def hcp_market_concentration(product: str | None = None, window_days: int = 730, method: str = "hcp_count",
                             include_product: bool = True) -> ToolResult:
    product = resolve_product(product)
    dec = call_fn("fn_hcp_market_deciles", product, window_days=window_days, method=method,
                  include_product=include_product)
    hcp = call_fn("fn_hcp_market_volume", product, window_days=window_days, method=method,
                  include_product=include_product)
    stats = []
    for per, g in hcp.groupby("period"):
        share = g["volume"] / g["volume"].sum()
        top10 = g.sort_values("volume", ascending=False).head(max(1, int(np.ceil(len(g) * 0.1))))["volume"].sum() / g["volume"].sum()
        stats.append(dict(period=per, hcps=len(g), volume=int(g["volume"].sum()),
                          top10pct_hcp_share=round(top10, 4), hhi=round(float((share ** 2).sum() * 10000), 1)))
    stats = pd.DataFrame(stats).sort_values("period", ascending=False)
    dec["period"] = pd.Categorical(dec["period"], ["PRE", "POST"])
    ins = [f"{r.period}: {r.hcps:,} treating HCPs; top 10% of HCPs hold {pct(r.top10pct_hcp_share)} of market volume "
           f"(HHI {r.hhi:,.0f})." for r in stats.itertuples()]
    if len(stats) == 2:
        pre, post = stats.set_index("period").loc["PRE"], stats.set_index("period").loc["POST"]
        top_up = post["top10pct_hcp_share"] > pre["top10pct_hcp_share"]
        hhi_up = post["hhi"] > pre["hhi"]
        if top_up == hhi_up:
            ins.append("Market is " + ("more concentrated" if top_up else "more scattered") +
                       f" after first {product} than before (top-decile share and HHI agree).")
        else:
            ins.append(f"Mixed signal: top-10% HCP share {'rises' if top_up else 'falls'} but HHI "
                       f"{'rises' if hhi_up else 'falls'} as the HCP base changes from {int(pre['hcps']):,} to {int(post['hcps']):,}.")
        if pre["volume"] < 200:
            ins.append(f"PRE period is thin ({int(pre['volume'])} administrations) - treat the comparison as directional.")
    return ToolResult(
        tool="hcp_market_concentration", title=f"HCP decile analysis around first {product}",
        subtitle=f"Is lung-therapy volume concentrated in a few HCPs or spread out, before vs after first {product}?",
        kpis=[kpi(f"{r.period}: top-10% HCP volume", r.top10pct_hcp_share, "pct", note=f"{r.hcps:,} HCPs · HHI {r.hhi:,.0f}",
                  lead=r.period == "POST") for r in stats.itertuples()],
        observations=ins[len(stats):],
        params={"product": product, "window_days": window_days, "method": method, "include_product": include_product},
        data={"deciles": dec, "concentration": stats, "hcp_volume": hcp},
        charts=[charts.grouped_bar(dec.astype({"period": str}), "decile", "pct_volume", "period",
                                   f"Market volume by HCP decile, pre vs post first {product}", y_format=".0%",
                                   subtitle="Decile 1 = highest-volume HCPs",
                                   x_title="Decile", y_title="Share of market volume",
                                   group_sort=["PRE", "POST"])],
        insights=ins,
        caveats=["Market = all lung systemic therapy administrations for the product's patients (supportive care excluded); "
                 "HCP = rendering provider (medical) or prescriber (pharmacy).",
                 f"method={method}: 'hcp_count' puts equal numbers of HCPs in each decile; 'volume' makes each decile ~10% "
                 "of volume.",
                 *_window_caveat(window_days, _cohort(product)["lookback_days_available"], "look-back")],
    )


def hcp_product_share_deciles(product: str | None = None, method: str = "volume") -> ToolResult:
    product = resolve_product(product)
    dec = call_fn("fn_hcp_product_share_deciles", product, method=method)
    hcp = call_fn("fn_hcp_product_share", product, method=method).sort_values("product_volume", ascending=False)
    top = dec.iloc[0]
    grp = dec.assign(decile_group=dec["decile"].map(lambda d: f"Decile {d}" if d <= 3 else "Deciles 4-10"))
    grp = grp.groupby("decile_group", as_index=False)["product_volume"].sum()
    grp = grp.set_index("decile_group").loc[[g for g in ["Decile 1", "Decile 2", "Decile 3", "Deciles 4-10"]
                                              if g in set(grp["decile_group"])]].reset_index()
    overall = dec["product_volume"].sum() / dec["market_volume"].sum()
    low = dec.loc[dec["product_share_of_market"].idxmin()]
    tail = dec.loc[dec["decile"] >= 4, "pct_product_volume"].sum()
    return ToolResult(
        tool="hcp_product_share_deciles", title=f"HCP deciles by {product} volume and market share",
        params={"product": product, "method": method}, data={"deciles": dec, "hcps": hcp},
        subtitle=f"HCPs ranked into deciles by {product} volume, with {product}'s share of each decile's lung-therapy volume",
        kpis=[kpi(f"HCPs prescribing {product}", int(dec["hcps"].sum())),
              kpi(f"{product} share in decile 1", top["product_share_of_market"], "pct", lead=True,
                  note="of those HCPs' lung-therapy volume"),
              kpi(f"Of {product} volume comes from decile 1", top["pct_product_volume"], "pct",
                  note=f"{int(top['hcps'])} HCPs"),
              kpi(f"Overall {product} share", overall, "pct", note=f"across its {int(dec['hcps'].sum()):,} HCPs")],
        observations=[f"{product} share falls from {pct(top['product_share_of_market'])} in decile 1 to "
                      f"{pct(low['product_share_of_market'])} in decile {int(low['decile'])}; deciles 4-10 "
                      f"({int(dec.loc[dec['decile'] >= 4, 'hcps'].sum()):,} HCPs) deliver {pct(tail)} of {product} volume."],
        charts=[charts.bar(dec.assign(decile=dec["decile"].astype(str)), "decile", "product_share_of_market",
                           f"{product} share of HCP lung-therapy volume by decile", sort=[str(d) for d in dec["decile"]], y_format=".0%",
                           subtitle=f"Decile 1 = highest {product} volume", highlight=str(dec["decile"].iloc[0]),
                           x_title="Decile", y_title=f"{product} market share",
                           tooltip_extra=[("hcps", "quantitative", None), ("product_volume", "quantitative", ",")]),
                charts.donut(grp, "decile_group", "product_volume", f"{product} volume by HCP decile",
                             subtitle="Share of total volume", keep_order=True, neutral_label="Deciles 4-10")],
        insights=[f"{int(dec['hcps'].sum()):,} HCPs administered/prescribed {product}.",
                  f"Decile 1: {int(top['hcps'])} HCPs deliver {pct(top['pct_product_volume'])} of {product} volume with "
                  f"{pct(top['product_share_of_market'])} share of their lung-therapy volume.",
                  f"Overall {product} share among its HCPs: {pct(overall)}."],
        caveats=["Deciles rank HCPs by product volume (decile 1 = top); market = the HCP's lung systemic therapy volume."],
    )


# ---------------------------------------------------------------------------
# Brand comparison
# ---------------------------------------------------------------------------
COMPARE_SECTIONS = ["scorecard", "trend", "patients", "payer", "lines", "persistency", "prescribers"]


def _section(spec: dict, section: str) -> dict:
    spec["usermeta"] = {"section": section}
    return spec


def compare_products(products: list[str] | None = None, grace_days: int = 60) -> ToolResult:
    """Side-by-side comparison of 2-4 products on the core brand metrics."""
    raw = [p for p in (products or []) if p]
    if len(raw) < 2:
        raise ValueError("Provide at least 2 products to compare.")
    prods = list(dict.fromkeys(resolve_product(p) for p in raw))[:4]
    if len(prods) < 2:
        raise ValueError("Provide at least 2 different products to compare.")
    treated = int(query("select count(*) from dim_patient where is_treated").iloc[0, 0])

    card: dict[str, dict] = {}          # metric -> {product: raw value}
    display: dict[str, dict] = {}       # metric -> {product: formatted}
    meta_rows: list[tuple[str, str, str | None]] = []   # (section, metric, better)

    def put(section, metric, product, value, fmt, better=None):
        if (section, metric, better) not in meta_rows:
            meta_rows.append((section, metric, better))
        card.setdefault(metric, {})[product] = None if value is None or (isinstance(value, float) and np.isnan(value)) else value
        display.setdefault(metric, {})[product] = "—" if card[metric][product] is None else fmt(value)

    f_int = lambda v: f"{int(v):,}"
    f_pct = lambda v: pct(v)
    f_num = lambda v: f"{v:.0f}"
    f_txt = lambda v: str(v)

    trends, ages, payers, lines, combos, curves, lorenz = [], [], [], [], [], [], []
    for p in prods:
        coh = _cohort(p)
        tr = call_fn("fn_product_trend", p).assign(product=p)
        trends.append(tr)
        put("Market", "Patients treated", p, len(coh), f_int, "higher")
        put("Market", "Share of treated lung patients", p, len(coh) / treated, f_pct, "higher")
        put("Market", f"Share in latest month ({tr['period'].iloc[-1]})", p, float(tr["patient_share"].iloc[-1]), f_pct, "higher")
        put("Market", "Administrations / fills", p, int(tr["product_events"].sum()), f_int, "higher")

        put("Patients", "Median age", p, float(coh["age"].median()), f_num)
        put("Patients", "Aged 65+", p, float((coh["age"] >= 65).mean()), f_pct)
        ag = call_fn("fn_age_distribution", p).assign(product=p)
        ages.append(ag[ag["age_group"] != "Unknown"])

        mix = call_fn("fn_payment_type_mix", p)
        put("Payer", "Medicare (all forms)", p, float(mix.loc[mix["payment_type"].str.contains("Medicare", na=False), "pct"].sum()), f_pct)
        put("Payer", "Commercial", p, float(mix.loc[mix["payment_type"] == "Commercial", "pct"].sum()), f_pct)
        put("Payer", "Medicaid", p, float(mix.loc[mix["payment_type"].str.contains("Medicaid", na=False), "pct"].sum()), f_pct)
        payers.append(mix.assign(product=p))

        sbl = call_fn("fn_product_share_by_line", p).assign(product=p)
        lines.append(sbl)
        for lb in ("1", "2"):
            r = sbl[sbl["line_bucket"] == lb]
            put("Lines of therapy", f"Share of line {lb} patients", p, float(r["product_share"].iloc[0]) if len(r) else None, f_pct, "higher")
        ct = call_fn("fn_combination_type", product=p)
        combo = ct.groupby("combination_type", as_index=False, sort=False)["lines"].sum()
        combo["pct"] = combo["lines"] / combo["lines"].sum()
        combos.append(combo.assign(product=p))
        put("Lines of therapy", "Used in combination", p,
            float(combo.loc[combo["combination_type"] != "Monotherapy", "pct"].sum()), f_pct)
        reg = call_fn("fn_regimen_distribution", product=p, top_n=1)
        put("Lines of therapy", "Most common regimen", p, reg["regimen"].iloc[0] if len(reg) else None, f_txt)

        sob = call_fn("fn_source_of_business", p, level="patient")
        ntb = sob.loc[sob["sob_category"].str.startswith("New to Brand"), "pct"].sum()
        put("Lines of therapy", "New starts switching/adding from other therapy", p, float(ntb), f_pct)

        curve, first, evt = _persistency_curve(p, grace_days, 30)
        curves.append(curve.assign(product=p))
        for d in (90, 180):
            r = curve[curve["day"] <= d]
            v = float(r["pct_persistent"].iloc[-1]) if len(r) and curve["day"].max() >= d else None
            put("Persistency", f"Persistent at {d} days", p, v, f_pct, "higher")

        hcp = call_fn("fn_hcp_product_share", p, method="hcp_count")
        put("Prescribers", "HCPs using the product", p, len(hcp), f_int, "higher")
        if len(hcp):
            vol = hcp.sort_values("product_volume", ascending=False)["product_volume"].to_numpy(dtype=float)
            top = vol[: max(1, int(np.ceil(len(vol) * 0.1)))].sum() / vol.sum()
            put("Prescribers", "Volume from top 10% of HCPs", p, float(top), f_pct)
            cum = np.cumsum(vol) / vol.sum()
            pts = [0.0] + [float(cum[max(0, int(np.ceil(len(vol) * q)) - 1)]) for q in np.arange(0.1, 1.01, 0.1)]
            lorenz.append(pd.DataFrame({"pct_hcps": np.round(np.arange(0, 1.01, 0.1), 2), "cum_volume": pts, "product": p}))

    rows = []
    for section, metric, better in meta_rows:
        vals = card[metric]
        leader = None
        if better and all(isinstance(v, (int, float)) for v in vals.values() if v is not None):
            nums = {k: v for k, v in vals.items() if v is not None}
            if len(nums) > 1:
                leader = max(nums, key=nums.get)
        rows.append({"section": section, "metric": metric, "better": better, "leader": leader,
                     **{p: display[metric].get(p, "—") for p in prods}})
    scorecard = pd.DataFrame(rows)
    raw_rows = [{"section": s, "metric": m, **{p: card[m].get(p) for p in prods}} for s, m, _ in meta_rows]
    scorecard_raw = pd.DataFrame(raw_rows)

    trend = pd.concat(trends)
    age = pd.concat(ages)
    pay = pd.concat(payers)
    top_types = pay.groupby("payment_type")["pct"].sum().sort_values(ascending=False).index[:6]
    pay["payment_type"] = pay["payment_type"].where(pay["payment_type"].isin(top_types), "Other")
    pay = pay.groupby(["product", "payment_type"], as_index=False)["pct"].sum()
    line_df = pd.concat(lines)
    line_df["line"] = "Line " + line_df["line_bucket"]
    combo_df = pd.concat(combos)
    curve_df = pd.concat(curves)
    lorenz_df = pd.concat(lorenz) if lorenz else pd.DataFrame(columns=["pct_hcps", "cum_volume", "product"])

    title_prods = " vs ".join(prods)
    ch = [
        _section(charts.line(trend, "period", "patient_share", "Share of treated lung patients by month", color="product",
                             y_format=".0%", x_title="", y_title="Patient share", domain=prods), "trend"),
        _section(charts.grouped_bar(trend, "period", "new_patients", "product", "Patients starting each product by month",
                                    x_title="", y_title="New patients", group_sort=prods, domain=prods), "trend"),
        _section(charts.grouped_bar(age, "age_group", "pct_patients", "product", "Age mix of each product's patients",
                                    y_format=".0%", x_title="Age group", y_title="Share of patients",
                                    x_sort=list(dict.fromkeys(age.sort_values("mean_age")["age_group"])),
                                    group_sort=prods, domain=prods), "patients"),
        _section(charts.grouped_bar(pay, "payment_type", "pct", "product", "Payer mix (share of claims)",
                                    y_format=".0%", x_title="", y_title="Share of claims", x_sort=list(top_types) + ["Other"],
                                    group_sort=prods, domain=prods), "payer"),
        _section(charts.grouped_bar(line_df, "line", "product_share", "product", "Share of patients in each line",
                                    y_format=".0%", x_title="", y_title="Patient share", group_sort=prods, domain=prods), "lines"),
        _section(charts.grouped_bar(combo_df, "combination_type", "pct", "product", "Monotherapy vs combination (share of lines)",
                                    y_format=".0%", x_title="", y_title="Share of lines",
                                    x_sort=["Monotherapy", "Doublet", "Triplet", "Quadruplet+"],
                                    group_sort=prods, domain=prods), "lines"),
        _section(charts.line(curve_df, "day", "pct_persistent", "Persistency (Kaplan-Meier)", color="product",
                             x_type="quantitative", y_format=".0%", x_title="Days since first claim",
                             y_title="Patients persistent", step=True, domain=prods), "persistency"),
        _section(charts.line(lorenz_df, "pct_hcps", "cum_volume", "Prescriber concentration: cumulative volume by top HCPs",
                             color="product", x_type="quantitative", y_format=".0%", x_title="Share of HCPs (ranked by volume)",
                             y_title="Cumulative share of product volume", domain=prods), "prescribers"),
    ]
    for spec in ch:
        if spec["usermeta"]["section"] == "prescribers":  # percent axis on x for the concentration curve
            spec["encoding"]["x"]["axis"] = {"format": ".0%"}
        if spec["usermeta"]["section"] == "payer":        # long payment-type labels
            spec["encoding"]["x"]["axis"] = {"labelAngle": -30, "labelAlign": "right"}

    leaders = scorecard[scorecard["leader"].notna()]
    ins = [f"{r['metric']}: {r['leader']} leads (" + ", ".join(f"{p} {r[p]}" for p in prods) + ")"
           for _, r in leaders.iterrows()
           if r["metric"].startswith(("Patients treated", "Share of line 1", "Persistent at 90"))]
    return ToolResult(
        tool="compare_products", title=f"Brand comparison: {title_prods}",
        params={"products": prods, "grace_days": grace_days},
        data={"scorecard": scorecard, "scorecard_raw": scorecard_raw, "share_trend": trend,
              "payer_mix": pay, "share_by_line": line_df, "persistency": curve_df},
        charts=ch, insights=ins,
        caveats=[LINE_BUCKET_NOTE,
                 "Shares are patient-based; a patient on a combination counts for each product in it.",
                 f"Persistency uses a {grace_days}-day grace period; tails with < 10 patients at risk are hidden.",
                 f"Data covers {coverage()['months']} months, so later lines and long-term persistency are truncated."],
        meta={"sections": COMPARE_SECTIONS},
    )


# ---------------------------------------------------------------------------
# Ad-hoc SQL (read-only)
# ---------------------------------------------------------------------------
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|export|import|install|load|"
                        r"pragma|set|call|checkpoint|vacuum)\b", re.I)


CHART_TYPES = ("bar", "line", "stacked_bar", "grouped_bar", "donut", "scatter", "heatmap")
_FORMATS = {"percent": ".0%", "percent1": ".1%", "integer": ",.0f", "number": ",.2~f"}


def _natural_order(values) -> list:
    """Category order for axes: numeric-aware ('<18', '20-29', ... '80+'), with Unknown/Other/None last."""
    def key(v):
        t = str(v)
        if t.strip().lower() in ("unknown", "other", "none", "null", "", "nan"):
            return (2, 0.0, t)
        m = re.search(r"-?\d+(\.\d+)?", t)
        return (0, float(m.group()) - (1e9 if t.lstrip().startswith("<") else 0), t) if m else (1, 0.0, t)
    return sorted(dict.fromkeys(str(v) for v in values), key=key)


def _custom_chart(df: pd.DataFrame, chart: dict) -> tuple[dict | None, str | None]:
    """Build a chart the model asked for from a query result. Returns (spec, None) or (None, reason).

    Only columns present in the result can be plotted, so every chart is drawn from data the query really returned.
    """
    if not isinstance(chart, dict):
        return None, "chart must be an object"
    kind = chart.get("type")
    if kind not in CHART_TYPES:
        return None, f"chart.type must be one of {list(CHART_TYPES)}"
    if df.empty:
        return None, "the query returned no rows"
    lower = {c.lower(): c for c in df.columns}

    def col(key: str, required: bool = True) -> str | None:
        name = chart.get(key)
        if not name:
            if required:
                raise ValueError(f"chart.{key} is required for a {kind} chart")
            return None
        if name not in df.columns and str(name).lower() not in lower:
            raise ValueError(f"chart.{key} '{name}' is not a column of the result (columns: {list(df.columns)})")
        return name if name in df.columns else lower[str(name).lower()]

    def numeric(c: str) -> None:
        if not pd.api.types.is_numeric_dtype(df[c]):
            raise ValueError(f"column '{c}' must be numeric to be plotted as a measure")

    try:
        x, y = col("x"), col("y")
        color = col("color", required=kind in ("stacked_bar", "grouped_bar"))
        value = col("value") if kind == "heatmap" else None
        label = col("label", required=False) if kind == "scatter" else None
        for c in ([x, y] if kind == "scatter" else [value] if kind == "heatmap" else [y]):
            numeric(c)
    except ValueError as e:
        return None, str(e)

    title = chart.get("title") or "Custom chart"
    sub = chart.get("subtitle")
    fmt = _FORMATS.get(chart.get("y_format") or "")
    x_title, y_title = chart.get("x_title"), chart.get("y_title")
    d = df.copy()
    for c in d.columns:  # dates -> ISO strings so they sort and label cleanly
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
    order = chart.get("sort", "desc" if kind == "bar" else "none")
    n_cat = d[x].nunique()
    if kind in ("bar", "stacked_bar", "grouped_bar", "line") and n_cat > 60:
        return None, f"{n_cat} distinct x values is too many to chart; aggregate or limit in SQL (max 60)"
    if kind == "donut" and d[x].nunique() > 12:
        return None, "a donut needs 12 or fewer categories; use a bar chart or group the tail into 'Other' in SQL"

    if kind == "bar":
        sort = {"desc": "-y", "asc": "y"}.get(order)
        if sort is None:
            sort = list(dict.fromkeys(d[x].astype(str)))  # keep SQL order
        d[x] = d[x].astype(str)
        return charts.bar(d, x, y, title, subtitle=sub, horizontal=bool(chart.get("horizontal", n_cat > 8)),
                          y_format=fmt, x_title=x_title if x_title is not None else "", y_title=y_title or y,
                          sort=sort, highlight=chart.get("highlight")), None
    if kind == "line":
        x_type = "quantitative" if pd.api.types.is_numeric_dtype(df[x]) else \
            "temporal" if pd.api.types.is_datetime64_any_dtype(df[x]) else "ordinal"
        spec = charts.line(d, x, y, title, color=color, subtitle=sub, x_type=x_type, y_format=fmt,
                           x_title=x_title if x_title is not None else "", y_title=y_title or y,
                           domain=list(dict.fromkeys(d[color])) if color and d[color].nunique() <= 8 else None)
        if x_type == "temporal":
            dates = pd.to_datetime(df[x])
            if (dates.dt.day == 1).all():  # period starts: label by month (or quarter), one tick per period
                quarterly = dates.dt.month.isin([1, 4, 7, 10]).all() and dates.dt.month.nunique() > 1
                spec["encoding"]["x"]["axis"] = {"format": "Q%q %Y" if quarterly else "%b %Y", "formatType": "time",
                                                 "tickCount": {"interval": "month", "step": 3 if quarterly else 1}}
                spec["encoding"]["tooltip"][0]["timeUnit"] = "yearquarter" if quarterly else "yearmonth"
        return spec, None
    if kind == "stacked_bar":
        x_sort = list(dict.fromkeys(d[x].astype(str)))
        d[x] = d[x].astype(str)
        return charts.stacked_bar(d, x, y, color, title, subtitle=sub, normalize=bool(chart.get("normalize", False)),
                                  x_sort=x_sort, x_title=x_title if x_title is not None else "", y_title=y_title or y,
                                  horizontal=bool(chart.get("horizontal", False)), neutral_label="Other",
                                  y_format=fmt), None
    if kind == "grouped_bar":
        x_sort = list(dict.fromkeys(d[x].astype(str)))
        d[x] = d[x].astype(str)
        return charts.grouped_bar(d, x, y, color, title, subtitle=sub, y_format=fmt, x_sort=x_sort,
                                  x_title=x_title if x_title is not None else "", y_title=y_title or y,
                                  group_sort=list(dict.fromkeys(d[color]))), None
    if kind == "donut":
        return charts.donut(d, x, y, title, subtitle=sub), None
    if kind == "scatter":
        if label and len(d) > 15:
            keep = set(d.nlargest(6, x).index) | set(d.nlargest(6, y).index) | set(d.nsmallest(3, y).index)
            d["_point_label"] = [str(v) if i in keep else "" for i, v in zip(d.index, d[label])]
            label_col = "_point_label"
        else:
            label_col = label
        return charts.scatter(d, x, y, title, color=color, subtitle=sub, y_format=fmt,
                              x_format=_FORMATS.get(chart.get("x_format") or ""), x_title=x_title, y_title=y_title,
                              label=label_col, tooltip_label=label), None
    return charts.heatmap(d, x, y, value, title, subtitle=sub, value_format=fmt, x_title=x_title, y_title=y_title,
                          x_sort=_natural_order(d[x]) if chart.get("sort") != "none" else list(dict.fromkeys(d[x].astype(str))),
                          y_sort=list(dict.fromkeys(d[y].astype(str)))), None


def customize_chart(ops: list[dict], source: str | None = None, chart: int = 0, title: str | None = None, *,
                    history: list[dict]) -> ToolResult:
    """Edit a chart already shown in this conversation (highlight, recolour, filter a range, reference lines...)."""
    with_charts = [r for r in history if r.get("charts")]
    if not with_charts:
        raise ValueError("there is no chart in this conversation yet: run an analysis (or run_sql with a chart) first")
    if source:
        matches = [r for r in with_charts if r.get("call_id") == source]
        if not matches:
            known = "; ".join(f"{r.get('call_id')}: {r['title']}" for r in with_charts[-8:])
            raise ValueError(f"no chart result with id '{source}'. Recent chart results: {known}")
        src = matches[-1]
    else:
        src = with_charts[-1]   # default: the latest chart shown
    specs = src["charts"]
    if not 0 <= chart < len(specs):
        listing = " | ".join(f"source={r.get('call_id')} chart={i}: {c.get('title', {}).get('text', '?')}"
                             for r in with_charts[-6:] for i, c in enumerate(r["charts"]))
        raise ValueError(f"chart index {chart} out of range for '{src['title']}' (it has {len(specs)} chart(s)). "
                         f"Charts available in this conversation: {listing}")
    spec, changes = customize.apply(specs[chart], ops)
    # editing an already-customised chart: keep its earlier edits in the description
    history_of_edits = (src.get("meta", {}).get("changes") or []) if src.get("tool") == "customize_chart" else []
    all_changes = history_of_edits + changes
    if title:
        spec.setdefault("title", {})["text"] = title
    rows = [{k: v for k, v in r.items() if not k.startswith("_")} for r in spec["data"]["values"]]
    return ToolResult(
        tool="customize_chart", title=title or spec.get("title", {}).get("text") or src["title"],
        subtitle="Customised: " + "; ".join(all_changes),
        params={"source": src.get("call_id"), "chart": chart, "ops": ops},
        data=pd.DataFrame(rows), charts=[spec], caveats=src.get("caveats", []),
        meta={"changes": all_changes,
              "chart_summary": f"Edited chart {chart} of '{src['title']}': {'; '.join(changes)}"
                               + (f" (on top of earlier edits: {'; '.join(history_of_edits)})" if history_of_edits else "")
                               + f". Now shows: {customize.describe(spec)}."})


def run_sql(sql: str, max_rows: int = 200, chart: dict | None = None) -> ToolResult:
    s = sql.strip().rstrip(";")
    if ";" in s or not re.match(r"^\s*(select|with|from|describe|summarize)\b", s, re.I) or _FORBIDDEN.search(s):
        raise ValueError("Only a single read-only SELECT/WITH/DESCRIBE statement is allowed.")
    lookup = bool(re.match(r"^\s*(describe|summarize)", s, re.I))
    df = query(s) if lookup else query(f"select * from ({s}) limit {int(max_rows)}")
    caveats = [f"Showing up to {max_rows} rows."]
    specs, meta = [], {}
    if lookup:
        meta["internal"] = True  # a schema lookup by the model: shown in the trace, not as a result card
    if chart:
        spec, err = _custom_chart(df, chart)
        if spec is None:
            # an error goes back to the model as is_error, so the agent loop fixes the spec/SQL and retries
            cols = ", ".join(f"{c} ({df[c].dtype})" for c in df.columns)
            raise ValueError(f"Chart not drawn: {err}. The query returned {len(df)} rows with columns: {cols}. "
                             "Fix the chart spec or the SQL and call run_sql again.")
        if chart.get("customize"):
            try:
                spec, changes = customize.apply(spec, chart["customize"])
            except ValueError as e:
                raise ValueError(f"Chart customisation failed: {e}. Fix chart.customize and call run_sql again.") from None
            meta["changes"] = changes
        specs.append(spec)
        if len(df) >= max_rows:
            caveats.append(f"The chart may be truncated: the query hit the {max_rows}-row cap.")
        meta["chart_summary"] = _chart_summary(df, chart) + (
            f" Customised: {'; '.join(meta['changes'])}. Now shows: {customize.describe(spec)}." if meta.get("changes") else "")
    title = (chart or {}).get("title") if specs else None
    return ToolResult(tool="run_sql", title=title or "Ad-hoc query", params={"sql": sql, **({"chart": chart} if chart else {})},
                      subtitle="Custom chart built from an ad-hoc query of the claims warehouse" if specs else None,
                      data=df, charts=specs, caveats=caveats, meta=meta)


def _chart_summary(df: pd.DataFrame, chart: dict) -> str:
    """What was actually drawn, in words, so the model can check the chart answers the question."""
    kind, x, y = chart["type"], chart.get("x"), chart.get("y")
    lower = {c.lower(): c for c in df.columns}
    real = lambda c: c if c in df.columns else lower.get(str(c).lower(), c)
    parts = [f"Drew a {kind.replace('_', ' ')} chart titled '{chart.get('title') or 'Custom chart'}'"]
    if kind == "heatmap":
        v = real(chart["value"])
        parts.append(f"{real(y)} (rows, {df[real(y)].nunique()}) x {real(x)} (columns, {df[real(x)].nunique()}), "
                     f"shaded by {v} (range {df[v].min():,.4g} to {df[v].max():,.4g})")
    elif kind == "scatter":
        parts.append(f"{len(df)} points: {real(x)} vs {real(y)}")
    else:
        m = real(y)
        parts.append(f"{real(y)} by {real(x)}: {df[real(x)].nunique()} categories, {len(df)} rows, "
                     f"values {df[m].min():,.4g} to {df[m].max():,.4g}")
    if chart.get("color"):
        parts.append(f"series = {real(chart['color'])} ({df[real(chart['color'])].nunique()} values)")
    fmt = chart.get("y_format")
    if fmt and fmt.startswith("percent") and df[real(chart.get("value") or y)].max() > 1.5:
        parts.append("WARNING: y_format is percent but values exceed 1.5 - percent formats expect fractions "
                     "(0.25 = 25%); divide by 100 in SQL or drop y_format")
    return "; ".join(parts) + "."
