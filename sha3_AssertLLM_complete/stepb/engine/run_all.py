#!/usr/bin/env python3
"""One-command candidate generation, Jasper checks, scoring, and six plots."""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
FAMILIES = ("F1", "F2", "F3", "F4", "F5")


def run(script: str, *arguments: str, env: dict[str, str] | None = None) -> None:
    command = [sys.executable, str(SCRIPTS / script), *arguments]
    print(f"\n=== {script} ===", flush=True)
    subprocess.run(command, check=True, env=env)


def ast_environment(output: Path, install: bool) -> dict[str, str]:
    """Find pyslang or, when requested, install it only under this run output."""
    env = os.environ.copy()
    deps = output / ".python_deps"
    if deps.is_dir():
        env["PYTHONPATH"] = os.pathsep.join(
            part for part in (str(deps), env.get("PYTHONPATH", "")) if part)
    probe = [sys.executable, "-c", "import pyslang"]
    if subprocess.run(probe, env=env, capture_output=True, check=False).returncode == 0:
        return env
    if not install:
        raise RuntimeError(
            "AST backend needs pyslang in this Python environment. Rerun with "
            "--install-pyslang to install pyslang==11.0.0 under the output directory "
            "(no system Python changes), or use --backend lexical."
        )
    deps.mkdir(parents=True, exist_ok=True)
    print(f"Installing pyslang==11.0.0 into {deps} ...", flush=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "--target", str(deps),
                    "pyslang==11.0.0"], check=True)
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(deps), os.environ.get("PYTHONPATH", "")) if part)
    if subprocess.run(probe, env=env, capture_output=True, check=False).returncode != 0:
        raise RuntimeError(f"pyslang installation completed but {sys.executable} cannot import it from {deps}")
    return env


def write_results(output: Path, summary: dict) -> None:
    evaluation = output / "evaluation"
    rows = []
    with (evaluation / "fold_curves.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if int(row["k"]) == summary["assertion_count"]:
                rows.append({
                    "held_out_family": row["held_out_family"],
                    "test_mutants": row["test_mutants"],
                    "full_AQ_lower_percent": row["detection_percent_lower"],
                    "full_AQ_upper_percent": row["detection_percent_upper"],
                    "plot": str((evaluation / f"{row['held_out_family']}_curve.svg").resolve()),
                })
    rows.append({
        "held_out_family": "AVG (equal-family)",
        "test_mutants": sum(summary["mutants_per_family"].values()),
        "full_AQ_lower_percent": summary["macro_full_set_percent_lower"],
        "full_AQ_upper_percent": summary["macro_full_set_percent_upper"],
        "plot": str((evaluation / "macro_curve.svg").resolve()),
    })
    with (output / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print("\nRESULTS (confirmed lower %; possible upper % includes UNKNOWN):")
    for row in rows:
        print(f"{row['held_out_family']:20s} N={row['test_mutants']:>5} "
              f"{float(row['full_AQ_lower_percent']):6.2f}%.."
              f"{float(row['full_AQ_upper_percent']):6.2f}%")
    print(f"{len(summary['active_families']) + 1} plots and complete plot data: "
          f"{evaluation}; {evaluation / 'plot_data.json'}")
    print(f"Endpoint table: {output / 'results.csv'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True, help="one path from config.rtl_files")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("ast", "lexical"), default="ast")
    parser.add_argument("--install-pyslang", action="store_true",
                        help="if absent, install pinned pyslang into output/.python_deps")
    parser.add_argument("--max-per-family", type=int, default=None,
                        help="optional pilot cap; absent means all candidates")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true",
                        help="generate candidates and Tcl files only; no Jasper results or plots")
    parser.add_argument("--cache-dir", type=Path, help="shared property result cache")
    parser.add_argument("--reuse-only", action="store_true", help="require existing pool/results; no Jasper")
    parser.add_argument("--refresh-pool", action="store_true")
    parser.add_argument("--retry-unknown", action="store_true")
    args = parser.parse_args()
    if args.reuse_only and args.refresh_pool:
        parser.error("--reuse-only cannot refresh the fault pool")
    config, output = args.config.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    generated, validation, run_output = output / "generated", output / "validation", output / "jasper"
    generation_args = ["--config", str(config), "--target", args.target,
                       "--output", str(generated), "--backend", args.backend, "--seed", str(args.seed)]
    if args.max_per_family is not None:
        generation_args.extend(("--max-per-family", str(args.max_per_family)))
    from shared_results import environment, save_json, signature, sha
    from collect_jasper import resolve
    settings = json.loads(config.read_text())
    root = resolve(settings.get("project_root", "."), config.parent)
    identity = signature(environment(settings, root))
    validated_manifest = validation / "validated_manifest.json"
    metadata_path = validation / "pool_context.json"
    validated = None
    if not args.refresh_pool and not args.dry_run and validated_manifest.is_file() and metadata_path.is_file():
        old = json.loads(validated_manifest.read_text())
        metadata = json.loads(metadata_path.read_text())
        if (metadata.get("environment_sha256") == identity and metadata.get("manifest_sha256") == signature(old)
                and all(m.get("syntax_validated") is True and Path(m["file"]).is_file()
                    and m.get("sha256") == sha(Path(m["file"]).read_bytes()) for m in old["mutants"])):
            validated = old
    if validated is None:
        if args.reuse_only:
            raise RuntimeError("no verified existing fault pool; --reuse-only never invokes Jasper")
        generator_env = ast_environment(output, args.install_pyslang) if args.backend == "ast" else None
        run("generate_mutants.py", *generation_args, env=generator_env)
        candidate_manifest = generated / "manifest.json"
        validation_args = ["--config", str(config), "--manifest", str(candidate_manifest), "--output", str(validation)]
        run("validate_mutants.py", *validation_args, *(["--dry-run"] if args.dry_run else []))
        if args.dry_run:
            run("collect_jasper.py", "--config", str(config), "--manifest", str(candidate_manifest),
                "--output", str(run_output), "--dry-run")
            print("Dry run complete; no Jasper or model API was invoked.")
            return
        validated = json.loads(validated_manifest.read_text())
        save_json(metadata_path, {"environment_sha256": identity, "manifest_sha256": signature(validated)})
    else:
        print(f"REUSE fixed fault pool: {validated_manifest}", flush=True)
    counts = {family: sum(m["family"] == family for m in validated["mutants"])
              for family in FAMILIES}
    if sum(count > 0 for count in counts.values()) < 2:
        raise RuntimeError(f"Held-out evaluation needs at least two nonempty validated families: {counts}. "
                           "Try another target RTL or expand the mutation templates; no score is invented.")
    run("collect_jasper.py", "--config", str(config), "--manifest", str(validated_manifest),
        "--output", str(run_output),
        *(["--cache-dir", str(args.cache_dir)] if args.cache_dir else []),
        *(["--reuse-only"] if args.reuse_only else []),
        *(["--retry-unknown"] if args.retry_unknown else []))
    run("evaluate.py", "--manifest", str(validated_manifest),
        "--matrix", str(run_output / "matrix.csv"), "--output", str(output / "evaluation"))
    summary = json.loads((output / "evaluation" / "summary.json").read_text(encoding="utf-8"))
    write_results(output, summary)


if __name__ == "__main__":
    main()
