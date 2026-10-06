"""
The image tag the documentation pins, decided once for every check.

A release candidate publishes only its own exact tag, so that is the only tag
a reader can pull. A final release also publishes a ``major.minor`` tag that
later patch releases move forward, and that is the one the documentation pins.
Every check that compares a documented image reference with the declared
version asks ``expected_image_tag``, so the checks cannot disagree about which
tag a release's documentation carries.

``tests/`` is a package, so pytest's default prepend mode imports it as
``tests.*``, and the helper is imported by that package name.  Import it as
``from tests.image_tag_support import expected_image_tag``.
"""

from __future__ import annotations

from packaging.version import Version


def expected_image_tag(declared: str) -> str:
    """
    Return the image tag the documentation must pin for a declared version.

    The pre-release tag is the version as ``pyproject.toml`` spells it, which
    is the spelling the release tag and so the image tag carry.

    Args:
        declared: ``project.version`` as written in ``pyproject.toml``.

    Returns:
        The declared string for a pre-release, else ``major.minor``.

    """
    version = Version(declared)
    if version.is_prerelease:
        return declared
    return f"{version.major}.{version.minor}"
