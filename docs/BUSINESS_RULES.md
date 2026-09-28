# CIQ business rules

This is the single reference for **how every number in CIQ is defined**. Each rule names the code that implements
it, so definitions and code can be checked against each other. Parameter values are the current defaults in
`ciq/config.py` or the stored functions (`ciq/sql/procedures.sql`). In the app, every result card's **Notes** tab
shows the same definitions for that analysis (from `ciq/methodology.py`).

Contents:
1. Data and patient cohort
2. Drug identification
3. Treatment events
4. Lines of therapy
5. Final IO cohort and source of business
6. Claim types: TRx, NRx, RRx, NBRx
7. Market, share and trend definitions
8. Other brand analytics (legacy SOB, persistency, payer, deciles, diagnoses)
9. Patient journey
10. Command Center and Compare
11. Assumptions and open decisions
12. Data limitations

---

## 1. Data and patient cohort

**Input:** ONE claims file, `Data/real_data/master_nsclc.csv`: a 15-column extract (5,778,852 rows, 9,993 patients,
2026-01-01 to 2026-07-31). Each row is one claim line and diagnosis slot, from one of three sources:

| `source` | Meaning | `code` holds |
|---|---|---|
| `RX` | Pharmacy claim (paid fills only) | NDC of the dispensed product |
| `MX_RX` | Medical claim line carrying a drug NDC | NDC (blank on most lines) |
| `MX_PX` | Medical claim line's procedure | CPT or HCPCS code (J-codes are drugs) |

**Cleaning** (`ciq/build/load.py`, after validation). The complete extract is kept in `src_master_nsclc`, cleaned:
- IDs lose the `.0` float artefact.
- NDCs are padded back to 11 digits (e.g. `2533754` becomes `00002533754`).
- The upstream `renderring_provider_id` typo column is merged into `rendering_provider_id`.
- Literal `"NAN"` codes are treated as blank.
- The original values are kept alongside, in `*_raw` columns.

**Reference files.** Separate files fill what the extract lacks:

| Field | Source | Coverage |
|---|---|---|
| Age | The claims file's own age column if present, else `Data/reference/patient_age.csv` | 86% of patients |
| Diagnosis descriptions | CMS ICD-10-CM FY2027 code file (`ciq/reference/icd10cm.csv`) | 99.96% of diagnosis events |
| Procedure descriptions | HCPCS Level II (`ciq/reference/hcpcs.csv`); CPT text is AMA-licensed, so none | HCPCS only; not used by any analysis |
| Gender | not available in any source | — |

**Cohort rule:** only patients with **at least one diagnosis matching `%C34%`** (lung cancer) are kept
(`COHORT_DX_LIKE`). This drops 24 of 9,993 patients, leaving **9,969**.

**Record funnel** from the extract to the analysis tables:
1. 5,778,852 source rows.
2. Minus 9,304 rows of the 24 non-lung-cancer patients.
3. Minus 1,940,877 `MX_RX` rows with no NDC: these only repeat the diagnoses of their `MX_PX` twin row.
4. The upstream file repeats every medical claim line once per diagnosis slot (up to 5). Collapsing that gives
   **1,042,390 claim lines**.
5. From those lines: 20,668 drug administrations/fills, 799,745 diagnosis events and 751,804 procedure events.

## 2. Drug identification

Every drug line is mapped to a **molecule** and **product** using `ciq/reference/drug_reference.csv` (about 100
molecules):
1. **HCPCS code first** (e.g. `J9271` is pembrolizumab / KEYTRUDA).
2. **Otherwise product name**, matched by regex against the NDC product name. The first matching row in the CSV
   wins, so specific names are listed before generic ones (nab-paclitaxel before paclitaxel).
3. **Unmapped `J9xxx`** chemotherapy codes become `OTHER ANTINEOPLASTIC`, so no chemo line is silently lost.
4. **Topical products** (creams) are excluded.

**Therapy groups** decide what counts where:

| Group | Examples | Market & share | Lines of therapy |
|---|---|---|---|
| `LUNG_SYSTEMIC` | IO, platinum, taxanes, pemetrexed, EGFR/ALK TKIs, ADCs, bevacizumab | yes | yes |
| `OTHER_ONCOLOGY` | hormonal therapy, ADT, non-lung chemo, J9 fallback | no | **yes** |
| `SUPPORTIVE` | G-CSF, denosumab, zoledronic acid, ESAs, trilaciclib, megestrol | no | no |

**Biosimilars and subcutaneous forms map to their reference molecule** (e.g. Mvasi, Zirabev → bevacizumab; Keytruda
Qlex → pembrolizumab). The **product form** (e.g. `KEYTRUDA QLEX`) is kept separately in `product_form`. For the IO
brands it is identified by:
- HCPCS: `J9277` Keytruda Qlex, `J9289` Opdivo Qvantig, `J9024` Tecentriq Hybreza
- or the product name containing QLEX / QVANTIG / HYBREZA.

## 3. Treatment events

- **One event per patient, day and molecule** (`fact_drug_event`). Several claim lines for the same drug on the
  same day count once.
- **Days supply:**
  - pharmacy fills: the dispensed days supply
  - infused drugs: a standard cycle length from the reference, e.g. 21 days for pembrolizumab, 14 for
    nivolumab, 28 for durvalumab
  - the larger of the two is used
- **HCP:** the rendering provider on medical claims; the prescriber on pharmacy claims.
- **Payment type:** the claim's own plan type (Commercial, Medicare, Medicare Advantage, Medicare Part D, Managed/FFS
  Medicaid, HIX…). If it's unknown, the patient's most frequent medical payment type is used instead.

## 4. Lines of therapy

Implemented in `ciq/build/lot.py`; specified by `tests/test_lot_rules.py`.

**Scope:**
- **Drugs:** all anticancer drugs (`LUNG_SYSTEMIC` + `OTHER_ONCOLOGY`), from pharmacy claims (NDC) and medical
  claims, including J-code drugs billed as procedures. Supportive care never defines a line.
- **Patients:** the lung-cancer cohort.

**Rules** (as agreed with the business):

| # | Rule | How it is applied |
|---|---|---|
| 1 | A gap of **120 days or more** between drug administrations is line advancement | Any gap of ≥ 120 days between consecutive administration dates starts a new line, even if the same drugs resume (flagged `is_retreatment`) |
| 2 | Adding a new drug is line advancement | After the regimen window, a drug not already in the regimen starts a new line |
| 3 | Removing a drug from the regimen is **not** line advancement | Dropping drugs (e.g. chemo-IO induction → IO maintenance) keeps the line |
| 4 | A new drug after the first **28 days and 2 cycles** is line advancement | See rule 5 for the window |
| 5 | The first 28 days and 2 cycles define the regimen; changes inside that window don't advance the line | Every drug given in days 1–28 of the line, or up to the line's 2nd administration if that comes later (slow schedules), joins the regimen |
| 6 | Changing to a completely new product is line advancement | A special case of rules 2/4 |
| 7 | Biosimilars and different forms (e.g. subcutaneous) are **not** line advancement | Drugs are compared by molecule; paclitaxel ↔ nab-paclitaxel is also treated as one drug (`LOT_EQUIVALENT_MOLECULES`) |

**Worked example** (from the business definition), all one line with regimen A + B + C:

| Day | Drugs | Why it doesn't advance |
|---|---|---|
| 1 | A, B | Line starts |
| 7 | C | Inside the first 28 days: joins the regimen |
| 21 | A, C | Inside the window |
| 35 | A, B | After the window, but A and B are already in the regimen |
| 42 | A, E (E = biosimilar of B) | E is the same molecule as B (rule 7) |

**Line fields:**
- Line end = last administration + days supply − 1, capped at the day before the next line and at data end.
- Line 1 = the **first line observed** in the data window, which may not be the patient's true first line.
- Each line records its regimen, the reason it started (`line_start_reason`) and whether it is ongoing at data end.

**Current output:** 2,825 lines for 2,488 treated patients. All 13 gaps of 120+ days in the data start a new line;
none of the 14 IV→SC switches does.

## 5. Final IO cohort and source of business

Implemented in `ciq/build/io_cohort.py`, producing `fact_io_claim`.

**Final cohort:** after lines of therapy are built from all anticancer drugs, **only these records are kept**:
Keytruda, Keytruda Qlex, Opdivo, Opdivo Qvantig, Tecentriq and Tecentriq Hybreza. Each keeps the line of therapy
it falls in. Current size: 2,810 claims (Keytruda 1,613, Opdivo 568, Tecentriq 629).

**Source of business (SOB)**, per brand family, reported by **month or week** (weeks start Monday). Each claim is
compared with the patient's **previous IO-brand claim**:

| SOB | Rule |
|---|---|
| **Initiated** | No earlier IO-brand claim in the data, **or** the same brand resumes after a gap of ≥ 120 days (a re-initiation, flagged `is_reinitiation`) |
| **Switched from Competitor** | The previous IO-brand claim was a different brand family |
| **Keytruda to Keytruda Qlex**, **Opdivo to Opdivo Qvantig**, **Tecentriq to Tecentriq Hybreza** | Same brand; the previous claim was IV and this one is subcutaneous |
| **Continuing** | Any other claim of the same brand (including SC back to IV) |

A patient who **starts directly on an SC form** is *Initiated*, not "to SC". For example, of 50 Keytruda Qlex
patients, 9 converted from IV Keytruda and 41 started on Qlex.

## 6. Claim types: TRx, NRx, RRx, NBRx

Also on `fact_io_claim` (one flag per claim, plus a `claim_type` label):

| Measure | Definition |
|---|---|
| **TRx** | Every IO-brand claim (total) |
| **NBRx** | New to brand: *Switched from Competitor*, or *Initiated* for a patient's first-ever claim of the brand. A re-initiation is **not** NBRx |
| **NRx** | New prescription: all NBRx, plus re-initiations, IV→SC switches, the brand's first claim in a new line of therapy, and pharmacy fills with refill code 0 |
| **RRx** | Repeat: TRx − NRx |

The hierarchy is NBRx ⊂ NRx ⊂ TRx, and `claim_type` = NBRx | NRx | RRx. Current Keytruda figures: TRx 1,613 =
NRx 647 + RRx 966; NBRx 612.

**Washout:** claims in the first **90 days** of data (Jan–Mar 2026) are flagged `in_washout`. There, a patient's
earlier IO use can't be seen, so *Initiated* and NBRx include patients who were already on therapy. The
`io_brand_dynamics` analysis can exclude them.

## 7. Market, share and trend definitions

- **Market:** patients treated with any `LUNG_SYSTEMIC` therapy.
- **Patient share:** a product's patients ÷ market patients. A patient on a combination counts for every product
  in it, so shares add to more than 100%.
- **Brand trend:** product patients, new-to-product patients (first claim of the product in the period) and share
  of the treated market, per month, quarter or year.
- **Incidence and prevalence** (lung cancer, ICD-10 C34):
  - prevalent = patients with a C34 diagnosis in the period
  - incident = patients whose first C34 diagnosis falls in the period, reported only once 90 days of history
    exist (earlier periods are flagged invalid)
  - treated / newly treated follow the same logic with lung systemic therapy
- **Demographics:** age (claims-recorded, one value per patient) and payment type. Gender is not available.

## 8. Other brand analytics

These apply to any product, not only the IO brands. The defaults can be changed per question.

| Analysis | Definition |
|---|---|
| **Treatments before first dose** (`pre_index_treatments`) | Drug classes, products or molecules received in the look-back window (default 730 days) before the patient's first claim of the product. Supportive care is excluded by default |
| **Source of business, general** (`source_of_business`) | Per administration, against a 180-day look-back and 90-day grace period: **Continuing** (product used within the grace period), **Restart** / **Restart + Switch** (used earlier, longer gap, with or without another therapy in between), **New to Brand + Add-on** (another lung therapy before and continuing after), **New to Brand + Switch** (another therapy before, not continued), **New to Market** (no lung therapy in the look-back). For the IO brands, prefer §5 |
| **Payer mix** | Share of claims (or patients) by payment type, and the top plans by claim volume. Payer **names** are not in the data; plans are shown by ID |
| **Combination therapy** | Share of lines that are monotherapy / doublet / triplet / quadruplet+, and the most common regimens |
| **Line progression / share by line / treatments by line** | From §4 lines. Lines are bucketed 1, 2, 3, 4+ |
| **Diagnoses around first dose** (`dx_around_index`) | Most common ICD-10 codes (full code or 3-character category) in a window before and/or after the first claim (default 540 days ≈ 18 months). Shown as % of the cohort and % of patients observed in the window |
| **Persistency** | Episodes of continuous therapy, where a new episode starts if the gap after the previous days-supply coverage exceeds the grace period (default 60 days). The first episode is summarised with a Kaplan-Meier curve; patients still on therapy at data end are **censored**, not counted as stopped. Curve points with fewer than 10 patients at risk are hidden |
| **HCP market concentration** | Treating HCPs' volume of all lung systemic therapy for the product's patients, in a window before (PRE) and after (POST) each patient's first claim (default 730 days). HCPs are split into deciles by count (equal numbers of HCPs) or by volume (each decile about 10% of volume); **decile 1 = highest volume** |
| **HCP deciles by brand share** | HCPs ranked by the product's volume (decile 1 = top), with the product's share of each decile's lung-therapy volume |
| **Brand comparison** (`compare_products`) | Scorecard across 3–4 brands (for two, use Compare, §10): patients, share, age, payer mix, share by line, combination use, source of business, persistency, prescriber base. The leader on each metric is highlighted |

## 9. Patient journey

Implemented in `ciq/tools/journey.py` and `pathway.py`. Everything is observed in claims; statuses are data
definitions, not clinical judgements.

**Cohort:** newly diagnosed patients, i.e. first C34 diagnosis at least 60 days after data start, so their
work-up is visible.

**Reference pathway stages** (each is the first date the stage is observed):

| Stage | Evidence in claims |
|---|---|
| Suspicion | Abnormal lung finding (ICD-10 R91.x, e.g. lung nodule) |
| Imaging work-up | CT chest, PET/CT, brain MRI (CPT 71250/71260/71270, 78815/78816, 70551–70553) |
| Specialist consult | New-patient consult visit (CPT 99204/99205); a proxy, as specialty is not in the data |
| Tissue diagnosis | Biopsy (bronchoscopic, EBUS, needle) or pathology (CPT 88305/88173) |
| Diagnosis confirmed | First lung-cancer diagnosis code (C34) |
| Biomarker testing | PD-L1 IHC, EGFR/BRAF/ALK/ROS1 molecular tests, NGS panels |
| Treatment start | First lung systemic therapy, lung resection surgery or radiation therapy |

Work-up stages are searched from 90 days before to 30 days after diagnosis.

**Journey status:**

| Status | Rule |
|---|---|
| On pathway | Treated within 30 days of diagnosis, not before diagnosis or tissue diagnosis |
| Delayed | Treated, but more than 30 days after diagnosis |
| Deviated | Treated before the diagnosis or tissue-diagnosis stage (by more than 7 days) |
| Stalled | No treatment 60+ days after diagnosis (with that much follow-up available) |
| In progress | No treatment yet, and diagnosed less than 60 days before data end |

**Pathway diagrams** can be edited in the chat (codes, stages, rules). An edited pathway re-runs the analysis and
is labelled as custom, with its differences from the standard pathway listed.

## 10. Command Center and Compare

- **Command Center:** compares the **last 3 months** of data (current; adjustable to 1–2) with the same number of months before (prior) for
  market size, brand performance and share. Every "what matters now" signal shows its evidence and a suggested action,
  from observed claims only (no AI-generated content).
- **Compare** (`compare_segments`) puts two sides on the same market, period and definitions:
  - **Sides** can be brands, HCP cohorts (e.g. by decile), patient cohorts or time periods.
  - **Predefined patient cohorts:** all treated; under 65 / 65+; Medicare / Commercial / Medicaid; line 1 only /
    reached line 2+; ever on a combination.
  - **Output:** a scorecard with differences (percentage points for shares, % for volumes), the drivers of the gap
    (HCP decile, payer, line, treatment mix) and the HCPs behind it.
  - **Saved cohorts:** HCP lists can be saved as named cohorts (stored in `Data/cohorts/cohorts.json`).

## 11. Assumptions and open decisions

These are implemented as described but **need business confirmation**. All can be changed in `ciq/config.py` or
`ciq/build/io_cohort.py`.

1. **"All drugs" for lines of therapy** is taken to mean all **anticancer** drugs. Supportive care (antiemetics,
   G-CSF, saline, bone agents) is excluded; including it would advance lines almost every visit.
2. **Restart after a 120+ day gap** of the same IO brand is labelled **Initiated** (a re-initiation), because the
   agreed SOB categories have no "Restart".
3. **NRx** includes the brand's first claim in a new line and IV→SC switches (definition in §6).
4. **SC back to IV** is labelled Continuing.
5. **Market for share** remains `LUNG_SYSTEMIC` only; `OTHER_ONCOLOGY` affects lines of therapy but not share.

## 12. Data limitations

These appear in every analysis's caveats and should be stated when sharing results.

- **Only 7 months of data** (Jan–Jul 2026).
  - Look-backs are truncated: "24 months before first dose" sees at most 7.
  - Yearly trends can't be measured.
  - Few patients can be followed through several lines, so later lines and progression are understated.
  - Early "new" patients may be continuing patients (left censoring).
- **Line 1 is the first observed line**, not necessarily the patient's first-ever line.
- **No gender; no payer names; age missing for 14% of patients.**
- **Paid pharmacy claims only.** Rejected and reversed claims are not in the extract.
- **Specialty is not in the data.** HCP types and the "specialist consult" stage are proxies.
- **The v2 extract is a different patient population from the v1 master.** Figures are not comparable across
  versions.
