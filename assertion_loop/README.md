# Assertion Improvement

Start from a dataset's input assertions. Qualify them in Step A, evaluate
fault detection in Step B, then use development faults to request improvements.

Review `config.json` and set `OPENROUTER_API_KEY` before a real run.
Install `pyslang==11.0.0`. Formal checks need JasperGold and `curl` is used for APIs.

From this folder, make a plan without API or Jasper calls:

```bash
python3 run.py ../i2c_AssertMiner_complete --model gpt --plan
```

Run the loop:

```bash
python3 run.py ../i2c_AssertMiner_complete --model gpt --rounds 3 --experiment main
```

Run budget or feedback comparisons:

```bash
python3 ablation/run_ablation.py ../i2c_AssertMiner_complete --model gpt --budgets 1,3,5 --experiment budgets
python3 controlled/run_controlled.py ../i2c_AssertMiner_complete --model gpt --budgets 1,3,5 --experiment feedback
```

Budgets count completed Step B rounds. Step A attempts are separate.
Development faults provide feedback. Validation compares budgets. Test faults
stay separate for `evaluate_frozen.py`. Use its `--help` for frozen evaluation.
New files go under the dataset's `gen_assertions/`.

`MANIFEST.json` keeps code compatibility data used by the program.
`examples/` contains templates with placeholder paths.

Offline tests use fake APIs and a fake Jasper process:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```
