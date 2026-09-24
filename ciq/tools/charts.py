"""Vega-Lite v5 chart spec builders.

Every tool returns renderable specs (data inlined) so the UI can draw the answer
dynamically (react-vega / vega-embed / altair). Styling follows the platform's
validated categorical palette; one y-axis per chart, thin marks, tooltips on.
"""
from __future__ import annotations

import pandas as pd

SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
# validated categorical order (light surface); never cycled - fold extras into "Other"
CATEGORICAL = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
PRIMARY = CATEGORICAL[0]
NEUTRAL = "#b5b4ae"  # "Other"/remainder buckets — never a categorical slot

CONFIG = {
    "background": None,
    "font": "Inter, system-ui, sans-serif",
    "view": {"stroke": None},
    "axis": {"labelAngle": 0, "labelColor": "#52514e", "titleColor": "#52514e", "gridColor": "#e8e7e3",
             "domainColor": "#c9c8c2", "tickColor": "#c9c8c2", "labelFontSize": 11, "titleFontSize": 11,
             "titleFontWeight": 500},
    "legend": {"labelColor": "#52514e", "titleColor": "#52514e", "orient": "top"},
    "title": {"color": "#0b0b0b", "fontSize": 14, "fontWeight": 600, "anchor": "start", "subtitleColor": "#52514e"},
    "range": {"category": CATEGORICAL},
    "bar": {"cornerRadiusEnd": 4, "stroke": "#fcfcfb", "strokeWidth": 2},
    "line": {"strokeWidth": 2},
    "point": {"size": 64, "filled": True},
    "arc": {"stroke": "#fcfcfb", "strokeWidth": 2},
}


def _fixed_scale(domain: list | None) -> dict:
    """Pin entities to categorical slots so an entity keeps its colour across every chart."""
    return {"scale": {"domain": list(domain), "range": CATEGORICAL[: len(domain)]}} if domain else {}


def _records(df: pd.DataFrame) -> list[dict]:
    from ciq.tools.base import _jsonable
    return _jsonable(df)


def _base(df: pd.DataFrame, title: str, subtitle: str | None) -> dict:
    t = {"text": title}
    if subtitle:
        t["subtitle"] = subtitle
    return {"$schema": SCHEMA, "title": t, "data": {"values": _records(df)}, "config": CONFIG,
            "width": "container", "height": 300}


def _tooltip(fields: list[tuple[str, str, str | None]]) -> list[dict]:
    out = []
    for f, typ, fmt in fields:
        t = {"field": f, "type": typ}
        if fmt:
            t["format"] = fmt
        out.append(t)
    return out


def bar(df: pd.DataFrame, x: str, y: str, title: str, *, subtitle: str | None = None, horizontal: bool = False,
        y_format: str | None = None, x_title: str | None = None, y_title: str | None = None,
        sort: str | list | None = "-y", color: str | None = None, label: bool = True,
        tooltip_extra: list[tuple[str, str, str | None]] | None = None) -> dict:
    """Bar chart. `y` is the measure. horizontal=True draws categories on the y axis (long labels)."""
    spec = _base(df, title, subtitle)
    cat_axis = {"field": x, "type": "nominal", "title": x_title if x_title is not None else x,
                "axis": {"labelLimit": 260}}
    if sort is not None:
        cat_axis["sort"] = sort if isinstance(sort, list) else ("-x" if horizontal and sort == "-y" else sort)
    val_axis = {"field": y, "type": "quantitative", "title": y_title if y_title is not None else y}
    if y_format:
        val_axis["axis"] = {"format": y_format}
    enc = {"x": val_axis, "y": cat_axis} if horizontal else {"x": cat_axis, "y": val_axis}
    tips = [(x, "nominal", None), (y, "quantitative", y_format)] + (tooltip_extra or [])
    enc["tooltip"] = _tooltip(tips)
    if color:
        enc["color"] = {"field": color, "type": "nominal"}
    mark = {"type": "bar", "color": PRIMARY} if not color else {"type": "bar"}
    if horizontal:
        spec["height"] = {"step": 26}
    layers = [{"mark": mark, "encoding": enc}]
    if label and not color:
        text_enc = dict(enc)
        text_enc.pop("tooltip")
        text_enc["text"] = {"field": y, "type": "quantitative", **({"format": y_format} if y_format else {})}
        layers.append({"mark": {"type": "text", "color": "#52514e", "fontSize": 11,
                                **({"align": "left", "dx": 4} if horizontal else {"baseline": "bottom", "dy": -3})},
                       "encoding": text_enc})
    spec["layer"] = layers
    return spec


def grouped_bar(df: pd.DataFrame, x: str, y: str, group: str, title: str, *, subtitle: str | None = None,
                y_format: str | None = None, x_title: str | None = None, y_title: str | None = None,
                x_sort: list | None = None, group_sort: list | None = None, domain: list | None = None) -> dict:
    spec = _base(df, title, subtitle)
    spec["mark"] = {"type": "bar"}
    spec["encoding"] = {
        "x": {"field": x, "type": "ordinal", "title": x if x_title is None else x_title, **({"sort": x_sort} if x_sort else {})},
        "xOffset": {"field": group, **({"sort": group_sort} if group_sort else {})},
        "y": {"field": y, "type": "quantitative", "title": y_title or y, **({"axis": {"format": y_format}} if y_format else {})},
        "color": {"field": group, "type": "nominal", "title": None, **({"sort": group_sort} if group_sort else {}),
                  **_fixed_scale(domain)},
        "tooltip": _tooltip([(x, "ordinal", None), (group, "nominal", None), (y, "quantitative", y_format)]),
    }
    return spec


def stacked_bar(df: pd.DataFrame, x: str, y: str, color: str, title: str, *, subtitle: str | None = None,
                normalize: bool = True, x_sort: list | None = None, x_title: str | None = None,
                y_title: str | None = None, horizontal: bool = False, neutral_label: str | None = None,
                y_format: str | None = None) -> dict:
    """Stacked bar. Series take categorical slots by total size; `neutral_label` (e.g. 'Other') is gray and last."""
    totals = df.groupby(color)[y].sum().sort_values(ascending=False)
    domain = [c for c in totals.index if c != neutral_label][: len(CATEGORICAL)]
    rng = CATEGORICAL[: len(domain)]
    if neutral_label is not None and neutral_label in totals.index:
        domain, rng = domain + [neutral_label], rng + [NEUTRAL]
    d = df.copy()
    d["_stack_order"] = d[color].map({c: i for i, c in enumerate(domain)})
    spec = _base(d, title, subtitle)
    spec["mark"] = {"type": "bar", "cornerRadiusEnd": 0}
    cat = {"field": x, "type": "ordinal", "title": x if x_title is None else x_title, **({"sort": x_sort} if x_sort else {})}
    val = {"field": y, "type": "quantitative", "title": y_title or y, "stack": "normalize" if normalize else "zero",
           **({"axis": {"format": ".0%"}} if normalize else {})}
    spec["encoding"] = {("y" if horizontal else "x"): cat, ("x" if horizontal else "y"): val,
                        "color": {"field": color, "type": "nominal", "title": None,
                                  "scale": {"domain": domain, "range": rng},
                                  "legend": {"orient": "bottom", "columns": 3, "labelLimit": 320}},
                        "order": {"field": "_stack_order", "type": "quantitative"},
                        "tooltip": _tooltip([(x, "ordinal", None), (color, "nominal", None),
                                             (y, "quantitative", y_format or (".1%" if normalize else None))])}
    return spec


def line(df: pd.DataFrame, x: str, y: str, title: str, *, color: str | None = None, subtitle: str | None = None,
         x_type: str = "ordinal", y_format: str | None = None, x_title: str | None = None,
         y_title: str | None = None, step: bool = False, points: bool = True,
         domain: list | None = None) -> dict:
    spec = _base(df, title, subtitle)
    mark = {"type": "line", "point": points}
    if step:
        mark["interpolate"] = "step-after"
    if not color:
        mark["color"] = PRIMARY
    enc = {"x": {"field": x, "type": x_type, "title": x if x_title is None else x_title},
           "y": {"field": y, "type": "quantitative", "title": y_title or y,
                 **({"axis": {"format": y_format}} if y_format else {})},
           "tooltip": _tooltip([(x, x_type, None), (y, "quantitative", y_format)] +
                               ([(color, "nominal", None)] if color else []))}
    if color:
        enc["color"] = {"field": color, "type": "nominal", "title": None, **_fixed_scale(domain)}
    spec["mark"] = mark
    spec["encoding"] = enc
    return spec


def donut(df: pd.DataFrame, category: str, value: str, title: str, *, subtitle: str | None = None,
          max_slices: int = 6) -> dict:
    """Part-to-whole for a handful of categories; extras fold into 'Other'."""
    d = df[[category, value]].copy().sort_values(value, ascending=False)
    if len(d) > max_slices:
        head, tail = d.iloc[: max_slices - 1], d.iloc[max_slices - 1:]
        d = pd.concat([head, pd.DataFrame({category: ["Other"], value: [tail[value].sum()]})], ignore_index=True)
    d["share"] = d[value] / d[value].sum()
    spec = _base(d, title, subtitle)
    spec["height"] = 260
    spec["encoding"] = {
        "theta": {"field": value, "type": "quantitative", "stack": True},
        "color": {"field": category, "type": "nominal", "title": None, "sort": list(d[category])},
        "order": {"field": value, "type": "quantitative", "sort": "descending"},
        "tooltip": _tooltip([(category, "nominal", None), (value, "quantitative", None), ("share", "quantitative", ".1%")]),
    }
    spec["layer"] = [{"mark": {"type": "arc", "innerRadius": 70, "outerRadius": 120}}]
    return spec
