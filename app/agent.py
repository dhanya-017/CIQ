"""Agent loop: Claude on Amazon Bedrock + CIQ analytics tools, streamed as UI events.

Events yielded (dicts, sent to the browser as SSE):
  {"type": "status", "text"}                      - progress line
  {"type": "thinking", "text"}                    - summarized reasoning delta
  {"type": "text", "text"}                        - answer text delta
  {"type": "tool_start", "id", "name", "label"}   - a tool call began
  {"type": "tool_result", "id", "name", "ok", "result" | "error"}
  {"type": "done"} / {"type": "error", "message"}
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import anthropic
from anthropic import AsyncAnthropicBedrock
from starlette.concurrency import run_in_threadpool

from app import settings, store
from ciq import config as ciq_config
from ciq.tools.base import coverage
from ciq.tools.registry import TOOLS, run_tool

TOOL_LABELS = {
    "data_overview": "Checking data coverage", "list_products": "Looking up products",
    "market_incidence_prevalence": "Measuring incidence & prevalence", "market_share": "Computing market share",
    "product_trend": "Tracking brand uptake", "patient_demographics": "Profiling patient demographics",
    "pre_index_treatments": "Reviewing prior treatments", "source_of_business": "Analysing source of business",
    "payer_mix": "Analysing payer mix", "combination_therapy": "Analysing combination regimens",
    "line_progression": "Tracing line progression", "product_share_by_line": "Computing share by line",
    "treatments_by_line": "Ranking treatments by line", "dx_around_index": "Profiling diagnoses",
    "persistency": "Estimating persistency", "hcp_market_concentration": "Decile-ing treating HCPs",
    "hcp_product_share_deciles": "Decile-ing HCPs by brand share", "run_sql": "Running a custom query",
    "compare_products": "Comparing brands side by side", "customize_chart": "Customising the chart",
    "ask_user": "Asking you a question",
}


def _system_prompt() -> str:
    cov = coverage()
    return f"""You are CIQ, the brand and patient analytics assistant inside Chryselys' data intelligence platform.
You answer commercial pharma questions for brand, insights and analytics teams by calling analytics tools over
claims data, then explaining what the numbers mean for the brand.

## Data
- Source: medical (MX) and pharmacy (RX) claims, {ciq_config.INDICATION_NAME}, {cov['n_patients']:,} patients,
  {cov['data_start']} to {cov['data_end']} ({cov['months']} months). No gender field, no payer names.
- Lines of therapy, sources of business and persistency are derived from claims with the business rules the
  tools describe in their caveats.

## How to work
- Always ground numbers in tool results. Never invent figures. If no tool fits, use run_sql (DuckDB, read-only).
- A user question may need several tools; call independent tools in parallel.
- If the user doesn't name a brand, use the focus brand given in the message context.
- For any comparison between brands ("compare", "vs", "against competitors"), call compare_products once with all
  the brands. That renders an interactive side-by-side panel. If the user asks to compare with "competitors" without
  naming them, pick the 2-3 largest products in the same drug class as the focus brand (use list_products if needed).
- Each tool result is shown to the user automatically as chart cards with the data table, so do not repeat
  every number or rebuild tables. Write the analyst's read-out: the answer first, then 2-5 key insights
  with the numbers that matter, then implications or suggested next questions when useful.
- State material data limitations briefly (e.g. the short data window truncating a 24-month look-back),
  once, in plain language. Don't bury the answer in caveats.
- If the user asks for a chart the standard tools don't draw (another chart type, breakdown or metric), use run_sql
  with `chart`. If the chart call fails, read the reason, fix the SQL or chart spec and retry. When it succeeds, check
  the "chart shown to the user" summary against the question and redraw if it doesn't answer it.
- When the user asks to change a chart already shown (highlight, colours, a range such as "deciles 1-5" or
  "Mar to May", a target line, bar vs line, sorting, labels), call customize_chart on it instead of re-running the
  analysis; put all requested edits in one call. Respect exact colours and ranges the user names. After a chart edit,
  reply in one or two sentences unless the user asked a question.
- Ask before analysing when the request has two or more materially different readings that lead to different
  analyses, e.g. "segment" (age? payer? line of therapy? prescriber?), "performance" (share? uptake? persistency?),
  "competitors" when several drug classes fit, "the trend" without a metric, or an edit that could target several
  charts. Call ask_user alone, with 2-4 concrete options per question (recommended first) and every open choice
  in one call. Don't ask about minor settings with an obvious default (time grain, top N, decile method): use the
  default and state it in one line. Never ask about something the user already specified, and don't ask twice in a
  row. When the user answers, carry out the original request with their choice.
- Never mention internal tool or function names to the user; suggest next steps as questions they can ask.
- Use markdown: short paragraphs, bold key figures, bullet lists; at most H3 headings, in sentence case
  (e.g. "KEYTRUDA share by HCP decile", not "KEYTRUDA HCP Market-Share Deciles"). Be concise."""


def _parse_ask(args) -> tuple[dict | None, str | None]:
    """Validate an ask_user call into {questions: [...]} for the UI, or return an error for the model."""
    qs = args.get("questions") if isinstance(args, dict) else None
    if not isinstance(qs, list) or not 1 <= len(qs) <= 3:
        return None, "questions must be a list of 1-3 questions"
    out = []
    for i, q in enumerate(qs):
        opts = q.get("options") if isinstance(q, dict) else None
        if not isinstance(q, dict) or not str(q.get("question", "")).strip():
            return None, f"questions[{i}] needs a `question`"
        if not isinstance(opts, list) or not 2 <= len(opts) <= 4 or not all(
                isinstance(o, dict) and str(o.get("label", "")).strip() for o in opts):
            return None, f"questions[{i}] needs 2-4 options, each with a `label`"
        out.append({"question": q["question"].strip(), "header": str(q.get("header") or "").strip()[:24],
                    "multi_select": bool(q.get("multi_select")),
                    "options": [{"label": o["label"].strip()[:80], "description": str(o.get("description") or "").strip()}
                                for o in opts]})
    return {"questions": out}, None


def _check_input(tool, args) -> str | None:
    """Validate model-supplied tool input against the tool's schema (types, enums)."""
    if not isinstance(args, dict):
        return "tool input must be an object"
    for k, v in args.items():
        spec = tool.properties.get(k)
        if spec is None or v is None:
            continue
        t = spec.get("type")
        if t == "integer" and not (isinstance(v, int) and not isinstance(v, bool)):
            return f"{k} must be an integer"
        if t == "string" and not isinstance(v, str):
            return f"{k} must be a string"
        if t == "boolean" and not isinstance(v, bool):
            return f"{k} must be a boolean"
        if t == "object" and not isinstance(v, dict):
            return f"{k} must be an object"
        if t == "array" and not isinstance(v, list):
            return f"{k} must be a list"
        if t == "array" and spec.get("items", {}).get("type") == "string" and not all(isinstance(x, str) for x in v):
            return f"{k} must be a list of strings"
        if t == "array" and spec.get("items", {}).get("type") == "object" and not all(isinstance(x, dict) for x in v):
            return f"{k} must be a list of objects"
        if "enum" in spec and v not in spec["enum"]:
            return f"{k} must be one of {spec['enum']}"
    missing = [r for r in tool.required if r not in args]
    return f"missing required: {missing}" if missing else None


_TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def _tool_defs() -> list[dict]:
    defs = [{**t.schema(), "eager_input_streaming": True} for t in TOOLS]
    defs[-1]["cache_control"] = {"type": "ephemeral"}   # cache the stable tools + system prefix
    return defs


class Agent:
    def __init__(self) -> None:
        kw = {"aws_region": settings.AWS_REGION}
        if settings.AWS_PROFILE:
            kw["aws_profile"] = settings.AWS_PROFILE
        self.client = AsyncAnthropicBedrock(**kw)
        self.system = [{"type": "text", "text": _system_prompt(), "cache_control": {"type": "ephemeral"}}]
        self.tools = _tool_defs()

    async def _execute(self, block, history: list[dict]) -> tuple[dict, dict]:
        """Run one tool call. Returns (tool_result block for the API, UI event)."""
        tool = _TOOLS_BY_NAME.get(block.name)
        err = f"unknown tool {block.name}" if tool is None else _check_input(tool, block.input)
        if err is None:
            try:
                res = await run_in_threadpool(run_tool, block.name, block.input, history)
                ui = {"type": "tool_result", "id": block.id, "name": block.name, "ok": True,
                      "result": await run_in_threadpool(res.to_dict, 500)}
                return {"type": "tool_result", "tool_use_id": block.id, "content": res.to_llm_text()}, ui
            except Exception as e:  # tool-level failure: report back so the model can recover
                err = f"{type(e).__name__}: {e}"
        return ({"type": "tool_result", "tool_use_id": block.id, "is_error": True, "content": err},
                {"type": "tool_result", "id": block.id, "name": block.name, "ok": False, "error": err})

    async def run(self, conv: store.Conversation, user_text: str, product: str | None) -> AsyncIterator[dict]:
        focus = product or conv.product or ciq_config.DEFAULT_PRODUCT
        conv.product = focus
        if conv.title == "New analysis":
            conv.title = user_text.strip().replace("\n", " ")[:80]
        if conv.pending:   # this message answers the clarifying question the last turn ended on
            pend, conv.pending = conv.pending, None
            conv.api_messages.append({"role": "user", "content": pend["results"] + [
                {"type": "tool_result", "tool_use_id": pend["ask_id"],
                 "content": f"The user answered (focus brand in the UI: {focus}):\n{user_text}"}]})
            conv.transcript.append({"role": "user", "text": user_text, "product": focus, "answer_to": pend["ask_id"]})
        else:
            conv.api_messages.append({"role": "user", "content": [
                {"type": "text", "text": f"<context>Focus brand selected in the UI: {focus}</context>\n\n{user_text}"}]})
            conv.transcript.append({"role": "user", "text": user_text, "product": focus})
        turn = {"role": "assistant", "steps": [], "results": [], "text": "", "thinking": ""}
        conv.transcript.append(turn)

        try:
            for _step in range(settings.MAX_AGENT_STEPS):
                json_retries = 0
                while True:
                    try:
                        async with self.client.messages.stream(
                            model=settings.MODEL_ID, max_tokens=settings.MAX_TOKENS,
                            system=self.system, tools=self.tools, messages=conv.api_messages,
                            thinking={"type": "adaptive", "display": "summarized"},
                            output_config={"effort": settings.EFFORT},
                        ) as stream:
                            async for ev in stream:
                                if ev.type == "content_block_start" and ev.content_block.type == "tool_use" \
                                        and ev.content_block.name != "ask_user":   # a question is not an analysis step
                                    name = ev.content_block.name
                                    yield {"type": "tool_start", "id": ev.content_block.id, "name": name,
                                           "label": TOOL_LABELS.get(name, name)}
                                elif ev.type == "content_block_delta":
                                    if ev.delta.type == "text_delta":
                                        turn["text"] += ev.delta.text
                                        yield {"type": "text", "text": ev.delta.text}
                                    elif ev.delta.type == "thinking_delta" and ev.delta.thinking:
                                        turn["thinking"] += ev.delta.thinking
                                        yield {"type": "thinking", "text": ev.delta.thinking}
                            msg = await stream.get_final_message()
                        break
                    except ValueError:   # unparseable streamed tool JSON: re-issue the turn (bounded)
                        json_retries += 1
                        if json_retries > 2:
                            raise
                        yield {"type": "status", "text": "Retrying a malformed tool call..."}

                conv.api_messages.append({"role": "assistant",
                                          "content": [b.model_dump(mode="json", exclude_none=True) for b in msg.content]})
                if msg.stop_reason == "pause_turn":
                    continue
                tool_uses = [b for b in msg.content if b.type == "tool_use"]
                if msg.stop_reason == "refusal":
                    yield {"type": "error", "message": "The model declined this request."}
                    break
                if not tool_uses:
                    break
                if msg.stop_reason == "max_tokens":
                    yield {"type": "error", "message": "Response was cut off (max tokens). Try a narrower question."}
                    break
                # a valid ask_user pauses the turn; an invalid one goes back to the model as an error
                asks, ask_errors, ask = [b for b in tool_uses if b.name == "ask_user"], [], None
                for b in asks:
                    parsed, err = _parse_ask(b.input) if ask is None else (None, "only one ask_user per turn")
                    if parsed:
                        ask = (b, parsed)
                    else:
                        ask_errors.append({"type": "tool_result", "tool_use_id": b.id, "is_error": True, "content": err})
                runnable = [b for b in tool_uses if b.name != "ask_user"]
                for b in runnable:
                    turn["steps"].append({"id": b.id, "name": b.name, "label": TOOL_LABELS.get(b.name, b.name),
                                          "input": b.input})
                history = [r for t in conv.transcript if t.get("role") == "assistant" for r in t.get("results", [])]
                outcomes = await asyncio.gather(*(self._execute(b, history) for b in runnable))
                for api_block, ui_event in outcomes:
                    for s in turn["steps"]:
                        if s["id"] == ui_event["id"]:
                            s["ok"] = ui_event["ok"]
                            s["error"] = ui_event.get("error")
                    if ui_event["ok"]:
                        turn["results"].append(ui_event["result"] | {"call_id": ui_event["id"]})
                    yield ui_event
                if ask:
                    block, parsed = ask
                    conv.pending = {"ask_id": block.id, "ask": parsed,
                                    "results": [o[0] for o in outcomes] + ask_errors}
                    turn["ask"] = {"id": block.id, **parsed}
                    yield {"type": "ask", "id": block.id, **parsed}
                    break   # the turn ends here; the user's next message is the answer
                conv.api_messages.append({"role": "user", "content": [o[0] for o in outcomes] + ask_errors})
                if turn["text"]:
                    turn["text"] += "\n\n"
                    yield {"type": "text", "text": "\n\n"}
            else:
                yield {"type": "error", "message": "Stopped after the maximum number of analysis steps."}
        except anthropic.APIConnectionError:
            yield {"type": "error", "message": "Could not reach Amazon Bedrock. Check network / AWS credentials."}
        except anthropic.AuthenticationError:
            yield {"type": "error", "message": "AWS credentials were rejected by Bedrock."}
        except anthropic.PermissionDeniedError as e:
            yield {"type": "error", "message": f"Bedrock permission denied: {e.message}"}
        except anthropic.RateLimitError:
            yield {"type": "error", "message": "Bedrock rate limit reached - please retry in a moment."}
        except anthropic.APIStatusError as e:
            yield {"type": "error", "message": f"Bedrock error {e.status_code}: {e.message}"}
        finally:
            if not conv.pending:   # a turn paused on a question is complete as it is
                _repair_history(conv)
            store.save(conv)
        yield {"type": "done", "conversation": conv.summary()}


def _repair_history(conv: store.Conversation) -> None:
    """Keep api_messages valid after an interrupted turn: every tool_use must be answered."""
    msgs = conv.api_messages
    if msgs and msgs[-1]["role"] == "assistant":
        pending = [b["id"] for b in msgs[-1]["content"] if b.get("type") == "tool_use"]
        if pending:
            msgs.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": i, "is_error": True, "content": "interrupted"} for i in pending]})
    elif msgs and msgs[-1]["role"] == "user" and all(b.get("type") == "text" for b in msgs[-1]["content"]):
        # the question never got a reply (e.g. connection error): drop it so history stays well-formed
        msgs.pop()
