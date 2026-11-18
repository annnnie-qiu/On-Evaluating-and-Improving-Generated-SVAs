#!/usr/bin/env python3
"""Show active Jasper jobs and AQ qualification progress."""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def age_text(started: str, fallback: float) -> str:
    try:
        dt = datetime.fromisoformat(started)
        seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    except (ValueError, TypeError):
        seconds = fallback
    return f"{max(0.0, seconds) / 60.0:.1f} min"


def manifest_count(path: Path) -> str:
    if not path.is_file():
        return "not created"
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    counts: dict[str, int] = {}
    for row in rows:
        state = str(row.get("status", "UNKNOWN"))
        counts[state] = counts.get(state, 0) + 1
    detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    return f"rows={len(rows)}" + (f"; {detail}" if detail else "")


def snapshot(project: Path) -> None:
    aq = project / "aq_out"
    statuses: list[dict[str, Any]] = []
    if aq.is_dir():
        for path in aq.rglob("status.json"):
            try:
                item = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            item["_status_path"] = str(path)
            statuses.append(item)
    running = []
    for item in statuses:
        pid = int(item.get("pid", -1) or -1)
        if item.get("state") == "RUNNING" and pid_alive(pid):
            running.append(item)
    print("=" * 78)
    print(f"Project: {project}")
    print(f"Active Jasper jobs: {len(running)}")
    for item in sorted(running, key=lambda x: str(x.get("started_at_utc", ""))):
        elapsed = age_text(str(item.get("started_at_utc", "")), float(item.get("elapsed_seconds", 0) or 0))
        print(f"  RUNNING pid={item.get('pid')} elapsed={elapsed}")
        print(f"    {item.get('label')}")
        print(f"    log: {item.get('log_file')}")
    if not running:
        stale = [x for x in statuses if x.get("state") == "RUNNING"]
        if stale:
            print(f"  No live PID; found {len(stale)} stale RUNNING status file(s).")

    print("AQ manifests:")
    for stage in ("syntax", "fpv", "vacuity"):
        path = aq / "manifests" / f"{stage}.csv"
        print(f"  {stage:<8}: {manifest_count(path)}")
    report = aq / "qualification_report.txt"
    if report.is_file():
        print("Qualification report:")
        for line in report.read_text(encoding="utf-8").splitlines()[1:]:
            print(f"  {line}")
    print("=" * 78)


def discover_project_root(start: Path) -> Path:
    for candidate in [start.resolve(), *start.resolve().parents]:
        if (candidate / "rtl").is_dir() and (candidate / "assertions").is_dir():
            return candidate
    raise SystemExit("Could not find a parent containing rtl/ and assertions/.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", help="project root; auto-detected when omitted")
    ap.add_argument("--watch", type=float, metavar="SECONDS", help="repeat until Ctrl-C")
    args = ap.parse_args()
    project = (
        Path(args.project_root).expanduser().resolve()
        if args.project_root
        else discover_project_root(Path.cwd())
    )
    try:
        while True:
            snapshot(project)
            if not args.watch:
                break
            time.sleep(max(2.0, args.watch))
    except KeyboardInterrupt:
        print("Stopped status watch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
