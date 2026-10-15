# nf-biofuse

## Summary

`nf-biofuse` serves a [VCF Zarr](https://github.com/sgkit-dev/vcf-zarr-spec) store to a Nextflow run as a PLINK 1
`.bed`/`.bim`/`.fam` fileset, mounted by [BioFuse](https://github.com/sgkit-dev/biofuse) instead of written to disk.
The pipeline sees ordinary PLINK 1 files and needs no change.

When the run starts, before the pipeline validates or stages any input, the plugin mounts the view on the host,
waits until the virtual trio is readable, and writes the pipeline's own input manifest from a template you
provide, with the mounted paths and a content-addressed identity of the view filled in. When the run ends, on
success, failure, Ctrl-C, `SIGTERM` or even `SIGKILL`, the mount is torn down and the manifest removed.

- **Pipeline-agnostic.** The manifest is your pipeline's samplesheet with placeholders (`{bed}`, `{bim}`, `{fam}`,
  `{prefix}`, `{view_id}`, ...), so any pipeline that reads a PLINK 1 trio from a samplesheet can consume the view.
  An [nf-core/gwas](https://github.com/nf-core/gwas) example is included.
- **Content-addressed identity.** `{view_id}` is `biofuse:` followed by the SHA-256 of everything that decides the
  served bytes: the store's content (or a revision you declare), the BioFuse and vcztools versions and every
  selection option. A pipeline that accepts a declared identity, such as nf-core/gwas through `genotype_view_id`,
  can then skip hashing the trio, which through the mount would cost a full decode. The same view always mounts
  at the same path, so `-resume` caches as usual.
- **Safe lifecycle.** A host process, the supervisor, owns the mount: exclusive ownership per view, a watchdog
  that tears down after a `SIGKILL`, `auto_unmount`, capped unmounts with a lazy detach and a FUSE connection
  abort as the last resort, and a `reap-stale` command for anything a crash leaves behind.
- **A record of the view.** Each run writes `<basename>.biofuse_view.json`: the identity and its preimage, every
  runtime version (including the `libfuse3` the mount daemon loaded), the mount readiness time, and the
  information a PLINK 1 view cannot carry (dosage, phase, multiallelic sites, pedigree/sex/phenotype, genetic map).

Requirements: Nextflow 25.10.0 or later; a Linux x86-64 host with `/dev/fuse` and a setuid `fusermount3`; tasks
that run on that host (local executor, for example one Slurm node) in Apptainer or Singularity, or without a
container; and the supervisor environment built from [`supervisor/environment.yml`](supervisor/environment.yml).
Rootful Docker tasks cannot read the mount.

## Get Started

1. Create the supervisor environment from a checkout of this repository at the release tag. It pins BioFuse
   0.1.0, vcztools 0.2.0 and `libfuse3>=3.18.3` from conda-forge (older libfuse builds can hang on `stat`) and
   installs the `nf-biofuse-supervisor` command:

   ```bash
   git clone --branch v0.1.0 https://github.com/lyh970817/nf-biofuse.git
   conda env create -f nf-biofuse/supervisor/environment.yml -p /opt/nf-biofuse-env
   ```

2. Write a manifest template: your pipeline's samplesheet, with placeholders where the mounted view goes.

   ```csv
   cohort_id,genome_build,ancestry,pgen,psam,pvar,bed,bim,fam,genotype_view_id
   cohort_a,GRCh38,EUR,,,,{bed},{bim},{fam},{view_id}
   ```

3. Enable the plugin and configure it in a config file, `biofuse.config`:

   ```groovy
   plugins {
       id 'nf-schema@2.8.0'      // the pipeline's own plugins, see the note below
       id 'nf-biofuse@0.1.0'
   }

   biofuse {
       vcz              = '/data/cohort_a.vcz'
       manifestTemplate = 'cohort.biofuse.template.csv'
       manifest         = 'cohort.biofuse.csv'
       supervisor       = '/opt/nf-biofuse-env/bin/nf-biofuse-supervisor'
   }
   ```

   Nextflow takes the plugin list from the last config file that declares one, or from `-plugins`, and does not
   add it to the pipeline's own list. Repeat the pipeline's plugins, with their versions, next to `nf-biofuse`,
   or the pipeline's plugins are loaded unpinned or not at all. `nextflow config <pipeline> | grep -A3 plugins`
   shows them.

4. Run the pipeline with that config, pointing its manifest parameter at the file the plugin writes:

   ```bash
   nextflow run nf-core/gwas -r <revision> -profile apptainer -c biofuse.config \
       --cohort_manifest cohort.biofuse.csv --analysis_manifest analyses.csv --outdir results
   ```

   The console shows `nf-biofuse: view biofuse:<hex> (declared) ready in 0.8s at ...` before the pipeline starts.
   The supervisor's log is `.nf-biofuse.log` in the launch directory.

The manifest must be a file the workflow reads, such as a samplesheet parameter. Files Nextflow reads while it
launches, such as a `-params-file` or a config, are read before any plugin starts.

## Examples

### nf-core/gwas

[`examples/nf-core-gwas`](examples/nf-core-gwas) holds a cohort manifest template and a config to copy into the
launch directory. nf-core/gwas takes
the declared identity in its cohort manifest's optional `genotype_view_id` column, records it in
`genotypes/<cohort_id>/<cohort_id>.genotype_view.json`, and skips digesting the trio. Every row of the analysis
manifest must name the one cohort the template declares.

```bash
cp examples/nf-core-gwas/biofuse.config examples/nf-core-gwas/cohort.biofuse.template.csv .
nextflow run nf-core/gwas -r <revision> -profile apptainer -c biofuse.config \
    --cohort_manifest cohort.biofuse.csv --analysis_manifest analyses.csv --outdir results
```

To have the pipeline digest the trio through the mount instead, as a parity check against a declared run, set
`identity = 'digest'`: `{view_id}` is then left empty.

### A pipeline that takes a PLINK prefix

Any text format works. For a samplesheet with one `--bfile`-style prefix column:

```text
sample_set	bfile
cohort_a	{prefix}
```

### Selecting what the view serves

The selection options are those of `vcztools view-plink`; they enter the view identity. The view always
skips multiallelic sites, and a store with variants outside autosomes 1-22 needs `autosomesOnly`:

```groovy
biofuse {
    vcz                 = '/data/biobank.vcz'
    manifestTemplate    = 'samplesheet.template.csv'
    manifest            = 'samplesheet.csv'
    autosomesOnly       = true
    samplesFile         = 'unrelated_eur.txt'
    sourceRevision      = 'biobank-release-7'   // declare the store revision instead of digesting it
    readaheadBufferSize = '64M'                 // serving only; does not enter the identity
}
```

### One Slurm node

A FUSE mount is visible only on the host that made it, so the pipeline and all its tasks share one node. This
job runs nf-core/gwas with its `single_node` profile (local executor sized from the allocation):

```bash
#!/bin/bash
#SBATCH --job-name=gwas-biofuse
#SBATCH --partition=<partition>
#SBATCH --account=<account>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=<cpus>
#SBATCH --mem=<memory, e.g. 64G>
#SBATCH --time=<time limit>
#SBATCH --output=%x-%j.log
set -euo pipefail

# Load Apptainer and Nextflow the way the site provides them.
export NXF_APPTAINER_CACHEDIR=<apptainer image cache>

nextflow run nf-core/gwas -r <revision> -profile single_node,apptainer \
    -c biofuse.config \
    --cohort_manifest cohort.biofuse.csv --analysis_manifest <analysis manifest> --outdir <results>
```

with `stateRoot = env('TMPDIR')` (node-local storage) in the `biofuse` scope. The full template is
[`examples/nf-core-gwas/slurm.sbatch`](examples/nf-core-gwas/slurm.sbatch).

### Configuration reference

| Option | Default | Meaning |
|---|---|---|
| `enabled` | `true` when `vcz` is set | Turn the plugin off without removing the scope. |
| `vcz` | required | The VCF Zarr store, Zarr format 2 or 3. |
| `manifestTemplate` | required | The manifest template (placeholders below). |
| `manifest` | required | Where the rendered manifest is written. An existing file there is refused unless it is this view's own manifest. |
| `basename` | store name without `.vcz` | File stem of the virtual trio. |
| `identity` | `declared` | `declared` fills `{view_id}`; `digest` leaves it empty. |
| `stateRoot` | `$XDG_RUNTIME_DIR`, `$TMPDIR` or `/tmp` | Where the mountpoint and view state live; must be visible to the task containers. |
| `recordDir` | launch directory | Where `<basename>.biofuse_view.json` is written. |
| `autosomesOnly`, `minAlleles`, `regions`, `targets`, `samples`, `samplesFile`, `forceSamples`, `include`, `exclude`, `types`, `excludeTypes` | unset | `vcztools view-plink` selection; enters the identity. |
| `sourceRevision` | content digest | Declared store revision for an externally versioned store. The default digest reads the whole store at every launch, so set this for large stores. |
| `readaheadBufferSize`, `readaheadWorkers`, `logLevel` | BioFuse defaults | How the view is served; not in the identity. |
| `readinessTimeout` | `120s` | How long the trio may take to become readable. |
| `keepState`, `verbose` | `false` | Keep the view's state directory and logs; log more. |
| `supervisor` | `nf-biofuse-supervisor` on `PATH` | The supervisor executable. |

Placeholders: `{bed}`, `{bim}`, `{fam}` (member paths), `{prefix}` (`<mount>/<basename>`), `{mount}`,
`{basename}`, `{view_id}` (the identity in declared mode, empty in digest mode). Any other `{name}` is refused.

The view identity contract, the constraints, recovery from a wedged mount and the local test tier are
documented in [`docs/operations.md`](docs/operations.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
