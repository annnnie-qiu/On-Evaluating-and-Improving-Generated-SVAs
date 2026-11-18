#!/usr/bin/env python3
"""OFFLINE TEST DOUBLE ONLY. These markers are not formal-verification evidence."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

tcl=Path(sys.argv[-1]).read_text()
def value(key):
    m=re.search(r"^set\s+"+key+r"\s+(?:\[file normalize\s+)?\{([^}]+)\}",tcl,re.M)
    return m.group(1) if m else ""
stage=value("AQ_STAGE")
source=Path(value("AQ_ASSERTION_FILE") or value("FH_ASSERTION")).read_text()
if os.environ.get("LOOP_TEST_EVENTS"):
    with Path(os.environ["LOOP_TEST_EVENTS"]).open("a") as f:
        f.write(json.dumps({"stage":stage or "faults","source":source})+"\n")
# Execute the REAL generated Tcl; only Jasper commands are stubbed. In particular,
# target inventories, label selection, classification, loops and catches are real.
def atom(s): return "{"+str(s).replace("\\","/").replace("{","\\{").replace("}","\\}")+"}"
names=re.findall(r"\b(\w+)\s*:\s*assert\b",source)
scope=value("AQ_TARGET_SCOPE") or value("FH_TARGET_SCOPE") or "toy_checker"
properties=[scope+"."+name for name in names]
statuses={}
for i,name in enumerate(properties):
    if "module EQ_MITER" in source:
        # Explicit offline double: calibration is equal; fixture mutants differ.
        # This is control-flow evidence, never solver evidence.
        statuses[name]="cex" if name.endswith("EQ_ENV_WITNESS") or "golden_calibration" not in str(Path(sys.argv[-1])) else "proven"
    elif stage=="vacuity": statuses[name]="proven" if "vac_bad" in source else "cex"
    elif stage: statuses[name]="cex" if "fpv_bad" in source else "proven"
    else: statuses[name]="cex" if i%2==0 else "proven"
# A native/implicit assertion fails in every run. It must not contaminate AQ.
properties.append("toy.native_unique_case_check")
statuses[properties[-1]]="cex"
if os.environ.get("LOOP_TEST_PROPERTIES_JSON"):
    statuses=json.loads(os.environ["LOOP_TEST_PROPERTIES_JSON"])
    properties=list(statuses)
shim="set MOCK_PROPERTIES [list "+" ".join(atom(p) for p in properties)+"]\n"
shim+="set MOCK_STATUSES [dict create "+" ".join(atom(p)+" "+atom(s) for p,s in statuses.items())+"]\n"
shim+="set MOCK_COMPILE_ERROR "+("1" if stage and "syntax_bad" in source else "0")+"\n"
shim+='''
proc clear {args} {}
proc analyze {args} { if {$::MOCK_COMPILE_ERROR} { error "fixture syntax error" } }
proc elaborate {args} {}
proc clock {args} {}
proc reset {args} {
    if {[lindex $args 0] eq "-none"} { return }
    if {[lindex $args 0] ne "-expression" || [llength $args] != 2} {
        error "OFFLINE_RESET_BAD_COMMAND $args"
    }
    set pins [lindex $args 1]
    foreach pin $pins {
        if {![regexp {^!?[A-Za-z_$][A-Za-z0-9_$.\[\]]*$} $pin]} {
            error "ERROR (ERS040): Invalid operand in reset expression: $pins. Use simple expressions for reset pins"
        }
    }
    puts "OFFLINE_RESET_PINS $pins"
}
proc prove {args} {}
proc get_property_list {args} { return $::MOCK_PROPERTIES }
proc get_property_info {name args} { return [dict create status [dict get $::MOCK_STATUSES $name]] }
rename exit mock_real_exit
proc exit {args} { mock_real_exit 0 }
'''
wrapper=shim+"\nif {[catch {source "+atom(Path(sys.argv[-1]).resolve())+"} detail]} { puts stderr $detail; mock_real_exit 1 }\n"
completed=subprocess.run(["tclsh"],input=wrapper,text=True,capture_output=True)
sys.stdout.write(completed.stdout)
sys.stderr.write(completed.stderr)
raise SystemExit(completed.returncode)
