---
phase: 30-appliance-layer
verified: 2026-09-18T03:44:26Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 3/5
  gaps_closed:
    - "R3-CR-01: `_saned_hosts` no longer raises on a Unicode decimal-digit port. The port branch (checks.py:989-995) now tests `set(maybe_port) <= _ASCII_DIGITS` instead of `maybe_port.isdigit()`. Reproduced directly: `_saned_hosts('host:\\u00b2')` returns `(('host', 6566),)` (falls through to the bare-hostname filter, matching the plan's own decision record) with no exception, for both a superscript-two port and an Arabic-Indic-digit port. `uv run pytest tests/test_checks.py -k 'Unicode or unicode' -q` -> 6 passed."
    - "R3-CR-02: the checks poll's own error path now ends itself. `render_error` (web/errors.py:212-214) exempts a request whose `HX-Target` is `CHECKS_POLL_TARGET_ID` (`checks-body`) from `HX-Retarget`/`HX-Reswap`, so a genuine server error to `GET /api/checks` replaces `#checks-body` (removing its `every 2s` trigger) instead of landing in `#status-message`. `uv run pytest tests/test_web_errors.py -k ChecksPollTarget -q` -> 19 passed. `tests/test_browser.py::TestPollEndsOnAnErrorResponse` was independently read: it now drives the failure through `route.continue_` with a tampered non-integer `attempt` query parameter, letting the real 422 `render_error` produces reach the browser with its real headers, rather than fabricating a response client-side. `uv run pytest tests/test_browser.py -k TestPollEndsOnAnErrorResponse -q` -> 4 passed (all four Chromium cases, not the round-3-era 1-of-4)."
    - "R3-WR-03: `CheckResult.skipped` is now read by both surfaces. `check_row_class`/`check_row_glyph`/`check_row_label` (checks.py:556, 579, 602) each check `result.skipped` first and return the neutral cold-start trio; `cli.py:1081` does the same for `saneless doctor`'s row marker. `uv run pytest tests/test_checks.py -k skipped -q` -> 16 passed; `test_doctor.py -k skipped` -> 10 passed."
    - "R3-WR-02: `CheckRefresher._run` (refresher.py:217-226) now wraps `self._tick()` in `try/except Exception`, logging and continuing rather than letting the thread die; `_probe_and_store` (refresher.py:336-343) moved `self._cache.store(results)` inside the `try` block so a raising store is caught by the same handler. `uv run pytest tests/test_refresher.py -k raising -q` -> 4 passed."
  gaps_remaining: []
  regressions: []
gaps: []
deferred: []
human_verification: []
new_findings_since_previous_round:
  - id: R4-WR-01
    severity: warning
    summary: >
      render_error's HX-Target exemption (web/errors.py:212) is keyed on the
      literal header value "checks-body" alone, with no method/path scoping.
      The strip's own "Check again" button (checks.html:104) POSTs to
      /api/checks/refresh with the same HX-Target header, so it is exempted
      identically -- but refresh_checks (routes.py:1528-1537) has no
      try/except around _checks_context, unlike its sibling get_checks
      (routes.py:1433-1438). Independently reproduced with a raising
      _checks_context: POST /api/checks/refresh returns 500 with no
      HX-Retarget/HX-Reswap headers, and the body is partials/error.html,
      which carries no id -- so the button's own hx-swap="outerHTML" replaces
      #checks-body with the error partial, deleting the strip and the only
      affordance that could bring it back, for the life of the tab.
    falsifies_success_criterion: false
    reasoning: >
      This requires _checks_context itself to raise (e.g. from
      worker.current_job_id, refresher.probe_in_flight, or local_time) --
      an already-broken internal invariant, not a normal button click or a
      normal check failure. It is narrower than R3-CR-02 (which fired on any
      4xx/5xx from the much more permissive GET route) and does not recur
      every 2s or overwrite an active scan's progress line, so it does not
      reproduce the specific harm success criteria 1 ("refreshed ... by a
      button") or 2 (recurring interference with a scan) describe. A full
      page reload recovers the strip. Treated as real follow-up debt for a
      future gap-closure round, not a phase blocker -- consistent with
      30-REVIEW.md round 4's own classification (warning, not critical).
  - id: R4-WR-02
    severity: warning
    summary: >
      docs/reference/web-api.md:136 still states "An attempt counter the
      server cannot read as an in-range number ... come back as the strip
      itself with no poll attached ... nothing asks again", worded to cover
      both directions of the clamp. Confirmed false for the below-range half:
      routes.py:1432 clamps a negative attempt to 0, which is the value a
      page load itself uses, so poll_attempt becomes 1 and the template's
      own hx-trigger emits `load, every 2s` -- a fresh chain starts rather
      than ending. test_a_negative_attempt_is_the_start_of_a_chain pins the
      behaviour the docs deny.
    falsifies_success_criterion: false
    reasoning: >
      Documentation-accuracy defect only; the input (a negative attempt) is
      never sent by the real page and only reachable by hand-crafting the
      query string, so no user-facing behavior is affected. Real doc debt,
      not a behavioral gap.
  - id: R4-WR-03
    severity: warning
    summary: >
      _segment_is_a_numeric_address_shorthand treats 0.0.0.0 as a legal
      dotted-quad and lets it through; the guard's own docstring names
      0.0.0.0 as "the worst of them" because a connect() to it reaches
      loopback on Linux. Independently reproduced:
      _saned_hosts('scanbox:0.0.0.0') == (('scanbox', 6566), ('0.0.0.0', 6566)).
    falsifies_success_criterion: false
    reasoning: >
      30-31's must_haves explicitly scoped R3-WR-01's closure to the
      numeric-shorthand class (0.0, 0x0.0, 0x7f.1, 127.1, 6566.0) and did not
      list 0.0.0.0 among its test cases; this is a residual the round-4
      review discovered as a follow-on to that fix, not a promise this round
      broke. Reaching it requires an operator to type the literal unspecified
      address into scanner.host or SANE_NET_HOSTS. Real follow-up debt, not
      a phase blocker.
---

# Phase 30: Appliance Layer Verification Report

**Phase Goal:** A non-technical household member can tell at a glance whether the appliance is
healthy and what a failure means — one shared check list behind both `saneless doctor` and a
cached status strip, page counts on every terminal job, plain-language errors with a next step,
human profile labels, queue position, and an owner-only flip prompt — with help text and the docs
for each new surface written in-phase.

**Verified:** 2026-09-18T03:44:26Z
**Status:** passed
**Re-verification:** Yes — superseding the prior (2026-09-17T12:00:00Z) `gaps_found` verification
(3/5), which was written after round-3 code review found `R3-CR-01` and `R3-CR-02` still open at
that HEAD. Gap-closure round 3 (plans 30-31..30-36) and the round-4 code review that followed it
are both now on the branch this verification reads.

## Method

I did not trust `SUMMARY.md` mutation-evidence claims or `30-REVIEW.md`'s round-4 closure verdicts
at face value. For each of the two prior blockers and the two compounding warnings I independently:

1. Read the current source at HEAD (`checks.py`, `web/errors.py`, `web/refresher.py`,
   `web/checks_cache.py`, `web/routes.py`, `web/templates/partials/checks.html`, `cli.py`).
2. Ran a live Python reproduction of the exact failing input the prior verification used
   (`_saned_hosts` against a superscript-two port and an Arabic-Indic-digit port; `0.0.0.0`
   segments) rather than reading the claim.
3. Ran the specific pytest selections the task named
   (`tests/test_checks.py -k "Unicode or unicode"`, `tests/test_web_errors.py -k ChecksPollTarget`)
   plus adjacent selections (`test_checks.py -k skipped`, `test_doctor.py -k skipped`,
   `test_refresher.py -k raising`, `test_checks_cache.py -k "stale or compare or release"`,
   `test_browser.py -k TestPollEndsOnAnErrorResponse`, `test_browser.py -k owner_is_offered_the_flip`)
   and read every one green rather than counting an orchestrator-reported total.
4. Read `tests/test_browser.py`'s `TestPollEndsOnAnErrorResponse` class body directly to confirm it
   now drives the failure server-side (`route.continue_` + a tampered non-integer `attempt`) rather
   than fabricating a client-side response, since that exact test-quality defect is what let
   `R3-CR-02` through the prior `passed` verification.
5. Independently reproduced `R4-WR-01` (the round-4 review's own closest-to-critical finding) with
   a standalone `TestClient` script against a monkeypatched `_checks_context`, rather than trusting
   the review's own transcript, and judged its severity myself against the five roadmap success
   criteria's actual wording.
6. Re-confirmed criteria 3, 4, and 5 at the artifact/wiring level (not re-derived from scratch,
   since round 3/4 did not touch their supporting files except incidentally through shared files
   like `routes.py` and `checks.py`).

I did not repeat the full test suite / lint / type-check run — the orchestrator already ran it at
this HEAD (3175 passed, ruff/format/ty/pyrefly clean, `prek` pre-push clean) and that is orthogonal
to whether the two blockers are actually closed, which only a targeted reproduction can show.

## Goal Achievement

### Observable Truths (Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `saneless doctor` runs the shared checks and exits non-zero on a red check, never on a healthy appliance; the index page shows the same checks, refreshed on load and by a button | ✓ VERIFIED | **R3-CR-01 closed**: `_saned_hosts`'s port branch (checks.py:992) now tests `set(maybe_port) <= _ASCII_DIGITS`; reproduced live -- `_saned_hosts('host:²')` and `_saned_hosts('host:٢٣')` both return `(('host', 6566),)` with no exception (falls through to the bare-hostname filter per the plan's decision record, rather than raising or silently deriving a bogus port). `pytest tests/test_checks.py -k "Unicode or unicode"` -> 6 passed. **R3-WR-03 closed**: `check_row_class`/`check_row_glyph`/`check_row_label` (checks.py:556-602) and `cli.py:1081` all branch on `result.skipped` first, rendering the neutral cold-start trio rather than a false green OK; `pytest -k skipped` across `test_checks.py`/`test_doctor.py` -> 26 passed. |
| 2 | The status strip stays fast with the scanner host unplugged and is skipped entirely while a scan is active, so it never contends with the exclusive scanner | ✓ VERIFIED | **R3-CR-02 closed**: `render_error` (web/errors.py:212-214) exempts a request whose `HX-Target` equals `CHECKS_POLL_TARGET_ID` ("checks-body") from `HX-Retarget`/`HX-Reswap`; `GET /api/checks`'s own failure therefore replaces `#checks-body` (killing its `every 2s` trigger) instead of overwriting `#status-message` every 2s during an active scan. `pytest tests/test_web_errors.py -k ChecksPollTarget` -> 19 passed. `TestPollEndsOnAnErrorResponse` was read directly and now drives the failure through `route.continue_` with a tampered non-integer `attempt`, reaching the app's real 422 and real headers -- not a client-side fabrication; `pytest tests/test_browser.py -k TestPollEndsOnAnErrorResponse` -> 4/4 passed (Chromium). **R3-WR-02 closed**: `CheckRefresher._run` (refresher.py:217-226) now has a per-tick `try/except Exception`, and `_probe_and_store`'s `self._cache.store(results)` (refresher.py:341) moved inside the guarded `try` block; `pytest tests/test_refresher.py -k raising` -> 4 passed. The gate-contention half (unaffected by round 3) remains sound: `run_checks(scanner_gate=...)` only wraps `_scanner_result`, and `skip_scanner` is still derived from `worker.current_job_id`. |
| 3 | Every terminal job shows pages scanned/removed/uploaded; manual duplex shows front/back counts during pass B; a queued job shows the wait line | ✓ VERIFIED (regression-checked) | `vocabulary.page_counts()`/`busy_line()` unchanged by round 3/4; still wired into `partials/status.html:19,67,77`, `partials/history.html:20`, and `routes.py:_busy_line` (line 619-658). No defect found. |
| 4 | Every user-facing error shows a plain-language message and a suggested next step, with raw technical detail inside a collapsed disclosure | ✓ VERIFIED (regression-checked) | `partials/status.html:108-115` still renders `job.error_category \| error_next_step` followed by a native `<details class="tech-details">` disclosure. Unaffected by either blocker. |
| 5 | Two browser contexts show the owner Continue/Abort (with Abort confirm) and the non-owner "Waiting for the stack to be flipped"; profile dropdowns show human labels/descriptions, feeder-first on sheet-fed scanners, and note a read-only config mount on the strip | ✓ VERIFIED (regression-checked, re-ran) | `pytest tests/test_browser.py -k owner_is_offered_the_flip` -> 1 passed against real two-context Chromium automation. `flip.html:60` still carries `hx-confirm` on Abort only. `routes.py:893` still sorts `not entry[1].uses_feeder`. `checks.py:1566` still renders the `IN_MEMORY_UNWRITABLE` read-only-mount message. |

**Score:** 5/5 truths verified.

### New Findings From Round-4 Review (Not Blocking, Tracked as Follow-Up)

Round-4 code review (`30-REVIEW.md`, 0 critical / 3 warning / 3 info) independently mutation-tested
all 12 round-3 closures and confirmed them closed; I independently reproduced the two former
criticals (above) rather than accepting that verdict at face value. Round 4's own new findings
(`R4-WR-01..03`, `R4-IN-01..03`) were read and, for the closest one to a criterion violation,
independently reproduced:

- **R4-WR-01** (reproduced independently with a monkeypatched `_checks_context` and a raw
  `TestClient`): `POST /api/checks/refresh` with a raising `_checks_context` returns 500 with
  **no** `HX-Retarget`/`HX-Reswap` headers (confirmed empty via `r.headers` inspection) because the
  request's `HX-Target: checks-body` matches the same exemption the polling `GET` route earns, but
  `refresh_checks` (routes.py:1528-1537) has no `try/except` fallback the way `get_checks` does.
  The button's own `hx-swap="outerHTML"` then replaces `#checks-body` with the id-less error
  partial, deleting the strip and its own recovery button for the life of the tab. **Judged not to
  falsify success criterion 1 or 2**: it requires `_checks_context` itself to raise (an
  already-broken internal invariant, not a normal check failure or a normal button click), it does
  not recur every 2s, and it does not overwrite an active scan's progress line — the specific harms
  the two criteria name. A full page reload recovers the strip. Real, unresolved, and should be
  closed in the next gap-closure round (`errors.py`'s exemption needs to be scoped to
  `GET /api/checks` specifically, and `refresh_checks` needs the same `_checks_fallback_context`
  guard `get_checks` has), but it is not a phase blocker.
- **R4-WR-02**: `docs/reference/web-api.md:136` still asserts a below-range `attempt` "come[s] back
  ... with no poll attached ... nothing asks again"; confirmed false — `routes.py:1432` clamps a
  negative attempt to 0, which is the value that starts a fresh `load, every 2s` chain, and
  `test_a_negative_attempt_is_the_start_of_a_chain` pins exactly that. Documentation-accuracy debt
  only; the input is never sent by the real page.
- **R4-WR-03**: `_segment_is_a_numeric_address_shorthand` still accepts the literal `0.0.0.0` as a
  legal dotted quad even though the same function's docstring names it "the worst of them" (a
  Linux `connect()` to it reaches loopback). Confirmed:
  `_saned_hosts('scanbox:0.0.0.0') == (('scanbox', 6566), ('0.0.0.0', 6566))`. Out of scope for
  30-31's stated must-haves (which named `0.0`, `0x0.0`, `0x7f.1`, `127.1`, `6566.0`, not
  `0.0.0.0`); a residual for the next round, reachable only by an operator typing the unspecified
  address into `scanner.host` or `SANE_NET_HOSTS`.

None of these three findings independently falsifies a ROADMAP success criterion under the
reasoning above; all three are carried forward as follow-up debt (see
`new_findings_since_previous_round` in the frontmatter) rather than structured as blocking gaps.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/checks.py` | Shared check registry, `_saned_hosts` rejecting a Unicode-digit port without raising | ✓ VERIFIED | `_saned_hosts` (line 992) tests ASCII-decimal only; reproduced no-raise on two Unicode-digit inputs |
| `src/saneless/web/errors.py` | `render_error` exempting the checks-poll's own error path | ✓ VERIFIED | `CHECKS_POLL_TARGET_ID` exemption present and test-covered (19 passing cases) |
| `src/saneless/web/templates/partials/checks.html` | `check_row` rendering `CheckResult.skipped` as a neutral row | ✓ VERIFIED | Macro reads the skipped-aware filters; 16+10 passing tests |
| `src/saneless/web/refresher.py` | `_run` with a per-tick exception backstop | ✓ VERIFIED | `try/except Exception` around `self._tick()`; `store` moved inside the guarded region |
| `src/saneless/web/checks_cache.py` | Compare-and-clear `release_manual_claim` | ✓ VERIFIED | `if self._last_manual_claim != stamp: return False` (line 299) |
| `src/saneless/vocabulary.py` | `page_counts`, `busy_line`, `error_advice`, `local_time` | ✓ VERIFIED (unchanged) | Confirmed still wired |
| `src/saneless/auto_profiles.py` | `_profile_label`/`_profile_description` | ✓ VERIFIED (unchanged) | Confirmed still wired |
| `src/saneless/web/routes.py` | `_profile_options` feeder-first sort, `_busy_line`, `_checks_context` guard asymmetry | ⚠️ PARTIAL | `get_checks` guards `_checks_context` with a fallback (line 1433-1438); `refresh_checks` does not (line 1536) — R4-WR-01, tracked as follow-up, not a phase blocker |
| `tests/test_browser.py::TestPollEndsOnAnErrorResponse` | A test proving the checks poll ends on a real server-produced failure | ✓ VERIFIED | Now drives the failure via `route.continue_` + tampered `attempt`; asserts real response headers; 4/4 passed |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `checks.py::_saned_hosts` | `checks.py::_scanner_preflight` | entries walked by the pre-probe | ✓ WIRED | No raise reaches `run_checks`'s generic handler for a Unicode-digit port |
| `web/routes.py::get_checks` | `web/errors.py::render_error` | `HX-Target: checks-body` exemption | ✓ WIRED | Poll's own failure replaces `#checks-body`, ending the trigger |
| `web/routes.py::refresh_checks` | `web/routes.py::_checks_context` | direct call, no guard | ⚠️ PARTIAL | Same exemption applies to this route's requests but there is no fallback context on a raise (R4-WR-01) |
| `web/templates/partials/checks.html::check_row` | `checks.py::CheckResult.skipped` | `check_row_class`/`check_row_glyph`/`check_row_label` filters | ✓ WIRED | Confirmed present and test-covered |
| `web/refresher.py::_run` | exception handling | per-tick `try/except` | ✓ WIRED | Confirmed; matches `ScanWorker._run`'s guarded-loop precedent |
| `web/routes.py::refresh_checks` | `web/checks_cache.py::release_manual_claim` | stamp carried from grant to release | ✓ WIRED | Compare-and-clear confirmed; a stale stamp changes nothing |
| `vocabulary.py::page_counts/busy_line` | `partials/status.html`, `partials/history.html` | Jinja filter calls | ✓ WIRED | Unchanged, re-confirmed |
| `auto_profiles.py::_profile_label/_profile_description` | config generation, web dropdown | direct call | ✓ WIRED | Unchanged, re-confirmed |
| `routes.py::_profile_options` | `index.html` `<select>` | feeder-first sort | ✓ WIRED | Unchanged, re-confirmed |
| `tests/test_browser.py` two-context test | Owner/non-owner flip prompt | real `browser.new_context()` cookie jars | ✓ WIRED | Re-ran; 1 passed |

### Requirements Coverage

| Requirement | Claimed by plans | Status | Evidence |
|---|---|---|---|
| APPL-01 | 30-06, 30-08, 30-11, 30-20, 30-21, 30-25, 30-28, 30-30, 30-31, 30-34 | ✓ SATISFIED | `doctor` runs the shared registry and no longer exits non-zero on a healthy scanner given a Unicode-digit port; skipped rows render honestly |
| APPL-02 | 30-04, 30-06, 30-07, 30-09, 30-11, 30-17, 30-20, 30-21, 30-24, 30-25, 30-26, 30-27, 30-28, 30-29, 30-30, 30-31, 30-32, 30-33, 30-34, 30-35, 30-36 | ✓ SATISFIED | Strip shows the same checks, refreshes on load/button, and the poll now self-terminates on a genuine failure; refresh-button's own unguarded failure path (R4-WR-01) tracked as follow-up, not blocking |
| APPL-03 | 30-01, 30-04, 30-09, 30-12, 30-17, 30-22 | ✓ SATISFIED | Unchanged, re-confirmed |
| APPL-04 | 30-01, 30-09, 30-10, 30-12, 30-17, 30-23 | ✓ SATISFIED | Unchanged, re-confirmed |
| APPL-05 | 30-02, 30-05, 30-15, 30-17, 30-18 | ✓ SATISFIED | Unchanged, re-confirmed |
| APPL-06 | 30-04, 30-06, 30-11, 30-20 | ✓ SATISFIED | Unchanged, re-confirmed |
| APPL-07 | 30-01, 30-02, 30-06, 30-08, 30-10, 30-14, 30-18, 30-19 | ✓ SATISFIED | Unchanged, re-confirmed |
| APPL-08 | 30-01, 30-03, 30-13 | ✓ SATISFIED | Unchanged, re-confirmed |
| APPL-09 | 30-03, 30-13, 30-19 | ✓ SATISFIED | Unchanged, re-confirmed |
| APPL-10 | 30-02, 30-16, 30-18, 30-19, 30-23 | ✓ SATISFIED (artifact presence, unaffected by this round) | |
| APPL-11 | 30-06, 30-11, 30-18 | ✓ SATISFIED (artifact presence; note doc-accuracy debt R4-WR-02 in `docs/reference/web-api.md`, unrelated surface) | |
| APPL-12 | 30-01, 30-09, 30-10, 30-12, 30-17, 30-18, 30-22 | ✓ SATISFIED | Unchanged, re-confirmed |

No orphaned requirements: all 12 APPL-* IDs are claimed by at least one plan's frontmatter
(including the six round-3 gap-closure plans) and have corresponding source evidence.

**Documentation-hygiene item, unchanged from the prior two verifications:** `.planning/REQUIREMENTS.md`
lines 129-140 and 316-327 still show `[ ]` / "Pending" for every APPL-* ID. This is a tracking
bookkeeping gap in a planning artifact, not evidence the requirements are unmet in the code — all
12 are now SATISFIED per this verification.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/saneless/web/errors.py` | 212 | `HX-Target` exemption matches any request to that target, not only the poll's `GET` | ⚠️ WARNING | A failing `POST /api/checks/refresh` deletes the strip instead of landing in `#status-message` (R4-WR-01, follow-up) |
| `src/saneless/web/routes.py` | 1528-1537 | `refresh_checks` has no fallback-context guard, unlike its sibling `get_checks` | ⚠️ WARNING | Same defect class as above; the asymmetry is the root cause |
| `docs/reference/web-api.md` | 136 | States a below-range `attempt` "ends" the poll; the code restarts a fresh chain for it | ⚠️ WARNING | Doc-accuracy only, no user-facing impact (R4-WR-02, follow-up) |
| `src/saneless/checks.py` | 715-759 | `_segment_is_a_numeric_address_shorthand` accepts the literal `0.0.0.0`, contradicting its own docstring | ⚠️ WARNING | An operator-typed `0.0.0.0` can put loopback in the dial list (R4-WR-03, follow-up) |

No `TBD`/`FIXME`/`XXX` markers found in any file read during this verification.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Unicode-digit port no longer crashes the host parser | `uv run python -c "from saneless.checks import _saned_hosts; print(_saned_hosts('host:²'))"` | `(('host', 6566),)`, no exception | ✓ PASS |
| Arabic-Indic-digit port no longer crashes the host parser | same, with `٢٣` | `(('host', 6566),)`, no exception | ✓ PASS |
| `_saned_hosts` Unicode-digit test suite | `pytest tests/test_checks.py -k "Unicode or unicode" -q` | 6 passed | ✓ PASS |
| Checks-poll self-termination test suite | `pytest tests/test_web_errors.py -k ChecksPollTarget -q` | 19 passed | ✓ PASS |
| Real browser poll-ends-on-error test | `pytest tests/test_browser.py -k TestPollEndsOnAnErrorResponse -q` | 4 passed (Chromium) | ✓ PASS |
| Skipped-row rendering | `pytest tests/test_checks.py -k skipped -q` / `tests/test_doctor.py -k skipped -q` | 16 passed / 10 passed | ✓ PASS |
| Refresher survives a raising tick/store | `pytest tests/test_refresher.py -k raising -q` | 4 passed | ✓ PASS |
| Compare-and-clear manual claim | `pytest tests/test_checks_cache.py -k "stale or compare or release" -q` | 10 passed | ✓ PASS |
| Owner/non-owner flip prompt (real two-context Chromium) | `pytest tests/test_browser.py -k owner_is_offered_the_flip -q` | 1 passed | ✓ PASS |
| R4-WR-01 reproduction: failing `POST /api/checks/refresh` | standalone `TestClient` script with `_checks_context` monkeypatched to raise | `500`, headers `{}` (no `HX-Retarget`), body is the id-less error partial | ✗ CONFIRMS R4-WR-01 (tracked as follow-up, not a criterion failure) |
| R4-WR-03 reproduction: `0.0.0.0` accepted as a dial-list entry | `_saned_hosts('scanbox:0.0.0.0')` | `(('scanbox', 6566), ('0.0.0.0', 6566))` | ✗ CONFIRMS R4-WR-03 (tracked as follow-up, not a criterion failure) |

### Probe Execution

No `scripts/*/tests/probe-*.sh` convention exists for this project and none was declared in any of
the 36 plans or summaries for this phase. Step 7c: SKIPPED — no probes declared or discovered.

### Human Verification Required

None. Per project CLAUDE.md, browser behavior is verified with Playwright automation, never marked
human-only. Success criterion 5's owner/non-owner flip prompt is fully automated in
`tests/test_browser.py` using real two-context Chromium, independently re-run above, not merely
trusted from a SUMMARY. No item in this phase requires physical hardware or an unstubbable external
service.

### Gaps Summary

Both blockers the prior verification (2026-09-17T12:00:00Z) found — `R3-CR-01` (a Unicode-digit
port crashing `_saned_hosts` and reddening the Scanner row) and `R3-CR-02` (the checks poll's error
path unable to end itself, overwriting an active scan's progress line every 2s) — are confirmed
closed at this HEAD, independently, through live reproduction of the exact failing inputs and
targeted test runs, not by reading `SUMMARY.md` or `30-REVIEW.md` claims. The two compounding
warnings from that verification (`R3-WR-03` skipped-row rendering, `R3-WR-02` refresher exception
backstop) are likewise confirmed closed.

Round-4 code review, which ran after round-3 gap closure, found three new warning-severity issues
(`R4-WR-01..03`) and three informational ones. I independently reproduced the two most consequential
(`R4-WR-01`, the checks-refresh button's own unguarded failure path deleting the strip; `R4-WR-03`,
the `0.0.0.0` dial-list residual) and judged neither falsifies a ROADMAP success criterion: both
require an atypical trigger (an already-broken internal invariant, or an operator typing the
unspecified address into a host setting) that the criteria's plain language does not describe, and
neither reproduces the specific recurring-interference or false-negative-exit harms the criteria
name. They are carried forward as tracked follow-up debt for a future gap-closure round rather than
structured as blocking gaps.

All five ROADMAP success criteria for Phase 30 hold at this HEAD. This phase is ready to close.

---

*Verified: 2026-09-18T03:44:26Z*
*Verifier: Claude (gsd-verifier)*
