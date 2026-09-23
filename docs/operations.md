# BioFuse launcher

`nf-core-gwas-biofuse`, in `launcher/biofuse/`, serves one [VCF Zarr](https://github.com/sgkit-dev/vcf-zarr-spec) cohort to the pipeline as a PLINK 1 BED/BIM/FAM view mounted by [BioFuse](https://github.com/sgkit-dev/biofuse), without writing the trio to disk. It mounts the store, waits until the virtual trio is readable, writes a one-row cohort manifest that points at it, runs the Nextflow command you give it, and tears the mount down however the run ends. This page is the operator's runbook: installation, the command-line reference, what the launcher writes, the view identity contract, the constraints, recovery from a wedged mount, and a single-node Slurm run with the evidence to collect from it.

## Why the launcher sits outside the pipeline

The launcher is not a pipeline process and cannot become one. Nextflow stages every `path` input of a task before the task runs, and the pipeline validates the cohort manifest — including that every `bed`, `bim` and `fam` path exists — before it creates any task. The mounted trio therefore has to exist, and stay readable, from before `nextflow run` starts until after its last task ends. A process inside the DAG could only mount a view for its own lifetime, which is too late for validation and staging and too short for the consumers. The launcher owns the mount for the whole run instead, from the host, and hands the pipeline an ordinary cohort manifest; the pipeline sees nothing but a PLINK 1 cohort with a declared `genotype_view_id`.

## Environment

Create the conda environment from the file in the repository; it installs the launcher itself as well as everything it runs:

```bash
conda env create -f launcher/biofuse/environment.yml -p <launcher_env>
conda activate <launcher_env>
nf-core-gwas-biofuse --version
```

It pins Python 3.12, `pyfuse3` 3.4.1 and `libfuse3>=3.18.3` from conda-forge, and `biofuse` 0.1.0, `vcztools` 0.2.0, `bio2zarr` 0.2.1, `zarr` 3.4.0 and `numcodecs` 0.17.0 from PyPI. `biofuse` and `vcztools` decide the bytes of the served trio and enter the view identity, so changing either pin changes every declared `genotype_view_id`. `bio2zarr` provides `vcf2zarr`, which converts a VCF into a store the launcher can serve.

The `libfuse3` floor is the fix, not a preference. In libfuse releases before 3.18.3, a build without `HAVE_STATX` leaves a `FUSE_STATX` request unanswered: a `stat` that asks for the birth time, as GNU `stat` does, blocks forever, and so does every later reader of the mount. That was the hang first seen under a Nextflow run over the mount ([lyh970817/gwas#19](https://github.com/lyh970817/gwas/issues/19)); the conda-forge 3.18.2 build is affected. fuse-3.18.3 replies `ENOSYS` instead. The library that matters is the one the mount daemon loads, and that is the environment's copy, never the host's: the conda `pyfuse3` extension resolves `libfuse3` from the environment. Distribution packages and container images built on them still ship older releases, so the runtime has to come from conda-forge. Every run records the `libfuse3` the daemon actually mapped, and its version, in the view record.

The host provides the rest: `/dev/fuse`, the kernel FUSE module, and a setuid `fusermount3` (or `fusermount`) on `PATH`, which the launcher needs to unmount and refuses to start without.

## Command-line reference

```text
nf-core-gwas-biofuse run --vcz STORE --cohort-id ID --genome-build {GRCh37,GRCh38} --ancestry LABEL
                         [options] -- COMMAND...
nf-core-gwas-biofuse identity --vcz STORE [selection options]
nf-core-gwas-biofuse reap-stale [--state-root DIR]
nf-core-gwas-biofuse --version
```

### `run`

Mounts the view, runs `COMMAND` against it and tears the mount down. Everything after `--` is the command. The literal token `{cohort_manifest}` anywhere in it is replaced by the path of the generated manifest; quote it so the shell passes it through unchanged.

| Option                         | Default                                         | Meaning                                                                                                                   |
| ------------------------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `--vcz STORE`                  | required                                        | The VCF Zarr store directory, Zarr format 2 or 3.                                                                         |
| `--cohort-id ID`               | required                                        | `cohort_id` of the manifest row; no whitespace or comma.                                                                  |
| `--genome-build`               | required                                        | `GRCh37` or `GRCh38`.                                                                                                     |
| `--ancestry LABEL`             | required                                        | `ancestry` of the manifest row, under the cohort manifest's own pattern.                                                  |
| `--basename NAME`              | the store name without `.vcz`                   | File stem of the virtual trio; no whitespace, comma or slash.                                                             |
| `--identity {declared,digest}` | `declared`                                      | Write the view identity into the manifest's `genotype_view_id`, or leave the cell empty so the pipeline digests the trio. |
| `--state-root DIR`             | `$XDG_RUNTIME_DIR`, else `$TMPDIR`, else `/tmp` | Where the mountpoint and run state live; no whitespace or comma.                                                          |
| `--record-dir DIR`             | the current directory                           | Where `<cohort_id>.biofuse_view.json` is written; must exist.                                                             |
| `--readiness-timeout SECONDS`  | `120`                                           | How long to wait for the virtual trio to become readable.                                                                 |
| `--shutdown-grace SECONDS`     | `30`                                            | How long a signalled command may take to exit before its process group is killed.                                         |
| `--keep-state`                 | off                                             | Leave the state directory and its logs in place after the run.                                                            |
| `-v`, `--verbose`              | off                                             | Also log the full view identity, the state directory and the daemon and command lines.                                    |

Selection options choose what the view serves. They enter the view identity and are passed to BioFuse's `mount-plink`, whose semantics are those of `vcztools`:

| Option                    | Meaning                                                                                                                   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| `--min-alleles N`         | Sites with at least `N` alleles listed in REF and ALT.                                                                    |
| `--regions LIST`          | Regions to include, `bcftools -r` syntax.                                                                                 |
| `--targets LIST`          | Target regions to include, `bcftools -t` syntax.                                                                          |
| `--samples LIST`          | Samples to include, `bcftools -s` syntax.                                                                                 |
| `--samples-file FILE`     | File of sample names to include; its content, not its path, enters the identity.                                          |
| `--force-samples`         | Only warn about unknown samples.                                                                                          |
| `--include EXPR`          | Filter expression selecting variant sites.                                                                                |
| `--exclude EXPR`          | Filter expression excluding variant sites.                                                                                |
| `--types LIST`            | Comma-separated variant types to include.                                                                                 |
| `--exclude-types LIST`    | Comma-separated variant types to exclude.                                                                                 |
| `--autosomes-only`        | Serve autosomes 1-22 only. Required when the store carries variants on any other contig; not combinable with `--targets`. |
| `--source-revision TOKEN` | Declare the store's revision instead of digesting its content (see [View identity](#view-identity)).                      |

`--max-alleles 2` is always passed and cannot be changed: `vcztools view-plink` rejects multiallelic sites unless they are skipped, so the biallelic projection is the only PLINK 1 view this backend serves.

Daemon options change only how the view is served and do not enter the identity:

| Option                             | Meaning                                                   |
| ---------------------------------- | --------------------------------------------------------- |
| `--readahead-buffer-size SIZE`     | Cap on the encoder's readahead window, for example `64M`. |
| `--readahead-workers N`            | Worker threads servicing the readahead pool.              |
| `--log-level {WARNING,INFO,DEBUG}` | Daemon logging verbosity.                                 |

The command runs in its own process group with these variables added to its environment: `BIOFUSE_COHORT_MANIFEST`, `BIOFUSE_MOUNT`, `BIOFUSE_BASENAME`, `BIOFUSE_VIEW_ID`, `BIOFUSE_VIEW_RECORD` and `BIOFUSE_READINESS_SECONDS`. `SIGINT`, `SIGTERM`, `SIGHUP` and `SIGQUIT` are forwarded to that group; Nextflow then has `--shutdown-grace` seconds to wind down before the group is killed, the mount is torn down, and the launcher ends by the same signal. A launcher started in the background by a non-interactive shell ignores `SIGINT`, so cancel it with `SIGTERM`.

| Exit status   | Meaning                                                                    |
| ------------- | -------------------------------------------------------------------------- |
| the command's | The command ran; its status is passed through.                             |
| `128 + N`     | The command or the launcher ended on signal `N`.                           |
| `1`           | A mount lifecycle step failed.                                             |
| `2`           | Invalid input or context, or another live launcher already owns this view. |
| `3`           | The mount daemon exited during startup; its log is printed.                |
| `4`           | The virtual trio did not become readable within `--readiness-timeout`.     |
| `126`, `127`  | The command could not be executed, or was not found.                       |

### `identity`

Prints `genotype_view_id` and the preimage it was taken over as JSON, without mounting anything. It accepts `--vcz` and the selection options, so it answers whether two stores or two selections would serve the same view before a run. Without `--source-revision` it reads every byte of the store.

### `reap-stale`

Tears down every view under `--state-root` (same default as `run`) whose launcher is gone: unmount, stop the daemon, abort a wedged connection if needed, remove the state. A view whose launcher is still alive is left alone. The same teardown runs automatically when a launcher reclaims a stale view it is about to mount, and a detached watchdog performs it when a launcher is killed with `SIGKILL`.

## What the launcher writes

Under the state root, per view:

```text
<state-root>/nf-core-gwas-biofuse/
├── view-<16 hex>.lock       ownership lock; empty and kept on purpose
└── view-<16 hex>/           removed at teardown unless --keep-state
    ├── mnt/                 the mountpoint: <basename>.bed, .bim, .fam
    ├── owner.pid            launcher pid and start time
    ├── daemon.pid           daemon pid and start time
    ├── cohort_manifest.csv  the temporary cohort manifest, while the command runs
    └── logs/
        ├── daemon.log       the mount daemon's output
        └── watchdog.log     the watchdog's report, when it had to act
```

`<16 hex>` is the first 16 hexadecimal characters of the view identity, so the same view under the same state root always mounts at the same path, and a second launcher for a view that a live launcher already owns refuses with exit status `2`. Ownership is an exclusive `flock` on the lock file, which the kernel releases however the launcher dies.

`cohort_manifest.csv` is written with mode `0600` after the view is ready and deleted first at teardown. It carries the pipeline's full cohort manifest header and one row: `cohort_id`, `genome_build`, `ancestry`, the three mounted `bed`/`bim`/`fam` paths and, in `declared` mode, `genotype_view_id`. The PLINK 2 columns are empty.

`<record-dir>/<cohort_id>.biofuse_view.json` is the view record, written once the view is ready and kept after the run; a later run with the same `cohort_id` and `--record-dir` overwrites it. Its fields are `schema` (`nf-core/gwas/biofuse-view-record/1`), `cohort_id`, `genotype_view_id` (always the computed identity), `identity_mode`, `declared_genotype_view_id` (what the manifest declared; `null` in `digest` mode), the `preimage`, `runtime` (`biofuse`, `vcztools`, `bio2zarr`, `zarr`, `numcodecs`, `pyfuse3`, `python`, and `libfuse3` as the version and path of the library mapped into the daemon), `kernel`, `mount_readiness_seconds`, `mount` (store path, Zarr format, mountpoint, basename and the daemon options) and `information_loss`, the information a PLINK 1 view of the store cannot carry: `dosage`, `phase`, `multiallelic_sites`, `fam_pedigree_sex_phenotype` and `genetic_map`.

The pipeline's own `genotypes/<cohort_id>/<cohort_id>.genotype_view.json` records the same token as `source.identity` `{"mode": "declared", "value": "biofuse:<hex>"}`, so the two records join on `genotype_view_id`. The pipeline record describes what the pipeline did with the supplied PLINK 1 bundle, which is nothing lossy; the launcher record describes what the view itself dropped.

## View identity

```text
genotype_view_id = "biofuse:" + sha256(canonical JSON of the preimage)
```

The canonical form is JSON with sorted keys, no whitespace and UTF-8 text unescaped. The identity answers one question — would this mount serve the same bytes as another one — so everything that changes the served bytes is in the preimage and nothing else is:

| Preimage field | Content                                                                                                                                                      |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `schema`       | `nf-core/gwas/biofuse-view-identity/1`                                                                                                                       |
| `backend`      | `{"family": "biofuse", "view": "mount-plink"}`                                                                                                               |
| `source`       | `kind` `vcz`, the store's `vcf_zarr_version` and writer (its `source` attribute), and `revision` `{mode, value}`                                             |
| `runtime`      | the `biofuse` and `vcztools` versions, which decide the encoded bytes                                                                                        |
| `projection`   | `max_alleles` (always `2`), every selection option as given (`null` when absent), `samples_file_sha256`, `force_samples`, and `contigs` `{policy, excluded}` |

The store revision has two modes. By default it is `sha256`: a digest over every file in the store, visited in sorted relative-path order, each contributing its relative path, size and content digest, so the value depends on names and bytes only and not on timestamps, ownership or permissions. That reads the whole store once per launch. `--source-revision TOKEN` declares the revision instead, with mode `declared`, for a store versioned outside the launcher; the token is then your promise that it never names different content, and nothing checks it.

`--autosomes-only` records the policy `autosomes` and the contigs it excluded rather than the target list it expands to, because that list is a function of the store and already covered by the revision.

Left out on purpose, because none of them changes a served byte: the basename, the readahead and log options, the mountpoint and state root, the host, the cohort metadata (`cohort_id`, `genome_build`, `ancestry`), and the `pyfuse3`, `libfuse3`, `zarr` and `numcodecs` versions. They are recorded in the view record instead. Two consequences follow. Renaming the basename or moving the state root never moves the identity. And because options enter the preimage as the text you gave, two spellings of one selection — samples listed in a different order, say — produce two identities; that can only cost reuse, never share a result between different views.

`--identity` decides what the pipeline is told, not what is computed. In `declared` mode, the default, the manifest carries `biofuse:<hex>`, the pipeline trusts it and never reads the trio to identify the cohort, and every reuse key downstream is built from it. In `digest` mode the cell is empty, so the pipeline's `PREPARE_GENOTYPE_SOURCE` task reads the whole trio through the mount and keys the cohort by the `sha256` of its bytes. That costs one full encoding pass and exists for parity checks: a declared run and a digest run of one view should produce the same results.

## Constraints

- **Apptainer or Singularity consumers only.** The mount is made without `allow_other`, so only the user who made it can read it. Rootful Docker cannot read it, and `--privileged` does not change that. Use `-profile apptainer` or `-profile singularity`; their automounts make the mounted paths visible inside task containers. Choose a state root that those containers can reach.
- **linux-64 only.** `pyfuse3` is published on conda-forge for x86-64 Linux only, so the launcher cannot be installed on arm64; the pipeline's `arm64` profile does not extend to mounted cohorts.
- **One host, one cohort.** A FUSE mount is visible only on the host that made it, so the pipeline and every task must run there: use the `single_node` profile inside one allocation. The launcher refuses to start when `SLURM_JOB_NUM_NODES` is greater than 1. The generated manifest declares exactly one cohort, so every row of the analysis manifest must name that `cohort_id`.
- **Autosomal, biallelic hard calls.** The view serves autosomes 1-22 (named `1`-`22` or `chr1`-`chr22`); a store with variants on any other contig is refused unless `--autosomes-only` is given, and one with no autosomal variants is refused outright. Multiallelic sites are always skipped. The BED holds hard calls only: dosage and phase are not carried.
- **Reconstructed placeholders.** A VCF Zarr store has no pedigree, sex, phenotype or genetic map. FAM columns 3-6 (father, mother, sex, phenotype) and the BIM centimorgan column are placeholders written by the encoder, not data. Supply sex and phenotypes to the pipeline through its phenotype and covariate files, never through the FAM.

## Recovering from a wedged mount

A mount is wedged when its kernel connection has outstanding requests that nothing will answer, typically after the daemon died without the kernel dropping the mount. Every process that touches it — `ls`, `stat`, `df`, a shell completing a path, a task — enters uninterruptible sleep (`D` state).

1. **Do not touch the mountpoint, and do not try to kill the stuck readers.** A process in `D` state ignores `SIGKILL`; killing more processes only adds to the pile. Do not run `ls`, `stat`, `mountpoint` or `df` on the mount or its parent directory to diagnose it.
2. **Stop the launcher cleanly if it is still alive.** Send `SIGTERM` to the launcher process. Its teardown caps every unmount attempt, falls back to a lazy detach, and aborts the kernel connection when the mount is still present.
3. **Otherwise reap it.** `nf-core-gwas-biofuse reap-stale --state-root <state_root>` tears down every view whose launcher is gone, including the connection abort.
4. **By hand, if the launcher environment is unavailable.** Find the mount in `/proc/self/mountinfo`, which never blocks. The third field is `0:<minor>`; write to that connection's abort file, then detach lazily:

   ```bash
   grep ' <state_root>/nf-core-gwas-biofuse/view-' /proc/self/mountinfo
   echo 1 > /sys/fs/fuse/connections/<minor>/abort
   fusermount3 -uz <state_root>/nf-core-gwas-biofuse/view-<16 hex>/mnt
   ```

   Aborting the connection makes every outstanding and future request fail with `ENOTCONN`, which releases the stuck readers so they exit. Then run `reap-stale`, or remove the view directory once it no longer appears in `/proc/self/mountinfo`.

`/sys/fs/fuse/connections/<minor>/waiting` shows the number of unanswered requests. For a hang in a live daemon, `kill -USR1 <daemon pid>` writes every daemon thread's Python stack to `logs/daemon.log`; add `--keep-state` to keep that log after the run.

## Single-node Slurm run

The pipeline, the launcher and every task share one allocation on one node: the launcher mounts the view on that node, and the [`single_node` profile](../usage.md#-profile) runs every task there with the local executor, sized from the allocation. The template below leaves every site value as a placeholder:

```bash
#!/bin/bash
#SBATCH --job-name=gwas-biofuse
#SBATCH --partition=<partition>
#SBATCH --account=<account>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=<cpus>
#SBATCH --mem=<mem>
#SBATCH --time=<time>
#SBATCH --output=%x-%j.log

set -euo pipefail

# <module lines: load Apptainer and Nextflow as the site provides them>
# <activate the launcher environment, for example: conda activate <launcher_env>>

export NXF_APPTAINER_CACHEDIR='<apptainer_image_cache>'
STATE_ROOT='<node_local_state_root>'
RECORD_DIR='<record_dir>'
WORK_DIR='<work_dir>'

nf-core-gwas-biofuse run \
    --vcz '<cohort.vcz>' \
    --cohort-id '<cohort_id>' --genome-build '<GRCh37|GRCh38>' --ancestry '<ancestry>' \
    --autosomes-only \
    --state-root "${STATE_ROOT}" \
    --record-dir "${RECORD_DIR}" \
    --readahead-buffer-size 64M \
    --verbose \
    -- nextflow run '<pipeline>' \
        -profile single_node,apptainer \
        -work-dir "${WORK_DIR}" \
        --cohort_manifest '{cohort_manifest}' \
        --analysis_manifest '<analysis_manifest.csv>' \
        --outdir '<outdir>'
```

Give `--mem` an explicit size: under `--mem=0` the profile cannot read the allocation's memory and falls back to 8 GB. Put the state root on storage local to the node, such as the job's temporary directory, and leave `--autosomes-only` out only when the store carries autosomes alone. When Slurm ends the job early, through `scancel` or the time limit, it signals every process in the job, the mount daemon included, and kills what is left after its kill-wait interval. The mount does not outlive that: the daemon runs with the `auto_unmount` mount option, so the kernel drops the mount when the daemon exits, however it exits. If a view directory is left under a state root that outlives the job, run `reap-stale` on that node.

## Evidence checklist for the validation run

The first run on a production Slurm cluster is the remaining acceptance step of [lyh970817/gwas#8](https://github.com/lyh970817/gwas/issues/8). Run the template above once, run the same analyses once more as a control on a materialised trio, and post the following as a dated comment on that issue:

- [ ] The launcher's log, run with `--verbose`: the full view identity, `mount ready in … at … (view …)`, `view record: …`, `cohort manifest: …` and `command exited with status 0`.
- [ ] The view record `<cohort_id>.biofuse_view.json`, with `runtime.libfuse3.version` at 3.18.3 or later.
- [ ] Nextflow's `pipeline_info/execution_trace_*.txt`, showing every task `COMPLETED` and no `PREPARE_GENOTYPE_SOURCE` task for the declared cohort.
- [ ] `genotypes/<cohort_id>/<cohort_id>.genotype_view.json`, whose `source.identity.value` equals the record's `genotype_view_id`.
- [ ] A result comparison against the control: the same analysis manifest run on the trio materialised from the same store with `vcztools view-plink`, the same selection options and `--max-alleles 2`, declared through an ordinary cohort manifest. Report the trio `sha256` sums on both sides and whether each published native result is byte-identical; name any file that differs only by timestamps, such as the MultiQC report. Materialise the trio with the following command, where `-o` takes the output stem verbatim and writes `<stem>.bed`, `<stem>.bim` and `<stem>.fam`; for a run with `--autosomes-only`, pass `--targets` with the store's own names for autosomes 1-22, which is what the launcher gives the daemon:

  ```bash
  vcztools view-plink --max-alleles 2 [same selection options] -o '<stem>' '<cohort.vcz>'
  ```

- [ ] The versions around the run: `apptainer --version`, `uname -r` and the `libfuse3` path and version from the record, plus the Nextflow version and pipeline revision.

## Tests

The launcher's own tests live in `launcher/biofuse/tests/`, outside nf-test discovery and CI. Run the local test tier from the repository root with the fixture bundle materialised; without `GWAS_TEST_FIXTURES` it exits 0 printing `SKIPPED`:

```bash
GWAS_TEST_FIXTURES=$(tests/fixtures/materialize.sh) launcher/biofuse/tests/run_local_tier.sh
```
