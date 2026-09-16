---
phase: 30-appliance-layer
plan: 10
subsystem: cli
tags: [click, guarded-group, exit-codes, secrets, local-time, derived-widths, doc-truth, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "error_next_step, local_time, LOCAL_TIME_FORMAT (plan 30-01)"
  - phase: 30-appliance-layer
    provides: "is_placeholder_token and PLACEHOLDER_TOKENS (plan 30-02)"
  - phase: 30-appliance-layer
    provides: "the doctor command that shares cli.py (plan 30-08)"
  - phase: 28-cli-errors
    provides: "_failure_line's D-08 shape, _GuardedGroup.invoke and the D-07 exit-code table"
provides:
  - "the `Try: <next step>` second stderr line on every classified saneless failure (APPL-04, D-12)"
  - "`saneless scan`'s placeholder-token refusal, before the scanner is opened (APPL-07, D-16)"
  - "_UNSET_CREDENTIAL_PROBLEM -- the developer constant naming the unset token on the CLI"
  - "_TIME_COL_WIDTH / _WIDEST_ZONE_TOKEN -- the derived Timestamp column, replacing the literal 22"
  - "resolve_job_title's local-zone fallback title, closing RESEARCH Open Question 4"
  - "_failure_lines(result) -- the test helper that asserts and strips the advice line"
affects: [30-11, 30-12, 30-13]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "additive-second-line: a locked output line is printed unchanged and the new information goes on a line after it, so both the scripts parsing it and the doc-truth tests pinning it survive"
    - "assert-and-strip test helper: when a deliberate change adds a line to every failure, one helper asserts the new line and returns the old shape, instead of ten assertions each being loosened to a substring check"
    - "derive-a-width from a rendered sample plus a documented worst case: the fixed half is measured from real output, only the variable half (the %Z token) carries a literal, and max() keeps a wider host from truncating"
    - "counting stub as the assertion: 'before the scanner is opened' is proved by a scanner class that records its own construction, not inferred from an exit code"

key-files:
  created: []
  modified:
    - src/saneless/cli.py
    - src/saneless/config.py
    - tests/test_cli.py
    - tests/test_config.py
    - tests/test_deployment_config.py
    - docs/how-to/configure-scan-profiles.md
    - docs/how-to/cli-scripting.md
    - docs/reference/cli-commands.md

key-decisions:
  - "The advice line is added only in the SanelessError arm's non-UNEXPECTED branch. _report_unexpected, the StorageError arm and both cancel arms print exactly what they printed before (D-12)"
  - "The token guard sits after resolve_job_title rather than immediately after _load_cli_settings, because ErrorCategory.CONFIG renders the exception verbatim and the plan's required message shape names the resolved title. It is still strictly before SaneBackend is constructed, which is what D-16 is about"
  - "Ten Phase 28 one-line stderr assertions were retargeted at a _failure_lines helper rather than relaxed, so each still means 'one failure line and no traceback'"
  - "_TIME_COL_WIDTH derives the fixed date/time half from a rendered sample and adds the documented five-character worst-case zone token, then max()es against the host's own rendering. It comes to 22 -- the reserve the dropped seconds used to occupy is exactly the reserve the zone now needs"
  - "config.py lost its `from datetime import UTC` import entirely: resolve_job_title was its only user"
  - "docs/how-to/configure-scan-profiles.md keeps the literal `Scan <date time>` placeholder and gains the zone sentence, because test_profile_howto_title_is_literal pins `Scan <`"

patterns-established:
  - "When a plan deliberately changes a line every failure prints, retarget the existing assertions through one helper that asserts the new line -- the old assertions keep their meaning and the new line gains ten more witnesses"
  - "A width that depends on the host's tz database is derived from a rendered sample for its fixed part and a named worst-case constant for its variable part"

requirements-completed: [APPL-04, APPL-07, APPL-12]

# Metrics
duration: 41min
completed: 2026-09-16
---

# Phase 30 Plan 10: CLI Advice, Refusal and Local Time Summary

**A second stderr line carries the category's next step without touching Phase 28's locked first line, `saneless scan` refuses a placeholder paperless-ngx token before the scanner is ever constructed, and both the `jobs` table and the generated document title now read the one shared `local_time` formatter while `jobs --json` stays UTC ISO-8601.**

## Performance

- **Duration:** 41 min
- **Started:** 2026-09-16T18:32Z
- **Completed:** 2026-09-16T19:13Z
- **Tasks:** 3 (6 commits: 3 RED, 3 GREEN)
- **Files modified:** 8

## Accomplishments

### Task 1 — the `Try:` line (APPL-04, D-12)

`_GuardedGroup.invoke` now echoes `Try: {error_next_step(category)}` to stderr immediately after `_failure_line`. Verified end to end on this machine:

```
Scanning 'x' with profile 'default': the paperless-ngx API token has not been set
Try: Correct the saneless configuration file, then restart saneless.
```

- `_failure_line` and its call site are byte-for-byte untouched — `git diff src/saneless/cli.py | grep -c "^-.*_failure_line"` is **0**, and a parametrised test asserts line 1 still equals `_failure_line(exc, category)` for all five advised categories.
- The three branches D-12 leaves alone are each pinned by a test that asserts no `Try: ` line appears: `_report_unexpected` (bare `SanelessError` and a non-saneless `RuntimeError`, both exit 5), the `StorageError` arm (exit 2) and the two cancel arms (exit 130).
- Exit codes per category are asserted against `exit_code_for` itself, not a second copy of the table.
- 23 tests selected by `-k advice` (the plan asked for ≥9), all passing.

### Task 2 — the scan token refusal (APPL-07, D-16)

`scan` raises a `ConfigError` through the group guard when `is_placeholder_token` says the configured token is blank, whitespace or a member of the literal placeholder set.

- **"Before the scanner is opened" is proved, not assumed.** The tests use a `StubScannerBackend` subclass that appends to a list on construction and on `get_devices`, plus a `PaperlessClient` stand-in that raises `AssertionError` if it is ever built. The list is asserted empty.
- The refusal is unconditional: a configured `paperless.consume_dir` has its own test and does not soften it.
- `devices`, `jobs` and `auto-profiles` each have a test proving they still exit 0 with the same placeholder token configured.
- The token value reaches `is_placeholder_token` and nothing else — a test asserts the configured value appears in neither stdout nor stderr.
- Real run: `SANELESS_PAPERLESS__TOKEN=changeme uv run saneless scan --profile default --title x` → the two lines above, **exit 2**.
- 11 tests selected by `-k placeholder` (the plan asked for ≥6).

### Task 3 — local time (APPL-12, D-34, D-35, RESEARCH Open Question 4)

- `jobs` renders `local_time(j.created_at)`; `saneless.cli.local_time is saneless.vocabulary.local_time` is asserted, so the CLI and the web filter cannot drift.
- `ts_w = 22` is gone. `_TIME_COL_WIDTH` derives the fixed `2026-09-16 14:03` half from a rendered sample and adds one space plus `_WIDEST_ZONE_TOKEN` (`len("+0545")`), then `max()`es that against the host's own rendering so a wider abbreviation cannot truncate. It evaluates to **22**, and `title_w` at 80 columns is still **24** — measured from the rendered header by a test, not recomputed from the constants.
- `jobs --json` is untouched and now carries a comment saying why, plus two tests: `created_at` ends with `+00:00` and round-trips to the same instant, and the local rendering never appears in the JSON output.
- `resolve_job_title`'s fallback is `f"Scan {local_time(now)}"`. Its docstring's "rendered in UTC whatever the zone of `now`" sentence is replaced by one naming the shared function, the three surfaces it keeps in step, and the fact that the string is user-facing twice (paperless-ngx title, History Title cell). `astimezone(UTC)` is gone from `config.py`, and so is the now-unused `from datetime import UTC`.
- Real run at 80 columns:

  ```
  Timestamp              Profile         Title                    Status
  ----------------------------------------------------------------------
  2026-09-11 10:16 CDT   default         Render Test              Uploading
  ```

## Task Commits

RED before GREEN in every task:

1. **Task 1: the `Try:` line** — `4cc1dea` (test, RED: 13 failed / 10 passed) → `5db5061` (feat, GREEN)
2. **Task 2: the scan token refusal** — `5e0e21e` (test, RED: 7 failed / 4 passed) → `4ae27e7` (feat, GREEN)
3. **Task 3: local time** — `fa64854` (test, RED: 12 failed) → `929f96a` (feat, GREEN)

No REFACTOR commits were needed; each GREEN landed clean under all four gates.

## TDD Gate Compliance

`git log --oneline` shows `test(30-10):` immediately before `feat(30-10):` for all three tasks. Each RED run was confirmed failing on the assertion the task exists to satisfy — never on a collection error masking a different problem, and never forced through with `--no-verify`, `SKIP=` or a stub. In each RED run the regression guards (the branches D-12 leaves alone; the three commands a placeholder token must not affect) passed from the start, which is what makes them guards rather than new behaviour.

## Files Created/Modified

- `src/saneless/cli.py` (+87 net) — `error_next_step`, `local_time` and `is_placeholder_token` imports; `_UNSET_CREDENTIAL_PROBLEM`; `_WIDEST_ZONE_TOKEN`; `_TIME_COL_SAMPLE` / `_TIME_COL_WIDTH`; the advice echo in `_GuardedGroup.invoke`; the token guard in `scan`; `local_time(j.created_at)` and `ts_w = _TIME_COL_WIDTH` in `jobs`; the `--json` comment.
- `src/saneless/config.py` — `local_time` import, the rewritten `resolve_job_title` docstring paragraph and its one-line fallback; `from datetime import UTC` removed.
- `tests/test_cli.py` (+~450) — `_failure_lines`, `_scan_raising`, `_refusing_paperless`, `_open_counting_scanner`, `_token_settings`, the `cli_local_zone` fixture, `_jobs_header_title_width`; classes `TestFailureAdvice`, `TestScanTokenRefusal`, `TestJobsTableWidth`, `TestJobsJsonContract`; ten existing assertions retargeted at `_failure_lines`. 137 → 178 tests.
- `tests/test_config.py` — the `config_local_zone` fixture and `TestResolveJobTitle` retargeted at the local zone through `local_time` itself.
- `tests/test_deployment_config.py` — `test_scripting_documents_jobs_json_created_at_as_utc`.
- `docs/how-to/cli-scripting.md` — the `created_at` example now carries `+00:00`; a new paragraph stating the JSON stays UTC while the table goes local; the generated-title sentence names the zone.
- `docs/how-to/configure-scan-profiles.md` — the `title` row's fallback sentence names the local zone.
- `docs/reference/cli-commands.md` — the `--title` default; two new `scan` paragraphs (the generated title and its `TZ` dependency; the token refusal); the placeholder token added to `scan`'s exit-2 causes; the `jobs` section's local-table-vs-UTC-JSON sentence.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking] Ten Phase 28 assertions asserted a one-line stderr**

- **Found during:** Task 1 (GREEN).
- **Issue:** 18 tests failed the moment the advice line shipped. Every one was a Phase 28 assertion of the form `assert len(result.stderr.splitlines()) == 1` or `assert result.stderr.splitlines() == [...]`, across `TestRequireSane`, `TestServeCommand`, `TestAutoProfiles` and `TestExitCodes`. The plan anticipated the doc-truth tests in `test_deployment_config.py` (which were indeed unaffected) but not these.
- **Fix:** A `_failure_lines(result)` helper that **asserts** the last stderr line starts with `Try: ` and returns everything before it. The ten call sites changed from `result.stderr.splitlines()` to `_failure_lines(result)` and their assertions are otherwise untouched, so each still means "one failure line, no traceback" — and the advice line gained ten more witnesses instead of ten assertions being weakened to substring checks.
- **Files modified:** `tests/test_cli.py`
- **Committed in:** `5db5061`

**2. [Rule 1 — Bug in the plan's own instruction] The token guard cannot sit "immediately after `_load_cli_settings`"**

- **Found during:** Task 2 (GREEN).
- **Issue:** The plan says to place the guard immediately after `settings = _load_cli_settings(ctx)`, *and* that the message must render as `Scanning '<title>' with profile '<profile>': the paperless-ngx API token has not been set`. Those two are incompatible: `ErrorCategory.CONFIG` makes `_failure_line` return `str(exc)` verbatim, so the "what saneless was doing" half has to be part of the exception message — and `resolved_title` does not exist until `resolve_job_title` has run, three statements later.
- **Fix:** The guard sits immediately after `resolved_title` is computed and before the manual-duplex check and `SaneBackend(...)`. D-16's actual invariant — before the scanner is opened — holds, and the counting stub proves it. The message shape is exactly the one the plan specified.
- **Files modified:** `src/saneless/cli.py`
- **Committed in:** `4ae27e7`

**3. [Rule 3 — Blocking] `from datetime import UTC` became unused in `config.py`**

- **Found during:** Task 3 (GREEN). `resolve_job_title` was its only consumer; ruff's F401 would have failed the gate.
- **Fix:** Removed the import. `datetime` itself remains a `TYPE_CHECKING`-only import for the `now` annotation.
- **Committed in:** `929f96a`

**4. [Rule 1 — Bug] A doc rewrite broke `test_profile_howto_title_is_literal`**

- **Found during:** Task 3 (GREEN), docs step. The first rewrite of `configure-scan-profiles.md` replaced the `Scan <date time>` placeholder with a concrete example, and a Phase 28 doc-truth test asserts `"Scan <" in text`.
- **Fix:** The sentence keeps the literal placeholder and appends the zone clause plus a concrete example, so both the assertion and the new information hold. Caught by the suite, not by review.
- **Committed in:** `929f96a`

### Small additions beyond the literal task text

- **The `scan` section of `docs/reference/cli-commands.md` gained two paragraphs** — one for the generated title's zone (and the `TZ` dependency a container operator will otherwise be bitten by), one for the token refusal — and the placeholder token joined `scan`'s exit-2 causes. Task 2 shipped a new, operator-visible exit-2 path; documenting it is Rule 2, and the exit-code doc-truth tests still pass.
- **`test_json_never_carries_the_local_rendering`** beyond the plan's `+00:00` assertion: pinning the offset proves the format, and this proves the *instant* was not localised.
- **`test_title_timestamp_uses_the_shared_formatter`** asserts `resolve_job_title(...) == f"Scan {local_time(now)}"` rather than a second hand-written copy of the format, which is the whole point of D-35.

### Deviation from `files_modified`

`tests/test_deployment_config.py` is not in the plan's `files_modified`, but the plan's Task 3 asks for a doc-truth test on `docs/how-to/cli-scripting.md`. That file already owns the `CLI_SCRIPTING` path constant and the other three scripting doc-truth tests, so the new assertion went there rather than duplicating the path constant into `tests/test_cli.py`.

---

**Total deviations:** 4 auto-fixed (2 blocking, 2 bugs), 3 small additions, 1 file-list deviation.
**Impact on plan:** No scope creep. No dependency added, no suppression introduced, no lint threshold changed, no `noqa`, no `type: ignore`.

## Issues Encountered

- **The worktree spawned on the wrong base for the fourth consecutive wave.** `git merge-base HEAD 3a15bbd` returned `a87b3dd`, so the startup guard's `git reset --hard` fired for real. This guard is load-bearing and must not be dropped.
- **Ruff `SIM300` (Yoda condition) fired on `assert cli_module._TIME_COL_WIDTH >= len(...)`.** Flipped to `assert len(...) <= cli_module._TIME_COL_WIDTH`. Worth knowing: ruff reads `<constant-ish name> >= <call>` as a Yoda comparison in an assert.
- **Serena's write tools were not used at all**; every edit went through `Read`/`Edit`/`Write` or `cat >>` with absolute worktree paths, per the environment note. No main-repo file was touched — `git status` in the worktree was clean of surprises at every commit.

## Verification

Every gate the plan names, run from the worktree after the final commit:

| Gate | Result |
|---|---|
| `uv run pytest tests/test_cli.py -q` | 178 passed |
| `uv run pytest tests/test_config.py tests/test_deployment_config.py -q` | passed (455 total with test_cli.py) |
| `uv run pytest -m "not browser and not sane_hardware" -q` | **2480 passed, 74 deselected** |
| `uv run ruff check --select DTZ .` | All checks passed |
| `uv run ruff check --select DTZ src/saneless/cli.py src/saneless/config.py` | All checks passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 62 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --stage pre-push --all-files` | all hooks passed, both "(full)" type checkers included |

Acceptance-criteria checks:

| Check | Expected | Actual |
|---|---|---|
| `grep -c 'f"Try: {error_next_step(category)}"' src/saneless/cli.py` | 1 | 1 |
| `git diff src/saneless/cli.py \| grep -c "^-.*_failure_line"` | 0 | 0 |
| `grep -c "def _failure_line(exc: SanelessError, category: ErrorCategory) -> str:"` | 1 | 1 |
| `pytest tests/test_cli.py -k advice -q` | ≥9 | 23 passed |
| `pytest tests/test_deployment_config.py -q` | passes | passes |
| `grep -c "is_placeholder_token(" src/saneless/cli.py` | 1 | 1 (inside `scan`) |
| `grep -c "sys.exit" src/saneless/cli.py` | unchanged | 0, and 0 on the base commit |
| `SANELESS_PAPERLESS__TOKEN=changeme uv run saneless scan --profile default --title x` | exit 2 | exit 2 |
| `pytest tests/test_cli.py -k placeholder -q` | ≥6 | 11 passed |
| `grep -c "local_time(j.created_at)" src/saneless/cli.py` | 1 | 1 |
| `grep -c "ts_w = 22" src/saneless/cli.py` | 0 | 0 |
| `grep -c "_TIME_COL_WIDTH" src/saneless/cli.py` | ≥2 | 2 (definition + use) |
| `grep -c "isoformat()" src/saneless/cli.py` | ≥1, in `--json` | 1, in `--json` |
| `grep -c "astimezone(UTC)" src/saneless/config.py` | 0 | 0 |
| `python -c "... c.local_time is v.local_time"` | exit 0 | exit 0 |
| `COLUMNS=80 uv run saneless jobs \| head -1 \| wc -c` | ≤81 | 71 |
| `git log --oneline` RED before GREEN | yes | yes, all three tasks |

One acceptance criterion needs a note: **`grep -c "_TIME_COL_WIDTH"` returns 2, not 3.** The criterion asks for "at least twice", which is met — one definition and one use in `jobs`. `_TIME_COL_SAMPLE` is a separate name because the derivation reads the rendered sample twice.

## Known Stubs

None. Every symbol this plan added is wired to a real caller and covered by tests. `_UNSET_CREDENTIAL_PROBLEM` is consumed by the one guard that exists for it; `_TIME_COL_WIDTH` is the `jobs` table's actual column width; `local_time` is imported by both `cli.py` and `config.py` and is the same object `saneless.vocabulary` exports.

## Threat Flags

None. Everything this plan touches is already in the plan's register, and the four `mitigate` dispositions are implemented:

| Threat ID | Mitigation as built |
|---|---|
| T-30-41 | The advice line renders `error_next_step(category)` with nothing interpolated. The parametrised test asserts the line equals that constant exactly, so no exception text can reach it |
| T-30-42 | The refusal message is `_UNSET_CREDENTIAL_PROBLEM`, a developer constant naming neither the token value, the paperless-ngx URL nor the config path. `test_the_placeholder_refusal_never_echoes_the_value` asserts the configured value appears in neither stream. The token is unwrapped only to call the predicate |
| T-30-43 | The `--json` branch is byte-for-byte unchanged apart from a comment, and three assertions pin it: the `+00:00` doc-truth test, the round-trip test and the "local rendering never appears" test |
| T-30-44 | The guard precedes `SaneBackend(...)`; a counting stub asserts zero constructions and zero `get_devices` calls, and a `PaperlessClient` stand-in raises if it is reached |

T-30-45 (the server's zone appearing in a paperless-ngx document title) remains `accept`, as the plan decided: the zone abbreviation is D-35's intent.

## User Setup Required

None for this plan, but the phase-level note from 30-01 is now load-bearing on three surfaces rather than one: `local_time` reads the process's zone, so a container reports UTC unless `TZ` is set. That now affects the `jobs` table, the generated document title that reaches paperless-ngx, and (once 30-13 registers the filter) the web history table. `docs/reference/cli-commands.md`'s new `scan` paragraph tells the operator to set `TZ`; the `docker-compose.yml` and `docs/reference/docker.md` `TZ=` lines are still owed by a later plan in this phase.

## Next Phase Readiness

- **30-11 / 30-13 (web surfaces)** — `local_time` now has a second real consumer, so the Jinja filter must register the same object rather than re-deriving the format; the identity assertion in `TestJobsTableWidth` is the model for the template-side one.
- **30-12 (docs/verification)** — the three documents this plan owns are corrected and `cli-scripting.md`'s `created_at` shape is pinned by a doc-truth test, so a later localisation of the machine contract fails the suite.
- **Anyone touching `_GuardedGroup.invoke`** — the advice line is now asserted by 23 tests plus ten `_failure_lines` call sites. Removing or reordering it will be loud.

No blockers.

## Self-Check: PASSED

- `src/saneless/cli.py` — FOUND, contains `Try: ` and `is_placeholder_token(`
- `src/saneless/config.py` — FOUND, contains `local_time(`
- `tests/test_cli.py`, `tests/test_config.py`, `tests/test_deployment_config.py` — FOUND
- `docs/how-to/configure-scan-profiles.md`, `docs/how-to/cli-scripting.md`, `docs/reference/cli-commands.md` — FOUND
- Commits `4cc1dea`, `5db5061`, `5e0e21e`, `4ae27e7`, `fa64854`, `929f96a` — all FOUND in `git log`, in RED-before-GREEN order, on top of the plan's base `3a15bbd`
- `STATE.md` and `ROADMAP.md` deliberately untouched — the orchestrator owns those writes

---
*Phase: 30-appliance-layer*
*Plan: 10*
*Completed: 2026-09-16*
