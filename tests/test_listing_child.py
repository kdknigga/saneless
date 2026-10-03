"""
Tests for ``saneless.scanner._listing_child``, the file every listing runs in.

The child reads one JSON request naming at most one device and an alarm, arms
the alarm before python-sane loads, lists the scanners, opens and closes the
named device when it is unlisted, and writes one JSON reply line.  Every
python-sane failure comes back as data, so only a signal or an unreadable
request ends the child without a reply.

The list-then-open decision is driven in this process over
``FakeSaneModule``, never real libsane.  The child imports only allowlisted
standard-library modules, and loading it pulls in neither ``saneless`` nor
python-sane.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import saneless
from saneless.scanner import _listing_child
from tests.fake_sane import FakeSaneDev, FakeSaneModule

if TYPE_CHECKING:
    from collections.abc import Callable

_CHILD_PATH = Path(saneless.__file__).parent / "scanner" / "_listing_child.py"

_TEST_DEVICE = ("test:0", "Noname", "frontend-tester", "virtual device")
_NET_DEVICE_ID = "net:scanbox.lan:test:0"
_IO_ERROR_MESSAGE = "Error during device I/O"

# Every module the child may import, and nothing else.  python-sane is not on
# the list: it is loaded late, by name, after the alarm is armed.
_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "contextlib",
        "fcntl",
        "importlib",
        "json",
        "os",
        "signal",
        "sys",
        "typing",
        "collections.abc",
    }
)


class _DeviceIOError(Exception):
    """Stands in for python-sane's ``_sane.error`` raised by a failed open."""


@pytest.mark.parametrize(
    ("request_line", "make_module", "expected"),
    [
        pytest.param(
            {"open": None, "alarm": 35},
            lambda: FakeSaneModule(devices=[_TEST_DEVICE]),
            {"devices": [list(_TEST_DEVICE)]},
            id="listing-only",
        ),
        pytest.param(
            {"open": "test:0", "alarm": 35},
            lambda: FakeSaneModule(devices=[_TEST_DEVICE]),
            {"devices": [list(_TEST_DEVICE)]},
            id="configured-id-is-listed",
        ),
        pytest.param(
            {"open": "", "alarm": 35},
            lambda: FakeSaneModule(devices=[_TEST_DEVICE]),
            {"devices": [list(_TEST_DEVICE)]},
            id="empty-id-is-no-open",
        ),
        pytest.param(
            {"open": _NET_DEVICE_ID, "alarm": 35},
            lambda: FakeSaneModule(devices=[]),
            {"devices": [], "opened": True},
            id="unlisted-id-opens",
        ),
        pytest.param(
            {"open": _NET_DEVICE_ID, "alarm": 35},
            lambda: FakeSaneModule(
                devices=[], open_error=_DeviceIOError(_IO_ERROR_MESSAGE)
            ),
            {
                "devices": [],
                "opened": False,
                "open_error": {
                    "type": "_DeviceIOError",
                    "message": _IO_ERROR_MESSAGE,
                },
            },
            id="open-fails",
        ),
        pytest.param(
            {"open": None, "alarm": 35},
            lambda: FakeSaneModule(get_devices_error=RuntimeError("boom")),
            {
                "devices": [],
                "list_error": {"type": "RuntimeError", "message": "boom"},
            },
            id="listing-raises",
        ),
        pytest.param(
            {"open": _NET_DEVICE_ID, "alarm": 35},
            lambda: FakeSaneModule(get_devices_error=RuntimeError("boom")),
            {
                "devices": [],
                "list_error": {"type": "RuntimeError", "message": "boom"},
                "opened": True,
            },
            id="listing-raises-open-still-attempted",
        ),
    ],
)
def test_respond_truth_table(
    request_line: dict[str, object],
    make_module: Callable[[], FakeSaneModule],
    expected: dict[str, object],
) -> None:
    """Each request and library state yields exactly the reply the schema names."""
    fake = make_module()

    assert _listing_child.respond(request_line, fake) == expected


def test_respond_lists_once_and_never_initialises() -> None:
    """
    ``respond`` lists exactly once and leaves ``init()`` to its caller.

    The entry point initialises SANE, so an in-process caller that reuses
    ``respond`` over an already initialised module does not initialise twice.
    """
    fake = FakeSaneModule(devices=[_TEST_DEVICE])

    _listing_child.respond({"open": None, "alarm": 35}, fake)

    assert fake.get_devices_call_count == 1
    assert fake.init_call_count == 0


def test_a_listed_device_is_not_opened() -> None:
    """A configured id that the listing already contains is never opened."""
    device = FakeSaneDev()
    fake = FakeSaneModule(device=device, devices=[_TEST_DEVICE])

    reply = _listing_child.respond({"open": "test:0", "alarm": 35}, fake)

    assert "opened" not in reply
    assert device.cancel_calls == 0
    assert device.close_calls == 0


def test_an_unlisted_device_is_opened_cancelled_and_closed() -> None:
    """An open that succeeds is cancelled and closed before the reply is built."""
    device = FakeSaneDev()
    fake = FakeSaneModule(device=device, devices=[])

    reply = _listing_child.respond({"open": _NET_DEVICE_ID, "alarm": 35}, fake)

    assert reply == {"devices": [], "opened": True}
    assert device.cancel_calls == 1
    assert device.close_calls == 1


def test_the_reply_round_trips_every_string() -> None:
    """
    A lone surrogate and non-ASCII text survive the JSON reply byte for byte.

    libsane hands device names over as bytes, and python-sane decodes them with
    ``surrogateescape``, so a name can carry a lone surrogate.  The default
    ``ensure_ascii`` encoding escapes it, and decoding gives the same string.
    """
    name = "x\udcff"
    fake = FakeSaneModule(devices=[(name, "Épson", "model", "scanner")])

    reply = _listing_child.respond({"open": None, "alarm": 35}, fake)
    decoded = json.loads(json.dumps(reply))

    assert decoded == reply
    assert decoded["devices"][0][0] == name
    assert decoded["devices"][0][1] == "Épson"


class _OrderedFakeSane(FakeSaneModule):
    """A ``FakeSaneModule`` that records its ``init()`` in a shared event list."""

    def __init__(
        self,
        events: list[str],
        *,
        devices: list[tuple[str, str, str, str]] | None = None,
        init_error: BaseException | None = None,
    ) -> None:
        """
        Create the double.

        Args:
            events: The list ``init()`` appends ``"init"`` to.
            devices: Passed to ``FakeSaneModule``.
            init_error: Passed to ``FakeSaneModule``.

        """
        super().__init__(devices=devices, init_error=init_error)
        self.events = events

    def init(self) -> tuple[int, int, int, int]:
        """
        Record the call in the shared list, then behave as the parent does.

        Returns:
            The version tuple ``FakeSaneModule.init`` returns.

        """
        self.events.append("init")
        return super().init()


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    stdin_text: str,
    fake: FakeSaneModule,
    events: list[str],
) -> tuple[int, str]:
    """
    Run the child's entry point in this process, over a fake ``sane`` module.

    The alarm is replaced by a recorder: pytest-timeout uses the signal
    method, so a real alarm armed here would end the test run.  The request
    and the reply travel through in-memory streams; the descriptor handling
    around the entry point is left to the tests that run the real script.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        stdin_text: What the child reads from stdin.
        fake: The module ``import sane`` resolves to.
        events: The shared list the alarm recorder appends to.

    Returns:
        The exit status ``main()`` returned, and everything it wrote as its
        reply.

    """
    reply_channel = io.StringIO()
    monkeypatch.setitem(sys.modules, "sane", fake)

    def record_alarm(seconds: int) -> int:
        events.append(f"alarm {seconds}")
        return 0

    monkeypatch.setattr(_listing_child.signal, "alarm", record_alarm)
    status = _listing_child.main(io.StringIO(stdin_text), reply_channel)
    return status, reply_channel.getvalue()


def test_main_arms_the_alarm_before_initialising_and_writes_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The alarm is armed before python-sane runs, and the reply is one line.

    An orphaned child stuck inside libsane is ended by the kernel, which only
    works if the alarm is running before the library is first entered.
    """
    events: list[str] = []
    fake = _OrderedFakeSane(events, devices=[_TEST_DEVICE])

    status, output = _run_main(
        monkeypatch, '{"open": null, "alarm": 35}\n', fake, events
    )

    assert status == 0
    assert events == ["alarm 35", "init"]
    assert fake.init_call_count == 1
    assert output.endswith("\n")
    assert output.count("\n") == 1
    assert json.loads(output) == {"devices": [list(_TEST_DEVICE)]}


def test_main_reports_a_failed_init_as_both_listing_and_open_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SANE that cannot start fails the listing and the requested open alike."""
    events: list[str] = []
    fake = _OrderedFakeSane(events, init_error=RuntimeError("SANE could not start"))

    status, output = _run_main(
        monkeypatch,
        json.dumps({"open": _NET_DEVICE_ID, "alarm": 35}) + "\n",
        fake,
        events,
    )

    error = {"type": "RuntimeError", "message": "SANE could not start"}
    assert status == 0
    assert json.loads(output) == {
        "devices": [],
        "list_error": error,
        "opened": False,
        "open_error": error,
    }
    assert fake.get_devices_call_count == 0


def test_main_round_trips_a_lone_surrogate_through_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The line ``main()`` writes decodes to the exact device name listed."""
    events: list[str] = []
    name = "x\udcff"
    fake = _OrderedFakeSane(events, devices=[(name, "Épson", "model", "scanner")])

    status, output = _run_main(
        monkeypatch, '{"open": null, "alarm": 35}\n', fake, events
    )

    assert status == 0
    assert output.isascii()
    assert json.loads(output)["devices"] == [[name, "Épson", "model", "scanner"]]


@pytest.mark.parametrize(
    "stdin_text",
    [
        pytest.param("not json\n", id="not-json"),
        pytest.param('{"open": 5, "alarm": 35}\n', id="open-not-a-string"),
        pytest.param('{"open": null, "alarm": -1}\n', id="negative-alarm"),
        pytest.param('{"open": null, "alarm": true}\n', id="boolean-alarm"),
        pytest.param('{"open": null}\n', id="alarm-missing"),
        pytest.param("[]\n", id="not-an-object"),
        pytest.param("", id="empty-stdin"),
    ],
)
def test_main_rejects_a_malformed_request_without_touching_sane(
    monkeypatch: pytest.MonkeyPatch,
    stdin_text: str,
) -> None:
    """
    A request the child cannot read ends it with status 2 and no reply.

    Nothing reaches python-sane and no alarm is armed: the parent treats any
    positive exit status as "returned no answer".
    """
    events: list[str] = []
    fake = _OrderedFakeSane(events, devices=[_TEST_DEVICE])

    status, output = _run_main(monkeypatch, stdin_text, fake, events)

    assert status == 2
    assert output == ""
    assert events == []
    assert fake.init_call_count == 0
    assert fake.get_devices_call_count == 0


def test_loading_the_child_imports_nothing_from_saneless_or_sane() -> None:
    """
    Loading the child file on its own pulls in no ``saneless`` module or sane.

    The child is run by path precisely so that it skips the package
    ``__init__``, which loads the configuration layer and costs far more than
    the listing itself.  Loading the file in a fresh interpreter under a private
    name must not run ``main()`` either, so python-sane stays unimported.
    """
    script = textwrap.dedent(
        """
        import importlib.util
        import os
        import sys

        spec = importlib.util.spec_from_file_location(
            "listing_child_probe", os.environ["SANELESS_TEST_MODULE"]
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print(sorted(k for k in sys.modules if k.startswith("saneless") or k == "sane"))
        """
    )

    # Every argv element is a literal and the per-run paths travel in the
    # environment, quoted so they are never re-split.
    result = subprocess.run(
        ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" -I -c "$SANELESS_TEST_CODE"'],
        env={
            **os.environ,
            "SANELESS_TEST_PYTHON": sys.executable,
            "SANELESS_TEST_CODE": script,
            "SANELESS_TEST_MODULE": str(_CHILD_PATH),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_the_child_imports_only_allowlisted_standard_library_modules() -> None:
    """
    Every import in the child names an allowlisted standard-library module.

    The child runs in a bare interpreter, so anything it imported beyond this
    list would either drag the package back in or widen what a tampered path
    could inject.  python-sane is never imported statically: it is loaded by
    name after the alarm is armed.
    """
    tree = ast.parse(_CHILD_PATH.read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert imported
    assert sorted(set(imported) - _ALLOWED_IMPORTS) == []
    assert "sane" not in imported
