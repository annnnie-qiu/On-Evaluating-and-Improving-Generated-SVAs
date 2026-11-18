#!/usr/bin/env python3
"""Run an external command with a live log, heartbeat, timeout, and PID status."""
from __future__ import annotations

import dataclasses
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping


@dataclasses.dataclass
class WatchedResult:
    returncode: int | None
    output: str
    timed_out: bool
    duration_seconds: float
    pid: int


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_status(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _terminate_group(proc: subprocess.Popen[object]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=10)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


def run_logged(
    command: str,
    *,
    cwd: Path,
    log_path: Path,
    timeout_seconds: float,
    label: str,
    shell: str = "/bin/bash",
    env: Mapping[str, str] | None = None,
    heartbeat_seconds: float = 60.0,
) -> WatchedResult:
    """Execute command while writing output directly to disk.

    The child starts in its own process group so a timeout terminates the shell
    and Jasper together. status.json is written beside the log and can be read
    by check_progress.py from another terminal.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    status_path = log_path.parent / "status.json"
    started_wall = _iso_now()
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            executable=shell,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=dict(env) if env is not None else None,
            start_new_session=True,
        )
        status: dict[str, object] = {
            "state": "RUNNING",
            "pid": proc.pid,
            "label": label,
            "command": command,
            "cwd": str(cwd),
            "log_file": str(log_path),
            "started_at_utc": started_wall,
            "last_heartbeat_utc": _iso_now(),
            "elapsed_seconds": 0.0,
            "timeout_seconds": timeout_seconds,
        }
        _write_status(status_path, status)
        print(
            f"[RUNNING] {label}; pid={proc.pid}; timeout={timeout_seconds:.0f}s; "
            f"log={log_path}",
            flush=True,
        )
        next_heartbeat = started + heartbeat_seconds
        timed_out = False
        while proc.poll() is None:
            now = time.monotonic()
            elapsed = now - started
            if elapsed >= timeout_seconds:
                timed_out = True
                print(f"[TIMEOUT] {label}; terminating pid={proc.pid}", flush=True)
                _terminate_group(proc)
                break
            if now >= next_heartbeat:
                status["last_heartbeat_utc"] = _iso_now()
                status["elapsed_seconds"] = round(elapsed, 1)
                status["log_bytes"] = log_path.stat().st_size if log_path.exists() else 0
                _write_status(status_path, status)
                print(
                    f"[HEARTBEAT] {label}; elapsed={elapsed / 60.0:.1f} min; "
                    f"pid={proc.pid}; log_bytes={status['log_bytes']}",
                    flush=True,
                )
                next_heartbeat = now + heartbeat_seconds
            time.sleep(min(2.0, max(0.1, timeout_seconds - elapsed)))
        if proc.poll() is None:
            proc.wait()
        log.flush()

    duration = time.monotonic() - started
    state = "TIMEOUT" if timed_out else ("COMPLETE" if proc.returncode == 0 else "ERROR")
    status.update({
        "state": state,
        "returncode": proc.returncode,
        "last_heartbeat_utc": _iso_now(),
        "elapsed_seconds": round(duration, 3),
        "log_bytes": log_path.stat().st_size if log_path.exists() else 0,
    })
    _write_status(status_path, status)
    output = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    print(
        f"[{state}] {label}; elapsed={duration / 60.0:.1f} min; "
        f"exit={proc.returncode}; log={log_path}",
        flush=True,
    )
    return WatchedResult(proc.returncode, output, timed_out, duration, proc.pid)
