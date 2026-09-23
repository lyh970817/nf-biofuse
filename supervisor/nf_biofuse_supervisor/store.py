# Written by the nf-core/gwas team and released under the MIT license.
"""The VCF Zarr store as the launcher reads it: root validation, root attributes, the contig census the
autosome policy is decided on, and the content digest that is the store's default revision."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass

import numpy as np
import zarr

AUTOSOMES = frozenset(str(number) for number in range(1, 23))
CONTIG_PREFIX = "chr"
_HASH_BLOCK = 1 << 20


class StoreError(ValueError):
    """The path is not a store the launcher can serve."""


@dataclass(frozen=True)
class StoreInfo:
    path: str
    zarr_format: int
    vcf_zarr_version: str | None
    writer: str | None
    # Every contig the store declares, in store order.
    contig_ids: tuple[str, ...]
    # The contigs that carry at least one variant, in store order.
    variant_contig_ids: tuple[str, ...]


def validate_root(path: str) -> int:
    """Return the Zarr format (2 or 3) of the group at ``path`` or raise :class:`StoreError`.

    A v3 root carries ``zarr.json`` with ``node_type: group``; a v2 root carries ``.zgroup``. Only the
    root document is read here, so the check is cheap and never opens an array.
    """
    if not os.path.isdir(path):
        raise StoreError(f"VCZ store not found (or not a directory): {path}")
    v3_document = os.path.join(path, "zarr.json")
    if os.path.isfile(v3_document):
        with open(v3_document, encoding="utf-8") as handle:
            metadata = json.load(handle)
        if metadata.get("zarr_format") == 3 and metadata.get("node_type") == "group":
            return 3
        raise StoreError(f"{v3_document} is not a Zarr v3 group document")
    v2_document = os.path.join(path, ".zgroup")
    if os.path.isfile(v2_document):
        with open(v2_document, encoding="utf-8") as handle:
            metadata = json.load(handle)
        if metadata.get("zarr_format") == 2:
            return 2
        raise StoreError(f"{v2_document} is not a Zarr v2 group document")
    raise StoreError(f"{path} is not a Zarr v2 or v3 group root (no zarr.json or .zgroup)")


def read_store(path: str) -> StoreInfo:
    zarr_format = validate_root(path)
    group = zarr.open_group(path, mode="r")
    for member in ("contig_id", "variant_contig"):
        if member not in group:
            raise StoreError(f"{path} has no '{member}' array, so it is not a VCF Zarr store")
    attributes = dict(group.attrs)
    contig_ids = tuple(str(contig) for contig in group["contig_id"][:])
    return StoreInfo(
        path=path,
        zarr_format=zarr_format,
        vcf_zarr_version=_as_text(attributes.get("vcf_zarr_version")),
        writer=_as_text(attributes.get("source")),
        contig_ids=contig_ids,
        variant_contig_ids=tuple(contig_ids[index] for index in _present_contig_indexes(group["variant_contig"])),
    )


def _as_text(value) -> str | None:
    return None if value is None else str(value)


def _present_contig_indexes(array) -> list[int]:
    """The distinct contig indexes in ``variant_contig``, read one chunk at a time so a large store
    never needs the whole array in memory at once."""
    step = int(array.chunks[0])
    present: set[int] = set()
    for start in range(0, int(array.shape[0]), step):
        present.update(int(value) for value in np.unique(array[start : start + step]))
    return sorted(present)


def autosome_number(contig: str) -> str | None:
    """``"1"`` for ``1`` or ``chr1``; ``None`` for anything that is not a human autosome. The ``chr``
    prefix is stripped the way ``vcztools view-plink`` normalises contig names for the BIM."""
    name = contig[len(CONTIG_PREFIX) :] if contig.startswith(CONTIG_PREFIX) else contig
    return name if name in AUTOSOMES else None


def is_autosome(contig: str) -> bool:
    return autosome_number(contig) is not None


def contig_census(store: StoreInfo) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the contigs that carry variants into (autosomal, other), each in store order."""
    autosomal = tuple(contig for contig in store.variant_contig_ids if is_autosome(contig))
    other = tuple(contig for contig in store.variant_contig_ids if not is_autosome(contig))
    return autosomal, other


def declared_autosomes(store: StoreInfo) -> tuple[str, ...]:
    """The autosomes in the store's own naming and order: what ``--autosomes-only`` targets."""
    return tuple(contig for contig in store.contig_ids if is_autosome(contig))


def content_digest(path: str) -> str:
    """SHA-256 over every file in the store.

    Files are visited in sorted relative-path order and each contributes one line,
    ``<relative path>\\0<size>\\0<sha256 of content>\\n``, to a single running digest, so the result
    is a function of names, sizes and bytes only: mtimes, ownership and permissions do not enter it.
    Directory symlinks are not followed.
    """
    entries: list[tuple[str, str]] = []
    for directory, _, filenames in os.walk(path):
        for filename in filenames:
            full_path = os.path.join(directory, filename)
            entries.append((os.path.relpath(full_path, path), full_path))
    entries.sort()
    outer = hashlib.sha256()
    for relative_path, full_path in entries:
        inner = hashlib.sha256()
        size = 0
        with open(full_path, "rb") as handle:
            for block in iter(lambda: handle.read(_HASH_BLOCK), b""):
                inner.update(block)
                size += len(block)
        outer.update(f"{relative_path}\0{size}\0{inner.hexdigest()}\n".encode("utf-8"))
    return outer.hexdigest()


def file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_HASH_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()
