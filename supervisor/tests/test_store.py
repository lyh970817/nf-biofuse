# SPDX-License-Identifier: Apache-2.0
"""Store validation, the contig census and the content digest."""

import os
import time

import pytest

from conftest import make_store
from nf_biofuse_supervisor import store


@pytest.mark.parametrize("zarr_format", [2, 3])
def test_root_validation_accepts_both_zarr_formats(tmp_path, zarr_format):
    path = make_store(tmp_path / "cohort.vcz", zarr_format=zarr_format)
    assert store.validate_root(path) == zarr_format
    assert store.read_store(path).zarr_format == zarr_format


def test_root_validation_refuses_non_stores(tmp_path):
    with pytest.raises(store.StoreError, match="not found"):
        store.validate_root(str(tmp_path / "missing.vcz"))
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "data.txt").write_text("x")
    with pytest.raises(store.StoreError, match="not a Zarr"):
        store.validate_root(str(plain))
    (plain / "zarr.json").write_text('{"zarr_format": 3, "node_type": "array"}')
    with pytest.raises(store.StoreError, match="group"):
        store.validate_root(str(plain))


def test_read_store_reads_attributes_and_contig_census(tmp_path):
    path = make_store(
        tmp_path / "cohort.vcz",
        contigs=("chr1", "chr2", "chrX", "chrM"),
        variant_contigs=(0, 0, 0, 2, 2, 0, 0),
        attrs={
            "vcf_zarr_version": "0.5",
            "source": "bio2zarr-0.2.1",
            "vcf_meta_information": [["fileformat", "VCFv4.3"]],
        },
    )
    info = store.read_store(path)
    assert info.vcf_zarr_version == "0.5"
    assert info.writer == "bio2zarr-0.2.1"
    assert info.contig_ids == ("chr1", "chr2", "chrX", "chrM")
    assert info.variant_contig_ids == ("chr1", "chrX")
    assert store.contig_census(info) == (("chr1",), ("chrX",))
    assert store.declared_autosomes(info) == ("chr1", "chr2")


def test_read_store_requires_the_vcz_arrays(tmp_path):
    import zarr

    zarr.create_group(str(tmp_path / "empty.vcz"), zarr_format=2)
    with pytest.raises(store.StoreError, match="contig_id"):
        store.read_store(str(tmp_path / "empty.vcz"))


@pytest.mark.parametrize(
    "contig, expected",
    [
        ("1", "1"),
        ("22", "22"),
        ("chr1", "1"),
        ("chr22", "22"),
        ("X", None),
        ("chrX", None),
        ("MT", None),
        ("chrM", None),
        ("23", None),
        ("0", None),
        ("chr", None),
    ],
)
def test_autosome_classification(contig, expected):
    assert store.autosome_number(contig) == expected
    assert store.is_autosome(contig) is (expected is not None)


def _write_tree(root, files):
    for relative, content in files.items():
        full = os.path.join(root, relative)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as handle:
            handle.write(content)


def test_content_digest_is_a_function_of_names_sizes_and_bytes_only(tmp_path):
    files = {".zgroup": b'{"zarr_format": 2}', "a/0": b"\x00\x01", "a/.zarray": b"{}", "b/0.0": b"xyz"}
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_tree(first, files)
    _write_tree(second, files)
    assert store.content_digest(str(first)) == store.content_digest(str(second))
    assert len(store.content_digest(str(first))) == 64

    # mtime, ownership and permission bits do not enter it.
    os.utime(second / "a" / "0", (0, 0))
    os.chmod(second / "b" / "0.0", 0o600)
    time.sleep(0.01)
    assert store.content_digest(str(first)) == store.content_digest(str(second))

    # One byte, a name or a size does.
    changed = tmp_path / "changed"
    _write_tree(changed, dict(files, **{"a/0": b"\x00\x02"}))
    assert store.content_digest(str(changed)) != store.content_digest(str(first))
    renamed = tmp_path / "renamed"
    _write_tree(renamed, {("a/1" if key == "a/0" else key): value for key, value in files.items()})
    assert store.content_digest(str(renamed)) != store.content_digest(str(first))
    grown = tmp_path / "grown"
    _write_tree(grown, dict(files, **{"b/0.0": b"xyz\n"}))
    assert store.content_digest(str(grown)) != store.content_digest(str(first))


def test_content_digest_lines_follow_the_documented_form(tmp_path):
    import hashlib

    root = tmp_path / "store"
    _write_tree(root, {"b": b"2", "a/x": b"11"})
    expected = hashlib.sha256()
    for relative, content in (("a/x", b"11"), ("b", b"2")):
        expected.update(f"{relative}\0{len(content)}\0{hashlib.sha256(content).hexdigest()}\n".encode("utf-8"))
    assert store.content_digest(str(root)) == expected.hexdigest()


def test_content_digest_of_a_real_store_is_stable_across_reads(store_path):
    assert store.content_digest(store_path) == store.content_digest(store_path)
