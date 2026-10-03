"""The CLI and logging tests fail under the mutants they exist to catch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_LOGGING_CONFIG = "src/saneless/logging_config.py"
_CLI = "src/saneless/cli.py"


def test_verbose_mirror_is_counted_by_its_stream(tmp_path: Path) -> None:
    """A ``configure_logging`` that ignores ``verbose`` attaches no stderr mirror."""
    check_mutant(
        tmp_path,
        "tests/test_logging.py::TestConfigureLogging::test_verbose_adds_stderr_handler",
        [
            Edit(
                _LOGGING_CONFIG,
                "    formatter = logging.Formatter(_FORMAT)\n",
                "    verbose = False\n    formatter = logging.Formatter(_FORMAT)\n",
            )
        ],
    )


def test_jobs_json_that_crashes_before_printing(tmp_path: Path) -> None:
    """A ``jobs --json`` that raises before printing prints no local time either."""
    check_mutant(
        tmp_path,
        "tests/test_cli.py::TestJobsJsonContract"
        "::test_json_never_carries_the_local_rendering",
        [
            Edit(
                _CLI,
                "    recent = read_recent_jobs(settings.output.db_path, limit)\n"
                "    if as_json:\n",
                "    recent = read_recent_jobs(settings.output.db_path, limit)\n"
                "    if as_json:\n"
                '        raise RuntimeError("jobs crashed before printing")\n',
            )
        ],
    )


def test_jobs_table_that_prints_nothing_when_empty(tmp_path: Path) -> None:
    """A ``jobs`` that prints nothing for an empty history still exits 0."""
    check_mutant(
        tmp_path,
        "tests/test_cli.py::TestJobsCommand::test_jobs_empty",
        [
            Edit(
                _CLI,
                "    else:\n        cols = shutil.get_terminal_size((80, 24)).columns\n",
                "    elif recent:\n"
                "        cols = shutil.get_terminal_size((80, 24)).columns\n",
            )
        ],
    )
