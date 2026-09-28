"""Step 3 - Staging: apply the patient cohort and rebuild claim lines -> `stg_claims` (temporary).

    cohort       keep patients with any diagnosis LIKE config.COHORT_DX_LIKE ('%C34%', lung cancer)
    clean-up     drop MX_RX rows without an NDC (they only repeat their MX_PX twin's diagnoses) and rows
                 without a service date
    claim lines  the input repeats each medical claim line once per diagnosis slot: a line key is rebuilt and
                 each line is emitted once, plus one DX row per line and diagnosis
    layout       stg_claims is long: DX rows + one PX/RX "anchor" row per line, with descriptions and age joined in

Every later step reads stg_claims, so a new input format only needs a new load/staging, nothing downstream.
Built as a chain of temp tables on purpose: each is seconds, but DuckDB plans the single combined query badly.
"""
from __future__ import annotations

from ciq import config


def run(ctx) -> None:
    con, log = ctx.con, ctx.log
    log(f"Cohort: patients with a diagnosis LIKE '{config.COHORT_DX_LIKE}'")
    con.execute(f"""
        create temp table stg_src as
        select * from src_master_nsclc
        where patient_id in (select distinct patient_id from src_master_nsclc
                             where diagnosis_code like '{config.COHORT_DX_LIKE}')
          and not (source = 'MX_RX' and code is null)
          and service_date is not null
    """)
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
    # diagnosis slot order = upstream order (slot 1 block first, ...): use the first row_id
    con.execute("""
        create temp table stg_dx as
        select source_line_id, diagnosis_code,
               row_number() over (partition by source_line_id order by min(row_id)) as code_position
        from stg_keyed where diagnosis_code is not null
        group by source_line_id, diagnosis_code
    """)
    con.execute("""
        create temp table stg_line_e as
        select l.*, age.age from stg_line l left join ref_patient_age age using (patient_id)
    """)
    common = """case when l.source = 'RX' then 'RX' else 'MX' end as source_table,
               l.claim_id, l.source_line_id, l.patient_id, l.service_date,
               cast(null as varchar) as claim_type"""
    tail = """l.age as pat_age_yr_nbr,
               cast(null as varchar) as place_of_svc_cd,
               cast(null as varchar) as provider_billing_id,
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
               'ICD-10-CM' as code_system, icd.short_desc as code_short_desc,
               'N' as is_line_anchor, cast(null as varchar) as ndc_cd, cast(null as varchar) as ndc_desc,
               cast(null as varchar) as prod_name, {tail}
        from stg_dx d join stg_line_e l using (source_line_id)
        left join ref_icd10cm icd on icd.code = d.diagnosis_code
        union all
        -- anchor rows: one per line with a procedure (MX_PX) or drug (MX_RX / RX) code
        select {common},
               case when l.source = 'MX_PX' then 'PX' else 'RX' end as code_type, cast(null as bigint) as code_position,
               l.code,
               case when l.source <> 'MX_PX' then 'NDC'
                    when regexp_matches(l.code, '^[0-9]{{4}}[0-9A-Z]$') then 'CPT' else 'HCPCS' end as code_system,
               case when l.source = 'MX_PX' then hc.short_desc else l.code_description end as code_short_desc,
               'Y' as is_line_anchor,
               case when l.source <> 'MX_PX' then l.code end as ndc_cd,
               cast(null as varchar) as ndc_desc,
               l.code_description as prod_name, {tail}
        from stg_line_e l
        left join ref_hcpcs hc on l.source = 'MX_PX' and hc.code = l.code
        where l.code is not null
    """)
    n_stg, n_cohort, n_lines = con.execute(
        "select count(*), count(distinct patient_id), count(distinct source_line_id) from stg_claims").fetchone()
    dropped = ctx.facts.get("source_patients", n_cohort) - n_cohort
    ctx.facts.update(cohort_patients=n_cohort, claim_lines=n_lines)
    log(f"  stg_claims: {n_stg:,} rows, {n_lines:,} claim lines, {n_cohort:,} cohort patients "
        f"({dropped:,} patients without {config.COHORT_DX_LIKE} dropped)")
