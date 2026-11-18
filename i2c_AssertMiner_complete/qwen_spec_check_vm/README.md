# Specification Checks

Read the specification and check technically qualified assertions.
Review API URLs and model settings in the scripts before use.

From the dataset folder:

```bash
python3 qwen_spec_check_vm/run_spec_all_models.py --project-root . --model gpt --runs 1
```

This command runs missing technical checks, then checks the selected model.
Use `-buggy` for controlled inputs. OpenRouter calls use `OPENROUTER_API_KEY`.

`run_spec_check_qwen.py` reads existing technical outputs and uses
`QWEN_API_TOKEN`. API calls use `curl`. PDF specifications also need `pypdf`.
