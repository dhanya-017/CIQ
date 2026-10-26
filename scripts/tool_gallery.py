"""Render every tool's output (charts + insights + caveats) into one local HTML page for review.

Run: python scripts/tool_gallery.py [PRODUCT]   -> docs/tool_gallery.html
"""
import html
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ciq.tools.registry import TOOLS, run_tool

OUT = Path(__file__).resolve().parent.parent / "docs" / "tool_gallery.html"


def main(product: str | None = None) -> None:
    sections, specs = [], []
    for t in TOOLS:
        if t.name in ("run_sql", "data_overview"):
            continue
        args = {"product": product} if product and "product" in t.properties else {}
        if t.name == "compare_products":
            args = {"products": [product or "KEYTRUDA", "OPDIVO", "TECENTRIQ"]}
        res = run_tool(t.name, args).to_dict()
        divs = []
        for spec in res["charts"]:
            specs.append(spec)
            divs.append(f'<div class="chart" id="c{len(specs) - 1}"></div>')
        lis = "".join(f"<li>{html.escape(i)}</li>" for i in res["insights"])
        cav = "".join(f"<li>{html.escape(c)}</li>" for c in res["caveats"])
        sections.append(f"""<section><h2>{html.escape(res['title'])}</h2>
<p class="tool"><code>{t.name}</code> {html.escape(json.dumps(res['params']))}</p>
<div class="charts">{''.join(divs)}</div><ul class="ins">{lis}</ul>
{f'<details><summary>Caveats</summary><ul>{cav}</ul></details>' if cav else ''}</section>""")
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(f"""<!doctype html><html><head><meta charset="utf-8"><title>LoTrace tool gallery</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
<style>
body{{font-family:Inter,system-ui,sans-serif;background:#fcfcfb;color:#0b0b0b;margin:0;padding:24px 16px;max-width:1200px;margin:auto}}
section{{border-top:1px solid #e8e7e3;padding:20px 0}} h2{{margin:0 0 4px;font-size:18px}}
.tool{{color:#52514e;font-size:12px;margin:0 0 12px}} .charts{{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}}
.chart{{width:100%;min-width:0}} .ins li{{margin:2px 0}} details{{color:#52514e;font-size:13px}}
@media (max-width:600px){{.charts{{grid-template-columns:1fr}}}}
</style></head><body><h1>LoTrace analytics tools{' - ' + html.escape(product) if product else ''}</h1>
{''.join(sections)}
<script>const specs={json.dumps(specs)};specs.forEach((s,i)=>vegaEmbed('#c'+i,s,{{actions:false}}));</script>
</body></html>""", encoding="utf-8")
    print(f"wrote {OUT} ({len(specs)} charts)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
