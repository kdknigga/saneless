---
phase: 30-appliance-layer
plan: 08
subsystem: cli
tags: [doctor, check-registry, exit-codes, doc-truth, assert-never, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "checks.py — CheckContext, CheckKey, CheckState, check_name, run_checks, worst_state (plan 30-06)"
  - phase: 30-appliance-layer
    provides: "ProfileStorage (plan 30-01), is_placeholder_token (plan 30-02)"
  - phase: 28-cli-errors
    provides: "_GuardedGroup, ExitCode and the D-07 exit-code table the doctor section extends"
provides:
  - "`saneless doctor` — the sixth command, and the CLI half of success criterion 1"
  - "_state_marker(CheckState) -> str — the three fixed-width bracket markers, a total match with assert_never"
  - "_MARKER_WIDTH / _NAME_COL_WIDTH / _NEXT_STEP_INDENT — derived column widths, never written down"
  - "_doctor_scanner / _doctor_paperless — the two failure-collapsing builders that keep doctor inside its documented exit set"
  - "docs/reference/cli-commands.md `## `saneless doctor`` with its **Exit codes:** table"
  - "a doc-truth test that derives the documented command count from the section headings"
affects: [30-11, 30-12]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "collapse-at-the-boundary: a command builds its collaborators through small helpers that turn every construction failure into the None the registry already has a row for, so the command's exit set stays equal to its documented table"
    - "derive-a-count doc-truth test: parse the number word out of the prose and compare it against the sections actually present, so the next command is caught by the same assertion as this one"
    - "a guard test for a decision not taken: assert the absence of `doctor --json` in both documents, so 'we chose not to ship it' cannot rot into a doc that promises it"

key-files:
  created:
    - tests/test_doctor.py
  modified:
    - src/saneless/cli.py
    - tests/test_deployment_config.py
    - docs/reference/cli-commands.md
    - docs/how-to/cli-scripting.md

key-decisions:
  - "A failed SaneBackend construction collapses to scanner=None for all three of its shapes — ImportError, require_sane's ConfigError and sane.init's ScanError — rather than the ImportError alone the plan named. require_sane translates the ImportError before SaneBackend.__init__ returns, so catching only ImportError would have produced exactly the refusal Amendment A-1 forbids"
  - "PaperlessError from PaperlessClient.__init__ collapses to paperless=None. Letting it out would exit 3, a code doctor's documented table does not list, and would cost the operator the other four rows to report a fact the Paperless row already has a sentence for"
  - "The bracket markers are a cli.py lookup (_state_marker), not check_state_label. That function answers 'what does a screen reader announce' and the answer is a word; the marker's job is a scannable fixed-width left margin"
  - "doctor is placed between jobs and serve in the reference, next to the other command that does not need python-sane"
  - "30-06's raising-check copy is confirmed unchanged: the UI-SPEC has no canonical sentence for it, and the invented copy is already in S1's voice"

patterns-established:
  - "A CLI command whose documented exit set is narrower than the group guard's must collapse its collaborators' construction failures itself; the guard cannot know that exit 3 is wrong for this command"
  - "Ruff S107 is not among the rules tests waive (only S105/S106 are), so a literal default on a parameter named `token` must be bound to a name with no password-ish word in it first"

requirements-completed: [APPL-01, APPL-07]

# Metrics
duration: 16min
completed: 2026-09-16
---

# Phase 30 Plan 08: `saneless doctor` Summary

**The sixth command reads the same registry the index page will, prints one fixed-width row per `CheckKey` with the next step indented under every amber and red one, and exits 2 on a failure and 0 on a warning — with no new `ExitCode` member and no `--json`.**

## Performance

- **Duration:** 16 min
- **Started:** 2026-09-16T17:26:00Z
- **Completed:** 2026-09-16T17:42:01Z
- **Tasks:** 2 (both TDD, four commits)
- **Files modified:** 5 (1 created, 4 modified)

## Accomplishments

- `saneless doctor` exists and runs against a real scanner. On this machine, with a placeholder token exported, it prints five aligned rows, names the unset token, and exits 2:

  ```
  [ OK ] Scanner     Hewlett-Packard hp_LaserJet_3030 is ready.
  [FAIL] Paperless   The paperless-ngx API token has not been set.
                     Put a real API token in the saneless config file, then restart saneless.
  [WARN] Profiles    Generated in memory — no configuration file is in use, so they are lost on restart.
                     Create a saneless config file so the profiles are saved.
  [WARN] Fallback    Not configured; scans cannot be kept if paperless-ngx is down.
                     Set a fallback folder in the saneless config so scans are kept when paperless-ngx is down.
  [ OK ] Data folder The data folder is writable.
  ```

- **D-01's mapping is tested per state, not asserted by inspection.** A warning exits 0; a failure exits 2; and a separate test pins `{int(c) for c in ExitCode} == {0, 1, 2, 3, 4, 5, 130}` so a future author who reaches for a sixth code breaks this file as well as the two doc-truth tests.
- **Amendment A-1 holds against the real failure shape, not the one the plan named.** See Deviations — the plan's `except ImportError` would not have caught a missing python-sane, because `require_sane` has already turned it into a `ConfigError` by the time `SaneBackend.__init__` returns. Two tests cover both shapes.
- **`doctor`'s documented exit set is true rather than aspirational.** Both collaborators it constructs can refuse — `SaneBackend` with `ScanError`, `PaperlessClient` with `PaperlessError` — and the group guard would have mapped those to 1 and 3, two codes the section does not list. Both are collapsed to the `None` the registry already has a row for, and there is a test for each.
- **The command count in the reference is no longer a number somebody has to remember to change.** The new doc-truth test parses the word out of the prose and compares it against the `## `saneless <cmd>`` headings present, so the seventh command fails the same assertion.
- All four gates clean with zero suppressions added, and the full non-browser suite is **2410 passed, 74 deselected** (2385 before this plan, plus 22 new `doctor` tests and 3 new doc-truth tests).

## Task Commits

1. **Task 1: the doctor command** — `17aba36` (test, RED: 20 failed, 2 guard tests passing) → `8c570e6` (feat, GREEN: 22 passed)
2. **Task 2: the reference section and the doc-truth tests** — `89201be` (test, RED: 2 failed) → `0f440b0` (docs, GREEN: 32 passed)

No REFACTOR commits were needed.

## Files Created/Modified

- `tests/test_doctor.py` (new, 594 lines, 22 tests) — five classes: help and the absent `--json`, the exit-code mapping per state, the rendering, the two end-to-end runs through the real registry, and the two "stays inside its documented exit set" cases.
- `src/saneless/cli.py` (+197) — `_state_marker`, the three derived widths, `_doctor_scanner`, `_doctor_paperless` and the `doctor` command. Three imports grew: `.checks`, `PaperlessError`, `ProfileStorage`, and `ScannerBackend` under `TYPE_CHECKING`.
- `tests/test_deployment_config.py` (+82) — the `doctor` row in `test_cli_reference_command_exit_codes_are_real` with its reasoning in the docstring, plus `test_cli_reference_command_count_matches_its_sections`, `test_doctor_promises_no_json_mode` and `test_scripting_does_not_claim_every_read_command_has_json`.
- `docs/reference/cli-commands.md` (+64) — the `doctor` section and the corrected count sentence.
- `docs/how-to/cli-scripting.md` (+21) — the corrected `--json` claim, a `doctor` exit-code subsection, and a worked example that gates a scan on it.

## The 30-06 copy reconciliation (requested by 30-06's summary)

30-06 authored `_CHECK_FAILED_MESSAGE` (`"This check could not be completed."`) and `_CHECK_FAILED_NEXT_STEP` (`"Restart saneless, then press Check again."`) for a check that raises, because its own plan required the row but supplied no words, and it asked 30-08 to confirm them against the UI-SPEC.

**Outcome: confirmed as written; `checks.py` is unchanged by this plan.**

- 30-UI-SPEC § S1's five-rows table has **twenty** message/next-step pairs and **none** of them is for a check that raised. The only `*(any)*` row is the cold-start `Checking…` one. A grep across every file in the phase directory for the sentence, or for any prose about what a raising check should say, finds only 30-06's own two files.
- § S2's `UNKNOWN` pair (`Something went wrong.` / `Start the scan again. If it keeps failing, check the saneless log.`) is the nearest thing in the tree, and it is deliberately **not** the right copy here: it is an `ErrorCategory` about a scan job that failed, its remedy is to re-run the scan, and it points at the log file — which D-13 keeps off this surface.
- The invented copy is already in S1's voice. `press Check again` appears in three of S1's own next steps (Scanner FAIL, Paperless SERVER_ERROR, Paperless UNREACHABLE) and `restart saneless` in four, so the compound is not a new register. It is path-free, URL-free and exception-free, which is the constraint that actually matters.

`doctor` renders it unchanged, so if the UI-SPEC's author ever supplies a canonical sentence, one edit in `checks.py` still moves both surfaces.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug in the plan's own instruction] `except ImportError` would not have caught a missing python-sane**

- **Found during:** Task 1, writing `_doctor_scanner`.
- **Issue:** The plan says "Obtain the scanner backend inside a `try`/`except ImportError` and pass `None` into the `CheckContext` on failure." But `SaneBackend.__init__` calls `require_sane()` for itself (`sane_backend.py:1961`), and `require_sane` catches the `ImportError` and re-raises it as a `ConfigError` carrying the install hint. An `except ImportError` around the constructor therefore catches nothing on the one machine the code exists for: the `ConfigError` reaches the group guard, which prints `scan`'s refusal line and exits 2 with **no rows at all** — the precise behaviour Amendment A-1 exists to prevent, arrived at through the code written to implement it.
- **Fix:** `_doctor_scanner` catches `(ImportError, ConfigError, ScanError)`. `ImportError` covers a hypothetical future path that does not route through `require_sane`; `ConfigError` is the real missing-python-sane shape; `ScanError` is `sane.init()` refusing.
- **Files modified:** `src/saneless/cli.py`, `tests/test_doctor.py`
- **Verification:** `test_a_missing_python_sane_still_prints_five_rows` (the `ImportError` shape) and `test_a_refusing_require_sane_still_prints_five_rows` (the `ConfigError` shape, which additionally asserts `"python-sane cannot be imported"` never reaches stderr).
- **Committed in:** `17aba36`, `8c570e6`

**2. [Rule 2 — Missing critical: a documented contract the code could break] `ScanError` and `PaperlessError` had to be collapsed too**

- **Found during:** Task 1, reconciling the command body against Task 2's `{0, 2, 5, 130}`.
- **Issue:** `doctor` constructs two collaborators that can refuse, and the group guard maps both refusals onto codes the new section does not list. `SaneBackend` raises `ScanError` when `sane.init()` fails → `classify_error` → `SCANNER` → **exit 1**. `PaperlessClient.__init__` raises `PaperlessError` for a URL httpx will not parse (`paperless.py:487-489`) → **exit 3**. Either would have made the `**Exit codes:**` table false the first time it happened, on a machine whose owner is already having a bad day, and would have replaced five diagnostic rows with one refusal.
- **Fix:** Both collapse to the `None` the registry already has a documented row for. `paperless=None` is `checks.py`'s own NOT_FOUND case ("The paperless-ngx API was not found at that URL."), which is exactly what an unparseable URL means. `scanner=None` renders "Scanner support is not installed on this machine." — which for a libsane that will not initialise is both the honest reading (this machine does not have working SANE support) and the right advice (reinstall it). Both choices are argued in the helpers' docstrings rather than left to be rediscovered.
- **Files modified:** `src/saneless/cli.py`, `tests/test_doctor.py`
- **Verification:** `test_an_unbuildable_paperless_client_does_not_exit_three` asserts five rows, the NOT_FOUND sentence and `ExitCode.CONFIG`.
- **Committed in:** `8c570e6`

**3. [Rule 1 — Bug: a doc claim this plan falsified] "All read commands support `--json`"**

- **Found during:** Task 2.
- **Issue:** `docs/how-to/cli-scripting.md:12` opened its JSON section with "All read commands support `--json` for machine-parseable output". `doctor` is a read command and deliberately has no `--json`, so shipping it made a documented sentence false — and false in the direction that would send a script author looking for a flag, finding a usage error, and filing a bug.
- **Fix:** The sentence now names `devices` and `jobs`, and a following sentence says `doctor` has no JSON mode and links to the gating example. `test_scripting_does_not_claim_every_read_command_has_json` refuses the old blanket claim.
- **Files modified:** `docs/how-to/cli-scripting.md`, `tests/test_deployment_config.py`
- **Committed in:** `89201be`, `0f440b0`

**4. [Rule 3 — Blocking] Ruff `S107` on the test helper's token default**

- **Found during:** Task 1 RED.
- **Issue:** `tests/**` waives `S101`, `ARG`, `S104`, `S105` and `S106` — but **not** `S107` (hardcoded password assigned to a function default). `def _make_settings(..., token: str = "real-token-value", ...)` tripped it, and CLAUDE.md forbids `# noqa` and forbids widening the ignore list.
- **Fix:** The named-literal-alias idiom from `vocabulary.py`'s `_REJECTED_WIRE_VALUE`: the literal is bound to `_NOT_A_PLACEHOLDER`, a name with no password-ish word in it, and the parameter defaults to that name. S105/S106/S107 all read the *name*, so a `Name` default is not a literal default and the rule does not fire.
- **Files modified:** `tests/test_doctor.py`
- **Committed in:** `17aba36`

### Small additions beyond the literal task text

- **A "checking readiness before a scan" example** in the scripting how-to. The plan asked for a paragraph on the exit semantics; a three-line `if ! saneless doctor` script is what a reader of that page came for, and it makes the ok/warn/fail mapping concrete.
- **`test_the_token_value_is_never_printed`.** A guard rather than a new requirement: `doctor` is the one command that reads the token and prints a report in the same breath, so the assertion that the configured value appears in neither stream belongs beside it (ASVS V7, T-30-31).
- **The `doctor` section names the `HEALTHCHECK` reason in prose**, as the plan asked, and the scripting how-to repeats it — an operator reading either page in isolation should not wire `doctor` to a healthcheck.

## Issues Encountered

- **The worktree spawned on the wrong base, for the third wave running.** `git merge-base HEAD fd3f631` returned `a87b3dd` and `HEAD` was at `ed2d620` ("Merge pull request #1"), so the startup guard's `git reset --hard` fired for real. This guard has now been load-bearing in three consecutive waves; it must not be dropped.
- **`grep -A N` through a pipe returned 0 for a marker that is demonstrably present** in the range, twice, while `sed -n 'a,bp' | grep -c` on the same range returned the right answer. The doc-truth test is the authority here and it passes; the acceptance greps that use `-A` should be read with suspicion in this environment.
- **`require_sane` now appears 7 times in `cli.py`, up from 5**, which reads like a violation of the acceptance criterion "count is unchanged". It is not: the two new matches are a docstring sentence in `_doctor_scanner` and the Amendment A-1 comment above the command, both of which exist to explain that `doctor` does *not* call it. The call sites are unchanged at four (lines 530, 662, 782, 892), none of them in `doctor`.

## Verification

All plan gates pass from the worktree:

- `uv run pytest tests/test_doctor.py -q` — 22 passed (the plan asked for at least 10)
- `uv run pytest tests/test_deployment_config.py -q` — 32 passed
- `uv run pytest tests/test_cli.py tests/test_checks.py -q` — 228 passed, nothing in the existing CLI suite disturbed
- `uv run pytest -m "not browser and not sane_hardware" -q` — **2410 passed, 74 deselected**
- `uv run ruff check .` / `uv run ruff format --check .` / `uv run ty check` / `uv run pyrefly check src tests` — all clean
- `SANELESS_PAPERLESS__TOKEN=changeme uv run saneless doctor` — five rows, exit **2**
- `uv run saneless doctor --help` — exit 0, shows the docstring; `uv run saneless doctor --json` — `Error: No such option: --json`, exit 2
- Acceptance greps: `def doctor(ctx: click.Context) -> None:` ×1, `ctx.exit(ExitCode.CONFIG)` present inside `doctor`, `_NAME_COL_WIDTH` ×3 (one definition, two uses), no `as_json` anywhere in `doctor`, `^## `saneless doctor`` ×1, `five commands` ×0, `doctor --json` ×0 in both documents
- `uv run python -c "from saneless.vocabulary import ExitCode; assert {int(c) for c in ExitCode} == {0,1,2,3,4,5,130}"` — exits 0
- RED precedes GREEN in both tasks; no `--no-verify`, no `SKIP=`, no suppression comment added anywhere

## Known Stubs

None. `doctor` is wired to the real `run_checks`, the real `SaneBackend` and the real `PaperlessClient`; nothing returns a hardcoded value and no row is a placeholder. The two builders return `None` on failure, which is the registry's documented input for "there is no such collaborator", not an empty stand-in.

## Threat Flags

None. Everything this plan touches is in the plan's register. T-30-31 (information disclosure through printed rows) is mitigated as written — every message and next step comes from `checks.py`, `doctor` adds only the bracket marker and the padded name — and the two new log lines it introduces log `type(exc).__name__` only, deliberately, because the `ConfigError`'s message names an install path and the `PaperlessError`'s names a URL that may carry `user:pass@`.

## Next Phase Readiness

- **30-11 (status strip)** can now rely on `run_checks` having a second caller: any change to `CheckContext`'s shape breaks `cli.py` at type-check time, which is the point of D-02.
- **30-12 (docs/verification)** gets `docs/reference/cli-commands.md`'s `doctor` section already pinned by three doc-truth tests, and a command-count assertion that no longer needs updating by hand.
- **Open for whoever owns the UI-SPEC:** if a canonical sentence for a check that raised is ever written, it replaces `_CHECK_FAILED_MESSAGE` / `_CHECK_FAILED_NEXT_STEP` in `checks.py` and both surfaces follow. Nothing in this plan hard-codes either string.

## Self-Check: PASSED

- `tests/test_doctor.py` — FOUND
- `src/saneless/cli.py` — FOUND, contains `def doctor(ctx: click.Context) -> None:`
- `docs/reference/cli-commands.md` — FOUND, contains `## `saneless doctor``
- `docs/how-to/cli-scripting.md` — FOUND, mentions `doctor`
- `tests/test_deployment_config.py` — FOUND, contains the `doctor` exit-code assertion
- Commits `17aba36`, `8c570e6`, `89201be`, `0f440b0` — all FOUND in `git log`
- `STATE.md` and `ROADMAP.md` not modified (the orchestrator owns those writes)

---
*Phase: 30-appliance-layer*
*Plan: 08*
*Completed: 2026-09-16*
