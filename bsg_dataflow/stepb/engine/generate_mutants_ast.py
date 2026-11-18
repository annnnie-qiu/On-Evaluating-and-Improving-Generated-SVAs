"""Source-preserving, pyslang syntax-tree-guided single-edit mutations.

The syntax tree locates RTL constructs; each output is still one textual edit
against the original source. Jasper elaboration remains a mandatory later gate.
"""

from __future__ import annotations

import re

from generate_mutants import Candidate, change_numeric_labels, parse_numeric_label


STATE_CHANGES = {
    "+": ("-", "^"), "-": ("+", "^"),
    "&": ("|", "^"), "|": ("&", "^"), "^": ("&", "|"),
    "<<": (">>",), ">>": ("<<",),
    "==": ("!=",), "!=": ("==",),
    "&&": ("||",), "||": ("&&",),
}
CONTROL_CHANGES = {
    ">": (">=", "<", "<=", "=="),
    ">=": (">", "<", "<=", "=="),
    "<": ("<=", ">", ">=", "=="),
    "<=": ("<", ">", ">=", "=="),
    "==": ("!=", ">", "<"),
    "!=": ("==", ">=", "<="),
    "&&": ("||",), "||": ("&&",),
    "&": ("|",), "|": ("&",),
}

NUMERIC_LITERAL = re.compile(r"(?:\d+'[sS]?[bBdDhHoO][0-9a-fA-F_xXzZ?]+)|\d+")


def _literal_candidates(source: str, nodes: list, containers: list, family: str, kind: str) -> list[Candidate]:
    """AVC-like literal edits restricted to expressions, never declarations."""
    result = []
    for node in nodes:
        if _kind(node) not in {"IntegerVectorExpression", "IntegerLiteralExpression"}:
            continue
        if not any(_contains(container, node) for container in containers):
            continue
        start, end = _span(node)
        old = source[start:end]
        if not NUMERIC_LITERAL.fullmatch(old):
            continue
        for new in change_numeric_labels(old):
            result.append(Candidate(family, kind, start, end, new))
    return result


def _span(node) -> tuple[int, int]:
    return node.sourceRange.start.offset, node.sourceRange.end.offset


def _kind(node) -> str:
    return str(node.kind).split(".")[-1]


def _contains(outer, inner) -> bool:
    start, end = _span(outer)
    inner_start, inner_end = _span(inner)
    return start <= inner_start and inner_end <= end


def generate_ast(source: str) -> list[Candidate]:
    try:
        import pyslang
    except ImportError as exc:
        raise RuntimeError("AST backend requires pyslang: python3 -m pip install pyslang==11.0.0") from exc

    tree = pyslang.syntax.SyntaxTree.fromText(source)
    if tree.diagnostics:
        raise ValueError(f"pyslang reported {len(tree.diagnostics)} parse diagnostics; cannot safely mutate this source")
    nodes = []
    tree.root.visit(lambda node: nodes.append(node))
    result: list[Candidate] = []

    # F1: a binary operator within the RHS of a nonblocking state update.
    nba_rhs = [node.right for node in nodes if _kind(node) == "NonblockingAssignmentExpression"]
    for node in nodes:
        token = getattr(node, "operatorToken", None)
        if token is None or not any(_contains(rhs, node) for rhs in nba_rhs):
            continue
        old = token.rawText
        if old in STATE_CHANGES:
            start = token.location.offset
            for new in STATE_CHANGES[old]:
                result.append(Candidate("F1", "state_update_operator", start, start + len(old), new))
    result.extend(_literal_candidates(source, nodes, nba_rhs, "F1", "assignment_value_change_AVC"))

    # F2: a ternary with two distinct branch expressions.
    for node in nodes:
        if _kind(node) != "ConditionalExpression":
            continue
        yes_start, yes_end = _span(node.left)
        no_start, no_end = _span(node.right)
        yes, no = source[yes_start:yes_end], source[no_start:no_end]
        if yes == no:
            continue
        start, end = _span(node)
        predicate = source[start:yes_start]
        between = source[yes_end:no_start]
        result.extend((
            Candidate("F2", "ternary_source_swap_CMC", start, end,
                      predicate + no + between + yes, start),
            Candidate("F2", "ternary_true_source_reused_CMC", no_start, no_end, yes, start),
            Candidate("F2", "ternary_false_source_reused_CMC", yes_start, yes_end, no, start),
        ))
        # Unlike branch swapping, this tests selection polarity while leaving
        # both source expressions intact. Parentheses preserve precedence.
        question = source.find("?", start, yes_start)
        if question >= 0:
            condition = source[start:question].strip()
            if condition:
                result.append(Candidate("F2", "ternary_condition_inversion_CMC",
                                        start, question, f"!({condition}) ", start))

    # F3: comparison/logic operators in an if predicate; a simple guard can
    # additionally be inverted. AST containment prevents touching the body.
    predicates = [node.predicate for node in nodes if _kind(node) == "ConditionalStatement"]
    for predicate in predicates:
        for node in nodes:
            token = getattr(node, "operatorToken", None)
            if token is None or not _contains(predicate, node):
                continue
            old = token.rawText
            if old in CONTROL_CHANGES:
                start = token.location.offset
                for new in CONTROL_CHANGES[old]:
                    result.append(Candidate("F3", "if_condition_operator", start,
                                            start + len(old), new))
        start, end = _span(predicate)
        guard = source[start:end].strip()
        if guard:
            match = re.fullmatch(r"!\s*([A-Za-z_]\w*)", guard)
            replacement = match.group(1) if match else f"!({guard})"
            result.append(Candidate("F3", "if_guard_inversion", start, end, replacement))

    # Control equations often live in combinational assignments rather than
    # `if` statements (for example queue enables and opcode decode flags).
    # Restrict these edits to a continuous assignment's RHS; no declaration
    # or case label is touched by this rule.
    continuous_rhs = [assignment.right
                      for declaration in nodes if _kind(declaration) == "ContinuousAssign"
                      for assignment in declaration.assignments
                      if _kind(assignment) == "AssignmentExpression"]
    for assignment in (node for node in nodes if _kind(node) == "AssignmentExpression"):
        left_start, left_end = _span(assignment.left)
        lhs = source[left_start:left_end]
        if not (lhs.endswith("_enable") or lhs.endswith("_sel")
                or any(_contains(rhs, assignment.right) for rhs in continuous_rhs)):
            continue
        rhs = assignment.right
        for node in nodes:
            if not hasattr(node, "sourceRange") or not _contains(rhs, node):
                continue
            token = getattr(node, "operatorToken", None)
            if token is not None and token.rawText in {"&", "|", "&&", "||", "==", "!="}:
                old = token.rawText
                start = token.location.offset
                for new in CONTROL_CHANGES[old]:
                    result.append(Candidate("F3", "control_equation_operator_LOR",
                                            start, start + len(old), new))
            if _kind(node) in {"UnaryBitwiseNotExpression", "UnaryLogicalNotExpression"}:
                start, end = _span(node)
                old = source[start:end]
                if old.startswith(("~", "!")):
                    result.append(Candidate("F3", "control_equation_negation",
                                            start, end, old[1:]))

    # F4: change one plain-case numeric item while avoiding duplicate values.
    for case in (node for node in nodes if _kind(node) == "CaseStatement"):
        if case.caseKeyword.rawText != "case":
            continue
        labels = []
        safe = True
        for item in case.items:
            if _kind(item) != "StandardCaseItem":
                continue  # default item
            expressions = list(item.expressions)
            if len(expressions) != 1:
                safe = False
                break
            start, end = _span(expressions[0])
            value = source[start:end]
            parsed = parse_numeric_label(value) if re.fullmatch(
                r"(?:\d+'[sS]?[bBdDhHoO][0-9a-fA-F_xXzZ]+)|\d+", value) else None
            if parsed is None:
                safe = False
                break
            labels.append((start, end, value, parsed))
        # A case arm may map the right state to the wrong output. Mutate only
        # constants in its statement, not the label used to select the arm.
        # This also applies to symbolic multi-label decode cases.
        clauses = [item.clause for item in case.items if hasattr(item, "clause")]
        result.extend(_literal_candidates(source, nodes, clauses, "F4", "case_output_value_change_AVC"))
        if not safe:
            continue
        for index, (start, end, value, _) in enumerate(labels):
            others = {entry[3] for j, entry in enumerate(labels) if j != index}
            for replacement in change_numeric_labels(value):
                parsed = parse_numeric_label(replacement)
                if parsed is not None and parsed not in others:
                    result.append(Candidate("F4", "case_label_change", start, end, replacement))

    # F5: explicit numeric range/bit-select changes, plus simple continuous
    # assignment rewiring. The later Jasper gate rejects out-of-range edits.
    for node in nodes:
        kind = _kind(node)
        if kind == "SimpleRangeSelect":
            start, end = _span(node)
            match = re.fullmatch(r"\s*(\d+)\s*:\s*(\d+)\s*", source[start:end])
            if match:
                msb, lsb = int(match.group(1)), int(match.group(2))
                if msb > lsb:
                    result.append(Candidate("F5", "range_shrink", start, end, f"{msb}:{lsb + 1}"))
                    result.append(Candidate("F5", "range_expand", start, end, f"{msb + 1}:{lsb}"))
                    result.append(Candidate("F5", "range_shift_up_DMS", start, end, f"{msb + 1}:{lsb + 1}"))
                    if lsb > 0:
                        result.append(Candidate("F5", "range_shift_down_DMS", start, end, f"{msb - 1}:{lsb - 1}"))
        elif kind == "ElementSelect":
            start, end = _span(node)
            match = re.fullmatch(r"\[\s*(\d+)\s*\]", source[start:end])
            if match:
                index = int(match.group(1))
                for alternative in (index + 1, index - 1):
                    if alternative >= 0:
                        result.append(Candidate("F5", "bit_index_shift", start, end,
                                                f"[{alternative}]"))
    simple_assignments = []
    for declaration in (node for node in nodes if _kind(node) == "ContinuousAssign"):
        for assignment in declaration.assignments:
            if _kind(assignment) != "AssignmentExpression":
                continue
            if _kind(assignment.left) != "IdentifierName" or _kind(assignment.right) != "IdentifierName":
                continue
            start, end = _span(assignment.right)
            simple_assignments.append((start, end, source[start:end]))
    rhs_names = {name for _, _, name in simple_assignments}
    for start, end, current in simple_assignments:
        for alternative in sorted(rhs_names - {current}):
            result.append(Candidate("F5", "continuous_assign_rewire", start, end, alternative))

    return [candidate for candidate in result
            if source[candidate.start:candidate.end] != candidate.replacement]
