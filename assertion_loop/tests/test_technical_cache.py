"""Shared technical cache identities and fail-closed persistence, offline only."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from looplib.bootstrap import MARKER
from looplib.inputs import prepare_existing, load_existing
from looplib.technical_cache import TechnicalCache
import looplib.technical_cache as cache_module


class TechnicalCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "toy"
        for name in ("rtl", "bind", "include", "assertions", "setup", "env", "bin"):
            (self.root / name).mkdir(parents=True)
        (self.root / "rtl" / "toy.sv").write_text('`include "common.svh"\nmodule toy; endmodule\n')
        (self.root / "include" / "common.svh").write_text('`include "nested.data"\n`define WIDTH 4\n')
        (self.root / "include" / "nested.data").write_text('`define DEPTH 2\n')
        (self.root / "bind" / "toy.sv").write_text('bind toy toy_checker check_i (.*);\n')
        (self.root / "env" / "formal.sv").write_text('module formal_env; endmodule\n')
        self.template = self.root / "setup" / "checker.sv"
        self.base_template = ('module toy_checker(input logic clk,rst,req,ack);\n'
                              'wire helper = req;\n'
                              'fixed_environment: assume property (@(posedge clk) !rst);\n'
                              + MARKER + '\nendmodule\n')
        self.template.write_text(self.base_template)
        self.tool = self.root / "bin" / "jg"
        self.tool.write_text("#!/bin/sh\nexit 0\n")
        self.tool.chmod(0o755)
        self.unit = {"name": "toy", "top": "toy", "checker": "toy_checker", "target_scope": "toy.check_i",
                     "template": "setup/checker.sv", "rtl_files": ["rtl/toy.sv"],
                     "bind_files": ["bind/toy.sv"], "include_dirs": ["include"],
                     "environment_files": ["env/formal.sv"], "fixed_dependencies": [],
                     "defines": ["FORMAL=1"], "clock_mode": "clocked", "clock": "clk", "reset_expression": "rst"}
        self.settings = {"jasper_executable": str(self.tool), "jasper_version_tag": "OFFLINE_FIXTURE",
                         "syntax_timeout_seconds": 10, "proof_timeout_seconds": 20}
        self.prop = {"label": "model_a_label", "expression": "req |-> ack"}

    def cache(self, stage="fpv", *, unit=None, settings=None):
        return TechnicalCache(self.root, unit or self.unit, settings or self.settings, stage)

    def decision(self, stage="fpv", *, status=None, prop=None):
        prop = prop or self.prop
        status = status or {"syntax": "PASS", "fpv": "PROVEN", "vacuity": "PASS"}[stage]
        log = self.root / "gen_assertions" / "ablation" / "model_a" / "step_a" / "run.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("OFFLINE evidence fixture\n")
        return {"label": prop["label"], "expression": prop["expression"], "stage": stage, "status": status,
                "passed": status in {"PASS", "PROVEN", "NOT_APPLICABLE"}, "log_file": str(log)}

    def test_verified_legacy_algorithm_reuses_evidence_but_still_checks_expression(self):
        cache=self.cache();cache.store(self.prop,self.decision())
        key=cache._key(self.prop);legacy=cache._compatible_keys[key][0]
        value=json.loads(cache._path(key).read_text());value.pop("payload_sha256")
        value["key"]=legacy;value["payload_sha256"]=cache_module._digest(value)
        cache._path(legacy).parent.mkdir(parents=True,exist_ok=True)
        cache._path(legacy).write_text(json.dumps(value));cache._path(key).unlink()
        hit=self.cache().lookup(self.prop)
        self.assertIsNotNone(hit);self.assertTrue(hit["cache_compatible_revision"])
        self.assertEqual(hit["cache_legacy_key"],legacy)
        self.assertIsNone(self.cache().lookup({**self.prop,"expression":"req |=> ack"}))
        self.template.write_text(self.base_template.replace("wire helper = req","wire helper = !req"))
        self.assertIsNone(self.cache().lookup(self.prop))

    def test_same_original_and_different_model_output_paths_reuse_original_evidence(self):
        for stage in ("syntax", "fpv", "vacuity"):
            with self.subTest(stage=stage):
                first = self.cache(stage)
                decision = self.decision(stage)
                first.store(self.prop, decision)
                settings = {**self.settings, "model": "deepseek", "repeat": 7,
                            "output_dir": "/a/different/model/output", "models": {"ignored": "alias"}}
                hit = self.cache(stage, settings=settings).lookup(self.prop)
                self.assertIsNotNone(hit)
                self.assertTrue(hit["cache_hit"])
                self.assertEqual(hit["cache_origin_log"], decision["log_file"])
                shared = Path(hit["cache_source_log"])
                self.assertTrue(shared.is_relative_to(first.root))
                self.assertEqual(shared.read_bytes(), Path(decision["log_file"]).read_bytes())
                self.assertEqual(hit["log_file"], decision["log_file"])
                self.assertEqual(hit["source_label"], self.prop["label"])

    def test_unreferenced_display_label_is_rebased_without_changing_semantics(self):
        self.cache().store(self.prop, self.decision())
        other = {**self.prop, "label": "other_model_label"}
        hit = self.cache().lookup(other)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["label"], other["label"])
        self.assertEqual(hit["expression"], other["expression"])
        self.assertEqual(hit["cache_source_label"], self.prop["label"])
        self.assertEqual(hit["source_label"], self.prop["label"])

    def test_formula_or_position_wrapper_changes_do_not_reuse_proof(self):
        self.cache().store(self.prop, self.decision())
        self.assertIsNone(self.cache().lookup({**self.prop, "expression": "req |=> ack"}))
        self.template.write_text(self.base_template.replace(MARKER, "generate if (1) begin : nested\n" + MARKER + "\nend endgenerate"))
        self.assertIsNone(self.cache().lookup(self.prop))

    def test_fixed_formal_dependency_mutations_invalidate_results(self):
        mutations = {
            "clock": lambda unit, settings: unit.update(clock="other_clk"),
            "reset": lambda unit, settings: unit.update(reset_expression="!rst"),
            "target_scope": lambda unit, settings: unit.update(target_scope="toy.other_i"),
            "top": lambda unit, settings: unit.update(top="other_top"),
            "defines": lambda unit, settings: unit.update(defines=["FORMAL=2"]),
            "timeout": lambda unit, settings: settings.update(proof_timeout_seconds=21),
            "tool_version": lambda unit, settings: settings.update(jasper_version_tag="OTHER_VERSION"),
        }
        self.cache().store(self.prop, self.decision())
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                unit, settings = copy.deepcopy(self.unit), copy.deepcopy(self.settings)
                mutate(unit, settings)
                self.assertIsNone(self.cache(unit=unit, settings=settings).lookup(self.prop))
        paths = {"helper": self.template, "assumption": self.template,
                 "bind": self.root / "bind" / "toy.sv", "rtl": self.root / "rtl" / "toy.sv",
                 "header": self.root / "include" / "common.svh", "transitive_header": self.root / "include" / "nested.data",
                 "environment": self.root / "env" / "formal.sv", "tool_binary": self.tool}
        for name, path in paths.items():
            before = path.read_bytes()
            with self.subTest(name=name):
                replacement = before.replace(b"helper = req", b"helper = ack") if name == "helper" else (
                    before.replace(b"!rst", b"rst") if name == "assumption" else before + b"\n// changed formal input\n")
                path.write_bytes(replacement)
                self.assertIsNone(self.cache().lookup(self.prop))
            path.write_bytes(before)

    def test_explicit_dependency_and_stage_algorithm_contents_are_keyed(self):
        extra = self.root / "env" / "extra.conf"
        extra.write_text("mode=one\n")
        self.unit["fixed_dependencies"] = ["env/extra.conf"]
        algorithm = self.root / "test_stage_algorithm.py"
        algorithm.write_text("algorithm_revision = 1\n")
        with patch.object(cache_module, "ALGORITHM_FILES", (*cache_module.ALGORITHM_FILES, algorithm)):
            self.cache().store(self.prop, self.decision())
            self.assertIsNotNone(self.cache().lookup(self.prop))
            algorithm.write_text("algorithm_revision = 2\n")
            self.assertIsNone(self.cache().lookup(self.prop))
            algorithm.write_text("algorithm_revision = 1\n")
            extra.write_text("mode=two\n")
            self.assertIsNone(self.cache().lookup(self.prop))

    def test_all_definitive_statuses_preserve_failure_diagnostics(self):
        for stage, statuses in cache_module.DEFINITIVE.items():
            for status in statuses:
                with self.subTest(stage=stage, status=status):
                    decision = self.decision(stage, status=status)
                    if not decision["passed"]:
                        decision.update(failed_stage=stage,
                            reason="AQ_COMPILE_ERROR fixture syntax error" if status == "FAIL" else "Recorded definitive formal failure",
                            diagnostic="AQ_COMPILE_ERROR fixture syntax error\nAQ_SYNTAX_FAIL" if status == "FAIL" else "Original CEX/vacuity evidence")
                    self.cache(stage).store(self.prop, decision)
                    hit = self.cache(stage).lookup(self.prop)
                    self.assertIsNotNone(hit)
                    self.assertEqual(hit["status"], status)
                    self.assertEqual(hit.get("diagnostic"), decision.get("diagnostic"))

    def test_unknown_timeout_error_incomplete_setup_and_ambiguous_results_are_not_cached(self):
        for stage in ("syntax", "fpv", "vacuity"):
            for status in ("UNKNOWN", "TIMEOUT", "ERROR", "NOT_RUN", "UNPROCESSED"):
                with self.subTest(stage=stage, status=status):
                    decision = self.decision(stage, status=status)
                    self.cache(stage).store(self.prop, decision)
                    self.assertIsNone(self.cache(stage).lookup(self.prop))
        for diagnostic in ("AQ_TARGET_ERROR mapping failed: matches=2", "AQ_ENVIRONMENT_ERROR invalid clock",
                           "AQ_STAGE_ERROR unknown stage", "AQ_COMPILE_ERROR AQ_SETUP_ERROR: Missing source"):
            with self.subTest(diagnostic=diagnostic):
                decision = {**self.decision("syntax", status="FAIL"), "reason": diagnostic, "diagnostic": diagnostic}
                self.cache("syntax").store(self.prop, decision)
                self.assertIsNone(self.cache("syntax").lookup(self.prop))
        for extra in ({"incomplete": True}, {"completed": False}, {"passed": False}):
            with self.subTest(extra=extra):
                self.cache().store(self.prop, {**self.decision(), **extra})
                self.assertIsNone(self.cache().lookup(self.prop))
        self.cache("syntax").store(self.prop, {**self.decision("syntax", status="FAIL"), "reason": "Command exit code 1"})
        self.assertIsNone(self.cache("syntax").lookup(self.prop))

    def test_corrupt_truncated_or_tampered_entry_is_always_a_miss(self):
        cache = self.cache()
        cache.store(self.prop, self.decision())
        path = next(cache.root.rglob("*.json"))
        original = path.read_text()
        for content in ("{", "null", "[]", original.replace('"PROVEN"', '"CEX"')):
            with self.subTest(content=content[:30]):
                path.write_text(content)
                self.assertIsNone(cache.lookup(self.prop))
        path.write_text(original)
        self.assertIsNotNone(cache.lookup(self.prop))

    def test_atomic_write_failure_does_not_leave_partial_pass_or_temp_file(self):
        cache = self.cache()
        with patch.object(cache_module.os, "replace", side_effect=OSError("fixture interrupted write")):
            cache.store(self.prop, self.decision())
        self.assertIsNone(cache.lookup(self.prop))
        self.assertEqual(list(cache.root.rglob("*.tmp")), [])
        self.assertEqual(list(cache.root.rglob("*.json")), [])

    def test_referenced_label_and_assert_control_scaffold_keep_label_sensitive_key(self):
        for scaffold in ("wire ref = model_a_label;\n", "initial $assertoff;\n"):
            with self.subTest(scaffold=scaffold):
                self.template.write_text(self.base_template.replace(MARKER, scaffold + MARKER))
                cache = self.cache()
                cache.store(self.prop, self.decision())
                self.assertIsNotNone(cache.lookup(self.prop))
                self.assertIsNone(cache.lookup({**self.prop, "label": "model_b_label"}))

    def test_label_reference_in_rtl_also_disables_display_label_normalization(self):
        path = self.root / "rtl" / "toy.sv"
        path.write_text(path.read_text() + "wire ref = model_a_label;\n")
        self.cache().store(self.prop, self.decision())
        self.assertIsNotNone(self.cache().lookup(self.prop))
        self.assertIsNone(self.cache().lookup({**self.prop, "label": "other_model_label"}))

    def imported(self, source):
        path = self.root / "assertions" / "toy_checker.sv"
        path.write_text(source)
        setup = {"units": [{**self.unit, "assertion_files": ["assertions/toy_checker.sv"]}]}
        unit = prepare_existing(self.root, setup)["units"][0]
        return unit, load_existing(self.root, unit)

    def test_changed_unrelated_original_assertion_preserves_unchanged_target_cache(self):
        source = ("module toy_checker(input logic clk,rst,req,ack);\n"
                  "original_one: assert property (@(posedge clk) req |-> ack);\n"
                  "original_two: assert property (@(posedge clk) ack |-> req);\nendmodule\n")
        unit, props = self.imported(source)
        cache = self.cache(unit=unit)
        cache.store(props[0], self.decision(prop=props[0]))
        # No newline/layout changes: unrelated body edits do not affect the
        # rendered single target or its preserved procedural/generate position.
        changed, new_props = self.imported(source.replace("ack |-> req", "ack |-> !req"))
        self.assertIsNotNone(self.cache(unit=changed).lookup(new_props[0]))
        # The changed sibling has never been proved under its new expression.
        self.assertIsNone(self.cache(unit=changed).lookup(new_props[1]))

    def test_original_wrapper_clock_and_named_scope_are_never_normalized(self):
        source = ("module toy_checker(input logic clk,rst,req,ack);\n"
                  "generate if (1) begin : original_scope\n"
                  "original: assert property (@(posedge clk) disable iff (rst) req |-> ack);\n"
                  "end endgenerate\nendmodule\n")
        unit, props = self.imported(source)
        self.cache(unit=unit).store(props[0], self.decision(prop=props[0]))
        for before, after in (("posedge clk", "negedge clk"), ("original_scope", "other_scope"),
                              ("disable iff (rst)", "disable iff (!rst)")):
            with self.subTest(change=after):
                changed, new_props = self.imported(source.replace(before, after))
                self.assertIsNone(self.cache(unit=changed).lookup(new_props[0]))

    def test_macro_include_and_file_sensitive_source_bypass_with_reason(self):
        path = self.root / "rtl" / "toy.sv"
        for source, expected in (('`include `HEADER\nmodule toy; endmodule\n', "nonliteral include"),
                                 ('module toy; initial $display(`__FILE__); endmodule\n', "Location/time-sensitive")):
            with self.subTest(source=source):
                path.write_text(source)
                cache = self.cache()
                cache.store(self.prop, self.decision())
                self.assertIsNone(cache.lookup(self.prop))
                self.assertIn(expected, cache.bypass_reason)

    def test_stage_caches_and_stage_specific_timeouts_are_separate(self):
        self.cache("syntax").store(self.prop, self.decision("syntax"))
        self.assertIsNone(self.cache("fpv").lookup(self.prop))
        self.assertIsNone(self.cache("vacuity").lookup(self.prop))
        changed = {**self.settings, "syntax_timeout_seconds": 11}
        self.assertIsNone(self.cache("syntax", settings=changed).lookup(self.prop))

    def test_tool_reinstall_metadata_invalidates_even_identical_launcher_bytes(self):
        cache = self.cache()
        cache.store(self.prop, self.decision())
        self.assertIsNotNone(cache.lookup(self.prop))
        before = self.tool.read_bytes()
        self.tool.touch()
        self.assertEqual(self.tool.read_bytes(), before)
        self.assertIsNone(cache.lookup(self.prop))
        cache.store(self.prop, self.decision())
        self.assertIsNotNone(cache.lookup(self.prop))
        (self.tool.parent / "updated_installation.version").write_text("new installation metadata\n")
        self.assertIsNone(cache.lookup(self.prop))

    def test_checker_include_bypasses_output_dependent_resolution(self):
        self.template.write_text('`include "common.svh"\n' + self.base_template)
        cache = self.cache()
        cache.store(self.prop, self.decision())
        self.assertIsNone(cache.lookup(self.prop))
        self.assertIn("Checker includes", cache.bypass_reason)
        self.assertEqual(list(cache.root.rglob("*.json")), [])

    def test_dependency_change_during_formal_run_cannot_receive_old_proof(self):
        cache = self.cache()
        self.assertIsNone(cache.lookup(self.prop))
        rtl = self.root / "rtl" / "toy.sv"
        rtl.write_text(rtl.read_text() + "wire changed_during_formal_run;\n")
        cache.store(copy.deepcopy(self.prop), self.decision())
        self.assertIn("changed between lookup and result", cache.bypass_reason)
        self.assertIsNone(self.cache().lookup(self.prop))
        self.assertEqual(list(cache.root.rglob("*.json")), [])
        # A new lookup followed by qualification of unchanged current inputs
        # can share its fresh result normally.
        self.assertIsNone(cache.lookup(self.prop))
        cache.store(self.prop, self.decision())
        self.assertIsNotNone(self.cache().lookup(self.prop))

    def test_external_file_io_in_every_formal_source_bypasses_even_explicit_data_dependency(self):
        data = self.root / "env" / "table.hex"
        data.write_text("00\n")
        self.unit["fixed_dependencies"] = ["env/table.hex"]
        paths = [self.template, self.root / "rtl" / "toy.sv", self.root / "bind" / "toy.sv",
                 self.root / "include" / "nested.data"]
        for path in paths:
            before = path.read_text()
            with self.subTest(path=path):
                path.write_text(before + '\ninitial $readmemh("table.hex", memory);\n')
                cache = self.cache()
                cache.store(self.prop, self.decision())
                self.assertIsNone(cache.lookup(self.prop))
                self.assertIn("External file I/O", cache.bypass_reason)
                data.write_text("ff\n")
                self.assertIsNone(cache.lookup(self.prop))
            path.write_text(before)
        for task in ("$readmemb", "$fopen", "$fread", "$fscanf", "$fgets", "$fgetc"):
            with self.subTest(task=task):
                self.template.write_text(self.base_template + f'initial {task}("table.hex", memory);\n')
                cache = self.cache()
                cache.store(self.prop, self.decision())
                self.assertIsNone(cache.lookup(self.prop))
                self.assertIn("External file I/O", cache.bypass_reason)

    def test_infrastructure_or_unrecognized_compile_failure_is_not_cached(self):
        messages = ["out of memory", "server connection lost", "resource limit reached",
                    "internal compiler error", "unrecognized compile failure", "syntax error; out of memory"]
        for message in messages:
            with self.subTest(message=message):
                decision = self.decision("syntax", status="FAIL")
                decision.update(reason="AQ_COMPILE_ERROR " + message, diagnostic="AQ_COMPILE_ERROR " + message)
                self.cache("syntax").store(self.prop, decision)
                self.assertIsNone(self.cache("syntax").lookup(self.prop))
        for message in ("syntax error", "unexpected token ')'", "undeclared identifier foo",
                        "'foo' is not declared", "unresolved identifier bar"):
            with self.subTest(message=message):
                decision = self.decision("syntax", status="FAIL")
                decision.update(reason="AQ_COMPILE_ERROR " + message, diagnostic="AQ_COMPILE_ERROR " + message)
                self.cache("syntax").store(self.prop, decision)
                self.assertIsNotNone(self.cache("syntax").lookup(self.prop))

    def test_shared_evidence_survives_archived_origin_and_corruption_is_a_miss(self):
        cache = self.cache()
        decision = self.decision()
        cache.store(self.prop, decision)
        initial = cache.lookup(self.prop)
        shared = Path(initial["cache_source_log"])
        original = Path(decision["log_file"])
        archived = self.root / "gen_assertions" / "_history" / "original_proof.log"
        archived.parent.mkdir(parents=True)
        original.rename(archived)
        hit = self.cache().lookup(self.prop)
        self.assertIsNotNone(hit)
        self.assertFalse(original.exists())
        self.assertEqual(hit["log_file"], str(original))
        self.assertEqual(hit["cache_origin_log"], str(original))
        self.assertEqual(shared.read_bytes(), archived.read_bytes())
        shared.write_text("corrupt incomplete evidence\n")
        self.assertIsNone(self.cache().lookup(self.prop))

    def test_missing_or_empty_log_evidence_never_creates_a_cached_pass(self):
        for stage, status in (("syntax", "PASS"), ("fpv", "PROVEN"), ("vacuity", "NOT_APPLICABLE")):
            for kind in ("absent", "missing", "empty"):
                with self.subTest(stage=stage, kind=kind):
                    decision = self.decision(stage, status=status)
                    path = Path(decision["log_file"])
                    if kind == "absent":
                        decision.pop("log_file")
                    elif kind == "missing":
                        path.unlink()
                    else:
                        path.write_bytes(b"")
                    cache = self.cache(stage)
                    cache.store(self.prop, decision)
                    self.assertIsNone(cache.lookup(self.prop))


if __name__ == "__main__":
    unittest.main()
