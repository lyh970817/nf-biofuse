# SPDX-License-Identifier: Apache-2.0
"""The sidecar record: everything the identity was taken over, the identity itself, and everything that
was deliberately left out of it but that a person reproducing the run will want to know."""

from __future__ import annotations

import ctypes
import importlib.metadata
import json
import os
import platform
import time

# Not the launcher's nf-core/gwas/biofuse-view-record/1: the record no longer names a cohort, which lives in
# the manifest template, and it records the manifest it published instead.
RECORD_SCHEMA = "nf-biofuse/view-record/1"
RECORD_SUFFIX = ".biofuse_view.json"
# What a PLINK 1 view of a VCF Zarr store cannot carry. The FAM's pedigree, sex and phenotype columns
# and the BIM's genetic-map column are placeholders written by the encoder, not data.
INFORMATION_LOSS = (
    "dosage",
    "phase",
    "multiallelic_sites",
    "fam_pedigree_sex_phenotype",
    "genetic_map",
)
RUNTIME_PACKAGES = ("biofuse", "vcztools", "bio2zarr", "zarr", "numcodecs", "pyfuse3")


def package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def mapped_libfuse3(pid: int) -> str | None:
    """The libfuse3 shared object mapped into process ``pid``, from ``/proc/<pid>/maps``."""
    with open(f"/proc/{pid}/maps", encoding="utf-8") as handle:
        paths = sorted({line.split()[-1] for line in handle if "/libfuse3.so" in line})
    return paths[0] if paths else None


def libfuse3_runtime(pid: int) -> dict:
    """The libfuse3 the daemon ``pid`` actually mapped, and that library's own ``fuse_pkgversion()``."""
    path = mapped_libfuse3(pid)
    if path is None:
        return {"version": None, "path": None}
    library = ctypes.CDLL(path)
    library.fuse_pkgversion.restype = ctypes.c_char_p
    return {"version": library.fuse_pkgversion().decode("ascii"), "path": path}


def runtime_versions(daemon_pid: int) -> dict:
    versions = {name: package_version(name) for name in RUNTIME_PACKAGES}
    versions["python"] = platform.python_version()
    versions["libfuse3"] = libfuse3_runtime(daemon_pid)
    return versions


def build_record(
    *,
    preimage: dict,
    genotype_view_id: str,
    identity_mode: str,
    declared_genotype_view_id: str | None,
    runtime: dict,
    mount_readiness_seconds: float,
    mount: dict,
    manifest: dict,
) -> dict:
    """``genotype_view_id`` is the identity computed from ``preimage``; ``declared_genotype_view_id`` is
    what the manifest declared to the pipeline through ``{view_id}``, which is nothing in ``digest`` mode."""
    return {
        "schema": RECORD_SCHEMA,
        "genotype_view_id": genotype_view_id,
        "identity_mode": identity_mode,
        "declared_genotype_view_id": declared_genotype_view_id,
        "preimage": preimage,
        "runtime": runtime,
        "kernel": platform.release(),
        "mount_readiness_seconds": round(float(mount_readiness_seconds), 3),
        "mount": mount,
        "manifest": manifest,
        "information_loss": list(INFORMATION_LOSS),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def record_path(record_dir: str, basename: str) -> str:
    return os.path.join(record_dir, f"{basename}{RECORD_SUFFIX}")


def write_record(path: str, record: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
