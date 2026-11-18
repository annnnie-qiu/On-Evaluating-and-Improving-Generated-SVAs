#!/usr/bin/env python3
"""Export one model/run's post-specification AQ for fault_holdout_eval.

The four-model checker writes PASS decisions, but its common final/AQ source
predates those decisions.  This script materializes exactly the PASS subset
from the original assertion source and writes a matching run_all.py config.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path


MODELS = ("qwen", "deepseek", "gpt", "gemini")
VARIANTS = {
    "original": ("assertions", "aq_out", "NORMAL"),
    "controlled": ("buggy_assertions", "aq_buggy_out", "BUGGY"),
}


def read_json(path: Path):
    if not path.is_file():
        raise ValueError(f"Required input is missing: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def load_aq_parser(project_root: Path):
    candidates = (Path(__file__).resolve().parent / "aq_source_parser.py",)
    script = next((path for path in candidates if path.is_file()), None)
    if script is None:
        raise ValueError("Cannot find aq_qualification_pipeline_v1/assertion_aq_pipeline.py")
    sys.path.insert(0, str(script.parent))
    spec = importlib.util.spec_from_file_location("fault_holdout_aq_parser", script)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load AQ parser: {script}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def export(
    base_config_path: Path,
    model: str,
    variant: str,
    run_number: int,
    assertion_source: str,
    output: Path,
) -> dict:
    if model not in MODELS or variant not in VARIANTS or run_number < 1:
        raise ValueError("Invalid model, variant, or run number")
    base_config_path = base_config_path.resolve()
    base = read_json(base_config_path)
    project_root = Path(base["project_root"])
    if not project_root.is_absolute():
        project_root = base_config_path.parent / project_root
    project_root = project_root.resolve()
    source_relative = Path(assertion_source)
    if source_relative.is_absolute() or ".." in source_relative.parts:
        raise ValueError("--assertion-source must be relative to the variant assertion folder")

    source_folder, aq_folder, expected_variant = VARIANTS[variant]
    source_path = project_root / source_folder / source_relative
    aq_output = project_root / aq_folder
    run_folder = aq_output / "spec_guard_four_models" / model / f"run_{run_number:02d}"
    summary = read_json(run_folder / "summary.json")
    if summary.get("run_status") != "COMPLETE":
        raise ValueError(f"Spec check did not complete: {run_folder}")
    if summary.get("model_key") != model or summary.get("input_variant") != expected_variant:
        raise ValueError(f"Model/variant mismatch in {run_folder / 'summary.json'}")
    if int(summary.get("run_number", -1)) != run_number:
        raise ValueError("Run number mismatch in spec-check summary")
    results = read_json(run_folder / "spec_check_results.json")
    if not isinstance(results, list) or len(results) != int(summary["technical_aq"]):
        raise ValueError("Spec-check results do not match the technical AQ count")
    if sum(row.get("verdict") == "PASS" for row in results) != int(summary["pass_count"]):
        raise ValueError("Spec-check PASS count disagrees with the summary")

    for row in results:
        if row.get("model_key") != model or row.get("run_number") != run_number:
            raise ValueError("Step A result row belongs to another model/run; do not copy trials")
        if summary.get("model_id") and row.get("model_id") != summary["model_id"]:
            raise ValueError("Step A model ID differs between results and summary")
        if not isinstance(row.get("fingerprint"), str) or not re.fullmatch(r"[0-9a-f]{64}", row["fingerprint"]):
            raise ValueError("Step A per-run decision fingerprint is missing; cannot verify trial provenance")
    if int(summary.get("final_aq", summary["pass_count"])) != int(summary["pass_count"]):
        raise ValueError("Step A final AQ count disagrees with its PASS count")

    inventory_files = read_json(aq_output / "manifests" / "inventory_files.json")
    inventory_assertions = read_json(aq_output / "manifests" / "inventory_assertions.json")
    vacuity = read_json(aq_output / "manifests" / "vacuity.json")
    relative_name = source_relative.as_posix()
    file_rows = [row for row in inventory_files if row["assertion_file"] == relative_name]
    if len(file_rows) != 1 or not source_path.is_file():
        raise ValueError(f"Cannot uniquely identify assertion source: {source_path}")
    source = source_path.read_text(encoding="utf-8")
    if source_hash(source) != file_rows[0]["source_sha256"]:
        raise ValueError("Assertion source changed since technical AQ; rerun Step A")

    parser = load_aq_parser(project_root)
    blocks, errors = parser.extract_assertions(source)
    if errors:
        raise ValueError(f"Cannot safely export assertion source: {errors}")
    block_by_id = {block.assertion_id: block for block in blocks}
    inventory = {row["assertion_id"]: row for row in inventory_assertions
                 if row["assertion_file"] == relative_name}
    if set(block_by_id) != set(inventory):
        raise ValueError("Parsed assertion inventory differs from the recorded Step A inventory")
    for assertion_id, block in block_by_id.items():
        row = inventory[assertion_id]
        if block.fingerprint != row["fingerprint"] or block.property_name != row["property_name"]:
            raise ValueError(f"Assertion changed since technical AQ: {assertion_id}")

    technical_ids = {row["assertion_id"] for row in vacuity
                     if row["assertion_file"] == relative_name
                     and row.get("status") in {"PASS", "NOT_APPLICABLE"}}
    for stage, good in (("syntax", "PASS"), ("fpv", "PROVEN")):
        path = aq_output / "manifests" / (stage + ".json")
        if path.is_file():
            accepted = {r["assertion_id"] for r in read_json(path)
                        if r["assertion_file"] == relative_name and r.get("status") == good}
            if not technical_ids <= accepted:
                raise ValueError(f"Vacuity-qualified IDs lack recorded {stage} success in {relative_name}")
    source_results = [row for row in results if row["assertion_file"] == relative_name]
    result_ids = [row["assertion_id"] for row in source_results]
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != technical_ids:
        raise ValueError("Model results do not match the qualified assertions for this source")
    for row in source_results:
        block = block_by_id[row["assertion_id"]]
        if row["property_name"] != block.property_name:
            raise ValueError(f"Model result property name mismatch: {row['assertion_id']}")
        if row.get("assertion_text") and row["assertion_text"].strip() != block.text.strip():
            raise ValueError(f"Model result assertion text mismatch: {row['assertion_id']}")
        if row.get("verdict") not in {"PASS", "FAIL"}:
            raise ValueError(f"Unresolved model verdict: {row['assertion_id']}")

    kept = [row for row in source_results if row["verdict"] == "PASS"]
    property_names = [row["property_name"] for row in kept]
    if len(property_names) != len(set(property_names)):
        raise ValueError("Property names are not unique; Jasper matrix mapping would be ambiguous")
    keep_ids = {row["assertion_id"] for row in kept}
    rendered = parser.render_subset(source, blocks, keep_ids, label_unnamed=True)

    output = output.resolve()
    aq_file = output / "final_AQ" / source_relative
    aq_file.parent.mkdir(parents=True, exist_ok=True)
    run_config = dict(base)
    run_config["project_root"] = str(project_root)
    run_config["assertion_file"] = str(aq_file)
    run_config["assertions"] = property_names
    config_path = output / "run_config.json"
    export_record = {
        "model": model,
        "variant": variant,
        "run_number": run_number,
        "source": str(source_path),
        "spec_results": str(run_folder / "spec_check_results.json"),
        "aq_file": str(aq_file),
        "config": str(config_path),
        "technical_count": len(source_results),
        "final_aq_count": len(kept),
        "aq_state": "NONEMPTY" if kept else "EMPTY_AQ",
        "assertions": property_names,
        "source_sha256": source_hash(source),
        "export_sha256": source_hash(rendered),
        "spec_results_sha256": hashlib.sha256((run_folder / "spec_check_results.json").read_bytes()).hexdigest(),
        "spec_summary": str(run_folder / "summary.json"),
        "spec_summary_sha256": hashlib.sha256((run_folder / "summary.json").read_bytes()).hexdigest(),
        "step_a_total_final_aq": int(summary["pass_count"]),
        "step_a_a0": summary.get("a0_count"),
        "step_a_final_yield_percent": summary.get("post_spec_qualification_yield_percent"),
        "step_a_decision_fingerprints": {row["assertion_id"]: row["fingerprint"] for row in source_results},
    }
    files = {aq_file: rendered, config_path: json.dumps(run_config, indent=2) + "\n",
             output / "export_record.json": json.dumps(export_record, indent=2) + "\n"}
    previous = {path: path.read_bytes() for path in files if path.is_file()}
    if any(previous[path] != files[path].encode() for path in previous):
        identity = hashlib.sha256(b"".join(str(p.relative_to(output)).encode() + data
                                             for p, data in previous.items())).hexdigest()
        for path, data in previous.items():
            history = output / "history" / identity / path.relative_to(output)
            history.parent.mkdir(parents=True, exist_ok=True)
            if not history.exists():
                history.write_bytes(data)
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
    return export_record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-config", type=Path, required=True)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--variant", choices=tuple(VARIANTS), required=True)
    parser.add_argument("--run", type=int, default=1, dest="run_number")
    parser.add_argument("--assertion-source", required=True,
                        help="relative source name under assertions/ or buggy_assertions/")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = export(args.base_config, args.model, args.variant, args.run_number,
                    args.assertion_source, args.output)
    print(f"Exported {result['final_aq_count']} assertions: {result['aq_file']}")
    print(f"Step B config: {result['config']}")


if __name__ == "__main__":
    main()
