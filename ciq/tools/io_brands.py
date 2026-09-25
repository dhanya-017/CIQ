"""IO brand dynamics: source of business and TRx/NRx/RRx/NBRx for Keytruda, Opdivo and Tecentriq (fact_io_claim)."""
from __future__ import annotations

import pandas as pd

from ciq import config
from ciq.tools import charts
from ciq.tools.base import ToolResult, call_fn, kpi, pct

BRANDS = list(config.IO_BRANDS)
SOB_ORDER = ["Initiated", "Switched from Competitor"] + \
            [f"{v['label']} to {v['sc_form'].title()}" for v in config.IO_BRANDS.values()] + ["Continuing"]

DEFINITIONS = [
    "Initiated: first IO-brand claim seen for the patient, or the same brand resumed after a gap of "
    f"{config.LOT_GAP_DAYS}+ days (re-initiation).",
    "Switched from Competitor: the patient's previous IO-brand claim was a different brand (Keytruda / Opdivo / "
    "Tecentriq).",
    "<Brand> to <Brand SC>: same brand, previous claim IV and this one subcutaneous (Keytruda Qlex, Opdivo Qvantig, "
    "Tecentriq Hybreza).",
    "Continuing: any other claim of the same brand.",
    "TRx = all claims; NBRx = new to brand (switched from a competitor, or first-ever use of the brand); NRx = new "
    "prescriptions (NBRx, re-initiations, IV-to-SC switches, the brand's first claim in a new line of therapy, and "
    "pharmacy fills with refill code 0); RRx = TRx - NRx.",
]


def _brand(brand: str | None) -> str | None:
    if not brand or brand.strip().upper() in ("ALL", "ANY", "*"):
        return None
    b = brand.strip().upper()
    for fam, v in config.IO_BRANDS.items():
        if b in (fam, v["sc_form"]) or b.startswith(fam):
            return fam
    raise ValueError(f"Unknown IO brand {brand!r}. Use one of {BRANDS} (their SC forms map to the same family) or ALL.")


def io_brand_dynamics(brand: str | None = None, grain: str = "month", exclude_washout: bool = False) -> ToolResult:
    fam = _brand(brand)
    grain = "week" if str(grain).lower().startswith("w") else "month"
    sob = call_fn("fn_io_sob", brand=fam, grain=grain, exclude_washout=exclude_washout)
    ct = call_fn("fn_io_claim_types", brand=fam, grain=grain, exclude_washout=exclude_washout)
    if sob.empty:
        raise ValueError("No IO-brand claims found (is the warehouse built from master_nsclc?)")
    periods = list(dict.fromkeys(sob.sort_values("period_start")["period"]))
    scope = config.IO_BRANDS[fam]["label"] if fam else "Keytruda, Opdivo, Tecentriq"
    unit = "week" if grain == "week" else "month"

    totals = sob.groupby("sob", as_index=False)["claims"].sum()
    totals["share"] = totals["claims"] / totals["claims"].sum()
    totals["order"] = totals["sob"].map({s: i for i, s in enumerate(SOB_ORDER)})
    totals = totals.sort_values("order").drop(columns="order")
    ct_tot = ct[["trx", "nrx", "rrx", "nbrx"]].sum()

    chart_list = []
    if fam:
        sob_p = sob.groupby(["period", "sob"], as_index=False)["claims"].sum()
        chart_list.append(charts.stacked_bar(
            sob_p, "period", "claims", "sob", f"{scope} source of business by {unit} (claims)",
            normalize=False, x_sort=periods, x_title="", y_title="Claims"))
        long = ct.melt(id_vars=["period"], value_vars=["trx", "nrx", "nbrx"], var_name="metric", value_name="claims")
        long["metric"] = long["metric"].str.upper().str.replace("X", "x")
        chart_list.append(charts.line(long, "period", "claims", f"{scope} TRx, NRx and NBRx by {unit}",
                                      color="metric", x_title="", y_title="Claims", domain=["TRx", "NRx", "NBRx"]))
    else:
        chart_list.append(charts.grouped_bar(
            ct, "period", "nbrx", "brand_family", f"NBRx (new-to-brand claims) by {unit}", x_title="",
            y_title="NBRx claims", x_sort=periods, group_sort=BRANDS, domain=BRANDS))
        mix = sob.groupby(["brand_family", "sob"], as_index=False)["claims"].sum()
        chart_list.append(charts.stacked_bar(
            mix, "brand_family", "claims", "sob", "Source of business mix by brand (share of claims)",
            normalize=True, x_sort=BRANDS, x_title="", y_title="Share of claims", horizontal=True))

    nbrx_share = ct_tot["nbrx"] / ct_tot["trx"] if ct_tot["trx"] else 0
    ins = [f"{r.sob}: {int(r.claims):,} claims ({pct(r.share)})" for r in totals.itertuples()]
    ins.append(f"TRx {int(ct_tot['trx']):,} = NRx {int(ct_tot['nrx']):,} + RRx {int(ct_tot['rrx']):,}; "
               f"NBRx {int(ct_tot['nbrx']):,} ({pct(nbrx_share)} of TRx).")
    if not fam:
        by = ct.groupby("brand_family")[["trx", "nbrx"]].sum()
        ins += [f"{config.IO_BRANDS[b]['label']}: TRx {int(r.trx):,}, NBRx {int(r.nbrx):,}" for b, r in by.iterrows()]
    caveats = [*DEFINITIONS,
               f"Data starts {pd.Timestamp(sob['period_start'].min()):%Y-%m-%d}: in the first "
               f"{config.SOB_WASHOUT_DAYS} days earlier IO use can't be seen, so 'Initiated' there includes "
               "patients who were already on therapy" + ("" if exclude_washout else
                                                         " (set exclude_washout=true to drop those claims)") + ".",
               "Final IO cohort: lines of therapy are built from all anticancer drugs first; then only Keytruda, "
               "Keytruda Qlex, Opdivo, Opdivo Qvantig, Tecentriq and Tecentriq Hybreza claims are kept."]
    wide = sob.pivot_table(index=["period_start", "period"] + ([] if fam else ["brand_family"]), columns="sob",
                           values="claims", aggfunc="sum", fill_value=0).reset_index()
    wide = wide[[c for c in wide.columns if c not in SOB_ORDER] + [s for s in SOB_ORDER if s in wide.columns]]
    return ToolResult(
        tool="io_brand_dynamics", title=f"{scope}: source of business and claim types by {unit}",
        subtitle="IO brand claims classified by source of business, with TRx / NRx / RRx / NBRx",
        params={"brand": fam or "ALL", "grain": grain, "exclude_washout": exclude_washout},
        data={"sob_by_period": wide, "claim_types_by_period": ct.drop(columns=["period_start"]), "sob_totals": totals},
        charts=chart_list,
        kpis=[kpi("TRx", int(ct_tot["trx"])), kpi("NBRx", int(ct_tot["nbrx"]), lead=True),
              kpi("NRx", int(ct_tot["nrx"])), kpi("NBRx share of TRx", nbrx_share, "pct")],
        insights=ins, caveats=caveats,
    )
