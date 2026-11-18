"""Content-addressed property results, independent of model/trial/output paths.

A property is shareable only inside the same checker scaffolding, formal
configuration, RTL/mutant, bind files and include dependency contents.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

SCHEMA = 1
PROTOCOL = "explicit_mutants_target_labels_v2"
STATUSES = {"CEX", "PROVEN", "UNKNOWN"}
_PARSER = None


def sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def signature(value) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")))


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def aq_parser():
    global _PARSER
    if _PARSER is None:
        path = Path(__file__).parent / "aq_source_parser.py"
        spec = importlib.util.spec_from_file_location("fh_aq_source_parser", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _PARSER = module
    return _PARSER


# Keep lexical tokens, including strings and multi-character operators. This
# ignores generated AQ-removal comments without equating a + +b with a++b.
_TOKEN = re.compile(r'''//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|\\[^\s]+|(?:<<<=|>>>=|&&&|->>|#-#|#=#|<<<|>>>|\|->|\|=>|===|!==|==\?|!=\?|<<=|>>=|\*\*|::|##|&&|\|\||==|!=|<=|>=|<<|>>|\+\+|--|\+=|-=|\*=|/=|%=|&=|\|=|\^=|~\^|\^~|~&|~\||->|=>|\+:|-:|\.\*)|[A-Za-z_$][A-Za-z0-9_$]*|[0-9][A-Za-z0-9_'?.]*|[^\s]''', re.S)


def tokens(source: str) -> list[str]:
    return [m.group() for m in _TOKEN.finditer(source)
            if not m.group().startswith(("//", "/*"))]


class Checker:
    def __init__(self, source: str, assertions: list[str], filename: str):
        self.source, self.assertions, self.filename = source, assertions, filename
        parser = aq_parser()
        blocks, errors = parser.extract_assertions(source)
        self.blocks = blocks
        names = [b.property_name for b in blocks]
        def simple(block):
            masked, _ = parser.mask_comments_and_strings(block.text)
            match = re.search(r"\bassert\s+(?:property\s*)?\(|\bassert\s*\(", masked)
            if not match:
                return False
            closing = parser.matching_delimiter(masked, match.end() - 1, "(", ")")
            if closing is None or masked[closing + 1:].strip() != ";":
                return False
            if block.assertion_kind == "immediate":
                expression = masked[match.end():closing]
                if set(tokens(expression)) & {"++", "--", "=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "<<=", ">>=", "<<<=", ">>>="}:
                    return False
                calls = set(re.findall(r"([A-Za-z_$][A-Za-z0-9_$]*)\s*\(", expression))
                pure_system = {"$isunknown", "$onehot", "$onehot0", "$countones", "$countbits",
                               "$signed", "$unsigned", "$bits", "$clog2", "$past", "$stable",
                               "$changed", "$rose", "$fell"}
                if calls - pure_system:
                    return False
            return True
        self.partial = (not errors and len(set(names)) == len(names)
                        and set(names) == set(assertions)
                        and all(b.named and b.assertion_kind in {"concurrent", "immediate"} and simple(b) for b in blocks)
                        and not re.search(r'`(?:__FILE__|__LINE__|include)\b|\$assert\w*', source))
        if self.partial:
            by_name = {b.property_name: b for b in blocks}
            parts, cursor, anchors = [], 0, {}
            for block in blocks:
                parts.append(source[cursor:block.start])
                anchors[block.property_name] = len(tokens("".join(parts)))
                cursor = block.end
            parts.append(source[cursor:])
            self.context = {"mode": "simple-assertion", "tokens": tokens("".join(parts))}
            def property_identity(name):
                block = by_name[name]
                masked, _ = parser.mask_comments_and_strings(block.text)
                start = re.search(r"\bassert\b", masked).start()
                # Display/property labels are not part of the checked formula.
                return signature([tokens(block.text[start:]), anchors[name]])
            self.keys = {name: property_identity(name) for name in assertions}
        else:
            # Unsupported syntax, effectful assertions and location-sensitive
            # constructs may reuse identical whole files, never partial subsets.
            self.context = {"mode": "whole-checker", "source": source,
                            "filename": (filename if re.search(r'`(?:__FILE__|__LINE__)\b', source) else Path(filename).name),
                            "assertions": sorted(assertions)}
            self.keys = {name: signature([self.context, name]) for name in assertions}
        self.context_sha256 = signature(self.context)
        self.aq_sha256 = signature([self.context_sha256, sorted(self.keys.items())])

    def subset(self, names: list[str]) -> str:
        if set(names) == set(self.assertions):
            return self.source
        if not self.partial:
            raise ValueError("this checker cannot safely isolate individual assertions")
        parts, cursor = [], 0
        for block in self.blocks:
            parts.append(self.source[cursor:block.start])
            parts.append(block.text if block.property_name in names else
                         ";" + "\n" * block.text.count("\n"))
            cursor = block.end
        parts.append(self.source[cursor:])
        return "".join(parts)


def logical(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        # External inputs are deliberately path-specific.
        return str(path.resolve())


def file_record(path: Path, root: Path) -> dict:
    data = path.read_bytes()
    result = {"name": logical(path, root), "sha256": sha(data)}
    if re.search(rb'`(?:__FILE__|__LINE__)\b', data):
        result["absolute_path"] = str(path.resolve())
    return result


def environment(config: dict, root: Path, checker: Checker | None = None,
                assertion_path: Path | None = None) -> dict:
    """Hash dependencies once per AQ; no model name or output directory key."""
    resolve = lambda x: (root / x).resolve()
    rtl = [resolve(x) for x in config["rtl_files"]]
    binds = [resolve(x) for x in config.get("bind_files", [])]
    includes = [resolve(x) for x in config.get("include_dirs", [])]
    headers = []
    for directory in includes:
        headers.append({"directory": logical(directory, root), "files": [
            file_record(p, root) for p in sorted(directory.rglob("*"))
            if p.is_file() and p.suffix in {".v", ".sv", ".vh", ".svh"}]})
    # Include directives can also resolve relative to a source file's parent.
    # Record these transitive dependencies, including nonstandard extensions.
    seen, dependencies = set(), []
    pending = [(p, p.read_text(errors="replace")) for p in [*rtl, *binds]]
    if assertion_path is not None and checker is not None:
        pending.append((assertion_path, checker.source))
    while pending:
        path, source = pending.pop()
        for line in source.splitlines():
            if not re.match(r'^\s*`include\b', line):
                continue
            match = re.match(r'^\s*`include\s+"([^"\n]+)"', line)
            if not match:
                raise ValueError(f"cannot fingerprint macro-expanded include in {path}: {line}")
            dependency = next((p for p in [path.parent / match[1],
                               *(d / match[1] for d in includes)] if p.is_file()), None)
            if dependency is None:
                raise ValueError(f"include dependency missing: {match[1]} in {path}")
            dependency = dependency.resolve()
            if dependency not in seen:
                seen.add(dependency)
                dependencies.append(file_record(dependency, root))
                pending.append((dependency, dependency.read_text(errors="replace")))
    executable = str(config.get("jasper_executable", "jg"))
    tool = shutil.which(executable)
    tool_identity = {"executable": executable,
                     "version_tag": config.get("jasper_version_tag", config.get("jasper_version", ""))}
    if tool and Path(tool).is_file():
        tool_identity["binary_sha256"] = sha(Path(tool).read_bytes())
    formal = {key: value for key, value in config.items() if key not in {
        "project_root", "rtl_files", "assertion_file", "assertions", "bind_files",
        "include_dirs", "syntax_timeout_seconds", "jasper_executable"}}
    formal.setdefault("clock_mode", "clocked")
    formal.setdefault("clock", "")
    formal.setdefault("reset_expression", "")
    # Old composite reset commands were rejected by Jasper. Their jobs must not
    # share proof/cache identity with the corrected multi-pin declaration.
    if '||' in str(formal['reset_expression']):
        formal['reset_encoding']='native_reset_pin_list_v1'
    formal.setdefault("defines", [])
    formal.setdefault("timeout_seconds", 14400)
    return {"schema": SCHEMA, "protocol": PROTOCOL, "formal": formal,
            "tool": tool_identity, "golden_rtl": [file_record(p, root) for p in rtl],
            "bind": [file_record(p, root) for p in binds], "include_dirs": headers,
            "dependencies": sorted(dependencies, key=lambda x: x["name"])}


def job_key(env: dict, checker: Checker, replacement_index: int, mutant: bytes,
            mutant_path: Path | None = None) -> str:
    location = None
    if re.search(rb'`(?:__FILE__|__LINE__)\b', mutant):
        if mutant_path is None:
            raise ValueError("location-sensitive mutant requires its source path")
        location = str(mutant_path.resolve())
    return signature({"environment": env, "checker_context": checker.context_sha256,
                      "replacement_index": replacement_index, "mutant_sha256": sha(mutant), "mutant_location": location})


class ResultCache:
    def __init__(self, root: Path):
        self.root = root

    def path(self, key: str) -> Path:
        return self.root / key[:2] / (key + ".json")

    def read(self, key: str) -> dict:
        path = self.path(key)
        if not path.is_file():
            return {}
        value = json.loads(path.read_text())
        if value.get("schema") != SCHEMA or value.get("context_sha256") != key:
            raise ValueError(f"shared cache metadata mismatch: {path}")
        properties = value.get("properties", {})
        if any(v.get("status") not in STATUSES for v in properties.values()):
            raise ValueError(f"invalid shared cache status: {path}")
        return properties

    def lookup(self, key: str, checker: Checker, retry_unknown: bool = False) -> dict[str, str]:
        records = self.read(key)
        return {name: records[identity]["status"] for name, identity in checker.keys.items()
                if identity in records and not (retry_unknown and records[identity]["status"] == "UNKNOWN")}

    @contextlib.contextmanager
    def lock(self, key: str, work: bool = False):
        path = self.path(key).with_suffix(".work.lock" if work else ".lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def put(self, key: str, checker: Checker, result: dict[str, str], origin: str) -> None:
        with self.lock(key):
            records = self.read(key)
            for name, status in result.items():
                if name not in checker.keys or status not in STATUSES:
                    raise ValueError(f"invalid property cache entry: {name}={status}")
                identity = checker.keys[name]
                old = records.get(identity)
                if old and old["status"] != status:
                    if old["status"] != "UNKNOWN" and status != "UNKNOWN":
                        raise ValueError(f"conflicting CEX/PROVEN for {name}; {origin}; previous={old['origin']}")
                    if status == "UNKNOWN":
                        continue
                records[identity] = {"status": status, "property": name, "origin": origin}
            save_json(self.path(key), {"schema": SCHEMA, "context_sha256": key, "properties": records})
