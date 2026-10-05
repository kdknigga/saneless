"""
Manual-duplex pairing, and what to say when the two passes cannot be paired.

A manual-duplex run scans the fronts in one pass and, once the operator has
flipped the stack, the backs in a second.  This module interleaves the two
passes by position into one document, reconciles the resolution they
reported, and words the warnings for a run whose passes cannot be paired by
position and are delivered as two PDFs instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from saneless import preservation
from saneless.exceptions import ScanError
from saneless.vocabulary import backs_pass_cap_warning

if TYPE_CHECKING:
    from collections.abc import Sequence

    from saneless.scanner.base import PageRecord, PassCapReached, ScanBatch

__all__ = [
    "DuplexMismatch",
    "backs_cap_warning",
    "duplex_mismatch_warning",
    "duplex_resolution",
    "interleave_duplex",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DuplexMismatch:
    """
    The two passes of a manual duplex run that cannot be paired by position.

    Either the page counts disagreed, a pass could not read a sheet, or the
    backs pass stopped at its cap. After a lost sheet, equal counts prove
    nothing: when each pass loses a different sheet, the counts match and the
    interleave pairs fronts with the wrong backs. A cap is the same kind of
    evidence: the backs pass fed a sheet it threw away, and the fronts pass
    never fed that sheet, so the stack was not the one pass A saw. The
    resolution is not here: each record carries the dpi its page was read
    back at.

    Attributes:
        fronts: Page records produced by pass A, in pass order.
        backs: Page records produced by pass B, in pass order.
        unreadable_sheets: Sheets skipped across both passes for failing their
            integrity checks. Nonzero is the reason the run was split. Zero
            means the split is a count difference or a cap.
        backs_cap: The cap the backs pass stopped at, or None when it ended on
            its own. A capped fronts pass never gets here: no backs pass is
            scanned after it.
        substituted_source: The flatbed source the profile asked for when the
            scanner's Auto source took pass A through the feeder instead, or
            None. The SANE backend never substitutes on manual duplex.

    """

    fronts: Sequence[PageRecord]
    backs: Sequence[PageRecord]
    unreadable_sheets: int
    backs_cap: PassCapReached | None = None
    substituted_source: str | None = None


def duplex_resolution(front: ScanBatch, back: ScanBatch) -> int:
    """
    Reconcile the resolution the two manual-duplex passes reported.

    A disagreement is logged, not fatal: each page's record carries the dpi
    its own pass read back and assembly lays every page out at its own, so
    no page loses its real size.

    Args:
        front: The batch pass A produced.
        back: The batch pass B produced.

    Returns:
        The resolution the interleaved batch reports: pass A's.

    """
    if front.actual_resolution != back.actual_resolution:
        logger.warning(
            "Manual duplex passes disagree on resolution: pass A reports %s dpi, "
            "pass B reports %s dpi; each page is laid out at its own pass's",
            front.actual_resolution,
            back.actual_resolution,
        )
    return front.actual_resolution


def backs_cap_warning(cap: PassCapReached | None, pages_kept: int) -> str | None:
    """
    Say that a manual-duplex backs pass stopped at its cap, and log it, if so.

    Worded apart from the pipeline's ``_pass_cap_warning``: the sheet past the
    cap has no scanned front, so resuming from it would recover nothing.

    Args:
        cap: The cap the backs pass reached, or None when it ended on its own.
        pages_kept: How many pages the two halves hold together.

    Returns:
        The warning text, or None when no cap was reached.

    """
    if cap is None:
        return None
    warning = backs_pass_cap_warning(pages_kept, cap.cap, cap.sheet_not_kept)
    logger.warning(warning)
    return warning


def duplex_mismatch_warning(mismatch: DuplexMismatch) -> str:
    """
    Say why the two manual-duplex passes were delivered as two PDFs.

    A lost sheet takes precedence over the counts: when each pass lost a
    different sheet the counts agree, and a "page count mismatch" sentence
    quoting two equal numbers would be false. It is the only sentence that
    mentions the lost sheets, so the generic unreadable-sheet warning is not
    added on top of it. A capped backs pass comes next for the same reason:
    its counts may agree too. The pass-cap sentence that names the sheet
    thrown away is joined after this one by the caller.

    Args:
        mismatch: Both passes, and the sheets the scanner could not read.

    Returns:
        The warning, naming the unreadable sheets when there were any, else
        the cap when the backs pass reached it, and otherwise the two page
        counts.

    """
    halves = (
        f"they were uploaded as two PDFs, {preservation.FRONTS_SUFFIX} and "
        f"{preservation.BACKS_SUFFIX}, for manual review."
    )
    count = mismatch.unreadable_sheets
    if count > 0:
        sheets = "1 sheet" if count == 1 else f"{count} sheets"
        return (
            f"The scanner could not read {sheets}, so the fronts and backs "
            f"could not be paired reliably; {halves}"
        )
    if mismatch.backs_cap is not None:
        return (
            "The scan of the backs stopped at its sheet cap, so the fronts and "
            f"backs could not be paired reliably; {halves}"
        )
    return (
        f"Page count mismatch: {len(mismatch.fronts)} fronts, "
        f"{len(mismatch.backs)} backs. Partial PDFs saved."
    )


def interleave_duplex(
    fronts: Sequence[PageRecord],
    backs: Sequence[PageRecord],
) -> list[PageRecord]:
    """
    Interleave front and back pages for manual duplex.

    Backs are reversed because the user flips the stack face-down,
    so the last front's back is scanned first in pass B.

    Records are reordered, never files: the ``a-`` and ``b-`` spool names do
    not sort into document order, so document order is this list's, never the
    directory's.

    Args:
        fronts: Front-side records from pass A.
        backs: Back-side records from pass B (in scan order).

    Returns:
        Interleaved records: A1, B1, A2, B2, ...

    Raises:
        ScanError: If front and back page counts do not match.

    """
    if len(fronts) != len(backs):
        msg = f"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs"
        raise ScanError(msg)
    backs_reversed = list(reversed(backs))
    result: list[PageRecord] = []
    for front, back in zip(fronts, backs_reversed, strict=True):
        result.append(front)
        result.append(back)
    return result
