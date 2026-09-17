---
phase: 30-appliance-layer
plan: 20
subsystem: api
tags: [checks, doctor, status-strip, profiles, threading, tdd]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: the shared check registry (checks.py), ProfileStorage, doctor and the web status strip
provides:
  - "config.profile_storage_for_loaded -- the single derivation of the no-write-attempted profile-storage fact"
  - "ScanWorker records that fact at both _generate_startup_profiles early returns, so a config-file appliance no longer shows a false amber Profiles row"
  - "doctor builds its CheckContext from the same function"
  - "an agreement test pinning doctor and the worker to one rendered Profiles row"
affects: [30-21, status-strip, checks]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "A fact both surfaces report is derived by one exported function, and a test asserts the call, not just that the two answers happen to match"
    - "Cross-thread state left unlocked states its own thread discipline in the property docstring, with a pointer from the __init__ seed"

key-files:
  created: []
  modified:
    - src/saneless/config.py
    - src/saneless/worker.py
    - src/saneless/cli.py
    - tests/test_config.py
    - tests/test_worker.py
    - tests/test_doctor.py

key-decisions:
  - "profile_storage_for_loaded lives in config.py, not checks.py: it is a question about a Settings object, and config.py already imports saneless.vocabulary so no new dependency direction is introduced"
  - "The function is deliberately unable to return IN_MEMORY_UNWRITABLE, and a test pins that: only an attempted write can produce that outcome, and no probe short of the write itself can see the EBUSY bind-mount failure D-09 was written for"
  - "_profile_storage stays unlocked; the reason is documented in the property docstring rather than papered over with a lock (IN-04)"
  - "The worker tests gate on stop() returning True rather than polling for a generated profile name: for a skipped generation there is no profile to wait for, and the bounded join is a deterministic 'startup step has run' signal"
  - "Task 3's agreement tests pass at the RED commit by design (Task 2 already fixed the worker); a third test pins that doctor CALLS the shared function, and that one is a genuine RED"

patterns-established:
  - "Agreement test: derive a shared fact from each surface's real code path (capture doctor's CheckContext, start a real ScanWorker), render both through run_checks, and compare state/message/next_step"

requirements-completed: [APPL-01, APPL-02, APPL-06]

# Metrics
duration: 42min
completed: 2026-09-16
---

# Phase 30 Plan 20: One Profiles Row Summary

**`config.profile_storage_for_loaded` is now the only derivation of "nothing was written, so where do the profiles live?", called by both `ScanWorker._generate_startup_profiles` and `saneless doctor`, ending the permanent false amber Profiles row on every config-file deployment.**

## Performance

- **Duration:** ~42 min
- **Tasks:** 3 (all TDD)
- **Files modified:** 6
- **Full suite:** 2903 passed

## Accomplishments

- **CR-01 closed.** A worker started against a non-bare config file now reports `ProfileStorage.PERSISTED`. The web Profiles row and `doctor` say the same thing for the same appliance.
- **The rule exists once.** `profile_storage_for_loaded(settings)` in `config.py`, exported, with the reasoning that used to sit as a comment in `cli.py` now in its docstring — including why it cannot report `IN_MEMORY_UNWRITABLE`.
- **IN-04 closed.** The `profile_storage` property documents its own thread discipline (one startup-time rebind on the worker thread, atomic under the GIL, deliberately unlocked unlike `front_pages`), with a cross-reference at the `__init__` seed.
- **IN-05 closed.** `PLACEHOLDER_TOKENS` justifies keeping the `changeme` literal by the upgrade path instead of citing `docker-compose.yml:30` and `docs/reference/docker.md:178` — lines this phase's own diff removed.
- **The gap is pinned.** 10 worker `profile_storage` tests, 6 config tests, 4 doctor agreement tests.

## Task Commits

1. **Task 1: one derivation of the no-write-attempted storage fact**
   - `2b700d0` test(30-20): pin the no-write-attempted profile storage derivation (RED)
   - `4e92306` feat(30-20): add the shared profile-storage derivation (GREEN, incl. IN-05)
2. **Task 2: the worker records the loaded-settings fact when no generation ran**
   - `94ae440` test(30-20): cover the skipped-generation profile-storage path (RED)
   - `7489ac6` fix(30-20): record the loaded-settings storage fact when generation is skipped (GREEN, incl. IN-04)
3. **Task 3: doctor reads the shared derivation**
   - `650988b` test(30-20): pin doctor and the worker to one Profiles row (RED)
   - `88466de` refactor(30-20): doctor derives profile storage through the shared function (GREEN)

No REFACTOR-gate commit was needed for Tasks 1 and 2; Task 3's GREEN *is* the refactor.

## The CR-01 reproduction, before and after

The plan asked for both outcomes recorded explicitly. The RED commit for Task 2 (`94ae440`) is the parent of the worker fix, so running the new cases there *is* the before-state measurement — no scratch worktree was needed.

**Before (`94ae440`, worker unchanged):** `uv run pytest tests/test_worker.py -q -k profile_storage` → **3 failed, 7 passed**

```
FAILED tests/test_worker.py::TestProfileStorage::test_profile_storage_is_persisted_when_generation_was_skipped
FAILED tests/test_worker.py::TestProfileStorage::test_profile_storage_is_persisted_when_no_scanner_was_found
FAILED tests/test_worker.py::TestProfileStorage::test_profile_storage_renders_a_green_profiles_row_with_a_config_file

E  AssertionError: assert <ProfileStorage.IN_MEMORY_NO_CONFIG_FILE: 'IN_MEMORY_NO_CONFIG_FILE'>
                       is <ProfileStorage.PERSISTED: 'PERSISTED'>
```

That third failure is the user-visible half: with a real config file mounted, the rendered Profiles row came out `WARN` with the message *"Generated in memory — no configuration file is in use, so they are lost on restart"* and the next step *"Create a saneless config file so the profiles are saved."*

**After (`7489ac6`):** `uv run pytest tests/test_worker.py -q -k profile_storage` → **10 passed**. The same appliance renders `OK` / `2 scan profiles configured.` with no next step, which is byte-for-byte what `doctor` prints.

## Files Created/Modified

- `src/saneless/config.py` — added `profile_storage_for_loaded` (+ `__all__` entry, `ProfileStorage` import); rewrote the `PLACEHOLDER_TOKENS` justification comment (IN-05)
- `src/saneless/worker.py` — both `_generate_startup_profiles` early returns record the loaded-settings fact; `profile_storage` docstring gained the thread-discipline paragraph and lost a now-false claim; seed comment cross-references it (IN-04)
- `src/saneless/cli.py` — `doctor` builds `CheckContext` from the shared function; inline conditional and the `ProfileStorage` import removed
- `tests/test_config.py` — `TestProfileStorageForLoaded` (6 tests)
- `tests/test_worker.py` — 5 new cases + `_second_profile` helper in `TestProfileStorage` (10 selected total)
- `tests/test_doctor.py` — `TestProfilesRowAgreement` (4 tests)

## Decisions Made

- **The early-return fix calls the shared function rather than inlining the conditional the review sketched.** The review's proposed patch reproduced the two-copies shape that caused CR-01 in the first place; one function is what makes a third copy impossible.
- **Worker tests gate on `stop()` returning `True`.** For the skipped-generation path there is no generated profile name to poll for. `stop()`'s bounded join is a deterministic signal that `_generate_startup_profiles` has completed, since it runs before the loop. No new `time.sleep` was introduced.
- **A `_second_profile` helper builds the non-bare shape.** Two profiles is what a real deployment looks like after `saneless auto-profiles` has written the file once, and it is exactly what `docker-compose.yml`'s mounted `./config` produces.
- **Task 3's RED is the call-site pin, not the agreement.** Because the GREEN is a pure refactor with no user-visible change, the agreement tests necessarily pass beforehand. A separate test monkeypatches `saneless.cli.profile_storage_for_loaded` and fails with `AttributeError` until `doctor` imports it — that is the genuine RED, and it is what makes a future third copy fail the suite.

## Deviations from Plan

None affecting behaviour. Two plan-text corrections worth recording:

**1. [Rule 1 - Bug] The `profile_storage` property docstring stated the CR-01 bug as intended behaviour**

- **Found during:** Task 2
- **Issue:** The docstring read *"A worker whose startup generation never ran, because the settings were not the bare default, reports the no-config-file value: nothing was written, which is exactly what that member says."* That is the defect, documented as a contract. Leaving it would have left doc-truth contradicting the fix.
- **Fix:** Replaced with the corrected statement — a worker that attempted no write reports what `config.profile_storage_for_loaded` says about the settings it loaded, the same function `doctor` calls. The `Returns:` clause was widened to match.
- **Files modified:** `src/saneless/worker.py`
- **Verification:** `uv run pytest tests/test_worker.py -q -k profile_storage` (10 passed); gate commands clean.
- **Committed in:** `7489ac6`

**2. [Documentation] The plan's `<verification>` `time.sleep` figure was stale**

- **Found during:** final verification
- **Issue:** The plan expects `grep -rc "time\.sleep" tests/` to total 17. It totals **19**, and did so at the base commit `3199941` as well.
- **Assessment:** Not a regression from this plan. `git diff 3199941 HEAD -- tests/ | grep -c "^+.*time\.sleep"` is **0** — this plan added no polling sleeps. The same check for `# noqa` / `# type: ignore` additions is also **0**; the two `# noqa: ARG003` in `config.py:638-639` are pre-existing pydantic-settings signature suppressions untouched here.
- **Action:** Recorded rather than "fixed" — changing unrelated tests to hit a stale number would be scope creep.

---

**Total deviations:** 1 auto-fixed (Rule 1 — doc-truth bug), 1 documentation discrepancy recorded.
**Impact on plan:** No scope creep. Every acceptance criterion in the plan was met as written.

## Acceptance Criteria

| Criterion | Result |
|-----------|--------|
| `grep -c "def profile_storage_for_loaded(" src/saneless/config.py` | 1 |
| `grep -c "docker-compose.yml:30" src/saneless/config.py` | 0 |
| `grep -c "changeme" docker-compose.yml` (citation was stale) | 0 |
| `grep -c "profile_storage_for_loaded(" src/saneless/worker.py` | 2 |
| `grep -c "_profile_storage" src/saneless/worker.py` | 8 |
| `grep -c "profile_storage_for_loaded(" src/saneless/cli.py` | 1 |
| `grep -cE "ProfileStorage\.(PERSISTED\|IN_MEMORY_NO_CONFIG_FILE) *$" src/saneless/cli.py` | 0 |
| `pytest tests/test_config.py -k ProfileStorageForLoaded` | 6 passed (≥4 required) |
| `pytest tests/test_worker.py -k profile_storage` | 10 passed (≥6 required), pre-attempt test unchanged |
| `pytest tests/test_doctor.py -k Agreement` | 4 passed (≥2 required) |
| `pytest tests/test_doctor.py` collected count vs parent | 22 → 26, none removed or skipped |
| `uv run pytest -q` | 2903 passed |
| `ruff check . && ruff format --check . && ty check && pyrefly check src tests` | all clean |

## Issues Encountered

- **`_generate_startup_profiles` has no public completion signal.** The existing tests in `TestProfileStorage` wait for `"flatbed" in worker.profile_names()`, which never becomes true when generation is skipped. Resolved by gating on `worker.stop()` returning `True`: the bounded join guarantees the startup step finished, because it runs before the loop is entered. Deterministic, and it adds no sleep.
- **PLR0915 did not trigger** on `_generate_startup_profiles` despite the two added statements, so the `_record_loaded_storage()` helper the plan authorised as a fallback was not needed.

## Threat Flags

None. No new network endpoint, auth path, file access pattern or schema change. `profile_storage_for_loaded` returns an enum member and reads `settings.config_path` for its None-ness only — the path never reaches a message, a log line or the LAN-visible page (T-30-20-01 satisfied). `checks.py` was not touched; plan 30-21 owns it.

## User Setup Required

None.

## Next Phase Readiness

- The Profiles row now has one source of truth, which is the precondition plan 30-21 needs before it edits `checks.py`'s `match` over `ProfileStorage`.
- `config.profile_storage_for_loaded` is exported and stable; any third surface that needs this fact must call it, and `TestProfilesRowAgreement::test_doctor_derives_the_row_through_the_shared_function` will fail if a copy is written instead.

## Self-Check: PASSED

- `src/saneless/config.py`, `src/saneless/worker.py`, `src/saneless/cli.py`, `tests/test_config.py`, `tests/test_worker.py`, `tests/test_doctor.py` — all present and modified.
- Commits `2b700d0`, `4e92306`, `94ae440`, `7489ac6`, `650988b`, `88466de` — all present in `git rev-list HEAD`.

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-16*
