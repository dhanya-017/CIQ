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


if __name__ == "__main__":
    for n, a in CASES:
        r = run_tool(n, a)
        print(f"\n=== {n} {a}\n" + "\n".join("  * " + i for i in r.insights))
