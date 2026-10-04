"""
The shared vocabulary gives every state, category and sentence one definition.

Job states, error categories, outcomes, exit codes and the operator-facing
copy all live in ``saneless.vocabulary``; these tests pin each mapping, its
totality over its enum, and the exact words the operator reads.
"""

from __future__ import annotations

import inspect
import json
import re
import signal
import time
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final, cast

import pytest
from fastapi.responses import JSONResponse

import saneless.job
from saneless import preservation
from saneless.exceptions import (
    AllPagesBlankError,
    ConfigError,
    DiskSpaceError,
    FeederEmptyError,
    NoScannerFoundError,
    PaperlessError,
    PaperlessIncompatibleError,
    PaperlessTimeoutError,
    PaperlessUncertainSendError,
    PaperlessUnconfirmedError,
    PdfError,
    ScanCancelledError,
    ScanError,
    StorageError,
)
from saneless.job import ErrorCategory as JobErrorCategory
from saneless.job import Job
from saneless.job import JobState as JobJobState
from saneless.vocabulary import (
    _BUSY_SEPARATOR,
    _SCAN_STAGE_PHRASES,
    ACTIVE_STATES,
    BACKS_SUFFIX,
    BUSY_STATES,
    CORRESPONDENTS_LOADING,
    CORRESPONDENTS_UNAVAILABLE,
    FALLBACK_NOT_UPLOADED_LINE,
    FRONTS_SUFFIX,
    IDLE_LINE,
    LOCAL_TIME_FORMAT,
    LOST_CONTACT_LINE,
    NO_SCRIPT_BACK_LINK,
    NO_SCRIPT_BODY,
    NO_SCRIPT_HEADING,
    NO_SCRIPT_LINE,
    NO_SCRIPT_PAGE_TITLE,
    PAPERLESS_TITLE_LIMIT,
    PARTIAL_SUFFIX,
    PASS_WAIT_STATES,
    PYTHON_SANE_INSTALL_NEXT_STEP,
    QUEUE_FULL_JOB_ERROR,
    RESTART_REASON,
    RESTART_UPLOADING_REASON,
    RETRY_PLACEHOLDER,
    RETRY_SENTENCE_PLACEHOLDER,
    TAG_FILTER_LABEL,
    TAGS_LOADING,
    TAGS_UNAVAILABLE,
    TERMINAL_STATES,
    TITLE_MAX_LENGTH,
    TITLE_SUFFIX_SEPARATOR,
    TOKEN_UNSET_JOB_ERROR,
    UNCONFIRMED_FILING_LABEL,
    UNCONFIRMED_SEND_LABEL,
    URL_UNSET_JOB_ERROR,
    WAITING_STATES,
    WARNED_UPLOAD_LABEL,
    WORKER_DEGRADED_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    CheckSurface,
    ConnectionStatus,
    ErrorAdvice,
    ErrorCategory,
    ExitCode,
    FlipOutcome,
    JobState,
    PassAnswer,
    PassPrompt,
    PassWait,
    ProfileStorage,
    RequestRejection,
    ScanOutcome,
    ScanStage,
    SubmitResult,
    WorkerHealth,
    ambiguous_source_error,
    backs_not_scanned_warning,
    backs_pass_cap_warning,
    blank_timeout_finish_warning,
    busy_line,
    cap_finish_warning,
    classify_error,
    connection_status_message,
    dropped_ids_warning,
    duplicate_warning,
    duration_phrase,
    error_advice,
    error_message,
    error_next_step,
    exit_code_for,
    exit_code_for_outcome,
    exit_code_for_signal,
    flip_answer_label,
    flip_deadline_note,
    flip_heading,
    half_delivery_error,
    half_title,
    is_amber_category,
    job_label,
    job_state_for,
    job_status_class,
    last_scan_detail,
    last_scan_line,
    local_time,
    non_owner_wait_line,
    outcome_line,
    page_counts,
    page_timeout_error,
    page_title,
    pages_phrase,
    pass_answer_label,
    pass_cap_warning,
    pass_heading,
    pass_wait_state,
    progress_label,
    python_sane_missing_message,
    rejection_message,
    rejection_status_code,
    removed_pages,
    removed_pages_note,
    render_check_step,
    restart_category,
    restart_error,
    sane_init_failure_message,
    scan_button_label,
    scan_child_crashed_error,
    scan_child_ended_error,
    scan_child_no_answer_error,
    scan_child_not_started_error,
    scan_child_out_of_time_error,
    scan_child_stopped_error,
    scan_child_unexpected_error,
    scan_hold_reason,
    scan_page_description,
    sixteen_bit_error,
    source_not_offered_error,
    stale_default_correspondent_label,
    stale_default_tag_label,
    state_label,
    substituted_source_warning,
    timeout_finish_warning,
    unlisted_correspondent_label,
    unlisted_tag_label,
    worker_health_detail,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


# The two failures that may already be in paperless-ngx, written out here
# rather than read from ``is_amber_category`` so the tests pin the set instead
# of repeating whatever the function says.
_AMBER_CATEGORIES = frozenset(
    {ErrorCategory.UNCONFIRMED_SEND, ErrorCategory.UNCONFIRMED_FILING}
)


class TestJobStateMembers:
    """Every JobState member is valued as its name."""

    @pytest.mark.parametrize("state", list(JobState))
    def test_job_state_value_equals_name(self, state: JobState) -> None:
        """Every JobState value is identical to its member name."""
        assert state.value == state.name


class TestErrorCategoryMembers:
    """ErrorCategory is the documented set of categories, each valued as its name."""

    def test_error_category_member_names(self) -> None:
        """
        ErrorCategory names are the documented categories.

        The documented categories include REJECTED for a submit that never
        ran, ASSEMBLY for a PDF that could not be built and
        ALL_BLANK for a scan whose every page empty-page detection judged
        blank, which is not a scanner fault.  UNCONFIRMED_SEND and
        UNCONFIRMED_FILING mark an upload that may already be in
        paperless-ngx, PAPERLESS_VERSION a paperless-ngx whose API version
        saneless cannot speak, and DISK_SPACE a server that ran out of room.
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
            "ALL_BLANK",
            "UNCONFIRMED_SEND",
            "UNCONFIRMED_FILING",
            "PAPERLESS_VERSION",
            "DISK_SPACE",
        }

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_category_value_equals_name(self, category: ErrorCategory) -> None:
        """Every ErrorCategory value is identical to its member name."""
        assert category.value == category.name


class TestScanOutcomeMembers:
    """ScanOutcome is SUCCESS or FALLBACK, each valued as its name."""

    def test_scan_outcome_has_exactly_two_members(self) -> None:
        """
        ScanOutcome is exactly SUCCESS and FALLBACK.

        There is no FAILED member and there will not be one: a failure raises,
        so ``outcome`` stays NULL and ``JobState.ERROR`` carries the failure.
        A returned FAILED would record the same fact in a second column that
        could disagree with the first about a job with exactly one fate.
        """
        assert {outcome.name for outcome in ScanOutcome} == {"SUCCESS", "FALLBACK"}

    @pytest.mark.parametrize("outcome", list(ScanOutcome))
    def test_scan_outcome_value_equals_name(self, outcome: ScanOutcome) -> None:
        """Every ScanOutcome value is identical to its member name."""
        assert outcome.value == outcome.name


class TestProfileStorage:
    """ProfileStorage names where the startup profiles ended up."""

    def test_profile_storage_member_names(self) -> None:
        """
        ProfileStorage names the three outcomes of the startup persist.

        ``StartupProfiles``'s write result is None for two genuinely
        different situations -- no config file was loaded, and the file could
        not be written -- and the status strip's Profiles row has to tell them
        apart.  Compared as a set: declaration order is not a contract.
        """
        assert {member.name for member in ProfileStorage} == {
            "PERSISTED",
            "IN_MEMORY_NO_CONFIG_FILE",
            "IN_MEMORY_UNWRITABLE",
        }

    @pytest.mark.parametrize("storage", list(ProfileStorage))
    def test_profile_storage_value_equals_name(self, storage: ProfileStorage) -> None:
        """Every ProfileStorage value is identical to its member name."""
        assert storage.value == storage.name

    def test_profile_storage_is_a_str_enum(self) -> None:
        """ProfileStorage compares equal to its own string, like its siblings."""
        assert ProfileStorage.PERSISTED == "PERSISTED"


class TestStateClassifications:
    """ACTIVE_STATES / TERMINAL_STATES / BUSY_STATES classification tests."""

    @pytest.mark.parametrize("state", list(JobState))
    def test_every_state_is_active_xor_terminal(self, state: JobState) -> None:
        """Each JobState is either active or terminal, never both."""
        assert (state in ACTIVE_STATES) ^ (state in TERMINAL_STATES)

    def test_classifications_cover_every_state(self) -> None:
        """ACTIVE_STATES and TERMINAL_STATES together are all of JobState."""
        assert set(JobState) == ACTIVE_STATES | TERMINAL_STATES

    def test_classifications_do_not_overlap(self) -> None:
        """ACTIVE_STATES and TERMINAL_STATES share no member."""
        assert not (ACTIVE_STATES & TERMINAL_STATES)

    def test_active_states_membership(self) -> None:
        """ACTIVE_STATES is the nine in-flight lifecycle states."""
        assert (
            frozenset(
                {
                    JobState.PENDING,
                    JobState.SCANNING,
                    JobState.AWAITING_FLIP,
                    JobState.AWAITING_NEXT_PASS,
                    JobState.AWAITING_BLANK_DECISION,
                    JobState.AWAITING_RETRY,
                    JobState.SCANNING_REVERSE,
                    JobState.ASSEMBLING,
                    JobState.UPLOADING,
                }
            )
            == ACTIVE_STATES
        )

    def test_terminal_states_membership(self) -> None:
        """TERMINAL_STATES is DONE, ERROR, FALLBACK and CANCELLED."""
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
        """A cancelled job is finished, so the machine is not working."""
        assert JobState.CANCELLED not in BUSY_STATES
        assert JobState.CANCELLED.value == "CANCELLED"

    def test_busy_states_is_derived_from_active_states(self) -> None:
        """BUSY_STATES is ACTIVE_STATES minus every state waiting for a person."""
        assert ACTIVE_STATES - WAITING_STATES == BUSY_STATES

    def test_waiting_states_are_the_pass_waits_and_the_flip_wait(self) -> None:
        """WAITING_STATES is the three multi-page waits plus the flip wait."""
        assert PASS_WAIT_STATES | {JobState.AWAITING_FLIP} == WAITING_STATES

    def test_pass_wait_states_membership(self) -> None:
        """PASS_WAIT_STATES is exactly the three multi-page waits."""
        assert (
            frozenset(
                {
                    JobState.AWAITING_NEXT_PASS,
                    JobState.AWAITING_BLANK_DECISION,
                    JobState.AWAITING_RETRY,
                }
            )
            == PASS_WAIT_STATES
        )

    @pytest.mark.parametrize(
        "state",
        [
            JobState.AWAITING_NEXT_PASS,
            JobState.AWAITING_BLANK_DECISION,
            JobState.AWAITING_RETRY,
        ],
    )
    def test_a_multi_page_wait_is_active_but_not_busy(self, state: JobState) -> None:
        """
        A job waiting for more pages is in flight, but the machine is idle.

        Classified exactly as the flip wait is: a waiting job that showed the
        busy spinner would look like a scan that hung.
        """
        assert state in ACTIVE_STATES
        assert state not in BUSY_STATES
        assert state in WAITING_STATES
        assert state in PASS_WAIT_STATES

    def test_the_flip_wait_is_waiting_but_not_a_pass_wait(self) -> None:
        """AWAITING_FLIP waits for a person, but not for a multi-page answer."""
        assert JobState.AWAITING_FLIP in WAITING_STATES
        assert JobState.AWAITING_FLIP not in PASS_WAIT_STATES

    def test_waiting_states_are_a_subset_of_active_states(self) -> None:
        """No state can wait for a person without also being in flight."""
        assert WAITING_STATES <= ACTIVE_STATES

    def test_awaiting_flip_is_active_but_not_busy(self) -> None:
        """AWAITING_FLIP is in flight but the machine is idle then."""
        assert JobState.AWAITING_FLIP in ACTIVE_STATES
        assert JobState.AWAITING_FLIP not in BUSY_STATES

    def test_busy_states_is_a_subset_of_active_states(self) -> None:
        """No state can be busy without also being active."""
        assert BUSY_STATES <= ACTIVE_STATES


class TestStateLabel:
    """state_label gives every JobState a short label."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (JobState.PENDING, "Pending"),
            (JobState.SCANNING, "Scanning"),
            (JobState.AWAITING_FLIP, "Waiting for flip"),
            (JobState.AWAITING_NEXT_PASS, "Waiting for more pages"),
            (JobState.AWAITING_BLANK_DECISION, "Waiting: blank pages found"),
            (JobState.AWAITING_RETRY, "Waiting: last scan failed"),
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
        """state_label returns the label the history table shows."""
        assert state_label(state) == expected

    @pytest.mark.parametrize("state", list(JobState))
    def test_state_label_is_complete(self, state: JobState) -> None:
        """Every JobState has a label that is not just its raw value."""
        label = state_label(state)
        assert label
        assert label != state.value


class TestProgressLabel:
    """progress_label gives every JobState its progress prose."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (JobState.PENDING, "Starting scan..."),
            (JobState.SCANNING, "Scanning..."),
            (JobState.AWAITING_FLIP, "Awaiting flip..."),
            (JobState.AWAITING_NEXT_PASS, "Waiting for the next page..."),
            (
                JobState.AWAITING_BLANK_DECISION,
                "Waiting for a decision about blank pages...",
            ),
            (
                JobState.AWAITING_RETRY,
                "The last scan failed; waiting for a decision...",
            ),
            (JobState.SCANNING_REVERSE, "Scanning reverse sides..."),
            (JobState.ASSEMBLING, "Assembling PDF..."),
            (JobState.UPLOADING, "Uploading to paperless-ngx..."),
        ],
    )
    def test_progress_label_strings(self, state: JobState, expected: str) -> None:
        """progress_label returns the prose the status area shows."""
        assert progress_label(state) == expected

    @pytest.mark.parametrize("state", list(JobState))
    def test_progress_label_is_complete(self, state: JobState) -> None:
        """Every JobState has progress prose that is not its raw value."""
        label = progress_label(state)
        assert label
        assert label != state.value

    def test_terminal_states_have_progress_prose_for_totality(self) -> None:
        """The four terminal states carry prose purely to stay total."""
        assert progress_label(JobState.DONE) == "Complete"
        assert progress_label(JobState.ERROR) == "Failed"
        assert progress_label(JobState.FALLBACK) == "Saved to folder"
        assert progress_label(JobState.CANCELLED) == "Cancelled"


class TestFlipAnswerLabel:
    """flip_answer_label gives every FlipOutcome its acknowledgment copy."""

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (FlipOutcome.CONTINUED, "Flip confirmed. Scanning reverse sides next..."),
            (FlipOutcome.ABORTED, "Aborting scan..."),
            (FlipOutcome.TIMED_OUT, "Flip wait timed out..."),
            (
                FlipOutcome.INTERRUPTED,
                "Stopping: saneless is shutting down and keeping the pages "
                "already scanned...",
            ),
        ],
    )
    def test_flip_answer_label_strings(
        self, outcome: FlipOutcome, expected: str
    ) -> None:
        """flip_answer_label returns the acknowledgment copy."""
        assert flip_answer_label(outcome) == expected

    def test_a_flip_wait_ends_in_one_of_four_ways(self) -> None:
        """
        Flipped, gave up, ran out of time, or saneless is stopping.

        The fourth is not the operator's decision, which is why it is its own
        member rather than an Abort: the pages already scanned are kept.
        """
        assert [outcome.value for outcome in FlipOutcome] == [
            "CONTINUED",
            "ABORTED",
            "TIMED_OUT",
            "INTERRUPTED",
        ]

    @pytest.mark.parametrize("outcome", list(FlipOutcome))
    def test_flip_answer_label_is_complete(self, outcome: FlipOutcome) -> None:
        """Every FlipOutcome has acknowledgment prose ending in ASCII dots."""
        label = flip_answer_label(outcome)
        assert label
        assert label != outcome.value
        assert label.endswith("...")
        assert "…" not in label


class TestPassWaitState:
    """pass_wait_state maps each multi-page question to its persisted state."""

    @pytest.mark.parametrize(
        ("wait", "expected"),
        [
            (PassWait.NEXT_PASS, JobState.AWAITING_NEXT_PASS),
            (PassWait.BLANK_DECISION, JobState.AWAITING_BLANK_DECISION),
            (PassWait.RETRY, JobState.AWAITING_RETRY),
        ],
    )
    def test_pass_wait_state(self, wait: PassWait, expected: JobState) -> None:
        """Each open question has its own state, so the row says what it waits on."""
        assert pass_wait_state(wait) == expected

    @pytest.mark.parametrize("wait", list(PassWait))
    def test_every_pass_wait_lands_in_a_pass_wait_state(self, wait: PassWait) -> None:
        """No multi-page question can put a job in a busy or terminal state."""
        assert pass_wait_state(wait) in PASS_WAIT_STATES

    def test_pass_wait_values_equal_names(self) -> None:
        """PassWait has exactly three members, each valued as its name."""
        assert [wait.value for wait in PassWait] == [
            "NEXT_PASS",
            "BLANK_DECISION",
            "RETRY",
        ]


class TestPassAnswer:
    """PassAnswer members and pass_answer_label acknowledgment copy."""

    def test_pass_answer_has_exactly_eight_members_in_order(self) -> None:
        """
        A multi-page wait ends in exactly one of eight ways.

        Six are the operator's buttons, one is the clock running out and one
        is saneless stopping, which keeps the pages already scanned.
        """
        assert [answer.value for answer in PassAnswer] == [
            "NEXT",
            "RESCAN",
            "FINISH",
            "ABORT",
            "SKIP_BLANKS",
            "KEEP_BLANKS",
            "TIMED_OUT",
            "INTERRUPTED",
        ]

    def test_flip_outcome_is_not_widened(self) -> None:
        """The flip wait has four outcomes; multi-page answers are a separate enum."""
        assert [outcome.value for outcome in FlipOutcome] == [
            "CONTINUED",
            "ABORTED",
            "TIMED_OUT",
            "INTERRUPTED",
        ]

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            (PassAnswer.NEXT, "Scanning more pages..."),
            (PassAnswer.RESCAN, "Discarded the last scan. Scanning again..."),
            (PassAnswer.FINISH, "Finishing the document..."),
            (PassAnswer.ABORT, "Aborting scan..."),
            (PassAnswer.SKIP_BLANKS, "Skipping blank pages..."),
            (PassAnswer.KEEP_BLANKS, "Keeping blank pages..."),
            (PassAnswer.TIMED_OUT, "Nobody answered; finishing the document..."),
            (
                PassAnswer.INTERRUPTED,
                "Stopping: saneless is shutting down and keeping the pages "
                "already scanned...",
            ),
        ],
    )
    def test_pass_answer_label_strings(self, answer: PassAnswer, expected: str) -> None:
        """pass_answer_label returns the acknowledgment copy verbatim."""
        assert pass_answer_label(answer) == expected

    def test_abort_reads_the_same_as_the_flip_abort(self) -> None:
        """Aborting a multi-page wait says exactly what aborting a flip says."""
        assert pass_answer_label(PassAnswer.ABORT) == flip_answer_label(
            FlipOutcome.ABORTED
        )

    def test_interrupted_reads_the_same_as_the_flip_interruption(self) -> None:
        """A server stop during either wait is acknowledged in the same words."""
        assert pass_answer_label(PassAnswer.INTERRUPTED) == flip_answer_label(
            FlipOutcome.INTERRUPTED
        )

    @pytest.mark.parametrize("answer", list(PassAnswer))
    def test_pass_answer_label_is_complete(self, answer: PassAnswer) -> None:
        """Every PassAnswer has acknowledgment prose ending in ASCII dots."""
        label = pass_answer_label(answer)
        assert label
        assert label != answer.value
        assert label.endswith("...")
        assert "…" not in label


class TestPassPrompt:
    """PassPrompt is a frozen, slotted value with fixed fields."""

    @staticmethod
    def _prompt() -> PassPrompt:
        return PassPrompt(
            number=1,
            wait=PassWait.NEXT_PASS,
            pages_kept=2,
            offered=frozenset({PassAnswer.NEXT}),
            timeout_seconds=600,
        )

    def test_pass_prompt_defaults(self) -> None:
        """The per-question fields default to empty, so each wait sets only its own."""
        prompt = self._prompt()
        assert prompt.number == 1
        assert prompt.wait is PassWait.NEXT_PASS
        assert prompt.pages_kept == 2
        assert prompt.offered == frozenset({PassAnswer.NEXT})
        assert prompt.timeout_seconds == 600
        assert prompt.last_pass_pages == 0
        assert prompt.last_pass_kept == 0
        assert prompt.pass_pages == 0
        assert prompt.blank_positions == ()
        assert prompt.error is None

    def test_pass_prompt_is_frozen(self) -> None:
        """
        A prompt cannot be widened after it is built, so its offer is fixed.

        The attribute name is a local rather than a literal so this exercises
        the dataclass's own runtime guard rather than a linter's rule about
        constant ``setattr`` targets.
        """
        prompt = self._prompt()
        attribute = "offered"
        with pytest.raises(FrozenInstanceError):
            setattr(prompt, attribute, frozenset(PassAnswer))

    def test_pass_prompt_is_slotted(self) -> None:
        """PassPrompt is slotted, so a typo cannot add a silent extra field."""
        assert not hasattr(self._prompt(), "__dict__")

    def test_pass_prompt_field_order(self) -> None:
        """The fields are the documented ones, in the documented order."""
        assert [field.name for field in fields(PassPrompt)] == [
            "number",
            "wait",
            "pages_kept",
            "offered",
            "timeout_seconds",
            "last_pass_pages",
            "last_pass_kept",
            "pass_pages",
            "blank_positions",
            "error",
        ]


class TestDurationPhrase:
    """duration_phrase names an operator-wait bound in the largest whole unit."""

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (600, "10 minutes"),
            (90, "90 seconds"),
            (3600, "1 hour"),
            (7200, "2 hours"),
            (60, "1 minute"),
            (1, "1 second"),
            (5400, "90 minutes"),
            (600.0, "10 minutes"),
            (599.6, "10 minutes"),
        ],
    )
    def test_duration_phrase(self, seconds: float, expected: str) -> None:
        """Seconds are rounded to whole seconds, then named in the largest unit."""
        assert duration_phrase(seconds) == expected


class TestPagesPhrase:
    """pages_phrase counts pages with the right noun."""

    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, "0 pages"), (1, "1 page"), (4, "4 pages")],
    )
    def test_pages_phrase(self, count: int, expected: str) -> None:
        """One page is a page; any other count is pages."""
        assert pages_phrase(count) == expected


class TestFinishWarnings:
    """The warnings a document finished without the operator pressing Finish carries."""

    def test_timeout_finish_warning(self) -> None:
        """A between-pass timeout names the page count and the wait."""
        assert timeout_finish_warning(4, 600) == (
            "Finished after 4 pages because nobody answered within 10 minutes; the document may be missing pages."
        )

    def test_timeout_finish_warning_single_page(self) -> None:
        """One page is a page, not pages."""
        assert timeout_finish_warning(1, 600) == (
            "Finished after 1 page because nobody answered within 10 minutes; "
            "the document may be missing pages."
        )

    def test_timeout_finish_warning_uses_the_duration_phrase(self) -> None:
        """The wait is named the way duration_phrase names it."""
        assert "within 90 seconds;" in timeout_finish_warning(4, 90)

    def test_cap_finish_warning(self) -> None:
        """The cap warning names where the document ended and the cap."""
        assert cap_finish_warning(512, 500) == (
            "Finished at 512 pages: no new scan starts once a document has 500 pages. Scan any remaining pages as a new document."
        )

    def test_pass_cap_warning_for_a_named_feeder(self) -> None:
        """A named feeder's cap names the sheet that was fed but not kept."""
        assert pass_cap_warning(500, 500, 501, auto_source=False) == (
            "Finished at 500 pages: one scan stops after 500 sheets, so sheet 501 "
            "was fed but not kept. Scan sheet 501 and any remaining pages as a new "
            "document."
        )

    def test_pass_cap_warning_for_an_auto_source(self) -> None:
        """The Auto cap also says how to stop the glass being scanned again."""
        assert pass_cap_warning(50, 50, 51, auto_source=True) == (
            "Finished at 50 pages: a scan from an Auto source through the feeder "
            "stops after 50 sheets, so sheet 51 was fed but not kept. If the feeder "
            "was already empty, the scanner was scanning its glass again; set "
            'auto_source_mode = "flatbed" for this profile. Otherwise scan sheet 51 '
            "and any remaining pages as a new document."
        )

    @pytest.mark.parametrize("auto_source", [False, True])
    def test_pass_cap_warning_shares_the_document_cap_lead_and_tail(
        self, *, auto_source: bool
    ) -> None:
        """Both cap warnings open and close the way cap_finish_warning does."""
        warning = pass_cap_warning(497, 500, 501, auto_source=auto_source)
        assert warning.startswith("Finished at 497 pages: ")
        assert "sheet 501 was fed but not kept" in warning
        assert warning.endswith("any remaining pages as a new document.")
        assert cap_finish_warning(497, 500).endswith(
            "any remaining pages as a new document."
        )

    def test_pass_cap_warning_counts_one_page(self) -> None:
        """A single kept page is a page, not pages."""
        assert pass_cap_warning(1, 500, 501, auto_source=False).startswith(
            "Finished at 1 page: "
        )

    def test_backs_pass_cap_warning(self) -> None:
        """
        A capped backs pass counts both halves and says to re-scan both sides.

        Its sheet past the cap has no scanned front, so the advice is not to
        resume from it, as the one-pass sentence says.
        """
        assert backs_pass_cap_warning(950, 500, 501) == (
            "Finished at 950 pages: the scan of the backs stops after 500 "
            "sheets, so sheet 501 of the turned-over stack was fed but not "
            "kept, and the stack held more sheets than the scan of the fronts "
            "fed. Check both documents, and scan any sheet missing from either "
            "again, both sides, as a new document."
        )

    def test_backs_not_scanned_warning(self) -> None:
        """A capped fronts pass names the sheet that makes pairing impossible."""
        assert backs_not_scanned_warning(501) == (
            "The backs were not scanned: sheet 501 is already in the output "
            "tray, so turning the stack over would pair every back with the "
            "wrong front."
        )

    def test_substituted_source_warning(self) -> None:
        """The substitution names the request, the feeder and the fix."""
        assert substituted_source_warning("Flatbed") == (
            "The scanner has no source named 'Flatbed', so its Auto source was "
            "scanned through the feeder, because this profile's auto_source_mode "
            'is "adf". Set the profile\'s source to one the scanner lists.'
        )

    def test_substituted_source_warning_shows_the_name_with_repr(self) -> None:
        """An empty or padded name is visible in the sentence."""
        assert "' Flatbed '" in substituted_source_warning(" Flatbed ")

    def test_source_not_offered_error(self) -> None:
        """The refusal names the request, the offered sources and the fix."""
        assert source_not_offered_error("Nope", ["Auto", "Flatbed", "ADF"]) == (
            "The scanner has no source named 'Nope'. It offers 'Auto', 'Flatbed', "
            "'ADF'; set the profile's source to one of those names."
        )

    def test_source_not_offered_error_when_the_scanner_lists_none(self) -> None:
        """An empty list is said plainly rather than as an empty enumeration."""
        assert source_not_offered_error("ADF", []) == (
            "The scanner has no source named 'ADF', and it lists no sources to "
            "choose from."
        )

    def test_ambiguous_source_error(self) -> None:
        """The refusal names the request and every entry it could have meant."""
        assert ambiguous_source_error("Adf", ["ADF", "adf"]) == (
            "The source 'Adf' matches more than one of the scanner's sources "
            "when case and surrounding spaces are ignored: 'ADF', 'adf'. Set "
            "the profile's source to one of those names exactly."
        )

    def test_sixteen_bit_error(self) -> None:
        """The refusal names the device, the two depths and the fix."""
        assert sixteen_bit_error("test:0") == (
            "The scanner test:0 is set to 16 bits per sample, and saneless scans "
            "at 8. Choose an 8-bit mode, such as Gray or Color, in the profile."
        )

    def test_scan_page_description_colour(self) -> None:
        """A colour page of known length is named by its size and dpi."""
        assert scan_page_description(9921, 14031, colour=True, dpi=1200) == (
            "a colour page of 9921 x 14031 pixels at 1200 dpi"
        )

    def test_scan_page_description_grey(self) -> None:
        """A page that is not colour is named as grey."""
        assert scan_page_description(2480, 3508, colour=False, dpi=300) == (
            "a grey page of 2480 x 3508 pixels at 300 dpi"
        )

    def test_scan_page_description_unknown_length(self) -> None:
        """A page whose length the device does not know says so."""
        assert scan_page_description(9921, -1, colour=True, dpi=1200) == (
            "a colour page 9921 pixels wide and of unknown length at 1200 dpi"
        )
        assert scan_page_description(2480, 0, colour=False, dpi=300) == (
            "a grey page 2480 pixels wide and of unknown length at 300 dpi"
        )

    def test_page_timeout_error_names_the_limit_and_the_page(self) -> None:
        """A timed-out page says how long it had, and for what page."""
        page = scan_page_description(9921, 14031, colour=True, dpi=1200)
        assert page_timeout_error("Page 3", 479.6, page, returned=True) == (
            "Page 3 timed out after 480s, the limit for a colour page of "
            "9921 x 14031 pixels at 1200 dpi"
        )

    def test_page_timeout_error_when_the_read_did_not_return(self) -> None:
        """A read the cancel did not end says saneless stopped the scanner."""
        page = scan_page_description(2480, 3508, colour=False, dpi=300)
        message = page_timeout_error("Page 1", 120.0, page, returned=False)
        assert message == (
            "Page 1 timed out after 120s, the limit for a grey page of "
            "2480 x 3508 pixels at 300 dpi; the scanner did not answer the "
            "cancel either, so saneless stopped it"
        )
        assert "still waiting" not in message

    def test_page_timeout_error_without_a_page(self) -> None:
        """With no page described, only the limit is named."""
        assert page_timeout_error("Page 2", 120.0, None, returned=True) == (
            "Page 2 timed out after 120s"
        )
        assert page_timeout_error("Page 2", 120.0, None, returned=False) == (
            "Page 2 timed out after 120s; the scanner did not answer the "
            "cancel either, so saneless stopped it"
        )

    def test_blank_timeout_finish_warning(self) -> None:
        """A blank-prompt timeout says the blank pages were left out."""
        assert blank_timeout_finish_warning(4, 600) == (
            "Finished after 4 pages because nobody answered about the blank "
            "pages within 10 minutes, so they were left out; the document may "
            "be missing pages."
        )

    def test_blank_timeout_finish_warning_single_page(self) -> None:
        """One page is a page, not pages, and an hour is named as an hour."""
        assert blank_timeout_finish_warning(1, 3600).startswith(
            "Finished after 1 page because nobody answered about the blank "
            "pages within 1 hour,"
        )


# A category's next step is the fallback for every error filed under it that
# does not carry a next step of its own, and the CLI's ``Try:`` line and the
# web job view both show it.  So it must be true for every one of them: a
# generic pointer is fine, a specific fix that some member contradicts is not.
# Each entry pairs an error that reaches the category with phrases its real fix
# contradicts; a fallback containing any of them would send that operator the
# wrong way.  Errors whose raise site supplies its own next step (a broken
# terminal prompt, an unreadable trust store) are left out on purpose.
_CONFIG_IS_NOT_THE_FIX = ("configuration file", "config file", "restart saneless")
_SCAN_IS_NOT_THE_RETRY = ("start the scan again", "scan again")
_CATEGORY_AUDIT: dict[ErrorCategory, tuple[tuple[str, tuple[str, ...]], ...]] = {
    ErrorCategory.CONFIG: (
        ("port already in use", _CONFIG_IS_NOT_THE_FIX),
        ("python-sane or libsane missing", _CONFIG_IS_NOT_THE_FIX),
        ("--config path not found", _CONFIG_IS_NOT_THE_FIX),
        ("HOME unset", _CONFIG_IS_NOT_THE_FIX),
        ("environment variable typo", _CONFIG_IS_NOT_THE_FIX),
        ("workspace folder not writable", _CONFIG_IS_NOT_THE_FIX),
        ("a single file mounted where a folder belongs", _CONFIG_IS_NOT_THE_FIX),
        ("unknown profile", _CONFIG_IS_NOT_THE_FIX),
        ("any one-shot command", _CONFIG_IS_NOT_THE_FIX),
    ),
    ErrorCategory.SCANNER: (
        ("saneless devices found no scanner", _SCAN_IS_NOT_THE_RETRY),
        ("listing timed out", _SCAN_IS_NOT_THE_RETRY),
        ("listing crashed or never answered", _SCAN_IS_NOT_THE_RETRY),
        ("SANE could not start in devices or auto-profiles", _SCAN_IS_NOT_THE_RETRY),
    ),
    ErrorCategory.UPLOAD: (
        (
            "serve could not build the client",
            ("start the scan again", "scan again", "api token is correct"),
        ),
        ("upload refused", ("start the scan again", "api token is correct")),
        ("paperless.url redirects", ("api token is correct",)),
        ("URL carries a user name or password", ("api token is correct",)),
    ),
}

# The category message states a cause, so it is held to the same rule over
# the members whose cause is something else.
_CATEGORY_MESSAGE_AUDIT: dict[
    ErrorCategory, tuple[tuple[str, tuple[str, ...]], ...]
] = {
    ErrorCategory.CONFIG: (
        ("port already in use", ("configuration is invalid",)),
        ("python-sane or libsane missing", ("configuration is invalid",)),
        ("HOME unset", ("configuration is invalid",)),
        ("workspace folder not writable", ("configuration is invalid",)),
    ),
}


def _audit_cases(
    audit: dict[ErrorCategory, tuple[tuple[str, tuple[str, ...]], ...]],
) -> list[object]:
    """
    Flatten an audit table into one test case per category, member and phrase.

    Args:
        audit: Each category's members, paired with the phrases they contradict.

    Returns:
        ``pytest.param(category, member, phrase)`` cases with readable ids.

    """
    return [
        pytest.param(category, member, phrase, id=f"{category}-{member}-{phrase}")
        for category, members in audit.items()
        for member, phrases in members
        for phrase in phrases
    ]


class TestErrorAdvice:
    """error_advice gives every ErrorCategory its advice."""

    @pytest.mark.parametrize(
        ("category", "member", "phrase"), _audit_cases(_CATEGORY_AUDIT)
    )
    def test_no_fallback_names_a_fix_a_member_contradicts(
        self, category: ErrorCategory, member: str, phrase: str
    ) -> None:
        """
        A category's next step holds for every error that reaches it.

        The next step is shown for each of them on the CLI and on the web, so
        a fix that is wrong for one member leads that operator astray.
        """
        next_step = error_next_step(category).lower()
        assert phrase not in next_step, (
            f"{category} advice {next_step!r} is wrong for {member!r}"
        )

    @pytest.mark.parametrize(
        ("category", "member", "phrase"), _audit_cases(_CATEGORY_MESSAGE_AUDIT)
    )
    def test_no_category_message_names_a_cause_a_member_contradicts(
        self, category: ErrorCategory, member: str, phrase: str
    ) -> None:
        """A category's message names no cause that one of its errors disproves."""
        message = error_message(category).lower()
        assert phrase not in message, (
            f"{category} message {message!r} is wrong for {member!r}"
        )

    def test_error_advice_fields(self) -> None:
        """ErrorAdvice carries exactly a message and a next step."""
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
        """Every ErrorCategory has a message and a next step."""
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
        error_message and error_next_step read the one lookup.

        There is exactly one ``match`` over ErrorCategory in the module and the
        two accessors are one-liners over it, so the pair cannot drift apart
        the way two parallel lookups would.
        """
        advice = error_advice(category)
        assert error_message(category) == advice.message
        assert error_next_step(category) == advice.next_step

    def test_error_advice_raises_on_unrecognised_value(self) -> None:
        """error_advice raises on a value outside ErrorCategory."""
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
            # CONFIG, SCANNER and UPLOAD also reach commands that scan
            # nothing, so they say "try again"; CONFIG and UPLOAD point at the
            # error itself because their members' fixes differ.
            (
                ErrorCategory.CONFIG,
                "Fix the problem the error names, then try again.",
            ),
            (
                ErrorCategory.SCANNER,
                "Check the scanner is switched on and connected, then try again.",
            ),
            (
                ErrorCategory.UPLOAD,
                "Fix the problem the error names (for example, paperless-ngx is "
                "not running, or paperless.url or paperless.token is wrong), "
                "then try again.",
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
            # Promises the folder and nothing more: a write the disk refused
            # names no amount, so "how much is needed" would point at a
            # figure the error beside it does not carry.
            (
                ErrorCategory.DISK_SPACE,
                "Free space on the server (the error names the folder), then "
                "start the scan again.",
            ),
        ],
    )
    def test_error_next_step_strings(
        self, category: ErrorCategory, expected: str
    ) -> None:
        """The next steps are the approved copy."""
        assert error_next_step(category) == expected

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_next_step_is_surface_neutral(self, category: ErrorCategory) -> None:
        """
        No next step names a surface, because both surfaces render it.

        The web page has no command line and the CLI has no Scan button, so a
        string naming either would be wrong on the other.
        """
        next_step = error_next_step(category).lower()
        assert "press scan" not in next_step
        assert "click" not in next_step
        assert "run the command" not in next_step

    def test_error_message_strings(self) -> None:
        """
        The messages are the approved copy.

        CONFIG's does not call the configuration invalid, because a port in
        use, a missing libsane or an unwritable folder is filed there too.
        """
        assert error_message(ErrorCategory.FEEDER) == (
            "The document feeder is empty or jammed."
        )
        assert error_message(ErrorCategory.CONFIG) == (
            "saneless is not set up correctly."
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

    def test_all_blank_advice_names_the_threshold_not_the_scanner(self) -> None:
        """
        An all-blank scan is advised to tune detection, not to check the scanner.

        The scanner worked: it returned pages, and empty-page detection judged
        every one of them blank.  The advice names the setting to lower and
        the way to turn detection off, and never sends the reader to a
        scanner that did nothing wrong.
        """
        advice = error_advice(ErrorCategory.ALL_BLANK)
        assert "empty_page_coverage_threshold" in advice.next_step
        assert "off" in advice.next_step
        assert "scanner" not in advice.message.lower()
        assert "scanner" not in advice.next_step.lower()

    def test_all_blank_advice_does_not_promise_a_pdf(self) -> None:
        """
        The fixed advice cannot know what was kept, so it does not say.

        The unfiltered pages are normally kept as one PDF, but when that PDF
        cannot be built the page files are kept instead, or nothing reaches
        the failed folder at all.  The job's own error says which; the advice
        shown beside every such job must not contradict it.
        """
        advice = error_advice(ErrorCategory.ALL_BLANK)
        assert "were kept as a PDF" not in advice.message
        assert "normally" in advice.message
        assert "error says what was kept" in advice.message

    @pytest.mark.parametrize(
        "category",
        [ErrorCategory.UNCONFIRMED_SEND, ErrorCategory.UNCONFIRMED_FILING],
    )
    def test_unconfirmed_advice_never_says_to_rescan_unchecked(
        self, category: ErrorCategory
    ) -> None:
        """
        The document may already be in paperless-ngx, so check before rescanning.

        A plain "start the scan again" would store the document twice.  The
        next step sends the reader to paperless-ngx's document list first, and
        says a copy the error names in failed/ is to be imported only if it is
        not there.
        """
        advice = error_advice(category)
        for text in (advice.message.lower(), advice.next_step.lower()):
            assert "start the scan again" not in text
            assert "scan again" not in text
        assert "document list" in advice.next_step
        assert "failed/" in advice.next_step
        assert "only if the document is not in paperless-ngx" in advice.next_step

    @pytest.mark.parametrize(
        "category",
        [ErrorCategory.UNCONFIRMED_SEND, ErrorCategory.UNCONFIRMED_FILING],
    )
    def test_unconfirmed_advice_does_not_promise_a_copy(
        self, category: ErrorCategory
    ) -> None:
        """
        The copy in failed/ is conditional, because sometimes there is none.

        A job that was uploading when saneless restarted keeps a copy only if
        the startup sweep found its PDF, and a copy that could not be written
        is reported as such.  An unconditional "a copy is kept" would send the
        operator who finds nothing in paperless-ngx looking for a file that
        does not exist.
        """
        next_step = error_advice(category).next_step
        assert "a copy is kept" not in next_step.lower()
        assert "If the error names a copy kept in failed/" in next_step

    def test_unconfirmed_messages_say_which_situation_it_is(self) -> None:
        """The received statement is stronger, so the two messages differ."""
        send = error_advice(ErrorCategory.UNCONFIRMED_SEND)
        filing = error_advice(ErrorCategory.UNCONFIRMED_FILING)
        assert "may have reached paperless-ngx" in send.message
        assert "received the document" in filing.message
        assert send.message != filing.message

    def test_paperless_version_advice_names_the_supported_release(self) -> None:
        """A refused API version is fixed by upgrading, not by a new token."""
        advice = error_advice(ErrorCategory.PAPERLESS_VERSION)
        assert "2.16 or later" in advice.next_step
        assert "9 or 10" in advice.message
        assert "token" not in advice.next_step

    def test_disk_space_advice_says_to_free_space(self) -> None:
        """A full disk is advised to free space, never to check the scanner."""
        advice = error_advice(ErrorCategory.DISK_SPACE)
        assert "disk space" in advice.message
        assert "Free space" in advice.next_step
        assert "scanner" not in advice.message.lower()
        assert "scanner" not in advice.next_step.lower()

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_advice_carries_no_numbers(self, category: ErrorCategory) -> None:
        """
        Advice is constant copy: the numbers belong to the error beside it.

        The one exception is the version advice, which names the paperless-ngx
        release and API versions saneless supports; those are facts about
        saneless, not about this job.
        """
        advice = error_advice(category)
        numbers = set(re.findall(r"\d+(?:\.\d+)?", advice.message + advice.next_step))
        allowed = (
            {"9", "10", "2.16"}
            if category is ErrorCategory.PAPERLESS_VERSION
            else set()
        )
        assert numbers <= allowed

    def test_rejected_error_message(self) -> None:
        """REJECTED explains that the scan never started."""
        assert error_message(ErrorCategory.REJECTED) == (
            "This scan was not started. Check that saneless is ready to scan, "
            "then try again."
        )


class TestTokenUnsetRejection:
    """An unset paperless-ngx token is a rejection of its own, with its own copy."""

    def test_token_unset_member_exists(self) -> None:
        """TOKEN_UNSET is a RequestRejection member of its own."""
        assert RequestRejection.TOKEN_UNSET.value == "TOKEN_UNSET"
        assert RequestRejection.TOKEN_UNSET.name == "TOKEN_UNSET"

    def test_token_unset_is_service_unavailable(self) -> None:
        """An unset token refuses with 503, beside the other not-ready arms."""
        assert rejection_status_code(RequestRejection.TOKEN_UNSET) == 503

    def test_token_unset_message(self) -> None:
        """The TOKEN_UNSET sentence is the approved copy, word for word."""
        assert rejection_message(RequestRejection.TOKEN_UNSET) == (
            "The paperless-ngx API token has not been set, so the scan was not "
            "started. Put a real API token in the saneless config file, then "
            "restart saneless."
        )

    def test_token_unset_does_not_reuse_worker_degraded_copy(self) -> None:
        """
        WORKER_DEGRADED is deliberately not reused for an unset token.

        "The scan service was unavailable" is untrue when the truth is that
        nobody ever set the token, so the two carry different words.
        """
        assert rejection_message(RequestRejection.TOKEN_UNSET) != rejection_message(
            RequestRejection.WORKER_DEGRADED
        )
        assert TOKEN_UNSET_JOB_ERROR != WORKER_DEGRADED_JOB_ERROR

    def test_token_unset_job_error(self) -> None:
        """The job-row error carries no trailing period, like its siblings."""
        assert TOKEN_UNSET_JOB_ERROR == (
            "Not started: the paperless-ngx API token has not been set"
        )
        assert not TOKEN_UNSET_JOB_ERROR.endswith(".")


class TestDeveloperConstantStrings:
    """Every user-facing string in this module is a developer constant."""

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_message_carries_no_internals(
        self, rejection: RequestRejection
    ) -> None:
        """No rejection message can carry input, a URL or exception text."""
        message = rejection_message(rejection)
        assert "{" not in message
        assert "%s" not in message
        assert "http" not in message.lower()
        assert "traceback" not in message.lower()

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_advice_carries_no_internals(self, category: ErrorCategory) -> None:
        """No ErrorAdvice field can carry input, a URL or exception text."""
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
    """local_time renders an instant in the server's zone, with the zone named."""

    def test_local_time_format_is_the_one_shared_format(self) -> None:
        """The web filter and the CLI table read one format constant."""
        assert LOCAL_TIME_FORMAT == "%Y-%m-%d %H:%M %Z"

    def test_local_time_renders_the_servers_zone(
        self, local_zone: Callable[[str], None]
    ) -> None:
        """A UTC instant renders in the server's local zone, named."""
        local_zone("America/Chicago")
        assert local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC)) == (
            "2026-09-16 14:03 CDT"
        )

    def test_local_time_names_utc_when_the_server_is_utc(
        self, local_zone: Callable[[str], None]
    ) -> None:
        """A UTC server still gets the zone named on the line."""
        local_zone("UTC")
        assert local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC)) == (
            "2026-09-16 19:03 UTC"
        )

    def test_local_time_ignores_the_values_own_zone(
        self, local_zone: Callable[[str], None]
    ) -> None:
        """
        A value carrying another zone still renders the server's.

        ``astimezone()`` is called with no argument, so the answer is the
        process's zone whatever the argument's tzinfo happens to be.
        """
        local_zone("America/Chicago")
        tokyo = timezone(timedelta(hours=9))
        assert local_time(datetime(2026, 9, 16, 19, 3, tzinfo=tokyo)) == (
            "2026-09-16 05:03 CDT"
        )


class TestLocalTimeTrailingSpace:
    """
    local_time never emits trailing whitespace.

    ``LOCAL_TIME_FORMAT`` ends in ``%Z``, which ``strftime`` renders as the
    empty string on a platform that reports no zone abbreviation, leaving the
    separator before it dangling, and a job title built from it would end in
    a space.

    No ``TZ`` value produces an empty ``%Z`` on glibc, so these tests pin the
    property by patching the module's format to one that ends in whitespace.
    """

    #: Stand-ins for a format whose final ``%Z`` rendered empty.  A literal
    #: space is the real case; the tab and the double space are there so the
    #: strip cannot be a special case for one character.
    WHITESPACE_FORMATS = ("%Y-%m-%d %H:%M ", "%Y-%m-%d %H:%M\t", "%Y-%m-%d %H:%M  ")

    def test_the_shared_format_still_names_the_zone(self) -> None:
        """
        The shared format ends in ``%Z``, so the zone stays on the line.

        Dropping the zone would satisfy the trailing-space property and break
        the documented ``2026-09-16 14:03 CDT`` shape.
        """
        assert LOCAL_TIME_FORMAT.endswith("%Z")

    @pytest.mark.parametrize("fmt", WHITESPACE_FORMATS)
    def test_a_format_ending_in_whitespace_renders_clean(
        self, monkeypatch: pytest.MonkeyPatch, fmt: str
    ) -> None:
        """
        Whatever whitespace the format leaves dangling is not returned.

        Args:
            monkeypatch: pytest's patcher, used on the module global that
                ``local_time`` looks up at call time.
            fmt: A format standing in for one whose ``%Z`` rendered empty.

        """
        monkeypatch.setattr("saneless.vocabulary.LOCAL_TIME_FORMAT", fmt)

        rendered = local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC))

        assert rendered == rendered.rstrip()

    def test_the_rest_of_the_rendering_is_untouched(
        self, monkeypatch: pytest.MonkeyPatch, local_zone: Callable[[str], None]
    ) -> None:
        """
        Only the trailing run goes: the date, the time and their separator stay.

        Args:
            monkeypatch: pytest's patcher, used on the module global.
            local_zone: The fixture that pins the process's zone, so the exact
                string below does not depend on the host's zone.

        """
        local_zone("UTC")
        monkeypatch.setattr("saneless.vocabulary.LOCAL_TIME_FORMAT", "%Y-%m-%d %H:%M ")

        rendered = local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC))

        assert rendered == "2026-09-16 19:03"

    def test_a_leading_character_is_never_stripped(
        self, monkeypatch: pytest.MonkeyPatch, local_zone: Callable[[str], None]
    ) -> None:
        """
        The strip is trailing-only, so a leading space in a format survives.

        Args:
            monkeypatch: pytest's patcher, used on the module global.
            local_zone: The fixture that pins the process's zone.

        """
        local_zone("UTC")
        monkeypatch.setattr("saneless.vocabulary.LOCAL_TIME_FORMAT", " %Y-%m-%d %H:%M ")

        rendered = local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC))

        assert rendered == " 2026-09-16 19:03"

    def test_a_real_render_is_never_empty(
        self, local_zone: Callable[[str], None]
    ) -> None:
        """
        A valid aware datetime always renders something on a real host.

        Args:
            local_zone: The fixture that pins the process's zone.

        """
        local_zone("America/Chicago")

        rendered = local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC))

        assert rendered
        assert rendered == rendered.rstrip()


class TestPageCounts:
    """page_counts sentence tests."""

    def test_page_counts_sentence(self) -> None:
        """The three counts render as one sentence."""
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
        A measured 0 is a measurement and renders as 0.

        Rendering nothing is for a NULL count.  A scan where nothing was blank
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
        ("scanned", "removed", "uploaded"),
        [
            (None, 2, 10),
            (12, None, 10),
            (12, 2, None),
            (None, None, None),
        ],
    )
    def test_page_counts_is_none_when_any_count_is_null(
        self, scanned: int | None, removed: int | None, uploaded: int | None
    ) -> None:
        """One NULL count means nothing at all is rendered."""
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            pages_scanned=scanned,
            pages_removed=removed,
            pages_uploaded=uploaded,
        )
        assert page_counts(job) is None

    def test_page_counts_is_none_for_an_uncounted_job(self) -> None:
        """A job that recorded no counts, such as an ERROR row, renders none."""
        job = Job(id="j", profile="default", title="t", state=JobState.ERROR)
        assert page_counts(job) is None


class TestRemovedPagesNote:
    """The informational note naming the pages removed as blank."""

    def test_several_positions_are_listed_in_order(self) -> None:
        """Several removed pages are listed in order, with the plural noun."""
        assert (
            removed_pages_note((2, 4, 6), 12)
            == "Removed as blank: pages 2, 4, 6 of 12 scanned."
        )

    def test_one_position_takes_the_singular_noun(self) -> None:
        """A single removed page reads "page 3", not "pages 3"."""
        assert removed_pages_note((3,), 12) == "Removed as blank: page 3 of 12 scanned."

    @pytest.mark.parametrize(
        ("positions", "scanned"),
        [(None, 12), ((), 12), ((2,), None), (None, None)],
    )
    def test_nothing_to_say_is_none(
        self, positions: tuple[int, ...] | None, scanned: int | None
    ) -> None:
        """No positions, an empty list or no scanned count renders nothing."""
        assert removed_pages_note(positions, scanned) is None

    def test_removed_pages_reads_a_job(self) -> None:
        """removed_pages delegates to removed_pages_note with a job's two fields."""
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            pages_scanned=4,
            pages_removed=2,
            pages_uploaded=2,
            removed_positions=(2, 4),
        )
        assert removed_pages(job) == "Removed as blank: pages 2, 4 of 4 scanned."

    def test_removed_pages_is_none_for_a_job_without_positions(self) -> None:
        """A job that recorded no positions renders no note."""
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            pages_scanned=4,
            pages_removed=0,
            pages_uploaded=4,
        )
        assert removed_pages(job) is None

    def test_the_note_never_becomes_a_warning(self) -> None:
        """A DONE job with removed pages and no warning stays a plain DONE."""
        job = Job(
            id="j",
            profile="default",
            title="t",
            state=JobState.DONE,
            outcome=ScanOutcome.SUCCESS,
            pages_scanned=4,
            pages_removed=2,
            pages_uploaded=2,
            removed_positions=(2, 4),
        )
        assert job.warning is None
        assert job_label(job.state, job.warning) == state_label(JobState.DONE)


class TestBusyLine:
    """busy_line in-progress line tests."""

    @pytest.mark.parametrize("state", sorted(BUSY_STATES))
    def test_busy_line_falls_back_to_progress_label(self, state: JobState) -> None:
        """With nothing extra known, the busy line is the progress prose."""
        assert busy_line(state) == progress_label(state)

    def test_busy_line_names_the_job_ahead(self) -> None:
        """A queued job is told what it is waiting for."""
        assert busy_line(JobState.PENDING, queue_title="Tax return", queue_ahead=1) == (
            "Waiting for 'Tax return' to finish (1 ahead of you)"
        )

    def test_busy_line_counts_more_than_one_ahead(self) -> None:
        """The count is the number of jobs ahead, not a fixed word."""
        assert busy_line(JobState.PENDING, queue_title="Tax return", queue_ahead=2) == (
            "Waiting for 'Tax return' to finish (2 ahead of you)"
        )

    def test_busy_line_says_next_in_line_instead_of_zero_ahead(self) -> None:
        """
        "(0 ahead of you)" is never rendered.

        It is technically true and reads like a bug.
        """
        assert busy_line(JobState.PENDING, queue_title="Tax return", queue_ahead=0) == (
            "Waiting for 'Tax return' to finish (next in line)"
        )

    def test_busy_line_needs_both_halves_of_the_queue_position(self) -> None:
        """A title with no count is not enough to claim a position."""
        assert busy_line(JobState.PENDING, queue_title="Tax return") == progress_label(
            JobState.PENDING
        )

    def test_busy_line_shows_the_front_count(self) -> None:
        """Pass B names how many fronts are already scanned."""
        assert busy_line(JobState.SCANNING_REVERSE, front_pages=12) == (
            "Front: 12 pages · " + progress_label(JobState.SCANNING_REVERSE)
        )

    def test_busy_line_pluralises_the_front_count(self) -> None:
        """One front page is a page, not pages."""
        assert busy_line(JobState.SCANNING_REVERSE, front_pages=1) == (
            "Front: 1 page · " + progress_label(JobState.SCANNING_REVERSE)
        )

    def test_busy_line_omits_an_unknown_front_count(self) -> None:
        """An unknown front count renders no count at all."""
        assert busy_line(JobState.SCANNING_REVERSE, front_pages=None) == (
            progress_label(JobState.SCANNING_REVERSE)
        )

    def test_busy_line_shows_the_front_count_only_on_pass_b(self) -> None:
        """The front count belongs to SCANNING_REVERSE and no other state."""
        assert busy_line(JobState.SCANNING, front_pages=12) == progress_label(
            JobState.SCANNING
        )

    def test_busy_line_queue_position_wins_over_the_front_count(self) -> None:
        """The queue line is the first branch, whatever else is known."""
        assert busy_line(
            JobState.SCANNING_REVERSE,
            front_pages=12,
            queue_title="Tax return",
            queue_ahead=2,
        ) == ("Waiting for 'Tax return' to finish (2 ahead of you)")

    def test_busy_line_leads_with_the_pages_kept_so_far(self) -> None:
        """A later multi-page pass names how many pages the document holds."""
        assert busy_line(JobState.SCANNING, pages_kept=4) == (
            f"4 pages so far {_BUSY_SEPARATOR} " + progress_label(JobState.SCANNING)
        )

    def test_busy_line_pluralises_the_pages_kept(self) -> None:
        """One kept page is a page, not pages."""
        assert busy_line(JobState.SCANNING, pages_kept=1) == (
            f"1 page so far {_BUSY_SEPARATOR} " + progress_label(JobState.SCANNING)
        )

    @pytest.mark.parametrize("pages_kept", [0, None])
    def test_busy_line_omits_an_empty_or_unknown_pages_kept(
        self, pages_kept: int | None
    ) -> None:
        """With no page kept yet, the first pass reads as the plain progress prose."""
        assert busy_line(JobState.SCANNING, pages_kept=pages_kept) == (
            progress_label(JobState.SCANNING)
        )

    @pytest.mark.parametrize("state", sorted(set(JobState) - {JobState.SCANNING}))
    def test_busy_line_shows_the_pages_kept_only_while_scanning(
        self, state: JobState
    ) -> None:
        """The kept count belongs to SCANNING and no other state."""
        assert busy_line(state, pages_kept=4) == progress_label(state)

    def test_busy_line_front_count_ignores_pages_kept(self) -> None:
        """The manual-duplex front count is unchanged by a kept count."""
        assert busy_line(JobState.SCANNING_REVERSE, front_pages=12, pages_kept=4) == (
            f"Front: 12 pages {_BUSY_SEPARATOR} "
            + progress_label(JobState.SCANNING_REVERSE)
        )

    def test_busy_line_queue_position_wins_over_the_pages_kept(self) -> None:
        """The queue line is still the first branch."""
        assert busy_line(
            JobState.SCANNING,
            pages_kept=4,
            queue_title="Tax return",
            queue_ahead=0,
        ) == ("Waiting for 'Tax return' to finish (next in line)")


class TestWorkerHealth:
    """WorkerHealth membership and /health detail tests."""

    def test_worker_health_members(self) -> None:
        """WorkerHealth is exactly HEALTHY, DEGRADED and DOWN."""
        assert [health.value for health in WorkerHealth] == [
            "HEALTHY",
            "DEGRADED",
            "DOWN",
        ]

    @pytest.mark.parametrize("health", list(WorkerHealth))
    def test_worker_health_value_equals_name(self, health: WorkerHealth) -> None:
        """Every WorkerHealth value is identical to its member name."""
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
        """worker_health_detail returns the documented /health detail."""
        assert worker_health_detail(health) == expected

    @pytest.mark.parametrize("health", list(WorkerHealth))
    def test_worker_health_detail_is_complete(self, health: WorkerHealth) -> None:
        """Every WorkerHealth has a non-empty detail string."""
        assert worker_health_detail(health)

    def test_worker_health_detail_raises_on_unrecognised_value(self) -> None:
        """worker_health_detail raises on a value outside WorkerHealth."""
        bad = cast("WorkerHealth", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            worker_health_detail(bad)


class TestSubmitResult:
    """SubmitResult names the four answers a submit can get, each valued as its name."""

    def test_submit_result_members(self) -> None:
        """SubmitResult is exactly ACCEPTED, QUEUE_FULL, DOWN, DEGRADED."""
        assert [result.value for result in SubmitResult] == [
            "ACCEPTED",
            "QUEUE_FULL",
            "DOWN",
            "DEGRADED",
        ]

    @pytest.mark.parametrize("result", list(SubmitResult))
    def test_submit_result_value_equals_name(self, result: SubmitResult) -> None:
        """Every SubmitResult value is identical to its member name."""
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
        RequestRejection.URL_UNSET,
        "The paperless-ngx address has not been set, so the scan was not "
        "started. Set paperless.url in the saneless config file, then restart "
        "saneless.",
    ),
    (
        RequestRejection.UNKNOWN_PROFILE,
        "That scan profile does not exist. Reload the page to see the current "
        "profiles.",
    ),
    (
        RequestRejection.MULTI_PAGE_MANUAL_DUPLEX,
        "Multiple pages is not available with manual duplex, so the scan was not "
        "started. Untick Multiple pages or choose another profile, then try again.",
    ),
    (
        RequestRejection.TITLE_TOO_LONG,
        "The title is too long. Shorten it to 118 characters or fewer.",
    ),
    (
        RequestRejection.TITLE_HAS_CONTROL,
        "The title contains a tab or another control character. Remove it, then "
        "try again.",
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
    (
        RequestRejection.HOST_NOT_ALLOWED,
        "saneless does not answer to this address. Add the host name you used "
        "to [web] allowed_hosts in the saneless config file, then restart "
        "saneless.",
    ),
]

_REJECTION_STATUS_CODES: list[tuple[RequestRejection, int]] = [
    (RequestRejection.QUEUE_FULL, 429),
    (RequestRejection.WORKER_DOWN, 503),
    (RequestRejection.WORKER_DEGRADED, 503),
    (RequestRejection.TOKEN_UNSET, 503),
    (RequestRejection.URL_UNSET, 503),
    (RequestRejection.UNKNOWN_PROFILE, 422),
    (RequestRejection.MULTI_PAGE_MANUAL_DUPLEX, 422),
    (RequestRejection.TITLE_TOO_LONG, 422),
    (RequestRejection.TITLE_HAS_CONTROL, 422),
    (RequestRejection.INVALID_REQUEST, 422),
    (RequestRejection.CROSS_SITE, 403),
    (RequestRejection.NOT_FOUND, 404),
    (RequestRejection.METHOD_NOT_ALLOWED, 405),
    (RequestRejection.INTERNAL, 500),
    (RequestRejection.CLIENT_ERROR, 400),
    (RequestRejection.HOST_NOT_ALLOWED, 421),
]

_JOB_ROW_TEXTS: list[str] = [
    QUEUE_FULL_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    WORKER_DEGRADED_JOB_ERROR,
    TOKEN_UNSET_JOB_ERROR,
    URL_UNSET_JOB_ERROR,
    RESTART_REASON,
    RESTART_UPLOADING_REASON,
]


class TestRequestRejection:
    """RequestRejection membership, message, status code and job-row copy tests."""

    def test_request_rejection_members(self) -> None:
        """RequestRejection names one member per rendered error."""
        assert {rejection.name for rejection in RequestRejection} == {
            "QUEUE_FULL",
            "WORKER_DOWN",
            "WORKER_DEGRADED",
            "TOKEN_UNSET",
            "URL_UNSET",
            "UNKNOWN_PROFILE",
            "MULTI_PAGE_MANUAL_DUPLEX",
            "TITLE_TOO_LONG",
            "TITLE_HAS_CONTROL",
            "INVALID_REQUEST",
            "CROSS_SITE",
            "NOT_FOUND",
            "METHOD_NOT_ALLOWED",
            "INTERNAL",
            "CLIENT_ERROR",
            "HOST_NOT_ALLOWED",
        }

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_request_rejection_value_equals_name(
        self, rejection: RequestRejection
    ) -> None:
        """Every RequestRejection value is identical to its member name."""
        assert rejection.value == rejection.name

    def test_message_table_covers_every_member(self) -> None:
        """The pinned message table names every RequestRejection member."""
        assert {rejection for rejection, _ in _REJECTION_MESSAGES} == set(
            RequestRejection
        )

    def test_status_code_table_covers_every_member(self) -> None:
        """The pinned status-code table names every RequestRejection member."""
        assert {rejection for rejection, _ in _REJECTION_STATUS_CODES} == set(
            RequestRejection
        )

    @pytest.mark.parametrize(("rejection", "expected"), _REJECTION_MESSAGES)
    def test_rejection_message_strings(
        self, rejection: RequestRejection, expected: str
    ) -> None:
        """rejection_message returns the approved copy verbatim."""
        assert rejection_message(rejection) == expected

    @pytest.mark.parametrize(("rejection", "expected"), _REJECTION_STATUS_CODES)
    def test_rejection_status_codes(
        self, rejection: RequestRejection, expected: int
    ) -> None:
        """rejection_status_code returns the documented HTTP status."""
        assert rejection_status_code(rejection) == expected

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_message_is_complete(self, rejection: RequestRejection) -> None:
        """Every RequestRejection has a non-empty message."""
        assert rejection_message(rejection)

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_status_code_is_complete(
        self, rejection: RequestRejection
    ) -> None:
        """Every RequestRejection has an HTTP error status."""
        assert 400 <= rejection_status_code(rejection) <= 599

    @pytest.mark.parametrize("rejection", list(RequestRejection))
    def test_rejection_message_style(self, rejection: RequestRejection) -> None:
        """Every rejection message ends with a period and has no "!"."""
        message = rejection_message(rejection)
        assert message.endswith(".")
        assert "!" not in message

    def test_rejection_message_raises_on_unrecognised_value(self) -> None:
        """rejection_message raises on a value outside RequestRejection."""
        bad = cast("RequestRejection", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            rejection_message(bad)

    def test_rejection_status_code_raises_on_unrecognised_value(self) -> None:
        """rejection_status_code raises on a value outside RequestRejection."""
        bad = cast("RequestRejection", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            rejection_status_code(bad)

    def test_title_max_length_is_what_paperless_keeps_after_a_half_suffix(
        self,
    ) -> None:
        """
        The title cap is 118: paperless-ngx keeps 127 characters of a title.

        A split duplex job appends " (fronts)" or " (backs)" to the title it
        sends, so the cap leaves room for the longer of the two, and a title at
        the cap still reaches paperless-ngx whole.
        """
        assert TITLE_MAX_LENGTH == 118
        assert PAPERLESS_TITLE_LIMIT == 127
        longest_suffix = max(
            len(half_title("", suffix)) for suffix in (FRONTS_SUFFIX, BACKS_SUFFIX)
        )
        assert PAPERLESS_TITLE_LIMIT - longest_suffix == TITLE_MAX_LENGTH

    def test_half_title_joins_the_title_and_the_suffix(self) -> None:
        """A half's title is the operator's title, a space, then the suffix."""
        assert TITLE_SUFFIX_SEPARATOR == " "
        assert half_title("T", FRONTS_SUFFIX) == "T (fronts)"
        assert half_title("T", BACKS_SUFFIX) == "T (backs)"

    def test_half_title_at_the_cap_fits_what_paperless_keeps(self) -> None:
        """The longest half title a capped title can make is exactly 127 long."""
        title = "x" * TITLE_MAX_LENGTH
        assert len(half_title(title, FRONTS_SUFFIX)) == PAPERLESS_TITLE_LIMIT
        assert len(half_title(title, BACKS_SUFFIX)) < PAPERLESS_TITLE_LIMIT

    def test_title_suffixes_are_spelled_once(self) -> None:
        """The three suffixes live in vocabulary; preservation re-exports them."""
        assert PARTIAL_SUFFIX == "(partial)"
        assert FRONTS_SUFFIX == "(fronts)"
        assert BACKS_SUFFIX == "(backs)"
        assert preservation.FRONTS_SUFFIX is FRONTS_SUFFIX
        assert preservation.BACKS_SUFFIX is BACKS_SUFFIX
        assert preservation.PARTIAL_SUFFIX is PARTIAL_SUFFIX
        assert {"FRONTS_SUFFIX", "BACKS_SUFFIX", "PARTIAL_SUFFIX"} <= set(
            preservation.__all__
        )

    def test_title_too_long_message_reads_the_cap(self) -> None:
        """The TITLE_TOO_LONG message names the same cap the form enforces."""
        assert str(TITLE_MAX_LENGTH) in rejection_message(
            RequestRejection.TITLE_TOO_LONG
        )

    def test_job_row_texts(self) -> None:
        """The rejected-row texts and the restart reason are the approved copy."""
        assert QUEUE_FULL_JOB_ERROR == "Not started: the scan queue was full"
        assert WORKER_DOWN_JOB_ERROR == "Not started: the scan service was not running"
        assert WORKER_DEGRADED_JOB_ERROR == (
            "Not started: the scan service was unavailable"
        )
        assert RESTART_REASON == "The server restarted before this scan finished"

    @pytest.mark.parametrize("text", _JOB_ROW_TEXTS)
    def test_job_row_texts_have_no_trailing_period(self, text: str) -> None:
        """Job-row texts follow the job.error convention of no trailing period."""
        assert text
        assert not text.endswith(".")


_KEPT_SENTENCE = "The scan was preserved at /data/failed/kept.pdf"
"""A kept-file sentence, as the startup sweep words one."""


class TestRestartWording:
    """The text and category a job gets when a restart or stop ends it."""

    def test_restart_uploading_reason_says_it_may_have_arrived(self) -> None:
        """An upload cut short by a restart may already be in paperless-ngx."""
        assert "may have reached paperless-ngx" in RESTART_UPLOADING_REASON
        assert "before this scan finished" not in RESTART_UPLOADING_REASON

    def test_restart_error_while_uploading(self) -> None:
        """An uploading job gets the uploading reason, alone or before the kept file."""
        assert restart_error(JobState.UPLOADING, None) == RESTART_UPLOADING_REASON
        assert restart_error(JobState.UPLOADING, "kept.") == (
            f"{RESTART_UPLOADING_REASON}. kept."
        )

    def test_restart_error_before_uploading(self) -> None:
        """A job not yet uploading keeps the plain restart reason."""
        assert restart_error(JobState.SCANNING, None) == RESTART_REASON
        assert restart_error(JobState.ASSEMBLING, "kept.") == f"{RESTART_REASON}. kept."

    @pytest.mark.parametrize(
        "state", sorted(ACTIVE_STATES - {JobState.UPLOADING}, key=str)
    )
    def test_restart_error_for_every_other_active_state(self, state: JobState) -> None:
        """Every active state before the upload reads as never finished."""
        assert restart_error(state, None) == RESTART_REASON
        assert restart_error(state, _KEPT_SENTENCE) == (
            f"{RESTART_REASON}. {_KEPT_SENTENCE}"
        )

    def test_restart_category_while_uploading_is_unconfirmed_send(self) -> None:
        """An uploading job restarted is the amber after-send category."""
        assert restart_category(JobState.UPLOADING) is ErrorCategory.UNCONFIRMED_SEND

    @pytest.mark.parametrize(
        "state", sorted(ACTIVE_STATES - {JobState.UPLOADING}, key=str)
    )
    def test_restart_category_for_every_other_active_state_is_none(
        self, state: JobState
    ) -> None:
        """A job restarted before the upload carries no category, only its text."""
        assert restart_category(state) is None

    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=str))
    def test_restart_category_refuses_a_terminal_state(self, state: JobState) -> None:
        """A finished job is never restarted, so asking about one is a caller bug."""
        with pytest.raises(ValueError, match=state.value):
            restart_category(state)

    @pytest.mark.parametrize("state", sorted(TERMINAL_STATES, key=str))
    def test_restart_error_refuses_a_terminal_state(self, state: JobState) -> None:
        """The text follows the same rule as the category."""
        with pytest.raises(ValueError, match=state.value):
            restart_error(state, None)

    def test_restart_category_raises_on_unrecognised_value(self) -> None:
        """A value outside JobState fails loudly rather than choosing a category."""
        bad = cast("JobState", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            restart_category(bad)


class TestDroppedIdsWarning:
    """A stale id dropped before scanning is named in one sentence."""

    def test_dropped_single_tag(self) -> None:
        """One tag: the sentence the operator reads on every surface."""
        assert dropped_ids_warning((9,), None) == (
            "tag 9 no longer exists in paperless-ngx and was not applied."
        )

    def test_dropped_single_correspondent(self) -> None:
        """One correspondent reads the same way."""
        assert dropped_ids_warning((), 12) == (
            "correspondent 12 no longer exists in paperless-ngx and was not applied."
        )

    def test_dropped_tags_and_correspondent_are_one_sentence(self) -> None:
        """Every dropped id is listed once, in a single sentence."""
        assert dropped_ids_warning((3, 9), 12) == (
            "tags 3 and 9, and correspondent 12, no longer exist in paperless-ngx "
            "and were not applied."
        )

    def test_dropped_three_tags_and_one_with_a_correspondent(self) -> None:
        """Longer lists use commas; one tag and a correspondent need none."""
        assert dropped_ids_warning((3, 7, 9), None) == (
            "tags 3, 7 and 9 no longer exist in paperless-ngx and were not applied."
        )
        assert dropped_ids_warning((9,), 12) == (
            "tag 9 and correspondent 12 no longer exist in paperless-ngx and were "
            "not applied."
        )

    def test_nothing_dropped_is_no_warning(self) -> None:
        """Nothing dropped: no warning at all, so the DONE stays clean."""
        assert dropped_ids_warning((), None) is None


class TestStaleDefaultAndUnlistedLabels:
    """The labels the form puts on a ticked id it cannot name from the list."""

    def test_stale_default_tag_label_says_it_will_be_skipped(self) -> None:
        """A tag missing from a list that was read is named with the note."""
        assert stale_default_tag_label(7) == (
            "tag 7 (no longer in paperless-ngx; will be skipped)"
        )

    def test_stale_default_correspondent_label_is_the_tag_label_s_twin(
        self,
    ) -> None:
        """The correspondent reads the same way, with its own noun."""
        assert stale_default_correspondent_label(12) == (
            "correspondent 12 (no longer in paperless-ngx; will be skipped)"
        )

    def test_unlisted_labels_claim_nothing_about_paperless(self) -> None:
        """Without a list, the id is all the page can honestly say."""
        assert unlisted_tag_label(7) == "tag 7"
        assert unlisted_correspondent_label(12) == "correspondent 12"


class TestDuplicateWarning:
    """A duplicate refusal is a delivered scan, worded as such."""

    def test_duplicate_warning_names_the_document(self) -> None:
        """The id, what did not happen, and nothing that invites a rescan."""
        warning = duplicate_warning(42, in_trash=False)
        assert warning == (
            "paperless-ngx already holds this file as document #42; it was not "
            "stored again, and this scan's title and tags were not applied to it."
        )

    def test_duplicate_warning_without_an_id_says_an_existing_document(
        self,
    ) -> None:
        """No id in the answer is never "document #None"."""
        warning = duplicate_warning(None, in_trash=False)
        assert "an existing document" in warning
        assert "#" not in warning
        assert "None" not in warning
        assert "was not stored again" in warning

    @pytest.mark.parametrize("document_id", [42, None])
    def test_duplicate_warning_in_the_trash_says_so(
        self, document_id: int | None
    ) -> None:
        """A trashed original is where the user must look for it."""
        warning = duplicate_warning(document_id, in_trash=True)
        assert warning.startswith(duplicate_warning(document_id, in_trash=False))
        assert warning.endswith("That document is in paperless-ngx's trash.")

    def test_duplicate_warning_can_name_the_half(self) -> None:
        """A split duplex job says which of its two PDFs was the duplicate."""
        warning = duplicate_warning(42, in_trash=False, half="(backs)")
        assert warning.startswith(
            "paperless-ngx already holds the (backs) half as document #42;"
        )

    def test_duplicate_warning_never_says_scan_again(self) -> None:
        """The document is there: the warning must not send the user to rescan."""
        for document_id in (42, None):
            for in_trash in (False, True):
                warning = duplicate_warning(document_id, in_trash=in_trash).casefold()
                assert "scan again" not in warning
                assert "start the scan" not in warning


class TestHalfDeliveryError:
    """A split duplex job names the half that reached paperless-ngx."""

    def test_per_half_error_names_both_halves_and_the_reason(self) -> None:
        """Which half is in paperless-ngx, which is not, and why."""
        assert half_delivery_error("(fronts)", "(backs)", "refused 400") == (
            "The (fronts) half reached paperless-ngx; the (backs) half failed: "
            "refused 400"
        )


class TestConnectionStatus:
    """ConnectionStatus membership, wire-value and message tests."""

    def test_connection_status_has_exactly_eight_members(self) -> None:
        """
        ConnectionStatus declares exactly eight outcomes.

        A count guard, not a name list: adding a member should fail the
        parametrised completeness test below -- which forces a user-facing
        message -- rather than a hand-written roster that only records what the
        enum happened to contain when it was written.
        """
        assert len(list(ConnectionStatus)) == 8

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (ConnectionStatus.CONNECTED, "connected"),
            (ConnectionStatus.TOKEN_REJECTED, "token_rejected"),
            (ConnectionStatus.NOT_FOUND, "not_found"),
            (ConnectionStatus.SERVER_ERROR, "server_error"),
            (ConnectionStatus.UNREACHABLE, "unreachable"),
            (ConnectionStatus.INCOMPATIBLE, "incompatible_version"),
            (ConnectionStatus.REDIRECTED, "redirected"),
            (ConnectionStatus.MISCONFIGURED, "misconfigured"),
        ],
    )
    def test_connection_status_wire_values(
        self,
        status: ConnectionStatus,
        expected: str,
    ) -> None:
        """
        Each member serialises to the string the web API documents.

        Compared against plain str literals, not against other enum members:
        `GET /api/paperless/test` puts this value straight into a JSON body and
        `docs/reference/web-api.md` documents the exact spelling, so what this
        test has to prove is wire compatibility, not enum identity.
        """
        assert status == expected

    def test_legacy_wire_values_are_unchanged(self) -> None:
        """The connected, token_rejected and unreachable strings are spelled exactly."""
        assert ConnectionStatus.CONNECTED == "connected"
        assert ConnectionStatus.TOKEN_REJECTED == "token_rejected"
        assert ConnectionStatus.UNREACHABLE == "unreachable"

    @pytest.mark.parametrize("status", list(ConnectionStatus))
    def test_json_round_trip_needs_no_custom_encoder(
        self,
        status: ConnectionStatus,
    ) -> None:
        """A member serialises as its bare string with the stdlib encoder."""
        assert json.dumps({"status": status}) == json.dumps({"status": status.value})

    def test_connected_serialises_to_the_documented_body(self) -> None:
        """The success body, as a JSON response renders it, is what web-api.md shows."""
        assert (
            JSONResponse({"status": ConnectionStatus.CONNECTED}).body
            == b'{"status":"connected"}'
        )

    @pytest.mark.parametrize("status", list(ConnectionStatus))
    def test_connection_status_message_is_complete(
        self,
        status: ConnectionStatus,
    ) -> None:
        """Every ConnectionStatus has a message that is not its raw value."""
        message = connection_status_message(status)
        assert message
        assert message != status.value

    def test_connection_status_message_strings(self) -> None:
        """connection_status_message returns developer-authored prose."""
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
        assert connection_status_message(ConnectionStatus.INCOMPATIBLE) == (
            "This paperless-ngx does not speak an API version saneless supports."
        )
        assert connection_status_message(ConnectionStatus.REDIRECTED) == (
            "Paperless-ngx answered from a different address."
        )
        assert connection_status_message(ConnectionStatus.MISCONFIGURED) == (
            "The paperless-ngx address or API token in the saneless config "
            "cannot be used."
        )

    def test_connection_status_message_raises_on_unrecognised_value(self) -> None:
        """connection_status_message raises on a value outside the enum."""
        bad = cast("ConnectionStatus", "teapot")
        with pytest.raises(AssertionError):
            connection_status_message(bad)


class TestJobStateFor:
    """job_state_for outcome-to-state mapping tests."""

    @pytest.mark.parametrize("outcome", list(ScanOutcome))
    def test_job_state_for_is_total(self, outcome: ScanOutcome) -> None:
        """Every ScanOutcome maps to a state."""
        assert isinstance(job_state_for(outcome), JobState)

    @pytest.mark.parametrize("outcome", list(ScanOutcome))
    def test_job_state_for_always_lands_in_terminal_states(
        self,
        outcome: ScanOutcome,
    ) -> None:
        """
        An outcome always maps to a finished job.

        A ScanOutcome only exists once the pipeline has resolved, so mapping one
        onto an ACTIVE_STATES member would mean the worker wrote "still in
        flight" over a job that is done.
        """
        assert job_state_for(outcome) in TERMINAL_STATES

    def test_success_maps_to_done(self) -> None:
        """SUCCESS is the ordinary finished job."""
        assert job_state_for(ScanOutcome.SUCCESS) is JobState.DONE

    def test_fallback_maps_to_fallback(self) -> None:
        """FALLBACK gets its own state rather than being folded into DONE."""
        assert job_state_for(ScanOutcome.FALLBACK) is JobState.FALLBACK

    def test_job_state_for_raises_on_unrecognised_value(self) -> None:
        """job_state_for raises on a value outside ScanOutcome."""
        bad = cast("ScanOutcome", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            job_state_for(bad)


class TestUnrecognisedValue:
    """Every total lookup raises on a value outside its enum."""

    def test_state_label_raises_on_unrecognised_value(self) -> None:
        """
        state_label raises rather than echoing an unknown value back.

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
        """progress_label raises on a value outside JobState."""
        bad = cast("JobState", "UNKNOWN")
        with pytest.raises(AssertionError):
            progress_label(bad)

    def test_error_message_raises_on_unrecognised_value(self) -> None:
        """error_message raises on a value outside ErrorCategory."""
        bad = cast("ErrorCategory", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            error_message(bad)


class TestClassifyError:
    """classify_error exception-to-category mapping tests."""

    def test_feeder_empty_error_is_feeder(self) -> None:
        """FeederEmptyError classifies as FEEDER."""
        assert classify_error(FeederEmptyError("no paper")) is ErrorCategory.FEEDER

    def test_config_error_is_config(self) -> None:
        """ConfigError classifies as CONFIG."""
        assert classify_error(ConfigError("bad toml")) is ErrorCategory.CONFIG

    def test_scan_error_is_scanner(self) -> None:
        """ScanError classifies as SCANNER."""
        assert classify_error(ScanError("device busy")) is ErrorCategory.SCANNER

    def test_paperless_error_is_upload(self) -> None:
        """PaperlessError classifies as UPLOAD."""
        assert classify_error(PaperlessError("http 500")) is ErrorCategory.UPLOAD

    def test_unrelated_exception_is_unknown(self) -> None:
        """An exception outside the saneless hierarchy is UNKNOWN."""
        assert classify_error(ValueError("who knows")) is ErrorCategory.UNKNOWN

    def test_paperless_timeout_error_subclasses_paperless_error(self) -> None:
        """PaperlessTimeoutError narrows PaperlessError rather than SanelessError."""
        assert issubclass(PaperlessTimeoutError, PaperlessError)

    def test_paperless_timeout_error_is_unconfirmed_filing(self) -> None:
        """
        A poll that ran out after acceptance is unconfirmed, not a failed upload.

        paperless-ngx answered the upload with a task id, so it holds the
        document; only the confirmation that it was filed never came.  A
        40-page OCR can outlast the task timeout.  Reported as a plain upload
        failure, it would tell the reader to scan again and store the document
        twice.  ``PaperlessTimeoutError`` subclasses
        ``PaperlessUnconfirmedError``, so the arm for that class covers it with
        no arm of its own.
        """
        assert issubclass(PaperlessTimeoutError, PaperlessUnconfirmedError)
        assert (
            classify_error(PaperlessTimeoutError("timed out"))
            is ErrorCategory.UNCONFIRMED_FILING
        )

    def test_uncertain_send_is_unconfirmed_send(self) -> None:
        """An upload whose answer never came back may be in paperless-ngx."""
        assert (
            classify_error(PaperlessUncertainSendError("x"))
            is ErrorCategory.UNCONFIRMED_SEND
        )

    def test_unconfirmed_is_unconfirmed_filing(self) -> None:
        """An accepted upload that was never confirmed filed is amber, not red."""
        assert (
            classify_error(PaperlessUnconfirmedError("x"))
            is ErrorCategory.UNCONFIRMED_FILING
        )

    def test_incompatible_is_paperless_version(self) -> None:
        """A refused API version gets its own advice, not the token advice."""
        assert (
            classify_error(PaperlessIncompatibleError("x"))
            is ErrorCategory.PAPERLESS_VERSION
        )

    @pytest.mark.parametrize(
        "exc_type",
        [
            PaperlessUncertainSendError,
            PaperlessUnconfirmedError,
            PaperlessIncompatibleError,
        ],
    )
    def test_narrower_paperless_types_subclass_paperless_error(
        self, exc_type: type[PaperlessError]
    ) -> None:
        """Every narrower paperless failure is still caught as a PaperlessError."""
        assert issubclass(exc_type, PaperlessError)

    def test_plain_paperless_error_is_still_upload(self) -> None:
        """A plain PaperlessError is UPLOAD; the narrower arms do not catch it."""
        assert classify_error(PaperlessError("x")) is ErrorCategory.UPLOAD

    def test_disk_space_error_is_disk_space(self) -> None:
        """A full disk is its own category, never SCANNER or ASSEMBLY."""
        assert classify_error(DiskSpaceError("x")) is ErrorCategory.DISK_SPACE

    def test_no_scanner_found_error_is_scanner(self) -> None:
        """Finding no scanner is a scanner condition, not a configuration one."""
        assert classify_error(NoScannerFoundError("x")) is ErrorCategory.SCANNER

    def test_feeder_empty_wins_over_its_scan_error_base(self) -> None:
        """FeederEmptyError is checked before its ScanError base class."""
        assert issubclass(FeederEmptyError, ScanError)
        assert classify_error(FeederEmptyError("no paper")) is ErrorCategory.FEEDER

    def test_classify_error_pdf_error_is_assembly(self) -> None:
        """PdfError classifies as ASSEMBLY, never as SCANNER."""
        assert classify_error(PdfError("disk full")) is ErrorCategory.ASSEMBLY

    def test_classify_error_scan_cancelled_error_is_unknown(self) -> None:
        """
        ScanCancelledError has no category of its own.

        A cancel is not a failure category: callers test for it before they
        classify, so reaching ``classify_error`` with one is already a bug and
        UNKNOWN is the honest answer.
        """
        assert classify_error(ScanCancelledError("stopped")) is ErrorCategory.UNKNOWN

    def test_all_pages_blank_error_is_all_blank(self) -> None:
        """AllPagesBlankError classifies as ALL_BLANK, never SCANNER."""
        assert classify_error(AllPagesBlankError("x")) is ErrorCategory.ALL_BLANK

    def test_classify_error_storage_error_is_unknown(self) -> None:
        """
        StorageError stays UNKNOWN; its exit code 2 is assigned by type.

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
    (ErrorCategory.ALL_BLANK, ExitCode.ALL_BLANK),
    (ErrorCategory.UNCONFIRMED_SEND, ExitCode.UNCONFIRMED),
    (ErrorCategory.UNCONFIRMED_FILING, ExitCode.UNCONFIRMED),
    (ErrorCategory.PAPERLESS_VERSION, ExitCode.PAPERLESS),
    (ErrorCategory.DISK_SPACE, ExitCode.DISK_SPACE),
]


class TestExitCode:
    """ExitCode definition and exit_code_for mapping tests."""

    def test_exit_code_members_and_values(self) -> None:
        """
        ExitCode is the one definition of the CLI exit codes.

        The pinned set lists all fifteen members: saneless's own codes 0 to
        10, then the shell-convention codes for SIGHUP (129), Ctrl-C (130),
        a broken pipe (141) and SIGTERM (143).
        """
        assert {(member.name, int(member)) for member in ExitCode} == {
            ("SUCCESS", 0),
            ("SCAN", 1),
            ("CONFIG", 2),
            ("PAPERLESS", 3),
            ("PDF", 4),
            ("UNEXPECTED", 5),
            ("SAVED_TO_FOLDER", 6),
            ("UPLOADED_WITH_WARNING", 7),
            ("ALL_BLANK", 8),
            ("UNCONFIRMED", 9),
            ("DISK_SPACE", 10),
            ("HANGUP", 129),
            ("CANCELLED", 130),
            ("BROKEN_PIPE", 141),
            ("TERMINATED", 143),
        }

    def test_exit_code_members_are_declared_in_ascending_value_order(self) -> None:
        """
        The enum lists its codes in value order, as every table pinned to it does.

        saneless's own small codes come first, then the shell-convention codes
        129, 130, 141 and 143, each 128 plus a signal number.
        """
        values = [int(member) for member in ExitCode]
        assert values == sorted(values)

    def test_broken_pipe_is_the_shells_sigpipe_code(self) -> None:
        """A reader that went away exits 141, 128 plus SIGPIPE, as a shell's would."""
        assert int(ExitCode.BROKEN_PIPE) == 128 + signal.SIGPIPE == 141


class TestExitCodeForSignal:
    """exit_code_for_signal maps an interrupting signal to 128 + its number."""

    def test_sighup_is_hangup(self) -> None:
        """A dropped SSH session's SIGHUP exits 129."""
        assert exit_code_for_signal(signal.SIGHUP) is ExitCode.HANGUP
        assert int(ExitCode.HANGUP) == 128 + signal.SIGHUP

    def test_sigterm_is_terminated(self) -> None:
        """A SIGTERM exits 143."""
        assert exit_code_for_signal(signal.SIGTERM) is ExitCode.TERMINATED
        assert int(ExitCode.TERMINATED) == 128 + signal.SIGTERM

    @pytest.mark.parametrize("signum", [None, signal.SIGUSR1])
    def test_anything_else_is_unexpected(self, signum: int | None) -> None:
        """
        A signal saneless installs no handler for cannot legitimately get here.

        ``None`` is the server stopping, which the CLI never sees; any other
        signal would be a bug, so it exits as one.
        """
        assert exit_code_for_signal(signum) is ExitCode.UNEXPECTED

    def test_exit_code_table_covers_every_category(self) -> None:
        """The pinned mapping table names every ErrorCategory member."""
        assert {category for category, _ in _EXIT_CODES_FOR_CATEGORIES} == set(
            ErrorCategory
        )

    @pytest.mark.parametrize(("category", "expected"), _EXIT_CODES_FOR_CATEGORIES)
    def test_exit_code_for_mapping(
        self, category: ErrorCategory, expected: ExitCode
    ) -> None:
        """exit_code_for returns the documented exit code per category."""
        assert exit_code_for(category) is expected

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_exit_code_for_is_total(self, category: ErrorCategory) -> None:
        """Every ErrorCategory maps to an ExitCode."""
        assert isinstance(exit_code_for(category), ExitCode)

    def test_exit_code_for_raises_on_unrecognised_value(self) -> None:
        """exit_code_for raises on a value outside ErrorCategory."""
        bad = cast("ErrorCategory", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            exit_code_for(bad)


_EXIT_CODES_FOR_OUTCOMES: list[tuple[ScanOutcome, str | None, ExitCode]] = [
    (ScanOutcome.SUCCESS, None, ExitCode.SUCCESS),
    (ScanOutcome.SUCCESS, "", ExitCode.SUCCESS),
    (ScanOutcome.SUCCESS, "w", ExitCode.UPLOADED_WITH_WARNING),
    (ScanOutcome.FALLBACK, None, ExitCode.SAVED_TO_FOLDER),
    (ScanOutcome.FALLBACK, "", ExitCode.SAVED_TO_FOLDER),
    (ScanOutcome.FALLBACK, "w", ExitCode.SAVED_TO_FOLDER),
]


class TestExitCodeForOutcome:
    """exit_code_for_outcome delivered-outcome mapping tests."""

    def test_exit_code_for_outcome_table_covers_every_outcome(self) -> None:
        """The pinned mapping table names every ScanOutcome member."""
        assert {outcome for outcome, _, _ in _EXIT_CODES_FOR_OUTCOMES} == set(
            ScanOutcome
        )

    @pytest.mark.parametrize(
        ("outcome", "warning", "expected"), _EXIT_CODES_FOR_OUTCOMES
    )
    def test_exit_code_for_outcome_mapping(
        self, outcome: ScanOutcome, warning: str | None, expected: ExitCode
    ) -> None:
        """
        A delivered document exits 0 only when nothing went wrong on the way.

        An empty warning is no warning.  A FALLBACK exits 6 whether or not it
        also carries a warning: the missing title, tags and correspondent are
        the larger problem, and one process can only report one code.
        """
        assert exit_code_for_outcome(outcome, warning) is expected

    @pytest.mark.parametrize("outcome", list(ScanOutcome))
    @pytest.mark.parametrize("warning", [None, "", "w"])
    def test_exit_code_for_outcome_is_never_a_failure_code(
        self, outcome: ScanOutcome, warning: str | None
    ) -> None:
        """
        A delivered outcome never borrows a failure code.

        A script that retries on 3 would scan the stack a second time if a
        document that reached paperless-ngx or its consume folder exited 3.
        """
        assert exit_code_for_outcome(outcome, warning) in {
            ExitCode.SUCCESS,
            ExitCode.SAVED_TO_FOLDER,
            ExitCode.UPLOADED_WITH_WARNING,
        }

    def test_exit_code_for_outcome_raises_on_unrecognised_value(self) -> None:
        """exit_code_for_outcome raises on a value outside ScanOutcome."""
        bad = cast("ScanOutcome", "UNRECOGNISED")
        with pytest.raises(AssertionError):
            exit_code_for_outcome(bad, None)


class TestJobLabel:
    """job_label history-row label tests."""

    def test_job_label_warned_done(self) -> None:
        """A DONE job that carries a warning is not labelled "Complete"."""
        assert job_label(JobState.DONE, "w") == "Uploaded with a warning"

    def test_job_label_warned_done_reads_the_constant(self) -> None:
        """The warned label is the one module constant, not a second spelling."""
        assert job_label(JobState.DONE, "w") == WARNED_UPLOAD_LABEL
        assert WARNED_UPLOAD_LABEL == "Uploaded with a warning"

    @pytest.mark.parametrize("warning", [None, ""])
    def test_job_label_clean_done(self, warning: str | None) -> None:
        """A DONE job with no warning, or an empty one, is "Complete"."""
        assert job_label(JobState.DONE, warning) == "Complete"

    def test_job_label_warned_fallback(self) -> None:
        """A FALLBACK keeps "Saved to folder" whether or not it has a warning."""
        assert job_label(JobState.FALLBACK, "w") == "Saved to folder"

    @pytest.mark.parametrize(
        "state", [state for state in JobState if state is not JobState.DONE]
    )
    @pytest.mark.parametrize("warning", [None, "", "w"])
    def test_job_label_delegates_to_state_label(
        self, state: JobState, warning: str | None
    ) -> None:
        """Every state but a warned DONE is labelled exactly as state_label does."""
        assert job_label(state, warning) == state_label(state)

    def test_job_label_raises_on_unrecognised_value(self) -> None:
        """job_label raises on a value outside JobState, as state_label does."""
        bad = cast("JobState", "UNKNOWN")
        with pytest.raises(AssertionError):
            job_label(bad, None)

    @pytest.mark.parametrize(
        ("category", "label"),
        [
            (ErrorCategory.UNCONFIRMED_SEND, UNCONFIRMED_SEND_LABEL),
            (ErrorCategory.UNCONFIRMED_FILING, UNCONFIRMED_FILING_LABEL),
        ],
    )
    def test_job_label_amber_error_names_its_category(
        self, category: ErrorCategory, label: str
    ) -> None:
        """A failure that may have reached paperless-ngx is never "Failed"."""
        assert job_label(JobState.ERROR, None, category) == label

    def test_job_label_amber_labels_pin_their_words(self) -> None:
        """
        The two amber labels read as the outcome, not as a failure.

        Neither is wider than the widest state label, so the status column
        of `saneless jobs` -- sized so a row fits 80 columns -- holds both.
        """
        assert UNCONFIRMED_SEND_LABEL == "May be in paperless-ngx"
        assert UNCONFIRMED_FILING_LABEL == "Received, not confirmed"
        widest_state = max(len(state_label(state)) for state in JobState)
        assert len(UNCONFIRMED_SEND_LABEL) <= widest_state
        assert len(UNCONFIRMED_FILING_LABEL) <= widest_state

    @pytest.mark.parametrize(
        "category", [c for c in ErrorCategory if c not in _AMBER_CATEGORIES]
    )
    def test_job_label_red_error_is_failed_not_amber(
        self, category: ErrorCategory
    ) -> None:
        """Every other categorised failure keeps the "Failed" label."""
        assert job_label(JobState.ERROR, None, category) == "Failed"

    @pytest.mark.parametrize(
        "state", [state for state in JobState if state is not JobState.ERROR]
    )
    @pytest.mark.parametrize("category", sorted(_AMBER_CATEGORIES))
    def test_job_label_amber_category_only_speaks_for_an_error(
        self, state: JobState, category: ErrorCategory
    ) -> None:
        """A category on a row that did not fail changes nothing about its label."""
        assert job_label(state, "w", category) == job_label(state, "w")


class TestIsAmberCategory:
    """is_amber_category: which failures wear the amber look."""

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_amber_exactly_for_the_two_maybe_delivered_categories(
        self, category: ErrorCategory
    ) -> None:
        """
        Only a failure that may already be in paperless-ngx is amber.

        Parametrised over the whole enum, so a new category is checked here
        the moment it exists.
        """
        assert is_amber_category(category) is (category in _AMBER_CATEGORIES)

    def test_amber_raises_on_unrecognised_value(self) -> None:
        """A value outside ErrorCategory is refused, not silently red."""
        bad = cast("ErrorCategory", "NOT_A_CATEGORY")
        with pytest.raises(AssertionError):
            is_amber_category(bad)


class TestJobStatusClass:
    """job_status_class: the one place a job row's colour is chosen."""

    @pytest.mark.parametrize(
        ("state", "warning", "category", "expected"),
        [
            (JobState.DONE, None, None, "status-done"),
            (JobState.DONE, "", None, "status-done"),
            (JobState.DONE, "w", None, "status-fallback"),
            (JobState.DONE, None, ErrorCategory.UNCONFIRMED_SEND, "status-done"),
            (JobState.FALLBACK, None, None, "status-fallback"),
            (JobState.FALLBACK, "w", None, "status-fallback"),
            (JobState.ERROR, None, ErrorCategory.UPLOAD, "status-error"),
            (JobState.ERROR, None, ErrorCategory.UNCONFIRMED_SEND, "status-fallback"),
            (
                JobState.ERROR,
                None,
                ErrorCategory.UNCONFIRMED_FILING,
                "status-fallback",
            ),
            (JobState.ERROR, None, None, "status-error"),
            (JobState.ERROR, "w", ErrorCategory.SCANNER, "status-error"),
            (JobState.CANCELLED, None, None, "status-cancelled"),
            (
                JobState.CANCELLED,
                "w",
                ErrorCategory.UNCONFIRMED_SEND,
                "status-cancelled",
            ),
        ],
    )
    def test_terminal_rows_amber_red_green_or_grey(
        self,
        state: JobState,
        warning: str | None,
        category: ErrorCategory | None,
        expected: str,
    ) -> None:
        """Each terminal row gets the class its outcome deserves."""
        assert job_status_class(state, warning, category) == expected

    @pytest.mark.parametrize(
        "category", [c for c in ErrorCategory if c not in _AMBER_CATEGORIES]
    )
    def test_every_red_category_is_status_error(self, category: ErrorCategory) -> None:
        """Only the two amber categories escape the red failure look."""
        assert job_status_class(JobState.ERROR, None, category) == "status-error"

    @pytest.mark.parametrize("state", sorted(ACTIVE_STATES))
    @pytest.mark.parametrize("category", [None, ErrorCategory.UNCONFIRMED_SEND])
    def test_active_rows_carry_no_status_class(
        self, state: JobState, category: ErrorCategory | None
    ) -> None:
        """A job still in flight is not coloured at all."""
        assert job_status_class(state, "w", category) == ""

    def test_job_status_class_raises_on_unrecognised_value(self) -> None:
        """A value outside JobState is refused, as state_label refuses it."""
        bad = cast("JobState", "UNKNOWN")
        with pytest.raises(AssertionError):
            job_status_class(bad, None, None)


_DELIVERED_OUTCOME_LINES: list[tuple[JobState, str | None, str]] = [
    (JobState.DONE, None, "Done: T"),
    (JobState.DONE, "", "Done: T"),
    (JobState.DONE, "w", "Uploaded with a warning: T"),
    (JobState.FALLBACK, None, "Saved to folder: T"),
    (JobState.FALLBACK, "", "Saved to folder: T"),
    (JobState.FALLBACK, "w", "Saved to folder: T"),
]


class TestOutcomeLine:
    """outcome_line delivered-outcome headline tests."""

    @pytest.mark.parametrize(("state", "warning", "expected"), _DELIVERED_OUTCOME_LINES)
    def test_outcome_line_delivered(
        self, state: JobState, warning: str | None, expected: str
    ) -> None:
        """
        A delivered outcome's line names what happened, then the title.

        Only a clean DONE says "Done".  A warned DONE and a FALLBACK each say
        in their own words that something went wrong on the way.
        """
        assert outcome_line(state, warning, "T") == expected

    @pytest.mark.parametrize(
        "state",
        [
            state
            for state in JobState
            if state not in {JobState.DONE, JobState.FALLBACK}
        ],
    )
    def test_outcome_line_refuses_undelivered_state(self, state: JobState) -> None:
        """Only a delivered outcome has an outcome line; the error names the state."""
        with pytest.raises(ValueError, match=state.value):
            outcome_line(state, None, "T")

    def test_outcome_line_fallback_not_uploaded_constant(self) -> None:
        """The line a FALLBACK adds on stderr says what was lost, in fixed words."""
        assert FALLBACK_NOT_UPLOADED_LINE == (
            "Not uploaded: saved to the consume folder without its title, tags or "
            "correspondent"
        )


class TestJobModuleReExports:
    """saneless.job re-exports the vocabulary's enums, not copies of them."""

    def test_job_module_re_exports_the_same_job_state(self) -> None:
        """saneless.job.JobState is the vocabulary object, not a copy."""
        assert saneless.job.JobState is JobState

    def test_job_module_re_exports_the_same_error_category(self) -> None:
        """saneless.job.ErrorCategory is the vocabulary object, not a copy."""
        assert saneless.job.ErrorCategory is ErrorCategory

    def test_existing_from_import_still_resolves(self) -> None:
        """`from saneless.job import ...` resolves to the vocabulary enums."""
        assert JobErrorCategory is ErrorCategory
        assert JobJobState is JobState

    def test_job_module_declares_no_enum_of_its_own(self) -> None:
        """job.py defines no enum of its own; both live in the vocabulary."""
        assert JobState.__module__ == "saneless.vocabulary"
        assert ErrorCategory.__module__ == "saneless.vocabulary"


class TestJobActivityProperties:
    """Job.is_active and Job.is_busy follow the state classifications."""

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
        """A Job answers is_active/is_busy for every lifecycle state."""
        job = Job(id="j", profile="default", title="t", state=state)
        assert (job.is_active, job.is_busy) == expected

    def test_awaiting_flip_is_active_but_not_busy(self) -> None:
        """A flip prompt leaves the job in flight while the machine idles."""
        job = Job(id="j", profile="default", title="t", state=JobState.AWAITING_FLIP)
        assert job.is_active
        assert not job.is_busy

    def test_done_is_neither_active_nor_busy(self) -> None:
        """A finished job is neither in flight nor working."""
        job = Job(id="j", profile="default", title="t", state=JobState.DONE)
        assert not job.is_active
        assert not job.is_busy

    def test_properties_are_not_dataclass_fields(self) -> None:
        """is_active/is_busy are properties, so they stay out of __init__."""
        field_names = {f.name for f in fields(Job)}
        assert "is_active" not in field_names
        assert "is_busy" not in field_names


# One aware instant for the status-area sentences below.  Every expectation
# renders it through ``local_time``, so the tests hold in any ``TZ``.
_STARTED = datetime(2026, 9, 30, 19, 3, tzinfo=UTC)

# The label every job state puts on the Scan button, written out here rather
# than read from ``scan_button_label`` so the table is pinned, not echoed.  A
# new ``JobState`` member has no row, so the lookup below fails for it.
_SCAN_BUTTON_LABELS: dict[JobState, str] = {
    JobState.PENDING: "Queued…",
    JobState.SCANNING: "Scanning…",
    JobState.AWAITING_FLIP: "Waiting for flip…",
    JobState.AWAITING_NEXT_PASS: "Waiting for you…",
    JobState.AWAITING_BLANK_DECISION: "Waiting for you…",
    JobState.AWAITING_RETRY: "Waiting for you…",
    JobState.SCANNING_REVERSE: "Scanning…",
    JobState.ASSEMBLING: "Scanning…",
    JobState.UPLOADING: "Scanning…",
    JobState.DONE: "Scan",
    JobState.ERROR: "Scan",
    JobState.FALLBACK: "Scan",
    JobState.CANCELLED: "Scan",
}


class TestStatusAreaLines:
    """The two fixed lines of the status area."""

    def test_idle_line(self) -> None:
        """With no job in flight and nothing blocking, the area says it is ready."""
        assert IDLE_LINE == "Ready to scan."

    def test_lost_contact_line(self) -> None:
        """A poll that cannot read the store says so and that it keeps trying."""
        assert LOST_CONTACT_LINE == (
            "Cannot read the scan's progress right now — retrying..."
        )


class TestScanButtonLabel:
    """The Scan button's label follows the state of the job in flight."""

    def test_no_job_is_scan(self) -> None:
        """With no job at all, the button offers a scan."""
        assert scan_button_label(None) == "Scan"

    @pytest.mark.parametrize("state", list(JobState))
    def test_every_state_has_its_label(self, state: JobState) -> None:
        """Each state's label is the pinned one, and a new member fails here."""
        assert scan_button_label(state) == _SCAN_BUTTON_LABELS[state]

    def test_button_labels_use_the_ellipsis_character(self) -> None:
        """A button label ends in U+2026, never in three ASCII periods."""
        for state in JobState:
            assert not scan_button_label(state).endswith("...")


class TestPageTitle:
    """The browser tab's title never names the job, only its state."""

    def test_no_job(self) -> None:
        """With no job in flight the tab is just the product name."""
        assert page_title(None) == "saneless"

    def test_pending_queued(self) -> None:
        """A job waiting behind another says it is queued."""
        assert page_title(JobState.PENDING, queued=True) == "Queued — saneless"

    def test_pending_starting(self) -> None:
        """A job next to run says it is starting."""
        assert page_title(JobState.PENDING, queued=False) == "Starting — saneless"

    @pytest.mark.parametrize("state", sorted(ACTIVE_STATES - {JobState.PENDING}))
    def test_other_active_states_use_the_state_label(self, state: JobState) -> None:
        """A running job is named by the history table's label for its state."""
        assert page_title(state) == f"{state_label(state)} — saneless"

    def test_done_clean(self) -> None:
        """A clean upload says Done, as the outcome line does."""
        assert page_title(JobState.DONE) == "Done — saneless"

    def test_done_with_a_warning(self) -> None:
        """A warned upload says so in the tab too."""
        assert page_title(JobState.DONE, warning="a page was skipped") == (
            "Uploaded with a warning — saneless"
        )

    def test_fallback(self) -> None:
        """A scan kept in the folder says where it went."""
        assert page_title(JobState.FALLBACK) == "Saved to folder — saneless"

    def test_cancelled(self) -> None:
        """A cancelled scan says it was cancelled."""
        assert page_title(JobState.CANCELLED) == "Cancelled — saneless"

    @pytest.mark.parametrize("category", [None, *list(ErrorCategory)])
    def test_error_uses_the_job_label(self, category: ErrorCategory | None) -> None:
        """A failure reads as the history row does, amber categories included."""
        assert page_title(JobState.ERROR, category=category) == (
            f"{job_label(JobState.ERROR, None, category)} — saneless"
        )

    def test_error_amber_category_is_not_failed(self) -> None:
        """A failure that may be in paperless-ngx is not called Failed in the tab."""
        assert page_title(JobState.ERROR, category=ErrorCategory.UNCONFIRMED_SEND) == (
            f"{UNCONFIRMED_SEND_LABEL} — saneless"
        )

    def test_takes_no_title(self) -> None:
        """No parameter can carry the job's title into the tab."""
        assert not any(
            "title" in name for name in inspect.signature(page_title).parameters
        )

    @pytest.mark.parametrize("state", [None, *list(JobState)])
    def test_warning_text_never_reaches_the_tab(self, state: JobState | None) -> None:
        """Only whether a warning exists is read, never its words."""
        sentinel = "Sentinel title 7f3a"
        for queued in (False, True):
            for category in (None, *ErrorCategory):
                rendered = page_title(
                    state, warning=sentinel, category=category, queued=queued
                )
                assert sentinel not in rendered
                assert rendered == "saneless" or rendered.endswith(" — saneless")


class TestLastScanLine:
    """The line under the idle line naming the previous scan's outcome."""

    def _line(
        self,
        state: JobState,
        *,
        warning: str | None = None,
        category: ErrorCategory | None = None,
    ) -> str:
        """
        Return the line for a job titled "Tax" started at ``_STARTED``.

        Args:
            state: The job's state.
            warning: The job's warning, if any.
            category: The job's error category, if any.

        Returns:
            The rendered line.

        """
        return last_scan_line(
            state, warning=warning, category=category, title="Tax", created_at=_STARTED
        )

    def test_done_clean(self) -> None:
        """A clean upload has a tick and says Done."""
        assert self._line(JobState.DONE) == (
            f"Last scan: ✓ Done: Tax — started {local_time(_STARTED)}"
        )

    def test_done_with_a_warning(self) -> None:
        """A warned upload has the warning sign and says so."""
        assert self._line(JobState.DONE, warning="a page was skipped") == (
            "Last scan: ⚠ Uploaded with a warning: Tax — started "
            f"{local_time(_STARTED)}"
        )

    def test_fallback(self) -> None:
        """A scan kept in the folder has the arrow and says where it went."""
        assert self._line(JobState.FALLBACK) == (
            f"Last scan: → Saved to folder: Tax — started {local_time(_STARTED)}"
        )

    def test_fallback_with_a_warning_keeps_the_arrow(self) -> None:
        """A FALLBACK's warning goes on the detail line, not into the glyph."""
        assert self._line(JobState.FALLBACK, warning="upload failed") == (
            f"Last scan: → Saved to folder: Tax — started {local_time(_STARTED)}"
        )

    def test_cancelled(self) -> None:
        """A cancelled scan has the stop sign."""
        assert self._line(JobState.CANCELLED) == (
            f"Last scan: ⊘ Cancelled: Tax — started {local_time(_STARTED)}"
        )

    def test_error_without_a_category(self) -> None:
        """A failure with no category is red and says Failed."""
        assert self._line(JobState.ERROR) == (
            f"Last scan: ✗ Failed: Tax — started {local_time(_STARTED)}"
        )

    @pytest.mark.parametrize("category", sorted(set(ErrorCategory) - _AMBER_CATEGORIES))
    def test_error_red_category(self, category: ErrorCategory) -> None:
        """Every failure that did not deliver the scan says Failed."""
        assert self._line(JobState.ERROR, category=category) == (
            f"Last scan: ✗ Failed: Tax — started {local_time(_STARTED)}"
        )

    @pytest.mark.parametrize("category", sorted(_AMBER_CATEGORIES))
    def test_error_amber_category(self, category: ErrorCategory) -> None:
        """A failure that may be in paperless-ngx has the warning sign and label."""
        label = job_label(JobState.ERROR, None, category)
        assert label != "Failed"
        assert self._line(JobState.ERROR, category=category) == (
            f"Last scan: ⚠ {label}: Tax — started {local_time(_STARTED)}"
        )

    def test_reuses_the_outcome_line(self) -> None:
        """The delivered outcomes read exactly as the live outcome line does."""
        for state in (JobState.DONE, JobState.FALLBACK):
            for warning in (None, "w"):
                assert outcome_line(state, warning, "Tax") in self._line(
                    state, warning=warning
                )

    @pytest.mark.parametrize("state", sorted(ACTIVE_STATES))
    def test_active_state_is_refused(self, state: JobState) -> None:
        """A job still in flight has no last-scan line."""
        with pytest.raises(ValueError, match="terminal"):
            self._line(state)


class TestLastScanDetail:
    """The optional second line under the last-scan line."""

    def test_done_clean_has_none(self) -> None:
        """A clean upload needs no second line."""
        detail = last_scan_detail(
            JobState.DONE, warning=None, category=None, error=None
        )
        assert detail is None

    def test_done_warning(self) -> None:
        """A warned upload shows its warning."""
        detail = last_scan_detail(
            JobState.DONE, warning="w1", category=None, error=None
        )
        assert detail == "w1"

    def test_fallback_warning(self) -> None:
        """A scan kept in the folder shows why it was not uploaded."""
        detail = last_scan_detail(
            JobState.FALLBACK, warning="w2", category=None, error=None
        )
        assert detail == "w2"

    def test_fallback_without_a_warning_has_none(self) -> None:
        """With nothing to add, a FALLBACK has no second line."""
        detail = last_scan_detail(
            JobState.FALLBACK, warning=None, category=None, error=None
        )
        assert detail is None

    def test_cancelled_has_none(self) -> None:
        """A cancel is a deliberate stop with nothing more to say."""
        detail = last_scan_detail(
            JobState.CANCELLED, warning="w", category=None, error="e"
        )
        assert detail is None

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_with_a_category(self, category: ErrorCategory) -> None:
        """A categorised failure shows its two advice sentences, one space apart."""
        detail = last_scan_detail(
            JobState.ERROR, warning=None, category=category, error="raw text"
        )
        assert detail == f"{error_message(category)} {error_next_step(category)}"

    def test_error_without_a_category(self) -> None:
        """An uncategorised failure shows its error text."""
        detail = last_scan_detail(
            JobState.ERROR, warning=None, category=None, error="raw text"
        )
        assert detail == "raw text"

    @pytest.mark.parametrize("state", sorted(ACTIVE_STATES))
    def test_active_state_is_refused(self, state: JobState) -> None:
        """A job still in flight has no last-scan detail."""
        with pytest.raises(ValueError, match="terminal"):
            last_scan_detail(state, warning=None, category=None, error=None)


# The four lines a viewer who did not start a waiting scan reads, with the
# deadline and without it, as the product copy states them.
_WAIT_LINES: dict[JobState, tuple[str, str]] = {
    JobState.AWAITING_FLIP: (
        "Waiting for the stack to be flipped. It can be continued from the "
        "device that started this scan — it stops at {deadline} if nobody does.",
        "Waiting for the stack to be flipped. It can be continued from the "
        "device that started this scan.",
    ),
    JobState.AWAITING_NEXT_PASS: (
        "Waiting for the next page. It can be answered from the device that "
        "started this scan — it stops waiting at {deadline} if nobody does.",
        "Waiting for the next page. It can be answered from the device that "
        "started this scan.",
    ),
    JobState.AWAITING_BLANK_DECISION: (
        "Waiting for a decision about blank pages. It can be answered from the "
        "device that started this scan — it stops waiting at {deadline} if "
        "nobody does.",
        "Waiting for a decision about blank pages. It can be answered from the "
        "device that started this scan.",
    ),
    JobState.AWAITING_RETRY: (
        "The last scan failed; waiting for a decision. It can be answered from "
        "the device that started this scan — it stops waiting at {deadline} if "
        "nobody does.",
        "The last scan failed; waiting for a decision. It can be answered from "
        "the device that started this scan.",
    ),
}


class TestNonOwnerWaitLine:
    """What a viewer who cannot answer a waiting scan is told."""

    @pytest.mark.parametrize("state", list(_WAIT_LINES))
    def test_with_a_deadline(self, state: JobState) -> None:
        """The line names when the wait ends, in local time with its zone."""
        expected = _WAIT_LINES[state][0].format(deadline=local_time(_STARTED))
        assert non_owner_wait_line(state, deadline=_STARTED) == expected

    @pytest.mark.parametrize("state", list(_WAIT_LINES))
    def test_without_a_deadline(self, state: JobState) -> None:
        """Before the wait's start is recorded, the line names no time."""
        assert non_owner_wait_line(state, deadline=None) == _WAIT_LINES[state][1]

    @pytest.mark.parametrize("state", list(_WAIT_LINES))
    def test_no_trailing_ellipsis(self, state: JobState) -> None:
        """A waiting line is a full stop, never an in-progress ellipsis."""
        for deadline in (None, _STARTED):
            line = non_owner_wait_line(state, deadline=deadline)
            assert not line.endswith(("...", "…"))

    @pytest.mark.parametrize("state", sorted(set(JobState) - set(_WAIT_LINES)))
    def test_other_states_are_refused(self, state: JobState) -> None:
        """Only the four waits for a person have a waiting line."""
        with pytest.raises(ValueError, match="not waiting"):
            non_owner_wait_line(state, deadline=None)


class TestPromptHeadings:
    """The first line of each owner prompt names the job."""

    def test_flip_heading(self) -> None:
        """The flip prompt names the scan in curly quotes."""
        assert flip_heading("Tax") == "Flip the stack for “Tax”"

    def test_pass_heading(self) -> None:
        """The multi-page prompt names the document in curly quotes."""
        assert pass_heading("Tax") == "Adding pages to “Tax”"


class TestFlipDeadlineNote:
    """The note under the flip prompt's buttons saying what a timeout does."""

    def test_with_a_deadline(self) -> None:
        """The note names the time the wait ends."""
        assert flip_deadline_note(deadline=_STARTED, timeout_seconds=600) == (
            f"If nobody presses Continue by {local_time(_STARTED)}, the scan "
            "stops, nothing is uploaded, and the front sides already scanned "
            "are kept in failed/."
        )

    def test_without_a_deadline(self) -> None:
        """Before the wait's start is recorded, the note names the duration."""
        assert flip_deadline_note(deadline=None, timeout_seconds=600) == (
            "If nobody presses Continue within 10 minutes, the scan stops, "
            "nothing is uploaded, and the front sides already scanned are kept "
            "in failed/."
        )

    def test_duration_is_named_in_its_own_unit(self) -> None:
        """A 90-second wait is named in seconds."""
        note = flip_deadline_note(deadline=None, timeout_seconds=90)
        assert "within 90 seconds," in note


class TestListCopy:
    """What the tag and correspondent lists say while they load or cannot."""

    def test_tags_loading(self) -> None:
        """The tag list's placeholder says it is loading, with an ellipsis."""
        assert TAGS_LOADING == "Loading tags from paperless-ngx..."

    def test_correspondents_loading(self) -> None:
        """The correspondent help line says the list is loading."""
        assert CORRESPONDENTS_LOADING == "Loading correspondents from paperless-ngx..."

    def test_tags_unavailable(self) -> None:
        """A tag list that could not load says it keeps trying and how to retry."""
        assert TAGS_UNAVAILABLE == (
            "Tags could not be loaded from paperless-ngx. saneless keeps trying; "
            "press ↻ to try now."
        )

    def test_correspondents_unavailable(self) -> None:
        """The correspondent twin of the unavailable line."""
        assert CORRESPONDENTS_UNAVAILABLE == (
            "Correspondents could not be loaded from paperless-ngx. saneless "
            "keeps trying; press ↻ to try now."
        )

    def test_filter_label_matches_its_placeholder(self) -> None:
        """The filter's accessible name is the words it visibly shows."""
        assert TAG_FILTER_LABEL == "Filter tags"


class TestScanHoldReason:
    """Why the Scan button is held while the lists load."""

    def test_both_lists(self) -> None:
        """With both lists shown, the reason names both."""
        assert scan_hold_reason(tags=True, correspondents=True) == (
            "Scan waits for the tags and correspondents to load..."
        )

    def test_tags_only(self) -> None:
        """With only tags shown, the reason names the tags."""
        assert scan_hold_reason(tags=True, correspondents=False) == (
            "Scan waits for the tags to load..."
        )

    def test_correspondents_only(self) -> None:
        """With only correspondents shown, the reason names them."""
        assert scan_hold_reason(tags=False, correspondents=True) == (
            "Scan waits for the correspondents to load..."
        )

    def test_neither_list(self) -> None:
        """With no list shown there is nothing to wait for."""
        assert scan_hold_reason(tags=False, correspondents=False) is None


class TestNoScriptCopy:
    """What a browser with JavaScript turned off is told."""

    def test_noscript_line(self) -> None:
        """The line under the Scan heading says why nothing works and what to do."""
        assert NO_SCRIPT_LINE == (
            "Scanning from this page needs JavaScript, which is turned off in "
            "this browser. Turn it on and reload the page, or run saneless scan "
            "on the server."
        )

    def test_refusal_page(self) -> None:
        """The page a form post without JavaScript lands on says no scan started."""
        assert NO_SCRIPT_PAGE_TITLE == "Scan not started — saneless"
        assert NO_SCRIPT_HEADING == "Scan not started"
        assert NO_SCRIPT_BODY == (
            "Scanning from this page needs JavaScript, which is turned off in "
            "this browser, so no scan was started. Turn it on and reload the "
            "page, or run saneless scan on the server."
        )
        assert NO_SCRIPT_BACK_LINK == "Back to the scan page"

    def test_refusal_title_is_its_heading(self) -> None:
        """The tab and the heading say the same thing."""
        assert f"{NO_SCRIPT_HEADING} — saneless" == NO_SCRIPT_PAGE_TITLE


# What each surface says in place of the two retry placeholders.  Written out
# rather than read from the module, so a change to either ending fails here.
_SURFACE_ENDINGS: Final = (
    (CheckSurface.STRIP, "press Check again", "Press Check again"),
    (CheckSurface.DOCTOR, "run saneless doctor again", "Run saneless doctor again"),
)


class TestRenderCheckStep:
    """
    One check row, two endings: each surface names its own way to retry.

    The strip has a Check again button; ``saneless doctor`` is run again from a
    terminal, where no button exists.
    """

    @pytest.mark.parametrize(("surface", "mid", "start"), _SURFACE_ENDINGS)
    def test_the_mid_sentence_placeholder(
        self, surface: CheckSurface, mid: str, start: str
    ) -> None:
        """A retry after a comma is spelled in lower case."""
        step = f"Check the scanner is on, then {RETRY_PLACEHOLDER}."
        assert (
            render_check_step(step, surface) == f"Check the scanner is on, then {mid}."
        )
        assert start not in render_check_step(step, surface)

    @pytest.mark.parametrize(("surface", "mid", "start"), _SURFACE_ENDINGS)
    def test_the_sentence_start_placeholder(
        self, surface: CheckSurface, mid: str, start: str
    ) -> None:
        """A retry that opens a sentence is capitalised."""
        step = f"{RETRY_SENTENCE_PLACEHOLDER}."
        assert render_check_step(step, surface) == f"{start}."
        assert mid not in render_check_step(step, surface)

    @pytest.mark.parametrize(("surface", "mid", "start"), _SURFACE_ENDINGS)
    def test_both_placeholders_in_one_step(
        self, surface: CheckSurface, mid: str, start: str
    ) -> None:
        """Every placeholder in a step is replaced, not only the first."""
        step = (
            f"{RETRY_SENTENCE_PLACEHOLDER}. If that fails, restart, then "
            f"{RETRY_PLACEHOLDER}."
        )
        assert render_check_step(step, surface) == (
            f"{start}. If that fails, restart, then {mid}."
        )

    @pytest.mark.parametrize("surface", list(CheckSurface))
    def test_a_step_with_no_placeholder_is_unchanged(
        self, surface: CheckSurface
    ) -> None:
        """A step that does not retry reads the same on both surfaces."""
        step = (
            "Correct paperless.url in the saneless config file, then restart saneless."
        )
        assert render_check_step(step, surface) == step
        assert render_check_step("", surface) == ""

    @pytest.mark.parametrize("surface", list(CheckSurface))
    def test_other_braces_are_inert(self, surface: CheckSurface) -> None:
        """
        Only the two placeholders are replaced; no other brace is interpreted.

        A format call would raise on ``{0}`` or substitute ``{name}``, so the
        row text is never handed to one.
        """
        step = f"Set [scanner] device to {{0}} or {{name}}, then {RETRY_PLACEHOLDER}."
        rendered = render_check_step(step, surface)
        assert rendered.startswith("Set [scanner] device to {0} or {name}, then ")
        assert RETRY_PLACEHOLDER not in rendered

    def test_the_placeholders_differ(self) -> None:
        """The two placeholders are distinct, so each keeps its own case."""
        assert RETRY_PLACEHOLDER != RETRY_SENTENCE_PLACEHOLDER
        assert RETRY_PLACEHOLDER.lower() == RETRY_SENTENCE_PLACEHOLDER.lower()

    def test_the_strip_ending_names_the_button(self) -> None:
        """The strip's ending names the button on the page beside it."""
        assert "Check again" in render_check_step(
            RETRY_SENTENCE_PLACEHOLDER, CheckSurface.STRIP
        )

    def test_the_doctor_ending_names_no_button(self) -> None:
        """Doctor's ending names the command, never a web button."""
        for placeholder in (RETRY_PLACEHOLDER, RETRY_SENTENCE_PLACEHOLDER):
            rendered = render_check_step(placeholder, CheckSurface.DOCTOR)
            assert "Check again" not in rendered
            assert "saneless doctor again" in rendered

    def test_an_unknown_surface_raises(self) -> None:
        """A value outside CheckSurface is a programming error."""
        bad = cast("CheckSurface", "KIOSK")
        with pytest.raises(AssertionError):
            render_check_step("Nothing to retry.", bad)


class TestScanChildSentences:
    """The sentences for a scan child that was stopped, died or went astray."""

    def test_every_scan_stage_has_a_phrase(self) -> None:
        """Each stage has its phrase, so a new stage cannot go unnamed."""
        assert set(_SCAN_STAGE_PHRASES) == set(ScanStage)

    def test_scan_stage_values(self) -> None:
        """The stage values are the lower-case words the protocol carries."""
        assert [stage.value for stage in ScanStage] == [
            "startup",
            "open",
            "configure",
            "start",
            "read",
            "cancel",
            "close",
            "restart",
            "exit",
        ]

    @pytest.mark.parametrize(
        ("stage", "page", "phrase"),
        [
            (ScanStage.STARTUP, None, "while the scanner library was starting"),
            (ScanStage.STARTUP, 4, "while the scanner library was starting"),
            (ScanStage.OPEN, None, "while opening the scanner"),
            (ScanStage.CONFIGURE, 2, "while setting up the scan"),
            (ScanStage.START, 5, "while starting page 5"),
            (ScanStage.START, None, "while starting a page"),
            (ScanStage.READ, 3, "while reading page 3"),
            (ScanStage.READ, None, "while reading a page"),
            (ScanStage.CANCEL, 6, "while cancelling page 6"),
            (ScanStage.CANCEL, None, "while cancelling the scan"),
            (ScanStage.CLOSE, None, "while closing the scanner"),
            (ScanStage.RESTART, None, "while restarting the scanner library"),
            (ScanStage.EXIT, 7, "while finishing the scan"),
        ],
    )
    def test_scan_child_stopped_error_names_the_stage(
        self, stage: ScanStage, page: int | None, phrase: str
    ) -> None:
        """A stopped child names the stage, and the page only where it has one."""
        assert scan_child_stopped_error(stage, page) == (
            f"The scanner stopped answering {phrase}; saneless stopped it"
        )

    def test_scan_child_stopped_error_examples(self) -> None:
        """The stopped-child sentences read as written."""
        assert scan_child_stopped_error(ScanStage.READ, 3) == (
            "The scanner stopped answering while reading page 3; saneless stopped it"
        )
        assert scan_child_stopped_error(ScanStage.CLOSE, None) == (
            "The scanner stopped answering while closing the scanner; "
            "saneless stopped it"
        )
        assert scan_child_stopped_error(ScanStage.EXIT, 7) == (
            "The scanner stopped answering while finishing the scan; "
            "saneless stopped it"
        )

    def test_scan_child_crashed_error(self) -> None:
        """A crash names the signal and the stage."""
        assert scan_child_crashed_error(ScanStage.READ, 2, "SIGSEGV") == (
            "The scanning process died from SIGSEGV while reading page 2"
        )

    def test_scan_child_ended_error(self) -> None:
        """A child that exited by itself names its status and the stage."""
        assert scan_child_ended_error(ScanStage.OPEN, None, 1) == (
            "The scanning process ended unexpectedly (exit status 1) while "
            "opening the scanner"
        )

    def test_scan_child_out_of_time_error(self) -> None:
        """A child its own time limit ended says so, not that it crashed."""
        assert scan_child_out_of_time_error(ScanStage.READ, 2) == (
            "The scanner stopped answering while reading page 2, and the "
            "scanning process stopped at its own time limit"
        )

    def test_scan_child_unexpected_error(self) -> None:
        """An unexpected exception type is named with the stage."""
        assert scan_child_unexpected_error("TypeError", ScanStage.CONFIGURE, None) == (
            "The scanning process failed unexpectedly (TypeError) while setting "
            "up the scan"
        )

    def test_scan_child_no_answer_error(self) -> None:
        """A reply saneless could not read is named as such."""
        assert scan_child_no_answer_error(ScanStage.OPEN, None) == (
            "The scanning process sent a reply saneless could not read while "
            "opening the scanner; saneless stopped it"
        )

    def test_scan_child_not_started_error(self) -> None:
        """A child that never started says so."""
        assert scan_child_not_started_error() == (
            "The scanning process could not be started"
        )


class TestSaneStartFailureTexts:
    """The SANE start-failure texts every reporting path shares."""

    def test_sane_init_failure_message(self) -> None:
        """A SANE initialisation failure keeps its reason."""
        assert sane_init_failure_message("Error during device I/O") == (
            "Could not initialise SANE: Error during device I/O"
        )

    def test_python_sane_missing_message(self) -> None:
        """The install hint keeps the import's reason in brackets."""
        assert python_sane_missing_message("No module named 'sane'") == (
            "python-sane cannot be imported (No module named 'sane'). Install the "
            "SANE development package (libsane-dev on Debian/Ubuntu, "
            "sane-backends-devel on Fedora/RHEL) and reinstall saneless; see "
            "Install on Bare Metal in the documentation"
        )

    def test_python_sane_install_next_step(self) -> None:
        """The install next step is the one the install hint has always had."""
        assert PYTHON_SANE_INSTALL_NEXT_STEP == (
            "Install the SANE development package and reinstall saneless, "
            "as the error says, then run the command again."
        )
