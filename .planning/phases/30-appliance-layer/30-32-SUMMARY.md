---
phase: 30-appliance-layer
plan: 32
subsystem: ui
tags: [htmx, fastapi, jinja2, playwright, error-handling, polling, asvs-v7]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the status strip, its bounded cold-start poll and POLL_ATTEMPT_CAP (30-27), the settling poll and the htmx-config meta guard (30-29)"
provides:
  - "errors.CHECKS_POLL_TARGET_ID, the one swap target exempt from the htmx error retarget"
  - "render_error suppresses HX-Retarget/HX-Reswap for a request whose HX-Target is the strip's swap target, so a failing poll replaces the polling element and the chain ends"
  - "GET /api/checks clamps its own attempt counter instead of bounding it at the Query, so an out-of-range counter is a trigger-free 200 rather than a 422"
  - "routes._checks_fallback_context, the developer-constant cold strip a failed render falls back to"
  - "a browser measurement of the failing poll driven by the application's own response, which fails when the exemption is removed"
affects: [status strip, the app-wide htmx error contract, any future polling element]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "an htmx response header rule may carry one named exemption, keyed off the HX-Target request header and pinned to the template's id by a test"
    - "a route that a polling element targets absorbs its own ordinary failures into a trigger-free 200 instead of handing them to the error renderer"
    - "a browser measurement tampers the request with route.continue_ and lets the application answer, rather than fulfilling it with a hand-written body"
    - "a request-parameter range is clamped inside the handler when the out-of-range response would itself be harmful; the type is still validated at the boundary"

key-files:
  created: []
  modified:
    - src/saneless/web/errors.py
    - src/saneless/web/routes.py
    - tests/test_web_errors.py
    - tests/test_web_checks.py
    - tests/test_browser.py
    - docs/reference/web-api.md

key-decisions:
  - "The fix is the response-header exemption, not a client-side hx-on::response-error: htmx 2.0.8's ct() poll loop re-arms from a closure and never re-reads hx-trigger, so removing the attribute is a no-op against an already-armed poll"
  - "The route hardening is kept as hardening, not as the fix: it cannot cover the 422 (rejected during request validation, before the handler runs) nor a 405/429, but it is the better answer for the failures it can cover because it leaves the five rows and the Check again button on the page"
  - "The Query range bound is replaced by an in-handler clamp; the property it defended (a crafted value never reaches _checks_context and is never rendered into a body) is unchanged, only the failure mode is"
  - "The htmx-config [45].. meta rule stays and is restated as load-bearing FOR the fix rather than as evidence there was never a defect"
  - "A transport-level failure (no response at all) is an accepted residual, recorded in the code, in the plan's decision record and in the shipped docs rather than left for a fourth review round"

patterns-established:
  - "Named-exemption-with-a-pin: one element may be exempted from an app-wide response rule, provided a test asserts the exempt id is the id the template actually ships"
  - "Drive-the-failure-through-the-server: a browser measurement of an error path tampers the request and lets the app build the response, so the headers under test are the ones production sends"
  - "Fallback-of-constants: a guarded render falls back to a context of developer-authored constants only, with the exception going to logger.exception and nowhere near the body"

requirements-completed: [APPL-02]

# Metrics
duration: 21min
completed: 2026-09-17
---

# Phase 30 Plan 32: The Checks Poll's Own Failure Ends the Checks Poll Summary

**A failing `GET /api/checks` is now the one htmx error in this application that is not retargeted into `#status-message`, so it replaces `#checks-body`, the poll chain ends with it, and the scan-progress slot is left alone — measured in Chromium against the response the application really sends, and the measurement fails when the exemption is removed.**

## Performance

- **Duration:** ~21 min
- **Started:** 2026-09-17T20:20 CDT (base commit `2b3cced`)
- **Completed:** 2026-09-17T20:41 CDT
- **Tasks:** 3
- **Files modified:** 6

## Accomplishments

- `render_error` no longer sets `HX-Retarget`/`HX-Reswap` when the request's `HX-Target` is `checks-body`. Every response-bearing failure path of the strip's poll — 422, 500, 404, 405, 429 — is now swapped by the polling element's own `hx-target="this" hx-swap="outerHTML"`, which detaches `#checks-body` and ends the chain.
- `#status-message` is no longer written by a failing checks poll, so an in-progress scan's progress line cannot be replaced by a generic error sentence every two seconds (D-03).
- Every other htmx error response still retargets to `#status-message` exactly as before. The exemption is one condition keyed off one id, pinned to the template by a test.
- `GET /api/checks` clamps its own `attempt` instead of bounding it at the `Query`: `attempt=99` and `attempt=-5` are 200s, the first trigger-free and the second the start of a fresh chain. A value that is not an integer is still a 422 — and with the exemption in place that 422 ends the poll too.
- A failure inside the watcher stamp or the context build is caught and rendered as `_checks_fallback_context()`: five cold rows, `POLL_GAVE_UP_LINE`, the `Check again` button, no trigger, and nothing of the exception on a page the whole LAN can read (ASVS V7).
- `TestPollEndsOnAnErrorResponse` was rebuilt around a server-produced failure and now **fails when the exemption is reverted**, which the previous version could not do.
- `docs/reference/web-api.md` states the two real endings of a failing poll and the one residual, and the disproved sentence is gone.

## Task Commits

1. **Task 1: the strip's own swap target is exempt from the error retarget**
   - `7ce9b0a` (test — RED: 15 of 19 new cases failed, `errors.CHECKS_POLL_TARGET_ID` did not exist)
   - `75b4c53` (fix — GREEN)
2. **Task 2: the checks route stops producing errors for its own ordinary failures**
   - `23816b5` (test — RED: 8 failures across the rewritten `TestBoundedPoll` cases and the new `TestTheStripSurvivesItsOwnFailure`)
   - `0d06eed` (fix — GREEN)
3. **Task 3: measure the ending against the app's own response, and correct the docs**
   - `65e0e44` (test + docs — no separate RED/GREEN pair: the class is a rewrite of an existing measurement over source already fixed by tasks 1–2; its RED evidence is the revert mutation below)

No REFACTOR commit was needed: neither implementation left anything to clean up.

## R3-CR-02: the defect, the mechanism, and the measurement

**The defect was real.** `render_error` set `HX-Retarget: #status-message` on *every* htmx error response. In the vendored `htmx-2.0.8.min.js` the retarget is applied to the response's target **before** the swap decision is taken:

```
if(T(n,/HX-Retarget:/i)){e.target=Un(t,n.getResponseHeader("HX-Retarget"))}
...
if(m.shouldSwap){if(n.status===286){lt(t)}...}
```

So a 4xx/5xx from `GET /api/checks` was written into `#status-message`; `#checks-body` was never replaced, kept its `hx-trigger="every 2s"`, and went on polling — and failing — for the life of the tab, overwriting the scan-progress slot twice a minute.

**Why the attribute-removal fix was rejected.** Read from the same bundle: `ct(e,t,n)` re-arms itself from a closure (`r.timeout=setTimeout(function(){if(se(e)&&r.cancelled!==true){...;ct(e,t,n)}},n.pollInterval)`); `se(e)` is `e.getRootNode({composed:true})===document`; `r.cancelled` is set only by `lt(e)`, called in exactly one place, the `status===286` branch. The loop never re-reads `hx-trigger`, so `hx-on::response-error="this.removeAttribute('hx-trigger')"` is a no-op against an already-armed poll.

**The header htmx sends.** `mn(e,t,n)` builds `{"HX-Request":"true",...,"HX-Target":a(t,"id"),...}` — the target element's id with **no leading `#`**. The strip's polling body carries `hx-target="this"` on `<div id="checks-body">`, so its poll requests arrive with `HX-Target: checks-body`, which is exactly what `CHECKS_POLL_TARGET_ID` is compared against.

**Measured, in Chromium, against the application's own response** (`tests/test_browser.py::TestPollEndsOnAnErrorResponse`, on `cold_strip_server` with the refresher stopped so no cache fill can end the chain):

| Case | Mechanism | Observation window | Outcome |
|---|---|---|---|
| tampered `attempt` → real 422 from `render_error` | `route.continue_(url=...)`, so the server builds the response | 9 000 ms after the swap (4.5 poll intervals) | `#checks-body` count 0, the app's own error partial in `#checks-strip`, request count unchanged and below `POLL_ATTEMPT_CAP` |
| the same response's headers | `page.on("response")` recorder | — | status 422, no `hx-retarget`, no `hx-reswap` |
| `#status-message` across the window | text captured at the swap and re-read after | 9 000 ms | unchanged; no `.status-error` inside the slot |

**Why the previous measurement could not fail.** It answered the poll from inside the browser with a hand-written body carrying **no response headers at all**. With no `HX-Retarget` on it, the fabricated failure swapped exactly as the class expected — while the application's own failure did not. The class concluded "there is no defect" from a response this application never sends. That sentence is gone (`grep -c "there is no defect" tests/test_browser.py` → 0) and no test in the class fulfils a route any more.

## Mutation evidence (run by hand, recorded as the plan requires)

| Mutation | Test | Observed |
|---|---|---|
| Restore the unconditional `headers["HX-Retarget"]`/`["HX-Reswap"]` assignment in `render_error` | `test_a_checks_poll_error_is_not_retargeted` | **12 failed, 0 passed** — `AssertionError: assert 'hx-retarget' not in Headers({'retry-after': '30', 'hx-retarget': '#st...` |
| Change `CHECKS_POLL_TARGET_ID` to `"checks-body-mutant"` | `test_the_exempt_id_is_the_id_the_strip_template_ships` | **1 failed** — `assert 'id="checks-body-mutant"' in '<div id="checks-body"\n     hx-get="/api/checks?attempt=...` |
| Restore `Query(ge=0, le=POLL_ATTEMPT_CAP)` on `get_checks` | `test_an_attempt_outside_the_bound_ends_the_chain_instead_of_erroring`, `test_a_negative_attempt_is_the_start_of_a_chain`, `test_a_crafted_attempt_never_reaches_the_context_or_the_body` | **3 failed** — `assert 422 == 200` |
| Remove the `try`/`except` from `get_checks` | `TestTheStripSurvivesItsOwnFailure` (all cases) | **5 failed** — `RuntimeError: zz-checks-boom` propagates out of the handler; no response is produced at all |
| **Revert task 1's exemption (headers set unconditionally), with tasks 2–3 in place** | `TestPollEndsOnAnErrorResponse` in Chromium | **3 failed, 1 passed** — `test_a_failed_poll_request_replaces_the_strip_and_stops`, `test_the_failing_poll_response_carries_no_retarget` and `test_the_failing_poll_never_writes_the_status_message_slot` all fail with `AssertionError: Locator expected to have count '1' / Actual value: 0` for `#checks-strip .status-error`: the error body never reaches the strip because it is retargeted into `#status-message`, so `#checks-body` survives. The one case that still passes is `test_the_htmx_config_meta_swaps_error_responses`, which reads `base.html` and is correctly independent of `errors.py`. |

The exemption was restored after each mutation and the suite re-run green before any commit; `git status` was verified clean of stray mutation edits.

## Files Created/Modified

- `src/saneless/web/errors.py` — new `CHECKS_POLL_TARGET_ID` (exported) with the mechanism in a comment: htmx sends the id with no `#`, and an armed poll ends only by the element leaving the DOM or a 286, never by losing an attribute. `render_error` sets the two headers only when `request.headers.get("HX-Target") != CHECKS_POLL_TARGET_ID`; the template, context keys, status code, `extra_headers` and `refresh_history` derivation are untouched. Module docstring, function docstring and the `Returns:` section all name the exemption, the htmx 2.0.8 ordering it rests on, and `base.html`'s `[45]..` rule.
- `src/saneless/web/routes.py` — `get_checks` takes `Annotated[int, Query()]` and clamps with `min(max(attempt, 0), POLL_ATTEMPT_CAP)` before anything reads it; the watcher stamp and the context build are wrapped in one `try`/`except Exception` logging with `logger.exception`; the `TemplateResponse` call stays outside the guard, so there is one render path and one status code. New module-private `_checks_fallback_context()` returning only developer constants, with a docstring saying why each value is what it is.
- `tests/test_web_errors.py` — new `TestChecksPollTargetIsExemptFromTheRetarget` (19 cases): the exempt target across every `RequestRejection`, `Retry-After` and `Allow` surviving the exemption, every other target and every plain request unchanged, and the template-id pin. Header assertions are on `response.headers` membership, never on a rendered body.
- `tests/test_web_checks.py` — `TestBoundedPoll`'s 422-on-crafted-attempt case rewritten into four: the out-of-range ending, the negative-attempt start, the never-reaches-the-context/body property (with a recording `_checks_context`), and the still-422 type check. New `TestTheStripSurvivesItsOwnFailure` (5 cases) over both halves of the guarded region, including an explicit absence check for `"Traceback"`, `"RuntimeError"`, `".py"` and the injected marker, plus a `caplog` assertion that the traceback *is* logged.
- `tests/test_browser.py` — `_fail_the_checks_poll` and `_CHECKS_ERROR_BODY` deleted; new `_tampered_url`, `_tamper_the_checks_poll` (tampers and passes on, keeping the `route.fallback()` arm and its egress-gate justification) and `_record_checks_responses`. `TestPollEndsOnAnErrorResponse` rebuilt: the swap-and-stillness case, the response-header case, the `#status-message`-untouched case, and the meta-rule guard with its docstring corrected.
- `docs/reference/web-api.md` — the "A poll whose request fails also ends… the failure response replaces the strip" sentence replaced by the two real endings and the accepted residual.

## Decisions Made

- **The exemption is the fix; the route hardening is hardening.** The exemption is one condition in one function and covers every response-bearing failure path at once, including the 422 that FastAPI raises during request validation before the handler body runs. The route guard cannot cover that 422, nor a 405/429, so it could not have been the fix — but it is the better answer for what it *can* cover, because it leaves the five rows and the button on the page instead of an error box.
- **Client-side attribute removal was rejected on the evidence, not on taste.** The bundle was read: the poll loop re-arms from a closure and never re-reads `hx-trigger`. Shipping that would have been a third round of "a mechanism that reads as a fix and is not one".
- **`HX-Target` is client-supplied, and that is accepted (T-30-32-04).** A client can opt *itself* out of the retarget. The only effect is which region of that client's own page receives its own error body; the body is identical either way, no server state is reachable, and no other client is affected.
- **The clamp replaces a range bound, never the type bound.** `attempt=abc` is still a 422. What changed is that an out-of-range *number* now ends the chain quietly, which is what the strip wanted anyway, instead of erroring into a renderer whose headers used to keep the poll alive.
- **The residual is stated, not hidden.** A transport-level failure produces no response, so neither mechanism fires; that tab keeps asking an origin that is not answering. It overwrites nothing and generates no scanner or paperless-ngx traffic. Closing it would need a client-side timer this application has no other reason to own. It is recorded in `errors.py`'s neighbourhood via the plan's decision record, and in the shipped docs.

## Deviations from Plan

None — plan executed exactly as written. No deviation rule fired; no auto-fix was needed.

Two wording-level implementation details, neither a change of behaviour:

- `get_checks`' docstring describes the clamp as "the clamp into `0..POLL_ATTEMPT_CAP`" rather than quoting the expression a second time, so the plan's acceptance grep (`grep -c "min(max(attempt"` is exactly 1) reads the code and not a docstring echo of it.
- `TestPollEndsOnAnErrorResponse`'s class docstring says the old measurement "fulfilled the poll from inside the browser" rather than naming `route.fulfill`, so the plan's acceptance grep (no `route.fulfill` inside the class bounds) matches nothing at all inside the class. The one remaining `route.fulfill` in the module is at line 2562, far outside the class.

## Issues Encountered

- The rebuilt browser class needed a failure the *server* produces, and the application ships no failing route. Resolved with `route.continue_(url=...)` and a tampered `attempt`: FastAPI's own validation produces the 422 and `render_error` builds it, so the headers under test are production's. The page-level route handler is still narrowed with `route.fallback()` to same-origin `GET /api/checks` only — `/api/checks/refresh` shares the prefix and is handed back to the module's egress gate.
- Typing the recording `_checks_context` stand-in without a suppression required importing `starlette.datastructures.State` into the test module's `TYPE_CHECKING` block. No `# type: ignore` or `# noqa` was added anywhere in this plan.
- `TestTheStripSurvivesItsOwnFailure` fails in the RED state by the injected `RuntimeError` propagating out of `TestClient` rather than by a status-code mismatch, because this module's client re-raises server exceptions. That is still an honest RED and the same tests turn green on the guard alone.

## Verification

Every gate in the plan's `<verification>` block was run in this worktree:

- `uv run pytest tests/test_web_errors.py tests/test_web_checks.py -q` — **257 passed**.
- `uv run pytest tests/test_browser.py -q` — **133 passed** (offline; every context's egress gate recorded nothing).
- `uv run pytest -q` — **3096 passed**, 0 failed, 0 skipped.
- `uv run ruff check .` — clean.
- `uv run ruff format --check .` — 63 files already formatted.
- `uv run ty check` — all checks passed.
- `uv run pyrefly check src tests` — 0 errors.
- `uv run prek run --all-files` — every hook passed.
- `uv run prek run --stage pre-push --all-files` — every hook passed, including `ty check` (full) and `pyrefly check src tests`.
- The revert-mutation for task 3 was executed and its failure observed before the exemption was restored; the observed failure text is in the mutation table above.

Acceptance greps:

- `grep -c "CHECKS_POLL_TARGET_ID" src/saneless/web/errors.py` → **6** (≥ 3 required).
- `grep -n "HX-Retarget" src/saneless/web/errors.py` → the one assignment is inside the conditional naming `CHECKS_POLL_TARGET_ID`; the unconditional form is gone.
- `grep -n "le=POLL_ATTEMPT_CAP" src/saneless/web/routes.py` → **no matches**.
- `grep -c "min(max(attempt" src/saneless/web/routes.py` → **1**.
- `grep -c "_checks_fallback_context" src/saneless/web/routes.py` → **3** (≥ 2 required).
- `grep -n "route.fulfill" tests/test_browser.py` → one match at line 2562, outside `TestPollEndsOnAnErrorResponse` (class spans 3273–3409).
- `grep -c "there is no defect" tests/test_browser.py` → **0**.
- `grep -c "the failure response replaces the strip" docs/reference/web-api.md` → **0**.

## Known Stubs

None. Every branch this plan added is reached by a test: the exemption, both sides of the retarget condition, both halves of the guarded region, the clamp at both ends of its range, and the fallback context.

## Threat Flags

None. No new endpoint, auth path, file access pattern or schema was introduced; no route was added or removed. The plan's register is honoured as written:

- **T-30-32-01** (poll vs `render_error`) — mitigated by the exemption; pinned by the browser class, which fails when it is reverted.
- **T-30-32-02** (`attempt` tampering) — mitigated by the in-handler clamp; pinned by `test_a_crafted_attempt_never_reaches_the_context_or_the_body`, which records what `_checks_context` was actually handed (`[cap, 0, cap]`) and asserts the crafted string never reaches a rendered `hx-get`.
- **T-30-32-03** (exception text on a LAN-visible page) — mitigated by the constants-only fallback; pinned by `test_no_part_of_the_exception_reaches_the_page`, with `test_the_failure_is_logged_with_its_traceback` proving nothing was merely swallowed.
- **T-30-32-04** (`HX-Target` spoofing) — accepted, argued above.
- **T-30-32-05** (poll against a dead origin) — accepted; stated in the code's decision record and in `docs/reference/web-api.md`.
- **T-30-32-SC** — no package installed; the vendored htmx bundle was read only, and its `integrity` pin in `base.html` is untouched.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- R3-CR-02 is closed with a measurement that fails under the mutation it is named for, which is the property the previous two rounds lacked.
- `CHECKS_POLL_TARGET_ID` is the single place any future polling element would be registered. The rule it establishes — one named exemption, pinned to the template's id by a test — is the one to follow if a second element is ever made to poll.
- Out of scope and still open: **R3-WR-04**, the poll's *window* (whether ten attempts at 2 s is long enough for the worst-case probe, and what the strip says when a chain runs out while a probe is demonstrably still running) belongs to plan 30-35, which owns the cap constants and `_checks_context`'s `gave_up` branch. This plan changed neither the cap's value nor the give-up line's wording.
- One standing dependency remains explicit and guarded: `base.html`'s `htmx-config` `[45]..` rule is what makes an error body swap at all, so it is now load-bearing *for this fix*. `test_the_htmx_config_meta_swaps_error_responses` still fails if it is flipped back to htmx's default.

## Self-Check: PASSED

All six files named above exist on disk, and all five task commits (`7ce9b0a`, `75b4c53`, `23816b5`, `0d06eed`, `65e0e44`) are present in this worktree's history, with this SUMMARY committed on top of them. `git status --short` is clean: no mutation edit survived, and `STATE.md` and `ROADMAP.md` were not touched — the orchestrator owns those.
