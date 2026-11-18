"""Actual cut-SV elaboration, constant evaluation, and two-phase gating."""
import io,itertools,json,sys,unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from looplib.calibration_cut import flat_transition,UnsupportedCut,METHOD
from looplib.equivalence import design_info,qualify_mutant,slang
from looplib.common import EnvironmentLock
import test_jasper_workspace as workspace

UNIT={'top':'toy','clock_mode':'clocked','clock':'clk','reset_expression':'rst'}
RTL='''module toy(input clk,rst,req,output logic a,b);
always @(posedge clk) begin
 if(rst) begin a<=0;b<=0;end
 else if(req) begin a<=b;b<=a;end
end
endmodule'''

class CutTests(unittest.TestCase):
 def test_real_cut_is_stateless_and_preserves_nonblocking_rhs_and_hold(self):
  source,info=flat_transition(RTL,UNIT)
  self.assertEqual(design_info(source,'toy')['states'],{})
  self.assertEqual(info['registers'],['a','b'])
  self.assertIn('EQ_NEXT_a = a;',source);self.assertIn('EQ_NEXT_b = b;',source)
  self.assertRegex(source,r'EQ_NEXT_a\s*=\s*b');self.assertRegex(source,r'EQ_NEXT_b\s*=\s*a')
  self.assertNotIn('posedge',source);self.assertNotIn('<=',source)
  self.assertIn('assign a = EQ_CUR_a;',source)

 def test_actual_generated_bodies_constant_evaluate_for_every_toy_state_and_input(self):
  # Execute the transformed statements through Slang's real constant-function
  # evaluator, independently checking NBA swap and hold behavior exhaustively.
  source,info=flat_transition(RTL,UNIT);p=slang();tree=p.syntax.SyntaxTree.fromText(source);bodies=[]
  def body(n):
   if str(n.kind).endswith('AlwaysCombBlock'):bodies.append(str(n.statement))
  tree.root.visit(body)
  function='''function automatic logic [1:0] step(input logic EQ_CUR_a,EQ_CUR_b,req,rst);
logic a,b,EQ_NEXT_a,EQ_NEXT_b;
begin a=EQ_CUR_a;b=EQ_CUR_b;
'''+ '\n'.join(bodies)+'''\nstep={EQ_NEXT_a,EQ_NEXT_b};end endfunction\n'''
  expected={};params=[]
  for index,(a,b,req,rst) in enumerate(itertools.product((0,1),repeat=4)):
   name=f'V{index}';expected[name]=0 if rst else ((b<<1)|a) if req else ((a<<1)|b)
   params.append(f'localparam logic[1:0] {name}=step(1\'b{a},1\'b{b},1\'b{req},1\'b{rst});')
  compilation=p.ast.Compilation();compilation.addSyntaxTree(p.syntax.SyntaxTree.fromText('module test;'+function+'\n'.join(params)+'endmodule'))
  root=compilation.getRoot();self.assertFalse([d for d in compilation.getAllDiagnostics() if d.isError()])
  actual={}
  def value(n):
   if str(n.kind).endswith('Parameter') and n.name in expected:actual[n.name]=str(n.value)
  root.visit(value)
  self.assertEqual(set(actual),set(expected))
  for name,result in actual.items():
   literal=result.split("'")[-1].lstrip('s');value=int(literal[1:],{'b':2,'d':10,'h':16,'o':8}[literal[0]])
   self.assertEqual(value,expected[name],(name,result))

 def test_nonansi_signed_types_are_preserved(self):
  s='module toy(clk,rst,d,q);input clk,rst;input signed[3:0]d;output reg signed[3:0]q;always @(posedge clk) if(rst)q<=0;else q<=d>>>1;endmodule'
  cut,info=flat_transition(s,UNIT)
  self.assertEqual(design_info(cut,'toy')['states'],{})
  self.assertIn('signed',cut)

 def test_complete_if_else_is_stateless_but_incomplete_branch_is_rejected(self):
  rtl='module toy(input clk,rst,req,output logic q);logic v;always_comb if(req)v=1;else v=0;always @(posedge clk)if(rst)q<=0;else q<=v;endmodule'
  cut,_=flat_transition(rtl,UNIT);self.assertEqual(design_info(cut,'toy')['states'],{})
  with self.assertRaises(UnsupportedCut):flat_transition(rtl.replace(';else v=0',''),UNIT)

 def test_unsigned_full_case_domain_coverage_is_required_without_default(self):
  rtl='module toy(input clk,rst,input[1:0]sel,output logic q);logic v;always_comb case(sel)0:v=0;1:v=1;2:v=0;3:v=1;endcase always @(posedge clk)if(rst)q<=0;else q<=v;endmodule'
  cut,_=flat_transition(rtl,UNIT);self.assertEqual(design_info(cut,'toy')['states'],{})
  with self.assertRaises(UnsupportedCut):flat_transition(rtl.replace('3:v=1;',''),UNIT)
  with self.assertRaises(UnsupportedCut):flat_transition(rtl.replace('input[1:0]sel','input signed[1:0]sel'),UNIT)

 def test_unsupported_storage_or_semantics_fall_back_instead_of_being_cut(self):
  cases=[RTL.replace('posedge clk','posedge clk or posedge rst'),
         RTL.replace('a<=b','a=b'),RTL.replace('a<=b','a[0]<=b'),
         RTL.replace('logic a,b','logic a=0,b'),
         RTL.replace('a<=b','a<=$random'),
         RTL.replace('endmodule','logic x;always @* if(req)x=a;endmodule'),
         RTL.replace('endmodule','logic x;always @* begin // leading comment\n x=x;end endmodule'),
         RTL.replace('endmodule','logic x,v;always @* if(x++)v=0;else v=1;endmodule'),
         RTL.replace('endmodule','logic[1:0]x;logic ix;always @* begin x=0;x[ix]=req;ix=req;end endmodule')]
  for text in cases:
   with self.subTest(text=text),self.assertRaises(UnsupportedCut):flat_transition(text,UNIT)


class ExecutionTests(unittest.TestCase):
 setUp=workspace.PoolRetryTests.setUp
 prepare=workspace.PoolRetryTests.prepare
 def sequential(self):
  (self.dataset/'rtl/toy.sv').write_text('module toy(input clk,rst,req,output logic ack);always @(posedge clk) if(rst)ack<=0;else if(req)ack<=~ack;endmodule')
  self.lock=EnvironmentLock(self.dataset,self.setup)
 def execute(self,env=None):
  directory=self.dataset/'gen_assertions/golden_calibration'
  with patch.dict('os.environ',env or {}),redirect_stdout(io.StringIO()):
   return qualify_mutant(self.dataset,self.unit,{'id':'golden_calibration','sha256':self.lock.identity},self.config,self.lock,directory,calibration=True)
 def test_stateless_step_and_original_clocked_environment_are_both_required(self):
  self.sequential();result=self.execute()
  self.assertEqual(result['status'],'EQUIVALENT');self.assertEqual(result['calibration_method'],METHOD)
  self.assertEqual(set(result['phases']),{'next_state','environment'})
  logs={phase:Path(record['log']) for phase,record in result['phases'].items()}
  step=(logs['next_state'].parent/'miter.sv').read_text();environment=(logs['environment'].parent/'miter.sv').read_text()
  self.assertNotIn('posedge',step);self.assertNotIn('assume',step)
  self.assertIn('fixed_environment',environment);self.assertIn('assume property',environment)
  self.assertNotIn('EQ_CAL_STEP',environment);self.assertNotIn('EQ_OUTPUT_0001:',environment)
  self.assertIn('EQ_INIT_0001',environment)
  self.assertIn('set FH_CLOCK_MODE {none}',(logs['next_state'].parent/'run.tcl').read_text())
  self.assertIn('set FH_CLOCK_MODE {clocked}',(logs['environment'].parent/'run.tcl').read_text())
 def test_unknown_step_never_becomes_equivalent_or_launches_environment(self):
  self.sequential()
  properties={'EQ_MITER.EQ_OUTPUT_0001':'unknown','EQ_MITER.EQ_OUTPUT_0002':'proven','EQ_MITER.EQ_ENV_WITNESS':'cex'}
  result=self.execute({'LOOP_TEST_PROPERTIES_JSON':json.dumps(properties)})
  self.assertEqual(result['status'],'UNRESOLVED');self.assertNotIn('environment',result['phases'])
 def test_unreachable_environment_blocks_despite_proven_combinational_step(self):
  self.sequential()
  import looplib.jasper_jobs as jobs
  original=jobs.JasperAttempt.run
  def run(job,exe,tcl,timeout,**kwargs):
   source=(job.directory/'miter.sv').read_text()
   if 'fixed_environment' in source:
    with patch.dict('os.environ',{'LOOP_TEST_PROPERTIES_JSON':json.dumps({'EQ_MITER.EQ_ENV_WITNESS':'proven'})}):
     return original(job,exe,tcl,timeout,**kwargs)
   return original(job,exe,tcl,timeout,**kwargs)
  with patch('looplib.jasper_jobs.JasperAttempt.run',new=run):result=self.execute()
  self.assertEqual(result['status'],'UNRESOLVED')
  self.assertTrue(result['phases']['next_state']['passed']);self.assertFalse(result['phases']['environment']['passed'])

if __name__=='__main__':unittest.main()
