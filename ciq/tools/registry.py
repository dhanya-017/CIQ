"""Tool registry: agent-facing definitions (Claude tool-use format) and a dispatcher.

    from ciq.tools.registry import anthropic_tools, run_tool
    client.messages.create(..., tools=anthropic_tools())
    result = run_tool(block.name, block.input)       # -> ToolResult
    result.to_llm_text()  # feed back as tool_result content
    result.to_dict()      # send to UI (charts = Vega-Lite specs)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ciq import config
from ciq.tools import analytics as a
from ciq.tools.base import ToolResult

PRODUCT = {"type": "string", "description": f"Brand/product name (e.g. KEYTRUDA, OPDIVO, TAGRISSO, CARBOPLATIN) or molecule "
                                             f"(e.g. pembrolizumab). Defaults to {config.DEFAULT_PRODUCT}. "
                                             "Use list_products to discover names."}
PRODUCT_OPT = {**PRODUCT, "description": "Optional product to restrict the cohort to patients/lines containing it; "
                                         "omit for the whole lung systemic therapy market."}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    properties: dict
    handler: Callable[..., ToolResult]
    required: tuple[str, ...] = ()

    def schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.properties, "required": list(self.required)}}


def _schema_hint() -> str:
    """Column names of the main tables, so the model can write SQL without a `describe` round trip."""
    tables = ["fact_drug_event", "fact_dx_event", "fact_px_event", "dim_patient", "dim_hcp", "lot_line", "lot_line_drug",
              "ref_drug"]
    try:
        from ciq.tools.base import query
        cols = query("select table_name, string_agg(column_name, ', ' order by ordinal_position) cols "
                     "from information_schema.columns where table_name in (" + ", ".join(f"'{t}'" for t in tables) +
                     ") group by 1")
    except Exception:  # warehouse not built yet (e.g. during setup): fall back to table names only
        return "Tables: " + ", ".join(tables) + "."
    by_name = dict(zip(cols["table_name"], cols["cols"]))
    return "Tables and columns:\n" + "\n".join(f"- {t}({by_name[t]})" for t in tables if t in by_name)


def _int(desc: str, default: int, minimum: int = 1) -> dict:
    return {"type": "integer", "description": f"{desc} Default {default}.", "minimum": minimum}


TOOLS: list[Tool] = [
    Tool("data_overview", "Describe the available data: date coverage, patient counts, tables and known gaps "
         "(e.g. no gender, no payer names). Call first when unsure what the data can answer.", {}, a.data_overview),
    Tool("list_products", "List all products (brands/generics) in the claims with drug class, therapy group and patient "
         "counts. Use to resolve product names or discover competitors.",
         {"therapy_group": {"type": "string", "enum": ["LUNG_SYSTEMIC", "OTHER_ONCOLOGY", "SUPPORTIVE"],
                            "description": "Optional filter."}}, a.list_products),
    Tool("market_incidence_prevalence", "Market overview: incidence (newly diagnosed) and prevalence (diagnosed) lung "
         "cancer patients over time, plus treated and newly-treated patients. Answers 'what is the incidence and "
         "prevalence of the market and has it changed'.",
         {"grain": {"type": "string", "enum": ["month", "quarter", "year"], "description": "Time grain. Default month."},
          "washout_days": _int("Clean period with no prior dx required to call a patient incident.", 90)},
         a.market_incidence_prevalence),
    Tool("market_share", "Patient share of each lung systemic therapy product in the treated market (competitive landscape).",
         {"top_n": _int("Number of products.", 10)}, a.market_share),
    Tool("product_trend", "Brand uptake over time: product patients, new-to-product patients and share of treated market "
         "by period.", {"product": PRODUCT, "grain": {"type": "string", "enum": ["month", "quarter", "year"]}},
         a.product_trend),
    Tool("patient_demographics", "Demographics of a product's patient cohort: age-group distribution, mean/median age, "
         "payment type split. (Gender is not available in this data.)", {"product": PRODUCT}, a.patient_demographics),
    Tool("pre_index_treatments", "Most common treatments (drug classes, products or molecules) patients received in the "
         "look-back window before their first claim of the product (prior therapy history).",
         {"product": PRODUCT, "lookback_days": _int("Look-back window in days before first product claim.", 730),
          "level": {"type": "string", "enum": ["drug_class", "product", "molecule"], "description": "Default drug_class."},
          "include_supportive": {"type": "boolean", "description": "Include supportive care (G-CSF, bone agents). Default false."}},
         a.pre_index_treatments),
    Tool("source_of_business", "Source of business for a brand: share of claims/patients that are Continuing, New to "
         "Market, New to Brand (Switch / Add-on), Restart, Restart+Switch.",
         {"product": PRODUCT, "lookback_days": _int("Look-back for prior therapy.", 180),
          "grace_days": _int("Max gap to still count as continuing.", 90),
          "level": {"type": "string", "enum": ["event", "patient"], "description": "event = every claim; patient = first claim per patient. Default event."},
          "full_lookback_only": {"type": "boolean", "description": "Only claims with a complete look-back window. Default false."}},
         a.source_of_business),
    Tool("payer_mix", "Payer mix of a product: share by payment type (Commercial, Medicare, Medicaid...) and top N payer "
         "plans by claim volume.",
         {"product": PRODUCT, "top_n": _int("Number of payers.", 10),
          "level": {"type": "string", "enum": ["event", "patient"]}}, a.payer_mix),
    Tool("combination_therapy", "Combination therapy analysis: share of monotherapy vs doublet vs triplet regimens and "
         "the most common drug combinations, for a product's lines or the whole market.",
         {"product": PRODUCT_OPT, "top_n": _int("Number of regimens.", 10)}, a.combination_therapy),
    Tool("line_progression", "Progression through lines of therapy: patients reaching each line and % progressing to "
         "later lines (e.g. 3+).",
         {"product": PRODUCT_OPT, "min_line": _int("Threshold line for 'later lines'.", 3),
          "clean_start_only": {"type": "boolean", "description": "Only patients whose first therapy starts >= 90 days into the data (likely true 1L). Default false."}},
         a.line_progression),
    Tool("product_share_by_line", "A product's patient share within each line of therapy (1, 2, 3, 4+).",
         {"product": PRODUCT, "max_line_bucket": _int("Lines >= this are grouped as N+.", 4)}, a.product_share_by_line),
    Tool("treatments_by_line", "Most common regimens in each line of therapy (1, 2, 3, 4+).",
         {"product": PRODUCT_OPT, "top_n": _int("Regimens per line.", 5),
          "max_line_bucket": _int("Lines >= this are grouped as N+.", 4)}, a.treatments_by_line),
    Tool("dx_around_index", "Most common diagnosis (ICD-10) codes among a product's patients in a window before and/or "
         "after their first product claim.",
         {"product": PRODUCT, "window_days": _int("Window length in days.", 540),
          "period": {"type": "string", "enum": ["pre", "post", "both"], "description": "Default both."},
          "top_n": _int("Codes per period.", 10),
          "code_level": {"type": "string", "enum": ["code", "category"], "description": "Full code or 3-char category. Default code."},
          "exclude_indication": {"type": "boolean", "description": "Exclude C34 lung cancer codes. Default false."}},
         a.dx_around_index),
    Tool("persistency", "Treatment persistency (Kaplan-Meier) for a product: % of patients still on therapy over time, "
         "discontinuation rate and median time on therapy.",
         {"product": PRODUCT, "grace_days": _int("Allowed gap beyond days supply before discontinuation.", 60),
          "step_days": _int("Curve resolution in days.", 30)}, a.persistency),
    Tool("hcp_market_concentration", "Physician decile analysis: are treating HCPs for the market concentrated or "
         "scattered, before vs after patients' first product claim (market = all lung systemic therapies).",
         {"product": PRODUCT, "window_days": _int("Window before/after first product claim.", 730),
          "method": {"type": "string", "enum": ["hcp_count", "volume"], "description": "Decile method. Default hcp_count."},
          "include_product": {"type": "boolean", "description": "Include the product itself in market volume. Default true."}},
         a.hcp_market_concentration),
    Tool("hcp_product_share_deciles", "Decile HCPs by product volume and show the product's market share within each "
         "decile (brand concentration among prescribers/administering physicians).",
         {"product": PRODUCT, "method": {"type": "string", "enum": ["volume", "hcp_count"]}}, a.hcp_product_share_deciles),
    Tool("compare_products", "Side-by-side comparison of 2-4 brands/products: scorecard (patients, share, age, payer "
         "mix, line share, combination use, source of business, persistency, prescriber base) plus overlaid charts. "
         "Use for any 'compare', 'vs', 'benchmark against competitors' question. The UI renders an interactive "
         "comparison panel where the user can swap brands.",
         {"products": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4,
                       "description": "2-4 product names, e.g. [\"KEYTRUDA\", \"OPDIVO\", \"TECENTRIQ\"]."},
          "grace_days": _int("Persistency grace period.", 60)}, a.compare_products, required=("products",)),
    Tool("run_sql", "Run a read-only SQL (DuckDB) query against the warehouse for questions no other tool answers, "
         "optionally drawing a custom chart of the result. Use `chart` whenever the user asks for a chart, breakdown "
         "or chart type the other tools don't produce (e.g. 'plot X by Y', 'show this as a line/heatmap'). "
         "Stored functions fn_* can be selected from (e.g. select * from fn_product_index('KEYTRUDA')). "
         + _schema_hint(),
         {"sql": {"type": "string", "description": "A single SELECT/WITH statement. For a chart, aggregate in SQL to "
                                                   "exactly the rows to plot, in long/tidy form (one row per x, or per "
                                                   "x + series), ordered as they should appear."},
          "max_rows": _int("Row cap.", 200),
          "chart": {"type": "object", "description": "Optional chart of the query result, drawn in the platform's "
                    "style. Columns must exist in the result. If the spec is invalid the call fails with the reason "
                    "and the available columns - fix it and call again. The result then describes the chart drawn; "
                    "check it answers the user's question.",
                    "properties": {
                        "type": {"type": "string", "enum": list(a.CHART_TYPES), "description":
                                 "bar: one measure by category. line: trend over an ordered x (dates, months), "
                                 "`color` for multiple series. stacked_bar: composition (needs `color`; "
                                 "normalize=true for 100%). grouped_bar: compare series side by side (needs `color`). "
                                 "donut: part-to-whole, <=6 categories. scatter: two measures (x and y numeric), "
                                 "`label` names points. heatmap: two categories (x, y) shaded by `value`."},
                        "x": {"type": "string", "description": "Column for the x axis / category (donut: slice)."},
                        "y": {"type": "string", "description": "Column for the measure (heatmap: row category)."},
                        "color": {"type": "string", "description": "Optional series column."},
                        "value": {"type": "string", "description": "heatmap only: numeric column that shades the cells."},
                        "label": {"type": "string", "description": "scatter only: column used to label each point."},
                        "title": {"type": "string", "description": "Chart title in sentence case."},
                        "subtitle": {"type": "string", "description": "One line: how to read it / what is counted."},
                        "x_title": {"type": "string"}, "y_title": {"type": "string"},
                        "y_format": {"type": "string", "enum": list(a._FORMATS),
                                     "description": "percent/percent1 expect fractions (0.25 = 25%)."},
                        "sort": {"type": "string", "enum": ["desc", "asc", "none"],
                                 "description": "bar only: order bars by value, or 'none' to keep SQL order. Default desc."},
                        "horizontal": {"type": "boolean", "description": "Bars along y (long labels). Default: auto."},
                        "normalize": {"type": "boolean", "description": "stacked_bar: 100% stacks."},
                        "highlight": {"type": "string", "description": "bar only: x value to call out."}},
                    "required": ["type", "x", "y"]}},
         a.run_sql, required=("sql",)),
]

_BY_NAME = {t.name: t for t in TOOLS}


def anthropic_tools() -> list[dict]:
    return [t.schema() for t in TOOLS]


def run_tool(name: str, arguments: dict | None = None) -> ToolResult:
    if name not in _BY_NAME:
        raise KeyError(f"Unknown tool {name!r}. Available: {sorted(_BY_NAME)}")
    tool = _BY_NAME[name]
    args = {k: v for k, v in (arguments or {}).items() if k in tool.properties and v is not None}
    return tool.handler(**args)
