import argparse
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
import contextlib
import io
from pathlib import Path

from native_batch import measure
from run_stepb import summarize
from run_batch import module_configs
from shared_results import save_json, sha, signature
from run_batch import pool_environment


class StepBTests(unittest.TestCase):
    def test_native_same_aq_reuses_session_and_new_subset_remeasures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / 'calls'
            tool = root / 'jg_fake'
            tool.write_text('''#!''' + sys.executable + '''
import pathlib, re, sys
counter = pathlib.Path(''' + repr(str(counter)) + ''')
with counter.open('a') as f: f.write('call\\n')
tcl = pathlib.Path(sys.argv[-1]).read_text()
n = int(re.search(r'set RQ2_EXPECTED_ASSERTS\\s+(\\d+)', tcl)[1])
print('RQ2_ACTIVE_ASSERTION_COUNT count='+str(n))
print('RQ2_ACTIVE_COVER_COUNT count=0')
print(f'RQ2_PROOF_COUNTS total={n} proven={n} bad=0')
print(f'RQ2_MUTATION_COUNTS checked={5*n} total=20 unchecked={20-5*n} undetectable=0 unprocessed=0 excluded=0')
print('RQ2_MUTATION_PASS')
''')
            tool.chmod(0o755)
            (root / 'rtl').mkdir()
            (root / 'rtl/top.sv').write_text('module top; endmodule')
            aqconfig = root / 'aq.json'
            save_json(aqconfig, {})
            source = root / 'aq.sv'
            source.write_text('module checker;\na: assert property (1);\nb: assert property (1);\nendmodule\n')
            config = root / 'config.json'
            save_json(config, {'project_root': str(root), 'top': 'top',
                'rtl_files': ['rtl/top.sv'], 'assertion_file': str(source),
                'clock_mode': 'none', 'jasper_executable': str(tool)})
            record = {'config': str(config), 'aq_file': str(source), 'source': str(source),
                      'assertions': ['a', 'b'], 'step_a_a0': 2}
            first = measure(record, root / 'out', aqconfig)
            second = measure(dict(record, model='different', run_number=3), root / 'out', aqconfig)
            self.assertEqual(first['jasper_native_percent'], 50)
            self.assertTrue(second['resumed'])
            self.assertEqual(len(counter.read_text().splitlines()), 1)
            source.write_text('module checker;\na: assert property (1);\n;\nendmodule\n')
            record['assertions'] = ['a']
            third = measure(record, root / 'out', aqconfig)
            self.assertEqual(third['jasper_native_percent'], 25)
            self.assertEqual(len(counter.read_text().splitlines()), 2)
            (root / 'rtl/top.sv').write_text('module top; wire changed; endmodule')
            with self.assertRaisesRegex(ValueError, 'No native'):
                measure(record, root / 'out', aqconfig, reuse_only=True)
            self.assertEqual(len(counter.read_text().splitlines()), 2)

    def test_dataset_averages_three_scores_and_refuses_missing_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checks, slots = [], []
            for trial, value in enumerate([20, 40, 90], 1):
                checks.append({'module': 'unit', 'variant': 'original', 'model': 'qwen', 'run': trial})
                slots.append({'variant': 'original', 'model': 'qwen', 'run': trial, 'aq_sha256': str(trial)})
                folder = root / 'unit/original/qwen' / f'run_{trial:02d}'
                save_json(folder / 'native_result.json', {'status': 'PASS', 'aq_sha256': str(trial),
                    'jasper_native_percent': value, 'fixed_universe_percent': value})
                save_json(folder / 'run_summary.json', {'status': 'PASS', 'aq_sha256': str(trial),
                    'fixed_budget_area_percent': value / 2})
                save_json(folder / 'evaluation/summary.json', {'macro_full_set_percent_lower': value,
                    'macro_full_set_percent_upper': value + 10, 'active_families': ['F1', 'F2']})
            save_json(root / 'step_a_output_check.json', checks)
            save_json(root / 'unit/step_a_outputs.json', {'slots': slots})
            self.assertTrue(summarize(root, root, ['qwen'], ['original']))
            avg = json.loads((root / 'comparison_average.json').read_text())[0]
            self.assertEqual(avg['jasper_mutation_coverage_percent'], 50)
            self.assertEqual(avg['our_full_AQ_detection_percent'], 50)
            self.assertEqual(avg['our_effective_area_percent'], 25)
            (root / 'unit/original/qwen/run_02/native_result.json').unlink()
            self.assertFalse(summarize(root, root, ['qwen'], ['original']))
            self.assertEqual(json.loads((root / 'comparison_average.json').read_text())[0]['status'], 'INCOMPLETE')

    def test_nested_sv_mapping_inherits_formal_and_bind_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_json(root / 'aq_out/manifests/inventory_files.json', [{'assertion_file': 'nested/unit.sv'}])
            save_json(root / 'aq.json', {'units': {'mapping': {'nested/*.sv': {
                'top': 'top', 'rtl_files': ['rtl/pkg.sv', 'rtl/top.sv'],
                'include_dirs': ['include'], 'defines': ['WIDTH=8']}}},
                'formal': {'clock_mode': 'clocked', 'clock': 'clk', 'reset_expression': 'rst'},
                'bind': {'mode': 'mapping', 'mapping': {'nested/*': ['bind.sv']}},
                'jasper': {'executable': 'jg'}, 'stages': {'syntax': {}, 'fpv': {}}})
            modules = module_configs(root, root / 'out', root / 'aq.json')
            config = json.loads(Path(modules[0]['base_config']).read_text())
            self.assertEqual(config['bind_files'], ['bind.sv'])
            self.assertEqual(config['clock'], 'clk')
            self.assertEqual(config['defines'], ['WIDTH=8'])
            self.assertEqual(config['include_dirs'], ['include'])
            self.assertEqual(modules[0]['target'], 'rtl/top.sv')

    def test_in_place_summary_declares_na_scope_and_keeps_real_errors_in_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / 'rtl').mkdir()
            (root / 'rtl/top.sv').write_text('module top; endmodule\n')
            aq = root / 'aq.json'
            save_json(aq, {})
            checks, native_values = [], {}
            names = ['good', 'no_candidates', 'one_class', 'native_zero']
            for name in names:
                config = root / name / 'config.json'
                save_json(config, {'project_root': str(root), 'rtl_files': ['rtl/top.sv'],
                                   'assertion_file': 'unused', 'top': 'top', 'clock_mode': 'none'})
                families = [] if name == 'no_candidates' else ['F5'] if name == 'one_class' else ['F3', 'F5']
                mutants = []
                for i, family in enumerate(families):
                    path = root / name / 'fault_pool/generated' / f'{family}_{i}.sv'
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f'module top; wire {family}; endmodule\n')
                    mutants.append({'id': f'{family}_{i}', 'family': family, 'replace': 'rtl/top.sv',
                        'file': str(path), 'sha256': sha(path.read_bytes()), 'syntax_validated': True})
                pool = root / name / 'fault_pool'
                manifest = {'assertions': ['a'], 'mutants': mutants}
                save_json(pool / 'generated/manifest.json', manifest)
                save_json(pool / 'validation/validated_manifest.json', manifest)
                save_json(pool / 'validation/pool_context.json', {'environment_sha256': pool_environment(config),
                                                                'manifest_sha256': signature(manifest)})
                if mutants:
                    (pool / 'validation/validation_report.csv').write_text('id,status,reason\n' +
                        ''.join(m['id'] + ',PASS,ok\n' for m in mutants))
                slots = []
                for trial in range(1, 4):
                    identity = name + str(trial)
                    checks.append({'module': name, 'variant': 'original', 'model': 'qwen', 'run': trial, 'aq_count': 1})
                    slots.append({'variant': 'original', 'model': 'qwen', 'run': trial, 'aq_sha256': identity})
                    record = {'config': str(config), 'assertions': ['a'], 'module': name, 'trial': trial}
                    save_json(root / name / 'checked_step_a_outputs/original/qwen' / f'run_{trial:02d}' / 'input/export_record.json', record)
                    native_values[name, trial] = {'status': 'NO_ELIGIBLE_MUTANTS' if name == 'native_zero' else 'PASS',
                        'aq_sha256': identity, 'cache_identity': identity,
                        'jasper_native_percent': None if name == 'native_zero' else [20, 40, 90][trial-1],
                        'fixed_universe_percent': None if name == 'native_zero' else 50}
                    if name == 'good':
                        slot = root / name / 'original/qwen' / f'run_{trial:02d}'
                        value = [30, 50, 70][trial-1]
                        save_json(slot / 'run_summary.json', {'status': 'PASS', 'aq_sha256': identity,
                            'pool_sha256': signature(mutants), 'fixed_budget_area_percent': value / 2})
                        save_json(slot / 'evaluation/summary.json', {'assertion_count': 1,
                            'macro_full_set_percent_lower': value, 'macro_full_set_percent_upper': value,
                            'active_families': families})
                save_json(root / name / 'step_a_outputs.json', {'slots': slots})
            save_json(root / 'step_a_output_check.json', checks)
            def cached(record, *args, **kwargs):
                self.assertTrue(kwargs.get('reuse_only'), 'summary may never launch Jasper')
                return native_values[record['module'], record['trial']]
            with patch('native_repair.measure', side_effect=cached), contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(summarize(root, root, ['qwen'], ['original'], aq_config=aq))
            scope = json.loads((root / 'module_scope.json').read_text())
            self.assertEqual(scope['evaluated_modules'], ['good'])
            self.assertEqual(set(scope['holdout_not_applicable_modules']), {'no_candidates', 'one_class'})
            self.assertEqual(set(scope['native_not_applicable_modules']), {'native_zero'})
            result = json.loads((root / 'comparison_average.json').read_text())[0]
            self.assertEqual(result['status'], 'PASS_ON_APPLICABLE_SCOPE')
            self.assertEqual(result['jasper_mutation_coverage_percent'], 50)
            self.assertEqual(result['our_full_AQ_detection_percent'], 50)
            self.assertEqual(result['evaluated_module_count'], 1)
            self.assertIn('50.00%', (root / 'comparison_average.md').read_text())
            metadata_path = root / 'no_candidates/fault_pool/validation/pool_context.json'
            metadata = json.loads(metadata_path.read_text())
            metadata['environment_sha256'] = 'stale'
            save_json(metadata_path, metadata)
            with patch('native_repair.measure', side_effect=cached), contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(summarize(root, root, ['qwen'], ['original'], aq_config=aq))
            scope = json.loads((root / 'module_scope.json').read_text())
            self.assertIn('no_candidates', scope['evaluated_modules'])
            result = json.loads((root / 'comparison_average.json').read_text())[0]
            self.assertEqual(result['status'], 'INCOMPLETE')
            self.assertEqual(result['jasper_mutation_coverage_percent'], '')


if __name__ == '__main__':
    unittest.main()
