"""Recovery keeps imported assertion slots and their original source identity."""
from __future__ import annotations

from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from looplib.common import read, save
from looplib.recovery import prepare_recovery


class InputRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "experiment" / "qwen" / "run_01" / "units" / "queue"
        self.context = {"dataset": "toy", "generator_model": "qwen", "repeat": 1, "unit": "queue"}
        self.config = {"models": {"qwen": {"id": "fixture/model"}}}
        self.lock = SimpleNamespace(identity="unchanged-environment-fixture")
        self.client = SimpleNamespace(records=lambda: [])
        self.write_recipe("old-version")
        self.originals = [self.original(n) for n in (1, 2, 3)]
        save(self.directory / "input_assertions.json", self.originals)

    @staticmethod
    def original(n):
        rid = f"input_{n:04d}"
        return {"slot_id": rid, "repair_id": rid, "label": f"original_check_{n}",
                "source_label": f"original_check_{n}", "expression": f"q == {n}",
                "source_file": "assertions/queue_assertions.sv", "source_line": 10 + n,
                "basis": "existing assertion"}

    def write_recipe(self, version):
        save(self.directory / "recipe.json", {"sha256": version, "context": self.context})
        save(self.directory / "fixed_environment.json", {"sha256": self.lock.identity})

    def attempt_path(self, attempt=1, round_=1, name="attempt.json"):
        return self.directory / "rounds" / f"round_{round_:02d}" / "step_a_attempts" / f"attempt_{attempt:02d}" / name

    def recover(self, version="current-version"):
        with redirect_stdout(io.StringIO()):
            return prepare_recovery(self.directory, version, self.lock, self.context, self.client, self.config)

    def assert_original_metadata(self, item, n):
        original = self.original(n)
        for key in ("slot_id", "repair_id", "label", "source_label", "source_file", "source_line"):
            self.assertEqual(item[key], original[key], key)
        self.assertTrue(Path(item["recovery_source_file"]).is_file())
        self.assertNotIn("checks", item)
        self.assertNotIn("technical_pass", item)
        self.assertNotIn("spec_check", item)

    def test_pre_b1_accepted_held_and_problem_preserve_original_slots(self):
        checkpoint = self.attempt_path(2)
        save(checkpoint, {
            "accepted": [{**self.originals[0], "checks": {"syntax": "PASS"}, "spec_check": {"verdict": "PASS"}}],
            "held_candidates": [{**self.originals[1], "expression": "q == 20", "checks": {"syntax": "PASS"}}],
            # Actual failure records can carry no slot/source metadata.
            "problems": [{"repair_id": "input_0003", "label": "original_check_3", "expression": "q == 30",
                          "failed_stage": "fpv", "technical_pass": False}],
        })
        before = checkpoint.read_bytes()
        state = self.recover()
        items = {item["repair_id"]: item for item in state["candidates"]}
        self.assertEqual(set(items), {"input_0001", "input_0002", "input_0003"})
        for n in (1, 2, 3):
            self.assert_original_metadata(items[f"input_{n:04d}"], n)
        self.assertEqual(items["input_0002"]["expression"], "q == 20")
        self.assertEqual(items["input_0003"]["expression"], "q == 30")
        archived = Path(state["history"]) / checkpoint.relative_to(self.directory)
        self.assertEqual(archived.read_bytes(), before)

    def test_paid_repair_reply_keeps_original_label_without_repeating_metadata(self):
        save(self.attempt_path(2), {"held_candidates": [{**self.originals[1], "expression": "q == 20"}],
                                  "problems": [{"repair_id": "input_0002", "expression": "q == 20", "failed_stage": "syntax"}]})
        reply = self.attempt_path(3, name="generation.json")
        save(reply, {"assertions": [{"repair_id": "input_0002", "expression": "q == 22"}]})
        state = self.recover()
        item = next(item for item in state["candidates"] if item["repair_id"] == "input_0002")
        self.assert_original_metadata(item, 2)
        self.assertEqual(item["expression"], "q == 22")
        self.assertEqual(Path(item["recovery_source_file"]), Path(state["history"]) / reply.relative_to(self.directory))

    def test_completed_step_b_restarts_original_inputs_without_future_budget_leakage(self):
        late = [{**self.originals[0], "expression": "future_budget_changed_original"},
                {"repair_id": "b020_candidate_001", "label": "future_added", "expression": "future_budget_added"}]
        save(self.directory / "rounds" / "round_20" / "round.json",
             {"step_b_ran": True, "step_b_rounds_completed": 20, "accepted": late})
        save(self.attempt_path(1, round_=20, name="generation.json"), {"assertions": late})
        save(self.attempt_path(2), {"problems": [{"repair_id": "input_0001", "expression": "late_bad",
                                               "failed_stage": "fpv"}]})
        state = self.recover()
        self.assertEqual(state["strategy"], "revalidate_original_pre_B1_batch")
        self.assertEqual([item["expression"] for item in state["candidates"]],
                         [item["expression"] for item in self.originals])
        self.assertEqual(state["failure_history"], {})
        for n, item in enumerate(state["candidates"], 1):
            self.assert_original_metadata(item, n)
        self.assertTrue((Path(state["history"]) / "rounds" / "round_20" / "round.json").is_file())

    def test_second_upgrade_before_revalidation_preserves_repaired_seed(self):
        save(self.attempt_path(2), {"held_candidates": [{**self.originals[0], "expression": "q == 100"}]})
        first = self.recover("revision-one")
        self.write_recipe("revision-one")
        # run_unit saves original inputs before beginning revalidation.
        save(self.directory / "input_assertions.json", self.originals)
        second = self.recover("revision-two")
        self.assertEqual([item["expression"] for item in second["candidates"]],
                         [item["expression"] for item in first["candidates"]])
        item = next(item for item in second["candidates"] if item["repair_id"] == "input_0001")
        self.assert_original_metadata(item, 1)
        self.assertEqual(item["expression"], "q == 100")

    def test_nested_policy_candidate_metadata_is_preserved(self):
        save(self.attempt_path(), {"problems": [{"repair_id": "input_0002", "failed_stage": "expression_policy",
                                                "candidate": {**self.originals[1], "expression": "q == 222"}}]})
        state = self.recover()
        item = next(item for item in state["candidates"] if item["repair_id"] == "input_0002")
        self.assert_original_metadata(item, 2)
        self.assertEqual(item["expression"], "q == 222")

    def test_legacy_generated_source_file_retains_archived_checkpoint_meaning(self):
        (self.directory / "input_assertions.json").unlink()
        checkpoint = self.attempt_path()
        save(checkpoint, {"accepted": [{"repair_id": "old_1", "label": "old_label", "expression": "req |-> ack"}]})
        state = self.recover()
        item = state["candidates"][0]
        self.assertEqual(item["source_file"], str(Path(state["history"]) / checkpoint.relative_to(self.directory)))
        self.assertEqual(item["source_label"], "old_label")
        self.assertNotIn("recovery_source_file", item)
        self.assertNotIn("slot_id", item)


if __name__ == "__main__":
    unittest.main()
