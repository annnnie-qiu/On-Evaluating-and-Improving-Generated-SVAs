"""Small, dependency-free utilities. All experiment writes stay under gen_assertions."""
from __future__ import annotations
import csv
import hashlib
import json
import os
import subprocess
import time
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class EnvironmentSetupError(RuntimeError):
    """Fixed setup or target mapping failed; changing a property cannot repair it."""

class SpecEvidenceRequired(EnvironmentSetupError):
    """Spec remains unresolved after model review; pending state must be kept."""


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as handle:
            handle.write(json.dumps(value,ensure_ascii=False,indent=2)+"\n")
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

def validate_budgets(value):
    if not isinstance(value,list) or not value or any(not isinstance(n,int) or isinstance(n,bool) or n<1 for n in value):
        raise ValueError("Step B budgets must be a nonempty list of positive integers")
    if len(set(value))!=len(value):
        raise ValueError("Step B budgets must not contain duplicates")
    return sorted(value)

def implementation_digest():
    files=[p for folder in (ROOT/"looplib",ROOT/"vendor") for p in folder.rglob("*")
           if p.is_file() and p.suffix in {".py",".tcl"}]
    return digest({str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)})

def table(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix=path.name+".",suffix=".tmp",dir=path.parent)
    try:
        with os.fdopen(fd,"w",newline="",encoding="utf-8-sig" if rows else "utf-8") as handle:
            if rows:
                keys=list(dict.fromkeys(k for row in rows for k in row))
                writer=csv.DictWriter(handle,fieldnames=keys)
                writer.writeheader();writer.writerows(rows)
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def command(argv, directory, log, timeout=None, on_line=None):
    """No shell; terminate the whole process group on timeout/interrupt."""
    import signal
    directory, log = Path(directory), Path(log)
    directory.mkdir(parents=True, exist_ok=True)
    log.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    last_heartbeat=start
    with log.open("w", encoding="utf-8") as handle, log.open("r",encoding="utf-8",errors="replace") as reader:
        p = subprocess.Popen([str(x) for x in argv], cwd=directory, stdout=handle,
                             stderr=subprocess.STDOUT, start_new_session=True)
        buffer=""
        def drain(final=False):
            nonlocal buffer
            if on_line is None: return
            buffer+=reader.read()
            while "\n" in buffer:
                line,buffer=buffer.split("\n",1)
                on_line(line)
            if final and buffer:
                on_line(buffer)
                buffer=""
        try:
            while True:
                try:
                    result=p.wait(timeout=.2 if on_line else 30)
                    drain(final=True)
                    return result
                except subprocess.TimeoutExpired:
                    drain()
                    now=time.monotonic()
                    elapsed=now-start
                    if now-last_heartbeat>=30:
                        print(f"[WAIT] {log.name} {elapsed:.0f}s; log={log}", flush=True)
                        last_heartbeat=now
                    if timeout and elapsed>=timeout:
                        raise TimeoutError(f"Tool timeout: {log}")
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()


def resolve(root, value):
    p = Path(value).expanduser()
    return (p if p.is_absolute() else Path(root) / p).resolve()

def source_files(root):
    return sorted(p for p in Path(root).rglob("*") if p.is_file()
                  and p.suffix.lower() in {".v", ".sv", ".sva", ".svh", ".vh", ".f", ".tcl", ".md", ".txt", ".rst", ".json"})

class EnvironmentLock:
    def __init__(self, dataset, setup):
        self.dataset, self.setup = Path(dataset), setup
        paths = set()
        setup_config=self.dataset/"gen_assertions"/"setup"/"dataset.json"
        if setup_config.is_file(): paths.add(setup_config)
        for name in ("rtl", "bind", "spec", "assertions"):
            paths.update(source_files(self.dataset / name))
        for unit in setup["units"]:
            for name in ("rtl_files", "bind_files", "environment_files", "spec_files", "assertion_files", "fixed_dependencies"):
                paths.update(resolve(self.dataset, p) for p in unit.get(name, []))
            paths.add(resolve(self.dataset, unit["template"]))
            for directory in unit.get("include_dirs", []):
                paths.update(source_files(resolve(self.dataset, directory)))
        self.paths = sorted(paths)
        self.initial = self.snapshot()
        self.identity = digest({"sources": self.initial, "setup": setup})

    def snapshot(self):
        return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.paths}

    def check(self):
        if self.snapshot() != self.initial:
            raise RuntimeError("Fixed RTL/spec/bind/template/dependency changed during the run; stopped. Assumptions cannot change.")

    def write(self, path):
        save(path, {"sha256": self.identity, "files": self.initial, "setup": self.setup,
                    "policy": "LLM can return property expressions only. Fixed environment is immutable."})
