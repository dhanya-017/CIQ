"""Business-rule tests for the final IO cohort: SOB and claim types (built warehouse).

Line-of-therapy rules are tested on synthetic patients in test_lot_rules.py.

Run: python -m pytest tests/test_business_rules.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ciq import config

# ---------------------------------------------------------------------------
# Final IO cohort (requires the warehouse built from master_nsclc)
# ---------------------------------------------------------------------------
def _io():
    from ciq.tools.base import query
    try:
        return query("select * from fact_io_claim")
    except Exception:
        pytest.skip("fact_io_claim not in the warehouse (built from claims_master?)")


def test_io_cohort_keeps_only_io_brands_and_forms():
    io = _io()
    forms = {b for b in config.IO_BRANDS} | {v["sc_form"] for v in config.IO_BRANDS.values()}
    assert set(io["brand_family"]) <= set(config.IO_BRANDS)
    assert set(io["product_form"]) <= forms


def test_io_sob_categories_and_claim_type_identities():
    io = _io()
    allowed = {"Initiated", "Switched from Competitor", "Continuing"} | \
              {f"{v['label']} to {v['sc_form'].title()}" for v in config.IO_BRANDS.values()}
    assert set(io["sob"]) <= allowed
    assert (io["trx"] == io["nrx"] + io["rrx"]).all()
    assert (io["nbrx"] <= io["nrx"]).all()                          # NBRx is a subset of NRx
    assert (io.loc[io["sob"] == "Switched from Competitor", "nbrx"] == 1).all()
    assert (io.loc[io["sob"] == "Continuing", "nbrx"] == 0).all()


def test_io_sc_switch_never_starts_a_new_line():
    io = _io().sort_values(["patient_id", "service_date"])
    sw = io[io["sob"].str.contains(" to ")]
    for r in sw.itertuples():
        prev = io[(io["patient_id"] == r.patient_id) & (io["service_date"] == r.prev_io_date)]
        assert (prev["line_number"] == r.line_number).all()


def test_io_brand_dynamics_tool():
    from ciq.tools.registry import run_tool
    for args in ({}, {"brand": "keytruda qlex", "grain": "week"}, {"brand": "OPDIVO", "exclude_washout": True}):
        d = run_tool("io_brand_dynamics", args).to_dict()
        assert d["charts"] and d["data"]
