# CIQ developer guide

This guide takes you from cloning the repo to shipping a change. Skim the architecture section first; the rest is
reference for common tasks.

## 1. Setup (about 10 minutes)

**You need:**
- Python 3.10+ (3.12 recommended)
- Git
- AWS credentials with Bedrock access to Claude, i.e. `bedrock:InvokeModel*` on the `us.anthropic.claude-sonnet-5` inference profile, region `us-east-1`
- The claims master file: request access from the data owner (see [Data/README.md](../Data/README.md))

**Steps:**
1. Clone:
   ```bash
   git clone https://github.com/SatyamSaitama/ciq-platform.git
   cd ciq-platform
   ```
2. Copy `master_nsclc.csv` and `mx_nsclc.csv` into `Data/real_data/` (see `Data/README.md`).
3. Run the setup script:
   - Windows: `.\setup.ps1`
   - macOS/Linux: `./setup.sh`

   It creates `.venv`, installs packages, creates `.env`, builds the warehouse, runs the tests and runs `scripts/doctor.py`.
4. Start the app:
   - Windows: `.\run.ps1 -Reload`
   - macOS/Linux: `./run.sh --reload`

   Then open http://localhost:8000.

**If something fails,** run `python scripts/doctor.py`. It checks each prerequisite and prints the fix.
Use `--skip-bedrock` to skip the (tiny, paid) model call.

## 2. Architecture

```
Data/real_data/master_nsclc.csv (+ mx_nsclc.csv, ciq/reference/icd10cm.csv for enrichment)
        │  ciq/build/sources.py: clean, enrich, keep %C34% cohort -> stg_claims  (CIQ_SOURCE=claims_master: v1 parquet)
        │  python -m ciq.build.build_warehouse   (~2 min, rebuild when data or build logic changes)
        ▼
Data/warehouse/ciq.duckdb
  ref_drug, ref_drug_code_map     curated drug dictionary + code→molecule map
  fact_drug_event                 one row per patient/day/molecule (MX + paid RX)
  fact_dx_event, fact_px_event    diagnoses, procedures
  dim_patient, dim_hcp            conformed dimensions
  lot_line, lot_line_drug         lines of therapy (ciq/build/lot.py)
  fn_*                            stored functions = one per business question (ciq/sql/procedures.sql)
        │
        ▼
ciq/tools/analytics.py    Python tool per question: calls fn_*, adds Vega-Lite charts, insights, caveats → ToolResult
ciq/tools/registry.py     tool catalog: Claude tool schemas + run_tool() dispatcher
        │
        ▼
app/agent.py              agent loop: Claude on Bedrock ⇄ tools, streamed as events
app/server.py             FastAPI: /api/chat (SSE), /api/tools/{name}, /api/conversations, static UI
app/static/               no-build UI (index.html, app.js, styles.css) rendering events, cards, charts
```

### One chat request, end to end
1. The UI POSTs `/api/chat` with the question and the focus brand, then reads the Server-Sent Events stream.
2. `Agent.run` appends the question to the conversation and calls Claude, with streaming, summarized adaptive thinking and all tool schemas.
3. Claude requests tools. They run in parallel in a thread pool (`run_tool`), and each `ToolResult` is split two ways:
   - `to_dict()` → the UI, as a chart card
   - `to_llm_text()` → back to Claude, as compact text
4. The loop repeats until Claude answers without tools (max `CIQ_MAX_AGENT_STEPS`).
5. The conversation is saved to `Data/conversations/<id>.json`:
   - `api_messages`: the exact model history, including thinking blocks
   - `transcript`: what the UI re-renders

## 3. Repo layout

| Path | What lives there |
|---|---|
| `ciq/config.py` | Data paths, default product, indication, line-of-therapy business rules |
| `ciq/reference/drug_reference.csv` | Drug dictionary: molecule, product, class, therapy group, days supply, HCPCS codes, name regex |
| `ciq/build/` | Warehouse build, line-of-therapy algorithm, reinstall the stored functions only |
| `ciq/sql/procedures.sql` | All `fn_*` table macros |
| `ciq/tools/` | `base.py` (DB access, ToolResult), `charts.py` (Vega-Lite builders), `analytics.py` (tools), `registry.py` |
| `app/` | `settings.py` (env/.env), `agent.py`, `server.py`, `store.py`, `static/` |
| `scripts/` | `doctor.py` (environment check), `tool_gallery.py` (render every tool to `docs/tool_gallery.html`) |
| `tests/` | Tool smoke tests (run against the real warehouse) |

## 4. Common tasks

### Add or fix a drug
1. Edit `ciq/reference/drug_reference.csv`. Row order matters: the first matching regex wins, so put specific
   names before generic ones (e.g. nab-paclitaxel before paclitaxel).
2. `therapy_group`:
   - `LUNG_SYSTEMIC`: counts toward the market and lines of therapy
   - `OTHER_ONCOLOGY`: other-tumour anticancer drugs; count toward lines of therapy, not the market
   - `SUPPORTIVE`: G-CSF, bone agents and similar
3. Rebuild: `python -m ciq.build.build_warehouse`.
4. Check unmapped J-codes: `select * from ref_drug_code_map where match_type = 'J9_FALLBACK'`.

### Add a new analysis (tool)
1. **Stored function:** add `fn_<name>` to `ciq/sql/procedures.sql`, then run `python -m ciq.build.install_procedures`.
   - Every parameter except the first uses a default (`p_x := ...`).
   - DuckDB requires **named arguments** for defaulted params, including when one macro calls another.
2. **Tool:** add a function in `ciq/tools/analytics.py` that:
   - calls `call_fn("fn_<name>", product, x=...)` (Python kwargs become `p_x :=`)
   - builds charts with `ciq/tools/charts.py`
   - returns a `ToolResult` with insights and caveats
3. **Register it** in `ciq/tools/registry.py`. The description is what Claude reads when choosing tools: say
   plainly which questions it answers. Add a label in `TOOL_LABELS` (`app/agent.py`).
4. **Test it:** add a case to `tests/test_tools.py`, run `pytest`, then `python scripts/tool_gallery.py` to review the charts.

### Change business rules
- **Line-of-therapy window, gap and equivalent swaps:** `ciq/config.py`, then rebuild.
- **Source-of-business, persistency and decile defaults:** tool parameters. Change the defaults in `procedures.sql`
  and `analytics.py` together.

### Change the model or the agent's behaviour
- **Model, effort, step limits:** `.env` (see `.env.example`). No code change needed.
- **System prompt:** `_system_prompt()` in `app/agent.py`. Keep it stable: it's prompt-cached together with the tool list.
- **Bedrock:** we use `AnthropicBedrock` (the bedrock-runtime InvokeModel API). The newer Mantle endpoint is
  denied for our IAM users.

### UI
- Plain JS, with no build step. Edit `app/static/*` and hard-refresh.
- Colours are CSS tokens in `styles.css` (Chryselys navy `#004567`, gold `#C98B27`).
- Chart colours come from `charts.py` (validated categorical palette; brands pinned to slots with `domain=`).
- The comparison panel is `renderCompare()` in `app.js`. It re-runs `/api/tools/compare_products` directly, without the LLM.

## 5. Testing and debugging

- **Tests:** `python -m pytest tests -q`. Every tool runs against the real warehouse and must serialize to JSON.
- **Try a tool without the LLM:**
  `python -c "from ciq.tools import run_tool; print(run_tool('payer_mix', {'product': 'OPDIVO'}).to_llm_text())"`
- **Ad-hoc SQL:** use `duckdb Data/warehouse/ciq.duckdb`, or the `run_sql` tool.
- **"Could not set lock on file ... ciq.duckdb" when rebuilding:** the running app holds the warehouse open.
  Stop it, rebuild, then restart.
- **Bedrock errors:**
  - 403 = missing model permission, which your AWS admin grants
  - 429 = rate limit
  - The UI shows the message, and `doctor.py` reproduces the call.
- **Model gives odd numbers:** open the card's Data tab (exactly what the model saw) and the Notes tab (method).
  The answer should only restate those.

## 6. Rules

- **Never commit data.** Nothing from `Data/` except `.gitkeep` and the README, no conversation files, no
  `docs/tool_gallery.html` (it embeds aggregates). `.gitignore` enforces this. Don't force-add.
- **No credentials in code.** Use AWS profiles or SSO and `.env`, which is git-ignored.
- **Numbers come from tools only.** The agent interprets; the maths stays in SQL and Python where it can be tested.
- **Every tool states its caveats** (data window, definitions), so the agent can pass them on.
