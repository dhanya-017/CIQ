"""Central configuration for the CIQ data platform."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "Data"
WAREHOUSE_DIR = DATA_DIR / "warehouse"
WAREHOUSE_DB = WAREHOUSE_DIR / "ciq.duckdb"

# ---------------------------------------------------------------------------
# Input source (CIQ_SOURCE)
#   master_nsclc   (default) Data/real_data/master_nsclc.csv - 15-column claims extract, enriched at build time
#                  with age + billing provider (raw MX extract) and code descriptions (reference files)
#   claims_master  (legacy v1) Data/master/claims_master.parquet - 77-column long-format master
# Roll back to v1: set CIQ_SOURCE=claims_master and rebuild, or restore Data/warehouse/backup/ciq_v1_*.duckdb.
# ---------------------------------------------------------------------------
SOURCE = os.getenv("CIQ_SOURCE", "master_nsclc").strip().lower()
MASTER_PARQUET = DATA_DIR / "master" / "claims_master.parquet"          # v1 input; also a description/age fallback
MASTER_NSCLC_CSV = DATA_DIR / "real_data" / "master_nsclc.csv"          # v2 input
RAW_MX_CSV = DATA_DIR / "real_data" / "mx_nsclc.csv"                   # v2 enrichment: age, billing provider

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"
DRUG_REFERENCE_CSV = REFERENCE_DIR / "drug_reference.csv"
ICD10CM_CSV = REFERENCE_DIR / "icd10cm.csv"                              # public CMS code file (scripts/fetch_icd10cm.py)
SQL_DIR = Path(__file__).resolve().parent / "sql"

# Default brand the tools analyse when the caller does not specify one.
DEFAULT_PRODUCT = "KEYTRUDA"

# Market definition: ICD-10 prefix identifying the indication.
INDICATION_DX_PREFIX = "C34"
INDICATION_NAME = "Lung cancer (NSCLC market)"
# Patient cohort (v2): patients with any diagnosis matching this LIKE pattern are kept; everyone else is dropped.
COHORT_DX_LIKE = "%C34%"

# Line-of-therapy business rules (applied at build time; see ciq/build/lot.py and ciq/methodology.py).
LOT_REGIMEN_WINDOW_DAYS = 28     # rule 5: drugs given in the first N days of a line define its regimen...
LOT_REGIMEN_WINDOW_CYCLES = 2    # ...and in its first N cycles (administration occasions), if those end later
LOT_GAP_DAYS = 120               # rule 1: >= N days between administrations is a line advancement
CLEAN_START_DAYS = 90            # first therapy >= N days after data start => "clean" (likely true) start
# Drugs that build lines: all anticancer therapy from pharmacy (NDC) and medical claims, including J-code drugs
# billed as procedures. Supportive care (G-CSF, bone agents, antiemetics, ...) never defines a line.
LOT_THERAPY_GROUPS = ("LUNG_SYSTEMIC", "OTHER_ONCOLOGY")

# Rule 7: molecules treated as the same drug within a line (a swap is not a new line). Biosimilars and SC forms
# need no entry: they map to their reference molecule in ciq/reference/drug_reference.csv. nab-paclitaxel is an
# albumin-bound formulation of paclitaxel (a "different form"). Different molecules such as carboplatin vs
# cisplatin are NOT listed: under rules 2/6 switching between them advances the line.
LOT_EQUIVALENT_MOLECULES = [
    {"paclitaxel", "nab-paclitaxel"},
]

# ---------------------------------------------------------------------------
# Final IO cohort: after lines of therapy are built, only these products' records are kept (fact_io_claim).
# Each brand family has an IV reference product and a subcutaneous (SC) form; IV <-> SC is the same molecule,
# so it never advances a line (rule 7), but SOB reports "<Brand> to <Brand SC>" separately.
# ---------------------------------------------------------------------------
IO_BRANDS = {
    "KEYTRUDA":  {"label": "Keytruda",  "sc_form": "KEYTRUDA QLEX",     "sc_codes": ["J9277"], "sc_name": "QLEX"},
    "OPDIVO":    {"label": "Opdivo",    "sc_form": "OPDIVO QVANTIG",    "sc_codes": ["J9289"], "sc_name": "QVANTIG"},
    "TECENTRIQ": {"label": "Tecentriq", "sc_form": "TECENTRIQ HYBREZA", "sc_codes": ["J9024"], "sc_name": "HYBREZA"},
}
# Claims in the first N days of data can't see earlier IO use, so "Initiated" there may be a continuing patient.
SOB_WASHOUT_DAYS = 90
