"""Plug-and-play input handling: locate, read and normalise a claims extract, whatever its file type or header style.

    path      CLI --input  >  CIQ_INPUT  >  Data/real_data/master_nsclc.csv
              (if that CSV is missing, the newest Data/real_data/data_stack*.parquet)
    format    .csv / .csv.gz / .txt / .tsv (delimiter sniffed) or .parquet
    columns   headers are normalised (case, spaces, dashes) and mapped to canonical names through ALIASES, so
              `Service Date`, `svc_dt` and `service_from_date` all become `svc_dt`. Optional columns may be absent.
              A wide `data_stack` extract (I/P/RX, diagnosis_code_1..N, procedure_code + ndc_cd) is expanded to the
              same long master_nsclc shape before validation.

Everything is read as text; typing and cleaning happen in ciq/build/load.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from ciq import config

# canonical column -> accepted header spellings (after normalisation: lower case, non-alphanumerics -> "_")
ALIASES: dict[str, list[str]] = {
    "claim_id": ["claim_id", "claimid", "dx_claim_id", "claim_number", "claim_no"],
    "patient_id": ["patient_id", "patientid", "pat_id", "patient", "member_id"],
    "svc_dt": ["svc_dt", "service_date", "service_from_date", "date_of_service", "dos", "svc_date", "claim_date"],
    "diagnosis_code": ["diagnosis_code", "dx_code", "diag_code", "diag_cd", "icd10_code", "icd_code", "icd10"],
    "code": ["code", "procedure_or_ndc", "ndc_or_procedure_code", "product_code"],
    "code_description": ["code_description", "description", "product_name", "prod_name", "drug_name"],
    "rendering_provider_id": ["rendering_provider_id", "provider_rendering_id"],
    "renderring_provider_id": ["renderring_provider_id", "prescriber_id", "prescribing_provider_id"],
    "referring_provider_id": ["referring_provider_id", "provider_referring_id"],
    "plan_id": ["plan_id", "payer_plan_id", "pri_payer_plan_id"],
    "plan_name": ["plan_name", "payment_type", "pay_type", "most_freq_rx_mop", "pri_plan_payment_type"],
    "days_supply": ["days_supply", "day_supply", "days_supply_qty"],
    "quantity": ["quantity", "qty", "units", "dx_unit_of_svc_amt"],
    "refill_code": ["refill_code", "refill_number", "refill_nbr", "fill_number"],
    "source": ["source", "record_source", "source_type", "claim_source"],
    "age": ["age", "pat_age_yr_nbr", "patient_age", "age_years"],          # optional (else CIQ_PATIENT_AGE)
}
REQUIRED = ["patient_id", "svc_dt", "source", "diagnosis_code", "code"]
OPTIONAL = [c for c in ALIASES if c not in REQUIRED]

# source values accepted for master_nsclc layout (after upper/trim) -> canonical
SOURCE_VALUES = {"RX": "RX", "PHARMACY": "RX", "MX_RX": "MX_RX", "MX_NDC": "MX_RX", "MX_PX": "MX_PX",
                 "MX_PROC": "MX_PX", "MEDICAL": "MX_PX"}

# v1 claims_master long layout (no longer supported as input; recognised only to give a clear message)
V1_MARKERS = {"source_table", "code_type", "is_line_anchor", "service_date", "code_system"}

# service-date formats tried in order (US month/day before day/month)
DATE_FORMATS = ["%Y-%m-%d %H:%M:%S.%g", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%m/%d/%Y",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%y", "%d-%b-%Y", "%d-%b-%y", "%Y%m%d"]


def _norm(header: str) -> str:
    return re.sub(r"[^0-9a-z]+", "_", header.strip().lower()).strip("_")


@dataclass
class InputInfo:
    path: Path
    reader: str                          # SQL table expression that reads the file (all text)
    headers: list[str]                   # original headers
    layout: str                          # master_nsclc | data_stack | claims_master (v1, unsupported) | unknown
    mapping: dict[str, str] = field(default_factory=dict)   # canonical -> original header
    missing_required: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)       # headers not used


def input_path(override: str | Path | None = None) -> Path:
    return Path(override) if override else config.INPUT_PATH


def reader_sql(path: Path, typed: bool = False) -> str:
    """DuckDB table expression reading the file; all columns as text unless typed=True."""
    p = path.as_posix().replace("'", "''")
    name = path.name.lower()
    if name.endswith(".parquet"):
        return f"read_parquet('{p}')"
    if not any(name.endswith(s) for s in (".csv", ".csv.gz", ".txt", ".tsv", ".gz")):
        raise ValueError(f"Unsupported input file type: {path.name} (use .csv, .csv.gz, .tsv, .txt or .parquet)")
    text = "" if typed else "all_varchar = true, "
    return f"read_csv('{p}', {text}header = true, sample_size = -1)"


def _quoted(normed: dict[str, str], name: str) -> str | None:
    h = normed.get(name)
    if h is None:
        return None
    return '"' + h.replace('"', '""') + '"'


def _dx_slots(normed: dict[str, str]) -> list[str]:
    slots = []
    for k, h in normed.items():
        m = re.fullmatch(r"diagnosis_code_(\d+)", k)
        if m:
            slots.append((int(m.group(1)), '"' + h.replace('"', '""') + '"'))
    return [q for _, q in sorted(slots)]


def _is_data_stack(normed: dict[str, str]) -> bool:
    """Wide IQVIA-style stack: I/P/RX + diagnosis_code_1..N, not the long master_nsclc extract."""
    return ({"patient_id", "service_from_date", "data_source"} <= set(normed)
            and bool(_dx_slots(normed))
            and ("procedure_code" in normed or "ndc_cd" in normed)
            and "source" not in normed)


def inspect(path: Path, con: duckdb.DuckDBPyConnection | None = None) -> InputInfo:
    """Read only the header: layout, column mapping, missing required columns."""
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {path}")
    con = con or duckdb.connect()
    reader = reader_sql(path)
    headers = [r[0] for r in con.execute(f"describe select * from {reader.replace('sample_size = -1', 'sample_size = 1000')}").fetchall()]
    normed = {_norm(h): h for h in headers}
    if V1_MARKERS <= set(normed):
        return InputInfo(path, reader_sql(path, typed=True), headers, "claims_master")
    mapping = {}
    for canon, names in ALIASES.items():
        hit = next((normed[n] for n in names if n in normed), None)
        if hit is not None and hit not in mapping.values():
            mapping[canon] = hit
    if _is_data_stack(normed):
        used = {normed[k] for k in ("patient_id", "claim_id", "service_from_date", "data_source",
                                    "procedure_code", "ndc_cd", "provider_rendering_id", "rendering_provider_id",
                                    "provider_referring_id", "referring_provider_id", "days_supply", "quantity",
                                    "refill_code", "plan_id", "pri_payer_plan_id", "payer_plan_id", "plan_name",
                                    "age", "code_description", "prescriber_id") if k in normed}
        used.update(normed[k] for k in normed if re.fullmatch(r"diagnosis_code_\d+", k))
        used.update(mapping.values())
        missing = [c for c in ("patient_id", "svc_dt") if c not in mapping]
        if "data_source" not in normed:
            missing.append("source")
        if not _dx_slots(normed):
            missing.append("diagnosis_code")
        if "procedure_code" not in normed and "ndc_cd" not in normed:
            missing.append("code")
        return InputInfo(path, reader, headers, "data_stack", mapping, missing,
                         [h for h in headers if h not in used])
    missing = [c for c in REQUIRED if c not in mapping]
    layout = "master_nsclc" if len(missing) < len(REQUIRED) else "unknown"
    unmapped = [h for h in headers if h not in mapping.values()]
    return InputInfo(path, reader, headers, layout, mapping, missing, unmapped)


def canonical_select(info: InputInfo) -> str:
    """SELECT list turning the file's columns into the canonical master_nsclc columns (missing optional -> NULL)."""
    cols = []
    for canon in ALIASES:
        src = info.mapping.get(canon)
        cols.append(f'"{src}" as {canon}' if src else f"cast(null as varchar) as {canon}")
    return ", ".join(cols)


def expand_data_stack_sql(info: InputInfo) -> str:
    """Expand a wide data_stack extract to the long master_nsclc columns (varchar), including row_id.

    I/P (and MX) rows with a procedure become MX_PX; medical NDCs become MX_RX; pharmacy rows become RX.
    Each diagnosis slot is a repeated line, matching the master_nsclc extract staging already expects.
    """
    normed = {_norm(h): h for h in info.headers}
    q = lambda n: _quoted(normed, n)

    def v(name: str, *alts: str) -> str:
        for n in (name, *alts):
            col = q(n)
            if col:
                return f"cast({col} as varchar)"
        return "cast(null as varchar)"

    pid, svc, ds = v("patient_id"), v("service_from_date"), v("data_source")
    claim, px, ndc = v("claim_id"), v("procedure_code"), v("ndc_cd")
    rend = v("provider_rendering_id", "rendering_provider_id", "prescriber_id")
    ref = v("provider_referring_id", "referring_provider_id")
    supply, qty, refill = v("days_supply"), v("quantity"), v("refill_code")
    plan_id, plan_name, age = v("plan_id", "pri_payer_plan_id", "payer_plan_id"), v("plan_name"), v("age")
    desc = v("code_description", "product_name", "prod_name")
    dx_cols = _dx_slots(normed)
    if not dx_cols:
        raise ValueError("data_stack file has no diagnosis_code_N columns")
    dx_list = ", ".join(dx_cols)
    medical = "('I', 'P', 'INP', 'OUT', 'MX', 'MEDICAL', 'MX_PX', 'MX_RX')"
    pharmacy = "('RX', 'PHARMACY', 'PD')"
    blank = lambda c: f"nullif(trim(cast({c} as varchar)), '')"
    return f"""(
        with raw as (
            select row_number() over () as _src_row,
                   {claim} as claim_id, {pid} as patient_id, {svc} as svc_dt,
                   upper(trim({ds})) as data_source,
                   {px} as procedure_code, {ndc} as ndc_cd, {desc} as code_description,
                   {rend} as rendering_provider_id, {ref} as referring_provider_id,
                   {plan_id} as plan_id, {plan_name} as plan_name, {supply} as days_supply,
                   {qty} as quantity, {refill} as refill_code, {age} as age,
                   {dx_list}
            from {info.reader}
        ),
        codes as (
            select *, 'MX_PX' as source, procedure_code as code,
                   concat_ws('|', cast(_src_row as varchar), 'MX_PX') as _line
            from raw
            where data_source in {medical} and {blank('procedure_code')} is not null
            union all
            select *, 'MX_RX' as source, ndc_cd as code,
                   concat_ws('|', cast(_src_row as varchar), 'MX_RX') as _line
            from raw
            where data_source in {medical} and {blank('ndc_cd')} is not null
            union all
            select *, 'RX' as source, ndc_cd as code,
                   concat_ws('|', cast(_src_row as varchar), 'RX') as _line
            from raw
            where data_source in {pharmacy}
            union all
            select *, 'MX_PX' as source, cast(null as varchar) as code,
                   concat_ws('|', cast(_src_row as varchar), 'DX') as _line
            from raw
            where data_source in {medical}
              and {blank('procedure_code')} is null and {blank('ndc_cd')} is null
            union all
            select *, data_source as source, coalesce({blank('procedure_code')}, {blank('ndc_cd')}) as code,
                   concat_ws('|', cast(_src_row as varchar), 'OTHER') as _line
            from raw
            where data_source is not null
              and data_source not in {medical}
              and data_source not in {pharmacy}
        ),
        dx as (
            select _line, diagnosis_code
            from codes
            unpivot (diagnosis_code for _slot in ({dx_list}))
            where {blank('diagnosis_code')} is not null
        )
        select row_number() over () as row_id,
               c.claim_id, c.patient_id, c.svc_dt, d.diagnosis_code, c.code, c.code_description,
               c.rendering_provider_id, cast(null as varchar) as renderring_provider_id,
               c.referring_provider_id, c.plan_id, c.plan_name, c.days_supply, c.quantity,
               c.refill_code, c.source, c.age
        from codes c
        left join dx d using (_line)
    )"""


def date_sql(col: str) -> str:
    """Parse a text date column with the first format that works."""
    tries = ", ".join(f"try_strptime(trim({col}), '{f}')" for f in DATE_FORMATS)
    return f"cast(coalesce({tries}) as date)"


def source_sql(col: str) -> str:
    whens = " ".join(f"when '{k}' then '{v}'" for k, v in SOURCE_VALUES.items())
    return f"case upper(trim({col})) {whens} else upper(trim({col})) end"


def strip0(col: str) -> str:
    """SQL: '10567886.0' -> '10567886' (IDs that went through a float upstream); blank -> NULL."""
    return f"nullif(regexp_replace(trim({col}), '\\.0$', ''), '')"


def fingerprint(paths: list[Path]) -> str:
    """Identity of the build inputs (path, size, modification time) for rebuild-if-changed."""
    parts = []
    for p in paths:
        if p and Path(p).exists():
            st = Path(p).stat()
            parts.append(f"{Path(p).resolve()}|{st.st_size}|{int(st.st_mtime)}")
        else:
            parts.append(f"{p}|missing")
    return ";".join(parts)
