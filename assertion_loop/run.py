#!/usr/bin/env python3
"""Run main generation loop beside one dataset or every immediate-child dataset."""
from __future__ import annotations
import argparse
import copy
from contextlib import contextmanager
import fcntl
import json
import re
import shutil
import sys
import traceback
from pathlib import Path
from looplib.common import ROOT,EnvironmentLock,read,save,resolve,EnvironmentSetupError,digest,validate_budgets
from looplib.bootstrap import init_dataset
from looplib.llm import Client,RepeatedRequestError
from looplib.faults import build_pool,pool_protocol
from looplib.engine import run_unit,StepAAttemptLimit
from looplib.recovery import archive_step_b_results
from looplib.reporting import report,print_effectiveness

@contextmanager
def dataset_guard(dataset):
    """Protect setup/import as well as the actual solver loop from other writers."""
    output=Path(dataset)/"gen_assertions";output.mkdir(parents=True,exist_ok=True)
    with (output/".loop.lock").open("a") as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError(f"Another setup/loop is running for {dataset}; use --report-only to inspect it") from None
        yield


def datasets(target,parent):
    parent=Path(parent).expanduser().resolve()
    if target=="all":
        selected=[p for p in sorted(parent.iterdir()) if p.is_dir() and all((p/n).is_dir() for n in ("rtl","spec","bind"))]
        if not selected: raise ValueError(f"No datasets with rtl/spec/bind in {parent}")
        return selected
    p=Path(target).expanduser()
    if not p.is_absolute() and not p.is_dir(): p=parent/p
    p=p.resolve()
    if not p.is_dir(): raise ValueError(f"Dataset not found: {p}")
    return [p]

def parse_budgets(text):
    try:
        return validate_budgets([int(part.strip()) for part in text.split(",")])
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use distinct positive Step B budgets separated by commas, e.g. 1,3,5,10,15,20") from error

def parser(ablation=False,controlled=False):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("dataset",help="dataset directory/name or all")
    p.add_argument("--datasets-root",type=Path,default=ROOT.parent)
    p.add_argument("--config",type=Path,help="explicit runtime config; default restores saved experiment config or uses config.json")
    p.add_argument("--model",action="append",choices=("qwen","deepseek","gpt","gemini","opus","gpt61"),help="repeatable; default all four")
    p.add_argument("--rounds",type=int,help="main STEP B rounds only; default max_rounds=3 TODO")
    if ablation:
        p.add_argument("--budgets",type=parse_budgets,help="comma-separated Step B checkpoints; default config.ablation_budgets; runs one trajectory to their maximum")
    if controlled:
        p.add_argument("--condition",choices=("both","targeted","none"),default="both",help="paired arms share exactly the same qualified B1 baseline")
    p.add_argument("--feedback",choices=("targeted","none"),help="directed development feedback policy (ordinary ablation/main)")
    p.add_argument("--keep-running-after-saturation",action="store_true",help="explicitly retain fixed outer budget even when dev saturates")
    p.add_argument("--prepare-pool",action="store_true",help="enumerate/qualify/split original F1-F5 only; no LLM or API key required")
    p.add_argument("--fault-qualification",choices=("syntax","equivalence"),
                   help="syntax: original F1-F5 compile-and-detect flow (default); equivalence: explicit optional independent qualification")
    p.add_argument("--repeats",type=int,default=1)
    p.add_argument("--step-a-max-attempts",type=int,help="maximum Step A attempts per Step B round; default 50; failure skips to the next model")
    p.add_argument("--experiment",default="default",help="new name for a new independent experiment")
    p.add_argument("--unit",action="append",help="restrict to configured unit(s)")
    p.add_argument("--holdout-family",choices=("F1","F2","F3","F4","F5"))
    p.add_argument("--backend",choices=("ast","lexical"))
    p.add_argument("--max-output-tokens",type=int,
                   help="override the total response token budget after restoring saved config (includes reasoning where applicable)")
    p.add_argument("--init-only",action="store_true",help="write/reuse fixed templates/config, no API/Jasper")
    p.add_argument("--plan",action="store_true",help="validate configuration and emit plan, no API/Jasper/results")
    p.add_argument("--check-api",action="store_true",help="check HTTPS/model IDs/prices only; no paid generation or Jasper")
    p.add_argument("--report-only",action="store_true",help="rebuild reports only, no API/Jasper")
    return p

def main(ablation=False,argv=None,*,controlled=False):
    if controlled: ablation=True
    args=parser(ablation,controlled).parse_args(argv)
    if args.repeats<1 or (args.rounds is not None and args.rounds<1): raise ValueError("Rounds/repeats must be positive")
    if ablation and args.rounds is not None: raise ValueError("Use --budgets for ablation checkpoints; --rounds is for main only")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*",args.experiment) or args.experiment in {".",".."}:
        raise ValueError("Use a simple experiment name")
    selected_datasets=datasets(args.dataset,args.datasets_root)
    defaults=read(ROOT/"config.json")
    config=read(args.config) if args.config else copy.deepcopy(defaults)
    if args.config is None and len(selected_datasets)==1:
        previous=selected_datasets[0]/"gen_assertions"/("controlled" if controlled else "ablation" if ablation else "main")/args.experiment
        if args.holdout_family: previous=previous/args.holdout_family
        if (previous/"config_snapshot.json").is_file():
            restored=read(previous/"config_snapshot.json")
            if restored.get("protocol_version")!=16 and not (args.report_only or args.init_only):
                raise ValueError("This experiment uses the previous syntax-only fault pool. Preserve it and choose a NEW --experiment name for v16 independent fault qualification; no old data was deleted.")
            config.update(restored)
            print(f"[CONFIG] restored runtime settings: {previous/'config_snapshot.json'}",flush=True)
            if restored.get("api_request_policy")!="capability_defaults_v1":
                # Older tool versions wrote a universal limit into every
                # snapshot. Migrate that legacy default instead of requiring
                # the user to keep increasing a number or deleting data.
                config["max_output_tokens"]=None
                config["api_request_policy"]="capability_defaults_v1"
                print("[CONFIG] migrated legacy API policy: provider-default output limit; model-specific capabilities",flush=True)
    for key,model in config["models"].items():
        base=defaults.get("models",{}).get(key,{})
        if model.get("id")==base.get("id"):
            for option,value in base.items(): model.setdefault(option,copy.deepcopy(value))
    config["protocol_version"]=16
    if args.feedback:config["feedback_policy"]=args.feedback
    if args.fault_qualification:config["fault_qualification"]=args.fault_qualification
    elif args.config is None and not (args.report_only or args.init_only):
        # Normal improve runs use the user's original Step B. Legacy snapshots
        # must not silently re-enable the added equivalence calibration.
        config["fault_qualification"]="syntax"
    pool_protocol(config)
    if args.keep_running_after_saturation:config["stop_on_saturation"]=False
    if args.backend: config["backend"]=args.backend
    if args.rounds: config["max_rounds"]=args.rounds
    if ablation:
        config["ablation_budgets"]=validate_budgets(args.budgets if args.budgets is not None else config.get("ablation_budgets",defaults["ablation_budgets"]))
    config.setdefault("input_mode",defaults["input_mode"])
    config.setdefault("step_a_max_attempts",defaults["step_a_max_attempts"])
    if args.step_a_max_attempts is not None:
        config["step_a_max_attempts"]=args.step_a_max_attempts
    config["spec_policy"]="contradiction_only"
    if config["input_mode"] not in {"existing_assertions","generate"}:
        raise ValueError("input_mode must be existing_assertions (or explicit legacy generate)")
    if args.max_output_tokens is not None:
        config["max_output_tokens"]=args.max_output_tokens
        print(f"[CONFIG] CLI override max_output_tokens={args.max_output_tokens}",flush=True)
    # Legacy repair counts have different semantics; use the explicit attempt limit.
    if "step_a_max_repairs" in config:
        config.pop("step_a_max_repairs")
        print(f"[CONFIG] Ignoring legacy step_a_max_repairs; step_a_max_attempts={config['step_a_max_attempts']}",flush=True)
    if "candidates_per_round" in config:
        config.pop("candidates_per_round")
        print("[CONFIG] Removed legacy candidates_per_round: no assertion-count limit in generation or acceptance.",flush=True)
    # TODO: choose main STEP B max_rounds=3 after examining ablation.
    budgets=config["ablation_budgets"] if ablation else validate_budgets([config["max_rounds"]])
    for k in ("max_rounds","workers","syntax_timeout_seconds","proof_timeout_seconds","step_a_max_attempts"):
        if not isinstance(config[k],int) or isinstance(config[k],bool) or config[k]<1: raise ValueError(f"{k} must be a positive integer")
    if config.get("max_output_tokens") is not None and (not isinstance(config["max_output_tokens"],int) or isinstance(config["max_output_tokens"],bool) or config["max_output_tokens"]<1):
        raise ValueError("max_output_tokens must be a positive integer or null (provider default)")
    if config["max_mutants_per_family"] is not None and config["max_mutants_per_family"]<1: raise ValueError("max_mutants_per_family must be positive/null")
    if config.get("assertion_budget") is not None and config["assertion_budget"]<1: raise ValueError("assertion_budget must be positive/null")
    if not 0<=config["test_fraction"]<1 or not 0<=config["validation_fraction"]<1 or config["test_fraction"]+config["validation_fraction"]>=1:
        raise ValueError("test_fraction + validation_fraction must be <1; both must be nonnegative")
    mode="controlled" if controlled else "ablation" if ablation else "main"
    models=args.model or ["qwen","deepseek","gpt","gemini"]
    failures=[]
    for dataset in selected_datasets:
        experiment=dataset/"gen_assertions"/mode/args.experiment
        if args.holdout_family: experiment=experiment/args.holdout_family
        if args.report_only:
            rows=report(experiment)
            print_effectiveness(rows)
            print(f"[REPORT] {len(rows)} rows: {experiment/'comparison.html'}")
            continue
        try:
            with dataset_guard(dataset):
                setup=init_dataset(dataset)
                inventory=None
                if config["input_mode"]=="existing_assertions":
                    from looplib.inputs import prepare_existing
                    setup=prepare_existing(dataset,setup)
                    # This is audit metadata, not a change to the formal setup.
                    # Keep it separate so older compatible checkpoints stay valid.
                    inventory=setup.pop("input_inventory",None)
                    setup_file=dataset/"gen_assertions"/"setup"/"dataset.json"
                    if read(setup_file)!=setup:
                        old=read(setup_file)
                        save(setup_file.with_name("dataset.previous_"+digest(old)[:12]+".json"),old)
                        save(setup_file,setup)
                environment_setup=copy.deepcopy(setup)
                if args.unit:
                    missing=set(args.unit)-{u["name"] for u in setup["units"]}
                    if missing: raise ValueError(f"Unknown unit(s): {sorted(missing)}")
                    setup={**setup,"units":[u for u in setup["units"] if u["name"] in args.unit]}
                if inventory is not None:
                    print(f"[INPUTS] {dataset.name}: {inventory['mapped_source_file_count']}/{inventory['source_file_count']} source files mapped; "
                          f"{inventory['mapped_unit_assertion_count']} assertions across {len(inventory['units'])} units; "
                          f"selected_units={len(setup['units'])}; support_files={len(inventory['support_files'])}; "
                          f"explicitly_excluded_files={len(inventory['excluded_alternative_files'])}",flush=True)
                    selected={unit["name"] for unit in setup["units"]}
                    for row in inventory["units"]:
                        if row["unit"] in selected:
                            print(f"[INPUTS] {row['unit']}: {','.join(row['source_files'])} assertions={row['assertion_count']}",flush=True)
                    print(f"[INPUTS] inventory: {dataset/'gen_assertions'/'setup'/'imported'/'input_inventory.json'}",flush=True)
                if args.init_only:
                    print(f"[SETUP] {dataset/'gen_assertions'/'setup'/'dataset.json'}")
                    continue
                lock=EnvironmentLock(dataset,environment_setup)
                experiment.mkdir(parents=True,exist_ok=True)
                if args.plan:
                    save(experiment/"plan.json",{"status":"PLANNED_NOT_EXECUTED","dataset":str(dataset),"units":setup,
                        "budgets":budgets,"budget_type":"step_b_rounds","step_a_policy":"all_pass_with_attempt_limit",
                        "step_a_max_attempts":config["step_a_max_attempts"],
                        "trajectory_policy":"one trajectory per model/unit/repeat; checkpoints are cumulative prefixes",
                        "max_step_b_rounds":max(budgets),"repeats":args.repeats,
                        "input_mode":config["input_mode"],
                        "pool_protocol":pool_protocol(config),
                        "input_inventory":inventory,
                        "spec_policy":config["spec_policy"],
                        "models":{m:config["models"][m] for m in models},"spec_checker":"gpt",
                        "environment_sha256":lock.identity,"test_policy":"fixed site split, or entire held-out family; never feedback"})
                    print(f"[PLAN] No API or Jasper executed: {experiment/'plan.json'}")
                    continue
                if args.check_api:
                    client=Client(config,experiment/"api")
                    client.preflight(sorted(set(models)|{"gpt"}))
                    print("[API_OK] HTTPS verification and configured model IDs passed; no generation or Jasper executed.",flush=True)
                    continue
                if not shutil.which(config["jasper_executable"]):
                    raise RuntimeError(f"Jasper executable not found: {config['jasper_executable']}; run on the Jasper VM or use --plan")
                # A cross-process dataset lock protects the shared pool and usage ledger.
                if True:  # dataset_guard already holds the cross-process lock.
                    # All runtime settings live with the experiment, not just the replaceable toolkit.
                    snapshot=experiment/"config_snapshot.json"
                    stored={k:v for k,v in config.items() if k.lower() not in {"api_key","openrouter_api_key","secret","password"}}
                    # Different denominators must never overwrite measured B.
                    if snapshot.exists() and pool_protocol(read(snapshot))!=pool_protocol(config):
                        measured=list(experiment.rglob("rounds/round_*/round.json"))+list(experiment.rglob("budgets/budget_*/summary.json"))
                        measured=[p for p in measured if "_history" not in p.relative_to(experiment).parts]
                        if measured:
                            destinations={p.parents[2] for p in measured}
                            if (pool_protocol(read(snapshot))=="observable_equivalence_v1" and
                                pool_protocol(config)=="syntax_validated_f1_f5_v1" and
                                all((d/'recipe.json').is_file() and (d/'fixed_environment.json').is_file()
                                    and (d/'rounds/round_01/step_a_attempts').is_dir() for d in destinations)):
                                for destination in sorted(destinations):
                                    archive_step_b_results(destination,"Restore original F1-F5 compile-and-detect workflow; old denominator archived separately.")
                            else:
                                raise EnvironmentSetupError("This experiment already has Step B measurements with a different fault denominator. Data and config are unchanged; choose a new --experiment for the other --fault-qualification mode.")
                    if snapshot.exists() and read(snapshot)!=stored:
                        old=read(snapshot)
                        save(experiment/("config_snapshot.previous_"+digest(old)[:12]+".json"),old)
                    save(snapshot,stored)
                    print(f"[PROTOCOL] {pool_protocol(config)}; "+("mutation score on syntax-valid F1-F5; behavioral equivalence not established" if config.get('fault_qualification')=='syntax' else "independently qualified observable differences"),flush=True)
                    print(f"[RUN] mode={mode} models={','.join(models)} repeats={args.repeats} "
                          f"Step_B_checkpoints={','.join(map(str,budgets))} max_Step_B={max(budgets)}; "
                          f"Step_A_max_attempts={config['step_a_max_attempts']}; "
                          "one trajectory per model/unit/repeat; Step A repairs do not count",flush=True)
                    prior_inventory=read(experiment/"run_inventory.json") if (experiment/"run_inventory.json").is_file() else {}
                    inventory_row={"dataset":dataset.name,"units":[u["name"] for u in setup["units"]],
                         "models":models,"repeats":args.repeats,"budgets":budgets,
                         "conditions":(["targeted","none"] if args.condition=="both" else [args.condition]) if controlled else [config.get("feedback_policy","targeted")],
                         "pool_protocol":pool_protocol(config),"environment_sha256":lock.identity}
                    if prior_inventory.get("environment_sha256")==lock.identity:
                        for field in ("units","models","budgets","conditions"):
                            inventory_row[field]=sorted(set(inventory_row[field]+prior_inventory.get(field,[])))
                        inventory_row["repeats"]=max(args.repeats,prior_inventory.get("repeats",1))
                    save(experiment/"run_inventory.json",inventory_row)
                    if args.prepare_pool:
                        for unit in setup["units"]:
                            pool=build_pool(dataset,unit,config,lock,args.holdout_family)
                            print(f"[POOL_DONE] {unit['name']} {pool.get('qualification_counts')} dev={len(pool['dev_ids'])} validation={len(pool['validation_ids'])} test={len(pool['test_ids'])}; {pool['directory']}",flush=True)
                        continue
                    client=Client(config,experiment/"api")
                    client.preflight(sorted(set(models)|{"gpt"}))
                    for unit in setup["units"]:
                        lock.check()
                        pool=build_pool(dataset,unit,config,lock,args.holdout_family,plan_only=True)
                        for model in models:
                            for repeat in range(1,args.repeats+1):
                                destination=experiment/model/f"run_{repeat:02d}"/"units"/unit["name"]
                                try:
                                    if controlled:
                                        from looplib.controlled import run_pair
                                        conditions=["targeted","none"] if args.condition=="both" else [args.condition]
                                        arm_failures=run_pair(dataset,unit,model,repeat,config,client,pool,lock,experiment,budgets,conditions)
                                        failures.extend(arm_failures)
                                        if any(x.get("status") in {"STEP_A_LIMIT_REACHED","NO_PROGRESS"} for x in arm_failures):
                                            print(f"[NEXT_MODEL] controlled {model}/{unit['name']}: a branch reached the Step A cap or stopped without progress; remaining repeats skipped",flush=True)
                                            report(experiment)
                                            break
                                    else:
                                        run_unit(dataset,unit,model,repeat,config,client,pool,lock,destination,budgets,evaluate_test=not ablation)
                                    old_error=destination/"error.json"
                                    if old_error.is_file() and read(old_error).get("status") in {"STEP_A_LIMIT_REACHED","FAILED","NO_PROGRESS"}:
                                        # A recovered run must not retain a stale
                                        # active failure. Preserve its diagnostic.
                                        resolved=old_error.with_name("error.resolved_"+digest(read(old_error))[:12]+".json")
                                        if not resolved.exists(): shutil.copyfile(old_error,resolved)
                                        old_error.unlink()
                                except RepeatedRequestError as e:
                                    failure={"dataset":str(dataset),"model":model,"repeat":repeat,"unit":unit["name"],"status":"NO_PROGRESS","error":str(e)}
                                    save(destination/"error.json",failure);failures.append(failure)
                                    print(f"[NEXT_MODEL] NO_PROGRESS {model}/{unit['name']}: {e}",flush=True)
                                    report(experiment)
                                    break
                                except StepAAttemptLimit as e:
                                    save(destination/"error.json",{"status":"STEP_A_LIMIT_REACHED","error":str(e)})
                                    failures.append({"dataset":str(dataset),"model":model,"repeat":repeat,
                                        "unit":unit["name"],"status":"STEP_A_LIMIT_REACHED","error":str(e)})
                                    print(f"[NEXT_MODEL] {model}/{unit['name']}: {e}",flush=True)
                                    report(experiment)
                                    break
                                except Exception as e:
                                    save(destination/"error.json",{"status":"FAILED","error":str(e),"traceback":traceback.format_exc()})
                                    if isinstance(e,EnvironmentSetupError): raise
                                    failures.append({"dataset":str(dataset),"model":model,"repeat":repeat,"unit":unit["name"],"error":str(e)})
                                    print(f"[FAILED] {failures[-1]}",file=sys.stderr,flush=True)
                                report(experiment)
                    client.export()
                    save(experiment/"failures.json",[x for x in failures if x["dataset"]==str(dataset)])
                    print_effectiveness(report(experiment))
                print(f"[DONE] {experiment/'comparison.html'}",flush=True)
        except Exception as e:
            failures.append({"dataset":str(dataset),"error":str(e)})
            print(f"[FAILED] {dataset}: {e}",file=sys.stderr,flush=True)
    return 1 if failures else 0

if __name__=="__main__":
    raise SystemExit(main())
