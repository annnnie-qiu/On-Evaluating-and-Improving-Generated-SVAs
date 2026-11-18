from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from looplib.common import EnvironmentSetupError
from looplib.inputs import ADDITION_MARKER, load_existing, prepare_existing, render_slots
from looplib.properties import validate_expression


class ExistingInputsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.dataset = Path(self.temporary.name) / "toy"
        (self.dataset / "assertions").mkdir(parents=True)
        self.setup = {"version": 1, "units": [{"name": "toy", "checker": "toy_checks",
                       "clock_mode": "clocked", "clock": "clk", "reset_expression": "rst"}]}

    def source(self, content, name="existing.sv"):
        path = self.dataset / "assertions" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))
        return path

    def prepared(self):
        imported = prepare_existing(self.dataset, self.setup)
        unit = imported["units"][0]
        template = (self.dataset / unit["template"]).read_bytes().decode("utf-8")
        return unit, template, load_existing(self.dataset, unit)

    def render(self, template, unit, properties, validator=None):
        return render_slots(template, unit, properties,
                            validator or validate_expression,
                            lambda prop: f"{prop['label']}: assert ({prop['expression']});")

    def test_import_does_not_modify_input_setup_or_source_or_create_dataset_json(self):
        content = "module toy_checks(input clk, rst, req, ack);\n a: assert property (@(posedge clk) disable iff (rst) req |-> ack);\nendmodule\n"
        path = self.source(content)
        before = copy.deepcopy(self.setup)
        unit, template, properties = self.prepared()
        self.assertEqual(self.setup, before)
        self.assertEqual(path.read_bytes(), content.encode())
        self.assertFalse((self.dataset / "gen_assertions/setup/dataset.json").exists())
        self.assertEqual(self.render(template, unit, properties), content)
        self.assertEqual(unit["assertion_files"], ["assertions/existing.sv"])
        self.assertEqual(unit["input_mode"], "existing_assertions")
        self.assertEqual(properties[0]["slot_id"], "input_0001")
        self.assertEqual(properties[0]["repair_id"], "input_0001")
        self.assertEqual(properties[0]["source_label"], "a")
        manifest = json.loads((self.dataset / unit["assertion_manifest"]).read_text())
        self.assertEqual(manifest["assertion_count"], 1)
        self.assertEqual(manifest["slots"], unit["assertion_slots"])

    def test_original_syntax_error_reaches_actual_gate_without_local_validation(self):
        content = "module toy_checks;\n a: assert property (@(negedge clk) disable iff (rst || !ready) (q == d) && && valid);\nendmodule"
        self.source(content)
        unit, template, properties = self.prepared()
        called = []
        def validator(expression, selected_unit):
            called.append(expression)
            return expression
        self.assertEqual(self.render(template, unit, properties, validator), content)
        self.assertEqual(called, [])
        properties[0]["expression"] = "(q == d) && valid"
        updated = self.render(template, unit, properties, validator)
        self.assertEqual(updated, content.replace("(q == d) && && valid", "(q == d) && valid"))
        self.assertEqual(called, ["(q == d) && valid"])
        self.assertIn("@(negedge clk) disable iff (rst || !ready)", unit["assertion_slots"][0]["prefix"])

    def test_procedural_generate_scopes_helpers_assumptions_covers_bind_unchanged(self):
        content = """module toy_checks(input clk, rst, req, ack);
  logic helper;
  always_comb helper = req;
  fixed: assume property (@(posedge clk) !rst);
  generate if (1) begin : original_scope
    first: assert property (@(posedge clk) disable iff (rst) req |-> ack);
    always_comb begin : immediate_scope
      second: assert (helper == ack);
    end
    seen: cover property (@(posedge clk) req);
  end endgenerate
endmodule
bind toy toy_checks original_i (.*);
"""
        self.source(content)
        unit, template, properties = self.prepared()
        self.assertEqual(self.render(template, unit, properties), content)
        self.assertEqual(unit["assertion_slots"][1]["assertion_kind"], "immediate")
        properties[1]["expression"] = "helper != ack"
        self.assertEqual(self.render(template, unit, properties), content.replace("assert (helper == ack)", "assert (helper != ack)"))
        with self.assertRaisesRegex(ValueError, "Only a property body"):
            properties[0]["expression"] = "req); assume property(1'b0"
            self.render(template, unit, properties)

    def test_crlf_original_bytes_preserved_and_marker_adds_no_newline(self):
        content = "module toy_checks;\r\n a: assert property (@(posedge clk)\r\n disable iff (rst) req |-> ack);\r\nendmodule"
        self.source(content)
        unit, template, properties = self.prepared()
        self.assertEqual(template.count(ADDITION_MARKER), 1)
        self.assertEqual(self.render(template, unit, properties).encode(), content.encode())

    def test_absent_assertions_become_null_statements_with_original_newlines(self):
        content = """module toy_checks;
  always_comb begin
    if (enable)
      first: assert (req == ack);
    else
      second: assert (req != ack);
  end
endmodule
"""
        self.source(content)
        unit, template, properties = self.prepared()
        rendered = self.render(template, unit, properties[1:])
        self.assertEqual(rendered, content.replace("first: assert (req == ack);", ";"))
        self.assertIn("if (enable)\n      ;\n    else", rendered)
        self.assertEqual(rendered.count("\n"), content.count("\n"))

    def test_new_properties_use_only_addition_marker(self):
        content = "module toy_checks;\n a: assert property (@(posedge clk) req |-> ack);\nendmodule\n"
        self.source(content)
        unit, template, properties = self.prepared()
        properties.append({"label": "added", "expression": "req == ack"})
        rendered = self.render(template, unit, properties)
        self.assertIn("a: assert property (@(posedge clk) req |-> ack);", rendered)
        self.assertIn("\nadded: assert (req == ack);\nendmodule", rendered)

    def test_missing_source_and_ambiguous_sources_have_actionable_errors(self):
        with self.assertRaisesRegex(EnvironmentSetupError, "found 0"):
            prepare_existing(self.dataset, self.setup)
        self.source("module toy_checks; a: assert (x); endmodule", "one.sv")
        self.source("module toy_checks; a: assert (x); endmodule", "two.v")
        with self.assertRaisesRegex(EnvironmentSetupError, "found 2"):
            prepare_existing(self.dataset, self.setup)
        self.setup["units"][0]["assertion_files"] = ["assertions/one.sv"]
        imported = prepare_existing(self.dataset, self.setup)
        self.assertEqual(imported["units"][0]["assertion_files"], ["assertions/one.sv"])

    def test_explicit_source_alias_and_invalid_mapping(self):
        self.source("module toy_checks;\n a: assert (x);\nendmodule")
        self.setup["units"][0]["assertion_source"] = "assertions/existing.sv"
        self.assertEqual(self.prepared()[0]["assertion_files"], ["assertions/existing.sv"])
        self.setup["units"][0]["assertion_files"] = ["assertions/existing.sv", "assertions/existing.sv"]
        with self.assertRaisesRegex(EnvironmentSetupError, "exactly one"):
            prepare_existing(self.dataset, self.setup)
        self.setup["units"][0]["assertion_files"] = ["assertions/missing.sv"]
        with self.assertRaisesRegex(EnvironmentSetupError, "missing"):
            prepare_existing(self.dataset, self.setup)

    def test_named_property_invocations_fail_safely_and_bare_signal_is_supported(self):
        self.source("module toy_checks;\n property p; @(posedge clk) req |-> ack; endproperty\n a: assert property (p);\nendmodule")
        with self.assertRaisesRegex(EnvironmentSetupError, "named property/sequence invocation"):
            prepare_existing(self.dataset, self.setup)
        content = "module toy_checks;\n a: assert property (@(posedge clk) valid);\nendmodule"
        self.source(content)
        unit, template, properties = self.prepared()
        self.assertEqual(properties[0]["expression"], "valid")
        self.assertEqual(self.render(template, unit, properties), content)

    def test_unnamed_assertions_get_stable_labels(self):
        content = "module toy_checks;\n assert (ready);\n assert (valid);\nendmodule"
        self.source(content)
        unit, template, properties = self.prepared()
        self.assertEqual([p["label"] for p in properties], ["loop_input_0001", "loop_input_0002"])
        self.assertEqual(unit["assertion_slots"][0]["original_statement"], "assert (ready);")
        rendered = self.render(template, unit, properties[1:])
        self.assertIn("loop_input_0002: assert (valid);", rendered)

    def test_duplicate_source_labels_and_unknown_or_duplicate_slots_fail(self):
        self.source("module toy_checks;\n a: assert (x);\n a: assert (y);\nendmodule")
        with self.assertRaisesRegex(EnvironmentSetupError, "duplicate assertion labels"):
            prepare_existing(self.dataset, self.setup)
        self.source("module toy_checks;\n a: assert (x);\nendmodule")
        unit, template, properties = self.prepared()
        with self.assertRaisesRegex(ValueError, "Duplicate assertion label"):
            self.render(template, unit, properties + properties)
        bad = copy.deepcopy(properties)
        bad[0]["slot_id"] = "input_9999"
        with self.assertRaisesRegex(ValueError, "Unknown imported"):
            self.render(template, unit, bad)

    def test_assert_action_suffix_and_original_clock_are_fixed_during_repair(self):
        content = 'module toy_checks;\n a: assert property (@(posedge clk iff enable) disable iff (!rst_n) req |-> ack) else $error("not ack");\nendmodule'
        self.source(content)
        unit, template, properties = self.prepared()
        properties[0]["expression"] = "req |=> ack"
        updated = self.render(template, unit, properties)
        self.assertEqual(updated, content.replace("req |-> ack", "req |=> ack"))

    def test_all_six_real_bsg_sources_and_one_syntax_repair_preserve_everything_else(self):
        real_folder = ROOT.parent / "stepb_delivery/audit_source/bsg_cache/assertions"
        if not real_folder.is_dir():
            self.skipTest("Optional local source dataset is not bundled with the tool")
        expected = {"bsg_cache_buffer_queue": 17, "bsg_cache_decode": 23,
                    "bsg_cache_non_blocking_decode": 53, "bsg_cache_sbuf": 8,
                    "bsg_cache_tbuf": 10, "bsg_cache_to_dram_ctrl_tx": 12}
        units = []
        originals = {}
        for name in expected:
            path = real_folder / f"{name}_assertions.v"
            content = path.read_bytes().decode()
            self.source(content, path.name)
            originals[name] = content
            units.append({"name": name, "checker": name + "_assertions",
                          "clock_mode": "none" if "decode" in name else "clocked",
                          "clock": "clk_i", "reset_expression": "reset_i"})
        imported = prepare_existing(self.dataset, {"version": 1, "units": units})
        self.assertEqual(sum(len(unit["assertion_slots"]) for unit in imported["units"]), 123)
        for unit in imported["units"]:
            name = unit["name"]
            properties = load_existing(self.dataset, unit)
            self.assertEqual(len(properties), expected[name])
            template = (self.dataset / unit["template"]).read_bytes().decode()
            # Original invalid expressions must not be locally rejected.
            def original_validator(expression, selected_unit):
                self.fail("An unchanged source assertion reached repair validation")
            rendered = self.render(template, unit, properties, original_validator)
            self.assertEqual(rendered, originals[name], name)
            if name == "bsg_cache_to_dram_ctrl_tx":
                bad = next(prop for prop in properties if prop["label"] == "a_controlled_syntax_fail")
                self.assertEqual(bad["source_line"], 145)
                previous = bad["expression"]
                bad["expression"] = previous.replace("&& &&", "&&")
                fixed = self.render(template, unit, properties)
                self.assertEqual(fixed, originals[name].replace("&& &&", "&&"))
            if name == "bsg_cache_buffer_queue":
                self.assertIn("begin : combinational_output_assertions", rendered)
                self.assertEqual(rendered.count("assume property"), 2)
            if name == "bsg_cache_decode":
                self.assertIn("always_comb begin : field_exact_assertions", rendered)
                self.assertTrue(all(slot["assertion_kind"] == "immediate" for slot in unit["assertion_slots"]))


if __name__ == "__main__":
    unittest.main()
