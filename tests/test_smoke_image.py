"""
The documented container commands must be readable and safe to run.

The image smoke run takes the ``docker run`` a reader is told to paste from
README.md and from the Quick Start, rather than keeping its own copy, so a
documented command that drifts until it no longer works turns CI red. That
text is editable by anyone who can open a pull request, and CI hands it to a
container engine, so the smoke script reads it with ``shlex`` (never a shell),
holds it to an allow-list of flags and mounts, and only then swaps in the
values a test run needs: the image under test, a loopback port, a scratch
config directory, a throwaway data volume and a unique container name.

These tests pin that contract without a container engine: what the extractor
finds in a page, every shape the validator refuses, what the substitution
changes and leaves alone, and that README and Quick Start show the same,
complete command.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.smoke_image import (
    ALLOWED_RUN_FLAGS,
    DocumentedRun,
    SmokeFailure,
    documented_docker_runs,
    parse_documented_run,
    substitute_documented_run,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
QUICK_START = REPO_ROOT / "docs" / "getting-started" / "quick-start.md"
PYPROJECT = REPO_ROOT / "pyproject.toml"

# The published image, assembled from its parts: every tracked file is swept
# for references to the image, and a literal here would be one with no tag.
_REGISTRY = "ghcr.io"
_OWNER = "kdknigga"
_PROJECT = "saneless"
IMAGE_NAME = f"{_REGISTRY}/{_OWNER}/{_PROJECT}"
SEED_TAG = "1.2.3"
SEED_IMAGE = f"{IMAGE_NAME}:{SEED_TAG}"

CONFIG_TARGET = "/etc/saneless"
DATA_TARGET = "/var/lib/saneless"
SCANNER_HOST_ENV = "SANELESS_SCANNER__HOST="

# A complete documented command, as the extractor returns it.
GOOD_RUN = [
    "docker",
    "run",
    "-d",
    "--name",
    "saneless",
    "-p",
    "8080:8080",
    "--stop-timeout",
    "90",
    "-v",
    f"$(pwd)/config:{CONFIG_TARGET}",
    "-v",
    f"saneless-data:{DATA_TARGET}",
    "-e",
    f"{SCANNER_HOST_ENV}192.168.1.50",
    SEED_IMAGE,
]


def _without(argv: list[str], *pairs: tuple[str, str]) -> list[str]:
    """Return ``argv`` with each ``flag value`` pair in ``pairs`` removed."""
    out = list(argv)
    for flag, value in pairs:
        for index in range(len(out) - 1):
            if out[index] == flag and out[index + 1] == value:
                del out[index : index + 2]
                break
        else:
            msg = f"{flag} {value} is not in the seed"
            raise AssertionError(msg)
    return out


def _with(argv: list[str], *extra: str) -> list[str]:
    """Return ``argv`` with ``extra`` inserted just before the image."""
    return [*argv[:-1], *extra, argv[-1]]


def _declared_version() -> str:
    """Return ``project.version`` exactly as ``pyproject.toml`` spells it."""
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_a_plain_fence_yields_its_command() -> None:
    """A fenced ``docker run`` wrapped with backslashes is read as one argv."""
    text = (
        "Run it:\n\n"
        "```bash\n"
        "docker run -d --name saneless \\\n"
        "  -p 8080:8080 --stop-timeout 90 \\\n"
        f'  -v "$(pwd)/config:{CONFIG_TARGET}" \\\n'
        f"  -v saneless-data:{DATA_TARGET} \\\n"
        f"  -e {SCANNER_HOST_ENV}192.168.1.50 \\\n"
        f"  {SEED_IMAGE}\n"
        "```\n"
    )
    assert documented_docker_runs(text) == [GOOD_RUN]


def test_an_indented_tab_fence_yields_its_command() -> None:
    """A fence indented under a ``=== "Docker"`` tab is found and dedented."""
    text = (
        '=== "Docker"\n\n'
        "    Start it:\n\n"
        "    ```bash\n"
        "    docker run -d --name saneless -p 8080:8080 \\\n"
        "      --stop-timeout 90 \\\n"
        f'      -v "$(pwd)/config:{CONFIG_TARGET}" \\\n'
        f"      -v saneless-data:{DATA_TARGET} \\\n"
        f"      -e {SCANNER_HOST_ENV}192.168.1.50 \\\n"
        f"      {SEED_IMAGE}\n"
        "    ```\n\n"
        '=== "Bare metal"\n\n'
        "    ```bash\n"
        "    saneless serve\n"
        "    ```\n"
    )
    assert documented_docker_runs(text) == [GOOD_RUN]


def test_every_documented_run_is_returned_in_page_order() -> None:
    """Two fences of the image give two argv lists, first one first."""
    second = f"docker run --rm -p 9090:8080 --stop-timeout 90 {SEED_IMAGE}\n"
    text = (
        f"```bash\ndocker run -p 8080:8080 {SEED_IMAGE}\n```\n\n"
        f"Then:\n\n```sh\n{second}```\n"
    )
    assert documented_docker_runs(text) == [
        ["docker", "run", "-p", "8080:8080", SEED_IMAGE],
        [
            "docker",
            "run",
            "--rm",
            "-p",
            "9090:8080",
            "--stop-timeout",
            "90",
            SEED_IMAGE,
        ],
    ]


def test_other_images_compose_blocks_and_prose_are_ignored() -> None:
    """Only fenced runs of the published image count."""
    text = (
        f"Prose mentions `docker run -p 8080:8080 {SEED_IMAGE}` inline.\n\n"
        f"docker run -p 8080:8080 {SEED_IMAGE}\n\n"
        "```bash\n"
        "docker run --rm -p 8000:8000 docker.io/library/nginx:1.27\n"
        "docker compose up -d\n"
        "```\n\n"
        "```yaml\n"
        "services:\n"
        "  saneless:\n"
        f"    image: {SEED_IMAGE}\n"
        "```\n\n"
        "```bash\n"
        f"docker run -p 8080:8080 {IMAGE_NAME}-other:{SEED_TAG}\n"
        "```\n"
    )
    assert documented_docker_runs(text) == []


def test_a_command_the_shell_could_not_parse_is_refused() -> None:
    """An unbalanced quote fails loudly rather than yielding a wrong argv."""
    text = f'```bash\ndocker run -e "A=b -p 8080:8080 {SEED_IMAGE}\n```\n'
    with pytest.raises(SmokeFailure):
        documented_docker_runs(text)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_a_complete_command_parses_into_its_parts() -> None:
    """Each field of the parsed run is the documented value."""
    assert parse_documented_run(GOOD_RUN) == DocumentedRun(
        publish="8080:8080",
        config_source="$(pwd)/config",
        config_options="",
        data_volume="saneless-data",
        env=(f"{SCANNER_HOST_ENV}192.168.1.50",),
        stop_timeout="90",
        name="saneless",
        detach=True,
        image=SEED_IMAGE,
    )


def test_the_joined_flag_form_parses_like_the_separate_form() -> None:
    """``--flag=value`` is read the same as ``--flag value``."""
    joined = [
        "docker",
        "run",
        "--name=saneless",
        "-p=8080:8080",
        "--stop-timeout=90",
        f"-v=$(pwd)/config:{CONFIG_TARGET}:ro",
        f"-v=saneless-data:{DATA_TARGET}",
        "--add-host=host.docker.internal:host-gateway",
        "--rm",
        SEED_IMAGE,
    ]
    run = parse_documented_run(joined)
    assert run.name == "saneless"
    assert run.publish == "8080:8080"
    assert run.stop_timeout == "90"
    assert (run.config_source, run.config_options) == ("$(pwd)/config", "ro")
    assert run.data_volume == "saneless-data"
    assert run.detach is False
    assert run.env == ()


def test_the_allow_list_is_the_documented_one() -> None:
    """Every flag a getting-started command needs, and nothing that widens it."""
    assert set(ALLOWED_RUN_FLAGS) == {
        "-d",
        "--rm",
        "--name",
        "-p",
        "-v",
        "-e",
        "--stop-timeout",
        "--add-host",
    }


REFUSED_RUNS = {
    "privileged": _with(GOOD_RUN, "--privileged"),
    "network": _with(GOOD_RUN, "--network", "host"),
    "network joined": _with(GOOD_RUN, "--network=host"),
    "entrypoint": _with(GOOD_RUN, "--entrypoint", "sh"),
    "device": _with(GOOD_RUN, "--device", "/dev/bus/usb"),
    "no publish": _without(GOOD_RUN, ("-p", "8080:8080")),
    "two publishes": _with(GOOD_RUN, "-p", "9090:8080"),
    "wrong container port": [
        "9090:80" if token == "8080:8080" else token for token in GOOD_RUN
    ],
    "publish without a host side": [
        "8080" if token == "8080:8080" else token for token in GOOD_RUN
    ],
    "no config mount": _without(GOOD_RUN, ("-v", f"$(pwd)/config:{CONFIG_TARGET}")),
    "two config mounts": _with(GOOD_RUN, "-v", f"/srv/other:{CONFIG_TARGET}"),
    "config mount propagation": [
        f"$(pwd)/config:{CONFIG_TARGET}:rshared"
        if token == f"$(pwd)/config:{CONFIG_TARGET}"
        else token
        for token in GOOD_RUN
    ],
    "no data volume": _without(GOOD_RUN, ("-v", f"saneless-data:{DATA_TARGET}")),
    "data as a host path": [
        f"/srv/data:{DATA_TARGET}" if token == f"saneless-data:{DATA_TARGET}" else token
        for token in GOOD_RUN
    ],
    "host root mounted": _with(GOOD_RUN, "-v", "/:/host"),
    "docker socket mounted": _with(
        GOOD_RUN, "-v", "/var/run/docker.sock:/var/run/docker.sock"
    ),
    "no stop timeout": _without(GOOD_RUN, ("--stop-timeout", "90")),
    "stop timeout not a number": [
        "ninety" if token == "90" else token for token in GOOD_RUN
    ],
    "newline in a token": [
        "saneless\n--privileged" if token == "saneless" else token for token in GOOD_RUN
    ],
    "host variable passed through": _with(GOOD_RUN, "-e", "GITHUB_TOKEN"),
    "command after the image": [*GOOD_RUN, "sh"],
    "no image": GOOD_RUN[:-1],
    "flag missing its value": [*GOOD_RUN[:-1], "--name"],
    "another image": [*GOOD_RUN[:-1], "docker.io/library/nginx:1.27"],
    "not a run": ["docker", "exec", *GOOD_RUN[2:]],
}


@pytest.mark.parametrize("argv", REFUSED_RUNS.values(), ids=REFUSED_RUNS.keys())
def test_a_command_outside_the_safe_shape_is_refused(argv: list[str]) -> None:
    """A flag, mount or shape the smoke run must not execute is refused."""
    assert argv != GOOD_RUN
    with pytest.raises(SmokeFailure):
        parse_documented_run(argv)


# ---------------------------------------------------------------------------
# Substitution
# ---------------------------------------------------------------------------


def test_substitution_swaps_only_the_run_specific_values() -> None:
    """Image, port, config dir, volume and name change; everything else stays."""
    substituted = substitute_documented_run(
        GOOD_RUN,
        image="saneless:ci",
        config_dir="/srv/smoke-config",
        name="saneless-smoke-1",
        volume="saneless-smoke-data-1",
    )
    assert substituted == [
        "run",
        "-d",
        "--name",
        "saneless-smoke-1",
        "-p",
        "127.0.0.1::8080",
        "--stop-timeout",
        "90",
        "-v",
        f"/srv/smoke-config:{CONFIG_TARGET}:z",
        "-v",
        f"saneless-smoke-data-1:{DATA_TARGET}",
        "-e",
        f"{SCANNER_HOST_ENV}192.168.1.50",
        "saneless:ci",
    ]


def test_substitution_adds_detach_and_a_name_and_keeps_mount_options() -> None:
    """A run without ``-d`` or ``--name`` gains both; ``ro`` becomes ``ro,z``."""
    argv = [
        "docker",
        "run",
        "-p=8080:8080",
        "--stop-timeout",
        "90",
        "-v",
        f"$(pwd)/config:{CONFIG_TARGET}:ro",
        f"-v=saneless-data:{DATA_TARGET}",
        "--add-host=host.docker.internal:host-gateway",
        SEED_IMAGE,
    ]
    substituted = substitute_documented_run(
        argv, image="saneless:ci", config_dir="/c", name="n", volume="v"
    )
    assert substituted == [
        "run",
        "-d",
        "--name",
        "n",
        "-p=127.0.0.1::8080",
        "--stop-timeout",
        "90",
        "-v",
        f"/c:{CONFIG_TARGET}:ro,z",
        f"-v=v:{DATA_TARGET}",
        "--add-host=host.docker.internal:host-gateway",
        "saneless:ci",
    ]


def test_substitution_validates_first() -> None:
    """A command the validator refuses is never substituted."""
    with pytest.raises(SmokeFailure):
        substitute_documented_run(
            _with(GOOD_RUN, "--privileged"),
            image="saneless:ci",
            config_dir="/c",
            name="n",
            volume="v",
        )


# ---------------------------------------------------------------------------
# The real pages
# ---------------------------------------------------------------------------


def test_readme_and_quick_start_show_the_same_complete_run() -> None:
    """
    README and Quick Start each show one complete run, and it is the same one.

    Both pages are the first thing a container user pastes, and the smoke run
    executes both, so they must agree, and the one command must carry
    everything a working appliance needs: a name to ``docker exec`` into, a
    detached start, the data volume, the scanner host and the stop budget,
    on the image tag ``pyproject.toml`` publishes.
    """
    readme = documented_docker_runs(README.read_text(encoding="utf-8"))
    quick = documented_docker_runs(QUICK_START.read_text(encoding="utf-8"))
    assert len(readme) == 1, f"README.md shows {len(readme)} runs: {readme}"
    assert len(quick) == 1, f"quick-start.md shows {len(quick)} runs: {quick}"
    assert readme == quick, (
        f"README.md and quick-start.md disagree:\n{readme[0]}\n{quick[0]}"
    )
    run = parse_documented_run(readme[0])
    assert run.name
    assert run.detach
    assert run.data_volume
    assert any(item.startswith(SCANNER_HOST_ENV) for item in run.env), run.env
    assert run.stop_timeout == "90"
    assert run.image == f"{IMAGE_NAME}:{_declared_version()}"
