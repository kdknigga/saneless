---
phase: 30-appliance-layer
plan: 27
subsystem: web
tags: [denial-of-service, htmx, cold-start, polling, browser-measured, tdd, in-07]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the Check again button as the way forward out of the give-up state, rate-limited at one probe per 2 s (30-26); CheckRefresher.probe_now as the one probe path the poll waits on (30-25); CheckCache's cold-cache `results is None` as the poll's primary terminating condition (30-07)"
provides:
  - "POLL_ATTEMPT_CAP = 10: the cold-start poll's second ending, counted in attempts the browser carries in the URL"
  - "POLL_GAVE_UP_LINE: the server-authored sentence that replaces the freshness line once the strip has stopped asking"
  - "_checks_context(state, *, attempt=0) returning poll_attempt: the next attempt number, or None when there is to be no next request"
  - "GET /api/checks?attempt=N, bounded at the route by Query(ge=0, le=POLL_ATTEMPT_CAP)"
  - "A polling body that no longer carries `load`, so the interval in the markup is the interval the browser uses"
affects: [appliance-layer, status-strip, cold-start-latency]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Count in the URL, not on the server: the strip is one shared cache read with nothing per-tab to keep, so the attempt rides in the query string and a browser that goes away takes its count with it"
    - "Measure the loop before capping it: a cap counted in attempts is only a cap in time if the interval is honest, and the markup's interval was off by 85x"
    - "A give-up state keeps its way forward: the rows, the button and a sentence naming the button all survive the poll ending"
    - "Rendered-HTML assertions for absence, always paired with the same reading finding it when present"

key-files:
  created: []
  modified:
    - src/saneless/checks.py
    - src/saneless/web/routes.py
    - src/saneless/web/templates/partials/checks.html
    - tests/test_web_checks.py
    - tests/test_browser.py
    - docs/reference/web-api.md
    - pyproject.toml

key-decisions:
  - "The cap is 10 attempts, chosen from the measured rate and not from the markup: at the honest 2 s interval that is ~18-20 s of asking, about 20 refresher ticks and ~2.5x the worst cold-start probe budget (PROBE_CONNECT_SECONDS + PROBE_READ_SECONDS)"
  - "`load` was dropped from every polled body, keeping it only where the strip is first parsed (poll_attempt == 1). Without this the cap would have been spent in a quarter of a second and the legitimate cold start cut off before the refresher's first tick"
  - "The attempt lives in the URL rather than in server state: per-tab counters on a shared cache read would be state with no owner and no expiry, and the browser already carries exactly one number"
  - "At the cap the freshness line is replaced, not augmented: there is no freshness to report because nothing has ever been checked"
  - "The placeholder rows stay at the cap. They still read `Checking…` beside a line saying the checks have not run yet, which is the honest pair: the rows describe what is known about each check (nothing), and the line describes why"
  - "junit_family = legacy in pyproject: two tests report measurements through record_property, which raises a PytestWarning under the default xunit2 and would become a collection error under filterwarnings = [\"error\"]"

patterns-established:
  - "Before-and-after request counts from a real browser, recorded as test properties rather than printed, so the number that justified a constant is readable from a --junitxml run and not only from a commit message"

requirements-completed: [APPL-02]

# Metrics
duration: 22min
completed: 2026-09-16
---

# Phase 30 Plan 27: An End to the Cold-Start Poll Summary

**A tab left open on an appliance whose refresher has died now issues exactly ten requests and
goes quiet, instead of the 254-in-six-seconds it really issued before -- and it goes quiet
without going blank: the five rows, the Check again button and a server-authored line saying
the checks have not run yet are all still there, verified in Chromium against a swap target
that carries exactly one attribute, `id=checks-body`.**

## Performance

- **Duration:** ~22 min
- **Started:** 2026-09-16T23:31Z (local 23:31 CDT)
- **Completed:** 2026-09-16T23:53Z
- **Tasks:** 2 (both TDD, 3 commits: one measurement-RED, one assertion-RED, one GREEN)
- **Files modified:** 7 (+609 / −22)

## The measurement, which is the point of this plan

Task 1 existed because IN-07's proposed fix is a cap, and a cap chosen against the interval in
the markup would be wrong by whatever factor the markup was wrong by. It was wrong by 85.

| What | Before | After |
|------|--------|-------|
| Abandoned cold strip, requests in a 6 s window | **254** | stops after **10**, then 0 for the rest of the window |
| Effective rate | **42.3 requests/second** | 0.5 requests/second (the advertised `every 2s`) |
| Server-side poll chain, followed link by link | **60 links and still going** (the loop's own limit) | **11 links** (the bare route plus attempts 1…10) |
| Healthy cold start, requests to reach results | **39** | **2** |

All four measured, not inferred: the browser figures come from Playwright request interception
over a real uvicorn server (`record_property`, readable from `--junitxml`), the chain figures
from following each response's own `hx-get` through a `TestClient`.

**Why 42/second and not 0.5.** htmx re-fires the `load` trigger for content it has just swapped
in. The strip's body carried `hx-trigger="load, every 2s"` and swapped in a copy of *itself*, so
every response immediately caused the next request. The `every 2s` in the markup was decoration;
the real interval was one round trip on loopback. This is the ambiguity the review left open
("twice a second") and the measurement settled it in the worse direction.

**Why that made the cap insufficient on its own.** Ten attempts at 42/second is a quarter of a
second. The legitimate cold start -- which takes about one `TICK_SECONDS` for the refresher to
store -- would have been cut off before it ever had a chance. So the fix is two things, and the
second one is what makes the first one mean anything: `load` now rides only on the body a page
render or an out-of-band swap first parses (`poll_attempt == 1`), never on a polled one.

**Why the cap is 10.** At the honest 2 s interval, ten attempts is ~18–20 s of asking. That is
about twenty refresher ticks, and about two and a half times the worst probe budget a cold start
can cost (`PROBE_CONNECT_SECONDS` 2 s for saned plus `PROBE_READ_SECONDS` 5 s for Paperless). The
healthy cold start now settles on its **second** request, so it is given eight it will never
need. The number and its reasoning are both in the constant's comment in `checks.py`.

## Accomplishments

- **IN-07 closed.** `POLL_ATTEMPT_CAP` gives the poll a second ending that does not depend on the
  refresher thread being alive. `_checks_context(state, *, attempt=0)` returns `poll_attempt` --
  the number the next request should carry, or `None` — and the template emits its request
  attributes only when that is set. "Should the browser ask again" is decided in Python, never in
  the markup.
- **The markup is honest again.** The polled body carries `hx-trigger="every 2s"` and nothing
  else, so the interval a reader sees is the interval the browser uses. Pinned by a rendered-HTML
  test: `attempt=0` carries `load,`, `attempt=1` does not.
- **The capped swap target is inert.** Asserted against the attributes the server really rendered
  (`hx-` absent entirely, `aria-busy` absent), paired in the same test with the attempt-0 body
  where the same reading finds them — an absence assertion is worth nothing without its pair.
  Confirmed independently in a real browser: `['id=checks-body']` is the entire attribute list
  at give-up (T-30-27-04).
- **Giving up keeps the way forward.** Five placeholder rows, the Check again button, and
  `POLL_GAVE_UP_LINE` in place of the freshness line. Driven in a real browser from the given-up
  state: the click probed, real results landed (`Last checked 2026-09-16 23:52 CDT.`), and **zero**
  further poll requests followed.
- **`attempt` is bounded at the route.** `Annotated[int, Query(ge=0, le=POLL_ATTEMPT_CAP)]`, the
  same idiom the tag filter's `max_length` uses. `cap + 1`, `-1`, `"nine"` and `"1e9"` are all 422
  before anything reaches the context (T-30-27-02). The value never appears in the visible body —
  only in the next request's URL (T-30-27-03).
- **D-06 is still primary.** A warm cache carries no trigger at attempt 0, 3 or the cap, and the
  chain test pins that results end it at the very first link. The cap is a second ending, not a
  replacement.
- **26 new tests**, suite at **3025 passing** (baseline 3011), no test removed, `tests/`
  `time.sleep` count unchanged at **17**, no new route so `_ROUTE_CALLS` / `_ROUTE_SKIPS` and
  `tests/test_app_lifespan.py` are untouched.

## Task Commits

1. **Task 1: measure what the cold-start poll actually does, in a real browser** — `f46ec98` (test, measurement-RED)
2. **Task 2: the server decides when to stop asking** — `a41171a` (test, RED), `1995423` (fix, GREEN)

No REFACTOR commit was needed: the GREEN commit landed clean under ruff, ruff-format, ty and
pyrefly.

## TDD Gate Compliance

Both tasks ran RED before implementation, with a `test(...)` commit preceding the `fix(...)` one.

- **Task 1 RED** is a measurement by the plan's own design, and it is the one place in this phase
  where that was the right shape: the cap's value is an *input* from the browser, not a
  prediction, so the test asserts only what was already true (`observed > 1`, the poll still
  running, the storing case reaching results) and records the numbers. Both cases passed at
  Task 1, as intended.
- **Task 2 RED:** 12 failed, and they are the twelve that carry the plan's claim — ten new
  assertions in `TestBoundedPoll`, the inverted chain test, and the one existing assertion whose
  spelling the change makes wrong (`hx-get="/api/checks"` becomes
  `hx-get="/api/checks?attempt=1"`). Ten failed on `AttributeError: module 'saneless.checks' has
  no attribute 'POLL_ATTEMPT_CAP'` and two on the rendered markup. Nothing passed unexpectedly.
  The constants are reached through `checks_module.` — the module-attribute idiom this file
  already uses for `routes_module`, `refresher_module` and `app_module` — precisely so RED is
  twelve informative failures rather than one collection error that says nothing.

## Verification

| Gate | Result |
|------|--------|
| `uv run pytest -q` | 3025 passed (baseline 3011) |
| `uv run pytest tests/test_web_checks.py -q` | 99 passed (89 after Task 1; none removed) |
| `uv run pytest tests/test_web_checks.py -q -k "Poll or poll"` | 17 selected, all passed (≥6 required) |
| `uv run pytest tests/test_browser.py -q -k ColdStartPollIsBounded` | 2 selected, both passed |
| `uv run pytest tests/test_app_lifespan.py -q` | 24 passed, file untouched (`git diff --stat` empty) |
| `uv run ruff check .` | clean |
| `uv run ruff format --check .` | clean |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --stage pre-push --all-files` | all hooks passed (ty full, pyrefly full, ruff, ruff-format) |
| `grep -c "POLL_ATTEMPT_CAP" src/saneless/checks.py` | 2 (≥2 required) |
| `grep -c "POLL_ATTEMPT_CAP" src/saneless/web/routes.py` | 5 (≥1 required) |
| `grep -c "POLL_GAVE_UP_LINE" src/saneless/checks.py` | 2 |
| `ls src/saneless/web/static/` | `app.css`, `vendor/` — the only `.js` is the vendored `htmx-2.0.8.min.js` |
| `tests/` `time.sleep` count over source files | 17 (unchanged) |
| Commit deletions (`git diff --diff-filter=D HEAD~3 HEAD`) | none |

## Browser validation

Playwright MCP tools were not exposed to this executor, so Playwright was driven directly — both
inside the suite (`tests/test_browser.py`, session Chromium, private uvicorn servers behind the
module's no-egress gate) and through a standalone script against a real loopback server. Per
CLAUDE.md, nothing was deferred to a human.

1. **A cold start's first real results land on their own.** `storing_strip_server` (the new
   fixture: refresher alive, so the cache is cold on arrival and warm a tick later) reaches its
   results state with no interaction — five real rows, no `Checking…`, no click. 2 requests.
2. **A tab whose cache never fills stops.** `cold_strip_server` (refresher stopped) issues exactly
   `POLL_ATTEMPT_CAP` requests, the last one `?attempt=10`, and then **nothing** for a further six
   seconds — three of the advertised intervals, so a poll that had merely paused would have been
   caught.
3. **A given-up tab still offers a way forward.** Five `.check-row` elements, one `.check-refresh`
   button, and `.check-meta` reading exactly `POLL_GAVE_UP_LINE`. Screenshotted light and dark:
   neutral `·` glyphs, no red, nothing that reads as an error.
4. **The swap target carries no unconditional htmx request attribute.** Read out of the live DOM
   at give-up: `['id=checks-body']`, the complete attribute list. No `hx-*`, no `aria-busy`, so
   there is no self-re-arming `outerHTML` loop.
5. **Check again from the given-up state works.** Clicked in the browser: the meta line became
   `Last checked 2026-09-16 23:52 CDT.`, five real rows rendered, and zero further poll requests
   followed (results end the poll, as they always did).

## Deviations from Plan

### Auto-fixed issues

**1. [Rule 3 — Blocking] `record_property` is unusable under this project's pytest settings**

- **Found during:** Task 1
- **Issue:** The plan requires the measured request count to be "captured, not asserted against a
  guess" and carried in the test output. `print` is unavailable (ruff `T20` is selected and not
  ignored for `tests/`), and `record_property` raises `PytestWarning: record_property is
  incompatible with junit_family 'xunit2'` under the default family — which
  `filterwarnings = ["error"]` turns into a **collection error**, so a `--junitxml` run would fail
  on exactly the tests whose output it was asked to capture.
- **Fix:** `junit_family = "legacy"` in `[tool.pytest.ini_options]`, with a comment naming the two
  tests and the interaction. Nothing in CI passes `--junitxml` today, so this changes no existing
  behaviour; it removes a landmine the new tests would otherwise have planted.
- **Files modified:** `pyproject.toml`
- **Commit:** `f46ec98`

### Choices taken inside the plan's latitude

- **The give-up line is delivered as `freshness_line`, not as a second context key.** The plan
  says the context gains "one new key" (`poll_attempt`) and that the give-up line goes "in place
  of the freshness line at the cap" — so the meta slot keeps one source and the template keeps one
  `{{ freshness_line }}`. A second key would have put a conditional in the markup for a value the
  route already knows.
- **`{%- if poll_attempt %}` rather than `is not none`.** `poll_attempt` is `attempt + 1`, so it
  is never `0`; truthiness and `is not none` are the same test here and the shorter one reads
  better beside the existing `{% if oob %}`.

## Issues Encountered

**One acceptance criterion is literally unsatisfiable and was read for its intent.** The plan's
Task 2 criterion says a test "asserts the returned HTML contains no `hx-` attribute". The returned
HTML always contains `hx-post="/api/checks/refresh"` — that is the Check again button, which
another of the same plan's must_haves requires to survive the cap. Taken literally the two
requirements contradict. The register entry for T-30-27-04 says what was meant: "the capped
**body** carries no `hx-` attribute at all", i.e. the swap target's own attribute list. That is
what `test_the_capped_body_carries_no_request_attribute_and_the_first_one_does` asserts, against
`_body_attrs(...)` of the rendered response, and it is independently confirmed against the live
DOM (`['id=checks-body']`). Intent satisfied; the literal wording is not, and cannot be.

**The plan expected the healthy cold start's request count to be "unchanged from Task 1's second
measurement". It changed, from 39 to 2, and that is the fix working.** The plan was written before
the `load` re-fire was measured, so it assumed the cold start already ran at 2 s intervals. It did
not — those 39 requests were 39 round trips in under a second. The property the plan actually
cares about ("the first real results still land in the strip on their own, measured in a real
browser") holds exactly, and now costs 2 requests instead of 39. Recorded because the raw number
moving is otherwise easy to misread as a regression.

**`grep -rc "time\.sleep" tests/` still reports 19, not 17.** Unchanged from plan 30-26's finding:
`-r` descends into `tests/__pycache__/` and a stale `.pyc` contains the byte sequence twice. Over
the ten source files the count is **17**, verified with `git grep -c "time\.sleep" -- 'tests/*.py'`,
and neither file this plan touched contains a sleep. Both browser waits use Playwright's own
`page.wait_for_timeout` and `expect(...)` auto-waiting.

## Residual, recorded deliberately

- **The cap is per chain, not per tab or per client.** Anything that legitimately re-renders the
  strip — a page load, the terminal-state reload, a scan submit's out-of-band swap, a Check again
  click — starts a fresh chain at attempt 0. That is intended: each of those is a real event
  saying somebody is here, and the thing being bounded is an *abandoned* tab, which by definition
  produces none of them. A page reloaded every twenty seconds by a human would poll continuously,
  and so it should.
- **The rows still read `Checking…` at the cap.** The plan requires the placeholder rows to stay,
  and they carry the only thing that is true of each check: nothing is known. The line beneath
  them is what says the checks have not run and will not until somebody asks. A separate
  give-up glyph or row message would be new vocabulary for a state that already has a sentence.
- **`D-05`'s lazy refresher is now woken ten times rather than indefinitely by an abandoned tab.**
  Each `GET /api/checks` still stamps `note_watcher()`, so a poll that gave up also stops
  extending the watch window — which is a small second benefit, not a designed one, and is why the
  give-up window (~20 s) sits comfortably inside `WATCH_WINDOW_SECONDS` (90 s) rather than
  interacting with it.

## Known Stubs

None. No placeholder value, empty-collection default or "coming soon" string was introduced. The
five `Checking…` rows are the pre-existing D-06 cold-start vocabulary, not a stub.

## Threat Flags

None. No route was added — `GET /api/checks` gained an optional, bounded query parameter, which is
why `tests/test_app_lifespan.py` needed no edit. No package was installed, no client script file
was introduced, and `src/saneless/web/static/` still holds exactly one `.js`, the vendored
`htmx-2.0.8.min.js`.

### Threat register outcomes

| Threat ID | Disposition | Outcome |
|-----------|-------------|---------|
| T-30-27-01 | mitigate | **Closed.** The abandoned-tab request volume went from unbounded (measured at 42/s) to exactly 10 and then silence. Each request was always a cache read and never a probe, so what closed is request volume, as scoped. |
| T-30-27-02 | mitigate | **Closed.** `Query(ge=0, le=POLL_ATTEMPT_CAP)` at the route; `cap + 1`, `-1`, `"nine"` and `"1e9"` are 422 before the context is built. |
| T-30-27-03 | mitigate | **Closed.** `POLL_GAVE_UP_LINE` is a developer constant in `checks.py` beside `CHECKING_MESSAGE`, carrying no path, URL, host or exception text, and `attempt` reaches only the next request's URL, never the visible body. |
| T-30-27-04 | mitigate | **Closed.** The htmx attributes stay inside a conditional the server supplies; the capped body's rendered attribute list is empty, confirmed both in a rendered-HTML test and against the live DOM. |
| T-30-27-SC | n/a | Unchanged. No package installed, `pyproject.toml` touched only for a pytest ini option, htmx still the vendored 2.0.8, no `app.js` reintroduced. |

## Self-Check

- `src/saneless/checks.py` — FOUND
- `src/saneless/web/routes.py` — FOUND
- `src/saneless/web/templates/partials/checks.html` — FOUND
- `tests/test_web_checks.py` — FOUND
- `tests/test_browser.py` — FOUND
- `docs/reference/web-api.md` — FOUND
- `pyproject.toml` — FOUND
- `.planning/phases/30-appliance-layer/30-27-SUMMARY.md` — FOUND
- Commits `f46ec98`, `a41171a`, `1995423` — all FOUND in `git log`

## Self-Check: PASSED
