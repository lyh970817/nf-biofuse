# Written by the nf-core/gwas team and released under the MIT license.
"""The temporary cohort manifest the launched pipeline reads.

One row in the pipeline's own cohort manifest schema (``assets/schema_cohort_manifest.json``): only the
PLINK 1 group and the declared identity are populated. The column constraints repeated here are the
schema's, so the launcher rejects a cohort declaration it would otherwise hand to the pipeline to reject.
"""

from __future__ import annotations

import os
import re

COLUMNS = (
    "cohort_id",
    "genome_build",
    "ancestry",
    "pgen",
    "psam",
    "pvar",
    "bed",
    "bim",
    "fam",
    "genotype_view_id",
)
HEADER = ",".join(COLUMNS)
GENOME_BUILDS = ("GRCh37", "GRCh38")
COHORT_ID_PATTERN = re.compile(r"^\S+$")
ANCESTRY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
VIEW_ID_PATTERN = re.compile(r"^([a-z][a-z0-9_]*:)?[0-9a-f]{16,64}$")
# Cells are written unquoted, so a cell may not carry the separator or whitespace; the schema's own
# ``^\S+`` patterns already forbid the latter for every path column.
CELL_PATTERN = re.compile(r"^[^\s,]+$")


class ManifestError(ValueError):
    """A cohort declaration the pipeline's manifest schema would reject."""


def validate_cohort(cohort_id: str, genome_build: str, ancestry: str) -> None:
    if not COHORT_ID_PATTERN.match(cohort_id) or "," in cohort_id:
        raise ManifestError(f"cohort_id {cohort_id!r} must be non-empty and contain no whitespace or comma")
    if genome_build not in GENOME_BUILDS:
        raise ManifestError(f"genome_build must be one of {', '.join(GENOME_BUILDS)}, got {genome_build!r}")
    if not ANCESTRY_PATTERN.match(ancestry):
        raise ManifestError(
            f"ancestry {ancestry!r} must start with a letter or digit and contain only letters, digits, "
            "underscores, dots and dashes"
        )


def render(
    *,
    cohort_id: str,
    genome_build: str,
    ancestry: str,
    bed: str,
    bim: str,
    fam: str,
    genotype_view_id: str | None,
) -> str:
    """The manifest text: the exact header, then one row with the PLINK 1 trio and the identity."""
    validate_cohort(cohort_id, genome_build, ancestry)
    for column, path, extension in (("bed", bed, ".bed"), ("bim", bim, ".bim"), ("fam", fam, ".fam")):
        if not CELL_PATTERN.match(path) or not path.endswith(extension):
            raise ManifestError(f"{column} path {path!r} must end in {extension} and contain no whitespace or comma")
    if genotype_view_id is not None and not VIEW_ID_PATTERN.match(genotype_view_id):
        raise ManifestError(f"genotype_view_id {genotype_view_id!r} does not match the schema pattern")
    cells = {
        "cohort_id": cohort_id,
        "genome_build": genome_build,
        "ancestry": ancestry,
        "bed": bed,
        "bim": bim,
        "fam": fam,
        "genotype_view_id": genotype_view_id or "",
    }
    row = ",".join(cells.get(column, "") for column in COLUMNS)
    return f"{HEADER}\n{row}\n"


def write(path: str, text: str) -> None:
    """Write the manifest readable by its owner only: it names paths under a private state root."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise
    with handle:
        # The mode in os.open only applies to a newly created file; an existing one keeps its mode.
        os.fchmod(descriptor, 0o600)
        handle.write(text)
