"""The warehouse build: ONE claims file in, Data/warehouse/ciq.duckdb out.

Each step is a module in ciq/build with a `run(ctx)` function, run in this order:

    1  load               read + validate the input file         -> src_master_nsclc
    2  reference          descriptions + age lookups              -> ref_icd10cm, ref_hcpcs, ref_patient_age
    3  staging            %C34% cohort, rebuild claim lines       -> stg_claims (temporary)
    4  drugs              map drug lines to molecules/products     -> ref_drug, ref_drug_code_map
    5  facts              events per patient and day               -> fact_drug_event, fact_dx_event, fact_px_event
    6  dimensions         patients, HCPs, data coverage            -> dim_patient, dim_hcp, ref_data_coverage
    7  lot                lines of therapy (business rules)        -> lot_line, lot_line_drug
    8  io_cohort          final IO cohort, SOB, TRx/NRx/RRx/NBRx   -> fact_io_claim
    9  install_procedures stored functions for the tools           -> fn_*
   10  (here)             record rules + input fingerprint         -> ref_build_params

Safety: the build writes to a separate file and only replaces the live warehouse when every step succeeds; the
previous warehouse goes to Data/warehouse/backup/ (config.WAREHOUSE_BACKUPS_KEPT kept).

Run:  python -m ciq.build.build_warehouse [--input FILE] [--validate-only] [--if-changed]
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import duckdb

from ciq import config
from ciq.build import dimensions, drugs, facts, inputs, install_procedures, io_cohort, load, lot, reference, staging


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


@dataclass
class BuildContext:
    """What every step receives."""
    con: duckdb.DuckDBPyConnection
    input: inputs.InputInfo
    log: Callable[[str], None] = log
    validate_only: bool = False
    facts: dict = field(default_factory=dict)      # counts steps report, recorded in ref_build_params


STEPS: list[tuple[str, Callable[[BuildContext], None]]] = [
    ("Load and validate the input", load.run),
    ("Reference data", reference.run),
    ("Cohort and claim lines", staging.run),
    ("Drug identification", drugs.run),
    ("Facts", facts.run),
    ("Dimensions", dimensions.run),
    ("Lines of therapy", lot.run),
    ("Final IO cohort", io_cohort.run),
    ("Stored functions", install_procedures.run),
]


def build(input_path: str | Path | None = None, validate_only: bool = False, if_changed: bool = False) -> bool:
    """Build the warehouse. Returns True if a new warehouse was swapped in."""
    t0 = time.time()
    if input_path:
        config.INPUT_PATH = Path(input_path)
    config.WAREHOUSE_DIR.mkdir(parents=True, exist_ok=True)
    fp = build_fingerprint()
    if if_changed and config.WAREHOUSE_DB.exists():
        if not config.INPUT_PATH.exists():
            log(f"Input {config.INPUT_PATH} not found - keeping the existing warehouse (runtime-only install)")
            return False
        if _stored_fingerprint() == fp:
            log("Warehouse is up to date (input, reference files, rules and build code unchanged)")
            return False
        log("Input or build logic changed - rebuilding")
    if not config.INPUT_PATH.exists():
        raise FileNotFoundError(f"Input file not found: {config.INPUT_PATH} (set CIQ_INPUT or pass --input; "
                                "see Data/README.md)")

    tmp = config.WAREHOUSE_DIR / "ciq.building.duckdb"
    _remove(tmp)
    con = duckdb.connect(str(tmp))
    ok = False
    try:
        info = inputs.inspect(config.INPUT_PATH, con)
        if info.layout == "claims_master":
            raise ValueError("This is a v1 claims_master file, which is no longer a supported input. Use the "
                             "master_nsclc extract, or check out git tag v1-claims-master for the old version.")
        log(f"Input {config.INPUT_PATH} ({config.INPUT_PATH.stat().st_size / 1e6:,.0f} MB)")
        ctx = BuildContext(con, info, validate_only=validate_only)
        for i, (name, step) in enumerate(STEPS, 1):
            log(f"Step {i}/{len(STEPS)}: {name}")
            step(ctx)
            if validate_only:            # step 1 validates; nothing is built
                return False
        _record(ctx, fp)
        con.execute("checkpoint")
        _summary(con)
        ok = True
    finally:
        con.close()
        if not ok:
            _remove(tmp)
    _swap_in(tmp)
    log(f"Done in {time.time() - t0:.0f}s -> {config.WAREHOUSE_DB}")
    return True


# ---------------------------------------------------------------------------
# Build record, summary, safe swap, change detection
# ---------------------------------------------------------------------------
def _record(ctx: BuildContext, fp: str) -> None:
    params = {
        "input_path": str(config.INPUT_PATH), "input_fingerprint": fp,
        "cohort_dx_like": config.COHORT_DX_LIKE, "indication_dx_prefix": config.INDICATION_DX_PREFIX,
        "lot_regimen_window_days": config.LOT_REGIMEN_WINDOW_DAYS,
        "lot_regimen_window_cycles": config.LOT_REGIMEN_WINDOW_CYCLES, "lot_gap_days": config.LOT_GAP_DAYS,
        "lot_therapy_groups": ",".join(config.LOT_THERAPY_GROUPS), "clean_start_days": config.CLEAN_START_DAYS,
        "sob_washout_days": config.SOB_WASHOUT_DAYS, "default_product": config.DEFAULT_PRODUCT, **ctx.facts,
    }
    ctx.con.execute("create table ref_build_params (param varchar, value varchar)")
    ctx.con.executemany("insert into ref_build_params values (?, ?)", [(k, str(v)) for k, v in params.items()])


def _summary(con) -> None:
    for t in ["src_master_nsclc", "ref_drug_code_map", "fact_drug_event", "fact_dx_event", "fact_px_event",
              "dim_patient", "dim_hcp", "lot_line", "lot_line_drug", "fact_io_claim"]:
        log(f"  {t:<20} {con.execute(f'select count(*) from {t}').fetchone()[0]:>12,} rows")


def _remove(db: Path) -> None:
    for f in (db, db.with_name(db.name + ".wal")):
        if f.exists():
            f.unlink()


def _swap_in(tmp: Path) -> None:
    """Move the live warehouse to backup/, put the new build in its place, prune old automatic backups."""
    live = config.WAREHOUSE_DB
    backup_dir = config.WAREHOUSE_DIR / "backup"
    backup_dir.mkdir(exist_ok=True)
    if live.exists():
        backup = backup_dir / f"ciq_auto_{datetime.now():%Y%m%d_%H%M%S}.duckdb"
        try:
            os.replace(live, backup)
        except PermissionError:
            raise RuntimeError(f"The new warehouse is built ({tmp.name}) but the current one is in use - stop the app "
                               f"and run the build again (or replace {live.name} with {tmp.name} by hand).") from None
        log(f"  previous warehouse kept as backup/{backup.name}")
    wal = live.with_name(live.name + ".wal")
    if wal.exists():
        wal.unlink()
    os.replace(tmp, live)
    for old in sorted(backup_dir.glob("ciq_auto_*.duckdb"), reverse=True)[config.WAREHOUSE_BACKUPS_KEPT:]:
        old.unlink()


def build_fingerprint() -> str:
    """Everything whose change should trigger a rebuild: input, reference files, rules, build code."""
    files = inputs.fingerprint([config.INPUT_PATH, config.PATIENT_AGE_CSV, config.DRUG_REFERENCE_CSV,
                                config.ICD10CM_CSV, config.HCPCS_CSV])
    rules = repr((config.COHORT_DX_LIKE, config.LOT_REGIMEN_WINDOW_DAYS, config.LOT_REGIMEN_WINDOW_CYCLES,
                  config.LOT_GAP_DAYS, config.LOT_THERAPY_GROUPS, sorted(map(sorted, config.LOT_EQUIVALENT_MOLECULES)),
                  config.IO_BRANDS, config.SOB_WASHOUT_DAYS, config.CLEAN_START_DAYS))
    code = hashlib.sha1()
    for f in sorted(Path(__file__).parent.glob("*.py")) + [config.SQL_DIR / "procedures.sql"]:
        code.update(f.read_bytes())
    return hashlib.sha1(f"{files}|{rules}|{code.hexdigest()}".encode()).hexdigest()


def _stored_fingerprint() -> str | None:
    try:
        con = duckdb.connect(str(config.WAREHOUSE_DB), read_only=True)
        try:
            row = con.execute("select value from ref_build_params where param = 'input_fingerprint'").fetchone()
        finally:
            con.close()
        return row[0] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ciq.build.build_warehouse",
                                 description="Build the CIQ warehouse from one claims file (plug and play).")
    ap.add_argument("--input", help="claims file (.csv / .csv.gz / .tsv / .parquet); default CIQ_INPUT or "
                                    "Data/real_data/master_nsclc.csv")
    ap.add_argument("--validate-only", action="store_true", help="check the input and report; build nothing")
    ap.add_argument("--if-changed", action="store_true", help="skip if input, reference files, rules and code "
                                                                "are unchanged")
    a = ap.parse_args(argv)
    try:
        build(a.input, validate_only=a.validate_only, if_changed=a.if_changed)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        print(f"\nBUILD STOPPED: {e}", file=sys.stderr)
        return 1
    return 0
