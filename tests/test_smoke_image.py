"""
The documented container commands must be readable and safe to run.

The image smoke run takes the ``docker run`` a reader is told to paste from
README.md and from the Quick Start, rather than keeping its own copy, so a
documented command that stops working turns CI red. That text is editable by
anyone who can open a pull request, and CI hands it to a container engine, so
the smoke script reads it with ``shlex`` (never a shell),
holds it to an allow-list of flags and mounts, and only then swaps in the
values a test run needs: the image under test, a loopback port, a scratch
config directory, a throwaway data volume and a unique container name.

These tests pin that contract without a container engine: what the extractor
finds in a page, every shape the validator refuses, what the substitution
changes and leaves alone, and that README and Quick Start show the same,
complete command. They also pin the pieces the smoke run starts the command
with: the fake paperless-ngx it serves and the token it must see, the Scan
button reader, where the container finds the host, and how the argv reaches
the engine.
"""

from __future__ import annotations

import http.client
import json
import socket
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from scripts.smoke_image import (
    ALLOWED_RUN_FLAGS,
    DocumentedRun,
    Engine,
    FakeHit,
    SmokeFailure,
    _argv_lines,
    _host_endpoint,
    _remove_leftovers,
    documented_docker_runs,
    fake_paperless,
    parse_documented_run,
    require_token_seen,
    scan_button_enabled,
    substitute_documented_run,
)

if TYPE_CHECKING:
    import subprocess

    from tests.conftest import SocketGuard

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
QUICK_START = REPO_ROOT / "docs" / "getting-started" / "quick-start.md"
WHICH_SETUP = REPO_ROOT / "docs" / "getting-started" / "which-setup.md"
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


def test_a_hash_inside_a_word_stays_in_the_word() -> None:
    """``A=b#c`` is one word, as in a shell; a ``#`` there starts no comment."""
    text = f"```bash\ndocker run -e A=b#c -p 8080:8080 {SEED_IMAGE}\n```\n"
    assert documented_docker_runs(text) == [
        ["docker", "run", "-e", "A=b#c", "-p", "8080:8080", SEED_IMAGE],
    ]


def test_a_whole_line_comment_before_the_run_is_skipped() -> None:
    """A comment line in the block is not part of the command after it."""
    text = (
        "```bash\n"
        "  # Start it in the background:\n"
        f"docker run -p 8080:8080 {SEED_IMAGE}\n"
        "```\n"
    )
    assert documented_docker_runs(text) == [
        ["docker", "run", "-p", "8080:8080", SEED_IMAGE],
    ]


@pytest.mark.parametrize(
    "command",
    [
        f"docker run -p 8080:8080 {SEED_IMAGE}  # serves the UI",
        f"docker run -p 8080:8080 #-v x:/y \\\n  {SEED_IMAGE}",
    ],
    ids=["trailing comment", "comment inside a continuation"],
)
def test_a_comment_inside_a_command_is_refused(command: str) -> None:
    """A mid-command comment hides words, so the command is refused."""
    text = f"```bash\n{command}\n```\n"
    with pytest.raises(SmokeFailure, match="comment"):
        documented_docker_runs(text)


def test_a_quoted_hash_is_not_a_comment() -> None:
    """A word that opens with a quote holds its ``#`` literally."""
    text = f"```bash\ndocker run -e 'A=#x' -e \"#y\" {SEED_IMAGE}\n```\n"
    assert documented_docker_runs(text) == [
        ["docker", "run", "-e", "A=#x", "-e", "#y", SEED_IMAGE],
    ]


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


def test_real_readme_and_quick_start_show_the_same_complete_run() -> None:
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


def test_real_which_setup_runs_have_the_quick_start_shape() -> None:
    """
    The setup-shape runs pass the allow-list and match README's shape.

    Quick Start, which these shapes lead to, runs ``docker exec saneless``,
    so each run must start detached under that name. The smoke run does not
    execute these commands, so this is where their flags are validated.
    """
    runs = documented_docker_runs(WHICH_SETUP.read_text(encoding="utf-8"))
    assert len(runs) == 2, f"which-setup.md shows {len(runs)} runs: {runs}"
    parsed = [parse_documented_run(argv) for argv in runs]
    for run in parsed:
        assert run.name == "saneless", run
        assert run.detach, run
        assert run.data_volume, run
        assert any(item.startswith(SCANNER_HOST_ENV) for item in run.env), run.env
        assert run.stop_timeout == "90", run
        assert run.image == f"{IMAGE_NAME}:{_declared_version()}", run
    assert parsed[0].add_hosts == ("host.docker.internal:host-gateway",)


# ---------------------------------------------------------------------------
# What the smoke run starts the command with
# ---------------------------------------------------------------------------

EMPTY_LIST = {"count": 0, "next": None, "previous": None, "results": []}


def _get(url: str, path: str, authorization: str) -> tuple[int, object]:
    """GET ``path`` from the server at ``url`` and return status and JSON body."""
    host_port = url.removeprefix("http://")
    host, _, port = host_port.rpartition(":")
    connection = http.client.HTTPConnection(host, int(port), timeout=5)
    try:
        connection.request("GET", path, headers={"Authorization": authorization})
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_the_fake_paperless_answers_a_list_and_records_the_call() -> None:
    """A tag-list GET with the token gets the empty list, and is recorded."""
    with fake_paperless("127.0.0.1", "127.0.0.1", "t") as fake:
        assert fake.url.startswith("http://127.0.0.1:")
        status, body = _get(fake.url, "/api/tags/", "Token t")
        assert (status, body) == (200, EMPTY_LIST)
        assert fake.hits == [FakeHit("GET", "/api/tags/", "Token t")]


def test_the_fake_paperless_refuses_another_token_but_records_it() -> None:
    """A wrong token is answered 401, as paperless-ngx would, and still recorded."""
    with fake_paperless("127.0.0.1", "127.0.0.1", "t") as fake:
        status, _ = _get(fake.url, "/api/correspondents/", "Token other")
        assert status == 401
        assert fake.hits == [FakeHit("GET", "/api/correspondents/", "Token other")]


def test_the_fake_paperless_stops_listening_on_exit(
    socket_guard: SocketGuard,
) -> None:
    """
    Once the context exits, nothing accepts connections on its port.

    The port is allowed through the socket guard, so the refusal comes from
    the kernel and not from the guard.
    """
    with fake_paperless("127.0.0.1", "127.0.0.1", "t") as fake:
        port = int(fake.url.rpartition(":")[2])
    socket_guard.allow_port(port)
    with (
        pytest.raises(ConnectionRefusedError),
        socket.create_connection(("127.0.0.1", port), 2),
    ):
        pass


def test_the_token_check_passes_when_the_smoke_token_arrived() -> None:
    """One request carrying the token is enough."""
    require_token_seen(
        [
            FakeHit("GET", "/api/tags/", None),
            FakeHit("GET", "/api/correspondents/", "Token smoke"),
        ],
        "smoke",
    )


@pytest.mark.parametrize(
    "hits",
    [
        [],
        [FakeHit("GET", "/api/tags/", None)],
        [FakeHit("GET", "/api/tags/", "Token another")],
        [FakeHit("GET", "/api/tags/", "Token smoke-and-more")],
        [FakeHit("GET", "/api/tags/", "Bearer smoke")],
    ],
    ids=["nothing", "no header", "another token", "longer token", "another scheme"],
)
def test_the_token_check_fails_unless_the_smoke_token_arrived(
    hits: list[FakeHit],
) -> None:
    """No request carrying ``Token <smoke token>`` means the config was not used."""
    with pytest.raises(SmokeFailure):
        require_token_seen(hits, "smoke")


@pytest.mark.parametrize(
    ("html", "enabled"),
    [
        ('<button type="submit" id="scan-btn" class="x">Scan</button>', True),
        ('<button type="submit" id="scan-btn" autofocus>Scan</button>', True),
        ('<button type="submit" id="scan-btn" disabled>Scan</button>', False),
        (
            '<button type="submit" id="scan-btn" disabled="disabled">Scan</button>',
            False,
        ),
        (
            '<div hx-swap-oob="true"><ul></ul></div>\n'
            '<button type="submit" id="scan-btn" hx-swap-oob="true"\n'
            '        aria-describedby="scan-blocked-reason"\n'
            "        disabled>\n    Scan\n</button>",
            False,
        ),
    ],
    ids=["enabled", "autofocus", "bare disabled", "valued disabled", "multi-line"],
)
def test_the_scan_button_reader_sees_disabled(html: str, *, enabled: bool) -> None:
    """Only a ``disabled`` attribute on the Scan button makes it disabled."""
    assert scan_button_enabled(html) is enabled


def test_the_scan_button_reader_ignores_other_disabled_buttons() -> None:
    """Another disabled button does not disable Scan."""
    html = (
        '<button id="retry" disabled>Retry</button>'
        '<button type="submit" id="scan-btn">Scan</button>'
    )
    assert scan_button_enabled(html) is True


@pytest.mark.parametrize(
    "html",
    ["", "<p>Scan</p>", '<input id="scan-btn" disabled>', '<button id="scan">'],
    ids=["empty", "no button", "not a button", "another id"],
)
def test_a_page_without_the_scan_button_fails(html: str) -> None:
    """No Scan button at all is a failure, never a pass."""
    with pytest.raises(SmokeFailure):
        scan_button_enabled(html)


def _engine(*, podman: bool) -> Engine:
    """Return an engine value that never runs anything."""
    return Engine(
        docker="/usr/bin/docker", image="saneless:ci", podman=podman, health_seconds=1.0
    )


def test_podman_reaches_the_host_by_name_and_the_fake_listens_everywhere() -> None:
    """Podman maps ``host.containers.internal`` to the host, not to its loopback."""
    assert _host_endpoint(_engine(podman=True), "") == ("", "host.containers.internal")


def test_docker_reaches_the_host_at_the_bridge_gateway() -> None:
    """On Docker the fake binds the bridge gateway and the container uses it."""
    assert _host_endpoint(_engine(podman=False), "172.17.0.1") == (
        "172.17.0.1",
        "172.17.0.1",
    )


def test_docker_without_a_bridge_gateway_fails() -> None:
    """No gateway means the container could not reach the fake: say so."""
    with pytest.raises(SmokeFailure):
        _host_endpoint(_engine(podman=False), "")


def test_a_cleanup_step_that_overruns_does_not_stop_the_rest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Container, volume and directory are each removed, and nothing raises."""
    calls: list[str] = []

    def overrun(
        _engine: Engine, script: str, **_values: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(script)
        msg = "engine command timed out"
        raise SmokeFailure(msg)

    monkeypatch.setattr(Engine, "sh", overrun)
    directory = tmp_path / "scratch"
    directory.mkdir()
    _remove_leftovers(_engine(podman=False), "c", volume="v", directory=directory)
    assert len(calls) == 2, calls
    assert "volume rm" in calls[1]
    assert not directory.exists()
    assert capsys.readouterr().err.count("timed out") == 2


def test_the_argv_travels_one_word_per_line() -> None:
    """Each word is one line, kept exactly, ``$(pwd)`` and spaces included."""
    argv = ["run", "-d", "-v", "$(pwd)/a b:/etc/saneless:z", "-e", "X=$HOME"]
    assert _argv_lines(argv) == "run\n-d\n-v\n$(pwd)/a b:/etc/saneless:z\n-e\nX=$HOME"


def test_a_word_with_a_newline_cannot_travel() -> None:
    """A newline would split one word into two flags, so it is refused."""
    with pytest.raises(SmokeFailure):
        _argv_lines(["run", "--name", "a\n--privileged", "saneless:ci"])
