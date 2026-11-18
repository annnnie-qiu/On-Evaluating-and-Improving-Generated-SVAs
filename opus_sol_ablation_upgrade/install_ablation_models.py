#!/usr/bin/env python3
"""Add Opus 5 / GPT-6.1 Sol without replacing existing model or runtime settings."""
from __future__ import annotations
import argparse
import ast
import datetime
import json
from pathlib import Path
import shutil

NEW_MODELS = {
    "opus": ("Claude Opus 5", "anthropic/claude-opus-5"),
    "gpt61": ("GPT-6.1 Sol", "openai/gpt-6.1-sol"),
}
OLD_KEYS = '("qwen","deepseek","gpt","gemini")'
NEW_KEYS = '("qwen","deepseek","gpt","gemini","opus","gpt61")'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aq-runner", action="append", type=Path, required=True,
                        help="aq_ablation_runner folder; repeat for multiple datasets")
    parser.add_argument("--loop", type=Path, required=True, help="assertion_loop folder")
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    changes = {}

    def read(path):
        path = path.expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"File missing: {path}")
        return path, path.read_bytes().decode("utf-8")

    def replace_once(text, old, new, path):
        if new in text:
            return text
        if text.count(old) != 1:
            raise ValueError(f"Unrecognized version: {path}; expected one occurrence of {old!r}. No files changed.")
        return text.replace(old, new, 1)

    for folder in args.aq_runner:
        path, original = read(folder / "four_model_spec_checker.py")
        newline = "\r\n" if "\r\n" in original else "\n"
        source = original.replace("\r\n", "\n")
        tree = ast.parse(source)
        registry = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "MODEL_CONFIGS" for t in node.targets))
        if not isinstance(registry, ast.Dict):
            raise ValueError(f"Expected literal MODEL_CONFIGS: {path}")
        # IDs are literal strings, while endpoint values may be module constants.
        known = {ast.literal_eval(k): v for k, v in zip(registry.keys, registry.values)}
        additions = []
        for key, (display, model_id) in NEW_MODELS.items():
            if key in known:
                node = known[key]
                values = {ast.literal_eval(k): v for k, v in zip(node.keys, node.values)}
                if ast.literal_eval(values["model_id"]) != model_id:
                    raise ValueError(f"Existing {key} has a different ID: {path}")
                continue
            additions.append(f'''    "{key}": {{
        "display_name": "{display}",
        "model_id": "{model_id}",
        "api_type": "openrouter",
        "api_url": OPENROUTER_API_URL,
        "token_env": "OPENROUTER_API_KEY",
        "token_file": "OPENROUTER_API_KEY",
        "supports_temperature": False,
    }},
''')
        if additions:
            lines = source.splitlines(keepends=True)
            # The registry's closing brace is on its own line in this runner.
            if lines[registry.end_lineno - 1].strip() != "}":
                raise ValueError(f"Unrecognized registry layout: {path}")
            lines.insert(registry.end_lineno - 1, "".join(additions))
            source = "".join(lines)
        declaration = 'DEFAULT_MODEL_KEYS = ("qwen", "deepseek", "gpt", "gemini")'
        if declaration not in source:
            source = source.replace("MODEL_CONFIGS = {", declaration + "\n\nMODEL_CONFIGS = {", 1)
        source = replace_once(source, "selected_models = args.model or list(MODEL_CONFIGS)",
                              "selected_models = args.model or list(DEFAULT_MODEL_KEYS)", path)
        changes[path] = source.replace("\n", newline)

        path, source = read(folder / "run_ablation.py")
        source = replace_once(source, "selected_models = args.model or list(checker.MODEL_CONFIGS)",
                              'selected_models = args.model or list(getattr(checker, "DEFAULT_MODEL_KEYS", checker.MODEL_CONFIGS))', path)
        changes[path] = source

    path, source = read(args.loop / "config.json")
    config = json.loads(source)
    for key, (_, model_id) in NEW_MODELS.items():
        if key in config["models"] and config["models"][key]["id"] != model_id:
            raise ValueError(f"Existing {key} has a different ID: {path}")
        config["models"].setdefault(key, {"id": model_id, "supports_temperature": False})
    changes[path] = json.dumps(config, ensure_ascii=False, indent=2) + "\n"

    for name in ("run.py", "evaluate_frozen.py"):
        path, source = read(args.loop / name)
        old = OLD_KEYS if name == "run.py" else OLD_KEYS.replace('"', "'")
        new = NEW_KEYS if name == "run.py" else NEW_KEYS.replace('"', "'")
        changes[path] = replace_once(source, "choices=" + old, "choices=" + new, path)
    path, source = read(args.loop / "report_final.py")
    changes[path] = replace_once(source,
        "result=report_final(experiment,budget,write_outputs=False,verbose=False,\n                            models=['qwen','deepseek','gpt','gemini'])",
        "result=report_final(experiment,budget,write_outputs=False,verbose=False)", path)

    # Validate every target before writing anything; incompatible versions fail closed.
    changed = {}
    for path, source in changes.items():
        if path.suffix == ".py":
            ast.parse(source, filename=str(path))
        if source.encode("utf-8") != path.read_bytes():
            changed[path] = source
    if args.check_only:
        for path in changed:
            print(f"WOULD UPDATE {path}")
        print(f"Validation passed: {len(changed)} files need updating. No files changed.")
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    for path, source in changed.items():
        backup = path.with_name(path.name + ".before_opus_sol_" + stamp)
        shutil.copy2(path, backup)
        path.write_bytes(source.encode("utf-8"))
        print(f"UPDATED {path}\n  BACKUP {backup}")
    print(f"Done: {len(changed)} files updated. Existing model IDs, credentials and runtime settings preserved.")


if __name__ == "__main__":
    main()
