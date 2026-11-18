import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import native_batch
import native_repair
from shared_results import save_json
from run_stepb import summarize


class NativeRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'rtl').mkdir()
        (self.root / 'rtl/top.sv').write_text('module top; endmodule\n')
        self.source = self.root / 'aq.sv'
        self.source.write_text('module props; a: assert property (1); endmodule\n')
        self.mode, self.calls = self.root / 'mode', self.root / 'calls'
        self.mode.write_text('success')
        tool = self.root / 'fake_jg'
        tool.write_text('#!' + sys.executable + '\n' + '''import pathlib,sys
root = pathlib.Path(''' + repr(str(self.root)) + ''')
with (root/'calls').open('a') as f: f.write('call\\n')
mode = (root/'mode').read_text()
tcl = pathlib.Path(sys.argv[-1]).read_text()
repaired = 'RQ2_SOURCE_LABELS' in tcl
n = 8 if mode in ['expanded', 'foreign'] else 1
print(f'RQ2_ACTIVE_ASSERTION_COUNT count={n}')
for i in range(n):
    label = 'unselected' if mode == 'foreign' and i == n-1 else 'a'
    print(f'RQ2_ACTIVE_ASSERTION name=top.i_assertions.g[{i}].{label}')
if n > 1 and not repaired:
    print('RQ2_MUTATION_ERROR')
    sys.exit(0)
print('RQ2_ACTIVE_COVER_COUNT count=0')
print(f'RQ2_PROOF_COUNTS total={n} proven={n} bad=0')
if mode == 'zero':
    print('RQ2_MUTATION_COUNTS checked=0 total=0 unchecked=0 undetectable=0 unprocessed=0 excluded=0')
else:
    print('RQ2_MUTATION_COUNTS checked=6 total=10 unchecked=4 undetectable=0 unprocessed=0 excluded=0')
print('RQ2_MUTATION_PASS')
''')
        tool.chmod(0o755)
        self.aq_config = self.root / 'aq_config.json'
        save_json(self.aq_config, {})
        self.config = self.root / 'config.json'
        save_json(self.config, {'project_root': str(self.root), 'top': 'top', 'rtl_files': ['rtl/top.sv'],
            'assertion_file': str(self.source), 'clock_mode': 'none', 'jasper_executable': str(tool)})
        self.record = {'config': str(self.config), 'aq_file': str(self.source), 'source': 'aq.sv',
                       'assertions': ['a'], 'step_a_a0': 1}
        self.output = self.root / 'out'

    def old(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return native_batch.measure(self.record, self.output, self.aq_config)

    def repair(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return native_repair.measure(self.record, self.output, self.aq_config, **kwargs)

    def count(self):
        return len(self.calls.read_text().splitlines()) if self.calls.exists() else 0

    def test_old_success_keeps_identity_and_never_reruns(self):
        old = self.old()
        files = {p: p.read_bytes() for p in Path(old['native_cache_directory']).rglob('*.json')}
        repaired = self.repair(known_gaps_only=True)
        self.assertEqual(repaired['cache_identity'], old['cache_identity'])
        self.assertEqual(repaired['jasper_native_percent'], 60)
        self.assertEqual(self.count(), 1)
        self.assertEqual(files, {p: p.read_bytes() for p in files})

    def test_complete_zero_report_is_recovered_without_jasper(self):
        self.mode.write_text('zero')
        with self.assertRaisesRegex(Exception, 'eligible mutation items = 0'):
            self.old()
        old_files = {p: p.read_bytes() for p in self.output.rglob('result.json')}
        result = self.repair(reuse_only=True)
        self.assertEqual(result['status'], 'NO_ELIGIBLE_MUTANTS')
        self.assertIsNone(result['jasper_native_percent'])
        self.assertIsNone(result['fixed_universe_percent'])
        self.assertEqual(self.count(), 1)
        self.assertEqual(old_files, {p: p.read_bytes() for p in old_files})

    def test_only_failed_expanded_context_is_measured_once_then_shared(self):
        self.mode.write_text('expanded')
        with self.assertRaisesRegex(Exception, 'active assertions=8, expected=1'):
            self.old()
        old_path = next(self.output.rglob('result.json'))
        old_bytes = old_path.read_bytes()
        result = self.repair(known_gaps_only=True)
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual(result['source_assertion_count'], 1)
        self.assertEqual(result['active_assertion_count'], 8)
        self.assertEqual(len(result['source_instance_map']['a']), 8)
        self.assertEqual(result['jasper_native_percent'], 60)
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.repair(known_gaps_only=True)['status'], 'PASS')
        self.assertEqual(self.repair(reuse_only=True)['status'], 'PASS')
        self.assertEqual(self.count(), 2)
        self.assertEqual(old_path.read_bytes(), old_bytes)

    def test_unknown_property_is_rejected_and_failure_is_not_repeated(self):
        self.mode.write_text('foreign')
        with self.assertRaises(Exception):
            self.old()
        with self.assertRaisesRegex(ValueError, 'not uniquely mapped'):
            self.repair(known_gaps_only=True)
        self.assertEqual(self.count(), 2)
        with self.assertRaisesRegex(RuntimeError, 'REUSE FAILURE'):
            self.repair(known_gaps_only=True)
        self.assertEqual(self.count(), 2)

    def test_repair_only_refuses_new_or_changed_context_without_starting_jasper(self):
        with self.assertRaisesRegex(ValueError, 'No matching previous'):
            self.repair(known_gaps_only=True)
        self.assertEqual(self.count(), 0)
        self.old()
        (self.root / 'rtl/top.sv').write_text('module top; wire changed; endmodule\n')
        with self.assertRaisesRegex(ValueError, 'No matching previous'):
            self.repair(known_gaps_only=True)
        self.assertEqual(self.count(), 1)

    def test_saved_zero_without_completed_proof_is_not_treated_as_na(self):
        self.mode.write_text('zero')
        with self.assertRaises(Exception):
            self.old()
        log = next(self.output.rglob('run.log'))
        log.write_text(log.read_text().replace('proven=1 bad=0', 'proven=0 bad=1'))
        with self.assertRaisesRegex(ValueError, 'measurement is missing'):
            self.repair(reuse_only=True)
        self.assertEqual(self.count(), 1)

    def test_na_native_does_not_erase_completed_our_scores_or_invent_an_average(self):
        checks, slots = [], []
        for trial in range(1, 4):
            checks.append({'module': 'unit', 'variant': 'original', 'model': 'qwen', 'run': trial, 'aq_count': 1})
            slots.append({'variant': 'original', 'model': 'qwen', 'run': trial, 'aq_sha256': str(trial)})
            folder = self.output / 'unit/original/qwen' / f'run_{trial:02d}'
            save_json(folder / 'native_result.json', {'status': 'NO_ELIGIBLE_MUTANTS', 'aq_sha256': str(trial),
                'jasper_native_percent': None, 'fixed_universe_percent': None})
            save_json(folder / 'run_summary.json', {'status': 'PASS', 'aq_sha256': str(trial), 'fixed_budget_area_percent': 20})
            save_json(folder / 'evaluation/summary.json', {'assertion_count': 1, 'active_families': ['F1', 'F2'],
                'macro_full_set_percent_lower': 50, 'macro_full_set_percent_upper': 50})
        save_json(self.output / 'step_a_output_check.json', checks)
        save_json(self.output / 'unit/step_a_outputs.json', {'slots': slots})
        self.assertTrue(summarize(self.root, self.output, ['qwen'], ['original']))
        row = json.loads((self.output / 'comparison_average.json').read_text())[0]
        self.assertEqual(row['status'], 'PASS_WITH_UNDEFINED_NATIVE')
        self.assertEqual(row['our_full_AQ_detection_percent'], 50)
        self.assertEqual(row['jasper_mutation_coverage_percent'], '')

    @unittest.skipUnless(shutil.which('tclsh'), 'Tcl runtime unavailable')
    def test_actual_tcl_maps_generated_instances_and_rejects_foreign_properties(self):
        folder = self.root / 'tclrun'
        folder.mkdir()
        values = {'project_root': str(self.root), 'run_dir': str(folder), 'assertion_file': str(self.source),
            'top': 'top', 'subset_key': 'test', 'expected_assertions': '1', 'removed_cover_count': '0',
            'rtl_files_tcl': '{' + str(self.root / 'rtl/top.sv') + '}', 'include_dirs_tcl': '',
            'defines_tcl': '', 'bind_files_tcl': '', 'clock_mode_tcl': '{none}', 'clock_tcl': '{}',
            'reset_expression_tcl': '{}', 'mutation_scope_tcl': '{hierarchy}'}
        template = (Path(native_repair.__file__).parent / 'native/jasper_mutation_instances.tcl').read_text()
        script = native_repair.Pipeline._render(template.replace('@@SOURCE_LABELS@@', '{a}'), values)
        commands = '''rename exit real_exit
proc exit {args} {real_exit 0}
proc clear {args} {}
proc analyze {args} {}
proc elaborate {args} {}
proc clock {args} {}
proc reset {args} {}
proc prove {args} {puts PROOF_WAS_CALLED}
proc get_property_list {args} {
    if {[lindex $args 1] eq "type assert"} {return $::TEST_PROPERTIES}
    return [list]
}
proc get_property_info {args} {return [dict create status proven]}
proc check_cov {args} {
    if {[lindex $args 0] eq "-report"} {
        return [list [dict create top [dict create checker_coverage [dict create checked 6 total 10 unchecked 4 undetectable 0 unprocessed 0 excluded 0]]]]
    }
}
'''
        names = ['top.i_assertions.g[' + str(i) + '].a' for i in range(8)]
        for foreign in [False, True]:
            with self.subTest(foreign=foreign):
                current = list(names)
                if foreign:
                    current[-1] = 'top.i_assertions.extra.unselected'
                prelude = 'set TEST_PROPERTIES [list ' + ' '.join('{' + n + '}' for n in current) + ']\n'
                run = subprocess.run([shutil.which('tclsh')], input=commands + prelude + script,
                                     text=True, capture_output=True)
                self.assertEqual(run.stderr, '')
                if foreign:
                    self.assertIn('RQ2_ASSERTION_MAPPING_ERROR', run.stdout)
                    self.assertNotIn('PROOF_WAS_CALLED', run.stdout)
                    self.assertNotIn('RQ2_MUTATION_PASS', run.stdout)
                else:
                    self.assertIn('RQ2_SOURCE_ASSERTION label=a instances=8', run.stdout)
                    self.assertIn('RQ2_PROOF_COUNTS total=8 proven=8 bad=0', run.stdout)
                    self.assertIn('RQ2_MUTATION_PASS', run.stdout)


if __name__ == '__main__':
    unittest.main()
