"""Market Command Center: every signal is evidence-backed and actionable; lenses render; nothing is invented.

Run: python -m pytest tests -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ciq.tools import command_center as cc


@pytest.mark.parametrize("product,window", [("KEYTRUDA", 0), ("KEYTRUDA", 1), ("OPDIVO", 1)])
def test_build(product, window):
    d = cc.build(product, window)
    json.dumps(d)
    c = d["context"]
    assert c["product"] == product and c["window"] == window
    if window == 0:
        assert c["prior"] is None
    else:
        assert c["prior"]["label"] != c["current"]["label"]
    # market $ is not in claims: shown as unavailable, never estimated
    value = next(k for k in d["market"] if k["id"] == "value")
    assert value["value"] is None and value["unavailable"]
    assert all(k["delta"].get("coverage") for k in d["market"] if k.get("delta"))
    assert d["signals"]
    if window:
        assert d["signals"][0]["id"] == "share"
    for s in d["signals"]:
        assert s["headline"] and s["evidence"] and s["action"]["kind"] in ("time", "brands", "hcp")
        if "hcps" in s:
            assert len(s["hcps"]["hcp_ids"]) >= len(s["hcps"]["rows"]) > 0
    assert set(d["lenses"]) == {"market", "hcps", "patients", "competitors"}
    assert all(lens["charts"] and lens["questions"] for lens in d["lenses"].values())
    snap = d["snapshot"]
    assert snap["total_patients"] > 0
    assert [b["product"] for b in snap["brands"]] == ["KEYTRUDA", "OPDIVO", "TECENTRIQ"]
    assert all(b["patients"] > 0 for b in snap["brands"])
    assert all(b["patients"] <= snap["total_patients"] for b in snap["brands"])
    if window == 0:
        mix_segs = {v["segment"] for v in snap["mix_chart"]["data"]["values"]}
        assert mix_segs == {"Incidence", "Prevalence"}
        assert snap["mix_chart"]["title"]["text"] == "Incidence and Prevalence of NSCLC"
        assert "product" not in snap["mix_chart"]["data"]["values"][0]
        assert snap["mix_chart"] and len({v["period"] for v in snap["mix_chart"]["data"]["values"]}) > 1
        trend_prods = {v["product"] for v in snap["brand_trend"]["data"]["values"]}
        assert trend_prods == {"KEYTRUDA", "OPDIVO", "TECENTRIQ"}
        src = snap["brand_source"]
        assert src
        src_vals = src["data"]["values"]
        assert {v["product"] for v in src_vals} == {"KEYTRUDA", "OPDIVO", "TECENTRIQ"}
        assert {v["segment"] for v in src_vals} == {"New", "Continued", "Switch"}
        assert src["encoding"]["color"]["scale"]["range"][0] == "#007A73"
        assert src["encoding"]["color"]["scale"]["domain"][:3] == [
            "New to Keytruda", "Continuing on Keytruda", "Switched to Keytruda"]
        switches = [v for v in src_vals if v["segment"] == "Switch"]
        assert switches and all(
            v.get("IV") is not None and v.get("SUBQ") is not None and v["IV"] + v["SUBQ"] == v["patients"]
            for v in switches)
        assert all(v.get("IV") is None and v.get("SUBQ") is None
                   for v in src_vals if v["segment"] != "Switch")
        reach = snap["lot_reach_chart"]
        assert reach and "Patients reaching each line" in reach["title"]["text"]
        reach_vals = reach["data"]["values"]
        assert reach_vals and reach_vals[0]["axis"] == "1L"
        assert reach_vals[0]["patients"] > 0
        assert "(" in reach_vals[0]["caption"] and "\n" not in reach_vals[0]["caption"]
        assert reach["config"]["axis"]["grid"] is False
        regs = snap["lot_regimen_charts"]
        assert regs and 1 <= len(regs) <= 3
        assert "1L" in regs[0]["title"]["text"]
        assert regs[0]["layer"][0]["encoding"]["y"]["title"] == "Product Combinations"
        assert regs[0]["layer"][0]["encoding"]["y"]["axis"]["titleOrient"] == "left"
        assert regs[0]["layer"][0]["encoding"]["y"]["axis"].get("minExtent", 0) >= 200
        assert regs[0]["layer"][0]["encoding"]["y"]["sort"] == "-x"
        assert regs[0]["data"]["values"]
        dot = snap["lot_dot_chart"]
        assert dot and dot["title"]["text"] == "Days on therapy by line"
        dot_vals = dot["data"]["values"]
        assert {v["stat"] for v in dot_vals} == {"Mean days", "Median days"}
        assert {v["line"] for v in dot_vals} == set(range(1, 11))
        l1 = [v for v in dot_vals if v["line"] == 1]
        assert l1 and all(v["n"] > 0 and 20 < v["days"] < 250 for v in l1)
        mol = snap["lot_molecule_chart"]
        assert mol and mol["title"]["text"] == "Molecule share by line of therapy"
        mol_vals = mol["data"]["values"]
        assert mol_vals and {v["line"] for v in mol_vals} <= {1, 2, 3}
        assert {v["drug"] for v in mol_vals} == {"pembrolizumab", "atezolizumab", "nivolumab"}
        tot = {}
        for v in mol_vals:
            tot[v["drug"]] = tot.get(v["drug"], 0) + int(v["n_patients"])
        assert mol["encoding"]["y"]["sort"] == sorted(tot, key=tot.get, reverse=True)
        pembro_1l = [v for v in mol_vals if v["drug"] == "pembrolizumab" and v["line"] == 1]
        assert pembro_1l and pembro_1l[0]["pct"] > 0
        assert mol["encoding"]["x"]["scale"]["domain"] == [0, 100]
        flow = snap["lot_flow_chart"]
        assert flow and "1L → 2L flow" in flow["title"]["text"]
        assert "pembrolizumab" in flow["title"]["text"]
        assert flow["layer"][0]["mark"]["type"] == "area"
        ribbon = flow["layer"][0]["data"]["values"]
        assert ribbon and ribbon[0]["n"] >= 1
        assert "L1" in ribbon[0] and "L2" in ribbon[0]
    else:
        assert snap["mix_chart"] is None
        assert snap["brand_trend"] is None
        assert snap["brand_source"] is None
        assert snap.get("lot_reach_chart") is None
        assert snap.get("lot_regimen_charts") is None
        assert snap.get("lot_dot_chart") is None
        assert snap.get("lot_molecule_chart") is None
        assert snap.get("lot_flow_chart") is None


def test_signal_actions_run():
    """Each signal's Explore action is a valid comparison."""
    from ciq.tools.compare import compare_segments
    for s in cc.build("KEYTRUDA", 1)["signals"]:
        a = s["action"]
        if a["a"] and a["b"]:
            compare_segments(a["kind"], a["a"], a["b"], "KEYTRUDA")
