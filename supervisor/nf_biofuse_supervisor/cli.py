# Written by the nf-core/gwas team and released under the MIT license.
"""``nf-core-gwas-biofuse``: mount one VCF Zarr cohort as a PLINK 1 view and run a command against it.

    nf-core-gwas-biofuse run --vcz STORE.vcz --cohort-id ID --genome-build GRCh37 --ancestry EUR \\
        [selection options] -- nextflow run nf-core/gwas --cohort_manifest '{cohort_manifest}' ...
    nf-core-gwas-biofuse identity --vcz STORE.vcz [selection options]
    nf-core-gwas-biofuse reap-stale [--state-root DIR]

The literal token ``{cohort_manifest}`` anywhere in the command is replaced with the generated manifest
path. ``BIOFUSE_COHORT_MANIFEST``, ``BIOFUSE_MOUNT``, ``BIOFUSE_BASENAME``, ``BIOFUSE_VIEW_ID``,
``BIOFUSE_VIEW_RECORD`` and ``BIOFUSE_READINESS_SECONDS`` are exported into the command's environment.
The command's exit status is the launcher's; a signal that ends the run is re-raised after teardown.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass

from . import __version__, identity, manifest, mount, record, store

PROG = "nf-core-gwas-biofuse"
MANIFEST_TOKEN = "{cohort_manifest}"
PATH_CELL = re.compile(r"^[^\s,]+$")
HANDLED_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)
COMMAND_POLL_INTERVAL = 0.2


class UsageError(ValueError):
    """An input or execution context the launcher refuses. Exit status 2."""


class Log:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose

    def __call__(self, message: str) -> None:
        print(f"[{PROG}] {message}", file=sys.stderr, flush=True)

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

    run = subparsers.add_parser("run", help="Mount the view, run a command against it, tear the mount down.")
    run.add_argument("--vcz", required=True, metavar="STORE", help="The VCF Zarr store directory.")
    run.add_argument("--cohort-id", required=True, help="Cohort identifier for the manifest row.")
    run.add_argument(
        "--genome-build", required=True, choices=manifest.GENOME_BUILDS, help="Genome build of the cohort."
    )
    run.add_argument("--ancestry", required=True, help="Ancestry label for the manifest row.")
    run.add_argument("--basename", help="Fileset stem of the virtual trio (default: the store name without .vcz).")
    run.add_argument(
        "--identity",
        choices=("declared", "digest"),
        default="declared",
        help=(
            "Declare the view identity to the pipeline (default), or leave the column empty so the pipeline "
            "digests the trio through the mount."
        ),
    )
    run.add_argument(
        "--state-root",
        default=mount.default_state_root(),
        help="Where the mountpoint and run state live (default: $XDG_RUNTIME_DIR, else $TMPDIR, else /tmp).",
    )
    run.add_argument(
        "--record-dir",
        default=os.getcwd(),
        help="Where the <cohort_id>.biofuse_view.json record is written (default: the current directory).",
    )
    run.add_argument(
        "--readiness-timeout",
        type=float,
        default=120.0,
        metavar="SECONDS",
        help="How long to wait for the virtual trio to become readable (default: 120).",
    )
    run.add_argument(
        "--shutdown-grace",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="How long a signalled command may take to exit before it is killed (default: 30).",
    )
    run.add_argument(
        "--keep-state", action="store_true", help="Leave the state directory and logs in place after the run."
    )
    run.add_argument("-v", "--verbose", action="store_true")
    _add_selection_arguments(run)
    daemon = run.add_argument_group("daemon options (do not enter the view identity)")
    daemon.add_argument(
        "--readahead-buffer-size", metavar="SIZE", help="Cap on the encoder's readahead window, for example 64M."
    )
    daemon.add_argument(
        "--readahead-workers", type=int, metavar="N", help="Worker threads servicing the readahead pool."
    )
    daemon.add_argument("--log-level", choices=("WARNING", "INFO", "DEBUG"), help="Daemon logging verbosity.")
    run.add_argument("command", nargs=argparse.REMAINDER, help="The command to run, after '--'.")

    identity_parser = subparsers.add_parser(
        "identity", help="Print the view identity and its preimage without mounting anything."
    )
    identity_parser.add_argument("--vcz", required=True, metavar="STORE", help="The VCF Zarr store directory.")
    _add_selection_arguments(identity_parser)

    reap = subparsers.add_parser("reap-stale", help="Tear down views whose launcher is gone.")
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
            f"{package} is not installed in this environment; create it from launcher/biofuse/environment.yml"
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
# Execution-context validation the launcher owns
# ----------------------------------------------------------------------------------------------------


def validate_context(args: argparse.Namespace, basename: str) -> None:
    nodes = os.environ.get("SLURM_JOB_NUM_NODES", "")
    if nodes.isdigit() and int(nodes) > 1:
        raise UsageError(
            f"SLURM_JOB_NUM_NODES={nodes}: the mount is visible on one host only, so a multi-node allocation "
            "cannot use it"
        )
    if not args.command:
        raise UsageError("no command given after '--'")
    # The launcher's own teardown runs fusermount; find it before anything is mounted.
    mount.fusermount_path()
    # The manifest is written unquoted, and its trio paths are made of the state root and the basename.
    if not PATH_CELL.match(args.state_root):
        raise UsageError(f"--state-root {args.state_root!r} may not contain whitespace or a comma")
    if not PATH_CELL.match(basename) or "/" in basename:
        raise UsageError(f"--basename {basename!r} may not contain whitespace, a comma or a slash")
    manifest.validate_cohort(args.cohort_id, args.genome_build, args.ancestry)
    if not os.path.isdir(args.record_dir):
        raise UsageError(f"--record-dir {args.record_dir!r} is not a directory")
    if args.samples_file and not os.path.isfile(args.samples_file):
        raise UsageError(f"--samples-file {args.samples_file!r} is not a file")


# ----------------------------------------------------------------------------------------------------
# Signals and the command
# ----------------------------------------------------------------------------------------------------


class Signalled(Exception):
    def __init__(self, signum: int):
        super().__init__(signum)
        self.signum = signum


class SignalGuard:
    """Records the first handled signal and forwards every one to the command's process group.

    The handler never raises: the run notices the signal at its next poll and winds down through the
    one teardown path, so a signal can neither interrupt the teardown nor land between starting the
    command and registering it. Nextflow spawns children and container runtimes that must wind down
    too, and a graceful Nextflow shutdown is what lets the mount be released cleanly."""

    def __init__(self):
        self.received: int | None = None
        self._child: subprocess.Popen | None = None
        self._forwarded = False
        self._previous: dict = {}

    def install(self, log: Log) -> None:
        for signum in HANDLED_SIGNALS:
            # A signal ignored on entry stays ignored: a launcher backgrounded without job control
            # has SIGINT set to SIG_IGN by its shell so that a terminal Ctrl-C does not reach it.
            if signal.getsignal(signum) == signal.SIG_IGN:
                log.debug(f"{signal.Signals(signum).name} is ignored in this context; cancel with SIGTERM")
                continue
            self._previous[signum] = signal.signal(signum, self._handle)

    def restore(self) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous.clear()

    def _forward(self, signum: int) -> None:
        if self._child is None:
            return
        self._forwarded = True
        try:
            os.killpg(self._child.pid, signum)
        except ProcessLookupError:
            pass

    def _handle(self, signum, _frame) -> None:
        if self.received is None:
            self.received = signum
        self._forward(signum)

    def adopt(self, child: subprocess.Popen | None) -> None:
        """Register the command. A signal that arrived before it was registered is forwarded now."""
        self._child = child
        if child is not None and self.received is not None and not self._forwarded:
            self._forward(self.received)

    def check(self) -> None:
        if self.received is not None:
            raise Signalled(self.received)


def run_command(argv: list[str], env: dict, guard: SignalGuard, shutdown_grace: float, log: Log) -> int:
    """Run the command in its own process group and return its status, ``128 + N`` for a death by
    signal N as a shell reports it. On a handled signal the command has ``shutdown_grace`` seconds to
    exit before its group is killed; the signal then propagates as :class:`Signalled`."""
    try:
        child = subprocess.Popen(argv, env=env, process_group=0)
    except OSError as error:
        log(f"cannot run {argv[0]!r}: {error.strerror}")
        return 127 if isinstance(error, FileNotFoundError) else 126
    guard.adopt(child)
    try:
        while guard.received is None:
            try:
                status = child.wait(timeout=COMMAND_POLL_INTERVAL)
            except subprocess.TimeoutExpired:
                continue
            # A command that ended as the signal arrived ended because of it, most likely by the forward.
            if guard.received is None:
                return 128 - status if status < 0 else status
        log(f"received {signal.Signals(guard.received).name}")
        if child.poll() is not None:
            raise Signalled(guard.received)
        log(f"waiting up to {shutdown_grace:g}s for the command to exit")
        try:
            child.wait(timeout=shutdown_grace)
        except subprocess.TimeoutExpired:
            log(f"command did not exit within {shutdown_grace:g}s; sending SIGKILL")
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            # Never waited for unbounded: a reader stuck on a wedged mount is in D state and dies only
            # once the teardown has aborted the connection under it.
            try:
                child.wait(timeout=mount.UNMOUNT_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
        raise Signalled(guard.received)
    finally:
        guard.adopt(None)


def substitute(argv: list[str], manifest_path: str) -> list[str]:
    return [argument.replace(MANIFEST_TOKEN, manifest_path) for argument in argv]


# ----------------------------------------------------------------------------------------------------
# Subcommands
# ----------------------------------------------------------------------------------------------------


def command_run(args: argparse.Namespace) -> int:
    log = Log(args.verbose)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    store_path = os.path.realpath(args.vcz)
    basename = args.basename or default_basename(args.vcz)
    validate_context(args, basename)
    store_info = store.read_store(store_path)
    plan = resolve_view(args, store_info, resolve_revision(args, store_path, log))
    paths = mount.ViewPaths.under(os.path.realpath(args.state_root), plan.short_id)
    trio = {extension: os.path.join(paths.mount_dir, f"{basename}.{extension}") for extension in ("bed", "bim", "fam")}
    declared = plan.genotype_view_id if args.identity == "declared" else None
    manifest_text = manifest.render(
        cohort_id=args.cohort_id,
        genome_build=args.genome_build,
        ancestry=args.ancestry,
        genotype_view_id=declared,
        **trio,
    )
    log.debug(f"view id      : {plan.genotype_view_id}")
    log.debug(f"state dir    : {paths.view_dir}")

    lock = mount.claim_view(paths, log)
    guard = SignalGuard()
    guard.install(log)
    daemon = None
    watchdog = None
    exit_status = 1
    try:
        argv = mount.daemon_argv(store_path, basename, paths.mount_dir, plan.daemon_options)
        log.debug(f"daemon cmd   : {' '.join(argv)}")
        daemon = mount.start_daemon(paths, argv)
        watchdog = mount.start_watchdog(paths, args.keep_state)
        log.debug(f"watchdog pid : {watchdog.pid}")
        readiness = mount.wait_ready(paths, basename, daemon, args.readiness_timeout, guard.check)
        log(f"mount ready in {readiness:.3f}s at {paths.mount_dir} (view {plan.short_id})")

        view_record = record.build_record(
            cohort_id=args.cohort_id,
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
        )
        record_file = record.record_path(os.path.abspath(args.record_dir), args.cohort_id)
        record.write_record(record_file, view_record)
        log(f"view record: {record_file}")
        manifest.write(paths.manifest, manifest_text)
        log(f"cohort manifest: {paths.manifest}")

        env = dict(
            os.environ,
            BIOFUSE_COHORT_MANIFEST=paths.manifest,
            BIOFUSE_MOUNT=paths.mount_dir,
            BIOFUSE_BASENAME=basename,
            BIOFUSE_VIEW_ID=plan.genotype_view_id,
            BIOFUSE_VIEW_RECORD=record_file,
            BIOFUSE_READINESS_SECONDS=f"{readiness:.3f}",
        )
        command = substitute(args.command, paths.manifest)
        log.debug(f"exec         : {' '.join(command)}")
        guard.check()
        exit_status = run_command(command, env, guard, args.shutdown_grace, log)
        log(f"command exited with status {exit_status}")
    except Signalled as ended:
        log(f"tearing the mount down after {signal.Signals(ended.signum).name}")
        exit_status = 128 + ended.signum
    except mount.MountError as failure:
        log(f"error: {failure}")
        exit_status = failure.exit_code
        _print_daemon_log(paths, log)
    finally:
        log.debug("cleanup: starting")
        mount.teardown(paths, daemon, watchdog, args.keep_state, log)
        mount.release_lock(lock)
        log.debug("cleanup: done")
        guard.restore()

    if guard.received is not None:
        # End the way the signal would have ended the launcher, so a calling shell sees it.
        signal.signal(guard.received, signal.SIG_DFL)
        os.kill(os.getpid(), guard.received)
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
    handlers = {"run": command_run, "identity": command_identity, "reap-stale": command_reap_stale}
    try:
        return handlers[args.subcommand](args)
    except (UsageError, store.StoreError, manifest.ManifestError, mount.MountError) as failure:
        print(f"{PROG}: error: {failure}", file=sys.stderr, flush=True)
        return getattr(failure, "exit_code", 2)


if __name__ == "__main__":
    sys.exit(main())
