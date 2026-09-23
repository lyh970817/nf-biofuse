# SPDX-License-Identifier: Apache-2.0
"""Mount lifecycle: the state layout, daemon start, readiness, teardown, the SIGKILL watchdog and stale
reclaim.

WHY THE TEARDOWN IS THE WAY IT IS
---------------------------------
An orphaned FUSE mount -- kernel mount still present, daemon gone -- is not a tidiness problem. Any
process that touches such a mountpoint blocks in uninterruptible sleep (``D`` state) and cannot be
killed, not even with SIGKILL. That takes out the machine, not just the run. Four properties defend
against it:

1. ``auto_unmount`` (set by :mod:`nf_biofuse_supervisor.daemon`) makes the KERNEL drop the mount when
   the daemon dies. It is the only mechanism that survives a SIGKILL, a cgroup kill or a session
   teardown, where no userspace handler runs.
2. ``fusermount3 -u`` HANGS on a wedged mount rather than returning non-zero, so ``-u || -uz`` never
   reaches the fallback. Every unmount attempt is time-capped, and ``-uz`` follows whenever the mount is
   still present, never predicated on the status of ``-u``.
3. Mount-state checks read ``/proc/self/mountinfo`` ONLY. ``mountpoint``, ``stat``, ``ls``, ``os.path``
   predicates and globs all stat the mountpoint and would block in ``D`` on exactly the condition this
   code exists to handle. Nothing here stats a mountpoint, and the few reads that must touch one run in
   a child process with a deadline that is never waited past.
4. Aborting the kernel connection (``/sys/fs/fuse/connections/<minor>/abort``) is the only thing that
   releases readers already stuck on a wedged mount. It is available in teardown, in the watchdog and
   in ``reap-stale``.

OWNERSHIP
---------
A view is owned by whoever holds an exclusive ``flock`` on ``view-<id>.lock``, beside the view
directory. The supervisor takes it before it touches the view and holds it until it exits, so the kernel
releases it the moment the supervisor dies, however it dies, and a pid that has since been reused can
never look like a live owner. The watchdog waits on the same lock, which is how it learns that the
supervisor is gone, and it reaps only while holding it, so it can never tear down a view a newer
supervisor has already reclaimed. The lock file outlives the view on purpose: deleting it would let two
processes lock two different files of the same name. ``owner.pid`` names the supervisor that holds the
lock.

PUBLISHED FILES
---------------
The manifest and the readiness report live outside the view directory, where the pipeline and the plugin
read them. Each is registered in ``published.json`` with the digest of its content before it is written,
and every teardown -- the supervisor's own, the watchdog's and ``reap-stale`` -- removes it first, so
nothing new can resolve the trio once teardown starts. A file whose content has changed since is left
alone: it is no longer the supervisor's.

State layout, under the state root::

    nf-biofuse/
        view-<16 hex>.lock   the ownership lock (empty, kept)
        view-<16 hex>/
            mnt/                 the mountpoint
            owner.pid            "<supervisor pid> <supervisor start time>"
            daemon.pid           "<daemon pid> <daemon start time>"
            published.json       the files outside the view to remove at teardown, with their digests
            logs/daemon.log      the daemon's stdout and stderr
            logs/watchdog.log    the watchdog's report, when it had to act and the state is kept
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

from . import manifest

STATE_DIRNAME = "nf-biofuse"
VIEW_PREFIX = "view-"
LOCK_SUFFIX = ".lock"
UNMOUNT_TIMEOUT = 5.0
DAEMON_TERM_GRACE = 5.0
READ_PROBE_TIMEOUT = 5.0
READY_POLL_INTERVAL = 0.05
# Long enough for a watchdog to finish reaping the previous supervisor's view: three capped unmounts, a
# daemon grace period and three more capped unmounts.
CLAIM_WAIT = 60.0
CLAIM_POLL_INTERVAL = 0.2


class MountError(RuntimeError):
    """A lifecycle step failed; ``exit_code`` is the supervisor's exit status for it."""

    exit_code = 1


class OwnedViewError(MountError):
    exit_code = 2


class DaemonStartupError(MountError):
    exit_code = 3


class ReadinessTimeout(MountError):
    exit_code = 4


def default_state_root() -> str:
    for variable in ("XDG_RUNTIME_DIR", "TMPDIR"):
        value = os.environ.get(variable)
        if value:
            return value
    return "/tmp"


@dataclass(frozen=True)
class ViewPaths:
    view_dir: str

    @classmethod
    def under(cls, state_root: str, short_id: str) -> ViewPaths:
        return cls(os.path.join(state_root, STATE_DIRNAME, f"{VIEW_PREFIX}{short_id}"))

    @property
    def lock_file(self) -> str:
        return f"{self.view_dir}{LOCK_SUFFIX}"

    @property
    def mount_dir(self) -> str:
        return os.path.join(self.view_dir, "mnt")

    @property
    def owner_pid_file(self) -> str:
        return os.path.join(self.view_dir, "owner.pid")

    @property
    def daemon_pid_file(self) -> str:
        return os.path.join(self.view_dir, "daemon.pid")

    @property
    def log_dir(self) -> str:
        return os.path.join(self.view_dir, "logs")

    @property
    def daemon_log(self) -> str:
        return os.path.join(self.log_dir, "daemon.log")

    @property
    def watchdog_log(self) -> str:
        return os.path.join(self.log_dir, "watchdog.log")

    @property
    def published(self) -> str:
        return os.path.join(self.view_dir, "published.json")


# ----------------------------------------------------------------------------------------------------
# Non-blocking mount-state primitives. Every one reads /proc or /sys only.
# ----------------------------------------------------------------------------------------------------


def _unescape_mountinfo(field: str) -> str:
    # mountinfo escapes space, tab, newline and backslash as three octal digits (\040 and so on).
    out = []
    index = 0
    while index < len(field):
        if field[index] == "\\" and index + 4 <= len(field) and field[index + 1 : index + 4].isdigit():
            out.append(chr(int(field[index + 1 : index + 4], 8)))
            index += 4
        else:
            out.append(field[index])
            index += 1
    return "".join(out)


def _mountinfo_entries():
    """Yield ``(mountpoint, "major:minor")`` for every mount visible to this process."""
    with open("/proc/self/mountinfo", encoding="utf-8") as handle:
        for line in handle:
            fields = line.split()
            yield _unescape_mountinfo(fields[4]), fields[2]


def mount_present(mountpoint: str) -> bool:
    return any(entry == mountpoint for entry, _ in _mountinfo_entries())


def fuse_minor(mountpoint: str) -> str | None:
    """The kernel FUSE connection minor behind ``mountpoint``: for FUSE the device is ``0:<minor>``
    and the minor names the directory under ``/sys/fs/fuse/connections``."""
    for entry, device in _mountinfo_entries():
        if entry == mountpoint:
            return device.split(":", 1)[1]
    return None


def abort_fuse_connection(mountpoint: str, log) -> bool:
    """Abort the kernel connection behind ``mountpoint``; this makes every outstanding and future
    request fail with ENOTCONN instead of waiting for a daemon that will never answer."""
    minor = fuse_minor(mountpoint)
    if minor is None:
        return False
    log(f"aborting fuse connection {minor} for {mountpoint}")
    try:
        with open(f"/sys/fs/fuse/connections/{minor}/abort", "w", encoding="ascii") as handle:
            handle.write("1")
    except OSError as error:
        log(f"could not abort fuse connection {minor}: {error.strerror}")
        return False
    return True


def fusermount_path() -> str:
    path = shutil.which("fusermount3") or shutil.which("fusermount")
    if path is None:
        raise MountError("fusermount3/fusermount not on PATH")
    return path


def run_capped(argv: list[str], timeout: float) -> int | None:
    """Run ``argv`` with stdio discarded and return its status, or ``None`` if it did not finish in
    ``timeout`` seconds. A child that outlives the cap is killed and never waited for: on a wedged mount
    it is in ``D`` state and a wait would block forever."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return None


def read_probe(path: str, size: int, timeout: float = READ_PROBE_TIMEOUT) -> bool:
    """True if a child could read exactly ``size`` bytes from ``path`` within ``timeout``. The read
    runs in a child for the same reason as :func:`run_capped`."""
    process = subprocess.Popen(
        ["head", "-c", str(size), path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        data, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.stdout.close()
        return False
    return process.returncode == 0 and len(data) == size


def unmount(mountpoint: str, log) -> bool:
    """Tear ``mountpoint`` down, escalating only as far as necessary. True when it is gone."""
    if not mount_present(mountpoint):
        return True
    fusermount = fusermount_path()
    # 1. Graceful. ``-u`` blocks on a wedged mount instead of failing, so it is capped, not tested.
    run_capped([fusermount, "-u", mountpoint], UNMOUNT_TIMEOUT)
    if not mount_present(mountpoint):
        return True
    # 2. Lazy detach -- never predicated on step 1's status.
    log("mount still present after -u; lazy detach")
    run_capped([fusermount, "-uz", mountpoint], UNMOUNT_TIMEOUT)
    if not mount_present(mountpoint):
        return True
    # 3. Still there: the connection is wedged. Abort it, then detach again.
    log(f"mount {mountpoint} is wedged; aborting its kernel connection")
    abort_fuse_connection(mountpoint, log)
    run_capped([fusermount, "-uz", mountpoint], UNMOUNT_TIMEOUT)
    if mount_present(mountpoint):
        log(f"WARNING: {mountpoint} is STILL present after abort + lazy detach")
        return False
    return True


# ----------------------------------------------------------------------------------------------------
# Processes
# ----------------------------------------------------------------------------------------------------


def _proc_stat(pid: int) -> tuple[str, str] | None:
    """``(state, start time)`` from ``/proc/<pid>/stat``; the start time is in clock ticks since boot
    and, with the pid, identifies one process incarnation."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    # The command name is parenthesised and may contain spaces; everything after it is fixed-position.
    fields = content.rpartition(")")[2].split()
    return fields[0], fields[19]


def process_start_time(pid: int) -> str | None:
    stat = _proc_stat(pid)
    return stat[1] if stat else None


def process_alive(pid: int, start_time: str | None = None) -> bool:
    """True while ``pid`` runs. A zombie has exited and counts as gone, which matters because the
    supervisor's own children stay zombies until they are reaped. With ``start_time``, a process that
    reuses the pid counts as gone too."""
    stat = _proc_stat(pid)
    if stat is None or stat[0] in ("Z", "X"):
        return False
    return start_time is None or stat[1] == start_time


def write_pid_file(path: str, pid: int) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(f"{pid} {process_start_time(pid) or ''}\n")


def read_pid_file(path: str) -> tuple[int, str | None] | None:
    try:
        with open(path, encoding="ascii") as handle:
            fields = handle.read().split()
        return int(fields[0]), (fields[1] if len(fields) > 1 else None)
    except (OSError, ValueError, IndexError):
        return None


def stop_daemon(daemon: tuple[int, str | None] | None, log, grace: float = DAEMON_TERM_GRACE) -> None:
    """TERM, then KILL. biofuse 0.1.0 does not exit when its mount is detached; it must be signalled or
    it lingers forever holding its resident set. ``daemon`` is ``(pid, start time)``, so a pid reused
    since the daemon died is never signalled."""
    if daemon is None or not process_alive(*daemon):
        return
    pid = daemon[0]
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not process_alive(*daemon):
            return
        time.sleep(0.1)
    log(f"daemon {pid} ignored SIGTERM; SIGKILL")
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


# ----------------------------------------------------------------------------------------------------
# View ownership and state
# ----------------------------------------------------------------------------------------------------


def try_lock(paths: ViewPaths, blocking: bool = False) -> int | None:
    """An open descriptor holding the view's exclusive lock, or ``None`` if someone else holds it.
    The descriptor is close-on-exec, so no child inherits the lock and outlives the holder with it."""
    descriptor = os.open(paths.lock_file, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def release_lock(descriptor: int | None) -> None:
    if descriptor is not None:
        os.close(descriptor)


def publish(paths: ViewPaths, path: str, text: str) -> None:
    """Register ``path``, about to be written with ``text``, for removal at teardown."""
    entries = _published(paths)
    entries = [entry for entry in entries if entry["path"] != path]
    entries.append({"path": path, "sha256": manifest.digest(text)})
    manifest.write(paths.published, json.dumps(entries, indent=2) + "\n")


def _published(paths: ViewPaths) -> list[dict]:
    try:
        with open(paths.published, encoding="utf-8") as handle:
            return list(json.load(handle))
    except (OSError, ValueError):
        return []


def remove_published(paths: ViewPaths, log) -> None:
    """Remove every registered file that still holds what the supervisor wrote."""
    for entry in _published(paths):
        if not manifest.remove_if_unchanged(entry["path"], entry["sha256"]):
            log(f"leaving {entry['path']} in place: its content changed since it was written")


def remove_state(paths: ViewPaths, log) -> bool:
    """Remove the view directory, but only once its mountpoint is gone: removing a directory tree
    walks it, and walking a mountpoint stats it."""
    if mount_present(paths.mount_dir):
        log(f"WARNING: leaving {paths.view_dir} in place because its mount is still present")
        return False
    shutil.rmtree(paths.view_dir, ignore_errors=True)
    return True


def reap_view(paths: ViewPaths, log, keep_state: bool = False) -> bool:
    """Reap one view whose supervisor is gone; the caller holds its lock. True when no mount remains
    and, unless ``keep_state``, no state either."""
    remove_published(paths, log)
    unmount(paths.mount_dir, log)
    stop_daemon(read_pid_file(paths.daemon_pid_file), log)
    # The daemon dying releases an auto_unmount mount; confirm and force if not.
    if mount_present(paths.mount_dir):
        unmount(paths.mount_dir, log)
    if keep_state:
        return not mount_present(paths.mount_dir)
    return remove_state(paths, log)


def view_dirs(state_root: str) -> list[str]:
    container = os.path.join(state_root, STATE_DIRNAME)
    try:
        names = sorted(os.listdir(container))
    except FileNotFoundError:
        return []
    # isdir stats the view directory, never its mnt/ child.
    return [
        os.path.join(container, name)
        for name in names
        if name.startswith(VIEW_PREFIX) and os.path.isdir(os.path.join(container, name))
    ]


def reap_stale(state_root: str, log) -> int:
    """Reap every view under ``state_root`` whose supervisor is gone. Returns the number reaped."""
    log(f"reaping stale biofuse views under {os.path.join(state_root, STATE_DIRNAME)}")
    reaped = 0
    directories = view_dirs(state_root)
    for view_dir in directories:
        paths = ViewPaths(view_dir)
        lock = try_lock(paths)
        if lock is None:
            owner = read_pid_file(paths.owner_pid_file)
            log(f"  {view_dir}: in use (supervisor {owner[0] if owner else 'unknown'}), leaving it alone")
            continue
        try:
            log(f"  {view_dir}: orphaned, reaping")
            reap_view(paths, log)
            reaped += 1
        finally:
            release_lock(lock)
    if not directories:
        log("  nothing to reap")
    return reaped


def claim_view(paths: ViewPaths, log) -> int:
    """Take ownership of the view: refuse if a live supervisor owns it, reap it if it is stale. Returns
    the lock descriptor, which the caller holds until it exits."""
    os.makedirs(os.path.dirname(paths.view_dir), exist_ok=True)
    deadline = time.monotonic() + CLAIM_WAIT
    while (lock := try_lock(paths)) is None:
        owner = read_pid_file(paths.owner_pid_file)
        if owner is not None and process_alive(*owner):
            raise OwnedViewError(f"another supervisor (pid {owner[0]}) already owns the view at {paths.view_dir}")
        # The holder is not a live supervisor, so it is a watchdog reaping the previous one's view.
        if time.monotonic() > deadline:
            raise OwnedViewError(f"the view at {paths.view_dir} stayed locked for {CLAIM_WAIT:g}s")
        time.sleep(CLAIM_POLL_INTERVAL)
    try:
        # Holding the lock, any state already here belongs to a supervisor that is gone.
        if os.path.isdir(paths.view_dir):
            log(f"reclaiming stale state from a previous run: {paths.view_dir}")
            if not reap_view(paths, log):
                raise MountError(f"could not reclaim {paths.view_dir}: its mount is still present")
        os.makedirs(paths.log_dir)
        os.makedirs(paths.mount_dir)
        os.chmod(paths.view_dir, 0o700)
        write_pid_file(paths.owner_pid_file, os.getpid())
    except BaseException:
        release_lock(lock)
        raise
    return lock


def daemon_argv(store: str, basename: str, mount_dir: str, options: list[str]) -> list[str]:
    return [
        sys.executable,
        "-m",
        "nf_biofuse_supervisor.daemon",
        "mount-plink",
        store,
        mount_dir,
        "--basename",
        basename,
        *options,
    ]


def start_daemon(paths: ViewPaths, argv: list[str]) -> subprocess.Popen:
    """Start the daemon in its own session so no terminal hangup or job-control signal reaches it;
    the supervisor, or its watchdog, is what stops it."""
    with open(paths.daemon_log, "ab") as log_handle:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True
        )
    write_pid_file(paths.daemon_pid_file, process.pid)
    return process


def start_watchdog(paths: ViewPaths, keep_state: bool) -> subprocess.Popen:
    """A handler cannot fire for SIGKILL, so a detached child waits on the view lock, which the kernel
    releases when the supervisor dies, and performs the same teardown."""
    argv = [
        sys.executable,
        "-m",
        "nf_biofuse_supervisor.mount",
        "--supervisor-pid",
        str(os.getpid()),
        "--supervisor-start",
        process_start_time(os.getpid()) or "",
        "--view-dir",
        paths.view_dir,
    ]
    if keep_state:
        argv.append("--keep-state")
    with open(paths.watchdog_log, "ab") as log_handle:
        return subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT, start_new_session=True
        )


def wait_ready(paths: ViewPaths, basename: str, daemon: subprocess.Popen, timeout: float, check) -> float:
    """Wait until the mount is present AND a short read of every trio member succeeds; the mount can
    exist while the encoder still errors. ``check`` is called once per poll and raises to abandon the
    wait. Returns the seconds it took."""
    started = time.monotonic()
    deadline = started + timeout
    members = (
        (os.path.join(paths.mount_dir, f"{basename}.bed"), 3),
        (os.path.join(paths.mount_dir, f"{basename}.bim"), 1),
        (os.path.join(paths.mount_dir, f"{basename}.fam"), 1),
    )
    while time.monotonic() < deadline:
        check()
        if daemon.poll() is not None:
            raise DaemonStartupError(f"mount daemon exited with status {daemon.returncode} during startup")
        if mount_present(paths.mount_dir) and all(read_probe(path, size) for path, size in members):
            return time.monotonic() - started
        time.sleep(READY_POLL_INTERVAL)
    raise ReadinessTimeout(f"timed out after {timeout:g}s waiting for the virtual trio")


def teardown(
    paths: ViewPaths, daemon: subprocess.Popen | None, watchdog: subprocess.Popen | None, keep_state: bool, log
) -> bool:
    """The one exit path for every way a serve ends. The watchdog is stopped last, so a supervisor killed
    part-way through still has its teardown finished for it. True when no mount remains."""
    # Remove the manifest and the readiness report first so nothing new can resolve the trio.
    remove_published(paths, log)
    # Unmount before stopping the daemon: a graceful unmount lets an in-flight reader drain.
    unmount(paths.mount_dir, log)
    if daemon is not None:
        stop_daemon((daemon.pid, None), log)
        daemon.poll()
    # With auto_unmount the daemon's death removes the mount; verify, and force if anything is left.
    if mount_present(paths.mount_dir):
        log("mount outlived the daemon; forcing")
        unmount(paths.mount_dir, log)
    released = not mount_present(paths.mount_dir)
    if keep_state:
        log(f"--keep-state set, leaving {paths.view_dir}")
    else:
        remove_state(paths, log)
    if watchdog is not None:
        watchdog.terminate()
        try:
            watchdog.wait(timeout=5)
        except subprocess.TimeoutExpired:
            watchdog.kill()
    return released


# ----------------------------------------------------------------------------------------------------
# The watchdog process
# ----------------------------------------------------------------------------------------------------


def watchdog_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nf_biofuse_supervisor.mount", description="SIGKILL watchdog (internal).")
    parser.add_argument("--supervisor-pid", type=int, required=True)
    parser.add_argument("--supervisor-start", required=True)
    parser.add_argument("--view-dir", required=True)
    parser.add_argument("--keep-state", action="store_true")
    args = parser.parse_args(argv)

    # The watchdog must not die with the supervisor's session or terminal.
    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT):
        signal.signal(signum, signal.SIG_IGN)

    def log(message: str) -> None:
        print(f"[watchdog {time.strftime('%H:%M:%S')}] {message}", flush=True)

    paths = ViewPaths(args.view_dir)
    # Blocks until the supervisor releases the lock, which the kernel does when it dies.
    lock = try_lock(paths, blocking=True)
    try:
        # Only reap a view still owned by the supervisor watched: otherwise it cleaned up itself, or a
        # newer supervisor has reclaimed the view and already finished with it.
        watched = (args.supervisor_pid, args.supervisor_start)
        if not os.path.isdir(paths.view_dir) or read_pid_file(paths.owner_pid_file) != watched:
            return 0
        log(f"supervisor {args.supervisor_pid} is gone with the view still owned; reaping")
        reap_view(paths, log, keep_state=args.keep_state)
        log("done")
    finally:
        release_lock(lock)
    return 0


if __name__ == "__main__":
    sys.exit(watchdog_main())
