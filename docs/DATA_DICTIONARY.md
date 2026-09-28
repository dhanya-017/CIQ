# CIQ data dictionary

What every input file and warehouse table contains. Row counts are from the current build (v2 input,
`master_nsclc.csv`, data 2026-01-01 to 2026-07-31). How the values are derived is explained in
[BUSINESS_RULES.md](BUSINESS_RULES.md).

## 1. Input files

All input files are **patient-level and local only** (`Data/`, git-ignored). They come from the secure data share.

### `Data/real_data/master_nsclc.csv` (required, the build input)

5,778,852 rows × 15 columns, produced upstream by `NSCLC_Master_Data.ipynb` from the raw MX and RX extracts. The
file has quirks from that process; the build cleans them all (see "Cleaning").

| Column | Meaning | Quirks in the file |
|---|---|---|
| `claim_id` | Claim identifier (medical: `dx_claim_id`; pharmacy: `claim_id`) | 20-digit IDs, stored as text |
| `patient_id` | De-identified patient ID | — |
| `svc_dt` | Service date | Text `YYYY-MM-DD HH:MM:SS.mmm` |
| `diagnosis_code` | ICD-10-CM code from one diagnosis slot (medical slots 1–5; pharmacy has one) | Medical lines repeat once per slot |
| `code` | `RX` / `MX_RX`: NDC; `MX_PX`: CPT/HCPCS procedure code | NDCs stored as floats (`31722020410.0`) with leading zeros lost; missing procedure codes stored as the text `NAN` |
| `code_description` | `RX`: brand/product name; `MX_RX`: product name; `MX_PX`: only for the 6 IO J-codes | — |
| `renderring_provider_id` | Pharmacy prescriber (typo'd name from upstream) | Float-formatted (`10567886.0`) |
| `rendering_provider_id` | Medical rendering provider | Float-formatted |
| `referring_provider_id` | Medical referring provider | Float-formatted |
| `plan_id` | Payer plan ID | — |
| `plan_name` | Payment type (Commercial, Medicare, Medicare Advantage, Medicare A/B, Medicare Part D, Mgd/FFS Medicaid, HIX, Assistance, Unknown) | Not the payer's name |
| `days_supply` | Pharmacy days supply | `30.0` |
| `quantity` | Pharmacy quantity / medical units of service | `5.0` |
| `refill_code` | Pharmacy refill number (0 = new prescription) | `0.0` |
| `source` | `RX` pharmacy, `MX_RX` medical line with NDC, `MX_PX` medical procedure | — |

### `Data/real_data/mx_nsclc.csv` (recommended, enrichment)

The raw medical extract (about 62 columns). The build reads only:
- `patient_id`, `pat_age_yr_nbr`: **age** per patient
- `dx_claim_id`, `provider_billing_id`: **billing provider** per claim, used when a line has no rendering provider

### `Data/master/claims_master.parquet` (optional)

- The **v1** input: 77 columns in long format, one row per claim-line code, with descriptions.
- Used as a fallback for code descriptions and age, and as the input when `CIQ_SOURCE=claims_master`.

### `ciq/reference/drug_reference.csv` (in git)

The curated drug dictionary. Columns:
- `molecule`, `product`, `drug_class`
- `therapy_group`: `LUNG_SYSTEMIC` / `OTHER_ONCOLOGY` / `SUPPORTIVE`
- `default_days_supply`: cycle length used for infused drugs
- `hcpcs_codes`: `;`-separated
- `name_regex`: matched against product names; the first match wins

### `ciq/reference/icd10cm.csv` (in git)

The public CMS ICD-10-CM FY2027 code list (98,403 codes): `code` (dotted), `is_billable`, `short_desc`. Regenerate
it with `python scripts/fetch_icd10cm.py [year]`.

## 2. Warehouse tables (`Data/warehouse/ciq.duckdb`)

### Source and reference

**`src_master_nsclc`**: the complete input extract, cleaned. 5,778,852 rows.

| Column | Meaning |
|---|---|
| `row_id` | Row number in the file (preserves the upstream diagnosis-slot order) |
| `source`, `claim_id`, `patient_id` | As in the file (IDs without `.0`) |
| `service_date` | `svc_dt` as a DATE |
| `diagnosis_code` | ICD-10-CM code (upper-case; `NAN` → null) |
| `code` | NDC-11 for `RX` / `MX_RX`; CPT/HCPCS for `MX_PX` (`NAN` → null) |
| `code_description` | As in the file |
| `rendering_provider_id` | Rendering provider, merged with the `renderring_provider_id` typo column |
| `referring_provider_id`, `plan_id`, `plan_name` | Cleaned |
| `days_supply`, `quantity` (double), `refill_code` (integer) | Numeric |
| `svc_dt_raw`, `code_raw`, `rendering_provider_id_raw`, `renderring_provider_id_raw` | Original values before cleaning |

**Other reference tables:**

| Table | Rows | Contents |
|---|---|---|
| `ref_drug` | 105 | `drug_reference.csv` as loaded |
| `ref_drug_code_map` | 688 | Every drug signature seen in the claims → `molecule`, `product`, `drug_class`, `therapy_group`, `default_days_supply`, `match_type` (`HCPCS` / `NAME` / `J9_FALLBACK`). Keys: `code_system`, `code`, `prod_name`, `ndc_desc` |
| `ref_icd10cm` | 98,403 | `code`, `is_billable`, `short_desc` |
| `ref_code_desc_v1` | 10,799 | Descriptions from the v1 master: `code`, `code_type` (DX/PX), `code_short_desc` |
| `ref_patient_age` | 9,993 | `patient_id`, `age`, `age_source` (`mx_nsclc.csv` or `claims_master (v1)`) |
| `ref_claim_billing` | 255,206 | `claim_id`, `provider_billing_id` |
| `ref_data_coverage` | 1 | `data_start`, `data_end`, `n_patients`, `n_source_rows`, `built_at` |
| `ref_build_params` | 14 | `param`, `value`: the rules and source used by this build (gap days, regimen window, therapy groups, cohort pattern, washout, source file, row counts) |

### Facts

**`fact_drug_event`**: one row per patient, day and molecule. 20,668 rows.

| Column | Meaning |
|---|---|
| `patient_id`, `service_date` | Who and when |
| `molecule`, `product`, `drug_class`, `therapy_group` | From the drug dictionary (biosimilars/SC map to the reference molecule and product) |
| `product_form` | Product form: e.g. `KEYTRUDA QLEX` vs `KEYTRUDA`; equals `product` for non-IO drugs |
| `source` | `MX` or `RX` |
| `claim_id`, `code` | A representative claim and code (HCPCS or NDC) |
| `hcp_id` | Rendering provider (medical, else billing) or prescriber (pharmacy) |
| `payer_plan_id`, `payment_type_raw`, `payment_type` | Plan, the claim's own payment type, and the payment type used (patient's modal type if unknown) |
| `days_supply` | Max of dispensed days supply and the drug's standard cycle length |
| `units`, `refill_code` | Units / quantity; lowest refill code that day (pharmacy) |
| `billed_amt`, `place_of_svc_cd`, `rx_sob` | Not available in the v2 input (null) |
| `n_claim_lines` | Claim lines combined into this event |

**`fact_dx_event`**: one row per patient, day and diagnosis code. 799,745 rows.

| Column | Meaning |
|---|---|
| `dx_code`, `dx_desc` | ICD-10-CM code and short description |
| `dx_category` | First 3 characters (e.g. `C34`) |
| `best_position` | Best (lowest) diagnosis slot the code appeared in |
| `from_rx` | Seen on a pharmacy claim |
| `hcp_id`, `n_mentions` | Provider; number of claim-line mentions that day |

**`fact_px_event`**: one row per patient, day and procedure code. 751,804 rows.
- `px_code`, `code_system` (CPT / HCPCS), `px_desc` (partial)
- `hcp_id`, `n_lines`
- `claim_type`, `billed_amt`: null in v2

**`fact_io_claim`**: the final IO cohort. 2,810 rows. One row per patient, day and IO brand family.

| Column | Meaning |
|---|---|
| `service_date`, `week_start` (Monday), `month_start` | Date and reporting periods |
| `brand_family` | `KEYTRUDA`, `OPDIVO` or `TECENTRIQ` |
| `product_form`, `is_sc` | IV or SC form (e.g. `KEYTRUDA QLEX`, true) |
| `molecule`, `source`, `claim_id`, `code`, `hcp_id`, `payer_plan_id`, `payment_type`, `days_supply`, `units`, `refill_code` | From `fact_drug_event` |
| `line_number`, `line_start_date`, `line_regimen`, `line_combination_type` | The line of therapy the claim falls in |
| `prev_io_brand`, `prev_io_form`, `prev_io_date`, `days_since_prev_io` | The patient's previous IO-brand claim |
| `sob` | Initiated / Switched from Competitor / `<Brand> to <Brand SC>` / Continuing |
| `is_reinitiation` | Same brand resumed after a gap of 120+ days |
| `in_washout` | Claim in the first 90 days of data |
| `trx`, `nrx`, `rrx`, `nbrx` | 0/1 claim-type flags (trx always 1) |
| `claim_type` | NBRx / NRx / RRx |
| `age`, `age_group` | From `dim_patient` |

### Dimensions

**`dim_patient`**: 9,969 cohort patients.

| Column | Meaning |
|---|---|
| `age`, `age_group` | Age (1–110, else null) and group (`<18`, `20-29` … `80-89`, `85+`, `Unknown`) |
| `gender` | Always null (not in the data) |
| `payment_type` | Most frequent medical payment type |
| `first_claim_date`, `last_claim_date` | Observed span |
| `first_indication_dx_date`, `n_indication_dx_days` | First C34 diagnosis; days with a C34 diagnosis |
| `first_lung_tx_date`, `last_lung_tx_date`, `is_treated` | Lung systemic therapy span |
| `has_mx`, `has_rx`, `n_claim_lines` | Claim coverage |
| `clean_start` | First therapy ≥ 90 days after data start (likely a true first line) |
| `max_line` | Highest line of therapy reached |

**`dim_hcp`**: 2,966 HCPs.
- `hcp_id`
- `n_drug_events`, `n_lung_tx_events`
- `n_patients`, `n_lung_tx_patients`
- `first_event_date`, `last_event_date`

### Lines of therapy

**`lot_line`**: one row per patient and line. 2,825 rows.

| Column | Meaning |
|---|---|
| `line_number` | 1 = first line observed |
| `start_date`, `end_date`, `last_admin_date`, `duration_days` | Line span (end = last administration + days supply − 1, capped at the next line and data end) |
| `regimen`, `regimen_molecules`, `regimen_classes` | Products / molecules / classes defining the line (from the regimen window) |
| `n_drugs`, `combination_type` | Number of products; Monotherapy / Doublet / Triplet / Quadruplet+ |
| `n_admin_days` | Administration days in the line |
| `regimen_window_end` | End of the 28-day / 2-cycle regimen window |
| `line_start_reason` | "First therapy observed", "New drug after the regimen window: …", "Gap of N days …" |
| `is_retreatment` | Line started by a 120+ day gap with the same drugs |
| `is_last_line`, `is_ongoing_at_data_end` | Latest line; still active within 30 days of data end |

**`lot_line_drug`**: one row per patient, line and molecule. 5,300 rows.
- `molecule`, `product`, `drug_class`
- `in_initial_regimen`
- `first_date`, `last_date`, `n_admin_days`

## 3. App data (`Data/`, runtime)

| Path | Contents |
|---|---|
| `Data/conversations/<id>.json` | Saved chat sessions: `api_messages` (model history), `transcript` (UI turns), `pending` (open clarifying question). Contains query results; treat as patient-derived data |
| `Data/cohorts/cohorts.json` | Saved HCP cohorts (name, definition, HCP IDs) |
| `Data/warehouse/backup/` | Copies of earlier warehouses (e.g. the v1 build) |
