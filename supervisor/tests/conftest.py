# Written by the nf-core/gwas team and released under the MIT license.
import argparse
import os

import numpy as np
import pytest
import zarr

from nf_core_gwas_biofuse import cli, store

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
COHORT_SCHEMA = os.path.join(REPO_ROOT, "assets", "schema_cohort_manifest.json")
DEFAULT_ATTRS = {"vcf_zarr_version": "0.5", "source": "bio2zarr-0.2.1"}


def make_store(path, *, contigs=("1", "2"), variant_contigs=(0, 0, 1, 1), zarr_format=2, attrs=None):
    """A minimal VCF Zarr store: the root attributes, the contig names and the per-variant contig index,
    which is everything the launcher reads."""
    group = zarr.create_group(str(path), zarr_format=zarr_format)
    group.attrs.update(DEFAULT_ATTRS if attrs is None else attrs)
    names = np.array(list(contigs))
    contig_id = group.create_array("contig_id", shape=names.shape, dtype=names.dtype)
    contig_id[:] = names
    indexes = np.array(list(variant_contigs), dtype=np.int8)
    variant_contig = group.create_array("variant_contig", shape=indexes.shape, dtype=indexes.dtype, chunks=(2,))
    variant_contig[:] = indexes
    return str(path)


@pytest.fixture
def store_path(tmp_path):
    return make_store(tmp_path / "cohort.vcz")


@pytest.fixture
def store_info(store_path):
    return store.read_store(store_path)


@pytest.fixture
def pinned_versions(monkeypatch):
    monkeypatch.setattr(cli, "_required_version", lambda package: {"biofuse": "0.1.0", "vcztools": "0.2.0"}[package])


def parse_run(*extra):
    """Parsed ``run`` arguments with a fixed cohort and a placeholder command."""
    parser = cli.build_parser()
    return parser.parse_args(
        [
            "run",
            "--vcz",
            "cohort.vcz",
            "--cohort-id",
            "cohort_a",
            "--genome-build",
            "GRCh37",
            "--ancestry",
            "EUR",
            *extra,
            "--",
            "true",
        ]
    )


def info(**overrides) -> store.StoreInfo:
    fields = dict(
        path="/data/cohort.vcz",
        zarr_format=2,
        vcf_zarr_version="0.5",
        writer="bio2zarr-0.2.1",
        contig_ids=("1", "2"),
        variant_contig_ids=("1", "2"),
    )
    fields.update(overrides)
    return store.StoreInfo(**fields)


REVISION = ("sha256", "ab" * 32)


def plan(args: argparse.Namespace, store_info=None, revision=REVISION) -> cli.ViewPlan:
    return cli.resolve_view(args, store_info or info(), revision)
