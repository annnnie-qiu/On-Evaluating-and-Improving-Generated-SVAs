#!/usr/bin/env python3
"""Run the existing Step B with a verified Qwen 32B model label."""
from __future__ import annotations

import argparse
import json
import runpy
import sys
from pathlib import Path

MODEL_ID = "qwen/qwen-2.5-coder-32b-instruct"


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--models", nargs="+", default=["qwen"])
    parser.add_argument("--variants", nargs="+", default=["original", "controlled"])
    args, remaining = parser.parse_known_args()
    root = args.project_root.expanduser().resolve()
    script = Path(__file__).resolve().parent / "run_stepb.py"
    if not script.is_file():
        parser.error(f"Place this launcher beside the existing stepb/run_stepb.py: {script}")
    if "--help" not in remaining and "-h" not in remaining and "qwen" in args.models:
        for variant in args.variants:
            aq_name = {"original": "aq_out", "controlled": "aq_buggy_out"}.get(variant)
            if aq_name is None:
                parser.error(f"Unknown variant: {variant}")
            for trial in range(1, 4):
                folder = root / aq_name / "spec_guard_four_models/qwen" / f"run_{trial:02d}"
                try:
                    summary = json.loads((folder / "summary.json").read_text(encoding="utf-8-sig"))
                    rows = json.loads((folder / "spec_check_results.json").read_text(encoding="utf-8-sig"))
                    if (summary.get("model_id") != MODEL_ID or summary.get("run_status") != "COMPLETE"
                            or not isinstance(rows, list)
                            or any(not isinstance(row, dict) or row.get("model_id") != MODEL_ID for row in rows)):
                        raise ValueError("Qwen 32B results are missing, incomplete, or mixed with another model")
                except (OSError, ValueError, TypeError, AttributeError) as error:
                    parser.error(f"{variant}/qwen/run_{trial:02d}: {error}. Complete the matching OpenRouter Step A first.")
    namespace = runpy.run_path(str(script), run_name="openrouter_qwen_stepb")
    namespace["MODEL_NAMES"]["qwen"] = "Qwen2.5-Coder-32B-Instruct (OpenRouter)"
    sys.argv = [str(script), "--project-root", str(root), "--models", *args.models,
                "--variants", *args.variants, *remaining]
    return namespace["main"]()


if __name__ == "__main__":
    raise SystemExit(main())
