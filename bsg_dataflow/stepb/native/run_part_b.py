#!/usr/bin/env python3
"""Portable Part-B Integrity and AQ-Compactness measurement with Jasper.

The important invariant is that every subset run contains exactly the selected
AQ assertions, no cover properties, the same RTL/mutation universe, and no
unprocessed mutation items.  Compactness is searched only after those checks.

Integrity          = detected RTL mutations / fixed RTL mutation universe
AQ retention ratio = |A*| / |AQ|
Redundancy ratio   = 1 - |A*| / |AQ|

The optional end-to-end ratio |A*| / |A0| is reported only as a derived
cross-pipeline value.  It is not used as the Part-B compactness denominator.

The exact search uses Jasper's mutation checker-coverage count as a monotonic
set oracle.  The mutation universe is fixed as total - excluded - unprocessed.
Jasper's checker-relative "undetectable" classification is allowed to change
when assertions are removed; the raw total, excluded count, and unprocessed
count must remain stable.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import fnmatch
import hashlib
import itertools
import json
import math
import os
import random
import re
import shlex
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from process_watch import run_logged

try:
    from assertion_aq_pipeline import (
        extract_assertions,
        find_statement_end,
        include_label_prefix,
        mask_comments_and_strings,
        matching_delimiter,
        render_subset,
        skip_space,
    )
except ImportError as exc:
    raise SystemExit(
        "PART-B ERROR: assertion_aq_pipeline.py must be next to run_part_b.py"
    ) from exc


ALGORITHM_VERSION = "portable_jasper_checker_oracle_v5_fixed_fault_universe"
RTL_SUFFIXES = {".v", ".sv", ".vh", ".svh", ".sva"}


class RQ2Error(RuntimeError):
    pass


class SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        raise RQ2Error(f"Template references unknown variable {{{key}}}")


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def load_structured(path: Path) -> Any:
    if not path.is_file():
        raise RQ2Error(f"File does not exist: {path}")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RQ2Error("Reading YAML requires PyYAML: python3 -m pip install pyyaml") from exc
    return yaml.safe_load(text)


def resolve_path(value: str | Path, base: Path) -> Path:
    path = Path(os.path.expandvars(os.path.expanduser(str(value))))
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def discover_project_root(config_dir: Path, paths: Mapping[str, Any]) -> Path:
    aq_value = str(paths.get("aq_output_dir", "aq_out"))
    rtl_value = str(paths.get("rtl_dir", "rtl"))
    searched: list[str] = []
    for candidate in [config_dir.resolve(), *config_dir.resolve().parents]:
        aq_root = resolve_path(aq_value, candidate)
        rtl_root = resolve_path(rtl_value, candidate)
        searched.append(str(candidate))
        if (aq_root / "final_summary.json").is_file() and rtl_root.is_dir():
            return candidate
    raise RQ2Error(
        "project_root=auto could not find rtl/ and aq_out/final_summary.json.\nSearched: "
        + " | ".join(searched)
    )


def tcl_atom(value: str | Path) -> str:
    """Quote one Tcl list atom without allowing command substitution."""
    text = str(value).replace("\\", "/").replace("{", "\\{").replace("}", "\\}")
    return "{" + text + "}"


def tcl_list(values: Sequence[str | Path]) -> str:
    return " ".join(tcl_atom(value) for value in values)


def find_aq_config(project_root: Path, explicit: str | Path | None) -> Path:
    if explicit:
        path = resolve_path(explicit, project_root)
        if not path.is_file():
            raise RQ2Error(f"AQ configuration does not exist: {path}")
        return path
    candidates = [
        project_root / "aq_qualification_pipeline_v1" / "aq_config.json",
        project_root / "aq_qualification_pipeline_v1" / "aq_config.yaml",
        project_root / "aq_config.json",
        project_root / "aq_config.yaml",
    ]
    for path in candidates:
        if path.is_file():
            return path
    raise RQ2Error(
        "AQ configuration was not found. Put this toolkit inside the dataset "
        "folder or pass --aq-config PATH."
    )


def match_mapping(mapping: Mapping[str, Any], assertion_file: str) -> Mapping[str, Any]:
    matches: list[tuple[int, Mapping[str, Any]]] = []
    for pattern, value in mapping.items():
        if fnmatch.fnmatch(assertion_file, str(pattern)) or Path(assertion_file).match(str(pattern)):
            if isinstance(value, Mapping):
                matches.append((len(str(pattern)), value))
    if not matches:
        raise RQ2Error(f"No units.mapping entry matches {assertion_file}")
    return max(matches, key=lambda item: item[0])[1]


def match_bind_files(bind_cfg: Mapping[str, Any], assertion_file: str) -> list[str]:
    if str(bind_cfg.get("mode", "none")).lower() != "mapping":
        return []
    mapping = bind_cfg.get("mapping", {})
    if not isinstance(mapping, Mapping):
        return []
    matches: list[tuple[int, list[str]]] = []
    for pattern, value in mapping.items():
        if fnmatch.fnmatch(assertion_file, str(pattern)) or Path(assertion_file).match(str(pattern)):
            files = [str(x) for x in value] if isinstance(value, list) else [str(value)]
            matches.append((len(str(pattern)), files))
    return max(matches, key=lambda item: item[0])[1] if matches else []


def safe_slug(value: str, limit: int = 80) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "item"
    return slug[:limit]


def stable_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.is_dir():
        return "MISSING"
    files = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in RTL_SUFFIXES
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        tmp.write_text(text, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_csv_json(base: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    materialized = list(rows)
    atomic_write(
        base.with_suffix(".json"),
        json.dumps(materialized, ensure_ascii=False, indent=2) + "\n",
    )
    fields: list[str] = []
    for row in materialized:
        for key in row:
            if key not in fields:
                fields.append(key)
    csv_path = base.with_suffix(".csv")
    tmp = csv_path.with_name(f".{csv_path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
            if fields:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(materialized)
        os.replace(tmp, csv_path)
    finally:
        if tmp.exists():
            tmp.unlink()


def neutralize_cover_statements(source: str) -> tuple[str, int, list[str]]:
    """Remove immediate/concurrent cover statements while preserving line count.

    Mutation compactness must be attributable only to the selected assertions.
    Unsupported cover syntax fails closed instead of silently leaving a checker.
    """
    masked, errors = mask_comments_and_strings(source)
    token_re = re.compile(r"\bcover\b", re.IGNORECASE)
    spans: list[tuple[int, int]] = []
    occupied_until = -1
    for match in token_re.finditer(masked):
        if match.start() < occupied_until:
            continue
        pos = skip_space(masked, match.end())
        property_match = re.match(r"property\b", masked[pos:], re.IGNORECASE)
        if property_match:
            pos = skip_space(masked, pos + property_match.end())
        if pos >= len(masked) or masked[pos] != "(":
            errors.append(f"Cannot safely remove cover at line {source.count(chr(10), 0, match.start()) + 1}")
            continue
        close_pos = matching_delimiter(masked, pos, "(", ")")
        if close_pos is None:
            errors.append("Unclosed cover-statement parentheses")
            continue
        end, end_error = find_statement_end(masked, close_pos + 1)
        if end is None:
            errors.append(end_error or "No safe end found for cover statement")
            continue
        start, _label = include_label_prefix(source, match.start())
        spans.append((start, end))
        occupied_until = end
    if errors:
        return source, len(spans), errors
    parts: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(spans, start=1):
        parts.append(source[cursor:start])
        text = source[start:end]
        parts.append(f"/* RQ2 removed cover C{index:04d} */" + "\n" * text.count("\n"))
        cursor = end
    parts.append(source[cursor:])
    return "".join(parts), len(spans), []


@dataclasses.dataclass
class Unit:
    assertion_file: str
    unit_id: str
    top: str
    module_guess: str
    a0_count: int
    aq_count: int
    qualification_yield: float
    source_path: Path
    source: str
    source_sha256: str
    blocks: list[Any]
    rtl_files: list[Path]
    include_dirs: list[Path]
    defines: list[str]
    bind_files: list[Path]
    clock_mode: str
    clock: str
    reset_expression: str


@dataclasses.dataclass
class CoverageResult:
    assertion_file: str
    unit_id: str
    top: str
    subset_key: str
    subset_size: int
    assertion_ids: list[str]
    source_sha256: str
    experiment_hash: str
    status: str
    checked: int
    eligible: int
    total: int
    unchecked: int
    undetectable: int
    unprocessed: int
    excluded: int
    active_assertion_count: int
    active_cover_count: int
    active_properties: list[str]
    integrity: float | None
    command: str
    run_dir: str
    log_file: str
    exit_code: int | None
    duration_seconds: float
    reason: str
    resumed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class OptimizationResult:
    assertion_file: str
    unit_id: str
    top: str
    source_sha256: str
    experiment_hash: str
    a0_count: int
    aq_count: int
    target_checked: int
    eligible_faults: int
    selected_ids: list[str]
    selected_count: int
    exact_minimum: bool
    method: str
    stop_reason: str
    oracle_runs_used: int
    duration_seconds: float

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class Pipeline:
    def __init__(self, config_path: Path, args: argparse.Namespace):
        self.config_path = config_path.resolve()
        raw = load_structured(self.config_path)
        if not isinstance(raw, Mapping):
            raise RQ2Error("The top level of the configuration must be a mapping")
        self.cfg = dict(raw)
        self.config_dir = self.config_path.parent
        paths = self.cfg.get("paths", {})
        if not isinstance(paths, Mapping):
            raise RQ2Error("paths must be a mapping")
        project_value = self.cfg.get("project_root", ".")
        self.project_root = (
            discover_project_root(self.config_dir, paths)
            if str(project_value).strip().lower() == "auto"
            else resolve_path(project_value, self.config_dir)
        )
        self.aq_root = resolve_path(paths.get("aq_output_dir", "aq_out"), self.project_root)
        self.output = resolve_path(paths.get("output_dir", "aq_out/part_b_set_quality"), self.project_root)
        self.rtl_dir = resolve_path(paths.get("rtl_dir", "rtl"), self.project_root)
        aq_config_value = getattr(args, "aq_config", None) or self.cfg.get("aq_config")
        self.aq_config_path = find_aq_config(self.project_root, aq_config_value)
        aq_raw = load_structured(self.aq_config_path)
        if not isinstance(aq_raw, Mapping):
            raise RQ2Error("The AQ configuration must be a mapping")
        self.aq_cfg = dict(aq_raw)
        self.spec_results = Path(args.spec_results).resolve() if getattr(args, "spec_results", None) else None
        self.spec_pass_keys = self._load_spec_pass_keys(self.spec_results)
        self.manifest_dir = self.output / "manifests"
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self.force = bool(args.force)
        self.dry_run = bool(args.dry_run)
        self.timeout = float(args.timeout or self.cfg.get("timeout_seconds", 14400))
        self.include = list(args.include or ["*"])
        self.units: list[Unit] = []
        self.coverage: dict[tuple[str, str], CoverageResult] = {}
        self.optimizations: dict[str, OptimizationResult] = {}
        self.new_oracle_runs: dict[str, int] = {}
        self.rtl_sha256 = hash_tree(self.rtl_dir)
        self.experiment_hash = self._experiment_hash()
        self._load_coverage_cache()
        self._load_optimization_cache()

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.project_root).as_posix()
        except ValueError:
            return str(path.resolve())

    def mutation_config(self) -> Mapping[str, Any]:
        cfg = self.cfg.get("mutation", {})
        if not isinstance(cfg, Mapping):
            raise RQ2Error("mutation must be a mapping")
        return cfg

    def optimization_config(self) -> Mapping[str, Any]:
        cfg = self.cfg.get("optimization", {})
        if not isinstance(cfg, Mapping):
            raise RQ2Error("optimization must be a mapping")
        return cfg

    def _experiment_hash(self) -> str:
        mutation = dict(self.mutation_config())
        template_file = mutation.get("tcl_template_file")
        template_sha = ""
        if template_file:
            path = resolve_path(str(template_file), self.config_dir)
            template_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        jasper_cfg = self.cfg.get("jasper", {})
        return stable_hash(
            {
                "algorithm": ALGORITHM_VERSION,
                "mutation": mutation,
                "eligibility": self.cfg.get("eligibility", {}),
                "template_sha256": template_sha,
                "rtl_sha256": self.rtl_sha256,
                "jasper": jasper_cfg,
                "aq_config": self.aq_cfg,
                "spec_results": str(self.spec_results) if self.spec_results else "",
                "spec_pass_keys": sorted(self.spec_pass_keys) if self.spec_pass_keys is not None else None,
            }
        )

    @staticmethod
    def _load_spec_pass_keys(path: Path | None) -> set[tuple[str, str]] | None:
        if path is None:
            return None
        if not path.is_file():
            raise RQ2Error(f"Specification-result file does not exist: {path}")
        if path.suffix.lower() == ".json":
            rows = json.loads(path.read_text(encoding="utf-8-sig"))
        else:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        if not isinstance(rows, list):
            raise RQ2Error("Specification-result file must contain a row list")
        keys: set[tuple[str, str]] = set()
        seen = 0
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            assertion_file = str(row.get("assertion_file", "")).strip()
            assertion_id = str(row.get("assertion_id", "")).strip()
            verdict = str(row.get("final_verdict", row.get("verdict", ""))).strip().upper()
            if assertion_file and assertion_id:
                seen += 1
                if verdict in {"PASS", "UNRESOLVED"}:
                    keys.add((assertion_file, assertion_id))
        if seen == 0:
            raise RQ2Error("Specification-result file contains no assertion_file/assertion_id rows")
        return keys

    def _unit_settings(self, assertion_file: str) -> dict[str, Any]:
        units_cfg = self.aq_cfg.get("units", {})
        if not isinstance(units_cfg, Mapping):
            raise RQ2Error("AQ units must be a mapping")
        mapping = units_cfg.get("mapping", {})
        if not isinstance(mapping, Mapping):
            raise RQ2Error("AQ units.mapping must be a mapping")
        unit_cfg = match_mapping(mapping, assertion_file)
        rtl_cfg = self.aq_cfg.get("rtl", {})
        if not isinstance(rtl_cfg, Mapping):
            rtl_cfg = {}
        bind_cfg = self.aq_cfg.get("bind", {})
        if not isinstance(bind_cfg, Mapping):
            bind_cfg = {}
        formal_cfg = self.aq_cfg.get("formal", {})
        if not isinstance(formal_cfg, Mapping):
            formal_cfg = {}
        rtl_files = [resolve_path(str(x), self.project_root) for x in unit_cfg.get("rtl_files", [])]
        if not rtl_files:
            raise RQ2Error(f"{assertion_file}: units.mapping must define rtl_files")
        include_dirs = [resolve_path(str(x), self.project_root) for x in rtl_cfg.get("include_dirs", ["rtl"])]
        bind_files = [resolve_path(x, self.project_root) for x in match_bind_files(bind_cfg, assertion_file)]
        missing = [str(path) for path in [*rtl_files, *bind_files] if not path.is_file()]
        if missing:
            raise RQ2Error(f"{assertion_file}: required source files are missing: {', '.join(missing)}")
        return {
            "top": str(unit_cfg.get("top", "")),
            "rtl_files": rtl_files,
            "include_dirs": include_dirs,
            "defines": [str(x) for x in rtl_cfg.get("defines", [])],
            "bind_files": bind_files,
            "clock_mode": str(unit_cfg.get("clock_mode", formal_cfg.get("clock_mode", "auto"))),
            "clock": str(unit_cfg.get("clock", formal_cfg.get("clock", ""))),
            "reset_expression": str(unit_cfg.get("reset_expression", formal_cfg.get("reset_expression", ""))),
        }

    def inventory(self) -> list[Unit]:
        raw = load_structured(self.aq_root / "final_summary.json")
        if not isinstance(raw, list):
            raise RQ2Error("final_summary.json must be a JSON array")
        units: list[Unit] = []
        errors: list[str] = []
        for row in raw:
            if not isinstance(row, Mapping):
                continue
            rel = str(row.get("assertion_file", ""))
            if not rel or rel.startswith("__"):
                continue
            if not rel or not any(Path(rel).match(pattern) for pattern in self.include):
                continue
            aq_count = int(row.get("aq_count", 0) or 0)
            if aq_count <= 0:
                continue
            source_path = self.aq_root / "final" / "AQ" / rel
            if not source_path.is_file():
                errors.append(f"AQ source missing: {source_path}")
                continue
            source = source_path.read_text(encoding="utf-8", errors="replace")
            blocks, parse_errors = extract_assertions(source)
            if parse_errors:
                errors.append(f"{rel}: {' | '.join(parse_errors)}")
                continue
            if len(blocks) != aq_count:
                errors.append(f"{rel}: summary AQ={aq_count}, extracted={len(blocks)}")
                continue
            if self.spec_pass_keys is not None:
                original_blocks = list(blocks)
                selected = {
                    block.assertion_id
                    for block in blocks
                    if (rel, block.assertion_id) in self.spec_pass_keys
                }
                source = render_subset(source, blocks, selected, label_unnamed=True)
                filtered_blocks, parse_errors = extract_assertions(source)
                if parse_errors:
                    errors.append(f"{rel}: post-spec source parse failed: {' | '.join(parse_errors)}")
                    continue
                original_by_line = {block.start_line: block for block in original_blocks}
                blocks = [
                    dataclasses.replace(
                        block,
                        assertion_id=original_by_line[block.start_line].assertion_id,
                    )
                    if block.start_line in original_by_line
                    else block
                    for block in filtered_blocks
                ]
                aq_count = len(blocks)
                if aq_count == 0:
                    continue
            _probe, cover_count, cover_errors = neutralize_cover_statements(source)
            if cover_errors:
                errors.append(f"{rel}: cover isolation failed: {' | '.join(cover_errors)}")
                continue
            try:
                settings = self._unit_settings(rel)
            except RQ2Error as exc:
                errors.append(str(exc))
                continue
            top = str(settings["top"] or row.get("top", ""))
            if not top:
                errors.append(f"{rel}: top is empty")
                continue
            units.append(
                Unit(
                    assertion_file=rel,
                    unit_id=f"{safe_slug(Path(rel).stem)}__{hashlib.sha1(rel.encode()).hexdigest()[:8]}",
                    top=top,
                    module_guess=str(row.get("module_guess", "")),
                    a0_count=int(row.get("original_assertions", 0) or 0),
                    aq_count=aq_count,
                    qualification_yield=float(row.get("qualification_yield", 0) or 0),
                    source_path=source_path,
                    source=source,
                    source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                    blocks=blocks,
                    rtl_files=settings["rtl_files"],
                    include_dirs=settings["include_dirs"],
                    defines=settings["defines"],
                    bind_files=settings["bind_files"],
                    clock_mode=settings["clock_mode"],
                    clock=settings["clock"],
                    reset_expression=settings["reset_expression"],
                )
            )
            print(f"RQ2 inventory file: {rel}; AQ={aq_count}; covers_to_disable={cover_count}; top={top}")
        if errors:
            raise RQ2Error("Part-B inventory errors:\n  " + "\n  ".join(errors))
        if not units:
            raise RQ2Error("No non-empty AQ was found; finish qualification first")
        self.units = units
        write_csv_json(
            self.manifest_dir / "inventory",
            [
                {
                    "assertion_file": u.assertion_file,
                    "unit_id": u.unit_id,
                    "top": u.top,
                    "a0_count": u.a0_count,
                    "aq_count": u.aq_count,
                    "qualification_yield": u.qualification_yield,
                    "aq_source": self.relative(u.source_path),
                    "source_sha256": u.source_sha256,
                    "rtl_sha256": self.rtl_sha256,
                    "experiment_hash": self.experiment_hash,
                    "rtl_files": ";".join(self.relative(p) for p in u.rtl_files),
                    "bind_files": ";".join(self.relative(p) for p in u.bind_files),
                    "clock_mode": u.clock_mode,
                    "clock": u.clock,
                    "reset_expression": u.reset_expression,
                }
                for u in units
            ],
        )
        print(f"RQ2 inventory: files={len(units)}, A0={sum(u.a0_count for u in units)}, AQ={sum(u.aq_count for u in units)}")
        return units

    def _load_coverage_cache(self) -> None:
        path = self.manifest_dir / "subset_runs.json"
        if not path.is_file() or self.force:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            eprint(f"PART-B WARNING: ignoring damaged cache {path}: {exc}")
            return
        known = {f.name for f in dataclasses.fields(CoverageResult)}
        for row in raw if isinstance(raw, list) else []:
            try:
                item = CoverageResult(**{k: v for k, v in row.items() if k in known})
            except (TypeError, AttributeError):
                continue
            if item.status == "PASS":
                self.coverage[(item.assertion_file, item.subset_key)] = item

    def _load_optimization_cache(self) -> None:
        path = self.manifest_dir / "optimization.json"
        if not path.is_file() or self.force:
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        known = {f.name for f in dataclasses.fields(OptimizationResult)}
        for row in raw if isinstance(raw, list) else []:
            try:
                item = OptimizationResult(**{k: v for k, v in row.items() if k in known})
            except (TypeError, AttributeError):
                continue
            self.optimizations[item.assertion_file] = item

    def _flush_coverage(self) -> None:
        rows = [x.as_dict() for x in sorted(self.coverage.values(), key=lambda y: (y.assertion_file, y.subset_key))]
        write_csv_json(self.manifest_dir / "subset_runs", rows)

    def _flush_optimizations(self) -> None:
        rows = [self.optimizations[k].as_dict() for k in sorted(self.optimizations)]
        write_csv_json(self.manifest_dir / "optimization", rows)
        self._write_minimal_subset_artifacts(partial=True)

    @staticmethod
    def subset_key(ids: set[str]) -> str:
        return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:20]

    def _template(self) -> str:
        cfg = self.mutation_config()
        if cfg.get("tcl_template_file"):
            return resolve_path(str(cfg["tcl_template_file"]), self.config_dir).read_text(encoding="utf-8")
        return str(cfg.get("tcl_template", ""))

    @staticmethod
    def _render(template: str, values: Mapping[str, str]) -> str:
        pattern = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
        unknown = sorted({m.group(1) for m in pattern.finditer(template)} - set(values))
        if unknown:
            raise RQ2Error("Tcl template unknown variables: " + ", ".join(unknown))
        return pattern.sub(lambda m: values[m.group(1)], template)

    def _eligibility(self, counts: Mapping[str, int]) -> tuple[int, str | None]:
        policy = self.cfg.get("eligibility", {})
        if not isinstance(policy, Mapping):
            policy = {}
        if int(counts["unprocessed"]) != 0 and bool(policy.get("require_no_unprocessed", True)):
            return 0, f"unprocessed={counts['unprocessed']}; mutation universe is incomplete"
        mode = str(policy.get("denominator", "fixed_total_minus_excluded"))
        if mode == "fixed_total_minus_excluded":
            eligible = (
                int(counts["total"])
                - int(counts["excluded"])
                - int(counts["unprocessed"])
            )
        elif mode == "checked_plus_unchecked":
            eligible = int(counts["checked"]) + int(counts["unchecked"])
        elif mode == "jasper_total":
            eligible = int(counts["total"])
        else:
            return 0, f"Unknown eligibility.denominator={mode}"
        if eligible <= 0:
            return 0, "eligible mutation items = 0"
        if bool(policy.get("require_partition_sum", True)):
            component_sum = sum(int(counts[k]) for k in ("checked", "unchecked", "undetectable", "unprocessed", "excluded"))
            if component_sum != int(counts["total"]):
                return 0, f"Jasper fields do not form a partition: components={component_sum}, total={counts['total']}"
        return eligible, None

    def measure(self, unit: Unit, ids: set[str]) -> CoverageResult:
        key = self.subset_key(ids)
        cache_key = (unit.assertion_file, key)
        old = self.coverage.get(cache_key)
        if (
            old and not self.force
            and old.source_sha256 == unit.source_sha256
            and old.experiment_hash == self.experiment_hash
            and old.top == unit.top
            and set(old.assertion_ids) == ids
        ):
            old.resumed = True
            print(
                f"[RESUME] mutation file={unit.assertion_file} "
                f"subset={len(ids)}/{unit.aq_count} key={key}",
                flush=True,
            )
            return old
        if not ids:
            return CoverageResult(
                assertion_file=unit.assertion_file, unit_id=unit.unit_id, top=unit.top,
                subset_key=key, subset_size=0, assertion_ids=[], source_sha256=unit.source_sha256,
                experiment_hash=self.experiment_hash, status="PASS", checked=0, eligible=0,
                total=0, unchecked=0, undetectable=0, unprocessed=0, excluded=0,
                active_assertion_count=0, active_cover_count=0, active_properties=[], integrity=None,
                command="", run_dir="", log_file="", exit_code=0, duration_seconds=0.0,
                reason="empty subset",
            )
        run_dir = self.output / "runs" / unit.unit_id / f"n{len(ids):04d}_{key}"
        run_dir.mkdir(parents=True, exist_ok=True)
        assertion_source = run_dir / f"{Path(unit.assertion_file).stem}__subset.sv"
        subset_source = render_subset(unit.source, unit.blocks, ids, label_unnamed=True)
        subset_source, removed_covers, cover_errors = neutralize_cover_statements(subset_source)
        if cover_errors:
            raise RQ2Error(f"{unit.assertion_file}: cannot isolate cover statements: {' | '.join(cover_errors)}")
        atomic_write(assertion_source, subset_source)
        tcl_file = run_dir / "run.tcl"
        log_file = run_dir / "run.log"
        jasper_cfg = self.cfg.get("jasper", {})
        jasper = str(jasper_cfg.get("executable", "jg")) if isinstance(jasper_cfg, Mapping) else "jg"
        cfg = self.mutation_config()
        values = {
            "project_root": str(self.project_root), "rtl_dir": str(self.rtl_dir),
            "run_dir": str(run_dir), "assertion_file": str(assertion_source),
            "assertion_rel": unit.assertion_file, "top": unit.top, "subset_key": key,
            "subset_size": str(len(ids)), "expected_assertions": str(len(ids)),
            "removed_cover_count": str(removed_covers), "jasper_executable": jasper,
            "tcl_file": str(tcl_file), "log_file": str(log_file),
            "rtl_files_tcl": tcl_list(unit.rtl_files),
            "include_dirs_tcl": tcl_list(unit.include_dirs),
            "defines_tcl": tcl_list(unit.defines),
            "bind_files_tcl": tcl_list(unit.bind_files),
            "clock_mode": unit.clock_mode,
            "clock": unit.clock,
            "reset_expression": unit.reset_expression,
            "clock_mode_tcl": tcl_atom(unit.clock_mode),
            "clock_tcl": tcl_atom(unit.clock),
            "reset_expression_tcl": tcl_atom(unit.reset_expression),
            "mutation_scope_tcl": tcl_atom(str(cfg.get("scope", "hierarchy"))),
        }
        values.update({f"q_{k}": shlex.quote(v) for k, v in list(values.items())})
        atomic_write(tcl_file, self._render(self._template(), values))
        command_template = str(cfg.get("command", "{q_jasper_executable} -batch {q_tcl_file}"))
        command = command_template.format_map(SafeFormatDict(values))
        atomic_write(run_dir / "command.txt", command + "\n")
        if self.dry_run:
            result = CoverageResult(
                assertion_file=unit.assertion_file, unit_id=unit.unit_id, top=unit.top,
                subset_key=key, subset_size=len(ids), assertion_ids=sorted(ids),
                source_sha256=unit.source_sha256, experiment_hash=self.experiment_hash,
                status="PLANNED", checked=0, eligible=0, total=0, unchecked=0,
                undetectable=0, unprocessed=0, excluded=0, active_assertion_count=0,
                active_cover_count=0, active_properties=[], integrity=None, command=command,
                run_dir=self.relative(run_dir), log_file=self.relative(log_file), exit_code=None,
                duration_seconds=0.0, reason="dry-run",
            )
            atomic_write(log_file, "[RQ2] dry-run; command not executed\n")
            return result
        zero = {k: 0 for k in ("checked", "total", "unchecked", "undetectable", "unprocessed", "excluded")}
        attempt_dir = run_dir / "attempts" / f"attempt_{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        label = (
            f"Part-B mutation file={unit.assertion_file} "
            f"subset={len(ids)}/{unit.aq_count} key={key}"
        )
        watched = run_logged(
            command,
            cwd=attempt_dir,
            log_path=log_file,
            timeout_seconds=float(cfg.get("timeout_seconds", self.timeout)),
            label=label,
            shell=str(cfg.get("shell", "/bin/bash")),
            env=os.environ.copy(),
            heartbeat_seconds=float(cfg.get("heartbeat_seconds", 60)),
        )
        output = watched.output
        if not watched.timed_out:
            counts = self.parse_counts(output) or zero
            active_count = self.parse_named_count(output, "RQ2_ACTIVE_ASSERTION_COUNT")
            cover_count = self.parse_named_count(output, "RQ2_ACTIVE_COVER_COUNT")
            active_properties = re.findall(r"^RQ2_ACTIVE_ASSERTION\s+name=(.+)$", output, re.MULTILINE)
            proof = self.parse_proof_counts(output)
            reasons: list[str] = []
            if watched.returncode not in {int(x) for x in cfg.get("success_exit_codes", [0])}:
                reasons.append(f"exit={watched.returncode}")
            if not re.search(r"^RQ2_MUTATION_PASS$", output, re.MULTILINE):
                reasons.append("missing RQ2_MUTATION_PASS")
            if self.parse_counts(output) is None:
                reasons.append("missing RQ2_MUTATION_COUNTS")
            if active_count != len(ids):
                reasons.append(f"active assertions={active_count}, expected={len(ids)}")
            if proof != (len(ids), len(ids), 0):
                reasons.append(f"proof counts={proof}, expected total/proven/bad={len(ids)}/{len(ids)}/0")
            eligible, eligibility_error = self._eligibility(counts)
            if eligibility_error:
                reasons.append(eligibility_error)
            status = "ERROR" if reasons else "PASS"
            reason = "; ".join(reasons) if reasons else "strict Jasper mutation coverage complete"
            exit_code = watched.returncode
        else:
            counts, eligible, active_count, cover_count, active_properties = zero, 0, 0, 0, []
            status, reason, exit_code = "TIMEOUT", "Jasper subset timeout", None
        duration = round(watched.duration_seconds, 6)
        result = CoverageResult(
            assertion_file=unit.assertion_file, unit_id=unit.unit_id, top=unit.top,
            subset_key=key, subset_size=len(ids), assertion_ids=sorted(ids),
            source_sha256=unit.source_sha256, experiment_hash=self.experiment_hash,
            status=status, checked=int(counts["checked"]), eligible=int(eligible), total=int(counts["total"]),
            unchecked=int(counts["unchecked"]), undetectable=int(counts["undetectable"]),
            unprocessed=int(counts["unprocessed"]), excluded=int(counts["excluded"]),
            active_assertion_count=int(active_count), active_cover_count=int(cover_count),
            active_properties=active_properties,
            integrity=(int(counts["checked"]) / int(eligible) if status == "PASS" and eligible else None),
            command=command, run_dir=self.relative(run_dir), log_file=self.relative(log_file),
            exit_code=exit_code, duration_seconds=duration, reason=reason,
        )
        atomic_write(run_dir / "result.json", json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n")
        if status == "PASS":
            self.coverage[cache_key] = result
            self.new_oracle_runs[unit.assertion_file] = self.new_oracle_runs.get(unit.assertion_file, 0) + 1
            self._flush_coverage()
        return result

    @staticmethod
    def parse_counts(output: str) -> dict[str, int] | None:
        match = re.search(
            r"^RQ2_MUTATION_COUNTS\s+checked=(\d+)\s+total=(\d+)\s+unchecked=(\d+)\s+"
            r"undetectable=(\d+)\s+unprocessed=(\d+)\s+excluded=(\d+)\s*$",
            output, re.MULTILINE,
        )
        if not match:
            return None
        keys = ["checked", "total", "unchecked", "undetectable", "unprocessed", "excluded"]
        return dict(zip(keys, map(int, match.groups())))

    @staticmethod
    def parse_named_count(output: str, token: str) -> int:
        match = re.search(rf"^{re.escape(token)}\s+count=(\d+)\s*$", output, re.MULTILINE)
        return int(match.group(1)) if match else -1

    @staticmethod
    def parse_proof_counts(output: str) -> tuple[int, int, int]:
        match = re.search(r"^RQ2_PROOF_COUNTS\s+total=(\d+)\s+proven=(\d+)\s+bad=(\d+)\s*$", output, re.MULTILINE)
        return tuple(map(int, match.groups())) if match else (-1, -1, -1)

    @staticmethod
    def require_pass(result: CoverageResult) -> CoverageResult:
        if result.status != "PASS":
            raise RQ2Error(
                f"Jasper strict mutation run failed: {result.assertion_file}, subset={result.subset_key}, "
                f"status={result.status}, reason={result.reason}, log={result.log_file}"
            )
        return result

    def run_integrity(self) -> list[CoverageResult]:
        if not self.units:
            self.inventory()
        results: list[CoverageResult] = []
        for index, unit in enumerate(self.units, start=1):
            print(
                f"[PROGRESS] integrity file={index}/{len(self.units)} "
                f"assertion_file={unit.assertion_file} AQ={unit.aq_count}",
                flush=True,
            )
            ids = {b.assertion_id for b in unit.blocks}
            result = self.measure(unit, ids)
            if self.dry_run:
                results.append(result)
                continue
            self.require_pass(result)
            results.append(result)
            native_denominator = result.checked + result.unchecked
            native_text = (
                f"{100.0 * result.checked / native_denominator:.2f}% "
                f"({result.checked}/{native_denominator})"
                if native_denominator else "N/A (0/0)"
            )
            print(
                f"Integrity {unit.assertion_file}: {100.0 * result.checked / result.eligible:.2f}% "
                f"({result.checked}/{result.eligible}); Jasper-native eligible ratio="
                f"{native_text}; raw_total={result.total}; "
                f"undetectable={result.undetectable}; excluded={result.excluded}"
            , flush=True)
        write_csv_json(self.output / "integrity_runs", [r.as_dict() for r in results])
        if not self.dry_run:
            self._print_integrity_summary(results)
        return results

    def _print_integrity_summary(self, results: Sequence[CoverageResult]) -> None:
        if not results:
            return
        print("\nPART-B INTEGRITY SUMMARY")
        for result in results:
            integrity = 100.0 * result.checked / result.eligible
            native_denominator = result.checked + result.unchecked
            native = 100.0 * result.checked / native_denominator if native_denominator else 0.0
            print(
                f"  FILE {result.assertion_file}: Integrity={integrity:.2f}% "
                f"({result.checked}/{result.eligible}); Jasper-native eligible ratio={native:.2f}% "
                f"({result.checked}/{native_denominator}); undetectable={result.undetectable}; "
                f"excluded={result.excluded}"
            )
        macro = sum(result.checked / result.eligible for result in results) / len(results)
        print(f"  MACRO AVERAGE: Integrity={100.0 * macro:.2f}%")
        unit_by_file = {unit.assertion_file: unit for unit in self.units}
        tops = [unit_by_file[result.assertion_file].top for result in results]
        duplicate_tops = [top for top, count in Counter(tops).items() if count > 1]
        if not duplicate_tops:
            checked = sum(result.checked for result in results)
            eligible = sum(result.eligible for result in results)
            native_denominator = sum(result.checked + result.unchecked for result in results)
            native = checked / native_denominator if native_denominator else 0.0
            print(
                f"  MICRO WEIGHTED: Integrity={100.0 * checked / eligible:.2f}% "
                f"({checked}/{eligible}); Jasper-native eligible ratio={100.0 * native:.2f}% "
                f"({checked}/{native_denominator})"
            )
        else:
            print(
                "  MICRO WEIGHTED: not reported because mutation universes share tops: "
                + ", ".join(duplicate_tops)
            )
        print(f"  Results: {self.output / 'integrity_runs.csv'}", flush=True)

    def _coverage_checked(self, unit: Unit, ids: set[str], baseline: CoverageResult) -> int:
        if not ids:
            return 0
        result = self.require_pass(self.measure(unit, ids))
        stable = (
            result.eligible == baseline.eligible
            and result.total == baseline.total
            and result.excluded == baseline.excluded
            and result.unprocessed == baseline.unprocessed
        )
        if not stable:
            raise RQ2Error(
                f"{unit.assertion_file}: mutation universe changed for subset {result.subset_key}; "
                f"baseline F/total/excluded/unprocessed="
                f"{baseline.eligible}/{baseline.total}/{baseline.excluded}/{baseline.unprocessed}, "
                f"subset={result.eligible}/{result.total}/{result.excluded}/{result.unprocessed}. "
                "The checker-relative undetectable count may change and is not part of this stability check."
            )
        if result.checked > baseline.checked:
            raise RQ2Error(
                f"{unit.assertion_file}: non-monotonic oracle: subset checked={result.checked} > full AQ={baseline.checked}"
            )
        return result.checked

    def _backward_reduce(self, unit: Unit, order: list[str], target: int, baseline: CoverageResult, label: str) -> set[str]:
        print(f"  BACKWARD PASS {label}: start={len(order)}")
        current = set(order)
        for assertion_id in order:
            trial = current - {assertion_id}
            trial_checked = self._coverage_checked(unit, trial, baseline)
            if trial_checked == target:
                current = trial
                print(f"    redundant: {assertion_id}; remaining={len(current)}")
            else:
                print(
                    f"    required: {assertion_id}; detected={trial_checked}/{target}; "
                    f"remaining={len(current)}"
                )
        print(f"  BACKWARD PASS {label}: candidate={len(current)}")
        return current

    def optimize_unit(self, unit: Unit) -> OptimizationResult:
        cfg = self.optimization_config()
        all_ids = [b.assertion_id for b in unit.blocks]
        baseline = self.require_pass(self.measure(unit, set(all_ids)))
        target = baseline.checked
        started = time.monotonic()
        before_runs = self.new_oracle_runs.get(unit.assertion_file, 0)
        if target == 0:
            return OptimizationResult(
                unit.assertion_file, unit.unit_id, unit.top, unit.source_sha256, self.experiment_hash,
                unit.a0_count, unit.aq_count, 0, baseline.eligible, [], 0, True,
                "EXACT_EMPTY", "AQ detects zero items in the fixed mutation universe", 0,
                round(time.monotonic() - started, 6),
            )
        orders: list[tuple[str, list[str]]] = [("forward", list(all_ids))]
        if int(cfg.get("backward_orders", 2)) >= 2:
            orders.append(("reverse", list(reversed(all_ids))))
        for index in range(max(0, int(cfg.get("backward_orders", 2)) - 2)):
            order = list(all_ids)
            random.Random(stable_hash([unit.assertion_file, index])).shuffle(order)
            orders.append((f"shuffle_{index + 1}", order))
        candidates = [self._backward_reduce(unit, order, target, baseline, label) for label, order in orders]
        best = min(candidates, key=lambda value: (len(value), sorted(value)))
        exact = False
        method = "STRICT_JASPER_BACKWARD_INCLUSION_MINIMAL"
        stop_reason = "exact search disabled"
        if str(cfg.get("mode", "exact_then_fallback")) == "exact_then_fallback":
            max_assertions = int(cfg.get("exact_max_assertions", 60))
            max_runs = int(cfg.get("max_subset_runs_per_file", 2000))
            deadline = started + float(cfg.get("exact_timeout_seconds", 604800))
            if len(all_ids) > max_assertions:
                stop_reason = f"AQ size {len(all_ids)} exceeds exact_max_assertions={max_assertions}"
            else:
                singleton: dict[str, int] = {}
                aborted = False
                print("  SINGLETON AUDIT")
                for assertion_id in all_ids:
                    if time.monotonic() > deadline or self.new_oracle_runs.get(unit.assertion_file, 0) - before_runs >= max_runs:
                        aborted = True
                        stop_reason = "budget exhausted during singleton audit"
                        break
                    singleton[assertion_id] = self._coverage_checked(unit, {assertion_id}, baseline)
                write_csv_json(
                    self.output / "singletons" / unit.unit_id,
                    [
                        {"assertion_file": unit.assertion_file, "assertion_id": aid, "checked_alone": score,
                         "baseline_checked": target, "alone_preserves_full_score": score == target}
                        for aid, score in sorted(singleton.items())
                    ],
                )
                if not aborted:
                    maximum = max(singleton.values(), default=0)
                    lower = math.ceil(target / maximum) if maximum else len(all_ids)
                    ordered = sorted(all_ids, key=lambda x: (-singleton[x], x))
                    exact = True
                    for size in range(lower, len(best)):
                        found: set[str] | None = None
                        print(f"  EXACT CARDINALITY {size}")
                        for combo in itertools.combinations(ordered, size):
                            if sum(singleton[x] for x in combo) < target:
                                continue
                            if time.monotonic() > deadline or self.new_oracle_runs.get(unit.assertion_file, 0) - before_runs >= max_runs:
                                exact = False
                                stop_reason = "exact search time/run budget exhausted"
                                break
                            subset = set(combo)
                            if self._coverage_checked(unit, subset, baseline) == target:
                                found = subset
                                break
                        if not exact:
                            break
                        if found is not None:
                            best = found
                            stop_reason = f"first full-score subset found at cardinality {size}"
                            break
                    if exact:
                        method = "EXACT_STRICT_JASPER_MONOTONIC_ORACLE"
                        if not stop_reason.startswith("first full"):
                            stop_reason = "lower bound and all smaller cardinalities exhausted"
        final_score = self._coverage_checked(unit, set(best), baseline)
        if final_score != target:
            raise RQ2Error(f"{unit.assertion_file}: final A* validation changed score {final_score} != {target}")
        return OptimizationResult(
            assertion_file=unit.assertion_file, unit_id=unit.unit_id, top=unit.top,
            source_sha256=unit.source_sha256, experiment_hash=self.experiment_hash,
            a0_count=unit.a0_count, aq_count=unit.aq_count, target_checked=target,
            eligible_faults=baseline.eligible, selected_ids=sorted(best), selected_count=len(best),
            exact_minimum=exact, method=method, stop_reason=stop_reason,
            oracle_runs_used=self.new_oracle_runs.get(unit.assertion_file, 0) - before_runs,
            duration_seconds=round(time.monotonic() - started, 6),
        )

    def run_compactness(self) -> list[OptimizationResult]:
        if not self.units:
            self.inventory()
        if self.dry_run:
            eprint("PART-B WARNING: dry-run generates only full-AQ cases; compactness is not executed")
            self.run_integrity()
            return []
        results: list[OptimizationResult] = []
        for index, unit in enumerate(self.units, start=1):
            old = self.optimizations.get(unit.assertion_file)
            if (
                old and not self.force and old.exact_minimum and old.aq_count == unit.aq_count
                and old.source_sha256 == unit.source_sha256 and old.experiment_hash == self.experiment_hash
                and old.top == unit.top
            ):
                print(f"Compactness resumed: {unit.assertion_file}")
                results.append(old)
                continue
            print(
                f"[PROGRESS] compactness file={index}/{len(self.units)} "
                f"assertion_file={unit.assertion_file} AQ={unit.aq_count}",
                flush=True,
            )
            result = self.optimize_unit(unit)
            self.optimizations[unit.assertion_file] = result
            self._flush_optimizations()
            results.append(result)
            label = "EXACT" if result.exact_minimum else "CANDIDATE"
            print(
                f"  {label}: selected={result.selected_count}/{result.aq_count}; "
                f"aq_retention={100.0 * result.selected_count / result.aq_count:.2f}%; "
                f"redundancy={100.0 * (1.0 - result.selected_count / result.aq_count):.2f}%"
            )
        return results

    def _write_minimal_subset_artifacts(self, partial: bool) -> None:
        rows: list[dict[str, Any]] = []
        for unit in self.units:
            opt = self.optimizations.get(unit.assertion_file)
            if not opt or opt.source_sha256 != unit.source_sha256 or opt.experiment_hash != self.experiment_hash:
                continue
            block_by_id = {b.assertion_id: b for b in unit.blocks}
            subset_dir = "A_star" if opt.exact_minimum else "A_candidate"
            subset_source = self.output / "final" / subset_dir / unit.assertion_file
            atomic_write(subset_source, render_subset(unit.source, unit.blocks, set(opt.selected_ids)))
            for assertion_id in opt.selected_ids:
                block = block_by_id[assertion_id]
                rows.append(
                    {"assertion_file": unit.assertion_file, "top": unit.top,
                     "assertion_id": assertion_id, "property_name": block.property_name,
                     "start_line_in_aq": block.start_line, "exact_minimum": opt.exact_minimum,
                     "subset_source": self.relative(subset_source)}
                )
        write_csv_json(self.output / ("minimal_subset_partial" if partial else "minimal_subset"), rows)

    @staticmethod
    def file_metric_row(unit: Unit, baseline: CoverageResult, opt: OptimizationResult) -> dict[str, Any]:
        return {
            "scope_type": "FILE", "scope": unit.assertion_file, "top": unit.top,
            "a0_count": unit.a0_count, "aq_count": unit.aq_count,
            "qualification_yield": unit.qualification_yield,
            "qualification_yield_percent": round(100.0 * unit.qualification_yield, 4),
            "detected_faults": baseline.checked, "eligible_faults": baseline.eligible,
            "jasper_total": baseline.total, "undetectable": baseline.undetectable,
            "excluded": baseline.excluded, "integrity": round(baseline.checked / baseline.eligible, 6),
            "integrity_percent": round(100.0 * baseline.checked / baseline.eligible, 4),
            "jasper_native_eligible": baseline.checked + baseline.unchecked,
            "jasper_native_eligible_ratio": (
                round(baseline.checked / (baseline.checked + baseline.unchecked), 6)
                if baseline.checked + baseline.unchecked else ""
            ),
            "jasper_native_eligible_ratio_percent": (
                round(100.0 * baseline.checked / (baseline.checked + baseline.unchecked), 4)
                if baseline.checked + baseline.unchecked else ""
            ),
            "raw_checked_over_total": round(baseline.checked / baseline.total, 6) if baseline.total else "",
            "raw_checked_over_total_percent": round(100.0 * baseline.checked / baseline.total, 4) if baseline.total else "",
            "selected_count": opt.selected_count,
            "aq_retention_ratio": round(opt.selected_count / unit.aq_count, 6),
            "aq_retention_percent": round(100.0 * opt.selected_count / unit.aq_count, 4),
            "redundancy": round(1.0 - opt.selected_count / unit.aq_count, 6),
            "redundancy_percent": round(100.0 * (1.0 - opt.selected_count / unit.aq_count), 4),
            "end_to_end_ratio_optional": round(opt.selected_count / unit.a0_count, 6),
            "end_to_end_percent_optional": round(100.0 * opt.selected_count / unit.a0_count, 4),
            "a_star_proven_minimum": opt.exact_minimum, "method": opt.method,
            "stop_reason": opt.stop_reason,
        }

    def summary(self) -> list[dict[str, Any]]:
        if not self.units:
            self.inventory()
        file_rows: list[dict[str, Any]] = []
        baselines: list[CoverageResult] = []
        opts: list[OptimizationResult] = []
        for unit in self.units:
            ids = {b.assertion_id for b in unit.blocks}
            baseline = self.coverage.get((unit.assertion_file, self.subset_key(ids)))
            opt = self.optimizations.get(unit.assertion_file)
            if (
                not baseline or not opt or baseline.source_sha256 != unit.source_sha256
                or baseline.experiment_hash != self.experiment_hash
                or opt.source_sha256 != unit.source_sha256 or opt.experiment_hash != self.experiment_hash
            ):
                raise RQ2Error(f"{unit.assertion_file}: current Integrity/Compactness results are missing; run all")
            baselines.append(baseline)
            opts.append(opt)
            file_rows.append(self.file_metric_row(unit, baseline, opt))
        numeric = lambda key: [float(row[key]) for row in file_rows]
        native_ratios = [
            float(row["jasper_native_eligible_ratio"])
            for row in file_rows
            if row["jasper_native_eligible_ratio"] != ""
        ]
        native_ratio_percents = [
            float(row["jasper_native_eligible_ratio_percent"])
            for row in file_rows
            if row["jasper_native_eligible_ratio_percent"] != ""
        ]
        macro = {
            "scope_type": "MACRO_AVERAGE", "scope": "MEAN_OF_FILES", "top": "",
            "a0_count": "", "aq_count": "",
            "qualification_yield": round(sum(numeric("qualification_yield")) / len(file_rows), 6),
            "qualification_yield_percent": round(sum(numeric("qualification_yield_percent")) / len(file_rows), 4),
            "detected_faults": "", "eligible_faults": "", "jasper_total": "",
            "undetectable": "", "excluded": "",
            "integrity": round(sum(numeric("integrity")) / len(file_rows), 6),
            "integrity_percent": round(sum(numeric("integrity_percent")) / len(file_rows), 4),
            "jasper_native_eligible": "",
            "jasper_native_eligible_ratio": round(sum(native_ratios) / len(native_ratios), 6) if native_ratios else "",
            "jasper_native_eligible_ratio_percent": round(sum(native_ratio_percents) / len(native_ratio_percents), 4) if native_ratio_percents else "",
            "raw_checked_over_total": round(sum(numeric("raw_checked_over_total")) / len(file_rows), 6),
            "raw_checked_over_total_percent": round(sum(numeric("raw_checked_over_total_percent")) / len(file_rows), 4),
            "selected_count": "",
            "aq_retention_ratio": round(sum(numeric("aq_retention_ratio")) / len(file_rows), 6),
            "aq_retention_percent": round(sum(numeric("aq_retention_percent")) / len(file_rows), 4),
            "redundancy": round(sum(numeric("redundancy")) / len(file_rows), 6),
            "redundancy_percent": round(sum(numeric("redundancy_percent")) / len(file_rows), 4),
            "end_to_end_ratio_optional": round(sum(numeric("end_to_end_ratio_optional")) / len(file_rows), 6),
            "end_to_end_percent_optional": round(sum(numeric("end_to_end_percent_optional")) / len(file_rows), 4),
            "a_star_proven_minimum": all(x.exact_minimum for x in opts),
            "method": "UNWEIGHTED_MEAN_OF_FILE_METRICS", "stop_reason": "",
        }
        total_a0 = sum(u.a0_count for u in self.units)
        total_aq = sum(u.aq_count for u in self.units)
        total_checked = sum(x.checked for x in baselines)
        total_eligible = sum(x.eligible for x in baselines)
        total_native_eligible = sum(x.checked + x.unchecked for x in baselines)
        total_selected = sum(x.selected_count for x in opts)
        duplicate_tops = [top for top, count in Counter(u.top for u in self.units).items() if count > 1]
        disjoint = not duplicate_tops
        micro_integrity = total_checked / total_eligible if disjoint and total_eligible else None
        micro = {
            "scope_type": "MICRO_WEIGHTED", "scope": "SUM_OF_DISJOINT_FILE_UNIVERSES", "top": "",
            "a0_count": total_a0, "aq_count": total_aq,
            "qualification_yield": round(total_aq / total_a0, 6),
            "qualification_yield_percent": round(100.0 * total_aq / total_a0, 4),
            "detected_faults": total_checked if disjoint else "",
            "eligible_faults": total_eligible if disjoint else "", "jasper_total": sum(x.total for x in baselines),
            "undetectable": sum(x.undetectable for x in baselines), "excluded": sum(x.excluded for x in baselines),
            "integrity": round(micro_integrity, 6) if micro_integrity is not None else "",
            "integrity_percent": round(100.0 * micro_integrity, 4) if micro_integrity is not None else "",
            "jasper_native_eligible": total_native_eligible if disjoint else "",
            "jasper_native_eligible_ratio": round(total_checked / total_native_eligible, 6) if disjoint and total_native_eligible else "",
            "jasper_native_eligible_ratio_percent": round(100.0 * total_checked / total_native_eligible, 4) if disjoint and total_native_eligible else "",
            "raw_checked_over_total": round(total_checked / sum(x.total for x in baselines), 6) if disjoint and sum(x.total for x in baselines) else "",
            "raw_checked_over_total_percent": round(100.0 * total_checked / sum(x.total for x in baselines), 4) if disjoint and sum(x.total for x in baselines) else "",
            "selected_count": total_selected,
            "aq_retention_ratio": round(total_selected / total_aq, 6),
            "aq_retention_percent": round(100.0 * total_selected / total_aq, 4),
            "redundancy": round(1.0 - total_selected / total_aq, 6),
            "redundancy_percent": round(100.0 * (1.0 - total_selected / total_aq), 4),
            "end_to_end_ratio_optional": round(total_selected / total_a0, 6),
            "end_to_end_percent_optional": round(100.0 * total_selected / total_a0, 4),
            "a_star_proven_minimum": all(x.exact_minimum for x in opts),
            "method": "WEIGHTED_SUM; mutation universes disjoint by unique top" if disjoint else "INVALID_INTEGRITY_DUPLICATE_TOPS",
            "stop_reason": "" if disjoint else "duplicate tops: " + ", ".join(duplicate_tops),
        }
        rows = file_rows + [macro, micro]
        write_csv_json(self.output / "rq2_final_metrics", rows)
        self._write_minimal_subset_artifacts(partial=False)
        print("\nPART-B SET-LEVEL RESULTS")
        for row in file_rows:
            subset_status = "EXACT" if row["a_star_proven_minimum"] else "CANDIDATE"
            print(
                f"  FILE {row['scope']}: Integrity={row['integrity_percent']}% "
                f"({row['detected_faults']}/{row['eligible_faults']}), "
                f"Jasper-native eligible ratio={row['jasper_native_eligible_ratio_percent']}% "
                f"({row['detected_faults']}/{row['jasper_native_eligible']}), "
                f"AQ Retention={row['aq_retention_percent']}% "
                f"({row['selected_count']}/{row['aq_count']}), "
                f"Redundancy={row['redundancy_percent']}%, "
                f"End-to-end(optional)={row['end_to_end_percent_optional']}%, "
                f"subset={subset_status}"
            )
        print(
            f"  MACRO AVERAGE: Integrity={macro['integrity_percent']}%, "
            f"AQ Retention={macro['aq_retention_percent']}%, "
            f"Redundancy={macro['redundancy_percent']}%, "
            f"End-to-end(optional)={macro['end_to_end_percent_optional']}%"
        )
        if disjoint:
            print(
                f"  MICRO WEIGHTED: Integrity={micro['integrity_percent']}% "
                f"({micro['detected_faults']}/{micro['eligible_faults']}), "
                f"AQ Retention={micro['aq_retention_percent']}% "
                f"({micro['selected_count']}/{micro['aq_count']}), "
                f"Redundancy={micro['redundancy_percent']}%, "
                f"End-to-end(optional)={micro['end_to_end_percent_optional']}%"
            )
        else:
            print(f"  MICRO WEIGHTED Integrity not reported: duplicate tops {duplicate_tops}")
        exact_count = sum(1 for opt in opts if opt.exact_minimum)
        print(
            f"  SUBSET CERTIFICATION: exact={exact_count}/{len(opts)}; "
            f"candidate={len(opts) - exact_count}/{len(opts)}"
        )
        print("\nINTERPRETATION")
        print("  Integrity is mutation-model-relative fault detection coverage.")
        print("  Lower AQ Retention means more redundancy only for this mutation universe.")
        print("  CANDIDATE subsets are validated reductions but are not proven globally minimum.")
        print(f"  Results: {self.output / 'rq2_final_metrics.csv'}")
        print(f"  Selected assertions: {self.output / 'minimal_subset.csv'}", flush=True)
        return rows

    def run_all(self) -> None:
        self.inventory()
        self.run_integrity()
        if not self.dry_run:
            self.run_compactness()
            self.summary()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Portable Jasper Part-B Integrity and AQ Compactness")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--aq-config", type=Path, help="AQ configuration; auto-detected by default")
    parser.add_argument(
        "--spec-results",
        type=Path,
        help="Optional CSV/JSON spec-check results; PASS and UNRESOLVED rows define the post-spec AQ",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--include", action="append", help="assertion_file glob; repeatable")
    parser.add_argument("command", choices=["inventory", "integrity", "compactness", "summary", "all"])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pipeline = Pipeline(args.config, args)
        if args.command == "inventory":
            pipeline.inventory()
        elif args.command == "integrity":
            pipeline.inventory(); pipeline.run_integrity()
        elif args.command == "compactness":
            pipeline.inventory(); pipeline.run_compactness(); pipeline.summary()
        elif args.command == "summary":
            pipeline.inventory(); pipeline.summary()
        else:
            pipeline.run_all()
        print(f"Complete: {args.command}; output directory: {pipeline.output}")
        return 0
    except RQ2Error as exc:
        eprint(f"RQ2 ERROR: {exc}")
        return 2
    except KeyboardInterrupt:
        eprint("PART-B: interrupted; completed subset cache and partial artifacts were preserved")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
