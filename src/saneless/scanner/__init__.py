"""
Scanner abstraction layer for saneless.

The package re-exports nothing.  The backend-agnostic interface and its data
types (``ScannerBackend``, ``DeviceInfo``, ``DeviceCapabilities``,
``ScanSettings``, ``ScanBatch``) live in ``saneless.scanner.base``, and the
python-sane implementation lives in ``saneless.scanner.sane_backend``.
Callers import each from its module by name, so importing this package never
reaches python-sane.
"""

__all__: list[str] = []
