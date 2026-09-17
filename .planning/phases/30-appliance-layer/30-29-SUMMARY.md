---
phase: 30-appliance-layer
plan: 29
subsystem: ui
tags: [htmx, fastapi, jinja2, playwright, single-flight, polling, threading]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the status strip, its cache, the lazy refresher's single-flight probe lock, the 2 s manual-refresh floor (30-25, 30-26) and the bounded cold-start poll (30-27)"
provides:
  - "CheckRefresher.probe_now returns whether it actually probed, so a caller can tell a probe from a collapse"
  - "CheckRefresher.probe_in_flight, a locked() read of the single-flight lock that never acquires it"
  - "CheckCache.release_manual_claim, so a collapsed click gives the manual-refresh floor back"
  - "a settling poll: the strip keeps asking while a probe is in flight, bounded by POLL_ATTEMPT_CAP"
  - "a measured non-reproduction verdict for R2-IN-04, with a guard on the htmx-config rule that causes it"
affects: [status strip, appliance layer, any future change to base.html's htmx-config meta]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a lock is observed with locked() and never acquired from a request thread"
    - "a probe reports 'did I get the lock', never 'did I store'"
    - "a browser measurement forces a status with page.route/route.fulfill rather than adding a failing route to the app"
    - "a measured browser property is guarded by asserting its mechanism, not only its effect"

key-files:
  created: []
  modified:
    - src/saneless/web/refresher.py
    - src/saneless/web/checks_cache.py
    - src/saneless/web/routes.py
    - src/saneless/web/templates/partials/checks.html
    - docs/reference/web-api.md
    - tests/test_refresher.py
    - tests/test_checks_cache.py
    - tests/test_web_checks.py
    - tests/test_browser.py

key-decisions:
  - "'Did this call probe' is 'did it get the lock', not 'did it store': the except arm probed and stored nothing and still returns True, because a caller that read it as a collapse would ask the page to wait for a result that is never coming"
  - "One render path in refresh_checks, not two: the context derives the trigger from probe_in_flight, which is still True on the collapse branch, so no second TemplateResponse and no poll_once context key exist"
  - "gave_up still requires an empty cache, so POLL_GAVE_UP_LINE stays cold-start-only and a settling poll that runs out of attempts keeps its last-checked line"
  - "release_manual_claim clears unconditionally; safe because only a granted caller calls it, microseconds later, and a refusal changes no state so nobody else can have overwritten the stamp"
  - "R2-IN-04 is closed as not reproducing rather than fixed: base.html's htmx-config swaps [45].. responses, measured at 1 request in 9 s, so there is no defect to fix"

patterns-established:
  - "Probe-report boolean: a single-flight entry point returns whether it took the flight, so the caller can compensate for a collapse instead of guessing"
  - "Observe-never-acquire: request-thread code reads Lock.locked() and never acquires, the same rule _checks_context already applied to the scanner gate"
  - "Claim-then-refund: a rate-limit claim taken before the work is discovered to be unnecessary is released on exactly the branch that did no work"
  - "Measure-and-guard: a review finding that does not reproduce is closed with a browser measurement plus an assertion on the mechanism that causes the non-reproduction"

requirements-completed: [APPL-02]

# Metrics
duration: 42min
completed: 2026-09-17
---

# Phase 30 Plan 29: Collapsed-Refresh Delivery and the Error-Path Poll Summary

**A Check again click that collides with a background probe now delivers that probe's result to the page with no second click and without spending the 2 s floor, and R2-IN-04 is closed as measured non-reproduction (1 request in 9 s) rather than fixed.**

## Performance

- **Duration:** ~42 min
- **Started:** 2026-09-17T17:33Z
- **Completed:** 2026-09-17T18:16Z
- **Tasks:** 3
- **Files modified:** 9

## Accomplishments

- `probe_now()` reports whether it probed, so `POST /api/checks/refresh` can tell a real probe from a collapse instead of guessing.
- A collapsed click renders a body that asks `/api/checks` for itself, so the in-flight probe's result reaches the page unaided. Before this, the strip was refetched only by another click, the terminal-state reload, or a page load — so the button could visibly do nothing.
- A collapsed click gives the manual-refresh claim back, so the very next click is honoured rather than refused. Before this, a collapse spent the floor for traffic nobody generated — the button doing nothing *twice*.
- The settling poll is bounded by the same `POLL_ATTEMPT_CAP` the cold start uses, on the same route-validated `attempt` parameter, so a probe wedged in an unbounded `getaddrinfo` cannot make a tab ask forever.
- The strip still never polls when nothing is in flight: with results present and no probe holding the lock, neither `GET /api/checks` nor the index carries any `hx-` request attribute.
- R2-IN-04 measured in Chromium against the application as it ships, with a guard on the `htmx-config` rule that makes it a non-issue.

## Task Commits

1. **Task 1: a collapsed probe is visible to its caller and does not spend the floor**
   - `f92a706` (test — RED: 12 failures, `probe_now` returned `None`, `probe_in_flight` and `release_manual_claim` absent)
   - `bbf4547` (fix — GREEN)
2. **Task 2: the strip keeps asking exactly as long as a probe is in flight**
   - `14c0799` (test — RED: 5 of 9 new tests failed, 4 were guards on unchanged properties)
   - `06beff6` (fix — GREEN)
3. **Task 3: measure IN-04 in a real browser and pin the reason it is not a defect**
   - `716e84e` (test — no RED/GREEN pair by design; the tests pass against unchanged source, and that is the finding)

## R2-IN-04: verdict and measurements

**Verdict: does not reproduce in this application. No source change was made, and none should be.**

The review reasoned that `POLL_ATTEMPT_CAP` cannot bound the poll when `/api/checks` answers a non-2xx, because "htmx does not swap on an error response", so the old polling body keeps its `every 2s` and the attempt count never reaches the server. That premise holds for htmx's **default** `responseHandling` and is false here. `src/saneless/web/templates/base.html:11-12` ships a customised `htmx-config` meta:

```
{"responseHandling":[{"code":"204","swap":false},{"code":"[23]..","swap":true},{"code":"[45]..","swap":true,"error":true}]}
```

The third rule — `{"code":"[45]..","swap":true,"error":true}` — is the whole of the non-reproduction. This application swaps 4xx and 5xx, so the error response replaces the polling body via its `outerHTML` swap and takes the trigger with it. The chain ends on the failure itself, one link in.

**Measured request counts** (Chromium, `tests/test_browser.py::TestPollEndsOnAnErrorResponse`, against the real app on `cold_strip_server` with the refresher stopped so no cache fill could end the chain; status forced with Playwright `page.route` + `route.fulfill`, never by adding a route to the application):

| Forced status | Requests to `/api/checks` | Observation window | Outcome |
|---|---|---|---|
| 500 | **1** | 9 000 ms after the swap (4.5 poll intervals) | `#checks-body` replaced by the error body; zero further requests |
| 422 | **1** | 9 000 ms after the swap (4.5 poll intervals) | identical — this is the review's hand-crafted-`attempt` case |

This reproduces the planner's standalone measurement (1 request in nine seconds) against the application itself. The count did not grow with the window: stillness is asserted separately from the count, so a poll that had merely paused would have fired four more times inside the window and been caught.

**Guard, not snapshot.** `test_the_htmx_config_meta_swaps_error_responses` reads `base.html` and asserts the `[45]..` rule still has `swap: true` and `error: true`. The status strip's poll has a dependency on a meta tag two files away; flipping that rule back to htmx's default would restore exactly the unbounded error-path poll the review described, and now something fails when it does.

## Cost the settling poll adds

The new disjunct in `_checks_context` — keep asking when `cached.results is None` **or** `probe_in_flight` — buys the one case WR-03 describes: results exist, but this render is of the pre-probe entry and the answer is about to be superseded, and nothing was going to fetch it.

What it costs: **a page render (or a strip fetch) that happens to land during a background probe issues a small, bounded number of extra requests before it settles.** Each is a cache read and never a probe, so it generates no scanner, saned or paperless-ngx traffic. The chain is bounded by `POLL_ATTEMPT_CAP` (10) on the same `attempt` parameter the route already validates with `Query(ge=0, le=POLL_ATTEMPT_CAP)` — measured in `test_the_settling_poll_chain_is_bounded_even_with_a_probe_stuck`, which holds the probe lock for the whole chain and still sees it end at 11 links (the bare route plus attempts 1..10). In practice a probe finishes in well under one 2 s interval, so the realistic cost is one extra cache read. The window in which a render can land on a probe at all is one probe's duration out of every 30 s TTL.

`probe_in_flight` is `Lock.locked()` — an observation, never an acquire — so no request thread can be parked behind the probe it is asking about (T-30-29-03).

## Files Created/Modified

- `src/saneless/web/refresher.py` — `probe_now`/`_probe_and_store` return `bool`; new `probe_in_flight` property; `_tick` drops the boolean explicitly.
- `src/saneless/web/checks_cache.py` — new `release_manual_claim()`, with the argument for why an unconditional clear is safe and cannot be abused to defeat the floor.
- `src/saneless/web/routes.py` — `_checks_context` keeps asking while a probe is in flight, still capped, with `gave_up` unchanged; `refresh_checks` releases the claim on a collapse and keeps its single render path.
- `src/saneless/web/templates/partials/checks.html` — stated fact 2 rewritten from "there is never a steady-state poll" to the property the code now has. The attribute block is unchanged: it already keys on `poll_attempt`.
- `docs/reference/web-api.md` — the collapsed refresh now has a stated delivery mechanism and a stated refund of the floor; `GET /api/checks` records that a failed poll request also ends the poll.
- `tests/test_refresher.py` — 8 new tests: the three `probe_now` outcomes, `_tick`'s unchanged contract, and four on `probe_in_flight` (idle, held, read from inside the probe, and no-acquire).
- `tests/test_checks_cache.py` — new `TestReleaseManualClaim`, 5 tests including grant→release→grant→refused.
- `tests/test_web_checks.py` — `_RecordingRefresher` forwards the real boolean and delegates `probe_in_flight`; new `TestCollapsedRefreshStillDelivers`, 9 tests.
- `tests/test_browser.py` — new `TestPollEndsOnAnErrorResponse` (500, 422, and the meta-rule guard) plus `_fail_the_checks_poll`.

## Decisions Made

- **The probe's boolean is about the lock, not the store.** A `run_checks` that raised returns `True`: the probe happened and stored nothing. Reading that as a collapse would make the handler ask the page to wait for a result that is never coming, and would ask the *page* to absorb a registry failure that last-known-good already handles.
- **One render path in `refresh_checks`.** The context derives the trigger from `probe_in_flight`, which is still `True` on the collapse branch because the other checker has not released yet — so the single existing render covers both outcomes. No second `TemplateResponse`, no `poll_once` context key (`grep -c poll_once` is 0 in both `routes.py` and `checks.html`), so there is nothing for a later reader to keep in step between route and template.
- **`gave_up` deliberately ignores the new fact.** `POLL_GAVE_UP_LINE` says the checks have not run yet, which beside five rows that did run would be a lie the strip tells about itself. A settling poll that runs out of attempts keeps its `Last checked HH:MM` line — pinned by `test_a_settling_poll_that_runs_out_keeps_its_last_checked_line`.
- **The release is unconditional.** Only a *granted* caller calls it, microseconds later on the same thread, and any competing claimer inside that window is refused by the interval — and a refusal changes no state, so no other thread can have overwritten the stamp this caller wrote.
- **The browser measurement forces the status from the browser side.** `page.route` + `route.fulfill`, so the application stays exactly as it ships. The page-level handler takes priority over the module's context-level egress gate only for same-origin `GET /api/checks` — requests the gate would have continued anyway — and `route.fallback()` hands everything else, `/api/checks/refresh` included, back to the gate. No egress an assertion would have caught can hide behind it.

## Deviations from Plan

None — plan executed exactly as written. No deviation rule fired; no auto-fix was needed.

Two small implementation details worth naming, neither a deviation from the plan's stated behaviour:

- `_tick` assigns the dropped boolean to `_` with a comment, rather than calling bare. The plan said `_tick`'s behaviour is unchanged, and it is; this only makes "the return value is deliberately discarded here" legible to the next reader.
- `poll_attempt` is expressed as `attempt + 1 if keep_asking and attempt < POLL_ATTEMPT_CAP else None` rather than as a chain that reuses `gave_up`. The plan specified exactly this shape ("`None` when not keeping asking or when `attempt >= POLL_ATTEMPT_CAP`"); `gave_up` remains, used only by the freshness line, which is what keeps the give-up sentence cold-start-only.

## Issues Encountered

- `probe_now`'s three outcomes had to be tested without a thread, because a thread would have made the overlap a matter of scheduling luck. Resolved with the idiom the suite already had: the test takes `_probe_lock` directly, which is what `_RecordingRefresher.probe_lock` was built for. No test in this plan sleeps or starts a thread; the tree-wide `time.sleep` count in `tests/` is still 17.
- `_RecordingRefresher` had to gain both new members in the **RED** commit. A stub still returning `None` from `probe_now` would have made the route take the collapse branch on every call, and every collapse test would have passed for entirely the wrong reason.
- The module's own rule (`_record_checks_requests`' docstring) is that the egress gate is the one handler allowed to decide a request's fate. Task 3 needs a second handler, so the exception is argued in `_fail_the_checks_poll`'s docstring and narrowed with `route.fallback()` to same-origin `GET /api/checks` alone.

## Verification

All of the plan's `<verification>` gates were run in this worktree:

- `uv run pytest -q` — **3050 passed**, above the 3025 baseline (+25: 8 refresher, 5 cache, 9 web-checks, 3 browser).
- `uv run pytest -q -m browser` — **120 passed** before Task 3, **123** after; offline, every context's egress gate recorded nothing.
- `uv run pytest -q -m "not browser"` — 2927 passed.
- `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check src tests` — clean (pyrefly: 0 errors).
- `uv run prek run --all-files` — all hooks passed.
- `ls src/saneless/web/static/` — no `.js` but the vendored `htmx-2.0.8.min.js`; no client script added.
- `grep -rc "time\.sleep" --include='*.py' tests/ | awk -F: '{s+=$2} END {print s}'` — **17**, unmoved.
- `grep -rn "# noqa\|# type: ignore"` over `src/saneless/web/` and the four touched test files — **no matches**.
- `uv run pytest tests/test_app_lifespan.py -q` — 24 passed, with no edit to `_ROUTE_CALLS` or `_ROUTE_SKIPS`: no route was added or removed.
- `uv run pytest tests/test_checks_cache.py -q -k "claim or release"` — 16 selected, all passed.
- `grep -c "def release_manual_claim" src/saneless/web/checks_cache.py` — 1. `grep -c "def probe_in_flight" src/saneless/web/refresher.py` — 1, with no `acquire` call in the property body.
- `grep -c "poll_once"` in `routes.py` and `checks.html` — 0 in both.

## Known Stubs

None. Every code path this plan added is reached by a test, and no placeholder, empty default or "coming soon" string was introduced.

## Threat Flags

None. No new network endpoint, auth path, file access pattern or schema was introduced; no route was added or removed (`test_app_lifespan.py` unchanged). Every disposition in the plan's threat register is mitigated or, for T-30-29-05, closed as non-reproducing with a guard:

- **T-30-29-01** (release weakening the floor) — pinned by `TestReleaseManualClaim::test_a_release_then_a_grant_leaves_the_new_grants_floor_standing` and by the route test that three clicks inside one interval still cost exactly one probe.
- **T-30-29-02** (settling poll unbounded) — pinned by the at-cap test and the held-lock chain test.
- **T-30-29-03** (render contending for the probe lock) — `locked()` only; pinned by `test_reading_probe_in_flight_does_not_take_the_probe_lock`.
- **T-30-29-04** (collapse observable in the response) — the collapse branch renders the same partial from the same cache read; the only difference is that the body asks again, which a cold start also does.
- **T-30-29-05** (error-path poll) — measured non-reproduction, guarded on the meta rule.
- **T-30-29-SC** — no package installed; htmx stays on the vendored 2.0.8 with its SRI pin.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- R2-WR-03 is closed on all three of the consequences the review named, each with a test that fails against the parent commit.
- R2-IN-04 is closed as measured non-reproduction with a guard; it should not be re-raised in a later review round.
- `probe_in_flight` is a general-purpose reader. Any future surface that wants to know whether the appliance is mid-probe can read it without acquiring anything; the rule it follows ("observe, never acquire") is the one `_checks_context` already applied to the scanner gate.
- One standing dependency is now explicit and guarded: the strip's poll behaviour on a failed request is decided by `base.html`'s `htmx-config`. A future change to that meta must keep the `[45]..` rule's `swap: true` or the error-path poll becomes unbounded again.

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-17*

## Self-Check: PASSED

All nine modified files exist on disk. All six commits exist in this worktree's history on top of `d3f20a6`: `f92a706`, `bbf4547`, `14c0799`, `06beff6`, `716e84e`, `e1cc09c`. Working tree clean — no modified or untracked files left behind.
