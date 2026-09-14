"""
Tests for the shared saneless vocabulary module.

Covers requirements: CTR-01, CTR-02, CTR-05.
"""

from __future__ import annotations

import json
from dataclasses import fields
from typing import cast

import pytest

import saneless.job
from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    PaperlessTimeoutError,
    ScanError,
)
from saneless.job import ErrorCategory as JobErrorCategory
from saneless.job import Job
from saneless.job import JobState as JobJobState
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    TERMINAL_STATES,
    ConnectionStatus,
    ErrorCategory,
    JobState,
    ScanOutcome,
    classify_error,
    connection_status_message,
    error_message,
    job_state_for,
    progress_label,
    state_label,
)


class TestJobStateMembers:
    """JobState membership tests."""

    def test_job_state_has_exactly_nine_members(self) -> None:
        """
        JobState declares exactly nine lifecycle members (CTR-01, DPLX-06).

        A count guard, not a name list: adding a member should fail the
        parametrised completeness tests below -- which force a label and a
        classification decision -- rather than a hand-written roster that only
        records what the enum happened to contain when it was written.
        """
        assert len(list(JobState)) == 9

    @pytest.mark.parametrize("state", list(JobState))
    def test_job_state_value_equals_name(self, state: JobState) -> None:
        """Every JobState value is identical to its member name (CTR-01)."""
        assert state.value == state.name


class TestErrorCategoryMembers:
    """ErrorCategory membership tests."""

    def test_error_category_member_names(self) -> None:
        """
        ErrorCategory names are the five documented categories (CTR-05).

        Compared as a set: declaration order is not part of any contract, and
        pinning it would fail a harmless reordering.
        """
        assert {category.name for category in ErrorCategory} == {
            "FEEDER",
            "CONFIG",
            "SCANNER",
            "UPLOAD",
            "UNKNOWN",
        }

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_category_value_equals_name(self, category: ErrorCategory) -> None:
        """Every ErrorCategory value is identical to its member name (CTR-05)."""
        assert category.value == category.name


class TestScanOutcomeMembers:
    """ScanOutcome membership tests."""

    def test_scan_outcome_has_exactly_two_members(self) -> None:
        """
        ScanOutcome is exactly SUCCESS and FALLBACK (CTR-02, OUTC-02).

        There is no FAILED member and there will not be one: a failure raises,
        so ``outcome`` stays NULL and ``JobState.ERROR`` carries the failure.
        A returned FAILED would record the same fact in a second column that
        could disagree with the first about a job with exactly one fate.
        """
        assert {outcome.name for outcome in ScanOutcome} == {"SUCCESS", "FALLBACK"}

    @pytest.mark.parametrize("outcome", list(ScanOutcome))
    def test_scan_outcome_value_equals_name(self, outcome: ScanOutcome) -> None:
        """Every ScanOutcome value is identical to its member name (CTR-02)."""
        assert outcome.value == outcome.name


class TestStateClassifications:
    """ACTIVE_STATES / TERMINAL_STATES / BUSY_STATES classification tests."""

    @pytest.mark.parametrize("state", list(JobState))
    def test_every_state_is_active_xor_terminal(self, state: JobState) -> None:
        """Each JobState is either active or terminal, never both (CTR-01)."""
        assert (state in ACTIVE_STATES) ^ (state in TERMINAL_STATES)

    def test_classifications_cover_every_state(self) -> None:
        """ACTIVE_STATES and TERMINAL_STATES together are all of JobState (CTR-01)."""
        assert set(JobState) == ACTIVE_STATES | TERMINAL_STATES

    def test_classifications_do_not_overlap(self) -> None:
        """ACTIVE_STATES and TERMINAL_STATES share no member (CTR-01)."""
        assert not (ACTIVE_STATES & TERMINAL_STATES)

    def test_active_states_membership(self) -> None:
        """ACTIVE_STATES is the six in-flight lifecycle states (CTR-01, DPLX-06)."""
        assert (
            frozenset(
                {
                    JobState.PENDING,
                    JobState.SCANNING,
                    JobState.AWAITING_FLIP,
                    JobState.SCANNING_REVERSE,
                    JobState.ASSEMBLING,
                    JobState.UPLOADING,
                }
            )
            == ACTIVE_STATES
        )

    def test_terminal_states_membership(self) -> None:
        """TERMINAL_STATES is exactly DONE, ERROR and FALLBACK (CTR-01, OUTC-02)."""
        assert (
            frozenset({JobState.DONE, JobState.ERROR, JobState.FALLBACK})
            == TERMINAL_STATES
        )

    def test_busy_states_is_derived_from_active_states(self) -> None:
        """BUSY_STATES is ACTIVE_STATES minus AWAITING_FLIP (CTR-01)."""
        assert ACTIVE_STATES - {JobState.AWAITING_FLIP} == BUSY_STATES

    def test_awaiting_flip_is_active_but_not_busy(self) -> None:
        """AWAITING_FLIP is in flight but the machine is idle then (CTR-01)."""
        assert JobState.AWAITING_FLIP in ACTIVE_STATES
        assert JobState.AWAITING_FLIP not in BUSY_STATES

    def test_busy_states_is_a_subset_of_active_states(self) -> None:
        """No state can be busy without also being active (CTR-01)."""
        assert BUSY_STATES <= ACTIVE_STATES


class TestStateLabel:
    """state_label short-label lookup tests."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (JobState.PENDING, "Pending"),
            (JobState.SCANNING, "Scanning"),
            (JobState.AWAITING_FLIP, "Waiting for flip"),
            (JobState.SCANNING_REVERSE, "Scanning backs"),
            (JobState.ASSEMBLING, "Assembling"),
            (JobState.UPLOADING, "Uploading"),
            (JobState.DONE, "Complete"),
            (JobState.ERROR, "Failed"),
            (JobState.FALLBACK, "Saved to folder"),
        ],
    )
    def test_state_label_strings(self, state: JobState, expected: str) -> None:
        """state_label returns the label the history table has always shown (CTR-01)."""
        assert state_label(state) == expected

    @pytest.mark.parametrize("state", list(JobState))
    def test_state_label_is_complete(self, state: JobState) -> None:
        """Every JobState has a label that is not just its raw value (CTR-01)."""
        label = state_label(state)
        assert label
        assert label != state.value


class TestProgressLabel:
    """progress_label progress-prose lookup tests."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (JobState.PENDING, "Starting scan..."),
            (JobState.SCANNING, "Scanning..."),
            (JobState.AWAITING_FLIP, "Awaiting flip..."),
            (JobState.SCANNING_REVERSE, "Scanning reverse sides..."),
            (JobState.ASSEMBLING, "Assembling PDF..."),
            (JobState.UPLOADING, "Uploading to paperless-ngx..."),
        ],
    )
    def test_progress_label_strings(self, state: JobState, expected: str) -> None:
        """progress_label returns the prose the status area has always shown (CTR-01)."""
        assert progress_label(state) == expected

    @pytest.mark.parametrize("state", list(JobState))
    def test_progress_label_is_complete(self, state: JobState) -> None:
        """Every JobState has progress prose that is not its raw value (CTR-01)."""
        label = progress_label(state)
        assert label
        assert label != state.value

    def test_terminal_states_have_progress_prose_for_totality(self) -> None:
        """The three terminal states carry prose purely to stay total (CTR-01)."""
        assert progress_label(JobState.DONE) == "Complete"
        assert progress_label(JobState.ERROR) == "Failed"
        assert progress_label(JobState.FALLBACK) == "Saved to folder"


class TestErrorMessage:
    """error_message category-message lookup tests."""

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_message_is_complete(self, category: ErrorCategory) -> None:
        """Every ErrorCategory has a plain-language message (CTR-05)."""
        message = error_message(category)
        assert message
        assert message != category.value

    def test_error_message_strings(self) -> None:
        """error_message returns developer-authored prose, not exception text (CTR-05)."""
        assert error_message(ErrorCategory.FEEDER) == (
            "The document feeder is empty or jammed."
        )
        assert error_message(ErrorCategory.CONFIG) == (
            "The saneless configuration is invalid."
        )
        assert error_message(ErrorCategory.SCANNER) == (
            "The scanner could not complete the scan."
        )
        assert error_message(ErrorCategory.UPLOAD) == (
            "The document could not be sent to paperless-ngx."
        )
        assert error_message(ErrorCategory.UNKNOWN) == "Something went wrong."


class TestConnectionStatus:
    """ConnectionStatus membership, wire-value and message tests."""

    def test_connection_status_has_exactly_five_members(self) -> None:
        """
        ConnectionStatus declares exactly five outcomes (OUTC-08).

        A count guard, not a name list: adding a member should fail the
        parametrised completeness test below -- which forces a user-facing
        message -- rather than a hand-written roster that only records what the
        enum happened to contain when it was written.
        """
        assert len(list(ConnectionStatus)) == 5

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (ConnectionStatus.CONNECTED, "connected"),
            (ConnectionStatus.TOKEN_REJECTED, "token_rejected"),
            (ConnectionStatus.NOT_FOUND, "not_found"),
            (ConnectionStatus.SERVER_ERROR, "server_error"),
            (ConnectionStatus.UNREACHABLE, "unreachable"),
        ],
    )
    def test_connection_status_wire_values(
        self,
        status: ConnectionStatus,
        expected: str,
    ) -> None:
        """
        Each member serialises to the string the web API documents (OUTC-08).

        Compared against plain str literals, not against other enum members:
        `GET /api/paperless/test` puts this value straight into a JSON body and
        `docs/reference/web-api.md` documents the exact spelling, so what this
        test has to prove is wire compatibility, not enum identity.
        """
        assert status == expected

    def test_legacy_wire_values_are_unchanged(self) -> None:
        """The three pre-existing API strings are byte-identical (OUTC-08)."""
        assert ConnectionStatus.CONNECTED == "connected"
        assert ConnectionStatus.TOKEN_REJECTED == "token_rejected"
        assert ConnectionStatus.UNREACHABLE == "unreachable"

    @pytest.mark.parametrize("status", list(ConnectionStatus))
    def test_json_round_trip_needs_no_custom_encoder(
        self,
        status: ConnectionStatus,
    ) -> None:
        """A member serialises as its bare string with the stdlib encoder (OUTC-08)."""
        assert json.dumps({"status": status}) == json.dumps({"status": status.value})

    def test_connected_serialises_to_the_documented_body(self) -> None:
        """The success body is exactly what web-api.md shows (OUTC-08)."""
        assert (
            json.dumps({"status": ConnectionStatus.CONNECTED})
            == '{"status": "connected"}'
        )

    @pytest.mark.parametrize("status", list(ConnectionStatus))
    def test_connection_status_message_is_complete(
        self,
        status: ConnectionStatus,
    ) -> None:
        """Every ConnectionStatus has a message that is not its raw value (OUTC-08)."""
        message = connection_status_message(status)
        assert message
        assert message != status.value

    def test_connection_status_message_strings(self) -> None:
        """connection_status_message returns developer-authored prose (OUTC-08)."""
        assert connection_status_message(ConnectionStatus.CONNECTED) == (
            "Connected to paperless-ngx."
        )
        assert connection_status_message(ConnectionStatus.TOKEN_REJECTED) == (
            "Paperless-ngx rejected the API token."
        )
        assert connection_status_message(ConnectionStatus.NOT_FOUND) == (
            "The paperless-ngx API was not found at that URL."
        )
        assert connection_status_message(ConnectionStatus.SERVER_ERROR) == (
            "Paperless-ngx returned a server error."
        )
        assert connection_status_message(ConnectionStatus.UNREACHABLE) == (
            "Could not reach paperless-ngx."
        )

    def test_connection_status_message_raises_on_unrecognised_value(self) -> None:
        """connection_status_message raises on a value outside the enum (OUTC-08)."""
        bad = cast("ConnectionStatus", "teapot")
        with pytest.raises(AssertionError):
            connection_status_message(bad)


class TestJobStateFor:
    """job_state_for outcome-to-state mapping tests."""

    @pytest.mark.parametrize("outcome", list(ScanOutcome))
    def test_job_state_for_is_total(self, outcome: ScanOutcome) -> None:
        """Every ScanOutcome maps to a state (OUTC-02)."""
        assert isinstance(job_state_for(outcome), JobState)

    @pytest.mark.parametrize("outcome", list(ScanOutcome))
    def test_job_state_for_always_lands_in_terminal_states(
        self,
        outcome: ScanOutcome,
    ) -> None:
        """
        An outcome always maps to a finished job (OUTC-02).

        A ScanOutcome only exists once the pipeline has resolved, so mapping one
        onto an ACTIVE_STATES member would mean the worker wrote "still in
        flight" over a job that is done.
        """
        assert job_state_for(outcome) in TERMINAL_STATES

    def test_success_maps_to_done(self) -> None:
        """SUCCESS is the ordinary finished job (OUTC-02)."""
        assert job_state_for(ScanOutcome.SUCCESS) is JobState.DONE

    def test_fallback_maps_to_fallback(self) -> None:
        """FALLBACK gets its own state rather than being folded into DONE (OUTC-02)."""
        assert job_state_for(ScanOutcome.FALLBACK) is JobState.FALLBACK

    def test_job_state_for_raises_on_unrecognised_value(self) -> None:
        """job_state_for raises on a value outside ScanOutcome (OUTC-02)."""
        bad = cast("ScanOutcome", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            job_state_for(bad)


class TestUnrecognisedValue:
    """Total-lookup fall-through behaviour tests."""

    def test_state_label_raises_on_unrecognised_value(self) -> None:
        """
        state_label raises rather than echoing an unknown value back (CTR-01).

        Raising is safe here because ``Job.state`` is only ever built through
        ``JobState(row[3])`` in ``JobStore.get_job`` and ``JobStore.list_recent``,
        which already rejects any value the enum does not name -- so an
        unrecognised value cannot reach a template.  That matters: a raising
        filter inside a Jinja render escapes ``TemplateResponse`` as a bare
        HTTP 500, and htmx 2 does not swap non-2xx bodies, so the observable
        failure would be the status area freezing silently while the one-second
        poll keeps hammering the server.  ``typing.assert_never`` raises
        ``AssertionError`` from a real ``raise`` statement, so ``python -O``
        does not strip the guard.
        """
        bad = cast("JobState", "UNKNOWN")
        with pytest.raises(AssertionError):
            state_label(bad)

    def test_progress_label_raises_on_unrecognised_value(self) -> None:
        """progress_label raises on a value outside JobState (CTR-01)."""
        bad = cast("JobState", "UNKNOWN")
        with pytest.raises(AssertionError):
            progress_label(bad)

    def test_error_message_raises_on_unrecognised_value(self) -> None:
        """error_message raises on a value outside ErrorCategory (CTR-05)."""
        bad = cast("ErrorCategory", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            error_message(bad)


class TestClassifyError:
    """classify_error exception-to-category mapping tests."""

    def test_feeder_empty_error_is_feeder(self) -> None:
        """FeederEmptyError classifies as FEEDER (CTR-05)."""
        assert classify_error(FeederEmptyError("no paper")) is ErrorCategory.FEEDER

    def test_config_error_is_config(self) -> None:
        """ConfigError classifies as CONFIG (CTR-05)."""
        assert classify_error(ConfigError("bad toml")) is ErrorCategory.CONFIG

    def test_scan_error_is_scanner(self) -> None:
        """ScanError classifies as SCANNER (CTR-05)."""
        assert classify_error(ScanError("device busy")) is ErrorCategory.SCANNER

    def test_paperless_error_is_upload(self) -> None:
        """PaperlessError classifies as UPLOAD (CTR-05)."""
        assert classify_error(PaperlessError("http 500")) is ErrorCategory.UPLOAD

    def test_unrelated_exception_is_unknown(self) -> None:
        """An exception outside the saneless hierarchy is UNKNOWN (CTR-05)."""
        assert classify_error(ValueError("who knows")) is ErrorCategory.UNKNOWN

    def test_paperless_timeout_error_subclasses_paperless_error(self) -> None:
        """PaperlessTimeoutError narrows PaperlessError rather than SanelessError (OUTC-08)."""
        assert issubclass(PaperlessTimeoutError, PaperlessError)

    def test_paperless_timeout_error_is_upload(self) -> None:
        """
        PaperlessTimeoutError classifies as UPLOAD with no new arm (OUTC-08).

        The point of subclassing PaperlessError is that the existing
        ``isinstance(exc, PaperlessError)`` check already covers it: adding an
        arm for the subclass would be dead code, and forgetting one would be a
        silent reclassification to UNKNOWN.
        """
        assert (
            classify_error(PaperlessTimeoutError("timed out")) is ErrorCategory.UPLOAD
        )

    def test_feeder_empty_wins_over_its_scan_error_base(self) -> None:
        """FeederEmptyError is checked before its ScanError base class (CTR-05)."""
        assert issubclass(FeederEmptyError, ScanError)
        assert classify_error(FeederEmptyError("no paper")) is ErrorCategory.FEEDER


class TestJobModuleReExports:
    """saneless.job re-export tests."""

    def test_job_module_re_exports_the_same_job_state(self) -> None:
        """saneless.job.JobState is the vocabulary object, not a copy (CTR-01)."""
        assert saneless.job.JobState is JobState

    def test_job_module_re_exports_the_same_error_category(self) -> None:
        """saneless.job.ErrorCategory is the vocabulary object, not a copy (CTR-05)."""
        assert saneless.job.ErrorCategory is ErrorCategory

    def test_existing_from_import_still_resolves(self) -> None:
        """The long-standing `from saneless.job import ...` spelling still works (CTR-01)."""
        assert JobErrorCategory is ErrorCategory
        assert JobJobState is JobState

    def test_job_module_declares_no_enum_of_its_own(self) -> None:
        """job.py owns no enum definition any more (CTR-01)."""
        assert JobState.__module__ == "saneless.vocabulary"
        assert ErrorCategory.__module__ == "saneless.vocabulary"


class TestJobActivityProperties:
    """Job.is_active / Job.is_busy tests."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (JobState.PENDING, (True, True)),
            (JobState.SCANNING, (True, True)),
            (JobState.AWAITING_FLIP, (True, False)),
            (JobState.ASSEMBLING, (True, True)),
            (JobState.UPLOADING, (True, True)),
            (JobState.DONE, (False, False)),
            (JobState.ERROR, (False, False)),
            (JobState.FALLBACK, (False, False)),
        ],
    )
    def test_job_reports_activity(
        self,
        state: JobState,
        expected: tuple[bool, bool],
    ) -> None:
        """A Job answers is_active/is_busy for every lifecycle state (CTR-01)."""
        job = Job(id="j", profile="default", title="t", state=state)
        assert (job.is_active, job.is_busy) == expected

    def test_awaiting_flip_is_active_but_not_busy(self) -> None:
        """A flip prompt leaves the job in flight while the machine idles (CTR-01)."""
        job = Job(id="j", profile="default", title="t", state=JobState.AWAITING_FLIP)
        assert job.is_active
        assert not job.is_busy

    def test_done_is_neither_active_nor_busy(self) -> None:
        """A finished job is neither in flight nor working (CTR-01)."""
        job = Job(id="j", profile="default", title="t", state=JobState.DONE)
        assert not job.is_active
        assert not job.is_busy

    def test_properties_are_not_dataclass_fields(self) -> None:
        """is_active/is_busy are properties, so they stay out of __init__ (CTR-01)."""
        field_names = {f.name for f in fields(Job)}
        assert "is_active" not in field_names
        assert "is_busy" not in field_names
