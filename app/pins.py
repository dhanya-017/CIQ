"""Pinned landing-page visualisations (charts copied from chat results)."""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from app.settings import ROOT

PIN_PATH = ROOT.parent / "Data" / "pins.json"
MAX_PINS = 40
_lock = threading.Lock()


def _load() -> list[dict]:
    if not PIN_PATH.exists():
        return []
    try:
        data = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save(pins: list[dict]) -> None:
    PIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PIN_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(pins, default=str), encoding="utf-8")
    tmp.replace(PIN_PATH)


def list_pins() -> list[dict]:
    with _lock:
        return list(_load())


def add_pin(payload: dict) -> dict:
    pin = {
        "id": uuid.uuid4().hex[:12],
        "key": str(payload.get("key") or "")[:240],
        "title": str(payload.get("title") or "Pinned chart")[:200],
        "subtitle": str(payload.get("subtitle") or "")[:400],
        "tool": str(payload.get("tool") or "")[:80],
        "product": payload.get("product"),
        "conversation_id": payload.get("conversation_id"),
        "charts": payload.get("charts") if isinstance(payload.get("charts"), list) else [],
        "kpis": payload.get("kpis") if isinstance(payload.get("kpis"), list) else [],
        "visual": payload.get("visual") if isinstance(payload.get("visual"), dict) else None,
        "pinned_at": time.time(),
    }
    if not pin["charts"] and not pin["visual"]:
        raise ValueError("nothing to pin")
    with _lock:
        pins = _load()
        if pin["key"]:
            for existing in pins:
                if existing.get("key") == pin["key"]:
                    return existing
        if len(pins) >= MAX_PINS:
            raise ValueError(f"at most {MAX_PINS} pinned charts — unpin one first")
        pins.append(pin)
        _save(pins)
    return pin


def remove_pin(pid: str) -> bool:
    with _lock:
        pins = _load()
        keep = [p for p in pins if p.get("id") != pid]
        if len(keep) == len(pins):
            return False
        _save(keep)
        return True
