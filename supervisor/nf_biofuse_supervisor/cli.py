# SPDX-License-Identifier: Apache-2.0
"""``nf-biofuse-supervisor``: own the lifecycle of one BioFuse PLINK 1 view of a VCF Zarr store.

    nf-biofuse-supervisor serve --vcz STORE.vcz --manifest-template TEMPLATE --manifest OUT \\
        [selection options] [--watch-pid PID] [--ready-file FILE]
    nf-biofuse-supervisor identity --vcz STORE.vcz [selection options]
    nf-biofuse-supervisor reap-stale [--state-root DIR]

``serve`` is what the nf-biofuse Nextflow plugin starts before a run: it computes the view identity,
mounts the view, waits until the virtual trio is readable, writes the view record and the manifest
rendered from the template, reports readiness in ``--ready-file``, and serves until it is asked to stop
(``SIGTERM``, ``SIGINT``, ``SIGHUP`` or ``SIGQUIT``) or the process ``--watch-pid`` names is gone. It
then tears the mount down, removes the manifest and the readiness report, and exits.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from dataclasses import dataclass

from . import __version__, identity, manifest, mount, record, store

PROG = "nf-biofuse-supervisor"
PATH_CELL = re.compile(r"^[^\s,]+$")
HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
SERVE_POLL_INTERVAL = 0.2


class UsageError(ValueError):
    """An input or execution context the supervisor refuses. Exit status 2."""

    exit_code = 2


class Log:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def __call__(self, message: str) -> None:
        print(f"[{PROG} {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)

    def debug(self, message: str) -> None:
        if self.verbose:
            self(message)


# ----------------------------------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------------------------------


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("selection options (enter the view identity)")
    group.add_argument(
        "--min-alleles", type=int, metavar="N", help="Sites with at least N alleles listed in REF and ALT."
    )
    group.add_argument("--regions", metavar="LIST", help="Regions to include (bcftools -r syntax).")
    group.add_argument("--targets", metavar="LIST", help="Target regions to include (bcftools -t syntax).")
    group.add_argument("--samples", metavar="LIST", help="Samples to include (bcftools -s syntax).")
    group.add_argument(
        "--samples-file", metavar="FILE", help="File of sample names to include; its content enters the identity."
    )
    group.add_argument("--force-samples", action="store_true", help="Only warn about unknown samples.")
    group.add_argument("--include", metavar="EXPR", help="Filter expression to include variant sites.")
    group.add_argument("--exclude", metavar="EXPR", help="Filter expression to exclude variant sites.")
    group.add_argument("--types", metavar="LIST", help="Comma-separated variant types to include.")
    group.add_argument("--exclude-types", metavar="LIST", help="Comma-separated variant types to exclude.")
    group.add_argument(
        "--autosomes-only",
        action="store_true",
        help="Serve autosomes 1-22 only. Required when the store carries variants on any other contig.",
    )
    group.add_argument(
        "--source-revision",
        metavar="TOKEN",
        help="Declare the store's revision instead of digesting its content, for an externally versioned store.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    serve = subparsers.add_parser("serve", help="Mount the view, publish its manifest, serve until stopped.")
    serve.add_argument("--vcz", required=True, metavar="STORE", help="The VCF Zarr store directory.")
    serve.add_argument(
        "--manifest-template",
        required=True,
        metavar="FILE",
        help=f"Template of the manifest to write; placeholders: {', '.join(manifest.PLACEHOLDERS)}.",
    )
    serve.add_argument("--manifest", required=True, metavar="FILE", help="Where to write the rendered manifest.")
    serve.add_argument("--basename", help="Fileset stem of the virtual trio (default: the store name without .vcz).")
    serve.add_argument(
        "--identity",
        choices=("declared", "digest"),
        default="declared",
        help="Fill {view_id} with the view identity (default), or leave it empty so the consumer digests the trio.",
    )
    serve.add_argument(
        "--state-root",
        default=mount.default_state_root(),
        help="Where the mountpoint and view state live (default: $XDG_RUNTIME_DIR, else $TMPDIR, else /tmp).",
    )
    serve.add_argument(
        "--record-dir",
        default=os.getcwd(),
        help="Where the <basename>.biofuse_view.json record is written (default: the current directory).",
    )
    serve.add_argument(
        "--readiness-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="How long to wait for the virtual trio to become readable (default: 120).",
    )
    serve.add_argument("--watch-pid", type=int, metavar="PID", help="Tear down when this process is gone.")
    serve.add_argument("--ready-file", metavar="FILE", help="Where readiness, or the reason for failing, is reported.")
    serve.add_argument(
        "--keep-state", action="store_true", help="Leave the view state directory and logs in place after teardown."
    )
    serve.add_argument("-v", "--verbose", action="store_true")
    _add_selection_arguments(serve)
    daemon = serve.add_argument_group("daemon options (do not enter the view identity)")
    daemon.add_argument(
        "--readahead-buffer-size", metavar="SIZE", help="Cap on the encoder's readahead window, for example 64M."
    )
    daemon.add_argument(
        "--readahead-workers", type=int, metavar="N", help="Worker threads servicing the readahead pool."
    )
    daemon.add_argument("--log-level", choices=("WARNING", "INFO", "DEBUG"), help="Daemon logging verbosity.")

    identity_parser = subparsers.add_parser(
        "identity", help="Print the view identity and its preimage without mounting anything."
    )
    identity_parser.add_argument("--vcz", required=True, metavar="STORE", help="The VCF Zarr store directory.")
    _add_selection_arguments(identity_parser)

    reap = subparsers.add_parser("reap-stale", help="Tear down views whose supervisor is gone.")
    reap.add_argument("--state-root", default=mount.default_state_root())
    return parser


# ----------------------------------------------------------------------------------------------------
# Resolving a view from the arguments and the store
# ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewPlan:
    preimage: dict
    genotype_view_id: str
    short_id: str
    daemon_options: list[str]
    # What the daemon is given as --targets: the user's, or the autosomes the policy selects.
    daemon_targets: str | None


def resolve_view(args: argparse.Namespace, store_info: store.StoreInfo, revision: tuple[str, str]) -> ViewPlan:
    """Everything the identity and the daemon command line are made of, from parsed arguments."""
    autosomal, other = store.contig_census(store_info)
    if not autosomal:
        contigs = ", ".join(store_info.variant_contig_ids) or "none"
        raise UsageError(f"{store_info.path} carries no variants on an autosome (contigs with variants: {contigs})")
    if args.autosomes_only:
        if args.targets:
            raise UsageError("--autosomes-only selects its own targets and cannot be combined with --targets")
        # The preimage records the policy and what it excluded rather than the target list it expands
        # to, which is a function of the store and therefore already covered by the revision.
        policy, excluded = "autosomes", other
        daemon_targets = ",".join(store.declared_autosomes(store_info))
    else:
        if other:
            raise UsageError(
                f"{store_info.path} carries variants on non-autosomal contigs ({', '.join(other)}); the PLINK 1 view "
                "reconstructs FAM sex as a placeholder, so pass --autosomes-only to serve autosomes 1-22 only"
            )
        policy, excluded = "all", ()
        daemon_targets = args.targets
    samples_file = os.path.abspath(args.samples_file) if args.samples_file else None

    projection = identity.build_projection(
        min_alleles=args.min_alleles,
        regions=args.regions,
        targets=args.targets,
        samples=args.samples,
        samples_file_sha256=store.file_digest(samples_file) if samples_file else None,
        include=args.include,
        exclude=args.exclude,
        types=args.types,
        exclude_types=args.exclude_types,
        force_samples=args.force_samples,
        contig_policy=policy,
        excluded_contigs=excluded,
    )
    preimage = identity.build_preimage(
        vcf_zarr_version=store_info.vcf_zarr_version,
        writer=store_info.writer,
        revision_mode=revision[0],
        revision_value=revision[1],
        biofuse_version=_required_version("biofuse"),
        vcztools_version=_required_version("vcztools"),
        projection=projection,
    )
    view_id = identity.view_id(preimage)

    # vcztools view-plink rejects multiallelic sites unless --max-alleles 2 skips them (its documented
    # contract), so the biallelic projection is always requested and always declared.
    options = ["--max-alleles", str(identity.MAX_ALLELES)]
    for flag, value in (
        ("--min-alleles", args.min_alleles),
        ("--regions", args.regions),
        ("--targets", daemon_targets),
        ("--samples", args.samples),
        ("--samples-file", samples_file),
        ("--include", args.include),
        ("--exclude", args.exclude),
        ("--types", args.types),
        ("--exclude-types", args.exclude_types),
        ("--readahead-buffer-size", getattr(args, "readahead_buffer_size", None)),
        ("--readahead-workers", getattr(args, "readahead_workers", None)),
        ("--log-level", getattr(args, "log_level", None)),
    ):
        if value is not None:
            options.extend([flag, str(value)])
    if args.force_samples:
        options.append("--force-samples")

    return ViewPlan(
        preimage=preimage,
        genotype_view_id=view_id,
        short_id=identity.short_view_id(view_id),
        daemon_options=options,
        daemon_targets=daemon_targets,
    )


def _required_version(package: str) -> str:
    version = record.package_version(package)
    if version is None:
        raise UsageError(
            f"{package} is not installed in this environment; create it from supervisor/environment.yml"
        )
    return version


def resolve_revision(args: argparse.Namespace, store_path: str, log: Log) -> tuple[str, str]:
    if args.source_revision is not None:
        if not args.source_revision.strip():
            raise UsageError("--source-revision must not be blank")
        return "declared", args.source_revision
    log.debug(f"digesting the content of {store_path}")
    return "sha256", store.content_digest(store_path)


def default_basename(store_path: str) -> str:
    name = os.path.basename(os.path.normpath(store_path))
    return name[: -len(".vcz")] if name.endswith(".vcz") else name


# ----------------------------------------------------------------------------------------------------
# Execution-context validation the supervisor owns
# ----------------------------------------------------------------------------------------------------


def validate_context(args: argparse.Namespace, basename: str) -> None:
    nodes = os.environ.get("SLURM_JOB_NUM_NODES", "")
    if nodes.isdigit() and int(nodes) > 1:
        raise UsageError(
            f"SLURM_JOB_NUM_NODES={nodes}: the mount is visible on one host only, so a multi-node allocation "
            "cannot use it"
        )
    # The supervisor's own teardown runs fusermount; find it before anything is mounted.
    mount.fusermount_path()
    # The trio paths are substituted verbatim into the manifest, and they are made of the state root and
    # the basename, so neither may carry a separator or whitespace.
    if not PATH_CELL.match(args.state_root):
        raise UsageError(f"--state-root {args.state_root!r} may not contain whitespace or a comma")
    if not PATH_CELL.match(basename) or "/" in basename:
        raise UsageError(f"--basename {basename!r} may not contain whitespace, a comma or a slash")
    if not os.path.isdir(args.record_dir):
        raise UsageError(f"--record-dir {args.record_dir!r} is not a directory")
    if args.samples_file and not os.path.isfile(args.samples_file):
        raise UsageError(f"--samples-file {args.samples_file!r} is not a file")


def watched_process(pid: int | None) -> tuple[int, str | None] | None:
    """``(pid, start time)`` of the process whose end ends the serve, so a reused pid never counts as it."""
    if pid is None:
        return None
    start_time = mount.process_start_time(pid)
    if start_time is None or not mount.process_alive(pid, start_time):
        raise UsageError(f"--watch-pid {pid} is not a running process")
    return pid, start_time


# ----------------------------------------------------------------------------------------------------
# Readiness report and signals
# ----------------------------------------------------------------------------------------------------


class ReadyReport:
    """The one file the plugin polls: ``{"status": "ready", ...}`` once the manifest is written, or
    ``{"status": "failed", "exit_code": N, "error": ...}`` when the supervisor gives up first. It is
    written atomically, so a reader never sees half of it."""

    def __init__(self, path: str | None):
        self.path = os.path.abspath(path) if path else None

    @staticmethod
    def render(status: str, **fields) -> str:
        return json.dumps({"status": status, **fields}, indent=2) + "\n"

    def write(self, text: str) -> None:
        if self.path is not None:
            manifest.write(self.path, text)

    def failed(self, exit_code: int, error: str) -> None:
        try:
            self.write(self.render("failed", exit_code=exit_code, error=error))
        except OSError:
            pass


class Signalled(Exception):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class SignalGuard:
    """Records the first stop signal. The handler never raises: the supervisor notices the signal at its
    next poll and winds down through the one teardown path, so a signal can never interrupt the teardown."""

    def __init__(self):
        self.received: int | None = None
        self._previous: dict = {}

    def install(self) -> None:
        # Installed unconditionally: these signals are the supervisor's stop protocol, and it runs in a
        # session of its own, so none of them reaches it by accident from a terminal.
        for signum in HANDLED_SIGNALS:
            self._previous[signum] = signal.signal(signum, self._handle)

    def restore(self) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()

    def _handle(self, signum, _frame) -> None:
        if self.received is None:
            self.received = signum

    def check(self) -> None:
        if self.received is not None:
            raise Signalled(self.received)


def detach_session(log: Log) -> None:
    """Leave the caller's session and process group. Ctrl-C at a terminal signals Nextflow's foreground
    process group, and closing the terminal hangs its session up; the supervisor must outlive both
    until Nextflow has stopped using the mount, and it is stopped by the plugin or the watched pid."""
    try:
        os.setsid()
    except PermissionError:
        log.debug("already a session leader")


def serve_until_stopped(guard: SignalGuard, watched: tuple[int, str | None] | None, log: Log) -> None:
    """Block until a stop signal arrives (raises :class:`Signalled`) or the watched process is gone."""
    while True:
        guard.check()
        if watched is not None and not mount.process_alive(*watched):
            log(f"watched process {watched[0]} is gone; tearing down")
            return
        time.sleep(SERVE_POLL_INTERVAL)


# ----------------------------------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------------------------------


def command_serve(args: argparse.Namespace) -> int:
    log = Log(args.verbose)
    detach_session(log)
    report = ReadyReport(args.ready_file)
    try:
        watched = watched_process(args.watch_pid)
        store_path = os.path.realpath(args.vcz)
        basename = args.basename or default_basename(args.vcz)
        validate_context(args, basename)
        template = manifest.read_template(args.manifest_template)
        store_info = store.read_store(store_path)
        plan = resolve_view(args, store_info, resolve_revision(args, store_path, log))
        paths = mount.ViewPaths.under(os.path.realpath(args.state_root), plan.short_id)
        declared = plan.genotype_view_id if args.identity == "declared" else None
        manifest_path = os.path.abspath(args.manifest)
        manifest_text = manifest.render(
            template, manifest.values(mount_dir=paths.mount_dir, basename=basename, view_id=declared)
        )
        manifest.check_target(manifest_path, manifest_text)
    except (UsageError, store.StoreError, manifest.ManifestError, mount.MountError) as failure:
        report.failed(getattr(failure, "exit_code", 2), str(failure))
        raise
    log.debug(f"view id      : {plan.genotype_view_id}")
    log.debug(f"state dir    : {paths.view_dir}")

    guard = SignalGuard()
    guard.install()
    try:
        lock = mount.claim_view(paths, log)
    except mount.MountError as failure:
        report.failed(failure.exit_code, str(failure))
        guard.restore()
        raise
    daemon = None
    watchdog = None
    exit_status = 1
    released = False
    try:
        argv = mount.daemon_argv(store_path, basename, paths.mount_dir, plan.daemon_options)
        log.debug(f"daemon cmd   : {' '.join(argv)}")
        daemon = mount.start_daemon(paths, argv)
        watchdog = mount.start_watchdog(paths, args.keep_state)
        log.debug(f"watchdog pid : {watchdog.pid}")
        readiness = mount.wait_ready(paths, basename, daemon, args.readiness_timeout, guard.check)
        log(f"mount ready in {readiness:.3f}s at {paths.mount_dir} (view {plan.short_id})")

        record_file = record.record_path(os.path.abspath(args.record_dir), basename)
        record.write_record(
            record_file,
            record.build_record(
                preimage=plan.preimage,
                genotype_view_id=plan.genotype_view_id,
                identity_mode=args.identity,
                declared_genotype_view_id=declared,
                runtime=record.runtime_versions(daemon.pid),
                mount_readiness_seconds=readiness,
                mount={
                    "store": store_path,
                    "zarr_format": store_info.zarr_format,
                    "mountpoint": paths.mount_dir,
                    "basename": basename,
                    "daemon_options": plan.daemon_options,
                },
                manifest={"path": manifest_path, "template": os.path.abspath(args.manifest_template)},
            ),
        )
        log(f"view record: {record_file}")
        # Registered before it is written, so a teardown from any point on removes it.
        mount.publish(paths, manifest_path, manifest_text)
        manifest.write(manifest_path, manifest_text)
        log(f"manifest: {manifest_path}")
        guard.check()
        if report.path is not None:
            ready = {
                "manifest": manifest_path,
                "mount": paths.mount_dir,
                "basename": basename,
                "genotype_view_id": plan.genotype_view_id,
                "identity_mode": args.identity,
                "record": record_file,
                "readiness_seconds": round(readiness, 3),
                "view_dir": paths.view_dir,
            }
            ready_text = report.render("ready", **ready)
            mount.publish(paths, report.path, ready_text)
            report.write(ready_text)
        log("serving; stop with SIGTERM")
        serve_until_stopped(guard, watched, log)
        exit_status = 0
    except Signalled as ended:
        log(f"received {signal.Signals(ended.signum).name}; tearing down")
        exit_status = 0
    except mount.MountError as failure:
        log(f"error: {failure}")
        exit_status = failure.exit_code
        report.failed(exit_status, str(failure))
        _print_daemon_log(paths, log)
    finally:
        log.debug("teardown: starting")
        released = mount.teardown(paths, daemon, watchdog, args.keep_state, log)
        mount.release_lock(lock)
        guard.restore()
        log.debug("teardown: done")
    if not released:
        log("teardown left the mount in place; see the recovery section of the documentation")
        return 1
    log(f"stopped with status {exit_status}")
    return exit_status


def _print_daemon_log(paths: mount.ViewPaths, log: Log) -> None:
    try:
        with open(paths.daemon_log, encoding="utf-8", errors="replace") as handle:
            content = handle.read().strip()
    except OSError:
        return
    if content:
        log("daemon log follows:")
        print(content, file=sys.stderr, flush=True)


def command_identity(args: argparse.Namespace) -> int:
    store_path = os.path.realpath(args.vcz)
    store_info = store.read_store(store_path)
    plan = resolve_view(args, store_info, resolve_revision(args, store_path, Log()))
    json.dump({"genotype_view_id": plan.genotype_view_id, "preimage": plan.preimage}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


def command_reap_stale(args: argparse.Namespace) -> int:
    mount.fusermount_path()
    mount.reap_stale(os.path.realpath(args.state_root), Log())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {"serve": command_serve, "identity": command_identity, "reap-stale": command_reap_stale}
    try:
        return handlers[args.subcommand](args)
    except (UsageError, store.StoreError, manifest.ManifestError, mount.MountError) as failure:
        print(f"{PROG}: error: {failure}", file=sys.stderr, flush=True)
        return getattr(failure, "exit_code", 2)


if __name__ == "__main__":
    sys.exit(main())
