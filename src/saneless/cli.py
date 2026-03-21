"""
Click CLI group with scan and devices commands.

Provides the main command-line interface for saneless, including
scanner discovery and the full scan-to-upload pipeline.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click
import uvicorn

from .config import load_settings
from .exceptions import PaperlessError, ScanError
from .job import JobStore
from .logging_config import configure_logging
from .paperless import PaperlessClient
from .pipeline import PipelineRequest, run_pipeline
from .scanner.sane_backend import SaneBackend
from .web.app import create_app

__all__ = ["cli"]

logger = logging.getLogger(__name__)


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

    scanner = SaneBackend()
    paperless = PaperlessClient(
        settings.paperless.url,
        settings.paperless.token,
        settings.paperless.consume_dir,
    )

    def status_callback(msg: str) -> None:
        click.echo(msg)

    try:
        request = PipelineRequest(
            profile_name=profile,
            title=title,
            tags=settings.profiles[profile].default_tags or None,
            correspondent=settings.profiles[profile].default_correspondent,
            status_callback=status_callback,
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
    help="Show raw SANE options.",
)
@click.pass_context
def devices(ctx: click.Context, *, as_json: bool, capabilities: bool) -> None:
    """List available scanning devices."""
    _settings = ctx.obj["settings"]

    if not as_json:
        click.echo("Discovering scanners...")
    scanner = SaneBackend()
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
        # Table header
        header = f"{'Name':<30} {'Vendor':<15} {'Model':<20} {'Type'}"
        click.echo(header)
        click.echo("-" * len(header))
        for d in device_list:
            click.echo(f"{d.name:<30} {d.vendor:<15} {d.model:<20} {d.device_type}")

    if capabilities:
        click.echo()
        for d in device_list:
            caps = scanner.get_capabilities(d.name)
            click.echo(f"Capabilities for {d.name}:")
            click.echo(f"  Sources: {', '.join(caps.sources)}")
            click.echo(f"  Resolutions: {', '.join(str(r) for r in caps.resolutions)}")
            click.echo(f"  Modes: {', '.join(caps.modes)}")
            if caps.raw_options:
                click.echo("  Raw options:")
                for opt in caps.raw_options:
                    if len(opt) >= 2:
                        click.echo(f"    {opt[1]}")


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--limit", default=20, type=int, help="Maximum jobs to show.")
@click.pass_context
def jobs(ctx: click.Context, *, as_json: bool, limit: int) -> None:
    """List recent scan job history."""
    settings = ctx.obj["settings"]
    db_path = str(Path(settings.output.tmp_dir) / "saneless.db")
    store = JobStore(db_path=db_path)
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
                            "state": j.state.value,
                            "created_at": j.created_at.isoformat(),
                        }
                        for j in recent
                    ],
                    indent=2,
                )
            )
        else:
            header = f"{'Timestamp':<22} {'Profile':<15} {'Title':<30} {'Status'}"
            click.echo(header)
            click.echo("-" * len(header))
            for j in recent:
                click.echo(
                    f"{j.created_at.strftime('%Y-%m-%d %H:%M:%S'):<22} "
                    f"{j.profile:<15} {j.title:<30} {j.state.value}"
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

    scanner = SaneBackend()
    app = create_app(settings, scanner)

    click.echo(f"Serving on http://{actual_host}:{actual_port}")

    uvicorn.run(
        app,
        host=actual_host,
        port=actual_port,
        log_config=None,
        log_level=settings.output.log_level.lower(),
        access_log=True,
    )
