# shellcheck shell=bash
# SPDX-License-Identifier: Apache-2.0
#
# Shared helpers for the local FUSE tier (run_local_tier.sh and the scripts it runs). Source it; do not
# execute it. Nothing here is CI: every script that sources it needs /dev/fuse, the supervisor
# environment, Nextflow with the plugin installed, and the nf-core/gwas fixture bundle, and says SKIPPED
# when the host lacks one of them.
#
# SAFETY. A FUSE mount whose daemon has gone blocks every process that touches it in uninterruptible
# sleep. Mount state is therefore read from /proc/mounts and /sys/fs/fuse/connections only; the few reads
# of a mounted file run under `timeout` or inside a pipeline task, and nothing here lists, stats or globs
# a mountpoint.
#
# Environment:
#   SUPERVISOR          the nf-biofuse-supervisor of an environment built from supervisor/environment.yml
#                       (default: the one on PATH)
#   NXF_PLUGINS_DIR     where `make install` put the plugin (Nextflow's default when unset)
#   GWAS_TEST_FIXTURES  the materialized nf-core/gwas fixture bundle: the fixture trio, the VCF the store is
#                       built from, and the test profile's analysis manifest
#   VCZ                 a VCF Zarr store built from the fixture's example_all.vcf.gz; built when unset
#   GWAS_PIPELINE       an nf-core/gwas checkout whose cohort manifest takes genotype_view_id; the E2E and
#                       benchmark steps run it, and the PLINK 2 image is read from its modules
#   GWAS_PLUGINS        the pipeline's own plugins, comma-separated (default: read from its nextflow.config);
#                       a -c config's plugins block replaces the pipeline's list, so it has to repeat them
#   PLINK2_SIF          the PLINK 2 image; otherwise the pipeline's own, from the Nextflow image cache
#                       (NXF_APPTAINER_CACHEDIR or NXF_SINGULARITY_CACHEDIR) or downloaded
#   LOCAL_TIER_ROOT     where each script puts its mounts, runs and outputs (default: a new temporary
#                       directory under TMPDIR)

TESTS_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd "$TESTS_DIR/../.." && pwd -P)
PLUGIN_VERSION=$(sed -n "s/^version = '\(.*\)'$/\1/p" "$REPO_ROOT/build.gradle")
PLUGIN_ID="nf-biofuse@$PLUGIN_VERSION"
SUPERVISOR=${SUPERVISOR:-$(command -v nf-biofuse-supervisor 2>/dev/null)}
# The fixture cohort, as its own manifest declares it; the basename makes the mounted trio carry the
# fixture's member names, so a digest of either is the same genotype view.
COHORT_ID=example_cohort
GENOME_BUILD=GRCh37
ANCESTRY=EUR
BASENAME=${BASENAME:-example_all}
READ_TIMEOUT=${READ_TIMEOUT:-300}

skip() {
    echo "SKIPPED: $*"
    exit 0
}

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# The gate every script shares. Missing prerequisites skip; they are not failures.
require_fuse_host() {
    [ -e /dev/fuse ] || skip "/dev/fuse is not present"
    [ -n "$SUPERVISOR" ] && [ -x "$SUPERVISOR" ] || skip "no nf-biofuse-supervisor (set SUPERVISOR or put it on PATH)"
    local tool
    for tool in nextflow apptainer python3; do
        command -v "$tool" >/dev/null 2>&1 || skip "$tool is not on PATH"
    done
    [ -d "${NXF_PLUGINS_DIR:-$HOME/.nextflow/plugins}/nf-biofuse-$PLUGIN_VERSION" ] ||
        skip "$PLUGIN_ID is not installed in ${NXF_PLUGINS_DIR:-$HOME/.nextflow/plugins}; run make install"
    [ -n "${GWAS_TEST_FIXTURES:-}" ] || skip "GWAS_TEST_FIXTURES is not set"
    fixture_genotypes >/dev/null || skip "no genotypes/example_all.bed under GWAS_TEST_FIXTURES=$GWAS_TEST_FIXTURES"
}

require_pipeline() {
    [ -n "${GWAS_PIPELINE:-}" ] || skip "GWAS_PIPELINE is not set"
    [ -f "$GWAS_PIPELINE/main.nf" ] || skip "GWAS_PIPELINE=$GWAS_PIPELINE has no main.nf"
}

# The store is built from the fixture VCF only when none was given; building needs tabix, because
# vcf2zarr reads a multi-contig VCF through its index, and vcf2zarr with bio2zarr's vcf extra.
require_store_tools() {
    [ -n "${VCZ:-}" ] && return 0
    command -v tabix >/dev/null 2>&1 || skip "VCZ is not set and tabix (htslib) is not on PATH to build it"
    [ -x "$(dirname "$SUPERVISOR")/vcf2zarr" ] || skip "VCZ is not set and vcf2zarr is not beside the supervisor to build it"
    "$(dirname "$SUPERVISOR")/python" -c 'import cyvcf2' 2>/dev/null ||
        skip "VCZ is not set and cyvcf2 is missing; install the supervisor's [test] extra to build it"
}

fixture_genotypes() {
    local directory
    for directory in "$GWAS_TEST_FIXTURES/results/fixtures/genotypes" "$GWAS_TEST_FIXTURES/genotypes"; do
        if [ -f "$directory/example_all.bed" ]; then
            echo "$directory"
            return 0
        fi
    done
    return 1
}

# A directory for one script's work: under LOCAL_TIER_ROOT when the caller gave one, else temporary.
work_dir() { # $1 = label
    if [ -n "${LOCAL_TIER_ROOT:-}" ]; then
        mkdir -p "$LOCAL_TIER_ROOT/$1"
        (cd "$LOCAL_TIER_ROOT/$1" && pwd -P)
    else
        mktemp -d "${TMPDIR:-/tmp}/nf-biofuse-$1.XXXXXX"
    fi
}

# Build a VCF Zarr store from the fixture VCF into $1 and print its path.
build_vcz() { # $1 = destination directory
    local destination=$1 vcf
    vcf="$(fixture_genotypes)/example_all.vcf.gz"
    [ -f "$vcf" ] || fail "no $vcf to build the store from"
    mkdir -p "$destination"
    rm -rf "$destination/cohort.vcz"
    cp "$vcf" "$destination/example_all.vcf.gz"
    tabix -f -p vcf "$destination/example_all.vcf.gz" || fail "tabix could not index $vcf"
    "$(dirname "$SUPERVISOR")/vcf2zarr" convert -Q "$destination/example_all.vcf.gz" "$destination/cohort.vcz" >&2 ||
        fail "vcf2zarr could not convert $vcf"
    echo "$destination/cohort.vcz"
}

# Set VCZ, building the store under $1 when the caller did not give one.
ensure_vcz() { # $1 = directory to build into
    if [ -z "${VCZ:-}" ]; then
        VCZ=$(build_vcz "$1") || exit 1
    fi
    [ -d "$VCZ" ] || fail "VCZ=$VCZ is not a directory"
    export VCZ
}

# The PLINK 2 image of the pipeline's own PLINK 2 modules, found the way Nextflow names it in its cache.
plink2_image() { # $1 = directory to download into when no cache holds it
    if [ -n "${PLINK2_SIF:-}" ]; then
        echo "$PLINK2_SIF"
        return 0
    fi
    [ -n "${GWAS_PIPELINE:-}" ] || fail "set PLINK2_SIF, or GWAS_PIPELINE to find the pipeline's PLINK 2 image"
    local url name directory
    url=$(sed -n "s|.*'\(https://[^']*\)'.*|\1|p" "$GWAS_PIPELINE/modules/local/plink2/makebed/main.nf" | head -n 1)
    [ -n "$url" ] || fail "no Apptainer image URL in $GWAS_PIPELINE/modules/local/plink2/makebed/main.nf"
    name="$(printf '%s' "${url#https://}" | tr '/:' '--').img"
    for directory in "${NXF_APPTAINER_CACHEDIR:-}" "${NXF_SINGULARITY_CACHEDIR:-}"; do
        if [ -n "$directory" ] && [ -f "$directory/$name" ]; then
            echo "$directory/$name"
            return 0
        fi
    done
    mkdir -p "$1"
    if [ ! -f "$1/$name" ]; then
        curl --fail --location --silent --show-error --output "$1/$name.part" "$url" ||
            fail "could not download $url"
        mv "$1/$name.part" "$1/$name"
    fi
    echo "$1/$name"
}

# plink2 in the pipeline's image, outside any pipeline. The directories it reads and writes are bound at
# the same paths.
plink2_in_image() { # $1 = image, $2.. = directories to bind, then --, then plink2 arguments
    local image=$1 binds=()
    shift
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do
        binds+=(--bind "$1:$1")
        shift
    done
    shift
    timeout "$READ_TIMEOUT" apptainer exec "${binds[@]}" "$image" plink2 "$@"
}

# ---- The plugin configuration ----

# The pipeline's own plugins, which a -c config has to repeat because Nextflow takes the plugin list from
# the last config that declares one.
pipeline_plugins() {
    if [ -n "${GWAS_PLUGINS:-}" ]; then
        echo "$GWAS_PLUGINS"
        return 0
    fi
    sed -n "s/^[[:space:]]*id[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" "$GWAS_PIPELINE/nextflow.config" | paste -sd, -
}

# Write a config that loads the plugin (and the listed plugins) and serves VCZ as the fixture cohort.
write_plugin_config() { # $1 = path, $2 = state root, $3 = manifest, $4 = template, $5 = identity, $6 = other plugins
    local plugin
    {
        echo "plugins {"
        for plugin in ${6//,/ } "$PLUGIN_ID"; do
            echo "    id '$plugin'"
        done
        echo "}"
        cat <<EOF
biofuse {
    vcz = '$VCZ'
    manifestTemplate = '$4'
    manifest = '$3'
    basename = '$BASENAME'
    identity = '$5'
    stateRoot = '$2'
    recordDir = '$(dirname "$3")'
    supervisor = '$SUPERVISOR'
    readaheadBufferSize = '64M'
    verbose = true
}
EOF
    } >"$1"
}

# The nf-core/gwas cohort manifest template for the fixture cohort.
write_gwas_template() { # $1 = path
    printf 'cohort_id,genome_build,ancestry,pgen,psam,pvar,bed,bim,fam,genotype_view_id\n%s,%s,%s,,,,{bed},{bim},{fam},{view_id}\n' \
        "$COHORT_ID" "$GENOME_BUILD" "$ANCESTRY" >"$1"
}

# The control's cohort manifest: the fixture cohort on the fixture's own PLINK 1 trio.
write_control_manifest() { # $1 = path
    local fixture
    fixture=$(fixture_genotypes)
    printf 'cohort_id,genome_build,ancestry,pgen,psam,pvar,bed,bim,fam\n%s,%s,%s,,,,%s,%s,%s\n' \
        "$COHORT_ID" "$GENOME_BUILD" "$ANCESTRY" \
        "$fixture/example_all.bed" "$fixture/example_all.bim" "$fixture/example_all.fam" >"$1"
}

# The nf-core/gwas test route for one run directory, into PIPELINE; the run directory is the launch
# directory, so the plugin's .nf-biofuse.log lands there.
pipeline_argv() { # $1 = run directory, $2 = cohort manifest, then extra nextflow options
    local run=$1 manifest=$2
    shift 2
    # shellcheck disable=SC2034 # read by the sourcing script
    PIPELINE=(nextflow -log "$run/nextflow.log" run "$GWAS_PIPELINE" -profile "${NXF_PROFILE:-test,apptainer}"
        --cohort_manifest "$manifest" --outdir "$run/out" -work-dir "$run/work" "$@")
}

# ---- Mount state, from /proc and /sys only ----

# Biofuse mounts under a state root.
mounts_under() { # $1 = state root
    awk -v root="$1/" '$1 == "biofuse" && index($2, root) == 1 { n++ } END { print n + 0 }' /proc/mounts
}

# Supervisors, daemons and watchdogs whose command line names a state root.
processes_under() { # $1 = state root
    local pid count=0 cmd
    for pid in $(pgrep -f 'nf_biofuse_supervisor\.(daemon|mount)|nf-biofuse-supervisor serve' 2>/dev/null); do
        cmd=" $(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null) "
        case "$cmd" in *" $1 "* | *"$1/"*) count=$((count + 1)) ;; esac
    done
    echo "$count"
}

# The FUSE connections that exist now, to tell the ones a script creates from the machine's own.
fuse_connections() {
    ls /sys/fs/fuse/connections 2>/dev/null | tr '\n' ' '
}

# Abort every connection created since $1 (a fuse_connections snapshot) that has requests outstanding
# and no daemon under $2 left to answer them; print how many were wedged.
abort_wedged() { # $1 = baseline, $2 = state root
    local connection minor waiting wedged=0
    for connection in /sys/fs/fuse/connections/*/; do
        [ -d "$connection" ] || continue
        minor=$(basename "$connection")
        case " $1 " in *" $minor "*) continue ;; esac
        waiting=$(cat "$connection/waiting" 2>/dev/null || echo 0)
        case "$waiting" in '' | *[!0-9]*) waiting=0 ;; esac
        if [ "$waiting" -gt 0 ] && [ "$(processes_under "$2")" -eq 0 ]; then
            echo "wedged fuse connection $minor ($waiting waiting); aborting it" >&2
            echo 1 >"$connection/abort" 2>/dev/null
            wedged=$((wedged + 1))
        fi
    done
    echo "$wedged"
}

# View state directories left under a state root (the empty view-<id>.lock files are kept by design).
state_dirs_under() { # $1 = state root
    local directory count=0
    # -d stats the view directory itself, never the mountpoint inside it.
    for directory in "$1"/nf-biofuse/view-*; do
        [ -d "$directory" ] && count=$((count + 1))
    done
    echo "$count"
}

# 0 when a state root holds no mount, no supervisor process, no view state and no wedged connection. A
# leftover mount is reaped so the next step starts clean, and still counts as a failure.
assert_released() { # $1 = baseline, $2 = state root, $3 = label
    local mounts processes states wedged
    sleep 1
    mounts=$(mounts_under "$2")
    processes=$(processes_under "$2")
    states=$(state_dirs_under "$2")
    wedged=$(abort_wedged "$1" "$2")
    if [ "$mounts" -eq 0 ] && [ "$processes" -eq 0 ] && [ "$states" -eq 0 ] && [ "$wedged" -eq 0 ]; then
        echo "  released after $3 (mounts=0 processes=0 state_dirs=0 wedged=0)"
        return 0
    fi
    echo "  LEAK after $3 (mounts=$mounts processes=$processes state_dirs=$states wedged=$wedged); reaping" >&2
    "$SUPERVISOR" reap-stale --state-root "$2" >&2
    return 1
}

# Wait until a file exists; 0 when it does, 1 after $2 seconds or when process $3 is gone.
wait_for_file() { # $1 = file, $2 = seconds, $3 = pid to watch (optional)
    local deadline=$((SECONDS + $2))
    while [ "$SECONDS" -lt "$deadline" ]; do
        [ -e "$1" ] && return 0
        if [ -n "${3:-}" ] && ! kill -0 "$3" 2>/dev/null; then
            return 1
        fi
        sleep 0.2
    done
    return 1
}

# Drop a directory tree's clean pages from the page cache, so the next read of it comes from storage.
# POSIX_FADV_DONTNEED needs no privilege for files the caller can open.
evict_page_cache() { # $@ = files or directories
    python3 - "$@" <<'PY'
import os
import sys

for root in sys.argv[1:]:
    paths = [root] if os.path.isfile(root) else [
        os.path.join(directory, name) for directory, _, names in os.walk(root) for name in names
    ]
    for path in paths:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(descriptor)
PY
}
