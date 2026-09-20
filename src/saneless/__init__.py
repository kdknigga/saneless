"""Saneless -- SANE scanner to paperless-ngx bridge."""

from saneless.config import Settings


def main() -> None:
    """
    Entry point for the saneless CLI, and through ``serve`` for the web app.

    Process-wide setup that every command needs happens here, before the CLI
    parses anything, so no module has to do it as a side effect of import.
    """
    from .cli import cli
    from .pages import allow_large_scans

    allow_large_scans()
    cli()


__all__ = ["Settings", "main"]
