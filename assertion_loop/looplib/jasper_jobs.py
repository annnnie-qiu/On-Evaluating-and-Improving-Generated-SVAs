"""Fresh Jasper workspaces for retries; never acquire or remove another lock."""
from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
import time

from .common import command, save


class JasperAttempt:
    """Keep immutable inputs/logs and a compatible latest-log view at root.

    Jasper creates jgproject in cwd. A new cwd for EVERY invocation prevents a
    stopped or still-running old session from blocking a retry. No product-
    specific force-ownership flags, process killing, or lock deletion is used.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.directory = Path(tempfile.mkdtemp(prefix="attempt_", dir=self.root))
        self.log = self.directory / "run.log"
        self.log.touch()
        self.metadata = {"directory": str(self.directory), "log": str(self.log),
                         "project_directory": str(self.directory / "jgproject"),
                         "status": "PREPARED"}
        self._record()
        self.publish("run.log")

    def publish(self, name):
        """Atomically point a legacy path at this attempt; archive old files."""
        target = self.directory / name
        if not target.is_file():
            raise FileNotFoundError(target)
        link = self.root / name
        if link.exists() and not link.is_symlink():
            # Preserve the old (pre-isolation) log/Tcl/source rather than
            # truncating it, including logs from a possibly active old process.
            fd, archive = tempfile.mkstemp(prefix=name + ".previous_", dir=self.root)
            os.close(fd)
            os.replace(link, archive)
        fd, temporary = tempfile.mkstemp(prefix="." + name + ".", dir=self.root)
        os.close(fd)
        os.unlink(temporary)
        try:
            os.symlink(str(target.relative_to(self.root)), temporary)
            os.replace(temporary, link)
        finally:
            if os.path.lexists(temporary):
                os.unlink(temporary)

    def _record(self):
        save(self.directory / "execution.json", self.metadata)
        save(self.root / "current_run.json", self.metadata)

    def run(self, executable, tcl, timeout, *, on_line=None):
        script = Path(tcl).resolve()
        # All callers emit absolute source/include paths. Keep the command line
        # unchanged; cwd isolation also works with existing Jasper wrappers.
        argv = [str(executable), "-batch", str(script)]
        self.metadata.update(status="RUNNING", argv=argv, timeout_seconds=timeout)
        self._record()
        print(f"[JASPER] fresh workspace={self.directory}; log={self.log}", flush=True)
        started = time.monotonic()
        try:
            code = command(argv, self.directory, self.log, timeout, on_line=on_line)
        except BaseException as error:
            self.metadata.update(status="INTERRUPTED" if isinstance(error, KeyboardInterrupt) else
                                 "TIMEOUT" if isinstance(error, TimeoutError) else "ERROR",
                                 error_type=type(error).__name__, error=str(error))
            raise
        else:
            self.metadata.update(status="EXITED" if code == 0 else "FAILED", returncode=code)
            if code:
                details = error_details(self.log)
                print(f"[JASPER] exit={code}; {details}; log={self.log}", flush=True)
            return code
        finally:
            self.metadata["elapsed_seconds"] = time.monotonic() - started
            self._record()


def error_details(log):
    lines = Path(log).read_text(errors="replace").splitlines()
    errors = [line.strip() for line in lines if re.match(
        r"^\s*(?:ERROR\b|FATAL\b|Error:|Fatal:|FH_(?:SETUP|PROVE)_ERROR\b)", line)]
    return " | ".join((errors or [line.strip() for line in lines if line.strip()])[-5:])[:1600] or "empty Jasper log"
