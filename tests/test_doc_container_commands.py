"""
Container commands in the docs must run in the documented deployment.

A troubleshooting step that names a container nobody created, a compose
service the shipped ``docker-compose.yml`` does not define, or a program the
image does not ship fails the moment a reader pastes it, usually while they
are already stuck. These tests sweep README.md and every page under ``docs/``
for ``docker compose exec`` and ``docker exec`` and hold each one to the
service names, the documented ``docker run --name`` values and the programs
the image is known to contain.

They also hold the scanner-host page's firewall advice to saned's two
connections: 6566/tcp for control, and a separate connection for image data.
Opening 6566/tcp alone lists devices and turns the Scanner row green, and then
every scan times out.
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING

from saneless.scanner.saned_probe import SANED_PORT

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE = REPO_ROOT / "docker-compose.yml"
README = REPO_ROOT / "README.md"
DOCS_DIR = REPO_ROOT / "docs"
SCANNER_HOST_PAGE = DOCS_DIR / "how-to" / "scanner-host-discovery.md"
# The section of the scanner-host page that holds the firewall advice.
TROUBLESHOOTING_HEADING = re.compile(r"^## Troubleshooting\b.*$", re.MULTILINE)

# The programs the image is known to ship. `ping` and `curl` are absent, and
# the image smoke test asserts curl stays absent, so a doc command that execs
# either one cannot run as written.
IMAGE_PROGRAMS = frozenset({"saneless", "python", "sh", "cat", "ls", "rm"})

# A service key directly under `services:`, at the two-space indent compose uses.
COMPOSE_SERVICE_KEY = re.compile(r"^  (?P<name>[A-Za-z0-9_.-]+):\s*$")
# Any other top-level key ends the `services:` block.
TOP_LEVEL_KEY = re.compile(r"^[A-Za-z0-9_.-]+:")

EXEC_COMMAND = re.compile(
    r"\bdocker(?P<compose>\s+compose|-compose)?\s+exec\b(?P<rest>[^`]*)"
)
RUN_NAME = re.compile(r"--name(?:=|\s+)(?P<name>[A-Za-z0-9_.-]+)")

# Exec options that take a value as the next word, for both `docker exec` and
# `docker compose exec`; the value is not the service or container name.
VALUE_FLAGS = frozenset(
    {
        "-e",
        "--env",
        "--env-file",
        "-u",
        "--user",
        "-w",
        "--workdir",
        "--index",
        "--detach-keys",
    }
)

ADD_SERVICE_SANE = re.compile(r"--add-service=sane(?![\w-])")
ADD_SERVICE_SANED = re.compile(r"--add-service=saned(?![\w-])")
DATA_PORTRANGE = re.compile(r"\bdata_portrange\s*=\s*(?P<low>\d+)\s*-\s*(?P<high>\d+)")
UFW_CONTROL_PORT = re.compile(rf"\bufw allow {SANED_PORT}/tcp\b")
UFW_RANGE = re.compile(r"\bufw allow (?P<low>\d+):(?P<high>\d+)/tcp\b")


def _doc_pages() -> dict[str, str]:
    """Return README.md and every Markdown page under ``docs/``, keyed by path."""
    paths = [README, *sorted(DOCS_DIR.rglob("*.md"))]
    assert len(paths) > 1, f"no Markdown pages found under {DOCS_DIR}"
    return {
        str(path.relative_to(REPO_ROOT)): path.read_text(encoding="utf-8")
        for path in paths
    }


def _compose_services(compose_text: str) -> set[str]:
    """Return the service names defined under ``services:`` in a compose file."""
    services: set[str] = set()
    inside = False
    for line in compose_text.splitlines():
        if line.rstrip() == "services:":
            inside = True
            continue
        if inside and TOP_LEVEL_KEY.match(line):
            break
        match = COMPOSE_SERVICE_KEY.match(line) if inside else None
        if match:
            services.add(match["name"])
    return services


def _commands(text: str) -> list[tuple[int, str]]:
    """
    Return each line of ``text`` with its shell continuations joined.

    A command wrapped with trailing backslashes is read as one, numbered by
    its first line, so a ``--name`` or a program on a later line still
    belongs to it.
    """
    lines = text.splitlines()
    commands: list[tuple[int, str]] = []
    index = 0
    while index < len(lines):
        start = index
        parts = [lines[index]]
        while lines[index].rstrip().endswith("\\") and index + 1 < len(lines):
            index += 1
            parts.append(lines[index])
        commands.append((start + 1, " ".join(part.rstrip("\\ ") for part in parts)))
        index += 1
    return commands


def _documented_run_names(pages: Iterable[str]) -> set[str]:
    """Return every ``--name`` a documented ``docker run`` gives its container."""
    return {
        match["name"]
        for text in pages
        for _, command in _commands(text)
        if "docker run" in command
        for match in RUN_NAME.finditer(command)
    }


def _exec_targets(rest: str) -> tuple[str | None, str | None]:
    """Return the service or container, and the program, an exec command names."""
    positional: list[str] = []
    skip_value = False
    for word in rest.split():
        if skip_value:
            skip_value = False
        elif word.startswith("-"):
            skip_value = word in VALUE_FLAGS
        else:
            positional.append(word.strip("'\""))
    target = positional[0] if positional else None
    program = positional[1] if len(positional) > 1 else None
    return target, program


def _exec_commands(pages: Mapping[str, str]) -> list[tuple[str, bool, str, str]]:
    """Return ``(where, is_compose, rest, line)`` for every exec command found."""
    found: list[tuple[str, bool, str, str]] = []
    for label, text in pages.items():
        for number, command in _commands(text):
            found.extend(
                (f"{label}:{number}", bool(match["compose"]), match["rest"], command)
                for match in EXEC_COMMAND.finditer(command)
            )
    return found


def _exec_offenders(pages: Mapping[str, str], services: set[str]) -> list[str]:
    """
    Report every exec command that cannot run in the documented deployment.

    Args:
        pages: Page text keyed by the path to report it under.
        services: The service names the shipped compose file defines.

    Returns:
        One line per problem: an unknown compose service, a container name no
        documented ``docker run --name`` creates, or a program outside
        ``IMAGE_PROGRAMS``.

    """
    run_names = _documented_run_names(pages.values())
    offenders: list[str] = []
    for where, is_compose, rest, command in _exec_commands(pages):
        target, program = _exec_targets(rest)
        if target is None:
            continue
        shown = command.strip()
        if is_compose and target not in services:
            offenders.append(f"{where}: no compose service {target!r}: {shown}")
        if not is_compose and target not in run_names:
            offenders.append(
                f"{where}: no documented `docker run --name {target}`: {shown}"
            )
        if program not in IMAGE_PROGRAMS:
            offenders.append(f"{where}: image does not ship {program!r}: {shown}")
    return offenders


def _section(text: str, heading: re.Pattern[str]) -> str:
    """Return the text under the first ``##`` heading ``heading`` matches."""
    match = heading.search(text)
    if match is None:
        return ""
    return text[match.end() :].split("\n## ", 1)[0]


def _firewall_offenders(text: str) -> list[str]:
    """
    Report firewall advice that leaves saned's data connection closed.

    Returns:
        One line per problem: firewalld advice that does not use the stock
        ``sane`` service (or names ``saned``, which firewalld does not have),
        no pinned ``data_portrange``, no ufw rule for 6566/tcp, or no ufw rule
        opening the pinned range.

    """
    offenders: list[str] = []
    if ADD_SERVICE_SANED.search(text):
        offenders.append("firewalld has no `saned` service; it is `sane`")
    elif not ADD_SERVICE_SANE.search(text):
        offenders.append("no `firewall-cmd --permanent --add-service=sane`")
    ranges = {(m["low"], m["high"]) for m in DATA_PORTRANGE.finditer(text)}
    if not ranges:
        offenders.append("no `data_portrange = LOW - HIGH` for saned.conf")
    if not UFW_CONTROL_PORT.search(text):
        offenders.append(f"no `ufw allow {SANED_PORT}/tcp`")
    opened = {(m["low"], m["high"]) for m in UFW_RANGE.finditer(text)}
    offenders.extend(
        f"data_portrange {low} - {high} has no `ufw allow {low}:{high}/tcp`"
        for low, high in sorted(ranges - opened)
    )
    return offenders


def test_compose_file_defines_services() -> None:
    """The service reader finds the shipped compose file's services."""
    assert _compose_services(COMPOSE.read_text(encoding="utf-8"))


def test_the_sweep_finds_exec_commands_in_the_docs() -> None:
    """The sweep below has something to check, so its pass means something."""
    assert _exec_commands(_doc_pages()), "no docker exec command found in the docs"


def test_container_commands_run_in_the_documented_deployment() -> None:
    """Every exec in the docs names a real service or container and a shipped tool."""
    services = _compose_services(COMPOSE.read_text(encoding="utf-8"))
    offenders = _exec_offenders(_doc_pages(), services)
    assert not offenders, "container commands that cannot run:\n" + "\n".join(offenders)


def test_firewall_advice_opens_the_data_connection() -> None:
    """The scanner-host page opens both of saned's connections."""
    text = SCANNER_HOST_PAGE.read_text(encoding="utf-8")
    section = _section(text, TROUBLESHOOTING_HEADING)
    assert section, f"{SCANNER_HOST_PAGE.name} has no `## Troubleshooting` section"
    offenders = _firewall_offenders(section)
    assert not offenders, "firewall advice leaves scans to time out:\n" + "\n".join(
        offenders
    )


GOOD_FIREWALL = f"""\
sudo firewall-cmd --permanent --add-service=sane
sudo firewall-cmd --reload
Set `data_portrange = 10000 - 10100` in `/etc/sane.d/saned.conf`, then:
sudo ufw allow {SANED_PORT}/tcp
sudo ufw allow 10000:10100/tcp
"""


def test_seeded_exec_sweep_reports_each_broken_command_once() -> None:
    """Each kind of broken exec command is reported, and only once."""
    services = _compose_services(COMPOSE.read_text(encoding="utf-8"))
    assert "web" not in services
    run_line = "docker run -d \\\n  --name saneless \\\n  image\n"
    seeds = {
        "unknown service": "`docker compose exec web saneless devices`",
        "unshipped program": run_line + "docker exec saneless ping host\n",
        "undocumented name": "docker exec saneless saneless devices\n",
        "unshipped after flags": "docker compose exec -T -u root saneless curl x\n",
    }
    for name, text in seeds.items():
        offenders = _exec_offenders({name: text}, services)
        assert len(offenders) == 1, (name, offenders)


def test_seeded_exec_sweep_accepts_commands_that_run() -> None:
    """Commands that run in the documented deployment are not reported."""
    services = _compose_services(COMPOSE.read_text(encoding="utf-8"))
    service = min(services)
    text = (
        "docker run -d \\\n  --name=scanbox \\\n  image\n"
        "docker exec scanbox saneless devices\n"
        f"Run `docker compose exec {service} saneless jobs` or\n"
        f"docker compose exec -T {service} sh -c 'ls /etc'\n"
    )
    assert _exec_offenders({"good": text}, services) == []


def test_seeded_firewall_check_reports_each_gap_once() -> None:
    """Each way the firewall advice can leave scans timing out is reported once."""
    seeds = {
        "saned service": GOOD_FIREWALL.replace("=sane\n", "=saned\n"),
        "port only": GOOD_FIREWALL.replace(
            "--add-service=sane", f"--add-port={SANED_PORT}/tcp"
        ),
        "range mismatch": GOOD_FIREWALL.replace("10000:10100", "10000:10200"),
        "no portrange": GOOD_FIREWALL.replace(
            "`data_portrange = 10000 - 10100`", "a data port range"
        ).replace("sudo ufw allow 10000:10100/tcp\n", ""),
        "no control port": GOOD_FIREWALL.replace(
            f"sudo ufw allow {SANED_PORT}/tcp\n", ""
        ),
    }
    for name, text in seeds.items():
        assert text != GOOD_FIREWALL, name
        offenders = _firewall_offenders(text)
        assert len(offenders) == 1, (name, offenders)


def test_seeded_firewall_check_accepts_working_advice() -> None:
    """Advice that opens both connections is not reported."""
    assert _firewall_offenders(GOOD_FIREWALL) == []


# ---------------------------------------------------------------------------
# A saned on the container's own host
# ---------------------------------------------------------------------------

WHICH_SETUP_PAGE = DOCS_DIR / "getting-started" / "which-setup.md"
SHAPE_TWO_HEADING = re.compile(r"^## Shape 2\b.*$", re.MULTILINE)
HOST_GATEWAY = "host.docker.internal:host-gateway"
# A compose `extra_hosts:` key whose entry maps the name to the host gateway,
# as a block sequence item or a flow sequence, the value quoted or not.
EXTRA_HOSTS_ENTRY = re.compile(
    r"^(?P<indent>[ \t]*)extra_hosts:[ \t]*"
    r"(?:\[[ \t]*[\"']?" + re.escape(HOST_GATEWAY) + r"[\"']?[ \t]*\]"
    r"|\n(?P=indent)[ \t]+-[ \t]*[\"']?" + re.escape(HOST_GATEWAY) + r"[\"']?)"
    r"[ \t]*$",
    re.MULTILINE,
)
ADD_HOST_FLAG = re.compile(r"--add-host(?:=|\s+)" + re.escape(HOST_GATEWAY) + r"(?!\S)")
SANED_CONF = re.compile(r"\bsaned\.conf\b")
NETWORK_INSPECT = re.compile(r"\bdocker network inspect\b")


def _shape_two_offenders(section: str) -> list[str]:
    """
    Report what the same-host container recipe leaves out.

    On Linux Docker Engine, ``host.docker.internal`` resolves only when the
    container is given it, and saned refuses every client its ``saned.conf``
    does not list, so the recipe needs both, plus the way to find the subnet
    to list.

    Returns:
        One line per problem: no compose ``extra_hosts`` entry mapping the
        name to ``host-gateway``, no ``docker run`` passing ``--add-host`` for
        it, no ``saned.conf`` step, or no ``docker network inspect`` to find
        the container network's subnet.

    """
    offenders: list[str] = []
    if not EXTRA_HOSTS_ENTRY.search(section):
        offenders.append(f'no compose `extra_hosts: - "{HOST_GATEWAY}"`')
    runs = [command for _, command in _commands(section) if "docker run" in command]
    if not any(ADD_HOST_FLAG.search(command) for command in runs):
        offenders.append(f"no `docker run --add-host={HOST_GATEWAY}`")
    if not SANED_CONF.search(section):
        offenders.append("no `saned.conf` line allowing the container network")
    if not NETWORK_INSPECT.search(section):
        offenders.append("no `docker network inspect` to find the subnet")
    return offenders


GOOD_SHAPE_TWO = f"""\
```yaml
services:
  saneless:
    extra_hosts:
      - "{HOST_GATEWAY}"
```

```bash
docker run -d \\
  --add-host={HOST_GATEWAY} \\
  image
```

Find the subnet with `docker network inspect saneless_default` and add it to
`/etc/sane.d/saned.conf`.
"""


def test_same_host_recipe_reaches_the_host_saned() -> None:
    """Which-setup's same-host shape maps the host gateway and opens saned to it."""
    section = _section(WHICH_SETUP_PAGE.read_text(encoding="utf-8"), SHAPE_TWO_HEADING)
    assert section, f"{WHICH_SETUP_PAGE.name} has no `## Shape 2` section"
    offenders = _shape_two_offenders(section)
    assert not offenders, (
        "the same-host container recipe cannot reach saned as written:\n"
        + "\n".join(offenders)
    )


def test_seeded_same_host_check_reports_each_gap_once() -> None:
    """Each missing piece of the same-host recipe is reported, and only once."""
    seeds = {
        "no extra_hosts": GOOD_SHAPE_TWO.replace("extra_hosts:", "labels:"),
        "wrong gateway": GOOD_SHAPE_TWO.replace(
            f'"{HOST_GATEWAY}"', '"host.docker.internal:10.0.0.1"'
        ),
        "no add-host": GOOD_SHAPE_TWO.replace(f"  --add-host={HOST_GATEWAY} \\\n", ""),
        "no saned.conf": GOOD_SHAPE_TWO.replace(
            "`/etc/sane.d/saned.conf`", "the allow list"
        ),
        "no inspect": GOOD_SHAPE_TWO.replace(
            "`docker network inspect saneless_default`", "your tools"
        ),
    }
    for name, text in seeds.items():
        assert text != GOOD_SHAPE_TWO, name
        offenders = _shape_two_offenders(text)
        assert len(offenders) == 1, (name, offenders)


def test_seeded_same_host_check_accepts_a_complete_recipe() -> None:
    """A recipe with every piece, in either flag form, is not reported."""
    assert _shape_two_offenders(GOOD_SHAPE_TWO) == []
    spaced = GOOD_SHAPE_TWO.replace(
        f"--add-host={HOST_GATEWAY}", f"--add-host {HOST_GATEWAY}"
    ).replace(
        f'    extra_hosts:\n      - "{HOST_GATEWAY}"',
        f'    extra_hosts: ["{HOST_GATEWAY}"]',
    )
    assert spaced != GOOD_SHAPE_TWO
    assert _shape_two_offenders(spaced) == []
