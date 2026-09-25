"""Final IO cohort: IO-brand records with line of therapy, source of business (SOB) and claim types.

Built after lines of therapy (which use every anticancer drug), keeping only the IO brands in config.IO_BRANDS
(Keytruda / Keytruda Qlex, Opdivo / Opdivo Qvantig, Tecentriq / Tecentriq Hybreza). One row per patient, day
and brand family (fact_drug_event grain).

SOB: each claim is classified against the patient's previous IO-brand claim:
    Initiated                  no earlier IO-brand claim in the data, or the same brand resumes after a gap of
                               LOT_GAP_DAYS+ (a re-initiation; is_reinitiation = true)
    Switched from Competitor   the previous IO-brand claim was a different brand family
    <Brand> to <Brand SC>      same family, previous claim IV and this one subcutaneous (e.g. Keytruda to Keytruda Qlex)
    Continuing                 everything else in the same family (incl. SC back to IV)

Claim types (every claim is TRx):
    NBRx   new to brand: Switched from Competitor, or Initiated without earlier use of the brand (not re-initiation)
    NRx    new prescription: NBRx, re-initiation, IV -> SC switch, first claim of the brand in a new line of therapy,
           or a pharmacy fill with refill code 0
    RRx    repeat: TRx that is not NRx
claim_type = NBRx | NRx | RRx (hierarchical: NBRx is also NRx).

in_washout flags claims in the first SOB_WASHOUT_DAYS of data, where earlier IO use can't be seen (so "Initiated"
there may really be a continuing patient).
"""
from __future__ import annotations

from ciq import config


def build_io_cohort(con, log=print) -> None:
    brands = list(config.IO_BRANDS)
    in_brands = ", ".join(f"'{b}'" for b in brands)
    sc_forms = ", ".join(f"'{b['sc_form']}'" for b in config.IO_BRANDS.values())
    label_case = " ".join(f"when '{b}' then '{v['label']} to {v['sc_form'].title()}'" for b, v in config.IO_BRANDS.items())
    con.execute(f"""
        create table fact_io_claim as
        with io as (
            select f.patient_id, f.service_date, f.product as brand_family, f.product_form, f.molecule,
                   f.product_form in ({sc_forms}) as is_sc, f.source, f.claim_id, f.code, f.hcp_id,
                   f.payer_plan_id, f.payment_type, f.days_supply, f.units, f.refill_code
            from fact_drug_event f
            where f.product in ({in_brands})
        ),
        lined as (   -- the line of therapy each IO claim falls in (lines are contiguous per patient)
            select io.*, l.line_number, l.start_date as line_start_date, l.regimen as line_regimen,
                   l.combination_type as line_combination_type
            from io asof left join lot_line l
              on io.patient_id = l.patient_id and io.service_date >= l.start_date
        ),
        seq as (
            select *,
                   lag(brand_family) over w as prev_io_brand,
                   lag(product_form) over w as prev_io_form,
                   lag(is_sc) over w as prev_is_sc,
                   lag(service_date) over w as prev_io_date,
                   lag(line_number) over w as prev_io_line,
                   count(*) over (partition by patient_id, brand_family order by service_date, product_form
                                  rows between unbounded preceding and 1 preceding) as prior_brand_claims
            from lined
            window w as (partition by patient_id order by service_date, product_form)
        ),
        classified as (
            select *,
                   service_date - prev_io_date as days_since_prev_io,
                   prev_io_brand = brand_family and service_date - prev_io_date >= {config.LOT_GAP_DAYS} as is_reinitiation,
                   case
                       when prev_io_brand is null then 'Initiated'
                       when prev_io_brand <> brand_family then 'Switched from Competitor'
                       when service_date - prev_io_date >= {config.LOT_GAP_DAYS} then 'Initiated'
                       when not prev_is_sc and is_sc then case brand_family {label_case} end
                       else 'Continuing'
                   end as sob
            from seq
        ),
        typed as (
            select *,
                   (sob = 'Switched from Competitor' or (sob = 'Initiated' and prior_brand_claims = 0)) as is_nbrx,
                   (sob in ('Initiated', 'Switched from Competitor')
                    or sob like '% to %'
                    or (prev_io_brand = brand_family and line_number is distinct from prev_io_line)
                    or (source = 'RX' and refill_code = 0)) as is_nrx
            from classified
        )
        select t.patient_id, t.service_date,
               cast(date_trunc('week', t.service_date) as date) as week_start,
               cast(date_trunc('month', t.service_date) as date) as month_start,
               t.brand_family, t.product_form, t.is_sc, t.molecule, t.source, t.claim_id, t.code, t.hcp_id,
               t.payer_plan_id, t.payment_type, t.days_supply, t.units, t.refill_code,
               t.line_number, t.line_start_date, t.line_regimen, t.line_combination_type,
               t.prev_io_brand, t.prev_io_form, t.prev_io_date, t.days_since_prev_io,
               t.sob, coalesce(t.is_reinitiation, false) as is_reinitiation,
               t.service_date < c.data_start + {config.SOB_WASHOUT_DAYS} as in_washout,
               1 as trx,
               cast(t.is_nrx as integer) as nrx,
               1 - cast(t.is_nrx as integer) as rrx,
               cast(t.is_nbrx as integer) as nbrx,
               case when t.is_nbrx then 'NBRx' when t.is_nrx then 'NRx' else 'RRx' end as claim_type,
               p.age, p.age_group
        from typed t
        cross join ref_data_coverage c
        left join dim_patient p using (patient_id)
        order by t.patient_id, t.service_date
    """)
    rows = con.execute("""select brand_family, count(*) claims, count(distinct patient_id) patients,
                                 sum(nbrx) nbrx, sum(nrx) nrx, sum(rrx) rrx
                          from fact_io_claim group by 1 order by 1""").fetchall()
    for r in rows:
        log(f"  {r[0]:<10} {r[1]:>6,} claims  {r[2]:>5,} patients  NBRx {r[3]:>5,}  NRx {r[4]:>5,}  RRx {r[5]:>6,}")
