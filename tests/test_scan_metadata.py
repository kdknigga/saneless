"""Tests for the one policy that says what an untouched metadata control means."""

from __future__ import annotations

from saneless.config import ProfileConfig
from saneless.scan_metadata import ScanMetadata, resolve_scan_metadata

_RECEIPTS = ProfileConfig.model_validate(
    {"default_tags": [3, 7], "default_correspondent": 12}
)


class TestResolveScanMetadata:
    """An unanswered control takes the profile's default; an answer is whole."""

    def test_no_answer_gives_the_profile_defaults(self) -> None:
        """Nothing answered: the profile's tags and correspondent apply."""
        metadata = resolve_scan_metadata(
            _RECEIPTS, tags=None, correspondent=None, correspondent_given=False
        )

        assert metadata == ScanMetadata(tags=(3, 7), correspondent=12)

    def test_a_cleared_tag_list_is_an_answer(self) -> None:
        """An empty list means no tags, not the profile's tags."""
        metadata = resolve_scan_metadata(
            _RECEIPTS, tags=[], correspondent=None, correspondent_given=False
        )

        assert metadata.tags == ()
        assert metadata.correspondent == 12

    def test_given_tags_replace_the_defaults_and_repeats_collapse(self) -> None:
        """A given list is the whole answer, each id kept once, first place kept."""
        metadata = resolve_scan_metadata(
            _RECEIPTS, tags=[7, 3, 7], correspondent=None, correspondent_given=False
        )

        assert metadata.tags == (7, 3)

    def test_no_correspondent_is_an_answer(self) -> None:
        """'No correspondent' given means none, even with a profile default."""
        metadata = resolve_scan_metadata(
            _RECEIPTS, tags=None, correspondent=None, correspondent_given=True
        )

        assert metadata.correspondent is None
        assert metadata.tags == (3, 7)

    def test_a_given_correspondent_replaces_the_default(self) -> None:
        """A chosen correspondent beats the profile's."""
        metadata = resolve_scan_metadata(
            _RECEIPTS, tags=None, correspondent=5, correspondent_given=True
        )

        assert metadata.correspondent == 5

    def test_an_unanswered_correspondent_ignores_the_value_passed(self) -> None:
        """Without an answer the value is meaningless and the default applies."""
        metadata = resolve_scan_metadata(
            _RECEIPTS, tags=None, correspondent=5, correspondent_given=False
        )

        assert metadata.correspondent == 12

    def test_a_bare_profile_gives_nothing(self) -> None:
        """A profile with no defaults resolves an untouched scan to no metadata."""
        metadata = resolve_scan_metadata(
            ProfileConfig(), tags=None, correspondent=None, correspondent_given=False
        )

        assert metadata == ScanMetadata(tags=(), correspondent=None)
