"""Only pure expressions enter the immutable checker. No model-authored SV files."""
from __future__ import annotations
import re
from .bootstrap import MARKER
from .common import resolve

SYSTEM_FUNCTIONS={"$past","$stable","$rose","$fell","$changed","$onehot","$onehot0",
                  "$countones","$isunknown","$bits","$clog2","$signed","$unsigned",
                  "$size","$high","$low","$left","$right","$sampled"}
FORBIDDEN=re.compile(r"\b(?:assume|restrict|assert|cover|module|endmodule|property|endproperty|sequence|endsequence|function|endfunction|task|endtask|always|always_comb|initial|final|assign|force|release|disable|iff|bind|checker|let|generate|endgenerate|import|export)\b")

def validate_expression(expression,unit):
    if not isinstance(expression,str) or not expression.strip(): raise ValueError("Empty expression")
    if len(expression)>16000: raise ValueError("Expression too long")
    if any(x in expression for x in (";","`","//","/*","*/",'"',"\\","@","++","--")) or FORBIDDEN.search(expression):
        raise ValueError("Only a property body is allowed; no statements/macros/assumptions/clock/reset changes")
    if re.search(r"(?<![<>=!])=(?!=|>)",expression): raise ValueError("Assignments are forbidden")
    stack=[]
    for c in expression:
        if c in "([{": stack.append(c)
        elif c in ")]}":
            expected={")":"(","]":"[","}":"{"}[c]
            if not stack or stack.pop()!=expected: raise ValueError("Unbalanced delimiters")
    if stack: raise ValueError("Unbalanced delimiters")
    allowed=SYSTEM_FUNCTIONS|set(unit.get("pure_functions",[]))|{"not","and","or","first_match","strong","weak","until","until_with","s_until","s_until_with","throughout","within","intersect"}
    for fn in re.findall(r"(?<![\w$])([A-Za-z_$][\w$]*)\s*\(",expression):
        if fn not in allowed: raise ValueError(f"Function call {fn} is not on the fixed pure-function allowlist")
    if unit["clock_mode"]=="none" and ("|->" in expression or "|=>" in expression or "##" in expression or "$past" in expression):
        raise ValueError("Combinational unit needs an immediate Boolean expression")
    return expression.strip()

def statement(prop,unit):
    e=validate_expression(prop["expression"],unit)
    if not re.fullmatch(r"[A-Za-z_]\w*",prop["label"]): raise ValueError("Unsafe label")
    if unit["clock_mode"]=="none": return f"always_comb begin\n  {prop['label']}: assert ({e});\nend"
    guard=f" disable iff ({unit['reset_expression']})" if unit.get("reset_expression") else ""
    return f"{prop['label']}: assert property (@(posedge {unit['clock']}){guard} ({e}));"

def render(dataset,unit,properties):
    template=resolve(dataset,unit["template"]).read_bytes().decode("utf-8")
    if template.count(MARKER)!=1: raise ValueError("Fixed template marker invalid")
    if unit.get("input_mode")=="existing_assertions":
        from .inputs import render_slots
        return render_slots(template,unit,properties,validate_expression,lambda p:statement(p,unit))
    return template.replace(MARKER,"\n".join(statement(p,unit) for p in properties))


def remember_failure(history,problem,*,verified=False):
    """Remember distinct expression/gate failures for the same repair obligation."""
    rid=problem.get("repair_id")
    expression=problem.get("expression")
    stage=problem.get("failed_stage")
    if not rid or not isinstance(expression,str) or stage not in {"syntax","fpv","vacuity","spec"}: return
    rows=history.setdefault(rid,[])
    record={k:problem[k] for k in ("label","expression","failed_stage","status","verdict","reason","diagnostic",
                                   "question","suggested_change","evidence_quote","log_file") if k in problem}
    record["verified_for_current_run"]=verified
    for i,previous in enumerate(rows):
        if previous.get("failed_stage")==stage and previous.get("expression","").strip()==expression.strip():
            if verified or not previous.get("verified_for_current_run"): rows[i]=record
            return
    rows.append(record)

def generation_messages(spec,rtl,template,accepted,feedback,*,initial=False):
    import json
    from .llm import recovery_prompt_text
    data=json.loads(feedback)
    pending=data.get("repair_questions",[])
    recovery=data.get("output_recovery")
    # Full expression/failure history is a disk-side audit/cache, not a growing
    # list of forbidden answers in the model's context. Keep only hard interface
    # facts supported by compiler diagnostics, plus the current failure.
    prompt_feedback={k:v for k,v in data.items() if k not in {"failure_history","held_candidates","output_recovery"}}
    if "repair_questions" in prompt_feedback:
        prompt_feedback["repair_questions"]=[{k:v for k,v in row.items() if k!="unchanged_repeats"} for row in prompt_feedback["repair_questions"]]
    unavailable=sorted({name for rows in data.get("failure_history",{}).values() for row in rows
                        for name in re.findall(r"'([A-Za-z_$][\w$]*)' is not declared",row.get("diagnostic",""))})
    if unavailable:
        prompt_feedback["checker_visibility"]={"not_declared_in_checker":unavailable,"source":"compiler diagnostics for the fixed checker"}
    feedback_text=json.dumps(prompt_feedback,ensure_ascii=False)
    identifier=pending[0]["repair_id"] if pending else "candidate_001"
    example=f"BEGIN_ASSERTION {identifier}\n<property expression only>\nEND_ASSERTION"
    coverage="Aim to cover all major RTL behaviors and corner cases supported by the specification. " if initial else ""
    messages=[{"role":"system","content":
        "Generate SystemVerilog assertions from the specification and RTL. "+coverage+"Return only expression blocks in this format:\n"+example+"\n"
        "Do not write modules, assumptions, clock events or disable iff. The fixed harness adds those. "
        "Use only signals available in the checker. Fix the reported problems; add useful missing checks. "
        "Do not repeat accepted expressions. "
        "Use a distinct block ID per assertion, output each once, and finish the complete batch with END_BATCH. "
        "If no justified additional assertion exists and there is no pending repair, return NO_NEW_ASSERTIONS followed by a reason. "
        "If FEEDBACK has repair_questions, repair those items only and copy each repair_id as its block ID. "
        "For a spec question you may instead return BEGIN_REVIEW <repair_id> / evidence-based explanation / END_REVIEW when the expression should be kept. "
        "Derive corrections from the spec, RTL and fixed checker; use errors to correct that derivation. "
        "Keep the intended specification coverage, correcting a mistaken interpretation when needed; do not omit a failed check or replace it with an unrelated/trivial check."},
        {"role":"user","content":f"SPEC:\n{spec}\nRTL:\n{rtl}\nFIXED CHECKER:\n{template}\n"
         f"ACCEPTED:\n{accepted}\nFEEDBACK:\n{feedback_text}"}]
    if pending:
        # Keep the actionable repair task at the end of the request, separate
        # from the large RTL/checker context. Never resend waiting siblings.
        tasks=[{k:p[k] for k in ("repair_id","failed_stage","expression","candidate","question","reason","diagnostic",
                                "verdict","suggested_change","previous_review","evidence_quote","repair_instruction",
                                "source_file","source_line","assertion_kind","fixed_prefix","fixed_suffix","last_response_error","invalid_reply") if k in p} for p in pending]
        has_spec=any(p.get("failed_stage")=="spec" for p in pending)
        stage_instruction=("For spec items only, assess GPT's suggestion against the actual spec/RTL. Return a revised BEGIN_ASSERTION, or BEGIN_REVIEW <repair_id> with exact supporting evidence if the expression should be kept. " if has_spec else
                           "All these items require corrected BEGIN_ASSERTION expressions. BEGIN_REVIEW is forbidden for syntax, FPV, vacuity, expression or format errors. ")
        messages.append({"role":"user","content":stage_instruction+
            "Review the reported problems for the existing assertions below. For syntax/FPV/vacuity failures return a corrected expression using BEGIN_ASSERTION. "
            "Derive the correction from the design behavior; do not append unrelated conditions merely to change the text. "
            "Keep the intended specification coverage and all assumptions unchanged. Do not return accepted or waiting assertions.\n"
            "FEEDBACK:\n"+json.dumps({"repair_questions":tasks,**({"checker_visibility":prompt_feedback["checker_visibility"]} if unavailable else {})},ensure_ascii=False)})
    if recovery:
        messages.append({"role":"user","content":
            "Your last reply was rejected before qualification. Return one complete, finite batch with no duplicate blocks and finish with END_BATCH. "
            "For pending technical failures return corrected expression blocks only; REVIEW is permitted only for explicitly pending spec questions. "
            "Preserve all pending repair IDs. If there are no pending repairs, use new distinct IDs. "
            "ERROR:\n"+str(recovery.get("error",""))+"\nPREVIOUS RAW REPLY:\n"+recovery_prompt_text(recovery.get("raw_reply",""))})
    return messages


def spec_messages(spec,assertion,*,rtl="",context=None,review=""):
    import json
    return [{"role":"system","content":
        "Check this existing assertion only for contradiction with the specification. Read the target identity, RTL, bind and fixed checker context. "
        "Return PASS when no explicit specification requirement is contradicted. Omitted or unspecified behavior is not a violation and does not require the assertion to be derivable from the spec. "
        "Return FAIL only for an identifiable contradiction; quote the exact conflicting requirement and give a specific suggested correction. "
        "Review any model explanation and evidence; do not repeat a resolved information request without considering it. "
        "Use RTL to interpret signals and behavior, while explicit specification requirements are the contradiction criterion. "
        "Technical PASS does not itself establish absence of contradiction. Reserve UNRESOLVED for inability to read or assess the supplied inputs, and state exactly why; "
        "do not return UNRESOLVED merely because a behavior is not specified, and do not request arbitrary changes or additional behavioral requirements in that case. "
        'Return JSON only: {"verdict":"PASS|FAIL|UNRESOLVED","question":"...","reason":"...","evidence_quote":"...","suggested_change":"..."}.'},
        {"role":"user","content":f"Specification:\n{spec}\nTarget identity and bind:\n{json.dumps(context or {},ensure_ascii=False)}"
         f"\nTarget RTL:\n{rtl}\nAssertion and fixed context:\n{assertion}\nModel review of your prior suggestion:\n{review}"}]
