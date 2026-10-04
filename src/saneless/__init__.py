"""Saneless -- SANE scanner to paperless-ngx bridge."""


def main() -> None:
    """
    Entry point for the saneless CLI, and through ``serve`` for the web app.

    Process-wide setup that every command needs happens here, before the CLI
    parses anything, so no module has to do it as a side effect of import.
    SIGPIPE is blocked first of all, before any import could start a thread,
    so every thread inherits the mask and a write to a peer that has gone
    raises ``BrokenPipeError``.
    The process-wide teardown happens here too: whatever the command ends
    with, a standard stream that can no longer be written is drained before
    the interpreter's own last flush could change the exit code.
    """
    from .sigpipe import block_sigpipe

    block_sigpipe()

    from .cli import cli, drain_dead_streams
    from .pages import allow_large_scans

    allow_large_scans()
    try:
        cli()
    finally:
        drain_dead_streams()


__all__ = ["main"]
