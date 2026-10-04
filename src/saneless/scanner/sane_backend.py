"""
The parent's proxy to the scanner library, which this process never loads.

``SaneBackend`` imports no python-sane and makes no SANE call here:

- Scanners are listed, opened for the health check and asked for their
  options in a short-lived listing child.  See
  docs/explanation/decisions/0002-listing-in-a-child-process.md.
- A scan runs in a scan child (``scan_child``): one child per scan session,
  restarted between passes when the job asks, and reaped before the call
  that started it returns or raises.

So a backend that hangs or crashes takes only its child with it.  What a
child does with the device -- reading its options, choosing the source,
configuring, framing and reading the scan -- lives in the child-side modules.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import math
from typing import TYPE_CHECKING, Final

from saneless.exceptions import (
    ConfigError,
    ListingNoAnswerError,
    ScanError,
    describe_text,
)
from saneless.scanner import scan_child
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    DeviceSurvey,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)
from saneless.scanner.listing import (
    ChildError,
    ListingReply,
    ListingRequest,
    run_listing_child,
)
from saneless.scanner.net_hosts import exported_sane_net_hosts
from saneless.scanner.options import _constraint, _is_number
from saneless.scanner.scan_child import ScanChildSession
from saneless.text_safety import neutralise_controls
from saneless.vocabulary import (
    PYTHON_SANE_INSTALL_NEXT_STEP,
    python_sane_missing_message,
    sane_init_failure_message,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Generator

    from saneless.scanner.base import PageSink
    from saneless.scanner.listing import OptionEntry
    from saneless.scanner.scan_child import ChildProcess


def require_sane() -> None:
    """
    Check that python-sane is installed, or fail at once with an install hint.

    The SANE-using CLI commands run this first; it is never called at import,
    so ``--help`` stays free of python-sane.  It loads nothing: importing
    python-sane loads libsane, which belongs in a child process, where a
    misbehaving backend cannot take saneless down with it.  So it only asks
    ``importlib.util.find_spec`` whether ``sane`` and its ``_sane`` extension
    can be found.  A missing ``libsane.so`` is found later, by the first
    child that loads python-sane, and reported with the same words.

    Raises:
        ConfigError: If ``sane`` or ``_sane`` cannot be found.

    """
    for name in ("sane", "_sane"):
        try:
            spec = importlib.util.find_spec(name)
        except ValueError:
            # A module already in sys.modules with no spec is not python-sane.
            spec = None
        if spec is None:
            # The configuration category's own advice cannot know that the
            # fix is an install, and nothing in the config file brings the
            # library back.
            raise ConfigError(
                python_sane_missing_message(f"No module named {name!r}"),
                next_step=PYTHON_SANE_INSTALL_NEXT_STEP,
            )


def _launch_listing(
    request: ListingRequest,
    *,
    configured_host: str,
    abort: threading.Event | None = None,
) -> ListingReply:
    """
    Start one listing child and return its reply.

    This is the one place this module starts a listing child, and it is looked
    up at call time, so the test suite can replace it with an in-process
    stand-in that answers from a fake python-sane module.
    """
    return run_listing_child(request, configured_host=configured_host, abort=abort)


def _launch_scan_child(configured_host: str) -> ChildProcess:
    """
    Start one scan child for a scan session.

    This is the one place this module starts a scan child, and it is looked
    up at call time, so the test suite can replace it with a stand-in that
    runs the child's own code in this process over a fake python-sane module.
    """
    return scan_child.start_scan_child(configured_host)


def _listed_devices(reply: ListingReply) -> tuple[DeviceInfo, ...]:
    """
    Turn the devices a listing child reported into ``DeviceInfo`` objects.

    Args:
        reply: The child's validated reply.

    Returns:
        One ``DeviceInfo`` per device, in the order the child listed them.

    """
    return tuple(
        DeviceInfo(name=name, vendor=vendor, model=model, device_type=device_type)
        for name, vendor, model, device_type in reply.devices
    )


# The exceptions a child reports when python-sane itself could not be loaded,
# as opposed to a SANE that loaded and would not initialise.
_IMPORT_FAILURES: Final = frozenset({"ImportError", "ModuleNotFoundError"})


def _child_reason(error: ChildError) -> str:
    """
    Normalise a child's failure text and escape its control characters.

    libsane's text can repeat what a LAN peer sent, so it is defused before
    it reaches any message.

    Returns:
        The one-line, defused reason.

    """
    return neutralise_controls(describe_text(error.message, error.type_name))


def _start_failure(error: ChildError) -> ConfigError | ScanError:
    """
    Turn a child's report that the scanner library would not start into an error.

    A python-sane the child could not import is the install problem
    ``require_sane`` reports, with the same words; any other failure is SANE
    refusing to initialise.

    Args:
        error: The child's ``init_error``.

    Returns:
        The error to raise.

    """
    reason = _child_reason(error)
    if error.type_name in _IMPORT_FAILURES:
        return ConfigError(
            python_sane_missing_message(reason),
            next_step=PYTHON_SANE_INSTALL_NEXT_STEP,
        )
    return ScanError(sane_init_failure_message(reason))


def _option_tuples(options: tuple[OptionEntry, ...]) -> list[tuple]:
    """
    Rebuild SANE option tuples from a child's option entries.

    Only the name (index 1) and the constraint (index 8) are filled: they are
    all a capability read uses, and they sit where ``_constraint`` looks for
    them.  A word list stays a list and a range a tuple, the shapes
    python-sane gives.

    Args:
        options: The ``(name, kind, values)`` entries the child reported.

    Returns:
        One nine-element tuple per entry, in the device's order.

    """
    rebuilt: list[tuple] = []
    for name, kind, values in options:
        constraint: list | tuple | None
        if kind == "list":
            constraint = list(values)
        elif kind == "range":
            constraint = tuple(values)
        else:
            constraint = None
        rebuilt.append((None, name, None, None, None, None, None, None, constraint))
    return rebuilt


__all__ = ["SaneBackend", "require_sane"]

logger = logging.getLogger(__name__)


class SaneBackend(ScannerBackend):
    """
    Scanner backend over python-sane, which it only ever runs in children.

    This process makes no python-sane call: a scan runs in a scan child, one
    per scan session, and listing, open checks and capability reads each run
    in a short-lived listing child.  A scan's child is reaped before the call
    that started it returns or raises.  See
    docs/explanation/decisions/0016-scan-sessions-in-a-child-process.md.
    """

    def __init__(self, host: str = "") -> None:
        """
        Check that python-sane is installed, and keep the configured host.

        Nothing is loaded and no child is started: the first child does that,
        and reports a scanner library that cannot be loaded or started.  The
        operator is told which scanner host the children will use: the
        configured one, or none of it when ``SANE_NET_HOSTS`` is exported
        with a value, which wins.  This process's environment is not changed.

        Args:
            host: Colon-separated sane-net hosts, the ``scanner.host``
                setting, handed to every child.  A child's ``SANE_NET_HOSTS``
                is derived from it, never copied from this process.

        Raises:
            ConfigError: If python-sane is not installed (``require_sane``).

        """
        require_sane()
        if host:
            # An exported non-empty value wins over the configured host; an
            # exported empty value counts as unset.
            exported = exported_sane_net_hosts()
            if exported:
                logger.info(
                    "SANE_NET_HOSTS already set externally (%s), "
                    "ignoring scanner.host config",
                    exported,
                )
            else:
                logger.info("SANE net host discovery configured: %s", host)
        self._host = host
        self._session: ScanChildSession | None = None

    def _start_scan_child(self) -> ChildProcess:
        """Start one scan child for this backend's configured host."""
        return _launch_scan_child(self._host)

    @contextlib.contextmanager
    def scan_session(
        self,
        *,
        abort: threading.Event | None = None,
        live: threading.Event | None = None,
    ) -> Generator[None]:
        """
        Run every scan inside the block in one scan child.

        The child starts at the first ``scan_pages`` call, serves each later
        one, and is asked to exit -- and killed if it does not -- and reaped
        when the block ends, whatever ends it.

        Args:
            abort: Set by another thread to stop the session part way; the
                child is then cancelled, and killed if it does not stop
                within the grace.
            live: Set while the session's child exists, so a stopping server
                can wait for it to be reaped.

        Yields:
            Nothing; ``scan_pages`` and ``reinitialise`` use the session.

        Raises:
            RuntimeError: If a session is already open on this backend.
            ScanError: If the child had to be killed when the session ended,
                and nothing else was being raised.

        """
        if self._session is not None:
            msg = "A scan session is already open on this backend"
            raise RuntimeError(msg)
        session = ScanChildSession(self._start_scan_child, abort=abort, live=live)
        self._session = session
        try:
            with session:
                yield
        finally:
            self._session = None

    def close(self) -> None:
        """
        End a scan child that a session left running; never raises.

        A session ends its own child, so there is normally nothing to do.
        An exception out of a close would replace the error the operator
        needs to see, so a child that ends badly is logged instead.
        """
        session = self._session
        if session is None:
            return
        self._session = None
        try:
            session.close()
        except Exception as error:
            logger.warning("The scan session did not end cleanly: %s", error)

    def reinitialise(self) -> None:
        """
        Restart SANE in the session's scan child before a later pass.

        After a saned restart the net backend never reconnects its stale
        control connection, so every later open fails until SANE is
        restarted.  With no session, or no child yet, there is nothing to
        restart: the next pass starts a fresh child, whose SANE starts fresh.

        Raises:
            ScanError: If the child did not restart SANE: it failed, stopped
                answering or died.  It is reaped, and the next pass starts a
                fresh one.
            ScanInterrupted: If the session's abort was set.

        """
        session = self._session
        if session is None:
            logger.debug("No scan session is open, so SANE was not restarted")
            return
        session.restart()

    def get_devices(self) -> list[DeviceInfo]:
        """
        List the available scanning devices, in a short-lived child process.

        See docs/explanation/decisions/0002-listing-in-a-child-process.md.

        Returns:
            List of DeviceInfo objects for each discovered device.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ConfigError: If the child could not import python-sane, with the
                install hint.
            ScanError: If SANE would not initialise in the child, or could not
                list the devices, with its message normalised and its control
                characters escaped.

        """
        reply = _launch_listing(ListingRequest(), configured_host=self._host)
        if reply.init_error is not None:
            raise _start_failure(reply.init_error)
        if reply.list_error is not None:
            # libsane's text can repeat what a LAN peer sent.
            reason = describe_text(reply.list_error.message, reply.list_error.type_name)
            list_msg = f"Could not list scanners: {neutralise_controls(reason)}"
            raise ScanError(list_msg)
        return list(_listed_devices(reply))

    def list_and_open(
        self, open_if_unlisted: str, *, abort: threading.Event | None = None
    ) -> DeviceSurvey:
        """
        List the devices and open an unlisted configured one, in one child.

        The Scanner health check's list-then-open, both in one child.  The
        child opens the configured id only when its listing lacks it, then
        closes it at once.  The survey carries exception class names only,
        since a ``net:`` id is a LAN address and SANE's text can repeat it.
        A scanner library that would not start in the child is named in the
        survey's ``start_error`` and logged once with its reason.

        Args:
            open_if_unlisted: The configured device id, or ``""`` when none
                is configured.
            abort: Set by another thread to stop the child part way; the
                launcher then kills and reaps it on this thread.

        Returns:
            What the child's listing and open found.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ListingAbortedError: ``abort`` was set while the child ran.

        """
        reply = _launch_listing(
            ListingRequest(open=open_if_unlisted or None),
            configured_host=self._host,
            abort=abort,
        )
        start_error = None
        if reply.init_error is not None:
            start_error = reply.init_error.type_name
            # Safe to log: a SANE initialisation error is a status string, and
            # an import error names a module; neither names a device.
            logger.warning(
                "The scanner library could not be started: %s",
                _child_reason(reply.init_error),
            )
        return DeviceSurvey(
            devices=_listed_devices(reply),
            list_error=reply.list_error.type_name if reply.list_error else None,
            configured_opened=reply.opened,
            open_error=reply.open_error.type_name if reply.open_error else None,
            start_error=start_error,
        )

    def open_and_close(self, device_id: str) -> None:
        """
        Open a device and close it again, in a short-lived listing child.

        The base class's in-process default would log the device and the
        exception's text, so the open runs in a listing child instead.  A
        listed device is taken as reachable, as the health check takes it.

        Args:
            device_id: SANE device identifier string.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ScanError: If ``device_id`` is empty, in which case no child is
                started, or if the open failed, naming the failure's class
                only.

        """
        if not device_id:
            # An empty id would list only and report success.
            msg = "No scanner was named to open"
            raise ScanError(msg)
        survey = self.list_and_open(device_id)
        if survey.configured_opened is False:
            reason = survey.open_error or "unknown error"
            msg = f"Could not open the scanner ({reason})"
            raise ScanError(msg)

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Query device capabilities and available options, in a listing child.

        The child opens the device, reads its option list and closes it; this
        process records what the device reports for its source, resolution
        and mode options.  See
        docs/explanation/decisions/0002-listing-in-a-child-process.md.

        Args:
            device_id: SANE device identifier string.

        Returns:
            DeviceCapabilities with parsed option information. Resolution
            support is reported in whichever shape the device used -- a word
            list or a range -- with at most one of the two populated and
            neither derived from the other.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ConfigError: If the child could not import python-sane, with the
                install hint.
            ScanError: If SANE would not initialise in the child, or the
                device could not be opened or report its options, naming the
                device; the id and the reason have their control characters
                escaped.

        """
        reply = _launch_listing(
            ListingRequest(capabilities=device_id), configured_host=self._host
        )
        shown = neutralise_controls(device_id)
        if reply.init_error is not None:
            raise _start_failure(reply.init_error)
        if reply.open_error is not None:
            open_msg = (
                f"Could not open scanner {shown}: {_child_reason(reply.open_error)}"
            )
            raise ScanError(open_msg)
        if reply.options_error is not None:
            options_msg = (
                f"Could not read options from {shown}: "
                f"{_child_reason(reply.options_error)}"
            )
            raise ScanError(options_msg)
        if reply.options is None:
            no_options_msg = f"The scanner library returned no options for {shown}"
            raise ListingNoAnswerError(no_options_msg)
        raw_options = _option_tuples(reply.options)
        sources = _constraint(raw_options, "source").values or []
        modes = _constraint(raw_options, "mode").values or []
        resolution = _constraint(raw_options, "resolution")

        return DeviceCapabilities(
            sources=[str(s) for s in sources],
            # Only finite numbers are resolutions: the child passes an odd
            # member on as text, and NaN or infinity as they came.
            resolutions=[
                int(r)
                for r in resolution.values or []
                if _is_number(r) and math.isfinite(r)
            ],
            modes=[str(m) for m in modes],
            # Option 0 is named '' and a group heading None; neither is an
            # option anyone can set.
            option_names=tuple(
                str(opt[1]) for opt in raw_options if len(opt) >= 2 and opt[1]
            ),
            resolution_range=resolution.span,
        )

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Acquire pages in a scan child, handing each one to the sink.

        Inside ``scan_session`` the pass runs in the session's child; outside
        one it runs in a child of its own, reaped before this returns or
        raises.  The child matches the requested source (refusing before
        anything is started when none matches), sets the scan parameters,
        and feeds or snaps the pages, validating and cropping each one.
        Each accepted page is handed to ``sink`` as it arrives and
        acknowledged before the child reads another sheet, so no list of
        images is held.  See
        docs/explanation/decisions/0005-page-sink-contract.md.

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).
            sink: Where each accepted page goes, exactly once per page, after
                validation and cropping.

        Returns:
            A ScanBatch carrying the records the sink returned, the resolution
            the device actually used, how many fed sheets failed their
            integrity checks, the requested source when the device's
            ``Auto`` stood in for it and fed, and the cap the pass stopped at
            when the feeder was still feeding there.  A capped pass keeps its
            pages and is not an error.

        Raises:
            ScanError: If the requested source matches none the device
                offers, or several; if a page times out; if a flatbed scan
                returns a page that fails its integrity checks -- unlike a
                fed sheet, there is no next page to skip to -- if the sink
                could not take a page; or if the child stopped answering,
                died or could not be started.
            FeederEmptyError: If the ADF feeder is empty.
            ConfigError: If the child could not load python-sane.
            ScanInterrupted: If the session's abort was set.

        """
        session = self._session
        if session is not None:
            return session.scan_pass(device_id, settings, sink)
        with ScanChildSession(self._start_scan_child) as one_pass:
            return one_pass.scan_pass(device_id, settings, sink)
