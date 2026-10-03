"""
The config tests fail under the mutants they exist to catch.

Each mutant is applied to a copy of the repository, and the named test must
pass on the clean copy and fail on the mutated one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_PURITY_TEST = (
    "tests/test_config.py::TestProfileStorageForLoaded::"
    "test_it_is_pure_and_touches_no_filesystem"
)
_OWNED_KEY_TEST = (
    "tests/test_auto_profiles.py::TestScanProfileHowToDocumentsOwnership::"
    "test_owned_key_list_in_the_guide_names_every_owned_key"
)
_GUIDE = "docs/how-to/configure-scan-profiles.md"


def test_profile_storage_for_loaded_that_reads_the_disk_is_caught(
    tmp_path: Path,
) -> None:
    """The purity test fails when the derivation asks the filesystem about the file."""
    check_mutant(
        tmp_path,
        _PURITY_TEST,
        [
            Edit(
                "src/saneless/config.py",
                "    if settings.config_path is not None:\n"
                "        return ProfileStorage.PERSISTED\n"
                "    return ProfileStorage.IN_MEMORY_NO_CONFIG_FILE\n",
                "    settings.config_path.exists()\n"
                "    if settings.config_path is not None:\n"
                "        return ProfileStorage.PERSISTED\n"
                "    return ProfileStorage.IN_MEMORY_NO_CONFIG_FILE\n",
            )
        ],
    )


def test_a_guide_that_lists_too_few_owned_keys_is_caught(tmp_path: Path) -> None:
    """The owned-key test fails when the guide's list names only ``label``."""
    check_mutant(
        tmp_path,
        _OWNED_KEY_TEST,
        [
            Edit(
                _GUIDE,
                "only the generated keys (`label`, `description`, `source`, "
                "`resolution`, `mode`, `auto_source_mode`, `duplex`, "
                "`auto_generated`)",
                "only the generated keys (`label`)",
            )
        ],
    )
