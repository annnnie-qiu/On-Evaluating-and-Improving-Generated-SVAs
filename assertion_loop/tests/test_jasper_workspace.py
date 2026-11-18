"""Reproduce Jasper ownership failures with real subprocesses (no license).

The executable doubles deliberately leave jgproject locked after exit. These
tests check launching/recovery, not Jasper's formal proof correctness.
"""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from looplib.common import EnvironmentLock, EnvironmentSetupError, read
from looplib.equivalence import qualify_mutant
from looplib.engine import run_unit
from looplib.faults import build_pool
from looplib.jasper_jobs import JasperAttempt
import test_existing_workflow as fixtures


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jasper workspace ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.executable = self.root / "fake jg"
        self.executable.write_text("#!" + sys.executable + "\n" + '''
from pathlib import Path
import os, sys
project = Path('jgproject')
if project.exists():
    print('ERROR: Cannot obtain ownership of project directory: jgproject', flush=True)
    raise SystemExit(1)
project.mkdir()
(project/'owner').write_text(str(os.getpid()))
script = Path(sys.argv[-1])
assert script.is_absolute() and script.is_file()
if script.read_text() == 'FAIL':
    print('ERROR: DELIBERATE_OFFLINE_FAILURE', flush=True)
    raise SystemExit(1)
print('OFFLINE_EXECUTED ' + str(script), flush=True)
''')
        self.executable.chmod(0o755)

    def launch(self, root, text="PASS"):
        job = JasperAttempt(root)
        script = job.directory / "run.tcl"
        script.write_text(text)
        job.publish("run.tcl")
        with redirect_stdout(io.StringIO()):
            code = job.run(self.executable, script, 10)
        return job, code

    def test_stale_root_project_and_legacy_logs_are_preserved(self):
        project = self.root / "jgproject"
        project.mkdir()
        (project / "owner").write_text("DO_NOT_TOUCH_OLD_OWNER")
        (self.root / "run.log").write_text("ORIGINAL ERROR LOG")
        (self.root / "run.tcl").write_text("ORIGINAL TCL")
        first, code = self.launch(self.root)
        second, retry_code = self.launch(self.root)
        self.assertEqual((code, retry_code), (0, 0))
        self.assertNotEqual(first.directory, second.directory)
        self.assertEqual((project / "owner").read_text(), "DO_NOT_TOUCH_OLD_OWNER")
        self.assertEqual([p.read_text() for p in self.root.glob("run.log.previous_*")], ["ORIGINAL ERROR LOG"])
        self.assertEqual([p.read_text() for p in self.root.glob("run.tcl.previous_*")], ["ORIGINAL TCL"])
        self.assertEqual((self.root / "run.log").resolve(), second.log)
        self.assertEqual(read(self.root / "current_run.json")["returncode"], 0)
        self.assertEqual(read(first.directory / "execution.json")["status"], "EXITED")

    def test_failure_then_retry_keeps_exact_failed_log(self):
        first, code = self.launch(self.root, "FAIL")
        before = first.log.read_bytes()
        self.assertEqual(code, 1)
        self.assertEqual(read(first.directory / "execution.json")["status"], "FAILED")
        self.assertIn(b"DELIBERATE_OFFLINE_FAILURE", before)
        second, code = self.launch(self.root)
        self.assertEqual(code, 0)
        self.assertEqual(first.log.read_bytes(), before)
        self.assertTrue((first.directory / "jgproject/owner").is_file())
        self.assertNotEqual(first.directory, second.directory)

    def test_interruption_and_timeout_keep_metadata_and_do_not_reuse_cwd(self):
        for error, expected in ((KeyboardInterrupt(), "INTERRUPTED"), (TimeoutError("OFFLINE"), "TIMEOUT")):
            with self.subTest(status=expected):
                job = JasperAttempt(self.root)
                script = job.directory / "run.tcl"
                script.write_text("PASS")
                with patch("looplib.jasper_jobs.command", side_effect=error), redirect_stdout(io.StringIO()):
                    with self.assertRaises(type(error)):
                        job.run(self.executable, script, 10)
                self.assertEqual(read(job.directory / "execution.json")["status"], expected)
                retry, code = self.launch(self.root)
                self.assertEqual(code, 0)
                self.assertNotEqual(job.directory, retry.directory)

    def test_old_workspace_can_remain_in_use_without_a_collision(self):
        # First job has a live writer holding its log open and a locked project.
        first = JasperAttempt(self.root)
        (first.directory / "jgproject").mkdir()
        with first.log.open("a") as writer:
            writer.write("FIRST STILL ACTIVE\n")
            writer.flush()
            second, code = self.launch(self.root)
            writer.write("FIRST FINISHED LATER\n")
        self.assertEqual(code, 0)
        self.assertIn("FIRST FINISHED LATER", first.log.read_text())
        self.assertNotIn("FIRST STILL ACTIVE", second.log.read_text())


class PoolRetryTests(unittest.TestCase):
    def setUp(self):
        fixtures.ExistingWorkflowTests.setUp(self)
        self.prepare(one=True)
        # Real executable double refuses a reused cwd; the production Tcl runs
        # in tclsh via fake_jasper.py, including property selection/parsing.
        fake = ROOT / "tests/fake_jasper.py"
        wrapper = self.dataset / "locked_jg"
        wrapper.write_text("#!" + sys.executable + "\n" + f'''
from pathlib import Path
import os,sys
p=Path('jgproject')
if p.exists():
 print('ERROR: Cannot obtain ownership of project directory: jgproject',flush=True)
 raise SystemExit(1)
p.mkdir()
(p/'owner').write_text('OFFLINE_RETAINED_LOCK')
os.execv(sys.executable,[sys.executable,{str(fake)!r},*sys.argv[1:]])
''')
        wrapper.chmod(0o755)
        self.config["jasper_executable"] = str(wrapper)

    prepare = fixtures.ExistingWorkflowTests.prepare
    fake_measure = fixtures.ExistingWorkflowTests.fake_measure
    passing_technical_stub = fixtures.ExistingWorkflowTests.passing_technical_stub

    def test_calibration_ignores_old_root_lock_and_caches_definitive_result(self):
        directory = self.dataset / "gen_assertions/calibration/golden_calibration"
        (directory / "jgproject").mkdir(parents=True)
        (directory / "jgproject/owner").write_text("OLD_OWNER")
        args = (self.dataset, self.unit, {"id": "golden_calibration", "sha256": self.lock.identity},
                self.config, self.lock, directory)
        with redirect_stdout(io.StringIO()):
            result = qualify_mutant(*args, calibration=True)
        self.assertEqual(result["status"], "EQUIVALENT")
        self.assertTrue(all(Path(p).is_file() for p in result["evidence_files"]))
        self.assertIn("attempt_", result["witness"]["proof_log"])
        with patch("looplib.equivalence.JasperAttempt", side_effect=AssertionError("Cache hit must not launch")), redirect_stdout(io.StringIO()):
            self.assertEqual(qualify_mutant(*args, calibration=True), result)
        self.assertEqual((directory / "jgproject/owner").read_text(), "OLD_OWNER")

    def test_failed_calibration_is_retried_in_fresh_workspace_with_error_evidence(self):
        directory = self.dataset / "gen_assertions/golden_calibration"
        args = (self.dataset, self.unit, {"id": "golden_calibration", "sha256": self.lock.identity},
                self.config, self.lock, directory)
        original = self.config["jasper_executable"]
        bad = self.dataset / "failing_jg"
        bad.write_text("#!" + sys.executable + "\nprint('ERROR: Cannot obtain ownership of project directory: jgproject')\nraise SystemExit(1)\n")
        bad.chmod(0o755)
        self.config["jasper_executable"] = str(bad)
        with redirect_stdout(io.StringIO()):
            failed = qualify_mutant(*args, calibration=True)
        self.assertEqual(failed["status"], "UNRESOLVED")
        self.assertIn("Cannot obtain ownership", failed["reason"])
        log = next(Path(p) for p in failed["evidence_files"] if p.endswith("run.log"))
        before = log.read_bytes()
        self.config["jasper_executable"] = original
        with redirect_stdout(io.StringIO()):
            result = qualify_mutant(*args, calibration=True)
        self.assertEqual(result["status"], "EQUIVALENT")
        self.assertEqual(log.read_bytes(), before)
        self.assertEqual(len(list(directory.glob("attempt_*"))), 2)

    def test_pool_restart_retries_fixed_setup_and_mutant_syntax_without_lock_conflicts(self):
        with redirect_stdout(io.StringIO()):
            pool = build_pool(self.dataset, self.unit, self.config, self.lock)
        directory = Path(pool["directory"])
        original_logs = {p: p.read_bytes() for p in directory.rglob("attempt_*/run.log")}
        self.assertTrue(original_logs)
        # Simulate an interrupted pool build: successful qualification evidence
        # exists, but the final pool manifest was not committed.
        (directory / "pool.json").unlink()
        with redirect_stdout(io.StringIO()):
            retried = build_pool(self.dataset, self.unit, self.config, self.lock)
        self.assertEqual(pool["identity"], retried["identity"])
        self.assertEqual(pool["dev_ids"], retried["dev_ids"])
        self.assertTrue(all(p.read_bytes() == text for p, text in original_logs.items()))
        self.assertEqual(len(list((directory / "fixed_setup").glob("attempt_*"))), 2)
        self.assertTrue(all(m["status"] != "UNRESOLVED" for m in retried["validation"]))

    def test_v16_upgrade_resumes_passed_step_a_without_gpt_or_technical_rerun(self):
        previous = read(ROOT / "MANIFEST.json")["resume_compatible_implementations"][-1]
        pending = {**self.pool, "pending": True}
        calls = []
        def judge(*args):
            calls.append(args)
            return {"verdict": "PASS", "question": "", "reason": "offline", "evidence_quote": ""}
        client = SimpleNamespace(call=judge, records=lambda: [])
        args = (self.dataset, self.unit, "deepseek", 1, self.config, client,
                pending, self.lock, self.directory, [1], False)
        with patch("looplib.engine.implementation_digest", return_value=previous), \
             patch("looplib.engine.qualify", side_effect=self.passing_technical_stub), \
             patch("looplib.engine.build_pool", side_effect=EnvironmentSetupError("OFFLINE ownership failure")), \
             redirect_stdout(io.StringIO()):
            with self.assertRaises(EnvironmentSetupError):
                run_unit(*args)
        checkpoint = self.directory / "rounds/round_01/step_a_attempts/attempt_01/attempt.json"
        before = checkpoint.read_bytes()
        self.assertEqual(len(calls), 1)
        output = io.StringIO()
        with patch("looplib.engine.qualify", side_effect=AssertionError("Must reuse completed Step A")), \
             patch("looplib.engine.build_pool", return_value=self.pool), \
             patch("looplib.engine.measure", side_effect=self.fake_measure), \
             patch.object(client, "call", side_effect=AssertionError("Must not call GPT or generator")), \
             redirect_stdout(output):
            rows = run_unit(*args)
        self.assertEqual(rows[0]["qualified_count"], 1)
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertIn("checkpoint=resume", output.getvalue())
        self.assertIn("compatible implementation", output.getvalue())
        self.assertFalse((self.directory / "_history").exists())


if __name__ == "__main__":
    unittest.main()
