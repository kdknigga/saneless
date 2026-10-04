"""
What one browser may see of one job: the only job shape templates receive.

Any device on the LAN can load the web page, and a job's title, first-page
picture and error and warning text, which can name host paths, the kept PDF and
the paperless-ngx address, are not for all of them.  Templates are handed a
:class:`JobView`, never a :class:`~saneless.job.Job`, and :func:`build_job_view`
decides once, on the server, what goes into it.

The owner sees the title, the thumbnail and the text with every host path and
web address replaced by the setting that holds it, or by ``<path>``.  Every
other browser sees that a scan happened and how it ended, with a generic title,
no thumbnail and fixed sentences; the gated fields are absent from the view, not
hidden with CSS.  The full text stays in the log and in ``saneless jobs``.
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

# Any web address: one from an earlier configuration or a library's message
# would slip past a match on the current URL, and the owner never needs it.  The
# scheme is case-insensitive, and ``paperless.url`` is kept as typed, so
# ``HTTPS://`` reaches the stored text as written.
_URL = re.compile(r"https?://\S+", re.IGNORECASE)

# Punctuation, quotes and brackets that end the sentence around a URL or a path
# rather than belonging to it.  ``\S+`` swallows them, so they are put back
# after the replacement.
_URL_TRAILING = ").,:;'\"]>"

# A host path counts only where it starts and ends as a whole path, so the
# ``tmp`` directory does not also match ``tmp2`` beside it.  The lookahead
# still lets a path end a sentence: a full stop is a boundary unless a name
# character follows it.
_PATH_BEFORE = r"(?<![\w.-])"
_PATH_AFTER = r"(?![\w-]|\.\w)"

# Any absolute path left once the configured directories and web addresses are
# named: a device node, a path under an earlier configuration, one a library
# named.  It starts at a "/" not directly after a name character, dot, "<", ">"
# or "-", so "failed/x.pdf", "<output.tmp_dir>/x" and "and/or" are left alone.
_OTHER_PATH = re.compile(r"(?<![\w.<>-])/[^\s'\"<>]+")

# What the pipeline writes directly before the path of something it kept in the
# failed folder; every sentence ``preservation.KeptGroup`` composes puts it
# before its first path.  A preservation that kept nothing never writes it.
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
        """Whether this job is still in flight (not in a terminal state)."""
        return self.state in ACTIVE_STATES

    @property
    def is_busy(self) -> bool:
        """Whether the machine is working (active, but not waiting for a human)."""
        return self.state in BUSY_STATES


def owns_detail(presented: str | None, recorded: str | None) -> bool:
    """
    Report whether a presented token may see a job's title, preview and text.

    Deliberately not ``owner.is_owner``: there a job with no recorded token may
    be answered by anyone, while here nobody may see its detail, because no
    browser can prove it made the job.  A browser presenting no token owns
    nothing.

    Both sides are encoded before ``secrets.compare_digest``, which refuses a
    non-ASCII ``str`` but compares bytes of any two lengths in constant time.

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

    The configured and the resolved spelling differ when the path runs through
    a symlink.  Only absolute spellings are returned, so a directory configured
    as ``data`` does not match the word in a sentence, and never the root,
    which would rewrite every separator.

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

    In one pass, longest spelling first so a nested directory is named by the
    inner one: ``data_dir`` plus a separator is removed, so a kept file reads
    ``failed/<file>.pdf``, and otherwise ``data_dir``, ``tmp_dir`` and
    ``consume_dir`` become their setting names.  Then every web address becomes
    ``<paperless.url>`` and every other absolute path ``<path>``, each keeping
    the punctuation after it.

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

    For owner-visible text that does not come from a job view, such as the
    failed-pass prompt's scanner error, with exactly the rule a job view
    applies.  Whether the viewer is the owner is the caller's decision; anyone
    else must not see the text in any form.

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

    An error saying part of the scan was kept is worth knowing on any device;
    the kept file's name, which carries the title, is not.  Only one of
    ``_KEPT_BEFORE``'s phrases followed by a failed-folder path counts, because
    a preservation that kept nothing names that folder too.

    """
    folders = "|".join(
        re.escape(f"{spelling}/") for spelling in _spellings(settings.output.failed_dir)
    )
    phrases = "|".join(re.escape(phrase) for phrase in _KEPT_BEFORE)
    if re.search(f"(?:{phrases})(?:{folders})", text):
        return HIDDEN_PRESERVED_ERROR
    return HIDDEN_ERROR_DETAIL
