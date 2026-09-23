# Written by the nf-core/gwas team and released under the MIT license.
"""The launcher's own execution context, its command handling and its signal guard, without a mount."""

import os
import signal
import threading
import time

import pytest

from nf_core_gwas_biofuse import cli, mount

ENV = dict(os.environ)
RUN = ["run", "--vcz", "cohort.vcz", "--cohort-id", "cohort_a", "--genome-build", "GRCh37", "--ancestry", "EUR"]


@pytest.fixture(autouse=True)
def fusermount(monkeypatch):
    monkeypatch.setattr(mount, "fusermount_path", lambda: "/usr/bin/fusermount3")
    monkeypatch.delenv("SLURM_JOB_NUM_NODES", raising=False)


def _run(capsys, *extra):
    status = cli.main([*RUN, *extra])
    return status, capsys.readouterr().err


def test_multi_node_allocation_is_refused(capsys, monkeypatch):
    monkeypatch.setenv("SLURM_JOB_NUM_NODES", "2")
    status, err = _run(capsys, "--", "true")
    assert status == 2
    assert "SLURM_JOB_NUM_NODES=2" in err


@pytest.mark.parametrize(
    "extra, message",
    [
        (["--"], "no command"),
        (["--state-root", "/tmp/a,b", "--", "true"], "--state-root"),
        (["--basename", "a/b", "--", "true"], "--basename"),
        (["--ancestry", "E UR", "--", "true"], "ancestry"),
        (["--record-dir", "/nonexistent/records", "--", "true"], "--record-dir"),
        (["--samples-file", "/nonexistent/samples.txt", "--", "true"], "--samples-file"),
    ],
)
def test_inputs_the_launcher_owns_are_validated_before_anything_is_mounted(capsys, extra, message):
    status, err = _run(capsys, *extra)
    assert status == 2
    assert message in err


def test_a_store_that_is_not_zarr_is_refused(capsys, tmp_path):
    status = cli.main(
        [
            "run",
            "--vcz",
            str(tmp_path),
            "--cohort-id",
            "c",
            "--genome-build",
            "GRCh37",
            "--ancestry",
            "EUR",
            "--",
            "true",
        ]
    )
    assert status == 2
    assert "not a Zarr v2 or v3 group root" in capsys.readouterr().err


def test_default_basename_strips_only_the_vcz_suffix():
    assert cli.default_basename("/data/cohort.vcz") == "cohort"
    assert cli.default_basename("/data/cohort.v2.vcz/") == "cohort.v2"
    assert cli.default_basename("/data/cohort") == "cohort"


def test_the_manifest_token_is_substituted_everywhere():
    assert cli.substitute(["run", "--cohort_manifest", "{cohort_manifest}", "x={cohort_manifest}"], "/m.csv") == [
        "run",
        "--cohort_manifest",
        "/m.csv",
        "x=/m.csv",
    ]


def test_command_status_is_propagated_the_way_a_shell_reports_it():
    guard = cli.SignalGuard()
    assert cli.run_command(["sh", "-c", "exit 42"], ENV, guard, 1, cli.Log()) == 42
    assert cli.run_command(["sh", "-c", "kill -TERM $$"], ENV, guard, 1, cli.Log()) == 128 + signal.SIGTERM
    assert cli.run_command(["/nonexistent/command"], ENV, guard, 1, cli.Log()) == 127


def test_a_signal_is_forwarded_to_the_command_and_then_propagates():
    guard = cli.SignalGuard()
    timer = threading.Timer(0.5, guard._handle, (signal.SIGTERM, None))
    timer.start()
    started = time.monotonic()
    with pytest.raises(cli.Signalled) as ended:
        cli.run_command(["sleep", "30"], ENV, guard, 10, cli.Log())
    assert ended.value.signum == signal.SIGTERM
    assert time.monotonic() - started < 5


def test_a_signal_before_the_command_is_registered_still_reaches_it():
    guard = cli.SignalGuard()
    guard._handle(signal.SIGTERM, None)
    started = time.monotonic()
    with pytest.raises(cli.Signalled):
        cli.run_command(["sleep", "30"], ENV, guard, 10, cli.Log())
    assert time.monotonic() - started < 5


def test_a_command_that_ignores_the_signal_is_killed_after_the_grace_period():
    guard = cli.SignalGuard()
    guard._handle(signal.SIGTERM, None)
    started = time.monotonic()
    with pytest.raises(cli.Signalled):
        cli.run_command(["sh", "-c", "trap '' TERM; sleep 30"], ENV, guard, 1, cli.Log())
    assert time.monotonic() - started < 5
