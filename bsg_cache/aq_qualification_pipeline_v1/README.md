# Technical Qualification

Run syntax, formal proof and vacuity checks with JasperGold.
Review `aq_config.json` for RTL files, binds, clock and reset.

From the dataset folder:

```bash
python3 aq_qualification_pipeline_v1/run_aq_qualification.py inventory
python3 aq_qualification_pipeline_v1/run_aq_qualification.py
```

Use `-buggy` for `buggy_assertions/`.
New outputs go to `aq_out/` or `aq_buggy_out/`.
JSON config uses the standard library. YAML config also needs PyYAML.
