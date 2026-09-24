"""Market Command Center: every signal is evidence-backed and actionable; lenses render; nothing is invented.

Run: python -m pytest tests -q
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from ciq.tools import command_center as cc


@pytest.mark.parametrize("product,window", [("KEYTRUDA", 3), ("KEYTRUDA", 1), ("OPDIVO", 2)])
def test_build(product, window):
    d = cc.build(product, window)
    json.dumps(d)
    c = d["context"]
    assert c["product"] == product and c["window"] == window and c["prior"]["label"] != c["current"]["label"]
    # market $ is not in claims: shown as unavailable, never estimated
    value = next(k for k in d["market"] if k["id"] == "value")
    assert value["value"] is None and value["unavailable"]
    assert all(k["delta"].get("coverage") for k in d["market"] if k.get("delta"))
    assert d["signals"] and d["signals"][0]["id"] == "share"
    for s in d["signals"]:
        assert s["headline"] and s["evidence"] and s["action"]["kind"] in ("time", "brands", "hcp")
        if "hcps" in s:
            assert len(s["hcps"]["hcp_ids"]) >= len(s["hcps"]["rows"]) > 0
    assert set(d["lenses"]) == {"market", "hcps", "patients", "competitors"}
    assert all(lens["charts"] and lens["questions"] for lens in d["lenses"].values())


def test_signal_actions_run():
    """Each signal's Explore action is a valid comparison."""
    from ciq.tools.compare import compare_segments
    for s in cc.build("KEYTRUDA", 3)["signals"]:
        a = s["action"]
        if a["a"] and a["b"]:
            compare_segments(a["kind"], a["a"], a["b"], "KEYTRUDA")
