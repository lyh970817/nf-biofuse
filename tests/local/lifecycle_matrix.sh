#!/usr/bin/env bash
# Written by the nf-core/gwas team and released under the MIT license.
#
# Lifecycle robustness matrix for nf-core-gwas-biofuse. Each case asserts, after the launcher is gone:
#   - no biofuse mount left in /proc/mounts under the test state root
#   - no mount daemon and no watchdog left alive for that state root
#   - no view state directory left behind (the empty view-<id>.lock files are kept by design)
#   - no wedged FUSE connection (outstanding requests with no live daemon) that the run created
#
# Usage: VCZ=/path/to/store.vcz [STATE_ROOT=DIR] lifecycle_matrix.sh
# Exits 0 with "SKIPPED: <reason>" when the host cannot run the FUSE tier at all.
set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

skip() {
    echo "SKIPPED: $*"
    exit 0
}
[ -e /dev/fuse ] || skip "/dev/fuse is not present"
command -v nf-core-gwas-biofuse >/dev/null 2>&1 || skip "nf-core-gwas-biofuse is not on PATH"
command -v biofuse >/dev/null 2>&1 || skip "biofuse is not on PATH"
command -v apptainer >/dev/null 2>&1 || skip "apptainer is not on PATH"
command -v python3 >/dev/null 2>&1 || skip "python3 is not on PATH"

if [ -z "${VCZ:-}" ]; then
    echo "usage: VCZ=/path/to/store.vcz $0" >&2
    exit 2
fi
LAUNCHER=$(command -v nf-core-gwas-biofuse)
STATE_ROOT=${STATE_ROOT:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/nf-core-gwas-biofuse-lifecycle}
mkdir -p "$STATE_ROOT"
VIEWS="$STATE_ROOT/nf-core-gwas-biofuse"
COHORT=(--cohort-id lifecycle --genome-build GRCh37 --ancestry EUR)
# FUSE connections that existed before the matrix belong to other mounts on the machine.
BASELINE_CONNECTIONS=" $(ls /sys/fs/fuse/connections 2>/dev/null | tr '\n' ' ') "

pass=0
fail=0

scoped_processes() { # $1 = module; prints how many of its processes name this state root
    pgrep -f "$1" 2>/dev/null |
        xargs -r -I{} sh -c 'tr "\0" " " < /proc/{}/cmdline 2>/dev/null; echo' |
        grep -c "$STATE_ROOT" || true
}

# SAFETY: every check below is a pure /proc or /sys read, or an `ls` of the view container only.
# Nothing stats a mountpoint: `mountpoint`, `stat`, `ls` and `find` on a wedged mount all block in
# uninterruptible `D` state, which is precisely the condition these assertions exist to detect.
assert_clean() {
    local label=$1
    sleep 2 # allow the teardown or the watchdog to finish
    local m p d s w c n
    m=$(grep '^biofuse ' /proc/mounts | grep -c "$STATE_ROOT" || true)
    p=$(scoped_processes nf_core_gwas_biofuse.daemon)
    d=$(scoped_processes nf_core_gwas_biofuse.mount)
    s=$(ls -1 "$VIEWS" 2>/dev/null | grep -c '^view-[0-9a-f]*$' || true)
    w=0
    for c in /sys/fs/fuse/connections/*/; do
        [ -d "$c" ] || continue
        case "$BASELINE_CONNECTIONS" in *" $(basename "$c") "*) continue ;; esac
        n=$(cat "$c/waiting" 2>/dev/null || echo 0)
        case "$n" in '' | *[!0-9]*) n=0 ;; esac
        [ "$n" -gt 0 ] && [ "$p" -eq 0 ] && w=$((w + n))
    done
    if [ "$m" -eq 0 ] && [ "$s" -eq 0 ] && [ "$p" -eq 0 ] && [ "$d" -eq 0 ] && [ "$w" -eq 0 ]; then
        printf '  %-46s CLEAN   (mounts=%s state_dirs=%s daemons=%s watchdogs=%s wedged=%s)\n' "$label" "$m" "$s" "$p" "$d" "$w"
        pass=$((pass + 1))
    else
        printf '  %-46s LEAK    (mounts=%s state_dirs=%s daemons=%s watchdogs=%s wedged=%s)\n' "$label" "$m" "$s" "$p" "$d" "$w"
        fail=$((fail + 1))
        grep '^biofuse ' /proc/mounts | sed 's/^/      /'
        ls -1 "$VIEWS" 2>/dev/null | sed 's/^/      /'
        sed 's/^/      log: /' "$STATE_ROOT/last.log"
    fi
}

launcher_argv() { # $@ = command for the launcher to run
    printf '%s\0' "$LAUNCHER" run --vcz "$VCZ" --state-root "$STATE_ROOT" --record-dir "$STATE_ROOT" "${COHORT[@]}" \
        --readahead-buffer-size 16M -v -- "$@"
}

launch() { # $@ = command; runs in the foreground, output to last.log
    local argv
    mapfile -d '' argv < <(launcher_argv "$@")
    "${argv[@]}" >"$STATE_ROOT/last.log" 2>&1
}

# The background launchers are started in this shell, not a command substitution, so that `wait` can
# reap them and report their exit status; the pid is left in LPID.
launch_bg() { # $@ = command
    local argv
    mapfile -d '' argv < <(launcher_argv "$@")
    # Job control gives the launcher its own process group with default signal dispositions, which is
    # what a real terminal job has. Without it, bash sets SIGINT and SIGQUIT to SIG_IGN in background
    # children of a non-interactive shell (see launch_bg_ignoring_sigint).
    set -m
    "${argv[@]}" >"$STATE_ROOT/last.log" 2>&1 &
    LPID=$!
    set +m
}

launch_bg_ignoring_sigint() { # $@ = command
    local argv
    mapfile -d '' argv < <(launcher_argv "$@")
    "${argv[@]}" >"$STATE_ROOT/last.log" 2>&1 &
    LPID=$!
}

wait_ready() { # $1 = launcher pid
    for _ in $(seq 1 1200); do
        grep -q 'mount ready' "$STATE_ROOT/last.log" 2>/dev/null && return 0
        kill -0 "$1" 2>/dev/null || return 1
        sleep 0.1
    done
    return 1
}

echo "=============================================================="
echo "LIFECYCLE MATRIX  (state root: $STATE_ROOT)"
echo "=============================================================="

echo "[1] normal completion (command exits 0)"
launch bash -c 'timeout 30 head -c 3 "$BIOFUSE_MOUNT/$BIOFUSE_BASENAME.bed" >/dev/null && echo consumer-ok'
rc=$?
echo "    launcher rc=$rc (expected 0)  consumer-ok=$(grep -cx consumer-ok "$STATE_ROOT/last.log")"
[ "$rc" -eq 0 ] || fail=$((fail + 1))
assert_clean "normal completion"

echo "[2] command failure (command exits 42)"
launch bash -c 'exit 42'
rc=$?
echo "    launcher rc=$rc (expected 42; status must propagate)"
[ "$rc" -eq 42 ] || fail=$((fail + 1))
assert_clean "command failure"

echo "[3] SIGINT via a real controlling terminal (Ctrl-C)"
python3 "$here/pty_signal_test.py" "$LAUNCHER" "$VCZ" "$STATE_ROOT" SIGINT ctrl-c >"$STATE_ROOT/last.log" 2>&1
rc=$?
echo "    pty harness rc=$rc  ($(grep -c 'received SIGINT' "$STATE_ROOT/last.log") handler fired; $(grep -o 'launcher exit status: -\?[0-9]*' "$STATE_ROOT/last.log"))"
[ "$rc" -eq 0 ] || fail=$((fail + 1))
assert_clean "SIGINT (Ctrl-C, PTY)"

echo "[3b] SIGINT to a launcher backgrounded without job control (ignored), then SIGTERM"
launch_bg_ignoring_sigint sleep 300
pid=$LPID
wait_ready "$pid"
kill -INT "$pid" 2>/dev/null
sleep 2
if kill -0 "$pid" 2>/dev/null; then
    echo "    launcher still running: SIGINT was SIG_IGN on entry and stays ignored"
else
    echo "    launcher exited on an ignored SIGINT"
    fail=$((fail + 1))
fi
kill -TERM "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
assert_clean "SIGINT ignored (backgrounded) + SIGTERM"

echo "[4] SIGTERM to launcher while the command is running"
launch_bg sleep 300
pid=$LPID
wait_ready "$pid"
kill -TERM "$pid"
wait "$pid"
echo "    launcher rc=$? (expected 143)"
assert_clean "SIGTERM"

echo "[5] SIGHUP to launcher (terminal closed)"
launch_bg sleep 300
pid=$LPID
wait_ready "$pid"
kill -HUP "$pid"
wait "$pid"
echo "    launcher rc=$? (expected 129)"
assert_clean "SIGHUP"

echo "[6] SIGKILL to launcher (no handler can fire; the watchdog must reap)"
launch_bg sleep 300
pid=$LPID
wait_ready "$pid"
kill -KILL "$pid"
wait "$pid"
echo "    launcher rc=$? (expected 137)"
sleep 3
assert_clean "SIGKILL (watchdog reap)"

echo "[7] SIGKILL launcher while a consumer is streaming the .bed"
launch_bg bash -c 'timeout 60 cat "$BIOFUSE_MOUNT/$BIOFUSE_BASENAME.bed" | (sleep 30; cat >/dev/null)'
pid=$LPID
wait_ready "$pid"
sleep 1
kill -KILL "$pid"
wait "$pid" 2>/dev/null
sleep 4
assert_clean "SIGKILL mid-read"

echo "[8] stale state reclaimed by the next launcher run (launcher and watchdog both killed)"
launch_bg sleep 300
pid=$LPID
wait_ready "$pid"
# A kill that takes the launcher and its watchdog but spares the daemon leaves a live mount, a live
# daemon and a state directory behind; the next launcher for the SAME view must reclaim all three.
watchdog=$(pgrep -f "nf_core_gwas_biofuse.mount --launcher-pid $pid ")
kill -KILL "$pid" $watchdog 2>/dev/null
wait "$pid" 2>/dev/null
echo "    left behind: mounts=$(grep '^biofuse ' /proc/mounts | grep -c "$STATE_ROOT") daemons=$(scoped_processes nf_core_gwas_biofuse.daemon)"
launch bash -c 'timeout 30 head -c 3 "$BIOFUSE_MOUNT/$BIOFUSE_BASENAME.bed" >/dev/null && echo reclaimed-ok'
rc=$?
echo "    second launcher rc=$rc  reclaimed-ok=$(grep -cx reclaimed-ok "$STATE_ROOT/last.log")  reclaim-logged=$(grep -c 'reclaiming stale state' "$STATE_ROOT/last.log")"
[ "$rc" -eq 0 ] && grep -q 'reclaiming stale state' "$STATE_ROOT/last.log" || fail=$((fail + 1))
assert_clean "stale reclamation"

echo "=============================================================="
echo "PASS=$pass FAIL=$fail"
echo "=============================================================="
pgrep -fa 'nf_core_gwas_biofuse\.(daemon|mount)' | grep "$STATE_ROOT" | sed 's/^/STRAY: /' || echo "no stray daemons or watchdogs"
exit "$fail"
