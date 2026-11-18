import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import native_batch  # also establishes bundled engine imports
import test_step_a_outputs as step_a_fixture
import test_export_model_aq as export_fixture
from run_batch import module_configs, pool_environment
from shared_results import save_json, sha, signature
from run_stepb import summarize

HERE = Path(__file__).resolve().parent


class EmptyAQTests(unittest.TestCase):
    def test_common_scope_reports_all_empty_module_without_diluting_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checks = []
            for module, count in [('measured', 1), ('empty', 0)]:
                slots = []
                for trial in range(1, 4):
                    checks.append({'module': module, 'variant': 'original', 'model': 'qwen',
                                   'run': trial, 'aq_count': count})
                    slots.append({'variant': 'original', 'model': 'qwen', 'run': trial,
                                  'aq_sha256': str(trial)})
                    if count:
                        folder = root / module / 'original/qwen' / f'run_{trial:02d}'
                        save_json(folder / 'native_result.json', {'status': 'PASS', 'aq_sha256': str(trial),
                            'jasper_native_percent': 50, 'fixed_universe_percent': 40})
                        save_json(folder / 'run_summary.json', {'status': 'PASS', 'aq_sha256': str(trial),
                            'fixed_budget_area_percent': 25})
                        save_json(folder / 'evaluation/summary.json', {'assertion_count': 1,
                            'macro_full_set_percent_lower': 60, 'macro_full_set_percent_upper': 60,
                            'active_families': ['F1', 'F2']})
                save_json(root / module / 'step_a_outputs.json', {'slots': slots})
            save_json(root / 'step_a_output_check.json', checks)
            self.assertTrue(summarize(root, root, ['qwen'], ['original']))
            result = json.loads((root / 'comparison_average.json').read_text())[0]
            self.assertEqual(result['evaluated_module_count'], 1)
            self.assertEqual(result['total_inventory_modules'], 2)
            self.assertEqual(result['all_empty_aq_modules'], 'empty')
            self.assertEqual(result['jasper_mutation_coverage_percent'], 50)
            self.assertEqual(result['our_full_AQ_detection_percent'], 60)

    def fixture(self):
        case = step_a_fixture.StepAOutputTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        root = case.root
        counter = root / 'calls.jsonl'
        tool = root / 'fake_jg'
        tool.write_text('#!' + sys.executable + '''
import pathlib, re, sys
t = pathlib.Path(sys.argv[-1]).read_text()
with pathlib.Path(''' + repr(str(counter)) + ''').open('a') as f: f.write('call\\n')
if 'RQ2_EXPECTED_ASSERTS' in t:
 n = int(re.search(r'set RQ2_EXPECTED_ASSERTS\\s+(\\d+)', t)[1])
 print('RQ2_ACTIVE_ASSERTION_COUNT count='+str(n))
 source = pathlib.Path(re.search(r'set RQ2_ASSERTION_FILE\\s+\\[file normalize "([^"]+)"\\]', t)[1]).read_text()
 names = re.findall(r'([A-Za-z_]\\w*)\\s*:\\s*assert\\s+property', source)
 for name in names: print('RQ2_ACTIVE_ASSERTION name=top.i_assertions.'+name)
 print('RQ2_ACTIVE_COVER_COUNT count=0')
 print(f'RQ2_PROOF_COUNTS total={n} proven={n} bad=0')
 print(f'RQ2_MUTATION_COUNTS checked={n} total=10 unchecked={10-n} undetectable=0 unprocessed=0 excluded=0')
 print('RQ2_MUTATION_PASS')
else:
 source = pathlib.Path(re.search(r'set FH_ASSERTION \\{([^}]+)\\}', t)[1]).read_text()
 names = re.findall(r'([A-Za-z_]\\w*)\\s*:\\s*assert\\s+property', source)
 for name in names: print('FH_PROPERTY\\t'+name+'\\t'+('cex' if name=='a' else 'proven'))
 print('FH_DONE')
''')
        tool.chmod(0o755)
        aq = root / 'aq.json'
        save_json(aq, {'units': {'mapping': {'*block_assertions.v': {
            'top': 'top', 'rtl_files': ['rtl/top.sv'], 'clock_mode': 'none'}}},
            'rtl': {'include_dirs': ['rtl'], 'defines': []},
            'jasper': {'executable': str(tool)}, 'stages': {'syntax': {}, 'fpv': {}}})
        return case, aq, counter

    def command(self, case, aq):
        return [sys.executable, str(HERE / 'run_stepb.py'), '--project-root', str(case.root),
                '--aq-config', str(aq), '--output', str(case.out), '--variants', 'original']

    def test_zero_technical_aq_is_valid_for_all_models_and_launches_no_tools(self):
        case, aq, counter = self.fixture()
        manifests = case.root / 'aq_out/manifests'
        rows = json.loads((manifests / 'vacuity.json').read_text())
        for row in rows:
            row['status'] = 'VACUOUS'
        save_json(manifests / 'vacuity.json', rows)
        for model in step_a_fixture.MODELS:
            for trial in range(1, 4):
                folder = case.run_folder(model, trial)
                summary = json.loads((folder / 'summary.json').read_text())
                summary.update(technical_aq=0, pass_count=0, final_aq=0,
                               post_spec_qualification_yield_percent=0)
                save_json(folder / 'summary.json', summary)
                save_json(folder / 'spec_check_results.json', [])
        completed = subprocess.run([*self.command(case, aq), '--reuse-only'], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertFalse(counter.exists())
        checks = json.loads((case.out / 'step_a_output_check.json').read_text())
        self.assertEqual(len(checks), 12)
        self.assertTrue(all(r['status'] == 'INPUTS_VERIFIED' and r['aq_count'] == 0 for r in checks))
        scope = json.loads((case.out / 'module_scope.json').read_text())
        self.assertEqual(scope['all_empty_aq_modules'], ['block_assertions'])
        averages = json.loads((case.out / 'comparison_average.json').read_text())
        self.assertEqual(len(averages), 4)
        self.assertTrue(all(r['status'] == 'EMPTY_AQ' and r['jasper_mutation_coverage_percent'] == '' for r in averages))
        # Missing outputs are still an error; an absent file cannot become empty AQ.
        (case.run_folder('qwen', 2) / 'spec_check_results.json').unlink()
        bad = subprocess.run([*self.command(case, aq), '--check-only'], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        self.assertFalse(counter.exists())

    def test_model_can_reject_all_technically_qualified_assertions(self):
        case = export_fixture.ExportModelAQTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.prepare('original')
        folder = case.root / 'aq_out/spec_guard_four_models/deepseek/run_01'
        rows = json.loads((folder / 'spec_check_results.json').read_text())
        for row in rows:
            row['verdict'] = 'FAIL'
        save_json(folder / 'spec_check_results.json', rows)
        summary = json.loads((folder / 'summary.json').read_text())
        summary.update(pass_count=0, final_aq=0)
        save_json(folder / 'summary.json', summary)
        record = export_fixture.export(case.base_config, 'deepseek', 'original', 1,
                                       export_fixture.SOURCE_NAME, case.root / 'empty_export')
        self.assertEqual(record['technical_count'], 2)
        self.assertEqual(record['aq_state'], 'EMPTY_AQ')
        self.assertEqual(record['assertions'], [])
        blocks, errors = case.parser.extract_assertions(Path(record['aq_file']).read_text())
        self.assertEqual((blocks, errors), ([], []))

    def test_one_empty_trial_uses_same_pool_zero_detection_and_native_na(self):
        case, aq, counter = self.fixture()
        folder = case.run_folder('qwen', 1)
        rows = json.loads((folder / 'spec_check_results.json').read_text())
        for row in rows:
            row['verdict'] = 'FAIL'
        save_json(folder / 'spec_check_results.json', rows)
        summary = json.loads((folder / 'summary.json').read_text())
        summary.update(pass_count=0, final_aq=0, post_spec_qualification_yield_percent=0)
        save_json(folder / 'summary.json', summary)
        module_configs(case.root, case.out, aq)
        mutants = []
        for family in ['F1', 'F2']:
            file = case.root / (family + '.sv')
            file.write_text('module top; wire ' + family + '; endmodule')
            mutants.append({'id': family + '_001', 'family': family, 'replace': 'rtl/top.sv',
                'file': str(file), 'sha256': sha(file.read_bytes()), 'syntax_validated': True})
        pool = case.out / 'block_assertions/fault_pool/validation'
        validated = {'assertions': [b.property_name for b in case.blocks], 'mutants': mutants}
        save_json(pool / 'validated_manifest.json', validated)
        (pool / 'validation_report.csv').write_text('id,status\nF1_001,PASS\nF2_001,PASS\n')
        save_json(pool / 'pool_context.json', {'environment_sha256': pool_environment(
            case.out / 'block_assertions/base_config.json'), 'manifest_sha256': signature(validated)})
        command = [*self.command(case, aq), '--models', 'qwen']
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        result = json.loads((case.out / 'comparison_average.json').read_text())[0]
        self.assertEqual(result['status'], 'PASS_WITH_UNDEFINED_NATIVE')
        self.assertEqual(result['jasper_mutation_coverage_percent'], '')
        self.assertAlmostEqual(result['our_full_AQ_detection_percent'], 200 / 3)
        self.assertEqual(result['evaluated_module_count'], 1)
        empty_trial = json.loads((case.out / 'block_assertions/original/qwen/run_01/evaluation/summary.json').read_text())
        self.assertEqual(empty_trial['macro_full_set_percent_lower'], 0)
        before = counter.read_text()
        repeated = subprocess.run([*command, '--reuse-only'], capture_output=True, text=True)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(counter.read_text(), before)
        repaired = subprocess.run([*command, '--stage', 'native', '--repair-native-only'],
                                  capture_output=True, text=True)
        self.assertEqual(repaired.returncode, 0, repaired.stdout + repaired.stderr)
        self.assertNotIn('[2/4]', repaired.stdout)
        self.assertEqual(counter.read_text(), before)
        before_dirs = {p for p in case.out.rglob('*') if p.is_dir()}
        summary_only = subprocess.run([*command, '--stage', 'summary'], capture_output=True, text=True)
        self.assertEqual(summary_only.returncode, 0, summary_only.stdout + summary_only.stderr)
        self.assertNotIn('[2/4]', summary_only.stdout)
        self.assertNotIn('[3/4]', summary_only.stdout)
        self.assertEqual(counter.read_text(), before)
        self.assertEqual(before_dirs, {p for p in case.out.rglob('*') if p.is_dir()})


if __name__ == '__main__':
    unittest.main()
