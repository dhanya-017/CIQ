"""Market Command Center: the home screen for a brand in its market.

Answers, from observed claims only: how big is the market, how is the brand doing, what changed (current window vs
the prior window of equal length), where is the opportunity, and what to investigate next. Every signal carries its
evidence and an action; nothing here is model-generated.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ciq import config
from ciq.tools import charts, compare
from ciq.tools.base import coverage, json_safe, pct, query, resolve_product

LUNG = "therapy_group = 'LUNG_SYSTEMIC'"
OTHER_INDICATIONS = ["Breast cancer", "Melanoma", "Colorectal cancer", "Multiple myeloma"]


def _windows(n: int) -> tuple[str, str, list[str]]:
    """Current = last n months of data, prior = the n months before -> ('YYYY-MM..YYYY-MM', ..., months)."""
    cov = coverage()
    months = [str(m) for m in pd.period_range(cov["data_start"], cov["data_end"], freq="M")]
    n = max(1, min(n, len(months) // 2))
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
               count(distinct patient_id) filter (where product = $p) p_patients,
               count(*) filter (where product = $p) p_volume,
               count(distinct hcp_id) filter (where product = $p) p_hcps,
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


def build(product: str | None = None, window: int = 3) -> dict:
    product = resolve_product(product)
    cov = coverage()
    pri, cur, months = _windows(window)
    lp, lc = _label(pri), _label(cur)
    ps, cs = _period_stats(pri, product), _period_stats(cur, product)
    drug_class = query("select any_value(drug_class) c from fact_drug_event where product = $p", {"p": product}).c.iloc[0]
    cls_name = _nice_class(drug_class)

    # ---------------- market snapshot (current window, change vs prior)
    market = [
        {"id": "patients", "label": "Treated patients", "value": cs["patients"], "format": "int",
         "delta": _delta(cs["patients"], ps["patients"], "rel") | {"coverage": True}},
        {"id": "volume", "label": "Therapy administrations", "value": cs["volume"], "format": "int",
         "delta": _delta(cs["volume"], ps["volume"], "rel") | {"coverage": True}},
        {"id": "hcps", "label": "Treating HCPs", "value": cs["hcps"], "format": "int",
         "delta": _delta(cs["hcps"], ps["hcps"], "rel") | {"coverage": True}},
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
         "delta": _delta(cs["share"], ps["share"], "pp"), "lead": True},
        {"id": "vshare", "label": f"{product} volume share", "value": cs["vshare"], "format": "pct",
         "delta": _delta(cs["vshare"], ps["vshare"], "pp")},
        {"id": "new", "label": f"New-to-{product} patients", "value": cs["p_new"], "format": "int",
         "delta": _delta(cs["p_new"], ps["p_new"], "rel")},
        {"id": "opportunity", "label": "HCP opportunity", "value": len(opp), "format": "int",
         "note": f"high-volume HCPs, {product} share < {pct(overall)} (whole window)"},
    ]

    # ---------------- what matters now (observed signals, each with evidence and an action)
    signals = []
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

    # competitor movement among high-volume HCPs (in-class share, active in both windows)
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
    comp_gain = query(f"""
        select product,
          count(*) filter (where service_date >= $s2 and service_date < $e2) / $n2
          - count(*) filter (where service_date >= $s1 and service_date < $e1) / $n1 as gain_pm
        from fact_drug_event where drug_class = $c and product <> $p and hcp_id = any($ids) group by 1
        having gain_pm > 0 order by gain_pm desc""",
                      {"s1": s1, "e1": e1, "s2": s2, "e2": e2, "c": drug_class, "p": product,
                       "n1": window, "n2": window, "ids": shifted.hcp_id.tolist() or ["-"]})
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
    tp_long = pd.concat([top_products.assign(window=lp, share=top_products.share_a),
                         top_products.assign(window=lc, share=top_products.share_b)])
    deciles = query("select * from fn_hcp_product_share_deciles($p)", {"p": product}).assign(decile=lambda d: d.decile.astype(str))
    lines = query("select * from fn_product_share_by_line($p)", {"p": product}).assign(line=lambda d: "Line " + d.line_bucket)
    movement_chart = next((c for c in tc.charts if "HCP movement" in c.get("title", {}).get("text", "")), None)
    in_class = rivals.assign(product=rivals["product"]).sort_values("delta_pp")
    in_class = pd.concat([in_class, shares[shares["product"] == product]])

    top_chart = charts.grouped_bar(tp_long, "product", "share", "window", f"Top products: {lp} vs {lc}",
                                   subtitle="Share of treated lung-cancer patients", y_format=".0%", x_title="",
                                   y_title="Patient share", x_sort=top_products["product"].tolist(), group_sort=[lp, lc],
                                   domain=[lp, lc])
    top_chart["encoding"]["x"]["axis"] = {"labelAngle": -35, "labelLimit": 110}

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
            compare._diverging(in_class, "product", "delta_pp", f"Share change, {lp} → {lc}",
                               f"{cls_name[:1].upper() + cls_name[1:]}s, change in share of treated patients", lp, lc,
                               names=("Lost share", "Gained share"))],
            "questions": [f"Compare {product} with its top competitors",
                          f"Which HCPs use more of a competitor than {product}?",
                          f"Where is {product} losing patients to competitors?"]},
    }

    return json_safe({
        "context": {"indication": config.INDICATION_NAME, "indications": [config.INDICATION_NAME] + OTHER_INDICATIONS,
                    "product": product, "drug_class": drug_class, "geography": "United States",
                    "geography_note": "Regional views need a geography source (HCP address or patient ZIP3).",
                    "data_start": cov["data_start"], "data_end": cov["data_end"],
                    "window": window, "windows": [1, 2, 3], "current": {"spec": cur, "label": lc},
                    "prior": {"spec": pri, "label": lp}},
        "market": market, "market_note": "Growth partly reflects claims capture ramping up early in the data window.",
        "brand": brand, "signals": signals, "lenses": lenses,
        "notes": ["Claims capture ramps up in the first months of the data (Jan-Mar), so raw volume and patient growth "
                  "partly reflects data coverage; shares are the like-for-like comparison.",
                  f"Current = {lc}, prior = {lp} (equal-length windows)."],
    })
