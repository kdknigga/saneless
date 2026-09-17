---
phase: 30-appliance-layer
plan: 22
subsystem: testing
tags: [logging, strftime, log-injection, tdd, pytest, caplog]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "local_time / LOCAL_TIME_FORMAT (D-34, D-35) and _note_pass_count (A-4), the two call sites this plan hardens"
provides:
  - "local_time that can never return trailing whitespace, so no host shape files a paperless-ngx document whose title ends in a space (IN-06)"
  - "Every user-supplied-title interpolation in pipeline.py logged with %r, so a newline in a title cannot forge a log record (IN-03)"
  - "A source-level guard test that fails if a new '%s'-shaped interpolation is added to pipeline.py"
affects: [paperless-ngx delivery, operator log review, any later phase adding a pipeline log line]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "%r for any request-supplied value in a log line (the web/errors.py discipline, now applied in pipeline.py)"
    - "Pin an unreproducible platform case as a property via monkeypatched module globals rather than skipping it"

key-files:
  created: []
  modified:
    - src/saneless/vocabulary.py
    - src/saneless/pipeline.py
    - tests/test_vocabulary.py
    - tests/test_pipeline.py

key-decisions:
  - "rstrip, not ' '.join(value.split()): the strip is trailing-only so the date/time separator the format deliberately contains survives"
  - "The empty-%Z platform is pinned as a property via monkeypatched LOCAL_TIME_FORMAT, because POSIX requires a zone abbreviation of at least three characters so no TZ value reproduces it on glibc"
  - "A doc-truth test asserts LOCAL_TIME_FORMAT still ends in %Z, so the trailing-space property cannot be satisfied by dropping the zone D-34 pins"
  - "The profile/device log line was converted to %r alongside the three title lines, to satisfy the plan's own zero-'%s' acceptance grep and leave no old-style call site for a later author to copy"

patterns-established:
  - "Log-line escaping: request input goes into a log record with %r, asserted on record.getMessage() rather than record.args"
  - "Source-level guard test: inspect.getsource(module) assertions catch a future call site that no per-call behaviour test would reach"

requirements-completed: [APPL-03, APPL-12]

# Metrics
duration: 14min
completed: 2026-09-16
---

# Phase 30 Plan 22: Log and Title Hygiene Summary

**`local_time` now rstrips its rendered value so an empty `%Z` cannot leave a trailing space in a paperless-ngx document title, and all four quoted `%s` interpolations in `pipeline.py` became `%r` so a newline in a job title cannot forge a log record.**

## Performance

- **Duration:** 14 min
- **Started:** 2026-09-16T21:59:36Z
- **Completed:** 2026-09-16T22:13:52Z
- **Tasks:** 2 (TDD, 4 commits)
- **Files modified:** 4

## Accomplishments

- **IN-06 closed.** `local_time` returns `value.astimezone().strftime(LOCAL_TIME_FORMAT).rstrip()`. A platform whose `%Z` renders as the empty string no longer leaves the separator dangling, so `resolve_job_title`'s `f"Scan {local_time(now)}"` cannot produce a document title ending in a space. The docstring now names the case, the consumer it reaches, and why the strip is trailing-only.
- **IN-03 closed.** The three title-interpolating log lines in `pipeline.py` (`_note_pass_count`'s failure path, and both "Pipeline complete" lines) now use `%r`. A comment at `_note_pass_count` names the rule rather than the mechanics: a job title is request input, bounded only in length and never in character set, so it can contain newlines.
- **A guard against the next one.** `TestTitleLogEscaping.test_no_quoted_percent_s_interpolation_remains` reads `pipeline.py`'s own source and fails if any `'%s'` reappears — the only check that would catch a new call site added later in the old style.
- Full suite green at **2900 passed** (up from 2891 at the parent commit — 9 tests added, none removed).

## Task Commits

1. **Task 1: local_time cannot emit trailing whitespace**
   - RED — `4e44d6e` (`test(30-22): pin local_time against a trailing zone separator`)
   - GREEN — `b77ef2b` (`fix(30-22): strip the empty-zone separator from a rendered local time`)
   - REFACTOR — none needed
2. **Task 2: a job title in a pipeline log line is escaped**
   - RED — `1853cf0` (`test(30-22): pin pipeline title log lines against forged newlines`)
   - GREEN — `fc737f9` (`fix(30-22): escape user-supplied titles in pipeline log lines`)
   - REFACTOR — none needed

**Plan metadata:** committed with this SUMMARY.

## Files Created/Modified

- `src/saneless/vocabulary.py` — `local_time` rstrips the formatted value; docstring gains the empty-`%Z` case, the `resolve_job_title` consequence, and the reason the strip is trailing-only. `LOCAL_TIME_FORMAT` untouched.
- `src/saneless/pipeline.py` — four format strings converted from `'%s'` to `%r` (the redundant quotes removed, since `%r` supplies its own); one comment added at `_note_pass_count`. No argument expression, log level, or `logger.exception`/`exc_info` usage changed.
- `tests/test_vocabulary.py` — `TestLocalTimeTrailingSpace` (5 tests): the `%Z` doc truth, three parametrised whitespace formats, the exact-render and leading-character guards, and a real-host non-empty check.
- `tests/test_pipeline.py` — `TestTitleLogEscaping` (5 tests) plus the `_note_pass_count` import and `inspect`.

## Decisions Made

- **The empty-`%Z` platform is pinned as a property, not reproduced.** POSIX requires a zone abbreviation of at least three characters, so no `TZ` value yields an empty `%Z` on glibc. The tests monkeypatch `saneless.vocabulary.LOCAL_TIME_FORMAT` to formats ending in a space, a tab, and two spaces instead — `local_time` looks the constant up as a module global at call time, which is what makes that work.
- **`rstrip`, not `" ".join(value.split())`.** The latter would also collapse the interior spacing the format deliberately contains. A companion test asserts a leading space in a format survives, so the strip is provably trailing-only.
- **The doc truth is a test, not a comment.** `test_the_shared_format_still_names_the_zone` asserts `LOCAL_TIME_FORMAT.endswith("%Z")`, so a future author cannot satisfy the trailing-space property by deleting the zone that D-34 pins.
- **Assertions are on `record.getMessage()`, never on `record.args`.** The raw argument is the unescaped title by design; asserting on it would pass whatever the format string said.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Two RED tests asserted an exact string without pinning the host's zone**
- **Found during:** Task 1 (RED)
- **Issue:** `test_the_rest_of_the_rendering_is_untouched` and `test_a_leading_character_is_never_stripped` asserted `"2026-09-16 19:03"` while the host renders `America/Chicago`, so they failed at `14:03` — the wrong reason for a RED failure, and they would have stayed red after the fix.
- **Fix:** Both now take the file's existing `local_zone` fixture and pin `UTC` before asserting.
- **Files modified:** `tests/test_vocabulary.py`
- **Verification:** RED re-run showed 5 failures, every one on trailing whitespace; GREEN turned all 5 green.
- **Committed in:** `4e44d6e` (Task 1 RED commit)

**2. [Rule 3 - Blocking] `PLR0913` on the pipeline test helper**
- **Found during:** Task 2 (RED)
- **Issue:** `_completion_message` took 6 arguments; ruff's `PLR0913` caps it at 5, and the project forbids `# noqa`.
- **Fix:** Split out a `_forging_request()` classmethod and moved the `tmp_dir` assignment to the two callers, bringing the helper to 5 parameters. No suppression added.
- **Files modified:** `tests/test_pipeline.py`
- **Verification:** `uv run ruff check .` clean; RED still failed for the right reason afterwards.
- **Committed in:** `1853cf0` (Task 2 RED commit)

**3. [Rule 2 - Missing Critical] The profile/device log line converted alongside the three title lines**
- **Found during:** Task 2 (GREEN)
- **Issue:** The plan's `<action>` names three sites, but its own acceptance criterion requires `grep -cE "'%s'" src/saneless/pipeline.py` to be 0. A fourth line — `"Scanning with profile '%s' on device '%s'"` at what was line 2113 — also carries request-derived input (`request.profile_name`) in the old style.
- **Fix:** Converted it to `"Scanning with profile %r on device %r"`. Only the format string changed; the argument expressions and level are untouched.
- **Files modified:** `src/saneless/pipeline.py`
- **Verification:** `grep -cE "'%s'"` is now 0 and `grep -c "%r"` is 6; `test_no_quoted_percent_s_interpolation_remains` passes.
- **Committed in:** `fc737f9` (Task 2 GREEN commit)

---

**Total deviations:** 3 auto-fixed (2 blocking, 1 missing critical)
**Impact on plan:** Both blocking fixes were test-side corrections needed to make RED fail honestly. The fourth `%r` conversion reconciles the plan's prose with its own acceptance grep and removes the last old-style call site a later author could copy. No scope creep — `src/saneless/config.py` was not touched, as plan 30-20 owns it in this wave.

## Issues Encountered

- **The plan's `time.sleep` verification count is stale.** `grep -rc "time\.sleep" tests/ | awk -F: '{s+=$2} END {print s}'` reports **19**, not the 17 the plan's `<verification>` block names. The drift predates this plan: `git diff <base>..HEAD -- tests/` adds **zero** `time.sleep` lines. Flagged for the verifier rather than chased, since correcting the count is out of this plan's scope.

## Verification

| Check | Result |
|---|---|
| `uv run pytest -q` | 2900 passed |
| `uv run pytest tests/test_vocabulary.py -q -k LocalTimeTrailingSpace` | 5 passed (≥3 required) |
| `uv run pytest tests/test_pipeline.py -q -k TitleLogEscaping` | 5 passed (≥3 required) |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -cE "'%s'" src/saneless/pipeline.py` | 0 |
| `grep -c "%r" src/saneless/pipeline.py` | 6 (≥3 required) |
| `LOCAL_TIME_FORMAT.endswith("%Z")` | True |
| `git diff --stat` touches `src/saneless/config.py` | No |

## Success Criteria

- [x] `local_time` never returns trailing whitespace, and `LOCAL_TIME_FORMAT` still ends in `%Z`
- [x] Every title interpolation in `pipeline.py` uses `%r`

## Threat Model Outcome

| Threat ID | Disposition | Outcome |
|---|---|---|
| T-30-22-01 | mitigate | Done. `%r` escapes control characters at all four sites; pinned by three `record.getMessage()` assertions on a title containing `\n`. |
| T-30-22-02 | mitigate | Done. `local_time` rstrips, so no host files a title differing from another's by an invisible character. |
| T-30-22-03 | accept | Unchanged. The escaped title is the operator's own input in the operator's own log; no new value and no new sink. |
| T-30-22-SC | n/a | No package installed; `pyproject.toml` and `uv.lock` untouched. |

No new security-relevant surface was introduced — no endpoint, auth path, file access pattern, or schema change. No threat flags.

## Known Stubs

None.

## TDD Gate Compliance

Both tasks ran a full RED → GREEN cycle with a distinct commit per gate, and each RED was verified to fail for the intended reason before the fix was written. No REFACTOR gate was needed: both fixes are single-expression changes with nothing to clean up.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Both gap-closure findings for this plan (IN-03, IN-06) are closed and pinned by tests.
- No blockers. The only open item is the stale `time.sleep` count in this plan's own verification block (19 vs. the 17 named), which belongs to the verifier, not to this plan.

## Self-Check: PASSED

All five files named above exist on disk, and all five commits
(`4e44d6e`, `b77ef2b`, `1853cf0`, `fc737f9`, `082d44b`) are present in
`31999416c2b044803e409e7227ff1979facca735..HEAD`.

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-16*
