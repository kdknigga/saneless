---
phase: 30-appliance-layer
plan: 34
subsystem: ui
tags: [health-checks, accessibility, jinja2, click, screen-reader, tdd, r3-wr-03]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the shared check registry and its state lookups (30-01..30-30), the host-parser hardening in checks.py (30-31) and the strip's poll/error contract (30-32)"
provides:
  - "checks.SKIPPED_STATE_LABEL, the screen-reader word for a row nothing probed"
  - "checks.check_row_class / check_row_glyph / check_row_label, row-level markers that take a whole CheckResult and read the skipped flag before the state"
  - "cli._SKIPPED_MARKER and cli._row_marker, doctor's counterpart of the same substitution"
  - "a _MARKER_WIDTH derived over the state tokens and the skipped one together, so _NEXT_STEP_INDENT stays correct by construction"
  - "a Chromium measurement of a real skipped row that fails when the wiring is reverted"
affects: [status strip, saneless doctor, 30-UI-SPEC S1, any future CheckResult flag]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a row-level lookup takes the whole record and delegates to the field-level lookup, so a flag that changes what is rendered cannot be missed by a call site that only had the field"
    - "borrow a marker's glyph and colour without borrowing its word, when the two convey different things to a reader and a listener"
    - "a derived column width is computed over every token that can occupy the column, not over the enum that used to be the only source"
    - "wire a surface for a row it cannot yet produce, and say in a comment why, so the next reviewer reads the reason instead of filing it as dead code"

key-files:
  created: []
  modified:
    - src/saneless/checks.py
    - src/saneless/cli.py
    - src/saneless/web/app.py
    - src/saneless/web/templates/partials/checks.html
    - tests/test_checks.py
    - tests/test_web_checks.py
    - tests/test_doctor.py
    - tests/test_web.py
    - tests/test_browser.py
    - .planning/phases/30-appliance-layer/30-UI-SPEC.md

key-decisions:
  - "The skipped row borrows CHECKING_GLYPH and CHECKING_STATE_CLASS but not CHECKING_STATE_LABEL: a listener hearing 'Checking' over a row that was deliberately not probed is told a probe is running, which is a smaller version of the lie the green tick told, so SKIPPED_STATE_LABEL = 'Not checked' is a new constant and the only new vocabulary this plan adds"
  - "The substitution happens at the template's call site, not inside the check_row macro: the macro's parameter names stay state_class/glyph/state_label and now mean 'the marker this row renders', which keeps the rule in Python and the macro deciding nothing"
  - "check_state_class/glyph/label stay registered as Jinja filters even though the template reaches for none of them, because they are what the row filters delegate to and removing a registered filter name is a separate decision from adding one"
  - "_MARKER_WIDTH is computed over the three state markers AND _SKIPPED_MARKER rather than relying on the four literals happening to be six characters, so _NEXT_STEP_INDENT survives any one of them being respelled"
  - "doctor gets the marker although no current invocation can produce a skipped row (the command defaults skip_scanner and passes no scanner gate); the comment above _SKIPPED_MARKER says so explicitly and gives D-02 and this finding's own history as the reason"
  - "The UI-SPEC's five-row table gained the busy row alongside the paused one: both are skipped rows the registry can build, and a contract that named only one of them would leave the other undocumented"

patterns-established:
  - "Every mutation named in the plan's acceptance criteria was applied, run, counted and reverted, and the failure counts are recorded below"
  - "A rendering change to the strip is measured in Chromium on the row the application really produces, and the measurement is proved by re-running it under the mutation"

requirements-completed: [APPL-01, APPL-02]

# Metrics
duration: 19min
completed: 2026-09-17
---

# Phase 30 Plan 34: The Skipped Flag Both Surfaces Were Documented to Render Summary

**`CheckResult.skipped` is now read by the code and not only by two docstrings: a Scanner row nobody probed renders the neutral `·` in `check-checking` and is announced as "Not checked", prints `[SKIP]` in `saneless doctor`, and still carries `CheckState.OK` so a scripted health gate stays green — verified in Chromium against the row the application itself produces, and each of the three wirings fails a test the moment it is reverted.**

## Performance

- **Duration:** ~19 min (first commit 2026-09-17T20:54:03-05:00, last 21:12:46-05:00)
- **Tasks:** 3 (each RED → GREEN)
- **Commits:** 7
- **Files modified:** 10 (0 created, 0 deleted)

## What was built

### Task 1 — row-level markers in `checks.py` (`f98c629` RED, `31eba84` GREEN)

`SKIPPED_STATE_LABEL: Final = "Not checked"` sits immediately after `CHECKING_STATE_LABEL`, with the reason in a comment: the glyph and the colour class are reused from the cold-start trio because U+00B7 already means "not yet, not bad", but the *word* is not, because "Checking" spoken over a row that was deliberately not probed tells a listener a probe is running.

`check_row_class`, `check_row_glyph` and `check_row_label` each take a whole `CheckResult` and delegate to their `check_state_*` counterpart unless `result.skipped` is set. All three are exported. The three `check_state_*` functions are untouched — they are still the state's own vocabulary and still what the row functions delegate to.

`_scanner_skipped`'s and `_scanner_busy`'s docstrings now name the four functions that read the flag (`check_row_class`, `check_row_glyph`, `check_row_label`, `cli._row_marker`), so the claim is checkable by grep rather than taken on trust, and each says that the state stays `OK` precisely so D-01's exit-code gate does not go red for a probe nobody took.

### Task 2 — the strip (`a19a24e` RED, `a840b05` GREEN)

`_build_templates` registers the three new filters bound to the shared functions themselves. The results branch of `partials/checks.html` now calls `check_row` with `c | check_row_class`, `c | check_row_glyph`, `c | check_row_label`; the macro's parameter list and body are unchanged, and its new comment says the three parameters mean "what this row renders", not "what its state renders". The cold branch is untouched — `_CheckingRow` is not a `CheckResult` and still hands its own trio in, which is right, because on a cold start a probe really is coming.

The UI-SPEC was amended in four places: the glyph table names U+00B7's second use, the five-row table gained the busy row beside the paused one, a new paragraph records the neutral marker and the "Not checked" screen-reader word with the reason the state cannot be drawn from, and the "templates own no vocabulary" bullet now names the three filters the template actually reaches for.

### Task 3 — `saneless doctor` (`d70e21e` RED, `25dfa19` GREEN)

`_SKIPPED_MARKER: Final = "[SKIP]"` and `_row_marker(result)`, which reads the flag before the state. `doctor`'s row loop calls `_row_marker(result)`; `_state_marker`, its total `match` and its docstring are untouched. `_MARKER_WIDTH` is now computed over the state tokens and the skipped one together.

The comment above `_SKIPPED_MARKER` states plainly that no current `doctor` invocation produces the marker — the command builds its `CheckContext` with `skip_scanner` defaulted and passes no scanner gate — and gives both reasons it exists anyway: D-02's claim that one registry feeds both surfaces, and the fact that leaving this very flag half-wired is what produced the finding.

### Browser measurement (`180aac3`)

`tests/test_browser.py`'s existing paused-strip test already drives a genuine skipped Scanner row through a real worker holding the scanner gate, which makes it the one place the defect is observable in a browser rather than in markup. It now also asserts the glyph carries `check-checking` and not `check-ok`, renders U+00B7, that the row contains no tick, and that the screen-reader word is `Not checked:` and not `OK:`.

## Mutation evidence

Every mutation named in the plan's acceptance criteria was applied, run and reverted by hand.

| Task | Mutation applied | Result |
|---|---|---|
| 1 | remove the `if result.skipped:` guard from all three `check_row_*`, so each delegates unconditionally | **6 failed**, 10 passed — `test_a_skipped_row_is_neutral_whatever_its_state[OK,WARN,FAIL]`, `test_a_skipped_row_never_renders_the_ok_marker[OK]`, `test_the_two_skipped_rows_render_neutral[_scanner_skipped,_scanner_busy]`. All three functions are covered: the neutral-trio case asserts class, glyph and label together |
| 2 | revert the template's three call-site filters to `c.state \| check_state_*` | **6 failed**, 122 passed — the three `test_the_template_draws_no_row_from_a_state_alone` cases plus `test_a_skipped_row_renders_the_neutral_marker`, `test_a_skipped_row_carries_no_ok_marker` and `test_the_skipped_row_authors_no_vocabulary_of_its_own` |
| 2 (browser) | the same mutation, against Chromium | **1 failed** — and the assertion message shows the defect itself: the paused Scanner row's glyph attribute read `check-glyph check-ok` |
| 3 | remove the `if result.skipped:` guard from `_row_marker` | **4 failed**, 32 passed — `test_a_skipped_row_gets_the_skipped_marker[OK,WARN,FAIL]` and `test_the_printed_table_marks_the_skipped_row` |

Each mutation was reverted with `git checkout -- <the one file>` and the suite re-run green before proceeding.

## Verification

| Check | Result |
|---|---|
| `uv run pytest tests/test_checks.py tests/test_web_checks.py tests/test_doctor.py -q` | pass |
| `uv run pytest -q` | **3156 passed** |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | all hooks pass |
| `grep -rn "skipped" src/saneless/web/templates src/saneless/cli.py` | **8 matches in 2 files** — the grep that returned nothing when round 3 ran it |

Acceptance greps: `check_row_*` in `checks.py` → 9 (plan floor 6); `SKIPPED_STATE_LABEL` in `checks.py` → 5 (floor 2); `check_state_*` in `checks.html` → **0**; `check_row_` in `app.py` → 6 (floor 3); `_SKIPPED_MARKER|_row_marker` in `cli.py` → 9 (floor 4); `_state_marker(result.state)` inside `doctor`'s row loop → **none** (the two remaining matches are inside `_row_marker` itself and its docstring). `_MARKER_WIDTH` is 6, equal to `len(_SKIPPED_MARKER)`.

## Deviations from Plan

### 1. [Rule 2 — missing critical coverage] `tests/test_web.py`'s filter-registration tests extended

- **Found during:** Task 2
- **Issue:** The plan's action requires the three new filters to be bound "to the shared functions themselves and never to wrappers -- the same rule the existing registrations state". The only thing that makes that rule checkable is `test_each_filter_is_the_shared_implementation` in `tests/test_web.py`, which was not in the plan's `files_modified`. Registering three filters that no test pins by identity is the same half-wiring this plan exists to close.
- **Fix:** Added the three names to `test_registers_the_eight_new_filters` (renamed `..._eleven_new_filters`) and three `is` assertions to `test_each_filter_is_the_shared_implementation`.
- **Files modified:** `tests/test_web.py`
- **Commits:** `a19a24e` (RED), `a840b05` (GREEN)

### 2. [Rule 2 — CLAUDE.md-mandated browser validation] `tests/test_browser.py` extended

- **Found during:** after Task 3
- **Issue:** Project CLAUDE.md requires all web-rendering changes to be validated via a real browser and forbids marking such checks manual-only. The plan scoped verification to pytest and the type checkers.
- **Fix:** Extended the existing `test_a_scan_in_flight_pauses_the_scanner_row_and_says_so`, which already produces a genuine skipped row via a real worker holding the scanner gate, with glyph-class, glyph-text and screen-reader-word assertions. Proved it is a real measurement by re-running it under the task 2 mutation, where it fails showing `check-glyph check-ok`.
- **Files modified:** `tests/test_browser.py`
- **Commit:** `180aac3`

### 3. [Rule 1 — my own test bug] `.sr-only` count scoped to its span

- **Found during:** Task 2 GREEN
- **Issue:** `test_the_skipped_row_authors_no_vocabulary_of_its_own` asserted `row.count(SKIPPED_STATE_LABEL) == 1` and got 2. Not a defect in the implementation: UI-SPEC S1's sentence for this row is "Not checked while a scan is running.", which begins with the label's own two words.
- **Fix:** Count `<span class="sr-only">Not checked:</span>` rather than the bare phrase, with the coincidence recorded in the test's docstring so the next reader does not "fix" it back.
- **Commit:** `a840b05`

### 4. [Rule 3 — blocking] `Final` was not imported in `cli.py`

- **Found during:** Task 3 GREEN
- **Issue:** `_SKIPPED_MARKER: Final = "[SKIP]"` raised ruff `F821 Undefined name 'Final'`; `cli.py` imported only `TYPE_CHECKING, assert_never` from `typing`.
- **Fix:** Added `Final` to the existing `typing` import. `CheckResult` was likewise added to the existing `.checks` import for `_row_marker`'s annotation.
- **Commit:** `25dfa19`

### 5. [Documented, not fixed] one acceptance grep returns 4 where the plan predicted 3

- `grep -c "check_row_class\|check_row_glyph\|check_row_label" src/saneless/web/templates/partials/checks.html` returns **4**, not 3. Three are the call site; the fourth is the header comment, which names all three on one line because the plan's own action text requires the comment to explain the substitution and because naming them is what makes the claim greppable — the same discipline applied to `_scanner_skipped`'s docstring in task 1. The criterion's intent (all three filters used, none of the state filters) holds and is pinned by two parametrised tests rather than by a count.

## Threat Flags

None. The plan's register is unchanged and its dispositions hold:

- **T-30-34-01 (Spoofing, mitigated):** the strip cannot claim a verified-healthy scanner it never looked at — pinned by `test_a_skipped_row_carries_no_ok_marker` and by the Chromium assertion.
- **T-30-34-02 (Repudiation, mitigated):** `doctor` prints `[SKIP]`, so a captured transcript records what was actually checked.
- **T-30-34-03 (DoS on the exit code, accepted):** unchanged and now pinned — `test_a_skipped_row_does_not_fail_the_gate` asserts exit 0 with a skipped row present, and `worst_state` was not touched.
- **T-30-34-04 (Information disclosure, accepted):** `SKIPPED_STATE_LABEL` and `_SKIPPED_MARKER` are developer-authored constants with no host, path, URL or exception text (ASVS V7).
- **T-30-34-SC (Tampering, accepted):** no packages installed; `uv.lock` untouched.

## Known Stubs

None. Every function this plan added is reached by a test, including `_row_marker`, whose production caller is unreachable today — it is exercised directly at the seam and through a `run_checks` stub, which is the arrangement the plan's decision record calls for.

## Out of scope, as planned

`CheckState` gained no fourth member; "we did not look" remains a flag. The skipped rows' message wording is UI-SPEC S1's and is unchanged. `routes.py` was not edited — the cold branch still passes `_CheckingRow`'s own attributes.

## Self-Check: PASSED

- `src/saneless/checks.py`, `src/saneless/cli.py`, `src/saneless/web/app.py`, `src/saneless/web/templates/partials/checks.html`, `tests/test_checks.py`, `tests/test_web_checks.py`, `tests/test_doctor.py`, `tests/test_web.py`, `tests/test_browser.py`, `.planning/phases/30-appliance-layer/30-UI-SPEC.md` — all present.
- Commits `f98c629`, `31eba84`, `a19a24e`, `a840b05`, `d70e21e`, `25dfa19`, `180aac3` — all present in `git log`.
- `git diff --diff-filter=D --name-only 8443f7e..HEAD` is empty: no file was deleted.
- STATE.md and ROADMAP.md were not modified (worktree mode; the orchestrator owns them).
</content>
</invoke>
