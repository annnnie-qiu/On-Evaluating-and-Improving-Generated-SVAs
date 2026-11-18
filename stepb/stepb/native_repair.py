"""Repair native-only gaps while retaining the original, verified cache keys.

The original adapter, Tcl and implementation stay byte-for-byte unchanged.
Old successful measurements retain their identities. Repaired jobs use a
separate implementation/template identity and never overwrite old evidence.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
from pathlib import Path

import native_batch as legacy
from run_part_b import Pipeline, Unit, render_subset, neutralize_cover_statements
from shared_results import Checker, environment, signature, sha, save_json
from aq_source_parser import extract_assertions
from collect_jasper import tcl_list

HERE = Path(__file__).resolve().parent
_FAILED = {}


def _context(record, output, scope):
    config = json.loads(Path(record['config']).read_text())
    root = Path(config['project_root'])
    path = Path(record['aq_file'])
    source = path.read_text()
    checker = Checker(source, record['assertions'], str(path))
    old_template = HERE / 'native/jasper_mutation_set_quality.tcl'
    identity = signature({'environment': environment(config, root, checker, path),
        'aq': checker.aq_sha256, 'scope': scope, 'template': sha(old_template.read_bytes()),
        'implementation': sha((HERE / 'native/run_part_b.py').read_bytes()),
        'adapter': sha((HERE / 'native_batch.py').read_bytes())})
    folder = output / '_native_results' / identity
    canonical = folder / path.name
    # legacy.measure(reuse_only=True) has prepared the canonical copy.
    source = canonical.read_text()
    if Checker(source, record['assertions'], str(path)).aq_sha256 != checker.aq_sha256:
        raise ValueError('native canonical AQ does not match current Step A')
    blocks, errors = extract_assertions(source)
    if errors or len(blocks) != len(record['assertions']):
        raise ValueError('native source inventory mismatch')
    resolve = lambda value: (root / value).resolve()
    unit = Unit(assertion_file=Path(record['source']).name, unit_id='aq', top=config['top'],
        module_guess='', a0_count=record.get('step_a_a0', 0), aq_count=len(blocks),
        qualification_yield=0, source_path=canonical, source=source, source_sha256=sha(source), blocks=blocks,
        rtl_files=[resolve(p) for p in config['rtl_files']],
        include_dirs=[resolve(p) for p in config.get('include_dirs', [])],
        bind_files=[resolve(p) for p in config.get('bind_files', [])],
        defines=config.get('defines', []), clock_mode=config.get('clock_mode', 'none'),
        clock=config.get('clock', ''), reset_expression=config.get('reset_expression', ''))
    return config, root, checker, unit, identity, folder


def _labels(unit):
    ids = {b.assertion_id for b in unit.blocks}
    source = render_subset(unit.source, unit.blocks, ids, label_unnamed=True)
    source, _, errors = neutralize_cover_statements(source)
    blocks, parse_errors = extract_assertions(source)
    labels = [b.property_name for b in blocks]
    if errors or parse_errors or len(labels) != len(ids) or len(set(labels)) != len(labels):
        raise ValueError('cannot map the selected source assertions uniquely')
    if not all(re.fullmatch(r'[A-Za-z_$][A-Za-z0-9_$]*', label) for label in labels):
        raise ValueError('unsupported escaped assertion label; retain evidence for inspection')
    return labels


def _evidence(data, unit, identity, root):
    """Require complete saved tool evidence, including every expanded instance."""
    if (data.get('experiment_hash') != identity or data.get('source_sha256') != unit.source_sha256
            or data.get('top') != unit.top or data.get('assertion_file') != unit.assertion_file
            or set(data.get('assertion_ids', [])) != {b.assertion_id for b in unit.blocks}
            or data.get('exit_code') != 0 or data.get('status') in {'TIMEOUT', 'PLANNED'}):
        raise ValueError('saved native evidence is incomplete or belongs to other inputs')
    path = Path(data['log_file'])
    path = path if path.is_absolute() else root / path
    raw = path.read_bytes()
    text = raw.decode('utf-8', errors='replace')
    if len(re.findall(r'^RQ2_MUTATION_PASS$', text, re.MULTILINE)) != 1:
        raise ValueError('saved native log has no completed mutation report')
    counts = Pipeline.parse_counts(text)
    if counts is None or any(data.get(k) != v for k, v in counts.items()):
        raise ValueError('saved native counts are missing or disagree with the raw log')
    if counts['unprocessed'] != 0 or sum(counts[k] for k in
            ['checked', 'unchecked', 'undetectable', 'unprocessed', 'excluded']) != counts['total']:
        raise ValueError('native mutation report is incomplete or has an invalid partition')
    active = Pipeline.parse_named_count(text, 'RQ2_ACTIVE_ASSERTION_COUNT')
    names = re.findall(r'^RQ2_ACTIVE_ASSERTION\s+name=(.+)$', text, re.MULTILINE)
    if active <= 0 or len(names) != active or len(set(names)) != active:
        raise ValueError('elaborated assertion inventory is missing or duplicated')
    labels = _labels(unit)
    instances = {label: [] for label in labels}
    for name in names:
        matches = [label for label in labels if name == label or name.endswith('.' + label)]
        if len(matches) != 1:
            raise ValueError(f'active assertion is not uniquely mapped to selected AQ: {name}')
        instances[matches[0]].append(name)
    if any(not names for names in instances.values()):
        raise ValueError('a selected source assertion has no elaborated instance')
    if Pipeline.parse_proof_counts(text) != (active, active, 0):
        raise ValueError('not every expanded AQ instance was proven on golden RTL')
    eligible = counts['total'] - counts['excluded'] - counts['unprocessed']
    if eligible < 0:
        raise ValueError('negative native mutation denominator')
    denominator = counts['checked'] + counts['unchecked']
    status = 'NO_ELIGIBLE_MUTANTS' if eligible == 0 else 'UNDEFINED_NATIVE_RATIO' if denominator == 0 else 'PASS'
    answer = dict(data, status=status, eligible=eligible, active_assertion_count=active,
        active_properties=names, source_assertion_count=len(labels), source_instance_map=instances,
        integrity=counts['checked'] / eligible if eligible else None,
        jasper_native_percent=100 * counts['checked'] / denominator if denominator else None,
        fixed_universe_percent=100 * counts['checked'] / eligible if eligible else None,
        evidence_log_sha256=sha(raw))
    answer['reason'] = ('Verified complete native report has no eligible mutation items; ratio N/A.'
                        if eligible == 0 else 'Verified complete native report has no checked/unchecked items; native ratio N/A.'
                        if denominator == 0 else 'All selected AQ source assertions mapped to proven elaborated instances.')
    return answer


def _decorate(data, checker, identity, folder, scope):
    return dict(data, cache_identity=identity, aq_sha256=checker.aq_sha256,
                native_cache_directory=str(folder), mutation_scope=scope)


class InstancePipeline(Pipeline):
    def _template(self):
        text = super()._template()
        return text.replace('@@SOURCE_LABELS@@', tcl_list(self.source_labels))


def measure(record, output, aq_config, *, dry_run=False, reuse_only=False, scope='hierarchy',
            known_gaps_only=False):
    output = Path(output)
    # Preserve successful caches under the exact adapter/template identity that
    # produced them. No change to the original implementation or cache files.
    try:
        old = legacy.measure(record, output, aq_config, dry_run=dry_run,
                             reuse_only=True, scope=scope)
        if old['jasper_native_percent'] is not None or dry_run:
            return old
        config, root, checker, unit, identity, folder = _context(record, output, scope)
        verified = _evidence(old, unit, identity, root)
        return _decorate(verified, checker, identity, folder, scope)
    except ValueError as error:
        if not str(error).startswith('No native check_cov cache'):
            raise
    config, root, checker, unit, old_identity, old_folder = _context(record, output, scope)
    failed_key = (old_identity, dry_run, reuse_only, known_gaps_only)
    if failed_key in _FAILED:
        raise RuntimeError('REUSE FAILURE; no additional Jasper: ' + _FAILED[failed_key])
    try:
        return _measure_missing(record, output, aq_config, config, root, checker, unit,
                                old_identity, old_folder, dry_run, reuse_only, scope, known_gaps_only)
    except Exception as error:
        # An identical failed AQ/context is attempted at most once per command.
        # A fresh command may retry a transient tool/license failure.
        _FAILED[failed_key] = str(error)
        raise


def _measure_missing(record, output, aq_config, config, root, checker, unit,
                     old_identity, old_folder, dry_run, reuse_only, scope, known_gaps_only):
    ids = {b.assertion_id for b in unit.blocks}
    previous_path = old_folder / 'runs/aq' / f'n{len(ids):04d}_{Pipeline.subset_key(ids)}' / 'result.json'
    if previous_path.is_file():
        try:
            previous = json.loads(previous_path.read_text())
            recovered = _evidence(previous, unit, old_identity, root)
        except (OSError, ValueError, KeyError, TypeError):
            pass
        else:
            print(f'RECOVER NATIVE {unit.assertion_file}: {recovered["status"]}; saved report; no Jasper', flush=True)
            return _decorate(dict(recovered, resumed=True, recovered_from=str(previous_path)),
                             checker, old_identity, old_folder, scope)
    template = HERE / 'native/jasper_mutation_instances.tcl'
    identity = signature({'protocol': 'mapped_elaborated_native_v1', 'original_context': old_identity,
        'template': sha(template.read_bytes()), 'repair_implementation': sha(Path(__file__).read_bytes())})
    folder = output / '_native_results' / identity
    saved = folder / 'repair_result.json'
    if saved.is_file():
        data = json.loads(saved.read_text())
        verified = _evidence(data, unit, identity, root)
        if data.get('evidence_log_sha256') != verified['evidence_log_sha256']:
            raise ValueError('saved repaired log changed; refusing stale coverage')
        print(f'REUSE NATIVE REPAIR {unit.assertion_file}: {verified["status"]}; no Jasper', flush=True)
        return _decorate(dict(verified, resumed=True), checker, identity, folder, scope)
    if reuse_only:
        raise ValueError(f'Native measurement is missing for {unit.assertion_file}; --reuse-only never starts Jasper')
    if known_gaps_only:
        if not previous_path.is_file():
            raise ValueError(f'No matching previous native attempt for {unit.assertion_file}; '
                             'repair-only stopped without Jasper; check cache path/configuration')
        previous = json.loads(previous_path.read_text())
        if (previous.get('experiment_hash') != old_identity
                or previous.get('source_sha256') != unit.source_sha256
                or previous.get('top') != unit.top
                or set(previous.get('assertion_ids', [])) != ids):
            raise ValueError('Previous native attempt belongs to other inputs; repair-only stopped without Jasper')
    folder.mkdir(parents=True, exist_ok=True)
    canonical = folder / unit.source_path.name
    if not canonical.exists():
        canonical.write_text(unit.source)
    elif canonical.read_text() != unit.source:
        raise ValueError('repaired canonical source changed')
    unit = dataclasses.replace(unit, source_path=canonical)
    cfg = {'project_root': str(root), 'aq_config': str(aq_config),
        'paths': {'output_dir': str(folder), 'rtl_dir': 'rtl'},
        'jasper': {'executable': config.get('jasper_executable', 'jg')},
        'mutation': {'scope': scope, 'tcl_template_file': str(template),
            'timeout_seconds': config.get('timeout_seconds', 7200), 'heartbeat_seconds': 30},
        'eligibility': {'denominator': 'fixed_total_minus_excluded',
            'require_no_unprocessed': True, 'require_partition_sum': True}}
    save_json(folder / 'native_config.json', cfg)
    args = argparse.Namespace(force=False, dry_run=dry_run, timeout=None, include=None,
                              aq_config=str(aq_config), spec_results=None)
    pipeline = InstancePipeline(folder / 'native_config.json', args)
    pipeline.experiment_hash = identity
    pipeline.source_labels = _labels(unit)
    print(f'FILL NATIVE GAP {unit.assertion_file}: {len(ids)} source assertions; '
          'prove all mapped elaborated instances; existing F1-F5 results untouched', flush=True)
    result = pipeline.measure(unit, ids)
    if dry_run:
        return _decorate(result.as_dict(), checker, identity, folder, scope)
    data = _evidence(result.as_dict(), unit, identity, root)
    data = _decorate(dict(data, original_cache_identity=old_identity), checker, identity, folder, scope)
    save_json(saved, data)
    return data
