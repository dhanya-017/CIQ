"""Pinned landing-page charts."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import pins


def test_add_and_remove_pin(tmp_path, monkeypatch):
    monkeypatch.setattr(pins, "PIN_PATH", tmp_path / "pins.json")
    p = pins.add_pin({"title": "Share", "key": "k1", "charts": [{"mark": "bar", "data": {"values": []}}]})
    assert p["id"] and p["title"] == "Share"
    assert len(pins.list_pins()) == 1
    again = pins.add_pin({"title": "Share", "key": "k1", "charts": [{"mark": "bar"}]})
    assert again["id"] == p["id"]
    assert pins.remove_pin(p["id"])
    assert pins.list_pins() == []
