"""Patient Cohort Builder: nested AND/OR groups of ICD / CPT / NDC criteria."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ciq.tools import cohort_builder as cb


def test_icd_suggest_c34():
    from ciq.tools.base import query
    hits = cb.suggest_codes("ICD", "C34")
    assert hits and hits[0]["code"].replace(".", "").upper() == "C34"
    assert ".xx" not in (hits[0]["code"] or "").lower()
    assert ".xx" not in (hits[0]["label"] or "").lower()
    observed = set(query("select distinct dx_code from fact_dx_event where replace(dx_code, '.', '') ilike 'C34%'").dx_code)
    assert {h["code"] for h in hits[1:]} <= observed


def test_c34_matches_all_subcodes():
    from ciq.tools.base import query
    tree = {"kind": "group", "children": [{"kind": "criterion", "system": "ICD", "code": "C34"}]}
    d = cb.evaluate_tree(tree)
    n = int(query("select count(distinct patient_id) n from fact_dx_event where replace(dx_code, '.', '') ilike 'C34%'").n.iloc[0])
    assert d["size"] == n
    one = cb.evaluate_tree({"kind": "group", "children": [{"kind": "criterion", "system": "ICD", "code": "C34.11"}]})
    fam = cb.evaluate_tree({"kind": "group", "children": [{"kind": "criterion", "system": "ICD", "code": "C34.1"}]})
    assert one["size"] <= fam["size"] <= d["size"]


def test_ndc_search_drug_name_returns_ndc_codes():
    hits = cb.search_codes("Keytruda", system="NDC")
    assert hits
    assert all(h["system"] == "NDC" and h["code"].isdigit() and len(h["code"]) == 11 for h in hits)
    assert any(h["code"] == "00006302602" for h in hits)
    assert all(not h["code"].startswith("J") for h in hits)
    qlex = [h for h in hits if "qlex" in (h["label"] or "").lower()]
    assert qlex and all(h["code"].startswith("00006") for h in qlex)


def test_ndc_suggest_brand():
    hits = cb.suggest_codes("NDC", "KEYTRUDA")
    assert hits
    assert all(h["code"].isdigit() and len(h["code"]) == 11 for h in hits)
    assert any("KEYTRUDA" in (h["label"] or "").upper() for h in hits)


def test_ndc_suggest_returns_ndc_numbers():
    hits = cb.suggest_codes("NDC", "0000")
    assert hits
    assert all(h["system"] == "NDC" and h["code"].isdigit() and len(h["code"]) >= 10 for h in hits)
    assert cb.suggest_codes("NDC", "J9271") == []


def test_cpt_codes_stay_visible_beside_other_systems():
    hits = cb.search_codes("J9")
    cpt = [h for h in hits if h["system"] == "CPT" and h["code"].upper().startswith("J9")]
    assert cpt
    assert all(h["system"] != "NDC" or h["code"].isdigit() for h in hits)
    named = cb.search_codes("Keytruda")
    assert any(h["system"] == "CPT" and h["code"] == "J9271" for h in named)
    assert any(h["system"] == "NDC" and h["code"] == "00006302602" for h in named)
    assert all(h["code"].isdigit() for h in named if h["system"] == "NDC")


def test_cpt_suggest_uses_procedure_codes():
    hits = cb.suggest_codes("CPT", "96")
    assert hits
    assert all(h["system"] == "CPT" and h["code"][:1].isdigit() for h in hits)
    j = cb.suggest_codes("CPT", "J9271")
    assert j and j[0]["code"] == "J9271" and j[0]["system"] == "CPT"
    assert cb.suggest_codes("NDC", "J9271") == []


def test_procedure_code_is_cpt_and_ndc_cd_is_ndc():
    ndc = cb.evaluate_tree({"kind": "group", "children": [
        {"kind": "criterion", "system": "NDC", "code": "J9271"}]})
    cpt = cb.evaluate_tree({"kind": "group", "children": [
        {"kind": "criterion", "system": "CPT", "code": "J9271"}]})
    assert ndc["size"] == 0
    assert cpt["size"] > 0
    assert "ndc_cd" in (ndc["sql"] or "")
    assert "procedure_code" in (cpt["sql"] or "")


def test_evaluate_lung_dx():
    tree = {"kind": "group", "op": "AND", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"}]}
    d = cb.evaluate_tree(tree)
    assert d["size"] > 0 and d["size"] <= d["universe"]


def test_and_or_tree():
    tree = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA", "join": "AND"}]}
    d = cb.evaluate_tree(tree)
    assert d["size"] > 0
    assert "KEYTRUDA" in cb.summarize_tree(tree)


def test_parse_lung_cancer():
    t = cb.parse_question("How many patients are Diagnosed with Lung Cancer? %C34%")
    assert t and "C34" in cb.summarize_tree(t)
    d = cb.evaluate_tree(t)
    assert d["size"] > 0


def test_parse_c34_and_c78():
    t = cb.parse_question(
        "How many patients are Diagnosed with Lung Cancer (%C34%) and Diagnosed with NSCLC (%C78%,%C79%)?")
    lab = cb.summarize_tree(t)
    assert "C34" in lab and "C78" in lab and "C79" in lab
    d = cb.evaluate_tree(t)
    assert 0 < d["size"] < d["universe"]


def test_parse_c34_and_io_drugs():
    t = cb.parse_question(
        "How many patients are Diagnosed with Lung Cancer (%C34%) and taken the Drugs "
        "(KEYTRUDA/ KEYTRUDA QLEX/ OPDIVO/ OPDIVO QVANTIG/ TECENTRIQ/ TECENTRIQ HYBREZA)?")
    lab = cb.summarize_tree(t)
    assert "C34" in lab and "KEYTRUDA" in lab and "OPDIVO" in lab
    d = cb.evaluate_tree(t)
    assert d["size"] > 0


def test_multi_codes_in_one_criterion():
    tree = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "codes": [{"code": "C34"}, {"code": "C34.1"}]}]}
    d = cb.evaluate_tree(tree)
    assert d["size"] > 0
    label = cb.summarize_tree(tree)
    assert "C34" in label and "C34.1" in label


def test_sequential_c34_then_c78():
    and_tree = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "ICD", "code": "C78", "join": "AND"}]}
    seq_tree = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "ICD", "code": "C78", "join": "SEQ",
         "sequence": {"window": "90", "direction": "preceding", "occurrence": 1}}]}
    both = cb.evaluate_tree(and_tree)
    seq = cb.evaluate_tree(seq_tree)
    assert seq["size"] > 0
    assert seq["size"] <= both["size"]
    assert "interval 90 day" in (seq["sql"] or "")
    assert "SEQ[90d x1]" in cb.summarize_tree(seq_tree)


def test_parse_drops_codes_not_in_extract():
    t = cb.parse_question("patients with %QQ99%")
    assert t is None


def test_parse_question_uses_study_period():
    t = cb.parse_question("How many patients have %C34%?")
    period = {"from": "2023-02-01", "to": "2024-12-31"}
    asked = cb.evaluate_tree(t, period)
    manual = cb.evaluate_tree(
        {"kind": "group", "children": [{"kind": "criterion", "system": "ICD", "code": "C34"}]},
        period)
    assert asked["size"] == manual["size"]
    assert asked["universe"] == manual["universe"]
    ans = cb.ask_answer(asked)
    assert "index-date filter" in ans
    assert "2023-02-01" in ans


def test_parse_followed_by():
    t = cb.parse_question(
        "patients with %C34% followed by %C78% within 90 days at least once")
    lab = cb.summarize_tree(t)
    assert "C34" in lab and "C78" in lab and "SEQ" in lab
    d = cb.evaluate_tree(t)
    assert d["size"] > 0


def test_nested_group_vs_criteria_combo():
    """Parent AND/OR rows combine first, then that set is joined to the nested group."""
    tree = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "ICD", "code": "C78", "join": "OR"},
        {"kind": "group", "join": "AND", "children": [
            {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA"}]}]}
    flat = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "ICD", "code": "C78", "join": "OR"},
        {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA", "join": "AND"}]}
    nested = cb.evaluate_tree(tree)
    same = cb.evaluate_tree(flat)
    assert nested["size"] == same["size"] > 0
    assert "KEYTRUDA" in cb.summarize_tree(tree)
    interleaved = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "group", "join": "AND", "children": [
            {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA"}]},
        {"kind": "criterion", "system": "ICD", "code": "C78", "join": "OR"}]}
    assert cb.evaluate_tree(interleaved)["size"] == nested["size"]


def _first_ever_in_window(where: str, start: str, end: str) -> int:
    from ciq.tools.base import query
    return int(query(f"""
        select count(*) n from (
          select patient_id, min(service_date) as index_date
          from fact_dx_event
          {where}
          group by 1
          having min(service_date) >= DATE '{start}'
             and min(service_date) <= DATE '{end}')
    """).n.iloc[0])


def _icd_stems(n: int = 2) -> list[str]:
    from ciq.tools.base import query
    df = query("""
        select left(replace(dx_code, '.', ''), 3) as stem
        from fact_dx_event
        group by 1
        order by count(distinct patient_id) desc
        limit 8
    """)
    return [str(s) for s in df.stem.tolist()[:n]]


def _icd_group(*codes: str, join: str = "AND") -> dict:
    kids = [{"kind": "criterion", "system": "ICD", "code": codes[0]}]
    for c in codes[1:]:
        kids.append({"kind": "criterion", "system": "ICD", "code": c, "join": join})
    return {"kind": "group", "children": kids}


def test_study_period_index_date():
    """ICD + dates: first-ever qualifying diagnosis in From-To is the index set."""
    from ciq.tools.base import coverage
    cov = coverage()
    period = {"from": cov["data_start"], "to": cov["data_end"]}
    stem = _icd_stems(1)[0]
    tree = _icd_group(stem)
    dx = cb.evaluate_tree(tree)
    both = cb.evaluate_tree(tree, period)
    any_dx = cb.evaluate_tree(None, period)
    want = _first_ever_in_window(
        f"where replace(dx_code, '.', '') ilike '{stem}%'", cov["data_start"], cov["data_end"])
    assert both["size"] == both["universe"] == want
    assert both["size"] <= dx["size"]
    assert both["size"] <= any_dx["size"]
    assert stem in (both["sql"] or "")
    assert f"first ICD:{stem}" in cb.summarize_tree(tree, period)
    assert any_dx["size"] == _first_ever_in_window("", cov["data_start"], cov["data_end"])
    narrow = {"from": cov["data_start"], "to": cov["data_start"]}
    early = cb.evaluate_tree(tree, narrow)
    assert early["size"] <= both["size"]


def test_dates_only_are_first_any_diagnosis():
    """Dates alone = first any diagnosis; ICD codes become the qualifying index set."""
    period = {"from": "2023-01-01", "to": "2023-12-31"}
    stem = _icd_stems(1)[0]
    empty = cb.evaluate_tree(None, period)
    coded = cb.evaluate_tree(_icd_group(stem), period)
    want_any = _first_ever_in_window("", "2023-01-01", "2023-12-31")
    want_stem = _first_ever_in_window(
        f"where replace(dx_code, '.', '') ilike '{stem}%'", "2023-01-01", "2023-12-31")
    assert empty["size"] == empty["universe"] == want_any
    assert coded["size"] == coded["universe"] == want_stem
    assert stem in (coded["sql"] or "")
    # First qualifying ICD can fall in-window even if an earlier non-qualifying
    # diagnosis was before Start, so this set need not sit inside dates-only.


def test_and_or_seq_stay_inside_index_filter():
    """With dates, ICD codes are one qualifying OR set; first diagnosis must fall in From-To."""
    period = {"from": "2023-02-01", "to": "2024-12-31"}
    a, b = (_icd_stems(2) + [None, None])[:2]
    dates = cb.evaluate_tree(None, period)
    want = _first_ever_in_window("", "2023-02-01", "2024-12-31")
    assert dates["size"] == dates["universe"] == want
    one = cb.evaluate_tree(_icd_group(a), period)
    assert one["size"] == one["universe"]
    want_a = _first_ever_in_window(
        f"where replace(dx_code, '.', '') ilike '{a}%'", "2023-02-01", "2024-12-31")
    assert one["size"] == want_a
    if not b or b == a:
        return
    anded = cb.evaluate_tree(_icd_group(a, b, join="AND"), period)
    ored = cb.evaluate_tree(_icd_group(a, b, join="OR"), period)
    seq = cb.evaluate_tree(_icd_group(a, b, join="SEQUENTIAL EVENT"), period)
    want_ab = _first_ever_in_window(
        f"where replace(dx_code, '.', '') ilike '{a}%' or replace(dx_code, '.', '') ilike '{b}%'",
        "2023-02-01", "2024-12-31")
    assert anded["size"] == ored["size"] == seq["size"] == want_ab


def test_followup_is_post_index_not_clipped_at_to():
    """Criteria match on/after first diagnosis through the extract, not before index and not cut at To."""
    from datetime import date

    from ciq.tools.base import query

    period = {"from": "2023-01-01", "to": "2023-12-31"}
    tree = {"kind": "group", "children": [
        {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA"}]}
    d = cb.evaluate_tree(tree, period)
    sql = d["sql"] or ""
    assert "service_date >= i.index_date" in sql
    assert "service_date <= DATE '2023-12-31'" not in sql
    idx = cb.index_dates_sql(tree, *cb.parse_study_period(period))
    post = cb._code_sql("NDC", "KEYTRUDA", after_index=True)
    clipped = cb._code_sql("NDC", "KEYTRUDA", after_index=True, window_end=date(2023, 12, 31))
    pre = (post or "").replace("e.service_date >= i.index_date", "e.service_date < i.index_date")
    n_post = int(query(f"select count(*) n from ({idx}) i where {post}").n.iloc[0])
    n_clip = int(query(f"select count(*) n from ({idx}) i where {clipped}").n.iloc[0])
    n_pre_only = int(query(
        f"select count(*) n from ({idx}) i where {pre} and not {post}").n.iloc[0])
    assert d["size"] == n_post
    assert d["size"] >= n_clip
    assert n_pre_only >= 0
    assert d["size"] == n_post  # pre-index KEYTRUDA alone does not qualify


def test_ndc_and_cpt_both_required_after_index():
    """If both NDC and CPT are given, the patient needs at least one of each after index."""
    from ciq.tools.base import query

    period = {"from": "2023-01-01", "to": "2023-12-31"}
    px = str(query(
        "select px_code from fact_px_event "
        "group by 1 order by count(*) desc limit 1"
    ).px_code.iloc[0])
    icd = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"}]}
    ndc = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA", "join": "AND"}]}
    both = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "NDC", "code": "KEYTRUDA", "join": "AND"},
        {"kind": "criterion", "system": "CPT", "code": px, "join": "AND"}]}
    n_icd = cb.evaluate_tree(icd, period)
    n_ndc = cb.evaluate_tree(ndc, period)
    n_both = cb.evaluate_tree(both, period)
    assert n_icd["size"] == n_icd["universe"]
    assert n_ndc["size"] <= n_icd["size"]
    assert n_both["size"] <= n_ndc["size"]
    sql = n_both["sql"] or ""
    assert "fact_drug_event" in sql and "fact_px_event" in sql
    assert " and " in sql
    assert "NDC:KEYTRUDA" in cb.summarize_tree(both, period)
    assert f"CPT:{px}" in cb.summarize_tree(both, period)


def test_or_between_ndc_and_cpt_is_not_and():
    """The OR between NDC and CPT is kept when a study period sets the index."""
    period = {"from": "2023-01-01", "to": "2023-12-31"}
    px = "J9271"
    both_and = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "NDC", "code": "00006302602", "join": "AND"},
        {"kind": "criterion", "system": "CPT", "code": px, "join": "AND"}]}
    both_or = {"kind": "group", "children": [
        {"kind": "criterion", "system": "ICD", "code": "C34"},
        {"kind": "criterion", "system": "NDC", "code": "00006302602", "join": "AND"},
        {"kind": "criterion", "system": "CPT", "code": px, "join": "OR"}]}
    n_and = cb.evaluate_tree(both_and, period)
    n_or = cb.evaluate_tree(both_or, period)
    assert " or exists " in (n_or["sql"] or "")
    assert " or exists " not in (n_and["sql"] or "")
    assert n_or["size"] >= n_and["size"]
    summary = cb.summarize_tree(both_or, period)
    assert " OR " in summary
    assert "NDC:00006302602" in summary and f"CPT:{px}" in summary

