"""Regression: equivalence must never launch in explicit original-flow mode.

Subprocesses execute production Tcl through an explicitly fake Jasper. These
tests establish branching, resume and measurement semantics, not solver speed.
"""
import copy
import io
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from looplib.common import read,save,EnvironmentSetupError,digest
from looplib.engine import run_unit
from looplib.faults import build_pool,pool_protocol,SYNTAX_PROTOCOL,feedback
from looplib.reporting import report
import run
import test_existing_workflow as fixtures


class OperationalProtocolTests(unittest.TestCase):
    setUp=fixtures.ExistingWorkflowTests.setUp
    def prepare(self,**kwargs):
        (self.dataset/'rtl/toy.sv').write_text("module toy(input logic clk,rst,req,output logic ack);\nalways_comb begin\n if(req) ack=1'b1;\n else ack=1'b0;\nend\nendmodule\n")
        fixtures.ExistingWorkflowTests.prepare(self,**kwargs)
    passing_technical_stub=fixtures.ExistingWorkflowTests.passing_technical_stub

    def judge(self,*args):
        return {'verdict':'PASS','question':'','reason':'OFFLINE STUB','evidence_quote':''}

    def test_syntax_pool_uses_real_syntax_tcl_and_never_calls_equivalence(self):
        self.prepare(one=True)
        self.config['fault_qualification']='syntax'
        original={p:p.read_bytes() for folder in ('rtl','bind','spec','assertions') for p in (self.dataset/folder).rglob('*') if p.is_file()}
        with patch('looplib.equivalence.qualify_mutant',side_effect=AssertionError('No equivalence jobs in original-flow mode')), redirect_stdout(io.StringIO()):
            pool=build_pool(self.dataset,self.unit,self.config,self.lock)
        self.assertTrue(pool['mutants'])
        self.assertEqual(pool['protocol'],SYNTAX_PROTOCOL)
        self.assertTrue(all(m['status']=='SYNTAX_VALID' for m in pool['mutants']))
        self.assertFalse((Path(pool['directory'])/'equivalence').exists())
        self.assertTrue(list((Path(pool['directory'])/'validation').glob('*/attempt_*/run.log')))
        self.assertEqual(pool['qualification_counts']['DIFFERENT'],0)
        self.assertTrue(all(p.read_bytes()==s for p,s in original.items()))
        self.assertIn('equivalent',pool['validity'])
        with patch('looplib.faults.JasperAttempt',side_effect=AssertionError('Completed pool must be reused')):
            self.assertEqual(build_pool(self.dataset,self.unit,self.config,self.lock),pool)

    def test_strict_mode_still_requires_calibration_and_identity_is_distinct(self):
        self.prepare(one=True)
        strict=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        syntax=build_pool(self.dataset,self.unit,{**self.config,'fault_qualification':'syntax'},self.lock,plan_only=True)
        self.assertNotEqual(strict['identity'],syntax['identity'])
        self.assertEqual(pool_protocol(self.config),'observable_equivalence_v1')
        with patch('looplib.equivalence.qualify_mutant',side_effect=RuntimeError('STRICT_CALIBRATION_ENTERED')),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,'STRICT_CALIBRATION_ENTERED'):
                build_pool(self.dataset,self.unit,self.config,self.lock)

    def blocked_baseline(self):
        self.prepare(one=True)
        self.previous=read(ROOT/'MANIFEST.json')['resume_compatible_implementations'][-1]
        pending=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        self.client=SimpleNamespace(call=self.judge,records=lambda:[])
        with patch('looplib.engine.implementation_digest',return_value=self.previous), \
             patch('looplib.engine.qualify',side_effect=self.passing_technical_stub), \
             patch('looplib.engine.build_pool',side_effect=EnvironmentSetupError('OFFLINE golden stalled')),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(EnvironmentSetupError,'OFFLINE golden stalled'):
                run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
        # Reproduce legacy recipe schema: delivered v16.3 did not store the new
        # current-input hash. The hash itself uses the exact previous revision.
        recipe=read(self.directory/'recipe.json');recipe.pop('current_inputs_sha256',None);recipe.pop('pool_protocol',None)
        save(self.directory/'recipe.json',recipe)
        self.checkpoint=self.directory/'rounds/round_01/step_a_attempts/attempt_01/attempt.json'
        self.before=self.checkpoint.read_bytes()

    def test_pre_b1_switch_reuses_passed_step_a_and_real_step_b_collector(self):
        self.blocked_baseline()
        self.config['fault_qualification']='syntax'
        pending=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        output=io.StringIO()
        with patch.object(self.client,'call',side_effect=AssertionError('No LLM rerun')), \
             patch('looplib.engine.qualify',side_effect=AssertionError('No Step A rerun')), \
             patch('looplib.equivalence.qualify_mutant',side_effect=AssertionError('No equivalence jobs')),redirect_stdout(output):
            rows=run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
            # Restart must also keep the alias recipe and every completed B1.
            with patch('looplib.engine.measure',side_effect=AssertionError('Do not remeasure completed budget')):
                again=run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
        self.assertEqual(rows,again)
        self.assertEqual(self.checkpoint.read_bytes(),self.before)
        self.assertIn('[RESUME_STEP_A]',output.getvalue())
        self.assertEqual(rows[0]['pool_protocol'],SYNTAX_PROTOCOL)
        self.assertEqual(rows[0]['step_a_repairs_used'],0)
        self.assertFalse((self.directory/'_history').exists())
        self.assertTrue((self.directory/'rounds/round_01/step_b/jasper/matrix.csv').is_file())
        self.assertFalse((self.directory/'budgets/budget_01/step_b_test').exists())
        self.assertEqual(read(self.directory/'pool_protocol_transition.json')['step_b_measurements_before_switch'],0)
        experiment=self.directory.parents[3]
        report(experiment)
        self.assertIn(SYNTAX_PROTOCOL,(experiment/'effectiveness.csv').read_text())
        self.assertIn('Syntax-valid denominator',(experiment/'comparison.html').read_text())

    def test_engine_cannot_switch_after_measured_step_b(self):
        self.blocked_baseline()
        save(self.directory/'rounds/round_01/round.json',{'step_b_ran':True})
        self.config['fault_qualification']='syntax'
        pending=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        with redirect_stdout(io.StringIO()),self.assertRaisesRegex(EnvironmentSetupError,'Cannot switch'):
            run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
        self.assertEqual(self.checkpoint.read_bytes(),self.before)
        self.assertFalse((self.directory/'_history').exists())

    def test_cli_rejects_changed_denominator_before_snapshot_or_api(self):
        self.prepare(one=True)
        experiment=self.directory.parents[3]
        save(experiment/'config_snapshot.json',self.config)
        save(self.directory/'rounds/round_01/round.json',{'step_b_ran':True})
        before=(experiment/'config_snapshot.json').read_bytes()
        output=io.StringIO()
        with patch.object(run,'Client',side_effect=AssertionError('No API')),redirect_stdout(output),redirect_stderr(output):
            code=run.main(ablation=True,argv=[str(self.dataset),'--experiment','offline','--fault-qualification','syntax','--budgets','1'])
        self.assertEqual(code,1)
        self.assertIn('different fault denominator',output.getvalue())
        self.assertEqual((experiment/'config_snapshot.json').read_bytes(),before)

    def test_feedback_does_not_claim_syntax_valid_misses_are_proven_gaps(self):
        self.pool['protocol']=SYNTAX_PROTOCOL
        msg=feedback(self.pool,{'undetected_ids':['dev_1'],'unknown_ids':[],'unknown_only_count':0,'detection_lower_percent':0})
        self.assertIn('equivalent or unobservable',msg['instruction'])
        self.assertNotIn('independently validated',msg['instruction'])

    def test_success_archives_prior_calibration_failure_instead_of_reporting_it_as_active(self):
        self.prepare(one=True)
        experiment=self.directory.parents[3]
        save(experiment/'config_snapshot.json',self.config)
        error=self.directory/'error.json'
        save(error,{'status':'FAILED','error':'Previous golden calibration failed'})
        before=error.read_bytes()
        client=SimpleNamespace(preflight=lambda *a:None,export=lambda:None)
        with patch.object(run,'Client',return_value=client),patch.object(run,'run_unit',return_value=[]),redirect_stdout(io.StringIO()):
            code=run.main(ablation=True,argv=[str(self.dataset),'--experiment','offline','--model','deepseek','--budgets','1','--fault-qualification','syntax'])
        self.assertEqual(code,0)
        self.assertFalse(error.exists())
        archives=list(error.parent.glob('error.resolved_*.json'))
        self.assertEqual(len(archives),1)
        self.assertEqual(archives[0].read_bytes(),before)

    def test_plan_records_protocol_and_all_ablation_prefixes(self):
        self.prepare(one=True)
        with patch.object(run,'Client',side_effect=AssertionError('No API')),redirect_stdout(io.StringIO()):
            code=run.main(ablation=True,argv=[str(self.dataset),'--plan','--experiment','syntax_plan','--fault-qualification','syntax','--budgets','1,3,5,10,15,20'])
        self.assertEqual(code,0)
        plan=read(self.dataset/'gen_assertions/ablation/syntax_plan/plan.json')
        self.assertEqual(plan['pool_protocol'],SYNTAX_PROTOCOL)
        self.assertEqual(plan['max_step_b_rounds'],20)

    def test_reports_reject_mixed_protocols_instead_of_averaging(self):
        self.prepare(one=True)
        experiment=self.directory.parents[3]
        for i,protocol in enumerate(('observable_equivalence_v1',SYNTAX_PROTOCOL),1):
            save(experiment/f'x{i}/budgets/budget_01/summary.json',{'budget_type':'step_b_rounds','pool_protocol':protocol})
        with self.assertRaisesRegex(ValueError,'Different fault eligibility'):
            report(experiment)


if __name__=='__main__':unittest.main()
