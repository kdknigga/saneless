"""
Tests for the scan metadata policy and the stale-id check.

The policy says what an untouched metadata control means; the check drops an
id paperless-ngx no longer has, and only when a successful fetch shows it.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from saneless.config import ProfileConfig
from saneless.exceptions import ConfigError, PaperlessError
from saneless.paperless import PaperlessClient
from saneless.scan_metadata import (
    ClientMetadataLookup,
    ScanMetadata,
    check_scan_metadata,
    resolve_scan_metadata,
)
from saneless.vocabulary import dropped_ids_warning

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


class _ScriptedLookup:
    """A lookup that answers each list from a script and counts every call."""

    def __init__(
        self,
        *,
        tags: list[frozenset[int] | None] | None = None,
        correspondents: list[frozenset[int] | None] | None = None,
    ) -> None:
        """Take one answer per expected call, per list, in order."""
        self._answers = {"tags": tags or [], "correspondents": correspondents or []}
        self.calls: list[tuple[str, bool]] = []

    def _answer(self, what: str, *, fresh: bool) -> frozenset[int] | None:
        """Record the call and hand out the next scripted answer."""
        self.calls.append((what, fresh))
        return self._answers[what].pop(0)

    def tag_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Answer the tag list."""
        return self._answer("tags", fresh=fresh)

    def correspondent_ids(self, *, fresh: bool) -> frozenset[int] | None:
        """Answer the correspondent list."""
        return self._answer("correspondents", fresh=fresh)


class TestCheckScanMetadataStaleIds:
    """An id is dropped only when a successful refetch still lacks it."""

    def test_no_ids_make_no_lookup(self) -> None:
        """Nothing to check means nothing is asked of paperless-ngx."""
        lookup = _ScriptedLookup()
        metadata = ScanMetadata(tags=(), correspondent=None)

        assert check_scan_metadata(metadata, lookup) == (metadata, None)
        assert lookup.calls == []

    def test_known_ids_pass_with_one_call_per_list(self) -> None:
        """Every id found at the first look: no refetch, no warning."""
        lookup = _ScriptedLookup(
            tags=[frozenset({3, 7})], correspondents=[frozenset({12})]
        )
        metadata = ScanMetadata(tags=(3, 7), correspondent=12)

        assert check_scan_metadata(metadata, lookup) == (metadata, None)
        assert lookup.calls == [("tags", False), ("correspondents", False)]

    def test_a_stale_tag_is_dropped_after_one_refetch(self) -> None:
        """Still missing after the refetch: dropped, and the warning names it."""
        lookup = _ScriptedLookup(tags=[frozenset({3, 7}), frozenset({3, 7})])

        checked, warning = check_scan_metadata(
            ScanMetadata(tags=(3, 9), correspondent=None), lookup
        )

        assert checked == ScanMetadata(tags=(3,), correspondent=None)
        assert warning == dropped_ids_warning((9,), None)
        assert lookup.calls == [("tags", False), ("tags", True)]

    def test_a_tag_found_on_the_refetch_is_kept(self) -> None:
        """A tag created since the first look is kept, and there is one refetch."""
        lookup = _ScriptedLookup(tags=[frozenset({3}), frozenset({3, 9})])
        metadata = ScanMetadata(tags=(3, 9), correspondent=None)

        assert check_scan_metadata(metadata, lookup) == (metadata, None)
        assert lookup.calls == [("tags", False), ("tags", True)]

    def test_an_unavailable_lookup_passes_the_ids_unchecked(self) -> None:
        """paperless-ngx unreachable: the ids go through and the upload decides."""
        lookup = _ScriptedLookup(tags=[None], correspondents=[None])
        metadata = ScanMetadata(tags=(3, 9), correspondent=12)

        assert check_scan_metadata(metadata, lookup) == (metadata, None)
        assert lookup.calls == [("tags", False), ("correspondents", False)]

    def test_a_failed_refetch_never_drops_a_stale_looking_id(self) -> None:
        """Only a successful fetch proves an id stale; a failed one proves nothing."""
        lookup = _ScriptedLookup(tags=[frozenset({3}), None])
        metadata = ScanMetadata(tags=(3, 9), correspondent=None)

        assert check_scan_metadata(metadata, lookup) == (metadata, None)
        assert lookup.calls == [("tags", False), ("tags", True)]

    def test_a_stale_correspondent_is_dropped_against_a_known_empty_list(
        self,
    ) -> None:
        """An empty list is an answer: the correspondent is gone."""
        lookup = _ScriptedLookup(correspondents=[frozenset(), frozenset()])

        checked, warning = check_scan_metadata(
            ScanMetadata(tags=(), correspondent=12), lookup
        )

        assert checked == ScanMetadata(tags=(), correspondent=None)
        assert warning == dropped_ids_warning((), 12)
        assert lookup.calls == [("correspondents", False), ("correspondents", True)]

    def test_stale_tags_and_correspondent_share_one_warning(self) -> None:
        """Both lists short: every dropped id is named in the one warning."""
        lookup = _ScriptedLookup(
            tags=[frozenset({3}), frozenset({3})],
            correspondents=[frozenset({4}), frozenset({4})],
        )

        checked, warning = check_scan_metadata(
            ScanMetadata(tags=(9, 3, 8), correspondent=12), lookup
        )

        assert checked == ScanMetadata(tags=(3,), correspondent=None)
        assert warning == dropped_ids_warning((9, 8), 12)


class _FakeMetadataClient:
    """A client double answering the two metadata lists, recording each timeout."""

    def __init__(
        self,
        tags: object = None,
        correspondents: object = None,
        *,
        error: Exception | None = None,
    ) -> None:
        """Serve ``tags`` and ``correspondents``, or raise ``error`` from both."""
        self._tags = tags
        self._correspondents = correspondents
        self._error = error
        self.timeouts: list[tuple[str, float | None]] = []

    def get_tags(self, *, timeout: float | None = None) -> list[dict[str, object]]:
        """Answer the tag list."""
        return self._serve("tags", self._tags, timeout)

    def get_correspondents(
        self, *, timeout: float | None = None
    ) -> list[dict[str, object]]:
        """Answer the correspondent list."""
        return self._serve("correspondents", self._correspondents, timeout)

    def _serve(
        self, what: str, answer: object, timeout: float | None
    ) -> list[dict[str, object]]:
        """Record the request, then raise or answer."""
        self.timeouts.append((what, timeout))
        if self._error is not None:
            raise self._error
        return cast("list[dict[str, object]]", answer)


class TestClientMetadataLookup:
    """The CLI's lookup reads the client once, briefly, and trusts only data."""

    def test_lookup_reads_int_ids_of_dict_entries_with_a_short_timeout(self) -> None:
        """Only an int id on a dict counts; the fetch has the 5 s budget."""
        client = _FakeMetadataClient(
            tags=[{"id": 3}, {"id": True}, {"id": "7"}, 9, {"name": "x"}, {"id": 11}]
        )

        assert ClientMetadataLookup(client).tag_ids(fresh=False) == frozenset({3, 11})
        assert client.timeouts == [("tags", 5.0)]

    def test_lookup_does_not_refetch_what_it_just_fetched(self) -> None:
        """The CLI's first fetch is already fresh, so a fresh look reuses it."""
        client = _FakeMetadataClient(correspondents=[{"id": 12}])
        lookup = ClientMetadataLookup(client)

        first = lookup.correspondent_ids(fresh=False)

        assert lookup.correspondent_ids(fresh=True) == first == frozenset({12})
        assert client.timeouts == [("correspondents", 5.0)]

    @pytest.mark.parametrize(
        "error",
        [PaperlessError("unreachable"), ConfigError("no url")],
        ids=["paperless-error", "config-error"],
    )
    def test_lookup_failure_is_unavailable(self, error: Exception) -> None:
        """A fetch that raises a client error means 'cannot tell', not 'none'."""
        lookup = ClientMetadataLookup(_FakeMetadataClient(error=error))

        assert lookup.tag_ids(fresh=False) is None
        assert lookup.correspondent_ids(fresh=False) is None

    def test_lookup_of_a_non_list_is_unavailable(self) -> None:
        """A stub's MagicMock answer is not data, so nothing is judged stale."""
        lookup = ClientMetadataLookup(MagicMock(spec=PaperlessClient))

        assert lookup.tag_ids(fresh=False) is None
