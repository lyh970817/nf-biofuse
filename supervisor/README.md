# nf-biofuse-supervisor

The lifecycle owner behind the [nf-biofuse](../README.md) Nextflow plugin. The plugin starts it before a run; it
mounts one [VCF Zarr](https://github.com/sgkit-dev/vcf-zarr-spec) store as a [BioFuse](https://github.com/sgkit-dev/biofuse)
PLINK 1 view, waits until the virtual `.bed`/`.bim`/`.fam` are readable, writes the view record and the manifest
rendered from a template, reports readiness, and tears the mount down when it is stopped or when the Nextflow
process it watches is gone, including after a `SIGKILL` of either.

It was ported from the nf-core/gwas host-side launcher (`launcher/biofuse` at lyh970817/gwas commit `fa44e58`),
keeping its mount lifecycle, view ownership, SIGKILL watchdog, stale reclaim and view identity unchanged. What
changed: it no longer wraps a command (the plugin runs inside Nextflow instead), and it writes any manifest from a
template instead of the nf-core/gwas cohort manifest.

## Environment

```bash
conda env create -f supervisor/environment.yml -p /path/to/env
/path/to/env/bin/nf-biofuse-supervisor --version
```

The environment pins the encoder versions that enter the view identity and the `libfuse3` floor the mount daemon
requires; see [docs/operations.md](../docs/operations.md). `pyfuse3` is x86-64 only on conda-forge.

## Commands

- `nf-biofuse-supervisor serve --vcz STORE --manifest-template FILE --manifest FILE [options]` mounts the view,
  publishes the manifest and serves until `SIGTERM`, `SIGINT`, `SIGHUP` or `SIGQUIT`, or until `--watch-pid` is gone.
  The plugin passes `--watch-pid` (its JVM) and `--ready-file`.
- `nf-biofuse-supervisor identity --vcz STORE [selection options]` prints the view identity and its preimage without
  mounting anything.
- `nf-biofuse-supervisor reap-stale [--state-root DIR]` tears down views whose supervisor is gone.

## Tests

The pure tier needs no mount:

```bash
pip install -e 'supervisor[test]'   # or use the environment above plus pytest
cd supervisor && pytest
```

The local FUSE tier, which drives Nextflow with the plugin against real mounts, lives in
[`tests/local`](../tests/local) at the repository root.
