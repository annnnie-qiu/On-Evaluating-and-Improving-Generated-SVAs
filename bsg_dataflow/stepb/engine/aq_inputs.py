"""Audit real Step A model/run outputs and plan shared proof inputs."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from shared_results import Checker, aq_parser, environment, save_json, signature


def audit_inputs(exported: list[tuple], rows: list[dict], module_dir: Path) -> dict:
    groups, cells, provenance = defaultdict(list), [], defaultdict(list)
    sets = {}
    for row, record, _ in exported:
        path = Path(record["aq_file"])
        checker = Checker(path.read_text(), record["assertions"], str(path))
        sets[checker.aq_sha256] = checker
        config = json.loads(Path(record["config"]).read_text())
        env = environment(config, Path(config["project_root"]), checker, path)
        group_key = signature([checker.aq_sha256, env])
        slot = {"variant": row["variant"], "model": row["model"], "run": row["run"],
                "aq_count": len(record["assertions"]), "assertion_names": " | ".join(record["assertions"]), "aq_sha256": checker.aq_sha256,
                "aq_state": "NONEMPTY" if record["assertions"] else "EMPTY_AQ",
                "group_sha256": group_key, "source": record["source"],
                "step_a_results": record["spec_results"], "aq_file": str(path),
                "step_a_total_final_aq": record["step_a_total_final_aq"],
                "step_a_a0": record.get("step_a_a0"),
                "step_a_yield_percent": record.get("step_a_final_yield_percent")}
        cells.append(slot)
        groups[group_key].append(slot)
        # Identical final decisions are allowed. Independent-run fingerprints
        # must nevertheless come from the matching Step A run, not a copied file.
        evidence = signature([row["variant"], record["step_a_decision_fingerprints"]])
        # Empty technical AQ has no model decisions or independent API calls.
        if record["step_a_decision_fingerprints"]:
            provenance[evidence].append(slot)
        row["aq_sha256"] = checker.aq_sha256
        row["aq_state"] = "NONEMPTY" if record["assertions"] else "EMPTY_AQ"
    duplicates = [slots for slots in provenance.values() if len(slots) > 1]
    result = {"requested_slots": len(rows), "available_slots": len(exported),
              "empty_aq_slots": sum(not r['assertions'] for _, r, _ in exported),
              "unique_aq_sets": len(groups), "slots": cells,
              "missing_or_invalid": [{k: r.get(k) for k in ("variant", "model", "run", "reason")}
                                     for r in rows if r["status"] == "INPUT_ERROR"],
              "groups": [{"sha256": key, "members": [
                  f"{s['variant']}/{s['model']}/run_{s['run']:02d}" for s in slots]}
                         for key, slots in groups.items()],
              "duplicate_step_a_provenance": duplicates,
              "note": "12 model/run slots need not be 12 different AQ contents; identical decisions are reported explicitly"}
    save_json(module_dir / "step_a_outputs.json", result)
    if cells:
        with (module_dir / "step_a_outputs.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(cells[0]))
            writer.writeheader()
            writer.writerows(cells)
    print(f"STEP A OUTPUTS {module_dir.name}: {len(exported)}/{len(rows)} real slots; {len(groups)} distinct AQ sets; "
          f"{result['empty_aq_slots']} EMPTY_AQ slots", flush=True)
    if duplicates:
        raise ValueError("different model/run slots have identical Step A decision fingerprints; inspect step_a_outputs.json")
    return result


def shared_measurement_inputs(exported: list[tuple], module_dir: Path) -> list[dict]:
    """Union subsets only when the original wrapper/helpers/assumptions match."""
    contexts = defaultdict(list)
    for row, record, _ in exported:
        if not record["assertions"]:
            continue
        path = Path(record["aq_file"])
        checker = Checker(path.read_text(), record["assertions"], str(path))
        config = json.loads(Path(record["config"]).read_text())
        env = environment(config, Path(config["project_root"]), checker, path)
        contexts[signature([env, checker.context_sha256])].append((record, checker, config))
    planned = []
    for key, members in contexts.items():
        record, checker, config = members[0]
        all_keys, conflict = {}, False
        for _, item, _ in members:
            for name, digest in item.keys.items():
                if name in all_keys and all_keys[name] != digest:
                    conflict = True
                all_keys[name] = digest
        source = Path(record["source"]).read_text()
        parser = aq_parser()
        blocks, errors = parser.extract_assertions(source)
        names = set(all_keys)
        rendered = parser.render_subset(source, blocks,
            {b.assertion_id for b in blocks if b.property_name in names}, label_unnamed=True)
        union = Checker(rendered, sorted(names), str(Path(record["aq_file"]).name))
        can_union = (not conflict and not errors and checker.partial and union.partial
                     and union.context_sha256 == checker.context_sha256
                     and union.keys == all_keys)
        if can_union:
            candidates = [(key, rendered, sorted(names), config)]
        else:
            # Unsupported scopes are still deduplicated as complete checkers.
            distinct = {item.aq_sha256: (r, item, c) for r, item, c in members}
            candidates = [(signature([key, digest]), item.source, item.assertions, c)
                          for digest, (r, item, c) in distinct.items()]
        for identity, text, assertions, original in candidates:
            folder = module_dir / "shared_measurements" / identity
            path = folder / Path(record["aq_file"]).name
            folder.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            new = dict(original, assertion_file=str(path.resolve()), assertions=assertions)
            config_path = folder / "config.json"
            save_json(config_path, new)
            planned.append({"config": str(config_path), "assertions": assertions,
                            "folder": str(folder), "aq_sha256": identity})
    save_json(module_dir / "shared_measurements.json", planned)
    return planned
