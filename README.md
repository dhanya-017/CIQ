# CIQ Platform — Data & Tool Layer

Data layer for an agentic brand and patient analytics platform built on the claims data in
`Data/` (medical claims (MX) and pharmacy claims (RX), lung cancer / NSCLC market, ~10,000 patients).
The claims are loaded into a DuckDB warehouse. Each business question is answered by a **stored function**
(a DuckDB table macro), and each stored function is wrapped as an **agent tool**. A tool returns data,
Vega-Lite chart specs, headline insights and data caveats.

```
Data/real_data/master_nsclc.csv ─┐
Data/real_data/mx_nsclc.csv ─────┼─► build_warehouse.py ──► Data/warehouse/ciq.duckdb
ciq/reference/icd10cm.csv ───────┘   (ciq/build/sources.py)    ├── src_*   complete input extract, cleaned
                                                             ├── ref_*   drug dictionary, descriptions, enrichment
                                                             ├── fact_*  drug / dx / procedure events
                                                             ├── dim_*   patient, hcp
                                                             ├── lot_*   lines of therapy
                                                             ├── fact_io_claim  final IO cohort + SOB + TRx/NRx/NBRx
                                                             └── fn_*    stored functions (tools)
ciq/tools/analytics.py  ── one Python tool per question (calls fn_*, adds chart + insights + caveats)
ciq/tools/registry.py   ── Claude tool-use schemas + run_tool() dispatcher
```

## Getting started (new developers)

1. Clone the repo.
2. Put `master_nsclc.csv` and `mx_nsclc.csv` in `Data/real_data/` (see [Data/README.md](Data/README.md)).
3. Run `.\setup.ps1` (Windows) or `./setup.sh` (macOS/Linux).
4. Start the app with `.\run.ps1` or `./run.sh`.
5. If anything fails, run `python scripts/doctor.py` for a step-by-step check with fixes.

The full walkthrough is in [docs/DEVELOPER.md](docs/DEVELOPER.md): architecture, how a request flows through
the system, and how to add a drug, add an analysis, change business rules, or debug.

## Quick start (manual)

```bash
pip install -r requirements.txt
python -m ciq.build.build_warehouse        # full build (~2 min)
python -m ciq.build.install_procedures     # reinstall only the fn_* after editing ciq/sql/procedures.sql
python -m pytest tests -q                  # tools, business rules, LOT rules, charts
python scripts/tool_gallery.py [PRODUCT]   # docs/tool_gallery.html: every tool's charts, for review
```

```python
from ciq.tools import anthropic_tools, run_tool
tools = anthropic_tools()                        # pass to client.messages.create(tools=...)
res = run_tool("payer_mix", {"product": "KEYTRUDA", "top_n": 10})
res.to_llm_text()   # compact text to return as the tool_result
res.to_dict()       # {data, charts (Vega-Lite), insights, caveats, meta} for the UI
```

The stored functions can also be called straight from SQL:
`select * from fn_product_share_by_line('KEYTRUDA', p_max_line_bucket := 4)`.
DuckDB rule: any argument that has a default must be passed by name (`p_x := ...`).

## Web app (backend and UI in one server)

```powershell
.\run.ps1            # builds the warehouse if it is missing, then serves http://localhost:8000
```

- **Backend** (`app/`): a FastAPI server that also serves the static UI.
  - `app/agent.py` runs Claude Sonnet 5 on Amazon Bedrock with the CIQ tools.
  - It uses the `AnthropicBedrock` client (bedrock-runtime), streaming, summarized adaptive thinking, and prompt caching of the system prompt and tools.
  - Answers stream to the browser as server-sent events.
  - Conversations are saved to `Data/conversations/*.json`.
- **UI** (`app/static/`): plain HTML/JS/CSS with no build step, using the Chryselys branding (navy `#004567`, gold `#C98B27`, logo mark).
  - Brand selector, KPI start screen and suggested questions.
  - A live "analysis steps" trace.
  - Result cards with Chart / Data / Notes tabs and CSV download.
  - A streaming markdown read-out, and conversation history.
- **Credentials**: the standard AWS chain (default profile, region `us-east-1`). Environment variables you can set:
  - `AWS_PROFILE` and `AWS_REGION`
  - `CIQ_MODEL` (default `us.anthropic.claude-sonnet-5`)
  - `CIQ_EFFORT` (default `high`)
  - Your IAM user is denied the newer Bedrock "Mantle" endpoint, so the app uses bedrock-runtime.
- **API**:
  - `GET /api/meta`
  - `GET /api/overview?product=`
  - `POST /api/chat` (SSE)
  - `GET/DELETE /api/conversations[/id]`
  - `POST /api/tools/{name}` runs a tool directly, without the LLM

## Which tool answers which question

| Question | Tool | Stored function(s) |
|---|---|---|
| Yearly incidence & prevalence and how they have changed | `market_incidence_prevalence` | `fn_incidence_prevalence` |
| Demographics of the brand cohort | `patient_demographics` | `fn_age_distribution`, `fn_cohort_patients` |
| Most common treatments in the 24 months before the first brand dose | `pre_index_treatments` | `fn_pre_index_treatments` |
| Source of business | `source_of_business` | `fn_sob_events`, `fn_source_of_business` |
| Payer mix and top 10 payers | `payer_mix` | `fn_payment_type_mix`, `fn_top_payers` |
| Combination therapies | `combination_therapy` | `fn_combination_type`, `fn_regimen_distribution` |
| % of patients progressing to line 3+ | `line_progression` | `fn_line_progression` |
| Brand patient share in each line | `product_share_by_line` | `fn_product_share_by_line` |
| Common treatments in lines 1, 2, 3, 4+ | `treatments_by_line` | `fn_top_regimens_by_line` |
| Top dx codes 18 months before and after the first dose | `dx_around_index` (`window_days=540`) | `fn_dx_around_index` |
| Treatment persistency | `persistency` (Kaplan-Meier) | `fn_therapy_episodes` |
| HCP deciles: is the market concentrated or scattered (pre vs post) | `hcp_market_concentration` | `fn_hcp_market_volume`, `fn_hcp_market_deciles` |
| HCP deciles by brand market share | `hcp_product_share_deciles` | `fn_hcp_product_share`, `fn_hcp_product_share_deciles` |
| Brand uptake and competitive share | `product_trend`, `market_share` | `fn_product_trend`, `fn_market_share` |
| IO brand SOB (Initiated / Continuing / Switched from Competitor / IV-to-SC) and TRx-NRx-RRx-NBRx by month or week | `io_brand_dynamics` | `fn_io_sob`, `fn_io_claim_types` |
| Discovery and ad-hoc questions | `data_overview`, `list_products`, `run_sql` (read-only) | `fn_product_catalog` |

**The brand is a parameter in every tool** (`product`). It accepts a brand or a molecule name and is
case-insensitive. The default is `KEYTRUDA`, the largest brand in this NSCLC data; change it in `ciq/config.py`.

## Business rules

- **Drug identification** (`ciq/reference/drug_reference.csv`, about 100 molecules). A HCPCS J/Q/C code match
  is tried first. If none matches, a regex on the NDC product name is used (this covers pharmacy claims and
  unclassified codes such as J9999/C9399, e.g. Keytruda Qlex and Rybrevant Faspro). Any J9 code still
  unmapped falls back to `OTHER ANTINEOPLASTIC`. Each drug belongs to one therapy group:
  - `LUNG_SYSTEMIC`: defines the market and the lines of therapy
  - `OTHER_ONCOLOGY`: drugs for other tumours, such as hormonal therapy and ADT
  - `SUPPORTIVE`: G-CSF, bone agents, ESAs and similar
  To add a drug, add a row to the CSV and rebuild.
- **Events** are deduplicated to one row per patient, day and molecule. Only paid pharmacy claims are kept.
  Infused drugs are given a standard cycle length as their days supply (`default_days_supply`).
- **Input and cohort** (`ciq/build/sources.py`, `CIQ_SOURCE`):
  - Default input `Data/real_data/master_nsclc.csv` (15 columns), loaded completely into `src_master_nsclc`
    (IDs cleaned of `.0`, NDCs padded to 11 digits, the `renderring_provider_id` typo column merged).
  - Fields the app needs but the extract lacks are enriched: **age** and **billing provider** from
    `mx_nsclc.csv`, **diagnosis descriptions** from the CMS ICD-10-CM file (`scripts/fetch_icd10cm.py`), and
    the v1 master as a fallback.
  - **Cohort:** only patients with a diagnosis `LIKE '%C34%'` are kept.
- **Lines of therapy** (`ciq/build/lot.py`), built from **all anticancer drugs** (pharmacy NDCs and J-code
  drugs billed as procedures; supportive care excluded):
  1. A gap of **120+ days** between administrations is line advancement (even if the same drugs resume).
  2. Adding a new drug is line advancement.
  3. Removing a drug from the regimen is not.
  4. A new drug after the first **28 days and 2 cycles** is line advancement.
  5. The first 28 days and 2 cycles define the regimen; changes inside that window don't advance the line.
  6. Changing to a completely new product is line advancement.
  7. Biosimilars and different forms (subcutaneous) are not: drugs are compared by molecule.
  - Parameters are in `ciq/config.py`; the rules are tested in `tests/test_lot_rules.py`.
- **Final IO cohort** (`ciq/build/io_cohort.py` → `fact_io_claim`): after lines are built, only Keytruda,
  Keytruda Qlex, Opdivo, Opdivo Qvantig, Tecentriq and Tecentriq Hybreza records are kept, each with its line.
  - **SOB** (per claim, vs the patient's previous IO claim): **Initiated** (no earlier IO claim, or the same
    brand after a 120+ day gap), **Switched from Competitor** (previous claim a different brand), **Keytruda to
    Keytruda Qlex** / **Opdivo to Opdivo Qvantig** / **Tecentriq to Tecentriq Hybreza** (IV then SC), else
    **Continuing**. Reported by month or week.
  - **Claim types:** TRx = every claim; NBRx = new to brand; NRx = NBRx, re-initiations, IV-to-SC switches, the
    brand's first claim in a new line, and pharmacy fills with refill code 0; RRx = TRx - NRx.
  - Claims in the first 90 days of data are flagged `in_washout` (earlier IO use can't be seen).
- **Index date** is the patient's first claim for the product. Pre and post windows are measured from it.
- **Source of business** (per claim or per patient):
  - Continuing: the brand was used within the grace period before.
  - Restart / Restart + Switch: the brand was used earlier in the look-back, but the gap is longer than the grace period.
  - New to Brand + Add-on / New to Brand + Switch: no prior brand use, but another lung therapy was used in the look-back.
  - New to Market: no lung therapy at all in the look-back.
- **Persistency**: episodes break when the gap after the last days-supply coverage is longer than the grace
  period. Patients still on therapy at the data end are censored (Kaplan-Meier), not counted as discontinued.
- **HCP deciles**: decile 1 = highest-volume HCPs.
  - `hcp_count`: every decile has the same number of HCPs.
  - `volume`: every decile holds about 10% of the volume.
  - The HCP is the rendering provider on medical claims and the prescriber on pharmacy claims.

## Rolling back to v1 (claims_master.parquet)

The build before the master_nsclc migration is preserved three ways:
- **Code:** git tag `v1-claims-master` (`git checkout v1-claims-master`).
- **Data:** `Data/warehouse/backup/ciq_v1_claims_master_2026-09-25.duckdb`; copy it over `Data/warehouse/ciq.duckdb`
  (with the app stopped) to serve the old numbers immediately.
- **Build:** `CIQ_SOURCE=claims_master python -m ciq.build.build_warehouse` rebuilds from the v1 master with the
  current code.

## Data limitations (the tools report these in `caveats`)

- **The data covers only 2026-01-01 to 2026-07-31 (7 months).**
  - Yearly trends cannot be measured, so the default grain is monthly.
  - 24-month and 18-month windows are cut short. Each tool reports how much of the requested window the data actually covers.
  - Early months overstate "new" patients because there is no prior history to check (left-censoring).
  - Few patients can be followed through more than one or two lines, so line progression and
    line 3+ results are understated. `line 1` means the first line seen in the data.
- **Gender is not in the source data**, so demographics cover age and payment type only.
- **Payer names are not in the source data.** Payers appear as plan ids with their main payment type.
  Pharmacy claims take the payment type the patient most often has on medical claims.
- Patient ages: 41 of the Keytruda patients have no age recorded, and a few are under 18.
