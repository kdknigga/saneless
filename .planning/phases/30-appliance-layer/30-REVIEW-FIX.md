---
phase: 30-appliance-layer
fixed_at: 2026-09-18T04:50:00Z
review_path: .planning/phases/30-appliance-layer/30-REVIEW.md
iteration: 1
findings_in_scope: 6
fixed: 6
skipped: 0
status: all_fixed
---

# Phase 30: Code Review Fix Report

**Fixed at:** 2026-09-18T04:50:00Z
**Source review:** .planning/phases/30-appliance-layer/30-REVIEW.md (round 4)
**Iteration:** 1

**Summary:**
- Findings in scope: 6 (scope `all`: 3 warnings, 3 info)
- Fixed: 6
- Skipped: 0

Every fix was made in an isolated worktree on a temp branch and fast-forwarded
onto `autodev`. Final state of the tree after the last commit: fast suite
3072 passed (3051 before this round; +21 new cases), Chromium suite 124 passed,
`ruff check .`, `ruff format --check .`, `ty check` and
`pyrefly check src tests` all clean (pyrefly's 16 warnings are the pre-existing
baseline, identical on the untouched checkout).

## Fixed Issues

### R4-WR-01: R3-CR-02's exemption is not scoped to the poll, so a failing `Check again` deletes the strip -- and `refresh_checks` has no fallback

**Files modified:** `src/saneless/web/errors.py`, `src/saneless/web/routes.py`, `docs/reference/web-api.md`, `tests/test_web_errors.py`, `tests/test_web_checks.py`
**Commit:** c01e642
**Applied fix:** The retarget exemption in `render_error` is now
`_is_the_strip_fetching_itself(request)`: `HX-Target == checks-body` **and**
`method == GET`. The poll and the terminal-state reload are both
`GET /api/checks`; the only POST aimed at the strip is the `Check again`
button, whose failure now lands in `#status-message` like every other click's,
leaving the strip and the button on the page. `refresh_checks` guards its
`_checks_context` render the way `get_checks` does and falls back to
`_checks_fallback_context` at 200; the probe deliberately stays outside the
guard (a raising probe is a failed click, reported as an error, not hidden
behind a cold strip). Module docstring, constant comment and route docstring
updated; `web-api.md` no longer claims "a method the route does not serve"
replaces the strip and documents the POST route's fallback.

*Deviation from the suggested fix:* the reviewer proposed keying on
`request.url.path == "/api/checks"` as well. That would have broken all 17
cases of `TestChecksPollTargetIsExemptFromTheRetarget`, which drive the
exemption through the test-only `/_test/reject/{name}` route so every
`RequestRejection` is covered. The method alone separates the button from the
poll, and both halves of the key are pinned.

*Tests added:* `test_a_post_aimed_at_the_strip_is_still_retargeted`
(parametrised over every `RequestRejection`; the test route now serves POST),
`test_a_refused_check_again_click_lands_in_the_slot` (real
`POST /api/checks/refresh`, refused by `CrossOriginGuard`, carries
`HX-Retarget`), `test_a_get_aimed_at_the_strip_is_the_only_exempt_shape`,
`test_a_failure_inside_the_refresh_render_is_a_cold_strip_at_200`,
`test_the_refresh_failure_is_logged_with_its_traceback`, and
`test_a_probe_that_raises_is_a_failed_click_not_a_cold_strip`.
*Mutation:* dropping the method half of the key fails 14 cases. The four
Chromium cases in `TestPollEndsOnAnErrorResponse` and the real-click
`Check again` case pass unchanged; no browser test needed editing.

### R4-WR-02: `docs/reference/web-api.md` states that an out-of-range counter stops the poll; a below-range counter restarts it, and a test pins that

**Files modified:** `docs/reference/web-api.md`
**Commit:** 232b76a
**Applied fix:** Replaced the "nothing asks again" sentence with one that
states both directions of the clamp: above the range clamps to the larger cap
(give-up body, no poll); below the range -- which nothing on the page sends --
is read as zero and comes back as the first body of a fresh, bounded chain;
a rendering failure is the cold-start body with no poll. Now agrees with the
route docstring and `test_a_negative_attempt_is_the_start_of_a_chain`.

### R4-WR-03: the numeric-address guard's own docstring names `0.0.0.0` as the worst case, and the code dials it

**Files modified:** `src/saneless/checks.py`, `tests/test_checks.py`
**Commit:** 68e6231
**Applied fix:** `_segment_is_a_numeric_address_shorthand` now returns
`address.is_unspecified` for a legal dotted quad, so `0.0.0.0` is refused by
name while every other literal is still dialled. Docstrings of both the
shorthand guard and `_looks_like_a_host_name` state the exception.
*Tests added:* `test_the_unspecified_address_is_never_dialled` (parametrised
over `0.0.0.0` and `0.0.0.0:6566`, asserting `_saned_hosts(...) == ()`) and
`test_a_stray_character_in_a_port_does_not_add_a_loopback_dial`
(`_saned_hosts("scanbox:0.0.0.0") == (("scanbox", SANED_PORT),)`).
*Mutation:* reverting to `return False` fails all three.

### R4-IN-01: once an error body replaces the strip, nothing on the page can restore it, and two other swaps silently miss

**Files modified:** `docs/reference/web-api.md`
**Commit:** cc7e48c
**Applied fix:** Added the missing sentences: the strip does not come back on
its own after the exempt swap (no `checks-body` id on the error body, so the
terminal-state reload and the scan submit's out-of-band strip find nothing to
replace, and a later scan's paused note has nowhere to land); a page reload
restores it; and why that is accepted (after R4-WR-01 only the poll's own GET
with a counter nothing on the page mints can reach it).
*Decision, made deliberately:* `partials/error.html` was **not** given the
target id. Doing so would make an error partial a target for the out-of-band
swap and the terminal reload and would invalidate the pinned browser
assertion that `#checks-body` has count 0 after the swap, for a case only a
tampered request can reach. Existing browser assertions are unchanged and
still pass.

### R4-IN-02: `_checks_fallback_context`'s give-up line is justified with a claim that is false whenever the cache is warm

**Files modified:** `src/saneless/web/routes.py`
**Commit:** e4aa92d
**Applied fix:** Docstring rewritten to the real reason: the guard covers the
whole render, so a raise from the worker's job lookup, the refresher's lock or
the clock produces this body on an appliance whose cache may hold five true
rows; the body asserts nothing about the cache beyond the fact that it could
not be shown, and the line names the `Check again` button still on the page.
No behaviour change (the last-known-good alternative was not taken).

### R4-IN-03: three Jinja filters are registered with no template consumer

**Files modified:** `src/saneless/web/app.py`, `tests/test_web.py`
**Commit:** beaa24c
**Applied fix:** Grepped `src/saneless/web/templates`: no template uses
`check_state_class`, `check_state_glyph` or `check_state_label` (only the
`check_row_*` trio). Dropped the three registrations and app.py's now-unused
imports; comment rewritten to say they are deliberately not filters and why.
The Python functions stay. `tests/test_web.py`: the two registration tests
drop the three names (`test_registers_the_eleven_new_filters` renamed to
`test_registers_the_filters_the_templates_reach_for`), and a new
`test_the_state_lookups_are_not_filters` pins their absence.

---

_Fixed: 2026-09-18T04:50:00Z_
_Fixer: Claude (gsd-code-fixer)_
_Iteration: 1_
