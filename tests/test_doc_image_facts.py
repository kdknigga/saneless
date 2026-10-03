"""
The Docker reference states what the Dockerfile and release workflow build.

``docs/reference/docker.md`` opens with a table of image facts. Two of them are
easy to get wrong in prose because they are spread across files nobody reads
alongside the page: the entrypoint and default command live at the end of the
``Dockerfile``, and the platforms the image is built for live in the release
workflow's ``docker/build-push-action`` step. These checks read both files and
compare the table against them, so changing either one fails here until the
page follows.

The page's compose examples are checked too: every named volume an example
mounts, commented mounts included, must be declared under that example's
top-level ``volumes:``, or compose refuses the file once the mount is
uncommented. And every example that mounts the config directory -- a whole
saneless service a reader might copy -- mounts the data volume too, without
which the job database and preserved scans vanish when the container is
recreated.

The "Verifying the image" section says which published images carry no
attestations. That caveat names a fixed tag, the last one released before the
attestations were added, so a version bump that rewrites every
release-candidate tag must not rewrite it, and once the declared version is
final the caveat has to go.

Each checker returns its offences as strings, and a seeded bad input proves it
can fail.
"""

import json
import re
import tomllib
from pathlib import Path

from packaging.version import Version

from saneless.config import config_search_paths
from tests.workflow_support import (
    WORKFLOW_DIR,
    job_block,
    numbered,
    step_value,
    steps_using,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKER_REFERENCE = REPO_ROOT / "docs" / "reference" / "docker.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"
RELEASE_WORKFLOW = WORKFLOW_DIR / "release.yml"

# The job that pushes the image, and the action that builds it.
PUBLISH_JOB = "publish-docker"
BUILD_ACTION = "docker/build-push-action"

_FROM = re.compile(r"^FROM\s", re.IGNORECASE)
_FENCE = re.compile(r"^\s*```")
_HEADING = re.compile(r"^#{1,6}\s+(?P<title>.+?)\s*$")
_PLATFORM = re.compile(r"\b[a-z0-9]+/[a-z0-9]+(?:/v[0-9]+)?\b")
_RUNS_ON = re.compile(r"^    runs-on:\s*(?P<label>\S+)")
# A mount in a service's ``volumes:`` list, commented or not: ``- NAME:/path``
# or ``- "NAME:/path"``. A bind mount's source starts with ``.``, ``/`` or
# ``~``, so a source without ``/`` or ``.`` is a named volume.
_MOUNT = re.compile(
    r"^\s*(?P<comment>#\s*)?-\s+\"?(?P<source>[^\s\"':]+):(?P<target>/[^\s\"']*)"
)
# One volume's key under the top-level ``volumes:``, commented or not.
_DECLARATION = re.compile(r"^  (?P<comment>#\s*)?(?P<name>[A-Za-z0-9_.-]+):\s*$")


def _logical_instructions(text: str) -> list[str]:
    """
    Return a Dockerfile's instructions, continuation lines joined.

    A ``HEALTHCHECK`` whose probe sits on a continuation line therefore reads
    as one ``HEALTHCHECK`` instruction, and its ``CMD`` is never mistaken for
    the image's own default command. Comments and blank lines are dropped.
    """
    instructions: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        instructions.append(pending + line)
        pending = ""
    if pending:
        instructions.append(pending.strip())
    return instructions


def _dockerfile_exec_form(instruction: str, text: str | None = None) -> list[str]:
    """
    Return the exec-form list of the final stage's last ``instruction``.

    Args:
        instruction: ``ENTRYPOINT``, ``CMD`` or ``VOLUME``.
        text: The Dockerfile's text; the repository's own when omitted.

    Returns:
        The JSON list the instruction declares, such as ``["saneless"]``.

    Raises:
        AssertionError: When the final stage has no such instruction, or it
            is in shell form, which the page's table could not state as a
            plain argument list.

    """
    source = DOCKERFILE.read_text(encoding="utf-8") if text is None else text
    instructions = _logical_instructions(source)
    starts = [index for index, line in enumerate(instructions) if _FROM.match(line)]
    final_stage = instructions[starts[-1] + 1 :] if starts else instructions
    keyword = re.compile(rf"^{instruction}\s+(?P<value>.*)$", re.IGNORECASE)
    values = [
        match.group("value")
        for match in map(keyword.match, final_stage)
        if match is not None
    ]
    assert values, f"the Dockerfile's final stage declares no {instruction}"
    declared = json.loads(values[-1])
    assert isinstance(declared, list), f"{instruction} is not in exec form"
    return [str(part) for part in declared]


def _image_table(page_text: str) -> dict[str, str]:
    """
    Return the ``## Image`` table as ``{property: value}``, backticks dropped.

    Only the table directly under the ``## Image`` heading is read, so a
    later table with a ``Property`` column cannot answer for it.
    """
    rows: dict[str, str] = {}
    in_section = False
    for line in page_text.splitlines():
        heading = _HEADING.match(line)
        if heading is not None:
            if in_section:
                break
            in_section = line.startswith("## ") and heading.group("title") == "Image"
            continue
        if not in_section or not line.startswith("|"):
            continue
        cells = [cell.strip().replace("`", "") for cell in line.strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("-: "):
            continue
        rows[cells[0]] = cells[1]
    return rows


def _entrypoint_offences(dockerfile_text: str, page_text: str) -> list[str]:
    """
    Return each image-table row that disagrees with the Dockerfile.

    The ``Entrypoint`` row must be the ``ENTRYPOINT`` list joined by spaces,
    and the ``Default command`` row the ``CMD`` list, because ``docker run
    IMAGE doctor`` replaces only the command: it runs ``saneless doctor``.
    """
    table = _image_table(page_text)
    expected = {
        "Entrypoint": " ".join(_dockerfile_exec_form("ENTRYPOINT", dockerfile_text)),
        "Default command": " ".join(_dockerfile_exec_form("CMD", dockerfile_text)),
    }
    offences: list[str] = []
    for row, value in expected.items():
        stated = table.get(row)
        if stated is None:
            offences.append(f"the image table has no {row!r} row; expected {value!r}")
        elif stated != value:
            offences.append(
                f"the image table's {row!r} row says {stated!r}; "
                f"the Dockerfile declares {value!r}"
            )
    return offences


def _runner_platform(block: list[tuple[int, str]]) -> str:
    """
    Return the platform a job's GitHub-hosted runner builds for natively.

    GitHub's hosted Linux runners are x86-64 unless their label names the Arm
    image (``ubuntu-24.04-arm``), and without a ``platforms:`` input the build
    action builds for the runner's own platform only.
    """
    labels = [
        match.group("label")
        for match in (_RUNS_ON.match(line) for _, line in block)
        if match is not None
    ]
    assert labels, "the publish job declares no runs-on"
    return "linux/arm64" if labels[0].endswith("-arm") else "linux/amd64"


def _published_platforms(lines: list[tuple[int, str]] | None = None) -> tuple[str, ...]:
    """
    Return the platforms the release workflow builds the image for.

    Args:
        lines: ``(line number, line)`` pairs of a workflow; the repository's
            release workflow when omitted.

    Returns:
        The build step's ``platforms:`` entries, or the runner's own platform
        when the step names none.

    """
    workflow = numbered(RELEASE_WORKFLOW) if lines is None else lines
    block = job_block(workflow, PUBLISH_JOB)
    assert block, f"the release workflow has no {PUBLISH_JOB} job"
    steps = steps_using(block, BUILD_ACTION)
    assert len(steps) == 1, (
        f"expected one {BUILD_ACTION} step in {PUBLISH_JOB}, found {len(steps)}"
    )
    value = step_value(steps[0], "platforms")
    if not value:
        return (_runner_platform(block),)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _platform_offences(platforms: tuple[str, ...], page_text: str) -> list[str]:
    """Return an offence when the image table's platforms differ from the build's."""
    stated = _image_table(page_text).get("Platforms")
    if stated is None:
        return [f"the image table has no 'Platforms' row; expected {platforms}"]
    named = set(_PLATFORM.findall(stated))
    if named != set(platforms):
        return [
            f"the image table's 'Platforms' row names {sorted(named)}; "
            f"the release workflow builds {sorted(platforms)}"
        ]
    return []


def _compose_blocks(page_text: str) -> list[tuple[int, list[str]]]:
    """Return each fenced block holding a compose file, with its first line."""
    blocks: list[tuple[int, list[str]]] = []
    current: list[str] | None = None
    start = 0
    for number, line in enumerate(page_text.splitlines(), start=1):
        if _FENCE.match(line):
            if current is None:
                current, start = [], number + 1
            else:
                if any(entry.rstrip() == "services:" for entry in current):
                    blocks.append((start, current))
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def _volume_offences(page_text: str) -> list[str]:
    """
    Return each named volume a compose example mounts but does not declare.

    A live mount needs a live declaration under the block's top-level
    ``volumes:``. A commented mount is satisfied by a commented declaration
    as well, since uncommenting both together yields a valid file.
    """
    offences: list[str] = []
    for start, block in _compose_blocks(page_text):
        live: set[str] = set()
        commented: set[str] = set()
        in_volumes = False
        for line in block:
            if line.strip() and not line[0].isspace() and not line.startswith("#"):
                in_volumes = line.rstrip() == "volumes:"
                continue
            declaration = _DECLARATION.match(line)
            if in_volumes and declaration is not None:
                target = commented if declaration.group("comment") else live
                target.add(declaration.group("name"))
        for offset, line in enumerate(block):
            mount = _MOUNT.match(line)
            if mount is None:
                continue
            source = mount.group("source")
            if "/" in source or "." in source or source.startswith("~"):
                continue
            declared = live | commented if mount.group("comment") else live
            if source not in declared:
                offences.append(
                    f"docker.md:{start + offset}: mounts the volume {source!r}, "
                    "which the example's top-level volumes: does not declare"
                )
    return offences


# The last image the release pipeline published without attestations.
LAST_UNATTESTED_TAG = "0.2.0-rc.6"
VERIFYING_HEADING = "## Verifying the image"
_RC_TAG = re.compile(r"\b\d+\.\d+\.\d+-rc\.\d+\b")


def _section(page_text: str, heading: str) -> str:
    """Return the text under ``heading`` up to the next level-2 heading."""
    lines = page_text.splitlines()
    starts = [n for n, line in enumerate(lines) if line.strip() == heading]
    assert starts, f"no {heading!r} heading"
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def _attestation_caveat_offences(section: str, declared: str) -> list[str]:
    """
    Return what is wrong with the unattested-images caveat for a version.

    While the declared version is a release candidate, every candidate tag
    the section names must be the last unattested one; a bump that rewrote it
    would claim the new candidate has no attestations. Once the version is
    final, the section names no candidate tag at all.
    """
    tags = _RC_TAG.findall(section)
    if Version(declared).is_prerelease:
        return [
            f"names {tag} as an unattested image; the last one is {LAST_UNATTESTED_TAG}"
            for tag in tags
            if tag != LAST_UNATTESTED_TAG
        ]
    return [
        f"still names the release candidate {tag} under the final release "
        f"{declared}; state only that images carry the two attestations"
        for tag in tags
    ]


def _config_target() -> str:
    """Return the system config directory, the last place the loader searches."""
    return config_search_paths()[-1].parent.as_posix()


def _data_target() -> str:
    """Return the one directory the Dockerfile's final stage declares a VOLUME."""
    volumes = _dockerfile_exec_form("VOLUME")
    assert len(volumes) == 1, f"expected one VOLUME, the Dockerfile declares {volumes}"
    return volumes[0]


def _data_volume_offences(page_text: str) -> list[str]:
    """
    Return each compose example that mounts the config but not the data.

    An example mounting ``/etc/saneless`` is a whole saneless service, and
    one without a live mount at ``/var/lib/saneless`` loses the job database
    and ``failed/`` the next time compose recreates the container.
    """
    config_target = _config_target()
    data_target = _data_target()
    offences: list[str] = []
    for start, block in _compose_blocks(page_text):
        live = {
            mount.group("target").rstrip("/")
            for mount in map(_MOUNT.match, block)
            if mount is not None and not mount.group("comment")
        }
        if config_target in live and data_target not in live:
            offences.append(
                f"docker.md:{start}: mounts {config_target} but not the data "
                f"volume at {data_target}"
            )
    return offences


# --- seeded: each checker can fail -----------------------------------------

_SEEDED_DOCKERFILE = """\
FROM python:3.14-slim AS builder
CMD ["builder-only"]

FROM python:3.14-slim
HEALTHCHECK --interval=30s \\
    CMD ["python", "-c", "probe"]
ENTRYPOINT ["x"]
CMD ["serve"]
"""

_SEEDED_PAGE = """\
# Docker

## Image

| Property | Value |
|----------|-------|
| Entrypoint | `y` |
| Default command | `serve` |
| Platforms | `linux/amd64` |

## Volumes

```yaml
services:
  app:
    volumes:
      - ./config:/etc/app
      - app-data:/var/lib/app
      - other-data:/srv
      # - spare:/spare

volumes:
  app-data:
  # spare:
```
"""


def test_seeded_exec_form_skips_the_healthcheck_and_earlier_stages() -> None:
    """The parser reads the final stage's own CMD, not the probe's or a builder's."""
    assert _dockerfile_exec_form("ENTRYPOINT", _SEEDED_DOCKERFILE) == ["x"]
    assert _dockerfile_exec_form("CMD", _SEEDED_DOCKERFILE) == ["serve"]


def test_seeded_entrypoint_mismatch_is_reported() -> None:
    """A table saying ``y`` against ``ENTRYPOINT ["x"]`` is one offence."""
    offences = _entrypoint_offences(_SEEDED_DOCKERFILE, _SEEDED_PAGE)
    assert len(offences) == 1, offences
    assert "'y'" in offences[0]


def test_seeded_platform_mismatch_is_reported() -> None:
    """A table naming amd64 for an arm64 build is one offence."""
    assert _platform_offences(("linux/amd64",), _SEEDED_PAGE) == []
    assert len(_platform_offences(("linux/arm64",), _SEEDED_PAGE)) == 1


def test_seeded_platforms_default_to_the_runner() -> None:
    """No ``platforms:`` input means the runner's own platform; one given wins."""
    workflow = """\
jobs:
  publish-docker:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/build-push-action@0000 # v7
        with:
          context: .
"""
    lines: list[tuple[int, str]] = list(enumerate(workflow.splitlines(), start=1))
    assert _published_platforms(lines) == ("linux/amd64",)
    arm: list[tuple[int, str]] = [
        (n, line.replace("ubuntu-latest", "ubuntu-24.04-arm")) for n, line in lines
    ]
    assert _published_platforms(arm) == ("linux/arm64",)
    multi: list[tuple[int, str]] = [
        *lines,
        (99, "          platforms: linux/amd64,linux/arm64"),
    ]
    assert _published_platforms(multi) == ("linux/amd64", "linux/arm64")


def test_seeded_undeclared_volume_is_reported() -> None:
    """An undeclared named volume is one offence; bind mounts are not volumes."""
    offences = _volume_offences(_SEEDED_PAGE)
    assert len(offences) == 1, offences
    assert "'other-data'" in offences[0]


def test_seeded_commented_mount_needs_a_declaration() -> None:
    """A commented mount with no declaration at all is reported too."""
    page = _SEEDED_PAGE.replace("      - other-data:/srv\n", "").replace(
        "  # spare:\n", ""
    )
    offences = _volume_offences(page)
    assert len(offences) == 1, offences
    assert "'spare'" in offences[0]


def test_seeded_service_without_the_data_volume_is_reported() -> None:
    """A service mounting the config without the data volume is one offence."""
    data_mount = f"      - saneless-data:{_data_target()}\n"
    whole = (
        "```yaml\nservices:\n  saneless:\n    volumes:\n"
        f"      - ./config:{_config_target()}\n{data_mount}```\n"
    )
    assert _data_volume_offences(whole) == []
    commented = whole.replace("      - saneless-data", "      # - saneless-data")
    without = whole.replace(data_mount, "")
    for page in (commented, without):
        offences = _data_volume_offences(page)
        assert len(offences) == 1, offences
        assert _data_target() in offences[0]


def test_seeded_attestation_caveat_offences() -> None:
    """A rewritten tag, and any candidate tag after the final release, fail."""
    caveat = f"Images up to {LAST_UNATTESTED_TAG} carry neither."
    assert _attestation_caveat_offences(caveat, "0.2.0-rc.7") == []
    rewritten = caveat.replace(LAST_UNATTESTED_TAG, "0.2.0-rc.7")
    assert len(_attestation_caveat_offences(rewritten, "0.2.0-rc.7")) == 1
    assert len(_attestation_caveat_offences(caveat, "0.2.0")) == 1
    assert _attestation_caveat_offences("Images carry both.", "0.2.0") == []


# --- the real page -----------------------------------------------------------


def test_image_table_states_the_dockerfile_entrypoint_and_command() -> None:
    """The Entrypoint and Default command rows match the Dockerfile."""
    offences = _entrypoint_offences(
        DOCKERFILE.read_text(encoding="utf-8"),
        DOCKER_REFERENCE.read_text(encoding="utf-8"),
    )
    assert not offences, "\n".join(offences)


def test_image_table_names_the_platforms_the_release_builds() -> None:
    """The Platforms row names exactly what the release workflow builds."""
    offences = _platform_offences(
        _published_platforms(), DOCKER_REFERENCE.read_text(encoding="utf-8")
    )
    assert not offences, "\n".join(offences)


def test_every_compose_example_declares_the_volumes_it_mounts() -> None:
    """Each named volume a docker.md compose example mounts is declared."""
    page = DOCKER_REFERENCE.read_text(encoding="utf-8")
    blocks = _compose_blocks(page)
    assert blocks, "docker.md holds no compose example"
    mounts = [line for _, block in blocks for line in block if _MOUNT.match(line)]
    assert mounts, "docker.md's compose examples mount nothing"
    offences = _volume_offences(page)
    assert not offences, "\n".join(offences)


def test_every_whole_service_example_mounts_the_data_volume() -> None:
    """Each docker.md example mounting the config mounts the data volume too."""
    page = DOCKER_REFERENCE.read_text(encoding="utf-8")
    offences = _data_volume_offences(page)
    assert not offences, "\n".join(offences)


def test_the_attestation_caveat_fits_the_declared_version() -> None:
    """The unattested-images caveat names the right tag, and only in a pre-release."""
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "version"
    ]
    section = _section(DOCKER_REFERENCE.read_text(encoding="utf-8"), VERIFYING_HEADING)
    assert "attestation" in section, "the verifying section names no attestation"
    offences = _attestation_caveat_offences(section, declared)
    assert not offences, "\n".join(offences)
