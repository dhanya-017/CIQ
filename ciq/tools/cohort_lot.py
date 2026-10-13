"""Notebook line-of-therapy rules, applied to a cohort after index date + criteria.

Parameters (ciq.config): cycle gap 4d, line gap 90d, new agent advances only after
>= 2 cycles and >= 30 days on the original therapy. Agents come from
ciq/reference/lot_agent_map.csv (from Drug Mapping 2.xlsx).
"""
from __future__ import annotations

import re
from functools import lru_cache

import pandas as pd

from ciq import config
from ciq.tools.base import query, sql_literal

AGENT_RULES = [
    (r"keytruda|pembrolizumab", "pembrolizumab"),
    (r"opdivo|nivolumab", "nivolumab"),
    (r"tecentriq|atezolizumab", "atezolizumab"),
    (r"imfinzi|durvalumab", "durvalumab"),
    (r"bavencio|avelumab", "avelumab"),
    (r"libtayo|cemiplimab", "cemiplimab"),
    (r"yervoy|ipilimumab", "ipilimumab"),
    (r"imjudo|tremelimumab", "tremelimumab"),
    (r"\bnab\b|abraxane|protein.?bound", "nab-paclitaxel"),
    (r"paclitaxel|taxol", "paclitaxel"),
    (r"docetaxel|taxotere", "docetaxel"),
    (r"alimta|pemetrexed|pemfexy|pemrydi|axtle", "pemetrexed"),
    (r"gemcitabine|gemzar|infugem", "gemcitabine"),
    (r"vinorelbine|navelbine", "vinorelbine"),
    (r"etoposide|etopophos|toposar", "etoposide"),
    (r"irinotecan|camptosar", "irinotecan"),
    (r"topotecan|hycamtin", "topotecan"),
    (r"carboplatin|paraplatin", "carboplatin"),
    (r"cisplatin|platinol", "cisplatin"),
    (r"bevacizumab|avastin|mvasi|zirabev|alymsys|vegzelma|jobevne|avzivi", "bevacizumab"),
    (r"ramucirumab|cyramza", "ramucirumab"),
    (r"necitumumab|portrazza", "necitumumab"),
    (r"amivantamab|rybrevant", "amivantamab"),
]


def normalize_code(x) -> str | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    s = re.sub(r"[\s\-]", "", str(x)).upper()
    if s.endswith(".0"):
        s = s[:-2]
    if s.isdigit() and 5 < len(s) <= 11:
        return s.zfill(11)
    return s or None


def standard_agent(name: str) -> str:
    n = str(name).lower()
    for pattern, agent in AGENT_RULES:
        if re.search(pattern, n):
            return agent
    return n.strip()


@lru_cache(maxsize=1)
def load_agent_map() -> pd.DataFrame:
    path = config.LOT_AGENT_MAP_CSV
    if not path.exists():
        raise FileNotFoundError(f"Drug mapping not found: {path}")
    m = pd.read_csv(path, dtype=str).fillna("")
    m["code_key"] = m["code_key"].map(normalize_code)
    if "agent" not in m.columns and "raw_name" in m.columns:
        m["agent"] = m["raw_name"].map(standard_agent)
    m["agent"] = m["agent"].map(lambda a: standard_agent(a) if a else a)
    return m.dropna(subset=["code_key"]).drop_duplicates("code_key")[["code_key", "agent"]]


def build_cycles(admin: pd.DataFrame, cycle_window: int) -> pd.DataFrame:
    dates = admin[["patient_id", "_date"]].drop_duplicates()
    gap = dates.groupby("patient_id")["_date"].diff().dt.days
    dates = dates.copy()
    dates["cycle_num"] = (gap.isna() | (gap > cycle_window)).groupby(dates["patient_id"]).cumsum()
    admin = admin.merge(dates, on=["patient_id", "_date"], how="left")
    cycles = (admin.groupby(["patient_id", "cycle_num"], as_index=False)
              .agg(cycle_start=("_date", "min"), cycle_end=("_date", "max"),
                   agents=("agent", lambda s: frozenset(s))))
    cycles["cycle_regimen"] = cycles["agents"].map(lambda a: " + ".join(sorted(a)))
    return cycles


def assign_lines(cycles: pd.DataFrame, l_disgap: int, min_cycles: int, min_days: int,
                 gap_equal_advances: bool = False) -> pd.DataFrame:
    rows = []
    for pid, g in cycles.sort_values(["patient_id", "cycle_num"]).groupby("patient_id"):
        line, line_start, line_agents, n_in_line, prev_end = 1, None, set(), 0, None
        for r in g.itertuples(index=False):
            agents = set(r.agents)
            if prev_end is None:
                line_start, line_agents = r.cycle_start, set(agents)
            else:
                gap = (r.cycle_start - prev_end).days
                new = agents - line_agents
                dur = (prev_end - line_start).days
                too_long = gap >= l_disgap if gap_equal_advances else gap > l_disgap
                if too_long:
                    advance = True
                elif not new:
                    advance = False
                elif n_in_line >= min_cycles and dur >= min_days:
                    advance = True
                else:
                    advance = False
                if advance:
                    line += 1
                    line_start, line_agents, n_in_line = r.cycle_start, set(agents), 0
                else:
                    line_agents |= agents
            n_in_line += 1
            prev_end = r.cycle_end
            rows.append((pid, r.cycle_num, line, line_start))
    ln = pd.DataFrame(rows, columns=["patient_id", "cycle_num", "line_number", "line_start"])
    return cycles.merge(ln, on=["patient_id", "cycle_num"], how="left")


def default_rules() -> dict:
    return {
        "cycle_window_days": int(config.COHORT_LOT_CYCLE_WINDOW_DAYS),
        "line_gap_days": int(config.COHORT_LOT_GAP_DAYS),
        "min_cycles": int(config.COHORT_LOT_MIN_CYCLES),
        "min_days": int(config.COHORT_LOT_MIN_DAYS),
    }


def parse_rules(raw: dict | None = None) -> dict:
    """Clamp the four notebook LOT parameters to positive integers."""
    out = default_rules()
    if not raw:
        return out
    bounds = {
        "cycle_window_days": (1, 60),
        "line_gap_days": (1, 730),
        "min_cycles": (1, 30),
        "min_days": (1, 730),
    }
    for key, (lo, hi) in bounds.items():
        if raw.get(key) is None or raw.get(key) == "":
            continue
        try:
            v = int(raw[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number") from None
        if v < lo or v > hi:
            raise ValueError(f"{key} must be between {lo} and {hi}")
        out[key] = v
    return out


def lines_from_admin(admin: pd.DataFrame, cycle_window: int | None = None, line_gap: int | None = None,
                     min_cycles: int | None = None, min_days: int | None = None) -> pd.DataFrame:
    if admin.empty:
        return admin
    rules = parse_rules({
        "cycle_window_days": cycle_window,
        "line_gap_days": line_gap,
        "min_cycles": min_cycles,
        "min_days": min_days,
    })
    cycles = build_cycles(admin, rules["cycle_window_days"])
    lined = assign_lines(cycles, rules["line_gap_days"], rules["min_cycles"], rules["min_days"])
    line_reg = (lined.groupby(["patient_id", "line_number"])["agents"]
                .agg(lambda s: " + ".join(sorted(set().union(*s))))
                .rename("line_regimen").reset_index())
    return lined.merge(line_reg, on=["patient_id", "line_number"], how="left")


DOT_LINES = list(range(1, 11))


def days_on_therapy_by_line(lined: pd.DataFrame, lines: list[int] | None = None) -> list[dict]:
    """Per-patient line duration: last cycle_end minus first cycle_start (notebook `dot_days`)."""
    lines = list(lines or DOT_LINES)
    empty = {"n": 0, "mean": None, "median": None, "p25": None, "p75": None}
    if lined.empty:
        return [{"line": ln, **empty} for ln in lines]
    span = (lined.groupby(["patient_id", "line_number"], as_index=False)
            .agg(line_start=("cycle_start", "min"), line_end=("cycle_end", "max")))
    span["dot_days"] = (span["line_end"] - span["line_start"]).dt.days
    out = []
    for ln in lines:
        s = span.loc[span["line_number"] == ln, "dot_days"]
        n = int(s.shape[0])
        if n == 0:
            out.append({"line": ln, **empty})
            continue
        out.append({
            "line": int(ln),
            "n": n,
            "mean": float(s.mean()),
            "median": float(s.median()),
            "p25": float(s.quantile(0.25)),
            "p75": float(s.quantile(0.75)),
        })
    return out


MOLECULE_SHARE_LINES = (1, 2, 3)
MOLECULE_SHARE_TOP_N = 12


def molecule_share_by_line(lined: pd.DataFrame, lines: tuple[int, ...] = MOLECULE_SHARE_LINES,
                           top_n: int = MOLECULE_SHARE_TOP_N) -> dict:
    """% of patients on each line whose regimen includes the molecule (notebook `share` / `pct_of_line`)."""
    empty = {"lines": [{"line": ln, "n": 0} for ln in lines], "drugs": [], "share": []}
    if lined.empty:
        return empty
    pl = lined.drop_duplicates(["patient_id", "line_number"]).copy()
    pl = pl[pl["line_number"].isin(lines)]
    if pl.empty:
        return empty
    denom = pl.groupby("line_number")["patient_id"].nunique()
    pl["lot_drug"] = pl["line_regimen"].fillna("").map(
        lambda s: [p.strip() for p in str(s).split(" + ") if p.strip()])
    expl = pl.explode("lot_drug")
    expl = expl[expl["lot_drug"].notna() & (expl["lot_drug"] != "")]
    if expl.empty:
        return empty
    counts = (expl.groupby(["lot_drug", "line_number"])["patient_id"].nunique()
              .rename("n_patients").reset_index())
    top = (counts.groupby("lot_drug")["n_patients"].sum().nlargest(top_n).index.tolist())
    share = []
    for drug in top:
        for ln in lines:
            n_line = int(denom.get(ln, 0))
            hit = counts[(counts["lot_drug"] == drug) & (counts["line_number"] == ln)]
            n_pts = int(hit["n_patients"].iloc[0]) if len(hit) else 0
            share.append({
                "drug": str(drug),
                "line": int(ln),
                "n_patients": n_pts,
                "pct_of_line": (100.0 * n_pts / n_line) if n_line else 0.0,
            })
    return {
        "lines": [{"line": int(ln), "n": int(denom.get(ln, 0))} for ln in lines],
        "drugs": [str(d) for d in top],
        "share": share,
    }


FLOW_KEEP = {"pembrolizumab", "nivolumab", "atezolizumab"}
FLOW_SIDE = "either"
FLOW_MIN = 1


def regimen_has_keep(reg, keep: set[str] | None = None) -> bool:
    """True if the regimen contains at least one KEEP agent (alone or combined)."""
    keep = FLOW_KEEP if keep is None else keep
    if reg is None or (isinstance(reg, float) and pd.isna(reg)):
        return False
    ag = {a.strip().lower() for a in str(reg).split(" + ") if a.strip()}
    return bool(ag & keep)


def l1_l2_flow(lined: pd.DataFrame, side: str = FLOW_SIDE, min_flow: int = FLOW_MIN) -> dict:
    """Patient counts of 1L→2L regimen pairs that involve a KEEP IO agent."""
    empty = {"n_patients": 0, "n_transitions": 0, "side": side,
             "keep": sorted(FLOW_KEEP), "flows": []}
    if lined is None or lined.empty or "line_regimen" not in lined.columns:
        return empty
    pl = lined.drop_duplicates(["patient_id", "line_number"]).copy()
    pl["line_number"] = pl["line_number"].astype(int)
    w = pl.pivot(index="patient_id", columns="line_number", values="line_regimen")
    if 1 not in w.columns or 2 not in w.columns:
        return empty
    w = w.rename(columns={1: "L1", 2: "L2"}).dropna(subset=["L1", "L2"])
    if w.empty:
        return empty
    ok1, ok2 = w["L1"].map(regimen_has_keep), w["L2"].map(regimen_has_keep)
    mask = {"both": ok1 & ok2, "L1": ok1, "L2": ok2, "either": ok1 | ok2}.get(side, ok1 | ok2)
    w = w.loc[mask]
    if w.empty:
        return empty
    f12 = (w.groupby(["L1", "L2"]).size().rename("n").reset_index())
    f12 = f12[f12["n"] >= min_flow].sort_values("n", ascending=False)
    if f12.empty:
        return empty
    return {
        "n_patients": int(f12["n"].sum()),
        "n_transitions": int(len(f12)),
        "side": side,
        "keep": sorted(FLOW_KEEP),
        "flows": [{"L1": str(r.L1), "L2": str(r.L2), "n": int(r.n)} for r in f12.itertuples()],
    }


def _code_key_sql(col: str) -> str:
    cleaned = f"regexp_replace(upper(trim(cast({col} as varchar))), '[\\s\\-]', '', 'g')"
    return f"""
        case
          when regexp_full_match({cleaned}, '^[0-9]{{6,11}}$') then lpad({cleaned}, 11, '0')
          else {cleaned}
        end
    """


_MAPPED_ADMIN: dict[tuple, pd.DataFrame] = {}


def mapped_admin(pred: str, index_sql: str, window_end=None) -> pd.DataFrame:
    """Drug claims on/after index, mapped to agents. Cached per cohort SQL."""
    key = (pred, index_sql, None if window_end is None else str(window_end))
    hit = _MAPPED_ADMIN.get(key)
    if hit is not None:
        return hit.copy()
    mapping = load_agent_map()
    keys = ", ".join("'" + k.replace("'", "''") + "'" for k in mapping["code_key"] if k)
    key_sql = _code_key_sql("s.code")
    admin = query(f"""
        with idx as ({index_sql}),
        cohort as (select patient_id from dim_patient where {pred}),
        coded as (
          select s.patient_id, s.service_date as _date, {key_sql} as code_key
          from src_master_nsclc s
          join cohort c using (patient_id)
          join idx i using (patient_id)
          where s.code is not null and s.service_date >= i.index_date
            {("and s.service_date <= " + sql_literal(window_end)) if window_end else ""}
        )
        select patient_id, _date, code_key from coded where code_key in ({keys})
    """)
    if admin.empty:
        _MAPPED_ADMIN[key] = admin
        return admin.copy()
    admin = admin.merge(mapping, on="code_key", how="inner")
    if admin.empty:
        _MAPPED_ADMIN[key] = admin
        return admin.copy()
    admin["_date"] = pd.to_datetime(admin["_date"])
    admin["patient_id"] = admin["patient_id"].astype(str)
    admin = admin.drop_duplicates(["patient_id", "_date", "agent"])
    _MAPPED_ADMIN[key] = admin
    return admin.copy()


def for_cohort(pred: str | None, index_sql: str, window_end=None, rules: dict | None = None) -> dict:
    """LOT from mapped drugs on/after each patient's index, through the extract.

    pred only chooses patients (NDC/CPT filter). Every mapped treatment after
    index is used for lines; claims before the index date are dropped.
    """
    rules = parse_rules(rules)
    empty = {"patients_with_lot": 0, "patients": 0, "lines": [], "reaching": [],
             "top_regimens": [], "regimens_by_line": [], "days_on_therapy": [],
             "molecule_share": {"lines": [], "drugs": [], "share": []},
             "l1_l2_flow": {"n_patients": 0, "n_transitions": 0, "side": FLOW_SIDE,
                            "keep": sorted(FLOW_KEEP), "flows": []}, "rules": rules}
    if not pred:
        return empty
    n_cohort = int(query(f"select count(*) n from dim_patient where {pred}").n.iloc[0])
    empty["patients"] = n_cohort
    admin = mapped_admin(pred, index_sql, window_end)
    if admin.empty:
        return empty
    lined = lines_from_admin(
        admin,
        cycle_window=rules["cycle_window_days"],
        line_gap=rules["line_gap_days"],
        min_cycles=rules["min_cycles"],
        min_days=rules["min_days"],
    )
    per_pt = lined.groupby("patient_id")["line_number"].max()
    line_counts = per_pt.value_counts().sort_index()
    lines = [{"line": int(k), "patients": int(v)} for k, v in line_counts.items()]
    reach = lined.groupby("line_number")["patient_id"].nunique().sort_index()
    reaching = [{"line": int(k), "patients": int(v)} for k, v in reach.items()]
    first = lined[lined["line_number"] == 1].drop_duplicates("patient_id")
    top = (first["line_regimen"].value_counts().head(8)
           .rename_axis("regimen").reset_index(name="patients"))
    pl = lined.drop_duplicates(["patient_id", "line_number"])
    regimens_by_line = []
    for ln, sub in pl.groupby("line_number"):
        vc = sub["line_regimen"].value_counts().head(10)
        n_line = int(len(sub))
        regimens_by_line.append({
            "line": int(ln),
            "n": n_line,
            "regimens": [{"regimen": str(name), "patients": int(v),
                          "pct": (v / n_line) if n_line else 0}
                         for name, v in vc.items()],
        })
    return {
        "patients": n_cohort,
        "patients_with_lot": int(per_pt.shape[0]),
        "lines": lines,
        "reaching": reaching,
        "top_regimens": [{"regimen": r.regimen, "patients": int(r.patients)} for r in top.itertuples()],
        "regimens_by_line": regimens_by_line,
        "days_on_therapy": days_on_therapy_by_line(lined),
        "molecule_share": molecule_share_by_line(lined),
        "l1_l2_flow": l1_l2_flow(lined),
        "rules": rules,
    }
