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
from pathlib import Path
from typing import TYPE_CHECKING

import click
import uvicorn

from .auto_profiles import (
    generate_profiles,
    resolve_config_path,
    write_profiles_to_config,
)
from .config import load_settings, validate_settings_dirs
from .exceptions import PaperlessError, ScanError
from .job import JobStore
from .logging_config import configure_logging
from .paperless import PaperlessClient
from .pipeline import FlipCoordinator, PipelineEvent, PipelineRequest, run_pipeline
from .scanner.sane_backend import SaneBackend
from .vocabulary import FlipOutcome, JobState, progress_label, state_label
from .web.app import create_app

if TYPE_CHECKING:
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
    while the calling thread waits on an event for at most ``timeout`` seconds.
    Whichever of the operator's answer or the clock claims the single answer
    first is the answer -- the same claim-once shape as the web worker's
    coordinator, so an answer that lands as the wait expires is honoured rather
    than overwritten.

    A yes is ``CONTINUED``; a no is ``ABORTED``; and ``click.Abort`` (EOF at the
    prompt) is also ``ABORTED``, so giving up at the terminal and clicking Abort
    in the web UI end the job the same way.

    Accepted cost, deliberate and not a leak: after a timeout the prompt thread
    is abandoned.  It keeps its read on stdin until the process exits, and its
    prompt may be left sitting on the terminal.  That is bounded -- the job has
    already failed and the CLI is on its way out -- and a daemon thread parked
    on stdin was measured not to delay interpreter shutdown.  It must stay a
    daemon thread: a non-daemon one would hold the interpreter open at exit
    waiting for an answer nobody is going to give.
    """

    def __init__(self) -> None:
        """Start unanswered."""
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._outcome: FlipOutcome | None = None

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
        # One path for both endings.  If the prompt answered, _resolve finds
        # that answer already claimed and hands it back; if the wait expired,
        # TIMED_OUT is offered and wins unless the answer beat it after all.
        self._event.wait(timeout)
        return self._resolve(FlipOutcome.TIMED_OUT)

    def _prompt(self) -> None:
        """Ask the operator on the prompt thread and claim their answer."""
        # No bare ``except Exception`` here: anything unexpected leaves the
        # event unset, and the bounded wait turns that into TIMED_OUT, which is
        # the right fallback for a prompt that never produced an answer.
        try:
            flipped = click.confirm(_FLIP_PROMPT, default=True)
        except click.Abort:
            self._resolve(FlipOutcome.ABORTED)
            return
        self._resolve(FlipOutcome.CONTINUED if flipped else FlipOutcome.ABORTED)

    def _resolve(self, outcome: FlipOutcome) -> FlipOutcome:
        """
        Claim the single answer, unless one is already claimed.

        Returning the answer in effect, rather than asserting one exists, is
        what narrows ``FlipOutcome | None`` without an ``assert`` -- which
        ``S101`` bans in ``src/``.

        Args:
            outcome: The answer this caller is offering.

        Returns:
            The claimed answer: ``outcome`` if it was first, otherwise the
            answer that beat it.

        """
        with self._lock:
            if self._outcome is None:
                self._outcome = outcome
            claimed = self._outcome
        self._event.set()
        return claimed


def _truncate(value: str, width: int) -> str:
    """Truncate string to width, appending ellipsis if exceeding limit."""
    if len(value) <= width:
        return value
    return value[: width - 1] + "\u2026"


@click.group()
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
    help="Enable debug output.",
)
@click.pass_context
def cli(ctx: click.Context, config_path: str | None, *, verbose: bool) -> None:
    """Saneless -- SANE scanner to paperless-ngx bridge."""
    ctx.ensure_object(dict)

    try:
        settings = load_settings(config_path)
        validate_settings_dirs(settings)
    except Exception as exc:
        click.echo(f"Configuration error: {exc}", err=True)
        sys.exit(2)

    configure_logging(
        settings.output.log_file,
        settings.output.log_level,
        settings.output.log_max_bytes,
        settings.output.log_backup_count,
        verbose=verbose,
    )

    ctx.obj["settings"] = settings
    ctx.obj["verbose"] = verbose


@cli.command()
@click.option(
    "--profile",
    default="default",
    help="Scan profile name.",
)
@click.option(
    "--title",
    required=True,
    help="Document title.",
)
@click.pass_context
def scan(ctx: click.Context, profile: str, title: str) -> None:
    """Scan a document and upload to paperless-ngx."""
    settings = ctx.obj["settings"]

    if profile not in settings.profiles:
        click.echo(f"Unknown profile: {profile}", err=True)
        sys.exit(2)

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
        sys.exit(2)

    scanner = SaneBackend(host=settings.scanner.host)
    paperless = PaperlessClient(
        settings.paperless.url,
        settings.paperless.token,
        settings.paperless.consume_dir,
    )

    def status_callback(event: PipelineEvent) -> None:
        state = event.job_state
        if event is PipelineEvent.DONE:
            click.echo(f"Done: {title}")
        else:
            click.echo(progress_label(state))

    try:
        request = PipelineRequest(
            profile_name=profile,
            title=title,
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
    except ScanError as exc:
        click.echo(f"Scan error: {exc}", err=True)
        sys.exit(1)
    except PaperlessError as exc:
        click.echo(f"Paperless error: {exc}", err=True)
        sys.exit(3)
    finally:
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
    _settings = ctx.obj["settings"]

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
    settings = ctx.obj["settings"]
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
    settings = ctx.obj["settings"]
    actual_host = host or settings.output.web_host
    actual_port = port or settings.output.web_port

    scanner = SaneBackend(host=settings.scanner.host)
    app = create_app(settings, scanner)

    # Check port availability before starting to give a clear error
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((actual_host, actual_port))
    except OSError as e:
        msg = f"Cannot bind to {actual_host}:{actual_port}: {e}"
        raise click.ClickException(msg) from e
    finally:
        sock.close()

    click.echo(f"Serving on http://{actual_host}:{actual_port}")

    uvicorn.run(
        app,
        host=actual_host,
        port=actual_port,
        log_config=None,
        log_level=settings.output.log_level.lower(),
        access_log=True,
    )


@cli.command(name="auto-profiles")
@click.option("--force", is_flag=True, help="Overwrite existing profiles.")
@click.pass_context
def auto_profiles(ctx: click.Context, *, force: bool) -> None:
    """Generate scan profiles from scanner capabilities."""
    settings = ctx.obj["settings"]
    config_path_str: str | None = (
        ctx.parent.params.get("config_path") if ctx.parent else None
    )

    scanner = SaneBackend(host=settings.scanner.host)
    device_list = scanner.get_devices()
    if not device_list:
        click.echo("No scanners found.", err=True)
        sys.exit(1)

    # Use configured device or first discovered device
    device_id = settings.scanner.device or device_list[0].name
    caps = scanner.get_capabilities(device_id)
    profiles = generate_profiles(caps)

    config_path = resolve_config_path(config_path_str)
    written = write_profiles_to_config(config_path, profiles, force=force)

    if not written:
        click.echo("No new profiles written (use --force to overwrite).")
        return

    click.echo(f"Generated {len(written)} profile(s) in {config_path}:")
    for name in written:
        p = profiles[name]
        click.echo(
            f"  {name}: source={p.source}, resolution={p.resolution}, mode={p.mode}"
        )
