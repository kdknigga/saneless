"""
Tests for the shared saneless vocabulary module.

Covers requirements: CTR-01, CTR-02, CTR-05, ROBU-01, ROBU-02, ROBU-08.
"""

from __future__ import annotations

import json
import time
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import pytest

import saneless.job
from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    PaperlessTimeoutError,
    PdfError,
    ScanCancelledError,
    ScanError,
    StorageError,
)
from saneless.job import ErrorCategory as JobErrorCategory
from saneless.job import Job
from saneless.job import JobState as JobJobState
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    LOCAL_TIME_FORMAT,
    QUEUE_FULL_JOB_ERROR,
    RESTART_REASON,
    TERMINAL_STATES,
    TITLE_MAX_LENGTH,
    TOKEN_UNSET_JOB_ERROR,
    WORKER_DEGRADED_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    ConnectionStatus,
    ErrorAdvice,
    ErrorCategory,
    ExitCode,
    FlipOutcome,
    JobState,
    RequestRejection,
    ScanOutcome,
    SubmitResult,
    WorkerHealth,
    busy_line,
    classify_error,
    connection_status_message,
    error_advice,
    error_message,
    error_next_step,
    exit_code_for,
    flip_answer_label,
    job_state_for,
    local_time,
    page_counts,
    progress_label,
    rejection_message,
    rejection_status_code,
    state_label,
    worker_health_detail,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class TestJobStateMembers:
    """JobState membership tests."""

    def test_job_state_has_exactly_ten_members(self) -> None:
        """
        JobState declares exactly ten lifecycle members (CTR-01, DPLX-06, D-01).

        A count guard, not a name list: adding a member should fail the
        parametrised completeness tests below -- which force a label and a
        classification decision -- rather than a hand-written roster that only
        records what the enum happened to contain when it was written.
        """
        assert len(list(JobState)) == 10

    @pytest.mark.parametrize("state", list(JobState))
    def test_job_state_value_equals_name(self, state: JobState) -> None:
        """Every JobState value is identical to its member name (CTR-01)."""
        assert state.value == state.name


class TestErrorCategoryMembers:
    """ErrorCategory membership tests."""

    def test_error_category_member_names(self) -> None:
        """
        ErrorCategory names are the documented categories (CTR-05, D-06).

        The documented categories include REJECTED for a submit that never
        ran (D-06) and ASSEMBLY for a PDF that could not be built (D-04).
        Compared as a set: declaration order is not part of any contract, and
        pinning it would fail a harmless reordering.
        """
        assert {category.name for category in ErrorCategory} == {
            "FEEDER",
            "CONFIG",
            "SCANNER",
            "UPLOAD",
            "UNKNOWN",
            "REJECTED",
            "ASSEMBLY",
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
        """TERMINAL_STATES is DONE, ERROR, FALLBACK and CANCELLED (OUTC-02, D-01)."""
        assert (
            frozenset(
                {
                    JobState.DONE,
                    JobState.ERROR,
                    JobState.FALLBACK,
                    JobState.CANCELLED,
                }
            )
            == TERMINAL_STATES
        )

    def test_cancelled_is_not_busy(self) -> None:
        """A cancelled job is finished, so the machine is not working (D-01)."""
        assert JobState.CANCELLED not in BUSY_STATES
        assert JobState.CANCELLED.value == "CANCELLED"

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
            (JobState.CANCELLED, "Cancelled"),
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
        """The four terminal states carry prose purely to stay total (CTR-01)."""
        assert progress_label(JobState.DONE) == "Complete"
        assert progress_label(JobState.ERROR) == "Failed"
        assert progress_label(JobState.FALLBACK) == "Saved to folder"
        assert progress_label(JobState.CANCELLED) == "Cancelled"


class TestFlipAnswerLabel:
    """flip_answer_label acknowledgment-copy lookup tests."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (FlipOutcome.CONTINUED, "Flip confirmed. Scanning reverse sides next..."),
            (FlipOutcome.ABORTED, "Aborting scan..."),
            (FlipOutcome.TIMED_OUT, "Flip wait timed out..."),
        ],
    )
    def test_flip_answer_label_strings(
        self, outcome: FlipOutcome, expected: str
    ) -> None:
        """flip_answer_label returns the acknowledgment copy (DPLX-06, CR-01)."""
        assert flip_answer_label(outcome) == expected

    @pytest.mark.parametrize("outcome", list(FlipOutcome))
    def test_flip_answer_label_is_complete(self, outcome: FlipOutcome) -> None:
        """Every FlipOutcome has acknowledgment prose ending in ASCII dots (CR-01)."""
        label = flip_answer_label(outcome)
        assert label
        assert label != outcome.value
        assert label.endswith("...")
        assert "…" not in label


class TestErrorAdvice:
    """error_advice category-to-advice lookup tests (APPL-04, D-10, D-11)."""

    def test_error_advice_fields(self) -> None:
        """ErrorAdvice carries exactly a message and a next step (APPL-04)."""
        assert [field.name for field in fields(ErrorAdvice)] == [
            "message",
            "next_step",
        ]

    def test_error_advice_is_immutable(self) -> None:
        """
        ErrorAdvice is frozen, so a renderer cannot edit the approved copy.

        The attribute name is a local rather than a literal so this exercises
        the dataclass's own runtime guard rather than a linter's rule about
        constant ``setattr`` targets.
        """
        advice = error_advice(ErrorCategory.FEEDER)
        attribute = "message"
        with pytest.raises(FrozenInstanceError):
            setattr(advice, attribute, "tampered")

    def test_error_advice_is_slotted(self) -> None:
        """ErrorAdvice is slotted, so a typo cannot add a silent third field."""
        assert not hasattr(error_advice(ErrorCategory.FEEDER), "__dict__")

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_advice_is_complete(self, category: ErrorCategory) -> None:
        """Every ErrorCategory has a message and a next step (APPL-04, CTR-05)."""
        advice = error_advice(category)
        assert advice.message
        assert advice.next_step
        assert advice.message != category.value
        assert advice.next_step != category.value
        assert advice.message.endswith(".")
        assert advice.next_step.endswith(".")

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_advice_accessors_agree(self, category: ErrorCategory) -> None:
        """
        error_message and error_next_step read the one lookup (D-10, D-11).

        There is exactly one ``match`` over ErrorCategory in the module and the
        two accessors are one-liners over it, so the pair cannot drift apart
        the way two parallel lookups would.
        """
        advice = error_advice(category)
        assert error_message(category) == advice.message
        assert error_next_step(category) == advice.next_step

    def test_error_advice_raises_on_unrecognised_value(self) -> None:
        """error_advice raises on a value outside ErrorCategory (CTR-05)."""
        bad = cast("ErrorCategory", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            error_advice(bad)

    @pytest.mark.parametrize(
        ("category", "expected"),
        [
            (
                ErrorCategory.FEEDER,
                "Load the pages squarely in the feeder, clear any jam, then "
                "start the scan again.",
            ),
            (
                ErrorCategory.CONFIG,
                "Correct the saneless configuration file, then restart saneless.",
            ),
            (
                ErrorCategory.SCANNER,
                "Check the scanner is switched on and connected, then start the "
                "scan again.",
            ),
            (
                ErrorCategory.UPLOAD,
                "Check paperless-ngx is running and the API token is correct, "
                "then start the scan again.",
            ),
            (
                ErrorCategory.ASSEMBLY,
                "Start the scan again. If it keeps failing, check the server's "
                "free disk space.",
            ),
            (
                ErrorCategory.REJECTED,
                "Check the system status list for anything marked Failed, then "
                "start the scan again.",
            ),
            (
                ErrorCategory.UNKNOWN,
                "Start the scan again. If it keeps failing, check the saneless log.",
            ),
        ],
    )
    def test_error_next_step_strings(
        self, category: ErrorCategory, expected: str
    ) -> None:
        """The seven next steps are the approved UI-SPEC S2 copy (APPL-04)."""
        assert error_next_step(category) == expected

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_next_step_is_surface_neutral(self, category: ErrorCategory) -> None:
        """
        No next step names a surface, because both surfaces render it (D-12).

        The web page has no command line and the CLI has no Scan button, so a
        string naming either would be wrong on the other.
        """
        next_step = error_next_step(category).lower()
        assert "press scan" not in next_step
        assert "click" not in next_step
        assert "run the command" not in next_step

    def test_error_message_strings(self) -> None:
        """The messages are byte identical to what shipped before (APPL-04, D-10)."""
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
        assert error_message(ErrorCategory.ASSEMBLY) == (
            "The scanned pages could not be assembled into a PDF."
        )

    def test_rejected_error_message(self) -> None:
        """REJECTED explains that the scan never started (D-05, D-06)."""
        assert error_message(ErrorCategory.REJECTED) == (
            "This scan was not started. Check that saneless is ready to scan, "
            "then try again."
        )


class TestTokenUnsetRejection:
    """RequestRejection.TOKEN_UNSET vocabulary tests (APPL-07, D-15)."""

    def test_token_unset_member_exists(self) -> None:
        """TOKEN_UNSET is a RequestRejection member of its own (D-15)."""
        assert RequestRejection.TOKEN_UNSET.value == "TOKEN_UNSET"
        assert RequestRejection.TOKEN_UNSET.name == "TOKEN_UNSET"

    def test_token_unset_is_service_unavailable(self) -> None:
        """An unset token refuses with 503, beside the other not-ready arms."""
        assert rejection_status_code(RequestRejection.TOKEN_UNSET) == 503

    def test_token_unset_message(self) -> None:
        """The TOKEN_UNSET sentence is the approved UI-SPEC S8 copy (APPL-07)."""
        assert rejection_message(RequestRejection.TOKEN_UNSET) == (
            "The paperless-ngx API token has not been set, so the scan was not "
            "started. Put a real API token in the saneless config file, then "
            "restart saneless."
        )

    def test_token_unset_does_not_reuse_worker_degraded_copy(self) -> None:
        """
        WORKER_DEGRADED is deliberately not reused for an unset token (D-15).

        "The scan service was unavailable" is untrue when the truth is that
        nobody ever set the token, so the two carry different words.
        """
        assert rejection_message(RequestRejection.TOKEN_UNSET) != rejection_message(
            RequestRejection.WORKER_DEGRADED
        )
        assert TOKEN_UNSET_JOB_ERROR != WORKER_DEGRADED_JOB_ERROR

    def test_token_unset_job_error(self) -> None:
        """The job-row error carries no trailing period, like its siblings (D-05)."""
        assert TOKEN_UNSET_JOB_ERROR == (
            "Not started: the paperless-ngx API token has not been set"
        )
        assert not TOKEN_UNSET_JOB_ERROR.endswith(".")


class TestDeveloperConstantStrings:
    """Every user-facing string in this module is a developer constant (V7)."""

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_message_carries_no_internals(
        self, rejection: RequestRejection
    ) -> None:
        """No rejection message can carry input, a URL or exception text (V7)."""
        message = rejection_message(rejection)
        assert "{" not in message
        assert "%s" not in message
        assert "http" not in message.lower()
        assert "traceback" not in message.lower()

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_advice_carries_no_internals(self, category: ErrorCategory) -> None:
        """No ErrorAdvice field can carry input, a URL or exception text (V7)."""
        advice = error_advice(category)
        for text in (advice.message, advice.next_step):
            assert "{" not in text
            assert "%s" not in text
            assert "http" not in text.lower()
            assert "traceback" not in text.lower()


@pytest.fixture
def local_zone(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """
    Give one test control of the process's local zone, then restore it.

    ``local_time`` renders whatever zone the C library reports, so pinning
    ``TZ`` and calling ``time.tzset()`` is the only way to assert an exact
    string on a host in an unknown zone.  monkeypatch removes the env var at
    teardown; the trailing ``tzset`` is what makes the C library notice.
    """

    def _use(zone: str) -> None:
        monkeypatch.setenv("TZ", zone)
        time.tzset()

    yield _use
    time.tzset()


class TestLocalTime:
    """local_time shared timestamp formatter tests (APPL-12, D-34, D-35)."""

    def test_local_time_format_is_the_one_shared_format(self) -> None:
        """The web filter and the CLI table read one format constant (D-35)."""
        assert LOCAL_TIME_FORMAT == "%Y-%m-%d %H:%M %Z"

    def test_local_time_renders_the_servers_zone(
        self, local_zone: Callable[[str], None]
    ) -> None:
        """A UTC instant renders in the server's local zone, named (D-34)."""
        local_zone("America/Chicago")
        assert local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC)) == (
            "2026-09-16 14:03 CDT"
        )

    def test_local_time_names_utc_when_the_server_is_utc(
        self, local_zone: Callable[[str], None]
    ) -> None:
        """A UTC server still gets the zone named on the line (D-35)."""
        local_zone("UTC")
        assert local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC)) == (
            "2026-09-16 19:03 UTC"
        )

    def test_local_time_ignores_the_values_own_zone(
        self, local_zone: Callable[[str], None]
    ) -> None:
        """
        A value carrying another zone still renders the server's (D-34).

        ``astimezone()`` is called with no argument, so the answer is the
        process's zone whatever the argument's tzinfo happens to be.
        """
        local_zone("America/Chicago")
        tokyo = timezone(timedelta(hours=9))
        assert local_time(datetime(2026, 9, 16, 19, 3, tzinfo=tokyo)) == (
            "2026-09-16 05:03 CDT"
        )


class TestPageCounts:
    """page_counts sentence tests (APPL-03, D-32)."""

    def test_page_counts_sentence(self) -> None:
        """The three counts render as one sentence (APPL-03)."""
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            pages_scanned=12,
            pages_removed=2,
            pages_uploaded=10,
        )
        assert page_counts(job) == "12 pages scanned, 2 blank removed, 10 uploaded"

    def test_page_counts_pluralises_only_the_first_clause(self) -> None:
        """Only the scanned clause carries a noun, so only it pluralises."""
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            pages_scanned=1,
            pages_removed=0,
            pages_uploaded=1,
        )
        assert page_counts(job) == "1 page scanned, 0 blank removed, 1 uploaded"

    def test_page_counts_renders_a_measured_zero(self) -> None:
        """
        A measured 0 is a measurement and renders as 0 (D-32, Pitfall 4).

        D-32's "never render 0" is about NULL.  A scan where nothing was blank
        really did remove 0 pages, and saying so is the truth.
        """
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            pages_scanned=0,
            pages_removed=0,
            pages_uploaded=0,
        )
        assert page_counts(job) == "0 pages scanned, 0 blank removed, 0 uploaded"

    @pytest.mark.parametrize(
        "missing", ["pages_scanned", "pages_removed", "pages_uploaded"]
    )
    def test_page_counts_is_none_when_any_count_is_null(self, missing: str) -> None:
        """One NULL count means nothing at all is rendered (D-32)."""
        counts = {"pages_scanned": 12, "pages_removed": 2, "pages_uploaded": 10}
        counts[missing] = None
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            **counts,
        )
        assert page_counts(job) is None

    def test_page_counts_is_none_for_an_uncounted_job(self) -> None:
        """An ERROR or pre-Phase-23 row has no counts, so renders none (D-32)."""
        job = Job(id="j", profile="default", title="t", state=JobState.ERROR)
        assert page_counts(job) is None


class TestBusyLine:
    """busy_line in-progress line tests (APPL-08, D-25, D-33)."""

    @pytest.mark.parametrize("state", sorted(BUSY_STATES))
    def test_busy_line_falls_back_to_progress_label(self, state: JobState) -> None:
        """With nothing extra known, the busy line is today's prose (D-33)."""
        assert busy_line(state) == progress_label(state)

    def test_busy_line_names_the_job_ahead(self) -> None:
        """A queued job is told what it is waiting for (APPL-08, D-25)."""
        assert busy_line(JobState.PENDING, queue_title="Tax return", queue_ahead=1) == (
            "Waiting for 'Tax return' to finish (1 ahead of you)"
        )

    def test_busy_line_counts_more_than_one_ahead(self) -> None:
        """The count is the number of jobs ahead, not a fixed word (APPL-08)."""
        assert busy_line(JobState.PENDING, queue_title="Tax return", queue_ahead=2) == (
            "Waiting for 'Tax return' to finish (2 ahead of you)"
        )

    def test_busy_line_says_next_in_line_instead_of_zero_ahead(self) -> None:
        """
        "(0 ahead of you)" is never rendered (D-25).

        It is technically true and reads like a bug.
        """
        assert busy_line(JobState.PENDING, queue_title="Tax return", queue_ahead=0) == (
            "Waiting for 'Tax return' to finish (next in line)"
        )

    def test_busy_line_needs_both_halves_of_the_queue_position(self) -> None:
        """A title with no count is not enough to claim a position (D-25)."""
        assert busy_line(JobState.PENDING, queue_title="Tax return") == progress_label(
            JobState.PENDING
        )

    def test_busy_line_shows_the_front_count(self) -> None:
        """Pass B names how many fronts are already scanned (D-33, APPL-03)."""
        assert busy_line(JobState.SCANNING_REVERSE, front_pages=12) == (
            "Front: 12 pages · " + progress_label(JobState.SCANNING_REVERSE)
        )

    def test_busy_line_pluralises_the_front_count(self) -> None:
        """One front page is a page, not pages (D-33)."""
        assert busy_line(JobState.SCANNING_REVERSE, front_pages=1) == (
            "Front: 1 page · " + progress_label(JobState.SCANNING_REVERSE)
        )

    def test_busy_line_omits_an_unknown_front_count(self) -> None:
        """An unknown front count renders no count at all (D-33)."""
        assert busy_line(JobState.SCANNING_REVERSE, front_pages=None) == (
            progress_label(JobState.SCANNING_REVERSE)
        )

    def test_busy_line_shows_the_front_count_only_on_pass_b(self) -> None:
        """The front count belongs to SCANNING_REVERSE and no other state."""
        assert busy_line(JobState.SCANNING, front_pages=12) == progress_label(
            JobState.SCANNING
        )

    def test_busy_line_queue_position_wins_over_the_front_count(self) -> None:
        """The queue line is the first branch, whatever else is known (D-33)."""
        assert busy_line(
            JobState.SCANNING_REVERSE,
            front_pages=12,
            queue_title="Tax return",
            queue_ahead=2,
        ) == ("Waiting for 'Tax return' to finish (2 ahead of you)")


class TestWorkerHealth:
    """WorkerHealth membership and /health detail tests."""

    def test_worker_health_members(self) -> None:
        """WorkerHealth is exactly HEALTHY, DEGRADED and DOWN (ROBU-01)."""
        assert [health.value for health in WorkerHealth] == [
            "HEALTHY",
            "DEGRADED",
            "DOWN",
        ]

    @pytest.mark.parametrize("health", list(WorkerHealth))
    def test_worker_health_value_equals_name(self, health: WorkerHealth) -> None:
        """Every WorkerHealth value is identical to its member name (ROBU-01)."""
        assert health.value == health.name

    @pytest.mark.parametrize(
        ("health", "expected"),
        [
            (WorkerHealth.HEALTHY, "ok"),
            (WorkerHealth.DEGRADED, "job store failing"),
            (WorkerHealth.DOWN, "worker thread is down"),
        ],
    )
    def test_worker_health_detail_strings(
        self, health: WorkerHealth, expected: str
    ) -> None:
        """worker_health_detail returns the documented /health detail (ROBU-01)."""
        assert worker_health_detail(health) == expected

    @pytest.mark.parametrize("health", list(WorkerHealth))
    def test_worker_health_detail_is_complete(self, health: WorkerHealth) -> None:
        """Every WorkerHealth has a non-empty detail string (ROBU-01)."""
        assert worker_health_detail(health)

    def test_worker_health_detail_raises_on_unrecognised_value(self) -> None:
        """worker_health_detail raises on a value outside WorkerHealth (ROBU-01)."""
        bad = cast("WorkerHealth", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            worker_health_detail(bad)


class TestSubmitResult:
    """SubmitResult membership tests."""

    def test_submit_result_members(self) -> None:
        """SubmitResult is exactly ACCEPTED, QUEUE_FULL, DOWN, DEGRADED (ROBU-02)."""
        assert [result.value for result in SubmitResult] == [
            "ACCEPTED",
            "QUEUE_FULL",
            "DOWN",
            "DEGRADED",
        ]

    @pytest.mark.parametrize("result", list(SubmitResult))
    def test_submit_result_value_equals_name(self, result: SubmitResult) -> None:
        """Every SubmitResult value is identical to its member name (ROBU-02)."""
        assert result.value == result.name


_REJECTION_MESSAGES: list[tuple[RequestRejection, str]] = [
    (
        RequestRejection.QUEUE_FULL,
        "The scan queue is full. Wait for a scan to finish, then try again.",
    ),
    (
        RequestRejection.WORKER_DOWN,
        "The scan service is not running, so the scan was not started. "
        "Restart saneless, then try again.",
    ),
    (
        RequestRejection.WORKER_DEGRADED,
        "Job history cannot be saved right now, so the scan was not started. "
        "Check the server's free disk space and log, then try again.",
    ),
    (
        RequestRejection.TOKEN_UNSET,
        "The paperless-ngx API token has not been set, so the scan was not "
        "started. Put a real API token in the saneless config file, then "
        "restart saneless.",
    ),
    (
        RequestRejection.UNKNOWN_PROFILE,
        "That scan profile does not exist. Reload the page to see the current "
        "profiles.",
    ),
    (
        RequestRejection.TITLE_TOO_LONG,
        "The title is too long. Shorten it to 256 characters or fewer.",
    ),
    (
        RequestRejection.INVALID_REQUEST,
        "The request was not valid. Reload the page, then try again.",
    ),
    (
        RequestRejection.CROSS_SITE,
        "This request was blocked because it did not come from the saneless "
        "page. If saneless is behind a reverse proxy, make sure the proxy passes "
        "the original Host header.",
    ),
    (
        RequestRejection.NOT_FOUND,
        "That page or action does not exist. Reload the page, then try again.",
    ),
    (
        RequestRejection.METHOD_NOT_ALLOWED,
        "That action is not allowed. Reload the page, then try again.",
    ),
    (
        RequestRejection.INTERNAL,
        "Something went wrong on the server. Check the server log for details, "
        "then try again.",
    ),
    (
        RequestRejection.CLIENT_ERROR,
        "The request could not be completed. Reload the page, then try again.",
    ),
]

_REJECTION_STATUS_CODES: list[tuple[RequestRejection, int]] = [
    (RequestRejection.QUEUE_FULL, 429),
    (RequestRejection.WORKER_DOWN, 503),
    (RequestRejection.WORKER_DEGRADED, 503),
    (RequestRejection.TOKEN_UNSET, 503),
    (RequestRejection.UNKNOWN_PROFILE, 422),
    (RequestRejection.TITLE_TOO_LONG, 422),
    (RequestRejection.INVALID_REQUEST, 422),
    (RequestRejection.CROSS_SITE, 403),
    (RequestRejection.NOT_FOUND, 404),
    (RequestRejection.METHOD_NOT_ALLOWED, 405),
    (RequestRejection.INTERNAL, 500),
    (RequestRejection.CLIENT_ERROR, 400),
]

_JOB_ROW_TEXTS: list[str] = [
    QUEUE_FULL_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    WORKER_DEGRADED_JOB_ERROR,
    TOKEN_UNSET_JOB_ERROR,
    RESTART_REASON,
]


class TestRequestRejection:
    """RequestRejection membership, message, status code and job-row copy tests."""

    def test_request_rejection_members(self) -> None:
        """RequestRejection names one member per rendered error (D-05, ROBU-02)."""
        assert {rejection.name for rejection in RequestRejection} == {
            "QUEUE_FULL",
            "WORKER_DOWN",
            "WORKER_DEGRADED",
            "TOKEN_UNSET",
            "UNKNOWN_PROFILE",
            "TITLE_TOO_LONG",
            "INVALID_REQUEST",
            "CROSS_SITE",
            "NOT_FOUND",
            "METHOD_NOT_ALLOWED",
            "INTERNAL",
            "CLIENT_ERROR",
        }

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_request_rejection_value_equals_name(
        self, rejection: RequestRejection
    ) -> None:
        """Every RequestRejection value is identical to its member name (D-05)."""
        assert rejection.value == rejection.name

    def test_message_table_covers_every_member(self) -> None:
        """The pinned message table names every RequestRejection member (D-05)."""
        assert {rejection for rejection, _ in _REJECTION_MESSAGES} == set(
            RequestRejection
        )

    def test_status_code_table_covers_every_member(self) -> None:
        """The pinned status-code table names every RequestRejection member (D-05)."""
        assert {rejection for rejection, _ in _REJECTION_STATUS_CODES} == set(
            RequestRejection
        )

    @pytest.mark.parametrize(("rejection", "expected"), _REJECTION_MESSAGES)
    def test_rejection_message_strings(
        self, rejection: RequestRejection, expected: str
    ) -> None:
        """rejection_message returns the approved S3 copy verbatim (D-05, ROBU-02)."""
        assert rejection_message(rejection) == expected

    @pytest.mark.parametrize(("rejection", "expected"), _REJECTION_STATUS_CODES)
    def test_rejection_status_codes(
        self, rejection: RequestRejection, expected: int
    ) -> None:
        """rejection_status_code returns the documented HTTP status (ROBU-02)."""
        assert rejection_status_code(rejection) == expected

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_message_is_complete(self, rejection: RequestRejection) -> None:
        """Every RequestRejection has a non-empty message (D-05)."""
        assert rejection_message(rejection)

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_status_code_is_complete(
        self, rejection: RequestRejection
    ) -> None:
        """Every RequestRejection has an HTTP error status (ROBU-02)."""
        assert 400 <= rejection_status_code(rejection) <= 599

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_message_style(self, rejection: RequestRejection) -> None:
        """Every rejection message ends with a period and has no "!" (S3 style)."""
        message = rejection_message(rejection)
        assert message.endswith(".")
        assert "!" not in message

    def test_rejection_message_raises_on_unrecognised_value(self) -> None:
        """rejection_message raises on a value outside RequestRejection (D-05)."""
        bad = cast("RequestRejection", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            rejection_message(bad)

    def test_rejection_status_code_raises_on_unrecognised_value(self) -> None:
        """rejection_status_code raises on a value outside RequestRejection (D-05)."""
        bad = cast("RequestRejection", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            rejection_status_code(bad)

    def test_title_max_length(self) -> None:
        """The title cap is 256 characters (ROBU-08)."""
        assert TITLE_MAX_LENGTH == 256

    def test_title_too_long_message_reads_the_cap(self) -> None:
        """The TITLE_TOO_LONG message names the same cap the form enforces (ROBU-08)."""
        assert str(TITLE_MAX_LENGTH) in rejection_message(
            RequestRejection.TITLE_TOO_LONG
        )

    def test_job_row_texts(self) -> None:
        """The rejected-row texts and the restart reason are verbatim S3 (D-05)."""
        assert QUEUE_FULL_JOB_ERROR == "Not started: the scan queue was full"
        assert WORKER_DOWN_JOB_ERROR == "Not started: the scan service was not running"
        assert WORKER_DEGRADED_JOB_ERROR == (
            "Not started: the scan service was unavailable"
        )
        assert RESTART_REASON == "The server restarted before this scan finished"

    @pytest.mark.parametrize("text", _JOB_ROW_TEXTS)
    def test_job_row_texts_have_no_trailing_period(self, text: str) -> None:
        """Job-row texts follow the job.error convention of no trailing period (D-05)."""
        assert text
        assert not text.endswith(".")


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

    def test_classify_error_pdf_error_is_assembly(self) -> None:
        """PdfError classifies as ASSEMBLY, never as SCANNER (EXC-01, D-04)."""
        assert classify_error(PdfError("disk full")) is ErrorCategory.ASSEMBLY

    def test_classify_error_scan_cancelled_error_is_unknown(self) -> None:
        """
        ScanCancelledError has no category of its own (D-01).

        A cancel is not a failure category: callers test for it before they
        classify, so reaching ``classify_error`` with one is already a bug and
        UNKNOWN is the honest answer.
        """
        assert classify_error(ScanCancelledError("stopped")) is ErrorCategory.UNKNOWN

    def test_classify_error_storage_error_is_unknown(self) -> None:
        """
        StorageError stays UNKNOWN; its exit code 2 is assigned by type (D-07).

        ``ErrorCategory`` is persisted on job records, and a job database that
        cannot be opened is not any job's configuration failure.  The CLI guard
        maps StorageError to ``ExitCode.CONFIG`` by exception type, so this pin
        stops anyone "fixing" the exit code by reclassifying it.
        """
        assert classify_error(StorageError("bad db")) is ErrorCategory.UNKNOWN


_EXIT_CODES_FOR_CATEGORIES: list[tuple[ErrorCategory, ExitCode]] = [
    (ErrorCategory.FEEDER, ExitCode.SCAN),
    (ErrorCategory.SCANNER, ExitCode.SCAN),
    (ErrorCategory.CONFIG, ExitCode.CONFIG),
    (ErrorCategory.UPLOAD, ExitCode.PAPERLESS),
    (ErrorCategory.ASSEMBLY, ExitCode.PDF),
    (ErrorCategory.UNKNOWN, ExitCode.UNEXPECTED),
    (ErrorCategory.REJECTED, ExitCode.UNEXPECTED),
]


class TestExitCode:
    """ExitCode definition and exit_code_for mapping tests."""

    def test_exit_code_members_and_values(self) -> None:
        """ExitCode is the one definition of the CLI exit codes (EXC-02, D-07)."""
        assert {(member.name, int(member)) for member in ExitCode} == {
            ("SUCCESS", 0),
            ("SCAN", 1),
            ("CONFIG", 2),
            ("PAPERLESS", 3),
            ("PDF", 4),
            ("UNEXPECTED", 5),
            ("CANCELLED", 130),
        }

    def test_exit_code_table_covers_every_category(self) -> None:
        """The pinned mapping table names every ErrorCategory member (D-07)."""
        assert {category for category, _ in _EXIT_CODES_FOR_CATEGORIES} == set(
            ErrorCategory
        )

    @pytest.mark.parametrize(("category", "expected"), _EXIT_CODES_FOR_CATEGORIES)
    def test_exit_code_for_mapping(
        self, category: ErrorCategory, expected: ExitCode
    ) -> None:
        """exit_code_for returns the documented exit code per category (D-07)."""
        assert exit_code_for(category) is expected

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_exit_code_for_is_total(self, category: ErrorCategory) -> None:
        """Every ErrorCategory maps to an ExitCode (D-07)."""
        assert isinstance(exit_code_for(category), ExitCode)

    def test_exit_code_for_raises_on_unrecognised_value(self) -> None:
        """exit_code_for raises on a value outside ErrorCategory (D-07)."""
        bad = cast("ErrorCategory", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            exit_code_for(bad)


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
            (JobState.CANCELLED, (False, False)),
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
