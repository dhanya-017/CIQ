"""Line-of-therapy (LOT) algorithm - oncology business rules.

Rules (numbers match the business-rules document; parameters live in ciq.config):
1. Gap rule: a difference of >= LOT_GAP_DAYS (60) days between consecutive administrations is a line break
   (line advancement), whatever drugs follow.
2. Adding a new drug to the regimen after the regimen window is line advancement.
3. Removing a drug from the regimen (e.g. chemo-IO induction -> IO maintenance) is NOT line advancement.
4. A new drug that first appears after the regimen window (the first 28 days AND the first 2 cycles) is line
   advancement.
5. Regimen window: every drug given in the first LOT_REGIMEN_WINDOW_DAYS (28) days of the line, and in its first
   LOT_REGIMEN_WINDOW_CYCLES (2) cycles (administration occasions) if those end later, defines the regimen. The HCP may
   still be adjusting drugs/schedule, so changes inside the window do not advance the line.
6. Switching to a completely new product is line advancement (a special case of rules 2/4).
7. Switching to a biosimilar or a different formulation (e.g. IV -> subcutaneous) is NOT line advancement: drugs are
   compared by molecule, and biosimilar / SC products map to their reference molecule in ciq/reference; molecules
   listed together in LOT_EQUIVALENT_MOLECULES are also treated as the same drug.

Only LUNG_SYSTEMIC therapies define lines (supportive care and other-tumour drugs are ignored).
Line end = last administration + days supply - 1, capped at the next line start - 1 and at data end.
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
                           window: int = config.LOT_REGIMEN_WINDOW_DAYS,
                           window_cycles: int = config.LOT_REGIMEN_WINDOW_CYCLES,
                           gap_days: int = config.LOT_GAP_DAYS):
    """events: patient_id, service_date, molecule, product, drug_class, days_supply (LUNG_SYSTEMIC only)."""
    events = events.copy()
    events["service_date"] = pd.to_datetime(events["service_date"])
    events = events.sort_values(["patient_id", "service_date", "molecule"])
    info = events.drop_duplicates("molecule").set_index("molecule")[["product", "drug_class"]].to_dict("index")

    line_rows, drug_rows = [], []
    for pid, pev in events.groupby("patient_id", sort=False):
        lines = []
        cur = None
        for date, day in pev.groupby("service_date", sort=True):
            mols = set(day["molecule"])
            if cur is None:
                cur = _new_line(date, mols, "First therapy observed", window)
                lines.append(cur)
            else:
                gap = (date - cur["last"]).days
                cur_admin = cur["n_admin_days"] + 1
                if cur_admin == window_cycles:   # rule 5: the window also covers the first N cycles
                    cur["window_end"] = max(cur["window_end"], date)
                new_drugs = sorted(m for m in mols if canon(m) not in cur["canon"])
                if gap >= gap_days:                                              # rule 1
                    prev = cur
                    cur = _new_line(date, mols, f"Gap of {gap} days (>= {gap_days}) since the last administration",
                                    window)
                    cur["retreatment"] = {canon(m) for m in mols} <= prev["canon"]
                    lines.append(cur)
                elif date <= cur["window_end"]:                                  # rule 5
                    cur["regimen"] |= mols
                    cur["canon"] |= {canon(m) for m in mols}
                    cur["n_admin_days"] = cur_admin
                elif new_drugs:                                                  # rules 2, 4, 6
                    cur = _new_line(date, mols, f"New drug after the regimen window: {', '.join(new_drugs)}", window)
                    lines.append(cur)
                else:                                                            # rules 3, 7: same or fewer drugs
                    cur["n_admin_days"] = cur_admin
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
                regimen_window_end=ln["window_end"].date(),
                line_start_reason=ln["reason"],
                is_retreatment=ln["retreatment"],
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
    for df, cols in ((lot, ["start_date", "end_date", "last_admin_date", "regimen_window_end"]),
                     (lot_drug, ["first_date", "last_date"])):
        for c in cols:
            df[c] = pd.to_datetime(df[c])
    return lot, lot_drug


def _new_line(date, mols, reason: str, window: int) -> dict:
    return dict(start=date, last=date, regimen=set(mols), canon={canon(m) for m in mols}, events=[],
                n_admin_days=1, window_end=date + pd.Timedelta(days=window - 1),   # days 1..28 of the line
                reason=reason, retreatment=False)
