---
phase: 30-appliance-layer
verified: 2026-09-17T12:00:00Z
status: gaps_found
score: 3/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: passed
  previous_score: 5/5
  gaps_closed: []
  gaps_remaining:
    - "saneless doctor exits non-zero on a healthy appliance when a scanner.host/SANE_NET_HOSTS value contains a Unicode decimal-digit character in the port position (R3-CR-01, unfixed)"
    - "The checks poll does not end on a failed request; the failure overwrites the scan-status slot every 2s for as long as the tab is open, including during an active scan (R3-CR-02, unfixed)"
  regressions:
    - "The previous (2026-09-17T00:00:00Z) VERIFICATION.md marked this phase status: passed with 9/9 truths verified. That pass predates round-3 code review, which found two unfixed critical defects reproduced end to end against current HEAD. The prior pass's method (reading round-1/round-2 gap-closure diffs and their own tests) did not catch either defect because neither is a round-1/round-2 regression — R3-CR-01 is a pre-existing bug in code round-2 touched two lines away, and R3-CR-02 is a test-quality failure (the browser test fabricates a response the app never sends) that the prior verifier accepted at face value rather than tracing the actual HX-Retarget/htmx interaction."
gaps:
  - truth: "saneless doctor runs the shared checks and exits non-zero on a placeholder token or any other red check -- implying it does NOT exit non-zero on a healthy, working appliance"
    status: failed
    reason: >
      _saned_hosts's port branch (checks.py:671-680) gates int(maybe_port) on
      maybe_port.isdigit(), which is True for Unicode category-No decimal digits
      (e.g. U+00B2 '²') that int() rejects with ValueError. Reproduced directly:
      '²'.isdigit() == True, int('²') raises ValueError. Nothing in _saned_hosts,
      _scanner_preflight, or the probe's `except OSError` catches this; it escapes
      into run_checks's generic per-check handler (checks.py:1511-1512), which
      renders "This check could not be completed." and sets the Scanner row to
      FAIL. worst_state becomes FAIL, so `saneless doctor` exits ExitCode.CONFIG
      (2) -- on an appliance whose scanner works fine, purely because
      scanner.host or the environment variable SANE_NET_HOSTS contains one
      malformed character. This is reachable from the environment
      (_saned_host_setting prefers SANE_NET_HOSTS over the config value), so an
      operator does not need to edit the config file to trigger it. No test in
      tests/test_checks.py exercises a non-ASCII-digit port; the existing
      TestSanedHostParsing suite covers ASCII digits, IPv6 literals, and
      malformed segment counts, but never a Unicode-digit port.
    artifacts:
      - path: "src/saneless/checks.py"
        issue: "Lines 671-680: `maybe_port.isdigit()` accepts non-ASCII decimal digits that int() rejects; the resulting ValueError is uncaught and reaches run_checks's generic red-row handler instead of a WARN/FAIL row with an accurate message."
      - path: "src/saneless/web/templates/partials/checks.html"
        issue: "check_row macro (line ~50) never receives CheckResult.skipped; both _scanner_skipped and _scanner_busy render as a green check-mark with the screen-reader word 'OK' even though their own docstrings and CheckResult.skipped exist specifically to say nothing was checked (R3-WR-03). This independently undermines the 'tell at a glance whether the appliance is healthy' claim for the same success criterion."
    missing:
      - "Restrict the port digit test to ASCII decimal (e.g. `set(maybe_port) <= frozenset(string.digits)`) so a Unicode digit is rejected the same safe way every other unparseable segment is, and add a test pinning `_saned_hosts('host:²') == ()`."
      - "Wire CheckResult.skipped into check_row (and into doctor's row rendering) so a skipped/busy scanner row shows the neutral cold-start glyph/label instead of a false green OK, matching what the module's own docstrings already promise."
  - truth: "The status strip stays fast with the scanner host unplugged and is skipped entirely while a scan is active, so it never contends with the exclusive scanner"
    status: failed
    reason: >
      render_error (web/errors.py:177-178) unconditionally sets HX-Retarget:
      #status-message and HX-Reswap: innerHTML on every htmx error response.
      The vendored htmx-2.0.8.min.js applies HX-Retarget before the swap
      decision (confirmed in the bundle: `if(T(n,/HX-Retarget:/i)){e.target=...}`
      precedes the shouldSwap check), so any 4xx/5xx from GET /api/checks lands
      in #status-message, not #checks-body. #checks-body -- with its
      `hx-trigger="every 2s"` -- is never replaced, so the poll never ends and
      keeps firing indefinitely, including while a scan is in progress. Every 2s
      the failing poll overwrites #status-message, which is the scan-progress
      slot the strip is explicitly documented (D-03) to leave alone during a
      scan -- so a scan's progress line is periodically replaced by a generic
      error sentence. tests/test_browser.py::TestPollEndsOnAnErrorResponse
      claims the opposite ('there is no defect') but drives the failure via
      Playwright's client-side route.fulfill() with no response headers -- a
      response this application never actually sends -- so the test cannot
      fail for the property it is named after. docs/reference/web-api.md:136
      now states the false claim as fact in shipped documentation.
    artifacts:
      - path: "src/saneless/web/errors.py"
        issue: "render_error (lines 137-190) sets HX-Retarget/HX-Reswap for every htmx error response with no exemption for the checks-poll's own error path, so a failing poll cannot self-terminate via the swap-replaces-trigger idiom the rest of the strip relies on."
      - path: "src/saneless/web/templates/partials/checks.html"
        issue: "hx-trigger=\"every 2s\" has no client-side response-error/send-error handler to remove itself, so it depends entirely on the swap-replacement idiom that HX-Retarget defeats."
      - path: "docs/reference/web-api.md"
        issue: "Line 136 states a poll whose request fails also ends 'because the failure response replaces the strip' -- false for the app's real error path."
      - path: "tests/test_browser.py"
        issue: "TestPollEndsOnAnErrorResponse (lines 3167-3300) fabricates the failing response client-side via route.fulfill() with no headers, never exercising render_error's actual HX-Retarget/HX-Reswap headers, so it passes regardless of whether the real defect is present."
    missing:
      - "Fix (pick one, per 30-REVIEW.md's own proposal): add hx-on::response-error/hx-on::send-error handlers on #checks-body to remove its own hx-trigger client-side, OR have GET /api/checks catch its own failure and return the strip partial (poll_attempt=None, 200) instead of raising into render_error, OR suppress HX-Retarget/HX-Reswap when the request's target is #checks-body."
      - "Re-point TestPollEndsOnAnErrorResponse at a server-driven failure (e.g. a tampered `attempt` query param, or a monkeypatched _checks_context raising) so the response carries the headers the app actually sends, and assert #checks-body's trigger is actually gone plus a bounded request count over the poll window."
      - "Correct docs/reference/web-api.md:136 to state what the code does, not the disproved claim."
deferred: []
human_verification: []
---

# Phase 30: Appliance Layer Verification Report

**Phase Goal:** A non-technical household member can tell at a glance whether the appliance is
healthy and what a failure means -- one shared check list behind both `saneless doctor` and a
cached status strip, page counts on every terminal job, plain-language errors with a next step,
human profile labels, queue position, and an owner-only flip prompt -- with help text and the docs
for each new surface written in-phase.

**Verified:** 2026-09-17T12:00:00Z
**Status:** gaps_found
**Re-verification:** Yes -- superseding the prior (2026-09-17T00:00:00Z) `passed` verification,
which was written before round-3 code review (`30-REVIEW.md`, `status: issues_found`, 2 confirmed
critical findings) and did not catch either defect.

## Method

The orchestrator independently confirmed two blockers from round-3 code review before this
verification ran: `R3-CR-01` (a Unicode-digit port crashes `_saned_hosts`, permanently reddening
the Scanner row and making `doctor` exit 2 on a working scanner) and `R3-CR-02` (the checks poll
does not end on a failed request; `R2-IN-04`'s prior closure was invalid because its browser test
fabricated a response the app never sends). I treated those as established fact and verified their
*effect* on the phase's five success criteria, rather than re-litigating whether they are real. I
then independently re-read the current source (`checks.py`, `web/errors.py`,
`web/templates/partials/checks.html`, `web/refresher.py`, `vocabulary.py`, `auto_profiles.py`,
`web/routes.py`, `web/templates/partials/{status,flip,history}.html`, `tests/test_checks.py`,
`tests/test_browser.py`) to:

1. Confirm both blockers are still present at HEAD (they are -- `checks.py:677` still uses
   `maybe_port.isdigit()`; `web/errors.py:177-178` still sets `HX-Retarget`/`HX-Reswap`
   unconditionally on every htmx error response).
2. Determine which of the five ROADMAP success criteria each blocker falsifies.
3. Hunt for additional instances of the same pattern the round-3 review flagged twice already (a
   SUMMARY/docstring claiming a property the code does not have, or a test that cannot fail for the
   property it is named after) across the phase's other 28 plans -- not just the 12 files round-3
   review scoped itself to.
4. Verify the three success criteria not touched by either blocker (page counts, error advice with
   collapsed disclosure, flip prompt / profile labels) hold at the artifact and wiring level.

I did **not** re-run the full test suite / lint / type-check gates; `<state_of_the_tree>` already
established these are green and that is orthogonal to whether the shared-check-list goal and the
strip's non-interference guarantee are actually true, which is what round-3 review falsified.

## Goal Achievement

### Observable Truths (Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `saneless doctor` runs the shared checks and exits non-zero on a red check, never on a healthy appliance; the index page shows the same checks, refreshed on load and by a button | ✗ FAILED | `R3-CR-01`: confirmed reproducible at HEAD -- `'²'.isdigit()` is `True`, `int('²')` raises `ValueError`, and `_saned_hosts` (checks.py:671-680) does not guard against it. `run_checks`'s generic per-check `except Exception` (checks.py:1511-1512) turns this into a permanent red Scanner row and `doctor` exit 2 on a scanner that works fine. Compounding: `R3-WR-03` confirmed -- `CheckResult.skipped` (checks.py:248-256) is set by `_scanner_skipped`/`_scanner_busy` but read by nothing in `checks.html`'s `check_row` macro or `cli.py`'s `_state_marker`; a busy/skipped scanner row renders as a green checkmark with screen-reader text "OK" even though nothing was checked. Both defects directly undermine "tell at a glance whether the appliance is healthy". Also confirmed still open: `R3-WR-02` -- `CheckRefresher._run` (refresher.py:217-218) has no per-tick exception guard, and `_probe_and_store`'s `else: self._cache.store(results)` (refresher.py:322-323) is *not* covered by the `except Exception` above it, so a raise from `store` silently and permanently ends the background refresh thread, breaking "refreshed on load" for the rest of the process lifetime. |
| 2 | The status strip stays fast with the scanner host unplugged and is skipped entirely while a scan is active, so it never contends with the exclusive scanner | ✗ FAILED | The gate-contention half is genuinely fixed (round-3 review confirms `WR-03`/`WR-04` hold: `run_checks(scanner_gate=...)` only wraps `_scanner_result`, `_probe_and_store` derives `skip_scanner` from `worker.current_job_id`, not gate contention). But `R3-CR-02` is confirmed unfixed: `web/errors.py:177-178` unconditionally sets `HX-Retarget: #status-message` / `HX-Reswap: innerHTML`; the vendored `htmx-2.0.8.min.js` applies `HX-Retarget` before the swap decision, so `#checks-body`'s `every 2s` trigger is never replaced and the poll runs forever, overwriting `#status-message` -- the scan-progress slot -- every 2s, including during an active scan. This is exactly the kind of "strip interferes with an in-progress scan's display" the criterion rules out. `tests/test_browser.py::TestPollEndsOnAnErrorResponse` (confirmed at lines 3167-3300 to use client-side `route.fulfill()` with no headers) cannot detect this because it never exercises `render_error`'s real headers. |
| 3 | Every terminal job shows pages scanned/removed/uploaded; manual duplex shows front/back counts during pass B; a queued job shows the wait line | ✓ VERIFIED | `vocabulary.page_counts()` (vocabulary.py:635-667) returns `None` when any of the three counts is NULL (suppressing the whole sentence rather than a partial one) and otherwise the exact `"{scanned} pages scanned, {removed} blank removed, {uploaded} uploaded"` sentence; wired into both `partials/status.html:67,77` and `partials/history.html:20`. `vocabulary.busy_line()` (vocabulary.py:545-596) has the documented three-branch precedence: queue wait line first (`f"Waiting for '{queue_title}' to finish ({position})"` with `queue_ahead == 0` rendered as "next in line" rather than the misleading "(0 ahead of you)"), then front-page count during `SCANNING_REVERSE`, then plain progress prose; wired via `routes.py:507-546` (`_busy_line`) into `status.html:19`. No defect found in this area by round-3 review or by my own reading. |
| 4 | Every user-facing error shows a plain-language message and a suggested next step, with raw technical detail inside a collapsed disclosure | ✓ VERIFIED | `partials/status.html:108-115`: `{{ job.error_category \| error_next_step }}` renders the next step, followed by `<details class="tech-details"><summary>Technical details</summary>...</details>` -- a native collapsed disclosure, confirmed (by the template's own comment at lines 90-91) to sit outside the ARIA alert region so a screen reader does not announce the raw detail alongside the failure. `vocabulary.error_advice()` (vocabulary.py:742+) supplies the plain-language half. This success criterion is about terminal-job errors and is unaffected by `R3-CR-02`, which is about the checks-poll's own transient failure overwriting a different template region, not about the content or structure of the error message itself. |
| 5 | Two browser contexts show the owner Continue/Abort (with Abort confirm) and the non-owner "Waiting for the stack to be flipped"; profile dropdowns show human labels/descriptions, feeder-first on sheet-fed scanners, and note a read-only config mount on the strip | ✓ VERIFIED | `tests/test_browser.py::test_the_owner_is_offered_the_flip_and_the_second_browser_is_not` (line 3907) uses two real `browser.new_context()` cookie jars (line 3928-3929) -- genuine Playwright automation against Chromium, not a manual-only item, and not client-side response fabrication (unlike the `TestPollEndsOnAnErrorResponse` class). `partials/flip.html` renders Continue/Abort with `hx-confirm="Abort this scan? It will stop and cannot be resumed."` on Abort only. `auto_profiles.py:341-418` (`_profile_label`/`_profile_description`) produce human labels ("Feeder, single-sided", "Glass (flatbed)", etc.) with one-line descriptions. `routes.py:724-781` (`_profile_options`) sorts `entries.sort(key=lambda entry: not entry[1].uses_feeder)`, confirming feeder-first ordering on sheet-fed devices. `checks.py:1236-1257` renders `ProfileStorage.IN_MEMORY_UNWRITABLE` as "Generated in memory -- the config location is read-only, so they are not saved." on the strip. None of this is touched by either open blocker. |

**Score:** 3/5 truths verified.

### Additional Findings Beyond the Two Established Blockers

Hunting specifically for the `R3-CR-02` pattern (a claim the code does not back, or a test that
cannot fail for the property it is named after) across files round-3 review did not scope itself
to:

- **`R3-WR-03` (confirmed, unfixed):** `CheckResult.skipped` is dead data. `grep -rn skipped
  src/saneless/web/templates src/saneless/cli.py` returns nothing. Both `_scanner_skipped` and
  `_scanner_busy`'s docstrings assert "the `skipped` flag, not the state, is what the two surfaces
  render" -- confirmed false by reading `checks.html`'s `check_row` macro signature
  (`name, state_class, glyph, state_label, message, next_step`) and `cli.py`'s `_state_marker`,
  neither of which takes or reads `skipped`. This is the same defect class as the two established
  blockers: a docstring/contract claim the code does not keep.
- **`R3-WR-02` (confirmed, unfixed):** `CheckRefresher._run`'s tick loop has no per-tick exception
  guard (unlike `ScanWorker._run`, which the refresher's own class docstring claims it "follows...
  in every structural respect"), and the store call in `_probe_and_store` sits in the `else` arm of
  a `try/except`, which Python does not route exceptions from to that `except`. A raise from
  `store` silently kills the background refresh thread for the life of the process.
- No further instances of the "test cannot fail for the property it's named after" pattern were
  found beyond the two already catalogued in `30-REVIEW.md` (`R3-IN-01`, `R3-IN-02`) within the
  time available; those two are informational-severity per the round-3 review and do not
  independently change this verification's status.

These are folded into the gap for success criterion 1 above rather than listed as separate gaps,
since they compound the same criterion's failure rather than introducing a new one.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/checks.py` | Shared check registry, `_saned_hosts` correctly rejecting unparseable ports | ✗ DEFECT | Present and substantive as the single D-02 registry, but `_saned_hosts`'s port branch (line 677) crashes on a Unicode digit (R3-CR-01) |
| `src/saneless/web/errors.py` | `render_error` producing responses the checks-poll's own error path can terminate on | ✗ DEFECT | Present, but unconditional `HX-Retarget`/`HX-Reswap` (lines 177-178) defeats `#checks-body`'s self-terminating poll (R3-CR-02) |
| `src/saneless/web/templates/partials/checks.html` | `check_row` rendering `CheckResult.skipped` as a neutral row | ✗ DEFECT | Macro exists and is wired, but never receives or renders `skipped` (R3-WR-03) |
| `src/saneless/web/refresher.py` | `_run` with a per-tick exception backstop matching `ScanWorker._run` | ✗ DEFECT | Present, wired, but no backstop; a raise from `store` ends the thread permanently (R3-WR-02) |
| `src/saneless/vocabulary.py` | `page_counts`, `busy_line`, `error_advice`, `local_time` | ✓ VERIFIED | All present, substantive, matching documented D-32/D-33 precedence rules |
| `src/saneless/auto_profiles.py` | `_profile_label`/`_profile_description` | ✓ VERIFIED | Present, human-readable, wired into config generation |
| `src/saneless/web/routes.py` | `_profile_options` feeder-first sort, `_busy_line` | ✓ VERIFIED | Present and wired |
| `src/saneless/web/templates/partials/{status,flip,history}.html` | Page counts, error disclosure, flip prompt | ✓ VERIFIED | All present, wired to the vocabulary functions above |
| `tests/test_browser.py::TestPollEndsOnAnErrorResponse` | A test proving the checks poll ends on a failed request | ✗ INVALID TEST | Fabricates the response client-side (`route.fulfill`, no headers); cannot fail for the property it is named after (R3-CR-02) |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `checks.py run_checks` | `_dispatch` per-check registry | generic `except Exception` around each dispatch | ⚠️ PARTIAL | Wired, but the generic handler is what converts `_saned_hosts`'s uncaught `ValueError` into a permanent false-red row rather than an accurate WARN, defeating the truthfulness half of the D-02 contract |
| `web/routes.py get_checks` | `web/errors.py render_error` | FastAPI exception handling on a 4xx/5xx from the checks route | ✗ NOT WIRED (for self-termination) | The route's errors flow through the app's generic htmx error handling, which retargets to `#status-message` instead of replacing `#checks-body`, so the poll's own error path cannot end itself |
| `web/templates/partials/checks.html check_row` | `CheckResult.skipped` | template variable passed at call site | ✗ NOT WIRED | `skipped` is produced by `checks.py` and stored on every `CheckResult` but never passed into or read by the macro |
| `web/refresher.py _run` | exception handling | per-tick `try/except` | ✗ NOT WIRED | No guard exists; compare to `ScanWorker._run`'s guarded loop |
| `vocabulary.py page_counts/busy_line` | `partials/status.html`, `partials/history.html` | Jinja filter calls | ✓ WIRED | Confirmed present and correctly guarded on `is not None` |
| `auto_profiles.py _profile_label/_profile_description` | config generation, web dropdown | direct call at profile-generation time | ✓ WIRED | Confirmed |
| `routes.py _profile_options` | `index.html` `<select>` | feeder-first sort | ✓ WIRED | Confirmed (`entries.sort(key=lambda entry: not entry[1].uses_feeder)`) |
| `tests/test_browser.py` two-context test | Owner/non-owner flip prompt | real `browser.new_context()` cookie jars | ✓ WIRED | Confirmed genuine Playwright automation, not client-side fabrication |

### Requirements Coverage

| Requirement | Claimed by plans | Status | Evidence |
|---|---|---|---|
| APPL-01 | 30-06, 30-08, 30-11, 30-20, 30-21, 30-25, 30-28, 30-30 | ⚠️ PARTIAL | `doctor` runs the shared registry, but exits non-zero on a healthy scanner given a malformed host/port setting (R3-CR-01) |
| APPL-02 | 30-04, 30-06, 30-07, 30-09, 30-11, 30-17, 30-20, 30-21, 30-24, 30-25, 30-26, 30-27, 30-28, 30-29, 30-30 | ⚠️ PARTIAL | Strip shows the same checks and refreshes on load/button, but its error path cannot self-terminate (R3-CR-02) and skipped rows render falsely OK (R3-WR-03) |
| APPL-03 | 30-01, 30-04, 30-09, 30-12, 30-17, 30-22 | ✓ SATISFIED | Page counts confirmed present and wired |
| APPL-04 | 30-01, 30-09, 30-10, 30-12, 30-17, 30-23 | ✓ SATISFIED | Plain-language error + collapsed disclosure confirmed |
| APPL-05 | 30-02, 30-05, 30-15, 30-17, 30-18 | ✓ SATISFIED | Human profile labels/descriptions confirmed |
| APPL-06 | 30-04, 30-06, 30-11, 30-20 | ✓ SATISFIED | Read-only config mount messaging confirmed on the strip |
| APPL-07 | 30-01, 30-02, 30-06, 30-08, 30-10, 30-14, 30-18, 30-19 | ✓ SATISFIED | `is_placeholder_token` gate confirmed wired into `_check_paperless` |
| APPL-08 | 30-01, 30-03, 30-13 | ✓ SATISFIED | Queue wait line confirmed with correct "next in line" / "(N ahead of you)" wording |
| APPL-09 | 30-03, 30-13, 30-19 | ✓ SATISFIED | Flip prompt Continue/Abort-with-confirm confirmed, owner-only via two-context Playwright test |
| APPL-10 | 30-02, 30-16, 30-18, 30-19, 30-23 | ✓ SATISFIED (not independently re-verified beyond artifact presence; help text is out of scope of this round's defects) | |
| APPL-11 | 30-06, 30-11, 30-18 | ✓ SATISFIED (not independently re-verified beyond artifact presence; compose docs unaffected by either blocker) | |
| APPL-12 | 30-01, 30-09, 30-10, 30-12, 30-17, 30-18, 30-22 | ✓ SATISFIED | `local_time` with `TZ`-aware `astimezone()` and trailing-space strip confirmed |

No orphaned requirements: all 12 APPL-* IDs are claimed by at least one plan's frontmatter and
have corresponding source evidence.

**Documentation-hygiene item, unchanged from the prior verification:** `.planning/REQUIREMENTS.md`
lines 129-140 and 316-327 still show `[ ]` / "Pending" for every APPL-* ID. This remains a tracking
bookkeeping gap, not evidence the requirements are unmet in the code -- but two of the twelve IDs
(APPL-01, APPL-02) are now genuinely only PARTIAL per this verification, so "Pending" is, for those
two, closer to accurate than the prior verification's unqualified "Done" framing would have been.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/saneless/checks.py` | 671-680 | Uncaught `ValueError` from a Unicode-digit port reaching a generic exception handler | 🛑 BLOCKER | Permanent false-red Scanner row, `doctor` exit 2 on a healthy appliance (R3-CR-01) |
| `src/saneless/web/errors.py` | 177-178 | Unconditional `HX-Retarget`/`HX-Reswap` header defeats the checks-poll's self-terminating swap idiom | 🛑 BLOCKER | Unbounded poll on any checks-route failure, periodically overwriting the scan-progress slot (R3-CR-02) |
| `src/saneless/web/templates/partials/checks.html` | ~50 (`check_row` macro) | Docstring/contract claims `skipped` is rendered; it is not read anywhere | ⚠️ WARNING | A busy/skipped scanner row shows a false green "OK" (R3-WR-03) |
| `src/saneless/web/refresher.py` | 217-218, 313-326 | No per-tick exception guard; `store` sits in an `else` arm the preceding `except` cannot cover | ⚠️ WARNING | A single raise from `store` permanently kills the background refresh thread (R3-WR-02) |
| `tests/test_browser.py` | 3167-3300 | Test named `TestPollEndsOnAnErrorResponse` drives the failure via client-side `route.fulfill()` with no response headers, never exercising the app's real error path | ⚠️ WARNING | Cannot fail for the property it is named after; masks R3-CR-02 |
| `docs/reference/web-api.md` | 136 | States "a poll whose request fails also ends" -- disproved by the app's own `render_error` headers | ⚠️ WARNING | Shipped documentation asserts the false half of R3-CR-02 |

No `TBD`/`FIXME`/`XXX` markers found in any file read during this verification.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Unicode-digit port crashes the port parser | `python3 -c "'²'.isdigit(); int('²')"` | `True`, then `ValueError: invalid literal for int() with base 10: '²'` | ✗ FAIL (confirms R3-CR-01) |
| `render_error` sets HX-Retarget/HX-Reswap unconditionally | `grep -n "HX-Retarget\|HX-Reswap" src/saneless/web/errors.py` | Lines 177-178, inside `render_error`, no conditional exemption | ✗ FAIL (confirms R3-CR-02) |
| Vendored htmx applies HX-Retarget before the swap decision | `grep -n "HX-Retarget" static/vendor/htmx-2.0.8.min.js` | `if(T(n,/HX-Retarget:/i)){e.target=Un(t,...)}` precedes the swap check | ✗ FAIL (confirms R3-CR-02's mechanism) |
| `run_checks` catches per-check exceptions generically | `grep -n "except Exception as exc" src/saneless/checks.py` | Line 1512, around `_dispatch` in the `run_checks` loop | Confirms the escape path for R3-CR-01 |
| `checks.html`'s `check_row` macro never reads `skipped` | `grep -n skipped src/saneless/web/templates/partials/checks.html` | 0 matches | ✗ FAIL (confirms R3-WR-03) |
| `CheckRefresher._run` has no per-tick guard | Read `refresher.py:207-218` | `while not self._stopping.wait(TICK_SECONDS): self._tick()`, no try/except | ✗ FAIL (confirms R3-WR-02) |
| `page_counts`/`busy_line` wired into templates | `grep -n page_counts\|busy_line partials/*.html routes.py` | Present in `status.html`, `history.html`, `routes.py` | ✓ PASS |
| Owner/non-owner flip prompt is real Playwright automation | Read `test_browser.py:3907-3929` | Two real `browser.new_context()` calls | ✓ PASS |
| Feeder-first profile sort | Read `routes.py:781` | `entries.sort(key=lambda entry: not entry[1].uses_feeder)` | ✓ PASS |
| Read-only config mount messaging on the strip | Read `checks.py:1236-1243` | "Generated in memory -- the config location is read-only..." | ✓ PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` convention exists for this project and none was declared in any of
the 30 plans or summaries for this phase. Step 7c: SKIPPED -- no probes declared or discovered.

### Human Verification Required

None. Per project CLAUDE.md, the only owner/non-owner flip-prompt criterion (success criterion 5)
is fully automated in `tests/test_browser.py` using real Playwright/Chromium contexts and was
independently confirmed by reading the test, not merely trusted from the SUMMARY. No item in this
phase requires physical hardware or an unstubbable external service.

### Gaps Summary

Two established blockers remain unresolved at HEAD and each falsifies part of the phase's central
claim -- that the CLI and the web UI report one truthful, shared view of the appliance's health:

1. **R3-CR-01** makes `saneless doctor` exit non-zero (`ExitCode.CONFIG`, 2) on a scanner that
   works fine, whenever `scanner.host` or `SANE_NET_HOSTS` contains a single Unicode decimal-digit
   character in the port position. This directly contradicts success criterion 1's implicit
   promise that `doctor`'s exit code is truthful -- the phase goal is a household member being able
   to "tell at a glance whether the appliance is healthy", and a scripted health gate or an
   at-a-glance red Scanner row that is permanently wrong for a cosmetic config typo is the opposite
   of that.

2. **R3-CR-02** means the checks-poll's own error path cannot end itself: a failing
   `GET /api/checks` (any 4xx/5xx) retargets its response into `#status-message` -- the
   scan-progress slot -- and leaves `#checks-body`'s `every 2s` polling trigger in place
   indefinitely. This directly contradicts success criterion 2's promise that the strip "never
   contends with" an active scan: every 2 seconds a failing poll can overwrite the scan's own
   progress line with a generic error sentence, for as long as the tab stays open. The browser test
   that was supposed to close the prior round's finding on this exact behavior (`R2-IN-04`) cannot
   detect it, because it fabricates the failure response client-side rather than exercising the
   app's real `render_error` headers -- and the shipped documentation now states the disproved
   claim as fact.

Two further warning-severity defects compound success criterion 1's failure without introducing a
new one: `CheckResult.skipped` is produced but never rendered by either surface (a busy/skipped
scanner check shows a false green "OK"), and `CheckRefresher._run` has no per-tick exception
backstop, so a single raise from the cache store permanently silences the background auto-refresh
for the rest of the process's life.

Success criteria 3, 4, and 5 -- page counts and the queue wait line, plain-language errors with a
collapsed disclosure, and the owner-only flip prompt with human profile labels -- were
independently verified against the current source (not the SUMMARYs) and hold with no defects
found by either round-3 review or this pass.

This phase should not be closed. The prior `passed` verification is superseded; it did not catch
either blocker because it verified round-1/round-2 gap-closure diffs against their own tests rather
than tracing the actual runtime interaction (htmx header timing) or exercising a non-ASCII input
the existing test suite never tried.

---

*Verified: 2026-09-17T12:00:00Z*
*Verifier: Claude (gsd-verifier)*
