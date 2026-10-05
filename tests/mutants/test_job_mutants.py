"""
The job store's tests fail under the mutants they exist to catch.

Each check copies the repository, confirms the named test passes on the clean
copy, applies one hand-written edit to ``src/saneless/job.py`` and confirms the
same test then fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_JOB = "src/saneless/job.py"

_CREATE_JOB_READ_BACK = (
    "            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()\n"
    "        job = self._row_to_job(row)\n"
    "        # %r, not %s"
)
"""The read-back that closes ``create_job``'s transaction."""

_PROBE_WRITE = '            self._conn.execute(f"PRAGMA user_version = {version}")\n'
"""Probe's same-value ``user_version`` write."""

_PRUNE_STATEMENT = (
    "            deleted = self._conn.execute(\n"
    "                _PRUNE,\n"
    "                (\n"
    "                    cutoff,\n"
    "                    rejected,\n"
    "                    rejected,\n"
    "                    max_rows,\n"
    "                    rejected,\n"
    "                    rejected,\n"
    "                    REJECTED_HISTORY_ROWS,\n"
    "                ),\n"
    "            ).rowcount\n"
)
"""Prune's single counting ``DELETE``."""

_TWO_COUNT_PRUNE = (
    '            before = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]\n'
    '            self._conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))\n'
    "            self._conn.execute(\n"
    '                "DELETE FROM jobs WHERE id NOT IN "\n'
    '                "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",\n'
    "                (max_rows,),\n"
    "            )\n"
    '            after = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]\n'
    "            deleted = before - after\n"
)
"""A prune that counts, deletes by age, trims, counts again and subtracts."""


def test_lock_rule_sees_a_private_transaction_nested_in_create_job(
    tmp_path: Path,
) -> None:
    """
    The transitive lock-rule test fails when create_job calls a transaction helper.

    ``_pending_jobs`` opens its own ``with self._conn:``; called inside
    ``create_job``'s transaction, its exit commits the caller's insert early.
    """
    check_mutant(
        tmp_path,
        "tests/test_job.py::TestLockDiscipline::"
        "test_no_transaction_reaches_a_method_that_opens_its_own",
        [
            Edit(
                _JOB,
                _CREATE_JOB_READ_BACK,
                "            self._pending_jobs()\n" + _CREATE_JOB_READ_BACK,
            )
        ],
    )


def test_read_only_probe_test_sees_a_probe_without_its_write(tmp_path: Path) -> None:
    """The read-only probe test fails when probe drops its user_version write."""
    check_mutant(
        tmp_path,
        "tests/test_job.py::TestQueryMethods::test_probe_fails_on_a_read_only_store",
        [Edit(_JOB, _PROBE_WRITE, "")],
    )


def test_prune_trace_test_sees_the_two_count_prune(tmp_path: Path) -> None:
    """The prune trace test fails when prune counts, deletes, trims and subtracts."""
    check_mutant(
        tmp_path,
        "tests/test_job.py::TestPruneSingleStatement::"
        "test_prune_concurrent_window_between_count_and_delete_is_closed",
        [Edit(_JOB, _PRUNE_STATEMENT, _TWO_COUNT_PRUNE)],
    )
