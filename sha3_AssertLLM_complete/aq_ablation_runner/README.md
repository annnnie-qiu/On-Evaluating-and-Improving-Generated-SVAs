# Qualification Ablation

Remove one gate: `syntax`, `fpv`, `vacuity` or `llm`.
JasperGold is needed for formal checks. LLM checks need the selected API key.
Review `MODEL_CONFIGS` in `four_model_spec_checker.py` before use.

From the dataset folder:

```bash
python3 aq_ablation_runner/four_model_spec_checker.py --project-root . --model gpt --runs 1
python3 aq_ablation_runner/run_ablation.py --project-root . -remove fpv -normal --model gpt --runs 1
```

Use `-buggy` instead of `-normal` for controlled inputs.
Repeat `--model` to select more models.
New ablation outputs go to `aq_ablation_out/`.
