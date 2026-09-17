---
phase: 30-appliance-layer
plan: 23
subsystem: api
tags: [fastapi, htmx, jinja2, httpx, paperless-ngx, asvs, pytest]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "WebConfig.show_tags / show_correspondent (D-28), the D-29 profile-default fallback in start_scan, _tag_list_context and the index metadata fetches"
provides:
  - "A profile default that applies only when its control was absent from the form, never when the user cleared it (WR-06)"
  - "Flag-gated metadata fetches: the simple form costs zero paperless-ngx round trips per page load (IN-01)"
  - "paperless_test logs type(exc).__name__, plus an ast-parsed source guard over every logger call in web/routes.py (IN-02)"
affects: [web-routes, appliance-config, security-logging]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Form-shape config as the fact a submit cannot carry: gate a server-side fallback on the key that decided whether the control was rendered, never on the submitted value"
    - "Guard the shared context builder rather than each call site, returning the normal path's key set emptied so a ** spread stays complete"
    - "ast-parsed source guards for logging hygiene, so a multi-line call cannot slip past a line-based grep"

key-files:
  created: []
  modified:
    - src/saneless/web/routes.py
    - tests/test_web.py

key-decisions:
  - "The profile-default gate reads state.settings.web.show_tags / show_correspondent and nothing the request carries, so a crafted submit cannot choose whether the defaults apply (T-30-23-02)"
  - "Inside each gate the assignment is unconditional: with the control off the submit's value for that field is meaningless, so there is nothing to preserve"
  - "The show_tags fetch guard lives in _tag_list_context, not in index, so it covers all three call sites (index, /api/tags, /api/cache/invalidate?resource=tags)"
  - "The guarded tag context returns the normal path's five keys emptied rather than a shorter dict, because index spreads it with ** into a page that has nothing to do with tags"
  - "The correspondents context key is kept and set to [] rather than omitted: the template reaches for it inside its own {% if %}, and an absent key is a different bug from an empty one"
  - "The IN-02 source guard is parsed with ast rather than grepped, so a logger call wrapped over several lines is caught too"

patterns-established:
  - "Config-key gating for form-shape fallbacks: hiding a control changes what is asked, and the config key is the only record of which page was served"
  - "Counting mock transport over a real PaperlessClient with a cold MetadataCache as the way to assert a route's outbound request budget"

requirements-completed: [APPL-04, APPL-10]

# Metrics
duration: 17min
completed: 2026-09-17
---

# Phase 30 Plan 23: Form-Shape Defaults and Metadata Budget Summary

**Profile defaults now fill in only for controls the form omits, the simple form costs zero paperless-ngx round trips per page load, and `web/routes.py` has no log line left that renders an exception object.**

## Performance

- **Duration:** 17 min
- **Started:** 2026-09-17T03:21:12Z
- **Completed:** 2026-09-17T03:38:25Z
- **Tasks:** 3 (all TDD, 6 commits)
- **Files modified:** 2

## Accomplishments

- **WR-06 closed.** A household member who unticks every tag box gets a job with no tags again. The fallback is gated on `[web] show_tags` / `show_correspondent` — the only fact that distinguishes "the control was never rendered" from "the control was rendered and cleared", because both reach `start_scan` as an empty list / `None`.
- **IN-01 closed.** `_tag_list_context` returns early before its fetch when `show_tags` is off, and `index` fetches correspondents only when `show_correspondent` is on. With both off a cold-cache page load issues no paperless-ngx request at all — measured, not asserted by inspection.
- **IN-02 closed.** `paperless_test` logs `type(exc).__name__`, and an `ast`-parsed source guard now fails the suite if any `logger` call in the module is handed a bare `exc`.
- Browser layer re-validated: the existing Chromium suite (`tests/test_browser.py`, 127 tests including `TestSimplerFormInChromium`, which drives a real `show_tags = false` page and asserts the job row) passes against the new guards.

## The WR-06 before/after, explicitly

The review's named regression test is `TestProfileDefaultsFollowTheFormShape::test_a_cleared_tag_list_submits_no_tags`: `show_tags=True`, a profile carrying `default_tags = [41, 42]`, a submit with no `tags` field, asserted on the job row read back from the job store.

| | Job row value on the parent commit (`3199941`) | Job row value here |
|---|---|---|
| `job.tags`, cleared tag list, `show_tags=True` | `[41, 42]` — the profile's tags, silently re-applied | `[]` |
| `job.correspondent`, cleared, `show_correspondent=True` | `43` — the profile's correspondent | `None` |
| `job.tags`, no control, `show_tags=False` | `[41, 42]` | `[41, 42]` — D-29 preserved |
| `job.correspondent`, no control, `show_correspondent=False` | `43` | `43` — D-29 preserved |
| `job.tags`, submit names `tags=[7]`, `show_tags=True` | `[7]` | `[7]` — unchanged |

The RED run on the parent commit failed exactly three of the eight cases — the two cleared cases and the mixed-flags case's correspondent half — with `assert [41, 42] == []` and `assert 43 is None`. All eight pass here.

## Task Commits

1. **Task 1: the profile default applies when the control was absent, not when it was cleared**
   - RED: `3c78bac` (test) — `TestProfileDefaultsFollowTheFormShape`, 8 cases; 5 passed / 3 failed as designed
   - GREEN: `19a7b57` (fix) — the two gates plus the rewritten D-29 comment block
2. **Task 2: the index page does not fetch metadata it will not render**
   - RED: `f9bb738` (test) — `TestHiddenControlsCostNoMetadataFetch`, 6 cases; 2 passed / 4 failed as designed
   - GREEN: `86e97b6` (fix) — the `_tag_list_context` early return and the `index` correspondents guard
3. **Task 3: the last exception object in a routes log line**
   - RED: `4768070` (test) — `TestRouteLogsNameExceptionsOnly`, 2 cases; both failed as designed
   - GREEN: `f244c64` (fix) — `type(exc).__name__` in `paperless_test`

No REFACTOR commit was needed: `start_scan` gained two branches without crossing PLR0912, so the planned `_apply_form_shape_defaults` extraction was not required, and `_ScanForm` and `_status_context` are untouched.

## Files Created/Modified

- `src/saneless/web/routes.py` — `start_scan`'s fallback gated on the two `WebConfig` flags with a rewritten D-29 comment; `_tag_list_context` returns the emptied context before its fetch when `show_tags` is off; `index` fetches correspondents only when `show_correspondent` is on; `paperless_test` logs the exception class.
- `tests/test_web.py` — three new test classes (16 tests), the `_newest_job` / `_counted_app` helpers, a `_MetadataRequestCounter` mock-transport counter, and a `credential` keyword on the existing `_simple_form_app` so the placeholder-token refusal is reachable without a second fixture.

## Verification

| Gate | Result |
|---|---|
| `uv run pytest -q` | 2904 passed (parent commit: 2896; +8 net after the 16 new tests less none removed — the count is the two RED runs' tests now all green) |
| `uv run pytest tests/test_web.py -q` | 146 passed (parent: 130) |
| `uv run pytest tests/test_web.py tests/test_web_state_rendering.py -q` | 324 passed, no test removed |
| `uv run pytest tests/test_browser.py -q` | 127 passed (real Chromium) |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -c "tags = tags or found.default_tags" src/saneless/web/routes.py` | 0 |
| `grep -c "show_tags" src/saneless/web/routes.py` | 5 (≥ 3) |
| `grep -c "_get_cached_or_fetch(" src/saneless/web/routes.py` | 5 — unchanged from the parent commit; the call sites are guarded, not deleted |
| `grep -nE 'logger\.[a-z]+\([^)]*, *exc\)' src/saneless/web/routes.py` | 0 matches |
| `grep -c "type(exc).__name__" src/saneless/web/routes.py` | 2 (≥ 2) |
| `git diff --stat` | `src/saneless/web/routes.py` and `tests/test_web.py` only — no edit to `web/refresher.py`, `web/app.py` or `web/checks_cache.py` |

Test selection counts: `-k ProfileDefaultsFollowTheFormShape` selects 8 (≥ 7), `-k HiddenControlsCostNoMetadataFetch` selects 6 (≥ 6), `-k RouteLogsNameExceptionsOnly` selects 2 (≥ 2).

## Browser Validation

Per CLAUDE.md, no check here is left to a human. The page-shape claims this plan touches are covered by the project's own Playwright suite running real Chromium: `TestSimplerFormInChromium::test_the_tag_block_is_absent_and_the_profile_tags_still_apply` loads a `show_tags = false` server, asserts the Scan button is enabled and every tag selector has count 0, clicks Scan, and asserts the created job row carries the profile's tags. That test exercises the new `_tag_list_context` early return end to end and passes, as do the other 126 browser tests covering the full form's HTMX tag loading and filtering.

## Decisions Made

See `key-decisions` in the frontmatter. The load-bearing one: the gate reads the config key, not the submitted value. A submitted empty list is ambiguous by construction; the setting that decided which page was served is not. That also makes the gate a server-side fact a crafted submit cannot influence (T-30-23-02).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `_simple_form_app`'s new token argument tripped ruff S107**

- **Found during:** Task 1 (RED)
- **Issue:** The plan's requirement to reach `start_scan`'s placeholder-token refusal through the existing fixture needed a token argument on `_simple_form_app`. A parameter literally named `token` with a string default is `S107 Possible hardcoded password assigned to function default`, and the project forbids `# noqa`.
- **Fix:** Named the parameter `credential`, following the `_REFUSED_CREDENTIALS` / `_ACCEPTED_CREDENTIAL` naming `tests/test_web_errors.py` already uses for the same reason.
- **Files modified:** `tests/test_web.py`
- **Verification:** `uv run ruff check .` — No issues found.
- **Committed in:** `3c78bac` (part of the Task 1 RED commit)

**2. [Rule 2 - Robustness] The IN-02 source guard parses rather than greps**

- **Found during:** Task 3 (RED)
- **Issue:** The plan specified a line-matching guard. `web/routes.py` already wraps several `logger.warning(...)` calls over three lines, so a line-based regex would silently miss the exact shape most likely to be reintroduced.
- **Fix:** The guard walks `ast.parse(source)` for `Call` nodes on `logger.*` and fails on any positional or keyword argument that is the bare name `exc`. The plan's `grep -nE` acceptance criterion is also satisfied (0 matches).
- **Files modified:** `tests/test_web.py`
- **Verification:** The guard failed on the parent commit's line 847 and passes after `f244c64`.
- **Committed in:** `4768070` (part of the Task 3 RED commit)

---

**Total deviations:** 2 auto-fixed (1 × Rule 3, 1 × Rule 2)
**Impact on plan:** None on scope. Both changes are inside the plan's own files and serve the plan's stated intent; neither adds a dependency or a new surface.

## Issues Encountered

**The plan's `time.sleep` verification baseline is stale, not violated.** The plan asserts `grep -rc "time\.sleep" tests/` still totals 17; the tree totals 19 both before and after this plan. `tests/test_web.py` holds exactly 1 occurrence on the parent commit and 1 here — this plan added none. The two extra occurrences predate this worktree's base commit (`3199941`) and belong to other work; flagged here rather than chased, per the scope boundary.

## Threat Flags

None. No new network endpoint, auth path, file access pattern or schema change. The three registered mitigations are implemented and pinned: `T-30-23-01` by the behavioural log test and the `ast` guard, `T-30-23-02` by the config-key gate (asserted for both flags and their independence), `T-30-23-03` by the zero-request page-load tests. `T-30-23-04` remains accepted as stated. `pyproject.toml` is untouched, so `T-30-23-SC` is n/a as planned.

## Known Stubs

None. The empty context `_tag_list_context` returns when `show_tags` is off is a deliberate, documented guard and not a placeholder: it is the normal path's exact key set with empty values, and it is reached only when the template that would read those keys is not rendered.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

The three findings this plan owns (WR-06, IN-01, IN-02) are closed and guarded. Nothing here blocks the rest of the wave: the diff is confined to `web/routes.py` and `tests/test_web.py`, and the files other wave-1 plans own (`web/refresher.py`, `web/app.py`, `web/checks_cache.py`) are untouched.

## Self-Check: PASSED

- `src/saneless/web/routes.py` — FOUND (modified)
- `tests/test_web.py` — FOUND (modified)
- Commits `3c78bac`, `19a7b57`, `f9bb738`, `86e97b6`, `4768070`, `f244c64` — all FOUND in `git log`

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-17*
