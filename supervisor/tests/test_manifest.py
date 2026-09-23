# Written by the nf-core/gwas team and released under the MIT license.
"""The temporary cohort manifest against the pipeline's own schema."""

import csv
import io
import json
import os
import stat

import pytest

from conftest import COHORT_SCHEMA
from nf_core_gwas_biofuse import manifest

TRIO = {
    "bed": "/run/user/1000/nf-core-gwas-biofuse/view-0123456789abcdef/mnt/cohort.bed",
    "bim": "/run/user/1000/nf-core-gwas-biofuse/view-0123456789abcdef/mnt/cohort.bim",
    "fam": "/run/user/1000/nf-core-gwas-biofuse/view-0123456789abcdef/mnt/cohort.fam",
}
VIEW_ID = "biofuse:" + "ab" * 32


def render(**overrides):
    fields = dict(cohort_id="cohort_a", genome_build="GRCh37", ancestry="EUR", genotype_view_id=VIEW_ID, **TRIO)
    fields.update(overrides)
    return manifest.render(**fields)


def rows(text):
    """The rows the way nf-schema reads them: empty cells are absent, not empty strings."""
    reader = csv.DictReader(io.StringIO(text))
    return [{key: value for key, value in row.items() if value != ""} for row in reader]


def test_header_is_exactly_the_schema_columns():
    text = render()
    assert text.splitlines()[0] == "cohort_id,genome_build,ancestry,pgen,psam,pvar,bed,bim,fam,genotype_view_id"
    assert text.count("\n") == 2


@pytest.mark.parametrize("declared", [VIEW_ID, None])
def test_row_validates_against_the_cohort_manifest_schema(declared):
    jsonschema = pytest.importorskip("jsonschema")
    with open(COHORT_SCHEMA, encoding="utf-8") as handle:
        schema = json.load(handle)
    assert schema["items"]["additionalProperties"] is False
    parsed = rows(render(genotype_view_id=declared))
    # The schema's `exists` keyword is nf-schema's own and is not asserted here; the paths name a mount
    # that only exists while a launcher runs.
    jsonschema.Draft202012Validator(schema).validate(parsed)
    assert parsed[0]["bed"] == TRIO["bed"]
    assert "pgen" not in parsed[0]
    if declared is None:
        assert "genotype_view_id" not in parsed[0]
    else:
        assert parsed[0]["genotype_view_id"] == declared


def test_only_the_plink1_group_and_the_identity_are_populated():
    (row,) = rows(render())
    assert set(row) == {"cohort_id", "genome_build", "ancestry", "bed", "bim", "fam", "genotype_view_id"}


def test_write_is_owner_readable_only(tmp_path):
    path = tmp_path / "cohort_manifest.csv"
    path.write_text("stale\n")
    os.chmod(path, 0o644)
    manifest.write(str(path), render())
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text() == render()


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"cohort_id": "cohort a"}, "cohort_id"),
        ({"cohort_id": ""}, "cohort_id"),
        ({"genome_build": "hg19"}, "genome_build"),
        ({"ancestry": "_EUR"}, "ancestry"),
        ({"ancestry": "EU R"}, "ancestry"),
        ({"bed": "/mnt/cohort.bim"}, "bed"),
        ({"fam": "/mnt/co hort.fam"}, "fam"),
        ({"genotype_view_id": "BIOFUSE:" + "ab" * 32}, "genotype_view_id"),
        ({"genotype_view_id": "abc"}, "genotype_view_id"),
    ],
)
def test_rows_the_schema_would_reject_are_refused_first(overrides, message):
    with pytest.raises(manifest.ManifestError, match=message):
        render(**overrides)
