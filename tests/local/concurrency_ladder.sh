#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Concurrency ladder: N = 1, 2, 4, 8 concurrent Apptainer `plink2 --freq` tasks reading ONE mount, which
# is how a pipeline's parallel tasks read a view. Each level is its own Nextflow run of the consumer
# pipeline with the plugin, so each gets a fresh mount and a bad level cannot poison the next. A level
# passes when:
#   - the run exits 0 within CONSUMER_TIMEOUT (a task stuck on a wedged mount would hold it);
#   - every .afreq is identical to plink2 --freq on the fixture trio itself;
#   - the run leaves no mount, no supervisor, daemon or watchdog and no wedged connection behind.
# A wedged connection is aborted through /sys/fs/fuse/connections rather than by killing its readers.
#
# Usage: GWAS_TEST_FIXTURES=... GWAS_PIPELINE=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR] [LEVELS="1 2 4 8"]
#        [CONSUMER_TIMEOUT=300] concurrency_ladder.sh
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

LEVELS=${LEVELS:-"1 2 4 8"}
CONSUMER_TIMEOUT=${CONSUMER_TIMEOUT:-300}

require_fuse_host
require_store_tools
[ -n "${PLINK2_SIF:-}" ] || require_pipeline
work=$(work_dir ladder)
ensure_vcz "$work/store"
image=$(plink2_image "$work/images")
state_root="$work/state"
mkdir -p "$state_root" "$work/reference"
baseline=$(fuse_connections)
fixture=$(fixture_genotypes)
printf 'id,bed,bim,fam\ncohort,{bed},{bim},{fam}\n' >"$work/manifest.template.csv"

plink2_in_image "$image" "$fixture" "$work/reference" -- \
    --bfile "$fixture/example_all" --freq --out "$work/reference/fixture" >"$work/reference/fixture.stdout" 2>&1 ||
    fail "plink2 --freq on the fixture trio failed; see $work/reference/fixture.stdout"
reference="$work/reference/fixture.afreq"

# The largest number of tasks whose [start, complete] intervals overlap, from the trace.
max_overlap() { # $1 = trace file
    python3 - "$1" <<'PY' 2>/dev/null
import csv
import sys
from datetime import datetime

events = []
for row in csv.DictReader(open(sys.argv[1]), delimiter="\t"):
    parse = lambda value: datetime.strptime(value, "%Y-%m-%d %H:%M:%S.%f")
    events += [(parse(row["start"]), 1), (parse(row["complete"]), -1)]
running = peak = 0
for _, step in sorted(events, key=lambda event: (event[0], event[1])):
    running += step
    peak = max(peak, running)
print(peak)
PY
}

echo "store: $VCZ   run timeout: ${CONSUMER_TIMEOUT}s   reference: $(sha256sum "$reference" | cut -c1-16)"
failed=0
for n in $LEVELS; do
    run="$work/n$n"
    rm -rf "$run"
    mkdir -p "$run"
    write_plugin_config "$run/biofuse.config" "$state_root" "$run/cohort.biofuse.csv" "$work/manifest.template.csv" declared ""
    started=$(date +%s)
    # The outer deadline sends SIGTERM, on which Nextflow aborts and the plugin tears the mount down.
    (cd "$run" && timeout --kill-after=60 "$CONSUMER_TIMEOUT" nextflow -log "$run/nextflow.log" run "$TESTS_DIR/consumer" \
        -c "$run/biofuse.config" --manifest "$run/cohort.biofuse.csv" --mode ladder --consumers "$n" \
        --plink2_image "$image" --outdir "$run/out" -work-dir "$run/work" -with-trace "$run/trace.txt" \
        -ansi-log false) >"$run/stdout" 2>&1
    rc=$?
    elapsed=$(($(date +%s) - started))
    identical=0 bad=0
    for i in $(seq 1 "$n"); do
        if cmp -s "$run/out/c_$i.afreq" "$reference"; then
            identical=$((identical + 1))
        else
            bad=$((bad + 1))
            echo "    consumer $i: no identical .afreq"
        fi
    done
    peak=$(max_overlap "$run/trace.txt")
    printf '  N=%-2s nextflow rc=%s  %3ss  identical .afreq %s/%s  (at most %s tasks running at once)\n' \
        "$n" "$rc" "$elapsed" "$identical" "$n" "${peak:-?}"
    if [ "$rc" -eq 0 ] && [ "$bad" -eq 0 ] && assert_released "$baseline" "$state_root" "N=$n"; then
        echo "  N=$n PASS"
    else
        echo "  N=$n FAIL; stopping the ladder (run under $run)"
        failed=1
        break
    fi
done
if [ "$failed" -eq 0 ]; then echo "CONCURRENCY LADDER: PASS"; else echo "CONCURRENCY LADDER: FAIL"; fi
exit "$failed"
