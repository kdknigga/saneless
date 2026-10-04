"""
What one browser may see of one job: the only job shape templates receive.

Any device on the LAN can load the web page, and a job carries more than it
should show to all of them: the document's title, a picture of its first
page, and error and warning text that names host paths, the kept PDF (whose
file name carries the title) and the paperless-ngx address.  Templates are
therefore handed a :class:`JobView`, never a :class:`~saneless.job.Job`, and
:func:`build_job_view` decides once, on the server, what goes into it.

The browser that submitted the job -- the one presenting the owner token the
job recorded -- sees its title and thumbnail, and its error and warning text
with every host path and web address replaced by the name of the setting that
holds it, or by ``<path>`` when no setting does.  Every other browser sees that a scan happened and how it ended:
profile, state, time, error category and page counts, a generic title, no
thumbnail, and fixed sentences from ``vocabulary.py`` in place of the stored
text.  The gated fields are absent from such a view, not hidden with CSS, so
no template can render what the view does not carry.

Nothing is lost: the full text, paths and address included, stays in the log
and in ``saneless jobs``.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    HIDDEN_ERROR_DETAIL,
    HIDDEN_JOB_TITLE,
    HIDDEN_PRESERVED_ERROR,
    HIDDEN_WARNING_LINE,
)

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from saneless.config import Settings
    from saneless.job import Job
    from saneless.vocabulary import ErrorCategory, JobState

__all__ = ["JobView", "build_job_view", "owns_detail", "scrub_for_owner"]

# Any web address.  Stored text names paperless-ngx by its configured URL, and
# a URL from an earlier configuration, or from a library's own message, would
# slip past a match on the current one; the owner never needs the address, so
# every one is replaced.  The scheme is matched in any case: it is
# case-insensitive, and ``paperless.url`` is kept as it was typed, so
# ``HTTPS://`` is a working setting that reaches the stored text as written.
_URL = re.compile(r"https?://\S+", re.IGNORECASE)

# Punctuation that ends the sentence around a URL or a path rather than the
# URL or path itself, and the quotes and brackets that close around one.
# ``\S+`` swallows them, and they are put back after the replacement so
# "Paperless at <url>: refused" keeps its colon and "see '<url>' now" its
# closing quote.
_URL_TRAILING = ").,:;'\"]>"

# A host path counts only where it starts and ends as a whole path, so the
# ``tmp`` directory does not also match ``tmp2`` beside it.  The lookahead
# still lets a path end a sentence: a full stop is a boundary unless a name
# character follows it.
_PATH_BEFORE = r"(?<![\w.-])"
_PATH_AFTER = r"(?![\w-]|\.\w)"

# Any absolute path still in the text once the configured directories and web
# addresses are named: a device node, a path under an earlier configuration,
# one a library put in its own message.  It starts at a "/" that does not
# directly follow a name character, dot, "<", ">" or "-", so "failed/x.pdf",
# "<output.tmp_dir>/x", "3/4" and "and/or" are left alone; the punctuation
# after it is kept, as for a web address.
_OTHER_PATH = re.compile(r"(?<![\w.<>-])/[^\s'\"<>]+")

# What the pipeline writes directly before the path of something it kept in
# the failed folder.  Every sentence ``preservation.KeptGroup`` composes puts
# it before its first path: "The scan was preserved at <path>", "The 3 scanned
# page(s) were preserved at <path>", "2 of the 5 spooled page file(s) were
# preserved at <path>, <path>".  A preservation that kept nothing says "could
# NOT be preserved to" instead, so the phrase never appears in its message.
_KEPT_BEFORE = ("preserved at ",)


@dataclass(frozen=True, slots=True)
class JobView:
    """
    The parts of one job one viewer may see.

    The fields are exactly the attributes the templates read off a job, under
    the same names, so a template renders a view as it rendered a job.  It
    satisfies ``vocabulary.PageCounted`` and ``vocabulary.RemovedPagesNoted``,
    so ``page_counts`` and ``removed_pages`` format it too.

    Attributes:
        id: Unique job identifier.
        profile: Name of the scan profile the job used.
        state: The job's lifecycle state.
        created_at: Timezone-aware creation timestamp.
        error_category: Categorised error type, when the job failed.
        pages_scanned: Pages the scanner produced, once counted.
        pages_removed: Pages discarded as blank, once counted.
        pages_uploaded: Pages sent to paperless-ngx, once counted.
        removed_positions: The scanned page numbers removed as blank, once
            recorded.  Page numbers only, so every viewer sees them.
        title: The document title, or the generic title for a non-owner.
        thumbnail: The base64 JPEG thumbnail, or None for a non-owner.
        error: The error text with host paths named by setting, or a fixed
            sentence for a non-owner; None when the job has no error.
        warning: The warning text with host paths named by setting, or a fixed
            sentence for a non-owner; None when the job has no warning.

    """

    id: str
    profile: str
    state: JobState
    created_at: datetime
    error_category: ErrorCategory | None
    pages_scanned: int | None
    pages_removed: int | None
    pages_uploaded: int | None
    removed_positions: tuple[int, ...] | None
    title: str
    thumbnail: str | None
    error: str | None
    warning: str | None

    @property
    def is_active(self) -> bool:
        """Whether this job is still in flight (not DONE or ERROR)."""
        return self.state in ACTIVE_STATES

    @property
    def is_busy(self) -> bool:
        """Whether the machine is working (active, but not waiting for a human)."""
        return self.state in BUSY_STATES


def owns_detail(presented: str | None, recorded: str | None) -> bool:
    """
    Report whether a presented token may see a job's title, preview and text.

    This is deliberately not the flip prompt's ownership rule
    (``owner.is_owner``).  There a NULL recorded token means anyone may answer, so
    a manual-duplex job in flight across an upgrade stays answerable.  Here a
    NULL recorded token means nobody may see the detail: such a row was
    written before owner tokens existed, or records a refused submit, and no
    browser can prove it made it.  A browser presenting no token owns nothing.

    The comparison goes through ``secrets.compare_digest`` so no timing
    difference can be read off it.  Both sides are encoded first: the
    presented value arrives as text out of a header and ``compare_digest``
    refuses a non-ASCII ``str``, while it compares bytes of any two lengths
    safely.

    Args:
        presented: The token this request carries, or None.
        recorded: The token stored on the job row, or None when unowned.

    Returns:
        True only when both tokens are present and equal.

    """
    if presented is None or recorded is None:
        return False
    return secrets.compare_digest(presented.encode(), recorded.encode())


def build_job_view(job: Job, *, presented: str | None, settings: Settings) -> JobView:
    """
    Build what the browser presenting ``presented`` may see of ``job``.

    Args:
        job: The job to show.
        presented: The owner token the request carries, or None.
        settings: The running configuration, for the host paths to name.

    Returns:
        The owner's view, with the real title and thumbnail and relativised
        text, or everyone else's, with the generic title, no thumbnail and
        fixed sentences.  An absent or empty error or warning stays as it is
        in both, so a job with no warning never reads as a warned one.

    """
    if owns_detail(presented, job.owner_token):
        title = job.title
        thumbnail = job.thumbnail
        error = _relativise(job.error, settings) if job.error else job.error
        warning = _relativise(job.warning, settings) if job.warning else job.warning
    else:
        title = HIDDEN_JOB_TITLE
        thumbnail = None
        error = _hidden_error(job.error, settings) if job.error else job.error
        warning = HIDDEN_WARNING_LINE if job.warning else job.warning
    return JobView(
        id=job.id,
        profile=job.profile,
        state=job.state,
        created_at=job.created_at,
        error_category=job.error_category,
        pages_scanned=job.pages_scanned,
        pages_removed=job.pages_removed,
        pages_uploaded=job.pages_uploaded,
        # Page numbers only -- no title, no path -- so the owner and everyone
        # else see the same note.
        removed_positions=job.removed_positions,
        title=title,
        thumbnail=thumbnail,
        error=error,
        warning=warning,
    )


def _spellings(path: Path) -> set[str]:
    """
    Return the ways stored text may spell a configured directory.

    Text written by the pipeline may carry the path as configured or as the
    file system resolved it, which differ when the configured path runs
    through a symlink.  Only absolute spellings are returned: a directory
    configured as ``data`` or ``tmp`` would otherwise match those words in
    an ordinary sentence, and a relative path names no place on the host
    anyway.  Text that spells a relative directory as configured is left
    as it is.  The file system root is never returned: replacing ``/`` would
    rewrite every separator in the text.

    Args:
        path: The configured directory.

    Returns:
        The absolute spelling and the resolved one, without the root.

    """
    absolute = path.absolute()
    return {
        spelling
        for spelling in (str(absolute), str(path.resolve()))
        if spelling != absolute.anchor
    }


def _relativise(text: str, settings: Settings) -> str:
    """
    Replace every host path and web address in the owner's text.

    In one pass, with the longest spelling tried first so a directory nested
    inside another is named by the inner one:

    * ``data_dir`` followed by a separator is removed, so a kept file reads
      ``failed/<file>.pdf``, and ``data_dir`` on its own becomes
      ``<output.data_dir>``;
    * ``tmp_dir`` becomes ``<output.tmp_dir>``;
    * ``consume_dir``, when one is set, becomes ``<paperless.consume_dir>``.

    Each is matched in its absolute and its resolved spelling.  Then every
    ``http://`` or ``https://`` address becomes ``<paperless.url>``, and last
    every other absolute path becomes ``<path>``, each with the punctuation
    after it kept.  That covers a path from a row written under an older
    configuration, a device node and a path a library named.

    Args:
        text: The stored error or warning text.
        settings: The running configuration.

    Returns:
        The text with no configured host path and no web address in it.

    """
    output = settings.output
    names: dict[str, str] = {}
    for spelling in _spellings(output.data_dir):
        names[f"{spelling}/"] = ""
        names[spelling] = "<output.data_dir>"
    for spelling in _spellings(output.tmp_dir):
        names[spelling] = "<output.tmp_dir>"
    consume_dir = settings.paperless.consume_dir
    if consume_dir is not None:
        for spelling in _spellings(consume_dir):
            names[spelling] = "<paperless.consume_dir>"
    alternatives = "|".join(
        re.escape(needle) + ("" if needle.endswith("/") else _PATH_AFTER)
        for needle in sorted(names, key=len, reverse=True)
    )
    paths = re.compile(f"{_PATH_BEFORE}(?:{alternatives})")
    text = paths.sub(lambda match: names[match.group()], text)
    text = _URL.sub(lambda match: _name(match, "<paperless.url>"), text)
    return _OTHER_PATH.sub(lambda match: _name(match, "<path>"), text)


def scrub_for_owner(text: str, settings: Settings) -> str:
    """
    Replace every host path and web address in text shown to a job's owner.

    This is the one host-path rule for owner-visible text that does not come
    out of a job view: the failed-pass prompt shows the scanner's error while
    the job is still waiting, before any row stores it.  It applies exactly the
    rule a job view applies to its error and warning text, so the two cannot
    drift.  Whether the viewer is the owner at all is the caller's decision;
    anyone else must not be shown the text in any form.

    Args:
        text: The text to show, as the scanner or the pipeline wrote it.
        settings: The running configuration.

    Returns:
        The text with no configured host path and no web address in it.

    """
    return _relativise(text, settings)


def _name(match: re.Match[str], name: str) -> str:
    """
    Replace one matched address or path, keeping the punctuation after it.

    Args:
        match: A match of ``_URL`` or ``_OTHER_PATH``.
        name: What the matched text is replaced with.

    Returns:
        ``name`` followed by any trailing sentence punctuation.

    """
    matched = match.group()
    kept = matched.rstrip(_URL_TRAILING)
    return f"{name}{matched[len(kept) :]}"


def _hidden_error(text: str, settings: Settings) -> str:
    """
    Choose the fixed sentence a non-owner sees in place of an error.

    An error that says part of the scan was kept is worth knowing on any
    device; the kept file's name is not, because it carries the document's
    title.  Every other error gets the pointer to the full text.

    Only the pipeline's own statement that something was kept counts: a path
    inside the failed folder straight after one of ``_KEPT_BEFORE``'s
    phrases.  Merely naming that folder is not enough, because the message
    for a preservation that kept nothing names it too, as the destination it
    could not reach and often again in the file system's own error.

    Args:
        text: The stored error text.
        settings: The running configuration, for the failed folder's path.

    Returns:
        ``HIDDEN_PRESERVED_ERROR`` or ``HIDDEN_ERROR_DETAIL``.

    """
    folders = "|".join(
        re.escape(f"{spelling}/") for spelling in _spellings(settings.output.failed_dir)
    )
    phrases = "|".join(re.escape(phrase) for phrase in _KEPT_BEFORE)
    if re.search(f"(?:{phrases})(?:{folders})", text):
        return HIDDEN_PRESERVED_ERROR
    return HIDDEN_ERROR_DETAIL
