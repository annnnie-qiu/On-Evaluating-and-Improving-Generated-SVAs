#!/usr/bin/env python3
"""Run three independent post-spec AQ trials for every model and RTL module.

One validated, fixed RTL-mutant pool is shared by all model/variant/trial runs
within a module. Missing inputs and unscorable modules are recorded, never
silently omitted from a three-run average.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import csv
import hashlib
import json
import statistics
import subprocess
import sys
from pathlib import Path

from collect_jasper import input_fingerprint, make_tcl, resolve
from evaluate import FAMILIES, read_matrix, write_svg
from export_model_aq import MODELS, VARIANTS, export
from run_all import ast_environment, run
from shared_results import Checker, environment, job_key, sha, signature
from aq_inputs import audit_inputs, shared_measurement_inputs
from legacy_reuse import seed_existing_module
from step_a_outputs import inspect_outputs
from project_config import resolve_settings


HERE = Path(__file__).resolve().parent


def save_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def module_configs(project_root: Path, output: Path, aq_config: Path) -> list[dict]:
    config = json.loads(aq_config.read_text(encoding="utf-8"))
    mapping = config.get("units", {}).get("mapping", {})
    modules = []
    inventory_path = project_root / "aq_out/manifests/inventory_files.json"
    if inventory_path.is_file():
        inventory = {r["assertion_file"]: r for r in json.loads(inventory_path.read_text())}
        sources = list(inventory)
    else:
        sources = [p.lstrip("*") for p in mapping]
        inventory = {}
    for source in sources:
        if Path(source).is_absolute() or ".." in Path(source).parts or any(c in source for c in "*?["):
            raise ValueError(f"Invalid assertion source: {source}")
        settings, target, evidence = resolve_settings(project_root, config, source, inventory.get(source, {}))
        name = re.sub(r"[^A-Za-z0-9_.-]", "__", str(Path(source).with_suffix("")))
        if any(m["name"] == name for m in modules):
            raise ValueError(f"module output name collision: {source}")
        base = {
            "project_root": str(project_root),
            "top": settings["top"],
            "assertions": ["placeholder_replaced_by_export"],
            "rtl_files": settings["rtl_files"],
            "assertion_file": str(project_root / "aq_out" / "final" / "AQ" / source),
            "bind_files": settings["bind_files"],
            "include_dirs": settings["include_dirs"],
            "defines": settings["defines"],
            "clock_mode": settings["clock_mode"],
            "clock": settings["clock"],
            "reset_expression": settings["reset_expression"],
            "jasper_executable": config["jasper"].get("executable", "jg"),
            "jasper_version_tag": config["jasper"].get("version_tag", ""),
            "syntax_timeout_seconds": config["stages"]["syntax"].get("timeout_seconds", 1200),
            "timeout_seconds": config["stages"]["fpv"].get("timeout_seconds", 7200),
        }
        if not base["jasper_version_tag"]:
            base.pop("jasper_version_tag")
        path = output / name / "base_config.json"
        save_json(path, base)
        save_json(path.with_name("config_resolution.json"), evidence)
        print(f"CONFIG {source}: top={base['top']}; RTL files={len(base['rtl_files'])}; "
              f"target={target}; clock={base['clock_mode']} {base['clock']}", flush=True)
        modules.append({"source": source, "name": name,
                        "target": target, "base_config": path})
    return modules


def average_runs(run_rows: list[dict], group_dir: Path, budget: int) -> dict:
    """Average three independent curves on the same absolute assertion budget."""
    if len(run_rows) == 3 and all(row['status'] == 'EMPTY_AQ' for row in run_rows):
        result = {'status': 'EMPTY_AQ', 'trials': 3, 'assertion_counts': [0, 0, 0],
                  'reason': 'No final AQ in any requested slot of this module; no fault pool measured.'}
        save_json(group_dir / 'average.json', result)
        return result
    if len(run_rows) != 3 or any(row["status"] != "PASS" for row in run_rows):
        result = {"status": "INCOMPLETE", "reason": "all three trials must be PASS",
                  "trial_statuses": [row["status"] for row in run_rows]}
        save_json(group_dir / "average.json", result)
        return result
    summaries = [json.loads(Path(row["evaluation_summary"]).read_text(encoding="utf-8"))
                 for row in run_rows]
    family_lists = [summary["active_families"] for summary in summaries]
    if any(families != family_lists[0] for families in family_lists):
        raise ValueError("trials used different fault families; fixed-pool invariant broken")
    points = []
    for k in range(budget + 1):
        lower = [summary["macro_curve"][min(k, summary["assertion_count"])]["macro_percent_lower"]
                 for summary in summaries]
        upper = [summary["macro_curve"][min(k, summary["assertion_count"])]["macro_percent_upper"]
                 for summary in summaries]
        points.append({"k": k, "mean_lower_percent": statistics.mean(lower),
                       "mean_upper_percent": statistics.mean(upper),
                       "sd_lower_percent": statistics.stdev(lower),
                       "trial_lower_percent": lower, "trial_upper_percent": upper})
    # Same denominator B for every model/variant in this module. The curve is
    # held at its full-set endpoint once all assertions of a trial are selected.
    trial_area = [statistics.mean(summary["macro_curve"][min(k, summary["assertion_count"])]["macro_percent_lower"]
                                  for k in range(1, budget + 1)) for summary in summaries]
    trial_endpoint = [summary["macro_full_set_percent_lower"] for summary in summaries]
    trial_endpoint_upper = [summary["macro_full_set_percent_upper"] for summary in summaries]
    result = {"status": "PASS", "trials": 3, "assertion_budget": budget,
              "active_families": family_lists[0],
              "assertion_counts": [summary["assertion_count"] for summary in summaries],
              "trial_full_set_percent": trial_endpoint,
              "average_full_set_percent": statistics.mean(trial_endpoint),
              "trial_full_set_upper_percent": trial_endpoint_upper,
              "average_full_set_upper_percent": statistics.mean(trial_endpoint_upper),
              "sd_full_set_percent": statistics.stdev(trial_endpoint),
              "trial_fixed_budget_area_percent": trial_area,
              "average_fixed_budget_area_percent": statistics.mean(trial_area),
              "sd_fixed_budget_area_percent": statistics.stdev(trial_area),
              "curve": points,
              "area_definition": "mean of k=1..B held-out macro detection; endpoint padded after AQ count",
              "note": "average of three scores/curves, not a combined assertion set"}
    save_json(group_dir / "average.json", result)
    with (group_dir / "average_curve.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["k", "mean_lower_percent", "mean_upper_percent", "sd_lower_percent"])
        writer.writeheader()
        writer.writerows({key: point[key] for key in writer.fieldnames} for point in points)
    svg_points = [{"k": point["k"], "lower": point["mean_lower_percent"],
                   "upper": point["mean_upper_percent"]} for point in points]
    write_svg(group_dir / "average_curve.svg", svg_points, budget,
              title="Mean held-out detection across three trials",
              subtitle=f"Same RTL fault pool; common assertion budget B={budget}",
              lower_key="lower", upper_key="upper", ylabel="Mean detection (%)")
    return result


def summarize_groups(rows: list[dict], module_dir: Path, models: list[str],
                     variants: list[str], budget: int) -> None:
    for variant in variants:
        for model in models:
            group = [row for row in rows if row["variant"] == variant and row["model"] == model]
            group_dir = module_dir / variant / model
            result = average_runs(group, group_dir, budget)
            table = [{"trial": f"run_{row['run']:02d}", "status": row["status"],
                      "aq_count": row["aq_count"],
                      "full_set_percent": row["full_set_percent"],
                      "fixed_budget_area_percent": row["fixed_budget_area_percent"],
                      "source": row["run_dir"]} for row in group]
            table.append({"trial": "AVERAGE", "status": result["status"], "aq_count": "",
                          "full_set_percent": result.get("average_full_set_percent", ""),
                          "fixed_budget_area_percent": result.get("average_fixed_budget_area_percent", ""),
                          "source": str(group_dir / "average.json")})
            with (group_dir / "model_results.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(table[0]))
                writer.writeheader()
                writer.writerows(table)


def preserve_previous_run(run_dir: Path, manifest: dict) -> None:
    old = run_dir / "manifest.json"
    if not old.is_file() or json.loads(old.read_text()) == manifest:
        return
    history = run_dir / "history" / sha(old.read_bytes())
    paths = [old, run_dir / "run_summary.json", run_dir / "jasper/matrix.csv"]
    paths += [p for p in (run_dir / "evaluation").glob("*") if p.is_file()]
    for path in paths:
        if path.is_file():
            target = history / path.relative_to(run_dir)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(path.read_bytes())


def pool_environment(config_path: Path) -> str:
    config = json.loads(config_path.read_text())
    root = resolve(config.get("project_root", "."), config_path.parent)
    return signature(environment(config, root))


def cached_run_fresh(record: dict, run_dir: Path, validated: dict) -> bool:
    """Validate archived evidence; never launch a new golden proof."""
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text())
        summary = json.loads((run_dir / "evaluation" / "summary.json").read_text())
        if (manifest.get("assertions") != record["assertions"]
                or manifest.get("mutants") != validated["mutants"]
                or summary["assertion_count"] != len(record["assertions"])):
            return False
        matrix = read_matrix(run_dir / "jasper" / "matrix.csv", record["assertions"],
                             {m["id"]: m["family"] for m in validated["mutants"]})
        path = Path(record["config"])
        config = json.loads(path.read_text())
        root = resolve(config.get("project_root", "."), path.parent)
        golden = [resolve(x, root) for x in config["rtl_files"]]
        aq = resolve(config["assertion_file"], root)
        binds = [resolve(x, root) for x in config.get("bind_files", [])]
        includes = [resolve(x, root) for x in config.get("include_dirs", [])]
        checker = Checker(aq.read_text(), record["assertions"], str(aq))
        env = environment(config, root, checker, aq)
        legacy = False
        for item in validated["mutants"]:
            result = json.loads((run_dir / "jasper" / "runs" / item["id"] / "result.json").read_text())
            replaced = resolve(item["replace"], root)
            mutant = resolve(item["file"], run_dir)
            if replaced not in golden:
                return False
            if result.get("schema") == 2:
                key = job_key(env, checker, golden.index(replaced), mutant.read_bytes(), mutant)
                if result.get("context_sha256") != key or result.get("assertion_keys") != checker.keys:
                    return False
            else:
                legacy = True
                rtl = [mutant if p == replaced else p for p in golden]
                tcl = make_tcl(config, rtl, aq, binds, includes, config.get("defines", []))
                if result.get("input_sha256") != input_fingerprint(tcl, config, [*rtl, aq, *binds], includes):
                    return False
            if any(result.get("properties", {}).get(a) != matrix[a, item["id"]] for a in record["assertions"]):
                return False
        if legacy:
            # The old golden file is read only to verify pool lineage when the
            # old manifest predates pool fingerprints. It is not rerun.
            old = json.loads((run_dir / "jasper" / "runs" / "golden" / "result.json").read_text())
            tcl = make_tcl(config, golden, aq, binds, includes, config.get("defines", []))
            if old.get("input_sha256") != input_fingerprint(tcl, config, [*golden, aq, *binds], includes):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def validated_pool_can_resume(validated_path: Path, exported: list[tuple]) -> dict | None:
    try:
        validated = json.loads(validated_path.read_text())
        if not validated.get("mutants") or not all(m.get("syntax_validated") is True for m in validated["mutants"]):
            return None
        with (validated_path.parent / "validation_report.csv").open() as handle:
            statuses = {r["id"]: r["status"] for r in csv.DictReader(handle)}
        if any(statuses.get(m["id"]) != "PASS" for m in validated["mutants"]):
            return None
        for m in validated["mutants"]:
            path = resolve(m["file"], validated_path.parent)
            if not path.is_file() or (m.get("sha256") and m["sha256"] != sha(path.read_bytes())):
                return None
        identity = pool_environment(Path(exported[0][1]["config"]))
        metadata_path = validated_path.parent / "pool_context.json"
        if metadata_path.is_file():
            metadata = json.loads(metadata_path.read_text())
            if metadata.get("environment_sha256") == identity and metadata.get("manifest_sha256") == signature(validated):
                return validated
        elif any(cached_run_fresh(record, run_dir, validated) for _, record, run_dir in exported):
            save_json(metadata_path, {"environment_sha256": identity, "manifest_sha256": signature(validated),
                                      "origin": "verified legacy results"})
            return validated
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def run_module(module: dict, output: Path, models: list[str], variants: list[str],
               backend: str, generator_env: dict | None, cap: int | None,
               seed: int, dry_run: bool, resume: bool = False, *, check_only: bool = False,
               reuse_only: bool = False, refresh_pool: bool = False,
               force_results: bool = False, retry_unknown: bool = False,
               install_pyslang: bool = False, reuse_from: Path | None = None) -> list[dict]:
    module_dir = output / module["name"]
    cache_dir = output / "_shared_results"
    if not dry_run and not check_only:
        # Read old snapshots before writing current Step A exports into them.
        seed_existing_module(module_dir, cache_dir)
        if reuse_from and reuse_from.resolve() != output.resolve():
            seed_existing_module(reuse_from / module["name"], cache_dir, report_dir=module_dir / "external_reuse")
    rows, exported = [], []
    for variant in variants:
        for model in models:
            for trial in range(1, 4):
                run_dir = module_dir / variant / model / f"run_{trial:02d}"
                row = {"module": module["name"], "variant": variant, "model": model,
                       "run": trial, "status": "PENDING", "run_dir": str(run_dir),
                       "aq_count": "", "full_set_percent": "", "fixed_budget_area_percent": "",
                       "reason": "", "evaluation_summary": ""}
                rows.append(row)
                try:
                    record = export(module["base_config"], model, variant, trial,
                                    module["source"], (module_dir / "checked_step_a_outputs" / variant / model / f"run_{trial:02d}" / "input")
                                    if check_only else run_dir / "input")
                    row["aq_count"] = record["final_aq_count"]
                    exported.append((row, record, run_dir))
                except Exception as error:
                    row["status"], row["reason"] = "INPUT_ERROR", str(error)
    budget = max((row["aq_count"] for row, _, _ in exported), default=0)
    try:
        audit_inputs(exported, rows, module_dir)
        if len(exported) != len(rows):
            raise ValueError("not all requested model/run Step A outputs are available and valid")
    except Exception as error:
        for row, _, _ in exported:
            row["status"], row["reason"] = "BLOCKED_INPUTS", str(error)
        if not check_only:
            for row in rows:
                save_json(Path(row["run_dir"]) / "run_summary.json", row)
            summarize_groups(rows, module_dir, models, variants, budget)
        return rows
    if check_only:
        for row, _, _ in exported:
            row["status"] = "INPUTS_VERIFIED"
        return rows
    nonempty = [entry for entry in exported if entry[1]['assertions']]
    if not nonempty:
        print(f"EMPTY_AQ {module['name']}: all {len(rows)} verified slots contain 0 assertions; no Jasper", flush=True)
        for row, record, run_dir in exported:
            row.update(status='EMPTY_AQ', reason='All requested slots for this module have empty final AQ; '
                       'retained in audit, outside common nonempty-module measurement scope.')
            save_json(run_dir / 'empty_aq.json', {'status': 'EMPTY_AQ', 'aq_sha256': row['aq_sha256'],
                                                 'export_record': str(Path(record['config']).with_name('export_record.json'))})
            save_json(run_dir / 'run_summary.json', row)
        summarize_groups(rows, module_dir, models, variants, budget)
        return rows
    representative = nonempty[0][1]
    pool = module_dir / "fault_pool"
    generated, validation = pool / "generated", pool / "validation"
    try:
        validated = None if refresh_pool or dry_run else validated_pool_can_resume(
            validation / "validated_manifest.json", exported)
        if validated is None:
            if reuse_only:
                raise ValueError("no verified existing fault pool; --reuse-only never runs Jasper")
            env = generator_env
            if backend == "ast" and env is None:
                env = ast_environment(output, install_pyslang)
            arguments = ["--config", representative["config"], "--target", module["target"],
                         "--output", str(generated), "--backend", backend, "--seed", str(seed)]
            if cap is not None:
                arguments += ["--max-per-family", str(cap)]
            run("generate_mutants.py", *arguments, env=env)
            run("validate_mutants.py", "--config", representative["config"],
                "--manifest", str(generated / "manifest.json"), "--output", str(validation),
                *(["--dry-run"] if dry_run else []))
            if not dry_run:
                validated = json.loads((validation / "validated_manifest.json").read_text())
                save_json(validation / "pool_context.json", {
                    "environment_sha256": pool_environment(Path(representative["config"])),
                    "manifest_sha256": signature(validated), "origin": "mutant syntax validation"})
        else:
            print(f"REUSE fixed fault pool: {validation / 'validated_manifest.json'}", flush=True)
        if dry_run:
            for row, _, _ in exported:
                row["status"] = "DRY_RUN"
            return rows
        counts = {f: sum(m["family"] == f for m in validated["mutants"]) for f in FAMILIES}
        save_json(pool / "pool_summary.json", {"mutants_per_family": counts,
                  "active_families": [f for f in FAMILIES if counts[f]],
                  "representative_config": representative["config"]})
        if sum(bool(v) for v in counts.values()) < 2:
            raise ValueError(f"held-out evaluation needs at least two nonempty families: {counts}")
        planned = shared_measurement_inputs(exported, module_dir)
        for shared in planned:
            print(f"SHARED {module['name']}: {len(shared['assertions'])} union assertions; {shared['folder']}", flush=True)
            folder = Path(shared["folder"])
            save_json(folder / "manifest.json", dict(validated, assertions=shared["assertions"]))
            run("collect_jasper.py", "--config", shared["config"], "--manifest", str(folder / "manifest.json"),
                "--output", str(folder / "jasper"), "--cache-dir", str(cache_dir),
                *(["--reuse-only"] if reuse_only else []), *(["--force"] if force_results else []),
                *(["--retry-unknown"] if retry_unknown else []))
    except Exception as error:
        for row, _, run_dir in exported:
            row["status"], row["reason"] = "SHARED_MEASUREMENT_ERROR", str(error)
            save_json(run_dir / "run_summary.json", row)
        summarize_groups(rows, module_dir, models, variants, budget)
        return rows
    pool_hash = signature(validated["mutants"])
    for row, record, run_dir in exported:
        print(f"SCORE {module['name']}/{row['variant']}/{row['model']}/run_{row['run']:02d}: select AQ rows from shared matrix", flush=True)
        try:
            manifest_path = run_dir / "manifest.json"
            desired = dict(validated, assertions=record["assertions"])
            preserve_previous_run(run_dir, desired)
            save_json(manifest_path, desired)
            # Assemble this actual Step A subset from shared cells; never prove
            # common assertions again or replace it with another trial's AQ.
            run("collect_jasper.py", "--config", record["config"], "--manifest", str(manifest_path),
                "--output", str(run_dir / "jasper"), "--cache-dir", str(cache_dir), "--reuse-only")
            run("evaluate.py", "--manifest", str(manifest_path), "--matrix", str(run_dir / "jasper" / "matrix.csv"),
                "--output", str(run_dir / "evaluation"))
            summary_path = run_dir / "evaluation" / "summary.json"
            summary = json.loads(summary_path.read_text())
            row.update(status="PASS", evaluation_summary=str(summary_path), pool_sha256=pool_hash,
                       full_set_percent=summary["macro_full_set_percent_lower"],
                       fixed_budget_area_percent=statistics.mean(
                           summary["macro_curve"][min(k, summary["assertion_count"])]["macro_percent_lower"]
                           for k in range(1, budget + 1)))
        except Exception as error:
            row["status"], row["reason"] = "EVALUATION_ERROR", str(error)
        save_json(run_dir / "run_summary.json", row)
    summarize_groups(rows, module_dir, models, variants, budget)
    return rows


def report_existing_module(module: dict, output: Path, models: list[str],
                           variants: list[str]) -> list[dict]:
    """Rebuild tables/averages from completed runs; never invoke Jasper or an LLM."""
    module_dir = output / module["name"]
    rows = []
    pool_hashes = set()
    for variant in variants:
        for model in models:
            for trial in range(1, 4):
                run_dir = module_dir / variant / model / f"run_{trial:02d}"
                summary_path = run_dir / "evaluation" / "summary.json"
                manifest_path = run_dir / "manifest.json"
                export_path = run_dir / "input" / "export_record.json"
                row = {"module": module["name"], "variant": variant, "model": model,
                       "run": trial, "status": "MISSING_RESULT", "run_dir": str(run_dir),
                       "aq_count": "", "full_set_percent": "", "fixed_budget_area_percent": "",
                       "reason": "", "evaluation_summary": ""}
                rows.append(row)
                try:
                    previous_path = run_dir / 'run_summary.json'
                    if previous_path.is_file() and export_path.is_file():
                        previous = json.loads(previous_path.read_text())
                        exported = json.loads(export_path.read_text())
                        if previous.get('status') == 'EMPTY_AQ' and exported.get('final_aq_count') == 0:
                            row.update(status='EMPTY_AQ', aq_count=0, aq_sha256=previous.get('aq_sha256'),
                                       reason=previous.get('reason', 'Verified empty AQ'))
                            continue
                    if not (summary_path.is_file() and manifest_path.is_file() and export_path.is_file()):
                        raise FileNotFoundError("completed evaluation, manifest, or exported AQ is missing")
                    summary = json.loads(summary_path.read_text(encoding="utf-8"))
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    exported = json.loads(export_path.read_text(encoding="utf-8"))
                    count = summary["assertion_count"]
                    if count < 0 or count != exported["final_aq_count"] or count != len(manifest["assertions"]):
                        raise ValueError("AQ counts disagree between export, manifest, and evaluation")
                    if manifest["assertions"] != exported["assertions"]:
                        raise ValueError("exported AQ and Jasper manifest use different assertions")
                    pool_hash = hashlib.sha256(json.dumps(manifest["mutants"], sort_keys=True).encode()).hexdigest()
                    pool_hashes.add(pool_hash)
                    row.update({"status": "PASS", "aq_count": count,
                                "full_set_percent": summary["macro_full_set_percent_lower"],
                                "evaluation_summary": str(summary_path),
                                "pool_sha256": pool_hash})
                except Exception as error:
                    row["reason"] = str(error)
                    previous_path = run_dir / "run_summary.json"
                    if previous_path.is_file():
                        previous = json.loads(previous_path.read_text(encoding="utf-8"))
                        row["aq_count"] = previous.get("aq_count", "")
                        row["reason"] = previous.get("reason") or row["reason"]
    if len(pool_hashes) > 1:
        for row in rows:
            if row["status"] == "PASS":
                row["status"] = "POOL_MISMATCH"
                row["reason"] = "completed runs do not share one fixed RTL mutant pool"
    known_counts = [row["aq_count"] for row in rows if isinstance(row["aq_count"], int) and row["aq_count"] > 0]
    budget = max(known_counts, default=0)
    if budget:
        for row in rows:
            if row["status"] != "PASS":
                continue
            summary = json.loads(Path(row["evaluation_summary"]).read_text(encoding="utf-8"))
            row["fixed_budget_area_percent"] = statistics.mean(
                summary["macro_curve"][min(k, summary["assertion_count"])]["macro_percent_lower"]
                for k in range(1, budget + 1))
    for row in rows:
        save_json(Path(row["run_dir"]) / "run_summary.json", row)
    summarize_groups(rows, module_dir, models, variants, budget)
    return rows


def write_master(output: Path, rows: list[dict], models: list[str], variants: list[str]) -> None:
    fields = ["module", "variant", "model", "run", "status", "aq_count", "aq_state", "aq_sha256",
              "full_set_percent", "fixed_budget_area_percent", "pool_sha256", "reason", "run_dir"]
    with (output / "all_runs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    average_rows = []
    for module in sorted({row["module"] for row in rows}):
        for variant in variants:
            for model in models:
                path = output / module / variant / model / "average.json"
                if path.is_file():
                    result = json.loads(path.read_text(encoding="utf-8"))
                else:
                    result = {"status": "INCOMPLETE"}
                average_rows.append({"module": module, "variant": variant, "model": model,
                                     "status": result["status"],
                                     "average_full_set_percent": result.get("average_full_set_percent", ""),
                                     "average_fixed_budget_area_percent": result.get("average_fixed_budget_area_percent", ""),
                                     "average_file": str(path)})
    with (output / "all_averages.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(average_rows[0]))
        writer.writeheader()
        writer.writerows(average_rows)
    for module in sorted({row["module"] for row in rows}):
        module_rows = [row for row in average_rows if row["module"] == module]
        with (output / module / "module_averages.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(average_rows[0]))
            writer.writeheader()
            writer.writerows(module_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True,
                        help="bsg-cache root containing rtl/, assertions/, aq_out/")
    parser.add_argument("--aq-config", type=Path, default=None,
                        help="defaults to project_root/aq_qualification_pipeline_v1/aq_config.json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    parser.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=["original"])
    parser.add_argument("--modules", nargs="+", default=None,
                        help="optional assertion-source names or module stems; default is all AQ-config mappings")
    parser.add_argument("--backend", choices=("ast", "lexical"), default="ast")
    parser.add_argument("--install-pyslang", action="store_true")
    parser.add_argument("--max-per-family", type=int, default=None)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="reuse a verified fixed fault pool and completed per-run Jasper results")
    parser.add_argument("--report-only", action="store_true",
                        help="rebuild tables and three-run averages from existing evaluations; no Jasper, mutants, or LLM")
    parser.add_argument("--check-step-a-outputs", action="store_true",
                        help="read/export and audit all requested Step A outputs; no Jasper or model API")
    parser.add_argument("--reuse-only", action="store_true", help="reuse matching evidence; never launch Jasper")
    parser.add_argument("--reuse-from", type=Path, help="also read matching proof results in an existing old batch directory")
    parser.add_argument("--refresh-pool", action="store_true", help="explicitly regenerate and revalidate the fault pool")
    parser.add_argument("--force-results", action="store_true", help="remeasure shared union assertions")
    parser.add_argument("--retry-unknown", action="store_true", help="retry cached UNKNOWN cells in the shared measurement only")
    args = parser.parse_args()
    if args.report_only and (args.check_step_a_outputs or args.dry_run or args.resume or args.reuse_only):
        parser.error("--report-only cannot be combined with input checks or Jasper run options")
    if args.reuse_only and (args.refresh_pool or args.force_results):
        parser.error("--reuse-only cannot regenerate or force measurements")
    project_root, output = args.project_root.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if not args.report_only:
        inspect_outputs(project_root, output, args.models, args.variants)
    aq_config = args.aq_config or project_root / "aq_qualification_pipeline_v1" / "aq_config.json"
    modules = module_configs(project_root, output, aq_config.resolve())
    if args.modules:
        wanted = set(args.modules)
        modules = [module for module in modules
                   if module["name"] in wanted or module["source"] in wanted]
        if not modules or len(modules) != len(wanted):
            found = {module["name"] for module in modules} | {module["source"] for module in modules}
            raise ValueError(f"Unknown/duplicate module selection: {sorted(wanted - found)}")
    env = None  # AST dependency loading is lazy; existing pools need no parser installation.
    all_rows = []
    for module in modules:
        print(f"\n=== MODULE {module['name']} ===", flush=True)
        if args.report_only:
            all_rows.extend(report_existing_module(module, output, args.models, args.variants))
        else:
            all_rows.extend(run_module(module, output, args.models, args.variants,
                                       args.backend, env, args.max_per_family, args.seed,
                                       args.dry_run, args.resume, check_only=args.check_step_a_outputs,
                                       reuse_only=args.reuse_only, refresh_pool=args.refresh_pool,
                                       force_results=args.force_results, retry_unknown=args.retry_unknown,
                                       install_pyslang=args.install_pyslang, reuse_from=args.reuse_from))
        if not args.check_step_a_outputs and not args.dry_run:
            write_master(output, all_rows, args.models, args.variants)
    if args.dry_run:
        save_json(output / "dry_run_plan.json", all_rows)
        return
    if args.check_step_a_outputs:
        save_json(output / "step_a_output_check.json", all_rows)
        failures = [row for row in all_rows if row["status"] != "INPUTS_VERIFIED"]
        if failures:
            primary = [row for row in failures if row["status"] == "INPUT_ERROR"] or failures
            print(f"\nINPUT CHECK FAILED: {len(failures)} blocked/failed slots; primary reasons:", flush=True)
            for row in primary:
                print(f"{row['status']} {row['module']}/{row['variant']}/{row['model']}/run_{row['run']:02d}: "
                      f"{row.get('reason', 'reason unavailable')}", flush=True)
            sys.exit(f"Step A output check failed; details: {output / 'step_a_output_check.json'}; no Jasper ran")
        print("All requested Step A outputs were verified; no Jasper or model API ran.")
        return
    print(f"\nPer-run results: {output / 'all_runs.csv'}")
    print(f"Per-model three-run averages: {output / 'all_averages.csv'}")
    if not args.dry_run and any(row["status"] not in {"PASS", "EMPTY_AQ"} for row in all_rows):
        sys.exit("Some runs could not be scored; inspect status and reason in all_runs.csv")


if __name__ == "__main__":
    main()
