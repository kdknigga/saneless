---
phase: 30-appliance-layer
plan: 11
subsystem: ui
tags: [fastapi, jinja2, htmx, picocss, accessibility, aria-live, caching]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "30-06's shared check registry (CheckKey, CheckResult, run_checks) and its four Jinja filters"
  - phase: 30-appliance-layer
    provides: "30-07's CheckCache TTL cache and CheckRefresher lazy background thread"
  - phase: 30-appliance-layer
    provides: "30-09's filter registration in _build_templates() and the local_time filter"
provides:
  - "GET /api/checks -- the strip body, rendered from cache, never a probe"
  - "POST /api/checks/refresh -- the one handler allowed to probe, bypassing the TTL"
  - "partials/checks.html -- the swap target, the self-stopping cold-start poll and the Check again button"
  - "partials/terminal_reload.html -- the history loader plus a strip loader, factoring out four identical copies"
  - "#checks-card, the first article on the index page, with a persistent aria-live='polite' region"
  - "CheckRefresher.build_context() -- the public probe-context factory both probe paths share"
  - "the eleven .check-* CSS rules, every colour an alias over an existing token"
affects: [30-12, 30-13, browser-verification, doctor-cli-parity]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Self-stopping htmx poll: emit hx-trigger only while the poll is wanted"
    - "oob flag on a partial (the scan_button.html idiom) instead of an OOB wrapper div"
    - "Jinja macro for a shared row skeleton, so two loops share one markup body"

key-files:
  created:
    - src/saneless/web/templates/partials/checks.html
    - src/saneless/web/templates/partials/terminal_reload.html
    - tests/test_web_checks.py
  modified:
    - src/saneless/web/routes.py
    - src/saneless/web/refresher.py
    - src/saneless/web/app.py
    - src/saneless/checks.py
    - src/saneless/web/static/app.css
    - src/saneless/web/templates/index.html
    - src/saneless/web/templates/partials/status.html
    - src/saneless/web/templates/partials/status_response.html
    - tests/test_app_lifespan.py

key-decisions:
  - "Refresh probes directly in the handler rather than through the refresher's tick, because _tick returns early on a fresh cache and the button would be a no-op for the first 30 s after a page load"
  - "CheckRefresher.build_context() is public, so the button's probe and the thread's probe assemble the same dependencies instead of the route building a second context that could drift"
  - "The cold-start rows are a _CheckingRow dataclass, not a CheckResult: 'not looked yet' is not a CheckState and a fourth member would owe saneless doctor an exit-code rule for a state the CLI can never be in"
  - "checks.html branches once on 'checks is none' and shares a Jinja macro, rather than piping a non-existent checking state through check_state_class"
  - "The OOB strip uses the partial's own oob flag (the scan_button.html idiom); the plan's wrapper div would have produced two nested elements with id=checks-body"
  - "refresh_checks defaults False inside _status_context, so a route added later cannot acquire the behaviour by forgetting to say no"

patterns-established:
  - "Pattern C extended: the freshness line's four sentences are composed in Python, so the template writes no prose at all"
  - "A probe-count spy over saneless.web.routes.run_checks is how 'this render never probes' is proved, not inspection"
  - "The stylesheet's hex-literal list is pinned as an ordered list, so 'no new colour value' is a test, not a promise"

requirements-completed: [APPL-01, APPL-02, APPL-06, APPL-11]

# Metrics
duration: 62min
completed: 2026-09-16
---

# Phase 30 Plan 11: Status Strip Routes, Partial and Colours Summary

**The five-check status strip is on the index page, first and polite: `GET /` renders it from cache with zero probes, a cold cache polls itself full and then stops, and `Check again` is the only path in the app that may enter SANE from a request thread — and it refuses to while a scan holds the gate.**

## Performance

- **Duration:** ~62 min
- **Started:** 2026-09-16T00:00:00Z (worktree spawn)
- **Completed:** 2026-09-16
- **Tasks:** 3 (6 commits, RED → GREEN per task)
- **Files modified:** 12 (3 created)

## Accomplishments

- **Zero probes in a request handler (D-04).** `GET /` and `GET /api/checks` read `CheckCache.current()` and nothing else. A spy on `saneless.web.routes.run_checks` asserts the count is zero for both, which is the whole of success criterion 2 on the web side: an unplugged sane-net host costs a page render nothing because no page render touches it.
- **A poll that ends itself (D-06).** The cold body carries `hx-get`/`hx-trigger="load, every 2s"`/`aria-busy`; the body that replaces it carries none of them. This is `status.html`'s in-tree idiom, deliberately not HTTP 286, so the tuned `htmx-config` meta in `base.html` is untouched. A test pins that a result-bearing body has no `hx-trigger`, which is what keeps D-05's lazy refresher from being held awake by abandoned tabs (T-30-48).
- **A Refresh button that genuinely re-probes (D-09).** `POST /api/checks/refresh` calls `run_checks` itself, so it bypasses the TTL; `test_refresh_bypasses_a_fresh_cache` warms the cache, asserts `is_fresh()`, then asserts the click still probes exactly once. During a scan the gate is tried non-blocking and `skip_scanner` is set, so the click cannot become a second caller into SANE.
- **`note_watcher()` now has callers.** Three of them — `index`, `GET /api/checks` and `POST /api/checks/refresh` — plus a wiring test that drives the *real* `CheckRefresher` and asserts its watch stamp moves. Before this plan the method was dead and the refresher's first guard returned for ever.
- **Four duplicated hidden loaders became one partial that does more.** `partials/terminal_reload.html` holds the history loader and a new strip loader, included by all four terminal branches, so the strip un-pauses the moment a scan ends instead of waiting out the 30 s TTL.
- **No new colour value, proved.** The stylesheet's three hex literals are pinned as an ordered list and the `--saneless-status-fallback` declarations byte for byte; `.check-warn` reads that same property. The 68-test browser suite, which measures computed contrast in both schemes, passes unchanged.

## Task Commits

1. **Task 1: the two check routes and the strip context** — `46edeec` (test), `dfaf043` (feat)
2. **Task 2: partials/checks.html, the card, and the four state colours** — `7ccf990` (test), `6696b39` (feat)
3. **Task 3: terminal_reload.html and the out-of-band strip refresh** — `b478486` (test), `a730264` (feat)

## Files Created/Modified

- `src/saneless/web/templates/partials/checks.html` — the swap target: the cold-start poll, the row loop through a shared macro, the freshness line and the `Check again` button.
- `src/saneless/web/templates/partials/terminal_reload.html` — the history loader and the strip loader, the four `status.html` copies factored into one.
- `tests/test_web_checks.py` — 73 tests: zero-probe, watcher stamping, cold start, the Refresh bypass, the four freshness sentences, placement, accessibility, vocabulary, the stylesheet pins and the out-of-band table.
- `src/saneless/web/routes.py` — `_CheckingRow`, `_CHECKING_ROWS`, `_freshness_line`, `_checks_context`, `get_checks`, `refresh_checks`; `index` stamps the watcher; `start_scan` carries the strip out-of-band.
- `src/saneless/web/refresher.py` — `build_context()` made public, and `_tick` now calls it.
- `src/saneless/web/app.py` — the `_build_check_machinery` docstring records how the refresh route reaches the context factory.
- `src/saneless/checks.py` — `CHECKING_STATE_LABEL`, the spoken word a cold row needs.
- `src/saneless/web/static/app.css` — eleven `.check-*` rules; four colour aliases, seven layout rules, no new literal.
- `src/saneless/web/templates/index.html` — `#checks-card` as the first article, wrapping `#checks-strip`.
- `src/saneless/web/templates/partials/status.html` — four literal loaders replaced by four includes.
- `src/saneless/web/templates/partials/status_response.html` — the `refresh_checks` out-of-band branch.
- `tests/test_app_lifespan.py` — the two new routes added to the SANE-lifecycle route table.

## Decisions Made

- **The Refresh button probes in the handler, not through the refresher.** This is the `<mandatory_additions>` item 2 that 30-07 flagged. `_tick`'s second guard is `if self._cache.is_fresh(): return`, so routing a click through the refresher's policy would make the button do nothing for the first thirty seconds after any page load — exactly the window in which somebody who has just plugged the scanner back in presses it. The bypass is an explicit `run_checks` call in the handler followed by `cache.store(...)`. `test_refresh_bypasses_a_fresh_cache` is the test that this is not a no-op.
- **`CheckRefresher.build_context()` is public rather than `app.state.check_context`.** Both were written; the second was reverted (see deviation 1). The method keeps "how a probe's dependencies are assembled" in the one class that owns it, and means the route cannot build a second, drifting context.
- **The cold-start rows are not `CheckResult`s.** `check_state_class` / `check_state_glyph` / `check_state_label` are exhaustive `match`es over three `CheckState` members, and "we have not looked yet" is not one of them. A fourth member would owe `saneless doctor` an exit-code rule for a state the CLI can never reach, because it probes synchronously and always has an answer. So `_CheckingRow` carries `state_class`, `glyph` and `state_label` directly — every one of them a constant imported from `saneless.checks`, so the template still authors nothing.
- **The template branches once, on `checks is none`, and shares a Jinja macro.** UI-SPEC's snippet pipes `c.state` through the three filters for every row, which cannot work for a row that has no state. The branch is on presence, never on a `CheckState` value, so the "templates own no vocabulary" rule holds; a source test asserts `checks.html` contains no `== '` or `== "`.
- **The out-of-band strip uses the partial's own `oob` flag.** `partials/scan_button.html` already does this. The plan's literal `<div id="checks-body" hx-swap-oob="true">{% include %}</div>` would have nested one `#checks-body` inside another — a duplicate id, and an htmx swap that leaves the `hx-swap-oob` attribute in the DOM.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `PLR0915` refused the extra `create_app` statement**
- **Found during:** Task 1 GREEN.
- **Issue:** The first implementation put the probe-context factory on `app.state.check_context`, which is one more statement in `create_app` — already at exactly 50, ruff's `PLR0915` ceiling. CLAUDE.md forbids `# noqa`.
- **Fix:** Reverted the `app.py` plumbing and made `CheckRefresher.build_context()` public instead, so the route reaches the factory through `state.refresher` and `create_app` gains no statement. `_tick` now calls the same method, so the two probe paths are provably the same code. This is also the better design: the refresher owns the factory already.
- **Files modified:** `src/saneless/web/app.py`, `src/saneless/web/refresher.py`, `src/saneless/web/routes.py`
- **Verification:** `uv run ruff check .` clean; `test_refresh_during_a_scan_skips_the_scanner` and `test_refresh_when_idle_does_not_skip_the_scanner` pin the context the route builds.
- **Committed in:** `dfaf043`

**2. [Rule 3 - Blocking] `tests/test_app_lifespan.py` rejects unregistered routes**
- **Found during:** Task 1 GREEN, full-suite run.
- **Issue:** `test_sane_lifecycle_across_startup_every_route_and_shutdown` asserts every route is either driven or skipped with a stated reason. The two new routes were neither, so the test failed with `routes ['/api/checks', '/api/checks/refresh'] are neither driven nor skipped`.
- **Fix:** Added both to `_ROUTE_CALLS` with a comment stating why the refresh route in particular belongs in a SANE-lifecycle proof: it runs the scanner check through the same backend handle the worker uses and must not leave a call outstanding at `sane_exit()`.
- **Files modified:** `tests/test_app_lifespan.py`
- **Verification:** `uv run pytest tests/test_app_lifespan.py -q` — 19 passed.
- **Committed in:** `dfaf043`

**3. [Rule 2 - Missing Critical] `CHECKING_STATE_LABEL` added to `saneless.checks`**
- **Found during:** Task 1 GREEN.
- **Issue:** 30-06 shipped `CHECKING_GLYPH`, `CHECKING_STATE_CLASS` and `CHECKING_MESSAGE` but no screen-reader word. The glyph is `aria-hidden`, so without a word a cold row would be the only row on the page a listener hears with no state marker at all — a WCAG 1.4.1 regression in exactly the state the page spends its first seconds in.
- **Fix:** Added `CHECKING_STATE_LABEL: Final = "Checking"` beside the other three, in `__all__`, with a comment giving the same reasoning `check_state_label` gives for saying "Failed" rather than "FAIL". This follows 30-06's own stated rationale for putting the other three there.
- **Files modified:** `src/saneless/checks.py`
- **Verification:** `test_a_cold_row_is_spoken_too` asserts every cold row carries it; `tests/test_checks.py` unchanged and green.
- **Committed in:** `dfaf043`

**4. [Rule 1 - Bug] The plan's out-of-band wrapper would have duplicated an id**
- **Found during:** Task 3 GREEN.
- **Issue:** The plan specifies `{% if refresh_checks %}<div id="checks-body" hx-swap-oob="true">{% include "partials/checks.html" %}</div>{% endif %}`. The partial's root element *is* `<div id="checks-body">`, so this renders one inside the other: two elements with the same id, and an `outerHTML` OOB swap that installs a wrapper still carrying `hx-swap-oob` into the live DOM.
- **Fix:** Gave `checks.html` an `oob` flag exactly as `partials/scan_button.html` has one, and wrote the branch as `{% if refresh_checks %}{% with oob = true %}{% include "partials/checks.html" %}{% endwith %}{% endif %}`. One element, one id, the in-tree idiom.
- **Files modified:** `src/saneless/web/templates/partials/checks.html`, `src/saneless/web/templates/partials/status_response.html`
- **Verification:** `test_the_page_has_one_body_and_one_strip` asserts `id="checks-body"` appears exactly once in the rendered page; `test_a_scan_submit_carries_the_strip_out_of_band` asserts the OOB attribute is on the one element.
- **Committed in:** `a730264`

### Departures from the plan's literal text (not bugs)

- **The strip partial and `#checks-card` landed in Task 1's GREEN, not Task 2's.** Task 1's own behaviour list asserts rendered markup (five rows, `hx-trigger`, the freshness paragraph), which cannot be produced without the template and the card. Task 2 therefore delivered the stylesheet and the markup *tests*. Commit order is still `test(30-11)` before `feat(30-11)` for every task.
- **`refresh_checks` is a context-dict key, not a keyword argument.** The acceptance criterion greps for `refresh_checks=True`; `TemplateResponse` takes its context as a mapping, so the real form is `"refresh_checks": True`. The test pins the intent instead: exactly one `True` and exactly one `False` in `routes.py`.
- **The history loader lives in two template files, not one.** The plan's RED text says "exactly one template file"; its own acceptance criterion says the grep should return 2 and names `partials/error.html` as the second. The test asserts the honest version — `["error.html", "terminal_reload.html"]` — and separately asserts `status.html` holds none.
- **`grep -c 'hx-disinherit'` on `index.html` returns 3, not 1.** Two are the pre-existing C-10 comment and the attribute; the third is a word in the new placement comment. The attribute itself is asserted at exactly one occurrence by `test_the_form_gained_no_attribute`, and no attribute was added to the `<form>`.

---

**Total deviations:** 4 auto-fixed (2 × Rule 3 blocking, 1 × Rule 2 missing critical, 1 × Rule 1 bug), plus 4 documented departures from literal plan text.
**Impact on plan:** No scope creep. Deviations 1 and 4 both replace plan-specified constructs with the existing in-tree idiom for the same job; deviation 4 in particular fixes a duplicate-id defect that was in the plan as written.

## Known Stubs

None. Every row the strip renders comes from `run_checks` or from the cold-start constants, and the cache is filled by the refresher this plan finally gave a watcher to.

## Threat Flags

None. The plan's register (T-30-46 … T-30-51) covers the whole surface this plan adds, and each disposition is met:

| Threat | How it is met here |
|---|---|
| T-30-46 CSRF on the new POST | `CrossOriginGuard` is app-wide middleware; no per-route dependency was added (`grep -c Depends routes.py` is 0). `test_refresh_is_refused_cross_site` and the pre-existing `test_every_unsafe_route_rejects_a_cross_site_request` both cover it. |
| T-30-47 Refresh hammered from the LAN | The handler is a plain `def` on the threadpool; each press runs the bounded registry once and skips the scanner while the gate is held. |
| T-30-48 a steady-state poll | The trigger is emitted only on a cold cache; `test_a_body_with_results_does_not_poll` and `test_the_index_never_polls_once_results_exist` pin it. |
| T-30-49 a path, URL or token in a row | Every string comes from `checks.py`; the freshness line is composed in Python from four developer constants. |
| T-30-50 XSS in a check message | Jinja autoescape is on and no `\|safe` appears anywhere in the new templates; the glyphs are code points emitted by filters, not raw markup. |
| T-30-51 a health list interrupting a reader | `aria-live="polite"` on a persistent container, no `role="alert"` in the partial, and `test_the_page_keeps_exactly_one_assertive_region` pins the page at one. |

## Issues Encountered

- **The worktree spawned on the wrong base again — fifth wave running.** `git merge-base HEAD d692ab0` returned `a87b3dd`, so the startup guard's `git reset --hard` fired for real. Before the reset there was no `.planning/`, no `CLAUDE.md` and none of waves 1–5. This guard must not be dropped.
- **`tests/test_paperless.py::...[read-timeout-empty]` failed once under load, then passed alone and in two subsequent full runs.** It is a 0.05 s deadline test and this was a parallel-wave timing flake, unrelated to anything here. Not fixed (out of scope per the scope boundary); noted for whoever sees it again.
- **`ruff format` reflowed the test file twice** — expected, not a problem, but worth running `ruff format` before `ruff check` on a new file rather than after.
- **Bash refuses heredocs inside the worktree** ("too complex to verify it stays inside the worktree"), so the test file was appended to with the Edit tool rather than `cat >>`. The known-issues list is accurate on this.

## Verification

Every gate the plan names, run from the worktree:

- `uv run pytest tests/test_web_checks.py -q` — **73 passed** (plan asks for ≥ 12 in Task 1 and ≥ 9 new markup tests in Task 2; delivered 26 and 32 respectively, plus 15 for Task 3)
- `uv run pytest tests/test_web_state_rendering.py tests/test_web.py tests/test_web_errors.py tests/test_cross_origin.py -q` — green, with **no edits to `test_web_state_rendering.py` in Task 3's diff**, which is the proof the loader factoring is behaviour-preserving
- `uv run pytest -m "not browser and not sane_hardware" -q` — **2566 passed, 74 deselected**
- `uv run pytest -m browser -q` — **68 passed**, including the computed-contrast measurements over `app.css` in both schemes
- `uv run ruff check .` / `uv run ruff format --check .` / `uv run ty check` / `uv run pyrefly check src tests` — all clean, **zero suppressions added**

Browser validation: the Playwright-driven `-m browser` suite exercises the real rendered page in Chromium and passes against the new markup and stylesheet. It does not yet contain strip-specific assertions — the UI-SPEC § Verification Contract assigns those, and they belong with whichever plan owns the browser-test budget for this phase.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- The strip is live, first on the page, and reads from the cache the refresher fills. `note_watcher()` now has callers, so the refresher does work for the first time since 30-07 built it.
- `partials/checks.html` and its `oob` flag are the extension points for any later plan that needs to re-render the strip from another response.
- **Open for a later plan:** browser tests asserting the strip's own layout, the cold-start poll stopping in a real browser, and the `Check again` round trip. Nothing blocks them; they simply are not in this plan's budget.
- **Open for a later plan:** `saneless doctor`'s side of D-02 parity (30-08). This plan pins the web half's sentences against the registry, so the CLI plan has a fixed target.

## Self-Check: PASSED

All four claimed new files exist on disk; all six claimed commits exist in `git log`.

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-16*
