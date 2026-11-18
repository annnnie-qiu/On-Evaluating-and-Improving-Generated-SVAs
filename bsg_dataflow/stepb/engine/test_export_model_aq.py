import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from export_model_aq import export, load_aq_parser


SOURCE_NAME = "queue_assertions.sv"
SOURCE = """module queue_assertions;
  a_keep: assert property (@(posedge clk) reset |=> ready);
  a_drop: assert property (@(posedge clk) request |=> grant);
  a_unqualified: assert property (@(posedge clk) never_seen |=> ready);
endmodule
"""


class ExportModelAQTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.parser = load_aq_parser(self.root)
        self.blocks, errors = self.parser.extract_assertions(SOURCE)
        self.assertFalse(errors)
        self.base_config = self.root / "base.json"
        self.base_config.write_text(json.dumps({
            "project_root": str(self.root),
            "top": "queue",
            "rtl_files": ["rtl/queue.sv"],
            "assertion_file": "aq_out/final/AQ/queue_assertions.sv",
            "assertions": [block.property_name for block in self.blocks],
        }))

    def prepare(self, variant, *, stale_source=False):
        source_dir = "assertions" if variant == "original" else "buggy_assertions"
        aq_dir = "aq_out" if variant == "original" else "aq_buggy_out"
        source_folder = self.root / source_dir
        source_folder.mkdir()
        source = SOURCE
        (source_folder / SOURCE_NAME).write_text(source + ("// changed\n" if stale_source else ""))
        aq_output = self.root / aq_dir
        manifests = aq_output / "manifests"
        manifests.mkdir(parents=True)
        sha = hashlib.sha256(source.encode()).hexdigest()
        (manifests / "inventory_files.json").write_text(json.dumps([{
            "assertion_file": SOURCE_NAME, "source_sha256": sha,
        }]))
        (manifests / "inventory_assertions.json").write_text(json.dumps([{
            "assertion_file": SOURCE_NAME,
            "assertion_id": block.assertion_id,
            "property_name": block.property_name,
            "fingerprint": block.fingerprint,
        } for block in self.blocks]))
        (manifests / "vacuity.json").write_text(json.dumps([{
            "assertion_file": SOURCE_NAME,
            "assertion_id": block.assertion_id,
            "status": "PASS" if index < 2 else "VACUOUS",
        } for index, block in enumerate(self.blocks)]))
        run_folder = aq_output / "spec_guard_four_models" / "deepseek" / "run_01"
        run_folder.mkdir(parents=True)
        (run_folder / "summary.json").write_text(json.dumps({
            "run_status": "COMPLETE", "model_key": "deepseek",
            "input_variant": "NORMAL" if variant == "original" else "BUGGY",
            "run_number": 1, "technical_aq": 2, "pass_count": 1,
        }))
        (run_folder / "spec_check_results.json").write_text(json.dumps([{
            "assertion_file": SOURCE_NAME,
            "assertion_id": block.assertion_id,
            "property_name": block.property_name,
            "verdict": "PASS" if index == 0 else "FAIL",
            "model_key": "deepseek", "run_number": 1,
            "fingerprint": hashlib.sha256((block.assertion_id + "deepseek1").encode()).hexdigest(),
        } for index, block in enumerate(self.blocks[:2])]))

    def test_exports_only_model_passed_assertion(self):
        self.prepare("original")
        record = export(self.base_config, "deepseek", "original", 1,
                        SOURCE_NAME, self.root / "export_normal")
        rendered = Path(record["aq_file"]).read_text()
        kept, errors = self.parser.extract_assertions(rendered)
        self.assertFalse(errors)
        self.assertEqual([block.property_name for block in kept], ["a_keep"])
        self.assertIn("AQ removed", rendered)
        config = json.loads(Path(record["config"]).read_text())
        self.assertEqual(config["assertions"], ["a_keep"])
        self.assertEqual(config["assertion_file"], record["aq_file"])
        self.assertEqual(record["technical_count"], 2)
        self.assertEqual(record["final_aq_count"], 1)

    def test_controlled_variant_uses_buggy_input(self):
        self.prepare("controlled")
        record = export(self.base_config, "deepseek", "controlled", 1,
                        SOURCE_NAME, self.root / "export_controlled")
        self.assertIn("buggy_assertions", record["source"])
        self.assertIn("aq_buggy_out", record["spec_results"])

    def test_rejects_stale_source(self):
        self.prepare("original", stale_source=True)
        with self.assertRaisesRegex(ValueError, "changed since technical AQ"):
            export(self.base_config, "deepseek", "original", 1,
                   SOURCE_NAME, self.root / "export_stale")


if __name__ == "__main__":
    unittest.main()
