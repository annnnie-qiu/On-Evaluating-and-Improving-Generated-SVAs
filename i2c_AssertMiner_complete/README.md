# i2c_AssertMiner_complete

- `rtl/`: design source.
- `assertions/`: input assertions.
- `buggy_assertions/`: controlled assertion inputs.
- `bind/`: checker connections.
- `spec/`: specification inputs.

From this folder:

```bash
python3 aq_qualification_pipeline_v1/run_aq_qualification.py inventory
python3 aq_qualification_pipeline_v1/run_aq_qualification.py
python3 aq_ablation_runner/four_model_spec_checker.py --project-root . --model gpt --runs 3
```

Use `-buggy` on both run commands for controlled inputs.
Formal checks need JasperGold. Specification checks need the selected API key.
See the tool READMEs before running.

Step B reads completed Step A outputs:

```bash
python3 stepb/run_stepb.py --project-root . --models gpt --variants original --check-only
```
