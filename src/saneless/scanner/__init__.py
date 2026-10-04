"""
Scanner abstraction layer for saneless.

The package re-exports nothing: callers import the backend contract from
``saneless.scanner.base`` and the python-sane implementation from
``saneless.scanner.sane_backend`` by name, so importing this package never
reaches python-sane.
"""

__all__: list[str] = []
