"""
Run the web app under uvicorn, with a stop that reaches the check refresher.

Only ``serve`` imports this module, from inside its body, so no other command
loads uvicorn.  The server is handed sockets ``serve`` has already bound, and
a SIGTERM or Ctrl-C tells the check refresher to stop before uvicorn starts
draining the requests still being answered.
"""

from __future__ import annotations

import contextlib
import signal
import socket
import threading
from typing import TYPE_CHECKING, Final

import click
import uvicorn

from saneless.exceptions import ConfigError
from saneless.vocabulary import SERVE_NEVER_STARTED_NEXT_STEP
from saneless.web.services import Services

if TYPE_CHECKING:
    from types import FrameType

    from fastapi import FastAPI

__all__ = ["StoppingServer", "run_server", "stop_the_refresher_early"]

# How long, in whole seconds, a stopping web server waits for requests still
# being answered before it cancels them; uvicorn types it ``int | None``.  It
# covers one paperless-ngx call (a 2 s connect and a 5 s read), so the lifespan
# does not close the Paperless client under such a call; a multi-page list, a
# metadata load or a name lookup can still outlast it.  The stop budget is in
# docs/explanation/architecture.md.
_GRACEFUL_SHUTDOWN_SECONDS: Final = 8


def _socket_url(sock: socket.socket) -> str:
    """Return the URL a bound socket serves, with an IPv6 address bracketed."""
    address, port = sock.getsockname()[:2]
    shown = f"[{address}]" if sock.family == socket.AF_INET6 else address
    return f"http://{shown}:{port}"


def stop_the_refresher_early(app: FastAPI) -> None:
    """
    Tell the app's check refresher to stop, as soon as the server is told to.

    The lifespan stops the refresher too, but only after uvicorn's request
    drain; told this early, a check that cannot be cut short has the drain as
    well as the lifespan's join to end in.  A refresher still running after
    that join makes the lifespan leave the job store, the Paperless client and
    the scanner open.

    It runs in a signal handler on the main thread, where the lifespan may
    hold one of the refresher's locks, so it takes no lock: it only notes the
    stop, and the refresher acts on it from its own thread.  An app with no
    services, as in a test, is left alone.

    Args:
        app: The app being served.

    """
    found = getattr(getattr(app, "state", None), "services", None)
    if isinstance(found, Services):
        found.refresher.note_stop()


class StoppingServer(uvicorn.Server):
    """A uvicorn server that tells the check refresher when it is told to stop."""

    def __init__(self, config: uvicorn.Config, app: FastAPI) -> None:
        """
        Build the server for ``config``, serving ``app``.

        Args:
            config: uvicorn's configuration, built for ``app``.
            app: The app being served, whose refresher a stop is passed to.

        """
        super().__init__(config)
        self._served_app = app

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        """
        Stop the server as uvicorn does, then tell the check refresher.

        uvicorn installs this as its SIGTERM and SIGINT handler while it runs,
        so it is where both a ``kill`` and a Ctrl-C arrive, before uvicorn
        starts waiting for the requests still being answered.

        Args:
            sig: The signal received.
            frame: The frame the signal interrupted.

        """
        super().handle_exit(sig, frame)
        stop_the_refresher_early(self._served_app)


def run_server(app: FastAPI, sockets: list[socket.socket], log_level: str) -> None:
    """
    Announce the bound addresses and run uvicorn on the given sockets.

    Args:
        app: The ASGI app to serve.
        sockets: The bound, listening sockets; the caller closes them.
        log_level: The configured log level, which uvicorn follows.

    Raises:
        ConfigError: The server never started; uvicorn has already logged why.

    """
    urls = [_socket_url(sock) for sock in sockets]
    # Printed, not logged: a log record would disappear at log_level WARNING,
    # and uvicorn announces no address of its own when handed sockets.
    for url in urls:
        click.echo(f"Serving on {url}", err=True)

    # uvicorn follows the configured log_level, not -v, which is saneless's own
    # detail. A None log config attaches no uvicorn handlers, so its records
    # propagate to saneless's root handlers on the same stream.
    server = StoppingServer(
        uvicorn.Config(
            app,
            log_config=None,
            log_level=log_level.lower(),
            access_log=True,
            timeout_graceful_shutdown=_GRACEFUL_SHUTDOWN_SECONDS,
        ),
        app,
    )

    def _stop_requested(_signum: int, _frame: FrameType | None) -> None:
        """Ask the server, and the check refresher, to stop, as uvicorn's does."""
        server.should_exit = True
        stop_the_refresher_early(app)

    # A SIGTERM is a normal stop, exit 0, whether or not this is PID 1. On the
    # way out uvicorn re-raises the caught signal into the handler it found, and
    # with the default handler that would end the process by the signal; this
    # one makes it a no-op, and also stops a server SIGTERMed before uvicorn's
    # handler is in. A handler from outside Python (getsignal returns None) is
    # left alone, but an ignored SIGTERM is replaced for the run, as uvicorn's
    # own handler replaces it anyway.
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    owns_sigterm = (
        previous_sigterm is not None
        and threading.current_thread() is threading.main_thread()
    )
    if owns_sigterm:
        signal.signal(signal.SIGTERM, _stop_requested)
    try:
        # On Ctrl-C uvicorn shuts down gracefully and then re-raises the signal
        # as KeyboardInterrupt; stopping a running server is a normal stop, so
        # it is swallowed. A Ctrl-C before this point reaches the group guard
        # as a cancel.
        with contextlib.suppress(KeyboardInterrupt):
            server.run(sockets=sockets)
    except SystemExit:
        # uvicorn exits the process itself when start-up fails, with a code
        # that collides with this CLI's table; a server that never started is
        # handled below instead.
        if server.started:
            raise
    finally:
        if owns_sigterm and previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    # Server.run also returns quietly when start-up fails, such as the app's
    # lifespan raising; uvicorn has already logged why.
    if not server.started:
        msg = (
            f"The web server could not start on {', '.join(urls)}; "
            "the cause is in the preceding log lines"
        )
        raise ConfigError(msg, next_step=SERVE_NEVER_STARTED_NEXT_STEP)
