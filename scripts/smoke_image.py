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
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import http.client
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, LiteralString

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
                    return f"200 after {time.monotonic() - start:.1f} s"
                last = f"HTTP {status}"
            time.sleep(_POLL_SECONDS)
        msg = (
            f"GET http://127.0.0.1:{port}/health gave no 200 within "
            f"{engine.health_seconds:.0f} s; last: {last}\n"
            f"container log:\n{_logs(engine, name)}"
        )
        raise SmokeFailure(msg)


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
