"""LoTrace Data Intelligence - FastAPI backend serving the API and the web UI.

Run:  python -m uvicorn app.server:app --port 8000     then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pandas as pd

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import pins, settings, store
from app.agent import Agent, add_panel_result, rewind_last_exchange
from ciq.tools import command_center as cc
from ciq.tools import compare
from ciq.tools import cohort_builder as cbuild
from ciq import methodology
from ciq import config as ciq_config
from ciq.tools.base import coverage, json_safe, query, resolve_product, sql_literal
from ciq.tools.registry import TOOLS, run_tool

app = FastAPI(title="LoTrace Data Intelligence")
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
    hcp_ids: list[str] = Field(default_factory=list, max_length=20000)
    patient_ids: list[str] = Field(default_factory=list, max_length=100000)
    definition: str = ""
    product: str | None = None


class CohortFromSpecRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: str
    spec: str
    product: str | None = None


class CohortAskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    study_period: dict | None = None


class CohortTreeRequest(BaseModel):
    tree: dict | None = None
    study_period: dict | None = None
    name: str | None = None
    product: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=4000)
    conversation_id: str | None = None
    product: str | None = None
    retry: bool = False   # regenerate the reply to the conversation's last message (message is ignored)


class PlaygroundLotRequest(BaseModel):
    cycle_window_days: int | None = None
    line_gap_days: int | None = None
    min_cycles: int | None = None
    min_days: int | None = None


class PinRequest(BaseModel):
    key: str = Field(default="", max_length=240)
    title: str = Field(default="Pinned chart", max_length=200)
    subtitle: str = Field(default="", max_length=400)
    tool: str = Field(default="", max_length=80)
    product: str | None = None
    conversation_id: str | None = None
    charts: list = Field(default_factory=list)
    kpis: list = Field(default_factory=list)
    visual: dict | None = None


# ---------------------------------------------------------------------------
@app.get("/api/meta")
async def meta():
    def catalog():
        wanted = list(ciq_config.UI_BRANDS)
        listed = ", ".join(sql_literal(p) for p in wanted)
        df = query(f"""
            select product_form as product,
                   string_agg(distinct molecule, ', ') as molecules,
                   any_value(drug_class) as drug_class,
                   count(distinct patient_id) as patients
            from fact_drug_event
            where product_form in ({listed})
            group by product_form""")
        return df.set_index("product").reindex(wanted).dropna(how="all").reset_index()
    products = await run_in_threadpool(catalog)
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
                   (select count(distinct hcp_id) from fact_drug_event where product = ? or product_form = ?) product_hcps
        """, [p, p, p]).iloc[0]
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
async def command_center(product: str | None = None, window: int = 1, cohort: str | None = None):
    try:
        return await run_in_threadpool(cc.build, product, cc.normalize_window(window), cohort)
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
                "patient": compare.patient_cohort_options(),
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
    return [{k: v for k, v in c.items() if k not in ("hcp_ids", "patient_ids")} | {"size": compare.cohort_size(c)}
            for c in compare.list_cohorts()]


@app.post("/api/cohorts")
async def create_cohort(req: CohortRequest):
    try:
        c = await run_in_threadpool(compare.save_cohort, req.name, req.hcp_ids, req.definition, req.product,
                                    req.patient_ids)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {k: v for k, v in c.items() if k not in ("hcp_ids", "patient_ids")} | {"size": compare.cohort_size(c)}


@app.get("/api/cohorts/options")
async def cohort_options(kind: str = "patient", product: str | None = None):
    p = resolve_product(product)
    if kind == "hcp":
        opts = await run_in_threadpool(compare.hcp_cohort_options, p)
    else:
        opts = compare.patient_cohort_options()
    opts = [o for o in opts if not o.get("saved")]
    saved = [{k: v for k, v in c.items() if k not in ("hcp_ids", "patient_ids")} | {"size": compare.cohort_size(c)}
             for c in compare.list_cohorts()]
    return json_safe({"kind": kind, "product": p, "options": opts, "saved": saved})


@app.get("/api/cohorts/preview")
async def cohort_preview(kind: str = "patient", spec: str = "all", product: str | None = None):
    try:
        return json_safe(await run_in_threadpool(compare.preview_cohort, kind, spec, product))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/cohorts/from-spec")
async def create_cohort_from_spec(req: CohortFromSpecRequest):
    try:
        c = await run_in_threadpool(compare.save_cohort_from_spec, req.name, req.kind, req.spec, req.product)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {k: v for k, v in c.items() if k not in ("hcp_ids", "patient_ids")} | {"size": compare.cohort_size(c)}


@app.get("/api/codes/suggest")
async def codes_suggest(system: str = "ICD", q: str = "", limit: int = 20):
    try:
        return json_safe(await run_in_threadpool(cbuild.suggest_codes, system, q, limit))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/codes/search")
async def codes_search(q: str = "", system: str | None = None, limit: int = 25):
    hits = await run_in_threadpool(cbuild.search_codes, q, system, limit)
    return json_safe(hits[:limit])


@app.post("/api/cohorts/ask")
async def cohort_ask(req: CohortAskRequest):
    q = req.question.strip()
    tree = cbuild.parse_question(q)
    if tree is None:
        tree = cbuild.keep_warehouse_tree(await _bedrock_cohort_tree(q))
    if tree is None:
        return {"question": q, "size": 0, "empty": True, "tree": None, "label": None,
                "answer": "I could not match that to ICD, CPT or NDC codes in this extract. Name a code, then AND, OR, or followed by."}
    pack = json_safe(await run_in_threadpool(cbuild.json_tree, tree, req.study_period))
    pack["question"] = q
    pack["answer"] = cbuild.ask_answer(pack)
    return pack


@app.post("/api/cohorts/query")
async def cohort_query(req: CohortTreeRequest):
    try:
        return json_safe(await run_in_threadpool(cbuild.json_tree, req.tree, req.study_period, False))
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/cohorts/lot")
async def cohort_lot(req: CohortTreeRequest):
    try:
        return json_safe(await run_in_threadpool(cbuild.lot_for_tree, req.tree, req.study_period))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/playground/defaults")
async def playground_defaults():
    return json_safe(cc.playground_defaults())


@app.post("/api/playground/lot")
async def playground_lot(req: PlaygroundLotRequest | None = None):
    raw = req.model_dump() if req else {}
    try:
        return json_safe(await run_in_threadpool(cc.playground_lot, raw))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/cohorts/from-tree")
async def create_cohort_from_tree(req: CohortTreeRequest):
    if not (req.name or "").strip():
        raise HTTPException(400, "name is required")
    try:
        def save():
            ids = cbuild.patient_ids_for_tree(req.tree, study_period=req.study_period)
            label = cbuild.summarize_tree(req.tree, req.study_period)
            return compare.save_cohort(req.name, None, label[:300], req.product, ids)
        c = await run_in_threadpool(save)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {k: v for k, v in c.items() if k not in ("hcp_ids", "patient_ids")} | {"size": compare.cohort_size(c)}


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


@app.get("/api/pins")
async def get_pins():
    return json_safe(pins.list_pins())


@app.post("/api/pins")
async def create_pin(req: PinRequest):
    try:
        pin = await run_in_threadpool(pins.add_pin, json_safe(req.model_dump()))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return json_safe(pin)


@app.delete("/api/pins/{pid}")
async def delete_pin(pid: str):
    if not await run_in_threadpool(pins.remove_pin, pid):
        raise HTTPException(404, "not found")
    return {"ok": True}


@app.post("/api/tools/{name}")
async def call_tool(name: str, args: dict | None = None):
    """Run a tool directly (no LLM) - used by the UI to re-run a card with new parameters."""
    try:
        res = await run_in_threadpool(run_tool, name, args or {})
    except (KeyError, ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    return await run_in_threadpool(res.to_dict)


async def _bedrock_cohort_tree(q: str) -> dict | None:
    """Optional English→criteria tree when the local parser cannot read the question."""
    try:
        from anthropic import AsyncAnthropicBedrock
        client = AsyncAnthropicBedrock(aws_region=settings.AWS_REGION)
        msg = await asyncio.wait_for(client.messages.create(
            model=settings.MODEL_ID, max_tokens=800,
            messages=[{"role": "user", "content":
                       "Convert this patient-cohort question into a JSON criteria tree for a claims extract. "
                       "ICD/CPT/NDC only; use only codes named in the question. Codes in one criterion are OR. "
                       "join on later children must be AND, OR, or SEQ (SEQUENTIAL EVENT). "
                       'Schema: {"kind":"group","children":[{"kind":"criterion","system":"ICD"|"CPT"|"NDC",'
                       '"codes":[{"code":"..."}],"join":"AND"}]}. An ICD stem matches that family (C78 matches C78 and subcodes). '
                       "NDC may be a brand present in the extract. JSON only, no markdown.\n\n" + q}],
        ), timeout=12)
        text = "".join(getattr(b, "text", "") or "" for b in msg.content)
        start, end = text.find("{"), text.rfind("}")
        raw = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
    except Exception:
        return None
    if not isinstance(raw, dict) or (raw.get("kind") or "group") != "group":
        return None
    return raw


# ---------------------------------------------------------------------------
app.mount("/static", StaticFiles(directory=settings.STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(settings.STATIC_DIR / "index.html")
