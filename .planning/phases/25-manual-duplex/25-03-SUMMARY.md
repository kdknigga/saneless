---
phase: 25-manual-duplex
plan: 03
subsystem: pipeline
tags: [manual-duplex, flip-coordinator, threading, abc, match-assert-never, tdd]
requires:
  - "OutputConfig.flip_timeout_seconds (plan 25-02)"
  - "PipelineEvent.SCANNING_REVERSE -> JobState.SCANNING_REVERSE (plan 25-02)"
provides:
  - "saneless.vocabulary.FlipOutcome (CONTINUED | ABORTED | TIMED_OUT)"
  - "saneless.pipeline.FlipCoordinator ABC: wait_for_flip(self, timeout: float) -> FlipOutcome"
  - "saneless.pipeline._FlipContext(coordinator: FlipCoordinator, timeout: float), frozen"
  - "saneless.pipeline._flip_context(request, settings) -> _FlipContext (placeholder refusal for 25-04)"
  - "PipelineRequest.flip_coordinator: FlipCoordinator | None = None (replaces flip_event and abort_event)"
  - "saneless.worker.WorkerFlipCoordinator: signal_continue(), signal_abort(), wait_for_flip(timeout)"
  - "tests.conftest.AlwaysContinueFlipCoordinator"
affects: [25-04 (up-front refusal replaces _flip_context's None branch), 25-05 (worker job-outcome tests, _transition_event deletion), 25-06, 25-07 (ClickFlipCoordinator), 25-09 (docs quote the messages below), 28 EXC-04 (cancelled vocabulary)]
tech-stack:
  added: []
  patterns:
    - "ABC for a seam the project implements (ScannerBackend precedent), not typing.Protocol"
    - "frozen context dataclass to stay under PLR0913 (_DeliveryContext precedent)"
    - "single-answer claim under threading.Lock, written before Event.set; _resolve returns the claimed answer so no assert is needed"
    - "total match + assert_never over FlipOutcome"
key-files:
  created: []
  modified:
    - src/saneless/vocabulary.py
    - src/saneless/pipeline.py
    - src/saneless/worker.py
    - tests/conftest.py
    - tests/test_pipeline.py
    - tests/test_worker.py
decisions:
  - "The None-coordinator placeholder raises ConfigError instead of skipping the wait. Skipping would bring back C-02 (pass B starting at once). It is a small helper, _flip_context, and not inline code, because inline code pushed run_pipeline over PLR0915 (51 > 50 statements)"
  - "WorkerFlipCoordinator.wait_for_flip has one path for both endings: wait on the event, then _resolve(TIMED_OUT). _resolve returns whichever answer was claimed first, so a signal that won is returned unchanged and nothing needs an assert"
  - "The race test sets up the interleaving directly. It replaces the coordinator's _event.wait with a function that calls signal_continue and then returns False, so no timing is involved"
metrics:
  duration: "~35 min"
  completed: 2026-09-14
  tasks: 2
  files: 6
---

# Phase 25 Plan 03: The FlipCoordinator Seam Summary

The pipeline now waits for a manual-duplex flip in exactly one place: a single call to
`FlipCoordinator.wait_for_flip(timeout)`, capped by `output.flip_timeout_seconds`. The call returns one
`FlipOutcome`, and a `match` + `assert_never` handles it. The worker's `WorkerFlipCoordinator` accepts
only the first answer, so a second signal is ignored (D-16). The old wait could block the worker
thread forever (M-07). The old abort needed two steps: wake up, then check a second event to find out
why. Both problems are gone.

## Commits

| Gate | Task | Commit | Message |
|------|------|--------|---------|
| RED | 1 | `7c992d7` | test(25-03): add failing tests for the FlipCoordinator seam |
| GREEN | 2 | `14626af` | feat(25-03): FlipCoordinator seam with a bounded, single-answer flip wait |

No REFACTOR commit was needed.

At RED the suite failed with `ImportError: cannot import name 'FlipCoordinator' from
'saneless.pipeline'`, raised while pytest loaded `tests/conftest.py`. This is one of the two RED reasons
the plan allows. The import is at module level because the stub has to subclass the ABC, so it fails
while pytest is still loading conftest. No other error was involved. Commit-stage hooks (ruff, ty src,
pyrefly src) passed.

## Final signature

```python
def _scan_manual_duplex(
    scanner: ScannerBackend,
    device_id: str,
    scan_settings: ScanSettings,
    request: PipelineRequest,
    flip: _FlipContext,
) -> ScanBatch | _DuplexMismatch:
```

`notify` is derived inside the function (`request.status_callback or _noop_callback`), with the
same justifying comment as `_handle_duplex_mismatch`. Neither function has more than five
parameters.

## Exact ScanError messages (plans 25-05 and 25-09 assert on and document these)

- `FlipOutcome.ABORTED`:
  `Manual duplex scan aborted at the flip prompt`
- `FlipOutcome.TIMED_OUT` (built as `f"... after {flip.timeout:g} seconds: ..."`, so 600 renders as
  `600` and 0 as `0`):
  `Manual duplex flip wait timed out after 600 seconds: nobody confirmed the stack was flipped`

Both raise before pass B starts. As D-15 requires, both are plain `ScanError`s, so the job ends
`ERROR` / `SCANNER`. There is no `ScanCancelledError` and no `CANCELLED` state, and the log level is
unchanged; those belong to Phase 28.

The placeholder refusal message, which 25-04 replaces:
`Profile '<name>' is manual duplex, which needs a flip coordinator, and none was supplied`
(`ConfigError`).

## What was built

- **`vocabulary.FlipOutcome`**: a `StrEnum` with values equal to their names. Its docstring explains
  why there are exactly three members and no "still waiting" member. It is inserted into `__all__` in
  alphabetical order.
- **`pipeline.FlipCoordinator(ABC)`**: one abstract method whose body is only a docstring, following
  `ScannerBackend`. The class docstring explains why this is an ABC and not a Protocol, and that
  DPLX-04's "protocol" means "contract". It is exported via `__all__`.
- **`pipeline._FlipContext`**: a frozen dataclass. Its docstring gives PLR0913 as the reason it
  exists, and says the coordinator is non-Optional so the None check happens only once.
- **`PipelineRequest.flip_coordinator`**: replaces both `flip_event` and `abort_event`. The
  `threading` import under `TYPE_CHECKING` was removed from `pipeline.py`.
- **`pipeline._flip_context(request, settings)`**: builds the context from
  `output.flip_timeout_seconds`. It raises `ConfigError` when the request has no coordinator, and a
  comment points to 25-04.
- **`worker.WorkerFlipCoordinator`**: holds a `threading.Lock`, a `threading.Event` and a
  `FlipOutcome | None` slot. `_resolve` claims the answer under the lock, sets the event, and returns
  whichever answer was claimed. `src/saneless/worker.py` contains no `assert`.
- **`ScanWorker`**: `continue_flip` and `abort_flip` pass their signal to the coordinator.
  `_process_job` builds a `WorkerFlipCoordinator` for manual-duplex jobs, and the `finally` block
  clears it with one assignment. `_transition_event` was not touched and still appears 7 times; it is
  deleted in 25-05.

## Tests

- `tests/conftest.py`: `AlwaysContinueFlipCoordinator(FlipCoordinator)`.
- `tests/test_pipeline.py`: all 18 manual-duplex `PipelineRequest` constructions now pass
  `flip_coordinator=`. That covers `TestManualDuplex`, `TestDuplexMismatchDelivery`, the two duplex
  DPI tests and the shared-fake integration test. The integration test's `threading.Event` was
  replaced by the coordinator. Its `report_sources` line was left as is, because that change belongs
  to 25-06.
  A new `_FixedFlipCoordinator` stub records each timeout and the events seen before the wait. New
  tests:
  - the wait receives the configured timeout (42), `AWAITING_FLIP` comes before it, and
    `SCANNING_REVERSE` comes only after it
  - `ABORTED` raises `ScanError` matching "flip prompt", after one scan and with no upload
  - `TIMED_OUT` raises `ScanError` matching "flip wait" and containing the configured `17`, after one
    scan and with no upload
- `tests/test_worker.py`: the simulated pipeline now uses the coordinator with a 5 s limit. The
  request-capture test checks `flip_coordinator is None` for simplex jobs, and the abort test expects
  "flip prompt". New class `TestWorkerFlipCoordinator` has six tests, each waiting 0 seconds at most:
  - continue resolves to CONTINUED
  - abort resolves to ABORTED
  - D-16: a later abort is ignored
  - an unanswered wait resolves to TIMED_OUT
  - a continue after a timeout does not reopen the wait
  - a continue that races the timeout wins

## Verification

- `uv run pytest -q`: 1015 passed.
- `uv run ruff check .`, `uv run ruff format --check .` and `uv run ruff check . --select PLR0913,S101`
  are all clean.
- `uv run ty check` passes. `uv run pyrefly check src tests` reports 0 errors; its 4 warnings were
  there before this plan, in `pipeline.py:293`, `sane_backend.py` and `fake_sane.py`.
- Grep gates:
  - `flip_event|abort_event` has no matches in `src/` or `tests/test_pipeline.py`.
  - `class FlipCoordinator(ABC)` appears once.
  - `worker.py` contains no `assert`.
  - `ScanCancelledError|CANCELLED` has no matches in `src/`.
  - `_transition_event` still appears 7 times, as before.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] The call-site None handling became a helper to satisfy PLR0915**
- **Found during:** Task 2
- **Issue:** With the `None` check written inline in `run_pipeline`, the function reached 51
  statements, one over ruff's `PLR0915` limit of 50. CLAUDE.md forbids suppressing the rule.
- **Fix:** Moved the check into `_flip_context(request, settings) -> _FlipContext`. It is still the
  "minimal handling" the plan asked for, and 25-04 replaces it.
- **Files modified:** src/saneless/pipeline.py
- **Commit:** `14626af`

**Not added, and why:** the plan's must-have "a timed-out job releases the worker thread so the next
job runs" is true by construction here, because `wait_for_flip` is bounded. It is tested at the
coordinator level (`wait_for_flip(0)` returns `TIMED_OUT`). Plan 25-05 has the job-outcome test
("the next submitted job still runs"), so no duplicate worker end-to-end test was written here.

`tests/test_outcomes_e2e.py` was not edited. It still passes through `worker.continue_flip()`, and a
docstring there that mentions `flip_event` will be updated by a later plan.

Commits used `/usr/bin/git` and all hooks ran. No Serena editing tools were used.

## Threat surface

T-25-09 (unbounded wait) and T-25-10 (signal race) are mitigated as the plan specified. No new
endpoints or trust boundaries were added.

## Known Stubs

`_flip_context`'s `None` branch is a deliberate placeholder. It refuses safely just before pass A,
not before any scanner contact. Plan 25-04 replaces it with the up-front guard.

## Self-Check: PASSED

- FOUND: src/saneless/vocabulary.py, src/saneless/pipeline.py, src/saneless/worker.py,
  tests/conftest.py, tests/test_pipeline.py, tests/test_worker.py
- FOUND: 7c992d7 (RED), 14626af (GREEN)
