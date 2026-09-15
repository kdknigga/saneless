"""
Tests for the saneless exception hierarchy and the describe() message helper.

Covers requirements: M-17 (PDF assembly failures get a saneless type of their
own), N-08 (a deliberate cancel at the flip prompt is not a failure) and D-08
(an exception whose text is empty is still described by something readable).
"""

from __future__ import annotations

import httpx
import pytest

from saneless.exceptions import (
    PdfError,
    SanelessError,
    ScanCancelledError,
    ScanError,
    describe,
)


class TestHierarchy:
    """Placement of the Phase 28 exception types in the saneless hierarchy."""

    @pytest.mark.parametrize("exc_type", [PdfError, ScanCancelledError])
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

    @pytest.mark.parametrize("exc_type", [PdfError, ScanCancelledError])
    def test_single_message_constructor(self, exc_type: type[Exception]) -> None:
        """
        Each new type rebuilds from one message (Pitfall 9).

        ``pipeline._preserving`` re-raises ``type(exc)(message)``, so a type
        that grew a required constructor argument would turn a clean re-raise
        into a TypeError.
        """
        rebuilt = exc_type("rebuilt")
        assert str(rebuilt) == "rebuilt"


class TestDescribe:
    """describe() one-line exception text tests."""

    def test_describe_returns_the_exception_text(self) -> None:
        """An exception with text is described by that text (D-08)."""
        assert describe(ValueError("boom")) == "boom"

    def test_describe_falls_back_to_the_class_name_for_empty_text(self) -> None:
        """An httpx timeout that stringifies empty is named by its class (D-08)."""
        assert describe(httpx.ReadTimeout("")) == "ReadTimeout"

    def test_describe_falls_back_to_the_class_name_with_no_args(self) -> None:
        """An exception raised with no arguments is named by its class (D-08)."""
        assert describe(RuntimeError()) == "RuntimeError"
