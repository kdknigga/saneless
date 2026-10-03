"""The PDF tests fail under the regressions they exist to catch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_PDF_MODULE = "src/saneless/pdf.py"

_UTC_TEST = "tests/test_pdf.py::TestBuildPdfFilename::test_starts_with_a_utc_timestamp"
_MEMORY_TEST = (
    "tests/test_pdf.py::TestAssemblyMemory::test_peak_memory_is_flat_in_page_count"
)


def test_utc_timestamp_test_kills_a_local_time_stamp(tmp_path: Path) -> None:
    """The UTC-timestamp test fails when the file name is stamped in local time."""
    check_mutant(
        tmp_path,
        _UTC_TEST,
        [
            Edit(
                _PDF_MODULE,
                "datetime.now(tz=UTC).strftime",
                "datetime.now().strftime",
            )
        ],
    )


def test_peak_memory_test_kills_retained_pages(tmp_path: Path) -> None:
    """The peak-memory test fails when every finished page stays in memory."""
    check_mutant(
        tmp_path,
        _MEMORY_TEST,
        [
            Edit(
                _PDF_MODULE,
                "singles.append(str(single))",
                "singles.append(str(single)); "
                'globals().setdefault("_retained", []).append(single.read_bytes())',
            )
        ],
    )
