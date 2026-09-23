# nf-core-gwas-biofuse

Host-side launcher that serves a VCF Zarr cohort to nf-core/gwas as a [BioFuse](https://github.com/sgkit-dev/biofuse)
PLINK 1 view. It runs on the host before Nextflow starts: it mounts one cohort, waits until the virtual
`.bed`/`.bim`/`.fam` are readable, writes a temporary cohort manifest that declares the view's content-addressed
`genotype_view_id`, runs the command you give it, and tears the mount down on every exit path, including a `SIGKILL`
of the launcher itself.

## Environment

The launcher is installed into the conda environment it needs anyway; `environment.yml` pins the encoder versions
that enter the view identity and the `libfuse3` floor the mount daemon requires.

```bash
conda env create -f launcher/biofuse/environment.yml -p /path/to/env
```

`pyfuse3` is x86-64 only on conda-forge. The mount is readable by Apptainer or Singularity tasks but not by rootful
Docker, and it is visible on the launching host only, so the launcher refuses a multi-node Slurm allocation.

## Invocation

```bash
nf-core-gwas-biofuse run \
    --vcz /data/cohort.vcz --cohort-id cohort_a --genome-build GRCh38 --ancestry EUR \
    --autosomes-only --readahead-buffer-size 64M \
    -- nextflow run nf-core/gwas -profile apptainer \
         --cohort_manifest '{cohort_manifest}' --analysis_manifest analysis_manifest.csv --outdir results
```

`{cohort_manifest}` is replaced by the generated manifest path, and the command's exit status is the launcher's. The
view always excludes multiallelic sites (`--max-alleles 2`), and a store with variants on any contig other than
autosomes 1-22 needs `--autosomes-only`. Cancel a backgrounded launcher with `SIGTERM`.

Other subcommands:

- `nf-core-gwas-biofuse identity --vcz ...` prints the identity and its preimage without mounting anything.
- `nf-core-gwas-biofuse reap-stale [--state-root DIR]` tears down views whose launcher is gone.

## View identity

`genotype_view_id` is `biofuse:` followed by the SHA-256 of a canonical JSON preimage: the store's content digest (or
the `--source-revision` token you declare for an externally versioned store), its VCF Zarr version and writer, the
`biofuse` and `vcztools` versions, and every selection option. Options that change only how the view is served, such
as the basename, readahead and log settings, the mountpoint and the cohort metadata, are left out, so changing them
never moves the identity. `nf_core_gwas_biofuse/identity.py` documents the exact preimage.

Each run writes `<cohort_id>.biofuse_view.json` to `--record-dir` (default: the current directory) with the preimage,
the identity, every runtime version including the `libfuse3` the daemon loaded, the readiness time and the
information a PLINK 1 view cannot carry. `--identity digest` leaves the manifest's `genotype_view_id` empty so the
pipeline digests the trio through the mount instead.

## Tests

`pip install -e 'launcher/biofuse[test]'`, then `pytest launcher/biofuse/tests` for the pure tier.

The local FUSE tier runs on a host with `/dev/fuse`, this environment, Apptainer and Nextflow, and never in CI:

```bash
GWAS_TEST_FIXTURES=$(tests/fixtures/materialize.sh) launcher/biofuse/tests/run_local_tier.sh
```

It runs five steps one after another and prints PASS or FAIL for each: `lifecycle_matrix.sh` (every way a run
ends leaves no mount, process or wedged connection), `parity_check.sh` (the mounted trio is byte-identical to the
fixture's `example_all.{bed,bim,fam}` and `plink2 --missing --freq` agrees on both), `e2e_parity.sh`
(`-profile test,apptainer` through the view in declared and digest mode against a run on the fixture trio,
compared by `compare_results.sh`), `concurrency_ladder.sh` (1, 2, 4 and 8 concurrent `plink2 --freq` readers of
one mount) and `benchmark.sh` (mount readiness, E2E wall time, daemon peak RSS and I/O amplification, printed as a
table). It exits 0 with `SKIPPED: <reason>` when `/dev/fuse`, the launcher, `biofuse`, `apptainer`, `nextflow` or
`GWAS_TEST_FIXTURES` is missing. Each script also runs on its own. The variables it reads:

- `GWAS_TEST_FIXTURES`, the materialized fixture bundle (required).
- `VCZ`, a store converted from the fixture's `example_all.vcf.gz`. Without it the tier builds one with
  `vcf2zarr`, which needs `tabix` (htslib) on `PATH` and the `[test]` extra for bio2zarr's VCF reader.
- `LOCAL_TIER_ROOT`, where the mounts, runs and logs go (default: a new directory under `TMPDIR`).
- `NXF_APPTAINER_CACHEDIR`, passed to Nextflow and searched for the PLINK 2 image; `PLINK2_SIF` names that image
  directly.
- `STEPS`, a subset of `lifecycle parity e2e ladder benchmark`; `NXF_PROFILE`, `LEVELS` and `CONSUMER_TIMEOUT`
  as the scripts describe.
