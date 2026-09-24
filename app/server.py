"""CIQ Data Intelligence - FastAPI backend serving the API and the web UI.

Run:  python -m uvicorn app.server:app --port 8000     then open http://localhost:8000
"""
from __future__ import annotations

import json
import uuid

import pandas as pd

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import settings, store
from app.agent import Agent, add_panel_result, rewind_last_exchange
from ciq.tools import command_center as cc
from ciq.tools import compare
from ciq import methodology
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


class CompareRequest(BaseModel):
    kind: str = Field(pattern="^(brands|hcp|time|patient|multi)$")
    a: str = ""
    b: str = ""
    products: list[str] = []          # kind "multi": 3-4 brands side by side
    product: str | None = None
    conversation_id: str | None = None


class CohortRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    hcp_ids: list[str] = Field(min_length=1, max_length=20000)
    definition: str = ""
    product: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=4000)
    conversation_id: str | None = None
    product: str | None = None
    retry: bool = False   # regenerate the reply to the conversation's last message (message is ignored)


# ---------------------------------------------------------------------------
@app.get("/api/meta")
async def meta():
    products = await run_in_threadpool(
        lambda: query("select product, molecules, drug_class, patients from fn_product_catalog(p_therapy_group := 'LUNG_SYSTEMIC')"))
    return {"coverage": await run_in_threadpool(coverage), "model": settings.MODEL_LABEL,
            "user": {"name": settings.USER_NAME, "email": settings.USER_EMAIL, "role": settings.USER_ROLE},
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


@app.get("/api/command-center")
async def command_center(product: str | None = None, window: int = 3):
    try:
        return await run_in_threadpool(cc.build, product, max(1, min(window, 3)))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/compare/options")
async def compare_options(product: str | None = None):
    def build():
        p = resolve_product(product)
        cov = coverage()
        months = pd.period_range(cov["data_start"], cov["data_end"], freq="M")
        return {"product": p, "context": {"market": ciq_config.INDICATION_NAME, "start": cov["data_start"],
                                          "end": cov["data_end"], "patients": cov["n_patients"]},
                "hcp": compare.hcp_cohort_options(p),
                "patient": [{"id": k, "label": v[0]} for k, v in compare.PATIENT_COHORTS.items()],
                "months": [{"id": str(m), "label": m.strftime("%b %Y")} for m in months],
                "geography": {"available": False, "reason": "Needs a geography source (HCP address or patient ZIP3). "
                              "The IQVIA extract has ZIP codes on pharmacy claims only (about 10% of patients)."}}
    try:
        return await run_in_threadpool(build)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/compare")
async def run_compare(req: CompareRequest):
    """Run a comparison from the Compare panel and record it in the conversation, so follow-up questions
    ("why are they different?") have the full result in context."""
    conv = (store.load(req.conversation_id) if req.conversation_id else None) or store.new()

    def build():
        if req.kind == "multi":
            res = run_tool("compare_products", {"products": req.products})
            label = " vs ".join(res.params["products"])
        else:
            res = compare.compare_segments(req.kind, req.a, req.b, req.product)
            label = res.title
        return res, label
    try:
        res, label = await run_in_threadpool(build)
    except (ValueError, KeyError) as e:
        raise HTTPException(400, str(e))
    result = await run_in_threadpool(res.to_dict, 500)
    call_id = f"compare_{uuid.uuid4().hex[:10]}"
    result["call_id"] = call_id
    focus = res.params.get("product") or res.params.get("a") or req.product
    text = f"Compare {label}" + (f" (brand {focus})" if req.kind in ("hcp", "time", "patient") else "")
    add_panel_result(conv, text, focus, result, res.to_llm_text(), "compare_segments" if req.kind != "multi"
                     else "compare_products", req.model_dump(exclude={"conversation_id"}))
    await run_in_threadpool(store.save, conv)
    return json_safe({"conversation": conv.summary(), "text": text, "result": result})


@app.get("/api/cohorts")
async def cohorts():
    return [{k: v for k, v in c.items() if k != "hcp_ids"} | {"size": len(c["hcp_ids"])} for c in compare.list_cohorts()]


@app.post("/api/cohorts")
async def create_cohort(req: CohortRequest):
    try:
        c = await run_in_threadpool(compare.save_cohort, req.name, req.hcp_ids, req.definition, req.product)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {k: v for k, v in c.items() if k != "hcp_ids"} | {"size": len(c["hcp_ids"])}


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
    for turn in conv.transcript:   # results saved before the (i) panel existed get their methodology now
        for r in turn.get("results", []):
            r.setdefault("method", methodology.describe(r.get("tool", ""), r.get("params", {}), r.get("meta", {})))
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
