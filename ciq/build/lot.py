"""Line-of-therapy (LOT) algorithm.

Business rules (configurable in ciq.config):
1. Only LUNG_SYSTEMIC therapies define lines (supportive care and other-tumour drugs are ignored).
2. A line starts at the first therapy date; every drug started within LOT_REGIMEN_WINDOW_DAYS of the
   line start is part of that line's regimen.
3. After the window, starting a drug that is not in the regimen begins a new line
   (swaps between equivalent molecules, e.g. carboplatin <-> cisplatin, do not).
4. Dropping drugs (e.g. chemo -> IO maintenance) does not start a new line.
5. A gap > LOT_GAP_DAYS between administrations ends the line; if therapy resumes with only drugs
   from the same regimen it is a re-treatment of the same line, otherwise a new line.
6. Line end = last administration + days supply - 1, capped at next line start - 1 and data end.
"""
from __future__ import annotations

import pandas as pd

from ciq import config


def _canon_map() -> dict[str, str]:
    m = {}
    for group in config.LOT_EQUIVALENT_MOLECULES:
        key = "|".join(sorted(group))
        for mol in group:
            m[mol] = key
    return m


CANON = _canon_map()


def canon(molecule: str) -> str:
    return CANON.get(molecule, molecule)


def combo_type(n: int) -> str:
    return {1: "Monotherapy", 2: "Doublet", 3: "Triplet"}.get(n, "Quadruplet+")


def build_lines_of_therapy(events: pd.DataFrame, data_start: pd.Timestamp, data_end: pd.Timestamp,
                           window: int = config.LOT_REGIMEN_WINDOW_DAYS, gap_days: int = config.LOT_GAP_DAYS):
    """events: patient_id, service_date, molecule, product, drug_class, days_supply (LUNG_SYSTEMIC only)."""
    events = events.copy()
    events["service_date"] = pd.to_datetime(events["service_date"])
    events = events.sort_values(["patient_id", "service_date", "molecule"])
    info = events.drop_duplicates("molecule").set_index("molecule")[["product", "drug_class"]].to_dict("index")

    line_rows, drug_rows = [], []
    for pid, pev in events.groupby("patient_id", sort=False):
        lines = []  # each: dict(start, last, regimen(set mol), canon(set), events list, restart)
        cur = None
        for date, day in pev.groupby("service_date", sort=True):
            mols = set(day["molecule"])
            if cur is None:
                cur = _new_line(date, mols)
                lines.append(cur)
            else:
                in_window = (date - cur["start"]).days <= window
                gap = (date - cur["last"]).days
                is_new_drug = any(canon(m) not in cur["canon"] for m in mols)
                if in_window:
                    cur["regimen"] |= mols
                    cur["canon"] |= {canon(m) for m in mols}
                elif is_new_drug:
                    cur = _new_line(date, mols)
                    lines.append(cur)
                elif gap > gap_days:
                    cur["restart"] = True
            cur["last"] = date
            for r in day.itertuples(index=False):
                cur["events"].append((r.service_date, r.molecule, int(r.days_supply)))

        for i, ln in enumerate(lines, start=1):
            next_start = lines[i]["start"] if i < len(lines) else None
            end = max(d + pd.Timedelta(days=ds - 1) for d, _, ds in ln["events"])
            if next_start is not None:
                end = min(end, next_start - pd.Timedelta(days=1))
            end = min(end, data_end)
            regimen = sorted(ln["regimen"])
            products = sorted({info[m]["product"] for m in regimen})
            classes = sorted({info[m]["drug_class"] for m in regimen})
            ev = pd.DataFrame(ln["events"], columns=["d", "molecule", "ds"])
            line_rows.append(dict(
                patient_id=pid, line_number=i,
                start_date=ln["start"].date(), end_date=end.date(), last_admin_date=ln["last"].date(),
                duration_days=(end - ln["start"]).days + 1,
                regimen=" + ".join(products), regimen_molecules=" + ".join(regimen),
                regimen_classes=" + ".join(classes),
                n_drugs=len(products), combination_type=combo_type(len(products)),
                n_admin_days=ev["d"].nunique(),
                had_restart=ln["restart"],
                is_last_line=next_start is None,
                is_ongoing_at_data_end=(next_start is None) and (end >= data_end - pd.Timedelta(days=30)),
            ))
            for mol, g in ev.groupby("molecule"):
                drug_rows.append(dict(
                    patient_id=pid, line_number=i, molecule=mol,
                    product=info[mol]["product"], drug_class=info[mol]["drug_class"],
                    in_initial_regimen=mol in ln["regimen"],
                    first_date=g["d"].min().date(), last_date=g["d"].max().date(),
                    n_admin_days=g["d"].nunique(),
                ))

    lot = pd.DataFrame(line_rows)
    lot_drug = pd.DataFrame(drug_rows)
    for df, cols in ((lot, ["start_date", "end_date", "last_admin_date"]), (lot_drug, ["first_date", "last_date"])):
        for c in cols:
            df[c] = pd.to_datetime(df[c])
    return lot, lot_drug


def _new_line(date, mols):
    return dict(start=date, last=date, regimen=set(mols), canon={canon(m) for m in mols}, events=[], restart=False)
