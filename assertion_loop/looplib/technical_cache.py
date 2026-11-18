"""Reuse definitive Step A results for an identical target and formal setup.

The key contains the exact single-target checker, including its original
procedural/generate position. It never equates expressions in different scopes
or hashes the unrendered bodies of unrelated source assertions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .bootstrap import aq, target_scope
from .common import ROOT, resolve
from .properties import render


SCHEMA = 1
DEFINITIVE = {"syntax": {"PASS", "FAIL"}, "fpv": {"PROVEN", "CEX"},
              "vacuity": {"PASS", "NOT_APPLICABLE", "VACUOUS"}}
PASSING = {"syntax": {"PASS"}, "fpv": {"PROVEN"}, "vacuity": {"PASS", "NOT_APPLICABLE"}}
ALGORITHM_FILES = (
    ROOT / "looplib" / "technical_cache.py", ROOT / "looplib" / "technical.py",
    ROOT / "looplib" / "properties.py", ROOT / "looplib" / "inputs.py",
    ROOT / "looplib" / "bootstrap.py", ROOT / "looplib" / "common.py",
    ROOT / "vendor" / "aq" / "assertion_aq_pipeline.py",
    ROOT / "vendor" / "aq" / "aq_jasper_generic.tcl",
    ROOT / "vendor" / "aq" / "process_watch.py", ROOT / "examples" / "aq_base.json",
    ROOT / "vendor" / "aq" / "jasper_reset.py",
)
_COMMENTS_OR_STRING = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*.*?\*/', re.S)
_INCLUDE = re.compile(r'`include\b[^\r\n]*')
_LOCATION = re.compile(r'`(?:__FILE__|__DATE__|__TIME__)\b')
_FILE_IO = re.compile(r'\$(?:readmem[hb]|writemem[hb]|f(?:open|close|read|write[bhod]?|display[bhod]?|strobe[bhod]?|monitor[bhod]?|flush|scanf|gets|getc|putc|seek|tell|eof|error)|ungetc|rewind)\b')
_SETUP_ERROR = re.compile(r'^(?:AQ_(?:TARGET_ERROR|ENVIRONMENT_ERROR|STAGE_ERROR)\b|AQ_COMPILE_ERROR[^\r\n]*AQ_SETUP_ERROR:)', re.M)
_INFRA_ERROR = re.compile(
    r'license|licensing|no such file|file not found|cannot open|can.t open|permission denied|executable not found|'
    r'out[ -]of[ -]memory|\boom\b|memory (?:exhausted|allocation|limit)|insufficient memory|cannot allocate|bad_alloc|'
    r'connection|socket|network|broken pipe|communication|server[^\r\n]*(?:lost|unavailable|failure|error)|'
    r'timeout|timed out|internal(?:\s+\w+){0,2}\s+error|segmentation|crash|killed|interrupted|'
    r'resource(?:s)?\s+(?:exhausted|unavailable|error|limit|failure)', re.I)
_SOURCE_SYNTAX_ERROR = re.compile(
    r'\b(?:syntax|parse|parsing)\s+error\b|\bunexpected\s+(?:token|symbol|character|identifier)\b|'
    r'\b(?:undeclared|undefined|unresolved|unknown)\s+identifier\b|\bnot\s+declared\b|'
    r'\bidentifier\b[^\r\n]*(?:undeclared|undefined|unresolved)|'
    r'\b(?:unbalanced|unmatched|mismatched)\s+(?:delimiter|parenthes\w*|bracket|brace)\b', re.I)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _without_comments(source):
    return _COMMENTS_OR_STRING.sub(lambda match: match[0] if match[0].startswith('"') else
                                   re.sub(r'[^\r\n]', ' ', match[0]), source)


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


class TechnicalCache:
    """Content-addressed technical decisions, shared across models and repeats.

    A missing/unfingerprintable dependency or corrupt cache is a miss. Actual
    qualification remains responsible for reporting configuration/tool errors.
    """

    def __init__(self, dataset, unit, settings, stage):
        if stage not in DEFINITIVE:
            raise ValueError(f"Unknown technical cache stage: {stage}")
        self.dataset = Path(dataset).resolve()
        self.unit = unit
        self.settings = settings
        self.stage = stage
        self.root = self.dataset / "gen_assertions" / "_shared" / "step_a" / stage
        self.bypass_reason = ""
        self._observed = {}
        self._compatible_keys = {}

    def _logical(self, path):
        path = Path(path).resolve()
        try:
            return path.relative_to(self.dataset).as_posix()
        except ValueError:
            return str(path)

    def _file(self, path):
        path = Path(path).resolve()
        data = path.read_bytes()
        if _LOCATION.search(data.decode("utf-8", errors="replace")):
            # __FILE__ expands to generated output paths, which are deliberately
            # absent from this cross-output cache API. Do not guess its value.
            raise ValueError("Location/time-sensitive preprocessor construct")
        return {"path": self._logical(path), "sha256": hashlib.sha256(data).hexdigest()}

    def _tool(self, executable, path):
        path = Path(path).resolve()
        stat = path.stat()
        parents = []
        # Jasper's launcher can remain byte-identical across an installation
        # update. Record executable and adjacent installation metadata too.
        # Do not fingerprint a dataset/temp root: experiment output creation is
        # unrelated to the installed tool and must not invalidate a result.
        for directory in (path.parent, path.parent.parent):
            if directory == self.dataset or directory == Path(tempfile.gettempdir()).resolve() or directory == directory.parent:
                continue
            metadata = directory.stat()
            parents.append({"path": str(directory), "mtime_ns": metadata.st_mtime_ns,
                            "ctime_ns": metadata.st_ctime_ns})
        return {"executable": executable, "resolved": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns,
                "installation": parents,
                "version_tag": self.settings.get("jasper_version_tag", "user-not-set")}

    def _dependencies(self, rendered, roots, includes):
        # The actual gate writes this basename in a fresh input/ directory.
        # Resolve assertion includes through the configured include paths, not
        # through the source assertions/ directory which is never compiled here.
        virtual = self.root.parent / "_rendered" / (self.unit["name"] + "_assertions.sv")
        pending = [(path, path.read_text(encoding="utf-8", errors="replace")) for path in roots]
        pending.append((virtual, rendered))
        seen = set()
        dependencies = {}
        while pending:
            path, source = pending.pop()
            clean = _without_comments(source)
            if _LOCATION.search(clean):
                raise ValueError("Location/time-sensitive preprocessor construct")
            if _FILE_IO.search(clean):
                # File names can be computed at runtime. Even explicit setup
                # dependencies cannot establish which external bytes are read.
                raise ValueError("External file I/O cannot be fingerprinted safely")
            for directive in _INCLUDE.finditer(clean):
                match = re.fullmatch(r'`include\s+"([^"\r\n]+)"\s*', directive[0])
                if not match:
                    # Macro-expanded includes need compiler preprocessing to
                    # fingerprint safely. Leave this result unshared.
                    raise ValueError("Cannot fingerprint nonliteral include")
                name = match[1]
                candidates = [path.parent / name, *(directory / name for directory in includes)]
                dependency = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
                if dependency is None:
                    raise ValueError("Unresolved include dependency")
                if dependency in seen:
                    continue
                seen.add(dependency)
                dependencies[self._logical(dependency)] = self._file(dependency)
                pending.append((dependency, dependency.read_text(encoding="utf-8", errors="replace")))
        return [dependencies[name] for name in sorted(dependencies)]

    @staticmethod
    def _canonical_source(source, label, dependency_sources):
        """Normalize only a demonstrably unreferenced display label.

        The executed source is unchanged. All wrappers, whitespace, declaration
        positions and every other token remain part of the exact cache key.
        """
        masked, errors = aq.mask_comments_and_strings(source)
        blocks, extraction_errors = aq.extract_assertions(source)
        if errors or extraction_errors or len(blocks) != 1:
            return source, label
        block = blocks[0]
        if not block.named or block.property_name != label:
            return source, label
        declaration = re.match(r'([A-Za-z_]\w*)\s*:', masked[block.start:])
        if declaration is None or declaration[1] != label:
            return source, label
        start = block.start + declaration.start(1)
        stop = block.start + declaration.end(1)
        rest = masked[:start] + (" " * (stop - start)) + masked[stop:]
        token = re.compile(r'(?<![\w$])' + re.escape(label) + r'(?![\w$])')
        unsafe = re.compile(r'\$assert\w*|`(?:__FILE__|__LINE__|__DATE__|__TIME__)\b')
        if token.search(rest) or unsafe.search(rest):
            return source, label
        for dependency in dependency_sources:
            clean, dependency_errors = aq.mask_comments_and_strings(dependency)
            if dependency_errors or token.search(clean) or unsafe.search(clean):
                return source, label
        canonical = "LOOP_CACHE_TARGET"
        return source[:start] + canonical + source[stop:], canonical

    def _key(self, prop):
        self.bypass_reason = ""
        try:
            source = render(self.dataset, self.unit, [prop])
            if _INCLUDE.search(_without_comments(source)):
                # This API deliberately omits model/output directories. A local
                # header in the actual generated input directory could shadow
                # include_dirs, so a checker include cannot share a proof here.
                raise ValueError("Checker includes require output-directory-dependent resolution")
            includes = [resolve(self.dataset, value) for value in self.unit.get("include_dirs", [])]
            if any(not directory.is_dir() for directory in includes):
                raise ValueError("Configured include directory is missing")
            rtl = [resolve(self.dataset, value) for value in
                   self.unit["rtl_files"] + self.unit.get("environment_files", [])]
            binds = [resolve(self.dataset, value) for value in self.unit.get("bind_files", [])]
            original_sources = {resolve(self.dataset, value) for value in self.unit.get("assertion_files", [])}
            original_sources.add(resolve(self.dataset, self.unit["template"]))
            # Fixed dependencies are explicit user setup inputs. Original source
            # checkers are represented by the rendered target, not all siblings.
            extras = [resolve(self.dataset, value) for value in self.unit.get("fixed_dependencies", [])
                      if resolve(self.dataset, value) not in original_sources]
            roots = list(dict.fromkeys([*rtl, *binds, *extras]))
            executable = str(self.settings["jasper_executable"])
            tool = shutil.which(executable)
            if tool is None or not Path(tool).is_file():
                raise ValueError("Jasper executable cannot be fingerprinted")
            timeout = self.settings["syntax_timeout_seconds" if self.stage == "syntax" else "proof_timeout_seconds"]
            dependencies = self._dependencies(source, roots, includes)
            dependency_paths = list(dict.fromkeys([*roots, *(resolve(self.dataset, value["path"]) for value in dependencies)]))
            canonical_source, canonical_label = self._canonical_source(source, prop["label"],
                [path.read_text(encoding="utf-8", errors="replace") for path in dependency_paths])
            identity = {
                "schema": SCHEMA, "stage": self.stage,
                "target": {"label": canonical_label, "expression": prop["expression"]},
                "checker_source": canonical_source,
                "formal": {"top": self.unit["top"], "checker": self.unit["checker"],
                           "target_scope": target_scope(self.dataset, self.unit),
                           "clock_mode": self.unit["clock_mode"], "clock": self.unit.get("clock", ""),
                           "reset_expression": self.unit.get("reset_expression", ""),
                           "defines": self.unit.get("defines", []), "timeout_seconds": timeout,
                           "include_dirs": [self._logical(directory) for directory in includes]},
                "rtl": [self._file(path) for path in rtl], "bind": [self._file(path) for path in binds],
                "fixed_dependencies": [self._file(path) for path in extras],
                "includes": dependencies,
                "tool": self._tool(executable, tool),
                "algorithm": [{"name": str(path), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
                              for path in ALGORITHM_FILES],
            }
            key=_digest(identity)
            # These package-vetted revisions changed prompts / atomic writes,
            # not render(), target selection, formal qualification or parsing.
            manifest=json.loads((ROOT/"MANIFEST.json").read_text())
            compatible=[]
            snapshots=manifest.get("technical_cache_compatible_algorithms",[]) if all(path.is_relative_to(ROOT) for path in ALGORITHM_FILES) else []
            for snapshot in snapshots:
                names={path.relative_to(ROOT).as_posix() for path in ALGORITHM_FILES}
                missing=names-set(snapshot)
                if set(snapshot)-names:continue
                if missing:
                    # The new encoder leaves a single native pin's meaning
                    # unchanged. Composite reset evidence needs the new key.
                    if missing!={"vendor/aq/jasper_reset.py"} or '||' in self.unit.get('reset_expression',''):continue
                old_algorithm=[{"name":str(path),"sha256":snapshot[path.relative_to(ROOT).as_posix()]}
                               for path in ALGORITHM_FILES if path.relative_to(ROOT).as_posix() in snapshot]
                candidate=_digest({**identity,"algorithm":old_algorithm})
                if candidate!=key:compatible.append(candidate)
            self._compatible_keys[key]=compatible
            return key
        except (OSError, ValueError, KeyError, TypeError, UnicodeError) as error:
            self.bypass_reason = str(error)
            return None

    def _path(self, key):
        return self.root / key[:2] / (key + ".json")

    def _definitive(self, decision):
        if not isinstance(decision, dict) or decision.get("stage") != self.stage:
            return False
        status = decision.get("status")
        if status not in DEFINITIVE[self.stage] or type(decision.get("passed")) is not bool:
            return False
        if decision["passed"] != (status in PASSING[self.stage]):
            return False
        if any(decision.get(key) for key in ("setup_error", "target_error", "interrupted", "incomplete")):
            return False
        if any(key in decision and decision[key] is False for key in ("complete", "completed")):
            return False
        diagnostics = "\n".join(str(decision.get(key, "")) for key in ("reason", "diagnostic"))
        if _SETUP_ERROR.search(diagnostics) or "AQ_SETUP_ERROR:" in diagnostics:
            return False
        if status == "FAIL" and ("AQ_COMPILE_ERROR" not in diagnostics or _INFRA_ERROR.search(diagnostics)
                                  or not _SOURCE_SYNTAX_ERROR.search(diagnostics)):
            # A compiler exception, infrastructure error or unrecognized compile
            # failure is not evidence of a deterministic source syntax defect.
            return False
        return True

    def _load(self, key):
        try:
            value = json.loads(self._path(key).read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return None
            checksum = value.pop("payload_sha256", None)
            if checksum != _digest(value) or value.get("schema") != SCHEMA or value.get("key") != key:
                return None
            if value.get("stage") != self.stage or value.get("conflict") or not self._definitive(value.get("decision")):
                return None
            evidence = value.get("evidence")
            if not isinstance(evidence, dict) or not isinstance(evidence.get("path"), str) or not isinstance(evidence.get("origin_log"), str):
                return None
            evidence_path = (self.root / evidence["path"]).resolve()
            if not evidence_path.is_relative_to(self.root.resolve()):
                return None
            data = evidence_path.read_bytes()
            if not data or len(data) != evidence.get("bytes") or hashlib.sha256(data).hexdigest() != evidence.get("sha256"):
                return None
            value["_evidence_path"] = str(evidence_path)
            return value
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _target_identity(prop):
        # Runtime checks/basis can change without replacing the target. Include
        # its exact label/body and source slot, rather than an expression-only
        # identity or an object ID that would lose equivalent copied records.
        return _digest({key: prop.get(key) for key in
                        ("label", "expression", "slot_id", "source_label", "source_file", "source_line")})

    def lookup(self, prop):
        key = self._key(prop)
        self._observed[self._target_identity(prop)] = key
        value = self._load(key) if key is not None else None
        legacy_key=None
        if value is None and key is not None:
            for candidate in self._compatible_keys.get(key,[]):
                value=self._load(candidate)
                if value is not None:legacy_key=candidate;break
        if value is None:
            return None
        decision = copy.deepcopy(value["decision"])
        if legacy_key:decision.update(cache_compatible_revision=True,cache_legacy_key=legacy_key)
        source_label = decision.get("label")
        decision.update({"label": prop["label"], "expression": prop["expression"],
                         "cache_hit": True, "cache_key": key, "source_label": source_label,
                         "cache_source_label": source_label, "cache_source_log": value["_evidence_path"],
                         "cache_origin_log": value["evidence"]["origin_log"]})
        return decision

    def store(self, prop, decision):
        if not self._definitive(decision) or decision.get("label") != prop.get("label") or decision.get("expression") != prop.get("expression"):
            return
        key = self._key(prop)
        identity = self._target_identity(prop)
        if identity in self._observed and key != self._observed[identity]:
            # A proof belongs to the inputs observed before the subprocess,
            # never to a new dependency/tool/algorithm hash found after it.
            self.bypass_reason = "Formal inputs changed between lookup and result; result not shared"
            return
        if key is None:
            return
        try:
            if not isinstance(decision.get("log_file"), str) or not decision["log_file"]:
                self.bypass_reason = "Completed result has no log evidence to preserve"
                return
            origin = resolve(self.dataset, decision["log_file"])
            data = origin.read_bytes()
            if not data:
                self.bypass_reason = "Completed result has empty log evidence"
                return
            log_sha = hashlib.sha256(data).hexdigest()
            evidence = {"path": f"evidence/{log_sha[:2]}/{log_sha}.log", "sha256": log_sha,
                        "bytes": len(data), "origin_log": decision["log_file"]}
            evidence_path = self.root / evidence["path"]
            if not evidence_path.is_file() or evidence_path.read_bytes() != data:
                _atomic_write(evidence_path, data)
            value = {"schema": SCHEMA, "key": key, "stage": self.stage,
                     "decision": copy.deepcopy(decision), "evidence": evidence}
            value["payload_sha256"] = _digest(value)
            serialized = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            _atomic_write(self._path(key), serialized)
        except OSError:
            # A cache write is optional; a completed formal decision still stands.
            pass
