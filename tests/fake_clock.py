"""
A clock a test controls: time moves only when the test says so.

A retry loop bounded by a wall-clock budget cannot be tested against the real
clock without waiting that budget out, and a budget as long as pytest's own
per-test timeout cannot be tested that way at all.  ``FakeClock`` stands in for
both halves of the seam such a loop takes -- ``clock=fake.now`` and
``sleep=fake.sleep`` -- so every pause returns at once, time still advances by
exactly what was asked for, and the test can read back every wait afterwards.

It is a plain class rather than a fixture: a test builds one and hands its
bound methods straight to the constructor under test.
"""

from __future__ import annotations


class FakeClock:
    """
    A monotonic clock that advances only when slept on or advanced.

    ``sleep`` records the requested wait in ``waits`` and moves ``now`` on by
    exactly that much, without pausing.  ``advance`` moves ``now`` on without
    recording a wait, for time a test wants to pass during an attempt rather
    than between attempts.

    Attributes:
        waits: Every ``sleep`` duration, in the order it was requested.

    """

    def __init__(self, start: float = 0.0) -> None:
        """
        Start the clock at ``start`` with no waits recorded.

        Args:
            start: The first value ``now`` returns.

        """
        self._now = start
        self.waits: list[float] = []

    def now(self) -> float:
        """
        Read the clock.

        Returns:
            The current fake time, in seconds.

        """
        return self._now

    def sleep(self, seconds: float) -> None:
        """
        Record a wait and move the clock on by it, returning at once.

        Args:
            seconds: How long the caller asked to wait.

        """
        self._check(seconds)
        self.waits.append(seconds)
        self._now += seconds

    def advance(self, seconds: float) -> None:
        """
        Move the clock on without recording a wait.

        Args:
            seconds: How much fake time passes.

        """
        self._check(seconds)
        self._now += seconds

    @staticmethod
    def _check(seconds: float) -> None:
        """
        Refuse to move the clock backwards.

        Args:
            seconds: The requested step.

        Raises:
            ValueError: If ``seconds`` is negative, which no real clock or
                ``time.sleep`` accepts.

        """
        if seconds < 0:
            msg = f"a fake clock cannot move by a negative {seconds}s"
            raise ValueError(msg)
