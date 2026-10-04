"""
Seams through which a scan run waits for the operator.

The manual-duplex flip and the multi-page questions each have a coordinator
the web worker and the CLI implement, and both share the single-answer slot
that makes an operator's answer final.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import TYPE_CHECKING

from saneless.vocabulary import FlipOutcome

if TYPE_CHECKING:
    from saneless.vocabulary import PassAnswer, PassPrompt

__all__ = ["AnswerSlot", "FlipAnswerSlot", "FlipCoordinator", "PassCoordinator"]


class FlipCoordinator(ABC):
    """
    The one way a manual-duplex run waits for the operator to flip the stack.

    Pass A has fed the fronts; before pass B the pipeline asks this seam a
    single question -- has the stack been turned? -- and gets back a single,
    total ``FlipOutcome``.  The web worker answers it from the Continue and
    Abort routes, and the CLI answers it from a terminal prompt.

    This is an ``ABC`` and not a ``typing.Protocol`` on purpose, and the rule is
    observable in the tree: ``Protocol`` describes shapes this project does not
    own (``SaneDevice`` for python-sane's handle, ``_SettingsFactory`` for
    pydantic's constructor), while ``ABC`` defines seams the project implements
    itself (``ScannerBackend``).  Where this seam is called a "protocol" in
    lower case, the word means "contract", not ``typing.Protocol``.
    """

    @abstractmethod
    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Block until the flip wait resolves, for at most ``timeout`` seconds.

        An implementation answers once, and its answer is final: whichever of
        Continue, Abort or the clock resolves the wait first is what this
        returns, and a signal arriving after that is dropped.

        Args:
            timeout: The longest the wait may hold the calling thread, in
                seconds.  ``0`` is a valid bound and returns at once.

        Returns:
            ``CONTINUED`` when the operator flipped the stack, ``ABORTED`` when
            they gave up at the prompt, or ``TIMED_OUT`` when neither happened
            within ``timeout``.

        """

    @property
    def abort_cause(self) -> Exception | None:
        """
        Why the wait answered ``ABORTED``, when it was not the operator's choice.

        An ``ABORTED`` answer usually means someone gave up at the prompt, and
        the pipeline reports that as a cancellation.  But a coordinator can
        also answer ``ABORTED`` because its prompt broke -- a read error such as
        an I/O error or undecodable input -- and nobody chose to stop.  End of
        input, a closed terminal included, is not such a break: it is the
        operator's cancel.  Such a coordinator returns the exception here, so
        the pipeline records a failure rather than a cancellation without a
        fourth ``FlipOutcome`` member.

        Concrete rather than abstract, so a coordinator whose aborts are always
        an operator's needs no change.

        Returns:
            The exception that forced the abort, or ``None`` when there was
            none -- including whenever the answer was not ``ABORTED``.

        """
        return None


class PassCoordinator(ABC):
    """
    The one way a multi-page run asks the operator what happens next.

    Between passes the pipeline asks this seam one question at a time -- is
    there another page, what about the pages that look blank, what now that a
    pass failed -- and gets back a single ``PassAnswer``.  The web worker
    answers it from the multi-page routes, and the CLI from a terminal prompt.

    It is a sibling of ``FlipCoordinator`` and deliberately not a widening of
    it: the manual-duplex flip wait keeps its own seam and its own outcomes, so
    a multi-page answer can never reach a manual-duplex run.

    This is an ``ABC`` and not a ``typing.Protocol`` on purpose, and the rule is
    observable in the tree: ``Protocol`` describes shapes this project does not
    own (``SaneDevice`` for python-sane's handle, ``_SettingsFactory`` for
    pydantic's constructor), while ``ABC`` defines seams the project implements
    itself (``ScannerBackend``, ``FlipCoordinator``).
    """

    @abstractmethod
    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Put ``prompt`` to the operator and block until it resolves.

        The wait holds the calling thread for at most
        ``prompt.timeout_seconds``.  An implementation answers once, and its
        answer is final: whichever of the operator, the clock or a stop
        resolves the wait first is what this returns.

        A coordinator that could not show the prompt -- its terminal broke,
        say -- must report ``ABORT`` and set ``abort_cause``, so the pipeline
        records a failure rather than an operator's cancel.

        Args:
            prompt: The question, and the only answers it accepts.

        Returns:
            A member of ``prompt.offered``; ``TIMED_OUT`` when nobody answered
            within ``prompt.timeout_seconds``; or ``INTERRUPTED`` when saneless
            is stopping.

        """

    @property
    def abort_cause(self) -> Exception | None:
        """
        Why the wait answered ``ABORT``, when it was not the operator's choice.

        An ``ABORT`` answer usually means someone gave up at the prompt, and
        the pipeline reports that as a cancellation.  But a coordinator can
        also answer ``ABORT`` because its prompt broke -- a read error such as
        an I/O error or undecodable input -- and nobody chose to stop.  End of
        input, a closed terminal included, is not such a break: it is the
        operator's cancel.  Such a coordinator returns the exception here, so
        the pipeline records a failure rather than a cancellation without a
        further ``PassAnswer`` member.

        Concrete rather than abstract, so a coordinator whose aborts are always
        an operator's needs no change.

        Returns:
            The exception that forced the abort, or ``None`` when there was
            none -- including whenever the answer was not ``ABORT``.

        """
        return None

    @property
    def stopping(self) -> bool:
        """
        Whether saneless is stopping, so no further pass may start.

        An answer claimed just before a stop keeps its meaning at the prompt,
        but the pass it asks for must not begin: a pass outlasts the bounded
        stop, and a run killed inside one never reaches the guard that keeps
        its accepted pages.  The run checks this before every pass after the
        first and ends as interrupted instead.

        Concrete rather than abstract, so a coordinator that is never stopped
        this way -- the CLI's, where a signal interrupts the run directly --
        needs no change.

        Returns:
            True once stopping has begun; False otherwise.

        """
        return False


class AnswerSlot[T: StrEnum]:
    """
    One answer, claimed once, and final: the claim every coordinator shares.

    The web worker's and the CLI's coordinators differ in where an answer comes
    from -- the web routes, or a terminal prompt -- but not in how it is
    claimed.  That claim lives here, once, so a fix to it reaches both.  It is
    generic over the answer's enum, so the flip wait (``FlipOutcome``) and the
    multi-page waits (``PassAnswer``) share the claim without sharing anything
    else.  This is a concrete helper the coordinators compose, not a seam:
    ``FlipCoordinator`` and ``PassCoordinator`` stay the only contracts the
    pipeline waits on, and anything a coordinator adds on top -- the web one's
    arming, for instance -- stays in that coordinator.

    The answer is written under the lock *before* the event is set, so a waiter
    that wakes always finds an answer to read -- there is no window in which the
    event says "resolved" and the slot still says nothing.  That ordering is
    what makes it race-free by construction rather than by timing.
    """

    def __init__(self) -> None:
        """Start unanswered."""
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._outcome: T | None = None

    @property
    def answer(self) -> T | None:
        """The claimed answer, or ``None`` while the slot is unanswered."""
        with self._lock:
            return self._outcome

    def offer(self, outcome: T) -> bool:
        """
        Claim the answer with ``outcome`` if nothing has claimed it yet.

        An offer that loses leaves the slot and the event untouched.

        Args:
            outcome: The answer this caller is offering.

        Returns:
            Whether ``outcome`` became the answer.

        """
        with self._lock:
            if self._outcome is not None:
                return False
            self._outcome = outcome
        self._event.set()
        return True

    def settle(self, outcome: T) -> T:
        """
        Claim the answer with ``outcome`` unless one is claimed, and return it.

        This is the path that ends a wait: a timeout, or a CLI answer.
        Returning the answer in effect, rather than asserting one exists, is
        what narrows ``T | None`` to ``T`` without an
        ``assert`` -- which ``S101`` bans in ``src/``.

        Args:
            outcome: The answer this caller is offering.

        Returns:
            The claimed answer: ``outcome`` if it was first, otherwise the
            answer that beat it.

        """
        with self._lock:
            if self._outcome is None:
                self._outcome = outcome
            claimed = self._outcome
        self._event.set()
        return claimed

    def wait(self, timeout: float) -> None:
        """
        Block until the slot is answered, for at most ``timeout`` seconds.

        It reports nothing: the caller reads the result through ``settle``, which
        is right whether the wait was answered or expired.  A
        ``KeyboardInterrupt`` raised while waiting propagates to the caller.

        Args:
            timeout: The longest to wait, in seconds.

        """
        self._event.wait(timeout)


class FlipAnswerSlot(AnswerSlot[FlipOutcome]):
    """The flip wait's slot, by the name every caller and test already uses."""
