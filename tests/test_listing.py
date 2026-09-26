"""
Tests for ``saneless.scanner.listing``, the launcher every scanner listing uses.

The launcher starts a short-lived child interpreter, hands it one JSON request
line on stdin and waits for it under a hard deadline.  Whatever happens, the
child is dead and reaped before the launcher returns or raises: a child that
overruns the deadline is killed, and so is one still running when the wait is
interrupted.  A child that dies from a signal is a crashed listing, one that
runs out of time is a timed-out listing.  Both are ``ScanError`` subclasses,
and each logs one WARNING naming nothing but the signal or the deadline.  Anything else that
is not exactly the child's reply schema is a plain ``ScanError``.

The child's environment is the parent's minus every ``SANELESS_*`` variable,
with ``SANE_NET_HOSTS`` taken from the one derivation the scanner check uses.

No test here touches libsane.  Each points the launcher at a stand-in child
script written into ``tmp_path``, run exactly as the real child is run.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import pytest

from saneless.exceptions import (
    ListingCrashedError,
    ListingTimedOutError,
    ScanError,
    ScanInterrupted,
)
from saneless.scanner import listing
from saneless.scanner.listing import (
    ChildError,
    ListingReply,
    ListingRequest,
    child_environment,
    run_listing_child,
)

_LOGGER = "saneless.scanner.listing"
_NET_ID = "net:scanbox.lan:test:0"
_TEST_DEVICE = ("test:0", "Noname", "frontend-tester", "virtual device")
_NO_ANSWER = "The scanner library returned no answer while listing scanners"

# Generous on purpose: a passing timeout test finishes in well under a second.
# This bound only turns "the launcher never gave up" into a failure instead of
# a hung session.
_ELAPSED_CEILING_SECONDS = 5.0

# The real method, kept before any test swaps it for a spy.
_REAL_COMMUNICATE = subprocess.Popen.communicate

# Every stand-in below uses the standard library only, because the launcher
# runs it in isolated mode.

_REPLY_CHILD = """\
import json
import sys

sys.stdin.readline()
device = ["test:0", "Noname", "frontend-tester", "virtual device"]
sys.stdout.write(json.dumps({"devices": [device]}) + "\\n")
"""

_ECHO_REQUEST_CHILD = """\
import json
import sys

line = sys.stdin.readline().rstrip("\\n")
sys.stdout.write(json.dumps({"devices": [[line, "v", "m", "t"]]}) + "\\n")
"""

_NOISY_CHILD = """\
import json
import sys

sys.stdin.readline()
sys.stdout.write("noise\\n")
device = ["test:0", "Noname", "frontend-tester", "virtual device"]
sys.stdout.write(json.dumps({"devices": [device]}) + "\\n")
sys.stdout.write("\\n")
"""

_SEGFAULT_CHILD = """\
import os
import resource
import signal
from pathlib import Path

Path(os.environ["LISTING_TEST_PIDFILE"]).write_text(str(os.getpid()))
# No core file: this crash is deliberate.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.kill(os.getpid(), signal.SIGSEGV)
"""

_SLEEPER_CHILD = """\
import os
import signal
from pathlib import Path

Path(os.environ["LISTING_TEST_PIDFILE"]).write_text(str(os.getpid()))
signal.pause()
"""

_ALARM_CHILD = """\
import os
import signal

os.kill(os.getpid(), signal.SIGALRM)
"""

_EXIT_THREE_CHILD = """\
import sys

sys.exit(3)
"""

_SILENT_CHILD = """\
import sys

sys.stdin.readline()
"""

_NOISE_ONLY_CHILD = """\
import sys

sys.stdin.readline()
sys.stdout.write("starting up\\nnothing to report\\n")
"""

_ENVIRONMENT_CHILD = """\
import json
import os
import sys

sys.stdin.readline()
keys = json.dumps(sorted(k for k in os.environ if k.startswith("SANELESS_")))
sys.stdout.write(json.dumps({"devices": [[keys, "v", "m", "t"]]}) + "\\n")
"""

_FLAGS_CHILD = """\
import json
import sys

sys.stdin.readline()
isolated = str(sys.flags.isolated)
sys.stdout.write(json.dumps({"devices": [[isolated, "v", "m", "t"]]}) + "\\n")
"""


def _use_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str) -> Path:
    """
    Write a stand-in child into ``tmp_path`` and point the launcher at it.

    Args:
        monkeypatch: Undoes the redirection after the test.
        tmp_path: Where the script is written.
        source: The script's text.

    Returns:
        The script's path.

    """
    child = tmp_path / "stand_in_child.py"
    child.write_text(source, encoding="utf-8")
    monkeypatch.setattr(listing, "_CHILD_FILE", child)
    return child


def _pidfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Name a file a stand-in child writes its PID into.

    The variable has no ``SANELESS_`` prefix, so it survives the child
    environment's strip.

    Args:
        monkeypatch: Sets the variable for this test only.
        tmp_path: Where the file goes.

    Returns:
        The file's path; it exists only once the child has written it.

    """
    path = tmp_path / "child.pid"
    monkeypatch.setenv("LISTING_TEST_PIDFILE", str(path))
    return path


def _assert_reaped(pid: int) -> None:
    """
    Assert a child process is gone and already waited for.

    ``waitpid`` raising ``ChildProcessError`` means this process has no
    unreaped child with that PID: not a running one, and not a zombie.

    Args:
        pid: The child's process id.

    """
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert not Path(f"/proc/{pid}").exists()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """
    Return the launcher's WARNING records.

    Args:
        caplog: The test's log capture.

    Returns:
        Every WARNING the launcher's logger emitted.

    """
    return [
        record
        for record in caplog.records
        if record.name == _LOGGER and record.levelno == logging.WARNING
    ]


def _spy_on_communicate(
    monkeypatch: pytest.MonkeyPatch, interruption: BaseException | None = None
) -> list[subprocess.Popen[bytes]]:
    """
    Record every process whose ``communicate`` the launcher calls.

    ``Popen.communicate`` is replaced on the class for this test only, so the
    launcher still starts, kills and waits for a real child through the real
    ``Popen``.  With an interruption, the wait is interrupted while the child
    is still running, which is what a Ctrl-C or a server stop does to it.

    Args:
        monkeypatch: Undoes the replacement after the test.
        interruption: Raised instead of waiting, when given.

    Returns:
        The list each process is appended to, in the order they were waited on.

    """
    started: list[subprocess.Popen[bytes]] = []

    def communicate(
        self: subprocess.Popen[bytes],
        data: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        started.append(self)
        if interruption is not None:
            raise interruption
        return _REAL_COMMUNICATE(self, data, timeout)

    monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
    return started


class TestHappyPath:
    """A child that answers is decoded into a reply."""

    def test_a_reply_is_decoded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One device on stdout becomes one device tuple in the reply."""
        _use_child(monkeypatch, tmp_path, _REPLY_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply == ListingReply(devices=(_TEST_DEVICE,))

    def test_the_request_line_carries_the_device_and_the_alarm(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """
        The child is sent one JSON line naming the device and its own alarm.

        The alarm is the deadline rounded up plus a margin, so an orphaned
        child cannot outlive its parent's bound by long.  The device id goes
        on stdin, never in argv.
        """
        _use_child(monkeypatch, tmp_path, _ECHO_REQUEST_CHILD)

        reply = run_listing_child(ListingRequest(open=_NET_ID), configured_host="")

        line = reply.devices[0][0]
        assert line == '{"open": "net:scanbox.lan:test:0", "alarm": 35}'
        assert json.loads(line) == {"open": _NET_ID, "alarm": 35}

    def test_only_the_last_non_empty_line_is_the_reply(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Output before the reply, and a blank line after it, are ignored."""
        _use_child(monkeypatch, tmp_path, _NOISY_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply.devices == (_TEST_DEVICE,)


class TestCrash:
    """A child that dies from a signal is a crashed listing."""

    def test_a_segfault_is_reported_by_signal_name_and_reaped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        SIGSEGV raises the crash error, logs one WARNING and leaves no child.

        The log line names the signal and nothing host-derived: neither the
        device id nor any part of it.
        """
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        pidfile = _pidfile(monkeypatch, tmp_path)
        _use_child(monkeypatch, tmp_path, _SEGFAULT_CHILD)

        with pytest.raises(ListingCrashedError) as crashed:
            run_listing_child(ListingRequest(open=_NET_ID), configured_host="")

        assert isinstance(crashed.value, ScanError)
        assert "SIGSEGV" in str(crashed.value)
        records = _warnings(caplog)
        assert len(records) == 1
        message = records[0].getMessage()
        assert "SIGSEGV" in message
        assert "net:" not in message
        assert "scanbox" not in message
        _assert_reaped(int(pidfile.read_text(encoding="utf-8")))

    def test_a_sigalrm_death_is_a_timeout_not_a_crash(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The child's own alarm firing means it ran out of time."""
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        _use_child(monkeypatch, tmp_path, _ALARM_CHILD)

        with pytest.raises(ListingTimedOutError):
            run_listing_child(ListingRequest(), configured_host="")

        assert len(_warnings(caplog)) == 1


class TestDeadline:
    """A child that outlives the deadline is killed and reaped."""

    def test_an_overrun_is_stopped_at_the_patched_deadline_and_reaped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A sleeping child raises the timeout error, and is gone when it does.

        The deadline is patched after the module was imported, so this also
        proves the launcher reads it at call time.
        """
        caplog.set_level(logging.WARNING, logger=_LOGGER)
        monkeypatch.setattr(listing, "LISTING_DEADLINE_SECONDS", 0.2)
        started = _spy_on_communicate(monkeypatch)
        _use_child(monkeypatch, tmp_path, _SLEEPER_CHILD)
        _pidfile(monkeypatch, tmp_path)

        began = time.monotonic()
        with pytest.raises(ListingTimedOutError) as timed_out:
            run_listing_child(ListingRequest(open=_NET_ID), configured_host="")
        elapsed = time.monotonic() - began

        assert isinstance(timed_out.value, ScanError)
        assert "in time" in str(timed_out.value)
        assert elapsed < _ELAPSED_CEILING_SECONDS
        records = _warnings(caplog)
        assert len(records) == 1
        assert "net:" not in records[0].getMessage()
        assert len(started) == 1
        assert started[0].returncode is not None
        _assert_reaped(started[0].pid)


class TestNoAnswer:
    """A child that exits without a usable reply is a plain ScanError."""

    @pytest.mark.parametrize(
        "source",
        [
            pytest.param(_EXIT_THREE_CHILD, id="positive-exit-status"),
            pytest.param(_SILENT_CHILD, id="exit-zero-empty-stdout"),
            pytest.param(_NOISE_ONLY_CHILD, id="never-writes-a-reply"),
        ],
    )
    def test_no_reply_is_no_answer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
    ) -> None:
        """Neither subclass: the child did not crash and did not overrun."""
        _use_child(monkeypatch, tmp_path, source)

        with pytest.raises(ScanError) as failed:
            run_listing_child(ListingRequest(), configured_host="")

        assert type(failed.value) is ScanError
        assert str(failed.value) == _NO_ANSWER

    def test_a_missing_child_file_is_no_answer(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The interpreter's own non-zero exit is reported the same way."""
        monkeypatch.setattr(listing, "_CHILD_FILE", tmp_path / "does_not_exist.py")

        with pytest.raises(ScanError) as failed:
            run_listing_child(ListingRequest(), configured_host="")

        assert type(failed.value) is ScanError
        assert str(failed.value) == _NO_ANSWER


class TestInterrupt:
    """An interrupted wait kills and reaps the child before re-raising."""

    @pytest.mark.parametrize(
        "interruption",
        [
            pytest.param(KeyboardInterrupt(), id="keyboard-interrupt"),
            pytest.param(ScanInterrupted("stopping", signum=None), id="scan-stop"),
        ],
    )
    def test_an_interrupt_propagates_after_the_child_is_reaped(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        interruption: BaseException,
    ) -> None:
        """The interruption is re-raised as it was, with no child left behind."""
        started = _spy_on_communicate(monkeypatch, interruption)
        _use_child(monkeypatch, tmp_path, _SLEEPER_CHILD)
        _pidfile(monkeypatch, tmp_path)

        with pytest.raises(type(interruption)) as raised:
            run_listing_child(ListingRequest(), configured_host="")

        assert raised.value is interruption
        assert len(started) == 1
        assert started[0].returncode is not None
        _assert_reaped(started[0].pid)


def _valid_device_line(name: str) -> bytes:
    """
    Build one valid reply line holding a single device.

    Args:
        name: The device's name field.

    Returns:
        The reply line, newline included.

    """
    return json.dumps({"devices": [[name, "v", "m", "t"]]}).encode() + b"\n"


class TestReplyDecoder:
    """``ListingReply.from_stdout`` accepts exactly the child's schema."""

    @pytest.mark.parametrize(
        "out",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"not json\n", id="not-json"),
            pytest.param(b"[]\n", id="not-an-object"),
            pytest.param(b"{}\n", id="no-devices"),
            pytest.param(b'{"devices": "x"}\n', id="devices-not-a-list"),
            pytest.param(b'{"devices": [["a", "b", "c"]]}\n', id="three-fields"),
            pytest.param(b'{"devices": [["a", "b", "c", 4]]}\n', id="non-str-field"),
            pytest.param(b'{"devices": [], "extra": 1}\n', id="unknown-key"),
            pytest.param(
                b'{"devices": [], "list_error": {"type": "RuntimeError"}}\n',
                id="list-error-without-message",
            ),
            pytest.param(
                b'{"devices": [], "list_error": null}\n', id="list-error-null"
            ),
            pytest.param(b'{"devices": [], "opened": "yes"}\n', id="opened-not-bool"),
            pytest.param(b'{"devices": [], "opened": null}\n', id="opened-null"),
            pytest.param(
                b'{"devices": [], "opened": true, '
                b'"open_error": {"type": "error", "message": "x"}}\n',
                id="open-error-with-opened-true",
            ),
            pytest.param(
                b'{"devices": [], "open_error": {"type": "error", "message": "x"}}\n',
                id="open-error-without-opened",
            ),
            pytest.param(b'{"devices": [["\xff", "v", "m", "t"]]}\n', id="bad-utf8"),
            pytest.param(b"[" * 100_000 + b"]" * 100_000 + b"\n", id="deep-nesting"),
            pytest.param(b'{"devices": []}\nnoise\n', id="reply-not-the-last-line"),
        ],
    )
    def test_off_schema_output_is_a_plain_scan_error(self, out: bytes) -> None:
        """Nothing off-schema escapes as a json, Unicode or key error."""
        with pytest.raises(ScanError) as failed:
            ListingReply.from_stdout(out)

        assert type(failed.value) is ScanError
        assert str(failed.value) == _NO_ANSWER
        assert failed.value.__cause__ is None

    def test_a_reply_over_the_size_cap_is_refused(self) -> None:
        """A valid reply longer than the cap is refused; one under it is not."""
        cap = listing._MAX_REPLY_BYTES
        over = _valid_device_line("a" * cap)
        under = _valid_device_line("a" * (cap - 100))
        assert len(under) <= cap

        with pytest.raises(ScanError, match=_NO_ANSWER):
            ListingReply.from_stdout(over)
        assert ListingReply.from_stdout(under).devices[0][0] == "a" * (cap - 100)

    def test_an_empty_device_list_decodes(self) -> None:
        """A child that found nothing reports an empty tuple."""
        assert ListingReply.from_stdout(b'{"devices": []}\n') == ListingReply(
            devices=()
        )

    def test_a_list_error_decodes(self) -> None:
        """A listing that raised in the child carries its type and message."""
        reply = ListingReply.from_stdout(
            b'{"devices": [], "list_error": {"type": "RuntimeError", "message": "boom"}}\n'
        )

        assert reply.list_error == ChildError("RuntimeError", "boom")
        assert reply.opened is None
        assert reply.open_error is None

    def test_a_failed_open_decodes(self) -> None:
        """A requested open that failed carries its error beside ``opened``."""
        reply = ListingReply.from_stdout(
            b'{"devices": [], "opened": false, '
            b'"open_error": {"type": "error", "message": "Error during device I/O"}}\n'
        )

        assert reply.opened is False
        assert reply.open_error == ChildError("error", "Error during device I/O")

    def test_a_successful_open_decodes(self) -> None:
        """A requested open that worked reports ``opened`` and no error."""
        reply = ListingReply.from_stdout(b'{"devices": [], "opened": true}\n')

        assert reply.opened is True
        assert reply.open_error is None

    def test_a_lone_surrogate_round_trips(self) -> None:
        """A device name with an undecodable byte comes back as the same str."""
        reply = ListingReply.from_stdout(b'{"devices": [["\\udcff", "v", "m", "t"]]}\n')

        assert reply.devices[0][0] == "\udcff"


class TestChildEnvironment:
    """``child_environment`` strips secrets and derives the host list once."""

    def test_saneless_variables_are_dropped_and_the_host_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token never reaches the child; SANE's own settings do."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "secret")
        monkeypatch.setenv("SANE_CONFIG_DIR", "/x")

        env = child_environment("scanbox.lan")

        assert not [key for key in env if key.startswith("SANELESS_")]
        assert "secret" not in env.values()
        assert env["SANE_CONFIG_DIR"] == "/x"
        assert env["SANE_NET_HOSTS"] == "scanbox.lan"

    def test_an_exported_host_list_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A non-empty exported SANE_NET_HOSTS is passed on over the setting."""
        monkeypatch.setenv("SANE_NET_HOSTS", "a.lan:b.lan")

        assert child_environment("scanbox.lan")["SANE_NET_HOSTS"] == "a.lan:b.lan"

    @pytest.mark.parametrize(
        "exported",
        [pytest.param(None, id="unset"), pytest.param("", id="exported-empty")],
    )
    def test_no_host_list_means_no_variable(
        self, monkeypatch: pytest.MonkeyPatch, exported: str | None
    ) -> None:
        """With nothing to dial, the child gets no SANE_NET_HOSTS at all."""
        if exported is None:
            monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
        else:
            monkeypatch.setenv("SANE_NET_HOSTS", exported)

        assert "SANE_NET_HOSTS" not in child_environment("")

    def test_only_the_launcher_plumbing_reaches_the_child(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Inside the child, the only SANELESS_ variables are the launcher's two."""
        monkeypatch.setenv("SANELESS_PAPERLESS__TOKEN", "secret")
        _use_child(monkeypatch, tmp_path, _ENVIRONMENT_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert json.loads(reply.devices[0][0]) == [
            "SANELESS_LISTING_CHILD",
            "SANELESS_LISTING_PYTHON",
        ]

    def test_the_child_runs_in_isolated_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``-I`` reaches the interpreter, so no PYTHON* variable steers it."""
        _use_child(monkeypatch, tmp_path, _FLAGS_CHILD)

        reply = run_listing_child(ListingRequest(), configured_host="")

        assert reply.devices[0][0] == "1"


def test_the_child_argv_is_literal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    The argv holds no path and no device id: both travel elsewhere.

    The interpreter and the script are in the environment and the device id
    is on stdin, because argv is readable by every local user.  ``exec``
    makes the child's PID the interpreter's own, so killing and waiting on it
    leaves no grandchild.
    """
    started = _spy_on_communicate(monkeypatch)
    _use_child(monkeypatch, tmp_path, _REPLY_CHILD)

    run_listing_child(ListingRequest(open=_NET_ID), configured_host="")

    assert len(started) == 1
    assert started[0].args == (
        "/bin/sh",
        "-c",
        'exec "$SANELESS_LISTING_PYTHON" -I "$SANELESS_LISTING_CHILD"',
    )


def test_the_deadline_is_thirty_seconds() -> None:
    """The documented bound, and the alarm margin the request line relies on."""
    assert listing.LISTING_DEADLINE_SECONDS == 30.0
    assert listing._ALARM_MARGIN_SECONDS == 5


def test_the_crash_and_timeout_errors_are_scan_errors() -> None:
    """Every existing ``except ScanError`` boundary still catches both."""
    assert issubclass(ListingCrashedError, ScanError)
    assert issubclass(ListingTimedOutError, ScanError)
    assert not issubclass(ListingCrashedError, ListingTimedOutError)
    assert not issubclass(ListingTimedOutError, ListingCrashedError)
