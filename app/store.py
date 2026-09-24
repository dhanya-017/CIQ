"""File-backed conversation store.

Each conversation keeps two parallel records:
  api_messages  - the exact Messages-API history (content blocks incl. thinking) replayed to the model
  transcript    - UI-ready turns (user text, steps, tool results with charts, answer) for re-rendering
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from app.settings import CONVERSATION_DIR

_lock = threading.Lock()


@dataclass
class Conversation:
    id: str
    title: str = "New analysis"
    product: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    api_messages: list = field(default_factory=list)
    transcript: list = field(default_factory=list)

    def summary(self) -> dict:
        return {"id": self.id, "title": self.title, "product": self.product, "updated_at": self.updated_at,
                "turns": sum(1 for t in self.transcript if t.get("role") == "user")}


def _path(cid: str):
    if not all(c.isalnum() or c == "-" for c in cid):
        raise ValueError("bad conversation id")
    return CONVERSATION_DIR / f"{cid}.json"


def new() -> Conversation:
    return Conversation(id=uuid.uuid4().hex[:12])


def load(cid: str) -> Conversation | None:
    p = _path(cid)
    if not p.exists():
        return None
    return Conversation(**json.loads(p.read_text(encoding="utf-8")))


def save(conv: Conversation) -> None:
    conv.updated_at = time.time()
    CONVERSATION_DIR.mkdir(parents=True, exist_ok=True)
    with _lock:
        tmp = _path(conv.id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(conv), default=str), encoding="utf-8")
        tmp.replace(_path(conv.id))


def list_all(limit: int = 50) -> list[dict]:
    if not CONVERSATION_DIR.exists():
        return []
    convs = []
    for p in CONVERSATION_DIR.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            convs.append(Conversation(**d).summary())
        except Exception:
            continue
    return sorted(convs, key=lambda c: c["updated_at"], reverse=True)[:limit]


def delete(cid: str) -> None:
    p = _path(cid)
    if p.exists():
        p.unlink()
