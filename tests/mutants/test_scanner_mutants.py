"""
The scan-child tests fail, or skip, under the conditions they exist to catch.

Each check copies the repository, confirms the named test passes on the clean
copy, applies one hand-written mutant and confirms the test now fails (or, for
the real-libsane guard, is skipped).  The mutants target the parent's cancel
grace in ``scan_child.py``, the child's discard of a page read during a cancel
in ``scan_session.py``, and the skip guard of the real-child cancel test,
which must skip for missing options and fail for a failed option read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant, check_mutant_skips

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_SCAN_CHILD = "src/saneless/scanner/scan_child.py"
_SCAN_SESSION = "src/saneless/scanner/scan_session.py"
_SCAN_CHILD_LIBSANE = "tests/test_scan_child_libsane.py"

_PAGE_BUDGET_WAIT_SECONDS = 180
"""
How long the cancel-grace check may take.

Its mutated run waits on a child that ignores the cancel for the page budget,
two minutes at least, until the test's own sixty-second timeout ends it, on
top of the clean run and the copy.
"""


@pytest.mark.timeout(_PAGE_BUDGET_WAIT_SECONDS)
def test_the_abort_test_fails_when_the_cancel_waits_out_the_page(
    tmp_path: Path,
) -> None:
    """
    The mid-read abort test fails when the stop waits the page budget, not the grace.

    The mutant still cancels the child and still kills it if it will not
    exit, but only once a whole page's budget has passed: the same outcome,
    minutes late, while the server waits to stop.  It also drops the cut
    that holds any wait on a stopping child to the grace once the abort is
    set, which would otherwise save the mutated stop.
    """
    check_mutant(
        tmp_path,
        "tests/test_scan_child.py::test_an_abort_mid_read_returns_within_the_grace"
        "[ignores-cancel]",
        [
            Edit(
                _SCAN_CHILD,
                '            logger.info("The scan was stopped because saneless is '
                'stopping")\n'
                "            self._stop_child(ControlOp.CANCEL, CANCEL_GRACE_SECONDS)\n",
                '            logger.info("The scan was stopped because saneless is '
                'stopping")\n'
                "            budget = self._page_budget\n"
                "            self._stop_child(\n"
                "                ControlOp.CANCEL,\n"
                "                STAGE_DEADLINE_SECONDS if budget is None else budget[0],\n"
                "            )\n",
            ),
            Edit(
                _SCAN_CHILD,
                "        self._deadline = min(self._deadline, time.monotonic() + "
                "CANCEL_GRACE_SECONDS)\n",
                "        return\n",
            ),
        ],
    )


def test_the_discard_test_fails_when_a_cancelled_page_is_kept(
    tmp_path: Path,
) -> None:
    """
    The discard test fails when a page read during a cancel is handed on.

    A cancelled ``snap()`` can return a truncated image, so the check after
    the read is what keeps that image from being spooled as a page.  The
    mutant removes it.
    """
    check_mutant(
        tmp_path,
        "tests/test_scan_session.py::test_a_page_read_during_a_cancel_is_discarded",
        [
            Edit(
                _SCAN_SESSION,
                "    outlet.reading(None)\n"
                "    if outlet.cancel_requested():\n"
                "        _await_backend_threads(before)\n"
                "        raise PassStopped\n"
                "    return _as_image(image)\n",
                "    outlet.reading(None)\n    return _as_image(image)\n",
            )
        ],
    )


@pytest.mark.sane_hardware
def test_the_real_cancel_test_skips_when_libsane_lacks_the_read_delay_options(
    tmp_path: Path,
) -> None:
    """
    The real-child cancel test skips when the device lacks the read-delay options.

    The test asks a listing child for the device's option list before it
    scans.  The mutant stands in for a libsane build whose ``test`` backend
    has no read-delay or read-limit options: it hides those names from that
    list.  Without the guard the test would scan a page that is never slow,
    and its cancel would land after the read had already finished.
    """
    check_mutant_skips(
        tmp_path,
        f"{_SCAN_CHILD_LIBSANE}"
        "::test_a_cancel_mid_read_ends_the_real_child_within_the_grace",
        [
            Edit(
                _SCAN_CHILD_LIBSANE,
                "offered = {option[0] for option in reply.options}",
                "offered = {option[0] for option in reply.options}"
                " - _READ_DELAY_OPTION_NAMES",
            )
        ],
    )


@pytest.mark.sane_hardware
def test_the_real_cancel_test_fails_when_the_capability_read_fails(
    tmp_path: Path,
) -> None:
    """
    The real-child cancel test fails, not skips, when its option read fails.

    The mutant asks for the options of a device the test backend does not
    have, so the read fails to open it.  A failed read says nothing about
    which options libsane offers, so it must not pass for a backend that
    lacks the read-delay options.
    """
    check_mutant(
        tmp_path,
        f"{_SCAN_CHILD_LIBSANE}"
        "::test_a_cancel_mid_read_ends_the_real_child_within_the_grace",
        [
            Edit(
                _SCAN_CHILD_LIBSANE,
                'listing.ListingRequest(capabilities="test:0")',
                'listing.ListingRequest(capabilities="test:99")',
            )
        ],
    )
