"""
Custom exception hierarchy for saneless.

All saneless-specific exceptions inherit from SanelessError,
allowing callers to catch broad or narrow exception types.
"""

import errno

__all__ = [
    "AllPagesBlankError",
    "ConfigError",
    "DiskSpaceError",
    "FeederEmptyError",
    "ListingAbortedError",
    "ListingCrashedError",
    "ListingNoAnswerError",
    "ListingTimedOutError",
    "NoScannerFoundError",
    "PaperlessError",
    "PaperlessIncompatibleError",
    "PaperlessTimeoutError",
    "PaperlessTrustStoreError",
    "PaperlessUncertainSendError",
    "PaperlessUnconfirmedError",
    "PdfError",
    "SanelessError",
    "ScanCancelledError",
    "ScanError",
    "ScanInterrupted",
    "SpoolError",
    "StorageError",
    "describe",
    "describe_text",
    "failure_text",
    "is_out_of_space",
    "note_text",
]

# What ends a sentence already, so failure_text joins a note with one space
# rather than adding a second full stop.
_SENTENCE_ENDINGS = (".", "!", "?")

# The errno values that mean the filesystem has no room for a write: the
# device is full, or the writer's disk quota is used up.
_OUT_OF_SPACE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT})


class SanelessError(Exception):
    """
    Base exception for all saneless errors.

    Every saneless error is filed under a category whose advice is a
    fallback: it has to be true for every error in the category, so it says
    nothing specific.  A raise site that knows the fix for its own failure
    passes it as ``next_step``, and the CLI prints that instead of the
    category's fallback.  It is kept apart from the message, so ``str(exc)``,
    the log and ``job.error`` are unchanged by it.

    Attributes:
        next_step: The one sentence telling the operator what to do, or
            ``None`` when only the category's fallback applies.

    """

    def __init__(self, *args: object, next_step: str | None = None) -> None:
        """
        Record the exception's arguments and the fix its raise site knows.

        Args:
            *args: The exception's arguments, as for ``Exception``; the first
                is its message.
            next_step: What the operator should do about this failure, or
                ``None`` to use the category's fallback.  Keyword-only, so a
                second positional argument is never taken for it.

        """
        super().__init__(*args)
        self.next_step = next_step


class ConfigError(SanelessError):
    """Configuration loading or validation failure."""


class ScanError(SanelessError):
    """Scanner operation failure."""


class FeederEmptyError(ScanError):
    """ADF feeder is empty -- no paper detected."""


class NoScannerFoundError(ScanError):
    """
    No scanner could be found to scan with.

    A scanner condition, not a configuration one: the scanner is switched
    off, unplugged or unreachable, which the operator fixes at the scanner,
    and the same settings work once it answers.  ``ConfigError`` is kept for
    problems found when the configuration is loaded.  A ``ScanError``
    subclass, so ``classify_error`` files it as ``ErrorCategory.SCANNER``.
    """


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


class ListingAbortedError(ScanError):
    """
    A listing was stopped part way because saneless is stopping.

    Not a fault of the scanner or of the scanner library: the caller asked
    for the listing to end, and the process running it was killed and reaped
    before this was raised.  The listing saw nothing, so what it would have
    found is unknown, not absent.
    """


class SpoolError(ScanError):
    """
    The spool could not write a page, or could not measure its free space.

    A write error other than a full disk, or a spool directory whose free
    space cannot be read -- one removed, or whose mount went away.  A full
    disk is not this: a shortfall, or a write that runs out of space or
    quota, is a ``DiskSpaceError``.

    A ``ScanError`` subclass, so ``classify_error`` still files it as
    ``ErrorCategory.SCANNER`` and every exit code and message stays what it
    was.  It exists so a caller that must treat a failing disk differently
    from a device fault can test for it exactly: a jammed feeder is worth
    trying again, a disk that cannot be written is not.
    """


class DiskSpaceError(SanelessError):
    """
    The server ran out of disk space for a scan.

    Raised wherever a scan needs room it cannot get: the free-space check
    before scanning, the spool writing a page, the working directory, and
    assembling the PDF.  A sibling of ``ScanError`` and ``PdfError`` rather
    than a subclass of either, like ``AllPagesBlankError``, so no ``except
    ScanError`` can absorb it and blame the scanner, and it is never reported
    as a PDF the writer refused.  ``classify_error`` files it as
    ``ErrorCategory.DISK_SPACE``.  Its message names the folder and how much
    space is needed; the advice beside it names neither.
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


class PaperlessUncertainSendError(PaperlessError):
    """
    The upload was sent, but no usable answer came back.

    The request body was written before the connection failed or the reply
    was unusable -- a read timeout, a dropped connection, a proxy's 502 or
    504 -- so paperless-ngx may hold the document.  Sending it again, or
    saving it to the consume folder, could store it twice, so neither is
    done.  ``classify_error`` files it as ``ErrorCategory.UNCONFIRMED_SEND``,
    whose advice says to check paperless-ngx's document list first.
    """


class PaperlessUnconfirmedError(PaperlessError):
    """
    paperless-ngx accepted the upload but did not confirm filing it.

    A task id is held, so paperless-ngx received the document; the task then
    failed for a reason other than a duplicate, or never finished while
    saneless waited.  A stronger statement than
    ``PaperlessUncertainSendError``'s, so it is a class of its own.
    ``classify_error`` files it as ``ErrorCategory.UNCONFIRMED_FILING``, whose
    advice says to check paperless-ngx's document list before scanning again.
    """


class PaperlessTimeoutError(PaperlessUnconfirmedError):
    """
    Paperless-ngx did not resolve a consume task before the deadline.

    The upload was accepted -- the task being polled is its answer -- so a
    deadline that passes means only that filing was never confirmed: a long
    OCR can outlast it.  That is why this subclasses
    ``PaperlessUnconfirmedError`` and files as
    ``ErrorCategory.UNCONFIRMED_FILING`` rather than as a failed upload.
    """


class PaperlessIncompatibleError(PaperlessError):
    """
    paperless-ngx refused every API version saneless speaks.

    It answered 406 Not Acceptable, or advertised versions saneless does not
    support.  The token and the network are fine, and a refused request
    stores nothing, so ``classify_error`` files it as
    ``ErrorCategory.PAPERLESS_VERSION``, whose advice is to upgrade
    paperless-ngx rather than to check the token.
    """


class PaperlessTrustStoreError(PaperlessError):
    """
    The TLS trust store named by SSL_CERT_FILE or SSL_CERT_DIR could not be read.

    The trust anchors are read while the paperless-ngx client is built, so
    this is raised before any request is sent: the address, the token and
    paperless-ngx itself are untested, and nothing suggests any of them is
    wrong.  It stays a ``PaperlessError``, so ``classify_error`` files it as
    ``ErrorCategory.UPLOAD`` and ``serve`` exits 3 for it as before, and it
    carries a ``next_step`` naming the two variables.
    """


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


def is_out_of_space(exc: BaseException) -> bool:
    """
    Report whether an exception, or anything that caused it, is a full disk.

    A full disk often surfaces wrapped: a library catches the ``OSError`` and
    raises its own error ``from`` it, or raises while handling it.  This walks
    the exception, then its ``__cause__``, or its ``__context__`` when there is
    no cause, and stops at the first link seen twice, so a cycle ends.

    Args:
        exc: The exception to inspect.

    Returns:
        ``True`` when any link is an ``OSError`` whose ``errno`` is
        ``ENOSPC`` (no space left on the device) or ``EDQUOT`` (the disk quota
        is used up), otherwise ``False``.

    """
    seen: set[int] = set()
    link: BaseException | None = exc
    while link is not None and id(link) not in seen:
        seen.add(id(link))
        if isinstance(link, OSError) and link.errno in _OUT_OF_SPACE_ERRNOS:
            return True
        link = link.__cause__ if link.__cause__ is not None else link.__context__
    return False
