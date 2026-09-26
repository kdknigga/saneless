"""
Tests for the saneless exception hierarchy and the describe() message helper.

Covers requirements: M-17 (PDF assembly failures get a saneless type of their
own), N-08 (a deliberate cancel at the flip prompt is not a failure) and D-08
(an exception whose text is empty is still described by something readable).

Also covers the all-blank failure's own type, the "interrupted, not cancelled"
signal exception, and ``failure_text``, which renders an exception together
with the notes ``add_note`` attached to it.
"""

from __future__ import annotations

import signal

import httpx2
import pytest

from saneless.exceptions import (
    AllPagesBlankError,
    PdfError,
    SanelessError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
    describe,
    describe_text,
    failure_text,
    note_text,
)


class TestHierarchy:
    """Placement of the Phase 28 exception types in the saneless hierarchy."""

    @pytest.mark.parametrize(
        "exc_type", [PdfError, ScanCancelledError, AllPagesBlankError]
    )
    def test_new_types_are_saneless_errors(self, exc_type: type[Exception]) -> None:
        """PdfError and ScanCancelledError are SanelessError subclasses (M-17, N-08)."""
        assert issubclass(exc_type, SanelessError)

    def test_scan_cancelled_error_is_not_a_scan_error(self) -> None:
        """
        A cancel is deliberately not a ScanError (N-08, D-01).

        Every ``except ScanError`` in the pipeline and the CLI treats what it
        catches as a scanner failure: exit 1 and ``JobState.ERROR``.  Keeping the
        cancel outside that branch is what stops an operator's deliberate stop
        from being reported as a broken scanner.
        """
        assert not issubclass(ScanCancelledError, ScanError)

    def test_pdf_error_is_not_a_scan_error(self) -> None:
        """A full disk during assembly is never recorded as a scanner failure (D-04)."""
        assert not issubclass(PdfError, ScanError)

    def test_all_pages_blank_error_is_not_a_scan_error(self) -> None:
        """
        Pages the detector judged blank are never blamed on the scanner (D-10).

        A sibling of ``ScanError``, like ``PdfError``, so no ``except ScanError``
        can absorb it and send the reader to check a scanner that worked.
        """
        assert not issubclass(AllPagesBlankError, ScanError)

    @pytest.mark.parametrize(
        "exc_type", [PdfError, ScanCancelledError, AllPagesBlankError]
    )
    def test_single_message_constructor(self, exc_type: type[Exception]) -> None:
        """
        Each new type builds from one message (Pitfall 9).

        The pipeline no longer rebuilds an exception to re-raise it -- its run
        guard adds a note to the original -- but every saneless type is still
        raised from a single message across the codebase, so a type that grew
        a required constructor argument would turn those raises into a
        TypeError.
        """
        rebuilt = exc_type("rebuilt")
        assert str(rebuilt) == "rebuilt"


class TestDescribe:
    """describe() one-line exception text tests."""

    def test_describe_returns_the_exception_text(self) -> None:
        """An exception with text is described by that text (D-08)."""
        assert describe(ValueError("boom")) == "boom"

    def test_describe_falls_back_to_the_class_name_for_empty_text(self) -> None:
        """An httpx2 timeout that stringifies empty is named by its class (D-08)."""
        assert describe(httpx2.ReadTimeout("")) == "ReadTimeout"

    def test_describe_falls_back_to_the_class_name_with_no_args(self) -> None:
        """An exception raised with no arguments is named by its class (D-08)."""
        assert describe(RuntimeError()) == "RuntimeError"

    def test_describe_collapses_a_multi_line_message_to_one_line(self) -> None:
        """
        A message with newlines is described on one line (EXC-02, WR-01).

        Exit 5 exists for text saneless does not control -- pydantic's
        ``ValidationError`` renders over several lines -- and every wrapped
        SANE, img2pdf, Pillow or OSError message reaches ``job.error`` and the
        CLI line through ``describe``.
        """
        assert describe(RuntimeError("kaboom\n  second\tline\n")) == (
            "kaboom second line"
        )

    def test_describe_falls_back_to_the_class_name_for_whitespace_text(self) -> None:
        """Text that is only whitespace is as empty as no text at all (D-08)."""
        assert describe(OSError(" \n ")) == "OSError"


class TestDescribeText:
    """
    describe_text() applies describe()'s rule to text with no exception object.

    A scanner listing runs in a child process, which reports a failure as a
    type name and a message rather than an exception, so the parent needs the
    same normalisation without an object to call ``str`` on.
    """

    def test_whitespace_is_collapsed_to_one_line(self) -> None:
        """Leading, trailing and internal whitespace runs become single spaces."""
        assert describe_text("  two\n  lines ", "RuntimeError") == "two lines"

    def test_empty_text_falls_back_to_the_type_name(self) -> None:
        """A child failure with no message is named by its type."""
        assert describe_text("", "RuntimeError") == "RuntimeError"

    def test_whitespace_text_falls_back_to_the_type_name(self) -> None:
        """Text that is only whitespace is as empty as no text at all."""
        assert describe_text(" \n\t", "error") == "error"

    def test_describe_still_collapses_through_the_shared_rule(self) -> None:
        """``describe`` gives the same answer it gave before the rule moved."""
        assert describe(RuntimeError("a\nb")) == "a b"
        assert describe(RuntimeError("a\nb")) == describe_text("a\nb", "RuntimeError")


class TestScanInterrupted:
    """ScanInterrupted: a signal or a server stop, which is not a cancel (D-12)."""

    def test_it_is_a_base_exception_and_not_an_exception(self) -> None:
        """
        No ``except Exception`` can re-type it.

        A signal can land inside a backend ``except Exception: raise
        ScanError(...) from exc``; an ``Exception`` subclass would come out of
        that as a scanner failure.
        """
        exc = ScanInterrupted("Interrupted by SIGTERM", signum=signal.SIGTERM)
        assert isinstance(exc, BaseException)
        assert not isinstance(exc, Exception)

    def test_it_is_not_a_keyboard_interrupt(self) -> None:
        """Every "Ctrl-C means cancel" branch keeps treating it as something else."""
        assert not issubclass(ScanInterrupted, KeyboardInterrupt)

    def test_it_carries_the_signal_number(self) -> None:
        """The CLI turns the signal number into 128 + signum (D-13)."""
        exc = ScanInterrupted("Interrupted by SIGTERM", signum=signal.SIGTERM)
        assert exc.signum == signal.SIGTERM
        assert str(exc) == "Interrupted by SIGTERM"

    def test_a_server_stop_carries_no_signal_number(self) -> None:
        """The server stopping is an interruption with no signal behind it."""
        assert ScanInterrupted("The server is stopping").signum is None


class TestFailureText:
    """failure_text() renders an exception with every note on one line."""

    def test_without_notes_it_is_describe(self) -> None:
        """An exception with no notes renders exactly as ``describe`` does."""
        exc = ScanError("jam")
        assert failure_text(exc) == describe(exc)

    def test_a_note_follows_the_message_as_a_sentence(self) -> None:
        """
        The note is the part ``str(exc)`` drops, so it has to be rendered here.

        A preserved scan's "where the pages went" arrives as a note, and a
        surface that printed ``str(exc)`` would silently leave it out.
        """
        exc = ScanError("jam")
        exc.add_note("The 3 page(s) were preserved at /d/x.pdf")
        assert failure_text(exc) == "jam. The 3 page(s) were preserved at /d/x.pdf"

    @pytest.mark.parametrize("ending", [".", "!", "?"])
    def test_a_message_that_already_ends_a_sentence_gets_one_space(
        self, ending: str
    ) -> None:
        """No doubled full stop when the message already ends a sentence."""
        exc = ScanError(f"jam{ending}")
        exc.add_note("Kept.")
        assert failure_text(exc) == f"jam{ending} Kept."

    def test_a_multi_line_note_is_collapsed_to_one_line(self) -> None:
        """A note cannot add a line to the CLI's one-line report or to job.error."""
        exc = ScanError("jam")
        exc.add_note("kept\n  at\t/d/x.pdf\n")
        assert failure_text(exc) == "jam. kept at /d/x.pdf"

    def test_two_notes_are_joined_by_one_space(self) -> None:
        """Every note is rendered, in the order it was added."""
        exc = ScanError("jam")
        exc.add_note("First.")
        exc.add_note("Second.")
        assert failure_text(exc) == "jam. First. Second."

    def test_an_empty_message_falls_back_to_the_class_name(self) -> None:
        """The message part keeps ``describe``'s never-empty fallback."""
        exc = RuntimeError()
        exc.add_note("Kept.")
        assert failure_text(exc) == "RuntimeError. Kept."

    def test_note_text_of_an_exception_with_no_notes_is_empty(self) -> None:
        """No notes, no text: callers append ``note_text`` only when it is set."""
        assert note_text(ScanError("jam")) == ""

    def test_note_text_joins_and_collapses_the_notes(self) -> None:
        """note_text is the notes alone, one line, one space between them."""
        exc = ScanError("jam")
        exc.add_note("First\nline.")
        exc.add_note("Second.")
        assert note_text(exc) == "First line. Second."
