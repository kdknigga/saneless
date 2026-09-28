"""
Shared vocabulary for the saneless domain -- every word the system uses about itself.

This is a leaf module.  It must not import from ``job.py``, ``pipeline.py``,
``worker.py``, ``cli.py``, or anything under ``web/``.  Every consumer imports
from here; nothing here imports from a consumer.  The only intra-package import
permitted is ``saneless.exceptions``, which is itself a leaf.
"""

from __future__ import annotations

import signal
from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Final, Literal, Protocol, TypeIs, assert_never

from saneless.exceptions import (
    AllPagesBlankError,
    ConfigError,
    DiskSpaceError,
    FeederEmptyError,
    PaperlessError,
    PaperlessIncompatibleError,
    PaperlessUncertainSendError,
    PaperlessUnconfirmedError,
    PdfError,
    ScanError,
)

if TYPE_CHECKING:
    # Annotation-only, so the leaf rule is untouched either way -- both are
    # stdlib and importing them would not make this module depend on a
    # consumer.
    from collections.abc import Sequence
    from datetime import datetime

__all__ = [
    "ACTIVE_STATES",
    "BUSY_STATES",
    "FALLBACK_NOT_UPLOADED_LINE",
    "HIDDEN_ERROR_DETAIL",
    "HIDDEN_JOB_TITLE",
    "HIDDEN_PRESERVED_ERROR",
    "HIDDEN_WARNING_LINE",
    "LOCAL_TIME_FORMAT",
    "MULTI_PAGE_DISABLED_REASON",
    "MULTI_PAGE_HELP",
    "MULTI_PAGE_LABEL",
    "MULTI_PAGE_NEEDS_TERMINAL",
    "MULTI_PAGE_OPTION_HELP",
    "NOTHING_TO_FINISH",
    "PASS_WAIT_STATES",
    "QUEUE_FULL_JOB_ERROR",
    "RESTART_REASON",
    "SCAN_BLOCKED_REASON",
    "SCAN_BLOCKED_URL_REASON",
    "TERMINAL_STATES",
    "TITLE_MAX_LENGTH",
    "TOKEN_UNSET_JOB_ERROR",
    "UNCONFIRMED_FILING_LABEL",
    "UNCONFIRMED_SEND_LABEL",
    "URL_UNSET_JOB_ERROR",
    "WAITING_STATES",
    "WARNED_UPLOAD_LABEL",
    "WORKER_DEGRADED_JOB_ERROR",
    "WORKER_DOWN_JOB_ERROR",
    "CliChoice",
    "ConfigFileState",
    "ConnectionStatus",
    "ErrorAdvice",
    "ErrorCategory",
    "ExitCode",
    "FlipOutcome",
    "JobState",
    "PageCounted",
    "PaperSize",
    "PassAnswer",
    "PassPrompt",
    "PassPromptCopy",
    "PassWait",
    "ProfileStorage",
    "RemovedPagesNoted",
    "RequestRejection",
    "ScanOutcome",
    "SubmitResult",
    "WorkerHealth",
    "abort_question",
    "ambiguous_source_error",
    "backs_not_scanned_warning",
    "backs_pass_cap_note",
    "backs_pass_cap_warning",
    "blank_timeout_finish_warning",
    "busy_line",
    "cap_finish_warning",
    "classify_error",
    "cli_choice_hint",
    "cli_pass_choices",
    "cli_pass_question",
    "connection_status_message",
    "duration_phrase",
    "error_advice",
    "error_message",
    "error_next_step",
    "exit_code_for",
    "exit_code_for_outcome",
    "exit_code_for_signal",
    "flip_answer_label",
    "is_amber_category",
    "job_label",
    "job_state_for",
    "job_status_class",
    "local_time",
    "multi_page_manual_duplex_refusal",
    "outcome_line",
    "page_counts",
    "page_timeout_error",
    "pages_phrase",
    "pass_answer_label",
    "pass_cap_note",
    "pass_cap_warning",
    "pass_prompt_copy",
    "pass_wait_state",
    "progress_label",
    "rejection_message",
    "rejection_status_code",
    "removed_pages",
    "removed_pages_note",
    "scan_page_description",
    "sixteen_bit_error",
    "source_not_offered_error",
    "state_label",
    "substituted_source_warning",
    "timeout_finish_warning",
    "worker_health_detail",
]


PaperSize = Literal["full", "a3", "a4", "a5", "letter", "legal"]
"""
The paper sizes a profile can constrain a scan to.

``"full"`` means no constraint: the whole bed is scanned.  Every other name has
its dimensions in ``saneless.paper_sizes.PAPER_SIZES_MM``.
"""


# The member values for the two members whose names end in "PASS", named
# rather than written inline for the same reason ``_REJECTED_WIRE_VALUE`` is:
# ruff's S105 reads any string literal assigned to a name ending in "pass" as
# a hardcoded password.  A multi-page scan pass is not a password, the member
# names are fixed, and every StrEnum here has value == name, so the literals
# get names S105 does not flag rather than the convention getting an exception.
_NEXT_WAIT_STATE_VALUE = "AWAITING_NEXT_PASS"
_NEXT_WAIT_VALUE = "NEXT_PASS"


class JobState(StrEnum):
    """States in the scan job lifecycle."""

    PENDING = "PENDING"
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    AWAITING_NEXT_PASS = _NEXT_WAIT_STATE_VALUE
    AWAITING_BLANK_DECISION = "AWAITING_BLANK_DECISION"
    AWAITING_RETRY = "AWAITING_RETRY"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"
    FALLBACK = "FALLBACK"
    CANCELLED = "CANCELLED"


class ErrorCategory(StrEnum):
    """
    Categories of errors for programmatic handling.

    ``REJECTED`` is not a failure of a scan that ran.  It marks a job row
    written for a submit that was refused -- the queue was full, or the worker
    was down or degraded -- so the job never ran at all.
    ``JobStore.latest_run_job`` skips it, so the status area never reports a
    rejection as the job that just ended, while the history table still lists
    the row.

    ``ASSEMBLY`` means the scanned pages could not be assembled into a PDF --
    img2pdf or Pillow refused the images, or the output directory could not be
    written.  It is its own category so a full disk is never reported as a
    scanner failure.

    ``ALL_BLANK`` means empty-page detection judged every scanned page blank,
    so nothing was uploaded.  The scanner did its job, so this is its own
    category and is never reported as a scanner fault: the advice is to tune
    detection, not to check the scanner.

    ``UNCONFIRMED_SEND`` means the upload was sent but no usable answer came
    back, so paperless-ngx may hold the document.  ``UNCONFIRMED_FILING``
    means paperless-ngx accepted the upload -- it answered with a task id --
    but did not confirm filing it: the task failed for a reason other than a
    duplicate, or it did not finish while saneless waited.  Both are amber,
    not red, and neither advice says to start the scan again: the document
    may already be in paperless-ngx, so the reader checks its document list
    first, and imports the copy kept in ``failed/`` only if it is not there.
    They are two members rather than one because "received" is the stronger
    statement, and it is what the reader needs to judge whether a rescan is
    safe.

    ``PAPERLESS_VERSION`` means paperless-ngx refused every API version
    saneless speaks.  It is not ``UPLOAD``, whose advice to check the token
    would send the reader the wrong way; the fix is to upgrade paperless-ngx.

    ``DISK_SPACE`` means the server ran out of disk space for the scan, while
    scanning or while assembling the PDF.  It is its own category so a full
    disk is never reported as a scanner or PDF failure.  The error beside it
    names the folder and the space needed; the advice names neither.

    The values are persisted in the job store's ``error_category`` column,
    which is plain text with no constraint on its values.  Rows written
    before a member was added keep the category they were written with, and
    nothing is rewritten when one is added.  Every older value is still a
    member, so every older row still reads back.
    """

    FEEDER = "FEEDER"
    CONFIG = "CONFIG"
    SCANNER = "SCANNER"
    UPLOAD = "UPLOAD"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"
    ASSEMBLY = "ASSEMBLY"
    ALL_BLANK = "ALL_BLANK"
    UNCONFIRMED_SEND = "UNCONFIRMED_SEND"
    UNCONFIRMED_FILING = "UNCONFIRMED_FILING"
    PAPERLESS_VERSION = "PAPERLESS_VERSION"
    DISK_SPACE = "DISK_SPACE"


@dataclass(frozen=True, slots=True)
class ErrorAdvice:
    """
    What a reader is told about an error category, and what to do next.

    The two halves travel together because they are always rendered together:
    a message without a next step leaves the reader stuck, and a next step
    without a message leaves them guessing what went wrong.  Frozen and slotted
    so a renderer cannot edit approved copy in place, and so a typo cannot
    quietly add a third field that nothing renders.
    """

    message: str
    next_step: str


class PageCounted(Protocol):
    """
    Anything carrying a job's three page counts.

    It exists so this leaf module can format the counts sentence without
    importing ``job.py``, which imports from here.  ``Job`` and ``JobResult``
    satisfy it structurally; neither has to know the protocol exists.

    All three are ``int | None`` because they are NULL on every row that never
    counted anything -- ERROR, CANCELLED, REJECTED and every row written before
    the columns existed.
    """

    @property
    def pages_scanned(self) -> int | None:
        """Pages the scanner produced, or None if nothing counted them."""

    @property
    def pages_removed(self) -> int | None:
        """Pages discarded as blank, or None if nothing counted them."""

    @property
    def pages_uploaded(self) -> int | None:
        """Pages sent to paperless-ngx, or None if nothing counted them."""


class RemovedPagesNoted(Protocol):
    """
    Anything carrying the positions of the pages removed as blank.

    It exists for the same reason ``PageCounted`` does: this leaf module formats
    the note without importing ``job.py``.  ``Job``, ``JobResult`` and the web
    layer's ``JobView`` satisfy it structurally.
    """

    @property
    def removed_positions(self) -> tuple[int, ...] | None:
        """The 1-based scanned page numbers removed, or None if not recorded."""

    @property
    def pages_scanned(self) -> int | None:
        """Pages the scanner produced, or None if nothing counted them."""


class ProfileStorage(StrEnum):
    """
    What became of the generated scan profiles at startup.

    The worker makes exactly one attempt to persist generated profiles when it
    starts, and records the outcome here.  It has to be recorded rather than
    recomputed because ``_persist_generated_profiles`` returns ``None`` for two
    genuinely different situations -- no config file was loaded at all, and a
    config file was loaded but could not be written -- and the status strip's
    Profiles row must tell a household member which one happened.  One is
    "saneless has no config file to save to"; the other is "saneless has one
    and cannot write it", and only the second is worth investigating.

    A fresh ``os.access()`` probe at check time cannot substitute for the
    record.  The failure that matters is EBUSY on a single-file bind mount,
    where the directory is writable, ``os.access`` says yes, and only the
    rename fails.  The check reads what the write actually did.

    ``PERSISTED`` means the profiles are in the config file and will survive a
    restart.  Both ``IN_MEMORY_`` members mean they are in memory for this run
    only.

    "No config file was loaded" covers two situations, and this enum does not
    separate them: the searched directories held nothing, or one of them held a
    file under the old name that was detected and deliberately not read.
    ``ConfigFileState`` is what tells those apart, and it is the Configuration
    row, not the Profiles row, that reports the difference.
    """

    PERSISTED = "PERSISTED"
    IN_MEMORY_NO_CONFIG_FILE = "IN_MEMORY_NO_CONFIG_FILE"
    IN_MEMORY_UNWRITABLE = "IN_MEMORY_UNWRITABLE"


class ConfigFileState(StrEnum):
    """
    What the search for a configuration file found at startup.

    Recorded once, when the search runs, and never re-probed.  Which file was
    loaded is a fact about this process's past: the settings in hand came from
    that file, and a fresh stat cannot reproduce it -- a file created, renamed
    or deleted since startup would make a re-probe describe a program that is
    not running.  Mixing a recorded "loaded" with a freshly probed "stale"
    would also produce combinations none of these four members describe.
    Every surface that reports configuration -- the startup log, the status
    strip, ``saneless doctor`` and the one-shot commands -- reads the one
    recording, so they cannot disagree about the same appliance.

    ``LOADED`` is the healthy case.  ``LOADED_WITH_LEFTOVER`` is a warning and
    not a failure: the right file was read, and the file left beside it under
    the old name is a trap only for the next person to edit it, so the advice
    is to move anything still wanted out of it first.  ``NOT_FOUND`` is a
    warning too, because configuring saneless entirely through the environment
    is supported.  ``STALE_ONLY`` is a failure: a file under the old name is
    the only thing in the searched directories, so nothing the operator wrote
    was read, and saneless is running on defaults while appearing configured.
    """

    LOADED = "LOADED"
    LOADED_WITH_LEFTOVER = "LOADED_WITH_LEFTOVER"
    NOT_FOUND = "NOT_FOUND"
    STALE_ONLY = "STALE_ONLY"


class ExitCode(IntEnum):
    """
    The process exit codes of the saneless CLI.

    This is the one definition of the CLI exit codes.  The documentation
    tables that list them are pinned to this enum by a doc-truth test, so the
    two cannot drift apart.  Dispatch onto it is a ``match`` with
    ``assert_never`` in ``exit_code_for``, so a new ``ErrorCategory`` member
    fails the type gate until it is given an exit code.

    ``CANCELLED`` is 130, the shell's convention for a process stopped by
    SIGINT, because a cancel -- Ctrl-C or the operator declining the flip -- is
    a deliberate stop and not a failure.

    ``SAVED_TO_FOLDER`` (6) and ``UPLOADED_WITH_WARNING`` (7) are not failures
    either: each means a document *was* delivered, to the consume folder
    without its title, tags or correspondent, or to paperless-ngx with a
    warning about a skipped sheet or unequal duplex counts.  They are kept
    apart from ``PAPERLESS`` because a script that retries a paperless failure
    would scan the stack a second time.  When a run is both, the fallback
    wins, since the lost metadata is the larger problem.  They are chosen in
    ``exit_code_for_outcome``, not in ``exit_code_for``, because no error
    category leads to either.

    ``ALL_BLANK`` (8) means empty-page detection judged every page blank.
    Nothing was uploaded, and the pages were kept in ``failed/``, normally as
    one PDF (the page files, if it could not be built).  It
    is kept apart from ``SCAN`` because the scanner worked, and a script that
    checks the scanner on exit 1 would be sent the wrong way.

    ``UNCONFIRMED`` (9) means the document may already be in paperless-ngx:
    the upload was sent and no usable answer came back, or paperless-ngx
    received it and did not confirm filing it.  A copy is kept in
    ``failed/``.  It is kept apart from ``PAPERLESS`` because a script that
    retries on 3 is right to, and a script that retried on 9 could store the
    document twice: a script must never rescan on 9, and must check
    paperless-ngx's document list first.

    ``DISK_SPACE`` (10) means the server ran out of disk space for the scan,
    while scanning or while assembling the PDF.  The error line names the
    folder and how much space is needed.  It is kept apart from ``SCAN`` and
    ``PDF`` because neither the scanner nor the images were at fault, and
    freeing space is the fix.

    ``HANGUP`` (129) and ``TERMINATED`` (143) follow the shell's convention of
    128 plus the signal number, for a SIGHUP or a SIGTERM to a one-shot
    command.  They are an interruption rather than a cancel: nobody chose to
    stop, so the pages a scan already had are kept in ``failed/`` when they
    can be, unlike 130, which keeps nothing.  Not every interrupted command
    had pages -- any command but ``serve`` exits this way -- so the
    ``Interrupted:`` line is what says whether anything was kept, by naming
    its path, or where the pages were left.  A signal that arrives once a
    scan's outcome is settled does not produce either: the command exits
    with that outcome's own code.
    ``exit_code_for_signal`` chooses them.

    Members are declared in value order, the order every table pinned to
    this enum lists them in.
    """

    SUCCESS = 0
    SCAN = 1
    CONFIG = 2
    PAPERLESS = 3
    PDF = 4
    UNEXPECTED = 5
    SAVED_TO_FOLDER = 6
    UPLOADED_WITH_WARNING = 7
    ALL_BLANK = 8
    UNCONFIRMED = 9
    DISK_SPACE = 10
    HANGUP = 129
    CANCELLED = 130
    TERMINATED = 143


class ScanOutcome(StrEnum):
    """
    How a scan attempt resolved.

    There is no ``FAILED`` member, and there is not going to be one: a failure
    raises.  The ``outcome`` column therefore stays ``NULL`` on the error path
    and ``JobState.ERROR`` carries the failure on its own.  A returned
    ``FAILED`` would record the same fact in a second column, and two columns
    about a job with exactly one fate are two columns that can disagree.
    """

    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"


class FlipOutcome(StrEnum):
    """
    How a manual-duplex flip wait resolved.

    The four members are exhaustive over the ways that wait can end: the
    operator turned the stack and said so, the operator gave up, the clock
    ran out first, or saneless is stopping.  The last is not the operator's
    decision, so unlike giving up it keeps the pages already scanned: the
    pipeline answers it by raising ``ScanInterrupted``, the same
    "interrupted, not cancelled" ending a SIGTERM or SIGHUP gives a one-shot
    command.  ``FlipCoordinator.wait_for_flip`` returns exactly one of them,
    as one atomic answer -- which is the point, because the two events it
    replaced let a waiter wake up and then have to ask a second question to
    learn why.

    There is deliberately no "still waiting" member.  The method only returns
    once the wait has resolved, so such a value could never be observed, and
    a member no caller can receive is an arm every ``match`` would have to
    carry for nothing.
    """

    CONTINUED = "CONTINUED"
    ABORTED = "ABORTED"
    TIMED_OUT = "TIMED_OUT"
    INTERRUPTED = "INTERRUPTED"


class PassWait(StrEnum):
    """
    Which question a multi-page document is waiting on.

    The three members are exhaustive over the questions a multi-page scan can
    ask between passes: whether there is another page, what to do about pages
    that look blank, and what to do after a pass that failed.  Each has its own
    ``JobState`` (see ``pass_wait_state``), so a history row or ``saneless jobs``
    can say what the job is waiting on without asking the worker.
    """

    NEXT_PASS = _NEXT_WAIT_VALUE
    BLANK_DECISION = "BLANK_DECISION"
    RETRY = "RETRY"


class PassAnswer(StrEnum):
    """
    How a multi-page wait resolved.

    The eight members are exhaustive over the ways any of the three
    multi-page questions can end.  Six are the operator's answers: scan
    another pass, scan the last pass again, finish the document, abort it,
    and -- for pages that look blank -- skip or keep them.  ``TIMED_OUT`` is
    the clock running out first.  ``INTERRUPTED`` is saneless stopping; as
    with ``FlipOutcome.INTERRUPTED`` it is not the operator's decision, so it
    keeps the pages already scanned rather than discarding them.  Which of the
    operator's answers a given question accepts is carried by
    ``PassPrompt.offered``, not by this enum.

    This is a sibling of ``FlipOutcome`` and deliberately not a widening of
    it.  The flip wait's four outcomes, and what each of them means, must not
    change because a second kind of wait exists: every ``match`` over
    ``FlipOutcome`` would otherwise gain arms that can never be reached there.

    There is deliberately no "still waiting" member, for the same reason
    ``FlipOutcome`` has none: a wait only returns once it has resolved, so
    such a value could never be observed.
    """

    NEXT = "NEXT"
    RESCAN = "RESCAN"
    FINISH = "FINISH"
    ABORT = "ABORT"
    SKIP_BLANKS = "SKIP_BLANKS"
    KEEP_BLANKS = "KEEP_BLANKS"
    TIMED_OUT = "TIMED_OUT"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class PassPrompt:
    """
    One open multi-page question, as the operator is asked it.

    A prompt is built once per wait and never changed: it is frozen, so
    nothing between the worker that opens it and the surface that renders it
    can widen the answers it accepts.  The web UI and the CLI both render
    from this value directly.

    Attributes:
        number: The prompt's 1-based sequence number within the run.  An
            answer names the prompt it answers, so a click on a prompt that
            has already been replaced is recognised as stale.
        wait: Which question is open.
        pages_kept: How many pages the document holds now.  Pages skipped as
            blank are not counted.
        offered: The only answers this prompt accepts.  An answer outside
            this set -- stale, or forged -- is refused by set membership, so
            every consumer applies the same rule.
        timeout_seconds: How long the operator has to answer before the wait
            resolves as ``PassAnswer.TIMED_OUT``.
        last_pass_pages: For ``PassWait.NEXT_PASS``: how many pages the last
            accepted pass produced, kept and skipped together.
        last_pass_kept: For ``PassWait.NEXT_PASS``: how many of the last
            accepted pass's pages were kept.
        pass_pages: For ``PassWait.BLANK_DECISION``: how many pages the pass
            under decision produced.
        blank_positions: For ``PassWait.BLANK_DECISION``: the 1-based
            positions, within that pass, of the pages that look blank.
        error: For ``PassWait.RETRY``: the failed pass's error text.  It is
            unscrubbed, so a web renderer must scrub it before showing it.

    """

    number: int
    wait: PassWait
    pages_kept: int
    offered: frozenset[PassAnswer]
    timeout_seconds: float
    last_pass_pages: int = 0
    last_pass_kept: int = 0
    pass_pages: int = 0
    blank_positions: tuple[int, ...] = ()
    error: str | None = None


# The wire string for a rejected API token, named rather than written inline
# below.  Ruff's S105 reads any string literal assigned to a name containing
# "token" as a hardcoded credential; this is a public API value that
# ``docs/reference/web-api.md`` pins, not a secret, and neither the member name
# nor the string is free to change.
_REJECTED_WIRE_VALUE = "token_rejected"


class ConnectionStatus(StrEnum):
    """
    How a paperless-ngx connection test resolved.

    The values are lowercase snake_case and so break this module's otherwise
    uniform value-equals-name convention.  That is deliberate, not an
    oversight: ``web/routes.py:128`` serialises the value straight into the
    JSON body of ``GET /api/paperless/test`` and
    ``docs/reference/web-api.md:54-56`` documents the exact spelling of
    ``connected``, ``token_rejected`` and ``unreachable``.  Those three strings
    are a public wire contract and have to stay byte-identical; renaming them to
    match the member names would silently break every existing client.

    Only the *message* lookup below is a ``match`` with ``assert_never``.
    Deciding which member an HTTP response maps to is an ordered chain of
    status-code comparisons, and it lives in ``paperless.py`` -- for the same
    reason ``classify_error`` is an ``isinstance`` chain: it dispatches on a
    range of integers rather than on a closed set of enum members, so
    ``assert_never`` does not apply and a trailing fallback is the correct
    total answer.  This module imports no HTTP client and never maps a
    response status to a connection outcome.

    INCOMPATIBLE is a paperless-ngx that answered 406 Not Acceptable: it does
    not allow API version 9 or 10, so it is older than 2.16 or newer than
    this saneless knows.  Its wire value is ``incompatible_version``.
    """

    CONNECTED = "connected"
    TOKEN_REJECTED = _REJECTED_WIRE_VALUE
    NOT_FOUND = "not_found"
    SERVER_ERROR = "server_error"
    UNREACHABLE = "unreachable"
    INCOMPATIBLE = "incompatible_version"


class WorkerHealth(StrEnum):
    """
    How healthy the scan worker is, as ``/health`` reports it.

    ``DOWN`` means the worker thread is not alive.  ``DEGRADED`` means the
    thread is alive but has hit N consecutive loop-level failures -- job store
    writes, pruning -- and no store probe has succeeded since.
    ``HEALTHY`` is everything else.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class SubmitResult(StrEnum):
    """
    What ``ScanWorker.submit`` reports instead of blocking.

    ``ACCEPTED`` means the job is queued.  ``QUEUE_FULL`` means the bounded
    queue had no room.  ``DOWN`` covers every state in which the worker cannot
    take work at all: not started, stopped, and stopping.  ``DEGRADED`` means
    the thread is alive but the job store is failing, so a job could not be
    recorded truthfully.
    """

    ACCEPTED = "ACCEPTED"
    QUEUE_FULL = "QUEUE_FULL"
    DOWN = "DOWN"
    DEGRADED = "DEGRADED"


# The member value for the unset-token refusal, named rather than written
# inline, for the same reason ``_REJECTED_WIRE_VALUE`` is: ruff's S105 reads any
# string literal assigned to a name containing "token" as a hardcoded
# credential.  The member name is fixed and every StrEnum in this module has
# value == name, so the literal gets a name S105 does not flag rather than the
# convention getting an exception.
_UNSET_REJECTION_VALUE = "TOKEN_UNSET"


class RequestRejection(StrEnum):
    """
    Every error the web layer renders, one member per message.

    ``rejection_message`` and ``rejection_status_code`` give each member its
    user-facing sentence and its HTTP status.  The messages are developer
    constants: none of them contains request input or exception text, so
    nothing a client sent and nothing internal can reach the page through this
    path (V7).
    """

    QUEUE_FULL = "QUEUE_FULL"
    WORKER_DOWN = "WORKER_DOWN"
    WORKER_DEGRADED = "WORKER_DEGRADED"
    # Its own member rather than a reuse of WORKER_DEGRADED.  Degraded
    # says "the scan service was unavailable", which is untrue here: the
    # service is fine and nobody set the paperless-ngx API token.  Sharing the
    # member would send a household member looking for a broken server.
    TOKEN_UNSET = _UNSET_REJECTION_VALUE
    # The same refusal for the other half of an unconfigured paperless-ngx: an
    # empty ``paperless.url``.  Its own member so the sentence names the
    # setting to fill in rather than the token.
    URL_UNSET = "URL_UNSET"
    UNKNOWN_PROFILE = "UNKNOWN_PROFILE"
    # Multiple pages asked for on a manual-duplex profile.  The form disables
    # the checkbox for such a profile, so this is the refusal a forged or stale
    # submit gets, before any job row exists; its sentence says what to change.
    MULTI_PAGE_MANUAL_DUPLEX = "MULTI_PAGE_MANUAL_DUPLEX"
    TITLE_TOO_LONG = "TITLE_TOO_LONG"
    # A tab, ESC or any other C0/C1 control in the title: refused, never
    # stripped, and its sentence names the field to fix.
    TITLE_HAS_CONTROL = "TITLE_HAS_CONTROL"
    INVALID_REQUEST = "INVALID_REQUEST"
    CROSS_SITE = "CROSS_SITE"
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    INTERNAL = "INTERNAL"
    CLIENT_ERROR = "CLIENT_ERROR"
    # A well-formed Host that does not name saneless, sent with 421.  The
    # sentence is fixed; the refused Host travels beside it, never inside it.
    HOST_NOT_ALLOWED = "HOST_NOT_ALLOWED"


# The one title length cap.  The ``Form(max_length=...)`` validation on the scan
# route, the ``maxlength`` attribute on the title input and the TITLE_TOO_LONG
# message all read this constant, so the three cannot drift apart.
TITLE_MAX_LENGTH: Final = 256

# Job-row error texts.  A submit refused because the queue was full, the
# worker was down or degraded, or the paperless-ngx API token or address was
# never set still writes a job row, so history shows the attempt; these are that row's
# ``error``.  Like every other ``job.error`` they carry no trailing period.
QUEUE_FULL_JOB_ERROR: Final = "Not started: the scan queue was full"
WORKER_DOWN_JOB_ERROR: Final = "Not started: the scan service was not running"
WORKER_DEGRADED_JOB_ERROR: Final = "Not started: the scan service was unavailable"
# The literal is named first, and the exported constant aliases it, for the
# same reason ``_REJECTED_WIRE_VALUE`` is: ruff's S105 reads any string literal
# assigned to a name containing "token" as a hardcoded credential.  This is
# job-row copy *about* a token nobody set, not a token, and the exported name
# is fixed, so the literal gets a name S105 does not flag.
_UNSET_CREDENTIAL_JOB_ERROR = (
    "Not started: the paperless-ngx API token has not been set"
)
TOKEN_UNSET_JOB_ERROR: Final = _UNSET_CREDENTIAL_JOB_ERROR
URL_UNSET_JOB_ERROR: Final = "Not started: the paperless-ngx address has not been set"

# Why the Scan button is greyed out, rendered as a line beneath it.  It
# deliberately does not repeat the fix: the status strip's Paperless row, a few
# centimetres above on the same page, already carries "Put a real API token in
# the saneless config file, then restart saneless." as its next step.  Three
# surfaces name the same problem in the same words; only one owns the remedy.
#
# Like every other string here it is a developer constant: it names the problem
# and nothing else -- never the token value and never the paperless-ngx URL,
# which says where paperless-ngx runs (ASVS V7).  The em dash is the
# same one ``web/routes.py``'s paused-checks prefix already uses.
SCAN_BLOCKED_REASON: Final = (
    "The paperless-ngx API token has not been set — see System status above."
)
# The same line when the token is set but ``paperless.url`` is empty.
SCAN_BLOCKED_URL_REASON: Final = (
    "The paperless-ngx address has not been set — see System status above."
)

# What startup recovery passes to ``JobStore.fail_active_jobs`` for a job the
# previous process left in flight, and how a job a server stop interrupted
# begins its error, before the sentence naming what was kept.
RESTART_REASON: Final = "The server restarted before this scan finished"

# The words for a delivered scan that did not go cleanly.  A DONE job carrying
# a warning is labelled WARNED_UPLOAD_LABEL rather than "Complete", and a
# FALLBACK adds FALLBACK_NOT_UPLOADED_LINE on the CLI's stderr to say what was
# lost.  They live here once, beside "Complete" and "Saved to folder" in
# ``state_label``, so the CLI outcome line, the web status area and the
# history row cannot drift apart.  Neither carries a configured value.
WARNED_UPLOAD_LABEL: Final = "Uploaded with a warning"

# The words for a failure that may already be in paperless-ngx, in place of
# "Failed".  The job is an ERROR, but "Failed" -- like the red it is drawn in --
# reads as "scan it again", and a rescan here can file the document twice.  The
# first is for an upload that was sent with no usable answer back; the second,
# the stronger statement, for one paperless-ngx accepted but did not confirm
# filing.  ``job_label`` picks them from the category, so the history row and
# ``saneless jobs`` cannot drift apart.  Both fit the status column of
# ``saneless jobs``, which is sized from the widest state label.
UNCONFIRMED_SEND_LABEL: Final = "May be in paperless-ngx"
UNCONFIRMED_FILING_LABEL: Final = "Received, not confirmed"
FALLBACK_NOT_UPLOADED_LINE: Final = (
    "Not uploaded: saved to the consume folder without its title, tags or correspondent"
)

# What the web page shows of a job in place of its detail, to every browser
# other than the one that submitted it.  They see that a scan happened and how
# it ended -- profile, state, time, outcome and page counts -- never what the
# document is: no title, no thumbnail and none of the stored error or warning
# text, which names host paths, the kept file (whose name carries the title)
# and the paperless-ngx address.  ``saneless.web.job_view`` is the one place
# that decides who sees which.
#
# Every string here is a developer constant with no path and no URL in it.
# The warning line must stay non-empty: ``job_label`` and ``outcome_line`` read
# a warning's truthiness to say "Uploaded with a warning", and a hidden
# warning still has to read as one.  The full text stays in the log and in
# ``saneless jobs --json``, which is where each sentence sends the reader.
HIDDEN_JOB_TITLE: Final = "Scan (title hidden)"
HIDDEN_WARNING_LINE: Final = (
    "This scan has a warning. On the server, saneless jobs --json shows it."
)
HIDDEN_PRESERVED_ERROR: Final = (
    "The scan was kept in the failed folder on the server. "
    "saneless jobs --json shows where."
)
HIDDEN_ERROR_DETAIL: Final = (
    "On the server, saneless jobs --json and the log show the full message."
)

# How every user-facing timestamp is spelled, on the web page and in the CLI
# table alike.  ``%Z`` is on the format rather than in a column caption so
# the zone is named on every line and a copy-pasted timestamp is
# self-describing.  One constant, read by the Jinja filter and by ``cli.py``,
# is what stops the two surfaces from disagreeing.  Seconds are deliberately
# absent: they buy nothing a reader wants and they cost the CLI table three
# columns of title at 80 columns.
LOCAL_TIME_FORMAT: Final = "%Y-%m-%d %H:%M %Z"

# The separator between a count and the progress prose on the busy line: the
# manual-duplex front count, or the pages a multi-page document holds so far.
# U+00B7 MIDDLE DOT with a space either side.
_BUSY_SEPARATOR: Final = "·"

# The acknowledgements a flip wait and a multi-page wait share, so the two
# can never drift apart: aborting, or saneless stopping while either waits.
_ABORTING_ACKNOWLEDGEMENT: Final = "Aborting scan..."
_INTERRUPTED_ACKNOWLEDGEMENT: Final = (
    "Stopping: saneless is shutting down and keeping the pages already scanned..."
)

_SECONDS_PER_MINUTE: Final = 60
_SECONDS_PER_HOUR: Final = 3600


ACTIVE_STATES: frozenset[JobState] = frozenset(
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
"""Job states where the job is still in flight.

A job in one of these states has not reached an outcome yet, so the UI keeps
polling it.
"""

TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.DONE, JobState.ERROR, JobState.FALLBACK, JobState.CANCELLED}
)
"""Job states where the job has reached its final outcome.

Together with ``ACTIVE_STATES`` this partitions ``JobState``: every member is in
exactly one of the two sets.
"""

PASS_WAIT_STATES: frozenset[JobState] = frozenset(
    {
        JobState.AWAITING_NEXT_PASS,
        JobState.AWAITING_BLANK_DECISION,
        JobState.AWAITING_RETRY,
    }
)
"""Job states where a multi-page document is waiting for the operator's answer.

One state per ``PassWait`` question, so the row itself says what is being
waited on.  ``AWAITING_FLIP`` is not here: it waits for a person too, but for
a manual-duplex flip, not for a multi-page answer.
"""

WAITING_STATES: frozenset[JobState] = PASS_WAIT_STATES | {JobState.AWAITING_FLIP}
"""Job states where the job is in flight but waiting for a person.

The scanner is idle and nothing happens until someone acts: the flip wait and
the three multi-page waits.  Every one of them is in ``ACTIVE_STATES`` -- the
job is not finished -- and none is in ``BUSY_STATES``.
"""

BUSY_STATES: frozenset[JobState] = ACTIVE_STATES - WAITING_STATES
"""Job states where the machine itself is working.

Derived from ``ACTIVE_STATES`` and ``WAITING_STATES`` so the three can never
drift apart.  The distinction is "the machine is working" versus "we are
waiting for a person": a job in ``WAITING_STATES`` is active -- it is not
finished -- but the scanner is idle and the person has to act.  The web UI's
scan-button text and its ``aria-busy`` attribute depend on that difference, so
they read ``BUSY_STATES`` and not ``ACTIVE_STATES``.  A waiting job that showed
the busy spinner would look like a scan that hung.
"""


# The three multi-page waits as a type, so their labels can live in helpers of
# their own and still be checked for exhaustiveness.  With the waits inline,
# ``state_label`` and ``progress_label`` would each carry one arm per
# ``JobState`` member and cross ruff's PLR0912 branch limit; the limit is
# respected rather than raised, and nothing is suppressed.
type _PassWaitState = Literal[
    JobState.AWAITING_NEXT_PASS,
    JobState.AWAITING_BLANK_DECISION,
    JobState.AWAITING_RETRY,
]


def _pass_wait_state_label(state: _PassWaitState) -> str:
    """
    Return the short history label for a multi-page wait.

    Each names what the job is waiting on, so the row says it without the
    worker.

    Args:
        state: The multi-page wait to label.

    Returns:
        The user-facing label, e.g. ``"Waiting for more pages"``.

    Raises:
        AssertionError: If the value is not one of the three waits.

    """
    match state:
        case JobState.AWAITING_NEXT_PASS:
            label = "Waiting for more pages"
        case JobState.AWAITING_BLANK_DECISION:
            label = "Waiting: blank pages found"
        case JobState.AWAITING_RETRY:
            label = "Waiting: last scan failed"
        case _:
            assert_never(state)
    return label


def _pass_wait_progress_label(state: _PassWaitState) -> str:
    """
    Return the progress prose for a multi-page wait.

    Args:
        state: The multi-page wait to describe.

    Returns:
        The user-facing progress sentence, e.g. ``"Waiting for the next page..."``.

    Raises:
        AssertionError: If the value is not one of the three waits.

    """
    match state:
        case JobState.AWAITING_NEXT_PASS:
            label = "Waiting for the next page..."
        case JobState.AWAITING_BLANK_DECISION:
            label = "Waiting for a decision about blank pages..."
        case JobState.AWAITING_RETRY:
            label = "The last scan failed; waiting for a decision..."
        case _:
            assert_never(state)
    return label


def state_label(state: JobState) -> str:
    """
    Return the short human label for a job state.

    These are the labels the history table has always rendered; they are part
    of the user-visible surface and must not be reworded casually.

    Args:
        state: The job state to label.

    Returns:
        The user-facing label, e.g. ``"Waiting for flip"``.

    Raises:
        AssertionError: If the value is not a JobState member.

    """
    match state:
        case JobState.PENDING:
            label = "Pending"
        case JobState.SCANNING:
            label = "Scanning"
        case JobState.AWAITING_FLIP:
            label = "Waiting for flip"
        case (
            JobState.AWAITING_NEXT_PASS
            | JobState.AWAITING_BLANK_DECISION
            | JobState.AWAITING_RETRY
        ):
            label = _pass_wait_state_label(state)
        case JobState.SCANNING_REVERSE:
            label = "Scanning backs"
        case JobState.ASSEMBLING:
            label = "Assembling"
        case JobState.UPLOADING:
            label = "Uploading"
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case JobState.FALLBACK:
            label = "Saved to folder"
        case JobState.CANCELLED:
            label = "Cancelled"
        case _:
            assert_never(state)
    return label


def job_label(
    state: JobState, warning: str | None, category: ErrorCategory | None = None
) -> str:
    """
    Return the history-row label for a job, taking its warning into account.

    A DONE job that carries a warning -- a sheet the scanner skipped, or
    manual-duplex front and back counts that differed -- was uploaded, but
    calling it "Complete" would hide the warning from anyone skimming the
    history.  It gets ``WARNED_UPLOAD_LABEL`` instead.  An ERROR whose category
    is amber (``is_amber_category``) may already be in paperless-ngx, so it
    gets that category's label rather than "Failed".  Every other case,
    including a FALLBACK with a warning, is exactly ``state_label``: there is no
    ``JobState`` member for a warned upload, only a DONE plus a warning, and no
    member for a maybe-delivered one, only an ERROR plus its category.

    Args:
        state: The job state to label.
        warning: The job's warning, if any.  An empty string is no warning.
        category: The job's error category, if any.  Only an ERROR reads it.

    Returns:
        The user-facing label, e.g. ``"Uploaded with a warning"``.

    Raises:
        AssertionError: If the value is not a JobState member.

    """
    if state is JobState.DONE and warning:
        return WARNED_UPLOAD_LABEL
    if state is JobState.ERROR and category is not None and is_amber_category(category):
        return _unconfirmed_label(category)
    return state_label(state)


def _unconfirmed_label(category: _UnconfirmedCategory) -> str:
    """
    Return the history-row label for a failure that may be in paperless-ngx.

    Args:
        category: One of the two amber categories.

    Returns:
        ``UNCONFIRMED_SEND_LABEL`` or ``UNCONFIRMED_FILING_LABEL``.

    Raises:
        AssertionError: If the value is not one of the two amber categories.

    """
    match category:
        case ErrorCategory.UNCONFIRMED_SEND:
            label = UNCONFIRMED_SEND_LABEL
        case ErrorCategory.UNCONFIRMED_FILING:
            label = UNCONFIRMED_FILING_LABEL
        case _:
            assert_never(category)
    return label


def is_amber_category(category: ErrorCategory) -> TypeIs[_UnconfirmedCategory]:
    """
    Return whether a failure in this category is drawn amber rather than red.

    Red reads as "scan it again".  A failure that may already be in
    paperless-ngx -- sent with no usable answer, or accepted and not confirmed
    -- must not say that before the reader has checked the document list, so
    it wears the amber of a warned upload instead.  Every other failure did
    not deliver the scan, and is red.

    The match is exhaustive, so a new category has to choose its tone here.
    A True answer also narrows the category to the two amber members, so a
    caller can go on to pick the amber label or advice without a second list.

    Args:
        category: The failed job's error category.

    Returns:
        True for ``UNCONFIRMED_SEND`` and ``UNCONFIRMED_FILING``, else False.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    match category:
        case ErrorCategory.UNCONFIRMED_SEND | ErrorCategory.UNCONFIRMED_FILING:
            amber = True
        case (
            ErrorCategory.FEEDER
            | ErrorCategory.CONFIG
            | ErrorCategory.SCANNER
            | ErrorCategory.UPLOAD
            | ErrorCategory.UNKNOWN
            | ErrorCategory.REJECTED
            | ErrorCategory.ASSEMBLY
            | ErrorCategory.ALL_BLANK
            | ErrorCategory.PAPERLESS_VERSION
            | ErrorCategory.DISK_SPACE
        ):
            amber = False
        case _:
            assert_never(category)
    return amber


def job_status_class(
    state: JobState, warning: str | None, category: ErrorCategory | None
) -> str:
    """
    Return the CSS class that colours a job's status wherever it is listed.

    The one place a row's tone is decided, so no template compares states to
    pick a class.  A clean DONE is green.  A warned DONE and a FALLBACK are
    amber: delivered, but not cleanly.  An ERROR is red unless its category is
    amber (``is_amber_category``), and an ERROR recorded before categories
    existed is red.  A cancel is its own muted colour.  A job still in flight
    gets no class at all.

    Args:
        state: The job state.
        warning: The job's warning, if any.  An empty string is no warning.
        category: The job's error category, if any.  Only an ERROR reads it.

    Returns:
        ``"status-done"``, ``"status-fallback"``, ``"status-error"``,
        ``"status-cancelled"``, or ``""`` for an active job.

    Raises:
        AssertionError: If the value is not a JobState member.

    """
    match state:
        case JobState.DONE:
            css = "status-fallback" if warning else "status-done"
        case JobState.FALLBACK:
            css = "status-fallback"
        case JobState.ERROR:
            amber = category is not None and is_amber_category(category)
            css = "status-fallback" if amber else "status-error"
        case JobState.CANCELLED:
            css = "status-cancelled"
        case (
            JobState.PENDING
            | JobState.SCANNING
            | JobState.AWAITING_FLIP
            | JobState.AWAITING_NEXT_PASS
            | JobState.AWAITING_BLANK_DECISION
            | JobState.AWAITING_RETRY
            | JobState.SCANNING_REVERSE
            | JobState.ASSEMBLING
            | JobState.UPLOADING
        ):
            css = ""
        case _:
            assert_never(state)
    return css


def outcome_line(state: JobState, warning: str | None, title: str) -> str:
    """
    Return the one-line headline for a delivered scan.

    This is the line the CLI prints on stdout when a scan finishes and the
    headline of the web status area.  Only a clean DONE says ``Done:``; a
    warned DONE and a FALLBACK each name what went wrong, so no surface can
    report a degraded scan as a clean one.

    Args:
        state: The job's terminal state, DONE or FALLBACK.
        warning: The job's warning, if any.  An empty string is no warning.
        title: The document title, appended after the colon.

    Returns:
        The headline, e.g. ``"Uploaded with a warning: Invoice"``.

    Raises:
        ValueError: If ``state`` is not a delivered outcome.  A job that
            failed, was cancelled or is still running has no outcome line.

    """
    match state:
        case JobState.DONE:
            prefix = WARNED_UPLOAD_LABEL if warning else "Done"
        case JobState.FALLBACK:
            prefix = state_label(JobState.FALLBACK)
        case _:
            msg = f"{state.value} is not a delivered outcome, so has no outcome line"
            raise ValueError(msg)
    return f"{prefix}: {title}"


def progress_label(state: JobState) -> str:
    """
    Return the progress prose for a job state.

    This is the longer sentence the status area shows while a scan is running,
    and the line the CLI echoes.  The six original in-flight strings are byte
    identical to what shipped before -- including the literal three-period
    spelling of the trailing ellipsis, which is three ASCII periods and not
    U+2026.  ``SCANNING_REVERSE``'s prose is the exact line the CLI printed for
    the second duplex pass before the state existed, so giving pass B its own
    state changed no CLI output.  The three multi-page waits keep the same
    ellipsis and each names what it is waiting on, so a job that is waiting
    for a person never reads as a scan that is still running.

    ``DONE``, ``ERROR``, ``FALLBACK`` and ``CANCELLED`` have no progress prose
    in production: the status partial and the CLI both branch structurally for
    the four terminal states.  Their arms exist so the lookup is total and a
    future member cannot be forgotten; they have no production caller.

    Args:
        state: The job state to describe.

    Returns:
        The user-facing progress sentence, e.g. ``"Assembling PDF..."``.

    Raises:
        AssertionError: If the value is not a JobState member.

    """
    match state:
        case JobState.PENDING:
            label = "Starting scan..."
        case JobState.SCANNING:
            label = "Scanning..."
        case JobState.AWAITING_FLIP:
            label = "Awaiting flip..."
        case (
            JobState.AWAITING_NEXT_PASS
            | JobState.AWAITING_BLANK_DECISION
            | JobState.AWAITING_RETRY
        ):
            label = _pass_wait_progress_label(state)
        case JobState.SCANNING_REVERSE:
            label = "Scanning reverse sides..."
        case JobState.ASSEMBLING:
            label = "Assembling PDF..."
        case JobState.UPLOADING:
            label = "Uploading to paperless-ngx..."
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case JobState.FALLBACK:
            label = "Saved to folder"
        case JobState.CANCELLED:
            label = "Cancelled"
        case _:
            assert_never(state)
    return label


def busy_line(
    state: JobState,
    *,
    queue_title: str | None = None,
    queue_ahead: int | None = None,
    front_pages: int | None = None,
    pages_kept: int | None = None,
) -> str:
    """
    Return the one line the status area shows while a job is in flight.

    Four branches in strict precedence:

    1. The job is queued behind another one, so it is told what it is waiting
       for and how many jobs are ahead.  This wins outright: a job that has not
       started has nothing else worth saying.
    2. The job is on the second manual-duplex pass and the front count is
       known, so the count leads the progress prose.
    3. The job is scanning a later pass of a multi-page document that already
       holds at least one page, so the kept count leads the progress prose.
       With no page kept yet the first pass reads exactly as before.
    4. Otherwise the progress prose alone, exactly as before.

    ``(0 ahead of you)`` is never produced.  It is technically true and reads
    like a bug, so the last job in the queue is told it is ``next in line``.

    The trailing phrase in branch 2 is ``progress_label(SCANNING_REVERSE)`` --
    master-pinned copy with its own tests -- and not the history table's
    ``state_label``, which is a different owner with a different string.  The
    choice is deliberate.

    ``queue_title`` is the only user data any string here carries.  It is
    returned as plain text, escaped by Jinja's autoescape at render time, and
    is never logged from this module.

    Args:
        state: The state of the job being followed.
        queue_title: The title of the job ahead, when there is one.
        queue_ahead: How many jobs are ahead of the followed job.
        front_pages: Pages counted on the first manual-duplex pass, when that
            count is known.
        pages_kept: Pages a multi-page document holds so far, when it is one.
            Only read while the job is ``SCANNING``.

    Returns:
        One line of plain text.

    """
    if queue_title is not None and queue_ahead is not None:
        position = "next in line" if queue_ahead == 0 else f"{queue_ahead} ahead of you"
        return f"Waiting for '{queue_title}' to finish ({position})"
    label = progress_label(state)
    if state is JobState.SCANNING_REVERSE and front_pages is not None:
        return f"Front: {pages_phrase(front_pages)} {_BUSY_SEPARATOR} {label}"
    if state is JobState.SCANNING and pages_kept:
        return f"{pages_phrase(pages_kept)} so far {_BUSY_SEPARATOR} {label}"
    return label


def local_time(value: datetime) -> str:
    """
    Render a timestamp in the server's local zone, with the zone named.

    The argument must be timezone-aware.  Every timestamp saneless persists is
    (``JobStore`` writes ``datetime.now(tz=UTC)`` and ``_row_to_job`` parses it
    back from the isoformat string), so a naive value reaching here is a bug in
    the caller, not a case to guess at.

    ``astimezone()`` is called with no argument, so the zone is the process's
    own whatever zone the value carries.  That makes ``TZ`` load-bearing: a
    container reports UTC unless it is set, which does name a zone and helps
    nobody.  No ``zoneinfo`` import and no new config key is
    involved -- the operator's ``TZ`` is the single source.

    ``%Z`` renders as the empty string when the platform reports no zone
    abbreviation, which leaves the separator before it dangling at the end of
    the value.  ``resolve_job_title`` interpolates this result directly into
    ``f"Scan {local_time(now)}"``, so an unstripped value would file a
    paperless-ngx document whose title ends in a space -- an invisible
    difference from every other host's titles, in an artefact that leaves the
    appliance.  Hence the strip.  It is deliberately trailing-only: the space
    between the date and the time is part of the format and stays, which is
    why this is ``rstrip`` and not ``" ".join(value.split())``.

    Args:
        value: A timezone-aware timestamp.

    Returns:
        The timestamp as ``2026-09-16 14:03 CDT``, with no trailing
        whitespace.

    """
    return value.astimezone().strftime(LOCAL_TIME_FORMAT).rstrip()


def page_counts(job: PageCounted) -> str | None:
    """
    Return the page-count sentence for a job, or None if it has no counts.

    A NULL count renders nothing at all -- no element, no empty line -- and one
    NULL is enough to suppress the whole sentence, because a sentence naming
    two of three counts invites the reader to wonder about the third.
    This is the common path, not an edge: four of the six terminal cases have
    no counts by construction (ERROR, CANCELLED, REJECTED and every row written
    before the columns existed).

    A measured ``0`` is not a NULL and renders as ``0``.  A scan where nothing
    was blank really did remove 0 pages.  Consumers must therefore guard on
    ``is not None`` and never on truthiness, in Python and in Jinja alike
    (``is not none``), or a real zero disappears.

    Only the first clause carries a noun, so only the first clause pluralises.

    Args:
        job: Anything carrying the three page counts.

    Returns:
        ``"12 pages scanned, 2 blank removed, 10 uploaded"``, or None if any
        of the three counts is NULL.

    """
    scanned = job.pages_scanned
    removed = job.pages_removed
    uploaded = job.pages_uploaded
    if scanned is None or removed is None or uploaded is None:
        return None
    noun = "page" if scanned == 1 else "pages"
    return f"{scanned} {noun} scanned, {removed} blank removed, {uploaded} uploaded"


def removed_pages_note(
    positions: Sequence[int] | None, scanned: int | None
) -> str | None:
    """
    Return the sentence naming the pages removed as blank, or None.

    Removed pages are not kept, so this sentence is how the operator learns
    which sheets to rescan if a real page was taken for a blank one.  It is an
    informational note shown beside the page counts, never a warning: a scan
    that dropped the blank backs of a duplex stack is an ordinary success, and
    it must stay a plain DONE rather than turn amber.

    Pages are numbered by their scanned position in document order -- the
    numbering the per-page log lines use -- so the note reads the same for
    every source.  ``scanned`` is guarded with ``is None``, never truthiness,
    for the same reason ``page_counts`` is.

    Args:
        positions: The 1-based scanned page numbers removed, or None when
            nothing recorded them.
        scanned: How many pages the scanner produced, or None when uncounted.

    Returns:
        ``"Removed as blank: pages 2, 4, 6 of 12 scanned."`` (``page 3`` for a
        single page), or None when there are no positions to name or no
        scanned count to name them against.

    """
    if positions is None or not positions or scanned is None:
        return None
    noun = "page" if len(positions) == 1 else "pages"
    listed = ", ".join(str(position) for position in positions)
    return f"Removed as blank: {noun} {listed} of {scanned} scanned."


def removed_pages(job: RemovedPagesNoted) -> str | None:
    """
    Return the removed-pages note for a job, for the templates' filter.

    Args:
        job: Anything carrying the removed positions and the scanned count.

    Returns:
        What :func:`removed_pages_note` returns for the job's two fields.

    """
    return removed_pages_note(job.removed_positions, job.pages_scanned)


def flip_answer_label(outcome: FlipOutcome) -> str:
    """
    Return the acknowledgment the status area shows for an answered flip wait.

    Once a job's flip wait has been answered but the worker has not yet
    persisted the job's next state, the status area shows this sentence in
    place of the flip prompt, so the Continue and Abort buttons do not come
    back as though the click did nothing.  The trailing ellipsis is
    three ASCII periods, matching ``progress_label``.

    ``TIMED_OUT`` has an arm for totality: a timed-out job moves to ``ERROR``
    within the same worker step, so the status area is not expected to show
    it.  ``INTERRUPTED`` is what a stopping server answers; a poll that lands
    before the job's row moves on says the pages already scanned are kept.

    Args:
        outcome: The answer the job's flip wait received.

    Returns:
        The user-facing acknowledgment, e.g. ``"Aborting scan..."``.

    Raises:
        AssertionError: If the value is not a FlipOutcome member.

    """
    match outcome:
        case FlipOutcome.CONTINUED:
            label = "Flip confirmed. Scanning reverse sides next..."
        case FlipOutcome.ABORTED:
            label = _ABORTING_ACKNOWLEDGEMENT
        case FlipOutcome.TIMED_OUT:
            label = "Flip wait timed out..."
        case FlipOutcome.INTERRUPTED:
            label = _INTERRUPTED_ACKNOWLEDGEMENT
        case _:
            assert_never(outcome)
    return label


def pass_wait_state(wait: PassWait) -> JobState:
    """
    Return the job state a multi-page question puts its job in.

    The one mapping from a question to its state: the pipeline derives the
    waiting event it announces from this, so the two cannot drift apart.

    Args:
        wait: The question that is open.

    Returns:
        The member of ``PASS_WAIT_STATES`` naming that question.

    Raises:
        AssertionError: If the value is not a PassWait member.

    """
    match wait:
        case PassWait.NEXT_PASS:
            state = JobState.AWAITING_NEXT_PASS
        case PassWait.BLANK_DECISION:
            state = JobState.AWAITING_BLANK_DECISION
        case PassWait.RETRY:
            state = JobState.AWAITING_RETRY
        case _:
            assert_never(wait)
    return state


def pass_answer_label(answer: PassAnswer) -> str:
    """
    Return the acknowledgment the status area shows for an answered pass wait.

    The multi-page counterpart of ``flip_answer_label``: once a multi-page
    question has been answered but the worker has not yet persisted the job's
    next state, the status area shows this sentence in place of the prompt,
    so its buttons do not come back as though the click did nothing.  The
    trailing ellipsis is three ASCII periods, matching ``progress_label``.
    Aborting and being interrupted read exactly as they do for a flip wait.

    ``TIMED_OUT`` has an arm for totality: a timed-out wait finishes the
    document within the same worker step, so the status area is not expected
    to show it.

    Args:
        answer: The answer the job's multi-page wait received.

    Returns:
        The user-facing acknowledgment, e.g. ``"Scanning more pages..."``.

    Raises:
        AssertionError: If the value is not a PassAnswer member.

    """
    match answer:
        case PassAnswer.NEXT:
            label = "Scanning more pages..."
        case PassAnswer.RESCAN:
            label = "Discarded the last scan. Scanning again..."
        case PassAnswer.FINISH:
            label = "Finishing the document..."
        case PassAnswer.ABORT:
            label = _ABORTING_ACKNOWLEDGEMENT
        case PassAnswer.SKIP_BLANKS:
            label = "Skipping blank pages..."
        case PassAnswer.KEEP_BLANKS:
            label = "Keeping blank pages..."
        case PassAnswer.TIMED_OUT:
            label = "Nobody answered; finishing the document..."
        case PassAnswer.INTERRUPTED:
            label = _INTERRUPTED_ACKNOWLEDGEMENT
        case _:
            assert_never(answer)
    return label


def _counted(count: int, unit: str) -> str:
    """
    Return ``count`` followed by ``unit``, pluralised unless the count is one.

    Args:
        count: How many.
        unit: The singular noun, which pluralises with a trailing ``s``.

    Returns:
        E.g. ``"1 page"`` or ``"4 pages"``.

    """
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def pages_phrase(count: int) -> str:
    """
    Return a page count with its noun.

    Args:
        count: How many pages.

    Returns:
        ``"1 page"`` for one page, ``"4 pages"`` or ``"0 pages"`` otherwise.

    """
    return _counted(count, "page")


def duration_phrase(seconds: float) -> str:
    """
    Name an operator-wait bound in the largest unit that divides it evenly.

    The value is rounded to whole seconds first, so a configured ``600.0``
    reads as ``10 minutes``.  A bound that is a whole number of hours is named
    in hours, else one that is a whole number of minutes in minutes, else in
    seconds: ``5400`` is ``90 minutes``, not ``1.5 hours``, and ``90`` is
    ``90 seconds``, not ``1.5 minutes``.

    Args:
        seconds: The bound in seconds.

    Returns:
        E.g. ``"10 minutes"``, ``"1 hour"`` or ``"90 seconds"``.

    """
    whole = round(seconds)
    if whole and whole % _SECONDS_PER_HOUR == 0:
        return _counted(whole // _SECONDS_PER_HOUR, "hour")
    if whole and whole % _SECONDS_PER_MINUTE == 0:
        return _counted(whole // _SECONDS_PER_MINUTE, "minute")
    return _counted(whole, "second")


def timeout_finish_warning(pages_kept: int, timeout_seconds: float) -> str:
    """
    Return the warning for a document finished because nobody answered.

    A multi-page wait that times out with pages already kept finishes the
    document rather than discarding it, but the operator never said it was
    complete, so the upload carries this warning instead of reading as a
    clean success.  Only a page count and a duration are interpolated.

    Args:
        pages_kept: How many pages the finished document holds.
        timeout_seconds: The wait that ran out.

    Returns:
        The warning sentence.

    """
    return (
        f"Finished after {pages_phrase(pages_kept)} because nobody answered "
        f"within {duration_phrase(timeout_seconds)}; the document may be "
        "missing pages."
    )


def cap_finish_warning(pages_kept: int, cap: int) -> str:
    """
    Return the warning for a document finished because it reached the page cap.

    The cap stops a new pass from starting; it cannot cut one short, so the
    finished document may hold more pages than the cap, and the sentence says
    so honestly rather than claiming the document stopped at the cap.

    Args:
        pages_kept: How many pages the finished document holds.
        cap: The page count at which no new pass starts.

    Returns:
        The warning sentence.

    """
    return (
        f"Finished at {pages_phrase(pages_kept)}: no new scan starts once a "
        f"document has {pages_phrase(cap)}. Scan any remaining pages as a new "
        "document."
    )


# The lead a cap's sentence takes when the job failed after the capped pass:
# there is no finished document to count, but the sheet fed and not kept is
# still where the operator resumes.
_CAP_NOTE_LEAD = "The scan had reached its sheet cap before that: "


def _pass_cap_body(cap: int, sheet_not_kept: int, *, auto_source: bool) -> str:
    """
    Return what a capped pass did and where to resume, after the lead.

    Args:
        cap: The sheet count at which one scan stops.
        sheet_not_kept: The sheet that was fed but not kept.
        auto_source: Whether the capped scan was an Auto source sent through
            the feeder rather than a source named as a feeder.

    Returns:
        The sentence, starting in lower case, to follow a lead.

    """
    stopped = (
        f"stops after {_counted(cap, 'sheet')}, so sheet {sheet_not_kept} was "
        "fed but not kept."
    )
    resume = f"sheet {sheet_not_kept} and any remaining pages as a new document."
    if auto_source:
        return (
            f"a scan from an Auto source through the feeder {stopped} If "
            "the feeder was already empty, the scanner was scanning its glass "
            'again; set auto_source_mode = "flatbed" for this profile. Otherwise '
            f"scan {resume}"
        )
    return f"one scan {stopped} Scan {resume}"


def pass_cap_warning(
    pages_kept: int, cap: int, sheet_not_kept: int, *, auto_source: bool
) -> str:
    """
    Return the warning for a scan that stopped at its per-pass sheet cap.

    A feeder can only tell a full hopper from a runaway by feeding one more
    sheet, so the sheet past the cap was fed and thrown away. The sentence
    names that sheet, so the operator knows where to resume, and shares
    ``cap_finish_warning``'s lead and tail so both caps read as one family.
    An Auto source sent through the feeder has a lower cap because a platen
    rescanned as a feeder never reports the end of its feed; its sentence
    also says how to keep that source on the glass. Only counts are
    interpolated.

    Args:
        pages_kept: How many pages the finished document holds.
        cap: The sheet count at which one scan stops.
        sheet_not_kept: The sheet that was fed but not kept.
        auto_source: Whether the capped scan was an Auto source sent through
            the feeder rather than a source named as a feeder.

    Returns:
        The warning sentence.

    """
    body = _pass_cap_body(cap, sheet_not_kept, auto_source=auto_source)
    return f"Finished at {pages_phrase(pages_kept)}: {body}"


def pass_cap_note(cap: int, sheet_not_kept: int, *, auto_source: bool) -> str:
    """
    Return the cap sentence a job that failed after a capped pass carries.

    The job failed after the pass, so there is no finished document to count,
    but which sheet was fed and not kept is still the one fact the operator
    needs to resume, and without this it would survive only in the log. It
    follows the sentence saying where the pages were kept.

    Args:
        cap: The sheet count at which one scan stops.
        sheet_not_kept: The sheet that was fed but not kept.
        auto_source: Whether the capped scan was an Auto source sent through
            the feeder rather than a source named as a feeder.

    Returns:
        The sentence.

    """
    body = _pass_cap_body(cap, sheet_not_kept, auto_source=auto_source)
    return f"{_CAP_NOTE_LEAD}{body}"


def _backs_cap_body(cap: int, sheet_not_kept: int) -> str:
    """
    Return what a capped backs pass did and what to do, after the lead.

    Args:
        cap: The sheet count at which one scan stops.
        sheet_not_kept: The sheet of the turned-over stack that was fed but
            not kept.

    Returns:
        The sentence, starting in lower case, to follow a lead.

    """
    return (
        f"the scan of the backs stops after {_counted(cap, 'sheet')}, so sheet "
        f"{sheet_not_kept} of the turned-over stack was fed but not kept, and "
        "the stack held more sheets than the scan of the fronts fed. Check "
        "both documents, and scan any sheet missing from either again, both "
        "sides, as a new document."
    )


def backs_pass_cap_warning(pages_kept: int, cap: int, sheet_not_kept: int) -> str:
    """
    Return the warning for a manual duplex job whose backs pass hit its cap.

    The fronts pass ended on its own, so it fed no more sheets than the cap,
    and the backs pass fed one more than that: the turned-over stack held
    sheets whose fronts were never scanned, and which ones cannot be told.
    Resuming "from the next sheet", as ``pass_cap_warning`` advises, would
    recover nothing, so this sentence says to check both documents and scan
    the missing sheets again from both sides. It shares ``pass_cap_warning``'s
    lead, with the count of every page the job uploaded across both
    documents. Only counts are interpolated.

    Args:
        pages_kept: How many pages the two documents hold together.
        cap: The sheet count at which one scan stops.
        sheet_not_kept: The sheet of the turned-over stack that was fed but
            not kept.

    Returns:
        The warning sentence.

    """
    body = _backs_cap_body(cap, sheet_not_kept)
    return f"Finished at {pages_phrase(pages_kept)}: {body}"


def backs_pass_cap_note(cap: int, sheet_not_kept: int) -> str:
    """
    Return the backs-cap sentence a manual duplex job that then failed carries.

    The counterpart of ``pass_cap_note`` for a capped backs pass, whose
    advice differs; see ``backs_pass_cap_warning``.

    Args:
        cap: The sheet count at which one scan stops.
        sheet_not_kept: The sheet of the turned-over stack that was fed but
            not kept.

    Returns:
        The sentence.

    """
    return f"{_CAP_NOTE_LEAD}{_backs_cap_body(cap, sheet_not_kept)}"


def backs_not_scanned_warning(sheet_not_kept: int) -> str:
    """
    Return the warning for a manual duplex job whose fronts pass hit its cap.

    The sheet past the cap was fed and thrown away, so it lies in the output
    tray with the fronts. Pairing the backs by position needs the flipped stack
    to hold exactly the sheets whose fronts were kept; with that extra sheet on
    it, the first back fed belongs to no kept front, and every later back
    would land one page off. The job therefore ends with the fronts, and this
    sentence follows ``pass_cap_warning`` to say why the backs are missing.
    Only a count is interpolated.

    Args:
        sheet_not_kept: The sheet that was fed but not kept.

    Returns:
        The warning sentence.

    """
    return (
        f"The backs were not scanned: sheet {sheet_not_kept} is already in the "
        "output tray, so turning the stack over would pair every back with the "
        "wrong front."
    )


def substituted_source_warning(requested: str) -> str:
    """
    Return the warning for a flatbed request scanned through the feeder instead.

    A scanner that lists no source matching a flatbed request may still offer
    Auto, and a profile whose ``auto_source_mode`` is ``"adf"`` sends Auto
    through the feeder. The scan then did something other than what the
    profile named, so the job finishes with this warning rather than as a
    clean success. The name is shown with ``repr`` so an empty or padded name
    is visible. The caller neutralises the requested name before passing it
    in.

    Args:
        requested: The source the profile asked for.

    Returns:
        The warning sentence.

    """
    return (
        f"The scanner has no source named {requested!r}, so its Auto source was "
        "scanned through the feeder, because this profile's auto_source_mode is "
        '"adf". Set the profile\'s source to one the scanner lists.'
    )


def source_not_offered_error(requested: str, available: Sequence[str]) -> str:
    """
    Return the refusal for a profile source the scanner does not offer.

    Nothing has been scanned when this is raised, so the sentence only has to
    say what was asked for, what the scanner offers instead, and what to
    change. Every name is shown with ``repr`` so an empty or padded name is
    visible. The caller neutralises device-supplied text before passing it in.

    Args:
        requested: The source the profile asked for.
        available: The source names the scanner reports, in its order.

    Returns:
        The error sentence.

    """
    if not available:
        return (
            f"The scanner has no source named {requested!r}, and it lists no "
            "sources to choose from."
        )
    offered = ", ".join(repr(name) for name in available)
    return (
        f"The scanner has no source named {requested!r}. It offers {offered}; "
        "set the profile's source to one of those names."
    )


def sixteen_bit_error(device: str) -> str:
    """
    Return the refusal for a scanner set to 16 bits per sample.

    saneless writes 8-bit pages, and a 16-bit frame cannot be read back
    correctly, so the scan is refused before any page is started. The sentence
    names the device and says what to change. The caller neutralises the
    device name before passing it in.

    Args:
        device: The SANE device name.

    Returns:
        The error sentence.

    """
    return (
        f"The scanner {device} is set to 16 bits per sample, and saneless scans "
        "at 8. Choose an 8-bit mode, such as Gray or Color, in the profile."
    )


def scan_page_description(
    pixels_per_line: int, lines: int, *, colour: bool, dpi: int
) -> str:
    """
    Describe the page a scanner agreed to send, for a timeout message.

    The size is in the device's own pixels, as it reported them once the scan
    was set up, so the operator can see why a page was given the time it
    was. A length of zero or less is SANE's "not known in advance", which a
    feeder may report, and is named as unknown rather than printed.

    Args:
        pixels_per_line: The page's width in pixels.
        lines: The page's height in lines, or zero or less when unknown.
        colour: Whether the page is in colour rather than grey.
        dpi: The resolution the device scans at.

    Returns:
        E.g. ``"a colour page of 9921 x 14031 pixels at 1200 dpi"``.

    """
    kind = "colour" if colour else "grey"
    if lines <= 0:
        return (
            f"a {kind} page {pixels_per_line} pixels wide and of unknown length "
            f"at {dpi} dpi"
        )
    return f"a {kind} page of {pixels_per_line} x {lines} pixels at {dpi} dpi"


def page_timeout_error(
    page_label: str, seconds: float, page: str | None, *, returned: bool
) -> str:
    """
    Return the error for a page that did not arrive within its limit.

    The limit grows with the page, so the sentence names the limit and the
    page it was worked out for: a timeout on a large page then reads as a
    page that needed longer, not only as a dropped link. When the cancel did
    not end the read either, the sentence says saneless is still waiting.

    Args:
        page_label: The page, e.g. ``"Page 3"``.
        seconds: The limit the page was given.
        page: The page the limit was worked out for, from
            ``scan_page_description``, or ``None`` when there is none.
        returned: Whether the read came back after it was cancelled.

    Returns:
        The error sentence.

    """
    message = f"{page_label} timed out after {seconds:.0f}s"
    if page is not None:
        message += f", the limit for {page}"
    if not returned:
        message += (
            "; the scanner did not respond to the cancel, so saneless is "
            "still waiting for that read to return"
        )
    return message


def ambiguous_source_error(requested: str, matches: Sequence[str]) -> str:
    """
    Return the refusal for a profile source that matches several scanner sources.

    A scanner listing two names that differ only in case leaves no way to tell
    which one a differently cased request meant, so saneless refuses rather
    than guess. The caller neutralises device-supplied text before passing it
    in.

    Args:
        requested: The source the profile asked for.
        matches: Every scanner source the request matched.

    Returns:
        The error sentence.

    """
    listed = ", ".join(repr(name) for name in matches)
    return (
        f"The source {requested!r} matches more than one of the scanner's "
        f"sources when case and surrounding spaces are ignored: {listed}. Set "
        "the profile's source to one of those names exactly."
    )


def blank_timeout_finish_warning(pages_kept: int, timeout_seconds: float) -> str:
    """
    Return the warning for a document finished while blank pages went unanswered.

    Nobody said whether to keep the pages that looked blank, so they were left
    out and the document finished with the pages kept before them.

    Args:
        pages_kept: How many pages the finished document holds.
        timeout_seconds: The wait that ran out.

    Returns:
        The warning sentence.

    """
    return (
        f"Finished after {pages_phrase(pages_kept)} because nobody answered "
        f"about the blank pages within {duration_phrase(timeout_seconds)}, so "
        "they were left out; the document may be missing pages."
    )


# The wording of a multi-page scan's questions.  Each sentence is read by both
# the web prompt and the terminal, so the two surfaces say the same thing.

NOTHING_TO_FINISH: Final = (
    "Nothing to finish yet: every page so far was skipped as blank. "
    "Scan another page, or abort."
)
"""
Why Finish is withheld while no page is kept.

The web page shows it beside the disabled Finish button; the terminal prints it
when ``f`` is typed at the same point.  One sentence, so the two agree.
"""

MULTI_PAGE_LABEL: Final = "Multiple pages"
"""The scan form checkbox's label."""

MULTI_PAGE_HELP: Final = (
    "Asks after each scan whether there is another page, and puts every page "
    "in one document."
)
"""The help line under the scan form checkbox while it can be ticked."""

MULTI_PAGE_DISABLED_REASON: Final = "Not available with manual duplex."
"""The line in place of the checkbox's help while a manual-duplex profile is chosen."""

MULTI_PAGE_OPTION_HELP: Final = (
    "Ask after each scan whether there is another page, and put every page in "
    "one document. Needs an interactive terminal."
)
"""The ``--multi-page`` option's help text."""

MULTI_PAGE_NEEDS_TERMINAL: Final = (
    "--multi-page needs an interactive terminal: saneless asks after each scan "
    "whether there is another page. Run it from a terminal, or scan from the "
    "web UI."
)
"""The refusal when ``--multi-page`` is given without a terminal to ask on."""

_FINISH_LABEL: Final = "Finish document"
_ABORT_LABEL: Final = "Abort scan"
_ABORT_UPLOADS_NOTHING: Final = "Abort scan stops without uploading anything."
_ENDS_WITHOUT_UPLOAD: Final = "ends the scan without uploading anything."
_FAILED_SCAN_ALERT: Final = "The last scan failed, so none of its pages were added."
_NO_PAGES_KEPT: Final = "No pages kept yet."
_PUT_NEXT_PAGE: Final = "Put the next page on the scanner, then press Scan next page."


@dataclass(frozen=True, slots=True)
class PassPromptCopy:
    """
    Every sentence a web multi-page prompt shows, in display order.

    This is the one source of the prompt's wording: a template lays these
    strings out and composes none of its own.  A field a question does not
    use is None, so a template can test for it rather than for the question.

    Attributes:
        alert: For a failed pass only: the warning line saying none of its
            pages were added.
        detail: For a failed pass only: what the scanner reported, when
            anything was.
        headline: The bold first line.
        instruction: What to do next; None for the blank-page question, whose
            buttons say it.
        buttons: Each answer with its label, in display order.  The next-page
            question always lists Finish, even when the prompt does not offer
            it, so the web page can show it disabled beside its reason.
        finish_blocked: For the next-page question when Finish is not offered:
            why not.
        notes: The small lines under the buttons, in display order.
        abort_question: The confirmation Abort asks; None where there is no
            Abort button.

    """

    alert: str | None
    detail: str | None
    headline: str
    instruction: str | None
    buttons: tuple[tuple[PassAnswer, str], ...]
    finish_blocked: str | None
    notes: tuple[str, ...]
    abort_question: str | None


@dataclass(frozen=True, slots=True)
class CliChoice:
    """
    One answer a terminal multi-page prompt accepts, with the letter that picks it.

    Attributes:
        letter: The single lower-case letter that picks this answer.
        answer: The answer it picks.
        label: How the prompt lists it, the letter in brackets, e.g.
            ``"[r]e-scan last"``.

    """

    letter: str
    answer: PassAnswer
    label: str


def abort_question(pages_kept: int) -> str:
    """
    Return the confirmation a multi-page scan asks before it aborts.

    This is the one source of that question for both the web page's Abort
    button and the terminal's ``a``.  With nothing kept it is the flip
    prompt's sentence, byte for byte, because nothing is lost but the scan.

    Args:
        pages_kept: How many pages the document holds.

    Returns:
        The question, naming the pages that will not be uploaded.

    """
    if pages_kept < 1:
        return "Abort this scan? It will stop and cannot be resumed."
    return (
        f"Abort this scan? The {pages_phrase(pages_kept)} kept so far will not be "
        "uploaded, and the scan cannot be resumed."
    )


def multi_page_manual_duplex_refusal(profile: str) -> str:
    """
    Return the refusal for ``--multi-page`` with a manual-duplex profile.

    This is the one source of that refusal's wording.

    Args:
        profile: The profile name the operator gave.

    Returns:
        The refusal, naming the profile and what to do instead.

    """
    return (
        f"Profile '{profile}' is manual duplex, and --multi-page is not "
        "available with manual duplex. Scan without --multi-page, or choose "
        "another profile."
    )


def _kept_headline(pages_kept: int) -> str:
    """
    Return the line saying how many pages the document holds so far.

    Args:
        pages_kept: How many pages the document holds.

    Returns:
        E.g. ``"4 pages kept so far."`` or ``"No pages kept yet."``.

    """
    if pages_kept < 1:
        return _NO_PAGES_KEPT
    return f"{pages_phrase(pages_kept)} kept so far."


def _these_pages(pages_kept: int) -> str:
    """
    Return the kept pages as a demonstrative phrase.

    Args:
        pages_kept: How many pages the document holds; at least one.

    Returns:
        ``"this page"`` for one page, else e.g. ``"these 4 pages"``.

    """
    return "this page" if pages_kept == 1 else f"these {pages_kept} pages"


def _finish_timeout_note(prompt: PassPrompt) -> str:
    """
    Return what an unanswered next-page or failed-pass question will do.

    Args:
        prompt: The open question.

    Returns:
        The timeout note: it finishes with the pages kept, or, with none,
        ends the scan.

    """
    within = f"No answer within {duration_phrase(prompt.timeout_seconds)}"
    if prompt.pages_kept < 1:
        return f"{within} {_ENDS_WITHOUT_UPLOAD}"
    return f"{within} finishes the document with {_these_pages(prompt.pages_kept)}."


def _last_pass(prompt: PassPrompt) -> str:
    """
    Return the last accepted pass's pages as the object of a re-scan.

    Args:
        prompt: The open next-page question.

    Returns:
        ``"page"`` for a one-page pass, else e.g. ``"3 pages"``.

    """
    if prompt.last_pass_pages >= 2:
        return pages_phrase(prompt.last_pass_pages)
    return "page"


def _next_pass_copy(prompt: PassPrompt) -> PassPromptCopy:
    """
    Return the wording of the question whether there is another page.

    Args:
        prompt: The open next-page question.

    Returns:
        Its copy.

    """
    kept = prompt.pages_kept
    rescan = f"Re-scan throws away the last {_last_pass(prompt)} and scans again."
    if kept >= 1:
        instruction = (
            f"{_PUT_NEXT_PAGE} When there are no more pages, press {_FINISH_LABEL}."
        )
        consequence = (
            f"{_FINISH_LABEL} uploads {_these_pages(kept)} as one document. "
            f"{rescan} {_ABORT_UPLOADS_NOTHING}"
        )
    else:
        instruction = _PUT_NEXT_PAGE
        consequence = f"{rescan} {_ABORT_UPLOADS_NOTHING}"
    finish_offered = PassAnswer.FINISH in prompt.offered
    return PassPromptCopy(
        alert=None,
        detail=None,
        headline=_kept_headline(kept),
        instruction=instruction,
        buttons=(
            (PassAnswer.NEXT, "Scan next page"),
            (PassAnswer.FINISH, _FINISH_LABEL),
            (PassAnswer.RESCAN, f"Re-scan last {_last_pass(prompt)}"),
            (PassAnswer.ABORT, _ABORT_LABEL),
        ),
        finish_blocked=None if finish_offered else NOTHING_TO_FINISH,
        notes=(consequence, _finish_timeout_note(prompt)),
        abort_question=abort_question(kept),
    )


def _blank_list(prompt: PassPrompt) -> str:
    """
    Return the positions of a pass's blank pages as a headline subject.

    Positions are comma-separated, the style of the removed-as-blank note.

    Args:
        prompt: The open blank-page question.

    Returns:
        E.g. ``"Pages 2, 4, 6 of the 6 just scanned look blank."``, or
        ``"Page 3 of the 4 just scanned looks blank."`` for one blank page.

    """
    listed = ", ".join(str(position) for position in prompt.blank_positions)
    if len(prompt.blank_positions) == 1:
        return f"Page {listed} of the {prompt.pass_pages} just scanned looks blank."
    return f"Pages {listed} of the {prompt.pass_pages} just scanned look blank."


def _blank_copy(prompt: PassPrompt) -> PassPromptCopy:
    """
    Return the wording of the question what to do about blank-looking pages.

    There is no Abort here: it is one Skip away, at the next-page question.

    Args:
        prompt: The open blank-page question.

    Returns:
        Its copy.

    """
    blanks = len(prompt.blank_positions)
    survivors = prompt.pages_kept + prompt.pass_pages - blanks
    within = f"No answer within {duration_phrase(prompt.timeout_seconds)}"
    if prompt.pass_pages <= 1:
        headline = "This page looks blank."
        skip, keep, rescan = "Skip page", "Keep page", "Re-scan page"
        consequence = (
            "Skip page leaves it out of the document. Keep page adds it anyway. "
            "Re-scan page scans it again."
        )
        skipped = "skips it"
    else:
        headline = _blank_list(prompt)
        if blanks == 1:
            noun, left_out, them = "page", "that page", "it"
        else:
            noun, left_out, them = "pages", f"those {blanks} pages", "them"
        skip, keep = f"Skip blank {noun}", f"Keep blank {noun}"
        rescan = f"Re-scan all {prompt.pass_pages}"
        consequence = (
            f"Skip leaves {left_out} out of the document. Keep adds {them} "
            f"anyway. Re-scan throws away all {prompt.pass_pages} and scans "
            "them again."
        )
        skipped = "skips the blank pages"
    if survivors >= 1:
        timeout = f"{within} {skipped} and finishes the document."
    else:
        timeout = f"{within} {_ENDS_WITHOUT_UPLOAD}"
    if prompt.pages_kept < 1:
        kept_note = _NO_PAGES_KEPT
    else:
        kept_note = f"{pages_phrase(prompt.pages_kept)} already kept."
    return PassPromptCopy(
        alert=None,
        detail=None,
        headline=headline,
        instruction=None,
        buttons=(
            (PassAnswer.SKIP_BLANKS, skip),
            (PassAnswer.KEEP_BLANKS, keep),
            (PassAnswer.RESCAN, rescan),
        ),
        finish_blocked=None,
        notes=(consequence, kept_note, timeout),
        abort_question=None,
    )


def _retry_copy(prompt: PassPrompt, error: str | None) -> PassPromptCopy:
    """
    Return the wording of the question what to do after a pass failed.

    There is no Re-scan here: the failed pass added nothing to throw away.

    Args:
        prompt: The open failed-pass question.
        error: What the scanner reported, as it should be shown.

    Returns:
        Its copy.

    """
    kept = prompt.pages_kept
    return PassPromptCopy(
        alert=_FAILED_SCAN_ALERT,
        detail=None if error is None else f"The scanner reported: {error}",
        headline=_kept_headline(kept),
        instruction=(
            "Clear the scanner, put back every page from the failed scan, then "
            "press Scan again."
        ),
        buttons=(
            (PassAnswer.NEXT, "Scan again"),
            (PassAnswer.FINISH, _FINISH_LABEL),
            (PassAnswer.ABORT, _ABORT_LABEL),
        ),
        finish_blocked=None,
        notes=(
            f"{_FINISH_LABEL} uploads the {pages_phrase(kept)} kept. "
            f"{_ABORT_UPLOADS_NOTHING}",
            _finish_timeout_note(prompt),
        ),
        abort_question=abort_question(kept),
    )


def pass_prompt_copy(prompt: PassPrompt, *, error: str | None = None) -> PassPromptCopy:
    """
    Return every sentence the web page shows for an open multi-page question.

    This is the one source of the prompt's wording; the terminal's prompts
    (``cli_pass_question``) are built from the same sentences, so the two
    surfaces say the same thing.

    Args:
        prompt: The open question.
        error: For a failed pass, the error text to show in place of
            ``prompt.error``.  ``prompt.error`` is unscrubbed, so the web
            passes the scrubbed text here.

    Returns:
        The prompt's copy.

    Raises:
        AssertionError: If the prompt's wait is not a PassWait member.

    """
    match prompt.wait:
        case PassWait.NEXT_PASS:
            copy = _next_pass_copy(prompt)
        case PassWait.BLANK_DECISION:
            copy = _blank_copy(prompt)
        case PassWait.RETRY:
            copy = _retry_copy(prompt, prompt.error if error is None else error)
        case _:
            assert_never(prompt.wait)
    return copy


def _cli_rescan_label(prompt: PassPrompt) -> str:
    """
    Return how a terminal prompt lists the re-scan answer.

    Args:
        prompt: The open question.

    Returns:
        ``"[r]e-scan last"`` between passes, ``"[r]e-scan"`` for a one-page
        blank pass, else e.g. ``"[r]e-scan all 6"``.

    """
    if prompt.wait is PassWait.NEXT_PASS:
        return "[r]e-scan last"
    if prompt.pass_pages <= 1:
        return "[r]e-scan"
    return f"[r]e-scan all {prompt.pass_pages}"


def cli_pass_choices(prompt: PassPrompt) -> tuple[CliChoice, ...]:
    """
    Return the answers a terminal multi-page prompt accepts, in listing order.

    This is the one source of the terminal's letters and how it lists them.
    Only the answers the prompt offers are returned, so a letter for one it
    does not offer is not listed and is not accepted.

    Args:
        prompt: The open question.

    Returns:
        The offered choices: next, re-scan, finish, abort between passes;
        skip, keep, re-scan for blank pages; scan again, finish, abort after
        a failed pass.

    Raises:
        AssertionError: If the prompt's wait is not a PassWait member.

    """
    rescan = CliChoice("r", PassAnswer.RESCAN, _cli_rescan_label(prompt))
    finish = CliChoice("f", PassAnswer.FINISH, "[f]inish")
    abort = CliChoice("a", PassAnswer.ABORT, "[a]bort")
    match prompt.wait:
        case PassWait.NEXT_PASS:
            order = (CliChoice("n", PassAnswer.NEXT, "[n]ext"), rescan, finish, abort)
        case PassWait.BLANK_DECISION:
            order = (
                CliChoice("s", PassAnswer.SKIP_BLANKS, "[s]kip"),
                CliChoice("k", PassAnswer.KEEP_BLANKS, "[k]eep"),
                rescan,
            )
        case PassWait.RETRY:
            order = (
                CliChoice("n", PassAnswer.NEXT, "[n] scan again"),
                finish,
                abort,
            )
        case _:
            assert_never(prompt.wait)
    return tuple(choice for choice in order if choice.answer in prompt.offered)


def _cli_choice_list(prompt: PassPrompt) -> str:
    """
    Return the offered choices as a comma-separated list.

    Args:
        prompt: The open question.

    Returns:
        E.g. ``"[n]ext, [r]e-scan last, [f]inish, [a]bort"``.

    """
    return ", ".join(choice.label for choice in cli_pass_choices(prompt))


def cli_pass_question(prompt: PassPrompt) -> str:
    """
    Return the full text of a terminal multi-page prompt.

    This is the one source of the terminal's question; it reuses the web
    prompt's headlines, so the two say the same thing.  The failed-pass
    question is two lines: what failed, then what to do.  The error text is
    placed as given, so this module stays free of terminal concerns; the CLI
    escapes its control characters before asking, as it does on every other
    failure line, because that text comes from outside saneless.

    Args:
        prompt: The open question.

    Returns:
        The prompt text, ending with the offered choices.

    """
    choices = _cli_choice_list(prompt)
    if prompt.wait is PassWait.RETRY:
        failed = _FAILED_SCAN_ALERT
        if prompt.error is not None:
            failed = f"{failed.removesuffix('.')}: {prompt.error}"
        return (
            f"{failed}\n{_kept_headline(prompt.pages_kept)} Put back every page "
            f"from the failed scan. {choices}"
        )
    return f"{pass_prompt_copy(prompt).headline} {choices}"


def cli_choice_hint(prompt: PassPrompt) -> str:
    """
    Return what a terminal multi-page prompt says after an unrecognised letter.

    This is the one source of that hint.  It lists only the offered letters.

    Args:
        prompt: The open question.

    Returns:
        E.g. ``"Choose one of: [n]ext, [r]e-scan last, [f]inish, [a]bort"``.

    """
    return f"Choose one of: {_cli_choice_list(prompt)}"


def job_state_for(outcome: ScanOutcome) -> JobState:
    """
    Return the terminal job state a resolved scan outcome implies.

    A ``ScanOutcome`` only exists once the pipeline has finished, so every arm
    lands in ``TERMINAL_STATES``.  ``FALLBACK`` gets a state of its own rather
    than being folded into ``DONE``: the document reached the consume directory
    but its title, tags and correspondent were not applied, and calling that
    "Complete" is the silent success this vocabulary exists to remove.

    This is a ``match`` with ``assert_never`` and not a
    ``dict[ScanOutcome, JobState]`` on purpose.  A dict missing a member draws
    no diagnostic from either ``ty`` or ``pyrefly``; the same enum in a match
    is caught by both, at edit time, before a third outcome can fall silently
    through an ``else``.

    Args:
        outcome: The outcome the pipeline resolved to.

    Returns:
        The terminal JobState to persist for that outcome.

    Raises:
        AssertionError: If the value is not a ScanOutcome member.

    """
    match outcome:
        case ScanOutcome.SUCCESS:
            state = JobState.DONE
        case ScanOutcome.FALLBACK:
            state = JobState.FALLBACK
        case _:
            assert_never(outcome)
    return state


type _UnconfirmedCategory = Literal[
    ErrorCategory.UNCONFIRMED_SEND,
    ErrorCategory.UNCONFIRMED_FILING,
]


def _unconfirmed_advice(category: _UnconfirmedCategory) -> ErrorAdvice:
    """
    Return the advice for an upload that may already be in paperless-ngx.

    The two messages differ, because "received" is the stronger statement,
    but the next step is one: it never says to start the scan again, since a
    blind rescan could store the document twice.

    Args:
        category: One of the two unconfirmed categories.

    Returns:
        The message for the category and the shared next step, paired.

    Raises:
        AssertionError: If the value is not one of the two.

    """
    match category:
        case ErrorCategory.UNCONFIRMED_SEND:
            message = (
                "The upload may have reached paperless-ngx, but no answer came back."
            )
        case ErrorCategory.UNCONFIRMED_FILING:
            message = (
                "paperless-ngx received the document but did not confirm filing it."
            )
        case _:
            assert_never(category)
    return ErrorAdvice(
        message=message,
        next_step=(
            "Check paperless-ngx's document list before scanning again. A copy "
            "is kept in failed/; import it only if the document is not in "
            "paperless-ngx."
        ),
    )


def error_advice(category: ErrorCategory) -> ErrorAdvice:
    """
    Return what to tell a reader about an error category, and what to do next.

    This is the one ``match`` over ``ErrorCategory`` in this module.
    ``error_message`` and ``error_next_step`` are one-line accessors over it
    rather than lookups of their own, so a category can never end up with a
    message and no next step, or with two lookups that drift apart.

    The wording is surface-neutral.  Both the web page and the CLI render the
    same string, so a next step never says "press Scan" (the CLI has no
    button) and never says "run the command" (the page has no command line);
    it says "start the scan again", which is true on both.

    Every string is a developer-authored constant.  None of them interpolates
    exception text, request input, a URL, a token or a filesystem path, so
    nothing internal can reach a screen through this path (ASVS V7).  None
    carries a number either, except the paperless-ngx release and API
    versions saneless supports, which are facts about saneless rather than
    about the job.

    The two unconfirmed categories share a next step that never says to
    start the scan again: the document may already be in paperless-ngx, so a
    blind rescan could store it twice.  ``_unconfirmed_advice`` words the
    pair, as ``_pass_wait_state_label`` does for the multi-page waits.

    Args:
        category: The error category to describe.

    Returns:
        The plain-language message and the next step, paired.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    match category:
        case ErrorCategory.FEEDER:
            advice = ErrorAdvice(
                message="The document feeder is empty or jammed.",
                next_step=(
                    "Load the pages squarely in the feeder, clear any jam, "
                    "then start the scan again."
                ),
            )
        case ErrorCategory.CONFIG:
            advice = ErrorAdvice(
                message="The saneless configuration is invalid.",
                next_step=(
                    "Correct the saneless configuration file, then restart saneless."
                ),
            )
        case ErrorCategory.SCANNER:
            advice = ErrorAdvice(
                message="The scanner could not complete the scan.",
                next_step=(
                    "Check the scanner is switched on and connected, then "
                    "start the scan again."
                ),
            )
        case ErrorCategory.UPLOAD:
            advice = ErrorAdvice(
                message="The document could not be sent to paperless-ngx.",
                next_step=(
                    "Check paperless-ngx is running and the API token is "
                    "correct, then start the scan again."
                ),
            )
        case ErrorCategory.UNKNOWN:
            advice = ErrorAdvice(
                message="Something went wrong.",
                next_step=(
                    "Start the scan again. If it keeps failing, check the saneless log."
                ),
            )
        case ErrorCategory.ASSEMBLY:
            advice = ErrorAdvice(
                message="The scanned pages could not be assembled into a PDF.",
                next_step=(
                    "Start the scan again. If it keeps failing, check the "
                    "server's free disk space."
                ),
            )
        case ErrorCategory.ALL_BLANK:
            advice = ErrorAdvice(
                # Path-free and hedged: the error beside it says what was
                # really kept, which is one PDF unless building it failed.
                message=(
                    "Every page looked blank, so nothing was uploaded; the "
                    "pages are normally kept as one PDF, and the error says "
                    "what was kept."
                ),
                next_step=(
                    "If the pages are not blank, lower "
                    "empty_page_coverage_threshold for this profile or turn "
                    "empty-page detection off, then scan again."
                ),
            )
        case ErrorCategory.UNCONFIRMED_SEND | ErrorCategory.UNCONFIRMED_FILING:
            advice = _unconfirmed_advice(category)
        case ErrorCategory.PAPERLESS_VERSION:
            advice = ErrorAdvice(
                message=(
                    "This paperless-ngx does not speak an API version saneless "
                    "supports (9 or 10)."
                ),
                next_step=(
                    "saneless needs paperless-ngx 2.16 or later: upgrade "
                    "paperless-ngx, then start the scan again."
                ),
            )
        case ErrorCategory.DISK_SPACE:
            advice = ErrorAdvice(
                # Path-free and number-free: the error beside it names the
                # folder and how much space is needed.
                message="The server ran out of disk space for this scan.",
                next_step=(
                    "Free space on the server (the error names the folder and "
                    "how much is needed), then start the scan again."
                ),
            )
        case ErrorCategory.REJECTED:
            advice = ErrorAdvice(
                # Neutral on purpose: REJECTED also covers down and degraded
                # refusals, where no scan is running to wait for.
                message=(
                    "This scan was not started. Check that saneless is ready "
                    "to scan, then try again."
                ),
                next_step=(
                    "Check the system status list for anything marked Failed, "
                    "then start the scan again."
                ),
            )
        case _:
            assert_never(category)
    return advice


def error_message(category: ErrorCategory) -> str:
    """
    Return the plain-language user message for an error category.

    The message is half of an ``ErrorAdvice`` rather than a lookup of its own.
    There is exactly one ``match`` over ``ErrorCategory`` in this module, in
    ``error_advice``; this accessor reads it so the message and the next step
    cannot drift apart.

    Args:
        category: The error category to describe.

    Returns:
        A short sentence a non-technical reader can act on.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    return error_advice(category).message


def error_next_step(category: ErrorCategory) -> str:
    """
    Return the action a reader should take after an error category.

    The companion of ``error_message`` and, like it, a one-line accessor over
    ``error_advice``.

    Args:
        category: The error category to advise on.

    Returns:
        One imperative sentence -- two for ASSEMBLY and UNKNOWN -- that names
        what to do next without naming the web page or the command line.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    return error_advice(category).next_step


def connection_status_message(status: ConnectionStatus) -> str:
    """
    Return the plain-language user message for a connection-test outcome.

    Every message is a developer-authored constant.  No status code, no
    response body, no URL and no token is interpolated, so a paperless-ngx
    error page cannot reach the UI through this path.

    Args:
        status: The connection-test outcome to describe.

    Returns:
        A short sentence a non-technical reader can act on.

    Raises:
        AssertionError: If the value is not a ConnectionStatus member.

    """
    match status:
        case ConnectionStatus.CONNECTED:
            message = "Connected to paperless-ngx."
        case ConnectionStatus.TOKEN_REJECTED:
            message = "Paperless-ngx rejected the API token."
        case ConnectionStatus.NOT_FOUND:
            message = "The paperless-ngx API was not found at that URL."
        case ConnectionStatus.SERVER_ERROR:
            message = "Paperless-ngx returned a server error."
        case ConnectionStatus.UNREACHABLE:
            message = "Could not reach paperless-ngx."
        case ConnectionStatus.INCOMPATIBLE:
            message = (
                "This paperless-ngx does not speak an API version saneless supports."
            )
        case _:
            assert_never(status)
    return message


def worker_health_detail(health: WorkerHealth) -> str:
    """
    Return the ``/health`` detail string for a worker health state.

    These strings are a wire contract: the ``/health`` response body and
    ``docs/reference/web-api.md`` pin them, so they must not be reworded.

    Args:
        health: The worker health state to describe.

    Returns:
        The short detail string, e.g. ``"job store failing"``.

    Raises:
        AssertionError: If the value is not a WorkerHealth member.

    """
    match health:
        case WorkerHealth.HEALTHY:
            detail = "ok"
        case WorkerHealth.DEGRADED:
            detail = "job store failing"
        case WorkerHealth.DOWN:
            detail = "worker thread is down"
        case _:
            assert_never(health)
    return detail


def _reload_page_message(
    rejection: Literal[
        RequestRejection.INVALID_REQUEST,
        RequestRejection.NOT_FOUND,
        RequestRejection.METHOD_NOT_ALLOWED,
        RequestRejection.CLIENT_ERROR,
    ],
) -> str:
    """
    Return the message for a rejection whose remedy is "reload the page".

    These four are one group, not four unrelated arms: each names a different
    thing the browser got wrong and all four end in the same sentence, because
    reloading is the only thing a reader can usefully do about any of them.
    Grouping them keeps ``rejection_message`` readable as the twelfth member
    joins it.

    The parameter is typed as the four members this arm can pass, so the
    ``assert_never`` below still fails the type gate if the group ever grows,
    and ``rejection_message``'s own ``assert_never`` still fails it if
    ``RequestRejection`` grows.

    Args:
        rejection: One of the four reload-remedy rejections.

    Returns:
        The approved sentence for that rejection.

    Raises:
        AssertionError: If the value is outside the four-member group.

    """
    match rejection:
        case RequestRejection.INVALID_REQUEST:
            message = "The request was not valid. Reload the page, then try again."
        case RequestRejection.NOT_FOUND:
            message = (
                "That page or action does not exist. Reload the page, then try again."
            )
        case RequestRejection.METHOD_NOT_ALLOWED:
            message = "That action is not allowed. Reload the page, then try again."
        case RequestRejection.CLIENT_ERROR:
            message = (
                "The request could not be completed. Reload the page, then try again."
            )
        case _:
            assert_never(rejection)
    return message


def _config_file_message(
    rejection: Literal[
        RequestRejection.TOKEN_UNSET,
        RequestRejection.URL_UNSET,
        RequestRejection.HOST_NOT_ALLOWED,
    ],
) -> str:
    """
    Return the message for a rejection whose remedy is an edit to the config file.

    These three are one group: each names the one setting to change and ends
    in the same instruction, because saneless reads its config file only at
    start-up.  Each names the setting and never a value -- not the token, not
    the paperless-ngx URL (which says where paperless-ngx runs), and not the
    refused Host, which is shown beside the sentence, neutralised and bounded
    (ASVS V7).

    The parameter is typed as the three members this arm can pass, so the
    ``assert_never`` below still fails the type gate if the group ever grows.

    Args:
        rejection: One of the three config-file rejections.

    Returns:
        The approved sentence for that rejection.

    Raises:
        AssertionError: If the value is outside the three-member group.

    """
    match rejection:
        case RequestRejection.TOKEN_UNSET:
            message = (
                "The paperless-ngx API token has not been set, so the scan was "
                "not started. Put a real API token in the saneless config "
                "file, then restart saneless."
            )
        case RequestRejection.URL_UNSET:
            message = (
                "The paperless-ngx address has not been set, so the scan was "
                "not started. Set paperless.url in the saneless config file, "
                "then restart saneless."
            )
        case RequestRejection.HOST_NOT_ALLOWED:
            message = (
                "saneless does not answer to this address. Add the host name "
                "you used to [web] allowed_hosts in the saneless config file, "
                "then restart saneless."
            )
        case _:
            assert_never(rejection)
    return message


def rejection_message(rejection: RequestRejection) -> str:
    """
    Return the user-facing message for a web-layer rejection.

    This is the approved copy, and the only place it lives.
    Every message is a developer-authored constant that ends with a period.
    The TITLE_TOO_LONG message is built from ``TITLE_MAX_LENGTH`` so the number
    it names cannot drift from the cap the form enforces.

    Args:
        rejection: The rejection to describe.

    Returns:
        A sentence that says what happened and what to do next.

    Raises:
        AssertionError: If the value is not a RequestRejection member.

    """
    match rejection:
        case RequestRejection.QUEUE_FULL:
            message = (
                "The scan queue is full. Wait for a scan to finish, then try again."
            )
        case RequestRejection.WORKER_DOWN:
            message = (
                "The scan service is not running, so the scan was not started. "
                "Restart saneless, then try again."
            )
        case RequestRejection.WORKER_DEGRADED:
            message = (
                "Job history cannot be saved right now, so the scan was not "
                "started. Check the server's free disk space and log, then try "
                "again."
            )
        case (
            RequestRejection.TOKEN_UNSET
            | RequestRejection.URL_UNSET
            | RequestRejection.HOST_NOT_ALLOWED
        ):
            message = _config_file_message(rejection)
        case RequestRejection.UNKNOWN_PROFILE:
            message = (
                "That scan profile does not exist. Reload the page to see the "
                "current profiles."
            )
        case RequestRejection.MULTI_PAGE_MANUAL_DUPLEX:
            message = (
                "Multiple pages is not available with manual duplex, so the scan "
                "was not started. Untick Multiple pages or choose another "
                "profile, then try again."
            )
        case RequestRejection.TITLE_TOO_LONG:
            message = (
                "The title is too long. Shorten it to "
                f"{TITLE_MAX_LENGTH} characters or fewer."
            )
        case RequestRejection.TITLE_HAS_CONTROL:
            message = (
                "The title contains a tab or another control character. Remove "
                "it, then try again."
            )
        case (
            RequestRejection.INVALID_REQUEST
            | RequestRejection.NOT_FOUND
            | RequestRejection.METHOD_NOT_ALLOWED
            | RequestRejection.CLIENT_ERROR
        ):
            message = _reload_page_message(rejection)
        case RequestRejection.CROSS_SITE:
            message = (
                "This request was blocked because it did not come from the "
                "saneless page. If saneless is behind a reverse proxy, make sure "
                "the proxy passes the original Host header."
            )
        case RequestRejection.INTERNAL:
            message = (
                "Something went wrong on the server. Check the server log for "
                "details, then try again."
            )
        case _:
            assert_never(rejection)
    return message


def rejection_status_code(rejection: RequestRejection) -> int:
    """
    Return the HTTP status code a web-layer rejection is sent with.

    Args:
        rejection: The rejection to map.

    Returns:
        The HTTP status code, e.g. ``429`` for a full queue.

    Raises:
        AssertionError: If the value is not a RequestRejection member.

    """
    match rejection:
        case RequestRejection.QUEUE_FULL:
            status_code = 429
        case (
            RequestRejection.WORKER_DOWN
            | RequestRejection.WORKER_DEGRADED
            | RequestRejection.TOKEN_UNSET
            | RequestRejection.URL_UNSET
        ):
            status_code = 503
        case (
            RequestRejection.UNKNOWN_PROFILE
            | RequestRejection.MULTI_PAGE_MANUAL_DUPLEX
            | RequestRejection.TITLE_TOO_LONG
            | RequestRejection.TITLE_HAS_CONTROL
            | RequestRejection.INVALID_REQUEST
        ):
            status_code = 422
        case RequestRejection.CROSS_SITE:
            status_code = 403
        case RequestRejection.NOT_FOUND:
            status_code = 404
        case RequestRejection.METHOD_NOT_ALLOWED:
            status_code = 405
        case RequestRejection.INTERNAL:
            status_code = 500
        case RequestRejection.CLIENT_ERROR:
            status_code = 400
        case RequestRejection.HOST_NOT_ALLOWED:
            status_code = 421
        case _:
            assert_never(rejection)
    return status_code


def exit_code_for(category: ErrorCategory) -> ExitCode:
    """
    Return the CLI exit code for an error category.

    Three outcomes are resolved by exception type before a caller classifies at
    all, and so never reach this function:

    * A cancel is not a category.  ``ScanCancelledError`` and
      ``KeyboardInterrupt`` map to ``ExitCode.CANCELLED``.
    * Nor is an interruption.  ``ScanInterrupted`` maps through
      ``exit_code_for_signal`` to 128 plus the signal number.
    * ``StorageError`` classifies as ``UNKNOWN``, but it is a setup problem, so
      the CLI guard maps it to ``ExitCode.CONFIG`` (exit 2) by type.  The
      mapping is by type rather than by making ``classify_error`` return
      ``CONFIG`` because ``ErrorCategory`` is persisted on job records, and a
      store that cannot open is not a job's configuration failure.  An
      ``ErrorCategory.STORAGE`` member was rejected for the same reason: it
      would be a new persisted value no job could ever carry.

    ``UNKNOWN`` therefore reaches ``UNEXPECTED`` only for exceptions that are
    not saneless types -- and for a bare ``SanelessError``, which is itself a
    bug.

    A scan that delivered its document is not an error at all, even when it
    was degraded on the way; ``exit_code_for_outcome`` gives those their codes.

    ``PAPERLESS_VERSION`` shares ``PAPERLESS`` with ``UPLOAD``: a refused API
    version stores nothing, so a script that retries on 3 duplicates
    nothing.  The two unconfirmed categories share ``UNCONFIRMED`` (9), which
    a script must never retry on.

    Args:
        category: The error category to map.

    Returns:
        The exit code, e.g. ``ExitCode.PAPERLESS`` for an upload failure.

    Raises:
        AssertionError: If the value is not an ErrorCategory member.

    """
    match category:
        case ErrorCategory.FEEDER | ErrorCategory.SCANNER:
            exit_code = ExitCode.SCAN
        case ErrorCategory.CONFIG:
            exit_code = ExitCode.CONFIG
        case ErrorCategory.UPLOAD | ErrorCategory.PAPERLESS_VERSION:
            exit_code = ExitCode.PAPERLESS
        case ErrorCategory.UNCONFIRMED_SEND | ErrorCategory.UNCONFIRMED_FILING:
            exit_code = ExitCode.UNCONFIRMED
        case ErrorCategory.ASSEMBLY:
            exit_code = ExitCode.PDF
        case ErrorCategory.ALL_BLANK:
            exit_code = ExitCode.ALL_BLANK
        case ErrorCategory.DISK_SPACE:
            exit_code = ExitCode.DISK_SPACE
        case ErrorCategory.UNKNOWN | ErrorCategory.REJECTED:
            exit_code = ExitCode.UNEXPECTED
        case _:
            assert_never(category)
    return exit_code


def exit_code_for_outcome(outcome: ScanOutcome, warning: str | None) -> ExitCode:
    """
    Return the CLI exit code for a scan that delivered its document.

    A clean upload exits ``SUCCESS``.  An upload that carries a warning exits
    ``UPLOADED_WITH_WARNING``, and a document saved to the consume folder exits
    ``SAVED_TO_FOLDER`` whether or not it also carries a warning: the lost
    title, tags and correspondent are the larger problem, and one process can
    report only one code.

    This is a ``match`` with ``assert_never`` for the same reason
    ``job_state_for`` is: a third ``ScanOutcome`` member then fails both type
    checkers here until it is given a code.

    Args:
        outcome: The outcome the pipeline resolved to.
        warning: The run's warning, if any.  An empty string is no warning.

    Returns:
        The exit code, e.g. ``ExitCode.UPLOADED_WITH_WARNING``.

    Raises:
        AssertionError: If the value is not a ScanOutcome member.

    """
    match outcome:
        case ScanOutcome.SUCCESS:
            exit_code = ExitCode.UPLOADED_WITH_WARNING if warning else ExitCode.SUCCESS
        case ScanOutcome.FALLBACK:
            exit_code = ExitCode.SAVED_TO_FOLDER
        case _:
            assert_never(outcome)
    return exit_code


def exit_code_for_signal(signum: int | None) -> ExitCode:
    """
    Return the CLI exit code for a command a signal interrupted.

    SIGHUP and SIGTERM exit 128 plus the signal number, the shell's
    convention: 129 and 143.  They are the only signals a one-shot command
    installs a handler for.  Ctrl-C's SIGINT never reaches here: it is a
    ``KeyboardInterrupt``, a cancel, and exits ``CANCELLED``.

    Anything else -- ``None``, which is a server stop the CLI never sees, or a
    signal saneless installs no handler for -- cannot arrive here
    legitimately, so it is reported as the bug it would be.

    Args:
        signum: The signal that interrupted the command, or ``None``.

    Returns:
        ``ExitCode.HANGUP`` for SIGHUP, ``ExitCode.TERMINATED`` for SIGTERM,
        otherwise ``ExitCode.UNEXPECTED``.

    """
    if signum == signal.SIGHUP:
        return ExitCode.HANGUP
    if signum == signal.SIGTERM:
        return ExitCode.TERMINATED
    return ExitCode.UNEXPECTED


def classify_error(exc: Exception) -> ErrorCategory:
    """
    Map an exception to its error category.

    The checks are ordered, not matched: ``FeederEmptyError`` subclasses
    ``ScanError``, so the narrower class has to be tested first.  For the same
    reason the three narrower paperless classes are tested before
    ``PaperlessError``: an upload that may have arrived, an accepted upload
    that was never confirmed filed (a poll timeout among them, since
    ``PaperlessTimeoutError`` subclasses ``PaperlessUnconfirmedError``), and a
    refused API version each need advice of their own, and the
    ``PaperlessError`` arm would otherwise file all three as ``UPLOAD``.
    ``DiskSpaceError`` is a sibling of every other class here, so its place
    in the order does not matter.  This is an
    ``isinstance`` chain rather than a ``match`` because it dispatches on
    exception type instead of on an enum, so ``assert_never`` does not apply
    and the trailing ``UNKNOWN`` is the correct total fallback.

    ``ScanCancelledError`` is deliberately left ``UNKNOWN``: a cancel is not a
    failure category, and callers test for it before they classify.
    ``StorageError`` is also ``UNKNOWN``; its exit code is assigned by type, as
    ``exit_code_for`` explains.

    Args:
        exc: The caught exception.

    Returns:
        The appropriate ErrorCategory value.

    """
    category = ErrorCategory.UNKNOWN
    if isinstance(exc, FeederEmptyError):
        category = ErrorCategory.FEEDER
    elif isinstance(exc, ConfigError):
        category = ErrorCategory.CONFIG
    elif isinstance(exc, DiskSpaceError):
        category = ErrorCategory.DISK_SPACE
    elif isinstance(exc, ScanError):
        category = ErrorCategory.SCANNER
    elif isinstance(exc, PaperlessUncertainSendError):
        category = ErrorCategory.UNCONFIRMED_SEND
    elif isinstance(exc, PaperlessUnconfirmedError):
        category = ErrorCategory.UNCONFIRMED_FILING
    elif isinstance(exc, PaperlessIncompatibleError):
        category = ErrorCategory.PAPERLESS_VERSION
    elif isinstance(exc, PaperlessError):
        category = ErrorCategory.UPLOAD
    elif isinstance(exc, PdfError):
        category = ErrorCategory.ASSEMBLY
    elif isinstance(exc, AllPagesBlankError):
        category = ErrorCategory.ALL_BLANK
    return category
