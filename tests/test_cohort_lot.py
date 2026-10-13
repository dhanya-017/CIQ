"""Notebook LOT rules used by the patient cohort builder."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ciq.tools import cohort_lot as lot


def _admin(rows):
    df = pd.DataFrame(rows, columns=["patient_id", "_date", "agent"])
    df["_date"] = pd.to_datetime(df["_date"])
    return df


def test_mapping_file_standardizes_agents():
    m = lot.load_agent_map()
    assert not m.empty
    assert "pembrolizumab" in set(m.agent)
    assert "nab-paclitaxel" in set(m.agent)
    qlex = m[m.code_key == "J9277"]
    assert len(qlex) == 1 and qlex.agent.iloc[0] == "pembrolizumab"


def test_cycle_splits_when_gap_over_4_days():
    admin = _admin([
        ("p", "2023-01-01", "pembrolizumab"),
        ("p", "2023-01-03", "pembrolizumab"),
        ("p", "2023-01-10", "pembrolizumab"),
    ])
    cyc = lot.build_cycles(admin, 4)
    assert list(cyc.cycle_num) == [1, 2]


def test_gap_over_90_days_starts_new_line():
    admin = _admin([
        ("p", "2023-01-01", "pembrolizumab"),
        ("p", "2023-01-22", "pembrolizumab"),
        ("p", "2023-05-01", "pembrolizumab"),
    ])
    lined = lot.lines_from_admin(admin)
    by_cycle = lined.set_index("cycle_num")["line_number"].to_dict()
    assert by_cycle[1] == 1 and by_cycle[2] == 1 and by_cycle[3] == 2


def test_gap_of_90_days_stays_on_same_line():
    admin = _admin([
        ("p", "2023-01-01", "pembrolizumab"),
        ("p", "2023-01-22", "pembrolizumab"),
        ("p", "2023-04-22", "pembrolizumab"),
    ])
    lined = lot.lines_from_admin(admin)
    assert lined.line_number.max() == 1


def test_new_agent_before_min_cycles_does_not_advance():
    admin = _admin([
        ("p", "2023-01-01", "pembrolizumab"),
        ("p", "2023-01-10", "carboplatin"),
    ])
    lined = lot.lines_from_admin(admin)
    assert lined.line_number.max() == 1
    assert "carboplatin" in lined.line_regimen.iloc[-1]


def test_new_agent_after_two_cycles_and_30_days_advances():
    admin = _admin([
        ("p", "2023-01-01", "pembrolizumab"),
        ("p", "2023-01-22", "pembrolizumab"),
        ("p", "2023-02-12", "pembrolizumab"),
        ("p", "2023-02-20", "nivolumab"),
    ])
    lined = lot.lines_from_admin(admin)
    last = lined.sort_values("cycle_num").iloc[-1]
    assert last.line_number == 2
    assert last.line_regimen == "nivolumab"


def test_days_on_therapy_is_line_end_minus_start():
    admin = _admin([
        ("a", "2023-01-01", "pembrolizumab"),
        ("a", "2023-01-22", "pembrolizumab"),
        ("b", "2023-01-01", "pembrolizumab"),
        ("b", "2023-02-10", "pembrolizumab"),
    ])
    lined = lot.lines_from_admin(admin)
    stats = {r["line"]: r for r in lot.days_on_therapy_by_line(lined)}
    r = stats[1]
    assert r["n"] == 2
    assert r["mean"] == 30.5
    assert r["median"] == 30.5
    assert stats[2]["n"] == 0
    assert stats[2]["mean"] is None


def test_molecule_share_counts_each_agent_on_the_line():
    admin = _admin([
        ("a", "2023-01-01", "pembrolizumab"),
        ("a", "2023-01-01", "carboplatin"),
        ("b", "2023-01-01", "pembrolizumab"),
    ])
    lined = lot.lines_from_admin(admin)
    mol = lot.molecule_share_by_line(lined, lines=(1,), top_n=5)
    by = {r["drug"]: r for r in mol["share"] if r["line"] == 1}
    assert mol["lines"][0]["n"] == 2
    assert by["pembrolizumab"]["n_patients"] == 2
    assert by["pembrolizumab"]["pct_of_line"] == 100.0
    assert by["carboplatin"]["n_patients"] == 1
    assert by["carboplatin"]["pct_of_line"] == 50.0
    assert mol["drugs"][0] == "pembrolizumab"


def test_cohort_lot_uses_post_index_journey_past_study_to():
    """LOT uses mapped drugs from first diagnosis forward; study To does not cut the journey."""
    from ciq.tools import cohort_builder as cb
    from ciq.tools.base import coverage

    cov = coverage()
    period = {"from": cov["data_start"], "to": cov["data_start"]}
    pack = cb.lot_for_tree(None, period)
    start, end = cb.parse_study_period(period)
    pred, _ = cb._combine_pred(None, period)
    idx = cb.index_dates_sql(None, start, end)
    open_lot = lot.for_cohort(pred, idx)
    clip_lot = lot.for_cohort(pred, idx, window_end=end)
    assert pack["lot"]["patients_with_lot"] == open_lot["patients_with_lot"]
    assert pack["lot"]["patients_with_lot"] >= clip_lot["patients_with_lot"]
    assert pack["lot"]["patients"] == open_lot["patients"]


def test_l1_l2_flow_keeps_io_on_either_line():
    lined = pd.DataFrame([
        ("a", 1, "pembrolizumab"),
        ("a", 2, "nivolumab"),
        ("b", 1, "carboplatin + paclitaxel"),
        ("b", 2, "pembrolizumab"),
        ("c", 1, "carboplatin"),
        ("c", 2, "gemcitabine"),
        ("d", 1, "pembrolizumab"),
        ("d", 2, "pembrolizumab"),
    ], columns=["patient_id", "line_number", "line_regimen"])
    flow = lot.l1_l2_flow(lined)
    pairs = {(r["L1"], r["L2"], r["n"]) for r in flow["flows"]}
    assert flow["n_patients"] == 3
    assert ("pembrolizumab", "nivolumab", 1) in pairs
    assert ("carboplatin + paclitaxel", "pembrolizumab", 1) in pairs
    assert ("carboplatin", "gemcitabine", 1) not in pairs
    both = lot.l1_l2_flow(lined, side="both")
    assert both["n_patients"] == 2


def test_lot_uses_all_mapped_drugs_not_only_filter_ndc():
    """NDC filter only chooses patients; LOT still sees other post-index treatments."""
    from ciq.tools import cohort_builder as cb

    period = {"from": "2023-01-01", "to": "2023-12-31"}
    tree = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA", "join": "AND"}]}
    pack = cb.lot_for_tree(tree, period)
    lot = pack["lot"] or {}
    assert lot["patients"] == cb.evaluate_tree(tree, period)["size"]
    assert lot["patients_with_lot"] <= lot["patients"]
    regs = [r.get("regimen") or "" for r in (lot.get("top_regimens") or [])]
    assert regs
    assert any("pembrolizumab" in r for r in regs)
    # Filter NDC does not strip other agents from the post-index window.
    assert any("+" in r or r != "pembrolizumab" for r in regs)

