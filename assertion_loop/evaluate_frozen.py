#!/usr/bin/env python3
"""Evaluate one already frozen ablation/control budget on sealed test faults; no LLM."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import shutil
from pathlib import Path
from looplib.common import ROOT,EnvironmentLock,read,save,digest,table
from looplib.faults import measure,pool_protocol
from looplib.reporting import report
from run import datasets

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('dataset');p.add_argument('--datasets-root',type=Path,default=ROOT.parent)
    p.add_argument('--experiment',required=True);p.add_argument('--budget',type=int,required=True)
    p.add_argument('--mode',choices=('ablation','controlled'),default='ablation')
    p.add_argument('--model',action='append',choices=('qwen','deepseek','gpt','gemini','opus','gpt61'))
    p.add_argument('--condition',choices=('targeted','none'))
    p.add_argument('--unit',action='append');p.add_argument('--holdout-family',choices=('F1','F2','F3','F4','F5'))
    p.add_argument('--allow-additional-test-budget',action='store_true',help='mark test access exploratory if a different budget has already been tested')
    args=p.parse_args(argv)
    if args.budget<1:p.error('--budget must be positive')
    if not args.experiment or Path(args.experiment).name!=args.experiment or args.experiment in {'.','..'}:p.error('Use a simple experiment name')
    for dataset in datasets(args.dataset,args.datasets_root):
        experiment=dataset/'gen_assertions'/args.mode/args.experiment
        if args.holdout_family:experiment/=args.holdout_family
        config=read(experiment/'config_snapshot.json')
        if config.get('protocol_version')!=16:raise ValueError('Use v16 independently qualified frozen results')
        if not shutil.which(config['jasper_executable']):raise RuntimeError('Jasper not found: '+config['jasper_executable'])
        access_path=experiment/'test_access.json'
        access=read(access_path) if access_path.is_file() else {'budgets':[]}
        if access['budgets'] and args.budget not in access['budgets'] and not args.allow_additional_test_budget:
            raise ValueError('A different test budget was already accessed. Use --allow-additional-test-budget only for explicitly exploratory analysis.')
        candidates=[f for f in experiment.rglob(f'budgets/budget_{args.budget:02d}/summary.json') if not {'_baselines','_history'} & set(f.relative_to(experiment).parts)]
        selected=[]
        for f in candidates:
            row=read(f)
            if args.model and row['generator_model'] not in args.model:continue
            if args.unit and row['unit'] not in args.unit:continue
            if args.condition and row.get('feedback_condition',row.get('feedback_policy'))!=args.condition:continue
            selected.append((f,row))
        if not selected:raise ValueError('No qualified frozen checkpoints match this budget/model/unit/condition')
        results=[]
        with (dataset/'gen_assertions'/'.loop.lock').open('a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            # Record test access before the first proof, including interruptions.
            access['budgets']=sorted(set(access['budgets']+[args.budget]))
            access['status']='EXPLORATORY_MULTIPLE_BUDGETS' if len(access['budgets'])>1 else 'ONE_FROZEN_BUDGET'
            save(access_path,access)
            for f,row in selected:
                checkpoint=f.parent;unit_dir=checkpoint.parent.parent
                fixed=read(unit_dir/'fixed_environment.json');lock=EnvironmentLock(dataset,fixed['setup'])
                if lock.identity!=fixed['sha256'] or lock.initial!=fixed['files']:raise ValueError('Frozen environment changed; test evaluation stopped')
                unit=next(u for u in fixed['setup']['units'] if u['name']==row['unit'])
                props=read(checkpoint/'frozen_assertions.json')
                if row.get('frozen_set_sha256')!=digest(props):raise ValueError('Frozen assertion content changed')
                if any(any(p.get('checks',{}).get(g)!='PASS' for g in ('syntax','fpv','vacuity','spec')) for p in props):raise ValueError('Frozen input has incomplete Step A')
                pool_file=dataset/'gen_assertions'/'_shared'/'pools'/unit['name']/row['pool_identity'][:20]/'pool.json'
                pool=read(pool_file)
                if pool['identity']!=row['pool_identity'] or pool.get('protocol')!=pool_protocol(config):raise ValueError('Frozen pool mismatch')
                if row.get('pool_protocol',pool['protocol'])!=pool['protocol']:raise ValueError('Frozen summary protocol mismatch')
                for m in pool['mutants']:
                    if hashlib.sha256(Path(m['file']).read_bytes()).hexdigest()!=m['sha256']:raise ValueError('Frozen mutant changed')
                metrics=measure(dataset,unit,props,pool,'test',checkpoint/'step_b_test',config,lock)
                state=read(unit_dir/'rounds'/f"round_{row['step_b_rounds_completed']:02d}"/'round.json')
                labels=set(state['dev_metrics']['selected_labels']);selected_props=[p for p in props if p['label'] in labels]
                small=measure(dataset,unit,selected_props,pool,'test',checkpoint/'step_b_selected_test',config,lock)
                row.update(test_status='EVALUATED_AFTER_FREEZE',test_mutants=metrics['mutant_count'],test_detected=metrics['detected_count'],
                           test_lower_percent=metrics['detection_lower_percent'],test_upper_percent=metrics['detection_upper_percent'],
                           test_unknown_only=metrics['unknown_only_count'],selected_test_detected=small['detected_count'],selected_test_lower_percent=small['detection_lower_percent'],test_access_policy=access['status'])
                save(f,row);save(checkpoint/'frozen_test_summary.json',row);results.append(row)
                print(f"[FROZEN_TEST] {row.get('feedback_condition','')}/{row['generator_model']}/{unit['name']} B={args.budget} actual_B={row['step_b_rounds_completed']} {metrics['detected_count']}/{metrics['mutant_count']}; {checkpoint}",flush=True)
            table(experiment/f'frozen_test_budget_{args.budget:02d}.csv',results);report(experiment)
    return 0
if __name__=='__main__':raise SystemExit(main())
