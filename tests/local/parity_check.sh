#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Parity of a mounted BioFuse view with the fixture PLINK 1 trio it was converted from, read the way a
# pipeline reads it: the consumer pipeline runs with the plugin serving VCZ, and its tasks copy the three
# members out and run plink2 in the pipeline's PLINK 2 image on the staged trio. Afterwards:
#   - the mounted .bed, .bim and .fam are byte-identical to the fixture's example_all.{bed,bim,fam};
#   - FAM columns 1-2 match the fixture in order and columns 3-6 are all `0 0 0 -9`, the placeholders a
#     PLINK 1 view reconstructs (the record declares them as information loss);
#   - BIM columns 1, 2, 4, 5 and 6 match and every centimorgan position is 0;
#   - `plink2 --missing --freq` writes identical results from the mount and from the fixture.
# The column checks restate what byte identity already implies; they name the column when it fails.
#
# Usage: GWAS_TEST_FIXTURES=... GWAS_PIPELINE=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR] parity_check.sh
# Exits 0 with "SKIPPED: <reason>" when the host cannot run it.
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

require_fuse_host
require_store_tools
[ -n "${PLINK2_SIF:-}" ] || require_pipeline
work=$(work_dir parity)
ensure_vcz "$work/store"
image=$(plink2_image "$work/images")
state_root="$work/state"
run="$work/run"
rm -rf "$run"
mkdir -p "$state_root" "$run"
baseline=$(fuse_connections)
fixture=$(fixture_genotypes)
status=0

check() { # $1 = label, $2 = 0 for pass
    if [ "$2" -eq 0 ]; then echo "  PASS  $1"; else
        echo "  FAIL  $1"
        status=1
    fi
}

echo "store: $VCZ"
echo "fixture: $fixture"
echo "plink2 image: $image"
printf 'id,bed,bim,fam\ncohort,{bed},{bim},{fam}\n' >"$run/manifest.template.csv"
write_plugin_config "$run/biofuse.config" "$state_root" "$run/cohort.biofuse.csv" "$run/manifest.template.csv" declared ""
(cd "$run" && nextflow -log "$run/nextflow.log" run "$TESTS_DIR/consumer" -c "$run/biofuse.config" \
    --manifest "$run/cohort.biofuse.csv" --mode parity --plink2_image "$image" --outdir "$run/out" \
    -work-dir "$run/work" -ansi-log false) >"$run/stdout" 2>&1
check "the consumer pipeline ran through the plugin (nextflow rc=$?)" $?
grep -h 'nf-biofuse: view' "$run/stdout"

for extension in bed bim fam; do
    cmp -s "$run/out/mounted.$extension" "$fixture/example_all.$extension"
    check ".$extension byte-identical to the fixture ($(wc -c <"$fixture/example_all.$extension") bytes)" $?
done
cmp -s <(cut -f1,2 "$run/out/mounted.fam") <(cut -f1,2 "$fixture/example_all.fam")
check "FAM columns 1-2 equal, in order" $?
awk -F'\t' 'NF != 6 || $3 != "0" || $4 != "0" || $5 != "0" || $6 != "-9" { bad = 1 } END { exit bad }' \
    "$run/out/mounted.fam"
check "FAM columns 3-6 all 0 0 0 -9 ($(wc -l <"$run/out/mounted.fam") samples)" $?
cmp -s <(cut -f1,2,4,5,6 "$run/out/mounted.bim") <(cut -f1,2,4,5,6 "$fixture/example_all.bim")
check "BIM columns 1,2,4,5,6 equal" $?
awk -F'\t' '$3 != "0" { bad = 1 } END { exit bad }' "$run/out/mounted.bim"
check "BIM centimorgan column all 0 ($(wc -l <"$run/out/mounted.bim") variants)" $?

mkdir -p "$work/plink2"
plink2_in_image "$image" "$fixture" "$work/plink2" -- \
    --bfile "$fixture/example_all" --missing --freq --out "$work/plink2/fixture" >"$work/plink2/fixture.stdout" 2>&1
check "plink2 --missing --freq on the fixture" $?
for result in smiss vmiss afreq; do
    [ -s "$work/plink2/fixture.$result" ] && cmp -s "$run/out/plink2_mounted.$result" "$work/plink2/fixture.$result"
    check "plink2 .$result identical ($(wc -l <"$work/plink2/fixture.$result" 2>/dev/null || echo 0) lines)" $?
done

assert_released "$baseline" "$state_root" "the parity run" || status=1
[ ! -e "$run/cohort.biofuse.csv" ]
check "the manifest is gone after the run" $?
if [ "$status" -eq 0 ]; then echo "PARITY: PASS"; else echo "PARITY: FAIL (run under $run)"; fi
exit "$status"
