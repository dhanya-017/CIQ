"""Step 4 - Drug identification: map every drug line in stg_claims to a molecule / product / class / therapy group.

Uses the drug dictionary ciq/reference/drug_reference.csv  ->  ref_drug, ref_drug_code_map
    1. HCPCS code match (e.g. J9271 -> pembrolizumab / KEYTRUDA)
    2. else the first name regex (in CSV order) matching the product name
    3. else an unmapped J9xxx chemo code -> OTHER ANTINEOPLASTIC (so no chemo line is silently lost)
    topical products (creams) are never matched
To add or fix a drug: edit the CSV and rebuild (see docs/DEVELOPER.md).
"""
from __future__ import annotations

import re

import pandas as pd

from ciq import config


def load_drug_reference() -> pd.DataFrame:
    ref = pd.read_csv(config.DRUG_REFERENCE_CSV, dtype=str).fillna("")
    ref["default_days_supply"] = ref["default_days_supply"].astype(int)
    return ref


def build_drug_map(con, ref: pd.DataFrame) -> pd.DataFrame:
    """One row per distinct drug-line signature (code_system, code, prod_name, ndc_desc) that maps to a drug."""
    sigs = con.sql("""
        select distinct
               case when source_table = 'RX' then 'NDC' else code_system end as code_system,
               case when source_table = 'RX' then null else code end as code,
               code_short_desc, prod_name, ndc_desc
        from stg_claims
        where is_line_anchor = 'Y' and code_type in ('PX', 'RX')
          and (source_table = 'RX' or code_system = 'HCPCS' or prod_name is not null)
    """).df()

    code_to_row = {}
    for _, r in ref.iterrows():
        for code in filter(None, r["hcpcs_codes"].split(";")):
            code_to_row[code.strip()] = r
    patterns = [(re.compile(r["name_regex"], re.I), r) for _, r in ref.iterrows() if r["name_regex"]]

    out = []
    for s in sigs.itertuples(index=False):
        # pandas string columns can hold NaN (a truthy float) for nulls; normalise to None
        s = s._replace(**{f: v if isinstance(v, str) else None
                          for f, v in (("code", s.code), ("code_short_desc", s.code_short_desc))})
        hit = code_to_row.get(s.code) if s.code else None
        match_type = "HCPCS" if hit is not None else None
        if hit is None:
            text = " | ".join(x for x in (s.prod_name, s.ndc_desc) if isinstance(x, str))
            if text and not re.search(r"\bcream\b", text, re.I):
                for pat, r in patterns:
                    if pat.search(text):
                        hit, match_type = r, "NAME"
                        break
        if hit is None and s.code and s.code.startswith("J9") and s.code != "J9999":
            out.append(dict(code_system=s.code_system, code=s.code, prod_name=s.prod_name, ndc_desc=s.ndc_desc,
                            molecule=(s.code_short_desc or s.code).lower(), product="OTHER ANTINEOPLASTIC",
                            drug_class="OTHER ANTINEOPLASTIC", therapy_group="OTHER_ONCOLOGY",
                            default_days_supply=21, match_type="J9_FALLBACK"))
            continue
        if hit is None:
            continue
        out.append(dict(code_system=s.code_system, code=s.code, prod_name=s.prod_name, ndc_desc=s.ndc_desc,
                        molecule=hit["molecule"], product=hit["product"], drug_class=hit["drug_class"],
                        therapy_group=hit["therapy_group"], default_days_supply=int(hit["default_days_supply"]),
                        match_type=match_type))
    return pd.DataFrame(out)


def run(ctx) -> None:
    ref = load_drug_reference()
    ctx.con.register("ref_df", ref)
    ctx.con.execute("create table ref_drug as select * from ref_df")
    drug_map = build_drug_map(ctx.con, ref)
    ctx.con.register("drug_map_df", drug_map)
    ctx.con.execute("create table ref_drug_code_map as select * from drug_map_df")
    ctx.log(f"  mapped {len(drug_map):,} drug signatures -> {drug_map['molecule'].nunique()} molecules")
