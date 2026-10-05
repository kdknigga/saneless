"""
Drive the CLI's terminal prompts from a test without a terminal.

The CLI reads a prompt's answer on the main thread: it waits until stdin is
readable, then reads one line.  The wait is the one name a test replaces,
``saneless.cli._wait_readable``, because ``CliRunner``'s stdin has no file
descriptor to wait on.  Everything after the wait -- reading the line, parsing
it, asking again, end of input, a broken read -- then runs for real.

These are plain functions rather than fixtures, so a test calls the one it
needs with its own ``monkeypatch``.  Import them as
``from tests.prompt_support import readable``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import TextIO

    import pytest


class FakeClock:
    """
    A monotonic clock that only moves when a test moves it.

    Attributes:
        now: The current reading, in seconds.

    """

    def __init__(self, start: float = 1000.0) -> None:
        """
        Start the clock at ``start``.

        Args:
            start: The first reading, in seconds.

        """
        self.now = start

    def __call__(self) -> float:
        """
        Read the clock, as ``time.monotonic`` would be read.

        Returns:
            The current reading, in seconds.

        """
        return self.now

    def advance(self, seconds: float) -> None:
        """
        Move the clock forward.

        Args:
            seconds: How far to move it; a negative value moves it nowhere.

        """
        self.now += max(0.0, seconds)


def readable(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Make every prompt's wait report stdin readable at once.

    ``CliRunner``'s ``input=`` is then read line by line, and an exhausted
    input reads as end of input.

    Args:
        monkeypatch: Replaces ``saneless.cli._wait_readable``.

    """

    def ready(_stream: TextIO, _timeout: float) -> bool:
        """
        Report the stream readable without waiting.

        Returns:
            Always ``True``.

        """
        return True

    monkeypatch.setattr("saneless.cli._wait_readable", ready)


def never_readable(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    """
    Make every prompt's wait run out its whole timeout on ``clock``, unanswered.

    No wall-clock time passes: each wait moves the fake clock on by the
    timeout it was given and reports nothing to read, so a prompt's deadline
    passes the moment it is reached.

    Args:
        monkeypatch: Replaces ``saneless.cli._wait_readable`` and
            ``saneless.cli._monotonic``.
        clock: The clock the prompt reads its deadline from.

    """

    def silent(_stream: TextIO, timeout: float) -> bool:
        """
        Let ``timeout`` seconds pass on the fake clock with nothing typed.

        Returns:
            Always ``False``.

        """
        clock.advance(timeout)
        return False

    monkeypatch.setattr("saneless.cli._monotonic", clock)
    monkeypatch.setattr("saneless.cli._wait_readable", silent)


def broken_read(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    """
    Make every prompt's wait raise ``exc``, as a terminal that broke would.

    ``KeyboardInterrupt`` plays Ctrl-C arriving during the wait; an
    ``OSError`` or a ``UnicodeDecodeError`` plays a broken terminal.

    Args:
        monkeypatch: Replaces ``saneless.cli._wait_readable``.
        exc: What the wait raises.

    """

    def broken(_stream: TextIO, _timeout: float) -> bool:
        """
        Fail the wait.

        Raises:
            BaseException: ``exc``, every time.

        """
        raise exc

    monkeypatch.setattr("saneless.cli._wait_readable", broken)


def typed_after(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock, delays: Sequence[float]
) -> list[float]:
    """
    Make each line on stdin arrive after a pause on ``clock``, then nothing.

    The operator types the next line ``delays[i]`` seconds after the wait
    for it begins.  A wait shorter than that pause runs out unanswered and
    the rest of the pause carries over to the next wait, so a line can be
    typed after a deadline has passed.  Once ``delays`` is used up nobody
    types anything more, and every wait runs out its whole timeout.  No
    wall-clock time passes.

    Args:
        monkeypatch: Replaces ``saneless.cli._wait_readable`` and
            ``saneless.cli._monotonic``.
        clock: The clock the prompt reads its deadline from.
        delays: The pause before each line, in seconds, in order.

    Returns:
        The timeout each wait was given, filled in as the prompt waits.

    """
    pending = list(delays)
    waits: list[float] = []

    def wait(_stream: TextIO, timeout: float) -> bool:
        """
        Let time pass until the next line is typed, or ``timeout`` runs out.

        Returns:
            Whether a line was typed within ``timeout``.

        """
        waits.append(timeout)
        if pending and pending[0] <= timeout:
            clock.advance(pending.pop(0))
            return True
        if pending:
            pending[0] -= max(0.0, timeout)
        clock.advance(timeout)
        return False

    monkeypatch.setattr("saneless.cli._monotonic", clock)
    monkeypatch.setattr("saneless.cli._wait_readable", wait)
    return waits
