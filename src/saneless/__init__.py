"""Saneless -- SANE scanner to paperless-ngx bridge."""

from saneless.config import Settings


def main() -> None:
    """
    Entry point for the saneless CLI, and through ``serve`` for the web app.

    Process-wide setup that every command needs happens here, before the CLI
    parses anything, so no module has to do it as a side effect of import.
    The process-wide teardown happens here too: whatever the command ends
    with, a standard stream that can no longer be written is drained before
    the interpreter's own last flush could change the exit code.
    """
    from .cli import cli, drain_dead_streams
    from .pages import allow_large_scans

    allow_large_scans()
    try:
        cli()
    finally:
        drain_dead_streams()


__all__ = ["Settings", "main"]
