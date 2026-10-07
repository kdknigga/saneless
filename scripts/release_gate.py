"""
Refuse a release whose tag and ``pyproject.toml`` name different versions.

The tag (minus one leading ``v``) and the declared project version are both
parsed as PEP 440 versions and compared as versions, never as strings.
Whether the release is a pre-release comes from the parsed version, not from
how the tag is spelled.

The tag must also be semver-shaped. The image tagger derives the version
image tags from semver-shaped tag names only, but it pushes the floating
``next`` tag for any tag at all, so a valid PEP 440 tag such as ``v0.2.0rc7``
or ``v0.2`` would push an image with no version tag, move ``next`` onto it,
and let the index upload run after it.

A semver-shaped tag must also be spelled exactly as ``pyproject.toml`` spells
the version. The image tagger publishes the tag's own spelling as the image
tag, and the documentation pins the declared spelling, so ``v0.2.0-rc6``
against ``0.2.0-rc.6`` would publish ``:0.2.0-rc6`` while every page points at
``:0.2.0-rc.6``, a tag that does not exist. Both mismatches are only visible
after the image has been pushed, so the gate refuses them up front.

On success the script appends ``is_prerelease=true`` or ``is_prerelease=false``
to the file named by ``GITHUB_OUTPUT`` (when set) and echoes the same line to
standard output. On refusal it writes the reason to standard error, exits 1,
and writes no output, so no later job can read a routing value.

The tag name is read from the ``GITHUB_REF_NAME`` environment variable only;
the workflow must never interpolate it into the command line.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

# One pre-release identifier as the semver grammar defines it: a number with
# no leading zero, or an alphanumeric identifier. ``rc.06`` is not valid
# semver, so the image tagging action emits no tag for it.
_PRERELEASE_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"

# The tag shape docker/metadata-action's semver patterns accept:
# MAJOR.MINOR.PATCH, an optional ``-prerelease`` and an optional ``+build``.
_SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-{_PRERELEASE_ID}(?:\.{_PRERELEASE_ID})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


class GateError(Exception):
    """The tag and the project disagree, or one of them is not a version."""


def check(tag: str, declared: str) -> bool:
    """
    Compare a release tag with the project's declared version.

    Args:
        tag: The pushed tag name, with or without one leading ``v``.
        declared: The ``project.version`` value from ``pyproject.toml``.

    Returns:
        Whether the declared version is a pre-release (``rc``, ``a``, ``b``
        or ``dev``).

    Raises:
        GateError: Either value is not a PEP 440 version, the two name
            different versions, the tag is not semver-shaped, or it spells
            the version differently from ``declared``.

    """
    try:
        tagged = Version(tag.removeprefix("v"))
        project = Version(declared)
    except InvalidVersion as exc:
        raise GateError(str(exc)) from exc
    if tagged != project:
        msg = f"tag {tag} is version {tagged}, but pyproject.toml declares {project}"
        raise GateError(msg)
    image_tag = tag.removeprefix("v")
    if not _is_semver_shaped(tag):
        msg = (
            f"tag {tag} is not semver-shaped, so the image tagger would push no "
            "version tag for it, only the floating next tag; tag the release "
            "as vMAJOR.MINOR.PATCH with an optional -prerelease"
        )
        raise GateError(msg)
    if image_tag != declared:
        msg = (
            f"tag {tag} would publish image tag {image_tag!r}, but the docs pin "
            f"pyproject.toml's spelling {declared!r}; the tag must spell the "
            "version exactly as pyproject.toml does"
        )
        raise GateError(msg)
    return project.is_prerelease


def _declared_version(pyproject: Path) -> str:
    """
    Read ``project.version`` from a ``pyproject.toml`` file.

    Args:
        pyproject: Path of the file to read.

    Returns:
        The declared version string.

    Raises:
        GateError: The file cannot be read or parsed, or declares no
            ``project.version`` string.

    """
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        msg = f"cannot read {pyproject}: {exc}"
        raise GateError(msg) from exc
    version = data.get("project", {}).get("version")
    if not isinstance(version, str):
        msg = f"{pyproject} declares no project.version string"
        raise GateError(msg)
    return version


def _is_semver_shaped(tag: str) -> bool:
    """
    Report whether a tag (minus one leading ``v``) is semver-shaped.

    Args:
        tag: The pushed tag name.

    Returns:
        True when the tag is ``MAJOR.MINOR.PATCH`` with an optional
        ``-prerelease`` and ``+build``.

    """
    return _SEMVER.fullmatch(tag.removeprefix("v")) is not None


def main(argv: list[str] | None = None) -> int:
    """
    Run the gate against ``GITHUB_REF_NAME`` and the project file.

    Args:
        argv: Command-line arguments; ``None`` means ``sys.argv[1:]``.

    Returns:
        0 when the tag names the declared version, 1 otherwise.

    """
    parser = argparse.ArgumentParser(
        description="Refuse a release whose tag and pyproject.toml disagree."
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="project file to read the declared version from",
    )
    args = parser.parse_args(argv)

    tag = os.environ.get("GITHUB_REF_NAME", "")
    if not tag:
        sys.stderr.write(
            "release gate: GITHUB_REF_NAME is not set; "
            "run this script from a tag-triggered workflow\n"
        )
        return 1
    try:
        prerelease = check(tag, _declared_version(args.pyproject))
    except GateError as exc:
        sys.stderr.write(f"release gate: {exc}\n")
        return 1

    line = f"is_prerelease={'true' if prerelease else 'false'}\n"
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(line)
    sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
