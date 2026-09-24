"""
Release version gate tests.

A release is refused when the pushed tag and the version declared in
``pyproject.toml`` name different versions. The two are compared as parsed
PEP 440 versions, so ``v0.2.0-rc.6`` and ``v0.2.0rc6`` are the same release.
Whether the release is a pre-release comes from the parsed version, never from
how the tag happens to be spelled, and nothing is handed to later jobs when the
gate refuses.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from scripts.release_gate import GateError, check, main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("tag", "declared", "expected"),
    [
        ("v0.2.0-rc.6", "0.2.0-rc.6", True),
        ("v0.2.0rc6", "0.2.0-rc.6", True),
        ("v0.2.0rc7", "0.2.0rc7", True),
        ("v0.2.0", "0.2.0", False),
        ("v1.0.0.dev1", "1.0.0.dev1", True),
        ("v1.0.0.post1", "1.0.0.post1", False),
    ],
)
def test_matching_versions_report_whether_the_release_is_a_prerelease(
    tag: str, declared: str, *, expected: bool
) -> None:
    """A tag naming the declared version passes and reports pre-release status."""
    assert check(tag, declared) is expected


@pytest.mark.parametrize(
    ("tag", "declared"),
    [
        ("v0.2.0", "0.2.0-rc.6"),
        ("v9.9.9", "0.2.0-rc.6"),
        ("vbogus", "0.2.0-rc.6"),
    ],
)
def test_mismatched_or_unparseable_tag_is_refused(tag: str, declared: str) -> None:
    """A tag that names another version, or no version at all, is refused."""
    with pytest.raises(GateError):
        check(tag, declared)


def test_refusal_names_both_versions() -> None:
    """The refusal names the tag's version and the declared one, normalized."""
    with pytest.raises(GateError) as exc_info:
        check("v0.2.0", "0.2.0-rc.6")

    text = str(exc_info.value)
    assert re.search(r"\b0\.2\.0\b", text), text
    assert "0.2.0rc6" in text


def _pyproject(tmp_path: Path, version: str) -> Path:
    """
    Write a minimal ``pyproject.toml`` declaring ``version``.

    Args:
        tmp_path: Directory to write the file into.
        version: The ``project.version`` value.

    Returns:
        The path of the written file.

    """
    path = tmp_path / "pyproject.toml"
    path.write_text(
        f'[project]\nname = "demo"\nversion = "{version}"\n', encoding="utf-8"
    )
    return path


def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tag: str, declared: str
) -> tuple[int, Path]:
    """
    Run the gate's entry point with ``tag`` against a project at ``declared``.

    Args:
        monkeypatch: Fixture used to set the workflow environment.
        tmp_path: Directory holding the project file and the output file.
        tag: Value for ``GITHUB_REF_NAME``.
        declared: The project's declared version.

    Returns:
        The exit code and the path named by ``GITHUB_OUTPUT``.

    """
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_REF_NAME", tag)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    code = main(["--pyproject", str(_pyproject(tmp_path, declared))])
    return code, output


def test_mismatch_exits_nonzero_and_writes_no_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refused release fails the step and hands no routing value onward."""
    code, output = _run(monkeypatch, tmp_path, "v0.2.0", "0.2.0-rc.6")

    assert code == 1
    err = capsys.readouterr().err
    assert "0.2.0rc6" in err
    assert "v0.2.0" in err
    assert not output.exists() or output.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    ("tag", "line"),
    [
        ("v0.2.0-rc.6", "is_prerelease=true\n"),
        ("v0.2.0", "is_prerelease=false\n"),
    ],
)
def test_match_writes_exactly_one_routing_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tag: str,
    line: str,
) -> None:
    """An accepted release appends one routing line and echoes it to stdout."""
    code, output = _run(monkeypatch, tmp_path, tag, tag.removeprefix("v"))

    assert code == 0
    assert output.read_text(encoding="utf-8") == line
    captured = capsys.readouterr()
    assert captured.out == line
    assert captured.err == ""


def test_missing_ref_name_fails_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Without a tag name in the environment the gate refuses with a message."""
    output = tmp_path / "out"
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    code = main(["--pyproject", str(_pyproject(tmp_path, "0.2.0"))])

    assert code == 1
    err = capsys.readouterr().err
    assert "GITHUB_REF_NAME" in err
    assert "Traceback" not in err
    assert not output.exists()


def test_non_semver_tag_routes_by_version_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    A PEP 440-only tag spelling still routes as a pre-release, with a warning.

    The image tagger derives image tags from semver-shaped tag names only, so
    the warning tells the maintainer the image publish will fail for this
    spelling; the gate itself still succeeds.
    """
    code, output = _run(monkeypatch, tmp_path, "v0.2.0rc7", "0.2.0rc7")

    assert code == 0
    assert output.read_text(encoding="utf-8") == "is_prerelease=true\n"
    err = capsys.readouterr().err
    assert "warning" in err.lower()
    assert "semver" in err.lower()


def test_semver_tag_produces_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A semver-shaped tag passes silently apart from the routing line."""
    code, _ = _run(monkeypatch, tmp_path, "v0.2.0-rc.6", "0.2.0-rc.6")

    assert code == 0
    assert capsys.readouterr().err == ""
