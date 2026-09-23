#!/usr/bin/env bash
# Written by the nf-core/gwas team and released under the MIT license.
#
# Concurrency ladder: N = 1, 2, 4, 8 concurrent Apptainer `plink2 --freq` consumers of ONE mount, which is
# how a pipeline's parallel tasks read a view. Each level runs under its own launcher invocation, so a
# bad level cannot poison the next, and passes when:
#   - every consumer exits 0 within CONSUMER_TIMEOUT (a consumer stuck on a wedged mount would not);
#   - every .afreq is identical to plink2 --freq on the fixture trio itself;
#   - the launcher exits 0 and leaves no mount, no daemon or watchdog and no wedged connection behind.
# A wedged connection is aborted through /sys/fs/fuse/connections rather than by killing its readers.
#
# Usage: GWAS_TEST_FIXTURES=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR] [LEVELS="1 2 4 8"]
#        [CONSUMER_TIMEOUT=180] concurrency_ladder.sh
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

LEVELS=${LEVELS:-"1 2 4 8"}
CONSUMER_TIMEOUT=${CONSUMER_TIMEOUT:-180}

# ---- Inside the launched command: N concurrent consumers of the mount ----
if [ "${1:-}" = "--inside" ]; then
    n=$2 image=$3 out=$4
    pids=()
    for i in $(seq 1 "$n"); do
        (
            READ_TIMEOUT=$CONSUMER_TIMEOUT plink2_in_image "$image" "$BIOFUSE_MOUNT" "$out" -- \
                --bfile "$BIOFUSE_MOUNT/$BIOFUSE_BASENAME" --freq --out "$out/c_$i" >"$out/c_$i.stdout" 2>&1
            echo "$?" >"$out/c_$i.rc"
        ) &
        pids+=($!)
    done
    for pid in "${pids[@]}"; do wait "$pid"; done
    exit 0
fi

require_fuse_host
require_store_tools
work=$(work_dir ladder)
ensure_vcz "$work/store"
image=$(plink2_image "$work/images")
state_root="$work/state"
mkdir -p "$state_root" "$work/reference"
baseline=$(fuse_connections)
fixture=$(fixture_genotypes)

plink2_in_image "$image" "$fixture" "$work/reference" -- \
    --bfile "$fixture/$BASENAME" --freq --out "$work/reference/fixture" >"$work/reference/fixture.stdout" 2>&1 ||
    fail "plink2 --freq on the fixture trio failed; see $work/reference/fixture.stdout"
reference="$work/reference/fixture.afreq"

echo "store: $VCZ   consumer timeout: ${CONSUMER_TIMEOUT}s   reference: $(sha256sum "$reference" | cut -c1-16)"
failed=0
for n in $LEVELS; do
    out="$work/n$n"
    rm -rf "$out"
    mkdir -p "$out"
    started=$(date +%s)
    # The outer deadline outlives every consumer's; the launcher tears down on the TERM it sends.
    launcher_argv "$state_root" "$out" declared
    timeout --kill-after=60 $((CONSUMER_TIMEOUT + 120)) "${LAUNCH[@]}" \
        env CONSUMER_TIMEOUT="$CONSUMER_TIMEOUT" bash "${BASH_SOURCE[0]}" --inside "$n" "$image" "$out" \
        >"$out/launcher.log" 2>&1
    rc=$?
    elapsed=$(($(date +%s) - started))
    identical=0 bad=0
    for i in $(seq 1 "$n"); do
        consumer_rc=$(cat "$out/c_$i.rc" 2>/dev/null)
        if [ "$consumer_rc" = 0 ] && cmp -s "$out/c_$i.afreq" "$reference"; then
            identical=$((identical + 1))
        else
            bad=$((bad + 1))
            echo "    consumer $i: rc=${consumer_rc:-none}$([ "$consumer_rc" = 124 ] && echo ' (timed out)')"
            tail -n 2 "$out/c_$i.stdout" 2>/dev/null | sed 's/^/        /'
        fi
    done
    printf '  N=%-2s launcher rc=%s  %3ss  identical .afreq %s/%s\n' "$n" "$rc" "$elapsed" "$identical" "$n"
    if [ "$rc" -eq 0 ] && [ "$bad" -eq 0 ] && assert_released "$baseline" "$state_root" "N=$n"; then
        echo "  N=$n PASS"
    else
        echo "  N=$n FAIL; stopping the ladder (launcher log $out/launcher.log)"
        failed=1
        break
    fi
done
if [ "$failed" -eq 0 ]; then echo "CONCURRENCY LADDER: PASS"; else echo "CONCURRENCY LADDER: FAIL"; fi
exit "$failed"
