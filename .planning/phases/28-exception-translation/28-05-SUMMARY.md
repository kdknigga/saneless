---
phase: 28-exception-translation
plan: 05
subsystem: scanner
tags: [exceptions, sane, boundary, tdd]
requires:
  - "saneless.exceptions.describe (28-01)"
  - "saneless.exceptions.ConfigError / ScanError / FeederEmptyError"
provides:
  - "saneless.scanner.sane_backend.require_sane"
  - "ScanError translation at every python-sane call site in SaneBackend"
  - "tests.fake_sane failure seams: FakeSaneModule(init_error, open_error, get_devices_error), FakeSaneDev.fail_call/fail_assignment/fail_read"
affects: [28-12]
tech-stack:
  added: []
  patterns:
    - "Module-boundary wrap: catch Exception at the python-sane call, raise ScanError(f'... {device_id}: {describe(exc)}') from exc"
    - "Close failure in a finally is logged with exc_info, never re-raised"
key-files:
  created: []
  modified:
    - src/saneless/scanner/sane_backend.py
    - tests/fake_sane.py
    - tests/test_scanner.py
decisions:
  - "require_sane() is the one python-sane availability check; ConfigError with the import's own reason plus the libsane-dev / sane-backends-devel / Install on Bare Metal hint (D-05)"
  - "get_options() and flatbed start()+snap() translation live in two module helpers, _read_options and _snap_flatbed, so both get_options sites share one message and scan_pages stays within complexity limits"
  - "_configure_device assigns options in a source-first loop of (name, value) pairs with one try per assignment; setattr passes both ty and pyrefly"
  - "New FakeSaneDev seams are methods, not constructor keywords, because __init__ is already at ruff's PLR0913 limit"
metrics:
  duration: 25min
  completed: 2026-09-15
  tasks: 2
  files: 3
requirements: [EXC-01, EXC-02]
---

# Phase 28 Plan 05: SANE Backend Exception Boundary Summary

The SANE backend is now a module boundary. Every python-sane call in `SaneBackend` re-raises `_sane.error`, `RuntimeError` or `AttributeError` as `ScanError`. The message names the device, and the option and `!r` value where there is one. The original exception is kept as `__cause__`. A new `require_sane()` turns a failed python-sane import into a one-line `ConfigError` with an install hint.

## What Was Built

### Task 1: require_sane(), init / open / get_devices / close
- **`require_sane()`** (after `_ensure_sane`, exported in `__all__`):
  - Calls `_ensure_sane()` and catches `ImportError`, which also covers `ModuleNotFoundError`.
  - Raises `ConfigError("python-sane cannot be imported (<reason>). Install the SANE development package (libsane-dev on Debian/Ubuntu, sane-backends-devel on Fedora/RHEL) and reinstall saneless; see Install on Bare Metal in the documentation") from exc`.
  - Nothing calls it at import time.
- **`SaneBackend.__init__`**: calls `require_sane()`. If `sane.init()` fails it raises `ScanError("Could not initialise SANE: <orig>")`.
- **`_open_device`**:
  - A failing `sane.open` raises `ScanError("Could not open scanner <id>: <orig>")`.
  - If `dev.close()` fails, it logs `WARNING "Could not close scanner %s"` with `exc_info=True` and does not re-raise (T-28-16).
- **`get_devices`**: failure raises `ScanError("Could not list scanners: <orig>")`.
- **Fakes**:
  - `FakeSaneModule` gains the keywords `init_error`, `open_error` and `get_devices_error`.
  - `FakeSaneDev.fail_call("close", err)` counts the close call, then raises.

### Task 2: option assignment, read-back, get_options, flatbed start/snap
- **`_configure_device(..., *, has_source_option, device_id)`** (five parameters):
  - Loops over the options in source-first order.
  - A failed assignment raises `ScanError("Could not set <name> to <value!r> on <id>: <orig>")`.
  - A failed resolution read-back raises `Could not read back resolution from <id>: <orig>`.
- **`_read_options(dev, device_id)`**: used by both `get_capabilities` and `scan_pages`. Failure raises `Could not read options from <id>: <orig>`.
- **`_snap_flatbed(dev, device_id)`**: wraps `start()` and `snap()` only.
  - The exact message `"Document feeder out of documents"` raises `FeederEmptyError(_FEEDER_EMPTY_MESSAGE)`.
  - Anything else raises `ScanError("Scanner error on <id>: <orig>")`.
  - The unreadable-page check stays outside the try.
- **`_acquire_pages`**: now uses `describe(exc)`, with the wording otherwise unchanged.
- **Unchanged**: the `_set_geometry` crop fallback, the feeder-empty and all-unreadable raises, and the HARD-0x areas.
- **Fakes**:
  - `FakeSaneDev.fail_call` now also supports `"get_options"` and `"snap"`.
  - New `fail_assignment(option, err)` (checked in `__setattr__`) and `fail_read(option, err)` (checked in `__getattr__`).
  - `get_options` is still not recorded in `calls`, so the existing `_THREE_SHEET_FEEDER_CALLS` assertions still hold.
- **Test update**: `test_sane_backend_cancel_before_close_on_error` now expects `ScanError("Scanner error on test:0: scan failed")` with `__cause__`, and still checks cancel and close.

## Verification

- `uv run pytest tests/test_scanner.py -q`: 238 passed.
- `-k Boundary`: 22 passed. `-k require_sane`: 4 tests.
- `uv run pytest tests/test_pipeline.py tests/test_worker.py -q`: 216 passed.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1745 passed.
- `uv run pytest -m sane_hardware -q`: 5 passed. This uses the SANE `test` backend; no new test opens a device.
- `ruff check`, `ruff format --check`, `ty check` and `pyrefly check src tests` all report 0 errors. The 3 pyrefly warnings existed before this plan.
- Acceptance greps:
  - `def require_sane() -> None:` and `"require_sane"` in `__all__` are present.
  - `except ImportError as exc` appears once.
  - The noqa count is still 2.
  - `Could not open scanner` appears once.
  - `device_id: str` appears in `_configure_device`.
  - `Could not set ` and `Could not read options from` are present.
  - `Scanner error on page {page_num + 1}: {describe(exc)}` is present. Ruff format wrapped it onto its own line inside parentheses.

## Deviations from Plan

1. **[Rule 3 - Blocking] FakeSaneDev seams are methods, not constructor keywords.**
   - **Why:** `FakeSaneDev.__init__` already has ruff's PLR0913 maximum of five arguments, and suppressions are forbidden.
   - **What:** `close_error`, `options_error`, `snap_error` and the per-option failure seam became `fail_call(method, error)`, `fail_assignment(option, error)` and `fail_read(option, error)`. This follows the fake's existing `set_page_delay` and `load_feeder` style.
   - **Commits:** 51961d5, 510fef1.
2. **[Rule 3 - Blocking] The import-free test uses a fresh in-process import, not a subprocess.**
   - **Why:** `subprocess.run([sys.executable, ...])` trips ruff S603.
   - **What:** the test evicts `sane`, `_sane` and the backend from `sys.modules` with monkeypatch, restores the `saneless.scanner.sane_backend` package attribute, re-imports the backend, and asserts `sane` was not imported.
   - **RED note:** this regression guard passed during RED, as expected. Import-freedom already held; the test pins it.
3. **Helper extraction:** the `get_options` and flatbed `start`/`snap` wraps live in `_read_options` and `_snap_flatbed` rather than inline. The messages and behaviour match the plan exactly.

## TDD Gate Compliance

| Task | RED commit | GREEN commit |
|------|------------|--------------|
| 1 | 51961d5 | 5eee410 |
| 2 | 510fef1 | d9b143f |

- Task 1 RED: 10 of 11 new tests failed. The import guard passed, as noted above.
- Task 2 RED: all 12 new or rewritten tests failed.

## Known Stubs

None. `require_sane()` has no CLI caller yet; plan 28-12 wires it.

## Threat Flags

None. The only new surface is the one in the plan's threat model: `!r` option values, the close failure logged rather than raised, and fake-only tests.

## Self-Check: PASSED

- FOUND: src/saneless/scanner/sane_backend.py (require_sane, _read_options, _snap_flatbed)
- FOUND: tests/fake_sane.py (fail_call, fail_assignment, fail_read, module error seams)
- FOUND: tests/test_scanner.py (TestSaneBoundary)
- FOUND commits: 51961d5, 5eee410, 510fef1, d9b143f
