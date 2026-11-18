import json
import tempfile
import unittest
from pathlib import Path

from collect_jasper import input_fingerprint, make_tcl
from evaluate import read_matrix
from run_batch import average_runs, cached_run_fresh, module_configs, report_existing_module


class BatchTests(unittest.TestCase):
    def test_module_configs_come_from_aq_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aq = root / "aq.json"
            aq.write_text(json.dumps({
                "units": {"mapping": {"*block_assertions.v": {
                    "top": "block", "rtl_files": ["rtl/pkg.sv", "rtl/block.sv"],
                    "clock_mode": "none"}}},
                "rtl": {"include_dirs": ["rtl"], "defines": []},
                "jasper": {"executable": "jg"},
                "stages": {"syntax": {}, "fpv": {}},
            }))
            modules = module_configs(root, root / "out", aq)
            self.assertEqual(len(modules), 1)
            self.assertEqual(modules[0]["source"], "block_assertions.v")
            self.assertEqual(modules[0]["target"], "rtl/block.sv")

    def test_average_uses_common_budget_and_records_each_trial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = []
            for trial, curve in enumerate(([0, 50, 100], [0, 100], [0, 0, 50, 100]), 1):
                path = root / f"trial_{trial}.json"
                path.write_text(json.dumps({
                    "active_families": ["F1", "F2"],
                    "assertion_count": len(curve) - 1,
                    "macro_full_set_percent_lower": curve[-1],
                    "macro_full_set_percent_upper": curve[-1],
                    "macro_curve": [{"k": k, "macro_percent_lower": value,
                                     "macro_percent_upper": value}
                                    for k, value in enumerate(curve)],
                }))
                rows.append({"status": "PASS", "evaluation_summary": str(path)})
            result = average_runs(rows, root / "model", 3)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["assertion_counts"], [2, 1, 3])
            self.assertEqual(result["trial_full_set_percent"], [100, 100, 100])
            self.assertAlmostEqual(result["curve"][2]["mean_lower_percent"], 250 / 3)
            self.assertTrue((root / "model" / "average_curve.svg").is_file())

    def test_missing_trial_does_not_create_partial_average(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = average_runs([{"status": "PASS"}, {"status": "INPUT_ERROR"}], root, 3)
            self.assertEqual(result["status"], "INCOMPLETE")

    def test_report_only_recovers_original_results_without_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = {"name": "block_assertions"}
            for trial in range(1, 4):
                folder = root / "block_assertions" / "original" / "qwen" / f"run_{trial:02d}"
                (folder / "evaluation").mkdir(parents=True)
                (folder / "input").mkdir()
                (folder / "manifest.json").write_text(json.dumps({
                    "assertions": ["a"], "mutants": [
                        {"id": "F1_001", "family": "F1", "file": "one.sv"},
                        {"id": "F2_001", "family": "F2", "file": "two.sv"}]}))
                (folder / "input" / "export_record.json").write_text(json.dumps({
                    "final_aq_count": 1, "assertions": ["a"]}))
                (folder / "evaluation" / "summary.json").write_text(json.dumps({
                    "active_families": ["F1", "F2"], "assertion_count": 1,
                    "macro_full_set_percent_lower": 50,
                    "macro_full_set_percent_upper": 50,
                    "macro_curve": [
                        {"k": 0, "macro_percent_lower": 0, "macro_percent_upper": 0},
                        {"k": 1, "macro_percent_lower": 50, "macro_percent_upper": 50}]}))
            rows = report_existing_module(module, root, ["qwen"], ["original"])
            self.assertEqual([row["status"] for row in rows], ["PASS"] * 3)
            model_dir = root / "block_assertions" / "original" / "qwen"
            self.assertEqual(json.loads((model_dir / "average.json").read_text())["average_full_set_percent"], 50)
            self.assertEqual(len((model_dir / "model_results.csv").read_text().splitlines()), 5)

    def test_resume_skips_only_fresh_complete_jasper_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "rtl").mkdir()
            rtl = root / "rtl" / "top.sv"
            rtl.write_text("module top; endmodule\n")
            mutant = root / "mutant.sv"
            mutant.write_text("module top; wire x; endmodule\n")
            assertion = root / "aq.sv"
            assertion.write_text("module props; endmodule\n")
            config = {"project_root": str(root), "top": "top", "rtl_files": ["rtl/top.sv"],
                      "assertion_file": "aq.sv", "clock_mode": "none", "assertions": ["a"]}
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            run_dir = root / "run"
            (run_dir / "evaluation").mkdir(parents=True)
            (run_dir / "jasper").mkdir()
            item = {"id": "F1_001", "family": "F1", "replace": "rtl/top.sv", "file": str(mutant)}
            validated = {"mutants": [item]}
            (run_dir / "manifest.json").write_text(json.dumps({"assertions": ["a"], "mutants": [item]}))
            (run_dir / "evaluation" / "summary.json").write_text(json.dumps({"assertion_count": 1}))
            (run_dir / "jasper" / "matrix.csv").write_text("assertion_id,mutant_id,status\na,F1_001,CEX\n")
            for job_id, source in (("golden", rtl), ("F1_001", mutant)):
                job = run_dir / "jasper" / "runs" / job_id
                job.mkdir(parents=True)
                tcl = make_tcl(config, [source], assertion, [], [], [])
                fingerprint = input_fingerprint(tcl, config, [source, assertion], [])
                (job / "result.json").write_text(json.dumps({"input_sha256": fingerprint,
                                                                "properties": {"a": "PROVEN" if job_id == "golden" else "CEX"}}))
            record = {"config": str(config_path), "assertions": ["a"]}
            self.assertEqual(read_matrix(run_dir / "jasper" / "matrix.csv", ["a"], {"F1_001": "F1"}),
                             {("a", "F1_001"): "CEX"})
            for job_id, source in (("golden", rtl), ("F1_001", mutant)):
                cached = json.loads((run_dir / "jasper" / "runs" / job_id / "result.json").read_text())
                recomputed = input_fingerprint(make_tcl(config, [source], assertion, [], [], []),
                                               config, [source, assertion], [])
                self.assertEqual(cached["input_sha256"], recomputed)
            self.assertTrue(cached_run_fresh(record, run_dir, validated))
            rtl.write_text("module top; wire changed; endmodule\n")
            self.assertFalse(cached_run_fresh(record, run_dir, validated))
