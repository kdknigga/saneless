"""
A multi-page scan's prompts read word for word, on the web and in a terminal.

Every sentence the operator reads between the passes of a multi-page document
comes from the vocabulary module: the web prompt's headline, buttons and notes,
the abort confirmation, the Multiple pages checkbox's label and help line, and
the ``--multi-page`` option's help, refusals and single-letter prompts.  Each
is pinned verbatim, so the web page and the terminal cannot drift apart and no
template or command composes prose of its own.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TypedDict, Unpack

import pytest

from saneless.vocabulary import (
    MULTI_PAGE_DISABLED_REASON,
    MULTI_PAGE_HELP,
    MULTI_PAGE_LABEL,
    MULTI_PAGE_NEEDS_TERMINAL,
    MULTI_PAGE_OPTION_HELP,
    NOTHING_TO_FINISH,
    CliChoice,
    PassAnswer,
    PassPrompt,
    PassPromptCopy,
    PassWait,
    abort_question,
    cli_choice_hint,
    cli_pass_choices,
    cli_pass_question,
    local_time,
    multi_page_manual_duplex_refusal,
    pass_prompt_copy,
)

_NOTHING_TO_FINISH_TEXT = (
    "Nothing to finish yet: every page so far was skipped as blank. "
    "Scan another page, or abort."
)
_FLIP_ABORT_QUESTION = "Abort this scan? It will stop and cannot be resumed."
_ABORT_NOTHING = "Abort scan stops without uploading anything."
# When an unanswered question gives up, as an aware instant; every expectation
# renders it through ``local_time`` so the tests hold in any ``TZ``.
_DEADLINE = datetime(2026, 9, 30, 19, 3, tzinfo=UTC)


class _PromptFields(TypedDict, total=False):
    """The prompt fields a test here sets beyond the wait and the pages kept."""

    timeout_seconds: float
    last_pass_pages: int
    pass_pages: int
    blank_positions: tuple[int, ...]
    error: str | None


def _prompt(
    wait: PassWait, *, pages_kept: int, **fields: Unpack[_PromptFields]
) -> PassPrompt:
    """
    Build a prompt offering what the worker would offer for this wait.

    The prompt is numbered 1, waits 10 minutes and follows a one-page pass
    unless ``fields`` says otherwise.

    Args:
        wait: The question that is open.
        pages_kept: How many pages the document holds.
        **fields: The other prompt fields the test cares about.

    Returns:
        The prompt.

    """
    match wait:
        case PassWait.NEXT_PASS:
            offered = {PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.ABORT}
            if pages_kept >= 1:
                offered.add(PassAnswer.FINISH)
        case PassWait.BLANK_DECISION:
            offered = {
                PassAnswer.RESCAN,
                PassAnswer.SKIP_BLANKS,
                PassAnswer.KEEP_BLANKS,
            }
        case PassWait.RETRY:
            offered = {PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.ABORT}
    base = PassPrompt(
        number=1,
        wait=wait,
        pages_kept=pages_kept,
        offered=frozenset(offered),
        timeout_seconds=600,
        last_pass_pages=1,
    )
    return dataclasses.replace(base, **fields)


class TestNextPassPrompt:
    """The prompt asking whether there is another page."""

    def test_several_pages_kept(self) -> None:
        """Four pages kept after a one-page pass read as the full prompt."""
        copy = pass_prompt_copy(_prompt(PassWait.NEXT_PASS, pages_kept=4))
        assert copy == PassPromptCopy(
            alert=None,
            detail=None,
            headline="4 pages kept so far.",
            instruction=(
                "Put the next page on the scanner, then press Scan next page. "
                "When there are no more pages, press Finish document."
            ),
            buttons=(
                (PassAnswer.NEXT, "Scan next page"),
                (PassAnswer.FINISH, "Finish document"),
                (PassAnswer.RESCAN, "Re-scan last page"),
                (PassAnswer.ABORT, "Abort scan"),
            ),
            finish_blocked=None,
            notes=(
                "Finish document uploads these 4 pages as one document. "
                "Re-scan throws away the last page and scans again. "
                "Abort scan stops without uploading anything.",
                "No answer within 10 minutes finishes the document with these 4 pages.",
            ),
            abort_question=(
                "Abort this scan? The 4 pages kept so far will not be uploaded, "
                "and the scan cannot be resumed."
            ),
        )

    def test_last_pass_of_several_pages(self) -> None:
        """A re-scan after a three-page pass names the three pages it discards."""
        copy = pass_prompt_copy(
            _prompt(PassWait.NEXT_PASS, pages_kept=7, last_pass_pages=3)
        )
        assert (PassAnswer.RESCAN, "Re-scan last 3 pages") in copy.buttons
        assert "throws away the last 3 pages" in copy.notes[0]
        assert copy.notes[0] == (
            "Finish document uploads these 7 pages as one document. "
            "Re-scan throws away the last 3 pages and scans again. "
            "Abort scan stops without uploading anything."
        )

    def test_one_page_kept(self) -> None:
        """One kept page reads as "this page", never as "these 1 pages"."""
        copy = pass_prompt_copy(_prompt(PassWait.NEXT_PASS, pages_kept=1))
        assert copy.headline == "1 page kept so far."
        assert copy.notes[0].startswith(
            "Finish document uploads this page as one document."
        )
        assert copy.notes[1] == (
            "No answer within 10 minutes finishes the document with this page."
        )
        assert copy.abort_question == (
            "Abort this scan? The 1 page kept so far will not be uploaded, "
            "and the scan cannot be resumed."
        )

    def test_no_pages_kept(self) -> None:
        """
        With nothing kept, Finish is still listed but says why it cannot be used.

        The web page disables the button and shows the reason as text beside
        it, so the button stays in the list and the reason is its own field.
        """
        copy = pass_prompt_copy(_prompt(PassWait.NEXT_PASS, pages_kept=0))
        assert copy.headline == "No pages kept yet."
        assert copy.instruction == (
            "Put the next page on the scanner, then press Scan next page."
        )
        assert [answer for answer, _ in copy.buttons] == [
            PassAnswer.NEXT,
            PassAnswer.FINISH,
            PassAnswer.RESCAN,
            PassAnswer.ABORT,
        ]
        assert copy.finish_blocked == NOTHING_TO_FINISH
        assert copy.notes == (
            "Re-scan throws away the last page and scans again. "
            "Abort scan stops without uploading anything.",
            "No answer within 10 minutes ends the scan without uploading anything.",
        )
        assert copy.abort_question == _FLIP_ABORT_QUESTION
        assert copy.alert is None
        assert copy.detail is None

    def test_nothing_to_finish_wording(self) -> None:
        """The reason Finish is withheld is one sentence, shared with the CLI."""
        assert NOTHING_TO_FINISH == _NOTHING_TO_FINISH_TEXT

    def test_timeout_is_named_in_its_own_unit(self) -> None:
        """A 90-second wait is named in seconds in the timeout note."""
        copy = pass_prompt_copy(
            _prompt(PassWait.NEXT_PASS, pages_kept=4, timeout_seconds=90)
        )
        assert copy.notes[1] == (
            "No answer within 90 seconds finishes the document with these 4 pages."
        )

    def test_deadline_replaces_the_duration(self) -> None:
        """A known deadline is named as a time in place of the duration."""
        prompt = _prompt(PassWait.NEXT_PASS, pages_kept=4, timeout_seconds=90)
        copy = pass_prompt_copy(prompt, deadline=_DEADLINE)
        assert copy.notes[1] == (
            f"No answer by {local_time(_DEADLINE)} finishes the document with "
            "these 4 pages."
        )
        assert copy.notes[0] == pass_prompt_copy(prompt).notes[0]

    def test_deadline_with_nothing_kept_ends_the_scan(self) -> None:
        """With no page kept, the deadline form still says nothing is uploaded."""
        copy = pass_prompt_copy(
            _prompt(PassWait.NEXT_PASS, pages_kept=0), deadline=_DEADLINE
        )
        assert copy.notes[1] == (
            f"No answer by {local_time(_DEADLINE)} ends the scan without "
            "uploading anything."
        )

    def test_no_deadline_is_the_duration_form(self) -> None:
        """An explicit None deadline is exactly the copy without one."""
        prompt = _prompt(PassWait.NEXT_PASS, pages_kept=4)
        assert pass_prompt_copy(prompt, deadline=None) == pass_prompt_copy(prompt)


class TestAbortQuestion:
    """The confirmation Abort asks before it stops the scan."""

    @pytest.mark.parametrize(
        ("pages_kept", "question"),
        [
            (0, _FLIP_ABORT_QUESTION),
            (
                1,
                "Abort this scan? The 1 page kept so far will not be uploaded, "
                "and the scan cannot be resumed.",
            ),
            (
                12,
                "Abort this scan? The 12 pages kept so far will not be uploaded, "
                "and the scan cannot be resumed.",
            ),
        ],
    )
    def test_abort_question(self, pages_kept: int, question: str) -> None:
        """The question names the pages lost, or reuses the flip's at zero."""
        assert abort_question(pages_kept) == question


class TestBlankDecisionPrompt:
    """The prompt asking what to do about pages that look blank."""

    def test_one_page_pass(self) -> None:
        """A one-page pass talks about "this page" and offers no Abort."""
        copy = pass_prompt_copy(
            _prompt(
                PassWait.BLANK_DECISION,
                pages_kept=3,
                pass_pages=1,
                blank_positions=(1,),
            )
        )
        assert copy == PassPromptCopy(
            alert=None,
            detail=None,
            headline="This page looks blank.",
            instruction=None,
            buttons=(
                (PassAnswer.SKIP_BLANKS, "Skip page"),
                (PassAnswer.KEEP_BLANKS, "Keep page"),
                (PassAnswer.RESCAN, "Re-scan page"),
            ),
            finish_blocked=None,
            notes=(
                "Skip page leaves it out of the document. Keep page adds it "
                "anyway. Re-scan page scans it again.",
                "3 pages already kept.",
                "No answer within 10 minutes skips it and finishes the document.",
            ),
            abort_question=None,
        )

    def test_several_blank_pages_in_a_longer_pass(self) -> None:
        """Three blanks among six scanned are listed by position and counted."""
        copy = pass_prompt_copy(
            _prompt(
                PassWait.BLANK_DECISION,
                pages_kept=0,
                pass_pages=6,
                blank_positions=(2, 4, 6),
            )
        )
        assert copy.headline == "Pages 2, 4, 6 of the 6 just scanned look blank."
        assert copy.instruction is None
        assert copy.buttons == (
            (PassAnswer.SKIP_BLANKS, "Skip blank pages"),
            (PassAnswer.KEEP_BLANKS, "Keep blank pages"),
            (PassAnswer.RESCAN, "Re-scan all 6"),
        )
        assert copy.notes == (
            "Skip leaves those 3 pages out of the document. Keep adds them "
            "anyway. Re-scan throws away all 6 and scans them again.",
            "No pages kept yet.",
            "No answer within 10 minutes skips the blank pages and finishes the "
            "document.",
        )
        assert copy.abort_question is None
        assert copy.finish_blocked is None

    def test_one_blank_page_in_a_longer_pass(self) -> None:
        """One blank among four scanned is "Page 3" and "that page"."""
        copy = pass_prompt_copy(
            _prompt(
                PassWait.BLANK_DECISION,
                pages_kept=1,
                pass_pages=4,
                blank_positions=(3,),
            )
        )
        assert copy.headline == "Page 3 of the 4 just scanned looks blank."
        assert copy.buttons[:2] == (
            (PassAnswer.SKIP_BLANKS, "Skip blank page"),
            (PassAnswer.KEEP_BLANKS, "Keep blank page"),
        )
        assert copy.notes[0] == (
            "Skip leaves that page out of the document. Keep adds it anyway. "
            "Re-scan throws away all 4 and scans them again."
        )
        assert copy.notes[1] == "1 page already kept."

    def test_timeout_with_nothing_to_finish(self) -> None:
        """When skipping would leave no page at all, the timeout ends the scan."""
        copy = pass_prompt_copy(
            _prompt(
                PassWait.BLANK_DECISION,
                pages_kept=0,
                pass_pages=1,
                blank_positions=(1,),
            )
        )
        assert copy.notes[-1] == (
            "No answer within 10 minutes ends the scan without uploading anything."
        )

    def test_timeout_is_named_in_its_own_unit(self) -> None:
        """A 90-second wait is named in seconds in the timeout note."""
        copy = pass_prompt_copy(
            _prompt(
                PassWait.BLANK_DECISION,
                pages_kept=2,
                pass_pages=1,
                blank_positions=(1,),
                timeout_seconds=90,
            )
        )
        assert copy.notes[-1] == (
            "No answer within 90 seconds skips it and finishes the document."
        )

    def test_deadline_replaces_the_duration(self) -> None:
        """A known deadline is named as a time in place of the duration."""
        prompt = _prompt(
            PassWait.BLANK_DECISION,
            pages_kept=2,
            pass_pages=1,
            blank_positions=(1,),
            timeout_seconds=90,
        )
        copy = pass_prompt_copy(prompt, deadline=_DEADLINE)
        assert copy.notes[-1] == (
            f"No answer by {local_time(_DEADLINE)} skips it and finishes the document."
        )
        assert copy.notes[:-1] == pass_prompt_copy(prompt).notes[:-1]

    def test_deadline_for_several_blank_pages(self) -> None:
        """The several-page wording keeps its own verb in the deadline form."""
        copy = pass_prompt_copy(
            _prompt(
                PassWait.BLANK_DECISION,
                pages_kept=0,
                pass_pages=6,
                blank_positions=(2, 4, 6),
            ),
            deadline=_DEADLINE,
        )
        assert copy.notes[-1] == (
            f"No answer by {local_time(_DEADLINE)} skips the blank pages and "
            "finishes the document."
        )


class TestRetryPrompt:
    """The prompt after a pass that failed."""

    def test_failed_pass(self) -> None:
        """The failure, the pages kept and the re-feed instruction are all named."""
        copy = pass_prompt_copy(
            _prompt(PassWait.RETRY, pages_kept=4, error="Document feeder jammed")
        )
        assert copy == PassPromptCopy(
            alert="The last scan failed, so none of its pages were added.",
            detail="The scanner reported: Document feeder jammed",
            headline="4 pages kept so far.",
            instruction=(
                "Clear the scanner, put back every page from the failed scan, "
                "then press Scan again."
            ),
            buttons=(
                (PassAnswer.NEXT, "Scan again"),
                (PassAnswer.FINISH, "Finish document"),
                (PassAnswer.ABORT, "Abort scan"),
            ),
            finish_blocked=None,
            notes=(
                f"Finish document uploads the 4 pages kept. {_ABORT_NOTHING}",
                "No answer within 10 minutes finishes the document with these 4 pages.",
            ),
            abort_question=abort_question(4),
        )

    def test_error_override_replaces_the_raw_text(self) -> None:
        """The detail line shows the scrubbed text it is given, not the raw error."""
        copy = pass_prompt_copy(
            _prompt(PassWait.RETRY, pages_kept=4, error="/home/op/raw"),
            error="scrubbed",
        )
        assert copy.detail == "The scanner reported: scrubbed"

    def test_timeout_is_named_in_its_own_unit(self) -> None:
        """A 90-second wait is named in seconds in the timeout note."""
        copy = pass_prompt_copy(
            _prompt(PassWait.RETRY, pages_kept=4, error="jam", timeout_seconds=90)
        )
        assert copy.notes[-1] == (
            "No answer within 90 seconds finishes the document with these 4 pages."
        )

    def test_deadline_replaces_the_duration(self) -> None:
        """A known deadline is named as a time in place of the duration."""
        prompt = _prompt(PassWait.RETRY, pages_kept=4, error="jam", timeout_seconds=90)
        copy = pass_prompt_copy(prompt, deadline=_DEADLINE)
        assert copy.notes[-1] == (
            f"No answer by {local_time(_DEADLINE)} finishes the document with "
            "these 4 pages."
        )
        assert copy.notes[0] == pass_prompt_copy(prompt).notes[0]


class TestFormAndOptionWording:
    """The checkbox on the scan form and the ``--multi-page`` option."""

    def test_checkbox_wording(self) -> None:
        """The checkbox's label, help line and manual-duplex reason read verbatim."""
        assert MULTI_PAGE_LABEL == "Multiple pages"
        assert MULTI_PAGE_HELP == (
            "Asks after each scan whether there is another page, and puts every "
            "page in one document."
        )
        assert MULTI_PAGE_DISABLED_REASON == "Not available with manual duplex."

    def test_option_help(self) -> None:
        """The option's help says it needs a terminal."""
        assert MULTI_PAGE_OPTION_HELP == (
            "Ask after each scan whether there is another page, and put every "
            "page in one document. Needs an interactive terminal."
        )

    def test_needs_a_terminal_refusal(self) -> None:
        """The refusal without a terminal says why and what to do instead."""
        assert MULTI_PAGE_NEEDS_TERMINAL == (
            "--multi-page needs an interactive terminal: saneless asks after each "
            "scan whether there is another page. Run it from a terminal, or scan "
            "from the web UI."
        )

    def test_manual_duplex_refusal(self) -> None:
        """The manual-duplex refusal names the profile."""
        assert multi_page_manual_duplex_refusal("duplex") == (
            "Profile 'duplex' is manual duplex, and --multi-page is not available "
            "with manual duplex. Scan without --multi-page, or choose another "
            "profile."
        )


class TestCliPrompts:
    """The single-letter prompts ``saneless scan --multi-page`` asks."""

    def test_next_pass_with_pages_kept(self) -> None:
        """Every letter is offered once a page is kept."""
        prompt = _prompt(PassWait.NEXT_PASS, pages_kept=4)
        assert cli_pass_question(prompt) == (
            "4 pages kept so far. [n]ext, [r]e-scan last, [f]inish, [a]bort"
        )
        assert [c.letter for c in cli_pass_choices(prompt)] == ["n", "r", "f", "a"]
        assert cli_choice_hint(prompt) == (
            "Choose one of: [n]ext, [r]e-scan last, [f]inish, [a]bort"
        )

    def test_next_pass_with_nothing_kept(self) -> None:
        """At zero pages kept, ``[f]inish`` is neither asked nor hinted."""
        prompt = _prompt(PassWait.NEXT_PASS, pages_kept=0)
        assert cli_pass_question(prompt) == (
            "No pages kept yet. [n]ext, [r]e-scan last, [a]bort"
        )
        assert [c.letter for c in cli_pass_choices(prompt)] == ["n", "r", "a"]
        assert cli_choice_hint(prompt) == (
            "Choose one of: [n]ext, [r]e-scan last, [a]bort"
        )

    def test_blank_one_page_pass(self) -> None:
        """A one-page blank pass asks skip, keep or re-scan."""
        prompt = _prompt(
            PassWait.BLANK_DECISION, pages_kept=2, pass_pages=1, blank_positions=(1,)
        )
        assert cli_pass_question(prompt) == (
            "This page looks blank. [s]kip, [k]eep, [r]e-scan"
        )
        assert [c.letter for c in cli_pass_choices(prompt)] == ["s", "k", "r"]

    def test_blank_multi_page_pass(self) -> None:
        """A longer pass lists the blank positions and re-scans the whole pass."""
        prompt = _prompt(
            PassWait.BLANK_DECISION,
            pages_kept=2,
            pass_pages=6,
            blank_positions=(2, 4),
        )
        assert cli_pass_question(prompt) == (
            "Pages 2, 4 of the 6 just scanned look blank. "
            "[s]kip, [k]eep, [r]e-scan all 6"
        )

    def test_failed_pass(self) -> None:
        """The failed-pass prompt is two lines: what failed, then what to do."""
        prompt = _prompt(PassWait.RETRY, pages_kept=4, error="jam")
        assert cli_pass_question(prompt) == (
            "The last scan failed, so none of its pages were added: jam\n"
            "4 pages kept so far. Put back every page from the failed scan. "
            "[n] scan again, [f]inish, [a]bort"
        )
        assert [c.letter for c in cli_pass_choices(prompt)] == ["n", "f", "a"]

    def test_letters_map_to_answers(self) -> None:
        """Every letter, in every prompt, means one and only one answer."""
        expected = {
            "n": PassAnswer.NEXT,
            "r": PassAnswer.RESCAN,
            "f": PassAnswer.FINISH,
            "a": PassAnswer.ABORT,
            "s": PassAnswer.SKIP_BLANKS,
            "k": PassAnswer.KEEP_BLANKS,
        }
        prompts = [
            _prompt(PassWait.NEXT_PASS, pages_kept=4),
            _prompt(
                PassWait.BLANK_DECISION,
                pages_kept=0,
                pass_pages=6,
                blank_positions=(2,),
            ),
            _prompt(PassWait.RETRY, pages_kept=1, error="jam"),
        ]
        seen: list[CliChoice] = [
            choice for prompt in prompts for choice in cli_pass_choices(prompt)
        ]
        assert {choice.letter for choice in seen} == set(expected)
        for choice in seen:
            assert choice.answer is expected[choice.letter]
            assert choice.label.startswith(f"[{choice.letter}]")
