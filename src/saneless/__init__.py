"""Saneless -- SANE scanner to paperless-ngx bridge."""

from saneless.config import Settings


def main() -> None:
    """Entry point for the saneless CLI."""
    from .cli import cli

    cli()


__all__ = ["Settings", "main"]
