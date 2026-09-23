#!/usr/bin/env bash
# Written by the nf-core/gwas team and released under the MIT license.
#
# Minimal operational benchmark of a BioFuse view on the compact fixture: what running the pipeline
# through the launcher costs, not how the view compares with other encodings or scatter layouts.
#   - mount readiness: seconds from starting the daemon to a readable trio, per launcher run;
#   - E2E wall time of the REGENIE test route through the mount (declared identity) and on the fixture
#     trio, cold and warm: "cold" drops the store's (or the trio's) pages from the page cache first with
#     POSIX_FADV_DONTNEED, "warm" repeats the run at once; container images stay cached throughout;
#   - daemon peak RSS (VmHWM) per launcher run;
#   - I/O amplification: the daemon's /proc/<pid>/io over each workload, rchar (bytes it read from the
#     store, page cache included) and read_bytes (bytes that reached storage) against the bytes it served.
#     The launcher exposes no biofuse access log, so the bytes served are the daemon's wchar: a FUSE
#     daemon answers every read by writing the data, behind a 16-byte header, to /dev/fuse. Pages the
#     kernel already caches for the mounted file, such as those the launcher's readiness probe pulled
#     in, are not served again, so the sequential scan of one .bed shows the proxy slightly below its size.
#   - the launcher's own read_bytes before the command starts: its digest of the store (and its Python
#     imports) is what brings a cold store in from storage, before the daemon reads any of it.
# Results go to stdout as a Markdown table and to <work>/benchmark.tsv. Nothing here asserts a threshold.
#
# Usage: GWAS_TEST_FIXTURES=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR] [NXF_PROFILE=test,apptainer]
#        benchmark.sh
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

# ---- Inside the launched command ----

# The daemon's cumulative "rchar read_bytes wchar"; its pid file sits beside the mountpoint.
daemon_io() { # $1 = pid
    awk '/^rchar:/ { r = $2 } /^read_bytes:/ { b = $2 } /^wchar:/ { w = $2 } END { print r, b, w }' "/proc/$1/io"
}

# Run a command and append one TSV row: label, status, wall seconds, readiness, the daemon's rchar,
# read_bytes and wchar deltas over the command, its VmHWM in kB at the end, and what the launcher (the
# command's parent) had read by the time the command started, which is mostly its digest of the store.
measure() { # $1 = label, then the command
    local label=$1 pid before after started ended rc launcher
    shift
    pid=$(cut -d' ' -f1 "$(dirname "$BIOFUSE_MOUNT")/daemon.pid")
    launcher=$(daemon_io "$PPID")
    before=$(daemon_io "$pid")
    started=$(date +%s.%N)
    "$@"
    rc=$?
    ended=$(date +%s.%N)
    after=$(daemon_io "$pid")
    awk -v label="$label" -v rc="$rc" -v started="$started" -v ended="$ended" \
        -v readiness="$BIOFUSE_READINESS_SECONDS" -v before="$before" -v after="$after" \
        -v hwm="$(awk '/^VmHWM:/ { print $2 }' "/proc/$pid/status")" -v launcher="$launcher" 'BEGIN {
            split(before, b, " "); split(after, a, " "); split(launcher, l, " ")
            printf "%s\t%s\t%.2f\t%s\t%d\t%d\t%d\t%s\t%d\t%d\n", label, rc, ended - started, readiness,
                a[1] - b[1], a[2] - b[2], a[3] - b[3], hwm, l[1], l[2]
        }' >>"$BENCH_RESULTS"
    return "$rc"
}

if [ "${1:-}" = "--consumer" ]; then
    consumer=$2 image=$3 out=$4
    if [ "$consumer" = sequential_scan ]; then
        measure sequential_scan timeout "$READ_TIMEOUT" cat "$BIOFUSE_MOUNT/$BIOFUSE_BASENAME.bed" >/dev/null
    else
        measure plink2_freq plink2_in_image "$image" "$BIOFUSE_MOUNT" "$out" -- \
            --bfile "$BIOFUSE_MOUNT/$BIOFUSE_BASENAME" --freq --out "$out/mounted" >"$out/plink2.stdout" 2>&1
    fi
    exit $?
fi
if [ "${1:-}" = "--e2e" ]; then
    label=$2
    shift 2
    measure "$label" "$@"
    exit $?
fi

# ---- Driver ----

require_fuse_host
command -v nextflow >/dev/null 2>&1 || skip "nextflow is not on PATH"
require_store_tools
work=$(work_dir benchmark)
ensure_vcz "$work/store"
image=$(plink2_image "$work/images")
state_root="$work/state"
mkdir -p "$state_root"
baseline=$(fuse_connections)
fixture=$(fixture_genotypes)
export BENCH_RESULTS="$work/benchmark.tsv"
: >"$BENCH_RESULTS"
: >"$work/launcher_totals.tsv"
failed=0

released() { assert_released "$baseline" "$state_root" "$1" >/dev/null || failed=1; }
seconds_since() { awk -v started="$1" -v ended="$(date +%s.%N)" 'BEGIN { printf "%.2f", ended - started }'; }

# Two controlled consumers, each on a fresh mount, so neither is served from pages the kernel cached
# for the other: a full sequential read of the .bed, and plink2 --freq.
for consumer in sequential_scan plink2_freq; do
    out="$work/$consumer"
    rm -rf "$out"
    mkdir -p "$out"
    launch_view "$state_root" "$out" declared -- \
        bash "${BASH_SOURCE[0]}" --consumer "$consumer" "$image" "$out" >"$out/launcher.log" 2>&1 || failed=1
    released "the $consumer run"
done

# The E2E route, through the mount and on the trio, cold then warm.
for pass in cold warm; do
    run="$work/mount_$pass"
    rm -rf "$run"
    mkdir -p "$run"
    [ "$pass" = cold ] && evict_page_cache "$VCZ"
    pipeline_argv "$run" '{cohort_manifest}'
    started=$(date +%s.%N)
    (cd "$run" && launch_view "$state_root" "$run" declared -- \
        bash "${BASH_SOURCE[0]}" --e2e "e2e_mount_$pass" "${PIPELINE[@]}") >"$run/launcher.log" 2>&1 || failed=1
    printf 'launcher_total_%s\t%s\n' "$pass" "$(seconds_since "$started")" >>"$work/launcher_totals.tsv"
    released "the $pass mount run"

    run="$work/control_$pass"
    rm -rf "$run"
    mkdir -p "$run"
    [ "$pass" = cold ] && evict_page_cache "$fixture/$BASENAME.bed" "$fixture/$BASENAME.bim" "$fixture/$BASENAME.fam"
    write_control_manifest "$run/cohort_manifest.csv"
    pipeline_argv "$run" "$run/cohort_manifest.csv"
    started=$(date +%s.%N)
    (cd "$run" && "${PIPELINE[@]}") >"$run/nextflow.stdout" 2>&1
    rc=$?
    [ "$rc" -eq 0 ] || failed=1
    printf 'e2e_control_%s\t%s\t%s\t\t\t\t\t\n' "$pass" "$rc" "$(seconds_since "$started")" >>"$BENCH_RESULTS"
done

bed_size=$(wc -c <"$fixture/$BASENAME.bed")
store_size=$(du -sb "$VCZ" | cut -f1)
python3 - "$BENCH_RESULTS" "$work/launcher_totals.tsv" "$bed_size" "$store_size" <<'PY'
import csv
import sys

rows = {row[0]: row for row in csv.reader(open(sys.argv[1]), delimiter="\t")}
totals = dict(line.split("\t") for line in open(sys.argv[2]).read().splitlines())
bed, store = int(sys.argv[3]), int(sys.argv[4])


def field(label, index, default="n/a"):
    row = rows.get(label)
    return row[index] if row and len(row) > index and row[index] != "" else default


def mib(kib):
    return f"{int(kib) / 1024:.1f}" if kib != "n/a" else kib


def ratio(numerator, denominator):
    return f"{int(numerator) / int(denominator):.2f}" if "n/a" not in (numerator, denominator) and int(denominator) else "n/a"


print(f"BED size {bed} bytes; store on disk {store} bytes\n")
print(
    "| workload | status | wall s | readiness s | daemon peak RSS MiB | served B (daemon wchar) | daemon rchar B "
    "| daemon read_bytes B | rchar/served | rchar/BED | launcher read_bytes B before the command |"
)
print("|---|---|---|---|---|---|---|---|---|---|---|")
for label in ("sequential_scan", "plink2_freq", "e2e_mount_cold", "e2e_mount_warm", "e2e_control_cold", "e2e_control_warm"):
    served, rchar = field(label, 6), field(label, 4)
    print(
        f"| {label} | {field(label, 1)} | {field(label, 2)} | {field(label, 3)} | {mib(field(label, 7))} | {served} "
        f"| {rchar} | {field(label, 5)} | {ratio(rchar, served)} | {ratio(rchar, str(bed))} | {field(label, 9)} |"
    )
print()
print("| launcher wall s (mount, E2E and teardown) | cold | warm |")
print("|---|---|---|")
print(f"| through the mount | {totals.get('launcher_total_cold', 'n/a')} | {totals.get('launcher_total_warm', 'n/a')} |")
PY
if [ "$failed" -eq 0 ]; then echo "BENCHMARK: PASS (every run completed; raw rows in $BENCH_RESULTS)"; else
    echo "BENCHMARK: FAIL (a run failed or leaked; see $work)"
fi
exit "$failed"
