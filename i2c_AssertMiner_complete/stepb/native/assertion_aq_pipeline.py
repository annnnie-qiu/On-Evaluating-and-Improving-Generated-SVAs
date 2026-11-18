"""Fail-closed assertion splitter shared with the Step A source exporter."""
from __future__ import annotations
import dataclasses
import hashlib
import re
from typing import Sequence

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


