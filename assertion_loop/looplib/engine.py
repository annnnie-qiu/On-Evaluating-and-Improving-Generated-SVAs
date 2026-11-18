"""Step B owns the outer budget; Step A must pass within its attempt limit."""
from __future__ import annotations
import json
import copy
import re
import time
from pathlib import Path
from .common import ROOT, digest, read, save, resolve, implementation_digest, validate_budgets, SpecEvidenceRequired,EnvironmentSetupError
from .llm import totals,call_json,ModelProtocolError,RepeatedRequestError,recovery_prompt_text
from .properties import generation_messages,spec_messages,validate_expression,render,remember_failure
from .technical import qualify
from .faults import measure,feedback as fault_feedback,build_pool,pool_identity
from .recovery import prepare_recovery,recover_pending,archive_step_b_results


class StepAAttemptLimit(RuntimeError):
    """The current model exhausted its Step A attempts; preserve it and move on."""


def normalize(expression):
    return re.sub(r"\s+","",expression)


def gate_log(label,stage,status,*,reason="",log_file="",failed=False):
    # Flush each result so redirected/remote terminals show the return point.
    action=" -> back to LLM" if failed else ""
    print(f"[STEP_A] {label} {stage} {status}{action}",flush=True)
    if reason:
        print(f"  reason: {' '.join(str(reason).split())}",flush=True)
    if log_file:
        print(f"  log: {log_file}",flush=True)


def validate_verdict(value):
    if not isinstance(value,dict) or value.get("verdict") not in {"PASS","FAIL","UNRESOLVED"}:
        raise ValueError("GPT judge must return PASS/FAIL/UNRESOLVED")


def generation_attempt(dataset,unit,model,config,client,spec,rtl,template,accepted,
                       feedback,context,directory,seen,lock,*,seed_response=None):
    """One batch. Outstanding repair IDs survive omitted or malformed responses."""
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    failures={rid:[dict(row) for row in rows] for rid,rows in feedback.get("failure_history",{}).items()}
    for problem in feedback.get("repair_questions",[]):
        if problem.get("failed_stage")!="revalidation": remember_failure(failures,problem,verified=True)
    feedback={**feedback,"failure_history":failures}
    if feedback.get("repair_questions") and all(p.get("failed_stage")=="response_format" and not p.get("expression") for p in feedback["repair_questions"]):
        feedback={**feedback,"repair_questions":[]}
    messages=generation_messages(spec,rtl,template,
        json.dumps([{"label":p["label"],"expression":p["expression"]} for p in accepted],ensure_ascii=False),
        json.dumps(feedback,ensure_ascii=False),
        initial=(context["step_a_attempt"]==1 and not feedback.get("repair_questions") and
                 ((context["round"]==1 and not accepted) or (context["round"]==2 and unit.get("input_mode")=="existing_assertions"))))
    if seed_response is None: save(directory/"generation_prompt.json",messages)
    pending={p["repair_id"]:dict(p) for p in feedback.get("repair_questions",[])}
    protocol_retry=bool(pending) and all(p.get("failed_stage")=="spec_protocol" for p in pending.values())
    format_only=bool(pending) and all(p.get("failed_stage")=="response_format" and not p.get("expression") for p in pending.values())
    if format_only:
        pending={}
    repairing=bool(pending) and not protocol_retry
    # Keep all candidates in this B round, including siblings waiting at a gate.
    # Only the failing item is sent for repair. Its replacement loses ALL prior
    # passes; untouched siblings retain their expression-specific gate results.
    held={p["repair_id"]:{**p,"checks":dict(p.get("checks",{}))} for p in feedback.get("held_candidates",[])}
    changes=[]
    evidence_required=[]
    raw_count=0
    def validate_batch(response):
        if not isinstance(response,dict): raise ValueError("Return a JSON object with an assertions array")
        items=response.get("assertions")
        if response.get("no_new_assertions") and not pending and accepted and not feedback.get("held_candidates"):
            if not isinstance(response.get("reason"),str) or not response["reason"].strip():raise ValueError("NO_NEW_ASSERTIONS needs a reason")
            return
        if not isinstance(items,list) or not items:
            raise ValueError("Return a nonempty assertions array, or NO_NEW_ASSERTIONS with a reason when there are no repairs")
        if any(not isinstance(item,dict) or not (isinstance(item.get("expression"),str) or isinstance(item.get("review"),str)) for item in items):
            raise ValueError("Every assertion must have an expression, or an evidence-based spec review")
        if repairing:
            ids=[item.get("repair_id") for item in items]
            if any(not isinstance(k,str) or k not in pending for k in ids) or len(ids)!=len(set(ids)):
                raise ValueError("Each repair must copy a distinct pending repair_id")
            if any("review" in item and (pending[item["repair_id"]].get("failed_stage")!="spec" or not item["review"].strip()) for item in items):
                raise ValueError("Only a spec question may receive a nonempty KEEP review")
    try:
        save(directory/"repair_feedback.json",{"repair_questions":list(pending.values()),
             "held_candidates":list(held.values()),"failure_history":failures,"instruction":feedback.get("instruction","")})
        if pending and seed_response is None:
            print("[REPAIR] requesting correction or spec review from model="+model+"; "+
                  ", ".join(f"{key}:{value.get('failed_stage','unknown')}" for key,value in pending.items()),flush=True)
            print(f"[REPAIR] exact messages: {directory/'generation_prompt.json'}",flush=True)
        if protocol_retry:
            # A GPT transport/format failure is not an assertion defect. Retry
            # GPT at the same gate with its raw reply; never ask the generator.
            for rid,problem in pending.items():
                held[rid]["spec_protocol_error"]=problem.get("protocol_error",{})
            pending.clear()
            response={"assertions":[]}
            print("[SPEC_PROTOCOL] retry GPT checker; expressions and technical passes retained",flush=True)
        elif seed_response is None:
            response=call_json(client,model,"generation",messages,{**context,"repair_ids":list(pending),
                "review_ids":[rid for rid,p in pending.items() if p.get("failed_stage")=="spec"],
                "action":"repair_existing" if pending else "improve_from_feedback",
                "output_protocol":"assertion_blocks"},directory,validate_batch)
            save(directory/"generation.json",response)
        else:
            response=seed_response
            # A saved AQ can contain more than one generation batch. Preserve it
            # as proposals and recheck all entries; no new LLM call is implied.
            if not isinstance(response.get("assertions"),list): raise ValueError("Invalid recovery candidate array")
            save(directory/"recovered_candidates.json",response)
            print(f"[RECOVER] rechecking {len(response['assertions'])} saved candidates; no generation API call",flush=True)
        if not isinstance(response,dict): raise ValueError("Return a JSON object with an assertions array")
        items=response.get("assertions")
        if response.get("no_new_assertions") and not pending and accepted and not held:
            save(directory/"saturation.json",response)
            return {"accepted":accepted,"held_candidates":[],"failure_history":failures,"problems":[],"decisions":[],"changes":[],
                    "spec_evidence_required":[],"generated_count":0,"generation_reused":False,"valid_candidate_count":0,
                    "new_qualified_count":0,"no_new_assertions":True,"stop_reason":response.get("reason","")}
        if not isinstance(items,list) or (not items and not protocol_retry):
            raise ValueError("Return a nonempty assertions array")
        raw_count=sum(isinstance(item,dict) and isinstance(item.get("expression"),str) for item in items) if seed_response is None else 0
        if repairing:
            ids=[item.get("repair_id") if isinstance(item,dict) else None for item in items]
            if any(not isinstance(k,str) or k not in pending for k in ids) or len(ids)!=len(set(ids)):
                raise ValueError("Each repair must copy a distinct pending repair_id; do not add unrelated assertions")
        for i,item in enumerate(items,1):
            repair_id=item["repair_id"] if repairing or (seed_response is not None and item.get("repair_id")) else f"b{context['round']:03d}_candidate_{i:03d}"
            try:
                previous=pending.get(repair_id,{})
                if "review" in item:
                    if previous.get("failed_stage")!="spec" or repair_id not in held or not isinstance(item["review"],str) or not item["review"].strip():
                        raise ValueError("A KEEP review must answer a pending spec question for an existing assertion")
                    held[repair_id]["spec_review"]=item["review"].strip()
                    held[repair_id]["checks"].pop("spec",None)
                    changes.append({"repair_id":repair_id,"status":"KEEP_REVIEW","before":held[repair_id]["expression"],
                                    "after":held[repair_id]["expression"],"review":item["review"].strip()})
                    pending.pop(repair_id)
                    print(f"[REVIEW] {repair_id} KEEP original expression; model evidence -> GPT spec recheck; technical passes retained",flush=True)
                    continue
                # Original inputs are checked by the real syntax gate, including
                # syntax-bad expressions. Model changes remain expression-only.
                imported=seed_response is not None and item.get("slot_id")
                expression=item.get("expression") if imported else validate_expression(item.get("expression"),unit)
                if not isinstance(expression,str) or not expression.strip(): raise ValueError("Empty source assertion")
                key=normalize(expression)
                old=previous.get("expression") or (previous.get("candidate") or {}).get("expression")
                status="NEW" if old is None else ("UNCHANGED" if normalize(old)==key else "CHANGED")
                change={"repair_id":repair_id,"status":status,"before":old,"after":expression}
                changes.append(change)
                print(f"[CHANGE] {repair_id} {status}",flush=True)
                if old is not None: print(f"  before: {old}",flush=True)
                print(f"  after: {expression}",flush=True)
                if status=="UNCHANGED" and not previous.get("needs_revalidation"):
                    count=previous.get("unchanged_repeats",0)+1
                    pending[repair_id]={**previous,"unchanged_repeats":count,
                        "repair_instruction":("Review the GPT suggestion and return BEGIN_REVIEW with spec/RTL evidence if the expression is correct; otherwise revise it."
                            if previous.get("failed_stage")=="spec" else "Your expression is unchanged. Revise it using the recorded failure; do not return it unchanged.")}
                    print(f"[NO_PROGRESS] {repair_id} model returned the same failed expression; failure retained; no checks run",flush=True)
                    continue
                slot_backed=bool(item.get("slot_id") or held.get(repair_id,{}).get("slot_id"))
                if not imported and not slot_backed and key in {normalize(p["expression"]) for p in accepted+list(held.values()) if p.get("repair_id")!=repair_id}:
                    raise ValueError("Duplicate assertion; provide a distinct repaired check")
                seen.add(key)
                label=f"loop_b{context['round']:03d}_t{context['step_a_attempt']:02d}_a{i:03d}"
                origin=held.get(repair_id,{})
                if item.get("slot_id"): origin={**origin,**item}
                if origin.get("slot_id"): label=origin["label"]
                origin={k:v for k,v in origin.items() if k!="spec_review"}
                if seed_response is not None and item.get("source_label") and item["source_label"]!=label:
                    print(f"[RECOVER] historical label={item['source_label']} -> current label={label}",flush=True)
                held[repair_id]={**origin,"label":label,
                    "repair_id":repair_id,"expression":expression,"basis":str(item.get("basis","")),
                    "generated_for_step_b_round":context["round"],"step_a_attempt":context["step_a_attempt"],"checks":{}}
                known=next((row for row in reversed(failures.get(repair_id,[]))
                            if row.get("verified_for_current_run") and row.get("failed_stage")!="spec" and row["expression"].strip()==expression.strip()),None)
                if known and seed_response is None:
                    pending[repair_id]={**known,"label":label,"repair_id":repair_id,"expression":expression,
                        "repair_instruction":"This exact version already failed. Derive a correction from the spec, RTL and fixed checker using the diagnostic below.",
                        "repeated_rejected_expression":True}
                    change["repeated_rejected_expression"]=True
                    print(f"[KNOWN_FAILURE] {repair_id} previously failed {known['failed_stage']}; no repeated Jasper/spec check; back to LLM with current diagnostic and checker facts",flush=True)
                    continue
                pending.pop(repair_id,None)
            except (ValueError,TypeError,AttributeError) as e:
                pending[repair_id]={"repair_id":repair_id,"failed_stage":"expression_policy","candidate":item,"question":str(e)}
                gate_log(repair_id,"expression_policy","FAIL",reason=str(e),failed=True)
    except (ValueError,KeyError,TypeError,IndexError) as e:
        question=f"Please return complete BEGIN_ASSERTION <id> / property expression / END_ASSERTION blocks. Problem: {e}"
        gate_log("batch","response_format","FAIL",reason=question,failed=True)
        raw=getattr(e,"raw_text","")
        if not raw and (directory/"invalid_response.txt").is_file():raw=(directory/"invalid_response.txt").read_text()
        if not raw and (directory/"generation.json").is_file():raw=(directory/"generation.json").read_text()
        output_recovery={"error":question,"raw_reply":raw}
        save(directory/"output_recovery.json",output_recovery)
        if pending:
            for problem in pending.values(): problem["last_response_error"]=question
        else:
            key=f"b{context['round']:03d}_response"
            pending[key]={"repair_id":key,"failed_stage":"response_format","question":question}
    decisions=[]
    candidates=list(held.values())
    # An omitted, invalid or unchanged repair may not advance ANY sibling gate.
    if not pending and candidates:
        for stage in ("syntax","fpv","vacuity","spec"):
            remaining=[p for p in candidates if p["checks"].get(stage)!="PASS"]
            print(f"[GATE] preparing_B={context['round']} A_attempt={context['step_a_attempt']} "
                  f"{stage} passed={len(candidates)-len(remaining)}/{len(candidates)}",flush=True)
            if stage!="spec":
                lock.check()
                rows=qualify(dataset,unit,remaining,directory/"step_a"/stage,config,stage=stage) if remaining else []
                by_label={p["label"]:p for p in remaining}
                if len({row["label"] for row in rows})!=len(rows): raise ValueError("Duplicate qualification result")
                for decision in rows:
                    prop=by_label.get(decision["label"])
                    if prop is None or decision.get("stage")!=stage: raise ValueError("Qualification mapping mismatch")
                    decisions.append(decision)
                    if decision["passed"]:
                        prop["checks"][stage]="PASS"
                    else:
                        pending[prop["repair_id"]]={**prop,**decision,"repair_id":prop["repair_id"],"expression":prop["expression"]}
                        remember_failure(failures,pending[prop["repair_id"]],verified=True)
                        break
                if not pending and len(rows)!=len(remaining): raise ValueError("Incomplete qualification result without a failure")
            else:
                for prop in remaining:
                    lock.check()
                    verdict=check_spec(dataset,unit,prop,client,spec,context,directory,seed_response is not None,rtl=rtl)
                    if verdict["verdict"]=="PASS":
                        prop["checks"][stage]="PASS"
                        prop["spec_check"]=verdict
                    else:
                        pending[prop["repair_id"]]={**prop,"repair_id":prop["repair_id"],"failed_stage":"spec_protocol" if verdict.get("response_error") else "spec","label":prop["label"],
                            "protocol_error":verdict.get("protocol_error",{}),
                            "expression":prop["expression"],"question":verdict.get("question") or verdict.get("reason") or "Does this agree with the specification?",
                            "verdict":verdict["verdict"],"suggested_change":verdict.get("suggested_change",""),
                            "reason":verdict.get("reason",""),"evidence_quote":verdict.get("evidence_quote",""),
                            "previous_review":prop.get("spec_review","")}
                        remember_failure(failures,pending[prop["repair_id"]],verified=True)
                        if verdict["verdict"]=="UNRESOLVED" and prop.get("spec_review") and not verdict.get("response_error"):
                            evidence_required.append(pending[prop["repair_id"]])
                        break
            if pending:
                print(f"[GATE] {stage} STOP -> back to LLM; later checks blocked; siblings preserved",flush=True)
                break
            print(f"[GATE] {stage} ALL PASS {len(candidates)}/{len(candidates)}",flush=True)
    new=candidates if candidates and not pending else []
    if new and any(any(p["checks"].get(s)!="PASS" for s in ("syntax","fpv","vacuity","spec")) for p in new):
        raise RuntimeError("Step A cannot accept candidates before every batch gate passes")
    save(directory/"changes.json",changes)
    save(directory/"failure_history.json",failures)
    return {"accepted":[*accepted,*new],"held_candidates":[] if new else candidates,
            "failure_history":failures,
            "problems":list(pending.values()),"decisions":decisions,"changes":changes,
            "spec_evidence_required":evidence_required,
            "output_recovery":read(directory/"output_recovery.json") if (directory/"output_recovery.json").is_file() else None,
            "generated_count":raw_count,"generation_reused":seed_response is not None or protocol_retry,"valid_candidate_count":len(candidates),"new_qualified_count":len(new)}


def check_spec(dataset,unit,prop,client,spec,context,directory,revalidate=False,*,rtl=""):
    """One GPT check; called only after the entire batch passes technical gates."""
    bindings="\n\n".join(f"FILE {p}\n{resolve(dataset,p).read_text()}" for p in unit.get("bind_files",[]))
    messages=spec_messages(spec,render(dataset,unit,[prop]),rtl=rtl,
        context={"unit":unit["name"],"top":unit["top"],"checker":unit["checker"],"bind":bindings,
                 "spec_policy":"contradiction_only",
                 "expression":prop["expression"],"origin_file":prop.get("source_file"),
                 "technical_checks":prop["checks"]},review=prop.get("spec_review",""))
    if prop.get("spec_protocol_error"):
        previous=prop["spec_protocol_error"]
        messages.append({"role":"user","content":"Your preceding checker reply was malformed or incomplete. Reassess this SAME assertion from the supplied inputs and return one finite JSON verdict. Do not add repetitive whitespace. ERROR:\n"+str(previous.get("error",""))+"\nPREVIOUS REPLY:\n"+recovery_prompt_text(previous.get("raw_reply",""))})
    save(directory/"spec_prompts"/(prop["label"]+".json"),messages)
    print(f"[STEP_A] {prop['label']} spec RUN checker=gpt",flush=True)
    try:
        verdict=call_json(client,"gpt","spec_check",messages,{**context,"assertion":prop["label"],**({"reuse_cached_response":True} if revalidate else {})},
            directory/"spec_format"/prop["label"],lambda value: validate_verdict(value))
    except RepeatedRequestError:
        raise
    except (ModelProtocolError,ValueError,KeyError,TypeError,IndexError) as e:
        invalid=directory/"spec_format"/prop["label"]/("invalid_format_repair.txt" if (directory/"spec_format"/prop["label"]/"invalid_format_repair.txt").is_file() else "invalid_response.txt")
        raw=getattr(e,"raw_text","") or (invalid.read_text() if invalid.is_file() else "")
        verdict={"verdict":"UNRESOLVED","question":f"Specification check was unresolved: {e}","reason":"Invalid checker response","response_error":True,
                 "protocol_error":{"error":str(e),"raw_reply":raw}}
    save(directory/"spec_results"/(prop["label"]+".json"),verdict)
    failed=verdict["verdict"]!="PASS"
    gate_log(prop["label"],"spec",verdict["verdict"],
        reason=(verdict.get("question") or verdict.get("reason") or "Does this agree with the specification?") if failed else "",failed=failed)
    return verdict


def run_unit(dataset,unit,model,repeat,config,client,pool,lock,directory,budgets,evaluate_test=True,*,baseline_state=None):
    budgets=validate_budgets(budgets)
    attempt_limit=config.get("step_a_max_attempts",50)
    if not isinstance(attempt_limit,int) or isinstance(attempt_limit,bool) or attempt_limit<1:
        raise ValueError("step_a_max_attempts must be a positive integer")
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    recover_pending(directory)
    def ensure_pool():
        nonlocal pool
        if pool.get("pending"):
            pool=build_pool(dataset,unit,config,lock,pool.get("heldout_family"))
    spec="\n\n".join(f"FILE {p}\n{resolve(dataset,p).read_text()}" for p in unit["spec_files"])
    rtl="\n\n".join(f"FILE {p}\n{resolve(dataset,p).read_text()}" for p in unit["rtl_files"])
    template=resolve(dataset,unit["template"]).read_text()
    context={"dataset":Path(dataset).name,"generator_model":model,"repeat":repeat,"unit":unit["name"]}
    if config.get("feedback_condition"):
        context["feedback_condition"]=config["feedback_condition"]
    # Reporting checkpoints and the outer stopping budget do not change the
    # trajectory. Extending a compatible run must reuse its completed prefix.
    trajectory_config={k:v for k,v in config.items() if k not in {"ablation_budgets","max_rounds","step_a_max_attempts"}}
    recipe_inputs={"environment":lock.identity,"config":trajectory_config,"pool":pool["identity"],"context":context,
                   "protocol":"qualified_faults_strict_barriers_v16","implementation":implementation_digest()}
    recipe=digest(recipe_inputs)
    recipe_path=directory/"recipe.json"
    transition=None
    if recipe_path.exists() and read(recipe_path)["sha256"]!=recipe:
        previous_record=read(recipe_path)
        previous=previous_record["sha256"]
        compatible=read(ROOT/"MANIFEST.json").get("resume_compatible_implementations",[])
        versions=set(compatible)|{recipe_inputs["implementation"]}
        aliases={previous}
        if previous_record.get("checkpoint_recipe_sha256")==previous:
            aliases.add(previous_record.get("current_inputs_sha256"))
        same_inputs=any(digest({**recipe_inputs,"implementation":version}) in aliases for version in versions)
        if same_inputs:
            # Only package-verified control-only upgrades may retain old verdicts.
            # Environment, model/config, pool and context must still match exactly.
            recipe=previous
            print(f"[RESUME] {model}/{unit['name']}: compatible implementation update; existing attempts and Step B checkpoints retained",flush=True)
        else:
            # The qualification policy affects only Step B. Before any B1
            # measurement, an explicitly requested syntax mode may keep exactly
            # the same Step A checkpoint. Match the full old recipe rather than
            # trusting a PASS flag or just an expression string.
            migrated=False
            # A vetted Step B-only update cannot change B1 Step A. Match the
            # complete old recipe/environment/config; archive old B scores and
            # later-budget candidates before retaining the original B1 checks.
            measured=list(directory.glob("rounds/round_*/round.json"))+list(directory.glob("budgets/budget_*/summary.json"))
            fixed=directory/"fixed_environment.json"
            if baseline_state is None and fixed.is_file():
                old_fixed=read(fixed)
                for revision in read(ROOT/"MANIFEST.json").get("pre_b1_pool_compatible_revisions",[]):
                    prior_configs=[trajectory_config]
                    if config.get('fault_qualification')=='syntax':
                        prior_configs += [{k:v for k,v in trajectory_config.items() if k!='fault_qualification'},
                                          {**trajectory_config,'fault_qualification':'equivalence'}]
                    match=None
                    for prior_config in prior_configs:
                        old_identity=pool_identity(unit,{**config,**prior_config},lock.identity,pool.get("heldout_family"),
                            generator_hashes=revision["generator_files"],selection_protocol=revision.get('selection_protocol'))
                        # Missing fault_qualification in old recipes meant equivalence.
                        if 'fault_qualification' not in prior_config:
                            old_settings={**config,**prior_config};old_settings.pop('fault_qualification',None)
                            old_identity=pool_identity(unit,old_settings,lock.identity,pool.get('heldout_family'),
                                generator_hashes=revision['generator_files'],selection_protocol=revision.get('selection_protocol'))
                        old_recipe=digest({**recipe_inputs,'config':prior_config,"pool":old_identity,"implementation":revision["implementation"]})
                        if old_recipe in aliases:match=old_identity;break
                    if match is None:continue
                    if old_fixed.get("sha256")!=lock.identity or old_fixed.get("files")!=lock.initial:continue
                    if measured:archive_step_b_results(directory,'Verified Step B-only update; retain qualified B1 Step A and remeasure original fixed pool.')
                    recipe=previous
                    transition={"reason":"Verified Step B-only update; B1 Step A unchanged; old B measurements separately archived.",
                                "previous_recipe_sha256":previous,"previous_pool_identity":match,
                                "pool_identity":pool["identity"],"step_b_measurements_before_switch":0}
                    save(directory/"pool_parser_transition.json",transition)
                    print(f"[RESUME_STEP_A] {model}/{unit['name']}: Step B-only update; saved B1 attempts retained with their original PASS/FAIL results; no repeated GPT or technical checks for completed attempts",flush=True)
                    migrated=True
                    break
            if not migrated and config.get("fault_qualification")=="syntax" and baseline_state is None:
                old_pool=build_pool(dataset,unit,{**config,"fault_qualification":"equivalence"},lock,pool.get("heldout_family"),plan_only=True)
                prior_configs=[{k:v for k,v in trajectory_config.items() if k!="fault_qualification"},
                               {**trajectory_config,"fault_qualification":"equivalence"}]
                candidates={digest({**recipe_inputs,"config":cfg,"pool":old_pool["identity"],"implementation":version})
                            for cfg in prior_configs for version in versions}
                measured=list(directory.glob("rounds/round_*/round.json"))+list(directory.glob("budgets/budget_*/summary.json"))
                fixed=directory/"fixed_environment.json"
                if candidates & aliases and measured:
                    raise EnvironmentSetupError("Cannot switch the fault denominator after Step B was measured; choose a new experiment. Existing checkpoints are unchanged.")
                if candidates & aliases and not measured and fixed.is_file():
                    old_fixed=read(fixed)
                    if old_fixed.get("sha256")==lock.identity and old_fixed.get("files")==lock.initial:
                        recipe=previous
                        transition={"from":"observable_equivalence_v1","to":"syntax_validated_f1_f5_v1",
                                    "previous_recipe_sha256":previous,"previous_pool_identity":old_pool["identity"],
                                    "pool_identity":pool["identity"],"step_b_measurements_before_switch":0,
                                    "reason":"Explicit qualification switch before B1; all Step A inputs and implementation semantics match."}
                        save(directory/"pool_protocol_transition.json",transition)
                        print(f"[RESUME_STEP_A] {model}/{unit['name']}: retained pre-B1 attempts and passed gates; Step B mode explicitly changed to syntax; no old Step B result imported",flush=True)
                        migrated=True
            if not migrated:
                prepare_recovery(directory,recipe,lock,context,client,config)
    save(recipe_path,{"sha256":recipe,"context":context,"max_step_b_rounds":max(budgets),
                      "current_inputs_sha256":digest(recipe_inputs),"checkpoint_recipe_sha256":recipe,
                      "pool_protocol":config.get("fault_qualification","equivalence"),
                      "step_a_policy":"all_pass_with_attempt_limit","step_a_max_attempts":attempt_limit})
    lock.write(directory/"fixed_environment.json")
    recovery=read(directory/"recovery_seed.json") if (directory/"recovery_seed.json").exists() else {}
    accepted=[]
    history=[]
    imported=unit.get("input_mode")=="existing_assertions"
    if imported:
        from .inputs import load_existing
        originals=load_existing(dataset,unit)
        save(directory/"input_assertions.json",originals)
    else: originals=[]
    seed=recovery.get("candidates",[]) or originals
    feedback={"instruction":"Generate useful spec-based assertions."}
    if seed and recovery:
        feedback={"repair_questions":[{**p,"failed_stage":"revalidation","needs_revalidation":True} for p in seed],
                  "instruction":"Revalidate the preserved candidates under the current implementation before continuing.",
                  "failure_history":recovery.get("failure_history",{})}
    elif seed:
        feedback={"instruction":"Check the existing assertion files. Repair only failed items; preserve correct originals."}
        print(f"[INPUT] {unit['name']}: imported {len(seed)} assertions from assertions/; no initial generation API call",flush=True)
    started=time.monotonic()
    cumulative_generated=recovery.get("cumulative_generated",0)
    generation_calls=recovery.get("generation_calls_used",0)
    repair_calls=recovery.get("step_a_repairs_used",0)
    baseline=set(recovery.get("baseline_request_sha256",[]))
    seen={normalize(p["expression"]) for p in seed}
    common_requests=set(baseline_state.get("baseline_request_sha256",[])) if baseline_state else set()
    def usage_through(round_number):
        return [x for x in client.records() if
                (all(x.get(k)==v for k,v in context.items()) and (x.get("round",0)<=round_number or x.get("request_sha256") in baseline))
                or x.get("request_sha256") in common_requests]
    def next_feedback(dev,round_number):
        if config.get("feedback_policy","targeted")=="none":
            return {"repair_questions":[],"instruction":"Review the spec and RTL for justified missing behaviors. Preserve accepted checks and assumptions. Add useful nonduplicate checks; if no justified addition exists return NO_NEW_ASSERTIONS followed by the reason."}
        return {"repair_questions":[],"step_b":fault_feedback(pool,dev,config.get("feedback_examples",8),round_number)}
    summaries=[]
    def summarize(state,budget,reason=""):
        ensure_pool()
        props=state["accepted"]
        checkpoint=directory/"budgets"/f"budget_{budget:02d}"
        checkpoint.mkdir(parents=True,exist_ok=True)
        summary_path=checkpoint/"summary.json"
        if summary_path.is_file():
            previous=read(summary_path)
            if (previous.get("recipe_sha256")==recipe and previous.get("frozen_set_sha256")==digest(props) and
                previous.get("step_b_rounds_completed")==state["round"] and
                (not evaluate_test or previous.get("test_status")=="EVALUATED_AFTER_FREEZE")):
                summaries[:]=[x for x in summaries if x["budget"]!=budget]+[previous]
                return previous
        (checkpoint/"qualified_assertions.sv").write_text(render(dataset,unit,props))
        save(checkpoint/"frozen_assertions.json",props)
        dev=state["dev_metrics"]
        validation=measure(dataset,unit,props,pool,"validation",checkpoint/"step_b_validation",config,lock)
        test=measure(dataset,unit,props,pool,"test",checkpoint/"step_b_test",config,lock) if evaluate_test else None
        selected=set(dev["selected_labels"])
        selected_props=[p for p in props if p["label"] in selected]
        (checkpoint/"selected_assertions.sv").write_text(render(dataset,unit,selected_props))
        selected_validation=measure(dataset,unit,selected_props,pool,"validation",checkpoint/"step_b_selected_validation",config,lock)
        selected_test=measure(dataset,unit,selected_props,pool,"test",checkpoint/"step_b_selected_test",config,lock) if evaluate_test else None
        row={**context,"recipe_sha256":recipe,"budget":budget,"budget_type":"step_b_rounds","rounds_used":state["step_b_rounds_completed"],
             "step_b_rounds_completed":state["step_b_rounds_completed"],
             "step_a_repairs_used":state["step_a_repairs_used"],"generation_calls_used":state["generation_calls_used"],
             "stop_reason":reason or "step_b_round_budget_exhausted","recovered_from_history":bool(recovery),
             "recovery_strategy":recovery.get("strategy"),
             "recovery_prior_api_calls":sum(x.get("request_sha256") in baseline for x in client.records()
                 if all(x.get(k)==v for k,v in context.items())),
             "generated_count":state["cumulative_generated"],"unique_expressions_seen":state["unique_expressions_seen"],
             "input_mode":unit.get("input_mode","generate"),"initial_assertion_count":len(originals),
             "spec_policy":"contradiction_only",
             "qualified_count":len(props),"selected_count":len(selected_props),
             "dev_mutants":dev["mutant_count"],"dev_detected":dev["detected_count"],"dev_lower_percent":dev["detection_lower_percent"],
             "dev_unknown_only":dev["unknown_only_count"],"dev_upper_percent":dev["detection_upper_percent"],"dev_status":dev["status"],
             "validation_mutants":validation["mutant_count"],"validation_detected":validation["detected_count"],
             "validation_unknown_only":validation["unknown_only_count"],"validation_status":validation["status"],
             "validation_lower_percent":validation["detection_lower_percent"],"validation_upper_percent":validation["detection_upper_percent"],
             "selected_validation_lower_percent":selected_validation["detection_lower_percent"],
             "test_status":"EVALUATED_AFTER_FREEZE" if evaluate_test else "SEALED_NOT_EVALUATED",
             "test_mutants":test["mutant_count"] if test else None,"test_detected":test["detected_count"] if test else None,
             "test_lower_percent":test["detection_lower_percent"] if test else None,"test_upper_percent":test["detection_upper_percent"] if test else None,
             "selected_test_detected":selected_test["detected_count"] if selected_test else None,
             "selected_test_lower_percent":selected_test["detection_lower_percent"] if selected_test else None,
             "dev_preserves_detected_set":dev["preserves_detected_set"],**state["usage"],"output":str(checkpoint),"status":"EARLY_STOPPED" if state["round"]<budget else "COMPLETED",
             "feedback_policy":config.get("feedback_policy","targeted"),"pool_identity":pool["identity"],
             "pool_protocol":pool.get("protocol","unspecified"),"pool_validity":pool.get("validity"),
             "pool_candidate_count":pool.get("candidate_count"),"pool_valid_mutants":len(pool.get("mutants",[])),
             **{"pool_"+key.lower():value for key,value in pool.get("qualification_counts",{}).items()},"frozen_environment":lock.identity,
             "frozen_set_sha256":digest(props),"shared_baseline_source":state.get("shared_baseline_source"),"baseline_usage":baseline_state.get("usage") if baseline_state else None}
        save(checkpoint/"summary.json",row)
        # A checkpoint row is replaceable; historical attempts remain immutable.
        summaries[:]=[x for x in summaries if x["budget"]!=budget]+[row]
        save(directory/"summary.json",sorted(summaries,key=lambda x:x["budget"]))
        return row

    for r in range(1,max(budgets)+1):
        lock.check()
        rd=directory/"rounds"/f"round_{r:02d}"
        saved=rd/"round.json"
        if saved.exists():
            state=read(saved)
            if state["recipe_sha256"]!=recipe: raise RuntimeError("Stale Step B round checkpoint")
            accepted=state["accepted"]
            feedback=state["next_feedback"]
            cumulative_generated=state["cumulative_generated"]
            generation_calls=state["generation_calls_used"]
            repair_calls=state["step_a_repairs_used"]
            seen=set(state["seen_expressions"])
            history.append(state)
            if r in budgets:summarize(state,r,state.get("stop_reason",""))
            if state.get("stop_reason") and config.get("stop_on_saturation",False):break
            continue
        rd.mkdir(parents=True,exist_ok=True)
        if r==1 and baseline_state is not None:
            if (baseline_state.get("baseline_environment")!=lock.identity or baseline_state.get("baseline_unit")!=unit["name"] or
                baseline_state.get("baseline_pool_identity")!=pool["identity"] or not baseline_state.get("step_a_all_passed")):
                raise ValueError("Controlled baseline environment/unit/pool/qualification mismatch")
            accepted=copy.deepcopy(baseline_state["accepted"])
            if any(any(p.get("checks",{}).get(stage)!="PASS" for stage in ("syntax","fpv","vacuity","spec")) for p in accepted):
                raise ValueError("Shared baseline has incomplete Step A qualification")
            ensure_pool()
            feedback=next_feedback(baseline_state["dev_metrics"],1)
            state={**copy.deepcopy(baseline_state),"recipe_sha256":recipe,"next_feedback":feedback,
                   "shared_baseline_source":baseline_state.get("baseline_source"),"usage":totals(usage_through(1))}
            if not config.get("stop_on_saturation",False):state.pop("stop_reason",None)
            cumulative_generated=state["cumulative_generated"];generation_calls=state["generation_calls_used"]
            repair_calls=state["step_a_repairs_used"];seen=set(state["seen_expressions"])
            save(saved,state);(rd/"qualified_assertions.sv").write_text(render(dataset,unit,accepted));history.append(state)
            print(f"[BASELINE_SHARED] {model}/{unit['name']} AQ={len(accepted)}; no repeat generation, GPT or Jasper",flush=True)
            if r in budgets:summarize(state,r,state.get("stop_reason",""))
            if state.get("stop_reason") and config.get("stop_on_saturation",False):break
            continue
        aq_before=len(accepted)
        attempts_used=0
        problems=[]
        attempt=0
        while True:
            attempt+=1
            lock.check()
            ad=rd/"step_a_attempts"/f"attempt_{attempt:02d}"
            attempt_file=ad/"attempt.json"
            print(f"[STEP_A] {Path(dataset).name}/{model}/run_{repeat:02d}/{unit['name']} "
                  f"preparing_B={r}/{max(budgets)} attempt={attempt} repair={attempt-1} "
                  f"AQ={len(accepted)} historical_repairs={recovery.get('step_a_repairs_used',0)}"+
                  (" checkpoint=resume" if attempt_file.exists() else ""),flush=True)
            if attempt_file.exists():
                result=read(attempt_file)
                if result["recipe_sha256"]!=recipe: raise RuntimeError("Stale Step A attempt checkpoint")
                cumulative_generated=result["cumulative_generated"]
                generation_calls=result["generation_calls_used"]
                repair_calls=result["step_a_repairs_used"]
                seen=set(result["seen_expressions"])
            else:
                attempt_context={**context,"round":r,"step_b_round":r,"step_a_attempt":attempt,"step_a_repair":attempt-1}
                try:
                    result=generation_attempt(dataset,unit,model,config,client,spec,rtl,template,
                        accepted,feedback,attempt_context,ad,seen,lock,
                        seed_response={"assertions":seed} if seed and r==1 and attempt==1 else None)
                except (Exception,KeyboardInterrupt) as error:
                    save(ad/"interrupted.json",{"status":"STEP_A_INTERRUPTED","step_b_rounds_completed":len(history),
                        "step_a_attempt":attempt,"error_type":type(error).__name__,"error":str(error),
                        "pending_repairs":feedback.get("repair_questions",[]),"held_candidates":feedback.get("held_candidates",[]),
                        "failure_history":feedback.get("failure_history",{})})
                    print(f"[STEP_A] INTERRUPTED ({type(error).__name__}); Step B remains {len(history)}/{max(budgets)}; "
                          f"checkpoint={ad}",flush=True)
                    raise
                lock.check()
                cumulative_generated+=result["generated_count"]
                generation_calls+=int(not result.get("generation_reused"))
                repair_calls+=int(attempt>1 and not result.get("generation_reused"))
                result.update({"recipe_sha256":recipe,"step_b_round":r,"step_a_attempt":attempt,
                    "cumulative_generated":cumulative_generated,"generation_calls_used":generation_calls,
                    "step_a_repairs_used":repair_calls,"seen_expressions":sorted(seen)})
                save(attempt_file,result)
                (ad/"qualified_assertions.sv").write_text(render(dataset,unit,result["accepted"]))
            accepted=result["accepted"]
            problems=result["problems"]
            attempts_used=attempt
            if not problems and accepted:
                marker=directory/"step_a_limit.json"
                if marker.is_file() and read(marker).get("step_b_round")==r:
                    marker.unlink()
                print(f"[STEP_A] PASS -> Step B {r}/{max(budgets)}; attempts={attempt} AQ={len(accepted)}",flush=True)
                break
            if not problems: raise RuntimeError("Invalid empty Step A checkpoint")
            save(rd/"pending_repairs.json",{"step_a_attempt":attempt,"problems":problems,"held_candidates":result.get("held_candidates",[]),
                 "failure_history":result.get("failure_history",{})})
            if result.get("spec_evidence_required"):
                evidence={"status":"SPEC_EVIDENCE_REQUIRED","step_b_rounds_completed":len(history),
                          "step_a_attempt":attempt,"checkpoint":str(attempt_file),
                          "questions":result["spec_evidence_required"],
                          "note":"Selected model reviewed the GPT suggestion; GPT still cannot assess contradiction against the supplied inputs. Unspecified behavior alone is not a violation. No Step A pass or Step B advancement."}
                save(directory/"spec_evidence_required.json",evidence)
                questions="; ".join(p.get("question") or p.get("reason") or "Required specification evidence is missing" for p in evidence["questions"])
                print(f"[STEP_A] SPEC_EVIDENCE_REQUIRED -> STOP; Step B remains {len(history)}/{max(budgets)}; {questions}; checkpoint={attempt_file}",flush=True)
                raise SpecEvidenceRequired(f"{questions}; checkpoint retained: {attempt_file}; add the missing specification evidence before resuming")
            if attempt>=attempt_limit:
                records=[row for row in client.records() if all(row.get(key)==value for key,value in context.items())]
                stopped={"status":"STEP_A_LIMIT_REACHED",**context,"step_a_max_attempts":attempt_limit,
                    "step_a_attempts_used":attempt,"step_b_round":r,"step_b_rounds_completed":len(history),
                    "qualified_count":len(accepted),"problems":problems,"checkpoint":str(attempt_file),
                    "generation_calls_used":generation_calls,"step_a_repairs_used":repair_calls,"usage":totals(records)}
                save(directory/"step_a_limit.json",stopped)
                print(f"[STEP_A] LIMIT REACHED {attempt}/{attempt_limit}; model={model}; "
                      f"Step B remains {len(history)}/{max(budgets)} -> NEXT MODEL; checkpoint={attempt_file}",flush=True)
                raise StepAAttemptLimit(f"Step A failed after {attempt} attempts (limit={attempt_limit}); "
                                       f"checkpoint preserved: {attempt_file}")
            stages=", ".join(f"{p['repair_id']}:{p['failed_stage']}" for p in problems)
            print(f"[STEP_A] pending={len(problems)} ({stages}) -> back to LLM model={model}; "
                  f"Step B remains {len(history)}/{max(budgets)}",flush=True)
            feedback={"repair_questions":problems,
                      "held_candidates":result.get("held_candidates",[]),
                      "failure_history":result.get("failure_history",{}),
                      "instruction":"Repair every pending item using its repair_id. Keep accepted assertions and assumptions unchanged.",
                      "step_b":feedback.get("step_b",{}),"output_recovery":result.get("output_recovery")}
        # A repair never advances r. Only completed Step B measurements are counted.
        save(rd/"pending_repairs.json",{"step_a_attempt":attempts_used,"problems":[],"failure_history":result.get("failure_history",{})})
        ensure_pool()
        dev=measure(dataset,unit,accepted,pool,"dev",rd/"step_b",config,lock)
        feedback=next_feedback(dev,r)
        usage=usage_through(r)
        state={"recipe_sha256":recipe,"round":r,"step_b_round":r,"step_b_rounds_completed":r,
               "step_a_attempts_this_round":attempts_used,"step_a_repairs_this_round":attempts_used-1,
               "generation_calls_used":generation_calls,"step_a_repairs_used":repair_calls,
               "cumulative_generated":cumulative_generated,"unique_expressions_seen":len(seen),"seen_expressions":sorted(seen),
               "new_qualified_count":len(accepted)-aq_before,"qualified_count":len(accepted),"accepted":accepted,
               "step_a_all_passed":True,"next_feedback":feedback,
               "step_b_ran":True,"dev_metrics":dev,"usage":totals(usage),
               "elapsed_seconds_this_process":time.monotonic()-started}
        prior_detected=set(history[-1]["dev_metrics"]["detected_ids"]) if history else set()
        state["new_detected_ids"]=sorted(set(dev["detected_ids"])-prior_detected)
        state["accepted_sha256"]=digest(accepted)
        if config.get("stop_on_saturation",False):
            actionable=fault_feedback(pool,dev,None,r)["actionable_miss_count"]
            if result.get("no_new_assertions"):state["stop_reason"]="NO_JUSTIFIED_ADDITION: "+result.get("stop_reason","")
            elif dev["mutant_count"]==0:state["stop_reason"]="NO_EVALUABLE_DEV_FAULTS"
            elif not dev["undetected_ids"]:state["stop_reason"]="DEV_SATURATED"
            elif actionable==0:state["stop_reason"]="ONLY_UNRESOLVED_DETECTION_GAPS"
        (rd/"qualified_assertions.sv").write_text(render(dataset,unit,accepted))
        save(saved,state)
        history.append(state)
        print(f"[STEP_B] {unit['name']} {r}/{max(budgets)} completed AQ={len(accepted)} "
              f"A_repairs_total={repair_calls} generation_calls={generation_calls}",flush=True)
        save(directory/"progress.json",{"status":"QUALIFIED_CHECKPOINT","completed_step_b_rounds":r,"qualified_count":len(accepted),
             "new_detected_ids":state["new_detected_ids"],"dev_metrics":dev,"last_round":str(saved)})
        if r in budgets:summarize(state,r,state.get("stop_reason",""))
        if state.get("stop_reason") and config.get("stop_on_saturation",False):
            save(directory/"stop.json",{"reason":state["stop_reason"],"completed_step_b_rounds":r,"recipe_sha256":recipe})
            print(f"[STOP] {state['stop_reason']}; frozen AQ={len(accepted)}; no fabricated extra rounds",flush=True)
            break
    # Budgets beyond a justified early stop use the SAME frozen set, with the
    # actual completed round count and stop reason stated explicitly.
    ensure_pool()
    for budget in budgets:
        eligible=[state for state in history if state["round"]<=budget]
        if eligible:
            state=eligible[-1]
            summarize(state,budget,state.get("stop_reason",""))
    save(directory/"summary.json",summaries)
    final=directory/"final"
    final.mkdir(exist_ok=True)
    (final/"qualified_assertions.sv").write_text(render(dataset,unit,accepted))
    save(final/"assertions.json",accepted)
    return summaries
