"""Compare-as-diagnostic: every kind returns a scorecard, drivers and (where it applies) an opportunity list.

Run: python -m pytest tests -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ciq.tools import compare
from ciq.tools.registry import run_tool


def _check(res, kind):
    d = res.to_dict()
    json.dumps(d)
    score = d["data"]["scorecard"]
    assert score and all({"metric", "a_fmt", "b_fmt", "diff_fmt"} <= set(r) for r in score)
    assert d["charts"] and d["kpis"] and d["meta"]["compare"]["kind"] == kind
    assert "## insights" in res.to_llm_text()
    return d


def test_brands():
    d = _check(run_tool("compare_segments", {"kind": "brands", "a": "KEYTRUDA", "b": "opdivo"}), "brands")
    share = next(r for r in d["data"]["scorecard"] if r["metric"] == "Patient share of treated market")
    assert share["diff_fmt"].endswith("pp")
    opp = d["meta"]["compare"]["opportunity"]
    assert opp["count"] == len(d["data"]["opportunity_hcps"]) > 0     # HCPs where OPDIVO out-sells KEYTRUDA
    assert all(r["opdivo_volume"] > r["keytruda_volume"] for r in d["data"]["opportunity_hcps"])


def test_hcp_cohorts_use_volume_share():
    d = _check(compare.compare_segments("hcp", "decile:1", "decile:7", "KEYTRUDA"), "hcp")
    assert d["data"]["scorecard"][0]["metric"] == "KEYTRUDA volume share"
    assert d["data"]["scorecard"][0]["a_fmt"] == "51.2%"           # matches hcp_product_share_deciles decile 1


def test_time_periods_explain_movement():
    d = _check(compare.compare_segments("time", "2026-01..2026-03", "2026-04..2026-07", "KEYTRUDA"), "time")
    assert set(d["data"]["hcp_movement"][0]) >= {"movement", "hcps", "net_change_per_month"}
    assert any("per month" in r["metric"] for r in d["data"]["scorecard"])


def test_patient_cohorts_and_benchmark():
    _check(compare.compare_segments("patient", "age_lt65", "age_65plus", "KEYTRUDA"), "patient")
    _check(compare.compare_segments("hcp", "high_low", "all", "KEYTRUDA"), "hcp")


@pytest.mark.parametrize("kind,a,b,reason", [
    ("brands", "KEYTRUDA", "KEYTRUDA", "two different"),
    ("hcp", "decile:1", "top", "unknown HCP cohort"),
    ("time", "2026-05..2026-02", "2026-06..2026-07", "ends before"),
    ("patient", "age_lt65", "martians", "unknown patient cohort"),
    ("region", "a", "b", "kind must be"),
])
def test_bad_sides_explain_themselves(kind, a, b, reason):
    with pytest.raises(ValueError, match=reason):
        compare.compare_segments(kind, a, b, "KEYTRUDA")


def test_saved_cohort_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "_cohort_file", lambda: tmp_path / "cohorts.json")
    opp = compare.compare_segments("brands", "KEYTRUDA", "OPDIVO").meta["compare"]["opportunity"]
    c = compare.save_cohort("OPDIVO-leaning HCPs", opp["hcp_ids"], "HCPs where OPDIVO out-sells KEYTRUDA", "KEYTRUDA")
    assert any(o["id"] == f"saved:{c['id']}" for o in compare.hcp_cohort_options("KEYTRUDA"))
    d = _check(compare.compare_segments("hcp", f"saved:{c['id']}", "all", "KEYTRUDA"), "hcp")
    assert d["title"] == "OPDIVO-leaning HCPs vs All HCPs"
