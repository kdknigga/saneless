"""
Custom exception hierarchy for saneless.

All saneless-specific exceptions inherit from SanelessError,
allowing callers to catch broad or narrow exception types.
"""

__all__ = [
    "AllPagesBlankError",
    "ConfigError",
    "FeederEmptyError",
    "ListingCrashedError",
    "ListingNoAnswerError",
    "ListingTimedOutError",
    "PaperlessError",
    "PaperlessTimeoutError",
    "PdfError",
    "SanelessError",
    "ScanCancelledError",
    "ScanError",
    "ScanInterrupted",
    "StorageError",
    "describe",
    "describe_text",
    "failure_text",
    "note_text",
]

# What ends a sentence already, so failure_text joins a note with one space
# rather than adding a second full stop.
_SENTENCE_ENDINGS = (".", "!", "?")


class SanelessError(Exception):
    """Base exception for all saneless errors."""


class ConfigError(SanelessError):
    """Configuration loading or validation failure."""


class ScanError(SanelessError):
    """Scanner operation failure."""


class FeederEmptyError(ScanError):
    """ADF feeder is empty -- no paper detected."""


class ListingCrashedError(ScanError):
    """The scanner library died from a signal while listing scanners."""


class ListingNoAnswerError(ScanError):
    """
    The listing process gave no answer that could be read, or never started.

    It exited without a reply, its reply did not fit the schema, or it could
    not be started at all.  Like a crash or a timeout, this says the listing
    could not see, not that there was nothing to see.  A listing that ran
    and in which the scanner library itself raised is a plain ``ScanError``.
    """


class ListingTimedOutError(ScanError):
    """
    The scanner library did not finish listing scanners before the deadline.

    The listing was stopped: the process running it was killed and reaped
    before this was raised.
    """


class ScanCancelledError(SanelessError):
    """
    The operator deliberately stopped the scan at the flip prompt.

    A cancel is not a failure.  This is deliberately not a ``ScanError``, so no
    ``except ScanError`` anywhere can absorb it and report the operator's
    decision as a broken scanner.
    """


class PdfError(SanelessError):
    """
    The scanned pages could not be assembled into a PDF.

    A sibling of ``ScanError`` rather than a subclass, so a full disk or an
    image the PDF writer rejects is never recorded as a scanner failure.
    """


class AllPagesBlankError(SanelessError):
    """
    Empty-page detection judged every scanned page blank.

    The scanner worked: it returned pages, and the detector removed all of
    them.  A sibling of ``ScanError`` rather than a subclass, like
    ``PdfError``, so no ``except ScanError`` can absorb it and blame the
    scanner for pages the detector judged blank.
    """


class ScanInterrupted(BaseException):
    """
    A scan was interrupted by something other than the operator's decision.

    A SIGTERM or SIGHUP to a one-shot command, or the server stopping (then
    ``signum`` is ``None``), ends the scan without anyone choosing to discard
    it, so the pages already scanned are kept.  That is what separates it
    from a cancel, which keeps nothing.

    It is a ``BaseException`` because a signal can land inside a backend
    ``except Exception: raise ScanError(...) from exc``, which would re-type
    an ``Exception`` into a scanner failure.  It is deliberately not a
    ``KeyboardInterrupt`` subclass: that would send it down every "Ctrl-C
    means cancel" branch.

    Attributes:
        signum: The signal that interrupted the command, or ``None`` when the
            server is stopping.

    """

    def __init__(self, message: str, *, signum: int | None = None) -> None:
        """
        Record the message and the signal that caused the interruption.

        Args:
            message: What interrupted the scan, for the one line reporting it.
            signum: The signal number, or ``None`` for a server stop.

        """
        super().__init__(message)
        self.signum = signum


class PaperlessError(SanelessError):
    """Paperless-ngx API operation failure."""


class PaperlessTimeoutError(PaperlessError):
    """Paperless-ngx did not resolve a consume task before the deadline."""


class StorageError(SanelessError):
    """
    Job store schema or persistence failure.

    The job database cannot be used: the file cannot be opened or read as
    SQLite, or its jobs table is a shape this build does not recognise.  Every
    message names the job database path.  The CLI reports it as a setup
    problem with exit 2, not as an unexpected error.
    """


def describe(exc: BaseException) -> str:
    """
    Return a one-line description of an exception that is never empty.

    Some third-party exceptions stringify to an empty string -- an
    ``httpx2.ReadTimeout`` raised without a message is one -- and a user-visible
    line reading "Upload failed: " says nothing.  Falling back to the class
    name keeps the line readable.

    Others stringify over several lines -- pydantic's ``ValidationError`` and
    httpx2's ``HTTPStatusError`` do -- so the whitespace is collapsed here, once,
    and every boundary that wraps a message through ``describe`` keeps the CLI
    line and ``job.error`` to one line.

    Args:
        exc: The exception to describe.

    Returns:
        ``str(exc)`` with every run of whitespace collapsed to one space, or
        the exception's class name when that leaves nothing.

    """
    return describe_text(str(exc), type(exc).__name__)


def describe_text(message: str, type_name: str) -> str:
    """
    Apply ``describe``'s rule to a message that has no exception object.

    A scanner listing runs in a separate process, which reports a failure as
    its exception's type name and message rather than as an exception.  This
    is the one place the rule lives, so that text and an exception object are
    normalised the same way.

    Args:
        message: The failure's message, possibly empty or over several lines.
        type_name: The failure's type name, used when the message is empty.

    Returns:
        ``message`` with every run of whitespace collapsed to one space, or
        ``type_name`` when that leaves nothing.

    """
    return " ".join(message.split()) or type_name


def note_text(exc: BaseException) -> str:
    """
    Return every note attached to an exception, on one line.

    ``add_note`` stores notes in ``__notes__``, which ``str(exc)`` ignores.
    Each note's whitespace is collapsed as ``describe`` collapses a message,
    so a note can never add a line to the CLI's report or to ``job.error``.

    Args:
        exc: The exception whose notes to render.

    Returns:
        The notes in the order they were added, joined by one space, or an
        empty string when there are none.  A note that is only whitespace is
        left out.

    """
    collapsed = (" ".join(str(note).split()) for note in getattr(exc, "__notes__", ()))
    return " ".join(note for note in collapsed if note)


def failure_text(exc: BaseException) -> str:
    """
    Return the one line a failure surface reports an exception as.

    ``str(exc)`` ignores the notes ``add_note`` attached, and a note is how a
    preserved scan says where its pages went, so this is the one way a failure
    surface -- the CLI's stderr line, the log and ``job.error`` -- renders an
    exception.

    Args:
        exc: The exception to render.

    Returns:
        ``describe(exc)``, followed by ``note_text(exc)`` when there are
        notes: separated by ``". "``, or by one space when the message already
        ends a sentence.

    """
    message = describe(exc)
    notes = note_text(exc)
    if not notes:
        return message
    separator = " " if message.endswith(_SENTENCE_ENDINGS) else ". "
    return f"{message}{separator}{notes}"
