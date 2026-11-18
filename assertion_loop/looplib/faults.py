"""Fixed single-edit pools, development feedback, and sealed final test scoring."""
from __future__ import annotations
import csv
import hashlib
import random
import sys
from collections import defaultdict
from pathlib import Path
from .common import ROOT, digest, read, save, resolve, EnvironmentSetupError
from .jasper_jobs import JasperAttempt
from .bootstrap import target_scope
from .properties import render

sys.path.insert(0,str(ROOT/"vendor"/"faults"))
from generate_mutants import generate
from validate_mutants import syntax_tcl, parse_validation_log
from collect_jasper import collect

SYNTAX_PROTOCOL = "syntax_validated_f1_f5_v1"

def pool_protocol(settings):
    mode=settings.get("fault_qualification","equivalence")
    if mode=="syntax": return SYNTAX_PROTOCOL
    if mode=="equivalence":
        from .equivalence import VERSION
        return VERSION
    raise ValueError("fault_qualification must be syntax or equivalence")

def formal_config(dataset,unit,assertion_file,labels,settings):
    return {"project_root":str(dataset),"top":unit["top"],"target_scope":target_scope(dataset,unit),"assertions":labels,
            "rtl_files":[str(resolve(dataset,p)) for p in unit["rtl_files"]+unit.get("environment_files",[])],
            "assertion_file":str(assertion_file),
            "bind_files":[str(resolve(dataset,p)) for p in unit.get("bind_files",[])],
            "include_dirs":[str(resolve(dataset,p)) for p in unit.get("include_dirs",[])],
            "defines":unit.get("defines",[]),"clock_mode":unit["clock_mode"],
            "clock":unit.get("clock",""),"reset_expression":unit.get("reset_expression",""),
            "jasper_executable":settings["jasper_executable"],"jasper_version_tag":settings.get("jasper_version_tag","user-not-set"),
            "timeout_seconds":settings["proof_timeout_seconds"]}

def partition(mutants,seed,test_fraction,heldout=None,validation_fraction=0.25):
    """Group by target+source line; related alternatives never cross partitions."""
    heldout_test=[m for m in mutants if m["family"]==heldout] if heldout else []
    if heldout: mutants=[m for m in mutants if m["family"]!=heldout]
    # One source site stays together even when several mutation families touch it.
    # A held-out family's sites are sealed in full, including related edits.
    sealed={(m["replace"],m["line"]) for m in heldout_test}
    heldout_test.extend(m for m in mutants if (m["replace"],m["line"]) in sealed)
    mutants=[m for m in mutants if (m["replace"],m["line"]) not in sealed]
    grouped=defaultdict(list)
    for m in mutants: grouped[(m["replace"],m["line"])].append(m)
    keys=sorted(grouped); random.Random(seed).shuffle(keys)
    nt=min(len(keys)-2,max(1,round(len(keys)*test_fraction))) if len(keys)>=3 and test_fraction>0 and not heldout else 0
    nv=min(len(keys)-nt-1,max(1,round(len(keys)*validation_fraction))) if len(keys)-nt>=2 and validation_fraction>0 else 0
    dev,validation,test=[],[],list(heldout_test)
    for i,key in enumerate(keys):
        (test if i<nt else validation if i<nt+nv else dev).extend(grouped[key])
    return dev,validation,test

def pool_identity(unit,settings,environment,heldout=None,*,generator_hashes=None,
                  selection_protocol="standalone_f1_f5_selection_v1"):
    """Pure identity calculation, also used to verify a pre-B1 upgrade."""
    policy={k:settings[k] for k in ("backend","max_mutants_per_family","pool_seed","test_fraction","validation_fraction","syntax_timeout_seconds","jasper_executable","jasper_version_tag")}
    if generator_hashes is None:
        generator_hashes={name:hashlib.sha256((ROOT/"vendor"/"faults"/name).read_bytes()).hexdigest()
                          for name in ("generate_mutants.py","generate_mutants_ast.py","validate_mutants.py","collect_jasper.py")}
    policy["generator_sha256"]=digest(generator_hashes)
    policy["target_selection_protocol"]="bound_instance_labels_v3"
    protocol=pool_protocol(settings)
    policy["fault_qualification"]=protocol
    policy["proof_timeout_seconds"]=settings["proof_timeout_seconds"]
    policy["split_protocol"]="global_source_site_v2"
    if selection_protocol is not None:policy["selection_protocol"]=selection_protocol
    return digest({"environment":environment,"unit":unit,"policy":policy,"heldout":heldout})

def build_pool(dataset,unit,settings,lock,heldout=None,*,plan_only=False):
    from .equivalence import qualify_mutant
    protocol=pool_protocol(settings)
    identity=pool_identity(unit,settings,lock.identity,heldout)
    directory=Path(dataset)/"gen_assertions"/"_shared"/"pools"/unit["name"]/identity[:20]
    if plan_only:
        # Identity only: no generated RTL, probe, Jasper session or pool preparation.
        return {"identity":identity,"directory":str(directory),"pending":True,"heldout_family":heldout}
    manifest_path=directory/"pool.json"
    if manifest_path.exists():
        pool=read(manifest_path)
        if pool["identity"]!=identity: raise ValueError("Pool fingerprint mismatch")
        for m in pool["mutants"]:
            if hashlib.sha256(Path(m["file"]).read_bytes()).hexdigest()!=m["sha256"]:
                raise ValueError("Cached mutant was modified")
        return pool
    directory.mkdir(parents=True,exist_ok=True)
    lock.check()
    dummy=directory/"fixed_harness.sv"
    # Step B compilation uses the fixed empty harness, never an invented assertion.
    dummy.write_text(render(dataset,unit,[]))
    config=formal_config(dataset,unit,dummy,[],settings)
    config["allow_empty_targets"]=True
    rtl=[Path(p) for p in config["rtl_files"]]
    binds=[Path(p) for p in config["bind_files"]]
    includes=[Path(p) for p in config["include_dirs"]]
    # Validate the fixed harness first. A setup error must not become generator feedback.
    def validate(job,files):
        attempt=JasperAttempt(job)
        tcl=attempt.directory/"run.tcl"
        tcl.write_text(syntax_tcl(config,files,dummy,binds,includes,config["defines"],0))
        attempt.publish("run.tcl")
        try:
            code=attempt.run(settings["jasper_executable"],tcl,settings["syntax_timeout_seconds"])
            status,reason=parse_validation_log(attempt.log.read_text(errors="replace"),code)
        except TimeoutError:
            status,reason="UNRESOLVED","syntax timeout"
        return status,reason
    status,reason=validate(directory/"fixed_setup",rtl)
    if status!="PASS": raise EnvironmentSetupError(f"Step B fixed harness failed to elaborate: {reason}; inspect {directory/'fixed_setup'}")
    if settings["backend"]=="ast":
        try:
            from generate_mutants_ast import generate_ast
            import pyslang
        except ImportError:
            raise RuntimeError("AST backend needs pyslang: python3 -m pip install -r requirements.txt") from None
        generator=generate_ast
    else: generator=generate
    rng=random.Random(settings["pool_seed"])
    all_candidates=[]
    for target in unit["mutation_targets"]:
        path=resolve(dataset,target)
        source=path.read_text()
        print(f"[POOL_AST] {unit['name']} target={path}" if settings["backend"]=="ast" else
              f"[POOL_LEXICAL] {unit['name']} target={path}",flush=True)
        try:
            candidates=(generator(source,source_path=path,include_dirs=includes,defines=config["defines"])
                        if settings["backend"]=="ast" else generator(source))
        except ValueError as error:
            diagnostic=directory/"ast_parse_error.txt"
            diagnostic.write_text(f"Target: {path}\nInclude directories: {includes}\nDefines: {config['defines']}\n{error}\n")
            raise EnvironmentSetupError(f"Mutant source parse failed for {path}: {error}\nSaved diagnostics: {diagnostic}") from error
        print(f"[POOL_CANDIDATES] {unit['name']} target={path.name} count={len(candidates)}",flush=True)
        for c in candidates: all_candidates.append((target,path,source,c))
    enumeration=[{"target":target,"family":c.family,"line":source.count("\n",0,c.start)+1,
                  "offset":c.start,"operator":c.kind,"before":source[c.start:c.end],"after":c.replacement}
                 for target,path,source,c in all_candidates]
    save(directory/"candidate_inventory.json",{"total_candidates":len(enumeration),"candidates":enumeration,
         "target_scope":unit["mutation_targets"],"cap_per_family":settings["max_mutants_per_family"],
         "families":{f:{"candidates":sum(row["family"]==f for row in enumeration),
                         "source_sites":len({(row["target"],row["line"]) for row in enumeration if row["family"]==f}),
                         "operators":sorted({row["operator"] for row in enumeration if row["family"]==f})} for f in ("F1","F2","F3","F4","F5")},
         "generator":"original F1-F5; no replacement operators added"})
    # This calibration must prove that a golden design agrees with itself AND
    # the fixed environment permits an observation. No generated assertion enters it.
    calibration={"status":"NOT_REQUESTED","reason":"Syntax-only mutation score; independent behavioral difference is not established."}
    print(f"[POOL_MODE] {unit['name']} protocol={protocol}; "+
          ("syntax/elaboration -> assertion detection; no equivalence jobs" if protocol==SYNTAX_PROTOCOL else
           "independent equivalence qualification required before assertion detection"),flush=True)
    if protocol!=SYNTAX_PROTOCOL:
        calibration=qualify_mutant(dataset,unit,{"id":"golden_calibration","sha256":lock.identity},settings,lock,
                              directory/"equivalence"/"golden_calibration",calibration=True)
        if calibration["status"]!="EQUIVALENT":
            save(directory/"qualification_blocked.json",calibration)
            raise EnvironmentSetupError("Independent fault qualification calibration failed: "+calibration.get("reason",calibration["status"])+
                                    "; inspect "+str(directory/"equivalence"/"golden_calibration"))
    chosen=[]
    global_seen=set()
    for family in ("F1","F2","F3","F4","F5"):
        candidates=[x for x in all_candidates if x[3].family==family]
        cap=settings["max_mutants_per_family"]
        # Match the standalone Step B driver: preserve enumeration order when
        # uncapped, shuffle only on requested sampling, deduplicate across F1-F5.
        if cap is not None:rng.shuffle(candidates)
        unique=set()
        for target,path,source,c in candidates:
            text=source[:c.start]+c.replacement+source[c.end:]
            sha=hashlib.sha256(text.encode()).hexdigest()
            if (target,sha) in global_seen: continue
            unique.add((target,sha))
            global_seen.add((target,sha))
            index=len([m for m in chosen if m["family"]==family])+1
            mid=f"{family}_{index:05d}"
            output=directory/"mutants"/family/(mid+"_"+path.name)
            output.parent.mkdir(parents=True,exist_ok=True)
            output.write_text(text)
            chosen.append({"id":mid,"family":family,"replace":str(path),"file":str(output),"sha256":sha,
                           "line":source.count("\n",0,c.start)+1,"operator":c.kind,
                           "before":source[c.start:c.end],"after":c.replacement})
            if cap is not None and len(unique)>=cap: break
    validated=[]
    audit=[]
    for m in chosen:
        lock.check()
        print(f"[POOL] {unit['name']} {m['id']}",flush=True)
        files=[Path(m["file"]) if str(p)==m["replace"] else p for p in rtl]
        syntax_status,reason=validate(directory/"validation"/m["id"],files)
        if syntax_status=="PASS" and protocol==SYNTAX_PROTOCOL:
            qualified={"status":"SYNTAX_VALID","reason":"Elaborates under the fixed harness; behavioral difference not established."}
        elif syntax_status=="PASS":
            qualified=qualify_mutant(dataset,unit,m,settings,lock,directory/"equivalence"/m["id"])
        else:
            qualified={"status":"UNRESOLVED","reason":"Mutant syntax/elaboration: "+reason}
        record={**m,"syntax_status":syntax_status,**qualified}
        audit.append(record)
        # Only independently established observable differences are evaluable.
        if qualified["status"]==("SYNTAX_VALID" if protocol==SYNTAX_PROTOCOL else "DIFFERENT"):
            validated.append({**m,"syntax_validated":True,**qualified})
        save(directory/"qualification_progress.json",{"completed":len(audit),"total":len(chosen),"records":audit})
        print(f"[POOL_RESULT] {m['id']} {qualified['status']}: {qualified.get('reason','')}",flush=True)
    dev,validation,test=partition(validated,settings["pool_seed"],settings["test_fraction"],heldout,settings["validation_fraction"])
    from .common import table
    counts={status:sum(m["status"]==status for m in audit) for status in
            ("DIFFERENT","EQUIVALENT","UNRESOLVED","INTERFACE_INCOMPATIBLE","SYNTAX_VALID")}
    table(directory/"qualification.csv",[{k:v for k,v in m.items() if k not in {"proof_statuses","reference_ports","mutant_ports","reference_states","mutant_states"}} for m in audit])
    save(directory/"pool_audit.json",{"candidate_count":len(enumeration),"chosen_count":len(chosen),"counts":counts,
         "calibration":calibration,"records":audit,"selection_independent_of_assertions":True})
    result={"identity":identity,"directory":str(directory),"mutants":validated,
            "dev_ids":[m["id"] for m in dev],"validation_ids":[m["id"] for m in validation],"test_ids":[m["id"] for m in test],
            "heldout_family":heldout,"validation":audit,"qualification_counts":counts,
            "validity":("Syntax/elaboration validated only; equivalent or unobservable mutants may remain in the denominator."
                        if protocol==SYNTAX_PROTOCOL else "Only independently proven observable differences; generated assertions never determine eligibility."),
            "protocol":protocol,"candidate_count":len(enumeration),"chosen_count":len(chosen)}
    save(manifest_path,result)
    lock.check()
    return result

def score_rows(properties,mutants,rows,budget=None):
    labels=[p["label"] for p in properties]
    matrix={(r["assertion_id"],r["mutant_id"]):r["status"] for r in rows}
    expected={(a,m["id"]) for a in labels for m in mutants}
    if set(matrix)!=expected or len(rows)!=len(expected) or any(v not in {"CEX","PROVEN","UNKNOWN"} for v in matrix.values()):
        raise ValueError("Incomplete/duplicate/invalid assertion-by-mutant matrix")
    detected={m["id"] for m in mutants if any(matrix[a,m["id"]]=="CEX" for a in labels)}
    uncertain={m["id"] for m in mutants if m["id"] not in detected and any(matrix[a,m["id"]]=="UNKNOWN" for a in labels)}
    selected=[]
    remaining=set(labels)
    covered=set()
    while remaining and (budget is None or len(selected)<budget):
        best=min(remaining,key=lambda a:(-sum(matrix[a,m["id"]]=="CEX" and m["id"] not in covered for m in mutants),a))
        gains={m["id"] for m in mutants if matrix[best,m["id"]]=="CEX"}-covered
        if not gains: break
        selected.append(best)
        remaining.remove(best)
        covered.update(gains)
    n=len(mutants)
    return {"qualified_count":len(labels),"mutant_count":n,"detected_count":len(detected),
            "unknown_only_count":len(uncertain),"detection_lower_percent":100*len(detected)/n if n else None,
            "detection_upper_percent":100*(len(detected)+len(uncertain))/n if n else None,
            "selected_labels":selected,"selected_count":len(selected),
            "selected_detected_count":len(covered),"preserves_detected_set":covered==detected,
            "undetected_ids":[m["id"] for m in mutants if m["id"] not in detected],"unknown_ids":sorted(uncertain),
            "detected_ids":sorted(detected),"selection":"greedy candidate; no global minimum claim"}

def measure(dataset,unit,properties,pool,partition_name,directory,settings,lock):
    lock.check()
    directory=Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    ids=set(pool[partition_name+"_ids"])
    mutants=[m for m in pool["mutants"] if m["id"] in ids]
    if not properties or not mutants:
        result=score_rows(properties,mutants,[]) if not properties else {
            "qualified_count":len(properties),"mutant_count":0,"detected_count":0,"unknown_only_count":0,
            "detection_lower_percent":None,"detection_upper_percent":None,"selected_labels":[],
            "selected_count":0,"selected_detected_count":0,"preserves_detected_set":True,
            "undetected_ids":[],"detected_ids":[],"selection":"No evaluable mutants"}
    else:
        source=directory/(unit["name"]+"_assertions.sv")
        source.write_text(render(dataset,unit,properties))
        config=formal_config(dataset,unit,source,[p["label"] for p in properties],settings)
        config_path=directory/"config.json"
        manifest=directory/"manifest.json"
        save(config_path,config)
        save(manifest,{"assertions":config["assertions"],"mutants":mutants})
        collect(config_path,manifest,directory/"jasper",cache_dir=Path(dataset)/"gen_assertions"/"_shared"/"results")
        with (directory/"jasper"/"matrix.csv").open() as f: rows=list(csv.DictReader(f))
        result=score_rows(properties,mutants,rows,settings.get("assertion_budget"))
        if partition_name=="dev":
            selected=set(result["selected_labels"])
            (directory/"selected_assertions.sv").write_text(render(dataset,unit,[p for p in properties if p["label"] in selected]))
    result["partition"]=partition_name
    result["pool_protocol"]=pool.get("protocol","unspecified")
    result["pool_identity"]=pool["identity"]
    result["status"]="MEASURED" if properties and mutants else ("NO_QUALIFIED_ASSERTIONS" if not properties else "NO_EVALUABLE_MUTANTS")
    save(directory/"metrics.json",result)
    lock.check()
    return result

def feedback(pool,metrics,limit=8,round_number=1):
    remaining=set(metrics["undetected_ids"])
    dev=set(pool["dev_ids"])
    # Rotate across source sites/affected outputs before alternative edits at
    # one site. Exclude UNKNOWN measurements from actionable confirmed misses.
    uncertain=set(metrics.get("unknown_ids",[]))
    groups=defaultdict(list)
    for m in pool["mutants"]:
        if m["id"] in (remaining & dev)-uncertain:
            groups[(m.get("replace",""),m["line"],tuple(m.get("affected_outputs",[])))].append(m)
    keys=sorted(groups)
    if keys:
        offset=(round_number-1)%len(keys); keys=keys[offset:]+keys[:offset]
    ordered=[]
    for depth in range(max((len(g) for g in groups.values()),default=0)):
        for key in keys:
            group=groups[key]
            if depth<len(group):ordered.append(group[(depth+round_number-1)%len(group)])
    fields=("id","family","replace","line","operator","before","after","affected_outputs","witness")
    examples=[{k:m[k] for k in fields if k in m} for m in ordered[:limit] if limit is not None] if limit is not None else [{k:m[k] for k in fields if k in m} for m in ordered]
    basis=("candidate gaps from syntax-valid mutants; some may be equivalent or unobservable, so a miss alone does not establish a missing requirement"
           if pool.get("protocol")==SYNTAX_PROTOCOL else "independently validated missed behaviors")
    return {"instruction":f"Add spec-supported checks for {basis}. Derive a behavioral relation from the RTL and spec; a changed expression alone is not progress. Keep all assumptions and accepted checks fixed. If no justified addition exists, return NO_NEW_ASSERTIONS followed by the reason.",
            "development_detection_percent":metrics["detection_lower_percent"],"missed_examples":examples,
            "actionable_miss_count":len(ordered),"unknown_only_count":metrics["unknown_only_count"]}
