#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# The local FUSE tier of nf-biofuse, one heavy step at a time, every step driving Nextflow with the
# installed plugin:
#   lifecycle   lifecycle_matrix.sh: every way a run can end leaves no mount, process, state or wedge
#   parity      parity_check.sh: the mounted trio is the fixture trio, and plink2 agrees on both
#   e2e         e2e_parity.sh: nf-core/gwas -profile test,apptainer through the view equals the run on
#               the trio, in declared and digest mode, and -resume caches what the control's resume does
#   ladder      concurrency_ladder.sh: 1, 2, 4 and 8 concurrent Apptainer consumers of one mount
#   benchmark   benchmark.sh: readiness, wall time, daemon peak RSS and I/O amplification
# It is local only: CI never runs it.
#
# Exits 0 printing "SKIPPED: <reason>" when the host cannot run the tier: no /dev/fuse, no supervisor,
# apptainer or nextflow, the plugin not installed, or no GWAS_TEST_FIXTURES. Otherwise it prints PASS,
# FAIL or SKIPPED for each step and exits non-zero if any step failed.
#
# Environment (see local_tier_lib.sh for each):
#   SUPERVISOR, NXF_PLUGINS_DIR, GWAS_TEST_FIXTURES, VCZ, GWAS_PIPELINE, GWAS_PLUGINS, PLINK2_SIF,
#   NXF_APPTAINER_CACHEDIR, LOCAL_TIER_ROOT
#   STEPS                   a subset of "lifecycle parity e2e ladder benchmark" to run
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

require_fuse_host
require_store_tools

LOCAL_TIER_ROOT=${LOCAL_TIER_ROOT:-$(mktemp -d "${TMPDIR:-/tmp}/nf-biofuse-local-tier.XXXXXX")}
mkdir -p "$LOCAL_TIER_ROOT"
LOCAL_TIER_ROOT=$(cd "$LOCAL_TIER_ROOT" && pwd -P)
export LOCAL_TIER_ROOT
STEPS=${STEPS:-"lifecycle parity e2e ladder benchmark"}
echo "local tier root: $LOCAL_TIER_ROOT"
echo "plugin: $PLUGIN_ID from ${NXF_PLUGINS_DIR:-$HOME/.nextflow/plugins}; supervisor: $SUPERVISOR"

# One store for every step, so each step measures the same view.
if [ -z "${VCZ:-}" ]; then
    echo "building the store from the fixture VCF"
    ensure_vcz "$LOCAL_TIER_ROOT/store"
fi
export VCZ
echo "store: $VCZ"

declare -A scripts=(
    [lifecycle]=lifecycle_matrix.sh
    [parity]=parity_check.sh
    [e2e]=e2e_parity.sh
    [ladder]=concurrency_ladder.sh
    [benchmark]=benchmark.sh
)
summary=()
failed=0
for step in $STEPS; do
    script=${scripts[$step]:-}
    if [ -z "$script" ]; then
        echo "unknown step '$step'" >&2
        exit 2
    fi
    log="$LOCAL_TIER_ROOT/$step.log"
    echo
    echo "=== $step ($script) ==="
    started=$(date +%s)
    bash "$TESTS_DIR/$script" 2>&1 | tee "$log"
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ] && grep -q '^SKIPPED: ' "$log"; then
        result="SKIPPED"
    elif [ "$rc" -eq 0 ]; then
        result="PASS"
    else
        result="FAIL"
        failed=1
    fi
    summary+=("$(printf '%-10s %-7s %4ss  %s' "$step" "$result" "$(($(date +%s) - started))" "$log")")
done

# Nothing any step mounted may outlive the tier.
leftover=$(mounts_under "$LOCAL_TIER_ROOT")
echo
echo "=== summary ==="
printf '%s\n' "${summary[@]}"
if [ "$leftover" -ne 0 ]; then
    echo "FAIL: $leftover biofuse mount(s) left under $LOCAL_TIER_ROOT; reaping"
    for state_root in "$LOCAL_TIER_ROOT"/*/state; do
        [ -d "$state_root" ] && "$SUPERVISOR" reap-stale --state-root "$state_root"
    done
    failed=1
fi
if [ "$failed" -eq 0 ]; then echo "LOCAL TIER: PASS"; else echo "LOCAL TIER: FAIL"; fi
exit "$failed"
