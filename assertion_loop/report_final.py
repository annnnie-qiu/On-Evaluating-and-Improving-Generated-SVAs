#!/usr/bin/env python3
"""Read-only final AQ and original Step B class-macro effectiveness report.

No LLM, Jasper, generation, qualification or checkpoint modification.
Only already evaluated frozen-test results may be read; sealed tests stay sealed.
The original Our Effective uses the FULL qualified set on the FULL fixed pool,
averaged equally over nonempty fault families and then over declared modules.
Missing sealed-test measurements remain N/A, never zero or validation proxies.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import json
import statistics
from pathlib import Path


def read(path):return json.loads(Path(path).read_text())
def signature(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def mean(values):return statistics.mean(values) if values else None


def family_score(mutants,labels,matrix):
    groups={}
    for mutant in mutants:groups.setdefault(mutant['family'],[]).append(mutant['id'])
    rows=[]
    for family,ids in sorted(groups.items()):
        detected=sum(any(matrix[label,mid]=='CEX' for label in labels) for mid in ids)
        unknown=sum(not any(matrix[label,mid]=='CEX' for label in labels)
                    and any(matrix[label,mid]=='UNKNOWN' for label in labels) for mid in ids)
        rows.append({'family':family,'mutants':len(ids),'detected':detected,'unknown_only':unknown,
                     'lower_percent':100*detected/len(ids),'upper_percent':100*(detected+unknown)/len(ids)})
    n=sum(r['mutants'] for r in rows);detected=sum(r['detected'] for r in rows);unknown=sum(r['unknown_only'] for r in rows)
    return {'lower_percent':mean([r['lower_percent'] for r in rows]),
            'upper_percent':mean([r['upper_percent'] for r in rows]),'families':rows,
            'mutants':n,'detected':detected,'unknown_only':unknown,
            'micro_lower_percent':100*detected/n if n else None,
            'micro_upper_percent':100*(detected+unknown)/n if n else None}


def load_matrix(path,labels,mutants):
    matrix={};expected={(label,m['id']) for label in labels for m in mutants}
    with Path(path).open(newline='') as f:
        for row in csv.DictReader(f):
            key=(row['assertion_id'],row['mutant_id']);status=row['status'].upper()
            if key not in expected or key in matrix or status not in {'CEX','PROVEN','UNKNOWN'}:
                raise ValueError(f'Unexpected/duplicate matrix cell: {path}: {key}')
            matrix[key]=status
    if set(matrix)!=expected:raise ValueError(f'Incomplete matrix: {path}')
    return matrix


def unit_report(dataset,path):
    row=read(path);unit_dir=path.parents[2];budget_dir=path.parent
    result={key:row.get(key) for key in ('dataset','generator_model','repeat','unit','budget','step_b_rounds_completed','stop_reason','status')}
    result['condition']=row.get('feedback_condition',row.get('feedback_policy','targeted'))
    result.update(final_aq_count=None,final_output_gate_pass_percent=None,original_aq_yield_percent=None,
                  our_effective_percent=None,our_effective_upper_percent=None,
                  overall_micro_percent=None,full_pool_mutants=None,full_pool_detected=None,
                  our_status='MISSING_EVIDENCE',evidence_error='')
    try:
        props=read(budget_dir/'frozen_assertions.json')
        if signature(props)!=row['frozen_set_sha256']:raise ValueError('Frozen assertion hash mismatch')
        if len(props)!=row['qualified_count']:raise ValueError('Final AQ count mismatch')
        passed=sum(all(p.get('checks',{}).get(g)=='PASS' for g in ('syntax','fpv','vacuity','spec')) for p in props)
        result.update(final_aq_count=len(props),final_output_gate_pass_percent=100*passed/len(props) if props else None)
        if passed!=len(props):raise ValueError('Final AQ contains incomplete Step A checks')
        baseline=unit_dir/'rounds/round_01/round.json'
        result['baseline_aq_count']=read(baseline)['qualified_count'] if baseline.exists() else None
        pool=read(dataset/'gen_assertions/_shared/pools'/row['unit']/row['pool_identity'][:20]/'pool.json')
        if pool['identity']!=row['pool_identity'] or pool.get('protocol')!=row.get('pool_protocol'):
            raise ValueError('Frozen pool identity/protocol mismatch')
        if len({m['id'] for m in pool['mutants']})!=len(pool['mutants']):raise ValueError('Duplicate mutant ids')
        if any(m['family'] not in {'F1','F2','F3','F4','F5'} for m in pool['mutants']):raise ValueError('Unknown fault family')
        labels=[p['label'] for p in props]
        if len(set(labels))!=len(labels):raise ValueError('Duplicate qualified labels')
        actual=int(row['step_b_rounds_completed']);state=read(unit_dir/f'rounds/round_{actual:02d}/round.json')
        if signature(state['accepted'])!=signature(props):raise ValueError('Round/frozen AQ mismatch')
        full={};measured_ids=set();missing=[]
        for part,folder in [('dev',unit_dir/f'rounds/round_{actual:02d}/step_b'),
                            ('validation',budget_dir/'step_b_validation'),('test',budget_dir/'step_b_test')]:
            ids=set(pool[part+'_ids']);mutants=[m for m in pool['mutants'] if m['id'] in ids]
            if len(mutants)!=len(ids) or measured_ids & ids:raise ValueError('Invalid or overlapping pool partitions')
            measured_ids.update(ids)
            matrix_path=folder/'jasper/matrix.csv'
            if not mutants:
                result[part+'_class_macro_percent']=None;continue
            # Never use selected_validation or selected_test subset matrices.
            if part=='test' and row.get('test_status')!='EVALUATED_AFTER_FREEZE':
                missing.append(part);result[part+'_class_macro_percent']=None;continue
            if labels and not matrix_path.is_file():
                missing.append(part);result[part+'_class_macro_percent']=None;continue
            matrix=load_matrix(matrix_path,labels,mutants) if labels else {}
            score=family_score(mutants,labels,matrix)
            result[part+'_class_macro_percent']=score['lower_percent']
            result[part+'_micro_percent']=score['micro_lower_percent']
            result[part+'_mutants']=score['mutants'];result[part+'_detected']=score['detected']
            result[part+'_families']=score['families']
            full.update(matrix)
        if measured_ids!={m['id'] for m in pool['mutants']}:raise ValueError('Partitions do not cover full pool')
        result['pool_protocol']=pool['protocol']
        result['our_nonempty_classes']=len({m['family'] for m in pool['mutants']})
        if result['our_nonempty_classes']<2:result['our_status']='NOT_APPLICABLE_LESS_THAN_TWO_CLASSES'
        elif missing:result['our_status']='MISSING_PARTITIONS:'+','.join(missing)
        else:
            score=family_score(pool['mutants'],labels,full)
            result.update(our_effective_percent=score['lower_percent'],our_effective_upper_percent=score['upper_percent'],
                          overall_micro_percent=score['micro_lower_percent'],full_pool_mutants=score['mutants'],
                          full_pool_detected=score['detected'],
                          our_families=score['families'],our_status='COMPLETE_FULL_POOL')
    except (OSError,ValueError,KeyError,TypeError) as error:
        result['evidence_error']=str(error);result['our_status']='INVALID_OR_MISSING_EVIDENCE'
    return result


def table(path,rows):
    keys=list(dict.fromkeys(k for row in rows for k in row))
    with Path(path).open('w',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=keys);writer.writeheader()
        for row in rows:writer.writerow({k:json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v for k,v in row.items()})


def report_final(experiment,budget=None,*,write_outputs=True,verbose=True,models=None):
    experiment=Path(experiment).resolve()
    dataset=next((p for p in experiment.parents if (p/'gen_assertions').is_dir()),None)
    if dataset is None:raise ValueError('Experiment must be under dataset/gen_assertions')
    inventory=read(experiment/'run_inventory.json') if (experiment/'run_inventory.json').exists() else {}
    # Avoid walking thousands of Jasper workspaces and archived experiments.
    paths=list(experiment.glob('*/run_*/units/*/budgets/budget_*/summary.json'))
    paths+=list(experiment.glob('*/*/run_*/units/*/budgets/budget_*/summary.json'))
    paths=[p for p in paths if not {'_history','_baselines'} & set(p.relative_to(experiment).parts)]
    if budget is None:budget=max(inventory.get('budgets') or [read(p)['budget'] for p in paths] or [1])
    paths=[p for p in paths if int(read(p)['budget'])==budget]
    rows=[unit_report(dataset,p) for p in sorted(paths)]
    setup=read(dataset/'gen_assertions/setup/dataset.json')
    expected=inventory.get('units') or [u['name'] for u in setup['units']]
    models=models or inventory.get('models') or sorted({r['generator_model'] for r in rows})
    repeats=range(1,inventory.get('repeats',1)+1)
    conditions=inventory.get('conditions') or sorted({r['condition'] for r in rows}) or ['targeted']
    aggregates=[]
    for model in models:
      for repeat in repeats:
       for condition in conditions:
        selected=[r for r in rows if (r['generator_model'],r['repeat'],r['condition'])==(model,repeat,condition)]
        missing=sorted(set(expected)-{r['unit'] for r in selected})
        final_counts=[r['final_aq_count'] for r in selected if r['final_aq_count'] is not None]
        complete=(not missing and len(selected)==len(expected) and len(final_counts)==len(expected)
                  and all(r['our_status']!='INVALID_OR_MISSING_EVIDENCE' for r in selected))
        effective=[r['our_effective_percent'] for r in selected if r['our_status']=='COMPLETE_FULL_POOL']
        observed=sum(final_counts)
        full=[r for r in selected if r['our_status']=='COMPLETE_FULL_POOL']
        full_n=sum(r['full_pool_mutants'] for r in full);full_d=sum(r['full_pool_detected'] for r in full)
        aggregate={'dataset':dataset.name,'generator_model':model,'repeat':repeat,'condition':condition,'budget':budget,
            'measured_modules':len(selected),'expected_modules':len(expected),'missing_modules':missing,
            'final_aq_count':observed if complete else None,'observed_final_aq_count':observed,
            'final_output_gate_pass_percent':100.0 if complete and observed and all(r['final_output_gate_pass_percent']==100 for r in selected) else None,
            'original_aq_yield_percent':None,
            'our_effective_percent':mean(effective) if complete and len(effective)==len(expected) else None,
            'overall_micro_percent':100*full_d/full_n if full_n and complete and len(full)==len(expected) else None,
            'partial_our_module_mean_percent':mean(effective),
            'our_status':'COMPLETE' if complete and len(effective)==len(expected) else 'INCOMPLETE_SCOPE_OR_MEASUREMENTS',
            'aggregation':'nonempty fault classes equal; then declared modules equal; full qualified set, full fixed pool',
            'aq_note':'output gate-pass rate is not original |AQ|/|A0| generation yield'}
        for part in ('dev','validation','test'):
            scored=[r for r in selected if r.get(part+'_mutants') is not None]
            n=sum(r[part+'_mutants'] for r in scored);d=sum(r[part+'_detected'] for r in scored)
            same_scope=complete and len(scored)==len(expected)
            aggregate.update({part+'_micro_percent':100*d/n if n and same_scope else None,
                part+'_class_then_module_macro_percent':mean([r[part+'_class_macro_percent'] for r in scored]) if same_scope else None,
                part+'_partial_micro_percent':100*d/n if n else None,
                part+'_partial_class_then_module_macro_percent':mean([r[part+'_class_macro_percent'] for r in scored]),
                part+'_observed_mutants':n,part+'_observed_detected':d})
        aggregates.append(aggregate)
    if write_outputs:
        table(experiment/'final_aq_our_by_unit.csv',rows)
        table(experiment/'final_aq_our_comparison.csv',aggregates)
        (experiment/'final_aq_our_comparison.json').write_text(json.dumps({'budget':budget,'units':rows,'datasets':aggregates},ensure_ascii=False,indent=2)+'\n')
    if not verbose:return {'units':rows,'datasets':aggregates}
    print('[FINAL_REPORT] Original Our Effective = equal nonempty fault classes, then equal declared modules; full AQ / full fixed pool.')
    def percent(value):return 'N/A' if value is None else f'{value:.2f}%'
    for row in rows:
        print(f"[FINAL_UNIT] {row['generator_model']}/{row['unit']} run={row['repeat']} B={row['step_b_rounds_completed']} AQ={row['final_aq_count']}; "
              f"dev micro={percent(row.get('dev_micro_percent'))} class_mean={percent(row.get('dev_class_macro_percent'))}; "
              f"validation micro={percent(row.get('validation_micro_percent'))} class_mean={percent(row.get('validation_class_macro_percent'))}; {row['our_status']}")
    for row in aggregates:
        score='N/A' if row['our_effective_percent'] is None else f"{row['our_effective_percent']:.2f}%"
        count=row['final_aq_count'] if row['final_aq_count'] is not None else f"N/A (observed {row['observed_final_aq_count']})"
        micro='N/A' if row['overall_micro_percent'] is None else f"{row['overall_micro_percent']:.2f}%"
        print(f"[FINAL_REPORT] {row['generator_model']} run={row['repeat']} {row['condition']} AQ_count={count}; Overall_micro={micro}; Our_Effective_equal_classes_modules={score}; modules={row['measured_modules']}/{row['expected_modules']}; {row['our_status']}")
        for part in ('dev','validation','test'):
            print(f"[FINAL_PARTITION] {part}: micro={percent(row[part+'_micro_percent'])}; class_then_module_mean={percent(row[part+'_class_then_module_macro_percent'])}; "
                  f"observed {row[part+'_observed_detected']}/{row[part+'_observed_mutants']}; "
                  f"partial_scope_micro={percent(row[part+'_partial_micro_percent'])}; partial_scope_class_module_mean={percent(row[part+'_partial_class_then_module_macro_percent'])}")
    print(f"[FINAL_REPORT] {experiment/'final_aq_our_comparison.csv'}")
    return {'units':rows,'datasets':aggregates}


def report_budgets(experiment,budgets):
    """Read each saved prefix separately; never fill a missing prefix with B1."""
    experiment=Path(experiment)
    units=[];datasets=[];comparison=[]
    for budget in budgets:
        result=report_final(experiment,budget,write_outputs=False,verbose=False)
        units.extend(result['units']);datasets.extend(result['datasets'])
        for row in result['datasets']:
            observed=[u for u in result['units'] if
                (u['generator_model'],u['repeat'],u['condition'])==(row['generator_model'],row['repeat'],row['condition'])]
            item={'model':row['generator_model'],'budget':budget,'repeat':row['repeat'],'condition':row['condition'],
                  'measured_modules':row['measured_modules'],'expected_modules':row['expected_modules'],
                  'missing_modules':row['missing_modules'],
                  'actual_step_b_rounds_by_unit':{u['unit']:u['step_b_rounds_completed'] for u in observed},
                  'final_aq_count':row['final_aq_count'],
                  'observed_aq_count':row['observed_final_aq_count'] if observed else None,
                  'overall_micro_percent':row['overall_micro_percent'],'our_effective_percent':row['our_effective_percent'],
                  'full_pool_status':row['our_status']}
            for part in ('dev','validation','test'):
                item[part+'_micro_percent']=row[part+'_micro_percent']
                item[part+'_class_module_mean_percent']=row[part+'_class_then_module_macro_percent']
                item[part+'_observed_micro_percent']=row[part+'_partial_micro_percent']
                item[part+'_observed_class_module_mean_percent']=row[part+'_partial_class_then_module_macro_percent']
                item[part+'_observed_detected']=row[part+'_observed_detected']
                item[part+'_observed_mutants']=row[part+'_observed_mutants']
            comparison.append(item)
    comparison.sort(key=lambda r:(r['model'],r['repeat'],r['condition'],r['budget']))
    table(experiment/'ablation_aq_effectiveness.csv',comparison)
    table(experiment/'ablation_aq_effectiveness_by_unit.csv',units)
    output={'budgets':budgets,'comparison':comparison,'datasets':datasets,'units':units}
    (experiment/'ablation_aq_effectiveness.json').write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    def fmt(value):return 'N/A' if value is None else f'{value:.2f}%'
    print('[ABLATION_REPORT] Existing measurements only; missing budgets stay N/A. Observed-scope values are partial when modules are missing.')
    print('model | budget | repeat | condition | modules | observed AQ | dev micro | dev class/module mean | validation micro | validation class/module mean')
    for row in comparison:
        count='N/A' if row['observed_aq_count'] is None else str(row['observed_aq_count'])
        print(f"{row['model']} | {row['budget']} | {row['repeat']} | {row['condition']} | "
              f"{row['measured_modules']}/{row['expected_modules']} | {count} | "
              f"{fmt(row['dev_observed_micro_percent'])} | {fmt(row['dev_observed_class_module_mean_percent'])} | "
              f"{fmt(row['validation_observed_micro_percent'])} | {fmt(row['validation_observed_class_module_mean_percent'])}")
    print(f"[ABLATION_REPORT] {experiment/'ablation_aq_effectiveness.csv'}")
    print(f"[ABLATION_REPORT] {experiment/'ablation_aq_effectiveness_by_unit.csv'}")
    return output


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('dataset',type=Path);p.add_argument('--experiment',required=True)
    p.add_argument('--mode',choices=('ablation','controlled','main'),default='ablation')
    group=p.add_mutually_exclusive_group()
    group.add_argument('--budget',type=int)
    group.add_argument('--budgets',help='Read existing budget results together, e.g. 1,3,5,10')
    p.add_argument('--holdout-family',choices=('F1','F2','F3','F4','F5'))
    args=p.parse_args(argv)
    if args.budget is not None and args.budget<1:p.error('--budget must be positive')
    budgets=None
    if args.budgets is not None:
        try:
            budgets=[int(value.strip()) for value in args.budgets.split(',')]
            if not budgets or min(budgets)<1 or len(set(budgets))!=len(budgets):raise ValueError()
            budgets.sort()
        except ValueError:p.error('--budgets must contain distinct positive integers, e.g. 1,3,5,10')
    if Path(args.experiment).name!=args.experiment or args.experiment in {'.','..'}:p.error('Use a simple experiment name')
    dataset=args.dataset.expanduser()
    if not dataset.is_dir():dataset=Path(__file__).resolve().parent.parent/args.dataset
    experiment=dataset/'gen_assertions'/args.mode/args.experiment
    if args.holdout_family:experiment/=args.holdout_family
    if not experiment.is_dir():p.error(f'Experiment not found: {experiment}')
    if budgets is not None:report_budgets(experiment,budgets)
    else:report_final(experiment,args.budget)
    return 0


if __name__=='__main__':raise SystemExit(main())

