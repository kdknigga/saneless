"""Saneless -- SANE scanner to paperless-ngx bridge."""

from saneless.config import Settings


def main() -> None:
    """Entry point for the saneless CLI."""
    print("Hello from saneless!")  # noqa: T201


__all__ = ["Settings", "main"]
