"""
Tests for ``saneless.scanner._listing_child``, the file every listing runs in.

The child reads one JSON request naming at most one device and an alarm, arms
the alarm before python-sane loads, lists the scanners, opens and closes the
named device when it is unlisted, and writes one JSON reply line.  Every
python-sane failure comes back as data, so only a signal or an unreadable
request ends the child without a reply.

The list-then-open decision is driven in this process over
``FakeSaneModule``, never real libsane.  The child imports only allowlisted
standard-library modules and saneless's stdlib-only reply-pipe helper, and
loading it pulls in no python-sane.
"""

from __future__ import annotations

import ast
import importlib
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
from saneless import thread_unwinder
from saneless.scanner import _listing_child
from tests.fake_sane import FakeSaneDev, FakeSaneModule

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from types import ModuleType

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
        "importlib",
        "json",
        "os",
        "signal",
        "sys",
        "typing",
        "collections.abc",
        "saneless.scanner._child_stdio",
    }
)

# The saneless modules loading the child may pull in: the reply-pipe helper
# and the two packages above it, whose ``__init__`` files import nothing.  The
# thread-unwinder loader is not one of them: ``main`` imports it late.
_SANELESS_MODULES_LOADED = [
    "saneless",
    "saneless.scanner",
    "saneless.scanner._child_stdio",
]


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


# SANE option tuples: (index, name, title, desc, type, unit, size, cap,
# constraint).  Only the name and the constraint cross to the parent.
_OPTION_ZERO = (0, "", "Number of options", "", 1, 0, 4, 4, None)
_GROUP_HEADING = (1, None, "Scan Mode", "", 5, 0, 0, 0, None)
_MODE_OPTION = (2, "mode", "Scan mode", "", 3, 0, 6, 5, ["Gray", "Color"])
_DEPTH_OPTION = (3, "depth", "Bit depth", "", 1, 0, 4, 5, [1, 8, 16])
_RESOLUTION_OPTION = (4, "resolution", "Resolution", "", 2, 4, 4, 5, (1.0, 1200.0, 0.5))
_PREVIEW_OPTION = (5, "preview", "Preview", "", 0, 0, 4, 5, None)


class _OptionsDevice:
    """A device handle that reports a fixed option table, and counts its closing."""

    def __init__(
        self,
        options: Sequence[tuple[object, ...]],
        *,
        options_error: Exception | None = None,
        closing_error: Exception | None = None,
    ) -> None:
        """
        Create the handle.

        Args:
            options: What ``get_options()`` returns.  Unlike ``FakeSaneDev``,
                a name may be ``None``, as a group heading's is.
            options_error: Raised by ``get_options()`` instead, when given.
            closing_error: Raised by ``cancel()`` and ``close()``, when given,
                after each has counted its call.

        """
        self.options = options
        self.options_error = options_error
        self.closing_error = closing_error
        self.cancel_calls = 0
        self.close_calls = 0

    def get_options(self) -> list[tuple[object, ...]]:
        """
        Return the option table.

        Returns:
            The table the handle was made with.

        Raises:
            Exception: The configured ``options_error``.

        """
        if self.options_error is not None:
            raise self.options_error
        return list(self.options)

    def cancel(self) -> None:
        """
        Count the call.

        Raises:
            Exception: The configured ``closing_error``.

        """
        self.cancel_calls += 1
        if self.closing_error is not None:
            raise self.closing_error

    def close(self) -> None:
        """
        Count the call.

        Raises:
            Exception: The configured ``closing_error``.

        """
        self.close_calls += 1
        if self.closing_error is not None:
            raise self.closing_error


class _OptionsModule:
    """A ``sane`` stand-in whose ``open()`` returns one ``_OptionsDevice``."""

    def __init__(
        self, device: _OptionsDevice, *, open_error: Exception | None = None
    ) -> None:
        """
        Create the module.

        Args:
            device: What every ``open()`` returns.
            open_error: Raised by ``open()`` instead, when given.

        """
        self.device = device
        self.open_error = open_error
        self.opened: list[str] = []
        self.get_devices_calls = 0

    def get_devices(self) -> list[tuple[str, str, str, str]]:
        """
        Count the call and list one device.

        Returns:
            The one test device.

        """
        self.get_devices_calls += 1
        return [_TEST_DEVICE]

    def open(self, device_id: str) -> _OptionsDevice:
        """
        Record the id and open the device.

        Returns:
            The module's one device.

        Raises:
            Exception: The configured ``open_error``.

        """
        self.opened.append(device_id)
        if self.open_error is not None:
            raise self.open_error
        return self.device


def _capabilities_request(device_id: str = "test:0") -> dict[str, object]:
    """
    Build a capabilities request as the launcher sends it.

    Returns:
        The decoded request.

    """
    return {"open": None, "capabilities": device_id, "alarm": 0}


@pytest.mark.parametrize(
    ("option", "entry"),
    [
        pytest.param(
            _MODE_OPTION, ["mode", "list", ["Gray", "Color"]], id="string-list"
        ),
        pytest.param(_DEPTH_OPTION, ["depth", "list", [1, 8, 16]], id="word-list"),
        pytest.param(
            _RESOLUTION_OPTION, ["resolution", "range", [1.0, 1200.0, 0.5]], id="range"
        ),
        pytest.param(_PREVIEW_OPTION, ["preview", "none", []], id="unconstrained"),
        pytest.param(_OPTION_ZERO, ["", "none", []], id="option-zero"),
        pytest.param(_GROUP_HEADING, [None, "none", []], id="group-heading"),
    ],
)
def test_a_capabilities_request_reports_each_option(
    option: tuple[object, ...], entry: list[object]
) -> None:
    """
    Each option crosses as its name, its constraint's kind, and its values.

    The device is opened by the requested id, cancelled and closed once, and
    nothing is listed: a capabilities request is not a listing.  The JSON
    round trip is the one the reply line makes.
    """
    device = _OptionsDevice([option])
    module = _OptionsModule(device)

    reply = _listing_child.respond(_capabilities_request(), module)

    assert json.loads(json.dumps(reply)) == {"devices": [], "options": [entry]}
    assert module.opened == ["test:0"]
    assert module.get_devices_calls == 0
    assert (device.cancel_calls, device.close_calls) == (1, 1)


def test_a_capabilities_request_keeps_the_device_order() -> None:
    """A whole table comes back entry for entry, in the device's order."""
    table = [_OPTION_ZERO, _GROUP_HEADING, _MODE_OPTION, _RESOLUTION_OPTION]
    module = _OptionsModule(_OptionsDevice(table))

    reply = _listing_child.respond(_capabilities_request(), module)

    assert reply["options"] == [
        ["", "none", []],
        [None, "none", []],
        ["mode", "list", ["Gray", "Color"]],
        ["resolution", "range", [1.0, 1200.0, 0.5]],
    ]


def test_a_capabilities_request_that_cannot_open_reports_open_error() -> None:
    """An open that raises is reported, and no device is touched."""
    device = _OptionsDevice([_MODE_OPTION])
    module = _OptionsModule(device, open_error=_DeviceIOError(_IO_ERROR_MESSAGE))

    reply = _listing_child.respond(_capabilities_request(_NET_DEVICE_ID), module)

    assert reply == {
        "devices": [],
        "open_error": {"type": "_DeviceIOError", "message": _IO_ERROR_MESSAGE},
    }
    assert module.opened == [_NET_DEVICE_ID]
    assert (device.cancel_calls, device.close_calls) == (0, 0)


def test_a_capabilities_request_that_cannot_read_options_reports_options_error() -> (
    None
):
    """A ``get_options()`` that raises is reported, and the device still closed."""
    device = _OptionsDevice([], options_error=RuntimeError("no options"))
    module = _OptionsModule(device)

    reply = _listing_child.respond(_capabilities_request(), module)

    assert reply == {
        "devices": [],
        "options_error": {"type": "RuntimeError", "message": "no options"},
    }
    assert (device.cancel_calls, device.close_calls) == (1, 1)


def test_a_capabilities_request_swallows_a_failed_cancel_and_close() -> None:
    """
    The options already read are the answer, whatever closing does.

    Both closing steps are still attempted when the first one raises.
    """
    device = _OptionsDevice([_MODE_OPTION], closing_error=RuntimeError("stuck"))
    module = _OptionsModule(device)

    reply = _listing_child.respond(_capabilities_request(), module)

    assert reply == {
        "devices": [],
        "options": [["mode", "list", ["Gray", "Color"]]],
    }
    assert (device.cancel_calls, device.close_calls) == (1, 1)


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


_START_ERROR = {"type": "RuntimeError", "message": "SANE could not start"}


@pytest.mark.parametrize(
    ("request_line", "expected"),
    [
        pytest.param(
            {"open": None, "alarm": 35},
            {"devices": [], "list_error": _START_ERROR, "init_error": _START_ERROR},
            id="listing",
        ),
        pytest.param(
            {"open": _NET_DEVICE_ID, "alarm": 35},
            {
                "devices": [],
                "list_error": _START_ERROR,
                "opened": False,
                "open_error": _START_ERROR,
                "init_error": _START_ERROR,
            },
            id="listing-and-open",
        ),
        pytest.param(
            {"open": None, "capabilities": "test:0", "alarm": 35},
            {"devices": [], "init_error": _START_ERROR},
            id="capabilities",
        ),
    ],
)
def test_main_reports_a_sane_that_will_not_start_as_init_error(
    monkeypatch: pytest.MonkeyPatch,
    request_line: dict[str, object],
    expected: dict[str, object],
) -> None:
    """
    A SANE that cannot start is named as such, beside today's failure keys.

    The parent can then tell a library that would not start from a listing
    or an open that failed, and a listing request still fails its listing and
    its open alike.
    """
    events: list[str] = []
    fake = _OrderedFakeSane(events, init_error=RuntimeError("SANE could not start"))

    status, output = _run_main(
        monkeypatch, json.dumps(request_line) + "\n", fake, events
    )

    assert status == 0
    assert json.loads(output) == expected
    assert fake.get_devices_call_count == 0


def test_main_reports_a_missing_python_sane_as_init_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An interpreter without python-sane is a library that would not start.

    A ``None`` in ``sys.modules`` makes the import raise as a missing module
    does.  The alarm is replaced, as ``_run_main`` replaces it.
    """
    monkeypatch.setitem(sys.modules, "sane", None)
    monkeypatch.setattr(_listing_child.signal, "alarm", lambda _seconds: 0)
    reply_channel = io.StringIO()

    status = _listing_child.main(
        io.StringIO('{"open": null, "alarm": 35}\n'), reply_channel
    )

    reply = json.loads(reply_channel.getvalue())
    error = reply.get("init_error")
    assert status == 0
    assert reply == {"devices": [], "list_error": error, "init_error": error}
    assert error["type"] == "ModuleNotFoundError"


def test_the_child_loads_the_unwinder_through_saneless_loader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The child's late import reaches saneless's own unwinder loader."""
    calls: list[str] = []
    monkeypatch.setattr(
        thread_unwinder, "load_thread_unwinder", lambda: calls.append("loaded")
    )

    _listing_child.load_thread_unwinder()

    assert calls == ["loaded"]


def test_main_loads_the_unwinder_before_python_sane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The C library's thread unwinder is loaded before python-sane is imported.

    A backend thread that is the first to end would otherwise be the one to
    load it, and an asynchronous cancel there can leave the dynamic loader's
    lock held.  The alarm still comes first, so the unwinder's own work is
    under the deadline too.
    """
    events: list[str] = []
    fake = _OrderedFakeSane(events, devices=[_TEST_DEVICE])
    real_import = importlib.import_module

    def record_unwinder() -> None:
        events.append("unwinder")

    def record_import(name: str, package: str | None = None) -> ModuleType:
        events.append(f"import {name}")
        return real_import(name, package)

    monkeypatch.setattr(_listing_child, "load_thread_unwinder", record_unwinder)
    monkeypatch.setattr(_listing_child.importlib, "import_module", record_import)

    status, _ = _run_main(monkeypatch, '{"open": null, "alarm": 35}\n', fake, events)

    assert status == 0
    assert events == ["alarm 35", "unwinder", "import sane", "init"]


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
        pytest.param(
            '{"open": null, "capabilities": 5, "alarm": 35}\n',
            id="capabilities-not-a-string",
        ),
        pytest.param(
            '{"open": null, "capabilities": "", "alarm": 35}\n',
            id="capabilities-empty",
        ),
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


def test_loading_the_child_loads_only_the_stdio_helper_and_no_sane() -> None:
    """
    Loading the child file pulls in no python-sane, and almost no saneless.

    The child runs in a bare interpreter, and every module it loads widens
    what a tampered path could inject, so the only saneless modules it may
    pull in are the stdlib-only reply-pipe helper and the import-free packages
    above it.  Loading the file in a fresh interpreter under a private name
    must not run ``main()`` either, so python-sane stays unimported.
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

    result = subprocess.run(
        [sys.executable, "-I", "-c", script],
        env={
            **os.environ,
            "SANELESS_TEST_MODULE": str(_CHILD_PATH),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == repr(_SANELESS_MODULES_LOADED)


def test_the_child_imports_only_allowlisted_standard_library_modules() -> None:
    """
    Every import in the child names an allowlisted module.

    The child runs in a bare interpreter, so anything it imported beyond this
    list would either drag the package's heavy modules in or widen what a
    tampered path could inject.  python-sane is never imported statically: it is loaded by
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
