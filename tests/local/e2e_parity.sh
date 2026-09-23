#!/usr/bin/env bash
# Written by the nf-core/gwas team and released under the MIT license.
#
# End-to-end parity: three `nextflow run <repo> -profile test,apptainer` runs of the REGENIE test route,
# one after another, compared by compare_results.sh.
#   declared  through the launcher, the manifest declaring the view's biofuse:<hex> identity
#   digest    through the launcher, the manifest leaving the identity for the pipeline to digest
#   control   on the fixture's own example_all.{bed,bim,fam}, which the view serves under the same names
# Declared mode runs one task fewer than the control: a declared identity skips PREPARE_GENOTYPE_SOURCE.
#
# Usage: GWAS_TEST_FIXTURES=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR] [NXF_PROFILE=test,apptainer]
#        e2e_parity.sh
# NXF_APPTAINER_CACHEDIR is passed through to Nextflow, so a warm image cache spares the downloads.
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

require_fuse_host
command -v nextflow >/dev/null 2>&1 || skip "nextflow is not on PATH"
require_store_tools
work=$(work_dir e2e)
ensure_vcz "$work/store"
state_root="$work/state"
mkdir -p "$state_root"
baseline=$(fuse_connections)
failed=0

run_through_view() { # $1 = identity mode
    local run="$work/$1" rc
    rm -rf "$run"
    mkdir -p "$run"
    pipeline_argv "$run" '{cohort_manifest}'
    (cd "$run" && launch_view "$state_root" "$run" "$1" -- "${PIPELINE[@]}") >"$run/launcher.log" 2>&1
    rc=$?
    echo "$1: launcher rc=$rc $(grep -ho 'mount ready in [0-9.]*s' "$run/launcher.log")"
    [ "$rc" -eq 0 ] || { tail -n 30 "$run/launcher.log"; failed=1; }
    assert_released "$baseline" "$state_root" "the $1 run" || failed=1
}

run_control() {
    local run="$work/control" rc
    rm -rf "$run"
    mkdir -p "$run"
    write_control_manifest "$run/cohort_manifest.csv"
    pipeline_argv "$run" "$run/cohort_manifest.csv"
    (cd "$run" && "${PIPELINE[@]}") >"$run/nextflow.stdout" 2>&1
    rc=$?
    echo "control: nextflow rc=$rc"
    [ "$rc" -eq 0 ] || { tail -n 30 "$run/nextflow.stdout"; failed=1; }
}

echo "store: $VCZ"
echo "pipeline: $REPO_ROOT (-profile ${NXF_PROFILE:-test,apptainer})"
run_through_view declared
run_through_view digest
run_control

if [ "$failed" -eq 0 ]; then
    for mode in declared digest; do
        echo "compare $mode with control"
        bash "$TESTS_DIR/compare_results.sh" "$work/control" "$work/$mode" "$mode" || failed=1
    done
fi
if [ "$failed" -eq 0 ]; then echo "E2E PARITY: PASS"; else echo "E2E PARITY: FAIL (runs under $work)"; fi
exit "$failed"
