"""Validate a master_nsclc-layout input before building: errors stop the build, warnings are reported.

Run on its own:  python -m ciq.build.build_warehouse --validate-only [--input PATH]
The checks run on the loaded, canonical input table (see ciq/build/load.py), so they see exactly what the build
will use.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ciq import config
from ciq.build.inputs import SOURCE_VALUES, InputInfo


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines = ["Input validation: " + ("PASSED" if self.ok else "FAILED")]
        lines += [f"  {k}: {v}" for k, v in self.facts.items()]
        lines += [f"  ERROR   {e}" for e in self.errors]
        lines += [f"  WARNING {w}" for w in self.warnings]
        return "\n".join(lines)


def check_header(info: InputInfo) -> Report:
    r = Report()
    r.facts["file"] = str(info.path)
    r.facts["layout"] = info.layout
    if info.layout == "unknown":
        r.errors.append("Unrecognised file: none of the required columns found. Expected (any accepted spelling): "
                        + ", ".join(info_required_help()))
    elif info.missing_required:
        r.errors.append(f"Missing required column(s): {', '.join(info.missing_required)}. "
                        f"Accepted spellings: {', '.join(info_required_help(info.missing_required))}")
    if info.mapping:
        renamed = {c: h for c, h in info.mapping.items() if h != c}
        if renamed:
            r.facts["columns mapped"] = ", ".join(f"{h} -> {c}" for c, h in renamed.items())
    if info.unmapped:
        r.warnings.append(f"Column(s) not used by the build: {', '.join(info.unmapped)}")
    return r


def info_required_help(cols: list[str] | None = None) -> list[str]:
    from ciq.build.inputs import ALIASES, REQUIRED
    return [f"{c} ({'/'.join(ALIASES[c][:4])})" for c in (cols or REQUIRED)]


def check_data(con, table: str, r: Report) -> Report:
    """Content checks on the canonical input table `table` (text columns + parsed service_date, clean source)."""
    q = lambda s: con.execute(s.format(t=table)).fetchone()
    n, n_pat = q("select count(*), count(distinct patient_id) from {t}")
    r.facts["rows"] = f"{n:,}"
    r.facts["patients"] = f"{n_pat:,}"
    if n == 0:
        r.errors.append("The file has no data rows.")
        return r

    bad_dates, d0, d1 = q("select count(*) filter (where service_date is null and svc_dt is not null), "
                          "min(service_date), max(service_date) from {t}")
    blank_dates = q("select count(*) from {t} where svc_dt is null or trim(svc_dt) = ''")[0]
    r.facts["service dates"] = f"{d0} to {d1}"
    if bad_dates:
        example = q("select any_value(svc_dt) from {t} where service_date is null and svc_dt is not null")[0]
        msg = f"{bad_dates:,} rows ({bad_dates / n:.1%}) have a service date in an unrecognised format, e.g. '{example}'"
        (r.errors if bad_dates / n > 0.05 else r.warnings).append(msg + (" - more than 5%, stopping" if bad_dates / n > 0.05 else " - these rows are dropped"))
    if blank_dates:
        r.warnings.append(f"{blank_dates:,} rows have no service date and are dropped.")
    if d0 is not None and (d0.year < 2000 or d1.year > 2100):
        r.warnings.append(f"Implausible service dates ({d0} to {d1}): check the date format (US month/day is assumed).")

    blank_pid = q("select count(*) from {t} where patient_id is null or trim(patient_id) = ''")[0]
    if blank_pid:
        (r.errors if blank_pid / n > 0.05 else r.warnings).append(f"{blank_pid:,} rows have no patient_id.")

    known = tuple(sorted(set(SOURCE_VALUES.values())))
    unknown = con.execute(f"select source, count(*) from {table} where source not in {known} or source is null "
                          "group by 1 order by 2 desc limit 5").fetchall()
    if unknown:
        n_unknown = sum(c for _, c in unknown)
        r.errors.append(f"{n_unknown:,} rows have an unknown source value ({', '.join(f'{s!r}: {c:,}' for s, c in unknown)}). "
                        f"Accepted: {', '.join(sorted(SOURCE_VALUES))}.")
    by_src = dict(con.execute(f"select source, count(*) from {table} group by 1").fetchall())
    r.facts["rows by source"] = ", ".join(f"{k} {v:,}" for k, v in sorted(by_src.items(), key=lambda kv: str(kv[0])))
    if not by_src.get("RX"):
        r.warnings.append("No pharmacy (RX) rows: oral drugs and pharmacy fills will be missing.")
    if not by_src.get("MX_PX"):
        r.warnings.append("No medical procedure (MX_PX) rows: infused drugs (J-codes) and procedures will be missing.")

    cohort = q(f"select count(distinct patient_id) from {{t}} where diagnosis_code like '{config.COHORT_DX_LIKE}'")[0]
    r.facts[f"cohort ({config.COHORT_DX_LIKE})"] = f"{cohort:,} patients"
    if cohort == 0:
        r.errors.append(f"No patient has a diagnosis matching {config.COHORT_DX_LIKE}: the cohort would be empty.")

    bad_ndc = q("select count(*) from {t} where source in ('RX', 'MX_RX') and code is not null "
                "and not regexp_matches(regexp_replace(trim(code), '\\.0$', ''), '^[0-9]{{1,11}}$')")[0]
    if bad_ndc:
        r.warnings.append(f"{bad_ndc:,} pharmacy/NDC codes are not numeric NDCs; those lines can't be matched by NDC.")
    return r
