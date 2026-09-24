"""Compare as a diagnostic: two sides on the same market, period and methodology, then what / where / who.

Sides can be brands, HCP cohorts, patient cohorts or time periods. Every comparison returns
  scorecard  - the same metrics for A and B with the difference (pp for shares, % for volumes)
  drivers    - where the gap comes from (HCP deciles, payer, line, treatment mix, HCP movement)
  opportunity- the HCPs behind the gap, ready to export or save as a cohort
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import date

import numpy as np
import pandas as pd

from ciq import config
from ciq.tools import charts
from ciq.tools.base import ToolResult, coverage, kpi, pct, query, resolve_product

A_COLOR, B_COLOR = charts.CATEGORICAL[0], charts.CATEGORICAL[1]
LUNG = "therapy_group = 'LUNG_SYSTEMIC'"
MEDICARE = ("Medicare", "Medicare Advantage", "Medicare A", "Medicare B")
MEDICAID = ("Mgd Medicaid", "FFS Medicaid")

PATIENT_COHORTS = {
    "all": ("All treated patients", "true"),
    "age_lt65": ("Patients under 65", "age < 65"),
    "age_65plus": ("Patients 65+", "age >= 65"),
    "medicare": ("Medicare (all forms)", "payment_type in ('Medicare', 'Medicare Advantage', 'Medicare A', 'Medicare B')"),
    "commercial": ("Commercial", "payment_type = 'Commercial'"),
    "medicaid": ("Medicaid", "payment_type in ('Mgd Medicaid', 'FFS Medicaid')"),
    "line1": ("Line 1 only", "max_line = 1"),
    "line2plus": ("Reached line 2+", "max_line >= 2"),
    "combo": ("Ever on a combination", "patient_id in (select patient_id from lot_line where n_drugs >= 2)"),
}


# ---------------------------------------------------------------------------- saved HCP cohorts
def _cohort_file():
    return config.DATA_DIR / "cohorts" / "cohorts.json"


def list_cohorts() -> list[dict]:
    f = _cohort_file()
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else []


def save_cohort(name: str, hcp_ids: list[str], definition: str, product: str | None = None) -> dict:
    ids = sorted({str(h) for h in hcp_ids if h})
    if not name.strip() or not ids:
        raise ValueError("a cohort needs a name and at least one HCP")
    cohort = {"id": uuid.uuid4().hex[:10], "name": name.strip()[:80], "kind": "hcp", "hcp_ids": ids,
              "definition": definition[:300], "product": product, "created_at": time.time()}
    f = _cohort_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(list_cohorts() + [cohort], indent=1), encoding="utf-8")
    return cohort


# ---------------------------------------------------------------------------- side resolution
def _hcp_table(product: str) -> pd.DataFrame:
    return query(f"""
        select hcp_id, count(*) as market_volume, count(*) filter (where product = $p) as p_volume
        from fact_drug_event where {LUNG} and hcp_id is not null group by hcp_id""", {"p": product})


def hcp_cohort_options(product: str) -> list[dict]:
    h = _hcp_table(product)
    hv = h["market_volume"].quantile(0.75)
    overall = h["p_volume"].sum() / h["market_volume"].sum()
    opts = [{"id": "all", "label": "All HCPs (benchmark)"},
            {"id": "high_high", "label": f"High-volume, high {product} share",
             "detail": f"top-quartile lung volume (>= {hv:.0f}) and {product} share >= {pct(overall)}"},
            {"id": "high_low", "label": f"High-volume, low {product} share",
             "detail": f"top-quartile lung volume (>= {hv:.0f}) and {product} share < {pct(overall)}"},
            {"id": "nonusers", "label": f"HCPs not using {product}", "detail": "treat lung cancer, no brand volume"}]
    opts += [{"id": f"decile:{d}", "label": f"Decile {d} ({product} volume)"} for d in range(1, 11)]
    opts += [{"id": f"saved:{c['id']}", "label": c["name"], "detail": c["definition"], "saved": True}
             for c in list_cohorts()]
    return opts


def _resolve_hcps(spec: str, product: str) -> tuple[str, list[str], str]:
    """-> (label, hcp_ids, definition)"""
    h = _hcp_table(product)
    hv = h["market_volume"].quantile(0.75)
    overall = h["p_volume"].sum() / h["market_volume"].sum()
    h["share"] = h["p_volume"] / h["market_volume"]
    if spec == "all":
        sel, label, d = h, "All HCPs", "every HCP with lung systemic therapy volume"
    elif spec == "high_high":
        sel = h[(h.market_volume >= hv) & (h.share >= overall)]
        label, d = f"High-volume, high {product} share", f"lung volume >= {hv:.0f}, {product} share >= {pct(overall)}"
    elif spec == "high_low":
        sel = h[(h.market_volume >= hv) & (h.share < overall)]
        label, d = f"High-volume, low {product} share", f"lung volume >= {hv:.0f}, {product} share < {pct(overall)}"
    elif spec == "nonusers":
        sel, label, d = h[h.p_volume == 0], f"HCPs not using {product}", f"lung HCPs with no {product} volume"
    elif spec.startswith("decile:"):
        n = int(spec.split(":", 1)[1])
        dec = query("select hcp_id from fn_hcp_product_share($p, p_method := 'volume') where decile = $d",
                    {"p": product, "d": n})
        sel = h[h.hcp_id.isin(dec.hcp_id)]
        label, d = f"Decile {n}", f"HCPs in {product} volume decile {n} (decile 1 = highest)"
    elif spec.startswith("saved:"):
        cid = spec.split(":", 1)[1]
        c = next((c for c in list_cohorts() if c["id"] == cid), None)
        if not c:
            raise ValueError(f"saved cohort '{cid}' not found")
        return c["name"], c["hcp_ids"], c["definition"]
    else:
        raise ValueError(f"unknown HCP cohort '{spec}'. Options: all, high_high, high_low, nonusers, decile:1-10, "
                         f"saved:<id>")
    if sel.empty:
        raise ValueError(f"HCP cohort '{label}' is empty")
    return label, sel.hcp_id.tolist(), d


def _month_range(spec: str) -> tuple[str, date, date]:
    """'2026-01..2026-03' -> (label, first day, first day after)."""
    try:
        lo, hi = (s.strip() for s in spec.split(".."))
        start = pd.Timestamp(lo + "-01")
        end = pd.Timestamp(hi + "-01") + pd.offsets.MonthBegin(1)
    except Exception:
        raise ValueError(f"time period '{spec}' must look like 2026-01..2026-03") from None
    if end <= start:
        raise ValueError(f"time period '{spec}' ends before it starts")
    label = start.strftime("%b") + ("" if lo == hi else "–" + pd.Timestamp(hi + "-01").strftime("%b")) + f" {start.year}"
    return label, start.date(), end.date()


# ---------------------------------------------------------------------------- scorecard helpers
def _row(section, metric, a, b, kind="int", higher_is_better=True):
    """kind: pct (a share; diff in pp) | pp (a change in share) | int | num | days"""
    if a is None or b is None or (isinstance(a, float) and np.isnan(a)) or (isinstance(b, float) and np.isnan(b)):
        diff, dfmt = None, "—"
    elif kind in ("pct", "pp"):
        diff = (b - a) * 100
        dfmt = f"{diff:+.1f}pp"
    else:
        diff = b - a
        rel = f" ({(b / a - 1) * 100:+.0f}%)" if a else ""
        dfmt = (f"{diff:+,.0f}" if kind in ("int", "days") else f"{diff:+,.1f}") + rel
    fmt = {"pct": lambda v: pct(v), "pp": lambda v: f"{v * 100:+.1f}pp", "int": lambda v: f"{v:,.0f}",
           "num": lambda v: f"{v:,.1f}",
           "days": lambda v: f"{v:,.0f} days"}[kind]
    return {"section": section, "metric": metric, "a": a, "b": b, "a_fmt": "—" if a is None else fmt(a),
            "b_fmt": "—" if b is None else fmt(b), "diff": diff, "diff_fmt": dfmt, "kind": kind,
            "higher_is_better": higher_is_better}


def _pay_group(t: str | None) -> str:
    return "Medicare" if t in MEDICARE else "Medicaid" if t in MEDICAID else "Commercial" if t == "Commercial" \
        else "Other / unknown"


def _diverging(df: pd.DataFrame, cat: str, val: str, title: str, subtitle: str, a_label: str, b_label: str) -> dict:
    """Horizontal bars of B - A; bars point to the side that is higher, coloured by that side."""
    d = df.copy()
    d["_side"] = np.where(d[val] >= 0, f"Higher in {b_label}", f"Higher in {a_label}")
    spec = charts._base(d, title, subtitle)
    spec["height"] = {"step": 24}
    enc = {"y": {"field": cat, "type": "nominal", "title": None, "sort": {"field": val, "order": "descending"},
                 "axis": {"labelLimit": 220}},
           "x": {"field": val, "type": "quantitative", "title": f"{b_label} minus {a_label} (pp)"},
           "color": {"field": "_side", "type": "nominal", "title": None,
                     "scale": {"domain": [f"Higher in {a_label}", f"Higher in {b_label}"], "range": [A_COLOR, B_COLOR]}},
           "tooltip": [{"field": cat, "type": "nominal"}, {"field": val, "type": "quantitative", "format": "+.1f",
                                                           "title": "difference (pp)"}]}
    spec["layer"] = [{"mark": {"type": "bar"}, "encoding": enc},
                     {"mark": {"type": "rule", "color": "#9aa3ad"}, "encoding": {"x": {"datum": 0}}}]
    return spec


def _context(extra: str = "") -> dict:
    cov = coverage()
    return {"market": config.INDICATION_NAME, "period": f"{cov['data_start']} to {cov['data_end']}",
            "patients": cov["n_patients"], "note": extra}


# ---------------------------------------------------------------------------- brand vs brand
def _brand_metrics(p: str) -> dict:
    from ciq.tools.analytics import _persistency_curve
    cov = coverage()
    m = query(f"""
        with t as (select count(distinct patient_id) n from fact_drug_event where {LUNG})
        select count(distinct patient_id) patients, count(*) volume, count(distinct hcp_id) hcps,
               count(distinct patient_id) / (select n from t) as share
        from fact_drug_event where product = $p""", {"p": p}).iloc[0].to_dict()
    idx = query("select count(*) n from fn_product_index($p) where index_date >= $d",
                {"p": p, "d": pd.Timestamp(cov["data_start"]) + pd.Timedelta(days=config.CLEAN_START_DAYS)})
    m["new_starts"] = int(idx.n.iloc[0])
    tr = query("select period, patient_share from fn_product_trend($p)", {"p": p})
    m["share_first"], m["share_last"] = tr.patient_share.iloc[0], tr.patient_share.iloc[-1]
    m["period_first"], m["period_last"] = tr.period.iloc[0], tr.period.iloc[-1]
    sbl = query("select * from fn_product_share_by_line($p)", {"p": p})
    l1 = sbl[sbl.line_bucket == "1"]
    later = sbl[sbl.line_bucket != "1"]
    m["line1_share"] = float(l1.product_share.iloc[0]) if len(l1) else None
    m["line2p_share"] = float(later.product_patients.sum() / later.line_patients.sum()) if len(later) else None
    combo = query("""select avg((l.n_drugs >= 2)::int) combo from lot_line l
                     where exists (select 1 from lot_line_drug d where d.patient_id = l.patient_id
                                   and d.line_number = l.line_number and d.product = $p)""", {"p": p})
    m["combo_rate"] = combo.combo.iloc[0]
    pts = query(f"""select avg(age) mean_age, avg((age >= 65)::int) filter (where age is not null) pct65,
                          avg((payment_type in {MEDICARE})::int) medicare, avg((payment_type = 'Commercial')::int) commercial
                   from dim_patient where patient_id in (select patient_id from fact_drug_event where product = $p)""",
                {"p": p}).iloc[0].to_dict()
    m.update(pts)
    try:
        curve, _, _ = _persistency_curve(p, 60, 30)
        r = curve[curve.day <= 180]
        m["persist180"] = float(r.pct_persistent.iloc[-1]) if len(r) and curve.day.max() >= 180 else None
    except Exception:
        m["persist180"] = None
    return m


def compare_brands(a: str, b: str) -> ToolResult:
    a, b = resolve_product(a), resolve_product(b)
    if a == b:
        raise ValueError("pick two different brands")
    ma, mb = _brand_metrics(a), _brand_metrics(b)
    growth_a, growth_b = ma["share_last"] - ma["share_first"], mb["share_last"] - mb["share_first"]
    rows = [
        _row("Market performance", "Patient share of treated market", ma["share"], mb["share"], "pct"),
        _row("Market performance", "Patients", ma["patients"], mb["patients"]),
        _row("Market performance", "Administrations / fills", ma["volume"], mb["volume"]),
        _row("Market performance", f"Share change {ma['period_first']} → {ma['period_last']}", growth_a, growth_b, "pp"),
        _row("Market performance", f"New-to-brand patients (after first {config.CLEAN_START_DAYS} days)",
             ma["new_starts"], mb["new_starts"]),
        _row("Prescribers", "HCPs using the brand", ma["hcps"], mb["hcps"]),
        _row("Prescribers", "Patients per HCP", ma["patients"] / ma["hcps"], mb["patients"] / mb["hcps"], "num"),
        _row("Treatment", "Share of line-1 patients", ma["line1_share"], mb["line1_share"], "pct"),
        _row("Treatment", "Share of line-2+ patients", ma["line2p_share"], mb["line2p_share"], "pct"),
        _row("Treatment", "Lines given in combination", ma["combo_rate"], mb["combo_rate"], "pct", None),
        _row("Treatment", "Persistent at 180 days", ma["persist180"], mb["persist180"], "pct"),
        _row("Patients", "Mean age", ma["mean_age"], mb["mean_age"], "num", None),
        _row("Patients", "Aged 65+", ma["pct65"], mb["pct65"], "pct", None),
        _row("Patients", "Medicare (all forms)", ma["medicare"], mb["medicare"], "pct", None),
        _row("Patients", "Commercial", ma["commercial"], mb["commercial"], "pct", None),
    ]
    score = pd.DataFrame(rows)

    # where: HCPs deciled on total lung volume (1 = highest), each brand's share of that volume
    h = query(f"""
        with h as (select hcp_id, count(*) mv, count(*) filter (where product = $a) av, count(*) filter (where product = $b) bv
                   from fact_drug_event where {LUNG} and hcp_id is not null group by 1)
        select *, ntile(10) over (order by mv desc, hcp_id) as decile from h""", {"a": a, "b": b})
    dec = h.groupby("decile").agg(hcps=("hcp_id", "count"), market_volume=("mv", "sum"),
                                  a_volume=("av", "sum"), b_volume=("bv", "sum")).reset_index()
    dec["a_share"], dec["b_share"] = dec.a_volume / dec.market_volume, dec.b_volume / dec.market_volume
    dec["gap_pp"] = (dec.b_share - dec.a_share) * 100
    dec["volume_gap"] = dec.b_volume - dec.a_volume
    total_gap = dec.volume_gap.abs().sum() or 1
    dec["pct_of_gap"] = dec.volume_gap.abs() / total_gap
    long = pd.concat([dec.assign(brand=a, share=dec.a_share), dec.assign(brand=b, share=dec.b_share)])
    long["decile"] = long["decile"].astype(str)

    payer = query(f"""
        select d.product, p.payment_type, count(distinct d.patient_id) pts
        from fact_drug_event d join dim_patient p using (patient_id)
        where d.product in ($a, $b) group by all""", {"a": a, "b": b})
    payer["group"] = payer.payment_type.map(_pay_group)
    pay = payer.groupby(["group", "product"]).pts.sum().unstack(fill_value=0)
    pay = (pay / pay.sum()).reindex(columns=[a, b], fill_value=0).reset_index()
    pay["diff_pp"] = (pay[b] - pay[a]) * 100

    # who: HCPs where B out-sells A (the opportunity for A)
    opp = h[h.bv > h.av].assign(a_share=lambda x: x.av / x.mv, b_share=lambda x: x.bv / x.mv,
                                b_lead=lambda x: x.bv - x.av).sort_values(["b_lead", "mv"], ascending=False)
    opp = opp.rename(columns={"mv": "market_volume", "av": f"{a.lower()}_volume", "bv": f"{b.lower()}_volume"})
    lead_total = opp.b_lead.sum() or 1
    top_n = max(1, int(np.ceil(len(opp) * 0.2)))
    conc = opp.b_lead.head(top_n).sum() / lead_total if len(opp) else 0

    lead, lag = (a, b) if ma["share"] >= mb["share"] else (b, a)
    top_dec = dec.reindex(dec.pct_of_gap.sort_values(ascending=False).index).head(3)
    ins = [f"{a} reaches {pct(ma['share'])} of treated patients vs {pct(mb['share'])} for {b} "
           f"({(mb['share'] - ma['share']) * 100:+.1f}pp for {b}).",
           f"The volume gap is concentrated in HCP deciles {', '.join(map(str, top_dec.decile))} "
           f"({pct(top_dec.pct_of_gap.sum())} of it); decile 1 = highest-volume HCPs.",
           f"{len(opp):,} HCPs use more {b} than {a}; the top 20% of them ({top_n:,}) account for {pct(conc)} of {b}'s lead."
           if len(opp) else f"No HCP uses more {b} than {a}."]
    biggest = score.dropna(subset=["diff"]).loc[lambda s: s.kind == "pct"].assign(ad=lambda s: s["diff"].abs()) \
        .sort_values("ad", ascending=False).head(1)
    if len(biggest):
        r = biggest.iloc[0]
        ins.append(f"Largest rate difference: {r.metric.lower()} ({r.a_fmt} vs {r.b_fmt}, {r.diff_fmt}).")

    return ToolResult(
        tool="compare_segments", title=f"{a} vs {b}: competitive comparison",
        subtitle="Same market, same period, same definitions; the difference column is B minus A",
        params={"kind": "brands", "a": a, "b": b},
        data={"scorecard": score.drop(columns=["higher_is_better"]), "gap_by_hcp_decile": dec,
              "payer_mix": pay, "opportunity_hcps": opp[["hcp_id", "market_volume", f"{a.lower()}_volume",
                                                          f"{b.lower()}_volume", "a_share", "b_share", "b_lead"]]},
        charts=[charts.grouped_bar(long, "decile", "share", "brand", f"{a} vs {b} share of HCP lung volume, by HCP decile",
                                   subtitle="HCPs deciled on total lung-therapy volume (1 = highest)", y_format=".0%",
                                   x_title="HCP decile", y_title="Share of the decile's volume", x_sort=[str(i) for i in range(1, 11)],
                                   group_sort=[a, b], domain=[a, b]),
                _diverging(pay.rename(columns={"group": "payer"}), "payer", "diff_pp", f"Payer mix: {b} minus {a}",
                           "Share of each brand's patients by payment type", a, b)],
        kpis=[kpi(f"{a} patient share", ma["share"], "pct", lead=ma["share"] >= mb["share"]),
              kpi(f"{b} patient share", mb["share"], "pct", lead=mb["share"] > ma["share"]),
              kpi("Gap", abs(ma["share"] - mb["share"]) * 100, "num", note=f"pp, {lead} ahead"),
              kpi(f"HCPs using more {b} than {a}", len(opp), note=f"top {top_n:,} = {pct(conc)} of the gap")],
        observations=ins[1:],
        insights=ins,
        caveats=["Patient share: a patient on both brands counts for both. HCP = rendering provider (medical) or "
                 "prescriber (pharmacy).",
                 "Persistency and share change are limited by the short data window."],
        meta={"compare": {"kind": "brands", "a": a, "b": b, "headline": "Patient share of treated market",
                          "context": _context(), "opportunity": {"count": len(opp), "label":
                          f"HCPs where {b} out-sells {a}", "concentration": conc,
                          "hcp_ids": opp.hcp_id.head(2000).tolist(), "product": a}}},
    )


# ---------------------------------------------------------------------------- segments (HCP / patient / time)
def _segment(kind: str, spec: str, product: str) -> dict:
    if kind == "hcp":
        label, ids, d = _resolve_hcps(spec, product)
        return {"label": label, "definition": d, "pred": "list_contains($ids, hcp_id)", "params": {"ids": ids},
                "hcp_ids": ids, "months": None}
    if kind == "patient":
        if spec not in PATIENT_COHORTS:
            raise ValueError(f"unknown patient cohort '{spec}'. Options: {list(PATIENT_COHORTS)}")
        label, cond = PATIENT_COHORTS[spec]
        return {"label": label, "definition": cond,
                "pred": f"patient_id in (select patient_id from dim_patient where {cond})", "params": {}, "months": None}
    if kind == "time":
        label, start, end = _month_range(spec)
        months = (end.year - start.year) * 12 + end.month - start.month
        return {"label": label, "definition": f"claims from {start} to {end - pd.Timedelta(days=1)}",
                "pred": "service_date >= $start and service_date < $end", "params": {"start": start, "end": end},
                "months": months}
    raise ValueError("kind must be brands, hcp, patient or time")


def _seg_query(seg: dict, sql: str, product: str, suffix: str) -> pd.DataFrame:
    pred = seg["pred"]
    for k in seg["params"]:
        pred = pred.replace(f"${k}", f"${k}{suffix}")
    sql = sql.replace("{pred}", pred)
    params = {f"{k}{suffix}": v for k, v in seg["params"].items()} | {"p": product}
    return query(sql, {k: v for k, v in params.items() if f"${k}" in sql})   # duckdb rejects unused params


def _seg_metrics(seg: dict, product: str, sfx: str) -> dict:
    base = f"select * from fact_drug_event where {LUNG} and {{pred}}"
    m = _seg_query(seg, f"""
        with e as ({base})
        select count(distinct patient_id) patients, count(*) volume, count(distinct hcp_id) hcps,
               count(distinct patient_id) filter (where product = $p) p_patients,
               count(*) filter (where product = $p) p_volume,
               count(distinct hcp_id) filter (where product = $p) p_hcps,
               (select count(*) from fn_product_index($p) i where exists (select 1 from e where e.patient_id = i.patient_id
                  and e.service_date = i.index_date and e.product = $p)) new_p
        from e""", product, sfx).iloc[0].to_dict()
    m.update(_seg_query(seg, f"""
        select avg(age) mean_age, avg((age >= 65)::int) filter (where age is not null) pct65,
               avg((payment_type in {MEDICARE})::int) medicare, avg((payment_type = 'Commercial')::int) commercial
        from dim_patient where patient_id in (select patient_id from ({base}))""", product, sfx).iloc[0].to_dict())
    mix = _seg_query(seg, f"select product, count(distinct patient_id) pts, count(*) vol from ({base}) group by 1",
                     product, sfx)
    mix["share"] = mix.pts / (m["patients"] or 1)
    m["mix"] = mix
    return m


def compare_segments(kind: str, a: str, b: str, product: str | None = None) -> ToolResult:
    """Diagnostic comparison of two sides. kind: brands | hcp | patient | time."""
    if kind == "brands":
        return compare_brands(a, b)
    product = resolve_product(product)
    sa, sb = _segment(kind, a, product), _segment(kind, b, product)
    if sa["label"] == sb["label"]:
        raise ValueError("pick two different sides to compare")
    ma, mb = _seg_metrics(sa, product, "_a"), _seg_metrics(sb, product, "_b")
    total_volume = query(f"select count(*) n from fact_drug_event where {LUNG}").n.iloc[0]
    share = lambda m: m["p_patients"] / m["patients"] if m["patients"] else None
    vshare = lambda m: m["p_volume"] / m["volume"] if m["volume"] else None
    # HCP cohorts are judged on share of the HCPs' volume (small prescribers inflate patient share)
    head, head_name = (vshare, "volume share") if kind == "hcp" else (share, "patient share")
    other, other_name = (share, "patient share") if kind == "hcp" else (vshare, "volume share")
    rows = [_row(f"{product} performance", f"{product} {head_name}", head(ma), head(mb), "pct"),
            _row(f"{product} performance", f"{product} {other_name}", other(ma), other(mb), "pct"),
            _row(f"{product} performance", f"{product} patients", ma["p_patients"], mb["p_patients"]),
            _row(f"{product} performance", f"{product} administrations / fills", ma["p_volume"], mb["p_volume"]),
            _row(f"{product} performance", f"New-to-{product} patients (first claim here)", ma["new_p"], mb["new_p"]),
            _row(f"{product} performance", f"HCPs using {product}", ma["p_hcps"], mb["p_hcps"])]
    rows += [_row("Market", "Treated lung patients", ma["patients"], mb["patients"], higher_is_better=None),
             _row("Market", "Lung-therapy volume", ma["volume"], mb["volume"], higher_is_better=None),
             _row("Market", "Treating HCPs", ma["hcps"], mb["hcps"], higher_is_better=None)]
    if kind == "time":
        rows.append(_row("Market", "Lung-therapy volume per month", ma["volume"] / sa["months"],
                         mb["volume"] / sb["months"], "num", None))
        rows.append(_row(f"{product} performance", f"{product} volume per month", ma["p_volume"] / sa["months"],
                         mb["p_volume"] / sb["months"], "num"))
    else:
        rows.append(_row("Market", "% of all lung-therapy volume", ma["volume"] / total_volume,
                         mb["volume"] / total_volume, "pct", None))
        rows.append(_row("Market", "Patients per HCP", ma["patients"] / max(ma["hcps"], 1),
                         mb["patients"] / max(mb["hcps"], 1), "num", None))
    top_comp = lambda m: m["mix"][m["mix"]["product"] != product].sort_values("pts", ascending=False).head(1)
    ca, cb = top_comp(ma), top_comp(mb)
    rows += [_row("Competition", f"Top competitor share ({ca['product'].iloc[0] if len(ca) else '—'} / "
                                 f"{cb['product'].iloc[0] if len(cb) else '—'})",
                  float(ca.share.iloc[0]) if len(ca) else None, float(cb.share.iloc[0]) if len(cb) else None, "pct", False)]
    rows += [_row("Patients", "Mean age", ma["mean_age"], mb["mean_age"], "num", None),
             _row("Patients", "Aged 65+", ma["pct65"], mb["pct65"], "pct", None),
             _row("Patients", "Medicare (all forms)", ma["medicare"], mb["medicare"], "pct", None),
             _row("Patients", "Commercial", ma["commercial"], mb["commercial"], "pct", None)]
    score = pd.DataFrame(rows)

    # where: treatment mix difference (patient share of each product, B - A)
    mix = ma["mix"][["product", "share"]].merge(mb["mix"][["product", "share"]], on="product", how="outer",
                                                  suffixes=("_a", "_b")).fillna(0)
    mix["diff_pp"] = (mix.share_b - mix.share_a) * 100
    mix = mix.reindex(mix.diff_pp.abs().sort_values(ascending=False).index).head(10)
    la, lb = sa["label"], sb["label"]
    chart_list = [_diverging(mix, "product", "diff_pp", f"Treatment mix: {lb} minus {la}",
                             "Share of each side's treated patients receiving the product", la, lb)]
    data = {"scorecard": score.drop(columns=["higher_is_better"]), "treatment_mix": mix}
    ins = []
    sd = (head(mb) or 0) - (head(ma) or 0)
    ins.append(f"{product} {head_name} is {pct(head(ma) or 0)} in {la} vs {pct(head(mb) or 0)} in {lb} "
               f"({sd * 100:+.1f}pp).")
    top_mix = mix.iloc[0] if len(mix) else None
    if top_mix is not None:
        side = lb if top_mix.diff_pp > 0 else la
        ins.append(f"Biggest treatment-mix difference: {top_mix['product']} ({top_mix.share_a:.0%} vs "
                   f"{top_mix.share_b:.0%}, more in {side}).")
    opportunity = None

    if kind == "time":   # who moved: HCP-level change in monthly brand volume
        hv = query(f"""
            select hcp_id,
                   count(*) filter (where product = $p and service_date >= $s1 and service_date < $e1) a_vol,
                   count(*) filter (where product = $p and service_date >= $s2 and service_date < $e2) b_vol
            from fact_drug_event where {LUNG} and hcp_id is not null group by 1 having a_vol + b_vol > 0""",
                   {"p": product, "s1": sa["params"]["start"], "e1": sa["params"]["end"],
                    "s2": sb["params"]["start"], "e2": sb["params"]["end"]})
        hv["a_pm"], hv["b_pm"] = hv.a_vol / sa["months"], hv.b_vol / sb["months"]
        hv["change_pm"] = hv.b_pm - hv.a_pm
        hv["movement"] = np.select([hv.a_vol == 0, hv.b_vol == 0, hv.change_pm > 0.25, hv.change_pm < -0.25],
                                   ["New prescribers", "Stopped prescribing", "Increased", "Decreased"], "Stable")
        mv = hv.groupby("movement").agg(hcps=("hcp_id", "count"), net_change_per_month=("change_pm", "sum")).reset_index()
        order = ["New prescribers", "Increased", "Stable", "Decreased", "Stopped prescribing"]
        mv = mv.set_index("movement").reindex(order).dropna().reset_index()
        data["hcp_movement"] = mv
        spec = charts._base(mv, f"Where {product}'s change came from: HCP movement", f"{la} → {lb}, "
                            f"change in {product} administrations per month")
        spec["layer"] = [{"mark": {"type": "bar"}, "encoding": {
            "x": {"field": "movement", "type": "nominal", "sort": order, "title": None},
            "y": {"field": "net_change_per_month", "type": "quantitative", "title": "Net change per month"},
            "color": {"condition": {"test": "datum.net_change_per_month >= 0", "value": charts.CATEGORICAL[2]},
                      "value": charts.CATEGORICAL[7]},
            "tooltip": [{"field": "movement"}, {"field": "hcps", "title": "HCPs"},
                        {"field": "net_change_per_month", "format": "+.1f"}]}},
            {"mark": {"type": "text", "dy": -6, "fontSize": 11, "color": "#52514e"},
             "encoding": {"x": {"field": "movement", "type": "nominal", "sort": order},
                          "y": {"field": "net_change_per_month", "type": "quantitative"},
                          "text": {"field": "hcps", "type": "quantitative", "format": ",", "title": "HCPs"}}}]
        chart_list.insert(0, spec)
        dec = hv[hv.change_pm < 0].sort_values("change_pm")
        opp_cols = dec.rename(columns={"a_pm": f"{la} per month", "b_pm": f"{lb} per month"})
        data["opportunity_hcps"] = opp_cols[["hcp_id", f"{la} per month", f"{lb} per month", "change_pm", "movement"]]
        grow = mv.set_index("movement").net_change_per_month
        ins.append(f"{int((hv.movement == 'New prescribers').sum())} HCPs started and "
                   f"{int((hv.movement == 'Stopped prescribing').sum())} stopped prescribing {product}; "
                   f"{int((hv.movement == 'Increased').sum())} increased and {int((hv.movement == 'Decreased').sum())} "
                   "decreased their monthly volume.")
        if len(grow):
            ins.append(f"Largest driver: {grow.abs().idxmax().lower()} ({grow[grow.abs().idxmax()]:+.1f} "
                       f"{product} administrations per month).")
        opportunity = {"count": len(dec), "label": f"HCPs whose monthly {product} volume fell", "product": product,
                       "hcp_ids": dec.hcp_id.head(2000).tolist(), "concentration": None}
    elif kind == "hcp":
        opportunity = {"count": len(sb["hcp_ids"]), "label": f"HCPs in {lb}", "product": product,
                       "hcp_ids": sb["hcp_ids"][:2000], "concentration": None}

    headline_a, headline_b = head(ma) or 0, head(mb) or 0
    return ToolResult(
        tool="compare_segments", title=f"{la} vs {lb}",
        subtitle=f"{ {'hcp': 'HCP cohorts', 'patient': 'Patient cohorts', 'time': 'Time periods'}[kind]} compared for "
                 f"{product}; the difference column is B minus A",
        params={"kind": kind, "a": a, "b": b, "product": product},
        data=data, charts=chart_list,
        kpis=[kpi(f"{product} {head_name}: {la}", headline_a, "pct", lead=headline_a >= headline_b),
              kpi(f"{product} {head_name}: {lb}", headline_b, "pct", lead=headline_b > headline_a),
              kpi("Gap", abs(sd) * 100, "num", note="pp"),
              kpi("Treated patients", f"{ma['patients']:,} vs {mb['patients']:,}", "text")],
        observations=ins[1:], insights=ins,
        caveats=[f"A: {la} = {sa['definition']}.", f"B: {lb} = {sb['definition']}.",
                 "Patient share = patients with the brand / treated lung patients on that side; volume share = "
                 "brand administrations / all lung-therapy administrations on that side."]
                + (["Periods of different length: compare the per-month rows. Early months include patients already on "
                    "therapy before the data starts."] if kind == "time" else []),
        meta={"compare": {"kind": kind, "a": la, "b": lb, "product": product,
                          "headline": f"{product} {head_name}", "context": _context(), "opportunity": opportunity}},
    )
