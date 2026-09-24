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

# Line-of-therapy business rules (applied at build time).
LOT_REGIMEN_WINDOW_DAYS = 28     # drugs started within N days of line start form the regimen
LOT_GAP_DAYS = 90                # gap without any therapy that ends a line
CLEAN_START_DAYS = 90            # first therapy >= N days after data start => "clean" (likely true) start

# Molecules treated as interchangeable within a line (a swap is not a new line).
LOT_EQUIVALENT_MOLECULES = [
    {"carboplatin", "cisplatin"},
    {"paclitaxel", "nab-paclitaxel"},
]
