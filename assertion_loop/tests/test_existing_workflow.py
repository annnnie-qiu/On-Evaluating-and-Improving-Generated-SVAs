"""Offline integration regressions for qualifying existing assertion files.

API replies and mutation scores are explicit stubs.  The syntax-repair test uses
the production qualification subprocess and generated Tcl with fake_jasper.py;
these tests provide control-flow evidence, not licensed formal proof evidence.
"""
from __future__ import annotations

import copy
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from looplib.bootstrap import init_dataset
from looplib.common import EnvironmentLock, SpecEvidenceRequired, read, save
from looplib.engine import run_unit
from looplib.faults import score_rows
from looplib.inputs import prepare_existing
from looplib.llm import Client, assertion_blocks
from looplib.properties import render, spec_messages
import run


class ExistingWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dataset = Path(temporary.name) / "toy"
        for folder in ("rtl", "spec", "bind", "assertions"):
            (self.dataset / folder).mkdir(parents=True)
        (self.dataset / "rtl/toy.sv").write_text(
            "module toy(input logic clk, rst, req, output logic ack);\n"
            "assign ack = req;\nendmodule\n")
        self.spec = "The output ack equals req. The environment uses active-high reset rst."
        (self.dataset / "spec/toy.md").write_text(self.spec)
        self.bind = "bind toy toy_assertions fixed_checker_i (.*);\n"
        (self.dataset / "bind/toy_bind.sv").write_text(self.bind)
        self.original_file = self.dataset / "assertions/toy_assertions.sv"
        self.config = read(ROOT / "config.json")
        self.config.update(stop_on_saturation=False,stop_on_repeated_request=False)
        self.config["fault_qualification"]="equivalence"
        fake = ROOT / "tests/fake_jasper.py"
        fake.chmod(0o755)
        self.config.update({"backend": "lexical", "jasper_executable": str(fake),
                            "jasper_version_tag": "OFFLINE_TEST_DOUBLE",
                            "syntax_timeout_seconds": 10, "proof_timeout_seconds": 10,
                            "max_mutants_per_family": 1})
        self.events = []
        self.pool = {"identity": "OFFLINE_STUB_POOL", "mutants": [
            {"id": partition + "_1", "family": "F1", "line": 1,
             "operator": "OFFLINE_STUB", "before": "req", "after": "!req"}
            for partition in ("dev", "validation", "test")],
            "dev_ids": ["dev_1"], "validation_ids": ["validation_1"], "test_ids": ["test_1"]}

    def prepare(self, *, syntax_bad=False, one=False):
        bad_body = "syntax_bad |-> ack" if syntax_bad else "req |=> ack"
        second = "" if one else f"    a_bad: assert property (@(posedge clk) disable iff (rst) {bad_body});\n"
        self.original = (
            "module toy_assertions(input logic clk, rst, req, ack);\n"
            "  logic helper;\n"
            "  always_comb helper = req;\n"
            "  fixed_environment: assume property (@(posedge clk) !rst);\n"
            "  generate if (1) begin : original_scope\n"
            "    a_good: assert property (@(posedge clk) disable iff (rst) req |-> ack);\n"
            + second +
            "    seen: cover property (@(posedge clk) req);\n"
            "  end endgenerate\n"
            "endmodule\n")
        self.original_file.write_text(self.original)
        self.setup = prepare_existing(self.dataset, init_dataset(self.dataset))
        save(self.dataset / "gen_assertions/setup/dataset.json", self.setup)
        self.unit = self.setup["units"][0]
        self.lock = EnvironmentLock(self.dataset, self.setup)
        self.directory = self.dataset / "gen_assertions/ablation/offline/deepseek/run_01/units/toy"

    def fake_measure(self, dataset, unit, properties, pool, partition, directory, config, lock):
        """OFFLINE STUB: one mutant CEX for every supplied qualified check."""
        self.assertNotEqual(partition, "test", "Ablation must keep test sealed")
        self.assertTrue(all(all(prop["checks"].get(stage) == "PASS" for stage in
                               ("syntax", "fpv", "vacuity", "spec")) for prop in properties))
        self.events.append({"kind": "measure", "partition": partition,
                            "properties": copy.deepcopy(properties)})
        render(dataset, unit, properties)  # exercise production slot renderer
        mutants = [mutant for mutant in pool["mutants"] if mutant["id"] in pool[partition + "_ids"]]
        rows = [{"assertion_id": prop["label"], "mutant_id": mutant["id"], "status": "CEX"}
                for prop in properties for mutant in mutants]
        metrics = score_rows(properties, mutants, rows)
        metrics.update({"partition": partition, "status": "MEASURED"})
        save(Path(directory) / "metrics.json", metrics)
        return metrics

    def passing_technical_stub(self, dataset, unit, properties, directory, config, *, stage):
        """OFFLINE STUB: render real source and mark each technical gate PASS."""
        source = render(dataset, unit, properties)
        rows = []
        for prop in properties:
            self.events.append({"kind": "gate", "stage": stage, "label": prop["label"],
                                "expression": prop["expression"], "source": source})
            rows.append({"label": prop["label"], "expression": prop["expression"],
                         "stage": stage, "status": "PASS", "passed": True})
        return rows

    def run_with_client(self, call, *, model="deepseek", technical_stub=True):
        client = SimpleNamespace(call=call, records=lambda: [])
        with patch("looplib.engine.measure", side_effect=self.fake_measure), redirect_stdout(io.StringIO()):
            if technical_stub:
                with patch("looplib.engine.qualify", side_effect=self.passing_technical_stub):
                    return run_unit(self.dataset, self.unit, model, 1, self.config, client,
                                    self.pool, self.lock, self.directory, [1], evaluate_test=False)
            return run_unit(self.dataset, self.unit, model, 1, self.config, client,
                            self.pool, self.lock, self.directory, [1], evaluate_test=False)

    def test_original_syntax_failure_repairs_only_failed_slot_then_enters_step_b(self):
        self.prepare(syntax_bad=True)
        generator_calls = []
        formal_events = self.dataset / "gen_assertions/offline_formal_events.jsonl"
        def call(model, purpose, messages, context):
            self.events.append({"kind": "api", "purpose": purpose, "model": model})
            if purpose == "generation":
                generator_calls.append({"model": model, "messages": messages, "context": context})
                self.assertEqual(len(generator_calls), 1, "No initial generation or endless same-expression repair")
                self.assertEqual(context["repair_ids"], ["input_0002"])
                problem = json.loads(messages[-1]["content"].split("FEEDBACK:\n", 1)[1])["repair_questions"]
                self.assertEqual(len(problem), 1)
                self.assertEqual(problem[0]["repair_id"], "input_0002")
                self.assertEqual(problem[0]["failed_stage"], "syntax")
                self.assertEqual(problem[0]["expression"], "syntax_bad |-> ack")
                self.assertIn("fixture syntax error", problem[0]["diagnostic"])
                return assertion_blocks("BEGIN_ASSERTION input_0002\nreq |=> ack\nEND_ASSERTION", context["repair_ids"])
            self.assertEqual((model, purpose), ("gpt", "spec_check"))
            return {"verdict": "PASS", "question": "", "reason": "OFFLINE_STUB", "evidence_quote": self.spec,
                    "suggested_change": ""}
        with patch.dict(os.environ, {"LOOP_TEST_EVENTS": str(formal_events)}):
            rows = self.run_with_client(call, technical_stub=False)
        events = [json.loads(line) for line in formal_events.read_text().splitlines()]
        self.assertEqual([event["stage"] for event in events[:3]], ["syntax", "syntax", "syntax"])
        # a_good passed before a_bad failed and is never needlessly recompiled.
        self.assertEqual(sum(event["stage"] == "syntax" and "a_good: assert" in event["source"] for event in events), 1)
        self.assertEqual(sum(event["stage"] == "syntax" and "req |=> ack" in event["source"] for event in events), 1)
        self.assertEqual(generator_calls[0]["model"], "deepseek")
        self.assertEqual(rows[0]["initial_assertion_count"], 2)
        self.assertEqual(rows[0]["qualified_count"], 2)
        self.assertEqual(rows[0]["generation_calls_used"], 1)
        self.assertEqual(rows[0]["step_a_repairs_used"], 1)
        first = read(self.directory / "rounds/round_01/step_a_attempts/attempt_01/attempt.json")
        self.assertEqual(first["accepted"], [])
        self.assertEqual(first["problems"][0]["repair_id"], "input_0002")
        self.assertFalse(any(prop["checks"].get("fpv") for prop in first["held_candidates"]))
        final = read(self.directory / "final/assertions.json")
        self.assertEqual([(prop["label"], prop["slot_id"], prop["repair_id"]) for prop in final],
                         [("a_good", "input_0001", "input_0001"), ("a_bad", "input_0002", "input_0002")])
        self.assertEqual(final[0]["expression"], "req |-> ack")
        self.assertEqual(final[1]["source_file"], "assertions/toy_assertions.sv")
        self.assertEqual((self.directory / "final/qualified_assertions.sv").read_text(),
                         self.original.replace("syntax_bad |-> ack", "req |=> ack"))
        self.assertEqual(self.original_file.read_text(), self.original)
        self.assertEqual((self.dataset / "bind/toy_bind.sv").read_text(), self.bind)
        self.lock.check()
        first_measure = next(index for index, event in enumerate(self.events) if event["kind"] == "measure")
        self.assertEqual(sum(event.get("purpose") == "spec_check" for event in self.events[:first_measure]), 2)

    def test_all_four_models_import_correct_b1_without_generation_call(self):
        for model in ("qwen", "deepseek", "gpt", "gemini"):
            with self.subTest(model=model):
                self.prepare(one=True)
                self.directory = self.dataset / "gen_assertions/ablation/offline" / model / "run_01/units/toy"
                def call(key, purpose, messages, context):
                    self.assertEqual((key, purpose), ("gpt", "spec_check"))
                    return {"verdict": "PASS", "question": "", "reason": "OFFLINE_STUB", "evidence_quote": self.spec,
                            "suggested_change": ""}
                rows = self.run_with_client(call, model=model)
                self.assertEqual(rows[0]["generation_calls_used"], 0)
                self.assertEqual(rows[0]["step_a_repairs_used"], 0)
                self.assertEqual(rows[0]["qualified_count"], 1)
                self.assertEqual((self.directory / "final/qualified_assertions.sv").read_text(), self.original)
                self.assertEqual(self.original_file.read_text(), self.original)

    def test_gpt_fail_and_unresolved_accept_model_review_without_rewriting_or_repeating_technical_gates(self):
        for verdict in ("FAIL", "UNRESOLVED"):
            with self.subTest(verdict=verdict):
                self.prepare(one=True)
                self.directory = self.dataset / "gen_assertions/ablation" / ("review_" + verdict) / "deepseek/run_01/units/toy"
                self.events = []
                spec_calls = []
                reviews = []
                evidence = "SPEC: ack equals req. RTL assign ack = req confirms same-cycle req implies ack."
                suggestion = "Consider whether req should imply ack one cycle later; verify timing against RTL."
                def call(model, purpose, messages, context):
                    self.events.append({"kind": "api", "purpose": purpose, "model": model})
                    if purpose == "generation":
                        self.assertEqual(model, "deepseek")
                        reviews.append(messages)
                        self.assertEqual(len(reviews), 1)
                        task = json.loads(messages[-1]["content"].split("FEEDBACK:\n", 1)[1])["repair_questions"][0]
                        self.assertEqual(task["failed_stage"], "spec")
                        self.assertEqual(task["verdict"], verdict)
                        self.assertEqual(task["suggested_change"], suggestion)
                        self.assertEqual(task["expression"], "req |-> ack")
                        self.assertEqual(context["review_ids"], ["input_0001"])
                        return assertion_blocks("BEGIN_REVIEW input_0001\n" + evidence + "\nEND_REVIEW",
                                                context["repair_ids"], context["review_ids"])
                    self.assertEqual((model, purpose), ("gpt", "spec_check"))
                    spec_calls.append(messages)
                    self.assertLessEqual(len(spec_calls), 2)
                    text = "\n".join(message["content"] for message in messages)
                    for required in (self.spec, "assign ack = req", self.bind.strip(), "toy_assertions", "fixed_checker_i"):
                        self.assertIn(required, text)
                    if len(spec_calls) == 1:
                        self.assertFalse(any(event["kind"] == "measure" for event in self.events))
                        return {"verdict": verdict, "question": "Does the timing agree with spec?", "reason": "OFFLINE_STUB",
                                "evidence_quote": self.spec, "suggested_change": suggestion}
                    self.assertIn(evidence, text)
                    self.assertFalse(any(event["kind"] == "measure" for event in self.events), "KEEP review cannot approve itself")
                    return {"verdict": "PASS", "question": "", "reason": "Review resolved timing question.",
                            "evidence_quote": self.spec, "suggested_change": ""}
                rows = self.run_with_client(call)
                self.assertEqual(len(spec_calls), 2)
                self.assertEqual(len(reviews), 1)
                gate_events = [event for event in self.events if event["kind"] == "gate"]
                self.assertEqual([event["stage"] for event in gate_events], ["syntax", "fpv", "vacuity"])
                self.assertEqual(rows[0]["generation_calls_used"], 1)
                self.assertEqual(rows[0]["step_a_repairs_used"], 1)
                self.assertEqual(rows[0]["qualified_count"], 1)
                final = read(self.directory / "final/assertions.json")
                self.assertEqual(final[0]["expression"], "req |-> ack")
                self.assertEqual(final[0]["spec_review"], evidence)
                self.assertEqual(final[0]["spec_check"]["verdict"], "PASS")
                self.assertEqual((self.directory / "final/qualified_assertions.sv").read_text(), self.original)
                first = read(self.directory / "rounds/round_01/step_a_attempts/attempt_01/attempt.json")
                self.assertEqual(first["accepted"], [])
                self.assertEqual(first["problems"][0]["failed_stage"], "spec")
                second = read(self.directory / "rounds/round_01/step_a_attempts/attempt_02/attempt.json")
                self.assertEqual(second["changes"][0]["status"], "KEEP_REVIEW")
                self.assertEqual(second["problems"], [])

    def test_default_cli_plan_imports_existing_assertions_and_requested_prefixes_for_all_models(self):
        self.prepare(one=True)
        with patch.object(Client, "request") as request, redirect_stdout(io.StringIO()):
            status = run.main(ablation=True, argv=[str(self.dataset), "--plan", "--experiment", "existing_plan"])
        self.assertEqual(status, 0)
        request.assert_not_called()
        plan = read(self.dataset / "gen_assertions/ablation/existing_plan/plan.json")
        self.assertEqual(read(ROOT / "config.json")["input_mode"], "existing_assertions")
        self.assertEqual(plan["input_mode"], "existing_assertions")
        self.assertEqual(plan["spec_policy"], "contradiction_only")
        self.assertEqual(plan["budgets"], [1, 3, 5, 10, 15, 20])
        self.assertEqual(plan["max_step_b_rounds"], 20)
        self.assertEqual(set(plan["models"]), {"qwen", "deepseek", "gpt", "gemini"})
        self.assertEqual(plan["step_a_policy"], "all_pass_with_attempt_limit")
        self.assertEqual(plan["step_a_max_attempts"], 50)
        self.assertIn("one trajectory", plan["trajectory_policy"])
        self.assertEqual(plan["units"]["units"][0]["assertion_files"], ["assertions/toy_assertions.sv"])

    def test_missing_assertion_source_stops_before_llm_or_jasper(self):
        self.prepare(one=True)
        self.original_file.unlink()
        output = io.StringIO()
        with patch.object(Client, "request") as request, patch.object(run, "run_unit") as workflow, redirect_stdout(output), redirect_stderr(output):
            status = run.main(ablation=True, argv=[str(self.dataset), "--experiment", "missing_input"])
        self.assertEqual(status, 1)
        request.assert_not_called()
        workflow.assert_not_called()
        self.assertIn("assertion source", output.getvalue())
        self.assertFalse((self.dataset / "gen_assertions/ablation/missing_input/api").exists())

    def test_genuine_unresolved_context_after_model_review_checkpoints_and_replay_makes_no_new_api_call(self):
        self.prepare(one=True)
        api_calls = []
        question = "The supplied requirement excerpt is unreadable; provide its legible text to assess an explicit contradiction."
        def call(model, purpose, messages, context):
            api_calls.append((model, purpose))
            self.assertLessEqual(len(api_calls), 3, "An unresolved evidence checkpoint must stop paid retry loops")
            if purpose == "generation":
                self.assertEqual(model, "deepseek")
                return assertion_blocks(
                    "BEGIN_REVIEW input_0001\nThe current expression agrees with RTL; the damaged excerpt cannot be reconstructed safely.\nEND_REVIEW",
                    context["repair_ids"], context["review_ids"])
            self.assertEqual((model, purpose), ("gpt", "spec_check"))
            return {"verdict": "UNRESOLVED", "question": question,
                    "reason": "OFFLINE_STUB: unreadable context, not an undescribed behavior.",
                    "evidence_quote": "", "suggested_change": "Provide readable requirement text; do not invent a rewrite."}
        # Isolate this replay regression from other agents editing runtime files:
        # both invocations represent the same installed source revision.
        with patch("looplib.engine.implementation_digest", return_value="OFFLINE_STABLE_REVISION"):
            with self.assertRaisesRegex(SpecEvidenceRequired, "legible text"):
                self.run_with_client(call)
            count = len(api_calls)
            with self.assertRaisesRegex(SpecEvidenceRequired, "legible text"):
                self.run_with_client(call)
        self.assertEqual(count, 3)
        self.assertEqual(len(api_calls), count)
        self.assertEqual(api_calls, [("gpt", "spec_check"), ("deepseek", "generation"), ("gpt", "spec_check")])
        self.assertFalse(any(event["kind"] == "measure" for event in self.events))
        self.assertEqual([event["stage"] for event in self.events if event["kind"] == "gate"],
                         ["syntax", "fpv", "vacuity"])
        attempt = read(self.directory / "rounds/round_01/step_a_attempts/attempt_02/attempt.json")
        self.assertEqual(attempt["accepted"], [])
        self.assertEqual(attempt["spec_evidence_required"][0]["repair_id"], "input_0001")
        self.assertEqual(attempt["held_candidates"][0]["expression"], "req |-> ack")
        pending = read(self.directory / "rounds/round_01/pending_repairs.json")
        self.assertEqual(pending["step_a_attempt"], 2)
        self.assertEqual(pending["problems"][0]["failed_stage"], "spec")
        evidence = read(self.directory / "spec_evidence_required.json")
        self.assertEqual(evidence["status"], "SPEC_EVIDENCE_REQUIRED")
        self.assertEqual(evidence["step_b_rounds_completed"], 0)
        self.assertIn(question, evidence["questions"][0]["question"])
        self.assertFalse((self.directory / "rounds/round_01/round.json").exists())
        self.assertFalse((self.directory / "final/assertions.json").exists())
        self.assertEqual(self.original_file.read_text(), self.original)

    def test_twenty_step_b_rounds_preserve_original_and_use_cumulative_requested_prefixes(self):
        self.prepare(one=True)
        generation_rounds = []
        spec_calls = []
        def call(model, purpose, messages, context):
            if purpose == "generation":
                self.assertEqual(model, "deepseek")
                generation_rounds.append(context["round"])
                self.assertEqual(context["repair_ids"], [])
                self.assertGreater(context["round"], 1, "B1 must use the imported file")
                text = "\n".join(message["content"] for message in messages)
                self.assertIn("req |-> ack", text)
                return assertion_blocks(f"BEGIN_ASSERTION addition\nreq |-> ##{context['round']} ack\nEND_ASSERTION")
            self.assertEqual((model, purpose), ("gpt", "spec_check"))
            spec_calls.append(context["round"])
            return {"verdict": "PASS", "question": "", "reason": "OFFLINE_STUB: no explicit contradiction.",
                    "evidence_quote": "", "suggested_change": ""}
        client = SimpleNamespace(call=call, records=lambda: [])
        budgets = [1, 3, 5, 10, 15, 20]
        with patch("looplib.engine.qualify", side_effect=self.passing_technical_stub), \
             patch("looplib.engine.measure", side_effect=self.fake_measure), redirect_stdout(io.StringIO()):
            rows = run_unit(self.dataset, self.unit, "deepseek", 1, self.config, client,
                            self.pool, self.lock, self.directory, budgets, evaluate_test=False)
        self.assertEqual(generation_rounds, list(range(2, 21)))
        self.assertEqual(spec_calls, list(range(1, 21)))
        self.assertEqual(sum(event["kind"] == "measure" and event["partition"] == "dev" for event in self.events), 20)
        self.assertEqual(len(list((self.directory / "rounds").glob("round_*/round.json"))), 20)
        self.assertEqual([row["budget"] for row in rows], budgets)
        original_statement = self.unit["assertion_slots"][0]["original_statement"]
        for row in rows:
            budget = row["budget"]
            self.assertEqual(row["qualified_count"], budget)
            self.assertEqual(row["generation_calls_used"], budget - 1)
            self.assertEqual(row["step_a_repairs_used"], 0)
            self.assertEqual(row["step_b_rounds_completed"], budget)
            self.assertEqual(row["initial_assertion_count"], 1)
            self.assertEqual(row["spec_policy"], "contradiction_only")
            self.assertEqual(row["test_status"], "SEALED_NOT_EVALUATED")
            state = read(self.directory / "rounds" / f"round_{budget:02d}" / "round.json")
            frozen = read(self.directory / "budgets" / f"budget_{budget:02d}" / "frozen_assertions.json")
            self.assertEqual(frozen, state["accepted"])
            self.assertEqual(len(frozen), budget)
            self.assertEqual(frozen[0]["label"], "a_good")
            self.assertEqual(frozen[0]["slot_id"], "input_0001")
            self.assertEqual(frozen[0]["expression"], "req |-> ack")
            snapshot = (self.directory / "budgets" / f"budget_{budget:02d}" / "qualified_assertions.sv").read_text()
            self.assertEqual(snapshot.count(original_statement), 1)
            self.assertEqual(snapshot.count("fixed_environment: assume property"), 1)
        final = read(self.directory / "final/assertions.json")
        self.assertEqual(len(final), 20)
        self.assertEqual([prop["expression"] for prop in final[1:]], [f"req |-> ##{n} ack" for n in range(2, 21)])
        self.assertEqual(self.original_file.read_text(), self.original)

    def test_spec_policy_requires_explicit_contradiction_and_does_not_fail_silent_spec(self):
        messages = spec_messages("This document describes reset only.", "a: assert property (req |-> ack);",
                                 rtl="assign ack = req;", context={"unit": "toy"})
        policy = messages[0]["content"].lower()
        self.assertIn("contradict", policy)
        self.assertIn("explicit", policy)
        self.assertRegex(policy, r"silence|silent|not described|does not describe|does not mention|omission|omitted|unspecified|not specified")
        self.assertNotIn("if required specification information is truly absent, say unresolved", policy)
        self.prepare(one=True)
        (self.dataset / "spec/toy.md").write_text("Reset rst is active high. No output timing is described.")
        self.lock = EnvironmentLock(self.dataset, self.setup)
        def call(model, purpose, messages, context):
            self.assertEqual((model, purpose), ("gpt", "spec_check"))
            self.assertIn("No output timing is described.", messages[1]["content"])
            return {"verdict": "PASS", "question": "", "reason": "No explicit contradictory requirement exists.",
                    "evidence_quote": "", "suggested_change": ""}
        rows = self.run_with_client(call)
        self.assertEqual(rows[0]["spec_policy"], "contradiction_only")
        self.assertEqual(rows[0]["qualified_count"], 1)
        self.assertEqual(rows[0]["generation_calls_used"], 0)


if __name__ == "__main__":
    unittest.main()
