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

REQUIRED_MASTER_COLUMNS = {
    "source_table", "claim_id", "source_line_id", "patient_id", "service_date", "claim_type", "code_type",
    "code_position", "code", "code_system", "code_short_desc", "is_line_anchor", "ndc_cd", "ndc_desc", "prod_name",
    "pat_age_yr_nbr", "provider_rendering_id", "provider_billing_id", "provider_id", "pri_payer_plan_id",
    "payer_plan_id", "most_freq_rx_mop", "days_supply", "dx_unit_of_svc_amt", "quantity", "svc_bill_amt",
    "place_of_svc_cd", "rx_claim_type", "sob",
}


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
    from ciq import config
    p = config.MASTER_PARQUET
    if not p.exists():
        return report(FAIL, "Claims master", f"{p.relative_to(ROOT)} not found",
                      "copy claims_master.parquet from the secure data share into Data/master/ (see Data/README.md)")
    import duckdb
    con = duckdb.connect()
    cols = {r[0] for r in con.execute(f"describe select * from read_parquet('{p.as_posix()}')").fetchall()}
    missing = REQUIRED_MASTER_COLUMNS - cols
    if missing:
        return report(FAIL, "Claims master schema", f"missing columns: {sorted(missing)}",
                      "the file doesn't match the expected master layout - get the current extract")
    n, pts, d0, d1 = con.execute(f"select count(*), count(distinct patient_id), min(service_date), max(service_date) "
                                 f"from read_parquet('{p.as_posix()}')").fetchone()
    return report(OK, "Claims master", f"{n:,} rows, {pts:,} patients, {d0} to {d1}")


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
    print(f"CIQ environment check ({ROOT})\n")
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
