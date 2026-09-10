---
phase: 22-job-store-hardening
plan: 04
subsystem: database
tags: [sqlite, prune, rowcount, s608, list-subquery, trace-callback, threadpoolexecutor, tdd]

# Dependency graph
requires:
  - phase: 22-job-store-hardening (plan 22-02)
    provides: "_DELETE_JOBS and _SELECT_JOBS, the name-held SQL verbs _PRUNE and _NEWEST_IDS are built over so S608 has nothing to flag"
  - phase: 22-job-store-hardening (plan 22-03)
    provides: "@_locked (lock only) and the per-body `with self._conn:` transaction shape prune() follows, plus the ast test that forbids a public method calling another"
  - phase: 21-vocabulary-and-contracts
    provides: "the JobStore surface the prune callers in cli.py and web/ reach through unchanged"
provides:
  - "_NEWEST_IDS: the newest-ids subquery, held under a name so the nested SELECT never appears in an interpolated string's literal text"
  - "_PRUNE: DELETE FROM jobs WHERE created_at < ? OR id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?), with the UTC lexicographic-ordering note at the site"
  - "prune() as one statement reporting cursor.rowcount, with both SELECT COUNT(*) reads and the subtraction removed"
  - "TestPruneSingleStatement: two shuffled-insert-order pins, a barrier-raced concurrent-insert reconciliation, and a trace-callback assertion that prune() runs exactly one row-touching statement"
affects: [22-05 fail_active_jobs and list_pending, 22-06, 32-test-sweep]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a nested SELECT inside an interpolated SQL constant is held under its own name, because S608 matches `select ... from` anywhere in the literal text, not only at its start"
    - "sqlite3.Connection.set_trace_callback as a test instrument for asserting how many statements a method executes"
    - "a fixed coprime stride as a deterministic permutation, so an ordering test shuffles the same way on every run"

key-files:
  created: []
  modified:
    - src/saneless/job.py
    - tests/test_job.py

key-decisions:
  - "D-16 implemented literally: one DELETE unioning the age and row-cap predicates, count taken from cursor.rowcount, statement byte-identical to the SQL in CONTEXT.md"
  - "_NEWEST_IDS was added beyond the plan's interface list: writing the subquery inline inside _PRUNE tripped S608, and CLAUDE.md forbids the suppression that would silence it (deviation Rule 3)"
  - "a fourth test was added beyond the plan's two, because D-16's own equivalence argument makes the planned two non-discriminating; see Deviations"
  - "the shuffled-order tests assert the exact survivor SET, not only the count, per D-30 and threat T-22-05"
  - "the three pre-existing prune tests are byte-identical and their two time.sleep(0.01) calls are untouched (D-16, D-32)"
  - "no RED-commit symbol stub was needed: the tests name only prune, create_job, list_recent and _conn, all of which already existed"

patterns-established:
  - "Pattern: when a lint rule keys on literal text rather than on the assembled value, split the literal behind a name rather than reaching for a suppression"
  - "Pattern: when a refactor is provably equivalent, the honest RED signal is the property that actually changed (here, the provenance of the count), not the behaviour that did not"

requirements-completed: [STOR-04]

# Metrics
duration: 34min
completed: 2026-09-10
---

# Phase 22 Plan 04: prune() as One Statement Summary

`prune()` now issues a single `DELETE` that unions the age cutoff and the row cap and reports `cursor.rowcount`, replacing two `SELECT COUNT(*)` reads straddling two `DELETE`s, and the undocumented SQLite `LIST SUBQUERY` materialisation the collapse depends on is pinned by tests instead of assumed.

## What Was Built

### `src/saneless/job.py`

`_NEWEST_IDS` and `_PRUNE` join the module's statement constants, both assembled from the name-held verbs plan 22-02 declared. `_DELETE_JOBS`, which 22-02 deliberately left unconsumed, now has its consumer, and its docstring no longer claims otherwise.

The assembled statement is byte-identical to D-16's:

```
DELETE FROM jobs WHERE created_at < ? OR id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)
```

`prune()` keeps `@_locked`, its exact signature and its defaults, opens its own `with self._conn:` per Wave 3's shape, and calls no other public method. Its body is now three statements: compute the cutoff, execute, read `.rowcount`.

`_PRUNE`'s docstring carries three things at the site rather than in a plan document: the D-16 latent UTC assumption (both the `<` and the `ORDER BY` are lexicographic and correct only because every value is written as `datetime.now(tz=UTC).isoformat()`), the union-versus-sequence equivalence argument, and the fact that the equivalence rests on a query-plan behaviour SQLite declines to guarantee — so it is pinned by test, not by contract.

### `tests/test_job.py` — `TestPruneSingleStatement`

| Test | What it pins |
|---|---|
| `test_prune_shuffled_order_retains_the_newest_rows` | 60 rows inserted in rowid order, then backdated to a fixed coprime permutation so scan order and `created_at` order disagree; asserts the count is exactly 30 **and** the survivor set is exactly the newest 30 |
| `test_prune_shuffled_order_with_an_age_cutoff_active` | the same under an active age cutoff, with 20 rows backdated 30 days; asserts `deleted > SHUFFLE_EXPIRED` so the case provably exercises the union rather than either predicate alone |
| `test_prune_concurrent_insert_cannot_corrupt_the_count` | one `create_job` raced against `prune()` on a `threading.Barrier(2)` via `ThreadPoolExecutor`; asserts `after == before + 1 - deleted`, which holds under both interleavings, and that the count is never `-1` and never exceeds the pre-call row count |
| `test_prune_concurrent_window_between_count_and_delete_is_closed` | via `set_trace_callback`, that `prune()` executes exactly **one** row-touching statement, that it is a `DELETE`, and that no `COUNT(` appears in it |

The permutation is a written-out stride of 37 over 60 rows rather than an unseeded `random` shuffle, so a failure is reproducible.

## Deviations from Plan

### 1. [Rule 3 — Blocking] `_NEWEST_IDS` added; the subquery could not be written inline

**Found during:** Task 2.

**Issue:** The plan's interface list specifies one new constant, `_PRUNE`, built as an f-string over `_DELETE_JOBS`. Written that way, ruff reports `S608 Possible SQL injection vector through string-based query construction`. Research § Mechanic 7 recorded approach A (name-held verb prefix) as `S608`-clean, and it is — but the case it was verified on (`_FAIL_ACTIVE`) has no nested `SELECT`. `S608` matches `select\s+.+\bfrom\s` **anywhere** in the interpolated string's literal text, not only at its start, so the inline `SELECT id FROM jobs ORDER BY ...` trips it regardless of what the verb prefix does. CLAUDE.md forbids the `# noqa` that would silence it.

**Fix:** the subquery moved behind its own constant, `_NEWEST_IDS`, built from `_SELECT_JOBS` in exactly the way `_SELECT_ALL` and `_SELECT_RECENT` already are. `_PRUNE`'s literal text then contains no SQL verb at all, and both constants are ordinary strings the rule has nothing to say about. The assembled statement is unchanged.

**Files modified:** `src/saneless/job.py`. **Commit:** `22d954c`.

This generalises for plan 22-05: any statement with a nested `SELECT` needs the same treatment.

### 2. [Rule 2 — Missing critical coverage] A fourth test, because the planned two cannot be RED

**Found during:** Task 1.

**Issue — reported loudly, as the plan's constraints require.** The plan instructs that if a new test passes against the old implementation, it is not discriminating and must be strengthened "rather than proceeding". Both planned tests were written to their strongest form — exact count *and* exact survivor set, no inequalities — and **both still passed against the two-statement implementation.** That is not a weak test; it is a structural consequence of two facts the plan itself establishes:

1. **D-16's equivalence argument is exactly right.** The union and the sequential composition produce the same delete set in every case, so *no* single-threaded behavioural test can separate them. Strengthening cannot fix this, because there is no behavioural difference to detect.
2. **Wave 3's lock already closed the in-process race.** `prune()` and `create_job` are serialised on the same `RLock`, so a barrier-raced insert cannot land between the old two `COUNT(*)` reads either. The planned concurrent test therefore also passes pre-change.

The two planned tests are genuine and valuable — they are the D-30 / T-22-05 pins against a future libsqlite changing the query plan — but they are pins, not discriminators, and TDD needs a real RED.

**Fix:** added `test_prune_concurrent_window_between_count_and_delete_is_closed`, which observes through `sqlite3.Connection.set_trace_callback` how many row-touching statements `prune()` executes. This is the property that actually changed and the one ROADMAP criterion 5 names — the *provenance* of the count. It failed RED with a precise message (`prune() ran 4 row-touching statements: SELECT COUNT(*) FROM jobs; DELETE ...; DELETE ...; SELECT COUNT(*) FROM jobs`) and passes GREEN.

It also pins the threat register honestly. T-22-14 claims the count is exact "rather than only masking it behind plan 22-03's lock" — the trace assertion is what distinguishes those two, and without it the claim would have been untested.

**Files modified:** `tests/test_job.py`. **Commit:** `d1e005c`.

### 3. [Rule 1 — Stale documentation] `_DELETE_JOBS`'s docstring corrected

Its docstring said it was "used by nothing yet"; `_PRUNE` now consumes it. Corrected in the same commit as `_PRUNE`.

## TDD Gate Compliance

| Gate | Commit | Evidence |
|---|---|---|
| RED | `d1e005c` `test(22-04): ...` | `uv run pytest tests/test_job.py -k "prune_shuffled or prune_concurrent" -q` → `1 failed, 3 passed`, exit 1 |
| GREEN | `22d954c` `feat(22-04): ...` | same selection → `4 passed`; full suite `528 passed, 8 deselected` |
| REFACTOR | not needed | the GREEN implementation is three lines; there was nothing to clean up |

Both gates present and in order. Unlike Waves 1–3, the RED commit needed **no bare-symbol stub**: the tests name only `prune`, `create_job`, `list_recent` and `_conn`, all of which already existed, so the project-wide `ty` hook had nothing to reject.

The three-passed-at-RED result is the honest signal discussed in Deviation 2, not a slack test.

## Verification

| Gate | Result |
|---|---|
| `uv run pytest -m "not browser" -q` | **528 passed, 8 deselected** (baseline 524 + 4 new) |
| `uv run pytest tests/test_job.py -k prune -q` | 7 passed — the 3 pre-existing plus the 4 new |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| Protected prune tests | `test_prune_by_age`, `test_prune_by_count`, `test_prune_no_deletions` **byte-identical** to the wave base |
| `time.sleep` count in `tests/test_job.py` | 2 — unchanged (D-32) |
| `threading.Thread(` count | 0 (D-29) |
| Suppressions in tree | 7, all pre-dating this phase; 0 in `src/saneless/job.py` |
| `SELECT COUNT(*)` in `job.py` | 0 |
| `self._conn.execute(` in `prune()` | 1 |
| Files changed vs. wave base | `src/saneless/job.py`, `tests/test_job.py` only; **0 deletions** |

`STATE.md` and `ROADMAP.md` were not touched — the orchestrator owns those.

## Threat Model Verification

| Threat ID | Disposition | Evidence |
|---|---|---|
| T-22-05 | mitigated | `test_prune_shuffled_order_*` assert the exact survivor set against a `created_at` order deliberately disagreeing with insertion order. A planner change that stops materialising the `LIST SUBQUERY` goes red rather than under-deleting quietly. Verified here on libsqlite **3.34.1**; CI ships newer, which is the point of the pin. |
| T-22-14 | mitigated | Both `COUNT(*)` reads are gone; the count is `cursor.rowcount` on the one `DELETE`. `test_prune_concurrent_window_between_count_and_delete_is_closed` asserts exactly one row-touching statement, so the mitigation is verified structurally and not merely inherited from Wave 3's lock. `test_prune_concurrent_insert_cannot_corrupt_the_count` reconciles the count against the row-count delta under a real race. |
| T-22-15 | accepted, control in place | The UTC lexicographic assumption is stated in `_PRUNE`'s docstring at the site. No code path writes a non-UTC timestamp; `create_job` writes `datetime.now(tz=UTC).isoformat()`. |
| T-22-06 | mitigated | The only interpolated values in `_PRUNE` and `_NEWEST_IDS` are module-level literals; the cutoff and `max_rows` are bound `?` parameters. Ruff reports zero `S608` with zero suppressions. |
| T-22-SC | accepted | No packages installed. |

No new threat surface: no endpoint, auth path, file access or schema change. `worker.py`, `web/app.py`, `web/routes.py` and `cli.py` were not touched, and `prune()`'s signature and defaults are unchanged, so their callers are unaffected.

## Known Stubs

None.

## For Plan 22-05

- **A nested `SELECT` in an interpolated constant needs its own name.** `_FAIL_ACTIVE` over `_UPDATE_JOBS` has no subquery and will be clean as researched, but if `list_pending` or anything else grows one, follow `_NEWEST_IDS`.
- **`_UPDATE_JOBS` is still unconsumed** and its docstring still says so; 22-05 should correct it when it builds `_FAIL_ACTIVE`.
- **A provably-equivalent refactor has no behavioural RED.** If 22-05 hits the same shape, the discriminating property is the one that actually changed, and `set_trace_callback` is now an established instrument for observing it.

## Self-Check

- `src/saneless/job.py` — FOUND
- `tests/test_job.py` — FOUND
- `.planning/phases/22-job-store-hardening/22-04-SUMMARY.md` — FOUND
- commit `d1e005c` — FOUND
- commit `22d954c` — FOUND

## Self-Check: PASSED
