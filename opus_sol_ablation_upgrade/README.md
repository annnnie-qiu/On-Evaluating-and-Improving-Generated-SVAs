# Optional Model Tools

Add the `opus` and `gpt61` model entries to compatible tool copies.
Review the model IDs in `install_ablation_models.py` before use.

From this folder, check a target first:

```bash
python3 install_ablation_models.py --aq-runner ../i2c_AssertMiner_complete/aq_ablation_runner --loop ../assertion_loop --check-only
```

Run without `--check-only` to apply the update. The script makes backups.
Some supplied copies already have these entries.

`run_step_a_ablation.sh DATASET AQ_ABLATION_RUNNER` runs both models.
It needs JasperGold and the API keys used by the checker.
