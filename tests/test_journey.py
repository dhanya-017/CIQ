"""Patient journey: pathway vectors, statuses, similarity, transitions, cohorts."""
import pandas as pd
import pytest

from ciq.tools import compare, journey as J
from ciq.tools.registry import run_tool


def test_lcs_similarity():
    assert J._lcs(J.KEYS, J.KEYS) == 7
    assert J._lcs(["treatment", "diagnosis"], J.KEYS) == 1
    assert J._lcs_items(["tissue", "diagnosis", "imaging", "treatment"], J.KEYS) == ["tissue", "diagnosis", "treatment"]


def test_statuses_follow_definitions():
    df = J.patient_stages()
    assert set(df["status"]) <= set(J.STATUSES)
    treated = df[df["d_treatment"].notna()]
    assert (treated[treated.status == "On pathway"]["dx_to_tx"] <= J.DELAY_DAYS).all()
    assert (treated[treated.status == "Delayed"]["dx_to_tx"] > J.DELAY_DAYS).all()
    untreated = df[df["d_treatment"].isna()]
    assert set(untreated["status"]) <= {"Stalled", "In progress"}
    assert (untreated[untreated.status == "Stalled"]["followup_days"] >= J.STALL_DAYS).all()
    # every patient in the cohort is newly diagnosed after the clean period, and has a diagnosis stage
    assert df["d_diagnosis"].notna().all()
    assert df["similarity"].between(0, 1).all()


def test_overview_consistent():
    o = J.overview()
    n = o["context"]["patients"]
    assert sum(s["patients"] for s in o["status"]) == n
    assert sum(p["patients"] for p in o["patterns"]) == n          # archetypes partition the cohort
    assert sum(s["patients"] for s in o["similarity"]) == n
    dx = next(s for s in o["stages"] if s["key"] == "diagnosis")
    assert dx["patients"] == n
    for t in o["transitions"]:
        assert t["reached"] + t["not_reached"] + t["too_early"] == t["from"]


def test_transition_and_compare():
    t = J.transition("diagnosis", "treatment", "KEYTRUDA")
    assert sum(x["patients"] for x in t["distribution"]) == t["reached"]
    assert len(t["not_reached_ids"]) == t["not_reached"]
    c = J.compare_journeys("treated:yes", "treated:no")
    assert c["rows"][0]["a"] + c["rows"][0]["b"] == len(J.patient_stages())
    with pytest.raises(ValueError):
        J.transition("diagnosis", "diagnosis")
    with pytest.raises(ValueError):
        J.compare_journeys("all", "all")


def test_patient_drilldown_is_month_level():
    pid = J.patient_stages()["patient_id"].iloc[0]
    p = J.patient(pid)
    assert len(p["stages"]) == 7 and p["status"] in J.STATUSES and p["reason"]
    assert all("date" not in e for e in p["timeline"])          # no exact dates leave the server
    with pytest.raises(ValueError):
        J.patient("not-a-patient")


def test_patient_cohort_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(compare, "_cohort_file", lambda: tmp_path / "cohorts.json")
    ids = J.patients("status:Stalled")["patient_ids"][:50]
    c = compare.save_cohort("Stalled sample", None, "test", patient_ids=ids)
    assert c["kind"] == "patient" and compare.cohort_size(c) == 50
    assert any(o["id"] == f"saved:{c['id']}" for o in compare.patient_cohort_options())
    assert not any(o["id"] == f"saved:{c['id']}" for o in compare.hcp_cohort_options("KEYTRUDA"))
    sel, label = J._select(J.patient_stages(), f"saved:{c['id']}")
    assert len(sel) == 50 and (sel["status"] == "Stalled").all()
    res = compare.compare_segments("patient", f"saved:{c['id']}", "all", "KEYTRUDA")
    assert res.title


def test_agent_tool():
    for args in ({}, {"view": "transition", "a": "imaging", "b": "consult"},
                 {"view": "compare", "a": "payer:Medicare", "b": "payer:Commercial"}):
        r = run_tool("patient_journey", args)
        d = r.to_dict()
        assert d["method"]["question"] and r.insights


def test_brand_scope_filters_every_view():
    m, b = J.overview("KEYTRUDA", "market"), J.overview("KEYTRUDA", "brand")
    n = b["context"]["patients"]
    assert b["context"]["scope"] == "brand" and 0 < n < m["context"]["patients"]
    assert sum(s["patients"] for s in b["status"]) == n
    assert b["stages"][4]["patients"] == n                      # every brand patient has the diagnosis stage
    t = J.transition("diagnosis", "treatment", "KEYTRUDA", "brand")
    assert t["from"] == n
    assert J.patients("all", scope="brand", product="KEYTRUDA")["count"] == n
    with pytest.raises(ValueError):
        J.cohort("nonsense", "KEYTRUDA")


def test_brand_view_shares():
    bv = J.brand_view("KEYTRUDA")
    assert 0 < bv["capture"] < 1 and bv["brand_patients"] <= bv["systemic_treated"]
    bm = [r for r in bv["rows"] if r["dimension"] == "Biomarker testing"]
    assert sum(r["patients"] for r in bm) == bv["systemic_treated"]
    assert sum(r["brand_patients"] for r in bm) == bv["brand_patients"]
    assert sum(x["patients"] for x in bv["distribution"]) == bv["brand_patients"]
    small = J.overview("ALECENSA", "brand")
    assert small["context"]["small"]
    r = run_tool("patient_journey", {"view": "brand", "product": "KEYTRUDA"})
    assert r.kpis and r.charts
