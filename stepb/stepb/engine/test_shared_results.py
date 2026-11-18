import contextlib
import csv
import hashlib
import io
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from collect_jasper import collect, input_fingerprint, make_tcl
from legacy_reuse import seed_existing_module
from shared_results import Checker, environment, save_json, sha


class SharedResultsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'rtl').mkdir()
        (self.root / 'rtl/top.sv').write_text('module top; endmodule\n')
        (self.root / 'rtl/defs.svh').write_text('`define VERSION 1\n')
        self.mutants = []
        for i, family in enumerate(['F1', 'F2'], 1):
            path = self.root / f'{family}.sv'
            path.write_text(f'module top; wire mutant_{i}; endmodule\n')
            self.mutants.append({'id': family + '_001', 'family': family,
                'replace': 'rtl/top.sv', 'file': str(path)})
        self.counter = self.root / 'calls.jsonl'
        self.fake = self.root / 'fake_jg'
        self.fake.write_text('''#!/usr/bin/env python3
import sys,re,json,pathlib
p=pathlib.Path(sys.argv[-1])
tcl=p.read_text()
a=pathlib.Path(re.search(r'set FH_ASSERTION \\{([^}]+)\\}',tcl)[1])
names=re.findall(r'([A-Za-z_]\\w*)\\s*:\\s*assert(?:\\s+property)?',a.read_text())
with pathlib.Path(''' + repr(str(self.counter)) + ''').open('a') as f:f.write(json.dumps({'tcl':str(p),'assertions':names})+'\\n')
for name in names:print('FH_PROPERTY\\t'+name+'\\t'+('unknown' if name=='u' else 'cex' if name in ['a','c'] else 'proven'))
print('FH_DONE')
''')
        self.fake.chmod(0o755)
        self.cache = self.root / 'shared'

    def source(self, names, helper='logic x;'):
        bodies = {'a': 'x', 'b': '!x', 'c': 'x == 1', 'u': 'x == 0'}
        return 'module props;\n' + helper + '\n' + '\n'.join(
            f'{name}: assert property ({bodies[name]});' if name in names else f'/* AQ removed {name} */'
            for name in bodies) + '\nendmodule\n'

    def setup_run(self, folder, names, source=None):
        folder.mkdir(parents=True, exist_ok=True)
        aq = folder / 'props.sv'
        aq.write_text(source or self.source(names))
        config = {'project_root': str(self.root), 'rtl_files': ['rtl/top.sv'], 'top': 'top',
                  'assertion_file': str(aq), 'assertions': names, 'clock_mode': 'none',
                  'include_dirs': ['rtl'], 'jasper_executable': str(self.fake)}
        cp, mp = folder / 'config.json', folder / 'manifest.json'
        save_json(cp, config)
        save_json(mp, {'assertions': names, 'mutants': self.mutants})
        return cp, mp

    def run_collect(self, folder, names, **kwargs):
        cp, mp = self.setup_run(folder, names)
        with contextlib.redirect_stdout(io.StringIO()):
            result = collect(cp, mp, folder / 'jasper', cache_dir=self.cache, **kwargs)
        return result

    def calls(self):
        return [json.loads(line) for line in self.counter.read_text().splitlines()] if self.counter.exists() else []

    def test_identical_models_and_trials_reuse_all_cells_and_no_golden(self):
        first = self.run_collect(self.root / 'qwen/run_01', ['a', 'b'])
        second = self.run_collect(self.root / 'gpt/run_03', ['a', 'b'], reuse_only=True)
        self.assertEqual(first['jasper_sessions'], 2)
        self.assertEqual(second['jasper_sessions'], 0)
        self.assertEqual(second['cached_cells'], 4)
        self.assertEqual(len(self.calls()), 2)
        self.assertFalse(any('golden' in row['tcl'] for row in self.calls()))
        self.assertFalse((self.root / 'qwen/run_01/jasper/runs/golden').exists())
        a = (self.root / 'qwen/run_01/jasper/matrix.csv').read_text()
        b = (self.root / 'gpt/run_03/jasper/matrix.csv').read_text()
        self.assertEqual(a, b)

    def test_only_new_assertion_is_measured_for_overlapping_aq(self):
        self.run_collect(self.root / 'first', ['a', 'b'])
        stats = self.run_collect(self.root / 'second', ['a', 'c'])
        self.assertEqual(stats['jasper_sessions'], 2)
        self.assertEqual(stats['cached_cells'], 2)
        self.assertEqual([r['assertions'] for r in self.calls()], [['a', 'b'], ['a', 'b'], ['c'], ['c']])
        with (self.root / 'second/jasper/matrix.csv').open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertEqual({r['assertion_id'] for r in rows}, {'a', 'c'})

    def test_interrupted_collection_keeps_completed_mutant_cells(self):
        import collect_jasper
        original = collect_jasper.run_logged
        calls = []
        def interrupted(command, **kwargs):
            calls.append(kwargs['label'])
            if len(calls) == 2:
                raise KeyboardInterrupt
            return original(command, **kwargs)
        with patch.object(collect_jasper, 'run_logged', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_collect(self.root / 'interrupted', ['a', 'b'])
        self.assertEqual(len(self.calls()), 1)
        stats = self.run_collect(self.root / 'interrupted', ['a', 'b'])
        self.assertEqual(stats['jasper_sessions'], 1)
        self.assertEqual(stats['cached_cells'], 2)
        self.assertEqual(len(self.calls()), 2)

    def test_renamed_identical_assertion_reuses_formula_result(self):
        self.run_collect(self.root/'first',['a'])
        cp,mp=self.setup_run(self.root/'renamed',['renamed_a'],self.source(['a']).replace('a: assert','renamed_a: assert'))
        with contextlib.redirect_stdout(io.StringIO()):
            result=collect(cp,mp,self.root/'renamed/jasper',cache_dir=self.cache,reuse_only=True)
        self.assertEqual(result['jasper_sessions'],0)

    def test_duplicate_formulas_with_different_labels_are_proved_once(self):
        source='module props; logic x;\na: assert property (x);\nother: assert property (x);\nendmodule'
        cp,mp=self.setup_run(self.root/'duplicates',['a','other'],source)
        with contextlib.redirect_stdout(io.StringIO()):
            result=collect(cp,mp,self.root/'duplicates/jasper',cache_dir=self.cache)
        self.assertEqual(result['measured_cells'],2)
        self.assertEqual([r['assertions'] for r in self.calls()],[['a'],['a']])
        with (self.root/'duplicates/jasper/matrix.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),4)
        self.assertTrue(all(r['status']=='CEX' for r in rows))

    def test_same_property_name_with_changed_body_is_not_reused(self):
        self.run_collect(self.root / 'first', ['a'])
        cp, mp = self.setup_run(self.root / 'changed', ['a'], self.source(['a']).replace('property (x)', 'property (!x)'))
        with contextlib.redirect_stdout(io.StringIO()):
            stats = collect(cp, mp, self.root / 'changed/jasper', cache_dir=self.cache)
        self.assertEqual(stats['jasper_sessions'], 2)

    def test_different_formal_environment_or_helper_invalidates_cache(self):
        self.run_collect(self.root / 'first', ['a'])
        cp, mp = self.setup_run(self.root / 'helper', ['a'], self.source(['a'], 'logic x; assume property (x);'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect(cp, mp, self.root / 'helper/jasper', cache_dir=self.cache)['jasper_sessions'], 2)
        cp, mp = self.setup_run(self.root / 'reset', ['a'])
        cfg = json.loads(cp.read_text())
        cfg.update(clock_mode='clocked', clock='clk', reset_expression='reset')
        save_json(cp, cfg)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect(cp, mp, self.root / 'reset/jasper', cache_dir=self.cache)['jasper_sessions'], 2)
        (self.root / 'rtl/defs.svh').write_text('`define VERSION 2\n')
        stats = self.run_collect(self.root / 'header', ['a'])
        self.assertEqual(stats['jasper_sessions'], 2)

    def test_unknown_is_preserved_and_only_retried_when_requested(self):
        self.run_collect(self.root / 'one', ['a', 'u'])
        self.assertEqual(self.run_collect(self.root / 'two', ['a', 'u'])['jasper_sessions'], 0)
        stats = self.run_collect(self.root / 'three', ['a', 'u'], retry_unknown=True)
        self.assertEqual(stats['measured_cells'], 2)
        self.assertEqual([c['assertions'] for c in self.calls()][-2:], [['u'], ['u']])

    def test_reuse_only_cannot_launch_a_tool_for_missing_cells(self):
        with self.assertRaisesRegex(RuntimeError, 'no cached results'):
            self.run_collect(self.root / 'missing', ['a'], reuse_only=True)
        self.assertEqual(self.calls(), [])

    def test_simple_immediate_assertions_reuse_but_effectful_ones_do_not_split(self):
        source = "module props; logic x; always_comb begin\na: assert (x);\nb: assert (!x);\nend endmodule"
        cp, mp = self.setup_run(self.root / 'immediate_first', ['a','b'], source)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect(cp,mp,self.root / 'immediate_first/jasper',cache_dir=self.cache)['jasper_sessions'],2)
        cp, mp = self.setup_run(self.root / 'immediate_next', ['a'], source.replace('b: assert (!x);','/* removed */'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(collect(cp,mp,self.root / 'immediate_next/jasper',cache_dir=self.cache,reuse_only=True)['jasper_sessions'],0)
        effectful=Checker('module p; logic x; always_comb begin\na: assert (++x);\nend endmodule',['a'],'p.sv')
        self.assertFalse(effectful.partial)

    def test_assertion_scope_is_part_of_identity(self):
        one = 'module p; generate if (A) begin : left\na: assert property (x);\nend else begin : right\nend endgenerate endmodule'
        two = 'module p; generate if (A) begin : left\nend else begin : right\na: assert property (x);\nend endgenerate endmodule'
        a, b = Checker(one, ['a'], 'p.sv'), Checker(two, ['a'], 'p.sv')
        self.assertEqual(a.context_sha256, b.context_sha256)
        self.assertNotEqual(a.keys['a'], b.keys['a'])

    def test_old_pool_stays_reusable_when_later_step_a_aq_changes(self):
        from run_batch import validated_pool_can_resume
        module=self.root/'module'
        old=module/'original/qwen/run_01'
        cp,mp=self.setup_run(old/'input',['a','b'])
        cp=cp.rename(cp.with_name('run_config.json'))
        mp=mp.rename(old/'manifest.json')
        cfg=json.loads(cp.read_text());aq=Path(cfg['assertion_file'])
        enriched=[dict(m,syntax_validated=True,sha256=sha(Path(m['file']).read_bytes())) for m in self.mutants]
        manifest={'assertions':['a','b'],'mutants':enriched}
        save_json(mp,manifest)
        validation=module/'fault_pool/validation'
        save_json(validation/'validated_manifest.json',manifest)
        (validation/'validation_report.csv').write_text('id,status\nF1_001,PASS\nF2_001,PASS\n')
        for job,rtl in [('golden',self.root/'rtl/top.sv'),*((m['id'],Path(m['file'])) for m in enriched)]:
            tcl=make_tcl(cfg,[rtl],aq,[],[self.root/'rtl'],[])
            save_json(old/'jasper/runs'/job/'result.json',{
                'input_sha256':input_fingerprint(tcl,cfg,[rtl,aq],[self.root/'rtl']),
                'properties':{'a':'PROVEN' if job=='golden' else 'CEX','b':'PROVEN'}})
        with contextlib.redirect_stdout(io.StringIO()):
            seed_existing_module(module,self.cache)
        self.assertTrue((validation/'pool_context.json').exists())
        aq.write_text(self.source(['a']))
        cfg['assertions']=['a'];save_json(cp,cfg)
        record={'config':str(cp),'assertions':['a']}
        self.assertEqual(validated_pool_can_resume(validation/'validated_manifest.json',
            [({},record,old)]),manifest)

    def test_legacy_results_are_rehashed_before_shared_reuse(self):
        module = self.root / 'module'
        old = module / 'original/qwen/run_01'
        cp, mp = self.setup_run(old / 'input', ['a', 'b'])
        config_path = cp.with_name('run_config.json')
        cp.rename(config_path)
        manifest_path = old / 'manifest.json'
        mp.rename(manifest_path)
        cfg = json.loads(config_path.read_text())
        aq = Path(cfg['assertion_file'])
        for item in self.mutants:
            folder = old / 'jasper/runs' / item['id']
            rtl = [Path(item['file'])]
            tcl = make_tcl(cfg, rtl, aq, [], [self.root / 'rtl'], [])
            save_json(folder / 'result.json', {'input_sha256': input_fingerprint(tcl, cfg,
                [*rtl, aq], [self.root / 'rtl']), 'properties': {'a': 'CEX', 'b': 'PROVEN'}})
        original = (old / 'jasper/runs/F1_001/result.json').read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            report = seed_existing_module(module, self.cache)
        self.assertEqual(report['admitted_jobs'], 2)
        self.assertEqual(self.run_collect(self.root / 'new_model', ['a'], reuse_only=True)['jasper_sessions'], 0)
        self.assertEqual((old / 'jasper/runs/F1_001/result.json').read_bytes(), original)
        (self.root / 'rtl/top.sv').write_text('module top; wire changed; endmodule\n')
        # Old include-dir fingerprint includes the original design as well.
        with contextlib.redirect_stdout(io.StringIO()):
            report = seed_existing_module(module, self.root / 'different_cache')
        self.assertEqual(report['admitted_jobs'], 0)


if __name__ == '__main__':
    unittest.main()
