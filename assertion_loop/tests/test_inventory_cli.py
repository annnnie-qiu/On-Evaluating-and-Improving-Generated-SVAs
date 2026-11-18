"""CLI plans keep the full input audit separate from the formal unit setup."""
from __future__ import annotations
from contextlib import redirect_stderr,redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from looplib.common import EnvironmentLock,read,save
import run


class InventoryCLIIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dataset=(Path(temporary.name)/"two_units").resolve()
        for directory in ("rtl","spec","bind","assertions"):
            (self.dataset/directory).mkdir(parents=True)
        for name in ("alpha","beta"):
            (self.dataset/"rtl"/(name+".sv")).write_text(
                f"module {name}(input logic clk, rst, req, output logic ack);\n"
                "assign ack = req;\nendmodule\n")
            (self.dataset/"bind"/(name+"_bind.sv")).write_text(
                f"bind {name} {name}_assertions check_i (.*);\n")
        (self.dataset/"spec/shared.md").write_text("Each output ack equals req. Reset rst is active high.")
        (self.dataset/"assertions/alpha_assertions.sv").write_text(
            "module alpha_assertions(input logic clk, rst, req, ack);\n"
            "fixed_environment: assume property (@(posedge clk) !rst);\n"
            "a_valid: assert property (@(posedge clk) disable iff (rst) req |-> ack);\n"
            "a_idle: assert property (@(posedge clk) disable iff (rst) !req |-> !ack);\n"
            "endmodule\n")
        (self.dataset/"assertions/beta_assertions.sv").write_text(
            "module beta_assertions(input logic clk, rst, req, ack);\n"
            "always_comb begin : output_checks\n"
            "b_exact: assert (ack == req);\nend\nendmodule\n")
        self.config=read(ROOT/"config.json")
        self.config.update({"input_mode":"existing_assertions","key_env":"INVENTORY_TEST_NO_API_KEY",
                            "key_file":"","jasper_executable":str(self.dataset/"NEVER_RUN_JASPER")})
        self.config_file=Path(temporary.name)/"offline_config.json"
        save(self.config_file,self.config)
        self.setup_file=self.dataset/"gen_assertions/setup/dataset.json"
        self.inventory_file=self.dataset/"gen_assertions/setup/imported/input_inventory.json"

    def args(self,experiment):
        return [str(self.dataset),"--config",str(self.config_file),"--plan","--experiment",experiment]

    def assert_inventory(self,inventory):
        self.assertEqual(inventory["source_file_count"],2)
        self.assertEqual(inventory["mapped_source_file_count"],2)
        self.assertEqual(inventory["source_assertion_count"],3)
        self.assertEqual(inventory["mapped_unit_assertion_count"],3)
        self.assertEqual(inventory["unassigned_files"],[])
        rows={row["unit"]:row for row in inventory["units"]}
        self.assertEqual(set(rows),{"alpha","beta"})
        self.assertEqual(rows["alpha"]["assertion_count"],2)
        self.assertEqual(rows["alpha"]["source_files"],["assertions/alpha_assertions.sv"])
        self.assertEqual(rows["beta"]["assertion_count"],1)
        self.assertEqual(rows["beta"]["source_files"],["assertions/beta_assertions.sv"])

    def test_repeated_plan_keeps_inventory_separate_and_environment_identity_stable(self):
        output=io.StringIO()
        with patch.object(run,"Client",side_effect=AssertionError("Planning cannot call an API")) as client, \
             patch.object(run,"run_unit",side_effect=AssertionError("Planning cannot run qualification")) as workflow, \
             patch.object(run,"build_pool",side_effect=AssertionError("Planning cannot run Jasper")) as pool, \
             redirect_stdout(output),redirect_stderr(output):
            self.assertEqual(run.main(ablation=True,argv=self.args("repeat_plan")),0,output.getvalue())
            plan_file=self.dataset/"gen_assertions/ablation/repeat_plan/plan.json"
            first=read(plan_file)
            setup_bytes=self.setup_file.read_bytes()
            backups=list(self.setup_file.parent.glob("dataset.previous_*.json"))
            inventory_bytes=self.inventory_file.read_bytes()
            self.assert_inventory(first["input_inventory"])
            self.assertEqual(first["input_inventory"],read(self.inventory_file))
            self.assertNotIn("input_inventory",read(self.setup_file))
            self.assertNotIn("input_inventory",first["units"])
            # Editing audit-only metadata must not alter a formal environment.
            audit=read(self.inventory_file)
            audit["audit_note"]="Inventory presentation metadata only"
            save(self.inventory_file,audit)
            self.assertEqual(EnvironmentLock(self.dataset,read(self.setup_file)).identity,first["environment_sha256"])
            self.assertEqual(run.main(ablation=True,argv=self.args("repeat_plan")),0,output.getvalue())
            second=read(plan_file)
        client.assert_not_called();workflow.assert_not_called();pool.assert_not_called()
        self.assertEqual(second["environment_sha256"],first["environment_sha256"])
        self.assertEqual(self.setup_file.read_bytes(),setup_bytes)
        self.assertEqual(list(self.setup_file.parent.glob("dataset.previous_*.json")),backups)
        self.assertEqual(self.inventory_file.read_bytes(),inventory_bytes)
        self.assertEqual(second["input_inventory"],first["input_inventory"])
        self.assertIn("2/2 source files mapped; 3 assertions across 2 units; selected_units=2",output.getvalue())
        self.assertIn("[INPUTS] alpha: assertions/alpha_assertions.sv assertions=2",output.getvalue())
        self.assertIn("[INPUTS] beta: assertions/beta_assertions.sv assertions=1",output.getvalue())

    def test_ablation_entrypoint_restricts_details_but_retains_full_inventory(self):
        environment=dict(os.environ)
        environment.pop("INVENTORY_TEST_NO_API_KEY",None)
        completed=subprocess.run(
            [sys.executable,str(ROOT/"ablation/run_ablation.py"),*self.args("subset_plan"),"--unit","beta"],
            cwd=ROOT,env=environment,text=True,capture_output=True,timeout=30)
        self.assertEqual(completed.returncode,0,completed.stdout+completed.stderr)
        plan=read(self.dataset/"gen_assertions/ablation/subset_plan/plan.json")
        self.assertEqual(plan["status"],"PLANNED_NOT_EXECUTED")
        self.assertEqual([unit["name"] for unit in plan["units"]["units"]],["beta"])
        self.assert_inventory(plan["input_inventory"])
        self.assertEqual(plan["input_inventory"],read(self.inventory_file))
        self.assertEqual({unit["name"] for unit in read(self.setup_file)["units"]},{"alpha","beta"})
        self.assertNotIn("input_inventory",read(self.setup_file))
        self.assertIn("2/2 source files mapped; 3 assertions across 2 units; selected_units=1",completed.stdout)
        self.assertIn("[INPUTS] beta: assertions/beta_assertions.sv assertions=1",completed.stdout)
        self.assertNotIn("[INPUTS] alpha:",completed.stdout)
        self.assertIn(str(self.inventory_file),completed.stdout)
        experiment=self.dataset/"gen_assertions/ablation/subset_plan"
        self.assertFalse((experiment/"api").exists())
        self.assertFalse((self.dataset/"gen_assertions/_shared").exists())
        self.assertFalse(list(experiment.glob("*/run_*/units/*")))


if __name__=="__main__": unittest.main()
