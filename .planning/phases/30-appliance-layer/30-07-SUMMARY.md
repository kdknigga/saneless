---
phase: 30-appliance-layer
plan: 07
subsystem: appliance-health
tags: [ttl-cache, injectable-clock, last-known-good, daemon-thread, stop-event, bounded-join, scanner-gate, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "checks.py — CheckResult, CheckContext, run_checks (plan 30-06)"
  - phase: 30-appliance-layer
    provides: "ScanWorker.scanner_gate (plan 30-04)"
  - phase: 26-worker-and-web-robustness
    provides: "STOP_JOIN_SECONDS and the ScanWorker daemon/Event/bounded-join precedent (D-07, D-08)"
  - phase: 24-web-ui
    provides: "web/cache.py MetadataCache, the TTL/lock shape this copies and deviates from"
provides:
  - "src/saneless/web/checks_cache.py — CheckCache, one entry, 30 s TTL, injectable monotonic clock"
  - "CachedChecks — frozen snapshot carrying results, an aware checked_at, stale and age_seconds"
  - "CheckCache.current() — never probes, never discards; what a page render reads (D-04)"
  - "CheckCache.store(results) / CheckCache.is_fresh() — the refresher's write and guard"
  - "src/saneless/web/refresher.py — CheckRefresher, the lazy daemon thread that fills the cache"
  - "CheckRefresher.note_watcher() — the request-thread stamp that opens the 90 s watch window (D-05)"
  - "CheckRefresher._tick() — the whole refresh policy, synchronous and directly callable"
  - "CheckRefresher.start() / stop() -> bool — ScanWorker's four stop properties, shared STOP_JOIN_SECONDS"
  - "WATCH_WINDOW_SECONDS (90.0) and TICK_SECONDS (1.0) — module tunables read at call time"
affects: [30-08, 30-09, 30-11, 30-12]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "injectable clock as the alternative to a sleeping test: the TTL source is a constructor parameter, so every expiry assertion advances a float and the suite gains no wall-clock time"
    - "last-known-good cache: an expired entry is flagged stale and returned unchanged rather than dropped, so a paused or failed refresh degrades to 'here is the previous answer and its age' instead of a blank surface"
    - "one frozen _Entry rebound under one lock: results and both timestamps are replaced as a single immutable value, so a concurrent reader cannot observe a half-updated cache"
    - "synchronous loop body: the thread's _run is a three-line Event.wait loop over a plain _tick() method, so policy tests need no thread and the threaded test asserts only lifecycle"
    - "non-blocking gate probe as a feature flag: acquire(blocking=False) failing is translated into skip_scanner rather than into waiting, so exclusion with a long-running scan costs nothing"

key-files:
  created:
    - src/saneless/web/checks_cache.py
    - src/saneless/web/refresher.py
    - tests/test_checks_cache.py
    - tests/test_refresher.py
  modified: []

key-decisions:
  - "_last_watched is `float | None` with a None sentinel, not the plan's `0.0`. time.monotonic() on Linux counts from boot, so for the first 90 seconds of an appliance's life a 0.0 sentinel sits inside the watch window and the refresher would probe with nobody watching — precisely the case D-05 exists to prevent, and precisely when a container-started appliance is running. The None sentinel makes 'nobody has ever watched' unrepresentable as a timestamp"
  - "CachedChecks gained a fourth field, age_seconds, beyond the three the plan named. The behaviour block requires a stale entry to report 'an age of 30.1 seconds' and neither the monotonic stamp (not exposed) nor checked_at (wall clock, which can step) can yield it without the renderer re-deriving what the cache already knows"
  - "stale is False at cold start rather than True. stale is a statement about stored results; with results None there is nothing to be stale, and a renderer keying off `results is None` for D-06's Checking… rows would be misled by a stale flag on an entry that has no timestamp to show"
  - "The TTL boundary is `age >= ttl`, matching MetadataCache's `elapsed < ttl` freshness test exactly, so the two caches in web/ do not disagree about what 'exactly at the TTL' means"
  - "The sleeping call is never spelled literally in any of the four files, not even inside a docstring quoting tests/test_cache.py. The phase gate is a grep, and prose that names the thing it forbids defeats the grep; the prose says 'sleep for 1.1 real seconds' and cites the line number instead"
  - "The autouse cleanup fixture in tests/test_refresher.py is function-scoped, not module-scoped as the plan suggested. Module scope would let a leaked thread survive into every later test in the file, which is most of what the fixture exists to prevent"
  - "run_checks is imported at module level and spied via monkeypatch rather than injected as a fifth constructor parameter. The plan fixed the constructor at four parameters, and an injected registry would let the lifespan wire a refresher to something other than the one registry D-02 requires both surfaces to read"

patterns-established:
  - "A test module docstring that names the counter-example it inverts, with file and line, so the next author sees why the cheaper habit was rejected here"
  - "Barrier-synchronised concurrency assertion: threading.Barrier(n) forces the contended moment deterministically, replacing a sleep-and-hope race window"
  - "A callable class as a spy (`_RunChecksSpy`) recording the argument objects themselves, so `skip_scanner` can be asserted on the context that was actually built rather than inferred from behaviour"

requirements-completed: [APPL-02]

# Metrics
duration: 27min
completed: 2026-09-16
---

# Phase 30 Plan 07: The Check Cache and the Lazy Refresher Summary

**A 30-second TTL cache whose clock is a constructor parameter and which keeps the previous answer with its age, plus a daemon thread that refills it only while a page is being looked at and never while the worker holds the scanner.**

## Performance

- **Duration:** 27 min
- **Tasks:** 2 (both TDD, four commits)
- **Files created:** 4
- **Tests added:** 26 (12 cache, 14 refresher), all passing in 0.14 s combined

## What Was Built

### `src/saneless/web/checks_cache.py`

`CheckCache` holds one entry — the five checks are run and shown together, so there is
nothing to key on — and hands out a frozen `CachedChecks` carrying `results`,
`checked_at`, `stale` and `age_seconds`. `current()` never probes and never discards,
which is what makes D-04 true: a page render reads it while the scanner host is unplugged
and pays nothing.

The class docstring names `MetadataCache` as the shape it copies and states the three
deliberate deviations in order:

1. **The clock is a constructor parameter.** `cache.py:54,66` call `time.monotonic()`
   inline, which is the only reason `tests/test_cache.py:28` has to sleep for 1.1 real
   seconds. Here the TTL is asserted by advancing a float.
2. **It keeps last-known-good.** `MetadataCache.get_or_fetch` re-raises and caches nothing
   (Pitfall 6); D-08 needs the previous results *and* their age retained so the strip can
   say "Paused during scan — last checked 14:02".
3. **It is typed to `CheckResult`,** not `list[dict[str, object]]`, so the cached value is
   immutable all the way down.

One lock guards the whole rebind of a frozen `_Entry`, with a comment saying exactly that
and nothing else — so a concurrent reader can never see results from one store paired with
timestamps from another (T-30-30).

### `src/saneless/web/refresher.py`

`CheckRefresher` is the lazy daemon thread. `_run()` is
`while not self._stopping.wait(TICK_SECONDS): self._tick()`, so `stop()` wakes it at once
rather than after a full tick — the same property `queue.shutdown(immediate=True)` gives
the worker.

`_tick()` holds the entire policy and is a plain synchronous method:

1. Return if nobody has watched within `WATCH_WINDOW_SECONDS` (90.0, three TTL cycles
   after the operator walks away — D-05, T-30-26).
2. Return if the cache is still fresh (D-03, T-30-26).
3. Try `gate.acquire(blocking=False)`; build the context with
   `skip_scanner=not acquired`; run the checks; store; release in `finally`
   (D-08, T-30-27).
4. A raising `run_checks` is logged with no text reaching the cache or the page, and the
   previous entry stays (T-30-29).

`stop()` copies all four `ScanWorker.stop` properties — set the event first, join only if
alive, recompute liveness after the join, return the boolean, log on success — and imports
`STOP_JOIN_SECONDS` from `saneless.worker` rather than redefining it (T-30-28).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 — Missing correctness guard] `_last_watched` uses a `None` sentinel, not `0.0`**

- **Found during:** Task 2 GREEN, while writing the "no watcher stamped does nothing" guard
- **Issue:** The plan specified `self._last_watched: float = 0.0`. `time.monotonic()` on
  Linux counts from boot, so during the first 90 seconds of uptime `now - 0.0` is inside
  `WATCH_WINDOW_SECONDS` and the refresher would probe Paperless and the scanner with
  nobody watching. That is exactly the behaviour D-05 forbids, occurring exactly when a
  container-started appliance boots — the most likely deployment shape for this project.
- **Fix:** `self._last_watched: float | None = None`, with `_tick()` returning immediately
  on `None` and a comment giving the boot-time reasoning.
- **Files modified:** `src/saneless/web/refresher.py`
- **Commit:** 357aeb4

**2. [Rule 1 — Test bug] Float comparison in the age assertion**

- **Found during:** Task 1 GREEN
- **Issue:** `100.0 + 30.1` is `130.09999999999994`, so `age_seconds == 30.1` failed.
- **Fix:** `pytest.approx(30.1)`.
- **Files modified:** `tests/test_checks_cache.py`
- **Commit:** 3584a71

### Deliberate Additions

**3. `CachedChecks.age_seconds`** — the plan's field list named three fields, but the
behaviour block requires a stale entry to report "an age of 30.1 seconds". Neither the
monotonic stamp (private) nor `checked_at` (wall clock, steppable) can yield it without a
renderer re-deriving what the cache already computed.

**4. Literal spelling of the sleeping call removed from all four files' prose.** The plan's
acceptance criteria are greps (`grep -c` must be 0 / `grep -rn` must be empty), and
docstrings quoting `tests/test_cache.py`'s call defeated them. The prose now cites the
behaviour and the line number without the token, which keeps the phase-wide "count must not
rise" gate checkable by grep.

**5. The cleanup fixture is function-scoped, not module-scoped.** Module scope would let a
leaked thread survive into every later test in the file — most of what the fixture is for.

## Verification

| Check | Result |
|-------|--------|
| `pytest tests/test_checks_cache.py -q` | 12 passed in 0.13 s |
| `pytest tests/test_refresher.py -q` | 14 passed in 0.12 s |
| `pytest -m "not browser and not sane_hardware" -q` | 2411 passed, 74 deselected, 57.9 s, no thread-leak warnings |
| `ruff check .` | All checks passed |
| `ruff format --check .` | 61 files already formatted |
| `ty check` | All checks passed |
| `pyrefly check src tests` | 0 errors |
| `grep -rn "time.sleep"` over the four plan files | empty |
| `grep -rn "time.sleep" tests/` | 17 — unchanged from the phase baseline |

Acceptance greps:

| Criterion | Actual |
|-----------|--------|
| `clock: Callable[[], float] = time.monotonic` in `checks_cache.py` | 1 |
| `ttl: float = 30.0` in `checks_cache.py` | 1 |
| `checked_at` in `checks_cache.py` | ≥ 2 (7) |
| `from saneless.worker import STOP_JOIN_SECONDS` in `refresher.py` | 1 |
| `daemon=True` in `refresher.py` | 1 |
| `self._stopping.wait(` in `refresher.py` | 1 |
| `def _tick(self) -> None:` in `refresher.py` | 1 |
| `def stop(self) -> bool:` in `refresher.py` | 1 |

## TDD Gate Compliance

Both tasks ran RED → GREEN with the gates visible in `git log`:

| Commit | Gate |
|--------|------|
| 0ad87dd `test(30-07): add failing tests for the check cache` | RED (ModuleNotFoundError) |
| 3584a71 `feat(30-07): add the check cache with an injectable clock` | GREEN |
| b5096f3 `test(30-07): add failing tests for the lazy check refresher` | RED (ModuleNotFoundError) |
| 357aeb4 `feat(30-07): add the lazy check refresher thread` | GREEN |

No REFACTOR commit was needed for either task.

## Known Stubs

None. Both modules are complete and fully wired to their dependencies. They are not yet
*called* by anything — `app.py` builds neither and no route stamps `note_watcher()` — but
that wiring is plan 30-09's and plan 30-08's work respectively, exactly as the phase
sequences it.

## Threat Flags

None. No new network endpoint, auth path, file access or schema change was introduced;
both modules are in-process service objects whose only outward call is the `run_checks`
registry plan 30-06 already covered.

## Self-Check: PASSED

All four created source files exist on disk and all four commit hashes cited above resolve
in this worktree's history.

## Notes for Downstream Plans

- **30-08 (strip rendering):** read `CheckCache.current()`. `results is None` is D-06's
  cold start (render `CHECKING_*` rows); `stale is True` with `results` present is D-08's
  "Paused during scan — last checked HH:MM", and `checked_at` is the aware UTC datetime to
  localise for that line. `age_seconds` is there if a relative phrasing is wanted.
- **30-09 (lifespan wiring):** build the `CheckCache` and the `CheckRefresher` in
  `create_app`, put them on `app.state` as `checks` and `refresher`, call
  `refresher.start()` after `worker.start()`, and call `refresher.stop()` *before*
  `worker.stop()` in shutdown so no probe is in flight when `sane_exit()` runs. The
  context factory closes over `settings`, the scanner, the Paperless client and
  `worker.profile_storage`; the gate callable is `lambda: worker.scanner_gate`, deliberately
  late-bound. `stop()` returning `False` means a probe is still running and the Paperless
  client and scanner must be left open.
- **Refresh button (D-09):** the button's route should call `note_watcher()` and then force
  a probe. `_tick()` will not do it while the cache is fresh — a bypass path (or a cache
  `invalidate()`) is 30-08/30-11's to add.
