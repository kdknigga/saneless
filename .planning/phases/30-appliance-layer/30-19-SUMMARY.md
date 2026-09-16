---
phase: 30-appliance-layer
plan: 19
subsystem: web
tags: [playwright, owner-gate, cookies, hx-confirm, touch-targets, show-tags, d-15, responsive]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "plan 30-17's _make_gate, _serve, _make_paperless_deliver and _BrowserTestScanner"
  - phase: 30-appliance-layer
    provides: "plan 30-13's owner gate, flip.html hx-confirm and the AWAITING_FLIP three-way branch"
  - phase: 30-appliance-layer
    provides: "plan 30-14's scan_blocked flag, reason line and TOKEN_UNSET route guard"
  - phase: 30-appliance-layer
    provides: "plan 30-16's tag picker, tag-filter-form and TestTagFilterInChromium"
  - phase: 30-appliance-layer
    provides: "plan 30-11's status strip, Check again button and .check-row flex layout"
provides:
  - "the two-context owner proof (P9) -- success criterion 5, answered with two real cookie jars"
  - "the Abort confirmation round trip (P10), dismissed and accepted"
  - "the simpler form (P14): show_tags=false plus the profile's default_tags on the job"
  - "the blocked button through a run of status polls (P16) and the guard behind it (P17)"
  - "the three browser items 30-17 routed forward: .page-counts colour, Check again clicked, the strip at 320 px"
  - "flip_server / simple_form_server / private_blocked_server fixtures, _blocked_settings, _drive_to_flip_prompt"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "two hand-made browser contexts sharing ONE blocked list, so a single teardown assertion speaks for both"
    - "a second cookie jar as the honest sample for an ownership claim, rather than a foreign token written onto a row"
    - "a data-witness attribute used in the negative direction: the witness must be GONE, which is what proves an outerHTML swap"
    - "recorded-request lists instead of timeouts for 'nothing was sent' claims"
    - "retiring a parked job mid-test so the final out-of-band render is made with nothing active"

key-files:
  created: []
  modified:
    - tests/test_browser.py

key-decisions:
  - "P9 reloads the owner page before asserting, so the prompt is a decision taken on the presented cookie rather than a leftover of the response that minted it -- and so both pages have a Job History to be compared on"
  - "'both pages show the same state line' is asserted on the Job History row, not the status area: at AWAITING_FLIP the two status areas differ BY DESIGN, and that difference is the thing under test"
  - "P10 anchors its 'no abort was sent' claim on a completed status poll, so it says 'a whole round trip's worth of chance' rather than 'not in the first second'"
  - "P16's 'several status polls' is staged with a parked job and then the job is RETIRED mid-test, so the last out-of-band button render is made with nothing active and the blocked flag is the only thing still holding the button shut"
  - "P17 asserts the rejected row's error through the job store, because history.html renders a state label and never the sentence -- the sentence is what the CLI and the API surface"
  - "P11/P12/P13 were already covered by plan 30-16 and are not duplicated; the plan predates that landing"
  - "the 320 px wrap assertion counts line boxes of the message TEXT NODE, excluding .check-next, which is display:block and would otherwise read as a wrap on every row that carries a next step"

patterns-established:
  - "Pattern: an ownership or isolation claim gets as many browser contexts as it has parties; one context with staged server state proves the branch, not the mechanism"
  - "Pattern: a witness attribute is read in whichever direction the swap contract runs -- survives for innerHTML, vanishes for outerHTML"
  - "Pattern: when a flag has two disabling sources, remove the other one mid-test rather than reasoning about which one is doing the work"

requirements-completed: [APPL-07, APPL-09, APPL-10]

# Metrics
duration: 58min
completed: 2026-09-16
---

# Phase 30 Plan 19: Two Browsers, One Owner, and the Guard Behind the Button Summary

**Success criterion 5 is now answered with two real browsers and two real cookie jars rather than a foreign token written onto a job row; the Abort confirmation, the simpler form, the blocked button through a run of polls and the tampered-DOM refusal are all asserted in Chromium; and the last three browser items the phase had routed forward are closed.**

## Performance

- **Duration:** 58 min
- **Tasks:** 4 (4 commits — `type: execute`, no RED gate)
- **Files created:** 0 · **Files modified:** 1
- **Tests:** 114 → 127 browser tests; full suite 2888 passed

## Accomplishments

- **P9 — two contexts, two cookie jars, one owner.** `TestTwoBrowsersOneStack` builds `owner_ctx` and `viewer_ctx` with `browser.new_context()`, installs `_make_gate(blocked, egress_allowlist)` on **both** on the two lines immediately after, and shares one `blocked` list asserted empty in the `finally` that closes them. This is the shape Pitfall 9 and 30-17's `_make_gate` note were written for: a hand-made context inherits none of the overridden `context` fixture's routing, so without the explicit installation these would have been the only two pages in the module able to reach the real internet in CI, and nothing would have failed. The owner submits a real manual-duplex scan, both pages are then loaded from the same server state, and: the owner sees `Continue` and `Abort scan` and exactly two `[hx-post^='/api/flip/']` controls; the viewer reads `Waiting for the stack to be flipped` and has **zero**; `owner_ctx.cookies()` holds one `saneless_owner` with `httpOnly` true, `sameSite` `Lax` and `expires == -1`; `viewer_ctx.cookies()` holds none. That last pair is research assumption A6 measured rather than assumed — a browser really does keep the `Set-Cookie` an htmx XHR returned and really does send it back on the next request.
- **P9's D-26 absence guard.** The owner's prompt renders exactly two controls, and neither page's `body` text matches `override|take over|force`. The absence is the rendering, so it is asserted rather than left to a reviewer.
- **P10 — Abort asks first, and a "no" means nothing happened.** `hx-confirm` is one template attribute; a template test can only see that it is there. Here Chromium raises the dialog, its message is asserted **equal** to `Abort this scan? It will stop and cannot be resumed.`, dismissing it sends no `POST /api/flip/abort` (read off a recorded request list, anchored on a completed status poll so the claim is not "none in the first second"), the job is still `AWAITING_FLIP`, and the second click with an accept takes the job to `CANCELLED` through `Aborting scan...`.
- **P14 — the simpler form, and the tags it still applies.** A private server with `[web] show_tags = false`: `#tags-list`, `#tag-filter`, `#tag-filter-form`, `label.tag-option`, the tags refresh button and `#tags-help` are all **count 0** — absent, never hidden, which is the whole of D-28 (what is not in the markup cannot be re-shown from devtools, read by a screen reader or tabbed into). A real scan from that form then lands the profile's `default_tags` on the job row, which is D-29: the submit carried no `tags` field at all, so the ids on the row can only have come from the profile.
- **P16 — the courtesy survives the poll, and then stands alone.** A parked active job makes `#status-area` poll every second; three completed responses later the button is still `disabled` and still carries `aria-describedby="scan-blocked-reason"`, read **without** retrying so a trap that sprung and healed on the next tick cannot hide. Then the job is retired mid-test: the next out-of-band render is made with nothing active, so what is left holding the button shut is the blocked flag alone — and it is, with the text back to exactly `Scan` and the reason line still visible. `test_a_configured_appliance_shows_no_reason_line` also now asserts the button is **enabled**, so the blocked case is a difference rather than only a presence.
- **P17 — the button is the courtesy; the guard is the refusal (D-15).** `removeAttribute('disabled')` through `page.evaluate`, then a click. The route answers 503, the slot carries UI-SPEC S8's sentence in full, `#status-area` still reads `Ready to scan.` with no `[aria-busy]` anywhere, and a `Failed` row in `status-error` appears in Job History whose job carries `Not started: the paperless-ngx API token has not been set` and `ErrorCategory.REJECTED`.
- **The three routed items are closed (see below).** `.page-counts` joined the status-colour parametrisation; `Check again` is finally *clicked* in a browser; and the strip is measured at 320 px for the first time in this module's life.
- **No sleep added.** Every wait is Playwright auto-waiting, a completed-response anchor, or the conftest `wait_for_state`. `time.sleep` across `tests/` stands at **17**, unchanged.
- **Nothing is deferred to a person.** The module-level note 30-17 added already covers the classes below it, which is where all four new classes live. `grep -riE "manual.only|needs human|human verification" tests/test_browser.py` matches zero times.

## Task Commits

1. **Task 1: two browsers, one owner, and an Abort that asks first (P9, P10)** — `6c7e750`
   - `flip_server`, `_drive_to_flip_prompt`, `_history_row_text`
   - `_FLIP_CONTROL_SELECTOR`, `_WAITING_LINE`, `_ABORT_CONFIRMATION`, `_NO_THIRD_WAY_OUT`
   - `TestTwoBrowsersOneStack` — 2 tests
2. **Task 2: the simpler form, and the tags it still applies (P14)** — `22f6670`
   - `_simple_form_settings`, `simple_form_server`, `_TAG_MARKUP_SELECTORS`
   - `TestSimplerFormInChromium` — 1 test
3. **Task 3: the blocked button through the poll, and the guard behind it (P16, P17)** — `0e31ecd`
   - `_blocked_settings` extracted; `private_blocked_server`
   - `TestBlockedButtonThroughTheStatusPoll`, `TestTheGuardBehindTheBlockedButton` — 2 tests
   - one assertion added to `test_a_configured_appliance_shows_no_reason_line`
4. **Task 4: the three items 30-17 routed forward** — `25a5d2a`
   - `.page-counts` added to `test_status_colour_meets_aa_contrast` (+6 cases)
   - `test_check_again_replaces_the_body_it_is_aimed_at`
   - `test_the_strip_fits_a_320px_phone_without_a_sideways_scrollbar`
   - `_COUNT_CHECK_MESSAGE_LINES`

## The debt 30-17 routed to this plan

All three were named in 30-17's "what this plan did NOT cover" table, and none of them was in **this** plan's own task list. They are covered here because this is the last plan of the phase and there was nowhere else for them to go.

| Item | Status |
|------|--------|
| `.page-counts` colour parametrisation | **Closed.** It now runs in all three placements in both schemes, and its value is pinned to the same `_MUTED` constant `.status-cancelled` uses — one constant for one custom property, so a drift in `--pico-muted-color` must be reported by both or by neither. |
| The `Check again` round trip clicked in Chromium | **Closed.** The strip is probed first so the body arrives trigger-less and the button is the only remaining swap source; a `data-witness` attribute set from the test must be **gone** afterwards, which is the assertion an `innerHTML` swap would fail and every other assertion in the test would tolerate. The replacement is also asserted to carry its own button, because a swap that dropped it would look fine exactly once. This is P8's claim run in reverse: that slot must survive its swap, this body must not. |
| Strip layout at 320 px | **Closed.** No sideways scroll (`scrollWidth - clientWidth <= 0`), every `.check-row` inside the viewport, one x and one width for all five `.check-name` columns (a layout that "fitted" by letting the name column collapse per row would clear the overflow check and be unreadable), the refresh button still ≥ 44 px and inside the fold, and at least one message occupying more than one line box. **Measured during development:** line counts are `[1, 1, 3, 3, 1]` at 320 px and `[1, 1, 1, 1, 1]` at 1280 px, so the wrap assertion discriminates rather than passing at any width. |

## Deviations from Plan

### Interpretations, not deviations

**P11, P12 and P13 already existed and were not duplicated.** Plan 30-19 was written before 30-16 landed. `TestTagFilterInChromium` (30-16) already covers all three: every `label.tag-option` measured through `bounding_box()` against the 44 px floor *and* against the list width (strictly stronger than the plan's `width >= 44`); a filtered-out tick surviving the swap with its DOM order asserted; both ids and no `q` read off the intercepted `POST /api/scan` body; and Enter in the filter issuing the `GET /api/tags` and no POST at all. Re-writing them against a ten-tag list would have added a second set of assertions about the same contract and a second thing to keep in step. They are re-run green here. Only P14, which nothing covered, was written.

**P16's "several status polls" needed a job, and then needed that job taken away.** On the blocked page there is no poll at all — `#status-area` emits `hx-trigger` only while a job is active, and on the blocked server no submit can ever start one. Parking a job makes the poll run, but it also disables the button for a *second* reason, so the assertion would no longer isolate the blocked flag. Retiring the job mid-test is what resolves that: the final out-of-band render is made with nothing active, reached through a real swap rather than a fresh page load, and the flag is demonstrably the only thing left. The in-form half of the same trap is `test_the_blocked_button_survives_its_own_page_load_requests` (30-14) and was not rewritten.

**P9's "both pages show the same state line" is asserted on Job History.** At `AWAITING_FLIP` the owner's status area holds the prompt and the viewer's holds the waiting line — they differ **by design**, and that difference is the thing under test, so they cannot also be the thing asserted equal. The newest Job History row is where both pages report the same job, so title and state are compared there. The owner page is reloaded first, which also means the owner's prompt is a decision taken on the presented cookie rather than a leftover of the response that minted it.

**P17's history assertion is split between the DOM and the job store.** The plan asks for "a `Failed` row with `Not started: the paperless-ngx API token has not been set` … in Job History". `partials/history.html` renders a state label and the title, never `job.error`, so half of that is unavailable from the DOM and no template was changed to make it available. The rendered half (`Failed`, class `status-error`) is asserted on the page; the sentence is asserted on the row through the job store, which is the surface `saneless jobs` and the API actually expose it on. The literal is written out in the test rather than imported, because it is locked copy and this module writes locked copy as literals.

### Acceptance criteria that are unsatisfiable as literally written

None of these was "fixed" by editing another plan's code.

| Criterion | Actual | Why |
|-----------|--------|-----|
| `grep -c "browser.new_context()"` is 2 | **3 lines** | Two are the real calls (3627, 3628), each followed on the next two lines by a `_make_gate` installation. The third is line 394 — 30-17's Pitfall 9 prose, which names the call in order to explain why the factory exists. |
| `grep -n "saneless_owner"` matches at least twice | **1** | The name lives in `_OWNER_COOKIE_NAME` (line 2289, 30-13) and both the owner-present and viewer-absent assertions read it from there. Inlining the literal twice to satisfy a grep would be a worse module. |
| `grep -c "Waiting for the stack to be flipped"` is 1 | **2** | One is 30-13's `test_a_non_owning_browser_gets_no_flip_buttons_in_the_dom`; the other is this plan's `_WAITING_LINE`. The criterion predates 30-13's test. |
| `grep -c "The paperless-ngx API token has not been set — see System status above."` is 1 | **0** | 30-14 wrote that constant with a `\N{EM DASH}` escape (`_BLOCKED_REASON_TEXT`, line 2120), so the em dash character is not in the file. The behavioural assertion is intact — `test_the_reason_line_is_visible_text_beneath_the_button` compares the rendered text to that constant — and per this phase's convention the constant was **not** rewritten to make a grep green. |

### Auto-fixed Issues

None. No bug, missing-critical-functionality or blocking issue was found; every gate passed on the first or second attempt.

### Authentication gates

None.

## Verification

| Gate | Result |
|------|--------|
| `uv run pytest tests/test_browser.py -q` | 127 passed (offline, `blocked` empty on every context including the two hand-made ones) |
| `uv run pytest -q` | 2888 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | all hooks Passed |
| `uv run prek run --stage pre-push --all-files` | all hooks Passed |
| `time.sleep` across `tests/` | **17** — unchanged |
| `grep -c "time.sleep" tests/test_browser.py` | 1 — unchanged |
| `grep -c "Abort this scan? It will stop and cannot be resumed."` | 1 |
| `grep -c "Not started: the paperless-ngx API token has not been set"` | 1 |
| `grep -n "removeAttribute"` | 1 match, in the P17 test |
| `grep -riE "manual.only\|needs human\|human verification"` | 0 matches |

## Loose ends worth carrying into the phase gates

This is the last plan of the phase, so anything that looked unfinished anywhere is named here rather than left to be discovered.

1. **A second full-suite flake, newly observed.** `tests/test_cli.py::TestScanCommand::test_two_cli_scans_of_one_title_preserve_as_two_files` failed once during this plan's first full-suite run with `PaperlessError: Server down. The scan was preserved at .../failed/20260916-215839-94cbac69-same-title.pdf` — the preservation error escaping the CLI rather than being caught. It passed alone, passed with its whole module, and the very next full-suite run was **2888 passed** with no failure. This is *not* the known `TestPollTaskFailureTranslation` deadline flake and has not been reported before, so it is recorded here as a genuinely new, load-dependent flake for the regression gate to weigh rather than as a fixed item.
2. **`_BLOCKED_REASON_TEXT`'s escaped em dash (30-14).** Harmless today, but it means any future grep gate written against that sentence will read zero. Worth a one-line decision at phase close: keep the escape and stop writing greps against it, or inline the character.
3. **`_SESSION_COOKIE_EXPIRY == -1` is now asserted in two places** (30-13's single-context test and this plan's two-context one). Both read the same constant, so there is no drift, but a Playwright upgrade that changed the sentinel would fail both — which is the intended behaviour and is noted only so it is not mistaken for duplication to be removed.
4. **The `[web]` / `[output]` section incoherence** (`web_host` and `web_port` live under `[output]`, config.py's own comment acknowledges it). Deliberate and documented in-tree; named here only because a reader of the phase who meets `WebConfig` for the first time in `_simple_form_settings` will wonder.

## Threat Flags

None. This plan adds no route, no network surface and no schema change, and installs nothing — T-30-SC's "installs nothing" disposition holds. T-30-90 (hand-made contexts escaping the egress gate) is the threat this plan's central test was designed around and is mitigated as the register specifies: both contexts install `_make_gate`, they share one `blocked` list, and that list is asserted empty at teardown.

## Known Stubs

None.

## Self-Check: PASSED

- `tests/test_browser.py` — FOUND (modified)
- `.planning/phases/30-appliance-layer/30-19-SUMMARY.md` — FOUND
- `6c7e750` — FOUND
- `22f6670` — FOUND
- `0e31ecd` — FOUND
- `25a5d2a` — FOUND
