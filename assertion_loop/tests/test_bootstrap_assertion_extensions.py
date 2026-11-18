"""First-run plans must infer formal context from the selected checker source."""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from looplib.common import read
from looplib.llm import Client
import run


class BootstrapAssertionExtensionTests(unittest.TestCase):
    def make_dataset(self, parent, suffix, *, immediate=False):
        dataset = Path(parent) / ("dataset_" + suffix[1:] + ("_immediate" if immediate else ""))
        for directory in ("rtl", "spec", "bind", "assertions/nested"):
            (dataset / directory).mkdir(parents=True)
        # Distinct RTL-native clock/reset make a fallback-to-RTL error visible.
        (dataset / "rtl/toy.sv").write_text(
            "module toy(input clk, rtl_clk, rst_n, rtl_rst, req, output ack);\n"
            "assign ack = req;\n"
            "native_check: assert property (@(posedge rtl_clk) disable iff (rtl_rst) req |-> ack);\n"
            "endmodule\n")
        (dataset / "spec/toy.md").write_text("The output ack equals req.")
        (dataset / "bind/toy_bind.sv").write_text("bind toy checker fixed_i (.*);\n")
        expression = ("always_comb begin\n  original: assert (req == ack);\nend\n" if immediate else
                      "original: assert property (@(negedge clk) disable iff (!rst_n) req |-> ack);\n")
        source = ("module checker(input clk, rtl_clk, rst_n, rtl_rst, req, ack);\n"
                  "logic original_helper;\nassign original_helper = req;\n" + expression + "endmodule\n")
        path = dataset / "assertions/nested" / ("arbitrary_filename" + suffix)
        path.write_text(source)
        return dataset, path, source

    def plan(self, dataset):
        with patch.object(Client, "request") as request, patch.object(run, "run_unit") as workflow, redirect_stdout(io.StringIO()):
            status = run.main(ablation=True, argv=[str(dataset), "--plan", "--experiment", "first_plan"])
        self.assertEqual(status, 0)
        request.assert_not_called()
        workflow.assert_not_called()
        return read(dataset / "gen_assertions/ablation/first_plan/plan.json")

    def test_first_plan_uses_existing_clock_reset_and_helpers_for_headers_and_uppercase_sources(self):
        with tempfile.TemporaryDirectory() as parent:
            for suffix in (".vh", ".svh", ".SV"):
                with self.subTest(suffix=suffix):
                    dataset, source_file, original = self.make_dataset(parent, suffix)
                    self.assertFalse((dataset / "gen_assertions/setup/dataset.json").exists())
                    plan = self.plan(dataset)
                    unit = plan["units"]["units"][0]
                    self.assertEqual(unit["clock_mode"], "clocked")
                    self.assertEqual(unit["clock"], "clk")
                    self.assertEqual(unit["reset_expression"], "!rst_n")
                    self.assertEqual(unit["setup_note"], "Reused fixed checker")
                    self.assertEqual(unit["assertion_files"], [str(source_file.relative_to(dataset))])
                    self.assertIn("@(negedge clk) disable iff (!rst_n)", unit["assertion_slots"][0]["prefix"])
                    self.assertIn("original_helper", (dataset / unit["template"]).read_text())
                    self.assertEqual(source_file.read_text(), original)

    def test_first_plan_keeps_immediate_header_checker_combinational_despite_clocked_rtl(self):
        with tempfile.TemporaryDirectory() as parent:
            for suffix in (".VH", ".SVH", ".SV"):
                with self.subTest(suffix=suffix):
                    dataset, source_file, original = self.make_dataset(parent, suffix, immediate=True)
                    plan = self.plan(dataset)
                    unit = plan["units"]["units"][0]
                    self.assertEqual(unit["clock_mode"], "none")
                    self.assertEqual(unit["clock"], "")
                    self.assertEqual(unit["reset_expression"], "")
                    self.assertEqual(unit["setup_note"], "Reused fixed checker")
                    self.assertEqual(unit["assertion_slots"][0]["assertion_kind"], "immediate")
                    self.assertEqual(source_file.read_text(), original)


if __name__ == "__main__":
    unittest.main()
