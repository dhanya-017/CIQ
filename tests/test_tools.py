"""Smoke tests: every tool runs, returns data, and serialises to JSON.

Run: python -m pytest tests -q      (or: python tests/test_tools.py)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ciq.tools.registry import TOOLS, anthropic_tools, run_tool

CASES = [(t.name, {}) for t in TOOLS if t.name not in ("run_sql", "compare_products")] + [
    ("compare_products", {"products": ["KEYTRUDA", "opdivo", "TECENTRIQ"]}),
    ("compare_products", {"products": ["TAGRISSO", "ALECENSA"]}),
    ("run_sql", {"sql": "select product, count(*) n from fact_drug_event group by 1 order by n desc limit 5"}),
    ("patient_demographics", {"product": "opdivo"}),
    ("pre_index_treatments", {"product": "TAGRISSO", "level": "product"}),
    ("source_of_business", {"product": "TECENTRIQ", "level": "patient", "full_lookback_only": True}),
    ("dx_around_index", {"product": "pembrolizumab", "period": "post", "code_level": "category"}),
    ("persistency", {"product": "IMFINZI", "grace_days": 45}),
    ("hcp_market_concentration", {"product": "OPDIVO", "method": "volume"}),
    ("combination_therapy", {"product": "KEYTRUDA"}),
    ("market_incidence_prevalence", {"grain": "quarter"}),
]


@pytest.mark.parametrize("name,args", CASES)
def test_tool(name, args):
    res = run_tool(name, args)
    d = res.to_dict()
    json.dumps(d)  # must be JSON serialisable
    data = d["data"]
    assert (len(data) > 0) if isinstance(data, list) else any(len(v) for v in data.values())
    assert res.to_llm_text()


def test_schemas():
    tools = anthropic_tools()
    assert len({t["name"] for t in tools}) == len(tools)
    json.dumps(tools)


def test_unknown_product():
    with pytest.raises(ValueError, match="Unknown product"):
        run_tool("patient_demographics", {"product": "KEYTRUDDA_X"})


def test_sql_guard():
    with pytest.raises(ValueError):
        run_tool("run_sql", {"sql": "drop table dim_patient"})


TOP_PRODUCTS = "select product from fact_drug_event group by 1 order by count(distinct patient_id) desc limit 4"
CHART_CASES = [
    ("bar", "select age_group, count(*) patients from dim_patient where is_treated group by 1",
     {"x": "age_group", "y": "patients", "sort": "none"}),
    ("line", f"select date_trunc('month', service_date) as month_start, product, count(distinct patient_id) patients "
             f"from fact_drug_event where product in ({TOP_PRODUCTS}) group by 1, 2 order by 1",
     {"x": "month_start", "y": "patients", "color": "product"}),
    ("stacked_bar", "select 'Line ' || line_number line, combination_type, count(*) n from lot_line "
                    "where line_number <= 3 group by 1, 2 order by 1", {"x": "line", "y": "n", "color": "combination_type",
                                                                        "normalize": True, "y_format": "percent"}),
    ("grouped_bar", f"select payment_type, product, count(distinct patient_id) patients from fact_drug_event "
                    f"where product in ({TOP_PRODUCTS}) group by 1, 2", {"x": "payment_type", "y": "patients", "color": "product"}),
    ("donut", "select payment_type, count(*) n from dim_patient group by 1", {"x": "payment_type", "y": "n"}),
    ("scatter", "select product, count(distinct patient_id) patients, avg(days_supply) avg_days from fact_drug_event "
                "group by 1 having count(*) > 50", {"x": "patients", "y": "avg_days", "label": "product"}),
    ("heatmap", f"select d.product, p.age_group, count(distinct d.patient_id) patients from fact_drug_event d "
                f"join dim_patient p using (patient_id) where d.product in ({TOP_PRODUCTS}) group by 1, 2",
     {"x": "age_group", "y": "product", "value": "patients"}),
]


@pytest.mark.parametrize("kind,sql,chart", CHART_CASES)
def test_run_sql_chart(kind, sql, chart):
    res = run_tool("run_sql", {"sql": sql, "chart": {"type": kind, "title": f"Test {kind}", **chart}})
    d = res.to_dict()
    json.dumps(d)
    assert len(d["charts"]) == 1 and d["title"] == f"Test {kind}"
    assert "## chart shown to the user" in res.to_llm_text()


@pytest.mark.parametrize("chart,reason", [
    ({"type": "bar", "x": "age_grp", "y": "patients"}, "not a column"),       # typo'd column
    ({"type": "bar", "x": "patients", "y": "age_group"}, "must be numeric"),   # measure on the wrong axis
    ({"type": "grouped_bar", "x": "age_group", "y": "patients"}, "color is required"),
    ({"type": "radar", "x": "age_group", "y": "patients"}, "chart.type"),
])
def test_run_sql_chart_errors_go_back_to_model(chart, reason):
    # errors raise so the agent loop returns them as is_error and the model retries with a fixed spec
    with pytest.raises(ValueError, match=reason) as e:
        run_tool("run_sql", {"sql": "select age_group, count(*) patients from dim_patient group by 1", "chart": chart})
    assert "columns: age_group" in str(e.value) and "call run_sql again" in str(e.value)


def test_chart_summary_flags_percent_mismatch():
    res = run_tool("run_sql", {"sql": "select age_group, 100.0 * count(*) / sum(count(*)) over () pct from dim_patient group by 1",
                               "chart": {"type": "bar", "x": "age_group", "y": "pct", "y_format": "percent"}})
    assert "WARNING" in res.meta["chart_summary"]


if __name__ == "__main__":
    for n, a in CASES:
        r = run_tool(n, a)
        print(f"\n=== {n} {a}\n" + "\n".join("  * " + i for i in r.insights))
