---
phase: 30-appliance-layer
plan: 09
subsystem: appliance-health
tags: [app-factory, jinja-filters, dependency-injection, lifespan, dual-thread-shutdown, bounded-join, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "vocabulary.py — error_message, error_next_step, local_time, page_counts (plan 30-01)"
  - phase: 30-appliance-layer
    provides: "checks.py — CheckContext, check_name, check_state_label/class/glyph (plan 30-06)"
  - phase: 30-appliance-layer
    provides: "web/checks_cache.py CheckCache and web/refresher.py CheckRefresher (plan 30-07)"
  - phase: 26-worker-and-web-robustness
    provides: "the lifespan's bounded-join shutdown branch and STOP_JOIN_SECONDS (D-08, D-09, D-18)"
provides:
  - "eleven Jinja filters on app.state.templates — the three earlier ones plus check_name, check_state_class, check_state_glyph, check_state_label, error_message, error_next_step, local_time, page_counts"
  - "app.state.checks — the cold CheckCache every route and renderer reads"
  - "app.state.refresher — the CheckRefresher routes stamp with note_watcher()"
  - "CheckRefresher.request_stop() — the signal half of stop(), safe before start()"
  - "a lifespan that starts the refresher after the worker and closes nothing until both threads confirm they stopped (A-7)"
  - "web/app.py _build_templates() and _build_check_machinery() — the two module-level factories create_app now composes"
affects: [30-10, 30-11, 30-12]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "filter registration by identity, not by wrapper: a test asserts `env.filters['local_time'] is local_time`, so the web table and the `saneless doctor` table cannot drift apart without deleting the one implementation both read"
    - "signal-then-join for multiple bounded threads: a `request_stop()` that only sets the event lets a caller signal every thread before joining any, so N bounded joins cost one bound rather than N"
    - "an all-or-nothing close gate generalised from one thread to a set: the close sequence runs only when every background thread confirms, and the warning names which one did not"
    - "module-level factories as the answer to a statement-count lint, not a suppression: `_build_templates` and `_build_check_machinery` exist because inlining them puts `create_app` over ruff PLR0915, and each is a cohesive unit with its own docstring"

key-files:
  created: []
  modified:
    - src/saneless/web/app.py
    - src/saneless/web/refresher.py
    - tests/test_web.py
    - tests/test_app_lifespan.py
    - tests/test_refresher.py

key-decisions:
  - "`CheckRefresher.request_stop()` was added rather than splitting `ScanWorker.stop()`. The worker already sets its event as its first statement and joins last, so calling `refresher.request_stop()` before `worker.stop()` puts both events in place before either join begins — with no change to worker.py and no second entry point on the class that every other caller would then have to choose between"
  - "`CheckCache()` is built with no `ttl` argument. `MetadataCache` takes `settings.output.paperless_cache_ttl_seconds`, but there is no config key for the check cache and D-03 fixes the answer at the class default; one default read at call time is what a test or a future setting overrides"
  - "`profile_storage` is read inside the context factory rather than captured when the refresher is built. The worker records it when it writes the generated profiles, which happens after `create_app` has already returned, so a captured value would be the pre-startup placeholder for ever (D-22)"
  - "The overlap test asserts over the joins that actually happened, not over a fixed four-element list. The refresher's one-second `Event.wait` wakes on the event set before the worker's join even starts, so by the time `refresher.stop()` runs the thread has usually exited and `stop()` skips the join entirely — the absence of `refresher.join` *is* the overlap, and a test demanding it would fail on the healthy path"
  - "The shutdown warning moved the job id into a trailing `(current job: %s)` clause. The original `(job %s still running)` read as nonsense when the refresher was the stuck thread and the worker was idle with no job at all; the trailing form is honest in both branches and still carries the id the existing regression test greps for"
  - "`_build_templates()` and `_build_check_machinery()` are module-level rather than inline. Adding eight filters pushed `create_app` to 61 statements against ruff's PLR0915 bound of 50, and CLAUDE.md forbids `noqa` and rule-disabling — extraction is the only fix that is not a suppression"

patterns-established:
  - "A test class whose fixture deliberately never enters the lifespan (`unstarted_app`), closing the Paperless client and job store itself, so construction-time invariants are asserted on exactly the object a caller gets from the factory"
  - "Recording the internal `Event.set` and `Thread.join` of two objects into one ordered list, then asserting index relations over it — a deterministic substitute for the timing assertion an overlap claim invites"

requirements-completed: [APPL-02]

# Metrics
duration: 22min
completed: 2026-09-16
---

# Phase 30 Plan 09: Wiring the Appliance Layer into the App Summary

**Eight shared functions registered as Jinja filters by identity, a cold check cache and an unstarted refresher on `app.state`, and a lifespan that signals both background threads before joining either and closes nothing until both confirm.**

## Performance

- **Duration:** 22 min
- **Tasks:** 2 (both TDD, four commits: test → feat → test → feat)
- **Files modified:** 5
- **Tests added:** 13 (6 composition, 5 lifespan, 2 refresher), 1 lifespan test rewritten
- **Full non-browser suite:** 2449 passed in 68 s, no thread-leak warnings
- **Browser suite:** 77 passed in 20 s

## What Was Built

### `src/saneless/web/app.py` — `_build_templates()`

All eleven filters now live in one function whose docstring carries the block's original
constraint (registration before any template is loaded is Jinja's only requirement for
mutating `filters` and `globals` on a live Environment) and extends it with the reason
identity matters:

> Each name is bound to the shared function itself and never to a wrapper, because
> `local_time` here is the same object `cli.py` imports: the web table and the
> `saneless doctor` table cannot disagree about the zone or the format, and could not be
> made to without editing the one implementation both read (APPL-12).

The new eight are `check_name`, `check_state_class`, `check_state_glyph`,
`check_state_label` from `checks.py`, and `error_message`, `error_next_step`,
`local_time`, `page_counts` from `vocabulary.py`. The `JobState` global and its comment
about Jinja 3.1.6's unannotated `DEFAULT_NAMESPACE` moved across untouched.

`tests/test_web.py::TestAppComposition::test_each_filter_is_the_shared_implementation`
asserts each with `is`. A "renders the same string" test would pass today and drift the
first time either surface is edited; `is` is what makes APPL-12 checkable rather than
merely claimed.

### `src/saneless/web/app.py` — `_build_check_machinery()`

Returns the cold `CheckCache` and the unstarted `CheckRefresher`. The context factory is a
closure over `settings`, `scanner`, `paperless` and `worker`; the gate is
`lambda: worker.scanner_gate`, late-bound exactly as plan 30-07's contract requires. The
refresher never reaches into `app.state`, which is what keeps it unit-testable with no
FastAPI application at all.

Nothing probes. `app.state.checks.current().results is None` immediately after
`create_app`, and `app.state.refresher._thread.is_alive()` is `False` — both asserted on an
app whose lifespan has never been entered.

### `src/saneless/web/refresher.py` — `request_stop()`

```python
def request_stop(self) -> None:
    self._stopping.set()
```

`stop()` now calls it instead of setting the event itself, so the two entry points cannot
diverge. The docstring states why it exists: the lifespan has two threads, each with a
join bounded by `STOP_JOIN_SECONDS`, and signalling both before joining either keeps the
worst case at one bound instead of two (A-7).

### `src/saneless/web/app.py` — the lifespan

**Startup:** `refresher.start()` immediately after `worker.start()`, because the gate
accessor reads `worker.scanner_gate` and the context reads `worker.profile_storage`.
No probe is on the startup path — `test_startup_runs_no_check_probe` spies on
`refresher_module.run_checks` and records zero calls across an empty lifespan, with the
cache still cold afterwards. That cold cache is the state D-06 renders as `Checking…`.

**Shutdown:**

```python
refresher.request_stop()
worker_stopped = worker.stop()
refresher_stopped = refresher.stop()
if not (worker_stopped and refresher_stopped):
    ...
    return
```

Both events are in place before either join begins. The gate's comment explains the A-7
hazard in full: the refresher holds this same Paperless client and may be inside
`sane_get_devices`, so `paperless.close()` would raise inside a live probe and
`scanner.close()` would run `sane_exit()` with a SANE call outstanding, which
`sane_backend.shutdown()` documents as a segfault risk. The close order
`paperless → job_store → scanner` and its D-18 comment are untouched.

The warning names which thread is stuck — `"Scan worker"`, `"check refresher"`, or both
joined with `" and "` — and the job id moved to a trailing `(current job: %s)` clause so it
reads correctly when the worker is idle and the refresher is the problem.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] ruff PLR0915 on `create_app` after adding eight filters**

- **Found during:** Task 1 GREEN, at the `uv run ruff check .` gate
- **Issue:** Eight filter registrations plus the cache, the context factory and the
  refresher took `create_app` from 47 to 61 statements, over ruff's PLR0915 bound of 50.
  CLAUDE.md forbids `noqa`, `# type: ignore` and rule-disabling, so suppression was not
  available.
- **Fix:** Extracted `_build_templates()` and `_build_check_machinery()` as module-level
  functions with full docstrings. `create_app` came back to 46 statements and each helper
  is a cohesive unit — the one that owns all vocabulary, and the one that owns the check
  machinery.
- **Files modified:** `src/saneless/web/app.py`
- **Commit:** `2b6068e`

**2. [Rule 1 — Bug in the test I had just written] the overlap assertion demanded a join
that the healthy path skips**

- **Found during:** Task 2 GREEN
- **Issue:** `test_both_stop_events_are_set_before_either_join_begins` asserted a
  four-element set including `refresher.join`. It failed — not because the implementation
  was wrong but because the refresher thread had already exited by the time
  `refresher.stop()` ran, so `stop()` skipped the join. The test encoded an assumption the
  design deliberately falsifies.
- **Fix:** The assertion now requires both event sets, requires every join that *did*
  happen to follow both sets, and pins the non-vacuous core:
  `events.index("refresher.set") < events.index("worker.join")` — the refresher was
  already winding down while the worker's bounded join ran, which is the overlap itself.
  The docstring records why `refresher.join` is usually absent.
- **Files modified:** `tests/test_app_lifespan.py`
- **Commit:** `a1499af`

### Plan-Sanctioned Extension

`src/saneless/web/refresher.py` is not in the plan's `files_modified`, but Task 2's action
text explicitly anticipates this: *"call whatever method each class exposes for signalling
without joining — if `stop()` is the only entry point, split it in `refresher.py` … and
record the choice in the SUMMARY."* `stop()` was the only entry point, so it was split.
`worker.py` needed no change: `ScanWorker.stop()` already sets its event as its first
statement and joins last, so it is already a signal-then-join in one call.

## What Plan 30-07's Contract Required, and Where It Is Honoured

| 30-07 contract | Where |
|---|---|
| Build both in `create_app`, expose as `app.state.checks` / `app.state.refresher` | `_build_check_machinery`, `app.state` block |
| `refresher.start()` after `worker.start()` | lifespan startup; `test_the_refresher_starts_after_the_worker` |
| `refresher.stop()` before `worker.stop()`'s resources are released | shutdown gate; `test_shutdown_closes_resources_after_both_threads_stop` |
| Gate callable late-bound — `lambda: worker.scanner_gate` | `_build_check_machinery` |
| `stop()` returning `False` leaves Paperless and the scanner open | the `if not (worker_stopped and refresher_stopped)` branch; `test_shutdown_leaves_resources_open_when_the_refresher_does_not_stop` |

## Requirements

`APPL-02` (the refresher's lifecycle and its bounded, non-destructive shutdown) is
complete here.

`APPL-03`, `APPL-04` and `APPL-12` are **progressed, not completed**: their filters are now
available and provably shared, which is the VALIDATION row this plan owns, but the
templates that render them arrive in plans 30-10 and 30-12. Nothing in this plan renders a
status strip.

## Threat Model Outcomes

| Threat ID | Disposition | Outcome |
|---|---|---|
| T-30-36 | mitigate | Nothing closes until both threads confirm; otherwise the lifespan logs and returns with everything open for process exit |
| T-30-37 | mitigate | `request_stop()` before either join; asserted on recorded order, not timing |
| T-30-38 | mitigate | Zero `run_checks` calls across an empty lifespan, cache still cold |
| T-30-39 | mitigate | All eight filters return plain `str` (or `str | None` for `page_counts`); none is Markup-producing and no `|safe` was introduced |
| T-30-40 | accept | `app.state.checks` holds only developer-constant rows and timestamps |
| T-30-SC | accept | Nothing installed |

## Known Stubs

None. Every symbol this plan wires is called by the code it was wired into: the filters are
registered on the live environment, the cache and refresher are on `app.state`, and the
lifespan starts and stops the thread. The *templates* that will call the filters do not
exist yet — that is plan 30-10/30-12's work and is not a stub in this plan's surface.

## Verification

| Gate | Result |
|---|---|
| `uv run pytest tests/test_app_lifespan.py tests/test_web.py -q` | 90 passed |
| `uv run pytest -m "not browser and not sane_hardware" -q` | 2449 passed, no thread-leak warnings |
| `uv run pytest tests/test_browser.py -q` | 77 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 62 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |

Acceptance greps: `env.filters[` appears 11 times; `filters["local_time"] = local_time`,
`app.state.checks = `, `app.state.refresher = `, `refresher.start()` and `refresher.stop()`
each once; `refresher.start()` (line 247) follows `worker.start()` (line 243); `A-7` appears
three times in `app.py`.

## Commits

| Commit | Gate | Message |
|---|---|---|
| `5c64dc0` | RED | `test(30-09): add failing composition tests for filters and app.state` |
| `2b6068e` | GREEN | `feat(30-09): register the eight filters and inject the cache and refresher` |
| `dc56903` | RED | `test(30-09): add failing tests for the dual-thread shutdown gate` |
| `a1499af` | GREEN | `feat(30-09): start the refresher and gate shutdown on both threads` |

## TDD Gate Compliance

Both tasks ran RED before GREEN, and both RED commits were genuine: the first failed 5 of
6 with `AttributeError: 'State' object has no attribute 'refresher'`, the second failed 7
including `'CheckRefresher' object has no attribute 'request_stop'`. Neither could have
passed before its implementation.

Two tests passed inside their RED runs and were kept deliberately:
`test_the_existing_state_entries_and_filters_are_unchanged` (a regression guard on the five
earlier injections) and `test_startup_runs_no_check_probe` (a guarantee that must hold both
before and after the refresher is started — before, because nothing probed; after, because
nothing stamps a watcher).

## For the Next Plan

- `app.state.checks` and `app.state.refresher` are live. Routes reach them as
  `request.app.state.checks` / `.refresher` exactly as they reach `worker` and `cache`.
- Plan 30-11's routes must call `refresher.note_watcher()` — nothing calls it yet, so the
  refresher currently ticks and returns on the first guard for ever. That is correct today
  and becomes wrong the moment a strip is rendered without a stamp.
- All eight filters are registered, so a template may use `{{ result.state | check_state_class }}`
  with no further wiring.
- `refresher.request_stop()` exists if any future caller needs to signal without joining.

## Self-Check: PASSED

All five modified source files and the SUMMARY exist on disk; all four commit hashes
resolve in `git log`.
