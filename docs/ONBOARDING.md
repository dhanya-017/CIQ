# Welcome to CIQ: start here

CIQ ("Chryselys IQ") is Chryselys' data intelligence platform for pharma brand and patient analytics. An analyst asks
a business question in plain English, for example "What is Keytruda's patient share in each line of therapy?". CIQ
then:
1. picks the right analyses,
2. runs them on healthcare claims data,
3. shows the results as charts and tables,
4. writes a short analyst read-out, with the caveats that apply.

This page gets you oriented in your first days. It links to four deeper documents:

| Document | Read it for |
|---|---|
| [BUSINESS_RULES.md](BUSINESS_RULES.md) | **What the numbers mean.** Cohort, line-of-therapy rules, source of business, claim types, persistency, deciles, patient journey, and the assumptions behind them |
| [ARCHITECTURE.md](ARCHITECTURE.md) | **How it is built.** Components, data flow, warehouse, stored functions, tools, the AI agent, UI, API, configuration, security |
| [DATA_DICTIONARY.md](DATA_DICTIONARY.md) | **What each file and table contains.** The input file, cleaning, reference files, and every warehouse table and column |
| [DEVELOPER.md](DEVELOPER.md) | **How to change it.** Setup, adding a drug or an analysis, changing rules, testing, debugging |

Analysts and business users can mostly read this page and BUSINESS_RULES.md. Developers should read all five.

---

## 1. The product in one minute

- **Data:** US medical (MX) and pharmacy (RX) claims for about 10,000 lung-cancer (NSCLC market) patients,
  January to July 2026. The claims are IQVIA APLD-style (anonymised patient-level data).
- **Focus:** brand and patient analytics for immuno-oncology (IO) brands: Keytruda, Opdivo and Tecentriq, including
  their subcutaneous forms (Keytruda Qlex, Opdivo Qvantig, Tecentriq Hybreza). The rest of the lung-cancer market
  is included as competitive context.
- **Questions it answers** (the original brief):
  - market size and trend
  - patient demographics
  - treatments before a brand
  - source of business
  - payer mix
  - combination therapy and lines of therapy
  - diagnoses around the first dose
  - persistency
  - prescriber (HCP) deciles

  Newer additions:
  - Market Command Center
  - brand and cohort comparison
  - patient journey and pathway diagrams
  - IO source of business with TRx / NRx / RRx / NBRx
- **How:** a DuckDB analytics warehouse holds the claims and business rules. Each question is a tested **stored
  function**. An AI agent (Claude Sonnet 5 on Amazon Bedrock) chooses which of these to run and explains the
  results. The agent never calculates numbers itself.

## 2. The screens

| Screen | What it does |
|---|---|
| **Market Command Center** (home) | The brand in its market: size, brand performance, what changed versus the previous period, where the opportunity is. Every signal comes with its evidence and a suggested action. |
| **Chat** | Ask anything. Each answer shows the analysis steps it ran, result cards (Chart / Data / Notes, CSV and image export), a written read-out and follow-up suggestions. Conversations are saved and searchable. |
| **Compare** | Side-by-side comparison of brands, HCP cohorts, patient cohorts or time periods. It shows what is different, where the gap comes from, and which HCPs are behind it. |
| **Patient Journey** | How newly diagnosed patients move from suspicion through imaging, consult, tissue diagnosis, diagnosis and biomarker testing to treatment, and where they stall. |
| **Brand selector** (top bar) | Sets the focus brand used whenever a question doesn't name one. |
| **Data sources** (top bar) | Shows the connected source (IQVIA Claims). The "connect a data source" dialog is a **front-end prototype**: no backend is wired up yet. |

## 3. Day-one checklist

Ask the project lead (Satyam Kumar) for:
- [ ] **GitHub access** to the private repo `SatyamSaitama/ciq-platform`.
- [ ] **The data** from the secure data share: the claims file `master_nsclc.csv` (the only input) and,
      optionally, `patient_age.csv`. These are patient-level data: never email them, copy them to personal
      storage, or commit them to git.
- [ ] **AWS credentials** with Amazon Bedrock access. Your IAM identity needs `bedrock:InvokeModel` and
      `bedrock:InvokeModelWithResponseStream` on the `us.anthropic.claude-sonnet-5` inference profile, in `us-east-1`.

Then set up your machine (about 10 minutes; details in [DEVELOPER.md](DEVELOPER.md)):
1. Clone the repo.
2. Put `master_nsclc.csv` in `Data/real_data/` (and `patient_age.csv`, if you have it, in `Data/reference/`).
3. Run the setup script (it builds the warehouse in about 70 seconds, runs the tests and checks your environment):
   - Windows: `.\setup.ps1`
   - macOS/Linux: `./setup.sh`
4. Start the app and open http://localhost:8000:
   - Windows: `.\run.ps1`
   - macOS/Linux: `./run.sh`
5. If anything fails: `python scripts/doctor.py`. It checks every prerequisite and prints the fix.

## 4. Your first week

| Day | Do this | Why |
|---|---|---|
| 1 | Setup (above); click through every screen with Keytruda as the focus brand | See the product the way users do |
| 1 | Ask a few of the example questions on the start screen; open each card's **Notes** tab | Notes show how each number is calculated |
| 2 | Read [BUSINESS_RULES.md](BUSINESS_RULES.md) end to end | The rules are the product; most questions from users are about definitions |
| 2 | Open the warehouse (`duckdb Data/warehouse/ciq.duckdb`) and follow one patient through `src_master_nsclc` → `fact_drug_event` → `lot_line` → `fact_io_claim` | Seeing one patient's lines and SOB labels makes the rules concrete |
| 3 | Read [ARCHITECTURE.md](ARCHITECTURE.md); trace one chat request through `app/agent.py` → `ciq/tools/registry.py` → a tool → a `fn_*` macro | The whole stack in one path |
| 4 | Run the tests (`python -m pytest tests -q`) and read `tests/test_lot_rules.py` | The line-of-therapy rules are specified as tests |
| 5 | Make a small change on a branch (e.g. add a drug to `ciq/reference/drug_reference.csv`), rebuild, test, open a PR | The full change loop |

## 5. Glossary

| Term | Meaning |
|---|---|
| **MX / RX** | Medical claims (procedures, infusions, diagnoses) / pharmacy claims (dispensed prescriptions) |
| **APLD** | Anonymised patient-level data: claims linked by a de-identified patient ID |
| **NSCLC** | Non-small cell lung cancer. The cohort is defined by ICD-10 code C34 (lung cancer) |
| **ICD-10-CM** | Diagnosis code system (e.g. `C34.90`) |
| **CPT / HCPCS** | Procedure code systems. HCPCS "J-codes" (e.g. `J9271`) are drugs administered in a clinic and billed as procedures |
| **NDC** | National Drug Code: an 11-digit product code on pharmacy and some medical drug claims |
| **IO** | Immuno-oncology: here the PD-1/PD-L1 brands Keytruda (pembrolizumab), Opdivo (nivolumab) and Tecentriq (atezolizumab) |
| **IV / SC** | Intravenous / subcutaneous administration. Keytruda Qlex, Opdivo Qvantig and Tecentriq Hybreza are SC forms of the same molecules |
| **Biosimilar** | A near-identical version of a biologic drug (e.g. Mvasi for bevacizumab). Treated as the same drug |
| **LOT** | Line of therapy: a treatment regimen; line 2 starts when the regimen changes by the rules |
| **Regimen** | The set of drugs defining a line (e.g. carboplatin + pemetrexed + Keytruda) |
| **SOB** | Source of business: where a brand's claims come from (new starts, continuing patients, switches) |
| **TRx / NRx / RRx / NBRx** | Total / new / repeat / new-to-brand prescriptions (claims); see BUSINESS_RULES.md §6 |
| **HCP** | Health care professional (rendering or prescribing provider) |
| **Decile** | HCPs ranked by volume and split into 10 groups; decile 1 = highest volume |
| **Persistency** | How long patients stay on a drug (Kaplan-Meier survival-style curve) |
| **Index date** | A patient's first claim of the product being analysed; "before" and "after" windows count from it |
| **Washout** | The first days of data, where a patient's earlier history cannot be seen |
| **Stored function / `fn_*`** | A parameterised SQL query saved in the warehouse; each answers one business question |
| **Tool** | A Python wrapper around stored functions that the AI agent can call; returns data, charts, insights, caveats |

## 6. Ground rules

1. **Patient data never leaves the secure environment.** Nothing from `Data/` goes to git (`.gitignore` enforces this;
   don't force-add). Your notebooks contain patient-level output and are git-ignored too.
2. **Numbers come from tested code, never from the AI.** The agent chooses analyses and explains them; every figure
   comes from a stored function you can re-run.
3. **Every analysis states its caveats.** The data covers only seven months, so look-back windows are
   truncated and "new" patients early in the window may not really be new. Say so when you share results.
4. **Business rules change only with the business.** They are agreed definitions (see BUSINESS_RULES.md). Change
   `ciq/config.py` / the build and the matching tests together, and record why.
5. **Work on branches.** `main` is the known-good version. The pre-migration version is tagged `v1-claims-master`.

## 7. Current status (September 2026)

- **Data:** the input is `master_nsclc.csv` (the v2 extract), built on branch `feature/master-nsclc`. It has
  not been merged to `main` yet.
- **Input:** one claims file, plug and play (any file type or header style; validated before every build).
- **Rollback:** previous warehouses are kept in `Data/warehouse/backup/`; the pre-migration code is git tag
  `v1-claims-master`.
- **Open decisions** awaiting business confirmation are listed in [BUSINESS_RULES.md §11](BUSINESS_RULES.md#11-assumptions-and-open-decisions).
