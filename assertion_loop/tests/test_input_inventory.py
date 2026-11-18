"""Read-only inventory and fail-before-execution coverage for input sources."""
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
from looplib.inputs import input_inventory, prepare_existing


class InputInventoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dataset = Path(temporary.name) / "dataset"
        (self.dataset / "assertions").mkdir(parents=True)

    def source(self, relative, checker, *, assertion=True):
        path = self.dataset / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "\n  a_check: assert (signal_ok);\n" if assertion else "\n  logic support;\n"
        path.write_text(f"module {checker};" + body + "endmodule\n")
        return path

    def unit(self, name, checker, **extra):
        return {"name": name, "checker": checker, "clock_mode": "none", **extra}

    def test_all_supported_extensions_are_discovered_recursively_with_no_filename_assumptions(self):
        units = []
        for ordinal, suffix in enumerate((".v", ".sv", ".vh", ".svh", ".sva"), 1):
            checker = f"arbitrary_checker_{ordinal}"
            self.source(f"assertions/nested_{ordinal}/unrelated_name{suffix}", checker)
            units.append(self.unit(f"unit_{ordinal}", checker))
        self.source("assertions/helpers/not_a_target.svh", "support", assertion=False)
        setup = {"version": 1, "units": units}
        inventory = input_inventory(self.dataset, setup)
        self.assertFalse((self.dataset / "gen_assertions").exists(), "Read-only inventory may not create outputs")
        self.assertEqual(inventory["source_file_count"], 6)
        self.assertEqual(inventory["mapped_source_file_count"], 5)
        self.assertEqual(inventory["source_assertion_count"], 5)
        self.assertEqual(inventory["mapped_unit_assertion_count"], 5)
        self.assertEqual(inventory["unassigned_files"], [])
        self.assertEqual(inventory["support_files"], ["assertions/helpers/not_a_target.svh"])
        self.assertEqual(set(inventory["supported_suffixes"]), {".v", ".sv", ".vh", ".svh", ".sva"})
        imported = prepare_existing(self.dataset, setup)
        self.assertEqual(imported["input_inventory"]["mapped_source_file_count"], 5)
        self.assertTrue((self.dataset / "gen_assertions/setup/imported/input_inventory.json").exists())

    def test_unmapped_assertion_source_is_saved_and_refused_with_bind_unit_action(self):
        self.source("assertions/selected.sv", "selected_checker")
        self.source("assertions/deep/unassigned.svh", "missing_checker")
        setup = {"version": 1, "units": [self.unit("selected", "selected_checker")]}
        inventory = input_inventory(self.dataset, setup)
        self.assertEqual(inventory["unassigned_files"], ["assertions/deep/unassigned.svh"])
        with self.assertRaisesRegex(EnvironmentSetupError, r"Unassigned.*unassigned\.svh.*missing_checker.*bind/.*unit/checker"):
            prepare_existing(self.dataset, setup)
        saved = json.loads((self.dataset / "gen_assertions/setup/imported/input_inventory.json").read_text())
        self.assertEqual(saved["unassigned_files"], inventory["unassigned_files"])
        self.assertFalse((self.dataset / "gen_assertions/setup/dataset.json").exists())

    def test_helper_header_comments_and_strings_are_non_targets(self):
        self.source("assertions/main.v", "target_checker")
        header = self.dataset / "assertions/nested/header.svh"
        header.parent.mkdir(parents=True)
        header.write_text('// assert property (a);\n/* module ignored; assert (b); endmodule */\nlocalparam string TEXT = "assert property (c);";\n')
        self.source("assertions/helper.sv", "helper_checker", assertion=False)
        imported = prepare_existing(self.dataset, {"units": [self.unit("target", "target_checker")]})
        inventory = imported["input_inventory"]
        self.assertEqual(inventory["support_files"], ["assertions/helper.sv", "assertions/nested/header.svh"])
        self.assertEqual(inventory["source_assertion_count"], 1)
        self.assertEqual(inventory["unassigned_files"], [])

    def test_explicit_mapping_selects_one_same_checker_alternative_and_reports_exclusion(self):
        self.source("assertions/primary.sv", "checker")
        self.source("assertions/alternatives/alternate.sva", "checker")
        unit = self.unit("target", "checker", assertion_files=["assertions/primary.sv"])
        imported = prepare_existing(self.dataset, {"units": [unit]})
        inventory = imported["input_inventory"]
        self.assertEqual(inventory["mapped_files"], ["assertions/primary.sv"])
        self.assertEqual(inventory["excluded_alternative_files"], ["assertions/alternatives/alternate.sva"])
        self.assertEqual(inventory["unassigned_files"], [])
        self.assertEqual(inventory["units"][0]["mapping"], "explicit_source")
        self.assertEqual(inventory["source_assertion_count"], 2)
        self.assertEqual(inventory["mapped_unit_assertion_count"], 1)
        self.source("assertions/another_unit.v", "unrelated_checker")
        with self.assertRaisesRegex(EnvironmentSetupError, "unrelated_checker"):
            prepare_existing(self.dataset, {"units": [unit]})

    def test_same_source_intentionally_mapped_to_multiple_units_is_reported_without_rejection(self):
        self.source("assertions/shared.sv", "checker")
        units = [self.unit(name, "checker", assertion_files=["assertions/shared.sv"])
                 for name in ("first_bind", "second_bind")]
        imported = prepare_existing(self.dataset, {"units": units})
        inventory = imported["input_inventory"]
        self.assertEqual(inventory["mapped_source_file_count"], 1)
        self.assertEqual(inventory["source_assertion_count"], 1)
        self.assertEqual(inventory["mapped_unit_assertion_count"], 2)
        self.assertEqual(inventory["duplicate_mappings"], [{"source_file": "assertions/shared.sv",
                         "units": ["first_bind", "second_bind"], "checkers": ["checker", "checker"]}])

    def test_additional_assertion_module_inside_mapped_file_is_not_silently_ignored(self):
        path = self.source("assertions/shared.sv", "checker")
        path.write_text(path.read_text() + "module missed_checker;\n missed: assert (valid);\nendmodule\n")
        setup = {"units": [self.unit("first", "checker", assertion_files=["assertions/shared.sv"])]}
        with self.assertRaisesRegex(EnvironmentSetupError, "missed_checker"):
            prepare_existing(self.dataset, setup)
        setup["units"].append(self.unit("second", "missed_checker", assertion_files=["assertions/shared.sv"]))
        imported = prepare_existing(self.dataset, setup)
        self.assertEqual(imported["input_inventory"]["mapped_unit_assertion_count"], 2)
        self.assertEqual(imported["input_inventory"]["unassigned_files"], [])

    def test_unbound_assertion_header_fragment_is_not_misclassified_as_support(self):
        self.source("assertions/main.sv", "checker")
        (self.dataset / "assertions/fragment.svh").write_text("standalone: assert (ready);\n")
        setup = {"units": [self.unit("main", "checker")]}
        with self.assertRaisesRegex(EnvironmentSetupError, "fragment.svh.*outside module"):
            prepare_existing(self.dataset, setup)

    def test_explicit_source_outside_assertions_is_listed_and_source_setup_remains_unchanged(self):
        self.source("fixed_checkers/checker.sv", "checker")
        setup = {"units": [self.unit("main", "checker", assertion_source="fixed_checkers/checker.sv")]}
        original = copy.deepcopy(setup)
        imported = prepare_existing(self.dataset, setup)
        self.assertEqual(setup, original)
        self.assertEqual(imported["input_inventory"]["mapped_files"], ["fixed_checkers/checker.sv"])

    def test_real_six_sources_inventory_matches_imported_assertions(self):
        folder = ROOT.parent / "stepb_delivery/audit_source/bsg_cache/assertions"
        if not folder.is_dir():
            self.skipTest("Optional local source dataset is not bundled with the tool")
        units = []
        for original in sorted(folder.glob("*.v")):
            destination = self.dataset / "assertions" / original.name
            destination.write_bytes(original.read_bytes())
            checker = original.stem
            units.append(self.unit(checker.removesuffix("_assertions"), checker))
        imported = prepare_existing(self.dataset, {"units": units})
        inventory = imported["input_inventory"]
        self.assertEqual(inventory["source_file_count"], 6)
        self.assertEqual(inventory["mapped_source_file_count"], 6)
        self.assertEqual(inventory["source_assertion_count"], 123)
        self.assertEqual(inventory["mapped_unit_assertion_count"], 123)
        self.assertEqual(inventory["unassigned_files"], [])
        self.assertEqual(inventory["unknown_count_files"], [])


if __name__ == "__main__":
    unittest.main()
