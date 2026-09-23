#!/usr/bin/env bash
# Written by the nf-core/gwas team and released under the MIT license.
#
# Parity of a mounted BioFuse view with the fixture PLINK 1 trio it was converted from. The store is
# built from the fixture's example_all.vcf.gz (unless VCZ names one), mounted through the launcher, and
# checked from inside the launched command:
#   - the mounted .bed, .bim and .fam are byte-identical to the fixture's example_all.{bed,bim,fam};
#   - FAM columns 1-2 match the fixture in order and columns 3-6 are all `0 0 0 -9`, the placeholders a
#     PLINK 1 view reconstructs (the record declares them as information loss);
#   - BIM columns 1, 2, 4, 5 and 6 match and every centimorgan position is 0;
#   - `plink2 --missing --freq`, run in the pipeline's PLINK 2 image, writes identical results from both.
# The column checks restate what byte identity already implies; they name the column when it fails.
#
# Usage: GWAS_TEST_FIXTURES=... [VCZ=store.vcz] [LOCAL_TIER_ROOT=DIR] parity_check.sh
# Exits 0 with "SKIPPED: <reason>" when the host cannot run it.
set -u
# shellcheck source=local_tier_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/local_tier_lib.sh"

# ---- Inside the launched command: BIOFUSE_MOUNT and BIOFUSE_BASENAME are set by the launcher ----
if [ "${1:-}" = "--inside" ]; then
    work=$2 image=$3
    fixture=$(fixture_genotypes)
    mounted="$BIOFUSE_MOUNT/$BIOFUSE_BASENAME"
    status=0
    check() { # $1 = label, $2 = 0 for pass
        if [ "$2" -eq 0 ]; then echo "  PASS  $1"; else
            echo "  FAIL  $1"
            status=1
        fi
    }

    # Copy each member out under a deadline, then compare the copies.
    for extension in bed bim fam; do
        timeout "$READ_TIMEOUT" cat "$mounted.$extension" >"$work/mounted.$extension"
        check "read mounted .$extension" $?
        cmp -s "$work/mounted.$extension" "$fixture/$BASENAME.$extension"
        check ".$extension byte-identical to the fixture ($(wc -c <"$fixture/$BASENAME.$extension") bytes)" $?
    done

    cmp -s <(cut -f1,2 "$work/mounted.fam") <(cut -f1,2 "$fixture/$BASENAME.fam")
    check "FAM columns 1-2 equal, in order" $?
    awk -F'\t' 'NF != 6 || $3 != "0" || $4 != "0" || $5 != "0" || $6 != "-9" { bad = 1 } END { exit bad }' \
        "$work/mounted.fam"
    check "FAM columns 3-6 all 0 0 0 -9 ($(wc -l <"$work/mounted.fam") samples)" $?
    cmp -s <(cut -f1,2,4,5,6 "$work/mounted.bim") <(cut -f1,2,4,5,6 "$fixture/$BASENAME.bim")
    check "BIM columns 1,2,4,5,6 equal" $?
    awk -F'\t' '$3 != "0" { bad = 1 } END { exit bad }' "$work/mounted.bim"
    check "BIM centimorgan column all 0 ($(wc -l <"$work/mounted.bim") variants)" $?

    mkdir -p "$work/plink2"
    plink2_in_image "$image" "$BIOFUSE_MOUNT" "$work/plink2" -- \
        --bfile "$mounted" --missing --freq --out "$work/plink2/mounted" >"$work/plink2/mounted.stdout" 2>&1
    check "plink2 --missing --freq on the mount" $?
    plink2_in_image "$image" "$fixture" "$work/plink2" -- \
        --bfile "$fixture/$BASENAME" --missing --freq --out "$work/plink2/fixture" >"$work/plink2/fixture.stdout" 2>&1
    check "plink2 --missing --freq on the fixture" $?
    for result in smiss vmiss afreq; do
        [ -s "$work/plink2/fixture.$result" ] && cmp -s "$work/plink2/mounted.$result" "$work/plink2/fixture.$result"
        check "plink2 .$result identical ($(wc -l <"$work/plink2/fixture.$result" 2>/dev/null || echo 0) lines)" $?
    done
    exit "$status"
fi

require_fuse_host
require_store_tools
work=$(work_dir parity)
ensure_vcz "$work/store"
image=$(plink2_image "$work/images")
state_root="$work/state"
mkdir -p "$state_root"
baseline=$(fuse_connections)

echo "store: $VCZ"
echo "fixture: $(fixture_genotypes)"
echo "plink2 image: $image"
launch_view "$state_root" "$work" declared -- bash "${BASH_SOURCE[0]}" --inside "$work" "$image" \
    >"$work/launcher.log" 2>&1
rc=$?
grep -v '^\[nf-core-gwas-biofuse\]' "$work/launcher.log"
grep -h 'mount ready' "$work/launcher.log"
assert_released "$baseline" "$state_root" "the parity check" || rc=1
if [ "$rc" -eq 0 ]; then
    echo "PARITY: PASS"
else
    echo "PARITY: FAIL (status $rc; launcher log $work/launcher.log)"
fi
exit "$rc"
