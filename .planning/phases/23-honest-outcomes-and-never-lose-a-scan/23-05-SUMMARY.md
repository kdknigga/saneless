---
phase: 23-honest-outcomes-and-never-lose-a-scan
plan: 05
subsystem: ui
tags: [jinja2, htmx, picocss, playwright, click, fastapi]

# Dependency graph
requires:
  - phase: 23-01
    provides: "JobState.FALLBACK as a terminal member, its state_label -> 'Saved to folder', and TERMINAL_STATES widened to three"
  - phase: 23-02
    provides: "the `saneless jobs` command shape this plan re-renders"
provides:
  - "A FALLBACK branch in partials/status.html rendering `→ Saved to folder: {title}` plus the warning inline, plus the hidden history-reload div"
  - "A .status-fallback arm in partials/history.html and an amber .status-fallback rule in app.css"
  - "app.js re-enables #scan-btn on any of the three terminal status classes, not just two"
  - "`saneless jobs` prints humanised labels; `--json` keeps the raw state and gains outcome + warning"
  - "A _BrowserServer fixture carrying both the URL and the app, so browser tests can drive a job into any state"
  - ".planning/UI-SPEC.md extended with FALLBACK and corrected on two names stale since Phase 21"
affects: [23-07, 23-09, future-ui-phases]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Browser tests reach the live app through a NamedTuple fixture rather than a bare URL"
    - "Terminal-state UI behaviour is asserted against vocabulary.TERMINAL_STATES, not a hand-written state pair"
    - "CLI column widths are derived from max(len(state_label(s)) for s in JobState), not hardcoded"

key-files:
  created:
    - .planning/phases/23-honest-outcomes-and-never-lose-a-scan/deferred-items.md
  modified:
    - src/saneless/web/templates/partials/status.html
    - src/saneless/web/templates/partials/history.html
    - src/saneless/web/static/app.css
    - src/saneless/web/static/app.js
    - src/saneless/cli.py
    - tests/test_web_state_rendering.py
    - tests/test_cli.py
    - tests/test_browser.py
    - docs/how-to/cli-scripting.md
    - .planning/UI-SPEC.md

key-decisions:
  - "U+2192 RIGHTWARDS ARROW as the FALLBACK glyph; U+26A0 rejected because it has an emoji presentation by default and UI-SPEC forbids emoji"
  - "No role=\"alert\" on the fallback paragraphs: a fallback is a degradation, not a failure"
  - "The warning renders as its own visible <p>, not a tooltip, so a user whose metadata was dropped is told without hovering"
  - "`saneless jobs` table humanised to match the web UI's register; `--json` state deliberately left raw as a machine contract"
  - "Status column reserve derived from state_label lengths so a ninth JobState member cannot silently overflow 80 columns"
  - "data-theme=\"auto\" dark-mode defect recorded and deferred, not fixed: it flips the whole app's appearance and is outside this plan"

patterns-established:
  - "Mutation-check the browser assertions: revert the fix, confirm the test goes red, restore"
  - "New template interpolations of upstream-originating text get an explicit XSS regression test, not an assumption about autoescaping"

requirements-completed: [OUTC-02]

# Metrics
duration: 22min
completed: 2026-09-11
---

# Phase 23 Plan 05: FALLBACK Rendering Summary

**The `FALLBACK` state now renders as an amber `→ Saved to folder` line with its warning inline across the status area, the history table and `saneless jobs`, and the two latent bugs a new status class was predicted to trip — a permanently disabled Scan button and a stale history table — are closed and proven closed in a real browser.**

## Performance

- **Duration:** 22 min
- **Started:** 2026-09-11T15:21:00Z
- **Completed:** 2026-09-11T15:43:23Z
- **Tasks:** 3 completed
- **Files modified:** 10 modified, 1 created

## Accomplishments

- FALLBACK has a presentation of its own in all three surfaces, visually and textually distinct from both Complete and Failed, rather than being a state that existed in the enum and nowhere on screen.
- The Scan button no longer strands the user. Before this plan, `app.js` re-enabled it only on `.status-done` or `.status-error`; a fallback job would have left it disabled until a page reload — a successful scan that looked like a locked-up application, strictly worse than the silent DONE this phase exists to remove.
- The history table refreshes on a fallback, because the hidden `hx-trigger="load"` reload div is carried into the new branch.
- `saneless jobs` finally speaks the same language as the web UI, and `--json` gained `outcome` and `warning` so it is honest the moment plan 23-07's writer lands.
- `.planning/UI-SPEC.md` is a contract again rather than a fossil: it records FALLBACK in seven places and no longer names a filter (`humanize_state`) and a lookup table (`app.py:_STATE_LABELS`) that have not existed since Phase 21.

## Task Commits

Each task was committed atomically:

1. **Task 1: Status partial, history table, stylesheet, and the two latent UI-SPEC bugs** — `53c63dc` (feat)
2. **Task 2: `saneless jobs` humanised labels and the new result columns** — `303335c` (feat)
3. **Task 3: Playwright verification of the FALLBACK render, and the UI-SPEC update** — `eb3046b` (test)

## Files Created/Modified

- `src/saneless/web/templates/partials/status.html` — a third terminal branch between DONE and ERROR: the arrow line, the guarded inline warning paragraph, and the history-reload div.
- `src/saneless/web/templates/partials/history.html` — a `status-fallback` arm in the status-cell class expression; the label came free from `{{ job.state | state_label }}`.
- `src/saneless/web/static/app.css` — `.status-fallback` in amber, using neither `--pico-ins-color` nor `--pico-del-color`.
- `src/saneless/web/static/app.js` — `.status-fallback` added to the `htmx:afterSwap` re-enable condition.
- `src/saneless/cli.py` — `_STATUS_COL_WIDTH` derived from `state_label`, the table row humanised, `outcome` and `warning` added to the JSON payload.
- `tests/test_web_state_rendering.py` — the parametrised class and prose assertions extended to FALLBACK, plus four dedicated cases (history reload present/absent, inline warning, no empty paragraph, XSS escaping) and a `_set_warning` helper.
- `tests/test_cli.py` — humanised-label assertion, a FALLBACK table case, a narrow-terminal width case, and a JSON raw-state case.
- `tests/test_browser.py` — the `_BrowserServer` fixture and seven Playwright cases.
- `docs/how-to/cli-scripting.md` — the `--json` example refreshed with the two new keys and the FALLBACK state explained.
- `.planning/UI-SPEC.md` — FALLBACK recorded; two stale names corrected; two verified PicoCSS findings added.
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/deferred-items.md` — created, holding the two out-of-scope findings.

## Decisions Made

**Glyph: U+2192 RIGHTWARDS ARROW (`&#8594;`), rendered `→ Saved to folder: {title}`.** This was the discretionary part of D-05. It is a text-presentation code point, so it does not violate `UI-SPEC.md`'s glyphs-not-emoji rule; it is unmistakable next to `&#10003;` and `&#10007;`; and "it went somewhere else" is precisely what happened. U+26A0 WARNING SIGN was considered and rejected: most platforms give it an emoji presentation by default. The rejection is now written into the copy style guide so the next person does not re-litigate it.

**No `role="alert"`.** The ERROR branch has one; the FALLBACK branch deliberately does not. A screen reader interrupting the user to announce a scan that succeeded would overstate the situation, which is exactly what D-05 rejects. A test asserts the attribute's absence so it cannot be added by reflex later.

**The warning is a visible paragraph, not a tooltip.** A user who does not already know their title, tags and correspondent were dropped will not hover anything to find out. It also gives a long warning its own block to wrap in rather than destroying the status-area layout (T-23-22).

**Amber colour token.** `var(--pico-color-amber-600, #a16207)`. Verified against the CDN build `base.html` actually links rather than trusting the plan's spelling: `pico.min.css` carries the semantic tokens but **not** the `--pico-color-*` palette, which ships separately as `pico.colors.css`. The variable therefore does not resolve and the literal amber is what renders. The `var()` is kept so linking the palette file later picks it up with no CSS change. Recorded in `UI-SPEC.md` and `deferred-items.md`.

**`saneless jobs` table humanised, `--json` left raw.** The table is read by people and has been printing `DONE` while the web UI printed "Complete" since Phase 12; leaving it raw would have meant FALLBACK printing as `FALLBACK`, technically distinct but in a different register from every other surface. `--json` is a machine contract and a test now pins `state == "FALLBACK"` so a future humanising sweep cannot quietly break scripts. This is user-visible and belongs in the release notes.

**Column width derived, not hardcoded.** The reserve moved from `50` to `ts_w + profile_w + _STATUS_COL_WIDTH + 3`, where `_STATUS_COL_WIDTH = max(len(state_label(s)) for s in JobState)`. "Waiting for flip" is 16 characters against `AWAITING_FLIP`'s 13, so the old constant was already tight; a ninth member would have overflowed silently.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical functionality] `docs/how-to/cli-scripting.md` documented a `--json` payload that no longer matched its writer**

- **Found during:** Task 2 (`saneless jobs` result columns)
- **Issue:** The how-to's Job-history JSON example listed five keys. Adding `outcome` and `warning` made the published machine contract wrong, and it was the only place either key was described to a script author.
- **Fix:** Updated the example and added prose stating that `state` is always the raw uppercase enum value, that the humanised labels never appear in `--json`, and what a `FALLBACK` state means for the document.
- **Files modified:** `docs/how-to/cli-scripting.md`
- **Verification:** `uv run pytest -q` (669 passed); no test asserts the doc's content, so this is a documentation correctness fix rather than a covered behaviour.
- **Committed in:** `303335c` (part of the Task 2 commit)

**2. [Rule 2 - Missing critical functionality] Four `.planning/UI-SPEC.md` statements beyond the seven the plan named were false after this change**

- **Found during:** Task 3 (the UI-SPEC update)
- **Issue:** D-20's rule is that whatever behaviour changes, its written description is corrected in the same phase. Four further lines were made or left wrong: the Theme-mode row and the Accessibility Theme row asserted that `data-theme="auto"` honours the OS preference (it does not — see Issues); the Design-philosophy line quoted stale CSS/JS line counts; the polling note said the poll stops on "DONE/ERROR"; and the icon inventory omitted U+2192.
- **Fix:** Corrected all four, plus a new Warning-semantic row in the colour-token table and a two-item "findings" note under Color recording what was verified against the live PicoCSS build.
- **Files modified:** `.planning/UI-SPEC.md`
- **Verification:** `grep -c 'humanize_state'` → 0, `grep -c '_STATE_LABELS'` → 0, `grep -c 'status-fallback'` → 9, `grep -c 'Saved to folder'` → 3.
- **Committed in:** `eb3046b` (part of the Task 3 commit)

### Approach Changes

**3. [Refinement] `browser_server_url` kept as a derived fixture rather than rewritten at every call site**

- **Found during:** Task 3
- **Plan said:** change `browser_server_url` to yield a URL-plus-app object and update the existing browser tests' unpacking.
- **Done instead:** a new session-scoped `browser_server` fixture yields `_BrowserServer(url, app)`, and `browser_server_url` survives as a one-line fixture returning `browser_server.url`.
- **Why:** identical capability, and the eight pre-existing browser tests are untouched, so nothing unrelated to this plan could break in the rewrite. Both names are available to future tests.
- **Files modified:** `tests/test_browser.py`
- **Committed in:** `eb3046b`

---

**Total deviations:** 2 auto-fixed (both Rule 2), 1 approach refinement.
**Impact on plan:** No scope creep. Both auto-fixes are documentation that this plan's own changes falsified; leaving either would have produced exactly the contract-versus-code drift D-20 exists to prevent.

## Issues Encountered

**The suite was green before this plan started, and that meant nothing.** As wave 1 warned, `23-VALIDATION.md` predicted `tests/test_web_state_rendering.py` and `tests/test_cli.py` would go red once `list(JobState)` grew an eighth member, and they did not — those parametrised cases assert only structural properties that FALLBACK satisfied from its `state_label` arm alone. The work here was therefore driven by tests written specifically for it, not by pre-existing failures. Red was observed deliberately at each step: 6 failures in `test_web_state_rendering.py` and 5 in `test_cli.py` before their implementations landed.

**A standalone TDD RED commit remains impossible in this repo.** All three wave-1 executors hit this and so did this one: `prek` runs `ty` and `pyrefly` on every commit, and both reject a test file referencing symbols or markup that do not exist yet. `--no-verify`, `# type: ignore` and `SKIP=` are all forbidden or blocked. RED was observed and its output recorded in each commit message, then RED and GREEN were committed together.

**`data-theme="auto"` does not do what the project believes it does.** While verifying the amber against the real stylesheet, the CDN build turned out to scope Pico v2's automatic dark rule to `:root:not([data-theme])` — the attribute must be **absent**, not `"auto"` (that spelling was Pico v1). `base.html` sets `data-theme="auto"`, so the application renders in the light palette for everyone regardless of OS preference, and has since Phase 12. `UI-SPEC.md` asserted the opposite in three places. The written claims are corrected; the attribute is deliberately **not** changed — removing it flips the whole application's appearance and carries its own contrast-audit obligations for every component, which is not a side effect one new job state should have. Logged in `deferred-items.md` with the contrast measurement that fix would need to address.

**The browser assertions were mutation-checked, not merely observed green.** A passing Playwright test proves little if it would also pass against the unfixed code. Pointing `.status-fallback` at `--pico-ins-color` turns the colour test red in both schemes; removing `.status-fallback` from the `app.js` condition turns the button test red in both schemes. Both were reverted afterwards and the file contents re-verified.

## Verification

| Check | Result |
|-------|--------|
| `uv run pytest -q` | 669 passed |
| `uv run pytest tests/test_browser.py -m browser -q` | 15 passed (8 pre-existing + 7 new) |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| No `# type: ignore`, `# noqa`, disabled rule, or `!important` added | confirmed by grep |
| No `checkpoint:human-verify` task | confirmed — every browser assertion is automated |

Plan acceptance greps, all met: `status.html` `status-fallback` = 2, `history.html` = 1, `app.css` = 1, `app.js` = 1, `&#8594;` = 1, warning-sign glyphs = 0, `hx-get="/api/jobs/history"` = 3, `pico-ins-color|pico-del-color` = 2, `!important` = 0, `cli.py` `state_label` = 3, `j.state.value` = 1, `"outcome"` = 1, `"warning"` = 1, `UI-SPEC.md` `status-fallback` = 9, `Saved to folder` = 3, `humanize_state` = 0, `_STATE_LABELS` = 0.

## Threat Model Coverage

| Threat ID | Disposition | How it is discharged here |
|-----------|-------------|---------------------------|
| T-23-21 | mitigated | `test_fallback_warning_is_escaped_not_injected` drives a FALLBACK job with a warning of `<script>alert(1)</script>` and asserts the raw tag is absent while `&lt;script&gt;…` is present. Autoescaping is now pinned rather than assumed for this new interpolation. |
| T-23-22 | mitigated | The warning renders in its own `<p>`, not concatenated into the title line, so a long value wraps in its own block. The 500-character cap is plan 23-04's half. |
| T-23-23 | mitigated | `.status-fallback` added to the `htmx:afterSwap` condition, and `test_scan_button_re_enables_after_a_fallback_swap` drives a real htmx swap in Chromium and watches the button recover. Mutation-checked. |
| T-23-24 | mitigated | The hidden reload div is copied into the FALLBACK branch; `test_fallback_reloads_the_history_table` pins its presence for FALLBACK and absence for a live state, and `test_fallback_swap_refreshes_the_history_table` proves it fires in a browser. |
| T-23-25 | accepted | Unchanged by this plan — `j.title` has been echoed through `click.echo` since Phase 1. Recorded as a decided position. |

## Threat Flags

None. No new network endpoint, auth path, file-access pattern, or schema change. `outcome` and `warning` are existing columns gaining a reader, not a writer.

## Known Stubs

None. `outcome` and `warning` read `None` in `saneless jobs --json` because no writer exists until plan 23-07 — that is a truthful `null` for a column with no value, not a placeholder, and `test_jobs_json_keeps_the_raw_state_value` asserts it explicitly so the transition is visible when the writer lands.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Plan 23-07 lands the `FALLBACK` writer. Every surface it needs is now in place: the moment a job is written with `state=FALLBACK` and a `warning`, the status area, the history table, `saneless jobs` and `--json` all render it correctly with no further UI work.
- `.planning/UI-SPEC.md` is accurate as of this commit and can be trusted as the contract by later UI plans in this phase.
- One deferred item is outstanding and recorded in `deferred-items.md`: `data-theme="auto"` does not engage Pico v2's dark palette. It is not a blocker for this phase.

---
*Phase: 23-honest-outcomes-and-never-lose-a-scan*
*Completed: 2026-09-11*
