---
phase: 26-worker-and-web-robustness
plan: 12
subsystem: testing
tags: [playwright, browser-tests, ci, offline, htmx, scan-button]
requires:
  - phase: 26-03
    provides: "vendored, SRI-pinned Pico and htmx"
  - phase: 26-05
    provides: "#status-message alert slot"
  - phase: 26-11
    provides: "server-owned Scan button, hx-disabled-elt/hx-disinherit, app.js deleted, title maxlength"
provides:
  - "No-egress gate on every browser test (overridden pytest-playwright context fixture + egress_allowlist)"
  - "TestOfflinePage: B2, B3, B4 (light/dark), B14"
  - "TestServerOwnedScanButton: B5, B6, B7 through real scans"
  - "Gated _BrowserTestScanner, delivering_paperless and scan_harness fixtures"
  - "CI browser job running uv run pytest -m browser"
affects: [26-13]
tech-stack:
  added: []
  patterns:
    - "Offline proof in-test: context.route('**/*') abort-and-record, asserted at teardown"
    - "Non-retrying DOM read where a periodic poll would heal the defect under a retrying expect"
key-files:
  created: []
  modified:
    - tests/test_browser.py
    - .github/workflows/ci.yml
    - CONTRIBUTING.md
key-decisions:
  - "B7 reads #scan-btn with a non-retrying is_disabled() after both select loads fire htmx:afterRequest; a retrying expect waited out the 1 s status poll and passed with the trap sprung"
  - "The stub scanner returns a half-black page so empty-page detection does not end the real scan in ERROR"
  - "Paperless stubbing uses pytest monkeypatch (restores on teardown) rather than a hand-written finally"
  - "browser is not made a required ruleset check; that repository-settings decision stays with the user"
requirements-completed: [ROBU-11, ROBU-04, ROBU-09]
duration: ~45min
completed: 2026-09-14
---

# Phase 26 Plan 12: Offline browser proof of the Scan button, in CI Summary

**Every Playwright test now runs behind an abort-and-record egress gate. Real scans in Chromium show the Scan button is released at DONE, busy while held, and still disabled on a page opened mid-scan. A new `browser` CI job runs the whole module.**

## Performance

- **Duration:** about 45 min
- **Completed:** 2026-09-14
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- **B0 egress gate.** `tests/test_browser.py` overrides pytest-playwright's `context` fixture with `context.route("**/*", _gate)`. `_gate` continues requests addressed to an `egress_allowlist` base and aborts and records everything else. The test fails at teardown if anything was recorded. Every page in the module goes through it, including the DARK-01/DARK-02 tests.
- **TestOfflinePage.** B2: htmx 2.0.8 is loaded with three `responseHandling` rules and the `[45]..` rule swaps; there is no `data-theme` and no `pico.colors`. B3: nothing references `app.js`, and requesting it returns 404. B4: `#status-message` is a single empty `role="alert"` with zero height, placed before `#status-area`, in both schemes. B14: `#title-input` has maxlength 256, and filling it with 300 characters leaves 256.
- **TestServerOwnedScanButton.** B5: a real click reaches `.status-done`, then there is exactly one enabled `Scan` button with no `aria-busy`. B6: with the gate closed the button shows disabled, `aria-busy="true"` and `Scanning…` within 3 s, then is released once the gate opens. B7: a page loaded while a job is in SCANNING keeps the button disabled after the `/api/tags` and `/api/correspondents` loads.
- **Fallback-swap test rewritten.** It forces a stale disabled button with one evaluate, then asserts that the server's out-of-band render releases it with `aria-busy` absent. `_DISABLE_SCAN_BUTTON` is deleted.
- **CI job.** `.github/workflows/ci.yml` has a `browser` job with the same pinned SHAs and SANE headers step as `test`. It runs `uv run playwright install --with-deps chromium` and then `uv run pytest -m browser`. CONTRIBUTING.md now describes seven checks across three jobs.

## Task Commits

1. **Task 1: no-egress gate and page-structure checks (B0, B2, B3, B4, B14)**: `ee8cec6` (test)
2. **Task 2: Scan button through real scans (B5, B6, B7) and fallback-swap rewrite**: `0bb574f` (test)
3. **Task 3: browser CI job and CONTRIBUTING.md**: `4d6fff8` (ci)

## Executor Self-Checks (run locally, not committed)

| Mutation | Expected failure | Observed |
|---|---|---|
| Put a jsdelivr htmx `<script>` back into `base.html` | B0 gate | `AssertionError: the page tried to reach the network: ['https://cdn.jsdelivr.net/npm/htmx.org@2.0.8/dist/htmx.min.js']`: FAILED as expected |
| Remove `hx-disinherit="hx-disabled-elt"` from `index.html` | B7 | `AssertionError: a page load during an active scan re-enabled the Scan button` (3 of 3 runs): FAILED as expected |
| `htmx-config` holding only the `[45]..` entry | B5 and B2 | B5: `Locator expected to be visible ... #status-area .status-done` timed out after 15 s; B2: `assert 1 == 3` on `responseHandling.length`: FAILED as expected |

Every template was restored from a scratchpad backup after each mutation, and `git status` showed only the intended files.

**Assumption A3 (data: URIs):** confirmed. With the gate recording every non-allowlisted URL, Pico's inline `data:` SVGs never reached the handler, so no `data:` exemption was added.

**B14:** Playwright's `fill` respects `maxlength` (the value came back as 256 characters), so `press_sequentially` was not needed.

## Verification

- `uv run pytest tests/test_browser.py -m browser -q`: 39 passed, twice in a row with the default (random) order
- `uv run pytest -m browser -q`: 39 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1386 passed
- `uv run ruff check tests`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: clean. pyrefly reports 4 existing warnings in `pipeline.py`, `sane_backend.py` and `fake_sane.py`, none of them from this plan
- `uv run prek run --all-files` and `uv run prek run --stage pre-push --all-files`: pass
- The YAML structure assertion from the plan's Task 3 verify passes, `grep -c` finds the checkout pin 3 times, and the old "Run those locally" text is gone

## Decisions Made

- **B7 uses a non-retrying read (Rule 1).** The first version followed the plan and used `expect(#scan-btn).to_be_disabled()`, and it passed with `hx-disinherit` removed. A DOM mutation log showed the trap does spring: htmx strips `disabled` when the select loads finish. The retrying `expect` then waited for the 1 s status poll to re-render the button disabled. The test now registers an init script that counts `htmx:afterRequest` for the two selects, waits for both, and asserts `locator.is_disabled()` once. htmx 2.0.8 strips `disabled` before it fires `afterRequest`, so the read is deterministic.
- **Stub page content (Rule 3).** The white 100x100 page would be removed by the default profile's empty-page detection, and the job would end in ERROR (`All pages were detected as empty`). The page is now half black, so B5 and B6 can reach DONE.
- **monkeypatch for Paperless.** The plan said "restore both originals in `finally`". `pytest.MonkeyPatch.setattr` does the same restore and is the standard tool for it. `scan_harness` depends on `delivering_paperless`, so it tears down first: every job finishes while uploads are still stubbed.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] B7 did not detect the trap it was written for**
- **Found during:** Task 2, the mutation self-check
- **Issue:** the retrying `expect(...).to_be_disabled()` waited out the 1 s status poll, which re-disables the button
- **Fix:** wait for both select `htmx:afterRequest` events, then assert once with `is_disabled()`
- **Files modified:** tests/test_browser.py
- **Commit:** 0bb574f

**2. [Rule 3 - Blocking] The stub scan was detected as an empty page**
- **Found during:** Task 2
- **Issue:** an all-white page fails empty-page detection, so the job could never reach DONE
- **Fix:** `scan_pages` returns a half-black page
- **Files modified:** tests/test_browser.py
- **Commit:** 0bb574f

**3. [Rule 2 - Doc accuracy] Other CONTRIBUTING.md counts**
- **Found during:** Task 3
- **Issue:** "run the six commands", "all six checks" and "both CI jobs already install it" became wrong once a third job existed
- **Fix:** updated to seven checks and every CI job. Added one sentence under "How `master` is protected" saying `browser` runs on pull requests but is not yet a required check
- **Files modified:** CONTRIBUTING.md
- **Commit:** 4d6fff8

## Notes for the User

- **`browser` is not a required status check.** The `master` ruleset still requires only `lint` and `test`. Making `browser` required is a repository-settings change, and nothing in this plan changed it.
- The CI job has not run yet. It is proven when the branch is pushed; nothing was pushed here.

## Next Phase Readiness

- Plan 26-13 (message-slot browser tests B8-B13 and the LAN-IP cross-origin proof) can reuse `scan_harness`, the scanner gate, `delivering_paperless`, and `egress_allowlist` (append a second origin's base URL).

## Self-Check: PASSED

- FOUND: tests/test_browser.py, .github/workflows/ci.yml, CONTRIBUTING.md
- FOUND commits: ee8cec6, 0bb574f, 4d6fff8
