"""Strict syntax -> FPV -> antecedent reachability, using the existing AQ engine."""
from __future__ import annotations
import sys
import json
import re
from pathlib import Path
from .common import ROOT, read, save, command, resolve, EnvironmentSetupError
from .bootstrap import target_scope
from .properties import render

def runtime_diagnostics(text):
    # Jasper echoes Tcl procedure bodies. Only emitted, anchored AQ records
    # describe an event; `error "..."` and `puts "..."` definitions do not.
    return [line.strip() for line in text.splitlines() if re.match(
        r"^(?:AQ_(?:COMPILE_ERROR|TARGET_ERROR|ENVIRONMENT_ERROR|STAGE_ERROR|FPV_DETAIL|VACUITY_DETAIL|SYNTAX_FAIL|FPV_CEX|FPV_UNKNOWN|VACUOUS|VACUITY_UNKNOWN)(?:\s|$)|ERROR\b|FATAL\b|Error:|Fatal:|\[(?:ERROR|FATAL)\b)",line)]


def aq_config(dataset,unit,source_dir,output,settings):
    if unit["clock_mode"]=="clocked":
        from jasper_reset import reset_pins
        try:reset_pins(unit.get("reset_expression",""))
        except ValueError as error:
            raise EnvironmentSetupError(str(error)) from error
    generic=read(ROOT/"examples"/"aq_base.json")
    generic.update({"project_root":str(dataset),"paths":{"rtl_dir":"rtl","bind_dir":"bind",
                    "assertions_dir":str(source_dir),"output_dir":str(output)},"workers":settings.get("workers",1),
                    "jasper":{"executable":settings["jasper_executable"]},
                    "rtl":{"auto_discover":False,"files":[str(resolve(dataset,p)) for p in unit["rtl_files"]+unit.get("environment_files",[])],
                           "include_dirs":[str(resolve(dataset,p)) for p in unit.get("include_dirs",[])],"defines":unit.get("defines",[])},
                    "bind":{"mode":"none"},
                    "units":{"mapping":{source_dir.joinpath(unit["name"]+"_assertions.sv").name:{
                        "top":unit["top"],"target_scope":target_scope(dataset,unit),"clock_mode":unit["clock_mode"],"clock":unit.get("clock",""),
                        "reset_expression":unit.get("reset_expression",""),
                        "bind_files":[str(resolve(dataset,p)) for p in unit.get("bind_files",[])]}}}})
    for stage,cfg in generic["stages"].items():
        cfg["tcl_template_file"]=str(ROOT/"vendor"/"aq"/"aq_jasper_generic.tcl")
        cfg["timeout_seconds"]=settings["syntax_timeout_seconds"] if stage=="syntax" else settings["proof_timeout_seconds"]
    return generic

PASSING={"syntax":{"PASS"},"fpv":{"PROVEN"},"vacuity":{"PASS","NOT_APPLICABLE"}}


def qualify(dataset,unit,properties,directory,settings,*,stage=None):
    """Run one gate, stopping on its first failure. The caller owns the barrier.

    The optional all-gates form is used by standalone checks; it also requires
    the entire batch to pass before starting the next gate.
    """
    if stage is None:
        decisions={p["label"]:{"label":p["label"],"expression":p["expression"],"technical_pass":False} for p in properties}
        for gate in PASSING:
            rows=qualify(dataset,unit,properties,Path(directory)/gate,settings,stage=gate)
            for row in rows:
                decisions[row["label"]].update({gate:row["status"]})
                if not row["passed"]:
                    decisions[row["label"]].update({k:v for k,v in row.items() if k not in {"stage","status","passed"}})
            if any(not row["passed"] for row in rows): break
            if len(rows)!=len(properties): raise RuntimeError(f"Incomplete {gate} results without a failure")
        for result in decisions.values():
            result["technical_pass"]=all(result.get(g) in passing for g,passing in PASSING.items())
        save(Path(directory)/"decisions.json",list(decisions.values()))
        return list(decisions.values())
    if stage not in PASSING: raise ValueError(f"Unknown technical gate: {stage}")
    from .technical_cache import TechnicalCache
    cache=TechnicalCache(dataset,unit,settings,stage)
    directory=Path(directory)
    decisions=[]
    pending=[]
    batch=0
    def flush_pending():
        nonlocal batch
        if not pending: return True
        batch+=1
        expected=list(pending)
        fresh=_qualify_uncached(dataset,unit,expected,directory/f"batch_{batch:02d}",settings,stage=stage)
        pending.clear()
        labels=[row["label"] for row in fresh]
        if labels!=[prop["label"] for prop in expected[:len(fresh)]]:
            raise RuntimeError(f"Invalid {stage} result ordering")
        if not fresh or (len(fresh)!=len(expected) and all(row["passed"] for row in fresh)):
            raise RuntimeError(f"Incomplete {stage} results without a failure")
        if any(not row["passed"] for row in fresh[:-1]):
            raise RuntimeError(f"Unexpected {stage} result after first failure")
        for row in fresh:
            prop=next(p for p in properties if p["label"]==row["label"])
            cache.store(prop,row)
            decisions.append(row)
        return bool(fresh) and all(row["passed"] for row in fresh)
    for prop in properties:
        cached=cache.lookup(prop)
        if cached is None:
            pending.append(prop)
            continue
        # A cached later sibling cannot leap over an earlier unchecked item.
        if not flush_pending(): break
        decisions.append(cached)
        action="" if cached["passed"] else " -> back to LLM"
        print(f"[STEP_A_CACHE] {prop['label']} {stage} {cached['status']} HIT; no Jasper call{action}",flush=True)
        if cached.get("cache_source_log"):
            print(f"  evidence: {cached['cache_source_log']}",flush=True)
        if not cached["passed"]:
            if cached.get("reason"): print(f"  reason: {cached['reason']}",flush=True)
            break
    else:
        flush_pending()
    save(directory/"decisions.json",decisions)
    return decisions


def _qualify_uncached(dataset,unit,properties,directory,settings,*,stage):
    """Run one uncached contiguous group; AQ stops at its first failure."""
    directory=Path(directory)
    sources=directory/"input"
    sources.mkdir(parents=True,exist_ok=True)
    source=sources/(unit["name"]+"_assertions.sv")
    source.write_text(render(dataset,unit,properties))
    output=directory/"aq"
    config=directory/"aq_config.json"
    configuration=aq_config(dataset,unit,sources,output,settings)
    # No sibling job may start after the first failure. Other stages are never
    # launched by this subprocess; earlier passes are tracked by the caller.
    configuration.update({"standalone_stage":stage,"stop_on_failure":True,"workers":1})
    save(config,configuration)
    emitted=set()
    def announce(row):
        stage=row["stage"]
        label=row["label"]
        status=row.get("status","NOT_RUN")
        if (label,stage) in emitted: return
        emitted.add((label,stage))
        failed=status not in PASSING[stage]
        if not failed:
            print(f"[STEP_A] {label} {stage} {status}",flush=True)
        if failed:
            logfile=resolve(dataset,row["log_file"]) if row.get("log_file") else None
            text=logfile.read_text(errors="replace") if logfile and logfile.is_file() else ""
            diagnostics=runtime_diagnostics(text)
            reason=next((line for line in diagnostics if re.match(r"^AQ_(?:COMPILE_ERROR|TARGET_ERROR|ENVIRONMENT_ERROR|STAGE_ERROR)\b",line)),row.get("reason","No completed result"))
            setup_error=next((line for line in diagnostics if line.startswith(("AQ_TARGET_ERROR ","AQ_ENVIRONMENT_ERROR ","AQ_STAGE_ERROR ")) or (line.startswith("AQ_COMPILE_ERROR ") and "AQ_SETUP_ERROR:" in line)),None)
            print(f"[STEP_A] {label} {stage} {status}"+(" -> STOP (fixed setup/target error)" if setup_error else " -> back to LLM"),flush=True)
            print(f"  reason: {reason}",flush=True)
            if logfile: print(f"  log: {logfile}",flush=True)
            if diagnostics: print("  detail: "+" | ".join(diagnostics[:3])[:1200],flush=True)
            if setup_error:
                print("[STEP_A] SETUP ERROR -> STOP; no LLM repair; Step B not entered",flush=True)
                raise EnvironmentSetupError(f"{setup_error}; inspect {logfile}")
    def progress(line):
        if line.startswith("[STEP_A_RESULT] "):
            announce(json.loads(line[len("[STEP_A_RESULT] "):]))
    rc=command([sys.executable,ROOT/"vendor"/"aq"/"assertion_aq_pipeline.py","--config",config,
                "--project-root",dataset,stage],directory,directory/"technical.log",on_line=progress)
    if rc:
        raise RuntimeError(f"AQ engine failed (configuration/tool error), inspect {directory/'technical.log'}")
    rows={r["property_name"]:r for r in read(output/"manifests"/(stage+".json"))}
    decisions=[]
    for p in properties:
        row=rows.get(p["label"])
        if row is None: continue  # Deferred after a failure, not a failed check.
        passed=row.get("status") in PASSING[stage]
        logfile=resolve(dataset,row["log_file"]) if row.get("log_file") else None
        detail={"label":p["label"],"expression":p["expression"],"stage":stage,
                "log_file":str(logfile) if logfile else "",
                "status":row.get("status","NOT_RUN"),"passed":passed}
        announce({**row,"label":p["label"],"stage":stage})
        if not passed:
            logfile=resolve(dataset,row["log_file"]) if row.get("log_file") else None
            raw=logfile.read_text(errors="replace") if logfile and logfile.exists() else ""
            actual=runtime_diagnostics(raw)
            detail.update({"failed_stage":stage,"reason":actual[0] if actual else row.get("reason","No completed result"),
                "diagnostic":"\n".join(actual)[-10000:],"log_file":str(logfile) if logfile else ""})
        decisions.append(detail)
    save(directory/"decisions.json",decisions)
    return decisions
