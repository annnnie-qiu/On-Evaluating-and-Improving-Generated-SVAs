#!/usr/bin/env python3
"""One-command entry point for Syntax -> FPV -> Vacuity -> AQ."""
from __future__ import annotations

import sys
from pathlib import Path

from assertion_aq_pipeline import main as pipeline_main


COMMANDS = {"inventory", "syntax", "fpv", "vacuity", "all", "summary"}


def main() -> int:
    args = list(sys.argv[1:])
    if not any(argument == "--config" or argument.startswith("--config=") for argument in args):
        default_config = Path(__file__).resolve().with_name("aq_config.json")
        args[0:0] = ["--config", str(default_config)]
    if not any(argument in COMMANDS for argument in args):
        args.append("all")
    return pipeline_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
