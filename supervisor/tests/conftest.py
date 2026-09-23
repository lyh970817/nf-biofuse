# SPDX-License-Identifier: Apache-2.0
import argparse

import numpy as np
import pytest
import zarr

from nf_biofuse_supervisor import cli, store

DEFAULT_ATTRS = {"vcf_zarr_version": "0.5", "source": "bio2zarr-0.2.1"}


def make_store(path, *, contigs=("1", "2"), variant_contigs=(0, 0, 1, 1), zarr_format=2, attrs=None):
    """A minimal VCF Zarr store: the root attributes, the contig names and the per-variant contig index,
    which is everything the supervisor reads."""
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


SERVE = ["serve", "--vcz", "cohort.vcz", "--manifest-template", "template.csv", "--manifest", "manifest.csv"]


def parse_serve(*extra):
    """Parsed ``serve`` arguments with a fixed store, template and manifest."""
    return cli.build_parser().parse_args([*SERVE, *extra])


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
