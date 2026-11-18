"""Native Jasper check_cov: once per distinct complete AQ and formal context."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'engine'))
sys.path.insert(0, str(HERE / 'native'))
from run_part_b import Pipeline, Unit
from shared_results import Checker, environment, signature, sha, save_json
from aq_source_parser import extract_assertions


def measure(record, output, aq_config, *, dry_run=False, reuse_only=False, scope='hierarchy'):
    config = json.loads(Path(record['config']).read_text())
    root = Path(config['project_root'])
    source_path = Path(record['aq_file'])
    source = source_path.read_text()
    checker = Checker(source, record['assertions'], str(source_path))
    template = HERE / 'native/jasper_mutation_set_quality.tcl'
    # No model, variant, trial or output-directory identity in this key.
    identity = signature({'environment': environment(config, root, checker, source_path),
        'aq': checker.aq_sha256, 'scope': scope, 'template': sha(template.read_bytes()),
        'implementation': sha((HERE / 'native/run_part_b.py').read_bytes()),
        'adapter': sha(Path(__file__).read_bytes())})
    folder = output / '_native_results' / identity
    folder.mkdir(parents=True, exist_ok=True)
    canonical = folder / source_path.name
    if not canonical.is_file():
        canonical.write_text(source)
    source = canonical.read_text()
    if Checker(source, record['assertions'], str(source_path)).aq_sha256 != checker.aq_sha256:
        raise ValueError('native cached AQ identity mismatch')
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
    pipeline = Pipeline(folder / 'native_config.json', args)
    pipeline.experiment_hash = identity
    blocks, errors = extract_assertions(source)
    if errors or len(blocks) != len(record['assertions']):
        raise ValueError('native assertion inventory mismatch')
    resolve = lambda value: (root / value).resolve()
    unit = Unit(assertion_file=Path(record['source']).name, unit_id='aq', top=config['top'],
        module_guess='', a0_count=record.get('step_a_a0', 0), aq_count=len(blocks),
        qualification_yield=0, source_path=canonical, source=source,
        source_sha256=sha(source), blocks=blocks,
        rtl_files=[resolve(p) for p in config['rtl_files']],
        include_dirs=[resolve(p) for p in config.get('include_dirs', [])],
        bind_files=[resolve(p) for p in config.get('bind_files', [])],
        defines=config.get('defines', []), clock_mode=config.get('clock_mode', 'none'),
        clock=config.get('clock', ''), reset_expression=config.get('reset_expression', ''))
    ids = {b.assertion_id for b in blocks}
    previous = pipeline.coverage.get((unit.assertion_file, pipeline.subset_key(ids)))
    fresh = previous and previous.source_sha256 == unit.source_sha256 and previous.experiment_hash == identity
    if reuse_only and not fresh:
        raise ValueError(f'No native check_cov cache for {record["source"]}; --reuse-only forbids Jasper')
    result = pipeline.measure(unit, ids)
    if not dry_run:
        pipeline.require_pass(result)
    data = result.as_dict()
    denominator = result.checked + result.unchecked
    data['jasper_native_percent'] = 100 * result.checked / denominator if denominator else None
    data['fixed_universe_percent'] = 100 * result.checked / result.eligible if result.eligible else None
    data['cache_identity'] = identity
    data['mutation_scope'] = scope
    data['aq_sha256'] = checker.aq_sha256
    data['native_cache_directory'] = str(folder)
    return data
