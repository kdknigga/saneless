---
phase: 28-exception-translation
plan: 11
subsystem: pipeline
tags: [exceptions, cancel, manual-duplex, cli, worker, threading, tdd]
requires:
  - "28-01: ScanCancelledError, describe()"
  - "28-03: _require_pages before the flip match"
  - "28-07: worker records ScanCancelledError as CANCELLED, shutdown first"
  - "28-09: _GuardedGroup maps ScanCancelledError to exit 130, ScanError to exit 1"
provides:
  - "FlipCoordinator.abort_cause: concrete property, None by default"
  - "ClickFlipCoordinator records a broken prompt's exception as abort_cause under the claim lock"
  - "_scan_manual_duplex raises ScanCancelledError for an operator abort, ScanError('Flip prompt failed: ...') for a broken prompt"
affects:
  - "28-13 (docs still describe a flip-prompt abort as exit 1 / ERROR)"
tech-stack:
  added: []
  patterns:
    - "Claim and marker under one lock, read under the same lock (WorkerFlipCoordinator.abort_for_shutdown precedent)"
    - "Why-information travels beside the outcome as a property, not as a new enum member"
key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - src/saneless/cli.py
    - tests/test_pipeline.py
    - tests/test_cli.py
    - tests/test_worker.py
decisions:
  - "abort_cause is concrete on the FlipCoordinator ABC, so WorkerFlipCoordinator and test stubs need no change and FlipOutcome keeps three members (D-02, Phase 25 D-09)"
  - "ClickFlipCoordinator sets abort_cause only when the broken prompt's offer won; a Ctrl-C, EOF or timeout that claimed first keeps its own meaning"
  - "No cli.py or worker.py change was needed for the type mapping: 28-07 and 28-09 already route ScanCancelledError"
metrics:
  duration: "~30 min"
  completed: 2026-09-15
  tasks: 2
  files: 5
requirements: [EXC-04, EXC-02]
---

# Phase 28 Plan 11: Flip-Prompt Abort as a Cancel Summary

An operator's abort at the manual-duplex flip prompt is now a cancellation in every layer. The pipeline raises `ScanCancelledError("Manual duplex scan cancelled at the flip prompt")`. The CLI exits 130 for `n`, Ctrl-D and Ctrl-C, and the web Abort button ends the job CANCELLED. A broken terminal prompt and a flip timeout are still failures: the prompt failure raises `ScanError("Flip prompt failed: <cause>")` chained to the cause and exits 1. A shutdown-claimed abort is still ERROR with `RESTART_REASON`.

## Tasks

| Task | Name | RED | GREEN | Files |
|---|---|---|---|---|
| 1 | abort_cause contract on FlipCoordinator, recorded by ClickFlipCoordinator under the claim lock | 0f11470 | 570be6c | src/saneless/pipeline.py, src/saneless/cli.py, tests/test_pipeline.py, tests/test_cli.py |
| 2 | Flip match raises ScanCancelledError for an operator abort; CLI exits 130, web records CANCELLED | 7300106 | 95e83c9 | src/saneless/pipeline.py, tests/test_pipeline.py, tests/test_cli.py, tests/test_worker.py |

## What Was Built

### Task 1: abort_cause
- **`pipeline.py` `FlipCoordinator.abort_cause`**: a concrete `@property` returning `None`. Its docstring explains that the property is how a coordinator reports an `ABORTED` nobody chose (WR-08), without adding a fourth `FlipOutcome`.
- **`cli.py` `ClickFlipCoordinator`**:
  - `__init__` adds `_cause_lock` and `_abort_cause`.
  - `_prompt`'s `except Exception as exc:` still logs the traceback first. Then, holding `_cause_lock`, it calls `self._slot.offer(FlipOutcome.ABORTED)` and sets `_abort_cause = exc` only if that offer claimed the answer.
  - `abort_cause` reads under the same lock. A calling thread woken by the claim therefore cannot read the cause before it is set.
  - The class docstring and the WR-08 comment were reworded: n, EOF and Ctrl-C are the operator's cancel, and a broken prompt is a failure.
- **Tests**:
  - `TestFlipCoordinatorContract` in test_pipeline.py: the default is `None`, and FlipOutcome still has three members.
  - In `TestManualDuplexPrompt`, the WR-08 parametrised test was renamed to `test_a_broken_prompt_aborts_at_once_with_its_abort_cause_and_traceback` and now also asserts `abort_cause is failure`.
  - New tests show that n and EOF leave `abort_cause` None.
  - A deterministic race test covers Ctrl-C followed by a prompt failure. The prompt stays blocked on an Event until `wait_for_flip` has returned, then raises OSError. The prompt thread is joined, and the test asserts `abort_cause is None` and that the OSError is still logged.

### Task 2: the flip match
- **`pipeline.py`**:
  - Imports `ScanCancelledError` and `describe`.
  - `case FlipOutcome.ABORTED:` reads `flip.coordinator.abort_cause`. With a cause it raises `ScanError(f"Flip prompt failed: {describe(cause)}") from cause`. Without one it raises `ScanCancelledError(msg)`.
  - `TIMED_OUT` is unchanged.
  - The comment above the match now covers three cases: an explicit abort is a cancel, a broken prompt or a timeout is a failure, and a shutdown abort is recorded as a restart by the worker. The "Phase 28's EXC-04" forward reference is gone.
  - The Raises sections of `_scan_manual_duplex` and `run_pipeline` list `ScanCancelledError`.
- **test_pipeline.py**:
  - The abort test is now `test_manual_duplex_abort_at_the_flip_prompt_is_a_cancel`. It expects an exact-match `ScanCancelledError` that is not a `ScanError`, one scan pass and no upload.
  - A new broken-prompt test uses a `_BrokenPromptFlipCoordinator` stub and checks the exact message and `__cause__`.
- **test_cli.py**:
  - n, EOF and Ctrl-C (`saneless.cli.FlipAnswerSlot.wait` raises `KeyboardInterrupt`) each exit 130, and the last stderr line is the cancel line.
  - A broken `click.confirm` exits 1 with `Scan error: Flip prompt failed: [Errno 5] Input/output error`.
  - The unanswered-prompt timeout test now asserts exit code `== 1` instead of `!= 0`.
- **test_worker.py**:
  - `_mock_manual_duplex_pipeline` raises `ScanCancelledError(_CANCEL_MESSAGE)`. `_CANCEL_MESSAGE` moved to the top of the module.
  - A new helper, `_assert_cancelled_at_the_flip_prompt`, checks four things: the job is CANCELLED, the category is None, the error mentions "flip prompt", and there is exactly one `Job <id> cancelled` INFO record with no exc_info.
  - The helper is used by `test_worker_abort_flip`, by `test_abort_at_the_flip_prompt_cancels_the_job_before_pass_b` (real pipeline) and by `test_an_operator_abort_while_stopping_is_not_recorded_as_a_restart`.
  - The shutdown tests and flip timeout tests are unchanged and still pass with ERROR.

## Verification

- `uv run pytest tests/test_pipeline.py tests/test_cli.py tests/test_worker.py -k "abort or cancel or flip or Prompt or shutdown or timeout" -x -q`: 69 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1839 passed
- `uv run ruff check .`, `uv run ruff format --check .` and `uv run ty check` are clean. `uv run pyrefly check src tests` reports 0 errors, with the 3 existing warnings in files this plan did not touch.
- `uv run prek run --stage pre-push --all-files`: all hooks pass
- Selections:
  - `test_cli.py -k abort_cause`: 5 passed
  - `test_worker.py -k abort`: 9 passed
  - `test_cli.py -k Prompt`: 15 passed
- Acceptance checks:
  - `def abort_cause(self) -> Exception | None:` appears once in pipeline.py and once in cli.py.
  - `@abstractmethod` is on `wait_for_flip` only.
  - `self._slot.offer(FlipOutcome.ABORTED)` appears once in cli.py, inside `with self._cause_lock:`.
  - `len(FlipOutcome) == 3`.
  - "Manual duplex scan aborted at the flip prompt" no longer appears in src or tests.
  - `raise ScanCancelledError(msg)` and `Flip prompt failed: ` each appear once.
  - "Phase 28's EXC-04" is gone.

## TDD Gate Compliance

| Task | RED | GREEN |
|---|---|---|
| 1 | 0f11470 `test(28-11)` | 570be6c `feat(28-11)` |
| 2 | 7300106 `test(28-11)` | 95e83c9 `feat(28-11)` |

- **Task 1 RED:** 6 of 7 selected tests failed with AttributeError. `test_abort_cause_adds_no_fourth_flip_outcome` passed on purpose: it guards that FlipOutcome stays at three members.
- **Task 2 RED:** 9 tests failed:
  - both pipeline tests
  - the 4 CLI tests
  - the 3 worker tests that run the real pipeline: pass B, double-click and operator-abort-while-stopping.
- **Passed at Task 2 RED, as expected:** `test_worker_abort_flip`. Its stub pipeline was changed in the same commit to raise `ScanCancelledError`, and the worker already maps that type (28-07). Its RED signal comes from the real-pipeline tests next to it.
- No refactor commits.

## Deviations from Plan

- **Extra worker test updated:** `test_a_double_clicked_abort_cannot_abort_the_next_job` was not in the plan's list. It runs the real pipeline and asserted job 1 ended ERROR, so it now asserts CANCELLED.
- **Ctrl-C CLI test also stubs `click.confirm`:** it uses a blocking `click.confirm` stub that is released in `finally`, which makes the test deterministic. Otherwise the prompt thread would race `click.Abort` against CliRunner's EOF stdin, or would outlive the runner's stream isolation.
- **Tests now take `worker_for` in place of `mock_scanner`/`mock_paperless`:** `test_worker_abort_flip` needs `caplog`, and this swap keeps it within ruff's PLR0913 argument limit without a suppression.
- **Superseded 28-09 grep:** 28-09 checked that `except Exception as exc:` appears only in `_GuardedGroup.invoke`. That check no longer holds, because this plan names the exception in `ClickFlipCoordinator._prompt` to record it.

## Deferred Issues

- The docs still describe a flip-prompt abort as `Scan error: ... aborted at the flip prompt`, exit 1 and job ERROR:
  - `docs/how-to/set-up-adf-duplex.md:95`
  - `docs/how-to/cli-scripting.md:91`
  - `docs/reference/cli-commands.md:36`
  - `docs/reference/web-api.md:161`

  Plan 28-13 owns the documentation (T-28-45), and these files are outside this plan's `files_modified`.

## Threat Model Coverage

- **T-28-42:** a broken prompt records `abort_cause`, and the pipeline raises `ScanError` (exit 1). Pinned at the coordinator, pipeline and CLI levels.
- **T-28-43:** the claim and the cause are set under one lock and read under the same lock. The cause is set only when the offer won. A deterministic race test covers Ctrl-C followed by a prompt failure.
- **T-28-44:** the shutdown tests were re-run and still record ERROR with `RESTART_REASON`.
- **T-28-45:** every cancel exits 130, which is non-zero. Documenting this is left to 28-13.

## Known Stubs

None.

## Threat Flags

None.

## Self-Check: PASSED

- FOUND: src/saneless/pipeline.py (`def abort_cause`, `raise ScanCancelledError(msg)`)
- FOUND: src/saneless/cli.py (`def abort_cause`, `with self._cause_lock:`)
- FOUND commits: 0f11470, 570be6c, 7300106, 95e83c9
