---
phase: 25-manual-duplex
plan: 14
subsystem: pipeline, worker, cli
tags: [manual-duplex, concurrency, flip-coordinator, gap-closure, IN-01, IN-02, WR-08]
gap_closure: true
requires:
  - 25-10 (WorkerFlipCoordinator arming, bool-returning signals, CR-01 race tests)
  - 25-12 (cli.py and CLI flip timeout test final shapes)
provides:
  - "pipeline.FlipAnswerSlot: offer(outcome) -> bool, settle(outcome) -> FlipOutcome, wait(timeout) -> None, answer property; exported in __all__"
  - "WorkerFlipCoordinator composes FlipAnswerSlot (self._slot); arming is a one-way threading.Event latch local to it"
  - "ClickFlipCoordinator composes FlipAnswerSlot (self._slot); _prompt settles ABORTED and logs a traceback on any unexpected exception"
affects:
  - any future fix to flip answer claiming lands in FlipAnswerSlot only
tech-stack:
  added: []
  patterns:
    - "Claim-once answer as a concrete composed helper, not a second ABC (D-09 unchanged)"
    - "Coordinator-specific gating (web arming) is checked before offering to the shared slot"
    - "Prompt thread logs before it settles, so the log record exists by the time the waiter wakes"
key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - src/saneless/worker.py
    - src/saneless/cli.py
    - tests/test_pipeline.py
    - tests/test_worker.py
    - tests/test_cli.py
decisions:
  - "WR-08: an unexpected CLI prompt failure resolves FlipOutcome.ABORTED (not TIMED_OUT, not a fourth outcome), logged via logger.exception"
  - "IN-02: FlipAnswerSlot is the single claim-once implementation; neither coordinator owns a lock/event/outcome for the answer any more"
  - "WorkerFlipCoordinator arming became a threading.Event used as a one-way latch: a signal that sees it unset is dropped, one that sees it set offers to the slot"
requirements: [DPLX-04, DPLX-05]
metrics:
  duration: "~25 min"
  completed: 2026-09-14
  tasks: 2
  commits: 4
---

# Phase 25 Plan 14: One claim-once flip answer, and a CLI prompt that cannot hang the wait (IN-01, IN-02, WR-08) Summary

The web and CLI flip coordinators now share one claim-once implementation, `FlipAnswerSlot`, in `pipeline.py`. Before this, each had its own copy. A CLI prompt thread that fails unexpectedly (a lost terminal, undecodable input) now ends the flip wait right away as `ABORTED` and logs the traceback. It used to hang for `flip_timeout_seconds` and then wrongly report that nobody confirmed the flip. The stale `PipelineEvent.job_state` docstring is fixed.

## What changed

### Task 1: FlipAnswerSlot (IN-02) and the IN-01 docstring
- `src/saneless/pipeline.py`: new concrete `class FlipAnswerSlot`, placed right after `FlipCoordinator`. It holds a `threading.Lock`, a `threading.Event` and `_outcome`.
  - `offer(outcome) -> bool` claims only if the slot is unanswered, and sets the event only when it claimed.
  - `settle(outcome) -> FlipOutcome` claims if unanswered, sets the event and returns the answer in effect.
  - `wait(timeout) -> None` lets `KeyboardInterrupt` propagate.
  - `answer` is read under the lock.
  - The docstring now carries the "written under the lock before the event is set" ordering rationale and the S101 narrowing note.
  - Added to `__all__`, and `threading` is imported.
- IN-01: the `job_state` docstring now reads "Note that the returned state is not an instruction to write it".
- `tests/test_pipeline.py`: `TestFlipAnswerSlot` has 9 tests. They cover first-offer-wins, settle on unanswered and answered slots, waits woken by offer and by settle, `wait(0)` returning promptly, a 20-thread `Barrier` race that yields exactly one claim, and the `__all__` export. Every wait is bounded and no test sleeps.

### Task 2: Both coordinators compose the slot; WR-08
- `src/saneless/worker.py` `WorkerFlipCoordinator`:
  - `_lock`, `_event`, `_outcome` and `_resolve` are replaced by `self._slot = FlipAnswerSlot()` and `self._armed = threading.Event()`.
  - `_signal` returns `False` when unarmed and otherwise returns `self._slot.offer(...)`.
  - `wait_for_flip` arms, calls `self._slot.wait(timeout)`, then returns `self._slot.settle(TIMED_OUT)`.
  - The CR-01/D-16 docstring is kept. The job-id binding, `armed` and `answer` stay the same, and `ScanWorker` is unchanged.
- `src/saneless/cli.py` `ClickFlipCoordinator`:
  - It now owns only `self._slot`, and `_resolve` is gone. Ctrl-C still settles `ABORTED`, and expiry settles `TIMED_OUT`.
  - `_prompt` keeps its `click.Abort` handling. It also gains `except Exception:`, which calls `logger.exception("Flip prompt failed; treating it as an abort")` and then settles `ABORTED`.
  - The old "No bare `except Exception`" comment is replaced by the WR-08 rationale.
  - The prompt thread is still a daemon (D-19) and still uses `click.confirm` (D-11).
- `tests/test_cli.py`:
  - New parametrised `test_a_broken_prompt_aborts_at_once_with_a_logged_traceback`, run with `OSError(5, ...)` and `UnicodeDecodeError(...)`. It checks for `ABORTED` in under 5 s with `wait_for_flip(600)`, and for exactly one ERROR record containing "Flip prompt failed" with `exc_info` set.
  - The Ctrl-C test now monkeypatches `coordinator._slot.wait`.
  - `pytest` became a runtime import because the new test uses the parametrize decorator.
- `tests/test_worker.py`: `test_a_continue_racing_the_timeout_is_honoured` now monkeypatches `coordinator._slot.wait`.

## Verification
- Before the fix, the WR-08 test failed as expected (RED): both parametrisations hit the pytest timeout, with unhandled thread exceptions. The re-pointed tests failed with `AttributeError: _slot`.
- After the fix: `uv run pytest -q` passed, 1103 tests. `ruff check`, `ruff format --check`, `ty check`, `pyrefly check src tests`, `prek run --all-files` and `prek run --stage pre-push --all-files` all pass.
- The 25-10 CR-01 tests passed without changes: unarmed and early signals dropped, first armed answer wins, and the job-scoped route tests.
- Acceptance greps: no `def _resolve` is left in worker.py or cli.py, and `FlipAnswerSlot()` appears once in each file. No test monkeypatches a coordinator `_event`, and no `noqa` or `type: ignore` lines were added.
- `BLE001` did not flag the `except Exception:` handler that calls `logger.exception`, so the fallback `except OSError, UnicodeError:` form was not needed.

## Deviations from Plan

One small change from the plan: `WorkerFlipCoordinator._signal` still exists as a private helper. The plan said `signal_continue`/`signal_abort` return False when unarmed and otherwise call `self._slot.offer(...)`, and `_signal` does exactly that for both, so the armed check is not duplicated. Behaviour matches the plan.

Pyrefly reports 4 warnings, all in code this plan did not change (`contextlib.contextmanager` deprecation in pipeline.py, unnecessary `int()`/`float()` in sane_backend.py and fake_sane.py). They are out of scope and were not touched.

## TDD Gate Compliance
- Task 1: `test(25-14)` 3141d7c, then `feat(25-14)` 1e9d58e
- Task 2: `test(25-14)` 7d8c4ef, then `refactor(25-14)` d969e8a (the plan specifies `refactor` for this GREEN commit)

## Commits
- 3141d7c test(25-14): add failing tests for FlipAnswerSlot claim-once primitive
- 1e9d58e feat(25-14): add FlipAnswerSlot, the single claim-once flip answer
- 7d8c4ef test(25-14): add failing WR-08 prompt-failure test, re-point slot monkeypatches
- d969e8a refactor(25-14): compose FlipAnswerSlot in both flip coordinators; abort on broken prompt

## Threat model
- T-25-59 and T-25-60 are mitigated: `_prompt` catches the failure, logs it with its traceback and settles `ABORTED`, and the parametrised test bounds the return to 5 s.
- T-25-61 is mitigated: there is one `FlipAnswerSlot`, tested with a barrier-started race.
- T-25-62 is mitigated: arming stays only in `WorkerFlipCoordinator`, and the CR-01 tests pass unchanged.

## Self-Check: PASSED
