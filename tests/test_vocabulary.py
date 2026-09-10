"""
Tests for the shared saneless vocabulary module.

Covers requirements: CTR-01, CTR-02, CTR-05.
"""

from __future__ import annotations

import pytest

from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    TERMINAL_STATES,
    ErrorCategory,
    JobState,
    ScanOutcome,
)


class TestJobStateMembers:
    """JobState membership tests."""

    def test_job_state_has_exactly_seven_members(self) -> None:
        """JobState declares exactly seven lifecycle members (CTR-01)."""
        assert len(list(JobState)) == 7

    def test_job_state_member_names(self) -> None:
        """JobState names are the seven documented lifecycle states (CTR-01)."""
        assert [state.name for state in JobState] == [
            "PENDING",
            "SCANNING",
            "AWAITING_FLIP",
            "ASSEMBLING",
            "UPLOADING",
            "DONE",
            "ERROR",
        ]

    def test_job_state_has_no_future_phase_members(self) -> None:
        """JobState does not yet carry FALLBACK or SCANNING_REVERSE (CTR-01)."""
        names = {state.name for state in JobState}
        assert "FALLBACK" not in names
        assert "SCANNING_REVERSE" not in names

    @pytest.mark.parametrize("state", list(JobState))
    def test_job_state_value_equals_name(self, state: JobState) -> None:
        """Every JobState value is identical to its member name (CTR-01)."""
        assert state.value == state.name


class TestErrorCategoryMembers:
    """ErrorCategory membership tests."""

    def test_error_category_member_names(self) -> None:
        """ErrorCategory names are the five documented categories (CTR-05)."""
        assert [category.name for category in ErrorCategory] == [
            "FEEDER",
            "CONFIG",
            "SCANNER",
            "UPLOAD",
            "UNKNOWN",
        ]

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_category_value_equals_name(self, category: ErrorCategory) -> None:
        """Every ErrorCategory value is identical to its member name (CTR-05)."""
        assert category.value == category.name


class TestScanOutcomeMembers:
    """ScanOutcome membership tests."""

    def test_scan_outcome_has_exactly_two_members(self) -> None:
        """ScanOutcome is exactly SUCCESS and FALLBACK (CTR-02)."""
        assert [outcome.name for outcome in ScanOutcome] == ["SUCCESS", "FALLBACK"]

    def test_scan_outcome_has_no_failed_member(self) -> None:
        """ScanOutcome carries no FAILED member in this phase (CTR-02)."""
        assert "FAILED" not in {outcome.name for outcome in ScanOutcome}

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
        """ACTIVE_STATES is the five in-flight lifecycle states (CTR-01)."""
        assert (
            frozenset(
                {
                    JobState.PENDING,
                    JobState.SCANNING,
                    JobState.AWAITING_FLIP,
                    JobState.ASSEMBLING,
                    JobState.UPLOADING,
                }
            )
            == ACTIVE_STATES
        )

    def test_terminal_states_membership(self) -> None:
        """TERMINAL_STATES is exactly DONE and ERROR (CTR-01)."""
        assert frozenset({JobState.DONE, JobState.ERROR}) == TERMINAL_STATES

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
