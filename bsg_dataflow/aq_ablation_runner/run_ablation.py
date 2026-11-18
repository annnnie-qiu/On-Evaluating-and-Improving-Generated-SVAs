#!/usr/bin/env python3
"""Single-stage ablation runner for the assertion qualification framework.

The runner reuses the project's existing AQ/Jasper pipeline and four-model
specification checker.  Exactly one gate is removed while every other gate is
kept unchanged.  Existing compatible Jasper and LLM caches are reused; only
newly exposed assertions are evaluated.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import shutil
import statistics
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable


STAGES = ("syntax", "fpv", "vacuity", "llm")
MANIFEST_EXTENSIONS = (".json", ".csv")
VACUITY_PASS = {"PASS", "NOT_APPLICABLE"}


class AblationError(RuntimeError):
    """An expected configuration, artifact, or experiment error."""


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def load_module(path: Path, name: str) -> ModuleType:
    if not path.is_file():
        raise AblationError(f"Python module does not exist: {path}")
    module_dir = str(path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AblationError(f"Cannot import Python module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def looks_like_project(path: Path) -> bool:
    return (path / "rtl").is_dir() and (path / "assertions").is_dir()


def find_project_root(explicit: str | None) -> Path:
    if explicit:
        root = Path(explicit).expanduser().resolve()
        if not looks_like_project(root):
            raise AblationError(
                f"Project root must contain rtl/ and assertions/: {root}"
            )
        return root

    starts = [Path.cwd().resolve(), Path(__file__).resolve().parent]
    for start in starts:
        for candidate in (start, *start.parents):
            if looks_like_project(candidate):
                return candidate
    raise AblationError(
        "Cannot find the dataset root. Place aq_ablation_runner/ inside the "
        "dataset, or pass --project-root."
    )


def find_aq_config(project_root: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = project_root / path
        if not path.is_file():
            raise AblationError(f"AQ configuration does not exist: {path}")
        return path.resolve()

    folder = project_root / "aq_qualification_pipeline_v1"
    preferred = [folder / "aq_config.json", folder / "aq_config.yaml"]
    for path in preferred:
        if path.is_file():
            return path.resolve()
    candidates = sorted(folder.glob("aq_config*.json")) + sorted(
        folder.glob("aq_config*.yaml")
    )
    if len(candidates) == 1:
        return candidates[0].resolve()
    raise AblationError(
        "Cannot select the AQ configuration automatically. Pass --aq-config."
    )


def find_checker(project_root: Path, explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute():
            path = project_root / path
        if not path.is_file():
            raise AblationError(f"Specification checker does not exist: {path}")
        return path.resolve()

    candidates = [
        Path(__file__).resolve().with_name("four_model_spec_checker.py"),
        project_root / "qwen_spec_check_vm" / "qwen.py",
        project_root / "qwen_spec_check_vm" / "run_spec_check_qwen.py",
        project_root / "qwen.py",
    ]
    for path in candidates:
        if path.is_file():
            return path.resolve()
    raise AblationError(
        "Cannot find the four-model specification checker. Pass --checker."
    )


def require_checker_api(module: ModuleType) -> None:
    required = (
        "MODEL_CONFIGS",
        "read_specification",
        "run_one_model",
        "write_error_summary",
        "build_average_summary",
        "write_average_summary_table",
    )
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise AblationError(
            "The selected checker is not the four-model checker. Missing: "
            + ", ".join(missing)
        )


def copy_stage_manifests(source_output: Path, target_output: Path) -> None:
    """Seed the ablation with reusable manifests from the native run."""
    source = source_output / "manifests"
    target = target_output / "manifests"
    target.mkdir(parents=True, exist_ok=True)
    for stage in ("syntax", "fpv", "vacuity"):
        for extension in MANIFEST_EXTENSIONS:
            source_path = source / f"{stage}{extension}"
            target_path = target / f"{stage}{extension}"
            if source_path.is_file() and not target_path.exists():
                shutil.copy2(source_path, target_path)


def pipeline_arguments(
    project_root: Path,
    use_buggy: bool,
    force_jasper: bool,
    workers: int | None,
    timeout: float | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        project_root=str(project_root),
        buggy=use_buggy,
        dry_run=False,
        force=force_jasper,
        workers=workers,
        timeout=timeout,
        include=None,
        exclude=None,
    )


def redirect_pipeline(
    pipeline: Any,
    assertions_dir: Path,
    output_root: Path,
) -> None:
    """Redirect a native Pipeline instance without changing its config hash."""
    pipeline.assertions_dir = assertions_dir.resolve()
    pipeline.output_root = output_root.resolve()
    pipeline.manifest_dir = pipeline.output_root / "manifests"
    pipeline.manifest_dir.mkdir(parents=True, exist_ok=True)
    pipeline.units = []
    pipeline.results = {}
    pipeline._manifest_rows = {}


def synthetic_result(
    pipeline_module: ModuleType,
    stage: str,
    status: str,
    unit: Any,
    block: Any,
    reason: str,
) -> Any:
    return pipeline_module.RunResult(
        stage=stage,
        assertion_file=unit.rel,
        unit_id=unit.unit_id,
        module_guess=unit.module_guess,
        assertion_id=block.assertion_id,
        property_name=block.property_name,
        named=block.named,
        start_line=block.start_line,
        end_line=block.end_line,
        source_sha256=unit.source_sha256,
        fingerprint=block.fingerprint,
        stage_config_hash="ablation-synthetic-v1",
        status=status,
        reason=reason,
        exit_code=0,
        duration_seconds=0.0,
        command="",
        run_dir="",
        artifact="",
        log_file="",
        assertion_kind=block.assertion_kind,
        vacuity_mode="",
        resumed=True,
    )


def write_synthetic_stage(
    pipeline_module: ModuleType,
    pipeline: Any,
    stage: str,
    results: list[Any],
) -> None:
    rows = [result.as_dict() for result in results]
    pipeline_module.write_csv_json(pipeline.manifest_dir / stage, rows)
    pipeline.results[stage] = results
    pipeline._manifest_rows[stage] = rows


def synthesize_syntax_pass(pipeline_module: ModuleType, pipeline: Any) -> None:
    results = []
    for unit in pipeline.units:
        if unit.parse_errors:
            continue
        for block in unit.assertions:
            results.append(
                synthetic_result(
                    pipeline_module,
                    "syntax",
                    "PASS",
                    unit,
                    block,
                    "Syntax gate removed for this ablation.",
                )
            )
    write_synthetic_stage(pipeline_module, pipeline, "syntax", results)


def synthesize_fpv_pass(pipeline_module: ModuleType, pipeline: Any) -> None:
    syntax_results = pipeline.load_prior_stage("syntax")
    accepted = {
        (row.assertion_file, row.assertion_id)
        for row in syntax_results
        if row.status == "PASS"
    }
    results = []
    for unit in pipeline.units:
        for block in unit.assertions:
            if (unit.rel, block.assertion_id) not in accepted:
                continue
            results.append(
                synthetic_result(
                    pipeline_module,
                    "fpv",
                    "PROVEN",
                    unit,
                    block,
                    "FPV gate removed for this ablation.",
                )
            )
    write_synthetic_stage(pipeline_module, pipeline, "fpv", results)


def run_technical_ablation(
    pipeline_module: ModuleType,
    pipeline: Any,
    removed_stage: str,
) -> tuple[set[tuple[str, str]], str]:
    """Run every technical gate except the selected ablation gate."""
    if removed_stage == "syntax":
        synthesize_syntax_pass(pipeline_module, pipeline)
        pipeline.run_stage("fpv")
        pipeline.run_stage("vacuity")
        final_manifest = "vacuity"
    elif removed_stage == "fpv":
        pipeline.run_stage("syntax")
        synthesize_fpv_pass(pipeline_module, pipeline)
        pipeline.run_stage("vacuity")
        final_manifest = "vacuity"
    elif removed_stage == "vacuity":
        pipeline.run_stage("syntax")
        pipeline.run_stage("fpv")
        final_manifest = "fpv"
    elif removed_stage == "llm":
        pipeline.run_stage("syntax")
        pipeline.run_stage("fpv")
        pipeline.run_stage("vacuity")
        final_manifest = "vacuity"
    else:
        raise AblationError(f"Unsupported ablation stage: {removed_stage}")

    rows = read_json(pipeline.manifest_dir / f"{final_manifest}.json")
    if final_manifest == "fpv":
        qualified = {
            (row["assertion_file"], row["assertion_id"])
            for row in rows
            if row.get("status") == "PROVEN"
        }
    else:
        qualified = {
            (row["assertion_file"], row["assertion_id"])
            for row in rows
            if row.get("status") in VACUITY_PASS
        }
    return qualified, final_manifest


def inventory_rows(pipeline: Any) -> list[dict[str, Any]]:
    return read_json(pipeline.manifest_dir / "inventory_assertions.json")


def build_checker_assertions(
    pipeline: Any,
    selected_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    rows = inventory_rows(pipeline)
    units = {unit.rel: unit for unit in pipeline.units}
    result = []
    for row in rows:
        key = (row["assertion_file"], row["assertion_id"])
        if key not in selected_keys:
            continue
        unit = units.get(row["assertion_file"])
        if unit is None:
            raise AblationError(f"Missing source unit: {row['assertion_file']}")
        block = next(
            (
                candidate
                for candidate in unit.assertions
                if candidate.assertion_id == row["assertion_id"]
            ),
            None,
        )
        if block is None:
            raise AblationError(
                f"Missing assertion block: {row['assertion_file']} :: "
                f"{row['assertion_id']}"
            )
        result.append(
            {
                "assertion_file": row["assertion_file"],
                "assertion_id": row["assertion_id"],
                "property_name": row["property_name"],
                "assertion_text": block.text.strip(),
                "fingerprint": row["fingerprint"],
            }
        )
    return result


def merge_tree_without_overwrite(source: Path, target: Path) -> None:
    if not source.is_dir():
        return
    for source_file in source.rglob("*"):
        if not source_file.is_file():
            continue
        relative = source_file.relative_to(source)
        target_file = target / relative
        if target_file.exists():
            continue
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)


def seed_llm_cache(
    project_root: Path,
    target_output: Path,
    model_key: str,
    run_number: int,
) -> None:
    """Merge compatible old cache candidates; the checker validates hashes."""
    relative = Path(model_key) / f"run_{run_number:02d}"
    sources = [
        project_root / "aq_out" / "spec_guard_four_models" / relative,
        project_root / "aq_buggy_out" / "spec_guard_four_models" / relative,
    ]
    ablation_root = project_root / "aq_ablation_out"
    if ablation_root.is_dir():
        sources.extend(
            path.parent
            for path in ablation_root.glob(
                f"*/**/spec_guard_four_models/{model_key}/"
                f"run_{run_number:02d}/cache"
            )
        )
    target_run = target_output / relative
    for source_run in sources:
        if source_run.resolve() == target_run.resolve():
            continue
        merge_tree_without_overwrite(source_run / "cache", target_run / "cache")
        merge_tree_without_overwrite(source_run / "traces", target_run / "traces")


def selected_keys_from_spec_results(path: Path) -> set[tuple[str, str]]:
    rows = read_json(path)
    return {
        (str(row["assertion_file"]), str(row["assertion_id"]))
        for row in rows
        if row.get("verdict") == "PASS"
    }


def materialize_aq(
    pipeline_module: ModuleType,
    pipeline: Any,
    selected_keys: set[tuple[str, str]],
    destination: Path,
    metadata: dict[str, Any],
) -> None:
    source_root = destination / "assertions"
    selected_rows = []
    row_index = {
        (row["assertion_file"], row["assertion_id"]): row
        for row in inventory_rows(pipeline)
    }
    for unit in pipeline.units:
        keep = {
            assertion_id
            for assertion_file, assertion_id in selected_keys
            if assertion_file == unit.rel
        }
        target = source_root / unit.rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            pipeline_module.render_subset(unit.source, unit.assertions, keep),
            encoding="utf-8",
        )
    for key in sorted(selected_keys):
        row = dict(row_index[key])
        row.update(metadata)
        selected_rows.append(row)
    columns = sorted({key for row in selected_rows for key in row}) if selected_rows else [
        "assertion_file",
        "assertion_id",
    ]
    write_csv(destination / "selected_assertions.csv", selected_rows, columns)
    write_json(destination / "selected_assertions.json", selected_rows)
    write_json(
        destination / "aq_summary.json",
        {**metadata, "aq_count": len(selected_keys)},
    )


def ensure_normal_reference(
    project_root: Path,
    config_path: Path,
    pipeline_module: ModuleType,
    force_jasper: bool,
    workers: int | None,
    timeout: float | None,
) -> None:
    """Create the native normal technical AQ only when BA inputs are missing."""
    normal_output = project_root / "aq_out"
    required = (
        normal_output / "manifests" / "inventory_assertions.json",
        normal_output / "manifests" / "vacuity.json",
    )
    if all(path.is_file() for path in required):
        try:
            vacuity_rows = read_json(required[1])
            if any(row.get("status") in VACUITY_PASS for row in vacuity_rows):
                return
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            pass
    print(
        "[BA SETUP] Normal reference manifests are missing; running the normal "
        "technical AQ once.",
        flush=True,
    )
    pipeline = pipeline_module.Pipeline(
        config_path,
        pipeline_arguments(
            project_root,
            False,
            force_jasper,
            workers,
            timeout,
        ),
    )
    redirect_pipeline(pipeline, project_root / "assertions", normal_output)
    pipeline.inventory()
    pipeline.run_stage("syntax")
    pipeline.run_stage("fpv")
    pipeline.run_stage("vacuity")


def load_ba_labels(
    project_root: Path,
    buggy_inventory: list[dict[str, Any]],
) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    normal_inventory_path = project_root / "aq_out" / "manifests" / "inventory_assertions.json"
    normal_vacuity_path = project_root / "aq_out" / "manifests" / "vacuity.json"
    missing = [
        path
        for path in (normal_inventory_path, normal_vacuity_path)
        if not path.is_file()
    ]
    if missing:
        raise AblationError(
            "Balanced Accuracy requires the normal inventory and vacuity "
            "manifest. Missing: "
            + ", ".join(str(path) for path in missing)
        )
    normal_inventory = read_json(normal_inventory_path)
    normal_vacuity = read_json(normal_vacuity_path)
    normal_by_key = {
        (row["assertion_file"], row["assertion_id"]): row["fingerprint"]
        for row in normal_inventory
    }
    normal_fingerprints = {row["fingerprint"] for row in normal_inventory}
    buggy_fingerprints = {row["fingerprint"] for row in buggy_inventory}
    reference_positives = {
        normal_by_key[(row["assertion_file"], row["assertion_id"])]
        for row in normal_vacuity
        if row.get("status") in VACUITY_PASS
        and (row["assertion_file"], row["assertion_id"]) in normal_by_key
    }
    controlled_negatives = buggy_fingerprints - normal_fingerprints
    if not reference_positives or not controlled_negatives:
        raise AblationError(
            "Balanced Accuracy needs at least one reference positive and one "
            "buggy-only controlled negative."
        )
    labels = [
        {
            "fingerprint": fingerprint,
            "ground_truth": "REFERENCE_POSITIVE",
            "expected_prediction": "PASS",
        }
        for fingerprint in sorted(reference_positives)
    ] + [
        {
            "fingerprint": fingerprint,
            "ground_truth": "CONTROLLED_NEGATIVE",
            "expected_prediction": "FAIL",
        }
        for fingerprint in sorted(controlled_negatives)
    ]
    return reference_positives, controlled_negatives, labels


def calculate_ba_row(
    model_key: str,
    model_name: str,
    run_number: int,
    selected_keys: set[tuple[str, str]],
    key_to_fingerprint: dict[tuple[str, str], str],
    positives: set[str],
    negatives: set[str],
) -> dict[str, Any]:
    predicted_pass = {
        key_to_fingerprint[key]
        for key in selected_keys
        if key in key_to_fingerprint
    }
    tp = len(positives & predicted_pass)
    fn = len(positives - predicted_pass)
    fp = len(negatives & predicted_pass)
    tn = len(negatives - predicted_pass)
    tpr = tp / len(positives)
    tnr = tn / len(negatives)
    return {
        "model_key": model_key,
        "model_name": model_name,
        "run_number": run_number,
        "reference_positives": len(positives),
        "controlled_negatives": len(negatives),
        "true_positive": tp,
        "false_negative": fn,
        "true_negative": tn,
        "false_positive": fp,
        "positive_recall_percent": round(100.0 * tpr, 4),
        "negative_recall_percent": round(100.0 * tnr, 4),
        "balanced_accuracy_percent": round(50.0 * (tpr + tnr), 4),
    }


def write_ba_reports(
    output_root: Path,
    rows: list[dict[str, Any]],
    labels: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    columns = [
        "model_key",
        "model_name",
        "run_number",
        "reference_positives",
        "controlled_negatives",
        "true_positive",
        "false_negative",
        "true_negative",
        "false_positive",
        "positive_recall_percent",
        "negative_recall_percent",
        "balanced_accuracy_percent",
    ]
    write_csv(output_root / "balanced_accuracy_per_run.csv", rows, columns)
    write_json(output_root / "balanced_accuracy_per_run.json", rows)
    write_csv(
        output_root / "balanced_accuracy_labels.csv",
        labels,
        ["fingerprint", "ground_truth", "expected_prediction"],
    )
    average_rows = []
    for model_key in sorted({row["model_key"] for row in rows}):
        model_rows = [row for row in rows if row["model_key"] == model_key]
        values = [float(row["balanced_accuracy_percent"]) for row in model_rows]
        average_rows.append(
            {
                "model_key": model_key,
                "model_name": model_rows[0]["model_name"],
                "runs_completed": len(values),
                "reference_positives": model_rows[0]["reference_positives"],
                "controlled_negatives": model_rows[0]["controlled_negatives"],
                "mean_balanced_accuracy_percent": round(statistics.mean(values), 4),
                "population_sd_percent": round(
                    statistics.pstdev(values) if len(values) > 1 else 0.0,
                    4,
                ),
            }
        )
    average_columns = [
        "model_key",
        "model_name",
        "runs_completed",
        "reference_positives",
        "controlled_negatives",
        "mean_balanced_accuracy_percent",
        "population_sd_percent",
    ]
    write_csv(output_root / "balanced_accuracy_average.csv", average_rows, average_columns)
    write_json(output_root / "balanced_accuracy_average.json", average_rows)
    return average_rows


def write_experiment_summary(
    output_root: Path,
    removed_stage: str,
    variant: str,
    a0_count: int,
    technical_count: int,
    final_sets: list[dict[str, Any]],
    ba_averages: list[dict[str, Any]],
) -> None:
    rows = []
    ba_by_model = {row["model_key"]: row for row in ba_averages}
    for item in final_sets:
        ba = ba_by_model.get(item["model_key"], {})
        rows.append(
            {
                "removed_stage": removed_stage,
                "input_variant": variant.upper(),
                "model_key": item["model_key"],
                "model_name": item["model_name"],
                "run_number": item["run_number"],
                "a0_count": a0_count,
                "technical_input_count": technical_count,
                "final_aq_count": item["final_aq_count"],
                "qualification_yield_percent": round(
                    100.0 * item["final_aq_count"] / a0_count if a0_count else 0.0,
                    4,
                ),
                "mean_balanced_accuracy_percent": ba.get(
                    "mean_balanced_accuracy_percent", "N/A"
                ),
            }
        )
    columns = [
        "removed_stage",
        "input_variant",
        "model_key",
        "model_name",
        "run_number",
        "a0_count",
        "technical_input_count",
        "final_aq_count",
        "qualification_yield_percent",
        "mean_balanced_accuracy_percent",
    ]
    write_csv(output_root / "ablation_summary.csv", rows, columns)
    write_json(output_root / "ablation_summary.json", rows)


def print_summary(
    removed_stage: str,
    variant: str,
    a0_count: int,
    technical_count: int,
    final_sets: list[dict[str, Any]],
    ba_averages: list[dict[str, Any]],
    output_root: Path,
) -> None:
    print("\nABLATION RESULT")
    print(f"Removed gate       : {removed_stage.upper()}")
    print(f"Input variant      : {variant.upper()}")
    print(f"Generated set A0   : {a0_count}")
    print(f"Before LLM         : {technical_count}")
    print("\n| Model | Run | Final AQ | Qualification Yield |")
    print("|---|---:|---:|---:|")
    for item in final_sets:
        percentage = 100.0 * item["final_aq_count"] / a0_count if a0_count else 0.0
        print(
            f"| {item['model_name']} | {item['run_number']} | "
            f"{item['final_aq_count']}/{a0_count} | {percentage:.2f}% |"
        )
    if ba_averages:
        print("\n| Model | Runs | Mean BA | SD |")
        print("|---|---:|---:|---:|")
        for row in ba_averages:
            print(
                f"| {row['model_name']} | {row['runs_completed']} | "
                f"{row['mean_balanced_accuracy_percent']:.2f}% | "
                f"{row['population_sd_percent']:.2f}% |"
            )
    else:
        print("\nBalanced Accuracy  : N/A for the NORMAL-only variant")
    print(f"\nResults            : {output_root}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Remove exactly one qualification gate, rebuild AQ, and calculate "
            "controlled-validation Balanced Accuracy."
        )
    )
    parser.add_argument(
        "-remove",
        "--remove",
        required=True,
        choices=STAGES,
        help="Gate to remove: syntax, fpv, vacuity, or llm.",
    )
    parser.add_argument("--project-root", help="Dataset project root.")
    parser.add_argument("--aq-config", help="AQ JSON/YAML configuration path.")
    parser.add_argument("--checker", help="Four-model checker Python file.")
    variant = parser.add_mutually_exclusive_group()
    variant.add_argument(
        "-buggy",
        "--buggy",
        action="store_true",
        help="Use buggy_assertions/ (default when that folder exists).",
    )
    variant.add_argument(
        "-normal",
        "--normal",
        action="store_true",
        help="Use assertions/; BA is not calculated from a normal-only run.",
    )
    parser.add_argument(
        "--model",
        action="append",
        help="Checker model key; repeat to select multiple models.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Independent LLM runs per model. Default: 3.",
    )
    parser.add_argument(
        "--force-llm",
        action="store_true",
        help="Ignore matching LLM caches and rerun selected checks.",
    )
    parser.add_argument(
        "--force-jasper",
        action="store_true",
        help="Ignore matching Jasper manifests and rerun active stages.",
    )
    parser.add_argument("--workers", type=int, help="Jasper worker count override.")
    parser.add_argument("--timeout", type=float, help="Jasper timeout override.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    project_root = find_project_root(args.project_root)
    use_buggy = args.buggy or (
        not args.normal and (project_root / "buggy_assertions").is_dir()
    )
    variant = "buggy" if use_buggy else "normal"
    assertions_dir = project_root / ("buggy_assertions" if use_buggy else "assertions")
    if not assertions_dir.is_dir():
        raise AblationError(f"Assertions folder does not exist: {assertions_dir}")

    native_output = project_root / ("aq_buggy_out" if use_buggy else "aq_out")
    output_root = project_root / "aq_ablation_out" / f"remove_{args.remove}" / variant
    technical_output = output_root / "technical_aq"
    output_root.mkdir(parents=True, exist_ok=True)

    config_path = find_aq_config(project_root, args.aq_config)
    pipeline_path = config_path.parent / "assertion_aq_pipeline.py"
    pipeline_module = load_module(pipeline_path, "aq_ablation_native_pipeline")
    pipeline = pipeline_module.Pipeline(
        config_path,
        pipeline_arguments(
            project_root,
            use_buggy,
            args.force_jasper,
            args.workers,
            args.timeout,
        ),
    )
    redirect_pipeline(pipeline, assertions_dir, technical_output)
    pipeline.inventory()
    copy_stage_manifests(native_output, technical_output)
    pipeline.results = {}
    pipeline._manifest_rows = {}

    technical_keys, final_manifest = run_technical_ablation(
        pipeline_module,
        pipeline,
        args.remove,
    )
    current_inventory = inventory_rows(pipeline)
    inventory_files = read_json(
        pipeline.manifest_dir / "inventory_files.json"
    )
    a0_count = sum(
        int(row.get("original_assertions", 0) or 0)
        for row in inventory_files
    )
    technical_count = len(technical_keys)
    qualification = {
        "a0_count": a0_count,
        "aq_count": technical_count,
        "raw_readiness": "PASS" if technical_count == a0_count else "FAIL",
        "removed_stage": args.remove,
        "final_technical_manifest": final_manifest,
    }
    write_json(output_root / "technical_qualification.json", qualification)

    final_sets: list[dict[str, Any]] = []
    predictions: list[tuple[str, str, int, set[tuple[str, str]]]] = []
    if args.remove == "llm":
        selected = set(technical_keys)
        metadata = {
            "removed_stage": args.remove,
            "input_variant": variant,
            "model_key": "no_llm",
            "model_name": "LLM removed",
            "run_number": 1,
        }
        materialize_aq(
            pipeline_module,
            pipeline,
            selected,
            output_root / "final_AQ" / "no_llm" / "run_01",
            metadata,
        )
        final_sets.append({**metadata, "final_aq_count": len(selected)})
        predictions.append(("no_llm", "LLM removed", 1, selected))
    else:
        checker_path = find_checker(project_root, args.checker)
        checker = load_module(checker_path, "aq_ablation_spec_checker")
        require_checker_api(checker)
        selected_models = args.model or list(checker.MODEL_CONFIGS)
        unknown_models = [
            model for model in selected_models if model not in checker.MODEL_CONFIGS
        ]
        if unknown_models:
            raise AblationError(
                "Unknown model key(s): " + ", ".join(unknown_models)
            )
        specification = checker.read_specification(project_root)
        checker_assertions = build_checker_assertions(pipeline, technical_keys)
        llm_output = output_root / "spec_guard_four_models"
        for model_key in selected_models:
            config = checker.MODEL_CONFIGS[model_key]
            model_summaries = []
            for run_number in range(1, args.runs + 1):
                seed_llm_cache(
                    project_root,
                    llm_output,
                    model_key,
                    run_number,
                )
                run_folder = llm_output / model_key / f"run_{run_number:02d}"
                try:
                    summary = checker.run_one_model(
                        model_key,
                        config,
                        run_number,
                        project_root,
                        llm_output,
                        specification,
                        checker_assertions,
                        qualification,
                        variant,
                        args.force_llm,
                    )
                except Exception as error:
                    print(
                        f"[MODEL ERROR] {config['display_name']} "
                        f"run={run_number}: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    summary = checker.write_error_summary(
                        run_folder,
                        model_key,
                        config,
                        run_number,
                        qualification,
                        variant,
                        error,
                    )
                model_summaries.append(summary)
                results_path = run_folder / "spec_check_results.json"
                if summary.get("run_status") != "COMPLETE" or not results_path.is_file():
                    continue
                selected = selected_keys_from_spec_results(results_path)
                metadata = {
                    "removed_stage": args.remove,
                    "input_variant": variant,
                    "model_key": model_key,
                    "model_name": config["display_name"],
                    "run_number": run_number,
                }
                materialize_aq(
                    pipeline_module,
                    pipeline,
                    selected,
                    output_root
                    / "final_AQ"
                    / model_key
                    / f"run_{run_number:02d}",
                    metadata,
                )
                final_sets.append({**metadata, "final_aq_count": len(selected)})
                predictions.append(
                    (model_key, config["display_name"], run_number, selected)
                )
            average = checker.build_average_summary(
                model_key,
                config,
                model_summaries,
            )
            checker.write_average_summary_table(llm_output / model_key, average)

    ba_rows: list[dict[str, Any]] = []
    ba_averages: list[dict[str, Any]] = []
    if use_buggy:
        ensure_normal_reference(
            project_root,
            config_path,
            pipeline_module,
            args.force_jasper,
            args.workers,
            args.timeout,
        )
        positives, negatives, labels = load_ba_labels(
            project_root,
            current_inventory,
        )
        key_to_fingerprint = {
            (row["assertion_file"], row["assertion_id"]): row["fingerprint"]
            for row in current_inventory
        }
        for model_key, model_name, run_number, selected in predictions:
            ba_rows.append(
                calculate_ba_row(
                    model_key,
                    model_name,
                    run_number,
                    selected,
                    key_to_fingerprint,
                    positives,
                    negatives,
                )
            )
        ba_averages = write_ba_reports(output_root, ba_rows, labels)

    write_experiment_summary(
        output_root,
        args.remove,
        variant,
        a0_count,
        technical_count,
        final_sets,
        ba_averages,
    )
    print_summary(
        args.remove,
        variant,
        a0_count,
        technical_count,
        final_sets,
        ba_averages,
        output_root,
    )
    return 0 if final_sets else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"ABLATION ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)
