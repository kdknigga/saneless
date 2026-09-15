---
phase: 28-exception-translation
plan: 12
subsystem: cli
tags: [exceptions, exit-codes, sane, uvicorn, click, tdd]
requires:
  - "28-05: saneless.scanner.sane_backend.require_sane"
  - "28-06: PaperlessClient raises PaperlessError for an invalid URL"
  - "28-09: _GuardedGroup maps ConfigError to exit 2, PaperlessError to exit 3"
provides:
  - "require_sane() as the first statement of scan, devices, auto-profiles and serve"
  - "serve: port bind failure and uvicorn startup failure are ConfigError (exit 2)"
affects:
  - "EXC-02 validation rows: python-sane missing exits 2 with hint; serve bind/startup failure exits 2"
tech-stack:
  added: []
  patterns:
    - "Module-global require_sane import in cli.py, patched as saneless.cli.require_sane by _patch_cli"
    - "try/except SystemExit around uvicorn.run: clean codes re-raised, failure codes become ConfigError"
key-files:
  created: []
  modified:
    - src/saneless/cli.py
    - tests/test_cli.py
decisions:
  - "_CLICK_CONTROL_FLOW keeps click.ClickException: usage errors are ClickExceptions and must still be re-raised by the guard, so the plan's 'no ClickException in cli.py' grep is not met; only serve's raise was removed"
  - "The bind message uses describe(e), so an OSError with an empty str still names its class"
  - "New serve tests stub create_app so no JobStore is left open under filterwarnings=error"
metrics:
  duration: "~25 min"
  completed: 2026-09-15
  tasks: 2
  files: 2
requirements: [EXC-02]
---

# Phase 28 Plan 12: require_sane in SANE Commands and serve Exit Codes Summary

`scan`, `devices`, `auto-profiles` and `serve` now call `require_sane()` first. If python-sane is missing, or libsane cannot be loaded, the command prints one line with the import's reason and the libsane-dev / sane-backends-devel install hint, then exits 2. It stops before loading the config or creating the scanner. `serve` now uses the shared exit-code table:

| serve outcome | Exit |
|---|---|
| Port cannot be bound | 2 |
| uvicorn startup failure (its own exit 3) | 2 |
| Normal Ctrl-C stop | 0 |
| Malformed Paperless URL | 3 |

## Tasks

| Task | Name | RED | GREEN | Files |
|---|---|---|---|---|
| 1 | require_sane() first in scan, devices, auto-profiles and serve | 655defa | e68f1bf | src/saneless/cli.py, tests/test_cli.py |
| 2 | serve joins the exit-code table | 95b4c90 | 3274911 | src/saneless/cli.py, tests/test_cli.py |

## What Was Built

### Task 1
- **Import:** cli.py imports `SaneBackend, require_sane` from `.scanner.sane_backend` at module level.
- **Commands:** `require_sane()` is the first statement of the four SANE commands, each with a comment citing D-05 and CFG-10. `jobs` does not call it.
- **`_patch_cli`:** stubs `saneless.cli.require_sane` with a no-op, so CLI tests never import the real python-sane.
- **New `TestRequireSane` class:**
  - `test_python_sane_unavailable_exits_2_before_anything_else`: the 4 commands, each with the ModuleNotFoundError seam and the libsane ImportError seam (8 cases). Each case checks exit 2, exactly one stderr line with the reason and both package names, and no call to `load_settings` or the scanner class.
  - `test_jobs_needs_no_python_sane_on_a_fresh_install`: `data_dir` does not exist beforehand. The command exits 0, prints only the header and separator, and creates the directory.
  - `test_help_runs_without_python_sane`: `--help` on the 4 commands exits 0, the recorder sees no `require_sane` call, and `sys.modules["sane"]` is still None.

### Task 2
- **Bind failure:** raises `ConfigError(f"Cannot bind to {host}:{port}: {describe(e)}")`, which the guard turns into one line and exit 2.
- **`uvicorn.run`:** wrapped in `except SystemExit as exc:`.
  - Code `None` or `0` is re-raised unchanged.
  - Any other code raises `ConfigError("The web server could not start on <host>:<port> (uvicorn exit status N); the cause is in the log")`.
  - The comment explains the collision with exit 3 and that Ctrl-C returns normally (D-03).
- **`_CLICK_CONTROL_FLOW` docstring:** no longer says serve's bind failure is a ClickException.
- **New tests in `TestServeCommand`:**
  - bind failure: exit 2, stderr exactly one line, uvicorn not started, socket closed
  - SystemExit(3): exit 2
  - SystemExit(0) and SystemExit(None): exit 0
  - uvicorn returning normally: exit 0, no "Cancelled"
  - PaperlessError from `create_app`: exit 3

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1885 passed.
- `uv run pytest tests/test_cli.py -k python_sane -q`: 13 passed (at least 8 required).
- `uv run pytest tests/test_cli.py -k "serve or Serve" -q`: 17 passed.
- `ruff check`, `ruff format --check` and `ty check` are clean. `pyrefly check src tests` reports 0 errors and 3 warnings, all from before this plan.
- `uv run prek run --stage pre-push --all-files`: all hooks pass.
- Acceptance greps:
  - `    require_sane()` appears 4 times.
  - The `SaneBackend, require_sane` import line appears once.
  - There is no module-level `import sane` in src.
  - `except SystemExit as exc` appears once.
  - `Cannot bind to` appears once, in a ConfigError message.

## Deviations from Plan

**1. [Rule 1 - would-be regression] The "no ClickException in cli.py" grep is not met.**
- **Found during:** Task 2 GREEN.
- **Issue:** Plan 28-09 added `_CLICK_CONTROL_FLOW = (Exit, Abort, ClickException)`, which the guard re-raises. This plan was written before that change. Taking `ClickException` out would turn every click usage error (`UsageError`, `BadParameter`) into exit 5.
- **Fix:** Removed only serve's `raise click.ClickException`, as the action text says ("Remove the now-unused click.ClickException usage only"). The tuple stays, and its docstring no longer mentions serve. `ClickException` still appears in the tuple and in two docstrings.
- **Commit:** 3274911.

**2. The planned bind test did not exist.** The plan said to rewrite an existing bind test that expected exit 1, but `TestServeCommand` had none. A new test was added instead.

**3. The new serve tests stub `create_app`.** Otherwise the real app's `JobStore` would stay open when bind or startup fails, and `filterwarnings = ["error"]` would turn the ResourceWarning into a failure. For the malformed-URL case, the stub raises the exact `PaperlessError` that 28-06's client produces. The real client is not used because `_patch_cli` already replaces `PaperlessClient`.

## TDD Gate Compliance

Each task has a `test(28-12)` commit followed by a `feat(28-12)` commit (see the Tasks table).
- **Task 1 RED:** 29 of the 35 selected tests failed. The `_patch_cli` stub cannot patch an attribute that does not exist yet, and the new python_sane cases fail for that reason too.
- **Task 2 RED:** the bind test (exit 1) and the SystemExit(3) test (exit 3) failed. The exit-0 and exit-3 cases passed during RED on purpose: they pin behaviour the rewrite had to keep.

## Known Stubs

None.

## Threat Flags

None. T-28-46, T-28-47 and T-28-48 are each covered by a test. T-28-49 is accepted: the error text names only the module or the shared object.

## Self-Check: PASSED

- FOUND: src/saneless/cli.py (`require_sane()` x4, `except SystemExit as exc`)
- FOUND: tests/test_cli.py (`TestRequireSane`, new serve tests)
- FOUND commits: 655defa, e68f1bf, 95b4c90, 3274911
