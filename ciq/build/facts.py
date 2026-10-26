"""Step 5 - Facts: event tables at a fixed grain, deduplicated.

    fact_drug_event  one row per patient, day and molecule (medical + paid pharmacy): product, class, therapy group,
                     product_form (IV vs SC for the IO brands), HCP, payer, days supply (dispensed or standard cycle)
    fact_dx_event    one row per patient, day and ICD-10 code, with description and 3-character category
    fact_px_event    one row per patient, day and procedure code (CPT/HCPCS)
"""
from __future__ import annotations

from ciq import config


def product_form_sql() -> str:
    """SQL naming the product form: the SC form for IO brands billed as SC (code or name), else the product."""
    whens = []
    for brand, b in config.IO_BRANDS.items():
        codes = ", ".join(f"'{c}'" for c in b["sc_codes"])
        whens.append(f"when m.product = '{brand}' and (l.code in ({codes}) or upper(concat_ws(' ', l.prod_name, "
                     f"l.ndc_desc)) like '%{b['sc_name']}%') then '{b['sc_form']}'")
    return "case " + " ".join(whens) + " else m.product end"


def run(ctx) -> None:
    con, log = ctx.con, ctx.log
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
                   {product_form_sql()} as product_form
            from lines l
            join ref_drug_code_map m
              on m.code_system = l.code_system
             and m.code is not distinct from l.code
             and m.prod_name is not distinct from l.prod_name
             and m.ndc_desc is not distinct from l.ndc_desc
        )
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

    log("Building fact_dx_event")
    con.execute("""
        create table fact_dx_event as
        select patient_id, service_date, code as dx_code,
               any_value(code_short_desc) as dx_desc,
               left(replace(code, '.', ''), 3) as dx_category,
               min(code_position) as best_position,
               bool_or(source_table = 'RX') as from_rx,
               arg_min(coalesce(provider_rendering_id, provider_billing_id, provider_id),
                       lpad(cast(code_position as varchar), 3, '0') || source_line_id) as hcp_id,
               count(*) as n_mentions
        from stg_claims
        where code_type = 'DX' and code is not null
        group by patient_id, service_date, code
    """)

    log("Building fact_px_event")
    con.execute("""
        create table fact_px_event as
        select patient_id, service_date, code as px_code, code as procedure_code, code_system,
               any_value(code_short_desc) as px_desc,
               any_value(claim_type) as claim_type,
               arg_min(coalesce(provider_rendering_id, provider_billing_id), source_line_id) as hcp_id,
               count(*) as n_lines, sum(svc_bill_amt) as billed_amt
        from stg_claims
        where source_table = 'MX' and code_type = 'PX'
        group by patient_id, service_date, code, code_system
    """)
