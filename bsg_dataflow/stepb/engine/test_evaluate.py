import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from collect_jasper import classify, make_tcl, parse_log
from evaluate import FAMILIES, analyze, read_matrix
from generate_mutants import generate, parse_numeric_label
from validate_mutants import parse_validation_log, syntax_tcl


class HoldoutTests(unittest.TestCase):
    def setUp(self):
        self.assertions = ["a", "b", "c"]
        self.families = {f"{family}_m1": family for family in FAMILIES}
        self.families.update({f"{family}_m2": family for family in FAMILIES})
        self.matrix = {(a, m): "PROVEN" for a in self.assertions for m in self.families}

    def test_fixed_mutant_count_and_monotone_curve(self):
        for family in FAMILIES:
            self.matrix["a", f"{family}_m1"] = "CEX"
            self.matrix["b", f"{family}_m2"] = "CEX"
        rows, summary = analyze(self.assertions, self.families, self.matrix)
        for family in FAMILIES:
            fold = [r for r in rows if r["held_out_family"] == family]
            self.assertEqual([r["k"] for r in fold], [0, 1, 2, 3])
            self.assertEqual([r["test_mutants"] for r in fold], [2, 2, 2, 2])
            self.assertEqual([r["detection_percent_lower"] for r in fold], [0, 50, 100, 100])
        self.assertEqual(summary["macro_full_set_percent_lower"], 100)

    def test_held_out_data_does_not_change_selection(self):
        rows, summary1 = analyze(self.assertions, self.families, self.matrix)
        self.matrix["c", "F1_m1"] = "CEX"
        self.matrix["c", "F1_m2"] = "CEX"
        _, summary2 = analyze(self.assertions, self.families, self.matrix)
        self.assertEqual(summary1["held_out_results"]["F1"]["selection_order"],
                         summary2["held_out_results"]["F1"]["selection_order"])

    def test_only_nonempty_families_are_held_out(self):
        families = {"F1_m1": "F1", "F4_m1": "F4"}
        matrix = {(a, m): "PROVEN" for a in self.assertions for m in families}
        rows, summary = analyze(self.assertions, families, matrix)
        self.assertEqual(summary["active_families"], ["F1", "F4"])
        self.assertEqual(summary["missing_families"], ["F2", "F3", "F5"])
        self.assertEqual({r["held_out_family"] for r in rows}, {"F1", "F4"})

    def test_unknown_is_not_silently_counted_as_proven(self):
        self.matrix["a", "F1_m1"] = "UNKNOWN"
        rows, _ = analyze(self.assertions, self.families, self.matrix)
        row = next(r for r in rows if r["held_out_family"] == "F1" and r["k"] == 1)
        self.assertEqual(row["detection_percent_lower"], 0)
        self.assertEqual(row["detection_percent_upper"], 50)

    def test_evaluator_writes_five_fold_plots_and_one_average_plot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "manifest.json"
            matrix = root / "matrix.csv"
            output = root / "evaluation"
            manifest.write_text(json.dumps({
                "assertions": self.assertions,
                "mutants": [{"id": mutant, "family": family}
                            for mutant, family in self.families.items()],
            }))
            matrix.write_text("assertion_id,mutant_id,status\n" + "".join(
                f"{assertion},{mutant},{status}\n"
                for (assertion, mutant), status in self.matrix.items()))
            subprocess.run([sys.executable, str(Path(__file__).parent / "evaluate.py"),
                            "--manifest", str(manifest), "--matrix", str(matrix),
                            "--output", str(output)], check=True, capture_output=True, text=True)
            plots = sorted(path.name for path in output.glob("*.svg"))
            self.assertEqual(plots, [*(f"{family}_curve.svg" for family in FAMILIES),
                                     "macro_curve.svg"])
            for plot in plots:
                self.assertIn("<polyline", (output / plot).read_text())
            plot_data = json.loads((output / "plot_data.json").read_text())
            self.assertEqual(set(plot_data["families"]), set(FAMILIES))
            self.assertEqual(len(plot_data["macro_average"]["points"]), len(self.assertions) + 1)
            for family in FAMILIES:
                self.assertEqual(len(plot_data["families"][family]["points"]), len(self.assertions) + 1)
                self.assertEqual(plot_data["families"][family]["test_mutants"], 2)

    def test_matrix_requires_all_cells(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "matrix.csv"
            path.write_text("assertion_id,mutant_id,status\na,F1_m1,CEX\n")
            with self.assertRaisesRegex(ValueError, "incomplete"):
                read_matrix(path, self.assertions, self.families)

    def test_jasper_property_mapping(self):
        text = "FH_PROPERTY\t<embedded>::top.bind.a\tcex\nFH_PROPERTY\t<embedded>::top.bind.b\tproven\nFH_DONE\n"
        self.assertEqual(parse_log(text, ["a", "b"]), {"a": "CEX", "b": "PROVEN"})
        self.assertEqual(classify("bounded_proven"), "UNKNOWN")
        with self.assertRaisesRegex(ValueError, "finish cleanly"):
            parse_log('puts "FH_DONE"\n' + text.replace("FH_DONE\n", ""), ["a", "b"])

    def test_tcl_renders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rtl = root / "rtl.sv"
            assertion = root / "assert.sv"
            rtl.write_text("module top; endmodule\n")
            assertion.write_text("module a; endmodule\n")
            tcl = make_tcl({"top": "top", "clock_mode": "none"}, [rtl], assertion, [], [], [])
            self.assertIn("prove -all", tcl)
            self.assertIn("FH_PROPERTY", tcl)
            self.assertIn("clock -none", tcl)

    def test_generator_discovers_five_kinds_without_editing_original(self):
        source = """module top;
logic [1:0] state;
always_comb begin
  case (state)
    2'b00: x = 1'b0;
    2'b01: x = 1'b1;
    default: x = 1'b0;
  endcase
end
always_ff @(posedge clk) begin
  if (reset) state <= 2'b00;
  else state <= state + step;
end
assign y = sel ? a : b;
assign z = a;
assign w = b;
endmodule
"""
        candidates = generate(source)
        self.assertEqual({c.family for c in candidates}, set(FAMILIES))
        self.assertTrue(any(c.family == "F3" and c.replacement == "!reset" for c in candidates))
        self.assertIn("state <= state + step;", source)

    def test_ast_backend_discovers_five_kinds_when_pyslang_available(self):
        try:
            import pyslang  # noqa: F401
        except ImportError:
            self.skipTest("pyslang is not installed")
        from generate_mutants_ast import generate_ast
        source = """module top(input logic clk, reset, step, sel, a, b,
output logic x, output logic y, output logic z, output logic w);
logic [1:0] state;
always_comb begin
  case (state)
    2'b00: x = 1'b0;
    2'b01: x = 1'b1;
    default: x = 1'b0;
  endcase
end
always_ff @(posedge clk) begin
  if (reset) state <= 2'b00;
  else state <= state + step;
end
assign y = sel ? a : b;
assign z = a;
assign w = b;
endmodule
"""
        self.assertEqual({candidate.family for candidate in generate_ast(source)}, set(FAMILIES))
        kinds = {candidate.kind for candidate in generate_ast(source)}
        self.assertIn("ternary_condition_inversion_CMC", kinds)
        self.assertIn("case_output_value_change_AVC", kinds)

    def test_ast_expands_candidates_without_parse_errors(self):
        try:
            import pyslang
        except ImportError:
            self.skipTest("pyslang is not installed")
        from generate_mutants_ast import generate_ast
        source = """module m(input logic clk, sel, a, b, output logic y);
always_ff @(posedge clk) begin
  if (sel) y <= 1'b1; else y <= a + b;
end
assign z = sel ? a : b;
endmodule
"""
        variants = generate_ast(source)
        self.assertTrue(any(v.kind == "assignment_value_change_AVC" for v in variants))
        self.assertTrue(any(v.kind == "if_guard_inversion" for v in variants))
        for variant in variants:
            mutant = source[:variant.start] + variant.replacement + source[variant.end:]
            self.assertFalse(pyslang.syntax.SyntaxTree.fromText(mutant).diagnostics, variant)

    def test_case_changes_do_not_duplicate_existing_numeric_labels(self):
        source = """module top;
case (state)
  2'b00: x = 0;
  2'b01: x = 1;
  2'b10: x = 2;
endcase
endmodule
"""
        variants = [c for c in generate(source) if c.family == "F4"]
        self.assertTrue(variants)
        self.assertEqual({parse_numeric_label(c.replacement) for c in variants}, {3})
        self.assertNotIn("2'b00", {c.replacement for c in variants})

    def test_syntax_gate_tokens(self):
        self.assertEqual(parse_validation_log("FH_SYNTAX_PASS\n", 0)[0], "PASS")
        self.assertEqual(parse_validation_log("FH_SYNTAX_FAIL duplicate case item\n", 0)[0], "FAIL")
        self.assertEqual(parse_validation_log('puts "FH_SYNTAX_PASS"\n', 0)[0], "FAIL")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rtl, assertion = root / "top.sv", root / "aq.sv"
            rtl.write_text("module top; endmodule\n")
            assertion.write_text("module aq; endmodule\n")
            tcl = syntax_tcl({"top": "top"}, [rtl], assertion, [], [], [], 2)
            self.assertIn("analyze -sv", tcl)
            self.assertIn("elaborate -top $FH_TOP", tcl)
            self.assertIn("!= 2", tcl)

    def test_comparison_has_multiple_variants_and_no_default_twenty_cap(self):
        source = "module top;\n" + "if (a > b) x = a;\n" * 6 + "endmodule\n"
        variants = [c for c in generate(source) if c.family == "F3"]
        self.assertEqual(len(variants), 24)
        self.assertEqual({c.replacement for c in variants}, {">=", "<", "<=", "=="})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rtl = root / "top.sv"
            rtl.write_text(source)
            config = root / "config.json"
            config.write_text(json.dumps({
                "project_root": str(root), "rtl_files": ["top.sv"], "assertions": ["a"],
            }))
            output = root / "generated"
            subprocess.run([sys.executable, str(Path(__file__).parent / "generate_mutants.py"),
                            "--config", str(config), "--target", "top.sv",
                            "--output", str(output)], check=True, capture_output=True, text=True)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(sum(m["family"] == "F3" for m in manifest["mutants"]), 24)
            site_counts = {}
            for mutant in manifest["mutants"]:
                if mutant["family"] == "F3":
                    site_counts[mutant["site_id"]] = site_counts.get(mutant["site_id"], 0) + 1
            self.assertEqual(sorted(site_counts.values()), [4] * 6)

    def test_generate_then_jasper_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rtl").mkdir()
            rtl = root / "rtl" / "top.sv"
            rtl.write_text("""module top(input clk, reset, step, sel, a, b,
output logic x, output y, output z, output w);
logic [1:0] state;
always_comb begin
case (state)
2'b00: x = 1'b0;
2'b01: x = 1'b1;
default: x = 1'b0;
endcase
end
always_ff @(posedge clk) begin
if (reset) state <= 2'b00;
else state <= state + step;
end
assign y = sel ? a : b;
assign z = a;
assign w = b;
endmodule
""")
            assertion = root / "aq.sv"
            assertion.write_text("module aq; a: assert property (1'b1); endmodule\n")
            config = root / "config.json"
            config.write_text(json.dumps({
                "project_root": str(root), "top": "top", "rtl_files": ["rtl/top.sv"],
                "assertion_file": "aq.sv", "assertions": ["a"],
                "clock_mode": "clocked", "clock": "clk", "reset_expression": "reset",
            }))
            scripts = Path(__file__).parent
            generated = root / "generated"
            subprocess.run([sys.executable, str(scripts / "generate_mutants.py"),
                            "--config", str(config), "--target", "rtl/top.sv",
                            "--output", str(generated), "--max-per-family", "1"], check=True,
                           capture_output=True, text=True)
            manifest = generated / "manifest.json"
            self.assertEqual({m["family"] for m in json.loads(manifest.read_text())["mutants"]},
                             set(FAMILIES))
            output = root / "runs"
            validation = root / "validation"
            subprocess.run([sys.executable, str(scripts / "validate_mutants.py"),
                            "--config", str(config), "--manifest", str(manifest),
                            "--output", str(validation), "--dry-run"], check=True,
                           capture_output=True, text=True)
            self.assertEqual(len(list((validation / "runs").glob("*/run.tcl"))), 5)
            subprocess.run([sys.executable, str(scripts / "collect_jasper.py"),
                            "--config", str(config), "--manifest", str(manifest),
                            "--output", str(output), "--dry-run"], check=True,
                           capture_output=True, text=True)
            self.assertEqual(len(list((output / "runs").glob("*/run.tcl"))), 5)
            one_click = root / "one_click"
            subprocess.run([sys.executable, str(scripts / "run_all.py"),
                            "--config", str(config), "--target", "rtl/top.sv",
                            "--output", str(one_click), "--backend", "lexical",
                            "--max-per-family", "1", "--dry-run"], check=True,
                           capture_output=True, text=True)
            self.assertEqual(len(list((one_click / "validation" / "runs").glob("*/run.tcl"))), 5)
            self.assertEqual(len(list((one_click / "jasper" / "runs").glob("*/run.tcl"))), 5)
            try:
                import pyslang  # noqa: F401
            except ImportError:
                pass
            else:
                ast_click = root / "one_click_ast"
                subprocess.run([sys.executable, str(scripts / "run_all.py"),
                                "--config", str(config), "--target", "rtl/top.sv",
                                "--output", str(ast_click), "--max-per-family", "1",
                                "--dry-run"], check=True, capture_output=True, text=True)
                self.assertEqual(len(list((ast_click / "validation" / "runs").glob("*/run.tcl"))), 5)

    def test_validation_manifest_contains_only_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            golden, mutant, assertion = root / "golden.sv", root / "mutant.sv", root / "aq.sv"
            golden.write_text("module top; endmodule\n")
            mutant.write_text("module top; endmodule\n")
            assertion.write_text("module aq; endmodule\n")
            fake_jg = root / "fake_jg"
            fake_jg.write_text("#!/usr/bin/env python3\nimport sys\n"
                               "print('FH_SYNTAX_FAIL test rejection' if 'F1_001' in sys.argv[-1] "
                               "else 'FH_SYNTAX_PASS')\n")
            fake_jg.chmod(0o755)
            config = root / "config.json"
            config.write_text(json.dumps({
                "project_root": str(root), "top": "top", "rtl_files": ["golden.sv"],
                "assertion_file": "aq.sv", "jasper_executable": str(fake_jg),
            }))
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "assertions": ["a"],
                "mutants": [{"id": "F1_001", "family": "F1", "replace": "golden.sv", "file": "mutant.sv"},
                            {"id": "F2_001", "family": "F2", "replace": "golden.sv", "file": "mutant.sv"}],
            }))
            output = root / "validated"
            subprocess.run([sys.executable, str(Path(__file__).parent / "validate_mutants.py"),
                            "--config", str(config), "--manifest", str(manifest),
                            "--output", str(output)], check=True, capture_output=True, text=True)
            accepted = json.loads((output / "validated_manifest.json").read_text())["mutants"]
            self.assertEqual([item["id"] for item in accepted], ["F2_001"])


if __name__ == "__main__":
    unittest.main()
