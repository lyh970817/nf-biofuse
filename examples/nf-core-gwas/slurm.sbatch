#!/bin/bash
# nf-core/gwas on one Slurm node, with the cohort served by nf-biofuse.
#
# A FUSE mount is visible only on the host that made it, so Nextflow, the supervisor and every task share
# one node of one allocation: the single_node profile runs every task there with the local executor,
# sized from the allocation. Replace every <...> with the site's value; biofuse.config and
# cohort.biofuse.template.csv are the ones beside this file, copied into the launch directory.
#
#SBATCH --job-name=gwas-biofuse
#SBATCH --partition=<partition>
#SBATCH --account=<account>
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=<cpus>
#SBATCH --mem=<memory, for example 64G; give an explicit size, since single_node reads it>
#SBATCH --time=<time limit>
#SBATCH --output=%x-%j.log

set -euo pipefail

# <module lines: load Apptainer (or Singularity) and Nextflow as the site provides them>

export NXF_APPTAINER_CACHEDIR='<apptainer image cache>'

# The mount lives under biofuse.stateRoot; keep it on storage local to this node, for example by setting
# `stateRoot = env('TMPDIR')` in biofuse.config when Slurm gives the job a node-local TMPDIR.
nextflow run nf-core/gwas -r '<revision>' \
    -profile single_node,apptainer \
    -c biofuse.config \
    -work-dir '<work directory>' \
    --cohort_manifest cohort.biofuse.csv \
    --analysis_manifest '<analysis manifest>' \
    --outdir '<results directory>'

# When Slurm ends the job early (scancel, time limit) it signals every process of the job, the mount
# daemon included; the daemon's auto_unmount makes the kernel drop the mount. If a view directory is left
# under a state root that outlives the job, run on that node:
#   <supervisor environment>/bin/nf-biofuse-supervisor reap-stale --state-root '<state root>'
