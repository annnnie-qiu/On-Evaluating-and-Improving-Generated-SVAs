"""Run the production Tcl selectors against nested original checker scopes.

Only Jasper commands are replaced by fake_jasper.py. Selection, uniqueness,
exception handling and status classification execute in the real Tcl scripts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "vendor" / "faults"))
from collect_jasper import make_tcl, parse_log, tcl_atom
from validate_mutants import syntax_tcl


class NestedTargetTests(unittest.TestCase):
    bound = "bsg_cache_buffer_queue.bsg_cache_buffer_queue_assertions_inst"
    label = "queue_empty"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.rtl = self.folder / "queue.sv"
        self.source = self.folder / "checker.sv"
        self.bind = self.folder / "bind.sv"
        self.rtl.write_text("module queue; endmodule\n")
        self.source.write_text("""module queue_assertions;
always_comb begin : combinational_output_assertions
  queue_empty: assert (empty_o == (count == 0));
end
endmodule
""")
        self.bind.write_text("bind queue queue_assertions check_i (.*);\n")
        self.config = {"top": "queue", "clock_mode": "none", "assertions": [self.label],
                       "target_scope": self.bound}

    def execute(self, script, statuses):
        path = self.folder / "run.tcl"
        path.write_text(script)
        env = {**os.environ, "LOOP_TEST_PROPERTIES_JSON": json.dumps(statuses)}
        result = subprocess.run([sys.executable, str(ROOT / "tests" / "fake_jasper.py"),
                                 "-batch", str(path)], text=True, capture_output=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def aq_script(self, stage="syntax", *, scope=None, label=None):
        values = {
            "stage_tcl": tcl_atom(stage), "project_root_tcl": tcl_atom(self.folder),
            "assertion_file_tcl": tcl_atom(self.source), "top_tcl": "{queue}",
            "property_name_tcl": tcl_atom(label or self.label),
            "target_property_name_tcl": tcl_atom(label or self.label),
            "target_scope_tcl": tcl_atom(self.bound if scope is None else scope),
            "assertion_kind_tcl": "{immediate}", "vacuity_mode_tcl": "{witness}",
            "rtl_filelist_tcl": "{}", "rtl_files_tcl": tcl_atom(self.rtl),
            "include_dirs_tcl": "", "defines_tcl": "", "bind_files_tcl": tcl_atom(self.bind),
            "clock_mode_tcl": "{none}", "clock_signal_tcl": "{}", "reset_expression_tcl": "{}", "reset_pins_tcl": "{}",
        }
        template = (ROOT / "vendor" / "aq" / "aq_jasper_generic.tcl").read_text()
        return re.sub(r"\{\{(\w+)\}\}", lambda match: values[match[1]], template)

    def fh_script(self, *, syntax=False, scope=None, label=None):
        config = {**self.config}
        if scope is not None:
            config["target_scope"] = scope
        if label is not None:
            config["assertions"] = [label]
        args = (config, [self.rtl], self.source, [self.bind], [], [])
        return syntax_tcl(*args, 1) if syntax else make_tcl(*args)

    def test_original_queue_and_immediate_decode_scopes_pass_all_selectors(self):
        cases = [
            (self.bound, "combinational_output_assertions", "queue_empty"),
            ("bsg_decode.bsg_decode_assertions_inst", "field_exact_assertions", "opcode_exact"),
        ]
        for scope, block, label in cases:
            for prefix in ("", "<embedded>::", "wrapper.<embedded>::"):
                name = f"{prefix}{scope}.{block}.{label}"
                statuses = {name: "proven", f"{scope}.i_other.{label}_suffix": "cex",
                            f"queue.native.{label}": "cex"}
                with self.subTest(scope=scope, prefix=prefix):
                    text = self.execute(self.aq_script(scope=scope, label=label), statuses)
                    self.assertIn(f"AQ_TARGET\t{name}\n", text)
                    self.assertIn("AQ_SYNTAX_PASS", text)
                    text = self.execute(self.aq_script("fpv", scope=scope, label=label), statuses)
                    self.assertIn("AQ_FPV_PROVEN", text)
                    self.assertNotIn("AQ_FPV_CEX", text)
                    text = self.execute(self.fh_script(syntax=True, scope=scope, label=label), statuses)
                    self.assertIn("FH_SYNTAX_PASS", text)
                    text = self.execute(self.fh_script(scope=scope, label=label), statuses)
                    self.assertEqual(parse_log(text, [label]), {label: "PROVEN"})

    def test_wrong_instance_and_native_label_cannot_supply_a_target(self):
        wrong_names = [
            f"<embedded>::bsg_cache_buffer_queue.i_assertions.block.{self.label}",
            f"<embedded>::evil{self.bound}.block.{self.label}",
            f"<embedded>::{self.bound}_other.block.{self.label}",
            f"<embedded>::{self.bound}2.block.{self.label}",
            f"<embedded>::{self.bound}.block.{self.label}_suffix",
            f"<embedded>::{self.bound}.block.not_{self.label}",
            f"bsg_cache_buffer_queue.native.{self.label}",
        ]
        for name in wrong_names:
            with self.subTest(name=name):
                statuses = {name: "proven"}
                text = self.execute(self.aq_script(), statuses)
                self.assertIn("AQ_TARGET_ERROR", text)
                self.assertNotIn("AQ_SYNTAX_PASS", text)
                text = self.execute(self.fh_script(syntax=True), statuses)
                self.assertIn("FH_SYNTAX_FAIL", text)
                self.assertNotIn("FH_SYNTAX_PASS", text)
                text = self.execute(self.fh_script(), statuses)
                self.assertIn("target assertion mapping failed", text)
                self.assertNotIn("FH_DONE", text)

    def test_same_label_in_other_checker_is_background(self):
        target = f"<embedded>::{self.bound}.combinational_output_assertions.{self.label}"
        statuses = {target: "proven",
                    f"<embedded>::bsg_cache_buffer_queue.i_assertions.block.{self.label}": "cex",
                    f"bsg_cache_buffer_queue.native.{self.label}": "cex"}
        text = self.execute(self.aq_script("fpv"), statuses)
        self.assertIn("AQ_FPV_PROVEN", text)
        self.assertIn("target=1 background=2", text)
        text = self.execute(self.fh_script(), statuses)
        self.assertEqual(parse_log(text, [self.label]), {self.label: "PROVEN"})
        self.assertEqual(text.count("FH_PROPERTY\t"), 1)

    def test_duplicate_nested_labels_in_selected_instance_are_ambiguous(self):
        statuses = {f"<embedded>::{self.bound}.block_a.{self.label}": "proven",
                    f"<embedded>::{self.bound}.block_b.{self.label}": "cex"}
        text = self.execute(self.aq_script(), statuses)
        self.assertIn("AQ_TARGET_ERROR", text)
        self.assertIn("matches=2", text)
        self.assertNotIn("AQ_SYNTAX_PASS", text)
        text = self.execute(self.fh_script(syntax=True), statuses)
        self.assertIn("FH_SYNTAX_FAIL", text)
        self.assertIn("matches=2", text)
        text = self.execute(self.fh_script(), statuses)
        self.assertIn("matches=2", text)
        self.assertNotIn("FH_DONE", text)

    def test_scope_with_generate_index_uses_literal_matching(self):
        scope = "queue.gen[2].check_i"
        target = f"<embedded>::{scope}.output_checks.{self.label}"
        statuses = {target: "proven", f"queue.gen2.check_i.output_checks.{self.label}": "cex",
                    f"queue.gen[20].check_i.output_checks.{self.label}": "cex"}
        text = self.execute(self.aq_script("fpv", scope=scope), statuses)
        self.assertIn("AQ_FPV_PROVEN", text)
        text = self.execute(self.fh_script(scope=scope), statuses)
        self.assertEqual(parse_log(text, [self.label]), {self.label: "PROVEN"})

    def test_nested_vacuity_witness_keeps_instance_and_final_label(self):
        witness = "AQ_ANT_REACH_queue_empty"
        target = f"<embedded>::{self.bound}.combinational_output_assertions.{witness}"
        wrong = f"<embedded>::bsg_cache_buffer_queue.i_assertions.block.{witness}"
        for status, token in (("cex", "AQ_NON_VACUOUS"), ("proven", "AQ_VACUOUS")):
            with self.subTest(status=status):
                text = self.execute(self.aq_script("vacuity", label=witness),
                                    {target: status, wrong: "cex" if status == "proven" else "proven"})
                self.assertIn("\n" + token + "\n", text)


if __name__ == "__main__":
    unittest.main()
