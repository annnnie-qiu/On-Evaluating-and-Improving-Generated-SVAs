"""State-relation calibration: real SV compilation and conservative gates."""
import io
import itertools
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from looplib.common import EnvironmentLock
from looplib.equivalence import (miter_source,classify_calibration,qualify_mutant,
                                 UnsupportedEquivalence,CALIBRATION_METHOD)
import test_jasper_workspace as workspace

UNIT={'top':'toy','clock_mode':'clocked','clock':'clk','reset_expression':'rst'}
RTL='''module toy(input logic clk,rst,d,output logic q,output logic valid);
logic control;
always_ff @(posedge clk) if(rst) begin control<=0;q<=0;valid<=0;end
else begin control<=d;q<=control^d;valid<=control;end
endmodule'''


class CalibrationCompilerTests(unittest.TestCase):
    def test_proved_base_step_and_conditional_outputs_have_no_new_assumptions(self):
        direct,old=miter_source(RTL,RTL,'',UNIT)
        optimized,info=miter_source(RTL,RTL,'',UNIT,calibration=True)
        self.assertEqual(info['calibration_method'],CALIBRATION_METHOD)
        self.assertEqual(info['state_relation_registers'],['control','q','valid'])
        self.assertEqual(set(info['calibration_lemmas']),{'EQ_CAL_BASE','EQ_CAL_STEP'})
        self.assertIn('EQ_CAL_STATE_EQUAL |-> (ref_q == mut_q)',optimized)
        self.assertIn('EQ_CAL_STEP: assert property (@(posedge clk) (EQ_CAL_STATE_EQUAL |=> EQ_CAL_STATE_EQUAL));',optimized)
        self.assertIn("logic EQ_CAL_SEEN = 1'b0",optimized)
        self.assertIn("if (rst) EQ_CAL_SEEN <= 1'b0; else",optimized)
        self.assertIn('!EQ_CAL_SEEN |-> EQ_CAL_STATE_EQUAL',optimized)
        self.assertEqual([s.strip() for s in direct.splitlines() if 'assume' in s],
                         [s.strip() for s in optimized.splitlines() if 'assume' in s])
        self.assertNotIn('EQ_CAL_STATE_EQUAL',direct)
        self.assertNotIn('calibration_lemmas',old)

    def test_without_reset_history_initializes_and_step_does_not_cancel(self):
        source,info=miter_source(RTL,RTL,'',{**UNIT,'reset_expression':''},calibration=True)
        self.assertNotIn('disable iff',source)
        self.assertIn("always @(posedge clk) EQ_CAL_SEEN <= 1'b1;",source)
        self.assertEqual(info['conditional_outputs'],True)

    def test_empty_state_combinational_fallback_is_direct(self):
        rtl='module toy(input d,output q);assign q=~d;endmodule'
        source,info=miter_source(rtl,rtl,'',{'top':'toy','clock_mode':'none'},calibration=True)
        self.assertEqual(info['calibration_method'],'direct_output_comparison')
        self.assertEqual(info['calibration_lemmas'],{})
        self.assertNotIn('EQ_CAL_SEEN',source)
        self.assertIn('assert (ref_q == mut_q)',source)

    def test_explicit_initializers_are_checked_in_base_not_assumed_equal(self):
        rtl='module toy(input clk,d,output logic q=1);always @(posedge clk) q<=d;endmodule'
        source,info=miter_source(rtl,rtl,'',{**UNIT,'reset_expression':''},calibration=True)
        self.assertEqual(info['coupled_initial_registers'],[])
        self.assertIn('EQ_CAL_BASE:',source)
        self.assertIn('(ref_dut.q == mut_dut.q)',source)
        self.assertNotIn('assume',source)

    def test_different_sources_cannot_enter_self_calibration(self):
        with self.assertRaisesRegex(UnsupportedEquivalence,'identical'):
            miter_source(RTL,RTL.replace('control^d','control&d'),'',UNIT,calibration=True)

    def test_control_registers_in_relation_prevent_incomplete_output_only_invariant(self):
        source,info=miter_source(RTL,RTL,'',UNIT,calibration=True)
        relation=next(line for line in source.splitlines() if line.startswith('wire EQ_CAL_STATE_EQUAL'))
        for state in ('control','q','valid'):
            self.assertIn(f'ref_dut.{state} == mut_dut.{state}',relation)
        # q equality alone is not inductive when hidden control differs.
        def transition(s,d,rst):return (0,0,0) if rst else (d,s[0]^d,s[0])
        self.assertNotEqual(transition((0,0,0),0,0)[1],transition((1,0,0),0,0)[1])

    def test_induction_obligations_reject_initial_transition_and_output_mutations(self):
        states=list(itertools.product((0,1),repeat=3))
        inputs=list(itertools.product((0,1),repeat=2))
        def reference(s,inp):
            d,rst=inp
            return (0,0,0) if rst else (d,s[0]^d,s[0])
        def faulty(s,inp):
            d,rst=inp
            return (0,0,0) if rst else (1-d,s[0]^d,s[0])
        output=lambda s:(s[1],s[2])
        def obligations(mutant,initial=(0,0,0),observe=output):
            base=initial==(0,0,0)
            step=all(reference(s,i)==mutant(s,i) for s,i in itertools.product(states,inputs))
            outputs=all(output(s)==observe(s) for s in states)
            return base,step,outputs
        self.assertEqual(obligations(reference),(True,True,True))
        self.assertEqual(obligations(reference,initial=(1,0,0)),(False,True,True))
        self.assertEqual(obligations(faulty),(True,False,True))
        self.assertEqual(obligations(reference,observe=lambda s:(1-s[1],s[2])),(True,True,False))


class CalibrationGateTests(unittest.TestCase):
    def setUp(self):
        self.info={'outputs':{'EQ_OUTPUT_0001':'q'},
                   'calibration_lemmas':{'EQ_CAL_BASE':'base','EQ_CAL_STEP':'step'}}
        self.complete={'EQ_OUTPUT_0001':'PROVEN','EQ_CAL_BASE':'PROVEN',
                       'EQ_CAL_STEP':'PROVEN','EQ_ENV_WITNESS':'CEX'}

    def test_all_obligations_required(self):
        self.assertEqual(classify_calibration(self.complete,self.info),('EQUIVALENT',[]))
        for label in self.complete:
            for status in ('UNKNOWN','BOUNDED_PROVEN','MISSING','CEX' if label!='EQ_ENV_WITNESS' else 'PROVEN'):
                with self.subTest(label=label,status=status):
                    statuses={**self.complete,label:status}
                    self.assertEqual(classify_calibration(statuses,self.info),('UNRESOLVED',[]))
            statuses=dict(self.complete);statuses.pop(label)
            self.assertEqual(classify_calibration(statuses,self.info),('UNRESOLVED',[]))

    def test_proven_conditional_outputs_without_proven_state_relation_never_pass(self):
        statuses={'EQ_OUTPUT_0001':'PROVEN','EQ_ENV_WITNESS':'CEX'}
        self.assertEqual(classify_calibration(statuses,self.info)[0],'UNRESOLVED')


class CalibrationExecutionTests(unittest.TestCase):
    setUp=workspace.PoolRetryTests.setUp
    prepare=workspace.PoolRetryTests.prepare

    def test_production_tcl_selects_every_obligation_and_missing_step_blocks(self):
        (self.dataset/'rtl/toy.sv').write_text('module toy(input clk,rst,req,output logic ack);always_ff @(posedge clk) if(rst) ack<=0;else ack<=req;endmodule')
        self.lock=EnvironmentLock(self.dataset,self.setup)
        directory=self.dataset/'gen_assertions/golden_calibration'
        args=(self.dataset,self.unit,{'id':'golden_calibration','sha256':self.lock.identity},self.config,self.lock,directory)
        from looplib.calibration_cut import UnsupportedCut
        with patch('looplib.calibration_cut.flat_transition',side_effect=UnsupportedCut('test sequential fallback')),redirect_stdout(io.StringIO()):
            result=qualify_mutant(*args,calibration=True)
        self.assertEqual(result['status'],'EQUIVALENT')
        self.assertEqual(result['proof_statuses']['EQ_CAL_BASE'],'PROVEN')
        self.assertEqual(result['proof_statuses']['EQ_CAL_STEP'],'PROVEN')
        self.assertIn(CALIBRATION_METHOD,result['reason'])
        raw={"EQ_MITER."+label:status.lower() for label,status in result['proof_statuses'].items()}
        raw['EQ_MITER.EQ_CAL_STEP']='unknown'
        (directory/'result.json').unlink()
        with patch('looplib.calibration_cut.flat_transition',side_effect=UnsupportedCut('test sequential fallback')),patch.dict('os.environ',{'LOOP_TEST_PROPERTIES_JSON':json.dumps(raw)}),redirect_stdout(io.StringIO()):
            failed=qualify_mutant(*args,calibration=True)
        self.assertEqual(failed['status'],'UNRESOLVED')
        self.assertIn('EQ_CAL_STEP',failed['reason'])

    def test_real_mutant_comparison_remains_unconditional(self):
        rtl=self.dataset/'rtl/toy.sv'
        mutant=self.dataset/'gen_assertions/mutant.sv'
        mutant.parent.mkdir(parents=True,exist_ok=True)
        mutant.write_text(rtl.read_text().replace('assign ack = req','assign ack = !req'))
        import hashlib
        m={'id':'F3_1','replace':str(rtl.resolve()),'file':str(mutant),'sha256':hashlib.sha256(mutant.read_bytes()).hexdigest()}
        with redirect_stdout(io.StringIO()):
            result=qualify_mutant(self.dataset,self.unit,m,self.config,self.lock,self.dataset/'gen_assertions/F3_1')
        self.assertEqual(result['status'],'DIFFERENT')
        sv=Path(next(f for f in result['evidence_files'] if f.endswith('miter.sv'))).read_text()
        self.assertNotIn('EQ_CAL_STATE_EQUAL',sv)
        self.assertNotIn('EQ_CAL_BASE',sv)


if __name__=='__main__':unittest.main()
