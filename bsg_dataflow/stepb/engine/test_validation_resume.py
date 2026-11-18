import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import validate_mutants as validation
from process_watch import WatchedResult
from shared_results import save_json


class ValidationResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'rtl').mkdir()
        (self.root / 'rtl/top.sv').write_text('module top; endmodule\n')
        (self.root / 'rtl/defs.svh').write_text('`define X 1\n')
        (self.root / 'aq.sv').write_text('module p; a: assert property (1); endmodule\n')
        self.config, self.manifest, self.output = (self.root / x for x in ['config.json', 'manifest.json', 'out'])
        save_json(self.config, {'project_root': str(self.root), 'rtl_files': ['rtl/top.sv'],
            'assertion_file': 'aq.sv', 'top': 'top', 'clock_mode': 'none', 'include_dirs': ['rtl']})
        mutants = []
        for i in range(1, 4):
            path = self.root / f'm{i}.sv'
            path.write_text(f'module top; wire x{i}; endmodule\n')
            mutants.append({'id': f'F3_{i:03d}', 'family': 'F3', 'replace': 'rtl/top.sv', 'file': str(path)})
        save_json(self.manifest, {'assertions': ['a'], 'mutants': mutants})
        self.calls = []

    def tool(self, command, **kwargs):
        job = kwargs['log_path'].parent.parent.name
        self.calls.append(job)
        text = 'FH_SYNTAX_FAIL intentional invalid mutant\n' if job == 'F3_002' else 'FH_SYNTAX_PASS\n'
        kwargs['log_path'].write_text(text)
        return WatchedResult(0, text, False, 0.1, 12345)

    def run_validation(self, tool=None):
        with patch.object(validation, 'run_logged', side_effect=tool or self.tool), contextlib.redirect_stdout(io.StringIO()):
            return validation.validate(self.config, self.manifest, self.output)

    def test_interrupt_reuses_finished_pass_and_fail_and_only_runs_pending(self):
        def interrupted(command, **kwargs):
            if kwargs['log_path'].parent.parent.name == 'F3_003':
                raise KeyboardInterrupt
            return self.tool(command, **kwargs)
        with self.assertRaises(KeyboardInterrupt):
            self.run_validation(interrupted)
        self.assertFalse((self.output / 'validated_manifest.json').exists())
        stats = self.run_validation()
        self.assertEqual(stats, {'reused': 2, 'measured': 1, 'candidates': 3})
        self.assertEqual(self.calls, ['F3_001', 'F3_002', 'F3_003'])
        accepted = json.loads((self.output / 'validated_manifest.json').read_text())['mutants']
        self.assertEqual([m['id'] for m in accepted], ['F3_001', 'F3_003'])
        self.assertEqual(self.run_validation()['measured'], 0)

    def test_changed_header_invalidates_all_checkpoints(self):
        self.run_validation()
        (self.root / 'rtl/defs.svh').write_text('`define X 2\n')
        self.assertEqual(self.run_validation()['measured'], 3)

    def test_changed_mutant_invalidates_only_that_job(self):
        self.run_validation()
        (self.root / 'm3.sv').write_text('module top; wire new_value; endmodule\n')
        self.assertEqual(self.run_validation()['measured'], 1)

    def test_license_timeout_does_not_filter_a_mutant_or_cache_failure(self):
        def timeout(command, **kwargs):
            text = 'INFO: Waiting for license\n'
            kwargs['log_path'].write_text(text)
            return WatchedResult(-15, text, True, 1200, 12345)
        with self.assertRaisesRegex(RuntimeError, 'did not complete'):
            self.run_validation(timeout)
        self.assertFalse((self.output / 'validated_manifest.json').exists())
        self.assertEqual(list(self.output.rglob('validation_result.json')), [])
        self.assertEqual(self.run_validation()['measured'], 3)

    def test_tampered_log_rechecks_only_affected_job(self):
        self.run_validation()
        checkpoint = json.loads((self.output / 'runs/F3_002/validation_result.json').read_text())
        Path(checkpoint['log_file']).write_text('FH_SYNTAX_PASS\n')
        self.assertEqual(self.run_validation()['measured'], 1)

    def test_old_logs_are_preserved_but_cannot_substitute_for_input_hashes(self):
        old = self.output / 'runs/F3_001/run.log'
        old.parent.mkdir(parents=True)
        old.write_text('FH_SYNTAX_PASS\n')
        self.assertEqual(self.run_validation()['measured'], 3)
        self.assertEqual(old.read_text(), 'FH_SYNTAX_PASS\n')


if __name__ == '__main__':
    unittest.main()
