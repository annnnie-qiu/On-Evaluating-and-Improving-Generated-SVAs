"""Preserve older runs in the dataset and reuse proposals without trusting verdicts."""
from __future__ import annotations
import shutil
import time
from pathlib import Path
from .common import read,save,EnvironmentSetupError
from .properties import remember_failure


def attempt_order(path):
    parts=path.parts
    r=next((int(p[6:]) for p in parts if p.startswith("round_") and p[6:].isdigit()),0)
    a=next((int(p[8:]) for p in parts if p.startswith("attempt_") and p[8:].isdigit()),0)
    return r,a


def matching_records(client,context):
    return [record for record in client.records() if all(record.get(key)==value for key,value in context.items())]


def finish_recovery(directory,state):
    directory=Path(directory)
    history=Path(state["history"])
    if not history.resolve().is_relative_to((directory/"_history").resolve()):
        raise EnvironmentSetupError("Invalid recovery archive path; no files moved")
    seed=directory/"recovery_seed.json"
    if not (seed.is_file() and read(seed).get("history")==str(history)):
        for name in state["archive_entries"]:
            if Path(name).name!=name or name in {".","..","_history",".recovery_pending.json"}:
                raise EnvironmentSetupError("Invalid recovery entry; no further files moved")
            path=directory/name
            if not path.exists(): continue  # Already moved before an interruption.
            target=history/name
            if target.exists(): raise EnvironmentSetupError(f"Recovery destination already exists: {target}; both copies preserved")
            shutil.move(str(path),str(target))
        save(seed,state)
    journal=directory/".recovery_pending.json"
    if journal.exists(): journal.unlink()
    return state


def recover_pending(directory):
    policy=Path(directory)/".step_b_transition_pending.json"
    if policy.is_file():finish_step_b_transition(directory,read(policy))
    journal=Path(directory)/".recovery_pending.json"
    if journal.is_file():
        print("[RECOVER] completing interrupted data preservation",flush=True)
        return finish_recovery(directory,read(journal))
    return None


def finish_step_b_transition(directory,state):
    """Move only old Step B/later-round outputs; keep B1 Step A in place."""
    directory=Path(directory)
    history=Path(state["history"])
    if not history.resolve().is_relative_to((directory/"_history").resolve()):
        raise EnvironmentSetupError("Invalid Step B archive path")
    for name in state["entries"]:
        relative=Path(name)
        if relative.is_absolute() or '..' in relative.parts:
            raise EnvironmentSetupError("Invalid Step B archive entry")
        source=directory/relative;target=history/relative
        if target.exists():continue
        if source.exists():
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.move(str(source),str(target))
    save(directory/"step_b_policy_transition.json",state)
    journal=directory/".step_b_transition_pending.json"
    if journal.exists():journal.unlink()


def archive_step_b_results(directory,reason):
    """A new denominator never reuses old B scores or later-budget proposals.

    B1 step_a_attempts are deliberately NOT moved or rewritten. The engine must
    independently verify the old full recipe before trusting these checkpoints.
    """
    directory=Path(directory)
    recover_pending(directory)
    entries=[]
    rounds=directory/"rounds"
    if rounds.exists():
        for rd in sorted(rounds.iterdir()):
            if rd.name=='round_01':
                entries.extend(p.relative_to(directory).as_posix() for p in rd.iterdir() if p.name!='step_a_attempts')
            else:entries.append(rd.relative_to(directory).as_posix())
    for name in ('budgets','summary.json','progress.json','stop.json','error.json','step_a_limit.json','spec_evidence_required.json'):
        if (directory/name).exists():entries.append(name)
    if not entries:return
    history=directory/'_history'/f'step_b_policy_{time.time_ns()}'
    state={'history':str(history),'entries':entries,'reason':reason,
           'step_a_b1_retained':True,'old_step_b_results_reused':False}
    save(directory/'.step_b_transition_pending.json',state)
    finish_step_b_transition(directory,state)
    print(f"[STEP_B_POLICY] {directory.name}: prior Step B data preserved at {history}; B1 Step A checkpoints kept",flush=True)


def prepare_recovery(directory,recipe,lock,context,client,config):
    """Archive incompatible checkpoints, import assertion TEXT, and recheck it.

    B=0 can resume the latest repair proposals. If B has completed, restart
    from the original pre-B1 batch, never from a later-budget assertion set.
    Compatible recipe hashes never enter this function.
    """
    directory=Path(directory)
    old_recipe=read(directory/"recipe.json")
    previous_seed=read(directory/"recovery_seed.json") if (directory/"recovery_seed.json").is_file() else {}
    old_lock=directory/"fixed_environment.json"
    if not old_lock.is_file() or read(old_lock).get("sha256")!=lock.identity:
        raise EnvironmentSetupError("Fixed RTL/spec/bind/setup changed; old data is preserved. Use a new experiment name for a changed environment.")
    if old_recipe.get("context")!=context:
        raise EnvironmentSetupError("Stored model/unit/repeat context differs; old data is preserved. Use a new experiment name.")
    records=matching_records(client,context)
    model_id=config["models"][context["generator_model"]]["id"]
    if any(r.get("purpose")=="generation" and r.get("model_id") not in {None,model_id} for r in records):
        raise EnvironmentSetupError("Generator model ID changed; old data is preserved. Use a new experiment name.")
    attempts=sorted(directory.glob("rounds/round_*/step_a_attempts/attempt_*/attempt.json"),key=attempt_order)
    rounds=sorted(directory.glob("rounds/round_*/round.json"),key=attempt_order)
    completed=[p for p in rounds if read(p).get("step_b_ran") or read(p).get("step_b_rounds_completed",0)>0]
    generations=sorted(directory.glob("rounds/round_*/step_a_attempts/attempt_*/generation.json"),key=attempt_order)
    generations+=sorted(directory.glob("rounds/round_*/generation.json"),key=attempt_order)
    generations=sorted(generations,key=attempt_order)
    entries={}
    sources=[]
    def add(item,source,position):
        if not isinstance(item,dict): return
        candidate=item.get("candidate") if isinstance(item.get("candidate"),dict) else {}
        expression=item.get("expression")
        if not isinstance(expression,str):
            expression=candidate.get("expression")
        if not isinstance(expression,str) or not expression.strip(): return
        rid=item.get("repair_id") or candidate.get("repair_id") or item.get("label") or f"b001_candidate_{position:03d}"
        previous=entries.get(rid,{})
        entry={"repair_id":rid,"expression":expression,"basis":str(item.get("basis",previous.get("basis",""))),
               "source_label":item.get("label") or (previous.get("source_label") if previous.get("expression")==expression else None),
               "source_file":str(source)}
        # Technical failures and paid repair replies may contain only a repair
        # ID and expression. Merge the original slot metadata by that stable ID,
        # so a repaired original is still rendered at its original location.
        origin_fields=("slot_id","label","source_label","source_file","source_line","assertion_kind","fixed_prefix","fixed_suffix")
        origin={key:previous[key] for key in origin_fields if key in previous} if previous.get("slot_id") else {}
        for proposal in (candidate,item):
            if proposal.get("slot_id"):
                origin.update({key:proposal[key] for key in origin_fields
                               if key in proposal and proposal[key] is not None})
        if origin.get("slot_id"):
            label=origin.get("source_label") or origin.get("label")
            entry.update(origin)
            if label is not None: entry.update({"label":label,"source_label":label})
            # For imported originals source_file remains the dataset-relative
            # input file. Archive provenance uses a separate field; legacy
            # generated candidates retain source_file's checkpoint meaning.
            entry["recovery_source_file"]=str(source)
        entries[rid]=entry
        if str(source) not in sources: sources.append(str(source))
    latest={}
    checkpoints=sorted(attempts+rounds,key=lambda p:(attempt_order(p)[0],attempt_order(p)[1] if p.name=="attempt.json" else 10**9))
    if checkpoints: latest=read(checkpoints[-1])
    original_inputs=directory/"input_assertions.json"
    if original_inputs.is_file():
        originals=read(original_inputs)
        if not isinstance(originals,list):
            raise EnvironmentSetupError("Invalid saved input assertions; old data is preserved.")
        for i,item in enumerate(originals,1): add(item,original_inputs,i)
    if not completed:
        # Another upgrade can interrupt revalidation before a new checkpoint.
        # Keep those saved repairs above the original input baseline.
        for i,item in enumerate(previous_seed.get("candidates",[]),1):
            add(item,directory/"recovery_seed.json",i)
    if completed:
        # Preserve the initial proposal distribution and prevent future-budget leakage.
        initial=next((p for p in generations if attempt_order(p)==(1,1) or (attempt_order(p)[0]==1 and attempt_order(p)[1]==0)),None)
        if initial and not original_inputs.is_file():
            for i,item in enumerate(read(initial).get("assertions",[]),1): add(item,initial,i)
        strategy="revalidate_original_pre_B1_batch"
    else:
        for i,item in enumerate(latest.get("accepted",[])+latest.get("held_candidates",[])+latest.get("problems",[]),1):
            add(item,checkpoints[-1] if checkpoints else directory,i)
        # An interrupted attempt may have saved a paid reply before its checkpoint.
        if generations:
            newest=generations[-1]
            newest_order=attempt_order(newest)
            checkpoint_order=attempt_order(checkpoints[-1]) if checkpoints else (0,0)
            if newest_order>=checkpoint_order:
                for i,item in enumerate(read(newest).get("assertions",[]),1): add(item,newest,i)
        strategy="revalidate_latest_pre_B1_proposals"
    if not entries and previous_seed.get("candidates"):
        # A previous upgrade may have been interrupted during revalidation,
        # before any attempt checkpoint or new generation reply was saved.
        for i,item in enumerate(previous_seed["candidates"],1): add(item,directory/"recovery_seed.json",i)
    failures={}
    if not completed:
        # Old diagnostics are useful repair advice, never trusted new verdicts.
        # Do not import later-budget feedback into a restarted B1 experiment.
        for rid,rows in previous_seed.get("failure_history",{}).items():
            for problem in rows: remember_failure(failures,{**problem,"repair_id":rid},verified=False)
        for path in attempts:
            state=read(path)
            for rid,rows in state.get("failure_history",{}).items():
                for problem in rows: remember_failure(failures,{**problem,"repair_id":rid},verified=False)
            for problem in state.get("problems",[]): remember_failure(failures,problem,verified=False)
    generated=sum(r.get("purpose")=="generation" for r in records)
    generation_calls=max(generated,latest.get("generation_calls_used",0),previous_seed.get("generation_calls_used",0))
    distinct_rounds={r.get("round") for r in records if r.get("purpose")=="generation"}
    repair_calls=max(latest.get("step_a_repairs_used",0),previous_seed.get("step_a_repairs_used",0),generated-len(distinct_rounds))
    epoch=f"revision_{time.time_ns()}"
    history=directory/"_history"/epoch
    history.mkdir(parents=True,exist_ok=False)
    # Aggregate reports will be regenerated later; keep their previous bytes too.
    experiment=directory.parents[3] if directory.parent.name=="units" and directory.parent.parent.name.startswith("run_") else None
    reports=["comparison.csv","comparison_average.csv","comparison.json","comparison.html","effectiveness.csv","failures.json","config_snapshot.json",
             "api/usage.jsonl","api/usage.csv","api/cost_by_model.csv","api/cost_by_generator.csv","api/cost_summary.json","api/pricing_snapshot.json"]
    for name in reports:
        source=experiment/name if experiment is not None else None
        if source is not None and source.is_file():
            target=history/"_experiment_reports"/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(source,target)
    for item in entries.values():
        source_key="recovery_source_file" if item.get("slot_id") else "source_file"
        original=Path(item[source_key])
        try: item[source_key]=str(history/original.relative_to(directory))
        except ValueError: pass
    for rid,rows in failures.items():
        for row in rows:
            row["repair_id"]=rid
            if row.get("log_file"):
                try: row["log_file"]=str(history/Path(row["log_file"]).relative_to(directory))
                except ValueError: pass
    state={"status":"RECOVERED_PROPOSALS_REQUIRE_REVALIDATION","recipe_sha256":recipe,
           "previous_recipe_sha256":old_recipe.get("sha256"),"history":str(history),
           "strategy":strategy,"previous_completed_step_b_rounds":len(completed),
           "candidates":list(entries.values()),"failure_history":failures,"generation_calls_used":generation_calls,
           "step_a_repairs_used":repair_calls,"cumulative_generated":max(latest.get("cumulative_generated",0),previous_seed.get("cumulative_generated",0),
               sum(len(read(path).get("assertions",[])) for path in generations)),
           "baseline_request_sha256":[r["request_sha256"] for r in records if r.get("request_sha256")],
           "archive_entries":[path.name for path in directory.iterdir() if path.name!="_history"],
           "previous_sources":sources,"note":"Old verdicts are not imported as passes. Initial prompt changes apply to future API generation, not to recovered historical replies."}
    save(directory/".recovery_pending.json",state)
    finish_recovery(directory,state)
    print(f"[RECOVER] preserved previous run: {history}",flush=True)
    print(f"[RECOVER] {len(entries)} saved candidates; {strategy}; revalidate before Step B",flush=True)
    return state
