#!/usr/bin/env python3
"""Run one technical AQ pipeline and compare four LLM specification checks.

Complete technical qualification artifacts (Syntax, FPV, and Vacuity) are
reused without invoking the AQ runner. The runner is called only when artifacts
are missing or --rerun-aq is requested. The shared AQ is then checked in three
independent runs with Qwen, DeepSeek, GPT, and Gemini using the same prompt and
parser. The number of runs remains configurable with --runs.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
QWEN_API_URL = "https://ollama.morework.fyi/api/chat"
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OUTPUT_SUBFOLDER = "spec_guard_four_models"
MAX_SPEC_CHARACTERS = 80000
REQUEST_TIMEOUT_SECONDS = 900
PROMPT_VERSION = "spec_faithfulness_single_prompt_v3_unresolved_as_pass"
OPENROUTER_REQUEST_POLICY_VERSION = "low_reasoning_hidden_json_v1"


MODEL_CONFIGS = {
    "qwen": {
        "display_name": "Qwen 2.5 Coder 14B",
        "model_id": "qwen2.5-coder:14b",
        "api_type": "ollama",
        "api_url": QWEN_API_URL,
        "token_env": "QWEN_API_TOKEN",
        "token_file": "QWEN_API_TOKEN",
    },
    "deepseek": {
        "display_name": "DeepSeek V4.1 Flash",
        "model_id": "deepseek/deepseek-v4.1-flash",
        "api_type": "openrouter",
        "api_url": OPENROUTER_API_URL,
        "token_env": "OPENROUTER_API_KEY",
        "token_file": "OPENROUTER_API_KEY",
    },
    "gpt": {
        "display_name": "GPT-6 Luna",
        "model_id": "openai/gpt-6-luna",
        "api_type": "openrouter",
        "api_url": OPENROUTER_API_URL,
        "token_env": "OPENROUTER_API_KEY",
        "token_file": "OPENROUTER_API_KEY",
        "supports_temperature": False,
    },
    "gemini": {
        "display_name": "Gemini 3.5 Flash Lite",
        "model_id": "google/gemini-3.5-flash-lite",
        "api_type": "openrouter",
        "api_url": OPENROUTER_API_URL,
        "token_env": "OPENROUTER_API_KEY",
        "token_file": "OPENROUTER_API_KEY",
    },
}


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

Choose exactly one verdict, PASS or FAIL, and return JSON only:
{
  "reason": "short conclusion",
  "evidence_quote": "relevant exact quote or empty string",
  "conflict_dimension": "none | direction | polarity | timing | reset | condition | scope | other",
  "verdict": "PASS | FAIL"
}"""



class Heartbeat:
    """Print one progress message per minute while an API request is running."""

    def __init__(self, model_key: str, assertion_id: str):
        self.model_key = model_key
        self.assertion_id = assertion_id
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def __enter__(self):
        def report():
            started = time.monotonic()
            while not self.stop_event.wait(60):
                elapsed = (time.monotonic() - started) / 60
                print(
                    f"[WAITING] model={self.model_key} "
                    f"assertion={self.assertion_id} elapsed={elapsed:.1f} min",
                    flush=True,
                )

        self.thread = threading.Thread(target=report, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)


def variant_paths(project_root: Path, buggy: bool = False) -> tuple[Path, Path]:
    """Return assertion input and AQ output paths for one variant."""
    assertions_name = "buggy_assertions" if buggy else "assertions"
    aq_output_name = "aq_buggy_out" if buggy else "aq_out"
    return project_root / assertions_name, project_root / aq_output_name


def find_project_root(explicit_root: str | None, buggy: bool) -> Path:
    """Find a project containing spec/ and the selected assertions folder."""
    if explicit_root:
        candidates = [Path(explicit_root).expanduser().resolve()]
    else:
        candidates = [SCRIPT_DIR, *SCRIPT_DIR.parents, Path.cwd().resolve()]

    seen: set[Path] = set()
    for folder in candidates:
        if folder in seen:
            continue
        seen.add(folder)
        assertions_folder, _ = variant_paths(folder, buggy)
        if (folder / "spec").is_dir() and assertions_folder.is_dir():
            return folder

    assertions_name = "buggy_assertions" if buggy else "assertions"
    raise RuntimeError(
        f"Cannot find project root containing spec/ and {assertions_name}/. "
        "Use --project-root to select it explicitly."
    )


def run_technical_aq_once(
    project_root: Path,
    buggy: bool,
    skip_aq: bool,
    rerun_aq: bool,
) -> Path:
    """Reuse complete AQ artifacts, or run the technical pipeline if needed."""
    _, aq_output = variant_paths(project_root, buggy)
    report = aq_output / "qualification_report.json"
    vacuity = aq_output / "manifests" / "vacuity.json"

    existing_complete = False
    if report.is_file() and vacuity.is_file():
        try:
            existing_complete = bool(read_json(report).get("stages_complete", False))
        except (json.JSONDecodeError, OSError, AttributeError):
            existing_complete = False

    if existing_complete and not rerun_aq:
        print("[AQ] Complete technical AQ artifacts found; no AQ command will run.")
    elif skip_aq:
        raise RuntimeError(
            "--skip-aq was requested, but complete technical AQ artifacts "
            "were not found."
        )
    else:
        runner = (
            project_root
            / "aq_qualification_pipeline_v1"
            / "run_aq_qualification.py"
        )
        if runner.is_file():
            command = [
                sys.executable,
                str(runner),
                "--project-root",
                str(project_root),
            ]
            if buggy:
                command.append("-buggy")
            reason = "forced" if rerun_aq else "required because artifacts are missing"
            print(f"[AQ] Running the technical qualification pipeline ({reason}).")
            completed = subprocess.run(command, check=False)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Technical AQ pipeline failed with exit code "
                    f"{completed.returncode}."
                )
        else:
            raise RuntimeError(
                "Technical AQ results are missing and the AQ runner was not found: "
                f"{runner}"
            )

    if not report.is_file() or not vacuity.is_file():
        raise RuntimeError(
            "Technical AQ is incomplete. Expected qualification_report.json "
            "and manifests/vacuity.json."
        )

    qualification = read_json(report)
    if not qualification.get("stages_complete", False):
        raise RuntimeError(
            "Technical AQ did not complete Syntax, FPV, and Vacuity. "
            "Resolve the AQ pipeline failure before running LLM checks."
        )
    return aq_output


def read_secret(config: dict, project_root: Path) -> str:
    """Read a token from an environment variable or a local token file."""
    environment_name = config["token_env"]
    token = os.environ.get(environment_name, "").strip()
    if token:
        return token

    filename = config["token_file"]
    candidates = [
        SCRIPT_DIR / filename,
        project_root / filename,
        project_root / "qwen_spec_check_vm" / filename,
        project_root / "llm_spec_check_vm" / filename,
    ]
    for path in candidates:
        if not path.is_file():
            continue
        token = path.read_text(encoding="utf-8").strip()
        if token and "PASTE_" not in token and "TODO" not in token:
            return token

    locations = ", ".join(str(path) for path in candidates)
    raise RuntimeError(
        f"Missing {environment_name}. Set the environment variable or place "
        f"the token in one of: {locations}"
    )


def read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as error:
        raise RuntimeError("PDF specifications require: pip install pypdf") from error
    return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)


def read_specification(project_root: Path) -> str:
    """Combine supported natural-language files under spec/."""
    spec_folder = project_root / "spec"
    extensions = {".txt", ".md", ".markdown", ".rst", ".pdf"}
    sections = []
    for path in sorted(spec_folder.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if path.suffix.lower() == ".pdf":
            text = read_pdf(path)
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            relative = path.relative_to(spec_folder).as_posix()
            sections.append(f"\n===== SPEC FILE: {relative} =====\n{text.strip()}")

    specification = "\n".join(sections)
    if not specification:
        raise RuntimeError("No readable specification was found under spec/.")
    if len(specification) > MAX_SPEC_CHARACTERS:
        raise RuntimeError(
            f"Specification size is {len(specification)} characters; this "
            f"script supports at most {MAX_SPEC_CHARACTERS}."
        )
    return specification


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json_atomic(path: Path, value) -> None:
    """Write JSON atomically so an interrupted run cannot leave an empty cache."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_aq_assertions(
    assertions_folder: Path, aq_output: Path
) -> tuple[list[dict], dict]:
    """Load the technical AQ once and reuse it for every model."""
    manifests = aq_output / "manifests"
    inventory = read_json(manifests / "inventory_assertions.json")
    vacuity = read_json(manifests / "vacuity.json")
    qualification = read_json(aq_output / "qualification_report.json")

    qualified = {
        (row["assertion_file"], row["assertion_id"])
        for row in vacuity
        if row.get("status") in {"PASS", "NOT_APPLICABLE"}
    }
    reported_aq = qualification.get("aq_count")
    if reported_aq is None or int(reported_aq) != len(qualified):
        raise RuntimeError(
            f"AQ count mismatch: report={reported_aq}, "
            f"vacuity manifest={len(qualified)}"
        )

    source_cache: dict[str, list[str]] = {}
    assertions = []
    for row in inventory:
        key = (row["assertion_file"], row["assertion_id"])
        if key not in qualified:
            continue
        filename = row["assertion_file"]
        if filename not in source_cache:
            source_path = assertions_folder / filename
            if not source_path.is_file():
                raise RuntimeError(f"Assertion source does not exist: {source_path}")
            source_cache[filename] = source_path.read_text(
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


def build_prompt(specification: str, assertion: dict) -> str:
    """Build the single prompt shared by all four models."""
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


def run_curl(
    api_url: str,
    token: str,
    payload: dict,
    extra_headers: list[str] | None = None,
) -> dict:
    """Send one request and return raw HTTP data without logging the token."""
    started = time.monotonic()
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--max-time",
        str(REQUEST_TIMEOUT_SECONDS),
        "--write-out",
        "\n%{http_code}",
        api_url,
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Content-Type: application/json",
    ]
    for header in extra_headers or []:
        command.extend(["-H", header])
    command.extend(["-d", json.dumps(payload)])

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    duration_seconds = time.monotonic() - started
    body = completed.stdout
    status = None
    status_parse_error = ""
    try:
        body, status_text = completed.stdout.rsplit("\n", 1)
        status = int(status_text)
    except (ValueError, TypeError) as error:
        status_parse_error = str(error)
    response = None
    response_parse_error = ""
    try:
        response = json.loads(body)
    except json.JSONDecodeError as error:
        response_parse_error = str(error)
    return {
        "duration_seconds": duration_seconds,
        "curl_returncode": completed.returncode,
        "curl_stderr": completed.stderr,
        "http_status": status,
        "status_parse_error": status_parse_error,
        "raw_response_body": body,
        "response_json": response,
        "response_parse_error": response_parse_error,
    }


def normalize_message_content(content) -> str:
    """Normalize string or block-list message content into plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def normalize_token_usage(config: dict, response: dict) -> dict:
    """Return token counts only; this function never calculates cost."""
    if config["api_type"] == "openrouter":
        usage = response.get("usage", {})
        if not isinstance(usage, dict):
            usage = {}
        completion_details = usage.get("completion_tokens_details", {})
        if not isinstance(completion_details, dict):
            completion_details = {}
        return {
            "token_usage_reported": any(
                key in usage
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            ),
            "input_tokens": int(usage.get("prompt_tokens", 0) or 0),
            "output_tokens": int(usage.get("completion_tokens", 0) or 0),
            "reasoning_tokens": int(
                completion_details.get("reasoning_tokens", 0) or 0
            ),
            "total_tokens": int(usage.get("total_tokens", 0) or 0),
        }

    input_tokens = int(response.get("prompt_eval_count", 0) or 0)
    output_tokens = int(response.get("eval_count", 0) or 0)
    return {
        "token_usage_reported": any(
            key in response for key in ("prompt_eval_count", "eval_count")
        ),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": 0,
        "total_tokens": input_tokens + output_tokens,
    }


def call_model(
    token: str,
    config: dict,
    prompt: str,
    trace_file: Path,
    trace_context: dict,
) -> tuple[str, dict]:
    """Call one model and persist a redacted full request/response trace."""
    if config["api_type"] == "ollama":
        payload = {
            "model": config["model_id"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        extra_headers = []
    else:
        payload = {
            "model": config["model_id"],
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 4096,
            "stream": False,
            "reasoning": {
                "effort": "low",
                "exclude": True,
            },
            "response_format": {"type": "json_object"},
            "provider": {"require_parameters": True},
            "usage": {"include": True},
        }
        if config.get("supports_temperature", True):
            payload["temperature"] = 0
        extra_headers = [
            "X-OpenRouter-Title: Assertion Specification Check",
            "X-OpenRouter-Metadata: enabled",
        ]

    trace = {
        **trace_context,
        "trace_format_version": "full_api_trace_v1",
        "request": {
            "api_url": config["api_url"],
            "headers": {
                "Authorization": "Bearer [REDACTED]",
                "Content-Type": "application/json",
                **{
                    header.split(":", 1)[0]: header.split(":", 1)[1].strip()
                    for header in extra_headers
                    if ":" in header
                },
            },
            "payload": payload,
        },
        "status": "REQUEST_PREPARED",
    }
    write_json_atomic(trace_file, trace)

    http = run_curl(
        config["api_url"],
        token,
        payload,
        extra_headers=extra_headers,
    )
    trace["http"] = http

    try:
        if http["curl_returncode"] != 0:
            raise RuntimeError(f"curl failed: {http['curl_stderr'].strip()}")
        if http["http_status"] is None:
            raise RuntimeError(
                f"Cannot parse HTTP response status: {http['status_parse_error']}"
            )
        if http["http_status"] < 200 or http["http_status"] >= 300:
            raise RuntimeError(
                f"HTTP {http['http_status']}: {http['raw_response_body']}"
            )
        response = http["response_json"]
        if not isinstance(response, dict):
            raise RuntimeError(
                f"API returned invalid JSON: {http['raw_response_body']}"
            )
        if response.get("error"):
            raise RuntimeError(str(response["error"]))

        if config["api_type"] == "ollama":
            message = response.get("message", {})
            content = message.get("content", "") if isinstance(message, dict) else ""
        else:
            choices = response.get("choices", [])
            # A successful HTTP response without a usable choice is a semantic
            # non-decision, not an explicit contradiction. It is therefore
            # handled by the unresolved-as-PASS policy below. API error objects
            # and non-2xx responses are still rejected above.
            message = (
                choices[0].get("message", {})
                if choices and isinstance(choices[0], dict)
                else {}
            )
            content = message.get("content", "") if isinstance(message, dict) else ""

        content = normalize_message_content(content)
        token_usage = normalize_token_usage(config, response)
        trace["token_usage"] = token_usage
        if content.strip():
            trace["status"] = "API_SUCCESS"
            trace["error"] = ""
        else:
            # The HTTP/API call succeeded, but the provider did not return a
            # usable semantic decision. Under this experiment's open-world
            # policy, the parser records this as UNRESOLVED_AS_PASS. This is
            # deliberately distinct from authentication, HTTP, or network
            # failures, which still raise an exception above.
            trace["status"] = "API_SUCCESS_EMPTY"
            trace["error"] = "The model returned an empty response."
        write_json_atomic(trace_file, trace)
        return content, token_usage
    except Exception as error:
        trace["status"] = "API_ERROR"
        trace["error"] = str(error)
        write_json_atomic(trace_file, trace)
        raise


def unresolved_as_pass(reason: str, raw_verdict: str = "UNRESOLVED") -> dict:
    """Apply the configured policy: no explicit contradiction means PASS."""
    return {
        "verdict": "PASS",
        "raw_verdict": raw_verdict,
        "decision_status": "UNRESOLVED_AS_PASS",
        "reason": reason,
        "evidence_quote": "",
        "conflict_dimension": "none",
    }


def parse_decision(text: str) -> dict:
    """Parse a decision; unresolved model output is auditable but passes."""
    cleaned = text.strip()
    if not cleaned:
        return unresolved_as_pass(
            "No explicit specification contradiction was returned; "
            "empty model output was treated as unresolved and passed by policy."
        )

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)

    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return unresolved_as_pass(
                "No explicit machine-readable contradiction was returned; "
                "non-JSON model output was treated as unresolved and passed "
                "by policy."
            )
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return unresolved_as_pass(
                "No explicit machine-readable contradiction was returned; "
                "malformed JSON was treated as unresolved and passed by policy."
            )

    if not isinstance(value, dict):
        return unresolved_as_pass(
            "The model response was not a decision object and was treated "
            "as unresolved and passed by policy."
        )

    verdict = str(value.get("verdict", "")).strip().upper()
    if verdict in {"UNRESOLVED", "UNCERTAIN", "UNKNOWN", "AMBIGUOUS"}:
        return unresolved_as_pass(
            str(value.get("reason", "")).strip()
            or "The model marked the comparison unresolved; it passed by policy.",
            raw_verdict=verdict,
        )
    if verdict not in {"PASS", "FAIL"}:
        return unresolved_as_pass(
            "The model did not return an explicit PASS/FAIL contradiction "
            "decision; it was treated as unresolved and passed by policy.",
            raw_verdict=verdict or "MISSING",
        )

    return {
        "verdict": verdict,
        "raw_verdict": verdict,
        "decision_status": "MODEL_DECISION",
        "reason": str(value.get("reason", "")).strip(),
        "evidence_quote": str(value.get("evidence_quote", "")).strip(),
        "conflict_dimension": str(
            value.get("conflict_dimension", "none")
        ).strip().lower(),
    }


def save_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def write_summary_table(output_folder: Path, summary: dict) -> None:
    """Write one two-column result table for one model."""
    rows = [
        {"metric": "Input variant", "value": summary["input_variant"]},
        {"metric": "Model", "value": summary["model_name"]},
        {"metric": "Model ID", "value": summary["model_id"]},
        {"metric": "Independent run", "value": summary["run_number"]},
        {"metric": "Run status", "value": summary["run_status"]},
        {"metric": "Generated set (A0)", "value": summary["a0_count"]},
        {"metric": "Input technical AQ", "value": summary["technical_aq"]},
        {"metric": "PASS", "value": summary["pass_count"]},
        {"metric": "FAIL", "value": summary["fail_count"]},
        {
            "metric": "Unresolved counted as PASS",
            "value": summary.get("unresolved_as_pass_count", 0),
        },
        {"metric": "Final AQ", "value": summary["final_aq"]},
        {
            "metric": "Technical Raw Readiness",
            "value": summary["technical_raw_readiness"],
        },
        {
            "metric": "Specification Check Status",
            "value": summary["specification_check_status"],
        },
        {
            "metric": "Overall Raw Readiness",
            "value": summary["overall_raw_readiness"],
        },
        {
            "metric": "Technical Qualification Yield",
            "value": summary["technical_qualification_yield"],
        },
        {
            "metric": "Post-spec Qualification Yield",
            "value": summary["post_spec_qualification_yield"],
        },
        {"metric": "Error", "value": summary.get("error", "")},
    ]
    save_csv(output_folder / "summary_table.csv", rows, ["metric", "value"])


def write_model_results(
    output_folder: Path,
    model_key: str,
    config: dict,
    run_number: int,
    results: list[dict],
    qualification: dict,
    input_variant: str,
) -> dict:
    """Write detailed results and the model's summary table."""
    columns = [
        "model_key",
        "model_id",
        "assertion_file",
        "assertion_id",
        "property_name",
        "assertion_text",
        "verdict",
        "raw_verdict",
        "decision_status",
        "reason",
        "evidence_quote",
        "conflict_dimension",
        "token_usage_reported",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
        "trace_file",
    ]
    output_folder.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_folder / "spec_check_results.json", results)
    save_csv(output_folder / "spec_check_results.csv", results, columns)

    passed = [row for row in results if row["verdict"] == "PASS"]
    failed = [row for row in results if row["verdict"] == "FAIL"]
    unresolved_as_pass_rows = [
        row
        for row in results
        if row.get("decision_status") == "UNRESOLVED_AS_PASS"
    ]
    save_csv(output_folder / "passed_assertions.csv", passed, columns)
    save_csv(output_folder / "failed_assertions.csv", failed, columns)
    save_csv(output_folder / "aq_after_spec_filter.csv", passed, columns)

    a0 = int(qualification["a0_count"])
    technical_aq = len(results)
    final_aq = len(passed)
    technical_yield = technical_aq / a0 if a0 else 0.0
    final_yield = final_aq / a0 if a0 else 0.0
    technical_readiness = qualification.get("raw_readiness", "UNRESOLVED")
    specification_status = "FAIL" if failed else "PASS"
    if technical_readiness == "FAIL" or specification_status == "FAIL":
        overall_readiness = "FAIL"
    elif technical_readiness != "PASS":
        overall_readiness = "UNRESOLVED"
    else:
        overall_readiness = "PASS"

    summary = {
        "input_variant": input_variant.upper(),
        "model_key": model_key,
        "model_name": config["display_name"],
        "model_id": config["model_id"],
        "run_number": run_number,
        "run_status": "COMPLETE",
        "a0_count": a0,
        "technical_aq": technical_aq,
        "pass_count": len(passed),
        "fail_count": len(failed),
        "unresolved_as_pass_count": len(unresolved_as_pass_rows),
        "final_aq": final_aq,
        "technical_raw_readiness": technical_readiness,
        "specification_check_status": specification_status,
        "overall_raw_readiness": overall_readiness,
        "technical_qualification_yield": (
            f"{technical_aq}/{a0} ({100 * technical_yield:.2f}%)"
        ),
        "technical_qualification_yield_percent": 100 * technical_yield,
        "post_spec_qualification_yield": (
            f"{final_aq}/{a0} ({100 * final_yield:.2f}%)"
        ),
        "post_spec_qualification_yield_percent": 100 * final_yield,
        "error": "",
    }
    write_json_atomic(output_folder / "summary.json", summary)
    write_summary_table(output_folder, summary)

    lines = [
        "SPECIFICATION FAITHFULNESS CHECK",
        f"Input Variant                 : {summary['input_variant']}",
        f"Model                         : {summary['model_name']}",
        f"Model ID                      : {summary['model_id']}",
        f"Independent run               : {run_number}",
        f"Input technical AQ            : {technical_aq}",
        f"PASS                          : {len(passed)}",
        f"FAIL                          : {len(failed)}",
        f"Unresolved counted as PASS    : {len(unresolved_as_pass_rows)}",
        f"Final AQ                      : {final_aq}",
        f"Technical Raw Readiness       : {technical_readiness}",
        f"Specification Check Status    : {specification_status}",
        f"Overall Raw Readiness         : {overall_readiness}",
        f"Technical Qualification Yield : {summary['technical_qualification_yield']}",
        f"Post-spec Qualification Yield : {summary['post_spec_qualification_yield']}",
        "",
        "FAILED ASSERTIONS",
    ]
    if failed:
        for row in failed:
            lines.append(
                f"- {row['assertion_file']} :: {row['assertion_id']} :: "
                f"{row['reason']}"
            )
    else:
        lines.append("- None")
    text = "\n".join(lines) + "\n"
    (output_folder / "summary.txt").write_text(text, encoding="utf-8")
    return summary


def write_error_summary(
    output_folder: Path,
    model_key: str,
    config: dict,
    run_number: int,
    qualification: dict,
    input_variant: str,
    error: Exception,
) -> dict:
    """Write a table for a model that could not complete, then continue."""
    a0 = int(qualification.get("a0_count", 0) or 0)
    technical_aq = int(qualification.get("aq_count", 0) or 0)
    technical_yield = technical_aq / a0 if a0 else 0.0
    summary = {
        "input_variant": input_variant.upper(),
        "model_key": model_key,
        "model_name": config["display_name"],
        "model_id": config["model_id"],
        "run_number": run_number,
        "run_status": "ERROR",
        "a0_count": a0,
        "technical_aq": technical_aq,
        "pass_count": "N/A",
        "fail_count": "N/A",
        "unresolved_as_pass_count": "N/A",
        "final_aq": "N/A",
        "technical_raw_readiness": qualification.get(
            "raw_readiness", "UNRESOLVED"
        ),
        "specification_check_status": "ERROR",
        "overall_raw_readiness": "UNRESOLVED",
        "technical_qualification_yield": (
            f"{technical_aq}/{a0} ({100 * technical_yield:.2f}%)"
        ),
        "technical_qualification_yield_percent": 100 * technical_yield,
        "post_spec_qualification_yield": "N/A",
        "post_spec_qualification_yield_percent": "N/A",
        "error": str(error),
    }
    output_folder.mkdir(parents=True, exist_ok=True)
    write_json_atomic(output_folder / "summary.json", summary)
    write_summary_table(output_folder, summary)
    (output_folder / "summary.txt").write_text(
        "SPECIFICATION FAITHFULNESS CHECK\n"
        f"Model      : {config['display_name']}\n"
        f"Run        : {run_number}\n"
        "Run Status : ERROR\n"
        f"Error       : {error}\n",
        encoding="utf-8",
    )
    return summary


def run_one_model(
    model_key: str,
    config: dict,
    run_number: int,
    project_root: Path,
    output_root: Path,
    specification: str,
    assertions: list[dict],
    qualification: dict,
    input_variant: str,
    force: bool,
) -> dict:
    """Run one independent replicate over the shared technical AQ."""
    output_folder = output_root / model_key / f"run_{run_number:02d}"
    cache_folder = output_folder / "cache"
    traces_folder = output_folder / "traces"
    cache_folder.mkdir(parents=True, exist_ok=True)
    traces_folder.mkdir(parents=True, exist_ok=True)
    token = read_secret(config, project_root)
    spec_hash = hashlib.sha256(specification.encode()).hexdigest()

    print("\n" + "=" * 72)
    print(f"MODEL         : {config['display_name']}")
    print(f"MODEL ID      : {config['model_id']}")
    print(f"RUN           : {run_number}")
    print(f"ENDPOINT      : {config['api_url']}")
    print(f"TECHNICAL AQ  : {len(assertions)}")
    print("=" * 72)

    results = []
    for index, assertion in enumerate(assertions, start=1):
        cache_name = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "_",
            assertion["assertion_file"] + "__" + assertion["assertion_id"],
        ) + ".json"
        cache_file = cache_folder / cache_name
        trace_file = traces_folder / cache_name
        fingerprint = hashlib.sha256(
            (
                config["api_url"]
                + config["model_id"]
                + f"independent_run_{run_number}"
                + PROMPT_VERSION
                + (
                    OPENROUTER_REQUEST_POLICY_VERSION
                    if config["api_type"] == "openrouter"
                    else "ollama_json_v1"
                )
                + SYSTEM_PROMPT
                + spec_hash
                + assertion["fingerprint"]
            ).encode()
        ).hexdigest()

        if cache_file.is_file() and not force:
            try:
                cached = read_json(cache_file)
            except (json.JSONDecodeError, OSError):
                cached = None
            if cached and cached.get("fingerprint") == fingerprint:
                print(
                    f"[{model_key} run={run_number} {index}/{len(assertions)}] "
                    f"RESUMED {assertion['assertion_id']}",
                    flush=True,
                )
                results.append(cached)
                continue

        print(
            f"[{model_key} run={run_number} {index}/{len(assertions)}] "
            f"CHECKING {assertion['assertion_id']}",
            flush=True,
        )
        prompt = build_prompt(specification, assertion)
        trace_context = {
            "model_key": model_key,
            "model_name": config["display_name"],
            "requested_model": config["model_id"],
            "api_type": config["api_type"],
            "run_number": run_number,
            "assertion_index": index,
            "assertion_count": len(assertions),
            "assertion_file": assertion["assertion_file"],
            "assertion_id": assertion["assertion_id"],
            "property_name": assertion["property_name"],
            "cache_fingerprint": fingerprint,
        }
        try:
            with Heartbeat(model_key, assertion["assertion_id"]):
                response_text, token_usage = call_model(
                    token,
                    config,
                    prompt,
                    trace_file,
                    trace_context,
                )
            decision = parse_decision(response_text)
        except Exception as error:
            if trace_file.is_file():
                try:
                    trace = read_json(trace_file)
                    trace["pipeline_status"] = "ERROR"
                    trace["pipeline_error"] = str(error)
                    write_json_atomic(trace_file, trace)
                except (json.JSONDecodeError, OSError):
                    pass
            raise

        if trace_file.is_file():
            try:
                trace = read_json(trace_file)
                trace["pipeline_status"] = decision["decision_status"]
                trace["raw_verdict"] = decision["raw_verdict"]
                trace["mapped_verdict"] = decision["verdict"]
                write_json_atomic(trace_file, trace)
            except (json.JSONDecodeError, OSError):
                pass

        result = {
            **assertion,
            "model_key": model_key,
            "model_id": config["model_id"],
            "run_number": run_number,
            **decision,
            **token_usage,
            "trace_file": str(trace_file.relative_to(output_root)),
            "fingerprint": fingerprint,
        }
        write_json_atomic(cache_file, result)
        results.append(result)
        if decision["decision_status"] == "UNRESOLVED_AS_PASS":
            print("    -> PASS (UNRESOLVED_AS_PASS)", flush=True)
        else:
            print(f"    -> {decision['verdict']}", flush=True)

    return write_model_results(
        output_folder,
        model_key,
        config,
        run_number,
        results,
        qualification,
        input_variant,
    )


def mean(values: list[float]) -> float:
    """Return the arithmetic mean, or zero for an empty list."""
    return sum(values) / len(values) if values else 0.0


def build_average_summary(
    model_key: str,
    config: dict,
    run_summaries: list[dict],
) -> dict:
    """Average numeric results across completed independent runs."""
    completed = [row for row in run_summaries if row["run_status"] == "COMPLETE"]
    runs_requested = len(run_summaries)
    runs_completed = len(completed)
    if not completed:
        return {
            "model_key": model_key,
            "model_name": config["display_name"],
            "model_id": config["model_id"],
            "runs_requested": runs_requested,
            "runs_completed": 0,
            "a0_count": "N/A",
            "technical_aq": "N/A",
            "average_pass": "N/A",
            "average_fail": "N/A",
            "average_unresolved_as_pass": "N/A",
            "average_final_aq": "N/A",
            "average_post_spec_yield_percent": "N/A",
            "technical_raw_readiness": "UNRESOLVED",
            "complete_run_spec_passes": 0,
            "status": "ERROR",
        }

    average_pass = mean([float(row["pass_count"]) for row in completed])
    average_fail = mean([float(row["fail_count"]) for row in completed])
    average_unresolved_as_pass = mean(
        [float(row.get("unresolved_as_pass_count", 0)) for row in completed]
    )
    average_final_aq = mean([float(row["final_aq"]) for row in completed])
    average_yield = mean(
        [float(row["post_spec_qualification_yield_percent"]) for row in completed]
    )
    spec_passes = sum(
        row["specification_check_status"] == "PASS" for row in completed
    )
    return {
        "model_key": model_key,
        "model_name": config["display_name"],
        "model_id": config["model_id"],
        "runs_requested": runs_requested,
        "runs_completed": runs_completed,
        "a0_count": completed[0]["a0_count"],
        "technical_aq": completed[0]["technical_aq"],
        "average_pass": round(average_pass, 4),
        "average_fail": round(average_fail, 4),
        "average_unresolved_as_pass": round(average_unresolved_as_pass, 4),
        "average_final_aq": round(average_final_aq, 4),
        "average_post_spec_yield_percent": round(average_yield, 4),
        "technical_raw_readiness": completed[0]["technical_raw_readiness"],
        "complete_run_spec_passes": spec_passes,
        "status": "COMPLETE" if runs_completed == runs_requested else "PARTIAL",
    }


def write_average_summary_table(output_folder: Path, average: dict) -> None:
    """Write one model's three-run average as a two-column CSV table."""
    rows = [
        {"metric": "Model", "value": average["model_name"]},
        {"metric": "Model ID", "value": average["model_id"]},
        {"metric": "Runs requested", "value": average["runs_requested"]},
        {"metric": "Runs completed", "value": average["runs_completed"]},
        {"metric": "Generated set (A0)", "value": average["a0_count"]},
        {"metric": "Technical AQ", "value": average["technical_aq"]},
        {"metric": "Average PASS", "value": average["average_pass"]},
        {"metric": "Average FAIL", "value": average["average_fail"]},
        {
            "metric": "Average unresolved counted as PASS",
            "value": average["average_unresolved_as_pass"],
        },
        {"metric": "Average Final AQ", "value": average["average_final_aq"]},
        {
            "metric": "Average Post-spec Qualification Yield (%)",
            "value": average["average_post_spec_yield_percent"],
        },
        {
            "metric": "Technical Raw Readiness",
            "value": average["technical_raw_readiness"],
        },
        {
            "metric": "Specification-PASS runs",
            "value": (
                f"{average['complete_run_spec_passes']}/"
                f"{average['runs_completed']}"
            ),
        },
        {"metric": "Average status", "value": average["status"]},
    ]
    save_csv(output_folder / "average_summary_table.csv", rows, ["metric", "value"])
    write_json_atomic(output_folder / "average_summary.json", average)


def write_all_runs_and_averages(
    output_root: Path,
    all_run_summaries: list[dict],
    average_summaries: list[dict],
) -> None:
    """Store all 12 runs, store four averages, and print only the averages."""
    run_columns = [
        "model_key",
        "model_name",
        "model_id",
        "run_number",
        "run_status",
        "a0_count",
        "technical_aq",
        "pass_count",
        "fail_count",
        "unresolved_as_pass_count",
        "final_aq",
        "technical_raw_readiness",
        "specification_check_status",
        "overall_raw_readiness",
        "technical_qualification_yield",
        "post_spec_qualification_yield",
        "error",
    ]
    save_csv(
        output_root / "all_independent_runs_table.csv",
        all_run_summaries,
        run_columns,
    )
    write_json_atomic(
        output_root / "all_independent_runs_table.json",
        all_run_summaries,
    )

    average_columns = [
        "model_key",
        "model_name",
        "model_id",
        "runs_requested",
        "runs_completed",
        "a0_count",
        "technical_aq",
        "average_pass",
        "average_fail",
        "average_unresolved_as_pass",
        "average_final_aq",
        "average_post_spec_yield_percent",
        "technical_raw_readiness",
        "complete_run_spec_passes",
        "status",
    ]
    save_csv(
        output_root / "four_model_average_table.csv",
        average_summaries,
        average_columns,
    )
    write_json_atomic(
        output_root / "four_model_average_table.json",
        average_summaries,
    )

    markdown_lines = [
        "| Model | Runs | Avg PASS | Avg FAIL | Avg Unresolved-as-PASS | Avg Final AQ | Avg Post-spec QY |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in average_summaries:
        yield_value = row["average_post_spec_yield_percent"]
        yield_text = (
            "N/A" if yield_value == "N/A" else f"{float(yield_value):.2f}%"
        )
        markdown_lines.append(
            f"| {row['model_name']} | "
            f"{row['runs_completed']}/{row['runs_requested']} | "
            f"{row['average_pass']} | {row['average_fail']} | "
            f"{row['average_unresolved_as_pass']} | "
            f"{row['average_final_aq']} | {yield_text} |"
        )
    markdown = "\n".join(markdown_lines) + "\n"
    (output_root / "four_model_average_table.md").write_text(
        markdown,
        encoding="utf-8",
    )
    print("\nFOUR-MODEL THREE-RUN AVERAGES\n")
    print(markdown)


def write_token_report(
    output_root: Path,
    selected_models: list[str],
    runs_per_model: int,
) -> None:
    """Write per-call and aggregate input/output token counts to one text file."""
    rows = []
    for model_key in selected_models:
        for run_number in range(1, runs_per_model + 1):
            traces_folder = output_root / model_key / f"run_{run_number:02d}" / "traces"
            for trace_file in sorted(traces_folder.glob("*.json")):
                try:
                    trace = read_json(trace_file)
                except (json.JSONDecodeError, OSError):
                    continue
                usage = trace.get("token_usage", {})
                if not usage and isinstance(trace.get("normalized_usage"), dict):
                    legacy = trace["normalized_usage"]
                    usage = {
                        "token_usage_reported": any(
                            key in legacy
                            for key in (
                                "prompt_tokens",
                                "completion_tokens",
                                "total_tokens",
                            )
                        ),
                        "input_tokens": legacy.get("prompt_tokens", 0),
                        "output_tokens": legacy.get("completion_tokens", 0),
                        "reasoning_tokens": legacy.get("reasoning_tokens", 0),
                        "total_tokens": legacy.get("total_tokens", 0),
                    }
                reported = bool(usage.get("token_usage_reported", False))
                rows.append(
                    {
                        "model_key": model_key,
                        "model_name": trace.get(
                            "model_name", MODEL_CONFIGS[model_key]["display_name"]
                        ),
                        "run_number": run_number,
                        "assertion_id": trace.get("assertion_id", trace_file.stem),
                        "status": trace.get("status", ""),
                        "reported": reported,
                        "input_tokens": int(usage.get("input_tokens", 0) or 0),
                        "output_tokens": int(usage.get("output_tokens", 0) or 0),
                        "reasoning_tokens": int(
                            usage.get("reasoning_tokens", 0) or 0
                        ),
                        "total_tokens": int(usage.get("total_tokens", 0) or 0),
                    }
                )

    def aggregate(group_fields: tuple[str, ...]) -> list[dict]:
        groups = {}
        for row in rows:
            key = tuple(row[field] for field in group_fields)
            if key not in groups:
                groups[key] = {
                    **{field: row[field] for field in group_fields},
                    "calls": 0,
                    "reported_calls": 0,
                    "missing_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                }
            group = groups[key]
            group["calls"] += 1
            group["reported_calls"] += int(row["reported"])
            group["missing_calls"] += int(not row["reported"])
            for field in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "total_tokens",
            ):
                group[field] += row[field]
        return list(groups.values())

    by_run = aggregate(("model_key", "model_name", "run_number"))
    by_model = aggregate(("model_key", "model_name"))
    grand = aggregate(())
    grand_total = grand[0] if grand else {
        "calls": 0,
        "reported_calls": 0,
        "missing_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
    }

    lines = [
        "TOKEN USAGE REPORT",
        "",
        "Input tokens are the full prompt tokens sent to the model.",
        "Output tokens are the completion tokens returned or generated by the model.",
        "Reasoning tokens, when reported, are already included in output tokens.",
        "No prices or monetary costs are calculated by this script.",
        "",
        "PER MODEL AND RUN",
    ]
    for row in by_run:
        lines.append(
            f"- {row['model_name']} | run={row['run_number']} | "
            f"calls={row['reported_calls']}/{row['calls']} reported | "
            f"input={row['input_tokens']} | output={row['output_tokens']} | "
            f"reasoning={row['reasoning_tokens']} | total={row['total_tokens']}"
        )

    lines.extend(["", "PER MODEL"])
    for row in by_model:
        lines.append(
            f"- {row['model_name']} | "
            f"calls={row['reported_calls']}/{row['calls']} reported | "
            f"input={row['input_tokens']} | output={row['output_tokens']} | "
            f"reasoning={row['reasoning_tokens']} | total={row['total_tokens']}"
        )

    lines.extend(
        [
            "",
            "GRAND TOTAL",
            f"Calls found      : {grand_total['calls']}",
            f"Usage reported   : {grand_total['reported_calls']}",
            f"Usage missing    : {grand_total['missing_calls']}",
            f"Input tokens     : {grand_total['input_tokens']}",
            f"Output tokens    : {grand_total['output_tokens']}",
            f"Reasoning tokens : {grand_total['reasoning_tokens']}",
            f"Total tokens     : {grand_total['total_tokens']}",
            "",
            "PER API CALL",
        ]
    )
    for row in rows:
        usage_text = "YES" if row["reported"] else "NO"
        lines.append(
            f"- {row['model_name']} | run={row['run_number']} | "
            f"assertion={row['assertion_id']} | status={row['status']} | "
            f"usage={usage_text} | input={row['input_tokens']} | "
            f"output={row['output_tokens']} | "
            f"reasoning={row['reasoning_tokens']} | total={row['total_tokens']}"
        )

    report_path = output_root / "token_usage.txt"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\nTOKEN USAGE")
    print(f"Input tokens     : {grand_total['input_tokens']}")
    print(f"Output tokens    : {grand_total['output_tokens']}")
    print(f"Reasoning tokens : {grand_total['reasoning_tokens']}")
    print(f"Total tokens     : {grand_total['total_tokens']}")
    print(f"Report           : {report_path}")


def load_controlled_validation_universe(project_root: Path) -> tuple[set[str], set[str], list[dict]]:
    """Build auditable positive/negative labels from the paired folders.

    Reference positives are unchanged assertions that qualified in the normal
    technical AQ. Controlled negatives are assertions that occur only in the
    buggy inventory. Fingerprints are used so inserted assertions and changed
    line numbers do not disturb matching.
    """
    normal_output = project_root / "aq_out"
    buggy_output = project_root / "aq_buggy_out"
    normal_inventory_path = normal_output / "manifests" / "inventory_assertions.json"
    buggy_inventory_path = buggy_output / "manifests" / "inventory_assertions.json"
    normal_vacuity_path = normal_output / "manifests" / "vacuity.json"

    required = [normal_inventory_path, buggy_inventory_path, normal_vacuity_path]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise RuntimeError(
            "Balanced Accuracy requires existing normal and buggy AQ manifests. "
            f"Missing: {', '.join(missing)}"
        )

    normal_inventory = read_json(normal_inventory_path)
    buggy_inventory = read_json(buggy_inventory_path)
    normal_vacuity = read_json(normal_vacuity_path)

    def index_by_fingerprint(rows: list[dict], label: str) -> dict[str, dict]:
        indexed: dict[str, dict] = {}
        for row in rows:
            fingerprint = str(row.get("fingerprint", "")).strip()
            if not fingerprint:
                raise RuntimeError(f"{label} inventory contains an empty fingerprint.")
            if fingerprint in indexed:
                previous = indexed[fingerprint]
                raise RuntimeError(
                    f"{label} inventory contains duplicate fingerprint {fingerprint}: "
                    f"{previous.get('assertion_id')} and {row.get('assertion_id')}"
                )
            indexed[fingerprint] = row
        return indexed

    normal_by_fingerprint = index_by_fingerprint(normal_inventory, "normal")
    buggy_by_fingerprint = index_by_fingerprint(buggy_inventory, "buggy")
    normal_key_to_fingerprint = {
        (row["assertion_file"], row["assertion_id"]): row["fingerprint"]
        for row in normal_inventory
    }
    accepted_normal_keys = {
        (row["assertion_file"], row["assertion_id"])
        for row in normal_vacuity
        if row.get("status") in {"PASS", "NOT_APPLICABLE"}
    }
    reference_positive_fingerprints = {
        normal_key_to_fingerprint[key]
        for key in accepted_normal_keys
        if key in normal_key_to_fingerprint
    }
    controlled_negative_fingerprints = (
        set(buggy_by_fingerprint) - set(normal_by_fingerprint)
    )

    if not reference_positive_fingerprints:
        raise RuntimeError(
            "No reference-positive assertions were found in the normal "
            "technical AQ."
        )
    if not controlled_negative_fingerprints:
        raise RuntimeError(
            "No controlled negatives were found. buggy_assertions/ must "
            "contain additions that are not present in assertions/."
        )

    label_rows: list[dict] = []
    for fingerprint in sorted(reference_positive_fingerprints):
        normal_row = normal_by_fingerprint[fingerprint]
        buggy_row = buggy_by_fingerprint.get(fingerprint, {})
        label_rows.append(
            {
                "ground_truth": "REFERENCE_POSITIVE",
                "expected_final_verdict": "PASS",
                "fingerprint": fingerprint,
                "normal_assertion_file": normal_row.get("assertion_file", ""),
                "normal_assertion_id": normal_row.get("assertion_id", ""),
                "buggy_assertion_file": buggy_row.get("assertion_file", ""),
                "buggy_assertion_id": buggy_row.get("assertion_id", ""),
                "label_source": "normal technical AQ",
            }
        )
    for fingerprint in sorted(controlled_negative_fingerprints):
        buggy_row = buggy_by_fingerprint[fingerprint]
        label_rows.append(
            {
                "ground_truth": "CONTROLLED_NEGATIVE",
                "expected_final_verdict": "FAIL",
                "fingerprint": fingerprint,
                "normal_assertion_file": "",
                "normal_assertion_id": "",
                "buggy_assertion_file": buggy_row.get("assertion_file", ""),
                "buggy_assertion_id": buggy_row.get("assertion_id", ""),
                "label_source": "buggy-only inventory fingerprint",
            }
        )
    return (
        reference_positive_fingerprints,
        controlled_negative_fingerprints,
        label_rows,
    )


def calculate_balanced_accuracy_from_existing_results(
    project_root: Path,
    selected_models: list[str],
    runs_per_model: int,
) -> list[dict]:
    """Calculate BA from existing folders without calling any model or Jasper.

    A final PASS is an assertion present in the buggy technical AQ whose
    specification result is PASS. Assertions rejected by any earlier stage or
    by the specification check are final FAIL predictions.
    """
    (
        reference_positives,
        controlled_negatives,
        label_rows,
    ) = load_controlled_validation_universe(project_root)

    buggy_output = project_root / "aq_buggy_out"
    buggy_inventory_path = buggy_output / "manifests" / "inventory_assertions.json"
    buggy_inventory = read_json(buggy_inventory_path)
    buggy_key_to_fingerprint = {
        (row["assertion_file"], row["assertion_id"]): row["fingerprint"]
        for row in buggy_inventory
    }
    output_root = buggy_output / OUTPUT_SUBFOLDER
    output_root.mkdir(parents=True, exist_ok=True)
    label_columns = [
        "ground_truth",
        "expected_final_verdict",
        "fingerprint",
        "normal_assertion_file",
        "normal_assertion_id",
        "buggy_assertion_file",
        "buggy_assertion_id",
        "label_source",
    ]
    save_csv(output_root / "balanced_accuracy_ground_truth.csv", label_rows, label_columns)

    per_run_rows: list[dict] = []
    for model_key in selected_models:
        config = MODEL_CONFIGS[model_key]
        for run_number in range(1, runs_per_model + 1):
            run_folder = output_root / model_key / f"run_{run_number:02d}"
            summary_path = run_folder / "summary.json"
            results_path = run_folder / "spec_check_results.json"
            if not summary_path.is_file() or not results_path.is_file():
                continue
            summary = read_json(summary_path)
            if summary.get("run_status") != "COMPLETE":
                continue
            results = read_json(results_path)
            predicted_pass_fingerprints: set[str] = set()
            for row in results:
                if row.get("verdict") != "PASS":
                    continue
                key = (row.get("assertion_file"), row.get("assertion_id"))
                fingerprint = buggy_key_to_fingerprint.get(key)
                if fingerprint:
                    predicted_pass_fingerprints.add(fingerprint)

            true_positive = len(reference_positives & predicted_pass_fingerprints)
            false_negative = len(reference_positives - predicted_pass_fingerprints)
            false_positive = len(controlled_negatives & predicted_pass_fingerprints)
            true_negative = len(controlled_negatives - predicted_pass_fingerprints)
            positive_recall = true_positive / len(reference_positives)
            negative_recall = true_negative / len(controlled_negatives)
            balanced_accuracy = (positive_recall + negative_recall) / 2.0
            per_run_rows.append(
                {
                    "model_key": model_key,
                    "model_name": config["display_name"],
                    "run_number": run_number,
                    "reference_positives": len(reference_positives),
                    "controlled_negatives": len(controlled_negatives),
                    "true_positive": true_positive,
                    "false_negative": false_negative,
                    "true_negative": true_negative,
                    "false_positive": false_positive,
                    "positive_recall_percent": round(100 * positive_recall, 4),
                    "negative_recall_percent": round(100 * negative_recall, 4),
                    "balanced_accuracy_percent": round(100 * balanced_accuracy, 4),
                }
            )

    if not per_run_rows:
        raise RuntimeError(
            "No complete buggy specification-check results were found. Run "
            "the buggy checks first, or point --project-root to the project "
            "that contains aq_buggy_out/."
        )

    per_run_columns = [
        "model_key",
        "model_name",
        "run_number",
        "reference_positives",
        "controlled_negatives",
        "true_positive",
        "false_negative",
        "true_negative",
        "false_positive",
        "positive_recall_percent",
        "negative_recall_percent",
        "balanced_accuracy_percent",
    ]
    save_csv(output_root / "balanced_accuracy_per_run.csv", per_run_rows, per_run_columns)
    write_json_atomic(output_root / "balanced_accuracy_per_run.json", per_run_rows)

    average_rows: list[dict] = []
    for model_key in selected_models:
        model_rows = [row for row in per_run_rows if row["model_key"] == model_key]
        if not model_rows:
            continue
        values = [float(row["balanced_accuracy_percent"]) for row in model_rows]
        average_value = mean(values)
        variance = mean([(value - average_value) ** 2 for value in values])
        average_rows.append(
            {
                "model_key": model_key,
                "model_name": MODEL_CONFIGS[model_key]["display_name"],
                "runs_completed": len(model_rows),
                "reference_positives": model_rows[0]["reference_positives"],
                "controlled_negatives": model_rows[0]["controlled_negatives"],
                "average_balanced_accuracy_percent": round(average_value, 4),
                "population_sd_percent": round(variance ** 0.5, 4),
            }
        )

    average_columns = [
        "model_key",
        "model_name",
        "runs_completed",
        "reference_positives",
        "controlled_negatives",
        "average_balanced_accuracy_percent",
        "population_sd_percent",
    ]
    save_csv(output_root / "balanced_accuracy_average.csv", average_rows, average_columns)
    write_json_atomic(output_root / "balanced_accuracy_average.json", average_rows)

    print("\nCONTROLLED-VALIDATION BALANCED ACCURACY")
    print(
        "Reference positives are normal technical-AQ assertions; controlled "
        "negatives are buggy-only assertions."
    )
    print("\n| Model | Runs | Reference + | Controlled - | Mean BA | SD |")
    print("|---|---:|---:|---:|---:|---:|")
    for row in average_rows:
        print(
            f"| {row['model_name']} | {row['runs_completed']} | "
            f"{row['reference_positives']} | {row['controlled_negatives']} | "
            f"{row['average_balanced_accuracy_percent']:.2f}% | "
            f"{row['population_sd_percent']:.2f}% |"
        )
    print(f"\nBalanced Accuracy results: {output_root}")
    return average_rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reuse one technical AQ, then check it with Qwen, DeepSeek, "
            "GPT, and Gemini in independent repeated runs."
        )
    )
    parser.add_argument(
        "--project-root",
        help="Project containing spec/, assertions/, and the AQ pipeline.",
    )
    parser.add_argument(
        "-buggy",
        "--buggy",
        action="store_true",
        help="Use buggy_assertions/ and aq_buggy_out/.",
    )
    parser.add_argument(
        "--skip-aq",
        action="store_true",
        help=(
            "Require complete existing AQ results and fail if they are missing. "
            "Complete results are reused automatically even without this option."
        ),
    )
    parser.add_argument(
        "--rerun-aq",
        action="store_true",
        help=(
            "Invoke the AQ runner even when complete technical AQ artifacts exist. "
            "Use this after changing RTL, assertions, bind files, or AQ configuration."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore matching LLM caches and rerun the selected models.",
    )
    parser.add_argument(
        "--model",
        action="append",
        choices=list(MODEL_CONFIGS),
        help="Run only this model key; repeat for multiple models. Default: all four.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=3,
        help="Independent runs per selected model. Default: 3.",
    )
    parser.add_argument(
        "--ba-only",
        action="store_true",
        help=(
            "Read existing normal/buggy manifests and buggy model results, "
            "calculate Balanced Accuracy, and make no Jasper or API calls."
        ),
    )
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be at least 1")
    if args.skip_aq and args.rerun_aq:
        parser.error("--skip-aq and --rerun-aq cannot be used together")

    selected_models = args.model or list(MODEL_CONFIGS)
    if args.ba_only:
        project_root = find_project_root(args.project_root, False)
        calculate_balanced_accuracy_from_existing_results(
            project_root,
            selected_models,
            args.runs,
        )
        return 0

    input_variant = "buggy" if args.buggy else "normal"
    project_root = find_project_root(args.project_root, args.buggy)
    assertions_folder, _ = variant_paths(project_root, args.buggy)
    aq_output = run_technical_aq_once(
        project_root,
        args.buggy,
        args.skip_aq,
        args.rerun_aq,
    )
    specification = read_specification(project_root)
    assertions, qualification = load_aq_assertions(assertions_folder, aq_output)
    output_root = aq_output / OUTPUT_SUBFOLDER
    output_root.mkdir(parents=True, exist_ok=True)

    print("\nSHARED INPUT")
    print(f"Project root     : {project_root}")
    print(f"Input variant    : {input_variant.upper()}")
    print(f"Assertions       : {assertions_folder}")
    print(f"AQ output        : {aq_output}")
    print(f"Generated set A0 : {qualification['a0_count']}")
    print(f"Technical AQ     : {len(assertions)}")
    print(f"Models           : {', '.join(selected_models)}")
    print(f"Runs per model   : {args.runs}")
    print("Technical AQ is loaded once and shared by every selected model.")

    all_run_summaries = []
    average_summaries = []
    for model_key in selected_models:
        config = MODEL_CONFIGS[model_key]
        model_run_summaries = []
        for run_number in range(1, args.runs + 1):
            try:
                summary = run_one_model(
                    model_key,
                    config,
                    run_number,
                    project_root,
                    output_root,
                    specification,
                    assertions,
                    qualification,
                    input_variant,
                    args.force,
                )
            except Exception as error:
                print(
                    f"[MODEL ERROR] {config['display_name']} "
                    f"run={run_number}: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                summary = write_error_summary(
                    output_root / model_key / f"run_{run_number:02d}",
                    model_key,
                    config,
                    run_number,
                    qualification,
                    input_variant,
                    error,
                )
            model_run_summaries.append(summary)
            all_run_summaries.append(summary)

        average = build_average_summary(model_key, config, model_run_summaries)
        write_average_summary_table(output_root / model_key, average)
        average_summaries.append(average)

    write_all_runs_and_averages(
        output_root,
        all_run_summaries,
        average_summaries,
    )
    write_token_report(output_root, selected_models, args.runs)
    if args.buggy:
        try:
            calculate_balanced_accuracy_from_existing_results(
                project_root,
                selected_models,
                args.runs,
            )
        except Exception as error:
            print(
                f"[BA NOT AVAILABLE] {error}",
                file=sys.stderr,
                flush=True,
            )
    print(f"Results: {output_root}")
    return (
        0
        if all(row["run_status"] == "COMPLETE" for row in all_run_summaries)
        else 2
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as error:
        print(f"FOUR-MODEL SPEC CHECK ERROR: {error}", file=sys.stderr)
        raise SystemExit(2)

