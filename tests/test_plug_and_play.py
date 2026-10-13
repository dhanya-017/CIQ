"""Plug-and-play input: any file type / header spelling builds; bad files are rejected with a clear message.

Builds a tiny synthetic warehouse in a temp folder (the real warehouse is untouched).
Run: python -m pytest tests/test_plug_and_play.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import duckdb
import pandas as pd
import pytest

from ciq import config
from ciq.build import pipeline


def _claims() -> pd.DataFrame:
    """Two lung-cancer patients and one without lung cancer, in an unusual header/date style."""
    rows = []

    def add(pid, day, source, code, desc=None, dx="C34.90", age=70, supply=None):
        rows.append({"Patient ID": pid, "Service Date": f"02/{day:02d}/2026", "Record Source": source,
                     "Diagnosis Code": dx, "Procedure or NDC": code, "Description": desc, "Claim Number": f"c{pid}{day}",
                     "Rendering Provider ID": "111.0", "Payment Type": "Commercial", "Age": age, "Days Supply": supply})

    # p1: carboplatin + Keytruda IV, then Keytruda Qlex (SC) -> "Keytruda to Keytruda Qlex", same line
    for d in (1, 22):
        add("p1", d, "MX_PX", "J9271", "KEYTRUDA")
        add("p1", d, "MX_PX", "J9045")
    add("p1", 12, "MX_PX", "J9277", "KEYTRUDA QLEX")
    # p2: Opdivo, then Keytruda -> "Switched from Competitor"; plus an oral TKI on a pharmacy claim
    add("p2", 1, "MX_PX", "J9299", "OPDIVO", age=64)
    add("p2", 20, "MX_PX", "J9271", "KEYTRUDA", age=64)
    add("p2", 3, "rx", "310135030", "TAGRISSO", age=64, supply=30)
    # p3: no lung-cancer diagnosis -> outside the cohort
    add("p3", 5, "MX_PX", "J9271", "KEYTRUDA", dx="I10")
    return pd.DataFrame(rows)


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WAREHOUSE_DIR", tmp_path / "wh")
    monkeypatch.setattr(config, "WAREHOUSE_DB", tmp_path / "wh" / "ciq.duckdb")
    monkeypatch.setattr(config, "PATIENT_AGE_CSV", tmp_path / "no_age_file.csv")
    return tmp_path


def test_parquet_with_other_headers_builds_end_to_end(isolated):
    f = isolated / "claims.parquet"
    _claims().to_parquet(f, index=False)
    assert pipeline.build(input_path=f) is True
    con = duckdb.connect(str(config.WAREHOUSE_DB), read_only=True)
    assert {r[0] for r in con.execute("select patient_id from dim_patient").fetchall()} == {"p1", "p2"}   # p3 dropped
    sob = dict(con.execute("select patient_id || '@' || service_date, sob from fact_io_claim").fetchall())
    assert sob["p1@2026-02-12"] == "Keytruda to Keytruda Qlex"
    assert sob["p2@2026-02-20"] == "Switched from Competitor"
    lines = con.execute("select count(*) from lot_line where patient_id = 'p1'").fetchone()[0]
    assert lines == 1                                                   # IV -> SC is not a new line (rule 7)
    assert con.execute("select age from dim_patient where patient_id = 'p2'").fetchone()[0] == 64   # age column used
    assert con.execute("select count(*) from fact_drug_event where product = 'TAGRISSO'").fetchone()[0] == 1


def test_csv_gz_and_rebuild_only_if_changed(isolated):
    f = isolated / "claims.csv.gz"
    _claims().to_csv(f, index=False, compression="gzip")
    assert pipeline.build(input_path=f) is True
    assert pipeline.build(input_path=f, if_changed=True) is False       # unchanged -> skipped
    assert list((config.WAREHOUSE_DIR / "backup").glob("ciq_auto_*.duckdb")) == []


def test_rebuild_keeps_previous_warehouse_as_backup(isolated):
    f = isolated / "claims.csv"
    _claims().to_csv(f, index=False)
    pipeline.build(input_path=f)
    pipeline.build(input_path=f)
    assert len(list((config.WAREHOUSE_DIR / "backup").glob("ciq_auto_*.duckdb"))) == 1


def test_missing_required_column_is_rejected_and_live_warehouse_kept(isolated):
    good = isolated / "good.csv"
    _claims().to_csv(good, index=False)
    pipeline.build(input_path=good)
    bad = isolated / "bad.csv"
    _claims().drop(columns=["Record Source"]).to_csv(bad, index=False)
    with pytest.raises(ValueError, match="Missing required column"):
        pipeline.build(input_path=bad)
    con = duckdb.connect(str(config.WAREHOUSE_DB), read_only=True)                # the good build is still live
    assert con.execute("select count(*) from dim_patient").fetchone()[0] == 2


def test_unreadable_dates_and_unknown_sources_are_rejected(isolated):
    df = _claims()
    df["Service Date"] = "sometime"
    f = isolated / "bad_dates.csv"
    df.to_csv(f, index=False)
    with pytest.raises(ValueError, match="unrecognised format"):
        pipeline.build(input_path=f, validate_only=True)
    df = _claims()
    df["Record Source"] = "LAB"
    f = isolated / "bad_source.csv"
    df.to_csv(f, index=False)
    with pytest.raises(ValueError, match="unknown source"):
        pipeline.build(input_path=f, validate_only=True)


def _data_stack() -> pd.DataFrame:
    """Wide I/P/RX extract with diagnosis_code_1..N, matching Data/real_data/data_stack_*.parquet."""
    return pd.DataFrame([
        {"claim_id": "c1", "patient_id": "p1", "procedure_code": "J9271", "service_from_date": "2022-03-01",
         "data_source": "P", "ndc_cd": None, "diagnosis_code_1": "C3490", "diagnosis_code_2": "I10",
         "provider_rendering_id": "111", "days_supply": None, "quantity": 1.0},
        {"claim_id": "c2", "patient_id": "p1", "procedure_code": "J9045", "service_from_date": "2022-03-01",
         "data_source": "I", "ndc_cd": "00006302601", "diagnosis_code_1": "C3490", "diagnosis_code_2": None,
         "provider_rendering_id": "111", "days_supply": None, "quantity": 1.0},
        {"claim_id": "c3", "patient_id": "p2", "procedure_code": "J9299", "service_from_date": "2022-03-05",
         "data_source": "P", "ndc_cd": None, "diagnosis_code_1": "C3411", "diagnosis_code_2": None,
         "provider_rendering_id": "222", "days_supply": None, "quantity": 1.0},
        {"claim_id": "r1", "patient_id": "p2", "procedure_code": None, "service_from_date": "2022-03-06",
         "data_source": "RX", "ndc_cd": "310135030", "diagnosis_code_1": "C3411", "diagnosis_code_2": None,
         "provider_rendering_id": "222", "days_supply": 30.0, "quantity": 30.0},
        {"claim_id": "c4", "patient_id": "p3", "procedure_code": "J9271", "service_from_date": "2022-03-02",
         "data_source": "P", "ndc_cd": None, "diagnosis_code_1": "I10", "diagnosis_code_2": None,
         "provider_rendering_id": "111", "days_supply": None, "quantity": 1.0},
    ])


def test_data_stack_parquet_builds_end_to_end(isolated):
    from ciq.build import inputs
    f = isolated / "data_stack_20261010.parquet"
    _data_stack().to_parquet(f, index=False)
    assert inputs.inspect(f).layout == "data_stack"
    assert pipeline.build(input_path=f) is True
    con = duckdb.connect(str(config.WAREHOUSE_DB), read_only=True)
    assert {r[0] for r in con.execute("select patient_id from dim_patient").fetchall()} == {"p1", "p2"}
    products = {r[0] for r in con.execute("select distinct product from fact_drug_event").fetchall()}
    assert "KEYTRUDA" in products
    assert "OPDIVO" in products
    dx = {r[0] for r in con.execute("select distinct dx_code from fact_dx_event where patient_id = 'p1'").fetchall()}
    assert "C3490" in dx
    assert "I10" in dx
    sources = dict(con.execute("select source, count(*) from src_master_nsclc group by 1").fetchall())
    assert sources.get("MX_PX")
    assert sources.get("MX_RX")
    assert sources.get("RX")
    cov = con.execute("select data_start, data_end from ref_data_coverage").fetchone()
    assert str(cov[0]) == "2022-03-01"
