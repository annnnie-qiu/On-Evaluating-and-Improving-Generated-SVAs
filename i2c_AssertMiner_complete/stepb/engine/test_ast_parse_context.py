import tempfile
import unittest
from pathlib import Path

from generate_mutants_ast import generate_ast


class ParseContextTests(unittest.TestCase):
    def test_include_paths_and_configured_defines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "headers").mkdir()
            (root / "headers/defs.vh").write_text("`define VALUE 1'b0\n")
            source = ('`include "defs.vh"\n'
                      'module m(input clk, a, b, output reg q);\n'
                      '`ifdef ENABLE_UPDATE\n'
                      'always @(posedge clk) q <= a + b;\n'
                      '`else\n'
                      'always @(posedge clk) q <= `VALUE;\n'
                      '`endif\nendmodule\n')
            target = root / "main.v"
            target.write_text(source)
            without_define = generate_ast(source, source_path=target, include_dirs=[root / "headers"])
            with_define = generate_ast(source, source_path=target, include_dirs=[root / "headers"],
                                       defines=["ENABLE_UPDATE=1"])
            self.assertFalse(without_define)
            self.assertTrue(any(c.kind == "state_update_operator" for c in with_define))

    def test_target_directory_is_an_include_search_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "defs.vh").write_text("`define VALUE 1'b0\n")
            source = ('`include "defs.vh"\nmodule m(input clk, a, b, output reg q);\n'
                      'always @(posedge clk) if (a) q <= a + b; else q <= `VALUE;\nendmodule\n')
            self.assertTrue(generate_ast(source, source_path=root / "main.v"))

    def test_nonfatal_warning_is_reported_but_does_not_block(self):
        source = 'module m(input clk, a, b, output reg q);\nalways @(posedge clk) q <= a + b;\nendmodule'
        self.assertTrue(generate_ast(source))

    def test_parse_errors_keep_full_diagnostics(self):
        with self.assertRaisesRegex(ValueError, "(?s)parse errors.*missing_header"):
            generate_ast('`include "missing_header.vh"\nmodule m; endmodule\n')

    def test_header_and_macro_expansion_offsets_are_not_mutated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "header.vh").write_text(
                'module header(input clk, a, b, output reg q);\n'
                'always @(posedge clk) q <= a + b;\nendmodule\n'
                '`define UPDATE(x,y) x + y\n')
            source = ('`include "header.vh"\nmodule main(input clk, a, b, output reg q);\n'
                      'always @(posedge clk) q <= `UPDATE(a,b);\nendmodule\n')
            self.assertFalse(generate_ast(source, source_path=root / "main.v"))


if __name__ == "__main__":
    unittest.main()
