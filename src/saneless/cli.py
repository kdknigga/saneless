"""
The saneless command-line interface: one click group and its six commands.

``scan`` runs the scan-to-upload pipeline, ``devices`` lists the scanners
SANE can see, ``jobs`` prints the job history, ``serve`` starts the web
server, ``auto-profiles`` writes scan profiles from a scanner's capabilities,
and ``doctor`` runs the readiness checks. Every command runs inside one guard
that turns a failure into a single stderr line and a documented exit code.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import sys
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, assert_never
from uuid import uuid4

import click
import uvicorn

from .auto_profiles import (
    generate_profiles,
    write_profiles_to_config,
)
from .checks import (
    CheckContext,
    CheckKey,
    CheckResult,
    CheckState,
    check_name,
    run_checks,
    worst_state,
)
from .config import (
    Settings,
    is_placeholder_token,
    load_settings,
    log_config_sources,
    profile_storage_for_loaded,
    resolve_job_title,
    validate_settings_dirs,
    warn_on_legacy_duplex_sources,
)
from .exceptions import (
    ConfigError,
    PaperlessError,
    SanelessError,
    ScanCancelledError,
    ScanError,
    StorageError,
    describe,
)
from .job import CLI_JOBS_DEFAULT_LIMIT, JobStore
from .logging_config import configure_logging
from .paperless import PaperlessClient
from .pipeline import (
    FlipAnswerSlot,
    FlipCoordinator,
    PipelineEvent,
    PipelineRequest,
    run_pipeline,
)
from .scanner.sane_backend import SaneBackend, require_sane
from .vocabulary import (
    ErrorCategory,
    ExitCode,
    FlipOutcome,
    JobState,
    classify_error,
    error_next_step,
    exit_code_for,
    local_time,
    progress_label,
    state_label,
)
from .web.app import create_app

if TYPE_CHECKING:
    from .auto_profiles import ProfileWriteResult
    from .config import ProfileConfig
    from .scanner.base import DeviceCapabilities, DeviceInfo, ScannerBackend

__all__ = ["ClickFlipCoordinator", "cli"]

logger = logging.getLogger(__name__)

# How many not-yet-accepted connections the web server's socket queues. This
# is the backlog uvicorn itself listens with.
_LISTEN_BACKLOG: Final = 2048

# Width of the Status column in `saneless jobs`, derived rather than written
# down: the humanised labels are longer than the raw enum values they replaced,
# and a ninth JobState member must not be able to overflow an 80-column
# terminal without anyone noticing.
_STATUS_COL_WIDTH = max(len(state_label(state)) for state in JobState)

# The widest zone token ``%Z`` produces at a realistic offset: five characters,
# the ``+0545`` shape the tz database falls back to where there is no
# abbreviation. The only literal in the width below, and the one this host
# cannot demonstrate on its own.
_WIDEST_ZONE_TOKEN = len("+0545")

# Width of the Timestamp column in `saneless jobs`, derived from a rendered
# sample rather than written down, exactly as _STATUS_COL_WIDTH is. The date
# and time half is fixed-width; only the zone token this host happens to report
# varies, so the column reserves the widest realistic one instead of trusting
# the three letters most of the world sees. A host whose token is wider still
# wins the max(), so a zone abbreviation cannot silently truncate the column.
# Comes to 22, which is what ts_w was before local time arrived -- the reserve
# the seconds used to occupy is exactly the reserve the zone now needs.
_TIME_COL_SAMPLE = local_time(datetime(2026, 9, 16, 19, 3, tzinfo=UTC))
_TIME_COL_WIDTH = max(
    len(_TIME_COL_SAMPLE),
    len(_TIME_COL_SAMPLE.rsplit(" ", 1)[0]) + 1 + _WIDEST_ZONE_TOKEN,
)

# What the operator is asked between the two manual-duplex passes.  A yes/no
# question rather than "press Enter" on purpose: click's pause(), the only API that
# matches "press Enter", is a documented no-op off a terminal and would start
# pass B on the unflipped stack without a word.
_FLIP_PROMPT = (
    "Flip the stack over and load it back into the feeder. Scan the back sides?"
)

# Why `scan` refuses when nobody configured paperless-ngx. A developer
# constant: it names the problem and never the value, the URL or the config
# path. Lower-cased and without a full stop because it is the tail of the CLI's
# `<what saneless was doing>: <problem>` error line, unlike checks.py's
# sentence for the same fact, which stands alone in a table row.
#
# The name carries no password-ish word on purpose: ruff's S105 reads the
# *name* of the target, not the value, so `_TOKEN_...` here would be flagged as
# a hardcoded credential. This is vocabulary.py's `_REJECTED_WIRE_VALUE` idiom
# rather than a suppression.
_UNSET_CREDENTIAL_PROBLEM = "the paperless-ngx API token has not been set"


# Whether a human can answer a prompt here, behind a function of its own rather
# than written inline: CliRunner is genuinely not a terminal, so the non-TTY
# refusal test runs unpatched and the prompt test patches this one name.
# Written as a bare sys.stdin.isatty() in scan, one of the two could not exist.
def _stdin_is_interactive() -> bool:
    """Whether stdin is a terminal a human can answer a prompt on."""
    return sys.stdin.isatty()


class ClickFlipCoordinator(FlipCoordinator):
    """
    The CLI flip coordinator: a terminal prompt with a bounded wait.

    ``click.confirm`` has no timeout of its own, so it runs on a daemon thread
    while the calling thread waits for at most ``timeout`` seconds.  Whichever
    of the operator's answer or the clock claims the single answer first is the
    answer -- claimed through ``FlipAnswerSlot``, the same slot the web worker's
    coordinator uses, so an answer that lands as the wait expires is honoured
    rather than overwritten.

    A yes is ``CONTINUED``; a no is ``ABORTED``; and so are EOF at the prompt
    (``click.Abort`` on the prompt thread) and Ctrl-C (``KeyboardInterrupt`` on
    the calling thread, where Python delivers SIGINT).  Those three are an
    operator's abort -- a cancel -- so giving up at the terminal and clicking
    Abort in the web UI end the job the same way.  End of input counts as a
    cancel however it arrives, including a terminal that closes, since
    ``click.confirm`` reports it as ``click.Abort``.  A prompt that fails with a
    read error instead -- an I/O error from the terminal, undecodable input --
    also answers ``ABORTED`` at once, logged with its traceback, but it records
    the exception as ``abort_cause``: nobody chose to stop, so the scan is
    reported as failed (exit 1), not cancelled.

    Accepted cost, deliberate and not a leak: after a timeout the prompt thread
    is abandoned.  It keeps its read on stdin until the process exits, and its
    prompt may be left sitting on the terminal.  That is bounded -- the job has
    already failed and the CLI is on its way out -- and a daemon thread parked
    on stdin was measured not to delay interpreter shutdown.  It must stay a
    daemon thread: a non-daemon one would hold the interpreter open at exit
    waiting for an answer nobody is going to give.
    """

    def __init__(self) -> None:
        """Start unanswered, with no abort cause."""
        self._slot = FlipAnswerSlot()
        # Held across a broken prompt's claim and its cause, so the calling
        # thread, woken by that claim, cannot read the cause before it is set
        # (as WorkerFlipCoordinator.abort_for_shutdown does).
        self._cause_lock = threading.Lock()
        self._abort_cause: Exception | None = None

    @property
    def abort_cause(self) -> Exception | None:
        """
        The exception a broken prompt raised, if that failure claimed the answer.

        Returns:
            The prompt's exception, or ``None`` when the answer came from the
            operator (yes, no, EOF, Ctrl-C) or from the clock.

        """
        with self._cause_lock:
            return self._abort_cause

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Prompt the operator, and claim ``TIMED_OUT`` if ``timeout`` elapses first.

        Args:
            timeout: The longest to wait for an answer, in seconds.

        Returns:
            The one answer this coordinator resolved to.

        """
        prompt = threading.Thread(
            target=self._prompt, name="saneless-flip-prompt", daemon=True
        )
        prompt.start()
        try:
            self._slot.wait(timeout)
        except KeyboardInterrupt:
            # Ctrl-C lands here, not in click.confirm: Python handles SIGINT on
            # the main thread, which is this one, parked in the wait.  Offered
            # as ABORTED so it ends the job the way a web Abort does.
            return self._slot.settle(FlipOutcome.ABORTED)
        # One path for both endings.  If the prompt answered, settle finds that
        # answer already claimed and hands it back; if the wait expired,
        # TIMED_OUT is offered and wins unless the answer beat it after all.
        return self._slot.settle(FlipOutcome.TIMED_OUT)

    def _prompt(self) -> None:
        """Ask the operator on the prompt thread and claim their answer."""
        try:
            flipped = click.confirm(_FLIP_PROMPT, default=True)
        except click.Abort:
            self._slot.settle(FlipOutcome.ABORTED)
            return
        except Exception as exc:
            # Anything else used to kill this thread silently, leaving the
            # calling thread waiting out the whole timeout and then reporting
            # that nobody confirmed the flip, which was false.  The prompt
            # broke, so the scan stops now: ABORTED rather than a fourth
            # outcome, with the exception kept as abort_cause so the pipeline
            # reports a failure, not a cancel.  The cause is set only if this
            # claim won: a Ctrl-C or a timeout that answered first keeps its own
            # meaning.  Logged before the claim, so the record exists once the
            # wait wakes.
            logger.exception("Flip prompt failed; treating it as an abort")
            with self._cause_lock:
                claimed = self._slot.offer(FlipOutcome.ABORTED)
                if claimed:
                    self._abort_cause = exc
            return
        self._slot.settle(FlipOutcome.CONTINUED if flipped else FlipOutcome.ABORTED)


def _truncate(value: str, width: int) -> str:
    """Truncate string to width, appending ellipsis if exceeding limit."""
    if len(value) <= width:
        return value
    return value[: width - 1] + "\u2026"


_CLICK_CONTROL_FLOW: tuple[type[Exception], ...] = (
    click.exceptions.Exit,
    click.exceptions.Abort,
    click.ClickException,
)
"""click's own control-flow exceptions, which the group guard re-raises untouched.

``--help`` raises ``Exit``, EOF at a prompt raises ``Abort``, and a usage error
is a ``ClickException``; click's ``main`` turns each into its own output and exit
code. Held under a name so the guard's clause is
one short, parenthesis-free ``except``: the multi-type spelling ruff formats to
(PEP 758) does not parse on the older interpreter the pre-commit AST hooks run.
"""


_VERBOSE_HINT = "Run again with -v to see the traceback"
"""The hint an unexpected error's line ends with when no traceback was kept."""


def _logging_ready(ctx: click.Context) -> bool:
    """
    Whether ``_load_cli_settings`` has configured logging for this process.

    Before that, an ERROR record has no handler but ``logging.lastResort``,
    which would print it -- traceback and all -- straight to stderr, so the
    guard must not log at all.

    Args:
        ctx: The group's context; its ``obj`` is shared with the subcommand's.

    Returns:
        True once logging is configured.

    """
    obj = ctx.obj
    return isinstance(obj, dict) and bool(obj.get("logging_configured"))


def _unexpected_line(exc: BaseException) -> str:
    """
    Render the one line an exception that is not a saneless type is reported as.

    Args:
        exc: The exception to report.

    Returns:
        ``Unexpected error (<Type>): <message>``, with no trailing hint.

    """
    return f"Unexpected error ({type(exc).__name__}): {describe(exc)}"


def _failure_line(exc: SanelessError, category: ErrorCategory) -> str:
    """
    Render the one line a classified saneless failure is reported as.

    The prefixes are documented (``docs/how-to/set-up-adf-duplex.md`` quotes
    them), so they are kept as they were before the guard existed. A
    configuration error is printed as-is: the loader's renderer already wrote
    its own ``Configuration error in <file>:`` header.

    Args:
        exc: The failure.
        category: What ``classify_error`` made of it.

    Returns:
        The line to print on stderr.

    """
    match category:
        case ErrorCategory.FEEDER | ErrorCategory.SCANNER:
            line = f"Scan error: {exc}"
        case ErrorCategory.CONFIG:
            line = str(exc)
        case ErrorCategory.UPLOAD:
            line = f"Paperless error: {exc}"
        case ErrorCategory.ASSEMBLY:
            line = f"PDF error: {exc}"
        case ErrorCategory.UNKNOWN | ErrorCategory.REJECTED:
            line = _unexpected_line(exc)
        case _:
            assert_never(category)
    return line


def _log_failure(ctx: click.Context, exc: Exception) -> None:
    """
    Log a failure with its traceback, but only once logging is configured.

    The message names the failure too: when the log fell back to stderr the
    traceback is not rendered there, and the record must still say what went
    wrong.

    Args:
        ctx: The group's context.
        exc: The failure to log.

    """
    if _logging_ready(ctx):
        logger.error(
            "saneless %s failed: %s",
            ctx.invoked_subcommand,
            describe(exc),
            exc_info=exc,
        )


def _report_unexpected(ctx: click.Context, exc: Exception) -> None:
    """
    Report an exception that is not a saneless type as one stderr line.

    Once logging is configured the traceback goes to the log, and the line
    points at the log file only when the file handler really attached
    (``configure_logging``'s return value): a stderr fallback must not be called
    a log file. That fallback never renders a traceback, so without ``-v`` the
    line then says how to get one. Before logging is configured nothing is
    logged; ``-v`` prints the traceback to stderr instead, and without it the
    line says how to get one.

    ``serve`` has neither a log file nor a traceback-free sink: its stream
    renders the traceback the ``logger.error`` above just emitted, with or
    without ``-v``. The hint is suppressed there rather than telling an operator
    to restart a running service to see something already printed directly above
    the line.

    Args:
        ctx: The group's context.
        exc: The exception to report.

    """
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    line = _unexpected_line(exc)
    verbose = bool(obj.get("verbose"))
    if _logging_ready(ctx):
        logger.error(
            "Unexpected error in saneless %s", ctx.invoked_subcommand, exc_info=exc
        )
        log_file = obj.get("log_file")
        if log_file:
            line = f"{line}. Full details in {log_file}"
        elif not verbose and not obj.get("log_stream"):
            line = f"{line}. {_VERBOSE_HINT}"
    elif verbose:
        click.echo("".join(traceback.format_exception(exc)), err=True, nl=False)
    else:
        line = f"{line}. {_VERBOSE_HINT}"
    click.echo(line, err=True)


class _GuardedGroup(click.Group):
    """
    The CLI group with one last-resort handler around every command.

    Every failure a command raises becomes one stderr message and its
    ``ExitCode``, and no user ever sees a traceback unless they asked for one
    with ``-v``. The ``except`` clauses are ordered, and the order is the
    design:

    1. click's ``Exit``, ``Abort`` and ``ClickException`` are re-raised first.
       ``Exit`` and ``Abort`` subclass ``RuntimeError``, so a later
       ``except Exception`` would turn ``--help`` into exit 5.
    2. ``KeyboardInterrupt`` and ``ScanCancelledError`` are a cancel, not a
       failure: one line and exit 130.
    3. ``StorageError`` -- a job database saneless cannot use -- is a setup
       problem and exits 2. It is mapped here by type and sits before the
       ``SanelessError`` clause because ``ErrorCategory`` is persisted on job
       records, and ``classify_error`` deliberately keeps ``StorageError``
       ``UNKNOWN`` rather than growing a category for it. Its message already
       names the database path and the reason.
    4. Any other ``SanelessError`` is classified once, by the same
       ``classify_error`` the web worker uses, so the CLI's exit code and the
       job's category cannot disagree.
    5. Anything else is not a saneless type: ``Unexpected error (<Type>)``,
       exit 5, the traceback in the log.
    """

    def invoke(self, ctx: click.Context) -> object:
        """
        Invoke the subcommand, translating whatever it raises.

        Args:
            ctx: The group's context.

        Returns:
            Whatever the subcommand returned.

        """
        try:
            return super().invoke(ctx)
        except _CLICK_CONTROL_FLOW:
            raise
        except KeyboardInterrupt:
            logger.info("Command interrupted")
            click.echo("Cancelled (interrupted)", err=True)
            ctx.exit(ExitCode.CANCELLED)
        except ScanCancelledError as exc:
            logger.info("Scan cancelled: %s", exc)
            click.echo(str(exc), err=True)
            ctx.exit(ExitCode.CANCELLED)
        except StorageError as exc:
            _log_failure(ctx, exc)
            click.echo(f"Job database error: {exc}", err=True)
            ctx.exit(ExitCode.CONFIG)
        except SanelessError as exc:
            category = classify_error(exc)
            code = exit_code_for(category)
            if code is ExitCode.UNEXPECTED:
                _report_unexpected(ctx, exc)
            else:
                _log_failure(ctx, exc)
                # Two lines, and the first one is not ours to change. Line 1
                # keeps its documented `<what saneless was doing>: <problem>`
                # shape, printed unchanged, so a script parsing it and the
                # doc-truth message-shape tests that pin it are both unaffected.
                # Line 2 is the same error_next_step string the web error page
                # renders -- which is why the copy is surface-neutral and never
                # says "press Scan" or "run the command". The UNEXPECTED branch
                # above gets no advice: its category is a guess about an
                # exception saneless did not raise.
                click.echo(_failure_line(exc, category), err=True)
                click.echo(f"Try: {error_next_step(category)}", err=True)
            ctx.exit(code)
        except Exception as exc:
            _report_unexpected(ctx, exc)
            ctx.exit(ExitCode.UNEXPECTED)


@click.group(cls=_GuardedGroup)
# package_name makes click read the version from importlib.metadata, so
# pyproject.toml stays its single source. No custom message is passed: the
# default "%(prog)s, version %(version)s" is the whole contract, because
# `saneless doctor` already reports the Python and platform detail a longer
# block would duplicate. No short flag either -- `-v` below is already --verbose
# on this group, and click would bind it to whichever option declared it last,
# silently.
@click.version_option(package_name="saneless")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Path to config file.",
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Log saneless's own debug detail: stderr, and the log file outside serve.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
    # Click runs this callback before a subcommand parses its own --help, and
    # ctx.resilient_parsing is False there, so nothing may be loaded here: a
    # broken config would otherwise break `saneless serve --help`.
    # Each command loads through _load_cli_settings instead.
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose


def _load_cli_settings(ctx: click.Context, *, stream_logs: bool = False) -> Settings:
    """
    Load and validate settings and configure logging, once per process.

    Called by every command on first need rather than by the group callback, so
    ``--help`` never touches the configuration. Nothing is caught here: every
    failure reaches the group guard. Loading and directory validation raise
    ``ConfigError`` for every problem with the file -- including a TOML syntax
    error -- which the guard prints as rendered and exits 2; anything else is an
    unexpected error, exit 5. An unwritable ``log_file`` is not a failure:
    ``configure_logging`` warns on stderr, logs there instead, and the command
    runs. ``load_settings`` and ``configure_logging`` are called by their
    module-global names, which is where the tests patch them.

    Once logging is configured, ``ctx.obj`` records it (``logging_configured``)
    and records ``log_file`` only if the file handler really attached, so the
    guard logs failures and names the log file truthfully. In the streaming
    mode ``serve`` asks for, no file handler is attached at all, so
    ``ctx.obj["log_file"]`` is always None there and nothing ever offers
    "Full details in <log_file>" for a service that writes none.

    Args:
        ctx: The command's context; its ``obj`` carries ``config_path`` and
            ``verbose`` from the group, and caches the loaded settings.
        stream_logs: If True, configure the 12-factor service shape -- records
            stream to stderr and no log file is written, so ``docker logs`` or
            journald sees them and owns retention. Only ``serve`` passes it;
            every one-shot command keeps the rotating file handler unchanged.

    Returns:
        The loaded settings, the same object on every call.

    """
    cached = ctx.obj.get("settings")
    if isinstance(cached, Settings):
        return cached

    settings = load_settings(ctx.obj.get("config_path"))
    validate_settings_dirs(settings)
    # A service is handed no log file at all: configure_logging then attaches
    # one stderr handler and returns False, so the line below records no
    # log_file with no special case of its own. The rotation settings still go
    # along and are simply unused -- they govern one-shot mode, which is what
    # the configuration reference says and why no warning fires here.
    attached = configure_logging(
        None if stream_logs else settings.output.log_file,
        settings.output.log_level,
        max_bytes=settings.output.log_max_bytes,
        backup_count=settings.output.log_backup_count,
        verbose=bool(ctx.obj.get("verbose")),
    )
    ctx.obj["logging_configured"] = True
    ctx.obj["log_file"] = settings.output.log_file if attached else None
    # Read by _report_unexpected: a stream that already renders tracebacks
    # must not end an exit-5 line with "run again with -v".
    ctx.obj["log_stream"] = stream_logs

    # Emitted only now, once the log file handler exists to receive it.
    warn_on_legacy_duplex_sources(settings)
    # Which file and which environment keys, names only, once.
    log_config_sources(settings)

    ctx.obj["settings"] = settings
    return settings


@cli.command()
@click.option(
    "--profile",
    default="default",
    help="Scan profile name.",
)
@click.option(
    "--title",
    default="",
    help="Document title (default: the profile's title, else 'Scan <time>').",
)
@click.pass_context
def scan(ctx: click.Context, profile: str, title: str) -> None:
    """Scan a document and upload to paperless-ngx."""
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
    require_sane()
    settings = _load_cli_settings(ctx)

    if profile not in settings.profiles:
        click.echo(f"Unknown profile: {profile}", err=True)
        ctx.exit(ExitCode.CONFIG)

    # The one title rule the web form shares -- typed, else the profile's
    # title, else "Scan <time>"; blank after stripping counts as not typed.
    now = datetime.now(tz=UTC)
    resolved_title = resolve_job_title(title, settings.profiles[profile], now=now)

    # Refused here, before the scanner is opened, because a scan that
    # cannot upload is wasted paper. The refusal is unconditional -- a
    # configured paperless.consume_dir fallback does not soften it, or `scan`,
    # `doctor` and the web UI would disagree about whether the appliance can
    # work. `devices`, `auto-profiles` and `jobs` are deliberately untouched:
    # none of them talks to paperless-ngx.
    #
    # This is the second place cli.py unwraps the token; the PaperlessClient
    # construction below is the other. The value goes to the predicate and
    # nowhere else -- it is never logged, echoed or interpolated into the
    # message, which is a developer constant (ASVS V7). The line is built in the
    # `<what saneless was doing>: <problem>` shape because ErrorCategory.CONFIG
    # prints the exception as-is (_failure_line), so the "what saneless was
    # doing" half has to be part of the message.
    if is_placeholder_token(settings.paperless.token.get_secret_value()):
        msg = (
            f"Scanning '{resolved_title}' with profile '{profile}': "
            f"{_UNSET_CREDENTIAL_PROBLEM}"
        )
        raise ConfigError(msg)

    manual_duplex = settings.profiles[profile].duplex == "manual"
    # Refused here, before the backend exists, so no paper moves: from cron or a
    # pipe there is nobody to flip the stack, and pass A would be wasted.
    if manual_duplex and not _stdin_is_interactive():
        click.echo(
            f"Profile '{profile}' is manual duplex, which needs an interactive "
            "terminal: saneless must prompt you to flip the stack between the "
            "two passes. Run it from a terminal, or scan from the web UI.",
            err=True,
        )
        ctx.exit(ExitCode.CONFIG)

    scanner = SaneBackend(host=settings.scanner.host)
    # click runs close callbacks when this command's Context leaves its `with`
    # block: on success, on ctx.exit(), and while an exception propagates --
    # all of them before the guarded group's error handlers choose an exit
    # code. So SANE is already down by the time the error line is printed, and
    # no early exit path can skip it.
    ctx.call_on_close(scanner.close)
    paperless = PaperlessClient(
        settings.paperless.url,
        settings.paperless.token.get_secret_value(),
        settings.paperless.consume_dir,
    )

    def status_callback(event: PipelineEvent) -> None:
        state = event.job_state
        if event is PipelineEvent.DONE:
            click.echo(f"Done: {resolved_title}")
        else:
            click.echo(progress_label(state))

    try:
        request = PipelineRequest(
            profile_name=profile,
            title=resolved_title,
            # A uuid4, exactly as the worker supplies for a web job.  Without
            # one, every CLI run composed {timestamp}-{title-slug}.pdf and
            # rested on the timestamp alone -- while build_pdf_filename's whole
            # collision argument is "uniqueness comes from the job id".  That is
            # not cosmetic: preservation moves onto an explicit destination
            # path, which overwrites silently, so two same-second scans of the
            # same title would have destroyed one of them in failed/. Four kinds
            # of artefact land there, and every mid-scan fault can reach it.
            job_id=str(uuid4()),
            tags=settings.profiles[profile].default_tags or None,
            correspondent=settings.profiles[profile].default_correspondent,
            status_callback=status_callback,
            # run_pipeline is synchronous, so the flip wait holds this thread;
            # only the click.confirm read itself moves to the prompt thread.
            flip_coordinator=ClickFlipCoordinator() if manual_duplex else None,
        )
        run_pipeline(
            scanner,
            paperless,
            settings,
            request,
        )
    finally:
        # Failures reach the group guard, which prints one line and exits with
        # its ExitCode; the client is closed either way.
        paperless.close()


def _echo_capabilities(caps: DeviceCapabilities) -> None:
    """
    Print one device's capabilities, naming only what that device reported.

    Extracted from ``devices`` so that rendering the resolution constraint in
    whichever shape the device gave it does not push that command past ruff's
    PLR0912 branch limit. The limit is respected rather than raised, and
    nothing is suppressed.

    Every line is printed only when there is something to put after its label.
    A label followed by nothing is the symptom the operator actually saw on a
    range-reporting device: it reads as "this scanner offers none",
    when the truth was that saneless had not read what the scanner offered.

    Args:
        caps: The capabilities to render.

    """
    if caps.sources:
        click.echo(f"  Sources: {', '.join(caps.sources)}")
    if caps.resolutions:
        click.echo(f"  Resolutions: {', '.join(str(r) for r in caps.resolutions)}")
    elif caps.resolution_range is not None:
        # The device gave a span rather than an enumeration, so it is shown as
        # a span. Expanding it into a list of plausible values would print
        # saneless's own invention rather than the device's answer.
        low, high, step = caps.resolution_range
        click.echo(f"  Resolution range: {low:g} to {high:g} dpi in steps of {step:g}")
    if caps.modes:
        click.echo(f"  Modes: {', '.join(caps.modes)}")
    if caps.raw_options:
        click.echo("  Raw options:")
        for opt in caps.raw_options:
            if len(opt) >= 2:
                click.echo(f"    {opt[1]}")


def _capabilities_dict(caps: DeviceCapabilities) -> dict[str, object]:
    """
    Return one device's capabilities as the JSON object ``devices`` prints.

    This is the machine contract scripts read, so it carries exactly what
    ``_echo_capabilities`` shows and nothing it does not: a key appears only
    when the device reported something for it, and the resolution support
    keeps whichever shape the device gave, a list or a min/max/step range
    with the floats as reported.

    Args:
        caps: The capabilities to convert.

    Returns:
        The capabilities, keyed in the order the text mode prints them.

    """
    result: dict[str, object] = {}
    if caps.sources:
        result["sources"] = list(caps.sources)
    if caps.resolutions:
        result["resolutions"] = list(caps.resolutions)
    elif caps.resolution_range is not None:
        low, high, step = caps.resolution_range
        result["resolution_range"] = {"min": low, "max": high, "step": step}
    if caps.modes:
        result["modes"] = list(caps.modes)
    raw_names = [opt[1] for opt in caps.raw_options if len(opt) >= 2]
    if raw_names:
        result["raw_options"] = raw_names
    return result


def _probe_capabilities(scanner: ScannerBackend, name: str) -> DeviceCapabilities | str:
    """
    Read one device's capabilities, or report why they could not be read.

    Only a scanner error is caught: one device that will not answer must not
    hide the others, so its reason is logged and printed as one stderr line
    and the caller carries on. Anything else is a saneless bug and still
    reaches the group guard.

    Args:
        scanner: The open backend.
        name: The SANE device name to probe.

    Returns:
        The capabilities, or the one-line reason the probe failed.

    """
    try:
        return scanner.get_capabilities(name)
    except ScanError as exc:
        reason = describe(exc)
        logger.warning("Could not read capabilities for %s: %s", name, reason)
        click.echo(f"Capabilities for {name}: {reason}", err=True)
        return reason


def _echo_device_table(device_list: list[DeviceInfo]) -> None:
    """
    Print the device list as a table sized to the terminal.

    Args:
        device_list: The devices SANE reported.

    """
    cols = shutil.get_terminal_size((80, 24)).columns
    name_w = max(20, cols - 45)
    vendor_w = 15
    model_w = 20
    header = f"{'Name':<{name_w}} {'Vendor':<{vendor_w}} {'Model':<{model_w}} {'Type'}"
    click.echo(header)
    click.echo("-" * min(len(header), cols))
    for d in device_list:
        click.echo(
            f"{_truncate(d.name, name_w):<{name_w}} "
            f"{_truncate(d.vendor, vendor_w):<{vendor_w}} "
            f"{_truncate(d.model, model_w):<{model_w}} "
            f"{d.device_type}"
        )


def _devices_as_json(
    scanner: ScannerBackend, device_list: list[DeviceInfo], *, capabilities: bool
) -> int:
    """
    Print the device list as one JSON document on stdout.

    Without ``capabilities`` the document is the four keys it has always had,
    in the same order, so scripts written against it see no change. With it,
    each device also gets ``capabilities``, which is ``null`` alongside a
    ``capabilities_error`` reason when that device's probe failed.

    Args:
        scanner: The open backend.
        device_list: The devices SANE reported.
        capabilities: Whether to probe and include each device's capabilities.

    Returns:
        The number of devices whose capabilities could not be read.

    """
    data: list[dict[str, object]] = [
        {
            "name": d.name,
            "vendor": d.vendor,
            "model": d.model,
            "type": d.device_type,
        }
        for d in device_list
    ]
    failed = 0
    if capabilities:
        for entry, d in zip(data, device_list, strict=True):
            probed = _probe_capabilities(scanner, d.name)
            if isinstance(probed, str):
                entry["capabilities"] = None
                entry["capabilities_error"] = probed
                failed += 1
            else:
                entry["capabilities"] = _capabilities_dict(probed)
    click.echo(json.dumps(data, indent=2))
    return failed


def _devices_as_text(
    scanner: ScannerBackend, device_list: list[DeviceInfo], *, capabilities: bool
) -> int:
    """
    Print the device table, then each device's capabilities when asked.

    Args:
        scanner: The open backend.
        device_list: The devices SANE reported.
        capabilities: Whether to probe and print each device's capabilities.

    Returns:
        The number of devices whose capabilities could not be read.

    """
    _echo_device_table(device_list)
    failed = 0
    if capabilities:
        click.echo()
        for d in device_list:
            probed = _probe_capabilities(scanner, d.name)
            if isinstance(probed, str):
                failed += 1
                continue
            click.echo(f"Capabilities for {d.name}:")
            _echo_capabilities(probed)
    return failed


@cli.command()
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output as JSON.",
)
@click.option(
    "--capabilities",
    is_flag=True,
    help="Show sources, modes, resolution support and raw SANE option names.",
)
@click.pass_context
def devices(ctx: click.Context, *, as_json: bool, capabilities: bool) -> None:
    """List available scanning devices."""
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
    require_sane()
    _settings = _load_cli_settings(ctx)

    # Status goes to stderr, so stdout carries only data in either mode and a
    # pipe into jq or grep sees nothing else.
    if not as_json:
        click.echo("Discovering scanners...", err=True)
    scanner = SaneBackend(host=_settings.scanner.host)
    # Registered before the first SANE call, so an enumeration that fails still
    # leaves the process with SANE shut down.
    ctx.call_on_close(scanner.close)
    device_list = scanner.get_devices()

    if not device_list:
        click.echo("No scanners found." if not as_json else "[]")
        return

    render = _devices_as_json if as_json else _devices_as_text
    failed = render(scanner, device_list, capabilities=capabilities)
    # A device whose capabilities could not be read is reported where it
    # stands and the others still print; the exit code then says a scanner
    # read failed, once everything has been written.
    if failed:
        ctx.exit(ExitCode.SCAN)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option(
    "--limit", default=CLI_JOBS_DEFAULT_LIMIT, type=int, help="Maximum jobs to show."
)
@click.pass_context
def jobs(ctx: click.Context, *, as_json: bool, limit: int) -> None:
    """List recent scan job history."""
    settings = _load_cli_settings(ctx)
    # sqlite3.connect does not create parent directories, so data_dir must
    # exist before JobStore opens the database. Deliberately not hidden inside
    # the db_path property: a property with a filesystem side effect surprises.
    settings.output.data_dir.mkdir(parents=True, exist_ok=True)
    store = JobStore(db_path=settings.output.db_path)
    try:
        recent = store.list_recent(limit=limit)
        if as_json:
            click.echo(
                json.dumps(
                    [
                        {
                            "id": j.id,
                            "profile": j.profile,
                            "title": j.title,
                            # Raw enum value, deliberately not humanised: this
                            # is the machine contract and scripts compare
                            # against "DONE" / "FALLBACK".
                            "state": j.state.value,
                            # UTC ISO-8601, deliberately not localised: this is
                            # a machine contract documented in
                            # docs/how-to/cli-scripting.md, and local time is
                            # for *user-facing* surfaces. The human table below
                            # goes local; this does not.
                            "created_at": j.created_at.isoformat(),
                            "outcome": j.outcome.value if j.outcome else None,
                            "warning": j.warning,
                        }
                        for j in recent
                    ],
                    indent=2,
                )
            )
        else:
            cols = shutil.get_terminal_size((80, 24)).columns
            ts_w = _TIME_COL_WIDTH
            profile_w = 15
            # Three single spaces separate the four columns.
            title_w = max(15, cols - (ts_w + profile_w + _STATUS_COL_WIDTH + 3))
            header = (
                f"{'Timestamp':<{ts_w}} {'Profile':<{profile_w}} "
                f"{'Title':<{title_w}} {'Status'}"
            )
            click.echo(header)
            click.echo("-" * min(len(header), cols))
            for j in recent:
                click.echo(
                    # The one shared formatter the web history table reads, so
                    # the two surfaces cannot drift. Seconds are gone and
                    # the zone is named.
                    f"{local_time(j.created_at):<{ts_w}} "
                    f"{_truncate(j.profile, profile_w):<{profile_w}} "
                    f"{_truncate(j.title, title_w):<{title_w}} "
                    f"{state_label(j.state)}"
                )
    finally:
        store.close()


def _bind_listening_socket(host: str, port: int) -> socket.socket:
    """
    Bind and listen on the address ``serve`` was given, before uvicorn starts.

    The socket is bound here rather than by uvicorn so that the port saneless
    reports is the port it holds: with port 0 the OS chooses one, and there is
    no gap between checking a port and binding it for another process to take
    it in. The address family comes from resolving the host, so an IPv6
    address binds IPv6. Only the first address a name resolves to is bound,
    so ``--host`` is meant to take an address.

    The options match what uvicorn's own bind set. SO_REUSEADDR lets a
    restart bind a port whose last connections are still closing. On IPv6,
    IPV6_V6ONLY keeps ``::`` from also listening on IPv4 behind the
    operator's back. Port sharing is never switched on, because it would let
    a second process listen on the same port. ``listen()`` is called before
    the socket is handed over, so a connection made while the app starts up
    waits in the queue instead of being refused.

    Args:
        host: The address to bind.
        port: The port to bind; 0 asks the OS for a free one.

    Returns:
        The bound, listening socket.

    Raises:
        ConfigError: The host did not resolve or the address could not be
            bound. That is a setup problem, so it exits 2 like any other
            failure to start.

    """
    try:
        family, socktype, proto, _, sockaddr = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )[0]
        sock = socket.socket(family, socktype, proto)
    except OSError as exc:
        msg = f"Cannot bind to {host}:{port}: {describe(exc)}"
        raise ConfigError(msg) from exc
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if family == socket.AF_INET6:
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind(sockaddr)
        sock.listen(_LISTEN_BACKLOG)
    except OSError as exc:
        sock.close()
        msg = f"Cannot bind to {host}:{port}: {describe(exc)}"
        raise ConfigError(msg) from exc
    return sock


@cli.command()
@click.option("--host", default=None, help="Bind address.")
@click.option(
    "--port",
    default=None,
    type=click.IntRange(0, 65_535),
    help="Bind port; 0 lets the OS choose one.",
)
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the web server."""
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
    require_sane()
    # The one command that streams its logs: a service writes no file, so its
    # records reach `docker logs` and journald instead. Every other command
    # keeps the rotating file handler.
    settings = _load_cli_settings(ctx, stream_logs=True)
    actual_host = host or settings.output.web_host
    # An explicit 0 is a request for an OS-chosen port, not a missing value,
    # so only an absent flag falls back to the configured port.
    actual_port = port if port is not None else settings.output.web_port

    # serve scans nothing itself, so SANE failing to initialise is a failure
    # to start -- "can't start, fix your setup", exit 2 like a port that cannot
    # be bound -- not exit 1, which means a scan failed.
    try:
        scanner = SaneBackend(host=settings.scanner.host)
    except ScanError as exc:
        msg = f"The web server could not start: {exc}"
        raise ConfigError(msg) from exc
    # No close callback here, unlike the three one-shot commands: this backend
    # outlives the command body.  The app is handed it and the lifespan closes
    # it once the worker confirms it stopped, which is the only point at which
    # no thread can still be inside SANE.
    app = create_app(settings, scanner)

    sock = _bind_listening_socket(actual_host, actual_port)
    bound_port = sock.getsockname()[1]
    # An IPv6 address is bracketed so the URL can be pasted into a browser.
    shown_host = f"[{actual_host}]" if ":" in actual_host else actual_host
    click.echo(f"Serving on http://{shown_host}:{bound_port}", err=True)
    logger.info("Serving on http://%s:%d", shown_host, bound_port)

    # uvicorn follows the configured log_level, not -v: -v is saneless's own
    # detail and must not turn on uvicorn's or httpx's debug output. The
    # validated log level lower-cases to a name uvicorn accepts.
    #
    # The config needs nothing extra for the streaming mode, and adding
    # anything would break it: a None log config means uvicorn applies no
    # dictConfig and attaches no handlers of its own, so uvicorn.error,
    # uvicorn.access and uvicorn.asgi propagate to saneless's root handlers.
    # Leaving the access log on therefore puts per-request lines, and
    # uvicorn's own startup lines, on the same stream for free.
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_config=None,
            log_level=settings.output.log_level.lower(),
            access_log=True,
        )
    )
    # On Ctrl-C uvicorn shuts down gracefully and then re-raises the signal it
    # caught, which arrives here as KeyboardInterrupt. A running server being
    # stopped is a normal stop, exit 0, so it is swallowed here; a Ctrl-C
    # before this point -- while settings load or the app is built -- is not
    # uvicorn's to handle and reaches the group guard, exit 130.
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
    # Server.run returns quietly when start-up fails, such as the app's
    # lifespan raising; uvicorn has already logged why. Every command shares
    # one exit table, so that is a failure to start: one line, exit 2.
    if not server.started:
        msg = (
            f"The web server could not start on http://{shown_host}:{bound_port}; "
            "the cause is in the preceding log lines"
        )
        raise ConfigError(msg)


def _echo_write_result(
    result: ProfileWriteResult, profiles: dict[str, ProfileConfig]
) -> None:
    """
    Print what ``auto-profiles`` did to the config file, grouped by action.

    The group lines come from ``ProfileWriteResult.groups``, the same
    vocabulary the worker's startup log uses. Added and Refreshed
    groups are followed by one detail line per profile they wrote.

    Args:
        result: What the write did.
        profiles: The generated profiles, for the detail lines.

    """
    path = result.path.resolve()
    groups = result.groups()
    if not groups:
        click.echo(f"No changes to {path}.")
        return
    click.echo(f"Profiles in {path}:")
    for line, written in groups:
        click.echo(line)
        for name in written:
            p = profiles[name]
            click.echo(
                f"  {name}: source={p.source}, resolution={p.resolution}, mode={p.mode}"
            )


@cli.command(name="auto-profiles")
@click.option(
    "--force",
    is_flag=True,
    help=(
        "Refresh the generated keys of auto-generated profiles (hand-written "
        "profiles are never touched)."
    ),
)
@click.pass_context
def auto_profiles(ctx: click.Context, *, force: bool) -> None:
    """Generate scan profiles from scanner capabilities."""
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard. --help never
    # reaches this body, so it needs no python-sane.
    require_sane()
    settings = _load_cli_settings(ctx)

    scanner = SaneBackend(host=settings.scanner.host)
    # This command ends through ctx.exit() as well as by returning and by
    # raising; a close callback covers all three.
    ctx.call_on_close(scanner.close)
    device_list = scanner.get_devices()
    if not device_list:
        # A setup problem, exit 2 through the guard, exactly as `scan` reports
        # the same finding: an exit code means the same thing in every command.
        msg = (
            "No scanner found: auto-detection found no devices. "
            "Check what SANE can see with `saneless devices`"
        )
        raise ConfigError(msg)

    # Use configured device or first discovered device
    device_id = settings.scanner.device or device_list[0].name
    caps = scanner.get_capabilities(device_id)
    profiles = generate_profiles(caps)

    # The file that was loaded (including an explicit --config). With no loaded
    # file the target stays ./saneless.toml, and the output names the resolved
    # absolute path so the operator sees where it went.
    config_path = settings.config_path or Path("./saneless.toml")
    # A ConfigError here (a single-file bind mount, a non-UTF-8 file, or
    # merged text that would not parse) names the file and the fix; the group
    # guard prints it as-is and exits 2, with no traceback.
    try:
        result = write_profiles_to_config(config_path, profiles, force=force)
    except OSError as exc:
        click.echo(f"Cannot write {config_path}: {exc.strerror or exc}", err=True)
        ctx.exit(ExitCode.CONFIG)
    _echo_write_result(result, profiles)


def _state_marker(state: CheckState) -> str:
    """
    Return the bracketed token ``doctor`` prints in front of one check row.

    These are CLI affordances rather than vocabulary, which is why they are not
    ``check_state_label``: that function answers "what does a screen reader
    announce", and the answer there is ``"Failed"``, a word. Here the job is a
    scannable left margin in a fixed-width terminal, so all three tokens are
    the same width and a reader's eye finds the red rows without reading them.

    A total ``match``, for the reason ``checks.py``'s four lookups are: a
    fourth ``CheckState`` stops this function type-checking until somebody
    decides what it looks like.

    Args:
        state: The state to mark.

    Returns:
        ``"[ OK ]"``, ``"[WARN]"`` or ``"[FAIL]"``.

    Raises:
        AssertionError: If the value is not a CheckState member.

    """
    match state:
        case CheckState.OK:
            marker = "[ OK ]"
        case CheckState.WARN:
            marker = "[WARN]"
        case CheckState.FAIL:
            marker = "[FAIL]"
        case _:
            assert_never(state)
    return marker


# The token a row nobody probed prints instead of `[ OK ]`.
#
# No `doctor` invocation produces this marker today: the command builds its
# CheckContext with `skip_scanner` at its default and passes no scanner gate,
# so neither `_scanner_skipped` nor `_scanner_busy` is reachable from the CLI.
# It exists anyway, and deliberately. The point of one check registry is that
# it feeds both surfaces, so a surface that would mis-render a row the registry
# can build is a divergence already present and merely unreached -- and
# `CheckResult.skipped` was once left exactly that way, half-wired, rendered by
# neither surface while two docstrings said it was rendered by both.
# The first caller that passes `skip_scanner=True` should get a correct table,
# not a bug report.
#
# Six characters, like the three state tokens, so `_MARKER_WIDTH` below does
# not silently widen the name column for one row.
_SKIPPED_MARKER: Final = "[SKIP]"


def _row_marker(result: CheckResult) -> str:
    """
    Return the token ``doctor`` prints in front of one finished row.

    ``_state_marker`` answers "what does this state look like"; this answers
    "what does this row look like", and the two differ whenever ``skipped`` is
    set.  The flag is a fact about the probe and the state is a verdict about
    the appliance: a skipped row carries ``CheckState.OK`` so that a scripted
    health gate does not go red for a probe that was deliberately not taken,
    which means marking it from the state alone prints the one token a reader
    scans for as "fine" in front of a sentence saying nothing was checked.
    ``checks.check_row_class`` and its two siblings make the same substitution
    for the web strip, from the same flag.

    Args:
        result: The finished row about to be printed.

    Returns:
        ``_SKIPPED_MARKER`` when the probe was skipped, otherwise
        ``_state_marker(result.state)``.

    """
    if result.skipped:
        return _SKIPPED_MARKER
    return _state_marker(result.state)


# The three column widths `saneless doctor` renders with, all derived rather
# than written down, exactly as _STATUS_COL_WIDTH is and for the same reason: a
# sixth CheckKey with a longer name, or a fourth CheckState with a wider token,
# must not be able to overflow an 80-column terminal without anyone noticing.
# The indent puts a next step underneath the message it belongs to, so a row
# and its remedy read as one item rather than two.
#
# The marker width counts `_SKIPPED_MARKER` alongside the state tokens rather
# than relying on the four strings happening to be the same length, so the
# derivation stays correct if any one of them is ever respelled.
_MARKER_WIDTH = max(
    len(marker)
    for marker in (*(_state_marker(state) for state in CheckState), _SKIPPED_MARKER)
)
_NAME_COL_WIDTH = max(len(check_name(key)) for key in CheckKey)
_NEXT_STEP_INDENT = " " * (_MARKER_WIDTH + 1 + _NAME_COL_WIDTH + 1)


def _doctor_scanner(settings: Settings) -> ScannerBackend | None:
    """
    Build a scanner backend for one ``doctor`` run, or report that there is none.

    ``None`` is how ``CheckContext`` represents "no scanner support on this
    machine", and producing it here rather than letting the failure out is what
    lets ``doctor`` report a machine without scanner support instead of
    refusing to run on it.

    All three failure shapes collapse to ``None``. ``ImportError`` is the bare
    missing module; ``ConfigError`` is what ``require_sane`` -- which
    ``SaneBackend.__init__`` calls for itself -- raises once it has translated
    that ``ImportError``; and ``ScanError`` is ``sane.init()`` refusing. The
    last is the least obvious of the three and is deliberate: a libsane that
    will not initialise is SANE support this machine does not actually have,
    the row's "install scanner support, then restart" is the right advice for
    it, and letting it out instead would exit 1 -- a code ``doctor``'s
    documented table does not list, for a command that scans nothing.

    Args:
        settings: The loaded configuration, for the sane-net host.

    Returns:
        A backend, or None when none could be built.

    """
    try:
        return SaneBackend(host=settings.scanner.host)
    except (ImportError, ConfigError, ScanError) as exc:
        # The type name only. The ConfigError's own message names the install
        # hint and the ImportError names a shared object path, and neither
        # belongs on a report a household member is meant to act on.
        logger.info("Scanner support unavailable: %s", type(exc).__name__)
        return None


def _doctor_paperless(settings: Settings) -> PaperlessClient | None:
    """
    Build a Paperless client for one ``doctor`` run, or report that there is none.

    ``PaperlessClient.__init__`` refuses exactly one thing -- a URL httpx will
    not parse -- and it refuses it with ``PaperlessError``, which the group
    guard would turn into exit 3. That would cost the operator the other four
    rows to report a fact the Paperless row already has a sentence for, and it
    would put a code in ``doctor``'s output that its documented table does not
    list. ``None`` reaches ``checks.py`` as the "not found at that URL" row.

    Args:
        settings: The loaded configuration.

    Returns:
        A client, or None when none could be built.

    """
    try:
        return PaperlessClient(
            settings.paperless.url,
            settings.paperless.token.get_secret_value(),
            settings.paperless.consume_dir,
        )
    except PaperlessError as exc:
        # The message names the URL, which may carry user:pass@.
        logger.info("Paperless client unavailable: %s", type(exc).__name__)
        return None


# This command deliberately does NOT call require_sane(), which is the first
# statement of `scan`, `devices`, `serve` and `auto-profiles`. Those four cannot
# do their job without a scanner, so refusing early is honest. `doctor`'s job is
# to say what is wrong, and a machine with no python-sane is precisely the
# machine whose owner needs that said: it still has a token, profiles, a
# fallback folder and a data directory to be told about. The import failure is
# caught in _doctor_scanner and rendered as one FAIL row among five instead of a
# refusal to run at all.
#
# There is no --json, and this is a decision rather than an omission. Nothing in
# the docs, the tests, the Dockerfile or the compose file would consume it, and
# a container HEALTHCHECK that calls `doctor` -- the one caller that would have
# wanted a machine shape -- is deliberately not offered. A JSON mode would be a
# wire contract with no reader, and a wire contract is only free until the first
# person parses it. The human-readable table plus the exit code is the whole
# contract.
@cli.command()
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Check that saneless is ready to scan."""
    settings = _load_cli_settings(ctx)
    scanner = _doctor_scanner(settings)
    if scanner is not None:
        # Registered before the first SANE call, so a check that fails still
        # leaves the process with SANE shut down.
        ctx.call_on_close(scanner.close)
    paperless = _doctor_paperless(settings)
    try:
        results = run_checks(
            CheckContext(
                settings=settings,
                scanner=scanner,
                paperless=paperless,
                # A one-shot command attempts no persist, so this is the
                # derivation it is entitled to; the function's docstring has
                # the reasoning, including why it cannot report the third
                # outcome. The status strip calls the same function, which is
                # what keeps the two surfaces on one Profiles row.
                profile_storage=profile_storage_for_loaded(settings),
            )
        )
    finally:
        if paperless is not None:
            paperless.close()

    for result in results:
        click.echo(
            f"{_row_marker(result):<{_MARKER_WIDTH}} "
            f"{check_name(result.key):<{_NAME_COL_WIDTH}} {result.message}"
        )
        if result.next_step:
            click.echo(f"{_NEXT_STEP_INDENT}{result.next_step}")

    # Any failing check exits 2, and no new ExitCode member expresses it. Three
    # reasons, in order: tests/test_deployment_config.py:393,403 assert the
    # documented global tables equal every member, so a sixth code is a
    # documentation change in three files and a revision of the exit-code table;
    # 2 already means "can't start, fix your setup", which is what every red
    # check is saying; and `doctor` reports a list, so one process has one exit
    # code to give and splitting a red Paperless row out to 3 would mean
    # choosing which red row the shell gets to hear about. A WARN is
    # deliberately not a failure -- an appliance that scans and files is not
    # broken because it could be tidier, and a gate that goes red for tidiness
    # gets ignored.
    if worst_state(results) is CheckState.FAIL:
        ctx.exit(ExitCode.CONFIG)
