#!/usr/bin/env python3
"""Restore a missing Step A results JSON from verified same-run saved evidence.

No model calls, Jasper sessions, summary changes, or overwriting existing files.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parser_module(root):
    paths = [Path(__file__).resolve().parent / 'engine/aq_source_parser.py',
             root / 'stepb/engine/aq_source_parser.py']
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        raise ValueError('Place this script in stepb/, or keep stepb/engine/aq_source_parser.py in the project.')
    spec = importlib.util.spec_from_file_location('recovery_aq_parser', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def recover(root, variant='original', model='qwen', trial=1):
    root = Path(root).resolve()
    output_name, source_name, tag = ('aq_out', 'assertions', 'NORMAL') if variant == 'original' else (
        'aq_buggy_out', 'buggy_assertions', 'BUGGY')
    aq = root / output_name
    guard = aq / 'spec_guard_four_models'
    folder = guard / model / f'run_{trial:02d}'
    target = folder / 'spec_check_results.json'
    if target.exists() or target.is_symlink():
        raise ValueError(f'Target already exists; nothing was overwritten: {target}')
    summary = read(folder / 'summary.json')
    if (summary.get('run_status') != 'COMPLETE' or summary.get('model_key') != model
            or summary.get('run_number') != trial or summary.get('input_variant') != tag):
        raise ValueError('Same-run summary is missing, incomplete, or belongs to another model/run/variant.')
    model_id = summary.get('model_id')
    if not model_id:
        raise ValueError('Summary lacks the actual model ID; cannot authenticate recovery.')
    manifests = aq / 'manifests'
    vacuity = read(manifests / 'vacuity.json')
    keys = {(r['assertion_file'], r['assertion_id']) for r in vacuity
            if r.get('status') in {'PASS', 'NOT_APPLICABLE'}}
    if len(keys) != int(summary['technical_aq']):
        raise ValueError('Technical AQ inventory disagrees with the same-run summary.')
    inventory_files = {r['assertion_file']: r for r in read(manifests / 'inventory_files.json')}
    inventory = {(r['assertion_file'], r['assertion_id']): r
                 for r in read(manifests / 'inventory_assertions.json')}
    parser = parser_module(root)
    expected = {}
    for filename in sorted({k[0] for k in keys}):
        relative = Path(filename)
        if relative.is_absolute() or '..' in relative.parts:
            raise ValueError('Unsafe assertion inventory path')
        source = (root / source_name / relative).read_text(encoding='utf-8')
        if digest(source.encode()) != inventory_files[filename]['source_sha256']:
            raise ValueError(f'Assertion source changed since Step A: {filename}')
        blocks, errors = parser.extract_assertions(source)
        if errors:
            raise ValueError(f'Assertion source cannot be safely parsed: {filename}')
        for block in blocks:
            key = (filename, block.assertion_id)
            if key in keys:
                old = inventory[key]
                if old['fingerprint'] != block.fingerprint or old['property_name'] != block.property_name:
                    raise ValueError(f'Assertion identity changed: {key}')
                expected[key] = {'assertion_file': filename, 'assertion_id': block.assertion_id,
                                 'property_name': block.property_name, 'assertion_text': block.text}
    if set(expected) != keys:
        raise ValueError('Cannot recover all technical AQ assertion identities.')

    def validate(row):
        row = dict(row)
        key = (row.get('assertion_file'), row.get('assertion_id'))
        if key not in expected:
            raise ValueError(f'Unexpected assertion in saved evidence: {key}')
        if (row.get('model_key') != model or row.get('model_id') != model_id
                or int(row.get('run_number', -1)) != trial):
            raise ValueError(f'Saved evidence belongs to another model/run: {key}')
        row['run_number'] = trial
        if row.get('property_name') != expected[key]['property_name']:
            raise ValueError(f'Saved property name mismatch: {key}')
        if row.get('assertion_text', '').strip() != expected[key]['assertion_text'].strip():
            raise ValueError(f'Saved assertion text mismatch: {key}')
        if row.get('verdict') not in {'PASS', 'FAIL'} or not re.fullmatch(r'[0-9a-f]{64}', row.get('fingerprint', '')):
            raise ValueError(f'Missing saved verdict or request fingerprint: {key}')
        if row.get('trace_file'):
            path = (guard / row['trace_file']).resolve()
            if not path.is_relative_to(folder.resolve()):
                raise ValueError(f'Trace reference leaves this run: {key}')
            if path.is_file():
                trace = read(path)
                if (trace.get('model_key') != model or trace.get('run_number') != trial
                        or trace.get('assertion_file') != key[0] or trace.get('assertion_id') != key[1]
                        or trace.get('cache_fingerprint') != row['fingerprint']
                        or trace.get('mapped_verdict') != row['verdict']
                        or trace.get('status') not in {'API_SUCCESS', 'API_SUCCESS_EMPTY'}):
                    raise ValueError(f'Trace does not match this saved row: {key}')
        return key, row

    candidates = {key: [] for key in keys}
    failures = []
    csv_rows = {}
    csv_path = folder / 'spec_check_results.csv'
    if csv_path.is_file():
        with csv_path.open(encoding='utf-8-sig', newline='') as handle:
            for row in csv.DictReader(handle):
                key = (row.get('assertion_file'), row.get('assertion_id'))
                if key in csv_rows:
                    raise ValueError(f'Duplicate saved CSV assertion: {key}')
                csv_rows[key] = row
                if key in keys and row.get('fingerprint') and row.get('run_number'):
                    try:
                        key, value = validate(row)
                        candidates[key].append((value, csv_path))
                    except (ValueError, TypeError) as error:
                        failures.append(str(error))
    for path in sorted((folder / 'cache').glob('*.json')):
        try:
            raw = read(path)
            if (raw.get('assertion_file'), raw.get('assertion_id')) not in keys:
                continue
            key, row = validate(raw)
            candidates[key].append((row, path))
        except (ValueError, TypeError, OSError) as error:
            failures.append(f'{path.name}: {error}')
    for path in sorted((folder / 'traces').glob('*.json')):
        try:
            trace = read(path)
            key = (trace.get('assertion_file'), trace.get('assertion_id'))
            if key not in keys:
                continue
            # The qualifier can resume the same run after the batch size changes;
            # each API call still checks exactly one assertion. Batch size is
            # not an assertion/request identity and is not a recovery gate.
            if (trace.get('requested_model') != model_id
                    or trace.get('status') not in {'API_SUCCESS', 'API_SUCCESS_EMPTY'}):
                raise ValueError('Trace model ID or API status differs from this run.')
            messages = trace.get('request', {}).get('payload', {}).get('messages', [])
            prompt = '\n'.join(m.get('content', '') for m in messages if isinstance(m.get('content'), str))
            if expected[key]['assertion_text'].strip() not in prompt:
                raise ValueError('Trace request does not contain the recorded assertion source.')
            csv_row = csv_rows.get(key, {})
            if csv_row and (csv_row.get('verdict') != trace.get('mapped_verdict')
                            or csv_row.get('model_key') != model or csv_row.get('model_id') != model_id
                            or csv_row.get('property_name') != expected[key]['property_name']
                            or csv_row.get('assertion_text', '').strip() != expected[key]['assertion_text'].strip()
                            or (csv_row.get('trace_file') and
                                (guard / csv_row['trace_file']).resolve() != path.resolve())):
                raise ValueError('Same-run CSV and trace disagree.')
            row = dict(csv_row, **expected[key], model_key=trace.get('model_key'), model_id=trace['requested_model'],
                       run_number=trace.get('run_number'), verdict=trace.get('mapped_verdict'),
                       fingerprint=trace.get('cache_fingerprint'), trace_file=path.relative_to(guard).as_posix())
            key, row = validate(row)
            candidates[key].append((row, path))
        except (ValueError, TypeError, OSError) as error:
            failures.append(f'{path.name}: {error}')
    recovered, evidence = [], []
    for key in sorted(keys):
        choices = candidates[key]
        if not choices:
            raise ValueError(f'No authentic same-run evidence for {key}. No JSON was written. '
                             'Only this model/run may need its specification check resumed. '
                             + ' | '.join(failures[:3]))
        if len({(row['fingerprint'], row['verdict']) for row, _ in choices}) != 1:
            raise ValueError(f'Conflicting saved evidence for {key}; nothing was written.')
        row, path = choices[0]
        recovered.append(row)
        evidence.append({'assertion_file': key[0], 'assertion_id': key[1], 'evidence': str(path),
                         'evidence_sha256': digest(path.read_bytes())})
    passed = sum(r['verdict'] == 'PASS' for r in recovered)
    if passed != summary['pass_count'] or passed != summary['final_aq']:
        raise ValueError('Recovered PASS count disagrees with same-run summary; nothing was written.')
    if abs((100 * passed / summary['a0_count'] if summary['a0_count'] else 0)
           - summary['post_spec_qualification_yield_percent']) > 1e-6:
        raise ValueError('Saved summary qualification percentage disagrees with recovered rows.')
    payload = (json.dumps(recovered, ensure_ascii=False, indent=2) + '\n').encode()
    # Hard-link creation fails if another process creates the target first.
    fd, temporary = tempfile.mkstemp(prefix='.recover_spec_', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    report = {'restored': str(target), 'sha256': digest(payload), 'rows': len(recovered), 'passed': passed,
              'variant': variant, 'model': model, 'run': trial, 'evidence': evidence,
              'note': 'Reconstructed from same-run saved evidence; no new API or Jasper sessions.'}
    report_path = aq / 'fault_holdout_batch/recovery' / f'{variant}_{model}_run_{trial:02d}.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    parser.add_argument('--variant', choices=['original', 'controlled'], default='original')
    parser.add_argument('--model', choices=['qwen', 'deepseek', 'gpt', 'gemini'], default='qwen')
    parser.add_argument('--run', type=int, default=1, dest='trial')
    args = parser.parse_args()
    try:
        report = recover(args.project_root, args.variant, args.model, args.trial)
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f'RECOVERY STOPPED: {error}', file=sys.stderr)
        return 1
    print(f'RESTORED {report["rows"]} rows ({report["passed"]} PASS): {report["restored"]}')
    print('No Step A technical qualification, model API, or Jasper session ran. Now rerun Step B --check-only.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
