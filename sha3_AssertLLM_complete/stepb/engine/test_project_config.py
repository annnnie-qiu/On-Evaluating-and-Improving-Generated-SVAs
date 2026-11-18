import json
import tempfile
import unittest
from pathlib import Path

from project_config import resolve_settings
from run_batch import module_configs


class ProjectConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'rtl').mkdir()
        (self.root / 'assertions').mkdir()
        (self.root / 'rtl/aaa_pkg.sv').write_text('package aaa_pkg; endpackage')
        (self.root / 'rtl/decoder.sv').write_text('module decoder; endmodule')
        (self.root / 'rtl/zzz_unrelated.sv').write_text('module other; endmodule')
        self.source = self.root / 'assertions/decoder_assertions.v'
        self.source.write_text('module decoder_assertions; a: assert property (1); endmodule\n'
                               'bind decoder decoder_assertions checker_instance();')
        self.config = {'rtl': {'auto_discover': True, 'include_dirs': ['rtl']},
            'formal': {'clock_mode': 'auto'}, 'bind': {'mode': 'none'},
            'units': {'mapping': {'*bsg_cache_sbuf_assertions.v': {
                'top': 'bsg_cache_sbuf', 'rtl_files': ['rtl/cache.sv']}}},
            'jasper': {'executable': 'jg'}, 'stages': {'syntax': {}, 'fpv': {}}}

    def test_cache_mapping_does_not_block_dataflow_automatic_resolution(self):
        manifests = self.root / 'aq_out/manifests'
        manifests.mkdir(parents=True)
        (manifests / 'inventory_files.json').write_text(json.dumps([
            {'assertion_file': 'decoder_assertions.v', 'top': 'decoder'}]))
        path = self.root / 'aq.json'
        path.write_text(json.dumps(self.config))
        before = path.read_bytes()
        modules = module_configs(self.root, self.root / 'out', path)
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]['target'], 'rtl/decoder.sv')
        resolved = json.loads(Path(modules[0]['base_config']).read_text())
        self.assertEqual(resolved['top'], 'decoder')
        self.assertEqual(resolved['clock_mode'], 'none')
        self.assertEqual(resolved['bind_files'], [])
        self.assertEqual(resolved['rtl_files'], ['rtl/aaa_pkg.sv', 'rtl/decoder.sv', 'rtl/zzz_unrelated.sv'])
        self.assertEqual(path.read_bytes(), before)

    def test_clock_and_reset_inference_matches_existing_step_a_rule(self):
        self.source.write_text('module decoder_assertions;\n'
            'a: assert property (@(posedge clk_i) disable iff (reset_i) a |=> b); endmodule')
        resolved, target, _ = resolve_settings(self.root, self.config, self.source.name, {})
        self.assertEqual((resolved['clock_mode'], resolved['clock'], resolved['reset_expression']),
                         ('clocked', 'clk_i', 'reset_i'))
        self.assertEqual(target, 'rtl/decoder.sv')

    def test_exact_mapping_takes_precedence_over_wildcard(self):
        self.config['units']['mapping'] = {'*': {'top': 'wrong'},
            self.source.name: {'top': 'decoder', 'rtl_files': ['rtl/decoder.sv'], 'clock_mode': 'none'}}
        resolved, _, _ = resolve_settings(self.root, self.config, self.source.name, {})
        self.assertEqual(resolved['top'], 'decoder')
        self.assertEqual(resolved['rtl_files'], ['rtl/decoder.sv'])

    def test_multiple_clocks_are_rejected_instead_of_guessed(self):
        self.source.write_text('module decoder_assertions;\n'
            'a: assert property (@(posedge clk1) 1);\n'
            'b: assert property (@(posedge clk2) 1); endmodule')
        with self.assertRaisesRegex(ValueError, 'multiple SVA clocks'):
            resolve_settings(self.root, self.config, self.source.name, {})

    def test_filelist_is_not_silently_replaced_by_auto_discovery(self):
        self.config['rtl']['filelist'] = 'rtl/files.f'
        with self.assertRaisesRegex(ValueError, 'filelist'):
            resolve_settings(self.root, self.config, self.source.name, {})


if __name__ == '__main__':
    unittest.main()
