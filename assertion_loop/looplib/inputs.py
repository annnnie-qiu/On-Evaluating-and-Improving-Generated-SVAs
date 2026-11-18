"""Import existing checker assertions without relocating their fixed context.

The source file stays untouched.  Each imported assertion has one expression
slot; clock/reset prefixes, action blocks and surrounding procedural/generate
scopes are kept in the derived checker template.
"""
from __future__ import annotations

import copy
import hashlib
import re
import sys
from pathlib import Path

from .common import ROOT, EnvironmentSetupError, resolve, save

sys.path.insert(0, str(ROOT / "vendor" / "aq"))
import assertion_aq_pipeline as aq

ADDITION_MARKER = "/* LOOP_ASSERTIONS */"
SOURCE_SUFFIXES = {".v", ".sv", ".vh", ".svh", ".sva"}


def _source(path):
    # Path.read_text() translates CRLF.  Fixed wrappers must retain their bytes.
    return Path(path).read_bytes().decode("utf-8")


def _checker_span(source, checker, filename):
    masked, errors = aq.mask_comments_and_strings(source)
    if errors:
        raise EnvironmentSetupError(f"Cannot import {filename}: {'; '.join(errors)}")
    declarations = list(re.finditer(r"\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_$][\w$]*)\b", masked))
    matches = [m for m in declarations if m[1] == checker]
    if len(matches) != 1:
        raise EnvironmentSetupError(f"{filename}: expected exactly one module {checker}; found {len(matches)}")
    match = matches[0]
    end = re.search(r"\bendmodule\b", masked[match.end():])
    if end is None:
        raise EnvironmentSetupError(f"{filename}: checker module {checker} has no endmodule")
    end_start = match.end() + end.start()
    if any(match.start() < m.start() < end_start for m in declarations):
        raise EnvironmentSetupError(f"{filename}: cannot safely identify checker module boundaries")
    return match.start(), end_start, masked


def _matching_source(dataset, unit):
    checker = unit["checker"]
    configured = unit.get("assertion_files")
    if configured is None and unit.get("assertion_source") is not None:
        configured = [unit["assertion_source"]]
    if configured is not None:
        if not isinstance(configured, list) or len(configured) != 1 or not isinstance(configured[0], str):
            raise EnvironmentSetupError(f"{unit['name']}: assertion_files must contain exactly one checker source path")
        path = resolve(dataset, configured[0])
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            raise EnvironmentSetupError(f"{unit['name']}: missing or unsupported assertion source: {configured[0]}")
        try:
            path.relative_to(dataset)
        except ValueError as exc:
            raise EnvironmentSetupError(f"{unit['name']}: assertion source must be inside the dataset: {path}") from exc
        _checker_span(_source(path), checker, path)
        return path
    folder = dataset / "assertions"
    if not folder.is_dir():
        raise EnvironmentSetupError(f"{unit['name']}: dataset assertions/ folder is missing: {folder}")
    matches = []
    declaration = re.compile(r"\bmodule\s+(?:(?:automatic|static)\s+)?" + re.escape(checker) + r"\b")
    for path in sorted(folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        source = _source(path)
        masked, errors = aq.mask_comments_and_strings(source)
        if declaration.search(masked):
            if errors:
                raise EnvironmentSetupError(f"Cannot import {path}: {'; '.join(errors)}")
            matches.append(path)
    if len(matches) != 1:
        files = [str(p.relative_to(dataset)) for p in matches]
        raise EnvironmentSetupError(
            f"{unit['name']}: expected one source defining {checker} in assertions/; found {len(matches)}: {files}. "
            "Set this unit's assertion_files explicitly."
        )
    return matches[0]


def _expression_parts(block, filename, named_definitions):
    text = block.text
    masked, errors = aq.mask_comments_and_strings(text)
    if errors:
        raise EnvironmentSetupError(f"{filename}:{block.start_line}: unsafe assertion: {errors}")
    match = re.search(r"\bassert\b", masked)
    if match is None:
        raise EnvironmentSetupError(f"{filename}:{block.start_line}: assertion keyword not found")
    pos = aq.skip_space(masked, match.end())
    if block.assertion_kind == "concurrent":
        keyword = re.match(r"property\b", masked[pos:])
        if keyword is None:
            raise EnvironmentSetupError(f"{filename}:{block.start_line}: unsupported concurrent assertion")
        pos = aq.skip_space(masked, pos + keyword.end())
    else:
        delay = re.match(r"#\s*0\s*", masked[pos:])
        if delay:
            pos = aq.skip_space(masked, pos + delay.end())
    if pos >= len(masked) or masked[pos] != "(":
        raise EnvironmentSetupError(f"{filename}:{block.start_line}: assertion expression is not parenthesized")
    close = aq.matching_delimiter(masked, pos, "(", ")")
    if close is None:
        raise EnvironmentSetupError(f"{filename}:{block.start_line}: assertion parentheses are unbalanced")
    expression_start = aq.skip_space(masked, pos + 1)
    if block.assertion_kind == "concurrent":
        # Explicit clock and disable iff are fixed source text, never repairable.
        if expression_start < close and masked[expression_start] == "@":
            event = aq.skip_space(masked, expression_start + 1)
            if event < close and masked[event] == "(":
                event_end = aq.matching_delimiter(masked, event, "(", ")")
                if event_end is None or event_end >= close:
                    raise EnvironmentSetupError(f"{filename}:{block.start_line}: cannot isolate fixed clock event")
                expression_start = aq.skip_space(masked, event_end + 1)
            else:
                event_name = re.match(r"(?:[A-Za-z_$][\w$.:]*|\*)", masked[event:close])
                if event_name is None:
                    raise EnvironmentSetupError(f"{filename}:{block.start_line}: unsupported fixed clock event")
                expression_start = aq.skip_space(masked, event + event_name.end())
        disable = re.match(r"disable\s+iff\b", masked[expression_start:close])
        if disable:
            guard = aq.skip_space(masked, expression_start + disable.end())
            if guard >= close or masked[guard] != "(":
                raise EnvironmentSetupError(f"{filename}:{block.start_line}: cannot isolate fixed disable iff")
            guard_end = aq.matching_delimiter(masked, guard, "(", ")")
            if guard_end is None or guard_end >= close:
                raise EnvironmentSetupError(f"{filename}:{block.start_line}: fixed disable iff is unbalanced")
            expression_start = aq.skip_space(masked, guard_end + 1)
    expression_end = close
    while expression_end > expression_start and text[expression_end - 1].isspace():
        expression_end -= 1
    expression = text[expression_start:expression_end]
    if not expression.strip():
        raise EnvironmentSetupError(f"{filename}:{block.start_line}: empty assertion expression")
    expression_masked = masked[expression_start:expression_end]
    referenced = sorted(name for name in named_definitions if re.search(r"(?<![\w$])" + re.escape(name) + r"(?![\w$])", expression_masked))
    if referenced:
        raise EnvironmentSetupError(
            f"{filename}:{block.start_line}: named property/sequence invocation {referenced} cannot be safely repaired in place; "
            "provide an assertion with an inline body without changing shared definitions or assumptions"
        )
    return text[:expression_start], expression, text[expression_end:]


def input_inventory(dataset, setup):
    """Read every supported assertion source and report its unit coverage.

    This function is read-only.  A source explicitly selected for a checker may
    have same-checker alternatives; those alternatives are listed as excluded
    by that explicit mapping.  An additional assertion-bearing checker with no
    configured unit is always unassigned, including one in an otherwise mapped
    multi-module source.  No bind, top or clock is invented by this inventory.
    """
    dataset = Path(dataset).resolve()
    folder = dataset / "assertions"
    paths = {path.resolve() for path in folder.rglob("*")
             if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES} if folder.is_dir() else set()
    mappings = []
    by_source = {}
    explicit_checkers = set()
    for unit in setup.get("units", []):
        path = _matching_source(dataset, unit)
        relative = str(path.relative_to(dataset))
        paths.add(path)
        explicit = unit.get("assertion_files") is not None or unit.get("assertion_source") is not None
        if explicit:
            explicit_checkers.add(unit["checker"])
        if unit.get("assertion_slots"):
            count = len(unit["assertion_slots"])
        else:
            source = _source(path)
            begin, end, _ = _checker_span(source, unit["checker"], relative)
            blocks, errors = aq.extract_assertions(source[begin:end])
            count = None if errors else len(blocks)
        mapping = {"unit": unit["name"], "checker": unit["checker"],
                   "source_files": [relative], "assertion_count": count,
                   "mapping": "explicit_source" if explicit else "checker_module"}
        mappings.append(mapping)
        by_source.setdefault(path, []).append(mapping)
    files = []
    module_pattern = re.compile(r"\bmodule\s+(?:(?:automatic|static)\s+)?([A-Za-z_$][\w$]*)\b")
    for path in sorted(paths):
        source = _source(path)
        relative = str(path.relative_to(dataset))
        masked, lexical_errors = aq.mask_comments_and_strings(source)
        blocks, parse_errors = aq.extract_assertions(source)
        errors = list(dict.fromkeys([*lexical_errors, *parse_errors]))
        declarations = list(module_pattern.finditer(masked))
        module_rows = []
        module_spans = []
        for declaration in declarations:
            ending = re.search(r"\bendmodule\b", masked[declaration.end():])
            end = declaration.end() + ending.end() if ending else len(source)
            start = declaration.start()
            module_spans.append((start, end))
            module_source = source[start:end]
            module_masked = masked[start:end]
            module_blocks, module_errors = aq.extract_assertions(module_source)
            has_assertions = bool(re.search(r"\bassert\b", module_masked))
            mapped_units = [mapping["unit"] for mapping in by_source.get(path, [])
                            if mapping["checker"] == declaration[1]]
            alternative = has_assertions and not mapped_units and declaration[1] in explicit_checkers
            status = ("non_target" if not has_assertions else "mapped" if mapped_units
                      else "excluded_by_explicit_mapping" if alternative else "unassigned")
            module_rows.append({"checker": declaration[1], "has_assertions": has_assertions,
                                "assertion_count": None if module_errors else len(module_blocks),
                                "mapped_units": mapped_units, "status": status})
        outside_assertions = [match for match in re.finditer(r"\bassert\b", masked)
                              if not any(start <= match.start() < end for start, end in module_spans)]
        if outside_assertions:
            module_rows.append({"checker": None, "has_assertions": True,
                                "assertion_count": None, "mapped_units": [], "status": "unassigned"})
        has_assertions = bool(re.search(r"\bassert\b", masked))
        unassigned = [module["checker"] or "<outside module>" for module in module_rows
                      if module["status"] == "unassigned"]
        mapped_units = [mapping["unit"] for mapping in by_source.get(path, [])]
        status = ("unassigned" if unassigned else "mapped" if mapped_units
                  else "excluded_by_explicit_mapping" if has_assertions else "non_target")
        files.append({"source_file": relative, "status": status, "has_assertions": has_assertions,
                      "assertion_count": None if errors else len(blocks), "parse_errors": errors,
                      "mapped_units": mapped_units, "modules": module_rows,
                      "unassigned_checkers": unassigned})
    duplicate_mappings = [{"source_file": str(path.relative_to(dataset)),
                           "units": [mapping["unit"] for mapping in unit_mappings],
                           "checkers": [mapping["checker"] for mapping in unit_mappings]}
                          for path, unit_mappings in sorted(by_source.items()) if len(unit_mappings) > 1]
    return {"version": 1, "source_directory": "assertions", "supported_suffixes": sorted(SOURCE_SUFFIXES),
            "files": files, "units": mappings,
            "mapped_files": [row["source_file"] for row in files if row["mapped_units"]],
            "unassigned_files": [row["source_file"] for row in files if row["unassigned_checkers"]],
            "excluded_alternative_files": [row["source_file"] for row in files
                                           if row["status"] == "excluded_by_explicit_mapping"],
            "support_files": [row["source_file"] for row in files if row["status"] == "non_target"],
            "duplicate_mappings": duplicate_mappings,
            "source_file_count": len(files), "mapped_source_file_count": sum(bool(row["mapped_units"]) for row in files),
            "source_assertion_count": sum(row["assertion_count"] or 0 for row in files),
            "mapped_unit_assertion_count": sum(mapping["assertion_count"] or 0 for mapping in mappings),
            "unknown_count_files": [row["source_file"] for row in files if row["assertion_count"] is None]}


def prepare_existing(dataset, setup):
    """Return imported units; write derived templates/manifests, never originals.

    One checker source per unit is supported.  Assertions retain their original
    procedural/generate scopes.  Named property/sequence invocations are rejected
    rather than editing definitions that may also be used by fixed assumptions.
    """
    dataset = Path(dataset).resolve()
    result = copy.deepcopy(setup)
    for unit in result.get("units", []):
        path = _matching_source(dataset, unit)
        relative = str(path.relative_to(dataset))
        source = _source(path)
        begin, end, masked = _checker_span(source, unit["checker"], relative)
        if ADDITION_MARKER in source or re.search(r"/\* LOOP_SLOT\s", source):
            raise EnvironmentSetupError(f"{relative}: source contains reserved loop template markers")
        blocks, errors = aq.extract_assertions(source[begin:end])
        if errors:
            raise EnvironmentSetupError(f"Cannot safely import {relative}: {'; '.join(errors)}")
        if not blocks:
            raise EnvironmentSetupError(f"{relative}: checker {unit['checker']} contains no importable assertions")
        # Assertion extraction offsets are relative to the selected module.
        definitions = set(re.findall(r"\b(?:property|sequence)\s+([A-Za-z_$][\w$]*)\s*(?:\(|;)", masked[begin:end]))
        original_labels = [b.property_name for b in blocks if b.named]
        duplicates = sorted({name for name in original_labels if original_labels.count(name) > 1})
        if duplicates:
            raise EnvironmentSetupError(f"{relative}: duplicate assertion labels are ambiguous: {duplicates}")
        used_labels = set(original_labels)
        slots = []
        replacements = []
        for ordinal, block in enumerate(blocks, 1):
            slot_id = f"input_{ordinal:04d}"
            source_line = source.count("\n", 0, begin + block.start) + 1
            label = block.property_name if block.named else f"loop_{slot_id}"
            if not block.named:
                if label in used_labels:
                    raise EnvironmentSetupError(f"{relative}: imported unnamed label conflicts with {label}")
                used_labels.add(label)
            prefix, expression, suffix = _expression_parts(block, relative, definitions)
            if not block.named:
                prefix = f"{label}: " + prefix
            slots.append({"slot_id": slot_id, "label": label, "expression": expression,
                          "prefix": prefix, "suffix": suffix, "original_statement": block.text,
                          "source_file": relative, "source_line": source_line,
                          "assertion_kind": block.assertion_kind, "source_named": block.named})
            replacements.append((begin + block.start, begin + block.end, f"/* LOOP_SLOT {slot_id} */"))
        # Zero-byte insertion: removing the marker reproduces the surrounding
        # source exactly, including CRLF and an endmodule on the same line.
        replacements.append((end, end, ADDITION_MARKER))
        parts = []
        cursor = 0
        for start, stop, replacement in sorted(replacements):
            parts.extend((source[cursor:start], replacement))
            cursor = stop
        parts.append(source[cursor:])
        template = "".join(parts)
        directory = dataset / "gen_assertions" / "setup" / "imported"
        directory.mkdir(parents=True, exist_ok=True)
        name = unit["name"]
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise EnvironmentSetupError(f"Unsafe unit name: {name!r}")
        template_path = directory / f"{name}.sv"
        manifest_path = directory / f"{name}.json"
        template_path.write_bytes(template.encode("utf-8"))
        unit.update({"input_mode": "existing_assertions", "assertion_files": [relative],
                     "assertion_slots": slots, "template": str(template_path.relative_to(dataset)),
                     "assertion_manifest": str(manifest_path.relative_to(dataset))})
        save(manifest_path, {"version": 1, "unit": name, "checker": unit["checker"],
                             "source_file": relative, "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
                             "assertion_count": len(slots), "slots": slots,
                             "template": unit["template"]})
    inventory = input_inventory(dataset, result)
    result["input_inventory"] = inventory
    save(dataset / "gen_assertions" / "setup" / "imported" / "input_inventory.json", inventory)
    if inventory["unassigned_files"]:
        details = "; ".join(row["source_file"] + " (" + ", ".join(row["unassigned_checkers"]) + ")"
                            for row in inventory["files"] if row["unassigned_checkers"])
        raise EnvironmentSetupError(
            "Unassigned assertion-bearing sources: " + details + ". "
            "Add matching bind/ files and unit/checker mappings in gen_assertions/setup/dataset.json; "
            "assertion files cannot be silently skipped or assigned an invented top/clock. "
            "Inspect gen_assertions/setup/imported/input_inventory.json."
        )
    return result


def load_existing(dataset, unit):
    """Return every original assertion as a stable, initially unqualified item."""
    if unit.get("input_mode") != "existing_assertions" or not unit.get("assertion_slots"):
        raise EnvironmentSetupError(f"{unit['name']}: existing assertions have not been imported")
    return [{"label": slot["label"], "repair_id": slot["slot_id"], "slot_id": slot["slot_id"],
             "expression": slot["expression"], "basis": "existing assertion",
             "source_file": slot["source_file"], "source_line": slot["source_line"],
             "assertion_kind": slot["assertion_kind"],
             "fixed_prefix": slot["prefix"], "fixed_suffix": slot["suffix"],
             "source_label": slot["label"]} for slot in unit["assertion_slots"]]


def render_slots(template, unit, properties, validate_body, render_added):
    """Fill imported slots, preserving all fixed source and unchanged checks.

    ``validate_body(expression, unit)`` runs only for changed or added bodies.
    ``render_added(property)`` renders optional additions at the module marker.
    Original bodies intentionally bypass local expression validation so actual
    syntax failures reach Jasper and can receive its diagnostics.
    """
    slots = unit.get("assertion_slots", [])
    slot_map = {slot["slot_id"]: slot for slot in slots}
    if len(slot_map) != len(slots):
        raise EnvironmentSetupError("Imported slot identifiers are not unique")
    if template.count(ADDITION_MARKER) != 1:
        raise EnvironmentSetupError("Imported template needs exactly one addition marker")
    selected = {}
    added = []
    labels = set()
    for prop in properties:
        if prop["label"] in labels:
            raise ValueError(f"Duplicate assertion label: {prop['label']}")
        labels.add(prop["label"])
        slot_id = prop.get("slot_id")
        if not slot_id:
            validate_body(prop["expression"], unit)
            added.append(prop)
            continue
        if slot_id not in slot_map:
            raise ValueError(f"Unknown imported assertion slot: {slot_id}")
        if slot_id in selected:
            raise ValueError(f"Duplicate imported assertion slot: {slot_id}")
        if prop["label"] != slot_map[slot_id]["label"]:
            raise ValueError(f"Imported assertion label is fixed: {slot_map[slot_id]['label']}")
        selected[slot_id] = prop
    rendered = template
    for slot in slots:
        marker = f"/* LOOP_SLOT {slot['slot_id']} */"
        if rendered.count(marker) != 1:
            raise EnvironmentSetupError(f"Missing or duplicate imported template marker: {slot['slot_id']}")
        prop = selected.get(slot["slot_id"])
        if prop is None:
            # A null statement is legal even under a single-statement if/else;
            # a comment alone would consume the else or next statement.
            newlines = "".join(re.findall(r"\r\n|\r|\n", slot["original_statement"]))
            replacement = ";" + newlines
        elif prop["expression"].strip() == slot["expression"].strip():
            replacement = slot["original_statement"]
            if not slot.get("source_named", True):
                replacement = f"{slot['label']}: " + replacement
        else:
            validate_body(prop["expression"], unit)
            replacement = slot["prefix"] + prop["expression"].strip() + slot["suffix"]
        rendered = rendered.replace(marker, replacement, 1)
    additions = "\n".join(render_added(prop) for prop in added)
    if additions:
        additions = "\n" + additions + "\n"
    return rendered.replace(ADDITION_MARKER, additions, 1)
