#!/usr/bin/env python3
"""Conservative, deterministic single-edit RTL mutant generator (prototype).

This is a lexical operator generator, not a complete SystemVerilog AST tool.
Generated candidates must still pass Jasper elaboration and an independent
functional-validity check before being used as the paper's fault pool.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from collect_jasper import resolve


def reference_category(kind: str, family: str) -> str:
    """Coarse AssertLLM2 taxonomy; this generator does not run its driver."""
    if family == "F2":
        return "Condition"
    if family == "F5":
        return "Communication/connectivity" if "rewire" in kind else "Data"
    if family == "F3" and "guard" in kind:
        return "Condition"
    return "Semantic/logic"


@dataclass(frozen=True)
class Candidate:
    family: str
    kind: str
    start: int
    end: int
    replacement: str
    site_start: int | None = None


def mask_noncode(source: str) -> str:
    """Replace comments and quoted text with spaces, preserving offsets."""
    chars = list(source)
    i = 0
    state = "code"
    while i < len(chars):
        c = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if state == "code" and c == "/" and nxt == "/":
            chars[i] = chars[i + 1] = " "
            i += 2
            state = "line"
            continue
        if state == "code" and c == "/" and nxt == "*":
            chars[i] = chars[i + 1] = " "
            i += 2
            state = "block"
            continue
        if state == "code" and c == '"':
            chars[i] = " "
            i += 1
            state = "string"
            continue
        if state == "line" and c == "\n":
            state = "code"
        elif state == "block" and c == "*" and nxt == "/":
            chars[i] = chars[i + 1] = " "
            i += 2
            state = "code"
            continue
        elif state == "string" and c == "\\" and nxt:
            chars[i] = chars[i + 1] = " "
            i += 2
            continue
        elif state == "string" and c == '"':
            chars[i] = " "
            state = "code"
            i += 1
            continue
        if state != "code" and c != "\n":
            chars[i] = " "
        i += 1
    return "".join(chars)


def closing_paren(text: str, opening: int) -> int | None:
    depth = 0
    for pos in range(opening, len(text)):
        if text[pos] == "(":
            depth += 1
        elif text[pos] == ")":
            depth -= 1
            if depth == 0:
                return pos
    return None


def generate(source: str) -> list[Candidate]:
    clean = mask_noncode(source)
    result: list[Candidate] = []

    # F1: several arithmetic/bitwise alternatives per state-update site.
    state_replacements = {
        "+": ("-", "^"), "-": ("+", "^"),
        "&": ("|", "^"), "|": ("&", "^"), "^": ("&", "|"),
    }
    for assignment in re.finditer(r"\b[A-Za-z_]\w*(?:\[[^\]\n]+\])?\s*<=\s*(?P<rhs>[^;]+);", clean):
        rhs_start = assignment.start("rhs")
        rhs = assignment.group("rhs")
        for operator in re.finditer(r"(?<![+])\+(?![+])|(?<![-])-(?![-])|(?<![&])&(?![&])|(?<![|])\|(?![|])|(?<![~^])\^(?![~^])", rhs):
            token = operator.group().strip()
            position = rhs_start + operator.start()
            for replacement in state_replacements[token]:
                result.append(Candidate("F1", "state_update_operator", position,
                                        position + len(token), replacement))

    # F2: swap sources, or incorrectly reuse one source on both branches.
    simple_mux = re.compile(r"\b(?P<cond>[A-Za-z_]\w*)\s*\?\s*"
                            r"(?P<yes>[A-Za-z_]\w*)\s*:\s*(?P<no>[A-Za-z_]\w*)\b")
    for match in simple_mux.finditer(clean):
        if match.group("yes") != match.group("no"):
            replacement = (f"{match.group('cond')} ? {match.group('no')} : "
                           f"{match.group('yes')}")
            result.append(Candidate("F2", "ternary_source_swap", match.start(), match.end(),
                                    replacement, match.start()))
            result.append(Candidate("F2", "ternary_true_source_reused", match.start("no"),
                                    match.end("no"), match.group("yes"), match.start()))
            result.append(Candidate("F2", "ternary_false_source_reused", match.start("yes"),
                                    match.end("yes"), match.group("no"), match.start()))

    # F3: one independent mutant for each plausible relation replacement.
    changes = {
        ">": (">=", "<", "<=", "=="),
        ">=": (">", "<", "<=", "=="),
        "<": ("<=", ">", ">=", "=="),
        "<=": ("<", ">", ">=", "=="),
        "==": ("!=", ">", "<"),
        "!=": ("==", ">=", "<="),
        "&&": ("||",),
        "||": ("&&",),
    }
    condition_token = re.compile(r"==|!=|&&|\|\||>=|<=|(?<![<>])>(?![=>])|(?<![<>])<(?![=<])")
    for match in re.finditer(r"\bif\s*\(", clean):
        opening = clean.find("(", match.start(), match.end())
        ending = closing_paren(clean, opening)
        if ending is None:
            continue
        found_operator = False
        for operator in condition_token.finditer(clean, opening + 1, ending):
            token = operator.group()
            for replacement in changes[token]:
                result.append(Candidate("F3", "if_condition_operator", operator.start(),
                                        operator.end(), replacement))
            found_operator = True
        if not found_operator:
            inner = clean[opening + 1:ending]
            simple = re.fullmatch(r"\s*(!?)\s*([A-Za-z_]\w*)\s*", inner)
            if simple:
                start = opening + 1 + simple.start(1 if simple.group(1) else 2)
                end = opening + 1 + simple.end(1 if simple.group(1) else 2)
                result.append(Candidate("F3", "if_guard_inversion", start, end,
                                        "" if simple.group(1) else "!" + simple.group(2)))

    # F4: only simple plain-case numeric labels. Avoid introducing duplicate
    # numeric values, even if some tools merely warn about duplicate items.
    case_block = re.compile(r"\bcase\s*\([^)]*\)(?P<body>.*?)\bendcase\b", re.DOTALL)
    label = re.compile(r"(?m)^[ \t]*(?P<value>(?:\d+'[sS]?[bBdDhHoO][0-9a-fA-F_xXzZ]+)|\d+)\s*:")
    for block in case_block.finditer(clean):
        body = block.group("body")
        # Numeric equality cannot be checked statically against parameter or
        # multi-item labels in this lexical implementation; skip such cases.
        if re.search(r"(?m)^[ \t]*(?!default\b)[A-Za-z_]\w*\s*:", body):
            continue
        if re.search(r"(?m)^[ \t]*(?:\d+'[sS]?[bBdDhHoO][0-9a-fA-F_xXzZ]+|\d+)\s*,", body):
            continue
        body_start = block.start("body")
        labels = list(label.finditer(body))
        for index, match in enumerate(labels):
            old = match.group("value")
            start = body_start + match.start("value")
            other_values = {parse_numeric_label(other.group("value"))
                            for other_index, other in enumerate(labels) if other_index != index}
            for replacement in change_numeric_labels(old):
                value = parse_numeric_label(replacement)
                if value is not None and value not in other_values:
                    result.append(Candidate("F4", "case_label_change", start, start + len(old), replacement))

    # F5: change a numeric bit/range boundary or rewire one simple assign.
    for match in re.finditer(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", clean):
        msb, lsb = int(match.group(1)), int(match.group(2))
        if msb > lsb:
            result.append(Candidate("F5", "packed_range_shrink", match.start(),
                                    match.end(), f"[{msb}:{lsb + 1}]"))
            result.append(Candidate("F5", "packed_range_expand", match.start(),
                                    match.end(), f"[{msb + 1}:{lsb}]"))
    for match in re.finditer(r"\[\s*(\d+)\s*\]", clean):
        index = int(match.group(1))
        result.append(Candidate("F5", "bit_index_shift", match.start(),
                                match.end(), f"[{index + 1}]"))
        if index > 0:
            result.append(Candidate("F5", "bit_index_shift", match.start(),
                                    match.end(), f"[{index - 1}]"))
    simple_assign = list(re.finditer(r"\bassign\s+[A-Za-z_]\w*\s*=\s*(?P<rhs>[A-Za-z_]\w*)\s*;", clean))
    for assignment in simple_assign:
        current = assignment.group("rhs")
        for alternative in sorted({other.group("rhs") for other in simple_assign} - {current}):
            result.append(Candidate("F5", "continuous_assign_rewire",
                                    assignment.start("rhs"), assignment.end("rhs"),
                                    alternative))

    return [c for c in result if source[c.start:c.end] != c.replacement]


def change_numeric_labels(value: str) -> list[str]:
    if "'" not in value:
        number = int(value)
        return [str(x) for x in (number + 1, number - 1) if x >= 0]
    prefix, digits = value.split("'", 1)
    base_index = next((i for i, ch in enumerate(digits) if ch.lower() in "bdho"), None)
    if base_index is None:
        return []
    base = digits[base_index].lower()
    content = digits[base_index + 1:]
    if base == "b":
        replacements = []
        for i in range(len(content)):
            if content[i] in "01":
                replacement = "1" if content[i] == "0" else "0"
                replacements.append(prefix + "'" + digits[:base_index + 1] + content[:i] + replacement + content[i + 1:])
        return replacements
    if base == "d" and content.isdigit():
        width = int(prefix) if prefix.isdigit() else None
        limit = 2**width if width is not None else None
        numbers = (int(content) + 1, int(content) - 1)
        return [prefix + "'" + digits[:base_index + 1] + str(number)
                for number in numbers if number >= 0 and (limit is None or number < limit)]
    return []


def parse_numeric_label(value: str) -> int | None:
    if "'" not in value:
        return int(value)
    _, digits = value.split("'", 1)
    base_index = next((i for i, ch in enumerate(digits) if ch.lower() in "bdho"), None)
    if base_index is None:
        return None
    base = digits[base_index].lower()
    content = digits[base_index + 1:].replace("_", "")
    if not content or any(ch.lower() in "xz?" for ch in content):
        return None
    try:
        return int(content, {"b": 2, "d": 10, "h": 16, "o": 8}[base])
    except ValueError:
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--target", required=True, help="one path from config.rtl_files")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=("ast", "lexical"), default="lexical",
                        help="AST uses pyslang; lexical is the dependency-free prototype")
    parser.add_argument("--max-per-family", type=int, default=None,
                        help="optional sampling cap; omitted means generate every distinct candidate")
    parser.add_argument("--seed", type=int, default=1)
    args = parser.parse_args()
    if args.max_per_family is not None and args.max_per_family < 1:
        raise ValueError("--max-per-family must be positive")
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    root = resolve(config.get("project_root", "."), config_path.parent)
    target = resolve(args.target, root)
    configured = {resolve(x, root) for x in config["rtl_files"]}
    if target not in configured or not target.is_file():
        raise ValueError("--target must be an existing member of config.rtl_files")
    assertions = config.get("assertions")
    if not isinstance(assertions, list) or not assertions or not all(isinstance(x, str) for x in assertions):
        raise ValueError("config.assertions must list the AQ property names")
    source = target.read_text(encoding="utf-8")
    if args.backend == "ast":
        from generate_mutants_ast import generate_ast
        candidates = generate_ast(
            source, source_path=target,
            include_dirs=[resolve(x, root) for x in config.get("include_dirs", [])],
            defines=config.get("defines", []),
        )
    else:
        candidates = generate(source)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    manifest = {"assertions": assertions, "generator_backend": args.backend,
                "operator_reference": "AssertLLM2-inspired; independent source-preserving rules, not upstream driver",
                "mutants": []}
    audit = []
    global_seen: set[str] = set()
    for family in ("F1", "F2", "F3", "F4", "F5"):
        choices = [c for c in candidates if c.family == family]
        if args.max_per_family is not None:
            rng.shuffle(choices)
        seen: set[str] = set()
        for candidate in choices:
            mutated = source[:candidate.start] + candidate.replacement + source[candidate.end:]
            digest = hashlib.sha256(mutated.encode()).hexdigest()
            if digest in seen or digest in global_seen:
                continue
            seen.add(digest)
            global_seen.add(digest)
            mutant_id = f"{family}_{len(seen):03d}"
            path = output / "mutants" / family / f"{mutant_id}_{target.name}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(mutated, encoding="utf-8")
            manifest["mutants"].append({
                "id": mutant_id, "family": family,
                "replace": str(target.relative_to(root)),
                "file": path.relative_to(output).as_posix(),
                "operator": candidate.kind,
                "reference_category": reference_category(candidate.kind, family),
                "site_id": f"{family}:{candidate.site_start if candidate.site_start is not None else candidate.start}",
                "line": source.count("\n", 0, candidate.start) + 1,
                "before": source[candidate.start:candidate.end],
                "after": candidate.replacement,
                "sha256": digest,
            })
            if args.max_per_family is not None and len(seen) >= args.max_per_family:
                break
        audit.append({"family": family, "candidates": len(choices), "generated": len(seen)})
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "generation_summary.json").write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
    print(" ".join(f"{x['family']}={x['generated']}" for x in audit))
    print(f"Wrote {output / 'manifest.json'}; validate before evaluation")


if __name__ == "__main__":
    main()
