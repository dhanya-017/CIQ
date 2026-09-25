"""Build the CIQ analytics warehouse (DuckDB) from the configured claims extract (ciq.config.SOURCE).

Layers
------
src_*     the complete source extract, cleaned (v2: src_master_nsclc)
ref_*     curated reference data (drug dictionary, code descriptions, enrichment, data coverage)
fact_*    event-level facts, one row per patient/date/entity (deduplicated)
dim_*     conformed dimensions (patient, hcp)
lot_*     derived line-of-therapy tables
fn_*      stored table macros (see ciq/sql/procedures.sql) used as agent tools

Run:  python -m ciq.build.build_warehouse
"""
from __future__ import annotations

import re
import time
from datetime import datetime

import duckdb
import pandas as pd

from ciq import config
from ciq.build.io_cohort import build_io_cohort
from ciq.build.lot import build_lines_of_therapy
from ciq.build.sources import stage


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Reference: drug dictionary + code -> molecule map
# ---------------------------------------------------------------------------
def load_drug_reference() -> pd.DataFrame:
    ref = pd.read_csv(config.DRUG_REFERENCE_CSV, dtype=str).fillna("")
    ref["default_days_supply"] = ref["default_days_supply"].astype(int)
    ref["priority"] = range(len(ref))
    return ref


def build_drug_map(con: duckdb.DuckDBPyConnection, ref: pd.DataFrame) -> pd.DataFrame:
    """Map every distinct (code, prod_name, ndc_desc) drug-line signature to a molecule.

    HCPCS code match wins; otherwise the first regex (in reference order) that matches the
    product name / NDC description wins. Unmapped J9xxx codes fall back to a generic
    'OTHER ANTINEOPLASTIC' so no chemo line is silently dropped.
    """
    sigs = con.sql("""
        select distinct
               case when source_table = 'RX' then 'NDC' else code_system end as code_system,
               case when source_table = 'RX' then null else code end as code,
               code_short_desc, prod_name, ndc_desc
        from stg_claims
        where is_line_anchor = 'Y' and code_type in ('PX', 'RX')
          and (source_table = 'RX' or code_system = 'HCPCS' or prod_name is not null)
    """).df()

    code_to_row = {}
    for _, r in ref.iterrows():
        for code in filter(None, r["hcpcs_codes"].split(";")):
            code_to_row[code.strip()] = r
    patterns = [(re.compile(r["name_regex"], re.I), r) for _, r in ref.iterrows() if r["name_regex"]]

    out = []
    for s in sigs.itertuples(index=False):
        # pandas 3 string columns use NaN (truthy float) for nulls; normalise to None
        s = s._replace(**{f: v if isinstance(v, str) else None
                          for f, v in (("code", s.code), ("code_short_desc", s.code_short_desc))})
        hit = code_to_row.get(s.code) if s.code else None
        match_type = "HCPCS" if hit is not None else None
        if hit is None:
            text = " | ".join(x for x in (s.prod_name, s.ndc_desc) if isinstance(x, str))
            # topical creams (e.g. fluorouracil cream) are dermatology, not systemic therapy
            if text and not re.search(r"\bcream\b", text, re.I):
                for pat, r in patterns:
                    if pat.search(text):
                        hit, match_type = r, "NAME"
                        break
        if hit is None and s.code and s.code.startswith("J9") and s.code != "J9999":
            out.append(dict(code_system=s.code_system, code=s.code, prod_name=s.prod_name, ndc_desc=s.ndc_desc,
                            molecule=(s.code_short_desc or s.code).lower(), product="OTHER ANTINEOPLASTIC",
                            drug_class="OTHER ANTINEOPLASTIC", therapy_group="OTHER_ONCOLOGY",
                            default_days_supply=21, match_type="J9_FALLBACK"))
            continue
        if hit is None:
            continue
        out.append(dict(code_system=s.code_system, code=s.code, prod_name=s.prod_name, ndc_desc=s.ndc_desc,
                        molecule=hit["molecule"], product=hit["product"], drug_class=hit["drug_class"],
                        therapy_group=hit["therapy_group"], default_days_supply=int(hit["default_days_supply"]),
                        match_type=match_type))
    return pd.DataFrame(out)


# ---------------------------------------------------------------------------
# Main build
# ---------------------------------------------------------------------------
def build() -> None:
    t0 = time.time()
    config.WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    if config.WAREHOUSE_DB.exists():
        config.WAREHOUSE_DB.unlink()
    con = duckdb.connect(str(config.WAREHOUSE_DB))
    stage_info = stage(con, log)
    product_form_sql = _product_form_sql()

    # ---------------- reference ----------------
    log("Loading drug reference + building drug map")
    ref = load_drug_reference()
    con.register("ref_df", ref.drop(columns=["priority"]))
    con.execute("create table ref_drug as select * from ref_df")
    drug_map = build_drug_map(con, ref)
    con.register("drug_map_df", drug_map)
    con.execute("create table ref_drug_code_map as select * from drug_map_df")
    log(f"  mapped {len(drug_map):,} drug signatures -> {drug_map['molecule'].nunique()} molecules")

    # ---------------- fact_drug_event ----------------
    log("Building fact_drug_event")
    con.execute(f"""
        create table fact_drug_event as
        with lines as (
            select c.patient_id, c.service_date, c.source_table as source,
                   c.claim_id, c.source_line_id,
                   case when c.source_table = 'RX' then 'NDC' else c.code_system end as code_system,
                   case when c.source_table = 'RX' then null else c.code end as code,
                   c.prod_name, c.ndc_desc, c.ndc_cd,
                   case when c.source_table = 'RX' then c.provider_id
                        else coalesce(c.provider_rendering_id, c.provider_billing_id) end as hcp_id,
                   case when c.source_table = 'RX' then c.payer_plan_id else c.pri_payer_plan_id end as payer_plan_id,
                   c.most_freq_rx_mop as payment_type_raw,
                   c.days_supply as rx_days_supply,
                   coalesce(c.dx_unit_of_svc_amt, c.quantity) as units,
                   c.svc_bill_amt, c.place_of_svc_cd, c.rx_claim_type, c.sob as rx_sob, c.refill_code
            from stg_claims c
            where c.is_line_anchor = 'Y' and c.code_type in ('PX', 'RX')
              and (c.source_table = 'MX' or c.rx_claim_type = 'PD')   -- paid pharmacy claims only
        ),
        mapped as (
            select l.*, m.molecule, m.product, m.drug_class, m.therapy_group, m.default_days_supply,
                   {product_form_sql} as product_form
            from lines l
            join ref_drug_code_map m
              on m.code_system = l.code_system
             and m.code is not distinct from l.code
             and m.prod_name is not distinct from l.prod_name
             and m.ndc_desc is not distinct from l.ndc_desc
        )
        -- one administration/fill per patient, day and molecule
        select patient_id, service_date, molecule, product, drug_class, therapy_group,
               arg_min(source, source_line_id) as source,
               arg_min(claim_id, source_line_id) as claim_id,
               arg_min(code, source_line_id) as code,
               arg_min(hcp_id, (case when hcp_id is null then '1' else '0' end) || source_line_id) as hcp_id,
               arg_min(payer_plan_id, (case when payer_plan_id is null then '1' else '0' end) || source_line_id) as payer_plan_id,
               arg_min(payment_type_raw, (case when payment_type_raw is null then '1' else '0' end) || source_line_id) as payment_type_raw,
               greatest(coalesce(max(rx_days_supply), 0), max(default_days_supply)) as days_supply,
               sum(units) as units,
               sum(svc_bill_amt) as billed_amt,
               any_value(place_of_svc_cd) as place_of_svc_cd,
               any_value(rx_sob) as rx_sob,
               min(refill_code) as refill_code,
               arg_max(product_form, product_form <> product) as product_form,   -- SC form wins if both same day
               count(*) as n_claim_lines
        from mapped
        group by patient_id, service_date, molecule, product, drug_class, therapy_group
    """)

    # ---------------- fact_dx_event ----------------
    log("Building fact_dx_event")
    con.execute("""
        create table fact_dx_event as
        select patient_id, service_date, code as dx_code,
               any_value(code_short_desc) as dx_desc,
               left(replace(code, '.', ''), 3) as dx_category,
               min(code_position) as best_position,
               bool_or(source_table = 'RX') as from_rx,
               arg_min(coalesce(provider_rendering_id, provider_billing_id, provider_id), lpad(cast(code_position as varchar), 3, '0') || source_line_id) as hcp_id,
               count(*) as n_mentions
        from stg_claims
        where code_type = 'DX' and code is not null
        group by patient_id, service_date, code
    """)

    # ---------------- fact_px_event ----------------
    log("Building fact_px_event")
    con.execute("""
        create table fact_px_event as
        select patient_id, service_date, code as px_code, code_system,
               any_value(code_short_desc) as px_desc,
               any_value(claim_type) as claim_type,
               arg_min(coalesce(provider_rendering_id, provider_billing_id), source_line_id) as hcp_id,
               count(*) as n_lines, sum(svc_bill_amt) as billed_amt
        from stg_claims
        where source_table = 'MX' and code_type = 'PX'
        group by patient_id, service_date, code, code_system
    """)

    # ---------------- ref_data_coverage ----------------
    con.execute("""
        create table ref_data_coverage as
        select min(service_date) as data_start, max(service_date) as data_end,
               count(distinct patient_id) as n_patients,
               count(*) as n_source_rows,
               current_timestamp as built_at
        from stg_claims
    """)

    # ---------------- dim_patient ----------------
    log("Building dim_patient")
    con.execute(f"""
        create table dim_patient as
        with base as (
            select patient_id,
                   max(pat_age_yr_nbr) as age,
                   min(service_date) as first_claim_date,
                   max(service_date) as last_claim_date,
                   bool_or(source_table = 'MX') as has_mx,
                   bool_or(source_table = 'RX') as has_rx,
                   count(distinct source_line_id) as n_claim_lines
            from stg_claims group by patient_id
        ),
        mop as (   -- modal medical payment type per patient
            select patient_id, arg_max(payment_type_raw, lpad(cast(n as varchar), 10, '0') || payment_type_raw) as payment_type
            from (select patient_id, most_freq_rx_mop as payment_type_raw, count(*) n
                  from stg_claims where source_table = 'MX' and is_line_anchor = 'Y'
                    and most_freq_rx_mop is not null and most_freq_rx_mop not in ('Unknown', 'Unknown Medical')
                  group by 1, 2)
            group by patient_id
        ),
        dx as (
            select patient_id, min(service_date) as first_indication_dx_date,
                   count(distinct service_date) as n_indication_dx_days
            from fact_dx_event where dx_code like '{config.INDICATION_DX_PREFIX}%' group by 1
        ),
        tx as (
            select patient_id, min(service_date) as first_lung_tx_date, max(service_date) as last_lung_tx_date
            from fact_drug_event where therapy_group = 'LUNG_SYSTEMIC' group by 1
        )
        select b.patient_id,
               case when b.age between 1 and 110 then b.age end as age,
               case when b.age is null or b.age < 1 then 'Unknown'
                    when b.age < 18 then '<18'
                    when b.age >= 85 then '85+'
                    else cast(floor(b.age / 10) * 10 as int) || '-' || cast(floor(b.age / 10) * 10 + 9 as int)
               end as age_group,
               cast(null as varchar) as gender,          -- not available in source data
               coalesce(m.payment_type, 'Unknown') as payment_type,
               b.first_claim_date, b.last_claim_date,
               dx.first_indication_dx_date, coalesce(dx.n_indication_dx_days, 0) as n_indication_dx_days,
               tx.first_lung_tx_date, tx.last_lung_tx_date,
               tx.first_lung_tx_date is not null as is_treated,
               b.has_mx, b.has_rx, b.n_claim_lines
        from base b
        left join mop m using (patient_id)
        left join dx using (patient_id)
        left join tx using (patient_id)
    """)

    # payment type: medical claims carry it; pharmacy claims inherit the patient's modal medical payment type
    con.execute("""
        alter table fact_drug_event add column payment_type varchar;
        update fact_drug_event set payment_type = case
            when fact_drug_event.payment_type_raw is not null
                 and fact_drug_event.payment_type_raw not in ('Unknown', 'Unknown Medical')
                 then fact_drug_event.payment_type_raw
            else p.payment_type end
        from dim_patient p where p.patient_id = fact_drug_event.patient_id;
    """)

    # ---------------- dim_hcp ----------------
    log("Building dim_hcp")
    con.execute("""
        create table dim_hcp as
        select hcp_id,
               count(*) as n_drug_events,
               count(*) filter (where therapy_group = 'LUNG_SYSTEMIC') as n_lung_tx_events,
               count(distinct patient_id) as n_patients,
               count(distinct patient_id) filter (where therapy_group = 'LUNG_SYSTEMIC') as n_lung_tx_patients,
               min(service_date) as first_event_date, max(service_date) as last_event_date
        from fact_drug_event where hcp_id is not null group by hcp_id
    """)

    # ---------------- lines of therapy ----------------
    log("Building lines of therapy")
    data_start, data_end = con.execute("select data_start, data_end from ref_data_coverage").fetchone()
    events = con.execute(f"""
        select patient_id, service_date, molecule, product, drug_class, days_supply
        from fact_drug_event where therapy_group in {tuple(config.LOT_THERAPY_GROUPS)!r}
        order by patient_id, service_date
    """).df()
    lot, lot_drug = build_lines_of_therapy(events, pd.Timestamp(data_start), pd.Timestamp(data_end))
    con.register("lot_df", lot)
    con.register("lot_drug_df", lot_drug)
    con.execute("create table lot_line as select * from lot_df")
    con.execute("create table lot_line_drug as select * from lot_drug_df")
    con.execute(f"""
        alter table dim_patient add column clean_start boolean;
        alter table dim_patient add column max_line int;
        update dim_patient set
            clean_start = dim_patient.first_lung_tx_date >= c.data_start + {config.CLEAN_START_DAYS}
        from ref_data_coverage c;
        update dim_patient set max_line = l.max_line
        from (select patient_id, max(line_number) as max_line from lot_line group by 1) l
        where l.patient_id = dim_patient.patient_id;
    """)
    log(f"  {len(lot):,} lines for {lot['patient_id'].nunique():,} patients")

    # ---------------- final IO cohort: SOB + claim types ----------------
    log("Building fact_io_claim (IO brands only, with SOB and TRx/NRx/RRx/NBRx)")
    build_io_cohort(con, log)

    # ---------------- stored procedures (table macros) ----------------
    log("Installing stored functions")
    install_procedures(con)

    # ---------------- indexes-ish: sort for zone maps ----------------
    con.execute("create table ref_build_params as select * from (values "
                f"('lot_regimen_window_days', '{config.LOT_REGIMEN_WINDOW_DAYS}'),"
                f"('lot_gap_days', '{config.LOT_GAP_DAYS}'),"
                f"('lot_regimen_window_cycles', '{config.LOT_REGIMEN_WINDOW_CYCLES}'),"
                f"('clean_start_days', '{config.CLEAN_START_DAYS}'),"
                f"('indication_dx_prefix', '{config.INDICATION_DX_PREFIX}'),"
                f"('default_product', '{config.DEFAULT_PRODUCT}'),"
                f"('lot_therapy_groups', '{','.join(config.LOT_THERAPY_GROUPS)}'),"
                f"('cohort_dx_like', '{config.COHORT_DX_LIKE if stage_info['source'] == 'master_nsclc' else 'n/a'}'),"
                f"('sob_washout_days', '{config.SOB_WASHOUT_DAYS}'),"
                + ",".join(f"('{k}', '{v}')" for k, v in stage_info.items()) +
                ") t(param, value)")
    con.execute("checkpoint")

    for t in ["src_master_nsclc", "ref_drug", "ref_drug_code_map", "fact_drug_event", "fact_dx_event", "fact_px_event",
              "dim_patient", "dim_hcp", "lot_line", "lot_line_drug", "fact_io_claim"]:
        if not con.execute(f"select count(*) from duckdb_tables() where table_name = '{t}'").fetchone()[0]:
            continue
        n = con.execute(f"select count(*) from {t}").fetchone()[0]
        log(f"  {t:<20} {n:>12,} rows")
    con.close()
    log(f"Done in {time.time() - t0:.0f}s -> {config.WAREHOUSE_DB}")


def _product_form_sql() -> str:
    """SQL expression naming the product form: the SC form for IO brands billed as SC, else the product."""
    whens = []
    for brand, b in config.IO_BRANDS.items():
        codes = ", ".join(f"'{c}'" for c in b["sc_codes"])
        whens.append(f"when m.product = '{brand}' and (l.code in ({codes}) or upper(concat_ws(' ', l.prod_name, "
                     f"l.ndc_desc)) like '%{b['sc_name']}%') then '{b['sc_form']}'")
    return "case " + " ".join(whens) + " else m.product end"


def install_procedures(con: duckdb.DuckDBPyConnection) -> None:
    sql = (config.SQL_DIR / "procedures.sql").read_text(encoding="utf-8")
    con.execute(sql)


if __name__ == "__main__":
    build()
