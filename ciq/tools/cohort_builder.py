"""Patient cohort builder: incident diagnosis index, optional NDC/CPT, post-index LOT.

With a study period [Start, End]:
  1. Keep patients whose first-ever qualifying ICD (or any diagnosis if none is
     given) falls in that window. That date is the index. An earlier first
     diagnosis excludes the patient even if later codes fall inside the window.
  2. Optional NDC and/or CPT: at least one claim for the given codes on or after
     index, through the latest record. If both lists are given, the patient needs
     at least one of each. Pre-index claims do not count.
LOT then uses every mapped treatment from the index forward, not only the
filter codes, and does not stop at End Date.

Without dates, the nested AND/OR/SEQ tree is evaluated as ever-had codes.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from ciq import config
from ciq.tools.base import json_safe, query, sql_literal

SYSTEMS = ("ICD", "CPT", "NDC")
MAX_DEPTH = 8
MAX_LEAVES = 40
SEQ_WINDOWS = (30, 60, 90)


def _norm(code: str) -> str:
    return (code or "").strip().upper()


def _prefix(code: str) -> str:
    c = _norm(code).rstrip("*")
    return c.replace(".", "").replace(" ", "")


def suggest_codes(system: str, q: str, limit: int = 20) -> list[dict]:
    """Typeahead from codes observed in this warehouse. Reference tables supply labels only."""
    system = (system or "ICD").upper()
    if system not in SYSTEMS:
        raise ValueError("system must be ICD, CPT or NDC")
    qn = (q or "").strip()
    if not qn:
        return []
    n = max(1, min(int(limit), 40))
    like = sql_literal(qn.replace("*", "") + "%")
    contains = sql_literal("%" + qn.replace("*", "") + "%")
    compact = sql_literal(_prefix(qn) + "%")
    if system == "ICD":
        df = query(f"""
            with hits as (
              select dx_code as code, any_value(dx_desc) as dx_desc, count(*) as n
              from fact_dx_event
              where replace(dx_code, '.', '') ilike {compact} or dx_code ilike {like}
                    or coalesce(dx_desc, '') ilike {contains}
              group by dx_code
            )
            select h.code,
                   coalesce(nullif(h.dx_desc, ''), i.short_desc, h.code) as label
            from hits h
            left join ref_icd10cm i on replace(i.code, '.', '') = replace(h.code, '.', '')
            order by length(replace(h.code, '.', '')),
                     (case when replace(h.code, '.', '') ilike {compact} then 0 else 1 end),
                     h.n desc, h.code
            limit {n}""")
    elif system == "CPT":
        df = query(f"""
            with hits as (
              select px_code as code, any_value(px_desc) as px_desc, count(*) as n
              from fact_px_event
              where px_code ilike {like} or coalesce(px_desc, '') ilike {contains}
              group by px_code
            )
            select h.code, coalesce(nullif(h.px_desc, ''), r.short_desc, h.code) as label
            from hits h
            left join ref_hcpcs r on r.code = h.code
            order by h.n desc, h.code
            limit {n}""")
    else:
        df = query(f"""
            with hits as (
              select product as code,
                     concat_ws(' · ', nullif(product, ''), nullif(molecule, '')) as label,
                     count(*) as n, 0 as rank
              from fact_drug_event
              where product ilike {contains} or molecule ilike {contains}
                    or coalesce(product_form, '') ilike {contains}
              group by product, molecule
              union all
              select cast(code as varchar),
                     concat_ws(' · ', nullif(product, ''), cast(code as varchar)),
                     count(*), 1
              from fact_drug_event
              where coalesce(cast(code as varchar), '') ilike {like}
              group by code, product
            )
            select code, any_value(label) as label from hits
            where code is not null
            group by 1
            order by min(rank), sum(n) desc, length(code), code
            limit {n}""")
    hits = [{"system": system, "code": r.code, "label": r.label or r.code} for r in df.itertuples()]
    if system == "ICD":
        hits = _promote_icd_family(qn, hits)
    return hits


def _promote_icd_family(qn: str, hits: list[dict]) -> list[dict]:
    """Put the typed ICD stem first so a family is not replaced by one subcode."""
    stem = _norm(qn).rstrip("*")
    compact = _prefix(qn)
    if len(compact) < 3 or not compact[0].isalpha():
        return hits
    children = [h for h in hits if _prefix(h["code"]).startswith(compact)]
    if not children:
        return hits
    exact = next((h for h in hits if _prefix(h["code"]) == compact), None)
    rest = [h for h in hits if h is not exact]
    extra = (exact.get("label") or "") if exact else ""
    label = extra if extra and extra.upper() not in (stem.upper(), (exact["code"] if exact else "").upper()) else stem
    if exact:
        return [{"system": "ICD", "code": stem, "label": label}] + rest
    return [{"system": "ICD", "code": stem, "label": label}] + hits


def search_codes(q: str, system: str | None = None, limit: int = 25) -> list[dict]:
    """Search by disease/term or code fragment across one or all systems."""
    qn = (q or "").strip()
    if not qn:
        return []
    systems = [system.upper()] if system and system.upper() in SYSTEMS else list(SYSTEMS)
    out = []
    for s in systems:
        out.extend(suggest_codes(s, qn, limit=limit))
    return out[:limit]


def _codes_of(node: dict) -> list[str]:
    items = node.get("codes")
    if isinstance(items, list) and items:
        out = []
        for it in items:
            raw = it.get("code") if isinstance(it, dict) else it
            c = _norm(str(raw or ""))
            if c:
                out.append(c)
        return out
    c = _norm(node.get("code") or "")
    return [c] if c else []


def _event_table(sys: str) -> str:
    if sys == "ICD":
        return "fact_dx_event"
    if sys == "CPT":
        return "fact_px_event"
    if sys == "NDC":
        return "fact_drug_event"
    raise ValueError("system must be ICD, CPT or NDC")


def _event_pred(alias: str, sys: str, codes: list[str]) -> str | None:
    parts = []
    for code in codes:
        pref = _prefix(code)
        if not pref:
            continue
        pat = sql_literal(pref + "%")
        if sys == "ICD":
            parts.append(f"replace({alias}.dx_code, '.', '') ilike {pat}")
        elif sys == "CPT":
            parts.append(f"replace({alias}.px_code, '.', '') ilike {pat}")
        elif sys == "NDC":
            lit = sql_literal(code.rstrip("*"))
            parts.append(
                f"(replace(coalesce({alias}.code, ''), '.', '') ilike {pat} "
                f"or {alias}.product ilike {lit} or {alias}.molecule ilike {lit} "
                f"or {alias}.product_form ilike {lit})")
        else:
            raise ValueError("system must be ICD, CPT or NDC")
    if not parts:
        return None
    return "(" + " or ".join(parts) + ")"


def _codes_by_system(tree: dict | None) -> dict[str, list[str]]:
    """Unique ICD / CPT / NDC codes in tree order (nested groups flattened)."""
    out = {s: [] for s in SYSTEMS}
    seen = {s: set() for s in SYSTEMS}

    def walk(n):
        if not isinstance(n, dict):
            return
        if (n.get("kind") or "criterion") == "group":
            for ch in n.get("children") or []:
                walk(ch)
            return
        sys = (n.get("system") or "ICD").upper()
        if sys not in out:
            return
        for c in _codes_of(n):
            if c not in seen[sys]:
                seen[sys].add(c)
                out[sys].append(c)

    walk(tree)
    return out


def _follow_date_sql(alias: str = "e", window_end: date | None = None) -> str:
    """Claims on/after the patient's index. Study End Date is not an upper bound."""
    bits = [f"{alias}.service_date >= i.index_date"]
    if window_end:
        bits.append(f"{alias}.service_date <= {sql_literal(window_end)}")
    return " and ".join(bits)


def _post_index_exists(sys: str, codes: list[str]) -> str | None:
    pred = _event_pred("e", sys, codes)
    if not pred:
        return None
    return (f"exists (select 1 from {_event_table(sys)} e where e.patient_id = i.patient_id "
            f"and {pred} and {_follow_date_sql('e')})")


def _code_sql(sys: str, code: str, after_index: bool = False, window_end: date | None = None) -> str | None:
    pred = _event_pred("e", sys, [code])
    if not pred:
        return None
    if after_index:
        return (f"exists (select 1 from {_event_table(sys)} e where e.patient_id = i.patient_id "
                f"and {pred} and {_follow_date_sql('e', window_end)})")
    return f"patient_id in (select e.patient_id from {_event_table(sys)} e where {pred})"


def _leaf_sql(node: dict, after_index: bool = False, index_event: dict | None = None,
              window_end: date | None = None) -> str | None:
    if after_index and index_event is not None and node is index_event:
        return "true"
    sys = (node.get("system") or "ICD").upper()
    parts = [p for p in (_code_sql(sys, c, after_index, window_end) for c in _codes_of(node)) if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return "(" + " or ".join(parts) + ")"


def _child_sql(ch: dict, depth: int, leaves: list[int], after_index: bool = False,
               index_event: dict | None = None, window_end: date | None = None) -> str | None:
    kind = ch.get("kind") or "criterion"
    if kind == "group":
        return _group_sql(ch, depth + 1, leaves, after_index=after_index, index_event=index_event,
                          window_end=window_end)
    leaves[0] += max(1, len(_codes_of(ch)))
    if leaves[0] > MAX_LEAVES:
        raise ValueError("too many criteria")
    return _leaf_sql(ch, after_index=after_index, index_event=index_event, window_end=window_end)


def _is_seq(join: str) -> bool:
    j = re.sub(r"[\s-]+", "_", (join or "").upper())
    return j in ("SEQ", "SEQUENTIAL", "SEQUENTIAL_EVENT")


def _seq_window_days(seq: dict) -> int | None:
    raw = str((seq or {}).get("window") or "90").lower()
    if any(tok in raw for tok in ("any", "anytime", "any time")):
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    days = int(digits) if digits else 90
    return days if days in SEQ_WINDOWS else 90


def _seq_occurrence(seq: dict) -> int:
    try:
        n = int((seq or {}).get("occurrence") or 1)
    except (TypeError, ValueError):
        n = 1
    return 2 if n >= 2 else 1


def _seq_sql(index_ch: dict, follow_ch: dict, after_index: bool = False,
             window_end: date | None = None) -> str | None:
    """Patients with an index event then a later follow-up (not same day)."""
    if (index_ch.get("kind") or "criterion") == "group" or (follow_ch.get("kind") or "criterion") == "group":
        raise ValueError("sequential event needs ICD/NDC/CPT index and follow-up events")
    idx_sys = (index_ch.get("system") or "ICD").upper()
    fol_sys = (follow_ch.get("system") or "ICD").upper()
    idx_pred = _event_pred("idx", idx_sys, _codes_of(index_ch))
    fol_pred = _event_pred("fol", fol_sys, _codes_of(follow_ch))
    if not idx_pred or not fol_pred:
        return None
    seq = follow_ch.get("sequence") if isinstance(follow_ch.get("sequence"), dict) else {}
    days = _seq_window_days(seq)
    occ = _seq_occurrence(seq)
    order = "fol.service_date > idx.service_date"
    if days:
        order += f" and fol.service_date <= idx.service_date + interval {days} day"
    if after_index:
        order += " and " + _follow_date_sql("idx", window_end)
        if window_end:
            order += f" and fol.service_date <= {sql_literal(window_end)}"
        return (
            f"exists (select 1 from {_event_table(fol_sys)} fol "
            f"where fol.patient_id = i.patient_id and {fol_pred} and exists ("
            f"select 1 from {_event_table(idx_sys)} idx "
            f"where idx.patient_id = fol.patient_id and {idx_pred} and {order}) "
            f"group by fol.patient_id having count(*) >= {occ})"
        )
    return (
        f"patient_id in (select fol.patient_id from {_event_table(fol_sys)} fol "
        f"where {fol_pred} and exists (select 1 from {_event_table(idx_sys)} idx "
        f"where idx.patient_id = fol.patient_id and {idx_pred} and {order}) "
        f"group by fol.patient_id having count(*) >= {occ})"
    )


def _fold_sql(children: list[dict], depth: int, leaves: list[int], default_join: str = "AND",
              after_index: bool = False, index_event: dict | None = None,
              window_end: date | None = None) -> str | None:
    """Left-associative AND/OR; SEQ replaces the previous criterion with index→follow-up."""
    terms: list[str] = []
    joins: list[str] = []
    prev_ch = None
    for ch in children:
        join = (ch.get("join") or default_join or "AND").upper()
        if _is_seq(join) and terms:
            seq = _seq_sql(prev_ch, ch, after_index=after_index, window_end=window_end)
            if not seq:
                continue
            leaves[0] += max(1, len(_codes_of(ch)))
            if leaves[0] > MAX_LEAVES:
                raise ValueError("too many criteria")
            terms[-1] = seq
            prev_ch = ch
            continue
        sql = _child_sql(ch, depth, leaves, after_index=after_index, index_event=index_event,
                         window_end=window_end)
        if not sql:
            continue
        if not terms:
            terms.append(sql)
        else:
            if join not in ("AND", "OR"):
                raise ValueError("join must be AND, OR, or SEQUENTIAL EVENT")
            terms.append(sql)
            joins.append(join)
        prev_ch = ch
    if not terms:
        return None
    expr = terms[0]
    for joiner, nxt in zip(joins, terms[1:]):
        op = " and " if joiner == "AND" else " or "
        expr = f"({expr}{op}{nxt})"
    return expr


def _group_sql(node: dict, depth: int, leaves: list[int], after_index: bool = False,
               index_event: dict | None = None, window_end: date | None = None) -> str | None:
    """Criteria in a group combine first, then that result is AND/OR with nested groups."""
    if depth > MAX_DEPTH:
        raise ValueError("too many nested groups")
    kids = node.get("children") or []
    criteria = [c for c in kids if (c.get("kind") or "criterion") != "group"]
    nested = [c for c in kids if (c.get("kind") or "criterion") == "group"]
    expr = _fold_sql(criteria, depth, leaves, node.get("op") or "AND",
                     after_index=after_index, index_event=index_event, window_end=window_end)
    for g in nested:
        sql = _child_sql(g, depth, leaves, after_index=after_index, index_event=index_event,
                         window_end=window_end)
        if not sql:
            continue
        if expr is None:
            expr = sql
            continue
        join = (g.get("join") or "AND").upper()
        if _is_seq(join):
            raise ValueError("sequential event needs ICD/NDC/CPT index and follow-up events")
        if join not in ("AND", "OR"):
            join = "AND"
        op = " and " if join == "AND" else " or "
        expr = f"({expr}{op}{sql})"
    return expr


def _parse_iso_date(raw) -> date | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    text = str(raw).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError("study period dates must be YYYY-MM-DD") from None


def parse_study_period(spec: dict | None) -> tuple[date | None, date | None]:
    """Inclusive from/to calendar dates for first-diagnosis (index) filtering."""
    if not isinstance(spec, dict):
        return None, None
    start = _parse_iso_date(spec.get("from") or spec.get("start"))
    end = _parse_iso_date(spec.get("to") or spec.get("end"))
    if start and end and end < start:
        start, end = end, start
    return start, end


def _index_event(tree: dict | None) -> dict | None:
    """First coded criterion in tree order; SEQ follow-up rows are not the index."""
    if not tree:
        return None

    def walk(n):
        if (n.get("kind") or "criterion") == "group":
            for ch in n.get("children") or []:
                found = walk(ch)
                if found:
                    return found
            return None
        if _is_seq(n.get("join") or ""):
            return None
        return n if _codes_of(n) else None

    return walk(tree)


def index_dates_sql(tree: dict | None = None, start: date | None = None, end: date | None = None) -> str:
    """Index = first-ever qualifying diagnosis date (fact_dx_event.service_date).

    Study period: min qualifying ICD (any diagnosis if none given) must fall in
    [Start, End], inclusive. A first diagnosis before Start excludes the patient.
    Without dates, first event of the first coded criterion.
    """
    bits = []
    table = "fact_dx_event e"
    if start or end:
        icd = _codes_by_system(tree)["ICD"]
        if icd:
            ev = _event_pred("e", "ICD", icd)
            if ev:
                bits.append(ev)
    else:
        event = _index_event(tree)
        if event:
            sys = (event.get("system") or "ICD").upper()
            ev = _event_pred("e", sys, _codes_of(event))
            table = f"{_event_table(sys)} e"
            if ev:
                bits.append(ev)
    where = (" where " + " and ".join(bits)) if bits else ""
    having = []
    if start:
        having.append(f"min(e.service_date) >= {sql_literal(start)}")
    if end:
        having.append(f"min(e.service_date) <= {sql_literal(end)}")
    hav = (" having " + " and ".join(having)) if having else ""
    return f"select e.patient_id, min(e.service_date) as index_date from {table}{where} group by 1{hav}"


def _index_pred(start: date | None, end: date | None, tree: dict | None = None) -> str | None:
    """Patients who have an index event inside the study window."""
    if not start and not end:
        return None
    inner = index_dates_sql(tree, start, end)
    return f"patient_id in (select i.patient_id from ({inner}) i)"


def _combine_pred(tree: dict | None, study_period: dict | None) -> tuple[str | None, str | None]:
    start, end = parse_study_period(study_period)
    idx_sql = index_dates_sql(tree, start, end)
    idx = f"patient_id in (select i.patient_id from ({idx_sql}) i)" if (start or end) else None
    if start or end:
        by = _codes_by_system(tree)
        parts = []
        ndc = _post_index_exists("NDC", by["NDC"])
        cpt = _post_index_exists("CPT", by["CPT"])
        if ndc:
            parts.append(ndc)
        if cpt:
            parts.append(cpt)
        if parts:
            follow = " and ".join(parts)
            return (f"patient_id in (select i.patient_id from ({idx_sql}) i where {follow})", idx)
        return idx, idx
    pred = _group_sql(tree, 0, [0]) if tree else None
    return pred, idx


def evaluate_tree(tree: dict | None, study_period: dict | None = None) -> dict:
    """Count distinct patients matching the study period and/or boolean code tree."""
    if not tree and not parse_study_period(study_period)[0] and not parse_study_period(study_period)[1]:
        raise ValueError("missing criteria")
    pred, idx = _combine_pred(tree, study_period)
    warehouse = int(query("select count(*) n from dim_patient").n.iloc[0])
    universe = warehouse
    if idx:
        universe = int(query(f"select count(*) n from dim_patient where {idx}").n.iloc[0])
    if not pred:
        return {"size": 0, "universe": universe, "sql": None, "empty": True,
                "index_from": str(parse_study_period(study_period)[0] or ""),
                "index_to": str(parse_study_period(study_period)[1] or "")}
    n = int(query(f"select count(*) n from dim_patient where {pred}").n.iloc[0])
    start, end = parse_study_period(study_period)
    return {"size": n, "universe": universe, "sql": pred, "empty": False,
            "index_from": str(start) if start else "", "index_to": str(end) if end else ""}


def patient_ids_for_tree(tree: dict | None, cap: int = 100000, study_period: dict | None = None) -> list[str]:
    pred, _ = _combine_pred(tree, study_period)
    if not pred:
        return []
    df = query(f"select patient_id from dim_patient where {pred} limit {int(cap)}")
    return df.patient_id.astype(str).tolist()


def _fold_summary(children: list[dict], walk) -> str:
    acc = ""
    for ch in children:
        bit = walk(ch)
        if not bit:
            continue
        if not acc:
            acc = bit
        elif _is_seq(ch.get("join") or ""):
            seq = ch.get("sequence") if isinstance(ch.get("sequence"), dict) else {}
            days = _seq_window_days(seq)
            win = f"{days}d" if days else "any"
            acc = f"({acc} SEQ[{win} x{_seq_occurrence(seq)}] {bit})"
        else:
            acc = f"({acc} {(ch.get('join') or 'AND')} {bit})"
    return acc


def summarize_tree(tree: dict | None, study_period: dict | None = None) -> str:
    start, end = parse_study_period(study_period)
    if start or end:
        by = _codes_by_system(tree)
        icd = "|".join(by["ICD"])
        prefix = f"INDEX[{start or '…'}..{end or '…'}; first {('ICD:' + icd) if icd else 'diagnosis'}]"
        bits = [prefix]
        if by["NDC"]:
            bits.append("NDC:" + "|".join(by["NDC"]))
        if by["CPT"]:
            bits.append("CPT:" + "|".join(by["CPT"]))
        return " AND ".join(bits)
    prefix = ""

    def walk(n):
        if (n.get("kind") or "criterion") == "group":
            kids = n.get("children") or []
            criteria = [c for c in kids if (c.get("kind") or "criterion") != "group"]
            nested = [c for c in kids if (c.get("kind") or "criterion") == "group"]
            acc = _fold_summary(criteria, walk)
            for g in nested:
                bit = walk(g)
                if not bit:
                    continue
                if not acc:
                    acc = bit
                else:
                    acc = f"({acc} {(g.get('join') or 'AND')} {bit})"
            return acc
        codes = _codes_of(n)
        if not codes:
            return ""
        return f"{n.get('system', 'ICD')}:{ '|'.join(codes) }"
    body = walk(tree) if tree else ""
    if prefix and body:
        return f"{prefix} AND {body}"
    return prefix or body or "empty"


def json_tree(tree: dict | None, study_period: dict | None = None, include_lot: bool = False) -> dict:
    pack = evaluate_tree(tree, study_period)
    out = {"tree": tree, **pack, "label": summarize_tree(tree, study_period), "lot": None}
    if include_lot and not pack.get("empty") and pack.get("size"):
        try:
            from ciq.tools import cohort_lot
            pred, _ = _combine_pred(tree, study_period)
            start, end = parse_study_period(study_period)
            out["lot"] = cohort_lot.for_cohort(pred, index_dates_sql(tree, start, end))
        except Exception as e:
            out["lot"] = {"error": str(e), "patients_with_lot": 0, "patients": pack.get("size") or 0,
                          "lines": [], "top_regimens": []}
    return json_safe(out)


def lot_for_tree(tree: dict | None, study_period: dict | None = None) -> dict:
    pack = evaluate_tree(tree, study_period)
    if pack.get("empty") or not pack.get("size"):
        return json_safe({"lot": None, "size": pack.get("size") or 0})
    from ciq.tools import cohort_lot
    pred, _ = _combine_pred(tree, study_period)
    start, end = parse_study_period(study_period)
    return json_safe({"lot": cohort_lot.for_cohort(pred, index_dates_sql(tree, start, end)),
                      "size": pack["size"]})


def _split_and_clauses(text: str) -> list[str]:
    parts, buf, depth = [], [], 0
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "(":
            depth += 1
            buf.append(ch)
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
            i += 1
            continue
        if depth == 0 and text[i:i + 5].lower() == " and ":
            bit = "".join(buf).strip(" ?.,;")
            if bit:
                parts.append(bit)
            buf = []
            i += 5
            continue
        buf.append(ch)
        i += 1
    bit = "".join(buf).strip(" ?.,;")
    if bit:
        parts.append(bit)
    return parts or [text.strip()]


def _system_for(code: str) -> str:
    c = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
    if not c:
        return "ICD"
    if c.isdigit() or (len(c) == 5 and c[0].isdigit()):
        return "CPT"
    if c[0].isalpha() and any(ch.isdigit() for ch in c[1:3]):
        return "ICD"
    return "NDC"


def _codes_in_text(text: str) -> list[str]:
    out, seen = [], set()
    for raw in re.findall(r"%\s*([^%]+?)\s*%", text):
        for bit in re.split(r"[,/;]+", raw):
            code = bit.strip().strip("%").upper()
            if code and code not in seen:
                seen.add(code)
                out.append(code)
    for m in re.finditer(r"\b([A-TV-Z]\d{2}(?:\.\d{1,2})?)\b", text, re.I):
        code = m.group(1).upper()
        if code not in seen:
            seen.add(code)
            out.append(code)
    return out


def _drugs_in_text(text: str) -> list[str]:
    up = text.upper()
    names = sorted({*(config.UI_BRANDS or ()), *(config.IO_BRANDS or {})}, key=len, reverse=True)
    used = [False] * len(up)
    found = []
    for name in names:
        n = name.upper()
        start = 0
        while True:
            i = up.find(n, start)
            if i < 0:
                break
            if not any(used[i:i + len(n)]):
                found.append(name)
                for j in range(i, i + len(n)):
                    used[j] = True
            start = i + 1
    return found


def _clause_nodes(clause: str) -> list[dict]:
    low = clause.lower()
    by_sys: dict[str, list[str]] = {}
    for code in _codes_in_text(clause):
        by_sys.setdefault(_system_for(code), []).append(code)
    drugs = _drugs_in_text(clause)
    if drugs:
        by_sys.setdefault("NDC", [])
        have = {x.upper() for x in by_sys["NDC"]}
        for d in drugs:
            if d.upper() not in have:
                by_sys["NDC"].append(d)
                have.add(d.upper())
    if not by_sys and any(w in low for w in ("lung cancer", "lung ca", "nsclc")):
        pref = config.INDICATION_DX_PREFIX
        if pref and code_in_warehouse("ICD", pref):
            by_sys["ICD"] = [pref]
    nodes = []
    for sys, codes in by_sys.items():
        nodes.append({"kind": "criterion", "system": sys,
                      "codes": [{"code": c} for c in codes], "code": codes[0], "label": ""})
    return nodes


def _seq_from_text(text: str) -> dict:
    low = (text or "").lower()
    window = "any" if "any time" in low or "anytime" in low else "90"
    if "within 30" in low or "30 day" in low:
        window = "30"
    elif "within 60" in low or "60 day" in low:
        window = "60"
    elif "within 90" in low or "90 day" in low:
        window = "90"
    occ = 2 if "at least twice" in low or "at least 2" in low else 1
    return {"window": window, "direction": "preceding", "occurrence": occ}


def code_in_warehouse(sys: str, code: str) -> bool:
    """True if this ICD/CPT/NDC appears in the current claims extract."""
    sys = (sys or "ICD").upper()
    if sys not in SYSTEMS:
        return False
    pred = _event_pred("e", sys, [code])
    if not pred:
        return False
    return not query(f"select 1 x from {_event_table(sys)} e where {pred} limit 1").empty


def keep_warehouse_tree(tree: dict | None) -> dict | None:
    """Drop codes that are not in this extract; keep only AND / OR / SEQUENTIAL EVENT joins."""
    if not isinstance(tree, dict):
        return None

    def walk(n):
        if (n.get("kind") or "criterion") == "group":
            kids = [walk(c) for c in (n.get("children") or [])]
            kids = [c for c in kids if c]
            if not kids:
                return None
            out = {**n, "children": kids}
            join = (out.get("join") or "AND").upper()
            out["join"] = "SEQ" if _is_seq(join) else (join if join in ("AND", "OR") else "AND")
            return out
        sys = (n.get("system") or "ICD").upper()
        if sys not in SYSTEMS:
            return None
        codes = [c for c in _codes_of(n) if code_in_warehouse(sys, c)]
        if not codes:
            return None
        out = {**n, "system": sys, "code": codes[0],
               "codes": [{"code": c} for c in codes]}
        join = (out.get("join") or "AND").upper()
        out["join"] = "SEQ" if _is_seq(join) else (join if join in ("AND", "OR", "SEQ") else "AND")
        return out

    return walk(tree)


def ask_answer(pack: dict) -> str:
    n = int(pack.get("size") or 0)
    start, end = pack.get("index_from") or "", pack.get("index_to") or ""
    uni = pack.get("universe")
    if start or end:
        win = f"{start or '…'} to {end or '…'}"
        extra = (f" {int(uni):,} had a first qualifying diagnosis in that window."
                 if uni is not None else "")
        return f"{n:,} patients match after the index-date filter ({win}).{extra}"
    return f"{n:,} patients in this extract match these criteria."


def parse_question(question: str) -> dict | None:
    """Turn a plain-English cohort question into a boolean code tree, or None."""
    q = re.sub(r"\s+", " ", (question or "").strip())
    if not q:
        return None
    bits = re.split(r"\bfollowed by\b|\bsequential(?:\s+event)?\b", q, maxsplit=1, flags=re.I)
    if len(bits) == 2:
        left = _clause_nodes(bits[0])
        right = _clause_nodes(bits[1])
        if left and right:
            follow = right[0]
            follow["join"] = "SEQ"
            follow["sequence"] = _seq_from_text(q)
            return keep_warehouse_tree({"kind": "group", "children": [left[0], follow]})
    nodes = []
    for clause in _split_and_clauses(q):
        nodes.extend(_clause_nodes(clause))
    if not nodes:
        return None
    children = []
    for i, n in enumerate(nodes):
        if i:
            n["join"] = "AND"
        children.append(n)
    return keep_warehouse_tree({"kind": "group", "children": children})
