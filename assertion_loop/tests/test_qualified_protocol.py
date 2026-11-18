"""Real SV front-end checks and explicitly offline protocol integration tests."""
import copy,io,json,os,sys,tempfile,unittest,hashlib
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from looplib.common import read,save,digest
from looplib.equivalence import miter_source,design_info,classify,UnsupportedEquivalence
from looplib.faults import partition,feedback,score_rows
from looplib.llm import Client,assertion_blocks,ResponseFormatError,IncompleteResponseError,RepeatedRequestError,recovery_prompt_text
from looplib.engine import run_unit,StepAAttemptLimit
from looplib.controlled import run_pair
from looplib.reporting import report,dataset_report
import test_existing_workflow as fixtures
import evaluate_frozen,run
UNIT={'top':'toy','clock_mode':'clocked','clock':'clk','reset_expression':'rst'}
SEQ='module toy(input logic clk,rst,d,output logic q);always_ff @(posedge clk) if(rst) q<=0;else q<=d;endmodule'
class CompilerTests(unittest.TestCase):
 def test_state_correspondence(self):
  source,info=miter_source(SEQ,SEQ.replace('q<=d','q<=!d'),'',UNIT)
  self.assertEqual(info['coupled_initial_registers'],['q']);self.assertIn('ref_dut.q == mut_dut.q',source)
 def test_package_enum_struct_ports(self):
  s='package p;typedef enum logic[1:0]{A,B,C} op_t;typedef struct packed{logic x,y;} out_t;endpackage module toy(input p::op_t op,output p::out_t out);assign out=op;endmodule'
  source,info=miter_source(s,s,'',{'top':'toy','clock_mode':'none'})
  self.assertIn('input p::op_t op',source);self.assertIn('p::out_t ref_out',source)
 def test_interface_width_change(self):
  s='module toy #(parameter W=2)(input d,output [W-1:0] q);assign q=d;endmodule'
  source,info=miter_source(s,s.replace('W=2','W=3'),'',{'top':'toy','clock_mode':'none'})
  self.assertIsNone(source);self.assertEqual(info['status'],'INTERFACE_INCOMPATIBLE')
 def test_state_shape_change_is_unresolved(self):
  s='module toy(input clk,rst,d,output q);logic[1:0] count;always_ff @(posedge clk) count<=count+d;assign q=count[0];endmodule'
  source,info=miter_source(s,s.replace('[1:0] count','[2:0] count'),'',UNIT)
  self.assertIsNone(source);self.assertEqual(info['status'],'UNRESOLVED')
 def test_explicit_initializers_not_equalized(self):
  s=SEQ.replace('output logic q','output logic q=0');source,info=miter_source(s,s.replace('q=0','q=1'),'',UNIT)
  self.assertEqual(info['coupled_initial_registers'],[]);self.assertNotIn('EQ_INIT_',source)
 def test_blocking_clocked_state(self):
  s=SEQ.replace('always_ff','always').replace('q<=','q=');self.assertIn('q',design_info(s,'toy')['states'])
 def test_unrelated_top_missing_package(self):
  s=SEQ+' module other(input missing_pkg::data_t a);endmodule';source,info=miter_source(s,s,'',UNIT)
  self.assertNotIn('missing_pkg',source);self.assertEqual(info['status'],'PREPARED')
 def test_assumptions_on_reference_only(self):
  h='module check(input clk,rst,d,q);fixed:assume property(@(posedge clk) !rst);endmodule bind toy check ck(.*);'
  source,info=miter_source(SEQ,SEQ,h,UNIT)
  self.assertIn('bind toy__eq_ref check',source);self.assertNotIn('bind toy__eq_mut check',source)
 def test_package_mutation_not_silently_shared(self):
  s='package p;parameter N=1;endpackage module toy(output q);assign q=p::N;endmodule'
  with self.assertRaisesRegex(UnsupportedEquivalence,'Mutated package'):miter_source(s,s.replace('N=1','N=0'),'',{'top':'toy','clock_mode':'none'})
 def test_vacuous_equivalence_rejected(self):
  self.assertEqual(classify({'o':'PROVEN','EQ_ENV_WITNESS':'PROVEN'},{'o':'q'})[0],'UNRESOLVED')
  self.assertEqual(classify({'o':'PROVEN','EQ_ENV_WITNESS':'CEX'},{'o':'q'})[0],'EQUIVALENT')
  self.assertEqual(classify({'o':'CEX','EQ_ENV_WITNESS':'CEX'},{'o':'q'}),('DIFFERENT',['q']))
 def test_unknown_and_missing_not_proven(self):
  self.assertEqual(classify({'o':'UNKNOWN','EQ_ENV_WITNESS':'CEX'},{'o':'q'})[0],'UNRESOLVED')
  self.assertEqual(classify({'EQ_ENV_WITNESS':'CEX'},{'o':'q'})[0],'UNRESOLVED')
 def test_internal_mutant_assume_rejected(self):
  s='module toy(input d,output q);assign q=d;always_comb assume(d);endmodule'
  with self.assertRaisesRegex(UnsupportedEquivalence,'RTL-internal assumptions'):miter_source(s,s,'',{'top':'toy','clock_mode':'none'})
  # Comments and strings are not constraints; actual restrict properties are.
  clean='module toy(input d,output q); // assume this is an explanatory comment\nassign q=d;endmodule'
  self.assertEqual(miter_source(clean,clean,'',{'top':'toy','clock_mode':'none'})[1]['status'],'PREPARED')
  restricted=SEQ.replace('endmodule','restrict property(@(posedge clk) !rst);endmodule')
  with self.assertRaisesRegex(UnsupportedEquivalence,'RTL-internal assumptions'):miter_source(restricted,restricted,'',UNIT)
class FeedbackTests(unittest.TestCase):
 def test_source_sites_across_families_are_sealed(self):
  ms=[{'id':f'{i}_{j}','family':f'F{i}','replace':'a.sv','line':j} for i in (1,2,3) for j in range(12)]
  parts=partition(ms,14,.25);sites=[{(m['replace'],m['line']) for m in p} for p in parts]
  self.assertTrue(all(parts));self.assertFalse(sites[0]&sites[1] or sites[1]&sites[2] or sites[0]&sites[2])
 def test_rotation_unknown_and_test_exclusion(self):
  pool={'dev_ids':['a','b','u'],'mutants':[{'id':i,'replace':'x','line':n,'family':'F1','operator':'flip','before':'a','after':'!a','affected_outputs':['q']} for i,n in [('a',1),('b',2),('u',3),('test',4)]]}
  m={'undetected_ids':['a','b','u','test'],'unknown_ids':['u'],'detection_lower_percent':0,'unknown_only_count':1}
  self.assertEqual(feedback(pool,m,1,1)['missed_examples'][0]['id'],'a');self.assertEqual(feedback(pool,m,1,2)['missed_examples'][0]['id'],'b')
  self.assertEqual(feedback(pool,m,1)['actionable_miss_count'],2)
 def test_defaults_no_unrequested_count_or_token_quota(self):
  c=read(ROOT/'config.json');self.assertIsNone(c['max_mutants_per_family']);self.assertIsNone(c['max_output_tokens']);self.assertEqual(c['step_a_max_attempts'],50);self.assertNotIn('candidates_per_round',c)
 def test_finite_blocks_and_no_new_cannot_discard_repairs(self):
  self.assertTrue(assertion_blocks('NO_NEW_ASSERTIONS no justified gap')['no_new_assertions'])
  with self.assertRaises(ValueError):assertion_blocks('NO_NEW_ASSERTIONS discard',['failed'])
  self.assertEqual(len(assertion_blocks('BEGIN_ASSERTION x\nd |-> q\nEND_ASSERTION\nEND_BATCH')['assertions']),1)
class WorkflowTests(unittest.TestCase):
 def setUp(self):
  fixtures.ExistingWorkflowTests.setUp(self);self.dataset=self.dataset.resolve()
 prepare=fixtures.ExistingWorkflowTests.prepare
 fake_measure=fixtures.ExistingWorkflowTests.fake_measure
 passing_technical_stub=fixtures.ExistingWorkflowTests.passing_technical_stub
 def judge(self,*args):return {'verdict':'PASS','question':'','reason':'offline','evidence_quote':'','suggested_change':''}
 def execute(self,call,budgets):
  with patch('looplib.engine.qualify',side_effect=self.passing_technical_stub),patch('looplib.engine.measure',side_effect=self.fake_measure),redirect_stdout(io.StringIO()):
   return run_unit(self.dataset,self.unit,'deepseek',1,self.config,SimpleNamespace(call=call,records=lambda:[]),self.pool,self.lock,self.directory,budgets,False)
 def test_saturation_reports_actual_rounds_without_invented_evaluations(self):
  self.prepare(one=True);self.config['stop_on_saturation']=True;calls=[]
  def call(m,p,msg,c):calls.append((m,p));return self.judge()
  rows=self.execute(call,[1,3,5,10,15,20]);self.assertEqual(calls,[('gpt','spec_check')])
  self.assertTrue(all(r['step_b_rounds_completed']==1 for r in rows));self.assertEqual(rows[-1]['status'],'EARLY_STOPPED');self.assertEqual(rows[-1]['stop_reason'],'DEV_SATURATED')
  self.assertFalse((self.directory/'rounds/round_02').exists())
 def test_raw_format_recovery_regenerates_batch_without_fake_single_id(self):
  self.prepare(one=True);prompts=[]
  def call(m,p,msg,c):
   if p!='generation':return self.judge()
   prompts.append(copy.deepcopy(msg))
   if len(prompts)==1:raise ResponseFormatError('bad delimiter','EXACT RAW BAD REPLY')
   self.assertEqual(c['repair_ids'],[]);self.assertIn('EXACT RAW BAD REPLY',msg[-1]['content'])
   return assertion_blocks('BEGIN_ASSERTION n1\nreq |=> ack\nEND_ASSERTION\nBEGIN_ASSERTION n2\n!req |-> !ack\nEND_ASSERTION\nEND_BATCH')
  rows=self.execute(call,[1,2]);self.assertEqual(len(prompts),2);self.assertEqual(rows[-1]['qualified_count'],3)
 def test_gpt_truncation_retries_checker_only(self):
  self.prepare(one=True);calls=[]
  def call(m,p,msg,c):
   calls.append((m,p));self.assertEqual((m,p),('gpt','spec_check'))
   if len(calls)==1:raise IncompleteResponseError('length','{"verdict":"PASS"')
   self.assertIn('{"verdict":"PASS"',msg[-1]['content']);return self.judge()
  rows=self.execute(call,[1]);state=read(self.directory/'rounds/round_01/round.json')
  self.assertEqual(state['step_a_attempts_this_round'],2);self.assertEqual(rows[0]['generation_calls_used'],0);self.assertEqual(len([e for e in self.events if e['kind']=='gate']),3)
 def test_completed_prefix_persists_when_next_batch_hits_fifty(self):
  self.prepare(one=True)
  def call(m,p,msg,c):
   if p=='generation':raise ResponseFormatError('bad block','RAW BAD')
   return self.judge()
  with self.assertRaises(StepAAttemptLimit):self.execute(call,[1,3,20])
  row=read(self.directory/'budgets/budget_01/summary.json');self.assertEqual(row['validation_detected'],1);self.assertFalse((self.directory/'budgets/budget_20/summary.json').exists())
 def test_control_baseline_identical_and_none_arm_no_mutant_information(self):
  self.prepare(one=True);calls=[];self.pool['mutants'][0].update(before='SECRET_FAULT',replace='rtl/toy.sv',affected_outputs=['ack'])
  def call(m,p,msg,c):
   calls.append((m,p,copy.deepcopy(msg),dict(c)))
   if p=='generation':
    if c.get('feedback_condition')=='none':
     for secret in ('SECRET_FAULT','missed_examples','development_detection_percent'):self.assertNotIn(secret,json.dumps(msg))
    return assertion_blocks('BEGIN_ASSERTION supplement\nreq |=> ack\nEND_ASSERTION\nEND_BATCH')
   return self.judge()
  def measure(*args):
   dataset,u,props,pool,part,directory,c,lock=args
   if part!='dev':return self.fake_measure(*args)
   ms=[m for m in pool['mutants'] if m['id'] in pool['dev_ids']];rows=[{'assertion_id':p['label'],'mutant_id':m['id'],'status':'PROVEN'} for p in props for m in ms]
   metric=score_rows(props,ms,rows);metric.update(status='MEASURED',partition='dev');return metric
  experiment=self.dataset/'gen_assertions/controlled/paired'
  with patch('looplib.engine.qualify',side_effect=self.passing_technical_stub),patch('looplib.engine.measure',side_effect=measure),redirect_stdout(io.StringIO()):
   failed=run_pair(self.dataset,self.unit,'gpt',1,self.config,SimpleNamespace(call=call,records=lambda:[]),self.pool,self.lock,experiment,[1,2])
  self.assertEqual(failed,[])
  def state(arm):return read(experiment/arm/'gpt/run_01/units/toy/rounds/round_01/round.json')
  self.assertEqual(state('targeted')['accepted'],state('none')['accepted']);self.assertEqual(len([x for x in calls if x[3].get('feedback_condition')=='shared_baseline']),1)
  self.assertEqual([x[3]['feedback_condition'] for x in calls if x[1]=='generation'],['targeted','none'])
  self.assertIn('SECRET_FAULT',json.dumps(next(x[2] for x in calls if x[1]=='generation' and x[3]['feedback_condition']=='targeted')))
  rows=report(experiment);self.assertEqual(len(rows),4);self.assertTrue(all(r['complete_pair'] for r in read(experiment/'dataset_effectiveness.json')['paired']))
 def test_frozen_test_no_llm_and_exact_saved_assertions(self):
  self.prepare(one=True);self.pool['protocol']='observable_equivalence_v1'
  stub=self.dataset/'gen_assertions/stub.sv';stub.write_text('module toy;endmodule')
  for m in self.pool['mutants']:m.update(file=str(stub),sha256=hashlib.sha256(stub.read_bytes()).hexdigest())
  self.execute(self.judge,[1]);experiment=self.directory.parents[3];save(experiment/'config_snapshot.json',self.config)
  save(self.dataset/'gen_assertions/_shared/pools/toy'/self.pool['identity'][:20]/'pool.json',self.pool);measured=[]
  def measure(dataset,u,props,pool,part,directory,c,lock):
   self.assertEqual(part,'test');measured.append(props);return {'mutant_count':1,'detected_count':1,'unknown_only_count':0,'detection_lower_percent':100,'detection_upper_percent':100}
  with patch('evaluate_frozen.measure',side_effect=measure),patch('looplib.llm.Client.call',side_effect=AssertionError('No API')),redirect_stdout(io.StringIO()):
   self.assertEqual(evaluate_frozen.main([str(self.dataset),'--experiment','offline','--budget','1','--model','deepseek']),0)
  self.assertEqual(measured[0],read(self.directory/'budgets/budget_01/frozen_assertions.json'));self.assertEqual(read(experiment/'test_access.json')['budgets'],[1])
class RequestReportTests(unittest.TestCase):
 def test_identical_gpt_checker_request_also_stops_without_new_bill(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE"}):
   config=read(ROOT/"config.json");client=Client(config,Path(tmp)/"api")
   client.catalog={config["models"]["gpt"]["id"]:{"supported_parameters":[],"pricing":{}}}
   raw={"id":"offline","choices":[{"finish_reason":"stop","message":{"content":'{"verdict":"FAIL"}'}}],"usage":{}}
   with patch.object(client,"request",return_value=raw) as request,redirect_stdout(io.StringIO()):
    context={"generator_model":"deepseek","unit":"toy","round":1,"repeat":1,"assertion":"a"}
    client.call("gpt","spec_check",[{"role":"user","content":"same assertion and evidence"}],{**context,"step_a_attempt":1})
    with self.assertRaises(RepeatedRequestError):client.call("gpt","spec_check",[{"role":"user","content":"same assertion and evidence"}],{**context,"step_a_attempt":2})
   self.assertEqual(request.call_count,1)

 def test_concurrent_json_saves_are_atomic_and_have_distinct_temporaries(self):
  from concurrent.futures import ThreadPoolExecutor
  with tempfile.TemporaryDirectory() as tmp:
   target=Path(tmp)/"progress.json"
   with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda n:save(target,{"n":n}),range(100)))
   self.assertIn(read(target)["n"],range(100));self.assertFalse(list(Path(tmp).glob("*.tmp")))
   from looplib.common import table
   import csv
   csv_file=Path(tmp)/"report.csv"
   with ThreadPoolExecutor(max_workers=8) as pool:list(pool.map(lambda n:table(csv_file,[{"n":n}]),range(100)))
   with csv_file.open(encoding="utf-8-sig") as handle:rows=list(csv.DictReader(handle))
   self.assertEqual(len(rows),1);self.assertIn(int(rows[0]["n"]),range(100));self.assertFalse(list(Path(tmp).glob("*.tmp")))
 def test_dataset_guard_covers_setup_and_releases_after_failure(self):
  with tempfile.TemporaryDirectory() as tmp:
   with run.dataset_guard(tmp):
    with self.assertRaisesRegex(RuntimeError,"Another setup/loop"): 
     with run.dataset_guard(tmp):pass
   with run.dataset_guard(tmp):pass

 def test_runaway_duplicate_reply_compacts_without_losing_distinct_bodies(self):
  block='BEGIN_ASSERTION x\nd |-> q\nEND_ASSERTION\n'
  raw=block*1000+'BEGIN_ASSERTION y\n!d |-> !q\nEND_ASSERTION'
  compact=recovery_prompt_text(raw)
  self.assertLess(len(compact),len(raw)//20);self.assertIn('d |-> q',compact);self.assertIn('!d |-> !q',compact);self.assertIn('999',compact)

 def test_same_request_no_second_paid_call(self):
  with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'OPENROUTER_API_KEY':'OFFLINE_FAKE'}):
   c=read(ROOT/'config.json');client=Client(c,Path(tmp)/'api');client.catalog={c['models']['qwen']['id']:{'supported_parameters':[],'pricing':{}}}
   raw={'id':'offline','choices':[{'finish_reason':'stop','message':{'content':'BEGIN_ASSERTION x\nd |-> q\nEND_ASSERTION'}}],'usage':{}}
   with patch.object(client,'request',return_value=raw) as request,redirect_stdout(io.StringIO()):
    ctx={'generator_model':'qwen','unit':'toy','repeat':1,'round':2,'output_protocol':'assertion_blocks'}
    client.call('qwen','generation',[{'role':'user','content':'same'}],{**ctx,'step_a_attempt':1})
    with self.assertRaises(RepeatedRequestError):client.call('qwen','generation',[{'role':'user','content':'same'}],{**ctx,'step_a_attempt':2})
   self.assertEqual(request.call_count,1);self.assertEqual(len(client.records()),1)
 def test_missing_unit_not_success_only_average(self):
  with tempfile.TemporaryDirectory() as tmp:
   e=Path(tmp);save(e/'run_inventory.json',{'dataset':'d','units':['a','b'],'models':['gpt'],'repeats':1,'budgets':[1],'conditions':['targeted']})
   rows=[{'dataset':'d','unit':'a','generator_model':'gpt','repeat':1,'budget':1,'feedback_policy':'targeted','qualified_count':3,'validation_mutants':4,'validation_detected':2,'validation_lower_percent':50}]
   values,_=dataset_report(e,rows);r=values[0];self.assertEqual(r['missing_units'],['b']);self.assertIsNone(r['validation_micro_percent']);self.assertEqual(r['validation_partial_micro_percent'],50)
 def test_control_cli_options(self):
  a=run.parser(True,True).parse_args(['toy','--budgets','1,3,5,10,15,20','--prepare-pool']);self.assertTrue(a.prepare_pool);self.assertEqual(a.condition,'both');self.assertEqual(a.budgets,[1,3,5,10,15,20])
if __name__=='__main__':unittest.main()
