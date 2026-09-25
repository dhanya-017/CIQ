"""Source adapters: turn an input extract into the canonical `stg_claims` staging table.

Every adapter produces the same long-format staging layout the warehouse build reads (one row per claim-line
code: DX rows plus one PX/RX "anchor" row per line), so the build and everything downstream (stored functions,
tools, UI) are independent of the input format.

    claims_master  v1: Data/master/claims_master.parquet is already in this layout (a view over the file)
    master_nsclc   v2: Data/real_data/master_nsclc.csv (15 columns). The complete extract is loaded, cleaned, into
                   `src_master_nsclc`; fields the app needs but the extract lacks are enriched from:
                     age              raw MX extract (mx_nsclc.csv), then the v1 master     -> ref_patient_age
                     billing provider raw MX extract, by claim                               -> ref_claim_billing
                     dx descriptions  CMS ICD-10-CM (ciq/reference/icd10cm.csv), then v1 master
                     px descriptions  v1 master (CPT descriptions are AMA-licensed; many stay blank)
"""
from __future__ import annotations

import duckdb

from ciq import config

# Columns the build reads from stg_claims (the v1 master has more; they are simply not used).
STAGING_COLUMNS = [
    "source_table", "claim_id", "source_line_id", "patient_id", "service_date", "claim_type", "code_type",
    "code_position", "code", "code_system", "code_short_desc", "is_line_anchor", "ndc_cd", "ndc_desc", "prod_name",
    "pat_age_yr_nbr", "place_of_svc_cd", "provider_billing_id", "provider_rendering_id", "provider_referring_id",
    "provider_id", "dx_unit_of_svc_amt", "svc_bill_amt", "most_freq_rx_mop", "pri_payer_plan_id", "payer_plan_id",
    "rx_claim_type", "sob", "days_supply", "quantity", "refill_code",
]


def stage(con: duckdb.DuckDBPyConnection, log=print) -> dict:
    """Create `stg_claims` for the configured source. Returns facts about the load for ref_build_params."""
    if config.SOURCE == "claims_master":
        return stage_claims_master(con, log)
    if config.SOURCE == "master_nsclc":
        return stage_master_nsclc(con, log)
    raise ValueError(f"Unknown CIQ_SOURCE {config.SOURCE!r} (expected master_nsclc or claims_master)")


# ---------------------------------------------------------------------------
# v1: claims_master.parquet
# ---------------------------------------------------------------------------
def stage_claims_master(con, log=print) -> dict:
    p = config.MASTER_PARQUET
    if not p.exists():
        raise FileNotFoundError(f"{p} not found (CIQ_SOURCE=claims_master)")
    log(f"Staging v1 claims master ({p.name})")
    con.execute(f"create temp view stg_claims as select * from read_parquet('{p.as_posix()}')")
    return {"source": "claims_master", "source_file": p.name}


# ---------------------------------------------------------------------------
# v2: master_nsclc.csv
# ---------------------------------------------------------------------------
def _strip0(col: str) -> str:
    """'10567886.0' -> '10567886' (IDs that went through a float in the upstream notebook)."""
    return f"nullif(regexp_replace(trim({col}), '\\.0$', ''), '')"


def stage_master_nsclc(con, log=print) -> dict:
    src = config.MASTER_NSCLC_CSV
    if not src.exists():
        raise FileNotFoundError(f"{src} not found (CIQ_SOURCE=master_nsclc) - see Data/README.md")
    log(f"Loading complete extract {src.name} -> src_master_nsclc")
    ndc = f"lpad({_strip0('code')}, 11, '0')"
    con.execute(f"""
        create table src_master_nsclc as
        with raw as (
            select row_number() over () as row_id, *
            from read_csv('{src.as_posix()}', all_varchar = true, header = true, sample_size = -1)
        )
        select row_id,
               source,
               {_strip0('claim_id')}                                    as claim_id,
               {_strip0('patient_id')}                                  as patient_id,
               cast(try_strptime(svc_dt, '%Y-%m-%d %H:%M:%S.%g') as date) as service_date,
               nullif(nullif(upper(trim(diagnosis_code)), 'NAN'), '')  as diagnosis_code,
               case when source in ('RX', 'MX_RX') then {ndc}
                    else nullif(nullif(upper(trim(code)), 'NAN'), '') end as code,      -- NDC-11 or CPT/HCPCS
               nullif(trim(code_description), '')                       as code_description,
               coalesce({_strip0('rendering_provider_id')}, {_strip0('renderring_provider_id')}) as rendering_provider_id,
               {_strip0('referring_provider_id')}                       as referring_provider_id,
               {_strip0('plan_id')}                                     as plan_id,
               nullif(trim(plan_name), '')                              as plan_name,
               try_cast(days_supply as double)                          as days_supply,
               try_cast(quantity as double)                             as quantity,
               cast(try_cast(refill_code as double) as integer)         as refill_code,
               -- original values of the columns cleaned above, kept for traceability
               svc_dt as svc_dt_raw, code as code_raw,
               rendering_provider_id as rendering_provider_id_raw, renderring_provider_id as renderring_provider_id_raw
        from raw
    """)
    n_rows, n_pat = con.execute("select count(*), count(distinct patient_id) from src_master_nsclc").fetchone()
    log(f"  {n_rows:,} rows, {n_pat:,} patients")

    _enrichment(con, log)

    log(f"Staging cohort: patients with a diagnosis LIKE '{config.COHORT_DX_LIKE}'")
    # Built as a chain of temp tables: each step is seconds, but DuckDB plans the single combined query badly.
    con.execute(f"""
        create temp table stg_src as
        select * from src_master_nsclc
        where patient_id in (select distinct patient_id from src_master_nsclc
                             where diagnosis_code like '{config.COHORT_DX_LIKE}')
          -- MX_RX rows without an NDC only repeat the diagnoses of their MX_PX twin
          and not (source = 'MX_RX' and code is null)
    """)
    # the extract repeats each claim line once per diagnosis slot: rebuild a line key
    con.execute("""
        create temp table stg_keyed as
        select *, md5(concat_ws('|', source, claim_id, patient_id, service_date, coalesce(code, ''),
                      coalesce(code_description, ''), coalesce(rendering_provider_id, ''),
                      coalesce(referring_provider_id, ''), coalesce(plan_id, ''), coalesce(plan_name, ''),
                      coalesce(cast(quantity as varchar), ''), coalesce(cast(days_supply as varchar), ''),
                      coalesce(cast(refill_code as varchar), ''))) as source_line_id
        from stg_src
    """)
    con.execute("""
        create temp table stg_line as
        select source_line_id, any_value(source) as source, any_value(claim_id) as claim_id,
               any_value(patient_id) as patient_id, any_value(service_date) as service_date,
               any_value(code) as code, any_value(code_description) as code_description,
               any_value(rendering_provider_id) as rendering_provider_id,
               any_value(referring_provider_id) as referring_provider_id,
               any_value(plan_id) as plan_id, any_value(plan_name) as plan_name,
               any_value(quantity) as quantity, any_value(days_supply) as days_supply,
               any_value(refill_code) as refill_code
        from stg_keyed group by source_line_id
    """)
    # diagnosis slot order = upstream concat order (slot 1 block first, ...): use the first row_id
    con.execute("""
        create temp table stg_dx as
        select source_line_id, diagnosis_code,
               row_number() over (partition by source_line_id order by min(row_id)) as code_position
        from stg_keyed where diagnosis_code is not null
        group by source_line_id, diagnosis_code
    """)
    # enrich lines once (age, billing provider)
    con.execute("""
        create temp table stg_line_e as
        select l.*, age.age, bill.provider_billing_id
        from stg_line l
        left join ref_patient_age age using (patient_id)
        left join ref_claim_billing bill using (claim_id)
    """)
    common = """case when l.source = 'RX' then 'RX' else 'MX' end as source_table,
               l.claim_id, l.source_line_id, l.patient_id, l.service_date,
               cast(null as varchar) as claim_type"""
    tail = """l.age as pat_age_yr_nbr,
               cast(null as varchar) as place_of_svc_cd,
               case when l.source <> 'RX' then l.provider_billing_id end as provider_billing_id,
               case when l.source <> 'RX' then l.rendering_provider_id end as provider_rendering_id,
               l.referring_provider_id as provider_referring_id,
               case when l.source = 'RX' then l.rendering_provider_id end as provider_id,
               case when l.source <> 'RX' then l.quantity end as dx_unit_of_svc_amt,
               cast(null as double) as svc_bill_amt,
               l.plan_name as most_freq_rx_mop,
               case when l.source <> 'RX' then l.plan_id end as pri_payer_plan_id,
               case when l.source = 'RX' then l.plan_id end as payer_plan_id,
               case when l.source = 'RX' then 'PD' end as rx_claim_type,   -- the extract holds paid claims only
               cast(null as varchar) as sob,
               cast(l.days_supply as integer) as days_supply,
               case when l.source = 'RX' then l.quantity end as quantity,
               l.refill_code"""
    con.execute(f"""
        create temp table stg_claims as
        -- DX rows: one per line and diagnosis
        select {common}, 'DX' as code_type, d.code_position, d.diagnosis_code as code,
               'ICD-10-CM' as code_system, coalesce(icd.short_desc, v1.code_short_desc) as code_short_desc,
               'N' as is_line_anchor, cast(null as varchar) as ndc_cd, cast(null as varchar) as ndc_desc,
               cast(null as varchar) as prod_name, {tail}
        from stg_dx d join stg_line_e l using (source_line_id)
        left join ref_icd10cm icd on icd.code = d.diagnosis_code
        left join (select code, code_short_desc from ref_code_desc_v1 where code_type = 'DX') v1
               on v1.code = d.diagnosis_code
        union all
        -- anchor rows: one per line with a procedure (MX_PX) or drug (MX_RX / RX) code
        select {common},
               case when l.source = 'MX_PX' then 'PX' else 'RX' end as code_type, cast(null as bigint) as code_position,
               l.code,
               case when l.source <> 'MX_PX' then 'NDC'
                    when regexp_matches(l.code, '^[0-9]{{4}}[0-9A-Z]$') then 'CPT' else 'HCPCS' end as code_system,
               case when l.source = 'MX_PX' then v1.code_short_desc else l.code_description end as code_short_desc,
               'Y' as is_line_anchor,
               case when l.source <> 'MX_PX' then l.code end as ndc_cd,
               cast(null as varchar) as ndc_desc,
               l.code_description as prod_name, {tail}
        from stg_line_e l
        left join (select code, code_short_desc from ref_code_desc_v1 where code_type = 'PX') v1
               on l.source = 'MX_PX' and v1.code = l.code
        where l.code is not null
    """)
    n_stg, n_cohort, n_lines = con.execute(
        "select count(*), count(distinct patient_id), count(distinct source_line_id) from stg_claims").fetchone()
    log(f"  stg_claims: {n_stg:,} rows, {n_lines:,} claim lines, {n_cohort:,} cohort patients "
        f"({n_pat - n_cohort:,} patients without {config.COHORT_DX_LIKE} dropped)")
    return {"source": "master_nsclc", "source_file": src.name, "source_rows": n_rows,
            "source_patients": n_pat, "cohort_patients": n_cohort}


def _enrichment(con, log=print) -> None:
    """Reference tables that fill fields master_nsclc.csv lacks. Each exists (possibly empty) so joins never fail."""
    # ICD-10-CM descriptions (public CMS file)
    if config.ICD10CM_CSV.exists():
        con.execute(f"""create table ref_icd10cm as select code, is_billable = '1' as is_billable, short_desc
                        from read_csv('{config.ICD10CM_CSV.as_posix()}', header = true, all_varchar = true)""")
    else:
        log("  WARNING ICD-10-CM reference missing (run scripts/fetch_icd10cm.py) - dx descriptions limited")
        con.execute("create table ref_icd10cm (code varchar, is_billable boolean, short_desc varchar)")

    # v1 master: code descriptions + age fallback (only if the old master is still around)
    v1 = config.MASTER_PARQUET
    if v1.exists():
        con.execute(f"""create table ref_code_desc_v1 as
                        select code, code_type, any_value(code_short_desc) as code_short_desc
                        from read_parquet('{v1.as_posix()}')
                        where code is not null and code_short_desc is not null and code_type in ('DX', 'PX')
                        group by code, code_type""")
    else:
        con.execute("create table ref_code_desc_v1 (code varchar, code_type varchar, code_short_desc varchar)")

    # age: raw MX extract first, then v1 master
    raw_mx = config.RAW_MX_CSV
    raw_sel = (f"""select {_strip0('patient_id')} as patient_id, max(try_cast(try_cast(pat_age_yr_nbr as double) as integer)) as age
                   from read_csv('{raw_mx.as_posix()}', all_varchar = true, header = true, sample_size = -1)
                   group by 1""" if raw_mx.exists() else "select null::varchar as patient_id, null::integer as age where false")
    v1_sel = (f"""select patient_id, max(pat_age_yr_nbr) as age from read_parquet('{v1.as_posix()}') group by 1"""
              if v1.exists() else "select null::varchar as patient_id, null::integer as age where false")
    con.execute(f"""
        create table ref_patient_age as
        with a as ({raw_sel}), b as ({v1_sel})
        select p.patient_id, coalesce(a.age, b.age) as age,
               case when a.age is not null then 'mx_nsclc.csv' when b.age is not null then 'claims_master (v1)' end as age_source
        from (select distinct patient_id from src_master_nsclc) p
        left join a using (patient_id) left join b using (patient_id)
    """)
    # billing provider per medical claim (raw MX extract): fallback when a line has no rendering provider
    if raw_mx.exists():
        con.execute(f"""
            create table ref_claim_billing as
            select {_strip0('dx_claim_id')} as claim_id, min({_strip0('provider_billing_id')}) as provider_billing_id
            from read_csv('{raw_mx.as_posix()}', all_varchar = true, header = true, sample_size = -1)
            where provider_billing_id is not null group by 1
        """)
    else:
        con.execute("create table ref_claim_billing (claim_id varchar, provider_billing_id varchar)")
    got_age, n = con.execute("select count(age), count(*) from ref_patient_age").fetchone()
    log(f"  enrichment: age for {got_age:,}/{n:,} patients, billing provider for "
        f"{con.execute('select count(*) from ref_claim_billing').fetchone()[0]:,} claims, "
        f"{con.execute('select count(*) from ref_icd10cm').fetchone()[0]:,} ICD-10-CM descriptions")
