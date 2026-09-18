---
phase: 30-appliance-layer
plan: 36
subsystem: api
tags: [concurrency, threading, rate-limiting, fastapi, tdd, mutation-testing, r3-in-03]

# Dependency graph
requires:
  - phase: 30-appliance-layer
    provides: "the manual-refresh floor and MIN_MANUAL_REFRESH_SECONDS (30-26), release_manual_claim and the collapse branch probe_now reports (30-29), the refresh handler as 30-32 and 30-35 left it"
provides:
  - "CheckCache.claim_manual_refresh returning the stamp it wrote, or None when it refuses"
  - "CheckCache.release_manual_claim(stamp) -> bool, a compare-and-clear that gives back only the caller's own grant"
  - "a refresh handler that binds its grant to a local, branches on `is not None` and carries that stamp to its release"
  - "tests/test_web_checks.py::_RecordingCache and _a_recording_cache, the real cache recording what a request was granted and what it handed back"
  - "a stale-stamp case and two real-thread cases in TestReleaseManualClaim, none of which passes against an unconditional clear"
affects: [the Refresh button, any future caller of the manual-refresh floor]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a claim returns an identifier for the grant it made, and the release compares it before clearing, so the safety property is enforced at the point of use rather than argued about one call site"
    - "a permission whose value is a timestamp reports refusal as None and is tested with `is not None`, because a monotonic reading of 0.0 is a real grant and is falsey"
    - "a stub that observes a handler's calls is a subclass of the real collaborator, so every method the handler does not exercise is still the production one"

key-files:
  created: []
  modified:
    - src/saneless/web/checks_cache.py
    - src/saneless/web/routes.py
    - tests/test_checks_cache.py
    - tests/test_web_checks.py

key-decisions:
  - "Compare-and-clear on the returned stamp, not a token object and not a second lock: the stamp is already a float written under self._lock, so returning it costs nothing and the release's precondition becomes checkable where it is used"
  - "The refusal is None rather than a falsey stamp, and both the cache docstring and the handler docstring say so: time.monotonic() counts from boot, so 0.0 is a grant a freshly booted appliance really records"
  - "release_manual_claim reports whether it cleared, so 'a stale stamp changed nothing' is an assertion a test can make directly rather than inferring it from a later claim"
  - "The 'a collapse buys no Paperless request, no saned dial and no filesystem write' paragraph is kept verbatim: it is about what a release costs, which this plan does not change"
  - "routes.py was adapted to the new signatures inside the task-1 GREEN commit, in the truthiness shape the old code had, because this project's commit hooks type-check src/ and a broken call site cannot be committed; task 2's RED then failed for exactly that truthiness (deviation, Rule 3)"

patterns-established:
  - "Every mutation the plan names was applied, run, counted and reverted, and the observed failures are recorded below"
  - "A test that pins which value crossed a boundary compares two records the collaborator made, never a value the test computed"

requirements-completed: [APPL-02]

# Metrics
duration: 15min
completed: 2026-09-17
---

# Phase 30 Plan 36: The Release Gives Back Its Own Grant Summary

**`release_manual_claim` is now a compare-and-clear over the stamp `claim_manual_refresh` returns, so the 2 s floor under an unauthenticated LAN probe endpoint can no longer be lowered by a release that arrives after somebody else's grant — proved by a stale-stamp case and two real-thread cases that all fail against the unconditional clear, instead of by a paragraph of reasoning about one call site.**

## Performance

- **Duration:** ~15 min (base `8b13e33` at 21:57, first commit 2026-09-17T22:03:55-05:00, last 22:12:43-05:00)
- **Tasks:** 2 (both TDD, RED → GREEN, no REFACTOR needed)
- **Commits:** 4
- **Files modified:** 4 (0 created, 0 deleted)

## Accomplishments

### Task 1 — the claim reports its stamp, the release compares before clearing (`31a8fb3` RED, `916dea0` GREEN)

- `claim_manual_refresh` returns `float | None`: the stamp it wrote on a grant, `None` on a refusal. A refusal still writes nothing — the property the compare-and-clear leans on, and it is still pinned by `test_a_refused_claim_does_not_move_the_stamp_forward`.
- Its `Returns:` section states that **truthiness is not the contract**. `time.monotonic()` counts from boot on Linux, so 0.0 is a reading a grant can really record and it is falsey; a caller branching on truth would read the first click after a boot as a refusal. That is the same fact `_last_manual_claim`'s own comment gives for starting at `None` rather than 0.0.
- `release_manual_claim(stamp) -> bool` clears `self._last_manual_claim` only when it currently equals `stamp`, under the same one lock, and reports whether it cleared. `grep -c "self._last_manual_claim = None$"` is 1, and that line sits inside the comparison.
- The docstring's narrative safety argument — "only a granted caller calls this, microseconds later, on the same thread, and a competing claimer in that window is refused without writing" — is replaced by the rule the code enforces, with a sentence recording that the old argument was true of that call site and of nothing else: a retry, a second caller or a handler that grew a second release would each have lowered the floor.
- The "it cannot be abused to defeat the floor" paragraph is kept word for word. It is about what a *release* costs (a collapse issued no Paperless request, no saned dial, no filesystem write), which this plan does not touch.
- `TestReleaseManualClaim` grew from 5 cases to 8 and every case now asserts the release's own return as well as the floor's later behaviour.

### Task 2 — the one caller carries its stamp (`5fa247e` RED, `f4bd839` GREEN)

- `refresh_checks` binds the grant to a local and branches on `is not None`, then releases *that* stamp when `probe_now` reports a collapse. The shape the plan asked for is intact: no probe is attempted when the claim was refused, and the single `TemplateResponse` is still the last statement.
- Its docstring's collapse paragraph names the compare-and-clear — the handler gives back the grant it holds, identified rather than assumed — and states why the branch is `is not None` rather than a truth test.
- `_RecordingCache` (a subclass of the real `CheckCache`) and the `_a_recording_cache` context manager record what a request was granted and what it handed back. It is installed where the app builds its cache, so the routes and the refresher still share one instance, exactly as the `clocked` fixture does.
- The button's three documented behaviours are unchanged and still asserted by the classes that owned them: granted-and-probed spends the floor (`test_an_immediate_second_refresh_does_not_probe`), collapsed gives the claim back so the next click is honoured (`test_a_collapsed_refresh_does_not_spend_the_manual_floor`), refused re-renders the same partial with no probe (`test_a_refused_refresh_renders_what_a_cache_read_would`).

## Task Commits

1. **Task 1: the claim reports its stamp and the release compares before clearing**
   - `31a8fb3` (test — RED: 18 of 33 cases failed; `release_manual_claim() takes 1 positional argument but 2 were given`, and every claim assertion read `True`/`False` where the stamp contract wanted a value)
   - `916dea0` (feat — GREEN: 33 passed)
2. **Task 2: the one caller carries its stamp**
   - `5fa247e` (test — RED: `test_the_first_click_after_a_boot_is_a_grant_and_not_a_refusal` failed with `assert 0 == 1`, because a stamp of 0.0 is falsey and the handler's truthiness branch skipped the probe)
   - `f4bd839` (feat — GREEN: 139 passed in `tests/test_web_checks.py`, 3175 passed across the suite)

No REFACTOR commit: neither implementation left anything to clean up.

## Mutation evidence (run by hand, applied, counted and reverted)

| Mutation | Tests it kills | Observed |
|---|---|---|
| `release_manual_claim` clears unconditionally (`self._last_manual_claim = None` with no comparison) | 4 | `test_a_stale_stamp_does_not_clear_another_callers_claim`, `test_a_late_release_from_one_thread_leaves_the_others_grant`, `test_releasing_twice_leaves_the_floor_where_one_release_left_it`, `test_releasing_a_claim_that_was_never_granted_is_a_no_op` — the first two are the cases the plan names; e.g. `assert True is False` on the stale release, and the later claim inside the interval is granted where it must be refused |
| the handler releases a freshly read clock value (`state.checks.release_manual_claim(time.monotonic())`) | 2 | `test_a_collapsed_click_gives_back_the_stamp_it_was_granted` (`assert [11705995.895691177] == [100.0]`) and the plan-named `test_a_collapsed_refresh_does_not_spend_the_manual_floor`, because the claim is no longer given back and the next click is refused |
| the handler branches on truthiness (`if claim and not …`) — the state the task-2 RED commit was taken in | 1 | `test_the_first_click_after_a_boot_is_a_grant_and_not_a_refusal`: `spy.calls` is 0, so a 0.0 stamp suppressed the probe the first click after a boot most deserves |

Both mutations were reverted from a pre-mutation copy of the file kept in the scratchpad, not with `git checkout`, and the full suite was re-run clean afterwards.

## Verification

| Gate | Result |
|---|---|
| `uv run pytest tests/test_checks_cache.py tests/test_web_checks.py -q` | 33 + 139 passed |
| `uv run pytest -q` | 3175 passed |
| `uv run pytest tests/test_checks_cache.py -q -k "thread"` | 2 passed, both starting a real second thread |
| `uv run ruff check .` | no issues |
| `uv run ruff format --check .` | 63 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | all hooks passed |
| `uv run prek run --stage pre-push --all-files` | all hooks passed (ty full, pyrefly full) |
| `grep -rn "# type: ignore\|# noqa" src/saneless/web/checks_cache.py src/saneless/web/routes.py` | no matches |
| `grep -n "def claim_manual_refresh" -A 3 …` | `-> float \| None` |
| `grep -n "def release_manual_claim" -A 3 …` | `(self, stamp: float) -> bool` |
| `grep -c "self._last_manual_claim = None$" …` | 1, inside the `!= stamp` comparison |

## Files Created/Modified

- `src/saneless/web/checks_cache.py` — `claim_manual_refresh` returns the stamp or `None`; `release_manual_claim(stamp)` compares before clearing and reports whether it did; both docstrings rewritten where the contract moved.
- `src/saneless/web/routes.py` — `refresh_checks` binds its grant, branches on `is not None`, releases that stamp; docstring names the compare-and-clear.
- `tests/test_checks_cache.py` — claim assertions moved to the `None` contract; two new claim cases (the stamp reported, the zero stamp); `TestReleaseManualClaim` rewritten to the new signatures plus a stale-stamp case and two real-thread cases.
- `tests/test_web_checks.py` — `_RecordingCache` / `_a_recording_cache`; a first-click-after-a-boot case and a collapsed-click stamp-identity case.

## Decisions Made

Recorded in the frontmatter's `key-decisions`. The one worth restating: this plan changes **who may clear the stamp** and nothing else about when a refresh is allowed. `MIN_MANUAL_REFRESH_SECONDS`, D-09's bypass, the single-flight collapse and the "did I probe" contract `probe_now` reports are all untouched, and the plan's own `decision_record` carries the round-3 finding-coverage map for a re-verification to audit.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `routes.py` adapted to the new signatures inside the task-1 GREEN commit**

- **Found during:** Task 1 (GREEN)
- **Issue:** The plan has task 1 change both signatures and task 2 update the sole call site. Between them, `routes.py` calls `release_manual_claim()` with no argument and uses a `float | None` in a boolean `and`. This project's commit-stage hooks type-check `src/`, and `CLAUDE.md` forbids `--no-verify`, `SKIP=` and stubs to get a commit through, so the task-1 GREEN could not be committed with the call site left broken.
- **Fix:** The task-1 GREEN commit adapts the call site minimally and *in the truthiness shape the old code already had* (`claim = …; if claim and not probe_now(): release(claim)`). Task 2's RED then has a real failure to demonstrate — the falsey-0.0 grant — and its GREEN replaces the truthiness test with `is not None` and adds the docstring.
- **Files modified:** `src/saneless/web/routes.py`
- **Verification:** `ty` and `pyrefly` clean at the task-1 commit; `tests/test_web_checks.py` 137 passed at that point; task 2's RED then failed with `assert 0 == 1` for the boot case.
- **Committed in:** `916dea0`

**2. [Rule 2 - Missing critical coverage] The falsey-stamp case, in both files**

- **Found during:** Task 1 (RED), carried into task 2
- **Issue:** The plan's action says the `Returns:` section must state that truthiness is not the contract, but names no test for it. Nothing would have caught a caller that branched on truth, and that caller is exactly what the code shipped before this plan (`if state.checks.claim_manual_refresh() and …`) — a defect only for a stamp of 0.0, which `time.monotonic()` on a freshly booted appliance really produces.
- **Fix:** `test_a_stamp_of_zero_is_a_grant_and_not_a_refusal` in `TestClaimManualRefresh` and `test_the_first_click_after_a_boot_is_a_grant_and_not_a_refusal` in `TestRefreshMinimumInterval`. The second is the plan's task-2 RED evidence.
- **Files modified:** `tests/test_checks_cache.py`, `tests/test_web_checks.py`
- **Verification:** the route case failed before `f4bd839` and passes after; both pass in the final suite.
- **Committed in:** `31a8fb3`, `5fa247e`

**3. [Rule 1 - Bug] `test_concurrent_claims_grant_exactly_one` would have raised on the new return type**

- **Found during:** Task 1 (RED)
- **Issue:** It asserted `sorted(granted) == [False, True]` over the two threads' outcomes. With `float | None` outcomes, `sorted` on a mixed list raises `TypeError` rather than failing an assertion.
- **Fix:** `sorted(outcome is not None for outcome in granted) == [False, True]`, which keeps the property (exactly one of two concurrent claims is granted) and states it in the new contract.
- **Files modified:** `tests/test_checks_cache.py`
- **Verification:** passes; still fails if both threads are granted.
- **Committed in:** `31a8fb3`

---

**Total deviations:** 3 auto-fixed (1× Rule 1, 1× Rule 2, 1× Rule 3)
**Impact on plan:** None on scope. The Rule 3 adaptation preserves the plan's two-task TDD split and made task 2's RED sharper than the plan specified; the Rule 2 tests close the one hole the plan's own `Returns:` wording pointed at.

## Issues Encountered

One of the new threaded cases handed a `float | None` to `release_manual_claim(stamp: float)` and `ty` rejected it. Fixed with an explicit `assert second is not None` in the assertion block rather than with a cast or a suppression.

## Threat Flags

None. The two `mitigate` dispositions in the plan's register are both implemented — T-30-36-01 by the compare-and-clear and T-30-36-02 by doing the compare and the clear together under the existing lock, with `test_a_thread_refused_inside_the_interval_loses_nothing` and `test_a_late_release_from_one_thread_leaves_the_others_grant` exercising the window the old argument only described. No new network endpoint, auth path, file access or schema surface.

## Known Stubs

None. `_RecordingCache` is a subclass of the production cache whose every method does the real work.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

R3-IN-03 is closed, which is the last of the twelve round-3 findings; the plan's `decision_record` records where each of the other eleven landed and which two constraints were honoured rather than closed. Nothing here blocks re-verification.

## Self-Check: PASSED

---
*Phase: 30-appliance-layer*
*Completed: 2026-09-17*
