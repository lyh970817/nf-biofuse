# Written by the nf-core/gwas team and released under the MIT license.
"""The identity contract: canonical form, what moves the id and what must not."""

import hashlib
import re

import pytest

from conftest import info, parse_run, plan
from nf_core_gwas_biofuse import cli, identity, store

VIEW_ID = re.compile(r"^biofuse:[0-9a-f]{64}$")


def _preimage(**projection):
    return identity.build_preimage(
        vcf_zarr_version="0.5",
        writer="bio2zarr-0.2.1",
        revision_mode="sha256",
        revision_value="ab" * 32,
        biofuse_version="0.1.0",
        vcztools_version="0.2.0",
        projection=identity.build_projection(**projection),
    )


def test_canonical_json_sorts_keys_strips_whitespace_and_keeps_utf8():
    value = {"b": 1, "a": {"d": [1, 2], "c": "ü"}, "n": None, "t": True}
    assert identity.canonical_json(value) == '{"a":{"c":"ü","d":[1,2]},"b":1,"n":null,"t":true}'.encode("utf-8")


def test_canonical_json_refuses_nan():
    with pytest.raises(ValueError):
        identity.canonical_json({"x": float("nan")})


def test_view_id_is_namespaced_sha256_of_the_canonical_preimage():
    preimage = _preimage()
    view_id = identity.view_id(preimage)
    assert VIEW_ID.match(view_id)
    assert view_id == "biofuse:" + hashlib.sha256(identity.canonical_json(preimage)).hexdigest()
    assert identity.short_view_id(view_id) == view_id[len("biofuse:") : len("biofuse:") + 16]


def test_preimage_shape_and_pinned_projection():
    preimage = identity.build_preimage(
        vcf_zarr_version="0.5",
        writer="bio2zarr-0.2.1",
        revision_mode="declared",
        revision_value="release-3",
        biofuse_version="0.1.0",
        vcztools_version="0.2.0",
        projection=identity.build_projection(samples="S1,S2", contig_policy="autosomes", excluded_contigs=("X",)),
    )
    assert preimage == {
        "schema": "nf-core/gwas/biofuse-view-identity/1",
        "backend": {"family": "biofuse", "view": "mount-plink"},
        "source": {
            "kind": "vcz",
            "vcf_zarr_version": "0.5",
            "writer": "bio2zarr-0.2.1",
            "revision": {"mode": "declared", "value": "release-3"},
        },
        "runtime": {"biofuse": "0.1.0", "vcztools": "0.2.0"},
        "projection": {
            "max_alleles": 2,
            "min_alleles": None,
            "regions": None,
            "targets": None,
            "samples": "S1,S2",
            "samples_file_sha256": None,
            "include": None,
            "exclude": None,
            "types": None,
            "exclude_types": None,
            "force_samples": False,
            "contigs": {"policy": "autosomes", "excluded": ["X"]},
        },
    }


def test_golden_identity_pins_the_canonical_form():
    """A change to the preimage layout or the canonical serialisation moves every declared id; this
    pins the exact bytes and value so such a change is a deliberate one."""
    preimage = _preimage()
    assert identity.canonical_json(preimage) == (
        '{"backend":{"family":"biofuse","view":"mount-plink"},'
        '"projection":{"contigs":{"excluded":[],"policy":"all"},"exclude":null,"exclude_types":null,'
        '"force_samples":false,"include":null,"max_alleles":2,"min_alleles":null,"regions":null,'
        '"samples":null,"samples_file_sha256":null,"targets":null,"types":null},'
        '"runtime":{"biofuse":"0.1.0","vcztools":"0.2.0"},'
        '"schema":"nf-core/gwas/biofuse-view-identity/1",'
        '"source":{"kind":"vcz","revision":{"mode":"sha256","value":"' + "ab" * 32 + '"},'
        '"vcf_zarr_version":"0.5","writer":"bio2zarr-0.2.1"}}'
    ).encode("utf-8")
    assert identity.view_id(preimage) == "biofuse:635ddb2c04ba432a3077eb9a3b6719815331b3865841002bac98ec2a031d733f"


def test_invalid_modes_are_refused():
    with pytest.raises(ValueError):
        identity.build_projection(contig_policy="somatic")
    with pytest.raises(ValueError):
        identity.build_preimage(
            vcf_zarr_version="0.5",
            writer=None,
            revision_mode="mtime",
            revision_value="1",
            biofuse_version="0.1.0",
            vcztools_version="0.2.0",
            projection=identity.build_projection(),
        )


# ---------------------------------------------------------------------------------------------------
# Through the launcher's own plan: what is excluded and what is not
# ---------------------------------------------------------------------------------------------------


@pytest.mark.usefixtures("pinned_versions")
def test_operational_options_do_not_move_the_id():
    baseline = plan(parse_run())
    for extra in (
        ["--readahead-buffer-size", "16M"],
        ["--readahead-workers", "4"],
        ["--basename", "other_stem"],
        ["--log-level", "DEBUG"],
        ["--state-root", "/somewhere/else"],
        ["--record-dir", "/elsewhere"],
        ["--identity", "digest"],
        ["--keep-state"],
        ["--readiness-timeout", "5"],
    ):
        assert plan(parse_run(*extra)).genotype_view_id == baseline.genotype_view_id, extra
    # Cohort metadata is not in the identity either: the same bytes under another cohort keep the id.
    other_cohort = cli.build_parser().parse_args(
        [
            "run",
            "--vcz",
            "cohort.vcz",
            "--cohort-id",
            "cohort_b",
            "--genome-build",
            "GRCh38",
            "--ancestry",
            "AFR",
            "--",
            "true",
        ]
    )
    assert plan(other_cohort).genotype_view_id == baseline.genotype_view_id
    # Nor is the store's location: the same content elsewhere is the same view.
    assert plan(parse_run(), info(path="/elsewhere/renamed.vcz")).genotype_view_id == baseline.genotype_view_id


@pytest.mark.usefixtures("pinned_versions")
def test_selection_options_move_the_id():
    baseline = plan(parse_run())
    seen = {baseline.genotype_view_id}
    for extra in (
        ["--samples", "S001,S002"],
        ["--samples", "S002,S001"],
        ["--min-alleles", "2"],
        ["--regions", "1:1-1000"],
        ["--targets", "2"],
        ["--include", "QUAL>30"],
        ["--exclude", "QUAL<10"],
        ["--types", "snps"],
        ["--exclude-types", "indels"],
        ["--force-samples"],
    ):
        view_id = plan(parse_run(*extra)).genotype_view_id
        assert view_id not in seen, extra
        seen.add(view_id)


@pytest.mark.usefixtures("pinned_versions")
def test_max_alleles_is_always_requested_and_in_the_preimage():
    current = plan(parse_run())
    assert current.preimage["projection"]["max_alleles"] == 2
    assert current.daemon_options[:2] == ["--max-alleles", "2"]
    moved = dict(current.preimage, projection=dict(current.preimage["projection"], max_alleles=3))
    assert identity.view_id(moved) != current.genotype_view_id


@pytest.mark.usefixtures("pinned_versions")
def test_daemon_options_pass_only_what_was_given():
    # biofuse's own defaults (log level, readahead) are not restated on its command line.
    assert plan(parse_run()).daemon_options == ["--max-alleles", "2"]
    given = plan(
        parse_run("--log-level", "DEBUG", "--readahead-buffer-size", "16M", "--samples", "S1", "--force-samples")
    )
    assert given.daemon_options == [
        "--max-alleles",
        "2",
        "--samples",
        "S1",
        "--readahead-buffer-size",
        "16M",
        "--log-level",
        "DEBUG",
        "--force-samples",
    ]


@pytest.mark.usefixtures("pinned_versions")
def test_store_revision_and_encoder_versions_move_the_id(monkeypatch):
    baseline = plan(parse_run())
    assert plan(parse_run(), revision=("sha256", "cd" * 32)).genotype_view_id != baseline.genotype_view_id
    assert plan(parse_run(), revision=("declared", "v1")).genotype_view_id != baseline.genotype_view_id
    assert plan(parse_run(), info(writer="bio2zarr-0.3.0")).genotype_view_id != baseline.genotype_view_id
    assert plan(parse_run(), info(vcf_zarr_version="0.6")).genotype_view_id != baseline.genotype_view_id
    monkeypatch.setattr(cli, "_required_version", lambda package: {"biofuse": "0.2.0", "vcztools": "0.2.0"}[package])
    assert plan(parse_run()).genotype_view_id != baseline.genotype_view_id


@pytest.mark.usefixtures("pinned_versions")
def test_samples_file_enters_by_content_not_by_path(tmp_path):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("S001\nS002\n")
    second.write_text("S001\nS002\n")
    by_first = plan(parse_run("--samples-file", str(first)))
    assert by_first.genotype_view_id == plan(parse_run("--samples-file", str(second))).genotype_view_id
    assert by_first.preimage["projection"]["samples_file_sha256"] == hashlib.sha256(b"S001\nS002\n").hexdigest()
    second.write_text("S001\n")
    assert by_first.genotype_view_id != plan(parse_run("--samples-file", str(second))).genotype_view_id


@pytest.mark.usefixtures("pinned_versions")
def test_non_autosomal_store_needs_autosomes_only():
    mixed = info(contig_ids=("chr1", "chr2", "chrX", "chrM"), variant_contig_ids=("chr1", "chr2", "chrX"))
    with pytest.raises(cli.UsageError, match="chrX"):
        plan(parse_run(), mixed)
    restricted = plan(parse_run("--autosomes-only"), mixed)
    # The daemon is given the autosomes in the store's own naming; the preimage records the policy.
    assert restricted.daemon_targets == "chr1,chr2"
    assert restricted.daemon_options[restricted.daemon_options.index("--targets") + 1] == "chr1,chr2"
    assert restricted.preimage["projection"]["targets"] is None
    assert restricted.preimage["projection"]["contigs"] == {"policy": "autosomes", "excluded": ["chrX"]}
    # The policy itself is part of the identity.
    assert restricted.genotype_view_id != plan(parse_run("--autosomes-only")).genotype_view_id


@pytest.mark.usefixtures("pinned_versions")
def test_autosomal_store_serves_all_contigs_by_default():
    default = plan(parse_run())
    assert default.daemon_targets is None
    assert "--targets" not in default.daemon_options
    assert default.preimage["projection"]["contigs"] == {"policy": "all", "excluded": []}


@pytest.mark.usefixtures("pinned_versions")
def test_autosomes_only_cannot_combine_with_targets():
    with pytest.raises(cli.UsageError, match="--targets"):
        plan(parse_run("--autosomes-only", "--targets", "1"))


@pytest.mark.usefixtures("pinned_versions")
def test_store_without_autosomal_variants_is_refused():
    with pytest.raises(cli.UsageError, match="no variants on an autosome"):
        plan(parse_run(), info(contig_ids=("X", "Y"), variant_contig_ids=("X",)))


def test_revision_default_is_the_store_content_digest(store_path):
    assert cli.resolve_revision(parse_run(), store_path, cli.Log()) == ("sha256", store.content_digest(store_path))
    declared = parse_run("--source-revision", "release-3")
    assert cli.resolve_revision(declared, store_path, cli.Log()) == ("declared", "release-3")
    with pytest.raises(cli.UsageError, match="blank"):
        cli.resolve_revision(parse_run("--source-revision", " "), store_path, cli.Log())
