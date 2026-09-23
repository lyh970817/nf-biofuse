# SPDX-License-Identifier: Apache-2.0
"""The lifecycle owner behind the nf-biofuse Nextflow plugin.

The supervisor runs on the host, started by the plugin before Nextflow validates or stages anything. It
mounts one VCF Zarr store as a BioFuse PLINK 1 view, declares the view's content-addressed identity, writes
the manifest the pipeline reads, and tears the mount down when the run ends, however it ends.
"""

__version__ = "0.1.0"
