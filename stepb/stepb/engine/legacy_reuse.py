"""Promote existing per-trial results into the shared property cache.

Only freshly rehashed, matching source/config results are admitted. Original
logs, matrices and proof result files remain in place.
"""
from __future__ import annotations

import json
from pathlib import Path

from collect_jasper import input_fingerprint, make_tcl, resolve
from shared_results import Checker, ResultCache, STATUSES, environment, job_key, save_json, signature, sha


def seed_existing_module(module_dir: Path, cache_dir: Path, report_dir: Path | None = None) -> dict:
    report_dir = report_dir or module_dir
    cache = ResultCache(cache_dir)
    report = {"admitted_jobs": 0, "admitted_cells": 0, "skipped_jobs": 0, "runs": []}
    for config_path in sorted(module_dir.glob("*/*/run_*/input/run_config.json")):
        run_dir = config_path.parent.parent
        detail = {"run": str(run_dir), "admitted_jobs": 0, "skipped_jobs": 0}
        try:
            config = json.loads(config_path.read_text())
            manifest = json.loads((run_dir / "manifest.json").read_text())
            names = manifest["assertions"]
            if names != config["assertions"]:
                raise ValueError("config/manifest AQ mismatch")
            root = resolve(config.get("project_root", "."), config_path.parent)
            rtl = [resolve(x, root) for x in config["rtl_files"]]
            aq = resolve(config["assertion_file"], root)
            binds = [resolve(x, root) for x in config.get("bind_files", [])]
            includes = [resolve(x, root) for x in config.get("include_dirs", [])]
            checker = Checker(aq.read_text(), names, str(aq))
            env = environment(config, root, checker, aq)
            golden_cache = run_dir / "jasper/runs/golden/result.json"
            pool_manifest = module_dir / "fault_pool/validation/validated_manifest.json"
            if report_dir == module_dir and golden_cache.is_file() and pool_manifest.is_file():
                old = json.loads(golden_cache.read_text())
                tcl = make_tcl(config, rtl, aq, binds, includes, config.get("defines", []))
                if (old.get("input_sha256") == input_fingerprint(tcl, config, [*rtl, aq, *binds], includes)
                        and set(old.get("properties", {})) == set(names)
                        and all(x == "PROVEN" for x in old["properties"].values())):
                    pool = json.loads(pool_manifest.read_text())
                    if (pool.get("mutants") == manifest["mutants"] and all(
                            m.get("syntax_validated") is True
                            and resolve(m["file"], pool_manifest.parent).is_file()
                            and m.get("sha256") == sha(resolve(m["file"], pool_manifest.parent).read_bytes())
                            for m in pool["mutants"])):
                        save_json(pool_manifest.with_name("pool_context.json"), {
                            "environment_sha256": signature(environment(config, root)),
                            "manifest_sha256": signature(pool), "origin": str(golden_cache)})
            for item in manifest["mutants"]:
                path = run_dir / "jasper" / "runs" / item["id"] / "result.json"
                if not path.is_file():
                    detail["skipped_jobs"] += 1
                    continue
                result = json.loads(path.read_text())
                properties = result.get("properties", {})
                replaced = resolve(item["replace"], root)
                mutant = resolve(item["file"], run_dir)
                if replaced not in rtl or not mutant.is_file():
                    detail["skipped_jobs"] += 1
                    continue
                key = job_key(env, checker, rtl.index(replaced), mutant.read_bytes(), mutant)
                fresh = (result.get("context_sha256") == key
                         and result.get("assertion_keys") == checker.keys)
                if result.get("input_sha256"):
                    files = [mutant if p == replaced else p for p in rtl]
                    tcl = make_tcl(config, files, aq, binds, includes, config.get("defines", []))
                    fresh = result["input_sha256"] == input_fingerprint(tcl, config, [*files, aq, *binds], includes)
                if not fresh or set(properties) != set(names) or not set(properties.values()) <= STATUSES:
                    detail["skipped_jobs"] += 1
                    continue
                cache.put(key, checker, properties, str(path))
                detail["admitted_jobs"] += 1
                report["admitted_cells"] += len(properties)
        except (OSError, ValueError, KeyError, TypeError) as error:
            # Conflicting definitive statuses are a data-integrity failure, not
            # permission to silently choose one of the two old results.
            if "conflicting CEX/PROVEN" in str(error):
                raise
            detail["reason"] = str(error)
        report["admitted_jobs"] += detail["admitted_jobs"]
        report["skipped_jobs"] += detail["skipped_jobs"]
        report["runs"].append(detail)
    if report["runs"]:
        save_json(report_dir / "legacy_reuse.json", report)
        print(f"OLD RESULTS {module_dir.name}: admitted {report['admitted_jobs']} mutant jobs / "
              f"{report['admitted_cells']} cells; skipped {report['skipped_jobs']} stale/missing jobs", flush=True)
    return report
