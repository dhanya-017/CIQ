"""Download the public CMS ICD-10-CM code descriptions into ciq/reference/icd10cm.csv.

Used to describe diagnosis codes when the claims input carries no descriptions (master_nsclc.csv).
Run: python scripts/fetch_icd10cm.py [fiscal_year]     (default 2027)
"""
import csv
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "ciq" / "reference" / "icd10cm.csv"


def main(fy: str = "2027") -> None:
    url = f"https://www.cms.gov/files/zip/{fy}-code-descriptions-tabular-order.zip"
    data = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=60).read()
    z = zipfile.ZipFile(io.BytesIO(data))
    name = next(n for n in z.namelist() if n.endswith(f"icd10cm_order_{fy}.txt"))
    rows = []
    for line in z.read(name).decode("latin-1").splitlines():
        # fixed width: order(5) code(7) billable(1) short desc(60) long desc (long desc not kept: the app uses short)
        code = line[6:13].strip()
        if not code:
            continue
        dotted = code if len(code) <= 3 else f"{code[:3]}.{code[3:]}"
        rows.append((dotted, line[14:15], line[16:76].strip()))
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["code", "is_billable", "short_desc"])
        w.writerows(rows)
    print(f"wrote {len(rows):,} ICD-10-CM codes (FY{fy}) -> {OUT}")


if __name__ == "__main__":
    main(*sys.argv[1:2])
