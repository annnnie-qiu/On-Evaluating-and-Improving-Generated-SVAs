"""Read and reconcile the twelve existing Step A outputs without any API calls."""
from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

from export_model_aq import VARIANTS
from shared_results import save_json, sha, signature


def inspect_outputs(project_root: Path, output: Path, models: list[str], variants: list[str]) -> dict:
    slots, errors, provenance = [], [], defaultdict(list)
    for variant in variants:
        aq_folder = project_root / VARIANTS[variant][1]
        source = aq_folder / "spec_guard_four_models"
        for model in models:
            for trial in range(1, 4):
                slot = {"variant": variant, "model": model, "run": trial,
                        "step_a_directory": str(source / model / f"run_{trial:02d}"), "status": "ERROR"}
                slots.append(slot)
                try:
                    folder = Path(slot["step_a_directory"])
                    summary = json.loads((folder / "summary.json").read_text())
                    raw = (folder / "spec_check_results.json").read_bytes()
                    results = json.loads(raw)
                    if not isinstance(summary, dict):
                        raise ValueError("Step A summary must be an object")
                    if (summary.get("run_status") != "COMPLETE" or summary.get("model_key") != model
                            or summary.get("run_number") != trial
                            or summary.get("input_variant") != VARIANTS[variant][2]):
                        raise ValueError("Step A summary model/run/variant is inconsistent or incomplete")
                    if not isinstance(results, list) or len(results) != summary["technical_aq"]:
                        raise ValueError("technical AQ count does not match Step A results")
                    if any(not isinstance(r, dict) for r in results):
                        raise ValueError("Step A results must contain per-assertion objects")
                    ids = [(r["assertion_file"], r["assertion_id"]) for r in results]
                    if len(ids) != len(set(ids)):
                        raise ValueError("duplicate Step A assertion ID")
                    passed = [r for r in results if r.get("verdict") == "PASS"]
                    if any(r.get("verdict") not in {"PASS", "FAIL"} for r in results):
                        raise ValueError("Step A contains unresolved/unrecognized verdicts")
                    if len(passed) != summary["pass_count"] or len(passed) != summary["final_aq"]:
                        raise ValueError("Step A final AQ / PASS count disagrees with results")
                    trace_count = 0
                    for row in results:
                        if row.get("model_key") != model or row.get("run_number") != trial:
                            raise ValueError("Step A result row belongs to a different model/run")
                        if summary.get("model_id") and row.get("model_id") != summary["model_id"]:
                            raise ValueError("Step A row model ID does not match summary")
                        if not isinstance(row.get("fingerprint"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"]):
                            raise ValueError("Step A decision fingerprint is missing")
                        if row.get("trace_file"):
                            trace = (source / row["trace_file"]).resolve()
                            if not trace.is_relative_to(source.resolve()):
                                raise ValueError("Step A trace path leaves the output directory")
                            if trace.is_file():
                                record = json.loads(trace.read_text())
                                if any(record.get(k) != value for k, value in {
                                    "model_key": model, "run_number": trial,
                                    "assertion_id": row["assertion_id"],
                                    "cache_fingerprint": row["fingerprint"]}.items()):
                                    raise ValueError("Step A trace metadata does not match its result row")
                                if record.get("status") not in {"API_SUCCESS", "API_SUCCESS_EMPTY"}:
                                    raise ValueError("Step A trace does not record a completed model API call")
                                if record.get("mapped_verdict") != row["verdict"]:
                                    raise ValueError("Step A trace verdict differs from its saved result")
                                trace_count += 1
                    a0 = summary["a0_count"]
                    percent = 100 * len(passed) / a0 if a0 else 0.0
                    if abs(percent - summary["post_spec_qualification_yield_percent"]) > 1e-6:
                        raise ValueError("Step A AQ percentage disagrees with its actual final AQ count")
                    identity = signature(sorted((r["assertion_file"], r["assertion_id"],
                        r["property_name"], r.get("assertion_text", "")) for r in passed))
                    slot.update(status="VERIFIED", a0=a0, technical_aq=len(results), final_aq=len(passed),
                        final_aq_percent=percent, final_aq_sha256=identity,
                        spec_results_sha256=sha(raw), trace_records_checked=trace_count,
                        trace_records_missing=len(results) - trace_count,
                        assertion_files=sorted({r["assertion_file"] for r in passed}))
                    evidence = signature(sorted((r["assertion_file"], r["assertion_id"], r["fingerprint"]) for r in results))
                    if results:
                        provenance[signature([variant, evidence])].append(slot)
                    print(f"STEP A {variant}/{model}/run_{trial:02d}: AQ={len(passed)}/{a0}, "
                          f"{percent:.2f}%, content={identity[:12]}, traces={trace_count}/{len(results)}", flush=True)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    slot["reason"] = str(error)
                    errors.append(dict(slot))
    for duplicated in provenance.values():
        if len(duplicated) > 1:
            errors.append({"reason": "Step A decision fingerprints copied between independent runs",
                           "slots": [s["step_a_directory"] for s in duplicated]})
    groups = defaultdict(list)
    for slot in slots:
        if slot["status"] == "VERIFIED":
            groups[signature([slot["variant"], slot["final_aq_sha256"]])].append(
                f"{slot['variant']}/{slot['model']}/run_{slot['run']:02d}")
    report = {"requested_slots": len(slots), "verified_slots": sum(s["status"] == "VERIFIED" for s in slots),
              "unique_final_aq_contents": len(groups), "same_content_groups": list(groups.values()),
              "slots": slots, "errors": errors,
              "note": "independent model runs may return identical decisions; shared detection measurements do not replace trial provenance"}
    save_json(output / "step_a_outputs.json", report)
    columns = ["variant", "model", "run", "status", "a0", "technical_aq", "final_aq",
               "final_aq_percent", "final_aq_sha256", "trace_records_checked", "trace_records_missing",
               "step_a_directory", "reason"]
    with (output / "step_a_outputs.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(slots)
    if errors:
        raise ValueError(f"Step A outputs are missing/inconsistent; see {output / 'step_a_outputs.json'}; no Jasper or model API ran")
    return report
