"""Reproduce the reported ERS040 using production Tcl and a rejecting reset shim.

This is an API-contract regression, not a licensed Jasper execution.
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(ROOT/'vendor/aq'),str(ROOT/'vendor/faults')]
from jasper_reset import reset_pins,reset_command
from looplib.common import EnvironmentLock,read,save,EnvironmentSetupError
from looplib.technical import qualify,aq_config
from looplib.properties import render
from looplib.faults import build_pool,formal_config,measure
from looplib.technical_cache import TechnicalCache
import assertion_aq_pipeline as aq
import test_existing_workflow as fixtures


class ResetEncodingTests(unittest.TestCase):
    def test_native_pin_forms_and_nested_or(self):
        cases={'':[], 'rst':['rst'],'!rst_n':['!rst_n'],'~rst_n':['!rst_n'],
               "rst == 1'b0":['!rst'],"rst == 1'b1":['rst'],
               '!rst_i || wb_rst_i':['!rst_i','wb_rst_i'],
               '((!rst_i) || (wb_rst_i))':['!rst_i','wb_rst_i'],
               'a || (b || !c)':['a','b','!c'],
               '(!a) || !a':['!a'],
               '!(rst_n)':['!rst_n']}
        for expression,expected in cases.items():
            with self.subTest(expression=expression):self.assertEqual(reset_pins(expression),expected)
        self.assertEqual(reset_command('!rst_i || wb_rst_i'),'reset -expression {!rst_i wb_rst_i}')
        self.assertEqual(reset_command(''),'reset -none')

    def test_no_guessed_boolean_conversion_or_tcl_injection(self):
        for expression in ('a && b','a | b','a ^ b','a || !a','1','a; exit','[exec x]',
                           'a ||','(a || b','a || b)','a == 2','a ? b : c','f(a)'):
            with self.subTest(expression=expression),self.assertRaises(ValueError):reset_pins(expression)

    def test_independent_miter_qualifies_internal_reset_alias_in_reference_scope(self):
        from looplib.equivalence import miter_source
        source="module toy(input clk,arst_i,wb_rst_i,d,output logic q);wire rst_i=arst_i;always @(posedge clk)if(!rst_i || wb_rst_i)q<=0;else q<=d;endmodule"
        unit={'top':'toy','clock_mode':'clocked','clock':'clk','reset_expression':'!rst_i || wb_rst_i'}
        compiled,info=miter_source(source,source,'',unit)
        self.assertEqual(info['formal_reset_expression'],'!ref_dut.rst_i || wb_rst_i')
        self.assertIn('disable iff (!ref_dut.rst_i || wb_rst_i)',compiled)
        self.assertEqual(reset_command(info['formal_reset_expression']),'reset -expression {!ref_dut.rst_i wb_rst_i}')
        self.assertEqual(unit['reset_expression'],'!rst_i || wb_rst_i')


class ResetGateTests(unittest.TestCase):
    setUp=fixtures.ExistingWorkflowTests.setUp
    passing_technical_stub=fixtures.ExistingWorkflowTests.passing_technical_stub
    def prepare(self):
        fixtures.ExistingWorkflowTests.prepare(self,one=True)
        # Two independently active reset pins and an unchanged OR disable iff.
        self.original_file.write_text(self.original_file.read_text().replace('disable iff (rst)','disable iff (!rst || req)'))
        from looplib.bootstrap import init_dataset
        from looplib.inputs import prepare_existing
        self.setup=prepare_existing(self.dataset,init_dataset(self.dataset))
        self.unit=self.setup['units'][0];self.unit['reset_expression']='!rst || req'
        save(self.dataset/'gen_assertions/setup/dataset.json',self.setup)
        self.lock=EnvironmentLock(self.dataset,self.setup)
        from looplib.inputs import load_existing
        self.props=load_existing(self.dataset,self.unit)

    def test_original_command_reproduces_syntax_pass_then_environment_error(self):
        self.prepare()
        broken=(ROOT/'vendor/aq/aq_jasper_generic.tcl').read_text().replace('reset -expression $::AQ_RESET_PINS','reset -expression $::AQ_RESET_EXPRESSION')
        # Patch only template loading; every other production path remains real.
        original=aq.Pipeline.load_text_template
        def template(pipeline,cfg,inline,file):
            if file=='tcl_template_file':return broken
            return original(pipeline,cfg,inline,file)
        output=io.StringIO()
        with patch.object(aq.Pipeline,'load_text_template',new=template),redirect_stdout(output):
            # Production pipeline executes in its own Python subprocess. Supply
            # the old template file through aq_config rather than the mock above.
            bad_template=self.dataset/'old_reset.tcl';bad_template.write_text(broken)
            original_config=aq_config
            def config(*args):
                result=original_config(*args)
                for stage in result['stages'].values():stage['tcl_template_file']=str(bad_template)
                return result
            with patch('looplib.technical.aq_config',side_effect=config):
                syntax=qualify(self.dataset,self.unit,self.props,self.directory/'old_syntax',self.config,stage='syntax')
                self.assertTrue(syntax[0]['passed'])
                with self.assertRaisesRegex(EnvironmentSetupError,'ERS040'):
                    qualify(self.dataset,self.unit,self.props,self.directory/'old_fpv',self.config,stage='fpv')

    def test_fixed_step_a_keeps_disable_iff_and_both_reset_pins(self):
        self.prepare();before=self.original_file.read_bytes()
        with redirect_stdout(io.StringIO()):
            for stage in ('syntax','fpv','vacuity'):
                rows=qualify(self.dataset,self.unit,self.props,self.directory/stage,self.config,stage=stage)
                self.assertTrue(all(row['passed'] for row in rows),rows)
        for stage in ('fpv','vacuity'):
            logs=list((self.directory/stage).rglob('run.log'))
            self.assertTrue(any('OFFLINE_RESET_PINS !rst req' in log.read_text() for log in logs))
        self.assertEqual(self.original_file.read_bytes(),before)
        self.assertIn('disable iff (!rst || req)',render(self.dataset,self.unit,self.props))

    def test_step_b_uses_same_encoding_and_keeps_test_sealed(self):
        self.prepare()
        (self.dataset/'rtl/toy.sv').write_text("module toy(input logic clk,rst,req,output logic ack);always_comb if(req)ack=1;else ack=0;endmodule")
        self.lock=EnvironmentLock(self.dataset,self.setup)
        self.config['fault_qualification']='syntax'
        props=[{**p,'checks':dict.fromkeys(('syntax','fpv','vacuity','spec'),'PASS')} for p in self.props]
        with redirect_stdout(io.StringIO()):
            pool=build_pool(self.dataset,self.unit,self.config,self.lock)
            result=measure(self.dataset,self.unit,props,pool,'dev',self.directory/'step_b',self.config,self.lock)
        self.assertEqual(result['status'],'MEASURED')
        logs=list((self.directory/'step_b').rglob('run.log'))
        self.assertTrue(any('OFFLINE_RESET_PINS !rst req' in p.read_text() for p in logs))
        self.assertFalse((self.directory/'step_b_test').exists())

    def test_composite_cache_cannot_reuse_old_encoder_algorithms(self):
        self.prepare();cache=TechnicalCache(self.dataset,self.unit,self.config,'fpv')
        key=cache._key(self.props[0]);self.assertIsNotNone(key)
        self.assertFalse(cache._compatible_keys[key])

    def test_unsupported_reset_stops_as_setup_error_before_starting_a_solver(self):
        self.prepare();self.unit['reset_expression']='rst && req'
        with patch('looplib.technical.command',side_effect=AssertionError('Do not start a solver')),redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(EnvironmentSetupError,'Unsupported Jasper reset declaration'):
                qualify(self.dataset,self.unit,self.props,self.directory/'invalid',self.config,stage='fpv')


if __name__=='__main__':unittest.main()
