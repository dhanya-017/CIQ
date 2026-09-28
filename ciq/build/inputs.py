"""Plug-and-play input handling: locate, read and normalise a claims extract, whatever its file type or header style.

    path      CLI --input  >  CIQ_INPUT  >  Data/real_data/master_nsclc.csv
    format    .csv / .csv.gz / .txt / .tsv (delimiter sniffed) or .parquet
    columns   headers are normalised (case, spaces, dashes) and mapped to canonical names through ALIASES, so
              `Service Date`, `svc_dt` and `service_from_date` all become `svc_dt`. Optional columns may be absent.

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
    layout: str                          # master_nsclc | claims_master (v1, unsupported) | unknown
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
