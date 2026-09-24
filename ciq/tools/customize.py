"""Chart customisation: apply model-requested edits to a Vega-Lite spec built by ciq.tools.charts.

The model describes *what* to change ("highlight KEYTRUDA in red", "only deciles 1-5", "add a 30% target line");
this module turns each edit into a spec change against the chart's real fields and data. Anything that cannot be
applied raises ValueError naming the chart's fields and values, so the agent loop can correct the request and retry.
"""
from __future__ import annotations

import copy
import re
import warnings

import pandas as pd

from ciq.tools.charts import CATEGORICAL, PRIMARY

OPS = ("highlight", "color", "filter", "axis_range", "reference_line", "band", "chart_type", "sort", "labels",
       "title", "format", "legend", "size")
DIM = "#d3d8de"   # de-emphasised marks
NAMED = {"red": "#e34948", "green": "#1baf7a", "blue": "#2a78d6", "orange": "#eb6834", "yellow": "#eda100",
         "gold": "#c98b27", "navy": "#004567", "purple": "#4a3aa7", "pink": "#e87ba4", "teal": "#0f8a8a",
         "gray": "#9aa3ad", "grey": "#9aa3ad", "black": "#1f2937"}
FORMATS = {"percent": ".0%", "percent1": ".1%", "integer": ",.0f", "number": ",.2~f"}
_MARKS = ("bar", "line", "area", "point", "arc", "rect")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


# ---------------------------------------------------------------------------- spec helpers
def _mark_type(view: dict) -> str | None:
    m = view.get("mark")
    return m if isinstance(m, str) else (m or {}).get("type")


def _views(spec: dict) -> list[dict]:
    out = [spec] if "mark" in spec else []
    for layer in spec.get("layer", []):
        out.extend(_views(layer))
    return out


def _mark_views(spec: dict) -> list[dict]:
    """The data marks (overlays such as bands and reference lines excluded)."""
    return [v for v in _views(spec) if _mark_type(v) in _MARKS and v.get("description") != OVERLAY]


def _enc(spec: dict, view: dict | None = None) -> dict:
    """Effective encoding of a view: top-level (shared) channels overlaid by the view's own."""
    view = view or (_mark_views(spec) or [spec])[0]
    return {**spec.get("encoding", {}), **view.get("encoding", {})}


def _set_mark(view: dict, **props) -> None:
    m = view.get("mark")
    view["mark"] = {"type": m, **props} if isinstance(m, str) else {**m, **props}


def _rows(spec: dict) -> list[dict]:
    return spec.get("data", {}).get("values", [])


def _columns(spec: dict) -> list[str]:
    rows = _rows(spec)
    return [c for c in (rows[0] if rows else {}) if not c.startswith("_")]


def _cat_channels(spec: dict) -> list[tuple[str, str]]:
    """(channel, field) pairs for the categorical/time axes of the main view (x first)."""
    e = _enc(spec)
    return [(ch, e[ch]["field"]) for ch in ("x", "y") if ch in e and "field" in e[ch]
            and e[ch].get("type") in ("nominal", "ordinal", "temporal")]


def _measure(spec: dict) -> tuple[str, str] | None:
    e = _enc(spec)
    for ch in ("y", "x", "theta", "color"):
        if ch in e and "field" in e[ch] and e[ch].get("type") == "quantitative":
            return ch, e[ch]["field"]
    return None


def _series(spec: dict) -> str | None:
    c = _enc(spec).get("color", {})
    return c.get("field") if c.get("type") != "quantitative" else None


def _layered(spec: dict) -> dict:
    if "layer" not in spec:
        base = {k: spec.pop(k) for k in ("mark", "encoding", "transform") if k in spec}
        spec["layer"] = [base]
    return spec


ONE_ROW = {"values": [{}]}   # overlays (lines, bands, notes) draw once, not once per data row
OVERLAY = "ciq-overlay"      # tags overlay layers so later edits skip them


def _color(c) -> str:
    if not isinstance(c, str) or not re.match(r"^(#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?|[a-zA-Z]{3,20})$", c.strip()):
        raise ValueError(f"'{c}' is not a colour: use a hex code (e.g. #e34948) or a CSS colour name (e.g. red)")
    return NAMED.get(c.strip().lower(), c.strip())


# ---------------------------------------------------------------------------- value matching
def _norm(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f.is_integer() else repr(f)
    except (TypeError, ValueError):
        return str(v).strip().lower()


def _date_prefix(v) -> str | None:
    """'2026-03', 'Mar 2026', 'March 2026' -> '2026-03'; a full date -> 'YYYY-MM-DD'."""
    s = str(v).strip()
    if not re.search(r"\d{4}", s):   # needs a year; avoids reading '3' or 'Line 2' as a date
        return None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ts = pd.to_datetime(s)
    except (ValueError, TypeError):
        return None
    return ts.strftime("%Y-%m-%d") if re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}", s) else ts.strftime("%Y-%m")


def _matches(data_value, wanted) -> bool:
    if _norm(data_value) == _norm(wanted):
        return True
    if isinstance(data_value, str) and _ISO.match(data_value):
        p = _date_prefix(wanted)
        return bool(p) and data_value.startswith(p)
    return False


def _natural_key(v):
    """'<18' < '10-19' < '20-29' ... < 'Line 10' ...; text without numbers after; Unknown/Other last."""
    t = str(v)
    if t.strip().lower() in ("unknown", "other", "none", ""):
        return (2, 0.0, t)
    m = re.search(r"-?\d+(\.\d+)?", t)
    return (0, float(m.group()) - (1e9 if t.lstrip().startswith("<") else 0), t) if m else (1, 0.0, t)


def _distinct(spec: dict, field: str) -> list:
    return list(dict.fromkeys(r.get(field) for r in _rows(spec)))


def _describe_fields(spec: dict) -> str:
    parts = []
    for c in _columns(spec):
        vals = _distinct(spec, c)
        sample = ", ".join(map(str, vals[:12])) + (", ..." if len(vals) > 12 else "")
        parts.append(f"{c} [{sample}]")
    return "; ".join(parts)


def _find_field(spec: dict, values: list, field: str | None, prefer: list[str]) -> tuple[str, list]:
    """Field whose data contains every requested value, and the actual data values matched."""
    cols = _columns(spec)
    if field:
        if field not in cols:
            raise ValueError(f"field '{field}' is not in this chart. Fields and values: {_describe_fields(spec)}")
        candidates = [field]
    else:
        candidates = list(dict.fromkeys([f for f in prefer if f in cols] + cols))
    for f in candidates:
        dv = _distinct(spec, f)
        hits = [[d for d in dv if _matches(d, w)] for w in values]
        if all(hits):
            return f, list(dict.fromkeys(d for h in hits for d in h))
    raise ValueError(f"could not find {values} in the chart data. Fields and values: {_describe_fields(spec)}")


def _axis_order(spec: dict, field: str) -> list:
    """Display order of a categorical field: its explicit sort list if any, else data order."""
    for v in _views(spec):
        for ch in v.get("encoding", {}).values():
            if isinstance(ch, dict) and ch.get("field") == field and isinstance(ch.get("sort"), list):
                return ch["sort"]
    for ch in spec.get("encoding", {}).values():
        if isinstance(ch, dict) and ch.get("field") == field and isinstance(ch.get("sort"), list):
            return ch["sort"]
    return _distinct(spec, field)


def _position(order: list, bound, field: str) -> int:
    for i, v in enumerate(order):
        if _matches(v, bound):
            return i
    raise ValueError(f"'{bound}' is not a value of {field}. Values in order: {order[:40]}")


# ---------------------------------------------------------------------------- ops
def _op_highlight(spec, op):
    values = op.get("values") or ([op["value"]] if "value" in op else [])
    if not values:
        raise ValueError("highlight needs `values` (the categories/series to call out)")
    series = _series(spec)
    cats = [f for _, f in _cat_channels(spec)]
    field, actual = _find_field(spec, values, op.get("field"), [series, *cats] if series else cats)
    colour = _color(op["color"]) if op.get("color") else None
    if field == series:
        _recolor_series(spec, {a: colour for a in actual}, dim_others=True)
    else:
        if any(_mark_type(v) in ("line", "area") for v in _mark_views(spec)):
            raise ValueError(f"a line/area chart can't highlight individual {field} values; use a 'band' op over "
                             f"that {field} range, or 'highlight' a series")
        for v in _mark_views(spec):
            enc = v.setdefault("encoding", {})
            test = {"field": field, "oneOf": actual}
            if series or "field" in enc.get("color", {}):   # colour already carries data: fade the rest instead
                enc["opacity"] = {"condition": {"test": test, "value": 1}, "value": 0.25}
            else:
                base = colour or (v["mark"].get("color") if isinstance(v["mark"], dict) else None) or PRIMARY
                enc["color"] = {"condition": {"test": test, "value": base}, "value": DIM}
    return f"highlighted {', '.join(map(str, actual))}" + (f" in {op['color']}" if colour else "")


def _recolor_series(spec, mapping: dict, dim_others: bool = False) -> None:
    for holder in [spec, *_views(spec)]:
        c = holder.get("encoding", {}).get("color")
        if not c or "field" not in c:
            continue
        scale = c.setdefault("scale", {})
        domain = list(scale.get("domain") or (c["sort"] if isinstance(c.get("sort"), list) else None)
                      or _distinct(spec, c["field"]))
        rng = list(scale.get("range") or [CATEGORICAL[i % len(CATEGORICAL)] for i in range(len(domain))])
        rng += [CATEGORICAL[i % len(CATEGORICAL)] for i in range(len(rng), len(domain))]
        for i, d in enumerate(domain):
            if d in mapping:
                rng[i] = mapping[d] or rng[i]
            elif dim_others:
                rng[i] = DIM
        scale["domain"], scale["range"] = domain, rng


def _op_color(spec, op):
    series = _series(spec)
    if op.get("map"):
        mapping = {k: _color(v) for k, v in op["map"].items()}
        cats = [f for _, f in _cat_channels(spec)]
        field, _ = _find_field(spec, list(mapping), op.get("field"), [series, *cats] if series else cats)
        resolved = {}
        for want, col in mapping.items():
            for d in _distinct(spec, field):
                if _matches(d, want):
                    resolved[d] = col
        if field == series:
            _recolor_series(spec, resolved)
        else:
            for v in _mark_views(spec):
                enc = v.setdefault("encoding", {})
                old = enc.get("color", {})
                base = old.get("value") or (v["mark"].get("color") if isinstance(v["mark"], dict) else None) or PRIMARY
                conds = old.get("condition", [])
                conds = conds if isinstance(conds, list) else [conds]
                enc["color"] = {"condition": [{"test": {"field": field, "equal": d}, "value": c}
                                              for d, c in resolved.items()] + conds, "value": base}
        return "recoloured " + ", ".join(f"{k} -> {v}" for k, v in op["map"].items())
    if op.get("color"):
        c = _color(op["color"])
        heat = _enc(spec).get("color", {})
        if heat.get("type") == "quantitative":   # heatmap: recolour the sequential ramp
            for holder in [spec, *_views(spec)]:
                hc = holder.get("encoding", {}).get("color")
                if hc and hc.get("type") == "quantitative":
                    hc.setdefault("scale", {})["range"] = ["#f4f6f8", c]
            return f"heatmap shaded in {op['color']}"
        if series:
            raise ValueError(f"this chart has one colour per {series}; pass `map` {{value: colour}} instead")
        for v in _mark_views(spec):   # every mark this colour; earlier highlights are cleared (apply highlight after)
            _set_mark(v, color=c)
            enc = v.get("encoding", {})
            if "color" in enc and "field" not in enc["color"]:
                enc.pop("color")
            enc.pop("opacity", None)
        return f"coloured the chart {op['color']}"
    raise ValueError("color needs `color` (whole chart) or `map` {value: colour}")


def _op_filter(spec, op):
    rows = _rows(spec)
    cats = [f for _, f in _cat_channels(spec)]
    keep = rows
    if op.get("values") or op.get("exclude"):
        wanted = op.get("values") or op.get("exclude")
        series = _series(spec)
        field, actual = _find_field(spec, wanted, op.get("field"), [*cats, series] if series else cats)
        hit = {_norm(a) for a in actual}
        inside = lambda r: _norm(r.get(field)) in hit
        keep = [r for r in rows if inside(r) == bool(op.get("values"))]
        desc = f"{'kept only' if op.get('values') else 'removed'} {', '.join(map(str, actual))}"
    elif "from" in op or "to" in op:
        field = op.get("field") or (cats[0] if cats else None)
        if not field or field not in _columns(spec):
            raise ValueError(f"filter needs `field` for a range. Fields and values: {_describe_fields(spec)}")
        lo, hi = op.get("from"), op.get("to")
        vals = [r.get(field) for r in rows]
        if all(isinstance(v, (int, float)) for v in vals if v is not None) and \
                all(b is None or isinstance(b, (int, float)) for b in (lo, hi)):
            inside = lambda r: (lo is None or r[field] >= lo) and (hi is None or r[field] <= hi)
        elif all(isinstance(v, str) and _ISO.match(v) for v in vals if v is not None):
            plo, phi = (_date_prefix(b) if b is not None else None for b in (lo, hi))
            if (lo is not None and not plo) or (hi is not None and not phi):
                raise ValueError(f"'{lo}'/'{hi}' are not dates; {field} holds dates like {vals[0]}")
            inside = lambda r: (plo is None or r[field][:len(plo)] >= plo) and (phi is None or r[field][:len(phi)] <= phi)
        else:
            order = _axis_order(spec, field)
            i = _position(order, lo, field) if lo is not None else 0
            j = _position(order, hi, field) if hi is not None else len(order) - 1
            span = {_norm(v) for v in order[min(i, j):max(i, j) + 1]}
            inside = lambda r: _norm(r.get(field)) in span
        keep = [r for r in rows if inside(r)]
        desc = f"showing {field} from {lo if lo is not None else 'start'} to {hi if hi is not None else 'end'}"
    elif op.get("top"):
        m = _measure(spec)
        field = op.get("field") or (cats[0] if cats else _series(spec))
        by = op.get("by") or (m[1] if m else None)
        if not field or not by:
            raise ValueError("filter `top` needs a category `field` and a measure `by`")
        totals = pd.DataFrame(rows).groupby(field, sort=False)[by].sum().sort_values(ascending=False)
        top = set(map(_norm, totals.head(int(op["top"])).index))
        keep = [r for r in rows if _norm(r.get(field)) in top]
        desc = f"kept the top {int(op['top'])} {field} by {by}"
    else:
        raise ValueError("filter needs `values`, `exclude`, `from`/`to`, or `top`")
    if not keep:
        raise ValueError(f"that filter leaves no data. Fields and values: {_describe_fields(spec)}")
    if all("share" in r for r in keep) and (theta := _enc(spec).get("theta", {}).get("field")):
        total = sum(r[theta] or 0 for r in keep) or 1   # part-to-whole charts: shares of what's left
        keep = [{**r, "share": (r[theta] or 0) / total} for r in keep]
    spec["data"]["values"] = keep
    return desc


def _op_axis_range(spec, op):
    axis = op.get("axis", "y")
    view = (_mark_views(spec) or [spec])[0]
    ch = _enc(spec, view).get(axis)
    if not ch or ch.get("type") != "quantitative":
        raise ValueError(f"the {axis} axis is not numeric here; use `filter` with from/to to limit categories or dates")
    vals = [r[ch["field"]] for r in _rows(spec) if isinstance(r.get(ch["field"]), (int, float))]
    lo = op.get("min", min(0, min(vals, default=0)))
    hi = op.get("max", max(vals, default=1))
    # clamp rather than mark clipping: with width "container", vega sizes clip rects before layout (0px wide bars)
    for holder in [spec, *_views(spec)]:
        enc = holder.get("encoding", {})
        if axis in enc and enc[axis].get("type") == "quantitative":
            enc[axis].setdefault("scale", {}).update(domain=[lo, hi], clamp=True)
    return f"{axis} axis from {lo} to {hi}"


def _datum_channel(spec, axis: str, value):
    ch = _enc(spec).get(axis)
    if not ch or "field" not in ch:
        raise ValueError(f"this chart has no {axis} axis to draw on (fields: {_columns(spec)})")
    if _mark_type((_mark_views(spec) or [spec])[0]) == "arc":
        raise ValueError("reference lines and bands don't apply to donut charts")
    out = {"datum": value, "type": ch["type"]}
    if ch.get("type") == "temporal":
        p = _date_prefix(value)
        if not p:
            raise ValueError(f"'{value}' is not a date; the {axis} axis holds dates like {_rows(spec)[0][ch['field']]}")
        y, m, *d = map(int, p.split("-"))
        out["datum"] = {"year": y, "month": m, "date": d[0] if d else 1}   # vega DateTime, not a string
    elif ch.get("type") != "quantitative":
        order = _axis_order(spec, ch["field"])
        out["datum"] = order[_position(order, value, ch["field"])]
    return out


def _op_reference_line(spec, op):
    axis = op.get("axis", "y")
    if "value" not in op:
        raise ValueError("reference_line needs `value` (in the axis's units; percent axes use fractions, 0.3 = 30%)")
    colour = _color(op.get("color", "#c2362f"))
    pos = _datum_channel(spec, axis, op["value"])
    _layered(spec)
    rule = {"description": OVERLAY, "data": ONE_ROW, "mark": {"type": "rule", "color": colour, "strokeDash": [5, 4], "strokeWidth": 1.5},
            "encoding": {axis: pos}}
    spec["layer"].append(rule)
    if op.get("label"):
        other = "x" if axis == "y" else "y"
        txt = {"description": OVERLAY, "data": ONE_ROW, "mark": {"type": "text", "color": colour, "fontSize": 11, "fontWeight": 600,
                        **({"align": "right", "baseline": "bottom", "dy": -4} if axis == "y"
                           else {"align": "left", "baseline": "top", "dx": 4, "angle": 0})},
               "encoding": {axis: pos, other: {"value": "width" if axis == "y" else 0},
                            "text": {"value": op["label"]}}}
        spec["layer"].append(txt)
    return f"reference line at {axis} = {op['value']}" + (f" ('{op['label']}')" if op.get("label") else "")


def _op_band(spec, op):
    axis = op.get("axis", "x")
    if "from" not in op or "to" not in op:
        raise ValueError("band needs `from` and `to` on the axis")
    lo, hi = _datum_channel(spec, axis, op["from"]), _datum_channel(spec, axis, op["to"])
    colour = _color(op.get("color", "#c98b27"))
    band_pos = _enc(spec).get(axis, {}).get("type") in ("nominal", "ordinal")
    if band_pos:
        lo["bandPosition"], hi["bandPosition"] = 0, 1
    _layered(spec)
    rect = {"description": OVERLAY, "data": ONE_ROW, "mark": {"type": "rect", "color": colour, "opacity": 0.13},
            "encoding": {axis: lo, f"{axis}2": {k: v for k, v in hi.items() if k != "type"}}}
    spec["layer"].insert(0, rect)   # under the data marks
    if op.get("label"):
        spec["layer"].append({"description": OVERLAY, "data": ONE_ROW, "mark": {"type": "text", "color": colour, "fontSize": 11, "fontWeight": 600,
                                       "align": "left", "baseline": "top", "dx": 4, "dy": 4},
                              "encoding": {axis: lo, ("y" if axis == "x" else "x"): {"value": 0},
                                           "text": {"value": op["label"]}}})
    return f"shaded {axis} from {op['from']} to {op['to']}" + (f" ('{op['label']}')" if op.get("label") else "")


def _op_chart_type(spec, op):
    kind = op.get("type")
    if kind not in ("bar", "line", "area", "point"):
        raise ValueError("chart_type `type` must be bar, line, area or point")
    views = [v for v in _mark_views(spec) if _mark_type(v) in ("bar", "line", "area", "point")]
    if not views:
        raise ValueError("this chart type (donut/heatmap) can't be switched; build a new chart with run_sql instead")
    for v in views:
        m = v["mark"] if isinstance(v["mark"], dict) else {"type": v["mark"]}
        m = {k: val for k, val in m.items() if k in ("color", "clip", "tooltip")}
        m["type"] = kind
        if kind == "line":
            m["point"] = True
        if kind == "area":
            m["opacity"] = 0.35
            m["line"] = True
        v["mark"] = m
        enc = v.get("encoding", {})
        if kind != "bar" and "xOffset" in enc:   # grouped bars -> one line/area per series
            enc.pop("xOffset")
    return f"switched to a {kind} chart"


def _op_sort(spec, op):
    by, order = op.get("by", "value"), op.get("order", "desc")
    cats = _cat_channels(spec)
    if not cats:
        raise ValueError("this chart has no category axis to sort")
    ch, field = cats[0]
    m = _measure(spec)
    if by == "value":
        if not m:
            raise ValueError("no numeric measure to sort by")
        sort = {"field": m[1], "op": "sum", "order": "descending" if order == "desc" else "ascending"}
    elif by == "label":
        vals = sorted(_distinct(spec, field), key=_natural_key)
        sort = vals[::-1] if order == "desc" else vals
    elif by == "custom":
        want = op.get("values") or []
        order_now = _distinct(spec, field)
        sort = [d for w in want for d in order_now if _matches(d, w)]
        sort += [d for d in order_now if d not in sort]
    else:
        raise ValueError("sort `by` must be value, label or custom")
    for holder in [spec, *_views(spec)]:
        enc = holder.get("encoding", {})
        if ch in enc and enc[ch].get("field") == field:
            enc[ch]["sort"] = sort
    return f"sorted {field} by {by} ({order})"


def _op_labels(spec, op):
    show = op.get("show", True)
    m = _measure(spec)
    text_layers = [l for l in spec.get("layer", []) if _mark_type(l) == "text"
                   and l.get("encoding", {}).get("text", {}).get("field")]
    if not show:
        spec["layer"] = [l for l in spec.get("layer", []) if l not in text_layers]
        return "removed value labels"
    if text_layers:
        return "value labels already shown"
    views = [v for v in _mark_views(spec) if _mark_type(v) in ("bar", "line", "point")]
    if not views or not m:
        raise ValueError("value labels can be added to bar, line and point charts only")
    base = views[0]
    enc = {k: v for k, v in _enc(spec, base).items() if k in ("x", "y", "xOffset")}
    fmt = enc.get(m[0], {}).get("axis", {}).get("format")
    enc["text"] = {"field": m[1], "type": "quantitative", **({"format": fmt} if fmt else {})}
    horizontal = m[0] == "x"
    _layered(spec)
    spec["layer"].append({"mark": {"type": "text", "color": "#52514e", "fontSize": 11,
                                   **({"align": "left", "dx": 4} if horizontal else {"baseline": "bottom", "dy": -4})},
                          "encoding": enc})
    return "added value labels"


def _op_title(spec, op):
    t = spec.setdefault("title", {})
    if isinstance(t, str):
        t = spec["title"] = {"text": t}
    if op.get("text"):
        t["text"] = op["text"]
    if "subtitle" in op:
        t["subtitle"] = op["subtitle"] or None
    return "retitled the chart"


def _op_format(spec, op):
    fmt = FORMATS.get(op.get("format", ""))
    if not fmt:
        raise ValueError(f"format must be one of {list(FORMATS)} (percent formats expect fractions)")
    m = _measure(spec)
    axis = op.get("axis") or (m[0] if m else "y")
    for holder in [spec, *_views(spec)]:
        enc = holder.get("encoding", {})
        if axis in enc and enc[axis].get("type") == "quantitative":
            enc[axis].setdefault("axis", {})["format"] = fmt
        if "text" in enc and "field" in enc["text"]:
            enc["text"]["format"] = fmt
    return f"{axis} values formatted as {op['format']}"


def _op_legend(spec, op):
    pos = op.get("position", "top")
    if pos not in ("top", "bottom", "right", "left", "none"):
        raise ValueError("legend position must be top, bottom, right, left or none")
    for holder in [spec, *_views(spec)]:
        c = holder.get("encoding", {}).get("color")
        if c and "field" in c:
            c["legend"] = None if pos == "none" else {**(c.get("legend") or {}), "orient": pos}
    return "hid the legend" if pos == "none" else f"legend at {pos}"


def _op_size(spec, op):
    h = op.get("height")
    if not isinstance(h, int) or not 120 <= h <= 900:
        raise ValueError("size needs an integer `height` between 120 and 900 (pixels)")
    spec["height"] = h
    return f"height {h}px"


_HANDLERS = {n: globals()[f"_op_{n}"] for n in OPS}


def apply(spec: dict, ops: list[dict]) -> tuple[dict, list[str]]:
    """Return an edited copy of `spec` and a description of each change. Raises ValueError on a bad op."""
    if not isinstance(ops, list) or not ops:
        raise ValueError(f"ops must be a non-empty list of edits, each with `op` in {list(OPS)}")
    spec = copy.deepcopy(spec)
    done = []
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or op.get("op") not in _HANDLERS:
            raise ValueError(f"ops[{i}]: `op` must be one of {list(OPS)}")
        try:
            done.append(_HANDLERS[op["op"]](spec, op))
        except ValueError as e:
            raise ValueError(f"ops[{i}] ({op['op']}): {e}") from None
    return spec, done


def describe(spec: dict) -> str:
    """One line on what the chart now shows, for the model to check against the request."""
    rows = _rows(spec)
    cats = _cat_channels(spec)
    parts = [f"{len(rows)} data rows"]
    for _, f in cats:
        vals = _distinct(spec, f)
        parts.append(f"{f}: {len(vals)} values ({', '.join(map(str, vals[:8]))}{', ...' if len(vals) > 8 else ''})")
    if (s := _series(spec)) and s not in [f for _, f in cats]:
        parts.append(f"series {s}: {', '.join(map(str, _distinct(spec, s)))}")
    kinds = sorted({_mark_type(v) for v in _views(spec) if _mark_type(v)})
    parts.append("marks: " + ", ".join(kinds))
    return "; ".join(parts)
