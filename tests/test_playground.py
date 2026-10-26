"""Playground Tab: default vs user-defined LOT rules."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ciq.tools import cohort_lot as lot
from ciq.tools import command_center as cc


def test_parse_rules_defaults_and_clamp():
    d = lot.default_rules()
    assert d["cycle_window_days"] == 4
    assert d["line_gap_days"] == 90
    assert d["min_cycles"] == 2
    assert d["min_days"] == 30
    assert lot.parse_rules(None) == d
    custom = lot.parse_rules({"cycle_window_days": 7, "line_gap_days": 120})
    assert custom["cycle_window_days"] == 7
    assert custom["line_gap_days"] == 120
    assert custom["min_cycles"] == d["min_cycles"]
    assert custom["min_days"] == d["min_days"]


def test_parse_rules_rejects_out_of_range():
    try:
        lot.parse_rules({"min_days": 0})
        assert False
    except ValueError:
        pass


def test_playground_defaults_match_config():
    d = cc.playground_defaults()
    assert d == lot.default_rules()


def test_playground_lot_charts_and_custom_gap():
    base = cc.playground_lot(None)
    assert base["rules"] == lot.default_rules()
    assert base["patients"] > 0
    assert base["lot_reach_chart"]
    assert base["lot_regimen_charts"]
    assert base["lot_dot_chart"]
    assert base["lot_molecule_chart"]
    assert "lot_flow_chart" not in base
    wide = cc.playground_lot({"line_gap_days": 180})
    assert wide["rules"]["line_gap_days"] == 180
    assert wide["patients"] == base["patients"]
    assert wide["lot_reach_chart"]["title"]["text"].startswith("Patients reaching")
