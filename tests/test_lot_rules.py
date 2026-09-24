"""Line-of-therapy business rules (see ciq/build/lot.py): one test per rule, on synthetic patients.

Run: python -m pytest tests -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from ciq.build.lot import build_lines_of_therapy

START = pd.Timestamp("2026-01-01")
DRUGS = {  # molecule -> (product, class, days supply / cycle length)
    "pembrolizumab": ("KEYTRUDA", "PD-1/PD-L1 INHIBITOR", 21),
    "bevacizumab": ("BEVACIZUMAB", "ANTI-VEGF", 21),
    "carboplatin": ("CARBOPLATIN", "PLATINUM CHEMO", 21),
    "cisplatin": ("CISPLATIN", "PLATINUM CHEMO", 21),
    "pemetrexed": ("PEMETREXED", "ANTIMETABOLITE CHEMO", 21),
    "paclitaxel": ("PACLITAXEL", "TAXANE", 21),
    "nab-paclitaxel": ("NAB-PACLITAXEL", "TAXANE", 21),
    "ipilimumab": ("YERVOY", "CTLA-4 INHIBITOR", 42),
    "docetaxel": ("DOCETAXEL", "TAXANE", 21),
}


def lines_for(schedule: dict[int, list[str]]) -> pd.DataFrame:
    """schedule: {day number (1 = first day): [molecules given that day]} for one patient."""
    rows = [dict(patient_id="p1", service_date=START + pd.Timedelta(days=d - 1), molecule=m,
                 product=DRUGS[m][0], drug_class=DRUGS[m][1], days_supply=DRUGS[m][2])
            for d, mols in schedule.items() for m in mols]
    lot, _ = build_lines_of_therapy(pd.DataFrame(rows), START, pd.Timestamp("2026-12-31"))
    return lot


def test_business_illustration_is_one_line():
    # Day 1 A,B; day 7 C; day 21 A,C; day 35 A,B; day 42 A + biosimilar of B -> one line, regimen A+B+C.
    # (A biosimilar such as Mvasi maps to its reference molecule bevacizumab before lines are built.)
    lot = lines_for({1: ["pembrolizumab", "bevacizumab"], 7: ["carboplatin"], 21: ["pembrolizumab", "carboplatin"],
                     35: ["pembrolizumab", "bevacizumab"], 42: ["pembrolizumab", "bevacizumab"]})
    assert len(lot) == 1
    assert lot.iloc[0]["regimen_molecules"] == "bevacizumab + carboplatin + pembrolizumab"


def test_rule1_gap_of_60_days_is_a_new_line():
    lot = lines_for({1: ["pembrolizumab"], 61: ["pembrolizumab"]})       # 60 days apart
    assert len(lot) == 2 and "Gap of 60 days" in lot.iloc[1]["line_start_reason"]
    assert bool(lot.iloc[1]["is_retreatment"])                             # same drug resumed
    assert len(lines_for({1: ["pembrolizumab"], 60: ["pembrolizumab"]})) == 1   # 59 days apart


def test_rule2_4_new_drug_after_window_is_a_new_line():
    lot = lines_for({1: ["pembrolizumab"], 22: ["pembrolizumab"], 43: ["pembrolizumab", "pemetrexed"]})
    assert len(lot) == 2 and "pemetrexed" in lot.iloc[1]["line_start_reason"]


def test_rule3_removing_a_drug_is_not_a_new_line():
    lot = lines_for({1: ["pembrolizumab", "carboplatin", "pemetrexed"], 22: ["pembrolizumab", "pemetrexed"],
                     43: ["pembrolizumab"]})
    assert len(lot) == 1


def test_rule5_changes_inside_28_days_define_the_regimen():
    lot = lines_for({1: ["pembrolizumab"], 28: ["carboplatin"]})         # day 28 is still in the window
    assert len(lot) == 1 and lot.iloc[0]["n_drugs"] == 2
    # day 29, 3rd administration: past both 28 days and 2 cycles -> new line
    lot = lines_for({1: ["pembrolizumab"], 22: ["pembrolizumab"], 29: ["pembrolizumab", "carboplatin"]})
    assert len(lot) == 2
    # day 29 but only the 2nd administration: still within the first 2 cycles -> same line
    assert len(lines_for({1: ["pembrolizumab"], 29: ["pembrolizumab", "carboplatin"]})) == 1


def test_rule5_window_extends_to_the_second_cycle_for_slow_schedules():
    # q6w drug: the 2nd cycle starts on day 43, after 28 days; a drug added then still joins the regimen
    lot = lines_for({1: ["ipilimumab"], 43: ["ipilimumab", "pembrolizumab"]})
    assert len(lot) == 1 and lot.iloc[0]["n_drugs"] == 2


def test_rule6_complete_switch_is_a_new_line():
    lot = lines_for({1: ["carboplatin", "pemetrexed"], 22: ["carboplatin", "pemetrexed"], 43: ["docetaxel"]})
    assert len(lot) == 2 and lot.iloc[1]["regimen"] == "DOCETAXEL"


def test_rule6_platinum_swap_after_window_is_a_new_line():
    # carboplatin and cisplatin are different molecules (not a biosimilar or a new form of the same drug)
    lot = lines_for({1: ["cisplatin", "pemetrexed"], 22: ["cisplatin", "pemetrexed"], 43: ["carboplatin", "pemetrexed"]})
    assert len(lot) == 2


def test_rule7_formulation_switch_is_not_a_new_line():
    lot = lines_for({1: ["carboplatin", "paclitaxel"], 22: ["carboplatin", "paclitaxel"],
                     43: ["carboplatin", "nab-paclitaxel"]})
    assert len(lot) == 1
