"""Bound Step A attempts without dropping pending checks or advancing Step B."""
from __future__ import annotations
import copy
from contextlib import redirect_stderr,redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from looplib.bootstrap import init_dataset
from looplib.common import EnvironmentLock,read,save
from looplib.inputs import load_existing,prepare_existing
from looplib.engine import run_unit,StepAAttemptLimit
from looplib.faults import score_rows
import run


class StepAAttemptLimitTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dataset=(Path(temporary.name)/"toy").resolve()
        for name in ("rtl","spec","bind","assertions"):
            (self.dataset/name).mkdir(parents=True)
        (self.dataset/"rtl/toy.sv").write_text("module toy(input logic clk,rst,req,output logic ack); assign ack=req; endmodule\n")
        (self.dataset/"spec/toy.md").write_text("Ack equals req; reset rst is active high.")
        (self.dataset/"bind/toy.sv").write_text("bind toy toy_assertions check_i (.*);\n")
        (self.dataset/"assertions/toy.sv").write_text(
            "module toy_assertions(input logic clk,rst,req,ack);\n"
            "fixed_environment: assume property (@(posedge clk) !rst);\n"
            "original_check: assert property (@(posedge clk) disable iff (rst) req |-> ack);\nendmodule\n")
        self.setup=prepare_existing(self.dataset,init_dataset(self.dataset))
        self.setup.pop("input_inventory",None)
        save(self.dataset/"gen_assertions/setup/dataset.json",self.setup)
        self.unit=self.setup["units"][0]
        self.original=load_existing(self.dataset,self.unit)[0]
        self.lock=EnvironmentLock(self.dataset,self.setup)
        self.directory=self.dataset/"gen_assertions/ablation/offline/deepseek/run_01/units/toy"
        self.config=read(ROOT/"config.json")
        self.config.update(stop_on_saturation=False,stop_on_repeated_request=False)
        self.config.update({"step_a_max_attempts":50,"input_mode":"existing_assertions","backend":"lexical"})
        self.pool={"identity":"OFFLINE_FIXED_POOL","mutants":[{"id":part+"_1","family":"F1","line":1,
                    "operator":"OFFLINE","before":"req","after":"!req"} for part in ("dev","validation","test")],
                   "dev_ids":["dev_1"],"validation_ids":["validation_1"],"test_ids":["test_1"]}
        self.client=SimpleNamespace(records=lambda:[])
        self.calls=[]
        self.measure_events=[]
        self.allow_step_b=False
        self.first_round_bytes=None

    def attempt_stub(self,*,success_at=None,first_round_passes=False):
        def attempt(dataset,unit,model,config,client,spec,rtl,template,accepted,
                    feedback,context,directory,seen,lock,*,seed_response=None):
            number=context["step_a_attempt"]
            round_number=context["round"]
            self.calls.append((round_number,number))
            if round_number==2 and number==1:
                self.first_round_bytes=(self.directory/"rounds/round_01/round.json").read_bytes()
            candidate=copy.deepcopy(self.original) if round_number==1 else {
                "repair_id":"b002_candidate_001","label":"added_check","expression":"req |=> ack"}
            passed=first_round_passes and round_number==1 or success_at is not None and number==success_at
            candidate["checks"]={stage:"PASS" for stage in ("syntax","fpv","vacuity","spec")} if passed else {}
            problem={**candidate,"failed_stage":"syntax","reason":"OFFLINE syntax failure",
                     "diagnostic":"AQ_COMPILE_ERROR controlled syntax failure"}
            return {"accepted":[*copy.deepcopy(accepted),candidate] if passed else copy.deepcopy(accepted),
                    "held_candidates":[] if passed else [candidate],"problems":[] if passed else [problem],
                    "failure_history":{} if passed else {candidate["repair_id"]:[problem]},
                    "decisions":[],"changes":[],"generated_count":0 if seed_response is not None else 1,
                    "generation_reused":seed_response is not None,"valid_candidate_count":1,
                    "new_qualified_count":1 if passed else 0,"spec_evidence_required":[]}
        return attempt

    def measure_stub(self,dataset,unit,properties,pool,partition,directory,config,lock):
        self.assertTrue(self.allow_step_b,"A failed Step A must not run Step B")
        self.assertTrue(all(all(prop["checks"].get(stage)=="PASS" for stage in ("syntax","fpv","vacuity","spec")) for prop in properties))
        self.measure_events.append(partition)
        mutants=[mutant for mutant in pool["mutants"] if mutant["id"] in pool[partition+"_ids"]]
        matrix=[{"assertion_id":prop["label"],"mutant_id":mutant["id"],"status":"CEX"}
                for prop in properties for mutant in mutants]
        metrics=score_rows(properties,mutants,matrix)
        return {**metrics,"partition":partition,"status":"MEASURED"}

    def invoke(self,attempt,budgets=None,*,revision="OFFLINE_FIXED_REVISION"):
        with patch("looplib.engine.generation_attempt",side_effect=attempt), \
             patch("looplib.engine.measure",side_effect=self.measure_stub), \
             patch("looplib.engine.implementation_digest",return_value=revision), \
             redirect_stdout(io.StringIO()):
            return run_unit(self.dataset,self.unit,"deepseek",1,self.config,self.client,self.pool,self.lock,
                            self.directory,budgets or [1],evaluate_test=False)

    def test_fifty_failures_checkpoint_and_resume_without_attempt_fifty_one_or_step_b(self):
        stub=self.attempt_stub()
        with self.assertRaises(StepAAttemptLimit): self.invoke(stub)
        self.assertEqual(self.calls,[(1,number) for number in range(1,51)])
        marker=read(self.directory/"step_a_limit.json")
        self.assertEqual(marker["status"],"STEP_A_LIMIT_REACHED")
        self.assertEqual(marker["step_a_max_attempts"],50)
        self.assertEqual(marker["step_a_attempts_used"],50)
        self.assertEqual(marker["step_b_round"],1)
        self.assertEqual(marker["step_b_rounds_completed"],0)
        self.assertEqual(marker["problems"][0]["repair_id"],"input_0001")
        self.assertIn("usage",marker)
        checkpoint=Path(marker["checkpoint"])
        self.assertTrue(checkpoint.is_file())
        state=read(checkpoint)
        self.assertEqual(state["step_a_attempt"],50)
        self.assertEqual(state["problems"][0]["failed_stage"],"syntax")
        pending=read(self.directory/"rounds/round_01/pending_repairs.json")
        self.assertEqual(pending["step_a_attempt"],50)
        self.assertEqual(pending["held_candidates"][0]["expression"],self.original["expression"])
        self.assertFalse((checkpoint.parent.parent/"attempt_51").exists())
        self.assertFalse((self.directory/"rounds/round_01/round.json").exists())
        self.assertEqual(self.measure_events,[])
        checkpoint_bytes=checkpoint.read_bytes()
        with self.assertRaises(StepAAttemptLimit): self.invoke(stub)
        self.assertEqual(len(self.calls),50,"A resumed capped run must not start a fifty-first attempt")
        self.assertEqual(checkpoint.read_bytes(),checkpoint_bytes)
        self.assertEqual(self.measure_events,[])
        self.lock.check()

    def test_success_on_fiftieth_attempt_is_allowed_to_enter_step_b(self):
        self.allow_step_b=True
        rows=self.invoke(self.attempt_stub(success_at=50))
        self.assertEqual(self.calls,[(1,number) for number in range(1,51)])
        self.assertEqual(rows[0]["qualified_count"],1)
        self.assertEqual(rows[0]["step_b_rounds_completed"],1)
        state=read(self.directory/"rounds/round_01/round.json")
        self.assertEqual(state["step_a_attempts_this_round"],50)
        self.assertTrue(state["step_a_all_passed"])
        self.assertTrue(state["step_b_ran"])
        self.assertEqual(self.measure_events,["dev","validation","validation"])
        self.assertFalse((self.directory/"step_a_limit.json").exists())
        self.assertFalse((self.directory/"rounds/round_01/step_a_attempts/attempt_51").exists())

    def test_control_only_upgrade_keeps_legacy_attempts_without_archiving_or_resetting(self):
        legacy_revision="a"*64
        current_revision="b"*64
        stub=self.attempt_stub()
        with self.assertRaises(StepAAttemptLimit): self.invoke(stub,revision=legacy_revision)
        original_recipe=read(self.directory/"recipe.json")["sha256"]
        checkpoint=self.directory/"rounds/round_01/step_a_attempts/attempt_50/attempt.json"
        checkpoint_bytes=checkpoint.read_bytes()
        def stored(path):
            if Path(path).resolve()==(ROOT/"MANIFEST.json").resolve():
                return {"resume_compatible_implementations":[legacy_revision]}
            return read(path)
        with patch("looplib.engine.read",side_effect=stored),self.assertRaises(StepAAttemptLimit):
            self.invoke(stub,revision=current_revision)
        self.assertEqual(len(self.calls),50,"Installing the attempt cap must not reset previously attempted work")
        self.assertEqual(read(self.directory/"recipe.json")["sha256"],original_recipe)
        self.assertEqual(checkpoint.read_bytes(),checkpoint_bytes)
        self.assertFalse((self.directory/"_history").exists())
        self.assertFalse((self.directory/"rounds/round_01/step_a_attempts/attempt_51").exists())
        self.assertEqual(self.measure_events,[])

    def test_later_round_limit_preserves_completed_round_and_pending_proposals(self):
        self.allow_step_b=True
        with self.assertRaises(StepAAttemptLimit): self.invoke(self.attempt_stub(first_round_passes=True),[1,2])
        self.assertEqual(self.calls,[(1,1)]+[(2,number) for number in range(1,51)])
        first=self.directory/"rounds/round_01/round.json"
        self.assertEqual(first.read_bytes(),self.first_round_bytes)
        self.assertEqual(read(first)["accepted"][0]["expression"],self.original["expression"])
        marker=read(self.directory/"step_a_limit.json")
        self.assertEqual(marker["step_b_round"],2)
        self.assertEqual(marker["step_b_rounds_completed"],1)
        self.assertEqual(marker["problems"][0]["repair_id"],"b002_candidate_001")
        pending=read(self.directory/"rounds/round_02/pending_repairs.json")
        self.assertEqual(pending["held_candidates"][0]["expression"],"req |=> ack")
        self.assertEqual(self.measure_events,["dev","validation","validation"])  # Completed B1 is now reported before B2 repairs.
        self.assertFalse((self.directory/"rounds/round_02/round.json").exists())
        self.assertFalse((self.directory/"rounds/round_02/step_a_attempts/attempt_51").exists())

    def cli(self,args,execute):
        output=io.StringIO()
        with patch.object(run,"Client") as client, \
             patch.object(run.shutil,"which",return_value="/offline/jg"), \
             patch.object(run,"build_pool",return_value=self.pool), \
             patch.object(run,"run_unit",side_effect=execute),redirect_stdout(output),redirect_stderr(output):
            status=run.main(ablation=True,argv=[str(self.dataset),*args])
        client.return_value.preflight.assert_called_once()
        return status,output.getvalue()

    def test_cli_moves_to_next_model_and_skips_failed_model_repeats(self):
        calls=[]
        def execute(dataset,unit,model,repeat,config,*args,**kwargs):
            calls.append((model,repeat))
            if model=="qwen": raise StepAAttemptLimit("50 Step A attempts exhausted")
            return []
        status,output=self.cli(["--experiment","all_models","--repeats","3"],execute)
        self.assertEqual(status,1,"The partial experiment remains visibly unsuccessful")
        self.assertEqual(calls,[("qwen",1)]+[(model,repeat) for model in ("deepseek","gpt","gemini") for repeat in (1,2,3)])
        self.assertIn("qwen",output)
        self.assertIn("deepseek",output)
        failures=read(self.dataset/"gen_assertions/ablation/all_models/failures.json")
        self.assertEqual(len(failures),1)
        self.assertEqual(failures[0]["model"],"qwen")
        self.assertTrue((self.dataset/"gen_assertions/ablation/all_models/qwen/run_01/units/toy/error.json").is_file())

    def test_restored_legacy_snapshot_gets_fifty_and_cli_override_takes_precedence(self):
        experiment=self.dataset/"gen_assertions/ablation/legacy"
        previous=copy.deepcopy(self.config)
        previous.pop("step_a_max_attempts",None)
        previous["step_a_max_repairs"]=3
        previous["proof_timeout_seconds"]=19
        save(experiment/"config_snapshot.json",previous)
        seen=[]
        def execute(dataset,unit,model,repeat,config,*args,**kwargs):
            seen.append(copy.deepcopy(config))
            return []
        args=["--experiment","legacy","--model","deepseek","--repeats","1"]
        status,_=self.cli(args,execute)
        self.assertEqual(status,0)
        self.assertEqual(seen[-1]["step_a_max_attempts"],50)
        self.assertEqual(seen[-1]["proof_timeout_seconds"],19)
        self.assertNotIn("step_a_max_repairs",seen[-1])
        self.assertEqual(read(experiment/"config_snapshot.json")["step_a_max_attempts"],50)
        self.assertTrue(list(experiment.glob("config_snapshot.previous_*.json")))
        status,_=self.cli(args+["--step-a-max-attempts","7"],execute)
        self.assertEqual(status,0)
        self.assertEqual(seen[-1]["step_a_max_attempts"],7)
        self.assertEqual(read(experiment/"config_snapshot.json")["step_a_max_attempts"],7)


if __name__=="__main__": unittest.main()
