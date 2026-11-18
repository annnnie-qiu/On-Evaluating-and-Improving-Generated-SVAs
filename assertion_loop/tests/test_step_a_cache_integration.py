"""Shared Step A cache regressions with production AQ/Tcl and offline Jasper.

fake_jasper.py executes generated production Tcl with stubbed Jasper commands.
These tests establish reuse and gate ordering, not real formal correctness.
"""
from __future__ import annotations
from collections import Counter
from contextlib import redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from looplib.bootstrap import init_dataset,target_scope
from looplib.common import EnvironmentLock,read,save
from looplib.inputs import load_existing,prepare_existing
from looplib.technical import qualify


class StepACacheIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dataset=Path(temporary.name)/"toy"
        for name in ("rtl","spec","bind","assertions"):
            (self.dataset/name).mkdir(parents=True)
        (self.dataset/"rtl/toy.sv").write_text(
            "module toy(input logic clk, rst, req, output logic ack);\n"
            "assign ack = req;\nendmodule\n")
        (self.dataset/"spec/toy.md").write_text("Ack equals req; rst is active high.")
        (self.dataset/"bind/toy_bind.sv").write_text("bind toy toy_assertions check_i (.*);\n")
        self.source=self.dataset/"assertions/toy_assertions.sv"
        self.source.write_text(
            "module toy_assertions(input logic clk, rst, req, ack);\n"
            "fixed_environment: assume property (@(posedge clk) !rst);\n"
            "p_one: assert property (@(posedge clk) disable iff (rst) req |-> ack);\n"
            "p_two: assert property (@(posedge clk) disable iff (rst) !req |-> !ack);\n"
            "endmodule\n")
        self.setup=prepare_existing(self.dataset,init_dataset(self.dataset))
        save(self.dataset/"gen_assertions/setup/dataset.json",self.setup)
        self.unit=self.setup["units"][0]
        self.props=load_existing(self.dataset,self.unit)
        self.settings=read(ROOT/"config.json")
        fake=ROOT/"tests/fake_jasper.py"
        fake.chmod(0o755)
        self.settings.update({"jasper_executable":str(fake),"jasper_version_tag":"OFFLINE_TEST_DOUBLE",
                              "syntax_timeout_seconds":10,"proof_timeout_seconds":10})
        self.events=self.dataset/"gen_assertions/formal_events.jsonl"
        self.event_env=patch.dict(os.environ,{"LOOP_TEST_EVENTS":str(self.events)})
        self.event_env.start();self.addCleanup(self.event_env.stop)
        self.lock=EnvironmentLock(self.dataset,self.setup)
        self.fixed_bytes={path:path.read_bytes() for path in self.lock.paths}

    def events_read(self):
        return [json.loads(line) for line in self.events.read_text().splitlines()] if self.events.exists() else []

    def run_gate(self,model,properties=None,*,stage=None,tag="first"):
        directory=self.dataset/"gen_assertions/ablation/offline"/model/"run_01/units/toy"/tag
        with redirect_stdout(io.StringIO()):
            return qualify(self.dataset,self.unit,properties if properties is not None else self.props,
                           directory,self.settings,stage=stage)

    def assert_fixed_context_unchanged(self):
        self.lock.check()
        for path,original in self.fixed_bytes.items():
            self.assertEqual(path.read_bytes(),original,str(path))
        for event in self.events_read():
            self.assertEqual(event["source"].count("fixed_environment: assume property (@(posedge clk) !rst);"),1)

    def test_second_model_reuses_all_technical_gates_without_subprocess(self):
        first=self.run_gate("deepseek")
        initial_events=self.events_read()
        self.assertTrue(all(row["technical_pass"] for row in first))
        self.assertEqual(Counter(row["stage"] for row in initial_events),{"syntax":2,"fpv":2,"vacuity":2})
        with patch("looplib.technical.command",side_effect=AssertionError("A complete cache hit cannot launch AQ/Jasper")):
            second=self.run_gate("qwen",tag="second")
        self.assertTrue(all(row["technical_pass"] for row in second))
        self.assertEqual(self.events_read(),initial_events)
        self.assertEqual([(row["label"],row["expression"]) for row in second],
                         [(prop["label"],prop["expression"]) for prop in self.props])
        self.assert_fixed_context_unchanged()

    def test_changed_assertion_reruns_only_itself_and_preserves_cached_sibling(self):
        self.run_gate("deepseek")
        before=len(self.events_read())
        changed=copy.deepcopy(self.props)
        changed[1]["expression"]="req |-> ##2 ack"
        rows=self.run_gate("qwen",changed,tag="changed")
        new_events=self.events_read()[before:]
        self.assertTrue(all(row["technical_pass"] for row in rows))
        self.assertEqual([row["stage"] for row in new_events],["syntax","fpv","vacuity"])
        for event in new_events:
            self.assertIn("p_two: assert",event["source"])
            self.assertNotIn("p_one: assert",event["source"])
            if event["stage"]=="vacuity":
                self.assertIn("AQ_ANT_REACH_",event["source"])
                self.assertIn("(req) |-> 1'b0",event["source"])
            else:
                self.assertIn("req |-> ##2 ack",event["source"])
        self.assertEqual(rows[0]["expression"],self.props[0]["expression"])
        self.assertEqual(rows[1]["expression"],changed[1]["expression"])
        self.assert_fixed_context_unchanged()

    def test_cached_counterexample_stops_later_sibling_and_vacuity(self):
        bad=copy.deepcopy(self.props)
        bad[0]["expression"]="fpv_bad |-> ack"
        first=self.run_gate("deepseek",bad)
        events=self.events_read()
        self.assertEqual([row["stage"] for row in events],["syntax","syntax","fpv"])
        self.assertEqual(first[0]["failed_stage"],"fpv")
        self.assertEqual(first[0]["fpv"],"CEX")
        with patch("looplib.technical.command",side_effect=AssertionError("A cached CEX must return before new siblings/stages")):
            second=self.run_gate("qwen",bad,tag="second")
        self.assertEqual(self.events_read(),events)
        self.assertEqual(second[0]["failed_stage"],"fpv")
        self.assertFalse(any(row["technical_pass"] for row in second))
        self.assertNotIn("fpv",second[1])
        self.assertEqual(second[0]["label"],bad[0]["label"])
        self.assertEqual(second[0]["expression"],bad[0]["expression"])
        self.assertEqual(second[0]["log_file"],first[0]["log_file"])
        self.assertTrue(Path(second[0]["log_file"]).is_file())
        self.assertIn("AQ_FPV_CEX",second[0]["diagnostic"])
        self.assert_fixed_context_unchanged()

    def test_cached_syntax_failure_blocks_all_later_work(self):
        bad=copy.deepcopy(self.props)
        bad[0]["expression"]="syntax_bad |-> ack"
        first=self.run_gate("deepseek",bad)
        events=self.events_read()
        self.assertEqual([row["stage"] for row in events],["syntax"])
        self.assertEqual(first[0]["failed_stage"],"syntax")
        with patch("looplib.technical.command",side_effect=AssertionError("A cached syntax FAIL must short circuit")):
            second=self.run_gate("gemini",bad,tag="second")
        self.assertEqual(self.events_read(),events)
        self.assertFalse(any(row["technical_pass"] for row in second))
        self.assertNotIn("syntax",second[1])
        self.assertEqual(second[0]["log_file"],first[0]["log_file"])
        self.assertTrue(Path(second[0]["log_file"]).is_file())
        self.assertIn("fixture syntax error",second[0]["diagnostic"])
        self.assert_fixed_context_unchanged()

    def test_unknown_result_runs_again_in_another_model_directory(self):
        prop=self.props[0]
        inventory={target_scope(self.dataset,self.unit)+"."+prop["label"]:"unknown"}
        with patch.dict(os.environ,{"LOOP_TEST_PROPERTIES_JSON":json.dumps(inventory)}):
            first=self.run_gate("deepseek",[prop],stage="fpv")
            second=self.run_gate("qwen",[prop],stage="fpv",tag="second")
        self.assertEqual([row["stage"] for row in self.events_read()],["fpv","fpv"])
        self.assertEqual([first[0]["status"],second[0]["status"]],["UNKNOWN","UNKNOWN"])
        self.assertFalse(first[0].get("cache_hit",False))
        self.assertFalse(second[0].get("cache_hit",False))
        self.assert_fixed_context_unchanged()

    def test_persistent_cache_rebinds_current_fields_and_retains_source_proof(self):
        from looplib.technical_cache import TechnicalCache
        prop=self.props[0]
        first=self.run_gate("deepseek",[prop],stage="fpv")
        hit=TechnicalCache(self.dataset,self.unit,self.settings,"fpv").lookup(prop)
        fresh=TechnicalCache(self.dataset,self.unit,self.settings,"fpv").lookup(copy.deepcopy(prop))
        self.assertIsNotNone(hit)
        self.assertIsNotNone(fresh)
        self.assertTrue(fresh["cache_hit"])
        self.assertEqual(fresh["label"],prop["label"])
        self.assertEqual(fresh["expression"],prop["expression"])
        self.assertEqual(fresh["cache_key"],hit["cache_key"])
        self.assertEqual(fresh["cache_source_label"],prop["label"])
        shared_log=Path(fresh["cache_source_log"])
        self.assertTrue(shared_log.is_file())
        self.assertTrue(shared_log.resolve().is_relative_to((self.dataset/"gen_assertions/_shared/step_a").resolve()))
        self.assertEqual(shared_log.read_bytes(),Path(first[0]["log_file"]).read_bytes())
        self.assertIn("AQ_FPV_PROVEN",shared_log.read_text())
        with patch("looplib.technical.command",side_effect=AssertionError("Fresh cache instances must persist evidence")):
            cached=self.run_gate("gpt",[prop],stage="fpv",tag="second")
        self.assertTrue(cached[0]["cache_hit"])
        self.assertEqual(cached[0]["log_file"],first[0]["log_file"])
        self.assertEqual(len(self.events_read()),1)
        self.assert_fixed_context_unchanged()

    def test_shared_evidence_survives_output_archive_and_corruption_triggers_real_check(self):
        from looplib.technical_cache import TechnicalCache
        prop=self.props[0]
        first=self.run_gate("deepseek",[prop],stage="fpv")
        original_log=Path(first[0]["log_file"])
        proof_bytes=original_log.read_bytes()
        source_output=self.dataset/"gen_assertions/ablation/offline/deepseek/run_01/units/toy/first"
        archive=source_output.parent/"_history/revision_original"
        archive.parent.mkdir(parents=True)
        source_output.rename(archive)
        self.assertFalse(original_log.exists())
        hit=TechnicalCache(self.dataset,self.unit,self.settings,"fpv").lookup(prop)
        self.assertIsNotNone(hit)
        shared_log=Path(hit["cache_source_log"])
        self.assertTrue(shared_log.is_file())
        self.assertEqual(shared_log.read_bytes(),proof_bytes)
        self.assertEqual(hit["log_file"],str(original_log),"Origin provenance remains recorded after archiving")
        with patch("looplib.technical.command",side_effect=AssertionError("Archived original output must not force a repeated proof")):
            second=self.run_gate("qwen",[prop],stage="fpv",tag="after_archive")
        self.assertTrue(second[0]["cache_hit"])
        self.assertEqual(Path(second[0]["cache_source_log"]).read_bytes(),proof_bytes)
        self.assertEqual(len(self.events_read()),1)
        shared_log.write_bytes(proof_bytes+b"\nOFFLINE_CORRUPTED_EVIDENCE\n")
        self.assertIsNone(TechnicalCache(self.dataset,self.unit,self.settings,"fpv").lookup(prop))
        rerun=self.run_gate("gemini",[prop],stage="fpv",tag="after_corruption")
        self.assertEqual(rerun[0]["status"],"PROVEN")
        self.assertFalse(rerun[0].get("cache_hit",False))
        self.assertEqual([event["stage"] for event in self.events_read()],["fpv","fpv"])
        repaired=TechnicalCache(self.dataset,self.unit,self.settings,"fpv").lookup(prop)
        self.assertIsNotNone(repaired)
        self.assertEqual(Path(repaired["cache_source_log"]).read_bytes(),Path(rerun[0]["log_file"]).read_bytes())
        self.assertNotIn(b"OFFLINE_CORRUPTED_EVIDENCE",Path(repaired["cache_source_log"]).read_bytes())
        self.assert_fixed_context_unchanged()

    def test_timeout_decision_is_not_stored_or_reused(self):
        from looplib.technical_cache import TechnicalCache
        prop=self.props[0]
        cache=TechnicalCache(self.dataset,self.unit,self.settings,"fpv")
        cache.store(prop,{"label":prop["label"],"expression":prop["expression"],"stage":"fpv",
                          "status":"TIMEOUT","passed":False,"failed_stage":"fpv","reason":"Solver timeout"})
        self.assertIsNone(TechnicalCache(self.dataset,self.unit,self.settings,"fpv").lookup(prop))
        measured=self.run_gate("deepseek",[prop],stage="fpv")
        self.assertEqual(measured[0]["status"],"PROVEN")
        self.assertFalse(measured[0].get("cache_hit",False))
        self.assertEqual([row["stage"] for row in self.events_read()],["fpv"])
        self.assert_fixed_context_unchanged()


if __name__=="__main__": unittest.main()
