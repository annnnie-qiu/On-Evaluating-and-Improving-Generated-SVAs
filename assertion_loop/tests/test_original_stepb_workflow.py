"""User workflow: unchanged Step A, original mutants, dev feedback, cumulative SVA.

AST parsing/standalone generation are real. Jasper and API are explicit test
doubles; their statuses are not formal proof evidence.
"""
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout,redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from looplib.common import read,save,EnvironmentSetupError,EnvironmentLock
from looplib.engine import run_unit
from looplib.faults import build_pool,pool_identity,score_rows,feedback,SYNTAX_PROTOCOL
from looplib.recovery import archive_step_b_results,recover_pending
import run
import test_existing_workflow as fixtures


class OriginalStepBTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExistingWorkflowTests.setUp(self)
        self.dataset=self.dataset.resolve()
        self.original_file=self.dataset/'assertions/toy_assertions.sv'
    prepare=fixtures.ExistingWorkflowTests.prepare
    passing_technical_stub=fixtures.ExistingWorkflowTests.passing_technical_stub

    def judge(self,*args):return {'verdict':'PASS','question':'','reason':'OFFLINE STUB','evidence_quote':''}

    def old_calibration_failure(self,completed=False):
        self.prepare(one=True)
        # Match the production CLI: input_inventory is audit-only, not setup.
        self.setup.pop('input_inventory',None)
        save(self.dataset/'gen_assertions/setup/dataset.json',self.setup)
        self.lock=EnvironmentLock(self.dataset,self.setup)
        self.config.pop('fault_qualification',None) # actual old implicit equivalence default
        revision=read(ROOT/'MANIFEST.json')['pre_b1_pool_compatible_revisions'][-1]
        pending=build_pool(self.dataset,self.unit,self.config,self.lock,plan_only=True)
        pending['identity']=pool_identity(self.unit,self.config,self.lock.identity,
            generator_hashes=revision['generator_files'],selection_protocol=revision.get('selection_protocol'))
        self.client=SimpleNamespace(call=self.judge,records=lambda:[],preflight=lambda *a:None,export=lambda:None)
        with patch('looplib.engine.implementation_digest',return_value=revision['implementation']), \
             patch('looplib.engine.qualify',side_effect=self.passing_technical_stub), \
             patch('looplib.engine.build_pool',side_effect=EnvironmentSetupError('EQ_CAL_BASE CEX')),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(EnvironmentSetupError,'EQ_CAL_BASE CEX'):
                run_unit(self.dataset,self.unit,'deepseek',1,self.config,self.client,pending,self.lock,self.directory,[1],False)
        self.checkpoint=self.directory/'rounds/round_01/step_a_attempts/attempt_01/attempt.json'
        self.before=self.checkpoint.read_bytes()
        self.experiment=self.directory.parents[3]
        save(self.experiment/'config_snapshot.json',self.config)
        if completed:
            save(self.directory/'rounds/round_01/round.json',{'old_denominator':True,'step_b_ran':True})
            save(self.directory/'rounds/round_02/step_a_attempts/attempt_01/attempt.json',{'later_budget':'must not leak'})
            save(self.directory/'budgets/budget_01/summary.json',{'old_denominator':True})

    def resume_cli(self):
        output=io.StringIO()
        with patch.object(run,'Client',return_value=self.client), \
             patch.object(self.client,'call',side_effect=AssertionError('No repeated GPT/API')), \
             patch('looplib.engine.qualify',side_effect=AssertionError('No repeated Step A')), \
             patch('looplib.equivalence.qualify_mutant',side_effect=AssertionError('No equivalence job')), \
             redirect_stdout(output),redirect_stderr(output):
            result=run.main(ablation=True,argv=[str(self.dataset),'--model','deepseek','--repeats','1','--budgets','1','--experiment','offline'])
        self.assertEqual(result,0,output.getvalue())
        self.assertEqual(self.before,self.checkpoint.read_bytes())
        self.assertIn('[RESUME_STEP_A]',output.getvalue())
        self.assertNotIn('[POOL_EQ]',output.getvalue())
        row=read(self.directory/'budgets/budget_01/summary.json')
        self.assertEqual(row['pool_protocol'],SYNTAX_PROTOCOL)
        return row

    def test_original_command_restores_legacy_snapshot_without_calibration_or_step_a_rerun(self):
        self.old_calibration_failure()
        self.resume_cli()

    def test_old_scores_and_later_budget_proposals_preserved_separately(self):
        self.old_calibration_failure(completed=True)
        self.resume_cli()
        history=Path(read(self.directory/'step_b_policy_transition.json')['history'])
        self.assertEqual(read(history/'rounds/round_01/round.json')['old_denominator'],True)
        self.assertTrue((history/'rounds/round_02').is_dir())
        self.assertFalse((self.directory/'rounds/round_02').exists())

    def test_step_b_archive_resumes_after_interrupted_move_without_touching_step_a(self):
        self.prepare(one=True)
        checkpoint=self.directory/'rounds/round_01/step_a_attempts/attempt_01/attempt.json'
        save(checkpoint,{'sentinel':'do not rewrite'})
        save(self.directory/'rounds/round_01/round.json',{'old':1})
        save(self.directory/'rounds/round_02/round.json',{'old':2})
        before=checkpoint.read_bytes()
        import shutil
        move=shutil.move;calls=[]
        def interrupt(*args):
            calls.append(args)
            if len(calls)==2:raise KeyboardInterrupt
            return move(*args)
        with patch('looplib.recovery.shutil.move',side_effect=interrupt),self.assertRaises(KeyboardInterrupt):
            archive_step_b_results(self.directory,'test denominator change')
        recover_pending(self.directory)
        self.assertEqual(before,checkpoint.read_bytes())
        history=Path(read(self.directory/'step_b_policy_transition.json')['history'])
        self.assertTrue((history/'rounds/round_01/round.json').is_file())
        self.assertTrue((history/'rounds/round_02/round.json').is_file())

    def test_loop_mutant_texts_match_original_standalone_driver_exactly(self):
        self.prepare(one=True)
        source='module toy(input logic clk,rst,req,output logic ack);\nalways @(posedge clk) if(rst) ack <= 0; else ack <= req + 1;\nassign ack = req ? rst : ack;\nendmodule\n'
        (self.dataset/'rtl/toy.sv').write_text(source)
        self.lock=EnvironmentLock(self.dataset,self.setup)
        self.config.update(backend='ast',fault_qualification='syntax',max_mutants_per_family=None)
        with redirect_stdout(io.StringIO()):pool=build_pool(self.dataset,self.unit,self.config,self.lock)
        config=self.dataset/'standalone.json'
        save(config,{'project_root':str(self.dataset),'rtl_files':['rtl/toy.sv'],'assertions':['a_good'],'include_dirs':[],'defines':[]})
        result=subprocess.run([sys.executable,str(ROOT/'vendor/faults/generate_mutants.py'),
            '--config',str(config),'--target','rtl/toy.sv','--output',str(self.dataset/'standalone'),
            '--backend','ast','--seed',str(self.config['pool_seed'])],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        original=read(self.dataset/'standalone/manifest.json')['mutants']
        self.assertEqual([(m['family'],m['sha256']) for m in pool['mutants']],
                         [(m['family'],m['sha256']) for m in original])
        self.assertEqual(len({m['sha256'] for m in pool['mutants']}),len(pool['mutants']))

    def test_proven_by_all_is_feedback_and_qualified_addition_is_in_same_cumulative_file(self):
        self.prepare(one=True)
        self.config['fault_qualification']='syntax'
        self.pool['protocol']=SYNTAX_PROTOCOL
        calls=[];observations=[]
        def call(model,purpose,messages,context):
            calls.append((model,purpose))
            if purpose=='generation':
                self.assertIn('dev_1',json.dumps(messages))
                return {'assertions':[{'expression':'!req |-> !ack','basis':'OFFLINE supplement'}]}
            return self.judge()
        def measure(dataset,unit,props,pool,partition,directory,config,lock):
            self.assertNotEqual(partition,'test')
            mutants=[m for m in pool['mutants'] if m['id'] in pool[partition+'_ids']]
            rows=[{'assertion_id':p['label'],'mutant_id':m['id'],
                   'status':'CEX' if len(props)>1 and i==1 else 'PROVEN'}
                  for i,p in enumerate(props) for m in mutants]
            result=score_rows(props,mutants,rows);result.update(partition=partition,status='MEASURED')
            observations.append((partition,len(props),result['detected_count']))
            return result
        with patch('looplib.engine.qualify',side_effect=self.passing_technical_stub), \
             patch('looplib.engine.measure',side_effect=measure),redirect_stdout(io.StringIO()):
            rows=run_unit(self.dataset,self.unit,'deepseek',1,self.config,SimpleNamespace(call=call,records=lambda:[]),
                          self.pool,self.lock,self.directory,[1,2],False)
        self.assertEqual([r['dev_detected'] for r in rows],[0,1])
        self.assertEqual([r['qualified_count'] for r in rows],[1,2])
        sva=(self.directory/'rounds/round_02/qualified_assertions.sv').read_text()
        self.assertIn('a_good: assert',sva)
        self.assertIn('!req |-> !ack',sva)
        self.assertEqual(sva.count('fixed_environment: assume'),1)
        self.assertEqual(len([e for e in self.events if e['kind']=='gate' and e['label']=='a_good']),3)
        self.assertEqual(len([c for c in calls if c[1]=='generation']),1)
        props=[{'label':'a'},{'label':'b'}];mutants=[{'id':'m'}]
        result=score_rows(props,mutants,[{'assertion_id':'a','mutant_id':'m','status':'PROVEN'},
                                      {'assertion_id':'b','mutant_id':'m','status':'CEX'}])
        self.assertEqual(result['detected_count'],1) # one proven assertion is not a missed mutant


if __name__=='__main__':unittest.main()
