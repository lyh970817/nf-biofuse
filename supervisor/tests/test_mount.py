# SPDX-License-Identifier: Apache-2.0
"""View ownership, process liveness and stale reclaim, on views that were never mounted. The FUSE tier
(``lifecycle_matrix.sh``) covers the same paths against a real mount."""

import os
import signal
import subprocess
import sys
import time

import pytest

from nf_biofuse_supervisor import mount

HOLDER = """
import os, sys, time
from nf_biofuse_supervisor import mount
paths = mount.ViewPaths(sys.argv[1])
lock = mount.try_lock(paths)
os.makedirs(paths.view_dir, exist_ok=True)
mount.write_pid_file(paths.owner_pid_file, os.getpid())
print("locked", flush=True)
time.sleep(60)
"""


def _messages():
    messages = []
    return messages, messages.append


def _dead_process() -> tuple[int, str]:
    """The (pid, start time) of a process that has exited and been reaped."""
    process = subprocess.Popen(["true"])
    start_time = mount.process_start_time(process.pid)
    process.wait()
    return process.pid, start_time


def _write_pid(path, process):
    pid, start_time = process
    with open(path, "w", encoding="ascii") as handle:
        handle.write(f"{pid} {start_time}\n")


@pytest.fixture
def paths(tmp_path):
    view = mount.ViewPaths.under(str(tmp_path), "0123456789abcdef")
    os.makedirs(os.path.dirname(view.view_dir))
    return view


@pytest.fixture
def holder(paths):
    """Another process that owns the view: it holds the lock and is named in owner.pid."""
    process = subprocess.Popen([sys.executable, "-c", HOLDER, paths.view_dir], stdout=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "locked"
    yield process
    process.kill()
    process.wait()


def test_state_layout(tmp_path):
    view = mount.ViewPaths.under(str(tmp_path), "0123456789abcdef")
    assert view.view_dir == str(tmp_path / "nf-biofuse" / "view-0123456789abcdef")
    assert view.lock_file == view.view_dir + ".lock"
    assert [
        os.path.relpath(path, view.view_dir)
        for path in (view.mount_dir, view.owner_pid_file, view.daemon_pid_file, view.published, view.daemon_log)
    ] == [
        "mnt",
        "owner.pid",
        "daemon.pid",
        "published.json",
        os.path.join("logs", "daemon.log"),
    ]


def test_default_state_root_order(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1")
    monkeypatch.setenv("TMPDIR", "/scratch")
    assert mount.default_state_root() == "/run/user/1"
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    assert mount.default_state_root() == "/scratch"
    monkeypatch.delenv("TMPDIR")
    assert mount.default_state_root() == "/tmp"


def test_mountinfo_fields_are_unescaped():
    assert mount._unescape_mountinfo(r"/run/a\040b\011c\134d") == "/run/a b\tc\\d"
    assert mount._unescape_mountinfo("/plain") == "/plain"
    assert mount._unescape_mountinfo("/trailing\\04") == "/trailing\\04"


def test_mount_state_is_read_from_mountinfo(tmp_path):
    assert mount.mount_present("/proc")
    assert not mount.mount_present(str(tmp_path))
    assert mount.fuse_minor(str(tmp_path)) is None


def test_zombies_and_reused_pids_are_not_alive():
    assert mount.process_alive(os.getpid())
    assert mount.process_alive(os.getpid(), mount.process_start_time(os.getpid()))
    assert not mount.process_alive(os.getpid(), "0")
    process = subprocess.Popen(["true"])
    deadline = time.monotonic() + 10
    while mount._proc_stat(process.pid)[0] != "Z" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not mount.process_alive(process.pid)  # exited, not yet reaped
    process.wait()
    assert not mount.process_alive(process.pid)


def test_stop_daemon_never_signals_a_reused_pid():
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        mount.stop_daemon((sleeper.pid, "0"), print)
        assert sleeper.poll() is None
        mount.stop_daemon((sleeper.pid, mount.process_start_time(sleeper.pid)), print)
        assert sleeper.wait(timeout=5) == -signal.SIGTERM
    finally:
        sleeper.kill()
        sleeper.wait()


def test_the_view_lock_is_exclusive(paths):
    first = mount.try_lock(paths)
    assert first is not None
    assert mount.try_lock(paths) is None
    mount.release_lock(first)
    second = mount.try_lock(paths)
    assert second is not None
    mount.release_lock(second)


def test_the_view_lock_dies_with_its_holder(paths, holder):
    assert mount.try_lock(paths) is None
    holder.send_signal(signal.SIGKILL)
    holder.wait()
    lock = mount.try_lock(paths)
    assert lock is not None
    mount.release_lock(lock)


def test_claim_creates_the_view_and_names_its_owner(paths):
    messages, log = _messages()
    lock = mount.claim_view(paths, log)
    try:
        assert os.path.isdir(paths.mount_dir) and os.path.isdir(paths.log_dir)
        assert oct(os.stat(paths.view_dir).st_mode & 0o777) == oct(0o700)
        assert mount.read_pid_file(paths.owner_pid_file) == (os.getpid(), mount.process_start_time(os.getpid()))
        assert mount.try_lock(paths) is None
        assert messages == []
    finally:
        mount.release_lock(lock)


def test_claim_reclaims_a_view_whose_supervisor_is_gone(paths):
    os.makedirs(paths.log_dir)
    os.makedirs(paths.mount_dir)
    for pid_file in (paths.owner_pid_file, paths.daemon_pid_file):
        _write_pid(pid_file, _dead_process())
    with open(os.path.join(paths.log_dir, "daemon.log"), "w", encoding="utf-8") as handle:
        handle.write("from the previous run\n")
    messages, log = _messages()
    lock = mount.claim_view(paths, log)
    try:
        assert any("reclaiming stale state" in message for message in messages)
        assert os.listdir(paths.log_dir) == []
        assert mount.read_pid_file(paths.owner_pid_file)[0] == os.getpid()
    finally:
        mount.release_lock(lock)


def test_claim_refuses_a_view_a_live_supervisor_owns(paths, holder):
    started = time.monotonic()
    with pytest.raises(mount.OwnedViewError, match=str(holder.pid)):
        mount.claim_view(paths, print)
    assert time.monotonic() - started < mount.CLAIM_WAIT / 2
    assert holder.poll() is None


def test_reap_stale_leaves_live_views_alone(tmp_path, paths, holder):
    stale = mount.ViewPaths.under(str(tmp_path), "fedcba9876543210")
    os.makedirs(stale.mount_dir)
    messages, log = _messages()
    assert mount.reap_stale(str(tmp_path), log) == 1
    assert not os.path.exists(stale.view_dir)
    assert os.path.isdir(paths.view_dir)
    assert any("in use" in message and str(holder.pid) in message for message in messages)


def _watchdog(paths, pid, start_time):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "nf_biofuse_supervisor.mount",
            "--supervisor-pid",
            str(pid),
            "--supervisor-start",
            start_time,
            "--view-dir",
            paths.view_dir,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_watchdog_reaps_only_the_view_of_the_supervisor_it_watched(paths):
    watched = _dead_process()
    os.makedirs(paths.mount_dir)
    _write_pid(paths.owner_pid_file, _dead_process())
    # A newer supervisor has reclaimed the view: the watchdog of the old one leaves it alone.
    assert _watchdog(paths, *watched).returncode == 0
    assert os.path.isdir(paths.mount_dir)
    # Still owned by the supervisor it watched: the watchdog reaps it.
    _write_pid(paths.owner_pid_file, watched)
    result = _watchdog(paths, *watched)
    assert result.returncode == 0, result.stderr
    assert "reaping" in result.stdout
    assert not os.path.exists(paths.view_dir)


def test_reaping_removes_published_files_that_are_still_ours(tmp_path, paths):
    os.makedirs(paths.mount_dir)
    ours = tmp_path / "ours.csv"
    changed = tmp_path / "changed.csv"
    mount.publish(paths, str(ours), "ours\n")
    mount.publish(paths, str(changed), "as written\n")
    ours.write_text("ours\n")
    changed.write_text("edited since\n")
    messages, log = _messages()
    assert mount.reap_view(paths, log)
    assert not ours.exists()
    assert changed.read_text() == "edited since\n"
    assert any("changed.csv" in message for message in messages)
    assert not os.path.exists(paths.view_dir)


def test_the_watchdog_removes_the_published_manifest(tmp_path, paths):
    watched = _dead_process()
    os.makedirs(paths.mount_dir)
    _write_pid(paths.owner_pid_file, watched)
    target = tmp_path / "manifest.csv"
    mount.publish(paths, str(target), "row\n")
    target.write_text("row\n")
    assert _watchdog(paths, *watched).returncode == 0
    assert not target.exists()
