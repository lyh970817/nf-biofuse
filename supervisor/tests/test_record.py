# Written by the nf-core/gwas team and released under the MIT license.
"""The sidecar record's shape and the runtime facts it carries."""

import json
import os
import re

import pytest

from nf_core_gwas_biofuse import identity, record


def _preimage():
    return identity.build_preimage(
        vcf_zarr_version="0.5",
        writer="bio2zarr-0.2.1",
        revision_mode="sha256",
        revision_value="ab" * 32,
        biofuse_version="0.1.0",
        vcztools_version="0.2.0",
        projection=identity.build_projection(),
    )


def test_record_shape(tmp_path):
    preimage = _preimage()
    view_id = identity.view_id(preimage)
    built = record.build_record(
        cohort_id="cohort_a",
        preimage=preimage,
        genotype_view_id=view_id,
        identity_mode="declared",
        declared_genotype_view_id=view_id,
        runtime={
            "biofuse": "0.1.0",
            "vcztools": "0.2.0",
            "libfuse3": {"version": "3.18.3", "path": "/env/lib/libfuse3.so.3.18.3"},
        },
        mount_readiness_seconds=0.82849,
        mount={"mountpoint": "/run/user/1000/x/mnt", "basename": "cohort"},
    )
    assert list(built) == [
        "schema",
        "cohort_id",
        "genotype_view_id",
        "identity_mode",
        "declared_genotype_view_id",
        "preimage",
        "runtime",
        "kernel",
        "mount_readiness_seconds",
        "mount",
        "information_loss",
        "created",
    ]
    assert built["schema"] == "nf-core/gwas/biofuse-view-record/1"
    assert built["preimage"] == preimage
    assert identity.view_id(built["preimage"]) == built["genotype_view_id"]
    assert built["mount_readiness_seconds"] == 0.828
    assert built["information_loss"] == [
        "dosage",
        "phase",
        "multiallelic_sites",
        "fam_pedigree_sex_phenotype",
        "genetic_map",
    ]
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$", built["created"])
    assert built["kernel"] == os.uname().release

    path = record.record_path(str(tmp_path), "cohort_a")
    assert path == str(tmp_path / "cohort_a.biofuse_view.json")
    record.write_record(path, built)
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle) == built


def test_digest_mode_keeps_the_computed_id_but_declares_nothing():
    preimage = _preimage()
    built = record.build_record(
        cohort_id="c",
        preimage=preimage,
        genotype_view_id=identity.view_id(preimage),
        identity_mode="digest",
        declared_genotype_view_id=None,
        runtime={},
        mount_readiness_seconds=1,
        mount={},
    )
    assert built["identity_mode"] == "digest"
    assert built["genotype_view_id"].startswith("biofuse:")
    assert built["declared_genotype_view_id"] is None


def test_runtime_versions_name_every_component():
    versions = record.runtime_versions(os.getpid())
    assert set(versions) == {"biofuse", "vcztools", "bio2zarr", "zarr", "numcodecs", "pyfuse3", "python", "libfuse3"}
    assert re.match(r"^\d+\.\d+\.\d+", versions["python"])
    assert set(versions["libfuse3"]) == {"version", "path"}


def test_libfuse3_version_is_read_from_the_library_the_process_mapped():
    pytest.importorskip("pyfuse3")  # maps the environment's libfuse3 into this process
    runtime = record.libfuse3_runtime(os.getpid())
    assert runtime["path"] == record.mapped_libfuse3(os.getpid())
    assert "/libfuse3.so" in runtime["path"]
    assert re.match(r"^\d+\.\d+\.\d+$", runtime["version"])
