"""The worker's health tests fail under the mutants they exist to catch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_READ_ONLY_STORE_TEST = (
    "tests/test_worker.py::TestWorkerDegradedHealth::"
    "test_a_degraded_worker_on_a_read_only_store_stays_degraded"
)


def test_a_probe_that_cannot_write_fails_the_read_only_store_test(
    tmp_path: Path,
) -> None:
    """
    The read-only store test fails when the probe stops writing.

    Without its ``user_version`` write the probe succeeds on a store that
    cannot be written, so a degraded worker would report itself healed.
    """
    check_mutant(
        tmp_path,
        _READ_ONLY_STORE_TEST,
        [
            Edit(
                "src/saneless/job.py",
                '            self._conn.execute(f"PRAGMA user_version = {version}")\n',
                "",
            )
        ],
    )
