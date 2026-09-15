"""
Click CLI group with scan and devices commands.

Provides the main command-line interface for saneless, including
scanner discovery and the full scan-to-upload pipeline.
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
from typing import TYPE_CHECKING, assert_never

import click
import uvicorn

from .auto_profiles import (
    generate_profiles,
    write_profiles_to_config,
)
from .config import (
    Settings,
    load_settings,
    log_config_sources,
    resolve_job_title,
    validate_settings_dirs,
    warn_on_legacy_duplex_sources,
)
from .exceptions import (
    ConfigError,
    SanelessError,
    ScanCancelledError,
    StorageError,
    describe,
)
from .job import JobStore
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
    exit_code_for,
    progress_label,
    state_label,
)
from .web.app import create_app

if TYPE_CHECKING:
    from .auto_profiles import ProfileWriteResult
    from .config import ProfileConfig
    from .scanner.base import DeviceCapabilities

__all__ = ["ClickFlipCoordinator", "_truncate", "cli"]

logger = logging.getLogger(__name__)

# Width of the Status column in `saneless jobs`, derived rather than written
# down: the humanised labels are longer than the raw enum values they replaced,
# and a ninth JobState member must not be able to overflow an 80-column
# terminal without anyone noticing.
_STATUS_COL_WIDTH = max(len(state_label(state)) for state in JobState)

# What the operator is asked between the two manual-duplex passes.  A yes/no
# question rather than "press Enter" on purpose: click's pause(), the only API that
# matches "press Enter", is a documented no-op off a terminal and would start
# pass B on the unflipped stack without a word (C-02).
_FLIP_PROMPT = (
    "Flip the stack over and load it back into the feeder. Scan the back sides?"
)


# Whether a human can answer a prompt here, behind a function of its own rather
# than written inline: CliRunner is genuinely not a terminal, so the non-TTY
# refusal test runs unpatched and the prompt test patches this one name.
# Written as a bare sys.stdin.isatty() in scan, one of the two could not exist.
def _stdin_is_interactive() -> bool:
    """Whether stdin is a terminal a human can answer a prompt on."""
    return sys.stdin.isatty()


class ClickFlipCoordinator(FlipCoordinator):
    """
    The CLI flip coordinator: a terminal prompt with a bounded wait (D-19).

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
    Abort in the web UI end the job the same way (D-02).  A prompt that fails
    unexpectedly -- a lost terminal, undecodable input -- also answers
    ``ABORTED`` at once, logged with its traceback (WR-08), but it records the
    exception as ``abort_cause``: nobody chose to stop, so the scan is reported
    as failed (exit 1), not cancelled.

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
        # (the WorkerFlipCoordinator.abort_for_shutdown precedent, WR-06).
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
            # WR-08: anything else used to kill this thread silently, leaving
            # the calling thread waiting out the whole timeout and then
            # reporting that nobody confirmed the flip, which was false.  The
            # prompt broke, so the scan stops now: ABORTED rather than a fourth
            # outcome (D-09), with the exception kept as abort_cause so the
            # pipeline reports a failure, not a cancel (D-02).  The cause is
            # set only if this claim won: a Ctrl-C or a timeout that answered
            # first keeps its own meaning.  Logged before the claim, so the
            # record exists once the wait wakes.
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


def _logging_ready(ctx: click.Context) -> bool:
    """
    Whether ``_load_cli_settings`` has configured logging for this process.

    Before that, an ERROR record has no handler but ``logging.lastResort``,
    which would print it -- traceback and all -- straight to stderr, so the
    guard must not log at all (Pitfall 3).

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
    its own ``Configuration error in <file>:`` header (Phase 27 D-10).

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

    Args:
        ctx: The group's context.
        exc: The failure to log.

    """
    if _logging_ready(ctx):
        logger.error("saneless %s failed", ctx.invoked_subcommand, exc_info=exc)


def _report_unexpected(ctx: click.Context, exc: Exception) -> None:
    """
    Report an exception that is not a saneless type as one stderr line (D-06).

    Once logging is configured the traceback goes to the log, and the line
    points at the log file only when the file handler really attached
    (``configure_logging``'s return value): a stderr fallback must not be
    called a log file. Before logging is configured nothing is logged
    (Pitfall 3); ``-v`` prints the traceback to stderr instead, and without it
    the line says how to get one.

    Args:
        ctx: The group's context.
        exc: The exception to report.

    """
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    line = _unexpected_line(exc)
    if _logging_ready(ctx):
        logger.error(
            "Unexpected error in saneless %s", ctx.invoked_subcommand, exc_info=exc
        )
        log_file = obj.get("log_file")
        if log_file:
            line = f"{line}. Full details in {log_file}"
    elif obj.get("verbose"):
        click.echo("".join(traceback.format_exception(exc)), err=True, nl=False)
    else:
        line = f"{line}. Run again with -v to see the traceback"
    click.echo(line, err=True)


class _GuardedGroup(click.Group):
    """
    The CLI group with one last-resort handler around every command (EXC-02).

    Every failure a command raises becomes one stderr message and its D-07
    exit code, and no user ever sees a traceback unless they asked for one
    with ``-v`` (D-06). The ``except`` clauses are ordered, and the order is
    the design:

    1. click's ``Exit``, ``Abort`` and ``ClickException`` are re-raised first.
       ``Exit`` and ``Abort`` subclass ``RuntimeError``, so a later
       ``except Exception`` would turn ``--help`` into exit 5 (Pitfall 2).
    2. ``KeyboardInterrupt`` and ``ScanCancelledError`` are a cancel, not a
       failure: one line and exit 130 (D-03, D-01).
    3. ``StorageError`` -- a job database saneless cannot use -- is a setup
       problem and exits 2 (D-07 amendment). It is mapped here by type and
       sits before the ``SanelessError`` clause because ``ErrorCategory`` is
       persisted on job records, and ``classify_error`` deliberately keeps
       ``StorageError`` ``UNKNOWN`` rather than growing a category for it.
       Its message already names the database path and the reason.
    4. Any other ``SanelessError`` is classified once, by the same
       ``classify_error`` the web worker uses, so the CLI's exit code and the
       job's category cannot disagree.
    5. Anything else is not a saneless type: ``Unexpected error (<Type>)``,
       exit 5, the traceback in the log (M-17).
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
                click.echo(_failure_line(exc, category), err=True)
            ctx.exit(code)
        except Exception as exc:
            _report_unexpected(ctx, exc)
            ctx.exit(ExitCode.UNEXPECTED)


@click.group(cls=_GuardedGroup)
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
    help="Log saneless's own debug detail to the log file and stderr.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
    # Click runs this callback before a subcommand parses its own --help, and
    # ctx.resilient_parsing is False there, so nothing may be loaded here: a
    # broken config would otherwise break `saneless serve --help` (CFG-10).
    # Each command loads through _load_cli_settings instead.
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    ctx.obj["verbose"] = verbose


def _load_cli_settings(ctx: click.Context) -> Settings:
    """
    Load and validate settings and configure logging, once per process.

    Called by every command on first need rather than by the group callback,
    so ``--help`` never touches the configuration (CFG-10). Nothing is caught
    here: every failure reaches the group guard. Loading and directory
    validation raise ``ConfigError`` for every problem with the file --
    including a TOML syntax error (D-12) -- which the guard prints as rendered
    and exits 2; anything else is an unexpected error, exit 5 (D-06). An
    unwritable ``log_file`` is not a failure: ``configure_logging`` warns on
    stderr, logs there instead, and the command runs. ``load_settings`` and
    ``configure_logging`` are called by their module-global names, which is
    where the tests patch them.

    Once logging is configured, ``ctx.obj`` records it (``logging_configured``)
    and records ``log_file`` only if the file handler really attached, so the
    guard logs failures and names the log file truthfully.

    Args:
        ctx: The command's context; its ``obj`` carries ``config_path`` and
            ``verbose`` from the group, and caches the loaded settings.

    Returns:
        The loaded settings, the same object on every call.

    """
    cached = ctx.obj.get("settings")
    if isinstance(cached, Settings):
        return cached

    settings = load_settings(ctx.obj.get("config_path"))
    validate_settings_dirs(settings)
    attached = configure_logging(
        settings.output.log_file,
        settings.output.log_level,
        settings.output.log_max_bytes,
        settings.output.log_backup_count,
        verbose=bool(ctx.obj.get("verbose")),
    )
    ctx.obj["logging_configured"] = True
    ctx.obj["log_file"] = settings.output.log_file if attached else None

    # WR-05: emitted only now, once the log file handler exists to receive it.
    warn_on_legacy_duplex_sources(settings)
    # CFG-11: which file and which environment keys, names only, once.
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
    # config or touching the device, exit 2 through the guard (D-05). --help
    # never reaches this body, so it needs no python-sane (CFG-10).
    require_sane()
    settings = _load_cli_settings(ctx)

    if profile not in settings.profiles:
        click.echo(f"Unknown profile: {profile}", err=True)
        ctx.exit(ExitCode.CONFIG)

    # D-16: the one title rule the web form shares -- typed, else the profile's
    # title, else "Scan <time>"; blank after stripping counts as not typed.
    now = datetime.now(tz=UTC)
    resolved_title = resolve_job_title(title, settings.profiles[profile], now=now)

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
        # the D-07 code; the client is closed either way.
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
    range-reporting device (N-01): it reads as "this scanner offers none",
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
    # config or touching the device, exit 2 through the guard (D-05). --help
    # never reaches this body, so it needs no python-sane (CFG-10).
    require_sane()
    _settings = _load_cli_settings(ctx)

    if not as_json:
        click.echo("Discovering scanners...")
    scanner = SaneBackend(host=_settings.scanner.host)
    device_list = scanner.get_devices()

    if not device_list:
        click.echo("No scanners found." if not as_json else "[]")
        return

    if as_json:
        data = [
            {
                "name": d.name,
                "vendor": d.vendor,
                "model": d.model,
                "type": d.device_type,
            }
            for d in device_list
        ]
        click.echo(json.dumps(data, indent=2))
    else:
        # Table header with terminal-aware column widths
        cols = shutil.get_terminal_size((80, 24)).columns
        name_w = max(20, cols - 45)
        vendor_w = 15
        model_w = 20
        header = (
            f"{'Name':<{name_w}} {'Vendor':<{vendor_w}} {'Model':<{model_w}} {'Type'}"
        )
        click.echo(header)
        click.echo("-" * min(len(header), cols))
        for d in device_list:
            click.echo(
                f"{_truncate(d.name, name_w):<{name_w}} "
                f"{_truncate(d.vendor, vendor_w):<{vendor_w}} "
                f"{_truncate(d.model, model_w):<{model_w}} "
                f"{d.device_type}"
            )

    if capabilities:
        click.echo()
        for d in device_list:
            caps = scanner.get_capabilities(d.name)
            click.echo(f"Capabilities for {d.name}:")
            _echo_capabilities(caps)


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--limit", default=20, type=int, help="Maximum jobs to show.")
@click.pass_context
def jobs(ctx: click.Context, *, as_json: bool, limit: int) -> None:
    """List recent scan job history."""
    settings = _load_cli_settings(ctx)
    # sqlite3.connect does not create parent directories, so data_dir must
    # exist before JobStore opens the database. Deliberately not hidden inside
    # the db_path property: a property with a filesystem side effect surprises.
    Path(settings.output.data_dir).mkdir(parents=True, exist_ok=True)
    store = JobStore(db_path=str(settings.output.db_path))
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
            ts_w = 22
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
                    f"{j.created_at.strftime('%Y-%m-%d %H:%M:%S'):<{ts_w}} "
                    f"{_truncate(j.profile, profile_w):<{profile_w}} "
                    f"{_truncate(j.title, title_w):<{title_w}} "
                    f"{state_label(j.state)}"
                )
    finally:
        store.close()


@cli.command()
@click.option("--host", default=None, help="Bind address.")
@click.option("--port", default=None, type=int, help="Bind port.")
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the web server."""
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard (D-05). --help
    # never reaches this body, so it needs no python-sane (CFG-10).
    require_sane()
    settings = _load_cli_settings(ctx)
    actual_host = host or settings.output.web_host
    actual_port = port or settings.output.web_port

    scanner = SaneBackend(host=settings.scanner.host)
    app = create_app(settings, scanner)

    # Check port availability before starting to give a clear error. A port
    # that cannot be bound is a setup problem -- "can't start, fix your
    # setup" -- so it is a ConfigError, one line and exit 2 through the guard
    # (D-07 amendment), not exit 1, which means a scan error.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((actual_host, actual_port))
    except OSError as e:
        msg = f"Cannot bind to {actual_host}:{actual_port}: {describe(e)}"
        raise ConfigError(msg) from e
    finally:
        sock.close()

    click.echo(f"Serving on http://{actual_host}:{actual_port}")

    # uvicorn follows the configured log_level, not -v: -v is saneless's own
    # detail and must not turn on uvicorn's or httpx's debug output
    # (orchestrator resolution 5). The validated LogLevel Literal lower-cases
    # to a name uvicorn accepts.
    #
    # uvicorn exits 3 on a startup failure, which would read as "Paperless
    # error" in saneless's table; every command shares one table, so a failure
    # status becomes a ConfigError, exit 2 (D-07 amendment). A clean SystemExit
    # passes through unchanged. Ctrl-C needs no handling: uvicorn.run swallows
    # KeyboardInterrupt and returns (measured), so a normal stop exits 0 (D-03).
    try:
        uvicorn.run(
            app,
            host=actual_host,
            port=actual_port,
            log_config=None,
            log_level=settings.output.log_level.lower(),
            access_log=True,
        )
    except SystemExit as exc:
        if exc.code is None or exc.code == 0:
            raise
        msg = (
            f"The web server could not start on {actual_host}:{actual_port} "
            f"(uvicorn exit status {exc.code}); the cause is in the log"
        )
        raise ConfigError(msg) from exc


def _echo_write_result(
    result: ProfileWriteResult, profiles: dict[str, ProfileConfig]
) -> None:
    """
    Print what ``auto-profiles`` did to the config file, grouped by action.

    The group lines come from ``ProfileWriteResult.groups``, the same
    vocabulary the worker's startup log uses (D-04). Added and Refreshed
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
    # config or touching the device, exit 2 through the guard (D-05). --help
    # never reaches this body, so it needs no python-sane (CFG-10).
    require_sane()
    settings = _load_cli_settings(ctx)

    scanner = SaneBackend(host=settings.scanner.host)
    device_list = scanner.get_devices()
    if not device_list:
        click.echo("No scanners found.", err=True)
        ctx.exit(ExitCode.SCAN)

    # Use configured device or first discovered device
    device_id = settings.scanner.device or device_list[0].name
    caps = scanner.get_capabilities(device_id)
    profiles = generate_profiles(caps)

    # The file that was loaded (including an explicit --config). With no loaded
    # file the target stays ./saneless.toml, and the output names the resolved
    # absolute path so the operator sees where it went (orchestrator
    # resolution 2).
    config_path = settings.config_path or Path("./saneless.toml")
    # A ConfigError here (D-08: a single-file bind mount, a non-UTF-8 file, or
    # merged text that would not parse) names the file and the fix; the group
    # guard prints it as-is and exits 2, with no traceback.
    try:
        result = write_profiles_to_config(config_path, profiles, force=force)
    except OSError as exc:
        click.echo(f"Cannot write {config_path}: {exc.strerror or exc}", err=True)
        ctx.exit(ExitCode.CONFIG)
    _echo_write_result(result, profiles)
