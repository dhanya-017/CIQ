"""Methodology: how every analysis is calculated, in plain language.

Shown behind the (i) button on each result card and given to the agent, so the explanation a user reads is the
one the code implements. Keep it in step with ciq/sql/procedures.sql (stored functions) and ciq/build/lot.py.
"""
from __future__ import annotations

from ciq import config

W, C, G = config.LOT_REGIMEN_WINDOW_DAYS, config.LOT_REGIMEN_WINDOW_CYCLES, config.LOT_GAP_DAYS

# Oncology line-of-therapy business rules: (rule as agreed with the business, how the build applies it)
LOT_RULES: list[tuple[str, str]] = [
    (f"A difference of {G} days or more between drug administrations is a line break (line advancement).",
     f"Any gap of {G}+ days between consecutive administration dates starts a new line, even if the same drugs "
     "resume (such lines are flagged as re-treatment)."),
    ("Adding a new drug is line advancement.",
     "After the regimen window, a drug that is not in the line's regimen starts a new line."),
    ("Removing a drug from the existing regimen is not line advancement.",
     "Dropping drugs (e.g. chemo-IO induction to IO maintenance) keeps the line; fewer drugs never advance it."),
    (f"{W} days and {C} cycles rule: a new drug after the first {W} days and {C} cycles is line advancement.",
     f"A drug first given after the regimen window (see rule 5) starts a new line."),
    (f"The first {W} days and {C} cycles define the patient's regimen; changes in that period do not advance "
     "the line (the HCP may still be adjusting drugs or schedule).",
     f"Every drug given in days 1-{W} of the line, or up to the line's {'second' if C == 2 else f'{C}th'} "
     f"administration occasion if that is later (slow schedules), joins the regimen. Example: A+B on day 1, C on "
     "day 7, A+C on day 21, A+B on day 35 is one line with regimen A+B+C."),
    ("Changing to a completely new product is line advancement.",
     "A switch to drugs outside the regimen after the window starts a new line (rules 2/4)."),
    ("Changing to a biosimilar or a different form (e.g. subcutaneous) is not line advancement.",
     "Drugs are compared by molecule: biosimilars (e.g. Mvasi, Zirabev for bevacizumab) and subcutaneous forms "
     "(Keytruda Qlex, Opdivo Qvantig, Tecentriq Hybreza, Rybrevant Faspro) map to their reference molecule; "
     "paclitaxel and nab-paclitaxel are treated as one drug."),
]
LOT_SCOPE = ("Lines are built from lung-cancer systemic therapies only (supportive care and other-tumour drugs are "
             "ignored), from observed claims. Line 1 = first line seen in the data window, which may not be the "
             "patient's true first line. A line ends at its last administration + days supply, or the day before "
             "the next line starts.")


def lot_rules_prompt() -> str:
    """The LOT rules as the agent should know and quote them."""
    return "\n".join(f"  {i}. {rule}" for i, (rule, _) in enumerate(LOT_RULES, 1)) + f"\n  Scope: {LOT_SCOPE}"


# Per tool: the question, calculation steps, definitions, why it matters, data source, and whether LOT is used.
METHODS: dict[str, dict] = {
    "data_overview": dict(
        question="What data is available and what are its limits?",
        steps=["Counts patients and claims in the warehouse and reports the claim date range.",
               "Lists known gaps in the source (e.g. no gender, no payer names)."],
        reasoning="Sets expectations before interpreting any analysis: a ~7-month window limits trend and "
                  "long look-back questions.",
        source="ref_data_coverage, dim_patient"),
    "list_products": dict(
        question="Which products are in the data?",
        steps=["Groups drug administrations/fills by product with drug class and therapy group.",
               "Counts distinct patients and administrations per product."],
        definitions=["Therapy group LUNG_SYSTEMIC = lung-cancer systemic therapies; they define the market and "
                     "lines of therapy. SUPPORTIVE and OTHER_ONCOLOGY products are listed but excluded from both."],
        reasoning="Resolves brand names and identifies competitors within a drug class.",
        source="fn_product_catalog"),
    "market_incidence_prevalence": dict(
        question="How many lung-cancer patients are diagnosed and treated each period?",
        steps=[f"Prevalent = patients with a lung-cancer diagnosis (ICD-10 {config.INDICATION_DX_PREFIX}*) in the "
               "period.",
               "Incident = patients whose first lung-cancer diagnosis falls in the period, with at least the "
               "washout period of prior data (earlier periods are flagged as not valid).",
               "Treated = patients with any lung systemic therapy in the period; new-treated = first therapy in "
               "the period (same washout)."],
        reasoning="Sizes the market and shows whether the diagnosed and treated pool is growing.",
        source="fn_incidence_prevalence"),
    "market_share": dict(
        question="What share of treated lung-cancer patients receives each product?",
        steps=["Denominator: distinct patients with any lung systemic therapy in the data.",
               "Numerator: distinct patients with at least one claim for the product.",
               "Patient share = numerator / denominator."],
        definitions=["Patient-based share: a patient on a combination counts for every product in it, so shares "
                     "add up to more than 100%."],
        reasoning="Competitive landscape by reach (patients), not by volume or revenue.",
        source="fn_market_share"),
    "product_trend": dict(
        question="How is the brand's uptake changing over time?",
        steps=["For each period: distinct product patients, product administrations, and new-to-product patients "
               "(first product claim in the data falls in that period).",
               "Share of treated market = product patients / all lung-systemic-treated patients in the period."],
        definitions=["New-to-product in the first months of data includes patients already on therapy before the "
                     "data starts (left-censoring), so early periods are overstated."],
        reasoning="Uptake and momentum: whether the brand adds patients faster than the market.",
        source="fn_product_trend"),
    "patient_demographics": dict(
        question="Who are the brand's patients?",
        steps=["Cohort = patients with at least one claim for the product.",
               "Age groups, mean and median age from the patient dimension (age at data extract)."],
        definitions=["Gender is not in the source data."],
        reasoning="Profiles the treated population for messaging, access and benchmarking against competitors.",
        source="fn_cohort_patients, fn_age_distribution"),
    "pre_index_treatments": dict(
        question="What did patients receive before starting the brand?",
        steps=["Index date = each patient's first claim for the product.",
               "Collects every therapy in the look-back window before the index date, at drug-class, product or "
               "molecule level.",
               "Share = patients with that treatment / cohort patients."],
        definitions=["Where the data window is shorter than the look-back, only the available history is used "
                     "(flagged in the caveats)."],
        reasoning="Shows the patient journey into the brand: prior lines, sequencing and likely switch sources.",
        source="fn_pre_index_treatments"),
    "source_of_business": dict(
        question="Where do the brand's patients come from?",
        steps=["For each product administration (or each patient's first one at patient level), looks back over "
               "the look-back window:",
               "Continuing = product used within the grace period before; Restart = product used earlier but "
               "after a longer gap with no other therapy in between; Restart + Switch = same but another lung "
               "therapy in between.",
               "New to Brand + Add-on = no prior product, another lung therapy before and continuing after; New to "
               "Brand + Switch = another therapy before, not continued; New to Market = no lung therapy at all in "
               "the look-back."],
        reasoning="Separates growth from new patients, competitive switches and add-on use.",
        source="fn_sob_events, fn_source_of_business"),
    "payer_mix": dict(
        question="How are the brand's claims paid?",
        steps=["Groups product claims (or patients) by payment type (Commercial, Medicare, Medicare Advantage, "
               "Medicaid...).",
               "Ranks payer plans by share of the product's claims."],
        definitions=["Payer names are not in the source: plans are shown by id with their dominant payment type.",
                     "Pharmacy claims inherit the patient's most frequent medical payment type."],
        reasoning="Access and affordability planning: which channels and plans matter for the brand.",
        source="fn_payment_type_mix, fn_top_payers"),
    "combination_therapy": dict(
        question="Is the brand used alone or in combination?",
        steps=["Uses lines of therapy (rules below). Each line's regimen is the set of drugs in its regimen window.",
               "Monotherapy / doublet / triplet / quadruplet+ = number of distinct products in the regimen.",
               "Most common regimens ranked by patients."],
        reasoning="Regimen positioning: e.g. IO monotherapy vs chemo-IO combinations.",
        source="fn_combination_type, fn_regimen_distribution", uses_lot=True),
    "line_progression": dict(
        question="What share of patients progress to later lines?",
        steps=["Uses lines of therapy (rules below); each patient's highest observed line number.",
               "Patients reaching line N = patients whose highest line >= N; % of cohort = / patients with line 1."],
        definitions=["Short data window: few patients can be followed through several lines, so later-line "
                     "progression is understated."],
        reasoning="Size of the later-line opportunity and treatment durability.",
        source="fn_line_progression", uses_lot=True),
    "product_share_by_line": dict(
        question="What is the brand's patient share in each line of therapy?",
        steps=["Uses lines of therapy (rules below), bucketed into lines 1, 2, 3 and 4+.",
               "For each bucket: patients with a line in that bucket (denominator) and those whose line includes "
               "the product (numerator).",
               "Share = numerator / denominator."],
        reasoning="Where the brand is positioned in the treatment sequence, and where share is won or lost.",
        source="fn_product_share_by_line", uses_lot=True),
    "treatments_by_line": dict(
        question="Which regimens are most common in each line?",
        steps=["Uses lines of therapy (rules below), bucketed into lines 1, 2, 3 and 4+.",
               "Ranks regimens by patients within each bucket; % of line = regimen patients / line patients."],
        reasoning="Standard of care by line and the competitive set the brand faces in each line.",
        source="fn_top_regimens_by_line", uses_lot=True),
    "dx_around_index": dict(
        question="Which diagnoses appear before and after starting the brand?",
        steps=["Index date = first product claim.",
               "Counts patients with each diagnosis code (or 3-character category) in the window before and/or "
               "after the index date.",
               "% of cohort uses all cohort patients; % of observed uses patients with any diagnosis in the window."],
        reasoning="Comorbidities, disease progression markers and adverse-event signals around therapy start.",
        source="fn_dx_around_index"),
    "persistency": dict(
        question="How long do patients stay on the brand?",
        steps=["Builds continuous therapy episodes: an episode continues while each administration starts within "
               "days supply + the grace period of the previous coverage.",
               "Kaplan-Meier curve on each patient's first episode: patients still on therapy at data end are "
               "censored, not counted as discontinued.",
               "Median time on therapy = first day the curve falls to 50% (if reached)."],
        definitions=["Infused drugs use their standard cycle length as days supply (see drug reference)."],
        reasoning="Durability and adherence: longer persistency means more doses per patient.",
        source="fn_therapy_episodes"),
    "hcp_market_concentration": dict(
        question="Is the lung-cancer market concentrated in a few HCPs, before vs after the brand start?",
        steps=["Market volume = all lung systemic administrations for the brand's patients, split PRE (window "
               "before each patient's first brand claim) and POST (from it onward).",
               "HCPs ranked by volume into deciles (decile 1 = highest); 'hcp_count' puts equal HCP counts in each "
               "decile, 'volume' makes each decile ~10% of volume.",
               "Concentration = volume share of the top 10% of HCPs and the Herfindahl-Hirschman index (HHI)."],
        definitions=["HCP = rendering provider (medical claims) or prescriber (pharmacy claims)."],
        reasoning="Targeting: a concentrated market can be covered with fewer, high-value HCPs.",
        source="fn_hcp_market_volume, fn_hcp_market_deciles"),
    "hcp_product_share_deciles": dict(
        question="How is the brand's volume and share distributed across prescribers?",
        steps=["For every HCP: brand volume (administrations/fills) and lung-market volume (all lung systemic "
               "therapies).",
               "HCPs ranked by brand volume into deciles (decile 1 = highest). 'volume' method: each decile holds "
               "~10% of brand volume; 'hcp_count': equal HCP counts.",
               "Decile share = brand volume / market volume of the HCPs in that decile."],
        reasoning="Separates loyal high-volume prescribers from high-potential HCPs where the brand's share is "
                  "low (growth opportunity).",
        source="fn_hcp_product_share, fn_hcp_product_share_deciles"),
    "compare_products": dict(
        question="How do these brands compare?",
        steps=["Runs the same analyses for each brand: patients and share, age, payer mix, share by line and "
               "regimen type, source of business, persistency and prescriber base.",
               "Scorecard marks the leading brand on each metric."],
        reasoning="Benchmarking on a like-for-like basis.",
        source="several fn_* functions (see each section)", uses_lot=True),
    "compare_segments": dict(
        question="How do two sides differ, where is the gap, and who is behind it?",
        steps=["Both sides use the same market (lung-cancer systemic therapy claims), period and definitions.",
               "Brands: patients, share, volume, prescribers, share change, line share, combination use, 180-day "
               "persistency and patient mix for each brand. HCP / patient / time cohorts: the claims are filtered to "
               "each side, then the focus brand's share, volume, new patients and prescribers are measured on it.",
               "Difference = B minus A: percentage points for shares, absolute and relative change for counts.",
               "Where: brands - each brand's share of HCP volume by HCP decile (deciled on total lung volume) and "
               "payer mix; cohorts - treatment mix (share of each side's patients on each product); time - HCP "
               "movement (new, stopped, increased, decreased monthly brand volume).",
               "Who: the HCPs behind the gap (e.g. where the competitor out-sells the brand), which can be exported "
               "or saved as a cohort and compared again."],
        definitions=["HCP cohorts are judged on volume share (brand administrations / all lung administrations of "
                     "those HCPs); patient and time cohorts on patient share.",
                     "Time periods of different length are compared per month for volumes."],
        reasoning="Turns a side-by-side view into a diagnosis: quantify the gap, locate it, and hand the brand team "
                  "a target list.",
        source="ciq/tools/compare.py (fact_drug_event, dim_patient, lot_line, fn_hcp_product_share, fn_product_trend)"),
    "run_sql": dict(
        question="A custom question answered with an ad-hoc query.",
        steps=["The model wrote the read-only SQL below against the claims warehouse.",
               "The chart (if any) was drawn from exactly the rows the query returned."],
        reasoning="Covers questions the standard analyses don't; check the SQL and Data tab before sharing.",
        source="ad-hoc SQL"),
    "customize_chart": dict(
        question="An edited version of an earlier chart.",
        steps=["Starts from the chart of an earlier analysis in this conversation.",
               "Applies the requested edits below; the numbers are unchanged, except that a filter drops rows "
               "from view."],
        reasoning="Presentation changes only; see the original card for how the numbers were calculated.",
        source="earlier result in this conversation"),
}


def describe(tool: str, params: dict, meta: dict | None = None) -> dict:
    """Methodology payload for a result card's (i) panel."""
    m = METHODS.get(tool, {})
    out = {"question": m.get("question", ""), "steps": list(m.get("steps", [])),
           "definitions": list(m.get("definitions", [])), "reasoning": m.get("reasoning", ""),
           "source": m.get("source", ""), "params": {k: v for k, v in params.items() if k not in ("sql", "chart", "ops")}}
    if m.get("uses_lot"):
        out["lot_rules"] = [{"rule": r, "applied": a} for r, a in LOT_RULES]
        out["lot_scope"] = LOT_SCOPE
    if tool == "run_sql":
        out["sql"] = params.get("sql")
        if (meta or {}).get("chart_summary"):
            out["steps"].append((meta or {})["chart_summary"])
    if tool == "customize_chart":
        out["steps"] += [f"Edit: {c}" for c in (meta or {}).get("changes", [])]
    return out
