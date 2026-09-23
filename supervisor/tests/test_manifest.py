# SPDX-License-Identifier: Apache-2.0
"""The manifest rendered from a template, and the files the supervisor publishes outside the view."""

import csv
import io
import json
import os
import stat

import pytest

from nf_biofuse_supervisor import manifest

MOUNT = "/run/user/1000/nf-biofuse/view-0123456789abcdef/mnt"
VIEW_ID = "biofuse:" + "ab" * 32
EXAMPLES = os.path.join(os.path.dirname(__file__), "..", "..", "examples")
GWAS_TEMPLATE = os.path.join(EXAMPLES, "nf-core-gwas", "cohort.biofuse.template.csv")
GWAS_HEADER = "cohort_id,genome_build,ancestry,pgen,psam,pvar,bed,bim,fam,genotype_view_id"


def fields(view_id=VIEW_ID):
    return manifest.values(mount_dir=MOUNT, basename="cohort", view_id=view_id)


def test_every_placeholder_has_a_value():
    assert set(fields()) == set(manifest.PLACEHOLDERS)
    assert fields() == {
        "bed": f"{MOUNT}/cohort.bed",
        "bim": f"{MOUNT}/cohort.bim",
        "fam": f"{MOUNT}/cohort.fam",
        "prefix": f"{MOUNT}/cohort",
        "mount": MOUNT,
        "basename": "cohort",
        "view_id": VIEW_ID,
    }
    assert fields(view_id=None)["view_id"] == ""


def test_render_substitutes_placeholders_and_copies_everything_else():
    template = 'id\tbfile\n{basename}\t{prefix}\n{"json": "{view_id}", "braces": "{not a placeholder}"}\n'
    assert manifest.render(template, fields()) == (
        f'id\tbfile\ncohort\t{MOUNT}/cohort\n{{"json": "{VIEW_ID}", "braces": "{{not a placeholder}}"}}\n'
    )


def test_an_unknown_placeholder_is_refused():
    with pytest.raises(manifest.ManifestError, match=r"\{bedd\}"):
        manifest.render("{bedd},{bim}\n", fields())


def test_the_nf_core_gwas_example_renders_a_cohort_manifest_row():
    with open(GWAS_TEMPLATE, encoding="utf-8") as handle:
        template = handle.read()
    for view_id in (VIEW_ID, None):
        rendered = manifest.render(template, fields(view_id))
        (row,) = [
            {key: value for key, value in row.items() if value != ""}
            for row in csv.DictReader(io.StringIO(rendered))
        ]
        assert rendered.splitlines()[0] == GWAS_HEADER
        assert row["bed"] == f"{MOUNT}/cohort.bed"
        assert row["bim"] == f"{MOUNT}/cohort.bim"
        assert row["fam"] == f"{MOUNT}/cohort.fam"
        assert "pgen" not in row
        assert row.get("genotype_view_id") == view_id


def test_write_is_atomic_and_owner_readable_only(tmp_path):
    path = tmp_path / "manifest.csv"
    path.write_text("stale\n")
    os.chmod(path, 0o644)
    manifest.write(str(path), "fresh\n")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text() == "fresh\n"
    assert os.listdir(tmp_path) == ["manifest.csv"]


def test_an_identical_leftover_may_be_replaced_but_a_foreign_file_may_not(tmp_path):
    path = tmp_path / "manifest.csv"
    manifest.check_target(str(path), "content\n")
    path.write_text("content\n")
    manifest.check_target(str(path), "content\n")
    path.write_text("somebody else's samplesheet\n")
    with pytest.raises(manifest.ManifestError, match="refusing to overwrite"):
        manifest.check_target(str(path), "content\n")
    with pytest.raises(manifest.ManifestError, match="does not exist"):
        manifest.check_target(str(tmp_path / "missing" / "manifest.csv"), "content\n")


def test_remove_if_unchanged_leaves_a_changed_file(tmp_path):
    path = tmp_path / "manifest.csv"
    path.write_text("ours\n")
    digest = manifest.digest("ours\n")
    assert manifest.remove_if_unchanged(str(path), digest)
    assert not path.exists()
    assert manifest.remove_if_unchanged(str(path), digest)
    path.write_text("theirs\n")
    assert not manifest.remove_if_unchanged(str(path), digest)
    assert path.read_text() == "theirs\n"


def test_template_that_cannot_be_read_is_refused(tmp_path):
    with pytest.raises(manifest.ManifestError, match="cannot read the manifest template"):
        manifest.read_template(str(tmp_path / "missing.csv"))


def test_published_json_is_plain_data(tmp_path):
    # The registry is JSON so that a person recovering by hand can read what teardown will remove.
    from nf_biofuse_supervisor import mount

    paths = mount.ViewPaths.under(str(tmp_path), "0123456789abcdef")
    os.makedirs(paths.view_dir)
    target = tmp_path / "manifest.csv"
    mount.publish(paths, str(target), "one\n")
    mount.publish(paths, str(target), "two\n")
    with open(paths.published, encoding="utf-8") as handle:
        assert json.load(handle) == [{"path": str(target), "sha256": manifest.digest("two\n")}]
