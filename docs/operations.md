# Operating nf-biofuse

This is the operator's reference: how the plugin and its supervisor divide the work, the supervisor
environment and command line, what a run writes, the view identity contract, the constraints, recovery from a
wedged mount, and the evidence to collect from a first production run. It is ported from the runbook of the
nf-core/gwas host-side launcher (`docs/dev/biofuse_launcher.md` at lyh970817/gwas commit `fa44e58`), whose
lifecycle and identity the supervisor keeps unchanged.

## How a run uses the view

Nextflow stages every `path` input of a task before the task runs, and a pipeline typically validates its input
manifest, including that every file it names exists, before it creates any task. The mounted trio therefore has
to exist, and stay readable, from before the pipeline script runs until after its last task ends. A process
inside the pipeline could only mount a view for its own lifetime, which is too late for validation and staging
and too short for the consumers. The plugin mounts the view from the host for the whole run instead:

1. **`onFlowCreate`.** Nextflow calls a plugin's trace observers from `Session.start()`, on the main thread,
   before it parses the pipeline script. The plugin starts `nf-biofuse-supervisor serve` there and blocks until
   the supervisor reports the view ready, so no workflow code, parameter validation included, runs before the
   manifest and the trio exist. Measured on nf-core/gwas (Nextflow 26.04.6): the hook fires about 7 s before
   `ScriptRunner` launches the script and 8.5 s before nf-schema starts validating parameters, and a hook that
   blocks for 15 s delays validation by 15 s.
2. **The supervisor** computes the view identity (digesting the store unless a revision is declared), takes
   ownership of the view, starts the BioFuse mount daemon and a SIGKILL watchdog, waits until a short read of
   every trio member succeeds, writes the view record and the manifest rendered from the template, and writes
   its readiness report. It leaves the Nextflow JVM's session first, so a Ctrl-C at the terminal or a hangup,
   which Nextflow must survive long enough to stop its tasks, never reaches it.
3. **`onFlowComplete`.** When every task has ended, on success, failure or abort (Nextflow turns Ctrl-C,
   `SIGTERM` and `SIGHUP` into an abort that kills the tasks and then completes), the plugin sends the supervisor
   `SIGTERM` and waits for its teardown. A JVM shutdown hook does the same for exits that skip the observer.
4. **When nothing runs a handler.** The supervisor watches the JVM's pid and start time and tears down when the
   JVM is gone, which covers a `SIGKILL` of Nextflow. A detached watchdog waits on the supervisor's view lock and
   tears down when the supervisor is gone, which covers a `SIGKILL` of the supervisor. The mount daemon runs with
   `auto_unmount`, so the kernel drops the mount when the daemon dies, which covers a job-wide kill such as a
   Slurm cancellation.

The pipeline sees nothing of this: it reads an ordinary manifest that names ordinary PLINK 1 files.

### Loading the plugin next to the pipeline's own plugins

Nextflow replaces, rather than extends, the plugin list: `-plugins` on the command line, or a `plugins` block in a
`-c` config, becomes the whole list. Measured on Nextflow 26.04.6 with nf-core/gwas, whose `nextflow.config`
declares `nf-schema@2.8.0`: with `-plugins nf-biofuse@0.1.0` or a `-c` config declaring only `nf-biofuse`, the log
reports `Plugins declared=[nf-biofuse@0.1.0]`, Nextflow warns that the `validation.*` options are unrecognized,
and nf-schema is loaded later, unpinned, as `nf-schema version: latest` by the pipeline's `include`. List the
pipeline's plugins with their pinned versions next to `nf-biofuse`.

## The supervisor environment

```bash
conda env create -f supervisor/environment.yml -p <env>
<env>/bin/nf-biofuse-supervisor --version
```

It pins Python 3.12, `pyfuse3` 3.4.1 and `libfuse3>=3.18.3` from conda-forge, and `biofuse` 0.1.0, `vcztools`
0.2.0, `bio2zarr` 0.2.1, `zarr` 3.4.0 and `numcodecs` 0.17.0 from PyPI, and installs the supervisor from the
checkout. `biofuse` and `vcztools` decide the bytes of the served trio and enter the view identity, so changing
either pin changes every declared identity. `bio2zarr` provides `vcf2zarr`, which converts a VCF into a store.

The `libfuse3` floor is the fix, not a preference. In libfuse releases before 3.18.3, a build without
`HAVE_STATX` leaves a `FUSE_STATX` request unanswered: a `stat` that asks for the birth time, as GNU `stat` does,
blocks forever, and so does every later reader of the mount ([lyh970817/gwas#19](https://github.com/lyh970817/gwas/issues/19));
the conda-forge 3.18.2 build is affected. fuse-3.18.3 replies `ENOSYS` instead. The library that matters is the
one the mount daemon loads, which is the environment's copy, never the host's: the conda `pyfuse3` extension
resolves `libfuse3` from the environment. Distribution packages and container images built on them still ship
older releases, so the runtime has to come from conda-forge. Every run records the `libfuse3` the daemon
actually mapped, and its version, in the view record.

The host provides the rest: `/dev/fuse`, the kernel FUSE module, and a setuid `fusermount3` (or `fusermount`) on
`PATH`, which the supervisor needs to unmount and refuses to start without.

## The supervisor command line

```text
nf-biofuse-supervisor serve --vcz STORE --manifest-template FILE --manifest FILE [options]
nf-biofuse-supervisor identity --vcz STORE [selection options]
nf-biofuse-supervisor reap-stale [--state-root DIR]
nf-biofuse-supervisor --version
```

The plugin builds the `serve` command from the `biofuse` scope, passing each option only when it is set, and adds
`--watch-pid` (its JVM) and `--ready-file`. The command is also usable by hand, for example to inspect a view.

### `serve`

| Option | Default | Meaning |
|---|---|---|
| `--vcz STORE` | required | The VCF Zarr store directory, Zarr format 2 or 3. |
| `--manifest-template FILE` | required | The template; placeholders `{bed}` `{bim}` `{fam}` `{prefix}` `{mount}` `{basename}` `{view_id}`. |
| `--manifest FILE` | required | Where the rendered manifest is written. A file already there is refused unless it holds exactly this manifest. |
| `--basename NAME` | the store name without `.vcz` | File stem of the virtual trio; no whitespace, comma or slash. |
| `--identity {declared,digest}` | `declared` | Fill `{view_id}` with the view identity, or leave it empty so the pipeline digests the trio. |
| `--state-root DIR` | `$XDG_RUNTIME_DIR`, else `$TMPDIR`, else `/tmp` | Where the mountpoint and view state live; no whitespace or comma. |
| `--record-dir DIR` | the current directory | Where `<basename>.biofuse_view.json` is written; must exist. |
| `--readiness-timeout SECONDS` | `120` | How long to wait for the virtual trio to become readable. |
| `--watch-pid PID` | none | Tear down when this process (identified with its start time) is gone. |
| `--ready-file FILE` | none | Where readiness, or the reason for failing, is reported as JSON. |
| `--keep-state` | off | Leave the view state directory and its logs in place after teardown. |
| `-v`, `--verbose` | off | Also log the full view identity, the state directory and the daemon command line. |

Selection options choose what the view serves. They enter the view identity and are passed to BioFuse's
`mount-plink`, whose semantics are those of `vcztools view-plink`: `--min-alleles N`, `--regions LIST`,
`--targets LIST`, `--samples LIST`, `--samples-file FILE` (its content, not its path, enters the identity),
`--force-samples`, `--include EXPR`, `--exclude EXPR`, `--types LIST`, `--exclude-types LIST`,
`--autosomes-only` (serve autosomes 1-22 only; required when the store carries variants on any other contig;
not combinable with `--targets`) and `--source-revision TOKEN` (see [View identity](#view-identity)).
`--max-alleles 2` is always passed and cannot be changed: `vcztools view-plink` rejects multiallelic sites unless
they are skipped, so the biallelic projection is the only PLINK 1 view this backend serves.

Daemon options change only how the view is served and do not enter the identity: `--readahead-buffer-size SIZE`,
`--readahead-workers N` and `--log-level {WARNING,INFO,DEBUG}`.

`serve` ends on `SIGTERM`, `SIGINT`, `SIGHUP` or `SIGQUIT`, or when `--watch-pid` is gone, and then tears down.

| Exit status | Meaning |
|---|---|
| `0` | Served and torn down cleanly, whatever ended the serve. |
| `1` | A mount lifecycle step failed, or teardown left the mount in place. |
| `2` | Invalid input or context, a refused manifest path, or another live supervisor already owns this view. |
| `3` | The mount daemon exited during startup; its log is printed. |
| `4` | The virtual trio did not become readable within `--readiness-timeout`. |

The readiness report is `{"status": "ready", "manifest", "mount", "basename", "genotype_view_id",
"identity_mode", "record", "readiness_seconds", "view_dir"}` once the manifest is written, or `{"status":
"failed", "exit_code", "error"}` when the supervisor gives up first. The plugin reports a failure, with the last
lines of the supervisor's log, as the run's error.

### `identity`

Prints the view identity and the preimage it was taken over as JSON, without mounting anything. It accepts
`--vcz` and the selection options, so it answers whether two stores or two selections would serve the same view
before a run. Without `--source-revision` it reads every byte of the store.

### `reap-stale`

Tears down every view under `--state-root` whose supervisor is gone: removes its published files, unmounts,
stops the daemon, aborts a wedged connection if needed and removes the state. A view whose supervisor is still
alive is left alone. The same teardown runs automatically when a supervisor reclaims a stale view it is about to
mount, and the watchdog performs it when a supervisor is killed with `SIGKILL`.

## What a run writes

In the launch directory:

- the manifest (`biofuse.manifest`), while the run lasts: written with mode `0600` once the view is ready,
  removed first at teardown;
- `.nf-biofuse.log`, the supervisor's log, rewritten each run;
- `<basename>.biofuse_view.json`, the view record, in `biofuse.recordDir` (default: the launch directory), kept
  after the run; a later run with the same basename and record directory overwrites it.

Under the state root, per view:

```text
<state-root>/nf-biofuse/
├── view-<16 hex>.lock       ownership lock; empty and kept on purpose
└── view-<16 hex>/           removed at teardown unless keepState
    ├── mnt/                 the mountpoint: <basename>.bed, .bim, .fam
    ├── owner.pid            supervisor pid and start time
    ├── daemon.pid           daemon pid and start time
    ├── published.json       the manifest and readiness report, with the digests they were written with
    └── logs/
        ├── daemon.log       the mount daemon's output
        └── watchdog.log     the watchdog's report, when it had to act
```

`<16 hex>` is the first 16 hexadecimal characters of the view identity, so the same view under the same state
root always mounts at the same path, which is what lets `-resume` find its cached tasks (BioFuse reports every
served file with a zero modification time, so the path and size Nextflow hashes are stable). A second run for
a view that a live supervisor already owns fails with exit status `2`. Ownership is an exclusive `flock` on the
lock file, which the kernel releases however the supervisor dies.

Every teardown removes the files registered in `published.json` first, so nothing new can resolve the trio once
teardown starts, but only while they still hold what the supervisor wrote: a file changed since is left alone.

The view record's fields are `schema` (`nf-biofuse/view-record/1`), `genotype_view_id` (always the computed
identity), `identity_mode`, `declared_genotype_view_id` (what `{view_id}` declared; `null` in `digest` mode), the
`preimage`, `runtime` (`biofuse`, `vcztools`, `bio2zarr`, `zarr`, `numcodecs`, `pyfuse3`, `python`, and
`libfuse3` as the version and path of the library mapped into the daemon), `kernel`, `mount_readiness_seconds`,
`mount` (store path, Zarr format, mountpoint, basename and the daemon options), `manifest` (the manifest path and
template) and `information_loss`, the information a PLINK 1 view of the store cannot carry: `dosage`, `phase`,
`multiallelic_sites`, `fam_pedigree_sex_phenotype` and `genetic_map`.

nf-core/gwas records a declared identity in `genotypes/<cohort_id>/<cohort_id>.genotype_view.json` as
`source.identity` `{"mode": "declared", "value": "biofuse:<hex>"}`, so its record and the view record join on the
identity.

## View identity

```text
genotype_view_id = "biofuse:" + sha256(canonical JSON of the preimage)
```

The canonical form is JSON with sorted keys, no whitespace and UTF-8 text unescaped. The identity answers one
question — would this mount serve the same bytes as another one — so everything that changes the served bytes is
in the preimage and nothing else is:

| Preimage field | Content |
|---|---|
| `schema` | `nf-core/gwas/biofuse-view-identity/1` |
| `backend` | `{"family": "biofuse", "view": "mount-plink"}` |
| `source` | `kind` `vcz`, the store's `vcf_zarr_version` and writer (its `source` attribute), and `revision` `{mode, value}` |
| `runtime` | the `biofuse` and `vcztools` versions, which decide the encoded bytes |
| `projection` | `max_alleles` (always `2`), every selection option as given (`null` when absent), `samples_file_sha256`, `force_samples`, and `contigs` `{policy, excluded}` |

The schema string is the one the nf-core/gwas launcher used, kept so that a view keeps the identity the launcher
declared for it; the golden identity in the supervisor's tests is the launcher's.

The store revision has two modes. By default it is `sha256`: a digest over every file in the store, visited in
sorted relative-path order, each contributing its relative path, size and content digest, so the value depends
on names and bytes only and not on timestamps, ownership or permissions. That reads the whole store once per run,
before anything is mounted. `sourceRevision` declares the revision instead, with mode `declared`, for a store
versioned outside the plugin; the token is then your promise that it never names different content, and nothing
checks it.

`autosomesOnly` records the policy `autosomes` and the contigs it excluded rather than the target list it expands
to, because that list is a function of the store and already covered by the revision.

Left out on purpose, because none of them changes a served byte: the basename, the readahead and log options, the
mountpoint and state root, the host, the manifest template and everything it declares (for nf-core/gwas the cohort
metadata), and the `pyfuse3`, `libfuse3`, `zarr` and `numcodecs` versions. They are recorded in the view record
instead. Two consequences follow. Renaming the basename or moving the state root never moves the identity. And
because options enter the preimage as the text you gave, two spellings of one selection — samples listed in a
different order, say — produce two identities; that can only cost reuse, never share a result between different
views.

`identity` decides what the pipeline is told, not what is computed. In `declared` mode, the default, the manifest
carries `biofuse:<hex>`; nf-core/gwas trusts it, never reads the trio to identify the cohort, and builds every
reuse key downstream from it. In `digest` mode `{view_id}` is empty, so nf-core/gwas's `PREPARE_GENOTYPE_SOURCE`
task reads the whole trio through the mount and keys the cohort by the `sha256` of its bytes. That costs one full
encoding pass and exists for parity checks: a declared run and a digest run of one view produce the same results.

## Constraints

- **Apptainer or Singularity consumers only.** The mount is made without `allow_other`, so only the user who made
  it can read it. Rootful Docker cannot read it, and `--privileged` does not change that. Use an Apptainer or
  Singularity profile; their automounts make the mounted paths visible inside task containers. Choose a state root
  that those containers can reach.
- **linux-64 only.** `pyfuse3` is published on conda-forge for x86-64 Linux only, so the supervisor cannot be
  installed on arm64.
- **One host.** A FUSE mount is visible only on the host that made it, so Nextflow and every task must run there:
  the local executor, inside one allocation on a cluster. The supervisor refuses to start when
  `SLURM_JOB_NUM_NODES` is greater than 1.
- **One view per run.** The `biofuse` scope serves one store. For nf-core/gwas, the template declares exactly one
  cohort, so every row of the analysis manifest must name that `cohort_id`.
- **Workflow-read manifests only.** The manifest is written when the run starts; a file Nextflow reads while it
  launches, such as a `-params-file` or a config, is read before any plugin starts.
- **Autosomal, biallelic hard calls.** The view serves autosomes 1-22 (named `1`-`22` or `chr1`-`chr22`); a store
  with variants on any other contig is refused unless `autosomesOnly` is set, and one with no autosomal variants
  is refused outright. Multiallelic sites are always skipped. The BED holds hard calls only: dosage and phase are
  not carried.
- **Reconstructed placeholders.** A VCF Zarr store has no pedigree, sex, phenotype or genetic map. FAM columns 3-6
  (father, mother, sex, phenotype) and the BIM centimorgan column are placeholders written by the encoder, not
  data. Supply sex and phenotypes to the pipeline through its phenotype and covariate files, never through the FAM.

## Recovering from a wedged mount

A mount is wedged when its kernel connection has outstanding requests that nothing will answer, typically after
the daemon died without the kernel dropping the mount. Every process that touches it — `ls`, `stat`, `df`, a shell
completing a path, a task — enters uninterruptible sleep (`D` state).

1. **Do not touch the mountpoint, and do not try to kill the stuck readers.** A process in `D` state ignores
   `SIGKILL`; killing more processes only adds to the pile. Do not run `ls`, `stat`, `mountpoint` or `df` on the
   mount or its parent directory to diagnose it.
2. **Stop the supervisor cleanly if it is still alive.** Send `SIGTERM` to the supervisor (`owner.pid` in the view
   directory names it). Its teardown caps every unmount attempt, falls back to a lazy detach, and aborts the kernel
   connection when the mount is still present.
3. **Otherwise reap it.** `nf-biofuse-supervisor reap-stale --state-root <state_root>` tears down every view whose
   supervisor is gone, including the connection abort.
4. **By hand, if the supervisor environment is unavailable.** Find the mount in `/proc/self/mountinfo`, which
   never blocks. The third field is `0:<minor>`; write to that connection's abort file, then detach lazily:

   ```bash
   grep ' <state_root>/nf-biofuse/view-' /proc/self/mountinfo
   echo 1 > /sys/fs/fuse/connections/<minor>/abort
   fusermount3 -uz <state_root>/nf-biofuse/view-<16 hex>/mnt
   ```

   Aborting the connection makes every outstanding and future request fail with `ENOTCONN`, which releases the
   stuck readers so they exit. Then run `reap-stale`, or remove the view directory once it no longer appears in
   `/proc/self/mountinfo`.

`/sys/fs/fuse/connections/<minor>/waiting` shows the number of unanswered requests. For a hang in a live daemon,
`kill -USR1 <daemon pid>` writes every daemon thread's Python stack to `logs/daemon.log`; set `keepState = true`
to keep that log after the run.

## Evidence checklist for a first production run

The first run on a production Slurm cluster is the remaining acceptance step of
[lyh970817/gwas#8](https://github.com/lyh970817/gwas/issues/8). Run [the Slurm template](../examples/nf-core-gwas/slurm.sbatch)
once with `verbose = true`, run the same analyses once more as a control on a materialised trio, and collect:

- [ ] `.nf-biofuse.log`: the full view identity, `mount ready in … at … (view …)`, `view record: …`,
      `manifest: …`, `received SIGTERM; tearing down` and `stopped with status 0`.
- [ ] The view record `<basename>.biofuse_view.json`, with `runtime.libfuse3.version` at 3.18.3 or later.
- [ ] Nextflow's `pipeline_info/execution_trace_*.txt`, showing every task `COMPLETED` and no
      `PREPARE_GENOTYPE_SOURCE` task for the declared cohort.
- [ ] `genotypes/<cohort_id>/<cohort_id>.genotype_view.json`, whose `source.identity.value` equals the record's
      `genotype_view_id`.
- [ ] A result comparison against the control: the same analysis manifest run on the trio materialised from the
      same store with `vcztools view-plink`, the same selection options and `--max-alleles 2`, declared through an
      ordinary cohort manifest. Report the trio `sha256` sums on both sides and whether each published result is
      byte-identical; name any file that differs only by timestamps, such as the MultiQC report. `-o` takes the
      output stem verbatim and writes `<stem>.bed`, `<stem>.bim` and `<stem>.fam`; for a view with
      `autosomesOnly`, pass `--targets` with the store's own names for autosomes 1-22, which is what the
      supervisor gives the daemon:

      ```bash
      vcztools view-plink --max-alleles 2 [same selection options] -o '<stem>' '<cohort.vcz>'
      ```

- [ ] The versions around the run: `apptainer --version`, `uname -r`, the `libfuse3` path and version from the
      record, the Nextflow version, the pipeline revision and the plugin version.

## Tests

- **Plugin unit tests** (`make test`): config parsing and the supervisor command line, and the start, readiness,
  failure and stop paths against stand-in supervisors.
- **Supervisor pure tier** (`cd supervisor && pytest`, in the supervisor environment plus `pytest`): identity
  canonical form, exclusions and golden value, store digest, manifest templating and publication, view ownership,
  the watchdog and stale reclaim, on views that are never mounted.
- **Local FUSE tier** (`tests/local/run_local_tier.sh`): needs `/dev/fuse`, the supervisor environment, Nextflow
  with the plugin installed (`make install`), Apptainer, the nf-core/gwas fixture bundle (`GWAS_TEST_FIXTURES`)
  and an nf-core/gwas checkout (`GWAS_PIPELINE`), and skips without them. Its steps: `lifecycle_matrix.sh` (every
  way a run ends — completion, task failure, Ctrl-C at a terminal, `SIGTERM`, `SIGHUP`, `SIGKILL` of Nextflow,
  also mid-read, `SIGKILL` of the supervisor, a stale view reclaimed, a refused store, a second run for an owned
  view, `-resume` — leaves no mount, process, state, wedge or manifest), `parity_check.sh` (the mounted trio,
  read by pipeline tasks, is byte-identical to the fixture trio and plink2 agrees on both), `e2e_parity.sh`
  (nf-core/gwas `-profile test,apptainer` in declared and digest mode against a control on the fixture trio,
  then `-resume`), `concurrency_ladder.sh` (1, 2, 4 and 8 concurrent Apptainer `plink2 --freq` tasks on one
  mount) and `benchmark.sh` (readiness, wall time, daemon peak RSS and I/O amplification). For example:

  ```bash
  make install
  SUPERVISOR=<env>/bin/nf-biofuse-supervisor \
  GWAS_TEST_FIXTURES=<fixture bundle> GWAS_PIPELINE=<nf-core/gwas checkout> \
  NXF_APPTAINER_CACHEDIR=<image cache> \
      tests/local/run_local_tier.sh
  ```
