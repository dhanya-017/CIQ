"""Shared plumbing for analytics tools: DB access, macro calls, result envelope."""
from __future__ import annotations

import difflib
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import duckdb
import numpy as np
import pandas as pd

from ciq import config

_local = threading.local()


def connection() -> duckdb.DuckDBPyConnection:
    """Read-only, per-thread connection to the warehouse."""
    con = getattr(_local, "con", None)
    if con is None:
        if not config.WAREHOUSE_DB.exists():
            raise RuntimeError(f"Warehouse not built: run `python -m ciq.build.build_warehouse` ({config.WAREHOUSE_DB})")
        con = duckdb.connect(str(config.WAREHOUSE_DB), read_only=True)
        _local.con = con
    return con


def sql_literal(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return repr(float(v))
    if isinstance(v, (date, datetime)):
        return f"DATE '{v:%Y-%m-%d}'"
    return "'" + str(v).replace("'", "''") + "'"


def call_fn(fn: str, *positional: Any, **named: Any) -> pd.DataFrame:
    """Call a stored table macro. Named args map to `p_<name> := value` (None values are omitted)."""
    if not fn.isidentifier():
        raise ValueError(f"bad function name {fn!r}")
    args = [sql_literal(v) for v in positional]
    args += [f"p_{k} := {sql_literal(v)}" for k, v in named.items() if v is not None]
    return connection().execute(f"select * from {fn}({', '.join(args)})").df()


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    return connection().execute(sql, params or []).df()


def coverage() -> dict:
    r = query("select data_start, data_end, n_patients from ref_data_coverage").iloc[0]
    start, end = pd.Timestamp(r["data_start"]).date(), pd.Timestamp(r["data_end"]).date()
    return {"data_start": str(start), "data_end": str(end), "months": round((end - start).days / 30.44, 1),
            "n_patients": int(r["n_patients"])}


def resolve_product(product: str | None) -> str:
    """Normalise a user-supplied product/brand/molecule name to the canonical product key."""
    product = (product or config.DEFAULT_PRODUCT).strip()
    cat = query("select distinct product, molecule from fact_drug_event")
    products = set(cat["product"])
    up = product.upper()
    if up in products:
        return up
    by_mol = cat[cat["molecule"].str.lower() == product.lower()]
    if len(by_mol):
        return by_mol["product"].iloc[0]
    close = difflib.get_close_matches(up, sorted(products), n=5, cutoff=0.5)
    raise ValueError(f"Unknown product '{product}'. Closest matches: {close or 'none'}. "
                     f"Use list_products to see all available products.")


def num(x) -> float | None:
    """float(x), or None for a missing value (None, NaN, pd.NA) - e.g. a median of an all-missing column."""
    return None if x is None or pd.isna(x) else float(x)


def pct(x: float, digits: int = 1) -> str:
    return f"{x * 100:.{digits}f}%"


def kpi(label: str, value: Any, fmt: str = "int", note: str | None = None, lead: bool = False) -> dict:
    """Headline number for the UI's KPI strip. fmt: int | pct | num | days. lead=True marks the key takeaway."""
    return {"label": label, "value": value, "format": fmt, "note": note, "lead": lead}


@dataclass
class ToolResult:
    tool: str
    title: str
    params: dict
    data: pd.DataFrame | dict[str, pd.DataFrame]
    charts: list[dict] = field(default_factory=list)
    insights: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    subtitle: str | None = None                      # one-line "what this shows" under the card title
    kpis: list[dict] = field(default_factory=list)   # headline numbers (see kpi()); UI only
    observations: list[str] | None = None            # insights the KPI strip does not already say; None = all

    def to_dict(self, max_rows: int = 500) -> dict:
        def records(df: pd.DataFrame):
            return _jsonable(df.head(max_rows))
        data = {k: records(v) for k, v in self.data.items()} if isinstance(self.data, dict) else records(self.data)
        from ciq import methodology
        return json_safe({"tool": self.tool, "title": self.title, "subtitle": self.subtitle, "params": self.params,
                          "method": methodology.describe(self.tool, self.params, self.meta),
                          "data": data, "kpis": self.kpis,
                          "observations": self.insights if self.observations is None else self.observations,
                          "charts": self.charts, "insights": self.insights, "caveats": self.caveats,
                          "meta": {"coverage": coverage(), **self.meta}})

    def to_llm_text(self, max_rows: int = 40) -> str:
        """Compact text form for feeding back to the language model (charts omitted)."""
        parts = [f"# {self.title}", f"params: {self.params}"]
        frames = self.data.items() if isinstance(self.data, dict) else [("data", self.data)]
        for name, df in frames:
            parts.append(f"## {name} ({len(df)} rows)\n" + df.head(max_rows).to_csv(index=False))
        insights = self.insights + [o for o in (self.observations or []) if o not in self.insights]
        if insights:
            parts.append("## insights\n" + "\n".join(f"- {i}" for i in insights))
        if self.caveats:
            parts.append("## caveats\n" + "\n".join(f"- {c}" for c in self.caveats))
        if self.charts:
            parts.append("## charts shown (customize_chart `chart` index)\n" + "\n".join(
                f"{i}: {c.get('title', {}).get('text', '') if isinstance(c.get('title'), dict) else c.get('title', '')}"
                for i, c in enumerate(self.charts)))
        if self.meta.get("chart_summary"):
            parts.append("## chart shown to the user\n" + self.meta["chart_summary"])
        return "\n".join(parts)


def json_safe(obj: Any) -> Any:
    """Recursively replace NaN/inf (not valid JSON) with None and numpy scalars with Python values."""
    if isinstance(obj, dict):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float) and not np.isfinite(obj):
        return None
    return obj


def _jsonable(df: pd.DataFrame) -> list[dict]:
    out = df.copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime("%Y-%m-%d")
        elif out[c].dtype == object:
            out[c] = out[c].map(lambda v: v.isoformat() if isinstance(v, (date, datetime)) else v)
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict(orient="records")
