"""CSV/HTML reports generated only from measured checkpoints."""
from __future__ import annotations
import html
import statistics
from collections import defaultdict
from pathlib import Path
from .common import read,save,table

EFFECTIVENESS_COLUMNS=("feedback_condition","feedback_policy","stop_reason","dataset","generator_model","unit","repeat","budget","step_b_rounds_completed",
    "input_mode","spec_policy","initial_assertion_count","pool_protocol","pool_validity",
    "pool_candidate_count","pool_valid_mutants","pool_equivalent","pool_unresolved","pool_interface_incompatible",
    "qualified_count","selected_count","dev_detected","dev_mutants","dev_unknown_only",
    "dev_lower_percent","dev_upper_percent","validation_detected","validation_mutants",
    "validation_unknown_only","validation_lower_percent","validation_upper_percent",
    "selected_validation_lower_percent","dev_status","validation_status","step_a_repairs_used",
    "generation_calls_used","total_tokens","reported_cost_usd","estimated_cost_usd","status","output")

def print_effectiveness(rows):
    if not rows:
        print("[RESULT] No completed Step B budget results; missing measurements are not zero effectiveness.",flush=True)
        return
    print("[RESULT] Step B effectiveness: confirmed detected mutants / evaluable mutants; validation is not used as feedback.",flush=True)
    print("model | unit | repeat | Step B | dev effective | validation effective | AQ",flush=True)
    def score(row,partition):
        value=row.get(partition+"_lower_percent")
        return "NA" if value is None else f"{value:.2f}% ({row.get(partition+'_detected')}/{row.get(partition+'_mutants')})"
    for row in sorted(rows,key=lambda r:(r["dataset"],r["unit"],r["generator_model"],r["repeat"],r["budget"])):
        print(f"[METRIC_POOL] {row.get('pool_protocol','legacy unspecified')}",flush=True)
        print(f"{row['generator_model']} | {row['unit']} | {row['repeat']} | {row['budget']} | "
              f"{score(row,'dev')} | {score(row,'validation')} | {row['qualified_count']}",flush=True)

def report(experiment):
    experiment=Path(experiment)
    rows=[]
    legacy=[]
    for p in sorted(experiment.rglob("budgets/budget_*/summary.json")):
        if {"_baselines","_history"} & set(p.relative_to(experiment).parts):continue
        row=read(p)
        if row.get("budget_type")!="step_b_rounds":
            legacy.append(str(p))
            continue
        rows.append(row)
    if legacy and not rows:
        print("[REPORT] Preserved legacy generation-budget reports; use a new experiment for Step B budgets.",flush=True)
        return []
    protocols={r.get("pool_protocol","observable_equivalence_v1") for r in rows}
    if len(protocols)>1:
        raise ValueError("Different fault eligibility protocols in one experiment; aggregate them in separate experiments.")
    table(experiment/"comparison.csv",rows)
    table(experiment/"effectiveness.csv",[{key:r.get(key) for key in EFFECTIVENESS_COLUMNS} for r in rows])
    groups=defaultdict(list)
    for r in rows: groups[(r["dataset"],r["generator_model"],r["unit"],r["budget"],r.get("feedback_condition",r.get("feedback_policy","targeted")))].append(r)
    averages=[]
    metrics=("initial_assertion_count","qualified_count","selected_count","step_a_repairs_used","generation_calls_used","format_repair_calls","dev_detected","dev_mutants","dev_unknown_only","dev_lower_percent","dev_upper_percent",
             "validation_detected","validation_mutants","validation_unknown_only","validation_lower_percent","validation_upper_percent","test_lower_percent","test_upper_percent",
             "selected_test_lower_percent","total_tokens","estimated_cost_usd","reported_cost_usd")
    for key,samples in sorted(groups.items()):
        row={"dataset":key[0],"model":key[1],"unit":key[2],"budget":key[3],"repeats":len(samples),"feedback_condition":key[4]}
        for m in metrics:
            values=[r[m] for r in samples if r.get(m) is not None]
            row[m+"_mean"]=statistics.mean(values) if values else None
            row[m+"_sd"]=statistics.stdev(values) if len(values)>1 else None
        averages.append(row)
    table(experiment/"comparison_average.csv",averages)
    save(experiment/"comparison.json",{"runs":rows,"averages":averages})
    dataset_rows,paired=dataset_report(experiment,rows)
    trajectory_report(experiment)
    columns=("feedback_condition","feedback_policy","stop_reason","status","dataset","generator_model","unit","repeat","budget","step_b_rounds_completed","step_a_repairs_used","generation_calls_used","format_repair_calls","generated_count",
             "input_mode","spec_policy","initial_assertion_count","pool_protocol","pool_validity",
             "qualified_count","selected_count","dev_detected","dev_mutants","dev_unknown_only","dev_lower_percent","dev_upper_percent",
             "validation_detected","validation_mutants","validation_unknown_only","validation_lower_percent","validation_upper_percent","test_lower_percent","test_upper_percent",
             "selected_test_lower_percent","total_tokens","reported_cost_usd","reported_cost_missing_calls",
             "estimated_cost_usd","estimated_cost_missing_calls")
    def cell(v):
        return "—" if v is None else (f"{v:.6f}" if isinstance(v,float) else str(v))
    body="".join("<tr>"+"".join("<td>"+html.escape(cell(r.get(c)))+"</td>" for c in columns)+"</tr>" for r in rows)
    svg=""
    curves=defaultdict(list)
    for r in averages: curves[(r["dataset"],r["model"],r["unit"],r["feedback_condition"])].append(r)
    colors=["#2563eb","#dc2626","#059669","#9333ea"]
    for i,(key,points) in enumerate(sorted(curves.items())):
        points=sorted(points,key=lambda p:p["budget"])
        valid=[p for p in points if p.get("validation_lower_percent_mean") is not None]
        if not valid: continue
        maxb=max(p["budget"] for p in points)
        xy=" ".join(f"{50+450*p['budget']/maxb:.1f},{240-2*p['validation_lower_percent_mean']:.1f}" for p in valid)
        svg+=f'<div class="plot"><b>{html.escape(" / ".join(key))}</b><svg viewBox="0 0 540 285"><path d="M50 30V240H510" fill="none" stroke="#444"/><text x="5" y="35">100%</text><text x="20" y="245">0%</text><text x="180" y="280">Step B rounds (budget)</text><polyline points="{xy}" fill="none" stroke="{colors[i%4]}" stroke-width="3"/>'
        for p in valid:
            x,y=50+450*p["budget"]/maxb,240-2*p["validation_lower_percent_mean"]
            svg+=f'<circle cx="{x}" cy="{y}" r="4" fill="{colors[i%4]}"/><text x="{x-8}" y="260">{p["budget"]}</text>'
        svg+='</svg></div>'
    content='''<!doctype html><meta charset="utf-8"><title>Assertion loop comparison</title>
<style>body{font:14px system-ui;margin:28px;color:#162033}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #d3dbe5;padding:7px}th{background:#edf3fa;position:sticky;top:0}.scroll{overflow:auto}.plot{display:inline-block;width:520px;padding:16px;border:1px solid #ddd;margin:12px}svg{width:100%}p{max-width:1000px;line-height:1.6}</style>
<h1>Assertion loop comparison</h1><p>Budgets count completed Step B rounds. Step A attempts are counted separately. Each model, unit and repeat uses one cumulative trajectory. Step A must pass before Step B starts. Only development faults are used as feedback. Validation is used to compare budgets. Test faults are not evaluated during ablation. Costs are cumulative; do not add costs across budgets. A dash means no value is available.</p>
<h2>Step B effectiveness</h2><p>The lower rate counts mutants detected by at least one qualified assertion. The upper rate also includes unresolved mutants. See effectiveness.csv for units, dataset_effectiveness.csv for datasets, and controlled_pairs.csv for paired runs. Complete dataset rates require all expected units. Early stops reuse the last frozen set and keep the actual completed round count.</p>'''
    brief=("feedback_condition","generator_model","unit","repeat","budget","qualified_count","dev_detected","dev_mutants","dev_lower_percent",
           "validation_detected","validation_mutants","validation_unknown_only","validation_lower_percent","validation_upper_percent")
    protocol=next(iter(protocols),"no measured results")
    content+='<p>Pool protocol: '+html.escape(protocol)+'. '+('Syntax-valid denominator: equivalence and observability are not established.' if protocol=='syntax_validated_f1_f5_v1' else 'Only independently proven observable differences enter the denominator.')+'</p>'
    content+='<div class="scroll"><table><thead><tr>'+''.join('<th>'+c+'</th>' for c in brief)+'</tr></thead><tbody>'
    content+=''.join('<tr>'+''.join('<td>'+html.escape(cell(r.get(c)))+'</td>' for c in brief)+'</tr>' for r in rows)
    content+='</tbody></table></div><h2>Validation curves and full measurements</h2>'
    failures=read(experiment/"failures.json") if (experiment/"failures.json").is_file() else []
    if failures:
        content+='<p>Some runs failed or are incomplete. Missing measurements are not counted as zero.</p><ul>'+''.join('<li>'+html.escape(str(f))+'</li>' for f in failures)+'</ul>'
    content+=svg+'<div class="scroll"><table><thead><tr>'+''.join('<th>'+c+'</th>' for c in columns)+'</tr></thead><tbody>'+body+'</tbody></table></div>'
    (experiment/"comparison.html").write_text(content,encoding="utf-8")
    return rows


def dataset_report(experiment,rows):
    """Missing units are explicit. Never average only the successful models away."""
    experiment=Path(experiment)
    inventory=read(experiment/"run_inventory.json") if (experiment/"run_inventory.json").is_file() else {}
    expected=inventory.get("units") or sorted({r["unit"] for r in rows})
    datasets=sorted({r["dataset"] for r in rows} or ([inventory["dataset"]] if inventory.get("dataset") else []))
    models=inventory.get("models") or sorted({r["generator_model"] for r in rows})
    budgets=inventory.get("budgets") or sorted({r["budget"] for r in rows})
    conditions=inventory.get("conditions") or sorted({r.get("feedback_condition",r.get("feedback_policy","targeted")) for r in rows})
    repeats=range(1,inventory["repeats"]+1) if inventory.get("repeats") else sorted({r["repeat"] for r in rows})
    result=[]
    def score(samples,partition):
        n=sum(r.get(partition+"_mutants") or 0 for r in samples)
        detected=sum(r.get(partition+"_detected") or 0 for r in samples)
        values=[r[partition+"_lower_percent"] for r in samples if r.get(partition+"_lower_percent") is not None]
        complete=set(r["unit"] for r in samples)==set(expected)
        return {partition+"_mutants":n,partition+"_detected":detected,
                partition+"_partial_micro_percent":100*detected/n if n else None,
                partition+"_micro_percent":100*detected/n if n and complete else None,
                partition+"_macro_evaluable_percent":statistics.mean(values) if values else None,
                partition+"_evaluable_units":len(values)}
    for dataset in datasets:
      for model in models:
       for repeat in repeats:
        for budget in budgets:
         for condition in conditions:
            samples=[r for r in rows if (r["dataset"],r["generator_model"],r["repeat"],r["budget"],r.get("feedback_condition",r.get("feedback_policy","targeted")))==(dataset,model,repeat,budget,condition)]
            missing=sorted(set(expected)-{r["unit"] for r in samples})
            result.append({"dataset":dataset,"model":model,"repeat":repeat,"budget":budget,"feedback_condition":condition,
                           "pool_protocol":inventory.get("pool_protocol",next(iter({r.get('pool_protocol','observable_equivalence_v1') for r in samples}),None)),
                           "expected_units":len(expected),"measured_units":len(samples),"missing_units":missing,
                           "status":"COMPLETE_DATASET" if not missing else "INCOMPLETE_DATASET",
                           "qualified_count":sum(r["qualified_count"] for r in samples),
                           "early_stopped_units":sum(r.get("status")=="EARLY_STOPPED" for r in samples),
                           **score(samples,"dev"),**score(samples,"validation"),**score(samples,"test")})
    table(experiment/"dataset_effectiveness.csv",result)
    paired=[]
    for dataset in datasets:
     for model in models:
      for repeat in repeats:
       for budget in budgets:
        by_condition={c:{r["unit"]:r for r in rows if (r["dataset"],r["generator_model"],r["repeat"],r["budget"],r.get("feedback_condition"))==(dataset,model,repeat,budget,c)} for c in ("targeted","none")}
        if not any(by_condition.values()):continue
        common=set(by_condition["targeted"]) & set(by_condition["none"])
        for unit in common:
            a,b=by_condition["targeted"][unit],by_condition["none"][unit]
            if a.get("pool_identity")!=b.get("pool_identity"):raise ValueError("Paired pool identities differ")
        left=[by_condition["targeted"][u] for u in sorted(common)];right=[by_condition["none"][u] for u in sorted(common)]
        ls,rs=score(left,"validation"),score(right,"validation")
        n=ls["validation_mutants"];a=ls["validation_partial_micro_percent"];b=rs["validation_partial_micro_percent"]
        paired.append({"dataset":dataset,"model":model,"repeat":repeat,"budget":budget,"paired_units":len(common),
                       "expected_units":len(expected),"paired_population":sorted(common),"complete_pair":common==set(expected),
                       "validation_mutants":n,"targeted_detected":ls["validation_detected"],"none_detected":rs["validation_detected"],
                       "targeted_validation_percent":a,"none_validation_percent":b,"delta_percentage_points":a-b if a is not None and b is not None else None,
                       "comparison_scope":"all expected units" if common==set(expected) else "partial common units only"})
    table(experiment/"controlled_pairs.csv",paired)
    save(experiment/"dataset_effectiveness.json",{"datasets":result,"paired":paired})
    return result,paired


def trajectory_report(experiment):
    """Include failed-run spending that is later than the last completed prefix."""
    import json
    from .llm import totals
    experiment=Path(experiment);ledger=experiment/"api"/"usage.jsonl";records=[]
    if ledger.is_file():
        for line in ledger.read_text().splitlines():
            try:records.append(json.loads(line))
            except ValueError:continue  # an active writer can have an incomplete last line
    rows=[]
    for recipe in sorted(experiment.rglob("recipe.json")):
        if "_history" in recipe.relative_to(experiment).parts:continue
        directory=recipe.parent;context=read(recipe).get("context",{})
        states=[read(f) for f in sorted((directory/"rounds").glob("round_*/round.json"))]
        error=read(directory/"error.json") if (directory/"error.json").is_file() else {}
        limit=read(directory/"step_a_limit.json") if (directory/"step_a_limit.json").is_file() else {}
        stop=read(directory/"stop.json") if (directory/"stop.json").is_file() else {}
        usage=[r for r in records if all(r.get(k)==v for k,v in context.items())]
        rows.append({**context,"completed_step_b_rounds":len(states),"last_qualified_count":states[-1]["qualified_count"] if states else 0,
                     "status":error.get("status") or limit.get("status") or ("EARLY_STOPPED" if stop else "HAS_QUALIFIED_CHECKPOINT" if states else "PENDING"),
                     "stop_reason":stop.get("reason"),"error":error.get("error"),"step_a_attempt_cap":read(recipe).get("step_a_max_attempts"),
                     "attempt_files":len(list((directory/"rounds").glob("round_*/step_a_attempts/attempt_*/attempt.json"))),
                     **totals(usage),"usage_scope":"actual calls for this trajectory, including failed later attempts; shared baseline is a separate row",
                     "output":str(directory)})
    table(experiment/"trajectory_status.csv",rows)
    return rows
