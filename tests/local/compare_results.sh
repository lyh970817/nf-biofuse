#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Compare an nf-core/gwas run through a BioFuse view with a control run on the fixture trio. Each argument
# is a run directory holding out/ (the --outdir) and trace.txt (the -with-trace file); a plugin run also
# holds <basename>.biofuse_view.json, the view record.
#
#   - every REGENIE .regenie.gz is byte-identical;
#   - every GWASLab harmonised .gwaslab.tsv.gz is identical once decompressed (gzip stamps its header);
#   - digest mode: genotype_view.json is byte-identical, since the pipeline digested the same bytes under
#     the same member names;
#     declared mode: genotype_view.json records the identity the plugin declared, biofuse:<64 hex>, and
#     no members, because the pipeline never opened the trio;
#   - every task completed, and the run executed the control's tasks, less PREPARE_GENOTYPE_SOURCE in
#     declared mode, which is the one task a declared identity skips.
#
# Usage: compare_results.sh CONTROL_RUN CANDIDATE_RUN digest|declared
set -u

if [ "$#" -ne 3 ] || { [ "$3" != digest ] && [ "$3" != declared ]; }; then
    echo "usage: $0 CONTROL_RUN CANDIDATE_RUN digest|declared" >&2
    exit 2
fi
control=$1 candidate=$2 mode=$3
status=0

check() { # $1 = label, $2 = 0 for pass
    if [ "$2" -eq 0 ]; then echo "  PASS  $1"; else
        echo "  FAIL  $1"
        status=1
    fi
}

# Relative paths of published files matching a pattern, sorted.
published() { # $1 = run, $2 = find -name pattern
    (cd "$1/out" 2>/dev/null && find . -type f -name "$2" | sort)
}

# 0 when both runs published the same non-empty set of files matching a pattern; SHARED holds the list.
same_set() { # $1 = pattern, $2 = what
    local left right
    left=$(published "$control" "$1")
    right=$(published "$candidate" "$1")
    SHARED=$left
    [ -n "$left" ] && [ "$left" = "$right" ]
    check "$2: the same $(printf '%s\n' "$left" | grep -c .) file(s) published by both" $?
    [ -n "$left" ] && [ "$left" = "$right" ]
}

declared_identity() { # $1 = candidate genotype_view.json, $2 = the plugin's view record
    python3 - "$1" "$2" <<'PY'
import json
import re
import sys

view = json.load(open(sys.argv[1]))
identity = view["source"]["identity"]
assert identity["mode"] == "declared", identity
assert re.fullmatch(r"biofuse:[0-9a-f]{64}", identity["value"]), identity
assert view["source"]["members"] is None, view["source"]
record = json.load(open(sys.argv[2]))
assert record["identity_mode"] == "declared", record["identity_mode"]
assert record["declared_genotype_view_id"] == record["genotype_view_id"] == identity["value"], record
print(f"    declared {identity['value']}")
PY
}

if same_set '*.regenie.gz' 'REGENIE results'; then
    for relative in $SHARED; do
        cmp -s "$control/out/$relative" "$candidate/out/$relative"
        check "byte-identical ${relative#./}" $?
    done
fi

if same_set '*.gwaslab.tsv.gz' 'GWASLab harmonised tables'; then
    for relative in $SHARED; do
        cmp -s <(gzip -dc "$control/out/$relative") <(gzip -dc "$candidate/out/$relative")
        check "identical after decompression ${relative#./}" $?
    done
fi

if same_set '*.genotype_view.json' 'genotype view records'; then
    for relative in $SHARED; do
        if [ "$mode" = digest ]; then
            cmp -s "$control/out/$relative" "$candidate/out/$relative"
            check "byte-identical ${relative#./}" $?
        else
            declared_identity "$candidate/out/$relative" "$(find "$candidate" -maxdepth 1 -name '*.biofuse_view.json' | head -n 1)"
            check "${relative#./} records the plugin's declared biofuse identity" $?
        fi
    done
fi

# Process names and statuses from a trace, one "NAME STATUS" per task.
tasks() { # $1 = run
    awk -F'\t' '
        NR == 1 { for (i = 1; i <= NF; i++) column[$i] = i; next }
        {
            name = $column["name"]
            sub(/ \(.*$/, "", name)
            sub(/^.*:/, "", name)
            print name, $column["status"]
        }' "$1/trace.txt" | sort
}

control_tasks=$(tasks "$control")
candidate_tasks=$(tasks "$candidate")
! printf '%s\n' "$control_tasks" "$candidate_tasks" | grep -qv ' COMPLETED$'
check "every task completed ($(printf '%s\n' "$candidate_tasks" | grep -c .) vs $(printf '%s\n' "$control_tasks" | grep -c .) in the control)" $?
printf '%s\n' "$control_tasks" | grep -q '^PREPARE_GENOTYPE_SOURCE '
check "the control digested its trio in PREPARE_GENOTYPE_SOURCE" $?
expected=$control_tasks
[ "$mode" = declared ] && expected=$(printf '%s\n' "$control_tasks" | grep -v '^PREPARE_GENOTYPE_SOURCE ')
[ -n "$candidate_tasks" ] && [ "$candidate_tasks" = "$expected" ]
same_tasks=$?
if [ "$mode" = declared ]; then
    check "the control's tasks less PREPARE_GENOTYPE_SOURCE" "$same_tasks"
else
    check "the control's tasks, PREPARE_GENOTYPE_SOURCE included" "$same_tasks"
fi
if [ "$status" -ne 0 ]; then
    diff <(printf '%s\n' "$expected") <(printf '%s\n' "$candidate_tasks") | sed 's/^/        /'
fi
exit "$status"
