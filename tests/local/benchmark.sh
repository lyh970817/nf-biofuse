#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Minimal operational benchmark of a BioFuse view served by the plugin, on the compact fixture: what
# running a pipeline through the plugin costs, not how the view compares with other encodings.
#   - mount readiness: seconds from starting the daemon to a readable trio, from the view record;
#   - wall time of whole Nextflow runs: a sequential scan of the .bed, plink2 --freq, and the nf-core/gwas
#     REGENIE test route through the mount (declared identity) and on the fixture trio, cold and warm:
#     "cold" drops the store's (or the trio's) pages from the page cache first with POSIX_FADV_DONTNEED,
#     "warm" repeats the run at once; container images stay cached throughout;
#   - daemon peak RSS (VmHWM) per run;
#   - I/O amplification: the daemon's /proc/<pid>/io from readiness to its exit, rchar (bytes it read from
#     the store, page cache included) and read_bytes (bytes that reached storage) against the bytes it
#     served. The bytes served are the daemon's wchar: a FUSE daemon answers every read by writing the
#     data, behind a 16-byte header, to /dev/fuse. Pages the kernel already caches for the mounted file,
#     such as those the readiness probe pulled in, are not served again, so a sequential scan of one .bed
#     shows the proxy slightly below its size;
#   - the supervisor's own read_bytes at readiness: its digest of the store (and its Python imports) is
#     what brings a cold store in from storage, before the daemon reads any of it.
# A sampler outside Nextflow follows the view's daemon.pid and owner.pid, reading /proc only. Results go
# to stdout as a Markdown table and to <work>/benchmark.tsv. Nothing here asserts a threshold.
#
# Usage: GWAS_TEST_FIXTURES=... GWAS_PIPELINE=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR]
#        [NXF_PROFILE=test,apptainer] benchmark.sh
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

require_fuse_host
require_pipeline
require_store_tools
work=$(work_dir benchmark)
ensure_vcz "$work/store"
image=$(plink2_image "$work/images")
state_root="$work/state"
mkdir -p "$state_root"
baseline=$(fuse_connections)
fixture=$(fixture_genotypes)
plugins=$(pipeline_plugins)
results="$work/benchmark.tsv"
: >"$results"
printf 'id,bed,bim,fam\ncohort,{bed},{bim},{fam}\n' >"$work/manifest.template.csv"
write_gwas_template "$work/cohort.biofuse.template.csv"
failed=0

released() { assert_released "$baseline" "$state_root" "$1" >/dev/null || failed=1; }
now() { date +%s.%N; }
seconds_since() { awk -v started="$1" -v ended="$(now)" 'BEGIN { printf "%.2f", ended - started }'; }

# Follow one view from outside: once its manifest exists (the view is ready), note the daemon's and the
# supervisor's I/O; then sample the daemon until it exits and write "rchar read_bytes wchar vmhwm_kb" of
# its last sample less the ready one, plus the supervisor's read_bytes at readiness, to $2.
sample_view() { # $1 = manifest path, $2 = output file
    local manifest=$1 output=$2 daemon="" owner="" directory ready="" last="" hwm="" supervisor_read=""
    while [ ! -e "$manifest" ]; do
        [ -e "$output.stop" ] && return 0
        sleep 0.05
    done
    for directory in "$state_root"/nf-biofuse/view-*/; do
        [ -f "${directory}daemon.pid" ] && daemon=$(cut -d' ' -f1 "${directory}daemon.pid") &&
            owner=$(cut -d' ' -f1 "${directory}owner.pid")
    done
    [ -n "$daemon" ] || return 0
    ready=$(awk '/^rchar:/ { r = $2 } /^read_bytes:/ { b = $2 } /^wchar:/ { w = $2 } END { print r, b, w }' "/proc/$daemon/io")
    supervisor_read=$(awk '/^read_bytes:/ { print $2 }' "/proc/$owner/io" 2>/dev/null)
    while sample=$(awk '/^rchar:/ { r = $2 } /^read_bytes:/ { b = $2 } /^wchar:/ { w = $2 } END { print r, b, w }' "/proc/$daemon/io" 2>/dev/null) &&
        [ -n "$sample" ]; do
        last=$sample
        hwm=$(awk '/^VmHWM:/ { print $2 }' "/proc/$daemon/status" 2>/dev/null || echo "$hwm")
        sleep 0.1
    done
    awk -v ready="$ready" -v last="$last" -v hwm="$hwm" -v supervisor="$supervisor_read" 'BEGIN {
        split(ready, a, " "); split(last, b, " ")
        printf "%d %d %d %s %s\n", b[1] - a[1], b[2] - a[2], b[3] - a[3], hwm, supervisor
    }' >"$output"
}

# One Nextflow run through the plugin, sampled, appended to the results as one TSV row.
measure_plugin_run() { # $1 = label, $2 = run dir, $3 = manifest, then the nextflow command
    local label=$1 run=$2 manifest=$3 started rc sampler readiness
    shift 3
    rm -f "$run/sample" "$run/sample.stop"
    sample_view "$manifest" "$run/sample" &
    sampler=$!
    started=$(now)
    (cd "$run" && "$@") >"$run/nextflow.stdout" 2>&1
    rc=$?
    wall=$(seconds_since "$started")
    touch "$run/sample.stop"
    wait "$sampler"
    readiness=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["mount_readiness_seconds"])' "$run/$BASENAME.biofuse_view.json" 2>/dev/null)
    read -r rchar read_bytes wchar hwm supervisor_read <"$run/sample" 2>/dev/null
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$label" "$rc" "$wall" "${readiness:-}" "${rchar:-}" "${read_bytes:-}" \
        "${wchar:-}" "${hwm:-}" "${supervisor_read:-}" >>"$results"
    [ "$rc" -eq 0 ] || failed=1
}

# Two controlled consumers, each on a fresh mount, so neither is served from pages the kernel cached for
# the other: a full sequential read of the .bed, and plink2 --freq.
for consumer in scan ladder; do
    label=$([ "$consumer" = scan ] && echo sequential_scan || echo plink2_freq)
    run="$work/$label"
    rm -rf "$run"
    mkdir -p "$run"
    write_plugin_config "$run/biofuse.config" "$state_root" "$run/cohort.biofuse.csv" "$work/manifest.template.csv" declared ""
    measure_plugin_run "$label" "$run" "$run/cohort.biofuse.csv" \
        nextflow -log "$run/nextflow.log" run "$TESTS_DIR/consumer" -c "$run/biofuse.config" \
        --manifest "$run/cohort.biofuse.csv" --mode "$consumer" --consumers 1 --plink2_image "$image" \
        --outdir "$run/out" -work-dir "$run/work" -ansi-log false
    released "the $label run"
done

# The nf-core/gwas route, through the mount and on the trio, cold then warm.
for pass in cold warm; do
    run="$work/e2e_mount_$pass"
    rm -rf "$run"
    mkdir -p "$run"
    [ "$pass" = cold ] && evict_page_cache "$VCZ"
    write_plugin_config "$run/biofuse.config" "$state_root" "$run/cohort.biofuse.csv" "$work/cohort.biofuse.template.csv" declared "$plugins"
    pipeline_argv "$run" "$run/cohort.biofuse.csv" -c "$run/biofuse.config"
    measure_plugin_run "e2e_mount_$pass" "$run" "$run/cohort.biofuse.csv" "${PIPELINE[@]}"
    released "the $pass mount run"

    run="$work/e2e_control_$pass"
    rm -rf "$run"
    mkdir -p "$run"
    [ "$pass" = cold ] && evict_page_cache "$fixture/example_all.bed" "$fixture/example_all.bim" "$fixture/example_all.fam"
    write_control_manifest "$run/cohort_manifest.csv"
    pipeline_argv "$run" "$run/cohort_manifest.csv"
    started=$(now)
    (cd "$run" && "${PIPELINE[@]}") >"$run/nextflow.stdout" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || failed=1
    printf 'e2e_control_%s\t%s\t%s\t\t\t\t\t\t\n' "$pass" "$rc" "$(seconds_since "$started")" >>"$results"
done

bed_size=$(wc -c <"$fixture/example_all.bed")
store_size=$(du -sb "$VCZ" | cut -f1)
python3 - "$results" "$bed_size" "$store_size" <<'PY'
import csv
import sys

rows = {row[0]: row for row in csv.reader(open(sys.argv[1]), delimiter="\t")}
bed, store = int(sys.argv[2]), int(sys.argv[3])


def field(label, index, default="n/a"):
    row = rows.get(label)
    return row[index] if row and len(row) > index and row[index] != "" else default


def mib(kib):
    return f"{int(kib) / 1024:.1f}" if kib != "n/a" else kib


def ratio(numerator, denominator):
    if "n/a" in (numerator, denominator) or not int(denominator):
        return "n/a"
    return f"{int(numerator) / int(denominator):.2f}"


print(f"BED size {bed} bytes; store on disk {store} bytes\n")
print(
    "| workload | status | nextflow wall s | readiness s | daemon peak RSS MiB | served B (daemon wchar) "
    "| daemon rchar B | daemon read_bytes B | rchar/served | rchar/BED | supervisor read_bytes B at readiness |"
)
print("|---|---|---|---|---|---|---|---|---|---|---|")
for label in ("sequential_scan", "plink2_freq", "e2e_mount_cold", "e2e_mount_warm", "e2e_control_cold", "e2e_control_warm"):
    served, rchar = field(label, 6), field(label, 4)
    print(
        f"| {label} | {field(label, 1)} | {field(label, 2)} | {field(label, 3)} | {mib(field(label, 7))} | {served} "
        f"| {rchar} | {field(label, 5)} | {ratio(rchar, served)} | {ratio(rchar, str(bed))} | {field(label, 8)} |"
    )
PY
if [ "$failed" -eq 0 ]; then echo "BENCHMARK: PASS (every run completed; raw rows in $results)"; else
    echo "BENCHMARK: FAIL (a run failed or leaked; see $work)"
fi
exit "$failed"
