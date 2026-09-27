"""
Test doubles for driving a multi-page scan from a script.

A multi-page run asks its ``PassCoordinator`` a question between passes: is
there another page, what about the pages that look blank, what now that a pass
failed.  ``ScriptedPassCoordinator`` answers those questions from a list fixed
in advance and keeps every prompt it was shown, so a test can say both what the
operator answered and what the operator was asked.

Import it as ``from tests.multi_page_support import ...``; the bare
``multi_page_support`` form raises ``ModuleNotFoundError`` under pytest 9's
importlib mode, for the same reason ``tests.conftest`` is imported by its
package path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, override

from saneless.config import ProfileConfig
from saneless.pipeline import PassCoordinator
from saneless.vocabulary import PassAnswer
from tests.conftest import build_settings

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from saneless.config import Settings
    from saneless.vocabulary import PassPrompt

__all__ = ["DUPLEX_PROFILE", "ScriptedPassCoordinator", "multi_page_settings"]

# The name of the manual-duplex profile ``multi_page_settings`` adds, which a
# multi-page scan must refuse.
DUPLEX_PROFILE = "duplex"

# The two answers that are not the operator's choice: the clock running out,
# and saneless stopping.  A prompt never offers them, so a script may return
# them whatever the prompt offered.
_NOT_OFFERED_ANSWERS = frozenset({PassAnswer.TIMED_OUT, PassAnswer.INTERRUPTED})


class ScriptedPassCoordinator(PassCoordinator):
    """
    A pass coordinator whose operator answers from a script, in order.

    Each ``ask`` records the prompt, runs the optional ``on_ask`` hook, and
    returns the next scripted answer.  It never blocks.

    It fails the test loudly rather than improvising in two cases: when the run
    asks more questions than the script has answers, and when a scripted
    operator answer is not one the prompt offered -- a script that answers
    "skip blanks" to "is there another page?" describes a run that cannot
    happen.  ``TIMED_OUT`` and ``INTERRUPTED`` are exempt from the second rule,
    because no prompt offers them.

    Subclasses the ABC rather than duck-typing it, so a change to the contract
    is caught by the type checkers here as well as in the real coordinators.

    Attributes:
        prompts: Every prompt this coordinator was asked, in order.

    """

    def __init__(
        self,
        answers: Sequence[PassAnswer],
        *,
        on_ask: Callable[[PassPrompt], None] | None = None,
        cause: Exception | None = None,
    ) -> None:
        """
        Prepare the script.

        Args:
            answers: The answers to give, one per question, in order.
            on_ask: Called with each prompt after it is recorded and before it
                is answered, so a test can act while the run is waiting.
            cause: What ``abort_cause`` reports after an ``ABORT`` answer, to
                play a coordinator whose prompt broke rather than an operator
                who gave up.

        """
        self._answers = list(answers)
        self._on_ask = on_ask
        self._cause = cause
        self._last: PassAnswer | None = None
        self.prompts: list[PassPrompt] = []

    @override
    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Record the prompt and return the next scripted answer.

        Args:
            prompt: The question the run is asking.

        Returns:
            The next answer in the script.

        Raises:
            AssertionError: If the script is exhausted, or if the next answer
                is an operator answer the prompt did not offer.

        """
        self.prompts.append(prompt)
        if self._on_ask is not None:
            self._on_ask(prompt)
        if not self._answers:
            msg = (
                f"prompt {prompt.number} ({prompt.wait}) was asked, but the "
                f"script ran out after {len(self.prompts) - 1} answer(s)"
            )
            raise AssertionError(msg)
        answer = self._answers.pop(0)
        if answer not in _NOT_OFFERED_ANSWERS and answer not in prompt.offered:
            offered = ", ".join(sorted(prompt.offered))
            msg = (
                f"the script answers {answer} to prompt {prompt.number} "
                f"({prompt.wait}), which offered only: {offered}"
            )
            raise AssertionError(msg)
        self._last = answer
        return answer

    @property
    @override
    def abort_cause(self) -> Exception | None:
        """
        The scripted cause, once the coordinator has answered ``ABORT``.

        Returns:
            ``cause`` after an ``ABORT`` answer, otherwise ``None``.

        """
        return self._cause if self._last is PassAnswer.ABORT else None


def multi_page_settings(
    tmp_path: Path, *, detection: bool = False, timeout: int = 600
) -> Settings:
    """
    Build settings for a multi-page run, every directory under ``tmp_path``.

    The ``default`` profile is a flatbed, the headline multi-page case.  Beside
    it sit an ADF profile, so the configuration never has just one profile,
    and a manual-duplex profile named ``DUPLEX_PROFILE``, which a multi-page
    scan must be refused on.  Scratch space and ``failed/`` sit on separate
    subtrees, as ``build_settings`` arranges.

    Args:
        tmp_path: The test's own temporary directory.
        detection: Whether the ``default`` profile has empty-page detection on.
            Off by default, so a test about the loop is never also a test
            about blank pages.
        timeout: The operator-wait bound, in seconds, every prompt carries.

    Returns:
        A fresh Settings instance.

    """
    settings = build_settings(
        tmp_path,
        profiles={
            "default": ProfileConfig(
                source="Flatbed", enable_empty_page_detection=detection
            ),
            "adf": ProfileConfig(source="ADF"),
            DUPLEX_PROFILE: ProfileConfig(source="ADF", duplex="manual"),
        },
    )
    settings.output.operator_wait_timeout_seconds = timeout
    return settings
