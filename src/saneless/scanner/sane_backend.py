"""
SANE scanner backend implementation wrapping python-sane.

Key safety measures:
- sane.init() runs once behind a module-level guard, and the server
  re-initialises SANE at the start of each job, because after a saned restart
  the net backend's stale control connection fails every later open.  The
  restart is refused while a read is stuck or any handle is open: sane_exit
  closes open handles with a request that can wait forever on a vanished host.
- Scanners are listed in a short-lived child process, never in this one.  See
  docs/explanation/decisions/0002-listing-in-a-child-process.md.
- Device handles are closed by a context manager with cancel+close, except
  while a read is still inside SANE, which allows no other call on the device.
- Every blocking acquisition, fed or flatbed, runs on a daemon thread under
  one per-page timeout.

What is done with the device itself -- reading its options, choosing the
source, configuring and framing the scan -- lives in ``scan_session``; the
per-page timeout is worked out in ``page_budget``.
"""

from __future__ import annotations

import contextlib
import functools
import importlib.util
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    ListingNoAnswerError,
    ScanError,
    describe,
    describe_text,
)
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    DeviceSurvey,
    PassCapReached,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
    classify_source,
)
from saneless.scanner.listing import (
    ChildError,
    ListingReply,
    ListingRequest,
    run_listing_child,
)
from saneless.scanner.net_hosts import (
    SANE_NET_HOSTS,
    effective_sane_net_hosts,
    exported_sane_net_hosts,
)
from saneless.scanner.options import _constraint
from saneless.scanner.page_budget import (
    _PAGE_TIMEOUT_FLOOR_SECONDS,
    _describe_page,
    _page_budget_seconds,
    _page_label,
)
from saneless.scanner.scan_session import (
    _FEEDER_EMPTY_MESSAGE,
    _MAX_ADF_PAGES,
    _MAX_AUTO_FEEDER_PAGES,
    _MIN_PAGE_BYTES,
    _apply_paper_size,
    _as_image,
    _await_backend_threads,
    _configure_device,
    _ensure_sane,
    _native_thread_ids,
    _PageFraming,
    _read_options,
    _read_parameters,
    _refuse_sixteen_bit,
    _resolve_source,
    _route,
    _validate_page_image,
)
from saneless.text_safety import neutralise_controls
from saneless.vocabulary import (
    PYTHON_SANE_INSTALL_NEXT_STEP,
    page_timeout_error,
    python_sane_missing_message,
    sane_init_failure_message,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from PIL import Image

    from saneless.scanner.base import PageRecord, PageSink
    from saneless.scanner.listing import OptionEntry
    from saneless.scanner.scan_session import SaneDevice


def require_sane() -> None:
    """
    Check that python-sane is installed, or fail at once with an install hint.

    The SANE-using CLI commands run this first; it is never called at import,
    so ``--help`` stays free of python-sane.  It loads nothing: importing
    python-sane loads libsane, which belongs in a child process, where a
    misbehaving backend cannot take saneless down with it.  So it only asks
    ``importlib.util.find_spec`` whether ``sane`` and its ``_sane`` extension
    can be found.  A missing ``libsane.so`` is found later, where python-sane
    is first loaded -- a child, or, while scanning still happens in this
    process, building the backend -- and reported with the same words.

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


# How long the timeout path waits for a cancelled read to come back before it
# gives up on the handle.  A cooperative backend or a live saned returns
# within a round trip, and a `net` backend with a dead link returns within no
# grace at all, so a longer wait only delays the operator's error message.
_CANCEL_GRACE_SECONDS: float = 10.0


__all__ = ["SaneBackend", "require_sane", "shutdown"]

logger = logging.getLogger(__name__)


class _Slot:
    """
    The one value a reader thread hands back, or the one it raised.

    The consumer may give up before the producer ever writes.
    """

    __slots__ = ("error", "value")

    def __init__(self) -> None:
        """Start empty: no value written, and nothing raised."""
        self.value: object = None
        self.error: BaseException | None = None


@dataclass
class _Wedge:
    """
    What the module remembers about a reader thread still inside SANE.

    Mutated, never rebound.  ``device`` is a strong reference on purpose:
    ``SaneDev_dealloc`` calls ``sane_close()``, so a collected wedged handle
    would be closed under its read.  ``done`` identifies which acquisition is
    wedged, so a late wake-up cannot clear a wedge a later one recorded.

    The record is written before a timed-out read is cancelled, so an
    interrupt during the grace cannot skip it.  Every access holds
    ``_WEDGE_LOCK``.

    - ``settling`` is True while the worker waits out the grace
      (``_settle_or_wedge``); the worker owns the outcome then, and neither
      other thread closes the handle.  The worker ends it by clearing the
      record or by setting ``settling`` False, which makes a real wedge.
    - ``outstanding`` holds a token for each thread still inside SANE on the
      handle, the reader and the canceller.  Once ``settling`` is False, the
      thread that removes the last token closes the handle: a close while a
      ``net`` cancel is still in flight is as unsafe as one under a read.
    """

    stuck: bool = False
    done: threading.Event | None = None
    device: SaneDevice | None = None
    device_id: str = ""
    page_label: str = ""
    settling: bool = False
    outstanding: set[str] = field(default_factory=set)


# The close-or-wedge decision is a check and a write that must not be split.
_WEDGE_LOCK = threading.Lock()
_WEDGE = _Wedge()

_READER: Final = "reader"
_CANCELLER: Final = "canceller"


@dataclass
class _Init:
    """
    What the module remembers about the current ``sane_init``.

    Mutated, never rebound.  A later construction's host is compared against
    ``host``, and the warning names ``effective``, which differs whenever a
    non-empty ``SANE_NET_HOSTS`` was exported.

    Attributes:
        done: Whether ``sane.init()`` has returned successfully and not yet
            been undone by ``shutdown()``.
        host: The host argument that was in effect at that init.
        effective: The host list SANE's net backend will read, recorded at
            init; empty when there is none.
        written: The value saneless wrote into ``SANE_NET_HOSTS``, or ``None``
            when it wrote nothing.
        previous: What ``SANE_NET_HOSTS`` held before that write: ``None``
            when it was absent, ``""`` when it was exported empty.
        version: Whatever ``sane.init()`` returned, kept for the log line.

    """

    done: bool = False
    host: str = ""
    effective: str = ""
    written: str | None = None
    previous: str | None = None
    version: object = None


# "Look, then initialise" must not be split, or two racing constructions both
# call sane_init.  Not reentrant: shutdown() and _ensure_initialised() are
# called one after the other, never one inside the other.
_INIT_LOCK = threading.Lock()
_INIT = _Init()


@dataclass
class _OpenHandles:
    """
    How many device handles this process has open right now.

    On the net backend ``sane_exit`` closes each open handle with a request
    that waits for saned's reply, measured to outlast 40 seconds on a vanished
    host, so SANE is restarted only when this record holds no handle.  Handles
    are kept by identity, so closing one twice cannot count another as closed.

    It also enforces one cancel per timed-out read: on ``net`` each cancel is
    an unbounded request.  A cancelled handle skips the routine cancel before
    close, and its feeder iterator, whose ``__del__`` calls ``cancel()``, is
    parked until the handle is closed, when python-sane refuses that cancel.

    Attributes:
        handles: The ``id()`` of each handle opened by ``_open_device`` and
            not yet closed.
        cancelled: The ``id()`` of each open handle on which a cancel has
            already been issued.
        parked: The feeder iterator of a cancelled handle, by the handle's
            ``id()``, held until that handle is closed.

    """

    handles: set[int] = field(default_factory=set)
    cancelled: set[int] = field(default_factory=set)
    parked: dict[int, object] = field(default_factory=dict)


# Taken inside _WEDGE_LOCK, never the other way round, so the two cannot
# deadlock.
_HANDLES_LOCK = threading.Lock()
_OPEN_HANDLES = _OpenHandles()


def _handle_opened(dev: SaneDevice) -> None:
    """
    Record a handle ``sane_open`` has just returned.

    Args:
        dev: The handle.

    """
    with _HANDLES_LOCK:
        _OPEN_HANDLES.handles.add(id(dev))


def _handle_closed(dev: SaneDevice) -> None:
    """
    Record a handle as closed, whether or not its close succeeded.

    A parked iterator is dropped here, after the close and outside the lock,
    so its finaliser's cancel meets a closed handle and never reaches SANE.

    Args:
        dev: The handle.

    """
    with _HANDLES_LOCK:
        _OPEN_HANDLES.handles.discard(id(dev))
        _OPEN_HANDLES.cancelled.discard(id(dev))
        parked = _OPEN_HANDLES.parked.pop(id(dev), None)
    del parked


def _note_cancel_issued(dev: SaneDevice) -> None:
    """
    Record that a cancel has gone out on an open handle.

    A handle ``_open_device`` did not open is not recorded, as nothing would
    ever close it and clear the record.

    Args:
        dev: The handle the cancel was sent on.

    """
    with _HANDLES_LOCK:
        if id(dev) in _OPEN_HANDLES.handles:
            _OPEN_HANDLES.cancelled.add(id(dev))


def _cancel_was_issued(dev: SaneDevice) -> bool:
    """
    Report whether a cancel has already gone out on this handle.

    Args:
        dev: The handle.

    Returns:
        True if a cancel was issued on it since it was opened.

    """
    with _HANDLES_LOCK:
        return id(dev) in _OPEN_HANDLES.cancelled


def _park_iterator(dev: SaneDevice, iterator: object) -> bool:
    """
    Hold a cancelled handle's feeder iterator until the handle is closed.

    Dropping it earlier would send a second, unbounded cancel on the open
    handle from the worker thread.
    """
    with _HANDLES_LOCK:
        if id(dev) not in _OPEN_HANDLES.cancelled:
            return False
        _OPEN_HANDLES.parked[id(dev)] = iterator
        return True


def _handles_open() -> int:
    """
    Report how many device handles this process has open.

    Returns:
        The number of handles opened and not yet closed.

    """
    with _HANDLES_LOCK:
        return len(_OPEN_HANDLES.handles)


# Names no device and no host: a net: id is a LAN address.
_HANDLE_OPEN_REFUSAL: Final = (
    "Could not start a scan: a scanner handle from an earlier operation is "
    "still open, and SANE cannot be restarted safely while it is. Restart "
    "saneless if this does not clear."
)

# Thread names, so a stuck reader or cancel shows up in a faulthandler dump.
_READER_THREAD_PREFIX = "sane-read-"
_CANCEL_THREAD_NAME = "sane-cancel"


def _restore_sane_net_hosts() -> None:
    """
    Undo saneless's own ``SANE_NET_HOSTS`` write, and forget it either way.

    The caller holds ``_INIT_LOCK``.  The variable is put back only while it
    still holds the value saneless wrote; a value someone else wrote after
    init is left alone.
    """
    if _INIT.written is not None and os.environ.get(SANE_NET_HOSTS) == _INIT.written:
        if _INIT.previous is None:
            os.environ.pop(SANE_NET_HOSTS, None)
        else:
            os.environ[SANE_NET_HOSTS] = _INIT.previous
    _INIT.written = None
    _INIT.previous = None


def _ensure_initialised(host: str, *, log_level: int = logging.INFO) -> object:
    """
    Initialise SANE unless it already is, whoever asks.

    ``sane_init`` is process-global, so the guard is here, not in
    ``SaneBackend.__init__``: every caller still builds its own backend.

    A later construction naming a different host gets a WARNING: the net
    backend reads ``SANE_NET_HOSTS`` only when SANE initialises, so that host
    is not used until the next initialisation.

    A failed init records nothing, the environment included, so the next
    construction tries again.

    Args:
        host: Colon-separated sane-net hosts from the caller's configuration,
            or the empty string when none was configured.
        log_level: The level of the success lines; the warnings keep theirs.

    Returns:
        Whatever ``sane.init()`` returned for the current initialisation.

    Raises:
        ConfigError: If python-sane cannot be loaded, with the install hint,
            chained to the ``ImportError``.
        ScanError: If ``sane.init()`` fails, chained to the SANE error.

    """
    with _INIT_LOCK:
        if _INIT.done:
            if host and host != _INIT.host:
                logger.warning(
                    "SANE is already initialised with scanner host %s, so the "
                    "host %s configured here is not used for this process's "
                    "own scanner opens until SANE is next initialised. "
                    "Run one saneless per scanner host, or list both hosts "
                    "colon-separated in one configuration",
                    _INIT.effective or "none",
                    host,
                )
            return _INIT.version
        # An exported non-empty value wins over the configured host; an
        # exported empty value counts as unset.
        exported = exported_sane_net_hosts()
        if host and not exported:
            _INIT.previous = os.environ.get(SANE_NET_HOSTS)
            os.environ[SANE_NET_HOSTS] = host
            _INIT.written = host
            logger.log(log_level, "SANE net host discovery configured: %s", host)
        elif host:
            logger.log(
                log_level,
                "SANE_NET_HOSTS already set externally (%s), ignoring scanner.host config",
                exported,
            )
        try:
            sane_module = _ensure_sane()
        except ImportError as exc:
            # A python-sane that is installed but whose libsane will not load:
            # require_sane cannot see this, since it loads nothing.
            _restore_sane_net_hosts()
            raise ConfigError(
                python_sane_missing_message(describe(exc)),
                next_step=PYTHON_SANE_INSTALL_NEXT_STEP,
            ) from exc
        try:
            version = sane_module.init()
        except Exception as exc:
            # python-sane raises _sane.error, RuntimeError or AttributeError,
            # with no shared base, so the boundary catches Exception.
            _restore_sane_net_hosts()
            init_msg = f"Could not initialise SANE: {describe(exc)}"
            raise ScanError(init_msg) from exc
        _INIT.done = True
        _INIT.host = host
        _INIT.effective = effective_sane_net_hosts(host)
        _INIT.version = version
        logger.log(log_level, "SANE initialized, version %s", version)
        return version


def _read_outstanding() -> bool:
    """
    Report whether any thread is still inside a SANE read or its cancel.

    A read being cancelled counts from the moment its cancel is decided on,
    because the wedge is recorded before the cancel fires.

    Returns:
        True if a read or cancel recorded in the wedge has not come back.

    """
    with _WEDGE_LOCK:
        return _WEDGE.stuck


def shutdown(*, log_level: int = logging.INFO) -> None:
    """
    Shut SANE down for this process, or explain why it was not.

    Called at an entry point's shutdown and by
    ``SaneBackend.reinitialise()``; never from an interpreter-exit hook, which
    would run while a daemon reader may still be inside ``sane_read``.
    Idempotent.

    The call is skipped, with a log line, when SANE was never initialised, or
    while a read has not returned: ``sane_exit`` closes every open handle
    holding the GIL, which is close-while-reading on all of them at once.

    A failing ``sane_exit`` is logged and swallowed, and the guard is re-armed
    either way, so a later ``SaneBackend`` initialises afresh.
    """
    with _INIT_LOCK:
        if not _INIT.done:
            logger.debug("SANE was never initialised in this process; nothing to undo")
            return
        if _read_outstanding():
            logger.warning(
                "Leaving SANE initialised: a read has not returned, and "
                "sane_exit() closes every open handle, which SANE forbids "
                "while one is outstanding"
            )
            return
        try:
            _ensure_sane().exit()
        except Exception:
            logger.warning("Could not shut SANE down", exc_info=True)
        _restore_sane_net_hosts()
        _INIT.done = False
        _INIT.host = ""
        _INIT.effective = ""
        _INIT.version = None
        logger.log(log_level, "SANE shut down")


def _cancel_read(dev: SaneDevice, done: threading.Event) -> None:
    """
    Cancel a blocked read, on the thread ``_settle_or_wedge`` starts for it.

    On the ``net`` backend ``sane_cancel`` is a blocking RPC that hangs
    against a saned that stopped answering, so on the worker thread the grace
    would bound nothing.  It is sound off-thread because ``sane_cancel``
    releases the GIL, as ``sane_read`` does.

    A failing cancel is logged and swallowed; the read is already lost.
    Either way the thread gives up its wedge token (``_release_wedge``).
    """
    try:
        dev.cancel()
    except Exception:
        logger.warning("Cancelling the blocked read failed", exc_info=True)
    finally:
        _release_wedge(dev, done, _CANCELLER)


def _clear_wedge() -> None:
    """Forget the wedge record.  The caller holds ``_WEDGE_LOCK``."""
    _WEDGE.stuck = False
    _WEDGE.done = None
    _WEDGE.device = None
    _WEDGE.device_id = ""
    _WEDGE.page_label = ""
    _WEDGE.settling = False
    _WEDGE.outstanding.clear()


def _begin_settle(dev: SaneDevice, done: threading.Event, label: str) -> bool:
    """
    Record the wedge before the cancel fires, unless the read just returned.

    The check and the write are one critical section: the reader takes the
    same lock after setting ``done``, which closes the window in which it
    returns just after the timeout.
    """
    with _WEDGE_LOCK:
        if done.is_set():
            return False
        _WEDGE.stuck = True
        _WEDGE.done = done
        _WEDGE.device = dev
        _WEDGE.page_label = label
        _WEDGE.settling = True
        _WEDGE.outstanding.clear()
        _WEDGE.outstanding.update((_READER, _CANCELLER))
        return True


def _end_settle(
    done: threading.Event,
    canceller: threading.Thread | None,
    *,
    cancel_started: bool,
) -> bool:
    """
    Stop waiting: clear the wedge if nothing is left inside SANE, else keep it.

    The reader counts as finished once ``done`` is set.  The cancel thread
    counts as finished once it has given up its token, or if an interrupt
    stopped it from starting; ``cancel_started`` and ``is_alive()`` cover the
    two halves of an interrupted ``start()``.  True means the device context
    may close the handle as usual.
    """
    with _WEDGE_LOCK:
        if not (_WEDGE.stuck and _WEDGE.done is done):
            # Nothing was recorded: the read returned before the cancel.
            return True
        if done.is_set():
            _WEDGE.outstanding.discard(_READER)
        cancel_running = canceller is not None and canceller.is_alive()
        if not (cancel_started or cancel_running):
            _WEDGE.outstanding.discard(_CANCELLER)
        if not _WEDGE.outstanding:
            _clear_wedge()
            return True
        _WEDGE.settling = False
        return False


def _release_wedge(dev: SaneDevice, done: threading.Event, holder: str) -> None:
    """
    Give up one thread's token, and close the handle if it was the last.

    Only the last one out knows nothing is left inside SANE on the handle,
    which SANE requires before any other operation.  While the worker is
    still ``settling`` it owns the outcome, so nothing closes here.  A close
    failure is logged, never raised, as this runs on a daemon thread.

    Args:
        dev: The handle to release.
        done: The event identifying this acquisition; a thread belonging to
            some earlier, already-forgotten acquisition matches nothing here
            and does nothing.
        holder: Which token to give up: the reader's or the cancel thread's.

    """
    with _WEDGE_LOCK:
        if not (_WEDGE.stuck and _WEDGE.done is done):
            return
        _WEDGE.outstanding.discard(holder)
        if _WEDGE.settling or _WEDGE.outstanding:
            return
        logger.warning(
            "The %s on %s returned at last (%s); closing the handle",
            "read" if holder == _READER else "cancel",
            _WEDGE.device_id or "the scanner",
            _WEDGE.page_label,
        )
        try:
            dev.close()
        except Exception:
            logger.warning("Could not close the released scanner", exc_info=True)
        # Counted as closed either way, or every later SANE restart refuses.
        _handle_closed(dev)
        _clear_wedge()


def _name_wedged_device(dev: SaneDevice, device_id: str) -> bool:
    """
    Record which device is wedged, and report that it is.

    The device context knows the SANE name the acquisition helper lacks, so
    the later refusal can name the device.  Only the device id is recorded,
    never a host string.
    """
    with _WEDGE_LOCK:
        if _WEDGE.stuck and _WEDGE.device is dev:
            _WEDGE.device_id = device_id
            return True
        return False


def _refuse_if_wedged(device_id: str, operation: str) -> None:
    """
    Refuse a new SANE operation while a read is still outstanding.

    Called before anything is opened: ``sane_open`` on a device whose read
    never returned is itself forbidden.  The wedge clears itself when the late
    read returns, hence "if it does not" in the message.

    Raises:
        ScanError: If a reader is still inside SANE.

    """
    with _WEDGE_LOCK:
        if not _WEDGE.stuck:
            return
        wedged_id = _WEDGE.device_id or "the scanner"
        label = _WEDGE.page_label or "an earlier page"
    # Both ids can be LAN-supplied text bound for the terminal and log.
    shown = neutralise_controls(device_id)
    wedged_msg = (
        f"Could not {operation} {shown}: a read on "
        f"{neutralise_controls(wedged_id)} ({label}) "
        f"has not returned, and SANE allows no other operation on a device "
        f"while one is outstanding. The scan will be possible again as soon "
        f"as the scanner releases it. Restart saneless if it does not."
    )
    raise ScanError(wedged_msg)


def _settle_or_wedge(
    dev: SaneDevice, done: threading.Event, grace: float, label: str
) -> bool:
    """
    Run the cancel tail: record the wedge, cancel, wait out the grace, decide.

    The reader and the cancel thread share one grace.  A cancel still in
    flight when it runs out leaves the handle wedged even if the read
    returned: closing under a cancel is no safer than under a read.

    The wedge is recorded first and decided in a ``finally``, so an interrupt
    during the wait still ends it.  A signal landing between the record and
    the ``try`` leaves it settling forever, which refuses later scans but
    never closes a handle under a running call.

    Args:
        dev: The handle the blocked read is inside.
        done: The event identifying this acquisition.
        grace: Seconds to wait, after the cancel is fired, for the read to
            return and the cancel to come back.
        label: The page label, for the log and the later refusal.

    Returns:
        True if the read returned and the cancel, if one was sent, came back,
        so the caller's device context may close the handle normally.  False
        if not, in which case the handle is wedged and nothing may touch it.

    """
    if not _begin_settle(dev, done, label):
        return True
    began = time.monotonic()
    canceller: threading.Thread | None = None
    started = False
    try:
        deadline = began + grace
        canceller = threading.Thread(
            target=_cancel_read,
            args=(dev, done),
            name=_CANCEL_THREAD_NAME,
            daemon=True,
        )
        # Before the cancel goes out, so no later cleanup sends a second one.
        _note_cancel_issued(dev)
        canceller.start()
        started = True
        done.wait(_remaining(deadline))
        canceller.join(_remaining(deadline))
    finally:
        settled = _end_settle(done, canceller, cancel_started=started)
        if not settled:
            logger.critical(
                "%s: the scanner did not respond to the cancel within %.0fs. "
                "The device handle is being left open because SANE is still "
                "inside a call on it; no further scan can run until it returns.",
                label,
                time.monotonic() - began,
            )
    return settled


def _remaining(deadline: float) -> float:
    """
    Report how much of a wait is left.

    Args:
        deadline: The ``time.monotonic()`` reading the wait ends at.

    Returns:
        The seconds left, never negative.

    """
    return max(0.0, deadline - time.monotonic())


def _acquire_with_timeout(
    dev: SaneDevice,
    work: Callable[[], object],
    page_label: str,
    budget: _PageBudget,
) -> Image.Image:
    """
    Run one blocking SANE acquisition under a wall-clock bound.

    Each acquisition gets a fresh ``daemon=True`` thread: a non-daemon or
    pooled thread stuck in a blocking C call is joined at interpreter exit and
    stops the process from exiting at all.

    On timeout, or any exception once the reader may have started, the wedge
    is recorded, the read cancelled and both waited for (``_settle_or_wedge``).
    The late value is discarded unconditionally: measured on real libsane, a
    cancelled ``snap()`` hands back a truncated image rather than raising, and
    it passes ``_validate_page_image``.

    With no reader started there is nothing to settle; a cancel there would
    record a wedge no reader could ever clear.  A failed read waits for the
    backend's own threads before reporting (``_BACKEND_THREAD_EXIT_SECONDS``).

    Args:
        dev: The open handle the work will block inside.
        work: The blocking call, as a no-argument callable.
        page_label: Names the page in the timeout message and the thread.
        budget: How long to wait for the page and, after cancelling it, for
            the read; its description of the page goes in the timeout message.

    Returns:
        The acquired page.

    Raises:
        ScanError: If the page did not arrive within the timeout, naming the
            limit and the page it was for, and the unresponsive cancel as well
            when the read never came back.
        BaseException: Whatever the work raised, re-raised unchanged --
            including ``StopIteration``, by design, because that is the
            feeder-empty signal the caller's ladder is written around.

    """
    done = threading.Event()
    slot = _Slot()

    def read() -> None:
        before = _native_thread_ids()
        try:
            slot.value = work()
        except BaseException as exc:
            # Handed to the waiter: nothing may reach threading.excepthook.
            slot.error = exc
            # Before ``done`` is set, so nothing can cancel this failed read
            # while the backend's own reader is still running.
            _await_backend_threads(before)
        finally:
            done.set()
            _release_wedge(dev, done, _READER)

    # ``read`` holds ``work``, and through it the feeder iterator, until the
    # call returns; ``_acquire_pages`` drops its own reference after a wedge on
    # the strength of that, so the reader must never release it early.
    reader = threading.Thread(
        target=read, name=f"{_READER_THREAD_PREFIX}{page_label}", daemon=True
    )
    started = False
    try:
        reader.start()
        started = True
        finished = done.wait(budget.timeout)
    except BaseException:
        # A started reader is settled exactly as on a timeout, so the handle
        # is never closed under a running read; the exception is re-raised
        # unchanged.
        if started or reader.is_alive():
            _settle_or_wedge(dev, done, budget.grace, page_label)
        raise
    if finished:
        if slot.error is not None:
            raise slot.error
        return _as_image(slot.value)

    returned = _settle_or_wedge(dev, done, budget.grace, page_label)
    raise ScanError(
        page_timeout_error(page_label, budget.timeout, budget.page, returned=returned)
    )


@dataclass(frozen=True)
class _PageBudget:
    """
    How long one page may take, how long its cancel may, and how many a pass may.

    Both acquisition paths take the same record, so one sheet is bounded the
    same way whichever way it was presented; the flatbed path ignores the
    page cap.

    Attributes:
        timeout: Maximum seconds to wait for the sheet.
        grace: Maximum seconds to wait for a cancelled read to return.
        max_pages: The most sheets one feeder pass keeps. The sheet past it is
            fed, discarded and reported, never spooled.
        page: The page the timeout was worked out for, as the operator reads
            it in a timeout message, or ``None`` when it was not worked out
            from a page.

    """

    timeout: float = _PAGE_TIMEOUT_FLOOR_SECONDS
    grace: float = _CANCEL_GRACE_SECONDS
    max_pages: int = _MAX_ADF_PAGES
    page: str | None = None


_DEFAULT_PAGE_BUDGET = _PageBudget()


@dataclass(frozen=True)
class _FeedResult:
    """
    What one feeder pass produced.

    Attributes:
        records: The records the sink returned, in acquisition order.
        rejected: How many fed sheets were skipped for failing their integrity
            checks.
        sheet_not_kept: The number of the sheet fed past the page cap and
            discarded, or ``None`` when the feed ended on its own.

    """

    records: list[PageRecord]
    rejected: int
    sheet_not_kept: int | None


def _acquire_pages(
    dev: SaneDevice,
    sink: PageSink,
    framing: _PageFraming,
    budget: _PageBudget = _DEFAULT_PAGE_BUDGET,
) -> _FeedResult:
    """
    Spool the validated ADF pages, and report what was skipped or not kept.

    Pages flow one at a time to the sink; no list of images exists.

    python-sane's iterator turns exactly one message into ``StopIteration``,
    the only feeder-empty signal.  Every other exception is a real fault (a
    jam, an open cover, a busy device) and is reported as itself; do not infer
    "feeder empty" from a failure on the first page.

    An unreadable page is skipped and counted, but a pass in which every fed
    page was unreadable raises.  A skipped page may break manual-duplex
    parity; the pipeline reports that rather than hiding it.

    Args:
        dev: Open SANE device handle.
        sink: Where each accepted page goes, once, after validation and crop.
        framing: The crop applied to each accepted page and the dpi the sink
            records it at.
        budget: The per-page timeout, the cancel grace, and the most sheets
            the pass keeps.

    Returns:
        The records the sink returned, in acquisition order, how many fed
        sheets were skipped for failing their integrity checks, and the
        number of the sheet fed past the cap when there was one.

    Raises:
        FeederEmptyError: If the feeder produced no pages at all.
        ScanError: If a page times out, the device reports a fault, every
            sheet kept or skipped failed its integrity checks, or the sink
            could not take a page.

    """
    iterator = dev.multi_scan()

    records: list[PageRecord] = []
    page_num = 0
    # Kept apart from the pipeline's blank-page count: a corrupt page was not
    # removed for being blank.
    rejected_pages = 0
    sheet_not_kept: int | None = None
    try:
        while True:
            try:
                page_image = _acquire_with_timeout(
                    dev,
                    functools.partial(next, iterator),
                    _page_label(page_num),
                    budget,
                )
            except StopIteration:
                break
            except ScanError:
                raise
            except Exception as exc:
                scan_error_msg = (
                    f"Scanner error on page {page_num + 1}: {describe(exc)}"
                )
                raise ScanError(scan_error_msg) from exc

            # Detected on the sheet past the cap, since a full hopper only ends
            # when the next probe raises; that fed sheet is discarded uncounted.
            if page_num >= budget.max_pages:
                sheet_not_kept = page_num + 1
                logger.warning(
                    "Stopped the feed at the %d-page cap: sheet %d was fed "
                    "but not kept",
                    budget.max_pages,
                    sheet_not_kept,
                )
                break

            page_num += 1

            if not _validate_page_image(page_image, page_num):
                rejected_pages += 1
                continue

            records.append(sink.add(framing.crop(page_image), dpi=framing.resolution))
    finally:
        # After a cancel, the iterator's finaliser would send a second,
        # unbounded one, so it is parked until the close.  Otherwise dropping
        # it is safe: a wedged reader's callable keeps it alive.
        if not _park_iterator(dev, iterator):
            del iterator

    if page_num == 0:
        raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE)

    # "Unreadable", not "load paper".
    if rejected_pages == page_num:
        all_rejected_msg = (
            f"All {page_num} page(s) fed were unreadable and were skipped "
            f"(zero dimensions, or below {_MIN_PAGE_BYTES} bytes of image "
            f"data); no usable page was produced"
        )
        raise ScanError(all_rejected_msg)

    return _FeedResult(records, rejected_pages, sheet_not_kept)


def _snap_flatbed(
    dev: SaneDevice,
    device_id: str,
    sink: PageSink,
    framing: _PageFraming,
    budget: _PageBudget = _DEFAULT_PAGE_BUDGET,
) -> PageRecord:
    """
    Acquire one flatbed page under the ADF's timeout, validate it, and spool it.

    ``start()`` and ``snap()`` run as one unit under the same page budget a
    fed sheet gets.  The message python-sane's ADF iterator treats as the end
    of the feed maps to ``FeederEmptyError``; every other failure is a
    ``ScanError`` naming the device.

    Raises:
        FeederEmptyError: If SANE reports the feeder out of documents.
        ScanError: If the sheet did not arrive within the timeout, if the page
            fails its integrity checks, or for any other failure, chained to
            the original.

    """

    def start_and_snap() -> Image.Image:
        dev.start()
        # No cancel from snap() on failure: the backend's reader thread may
        # still be running.  The device context cancels once it has ended
        # (``_BACKEND_THREAD_EXIT_SECONDS``).
        return dev.snap(no_cancel=True)

    try:
        image = _acquire_with_timeout(dev, start_and_snap, _page_label(0), budget)
    except ScanError:
        raise
    except Exception as exc:
        if str(exc) == "Document feeder out of documents":
            raise FeederEmptyError(_FEEDER_EMPTY_MESSAGE) from exc
        snap_msg = f"Scanner error on {neutralise_controls(device_id)}: {describe(exc)}"
        raise ScanError(snap_msg) from exc

    # Fatal here rather than skipped: a flatbed has no next page.
    if not _validate_page_image(image, 1):
        unreadable_msg = (
            "The scanner returned an unreadable page (zero dimensions, or "
            f"below {_MIN_PAGE_BYTES} bytes of image data)"
        )
        raise ScanError(unreadable_msg)

    return sink.add(framing.crop(image), dpi=framing.resolution)


class SaneBackend(ScannerBackend):
    """
    Scanner backend wrapping python-sane.

    The first backend built in a process initialises SANE and later ones
    reuse it; the server also restarts SANE at the start of each scan job
    (``reinitialise``).  Scanners are listed, and the health check's open
    made, in a short-lived child process, never in this one.  Handles this
    process opens are cancelled and closed on every path unless a read on
    the handle has not returned.
    """

    def __init__(self, host: str = "") -> None:
        """
        Join this process's SANE, initialising it if nobody has yet.

        Args:
            host: Colon-separated sane-net hosts, applied at every
                initialisation this backend makes (construction and each
                ``reinitialise``) when ``SANE_NET_HOSTS`` is not already set to
                a non-empty value, and handed to every listing child.  A
                differing host arriving while SANE is already initialised is
                reported as not used until the next initialisation.

        Raises:
            ConfigError: If python-sane is not installed (``require_sane``),
                or is installed but cannot be loaded, as with a missing
                ``libsane.so``.
            ScanError: If ``sane.init()`` fails, chained to the SANE error.

        """
        require_sane()
        # A listing child's SANE_NET_HOSTS is derived from this setting, never
        # copied from this process's environment.
        self._host = host
        _ensure_initialised(host)

    def close(self) -> None:
        """
        Shut this process's SANE down, through the backend abstraction.

        Process-level, not per-object: what is released is this process's
        current ``sane_init``.  Never raises, because an exception at shutdown
        would replace the error the operator needs to see.
        """
        shutdown()

    def reinitialise(self) -> None:
        """
        Restart this process's SANE before a scan job or a later pass, or refuse to.

        Called under the scanner gate with no handle open, at the top of each
        scan job and before each later pass of a multi-pass scan.  After a
        saned restart the net backend never reconnects its stale control
        connection, so every later open fails until SANE is restarted.

        It refuses, before any SANE call, while a read has not returned or a
        handle is open: ``sane_exit`` would close it, and on a vanished
        ``net`` host each close waits with the gate held.

        Raises:
            ScanError: If a read has not returned, if a handle is open, or if
                ``sane.init()`` fails (chained to the SANE error; SANE is then
                left uninitialised, so the next job tries again).

        """
        _refuse_if_wedged("the scanner", "start a scan on")
        if _handles_open():
            raise ScanError(_HANDLE_OPEN_REFUSAL)
        shutdown(log_level=logging.DEBUG)
        _ensure_initialised(self._host, log_level=logging.DEBUG)
        logger.info("SANE re-initialised before this scan")

    @contextlib.contextmanager
    def _open_device(self, device_id: str) -> Generator[SaneDevice]:
        """
        Context manager for SANE device lifecycle.

        Cancel and close run on every exit path unless a reader is still
        inside SANE on this handle: ``sane_close`` holds the GIL while
        ``sane_read`` has released it, which python-sane cannot survive.  The
        cancel is also skipped once one was issued (``_OpenHandles``).

        This process never lists before it opens.  That is measured only for
        a ``net:`` device; a discovery backend such as ``escl`` or ``airscan``
        that cannot open an unlisted id would fail here with SANE's error.

        Args:
            device_id: SANE device identifier string.

        Yields:
            An open SANE device handle.

        Raises:
            ScanError: If the device cannot be opened, naming it and chained to
                the SANE error.

        """
        try:
            dev: SaneDevice = _ensure_sane().open(device_id)
        except Exception as exc:
            # The id may be LAN-supplied text; defused where the message is
            # built, so every sink gets the safe spelling.
            open_msg = (
                f"Could not open scanner {neutralise_controls(device_id)}: "
                f"{describe(exc)}"
            )
            raise ScanError(open_msg) from exc
        _handle_opened(dev)
        try:
            yield dev
        finally:
            if _name_wedged_device(dev, device_id):
                # Still counted: the reader thread closes it, and counts it
                # closed, when the late read returns (_release_wedge).
                logger.critical(
                    "Leaving scanner %s open: a read has not returned, so "
                    "neither cancel() nor close() may be issued on it",
                    device_id,
                )
            else:
                if not _cancel_was_issued(dev):
                    with contextlib.suppress(Exception):
                        dev.cancel()
                try:
                    dev.close()
                except Exception:
                    # Never raised: it would replace the error that ended the
                    # scan.
                    logger.warning(
                        "Could not close scanner %s", device_id, exc_info=True
                    )
                finally:
                    # Counted closed either way, or every later SANE restart
                    # refuses.
                    _handle_closed(dev)

    def get_devices(self) -> list[DeviceInfo]:
        """
        List the available scanning devices, in a short-lived child process.

        See docs/explanation/decisions/0002-listing-in-a-child-process.md.
        It still refuses while a read is outstanding, before any child is
        started, to keep one rule for every SANE entry point.

        Returns:
            List of DeviceInfo objects for each discovered device.

        Raises:
            ListingCrashedError: The listing child died from a signal.
            ListingTimedOutError: The listing child did not finish in time.
            ListingNoAnswerError: The listing child gave no usable answer, or
                could not be started.
            ConfigError: If the child could not import python-sane, with the
                install hint.
            ScanError: If a previous read has not returned, in which case no
                child is started; if SANE would not initialise in the child;
                or if SANE could not list the devices, with its message
                normalised and its control characters escaped.

        """
        # The refusal names the wedged device from its own record.
        _refuse_if_wedged("the scanners", "list")
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
            ScanError: If a previous read has not returned, in which case no
                child is started.

        """
        _refuse_if_wedged("the scanners", "list")
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
            ScanError: If ``device_id`` is empty or a previous read has not
                returned, in which case no child is started; or if the open
                failed, naming the failure's class only.

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
            resolutions=[int(r) for r in resolution.values or []],
            modes=[str(m) for m in modes],
            # Option 0 is named '' and a group heading None; neither is an
            # option anyone can set.
            option_names=tuple(
                str(opt[1]) for opt in raw_options if len(opt) >= 2 and opt[1]
            ),
            resolution_range=resolution.span,
        )

    def _scan_adf_pages(
        self,
        dev: SaneDevice,
        sink: PageSink,
        framing: _PageFraming,
        budget: _PageBudget = _DEFAULT_PAGE_BUDGET,
    ) -> _FeedResult:
        """
        Spool the validated ADF pages, with a per-page timeout.

        The timeout is per page, not per job, so a long stack is never cut
        short merely for being long.
        """
        return _acquire_pages(dev, sink, framing, budget)

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Acquire pages from scanner, handing each one to the sink.

        Matches the requested source (refusing before anything is started
        when none matches), sets the scan parameters, and acquires through
        multi_scan() for a feeder pass or snap() for the flatbed.  No progress
        callback is passed to snap(): a bad one segfaults the process.

        Each page is validated, cropped and handed to ``sink`` as it arrives;
        no list of images is held.  See
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
            ScanError: If a previous read has not returned, in which case no
                SANE call is made at all; if the requested source matches
                none the device offers, or several; if a page times out; if a
                flatbed scan returns a page that fails its integrity checks --
                unlike a fed sheet, there is no next page to skip to -- or if
                the sink could not take a page.
            FeederEmptyError: If the ADF feeder is empty.

        """
        _refuse_if_wedged(device_id, "scan from")
        with self._open_device(device_id) as dev:
            # Decisions after configuration use configured.options instead:
            # assigning the source or the mode reloads the descriptors.
            raw_options = _read_options(dev, device_id)

            choice = _resolve_source(
                raw_options,
                settings.source,
                resolve_feeder=settings.duplex == "manual",
            )

            configured = _configure_device(
                dev, settings, choice, options=raw_options, device_id=device_id
            )
            actual_resolution = configured.resolution

            # Before the paper size: whether the pass feeds decides how it
            # can be applied without cutting an edge off the page.
            use_adf = _route(choice, settings)

            framing = _apply_paper_size(
                dev,
                configured.options,
                settings,
                use_adf=use_adf,
                resolution=actual_resolution,
            )

            parameters = _read_parameters(dev, device_id)
            _refuse_sixteen_bit(parameters, device_id)

            # A source not named as a feeder (Auto sent through the feeder)
            # may be a platen rescanned forever, so it gets the lower cap.
            named_feeder = classify_source(choice.effective).uses_feeder
            budget = _PageBudget(
                timeout=_page_budget_seconds(parameters, actual_resolution),
                max_pages=_MAX_ADF_PAGES if named_feeder else _MAX_AUTO_FEEDER_PAGES,
                page=_describe_page(parameters, actual_resolution),
            )

            cap_reached: PassCapReached | None = None
            if use_adf:
                fed = self._scan_adf_pages(dev, sink, framing, budget)
                records, pages_rejected = fed.records, fed.rejected
                if fed.sheet_not_kept is not None:
                    cap_reached = PassCapReached(
                        cap=budget.max_pages,
                        sheet_not_kept=fed.sheet_not_kept,
                        auto_source=not named_feeder,
                    )
            else:
                records = [_snap_flatbed(dev, device_id, sink, framing, budget)]
                # Nothing was skipped: an unreadable sheet raised in there.
                pages_rejected = 0

        # Built after the device context has closed the handle.  An Auto that
        # stood in for a flatbed is reported only when it fed.
        return ScanBatch(
            pages=tuple(records),
            actual_resolution=actual_resolution,
            pages_rejected=pages_rejected,
            substituted_source=choice.substituted_from if use_adf else None,
            cap_reached=cap_reached,
        )
