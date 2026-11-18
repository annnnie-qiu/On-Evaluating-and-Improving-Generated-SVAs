#!/usr/bin/env python3
"""Check a real AQ assertion set against spec/ with a Qwen/Ollama endpoint."""

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


API_URL = "https://ollama.morework.fyi/api/chat"
AI_MODEL = "qwen2.5-coder:14b"
TOKEN_FILE = "QWEN_API_TOKEN"
OUTPUT_SUBFOLDER = "spec_guard_qwen_simple"
MAX_SPEC_CHARACTERS = 80000
REQUEST_TIMEOUT_SECONDS = 900

SCRIPT_DIR = Path(__file__).resolve().parent


SYSTEM_PROMPT = """You are a hardware assertion specification-faithfulness checker.

Compare one SystemVerilog assertion with the supplied specification. Return
only PASS or FAIL.

First check relevance. If the specification does not discuss the same
parameter, signal, interface, or behavior, return PASS. Do not use a related
name or an unrelated specification rule as evidence. Missing internal RTL
detail alone is not a failure.

Return FAIL if a relevant specification rule exists and the assertion:
- contradicts the required behavior;
- reverses direction, polarity, timing, reset, or conditions;
- excludes a behavior or configuration permitted by the specification;
- fixes a configurable parameter to an unsupported value; or
- invents a required relationship between configurable parameters.

Treat parameterized assertions as reusable across configurations permitted by
the specification unless explicitly limited to one configuration.

Before returning FAIL, verify that the cited specification text concerns the
same subject as the assertion. If the relationship is uncertain, return PASS.
For FAIL, provide a short exact relevant quote. For PASS, the quote may be
empty. Do not reveal hidden chain-of-thought.

Return JSON only:
{
  "verdict": "PASS | FAIL",
  "reason": "short conclusion",
  "evidence_quote": "relevant exact quote or empty string",
  "conflict_dimension": "none | direction | polarity | timing | reset | condition | scope | other"
}"""


VERIFY_FAIL_PROMPT = """You are independently checking whether one SystemVerilog assertion is faithful to the supplied specification.
 
Use only the supplied specification.
 
Return PASS if:
- the assertion is unrelated to the specification;
- the specification does not describe this behavior; or
- the assertion correctly checks the relevant requirement.
 
Return FAIL if:
- the assertion changes the direction, polarity, timing, reset behavior, or conditions of the relevant requirement; or
- there is any input or condition described by the specification for which the assertion produces, requires, or accepts behavior different from the requirement.
 
Do not reject an assertion only because the specification omits an internal RTL detail. If the relationship cannot be determined from the specification, return PASS.
 
For FAIL, provide a short exact quote from the relevant specification requirement. For PASS, the quote may be empty.
 
Return JSON only:
{
  "verdict": "PASS | FAIL",
  "reason": "short conclusion",
  "evidence_quote": "relevant exact quote or empty string",
  "conflict_dimension": "none | direction | polarity | timing | reset | condition | behavior"
}"""


class Heartbeat:
    """Print one progress message per minute while a request is running."""

    def __init__(self, assertion_id):
        self.assertion_id = assertion_id
        self.stop_event = threading.Event()
        self.thread = None

    def __enter__(self):
        def report():
            started = time.monotonic()
            while not self.stop_event.wait(60):
                elapsed = (time.monotonic() - started) / 60
                print(f"[WAITING] {self.assertion_id}; elapsed={elapsed:.1f} min", flush=True)

        self.thread = threading.Thread(target=report, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)


def variant_paths(project_root, buggy=False):
    """Return the assertion input and AQ output paths for one experiment variant."""
    assertions_name = "buggy_assertions" if buggy else "assertions"
    aq_output_name = "aq_buggy_out" if buggy else "aq_out"
    return project_root / assertions_name, project_root / aq_output_name


def find_project_root(explicit_root=None, buggy=False):
    """Find the parent containing the selected assertions, spec, and AQ output."""
    if explicit_root:
        candidates = [Path(explicit_root).expanduser().resolve()]
    else:
        candidates = [SCRIPT_DIR, *SCRIPT_DIR.parents]
    for folder in candidates:
        assertions_folder, aq_output = variant_paths(folder, buggy)
        if (
            (folder / "spec").is_dir()
            and assertions_folder.is_dir()
            and (aq_output / "manifests/vacuity.json").is_file()
        ):
            return folder
    assertions_name = "buggy_assertions" if buggy else "assertions"
    aq_output_name = "aq_buggy_out" if buggy else "aq_out"
    raise RuntimeError(
        "Cannot find project root. Expected spec/, "
        f"{assertions_name}/, and {aq_output_name}/manifests/vacuity.json."
    )


def read_token():
    path = SCRIPT_DIR / TOKEN_FILE
    if not path.is_file():
        raise RuntimeError(f"Missing token file: {path}")
    token = path.read_text(encoding="utf-8").strip()
    if not token or token == "PASTE_YOUR_NEW_BEARER_TOKEN_HERE":
        raise RuntimeError(f"Paste a valid Bearer token into {path}")
    return token


def read_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError("PDF specifications require: pip install pypdf") from error
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def read_specification(project_root):
    """Combine all supported files under the real spec/ folder."""
    spec_folder = project_root / "spec"
    extensions = {".txt", ".md", ".markdown", ".rst", ".pdf"}
    sections = []
    for path in sorted(spec_folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        text = read_pdf(path) if path.suffix.lower() == ".pdf" else path.read_text(
            encoding="utf-8", errors="replace"
        )
        if text.strip():
            relative = path.relative_to(spec_folder).as_posix()
            sections.append(f"\n===== SPEC FILE: {relative} =====\n{text.strip()}")
    specification = "\n".join(sections)
    if not specification:
        raise RuntimeError("No readable specification was found under spec/.")
    if len(specification) > MAX_SPEC_CHARACTERS:
        raise RuntimeError(
            f"Specification size is {len(specification)} characters; this simple "
            f"version supports at most {MAX_SPEC_CHARACTERS}."
        )
    return specification


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_aq_assertions(assertions_folder, aq_output):
    """Load only assertions that passed the first three AQ stages."""
    manifests = aq_output / "manifests"
    inventory = read_json(manifests / "inventory_assertions.json")
    vacuity = read_json(manifests / "vacuity.json")
    qualification = read_json(aq_output / "qualification_report.json")

    qualified = {
        (row["assertion_file"], row["assertion_id"])
        for row in vacuity
        if row.get("status") in {"PASS", "NOT_APPLICABLE"}
    }
    if qualification.get("aq_count") != len(qualified):
        raise RuntimeError(
            f"AQ count mismatch: report={qualification.get('aq_count')}, "
            f"vacuity manifest={len(qualified)}"
        )

    source_cache = {}
    assertions = []
    for row in inventory:
        key = (row["assertion_file"], row["assertion_id"])
        if key not in qualified:
            continue
        filename = row["assertion_file"]
        if filename not in source_cache:
            path = assertions_folder / filename
            source_cache[filename] = path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        lines = source_cache[filename]
        start = int(row["start_line"]) - 1
        end = int(row["end_line"])
        assertions.append(
            {
                "assertion_file": filename,
                "assertion_id": row["assertion_id"],
                "property_name": row["property_name"],
                "assertion_text": "\n".join(lines[start:end]).strip(),
                "fingerprint": row["fingerprint"],
            }
        )
    return assertions, qualification


def build_prompt(specification, assertion):
    return f"""{SYSTEM_PROMPT}

SPECIFICATION
{specification}

TARGET ASSERTION
File: {assertion['assertion_file']}
ID: {assertion['assertion_id']}
Name: {assertion['property_name']}

```systemverilog
{assertion['assertion_text']}
```
"""


def build_verify_prompt(specification, assertion):
    """Build an independent prompt without exposing the first decision."""
    return f"""{VERIFY_FAIL_PROMPT}

SPECIFICATION
{specification}

TARGET ASSERTION
File: {assertion['assertion_file']}
ID: {assertion['assertion_id']}
Name: {assertion['property_name']}

```systemverilog
{assertion['assertion_text']}
```
"""


def call_qwen(token, prompt):
    """Call the same endpoint and model shown in the supplied curl command."""
    payload = {
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    completed = subprocess.run(
        [
            "curl", "--silent", "--show-error",
            "--max-time", str(REQUEST_TIMEOUT_SECONDS),
            "--write-out", "\n%{http_code}", API_URL,
            "-H", f"Authorization: Bearer {token}",
            "-H", "Content-Type: application/json",
            "-d", json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"curl failed: {completed.stderr.strip()}")
    try:
        body, status_text = completed.stdout.rsplit("\n", 1)
        status = int(status_text)
    except (ValueError, TypeError) as error:
        raise RuntimeError(f"Cannot parse curl response: {completed.stdout}") from error
    if status < 200 or status >= 300:
        raise RuntimeError(f"HTTP {status}: {body}")
    result = json.loads(body)
    if result.get("error"):
        raise RuntimeError(str(result["error"]))
    message = result.get("message", {})
    content = message.get("content", "") if isinstance(message, dict) else ""
    if not content:
        raise RuntimeError(f"Unexpected endpoint response: {json.dumps(result)}")
    return content


def parse_decision(text):
    """Parse the model verdict without overriding its semantic decision."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise RuntimeError("The model response must be a JSON object.")

    verdict = str(value.get("verdict", "")).strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise RuntimeError(
            f"The model returned an invalid verdict: {value.get('verdict')!r}"
        )

    return {
        "verdict": verdict,
        "reason": str(value.get("reason", "")),
        "evidence_quote": str(value.get("evidence_quote", "")).strip(),
        "conflict_dimension": str(
            value.get("conflict_dimension", "none")
        ).strip().lower(),
    }


def save_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def write_results(output_folder, results, qualification, input_variant):
    columns = [
        "assertion_file", "assertion_id", "property_name", "assertion_text",
        "first_verdict", "first_reason", "first_evidence_quote",
        "first_conflict_dimension", "verification_verdict",
        "verification_reason", "verification_evidence_quote",
        "verification_conflict_dimension", "verdict", "reason",
        "evidence_quote", "conflict_dimension",
    ]
    output_folder.mkdir(parents=True, exist_ok=True)
    (output_folder / "spec_check_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    save_csv(output_folder / "spec_check_results.csv", results, columns)
    passed = [row for row in results if row["verdict"] == "PASS"]
    failed = [row for row in results if row["verdict"] == "FAIL"]
    first_failed = [row for row in results if row["first_verdict"] == "FAIL"]
    overturned = [
        row for row in results
        if row["first_verdict"] == "FAIL"
        and row["verification_verdict"] == "PASS"
    ]
    confirmed = [
        row for row in results
        if row["first_verdict"] == "FAIL"
        and row["verification_verdict"] == "FAIL"
    ]
    save_csv(output_folder / "passed_assertions.csv", passed, columns)
    save_csv(output_folder / "failed_assertions.csv", failed, columns)
    save_csv(output_folder / "overturned_failures.csv", overturned, columns)
    save_csv(output_folder / "aq_after_spec_filter.csv", passed, columns)

    a0 = int(qualification["a0_count"])
    technical_aq = len(results)
    final_aq = len(passed)
    technical_yield = technical_aq / a0 if a0 else 0.0
    final_yield = final_aq / a0 if a0 else 0.0
    prior_readiness = qualification.get("raw_readiness", "UNRESOLVED")
    spec_status = "FAIL" if failed else "PASS"
    if prior_readiness == "FAIL" or spec_status == "FAIL":
        readiness = "FAIL"
    elif prior_readiness != "PASS":
        readiness = "UNRESOLVED"
    else:
        readiness = "PASS"

    lines = [
        "QWEN SPECIFICATION FAITHFULNESS CHECK",
        f"Input Variant                 : {input_variant.upper()}",
        f"Model                         : {AI_MODEL}",
        f"Input technical AQ            : {technical_aq}",
        f"First-pass FAIL               : {len(first_failed)}",
        f"Double-check overturned       : {len(overturned)}",
        f"Double-check confirmed        : {len(confirmed)}",
        f"PASS                          : {len(passed)}",
        f"FAIL                          : {len(failed)}",
        f"Removed by spec guard         : {len(failed)}",
        f"Final AQ                      : {final_aq}",
        f"Technical Raw Readiness       : {prior_readiness}",
        f"Specification Guard Status    : {spec_status}",
        f"Overall Raw Readiness         : {readiness}",
        f"Technical Qualification Yield : {technical_aq}/{a0} ({100 * technical_yield:.2f}%)",
        f"Post-spec Qualification Yield : {final_aq}/{a0} ({100 * final_yield:.2f}%)",
        "",
        "FAILED ASSERTIONS",
    ]
    if failed:
        for row in failed:
            lines.append(f"- {row['assertion_file']} :: {row['assertion_id']} :: {row['reason']}")
    else:
        lines.append("- None")
    summary = "\n".join(lines) + "\n"
    (output_folder / "summary.txt").write_text(summary, encoding="utf-8")
    print("\n" + summary)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Check qualified assertions for specification inconsistency."
    )
    parser.add_argument(
        "--project-root",
        help="Project root containing spec/ and the selected assertion/AQ folders.",
    )
    parser.add_argument(
        "-buggy",
        "--buggy",
        action="store_true",
        help=(
            "Read buggy_assertions/ and aq_buggy_out/ instead of "
            "assertions/ and aq_out/."
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="Ignore matching cached LLM decisions."
    )
    args = parser.parse_args(argv)

    input_variant = "buggy" if args.buggy else "normal"
    project_root = find_project_root(args.project_root, args.buggy)
    assertions_folder, aq_output = variant_paths(project_root, args.buggy)
    token = read_token()
    specification = read_specification(project_root)
    assertions, qualification = load_aq_assertions(assertions_folder, aq_output)
    output_folder = aq_output / OUTPUT_SUBFOLDER
    cache_folder = output_folder / "cache"
    cache_folder.mkdir(parents=True, exist_ok=True)

    print(f"Project root  : {project_root}")
    print(f"Input variant : {input_variant.upper()}")
    print(f"Assertions    : {assertions_folder}")
    print(f"AQ output     : {aq_output}")
    print(f"Endpoint      : {API_URL}")
    print(f"Model         : {AI_MODEL}")
    print(f"AQ assertions : {len(assertions)}")

    spec_hash = hashlib.sha256(specification.encode()).hexdigest()
    results = []
    for index, assertion in enumerate(assertions, start=1):
        cache_name = re.sub(
            r"[^A-Za-z0-9_.-]+", "_",
            assertion["assertion_file"] + "__" + assertion["assertion_id"],
        ) + ".json"
        cache_file = cache_folder / cache_name
        fingerprint = hashlib.sha256(
            (
                API_URL + AI_MODEL + SYSTEM_PROMPT + VERIFY_FAIL_PROMPT
                + "spec_faithfulness_double_check_v3"
                + spec_hash + assertion["fingerprint"]
            ).encode()
        ).hexdigest()
        if cache_file.is_file() and not args.force:
            cached = read_json(cache_file)
            if cached.get("fingerprint") == fingerprint:
                print(f"[{index}/{len(assertions)}] RESUMED {assertion['assertion_id']}")
                results.append(cached)
                continue

        print(f"[{index}/{len(assertions)}] CHECKING {assertion['assertion_id']}", flush=True)
        with Heartbeat(assertion["assertion_id"]):
            response_text = call_qwen(token, build_prompt(specification, assertion))
        first_decision = parse_decision(response_text)
        print(f"    -> FIRST {first_decision['verdict']}", flush=True)

        verification = {
            "verdict": "NOT_RUN",
            "reason": "",
            "evidence_quote": "",
            "conflict_dimension": "none",
        }
        if first_decision["verdict"] == "FAIL":
            print(f"    -> DOUBLE-CHECKING {assertion['assertion_id']}", flush=True)
            with Heartbeat(assertion["assertion_id"] + " verification"):
                verification_text = call_qwen(
                    token,
                    build_verify_prompt(specification, assertion),
                )
            verification = parse_decision(verification_text)
            print(f"    -> DOUBLE-CHECK {verification['verdict']}", flush=True)

        final_decision = (
            verification
            if first_decision["verdict"] == "FAIL"
            else first_decision
        )
        result = {
            **assertion,
            "first_verdict": first_decision["verdict"],
            "first_reason": first_decision["reason"],
            "first_evidence_quote": first_decision["evidence_quote"],
            "first_conflict_dimension": first_decision["conflict_dimension"],
            "verification_verdict": verification["verdict"],
            "verification_reason": verification["reason"],
            "verification_evidence_quote": verification["evidence_quote"],
            "verification_conflict_dimension": verification["conflict_dimension"],
            "verdict": final_decision["verdict"],
            "reason": final_decision["reason"],
            "evidence_quote": final_decision["evidence_quote"],
            "conflict_dimension": final_decision["conflict_dimension"],
            "fingerprint": fingerprint,
        }
        cache_file.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        results.append(result)
        print(f"    -> FINAL {result['verdict']}", flush=True)

    write_results(output_folder, results, qualification, input_variant)
    print(f"Results: {output_folder}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"QWEN SPEC CHECK ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)

