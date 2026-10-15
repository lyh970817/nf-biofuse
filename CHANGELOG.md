# Changelog

## 0.1.0 (2026-10-15)

First version. The supervisor is ported from the nf-core/gwas host-side BioFuse launcher
(`launcher/biofuse` at lyh970817/gwas commit `fa44e58`); the import commit keeps its files verbatim, so the
history shows every change made for the plugin.

### Added

- The `biofuse` config scope, declared as a Nextflow config scope so strict config validation accepts it. The
  plugin is enabled when `biofuse.vcz` is set.
- A trace observer that, when the run is created and before the pipeline script runs, starts the supervisor
  and blocks until the view is mounted, readable and its manifest written; and that stops the supervisor when
  the run completes. A JVM shutdown hook covers exits that skip the observer.
- `nf-biofuse-supervisor serve`: mounts the view, writes the view record and the manifest rendered from a
  template, reports readiness in a JSON file, and serves until `SIGTERM`/`SIGINT`/`SIGHUP`/`SIGQUIT` or until the
  Nextflow JVM it watches is gone. It leaves the JVM's session so a terminal Ctrl-C or hangup never reaches it.
- Manifest templates with the placeholders `{bed}`, `{bim}`, `{fam}`, `{prefix}`, `{mount}`, `{basename}` and
  `{view_id}`; unknown placeholders are refused, and an existing file at the manifest path is never overwritten
  unless it is this view's own manifest.
- `published.json` in the view state: the manifest and the readiness report are registered with their digests
  and removed first by every teardown path (the supervisor's own, the watchdog's, `reap-stale` and a stale
  reclaim), unless their content changed since.
- An nf-core/gwas example (cohort manifest template, config, single-node Slurm job) and a validation pipeline.
- Plugin unit tests (Spock), and a local FUSE tier that drives Nextflow with the installed plugin: lifecycle
  matrix, trio parity, nf-core/gwas end-to-end parity with `-resume`, a concurrency ladder and a benchmark.

### Kept from the launcher

- Mount lifecycle: `/proc/self/mountinfo`-only state checks, `auto_unmount` through the in-process `pyfuse3.init`
  patch, capped `fusermount3 -u`, then an unconditional `-uz`, then a FUSE connection abort.
- Exclusive `flock` view ownership, the detached SIGKILL watchdog, stale reclaim and `reap-stale`.
- The view identity: preimage, canonical form, schema string `nf-biofuse/view-identity/1` and the golden identity.
- The supervisor environment: conda-forge Python 3.12, `pyfuse3` 3.4.1, `libfuse3>=3.18.3`; PyPI `biofuse` 0.1.0,
  `vcztools` 0.2.0, `bio2zarr` 0.2.1, `zarr` 3.4.0, `numcodecs` 0.17.0.

### Changed from the launcher

- The launcher wrapped `nextflow run` (`run -- COMMAND`); the supervisor is started by the plugin inside the run
  (`serve`), so the command wrapping, its process group and signal forwarding are gone.
- The nf-core/gwas cohort manifest (and its restated schema checks) is replaced by a user template, which makes
  the plugin independent of any pipeline's input format.
- The state directory is `<state-root>/nf-biofuse`; the record is `<basename>.biofuse_view.json` with schema
  `nf-biofuse/view-record/1`, which drops `cohort_id` and adds the manifest path and template.
- The view identity's schema string is `nf-biofuse/view-identity/1` instead of
  `nf-core/gwas/biofuse-view-identity/1`, so a store's `genotype_view_id` differs from the one the launcher declared.
- Exit status `0` for a clean teardown whatever ended the serve, instead of the wrapped command's status.
- Licensed Apache-2.0 (the plugin template's licence) instead of MIT.
