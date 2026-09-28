"""Step 2 - Reference data: the lookups that fill what the claims file doesn't carry. All are separate, optional files.

    ref_icd10cm      diagnosis descriptions   ciq/reference/icd10cm.csv (public CMS; scripts/fetch_icd10cm.py)
    ref_hcpcs        HCPCS Level II descriptions  ciq/reference/hcpcs.csv (public)
    ref_patient_age  age per patient          the claims file's `age` column; if a file has none, an optional
                                              separate patient_id,age file named by CIQ_PATIENT_AGE

A missing file gives an empty table (and a warning), never a failed build. The drug dictionary is loaded in
step 4 (drugs.py) because it maps the staged claim lines.
"""
from __future__ import annotations

from ciq import config
from ciq.build import inputs
from ciq.build.inputs import strip0


def _csv(path) -> str:
    return f"read_csv('{path.as_posix()}', header = true, all_varchar = true)"


def run(ctx) -> None:
    con, log = ctx.con, ctx.log

    if config.ICD10CM_CSV.exists():
        con.execute(f"create table ref_icd10cm as select code, is_billable = '1' as is_billable, short_desc "
                    f"from {_csv(config.ICD10CM_CSV)}")
    else:
        log("  WARNING ciq/reference/icd10cm.csv missing (run scripts/fetch_icd10cm.py): no diagnosis descriptions")
        con.execute("create table ref_icd10cm (code varchar, is_billable boolean, short_desc varchar)")

    if config.HCPCS_CSV.exists():
        con.execute(f"create table ref_hcpcs as select code, short_desc from {_csv(config.HCPCS_CSV)}")
    else:
        log("  WARNING ciq/reference/hcpcs.csv missing: no HCPCS procedure descriptions")
        con.execute("create table ref_hcpcs (code varchar, short_desc varchar)")

    # age: the claims file's own column wins; otherwise the separate patient_age lookup
    has_input_age = con.execute("select count(age) from src_master_nsclc").fetchone()[0] > 0
    age_file = config.PATIENT_AGE_CSV
    file_sel = "select null::varchar as patient_id, null::integer as age where false"
    if age_file is not None and age_file.exists():
        info = inputs.inspect(age_file, con)
        cols = {inputs._norm(h): h for h in info.headers}
        pid = cols.get("patient_id")
        age = next((cols[a] for a in inputs.ALIASES["age"] if a in cols), None)
        if pid and age:
            pid_col, age_col = f'"{pid}"', f'"{age}"'
            file_sel = (f"select {strip0(pid_col)} as patient_id, "
                        f"max(try_cast(try_cast({age_col} as double) as integer)) as age from {info.reader} group by 1")
        else:
            log(f"  WARNING {age_file.name} needs columns patient_id and age - ignored")
    elif not has_input_age:
        log("  WARNING the claims file has no age column (and no CIQ_PATIENT_AGE file): age-based analyses will be "
            "unavailable")
    con.execute(f"""
        create table ref_patient_age as
        with f as ({file_sel}),
             i as (select patient_id, max(age) as age from src_master_nsclc group by 1)
        select p.patient_id, coalesce(i.age, f.age) as age,
               case when i.age is not null then 'claims file' when f.age is not null then 'CIQ_PATIENT_AGE file' end as age_source
        from (select distinct patient_id from src_master_nsclc) p
        left join i using (patient_id) left join f using (patient_id)
    """)
    got, n = con.execute("select count(age), count(*) from ref_patient_age").fetchone()
    log(f"  reference: {con.execute('select count(*) from ref_icd10cm').fetchone()[0]:,} ICD-10-CM and "
        f"{con.execute('select count(*) from ref_hcpcs').fetchone()[0]:,} HCPCS descriptions; age for {got:,}/{n:,} patients")
