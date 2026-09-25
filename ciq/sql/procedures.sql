-- =============================================================================
-- CIQ stored functions (DuckDB table macros)
-- Each fn_* answers one analytic question and is exposed to the agent as a tool.
-- Product arguments are brand/product names from fact_drug_event.product
-- (case-insensitive), e.g. 'KEYTRUDA', 'OPDIVO', 'CARBOPLATIN'.
-- Day arguments are integers. Periods are relative to each patient's index date
-- (= first claim of the product in the data).
-- =============================================================================

-- ---------- scalar helpers ----------------------------------------------------
create or replace macro data_start() as (select data_start from ref_data_coverage);
create or replace macro data_end()   as (select data_end   from ref_data_coverage);
create or replace macro shift_days(d, n) as (cast(d as date) + cast(n as integer));

-- ---------- catalog / cohort --------------------------------------------------
-- All products in the data with patient and event counts.
create or replace macro fn_product_catalog(p_therapy_group := null) as table
select product,
       string_agg(distinct molecule, ', ') as molecules,
       any_value(drug_class) as drug_class,
       any_value(therapy_group) as therapy_group,
       count(distinct patient_id) as patients,
       count(*) as admin_events,
       min(service_date) as first_date,
       max(service_date) as last_date
from fact_drug_event
where p_therapy_group is null or therapy_group = upper(p_therapy_group)
group by product
order by patients desc;

-- Index date (first product claim) per patient.
create or replace macro fn_product_index(p_product) as table
select patient_id, min(service_date) as index_date, count(*) as n_product_events
from fact_drug_event
where product = upper(p_product)
group by patient_id;

-- Patient-level cohort for a product with demographics and follow-up.
create or replace macro fn_cohort_patients(p_product) as table
select i.patient_id, i.index_date, i.n_product_events,
       p.age, p.age_group, p.gender, p.payment_type,
       i.index_date - data_start() as lookback_days_available,
       data_end() - i.index_date   as followup_days_available,
       p.first_indication_dx_date, p.first_lung_tx_date, p.clean_start, p.max_line
from fn_product_index(p_product) i
join dim_patient p using (patient_id);

-- ---------- 1. market overview ------------------------------------------------
-- Incidence & prevalence of the indication (C34) by period.
--   prevalent  = patients with an indication dx in the period
--   incident   = patients whose first indication dx falls in the period, with at
--                least p_washout_days of prior data (earlier periods flagged invalid)
--   treated    = patients with a lung systemic therapy in the period
--   new_treated= patients whose first lung therapy falls in the period (same washout)
create or replace macro fn_incidence_prevalence(p_grain := 'month', p_washout_days := 90) as table
with dx as (
    select patient_id, service_date from fact_dx_event where dx_code like 'C34%'
), first_dx as (
    select patient_id, min(service_date) as first_dt from dx group by 1
), tx as (
    select patient_id, service_date from fact_drug_event where therapy_group = 'LUNG_SYSTEMIC'
), first_tx as (
    select patient_id, min(service_date) as first_dt from tx group by 1
), periods as (
    select distinct date_trunc(p_grain, service_date) as period_start from dx
)
select p.period_start,
       case lower(p_grain)
            when 'year' then strftime(p.period_start, '%Y')
            when 'quarter' then strftime(p.period_start, '%Y') || '-Q' || cast(quarter(p.period_start) as varchar)
            else strftime(p.period_start, '%Y-%m') end as period,
       (select count(distinct patient_id) from dx where date_trunc(p_grain, dx.service_date) = p.period_start) as prevalent_patients,
       (select count(*) from first_dx f where date_trunc(p_grain, f.first_dt) = p.period_start
            and f.first_dt >= shift_days(data_start(), p_washout_days)) as incident_patients,
       (select count(distinct patient_id) from tx where date_trunc(p_grain, tx.service_date) = p.period_start) as treated_patients,
       (select count(*) from first_tx f where date_trunc(p_grain, f.first_dt) = p.period_start
            and f.first_dt >= shift_days(data_start(), p_washout_days)) as new_treated_patients,
       p.period_start >= shift_days(data_start(), p_washout_days) as incidence_valid
from periods p
order by p.period_start;

-- Product uptake over time: product patients, new-to-product patients, share of treated market.
create or replace macro fn_product_trend(p_product, p_grain := 'month') as table
with mk as (
    select date_trunc(p_grain, service_date) as period_start, count(distinct patient_id) as market_patients
    from fact_drug_event where therapy_group = 'LUNG_SYSTEMIC' group by 1
), pr as (
    select date_trunc(p_grain, service_date) as period_start,
           count(distinct patient_id) as product_patients, count(*) as product_events
    from fact_drug_event where product = upper(p_product) group by 1
), nw as (
    select date_trunc(p_grain, index_date) as period_start, count(*) as new_patients
    from fn_product_index(p_product) group by 1
)
select mk.period_start,
       case lower(p_grain) when 'year' then strftime(mk.period_start, '%Y')
            when 'quarter' then strftime(mk.period_start, '%Y') || '-Q' || cast(quarter(mk.period_start) as varchar)
            else strftime(mk.period_start, '%Y-%m') end as period,
       mk.market_patients,
       coalesce(pr.product_patients, 0) as product_patients,
       coalesce(pr.product_events, 0) as product_events,
       coalesce(nw.new_patients, 0) as new_patients,
       round(coalesce(pr.product_patients, 0) / mk.market_patients, 4) as patient_share
from mk left join pr using (period_start) left join nw using (period_start)
order by mk.period_start;

-- Market share of each product among treated lung patients (patient-based).
create or replace macro fn_market_share(p_top_n := 15, p_therapy_group := 'LUNG_SYSTEMIC') as table
with t as (select count(distinct patient_id) as n from fact_drug_event where therapy_group = upper(p_therapy_group))
select product, any_value(drug_class) as drug_class,
       count(distinct patient_id) as patients, count(*) as admin_events,
       round(count(distinct patient_id) / (select n from t), 4) as patient_share
from fact_drug_event
where therapy_group = upper(p_therapy_group)
group by product
order by patients desc
limit p_top_n;

-- Age distribution of a product cohort (age at data extract; gender not available).
create or replace macro fn_age_distribution(p_product) as table
with c as (select * from fn_cohort_patients(p_product))
select age_group, count(*) as patients,
       round(count(*) / (select count(*) from c), 4) as pct_patients,
       round(avg(age), 1) as mean_age
from c group by age_group order by min(coalesce(age, 999));

-- ---------- 2. patient level --------------------------------------------------
-- Treatments received in the p_lookback_days before the first product claim.
-- p_level: 'drug_class' | 'product' | 'molecule'
create or replace macro fn_pre_index_treatments(p_product, p_lookback_days := 730, p_level := 'drug_class',
                                                p_include_supportive := false) as table
with idx as (select * from fn_product_index(p_product)),
ev as (
    select i.patient_id,
           case lower(p_level) when 'product' then f.product when 'molecule' then f.molecule
                else f.drug_class end as treatment,
           f.therapy_group
    from idx i
    join fact_drug_event f
      on f.patient_id = i.patient_id
     and f.service_date < i.index_date
     and f.service_date >= shift_days(i.index_date, -p_lookback_days)
    where f.product <> upper(p_product)
      and (p_include_supportive or f.therapy_group <> 'SUPPORTIVE')
)
select treatment, any_value(therapy_group) as therapy_group,
       count(distinct patient_id) as patients,
       (select count(*) from idx) as cohort_patients,
       round(count(distinct patient_id) / (select count(*) from idx), 4) as pct_of_cohort
from ev group by treatment order by patients desc;

-- Source of business at event level.
--   Continuing            product used within p_grace_days before
--   Restart               product used in lookback but gap > grace, no other therapy in between
--   Restart + Switch      as Restart, but another lung therapy was used in between
--   New to Brand + Add-on no prior product; another lung therapy active before AND continuing after
--   New to Brand + Switch no prior product; another lung therapy in lookback, not continued
--   New to Market         no lung therapy at all in lookback
create or replace macro fn_sob_events(p_product, p_lookback_days := 180, p_grace_days := 90) as table
with b as (
    select distinct patient_id, service_date from fact_drug_event where product = upper(p_product)
), o as (
    select distinct patient_id, service_date from fact_drug_event
    where therapy_group = 'LUNG_SYSTEMIC' and product <> upper(p_product)
), b2 as (
    select *, lag(service_date) over (partition by patient_id order by service_date) as prev_date
    from b
), x as (
    select b2.patient_id, b2.service_date,
           case when b2.prev_date >= shift_days(b2.service_date, -p_lookback_days) then b2.prev_date end as prev_product_date,
           exists (select 1 from o where o.patient_id = b2.patient_id and o.service_date < b2.service_date
                     and o.service_date >= shift_days(b2.service_date, -p_lookback_days)) as other_in_lookback,
           exists (select 1 from o where o.patient_id = b2.patient_id and o.service_date < b2.service_date
                     and o.service_date >= shift_days(b2.service_date, -p_grace_days)) as other_recent,
           exists (select 1 from o where o.patient_id = b2.patient_id and o.service_date >= b2.service_date
                     and o.service_date <= shift_days(b2.service_date, p_grace_days)) as other_after,
           exists (select 1 from o where o.patient_id = b2.patient_id and o.service_date < b2.service_date
                     and o.service_date > b2.prev_date) as other_since_prev
    from b2
)
select patient_id, service_date, prev_product_date,
       case when prev_product_date is not null and service_date - prev_product_date <= p_grace_days then 'Continuing'
            when prev_product_date is not null and other_since_prev then 'Restart + Switch'
            when prev_product_date is not null then 'Restart'
            when other_recent and other_after then 'New to Brand + Add-on'
            when other_in_lookback then 'New to Brand + Switch'
            else 'New to Market' end as sob_category,
       shift_days(service_date, -p_lookback_days) >= data_start() as has_full_lookback
from x;

-- Source of business aggregated. p_level: 'event' (every administration) | 'patient' (first in data)
create or replace macro fn_source_of_business(p_product, p_lookback_days := 180, p_grace_days := 90,
                                              p_level := 'event', p_full_lookback_only := false) as table
with e as (
    select *, row_number() over (partition by patient_id order by service_date) as rn
    from fn_sob_events(p_product, p_lookback_days := p_lookback_days, p_grace_days := p_grace_days)
), f as (
    select * from e
    where (lower(p_level) <> 'patient' or rn = 1)
      and ((not p_full_lookback_only) or has_full_lookback)
)
select sob_category, count(*) as events, count(distinct patient_id) as patients,
       round(count(*) / (select count(*) from f), 4) as pct
from f group by sob_category order by events desc;

-- Payment type (channel) mix of product claims. p_level: 'event' | 'patient'
create or replace macro fn_payment_type_mix(p_product, p_level := 'event') as table
with f as (select * from fact_drug_event where product = upper(p_product)),
g as (
    select coalesce(payment_type, 'Unknown') as payment_type, count(*) as events, count(distinct patient_id) as patients
    from f group by 1
)
select payment_type, events, patients,
       round(case when lower(p_level) = 'patient' then patients / (select count(distinct patient_id) from f)
                  else events / (select count(*) from f) end, 4) as pct
from g order by events desc;

-- Top payers (plan ids) for a product by claim volume.
create or replace macro fn_top_payers(p_product, p_top_n := 10) as table
with f as (select * from fact_drug_event where product = upper(p_product) and payer_plan_id is not null)
select payer_plan_id,
       mode(payment_type) as payment_type,
       count(*) as events, count(distinct patient_id) as patients,
       round(count(*) / (select count(*) from f), 4) as pct_events
from f group by payer_plan_id order by events desc limit p_top_n;

-- ---------- 3. therapy / lines ------------------------------------------------
-- Lines, optionally restricted to lines containing a product (p_product null = whole market).
create or replace macro fn_lines(p_product := null) as table
select l.*
from lot_line l
where p_product is null
   or exists (select 1 from lot_line_drug d where d.patient_id = l.patient_id
              and d.line_number = l.line_number and d.product = upper(p_product));

-- Mono / doublet / triplet split of regimens.
create or replace macro fn_combination_type(p_product := null) as table
with l as (select * from fn_lines(p_product := p_product))
select combination_type, n_drugs, count(*) as lines, count(distinct patient_id) as patients,
       round(count(*) / (select count(*) from l), 4) as pct_lines
from l group by combination_type, n_drugs order by n_drugs;

-- Most common regimens (drug combinations).
create or replace macro fn_regimen_distribution(p_product := null, p_top_n := 15) as table
with l as (select * from fn_lines(p_product := p_product))
select regimen, combination_type, n_drugs, count(*) as lines, count(distinct patient_id) as patients,
       round(count(distinct patient_id) / (select count(distinct patient_id) from l), 4) as pct_patients
from l group by regimen, combination_type, n_drugs order by patients desc limit p_top_n;

-- Patients reaching each line (cohort = product-treated patients, or all treated if null).
create or replace macro fn_line_progression(p_product := null, p_clean_start_only := false) as table
with c as (
    select p.patient_id, p.max_line from dim_patient p
    where p.max_line is not null
      and (p_product is null or p.patient_id in (select patient_id from fn_product_index(p_product)))
      and ((not p_clean_start_only) or p.clean_start)
), lines as (select distinct line_number from lot_line)
select l.line_number,
       count(c.patient_id) filter (where c.max_line >= l.line_number) as patients_reaching,
       count(c.patient_id) filter (where c.max_line = l.line_number) as patients_ending_here,
       round(count(c.patient_id) filter (where c.max_line >= l.line_number) / (select count(*) from c), 4) as pct_of_cohort
from lines l, c
group by l.line_number
having count(c.patient_id) filter (where c.max_line >= l.line_number) > 0
order by l.line_number;

-- Product share of patients in each line of therapy.
create or replace macro fn_product_share_by_line(p_product, p_max_line_bucket := 4) as table
with l as (
    select patient_id, line_number,
           case when line_number >= p_max_line_bucket then cast(p_max_line_bucket as varchar) || '+'
                else cast(line_number as varchar) end as line_bucket
    from lot_line
), d as (
    select distinct patient_id, line_number from lot_line_drug where product = upper(p_product)
)
select l.line_bucket, min(l.line_number) as line_sort,
       count(distinct l.patient_id) as line_patients,
       count(distinct d.patient_id) as product_patients,
       round(count(distinct d.patient_id) / count(distinct l.patient_id), 4) as product_share
from l left join d on d.patient_id = l.patient_id and d.line_number = l.line_number
group by l.line_bucket order by line_sort;

-- Most common regimens in each line bucket (1, 2, 3, 4+).
create or replace macro fn_top_regimens_by_line(p_top_n := 5, p_max_line_bucket := 4, p_product := null) as table
with l as (
    select *, case when line_number >= p_max_line_bucket then cast(p_max_line_bucket as varchar) || '+'
                   else cast(line_number as varchar) end as line_bucket
    from lot_line
    where p_product is null or patient_id in (select patient_id from fn_product_index(p_product))
), g as (
    select line_bucket, min(line_number) as line_sort, regimen, count(distinct patient_id) as patients
    from l group by line_bucket, regimen
), t as (
    select line_bucket, count(distinct patient_id) as line_patients from l group by line_bucket
)
select g.line_bucket, g.line_sort, g.regimen, g.patients, t.line_patients,
       round(g.patients / t.line_patients, 4) as pct_of_line,
       row_number() over (partition by g.line_bucket order by g.patients desc, g.regimen) as rank
from g join t using (line_bucket)
qualify rank <= p_top_n
order by g.line_sort, rank;

-- ---------- 4. diagnosis ------------------------------------------------------
-- Most common dx codes in a window before ('pre') or after ('post') the index date.
-- p_code_level: 'code' (full ICD-10) | 'category' (3-character)
create or replace macro fn_dx_around_index(p_product, p_period := 'pre', p_window_days := 540, p_top_n := 10,
                                           p_code_level := 'code', p_exclude_indication := false) as table
with idx as (select * from fn_product_index(p_product)),
ev as (
    select i.patient_id,
           case when lower(p_code_level) = 'category' then d.dx_category else d.dx_code end as code,
           d.dx_code, d.dx_desc
    from idx i join fact_dx_event d on d.patient_id = i.patient_id
    where ((lower(p_period) = 'pre'  and d.service_date <  i.index_date
                                     and d.service_date >= shift_days(i.index_date, -p_window_days))
        or (lower(p_period) = 'post' and d.service_date >  i.index_date
                                     and d.service_date <= shift_days(i.index_date, p_window_days)))
      and not (p_exclude_indication and d.dx_code like 'C34%')
), observed as (select count(distinct patient_id) as n from ev)
select code,
       case when lower(p_code_level) = 'category' then mode(dx_desc) else any_value(dx_desc) end as description,
       count(distinct patient_id) as patients,
       (select count(*) from idx) as cohort_patients,
       (select n from observed) as observed_patients,
       round(count(distinct patient_id) / (select count(*) from idx), 4) as pct_of_cohort,
       round(count(distinct patient_id) / (select n from observed), 4) as pct_of_observed
from ev group by code order by patients desc limit p_top_n;

-- ---------- 5. adherence ------------------------------------------------------
-- Continuous therapy episodes: a new episode starts when the gap after the
-- previous days-supply coverage exceeds p_grace_days.
-- censored = episode still possibly active at data end (discontinuation not observed).
create or replace macro fn_therapy_episodes(p_product, p_grace_days := 60) as table
with ev as (
    select patient_id, service_date, days_supply,
           shift_days(service_date, days_supply) as covered_until
    from fact_drug_event where product = upper(p_product)
), w as (
    select *, max(covered_until) over (partition by patient_id order by service_date
                                       rows between unbounded preceding and 1 preceding) as prev_cov
    from ev
), f as (
    select *, sum(case when prev_cov is null or service_date > shift_days(prev_cov, p_grace_days) then 1 else 0 end)
                  over (partition by patient_id order by service_date) as episode_number
    from w
)
select patient_id, episode_number,
       min(service_date) as episode_start,
       least(max(covered_until) - 1, data_end()) as episode_end,
       least(max(covered_until) - 1, data_end()) - min(service_date) + 1 as duration_days,
       count(*) as n_admins,
       max(shift_days(covered_until, p_grace_days)) > data_end() as censored
from f group by patient_id, episode_number;

-- ---------- 6. physician level ------------------------------------------------
-- HCP-level market volume (all lung systemic therapies) for the product cohort,
-- split PRE (window before index) and POST (index .. window after), with deciles.
-- p_method: 'hcp_count' (equal HCP counts per decile, decile 1 = top prescribers)
--           'volume'    (each decile holds ~10% of volume, decile 1 = top prescribers)
create or replace macro fn_hcp_market_volume(p_product, p_window_days := 730, p_method := 'hcp_count',
                                             p_include_product := true) as table
with idx as (select * from fn_product_index(p_product)),
ev as (
    select case when f.service_date < i.index_date then 'PRE' else 'POST' end as period,
           f.hcp_id, count(*) as volume, count(distinct f.patient_id) as patients
    from idx i join fact_drug_event f on f.patient_id = i.patient_id
    where f.therapy_group = 'LUNG_SYSTEMIC' and f.hcp_id is not null
      and (p_include_product or f.product <> upper(p_product))
      and f.service_date >= shift_days(i.index_date, -p_window_days)
      and f.service_date <= shift_days(i.index_date, p_window_days)
    group by 1, 2
), r as (
    select *,
           ntile(10) over (partition by period order by volume desc, hcp_id) as decile_hcp_count,
           sum(volume) over (partition by period order by volume desc, hcp_id rows unbounded preceding)
             / sum(volume) over (partition by period) as cum_share
    from ev
)
select period, hcp_id, volume, patients, cum_share,
       case when lower(p_method) = 'volume'
            then greatest(1, least(10, cast(ceil(round(cum_share * 10, 9)) as integer)))
            else decile_hcp_count end as decile
from r;

create or replace macro fn_hcp_market_deciles(p_product, p_window_days := 730, p_method := 'hcp_count',
                                              p_include_product := true) as table
with h as (select * from fn_hcp_market_volume(p_product, p_window_days := p_window_days, p_method := p_method,
                                            p_include_product := p_include_product))
select period, decile, count(*) as hcps, sum(volume) as volume,
       round(sum(volume) / any_value(t.total), 4) as pct_volume
from h join (select period, sum(volume) as total from h group by 1) t using (period)
group by period, decile order by period desc, decile;

-- HCP-level product share of the lung market, deciled on product volume (decile 1 = top).
create or replace macro fn_hcp_product_share(p_product, p_method := 'volume') as table
with h as (
    select hcp_id,
           count(*) filter (where therapy_group = 'LUNG_SYSTEMIC') as market_volume,
           count(*) filter (where product = upper(p_product)) as product_volume,
           count(distinct patient_id) filter (where product = upper(p_product)) as product_patients
    from fact_drug_event where hcp_id is not null group by hcp_id
), p as (
    select *, round(product_volume / nullif(market_volume, 0), 4) as product_share,
           ntile(10) over (order by product_volume desc, hcp_id) as decile_hcp_count,
           sum(product_volume) over (order by product_volume desc, hcp_id rows unbounded preceding)
             / sum(product_volume) over () as cum_share
    from h where product_volume > 0
)
select hcp_id, market_volume, product_volume, product_patients, product_share, cum_share,
       case when lower(p_method) = 'hcp_count' then decile_hcp_count
            else greatest(1, least(10, cast(ceil(round(cum_share * 10, 9)) as integer))) end as decile
from p;

create or replace macro fn_hcp_product_share_deciles(p_product, p_method := 'volume') as table
with h as (select * from fn_hcp_product_share(p_product, p_method := p_method))
select decile, count(*) as hcps,
       sum(product_volume) as product_volume,
       round(sum(product_volume) / (select sum(product_volume) from h), 4) as pct_product_volume,
       sum(market_volume) as market_volume,
       round(sum(product_volume) / nullif(sum(market_volume), 0), 4) as product_share_of_market,
       round(avg(product_share), 4) as avg_hcp_share
from h group by decile order by decile;

-- ---------- 7. IO brand dynamics (fact_io_claim; see ciq/build/io_cohort.py) ------------------------------
-- Source of business per IO brand family by month or week.
-- sob: Initiated | Switched from Competitor | <Brand> to <Brand SC> | Continuing
create or replace macro fn_io_sob(p_brand := null, p_grain := 'month', p_exclude_washout := false) as table
select case when lower(p_grain) = 'week' then week_start else month_start end as period_start,
       case when lower(p_grain) = 'week' then strftime(week_start, '%Y-%m-%d') else strftime(month_start, '%Y-%m') end as period,
       brand_family, sob,
       count(*) as claims,
       count(distinct patient_id) as patients
from fact_io_claim
where (p_brand is null or brand_family = upper(p_brand))
  and (not p_exclude_washout or not in_washout)
group by all
order by period_start, brand_family, sob;

-- TRx / NRx / RRx / NBRx per IO brand family by month or week.
create or replace macro fn_io_claim_types(p_brand := null, p_grain := 'month', p_exclude_washout := false) as table
select case when lower(p_grain) = 'week' then week_start else month_start end as period_start,
       case when lower(p_grain) = 'week' then strftime(week_start, '%Y-%m-%d') else strftime(month_start, '%Y-%m') end as period,
       brand_family,
       sum(trx) as trx, sum(nrx) as nrx, sum(rrx) as rrx, sum(nbrx) as nbrx,
       count(distinct patient_id) as patients,
       count(distinct patient_id) filter (where nbrx = 1) as nbrx_patients
from fact_io_claim
where (p_brand is null or brand_family = upper(p_brand))
  and (not p_exclude_washout or not in_washout)
group by all
order by period_start, brand_family;
