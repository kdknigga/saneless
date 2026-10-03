"""
The scanner tests fail, or skip, under the conditions they exist to catch.

Each check copies the repository, confirms the named test passes on the clean
copy, applies one hand-written mutant and confirms the test now fails (or, for
the real-libsane guard, is skipped).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant, check_mutant_skips

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_SANE_BACKEND = "src/saneless/scanner/sane_backend.py"

_DEFERRED_INTERRUPT_SECONDS = 180
"""
How long the interrupt check may take.

Its mutated run sits out the interrupted test's whole thirty-second page
budget, on top of the clean run and the copy.
"""


@pytest.mark.timeout(_DEFERRED_INTERRUPT_SECONDS)
def test_the_mid_read_interrupt_test_fails_when_the_interrupt_waits_out_the_page(
    tmp_path: Path,
) -> None:
    """
    The mid-read interrupt test fails when the interrupt waits out the page budget.

    The mutant leaves the interrupt to be raised, and the device to be settled,
    but only once the read has had its whole page timeout: the same outcome,
    arriving thirty seconds late.
    """
    check_mutant(
        tmp_path,
        "tests/test_scanner.py::TestSaneBackendCancelSequence"
        "::test_keyboard_interrupt_mid_read[ctrl-c]",
        [
            Edit(
                _SANE_BACKEND,
                "        if started or reader.is_alive():\n"
                "            _settle_or_wedge(dev, done, budget.grace, page_label)\n"
                "        raise\n",
                "        if started or reader.is_alive():\n"
                "            done.wait(budget.timeout)\n"
                "            _settle_or_wedge(dev, done, budget.grace, page_label)\n"
                "        raise\n",
            )
        ],
    )


def test_the_settle_test_fails_when_a_finished_read_is_not_rechecked(
    tmp_path: Path,
) -> None:
    """
    The finished-read settle test fails when the settle skips its done re-check.

    Without the re-check a read that returned in the instant before the
    timeout is recorded as wedged, and its handle refused until restart.
    """
    check_mutant(
        tmp_path,
        "tests/test_scanner.py::TestBeginSettle"
        "::test_an_already_finished_read_is_not_marked_wedged",
        [
            Edit(
                _SANE_BACKEND,
                "    with _WEDGE_LOCK:\n"
                "        if done.is_set():\n"
                "            return False\n"
                "        _WEDGE.stuck = True\n",
                "    with _WEDGE_LOCK:\n        _WEDGE.stuck = True\n",
            )
        ],
    )


@pytest.mark.sane_hardware
def test_the_real_cancel_test_skips_when_libsane_lacks_the_read_delay_options(
    tmp_path: Path,
) -> None:
    """
    The real-libsane cancel test skips when the device lacks the read-delay options.

    The mutant stands in for a libsane build whose ``test`` backend has no
    read-delay or read-limit options: it hides those four names from the
    device's option list.  python-sane stores an unknown option name as a plain
    attribute without complaint, so without the guard the test would run
    against a read that is never slow.
    """
    check_mutant_skips(
        tmp_path,
        "tests/test_sane_hardware.py::TestRealSaneCancelSequence"
        "::test_a_cancel_unblocks_a_slow_read_and_the_handle_still_closes",
        [
            Edit(
                "tests/test_sane_hardware.py",
                "offered = {option[1] for option in device.get_options()}",
                "offered = {option[1] for option in device.get_options()}"
                " - _READ_DELAY_OPTION_NAMES",
            )
        ],
    )
