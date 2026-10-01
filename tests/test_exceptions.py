"""
Tests for the saneless exception hierarchy and the describe() message helper.

Covers requirements: M-17 (PDF assembly failures get a saneless type of their
own), N-08 (a deliberate cancel at the flip prompt is not a failure) and D-08
(an exception whose text is empty is still described by something readable).

Also covers the all-blank failure's own type, the "interrupted, not cancelled"
signal exception, and ``failure_text``, which renders an exception together
with the notes ``add_note`` attached to it.  The delivery-uncertainty, API
version, disk-space and no-scanner types, and ``is_out_of_space``, which finds
a full disk anywhere in an exception's cause chain, are covered too, as is
the ``next_step`` a raise site can attach to any saneless error.
"""

from __future__ import annotations

import errno
import signal

import httpx2
import pytest

from saneless import exceptions as exceptions_module
from saneless.exceptions import (
    AllPagesBlankError,
    ConfigError,
    DiskSpaceError,
    NoScannerFoundError,
    PaperlessError,
    PaperlessIncompatibleError,
    PaperlessTimeoutError,
    PaperlessUncertainSendError,
    PaperlessUnconfirmedError,
    PdfError,
    SanelessError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
    describe,
    describe_text,
    failure_text,
    is_out_of_space,
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


class TestDeliveryAndDiskTypes:
    """Placement of the delivery-uncertainty, version and disk-space types."""

    @pytest.mark.parametrize(
        "exc_type",
        [
            PaperlessUncertainSendError,
            PaperlessUnconfirmedError,
            PaperlessIncompatibleError,
        ],
    )
    def test_narrower_paperless_types_are_paperless_errors(
        self, exc_type: type[Exception]
    ) -> None:
        """Every ``except PaperlessError`` still catches the narrower types."""
        assert issubclass(exc_type, PaperlessError)

    def test_poll_timeout_is_an_unconfirmed_filing(self) -> None:
        """
        A poll that ran out after acceptance is an unconfirmed filing.

        paperless-ngx held a task id, so it had the document; the timeout
        only means it never confirmed filing it.
        """
        assert issubclass(PaperlessTimeoutError, PaperlessUnconfirmedError)

    def test_uncertain_send_is_not_an_unconfirmed_filing(self) -> None:
        """A send with no answer is weaker than a received upload: kept apart."""
        assert not issubclass(PaperlessUncertainSendError, PaperlessUnconfirmedError)
        assert not issubclass(PaperlessUnconfirmedError, PaperlessUncertainSendError)

    def test_disk_space_error_is_neither_a_scan_nor_a_pdf_error(self) -> None:
        """
        A full disk is never blamed on the scanner or on the PDF writer.

        A sibling of ``ScanError`` and ``PdfError``, like
        ``AllPagesBlankError``, so no ``except ScanError`` can absorb it.
        """
        assert issubclass(DiskSpaceError, SanelessError)
        assert not issubclass(DiskSpaceError, ScanError)
        assert not issubclass(DiskSpaceError, PdfError)

    def test_no_scanner_found_error_is_a_scan_error_not_a_config_error(
        self,
    ) -> None:
        """Finding no scanner is a scanner condition, not a load-time problem."""
        assert issubclass(NoScannerFoundError, ScanError)
        assert not issubclass(NoScannerFoundError, ConfigError)

    @pytest.mark.parametrize(
        "exc_type",
        [
            PaperlessUncertainSendError,
            PaperlessUnconfirmedError,
            PaperlessIncompatibleError,
            DiskSpaceError,
            NoScannerFoundError,
        ],
    )
    def test_single_message_constructor(self, exc_type: type[Exception]) -> None:
        """Each type builds from one message, like every other saneless type."""
        assert str(exc_type("rebuilt")) == "rebuilt"


def _saneless_error_types() -> list[type[SanelessError]]:
    """Return every saneless error type the exceptions module exports."""
    return sorted(
        (
            exported
            for exported in vars(exceptions_module).values()
            if isinstance(exported, type) and issubclass(exported, SanelessError)
        ),
        key=lambda exported: exported.__name__,
    )


class TestNextStep:
    """
    A saneless error can carry the fix its raise site knows.

    The category's advice is a fallback that must be true for every error in
    the category, so it is generic.  A raise site that knows the specific fix
    attaches it as ``next_step``, and the CLI prints that instead.
    """

    def test_no_next_step_by_default(self) -> None:
        """An error raised without one has none, so the fallback is used."""
        assert ConfigError("x").next_step is None

    def test_a_next_step_is_kept_and_stays_out_of_the_message(self) -> None:
        """The fix is its own field; the one-line message is unchanged."""
        exc = ConfigError("x", next_step="Do y.")
        assert exc.next_step == "Do y."
        assert str(exc) == "x"
        assert exc.args == ("x",)
        assert describe(exc) == "x"

    def test_next_step_is_keyword_only(self) -> None:
        """A second positional argument stays an argument, never the fix."""
        exc = ScanError("a", "b")
        assert exc.args == ("a", "b")
        assert exc.next_step is None

    @pytest.mark.parametrize("exc_type", _saneless_error_types())
    def test_every_saneless_error_accepts_a_next_step(
        self, exc_type: type[SanelessError]
    ) -> None:
        """Every type takes the keyword, so any raise site can attach a fix."""
        exc = exc_type("message", next_step="Do this.")
        assert exc.next_step == "Do this."
        assert str(exc) == "message"
        assert exc_type("message").next_step is None

    def test_an_interrupt_is_not_a_saneless_error_and_is_unchanged(self) -> None:
        """
        ScanInterrupted keeps its own constructor.

        It is a ``BaseException`` outside the saneless hierarchy: an
        interruption is reported by the signal that caused it, not with a
        category's advice, so it takes no next step.
        """
        exc = ScanInterrupted("m", signum=signal.SIGTERM)
        assert not isinstance(exc, SanelessError)
        assert exc.signum == signal.SIGTERM
        assert str(exc) == "m"


class TestIsOutOfSpace:
    """is_out_of_space() finds a full disk anywhere in the cause chain."""

    @pytest.mark.parametrize("code", [errno.ENOSPC, errno.EDQUOT])
    def test_a_full_disk_or_quota_is_out_of_space(self, code: int) -> None:
        """No space left on the device, or a quota reached, is out of space."""
        assert is_out_of_space(OSError(code, "x"))

    def test_another_os_error_is_not_out_of_space(self) -> None:
        """A permission error is a setup problem, not a full disk."""
        assert not is_out_of_space(OSError(errno.EACCES, "x"))

    def test_an_os_error_with_no_errno_is_not_out_of_space(self) -> None:
        """An OSError built from a message alone carries no errno."""
        assert not is_out_of_space(OSError("x"))

    def test_a_non_os_error_is_not_out_of_space(self) -> None:
        """Only an OSError can report a full disk."""
        assert not is_out_of_space(RuntimeError("no space left"))

    def test_an_explicit_cause_is_followed(self) -> None:
        """A wrapper raised ``from`` an ENOSPC is out of space."""
        wrapper = RuntimeError("wrapped")
        wrapper.__cause__ = OSError(errno.ENOSPC, "x")
        assert is_out_of_space(wrapper)

    def test_an_implicit_context_is_followed(self) -> None:
        """A wrapper raised while handling an ENOSPC is out of space."""
        wrapper = RuntimeError("wrapped")
        wrapper.__context__ = OSError(errno.ENOSPC, "x")
        assert is_out_of_space(wrapper)

    def test_a_deep_chain_is_followed(self) -> None:
        """The full disk may sit several links down."""
        inner = ValueError("middle")
        inner.__cause__ = OSError(errno.EDQUOT, "x")
        outer = RuntimeError("outer")
        outer.__cause__ = inner
        assert is_out_of_space(outer)

    def test_a_chain_without_a_full_disk_is_not_out_of_space(self) -> None:
        """A chain of unrelated errors is not out of space."""
        wrapper = RuntimeError("wrapped")
        wrapper.__cause__ = OSError(errno.EACCES, "x")
        assert not is_out_of_space(wrapper)

    def test_a_self_referencing_chain_ends(self) -> None:
        """A cycle in the cause chain is walked once and ends."""
        first = RuntimeError("first")
        second = ValueError("second")
        first.__cause__ = second
        second.__cause__ = first
        assert not is_out_of_space(first)

    def test_an_exception_that_is_its_own_cause_ends(self) -> None:
        """An exception whose cause is itself does not loop."""
        looped = RuntimeError("looped")
        looped.__context__ = looped
        assert not is_out_of_space(looped)


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
