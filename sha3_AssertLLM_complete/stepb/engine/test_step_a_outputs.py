import csv
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aq_source_parser import extract_assertions
from run_batch import module_configs, pool_environment
from shared_results import save_json, sha, signature
from step_a_outputs import inspect_outputs


MODELS = ['qwen', 'deepseek', 'gpt', 'gemini']
SOURCE = '''module props;
logic x;
a: assert property (x);
b: assert property (!x);
c: assert property (x == 1);
d: assert property (x != 0);
e: assert property (x || !x);
endmodule
'''


class StepAOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'assertions').mkdir()
        (self.root / 'assertions/block_assertions.v').write_text(SOURCE)
        (self.root / 'rtl').mkdir()
        (self.root / 'rtl/top.sv').write_text('module top; endmodule\n')
        self.blocks, errors = extract_assertions(SOURCE)
        self.assertFalse(errors)
        manifests = self.root / 'aq_out/manifests'
        save_json(manifests / 'inventory_files.json', [{'assertion_file': 'block_assertions.v', 'source_sha256': sha(SOURCE)}])
        save_json(manifests / 'inventory_assertions.json', [{'assertion_file': 'block_assertions.v',
            'assertion_id': b.assertion_id, 'property_name': b.property_name, 'fingerprint': b.fingerprint} for b in self.blocks])
        save_json(manifests / 'vacuity.json', [{'assertion_file': 'block_assertions.v',
            'assertion_id': b.assertion_id, 'status': 'PASS'} for b in self.blocks])
        for index, model in enumerate(MODELS):
            for trial in range(1, 4):
                number = index * 3 + trial - 1
                results = [{'assertion_file': 'block_assertions.v', 'assertion_id': b.assertion_id,
                    'property_name': b.property_name, 'assertion_text': b.text,
                    'verdict': 'PASS' if i == 0 or number & (1 << (i-1)) else 'FAIL',
                    'model_key': model, 'model_id': model + '-test', 'run_number': trial,
                    'fingerprint': sha(b.assertion_id + model + str(trial))} for i, b in enumerate(self.blocks)]
                folder = self.run_folder(model, trial)
                save_json(folder / 'spec_check_results.json', results)
                count = sum(r['verdict'] == 'PASS' for r in results)
                save_json(folder / 'summary.json', {'run_status': 'COMPLETE', 'input_variant': 'NORMAL',
                    'model_key': model, 'model_id': model + '-test', 'run_number': trial,
                    'a0_count': 5, 'technical_aq': 5, 'pass_count': count, 'final_aq': count,
                    'post_spec_qualification_yield_percent': 100 * count / 5})
        self.out = self.root / 'out'

    def run_folder(self, model, trial):
        return self.root / 'aq_out/spec_guard_four_models' / model / f'run_{trial:02d}'

    def test_twelve_distinct_actual_outputs_are_preserved_and_share_two_proof_sessions(self):
        counter = self.root / 'calls.jsonl'
        fake = self.root / 'fake_jg'
        fake.write_text('''#!/usr/bin/env python3
import sys,re,pathlib,json
p=pathlib.Path(sys.argv[-1])
a=pathlib.Path(re.search(r'set FH_ASSERTION \\{([^}]+)\\}',p.read_text())[1])
names=re.findall(r'([A-Za-z_]\\w*)\\s*:\\s*assert\\s+property',a.read_text())
with pathlib.Path(''' + repr(str(counter)) + ''').open('a') as f:f.write(json.dumps(names)+'\\n')
for name in names: print('FH_PROPERTY\\t'+name+'\\t'+('cex' if name=='a' else 'proven'))
print('FH_DONE')
''')
        fake.chmod(0o755)
        aqconfig = self.root / 'aq.json'
        save_json(aqconfig, {'units': {'mapping': {'*block_assertions.v': {
            'top': 'top', 'rtl_files': ['rtl/top.sv'], 'clock_mode': 'none'}}},
            'rtl': {'include_dirs': ['rtl'], 'defines': []},
            'jasper': {'executable': str(fake)}, 'stages': {'syntax': {}, 'fpv': {}}})
        module_configs(self.root, self.out, aqconfig)
        base = self.out / 'block_assertions/base_config.json'
        mutants = []
        for family in ['F1', 'F2']:
            file = self.root / (family + '.sv')
            file.write_text(f'module top; wire {family}; endmodule\n')
            mutants.append({'id': family + '_001', 'family': family, 'replace': 'rtl/top.sv',
                            'file': str(file), 'sha256': sha(file.read_bytes()), 'syntax_validated': True})
        pool = self.out / 'block_assertions/fault_pool/validation'
        validated = {'assertions': [b.property_name for b in self.blocks], 'mutants': mutants}
        save_json(pool / 'validated_manifest.json', validated)
        (pool / 'validation_report.csv').write_text('id,status\nF1_001,PASS\nF2_001,PASS\n')
        save_json(pool / 'pool_context.json', {'environment_sha256': pool_environment(base),
                                             'manifest_sha256': signature(validated)})
        command = [sys.executable, str(Path(__file__).parent / 'run_batch.py'),
            '--project-root', str(self.root), '--aq-config', str(aqconfig),
            '--output', str(self.out), '--backend', 'lexical']
        completed = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(len(counter.read_text().splitlines()), 2)
        self.assertEqual(set(json.loads(counter.read_text().splitlines()[0])), {'a','b','c','d','e'})
        report = json.loads((self.out / 'step_a_outputs.json').read_text())
        self.assertEqual(report['verified_slots'], 12)
        self.assertEqual(report['unique_final_aq_contents'], 12)
        module_report = json.loads((self.out / 'block_assertions/step_a_outputs.json').read_text())
        self.assertEqual(module_report['unique_aq_sets'], 12)
        for index, model in enumerate(MODELS):
            for trial in range(1,4):
                folder = self.out / 'block_assertions/original' / model / f'run_{trial:02d}'
                raw = json.loads((self.run_folder(model,trial) / 'spec_check_results.json').read_text())
                wanted = {r['property_name'] for r in raw if r['verdict']=='PASS'}
                manifest = json.loads((folder / 'manifest.json').read_text())
                self.assertEqual(set(manifest['assertions']), wanted)
                self.assertTrue((folder / 'evaluation/macro_curve.svg').is_file())
                self.assertEqual(json.loads((folder / 'jasper/reuse_stats.json').read_text())['jasper_sessions'],0)
                self.assertFalse((folder / 'jasper/runs/golden').exists())
        # Second invocation must reuse both the pool and every property result.
        repeated = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
        self.assertEqual(len(counter.read_text().splitlines()), 2)

    def test_missing_trial_is_not_replaced_by_another_trial(self):
        (self.run_folder('gpt', 2) / 'spec_check_results.json').unlink()
        with self.assertRaisesRegex(ValueError, 'Step A outputs are missing'):
            inspect_outputs(self.root, self.out, MODELS, ['original'])
        report = json.loads((self.out / 'step_a_outputs.json').read_text())
        self.assertEqual(report['verified_slots'], 11)

    def test_copied_trial_metadata_is_rejected(self):
        path = self.run_folder('gpt', 2) / 'spec_check_results.json'
        rows = json.loads(path.read_text())
        for row in rows:
            row['run_number'] = 1
        save_json(path, rows)
        with self.assertRaisesRegex(ValueError, 'Step A outputs are missing'):
            inspect_outputs(self.root, self.out, MODELS, ['original'])

    def test_mismatched_model_api_trace_is_rejected(self):
        folder=self.run_folder('qwen',1)
        path=folder/'spec_check_results.json'
        rows=json.loads(path.read_text())
        rows[0]['trace_file']='qwen/run_01/traces/a.json'
        save_json(path,rows)
        save_json(folder/'traces/a.json',{'model_key':'qwen','run_number':2,
            'assertion_id':rows[0]['assertion_id'],'cache_fingerprint':rows[0]['fingerprint'],
            'status':'API_SUCCESS','mapped_verdict':'PASS'})
        with self.assertRaisesRegex(ValueError,'Step A outputs are missing'):
            inspect_outputs(self.root,self.out,MODELS,['original'])

    def test_independent_runs_can_genuinely_choose_same_aq(self):
        first = json.loads((self.run_folder('qwen', 1) / 'spec_check_results.json').read_text())
        path = self.run_folder('qwen', 2) / 'spec_check_results.json'
        second = json.loads(path.read_text())
        for a,b in zip(first,second):
            b['verdict'] = a['verdict']
        save_json(path, second)
        sp = path.with_name('summary.json')
        summary=json.loads(sp.read_text())
        summary.update(pass_count=1,final_aq=1,post_spec_qualification_yield_percent=20)
        save_json(sp,summary)
        report=inspect_outputs(self.root,self.out,MODELS,['original'])
        self.assertEqual(report['verified_slots'],12)
        self.assertEqual(report['unique_final_aq_contents'],11)


if __name__ == '__main__':
    unittest.main()
