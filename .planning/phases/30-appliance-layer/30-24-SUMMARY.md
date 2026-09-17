---
phase: 30-appliance-layer
plan: 24
subsystem: infra
tags: [threading, shutdown, fastapi-lifespan, sigterm, docstrings]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: CheckRefresher, the lifespan's two-thread guarded close (A-7, D-07, D-09)
provides:
  - "CheckRefresher.stop(timeout=None) honouring a caller-supplied join bound, clamped at zero"
  - "A single shutdown deadline in web/app.py that both joins spend, capping the worst case at one STOP_JOIN_SECONDS"
  - "_stop_threads(): the two-thread stop sequence extracted from create_app, carrying the corrected reasoning"
  - "Docstrings and comments that no longer claim the two joins overlap (WR-07)"
affects: [deployment, container-stop-grace-period, 30-25]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A shared monotonic deadline passed down to each bounded join, rather than a fresh bound per thread"
    - "Recording thread/clock doubles assert a timeout by value, never by elapsed wall time"

key-files:
  created: []
  modified:
    - src/saneless/web/refresher.py
    - src/saneless/web/app.py
    - tests/test_refresher.py
    - tests/test_app_lifespan.py

key-decisions:
  - "Chose WR-07's first option (make the claim true with a shared deadline) over the second (weaken the comment to a per-thread bound), because a container stop grace period is sized on the documented number"
  - "A negative remaining budget is clamped to zero inside stop(), not only at the call site, so no caller can accidentally spell join(None) -- an unbounded wait"
  - "Extracted _stop_threads() rather than suppressing ruff PLR0915: create_app sat at the 50-statement cap and the new deadline tipped it to 51"
  - "stop() delegates the default to STOP_JOIN_SECONDS when timeout is None, so every existing caller and test is untouched"

patterns-established:
  - "Shared-deadline shutdown: take one monotonic deadline before the first join and hand each subsequent join max(0.0, deadline - now)"
  - "Bound-by-value testing: a recording double over the thread (or over the app module's time) pins the timeout argument without any sleep"

requirements-completed: [APPL-02]

# Metrics
duration: 34min
completed: 2026-09-16
---

# Phase 30 Plan 24: One Shutdown Deadline, Spent Across Both Joins Summary

**Shutdown now takes a single `STOP_JOIN_SECONDS` deadline before either join and hands the refresher what is left of it, so two threads parked in an unbounded `getaddrinfo` cost one bound rather than two -- and the comment and docstrings that claimed the joins overlapped now say what the code actually does.**

## Performance

- **Duration:** ~34 min
- **Started:** 2026-09-16T00:00:00Z (approx.)
- **Completed:** 2026-09-16
- **Tasks:** 2 (each RED + GREEN)
- **Files modified:** 4

## Accomplishments

- `CheckRefresher.stop()` accepts an optional `timeout`; `None` still means the full `STOP_JOIN_SECONDS`, and a negative value is clamped to zero so a spent budget becomes a poll rather than `join(None)`.
- The lifespan computes `deadline = time.monotonic() + STOP_JOIN_SECONDS` before `worker.stop()` and calls `refresher.stop(timeout=max(0.0, deadline - time.monotonic()))`. A worker that eats the whole bound now leaves the refresher `0.0`, which is the assertion that makes the doubling impossible.
- Three pieces of prose that repeated the false claim are corrected and cross-referenced to WR-07: the `app.py` shutdown comment (now the `_stop_threads` docstring), `CheckRefresher.request_stop`'s docstring, and `CheckRefresher.stop`'s docstring. Two test docstrings that echoed it were corrected too.
- Nine new tests, none of which sleep: five over `stop`'s bound and four over the shared budget. The `tests/` `time.sleep` count is unchanged at 17.
- The leave-resources-open branch is byte-for-byte unchanged: a `False` from either `stop` still skips `paperless.close()` and `scanner.close()` (A-7, D-09), and its three existing tests stay green.

## Task Commits

1. **Task 1 (RED): pin the refresher join against a supplied bound** — `426c2c5` (test)
2. **Task 1 (GREEN): let a caller bound the refresher join** — `8e91351` (feat)
3. **Task 2 (RED): pin shutdown to a single join budget** — `0b53aa7` (test)
4. **Task 2 (GREEN): give shutdown one deadline instead of one per thread** — `1a4391b` (fix)

No REFACTOR commit was needed for either task.

## Files Created/Modified

- `src/saneless/web/refresher.py` — `stop(self, timeout: float | None = None) -> bool` with a zero-clamped budget; `stop`, `request_stop` and the class docstring corrected.
- `src/saneless/web/app.py` — `import time`; new module-level `_stop_threads(worker, refresher) -> tuple[bool, bool]` holding the shared deadline and the corrected reasoning; the lifespan now calls it in one statement.
- `tests/test_refresher.py` — `_RecordingThread` double plus five tests over the join bound; corrected `test_request_stop_signals_without_joining`'s docstring.
- `tests/test_app_lifespan.py` — `_FakeMonotonic`, `_recorded_refresher_budget`, and `TestShutdownSharesOneJoinBudget` (4 tests); two existing `refresher.stop` doubles updated to the new signature; the stale "joins overlap" docstring corrected.

## Decisions Made

- **Correct the code, not just the comment.** WR-07 offered either. The shared deadline was chosen because operators size a container stop grace period on the documented bound, so a per-thread bound would need the documentation *and* every deployment doubled.
- **Clamp inside `stop()`.** The plan's `max(0.0, ...)` at the call site is kept, but `stop()` clamps as well. A negative passed to `Thread.join` is treated as zero by CPython, but `None` is not, and defence in depth here costs one expression.
- **Delegate rather than replace in the test doubles.** `_recorded_refresher_budget`'s refresher double records the bound and then joins with the real default, so the thread always stops and the test leaks neither a thread nor an open store.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Extracted `_stop_threads()` to stay under ruff's `PLR0915`**
- **Found during:** Task 2 (GREEN)
- **Issue:** `create_app` was at ruff's 50-statement cap. Adding the `deadline` assignment made it 51 and `uv run ruff check .` failed. Project rules forbid `# noqa` or relaxing the rule.
- **Fix:** Moved `refresher.request_stop()`, the deadline, and the two `stop()` calls into a module-level `_stop_threads(worker, refresher) -> tuple[bool, bool]`, which the lifespan calls in one statement (net −3 statements). The corrected WR-07 reasoning moved with it into the helper's docstring.
- **Files modified:** `src/saneless/web/app.py`
- **Verification:** `uv run ruff check .` clean; all 2897 tests pass; the monkeypatch-based lifespan tests are unaffected because they patch the instances, not the call site.
- **Committed in:** `1a4391b`

**2. [Rule 3 - Blocking] Updated two existing `refresher.stop` test doubles to the new signature**
- **Found during:** Task 2 (GREEN)
- **Issue:** `recording_refresher_stop()` and `stuck_stop()` in `tests/test_app_lifespan.py` took no arguments, so `refresher.stop(timeout=...)` raised `TypeError` inside the lifespan and two existing shutdown tests failed.
- **Fix:** Both doubles now take `timeout: float | None = None`; the delegating one forwards it.
- **Files modified:** `tests/test_app_lifespan.py`
- **Verification:** `test_shutdown_closes_resources_after_both_threads_stop` and `test_shutdown_leaves_resources_open_when_the_refresher_does_not_stop` pass unchanged in substance.
- **Committed in:** `1a4391b`

**3. [Rule 2 - Missing Critical] Corrected two test docstrings that repeated the false claim**
- **Found during:** Tasks 1 and 2 (GREEN)
- **Issue:** T-30-24-03 is a repudiation threat about operator-facing shutdown documentation. `test_request_stop_signals_without_joining` and `test_both_stop_events_are_set_before_either_join_begins` both asserted in prose that signalling first makes the joins overlap and bounds the total — the exact claim WR-07 refutes. Leaving them would have re-seeded the error for the next reader.
- **Fix:** Both docstrings now say what the early signal actually buys (an idle refresher exits during the worker's join, so its own join is skipped) and point at the shared deadline as the thing that bounds the total. Neither test's assertions changed.
- **Files modified:** `tests/test_refresher.py`, `tests/test_app_lifespan.py`
- **Verification:** Both tests still pass; the assertions are untouched.
- **Committed in:** `8e91351`, `1a4391b`

---

**Total deviations:** 3 auto-fixed (2 blocking, 1 missing critical)
**Impact on plan:** No scope creep. The extraction is the only structural change, and it was forced by a lint cap the project forbids suppressing; the other two are the mechanical and documentary consequences of the signature change.

## Issues Encountered

- **RED had to be made honest twice.** The first draft of `test_stop_without_a_timeout_joins_with_the_shared_bound` asserted `False` from a double whose thread exits on join; it failed for the wrong reason. Corrected so the four genuine RED failures are all `TypeError: stop() got an unexpected keyword argument 'timeout'`.
- **Faking the clock without touching the real one.** Patching `time.monotonic` globally would have reached anything else using it. Instead the tests substitute the `time` name in `saneless.web.app`'s namespace with a small object exposing `monotonic()`. `raising=False` is used so the same helper worked in the RED commit, where `app.py` had no `time` import yet. The test is still non-vacuous: if the substitution missed, the real clock would return the full budget and the "worker spends the whole budget" cases would fail.

## Verification

- `uv run pytest -q` — 2897 passed (parent commit: 2888).
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests` — all clean.
- `grep -c "def stop(self, timeout: float | None = None) -> bool:" src/saneless/web/refresher.py` = 1.
- `grep -c "deadline" src/saneless/web/app.py` = 4; `grep -cE "refresher\.stop\(timeout=" src/saneless/web/app.py` = 1.
- Neither file claims "the two bounded joins overlap" (both greps = 0).
- `grep -rc "time\.sleep" tests/ | awk -F: '{s+=$2} END {print s}'` = 17, unchanged.
- `git diff <base> HEAD -- src/saneless/web/refresher.py | grep -c "^[-+].*def _tick"` = 0 — `_tick` is untouched, as plan 30-25 owns it.

## Known Stubs

None.

## Threat Flags

None — no new network endpoint, auth path, file access pattern or schema change. `pyproject.toml` is untouched, so T-30-24-SC remains n/a.

## User Setup Required

None. Operators who sized a container stop grace period on the documented `STOP_JOIN_SECONDS` now have a bound the code actually honours; no configuration change is required.

## Next Phase Readiness

- WR-07 is closed. Plan 30-25 owns `_tick`, `build_context` and `note_watcher`, none of which this plan touched.
- WR-02 (the unbounded `getaddrinfo`) is still open and is the pathological case this bound exists for; closing it would make the bound rarely reached rather than unnecessary.

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-16*

## Self-Check: PASSED

All four modified source/test files and the summary exist on disk, and all four
task commits (`426c2c5`, `8e91351`, `0b53aa7`, `1a4391b`) are present in the
branch history, followed by the commit that carries this summary.
