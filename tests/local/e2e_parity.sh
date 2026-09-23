#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# End-to-end parity on nf-core/gwas: `nextflow run $GWAS_PIPELINE -profile test,apptainer` of the REGENIE
# test route, run one after another, compared by compare_results.sh.
#   declared  with the plugin, the manifest declaring the view's biofuse:<hex> identity
#   digest    with the plugin, the manifest leaving the identity for the pipeline to digest
#   control   on the fixture's own example_all.{bed,bim,fam}, which the view serves under the same names
# Declared mode runs one task fewer than the control: a declared identity skips PREPARE_GENOTYPE_SOURCE.
# Then the declared run and the control are each resumed once: the mount is remounted at the same
# content-addressed path, so the declared resume must cache exactly the tasks the control's resume
# caches (every task whose inputs are the same; nf-core's MultiQC report re-runs in both, because its
# workflow summary carries the run's own timestamp).
#
# Usage: GWAS_TEST_FIXTURES=... GWAS_PIPELINE=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR]
#        [NXF_PROFILE=test,apptainer] e2e_parity.sh
# NXF_APPTAINER_CACHEDIR is passed through to Nextflow, so a warm image cache spares the downloads.
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

require_fuse_host
require_pipeline
require_store_tools
work=$(work_dir e2e)
ensure_vcz "$work/store"
state_root="$work/state"
mkdir -p "$state_root"
baseline=$(fuse_connections)
plugins=$(pipeline_plugins)
write_gwas_template "$work/cohort.biofuse.template.csv"
failed=0

run_through_plugin() { # $1 = identity mode, $2 = "resume" to resume the previous run in place
    local run="$work/$1" rc trace=trace.txt
    if [ "${2:-}" = resume ]; then
        trace="trace-resume.txt"
    else
        rm -rf "$run"
        mkdir -p "$run"
    fi
    write_plugin_config "$run/biofuse.config" "$state_root" "$run/cohort.biofuse.csv" "$work/cohort.biofuse.template.csv" "$1" "$plugins"
    pipeline_argv "$run" "$run/cohort.biofuse.csv" -c "$run/biofuse.config" -with-trace "$run/$trace" ${2:+-resume}
    (cd "$run" && "${PIPELINE[@]}") >"$run/nextflow.stdout" 2>&1
    rc=$?
    echo "$1${2:+ (resumed)}: nextflow rc=$rc $(grep -ho 'ready in [0-9.]*s' "$run/nextflow.stdout" | head -n 1)"
    [ "$rc" -eq 0 ] || { tail -n 30 "$run/nextflow.stdout"; failed=1; }
    assert_released "$baseline" "$state_root" "the $1${2:+ resumed} run" || failed=1
    [ ! -e "$run/cohort.biofuse.csv" ] || { echo "  the manifest outlived the run"; failed=1; }
}

run_control() { # $1 = "resume" to resume the previous run in place
    local run="$work/control" rc trace=trace.txt
    if [ "${1:-}" = resume ]; then
        trace="trace-resume.txt"
    else
        rm -rf "$run"
        mkdir -p "$run"
        write_control_manifest "$run/cohort_manifest.csv"
    fi
    pipeline_argv "$run" "$run/cohort_manifest.csv" -with-trace "$run/$trace" ${1:+-resume}
    (cd "$run" && "${PIPELINE[@]}") >"$run/nextflow.stdout" 2>&1
    rc=$?
    echo "control${1:+ (resumed)}: nextflow rc=$rc"
    [ "$rc" -eq 0 ] || { tail -n 30 "$run/nextflow.stdout"; failed=1; }
}

# "NAME STATUS" per task of a trace, sorted.
statuses() { # $1 = trace file
    awk -F'\t' '
        NR == 1 { for (i = 1; i <= NF; i++) column[$i] = i; next }
        {
            name = $column["name"]
            sub(/ \(.*$/, "", name)
            sub(/^.*:/, "", name)
            print name, $column["status"]
        }' "$1" | sort
}

echo "store: $VCZ"
echo "pipeline: $GWAS_PIPELINE (-profile ${NXF_PROFILE:-test,apptainer}, plugins: $plugins,$PLUGIN_ID)"
run_through_plugin declared
run_through_plugin digest
run_control

if [ "$failed" -eq 0 ]; then
    for mode in declared digest; do
        echo "compare $mode with control"
        bash "$TESTS_DIR/compare_results.sh" "$work/control" "$work/$mode" "$mode" || failed=1
    done
fi

if [ "$failed" -eq 0 ]; then
    run_through_plugin declared resume
    run_control resume
    declared_resume=$(statuses "$work/declared/trace-resume.txt")
    control_resume=$(statuses "$work/control/trace-resume.txt")
    rerun_declared=$(printf '%s\n' "$declared_resume" | grep -v ' CACHED$' | cut -d' ' -f1 | tr '\n' ' ')
    rerun_control=$(printf '%s\n' "$control_resume" | grep -v ' CACHED$' | grep -v '^PREPARE_GENOTYPE_SOURCE ' | cut -d' ' -f1 | tr '\n' ' ')
    echo "  resumed declared run: $(printf '%s\n' "$declared_resume" | grep -c ' CACHED$') cached, re-ran: ${rerun_declared:-none}"
    echo "  resumed control run:  $(printf '%s\n' "$control_resume" | grep -c ' CACHED$') cached, re-ran: ${rerun_control:-none}"
    if [ "$rerun_declared" = "$rerun_control" ]; then
        echo "  PASS  the mount adds no cache miss on -resume"
    else
        echo "  FAIL  the declared resume re-ran tasks the control's resume cached"
        failed=1
    fi
fi

if [ "$failed" -eq 0 ]; then echo "E2E PARITY: PASS"; else echo "E2E PARITY: FAIL (runs under $work)"; fi
exit "$failed"
