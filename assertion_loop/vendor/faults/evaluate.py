#!/usr/bin/env python3
"""Evaluate a fixed fault pool as assertions are added, with one family held out.

Input matrix rows are (assertion_id, mutant_id, status). A status is CEX,
PROVEN, or UNKNOWN. The matrix is measured once; no Jasper call occurs here.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path


FAMILIES = ("F1", "F2", "F3", "F4", "F5")
STATUSES = {"CEX", "PROVEN", "UNKNOWN"}


def read_manifest(path: Path) -> tuple[list[str], dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    assertions = data.get("assertions")
    mutants = data.get("mutants")
    if not isinstance(assertions, list) or not all(isinstance(x, str) and x for x in assertions):
        raise ValueError("manifest.assertions must be a nonempty list of unique names")
    if not assertions or len(set(assertions)) != len(assertions):
        raise ValueError("assertion names must be nonempty and unique")
    if not isinstance(mutants, list):
        raise ValueError("manifest.mutants must be a list")
    family_by_mutant: dict[str, str] = {}
    for item in mutants:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("each mutant needs a string id and family")
        mutant_id, family = item["id"], item.get("family")
        if not mutant_id or mutant_id in family_by_mutant or family not in FAMILIES:
            raise ValueError(f"duplicate/invalid mutant id or family: {item}")
        family_by_mutant[mutant_id] = family
    # Collection also supports one-family development or test partitions.
    # analyze() still requires >=2 families for leave-one-class-out analysis.
    return assertions, family_by_mutant


def read_matrix(path: Path, assertions: list[str], mutants: dict[str, str]) -> dict[tuple[str, str], str]:
    expected = {(a, m) for a in assertions for m in mutants}
    matrix: dict[tuple[str, str], str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not {"assertion_id", "mutant_id", "status"}.issubset(reader.fieldnames or []):
            raise ValueError("matrix CSV needs assertion_id,mutant_id,status columns")
        for row in reader:
            key = (row["assertion_id"], row["mutant_id"])
            status = row["status"].strip().upper()
            if key not in expected or key in matrix or status not in STATUSES:
                raise ValueError(f"unexpected/duplicate cell or invalid status: {row}")
            matrix[key] = status
    missing = expected - matrix.keys()
    if missing:
        raise ValueError(f"matrix is incomplete: {len(missing)} missing cells, e.g. {sorted(missing)[0]}")
    return matrix


def covered(mutant_id: str, chosen: list[str], matrix: dict[tuple[str, str], str]) -> bool:
    return any(matrix[a, mutant_id] == "CEX" for a in chosen)


def rate(mutant_ids: list[str], chosen: list[str], matrix: dict[tuple[str, str], str]) -> float:
    return sum(covered(m, chosen, matrix) for m in mutant_ids) / len(mutant_ids)


def greedy_training_order(
    assertions: list[str], training_families: list[str], by_family: dict[str, list[str]],
    matrix: dict[tuple[str, str], str],
) -> list[str]:
    """Greedy class-macro gain; no held-out-family cell is read here."""
    chosen: list[str] = []
    remaining = set(assertions)
    detected = {family: set() for family in training_families}
    while remaining:
        def score(candidate: str) -> tuple[float, str]:
            gain = sum(
                sum(matrix[candidate, m] == "CEX" for m in by_family[f] if m not in detected[f])
                / len(by_family[f])
                for f in training_families
            ) / len(training_families)
            return gain, candidate

        # Deterministic tie-breaking, independent of held-out results.
        best = min(remaining, key=lambda a: (-score(a)[0], a))
        chosen.append(best)
        remaining.remove(best)
        for family in training_families:
            detected[family].update(m for m in by_family[family] if matrix[best, m] == "CEX")
    return chosen


def analyze(assertions: list[str], families: dict[str, str], matrix: dict[tuple[str, str], str]) -> tuple[list[dict], dict]:
    if len(set(families.values())) < 2:
        raise ValueError("held-out evaluation needs at least two nonempty fault families")
    by_family: dict[str, list[str]] = defaultdict(list)
    for mutant_id, family in families.items():
        by_family[family].append(mutant_id)
    active_families = [family for family in FAMILIES if by_family[family]]
    if len(active_families) < 2:
        raise ValueError("held-out evaluation needs at least two nonempty fault families")
    rows: list[dict] = []
    orders: dict[str, list[str]] = {}
    for held_out in active_families:
        training = [f for f in active_families if f != held_out]
        order = greedy_training_order(assertions, training, by_family, matrix)
        orders[held_out] = order
        for k in range(len(assertions) + 1):
            selected = order[:k]
            test = by_family[held_out]
            detected = sum(covered(m, selected, matrix) for m in test)
            unknown_only = sum(
                not covered(m, selected, matrix)
                and any(matrix[a, m] == "UNKNOWN" for a in selected)
                for m in test
            )
            rows.append({
                "held_out_family": held_out,
                "k": k,
                "selected_assertion": order[k - 1] if k else "",
                "detected": detected,
                "test_mutants": len(test),
                "detection_percent_lower": 100 * detected / len(test),
                "detection_percent_upper": 100 * (detected + unknown_only) / len(test),
                "unknown_only": unknown_only,
            })
    macro_rows: list[dict] = []
    for k in range(len(assertions) + 1):
        fold_rows = [r for r in rows if r["k"] == k]
        macro_rows.append({
            "k": k,
            "macro_percent_lower": sum(r["detection_percent_lower"] for r in fold_rows) / len(active_families),
            "macro_percent_upper": sum(r["detection_percent_upper"] for r in fold_rows) / len(active_families),
        })
    unknown_cells = sum(status == "UNKNOWN" for status in matrix.values())
    summary = {
        "assertion_count": len(assertions),
        "active_families": active_families,
        "missing_families": [family for family in FAMILIES if family not in active_families],
        "mutants_per_family": {f: len(by_family[f]) for f in FAMILIES},
        "matrix_cells": len(matrix),
        "unknown_cells": unknown_cells,
        "selection": "greedy class-macro marginal CEX coverage on active training families; lexicographic ties",
        "held_out_results": {
            f: {
                "full_set_percent_lower": next(r["detection_percent_lower"] for r in rows if r["held_out_family"] == f and r["k"] == len(assertions)),
                "full_set_percent_upper": next(r["detection_percent_upper"] for r in rows if r["held_out_family"] == f and r["k"] == len(assertions)),
                "selection_order": orders[f],
            }
            for f in active_families
        },
        "macro_curve": macro_rows,
        "macro_full_set_percent_lower": macro_rows[-1]["macro_percent_lower"],
        "macro_full_set_percent_upper": macro_rows[-1]["macro_percent_upper"],
        "macro_area_percent_lower": sum(r["macro_percent_lower"] for r in macro_rows[1:]) / len(assertions),
        "macro_area_percent_upper": sum(r["macro_percent_upper"] for r in macro_rows[1:]) / len(assertions),
    }
    return rows, summary


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_svg(path: Path, curve: list[dict], count: int, *, title: str,
              subtitle: str, lower_key: str, upper_key: str, ylabel: str) -> None:
    """Draw one fixed-pool curve; plotting never reruns Jasper."""
    left, top, width, height = 70, 70, 630, 325
    xy = lambda k, pct: (left + width * k / count, top + height * (1 - pct / 100))
    lower = " ".join(f"{xy(row['k'], row[lower_key])[0]:.2f},{xy(row['k'], row[lower_key])[1]:.2f}" for row in curve)
    upper = " ".join(f"{xy(row['k'], row[upper_key])[0]:.2f},{xy(row['k'], row[upper_key])[1]:.2f}" for row in curve)
    grid = "".join(
        f'<line x1="{left}" y1="{y}" x2="{left+width}" y2="{y}" stroke="#ddd"/>'
        f'<text x="{left-12}" y="{y+4}" text-anchor="end" font-size="12">{pct}%</text>'
        for pct in (0, 25, 50, 75, 100) for y in [xy(0, pct)[1]]
    )
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="760" height="470" viewBox="0 0 760 470">
<rect width="760" height="470" fill="white"/>
<text x="380" y="25" text-anchor="middle" font-size="17">{title}</text>
<text x="380" y="48" text-anchor="middle" font-size="12" fill="#555">{subtitle}</text>
{grid}<line x1="{left}" y1="{top}" x2="{left}" y2="{top+height}" stroke="#222"/>
<line x1="{left}" y1="{top+height}" x2="{left+width}" y2="{top+height}" stroke="#222"/>
<polyline points="{upper}" fill="none" stroke="#999" stroke-width="2" stroke-dasharray="5 4"/>
<polyline points="{lower}" fill="none" stroke="#1764ac" stroke-width="3"/>
<text x="380" y="440" text-anchor="middle" font-size="14">Number of selected assertions (k)</text>
<text x="18" y="215" transform="rotate(-90 18 215)" text-anchor="middle" font-size="14">{ylabel}</text>
<text x="{left}" y="{top+height+18}" text-anchor="middle" font-size="12">0</text>
<text x="{left+width}" y="{top+height+18}" text-anchor="middle" font-size="12">{count}</text>
<text x="{left+width-5}" y="{top+18}" text-anchor="end" font-size="12" fill="#1764ac">solid: confirmed; dashed: possible with UNKNOWN</text>
</svg>'''
    path.write_text(svg + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assertions, families = read_manifest(args.manifest)
    matrix = read_matrix(args.matrix, assertions, families)
    rows, summary = analyze(assertions, families, matrix)
    args.output.mkdir(parents=True, exist_ok=True)
    write_csv(args.output / "fold_curves.csv", rows)
    write_csv(args.output / "macro_curve.csv", summary["macro_curve"])
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    plot_data = {
        "schema_version": 1,
        "x_axis": "number of selected assertions (k)",
        "y_axis": "confirmed/possible held-out detection percent",
        "assertion_count": len(assertions),
        "families": {
            family: {
                "test_mutants": summary["mutants_per_family"][family],
                "selection_order": summary["held_out_results"][family]["selection_order"],
                "points": [row for row in rows if row["held_out_family"] == family],
            }
            for family in summary["active_families"]
        },
        "macro_average": {
            "weighting": "each held-out family has equal weight at each k",
            "points": summary["macro_curve"],
            "full_set_percent_lower": summary["macro_full_set_percent_lower"],
            "full_set_percent_upper": summary["macro_full_set_percent_upper"],
            "area_percent_lower": summary["macro_area_percent_lower"],
            "area_percent_upper": summary["macro_area_percent_upper"],
        },
    }
    (args.output / "plot_data.json").write_text(json.dumps(plot_data, indent=2) + "\n", encoding="utf-8")
    for family in summary["active_families"]:
        fold = [row for row in rows if row["held_out_family"] == family]
        write_svg(args.output / f"{family}_curve.svg", fold, len(assertions),
                  title=f"Held-out {family}: detection vs assertion count",
                  subtitle=f"Fixed test pool: {fold[0]['test_mutants']} {family} mutants; selection uses other active families",
                  lower_key="detection_percent_lower", upper_key="detection_percent_upper",
                  ylabel="Held-out detection (%)")
    write_svg(args.output / "macro_curve.svg", summary["macro_curve"], len(assertions),
              title=f"Average across {len(summary['active_families'])} held-out fault families",
              subtitle="Equal weight per family at each assertion count (macro average)",
              lower_key="macro_percent_lower", upper_key="macro_percent_upper",
              ylabel="Average detection (%)")
    print(f"Wrote {args.output}; full-set={summary['macro_full_set_percent_lower']:.2f}% "
          f"area={summary['macro_area_percent_lower']:.2f}%")


if __name__ == "__main__":
    main()
