# SPDX-License-Identifier: Apache-2.0
"""The supervisor's execution context, its readiness report and its stop protocol, without a mount."""

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from conftest import make_store
from nf_biofuse_supervisor import cli, mount

TEMPLATE = "cohort_id,bed,bim,fam,genotype_view_id\nc,{bed},{bim},{fam},{view_id}\n"


@pytest.fixture(autouse=True)
def fusermount(monkeypatch):
    monkeypatch.setattr(mount, "fusermount_path", lambda: "/usr/bin/fusermount3")
    monkeypatch.delenv("SLURM_JOB_NUM_NODES", raising=False)
    # serve leaves the caller's session; a test process must stay in pytest's.
    monkeypatch.setattr(cli, "detach_session", lambda log: None)


@pytest.fixture
def workspace(tmp_path):
    template = tmp_path / "template.csv"
    template.write_text(TEMPLATE)
    return {
        "store": make_store(tmp_path / "cohort.vcz"),
        "template": str(template),
        "manifest": str(tmp_path / "manifest.csv"),
        "ready": str(tmp_path / "ready.json"),
        "state": str(tmp_path / "state"),
    }


def _serve(capsys, workspace, *extra):
    argv = [
        "serve",
        "--vcz",
        workspace["store"],
        "--manifest-template",
        workspace["template"],
        "--manifest",
        workspace["manifest"],
        "--ready-file",
        workspace["ready"],
        "--state-root",
        workspace["state"],
        *extra,
    ]
    status = cli.main(argv)
    return status, capsys.readouterr().err


def _report(workspace):
    with open(workspace["ready"], encoding="utf-8") as handle:
        return json.load(handle)


def test_multi_node_allocation_is_refused_and_reported(capsys, monkeypatch, workspace):
    monkeypatch.setenv("SLURM_JOB_NUM_NODES", "2")
    status, err = _serve(capsys, workspace)
    assert status == 2
    assert "SLURM_JOB_NUM_NODES=2" in err
    report = _report(workspace)
    assert report["status"] == "failed" and report["exit_code"] == 2
    assert "SLURM_JOB_NUM_NODES=2" in report["error"]


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--state-root", "/tmp/a,b"], "--state-root"),
        (["--basename", "a/b"], "--basename"),
        (["--record-dir", "/nonexistent/records"], "--record-dir"),
        (["--samples-file", "/nonexistent/samples.txt"], "--samples-file"),
        (["--watch-pid", "999999999"], "--watch-pid"),
    ],
)
def test_inputs_the_supervisor_owns_are_validated_before_anything_is_mounted(capsys, workspace, extra, message):
    status, err = _serve(capsys, workspace, *extra)
    assert status == 2
    assert message in err
    assert _report(workspace)["status"] == "failed"
    assert not os.path.exists(os.path.join(workspace["state"], mount.STATE_DIRNAME))


def test_a_store_that_is_not_zarr_is_refused(capsys, workspace, tmp_path):
    workspace["store"] = str(tmp_path)
    status, err = _serve(capsys, workspace)
    assert status == 2
    assert "not a Zarr v2 or v3 group root" in err


@pytest.mark.usefixtures("pinned_versions")
def test_an_unknown_placeholder_is_refused_before_mounting(capsys, workspace):
    with open(workspace["template"], "w", encoding="utf-8") as handle:
        handle.write("{bed},{cohort}\n")
    status, err = _serve(capsys, workspace)
    assert status == 2
    assert "{cohort}" in err
    assert not os.path.exists(os.path.join(workspace["state"], mount.STATE_DIRNAME))


@pytest.mark.usefixtures("pinned_versions")
def test_a_foreign_file_at_the_manifest_path_is_never_overwritten(capsys, workspace):
    with open(workspace["manifest"], "w", encoding="utf-8") as handle:
        handle.write("the user's own samplesheet\n")
    status, err = _serve(capsys, workspace)
    assert status == 2
    assert "refusing to overwrite" in err
    with open(workspace["manifest"], encoding="utf-8") as handle:
        assert handle.read() == "the user's own samplesheet\n"


def test_default_basename_strips_only_the_vcz_suffix():
    assert cli.default_basename("/data/cohort.vcz") == "cohort"
    assert cli.default_basename("/data/cohort.v2.vcz/") == "cohort.v2"
    assert cli.default_basename("/data/cohort") == "cohort"


def test_the_ready_report_is_json_with_a_status(tmp_path):
    report = cli.ReadyReport(str(tmp_path / "ready.json"))
    text = report.render("ready", manifest="/m.csv", readiness_seconds=0.5)
    report.write(text)
    assert json.loads((tmp_path / "ready.json").read_text()) == {
        "status": "ready",
        "manifest": "/m.csv",
        "readiness_seconds": 0.5,
    }
    report.failed(4, "timed out")
    assert json.loads((tmp_path / "ready.json").read_text()) == {
        "status": "failed",
        "exit_code": 4,
        "error": "timed out",
    }
    # Without a path nothing is written and nothing fails.
    cli.ReadyReport(None).failed(1, "x")


def test_watched_process_is_identified_by_pid_and_start_time():
    pid, start_time = cli.watched_process(os.getpid())
    assert pid == os.getpid() and start_time == mount.process_start_time(os.getpid())
    assert cli.watched_process(None) is None
    process = subprocess.Popen(["true"])
    process.wait()
    with pytest.raises(cli.UsageError):
        cli.watched_process(process.pid)


def test_serving_ends_when_the_watched_process_is_gone():
    sleeper = subprocess.Popen(["sleep", "30"])
    watched = cli.watched_process(sleeper.pid)
    guard = cli.SignalGuard()
    threading.Timer(0.5, sleeper.kill).start()
    started = time.monotonic()
    try:
        cli.serve_until_stopped(guard, watched, cli.Log())
    finally:
        sleeper.wait()
    assert time.monotonic() - started < 5


def test_serving_ends_on_a_stop_signal():
    guard = cli.SignalGuard()
    threading.Timer(0.5, guard._handle, (signal.SIGTERM, None)).start()
    with pytest.raises(cli.Signalled) as ended:
        cli.serve_until_stopped(guard, None, cli.Log())
    assert ended.value.signum == signal.SIGTERM


def test_the_stop_protocol_signals_are_handled_even_when_ignored_on_entry():
    # Started by a JVM, the supervisor may inherit SIG_IGN for some of them; SIGTERM must still stop it.
    code = (
        "import signal, sys\n"
        "from nf_biofuse_supervisor import cli\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "guard = cli.SignalGuard(); guard.install()\n"
        "print(signal.getsignal(signal.SIGTERM) == guard._handle, flush=True)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.stdout.strip() == "True", result.stderr


def test_serve_leaves_the_callers_session():
    # The real detach_session, in a child: afterwards the child leads a session of its own.
    code = (
        "import os\n"
        "from nf_biofuse_supervisor import cli\n"
        "cli.detach_session(cli.Log())\n"
        "print(os.getsid(0) == os.getpid(), os.getpgid(0) == os.getpid(), flush=True)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert result.stdout.split() == ["True", "True"], result.stderr
