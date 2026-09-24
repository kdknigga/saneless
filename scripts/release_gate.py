"""
Refuse a release whose tag and ``pyproject.toml`` name different versions.

The tag (minus one leading ``v``) and the declared project version are both
parsed as PEP 440 versions and compared as versions, never as strings, so
``v0.2.0-rc.6`` and ``v0.2.0rc6`` both match a project at ``0.2.0-rc.6``.
Whether the release is a pre-release comes from the parsed version, not from
how the tag is spelled.

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

# The tag shape docker/metadata-action's semver patterns accept:
# MAJOR.MINOR.PATCH, an optional ``-prerelease`` and an optional ``+build``.
_SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
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
        GateError: Either value is not a PEP 440 version, or the two name
            different versions.

    """
    try:
        tagged = Version(tag.removeprefix("v"))
        project = Version(declared)
    except InvalidVersion as exc:
        raise GateError(str(exc)) from exc
    if tagged != project:
        msg = f"tag {tag} is version {tagged}, but pyproject.toml declares {project}"
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

    if not _is_semver_shaped(tag):
        sys.stderr.write(
            f"release gate: warning: tag {tag} is not semver-shaped, so "
            "docker/metadata-action will emit no image tag for it and the image "
            "publish will fail before anything is uploaded to PyPI\n"
        )

    line = f"is_prerelease={'true' if prerelease else 'false'}\n"
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a", encoding="utf-8") as handle:
            handle.write(line)
    sys.stdout.write(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
