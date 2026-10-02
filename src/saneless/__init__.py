"""Saneless -- SANE scanner to paperless-ngx bridge."""

from saneless.config import Settings


def main() -> None:
    """
    Entry point for the saneless CLI, and through ``serve`` for the web app.

    Process-wide setup that every command needs happens here, before the CLI
    parses anything, so no module has to do it as a side effect of import.
    SIGPIPE is blocked first of all, before any import could start a thread,
    so every thread inherits the mask and a write to a peer that has gone
    raises ``BrokenPipeError`` even after libsane has re-armed the signal.
    The C library's thread unwinder is loaded next, before SANE can start a
    backend thread, so no backend thread is ever the one to load it.
    The process-wide teardown happens here too: whatever the command ends
    with, a standard stream that can no longer be written is drained before
    the interpreter's own last flush could change the exit code.
    """
    from .sigpipe import block_sigpipe
    from .thread_unwinder import load_thread_unwinder

    block_sigpipe()
    load_thread_unwinder()

    from .cli import cli, drain_dead_streams
    from .pages import allow_large_scans

    allow_large_scans()
    try:
        cli()
    finally:
        drain_dead_streams()


__all__ = ["Settings", "main"]
