# SPDX-License-Identifier: Apache-2.0
"""The manifest the pipeline reads, rendered from a user template.

The supervisor knows nothing of any pipeline's input format. The template is the pipeline's own
samplesheet (or any text file) with placeholders where the mounted view goes; the supervisor fills them
in once the view is readable and writes the result where the pipeline expects it. A placeholder is a
lowercase name in braces; every other character, braces included, is copied verbatim:

    {bed} {bim} {fam}   absolute paths of the mounted members
    {prefix}            the mounted fileset prefix, <mount>/<basename>, as PLINK's --bfile takes it
    {mount}             the mount directory
    {basename}          the fileset stem
    {view_id}           the view identity in declared mode, empty in digest mode

An nf-core/gwas cohort manifest template, for example:

    cohort_id,genome_build,ancestry,pgen,psam,pvar,bed,bim,fam,genotype_view_id
    cohort_a,GRCh38,EUR,,,,{bed},{bim},{fam},{view_id}
"""

from __future__ import annotations

import hashlib
import os
import re

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
PLACEHOLDERS = ("bed", "bim", "fam", "prefix", "mount", "basename", "view_id")


class ManifestError(ValueError):
    """A template or a manifest target the supervisor refuses. Exit status 2."""

    exit_code = 2


def read_template(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError as error:
        raise ManifestError(f"cannot read the manifest template {path!r}: {error.strerror}") from None


def values(*, mount_dir: str, basename: str, view_id: str | None) -> dict[str, str]:
    prefix = os.path.join(mount_dir, basename)
    return {
        "bed": f"{prefix}.bed",
        "bim": f"{prefix}.bim",
        "fam": f"{prefix}.fam",
        "prefix": prefix,
        "mount": mount_dir,
        "basename": basename,
        "view_id": view_id or "",
    }


def render(template: str, fields: dict[str, str]) -> str:
    """Substitute every placeholder; a name that is not a placeholder is refused, since it is a typo that
    would otherwise reach the pipeline as literal text."""
    unknown = sorted({name for name in PLACEHOLDER.findall(template) if name not in fields})
    if unknown:
        raise ManifestError(
            f"unknown placeholder(s) {', '.join('{' + name + '}' for name in unknown)} in the manifest template; "
            f"known: {', '.join('{' + name + '}' for name in PLACEHOLDERS)}"
        )
    return PLACEHOLDER.sub(lambda match: fields[match.group(1)], template)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_target(path: str, text: str) -> None:
    """Refuse to replace a file that is not this manifest. The manifest is removed at teardown, so a file
    already at the target is either one a previous run of the same view left behind, which is identical,
    or somebody else's, which the supervisor must neither overwrite nor later delete."""
    directory = os.path.dirname(path) or "."
    if not os.path.isdir(directory):
        raise ManifestError(f"the manifest directory {directory!r} does not exist")
    try:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read()
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError) as error:
        raise ManifestError(f"cannot read the existing file at the manifest path {path!r}: {error}") from None
    if existing != text:
        raise ManifestError(f"refusing to overwrite {path!r}: it exists and is not this view's manifest")


def write(path: str, text: str) -> None:
    """Write ``text`` atomically, readable by its owner only: it names paths under a private state root.
    A reader sees either no file or all of it."""
    temporary = f"{path}.{os.getpid()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def remove_if_unchanged(path: str, sha256: str) -> bool:
    """Delete ``path`` if it still holds the content whose digest is ``sha256``. True when it is gone."""
    try:
        with open(path, "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if current != sha256:
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    return True
