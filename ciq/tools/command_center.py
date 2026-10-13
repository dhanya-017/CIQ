"""Market Command Center: the home screen for a brand in its market.

Answers, from observed claims only: how big is the market, how is the brand doing, what changed (current window vs
the prior window of equal length), where is the opportunity, and what to investigate next. Every signal carries its
evidence and an action; nothing here is model-generated.
"""
from __future__ import annotations

from functools import lru_cache

import numpy as np
import pandas as pd

from ciq import config
from ciq.tools import charts, compare
from ciq.tools.base import coverage, json_safe, pct, query, resolve_product

LUNG = "therapy_group = 'LUNG_SYSTEMIC'"
OTHER_INDICATIONS = ["Breast cancer", "Melanoma", "Colorectal cancer", "Multiple myeloma"]
# 0 = all months in the data (no equal-length prior window)
WINDOWS = (1, 0)
# Dark / mid / light per IO family: new / continued / switch (incidence chart uses dark + light).
IO_FAMILY_PALETTE = {
    "KEYTRUDA": ("#007A73", "#3AA39C", "#8CD4CF"),
    "OPDIVO": ("#5c2d91", "#8B5CB8", "#c5a3e8"),
    "TECENTRIQ": ("#154a9a", "#4A7BC8", "#8bb6ef"),
}
_IO_FAMILY_SQL = """case
      when product in ('KEYTRUDA', 'KEYTRUDA QLEX') then 'KEYTRUDA'
      when product in ('OPDIVO', 'OPDIVO QVANTIG') then 'OPDIVO'
      when product in ('TECENTRIQ', 'TECENTRIQ HYBREZA') then 'TECENTRIQ'
      else product end"""
_C34 = "dx_code like 'C34%'"


def _io_product_names(families: list[str]) -> list[str]:
    names = []
    for b in families:
        names.append(b)
        sc = (config.IO_BRANDS.get(b) or {}).get("sc_form")
        if sc:
            names.append(sc)
    return names


def normalize_window(n: int) -> int:
    """Accept 1 (last month) or 0 (all months). Map removed controls to the nearest remaining option."""
    if n in (2,):
        return 1
    if n == 3:
        return 0
    return n if n in WINDOWS else 1


def _windows(n: int) -> tuple[str | None, str, list[str]]:
    """Current = last n months (or all months if n is 0); prior = the n months before, else None."""
    cov = coverage()
    months = [str(m) for m in pd.period_range(cov["data_start"], cov["data_end"], freq="M")]
    n = normalize_window(n)
    if n == 0:
        return None, f"{months[0]}..{months[-1]}", months
    n = max(1, min(n, max(1, len(months) // 2)))
    cur, pri = months[-n:], months[-2 * n:-n]
    return f"{pri[0]}..{pri[-1]}", f"{cur[0]}..{cur[-1]}", months


def _nice_class(c: str) -> str:
    """'PD-1/PD-L1 INHIBITOR' -> 'PD-1/PD-L1 inhibitor' (keep acronyms, lower-case words)."""
    return " ".join(w if any(ch.isdigit() for ch in w) or len(w) <= 4 else w.lower() for w in c.split())


def _label(spec: str) -> str:
    return compare._month_range(spec)[0]


def _period_stats(spec: str, product: str) -> dict:
    _, start, end = compare._month_range(spec)
    m = query(f"""
        with e as (select * from fact_drug_event where {LUNG} and service_date >= $s and service_date < $e)
        select count(distinct patient_id) patients, count(*) volume, count(distinct hcp_id) hcps,
               count(distinct patient_id) filter (where product = $p or product_form = $p) p_patients,
               count(*) filter (where product = $p or product_form = $p) p_volume,
               count(distinct hcp_id) filter (where product = $p or product_form = $p) p_hcps,
               (select count(*) from fn_product_index($p) where index_date >= $s and index_date < $e) p_new
        from e""", {"s": start, "e": end, "p": product}).iloc[0].to_dict()
    m["share"] = m["p_patients"] / m["patients"] if m["patients"] else 0
    m["vshare"] = m["p_volume"] / m["volume"] if m["volume"] else 0
    return m


def _product_shares(spec: str) -> pd.DataFrame:
    _, start, end = compare._month_range(spec)
    return query(f"""
        with e as (select * from fact_drug_event where {LUNG} and service_date >= $s and service_date < $e)
        select product, any_value(drug_class) drug_class, count(distinct patient_id) pts,
               count(distinct patient_id) / (select count(distinct patient_id) from e) as share
        from e group by product""", {"s": start, "e": end})


def _delta(cur, pri, kind):
    if kind == "pp":
        return {"value": (cur - pri) * 100, "fmt": f"{(cur - pri) * 100:+.1f}pp"}
    if not pri:
        return {"value": None, "fmt": "new"}
    return {"value": cur / pri - 1, "fmt": f"{(cur / pri - 1) * 100:+.0f}%"}


def build(product: str | None = None, window: int = 1) -> dict:
    product = resolve_product(product)
    window = normalize_window(window)
    cov = coverage()
    pri, cur, months = _windows(window)
    lc = _label(cur)
    lp = _label(pri) if pri else None
    cs = _period_stats(cur, product)
    ps = _period_stats(pri, product) if pri else None
    drug_class = query("select any_value(drug_class) c from fact_drug_event where product = $p or product_form = $p",
                       {"p": product}).c.iloc[0]
    cls_name = _nice_class(drug_class)

    # ---------------- market snapshot (current window, change vs prior)
    market = [
        {"id": "patients", "label": "Treated patients", "value": cs["patients"], "format": "int",
         **({"delta": _delta(cs["patients"], ps["patients"], "rel") | {"coverage": True}} if ps else {})},
        {"id": "volume", "label": "Therapy administrations", "value": cs["volume"], "format": "int",
         **({"delta": _delta(cs["volume"], ps["volume"], "rel") | {"coverage": True}} if ps else {})},
        {"id": "hcps", "label": "Treating HCPs", "value": cs["hcps"], "format": "int",
         **({"delta": _delta(cs["hcps"], ps["hcps"], "rel") | {"coverage": True}} if ps else {})},
        {"id": "value", "label": "Market value", "value": None, "format": "text",
         "unavailable": "Connect pricing (WAC/ASP) data to size the market in $"},
    ]

    # ---------------- brand performance
    h = compare._hcp_table(product)
    hv = h.market_volume.quantile(0.75)
    overall = h.p_volume.sum() / h.market_volume.sum()
    h["share"] = h.p_volume / h.market_volume
    opp = h[(h.market_volume >= hv) & (h.share < overall)].sort_values("market_volume", ascending=False)
    brand = [
        {"id": "share", "label": f"{product} patient share", "value": cs["share"], "format": "pct",
         "lead": True, **({"delta": _delta(cs["share"], ps["share"], "pp")} if ps else {})},
        {"id": "vshare", "label": f"{product} volume share", "value": cs["vshare"], "format": "pct",
         **({"delta": _delta(cs["vshare"], ps["vshare"], "pp")} if ps else {})},
        {"id": "new", "label": f"New-to-{product} patients", "value": cs["p_new"], "format": "int",
         **({"delta": _delta(cs["p_new"], ps["p_new"], "rel")} if ps else {})},
        {"id": "opportunity", "label": "HCP opportunity", "value": len(opp), "format": "int",
         "note": f"high-volume HCPs, {product} share < {pct(overall)} (whole window)"},
    ]

    # ---------------- what matters now (observed signals, each with evidence and an action)
    signals = []
    tc = None
    if pri and ps is not None:
        tc = compare.compare_segments("time", pri, cur, product)
        mv = pd.DataFrame(tc.data["hcp_movement"]).set_index("movement") if len(tc.data["hcp_movement"]) else pd.DataFrame()
        d_share = (cs["share"] - ps["share"]) * 100
        ev = []
        for key, verb in (("New prescribers", "started prescribing"), ("Increased", "increased monthly volume"),
                          ("Decreased", "decreased monthly volume"), ("Stopped prescribing", "stopped prescribing")):
            if key in mv.index and mv.loc[key, "hcps"]:
                ev.append(f"{int(mv.loc[key, 'hcps'])} HCPs {verb} ({mv.loc[key, 'net_change_per_month']:+.0f}/month)")
        signals.append({
            "id": "share", "direction": "up" if d_share > 0.05 else "down" if d_share < -0.05 else "flat",
            "headline": f"{product} patient share {'rose' if d_share > 0.05 else 'fell' if d_share < -0.05 else 'held'} "
                        f"{abs(d_share):.1f}pp to {pct(cs['share'])}",
            "evidence": f"{lp} {pct(ps['share'])} → {lc} {pct(cs['share'])}. " + "; ".join(ev[:3]) + ".",
            "action": {"type": "compare", "label": "Explore drivers", "kind": "time", "a": pri, "b": cur}})

        _, s1, e1 = compare._month_range(pri)
        _, s2, e2 = compare._month_range(cur)
        cls = query(f"""
            select hcp_id,
              count(*) filter (where service_date >= $s1 and service_date < $e1 and drug_class = $c) cls_a,
              count(*) filter (where service_date >= $s1 and service_date < $e1 and product = $p) p_a,
              count(*) filter (where service_date >= $s2 and service_date < $e2 and drug_class = $c) cls_b,
              count(*) filter (where service_date >= $s2 and service_date < $e2 and product = $p) p_b
            from fact_drug_event where {LUNG} and hcp_id is not null and hcp_id = any($ids) group by 1""",
                    {"s1": s1, "e1": e1, "s2": s2, "e2": e2, "c": drug_class, "p": product,
                     "ids": h[h.market_volume >= hv].hcp_id.tolist()})
        both = cls[(cls.cls_a > 0) & (cls.cls_b > 0)].copy()
        both["comp_share_a"] = 1 - both.p_a / both.cls_a
        both["comp_share_b"] = 1 - both.p_b / both.cls_b
        shifted = both[both.comp_share_b > both.comp_share_a + 1e-9].sort_values("cls_b", ascending=False)
        n_win = max(window, 1)
        comp_gain = query(f"""
            select product,
              count(*) filter (where service_date >= $s2 and service_date < $e2) / $n2
              - count(*) filter (where service_date >= $s1 and service_date < $e1) / $n1 as gain_pm
            from fact_drug_event where drug_class = $c and product <> $p and hcp_id = any($ids) group by 1
            having gain_pm > 0 order by gain_pm desc""",
                          {"s1": s1, "e1": e1, "s2": s2, "e2": e2, "c": drug_class, "p": product,
                           "n1": n_win, "n2": n_win, "ids": shifted.hcp_id.tolist() or ["-"]})
        if len(shifted):
            top = comp_gain.iloc[0] if len(comp_gain) else None
            signals.append({
                "id": "competitors", "direction": "down",
                "headline": f"{len(shifted)} high-volume HCPs shifted toward competitors",
                "evidence": (f"Within {cls_name}s, competitor share rose at these HCPs ({lp} → {lc})" +
                             (f"; {top['product']} accounts for {pct(top.gain_pm / comp_gain.gain_pm.sum())} of the "
                              f"competitor increase." if top is not None else ".")),
                "hcps": {"label": f"High-volume HCPs shifting toward competitors ({lp} → {lc})",
                         "rows": shifted.head(500).assign(
                             competitor_share_prior=lambda x: x.comp_share_a, competitor_share_current=lambda x: x.comp_share_b)
                         [["hcp_id", "cls_a", "cls_b", "competitor_share_prior", "competitor_share_current"]]
                         .rename(columns={"cls_a": f"{drug_class} admins {lp}", "cls_b": f"{drug_class} admins {lc}"})
                         .to_dict("records"),
                         "hcp_ids": shifted.hcp_id.tolist()},
                "action": {"type": "compare", "label": f"Compare {product} vs {top['product']}" if top is not None
                           else "Compare brands", "kind": "brands", "a": product,
                           "b": top["product"] if top is not None else None}})

        shares = _product_shares(pri).merge(_product_shares(cur), on=["product", "drug_class"], how="outer",
                                            suffixes=("_a", "_b")).fillna({"share_a": 0, "share_b": 0, "pts_a": 0, "pts_b": 0})
        shares["delta_pp"] = (shares.share_b - shares.share_a) * 100
        rivals = shares[(shares.drug_class == drug_class) & (shares["product"] != product)].sort_values("delta_pp", ascending=False)
        if len(rivals) and rivals.delta_pp.iloc[0] > 0.1:
            r = rivals.iloc[0]
            signals.append({
                "id": "rival", "direction": "watch",
                "headline": f"{r['product']} gained {r.delta_pp:.1f}pp of treated patients",
                "evidence": f"Fastest-growing {cls_name} competitor: {pct(r.share_a)} → {pct(r.share_b)} "
                            f"({lp} → {lc}).",
                "action": {"type": "compare", "label": f"Compare {product} vs {r['product']}", "kind": "brands",
                           "a": product, "b": r["product"]}})
    else:
        shares = _product_shares(cur).rename(columns={"share": "share_b", "pts": "pts_b"})
        shares["share_a"] = 0.0
        shares["pts_a"] = 0
        shares["delta_pp"] = 0.0
        rivals = shares[(shares.drug_class == drug_class) & (shares["product"] != product)].sort_values("share_b", ascending=False)

    signals.append({
        "id": "opportunity", "direction": "opportunity",
        "headline": f"{len(opp):,} high-volume HCPs are under-indexed on {product}",
        "evidence": f"Top-quartile lung-therapy volume (>= {hv:.0f} administrations) but {product} share below its "
                    f"{pct(overall)} average; together they treat {pct(opp.market_volume.sum() / h.market_volume.sum())} "
                    f"of lung-therapy volume (whole data window, {cov['data_start'][:7]} to {cov['data_end'][:7]}).",
        "hcps": {"label": f"High-volume HCPs with {product} share < {pct(overall)}",
                 "rows": opp.head(500).rename(columns={"market_volume": "lung_volume", "p_volume": f"{product.lower()}_volume",
                                                       "share": f"{product.lower()}_share"}).to_dict("records"),
                 "hcp_ids": opp.hcp_id.tolist()},
        "action": {"type": "compare", "label": "View opportunity", "kind": "hcp", "a": "high_low", "b": "all"}})

    # ---------------- lenses
    trend = query("select period, market_patients, product_patients, patient_share, new_patients from fn_product_trend($p)",
                  {"p": product})
    comp_set = [product] + rivals.sort_values("share_b", ascending=False)["product"].head(3).tolist()
    ctrend = query(f"""
        with m as (select strftime(date_trunc('month', service_date), '%Y-%m') period, patient_id, product
                   from fact_drug_event where {LUNG})
        select period, product, count(distinct patient_id) / any_value(t.n) as share
        from m join (select period, count(distinct patient_id) n from m group by 1) t using (period)
        where product = any($ps) group by all order by 1""", {"ps": comp_set})
    top_products = shares.sort_values("share_b", ascending=False).head(8)
    deciles = query("select * from fn_hcp_product_share_deciles($p)", {"p": product}).assign(decile=lambda d: d.decile.astype(str))
    lines = query("select * from fn_product_share_by_line($p)", {"p": product}).assign(line=lambda d: "Line " + d.line_bucket)
    movement_chart = next((c for c in (tc.charts if tc else []) if "HCP movement" in c.get("title", {}).get("text", "")), None)
    in_class = rivals.assign(product=rivals["product"]).sort_values("delta_pp")
    in_class = pd.concat([in_class, shares[shares["product"] == product]])

    if pri:
        tp_long = pd.concat([top_products.assign(window=lp, share=top_products.share_a),
                             top_products.assign(window=lc, share=top_products.share_b)])
        top_chart = charts.grouped_bar(tp_long, "product", "share", "window", f"Top products: {lp} vs {lc}",
                                       subtitle="Share of treated lung-cancer patients", y_format=".0%", x_title="",
                                       y_title="Patient share", x_sort=top_products["product"].tolist(), group_sort=[lp, lc],
                                       domain=[lp, lc])
        top_chart["encoding"]["x"]["axis"] = {"labelAngle": -35, "labelLimit": 110}
        competitor_change = compare._diverging(in_class, "product", "delta_pp", f"Share change, {lp} → {lc}",
                                               f"{cls_name[:1].upper() + cls_name[1:]}s, change in share of treated patients", lp, lc,
                                               names=("Lost share", "Gained share"))
    else:
        top_chart = charts.bar(top_products, "product", "share_b", f"Top products: {lc}",
                               subtitle="Share of treated lung-cancer patients", y_format=".0%", x_title="",
                               y_title="Patient share", horizontal=True)
        competitor_change = charts.bar(in_class.sort_values("share_b", ascending=False), "product", "share_b",
                                       f"{cls_name[:1].upper() + cls_name[1:]} share of treated patients, {lc}",
                                       y_format=".0%", x_title="", y_title="Patient share", horizontal=True)

    lenses = {
        "market": {"question": "What is happening in the overall treatment market?", "charts": [
            charts.line(trend.assign(share=trend.patient_share), "period", "share", f"{product} share of treated patients by month",
                        y_format=".0%", x_title="", y_title="Patient share"),
            top_chart],
            "questions": ["What is the incidence and prevalence of the market and has it changed recently?",
                          "Which products are gaining share of treated patients?",
                          f"How has {product} uptake and share of the treated market trended?"]},
        "hcps": {"question": "Who is driving the market?", "charts": [c for c in [
            charts.bar(deciles, "decile", "product_share_of_market", f"{product} share of HCP lung volume by decile",
                       subtitle=f"Decile 1 = highest {product} volume", sort=[str(i) for i in range(1, 11)], y_format=".0%",
                       x_title="Decile", y_title=f"{product} share", highlight="1"),
            movement_chart] if c],
            "questions": [f"Decile the HCPs based on {product} market share",
                          f"Is the market more concentrated across HCPs after first {product}?",
                          f"Which HCPs should we target to grow {product}?"]},
        "patients": {"question": "What is happening to patients?", "charts": [
            charts.bar(trend, "period", "new_patients", f"New-to-{product} patients by month", sort=None, x_title="",
                       y_title="Patients", subtitle="Early months include patients already on therapy before the data starts"),
            charts.bar(lines, "line", "product_share", f"{product} share of patients in each line", sort=None,
                       y_format=".0%", x_title="", y_title="Patient share")],
            "questions": [f"What is the source of business for {product}?", f"How persistent are {product} patients?",
                          f"What percentage of {product} patients progress to line 3+?"]},
        "competitors": {"question": "Who are we competing against?", "charts": [
            charts.line(ctrend, "period", "share", f"{cls_name[:1].upper() + cls_name[1:]} share of treated patients by month",
                        color="product", y_format=".0%", x_title="", y_title="Patient share", domain=comp_set),
            competitor_change],
            "questions": [f"Compare {product} with its top competitors",
                          f"Which HCPs use more of a competitor than {product}?",
                          f"Where is {product} losing patients to competitors?"]},
    }

    return json_safe({
        "context": {"indication": config.INDICATION_NAME, "indications": [config.INDICATION_NAME] + OTHER_INDICATIONS,
                    "product": product, "brands": _all_brands(), "drug_class": drug_class, "geography": "United States",
                    "geography_note": "Regional views need a geography source (HCP address or patient ZIP3).",
                    "data_start": cov["data_start"], "data_end": cov["data_end"],
                    "window": window, "windows": list(WINDOWS), "current": {"spec": cur, "label": lc},
                    "prior": {"spec": pri, "label": lp} if pri else None},
        "snapshot": _snapshot(product, window),
        "market": market, "market_note": "Growth partly reflects claims capture ramping up early in the data window.",
        "brand": brand, "signals": signals, "lenses": lenses,
        "notes": ["Claims capture ramps up in the first months of the data (Jan-Mar), so raw volume and patient growth "
                  "partly reflects data coverage; shares are the like-for-like comparison.",
                  (f"Current = {lc}, prior = {lp} (equal-length windows)." if pri
                   else f"Current = {lc} (all months in the data; no prior window).")],
    })


def _all_brands() -> list[dict]:
    df = query("""
        select product,
               any_value(drug_class) as drug_class,
               any_value(molecule) as molecules,
               count(distinct patient_id) as patients
        from fact_drug_event
        where coalesce(product, '') != ''
        group by product
        order by patients desc, product
        limit 500""")
    return [{"product": r.product, "drug_class": r.drug_class or "",
             "molecules": r.molecules or "", "patients": int(r.patients)}
            for r in df.itertuples()]


def _snapshot(product: str, window: int) -> dict:
    """Landing tiles: C34 cohort size and C34 patients treated with each IO family (IV + SC)."""
    families = list(config.IO_BRANDS)
    names = _io_product_names(families)
    latest = query("select date_trunc('month', data_end) as mo from ref_data_coverage").mo.iloc[0]
    if window == 1:
        total = int(query(f"""
            select count(distinct patient_id) n from fact_dx_event
            where {_C34} and date_trunc('month', service_date) = $mo""", {"mo": latest}).n.iloc[0])
        fam = query(f"""
            with nsclc as (
              select distinct patient_id from fact_dx_event
              where {_C34} and date_trunc('month', service_date) = $mo
            )
            select {_IO_FAMILY_SQL} as product, count(distinct d.patient_id) as patients
            from fact_drug_event d
            join nsclc using (patient_id)
            where d.product = any($ps) and date_trunc('month', d.service_date) = $mo
            group by 1""", {"ps": names, "mo": latest})
    else:
        total = int(query(f"select count(distinct patient_id) n from fact_dx_event where {_C34}").n.iloc[0])
        fam = query(f"""
            with nsclc as (
              select distinct patient_id from fact_dx_event where {_C34}
            )
            select {_IO_FAMILY_SQL} as product, count(distinct d.patient_id) as patients
            from fact_drug_event d
            join nsclc using (patient_id)
            where d.product = any($ps)
            group by 1""", {"ps": names})
    by = {r.product: int(r.patients) for r in fam.itertuples()}
    brands = [{"product": p, "patients": by.get(p, 0)} for p in families]
    mix_chart = _incidence_prevalence_chart() if window == 0 else None
    return {"total_patients": total, "brands": brands, "mix_chart": mix_chart,
            "brand_trend": _brand_month_chart(families) if window == 0 else None,
            "brand_source": _brand_source_chart(families) if window == 0 else None,
            "lot_reach_chart": _lot_reach_chart() if window == 0 else None,
            "lot_regimen_charts": _lot_regimen_charts() if window == 0 else None,
            "lot_dot_chart": _lot_dot_chart() if window == 0 else None,
            "lot_molecule_chart": _lot_molecule_chart() if window == 0 else None,
            "lot_flow_chart": _lot_flow_chart() if window == 0 else None}


def _c34_lot_sql() -> tuple[str, str]:
    pred = f"patient_id in (select distinct patient_id from fact_dx_event where {_C34})"
    index_sql = (
        f"select patient_id, min(service_date) as index_date "
        f"from fact_dx_event where {_C34} group by 1"
    )
    return pred, index_sql


@lru_cache(maxsize=8)
def _lot_pack_for_rules(cycle_window_days: int, line_gap_days: int, min_cycles: int, min_days: int) -> dict:
    from ciq.tools import cohort_lot
    pred, index_sql = _c34_lot_sql()
    return cohort_lot.for_cohort(pred, index_sql, rules={
        "cycle_window_days": cycle_window_days,
        "line_gap_days": line_gap_days,
        "min_cycles": min_cycles,
        "min_days": min_days,
    })


def _nsclc_notebook_lot() -> dict:
    """Notebook LOT for C34 patients, first C34 as index, default 4d / 90d / 2 / 30."""
    from ciq.tools import cohort_lot
    r = cohort_lot.default_rules()
    return _lot_pack_for_rules(r["cycle_window_days"], r["line_gap_days"], r["min_cycles"], r["min_days"])


def playground_defaults() -> dict:
    from ciq.tools import cohort_lot
    return cohort_lot.default_rules()


def playground_lot(rules: dict | None = None) -> dict:
    """LoT summaries for the C34 extract under the given (or default) four rules."""
    from ciq.tools import cohort_lot
    r = cohort_lot.parse_rules(rules)
    pack = _lot_pack_for_rules(r["cycle_window_days"], r["line_gap_days"], r["min_cycles"], r["min_days"])
    return {
        "rules": r,
        "patients": pack.get("patients") or 0,
        "patients_with_lot": pack.get("patients_with_lot") or 0,
        "lot_reach_chart": _lot_reach_chart(pack),
        "lot_regimen_charts": _lot_regimen_charts(pack),
        "lot_dot_chart": _lot_dot_chart(pack),
        "lot_molecule_chart": _lot_molecule_chart(pack),
        "lot_flow_chart": _lot_flow_chart(pack),
    }


def _clean_chart(spec: dict) -> dict:
    cfg = dict(spec.get("config") or {})
    axis = dict(cfg.get("axis") or {})
    axis["grid"] = False
    cfg["axis"] = axis
    cfg["bar"] = {**dict(cfg.get("bar") or {}), "cornerRadiusEnd": 0}
    spec["config"] = cfg
    return spec


def _lot_reach_chart(pack: dict | None = None) -> dict | None:
    """Patients reaching each line of therapy (notebook chart after the three market graphs)."""
    pack = pack if pack is not None else _nsclc_notebook_lot()
    rows = [r for r in (pack.get("reaching") or []) if int(r["line"]) <= 15]
    n = int(pack.get("patients_with_lot") or 0)
    if not rows or n <= 0:
        return None
    df = pd.DataFrame(rows)
    df["line"] = df["line"].astype(int)
    df["axis"] = [f"{int(x)}L" for x in df["line"]]
    df["pct"] = df["patients"] / n
    df["caption"] = [f"{int(p):,}({100 * q:.0f}%)" for p, q in zip(df["patients"], df["pct"])]
    order = df["axis"].tolist()
    hi = max(int(df["patients"].max()), 1)
    spec = charts._base(df, f"Patients reaching each line (N={n:,})", None)
    spec["height"] = 360
    spec["padding"] = {"top": 18, "left": 8, "right": 8, "bottom": 8}
    spec["layer"] = [
        {
            "mark": {"type": "bar", "color": "#4C78A8", "cornerRadiusEnd": 0},
            "encoding": {
                "x": {"field": "axis", "type": "ordinal", "title": "Line of therapy", "sort": order,
                      "axis": {"labelAngle": 0}},
                "y": {"field": "patients", "type": "quantitative", "title": "Patients",
                      "scale": {"domain": [0, hi * 1.18]}},
                "tooltip": charts._tooltip([("axis", "ordinal", None), ("patients", "quantitative", ",.0f"),
                                            ("pct", "quantitative", ".0%")]),
            },
        },
        {
            "mark": {"type": "text", "baseline": "bottom", "dy": -6, "fontSize": 11, "color": "#3a3936",
                     "clip": False},
            "encoding": {
                "x": {"field": "axis", "type": "ordinal", "sort": order},
                "y": {"field": "patients", "type": "quantitative",
                      "scale": {"domain": [0, hi * 1.18]}},
                "text": {"field": "caption", "type": "nominal"},
            },
        },
    ]
    return _clean_chart(spec)


def _lot_regimen_charts(pack: dict | None = None) -> list[dict]:
    """Top 10 regimens for 1L/2L/3L (notebook: value_counts head 10, % of patients on that line)."""
    pack = pack if pack is not None else _nsclc_notebook_lot()
    out = []
    for block in pack.get("regimens_by_line") or []:
        ln = int(block["line"])
        if ln not in (1, 2, 3) or not block.get("regimens"):
            continue
        n = int(block["n"])
        df = pd.DataFrame(block["regimens"])
        df["axis"] = df["regimen"].map(lambda t: t[:55])
        df["caption"] = [f"{int(p):,} ({100 * q:.0f}%)" for p, q in zip(df["patients"], df["pct"])]
        spec = charts._base(df, f"{ln}L  (n={n:,})", None)
        spec["height"] = {"step": 30}
        spec["padding"] = {"right": 56, "top": 4, "bottom": 8}
        spec["layer"] = [
            {
                "mark": {"type": "bar", "color": "#72B7B2", "cornerRadiusEnd": 0},
                "encoding": {
                    "y": {"field": "axis", "type": "ordinal", "title": "Product Combinations", "sort": "-x",
                          "axis": {"labelLimit": 200, "labelFontSize": 12, "labelPadding": 8,
                                   "titleOrient": "left", "titleAngle": -90, "titleAlign": "center",
                                   "titlePadding": 16, "titleFontSize": 12, "minExtent": 220}},
                    "x": {"field": "patients", "type": "quantitative", "title": "Patients"},
                    "tooltip": charts._tooltip([("regimen", "nominal", None),
                                                ("patients", "quantitative", ",.0f"),
                                                ("pct", "quantitative", ".0%")]),
                },
            },
            {
                "mark": {"type": "text", "align": "left", "dx": 4, "fontSize": 10, "color": "#3a3936",
                         "clip": False},
                "encoding": {
                    "y": {"field": "axis", "type": "ordinal", "sort": "-x"},
                    "x": {"field": "patients", "type": "quantitative"},
                    "text": {"field": "caption", "type": "nominal"},
                },
            },
        ]
        out.append(_clean_chart(spec))
    return out


def _lot_dot_chart(pack: dict | None = None) -> dict | None:
    """Mean vs median days on therapy by line (notebook grouped bars, lines 1–10)."""
    pack = pack if pack is not None else _nsclc_notebook_lot()
    rows = pack.get("days_on_therapy") or []
    if not rows or not any(int(r["n"]) > 0 for r in rows):
        return None
    long = []
    order = []
    for r in rows:
        ln, n = int(r["line"]), int(r["n"])
        label = f"{ln}L (n={n})"
        order.append(label)
        for stat, key in (("Mean days", "mean"), ("Median days", "median")):
            val = r.get(key)
            days = 0.0 if val is None else float(val)
            long.append({
                "axis": label, "line": ln, "stat": stat, "days": days, "n": n,
                "caption": "" if n == 0 else f"{days:.0f}",
                "p25": r.get("p25"), "p75": r.get("p75"),
            })
    df = pd.DataFrame(long)
    stats = ["Mean days", "Median days"]
    spec = charts._base(df, "Days on therapy by line", None)
    spec["height"] = 340
    spec["layer"] = [
        {
            "mark": {"type": "bar", "cornerRadiusEnd": 0},
            "encoding": {
                "x": {"field": "axis", "type": "ordinal", "title": "Line of therapy", "sort": order,
                      "axis": {"labelAngle": 0, "labelFontSize": 11, "labelLimit": 72},
                      "scale": {"paddingInner": 0.46, "paddingOuter": 0.15}},
                "xOffset": {"field": "stat", "type": "nominal", "sort": stats,
                            "scale": {"paddingInner": 0.12}},
                "y": {"field": "days", "type": "quantitative", "title": "Days on therapy"},
                "color": {"field": "stat", "type": "nominal", "title": None, "sort": stats,
                          "scale": {"domain": stats, "range": ["#4C78A8", "#F58518"]},
                          "legend": {"orient": "top", "direction": "horizontal"}},
                "tooltip": charts._tooltip([("axis", "ordinal", None), ("stat", "nominal", None),
                                            ("days", "quantitative", ",.1f"),
                                            ("n", "quantitative", ",.0f"),
                                            ("p25", "quantitative", ",.1f"),
                                            ("p75", "quantitative", ",.1f")]),
            },
        },
        {
            "mark": {"type": "text", "baseline": "bottom", "dy": -4, "fontSize": 10, "color": "#3a3936"},
            "encoding": {
                "x": {"field": "axis", "type": "ordinal", "sort": order},
                "xOffset": {"field": "stat", "type": "nominal", "sort": stats},
                "y": {"field": "days", "type": "quantitative"},
                "text": {"field": "caption", "type": "nominal"},
            },
        },
    ]
    return _clean_chart(spec)


_MOLECULE_CHART_DRUGS = ("pembrolizumab", "atezolizumab", "nivolumab")


def _lot_molecule_chart(pack: dict | None = None) -> dict | None:
    """IO molecules as % of patients on 1L/2L/3L: pembrolizumab, atezolizumab, nivolumab."""
    pack = pack if pack is not None else _nsclc_notebook_lot()
    mol = pack.get("molecule_share") or {}
    rows, denoms = mol.get("share") or [], mol.get("lines") or []
    keep = set(_MOLECULE_CHART_DRUGS)
    rows = [r for r in rows if r.get("drug") in keep]
    drugs = [d for d in _MOLECULE_CHART_DRUGS if any(r.get("drug") == d for r in rows)]
    if not rows or not drugs:
        return None
    n_by_line = {int(r["line"]): int(r["n"]) for r in denoms}
    series_order, series_range = [], ["#1f77b4", "#ff7f0e", "#2ca02c"]
    for ln in (1, 2, 3):
        series_order.append(f"{ln}L (n={n_by_line.get(ln, 0):,})")
    long = []
    for r in rows:
        ln = int(r["line"])
        series = f"{ln}L (n={n_by_line.get(ln, 0):,})"
        long.append({
            "drug": r["drug"], "line": ln, "series": series,
            "pct": float(r["pct_of_line"]), "n_patients": int(r["n_patients"]),
        })
    df = pd.DataFrame(long)
    rank = df.groupby("drug")["n_patients"].sum().sort_values(ascending=False)
    y_order = [d for d in rank.index.tolist() if d in drugs]
    spec = charts._base(df, "Molecule share by line of therapy", None)
    spec["height"] = {"step": 28}
    spec["mark"] = {"type": "bar", "cornerRadiusEnd": 0}
    spec["encoding"] = {
        "y": {"field": "drug", "type": "ordinal", "title": None, "sort": y_order,
              "axis": {"labelLimit": 180}},
        "yOffset": {"field": "series", "type": "nominal", "sort": series_order},
        "x": {"field": "pct", "type": "quantitative", "title": "% of patients in line",
              "scale": {"domain": [0, 100]}, "axis": {"tickCount": 6, "values": [0, 20, 40, 60, 80, 100]}},
        "color": {"field": "series", "type": "nominal", "title": None, "sort": series_order,
                  "scale": {"domain": series_order, "range": series_range[: len(series_order)]},
                  "legend": {"orient": "bottom", "direction": "horizontal"}},
        "tooltip": charts._tooltip([("drug", "nominal", None), ("series", "nominal", None),
                                    ("pct", "quantitative", ".1f"),
                                    ("n_patients", "quantitative", ",.0f")]),
    }
    return _clean_chart(spec)


_TAB20 = [
    "#1f77b4", "#aec7e8", "#ff7f0e", "#ffbb78", "#2ca02c", "#98df8a",
    "#d62728", "#ff9896", "#9467bd", "#c5b0d5", "#8c564b", "#c49c94",
    "#e377c2", "#f7b6d2", "#7f7f7f", "#c7c7c7", "#bcbd22", "#dbdb8d",
    "#17becf", "#9edae5",
]


def _lot_flow_chart(pack: dict | None = None) -> dict | None:
    """1L → 2L alluvial of regimens that contain pembrolizumab, nivolumab or atezolizumab."""
    pack = pack if pack is not None else _nsclc_notebook_lot()
    flow = pack.get("l1_l2_flow") or {}
    rows = flow.get("flows") or []
    tot = int(flow.get("n_patients") or 0)
    if not rows or tot <= 0:
        return None
    f12 = pd.DataFrame(rows)
    left = f12.groupby("L1")["n"].sum().sort_values(ascending=False)
    right = f12.groupby("L2")["n"].sum().sort_values(ascending=False)
    gap, scale = 0.010, 1 - 0.010 * max(len(left), len(right))
    if scale <= 0:
        scale = 0.5

    def layout(s):
        pos, y = {}, 0.0
        for k, v in s.items():
            h = float(v) / tot * scale
            pos[k] = (y, h)
            y += h + gap
        return pos

    l_pos, r_pos = layout(left), layout(right)
    colors = {k: _TAB20[i % 20] for i, k in enumerate(left.index)}
    off_l = {k: v[0] for k, v in l_pos.items()}
    off_r = {k: v[0] for k, v in r_pos.items()}
    xs = np.linspace(0, 1, 40)
    sm = (1 - np.cos(xs * np.pi)) / 2
    ribbons = []
    for i, r in enumerate(f12.itertuples()):
        h = float(r.n) / tot * scale
        y0, y1 = off_l[r.L1], off_r[r.L2]
        fid = f"{i}:{r.L1}→{r.L2}"
        for x, s in zip(xs, sm):
            ribbons.append({
                "id": fid, "L1": r.L1, "L2": r.L2, "n": int(r.n),
                "x": 0.325 + float(x) * 0.35,
                "ylo": y0 + float(s) * (y1 - y0),
                "yhi": y0 + h + float(s) * (y1 - y0),
            })
        off_l[r.L1] += h
        off_r[r.L2] += h
    left_nodes, right_nodes = [], []
    for k, (y, h) in l_pos.items():
        left_nodes.append({"side": "L1", "regimen": k, "label": f"{k[:60]}  ({int(left[k]):,})",
                           "x0": 0.30, "x1": 0.325, "y": y, "y2": y + h, "yMid": y + h / 2,
                           "color": colors[k], "n": int(left[k])})
    for k, (y, h) in r_pos.items():
        right_nodes.append({"side": "L2", "regimen": k, "label": f"{k[:60]}  ({int(right[k]):,})",
                            "x0": 0.675, "x1": 0.70, "y": y, "y2": y + h, "yMid": y + h / 2,
                            "color": "#999999", "n": int(right[k])})
    nodes = left_nodes + right_nodes
    n_nodes = max(len(l_pos), len(r_pos))
    height = max(520, 32 * n_nodes + 80)
    side = flow.get("side") or "either"
    n_tr = int(flow.get("n_transitions") or len(f12))
    spec = {
        "$schema": charts.SCHEMA,
        "title": {
            "text": "1L → 2L flow — regimens containing pembrolizumab, nivolumab or atezolizumab",
            "subtitle": f"(filter={side}; n={tot:,} patients, {n_tr} transitions)",
        },
        "width": "container",
        "height": height,
        "padding": {"left": 8, "right": 8, "top": 4, "bottom": 8},
        "autosize": {"type": "fit", "contains": "padding"},
        "layer": [
            {
                "data": {"values": json_safe(ribbons)},
                "mark": {"type": "area", "opacity": 0.5, "interpolate": "monotone", "clip": False,
                         "line": False},
                "encoding": {
                    "x": {"field": "x", "type": "quantitative",
                          "scale": {"domain": [-0.42, 1.42], "nice": False}, "axis": None},
                    "y": {"field": "ylo", "type": "quantitative", "stack": None,
                          "scale": {"domain": [-0.02, 1.02], "nice": False}, "axis": None},
                    "y2": {"field": "yhi"},
                    "color": {"field": "L1", "type": "nominal", "legend": None,
                              "scale": {"domain": list(left.index),
                                        "range": [_TAB20[i % 20] for i in range(len(left))]}},
                    "detail": {"field": "id", "type": "nominal"},
                    "tooltip": charts._tooltip([("L1", "nominal", None), ("L2", "nominal", None),
                                                ("n", "quantitative", ",.0f")]),
                },
            },
            {
                "data": {"values": json_safe(nodes)},
                "mark": {"type": "rect", "clip": False, "strokeWidth": 0},
                "encoding": {
                    "x": {"field": "x0", "type": "quantitative"},
                    "x2": {"field": "x1"},
                    "y": {"field": "y", "type": "quantitative"},
                    "y2": {"field": "y2"},
                    "color": {"field": "color", "type": "nominal", "scale": None, "legend": None},
                    "tooltip": charts._tooltip([("side", "nominal", None), ("regimen", "nominal", None),
                                                ("n", "quantitative", ",.0f")]),
                },
            },
            {
                "data": {"values": json_safe(left_nodes)},
                "mark": {"type": "text", "clip": False, "align": "right", "baseline": "middle",
                         "dx": -6, "fontSize": 11, "color": "#3a3936"},
                "encoding": {
                    "x": {"field": "x0", "type": "quantitative"},
                    "y": {"field": "yMid", "type": "quantitative"},
                    "text": {"field": "label", "type": "nominal"},
                },
            },
            {
                "data": {"values": json_safe(right_nodes)},
                "mark": {"type": "text", "clip": False, "align": "left", "baseline": "middle",
                         "dx": 6, "fontSize": 11, "color": "#3a3936"},
                "encoding": {
                    "x": {"field": "x1", "type": "quantitative"},
                    "y": {"field": "yMid", "type": "quantitative"},
                    "text": {"field": "label", "type": "nominal"},
                },
            },
        ],
        "resolve": {"scale": {"color": "independent"}},
    }
    return _clean_chart(spec)


def _incidence_prevalence_chart() -> dict | None:
    """100% mix of patients new to NSCLC vs already in NSCLC, by month (not the brand picker)."""
    mix = query("""
        with nsclc as (
          select distinct patient_id, date_trunc('month', service_date) as mo
          from fact_dx_event
          where dx_code like 'C34%'
        ),
        first as (
          select patient_id, min(mo) as first_mo from nsclc group by 1
        )
        select strftime(d.mo, '%Y-%m') as period,
               count(*) filter (where d.mo = f.first_mo) as incidence,
               count(*) filter (where d.mo > f.first_mo) as prevalence
        from nsclc d
        join first f using (patient_id)
        group by 1
        order by 1""")
    rows = []
    for r in mix.itertuples():
        inc, prev = int(r.incidence), int(r.prevalence)
        if inc + prev == 0:
            continue
        rows += [
            {"period": r.period, "segment": "Incidence", "patients": inc, "_stack_order": 0},
            {"period": r.period, "segment": "Prevalence", "patients": prev, "_stack_order": 1},
        ]
    if not rows:
        return None
    long = pd.DataFrame(rows)
    periods = sorted(long["period"].unique())
    spec = charts._base(long, "Incidence and Prevalence of NSCLC", None)
    spec["mark"] = {"type": "bar", "cornerRadiusEnd": 0}
    spec["encoding"] = {
        "x": {"field": "period", "type": "ordinal", "title": None, "sort": periods, "axis": {"labelAngle": 0}},
        "y": {"field": "patients", "type": "quantitative", "stack": "normalize",
              "title": "Share of NSCLC patients", "axis": {"format": ".0%"}},
        "color": {"field": "segment", "type": "nominal", "title": None,
                  "scale": {"domain": ["Incidence", "Prevalence"],
                            "range": [charts.CATEGORICAL[0], charts.CATEGORICAL[1]]},
                  "legend": {"orient": "bottom", "direction": "horizontal"}},
        "order": {"field": "_stack_order", "type": "quantitative"},
        "tooltip": charts._tooltip([("period", "ordinal", None), ("segment", "nominal", None),
                                    ("patients", "quantitative", ",.0f")]),
    }
    return _clean_chart(spec)


def _brand_month_chart(families: list[str]) -> dict:
    """Monthly C34 patients treated with each IO family (IV + SC)."""
    names = _io_product_names(families)
    df = query(f"""
        with nsclc as (
          select distinct patient_id, date_trunc('month', service_date) as mo
          from fact_dx_event where {_C34}
        )
        select strftime(n.mo, '%Y-%m') as period, {_IO_FAMILY_SQL} as product,
               count(distinct d.patient_id) as patients
        from fact_drug_event d
        join nsclc n on n.patient_id = d.patient_id
                    and n.mo = date_trunc('month', d.service_date)
        where d.product = any($ps)
        group by 1, 2""", {"ps": names})
    months = sorted(df["period"].unique()) if len(df) else []
    idx = pd.MultiIndex.from_product([months, families], names=["period", "product"])
    df = (df.set_index(["period", "product"]).reindex(idx, fill_value=0).reset_index()
          if months else pd.DataFrame(columns=["period", "product", "patients"]))
    spec = charts.line(df, "period", "patients", "Month on Month - Unique Patient Count by Drug",
                       color="product", domain=families, x_title="", y_title="Patients")
    spec["mark"] = {"type": "line", "point": {"filled": True, "size": 90, "strokeWidth": 1.5}}
    spec["encoding"]["shape"] = {
        "field": "product", "type": "nominal", "title": None,
        "scale": {"domain": families, "range": ["circle", "square", "triangle-up"]},
        "legend": {"orient": "bottom", "direction": "horizontal", "columns": 3}}
    spec["encoding"]["color"]["legend"] = {"orient": "bottom", "direction": "horizontal", "columns": 3}
    spec["encoding"]["color"]["scale"] = {
        "domain": families,
        "range": [IO_FAMILY_PALETTE[b][0] for b in families],
    }
    return _clean_chart(spec)


def _brand_source_chart(families: list[str]) -> dict | None:
    """Monthly stacked bars: New (first anticancer drug), Continued (last intake was this brand), Switch (in)."""
    sc_forms = [config.IO_BRANDS[b]["sc_form"] for b in families]
    src = query(f"""
        with nsclc as (
          select distinct patient_id from fact_dx_event where {_C34}
        ),
        rx as (
          select d.patient_id, d.service_date, d.product_form, {_IO_FAMILY_SQL} as family
          from fact_drug_event d
          join nsclc using (patient_id)
          where d.therapy_group in ('LUNG_SYSTEMIC', 'OTHER_ONCOLOGY')
        ),
        ordered as (
          select *, lag(family) over (partition by patient_id order by service_date, family) as last_family
          from rx
        ),
        month_first as (
          select *, date_trunc('month', service_date) as mo,
                 row_number() over (
                   partition by patient_id, family, date_trunc('month', service_date)
                   order by service_date, family) as rn
          from ordered
          where family = any($ps)
        )
        select strftime(mo, '%Y-%m') as period, family as product,
               case when last_family is null then 'New'
                    when last_family = family then 'Continued'
                    else 'Switch' end as segment,
               count(*) as patients,
               count(*) filter (where product_form = any($sc)) as subq
        from month_first
        where rn = 1
        group by 1, 2, 3
        order by 1, 2, 3""", {"ps": families, "sc": sc_forms})
    segments = ["New", "Continued", "Switch"]
    if src.empty:
        return None
    periods = sorted(src["period"].unique())
    idx = pd.MultiIndex.from_product([periods, families, segments], names=["period", "product", "segment"])
    long = (src.set_index(["period", "product", "segment"]).reindex(idx, fill_value=0).reset_index())
    labels = {b: config.IO_BRANDS[b]["label"] for b in families}
    phrase = {"New": "New to {}", "Continued": "Continuing on {}", "Switch": "Switched to {}"}
    long["_stack_order"] = long["segment"].map({s: i for i, s in enumerate(segments)})
    long["series"] = [phrase[seg].format(labels[prod])
                      for prod, seg in zip(long["product"], long["segment"])]
    switch = long["segment"] == "Switch"
    long["SUBQ"] = pd.Series(np.where(switch, long["subq"].astype(int), pd.NA), dtype="object")
    long["IV"] = pd.Series(np.where(switch, (long["patients"] - long["subq"]).astype(int), pd.NA), dtype="object")
    long = long.drop(columns=["subq"])
    domain, rng = [], []
    for brand in families:
        for i, seg in enumerate(segments):
            domain.append(phrase[seg].format(labels[brand]))
            rng.append(IO_FAMILY_PALETTE[brand][i])
    spec = charts._base(long, "Month on Month - Source of Business by Drug", None)
    spec["mark"] = {"type": "bar", "cornerRadiusEnd": 0}
    spec["encoding"] = {
        "x": {"field": "period", "type": "ordinal", "title": None, "sort": periods, "axis": {"labelAngle": 0}},
        "xOffset": {"field": "product", "type": "nominal", "sort": families},
        "y": {"field": "patients", "type": "quantitative", "stack": "normalize",
              "title": "Share of brand patients", "axis": {"format": ".0%"}},
        "color": {"field": "series", "type": "nominal", "title": None,
                  "scale": {"domain": domain, "range": rng},
                  "legend": {"orient": "bottom", "direction": "horizontal", "columns": 9,
                             "labelFontSize": 10, "symbolSize": 48, "columnPadding": 6,
                             "rowPadding": 0, "labelLimit": 180, "padding": 4}},
        "order": {"field": "_stack_order", "type": "quantitative"},
        "tooltip": charts._tooltip([("period", "ordinal", None), ("series", "nominal", None),
                                    ("patients", "quantitative", ",.0f"),
                                    ("IV", "quantitative", ",.0f"), ("SUBQ", "quantitative", ",.0f")]),
    }
    spec["encoding"]["tooltip"][1]["title"] = None
    spec["usermeta"] = {"skipNullTooltip": True}
    return _clean_chart(spec)
