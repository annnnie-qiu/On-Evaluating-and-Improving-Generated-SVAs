#!/usr/bin/env python3
"""Per-assertion-file SVA qualification pipeline for Cadence/Jasper.

The script deliberately does not hard-code a Jasper flow.  Every external
command, Tcl body, bind policy and result regex is supplied by the YAML/JSON
configuration file.  It uses only the Python standard library at runtime,
except that YAML configuration files require PyYAML.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from process_watch import run_logged


DEFAULT_ASSERTION_PATTERNS = ["**/*.sv", "**/*.svh", "**/*.sva", "**/*.v"]
STAGE_ORDER = ["syntax", "fpv", "vacuity"]
QUALIFIED_STATUSES = {
    "syntax": {"PASS"},
    "fpv": {"PROVEN"},
    # Immediate assertions have no temporal antecedent, so vacuity is N/A.
    # They remain qualified and continue into final/AQ.
    "vacuity": {"PASS", "NOT_APPLICABLE"},
}
REUSABLE_STATUSES = {
    # Every completed outcome is resumable. Use --force when a timeout, error,
    # or unresolved result should be attempted again.
    "syntax": {"PASS", "FAIL", "UNKNOWN", "TIMEOUT", "ERROR"},
    "fpv": {"PROVEN", "CEX", "UNKNOWN", "TIMEOUT", "ERROR"},
    "vacuity": {"PASS", "NOT_APPLICABLE", "VACUOUS", "UNKNOWN", "TIMEOUT", "ERROR"},
}


class AQError(RuntimeError):
    """Expected configuration or pipeline error."""


class SafeFormatDict(dict):
    """Give a helpful error when a command template has an unknown field."""

    def __missing__(self, key: str) -> str:
        raise AQError(f"Template references an unknown variable {{{key}}}")


@dataclasses.dataclass(frozen=True)
class AssertionBlock:
    assertion_id: str
    property_name: str
    named: bool
    start: int
    end: int
    start_line: int
    end_line: int
    text: str
    fingerprint: str
    assertion_kind: str


@dataclasses.dataclass(frozen=True)
class VacuityPlan:
    """How one assertion is evaluated for antecedent vacuity.

    ``witness`` means that a temporary ``antecedent |-> 1'b0`` assertion can
    be generated safely.  The other modes never modify the user's source:
    they are either explicitly not applicable or fail closed for review.
    """

    mode: str
    reason: str
    prefix: str = ""
    antecedent: str = ""


@dataclasses.dataclass
class AssertionUnit:
    path: Path
    rel: str
    unit_id: str
    module_guess: str
    source: str
    source_sha256: str
    assertions: list[AssertionBlock]
    detected_assertions: int
    parse_errors: list[str]
    top: str = ""
    module: str = ""


@dataclasses.dataclass
class RunResult:
    stage: str
    assertion_file: str
    unit_id: str
    module_guess: str
    assertion_id: str
    property_name: str
    named: bool
    start_line: int
    end_line: int
    source_sha256: str
    fingerprint: str
    stage_config_hash: str
    status: str
    reason: str
    exit_code: int | None
    duration_seconds: float
    command: str
    run_dir: str
    artifact: str
    log_file: str
    assertion_kind: str = "concurrent"
    vacuity_mode: str = ""
    resumed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AQError(f"Configuration file does not exist: {path}")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise AQError(
                "Reading YAML requires PyYAML. Run `python3 -m pip install pyyaml` "
                "or use a JSON configuration."
            ) from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise AQError("The configuration root must be a mapping/object")
    return data


def deep_get(data: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def resolve_path(value: str | Path, base: Path) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return p.resolve() if p.is_absolute() else (base / p).resolve()


def discover_aq_project_root(config_dir: Path, paths: Mapping[str, Any]) -> Path:
    """Find the nearest parent containing both RTL and assertion directories."""
    rtl_value = str(paths.get("rtl_dir", "rtl"))
    assertions_value = str(paths.get("assertions_dir", "assertions"))
    searched: list[str] = []
    for candidate in [config_dir.resolve(), *config_dir.resolve().parents]:
        rtl = resolve_path(rtl_value, candidate)
        assertions = resolve_path(assertions_value, candidate)
        searched.append(str(candidate))
        if rtl.is_dir() and assertions.is_dir():
            return candidate
    raise AQError(
        "project_root=auto could not find a parent containing both rtl/ and assertions/."
        "\nSearched: " + " | ".join(searched)
    )


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def hash_tree(root: Path) -> str:
    """Hash file paths and contents so cached formal results track RTL changes."""
    digest = hashlib.sha256()
    if not root.is_dir():
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def safe_slug(text: str, limit: int = 72) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._") or "unit"
    if len(slug) > limit:
        slug = slug[:limit]
    return slug


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def mask_comments_and_strings(source: str) -> tuple[str, list[str]]:
    """Replace comments/strings with spaces while retaining byte positions/newlines."""
    out = list(source)
    errors: list[str] = []
    i = 0
    state = "normal"
    start = 0
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ""
        if state == "normal":
            if ch == "/" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "line_comment"
                continue
            if ch == "/" and nxt == "*":
                start = i
                out[i] = out[i + 1] = " "
                i += 2
                state = "block_comment"
                continue
            if ch == '"':
                start = i
                out[i] = " "
                i += 1
                state = "string"
                continue
            i += 1
            continue
        if state == "line_comment":
            if ch == "\n":
                state = "normal"
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                state = "normal"
            else:
                if ch != "\n":
                    out[i] = " "
                i += 1
            continue
        if state == "string":
            if ch == "\\" and i + 1 < len(source):
                if source[i] != "\n":
                    out[i] = " "
                if source[i + 1] != "\n":
                    out[i + 1] = " "
                i += 2
            elif ch == '"':
                out[i] = " "
                i += 1
                state = "normal"
            else:
                if ch != "\n":
                    out[i] = " "
                i += 1
    if state == "block_comment":
        errors.append(f"Unterminated block comment starting at line {line_number(source, start)}")
    elif state == "string":
        errors.append(f"Unterminated string starting at line {line_number(source, start)}")
    return "".join(out), errors


def skip_space(masked: str, pos: int) -> int:
    while pos < len(masked) and masked[pos].isspace():
        pos += 1
    return pos


def matching_delimiter(masked: str, start: int, opener: str, closer: str) -> int | None:
    depth = 0
    for pos in range(start, len(masked)):
        ch = masked[pos]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return pos
    return None


def find_statement_end(masked: str, pos: int) -> tuple[int | None, str | None]:
    """Find the trailing semicolon of a common concurrent assertion statement.

    Parenthesized action blocks such as ``else $error(...);`` are supported.
    Procedural ``begin/end`` action blocks are rejected because a lightweight
    extractor cannot safely determine their full span in every legal SV form.
    """
    tail = masked[pos:]
    if re.match(r"\s*(?:else\s+)?begin\b", tail):
        return None, "The lightweight parser cannot safely split a begin/end assert action block"
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    for i in range(pos, len(masked)):
        ch = masked[i]
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack or stack[-1] != pairs[ch]:
                return None, f"Mismatched delimiter in assert action block near character {i}"
            stack.pop()
        elif ch == ";" and not stack:
            return i + 1, None
    return None, "No safe terminating semicolon was found for the assert statement"


def include_label_prefix(source: str, assert_start: int) -> tuple[int, str | None]:
    # Permit either ``label: assert property`` or a label on the preceding line.
    prefix = source[:assert_start]
    match = re.search(
        r"(?:^|[\n;])([ \t]*)([A-Za-z_$][A-Za-z0-9_$]*)\s*:\s*\Z", prefix
    )
    if match:
        return match.start(2), match.group(2)
    return assert_start, None


def detected_assertion_count(source: str) -> int:
    """Count supported concurrent and immediate assertion statements."""
    masked, _ = mask_comments_and_strings(source)
    return len(
        re.findall(
            r"\bassert\s*(?:property\b|(?:#\s*0\s*)?\()",
            masked,
            re.IGNORECASE,
        )
    )


def extract_assertions(source: str) -> tuple[list[AssertionBlock], list[str]]:
    """Extract concurrent ``assert property`` and immediate ``assert (...)``.

    The function is intentionally fail-closed.  Any unsafe construct produces
    an error; the caller then sends the entire file to manual_review.
    """
    masked, errors = mask_comments_and_strings(source)
    token_re = re.compile(r"\bassert\b", re.IGNORECASE)
    blocks: list[AssertionBlock] = []
    occupied_until = -1
    ordinal = 0
    for match in token_re.finditer(masked):
        if match.start() < occupied_until:
            continue
        pos = skip_space(masked, match.end())
        property_match = re.match(r"property\b", masked[pos:], re.IGNORECASE)
        if property_match:
            assertion_kind = "concurrent"
            pos = skip_space(masked, pos + property_match.end())
        else:
            assertion_kind = "immediate"
            delay_match = re.match(r"#\s*0\s*", masked[pos:])
            if delay_match:
                pos = skip_space(masked, pos + delay_match.end())
        ordinal += 1
        open_pos = pos
        if open_pos >= len(masked) or masked[open_pos] != "(":
            errors.append(
                f"The assert at line {line_number(source, match.start())} uses an unsupported or unsafe form"
            )
            continue
        close_pos = matching_delimiter(masked, open_pos, "(", ")")
        if close_pos is None:
            errors.append(f"The assert at line {line_number(source, match.start())} has an unclosed delimiter")
            continue
        end, end_error = find_statement_end(masked, close_pos + 1)
        if end is None:
            errors.append(f"Line {line_number(source, match.start())}: {end_error}")
            continue
        start, label = include_label_prefix(source, match.start())
        occupied_until = end
        prop_name = label or f"AQ_UNNAMED_{ordinal:04d}"
        assertion_id = f"A{ordinal:04d}_{safe_slug(prop_name, 40)}"
        block_text = source[start:end]
        blocks.append(
            AssertionBlock(
                assertion_id=assertion_id,
                property_name=prop_name,
                named=label is not None,
                start=start,
                end=end,
                start_line=line_number(source, start),
                end_line=line_number(source, max(start, end - 1)),
                text=block_text,
                fingerprint=hashlib.sha256(block_text.encode()).hexdigest(),
                assertion_kind=assertion_kind,
            )
        )
    return blocks, errors


def render_subset(
    source: str,
    assertions: Sequence[AssertionBlock],
    keep_ids: set[str],
    label_unnamed: bool = False,
) -> str:
    """Return the original file with non-selected target assertions neutralized."""
    parts: list[str] = []
    cursor = 0
    for block in assertions:
        parts.append(source[cursor:block.start])
        if block.assertion_id in keep_ids:
            block_text = block.text
            if label_unnamed and not block.named:
                leading = re.match(r"[ \t]*", block_text).group(0)  # type: ignore[union-attr]
                block_text = (
                    leading
                    + block.property_name
                    + ": "
                    + block_text[len(leading):]
                )
            parts.append(block_text)
        else:
            newline_count = block.text.count("\n")
            parts.append(
                f"/* AQ removed {block.assertion_id} */" + ("\n" * newline_count)
            )
        cursor = block.end
    parts.append(source[cursor:])
    return "".join(parts)


def _strip_balanced_outer_parentheses(text: str) -> str:
    """Remove only parentheses that enclose the complete expression."""
    value = text.strip()
    while value.startswith("("):
        masked, errors = mask_comments_and_strings(value)
        if errors:
            break
        close = matching_delimiter(masked, 0, "(", ")")
        if close is None or masked[close + 1 :].strip():
            break
        value = value[1:close].strip()
    return value


def _top_level_implications(text: str) -> tuple[list[tuple[int, str]], bool]:
    """Return top-level SVA implications and whether any implication exists."""
    masked, _ = mask_comments_and_strings(text)
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    found: list[tuple[int, str]] = []
    any_implication = False
    i = 0
    while i < len(masked):
        token = masked[i : i + 3]
        if token in {"|->", "|=>"}:
            any_implication = True
            if not stack:
                found.append((i, token))
            i += 3
            continue
        ch = masked[i]
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
        i += 1
    return found, any_implication


def plan_vacuity(block: AssertionBlock) -> VacuityPlan:
    """Classify an assertion using a reproducible antecedent-vacuity policy.

    Immediate assertions and concurrent properties without an implication do
    not have an implication antecedent, so this metric is N/A.  For a safely
    parsed top-level ``|->``/``|=>``, Jasper proves an automatically generated
    reachability witness.  Ambiguous constructs fail closed as manual review.
    """
    if block.assertion_kind == "immediate":
        return VacuityPlan("not_applicable_immediate", "immediate assertion has no temporal antecedent")

    masked, errors = mask_comments_and_strings(block.text)
    if errors:
        return VacuityPlan("manual_review", "comment/string parsing failed")
    match = re.search(r"\bassert\s+property\s*\(", masked, re.IGNORECASE)
    if not match:
        return VacuityPlan("manual_review", "cannot locate assert property body")
    open_pos = masked.find("(", match.start())
    close_pos = matching_delimiter(masked, open_pos, "(", ")")
    if close_pos is None:
        return VacuityPlan("manual_review", "assert property parentheses are not balanced")

    body = block.text[open_pos + 1 : close_pos]
    body_masked, body_errors = mask_comments_and_strings(body)
    if body_errors:
        return VacuityPlan("manual_review", "property body parsing failed")
    pos = skip_space(body_masked, 0)

    # Preserve an explicit clock event exactly as written.
    if pos < len(body_masked) and body_masked[pos] == "@":
        pos += 1
        pos = skip_space(body_masked, pos)
        if pos < len(body_masked) and body_masked[pos] == "(":
            event_end = matching_delimiter(body_masked, pos, "(", ")")
            if event_end is None:
                return VacuityPlan("manual_review", "clocking event is not balanced")
            pos = event_end + 1
        else:
            event = re.match(r"(?:[A-Za-z_$][A-Za-z0-9_$.:]*|\*)", body_masked[pos:])
            if not event:
                return VacuityPlan("manual_review", "unsupported clocking event")
            pos += event.end()
        pos = skip_space(body_masked, pos)

    # Preserve disable iff, including its condition and whitespace.
    disable = re.match(r"disable\s+iff\s*", body_masked[pos:], re.IGNORECASE)
    if disable:
        pos += disable.end()
        pos = skip_space(body_masked, pos)
        if pos >= len(body_masked) or body_masked[pos] != "(":
            return VacuityPlan("manual_review", "disable iff condition is not parenthesized")
        disable_end = matching_delimiter(body_masked, pos, "(", ")")
        if disable_end is None:
            return VacuityPlan("manual_review", "disable iff condition is not balanced")
        pos = skip_space(body_masked, disable_end + 1)

    prefix = body[:pos]
    expression = _strip_balanced_outer_parentheses(body[pos:])
    implications, any_implication = _top_level_implications(expression)
    if not implications:
        if any_implication:
            return VacuityPlan(
                "manual_review",
                "implication exists only inside an unsupported nested property",
            )
        return VacuityPlan(
            "not_applicable_no_implication",
            "concurrent property has no top-level implication antecedent",
        )
    if len(implications) != 1:
        return VacuityPlan("manual_review", "multiple top-level implications are ambiguous")
    offset, _operator = implications[0]
    antecedent = expression[:offset].strip()
    consequent = expression[offset + 3 :].strip()
    if not antecedent or not consequent:
        return VacuityPlan("manual_review", "empty implication antecedent or consequent")
    return VacuityPlan("witness", "safe top-level implication", prefix, antecedent)


def render_vacuity_witness(
    source: str,
    assertions: Sequence[AssertionBlock],
    target: AssertionBlock,
    plan: VacuityPlan,
) -> str:
    """Replace one target with an automatic antecedent-reachability witness."""
    if plan.mode != "witness":
        return render_subset(source, assertions, {target.assertion_id}, label_unnamed=True)
    leading = re.match(r"[ \t]*", target.text).group(0)  # type: ignore[union-attr]
    witness_name = f"AQ_ANT_REACH_{safe_slug(target.assertion_id, 48)}"
    prefix = plan.prefix
    if prefix and not prefix[-1].isspace():
        prefix += " "
    witness = (
        f"{leading}{witness_name}: assert property (\n"
        f"{leading}  {prefix}({plan.antecedent}) |-> 1'b0\n"
        f"{leading});"
    )
    # Preserve line count where practical so diagnostics still point near the
    # original assertion; extra lines are harmless inside the isolated file.
    parts: list[str] = []
    cursor = 0
    for block in assertions:
        parts.append(source[cursor:block.start])
        if block.assertion_id == target.assertion_id:
            parts.append(witness)
        else:
            parts.append(f"/* AQ removed {block.assertion_id} */" + "\n" * block.text.count("\n"))
        cursor = block.end
    parts.append(source[cursor:])
    return "".join(parts)


def module_guess(source: str) -> str:
    masked, _ = mask_comments_and_strings(source)
    found = re.search(r"\bmodule\s+([A-Za-z_$][A-Za-z0-9_$]*)", masked)
    return found.group(1) if found else ""


def infer_dut_top(assertion_module: str, suffixes: Sequence[str]) -> str:
    """Infer a DUT top from a conventional assertion-wrapper module name."""
    for suffix in suffixes:
        if suffix and assertion_module.endswith(suffix):
            candidate = assertion_module[: -len(suffix)]
            if candidate:
                return candidate
    return assertion_module


def tcl_word(value: str | Path) -> str:
    """Quote one Tcl list word without relying on shell quoting rules."""
    text = str(value).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return "{" + text + "}"


def infer_clock_and_reset(source: str) -> tuple[str, str, bool]:
    """Best-effort clock/reset inference from explicit SVA clocking and disable iff.

    The returned boolean is false when multiple distinct clocks are present.  In
    that case the configuration must provide a per-file clock explicitly.
    """
    masked, _ = mask_comments_and_strings(source)
    clocks = {
        match.group(2)
        for match in re.finditer(
            r"@\s*\(\s*(posedge|negedge)\s+([A-Za-z_$][A-Za-z0-9_$.]*)",
            masked,
        )
    }
    resets = [
        source[match.start(1) : match.end(1)].strip()
        for match in re.finditer(r"\bdisable\s+iff\s*\(([^()]*)\)", masked, re.DOTALL)
    ]
    clock = next(iter(clocks)) if len(clocks) == 1 else ""
    reset = resets[0] if resets and all(item == resets[0] for item in resets) else ""
    return clock, reset, len(clocks) <= 1


class Pipeline:
    def __init__(self, config_path: Path, args: argparse.Namespace):
        self.config_path = config_path.resolve()
        self.config = load_config(self.config_path)
        self.config_dir = self.config_path.parent
        paths = self.config.get("paths", {})
        if not isinstance(paths, Mapping):
            raise AQError("paths must be a mapping")
        project_value = args.project_root or self.config.get("project_root", ".")
        if args.project_root:
            project_path = Path(os.path.expandvars(os.path.expanduser(str(args.project_root))))
            self.project_root = project_path.resolve()
        elif str(project_value).strip().lower() == "auto":
            self.project_root = discover_aq_project_root(self.config_dir, paths)
        else:
            self.project_root = resolve_path(project_value, self.config_dir)
        self.input_variant = "buggy" if args.buggy else "normal"
        assertions_value = (
            paths.get("buggy_assertions_dir", "buggy_assertions")
            if args.buggy
            else paths.get("assertions_dir", "assertions")
        )
        output_value = (
            paths.get("buggy_output_dir", "aq_buggy_out")
            if args.buggy
            else paths.get("output_dir", "aq_out")
        )
        self.assertions_dir = resolve_path(assertions_value, self.project_root)
        self.rtl_dir = resolve_path(paths.get("rtl_dir", "rtl"), self.project_root)
        self.bind_dir = resolve_path(paths.get("bind_dir", "bind"), self.project_root)
        self.output_root = resolve_path(output_value, self.project_root)
        self.manifest_dir = self.output_root / "manifests"
        self.args = args
        self.dry_run = bool(args.dry_run)
        # Idempotent by default. Existing results are reused only when the
        # source fingerprint and stage configuration still match. --force is
        # the explicit way to rerun everything.
        self.force = bool(args.force)
        self.workers = int(args.workers or self.config.get("workers", 1))
        self.timeout = float(args.timeout or self.config.get("timeout_seconds", 1800))
        self.include = list(args.include or self.config.get("include", DEFAULT_ASSERTION_PATTERNS))
        self.exclude = list(args.exclude or self.config.get("exclude", []))
        self.units: list[AssertionUnit] = []
        self.results: dict[str, list[RunResult]] = {}
        # None means missing/corrupt. Keeping the cache also prevents the same
        # warning from being printed once per assertion file during summary.
        self._manifest_rows: dict[str, list[dict[str, Any]] | None] = {}
        if self.workers < 1:
            raise AQError("workers must be at least 1")
        self.manifest_dir.mkdir(parents=True, exist_ok=True)

    def relative_to_project(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return str(path.resolve())

    def discover_files(self) -> list[Path]:
        if not self.assertions_dir.is_dir():
            raise AQError(f"assertions_dir does not exist: {self.assertions_dir}")
        selected: set[Path] = set()
        for pattern in self.include:
            selected.update(p for p in self.assertions_dir.glob(pattern) if p.is_file())
        output_resolved = self.output_root.resolve()
        files = []
        for path in sorted(selected):
            try:
                path.resolve().relative_to(output_resolved)
                continue
            except ValueError:
                pass
            rel = path.relative_to(self.assertions_dir).as_posix()
            if any(fnmatch.fnmatch(rel, pat) for pat in self.exclude):
                continue
            files.append(path)
        return files

    def unit_config(self, rel: str) -> Mapping[str, Any]:
        units_cfg = self.config.get("units", {})
        mapping = units_cfg.get("mapping", {}) if isinstance(units_cfg, Mapping) else {}
        if rel in mapping and isinstance(mapping[rel], Mapping):
            return mapping[rel]
        for pattern, value in mapping.items():
            if fnmatch.fnmatch(rel, str(pattern)) and isinstance(value, Mapping):
                return value
        return {}

    def inventory(self) -> list[AssertionUnit]:
        units: list[AssertionUnit] = []
        manual_dir = self.output_root / "rejected" / "manual_review"
        discovered_files = self.discover_files()
        if not discovered_files:
            raise AQError(
                "Inventory did not find any assertion files."
                f"\n  assertions_dir: {self.assertions_dir}"
                f"\n  include: {self.include}"
                f"\n  exclude: {self.exclude}"
                "\nCheck project_root, paths.assertions_dir, file extensions, and globs. Linux is case-sensitive."
            )
        for path in discovered_files:
            rel = path.relative_to(self.assertions_dir).as_posix()
            source = path.read_text(encoding="utf-8", errors="replace")
            blocks, errors = extract_assertions(source)
            guess = module_guess(source)
            unit_cfg = self.unit_config(rel)
            default_top = deep_get(self.config, "units.default_top", "")
            default_module = deep_get(self.config, "units.default_module", "")
            suffixes = deep_get(
                self.config,
                "units.infer_top_suffixes",
                ["_assertions", "_assertion", "_sva", "_properties"],
            )
            if not isinstance(suffixes, list):
                suffixes = [str(suffixes)]
            inferred_top = infer_dut_top(guess, [str(item) for item in suffixes])
            suffix = hashlib.sha1(rel.encode()).hexdigest()[:8]
            unit_id = f"{safe_slug(Path(rel).with_suffix('').as_posix())}__{suffix}"
            unit = AssertionUnit(
                path=path.resolve(),
                rel=rel,
                unit_id=unit_id,
                module_guess=guess,
                source=source,
                source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                assertions=blocks,
                detected_assertions=detected_assertion_count(source),
                parse_errors=errors,
                top=str(unit_cfg.get("top", default_top or inferred_top)),
                module=str(unit_cfg.get("module", default_module or guess)),
            )
            units.append(unit)
            if errors:
                target = manual_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                (target.parent / f"{target.name}.reason.txt").write_text(
                    "\n".join(errors) + "\n", encoding="utf-8"
                )
        self.units = units
        rows: list[dict[str, Any]] = []
        assertion_rows: list[dict[str, Any]] = []
        for unit in units:
            rows.append(
                {
                    "assertion_file": unit.rel,
                    "absolute_path": str(unit.path),
                    "unit_id": unit.unit_id,
                    "module_guess": unit.module_guess,
                    "top": unit.top,
                    "module": unit.module,
                    "original_assertions": unit.detected_assertions,
                    "safely_extracted_assertions": len(unit.assertions),
                    "concurrent_assertions": sum(
                        a.assertion_kind == "concurrent" for a in unit.assertions
                    ),
                    "immediate_assertions": sum(
                        a.assertion_kind == "immediate" for a in unit.assertions
                    ),
                    "parse_status": "MANUAL_REVIEW" if unit.parse_errors else "OK",
                    "parse_errors": " | ".join(unit.parse_errors),
                    "source_sha256": unit.source_sha256,
                }
            )
            for block in unit.assertions:
                vacuity_plan = plan_vacuity(block)
                assertion_rows.append(
                    {
                        "assertion_file": unit.rel,
                        "unit_id": unit.unit_id,
                        "module_guess": unit.module_guess,
                        "assertion_id": block.assertion_id,
                        "property_name": block.property_name,
                        "assertion_kind": block.assertion_kind,
                        "vacuity_mode": vacuity_plan.mode,
                        "vacuity_reason": vacuity_plan.reason,
                        "named": block.named,
                        "start_line": block.start_line,
                        "end_line": block.end_line,
                        "fingerprint": block.fingerprint,
                        "source_sha256": unit.source_sha256,
                    }
                )
        write_csv_json(self.manifest_dir / "inventory_files", rows)
        write_csv_json(self.manifest_dir / "inventory_assertions", assertion_rows)
        self.write_summary()
        detected_total = sum(unit.detected_assertions for unit in units)
        extracted_total = sum(len(unit.assertions) for unit in units if not unit.parse_errors)
        manual_total = sum(bool(unit.parse_errors) for unit in units)
        for unit in units:
            status = "MANUAL_REVIEW" if unit.parse_errors else "OK"
            safe_count = 0 if unit.parse_errors else len(unit.assertions)
            print(
                "  Inventory file: "
                f"{unit.path} | detected={unit.detected_assertions} | "
                f"safe={safe_count} | {status}"
            )
        print(
            "Inventory: "
            f"files={len(units)}, detected_assertions={detected_total}, "
            f"safe_candidates={extracted_total}, manual_review_files={manual_total}"
        )
        if detected_total == 0:
            eprint(
                "AQ WARNING: Assertion files were found, but no `assert property` was detected. "
                "Check whether the source uses macros, immediate assertions, or another form."
            )
        elif extracted_total == 0:
            eprint(
                "AQ WARNING: No assertion can be run automatically. Inspect "
                f"{manual_dir} and parse_errors in inventory_files.csv."
            )
        return units

    def ensure_inventory(self) -> None:
        if not self.units:
            self.inventory()

    def stage_config(self, stage: str) -> Mapping[str, Any]:
        cfg = deep_get(self.config, f"stages.{stage}", {})
        if not isinstance(cfg, Mapping):
            raise AQError(f"stages.{stage} must be a mapping")
        return cfg

    def read_manifest_rows(
        self, stage: str, *, required: bool = False
    ) -> list[dict[str, Any]] | None:
        if stage in self._manifest_rows:
            rows = self._manifest_rows[stage]
        else:
            path = self.manifest_dir / f"{stage}.json"
            if not path.is_file():
                rows = None
            else:
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(raw, list):
                        raise ValueError("top-level JSON is not an array")
                    rows = [dict(row) for row in raw if isinstance(row, Mapping)]
                except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
                    rows = None
                    eprint(
                        f"AQ WARNING: Ignoring corrupt manifest {path}: {exc}. "
                        "The current stage will run again."
                    )
            self._manifest_rows[stage] = rows
        if rows is None and required:
            path = self.manifest_dir / f"{stage}.json"
            raise AQError(
                f"Missing or corrupt {stage} manifest: {path}. Run the {stage} stage first."
            )
        return rows

    def load_prior_stage(self, stage: str) -> list[RunResult]:
        if stage in self.results:
            return self.results[stage]
        rows = self.read_manifest_rows(stage, required=True)
        assert rows is not None
        known = {f.name for f in dataclasses.fields(RunResult)}
        results = [RunResult(**{k: v for k, v in row.items() if k in known}) for row in rows]
        self.results[stage] = results
        return results

    def candidate_ids(self, stage: str) -> dict[str, set[str]]:
        if stage == "syntax":
            return {
                unit.rel: {a.assertion_id for a in unit.assertions}
                for unit in self.units
                if not unit.parse_errors
            }
        previous = "syntax" if stage == "fpv" else "fpv"
        pass_statuses = QUALIFIED_STATUSES[previous]
        candidates: dict[str, set[str]] = {}
        for result in self.load_prior_stage(previous):
            if result.status in pass_statuses:
                candidates.setdefault(result.assertion_file, set()).add(result.assertion_id)
        return candidates

    def resume_map(self, stage: str, stage_hash: str) -> dict[tuple[str, str], RunResult]:
        if self.force:
            return {}
        rows = self.read_manifest_rows(stage)
        if rows is None:
            return {}
        known = {f.name for f in dataclasses.fields(RunResult)}
        result: dict[tuple[str, str], RunResult] = {}
        for row in rows:
            try:
                item = RunResult(**{k: v for k, v in row.items() if k in known})
            except TypeError:
                continue
            current_hash = item.stage_config_hash == stage_hash
            legacy_hash = not str(item.stage_config_hash).startswith("content-v2:")
            if (current_hash or legacy_hash) and item.status in REUSABLE_STATUSES[stage]:
                # Trust and migrate one pre-v2 manifest without rerunning
                # Jasper. Future semantic changes are checked by the v2 hash.
                if legacy_hash:
                    item.stage_config_hash = stage_hash
                result[(item.assertion_file, item.assertion_id)] = item
        return result

    def run_stage(self, stage: str) -> list[RunResult]:
        if stage not in STAGE_ORDER:
            raise AQError(f"Unknown stage: {stage}")
        self.ensure_inventory()
        cfg = self.stage_config(stage)
        if not cfg.get("command") and not self.dry_run:
            raise AQError(f"stages.{stage}.command is empty. Configure Jasper/a custom command or use --dry-run.")
        hash_config = dict(cfg)
        template_file = hash_config.pop("tcl_template_file", None)
        template_sha256 = ""
        if template_file:
            template_path = resolve_path(str(template_file), self.config_dir)
            template_sha256 = hashlib.sha256(template_path.read_bytes()).hexdigest()
        stage_hash_input: Any = {
            "stage": stage,
            "config": hash_config,
            "template_sha256": template_sha256,
            "bind": self.config.get("bind", {}),
            "rtl": self.config.get("rtl", {}),
            "formal": self.config.get("formal", {}),
            "units": self.config.get("units", {}),
            "jasper": self.config.get("jasper", {}),
            "rtl_sha256": hash_tree(self.rtl_dir),
        }
        if stage == "vacuity":
            # Prevent --resume from reusing results produced by the obsolete
            # guessed get_property_info vacuity attributes.
            stage_hash_input["algorithm"] = "antecedent_reachability_witness_v1"
        stage_hash = "content-v2:" + stable_hash(stage_hash_input)
        resume = self.resume_map(stage, stage_hash)
        candidate_ids = self.candidate_ids(stage)
        jobs: list[tuple[AssertionUnit, AssertionBlock]] = []
        reused: list[RunResult] = []
        for unit in self.units:
            block_map = {a.assertion_id: a for a in unit.assertions}
            for assertion_id in sorted(candidate_ids.get(unit.rel, set())):
                block = block_map.get(assertion_id)
                if block is None:
                    continue
                old = resume.get((unit.rel, assertion_id))
                if old and old.source_sha256 == unit.source_sha256 and old.fingerprint == block.fingerprint:
                    old.resumed = True
                    reused.append(old)
                else:
                    jobs.append((unit, block))

        candidate_total = len(jobs) + len(reused)
        print(
            f"Stage {stage}: candidates={candidate_total}, "
            f"to_run={len(jobs)}, resumed={len(reused)}, dry_run={self.dry_run}"
        )
        if candidate_total == 0:
            eprint(
                f"AQ WARNING: The {stage} stage has no assertion candidates. "
                "Inspect inventory_files.csv, inventory_assertions.csv, and PASS states in the previous manifest."
            )

        new_results: list[RunResult] = []
        if self.workers == 1:
            for job_index, (unit, block) in enumerate(jobs, start=1):
                print(
                    f"[PROGRESS] stage={stage} job={job_index}/{len(jobs)} "
                    f"file={unit.rel} assertion={block.assertion_id}",
                    flush=True,
                )
                result = self.run_one(
                    stage, cfg, stage_hash, unit, block, job_index, len(jobs)
                )
                new_results.append(result)
                print(
                    f"[PROGRESS] stage={stage} completed={job_index}/{len(jobs)} "
                    f"status={result.status}",
                    flush=True,
                )
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [
                    pool.submit(
                        self.run_one, stage, cfg, stage_hash, unit, block,
                        job_index, len(jobs),
                    )
                    for job_index, (unit, block) in enumerate(jobs, start=1)
                ]
                for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                    result = future.result()
                    new_results.append(result)
                    print(
                        f"[PROGRESS] stage={stage} completed={completed}/{len(jobs)} "
                        f"status={result.status} file={result.assertion_file}",
                        flush=True,
                    )
        results = sorted(
            reused + new_results,
            key=lambda r: (r.assertion_file, r.assertion_id),
        )
        self.results[stage] = results
        result_rows = [r.as_dict() for r in results]
        write_csv_json(self.manifest_dir / stage, result_rows)
        self._manifest_rows[stage] = result_rows
        self.materialize_stage_outputs(stage, results)
        self.write_summary()
        return results

    def expand_project_paths(self, values: Any) -> list[Path]:
        """Expand ordered relative paths or globs against the project root."""
        if values in (None, ""):
            return []
        specs = values if isinstance(values, list) else [values]
        expanded: list[Path] = []
        for raw in specs:
            spec = str(raw)
            path = Path(os.path.expandvars(os.path.expanduser(spec)))
            if path.is_absolute():
                matches = sorted(path.parent.glob(path.name)) if any(c in path.name for c in "*?[") else [path]
            elif any(c in spec for c in "*?["):
                matches = sorted(self.project_root.glob(spec))
            else:
                matches = [self.project_root / path]
            for match in matches:
                resolved = match.resolve()
                if resolved.is_file() and resolved not in expanded:
                    expanded.append(resolved)
        return expanded

    def rtl_inputs(self, unit: AssertionUnit) -> tuple[str, list[Path], list[Path], list[str]]:
        """Resolve the filelist, ordered RTL files, include directories, and defines."""
        unit_cfg = self.unit_config(unit.rel)
        filelist_value = unit_cfg.get("rtl_filelist", deep_get(self.config, "rtl.filelist", ""))
        filelist = str(resolve_path(filelist_value, self.project_root)) if filelist_value else ""

        file_specs = unit_cfg.get("rtl_files", deep_get(self.config, "rtl.files", []))
        rtl_files = self.expand_project_paths(file_specs)
        auto_discover = bool(deep_get(self.config, "rtl.auto_discover", True))
        if not filelist and not rtl_files and auto_discover:
            extensions = deep_get(
                self.config,
                "rtl.extensions",
                [".svh", ".vh", ".sv", ".v"],
            )
            allowed = {str(item).lower() for item in extensions}
            candidates = [
                path.resolve()
                for path in self.rtl_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in allowed
            ]
            # Package/header files are analyzed first as a portable best effort.
            rtl_files = sorted(
                candidates,
                key=lambda path: (
                    0 if path.suffix.lower() in {".svh", ".vh"} else 1,
                    0 if path.stem.endswith("_pkg") else 1,
                    path.as_posix(),
                ),
            )
        if not filelist and not rtl_files:
            raise AQError(
                f"No RTL input was resolved for {unit.rel}. Configure rtl.filelist, "
                "rtl.files, or enable rtl.auto_discover."
            )

        include_values = unit_cfg.get(
            "include_dirs",
            deep_get(self.config, "rtl.include_dirs", [str(self.rtl_dir)]),
        )
        include_specs = include_values if isinstance(include_values, list) else [include_values]
        include_dirs = [resolve_path(value, self.project_root) for value in include_specs]
        defines_value = unit_cfg.get("defines", deep_get(self.config, "rtl.defines", []))
        defines = [str(item) for item in (defines_value if isinstance(defines_value, list) else [defines_value]) if str(item)]
        return filelist, rtl_files, include_dirs, defines

    def formal_environment(self, unit: AssertionUnit) -> tuple[str, str, str]:
        """Return clock mode, clock signal, and reset expression for one file."""
        unit_cfg = self.unit_config(unit.rel)
        inferred_clock, inferred_reset, unambiguous = infer_clock_and_reset(unit.source)
        mode = str(unit_cfg.get("clock_mode", deep_get(self.config, "formal.clock_mode", "auto"))).lower()
        clock = str(unit_cfg.get("clock", deep_get(self.config, "formal.clock", "")) or inferred_clock)
        reset = str(
            unit_cfg.get(
                "reset_expression",
                deep_get(self.config, "formal.reset_expression", ""),
            )
            or inferred_reset
        )
        if mode == "auto":
            if not unambiguous:
                raise AQError(
                    f"Multiple clock signals were found in {unit.rel}; set units.mapping.{unit.rel}.clock."
                )
            mode = "clocked" if clock else "none"
        if mode not in {"clocked", "none"}:
            raise AQError(f"Unsupported clock_mode={mode!r} for {unit.rel}")
        if mode == "clocked" and not clock:
            raise AQError(f"clock_mode=clocked requires a clock signal for {unit.rel}")
        return mode, clock, reset

    def variables(
        self,
        stage: str,
        unit: AssertionUnit,
        block: AssertionBlock,
        run_dir: Path,
        assertion_file: Path,
        tcl_file: Path,
        log_file: Path,
        bind_files: Sequence[Path],
    ) -> dict[str, str]:
        rtl_filelist, rtl_files, include_dirs, defines = self.rtl_inputs(unit)
        clock_mode, clock_signal, reset_expression = self.formal_environment(unit)
        jasper_executable = str(deep_get(self.config, "jasper.executable", "jg"))
        vacuity_plan = plan_vacuity(block)
        values = {
            "stage": stage,
            "project_root": str(self.project_root),
            "output_root": str(self.output_root),
            "run_dir": str(run_dir),
            "assertion_file": str(assertion_file),
            "original_assertion_file": str(unit.path),
            "assertion_rel": unit.rel,
            "assertion_id": block.assertion_id,
            "property_name": block.property_name,
            "assertion_kind": block.assertion_kind,
            "vacuity_mode": vacuity_plan.mode,
            "unit_id": unit.unit_id,
            "rtl_dir": str(self.rtl_dir),
            "rtl_filelist": rtl_filelist,
            "rtl_files_tcl": " ".join(tcl_word(path) for path in rtl_files),
            "include_dirs_tcl": " ".join(tcl_word(path) for path in include_dirs),
            "defines_tcl": " ".join(tcl_word(value) for value in defines),
            "bind_dir": str(run_dir / "bind"),
            "bind_files": " ".join(str(p) for p in bind_files),
            "bind_files_tcl": " ".join(tcl_word(path) for path in bind_files),
            "top": unit.top,
            "module": unit.module,
            "clock_mode": clock_mode,
            "clock_signal": clock_signal,
            "reset_expression": reset_expression,
            "tcl_file": str(tcl_file),
            "log_file": str(log_file),
            "jasper_executable": jasper_executable,
        }
        for key in (
            "stage",
            "project_root",
            "assertion_file",
            "top",
            "property_name",
            "assertion_kind",
            "vacuity_mode",
            "rtl_filelist",
            "clock_mode",
            "clock_signal",
            "reset_expression",
        ):
            values[f"{key}_tcl"] = tcl_word(values[key])
        values.update({f"q_{key}": shlex.quote(value) for key, value in list(values.items())})
        return values

    def render_double_braces(self, template: str, values: Mapping[str, str]) -> str:
        pattern = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
        unknown = sorted({m.group(1) for m in pattern.finditer(template)} - set(values))
        if unknown:
            raise AQError(f"Tcl/bind template references unknown variables: {', '.join(unknown)}")
        return pattern.sub(lambda m: values[m.group(1)], template)

    def load_text_template(self, cfg: Mapping[str, Any], inline_key: str, file_key: str) -> str:
        if cfg.get(file_key):
            path = resolve_path(str(cfg[file_key]), self.config_dir)
            return path.read_text(encoding="utf-8")
        return str(cfg.get(inline_key, ""))

    def select_bind_sources(self, unit: AssertionUnit) -> list[Path]:
        bind_cfg = self.config.get("bind", {})
        if not isinstance(bind_cfg, Mapping):
            return []
        mode = str(bind_cfg.get("mode", "existing_auto"))
        unit_cfg = self.unit_config(unit.rel)
        explicit = unit_cfg.get("bind_files", [])
        if explicit:
            return [resolve_path(p, self.project_root) for p in explicit]
        mapping = bind_cfg.get("mapping", {})
        for pattern, value in mapping.items() if isinstance(mapping, Mapping) else []:
            if fnmatch.fnmatch(unit.rel, str(pattern)):
                values = value if isinstance(value, list) else [value]
                return [resolve_path(p, self.project_root) for p in values]
        if mode == "none" or mode == "template":
            return []
        if mode in {"existing_auto", "mapping"}:
            stem = unit.path.stem
            patterns = bind_cfg.get(
                "auto_patterns",
                [f"**/{stem}*bind*.sv", f"**/*{stem}*.sv", f"**/*{stem}*.svh"],
            )
            found: set[Path] = set()
            if self.bind_dir.is_dir():
                for pattern in patterns:
                    expanded = str(pattern).replace("{stem}", stem)
                    found.update(p.resolve() for p in self.bind_dir.glob(expanded) if p.is_file())
            return sorted(found)
        raise AQError(f"Unsupported bind.mode: {mode}")

    def prepare_bind(
        self,
        unit: AssertionUnit,
        target_dir: Path,
        preliminary_values: Mapping[str, str],
    ) -> list[Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        copied: list[Path] = []
        for src in self.select_bind_sources(unit):
            if not src.is_file():
                raise AQError(f"Bind file does not exist: {src}")
            target = target_dir / src.name
            shutil.copy2(src, target)
            copied.append(target.resolve())
        bind_cfg = self.config.get("bind", {})
        if isinstance(bind_cfg, Mapping) and str(bind_cfg.get("mode", "")) == "template":
            template = self.load_text_template(bind_cfg, "template", "template_file")
            if not template:
                raise AQError("bind.mode=template requires bind.template or bind.template_file")
            target_name = str(bind_cfg.get("target_name", f"{unit.unit_id}_bind.sv"))
            target = target_dir / target_name
            target.write_text(
                self.render_double_braces(template, preliminary_values), encoding="utf-8"
            )
            copied.append(target.resolve())
        return copied

    def run_one(
        self,
        stage: str,
        cfg: Mapping[str, Any],
        stage_hash: str,
        unit: AssertionUnit,
        block: AssertionBlock,
        progress_index: int = 0,
        progress_total: int = 0,
    ) -> RunResult:
        run_dir = self.output_root / "runs" / stage / unit.unit_id / block.assertion_id
        run_dir.mkdir(parents=True, exist_ok=True)
        assertion_file = run_dir / f"{unit.path.stem}__{block.assertion_id}{unit.path.suffix}"
        vacuity_plan = plan_vacuity(block)
        isolated_source = (
            render_vacuity_witness(unit.source, unit.assertions, block, vacuity_plan)
            if stage == "vacuity"
            else render_subset(unit.source, unit.assertions, {block.assertion_id}, label_unnamed=True)
        )
        assertion_file.write_text(
            isolated_source,
            encoding="utf-8",
        )
        tcl_file = run_dir / "run.tcl"
        log_file = run_dir / "run.log"
        prelim = self.variables(stage, unit, block, run_dir, assertion_file, tcl_file, log_file, [])
        bind_files = self.prepare_bind(unit, run_dir / "bind", prelim)
        values = self.variables(
            stage, unit, block, run_dir, assertion_file, tcl_file, log_file, bind_files
        )
        tcl_template = self.load_text_template(cfg, "tcl_template", "tcl_template_file")
        if tcl_template:
            tcl_file.write_text(self.render_double_braces(tcl_template, values), encoding="utf-8")
        command_template = str(cfg.get("command", ""))
        command = command_template.format_map(SafeFormatDict(values)) if command_template else ""
        (run_dir / "command.txt").write_text(command + "\n", encoding="utf-8")
        if self.dry_run:
            status, reason, exit_code, duration = "PLANNED", "dry-run: external command not executed", None, 0.0
            log_file.write_text("[AQ] dry-run; command not executed\n", encoding="utf-8")
        else:
            started = time.monotonic()
            env = os.environ.copy()
            env_cfg = cfg.get("env", {})
            if isinstance(env_cfg, Mapping):
                env.update({str(k): str(v) for k, v in env_cfg.items()})
            try:
                configured_timeout = float(cfg.get("timeout_seconds", self.timeout))
                outcome = run_logged(
                    command,
                    cwd=run_dir,
                    log_path=log_file,
                    timeout_seconds=configured_timeout,
                    label=(
                        f"AQ {stage} [{progress_index}/{progress_total}] "
                        f"{unit.rel}::{block.assertion_id}"
                    ),
                    shell=str(cfg.get("shell", "/bin/bash")),
                    env=env,
                )
                duration = outcome.duration_seconds
                exit_code = outcome.returncode
                if outcome.timed_out:
                    with log_file.open("a", encoding="utf-8") as handle:
                        handle.write("\n[AQ] TIMEOUT\n")
                    status, reason, exit_code = "TIMEOUT", f"Exceeded {configured_timeout} seconds", None
                else:
                    status, reason = self.parse_result(
                        stage, cfg, run_dir, outcome.output, exit_code if exit_code is not None else -1
                    )
            except Exception as exc:  # keep a per-property failure from aborting all workers
                duration = time.monotonic() - started
                log_file.write_text(f"[AQ] ERROR: {exc}\n", encoding="utf-8")
                status, reason, exit_code = "ERROR", str(exc), None
        result = RunResult(
            stage=stage,
            assertion_file=unit.rel,
            unit_id=unit.unit_id,
            module_guess=unit.module_guess,
            assertion_id=block.assertion_id,
            property_name=block.property_name,
            named=block.named,
            start_line=block.start_line,
            end_line=block.end_line,
            source_sha256=unit.source_sha256,
            fingerprint=block.fingerprint,
            stage_config_hash=stage_hash,
            status=status,
            reason=reason,
            exit_code=exit_code,
            duration_seconds=round(duration, 6),
            command=command,
            run_dir=self.relative_to_project(run_dir),
            artifact=self.relative_to_project(assertion_file),
            log_file=self.relative_to_project(log_file),
            assertion_kind=block.assertion_kind,
            vacuity_mode=vacuity_plan.mode if stage == "vacuity" else "",
        )
        (run_dir / "result.json").write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result

    def parse_result(
        self,
        stage: str,
        cfg: Mapping[str, Any],
        run_dir: Path,
        stdout: str,
        exit_code: int,
    ) -> tuple[str, str]:
        parser = cfg.get("parser", {})
        if not isinstance(parser, Mapping):
            parser = {}
        content = stdout
        for pattern in parser.get("search_files", []):
            for path in sorted(run_dir.glob(str(pattern))):
                if path.is_file() and path.name != "run.log":
                    content += f"\n===== {path.relative_to(run_dir)} =====\n"
                    content += path.read_text(encoding="utf-8", errors="replace")
        flags = re.IGNORECASE | re.MULTILINE

        def matched(key: str) -> bool:
            regex = parser.get(key)
            return bool(regex and re.search(str(regex), content, flags))

        success_codes = {int(x) for x in cfg.get("success_exit_codes", [0])}
        if stage == "syntax":
            if matched("fail_regex"):
                return "FAIL", "Matched fail_regex"
            if exit_code not in success_codes:
                return "FAIL", f"Command exit code {exit_code}"
            if matched("pass_regex"):
                return "PASS", "Matched pass_regex"
            if parser.get("pass_on_success_exit", True):
                return "PASS", f"Exit code {exit_code} with no matched failure pattern"
            return "UNKNOWN", "No syntax pass/fail regex matched"
        if stage == "fpv":
            if matched("cex_regex"):
                return "CEX", "Matched cex_regex"
            if matched("unknown_regex"):
                return "UNKNOWN", "Matched unknown_regex"
            if exit_code not in success_codes:
                return "UNKNOWN", f"Command exit code {exit_code} without an explicit CEX/PROVEN result"
            if matched("proven_regex"):
                return "PROVEN", "Matched proven_regex"
            return "UNKNOWN", "No proven/cex/unknown regex matched"
        if matched("vacuous_regex"):
            return "VACUOUS", "Matched vacuous_regex"
        if matched("not_applicable_regex"):
            return "NOT_APPLICABLE", "Vacuity is not applicable to this assertion form"
        if matched("unknown_regex"):
            return "UNKNOWN", "Matched unknown_regex"
        if exit_code not in success_codes:
            return "UNKNOWN", f"Command exit code {exit_code} without an explicit vacuity result"
        if matched("non_vacuous_regex"):
            return "PASS", "Matched non_vacuous_regex"
        return "UNKNOWN", "No vacuous/non_vacuous/unknown regex matched"

    def rejection_bucket(self, stage: str, status: str) -> str | None:
        if stage == "syntax":
            return {
                "FAIL": "syntax",
                "ERROR": "syntax",
                "UNKNOWN": "syntax_unknown",
                "TIMEOUT": "syntax_unknown",
            }.get(status)
        if stage == "fpv":
            return {
                "CEX": "fpv_cex",
                "UNKNOWN": "fpv_unknown",
                "TIMEOUT": "fpv_unknown",
                "ERROR": "fpv_unknown",
            }.get(status)
        return {
            "VACUOUS": "vacuity",
            "UNKNOWN": "vacuity_unknown",
            "TIMEOUT": "vacuity_unknown",
            "ERROR": "vacuity_unknown",
        }.get(status)

    def rejection_buckets_for_stage(self, stage: str) -> tuple[str, ...]:
        return {
            "syntax": ("syntax", "syntax_unknown"),
            "fpv": ("fpv_cex", "fpv_unknown"),
            "vacuity": ("vacuity", "vacuity_unknown"),
        }[stage]

    def materialize_stage_outputs(self, stage: str, results: Sequence[RunResult]) -> None:
        # Keep rejected/ synchronized with the current manifest. Without this,
        # a property that failed an earlier run but passes after a config/parser
        # fix would leave a stale file in rejected/ and mislead the user.
        # A dry-run must never erase genuine results from an earlier real run.
        if not self.dry_run:
            for unit in self.units:
                for bucket in self.rejection_buckets_for_stage(stage):
                    stale_dir = self.output_root / "rejected" / bucket / unit.unit_id
                    if stale_dir.is_dir():
                        shutil.rmtree(stale_dir)

        by_file: dict[str, list[RunResult]] = {}
        for result in results:
            by_file.setdefault(result.assertion_file, []).append(result)
            bucket = self.rejection_bucket(stage, result.status)
            if bucket:
                unit = next(u for u in self.units if u.rel == result.assertion_file)
                block = next(a for a in unit.assertions if a.assertion_id == result.assertion_id)
                target = (
                    self.output_root
                    / "rejected"
                    / bucket
                    / unit.unit_id
                    / f"{block.assertion_id}{unit.path.suffix}"
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    render_subset(unit.source, unit.assertions, {block.assertion_id}, label_unnamed=True),
                    encoding="utf-8",
                )
                reason_path = target.with_suffix(target.suffix + ".result.json")
                reason_path.write_text(
                    json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )

        pass_statuses = QUALIFIED_STATUSES[stage]
        work_name = {"syntax": "syntax_pass", "fpv": "fpv_pass", "vacuity": "vacuity_pass"}[stage]
        for unit in self.units:
            if unit.parse_errors:
                continue
            keep = {
                r.assertion_id
                for r in by_file.get(unit.rel, [])
                if r.status in pass_statuses
            }
            target_base = self.output_root / "work" / work_name
            if stage == "vacuity":
                target_base = self.output_root / "final" / "AQ"
            target = target_base / unit.rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(render_subset(unit.source, unit.assertions, keep), encoding="utf-8")
            bind_target = self.output_root / "work" / work_name / "_bind" / unit.unit_id
            if stage == "vacuity":
                bind_target = self.output_root / "final" / "AQ_bind" / unit.unit_id
            dummy_block = unit.assertions[0] if unit.assertions else AssertionBlock(
                "NONE", "NONE", False, 0, 0, 1, 1, "", "", "concurrent"
            )
            prelim = self.variables(
                stage, unit, dummy_block, bind_target.parent, target, bind_target / "run.tcl",
                bind_target / "run.log", []
            )
            self.prepare_bind(unit, bind_target, prelim)

    def result_counts(self, stage: str, unit: AssertionUnit) -> dict[str, int]:
        rows = self.results.get(stage)
        if rows is None:
            raw = self.read_manifest_rows(stage)
            if raw is not None:
                return count_status(
                    row
                    for row in raw
                    if row.get("assertion_file") == unit.rel
                    and row.get("source_sha256") == unit.source_sha256
                )
            return {}
        return count_status(
            dataclasses.asdict(r)
            for r in rows
            if r.assertion_file == unit.rel and r.source_sha256 == unit.source_sha256
        )

    def write_summary(self) -> None:
        if not self.units:
            return
        rows: list[dict[str, Any]] = []
        stage_exists = {
            stage: self.read_manifest_rows(stage) is not None for stage in STAGE_ORDER
        }
        all_stages_exist = all(stage_exists.values())
        for unit in self.units:
            syntax = self.result_counts("syntax", unit)
            fpv = self.result_counts("fpv", unit)
            vacuity = self.result_counts("vacuity", unit)
            original = unit.detected_assertions
            aq_count: int | str = (
                vacuity.get("PASS", 0) + vacuity.get("NOT_APPLICABLE", 0)
                if stage_exists["vacuity"]
                else ""
            )
            syntax_total = sum(syntax.values())
            fpv_total = sum(fpv.values())
            vacuity_total = sum(vacuity.values())
            complete_chain = (
                syntax_total == len(unit.assertions)
                and fpv_total == syntax.get("PASS", 0)
                and vacuity_total == fpv.get("PROVEN", 0)
            )
            planned_total = (
                syntax.get("PLANNED", 0)
                + fpv.get("PLANNED", 0)
                + vacuity.get("PLANNED", 0)
            )
            unknown_total = sum(
                syntax.get(k, 0) + fpv.get(k, 0) + vacuity.get(k, 0)
                for k in ("UNKNOWN", "TIMEOUT", "ERROR")
            )
            definitive_failures = (
                syntax.get("FAIL", 0)
                + fpv.get("CEX", 0)
                + vacuity.get("VACUOUS", 0)
            )
            if definitive_failures:
                readiness = "FAIL"
            elif (
                unit.parse_errors
                or original == 0
                or not all_stages_exist
                or not complete_chain
                or planned_total
                or unknown_total
            ):
                readiness = "UNRESOLVED"
            elif aq_count == original:
                readiness = "PASS"
            else:
                readiness = "UNRESOLVED"
            yield_value: float | str = ""
            if complete_chain and not unit.parse_errors and isinstance(aq_count, int) and original:
                yield_value = round(aq_count / original, 6)
            rows.append(
                {
                    "assertion_file": unit.rel,
                    "module_guess": unit.module_guess,
                    "top": unit.top,
                    "module": unit.module,
                    "original_assertions": original,
                    "concurrent_assertions": sum(
                        a.assertion_kind == "concurrent" for a in unit.assertions
                    ),
                    "immediate_assertions": sum(
                        a.assertion_kind == "immediate" for a in unit.assertions
                    ),
                    "syntax_pass": syntax.get("PASS", 0) if stage_exists["syntax"] else "",
                    "syntax_fail": syntax.get("FAIL", 0),
                    "syntax_unknown": syntax.get("UNKNOWN", 0) + syntax.get("TIMEOUT", 0) + syntax.get("ERROR", 0),
                    "fpv_proven": fpv.get("PROVEN", 0) if stage_exists["fpv"] else "",
                    "fpv_cex": fpv.get("CEX", 0),
                    "fpv_unknown": fpv.get("UNKNOWN", 0) + fpv.get("TIMEOUT", 0) + fpv.get("ERROR", 0),
                    "vacuity_pass": vacuity.get("PASS", 0) if stage_exists["vacuity"] else "",
                    "vacuity_not_applicable": (
                        vacuity.get("NOT_APPLICABLE", 0) if stage_exists["vacuity"] else ""
                    ),
                    "vacuous": vacuity.get("VACUOUS", 0),
                    "vacuity_unknown": vacuity.get("UNKNOWN", 0) + vacuity.get("TIMEOUT", 0) + vacuity.get("ERROR", 0),
                    "aq_count": aq_count,
                    "qualification_yield": yield_value,
                    "qualification_yield_percent": (
                        round(100.0 * yield_value, 2) if isinstance(yield_value, float) else ""
                    ),
                    "raw_readiness": readiness,
                    "parse_status": "MANUAL_REVIEW" if unit.parse_errors else "OK",
                    "parse_errors": " | ".join(unit.parse_errors),
                }
            )

        readiness_values = [str(row["raw_readiness"]) for row in rows]
        if any(value == "FAIL" for value in readiness_values):
            overall_readiness = "FAIL"
        elif readiness_values and all(value == "PASS" for value in readiness_values):
            overall_readiness = "PASS"
        else:
            overall_readiness = "UNRESOLVED"

        def numeric_sum(key: str) -> int:
            return sum(int(row[key]) for row in rows if isinstance(row.get(key), int))

        total_a0 = numeric_sum("original_assertions")
        yields_complete = bool(rows) and all(isinstance(row.get("qualification_yield"), float) for row in rows)
        total_aq: int | str = numeric_sum("aq_count") if yields_complete else ""
        overall_yield: float | str = (
            round(int(total_aq) / total_a0, 6)
            if isinstance(total_aq, int) and total_a0
            else ""
        )
        overall = {
            "assertion_file": "__OVERALL__",
            "module_guess": "",
            "top": "",
            "module": "",
            "original_assertions": total_a0,
            "concurrent_assertions": numeric_sum("concurrent_assertions"),
            "immediate_assertions": numeric_sum("immediate_assertions"),
            "syntax_pass": numeric_sum("syntax_pass") if stage_exists["syntax"] else "",
            "syntax_fail": numeric_sum("syntax_fail"),
            "syntax_unknown": numeric_sum("syntax_unknown"),
            "fpv_proven": numeric_sum("fpv_proven") if stage_exists["fpv"] else "",
            "fpv_cex": numeric_sum("fpv_cex"),
            "fpv_unknown": numeric_sum("fpv_unknown"),
            "vacuity_pass": numeric_sum("vacuity_pass") if stage_exists["vacuity"] else "",
            "vacuity_not_applicable": numeric_sum("vacuity_not_applicable") if stage_exists["vacuity"] else "",
            "vacuous": numeric_sum("vacuous"),
            "vacuity_unknown": numeric_sum("vacuity_unknown"),
            "aq_count": total_aq,
            "qualification_yield": overall_yield,
            "qualification_yield_percent": (
                round(100.0 * overall_yield, 2) if isinstance(overall_yield, float) else ""
            ),
            "raw_readiness": overall_readiness,
            "parse_status": "OK" if not any(unit.parse_errors for unit in self.units) else "MANUAL_REVIEW",
            "parse_errors": "",
        }
        write_csv_json(self.output_root / "final_summary", [*rows, overall])

        report = {
            "input_variant": self.input_variant,
            "assertions_dir": str(self.assertions_dir),
            "output_dir": str(self.output_root),
            "raw_readiness": overall_readiness,
            "a0_count": total_a0,
            "aq_count": total_aq if isinstance(total_aq, int) else None,
            "qualification_yield": overall_yield if isinstance(overall_yield, float) else None,
            "qualification_yield_percent": (
                round(100.0 * overall_yield, 2) if isinstance(overall_yield, float) else None
            ),
            "assertion_files": len(rows),
            "stages_complete": yields_complete,
        }
        atomic_write_text(
            self.output_root / "qualification_report.json",
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        )
        yield_text = (
            f"{total_aq}/{total_a0} ({100.0 * overall_yield:.2f}%)"
            if isinstance(total_aq, int) and isinstance(overall_yield, float)
            else "UNRESOLVED"
        )
        atomic_write_text(
            self.output_root / "qualification_report.txt",
            "AQ QUALIFICATION REPORT\n"
            f"Input Variant      : {self.input_variant.upper()}\n"
            f"Assertions Folder  : {self.assertions_dir}\n"
            f"Raw Readiness      : {overall_readiness}\n"
            f"Generated Set (A0) : {total_a0}\n"
            f"Qualified Set (AQ) : {total_aq if isinstance(total_aq, int) else 'UNRESOLVED'}\n"
            f"Qualification Yield: {yield_text}\n",
        )

    def print_summary(self) -> None:
        path = self.output_root / "qualification_report.txt"
        if path.is_file():
            print("\n" + path.read_text(encoding="utf-8").rstrip())

    def run_all(self) -> None:
        self.inventory()
        for stage in STAGE_ORDER:
            self.run_stage(stage)


def count_status(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
    return counts


def write_csv_json(base: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    atomic_write_text(
        json_path,
        json.dumps(list(rows), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    tmp_path = csv_path.with_name(
        f".{csv_path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    try:
        with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            if fieldnames:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
        os.replace(tmp_path, csv_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write beside the destination and atomically replace it on success."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        tmp_path.write_text(text, encoding=encoding)
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SVA Qualification (AQ) pipeline using each assertion file as one unit"
    )
    parser.add_argument("--config", required=True, type=Path, help="YAML or JSON configuration")
    parser.add_argument(
        "--project-root",
        help="Override project_root from the configuration; defaults to automatic discovery",
    )
    parser.add_argument(
        "-buggy",
        "--buggy",
        action="store_true",
        help=(
            "Read buggy_assertions/ and write aq_buggy_out/ instead of "
            "assertions/ and aq_out/."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Generate cases and commands without invoking Jasper")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Backward-compatible option; valid results are reused by default",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore existing manifests and rerun every candidate in the selected stage",
    )
    parser.add_argument("--workers", type=int, help="Parallel property count; defaults to configuration")
    parser.add_argument("--timeout", type=float, help="Timeout in seconds for one property")
    parser.add_argument("--include", action="append", help="Override include globs; repeatable")
    parser.add_argument("--exclude", action="append", help="Override exclude globs; repeatable")
    parser.add_argument(
        "command",
        choices=["inventory", "syntax", "fpv", "vacuity", "all", "summary"],
        help="Stage to execute",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pipeline = Pipeline(args.config, args)
        if args.command == "all":
            pipeline.run_all()
        elif args.command == "inventory":
            pipeline.inventory()
        elif args.command == "summary":
            pipeline.inventory()
            pipeline.write_summary()
        else:
            pipeline.inventory()
            pipeline.run_stage(args.command)
        pipeline.print_summary()
        print(f"Complete: {args.command}; output directory: {pipeline.output_root}")
        return 0
    except AQError as exc:
        eprint(f"AQ ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("AQ: interrupted by user")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

