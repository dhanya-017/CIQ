"""Step 6 - Dimensions: one row per patient / HCP, plus the data coverage record.

    ref_data_coverage  data start / end, patients, rows, build time
    dim_patient        age + age group, payment type (most frequent medical), first diagnosis / treatment dates
    dim_hcp            per HCP volumes and patients
Also fills fact_drug_event.payment_type: the claim's own type, else the patient's most frequent one.
"""
from __future__ import annotations

from ciq import config


def run(ctx) -> None:
    con, log = ctx.con, ctx.log
    con.execute("""
        create table ref_data_coverage as
        select min(service_date) as data_start, max(service_date) as data_end,
               count(distinct patient_id) as n_patients,
               count(*) as n_source_rows,
               current_timestamp as built_at
        from stg_claims
    """)

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

    con.execute("""
        alter table fact_drug_event add column payment_type varchar;
        update fact_drug_event set payment_type = case
            when fact_drug_event.payment_type_raw is not null
                 and fact_drug_event.payment_type_raw not in ('Unknown', 'Unknown Medical')
                 then fact_drug_event.payment_type_raw
            else p.payment_type end
        from dim_patient p where p.patient_id = fact_drug_event.patient_id;
    """)

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
