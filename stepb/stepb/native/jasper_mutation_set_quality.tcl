# Strict one-subset Jasper mutation checker-coverage run.
# Python has already removed every non-selected generated assertion and every
# cover statement.  This Tcl verifies the elaborated property inventory before
# it accepts any coverage number.

set RQ2_PROJECT_ROOT       [file normalize "{{project_root}}"]
set RQ2_RUN_DIR            [file normalize "{{run_dir}}"]
set RQ2_ASSERTION_FILE     [file normalize "{{assertion_file}}"]
set RQ2_DUT                "{{top}}"
set RQ2_SUBSET_KEY         "{{subset_key}}"
set RQ2_EXPECTED_ASSERTS   {{expected_assertions}}
set RQ2_REMOVED_COVERS     {{removed_cover_count}}
set RQ2_RTL_FILES          [list {{rtl_files_tcl}}]
set RQ2_INCLUDE_DIRS       [list {{include_dirs_tcl}}]
set RQ2_DEFINES            [list {{defines_tcl}}]
set RQ2_BIND_FILES         [list {{bind_files_tcl}}]
set RQ2_CLOCK_MODE         {{clock_mode_tcl}}
set RQ2_CLOCK              {{clock_tcl}}
set RQ2_RESET_EXPRESSION   {{reset_expression_tcl}}
set RQ2_MUTATION_SCOPE     {{mutation_scope_tcl}}

proc rq2_finish {token} {
    puts ""
    puts $token
    flush stdout
    exit -force
}

proc rq2_verify_files {files} {
    foreach source $files {
        if {![file exists $source]} {
            error "RQ2_SETUP_ERROR required file does not exist: $source"
        }
    }
}

proc rq2_analyze {assertion_file rtl_files bind_files incdirs defines clock_mode clock reset_expression} {
    set files [concat $rtl_files [list $assertion_file] $bind_files]
    rq2_verify_files $files
    set options [list]
    foreach directory $incdirs {
        lappend options "+incdir+$directory"
    }
    foreach definition $defines {
        lappend options "+define+$definition"
    }
    analyze -sv {*}$options {*}$files
    return [dict create clock_mode $clock_mode clock $clock reset_expression $reset_expression]
}

proc rq2_environment {cfg} {
    set mode [string tolower [string trim [dict get $cfg clock_mode]]]
    set clock_name [string trim [dict get $cfg clock]]
    set reset_expr [string trim [dict get $cfg reset_expression]]
    if {$mode eq "none"} {
        clock -none
        reset -none
        return
    }
    if {$clock_name eq ""} {
        error "RQ2_SETUP_ERROR clocked unit has no configured clock"
    }
    clock $clock_name
    if {$reset_expr eq ""} {
        reset -none
    } else {
        reset -expression $reset_expr
    }
}

proc rq2_normalize_status {raw} {
    if {![catch {set value [dict get $raw status]}]} {
        return [string tolower [string trim $value]]
    }
    if {[llength $raw] >= 2 && [string tolower [lindex $raw 0]] eq "status"} {
        return [string tolower [string trim [lindex $raw 1]]]
    }
    if {[llength $raw] == 1} {
        return [string tolower [string trim [lindex $raw 0]]]
    }
    return [string tolower [string trim $raw]]
}

proc rq2_extract_counts {report dut} {
    if {[llength $report] == 0} {
        error "check_cov -report returned no data"
    }
    set summary [lindex $report 0]
    if {![dict exists $summary $dut checker_coverage]} {
        error "checker_coverage missing for DUT $dut"
    }
    set checker [dict get $summary $dut checker_coverage]
    foreach key {checked total unchecked undetectable unprocessed excluded} {
        if {![dict exists $checker $key]} {
            error "checker_coverage field missing: $key"
        }
    }
    return $checker
}

puts "RQ2_INFO dut=$RQ2_DUT subset_key=$RQ2_SUBSET_KEY expected_assertions=$RQ2_EXPECTED_ASSERTS"
puts "RQ2_INFO assertion_file=$RQ2_ASSERTION_FILE removed_source_covers=$RQ2_REMOVED_COVERS"

clear -all
if {[catch {
    set RQ2_CFG [rq2_analyze \
        $RQ2_ASSERTION_FILE $RQ2_RTL_FILES $RQ2_BIND_FILES \
        $RQ2_INCLUDE_DIRS $RQ2_DEFINES $RQ2_CLOCK_MODE \
        $RQ2_CLOCK $RQ2_RESET_EXPRESSION]
} analyze_error]} {
    puts "RQ2_ANALYZE_ERROR $analyze_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}

# Keep the order from the user's known-working COV flow.
if {[catch {
    if {[string tolower $RQ2_MUTATION_SCOPE] eq "top"} {
        check_cov -init \
            -model {statement branch expression} \
            -type mutation \
            -covdut $RQ2_DUT \
            -exclude_module "*" \
            -include_module $RQ2_DUT
    } else {
        check_cov -init \
            -model {statement branch expression} \
            -type mutation \
            -covdut $RQ2_DUT
    }
} init_error]} {
    puts "RQ2_MUTATION_INIT_ERROR $init_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}

if {[catch {elaborate -top $RQ2_DUT} elaborate_error]} {
    puts "RQ2_ELABORATE_ERROR $elaborate_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}

if {[catch {rq2_environment $RQ2_CFG} environment_error]} {
    puts "RQ2_ENVIRONMENT_ERROR $environment_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}

# Hard invariant 1: the formal design contains exactly the selected AQ asserts.
if {[catch {
    set RQ2_ASSERTS [get_property_list -include {type assert}]
} property_error]} {
    puts "RQ2_PROPERTY_LIST_ERROR $property_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}
puts "RQ2_ACTIVE_ASSERTION_COUNT count=[llength $RQ2_ASSERTS]"
foreach property $RQ2_ASSERTS {
    puts "RQ2_ACTIVE_ASSERTION name=$property"
}
if {[llength $RQ2_ASSERTS] != $RQ2_EXPECTED_ASSERTS} {
    puts "RQ2_ASSERTION_COUNT_MISMATCH expected=$RQ2_EXPECTED_ASSERTS actual=[llength $RQ2_ASSERTS]"
    rq2_finish "RQ2_MUTATION_ERROR"
}

# Source cover statements were already removed by Python before this file was
# analyzed.  check_cov may create internal cover properties for its mutation
# instrumentation, so the runtime cover count is diagnostic rather than an
# assertion that the count must be zero.
if {[catch {
    set RQ2_COVERS [get_property_list -include {type cover}]
} cover_query_error]} {
    puts "RQ2_COVER_LIST_ERROR $cover_query_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}
puts "RQ2_ACTIVE_COVER_COUNT count=[llength $RQ2_COVERS]"
foreach property $RQ2_COVERS {
    puts "RQ2_ACTIVE_COVER name=$property"
}

if {[catch {prove -all} prove_error]} {
    puts "RQ2_PROVE_ERROR $prove_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}

# Hard invariant 3: all active AQ assertions are fully proven on original RTL.
set RQ2_PROVEN 0
set RQ2_BAD 0
foreach property $RQ2_ASSERTS {
    if {[catch {
        set raw [get_property_info $property -list status]
        set status [rq2_normalize_status $raw]
    } status_error]} {
        puts "RQ2_PROOF_STATUS_ERROR property=$property error=$status_error"
        incr RQ2_BAD
        continue
    }
    puts "RQ2_PROOF_DETAIL property=$property status=$status"
    if {[string first "bounded_proven" $status] >= 0} {
        incr RQ2_BAD
    } elseif {[string first "proven" $status] >= 0} {
        incr RQ2_PROVEN
    } else {
        incr RQ2_BAD
    }
}
puts "RQ2_PROOF_COUNTS total=[llength $RQ2_ASSERTS] proven=$RQ2_PROVEN bad=$RQ2_BAD"
if {$RQ2_BAD != 0 || $RQ2_PROVEN != [llength $RQ2_ASSERTS]} {
    rq2_finish "RQ2_MUTATION_ERROR"
}

if {[catch {
    check_cov -measure -type mutation -all
    check_cov -configure -checker_mode mutation
    set RQ2_REPORT [check_cov -report]
    set RQ2_COUNTS [rq2_extract_counts $RQ2_REPORT $RQ2_DUT]
} coverage_error]} {
    puts "RQ2_COVERAGE_ERROR $coverage_error"
    rq2_finish "RQ2_MUTATION_ERROR"
}

if {![catch {
    set report_file [open "$RQ2_RUN_DIR/check_cov_report_raw.txt" "w"]
    puts $report_file $RQ2_REPORT
    close $report_file
} report_write_error]} {
    puts "RQ2_INFO raw_report=$RQ2_RUN_DIR/check_cov_report_raw.txt"
} else {
    puts "RQ2_REPORT_WRITE_WARNING $report_write_error"
}

if {[catch {
    check_cov -export "$RQ2_RUN_DIR/mutation_cov.db" -force
} export_error]} {
    puts "RQ2_EXPORT_WARNING $export_error"
}

set checked      [dict get $RQ2_COUNTS checked]
set total        [dict get $RQ2_COUNTS total]
set unchecked    [dict get $RQ2_COUNTS unchecked]
set undetectable [dict get $RQ2_COUNTS undetectable]
set unprocessed  [dict get $RQ2_COUNTS unprocessed]
set excluded     [dict get $RQ2_COUNTS excluded]

puts "RQ2_MUTATION_COUNTS checked=$checked total=$total unchecked=$unchecked undetectable=$undetectable unprocessed=$unprocessed excluded=$excluded"
rq2_finish "RQ2_MUTATION_PASS"
