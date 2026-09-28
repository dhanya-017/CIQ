# CIQ architecture

How CIQ is built, from the claims file to the answer on screen. For definitions of the numbers see
[BUSINESS_RULES.md](BUSINESS_RULES.md); for column-level detail see [DATA_DICTIONARY.md](DATA_DICTIONARY.md); for
how-to tasks see [DEVELOPER.md](DEVELOPER.md).

## 1. Design principles

1. **The maths lives in tested code, not in the AI.** Every figure comes from a stored function (SQL) or a Python
   tool. The model only chooses analyses, sets their parameters and interprets the results.
2. **One analysis = one stored function = one tool.** The same function answers the question in SQL, in the API
   and in the chat, so results agree everywhere.
3. **Every result carries its method and caveats,** so the agent and the user see the same limitations.
4. **Business rules are applied once, at build time.** Lines of therapy, the cohort and SOB are materialised in the
   warehouse, not recomputed per question.
5. **Simple to run.** One Python process, one database file, no build step for the UI.

## 2. Tech stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 (3.10+) |
| Warehouse | DuckDB: a single file, `Data/warehouse/ciq.duckdb` (~200 MB); SQL table macros as stored functions |
| Data processing | pandas / numpy (line-of-therapy algorithm, Kaplan-Meier, tool post-processing) |
| AI model | Claude Sonnet 5 on Amazon Bedrock (`us.anthropic.claude-sonnet-5`) via the Anthropic Python SDK (`AnthropicBedrock`, bedrock-runtime API) |
| Backend | FastAPI + uvicorn; Server-Sent Events for streaming |
| Frontend | Plain HTML / CSS / JavaScript (no framework, no build step); Vega-Lite (vega-embed) for charts; marked + DOMPurify for the markdown read-out |
| Tests | pytest (runs against the real warehouse) |
| Source control | GitHub, private repo `SatyamSaitama/ciq-platform` |

## 3. System overview

```
 Data/real_data/master_nsclc.csv ─┐
 Data/real_data/mx_nsclc.csv ─────┤  enrichment: age, billing provider
 ciq/reference/icd10cm.csv ───────┤  diagnosis descriptions
 ciq/reference/drug_reference.csv┘  drug dictionary
            │
            │  BUILD  python -m ciq.build.build_warehouse   (~2 min)
            │    sources.py   load + clean + enrich + %C34% cohort  → stg_claims
            │    build_warehouse.py  facts, dimensions
            │    lot.py       lines of therapy (7 business rules)
            │    io_cohort.py final IO cohort, SOB, TRx/NRx/RRx/NBRx
            │    procedures.sql  install fn_* stored functions
            ▼
 Data/warehouse/ciq.duckdb  (read-only at runtime)
            │
            │  ciq/tools/*.py   one tool per analysis → ToolResult {data, charts, insights, caveats, kpis}
            │  ciq/tools/registry.py   tool catalog (Claude tool schemas) + run_tool()
            ▼
 app/agent.py   Claude on Bedrock ⇄ tools loop, streamed as events
 app/server.py  FastAPI: chat (SSE), command center, compare, journey, cohorts, conversations, static UI
            ▼
 app/static/   browser UI (index.html, app.js, journey.js, styles.css)
```

## 4. Build pipeline

Entry point `ciq/build/build_warehouse.py`; the input is chosen by `CIQ_SOURCE`.

| Step | Module | Output |
|---|---|---|
| 1. Stage the source | `sources.py` | `src_master_nsclc` (complete cleaned extract), `ref_icd10cm`, `ref_code_desc_v1`, `ref_patient_age`, `ref_claim_billing`, and a temporary `stg_claims` in a canonical long layout (one row per claim-line code) |
| 2. Map drugs | `build_warehouse.build_drug_map` | `ref_drug`, `ref_drug_code_map` (every drug signature → molecule / product / class / therapy group) |
| 3. Facts | `build_warehouse` | `fact_drug_event`, `fact_dx_event`, `fact_px_event` |
| 4. Dimensions | `build_warehouse` | `dim_patient`, `dim_hcp`, `ref_data_coverage` |
| 5. Lines of therapy | `lot.py` | `lot_line`, `lot_line_drug` (+ `dim_patient.max_line`, `clean_start`) |
| 6. IO cohort | `io_cohort.py` | `fact_io_claim` |
| 7. Stored functions | `sql/procedures.sql` | `fn_*` table macros |
| 8. Record parameters | `build_warehouse` | `ref_build_params` (rules and source used by this build) |

**Source adapters** (`sources.py`) turn any input into the same `stg_claims` layout, so everything after step 1 is
input-agnostic:
- `master_nsclc` (default, v2): the 15-column CSV is cleaned, enriched and reshaped. The upstream file repeats each
  medical line once per diagnosis slot; the adapter rebuilds a line key and emits one diagnosis row per line and
  code plus one procedure/drug "anchor" row per line.
- `claims_master` (legacy, v1): `claims_master.parquet` is already in this layout.

Staging runs as a chain of temporary tables, because DuckDB planned the single combined query badly (it stalled).
Keep it that way.

**Rebuild** whenever the data, `drug_reference.csv`, `config.py` business rules or build code changes. Stored
functions alone can be reinstalled with `python -m ciq.build.install_procedures`. The running app holds the database
file open, so stop it before rebuilding.

## 5. Warehouse

| Table | Grain | Purpose |
|---|---|---|
| `src_master_nsclc` | source row | The complete input extract, cleaned; originals in `*_raw` columns |
| `fact_drug_event` | patient × day × molecule | Every drug administration or fill, with product, class, therapy group, form, HCP, payer, days supply |
| `fact_dx_event` | patient × day × ICD-10 code | Diagnoses with description and category |
| `fact_px_event` | patient × day × procedure code | Procedures (CPT/HCPCS) |
| `dim_patient` | patient | Age, age group, payment type, first diagnosis / treatment dates, max line |
| `dim_hcp` | HCP | Volumes and patient counts |
| `lot_line` / `lot_line_drug` | patient × line (× drug) | Lines of therapy with regimen and start reason |
| `fact_io_claim` | patient × day × IO brand | Final IO cohort with line, SOB, TRx/NRx/RRx/NBRx, week/month |
| `ref_*` | — | Drug dictionary and code map, ICD-10-CM, enrichment tables, data coverage, build parameters |

Full column list: [DATA_DICTIONARY.md](DATA_DICTIONARY.md).

### Stored functions (`ciq/sql/procedures.sql`)

DuckDB table macros; call with `select * from fn_x(...)`. **Arguments with defaults must be passed by name**
(`p_window_days := 540`), including when one macro calls another.

| Area | Functions |
|---|---|
| Catalog & cohort | `fn_product_catalog`, `fn_product_index` (first claim per patient), `fn_cohort_patients` |
| Market | `fn_incidence_prevalence`, `fn_market_share`, `fn_product_trend`, `fn_age_distribution` |
| Patient | `fn_pre_index_treatments`, `fn_sob_events`, `fn_source_of_business`, `fn_payment_type_mix`, `fn_top_payers` |
| Lines | `fn_lines`, `fn_combination_type`, `fn_regimen_distribution`, `fn_line_progression`, `fn_product_share_by_line`, `fn_top_regimens_by_line` |
| Diagnosis | `fn_dx_around_index` |
| Adherence | `fn_therapy_episodes` |
| Physician | `fn_hcp_market_volume`, `fn_hcp_market_deciles`, `fn_hcp_product_share`, `fn_hcp_product_share_deciles` |
| IO brands | `fn_io_sob`, `fn_io_claim_types` |
| Helpers | `data_start()`, `data_end()`, `shift_days(d, n)` |

## 6. Tool layer (`ciq/tools/`)

A **tool** wraps one or more stored functions. It adds Vega-Lite chart specs, KPIs, insights and caveats, and
returns a `ToolResult` (`base.py`):
- `to_dict()` goes to the UI: data tables, charts, KPIs, notes.
- `to_llm_text()` goes back to the model: compact CSV-like tables plus the insights and caveats.

| Module | Tools |
|---|---|
| `analytics.py` | `data_overview`, `list_products`, `market_incidence_prevalence`, `market_share`, `product_trend`, `patient_demographics`, `pre_index_treatments`, `source_of_business`, `payer_mix`, `combination_therapy`, `line_progression`, `product_share_by_line`, `treatments_by_line`, `dx_around_index`, `persistency`, `hcp_market_concentration`, `hcp_product_share_deciles`, `compare_products`, `run_sql` (read-only SQL with an optional custom chart) |
| `io_brands.py` | `io_brand_dynamics`: IO source of business + TRx/NRx/RRx/NBRx by month/week |
| `compare.py` | `compare_segments` (brands, HCP cohorts, patient cohorts or periods); saved HCP cohorts |
| `journey.py` | `patient_journey` (journey map, transitions, statuses, patterns, HCPs) |
| `pathway.py` | `pathway_diagram` (editable stage diagram) |
| `customize.py` | `customize_chart` (highlight, recolour, filter, reference lines, chart type on any earlier chart) |
| `command_center.py` | Market Command Center data (used by the home screen, not an agent tool) |
| `charts.py` | Vega-Lite builders: bar, grouped/stacked bar, line, donut, scatter, heatmap. Uses a colour-blind-validated palette; entities keep their colour across charts |
| `registry.py` | Tool definitions for Claude (name, description, JSON schema) and `run_tool()` |

Agent-only tools in the registry: `ask_user` (ask a clarifying question) and `discard_result` (withdraw a wrong result).

`ciq/methodology.py` holds the plain-language method for every tool, shown in the card's **Notes** / (i) panel
and given to the agent.

## 7. The AI agent (`app/agent.py`)

**Model call:**
- Claude Sonnet 5 through `AnthropicBedrock`, with streaming.
- **Adaptive thinking** with summarized display (the "Thinking" panel), effort `high`.
- The system prompt and tool list are **prompt-cached**; keep the system prompt stable.

**Loop per question:**
1. The user's message is sent with a context line naming the focus brand.
2. Claude calls one or more tools. They run **in parallel** in a thread pool; inputs are validated against each
   tool's schema first.
3. Results go back to Claude as text, and to the UI as cards.
4. Repeat until Claude answers without tools, up to `CIQ_MAX_AGENT_STEPS` (12).

**Events streamed to the browser (SSE):**
- `status`, `thinking`, `text`
- `tool_start`, `tool_result`
- `ask` (clarifying question), `withdraw` (a result was retracted)
- `error`, `done`

**Clarifying questions:** when Claude calls `ask_user`, the turn ends and the question is stored as the
conversation's `pending` state. The user's next message is sent back as the answer.

**Conversations** are stored in `Data/conversations/<id>.json`:
- `api_messages`: the exact model history, including thinking blocks, which must be replayed unchanged
- `transcript`: what the UI re-renders

**Model access:** the IAM identity must be allowed to invoke the inference profile. The newer Bedrock "Mantle"
endpoint is explicitly denied for our users, which is why the bedrock-runtime client is used.

## 8. Backend API (`app/server.py`)

| Method & path | Purpose |
|---|---|
| `GET /` | The web UI |
| `GET /api/meta` | Coverage, model, products, suggested questions, tool list, user profile |
| `GET /api/overview?product=` | Start-screen KPIs and brand trend |
| `GET /api/command-center?product=&window=` | Market Command Center |
| `POST /api/chat` | Ask a question; streams agent events (SSE) |
| `GET /api/compare/options`, `POST /api/compare` | Compare screen: available sides; run a comparison |
| `GET /api/cohorts`, `POST /api/cohorts` | List / save HCP cohorts (`Data/cohorts/cohorts.json`) |
| `GET /api/journey`, `/api/journey/pathway`, `/transition`, `/patients`, `/patient/{pid}`, `/segments`, `/compare` | Patient Journey screen |
| `GET /api/conversations`, `GET/DELETE /api/conversations/{cid}` | Conversation history |
| `POST /api/tools/{name}` | Run any tool directly, without the model (used to re-run cards, e.g. swapping a brand in a comparison) |

Streamed events, conversations, journey data and direct tool results pass through `json_safe()`, which turns
NaN/inf into null (they aren't valid JSON). Use it for any new endpoint that returns analysis data.

## 9. Frontend (`app/static/`)

- `index.html` + `app.js`: shell, sidebar (new analysis, Patient Journey, search, recents), context bar (brand
  selector, Compare, data sources), Command Center, chat with streaming events, result cards (Chart / Data / Notes,
  CSV / image export, fullscreen), comparison panels, clarifying-question UI.
- `journey.js`: the Patient Journey screen.
- `styles.css`: Chryselys branding (navy `#004567`, gold `#C98B27`, Inter / Source Serif), light theme, responsive
  down to phone width, reduced-motion support.
- Charts are Vega-Lite specs produced in Python and rendered with vega-embed.
- The data-source "connect" dialog is a **UI prototype** with no backend yet.

## 10. Configuration

`.env` in the repo root (git-ignored; template `.env.example`); real environment variables win.

| Variable | Default | Meaning |
|---|---|---|
| `CIQ_SOURCE` | `master_nsclc` | Build input: `master_nsclc` (v2) or `claims_master` (v1) |
| `AWS_PROFILE`, `AWS_REGION` | default chain, `us-east-1` | AWS credentials for Bedrock |
| `CIQ_MODEL`, `CIQ_MODEL_LABEL` | `us.anthropic.claude-sonnet-5`, `Claude Sonnet 5` | Bedrock inference profile and its display name |
| `CIQ_EFFORT` | `high` | Model effort: `low` … `max` |
| `CIQ_MAX_TOKENS`, `CIQ_MAX_AGENT_STEPS` | `32000`, `12` | Output cap per model call; tool rounds per question |
| `CIQ_USER_NAME`, `CIQ_USER_EMAIL`, `CIQ_USER_ROLE` | OS user, empty, `Brand analytics` | Profile shown in the sidebar (there is no sign-in yet) |

Business-rule parameters (cohort pattern, line-of-therapy window / gap / therapy groups, IO brands and SC
codes, SOB washout) are in `ciq/config.py`. They are **not** environment variables, because changing them means
rebuilding and re-testing.

## 11. Repository layout

| Path | Contents |
|---|---|
| `ciq/config.py` | Paths, source switch, cohort and business-rule parameters, IO brands |
| `ciq/reference/` | `drug_reference.csv` (drug dictionary), `icd10cm.csv` (CMS diagnosis descriptions) |
| `ciq/build/` | `build_warehouse.py`, `sources.py`, `lot.py`, `io_cohort.py`, `install_procedures.py` |
| `ciq/sql/procedures.sql` | All stored functions |
| `ciq/tools/` | Tools, charts, registry (see §6) |
| `ciq/methodology.py` | Method text per analysis |
| `app/` | `server.py`, `agent.py`, `store.py`, `settings.py`, `static/` |
| `scripts/` | `doctor.py` (environment check), `fetch_icd10cm.py`, `tool_gallery.py` |
| `tests/` | Tools, line-of-therapy rules, IO business rules, compare, charts, journey |
| `Data/` | Local data only (git-ignored except README/.gitkeep): `real_data/`, `master/`, `warehouse/`, `conversations/`, `cohorts/` |
| `setup.ps1` / `setup.sh`, `run.ps1` / `run.sh` | One-command setup and start |

## 12. Security and data handling

- **Patient-level data stays out of git.** `.gitignore` excludes `Data/*` (except README/.gitkeep), notebooks,
  `.env` and the generated tool gallery.
- **No credentials in code.** AWS uses the standard credential chain (profile / SSO). The app never sees access
  keys in config.
- **Read-only at runtime.** The app opens the warehouse read-only. `run_sql` accepts a single SELECT / WITH /
  DESCRIBE statement and rejects writes.
- **No authentication yet.** The app is designed for local or trusted-network use. Before any shared deployment,
  put it behind authentication and an IP allowlist (see §13).
- **Data use:** confirm hosting rights with the data owner before moving the claims or warehouse to any cloud
  environment.

## 13. Deployment

Today CIQ runs locally (`run.ps1` / `run.sh`, http://localhost:8000). For a demo deployment the minimum is:
- one EC2 instance (t3.medium) with an IAM instance role for Bedrock,
- a reverse proxy for HTTPS and basic auth (e.g. Caddy), a security group limited to known IPs, and SSM instead of SSH.

Only the code and `ciq.duckdb` are needed at runtime; the input CSVs are needed only to rebuild. Keep a single
uvicorn worker, because the conversation store is file-based.

## 14. Known limitations and technical debt

- **No authentication or user management;** the conversation and cohort stores are local JSON files for a single
  instance.
- **The data-source connector is UI-only.**
- **Build time is about 2 minutes,** mostly the Python line-of-therapy loop. Fine at this size; for much larger data,
  move lines of therapy to SQL or vectorise it.
- **Procedure descriptions are partial** (CPT text is licensed).
- **Data window of 7 months** limits long look-backs (see BUSINESS_RULES.md §12).
- **A few source files start with a UTF-8 BOM.** Python handles this; be careful with tools that read files as
  plain text.
