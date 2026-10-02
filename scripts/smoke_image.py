"""
Run named smoke checks against a built saneless container image.

Usage::

    uv run --no-project python scripts/smoke_image.py IMAGE [--timeout SECONDS]

Every check starts one or more containers from ``IMAGE`` through the local
``docker`` CLI and either passes or raises ``SmokeFailure`` with the reason.
Podman's ``docker`` shim works too, for an image built with ``--format
docker``: Podman's default OCI build drops the HEALTHCHECK. Every check runs
whatever an earlier one did, so one run reports the whole picture: one line
per check, ``ok   <name>`` or ``FAIL <name>: <reason>``, then exit status 1 if
any check failed.

To extend the smoke test, write a function that takes an ``Engine`` and append
a ``Check`` for it to ``CHECKS``. The tuple's order is the order the checks run
and report in.

Child processes are started as ``/bin/sh -c 'eval "$SMOKE_SCRIPT"'``, a fixed
literal argv. The engine path, the image reference and every per-run value
(container name, mount path, the program a container runs) travel in the
environment and are double-quoted inside the command text, so no value is ever
re-split or re-parsed by the shell. The command text itself is always a literal
written in this module; ``Engine.sh`` takes it as ``LiteralString`` so the type
checkers reject anything built at run time. The obvious alternatives are both
rejected by this project's lint rules: a bare ``docker`` relying on PATH trips
ruff S607, and a resolved path (or any non-literal element) in argv trips S603.

The ``docker run`` commands the getting-started pages tell a reader to paste
are read out of those pages, not copied into this file, so a documented
command that stops working is caught. That text is editable by anyone who can
open a pull request, so it is split into words with ``shlex`` (never by a
shell), and held to an allow-list of flags and mounts before anything runs:
only the config directory and one named data volume may be mounted, and no
word may contain a newline. Only then are the values a test run needs swapped
in: the image under test, a loopback port, a scratch config directory, a
throwaway data volume and a unique container name.

Two checks run those commands, one from README.md and one from the Quick
Start, each as the page writes it apart from those values. The scratch config
names a fake paperless-ngx this script serves, and a token made for the run.
Each started container must answer ``/health``, render the Scan button
enabled, and have called the fake with that token: a failed list load also
releases Scan, so only the token on the fake's record proves the mounted
config was read. On Podman the container reaches the fake as
``host.containers.internal``; on Docker Engine, at the default bridge
network's gateway.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import uuid
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, LiteralString, override

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

# Upper bound on any single engine command, so a hung container cannot stall
# the run: the command is killed and its container removed.
_COMMAND_SECONDS = 120.0

# The first device enumeration must finish inside this budget. An unpruned
# backend list makes every backend probe for hardware it will never find.
_ENUMERATION_BUDGET_SECONDS = 1.0

# A data directory the image's UID 1000 account can create, and one the image
# never uses by default, so the database landing there proves the config file
# was read and obeyed.
_SMOKE_DATA_DIR = "/var/lib/saneless/smoke-elsewhere"
_DEFAULT_DB = "/var/lib/saneless/saneless.db"

# The only backends the image should load: network scanners through saned
# (net) and driverless eSCL/AirScan devices (escl).
_EXPECTED_BACKENDS = frozenset({"net", "escl"})

# Tools a runtime image has no use for; any of them is attack surface. pip's
# console scripts live in the base image's /usr/local/bin, which stays on PATH
# behind the venv, so an installer the Dockerfile failed to remove shows up
# here even though the venv's own interpreter cannot import it.
_FORBIDDEN_TOOLS = ("curl", "gcc", "cc", "uv", "pip", "pip3")

# Every interpreter in the runtime image. The venv's is the one PATH finds
# first, but it does not see the base image's site-packages, which is where
# pip lives if it was not removed; the base interpreter does see them.
_INTERPRETERS = ("/opt/venv/bin/python", "/usr/local/bin/python3")

_PORT = 8080
_HTTP_OK = 200
_POLL_SECONDS = 1.0
_TAIL_LINES = 20

_ENUMERATION_PROGRAM = """\
import sys
import time
import sane
sane.init()
start = time.monotonic()
sane.get_devices()
sys.stdout.write(f"{time.monotonic() - start:.2f}\\n")
"""

_DATA_DIR_PROBE = (
    f"saneless jobs && test -f {_SMOKE_DATA_DIR}/saneless.db && test ! -e {_DEFAULT_DB}"
)

_DLL_PROBE = (
    "cat /etc/sane.d/dll.conf && echo --- && "
    "{ [ ! -d /etc/sane.d/dll.d ] || ls -A /etc/sane.d/dll.d; }"
)

# Run by each interpreter in turn: it reports its own path and whether pip is
# importable from it. The program reaches the container as the probe's first
# positional argument, so it is never pasted into the shell text.
_PIP_PROGRAM = """\
import importlib.util
import sys
found = importlib.util.find_spec("pip") is not None
sys.stdout.write(f"{sys.executable} {found}\\n")
"""

# A missing interpreter fails the loop, so the probe cannot pass vacuously by
# asking an interpreter that is not there.
_PIP_SENTINEL = "pip-checked"
_PIP_PROBE = (
    f'for py in {" ".join(_INTERPRETERS)}; do "$py" -c "$1" || exit 1; done; '
    f"echo {_PIP_SENTINEL}"
)

_TOOLS_SENTINEL = "tools-checked"
_TOOLS_PROBE = (
    f'for tool in {" ".join(_FORBIDDEN_TOOLS)}; do command -v "$tool"; done; '
    f"echo {_TOOLS_SENTINEL}"
)


class SmokeFailure(Exception):
    """A smoke check found the image wrong, or could not complete."""


@dataclasses.dataclass(frozen=True)
class Engine:
    """
    The container engine the checks drive, and the image under test.

    Attributes:
        docker: Absolute path of the ``docker`` executable.
        image: The image reference given on the command line.
        podman: Whether ``docker`` is Podman's shim, which needs a different
            health probe (rootless Podman never runs scheduled healthchecks).
        health_seconds: How long to wait for the served container to answer.

    """

    docker: str
    image: str
    podman: bool
    health_seconds: float

    def sh(
        self,
        script: LiteralString,
        *,
        timeout: float = _COMMAND_SECONDS,
        **values: str,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run one literal shell command with the engine and image in scope.

        The command sees ``$SMOKE_DOCKER`` and ``$SMOKE_IMAGE``, plus
        ``$SMOKE_<KEY>`` for every keyword value. ``$SMOKE_NAME`` is always set:
        to the ``name`` value when given, otherwise to a fresh container name.
        When the command overruns ``timeout`` it is killed and the container
        called ``$SMOKE_NAME`` is removed, so nothing is left running.

        Args:
            script: Shell command text; always a literal in this module.
            timeout: Seconds before the command is killed.
            **values: Per-run values, exported as ``SMOKE_<KEY>``.

        Returns:
            The finished process, with text stdout and stderr.

        Raises:
            SmokeFailure: The command overran ``timeout``.

        """
        values.setdefault("name", _container_name())
        env = {
            **os.environ,
            "SMOKE_SCRIPT": script,
            "SMOKE_DOCKER": self.docker,
            "SMOKE_IMAGE": self.image,
            **{f"SMOKE_{key.upper()}": value for key, value in values.items()},
        }
        try:
            return subprocess.run(
                ["/bin/sh", "-c", 'eval "$SMOKE_SCRIPT"'],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            self.sh('exec "$SMOKE_DOCKER" rm -f -v "$SMOKE_NAME"', name=values["name"])
            msg = f"engine command timed out after {timeout:.0f} s"
            raise SmokeFailure(msg) from exc


@dataclasses.dataclass(frozen=True)
class Check:
    """
    One named smoke check.

    Attributes:
        name: What a pass proves, as printed in the report.
        run: Raises ``SmokeFailure`` on failure; may return a short detail
            (a measured value) to print beside a pass.

    """

    name: str
    run: Callable[[Engine], str | None]


def _container_name() -> str:
    """
    Return a container name no other run will use.

    Returns:
        A unique name, so a leftover can always be found and removed.

    """
    return f"saneless-smoke-{uuid.uuid4().hex[:12]}"


def _tail(text: str) -> str:
    """
    Keep the last lines of some output, for a failure message.

    Args:
        text: Captured output.

    Returns:
        The final lines, or a placeholder when there was none.

    """
    lines = text.strip().splitlines()[-_TAIL_LINES:]
    return "\n".join(lines) if lines else "(no output)"


def _require_success(result: subprocess.CompletedProcess[str], what: str) -> None:
    """
    Fail unless a command exited 0.

    Args:
        result: The finished command.
        what: The command's purpose, for the message.

    Raises:
        SmokeFailure: The command exited non-zero.

    """
    if result.returncode != 0:
        msg = f"{what} exited {result.returncode}\n{_tail(result.stderr)}"
        raise SmokeFailure(msg)


# ---------------------------------------------------------------------------
# The documented ``docker run`` commands
# ---------------------------------------------------------------------------

# The published image, assembled from its parts: the repository guard that
# pins every image reference to the released tag reads this file too, and a
# literal name here would read as a reference that carries no tag.
_IMAGE_REGISTRY = "ghcr.io"
_IMAGE_OWNER = "kdknigga"
_PROJECT_NAME = "saneless"
PUBLISHED_IMAGE = f"{_IMAGE_REGISTRY}/{_IMAGE_OWNER}/{_PROJECT_NAME}"

# Every flag a getting-started ``docker run`` needs, and nothing that widens
# what the container can reach: no privileges, no host network, no devices,
# no other entrypoint.
ALLOWED_RUN_FLAGS = (
    "-d",
    "--rm",
    "--name",
    "-p",
    "-v",
    "-e",
    "--stop-timeout",
    "--add-host",
)
# The allowed flags that take no value.
_SWITCH_FLAGS = frozenset({"-d", "--rm"})

_CONFIG_TARGET = "/etc/saneless"
_DATA_TARGET = "/var/lib/saneless"
# Read-only/read-write and the SELinux relabels; nothing that changes mount
# propagation.
_MOUNT_OPTIONS = frozenset({"ro", "rw", "z", "Z"})
_SELINUX_RELABELS = frozenset({"z", "Z"})
# Loopback only, on a port the engine picks.
_SMOKE_PUBLISH = f"127.0.0.1::{_PORT}"

# A Markdown code fence opener or closer, at any indentation: the
# getting-started pages nest their commands four spaces deep under a tab.
_FENCE = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})")
# A command line that starts a ``docker run``, with or without a ``$`` prompt.
_DOCKER_RUN = re.compile(r"^(?:\$\s+)?docker\s+run(?:\s|$)")
_PROMPT = re.compile(r"^\$\s+")
# A Docker object name: a named volume or a container name.
_OBJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# ``NAME=value``. A bare ``-e NAME`` would copy that variable from the
# environment of whoever runs the command, which on CI holds its secrets.
_ENV_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_ADD_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*[:=][^\s]+")
_SECONDS = re.compile(r"[0-9]+")


@dataclasses.dataclass(frozen=True)
class DocumentedRun:
    """
    A documented ``docker run``, validated and split into its parts.

    Attributes:
        publish: The ``-p`` value, ``HOST:8080``.
        config_source: The host side of the ``/etc/saneless`` mount.
        config_options: That mount's options, ``""`` when it has none.
        data_volume: The named volume mounted at ``/var/lib/saneless``.
        env: Every ``-e NAME=value``, in order.
        stop_timeout: The ``--stop-timeout`` seconds, as written.
        name: The ``--name`` value, or ``None`` when there is none.
        detach: Whether ``-d`` is given.
        image: The image reference, tag included.
        add_hosts: Every ``--add-host`` value, in order.
        remove: Whether ``--rm`` is given.

    """

    publish: str
    config_source: str
    config_options: str
    data_volume: str
    env: tuple[str, ...]
    stop_timeout: str
    name: str | None
    detach: bool
    image: str
    add_hosts: tuple[str, ...] = ()
    remove: bool = False


@dataclasses.dataclass(frozen=True)
class _Option:
    """
    One flag of a documented command, where it sits, and its value.

    Attributes:
        index: Position of the flag's word in the argv.
        flag: The flag, without any ``=value``.
        value: Its value, or ``None`` for a switch.
        joined: Whether the value is in the flag's own word, after ``=``.

    """

    index: int
    flag: str
    value: str | None
    joined: bool


def _refuse(argv: list[str], reason: str) -> SmokeFailure:
    """
    Build the failure for a documented command the smoke run will not execute.

    Args:
        argv: The documented command.
        reason: What is wrong with it.

    Returns:
        The exception to raise.

    """
    return SmokeFailure(f"documented command {shlex.join(argv)!r}: {reason}")


def _is_published_image(word: str) -> bool:
    """
    Say whether a word names the published image, by tag or digest.

    Args:
        word: One word of a command.

    Returns:
        True for the bare name, ``NAME:tag`` or ``NAME@digest``.

    """
    return word == PUBLISHED_IMAGE or word.startswith(
        (f"{PUBLISHED_IMAGE}:", f"{PUBLISHED_IMAGE}@")
    )


def _strip_indent(line: str, indent: int) -> str:
    """
    Remove up to ``indent`` characters of leading whitespace from a line.

    Args:
        line: A line inside a code fence.
        indent: The indentation of the fence that opened it.

    Returns:
        The line as it reads at the fence's own indentation.

    """
    leading = len(line) - len(line.lstrip(" \t"))
    return line[min(leading, indent) :]


def _fenced_blocks(text: str) -> list[str]:
    """
    Return the body of every fenced code block in a Markdown page.

    Args:
        text: The page.

    Returns:
        Each block's lines, dedented to the fence's indentation and joined.

    """
    lines = text.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        opener = _FENCE.match(lines[index])
        index += 1
        if opener is None:
            continue
        indent = len(opener["indent"])
        mark = opener["fence"]
        body: list[str] = []
        while index < len(lines):
            stripped = lines[index].strip()
            index += 1
            if len(stripped) >= len(mark) and set(stripped) == {mark[0]}:
                break
            body.append(_strip_indent(lines[index - 1], indent))
        blocks.append("\n".join(body))
    return blocks


def _block_commands(block: str) -> list[str]:
    """
    Return each command in a code block, backslash continuations joined.

    Args:
        block: A fenced block's body.

    Returns:
        One string per command, in order.

    """
    commands: list[str] = []
    parts: list[str] = []
    for line in block.splitlines():
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            parts.append(stripped[:-1])
            continue
        parts.append(stripped)
        commands.append(" ".join(parts).strip())
        parts = []
    if parts:
        commands.append(" ".join(parts).strip())
    return commands


def documented_docker_runs(text: str) -> list[list[str]]:
    """
    Read every fenced ``docker run`` of the published image out of a page.

    Fences at any indentation count, so a command inside a tab is found.
    Each command is split into words by ``shlex``, the way a POSIX shell
    would, but nothing is expanded or executed: ``"$(pwd)/config"`` comes
    back as the literal word ``$(pwd)/config``. Runs of other images,
    ``docker compose`` blocks and commands outside a fence are ignored.

    Args:
        text: A Markdown page.

    Returns:
        One argv per command, each starting ``["docker", "run", ...]``, in
        page order.

    Raises:
        SmokeFailure: A command naming the image cannot be split into words,
            so what it would run cannot be known.

    """
    runs: list[list[str]] = []
    for block in _fenced_blocks(text):
        for command in _block_commands(block):
            if not _DOCKER_RUN.match(command) or PUBLISHED_IMAGE not in command:
                continue
            try:
                argv = shlex.split(_PROMPT.sub("", command), comments=True)
            except ValueError as exc:
                msg = f"cannot split the documented command {command!r}: {exc}"
                raise SmokeFailure(msg) from exc
            if any(_is_published_image(word) for word in argv):
                runs.append(argv)
    return runs


def _run_options(argv: list[str]) -> tuple[list[_Option], int]:
    """
    Walk a documented command's flags, refusing any outside the allow-list.

    Args:
        argv: The documented command, ``["docker", "run", ...]``.

    Returns:
        Every flag in order, and the position of the image, which is the
        last word.

    Raises:
        SmokeFailure: The command is not a ``docker run``, a word holds a
            newline, a flag is not allowed or lacks its value, or the last
            word is not the published image.

    """
    if argv[:2] != ["docker", "run"]:
        raise _refuse(argv, "is not a `docker run`")
    if any("\n" in word for word in argv):
        raise _refuse(argv, "a word contains a newline")
    options: list[_Option] = []
    index = 2
    while index < len(argv) and argv[index].startswith("-"):
        flag, joined, value = argv[index].partition("=")
        if flag not in ALLOWED_RUN_FLAGS:
            raise _refuse(argv, f"{flag} is not an allowed flag")
        if flag in _SWITCH_FLAGS:
            if joined:
                raise _refuse(argv, f"{flag} takes no value")
            options.append(_Option(index, flag, None, joined=False))
            index += 1
        elif joined:
            options.append(_Option(index, flag, value, joined=True))
            index += 1
        elif index + 1 < len(argv):
            options.append(_Option(index, flag, argv[index + 1], joined=False))
            index += 2
        else:
            raise _refuse(argv, f"{flag} has no value")
    if index != len(argv) - 1:
        raise _refuse(argv, "the image must be the last word, with no command")
    if not _is_published_image(argv[index]):
        raise _refuse(argv, f"{argv[index]!r} is not the published image")
    return options, index


def _values(options: list[_Option], flag: str) -> list[str]:
    """
    Return the values given to one flag, in order.

    Args:
        options: The command's flags.
        flag: The flag to collect.

    Returns:
        Each value, ``""`` for a switch.

    """
    return [option.value or "" for option in options if option.flag == flag]


def _split_mount(argv: list[str], value: str) -> tuple[str, str, str]:
    """
    Split a ``-v`` value into its source, target and options.

    Args:
        argv: The documented command, for the failure message.
        value: ``SOURCE:TARGET`` or ``SOURCE:TARGET:OPTIONS``.

    Returns:
        The source, the target and the options (``""`` when none).

    Raises:
        SmokeFailure: The value has another shape, or an option outside the
            read-only, read-write and SELinux relabel options.

    """
    parts = value.split(":")
    if len(parts) not in {2, 3} or not all(parts[:2]):
        raise _refuse(argv, f"cannot read the mount {value!r}")
    source, target = parts[:2]
    options = parts[2] if len(parts) == 3 else ""
    if options and not set(options.split(",")) <= _MOUNT_OPTIONS:
        raise _refuse(argv, f"mount option {options!r} is not allowed")
    return source, target, options


def _mounts(argv: list[str], options: list[_Option]) -> tuple[str, str, str]:
    """
    Check the command mounts the config directory and one named data volume.

    Args:
        argv: The documented command.
        options: Its flags.

    Returns:
        The config mount's source and options, and the data volume's name.

    Raises:
        SmokeFailure: Either mount is missing or repeated, the data mount is
            not a named volume, or anything else is mounted.

    """
    config: list[tuple[str, str]] = []
    data: list[str] = []
    for value in _values(options, "-v"):
        source, target, mount_options = _split_mount(argv, value)
        if target == _CONFIG_TARGET:
            config.append((source, mount_options))
        elif target == _DATA_TARGET and _OBJECT_NAME.fullmatch(source):
            data.append(source)
        elif target == _DATA_TARGET:
            raise _refuse(argv, f"{_DATA_TARGET} must be a named volume")
        else:
            raise _refuse(argv, f"mounting {target!r} is not allowed")
    if len(config) != 1:
        raise _refuse(argv, f"needs exactly one mount at {_CONFIG_TARGET}")
    if len(data) != 1:
        raise _refuse(argv, f"needs exactly one named volume at {_DATA_TARGET}")
    return config[0][0], config[0][1], data[0]


def _single(argv: list[str], options: list[_Option], flag: str) -> str:
    """
    Return the value of a flag the command must give exactly once.

    Args:
        argv: The documented command.
        options: Its flags.
        flag: The flag.

    Returns:
        Its value.

    Raises:
        SmokeFailure: The flag is missing or repeated.

    """
    values = _values(options, flag)
    if len(values) != 1:
        raise _refuse(argv, f"needs exactly one {flag}, found {len(values)}")
    return values[0]


def parse_documented_run(argv: list[str]) -> DocumentedRun:
    """
    Validate a documented ``docker run`` and split it into its parts.

    The command must publish container port 8080 exactly once, mount the
    config directory at ``/etc/saneless`` and one named volume at
    ``/var/lib/saneless`` and nothing else, give ``--stop-timeout`` in
    seconds, pass only ``NAME=value`` variables, use only the flags in
    ``ALLOWED_RUN_FLAGS``, and end with the published image.

    Args:
        argv: The command as ``documented_docker_runs`` returns it.

    Returns:
        The command's parts.

    Raises:
        SmokeFailure: The command has any other shape.

    """
    options, image_index = _run_options(argv)
    publish = _single(argv, options, "-p")
    host, colon, container = publish.rpartition(":")
    if not colon or not host or container.removesuffix("/tcp") != str(_PORT):
        raise _refuse(argv, f"-p {publish} must publish container port {_PORT}")
    config_source, config_options, data_volume = _mounts(argv, options)
    stop_timeout = _single(argv, options, "--stop-timeout")
    if not _SECONDS.fullmatch(stop_timeout):
        raise _refuse(argv, f"--stop-timeout {stop_timeout!r} is not seconds")
    names = _values(options, "--name")
    if len(names) > 1 or not all(_OBJECT_NAME.fullmatch(name) for name in names):
        raise _refuse(argv, f"cannot use the container name(s) {names}")
    env = _values(options, "-e")
    if bad := [item for item in env if not _ENV_ASSIGNMENT.fullmatch(item)]:
        raise _refuse(argv, f"-e must assign a value: {bad}")
    add_hosts = _values(options, "--add-host")
    if bad := [item for item in add_hosts if not _ADD_HOST.fullmatch(item)]:
        raise _refuse(argv, f"cannot read --add-host {bad}")
    return DocumentedRun(
        publish=publish,
        config_source=config_source,
        config_options=config_options,
        data_volume=data_volume,
        env=tuple(env),
        stop_timeout=stop_timeout,
        name=names[0] if names else None,
        detach=bool(_values(options, "-d")),
        image=argv[image_index],
        add_hosts=tuple(add_hosts),
        remove=bool(_values(options, "--rm")),
    )


def _substituted(
    option: _Option, *, config_dir: str, name: str, volume: str
) -> str | None:
    """
    Return the value a smoke run gives one flag, or ``None`` to keep it.

    Args:
        option: One flag of a validated command.
        config_dir: The scratch config directory.
        name: The container name.
        volume: The data volume name.

    Returns:
        The replacement value, or ``None`` when the documented one stays.

    """
    if option.value is None:
        return None
    if option.flag == "-p":
        return _SMOKE_PUBLISH
    if option.flag == "--name":
        return name
    if option.flag == "-v":
        return _substituted_mount(option.value, config_dir=config_dir, volume=volume)
    return None


def _substituted_mount(value: str, *, config_dir: str, volume: str) -> str:
    """
    Return the ``-v`` value a smoke run uses in place of a validated one.

    Args:
        value: The documented ``SOURCE:TARGET[:OPTIONS]``.
        config_dir: The scratch config directory.
        volume: The data volume name.

    Returns:
        The config mount on ``config_dir`` with ``z`` in its options, or the
        data mount on ``volume`` with its options kept.

    """
    _, target, *rest = value.split(":")
    options = rest[0] if rest else ""
    if target != _CONFIG_TARGET:
        return f"{volume}:{target}" + (f":{options}" if options else "")
    if not _SELINUX_RELABELS & set(options.split(",")):
        options = f"{options},z" if options else "z"
    return f"{config_dir}:{target}:{options}"


def substitute_documented_run(
    run_argv: list[str], *, image: str, config_dir: str, name: str, volume: str
) -> list[str]:
    """
    Turn a documented ``docker run`` into the one a smoke run executes.

    The command is validated first. Then, in place: the image becomes
    ``image``; the port publish becomes loopback-only on a port the engine
    picks; the config mount's host side becomes ``config_dir`` with the
    SELinux ``z`` relabel added to its options; the data volume becomes
    ``volume``; the container name becomes ``name``. ``-d`` and ``--name``
    are added after ``run`` when the page leaves them out, so the container
    can always be found and removed. Every other word is kept, in order.

    Args:
        run_argv: The command as ``documented_docker_runs`` returns it.
        image: The image under test.
        config_dir: The scratch config directory to mount.
        name: A container name no other run uses.
        volume: A data volume name no other run uses.

    Returns:
        The argv to hand the engine, after ``docker``: it starts with
        ``run``.

    Raises:
        SmokeFailure: The command fails validation.

    """
    parse_documented_run(run_argv)
    options, image_index = _run_options(run_argv)
    words = list(run_argv)
    for option in options:
        value = _substituted(option, config_dir=config_dir, name=name, volume=volume)
        if value is None:
            continue
        if option.joined:
            words[option.index] = f"{option.flag}={value}"
        else:
            words[option.index + 1] = value
    words[image_index] = image
    prefix = ["run"]
    if not _values(options, "-d"):
        prefix.append("-d")
    if not _values(options, "--name"):
        prefix.extend(["--name", name])
    return [*prefix, *words[2:]]


def check_help(engine: Engine) -> None:
    """
    Check that the image's entry point runs and prints the CLI usage.

    Args:
        engine: The engine and image under test.

    Raises:
        SmokeFailure: ``--help`` failed or printed no usage line.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" "$SMOKE_IMAGE" --help'
    )
    _require_success(result, "saneless --help")
    if "Usage: saneless" not in result.stdout:
        msg = f"no 'Usage: saneless' in the output\n{_tail(result.stdout)}"
        raise SmokeFailure(msg)


def check_uid(engine: Engine) -> str:
    """
    Check that the image runs as UID 1000, never as root.

    Args:
        engine: The engine and image under test.

    Returns:
        The UID observed.

    Raises:
        SmokeFailure: The container runs as any other user.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" --entrypoint id "$SMOKE_IMAGE" -u'
    )
    _require_success(result, "id -u")
    uid = result.stdout.strip()
    if uid != "1000":
        msg = f"the container runs as UID {uid!r}, expected 1000"
        raise SmokeFailure(msg)
    return f"uid {uid}"


def check_enumeration(engine: Engine) -> str:
    """
    Check that the first ``sane.get_devices()`` in a fresh container is fast.

    Args:
        engine: The engine and image under test.

    Returns:
        The measured enumeration time.

    Raises:
        SmokeFailure: Enumeration failed or took the budget or longer.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" '
        '--entrypoint python "$SMOKE_IMAGE" -c "$SMOKE_PROGRAM"',
        program=_ENUMERATION_PROGRAM,
    )
    _require_success(result, "the enumeration program")
    try:
        elapsed = float(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        msg = f"no timing in the output\n{_tail(result.stdout)}"
        raise SmokeFailure(msg) from exc
    measured = f"first get_devices() took {elapsed:.2f} s"
    if elapsed >= _ENUMERATION_BUDGET_SECONDS:
        msg = f"{measured}, budget {_ENUMERATION_BUDGET_SECONDS:.1f} s"
        raise SmokeFailure(msg)
    return measured


def _write_config(directory: Path) -> None:
    """
    Write a config file that moves the data directory, readable by any UID.

    ``mkdtemp`` creates the directory ``0700``, and the container's UID 1000
    is not the host user on a CI runner, nor under rootless Podman's mapping,
    so both the directory and the file are opened up for reading.

    Args:
        directory: An empty directory to mount at ``/etc/saneless``.

    """
    config = directory / "saneless.toml"
    config.write_text(f'[output]\ndata_dir = "{_SMOKE_DATA_DIR}"\n', encoding="utf-8")
    directory.chmod(0o755)
    config.chmod(0o644)


def check_config_data_dir(engine: Engine) -> None:
    """
    Check that a mounted ``saneless.toml`` ``[output] data_dir`` takes effect.

    Args:
        engine: The engine and image under test.

    Raises:
        SmokeFailure: The database did not land in the configured directory,
            or also landed in the default one.

    """
    directory = Path(tempfile.mkdtemp(prefix="saneless-smoke-"))
    try:
        _write_config(directory)
        # :z relabels the directory for SELinux hosts; elsewhere it is a no-op.
        result = engine.sh(
            'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" '
            '-v "$SMOKE_CONFIG_DIR:/etc/saneless:ro,z" '
            '--entrypoint sh "$SMOKE_IMAGE" -c "$SMOKE_PROBE"',
            config_dir=str(directory),
            probe=_DATA_DIR_PROBE,
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    if result.returncode != 0:
        msg = (
            f"with data_dir = {_SMOKE_DATA_DIR!r} in /etc/saneless/saneless.toml, "
            f"the database did not land only there (exit {result.returncode})\n"
            f"{_tail(result.stderr)}"
        )
        raise SmokeFailure(msg)


def check_no_cannot_write(engine: Engine) -> None:
    """
    Check that a one-shot command warns about no unwritable path.

    Args:
        engine: The engine and image under test.

    Raises:
        SmokeFailure: ``saneless jobs`` failed or printed ``Cannot write``.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" "$SMOKE_IMAGE" jobs'
    )
    _require_success(result, "saneless jobs")
    if "Cannot write" in result.stderr:
        msg = f"saneless jobs warned about an unwritable path\n{_tail(result.stderr)}"
        raise SmokeFailure(msg)


def _archived_healthcheck(archive: Path) -> list[str] | None:
    """
    Read the HEALTHCHECK test from an image saved as an OCI archive.

    Args:
        archive: The ``oci-archive`` file the engine wrote.

    Returns:
        The HEALTHCHECK ``Test`` array, or ``None`` when the image has none.

    Raises:
        SmokeFailure: The archive is not a single-image OCI layout.

    """

    def blob(tar: tarfile.TarFile, digest: str) -> dict[str, Any]:
        member = tar.extractfile(f"blobs/sha256/{digest.removeprefix('sha256:')}")
        if member is None:
            msg = f"no blob {digest} in the saved image"
            raise SmokeFailure(msg)
        return json.load(member)

    try:
        with tarfile.open(archive) as tar:
            member = tar.extractfile("index.json")
            if member is None:
                msg = "no index.json in the saved image"
                raise SmokeFailure(msg)
            index = json.load(member)
            manifest = blob(tar, index["manifests"][0]["digest"])
            config = blob(tar, manifest["config"]["digest"])
    except (KeyError, IndexError, tarfile.TarError, json.JSONDecodeError) as exc:
        msg = f"cannot read the saved image: {exc!r}"
        raise SmokeFailure(msg) from exc
    test = config.get("config", {}).get("Healthcheck", {}).get("Test")
    return test if isinstance(test, list) else None


def _podman_health_cmd(engine: Engine) -> str | None:
    """
    Recover an image HEALTHCHECK that Podman cannot see, as ``--health-cmd``.

    Podman applies a HEALTHCHECK only from a Docker-format image config. An
    OCI-format image (what a registry push from BuildKit produces) still
    carries the HEALTHCHECK in its config blob, but Podman drops it, so the
    container would have none. In that case the image is saved as an OCI
    archive and its own HEALTHCHECK test is read back from the config blob, to
    be passed to ``run`` verbatim. The probe is still the image's own command;
    nothing here writes one.

    A Podman build in its default OCI format is the other case: Podman
    discards the HEALTHCHECK at build time, so the config blob has none
    either. That image cannot pass the check, and waiting out the health
    deadline on it would only report the engine's banner, so it is refused at
    once with the way to rebuild it.

    Args:
        engine: The Podman engine and the image under test.

    Returns:
        The image's HEALTHCHECK test as a JSON array, or ``None`` when Podman
        already sees it.

    Raises:
        SmokeFailure: The image could not be inspected or saved, or it
            carries no HEALTHCHECK at all.

    """
    seen = engine.sh(
        'exec "$SMOKE_DOCKER" image inspect --format "{{json .HealthCheck}}" '
        '"$SMOKE_IMAGE"'
    )
    _require_success(seen, "inspecting the image's healthcheck")
    if seen.stdout.strip() not in {"", "null"}:
        return None
    with tempfile.TemporaryDirectory(prefix="saneless-smoke-") as scratch:
        archive = Path(scratch) / "image.tar"
        saved = engine.sh(
            'exec "$SMOKE_DOCKER" save --format oci-archive -o "$SMOKE_ARCHIVE" '
            '"$SMOKE_IMAGE"',
            archive=str(archive),
        )
        _require_success(saved, "saving the image to read its healthcheck")
        test = _archived_healthcheck(archive)
    if not test:
        msg = (
            "the image carries no HEALTHCHECK. A Podman build drops it unless "
            "the image is built in Docker format: rebuild with "
            "`docker build --format docker` (or `podman build --format docker`)"
        )
        raise SmokeFailure(msg)
    return json.dumps(test)


@contextlib.contextmanager
def _served(engine: Engine, health_cmd: str | None = None) -> Generator[str]:
    """
    Run the image's default command detached, and always remove it after.

    The loopback-only port publish keeps the server off every other interface
    of the host running the checks.

    Args:
        engine: The engine and image under test.
        health_cmd: The image's own HEALTHCHECK test, when the engine could
            not read it from the image itself.

    Yields:
        The container's name.

    Raises:
        SmokeFailure: The container did not start.

    """
    name = _container_name()
    try:
        if health_cmd is None:
            started = engine.sh(
                'exec "$SMOKE_DOCKER" run -d --name "$SMOKE_NAME" '
                '--health-interval=1s -p "127.0.0.1::$SMOKE_PORT" "$SMOKE_IMAGE"',
                name=name,
                port=str(_PORT),
            )
        else:
            started = engine.sh(
                'exec "$SMOKE_DOCKER" run -d --name "$SMOKE_NAME" '
                '--health-cmd "$SMOKE_HEALTH_CMD" --health-interval=1s '
                '-p "127.0.0.1::$SMOKE_PORT" "$SMOKE_IMAGE"',
                name=name,
                port=str(_PORT),
                health_cmd=health_cmd,
            )
        _require_success(started, "starting the served container")
        yield name
    finally:
        engine.sh('exec "$SMOKE_DOCKER" rm -f -v "$SMOKE_NAME"', name=name)


def _logs(engine: Engine, name: str) -> str:
    """
    Fetch the end of a container's log, for a failure message.

    Args:
        engine: The engine the container runs under.
        name: The container's name.

    Returns:
        The last log lines, stdout and stderr merged.

    """
    result = engine.sh('exec "$SMOKE_DOCKER" logs "$SMOKE_NAME" 2>&1', name=name)
    return _tail(result.stdout)


def _health_probe(engine: Engine, name: str) -> tuple[bool, str]:
    """
    Ask the engine once whether the image's own HEALTHCHECK passes.

    Docker runs the HEALTHCHECK on its schedule, so its reported status is
    read. Rootless Podman never runs it on a schedule (there are no systemd
    timers), so the probe asks Podman to run it once instead.

    Args:
        engine: The engine the container runs under.
        name: The container's name.

    Returns:
        Whether the container is healthy, and what the engine said.

    """
    if engine.podman:
        result = engine.sh(
            'exec "$SMOKE_DOCKER" healthcheck run "$SMOKE_NAME"', name=name
        )
        return result.returncode == 0, (result.stdout + result.stderr).strip()
    result = engine.sh(
        'exec "$SMOKE_DOCKER" inspect --format "{{json .State.Health}}" "$SMOKE_NAME"',
        name=name,
    )
    said = (result.stdout + result.stderr).strip()
    return result.returncode == 0 and '"Status":"healthy"' in said, said


def check_healthcheck(engine: Engine) -> str:
    """
    Check that the image's own HEALTHCHECK reports the server healthy.

    Args:
        engine: The engine and image under test.

    Returns:
        How long the container took to become healthy.

    Raises:
        SmokeFailure: It was not healthy before the deadline.

    """
    health_cmd = _podman_health_cmd(engine) if engine.podman else None
    with _served(engine, health_cmd) as name:
        start = time.monotonic()
        deadline = start + engine.health_seconds
        said = ""
        while time.monotonic() < deadline:
            healthy, said = _health_probe(engine, name)
            if healthy:
                return f"healthy after {time.monotonic() - start:.1f} s"
            time.sleep(_POLL_SECONDS)
        msg = (
            f"not healthy within {engine.health_seconds:.0f} s; last health "
            f"report: {said or '(none)'}\ncontainer log:\n{_logs(engine, name)}"
        )
        raise SmokeFailure(msg)


def _host_port(engine: Engine, name: str) -> int:
    """
    Read the loopback port the engine published the server on.

    Args:
        engine: The engine the container runs under.
        name: The container's name.

    Returns:
        The host port mapped to the container's web port.

    Raises:
        SmokeFailure: The engine reported no usable mapping.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" port "$SMOKE_NAME" "$SMOKE_PORT"',
        name=name,
        port=str(_PORT),
    )
    _require_success(result, "reading the published port")
    lines = result.stdout.strip().splitlines()
    try:
        return int(lines[0].rsplit(":", 1)[1])
    except (IndexError, ValueError) as exc:
        msg = f"no host port in {result.stdout.strip()!r}"
        raise SmokeFailure(msg) from exc


def _health_status(port: int) -> int:
    """
    GET ``/health`` once from the host.

    Args:
        port: The loopback port the server is published on.

    Returns:
        The HTTP status code.

    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/health")
        return connection.getresponse().status
    finally:
        connection.close()


def check_host_health(engine: Engine) -> str:
    """
    Check that a host GET of the published ``/health`` returns 200.

    Args:
        engine: The engine and image under test.

    Returns:
        How long the server took to answer 200.

    Raises:
        SmokeFailure: No 200 arrived before the deadline.

    """
    with _served(engine) as name:
        port = _host_port(engine, name)
        try:
            healthy = _wait_for_health(engine, port)
        except SmokeFailure as exc:
            msg = f"{exc}\ncontainer log:\n{_logs(engine, name)}"
            raise SmokeFailure(msg) from exc
        return f"200 after {healthy:.1f} s"


# ---------------------------------------------------------------------------
# Running the documented ``docker run`` commands
# ---------------------------------------------------------------------------

# The repository the script lives in: the documented commands are read from
# its pages, never from a copy here.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_README = Path("README.md")
_QUICK_START = Path("docs") / "getting-started" / "quick-start.md"

# How a container under Podman finds the host it runs on.
_PODMAN_HOST = "host.containers.internal"
_HTTP_UNAUTHORIZED = 401
_REQUEST_SECONDS = 10.0
_SCAN_BUTTON_ID = "scan-btn"
# The bare default profile every fresh appliance has.
_METADATA_PATH = "/api/metadata?profile=default"

# Rebuilds the argv from ``$SMOKE_ARGV``, one word per line, and hands it to
# the engine. The here-document expands ``$SMOKE_ARGV`` once and never scans
# the result again, so ``$(pwd)`` or ``$HOME`` inside a word stays literal
# text; ``read -r`` with an empty IFS keeps every backslash and space.
_RUN_ARGV: LiteralString = (
    "set --\n"
    'while IFS= read -r arg; do set -- "$@" "$arg"; done <<SMOKE_ARGV_END\n'
    "$SMOKE_ARGV\n"
    "SMOKE_ARGV_END\n"
    'exec "$SMOKE_DOCKER" "$@"\n'
)


@dataclasses.dataclass(frozen=True)
class FakeHit:
    """
    One request the fake paperless-ngx received.

    Attributes:
        method: The HTTP method.
        path: The URL path, without the query string.
        authorization: The ``Authorization`` header, or ``None``.

    """

    method: str
    path: str
    authorization: str | None


@dataclasses.dataclass(frozen=True)
class FakePaperless:
    """
    A running fake paperless-ngx: where a container reaches it, and its log.

    Attributes:
        url: The base URL to write into the container's config.
        hits: Every request received, in arrival order.

    """

    url: str
    hits: list[FakeHit]


def _fake_handler(hits: list[FakeHit], token: str) -> type[BaseHTTPRequestHandler]:
    """
    Build a request handler that records into ``hits``.

    Args:
        hits: The list every request is appended to.
        token: The only API token the fake accepts.

    Returns:
        A handler class for ``ThreadingHTTPServer``.

    """
    expected = f"Token {token}"

    class _Handler(BaseHTTPRequestHandler):
        """Answer every GET with an empty list, as a fresh paperless-ngx does."""

        def do_GET(self) -> None:
            """Record the request, then answer the list or refuse the token."""
            authorization = self.headers.get("Authorization")
            path = urllib.parse.urlsplit(self.path).path
            hits.append(FakeHit(self.command, path, authorization))
            if authorization == expected:
                status = _HTTP_OK
                payload: object = {
                    "count": 0,
                    "next": None,
                    "previous": None,
                    "results": [],
                }
            else:
                status = _HTTP_UNAUTHORIZED
                payload = {"detail": "Invalid token."}
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        @override
        def log_message(self, format: str, *args: object) -> None:
            """Stay silent: the default writes every request to stderr."""

    return _Handler


@contextlib.contextmanager
def fake_paperless(
    bind_host: str, url_host: str, token: str
) -> Generator[FakePaperless]:
    """
    Serve a minimal paperless-ngx that records every request.

    Every GET is recorded and answered: the empty list paperless-ngx gives
    for tags and correspondents when the request carries ``Token <token>``,
    401 otherwise. A failed list load releases the Scan button just as a
    loaded one does, so only the record proves the container read its config
    and called this server with the token. The server listens on a port the
    kernel picks and is shut down and closed on exit.

    Args:
        bind_host: The address to listen on; ``""`` for every interface.
        url_host: The host name or address a container reaches it by.
        token: The API token to accept.

    Yields:
        The URL to configure, and every request received.

    """
    hits: list[FakeHit] = []
    server = ThreadingHTTPServer((bind_host, 0), _fake_handler(hits, token))
    thread = threading.Thread(
        target=server.serve_forever, name="fake-paperless", daemon=True
    )
    thread.start()
    try:
        yield FakePaperless(url=f"http://{url_host}:{server.server_port}", hits=hits)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def require_token_seen(hits: list[FakeHit], token: str) -> None:
    """
    Fail unless the fake paperless-ngx received ``Token <token>``.

    Args:
        hits: Every request the fake received.
        token: The token written into the container's config.

    Raises:
        SmokeFailure: No request carried that exact token, so the mounted
            config was not read, or something else overrode it.

    """
    expected = f"Token {token}"
    if any(hit.authorization == expected for hit in hits):
        return
    seen = ", ".join(
        f"{hit.method} {hit.path}"
        + (" (another Authorization)" if hit.authorization else " (no Authorization)")
        for hit in hits
    )
    msg = (
        "the fake paperless-ngx never received the token from the mounted "
        f"config; it received {len(hits)} request(s){': ' + seen if seen else ''}"
    )
    raise SmokeFailure(msg)


class _ScanButtonReader(HTMLParser):
    """Collect the attributes of every ``<button id="scan-btn">``."""

    def __init__(self) -> None:
        """Start with no buttons seen."""
        super().__init__(convert_charrefs=True)
        self.buttons: list[dict[str, str | None]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Keep the attributes of a Scan button start tag."""
        attributes = dict(attrs)
        if tag == "button" and attributes.get("id") == _SCAN_BUTTON_ID:
            self.buttons.append(attributes)


def scan_button_enabled(html: str) -> bool:
    """
    Say whether a page fragment renders the Scan button enabled.

    Args:
        html: The fragment.

    Returns:
        False when the button carries ``disabled`` in any form, else True.

    Raises:
        SmokeFailure: The fragment holds no Scan button, or more than one.

    """
    reader = _ScanButtonReader()
    reader.feed(html)
    reader.close()
    if len(reader.buttons) != 1:
        msg = (
            f'expected one <button id="{_SCAN_BUTTON_ID}">, found {len(reader.buttons)}'
        )
        raise SmokeFailure(msg)
    return "disabled" not in reader.buttons[0]


def _host_endpoint(engine: Engine, gateway: str) -> tuple[str, str]:
    """
    Choose where the fake paperless-ngx listens and how a container reaches it.

    Podman maps ``host.containers.internal`` to the host, but not to the
    host's loopback, so the fake listens on every interface. Docker Engine
    gives no such name without an extra flag; there the host is the default
    bridge network's gateway, and the fake listens on that address only.

    Args:
        engine: The engine the container runs under.
        gateway: The Docker bridge gateway; ignored under Podman.

    Returns:
        The address to bind, and the host to put in the configured URL.

    Raises:
        SmokeFailure: Docker reported no bridge gateway.

    """
    if engine.podman:
        return "", _PODMAN_HOST
    if not gateway:
        msg = "the docker bridge network reports no gateway to reach the host by"
        raise SmokeFailure(msg)
    return gateway, gateway


def _bridge_gateway(engine: Engine) -> str:
    """
    Read the IPv4 gateway of Docker's default bridge network.

    Args:
        engine: A Docker engine.

    Returns:
        The gateway address, or ``""`` when there is none.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" network inspect bridge '
        "--format '{{range .IPAM.Config}}{{.Gateway}} {{end}}'"
    )
    _require_success(result, "reading the bridge network's gateway")
    return next((word for word in result.stdout.split() if "." in word), "")


def _argv_lines(argv: list[str]) -> str:
    """
    Join an argv one word per line, for ``$SMOKE_ARGV``.

    Args:
        argv: The words to hand the engine.

    Returns:
        The words, joined by newlines.

    Raises:
        SmokeFailure: A word holds a newline, so it would arrive as two.

    """
    if bad := [word for word in argv if "\n" in word]:
        msg = f"cannot pass a word holding a newline: {bad!r}"
        raise SmokeFailure(msg)
    return "\n".join(argv)


def _write_paperless_config(directory: Path, url: str, token: str) -> None:
    """
    Write a config naming the fake paperless-ngx, readable by any UID.

    Args:
        directory: An empty directory to mount at ``/etc/saneless``.
        url: The fake's URL, as the container reaches it.
        token: The token the fake accepts.

    """
    config = directory / "saneless.toml"
    config.write_text(
        f'[paperless]\nurl = "{url}"\ntoken = "{token}"\n', encoding="utf-8"
    )
    directory.chmod(0o755)
    config.chmod(0o644)


def _get(port: int, path: str) -> tuple[int, str]:
    """
    GET a path from the published server once, from the host.

    Args:
        port: The loopback port the server is published on.
        path: The path, query string included.

    Returns:
        The HTTP status code and the body.

    """
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=_REQUEST_SECONDS)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        connection.close()


def _wait_for_health(engine: Engine, port: int) -> float:
    """
    Poll the published ``/health`` until it answers 200.

    Args:
        engine: The engine and image under test.
        port: The loopback port the server is published on.

    Returns:
        Seconds until the 200.

    Raises:
        SmokeFailure: No 200 arrived before the health deadline.

    """
    start = time.monotonic()
    deadline = start + engine.health_seconds
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            status = _health_status(port)
        except (OSError, http.client.HTTPException) as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if status == _HTTP_OK:
                return time.monotonic() - start
            last = f"HTTP {status}"
        time.sleep(_POLL_SECONDS)
    msg = (
        f"GET http://127.0.0.1:{port}/health gave no 200 within "
        f"{engine.health_seconds:.0f} s; last: {last}"
    )
    raise SmokeFailure(msg)


def _wait_for_scan(engine: Engine, port: int) -> float:
    """
    Poll the lazy list load until it renders the Scan button enabled.

    The page itself always holds Scan while its lists load; the list load's
    answer is the rendering that releases it.

    Args:
        engine: The engine and image under test.
        port: The loopback port the server is published on.

    Returns:
        Seconds until Scan was enabled.

    Raises:
        SmokeFailure: Scan was still disabled, or never rendered, at the
            health deadline.

    """
    start = time.monotonic()
    deadline = start + engine.health_seconds
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            status, body = _get(port, _METADATA_PATH)
        except (OSError, http.client.HTTPException) as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if status != _HTTP_OK:
                last = f"HTTP {status}"
            elif scan_button_enabled(body):
                return time.monotonic() - start
            else:
                last = "Scan rendered disabled"
        time.sleep(_POLL_SECONDS)
    msg = (
        f"GET {_METADATA_PATH} gave no enabled Scan button within "
        f"{engine.health_seconds:.0f} s; last: {last}"
    )
    raise SmokeFailure(msg)


def _run_documented(engine: Engine, page: Path) -> str:
    """
    Run a page's documented ``docker run`` and prove the appliance works.

    The page must show exactly one run of the published image. It is
    validated and substituted (see ``substitute_documented_run``) with a
    scratch config naming a fake paperless-ngx and a token made for this run,
    then started. The server must answer ``/health`` with 200, render Scan
    enabled, and have called the fake with the token. The container, its
    data volume, the config directory and the fake are removed afterwards,
    pass or fail.

    Args:
        engine: The engine and image under test.
        page: The page, relative to the repository root.

    Returns:
        How long the server took to answer and to enable Scan.

    Raises:
        SmokeFailure: The command could not be read, validated or started,
            or the appliance it started did not work.

    """
    runs = documented_docker_runs((_REPO_ROOT / page).read_text(encoding="utf-8"))
    if len(runs) != 1:
        msg = f"{page} shows {len(runs)} docker runs of {PUBLISHED_IMAGE}, expected 1"
        raise SmokeFailure(msg)
    parse_documented_run(runs[0])
    gateway = "" if engine.podman else _bridge_gateway(engine)
    bind_host, url_host = _host_endpoint(engine, gateway)
    token = uuid.uuid4().hex
    name = _container_name()
    volume = f"saneless-smoke-data-{uuid.uuid4().hex[:12]}"
    directory = Path(tempfile.mkdtemp(prefix="saneless-smoke-"))
    try:
        with fake_paperless(bind_host, url_host, token) as fake:
            _write_paperless_config(directory, fake.url, token)
            argv = substitute_documented_run(
                runs[0],
                image=engine.image,
                config_dir=str(directory),
                name=name,
                volume=volume,
            )
            started = engine.sh(_RUN_ARGV, argv=_argv_lines(argv), name=name)
            _require_success(started, f"starting the docker run from {page}")
            try:
                port = _host_port(engine, name)
                healthy = _wait_for_health(engine, port)
                enabled = _wait_for_scan(engine, port)
                require_token_seen(fake.hits, token)
            except SmokeFailure as exc:
                msg = f"{exc}\ncontainer log:\n{_logs(engine, name)}"
                raise SmokeFailure(msg) from exc
    finally:
        engine.sh('exec "$SMOKE_DOCKER" rm -f -v "$SMOKE_NAME"', name=name)
        engine.sh('exec "$SMOKE_DOCKER" volume rm -f "$SMOKE_VOLUME"', volume=volume)
        shutil.rmtree(directory, ignore_errors=True)
    return f"200 after {healthy:.1f} s, Scan enabled after {enabled:.1f} s"


def _documented_run_check(page: Path) -> Callable[[Engine], str]:
    """
    Make a check that runs one page's documented ``docker run``.

    Args:
        page: The page, relative to the repository root.

    Returns:
        The check's function.

    """

    def check(engine: Engine) -> str:
        """Run the page's command; see ``_run_documented``."""
        return _run_documented(engine, page)

    return check


def check_dll_conf(engine: Engine) -> None:
    """
    Check that SANE loads only the network backends.

    Args:
        engine: The engine and image under test.

    Raises:
        SmokeFailure: ``dll.conf`` enables anything but exactly ``net`` and
            ``escl``, or ``dll.d`` holds any file.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" '
        '--entrypoint sh "$SMOKE_IMAGE" -c "$SMOKE_PROBE"',
        probe=_DLL_PROBE,
    )
    _require_success(result, "reading the SANE backend config")
    conf, _, extra = result.stdout.partition("\n---\n")
    enabled = {
        stripped
        for line in conf.splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    }
    if enabled != _EXPECTED_BACKENDS:
        msg = (
            f"dll.conf enables {sorted(enabled)}, expected {sorted(_EXPECTED_BACKENDS)}"
        )
        raise SmokeFailure(msg)
    if extra.strip():
        msg = f"dll.d is not empty: {extra.split()}"
        raise SmokeFailure(msg)


def check_no_pip(engine: Engine) -> None:
    """
    Check that no interpreter in the runtime image can import ``pip``.

    Both interpreters are asked. The venv's alone would pass whether or not
    the Dockerfile removed pip, because a uv venv does not see the base
    image's site-packages, and that is exactly where pip would be left.

    Args:
        engine: The engine and image under test.

    Raises:
        SmokeFailure: Any interpreter can import pip, one of them is missing,
            or the probe did not finish.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" '
        '--entrypoint sh "$SMOKE_IMAGE" -c "$SMOKE_PROBE" probe "$SMOKE_PROGRAM"',
        probe=_PIP_PROBE,
        program=_PIP_PROGRAM,
    )
    _require_success(result, "the pip probe")
    lines = result.stdout.strip().splitlines()
    if not lines or lines[-1] != _PIP_SENTINEL:
        msg = f"the pip probe did not finish\n{_tail(result.stdout)}"
        raise SmokeFailure(msg)
    answers: dict[str, str] = {}
    for line in lines[:-1]:
        path, _, found = line.rpartition(" ")
        answers[path] = found
    if set(answers) != set(_INTERPRETERS):
        msg = f"the pip probe asked {sorted(answers)}, expected {list(_INTERPRETERS)}"
        raise SmokeFailure(msg)
    if importable := [path for path, found in answers.items() if found != "False"]:
        msg = f"pip is importable from {', '.join(importable)}"
        raise SmokeFailure(msg)


def check_no_tools(engine: Engine) -> None:
    """
    Check that the runtime image carries no curl, compiler, uv or pip.

    Args:
        engine: The engine and image under test.

    Raises:
        SmokeFailure: Any of the tools is on PATH, or the probe did not run.

    """
    result = engine.sh(
        'exec "$SMOKE_DOCKER" run --rm --name "$SMOKE_NAME" '
        '--entrypoint sh "$SMOKE_IMAGE" -c "$SMOKE_PROBE"',
        probe=_TOOLS_PROBE,
    )
    _require_success(result, "the tool probe")
    lines = result.stdout.strip().splitlines()
    if not lines or lines[-1] != _TOOLS_SENTINEL:
        msg = f"the tool probe did not finish\n{_tail(result.stdout)}"
        raise SmokeFailure(msg)
    if found := lines[:-1]:
        msg = f"found on PATH: {', '.join(found)}"
        raise SmokeFailure(msg)


CHECKS: tuple[Check, ...] = (
    Check("help", check_help),
    Check("uid 1000", check_uid),
    Check("device enumeration under one second", check_enumeration),
    Check("config data_dir honoured", check_config_data_dir),
    Check("no 'Cannot write' warning", check_no_cannot_write),
    Check("healthy via the image's HEALTHCHECK", check_healthcheck),
    Check("host GET /health returns 200", check_host_health),
    Check("dll.conf holds exactly net and escl, dll.d empty", check_dll_conf),
    Check("no interpreter can import pip", check_no_pip),
    Check("no curl, compiler, uv or pip on PATH", check_no_tools),
    Check("README docker run serves and enables Scan", _documented_run_check(_README)),
    Check(
        "Quick Start docker run serves and enables Scan",
        _documented_run_check(_QUICK_START),
    ),
)


def _run_check(check: Check, engine: Engine) -> bool:
    """
    Run one check and report its result.

    Args:
        check: The check to run.
        engine: The engine and image under test.

    Returns:
        Whether the check passed.

    """
    try:
        detail = check.run(engine)
    except (SmokeFailure, OSError) as exc:
        reason = str(exc).replace("\n", "\n    ")
        sys.stdout.write(f"FAIL {check.name}: {reason}\n")
        sys.stdout.flush()
        return False
    suffix = f" ({detail})" if detail else ""
    sys.stdout.write(f"ok   {check.name}{suffix}\n")
    sys.stdout.flush()
    return True


def _detect_engine(image: str, health_seconds: float) -> Engine:
    """
    Resolve the ``docker`` CLI and find out whether it is Podman.

    Args:
        image: The image reference to test.
        health_seconds: The health deadline.

    Returns:
        The engine to run the checks with.

    Raises:
        SmokeFailure: No ``docker`` executable is on PATH.

    """
    docker = shutil.which("docker")
    if docker is None:
        msg = "no docker executable on PATH"
        raise SmokeFailure(msg)
    engine = Engine(
        docker=docker, image=image, podman=False, health_seconds=health_seconds
    )
    version = engine.sh('exec "$SMOKE_DOCKER" --version')
    return dataclasses.replace(engine, podman="podman" in version.stdout.lower())


def main(argv: list[str] | None = None) -> int:
    """
    Run every smoke check against one image.

    Args:
        argv: Command-line arguments; ``None`` means ``sys.argv[1:]``.

    Returns:
        0 when every check passed, 1 otherwise.

    """
    parser = argparse.ArgumentParser(
        description="Run the named smoke checks against a built container image."
    )
    parser.add_argument("image", help="image reference to test, e.g. saneless:ci")
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the served container to be healthy (default 60)",
    )
    args = parser.parse_args(argv)

    try:
        engine = _detect_engine(args.image, args.timeout)
    except SmokeFailure as exc:
        sys.stderr.write(f"smoke test: {exc}\n")
        return 1
    results = [_run_check(check, engine) for check in CHECKS]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
