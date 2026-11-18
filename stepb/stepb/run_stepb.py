#!/usr/bin/env python3
"""Use existing Step A trials; compare native Jasper and explicit F1-F5 faults."""
from __future__ import annotations
import argparse
from collections import Counter
import csv
import json
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'engine'))
from shared_results import save_json, signature, sha
from evaluate import FAMILIES

MODEL_NAMES = {'qwen': 'Qwen2.5-coder-14b', 'deepseek': 'DeepSeek V4.1 Flash',
               'gpt': 'GPT-6 Luna', 'gemini': 'Gemini 3.5 Flash Lite'}


def table(path, rows):
    if not rows:
        return
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with path.open('w', newline='', encoding='utf-8-sig') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    save_json(path.with_suffix('.json'), rows)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def holdout_scope_status(folder, record):
    from run_batch import pool_environment, validated_pool_can_resume
    existing = _read_json(folder / 'validation/validated_manifest.json')
    if len({m.get('family') for m in existing.get('mutants', [])}) >= 2:
        # A verified completed pool needs no regeneration artifacts to resume.
        verified = validated_pool_can_resume(folder / 'validation/validated_manifest.json',
                                              [({}, record, folder.parent)])
        if verified is None:
            raise ValueError('completed fault pool is stale or unverified')
        return {'status': 'APPLICABLE', 'reason': 'Verified fixed pool has at least two nonempty classes.',
                'generated_counts': {}, 'validated_counts': dict(Counter(m['family'] for m in verified['mutants'])),
                'folds': len({m['family'] for m in verified['mutants']}), 'validated': verified}
    generated = _read_json(folder / 'generated/manifest.json')
    validated = _read_json(folder / 'validation/validated_manifest.json')
    metadata = _read_json(folder / 'validation/pool_context.json')
    if (metadata.get('environment_sha256') != pool_environment(Path(record['config']))
            or metadata.get('manifest_sha256') != signature(validated)):
        raise ValueError('fault pool does not match current RTL/configuration or saved manifest')
    candidates = generated.get('mutants')
    accepted = validated.get('mutants')
    if not isinstance(candidates, list) or not isinstance(accepted, list):
        raise ValueError('invalid generated/validated fault manifest')
    by_id = {m['id']: m for m in candidates}
    if len(by_id) != len(candidates) or any(m.get('family') not in FAMILIES for m in candidates):
        raise ValueError('duplicate IDs or invalid fault families')
    for item in candidates:
        path = Path(item['file'])
        path = path if path.is_absolute() else folder / 'generated' / path
        if not item.get('sha256') or sha(path.read_bytes()) != item['sha256']:
            raise ValueError('generated mutant hash mismatch: ' + item['id'])
    if candidates:
        with (folder / 'validation/validation_report.csv').open(newline='') as handle:
            report = list(csv.DictReader(handle))
        reports = {r['id']: r for r in report}
        if len(reports) != len(report) or set(reports) != set(by_id):
            raise ValueError('validation report is partial or contains duplicate/unexpected mutants')
        if any(r['status'] not in {'PASS', 'FAIL'} for r in report):
            raise ValueError('validation contains unfinished work')
        accepted_ids = {m['id'] for m in accepted}
        if len(accepted_ids) != len(accepted) or accepted_ids != {r['id'] for r in report if r['status'] == 'PASS'}:
            raise ValueError('validated mutant list disagrees with validation report')
        # These indicate an adapter/infrastructure problem, not inapplicability.
        if len({m['family'] for m in accepted}) < 2 and any(
                'active assertion count mismatch' in r.get('reason', '').lower()
                or 'timeout' in r.get('reason', '').lower()
                or 'no pass token' in r.get('reason', '').lower() for r in report if r['status'] == 'FAIL'):
            raise ValueError('pool may be incomplete because of assertion inventory/timeout/tool failure')
    for item in accepted:
        original = by_id.get(item['id'])
        path = Path(item['file'])
        if (original is None or item.get('syntax_validated') is not True
                or item.get('family') != original['family']
                or item.get('sha256') != original['sha256']
                or sha(path.read_bytes()) != item['sha256']):
            raise ValueError('validated mutant identity/hash mismatch')
    generated_counts = dict(Counter(m['family'] for m in candidates))
    counts = dict(Counter(m['family'] for m in accepted))
    if not candidates:
        if accepted:
            raise ValueError('validated mutants exist without generated candidates')
        status, reason = 'NO_GENERATED_MUTANTS', 'Current generator produced no candidates for this target; holdout N/A.'
    elif not accepted:
        # Do not silently drop modules with genuine or unexplained validation failures.
        raise ValueError('candidates were generated but none passed validation; inspect the original errors')
    elif len(counts) == 1:
        status, reason = 'SINGLE_FAULT_CLASS', 'Only one validated fault class exists; cross-class holdout N/A.'
    else:
        status, reason = 'APPLICABLE', 'At least two validated classes; use every available class as a held-out fold.'
    return dict(status=status, reason=reason, generated_counts=generated_counts,
                validated_counts=counts, folds=len(counts), validated=validated)



def summarize(root, output, models, variants, *, aq_config=None):
    checks = json.loads((output / 'step_a_output_check.json').read_text())
    all_modules = sorted({r['module'] for r in checks})
    excluded = [m for m in all_modules if all(r.get('aq_count') == 0 for r in checks if r['module'] == m)]
    nonempty_modules = [m for m in all_modules if m not in excluded]
    current = {}
    for module in all_modules:
        audit = json.loads((output / module / 'step_a_outputs.json').read_text())
        for slot in audit['slots']:
            current[(module, slot['variant'], slot['model'], slot['run'])] = slot['aq_sha256']
    modules = list(nonempty_modules)
    not_applicable, native_not_applicable, pool_errors = {}, {}, {}
    native_snapshots, native_errors, pool_snapshots = {}, {}, {}
    if aq_config is not None:
        from native_repair import measure
        from empty_aq import empty_result
        for module in nonempty_modules:
            print(f'SUMMARY ONLY {module}: verify saved applicability and native evidence; no Jasper', flush=True)
            slots = [(variant, model, trial) for variant in variants for model in models for trial in range(1, 4)]
            records = {}
            try:
                for variant, model, trial in slots:
                    path = output / module / 'checked_step_a_outputs' / variant / model / f'run_{trial:02d}' / 'input/export_record.json'
                    records[variant, model, trial] = _read_json(path)
                representative = next(r for r in records.values() if r['assertions'])
                pool = holdout_scope_status(output / module / 'fault_pool', representative)
                pool_snapshots[module] = pool
                if pool['status'] != 'APPLICABLE':
                    not_applicable[module] = {k: v for k, v in pool.items() if k != 'validated'}
                    modules.remove(module)
                    continue
            except (OSError, ValueError, KeyError, TypeError, StopIteration) as error:
                # A stale/missing/failed pool remains in scope and blocks its
                # mean. It must never be hidden as an applicability exclusion.
                pool_errors[module] = str(error)
            for variant, model, trial in slots:
                key = (module, variant, model, trial)
                try:
                    record = records[variant, model, trial]
                    saved = output / module / variant / model / f'run_{trial:02d}' / 'native_result.json'
                    previous = _read_json(saved) if saved.is_file() else {}
                    scope = previous.get('mutation_scope', 'hierarchy')
                    result = (measure(record, output, aq_config, reuse_only=True, scope=scope)
                              if record['assertions'] else empty_result(record, scope))
                    if result.get('aq_sha256') != current[key]:
                        raise ValueError('cached native AQ differs from current Step A')
                    native_snapshots[key] = result
                except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                    native_errors[key] = str(error)
            evidence = [native_snapshots.get((module, *slot)) for slot in slots]
            # Exclude only a universally undefined *nonempty* native universe.
            # Partial-empty AQ retains the previous zero-holdout/N/A-native
            # policy, so rejection by one model is not silently filtered out.
            if (module not in pool_errors and all(e is not None for e in evidence)
                    and all(e['status'] in {'NO_ELIGIBLE_MUTANTS', 'UNDEFINED_NATIVE_RATIO'}
                            and e['jasper_native_percent'] is None for e in evidence)):
                native_not_applicable[module] = sorted({e['status'] for e in evidence})
                modules.remove(module)
    scope_changed = bool(not_applicable or native_not_applicable)
    accepted_statuses = {'PASS', 'EMPTY_AQ', 'PASS_WITH_UNDEFINED_NATIVE', 'PASS_ON_APPLICABLE_SCOPE', 'NOT_APPLICABLE'}
    save_json(output / 'module_scope.json', {
        'policy': 'One fixed scope across all requested models/variants/trials. All-empty AQ modules, '
                  'verified pools with no generated candidates or only one validated class, and modules with '
                  'universally undefined nonempty native ratios are explicitly outside the common comparison scope. '
                  'Unexpected missing/stale/failed evidence stays in scope and blocks the mean.',
        'all_modules': all_modules, 'nonempty_aq_modules': nonempty_modules,
        'evaluated_modules': modules, 'all_empty_aq_modules': excluded,
        'holdout_not_applicable_modules': not_applicable,
        'native_not_applicable_modules': native_not_applicable,
        'unresolved_pool_errors': pool_errors, 'models': models, 'variants': variants,
        'partial_empty_policy': 'A model-specific empty trial on a measured module has zero holdout detection and N/A native; it is not filtered out.'})
    runs = []
    for model in models:
        for variant in variants:
            for trial in range(1, 4):
                native, effective, endpoints, upper, coverage_rows = [], [], [], [], []
                reasons, families, undefined_native = [], {}, []
                for module in modules:
                    key = (module, variant, model, trial)
                    if module in pool_errors:
                        reasons.append(f'{module}: {pool_errors[module]}')
                        continue
                    if key in native_errors:
                        reasons.append(f'{module}: native evidence unavailable: {native_errors[key]}')
                        continue
                    slot = output / module / variant / model / f'run_{trial:02d}'
                    native_path = slot / 'native_result.json'
                    summary_path = slot / 'evaluation/summary.json'
                    status_path = slot / 'run_summary.json'
                    if not ((key in native_snapshots or native_path.is_file()) and summary_path.is_file() and status_path.is_file()):
                        reasons.append(f'{module}: missing native or holdout result')
                        continue
                    n = native_snapshots[key] if key in native_snapshots else json.loads(native_path.read_text())
                    s = json.loads(summary_path.read_text())
                    r = json.loads(status_path.read_text())
                    if n['status'] not in {'PASS', 'EMPTY_AQ', 'NO_ELIGIBLE_MUTANTS', 'UNDEFINED_NATIVE_RATIO'} or r['status'] != 'PASS':
                        reasons.append(f'{module}: incomplete/undefined result')
                        continue
                    if n['status'] == 'PASS' and n['jasper_native_percent'] is None:
                        reasons.append(f'{module}: nonempty native ratio is undefined')
                        continue
                    if n['status'] == 'EMPTY_AQ' and s['assertion_count'] != 0:
                        reasons.append(f'{module}: empty native AQ does not match holdout AQ')
                        continue
                    expected = current[(module, variant, model, trial)]
                    if n.get('aq_sha256') != expected or r.get('aq_sha256') != expected:
                        reasons.append(f'{module}: results do not match the current Step A AQ')
                        continue
                    if aq_config is not None:
                        # Stage-only runs must not publish stale results from the other stage.
                        validated = pool_snapshots[module]['validated']
                        if validated is None or r.get('pool_sha256') != signature(validated['mutants']):
                            reasons.append(f'{module}: holdout pool/configuration is stale or unverified')
                            continue
                    if n['status'] in {'EMPTY_AQ', 'NO_ELIGIBLE_MUTANTS', 'UNDEFINED_NATIVE_RATIO'}:
                        undefined_native.append(module)
                    else:
                        native.append(n['jasper_native_percent'])
                        coverage_rows.append(n)
                    effective.append(r['fixed_budget_area_percent'])
                    endpoints.append(s['macro_full_set_percent_lower'])
                    upper.append(s['macro_full_set_percent_upper'])
                    families[module] = s['active_families']
                row = {'dataset': root.name, 'model': MODEL_NAMES[model], 'model_key': model,
                    'variant': variant, 'run': trial,
                    'status': 'INCOMPLETE' if reasons else ('EMPTY_AQ' if not nonempty_modules else 'NOT_APPLICABLE') if not modules else
                              'PASS_WITH_UNDEFINED_NATIVE' if undefined_native else 'PASS_ON_APPLICABLE_SCOPE' if scope_changed else 'PASS',
                    'module_count': len(endpoints), 'expected_modules': len(modules),
                    'total_inventory_modules': len(all_modules), 'all_empty_aq_modules': ' | '.join(excluded),
                    'native_undefined_modules': ' | '.join(undefined_native),
                    'jasper_mutation_coverage_percent': '', 'our_effective_area_percent': '',
                    'our_full_AQ_detection_percent': '', 'our_full_AQ_upper_percent': '',
                    'jasper_fixed_universe_percent': '', 'reason': ' | '.join(reasons)}
                if not reasons and modules:
                    row.update(jasper_mutation_coverage_percent=statistics.mean(native) if not undefined_native else '',
                        our_effective_area_percent=statistics.mean(effective),
                        our_full_AQ_detection_percent=statistics.mean(endpoints),
                        our_full_AQ_upper_percent=statistics.mean(upper),
                        jasper_fixed_universe_percent=statistics.mean(n['fixed_universe_percent'] for n in coverage_rows)
                            if not undefined_native else '')
                runs.append(row)
                save_json(output / 'dataset_trials' / variant / model / f'run_{trial:02d}.json',
                          dict(row, active_families=families))
    table(output / 'dataset_runs.csv', runs)
    averages = []
    metrics = ['jasper_mutation_coverage_percent', 'our_effective_area_percent',
               'our_full_AQ_detection_percent', 'our_full_AQ_upper_percent', 'jasper_fixed_universe_percent']
    for model in models:
        for variant in variants:
            rows = [r for r in runs if r['model_key'] == model and r['variant'] == variant]
            complete = len(rows) == 3 and all(r['status'] in accepted_statuses for r in rows)
            avg = {'dataset': root.name, 'model': MODEL_NAMES[model], 'variant': variant,
                   'status': (('EMPTY_AQ' if not nonempty_modules else 'NOT_APPLICABLE') if not modules else 'PASS_WITH_UNDEFINED_NATIVE'
                              if any(r['status'] == 'PASS_WITH_UNDEFINED_NATIVE' for r in rows) else 'PASS_ON_APPLICABLE_SCOPE' if scope_changed else 'PASS')
                              if complete else 'INCOMPLETE', 'runs': 3,
                   'evaluated_module_count': len(modules), 'total_inventory_modules': len(all_modules),
                   'all_empty_aq_modules': ' | '.join(excluded),
                   'native_undefined_modules': ' | '.join(sorted({m for r in rows for m in
                                               r['native_undefined_modules'].split(' | ') if m})),
                   'aggregation': 'equal modules in the declared common applicable scope, equal nonempty fault classes; '
                                  'then mean of three trials; see module_scope.json'}
            for metric in metrics:
                defined = complete and all(isinstance(r[metric], (int, float)) for r in rows)
                avg[metric] = statistics.mean(r[metric] for r in rows) if defined else ''
                avg[metric + '_sd'] = statistics.stdev(r[metric] for r in rows) if defined else ''
            averages.append(avg)
    table(output / 'comparison_average.csv', averages)
    lines = ['# Step B comparison', '',
        '| Dataset | LLM | Input | Jasper mutation coverage (%) | Our Effective: full-AQ detection (%) | Our curve area (%) | Status |',
        '|---|---|---|---:|---:|---:|---|']
    fmt = lambda value: f'{value:.2f}%' if isinstance(value, (int, float)) else 'N/A'
    for r in averages:
        lines.append(f'| {r["dataset"]} | {r["model"]} | {r["variant"]} | '
            f'{fmt(r[metrics[0]])} | {fmt(r[metrics[2]])} | {fmt(r[metrics[1]])} | {r["status"]} |')
    lines += ['', 'Each value is the mean of three complete trials. See dataset_runs.csv for individual scores.',
        'Jasper: checked/(checked+unchecked). Fixed-universe score is also retained in the CSV.',
        'Our area: mean detection at k=1..B on held-out classes, using a common module budget B.',
        'Full-AQ detection uses each trial\'s complete AQ. UNKNOWN contributes only to the separate upper bound.',
        'Available fault classes and missing classes are recorded per module.']
    lines += ['', f'Common measured module scope: {len(modules)}/{len(all_modules)} inventory modules.',
              'Modules empty in every requested slot (not measured): ' + (', '.join(excluded) or 'none') + '.',
              'Holdout not applicable (no candidates / one class): ' + (', '.join(not_applicable) or 'none') + '.',
              'Native ratio undefined in every nonempty trial: ' + (', '.join(native_not_applicable) or 'none') + '.',
              '**Means use the declared applicable module scope, not the full inventory.** '
              'Every model, variant and trial uses the same module list.',
              'See module_scope.json for the common scope. An empty trial on a measured module has zero explicit-pool detection; '
              'its native coverage is N/A. A verified complete native report with zero eligible mutants or zero '
              'checked/unchecked items also has an undefined native ratio. Dataset native means remain N/A if any '
              'measured module has an undefined native ratio; completed Our scores are retained. '
              'Missing/corrupt evidence remains an error.']
    (output / 'comparison_average.md').write_text('\n'.join(lines) + '\n')
    return all(r['status'] in accepted_statuses for r in runs)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root', type=Path, default=HERE.parent)
    p.add_argument('--aq-config', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--models', nargs='+', choices=MODEL_NAMES, default=list(MODEL_NAMES))
    p.add_argument('--variants', nargs='+', choices=['original', 'controlled'], default=['original', 'controlled'])
    p.add_argument('--stage', choices=['all', 'holdout', 'native', 'summary'], default='all')
    p.add_argument('--check-only', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--reuse-only', action='store_true')
    p.add_argument('--repair-native-only', action='store_true',
                   help='with --stage native, fill only contexts with matching previous native attempts; preserve all other caches')
    p.add_argument('--install-pyslang', action='store_true')
    p.add_argument('--backend', choices=['ast', 'lexical'], default='ast')
    p.add_argument('--max-per-family', type=int)
    p.add_argument('--native-scope', choices=['top', 'hierarchy'], default='hierarchy')
    args = p.parse_args()
    if sys.version_info < (3, 9):
        p.error('Use Python 3.9 or later.')
    if args.reuse_only and args.dry_run:
        p.error('--reuse-only cannot combine with --dry-run')
    if args.repair_native_only and args.stage != 'native':
        p.error('--repair-native-only requires --stage native')
    root = args.project_root.resolve()
    output = (args.output or root / 'aq_out/fault_holdout_batch').resolve()
    aq_config = (args.aq_config or root / 'aq_qualification_pipeline_v1/aq_config.json').resolve()
    output.mkdir(parents=True, exist_ok=True)
    base = [sys.executable, '-u', str(HERE / 'engine/run_batch.py'), '--project-root', str(root),
            '--output', str(output), '--aq-config', str(aq_config),
            '--models', *args.models, '--variants', *args.variants]
    print(f'[Step B] dataset={root}; output={output}', flush=True)
    print('[1/4] Audit and export existing Step A trials (no LLM calls)', flush=True)
    checked = subprocess.run([*base, '--check-step-a-outputs'])
    if checked.returncode:
        print(f'Input check stopped Step B before Jasper. See {output / "step_a_output_check.json"} '
              f'and {output / "step_a_outputs.json"} for the exact reasons.', file=sys.stderr)
        return checked.returncode
    if args.check_only:
        print(f'CHECK COMPLETE: {output / "step_a_outputs.csv"}', flush=True)
        return 0
    errors = []
    holdout_failed = False
    if args.stage == 'summary':
        print('[SUMMARY ONLY] Read existing evidence and update the original comparison files; '
              'no mutant generation, proof session or model API.', flush=True)
    if args.stage in ['all', 'holdout']:
        print('[2/4] Fixed F1-F5 pool and shared assertion × mutant matrix', flush=True)
        command = [*base, '--backend', args.backend]
        for flag in ['dry_run', 'reuse_only', 'install_pyslang']:
            if getattr(args, flag):
                command.append('--' + flag.replace('_', '-'))
        if args.stage == 'summary' and '--reuse-only' not in command:
            command.append('--reuse-only')
        if args.max_per_family is not None:
            command += ['--max-per-family', str(args.max_per_family)]
        if subprocess.run(command).returncode:
            holdout_failed = True
    if args.stage in ['all', 'native']:
        from native_repair import measure
        checks = json.loads((output / 'step_a_output_check.json').read_text())
        print('[3/4] Jasper native mutation coverage; reuse identical complete AQ', flush=True)
        for r in checks:
            slot = output / r['module'] / r['variant'] / r['model'] / f'run_{r["run"]:02d}'
            record_path = output / r['module'] / 'checked_step_a_outputs' / r['variant'] / r['model'] / f'run_{r["run"]:02d}' / 'input/export_record.json'
            print(f'NATIVE {r["module"]}/{r["variant"]}/{r["model"]}/run_{r["run"]:02d}', flush=True)
            try:
                record = json.loads(record_path.read_text())
                if not record['assertions']:
                    from empty_aq import empty_result
                    result = empty_result(record, args.native_scope)
                    print('EMPTY_AQ: no native Jasper session; native percentage N/A', flush=True)
                else:
                    result = measure(record, output, aq_config, dry_run=args.dry_run,
                                     reuse_only=args.reuse_only or args.stage == 'summary', scope=args.native_scope,
                                     known_gaps_only=args.repair_native_only)
                save_json(slot / ('native_plan.json' if args.dry_run else 'native_result.json'), result)
            except Exception as e:
                errors.append(str(e))
                save_json(slot / 'native_result.json', {'status': 'ERROR', 'reason': str(e)})
                print(f'NATIVE ERROR: {e}', flush=True)
    if not args.dry_run:
        print('[4/4] Three-trial averages and dataset comparison table', flush=True)
        complete = summarize(root, output, args.models, args.variants, aq_config=aq_config)
        print(f'RESULT: {output / "comparison_average.csv"}', flush=True)
        if holdout_failed:
            print('Holdout process reported unscored entries; see all_runs.csv. '
                  'The comparison explicitly declares applicability exclusions in module_scope.json.', flush=True)
        if holdout_failed and not complete:
            errors.append('holdout stage failed; inspect all_runs.csv and module_scope.json')
        elif args.stage in ['all', 'summary'] and not complete:
            errors.append('comparison incomplete; missing/failed results are not averaged')
    elif holdout_failed:
        errors.append('holdout dry-run failed; inspect terminal output')
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
