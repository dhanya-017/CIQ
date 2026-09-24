"""Pathway diagrams: code boxes, validated edits that recompute, custom-vs-standard labelling."""
import pytest

from ciq.tools import journey as J, pathway as P
from ciq.tools.registry import run_tool


def _hist(res, cid="p1"):
    return [dict(res.to_dict(), call_id=cid)]


def test_standard_matches_journey_view():
    v = P.pathway_diagram(history=[]).meta["visual"]
    o = J.overview()
    assert not v["custom"] and v["n"] == o["context"]["patients"]
    assert [s["patients"] for s in v["stages"]] == [s["patients"] for s in o["stages"]]
    for t, u in zip(v["transitions"], o["transitions"]):
        assert (t["reached"], t["not_reached"]) == (u["reached"], u["not_reached"])
    tissue = next(s for s in v["stages"] if s["key"] == "tissue")
    assert tissue["codes"][0]["code"] == "88305" and tissue["codes"][0]["desc"]


def test_edits_recompute_and_are_labelled():
    base = P.pathway_diagram(history=[])
    r = P.pathway_diagram("p1", [{"op": "remove_codes", "stage": "tissue", "codes": ["px:88305"]},
                                 {"op": "add_stage", "label": "Brain MRI", "codes": ["px:70551", "px:70553"],
                                  "after": "imaging"},
                                 {"op": "highlight", "transition": "diagnosis>biomarker"}], history=_hist(base))
    v = r.meta["visual"]
    tissue = next(s for s in v["stages"] if s["key"] == "tissue")
    assert v["custom"] and tissue["changed"] and tissue["vs_standard"] < 0
    mri = next(s for s in v["stages"] if s["label"] == "Brain MRI")
    assert mri["new"] and mri["window"] == "workup"
    assert v["highlight"] == {"transition": ["diagnosis", "biomarker"]}
    assert any("88305" in d for d in v["diff"]) and "Custom" in r.title
    # a further edit keeps the earlier ones
    r2 = P.pathway_diagram("p2", [{"op": "set_rule", "rule": "stall_days", "value": 90}], history=_hist(r, "p2"))
    assert len(r2.meta["changes"]) == len(r.meta["changes"]) + 1
    assert r2.meta["pathway"]["rules"]["stall_days"] == 90


def test_presentation_only_edit_stays_standard():
    r = P.pathway_diagram(None, [{"op": "highlight", "stage": "biomarker"}], history=[])
    assert not r.meta["visual"]["custom"] and r.meta["visual"]["highlight"] == {"stage": "biomarker"}


@pytest.mark.parametrize("ops, msg", [
    ([{"op": "add_codes", "stage": "imaging", "codes": ["px:99999"]}], "no claims match"),
    ([{"op": "add_codes", "stage": "imaging", "codes": ["71250"]}], "bad code"),
    ([{"op": "remove_stage", "stage": "diagnosis"}], "anchors the cohort"),
    ([{"op": "rename_stage", "stage": "nope", "label": "x"}], "no stage"),
    ([{"op": "remove_codes", "stage": "consult", "codes": ["px:99204", "px:99205"]}], "no codes"),
    ([{"op": "set_rule", "rule": "stall_days", "value": 999}], "between 0 and 365"),
    ([{"op": "explode"}], "unknown op"),
])
def test_invalid_edits_explain(ops, msg):
    with pytest.raises(ValueError, match=msg):
        P.pathway_diagram(None, ops, history=[])


def test_filter_and_merge():
    r = P.pathway_diagram(None, [{"op": "filter", "cohort": "payer:Medicare"},
                                 {"op": "merge_stages", "stages": ["imaging", "consult"], "label": "Work-up"}],
                          history=[])
    v = r.meta["visual"]
    assert v["cohort"] == "Medicare" and v["n"] < J.overview()["context"]["patients"]
    assert [s["label"] for s in v["stages"]].count("Work-up") == 1 and len(v["stages"]) == 6


def test_registry_and_unknown_source():
    r = run_tool("pathway_diagram", {}, [])
    assert r.to_dict()["method"]["definitions"][0].startswith("Suspicion")
    with pytest.raises(ValueError, match="no pathway diagram matches"):
        P.pathway_diagram("missing", [], history=[])
