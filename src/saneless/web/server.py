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
# being answered before it cancels them.  uvicorn types the setting
# ``int | None``, so it is an int.
#
# It is sized to the longest single call a request makes to paperless-ngx:
# the connection test and each page of a tag or correspondent list are bounded
# by a 2 s connect and a 5 s read, 7 s in all, so one such call that was under
# way when the stop came ends inside the drain, and the lifespan does not
# close the Paperless client under it.  Not every request fits.  A list
# fetch of more than one page is one such call per page.  A request that
# fetches both lists, as the scan form's lazy load (``GET /api/metadata``)
# does, makes two such calls in a row, and each may first wait as long again
# for another request's fetch of the same list.  A name lookup takes no
# timeout at all.  Any of these can outlast the drain.  uvicorn then
# cancels the request, but its worker thread keeps running, and so keeps the
# process alive, after the lifespan has closed what that thread was using.
#
# An idle server has no request to wait for, so its stop is uvicorn's 0.1 s
# poll and pause and the lifespan's one shared 5 s deadline for the scan worker
# and the check refresher, inside Docker's default 10 s grace period.  A stop
# that has to wait out a request to paperless-ngx can take this drain on top,
# as can a stop while a stopped scan's pages are being preserved; the
# documented 90 s stop grace period covers both.
_GRACEFUL_SHUTDOWN_SECONDS: Final = 8


def _socket_url(sock: socket.socket) -> str:
    """
    Return the URL a bound socket serves, with an IPv6 address bracketed.

    Args:
        sock: A bound socket.

    Returns:
        ``http://<address>:<port>``, bracketed so it pastes into a browser.

    """
    address, port = sock.getsockname()[:2]
    shown = f"[{address}]" if sock.family == socket.AF_INET6 else address
    return f"http://{shown}:{port}"


def stop_the_refresher_early(app: FastAPI) -> None:
    """
    Tell the app's check refresher to stop, as soon as the server is told to.

    The lifespan stops the refresher too, but only after uvicorn's request
    drain.  Told this early instead, the refresher starts no check after the
    one it is in, and a check that cannot be cut short has the drain as well
    as the lifespan's join to end in.  A refresher still running when that
    join ends makes the lifespan leave the job store, the Paperless client and
    the scanner open.

    It runs in a signal handler, on the main thread, and the lifespan stops the
    refresher on that same thread, both at shutdown and after a start-up that
    failed part way.  A handler that took the refresher's locks could land
    while the lifespan's own stop holds one, and wait on it for ever.  So this
    only notes the stop, one attribute store that takes no lock, and the
    refresher acts on it from its own thread.  The lifespan's own stop is then
    a second one, which is harmless.  An app with no services, as in a test,
    is left alone.

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
    # Printed, not logged: serve's log stream is stderr as well, so doing both
    # put the same line there twice, and a log record alone would disappear
    # at log_level WARNING. uvicorn announces no address of its own when it is
    # handed sockets, so this is the only place the address is shown.
    for url in urls:
        click.echo(f"Serving on {url}", err=True)

    # uvicorn follows the configured log_level, not -v: -v is saneless's own
    # detail and must not turn on uvicorn's or httpx2's debug output. The
    # validated log level lower-cases to a name uvicorn accepts.
    #
    # The config needs nothing extra for the streaming mode, and adding
    # anything would break it: a None log config means uvicorn applies no
    # dictConfig and attaches no handlers of its own, so uvicorn.error,
    # uvicorn.access and uvicorn.asgi propagate to saneless's root handlers.
    # Leaving the access log on therefore puts per-request lines, and
    # uvicorn's own startup lines, on the same stream for free.
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

    # A running server stopped with SIGTERM is a normal stop, exit 0, whether
    # or not it is PID 1. uvicorn installs its own handler while it runs, and
    # on the way out restores the handler it found and raises the caught
    # signal again into it. With the default handler that re-raise ends the
    # process by the signal (exit 143 in a shell) everywhere except as PID 1,
    # where the kernel ignores it. With this handler in place the re-raise is
    # one more stop request to a server that has already stopped, and the
    # command returns normally. It also covers a SIGTERM that lands before
    # uvicorn has installed its own: the server stops instead of the signal
    # being lost or ending the process mid-start-up. Like uvicorn's handler
    # in StoppingServer, it tells the check refresher to stop as well. The
    # previous handler is put back however the run ends. As in the CLI's
    # interrupt handlers, a handler installed from outside Python
    # (getsignal returns None) is left alone, since it could not be put back,
    # and only the main thread may install one at all. Unlike there, a
    # SIGTERM the command was started with ignored is replaced for the run:
    # uvicorn replaces it with its own handler while it runs anyway, so a
    # serve started that way still stops on SIGTERM, and this handler makes
    # the moments before and after uvicorn's agree. The ignored disposition
    # is what is put back.
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    owns_sigterm = (
        previous_sigterm is not None
        and threading.current_thread() is threading.main_thread()
    )
    if owns_sigterm:
        signal.signal(signal.SIGTERM, _stop_requested)
    try:
        # On Ctrl-C uvicorn shuts down gracefully and then re-raises the
        # signal it caught, which arrives here as KeyboardInterrupt. A running
        # server being stopped is a normal stop, exit 0, so it is swallowed
        # here; a Ctrl-C before this point -- while settings load or the app
        # is built -- is not uvicorn's to handle and reaches the group guard,
        # exit 130. The Ctrl-C itself reached StoppingServer.handle_exit,
        # which told the check refresher before the drain; by the time the
        # re-raise arrives here the lifespan has already stopped it.
        with contextlib.suppress(KeyboardInterrupt):
            server.run(sockets=sockets)
    except SystemExit:
        # uvicorn exits the process itself when start-up fails, with a code of
        # its own choosing that collides with this CLI's table. A server that
        # never started is this project's "could not start", handled below; a
        # started server exiting is uvicorn's own decision and is left alone.
        if server.started:
            raise
    finally:
        if owns_sigterm and previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
    # Server.run returns quietly when start-up fails, such as the app's
    # lifespan raising; uvicorn has already logged why. Every command shares
    # one exit table, so that is a failure to start: one line, exit 2.
    if not server.started:
        msg = (
            f"The web server could not start on {', '.join(urls)}; "
            "the cause is in the preceding log lines"
        )
        raise ConfigError(msg, next_step=SERVE_NEVER_STARTED_NEXT_STEP)
