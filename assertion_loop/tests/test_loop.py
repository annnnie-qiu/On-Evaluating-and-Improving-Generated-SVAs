from __future__ import annotations
import copy
import csv
import json
import io
from contextlib import redirect_stdout
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from looplib.common import EnvironmentLock,read,save,command,EnvironmentSetupError
from looplib.bootstrap import init_dataset,MARKER,target_scope
from looplib.properties import validate_expression,render,generation_messages
from looplib.technical import qualify,runtime_diagnostics
from looplib.faults import partition,score_rows,feedback
from looplib.faults import formal_config
from collect_jasper import make_tcl,parse_log,tcl_atom
from validate_mutants import syntax_tcl
from looplib.llm import Client,totals,json_content,call_json,ModelProtocolError,ResponseFormatError,assertion_blocks
from looplib.engine import run_unit,generation_attempt
from looplib.reporting import report
from looplib.recovery import prepare_recovery,recover_pending
import run

FAKE=ROOT/"tests"/"fake_jasper.py"

class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/"toy"
        for name in ("rtl","spec","bind"): (self.root/name).mkdir(parents=True)
        (self.root/"rtl"/"toy.sv").write_text('''module toy(input logic clk, rst, req, input logic [3:0] a,b, output logic [3:0] q, output logic ack);
always_ff @(posedge clk) begin
  if (rst) q <= 4'd0;
  else if (req) q <= a + b;
  else q <= q - 4'd1;
end
assign ack = req ? a[0] : b[1];
always_comb begin
  case (a[1:0])
    2'b00: ack = b[0];
    2'b01: ack = b[1];
    default: ack = req;
  endcase
end
endmodule
''')
        (self.root/"spec"/"spec.md").write_text("When req is asserted, ack shall follow. Reset is active high. Test fixture only.")
        (self.root/"bind"/"toy_bind.sv").write_text("bind toy toy_assertions check_i (.*);\n")
        self.setup=init_dataset(self.root)
        self.unit=self.setup["units"][0]
        self.unit.update({"clock_mode":"clocked","clock":"clk","reset_expression":"rst"})
        save(self.root/"gen_assertions"/"setup"/"dataset.json",self.setup)
        self.config=read(ROOT/"config.json")
        self.config.update(stop_on_saturation=False,stop_on_repeated_request=False)
        self.config["fault_qualification"]="equivalence"
        self.config.update({"backend":"lexical","max_mutants_per_family":2,"max_rounds":3,
                            "input_mode":"generate",  # Explicit legacy-generation regressions.
                            "jasper_executable":str(FAKE),"jasper_version_tag":"OFFLINE_TEST_DOUBLE",
                            "syntax_timeout_seconds":10,"proof_timeout_seconds":10,"max_output_tokens":4096})
        FAKE.chmod(0o755)

    def fake_request(self):
        counters={"generation":0,"spec":0}
        def request(path,body=None):
            if path=="/models": return {"data":[{"id":v["id"],"supported_parameters":["temperature","response_format"],
                         "pricing":{"prompt":"0.000001","completion":"0.000002"}} for v in self.config["models"].values()]}
            isgen=body["messages"][0]["content"].startswith("Generate")
            if isgen:
                counters["generation"]+=1
                item={"expression":f"req |-> ##{counters['generation']} ack","basis":"fixture"}
                text=body["messages"][-1]["content"]
                feedback=json.loads(text.split("FEEDBACK:\n",1)[1]) if "FEEDBACK:\n" in text else {}
                if feedback.get("repair_questions"): item["repair_id"]=feedback["repair_questions"][0]["repair_id"]
                payload={"assertions":[item]}
            else:
                counters["spec"]+=1
                payload={"verdict":"PASS","question":"","reason":"offline fixture","evidence_quote":""}
            return {"id":"fixture","choices":[{"message":{"content":json.dumps(payload)},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":0.00014}}
        return request,counters

class SafetyTests(Fixture):
    def test_fixed_assumptions_cannot_be_injected(self):
        malicious=["1); assume property (1'b0", "`include x", "a = 0", "$system('x')", "foo()", "@(posedge clk) req", "disable iff(rst) req", "a++", "a /*hi*/"]
        for e in malicious:
            with self.subTest(e=e), self.assertRaises(ValueError): validate_expression(e,self.unit)
        self.assertEqual(validate_expression("req |-> ##1 $stable(q)",self.unit),"req |-> ##1 $stable(q)")

    def test_template_assumptions_preserved_and_lock_detects_edit(self):
        template=self.root/self.unit["template"]
        template.write_text(template.read_text().replace(MARKER,"fixed_env: assume property (@(posedge clk) !rst);\n"+MARKER))
        lock=EnvironmentLock(self.root,self.setup)
        source=render(self.root,self.unit,[{"label":"a1","expression":"req |-> ack"}])
        self.assertIn("fixed_env: assume property",source)
        self.assertEqual(source.count("assume"),1)
        template.write_text(template.read_text().replace("!rst","rst"))
        with self.assertRaises(RuntimeError): lock.check()

    def test_stage_short_circuit_real_aq_engine(self):
        props=[{"label":f"a{i}","expression":e} for i,e in enumerate([
            "syntax_bad |-> ack","fpv_bad |-> ack","vac_bad |-> ack","req |-> ack"])]
        events=self.root/"gen_assertions"/"events.jsonl"
        with patch.dict(os.environ,{"LOOP_TEST_EVENTS":str(events)}):
            result=qualify(self.root,self.unit,props,self.root/"gen_assertions"/"technical",self.config)
        self.assertEqual([r.get("failed_stage") for r in result],["syntax",None,None,None])
        self.assertFalse(any(r["technical_pass"] for r in result))
        self.assertTrue(Path(result[0]["log_file"]).is_absolute())
        self.assertIn("fixture syntax error",result[0]["diagnostic"])
        log=[json.loads(s) for s in events.read_text().splitlines()]
        self.assertEqual([x["stage"] for x in log],["syntax"])

    def test_bootstrap_with_only_required_folders(self):
        self.assertFalse((self.root/"assertions").exists())
        self.assertTrue((self.root/self.unit["template"]).exists())
        self.assertIn("toy_assertions",(self.root/self.unit["template"]).read_text())

class TclTargetTests(Fixture):
    """Execute production Tcl with explicit target + failing native properties."""
    def execute(self,script,statuses):
        folder=self.root/"gen_assertions"/"tcl_regression"
        folder.mkdir(parents=True,exist_ok=True)
        path=folder/"run.tcl"
        path.write_text(script)
        env={**os.environ,"LOOP_TEST_PROPERTIES_JSON":json.dumps(statuses)}
        return subprocess.run([sys.executable,str(FAKE),"-batch",str(path)],capture_output=True,text=True,env=env,check=False)

    def inputs(self,labels=("loop_target",)):
        folder=self.root/"gen_assertions"/"tcl_regression"
        folder.mkdir(parents=True,exist_ok=True)
        source=folder/"checker.sv"
        source.write_text(render(self.root,self.unit,[{"label":x,"expression":"req |-> ack"} for x in labels]))
        config=formal_config(self.root,self.unit,source,list(labels),self.config)
        return config,[self.root/"rtl"/"toy.sv"],source,[self.root/"bind"/"toy_bind.sv"],[],[]

    def aq_script(self,stage,target=None,scope=None):
        config,rtl,source,binds,_,_=self.inputs()
        target=target or ("AQ_ANT_REACH_test" if stage=="vacuity" else "loop_target")
        vals={"stage_tcl":tcl_atom(stage),"project_root_tcl":tcl_atom(self.root),
              "assertion_file_tcl":tcl_atom(source),"top_tcl":"{toy}","property_name_tcl":"{loop_target}",
              "target_property_name_tcl":tcl_atom(target),"target_scope_tcl":tcl_atom(scope if scope is not None else config["target_scope"]),"assertion_kind_tcl":"{concurrent}",
              "vacuity_mode_tcl":"{witness}","rtl_filelist_tcl":"{}","rtl_files_tcl":tcl_atom(rtl[0]),
              "include_dirs_tcl":"","defines_tcl":"","bind_files_tcl":tcl_atom(binds[0]),
              "clock_mode_tcl":"{clocked}","clock_signal_tcl":"{clk}","reset_expression_tcl":"{rst}","reset_pins_tcl":"{rst}"}
        template=(ROOT/"vendor"/"aq"/"aq_jasper_generic.tcl").read_text()
        return re.sub(r"\{\{(\w+)\}\}",lambda m:vals[m[1]],template)

    def test_setup_two_global_assertions_one_target_is_valid(self):
        args=self.inputs()
        script=syntax_tcl(*args,1)
        result=self.execute(script,{"toy.check_i.loop_target":"proven","toy.native_unique_case_check":"cex"})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("FH_SYNTAX_PASS",result.stdout)
        self.assertIn("total=2 target=1 background=1",result.stdout)

    def test_step_b_only_emits_requested_targets_and_partial_subsets(self):
        config,rtl,source,binds,includes,defines=self.inputs(("loop_target","loop_other"))
        script=make_tcl(config,rtl,source,binds,includes,defines,expected=["loop_target"])
        result=self.execute(script,{"toy.check_i.loop_target":"proven","toy.native_unique_case_check":"cex"})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(parse_log(result.stdout,["loop_target"]),{"loop_target":"PROVEN"})
        self.assertNotIn("FH_PROPERTY\ttoy.native",result.stdout)

    def test_missing_and_duplicate_target_labels_fail_closed(self):
        script=syntax_tcl(*self.inputs(),1)
        for names in ({"toy.native_unique_case_check":"cex"},
                      {"toy.c1.loop_target":"proven","toy.c2.loop_target":"proven"},
                      {"toy.check_i.not_loop_target":"proven"}):
            with self.subTest(names=names):
                result=self.execute(script,names)
                self.assertIn("FH_SYNTAX_FAIL",result.stdout)
                self.assertNotIn("FH_SYNTAX_PASS",result.stdout)
                self.assertIn("target assertion mapping failed",result.stdout)

    def test_fpv_background_cex_cannot_reject_proven_target(self):
        result=self.execute(self.aq_script("fpv"),{"toy.check_i.loop_target":"proven","toy.native_unique_case_check":"cex"})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn("AQ_FPV_PROVEN",result.stdout)
        self.assertNotIn("AQ_FPV_CEX",result.stdout)

    def test_vacuity_uses_witness_only_not_native_cex(self):
        cases=[("proven","cex","AQ_VACUOUS"),("cex","proven","AQ_NON_VACUOUS"),("unknown","cex","AQ_VACUITY_UNKNOWN")]
        for target_status,native_status,token in cases:
            with self.subTest(target=target_status):
                result=self.execute(self.aq_script("vacuity"),{"toy.check_i.AQ_ANT_REACH_test":target_status,"toy.native_unique_case_check":native_status})
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn("\n"+token+"\n",result.stdout)

    def test_empty_fixed_harness_for_step_b_has_no_invented_probe(self):
        args=list(self.inputs(()))
        args[0]["allow_empty_targets"]=True
        result=self.execute(syntax_tcl(*args,0),{"toy.native_unique_case_check":"cex"})
        self.assertIn("FH_SYNTAX_PASS",result.stdout)
        self.assertIn("target=0 background=1",result.stdout)

class BoundInstanceTests(Fixture):
    execute=TclTargetTests.execute
    inputs=TclTargetTests.inputs
    aq_script=TclTargetTests.aq_script
    # Exact instance names observed in the user's Jasper 26.06 run.log.
    bound="bsg_cache_buffer_queue.bsg_cache_buffer_queue_assertions_inst"
    embedded="bsg_cache_buffer_queue.i_assertions"
    label="loop_b001_t01_a001"

    def inventory(self,label=None,bound_status="proven",other_status="cex"):
        label=label or self.label
        return {f"<embedded>::{self.embedded}.{label}":other_status,
                f"<embedded>::{self.bound}.{label}":bound_status}

    def test_actual_duplicate_inventory_aq_syntax_uses_configured_bind(self):
        result=self.execute(self.aq_script("syntax",self.label,self.bound),self.inventory())
        self.assertIn("AQ_SYNTAX_PASS",result.stdout)
        self.assertIn(f"AQ_TARGET\t<embedded>::{self.bound}.{self.label}",result.stdout)
        self.assertNotIn("AQ_TARGET_ERROR",result.stdout)

    def test_fpv_and_vacuity_do_not_borrow_other_instance_results(self):
        for good,other,token in (("proven","cex","AQ_FPV_PROVEN"),("cex","proven","AQ_FPV_CEX")):
            with self.subTest(stage="fpv",good=good):
                result=self.execute(self.aq_script("fpv",self.label,self.bound),self.inventory(bound_status=good,other_status=other))
                self.assertIn("\n"+token+"\n",result.stdout)
        witness="AQ_ANT_REACH_test"
        for good,other,token in (("cex","proven","AQ_NON_VACUOUS"),("proven","cex","AQ_VACUOUS")):
            with self.subTest(stage="vacuity",good=good):
                result=self.execute(self.aq_script("vacuity",witness,self.bound),self.inventory(witness,good,other))
                self.assertIn("\n"+token+"\n",result.stdout)

    def test_step_b_duplicate_inventory_and_subset_use_same_bound_scope(self):
        config,rtl,source,binds,includes,defines=self.inputs((self.label,"loop_other"))
        config["target_scope"]=self.bound
        statuses={**self.inventory(),**self.inventory("loop_other")}
        syntax=syntax_tcl(config,rtl,source,binds,includes,defines,2)
        result=self.execute(syntax,statuses)
        self.assertIn("FH_SYNTAX_PASS",result.stdout)
        measured=self.execute(make_tcl(config,rtl,source,binds,includes,defines,expected=[self.label]),statuses)
        self.assertEqual(parse_log(measured.stdout,[self.label]),{self.label:"PROVEN"})
        self.assertNotIn(f"FH_PROPERTY\t<embedded>::{self.embedded}",measured.stdout)

    def test_missing_or_ambiguous_bound_scope_never_falls_back(self):
        script=self.aq_script("syntax",self.label,self.bound)
        inventories=[{f"<embedded>::{self.embedded}.{self.label}":"proven"},
                     {f"w1.{self.bound}.{self.label}":"proven",f"w2.{self.bound}.{self.label}":"proven"},
                     {f"evil{self.bound}.{self.label}":"proven"}]
        for inventory in inventories:
            with self.subTest(inventory=inventory):
                result=self.execute(script,inventory)
                self.assertIn("AQ_TARGET_ERROR",result.stdout)
                self.assertNotIn("AQ_SYNTAX_PASS",result.stdout)

    def test_parameterized_bind_scope_does_not_change_template_or_assumptions(self):
        bind=self.root/"bind"/"toy_bind.sv"
        bind.write_text("bind toy toy_assertions #(.W($clog2(4)), .P((2+3))) selected_instance (.*);\n")
        template=self.root/self.unit["template"]
        template.write_text(template.read_text()+"\nbind toy toy_assertions i_assertions (.*);\n")
        before=template.read_bytes()
        self.assertEqual(target_scope(self.root,self.unit),"toy.selected_instance")
        self.assertEqual(template.read_bytes(),before)

    def test_log_diagnostics_ignore_echoed_tcl_error_definitions(self):
        actual="AQ_TARGET_ERROR target assertion mapping failed: label=x matches=2 names=a b"
        text='error "AQ_SETUP_ERROR: Required file does not exist: $path"\n'
        text+='puts "AQ_COMPILE_ERROR $compile_error"\n'
        text+='error "target assertion mapping failed: label=$label"\n'+actual+'\nAQ_SYNTAX_FAIL\n'
        self.assertEqual(runtime_diagnostics(text),[actual,"AQ_SYNTAX_FAIL"])

    def test_setup_failure_stops_before_another_model_or_llm_repair(self):
        config=self.root/"gen_assertions"/"config.json"
        save(config,self.config)
        fake,counter=self.fake_request()
        inventory={"w1.toy.check_i.loop_b001_t01_a001":"proven","w2.toy.check_i.loop_b001_t01_a001":"proven"}
        terminal=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY","LOOP_TEST_PROPERTIES_JSON":json.dumps(inventory)}),patch.object(Client,"request",side_effect=fake),redirect_stdout(terminal):
            self.assertEqual(run.main(argv=[str(self.root),"--config",str(config),"--experiment","setup_failure"]),1)
        self.assertEqual(counter["generation"],1)
        self.assertEqual(counter["spec"],0)
        self.assertIn("STOP (fixed setup/target error)",terminal.getvalue())
        self.assertNotIn("back to LLM",terminal.getvalue())
        self.assertNotIn("PASS -> Step B",terminal.getvalue())
        self.assertIn("scope=toy.check_i matches=2",terminal.getvalue())

class FaultTests(Fixture):
    def test_grouped_three_way_split_and_family_holdout(self):
        mutants=[{"id":f"F{i}_{j}_{k}","family":f"F{i}","replace":"toy.sv","line":j} for i in (1,2) for j in range(8) for k in range(2)]
        parts=partition(mutants,1,.25)
        self.assertEqual(sum(map(len,parts)),len(mutants))
        sites=[{(m["replace"],m["line"]) for m in p} for p in parts]
        self.assertFalse(sites[0]&sites[1] or sites[0]&sites[2] or sites[1]&sites[2])
        dev,val,test=partition(mutants,1,.25,"F2")
        self.assertEqual({m["id"] for m in test},{m["id"] for m in mutants})
        self.assertEqual(dev+val,[])  # Every F1 alternative shares a sealed F2 source site.
        self.assertTrue(all(m["family"]!="F2" for m in dev+val))

    def test_unknown_bounds_and_exact_detected_set(self):
        props=[{"label":"a"},{"label":"b"}]
        mutants=[{"id":"m1"},{"id":"m2"},{"id":"m3"}]
        statuses=[["CEX","UNKNOWN","PROVEN"],["CEX","PROVEN","PROVEN"]]
        rows=[{"assertion_id":p["label"],"mutant_id":m["id"],"status":statuses[i][j]} for i,p in enumerate(props) for j,m in enumerate(mutants)]
        result=score_rows(props,mutants,rows)
        self.assertAlmostEqual(result["detection_lower_percent"],100/3)
        self.assertAlmostEqual(result["detection_upper_percent"],200/3)
        self.assertEqual(result["selected_count"],1)
        self.assertTrue(result["preserves_detected_set"])
        with self.assertRaises(ValueError): score_rows(props,mutants,rows[:-1])

    def test_no_test_fault_in_feedback(self):
        pool={"dev_ids":["d"],"test_ids":["t"],"mutants":[{"id":x,"family":"F1","line":1,"operator":"x","before":x,"after":"z"} for x in ["d","t"]]}
        result=feedback(pool,{"undetected_ids":["d","t"],"detection_lower_percent":0,"unknown_only_count":0})
        self.assertEqual([x["before"] for x in result["missed_examples"]],["d"])

class AccountingTests(Fixture):
    def test_api_cache_usage_and_checker_cost_attribution(self):
        fake,count=self.fake_request()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake):
            client=Client(self.config,self.root/"gen_assertions"/"api")
            client.preflight(["qwen","gpt"])
            messages=[{"role":"system","content":"Generate"}]
            context={"generator_model":"qwen","unit":"toy","round":1}
            client.call("qwen","generation",messages,context)
            client.call("qwen","generation",messages,context)
            client.call("gpt","spec_check",[{"role":"system","content":"Check"}],context)
        self.assertEqual(count,{"generation":1,"spec":1})
        records=client.records()
        self.assertEqual(len(records),2)
        self.assertEqual(totals(records)["total_tokens"],240)
        self.assertEqual(totals(records)["missing_usage_calls"],0)
        self.assertAlmostEqual(totals(records)["estimated_cost_usd"],.00028)
        self.assertEqual(records[1]["generator_model"],"qwen")
        self.assertEqual(records[1]["api_model_key"],"gpt")
        self.assertNotIn("OFFLINE_FAKE_KEY",client.ledger.read_text())
        import csv
        with (client.output/"cost_by_generator.csv").open(encoding="utf-8-sig") as f:
            grouped=list(csv.DictReader(f))
        self.assertEqual(len(grouped),1)
        self.assertAlmostEqual(float(grouped[0]["generation_reported_cost_usd"]),.00014)
        self.assertAlmostEqual(float(grouped[0]["gpt_checker_reported_cost_usd"]),.00014)

    def test_missing_usage_not_assumed_free(self):
        total=totals([{"response_received":True}])
        self.assertEqual(total["missing_usage_calls"],1)
        self.assertEqual(total["reported_cost_missing_calls"],1)

class CurlTests(Fixture):
    def client(self):
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}):
            return Client(self.config,self.root/"gen_assertions"/"curl_api")

    def test_post_uses_previous_curl_method_without_key_in_arguments(self):
        client=self.client()
        completed=SimpleNamespace(returncode=0,stdout='{"ok":true}\n200',stderr="")
        with patch("looplib.llm.subprocess.run",return_value=completed) as call:
            self.assertEqual(client.request("/chat/completions",{"model":"fixture"}),{"ok":True})
        args=call.call_args.args[0]
        self.assertEqual(args[0],"curl")
        self.assertIn("--write-out",args)
        self.assertIn("--config",args)
        self.assertIn("-d",args)
        self.assertNotIn("OFFLINE_FAKE_KEY"," ".join(args))
        self.assertIn("Authorization: Bearer OFFLINE_FAKE_KEY",call.call_args.kwargs["input"])
        self.assertNotIn("--insecure",args)
        self.assertNotIn("-k",args)
        self.assertEqual(call.call_count,1)
        # Parse the exact header config with real curl, without making a request.
        parsed=subprocess.run(["curl","--config","-","--version"],input=call.call_args.kwargs["input"],capture_output=True,text=True,check=False)
        self.assertEqual(parsed.returncode,0,parsed.stderr)

    def test_catalog_get_also_uses_curl(self):
        client=self.client()
        with patch("looplib.llm.subprocess.run",return_value=SimpleNamespace(returncode=0,stdout='{"data":[]}\n200',stderr="")) as call:
            self.assertEqual(client.request("/models"),{"data":[]})
        self.assertNotIn("-d",call.call_args.args[0])

    def test_http_and_certificate_failures_are_clear_and_not_retried(self):
        client=self.client()
        failures=[(SimpleNamespace(returncode=0,stdout='{"error":"bad"}\n401',stderr=""),"HTTP 401"),
                  (SimpleNamespace(returncode=60,stdout='',stderr="certificate failed OFFLINE_FAKE_KEY"),"curl failed \\(60\\)"),
                  (SimpleNamespace(returncode=0,stdout='bad\n200',stderr=""),"non-JSON"),
                  (SimpleNamespace(returncode=0,stdout='{}',stderr=""),"HTTP status")]
        for response,message in failures:
            with self.subTest(message=message),patch("looplib.llm.subprocess.run",return_value=response) as call:
                with self.assertRaisesRegex(RuntimeError,message) as error: client.request("/models")
                self.assertNotIn("OFFLINE_FAKE_KEY",str(error.exception))
                self.assertEqual(call.call_count,1)

    def test_timeout_has_no_implicit_paid_retry(self):
        client=self.client()
        with patch("looplib.llm.subprocess.run",side_effect=subprocess.TimeoutExpired("curl",1)) as call:
            with self.assertRaisesRegex(RuntimeError,"not automatically retried"): client.request("/chat/completions",{})
        self.assertEqual(call.call_count,1)

    def test_previous_supported_payload_policy(self):
        client=self.client()
        fake,_=self.fake_request()
        with patch.object(Client,"request",side_effect=fake):
            client.preflight(["gpt","qwen"])
        for entry in client.catalog.values(): entry["supported_parameters"]+=["reasoning"]
        captured=[]
        def capture(path,body=None):
            captured.append(body)
            return fake(path,body)
        self.config["temperature"]=0
        with patch.object(Client,"request",side_effect=capture):
            for model in ("gpt","qwen"):
                client.call(model,"generation",[{"role":"system","content":"Generate"}],{"generator_model":model,"unit":"toy","round":1})
        self.assertNotIn("temperature",captured[0])
        self.assertEqual(captured[1]["temperature"],0)
        for body in captured:
            self.assertNotIn("reasoning",body)
            self.assertEqual(body["provider"],{"require_parameters":True})
            self.assertTrue(body["usage"]["include"])

class ReportingTests(Fixture):
    def test_legacy_generation_budgets_are_not_relabelled_as_step_b(self):
        experiment=self.root/"gen_assertions"/"ablation"/"legacy"
        save(experiment/"qwen"/"run_01"/"units"/"toy"/"budgets"/"budget_01"/"summary.json",
             {"budget":1,"rounds_used":1,"qualified_count":0})
        existing=experiment/"comparison.html"
        existing.write_text("original generation-budget report")
        self.assertEqual(report(experiment),[])
        self.assertEqual(existing.read_text(),"original generation-budget report")

class LiveTerminalTests(Fixture):
    def test_progress_is_forwarded_before_child_finishes(self):
        ack=self.root/"gen_assertions"/"ack"
        script="import pathlib,time,sys; p=pathlib.Path(sys.argv[1]); print('gate result',flush=True); "
        script+="exec(\"for i in range(50):\\n if p.exists(): break\\n time.sleep(.05)\"); sys.exit(0 if p.exists() else 7)"
        received=[]
        def progress(line):
            received.append(line)
            ack.write_text("received while child is running")
        rc=command([sys.executable,"-u","-c",script,ack],self.root,self.root/"gen_assertions"/"live.log",on_line=progress)
        self.assertEqual(rc,0)
        self.assertEqual(received,["gate result"])

class CandidateCountTests(Fixture):
    def test_more_than_six_initial_and_repair_blocks_reach_every_gate_and_keep_counts(self):
        lock=EnvironmentLock(self.root,self.setup)
        expressions=[f"req |-> ##{i} ack" for i in range(1,10)]
        for repairing in (False,True):
            with self.subTest(repairing=repairing):
                directory=self.root/"gen_assertions"/("repair_many" if repairing else "initial_many")
                catalog,_=self.fake_request()
                requests=[]; stages=[]; spec_calls=[]
                def request(path,body=None):
                    if path=="/models": return catalog(path,body)
                    if body["messages"][0]["content"].startswith("Generate"):
                        requests.append(body)
                        blocks=[]
                        for i,expression in enumerate(expressions,1):
                            block_id=f"b001_candidate_{i:03d}" if repairing else f"candidate_{i:03d}"
                            blocks.append(f"BEGIN_ASSERTION {block_id}\n{expression}\nEND_ASSERTION")
                        content="\n".join(blocks)
                    else:
                        spec_calls.append(body)
                        content=json.dumps({"verdict":"PASS","reason":"fixture"})
                    return {"id":"many-blocks-fixture","choices":[{"message":{"content":content},"finish_reason":"stop"}],
                            "usage":{"prompt_tokens":100,"completion_tokens":200,"total_tokens":300,"cost":.0005}}
                def qualify_fake(dataset,unit,props,path,settings,*,stage):
                    stages.append((stage,len(props)))
                    return [{**p,"stage":stage,"status":"PASS","passed":True} for p in props]
                pending=[{"repair_id":f"b001_candidate_{i:03d}","expression":f"req |-> ##{i+40} ack",
                          "failed_stage":"syntax","question":"fixture compile error"} for i in range(1,10)] if repairing else []
                feedback={"repair_questions":pending}
                with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=request),patch("looplib.engine.qualify",side_effect=qualify_fake),redirect_stdout(io.StringIO()):
                    client=Client(self.config,directory/"api"); client.preflight(["qwen","gpt"])
                    result=generation_attempt(self.root,self.unit,"qwen",self.config,client,"spec","rtl","checker",[],feedback,
                        {"round":1,"step_a_attempt":2 if repairing else 1},directory,set(),lock)
                self.assertEqual(len(requests),1)
                self.assertNotIn("Return at most",json.dumps(requests[0]["messages"]))
                self.assertEqual(stages,[("syntax",9),("fpv",9),("vacuity",9)])
                self.assertEqual(len(spec_calls),9)
                self.assertEqual(result["problems"],[])
                self.assertEqual(result["generated_count"],9)
                self.assertEqual(result["new_qualified_count"],9)
                self.assertEqual([p["expression"] for p in result["accepted"]],expressions)
                self.assertEqual(len(read(directory/"generation.json")["assertions"]),9)

    def test_legacy_count_cap_is_removed_from_restored_and_explicit_configs_without_data_loss(self):
        for explicit in (False,True):
            with self.subTest(explicit=explicit):
                name="explicit_count" if explicit else "restored_count"
                experiment=self.root/"gen_assertions"/"ablation"/name
                legacy={**self.config,"candidates_per_round":6}
                snapshot=experiment/"config_snapshot.json"; save(snapshot,legacy)
                raw=experiment/"api"/"responses"/"previous.json"; save(raw,{"retained":"original response"})
                original=raw.read_bytes()
                args=[str(self.root),"--model","qwen","--experiment",name]
                if explicit:
                    source=self.root/"gen_assertions"/"legacy_config.json"; save(source,legacy)
                    args.extend(["--config",str(source)])
                fake,_=self.fake_request(); terminal=io.StringIO()
                with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake),patch.object(run,"run_unit") as execute,redirect_stdout(terminal):
                    self.assertEqual(run.main(ablation=True,argv=args),0)
                self.assertNotIn("candidates_per_round",execute.call_args.args[4])
                self.assertNotIn("candidates_per_round",read(snapshot))
                self.assertEqual(read(next(experiment.glob("config_snapshot.previous_*.json")))["candidates_per_round"],6)
                self.assertEqual(raw.read_bytes(),original)
                self.assertIn("Removed legacy candidates_per_round",terminal.getvalue())


class PendingRepairTests(Fixture):
    def test_missing_failed_candidates_cannot_disappear(self):
        lock=EnvironmentLock(self.root,self.setup)
        replies=iter([
            {"assertions":[{"repair_id":"b001_candidate_002","expression":"req |-> ##3 ack"}]},
            {"assertions":[{"repair_id":"b001_candidate_001","expression":"req |-> ##4 ack"}]},
        ])
        def call(model,purpose,messages,context):
            return next(replies) if purpose=="generation" else {"verdict":"PASS"}
        client=SimpleNamespace(call=call)
        def qualify_fake(dataset,unit,props,directory,settings,*,stage):
            return [{**p,"stage":stage,"status":"PASS","passed":True} for p in props]
        failed=[{"repair_id":f"b001_candidate_{i:03d}","failed_stage":"syntax","expression":f"req |-> ##{i} ack"} for i in (1,2)]
        held=[{**p,"label":f"old{i}","checks":{}} for i,p in enumerate(failed)]
        accepted=[]; feedback={"repair_questions":failed,"held_candidates":held}; seen=set()
        with patch("looplib.engine.qualify",side_effect=qualify_fake),redirect_stdout(io.StringIO()):
            for attempt,expected in enumerate((1,0),1):
                result=generation_attempt(self.root,self.unit,"qwen",self.config,client,"spec","rtl","template",accepted,
                    feedback,{"round":1,"step_a_attempt":attempt},self.root/"gen_assertions"/f"attempt{attempt}",seen,lock)
                self.assertEqual(len(result["problems"]),expected)
                accepted=result["accepted"]
                feedback={"repair_questions":result["problems"],"held_candidates":result["held_candidates"]}
        self.assertEqual(len(accepted),2)
        self.assertEqual({p["repair_id"] for p in accepted},{"b001_candidate_001","b001_candidate_002"})

    def test_interruption_keeps_pending_and_never_enters_b(self):
        lock=EnvironmentLock(self.root,self.setup)
        directory=self.root/"gen_assertions"/"interrupted"
        def call(model,purpose,messages,context):
            if context["step_a_attempt"]==2: raise RuntimeError("offline transport interruption")
            return {"assertions":[{"expression":"req |-> ack"}]}
        client=SimpleNamespace(call=call)
        def qualify_fake(dataset,unit,props,directory,settings,*,stage):
            return [{**p,"stage":stage,"status":"CEX" if stage=="fpv" else "PASS","passed":stage!="fpv","failed_stage":"fpv","reason":"CEX"} for p in props]
        with patch("looplib.engine.qualify",side_effect=qualify_fake),patch("looplib.engine.build_pool") as pool_builder,patch("looplib.engine.measure") as measure,redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError,"offline transport"):
                run_unit(self.root,self.unit,"qwen",1,self.config,client,{"identity":"test","pending":True},lock,directory,[1])
        pool_builder.assert_not_called(); measure.assert_not_called()
        state=read(directory/"rounds"/"round_01"/"step_a_attempts"/"attempt_02"/"interrupted.json")
        self.assertEqual(state["step_b_rounds_completed"],0)
        self.assertEqual(state["pending_repairs"][0]["failed_stage"],"fpv")
        self.assertFalse(list(directory.rglob("round.json")))

class FormatProtocolTests(Fixture):
    def raw(self,content,finish="stop"):
        return {"id":"format-fixture","choices":[{"message":{"content":content},"finish_reason":finish}],
                "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":.00014}}

    def client(self):
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}):
            client=Client(self.config,self.root/"gen_assertions"/"format_api")
        client.catalog={v["id"]:{"supported_parameters":["response_format"],"pricing":{"prompt":".000001","completion":".000002"}} for v in self.config["models"].values()}
        return client

    def test_comments_and_trailing_commas_do_not_edit_strings(self):
        expression='req |-> (q == 4\'b0010)'
        value={"assertions":[{"expression":expression,"basis":"literal // and /* text */ ,} \\\""}]}
        raw=json.dumps(value).replace('"assertions":','/* outside comment */ "assertions":')
        raw=raw[:-1]+", // trailing comment\n}"
        self.assertEqual(json_content(raw),value)
        with self.assertRaises(ValueError): json_content('{"verdict":"FAIL","verdict":"PASS"}')
        with self.assertRaises(ValueError): json_content('{"expression":"unterminated}')

    def test_local_normalization_is_logged_without_another_paid_call(self):
        client=self.client(); terminal=io.StringIO()
        raw='{"assertions":[{"expression":"req |-> ack","basis":"ok",},],}'
        with patch.object(client,"request",return_value=self.raw(raw)) as request,redirect_stdout(terminal):
            value=call_json(client,"qwen","generation",[{"role":"system","content":"Generate"}],
                {"generator_model":"qwen","unit":"toy","round":1},self.root/"gen_assertions"/"format")
        self.assertEqual(value["assertions"][0]["expression"],"req |-> ack")
        self.assertEqual(request.call_count,1)
        self.assertIn("no extra API call",terminal.getvalue())
        self.assertEqual(len(list((client.output/"responses").glob("*.parse.json"))),1)

    def test_format_recovery_receives_original_reply_and_is_billed_to_generator(self):
        client=self.client()
        malformed='{"assertions":[{"expression":"req |-> ack","basis":"fixture", bad: 0}]}'
        repaired='{"assertions":[{"expression":"req |-> ack","basis":"fixture"}]}'
        directory=self.root/"gen_assertions"/"format"
        bodies=[]
        def request(path,body):
            bodies.append(body)
            return self.raw(malformed if len(bodies)==1 else repaired)
        context={"generator_model":"qwen","unit":"toy","round":1,"step_a_attempt":1}
        with patch.object(client,"request",side_effect=request),redirect_stdout(io.StringIO()):
            result=call_json(client,"qwen","generation",[{"role":"system","content":"Generate"}],context,directory)
        self.assertEqual(result,json.loads(repaired))
        self.assertEqual(len(bodies),2)
        self.assertIn(malformed,bodies[1]["messages"][-1]["content"])
        self.assertIn("ERROR:",bodies[1]["messages"][-1]["content"])
        self.assertEqual((directory/"invalid_response.txt").read_text(),malformed)
        usage=totals(client.records())
        self.assertEqual(usage["total_tokens"],240)
        self.assertEqual(usage["format_repair_calls"],1)
        import csv
        with (client.output/"cost_by_generator.csv").open(encoding="utf-8-sig") as handle: rows=list(csv.DictReader(handle))
        self.assertAlmostEqual(float(rows[0]["generation_reported_cost_usd"]),.00028)
        self.assertEqual(client.records()[1]["api_model_key"],"qwen")

    def test_persistently_invalid_format_stops_and_preserves_raw(self):
        client=self.client(); malformed='{"assertions":[{"expression":"req |-> ack", bad: 0}]}'
        directory=self.root/"gen_assertions"/"format"
        with patch.object(client,"request",return_value=self.raw(malformed)) as request,redirect_stdout(io.StringIO()):
            with self.assertRaises(ModelProtocolError):
                call_json(client,"qwen","generation",[{"role":"system","content":"Generate"}],{"generator_model":"qwen"},directory)
        self.assertEqual(request.call_count,2)
        self.assertTrue((directory/"protocol_error.json").is_file())
        self.assertTrue((directory/"invalid_format_repair.txt").is_file())

    def test_format_recovery_cannot_rewrite_an_expression_or_verdict(self):
        for purpose,bad,good in [
            ("generation",'{"assertions":[{"expression":"req |-> ack", bad: 0}]}','{"assertions":[{"expression":"1\'b1"}]}'),
            ("spec_check",'{"verdict":"FAIL", bad: 0}','{"verdict":"PASS"}')]:
            client=self.client()
            with self.subTest(purpose=purpose),patch.object(client,"request",side_effect=[self.raw(bad),self.raw(good)]),redirect_stdout(io.StringIO()):
                with self.assertRaises(ModelProtocolError):
                    call_json(client,"qwen",purpose,[{"role":"system","content":purpose}],{"generator_model":"qwen"},self.root/"gen_assertions"/purpose)

    def test_truncation_stops_without_a_blind_repair_call(self):
        client=self.client()
        with patch.object(client,"request",return_value=self.raw('{"assertions":[',"length")) as request:
            with self.assertRaises(ModelProtocolError):
                call_json(client,"qwen","generation",[{"role":"system","content":"Generate"}],{},self.root/"gen_assertions"/"format")
        self.assertEqual(request.call_count,1)

    def test_schema_is_used_only_when_advertised_and_has_repair_ids(self):
        client=self.client()
        client.catalog[self.config["models"]["gpt"]["id"]]["supported_parameters"].append("structured_outputs")
        payload=self.raw('{"assertions":[{"expression":"req |-> ack","basis":"fixture"}]}')
        with patch.object(client,"request",return_value=payload) as request:
            for model in ("qwen","gpt"):
                client.call(model,"generation",[{"role":"system","content":"Generate"}],{"repair_ids":["r1","r2"],"generator_model":model})
        qwen,gpt=[call.args[1] for call in request.call_args_list]
        self.assertEqual(qwen["response_format"],{"type":"json_object"})
        schema=gpt["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["schema"]["properties"]["assertions"]["items"]["properties"]["repair_id"]["enum"],["r1","r2"])

    def test_unchanged_failed_expression_is_visible_and_not_reproved(self):
        lock=EnvironmentLock(self.root,self.setup); directory=self.root/"gen_assertions"/"change"
        replies=iter([{"assertions":[{"expression":"req |-> ack"}]},
                      {"assertions":[{"repair_id":"b001_candidate_001","expression":"req |-> ack"}]},
                      {"assertions":[{"repair_id":"b001_candidate_001","expression":"req |-> ##1 ack"}]}])
        def call(model,purpose,messages,context): return next(replies) if purpose=="generation" else {"verdict":"PASS"}
        def qualify_fake(dataset,unit,props,directory,settings,*,stage):
            return [{**p,"stage":stage,"status":"CEX" if stage=="fpv" else "PASS",
                     "passed":stage!="fpv" or p["step_a_attempt"]>1,"failed_stage":"fpv","reason":"CEX"} for p in props]
        accepted=[]; feedback={}; terminal=io.StringIO()
        with patch("looplib.engine.qualify",side_effect=qualify_fake) as qualify_mock,redirect_stdout(terminal):
            for attempt in range(1,4):
                result=generation_attempt(self.root,self.unit,"qwen",self.config,SimpleNamespace(call=call),"spec","rtl","template",accepted,feedback,
                    {"round":1,"step_a_attempt":attempt},directory/str(attempt),set(),lock)
                accepted=result["accepted"]; feedback={"repair_questions":result["problems"],"held_candidates":result["held_candidates"]}
                if attempt==2:
                    self.assertEqual(result["problems"][0]["unchanged_repeats"],1)
                    self.assertIn("Revise it",result["problems"][0]["repair_instruction"])
        self.assertEqual(qualify_mock.call_count,5)
        self.assertEqual(len(accepted),1)
        self.assertIn("UNCHANGED",terminal.getvalue()); self.assertIn("CHANGED",terminal.getvalue())
        self.assertEqual(read(directory/"3"/"changes.json")[0]["before"],"req |-> ack")

    def test_bad_generation_protocol_returns_to_a_without_a_json_formatter(self):
        config=self.root/"gen_assertions"/"config.json"; save(config,self.config)
        fake,_=self.fake_request(); calls=[]
        def request(path,body=None):
            if path=="/models": return fake(path,body)
            calls.append(body)
            if body["messages"][0]["content"].startswith("Generate"):
                text='{"assertions":[{"expression":"req |-> ack", bad: 0}]}' if len(calls)==1 else "BEGIN_ASSERTION b001_response\nreq |-> ack\nEND_ASSERTION"
                return self.raw(text)
            return fake(path,body)
        terminal=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=request),redirect_stdout(terminal):
            self.assertEqual(run.main(argv=[str(self.root),"--config",str(config),"--model","qwen","--rounds","1","--experiment","bad_json"]),0)
        self.assertEqual(len(calls),3)
        self.assertIn("back to model in Step A",terminal.getvalue())
        experiment=self.root/"gen_assertions"/"main"/"bad_json"
        self.assertEqual(len(list(experiment.rglob("round.json"))),1)
        self.assertEqual(len(list(experiment.rglob("invalid_response.txt"))),1)
        self.assertFalse(list(experiment.rglob("format_repair_prompt.json")))
        records=[json.loads(line) for line in (experiment/"api"/"usage.jsonl").read_text().splitlines()]
        self.assertEqual([r["purpose"] for r in records],["generation","generation","spec_check"])

class CapabilityAPITests(Fixture):
    def catalog(self):
        # Capability fields read from OpenRouter's public catalog on 2026-11-10.
        return {
            "qwen":{"supported_parameters":["temperature","response_format"],"reasoning":None},
            "deepseek":{"supported_parameters":["temperature","reasoning","response_format","structured_outputs"],
                        "reasoning":{"mandatory":False,"supported_efforts":["max","high","low","none"]}},
            "gpt":{"supported_parameters":["reasoning","response_format","structured_outputs"],
                   "reasoning":{"mandatory":False,"supported_efforts":["high","low","none"]}},
            "gemini":{"supported_parameters":["temperature","reasoning","response_format","structured_outputs"],
                      "reasoning":{"mandatory":True,"supported_efforts":["high","medium","low","minimal"]}},
        }

    def test_all_four_generators_use_blocks_without_a_universal_limit_or_json_mode(self):
        config=copy.deepcopy(self.config); config["max_output_tokens"]=None
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}): client=Client(config,self.root/"gen_assertions"/"capability_api")
        client.catalog={config["models"][key]["id"]:value for key,value in self.catalog().items()}
        bodies=[]
        def request(path,body):
            bodies.append(body)
            content='{"verdict":"PASS","question":"","reason":"fixture","evidence_quote":""}' if body["messages"][0]["content"]=="Check" else "BEGIN_ASSERTION candidate_001\nreq |-> (q == 4'b0010)\nEND_ASSERTION"
            return {"id":"capability-fixture","choices":[{"message":{"content":content},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":.00014}}
        with patch.object(client,"request",side_effect=request),redirect_stdout(io.StringIO()):
            for model in ("qwen","deepseek","gpt","gemini"):
                value=client.call(model,"generation",[{"role":"system","content":"Generate"}],{"generator_model":model,"output_protocol":"assertion_blocks"})
                self.assertEqual(value["assertions"][0]["expression"],"req |-> (q == 4'b0010)")
            client.call("gpt","spec_check",[{"role":"system","content":"Check"}],{})
        for body in bodies:
            self.assertNotIn("max_tokens",body)
        for body in bodies[:4]: self.assertNotIn("response_format",body)
        self.assertNotIn("reasoning",bodies[0])
        self.assertEqual(bodies[1]["reasoning"],{"enabled":False})
        self.assertNotIn("reasoning",bodies[2]); self.assertNotIn("reasoning",bodies[3])
        self.assertEqual(bodies[4]["response_format"]["type"],"json_schema")

    def test_block_parser_preserves_literals_ids_and_whole_batches(self):
        value="```sv\r\nBEGIN_ASSERTION slot_1\r\nreq |-> (q == 4'b0010)\r\nEND_ASSERTION\r\nBEGIN_ASSERTION slot_2\r\n!req |=> $stable(q)\r\nEND_ASSERTION\r\n```"
        parsed=assertion_blocks(value,["slot_1","slot_2"])
        self.assertEqual([p["repair_id"] for p in parsed["assertions"]],["slot_1","slot_2"])
        self.assertEqual(parsed["assertions"][0]["expression"],"req |-> (q == 4'b0010)")
        invalid=["BEGIN_ASSERTION slot_1\na\nEND_ASSERTION\nBEGIN_ASSERTION slot_2\nb",
                 "BEGIN_ASSERTION slot_1\na\nEND_ASSERTION\nBEGIN_ASSERTION slot_1\nb\nEND_ASSERTION",
                 "prose\nBEGIN_ASSERTION slot_1\na\nEND_ASSERTION",
                 "BEGIN_ASSERTION slot_1\na\nBEGIN_ASSERTION slot_2\nb\nEND_ASSERTION"]
        for text in invalid:
            with self.subTest(text=text),self.assertRaises(ValueError): assertion_blocks(text,["slot_1","slot_2"])
        with self.assertRaises(ValueError): assertion_blocks("BEGIN_ASSERTION wrong_id\na\nEND_ASSERTION",["slot_1"])

    def test_legacy_snapshot_migrates_without_user_token_flags(self):
        experiment=self.root/"gen_assertions"/"ablation"/"legacy_api"
        legacy=copy.deepcopy(self.config); legacy.pop("api_request_policy",None); legacy["max_output_tokens"]=16384
        legacy["models"]["deepseek"].pop("reasoning_mode",None)
        snapshot=experiment/"config_snapshot.json"; save(snapshot,legacy)
        raw=experiment/"api"/"responses"/"original-truncated.json"; save(raw,{"usage":{"completion_tokens":16384,"completion_tokens_details":{"reasoning_tokens":16384}}}); original=raw.read_bytes()
        fake,_=self.fake_request()
        args=[str(self.root),"--model","deepseek","--experiment","legacy_api"]
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake),patch.object(run,"run_unit") as execute,redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(ablation=True,argv=args),0)
            config=execute.call_args.args[4]
            self.assertIsNone(config["max_output_tokens"])
            self.assertEqual(config["models"]["deepseek"]["reasoning_mode"],"off")
        self.assertEqual(raw.read_bytes(),original)
        self.assertIsNone(read(snapshot)["max_output_tokens"])
        self.assertEqual(read(next(experiment.glob("config_snapshot.previous_*.json")))["max_output_tokens"],16384)

    def test_mandatory_reasoning_is_not_disabled_or_sent_to_a_nonreasoning_model(self):
        config=copy.deepcopy(self.config); config["max_output_tokens"]=None
        config["models"]["gemini"]["reasoning_mode"]="off"
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}): client=Client(config,self.root/"gen_assertions"/"mandatory_api")
        client.catalog={config["models"][key]["id"]:value for key,value in self.catalog().items()}
        with patch.object(client,"request") as request,self.assertRaisesRegex(ValueError,"requires reasoning"):
            client.call("gemini","generation",[{"role":"system","content":"Generate"}],{"output_protocol":"assertion_blocks"})
        request.assert_not_called()


class OutputBudgetTests(Fixture):
    def cut_response(self,content=""):
        return {"id":"truncated-fixture","choices":[{"message":{"content":content},"finish_reason":"length"}],
                "usage":{"prompt_tokens":100,"completion_tokens":4096,"total_tokens":4196,"cost":.001,
                         "completion_tokens_details":{"reasoning_tokens":4096}}}

    def test_truncation_diagnostics_and_cache_do_not_trigger_another_charge(self):
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}):
            client=Client(self.config,self.root/"gen_assertions"/"budget_api")
        messages=[{"role":"system","content":"Generate"}]; context={"generator_model":"deepseek","unit":"toy","round":1}
        terminal=io.StringIO()
        with patch.object(client,"request",return_value=self.cut_response()) as request,redirect_stdout(terminal):
            for _ in range(2):
                with self.assertRaisesRegex(ModelProtocolError,"model/provider did not return a complete answer"):
                    call_json(client,"deepseek","generation",messages,context,self.root/"gen_assertions"/"cut")
        self.assertEqual(request.call_count,1)
        self.assertEqual(len(client.records()),1)
        record=client.records()[0]
        self.assertEqual(record["finish_reason"],"length")
        self.assertEqual(record["max_output_tokens_requested"],4096)
        self.assertEqual(record["reasoning_tokens"],4096)
        self.assertEqual(totals(client.records())["format_repair_calls"],0)
        diagnostic=read(client.output/"responses"/(record["request_sha256"]+".incomplete.json"))
        self.assertEqual(diagnostic["visible_content_characters"],0)
        self.assertEqual(diagnostic["reasoning_tokens"],4096)
        self.assertFalse(diagnostic["auto_retry"])
        self.assertFalse(diagnostic["partial_assertions_accepted"])
        self.assertIn("source=cache",terminal.getvalue())
        self.assertIn("completion_tokens=4096 reasoning_tokens=4096",terminal.getvalue())

    def test_valid_json_marked_length_is_still_rejected_and_missing_usage_is_unknown(self):
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}):
            client=Client(self.config,self.root/"gen_assertions"/"length_api")
        raw=self.cut_response(json.dumps({"assertions":[{"expression":"req |-> ack"}]}))
        raw["usage"].pop("completion_tokens_details")
        with patch.object(client,"request",return_value=raw) as request,redirect_stdout(io.StringIO()):
            with self.assertRaises(ModelProtocolError):
                call_json(client,"deepseek","generation",[{"role":"system","content":"Generate"}],{},self.root/"gen_assertions"/"valid_but_cut")
        self.assertEqual(request.call_count,1)
        diagnostic=read(next((client.output/"responses").glob("*.incomplete.json")))
        self.assertIsNone(diagnostic["reasoning_tokens"])
        self.assertGreater(diagnostic["visible_content_characters"],0)

    def test_cli_budget_overrides_saved_config_and_persists_without_deleting_data(self):
        experiment=self.root/"gen_assertions"/"ablation"/"budget_override"
        snapshot=experiment/"config_snapshot.json"
        stored={**self.config,"proof_timeout_seconds":9}; save(snapshot,stored)
        old=experiment/"api"/"responses"/"old-truncated.json"; save(old,self.cut_response()); original=old.read_bytes()
        fake,_=self.fake_request()
        args=[str(self.root),"--model","deepseek","--experiment","budget_override","--repeats","1"]
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake),patch.object(run,"run_unit") as execute,redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(ablation=True,argv=args+["--max-output-tokens","16384"]),0)
            effective=execute.call_args.args[4]
            self.assertEqual(effective["max_output_tokens"],16384)
            self.assertEqual(effective["proof_timeout_seconds"],9)
            self.assertEqual(execute.call_args.args[2],"deepseek")
            self.assertEqual(read(snapshot)["max_output_tokens"],16384)
            self.assertEqual(run.main(ablation=True,argv=args),0)
            self.assertEqual(execute.call_args.args[4]["max_output_tokens"],16384)
        self.assertEqual(old.read_bytes(),original)
        self.assertEqual(read(next(experiment.glob("config_snapshot.previous_*.json")))["max_output_tokens"],4096)

    def test_invalid_budget_stops_before_setup_or_api(self):
        for value in ("0","-1"):
            with self.subTest(value=value),patch.object(run,"init_dataset") as setup,patch.object(Client,"request") as request,redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(ValueError,"max_output_tokens must be a positive integer"):
                    run.main(argv=[str(self.root),"--max-output-tokens",value])
                setup.assert_not_called(); request.assert_not_called()

    def test_truncated_run_resumes_with_larger_budget_and_preserves_paid_history(self):
        config=self.root/"gen_assertions"/"config.json"; save(config,self.config)
        fake,counter=self.fake_request(); bodies=[]
        def request(path,body=None):
            if path=="/models":
                result=fake(path,body)
                for model in result["data"]: model["supported_parameters"].append("reasoning")
                return result
            bodies.append(body)
            if body["messages"][0]["content"].startswith("Generate") and body["max_tokens"]==4096:
                return self.cut_response()
            return fake(path,body)
        args=[str(self.root),"--config",str(config),"--model","deepseek","--rounds","1","--experiment","recover_cut","--step-a-max-attempts","1"]
        experiment=self.root/"gen_assertions"/"main"/"recover_cut"; terminal=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=request),redirect_stdout(terminal):
            self.assertEqual(run.main(argv=args),1)
            self.assertFalse(list(experiment.rglob("round.json")))
            first_record=json.loads((experiment/"api"/"usage.jsonl").read_text().splitlines()[0])
            old=experiment/"api"/"responses"/(first_record["request_sha256"]+".json"); original=old.read_bytes()
            fixed=args+["--max-output-tokens","16384"]
            self.assertEqual(run.main(argv=fixed),0)
            self.assertEqual(run.main(argv=fixed),0)  # compatible resume: no paid duplicate
        self.assertEqual(counter,{"generation":1,"spec":1})
        self.assertEqual([body["max_tokens"] for body in bodies],[4096,16384,16384])
        self.assertEqual(bodies[0]["reasoning"],{"enabled":False})
        self.assertEqual(bodies[1]["reasoning"],{"enabled":False})
        self.assertNotIn("reasoning",bodies[2])
        records=[json.loads(line) for line in (experiment/"api"/"usage.jsonl").read_text().splitlines()]
        self.assertEqual(len(records),3)
        self.assertEqual(old.read_bytes(),original)
        row=read(experiment/"comparison.json")["runs"][0]
        self.assertEqual(row["qualified_count"],1)
        self.assertEqual(row["generation_calls_used"],2)
        self.assertEqual(row["step_b_rounds_completed"],1)
        self.assertEqual(row["recovery_prior_api_calls"],1)
        self.assertEqual(row["total_tokens"],4436)
        self.assertAlmostEqual(row["reported_cost_usd"],.00128)
        self.assertEqual(row["format_repair_calls"],0)
        self.assertEqual(len(list(experiment.glob("deepseek/run_01/units/toy/_history/*"))),1)


class BatchBarrierTests(Fixture):
    def test_every_gate_stops_at_first_failure_and_blocks_all_later_gates(self):
        lock=EnvironmentLock(self.root,self.setup)
        events=self.root/"gen_assertions"/"barriers.jsonl"
        catalog,_=self.fake_request(); generations=[]; specs=[]
        repairs=["syntax_bad |-> ack","fpv_bad |-> ack","vac_bad |-> ack","req |-> ##1 ack","req |-> ##2 ack"]
        def event(value):
            with events.open("a") as handle: handle.write(json.dumps(value)+"\n")
        def request(path,body=None):
            if path=="/models": return catalog(path,body)
            if body["messages"][0]["content"].startswith("Generate"):
                generations.append(body)
                n=len(generations); event({"generation":n})
                item={"expression":repairs[n-1]}
                if n>1: item["repair_id"]="b001_candidate_001"
                payload={"assertions":[item]}
                if n==1: payload["assertions"].append({"expression":"req |-> ##9 ack"})
            else:
                specs.append(body); event({"spec":len(specs)})
                payload={"verdict":"FAIL" if len(specs)==1 else "PASS","question":"Correct the required latency.","reason":"fixture","evidence_quote":"ack shall follow"}
            return {"id":f"reply-{len(generations)}-{len(specs)}","choices":[{"message":{"content":json.dumps(payload)},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":0.00014}}
        directory=self.root/"gen_assertions"/"barrier_run"; terminal=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY","LOOP_TEST_EVENTS":str(events)}),patch.object(Client,"request",side_effect=request),redirect_stdout(terminal):
            client=Client(self.config,self.root/"gen_assertions"/"barrier_api"); client.preflight(["qwen","gpt"])
            rows=run_unit(self.root,self.unit,"qwen",1,self.config,client,{"identity":"pending","pending":True},lock,directory,[1],evaluate_test=False)
        timeline=[json.loads(line) for line in events.read_text().splitlines()]
        positions=[i for i,row in enumerate(timeline) if "generation" in row]
        chunks=[timeline[start+1:positions[n+1] if n+1<len(positions) else len(timeline)] for n,start in enumerate(positions)]
        self.assertEqual([row.get("stage") for row in chunks[0]],["syntax"])
        for n,blocked in ((0,{"fpv","vacuity"}),(1,{"vacuity"})):
            self.assertFalse(any(row.get("stage") in blocked or "spec" in row for row in chunks[n]))
        self.assertFalse(any("spec" in row for row in chunks[2]))
        self.assertEqual(sum("spec" in row for row in chunks[3]),1)  # stop before sibling GPT check
        self.assertEqual(len(generations),5); self.assertEqual(len(specs),3)
        self.assertEqual(rows[0]["qualified_count"],2); self.assertEqual(rows[0]["step_a_repairs_used"],4)
        # A changed expression starts at syntax even when only spec had failed.
        self.assertEqual([row["stage"] for row in chunks[4] if row.get("stage") in {"syntax","fpv","vacuity"}],["syntax","fpv","vacuity"])
        # The untouched sibling's successful syntax is retained, never dropped or retried.
        self.assertEqual(sum(row.get("stage")=="syntax" and "##9 ack" in row.get("source","") for row in timeline),1)
        for attempt,stage in ((1,"syntax"),(2,"fpv"),(3,"vacuity"),(4,"spec")):
            state=read(directory/"rounds"/"round_01"/"step_a_attempts"/f"attempt_{attempt:02d}"/"attempt.json")
            self.assertEqual(state["accepted"],[])
            self.assertEqual(len(state["held_candidates"]),2)
            self.assertEqual(state["problems"][0]["failed_stage"],stage)
        self.assertIn("syntax ALL PASS 2/2",terminal.getvalue())
        self.assertIn("spec STOP -> back to LLM",terminal.getvalue())
        self.assertIn("Correct the required latency.",generations[-1]["messages"][-1]["content"])

    def test_unchanged_replies_are_real_requests_with_failure_feedback(self):
        lock=EnvironmentLock(self.root,self.setup); catalog,_=self.fake_request()
        prompts=[]; events=self.root/"gen_assertions"/"no_progress.jsonl"; terminal=io.StringIO()
        def request(path,body=None):
            if path=="/models": return catalog(path,body)
            if body["messages"][0]["content"].startswith("Generate"):
                prompts.append(body); n=len(prompts)
                with events.open("a") as handle: handle.write(json.dumps({"generation":n})+"\n")
                item={"expression":"fpv_bad |-> ack" if n<4 else "req |-> ack"}
                if n>1: item["repair_id"]="b001_candidate_001"
                payload={"assertions":[item]}
                if n==1: payload["assertions"].append({"expression":"req |-> ##9 ack"})
            else: payload={"verdict":"PASS"}
            return {"id":f"live-reply-{len(prompts)}","choices":[{"message":{"content":json.dumps(payload)},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":0.00014}}
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY","LOOP_TEST_EVENTS":str(events)}),patch.object(Client,"request",side_effect=request),redirect_stdout(terminal):
            client=Client(self.config,self.root/"gen_assertions"/"no_progress_api"); client.preflight(["qwen","gpt"])
            rows=run_unit(self.root,self.unit,"qwen",1,self.config,client,{"identity":"pending","pending":True},lock,self.root/"gen_assertions"/"no_progress_run",[1],evaluate_test=False)
        records=[r for r in client.records() if r["purpose"]=="generation"]
        self.assertEqual(len(records),4); self.assertEqual(len({r["request_sha256"] for r in records}),4)
        for n,body in enumerate(prompts[1:],1):
            task=json.loads(body["messages"][-1]["content"].split("FEEDBACK:\n")[1])["repair_questions"][0]
            self.assertEqual(task["failed_stage"],"fpv"); self.assertEqual(task["expression"],"fpv_bad |-> ack")
            self.assertIn("AQ_FPV_CEX",task["diagnostic"])
            self.assertNotIn("unchanged_repeats",task)  # A counter alone is not new repair evidence.
            if n>1:
                prior=read(self.root/"gen_assertions/no_progress_run/rounds/round_01/step_a_attempts"/f"attempt_{n:02d}"/"attempt.json")
                self.assertEqual(prior["problems"][0]["unchanged_repeats"],n-1)
            snapshot=read(client.output/"requests"/(records[n]["request_sha256"]+".json"))
            self.assertEqual(snapshot["body"],body)
        timeline=[json.loads(line) for line in events.read_text().splitlines()]
        second=next(i for i,row in enumerate(timeline) if row.get("generation")==2)
        fourth=next(i for i,row in enumerate(timeline) if row.get("generation")==4)
        self.assertFalse(any("stage" in row for row in timeline[second:fourth]))
        self.assertEqual(terminal.getvalue().count("[NO_PROGRESS]"),2)
        self.assertIn("source=live request=",terminal.getvalue())
        self.assertEqual(rows[0]["qualified_count"],2)


class RepairMemoryTests(Fixture):
    def test_two_failed_versions_do_not_cycle_through_jasper_or_fill_prompt(self):
        lock=EnvironmentLock(self.root,self.setup)
        a="req |-> (ack && q == el0_r)"
        b="req |-> (ack && q == a)"
        c="req |-> ack"
        replies=iter([a,b,a,b,c]); prompts=[]; checked=[]
        def call(model,purpose,messages,context):
            if purpose!="generation": return {"verdict":"PASS"}
            prompts.append(messages)
            item={"expression":next(replies)}
            if len(prompts)>1: item["repair_id"]="b001_candidate_001"
            return {"assertions":[item]}
        def qualify_fake(dataset,unit,props,directory,settings,*,stage):
            rows=[]
            for p in props:
                checked.append((p["expression"],stage))
                failed=p["expression"]==a and stage=="syntax" or p["expression"]==b and stage=="fpv"
                rows.append({"label":p["label"],"expression":p["expression"],"stage":stage,"passed":not failed,
                             "status":"FAIL" if failed else "PASS","failed_stage":stage,
                             "diagnostic":"[ERROR (VERI-1128)] 'el0_r' is not declared" if p["expression"]==a else "AQ_FPV_CEX"})
                if failed: break
            return rows
        feedback={}; accepted=[]; terminal=io.StringIO(); directory=self.root/"gen_assertions"/"memory"
        with patch("looplib.engine.qualify",side_effect=qualify_fake),redirect_stdout(terminal):
            for attempt in range(1,6):
                result=generation_attempt(self.root,self.unit,"deepseek",self.config,SimpleNamespace(call=call),"spec","rtl","fixed checker",accepted,
                    feedback,{"round":1,"step_a_attempt":attempt},directory/str(attempt),set(),lock)
                accepted=result["accepted"]
                feedback={"repair_questions":result["problems"],"held_candidates":result["held_candidates"],"failure_history":result["failure_history"]}
                if attempt in (3,4):
                    self.assertTrue(result["problems"][0]["repeated_rejected_expression"])
                    self.assertEqual(result["decisions"],[])
        self.assertEqual(checked,[(a,"syntax"),(b,"syntax"),(b,"fpv"),(c,"syntax"),(c,"fpv"),(c,"vacuity")])
        self.assertEqual(len(accepted),1)
        self.assertEqual(len(result["failure_history"]["b001_candidate_001"]),2)
        self.assertEqual(terminal.getvalue().count("[KNOWN_FAILURE]"),2)
        # At B's failure, the earlier A expression is only on disk. The prompt
        # contains current B and the compiler fact, not a list of wrong answers.
        body=json.dumps(prompts[2]); self.assertNotIn(a,body)
        self.assertNotIn("failure_history",body); self.assertNotIn("previous_failures",body)
        task=json.loads(prompts[2][-1]["content"].split("FEEDBACK:\n")[1])
        self.assertEqual(task["checker_visibility"]["not_declared_in_checker"],["el0_r"])
        self.assertEqual(task["repair_questions"][0]["expression"],b)
        self.assertEqual(len(read(directory/"4"/"failure_history.json")["b001_candidate_001"]),2)

    def test_recovery_diagnostics_do_not_skip_current_revalidation(self):
        a="req |-> ack"; lock=EnvironmentLock(self.root,self.setup)
        history={"b001_candidate_001":[{"expression":a,"failed_stage":"fpv","diagnostic":"AQ_FPV_CEX","verified_for_current_run":False}]}
        def qualify_fake(dataset,unit,props,directory,settings,*,stage):
            return [{"label":p["label"],"expression":p["expression"],"stage":stage,"status":"PASS","passed":True} for p in props]
        client=SimpleNamespace(call=lambda *args:{"verdict":"PASS"})
        with patch("looplib.engine.qualify",side_effect=qualify_fake) as qualifier,redirect_stdout(io.StringIO()):
            result=generation_attempt(self.root,self.unit,"qwen",self.config,client,"spec","rtl","checker",[],
                {"failure_history":history,"repair_questions":[{"repair_id":"b001_candidate_001","expression":a,"failed_stage":"revalidation","needs_revalidation":True}]},
                {"round":1,"step_a_attempt":1},self.root/"gen_assertions"/"seed_check",set(),lock,
                seed_response={"assertions":[{"repair_id":"b001_candidate_001","expression":a}]})
        self.assertEqual(qualifier.call_count,3)
        self.assertEqual(len(result["accepted"]),1)


class RecoveryTests(Fixture):
    def context(self):
        return {"dataset":"toy","generator_model":"qwen","repeat":1,"unit":"toy"}

    def saved_run(self,completed=False):
        directory=self.root/"gen_assertions"/"main"/"same_name"/"qwen"/"run_01"/"units"/"toy"
        lock=EnvironmentLock(self.root,self.setup)
        save(directory/"recipe.json",{"sha256":"previous-version","context":self.context()})
        lock.write(directory/"fixed_environment.json")
        initial=directory/"rounds"/"round_01"/"step_a_attempts"/"attempt_01"/"generation.json"
        save(initial,{"assertions":[{"expression":"req |-> ack","basis":"initial"}]})
        checkpoint=directory/"rounds"/"round_01"/"step_a_attempts"/"attempt_03"/"attempt.json"
        save(checkpoint,{"accepted":[{"label":"old_pass","repair_id":"slot1","expression":"req |-> ##1 ack"}],
             "problems":[{"label":"old_fail","repair_id":"slot2","failed_stage":"fpv","expression":"req |-> ##2 ack"}],
             "generation_calls_used":3,"step_a_repairs_used":2,"cumulative_generated":4})
        (checkpoint.parent/"retained.log").write_bytes(b"original bytes\n")
        if completed:
            save(directory/"rounds"/"round_10"/"round.json",{"step_b_ran":True,"step_b_rounds_completed":10,
                 "accepted":[{"expression":"future_budget_only"}],"generation_calls_used":10,"cumulative_generated":12})
        return directory,lock,checkpoint

    def test_only_initial_prompt_gets_one_coverage_sentence(self):
        line="Aim to cover all major RTL behaviors and corner cases supported by the specification."
        args=("spec","rtl","checker","[]","{}")
        initial=generation_messages(*args,initial=True)
        repair=generation_messages(*args,initial=False)
        self.assertEqual(initial[0]["content"].count(line),1)
        self.assertNotIn(line,repair[0]["content"])

    def test_incompatible_pre_b1_run_retains_all_files_and_proposals(self):
        directory,lock,checkpoint=self.saved_run()
        before=checkpoint.read_bytes()
        old_report=directory.parents[3]/"comparison.html"
        old_report.write_bytes(b"original aggregate report")
        state=prepare_recovery(directory,"current",lock,self.context(),SimpleNamespace(records=lambda:[]),self.config)
        history=Path(state["history"])
        self.assertEqual((history/checkpoint.relative_to(directory)).read_bytes(),before)
        self.assertEqual((history/"_experiment_reports"/"comparison.html").read_bytes(),b"original aggregate report")
        self.assertEqual((history/checkpoint.parent.relative_to(directory)/"retained.log").read_bytes(),b"original bytes\n")
        self.assertEqual(read(history/"recipe.json")["sha256"],"previous-version")
        self.assertEqual({x["expression"] for x in state["candidates"]},{"req |-> ##1 ack","req |-> ##2 ack"})
        self.assertTrue(all("technical_pass" not in x and "spec_check" not in x for x in state["candidates"]))
        self.assertTrue((directory/"recovery_seed.json").is_file())
        self.assertEqual(state["failure_history"]["slot2"][0]["failed_stage"],"fpv")
        self.assertFalse(state["failure_history"]["slot2"][0]["verified_for_current_run"])

    def test_later_budget_assertions_cannot_leak_into_restarted_b1(self):
        directory,lock,_=self.saved_run(completed=True)
        state=prepare_recovery(directory,"current",lock,self.context(),SimpleNamespace(records=lambda:[]),self.config)
        self.assertEqual([x["expression"] for x in state["candidates"]],["req |-> ack"])
        self.assertEqual(state["strategy"],"revalidate_original_pre_B1_batch")
        self.assertTrue((Path(state["history"])/"rounds"/"round_10"/"round.json").is_file())
        self.assertEqual(state["failure_history"],{})

    def test_waiting_candidates_survive_another_tool_update(self):
        directory,lock,checkpoint=self.saved_run()
        previous=read(checkpoint)
        previous["held_candidates"]=[{"repair_id":"b001_candidate_003","label":"waiting_old",
                                      "expression":"req |-> ##3 ack","checks":{"syntax":"PASS"}}]
        save(checkpoint,previous); original=checkpoint.read_bytes()
        state=prepare_recovery(directory,"new-barrier-code",lock,self.context(),SimpleNamespace(records=lambda:[]),self.config)
        self.assertEqual({item["expression"] for item in state["candidates"]},
                         {"req |-> ##1 ack","req |-> ##2 ack","req |-> ##3 ack"})
        self.assertTrue(all("checks" not in item for item in state["candidates"]))
        self.assertEqual((Path(state["history"])/checkpoint.relative_to(directory)).read_bytes(),original)

    def test_interrupted_revalidation_seed_survives_a_second_update(self):
        directory,lock,_=self.saved_run()
        first=prepare_recovery(directory,"revision-one",lock,self.context(),SimpleNamespace(records=lambda:[]),self.config)
        # run_unit writes the new recipe/lock before starting revalidation.
        save(directory/"recipe.json",{"sha256":"revision-one","context":self.context()})
        lock.write(directory/"fixed_environment.json")
        original=(directory/"recovery_seed.json").read_bytes()
        second=prepare_recovery(directory,"revision-two",lock,self.context(),SimpleNamespace(records=lambda:[]),self.config)
        self.assertEqual([p["expression"] for p in first["candidates"]],[p["expression"] for p in second["candidates"]])
        for key in ("generation_calls_used","step_a_repairs_used","cumulative_generated"):
            self.assertEqual(second[key],first[key])
        self.assertEqual((Path(second["history"])/"recovery_seed.json").read_bytes(),original)

    def test_interrupted_archive_resumes_without_losing_old_files(self):
        import shutil
        directory,lock,checkpoint=self.saved_run()
        before=checkpoint.read_bytes(); real_move=shutil.move; moved=[]
        def interrupt(source,target):
            if moved: raise KeyboardInterrupt()
            result=real_move(source,target); moved.append(target); return result
        with patch("looplib.recovery.shutil.move",side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                prepare_recovery(directory,"current",lock,self.context(),SimpleNamespace(records=lambda:[]),self.config)
        self.assertTrue((directory/".recovery_pending.json").exists())
        state=recover_pending(directory)
        history=Path(state["history"])
        self.assertEqual((history/checkpoint.relative_to(directory)).read_bytes(),before)
        self.assertFalse((directory/".recovery_pending.json").exists())
        self.assertTrue((directory/"recovery_seed.json").exists())

    def test_environment_change_preserves_original_without_moving_it(self):
        directory,lock,_=self.saved_run()
        state=read(directory/"fixed_environment.json"); state["sha256"]="different"; save(directory/"fixed_environment.json",state)
        with self.assertRaises(EnvironmentSetupError):
            prepare_recovery(directory,"current",lock,self.context(),SimpleNamespace(records=lambda:[]),self.config)
        self.assertTrue((directory/"rounds").is_dir())
        self.assertFalse((directory/"_history").exists())

    def test_exact_prior_spec_body_is_reused_without_double_billing(self):
        fake,counter=self.fake_request()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake):
            client=Client(self.config,self.root/"gen_assertions"/"api")
            client.preflight(["gpt"])
            messages=[{"role":"system","content":"Check the same property"}]
            context={**self.context(),"round":1,"step_a_attempt":12,"assertion":"old_label"}
            client.call("gpt","spec_check",messages,context)
            # Simulate a legacy ledger without the new nested request-context field.
            record=client.records()[0]; record.pop("request_context"); record.pop("request_body_sha256")
            client.ledger.write_text(json.dumps(record)+"\n")
            replay={**context,"step_a_attempt":1,"reuse_cached_response":True}
            self.assertEqual(client.call("gpt","spec_check",messages,replay)["verdict"],"PASS")
            self.assertEqual(counter["spec"],1)
            self.assertEqual(len(client.records()),1)
            self.assertTrue((client.output/"cache_reuses.jsonl").is_file())
            changed=[{"role":"system","content":"A different specification check"}]
            client.call("gpt","spec_check",changed,{**replay,"step_a_attempt":2})
        self.assertEqual(counter["spec"],2)
        self.assertEqual(totals(client.records())["total_tokens"],240)

    def test_runtime_config_survives_toolkit_replacement(self):
        custom=copy.deepcopy(self.config); custom["proof_timeout_seconds"]=9
        path=self.root/"gen_assertions"/"custom_config.json"; save(path,custom)
        fake,_=self.fake_request()
        args=[str(self.root),"--model","qwen","--rounds","1","--experiment","settings"]
        toolkit=self.root.parent/"replacement_toolkit"; toolkit.mkdir()
        defaults=copy.deepcopy(self.config); defaults["jasper_executable"]="not-installed-new-default-jg"; defaults["proof_timeout_seconds"]=999
        save(toolkit/"config.json",defaults)
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake),patch.object(run,"run_unit") as execute,redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(argv=args+["--config",str(path)]),0)
            snapshot=self.root/"gen_assertions"/"main"/"settings"/"config_snapshot.json"
            self.assertEqual(read(snapshot)["proof_timeout_seconds"],9)
            self.assertNotIn("OFFLINE_FAKE_KEY",snapshot.read_text())
            with patch.object(run,"ROOT",toolkit):
                self.assertEqual(run.main(argv=args),0)
            self.assertEqual(execute.call_args.args[4]["proof_timeout_seconds"],9)
            self.assertEqual(execute.call_args.args[4]["jasper_executable"],str(FAKE))

    def test_same_experiment_after_tool_update_reuses_reply_and_rechecks(self):
        config=self.root/"gen_assertions"/"config.json"; save(config,self.config)
        fake,counter=self.fake_request()
        args=[str(self.root),"--config",str(config),"--model","qwen","--rounds","1","--experiment","same_name"]
        terminal=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake),redirect_stdout(terminal):
            self.assertEqual(run.main(argv=args),0)
            directory=self.root/"gen_assertions"/"main"/"same_name"/"qwen"/"run_01"/"units"/"toy"
            previous=read(directory/"recipe.json"); previous["sha256"]="old-tool-version"; save(directory/"recipe.json",previous)
            original=(directory/"rounds"/"round_01"/"round.json").read_bytes()
            self.assertEqual(run.main(argv=args),0)
            # Compatible repeat resumes checkpoints, without archiving again.
            self.assertEqual(run.main(argv=args),0)
        self.assertEqual(counter,{"generation":1,"spec":1})
        histories=list((directory/"_history").iterdir()); self.assertEqual(len(histories),1)
        self.assertEqual((histories[0]/"rounds"/"round_01"/"round.json").read_bytes(),original)
        row=read(directory/"summary.json")[0]
        self.assertEqual(row["qualified_count"],1)
        self.assertEqual(row["total_tokens"],240)
        self.assertTrue(row["recovered_from_history"])
        self.assertIn("no generation API call",terminal.getvalue())
        self.assertIn("no new API call",terminal.getvalue())

class AblationRunnerTests(Fixture):
    def test_plan_defaults_to_all_models_and_all_configured_prefixes_and_accepts_custom_budgets(self):
        config_path=self.root/"gen_assertions"/"plan_config.json"; save(config_path,self.config)
        base=[str(self.root),"--config",str(config_path),"--plan"]
        with patch.object(Client,"request") as request,redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(ablation=True,argv=base+["--experiment","default_plan"]),0)
            self.assertEqual(run.main(ablation=True,argv=base+["--experiment","custom_plan","--budgets","7,2,13"]),0)
        request.assert_not_called()
        default=read(self.root/"gen_assertions"/"ablation"/"default_plan"/"plan.json")
        self.assertEqual(set(default["models"]),{"qwen","deepseek","gpt","gemini"})
        self.assertEqual(default["budgets"],[1,3,5,10,15,20])
        self.assertEqual(default["max_step_b_rounds"],20)
        custom=read(self.root/"gen_assertions"/"ablation"/"custom_plan"/"plan.json")
        self.assertEqual(custom["budgets"],[2,7,13])
        self.assertEqual(custom["max_step_b_rounds"],13)

    def test_invalid_budgets_fail_before_dataset_setup_api_or_jasper(self):
        for text in ("", "1,0", "-1,2", "1,1", "1,,2", "1.5,2", "a,2"):
            with self.subTest(text=text),patch.object(run,"init_dataset") as setup,patch.object(Client,"request") as request,patch("sys.stderr",io.StringIO()):
                with self.assertRaises(SystemExit): run.main(ablation=True,argv=[str(self.root),"--budgets",text])
                setup.assert_not_called(); request.assert_not_called()
        config_path=self.root/"gen_assertions"/"invalid_config.json"
        for budgets in ([], [True,2], [0], [2,2], ["2"], [2.5], "1,2"):
            save(config_path,{**self.config,"ablation_budgets":budgets})
            with self.subTest(budgets=budgets),patch.object(run,"init_dataset") as setup,patch.object(Client,"request") as request:
                with self.assertRaises(ValueError): run.main(ablation=True,argv=[str(self.root),"--config",str(config_path)])
                setup.assert_not_called(); request.assert_not_called()

    def test_all_four_models_extend_one_trajectory_from_ten_to_twenty_and_report_exact_prefixes(self):
        # This is a stable-protocol prefix test. All invocations must use the
        # current original Step B mode, rather than switching from an explicit
        # equivalence config to the new ordinary-run syntax default halfway.
        self.config['fault_qualification']='syntax'
        aliases=["qwen","deepseek","gpt","gemini"]
        counters={key:0 for key in aliases}; spec_calls=[]; measurements=[]
        model_key={self.config["models"][key]["id"]:key for key in aliases}
        catalog,_=self.fake_request()
        def request(path,body=None):
            if path=="/models": return catalog(path,body)
            if body["messages"][0]["content"].startswith("Generate"):
                key=model_key[body["model"]]; counters[key]+=1
                latency=100*aliases.index(key)+counters[key]
                content=f"BEGIN_ASSERTION candidate_001\nreq |-> ##{latency} ack\nEND_ASSERTION"
            else:
                spec_calls.append(body)
                content=json.dumps({"verdict":"PASS","reason":"offline fixture"})
            return {"id":"prefix-fixture","choices":[{"message":{"content":content},"finish_reason":"stop"}],
                    "usage":{"prompt_tokens":100,"completion_tokens":20,"total_tokens":120,"cost":.00014}}
        mutants=[{"id":f"{part}_{i}","family":"F1","line":i,"operator":"fixture","before":"a","after":"b"}
                 for part in ("dev","validation","test") for i in range(1,21)]
        pool={"identity":"offline-prefix-pool","mutants":mutants,
              **{part+"_ids":[m["id"] for m in mutants if m["id"].startswith(part+"_")] for part in ("dev","validation","test")}}
        def qualify_fake(dataset,unit,props,directory,settings,*,stage):
            return [{**p,"stage":stage,"status":"PASS","passed":True} for p in props]
        def measure_fake(dataset,unit,props,pool,part,directory,settings,lock):
            self.assertNotEqual(part,"test")
            measurements.append((part,len(props)))
            selected=[m for m in pool["mutants"] if m["id"] in pool[part+"_ids"]]
            matrix=[{"assertion_id":p["label"],"mutant_id":m["id"],
                     "status":"CEX" if int(re.search(r"##(\d+)",p["expression"])[1])%100==int(m["id"].rsplit("_",1)[1]) else "PROVEN"}
                    for p in props for m in selected]
            metrics=score_rows(props,selected,matrix,settings.get("assertion_budget"))
            metrics.update({"partition":part,"status":"MEASURED"})
            save(Path(directory)/"metrics.json",metrics)
            return metrics
        config_path=self.root/"gen_assertions"/"prefix_config.json"; save(config_path,self.config)
        base=[str(self.root),"--experiment","all_prefixes","--repeats","1"]
        experiment=self.root/"gen_assertions"/"ablation"/"all_prefixes"; terminal=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=request),patch.object(run,"build_pool",return_value=pool),patch("looplib.engine.qualify",side_effect=qualify_fake),patch("looplib.engine.measure",side_effect=measure_fake),redirect_stdout(terminal):
            self.assertEqual(run.main(ablation=True,argv=base+["--config",str(config_path),"--budgets","1,2,3,5,10"]),0)
            self.assertEqual(counters,{key:10 for key in aliases})
            prefix_bytes={path:path.read_bytes() for path in experiment.glob("*/run_*/units/*/rounds/round_*/round.json")}
            self.assertEqual(run.main(ablation=True,argv=base+["--budgets","1,2,3,5,10,20"]),0)
            calls=len(spec_calls)
            self.assertEqual(run.main(ablation=True,argv=base),0)
        self.assertEqual(counters,{key:20 for key in aliases})
        self.assertEqual(len(spec_calls),80)
        self.assertEqual(calls,len(spec_calls))
        self.assertEqual(sum(part=="dev" for part,_ in measurements),80)
        self.assertFalse(list(experiment.glob("*/run_*/units/*/_history")))
        for path,original in prefix_bytes.items(): self.assertEqual(path.read_bytes(),original)
        rows=read(experiment/"comparison.json")["runs"]
        self.assertEqual(len(rows),24)
        for key in aliases:
            unit_dir=experiment/key/"run_01"/"units"/self.unit["name"]
            self.assertEqual(len(list((unit_dir/"rounds").glob("round_*/round.json"))),20)
            for budget in (1,2,3,5,10,20):
                frozen=read(unit_dir/"budgets"/f"budget_{budget:02d}"/"frozen_assertions.json")
                state=read(unit_dir/"rounds"/f"round_{budget:02d}"/"round.json")
                self.assertEqual(frozen,state["accepted"])
                row=next(r for r in rows if r["generator_model"]==key and r["budget"]==budget)
                self.assertEqual(row["qualified_count"],budget)
                self.assertEqual(row["generation_calls_used"],budget)
                self.assertEqual(row["total_tokens"],240*budget)
                self.assertEqual(row["validation_detected"],budget)
                self.assertEqual(row["validation_mutants"],20)
                self.assertEqual(row["validation_lower_percent"],100*budget/20)
                self.assertEqual(row["test_status"],"SEALED_NOT_EVALUATED")
        with (experiment/"effectiveness.csv").open(encoding="utf-8-sig") as handle: exported=list(csv.DictReader(handle))
        self.assertEqual(len(exported),24)
        self.assertEqual({int(row["budget"]) for row in exported},{1,2,3,5,10,20})
        self.assertIn("Step B effectiveness",(experiment/"comparison.html").read_text())
        self.assertIn("[RESULT] Step B effectiveness",terminal.getvalue())
        self.assertFalse(list(experiment.rglob("step_b_test")))

    def test_model_protocol_failure_is_recorded_and_other_models_still_run(self):
        config_path=self.root/"gen_assertions"/"failure_config.json"; save(config_path,self.config)
        fake,_=self.fake_request(); visited=[]
        def execute(dataset,unit,model,*args,**kwargs):
            visited.append(model)
            if model=="qwen": raise ModelProtocolError("offline truncated response")
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake),patch.object(run,"run_unit",side_effect=execute),redirect_stdout(io.StringIO()),patch("sys.stderr",io.StringIO()):
            self.assertEqual(run.main(ablation=True,argv=[str(self.root),"--config",str(config_path),"--experiment","one_failure"]),1)
        self.assertEqual(visited,["qwen","deepseek","gpt","gemini"])
        experiment=self.root/"gen_assertions"/"ablation"/"one_failure"
        failures=read(experiment/"failures.json")
        self.assertEqual(len(failures),1)
        self.assertEqual(failures[0]["model"],"qwen")
        self.assertIn("offline truncated response",(experiment/"comparison.html").read_text())

    def test_effectiveness_report_keeps_unknowns_and_missing_measurements_distinct(self):
        experiment=self.root/"gen_assertions"/"ablation"/"uncertainty"
        common={"dataset":"toy","generator_model":"deepseek","unit":"toy","repeat":1,"budget_type":"step_b_rounds",
                "qualified_count":1,"selected_count":1,"status":"COMPLETED"}
        rows=[{**common,"budget":2,"validation_detected":1,"validation_mutants":3,"validation_unknown_only":1,
               "validation_lower_percent":100/3,"validation_upper_percent":200/3},
              {**common,"budget":7,"validation_detected":0,"validation_mutants":0,"validation_unknown_only":0,
               "validation_lower_percent":None,"validation_upper_percent":None}]
        for row in rows:
            save(experiment/"deepseek"/"run_01"/"units"/"toy"/"budgets"/f"budget_{row['budget']:02d}"/"summary.json",row)
        report(experiment)
        with (experiment/"effectiveness.csv").open(encoding="utf-8-sig") as handle: values=list(csv.DictReader(handle))
        self.assertEqual(values[0]["validation_unknown_only"],"1")
        self.assertAlmostEqual(float(values[0]["validation_lower_percent"]),100/3)
        self.assertEqual(values[1]["validation_lower_percent"],"")
        means=read(experiment/"comparison.json")["averages"]
        self.assertIsNone(means[1]["validation_lower_percent_mean"])
        self.assertIsNone(means[0]["validation_lower_percent_sd"])


class IntegrationTests(Fixture):
    def test_full_main_and_resume_no_duplicate_paid_calls(self):
        config_path=self.root/"gen_assertions"/"test_config.json"
        save(config_path,self.config)
        fake,count=self.fake_request()
        events=self.root/"gen_assertions"/"execution_order.jsonl"
        def transport(path,body=None):
            if body is not None:
                event="generation" if body["messages"][0]["content"].startswith("Generate") else "spec_check"
                with events.open("a") as f: f.write(json.dumps({"event":event})+"\n")
            return fake(path,body)
        args=[str(self.root),"--config",str(config_path),"--model","qwen","--rounds","3"]
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY","LOOP_TEST_EVENTS":str(events)}),patch.object(Client,"request",side_effect=transport):
            self.assertEqual(run.main(argv=args),0)
            self.assertEqual(run.main(argv=args),0)
        self.assertEqual(count["generation"],3)
        out=self.root/"gen_assertions"/"main"/"default"
        rows=read(out/"comparison.json")["runs"]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["rounds_used"],3)
        self.assertEqual(rows[0]["qualified_count"],3)
        self.assertEqual(rows[0]["test_status"],"EVALUATED_AFTER_FREEZE")
        self.assertTrue((out/"comparison.html").exists())
        self.assertFalse((self.root/"aq_out").exists())
        timeline=[json.loads(line) for line in events.read_text().splitlines()]
        self.assertEqual(timeline[0]["event"],"generation")
        first=lambda key,value:next(i for i,row in enumerate(timeline) if row.get(key)==value)
        self.assertLess(first("event","generation"),first("stage","syntax"))
        self.assertLess(first("stage","vacuity"),first("event","spec_check"))
        self.assertLess(first("event","spec_check"),first("stage","faults"))
        self.assertFalse(list((self.root/"gen_assertions").rglob("fixed_probe.sv")))

    def test_failed_candidates_repair_before_any_step_b_pool(self):
        from looplib.faults import build_pool
        lock=EnvironmentLock(self.root,self.setup)
        pool=build_pool(self.root,self.unit,self.config,lock,plan_only=True)
        self.assertFalse(Path(pool["directory"]).exists())
        fake,counters=self.fake_request()
        generations=[]
        events=self.root/"gen_assertions"/"repair_order.jsonl"
        def request(path,body=None):
            response=fake(path,body)
            if body and body["messages"][0]["content"].startswith("Generate"):
                generations.append(body)
                with events.open("a") as f: f.write(json.dumps({"event":"generation","round":len(generations)})+"\n")
                if len(generations)<3:
                    payload=json.loads(response["choices"][0]["message"]["content"])
                    payload["assertions"][0]["expression"]="syntax_bad |-> ack"
                    response["choices"][0]["message"]["content"]=json.dumps(payload)
            return response
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY","LOOP_TEST_EVENTS":str(events)}),patch.object(Client,"request",side_effect=request):
            client=Client(self.config,self.root/"gen_assertions"/"repair_api")
            client.preflight(["qwen","gpt"])
            rows=run_unit(self.root,self.unit,"qwen",1,self.config,client,pool,lock,self.root/"gen_assertions"/"repair",[1,2,3],evaluate_test=False)
        self.assertEqual([r["qualified_count"] for r in rows],[1,2,3])
        self.assertEqual(counters["spec"],3)
        self.assertEqual([r["step_b_rounds_completed"] for r in rows],[1,2,3])
        self.assertEqual([r["generation_calls_used"] for r in rows],[3,4,5])
        self.assertTrue(all(r["step_a_repairs_used"]==2 for r in rows))
        self.assertEqual(rows[0]["total_tokens"],480)
        timeline=[json.loads(line) for line in events.read_text().splitlines()]
        first_b=next(i for i,x in enumerate(timeline) if x.get("stage")=="faults")
        third_generation=next(i for i,x in enumerate(timeline) if x.get("round")==3)
        self.assertGreater(first_b,third_generation)
        self.assertIn("syntax",json.dumps(generations[1]))

    def test_more_than_three_repairs_no_partial_bypass_and_terminal_logs(self):
        lock=EnvironmentLock(self.root,self.setup)
        pool={"identity":"offline-pending","pending":True,"heldout_family":None}
        config={**self.config,"step_a_max_repairs":1}  # Legacy cap cannot stop repair.
        fake,counters=self.fake_request()
        def request(path,body=None):
            response=fake(path,body)
            if body and body["messages"][0]["content"].startswith("Generate"):
                payload=json.loads(response["choices"][0]["message"]["content"])
                # First batch has one good and one failed candidate; keep the failure
                # pending through syntax, FPV and vacuity before allowing B.
                n=counters["generation"]
                if n==1:
                    payload["assertions"].append({"expression":"syntax_bad |-> ack"})
                elif n<=5:
                    payload["assertions"][0]["expression"]={2:"syntax_bad |-> ack",3:"fpv_bad |-> ack",4:"vac_bad |-> ack",5:"syntax_bad |-> ack"}[n]
                response["choices"][0]["message"]["content"]=json.dumps(payload)
            return response
        directory=self.root/"gen_assertions"/"unlimited"
        output=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=request),redirect_stdout(output):
            client=Client(config,self.root/"gen_assertions"/"unlimited_api")
            client.preflight(["qwen","gpt"])
            rows=run_unit(self.root,self.unit,"qwen",1,config,client,pool,lock,directory,[1],evaluate_test=False)
        self.assertEqual(counters["generation"],6)
        self.assertEqual(rows[0]["step_a_repairs_used"],5)
        self.assertEqual(rows[0]["step_b_rounds_completed"],1)
        self.assertEqual(rows[0]["qualified_count"],2)
        self.assertEqual(read(directory/"rounds"/"round_01"/"pending_repairs.json")["problems"],[])
        terminal=output.getvalue()
        for stage in ("syntax","fpv","vacuity"):
            self.assertRegex(terminal,stage+r" [A-Z_]+ -> back to LLM")
        self.assertIn("reason:",terminal)
        self.assertIn("log:",terminal)
        self.assertIn("PASS -> Step B 1/1; attempts=6 AQ=2",terminal)
        self.assertFalse(list(directory.rglob("rejected_after_repair_limit.json")))

    def test_ablation_prefixes_and_sealed_test(self):
        config_path=self.root/"gen_assertions"/"test_config.json"
        save(config_path,self.config)
        fake,count=self.fake_request()
        args=[str(self.root),"--config",str(config_path),"--model","deepseek"]
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=fake):
            self.assertEqual(run.main(ablation=True,argv=args),0)
        rows=read(self.root/"gen_assertions"/"ablation"/"default"/"comparison.json")["runs"]
        rows.sort(key=lambda r:r["budget"])
        budgets=self.config["ablation_budgets"]
        self.assertEqual([r["budget"] for r in rows],budgets)
        self.assertEqual([r["qualified_count"] for r in rows],budgets)
        self.assertTrue(all(r["test_status"]=="SEALED_NOT_EVALUATED" for r in rows))
        self.assertTrue(all(r["test_lower_percent"] is None for r in rows))
        self.assertEqual(count["generation"],max(budgets))
        self.assertEqual([r["total_tokens"] for r in rows],[240*budget for budget in budgets])
        self.assertFalse(list((self.root/"gen_assertions"/"ablation").rglob("step_b_test")))

    def test_spec_question_goes_back_to_generator(self):
        from looplib.faults import build_pool
        lock=EnvironmentLock(self.root,self.setup)
        pool=build_pool(self.root,self.unit,self.config,lock)
        fake,count=self.fake_request()
        prompts=[]
        def request(path,body=None):
            response=fake(path,body)
            if path!="/models" and body["messages"][0]["content"].startswith("Generate"):
                prompts.append(body["messages"])
            elif path!="/models" and count["spec"]==1:
                response["choices"][0]["message"]["content"]=json.dumps({"verdict":"FAIL","question":"Why is the required latency reversed?","reason":"fixture","evidence_quote":"ack shall follow"})
            return response
        terminal=io.StringIO()
        with patch.dict(os.environ,{"OPENROUTER_API_KEY":"OFFLINE_FAKE_KEY"}),patch.object(Client,"request",side_effect=request),redirect_stdout(terminal):
            client=Client(self.config,self.root/"gen_assertions"/"api")
            client.preflight(["gpt","qwen"])
            result=run_unit(self.root,self.unit,"qwen",1,self.config,client,pool,lock,self.root/"gen_assertions"/"question",[2])
        self.assertIn("Why is the required latency reversed?",json.dumps(prompts[1]))
        self.assertIn("spec FAIL -> back to LLM",terminal.getvalue())
        self.assertIn("Why is the required latency reversed?",terminal.getvalue())
        self.assertEqual(result[0]["qualified_count"],2)
        self.assertEqual(result[0]["step_a_repairs_used"],1)
        self.assertEqual(result[0]["generation_calls_used"],3)
        records=client.records()
        self.assertTrue(all(r["api_model_key"]=="gpt" for r in records if r["purpose"]=="spec_check"))

if __name__=="__main__": unittest.main()
