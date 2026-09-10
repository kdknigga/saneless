---
phase: 22-job-store-hardening
plan: 03
subsystem: database
tags: [sqlite, threading, rlock, decorator, pep695, concatenate, ast, inspect, threadpoolexecutor, tdd]

# Dependency graph
requires:
  - phase: 22-job-store-hardening (plan 22-01)
    provides: "self._lock (threading.RLock) created in __init__, conn.autocommit = False, and the WAL-before-the-flip ordering that makes `with self._conn:` a real transaction boundary"
  - phase: 22-job-store-hardening (plan 22-02)
    provides: "_COLUMNS, the derived statement constants, and the private undecorated _row_to_job that public methods share instead of calling one another"
  - phase: 21-vocabulary-and-contracts
    provides: "JobState, whose members the stress workers cycle through and whose reconstruction is the deserialisation invariant"
provides:
  - "_LOCKED_MARKER: the marker attribute name, spelled once and read by both the decorator and the test"
  - "_locked[**P, R]: the PEP 695 decorator that serialises a JobStore method on self._lock and stamps the marker"
  - "@_locked on all seven public JobStore methods, including close()"
  - "the transaction boundary moved into the method bodies as `with self._conn:`, with every explicit self._conn.commit() dropped"
  - "TestLockDiscipline: reflective lock coverage with no opt-out roster, an ast no-public-self-call walk, and a 2x200-round stress test"
affects: [22-04 prune, 22-05 fail_active_jobs and list_pending, 22-06, 26-worker-and-web-robustness]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "PEP 695 decorator over Callable[Concatenate[Self-class, P], R] so `self` stays typed inside the wrapper"
    - "marker attribute set via setattr through a module constant, never a literal and never a direct attribute assignment"
    - "reflective coverage test over inspect.getmembers with a floor assertion and no exemption roster"
    - "ast-walked structural invariant as a test, for a correctness rule no linter can see"
    - "ThreadPoolExecutor + Future.result() + threading.Barrier for deterministic, exception-propagating concurrency tests"

key-files:
  created: []
  modified:
    - src/saneless/job.py
    - tests/test_job.py

key-decisions:
  - "D-12/D-20 divergence, recorded deliberately: @_locked takes self._lock ONLY and never the connection's transaction context, because Connection.__exit__ runs after the body and must commit or roll back a connection close() has already closed"
  - "a @_locked(transaction=False) decorator factory was rejected: the lock-only form is simpler and the transaction boundary is more legible where it is executed"
  - "the reflective test detects the marker via getattr(member, _LOCKED_MARKER, False), not __wrapped__ (any functools.wraps decorator sets it) and not __qualname__ (wraps copies the wrapped function's, so it is identical either way)"
  - "no exemption roster, not even for close(): close() is decorated, so the test needs none"
  - "prune() takes its after-count inside the transaction rather than after the commit, so the two counts read the same snapshot"
  - "_UPDATE_JOBS and _DELETE_JOBS stay unconsumed: plan 22-04 builds _PRUNE over _DELETE_JOBS and plan 22-05 builds _FAIL_ACTIVE over _UPDATE_JOBS"
  - "the RED commit carries the bare _LOCKED_MARKER constant, because the project-wide ty pre-commit gate refuses a test that names a symbol which does not exist (Waves 1-2 precedent)"

patterns-established:
  - "Pattern: every public JobStore method is @_locked and opens its own `with self._conn:`; private helpers stay undecorated and are the only way two public methods share logic"
  - "Pattern: a structural rule that matters for correctness rather than style gets an ast-walking test, because ruff cannot see it"
  - "Pattern: thread exceptions in tests surface through Future.result(), never through raw threading.Thread"

requirements-completed: [STOR-01]

# Metrics
duration: 26min
completed: 2026-09-10
---

# Phase 22 Plan 03: Lock Discipline and Transaction Boundaries Summary

**Every public `JobStore` method now runs under one re-entrant lock and opens its own SQLite transaction, with a reflective test, an `ast` walk and a 2x200-round `ThreadPoolExecutor` stress test that fail the suite the moment any of the three rules is broken.**

## Performance

- **Duration:** 26 min
- **Started:** 2026-09-10T17:33:00-05:00
- **Completed:** 2026-09-10T17:59:00-05:00
- **Tasks:** 2 (RED, GREEN)
- **Files modified:** 2

## Accomplishments

- `_locked[**P, R]` — a PEP 695 decorator over `Callable[Concatenate[JobStore, P], R]` that holds `self._lock` for the whole call and stamps `_LOCKED_MARKER` on the wrapper via `setattr` through the module constant. All four checkers stay silent: `UP047` is satisfied by the PEP 695 spelling, `B010` by the constant-not-literal `setattr`, `TC003` by `Callable` already living under `TYPE_CHECKING`, and both type checkers by `Concatenate` making `self` typed so `self._lock` resolves.
- All seven public methods decorated — `create_job`, `get_job`, `update_state`, `update_thumbnail`, `list_recent`, `prune`, `close`. `__init__` and `_row_to_job` stay undecorated.
- The transaction boundary moved into the bodies. Every method that executes SQL now wraps its statements in `with self._conn:`; all four explicit `self._conn.commit()` calls are gone, and the context manager owns both the commit and the rollback on any uncaught exception — including `KeyboardInterrupt`, which a hand-written `try/except Exception` would not catch.
- Three structural tests that make the rules self-enforcing: reflective lock coverage with **no exemption roster** and a floor assertion so a vacuous match cannot pass; an `ast` walk proving no public method calls another; a 200-round two-thread stress test asserting zero exceptions plus four data invariants.
- Suite grew 521 → 524, all green. Stress test costs 0.12 s of a 60 s per-test budget and passed 10 consecutive runs.

## Task Commits

1. **Task 1 (RED): reflective lock coverage, ast self-call walk, 200-round stress test** — `4fe2a57` (test)
2. **Task 2 (GREEN): the `_locked` decorator and per-body transactions** — `8a87373` (feat)

## Files Created/Modified

- `src/saneless/job.py` — `_LOCKED_MARKER` and the `_locked` decorator added; `functools` and `typing.Concatenate` imported; all seven public methods decorated; `with self._conn:` moved into each SQL-executing body and every explicit `commit()` removed; `close()` decorated with deliberately no transaction context and a comment saying why.
- `tests/test_job.py` — `threading` and `concurrent.futures.ThreadPoolExecutor` imported; seven module constants (`PUBLIC_METHOD_FLOOR`, `STRESS_ROUNDS`, `STRESS_WORKERS`, `BARRIER_TIMEOUT`, `STRESS_STATES`, `PRUNE_MAX_AGE_DAYS`, `PRUNE_MAX_ROWS`); three helpers (`_job_store_classdef`, `_public_self_calls`, `_stress_worker`); and `class TestLockDiscipline` holding the three tests.

## Decisions Made

### The D-12 / D-20 divergence (the one this plan asked to record explicitly)

**`@_locked` takes `self._lock` and nothing else.** D-12's parenthetical — "the lock *and the connection's transaction context*" — is not implementable, and D-20 already corrected it. The measured reason:

`Connection.__exit__` runs *after* the decorated body and must commit or roll back. On `close()` the body has already closed the connection, so `with conn: conn.close()` raises `sqlite3.ProgrammingError: Cannot operate on a closed database`. RESEARCH verified this four ways, including a hand-rolled context manager that tries to check `conn.in_transaction` first — which fails identically, because `in_transaction` *itself* raises on a closed connection. C-07's own inline `with self._lock, self._conn:` prescription has the same defect; the review simply never showed `close()`.

The alternative that preserves the parenthetical — a parameterised `@_locked(transaction=False)` used only on `close()` — was rejected. It buys nothing the lock-only form does not already give (the marker still lands on every public method, so the reflective test still needs no roster) and costs a decorator factory plus typing ceremony. The transaction boundary is also strictly more legible where it is executed than hidden in a decorator.

The consequence for D-15 is exactly what the user asked for: `close()` reads

```python
@_locked
def close(self) -> None:
    """Close the database connection."""
    self._conn.close()
```

— `@_locked` and nothing else, no `_closed` flag, no saneless-owned use-after-close error. T-22-13 stays **accepted**; Phase 26 owns that fix and none of it was folded back in here.

### Marker detection

`getattr(member, _LOCKED_MARKER, False)` over `inspect.getmembers(JobStore, inspect.isfunction)`. `__wrapped__` was rejected because *any* `functools.wraps` decorator sets it, so an unrelated decorator would read as "locked". `__qualname__` was rejected because `functools.wraps` copies the *wrapped* function's qualname, making it identical whether the method is locked or not.

### No exemption roster, and a floor instead

The test carries no excused-names list — that is the hand-written roster Phase 21's D-09 warned about, and it is where the next exemption gets quietly added. Because `close()` is decorated, none is needed. Instead the test asserts `len(public) >= PUBLIC_METHOD_FLOOR` first, so a predicate that silently matches nothing cannot pass vacuously.

**Recorded blind spot, in a comment in the test:** `inspect.isfunction` does not see a public `@property`. `JobStore` has none today; if one is ever added, this test will not notice it is unserialised.

### `prune()` counts inside the transaction

The `after_count` moved inside the `with self._conn:` block. Previously it was read after the commit, which under concurrency is a second read of a table another thread may already have written to. Both counts now read the same snapshot. Behaviour is unchanged for the existing tests (D-16's single-statement `prune()` with `cursor.rowcount` is plan 22-04's work, not this one's).

### `_UPDATE_JOBS` and `_DELETE_JOBS` left unconsumed

Plan 22-02's docstring for `_UPDATE_JOBS` says its consumers "belong to the lock-and-transaction work". Checked against the actual later plans: **22-04 builds `_PRUNE` over `_DELETE_JOBS` and 22-05 builds `_FAIL_ACTIVE` over `_UPDATE_JOBS`.** Rewriting the two inline `"UPDATE jobs SET …"` literals here would have collided with plan 22-05, so they were left exactly as Wave 2 wrote them. No action needed by a later wave beyond what 22-05 already plans.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `src/saneless/job.py` modified in the RED commit to declare `_LOCKED_MARKER`**

- **Found during:** Task 1 (RED)
- **Issue:** The plan scopes Task 1's `<files>` to `tests/test_job.py` alone. But `.pre-commit-config.yaml` runs the `ty-checker` hook with `always_run: true` and `pass_filenames: false`, so it type-checks the whole project on every commit and rejects a test that names a symbol which does not exist. A RED commit containing only the test could not be committed.
- **Fix:** The bare `_LOCKED_MARKER = "__saneless_locked__"` constant (with its docstring) was added to `job.py` in the RED commit. Declaration only — no decorator, no marker assignment, no behaviour. This is the same resolution Waves 1-2 used for `StorageError` and `_COLUMNS`, and it is called out as the accepted precedent in this executor's briefing.
- **Behavioural RED/GREEN split preserved:** the decorator, the `setattr` marker stamp and the per-body transactions all landed in the GREEN commit, and the RED run genuinely failed on the absence of the marker on every method rather than on an `ImportError`.
- **Files modified:** `src/saneless/job.py`
- **Verification:** RED run showed `public JobStore methods missing the @_locked marker: close, create_job, get_job, list_recent, prune, update_state, update_thumbnail` — seven names, the real failure the test exists to catch.
- **Committed in:** `4fe2a57`

**2. [Rule 3 - Blocking] Two comment lines reworded to satisfy the plan's own `grep` acceptance criterion**

- **Found during:** Task 1 (RED)
- **Issue:** Task 1's acceptance criteria require `grep -c '_EXEMPT\|exempt' tests/test_job.py` to be `0`. The first draft's explanatory comment used the word "exemption" twice — prose about *not* having a roster, but the criterion is a literal text match.
- **Fix:** Reworded to "No opt-out list … A hand-written roster of excused names is where the next one gets quietly added." Same meaning, criterion satisfied.
- **Files modified:** `tests/test_job.py`
- **Verification:** `grep -c '_EXEMPT\|exempt' tests/test_job.py` → `0`
- **Committed in:** `4fe2a57`

---

**Total deviations:** 2 auto-fixed (both Rule 3 - blocking)
**Impact on plan:** No scope creep. Deviation 1 is the documented Waves 1-2 precedent for the project-wide `ty` gate; deviation 2 is cosmetic wording. Neither changes what was built.

## Issues Encountered

**`no_public_self_call` passes at RED, and that is correct.** The plan anticipated all three tests failing (via an `ImportError` on `_LOCKED_MARKER`). Because the constant had to be pre-declared for the `ty` gate, there was no `ImportError`, and nothing in `job.py` violated the no-public-self-call rule to begin with — Wave 2 had already routed the shared logic through the private undecorated `_row_to_job`. It is a *standing guard*, so its RED is a demonstrated detection rather than a red bar. Verified by temporarily inserting `self.get_job("deliberate-violation")` into `close()`: the test failed with `public JobStore methods calling other public methods: close -> get_job`. The violation was then reverted from a scratchpad backup and its absence confirmed before the RED commit. The selection as a whole exits non-zero at RED, satisfying the plan's automated verify.

**The stress test's RED was checked for luck, as the plan required.** It failed 5 runs out of 5 with `TypeError: 'NoneType' object is not subscriptable` — `create_job`'s in-transaction read-back returning nothing. That is precisely the unlocked failure signature RESEARCH measured on `:memory:` under `autocommit = False`, and precisely why D-14/D-29 forbade shipping an unlocked control run: the signature is not stable across `:memory:` vs. file. After GREEN it passed 10 runs out of 10.

**Worktree environment (known, not rediscovered).** `uv run pyrefly check` with no arguments cannot work in a Claude Code worktree — `.claude/worktrees/` is gitignored, so pyrefly checks nothing and exits 1, identically on unmodified `master`. Verified instead with `uv run pyrefly check src tests` → **0 errors**, and the two commits used `SKIP=pyrefly-checker`. Every other hook, including the project-wide `ty` hook, ran and passed. `--no-verify` was never used.

**Serena's language server failed to start** (`LanguageServerTerminatedException`, and its root pointed at the main repo rather than this worktree), so symbol-level edits fell back to `Edit` per the documented fallback order. No impact on the result.

## Verification

| Check | Result |
|---|---|
| `uv run pytest tests/test_job.py -k locked_coverage -q` | passed |
| `uv run pytest tests/test_job.py -k no_public_method_calls -q` | passed |
| `uv run pytest tests/test_job.py -k stress -q` | passed, 10 consecutive runs |
| `uv run pytest tests/test_job.py -k prune -q` | **3 passed unchanged** — D-16 evidence obligation |
| `uv run pytest tests/test_job.py -q` | 26 passed |
| `uv run pytest -m "not browser" -q` | **524 passed** (baseline entering the wave: 521) |
| stress test duration | **0.12 s** call, against the 60 s per-test timeout |
| warnings under `filterwarnings = ["error"]` | none |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |

Source assertions from the plan:

| Assertion | Expected | Actual |
|---|---|---|
| `grep -c '@_locked' src/saneless/job.py` | 7 (one per public method) | 7 |
| line immediately before `def close` | `    @_locked` | `    @_locked` |
| `grep -v '^ *#' src/saneless/job.py \| grep -c 'self._conn.commit()'` | 0 | 0 |
| `grep -c 'def _locked\[' src/saneless/job.py` | 1 | 1 |
| `close` body contains `with self._conn:` | no | no |
| `grep -c 'ThreadPoolExecutor' tests/test_job.py` | >= 1 | 2 |
| `grep -c 'threading.Thread(' tests/test_job.py` | 0 | 0 |
| `grep -c '_EXEMPT\|exempt' tests/test_job.py` | 0 | 0 |
| `grep -c 'time.sleep' tests/test_job.py` | exactly 2 (D-32) | 2 |
| `grep -rn 'noqa\|type: ignore' src/saneless/job.py` | nothing | nothing |
| tree-wide suppressions | 7, all pre-dating the phase | 7 (`config.py` x2, `scanner/__init__.py`, `sane_backend.py` x2, `test_cli.py`, `test_web.py`) |

## Threat Model Outcome

| Threat ID | Disposition | Evidence |
|---|---|---|
| T-22-03 | **mitigated** | `@_locked` on all seven public methods, proved reflectively with no exemption roster; the `ast` walk closes the non-nesting-transaction hazard `RLock` re-entrancy hides. |
| T-22-04 | **mitigated** | 2 threads x 200 rounds via `ThreadPoolExecutor` + `Future.result()`, zero exceptions, and all four data invariants asserted: `len(seen) == 400`, per-id last-state readback, `list_recent` reconciliation, and explicit `isinstance` deserialisation checks. |
| T-22-13 | **accepted, unchanged** | `close()` is `@_locked` with no transaction context and no `_closed` flag. Use-after-close remains reachable and is deliberately Phase 26's (D-15). Nothing from Phase 26 was folded back in. |
| T-22-SC | **accepted** | No packages installed. `uv.lock` untouched. |

No new security surface was introduced — no network endpoint, auth path, file access pattern or schema change. **No threat flags.**

## Known Stubs

None. Nothing in this plan is a placeholder, and nothing renders empty data to a UI.

## Next Phase Readiness

- **Plan 22-04 (`prune()` as one statement + `cursor.rowcount`)** — the method is decorated and its body already holds a single `with self._conn:`, so 22-04 replaces the statements inside an existing boundary. `_DELETE_JOBS` is waiting for `_PRUNE`. Note that 22-04 will remove the two `SELECT COUNT(*)` reads this plan just pulled inside the transaction.
- **Plan 22-05 (`fail_active_jobs`, `list_pending`)** — these are new *public* methods, so both structural tests apply to them automatically the moment they are written: they must be `@_locked` (the reflective test has no roster to add them to) and they must not call another public method. `_UPDATE_JOBS` is waiting for `_FAIL_ACTIVE`. Remember D-26: `sorted()` any `frozenset` before binding it into SQL parameters.
- **Phase 26** — inherits T-22-13 intact. `close()` has `@_locked` and a bare `self._conn.close()`; the `_closed` flag, the saneless-owned use-after-close error and the shutdown ordering are all still unclaimed, exactly as D-15 intended.
- **No blockers.**

---
*Phase: 22-job-store-hardening*
*Completed: 2026-09-10*

## Self-Check: PASSED

- `src/saneless/job.py` — found
- `tests/test_job.py` — found
- `.planning/phases/22-job-store-hardening/22-03-SUMMARY.md` — found
- commit `4fe2a57` — found
- commit `8a87373` — found
