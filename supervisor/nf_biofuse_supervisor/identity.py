# SPDX-License-Identifier: Apache-2.0
"""Content-addressed identity of a BioFuse PLINK 1 view.

A consumer such as nf-core/gwas keys every artifact it derives from a cohort on the cohort's
``genotype_view_id``. For a mounted view there are no bytes on disk to digest, so the supervisor declares
the identity instead, and the declaration has to answer the same question a digest would: would this mount serve the same bytes as
another one? Everything that changes the served bytes is in the preimage; everything that only changes
how fast, where or under what name they are served is not.

    genotype_view_id = "biofuse:" + sha256(canonical JSON of the preimage)

The canonical form is JSON with keys sorted, no whitespace and UTF-8 text unescaped. The preimage is:

    schema      ``nf-biofuse/view-identity/1``
    backend     ``{family: biofuse, view: mount-plink}``
    source      ``{kind: vcz, vcf_zarr_version, writer, revision: {mode: sha256 | declared, value}}``
    runtime     ``{biofuse, vcztools}`` -- the encoder versions, which decide the bytes
    projection  ``{max_alleles, min_alleles, regions, targets, samples, samples_file_sha256, include,
                exclude, types, exclude_types, force_samples, contigs: {policy, excluded}}``

``max_alleles`` is always 2: ``vcztools view-plink`` refuses multiallelic sites, so a biallelic projection
is the only PLINK 1 view this backend serves and it is declared rather than left implicit.

Excluded on purpose: the fileset basename, readahead workers and buffer, log options, mountpoint, state
root, host, the manifest template and everything it declares (for nf-core/gwas, the cohort metadata
``cohort_id``, ``genome_build`` and ``ancestry``), and the pyfuse3, libfuse3, zarr and numcodecs versions.
None of them changes a byte of the served trio; they are recorded in the sidecar record instead (see
:mod:`nf_biofuse_supervisor.record`).
"""

from __future__ import annotations

import hashlib
import json

# The preimage layout version; changing the string moves every id.
SCHEMA = "nf-biofuse/view-identity/1"
NAMESPACE = "biofuse"
BACKEND = {"family": "biofuse", "view": "mount-plink"}
SOURCE_KIND = "vcz"
MAX_ALLELES = 2
REVISION_MODES = ("sha256", "declared")
CONTIG_POLICIES = ("autosomes", "all")
SHORT_ID_LENGTH = 16


def build_projection(
    *,
    min_alleles: int | None = None,
    regions: str | None = None,
    targets: str | None = None,
    samples: str | None = None,
    samples_file_sha256: str | None = None,
    include: str | None = None,
    exclude: str | None = None,
    types: str | None = None,
    exclude_types: str | None = None,
    force_samples: bool = False,
    contig_policy: str = "all",
    excluded_contigs: tuple[str, ...] | list[str] = (),
) -> dict:
    """The projection block of the preimage. Every key is present so that an absent selection is a
    stable ``null`` rather than a missing key, and ``max_alleles`` is pinned to the declared value."""
    if contig_policy not in CONTIG_POLICIES:
        raise ValueError(f"contig policy must be one of {CONTIG_POLICIES}, got {contig_policy!r}")
    return {
        "max_alleles": MAX_ALLELES,
        "min_alleles": min_alleles,
        "regions": regions,
        "targets": targets,
        "samples": samples,
        "samples_file_sha256": samples_file_sha256,
        "include": include,
        "exclude": exclude,
        "types": types,
        "exclude_types": exclude_types,
        "force_samples": bool(force_samples),
        "contigs": {"policy": contig_policy, "excluded": list(excluded_contigs)},
    }


def build_preimage(
    *,
    vcf_zarr_version: str | None,
    writer: str | None,
    revision_mode: str,
    revision_value: str,
    biofuse_version: str,
    vcztools_version: str,
    projection: dict,
) -> dict:
    if revision_mode not in REVISION_MODES:
        raise ValueError(f"revision mode must be one of {REVISION_MODES}, got {revision_mode!r}")
    if not revision_value:
        raise ValueError("revision value must not be empty")
    return {
        "schema": SCHEMA,
        "backend": dict(BACKEND),
        "source": {
            "kind": SOURCE_KIND,
            "vcf_zarr_version": vcf_zarr_version,
            "writer": writer,
            "revision": {"mode": revision_mode, "value": revision_value},
        },
        "runtime": {"biofuse": biofuse_version, "vcztools": vcztools_version},
        "projection": dict(projection),
    }


def canonical_json(value) -> bytes:
    """The canonical serialisation the identity is taken over: sorted keys, no whitespace, UTF-8 text
    left unescaped, and no NaN or infinity."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def preimage_digest(preimage: dict) -> str:
    return hashlib.sha256(canonical_json(preimage)).hexdigest()


def view_id(preimage: dict) -> str:
    """The declared identity: the backend namespace, a colon, and the 64-hex SHA-256 of the preimage."""
    return f"{NAMESPACE}:{preimage_digest(preimage)}"


def short_view_id(identity: str) -> str:
    """The first 16 hex characters of the digest, which name the view's state directory."""
    digest = identity.split(":", 1)[1] if ":" in identity else identity
    return digest[:SHORT_ID_LENGTH]
