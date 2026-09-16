---
phase: 30-appliance-layer
plan: 17
subsystem: web
tags: [playwright, egress-gate, status-strip, contrast, htmx-polling, timestamps]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "plan 30-11's #checks-card / #checks-body strip and its three state classes"
  - phase: 30-appliance-layer
    provides: "plan 30-12's status.html ERROR branch, page-counts line and history Title cell"
  - phase: 30-appliance-layer
    provides: "plan 30-15's live profile description route and :empty rule"
  - phase: 30-appliance-layer
    provides: "plan 30-16's tagged_server fixture and the htmx afterSettle finding"
provides:
  - "_make_gate(blocked, allowlist) -- the module-level egress gate every context must install"
  - "_serve(settings, scanner) -- a private uvicorn app for the length of one test"
  - "_spool_pages(sink, count, resolution) -- the shared page-spooling both stubs use"
  - "_ManualDuplexScanner -- a stub whose pass A produces a known front count and whose pass B is gated"
  - "cold_strip_server / empty_history_server / described_profile_server fixtures"
  - "_probe_now(server) -- probe-and-store through the app's own refresh route, from outside the browser"
  - "_check_row(page, name) -- locate a strip row by its rendered name"
  - "the P1-P8 and P15 browser assertions"
affects: [30-19]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a route-gate factory at module level, so a hand-made browser context cannot be built without one"
    - "a private uvicorn app per test when the claim is about what is ABSENT from a whole page"
    - "stopping the background refresher and driving the cache through the app's own route, so a poll assertion is not racing a stopwatch"
    - "a witness attribute set from the test as the proof an element was updated rather than replaced"
    - "deriving the expected zone token from one surface and requiring the other to echo it, so the test is host-independent"

key-files:
  created: []
  modified:
    - tests/test_browser.py

key-decisions:
  - "_make_gate reads the allowlist at request time rather than capturing it, so a base appended inside a test still counts -- the behaviour lan_server and blocked_server already depend on"
  - "P2 stores results from OUTSIDE the browser (httpx POST /api/checks/refresh), so the swap observed is the page's own 2 s poll finding them rather than a button click's own swap"
  - "the cold-strip fixture stops the background refresher instead of racing it; with it running the cold window is 'however long a tick takes' and the assertion is a stopwatch"
  - "P4 sets the two inputs a live scan produces -- a current job and a held scanner gate -- rather than running one; the worker's gate-holding is test_worker.py's subject and the skip decision is test_refresher.py's"
  - "P6 runs its three cases as separate page loads on an empty-history server, so the NULL case can say 'no .page-counts element on this page at all' rather than only 'not on this row'"
  - "P5 uses ErrorCategory.UPLOAD, not UNKNOWN: UNKNOWN's next step ends 'check the saneless log', and a fixture carrying the word the D-13 assertion hunts for would be arguing with itself"
  - "P7 drives a real manual-duplex scan through the pipeline's own pass-count callback; the front count lives on the worker for the length of one pass and has no column to be written to"
  - "P8 marks the slot with a data-witness attribute -- every other assertion in the existing swap test is satisfied by an identical replacement node"

patterns-established:
  - "Pattern: any shared browser-context concern (routing, cookies, permissions) lives in a module-level factory, never in a fixture closure, or a second context silently gets none of it"
  - "Pattern: a browser claim about absence gets its own server, because the session store and the session config are shared with every other test in the module"
  - "Pattern: when a background thread would fill the state under test, stop it and drive the same state through the application's own route"

requirements-completed: [APPL-02, APPL-03, APPL-04, APPL-05, APPL-12]

# Metrics
duration: 22min
completed: 2026-09-16
---

# Phase 30 Plan 17: The Strip, the Errors, the Counts and the Timestamps in Chromium Summary

**The egress gate is now a module-level factory a hand-made context can install, and nine UI-SPEC verification rows that only a browser can answer — placement, cold-start polling, three verdict colours, the paused strip, the plain-language failure, the counts, the front count, the live description and the zone tokens — are asserted in Chromium.**

## Performance

- **Duration:** 22 min
- **Tasks:** 3 (3 commits — `type: execute`, no RED gate)
- **Files created:** 0 · **Files modified:** 1
- **Tests:** 93 → 114 browser tests; full suite 2861 passed

## Accomplishments

- **B0 — the gate is extractable and extracted (Pitfall 9, T-30-81).** `_make_gate(blocked, allowlist)` is a module-level factory and the overridden `context` fixture installs what it returns. The trap is written out above it in full: the closure used to live *inside* that fixture, so a test building its own context with `browser.new_context()` — which 30-19's owner/non-owner proof requires — would have carried **no gate at all**, could have reached the real internet in CI, and nothing would have failed, because the only `blocked` list anybody asserted on belonged to a context that test never used. The allowlist is read when each request arrives rather than captured, so a base appended mid-test still counts; the shared-`blocked` contract is stated so one assertion can speak for several contexts.
- **P1 — the strip is first, and phase 26's slot did not move.** `main > article` nth(0) is `#checks-card`, `compareDocumentPosition` confirms it precedes the Scan card, `#checks-body` occurs exactly once (the specific way an out-of-band-rendering partial can go wrong), and `#status-message.nextElementSibling.id === "status-area"`.
- **P2 — the cold-start poll starts and then stops, in a real browser.** On a private server whose refresher is stopped, the body carries `hx-trigger` matching `every 2s` and five `Checking…` rows. Results are then stored from **outside** the browser through `POST /api/checks/refresh`, so what discovers them is the page's own poll. The trigger-less state is asserted with a `#checks-body:not([hx-trigger])` locator — it is the attribute's *absence* that ends the poll, and that selector auto-waits for exactly that.
- **P3 — all three verdict colours, both schemes, plus forced dark.** Six parametrised cases on the **card** surface (the binding one) assert the computed colour equals the pinned token and the WCAG ratio clears 4.5, and three more do the same under a forced `data-theme="dark"` with a light OS. `.check-warn` reuses the existing `_AMBER` pair rather than a second set of literals — it shares one custom property with `.status-fallback`, so a drift must be reported by both. `_SUCCESS_GREEN` is new and pinned to UI-SPEC's `--pico-ins-color` values.
- **P4 — the paused strip.** With a current job on the worker and the scanner gate held, the Scanner row reads `Not checked while a scan is running.` and `.check-meta` **starts** `Paused during scan — ` (em dash, as D-08's specimen writes it).
- **P5 — one alert, a shut disclosure outside it, and no path (T-30-82).** Exactly one `[role="alert"]` in the status area, carrying both `error_message` and `error_next_step`; `details.tech-details` present, **not** `open`, and asserted to be outside the live region rather than merely after it. Expanding it reveals the specific message, `Category: UPLOAD` and the job id. `page.content()` contains neither the configured `log_file` nor the substring `.log` — the private server is what makes both of those whole-page claims meaningful.
- **P6 — counts in both places, or nowhere (T-30-83).** Three parametrised rows: `12 pages scanned, 2 blank removed, 10 uploaded` in the status area *and* in the history Title cell; `0 blank removed` for a measured zero; and **zero** `.page-counts` elements anywhere for NULL counts. Separate page loads on an empty-history server, so the negative case is stated at full strength.
- **P7 — the front count leads the busy line.** A real submit on a manual-duplex profile, a real `Continue` click, and `_ManualDuplexScanner` holding pass B parks the job in `SCANNING_REVERSE` with twelve fronts delivered through the pipeline's own `_note_pass_count` callback. The assertion is `startswith("Front: 12 pages · ")`, not containment: leading with the number is the contract.
- **P8 — the slot is *updated*, not replaced.** A `data-witness` attribute set from the test survives the swap, which is the one thing an `outerHTML` swap would fail and every other assertion in the pre-existing swap test would tolerate. A four-profile private server adds the two shapes the shared pair has no example of: a label-less profile offered under its own name (beside a labelled one, so Amendment A-3's fallback cannot pass by never being reached), and a description-less profile whose slot is `display: none` with no visible text.
- **P15 — one zone token, two surfaces.** The first history `Time` cell is matched against `^\d{4}-\d{2}-\d{2} \d{2}:\d{2} (\S+)$` and the zone is **captured from it**; the strip's freshness line is then required to end with that same token. Nothing about the runner's own `TZ` is written down.
- **No sleep added (T-30-84).** Every wait is Playwright `expect(...)` auto-waiting, a `threading.Event`, or the existing `wait_for_state` helper. `time.sleep` in `tests/` stands at 17, unchanged.
- **Nothing is deferred to a person.** A module-level note records, per CLAUDE.md, that every browser behaviour in this phase is automated and that the single out-of-suite item is scanner reachability against physical hardware, which `30-VALIDATION.md` already carries. `grep -riE "manual.only|needs human|human verification" tests/test_browser.py` matches zero times.

## Task Commits

1. **Task 1: extract the egress gate, then assert the strip (B0, P1–P4)** — `ca68021`
   - `_make_gate` + the Pitfall 9 note; `context` fixture rewritten to use it
   - `_serve` (private uvicorn app), `_spool_pages`, `cold_strip_server`, `_probe_now`, `_check_row`
   - `_SUCCESS_GREEN`, `_CHECK_STATE_COLOURS`
   - `TestStatusStripInChromium` — 12 tests
2. **Task 2: errors and page counts in the browser (P5, P6, P7)** — `4a6a913`
   - `empty_history_server`, `_ManualDuplexScanner`, `duplex_server`
   - `TestErrorRenderingInChromium`, `TestPageCountsInChromium`, `TestManualDuplexFrontCountInChromium` — 5 tests
3. **Task 3: the live profile description and the zone-bearing timestamps (P8, P15)** — `e39625b`
   - `_described_profile_settings`, `described_profile_server`
   - `TestProfileDescriptionInChromium`, `TestTimestampZonesInChromium` — 4 tests

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] `_PROBE_CONTEXT_CONTRAST`'s "card" comment named the wrong article**

- **Found during:** Task 1, while writing P3
- **Issue:** The JS probe's comment read "The first `<article>` is the Scan card". Since plan 30-11 inserted `#checks-card` above the form, the first article is the status strip's card. P3 measures the card surface, so a comment misidentifying which card that is would actively mislead the next reader of the very test being added.
- **Fix:** Rewritten to say that any `<article>` is Pico's secondary surface and that the two cards are the same colour, with the history of the change noted.
- **Files modified:** `tests/test_browser.py`
- **Commit:** `ca68021`

### Interpretations, not deviations

**P6's "three job rows" is three page loads, not one page.** The plan's own acceptance criterion asks for "three parametrised job rows", and its third assertion — "**zero** `.page-counts` elements anywhere on the page" — cannot hold while rows one and two are in the same history table. Parametrising is what lets the negative case be stated at full strength rather than narrowed to "not in this row".

**P4 does not run a live scan.** The plan suggested holding a job with `_BrowserTestScanner`'s gate. What the strip actually reads is two pieces of state — `worker.current_job_id is not None` and the scanner gate being unavailable — and both are set directly, which is the technique three existing fixtures in this module already use for the worker pointer. Running a real scan would have made the assertion depend on a background refresher tick landing inside the window between the worker acquiring the gate and the test reading the page: a stopwatch race, which is exactly the flake T-30-84 asks to be designed out. The refresher's own policy is covered by `tests/test_refresher.py` and the worker's gate-holding by `tests/test_worker.py`; what is provable only here is the rendering, and the rendering is what is proved.

**P2 warms the cache over HTTP rather than by clicking "Check again".** The plan says "wait for the swap", and the swap under test is the *poll's*. Clicking the button would have made the button's own swap the observed one, which proves a different thing. `POST /api/checks/refresh` from `httpx` is the application's own probe-and-store path (branch 3 of `CrossOriginGuard`, the non-browser branch), and it leaves the page untouched so the poll has to find the results itself.

### Authentication gates

None.

## Verification

| Gate | Result |
|------|--------|
| `uv run pytest tests/test_browser.py -q` | 114 passed |
| `uv run pytest -q` | 2861 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -n "def _make_gate(" tests/test_browser.py` | one match, column 1 |
| `grep -c "def _gate(" tests/test_browser.py` | 1 (inside `_make_gate`) |
| `grep -c "time.sleep" tests/test_browser.py` | 1 — unchanged |
| `time.sleep` across `tests/` | 17 — unchanged |
| `grep -riE "manual.only\|needs human\|human verification" tests/test_browser.py` | 0 matches |

## Accumulated browser debt: what this plan did NOT cover

Routed to **30-19**. None of these is in 30-17's scope and none was touched.

| Item | Deferred by | Status after this plan |
|------|-------------|------------------------|
| Colour parametrisation for `check-ok` / `check-warn` / `check-fail` | 30-11, 30-12 | **Done** — P3, six emulated cases plus three forced-dark |
| Colour parametrisation for `page-counts` | 30-11, 30-12 | **Still open.** `.page-counts` reads `var(--pico-muted-color)`, the identical computed colour `.status-cancelled` resolves to, and that value is already proven ≥ 4.5:1 in the status-area, history-cell and card contexts in both schemes. So this is **un-restated, not unmeasured** — a one-line parametrisation, not a gap in the evidence. |
| Cold-start poll stopping in a real browser | 30-11 | **Done** — P2 |
| The `Check again` round trip (the button clicked in Chromium) | 30-11 | **Still open.** This plan drives the same route over HTTP, deliberately, so P2's swap is the poll's. Nobody has yet clicked `.check-refresh` in a browser and watched `#checks-body` swap `outerHTML` under it. |
| Strip layout at 320 px | 30-11 | **Still open.** `.check-row` is a wrapping flex row with a fixed 1 rem glyph gutter and a 7 rem name column, designed so the message wraps under the name at 320 px rather than forcing a horizontal scrollbar. No viewport test in this module is narrower than 375 px. |

## Threat Flags

None. This plan adds no network surface, no route, no schema change and installs nothing; T-30-SC's "installs nothing" disposition holds.

## Known Stubs

None.

## Self-Check: PASSED

- `tests/test_browser.py` — FOUND (modified)
- `.planning/phases/30-appliance-layer/30-17-SUMMARY.md` — FOUND
- `ca68021` — FOUND
- `4a6a913` — FOUND
- `e39625b` — FOUND
