"""
Generate scan profiles from the scanner once at start-up, and persist them.

``StartupProfiles`` reads the scanner's capabilities, builds profiles from
them, writes them to the config file the settings were loaded from when there
is one, and reports which profiles to use in memory and where they are stored.
The worker runs it once, before its first job, and applies the result; this
module never touches the worker's state.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from saneless.auto_profiles import (
    device_type_of,
    generate_profiles,
    write_profiles_to_config,
)
from saneless.config import (
    CONFIG_FILENAME,
    absolute_or_as_spelled,
    config_file_state,
    config_search_paths,
    profile_storage_for_loaded,
)
from saneless.exceptions import ConfigError
from saneless.vocabulary import ConfigFileState, ProfileStorage

if TYPE_CHECKING:
    import threading
    from collections.abc import Mapping

    from saneless.auto_profiles import ProfileWriteResult
    from saneless.config import ProfileConfig, Settings
    from saneless.scanner.base import ScannerBackend

__all__ = ["StartupProfiles"]

logger = logging.getLogger(__name__)


def _profiles_after_persist(
    loaded: Mapping[str, ProfileConfig],
    generated: Mapping[str, ProfileConfig],
    result: ProfileWriteResult | None,
) -> dict[str, ProfileConfig]:
    """
    Choose the profiles to use in memory, matching what a restart will load.

    ``write_profiles_to_config`` never touches a same-name profile without
    ``auto_generated = true``, and start-up generation never forces, so a flagged one
    is skipped too.  A file that spells out a bare ``[profiles.default]``
    therefore keeps it.  Swapping the generated ``default`` into memory anyway
    would give this run one ``default`` and every later run another.

    Args:
        loaded: The bare default profile set the settings were loaded with.
        generated: The profiles generated from the scanner.
        result: What the write did to the config file, or ``None`` when
            nothing was persisted and the generated set is for this run only.

    Returns:
        A new dict: the generated set when nothing was persisted, otherwise
        each generated profile that was persisted, with the loaded profile
        kept for every name the write did not persist.

    """
    if result is None:
        return dict(generated)
    persisted = result.persisted
    profiles: dict[str, ProfileConfig] = {}
    for name, profile in generated.items():
        if name in persisted:
            profiles[name] = profile
        elif name in loaded:
            profiles[name] = loaded[name]
    for name, profile in loaded.items():
        profiles.setdefault(name, profile)
    return profiles


class StartupProfiles:
    """
    Generate profiles from the scanner once, and say where they are kept.

    Every dependency is injected: the settings the profiles are generated for
    and written through, the scanner backend they are read from, and the
    scanner gate the read holds.  It holds no worker state; :meth:`run`
    returns the profiles to apply and the storage outcome, and the worker
    records both.

    Args:
        settings: The application settings, read for the configured device,
            the loaded config file and its discovery record.
        scanner: The scanner backend the capabilities are read from.
        scanner_gate: The lock that keeps every other entry into SANE out
            while the capabilities are read.

    """

    def __init__(
        self,
        settings: Settings,
        scanner: ScannerBackend,
        scanner_gate: threading.Lock,
    ) -> None:
        """Store the injected dependencies."""
        self._settings = settings
        self._scanner = scanner
        self._scanner_gate = scanner_gate

    def run(
        self,
        *,
        loaded: Mapping[str, ProfileConfig],
        bare: bool,
    ) -> tuple[dict[str, ProfileConfig] | None, ProfileStorage]:
        """
        Generate profiles from the scanner once.

        The worker runs this on its thread before it takes any job, so the
        server is already answering requests while it works.  A page loaded
        meanwhile may list only ``default`` until it is reloaded; a job
        submitted meanwhile waits in the queue and then runs against the
        generated set.

        It is tried once per start.  A scanner failure is logged with the real
        exception class and the bare default is kept; the cause is never
        guessed.  Restarting saneless, or ``saneless auto-profiles``, retries.

        The profiles are written only to ``settings.config_path``, the file
        these settings were loaded from.  With no loaded file they are used in
        memory for this run (INFO); when the loaded file cannot be written they
        are used in memory too (WARNING).  Nothing is ever written to a path
        worked out afresh here.

        Memory matches what the file will load after a restart.  When the file
        already defines ``default``, the write keeps it, so the loaded
        ``default`` is kept in memory too rather than the generated one.

        Args:
            loaded: The profiles the settings currently hold.
            bare: Whether those profiles are the bare default, read under the
                same lock as ``loaded``.

        Returns:
            The profiles to swap in, or ``None`` when the loaded ones stay,
            and where the profiles in use are stored.

        """
        if not bare:
            # Nothing was generated, so nothing was persisted -- but the
            # profiles in hand came from the loaded file, and the Profiles row
            # must not tell a household member they are in memory and lost on
            # restart when they are in the file they just edited.
            return None, profile_storage_for_loaded(self._settings)
        profiles = self._read()
        if profiles is None:
            # A SANE failure during generation leaves the loaded profiles in
            # place; they are no more in-memory than they were a moment ago.
            return None, profile_storage_for_loaded(self._settings)
        result, storage = self._persist(profiles)
        return _profiles_after_persist(loaded, profiles, result), storage

    def _read(self) -> dict[str, ProfileConfig] | None:
        """
        Ask the scanner for its capabilities and build profiles from them.

        Returns:
            The generated profiles, or ``None`` when no scanner was found or the
            scanner could not be read -- both logged, with the bare default kept.

        """
        try:
            # Gated for the same reason _scan_job is, and it is a real second
            # entry into SANE rather than a precaution.  get_devices() lists in
            # a short-lived child process that holds the gate for its whole
            # life, so the gate still keeps a probe from overlapping it; and
            # get_capabilities() opens the device in this process and reads its
            # option list.  Held across both, because a probe slipping between
            # them is inside SANE just as surely as one during either.
            #
            # No restart of SANE here, unlike the top of every job: this runs
            # once, before any job, on the SANE the backend's constructor has
            # only just started, so there is no stale connection to clear.
            #
            # No re-entrancy hazard: this runs once, as the worker thread's
            # first act, strictly before any job -- so the gate is never
            # already held by this thread when it arrives here.
            #
            # That same fact makes this the health checks' one known gate
            # contender with no scan anywhere in sight: _current_job_id is
            # still None here, and the lifespan starts the refresher right
            # after the worker, so this window is exactly the cold-start poll's
            # window.  A check that loses this gate has therefore *not* lost it
            # to a scan and must not report one -- which is why checks.py has
            # _scanner_busy() beside _scanner_skipped().
            with self._scanner_gate:
                devices = self._scanner.get_devices()
                if not devices:
                    logger.warning(
                        "Auto-profiles: no scanners found, using bare default"
                    )
                    return None
                device_id = self._settings.scanner.device or devices[0].name
                caps = self._scanner.get_capabilities(device_id)
            return generate_profiles(caps, device_type_of(devices, device_id))
        except Exception as exc:
            # The exception class is named, never interpreted.  The old
            # message blamed the network for every failure, a parse error
            # included, and sent operators after faults that were not there.
            logger.warning(
                "Auto-profiles: could not read scanner capabilities (%s); keeping "
                "the bare default profile. Restart saneless or run "
                "'saneless auto-profiles' to retry",
                type(exc).__name__,
                exc_info=True,
            )
            return None

    def _log_no_config_file(self) -> None:
        """
        Say why the generated profiles were not written, naming the real reason.

        Two situations end up here and they want different sentences.  Usually
        nothing was found and the fix is to create a file, so the message
        lists the places that were looked at.  But a file under the superseded
        name sitting in one of those directories is also "nothing loaded", and
        telling that operator to create a file -- while naming the very
        directory the file they already wrote is in, without mentioning it --
        is how one appliance came to report four symptoms and no cause.

        The state comes from ``config_file_state``, the same derivation the
        startup log, the Configuration row, ``doctor`` and the CLI read, so
        this message cannot come to disagree with them.  Nothing is written
        and the superseded-name file is only named: a rename is the operator's
        to make, and doing it for them would be this process deciding which of
        two files holds the configuration.
        """
        discovery = self._settings.config_discovery
        if (
            discovery is not None
            and config_file_state(self._settings) is ConfigFileState.STALE_ONLY
        ):
            logger.info(
                "Auto-profiles: no config file was loaded, so the generated "
                "profiles are used for this run only and were not written; %s "
                "was ignored because saneless reads %s, not the old name; "
                "rename it to keep them",
                discovery.stale[0].absolute(),
                CONFIG_FILENAME,
            )
            return
        # The recorded search when there is one, so the list is what this
        # process actually looked at rather than what a fresh call would
        # return; settings built directly carry no recording and fall back.
        searched = (
            tuple(absolute_or_as_spelled(path) for path in discovery.searched)
            if discovery is not None
            else config_search_paths()
        )
        logger.info(
            "Auto-profiles: no config file was loaded, so the generated "
            "profiles are used for this run only and were not written; pass "
            "--config or create one of %s to keep them",
            ", ".join(str(path) for path in searched),
        )

    def _persist(
        self, profiles: dict[str, ProfileConfig]
    ) -> tuple[ProfileWriteResult | None, ProfileStorage]:
        """
        Write generated profiles to the loaded config file, if there is one.

        Failure to write is logged, never raised, whatever it raises: the
        profiles are still used in memory for this run.  Success is logged in
        the same group vocabulary the CLI prints.

        Args:
            profiles: The generated profiles.

        Returns:
            What the write did to the file, or ``None`` when nothing was
            persisted because no file was loaded or the write failed; and
            where the profiles are stored as a result.

        """
        config_path = self._settings.config_path
        if config_path is None:
            self._log_no_config_file()
            return None, ProfileStorage.IN_MEMORY_NO_CONFIG_FILE
        try:
            result = write_profiles_to_config(config_path, profiles)
        except (OSError, ConfigError) as exc:
            # The OSError text goes to the server log for the operator, never
            # into an HTTP response.
            logger.warning(
                "Auto-profiles: could not write %s (%s: %s); the generated "
                "profiles are used for this run only and will not survive a "
                "restart",
                config_path,
                type(exc).__name__,
                exc,
            )
            return None, ProfileStorage.IN_MEMORY_UNWRITABLE
        except Exception as exc:
            # Anything else -- a tomlkit container error, say; a parse or UTF-8
            # failure is already a ConfigError -- must not throw away the
            # generated profiles either.  Unexpected, so the traceback is
            # logged too; the exception class is named, never interpreted.
            logger.warning(
                "Auto-profiles: could not write %s (%s); the generated "
                "profiles are used for this run only and will not survive a "
                "restart",
                config_path,
                type(exc).__name__,
                exc_info=True,
            )
            # The same outcome as the branch above, and deliberately so: two
            # causes, one fact.  A file was loaded, and the profiles did not
            # reach it.  The row says that; the log says why.
            return None, ProfileStorage.IN_MEMORY_UNWRITABLE
        logger.info(
            "Auto-profiles: %s: %s",
            result.path,
            "; ".join(result.describe()) or "no changes",
        )
        return result, ProfileStorage.PERSISTED
