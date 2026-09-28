"""Environment check for new developers: `python scripts/doctor.py [--skip-bedrock]`.

Runs each prerequisite check in order and prints a fix for every failure.
"""
from __future__ import annotations

import importlib.util
import os
import sys

if os.name == "nt":
    os.system("")  # enable ANSI colours in older Windows consoles
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "\033[32mPASS\033[0m", "\033[33mWARN\033[0m", "\033[31mFAIL\033[0m"
results: list[str] = []

def report(status: str, name: str, detail: str = "", fix: str = "") -> bool:
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))
    if fix and status != OK:
        print(f"         fix: {fix}")
    results.append(status)
    return status == OK


def check_python() -> None:
    v = sys.version_info
    report(OK if v >= (3, 10) else FAIL, "Python", f"{v.major}.{v.minor}.{v.micro}", "install Python 3.10+ (3.12 recommended)")


def check_packages() -> bool:
    mods = {"duckdb": "duckdb", "pandas": "pandas", "pyarrow": "pyarrow", "numpy": "numpy", "anthropic": "anthropic[bedrock]",
            "boto3": "boto3", "fastapi": "fastapi", "uvicorn": "uvicorn", "pytest": "pytest"}
    missing = [pkg for mod, pkg in mods.items() if importlib.util.find_spec(mod) is None]
    return report(FAIL if missing else OK, "Python packages", f"missing: {missing}" if missing else "all installed",
                  "pip install -r requirements.txt")


def check_master() -> bool:
    """The one claims input file (header check only - fast) and the optional reference files."""
    from ciq import config
    from ciq.build import inputs, validate
    p = config.INPUT_PATH
    if not p.exists():
        if config.WAREHOUSE_DB.exists():
            return report(WARN, "Claims input", f"{p} not found - fine at runtime (the app reads only the warehouse); "
                          "needed only to rebuild", "put the claims file in Data/real_data/ or set CIQ_INPUT")
        return report(FAIL, "Claims input", f"{p} not found",
                      "copy master_nsclc.csv into Data/real_data/ (see Data/README.md), or set CIQ_INPUT to your file")
    try:
        info = inputs.inspect(p)
    except ValueError as e:
        return report(FAIL, "Claims input", str(e), "use a .csv, .csv.gz, .tsv or .parquet file")
    r = validate.check_header(info)
    ok = report(OK if r.ok else FAIL, "Claims input", f"{p.name} ({p.stat().st_size / 1e6:,.0f} MB, layout {info.layout})"
                if r.ok else "; ".join(r.errors), "fix the file's columns - see docs/DATA_DICTIONARY.md")
    for f, what in ((config.DRUG_REFERENCE_CSV, "drug dictionary"), (config.ICD10CM_CSV, "diagnosis descriptions"),
                    (config.HCPCS_CSV, "HCPCS descriptions")):
        report(OK if f.exists() else WARN, f"Reference: {f.name}", what if f.exists() else f"missing ({what})",
               "restore it from git (icd10cm.csv: python scripts/fetch_icd10cm.py)")
    has_age_col = "age" in info.mapping
    report(OK if (has_age_col or config.PATIENT_AGE_CSV.exists()) else WARN, "Reference: patient age",
           "age column in the claims file" if has_age_col else (config.PATIENT_AGE_CSV.name if config.PATIENT_AGE_CSV.exists()
           else "not available: age-based analyses will be empty"),
           "put patient_age.csv (patient_id,age) in Data/reference/ - optional")
    return ok


def check_warehouse() -> None:
    from ciq import config
    if not config.WAREHOUSE_DB.exists():
        report(WARN, "Warehouse", "not built yet", "python -m ciq.build.build_warehouse")
        return
    import duckdb
    try:
        con = duckdb.connect(str(config.WAREHOUSE_DB), read_only=True)
        n_fn = con.execute("select count(*) from duckdb_functions() where function_name like 'fn_%'").fetchone()[0]
        built = con.execute("select built_at from ref_data_coverage").fetchone()[0]
        report(OK if n_fn else WARN, "Warehouse", f"built {built:%Y-%m-%d %H:%M}, {n_fn} stored functions",
               "python -m ciq.build.install_procedures")
    except Exception as e:
        report(FAIL, "Warehouse", f"unreadable ({type(e).__name__}: {e})",
               "stop the running app (it locks the file), then python -m ciq.build.build_warehouse")


def check_aws() -> bool:
    from app import settings
    try:
        import boto3
        sess = boto3.Session(profile_name=settings.AWS_PROFILE, region_name=settings.AWS_REGION)
        ident = sess.client("sts").get_caller_identity()
        return report(OK, "AWS credentials", f"{ident['Arn']} (region {settings.AWS_REGION})")
    except Exception as e:
        return report(FAIL, "AWS credentials", f"{type(e).__name__}: {e}",
                      "run `aws configure` (or `aws sso login`) and/or set AWS_PROFILE / AWS_REGION in .env")


def check_bedrock() -> None:
    from anthropic import AnthropicBedrock
    from app import settings
    kw = {"aws_region": settings.AWS_REGION, **({"aws_profile": settings.AWS_PROFILE} if settings.AWS_PROFILE else {})}
    try:
        r = AnthropicBedrock(**kw).messages.create(model=settings.MODEL_ID, max_tokens=16,
                                                   messages=[{"role": "user", "content": "Reply with: ok"}])
        report(OK, "Bedrock model access", f"{settings.MODEL_ID} responded ({r.usage.output_tokens} tokens)")
    except Exception as e:
        report(FAIL, "Bedrock model access", f"{settings.MODEL_ID}: {type(e).__name__}: {str(e)[:200]}",
               "ask the AWS admin for bedrock:InvokeModel* on this model/inference profile, or set CIQ_MODEL in .env")


def main() -> None:
    skip_bedrock = "--skip-bedrock" in sys.argv
    from ciq import config
    print(f"CIQ environment check ({ROOT}) - input: {config.INPUT_PATH}\n")
    check_python()
    if check_packages():
        check_master()
        check_warehouse()
        if check_aws() and not skip_bedrock:
            check_bedrock()
    fails, warns = results.count(FAIL), results.count(WARN)
    print(f"\n{'All good - run the app with ./run.ps1 (Windows) or ./run.sh (macOS/Linux)' if not fails else f'{fails} check(s) failed'}"
          + (f", {warns} warning(s)" if warns else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
