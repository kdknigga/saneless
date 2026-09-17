---
phase: 30-appliance-layer
plan: 25
subsystem: web
tags: [threading, single-flight, health-checks, scanner-gate, htmx, tdd, wr-03, wr-04]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "checks.py's five-row registry and _scanner_skipped (30-21), CheckRefresher's injected dependencies and stop(timeout) (30-24), _checks_context's scan_active read from worker.current_job_id"
provides:
  - "run_checks(context, *, scanner_gate=None): the registry takes the gate and holds it around _check_scanner alone"
  - "CheckRefresher._probe_and_store: one probe implementation, admitting one checker at a time"
  - "CheckRefresher.probe_now(): the Refresh button's path into that one probe, bypassing the TTL and the watch window"
  - "CheckRefresher(scan_active=...): the scanner skip derived from the worker's own record of a job in flight"
  - "A refresh_checks handler that owns no probe, no gate and no registry import"
affects: [appliance-layer, status-strip, scan-latency, 30-26, 30-27]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Pass a mutual-exclusion primitive into the callee that needs it, rather than wrapping the whole call in it at the call site"
    - "Single flight by non-blocking acquire-or-leave: the second caller returns at once because the first one's store is imminent"
    - "Derive a user-facing claim from the fact it asserts, never from a proxy that can be true for another reason"
    - "Two public entry points (probe_now, _tick) both delegate to one private implementation; neither calls the other"

key-files:
  created: []
  modified:
    - src/saneless/checks.py
    - src/saneless/web/refresher.py
    - src/saneless/web/app.py
    - src/saneless/web/routes.py
    - tests/test_checks.py
    - tests/test_refresher.py
    - tests/test_web_checks.py
    - tests/test_app_lifespan.py
    - docs/reference/web-api.md

key-decisions:
  - "Extracted _scanner_result(context, scanner_gate) rather than inlining the acquire/release in run_checks' loop: the inline form needed `scanner_gate is not None` repeated in the finally to keep the type narrowing, which reads worse than the helper the plan already sanctioned"
  - "_scanner_result calls _dispatch(CheckKey.SCANNER, context) rather than _check_scanner directly, so the module keeps exactly one dispatch point and the assert_never totality check still covers the gated path"
  - "threading is imported into checks.py's TYPE_CHECKING block, not at runtime: the module uses it only in annotations and ruff TC003 refuses the runtime import"
  - "The probe lock is a plain threading.Lock taken non-blocking, not a re-entrant lock: re-entrancy would let a probe that somehow re-entered itself run twice, which is the thing being prevented"
  - "Tests pass a recording lock through typing.cast rather than a MagicMock, so the call sites still type-check under ty and pyrefly with no suppression"
  - "The during-a-scan web tests now start a real job (_start_a_scan) instead of holding worker.scanner_gate, because holding the gate is no longer what makes the row skipped -- that substitution is the whole of WR-04"

patterns-established:
  - "Gate-sampling doubles: a check's own stub (the backend's get_devices, the Paperless responder, _directory_accepts_a_write) records lock.acquire(blocking=False) from inside its own frame, so 'is the lock held right now' is answered without a thread and without a sleep"
  - "Re-entrant spy for single flight: the run_checks stand-in calls probe_now() from inside itself, making the overlap a fact of the call stack rather than of scheduling luck"

requirements-completed: [APPL-01, APPL-02]

# Metrics
duration: 47min
completed: 2026-09-16
---

# Phase 30 Plan 25: One Probe Path, and a Gate Held Only Where It Is Needed Summary

**The scanner gate is now passed into `run_checks` and held around `_check_scanner` alone instead of around all five checks, and both probe sites collapsed into one single-flighted `CheckRefresher.probe_now` whose scanner skip reads the worker's own `current_job_id` -- so a Paperless timeout can no longer park a scan start, and two health checkers contending can no longer tell a household member that a scan is running.**

## Performance

- **Duration:** ~47 min
- **Started:** 2026-09-16T22:26Z
- **Completed:** 2026-09-16T23:13Z
- **Tasks:** 3 (all TDD, 6 commits: three RED, three GREEN)
- **Files modified:** 9 (+931 / -96)

## Measured gate hold window

The plan asks for the before/after hold window for one `run_checks` call with a slow Paperless double. Measured with a `MockTransport` that sleeps 3.0 s before answering, a stub scanner backend, and a lock that accumulates the time between acquire and release:

| Shape | Gate held | `run_checks` wall time |
|-------|-----------|------------------------|
| Before -- the caller wraps the whole of `run_checks` | **3.001 s** | 3.001 s |
| After -- the gate is passed in, taken around the scanner alone | **0.000 s** (under 1 ms) | 3.001 s |

The run costs the same either way; what changed is that `ScanWorker._scan_job` is no longer inside that 3 s. With a real backend the "after" figure is whatever `sane_get_devices` costs, which is the only thing the gate exists to make exclusive. The plan's WR-03 shape -- Paperless' `httpx.Timeout(5.0, connect=2.0)` plus two filesystem writes -- is entirely outside the hold window now.

## Accomplishments

- **WR-03 closed.** `run_checks(context, *, scanner_gate=None)` takes the gate as a parameter. `_scanner_result` acquires it non-blockingly, runs the scanner check, and releases in a `finally` that survives a raising check. The Paperless HTTP budget and the two `NamedTemporaryFile` writes run outside any lock. `doctor` passes no gate and is bit-for-bit unaffected.
- **WR-04 closed.** `skip_scanner` is `self._scan_active()` -- the worker's `current_job_id`, the same fact `_checks_context` renders as `scan_active` -- not `not acquired`. A `Check again` click landing during a refresher tick on an idle appliance renders the strip's existing rows, not "Not checked while a scan is running" beside "Last checked 14:02".
- **One probe implementation.** `_probe_and_store` is reached by `_tick` (after its two guards) and by `probe_now` (deliberately bypassing them, D-09). Neither public method calls the other. `refresh_checks` is now `note_watcher()` then `probe_now()` and the `run_checks` import is gone from `routes.py`.
- **Single flight.** `_probe_lock` admits one checker, taken without blocking; a second checker leaves rather than paying for a duplicate Paperless request and two file writes. Pinned by a `run_checks` double that re-enters `probe_now()` from inside itself, so the overlap involves no thread scheduling.
- **The reference doc is true again.** `docs/reference/web-api.md`'s `POST /api/checks/refresh` section now states both new facts in prose: concurrent refreshes collapse, and the paused Scanner message appears only while a scan really is running.
- **26 new tests**, suite at **2991 passing** (baseline 2965), no test removed, `tests/` `time.sleep` count unchanged at **17**.

## Task Commits

1. **Task 1: run_checks takes the gate, and takes it only around the scanner** — `e02b796` (test, RED), `841e1de` (refactor, GREEN)
2. **Task 2: one single-flighted probe path, and a skip that means what it says** — `b37ffcb` (test, RED), `533962d` (fix, GREEN)
3. **Task 3: the button goes through the one probe path, and the docs say what happens** — `c88b6b7` (test, RED), `20b0380` (refactor, GREEN)

No REFACTOR commit was needed: each GREEN landed clean under ruff, ruff-format, ty and pyrefly.

## TDD Gate Compliance

Every task ran RED then GREEN in that order, with a `test(...)` commit preceding each implementation commit. No test passed unexpectedly during a RED phase:

- Task 1 RED: 9 of 10 new cases failed on `TypeError: run_checks() got an unexpected keyword argument 'scanner_gate'`. The tenth (`test_an_ungated_run_still_produces_every_row`) is a deliberate no-change guard over `doctor`'s call shape and is expected to pass at RED.
- Task 2 RED: 22 cases failed on `CheckRefresher.__init__() got an unexpected keyword argument 'scan_active'`.
- Task 3 RED: 10 cases failed, including all three source guards and the WR-04 regression.

## Verification

| Gate | Result |
|------|--------|
| `uv run pytest -q` | 2991 passed (baseline 2965) |
| `uv run ruff check .` | clean |
| `uv run ruff format --check .` | clean |
| `uv run ty check` | clean |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -c "scanner_gate: threading.Lock \| None = None" src/saneless/checks.py` | 1 |
| `grep -cE "^(from\|import) saneless\.(web\|cli)" src/saneless/checks.py` | 0 (leaf rule holds) |
| `grep -c "def probe_now(" src/saneless/web/refresher.py` | 1 |
| `grep -c "def _probe_and_store(" src/saneless/web/refresher.py` | 1 |
| `grep -c "_probe_lock" src/saneless/web/refresher.py` | 3 |
| `grep -cE "skip_scanner=not acquired" src/saneless/web/refresher.py` | 0 |
| `grep -c "scanner_gate" src/saneless/web/refresher.py` | 5 |
| `grep -c "scan_active" src/saneless/web/app.py` | 2 |
| `grep -c "scanner_gate" src/saneless/web/routes.py` | 0 |
| `grep -c "probe_now()" src/saneless/web/routes.py` | 1 |
| `grep -c "run_checks" src/saneless/web/routes.py` | 0 |
| `CheckRefresher.__init__` parameter count | 6 (self plus five) |
| `tests/` `time.sleep` count | 17 |
| `git diff --stat docs/reference/web-api.md` | changed |

## Browser validation

Driven with Playwright directly (`uv run python` against a loopback uvicorn serving the real app over a stub scanner and a stubbed Paperless client), because the Playwright MCP tools were not exposed to this executor. Per CLAUDE.md, nothing here was deferred to a human.

- The strip loads five rows and the cold-start body drops its `hx-trigger` once results exist, so the poll stops on its own.
- `Check again` re-renders the strip: still five rows, still "Last checked …", no paused row.
- Three rapid clicks -- the single-flight case -- leave the strip coherent: five rows, no blanked body, and no "Not checked while a scan is running" on an idle appliance. This is WR-04 exercised through a real browser rather than through a held lock.
- Screenshot captured and visually checked: Scanner/Paperless/Data folder green, Profiles/Fallback amber with their next steps, `Check again` button present and readable above the Scan card.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `import threading` had to go in `checks.py`'s `TYPE_CHECKING` block**

- **Found during:** Task 1 GREEN
- **Issue:** The plan says to add `import threading` "in its alphabetised position". `checks.py` has `from __future__ import annotations` and uses `threading` only in annotations, so ruff `TC003` rejected the runtime import and the gate would not pass.
- **Fix:** `import threading` sits at the top of the existing `if TYPE_CHECKING:` block. The annotation is unchanged and reads identically.
- **Files modified:** `src/saneless/checks.py`
- **Commit:** `841e1de`

**2. [Rule 3 - Blocking] `_ProbeSpy`, `_RunChecksSpy` and the lifespan's `spy_run_checks` had to accept the new keyword**

- **Found during:** Tasks 2 and 3
- **Issue:** Every `run_checks` double in the suite took `(context)` only; the new call passes `scanner_gate=`.
- **Fix:** Each double takes `*, scanner_gate: threading.Lock | None = None` and records it, which also gave Task 2 and Task 3 their "the gate is handed in, not held" assertions for free. `test_startup_runs_no_check_probe`'s spy takes `**_kwargs`.
- **Files modified:** `tests/test_refresher.py`, `tests/test_web_checks.py`, `tests/test_app_lifespan.py`
- **Commits:** `b37ffcb`, `c88b6b7`

**3. [Rule 1 - Bug] The new lifespan test leaked an unclosed sqlite connection**

- **Found during:** Task 2 GREEN
- **Issue:** `test_the_refreshers_scan_fact_is_the_workers_own_job_id` first built the app without entering its `TestClient`, so the job store's connection was never closed and the next test failed on a `ResourceWarning` from the collected connection.
- **Fix:** The assertions run inside `with TestClient(app):`, and `worker._current_job_id` is restored to `None` in a `finally` so shutdown sees an idle worker.
- **Files modified:** `tests/test_app_lifespan.py`
- **Commit:** `533962d`

**4. [Rule 2 - Correctness] Two stale docstrings corrected alongside the code**

- **Found during:** Tasks 2 and 3
- **Issue:** `build_context`'s `Returns:` still said the caller sets `skip_scanner` "from its own non-blocking attempt on the scanner gate" -- exactly the derivation WR-04 removes. `_checks_context`'s docstring said "reads the cache and never calls ``run_checks``", naming a symbol `routes.py` no longer imports.
- **Fix:** `build_context` now points at `_probe_and_store` and the worker's job record; `_checks_context` says "reads the cache and never probes", which is the same guarantee stated without a dangling name. The second change is also what makes the plan's `grep -c "run_checks" src/saneless/web/routes.py` is 0 gate reachable.
- **Files modified:** `src/saneless/web/refresher.py`, `src/saneless/web/routes.py`
- **Commits:** `533962d`, `20b0380`

### Structural choices inside the plan's latitude

- The plan offered `_scanner_result(context, scanner_gate)` as a lint escape hatch. It was taken up front rather than reactively: the inline loop form needs `scanner_gate is not None` written twice (once to acquire, once in the `finally`) to keep ty and pyrefly narrowing the optional, and the helper carries the non-blocking-contract docstring better than a comment inside a loop would.
- The plan suggested pinning "the lock is free during the fallback/data-dir checks" from "a fallback/data-dir path". The sample is taken inside `_directory_accepts_a_write`, which is the function that performs both writes -- the narrowest point that is genuinely inside those checks' execution.

## Residual, recorded deliberately

- **T-30-25-02 is only partially closed.** Single flight collapses *concurrent* checkers to one probe, so N simultaneous refreshes cost one Paperless request. A *serial* loop of clicks is still unbounded; plan 30-26 puts the minimum interval under it. This is stated in the threat register and is not closed here.
- **The one-TTL skipped-row window.** A skipped row stored during a scan stays on the strip until the next probe, so for up to one TTL after a scan ends the strip can still say "not checked while a scan is running" beside an idle appliance. It is documented in `_probe_and_store`'s docstring, closed in practice by the terminal-state reload the status area already issues, and -- unlike WR-04's case -- it is a row that was true when it was written.

## Known Stubs

None. No placeholder value, empty-collection default or "coming soon" string was introduced; every row the strip renders comes from the real registry.

## Threat Flags

None. No network endpoint, auth path, file-access pattern or schema was added. `threading` is stdlib and `pyproject.toml` is untouched, so T-30-25-SC (package installs) remains `n/a`.

## Self-Check

- `src/saneless/checks.py` — FOUND
- `src/saneless/web/refresher.py` — FOUND
- `src/saneless/web/app.py` — FOUND
- `src/saneless/web/routes.py` — FOUND
- `docs/reference/web-api.md` — FOUND
- `tests/test_checks.py` — FOUND
- `tests/test_refresher.py` — FOUND
- `tests/test_web_checks.py` — FOUND
- `tests/test_app_lifespan.py` — FOUND
- Commits `e02b796`, `841e1de`, `b37ffcb`, `533962d`, `c88b6b7`, `20b0380` — all FOUND in `git log`

## Self-Check: PASSED
