"""
Tests for the shared saneless vocabulary module.

Covers requirements: CTR-01, CTR-02, CTR-05, ROBU-01, ROBU-02, ROBU-08.
"""

from __future__ import annotations

import json
import re
import signal
import time
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import pytest

import saneless.job
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
    ACTIVE_STATES,
    BUSY_STATES,
    FALLBACK_NOT_UPLOADED_LINE,
    LOCAL_TIME_FORMAT,
    PASS_WAIT_STATES,
    QUEUE_FULL_JOB_ERROR,
    RESTART_REASON,
    TERMINAL_STATES,
    TITLE_MAX_LENGTH,
    TOKEN_UNSET_JOB_ERROR,
    UNCONFIRMED_FILING_LABEL,
    UNCONFIRMED_SEND_LABEL,
    URL_UNSET_JOB_ERROR,
    WAITING_STATES,
    WARNED_UPLOAD_LABEL,
    WORKER_DEGRADED_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
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
    duration_phrase,
    error_advice,
    error_message,
    error_next_step,
    exit_code_for,
    exit_code_for_outcome,
    exit_code_for_signal,
    flip_answer_label,
    is_amber_category,
    job_label,
    job_state_for,
    job_status_class,
    local_time,
    outcome_line,
    page_counts,
    page_timeout_error,
    pages_phrase,
    pass_answer_label,
    pass_cap_warning,
    pass_wait_state,
    progress_label,
    rejection_message,
    rejection_status_code,
    removed_pages,
    removed_pages_note,
    scan_page_description,
    sixteen_bit_error,
    source_not_offered_error,
    state_label,
    substituted_source_warning,
    timeout_finish_warning,
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
    """JobState membership tests."""

    def test_job_state_has_exactly_thirteen_members(self) -> None:
        """
        JobState declares exactly thirteen lifecycle members.

        A count guard, not a name list: adding a member should fail the
        parametrised completeness tests below -- which force a label and a
        classification decision -- rather than a hand-written roster that only
        records what the enum happened to contain when it was written.
        """
        assert len(list(JobState)) == 13

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
        ran (D-06), ASSEMBLY for a PDF that could not be built (D-04) and
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


class TestProfileStorage:
    """ProfileStorage membership tests (APPL-06, Amendment A-2, D-22)."""

    def test_profile_storage_member_names(self) -> None:
        """
        ProfileStorage names the three outcomes of the startup persist (A-2).

        ``_persist_generated_profiles`` returns None for two genuinely
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
        """Every ProfileStorage value is identical to its member name (A-2)."""
        assert storage.value == storage.name

    def test_profile_storage_is_a_str_enum(self) -> None:
        """ProfileStorage compares equal to its own string, like its siblings."""
        assert ProfileStorage.PERSISTED == "PERSISTED"


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
        """flip_answer_label returns the acknowledgment copy (DPLX-06, CR-01)."""
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
        """Every FlipOutcome has acknowledgment prose ending in ASCII dots (CR-01)."""
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
        """The flip wait keeps its four outcomes; the new answers are a sibling."""
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
    """PassPrompt value-type tests."""

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
        """A read the cancel did not end is named as still outstanding."""
        page = scan_page_description(2480, 3508, colour=False, dpi=300)
        assert page_timeout_error("Page 1", 120.0, page, returned=False) == (
            "Page 1 timed out after 120s, the limit for a grey page of "
            "2480 x 3508 pixels at 300 dpi; the scanner did not respond to the "
            "cancel, so saneless is still waiting for that read to return"
        )

    def test_page_timeout_error_without_a_page(self) -> None:
        """With no page described, only the limit is named."""
        assert page_timeout_error("Page 2", 120.0, None, returned=True) == (
            "Page 2 timed out after 120s"
        )
        assert page_timeout_error("Page 2", 120.0, None, returned=False) == (
            "Page 2 timed out after 120s; the scanner did not respond to the "
            "cancel, so saneless is still waiting for that read to return"
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
        says a copy is kept in failed/ to import only if it is not there.
        """
        advice = error_advice(category)
        for text in (advice.message.lower(), advice.next_step.lower()):
            assert "start the scan again" not in text
            assert "scan again" not in text
        assert "document list" in advice.next_step
        assert "failed/" in advice.next_step
        assert "only if the document is not in paperless-ngx" in advice.next_step

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


class TestLocalTimeTrailingSpace:
    """
    local_time never emits trailing whitespace (IN-06, APPL-12).

    ``LOCAL_TIME_FORMAT`` ends in ``%Z``, which ``strftime`` renders as the
    empty string on a platform that reports no zone abbreviation, leaving the
    separator before it dangling.  ``resolve_job_title`` interpolates the
    result straight into ``f"Scan {local_time(now)}"``, so such a host files a
    paperless-ngx document whose title ends in a space.

    That platform cannot be reproduced portably -- POSIX requires a zone
    abbreviation of at least three characters, so no ``TZ`` value produces an
    empty ``%Z`` on glibc -- so these tests pin the *property* instead, by
    monkeypatching the module's format to one that ends in whitespace.  Read
    together with ``test_the_shared_format_still_names_the_zone`` they say: the
    zone stays on the line, and whatever it renders as, the result is clean.
    """

    #: Stand-ins for a format whose final ``%Z`` rendered empty.  A literal
    #: space is the real case; the tab and the double space are there so the
    #: fix cannot be a special case for one character.
    WHITESPACE_FORMATS = ("%Y-%m-%d %H:%M ", "%Y-%m-%d %H:%M\t", "%Y-%m-%d %H:%M  ")

    def test_the_shared_format_still_names_the_zone(self) -> None:
        """
        The fix must not reach its goal by dropping ``%Z`` (D-34).

        A doc truth: D-34 pins the ``2026-09-16 14:03 CDT`` shape, so removing
        the zone would satisfy the trailing-space property and break the
        contract the constant exists to hold.
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
        """One NULL count means nothing at all is rendered (D-32)."""
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
        """An ERROR or pre-Phase-23 row has no counts, so renders none (D-32)."""
        job = Job(id="j", profile="default", title="t", state=JobState.ERROR)
        assert page_counts(job) is None


class TestRemovedPagesNote:
    """The informational note naming the pages removed as blank (D-07, D-08)."""

    def test_several_positions_are_listed_in_order(self) -> None:
        """The D-08 sentence, with the plural noun."""
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
        """With no page kept yet the first pass reads exactly as before."""
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
        "The title is too long. Shorten it to 256 characters or fewer.",
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
        """The narrower arms leave the base class where it was."""
        assert classify_error(PaperlessError("x")) is ErrorCategory.UPLOAD

    def test_disk_space_error_is_disk_space(self) -> None:
        """A full disk is its own category, never SCANNER or ASSEMBLY."""
        assert classify_error(DiskSpaceError("x")) is ErrorCategory.DISK_SPACE

    def test_no_scanner_found_error_is_scanner(self) -> None:
        """Finding no scanner is a scanner condition, not a configuration one."""
        assert classify_error(NoScannerFoundError("x")) is ErrorCategory.SCANNER

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

    def test_all_pages_blank_error_is_all_blank(self) -> None:
        """AllPagesBlankError classifies as ALL_BLANK, never SCANNER (D-10)."""
        assert classify_error(AllPagesBlankError("x")) is ErrorCategory.ALL_BLANK

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
        ExitCode is the one definition of the CLI exit codes (EXC-02, D-07).

        Rewritten when the all-blank failure (8) and the two signal
        interruptions (129 for SIGHUP, 143 for SIGTERM) joined the enum, and
        again for the upload that may already be in paperless-ngx (9) and the
        full disk (10), so the pinned set lists all fourteen members.
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
            ("TERMINATED", 143),
        }

    def test_exit_code_members_are_declared_in_ascending_value_order(self) -> None:
        """
        The enum lists its codes in value order, as every table pinned to it does.

        This replaces a test that pinned 130 as the last member.  That held
        while 130 was the only shell-convention code; SIGTERM's 143 (128 + 15)
        now follows it, so the lasting rule is value order: saneless's own
        small codes first, then 129, 130 and 143.
        """
        values = [int(member) for member in ExitCode]
        assert values == sorted(values)


class TestExitCodeForSignal:
    """exit_code_for_signal maps an interrupting signal to 128 + its number."""

    def test_sighup_is_hangup(self) -> None:
        """A dropped SSH session's SIGHUP exits 129 (D-13)."""
        assert exit_code_for_signal(signal.SIGHUP) is ExitCode.HANGUP
        assert int(ExitCode.HANGUP) == 128 + signal.SIGHUP

    def test_sigterm_is_terminated(self) -> None:
        """A SIGTERM exits 143 (D-13)."""
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
