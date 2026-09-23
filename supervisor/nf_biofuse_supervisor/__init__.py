# Written by the nf-core/gwas team and released under the MIT license.
"""Host-side BioFuse launcher for nf-core/gwas.

The launcher runs on the host before Nextflow starts. It mounts one VCF Zarr cohort as a PLINK 1 view,
declares the view's content-addressed identity to the pipeline through a temporary cohort manifest, runs
the supplied command, and tears the mount down on every exit path.
"""

__version__ = "0.1.0"
