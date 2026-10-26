"""Step 1 - Load: read the ONE claims input file, validate it, and store it cleaned as `src_master_nsclc`.

Reads any file type / header spelling (inputs.py), checks it (validate.py) and stops the build on errors. The
complete extract is kept (every row), with:
    IDs without the float artefact (".0"), NDCs padded to 11 digits, the `renderring_provider_id` typo column
    merged into rendering_provider_id, literal "NAN" codes -> NULL, service dates parsed, source values
    normalised; the original values of changed columns are kept in *_raw columns.
"""
from __future__ import annotations

from ciq.build import inputs, validate
from ciq.build.inputs import strip0


def run(ctx) -> None:
    info = ctx.input
    report = validate.check_header(info)
    if not report.ok:
        raise ValueError(report.render())

    ctx.log(f"Reading {info.path.name} ({info.layout})")
    inner = (inputs.expand_data_stack_sql(info) if info.layout == "data_stack"
             else f"(select row_number() over () as row_id, {inputs.canonical_select(info)} from {info.reader})")
    ctx.con.execute(f"""
        create temp table input_std as
        select * replace ({inputs.source_sql('source')} as source), {inputs.date_sql('svc_dt')} as service_date
        from {inner}
    """)
    validate.check_data(ctx.con, "input_std", report)
    ctx.log(report.render())
    if not report.ok:
        raise ValueError(report.render())
    if ctx.validate_only:
        return

    ndc = f"lpad({strip0('code')}, 11, '0')"
    procedure = "nullif(nullif(upper(trim(code)), 'NAN'), '')"
    ctx.con.execute(f"""
        create table src_master_nsclc as
        select row_id,
               source,
               {strip0('claim_id')}                                     as claim_id,
               {strip0('patient_id')}                                   as patient_id,
               service_date,
               nullif(nullif(upper(trim(diagnosis_code)), 'NAN'), '')  as diagnosis_code,
               case when source in ('RX', 'MX_RX') then {ndc} end       as ndc_cd,
               case when source = 'MX_PX' then {procedure} end          as procedure_code,
               case when source in ('RX', 'MX_RX') then {ndc}
                    else {procedure} end                                as code,      -- NDC-11 or procedure code
               nullif(trim(code_description), '')                       as code_description,
               coalesce({strip0('rendering_provider_id')}, {strip0('renderring_provider_id')}) as rendering_provider_id,
               {strip0('referring_provider_id')}                        as referring_provider_id,
               {strip0('plan_id')}                                      as plan_id,
               nullif(trim(plan_name), '')                              as plan_name,
               try_cast(days_supply as double)                          as days_supply,
               try_cast(quantity as double)                             as quantity,
               cast(try_cast(refill_code as double) as integer)         as refill_code,
               cast(try_cast(age as double) as integer)                 as age,        -- only if the file has one
               -- original values of the columns cleaned above, kept for traceability
               svc_dt as svc_dt_raw, code as code_raw,
               rendering_provider_id as rendering_provider_id_raw, renderring_provider_id as renderring_provider_id_raw
        from input_std
    """)
    ctx.con.execute("drop table input_std")
    n_rows, n_pat = ctx.con.execute("select count(*), count(distinct patient_id) from src_master_nsclc").fetchone()
    ctx.facts.update(source_file=info.path.name, source_rows=n_rows, source_patients=n_pat)
    ctx.log(f"  src_master_nsclc: {n_rows:,} rows, {n_pat:,} patients")
