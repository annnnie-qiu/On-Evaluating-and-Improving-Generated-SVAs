# Generic per-assertion JasperGold runner for the AQ qualification pipeline.
#
# This file is a template. The Python pipeline resolves every path and replaces
# the {{...}} fields when it creates a run directory. Project-specific RTL,
# top, bind, clock, reset, and include settings come from aq_config.json.

set AQ_STAGE            {{stage_tcl}}
set AQ_PROJECT_ROOT     [file normalize {{project_root_tcl}}]
set AQ_ASSERTION_FILE   [file normalize {{assertion_file_tcl}}]
set AQ_DUT              {{top_tcl}}
set AQ_PROPERTY_HINT    {{property_name_tcl}}
set AQ_TARGET_LABEL     {{target_property_name_tcl}}
set AQ_TARGET_SCOPE     {{target_scope_tcl}}
set AQ_ASSERTION_KIND   {{assertion_kind_tcl}}
set AQ_VACUITY_MODE     {{vacuity_mode_tcl}}
set AQ_RTL_FILELIST     {{rtl_filelist_tcl}}
set AQ_RTL_FILES        [list {{rtl_files_tcl}}]
set AQ_INCLUDE_DIRS     [list {{include_dirs_tcl}}]
set AQ_DEFINES          [list {{defines_tcl}}]
set AQ_BIND_FILES       [list {{bind_files_tcl}}]
set AQ_CLOCK_MODE       {{clock_mode_tcl}}
set AQ_CLOCK_SIGNAL     {{clock_signal_tcl}}
set AQ_RESET_EXPRESSION {{reset_expression_tcl}}
set AQ_RESET_PINS       {{reset_pins_tcl}}

proc aq_finish {token} {
    puts ""
    puts $token
    flush stdout
    exit -force
}

proc aq_verify_files {files} {
    foreach path $files {
        if {![file exists $path]} {
            error "AQ_SETUP_ERROR: Required file does not exist: $path"
        }
    }
}

proc aq_analyze_and_elaborate {} {
    set options [list]
    foreach directory $::AQ_INCLUDE_DIRS {
        lappend options "+incdir+$directory"
    }
    foreach definition $::AQ_DEFINES {
        lappend options "+define+$definition"
    }

    puts "AQ_INFO stage=$::AQ_STAGE"
    puts "AQ_INFO dut=$::AQ_DUT"
    puts "AQ_INFO assertion_file=$::AQ_ASSERTION_FILE"
    puts "AQ_INFO property_hint=$::AQ_PROPERTY_HINT"
    puts "AQ_INFO assertion_kind=$::AQ_ASSERTION_KIND"
    puts "AQ_INFO vacuity_mode=$::AQ_VACUITY_MODE"
    puts "AQ_INFO clock_mode=$::AQ_CLOCK_MODE"
    puts "AQ_INFO clock_signal=$::AQ_CLOCK_SIGNAL"
    puts "AQ_INFO reset_expression=$::AQ_RESET_EXPRESSION"
    puts "AQ_INFO reset_pins=$::AQ_RESET_PINS initialization=all-active proof=all-inactive"

    if {$::AQ_RTL_FILELIST ne ""} {
        aq_verify_files [list $::AQ_RTL_FILELIST $::AQ_ASSERTION_FILE]
        analyze -sv {*}$options -f $::AQ_RTL_FILELIST
    } else {
        aq_verify_files $::AQ_RTL_FILES
        analyze -sv {*}$options {*}$::AQ_RTL_FILES
    }

    analyze -sv {*}$options $::AQ_ASSERTION_FILE
    if {[llength $::AQ_BIND_FILES] > 0} {
        aq_verify_files $::AQ_BIND_FILES
        analyze -sv {*}$options {*}$::AQ_BIND_FILES
    }
    elaborate -top $::AQ_DUT
}

proc aq_apply_formal_environment {} {
    if {$::AQ_CLOCK_MODE eq "none"} {
        puts "AQ_INFO environment=combinational"
        clock -none
        reset -none
        return
    }
    if {$::AQ_CLOCK_SIGNAL eq ""} {
        error "AQ_SETUP_ERROR: A clocked unit requires a clock signal"
    }
    puts "AQ_INFO environment=clocked"
    clock $::AQ_CLOCK_SIGNAL
    if {$::AQ_RESET_EXPRESSION ne ""} {
        reset -expression $::AQ_RESET_PINS
    } else {
        reset -none
    }
}

proc aq_normalize_property_status {raw} {
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

proc aq_property_matches_target {property scope label} {
    # Compare literal names, never glob patterns: generate indices and escaped
    # identifiers may contain characters with a special meaning to string match.
    set suffix ".$label"
    set has_leaf [expr {[string length $property] >= [string length $suffix] &&
        [string range $property end-[expr {[string length $suffix]-1}] end] eq $suffix}]
    if {$scope eq ""} {
        set embedded_suffix "::$label"
        return [expr {$property eq $label || $has_leaf ||
            ([string length $property] >= [string length $embedded_suffix] &&
            [string range $property end-[expr {[string length $embedded_suffix]-1}] end] eq $embedded_suffix)}]
    }
    if {!$has_leaf} { return 0 }
    set parent [string range $property 0 end-[string length $suffix]]
    set start 0
    while {[set index [string first $scope $parent $start]] >= 0} {
        set after [expr {$index + [string length $scope]}]
        set before_ok [expr {$index == 0 || [string index $parent [expr {$index-1}]] eq "." ||
            ($index >= 2 && [string range $parent [expr {$index-2}] [expr {$index-1}]] eq "::")}]
        set after_ok [expr {$after == [string length $parent] || [string index $parent $after] eq "."}]
        # Named procedural/generate scopes may sit between the configured bind
        # instance and the final assertion label. Neither edge may match a
        # substring of a different instance or label.
        if {$before_ok && $after_ok} { return 1 }
        set start [expr {$index+1}]
    }
    return 0
}

proc aq_select_target {} {
    set all_properties [get_property_list -include {type assert}]
    set matches [list]
    foreach property $all_properties {
        puts "AQ_INVENTORY\t$property"
        if {[aq_property_matches_target $property $::AQ_TARGET_SCOPE $::AQ_TARGET_LABEL]} {
            lappend matches $property
        }
    }
    if {[llength $matches] != 1} {
        error "target assertion mapping failed: label=$::AQ_TARGET_LABEL scope=$::AQ_TARGET_SCOPE matches=[llength $matches] names=$matches inventory=$all_properties"
    }
    puts "AQ_TARGET\t[lindex $matches 0]"
    puts "AQ_ASSERTION_COUNTS total=[llength $all_properties] target=1 background=[expr {[llength $all_properties]-1}]"
    return $matches
}

proc aq_classify_properties {prefix} {
    set properties $::AQ_TARGET_PROPERTIES
    set total [llength $properties]
    set proven 0
    set cex 0
    set unresolved 0
    foreach property $properties {
        if {[catch {
            set raw [get_property_info $property -list status]
            set status [aq_normalize_property_status $raw]
        } status_error]} {
            puts "${prefix}_DETAIL property=$property status_query_error=$status_error"
            incr unresolved
            continue
        }
        puts "${prefix}_DETAIL property=$property status=$status"
        if {[string first "ar_cex" $status] >= 0 || [string first "cex" $status] >= 0} {
            incr cex
        } elseif {[string first "bounded_proven" $status] >= 0} {
            incr unresolved
        } elseif {[string first "undetermined" $status] >= 0 ||
                  [string first "unknown" $status] >= 0 ||
                  [string first "unprocessed" $status] >= 0 ||
                  [string first "error" $status] >= 0} {
            incr unresolved
        } elseif {[string first "proven" $status] >= 0} {
            incr proven
        } else {
            incr unresolved
        }
    }
    return [dict create total $total proven $proven cex $cex unresolved $unresolved]
}

proc aq_run_fpv {} {
    if {[catch {prove -all} prove_error]} {
        puts "AQ_FPV_DETAIL prove_error=$prove_error"
        aq_finish "AQ_FPV_UNKNOWN"
    }
    set counts [aq_classify_properties "AQ_FPV"]
    set total [dict get $counts total]
    set proven [dict get $counts proven]
    set cex [dict get $counts cex]
    set unresolved [dict get $counts unresolved]
    puts "AQ_FPV_COUNTS total=$total proven=$proven cex=$cex unresolved=$unresolved"
    if {$total == 0} {
        aq_finish "AQ_FPV_UNKNOWN"
    } elseif {$cex > 0} {
        aq_finish "AQ_FPV_CEX"
    } elseif {$unresolved > 0 || $proven != $total} {
        aq_finish "AQ_FPV_UNKNOWN"
    } else {
        aq_finish "AQ_FPV_PROVEN"
    }
}

proc aq_run_vacuity {} {
    # Python replaces the original implication with:
    #     antecedent |-> 1'b0
    # A CEX proves reachability and therefore non-vacuity. A full proof means
    # the antecedent is unreachable and the original assertion is vacuous.
    if {[catch {prove -all} prove_error]} {
        puts "AQ_VACUITY_DETAIL prove_error=$prove_error"
        aq_finish "AQ_VACUITY_UNKNOWN"
    }
    set counts [aq_classify_properties "AQ_VACUITY"]
    set total [dict get $counts total]
    set proven [dict get $counts proven]
    set cex [dict get $counts cex]
    set unresolved [dict get $counts unresolved]
    puts "AQ_VACUITY_COUNTS total=$total witness_proven=$proven witness_cex=$cex unresolved=$unresolved"
    if {$total == 0} {
        aq_finish "AQ_VACUITY_UNKNOWN"
    } elseif {$cex > 0} {
        aq_finish "AQ_NON_VACUOUS"
    } elseif {$unresolved > 0 || $proven != $total} {
        aq_finish "AQ_VACUITY_UNKNOWN"
    } else {
        aq_finish "AQ_VACUOUS"
    }
}

puts ""
puts "============================================================"
puts "AQ PER-ASSERTION JASPER RUN"
puts "============================================================"

clear -all
if {[catch {aq_analyze_and_elaborate} compile_error]} {
    puts "AQ_COMPILE_ERROR $compile_error"
    aq_finish "AQ_SYNTAX_FAIL"
}

if {[catch {set AQ_TARGET_PROPERTIES [aq_select_target]} target_error]} {
    puts "AQ_TARGET_ERROR $target_error"
    if {$AQ_STAGE eq "syntax"} { aq_finish "AQ_SYNTAX_FAIL" }
    if {$AQ_STAGE eq "fpv"} { aq_finish "AQ_FPV_UNKNOWN" }
    aq_finish "AQ_VACUITY_UNKNOWN"
}

if {$AQ_STAGE eq "syntax"} {
    aq_finish "AQ_SYNTAX_PASS"
}

if {$AQ_STAGE eq "vacuity" && [string match "not_applicable_*" $AQ_VACUITY_MODE]} {
    aq_finish "AQ_VACUITY_NOT_APPLICABLE"
}

if {$AQ_STAGE eq "vacuity" && $AQ_VACUITY_MODE eq "manual_review"} {
    aq_finish "AQ_VACUITY_UNKNOWN"
}

if {[catch {aq_apply_formal_environment} environment_error]} {
    puts "AQ_ENVIRONMENT_ERROR $environment_error"
    if {$AQ_STAGE eq "fpv"} {
        aq_finish "AQ_FPV_UNKNOWN"
    }
    aq_finish "AQ_VACUITY_UNKNOWN"
}

if {$AQ_STAGE eq "fpv"} {
    aq_run_fpv
} elseif {$AQ_STAGE eq "vacuity"} {
    aq_run_vacuity
} else {
    puts "AQ_STAGE_ERROR unknown_stage=$AQ_STAGE"
    aq_finish "AQ_SYNTAX_FAIL"
}
