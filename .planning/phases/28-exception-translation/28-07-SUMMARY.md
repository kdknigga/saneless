---
phase: 28-exception-translation
plan: 07
subsystem: worker
tags: [exceptions, worker, logging, cancel, tdd]
requires:
  - "28-01: saneless.exceptions.ScanCancelledError, PdfError"
  - "28-01: JobState.CANCELLED, ErrorCategory.ASSEMBLY"
provides:
  - "ScanWorker._scan_job records ScanCancelledError as JobState.CANCELLED (no category, INFO, no exc_info)"
  - "Every non-cancel, non-shutdown job failure is logged via logger.exception (exc_info carries the raised exception)"
affects:
  - "28-11 (pipeline starts raising ScanCancelledError for a flip abort; the worker is ready for it)"
tech-stack:
  added: []
  patterns:
    - "Three explicit except-block branches (shutdown, cancel, failure) instead of a category-is-None sentinel"
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - tests/test_worker.py
decisions:
  - "The shutdown check stays first in _scan_job: a shutdown-claimed Abort reaches the pipeline as ScanCancelledError too (D-02, WR-06)"
  - "_failure_record is kept, unchanged in behaviour, for _best_effort_fail only; _scan_job no longer calls it"
  - "The failure log call uses a local `kind` so `logger.exception(\"Job %s failed ...\")` stays on one line"
metrics:
  duration: "~20 min"
  completed: 2026-09-15
  tasks: 1
  files: 2
requirements: [EXC-04, EXC-05]
---

# Phase 28 Plan 07: Worker job endings (shutdown, cancel, failure) Summary

The web worker now ends a job in one of three ways, each in its own branch of `_scan_job`'s except block:

1. **Shutdown** (checked first): if shutdown claimed the flip answer, the job is written ERROR with `RESTART_REASON` and logged at INFO as "ended by shutdown".
2. **Cancel**: a `ScanCancelledError` is written CANCELLED with its message and no category, and logged once at INFO with no traceback.
3. **Failure**: anything else is classified and written ERROR with its category, and logged with `logger.exception`, so the record carries the traceback.

## Task

| Task | Name | Commits | Files |
| ---- | ---- | ------- | ----- |
| 1 | Worker records CANCELLED for ScanCancelledError and logs every failure with exc_info | 8f6958c (RED), f0dca43 (GREEN) | src/saneless/worker.py, tests/test_worker.py |

## What changed

- **`src/saneless/worker.py`**
  - Imports `ScanCancelledError` next to `ConfigError`.
  - `_scan_job`'s except block is now `if coordinator is not None and coordinator.aborted_by_shutdown:` / `elif isinstance(exc, ScanCancelledError):` / `else:`. Each branch calls `_finish_or_owe` and then logs. The existing owed-write comment is kept above the branches, plus a note on why the shutdown check comes first. The `if category is None:` sentinel is gone.
  - `_OwedWrite`'s attribute docstring now covers a CANCELLED write. `_failure_record`'s docstring says only `_best_effort_fail` uses it now, and `_scan_job`'s docstring names the cancel and shutdown endings.
  - Degraded counting is unchanged: job endings never call `_record_loop_failure`.
- **`tests/test_worker.py`**: new `TestWorkerJobEndings` class (9 tests) plus helpers `_raising_pipeline` and `_finish_one_job`. `_finish_one_job` stops the worker, which joins the thread, before the test reads caplog, so a log call made after the row write cannot race the assertions.
  - `test_a_cancel_is_recorded_cancelled_without_a_category`: CANCELLED, the message as error, category None.
  - `test_a_cancel_is_logged_once_at_info_without_exc_info`: one `Job <id> cancelled` INFO record with `exc_info is None`, and no ERROR record for the job.
  - `test_a_scan_error_is_logged_with_its_exc_info`: ERROR, SCANNER, one ERROR record whose `exc_info[1] is` the raised instance.
  - `test_every_failure_is_logged_with_its_exc_info[PaperlessError|ConfigError|PdfError|RuntimeError]`: UPLOAD, CONFIG, ASSEMBLY and UNKNOWN respectively, each with `exc_info[1] is` the raised instance.
  - `test_a_shutdown_claimed_cancel_is_still_recorded_as_a_restart`: a manual-duplex stub pipeline calls `abort_for_shutdown()` on the request's `WorkerFlipCoordinator`, then raises `ScanCancelledError`. Expected: ERROR, `RESTART_REASON`, no category, one "ended by shutdown" INFO record and no "cancelled" record.
  - `test_cancelled_jobs_never_degrade_the_worker`: `_DEGRADED_AFTER` cancels in a row all end CANCELLED, the worker stays HEALTHY and the next submit is ACCEPTED.

## Verification

- `uv run pytest tests/test_worker.py -k "cancel or exc_info or shutdown or degraded or abort" -q`: 29 passed
- `uv run pytest tests/test_worker.py -k "cancel or exc_info" -q`: 9 passed (7 or more required)
- `uv run pytest tests/test_worker.py tests/test_app_lifespan.py tests/test_outcomes_e2e.py -q`: 148 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1732 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv run pyrefly check src tests` all report 0 errors. pyrefly's 4 warnings are in pipeline.py, sane_backend.py and fake_sane.py, which this plan did not touch.
- Acceptance greps:
  - `if category is None:`: no match
  - `isinstance(exc, ScanCancelledError)`: 1 match
  - `logger.exception("Job %s failed`: 1 match
  - `_OwedWrite(JobState.CANCELLED`: 1 match

## TDD Gate Compliance

| Gate | Commit |
|------|--------|
| RED `test(28-07)` | 8f6958c |
| GREEN `feat(28-07)` | f0dca43 |

At RED, 8 of the 9 new tests failed: the job was ERROR instead of CANCELLED, and ERROR records had no exc_info. `test_a_shutdown_claimed_cancel_is_still_recorded_as_a_restart` already passed at RED, and this is intended. The old code checked shutdown before anything else, so the test is a regression guard: it keeps that check first now that a cancel branch exists (T-28-26). No refactor commit was needed.

## Deviations from Plan

None. The plan was executed as written, with two small adjustments:

- **Log text matching.** The plan asked for a record "containing `cancelled` and the job id". The tests match on `Job <id> cancelled` instead, because the shutdown line quotes the cancel's own message ("...cancelled at the flip prompt"). A looser match would have wrongly counted the shutdown line as a cancel line.
- **PLR0913 (too many arguments).** The parametrised test takes one `failure` tuple instead of separate `error_type` and `category` arguments. The shutdown test uses `worker_for` together with the same `default_settings` fixture instance, instead of `mock_scanner`, `mock_paperless` and `default_settings`. This keeps both tests within ruff's five-argument limit without a suppression.

## Threat Flags

None. T-28-25, T-28-26 and T-28-28 are each mitigated and pinned by a test. T-28-27 (tracebacks in the log) is accepted, as the plan states.

## Known Stubs

None. No production code raises `ScanCancelledError` yet; plan 28-11 wires the pipeline's flip abort to raise it. Until then, the abort tests that run through the real pipeline still see ERROR.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py (three branches, `_OwedWrite(JobState.CANCELLED`, `logger.exception("Job %s failed`)
- FOUND: tests/test_worker.py (`TestWorkerJobEndings`)
- FOUND commits: 8f6958c, f0dca43
