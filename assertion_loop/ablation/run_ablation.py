#!/usr/bin/env python3
"""Compare configurable prefixes of one Step B trajectory per model/unit/repeat."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from run import main
if __name__=="__main__":
    raise SystemExit(main(ablation=True))
