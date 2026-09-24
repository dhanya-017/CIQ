"""CIQ Data Intelligence - FastAPI backend serving the API and the web UI.

Run:  python -m uvicorn app.server:app --port 8000     then open http://localhost:8000
"""
from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import settings, store
from app.agent import Agent, rewind_last_exchange
from ciq import config as ciq_config
from ciq.tools.base import coverage, json_safe, query, resolve_product
from ciq.tools.registry import TOOLS, run_tool

app = FastAPI(title="CIQ Data Intelligence")
_agent: Agent | None = None


def agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


SUGGESTIONS = [
    ("Market overview", [
        "What is the incidence and prevalence of the market and has it changed recently?",
        "Analyze the demographics (age, payment type) of the {p} patient cohort",
        "How has {p} uptake and share of the treated market trended?",
    ]),
    ("Patient & treatment", [
        "What treatments (drug class) do patients receive in the 24 months before their first {p} administration?",
        "Analyze the source of business for {p}",
        "Analyze the payer mix of {p} and identify the top 10 payers",
    ]),
    ("Therapy & lines", [
        "Are there combination therapies that {p} patients undergo?",
        "What percentage of patients progress to later lines (3+)?",
        "What is the {p} patient share in each line, and the common treatments in lines 1, 2, 3, 4+?",
    ]),
    ("Diagnosis & adherence", [
        "What are the most common Dx codes in {p} patients 18 months before and after the first administration?",
        "Analyze the treatment persistency of {p}",
    ]),
    ("Compare brands", [
        "Compare {p} with its main competitors side by side",
        "How do {p} persistency and line share compare with other products in its drug class?",
    ]),
    ("Physician", [
        "Decile the treating physicians: is the market concentrated or scattered 24 months before and after first {p}?",
        "Decile the HCPs based on {p} market share",
    ]),
]


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=4000)
    conversation_id: str | None = None
    product: str | None = None
    retry: bool = False   # regenerate the reply to the conversation's last message (message is ignored)


# ---------------------------------------------------------------------------
@app.get("/api/meta")
async def meta():
    products = await run_in_threadpool(
        lambda: query("select product, drug_class, patients from fn_product_catalog(p_therapy_group := 'LUNG_SYSTEMIC')"))
    return {"coverage": await run_in_threadpool(coverage), "model": settings.MODEL_LABEL,
            "indication": ciq_config.INDICATION_NAME, "default_product": ciq_config.DEFAULT_PRODUCT,
            "products": products.to_dict(orient="records"),
            "suggestions": [{"group": g, "questions": q} for g, q in SUGGESTIONS],
            "tools": [{"name": t.name, "description": t.description} for t in TOOLS]}


@app.get("/api/overview")
async def overview(product: str | None = None):
    def build():
        p = resolve_product(product)
        k = query("""
            select (select count(*) from dim_patient) patients,
                   (select count(*) from dim_patient where is_treated) treated,
                   (select count(*) from fn_product_index(?)) product_patients,
                   (select count(distinct hcp_id) from fact_drug_event where product = ?) product_hcps
        """, [p, p]).iloc[0]
        trend = run_tool("product_trend", {"product": p}).to_dict()
        last = trend["data"][-1] if trend["data"] else {}
        return {"product": p, "kpis": [
            {"label": "Lung cancer patients", "value": int(k.patients)},
            {"label": "Treated with systemic therapy", "value": int(k.treated)},
            {"label": f"{p} patients", "value": int(k.product_patients)},
            {"label": f"{p} share of treated ({last.get('period', '')})",
             "value": last.get("patient_share"), "format": "pct"},
            {"label": f"HCPs using {p}", "value": int(k.product_hcps)},
        ], "trend_chart": trend["charts"][0] if trend["charts"] else None}
    try:
        return await run_in_threadpool(build)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    conv = (store.load(req.conversation_id) if req.conversation_id else None) or store.new()
    message = req.message.strip()
    if req.retry:
        if not req.conversation_id or conv.id != req.conversation_id:
            raise HTTPException(404, "conversation not found")
        try:
            message, last_product = rewind_last_exchange(conv)
        except ValueError as e:
            raise HTTPException(409, str(e))
        req.product = req.product or last_product
    elif not message:
        raise HTTPException(422, "message is empty")
    product = None
    if req.product:
        try:
            product = await run_in_threadpool(resolve_product, req.product)
        except ValueError as e:
            raise HTTPException(400, str(e))

    async def events():
        yield _sse({"type": "conversation", "id": conv.id})
        async for ev in agent().run(conv, message, product):
            yield _sse(ev)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(json_safe(ev), default=str)}\n\n"


@app.get("/api/conversations")
async def conversations():
    return store.list_all()


@app.get("/api/conversations/{cid}")
async def conversation(cid: str):
    conv = store.load(cid)
    if conv is None:
        raise HTTPException(404, "not found")
    return json_safe({**conv.summary(), "transcript": conv.transcript,
                      "pending": {"id": conv.pending["ask_id"], **conv.pending["ask"]} if conv.pending else None})


@app.delete("/api/conversations/{cid}")
async def delete_conversation(cid: str):
    store.delete(cid)
    return {"ok": True}


@app.post("/api/tools/{name}")
async def call_tool(name: str, args: dict | None = None):
    """Run a tool directly (no LLM) - used by the UI to re-run a card with new parameters."""
    try:
        res = await run_in_threadpool(run_tool, name, args or {})
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return await run_in_threadpool(res.to_dict)


# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(settings.STATIC_DIR / "index.html")
