# Step B

Compare explicit RTL faults with JasperGold mutation coverage.
Input is three completed Step A runs for each selected model and variant.

From this folder:

```bash
python3 run_stepb.py --project-root /path/to/dataset --models gpt --variants original --check-only
python3 run_stepb.py --project-root /path/to/dataset --models gpt --variants original
```

Install `pyslang==11.0.0` for AST faults. JasperGold needs mutation coverage support.
Use `--variants original` to select only the normal inputs.
New outputs go under the dataset's `aq_out/fault_holdout_batch/`.
See `--help` for stage and cache options.
