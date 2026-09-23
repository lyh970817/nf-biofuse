#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Lifecycle robustness matrix for the plugin. Every case runs Nextflow on the validation pipeline with
# the plugin serving VCZ, ends the run one way or another, and asserts afterwards:
#   - no biofuse mount left in /proc/mounts under the test state root;
#   - no supervisor, mount daemon or watchdog left alive for that state root;
#   - no view state directory left behind (the empty view-<id>.lock files are kept by design);
#   - no wedged FUSE connection (outstanding requests with no live daemon) that the run created;
#   - no manifest left at the path the plugin wrote it to.
#
# Usage: GWAS_TEST_FIXTURES=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR] lifecycle_matrix.sh
# Exits 0 with "SKIPPED: <reason>" when the host cannot run the FUSE tier at all.
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

require_fuse_host
require_store_tools
work=$(work_dir lifecycle)
ensure_vcz "$work/store"
STATE_ROOT="$work/state"
mkdir -p "$STATE_ROOT"
BASELINE=$(fuse_connections)
TEMPLATE="$work/manifest.template.csv"
printf 'id,bed,bim,fam\ncohort,{bed},{bim},{fam}\n' >"$TEMPLATE"
EXPECTED_READ="cohort bed magic: 6c1b01, variants: 2200, samples: 200"

pass=0
fail=0

record() { # $1 = label, $2 = 0 for pass, $3 = detail
    if [ "$2" -eq 0 ]; then
        printf '  %-58s PASS  %s\n' "$1" "$3"
        pass=$((pass + 1))
    else
        printf '  %-58s FAIL  %s\n' "$1" "$3"
        fail=$((fail + 1))
    fi
}

# A fresh launch directory for one case, with its plugin config; NF holds the command line.
setup_case() { # $1 = case name, then extra pipeline options
    CASE="$work/$1"
    shift
    rm -rf "$CASE"
    mkdir -p "$CASE"
    MANIFEST="$CASE/cohort.biofuse.csv"
    write_plugin_config "$CASE/biofuse.config" "$STATE_ROOT" "$MANIFEST" "$TEMPLATE" declared ""
    NF=(nextflow -log "$CASE/nextflow.log" run "$REPO_ROOT/validation" -c "$CASE/biofuse.config"
        --manifest "$MANIFEST" -work-dir "$CASE/work" -ansi-log false "$@")
}

run_fg() { # runs NF in the case directory; its status is returned
    (cd "$CASE" && exec "${NF[@]}") >"$CASE/stdout" 2>&1
}

# Start NF in the background with job control, so it leads its own process group as a terminal job
# does: its tasks join that group, and SIGINT/SIGQUIT keep their default dispositions. The nextflow
# launcher execs the JVM, so the pid left in NFPID is the JVM's.
run_bg() {
    set -m
    (cd "$CASE" && exec "${NF[@]}") >"$CASE/stdout" 2>&1 &
    NFPID=$!
    set +m
}

# Kill whatever the case's process group still holds: the tasks a SIGKILLed JVM leaves behind.
kill_group() { # $1 = process group id
    kill -KILL -- "-$1" 2>/dev/null
    sleep 1
}

view_file() { # $1 = file name inside the (single) view directory; prints its path
    local directory
    for directory in "$STATE_ROOT"/nf-biofuse/view-*/; do
        [ -f "$directory$1" ] && echo "$directory$1" && return 0
    done
    return 1
}

# The case assertion: released state root and no manifest left.
assert_clean() { # $1 = label, $2 = extra detail
    local released=0
    assert_released "$BASELINE" "$STATE_ROOT" "$1" >/dev/null 2>"$CASE/leak.txt" || released=1
    if [ -e "$MANIFEST" ]; then
        echo "manifest left at $MANIFEST" >>"$CASE/leak.txt"
        released=1
    fi
    record "$1" "$released" "${2:-}$([ "$released" -eq 0 ] || echo "; $(tr '\n' ' ' <"$CASE/leak.txt")")"
}

echo "=============================================================="
echo "LIFECYCLE MATRIX  (state root: $STATE_ROOT)"
echo "=============================================================="

echo "[1] normal completion"
setup_case normal
run_fg
rc=$?
read_ok=$(grep -c "$EXPECTED_READ" "$CASE/stdout")
record "run completes and the task reads the mounted trio" $((rc != 0 || read_ok != 1)) "nextflow rc=$rc, trio read=$read_ok"
assert_clean "normal completion leaves nothing behind"

echo "[2] pipeline failure (the task exits 42)"
setup_case failure --fail true
run_fg
rc=$?
record "a failing task fails the run" $((rc == 0)) "nextflow rc=$rc"
assert_clean "pipeline failure leaves nothing behind"

echo "[3] Ctrl-C at a real terminal while a task holds the mount"
setup_case ctrl_c --sleep 300 --marker "$work/ctrl_c/marker"
python3 "$TESTS_DIR/pty_signal_test.py" --marker "$CASE/marker" --cwd "$CASE" -- "${NF[@]}" >"$CASE/pty.log" 2>&1
rc=$?
record "Ctrl-C ends the run" "$rc" "$(grep -o 'exit status: -\?[0-9]*' "$CASE/pty.log")"
assert_clean "Ctrl-C (PTY) leaves nothing behind"

for signal in TERM HUP; do
    echo "[4] SIG$signal to Nextflow while a task holds the mount"
    setup_case "sig$signal" --sleep 300 --marker "$work/sig$signal/marker"
    run_bg
    pgid=$NFPID
    if wait_for_file "$CASE/marker" 180 "$NFPID"; then
        kill "-$signal" "$NFPID"
        wait "$NFPID"
        rc=$?
        record "SIG$signal ends the run" $((rc == 0)) "nextflow rc=$rc"
    else
        record "SIG$signal ends the run" 1 "the task never started; see $CASE/stdout"
        kill -TERM "$NFPID" 2>/dev/null
        wait "$NFPID"
    fi
    kill_group "$pgid"
    assert_clean "SIG$signal leaves nothing behind"
done

echo "[5] SIGKILL to Nextflow (no handler runs; the supervisor watches the JVM)"
setup_case sigkill --sleep 300 --marker "$work/sigkill/marker"
run_bg
pgid=$NFPID
wait_for_file "$CASE/marker" 180 "$NFPID" || record "SIGKILL case task started" 1 "see $CASE/stdout"
kill -KILL "$NFPID"
wait "$NFPID" 2>/dev/null
sleep 3
assert_clean "SIGKILL of the JVM: supervisor tears down" "while its orphaned task still runs"
kill_group "$pgid"

echo "[6] SIGKILL to Nextflow while a task streams the .bed through the mount"
setup_case sigkill_read --stream true --marker "$work/sigkill_read/marker"
run_bg
pgid=$NFPID
wait_for_file "$CASE/marker" 180 "$NFPID" || record "streaming case task started" 1 "see $CASE/stdout"
sleep 1
kill -KILL "$NFPID"
wait "$NFPID" 2>/dev/null
sleep 5
assert_clean "SIGKILL mid-read: no mount, no wedge"
kill_group "$pgid"

echo "[7] SIGKILL to the supervisor (the watchdog reaps while Nextflow still runs)"
setup_case supervisor_kill --sleep 300 --marker "$work/supervisor_kill/marker"
run_bg
pgid=$NFPID
wait_for_file "$CASE/marker" 180 "$NFPID" || record "supervisor-kill case task started" 1 "see $CASE/stdout"
owner=$(view_file owner.pid | xargs cat 2>/dev/null | cut -d' ' -f1)
kill -KILL "$owner" 2>/dev/null
sleep 5
record "the watchdog removed the mount while Nextflow runs" "$(mounts_under "$STATE_ROOT")" "mounts=$(mounts_under "$STATE_ROOT"), supervisor pid ${owner:-none}"
kill -TERM "$NFPID"
wait "$NFPID" 2>/dev/null
kill_group "$pgid"
assert_clean "SIGKILL of the supervisor leaves nothing behind"

echo "[8] stale state reclaimed by the next run (JVM, supervisor and watchdog all killed)"
setup_case stale --sleep 300 --marker "$work/stale/marker"
run_bg
pgid=$NFPID
wait_for_file "$CASE/marker" 180 "$NFPID" || record "stale case task started" 1 "see $CASE/stdout"
owner=$(view_file owner.pid | xargs cat 2>/dev/null | cut -d' ' -f1)
watchdog=$(pgrep -f "nf_biofuse_supervisor.mount --supervisor-pid $owner ")
# Everything that could tear down dies at once; the daemon is spared, so a live mount is left behind.
kill -KILL "$NFPID" "$owner" $watchdog 2>/dev/null
wait "$NFPID" 2>/dev/null
kill_group "$pgid"
left_mounts=$(mounts_under "$STATE_ROOT")
left_manifest=$([ -e "$MANIFEST" ] && echo 1 || echo 0)
stale_manifest=$MANIFEST
setup_case stale_reclaim
run_fg
rc=$?
reclaimed=$(grep -c 'reclaiming stale state' "$CASE/.nf-biofuse.log")
record "the next run reclaims the stale view and completes" $((rc != 0 || reclaimed != 1 || left_mounts != 1)) \
    "left behind: mounts=$left_mounts manifest=$left_manifest; next run rc=$rc, reclaim logged=$reclaimed"
record "the reclaim removed the dead run's manifest" $(($([ -e "$stale_manifest" ] && echo 1 || echo 0))) "$stale_manifest"
assert_clean "stale reclamation leaves nothing behind"

echo "[9] a store the supervisor refuses fails the run before anything is mounted"
setup_case refused
sed -i "s|^    vcz = .*|    vcz = '$CASE'|" "$CASE/biofuse.config"
run_fg
rc=$?
reason=$(grep -c 'not a Zarr v2 or v3 group root' "$CASE/stdout")
record "the run fails with the supervisor's reason" $((rc == 0 || reason == 0)) "nextflow rc=$rc, reason shown=$reason"
assert_clean "a refused start leaves nothing behind"

echo "[10] a second run for a view another live run serves is refused"
setup_case first --sleep 300 --marker "$work/first/marker"
run_bg
first_pid=$NFPID
first_case=$CASE
wait_for_file "$CASE/marker" 180 "$NFPID" || record "first run's task started" 1 "see $CASE/stdout"
setup_case second
run_fg
rc=$?
owned=$(grep -c 'already owns the view' "$CASE/stdout")
record "the second run fails and names the owner" $((rc == 0 || owned == 0)) "nextflow rc=$rc, owner named=$owned"
record "the first run's mount is untouched" $(($(mounts_under "$STATE_ROOT") != 1)) "mounts=$(mounts_under "$STATE_ROOT")"
kill -TERM "$first_pid"
wait "$first_pid" 2>/dev/null
kill_group "$first_pid"
CASE=$first_case
MANIFEST="$CASE/cohort.biofuse.csv"
assert_clean "both runs leave nothing behind"

echo "[11] -resume across two mounts of the same view caches the task"
setup_case resume -with-trace trace1.txt
run_fg
first_rc=$?
NF=("${NF[@]/trace1.txt/trace2.txt}" -resume)
run_fg
rc=$?
cached=$(awk -F'\t' 'NR > 1 && $5 == "CACHED"' "$CASE/trace2.txt" | wc -l)
record "the second run caches READ_TRIO" $((first_rc != 0 || rc != 0 || cached != 1)) "rc=$first_rc/$rc, cached tasks=$cached"
assert_clean "resumed runs leave nothing behind"

echo "=============================================================="
echo "PASS=$pass FAIL=$fail"
echo "=============================================================="
stray=$(pgrep -fa 'nf_biofuse_supervisor\.(daemon|mount)|nf-biofuse-supervisor serve' | grep -F "$STATE_ROOT")
[ -z "$stray" ] && echo "no stray supervisors, daemons or watchdogs" || echo "STRAY: $stray"
exit "$fail"
