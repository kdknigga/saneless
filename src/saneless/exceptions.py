"""
Custom exception hierarchy for saneless.

All saneless-specific exceptions inherit from SanelessError,
allowing callers to catch broad or narrow exception types.
"""

__all__ = [
    "ConfigError",
    "FeederEmptyError",
    "PaperlessError",
    "PaperlessTimeoutError",
    "PdfError",
    "SanelessError",
    "ScanCancelledError",
    "ScanError",
    "StorageError",
    "describe",
]


class SanelessError(Exception):
    """Base exception for all saneless errors."""


class ConfigError(SanelessError):
    """Configuration loading or validation failure."""


class ScanError(SanelessError):
    """Scanner operation failure."""


class FeederEmptyError(ScanError):
    """ADF feeder is empty -- no paper detected."""


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
    ``httpx.ReadTimeout`` raised without a message is one -- and a user-visible
    line reading "Upload failed: " says nothing.  Falling back to the class
    name keeps the line readable.

    Others stringify over several lines -- pydantic's ``ValidationError`` and
    httpx's ``HTTPStatusError`` do -- so the whitespace is collapsed here, once,
    and every boundary that wraps a message through ``describe`` keeps the CLI
    line and ``job.error`` to one line.

    Args:
        exc: The exception to describe.

    Returns:
        ``str(exc)`` with every run of whitespace collapsed to one space, or
        the exception's class name when that leaves nothing.

    """
    return " ".join(str(exc).split()) or type(exc).__name__
