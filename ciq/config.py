"""Central configuration for the CIQ data platform."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "Data"
MASTER_PARQUET = DATA_DIR / "master" / "claims_master.parquet"
WAREHOUSE_DIR = DATA_DIR / "warehouse"
WAREHOUSE_DB = WAREHOUSE_DIR / "ciq.duckdb"

REFERENCE_DIR = Path(__file__).resolve().parent / "reference"
DRUG_REFERENCE_CSV = REFERENCE_DIR / "drug_reference.csv"
SQL_DIR = Path(__file__).resolve().parent / "sql"

# Default brand the tools analyse when the caller does not specify one.
DEFAULT_PRODUCT = "KEYTRUDA"

# Market definition: ICD-10 prefix identifying the indication.
INDICATION_DX_PREFIX = "C34"
INDICATION_NAME = "Lung cancer (NSCLC market)"

# Line-of-therapy business rules (applied at build time; see ciq/build/lot.py and ciq/methodology.py).
LOT_REGIMEN_WINDOW_DAYS = 28     # rule 5: drugs given in the first N days of a line define its regimen...
LOT_REGIMEN_WINDOW_CYCLES = 2    # ...and in its first N cycles (administration occasions), if those end later
LOT_GAP_DAYS = 60                # rule 1: >= N days between administrations is a line break
CLEAN_START_DAYS = 90            # first therapy >= N days after data start => "clean" (likely true) start

# Rule 7: molecules treated as the same drug within a line (a swap is not a new line). Biosimilars and SC forms
# need no entry: they map to their reference molecule in ciq/reference/drug_reference.csv. nab-paclitaxel is an
# albumin-bound formulation of paclitaxel (a "different form"). Different molecules such as carboplatin vs
# cisplatin are NOT listed: under rules 2/6 switching between them advances the line.
LOT_EQUIVALENT_MOLECULES = [
    {"paclitaxel", "nab-paclitaxel"},
]
