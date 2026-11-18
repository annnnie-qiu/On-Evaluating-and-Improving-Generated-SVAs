"""Exercise RQ3 AST wiring and the reported pre-B1 upgrade.

Parsing is real pyslang. Jasper runs use the explicitly fake Tcl command shim;
model replies are stubs. These tests establish flow and cache preservation.
"""
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from looplib.common import EnvironmentLock,EnvironmentSetupError,read,save
from looplib.engine import run_unit
from looplib.faults import build_pool,pool_identity
import test_existing_workflow as fixtures


class ASTLoopIntegrationTests(unittest.TestCase):
    setUp=fixtures.ExistingWorkflowTests.setUp
    passing_technical_stub=fixtures.ExistingWorkflowTests.passing_technical_stub

    def prepare(self):
        fixtures.ExistingWorkflowTests.prepare(self,one=True)
        headers=self.dataset/'rtl/headers';headers.mkdir()
        (headers/'defs.vh').write_text("`define CLEAR 1'b0\n")
        (self.dataset/'rtl/toy.sv').write_text(
            '`include "defs.vh"\nmodule toy(input logic clk,rst,req,output logic ack);\n'
            '`ifdef ENABLE_UPDATE\nalways @(posedge clk) if(rst) ack <= `CLEAR; else ack <= req + ack;\n'
            '`else\nassign ack = req;\n`endif\nendmodule\n')
        self.unit['include_dirs']=['rtl/headers']
        self.unit['defines']=['ENABLE_UPDATE=1']
        save(self.dataset/'gen_assertions/setup/dataset.json',self.setup)
        self.lock=EnvironmentLock(self.dataset,self.setup)
        self.config.update(backend='ast',fault_qualification='syntax')

    def judge(self,*args):
        return {'verdict':'PASS','question':'','reason':'OFFLINE STUB','evidence_quote':''}

    def test_real_pool_entry_passes_include_dirs_and_defines(self):
        self.prepare()
        before={p:p.read_bytes() for folder in ('rtl','assertions','bind','spec')
                for p in (self.dataset/folder).rglob('*') if p.is_file()}
        with redirect_stdout(io.StringIO()):
            pool=build_pool(self.dataset,self.unit,self.config,self.lock)
        inventory=read(Path(pool['directory'])/'candidate_inventory.json')
        self.assertTrue(any(c['operator']=='state_update_operator' for c in inventory['candidates']))
        self.assertTrue(pool['mutants'])
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        with patch('looplib.faults.JasperAttempt',side_effect=AssertionError('Do not rerun a completed pool')):
            self.assertEqual(build_pool(self.dataset,self.unit,self.config,self.lock),pool)

    def test_real_parse_error_names_target_and_header_and_saves_diagnostic(self):
        self.prepare()
        (self.dataset/'rtl/toy.sv').write_text('`include "absent_header.vh"\nmodule toy; endmodule\n')
        self.lock=EnvironmentLock(self.dataset,self.setup)
        pending=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        with redirect_stdout(io.StringIO()),self.assertRaisesRegex(EnvironmentSetupError,'(?s)toy.sv.*absent_header'):
            build_pool(self.dataset,self.unit,self.config,self.lock)
        self.assertIn('absent_header',(Path(pending['directory'])/'ast_parse_error.txt').read_text())

    def old_failed_run(self):
        self.prepare()
        revision=read(ROOT/'MANIFEST.json')['pre_b1_pool_compatible_revisions'][0]
        pending=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        old_pool={**pending,'identity':pool_identity(self.unit,self.config,self.lock.identity,
                                                  generator_hashes=revision['generator_files'],
                                                  selection_protocol=revision.get('selection_protocol'))}
        self.client=SimpleNamespace(call=self.judge,records=lambda:[])
        with patch('looplib.engine.implementation_digest',return_value=revision['implementation']), \
             patch('looplib.engine.qualify',side_effect=self.passing_technical_stub), \
             patch('looplib.engine.build_pool',side_effect=EnvironmentSetupError('OLD AST PARSE FAILED')), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(EnvironmentSetupError,'OLD AST PARSE FAILED'):
                run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,old_pool,self.lock,self.directory,[1],False)
        self.checkpoint=self.directory/'rounds/round_01/step_a_attempts/attempt_01/attempt.json'
        self.checkpoint_before=self.checkpoint.read_bytes()
        return pending

    def test_upgrade_reuses_passed_step_a_then_real_pool_and_collector_then_restart(self):
        pending=self.old_failed_run()
        output=io.StringIO()
        with patch.object(self.client,'call',side_effect=AssertionError('No repeated GPT/API')), \
             patch('looplib.engine.qualify',side_effect=AssertionError('No repeated Step A')), \
             redirect_stdout(output):
            rows=run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
            with patch('looplib.engine.measure',side_effect=AssertionError('No repeated completed Step B')), \
                 patch('looplib.faults.JasperAttempt',side_effect=AssertionError('No repeated pool validation')):
                again=run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
        self.assertEqual(rows,again)
        self.assertEqual(self.checkpoint.read_bytes(),self.checkpoint_before)
        self.assertFalse((self.directory/'_history').exists())
        self.assertIn('[RESUME_STEP_A]',output.getvalue())
        self.assertIn('checkpoint=resume',output.getvalue())
        self.assertTrue((self.directory/'rounds/round_01/step_b/jasper/matrix.csv').is_file())
        self.assertEqual(rows[0]['step_b_rounds_completed'],1)

    def test_changed_config_does_not_use_parser_only_resume(self):
        pending=self.old_failed_run()
        self.config['proof_timeout_seconds']+=1
        pending=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        with patch('looplib.engine.prepare_recovery',side_effect=RuntimeError('REVALIDATION_REQUIRED')), \
             redirect_stdout(io.StringIO()),self.assertRaisesRegex(RuntimeError,'REVALIDATION_REQUIRED'):
            run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
        self.assertFalse((self.directory/'pool_parser_transition.json').exists())

    def test_existing_step_b_is_archived_without_importing_old_pool_results(self):
        pending=self.old_failed_run()
        save(self.directory/'rounds/round_01/round.json',{'step_b_ran':True})
        with patch.object(self.client,'call',side_effect=AssertionError('No repeated GPT')), \
             patch('looplib.engine.qualify',side_effect=AssertionError('No repeated Step A')),redirect_stdout(io.StringIO()):
            rows=run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
        archive=Path(read(self.directory/'step_b_policy_transition.json')['history'])
        self.assertEqual(read(archive/'rounds/round_01/round.json'),{'step_b_ran':True})
        self.assertEqual(self.checkpoint.read_bytes(),self.checkpoint_before)
        self.assertEqual(rows[0]['step_b_rounds_completed'],1)


if __name__=='__main__':unittest.main()
