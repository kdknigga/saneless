# Phase 22: Job Store Hardening - Research

**Researched:** 2026-09-10
**Domain:** Python 3.14 `sqlite3` transaction control, threading, schema migration, decorator typing
**Confidence:** HIGH (nine of the ten mechanics were settled by running code on this machine; the two remaining are flagged below)

## Summary

Every locked decision in `22-CONTEXT.md` is implementable, and the ladder, the lock, the single-statement `prune()`, the `_COLUMNS`/`sqlite3.Row` mapping and the reflective test were all built as working prototypes on this machine and verified against `ruff`, `ty`, `pyrefly` and `pytest` under the project's own configuration. The 200-round two-thread stress test runs in **0.28 s** — two hundred times inside Phase 20's 60-second timeout — with zero exceptions, zero warnings, and the data invariant holding.

Three findings change the shape of the plan and none of them are visible from reading the code:

1. **`PRAGMA journal_mode=WAL` raises `OperationalError: cannot change into wal mode from within a transaction` once `conn.autocommit = False` is set — but only on a file-backed database.** On `:memory:` it silently returns `'memory'` and passes. Every test in this project uses `:memory:`. Setting WAL after the autocommit flip therefore produces a store that passes the entire suite and crashes on the operator's machine at first open. The fix is one line of ordering, and it must be pinned by a `tmp_path` test.
2. **`@_locked` cannot wrap `close()` in the connection's transaction context.** `with self._conn: self._conn.close()` raises `ProgrammingError: Cannot operate on a closed database` from `Connection.__exit__`. This is a direct collision between D-12's parenthetical ("and the connection's transaction context") and D-15 ("`close()` gets `@_locked` and nothing else"). The decorator must take the **lock only**; the transaction context belongs in the method bodies.
3. **Ruff's `S608` fires on the `_COLUMNS`-driven SQL that STOR-04 requires**, and CLAUDE.md forbids both `# noqa` and disabling the rule. There is a clean, suppression-free construction — verified — but the planner must choose it deliberately rather than discover it at implementation time.

Beyond those, the D-16 equivalence argument was re-verified across nine cases (including the three worked in discussion) with byte-identical results, `cursor.rowcount` is exact, the ladder opens fresh / S3 / S3-via-bare-`ALTER` databases correctly and fails loudly and *atomically* on an S2 database, and both type checkers accept the `ParamSpec` decorator and `sqlite3.Row` with zero diagnostics.

**Primary recommendation:** Order `__init__` as `connect` → `row_factory` → `PRAGMA journal_mode=WAL` → `autocommit = False` → ladder; make `@_locked` lock-only and put `with self._conn:` inside the bodies; build every dynamic SQL string as a module-level constant whose leading token is a *name*, not a literal SQL verb.

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Serialising concurrent store access | Database access layer (`JobStore`) | — | `check_same_thread=False` is a promise the owner of the connection must keep; no other module holds a reference to it |
| Schema evolution | Database access layer (`job.py` module scope) | — | D-02 locks the ladder into `job.py` where both type checkers can see it |
| Transaction boundaries | Database access layer | — | Under `autocommit = False` a transaction is always open; nothing outside `JobStore` may leave one dangling |
| Result-column semantics | Domain vocabulary (`vocabulary.py`) | Database access layer | `ScanOutcome` round-trips through `job.py`; `vocabulary.py` stays a leaf |
| Active/pending state predicates | Domain vocabulary (`ACTIVE_STATES`) | Database access layer | Phase 21 D-04: derive the SQL predicate from the frozenset, never hand-list states |
| Retention policy (`max_age_days`, `max_rows`) | Configuration (`output.history_*`) | Database access layer | Already the case; unchanged this phase |

## User Constraints (from CONTEXT.md)

### Locked Decisions

Copied in summary form; **`22-CONTEXT.md` is authoritative and must be read in full.**

- **D-01** The ladder owns table creation. `CREATE TABLE IF NOT EXISTS` is deleted from `__init__`; migration step 1 *is* the `CREATE TABLE` at the S3 ten-column shape.
- **D-02** `_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...]` at module scope in `job.py`; index + 1 is the version. No `.sql` files, no migration library.
- **D-03** Fresh vs. legacy is decided by probing for the `jobs` table: `user_version = 0` and no table → fresh, run everything; `user_version = 0` and table exists → legacy, enter after step 1; `user_version = N > 0` → run `N+1`..head.
- **D-04** Only the S3 shape is a supported legacy input. S1/S2 ladder steps are deliberately **not** written. N-13's "test that opens a first-release schema" means a fixture built at the S3 (`dc9b8af`) shape.
- **D-05** Step 2 reads `PRAGMA table_info(jobs)`, asserts the ten S3 columns, and **raises** naming the database path and the missing columns if any are absent. No self-healing.
- **D-06** Physical column order is not a contract. Never assume ordinal position.
- **D-07** All six new columns become columns **and** `Job` dataclass fields with defaults, read back through the single row mapping. No production writers this phase. A deliberate, bounded exemption from Phase 21's D-06 that covers columns and their mirroring fields only.
- **D-08** `pages_scanned`, `pages_removed`, `pages_uploaded` are nullable `INTEGER` → `int | None = None`. `NULL` means "never recorded". Never `NOT NULL DEFAULT 0`.
- **D-09** `outcome` is `TEXT` → `ScanOutcome | None`, round-tripped exactly like `state`/`error_category`. `warning` and `owner_token` are `TEXT` → `str | None = None`.
- **D-10** `owner_token`'s semantics are decided elsewhere; this phase picks the column shape only.
- **D-11 (MANDATORY)** Re-sweep OUTC, DPLX and APPL before writing step 2; call out APPL-03 specifically. **Answered below in § D-11 Sweep.**
- **D-12** `@_locked` on every public method, enforced by a reflective `inspect`-based test with **no exemption list**.
- **D-13** `conn.autocommit = False`.
- **D-14** The 200-round two-thread test asserts zero exceptions **and** a data invariant. No deliberate no-lock control run.
- **D-15 (diverges from recommendation)** `close()` gets `@_locked` and nothing else. No `_closed` flag, no use-after-close error — Phase 26's.
- **D-16** `prune()` becomes one combined `DELETE` reporting `cursor.rowcount`. The three existing prune tests must pass **unchanged**. Add a concurrent-insert test. Comment the UTC/lexicographic latent assumption.
- **D-17** `_COLUMNS` tuple + `sqlite3.Row` + a single `_row_to_job`.
- **D-18 (diverges from recommendation)** Do **not** centralize the `tmp_dir / "saneless.db"` path. Phase 23's.
- **D-19** "FAILED" everywhere means the existing `JobState.ERROR`. No new `JobState` member, no rename.

### Claude's Discretion

- `fail_active_jobs()`'s signature — `fail_active_jobs(reason)` or a fixed "server restarted"; whether it also sets `error_category` is open (N-14: `error_category` is written but never read).
- `list_pending()`'s exact predicate and ordering — confirm against `ACTIVE_STATES` rather than hand-listing.
- Whether the ladder runs inside `__init__` or an explicit `migrate()` called from it — every test constructs `JobStore()` directly, so it must work with no extra call.
- Whether `PRAGMA journal_mode=WAL` survives — keep, drop, or make conditional, **but do not let it become the reason a migration step behaves differently in tests than in production.** (This is now the single most load-bearing discretion item; see § Pitfall 1.)
- Task and commit breakdown, provided the ladder and `_COLUMNS`/`_row_to_job` land before anything depending on them.
- The exact prose of the storage documentation update, which must not describe any of the six new columns as having a consumer yet.

### Deferred Ideas (OUT OF SCOPE)

Calling `fail_active_jobs()` at startup (Phase 26) · writing `outcome`/`warning` (Phase 23) · writing and rendering the page counts and `owner_token` (Phase 30) · moving the DB off `tmp_dir` (Phase 23) · centralizing the duplicated db path (Phase 23) · `close()` hardening and shutdown ordering (Phase 26) · worker resilience, 429 backpressure, `def` routes (Phase 26) · giving `error_category` a consumer (Phase 30) · a `pages_back`/duplex-split column **unless D-11's sweep demands it** · `PRAGMA application_id` · renaming `JobState.ERROR` (rejected outright).

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| STOR-01 | Two threads can call any mix of `JobStore` methods concurrently for 200 rounds without an exception (`RLock` around every public method; public methods never call other public methods) | § Pattern 2 (`@_locked`), § Pitfall 2 (`close()` collision), § Pitfall 3 (non-nesting connection CM makes "never call other public methods" a *correctness* rule), § Validation Architecture (verified 0.28 s, 0 exceptions, invariant holds) |
| STOR-02 | `PRAGMA user_version` ladder; a v1.0 database opens and migrates cleanly; the bare `ALTER … except: pass` is gone | § Pattern 1 (ladder, verified end to end on fresh / S3 / S3-via-`ALTER` / S2), § Verified Mechanic 1 (DDL + version stamp commit atomically), § Pitfall 1 (WAL ordering) |
| STOR-03 | Six new columns added in one migration | § Pattern 1 step 2, § D-11 Sweep (six confirmed sufficient), § Verified Mechanic 1 (`ALTER` is transactional and rolls back with the guard) |
| STOR-04 | Row mapping and column list exist once; `prune()` counts from one statement | § Pattern 3 (`_COLUMNS`/`_row_to_job`), § Pitfall 4 (S608 vs. CLAUDE.md), § Verified Mechanic 5 (nine-case equivalence + `rowcount`) |
| STOR-05 | `fail_active_jobs()` and `list_pending()` | § Pattern 4 (deriving predicates from `ACTIVE_STATES`, three suppression-free ways to bind a variable-length `IN`) |

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| CPython | everything | ✓ | 3.14.2 | — |
| `sqlite3` stdlib module | `job.py` | ✓ | bundled | — |
| **libsqlite3 (this machine)** | transaction/ladder behaviour | ✓ | **3.34.1** | — |
| `uv` | all commands | ✓ | project standard | — |
| `ruff` | lint/format gate | ✓ | ≥0.15.5 | — |
| `ty` | type gate | ✓ | ≥0.0.80 | — |
| `pyrefly` | type gate | ✓ | ≥1.2.0 | — |
| `pytest` + `pytest-timeout` | suite | ✓ | ≥9.0.2 / ≥2.4.0 | — |
| SQLite JSON1 extension | *only* if the `json_each(?)` option in § Pattern 4 is chosen | ✓ here (`ENABLE_JSON1`) | — | the module-constant form, which needs nothing |

**Missing dependencies with no fallback:** none.

**Note on libsqlite version drift.** This machine ships **3.34.1** (RHEL 9). GitHub Actions runners ship a much newer SQLite, and the Docker image will ship a third. Every behaviour reported below was observed on 3.34.1; the transaction semantics are stable across that range, but the `LIST SUBQUERY` query plan that underwrites § Verified Mechanic 5 is a planner decision and is *not* contractually stable. See § Pitfall 6.

## Verified Mechanics

Each item states **VERIFIED (executed)**, **VERIFIED (cited)** or **INFERRED**. Everything marked executed was run on this machine, Python 3.14.2 / libsqlite 3.34.1, and the observed output is reproduced.

### Mechanic 1 — `conn.autocommit = False` and the migration ladder (item 1)

**VERIFIED (executed) + VERIFIED (cited).**

```
in_transaction at open (before any stmt): True
read user_version -> (0,) | in_transaction after read: True
after CREATE TABLE, in_transaction: True
after SET user_version, in_transaction: True
user_version inside txn: 1
after rollback -> user_version: 0
table t after rollback: no such table: t
```

| Question | Answer | Evidence |
|---|---|---|
| Can `CREATE TABLE` and `PRAGMA user_version = N` commit atomically in one transaction? | **Yes.** A rollback discarded both the table *and* the version stamp. | executed, above |
| Does reading `PRAGMA user_version` open a transaction? | Under `autocommit = False` a transaction is *already* open before any statement runs, so the question is moot — `in_transaction` is `True` immediately after the assignment. | executed; docs: "Changing `autocommit` to `False` opens a new transaction" and "`sqlite3` ensures that a transaction is always open … `sqlite3` uses `BEGIN DEFERRED`" |
| Does `PRAGMA table_info` work inside a transaction? | Yes — returns column rows normally; returns `[]` (not an error) for a missing table. | executed |
| Does `ALTER TABLE … ADD COLUMN` roll back? | Yes. After a guard raised, the columns were gone and `user_version` was still `0`. | executed (ladder run, S2 case) |
| Does `PRAGMA journal_mode=WAL` work inside a transaction? | **No — it raises on a file database and silently succeeds on `:memory:`.** | executed; see § Pitfall 1 |
| Are there other statements SQLite refuses inside a transaction? | `VACUUM` and `PRAGMA journal_mode` changes are the two the ladder could plausibly reach. The ladder needs neither once WAL is moved. | INFERRED from the SQLite grammar; only WAL was tested |
| Is `executescript()` safe in the ladder? | **No.** The docs state it "implicitly commits any pending transaction before running a script". Using it would break the atomicity of a step's DDL plus its version stamp. Use `execute()` per statement. | VERIFIED (cited), Python 3.14 `sqlite3` docs § Transaction control |
| Can `PRAGMA user_version = ?` be parameterised? | **No** — `OperationalError: near "?": syntax error`. It must be interpolated. The value is an `int` from `enumerate`, so this is safe, and ruff does not flag `PRAGMA` under `S608`. | executed |

**Why D-13 is right, in the docs' own words** (Python 3.14 `sqlite3`):

> If there is no open transaction upon leaving the body of the `with` statement, or if `autocommit` is `True`, the context manager does nothing.

and, for the default mode:

> If `autocommit` is `LEGACY_TRANSACTION_CONTROL`, `isolation_level` is not `None`, *sql* is an `INSERT`, `UPDATE`, `DELETE`, or `REPLACE` statement, and there is no open transaction, a transaction is implicitly opened before executing *sql*.

DDL and `PRAGMA` are not in that list. So under the legacy default, `with self._conn:` around a migration step is genuinely a no-op — exactly the hazard D-13 names.

### Mechanic 2 — `@_locked` and the type checkers (item 2)

**VERIFIED (executed).** The following passed `ruff check`, `ruff format --check`, `ty check` and `pyrefly check` with **zero** diagnostics when placed in `src/saneless/`:

```python
def _locked[**P, R](
    method: Callable[Concatenate[JobStore, P], R],
) -> Callable[Concatenate[JobStore, P], R]:
    """Serialise a JobStore method on the store's lock."""

    @functools.wraps(method)
    def wrapper(self: JobStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    setattr(wrapper, _LOCKED_MARKER, True)
    return wrapper
```

Details that matter:

- **Use PEP 695 syntax (`def _locked[**P, R]`), not `ParamSpec`/`TypeVar` objects.** Ruff's `UP047` *requires* it: a `ParamSpec`+`TypeVar` version produced `UP047 Generic function '_locked' should use type parameters`. Both spellings type-check identically; only the PEP 695 one lints clean.
- **`Concatenate[JobStore, P]` is what makes `self` typed.** With a plain `Callable[P, R]` the bound-method view still works, but `self` inside the wrapper is untyped and `self._lock` becomes unresolvable.
- **`setattr(wrapper, _LOCKED_MARKER, True)` where `_LOCKED_MARKER` is a module constant.** Writing `wrapper.__saneless_locked__ = True` makes both checkers complain that a function has no such attribute; writing `setattr(wrapper, "__saneless_locked__", True)` with a *literal* trips ruff's `B010`. Passing the name through a module-level constant satisfies all three. This is not a trick — the constant is also what the test imports, so the marker name exists in exactly one place.
- **Neither checker complains about `with self._lock:` reaching a private attribute** from a module-level function, because `_locked` and `JobStore` share a module.
- **`functools.wraps` preserves everything the reflective test needs**: `__name__`, `__qualname__`, `__wrapped__`, and `inspect.signature()` (verified: `signature(JobStore.get).parameters.keys() == {"self", "job_id"}`).

**Reliable marker detection.** Verified behaviour of the three candidate signals:

| Signal | Verdict |
|---|---|
| Custom attribute via `getattr(member, _LOCKED_MARKER, False)` | **Use this.** Survives `functools.wraps`, unambiguous, and cannot be produced by accident. |
| `hasattr(member, "__wrapped__")` | Rejected — *any* `functools.wraps` decorator sets it, so an unrelated decorator would read as "locked". |
| `__qualname__` | Rejected — `functools.wraps` copies the *wrapped* function's qualname, so it is identical whether locked or not. |

One residual hazard, measured: `functools.wraps` copies `__dict__` from the wrapped function upward, so `@other_decorator` stacked **over** `@_locked` still reports the marker (correct — the lock is still applied), and `@_locked` stacked over `@other_decorator` also reports it (correct). The failure mode is safe: removing `@_locked` removes the marker in every stacking order tested.

Recommended test predicate — `inspect.getmembers(JobStore, inspect.isfunction)` rather than `callable`, because `callable` would silently skip a public `@property` or a `@staticmethod` while `isfunction` at least surfaces the latter. Neither predicate catches a public `@property`; if one is ever added, the reflective test is blind to it. Worth one line of comment in the test.

### Mechanic 3 — the migration ladder shape (item 3)

**VERIFIED (executed).** A full prototype ladder was run against five database shapes:

```
fresh :memory:            version = 2  cols = 16
fresh file                version = 2  journal = wal
reopen (idempotent)       version = 2  cols = 16
legacy S3                 version = 2  cols = [id … created_at, outcome, pages_scanned,
                                               pages_removed, pages_uploaded, warning, owner_token]
  row survived: {'id': 'x', 'title': 'T', 'outcome': None, 'pages_scanned': None}
legacy S3-via-bare-ALTER  version = 2  cols = [… created_at, error_category, outcome, …]   <- error_category LAST
legacy S2                 raised: missing: ['error_category']
  after failed open: user_version = 0  cols = [id … created_at]   <- untouched
```

Findings:

- **Each step gets its own transaction, and the version stamp goes inside it.** The loop is `for index in range(version, len(_MIGRATIONS)): _MIGRATIONS[index](conn); conn.execute(f"PRAGMA user_version = {index + 1}"); conn.commit()`. A shared transaction across the whole ladder would work too, but per-step commits mean a two-step ladder that fails at step 2 leaves a *valid* version-1 database rather than nothing — and with only one table and two steps the difference is immaterial today. **Per-step is the idiomatic and the safer default; recommend it.**
- **D-06 is confirmed empirically.** The S3-via-bare-`ALTER` database really does carry `error_category` last, and the ladder handled it because it never touches ordinals.
- **The D-05 guard fails atomically.** On the S2 database the guard raised before any `ALTER` ran; the rollback left `user_version = 0` and the original nine columns. Nothing half-applied.
- **The connection must be cleaned up on the failure path.** `__init__` should `rollback()` and `close()` before re-raising, otherwise a failed `JobStore(path)` leaks an open connection holding a `BEGIN DEFERRED`. Verified that `rollback()` + `close()` after a guard raise works cleanly.
- **Fresh-vs-legacy probe.** `SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'` is the right probe. `PRAGMA table_info(nope)` returns `[]` rather than raising, so it *could* also serve, but `sqlite_master` states the intent.

**Which exception type should the D-05 guard raise?** `src/saneless/exceptions.py` currently holds `SanelessError` (base), `ConfigError`, `ScanError`, `FeederEmptyError`, `PaperlessError`. **None of them fits** — a corrupt-shaped database is not a configuration error, a scanner error, or a Paperless error. Recommendation, in order:

1. **Add `StorageError(SanelessError)` to `exceptions.py`** and raise it. `exceptions.py` is a leaf (`vocabulary.py` imports it), so `job.py` may import from it freely and nothing is inverted. This is one class and one docstring, it gives Phase 28 (EXC-01/EXC-02) a target to translate *into* rather than a bare `RuntimeError` to discover later, and it gives Phase 26 the base class it will want when it adds the use-after-close error. Note `EXC-02` already lists the CLI's catch set as `ConfigError, ScanError, PaperlessError, PdfError` — a fifth type is exactly the kind of thing Phase 28 exists to reconcile, so introducing it now is not pre-empting that phase, it is feeding it.
2. Fallback if the planner wants zero new vocabulary: raise `ConfigError`. Defensible only on the reading that a database at an unexpected shape is a mis-configured deployment; the message would still name the path and the missing columns. Weaker, because it will be re-typed in Phase 28 anyway.

Do **not** raise a bare `sqlite3.OperationalError` or `RuntimeError` — `EXC-01` explicitly forbids third-party exception types escaping a module boundary, and a bare `RuntimeError` gives the CLI nothing to catch.

### Mechanic 4 — testing the concurrency claim (item 4)

**VERIFIED (executed).** Ran under this project's `pyproject.toml` pytest configuration (`filterwarnings = ["error"]`, `timeout = 60`, `timeout_method = "signal"`, `--strict-config`, `--strict-markers`):

```
tests/test_proto_stress.py ...                                          [100%]
0.28s call     tests/test_proto_stress.py::test_two_threads_200_rounds
3 passed in 0.90s
```

Design that produced that (deterministic, no `time.sleep`, warning-free):

- **`concurrent.futures.ThreadPoolExecutor(max_workers=2)` + `future.result()`.** `Future.result()` re-raises the worker's exception in the calling thread. This is the mechanism that makes thread exceptions fail the test. Raw `threading.Thread` **swallows** exceptions into `threading.excepthook`, and the test would pass green while the store was broken. If raw threads are preferred for some reason, the alternative is a `list` that each thread appends its exception to plus an `assert errors == []`, or overriding `threading.excepthook` — both strictly worse than `Future.result()`.
- **`threading.Barrier(2)` with a timeout** to start both threads in the same instant. No sleeping, no polling, and it cannot hang past the barrier timeout.
- **A `threading.Lock`-guarded plain `dict[job_id, expected_state]`** accumulated by both threads, checked after the join.

**The data invariant that is genuinely checkable** (this is what speaks to criterion 1's *"no interleaved-transaction corruption"*, per D-14):

1. `len(seen) == 2 * rounds` — every insert committed exactly once; no lost or duplicated write.
2. For every recorded `job_id`, `get_job(job_id)` returns a `Job` whose `state` equals the last state that thread set. This is the one that catches an interleaved transaction: a half-committed `UPDATE` shows up as a stale state.
3. `list_recent(limit=huge)` length reconciles with `len(seen)` minus anything pruned.
4. Every returned `Job` deserialises — `JobState(row["state"])` and `json.loads(row["tags"])` both succeed. A torn write surfaces here as a `ValueError`/`JSONDecodeError` rather than a silent wrong answer.

For the record, the *unlocked* store on Python 3.14 with `autocommit = False` fails differently from the review's baseline (which ran under the legacy default): observed `TypeError: 'NoneType' object is not subscriptable` (`:memory:`) and `InterfaceError: bad parameter or other API misuse` (file). This is exactly why D-14 rejected a deliberate no-lock control run — the failure signature is not stable. **Do not write one.**

**Budget:** 0.28 s of a 60 s timeout. There is ample headroom to raise the round count or add store methods to the mix; there is no reason to lower it.

### Mechanic 5 — `prune()` as one statement (item 5)

**VERIFIED (executed)** for the equivalence and the rowcount; **INFERRED / not contractually guaranteed** for the subquery-snapshot question. Both parts matter and they are different.

**Equivalence — verified.** The old two-`DELETE` composition and D-16's single statement were run side by side on nine cases, including the three worked during discussion. Identical `(deleted, remaining)` in every case:

```
n= 600 expired= 200 max_rows= 500   old=(200,400)  new=(200,400)  MATCH
n= 600 expired=  50 max_rows= 500   old=(100,500)  new=(100,500)  MATCH
n= 600 expired=   0 max_rows= 500   old=(100,500)  new=(100,500)  MATCH
n=   5 expired=   0 max_rows=   3   old=(2,3)      new=(2,3)      MATCH
n=   2 expired=   0 max_rows= 500   old=(0,2)      new=(0,2)      MATCH
n=  10 expired=  10 max_rows=   5   old=(10,0)     new=(10,0)     MATCH
n=   0 expired=   0 max_rows= 500   old=(0,0)      new=(0,0)      MATCH
n= 600 expired= 600 max_rows= 500   old=(600,0)    new=(600,0)    MATCH
n= 100 expired=  30 max_rows=  30   old=(70,30)    new=(70,30)    MATCH
```

The `new` column's first number is `cursor.rowcount`, not a recomputed count — so `rowcount` is exact for this `DELETE` in all nine cases, including the two zero-deletion cases where it correctly reported `0` (not `-1`).

**`cursor.rowcount` — VERIFIED (cited) and executed.** Python 3.14 docs:

> **rowcount** – Read-only attribute that provides the number of modified rows for `INSERT`, `UPDATE`, `DELETE`, and `REPLACE` statements; is `-1` for other statements, including CTE queries. It is only updated by the `execute()` and `executemany()` methods, after the statement has run to completion. This means that any resulting rows must be fetched in order for `rowcount` to be updated.

A `DELETE` produces no result rows, so the "must be fetched" caveat does not apply — `rowcount` is valid immediately after `execute()`. Observed `500`, `0`, and every value in the table above.

**Does the `NOT IN` subquery see partial deletions? — the honest answer.**

- The query plan materialises it. `EXPLAIN QUERY PLAN` for the exact D-16 statement on 1000 rows returns:
  ```
  (3, 0, 0, 'SCAN TABLE jobs')
  (10, 0, 0, 'LIST SUBQUERY 1')
  (16, 10, 0, 'SCAN TABLE jobs')
  (28, 10, 0, 'USE TEMP B-TREE FOR ORDER BY')
  ```
  `LIST SUBQUERY` plus `USE TEMP B-TREE FOR ORDER BY` means the right-hand side of the `IN` is fully evaluated into an ephemeral structure **before** the outer `SCAN TABLE jobs` begins. It cannot observe partial deletions. The `ORDER BY … LIMIT ?` forces this — a correlated or streamable form would not have been possible anyway.
- **SQLite does not document this as a guarantee.** `sqlite.org/isolation.html` says only: *"Within a single database connection X, a SELECT statement always sees all changes to the database that are completed prior to the start of the SELECT statement, whether committed or uncommitted"*, and about changes made *while* a statement is stepping: *"The answer is that this behavior is undefined … developers should diligently avoid writing applications that make assumptions about what will occur in that circumstance."*
- **But the D-16 argument survives even if re-evaluation happened.** Both predicates select a *prefix* of the `created_at` ordering, and the delete set shrinks monotonically, so a hypothetical re-evaluating engine converges on the same fixed point — *provided* scan order agrees with `created_at` order. It does not always: `test_prune_by_age` backdates a row with a raw `UPDATE`, so rowid order and `created_at` order diverge in the existing suite. Under materialisation this is harmless; under re-evaluation it would not be.

**Recommendation:** treat this as sound but *pinned by test rather than by contract*. Add a regression test that builds ~600 rows with a deliberately shuffled insertion order relative to `created_at` and asserts the exact `(rowcount, remaining)` pair. If a future libsqlite changes the plan, that test goes red loudly instead of `prune()` quietly under-deleting. This test costs about fifteen lines and is the only defence available, because the behaviour is not documented.

**The latent UTC assumption (D-16) is real.** `created_at` is `datetime.now(tz=UTC).isoformat()`, so every value ends `+00:00` and both `<` and `ORDER BY` are correct *lexicographically*. A `-05:00` timestamp would sort and compare wrongly, silently. Put the comment D-16 asks for directly above the statement.

### Mechanic 6 — `_COLUMNS` + `sqlite3.Row` + `_row_to_job` (item 6)

**VERIFIED (executed).** A sixteen-column `_COLUMNS` tuple driving `SELECT`, `INSERT` and placeholders, with `row_factory = sqlite3.Row` and a single `_row_to_job`, passed `ty` and `pyrefly` with **zero** diagnostics — including `JobState(row["state"])`, `ErrorCategory(row["error_category"]) if row["error_category"] else None`, `json.loads(row["tags"])`, and `pages: int | None = row["pages_scanned"]`.

**The friction is the opposite of what CONTEXT.md anticipated: there is none, and that is the problem.** Probe result:

| Expression | `ty` | `pyrefly` |
|---|---|---|
| `a: int = row["title"]` | accepted | accepted |
| `b: str = row["pages_scanned"]` | accepted | accepted |
| `c: int = row.keys()` | **error** — `list[str]` not assignable to `int` | **error** — same |
| `d: int = cursor.fetchone()` | accepted | accepted |
| `n: int = cursor.rowcount` | accepted (correctly typed `int`) | accepted |

`sqlite3.Row.__getitem__` and `Cursor.fetchone()` both return `Any` in typeshed. So `_row_to_job` gets **name-based access with no static type checking of the values at all**. `sqlite3.Row` removes the `row[11]` fragility D-17 targets, and it removes nothing else. Two consequences for the plan:

1. `def _row_to_job(row: sqlite3.Row) -> Job` type-checks fine even though `fetchone()` returns `Any` — no cast, no suppression needed.
2. **The tests must carry the correctness burden the checkers cannot.** TDD-first: a per-column round-trip test that writes a known value and asserts both the value *and its Python type* comes back. This is the only mechanism that will catch, say, `pages_scanned` coming back as `str` because someone stored `json.dumps(n)`.

`sqlite3.Row` is confirmed safe here for the reason CONTEXT.md gives: `JobStore` owns its connection exclusively. Also verified from the docs — *"Assigning to this attribute does not affect the `row_factory` of existing cursors belonging to this connection, only new ones"* — so set it in `__init__` before anything executes.

One gotcha worth a comment: `sqlite3.Row` column names are **case-insensitive** (`row["RADIUS"]` works in the docs' own example). Harmless here; noted so nobody builds a case-sensitivity assumption on it.

### Mechanic 7 — `fail_active_jobs()` and `list_pending()` (item 7)

**VERIFIED (executed)** for the SQL mechanics; the signature choices remain the planner's per CONTEXT.md.

**Deriving from `ACTIVE_STATES` rather than hand-listing.** `ACTIVE_STATES: frozenset[JobState]` and `TERMINAL_STATES` **partition** `JobState` — the docstring in `vocabulary.py` says so explicitly and Phase 21 has a test for it. So:

- `fail_active_jobs()` predicate: `WHERE state IN (<every member of ACTIVE_STATES>)`. Deriving it from the frozenset means Phase 23's `FALLBACK` and Phase 25's `SCANNING_REVERSE` are picked up automatically the moment they join the set.
- `list_pending()` predicate: **`WHERE state = ?` bound to `JobState.PENDING.value`, `ORDER BY created_at ASC`.** `PENDING` is the only pre-scan state, and this is a single scalar bind — no variable-length `IN` needed at all. But the *derivation* still matters: assert in a test that `ACTIVE_STATES - BUSY_STATES` and the pre-scan reading still hold, or better, express the predicate as the single member of `ACTIVE_STATES` that a not-yet-started job can be in. Do not hard-code the string `"PENDING"` into the SQL.

Because `ACTIVE_STATES` is a `frozenset`, **sort it before binding** — set iteration order is not stable across runs (`PYTHONHASHSEED`), and an unstable parameter order would make the SQL string vary between processes, defeating `sqlite3`'s statement cache and making test failures irreproducible. `tuple(sorted(s.value for s in ACTIVE_STATES))` is the fix.

**Three suppression-free ways to bind a variable-length `IN`**, all verified:

| Approach | Verified | Ruff `S608` | Notes |
|---|---|---|---|
| **A. Module-level constant, name-prefixed** — `_UPDATE_JOBS = "UPDATE jobs"` then `_FAIL_ACTIVE = f"{_UPDATE_JOBS} SET state = ?, error = ? WHERE state IN ({_ACTIVE_MARKS})"` | ✓ | clean | **Recommended.** Built once at import from `ACTIVE_STATES`; the SQL string is a constant, so `sqlite3`'s statement cache is fully effective. Zero new dependencies. |
| **B. `json_each(?)`** — `"… WHERE state IN (SELECT value FROM json_each(?))"` with `json.dumps(sorted(...))` as the single bound parameter | ✓ (`ENABLE_JSON1` present; returned `['a','b','e']`, `UPDATE` rowcount 2) | clean | Fully static SQL, no interpolation whatsoever — the most *honest* form. But JSON1 was a compile-time option before SQLite 3.38 (2022); this machine has it, most builds do, and it is not universally guaranteed. Adds a hidden runtime requirement. |
| **C. Inline f-string with a literal `UPDATE`/`SELECT` prefix** | ✓ functionally | **flagged** | Requires a `# noqa: S608` — forbidden by CLAUDE.md. Do not use. |

**On the `fail_active_jobs()` discretion items:**
- **Signature:** prefer `fail_active_jobs(reason: str = "Interrupted by restart") -> int`. M-03's verbatim prescription is `error="Interrupted by restart"`, STOR-05's prose is *"a 'server restarted' reason"*, and a defaulted parameter satisfies both while giving Phase 26 the seam it needs. Returning the count (from `cursor.rowcount`, verified exact for `UPDATE` at 500 rows) gives the *tests* — which are this method's only consumer this phase — something to assert other than a re-query.
- **`error_category`:** **do not set it.** N-14 records that `error_category` is written but never read, Phase 21's D-12 left it deliberately unwired, and Phase 30 (APPL-04) owns giving it a consumer. Writing a category here would add a second unread writer to a field the milestone is trying to justify or delete. Setting it to `ErrorCategory.UNKNOWN` would additionally be *wrong* — an interrupted restart is not an unknown failure.

## D-11 Sweep — the mandatory sufficiency check

**Finding: the six columns are sufficient. Do not add a seventh. One residual risk is named and bounded below.**

Method: re-read `.planning/REQUIREMENTS.md` OUTC-01..OUTC-10, DPLX-01..DPLX-07, APPL-01..APPL-12, and cross-checked against `src/saneless/pipeline.py` and `src/saneless/web/routes.py`.

**The strongest single piece of evidence** is that Phase 21 already shipped `ScanResult` at `src/saneless/pipeline.py:108-115`:

```
outcome: ScanOutcome
pages_scanned: int
pages_removed: int
pages_uploaded: int
warning: str | None = None
```

Five of the six columns are that dataclass, field for field. The sixth, `owner_token`, comes from APPL-09 and has no pipeline counterpart. So the migration is persisting an *already-designed, already-typed* result object plus one cookie value — not a speculative guess.

Requirement-by-requirement:

| Requirement | Needs a column? | Where it lands |
|---|---|---|
| OUTC-01 (Paperless FAILURE → FAILED with message) | No | existing `state` + `error` |
| OUTC-02 (`FALLBACK` state, rendered distinctly) | No | a `JobState` member (Phase 23), plus `outcome`/`warning` |
| OUTC-03 (duplex count mismatch recorded with a warning) | No | `warning` |
| OUTC-04 (preserved PDF path) | No | the requirement says verbatim *"the job's error names that path"* → existing `error` |
| OUTC-05..OUTC-08, OUTC-10 | No | file naming, DPI, polling, test harness — none persist per-job facts |
| OUTC-09 (`output.data_dir`) | No | a settings key; Phase 23 |
| DPLX-01..DPLX-07 | No | config fields, a coordinator protocol, a `JobState` member (`SCANNING_REVERSE`, Phase 25), and deletions |
| APPL-01/02 (doctor, status strip) | No | live probes with a TTL cache, not per-job |
| **APPL-03** (page counts for terminal jobs; front/back during pass B) | **See below** | `pages_scanned` / `pages_removed` / `pages_uploaded` |
| APPL-04 (plain-language errors) | No | `error_category` already exists |
| APPL-05/06/07 | No | profile fields and startup checks |
| APPL-08 (queue position) | No | `list_pending()` position, as CONTEXT.md already recorded |
| APPL-09 (owner-only flip prompt) | **Yes** | `owner_token` — already in the six |
| APPL-10/11/12 | No | form markup, compose docs, timezone rendering |

**APPL-03, worked through.** The requirement has two clauses.

*Clause 1 — "show pages scanned, pages removed as blank, and pages uploaded for every terminal job."* Fully covered by the three `INTEGER` columns. This is a terminal, persisted per-job fact.

*Clause 2 — "manual duplex shows front and back counts during pass B."* Three facts settle this:

1. **The web layer's only channel to in-flight job data is the job row.** `routes.py:81-86` fetches `state.job_store.get_job(state.worker.current_job_id)` and renders it. The worker exposes exactly one live attribute, `current_job_id` (`worker.py:125-127`). Even the thumbnail — the one piece of genuinely mid-scan data the UI shows — is delivered by *writing it into the row* (`worker.py:196-197` → `update_thumbnail`). So the established pattern for "visible during a scan" is indeed "a column".
2. **`PipelineEvent` is a bare `StrEnum` (`pipeline.py:38-43`) and carries no payload.** The front count exists in the pipeline only as `len(front_pages)`, logged at `pipeline.py:311` and never emitted. Delivering it would require changing the event protocol — and `PipelineEvent.SCANNING_REVERSE` already exists with a `job_state` property that Phase 25 is going to rework anyway. **The delivery mechanism for pass-B progress is Phase 25's design decision, not a schema fact.**
3. **The back count cannot be served by a column under any design this milestone contemplates.** A live back count means a database write per page. Nothing in the codebase writes per page (the thumbnail is written once, from the first front page). Phase 30's rendering of "Front: 12 · Back: scanning…" is satisfied by the front count alone, and the front count is `pages_scanned` written at the end of pass A — the same column, one write, exactly like `update_thumbnail`.

**Conclusion:** APPL-03 needs no seventh column. `pages_scanned` written once at the end of pass A carries the front count; the total overwrites it at terminal; the back count during pass B is not a per-job persisted fact and no column can make it one.

**Residual risk, named and bounded.** If Phase 25 or 30 decides to persist a *front/back split* (a `pages_front INTEGER` alongside the total), it will need one more migration step. That is now a **five-line addition to `_MIGRATIONS`, not a bare `ALTER TABLE … except: pass`** — which is precisely what this phase exists to make possible. D-11's stated purpose is "no later phase reaches for another bare `ALTER`", and the ladder achieves that whether or not a step 3 is ever written. Adding a seventh unwritten, unread, unrequested column now would widen D-07's deliberately bounded exemption to buy a saving the ladder has already made cheap. **Recommend: six columns, and record this paragraph in the plan so the decision is traceable if Phase 25 revisits it.**

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `sqlite3` (stdlib) | Python 3.14.2 / libsqlite 3.34.1 here | persistence, transactions, `PRAGMA user_version` | already the store; REQUIREMENTS.md § Out of Scope forbids new runtime dependencies |
| `threading` (stdlib) | 3.14.2 | `RLock`, `Barrier` | C-07's prescription |
| `functools` (stdlib) | 3.14.2 | `wraps` in `@_locked` | preserves `__name__`/`__qualname__`/`__wrapped__`/signature — verified |
| `inspect` (stdlib) | 3.14.2 | the reflective lock-coverage test | Phase 21's D-09 pattern |
| `concurrent.futures` (stdlib) | 3.14.2 | the two-thread stress test | `Future.result()` re-raises; raw `Thread` swallows |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| `pytest` | ≥9.0.2 | the suite | always |
| `pytest-timeout` | ≥2.4.0 | the 60 s hang guard | already configured; the stress test uses 0.5 % of it |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| a hand-rolled two-step ladder | `alembic` / `yoyo-migrations` | **Rejected by D-02**, and independently by REQUIREMENTS.md § Out of Scope (no new runtime dependencies). Buys rollback and branching this project will never use, against ~30 lines |
| `@_locked` decorator | inline `with self._lock, self._conn:` per method | **Rejected by D-12.** Also: the inline form breaks `close()` in exactly the way § Pitfall 2 documents |
| `Future.result()` | raw `threading.Thread` + `excepthook` | strictly worse; exceptions are swallowed by default |
| module-constant SQL | `json_each(?)` | both verified clean; `json_each` adds a soft JSON1 requirement |

**Installation:** none — every dependency is stdlib or already in the dev group.

## Package Legitimacy Audit

**Not applicable — this phase installs no external packages.** Everything used is CPython stdlib (`sqlite3`, `threading`, `functools`, `inspect`, `concurrent.futures`, `json`, `uuid`, `datetime`) or already pinned in `[dependency-groups].dev`. `slopcheck` was therefore not run.

## Architecture Patterns

### System flow: opening a store

```
JobStore(db_path)
   │
   ├─► sqlite3.connect(db_path, check_same_thread=False)
   │        (promise: caller serialises; nothing is thread-safe yet)
   │
   ├─► conn.row_factory = sqlite3.Row          ← before any cursor exists
   │
   ├─► conn.execute("PRAGMA journal_mode=WAL") ← MUST be here.  After the next
   │                                             line it raises on a file db and
   │                                             silently no-ops on :memory:
   │
   ├─► conn.autocommit = False                 ← a transaction is now always open
   │
   └─► _migrate(conn, db_path)
            │
            ├─ version = PRAGMA user_version
            │
            ├─ if version == 0:
            │      jobs table exists?  ── yes ─► version = 1   (legacy S3, D-03)
            │                           └─ no ─► version = 0   (fresh)
            │
            ├─ for step in _MIGRATIONS[version:]:
            │      ┌─────────── one transaction ───────────┐
            │      │  step(conn)                            │
            │      │    step 1: CREATE TABLE (S3 shape)     │
            │      │    step 2: table_info guard ─► raise   │───► rollback()
            │      │            6 × ALTER ADD COLUMN        │     close()
            │      │  PRAGMA user_version = <index+1>       │     raise StorageError
            │      │  commit()                              │
            │      └────────────────────────────────────────┘
            │
            └─ done: user_version == len(_MIGRATIONS)

Every public method thereafter:
   caller ─► @_locked ─► with self._lock:      ← serialisation (STOR-01)
                            method body
                               with self._conn:  ← transaction (D-13)
                                   execute(…)
                            (close() takes the lock and NOT the transaction)
```

### Recommended module structure inside `job.py`

```
job.py
├── __all__                    # unchanged: re-exports ErrorCategory, JobState
├── _LOCKED_MARKER             # the attribute name, in one place
├── _locked[**P, R]            # PEP 695 decorator, lock only
├── _COLUMNS: tuple[str, ...]  # the column list — STOR-04's "exactly once"
├── _COLUMN_LIST / _PLACEHOLDERS
├── _SELECT_ALL / _INSERT / _FAIL_ACTIVE / _LIST_PENDING / _PRUNE
│                              # module constants, name-prefixed (§ Pitfall 4)
├── _S3_COLUMNS: frozenset[str]# the D-05 guard's expectation
├── _V2_COLUMNS: tuple[tuple[str, str], ...]
├── _migrate_v1(conn)          # CREATE TABLE, S3 shape
├── _migrate_v2(conn)          # guard + 6 × ALTER
├── _MIGRATIONS = (_migrate_v1, _migrate_v2)
├── Job                        # dataclass, now 16 fields
└── JobStore
    ├── __init__               # NOT decorated — nothing else can see it yet
    ├── _row_to_job            # private: the row mapping, exactly once
    └── @_locked public methods
```

### Pattern 1: the ladder

```python
# Source: verified end-to-end on this machine; Python 3.14 sqlite3 docs § Transaction control
def _migrate(conn: sqlite3.Connection, db_path: str) -> None:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        has_jobs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone() is not None
        if has_jobs:
            version = 1          # D-03: legacy S3 joins the ladder after step 1
    for index in range(version, len(_MIGRATIONS)):
        _MIGRATIONS[index](conn)
        conn.execute(f"PRAGMA user_version = {index + 1}")   # cannot be bound
        conn.commit()
```

### Pattern 2: `@_locked`, lock only

```python
# Source: verified clean under ruff + ty + pyrefly in src/saneless/
_LOCKED_MARKER = "__saneless_locked__"

def _locked[**P, R](
    method: Callable[Concatenate[JobStore, P], R],
) -> Callable[Concatenate[JobStore, P], R]:
    """Serialise a JobStore method on the store's re-entrant lock."""

    @functools.wraps(method)
    def wrapper(self: JobStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    setattr(wrapper, _LOCKED_MARKER, True)
    return wrapper
```

Bodies then open their own transaction:

```python
    @_locked
    def get_job(self, job_id: str) -> Job | None:
        with self._conn:
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
        return None if row is None else self._row_to_job(row)

    @_locked
    def close(self) -> None:
        self._conn.close()          # NO `with self._conn:` — see Pitfall 2
```

### Pattern 3: `_COLUMNS` once

```python
_COLUMNS: tuple[str, ...] = (
    "id", "profile", "title", "state", "error", "error_category",
    "tags", "correspondent", "thumbnail", "created_at",
    "outcome", "pages_scanned", "pages_removed", "pages_uploaded",
    "warning", "owner_token",
)
_COLUMN_LIST = ", ".join(_COLUMNS)
_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)

_SELECT_JOBS = "SELECT"          # named so ruff's S608 heuristic sees a Name,
_INSERT_JOBS = "INSERT INTO jobs" # not a literal SQL verb — see Pitfall 4
_SELECT_ALL  = f"{_SELECT_JOBS} {_COLUMN_LIST} FROM jobs"
_INSERT      = f"{_INSERT_JOBS} ({_COLUMN_LIST}) VALUES ({_PLACEHOLDERS})"
```

and one mapping:

```python
    def _row_to_job(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            state=JobState(row["state"]),
            error_category=ErrorCategory(row["error_category"]) if row["error_category"] else None,
            outcome=ScanOutcome(row["outcome"]) if row["outcome"] else None,
            tags=json.loads(row["tags"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            ...
        )
```

`_row_to_job` must stay **private and undecorated** — see Pitfall 3.

### Pattern 4: the reflective lock-coverage test

```python
def test_every_public_method_is_locked() -> None:
    """Every public callable on JobStore carries the lock marker (D-12)."""
    unlocked = [
        name
        for name, member in inspect.getmembers(JobStore, inspect.isfunction)
        if not name.startswith("_")
        and getattr(member, _LOCKED_MARKER, False) is not True
    ]
    assert unlocked == []
```

No exemption list, per D-12 and D-15. Verified passing against a five-method prototype including `close()`.

### Anti-Patterns to Avoid

- **`executescript()` in a migration step.** It implicitly commits the pending transaction, so the DDL would land separately from the version stamp — exactly the failure D-13 exists to prevent.
- **`ALTER TABLE … ADD COLUMN … NOT NULL DEFAULT 0`** for the page counts. D-08 covers the semantics; mechanically it also backfills every historical row with a measured-zero lie that cannot be undone.
- **Applying `@_locked` to `_row_to_job` or any private helper.** The lock is re-entrant so it would not deadlock, but the reflective test only looks at public names and the decorator adds a frame per row for nothing.
- **`SELECT *` with `sqlite3.Row`.** Rejected by D-17: the `INSERT` still has to spell the columns, so "the column list exists once" would be false.
- **A deliberate no-lock control run in the test suite.** Rejected by D-14, and the measured failure signature on 3.14 differs from the review's baseline, confirming the fragility.
- **Hard-coding `"PENDING"` in `list_pending()`'s SQL.** Phase 21 D-04: derive from the vocabulary.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Transaction bracketing | manual `BEGIN`/`COMMIT` strings | `conn.autocommit = False` + `with self._conn:` | the CM rolls back on *any* uncaught exception including `KeyboardInterrupt`; a hand-written `try/except Exception` would not |
| Delete counting | `SELECT COUNT(*)` before/after | `cursor.rowcount` | N-17's race; verified exact including the zero case |
| Thread exception capture | `threading.Thread` + a shared error list | `ThreadPoolExecutor` + `Future.result()` | stdlib already re-raises in the calling thread; a hand-rolled list is one `try:` away from swallowing |
| Signature-preserving decoration | copying `__name__`/`__doc__` by hand | `functools.wraps` | also gives `__wrapped__` and a working `inspect.signature` |
| Column-name access | positional `row[11]` | `sqlite3.Row` | N-13's `thumbnail` bug is exactly the positional failure mode |
| Schema versioning | a `schema_version` table | `PRAGMA user_version` | zero rows, zero migration-of-the-migration-table, atomic with the DDL (verified) |
| Two-thread synchronisation in tests | sleeps and polling | `threading.Barrier` | TEST-02 / Phase 32 ban `time.sleep`; the barrier is deterministic |

**Key insight:** every single mechanic this phase needs is a documented contract of `sqlite3`, `threading`, `functools` or `concurrent.futures`. The only genuinely novel code is the two-function `_MIGRATIONS` tuple and the `_locked` decorator, and both are under fifteen lines.

## Common Pitfalls

### Pitfall 1: WAL after the autocommit flip — passes every test, crashes in production

**VERIFIED (executed).**

**What goes wrong:**
```
WAL inside txn (file):      RAISED OperationalError: cannot change into wal mode from within a transaction
WAL inside txn (:memory:):  ('memory')          <-- silently succeeds
WAL before autocommit=False (file): ('wal')     <-- correct
```

**Why it happens:** `conn.autocommit = False` opens a transaction *immediately* (verified: `in_transaction` is `True` before any statement). SQLite refuses a journal-mode change to WAL inside a transaction. An in-memory database has no journal to change, so it returns `'memory'` and no error.

**Why it is dangerous here specifically:** **every test in `tests/test_job.py` constructs `JobStore()` with the default `":memory:"`.** A store that sets WAL after the flip passes 332/332 tests and raises `OperationalError` on the operator's first `saneless serve`. This is the exact failure class CONTEXT.md's WAL discretion item warns about — "do not let it become the reason a migration step behaves differently in tests than in production" — and it is live, not hypothetical.

**How to avoid:** set `PRAGMA journal_mode=WAL` **before** `conn.autocommit = False`. Then pin it with a `tmp_path` file-backed test that constructs a real `JobStore(str(tmp_path / "x.db"))` and asserts `PRAGMA journal_mode` reads back `'wal'`. That test is the only thing standing between this ordering and a future refactor that reverses it.

**Warning signs:** any `:memory:`-only test suite around a file-backed production path; a `PRAGMA` that appears after an `autocommit` assignment.

**If the planner instead chooses to drop WAL** (a legitimate reading of the discretion item — the current code sets it and nothing depends on it), the file-backed test should assert the *chosen* mode explicitly, so the choice is recorded in the suite rather than in a commit message.

### Pitfall 2: `@_locked` on `close()` — the D-12 / D-15 collision

**VERIFIED (executed).** Every one of these raises `sqlite3.ProgrammingError: Cannot operate on a closed database`:

```
with conn:  conn.close()                     # autocommit=False   -> RAISED
with conn:  conn.close()                     # LEGACY, no open txn -> RAISED
with custom_txn_cm(conn):  conn.close()      # even a hand-rolled CM -> RAISED
conn.in_transaction  (after close)           # -> RAISED
```

**Why it happens:** `Connection.__exit__` runs *after* the body, and it must commit or roll back — on a connection the body has already closed. There is no escape: even a hand-written context manager that checks `conn.in_transaction` first fails, because `in_transaction` itself raises on a closed connection.

**How to avoid:** `@_locked` takes **`self._lock` only**. The transaction context (`with self._conn:`) goes in the bodies of the methods that execute SQL. `close()` then reads:

```python
    @_locked
    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
```

which satisfies D-15 literally ("`close()` gets `@_locked` and nothing else"), satisfies D-12's *purpose* (the reflective test still enumerates every public method and needs no exemption list), and diverges only from D-12's parenthetical "(and the connection's transaction context)". **This is a real, measured constraint, not a preference — the planner should record the divergence rather than attempt the literal reading.**

Note that C-07's own prescription (`with self._lock, self._conn:` inline at each site) has the identical problem; the review simply never showed `close()`.

*Alternative, if the planner wants the parenthetical preserved:* a parameterised `@_locked(transaction=False)` used only on `close()`. It keeps the marker on every public method (so no exemption list) and keeps transaction-wrapping as the default. It costs a nested decorator factory and more typing ceremony. **Not recommended** — the lock-only form is simpler and the transaction boundary is more legible when it is visible in the method body.

### Pitfall 3: `sqlite3.Connection` context managers do NOT nest

**VERIFIED (executed).** File-backed database, WAL, `autocommit = False`:

```python
with conn:
    conn.execute("INSERT INTO t VALUES (1)")
    with conn:                                  # nested
        conn.execute("INSERT INTO t VALUES (2)")
    conn.execute("INSERT INTO t VALUES (3)")
    raise RuntimeError("boom")                  # outer should roll everything back
```
```
rows visible after outer rollback: [1, 2]
```

**Rows 1 and 2 survived a rollback that should have discarded all three.** The inner `__exit__` committed the *outer* transaction; under `autocommit = False` a fresh transaction then opened, and only row 3 was rolled back.

**Why this matters for STOR-01:** the requirement's clause *"public methods never call other public methods"* is usually read as a style rule enforced by `RLock` re-entrancy. It is not. With the transaction context in the method bodies, a public method calling another public method silently commits half its work. `RLock` re-entrancy makes it not *deadlock*; nothing makes it correct.

**How to avoid:** shared logic goes in **private, undecorated** helpers (`_row_to_job`, `_migrate`). Add a test or a plan-level check that no public `JobStore` method calls another. Ruff cannot see this; a short `ast`-walking test could, or a code-review checklist item.

### Pitfall 4: ruff `S608` vs. CLAUDE.md's no-suppression rule

**VERIFIED (executed).** With this project's ruff configuration (`select` includes `S`), the following are **flagged** as `S608 Possible SQL injection vector through string-based query construction`:

```python
f"SELECT {_COLUMN_LIST} FROM jobs"                                   # flagged
"SELECT " + _COLUMN_LIST + " FROM jobs"                              # flagged
"SELECT {} FROM jobs".format(_COLUMN_LIST)                           # flagged
"SELECT %s FROM jobs" % (_COLUMN_LIST,)                              # flagged
f"INSERT INTO jobs ({_COLUMN_LIST}) VALUES ({_PLACEHOLDERS})"        # flagged
f"UPDATE jobs SET state = ? WHERE state IN ({_MARKS})"               # flagged
```

and the following are **clean**:

```python
_SELECT_JOBS = "SELECT"
f"{_SELECT_JOBS} {_COLUMN_LIST} FROM jobs"                           # clean
_INSERT_JOBS = "INSERT INTO jobs"
f"{_INSERT_JOBS} ({_COLUMN_LIST}) VALUES ({_PLACEHOLDERS})"          # clean
f"{_SELECT_ALL} WHERE id = ?"                                        # clean
f"PRAGMA user_version = {index + 1}"                                 # clean (PRAGMA not in the rule)
f"ALTER TABLE jobs ADD COLUMN {name} {sqltype}"                      # clean (ALTER not in the rule)
"… WHERE state IN (SELECT value FROM json_each(?))"                  # clean (no interpolation)
```

The rule's heuristic keys on the string *beginning* with a SQL verb literal.

**The bind.** STOR-04 requires the column list to exist exactly once, and SQL cannot parameterise identifiers, so *some* interpolation is unavoidable. CLAUDE.md forbids `# noqa`, `# type: ignore`, *and* disabling rules — and SWP-08 is separately committed to removing the eight suppressions already in the tree. There is no `per-file-ignores` escape that respects the project rule.

**Three honest options, ranked:**

1. **Module-level constants with a named verb** (the `_SELECT_JOBS` form above). Passes ruff with no suppression. It is partly linter-evasion, but the code is genuinely safe — every interpolated value is a module-level `tuple[str, ...]` literal that no user input can reach — and building the statements once at import is *better* engineering than rebuilding them per call. **Recommended, with a comment stating why the interpolation is safe**, so a reader is not left to infer it from the constant naming.
2. **`json_each(?)` for the `IN` clauses only.** Fully static SQL, nothing to evade. Verified working on libsqlite 3.34.1 (`ENABLE_JSON1`). Adds a soft dependency on the JSON1 extension, which was a compile option before SQLite 3.38.
3. **Raise it with the user.** The rule as configured is incompatible with STOR-04 as written. If the planner is uncomfortable with option 1's framing, this is a legitimate escalation rather than a silent `# noqa`.

**Also verified as ruff friction in the same area:** `UP047` *requires* PEP 695 `def _locked[**P, R]` over `ParamSpec`; `B010` fires on `setattr` with a literal attribute name (use the module constant); `TC003` wants `from collections.abc import Callable` inside `if TYPE_CHECKING:`; `F541` fires on an f-prefixed string with no placeholders. All four were hit while prototyping and all four have clean fixes.

### Pitfall 5: `frozenset` iteration order in SQL parameters

**INFERRED** (from `PYTHONHASHSEED` semantics; not separately executed).

`ACTIVE_STATES` is a `frozenset[JobState]`. Iterating it to build `IN (?, ?, ?, ?, ?)` parameters produces a *different order per process*. The parameter values are equivalent as a set, so the query result is correct — but the SQL string built from the count is fine while the *bound tuple* order varies, which makes a failing test's parameter dump irreproducible across runs and defeats reasoning about `sqlite3`'s statement cache. Always `sorted(...)`.

### Pitfall 6: pinning an undocumented SQLite query plan

**VERIFIED (executed) for the current behaviour; the *guarantee* is INFERRED.**

D-16's single statement is correct only because SQLite materialises the `IN (SELECT … ORDER BY … LIMIT ?)` right-hand side (`LIST SUBQUERY` in `EXPLAIN QUERY PLAN`) before the outer `SCAN TABLE jobs`. SQLite's isolation documentation explicitly declines to guarantee what a statement sees of its own concurrent modifications. This machine runs libsqlite **3.34.1**; CI and Docker run newer.

**How to avoid the risk:** a regression test that inserts rows whose `created_at` order deliberately disagrees with insertion (rowid) order and asserts the exact `(rowcount, remaining)` pair. Under materialisation the answer is stable; under any re-evaluating plan it would differ. That test converts an undocumented dependency into a loud failure.

### Pitfall 7: the existing migration test builds an S2 database and will now raise

**VERIFIED (read).** `tests/test_job.py:134` `test_jobstore_migration_adds_column` constructs a table with `thumbnail` but **without** `error_category` — that is the S2 shape. Under D-05 the new ladder will *correctly raise* on it. The test does not merely need rewriting for style; **it will fail**, and it must be replaced by two tests: one that opens an S3 fixture and migrates to head (N-13's "first-release schema" test, per D-04's verification note), and one that asserts the S2 shape raises with the path and the missing column names in the message.

### Pitfall 8: leaking a connection when the ladder raises

**VERIFIED (executed).** After the D-05 guard raises, the connection still holds an open `BEGIN DEFERRED`. `__init__` must `rollback()` and `close()` before re-raising, or a failed `JobStore(path)` leaves a file handle and a SQLite lock behind. Verified that the rollback-then-close sequence works and leaves `user_version = 0` with the original columns intact.

## Code Examples

### Opening the store (the ordering that matters)

```python
# Source: verified on Python 3.14.2 / libsqlite 3.34.1
def __init__(self, db_path: str = ":memory:") -> None:
    """Open the job store, enable WAL, and run the migration ladder."""
    self._lock = threading.RLock()
    self._conn = sqlite3.connect(db_path, check_same_thread=False)
    self._conn.row_factory = sqlite3.Row
    self._conn.execute("PRAGMA journal_mode=WAL")   # before the flip -- Pitfall 1
    self._conn.autocommit = False                   # D-13
    try:
        _migrate(self._conn, db_path)
    except Exception:
        self._conn.rollback()
        self._conn.close()
        raise
```

### The D-05 guard

```python
# Source: verified -- raises before any ALTER, rollback leaves the db untouched
_S3_COLUMNS: frozenset[str] = frozenset({
    "id", "profile", "title", "state", "error", "error_category",
    "tags", "correspondent", "thumbnail", "created_at",
})

def _migrate_v2(conn: sqlite3.Connection) -> None:
    """Add the six v2 result columns to a database already at the S3 shape."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    missing = _S3_COLUMNS - present
    if missing:
        msg = (
            f"job database at an unsupported schema: missing column(s) "
            f"{', '.join(sorted(missing))}"
        )
        raise StorageError(msg)          # EM rules: build the message first
    for name, sqltype in _V2_COLUMNS:
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sqltype}")
```

Note the message construction: ruff's `EM` rules (enabled) forbid a string literal directly inside `raise`. The database path must be threaded in from `__init__`, since `_migrate_v2` only receives a connection — pass it as a second parameter or wrap and re-raise in `_migrate`.

### `prune()`

```python
# Source: verified equivalent to the old two-statement form across 9 cases
_PRUNE_HEAD = "DELETE FROM jobs"
_PRUNE = (
    f"{_PRUNE_HEAD} WHERE created_at < ? "
    f"OR id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)"
)

@_locked
def prune(self, max_age_days: int = 7, max_rows: int = 500) -> int:
    """Remove jobs older than max_age_days and trim to max_rows."""
    # created_at is stored as datetime.now(tz=UTC).isoformat(), so every value
    # ends "+00:00" and both the "<" comparison and the ORDER BY are correct
    # lexicographically. A non-UTC timestamp in this column would silently
    # break both the cutoff and the ordering.
    cutoff = (datetime.now(tz=UTC) - timedelta(days=max_age_days)).isoformat()
    with self._conn:
        cursor = self._conn.execute(_PRUNE, (cutoff, max_rows))
    deleted = cursor.rowcount
    if deleted > 0:
        logger.debug("Pruned %d old jobs", deleted)
    return deleted
```

### The stress test

```python
# Source: executed under this project's pytest config -- 0.28 s, 3 passed, 0 warnings
def test_two_threads_200_rounds() -> None:
    """Two threads, 200 rounds each, zero exceptions and a data invariant (STOR-01)."""
    store = JobStore()
    barrier = threading.Barrier(2)
    seen: dict[str, JobState] = {}
    guard = threading.Lock()

    def churn(tag: str) -> None:
        barrier.wait(timeout=10)
        for i in range(200):
            job = store.create_job(profile="default", title=f"{tag}-{i}")
            store.update_state(job.id, JobState.SCANNING)
            store.get_job(job.id)
            store.list_recent(limit=10)
            store.update_state(job.id, JobState.DONE)
            with guard:
                seen[job.id] = JobState.DONE

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(churn, "A"), pool.submit(churn, "B")]
        for future in futures:
            future.result()          # re-raises anything a thread raised

    assert len(seen) == 400
    for job_id, expected in seen.items():
        fetched = store.get_job(job_id)
        assert fetched is not None
        assert fetched.state is expected
    store.close()
```

## Storage Documentation

**Finding: `docs/` has no page describing the job store, its schema, or its persistence guarantees.** `grep -rn "JobStore\|user_version\|schema\|migrat" docs/ README.md CONTRIBUTING.md` (excluding `PRD.md`) returns nothing. What exists is four scattered sentences plus a structural gap. All paths below are relative to the repository root.

| File / line | Current text | Why it needs work |
|---|---|---|
| `docs/reference/configuration.md:43` | `` `tmp_dir` \| string \| `"/tmp/saneless"` \| Temporary directory for scan files **and SQLite database** `` | True today and **stays true** this phase — D-18 defers the move to Phase 23. **Do not "fix" it here.** Listed so the planner does not touch it by mistake. |
| `docs/reference/configuration.md:48-49` | `history_retention_days` "Days to keep completed job history"; `history_max_rows` "Maximum job history entries in SQLite" | *"completed"* is wrong and becomes more visibly wrong once `fail_active_jobs()` exists: `prune()` deletes by `created_at` regardless of state, so an in-flight row older than the cutoff is deleted too. The single-statement rewrite is the moment to say what the two settings actually do — and that they interact (`OR`, not sequential). **Primary in-phase correction.** |
| `docs/reference/docker.md:30` | `` `/tmp/saneless` \| Scan temp files and SQLite database \| No (ephemeral OK) `` | *"ephemeral OK"* is the durability claim M-03 and N-39 both dispute. Phase 23 moves the database and rewrites this row. **Leave it; note it for Phase 23.** |
| `docs/reference/cli-commands.md:85` | "List recent scan job history from the SQLite database." | Accurate, and stays accurate. No change. |
| `docs/explanation/architecture.md:45-51` (§ Worker Thread Model) | "The worker communicates progress back to the web layer via state transitions on the `Job` object stored in SQLite." | Accurate but **incomplete after this phase**: it is now also the only mechanism by which anything is serialised between the two threads, and the sentence is the natural anchor for the new persistence paragraph. |
| `docs/explanation/architecture.md` — **structural gap** | There is no `## Persistence` / `## Job Store` section at all. Sections are: The Scan Pipeline, Scanner Abstraction, Pipeline Orchestration, PDF Assembly, Paperless Upload, Worker Thread Model, Configuration Layer, Web Layer. | **This is the "storage documentation" the roadmap means.** A short section is the honest deliverable: one connection owned exclusively by `JobStore`, an `RLock` around every public method, `PRAGMA user_version` with an ordered ladder, and what happens when a database at an unrecognised shape is opened (it refuses to start and says which columns are missing). |
| `src/saneless/job.py:1-7` (module docstring) | "SQLite-backed persistence **for crash recovery** and history" | M-03's verified complaint: no recovery code exists. This phase ships `fail_active_jobs()` **unwired** (Phase 26 calls it), so the promise is still not kept. Either soften the sentence now or note explicitly that Phase 26 makes it true. Not a `docs/` file, but it is the sentence a reader hits first. |

**Constraint from CONTEXT.md:** the documentation must not describe any of the six new columns as having a consumer. The safest prose says nothing about them at all, or says the schema is versioned and currently at version 2 without enumerating what version 2 added.

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | `pytest` ≥9.0.2 with `pytest-timeout` ≥2.4.0 |
| Config file | `pyproject.toml` § `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_job.py -q` |
| Full suite command | `uv run pytest -m "not browser" -q` |
| Live constraints | `filterwarnings = ["error"]`, `timeout = 60`, `timeout_method = "signal"`, `xfail_strict`, `--strict-config`, `--strict-markers` |
| Baseline | 332 tests, ~27 s (Phase 20) |
| TDD | `workflow.tdd_mode: true` — tests first |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| STOR-01 | Two threads × 200 rounds: zero exceptions | unit | `uv run pytest tests/test_job.py::test_two_threads_200_rounds -x` | ❌ Wave 0 |
| STOR-01 | …and the data invariant (every job readable at its last state; row count reconciles) | unit | same test | ❌ Wave 0 |
| STOR-01 | Every public `JobStore` method carries the lock marker (reflective, no exemptions) | unit | `uv run pytest tests/test_job.py::test_every_public_method_is_locked -x` | ❌ Wave 0 |
| STOR-01 | No public method calls another public method | unit (`ast` walk) | `uv run pytest tests/test_job.py::test_no_public_method_calls_another -x` | ❌ Wave 0 — *see Pitfall 3; this is a correctness rule, not style* |
| STOR-02 | A fresh `:memory:` store reaches head `user_version` with all 16 columns | unit | `uv run pytest tests/test_job.py::test_fresh_database_migrates_to_head -x` | ❌ Wave 0 |
| STOR-02 | An S3 fixture (`dc9b8af` shape) opens, migrates, preserves its rows, reports head | unit | `uv run pytest tests/test_job.py::test_s3_database_migrates -x` | ❌ Wave 0 (replaces `test_jobstore_migration_adds_column`) |
| STOR-02 | An S3-via-bare-`ALTER` fixture (`error_category` last) migrates correctly | unit | `uv run pytest tests/test_job.py::test_s3_alter_order_migrates -x` | ❌ Wave 0 — *pins D-06* |
| STOR-02 | Re-opening a head database is a no-op | unit | `uv run pytest tests/test_job.py::test_migration_is_idempotent -x` | ❌ Wave 0 |
| STOR-02 | An S2 database raises, naming the path and the missing columns, leaving the file untouched | unit | `uv run pytest tests/test_job.py::test_unsupported_schema_raises -x` | ❌ Wave 0 — *pins D-05* |
| STOR-02 | A **file-backed** store reports `journal_mode = wal` (or the chosen mode) | unit | `uv run pytest tests/test_job.py::test_file_database_journal_mode -x` | ❌ Wave 0 — **the only guard against Pitfall 1** |
| STOR-02 | The bare `ALTER … except: pass` is gone | grep | `! grep -q "except sqlite3.OperationalError" src/saneless/job.py` | n/a |
| STOR-03 | All six columns exist and round-trip as `None` on a fresh row | unit | `uv run pytest tests/test_job.py::test_new_columns_default_to_none -x` | ❌ Wave 0 |
| STOR-03 | Each column round-trips its Python type (`ScanOutcome`, `int`, `str`) when written directly | unit | `uv run pytest tests/test_job.py::test_new_columns_round_trip -x` | ❌ Wave 0 — *the checkers cannot verify this; see Mechanic 6* |
| STOR-04 | `test_prune_by_age`, `test_prune_by_count`, `test_prune_no_deletions` pass **unchanged** | unit (existing) | `uv run pytest tests/test_job.py -k prune -x` | ✅ exists |
| STOR-04 | `prune()` returns `cursor.rowcount` from one statement; equivalence at shuffled insert order | unit | `uv run pytest tests/test_job.py::test_prune_single_statement_equivalence -x` | ❌ Wave 0 — *pins Pitfall 6* |
| STOR-04 | A concurrent insert during prune cannot corrupt the count | unit | `uv run pytest tests/test_job.py::test_prune_count_under_concurrency -x` | ❌ Wave 0 — *D-16's explicit obligation* |
| STOR-04 | The column list and row mapping appear once | grep / unit | `uv run pytest tests/test_job.py::test_columns_declared_once -x` | ❌ Wave 0 |
| STOR-05 | `fail_active_jobs()` marks every `ACTIVE_STATES` row `ERROR` with the reason, returns the count, leaves terminal rows alone | unit | `uv run pytest tests/test_job.py::test_fail_active_jobs -x` | ❌ Wave 0 |
| STOR-05 | `fail_active_jobs()`'s predicate is derived from `ACTIVE_STATES`, not hand-listed | unit | `uv run pytest tests/test_job.py::test_fail_active_covers_every_active_state -x` | ❌ Wave 0 — *Phase 21 D-09 pattern* |
| STOR-05 | `list_pending()` returns only `PENDING`, oldest first | unit | `uv run pytest tests/test_job.py::test_list_pending_order -x` | ❌ Wave 0 |
| all | ruff, ruff format, ty, pyrefly clean; **zero new suppressions** | gate | `uv run ruff check . && uv run ruff format --check . && uv run ty check && uv run pyrefly check` | ✅ CI-01 |
| all | Nothing else regressed | gate | `uv run pytest -m "not browser" -q` | ✅ 332 baseline |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/test_job.py -q` (sub-second today) plus `uv run ruff check src/saneless/job.py tests/test_job.py`
- **Per wave merge:** `uv run pytest -m "not browser" -q` plus all four static gates
- **Phase gate:** full suite green, all four gates clean, `git grep -c "noqa\|type: ignore" src/ tests/` no higher than the pre-phase count of 8

### Wave 0 Gaps

- [ ] Import block in `tests/test_job.py` — needs `concurrent.futures`, `inspect`, `threading`, `uuid`, and `ScanOutcome`
- [ ] An S3 fixture helper (the `dc9b8af` ten-column `CREATE TABLE`) — used by three tests; put it in `tests/test_job.py`, not `conftest.py`, since nothing else needs it
- [ ] `test_jobstore_migration_adds_column` at `tests/test_job.py:134` must be **deleted and replaced** — it builds an S2 database, which the new ladder correctly rejects (Pitfall 7)
- [ ] `time.sleep(0.01)` at `tests/test_job.py:29` and `:76` — pre-existing, targeted by TEST-02 / Phase 32. **Not this phase's**, but the new tests must not add more; use distinct explicit `created_at` values instead
- [ ] No framework install needed

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `isolation_level` + implicit DML-only transactions | `Connection.autocommit = False` (PEP 249) | Python **3.12** | This is D-13. The legacy default is still `LEGACY_TRANSACTION_CONTROL` in 3.14 — the new mode is opt-in |
| `ParamSpec("P")` / `TypeVar("R")` objects | PEP 695 `def f[**P, R](...)` | Python **3.12** | ruff's `UP047` now *requires* it for generic functions — the old spelling fails lint |
| `sqlite3.version` | removed | Python **3.14** | `sqlite3.version` and `version_info` are gone; use `sqlite3.sqlite_version`. Hit while prototyping |
| positional `row[11]` | `row_factory = sqlite3.Row` + name access | long-standing | D-17; still `Any`-typed, so tests carry the type burden |
| ad-hoc `ALTER … except: pass` | `PRAGMA user_version` ladder | long-standing | N-13; this phase |

**Deprecated/outdated:** `sqlite3.version` / `sqlite3.version_info` (removed in 3.14) · default `isolation_level` transaction control (superseded but still the default) · `ParamSpec`-object generics (superseded by PEP 695).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The `LIST SUBQUERY` materialisation that makes D-16's single statement correct holds on the SQLite versions in CI and Docker, not just 3.34.1 here | Mechanic 5, Pitfall 6 | `prune()` silently under-deletes on some deployments. **Mitigated** by the shuffled-order regression test in the validation table — do not skip it |
| A2 | No statement the ladder needs (beyond `journal_mode`) is refused inside a transaction | Mechanic 1 | A future step 3 could fail at open. Only `journal_mode` was tested; `VACUUM` is the other known candidate and the ladder does not use it |
| A3 | APPL-03's "front and back counts during pass B" is transient UI feedback, not a persisted per-job fact | D-11 Sweep | Phase 25 or 30 needs a `pages_front` column and writes a step 3. Cost is five lines in `_MIGRATIONS`, not a bare `ALTER` — the ladder is what makes this acceptable. **This is the one D-11 residual and it is deliberate** |
| A4 | Ruff's `S608` heuristic (leading SQL verb literal) is stable across the ruff versions CI will use | Pitfall 4 | A ruff upgrade could start flagging the recommended constant form, and the fix would require re-litigating the suppression rule with the user |
| A5 | The SQLite JSON1 extension is present wherever saneless runs — **only relevant if the planner picks the `json_each(?)` option** | Mechanic 7 option B | `OperationalError: no such table function: json_each` at runtime on a build without JSON1. Avoided entirely by option A |
| A6 | `frozenset` iteration order instability is worth sorting around | Pitfall 5 | Only reproducibility, never correctness. Cheap to do; not verified by experiment |
| A7 | `StorageError(SanelessError)` is the right home for the D-05 guard, and Phase 28 will accept rather than re-type it | Mechanic 3 | Phase 28 renames or re-parents it. Low cost either way; the alternative (`ConfigError`) is worse |

## Open Questions (RESOLVED)

> **All five were resolved after this document was written.** The binding answers live in
> `22-CONTEXT.md` § "Post-research amendments" and are implemented by the plans. Recorded
> here so nobody re-opens a settled question from this file.
>
> | # | Resolution | Where |
> |---|---|---|
> | 1 | Lock-only `@_locked`; `with self._conn:` moves into method bodies | **D-20** — plan 22-03 |
> | 2 | Module-constant SQL form, with a safety comment. Re-confirmed by the user after a false "tree's first carve-out" argument was corrected | **D-22** — plan 22-01 |
> | 3 | Keep WAL, set before the autocommit flip, named file-backed test | **D-21** — plan 22-01 |
> | 4 | Yes — `fail_active_jobs() -> int` from `cursor.rowcount` | Claude's Discretion — plan 22-05 |
> | 5 | Yes — add the `ast` test; it is load-bearing, not stylistic | **D-24** — plan 22-03 |


1. **Does the planner accept the lock-only `@_locked`, diverging from D-12's parenthetical?**
   - What we know: the literal reading is *impossible* — `with self._conn: self._conn.close()` raises, and so does every hand-rolled variant (verified four ways).
   - What's unclear: whether the user considers the parenthetical load-bearing or incidental. D-15 explicitly wants `close()` decorated *and* explicitly rejects an exemption list, so something has to give.
   - Recommendation: implement lock-only, record the divergence in the plan with the measured evidence, and flag it in the phase summary. The parameterised `@_locked(transaction=False)` alternative exists if the user objects.

2. **How is `S608` to be satisfied without a suppression?**
   - What we know: the rule fires on every natural spelling of `_COLUMNS`-driven SQL; CLAUDE.md forbids `# noqa` and rule-disabling; the module-constant form passes cleanly.
   - What's unclear: whether the user reads the constant form as an acceptable construction or as a `# noqa` in disguise.
   - Recommendation: use the constant form with an explicit safety comment, and surface the choice in the plan so it is a decision rather than a discovery. Escalate if the planner is not comfortable.

3. **Keep, drop, or condition `PRAGMA journal_mode=WAL`?** (CONTEXT.md leaves this open.)
   - What we know: it must move before `autocommit = False` if kept; it is inert on `:memory:`; the mode persists in the file header so it only genuinely needs setting once.
   - Recommendation: **keep it, moved before the flip**, and add the file-backed assertion test. Dropping it would be a behaviour change to production concurrency in a phase whose stated behaviour change is "none intended".

4. **Does `fail_active_jobs()` return a count?** Not addressed by CONTEXT.md's discretion note.
   - Recommendation: yes — `-> int` from `cursor.rowcount`. The method has no production caller this phase, so its tests are its only consumer, and a return value is what they assert against.

5. **Should the plan add the `ast`-based "no public method calls another public method" test?** STOR-01 states the rule; nothing currently enforces it, and Pitfall 3 shows the consequence is silent data loss rather than a deadlock.
   - Recommendation: yes, and it is ~15 lines. If the planner disagrees, at minimum make it a code-review checklist item.

## Sources

### Primary (HIGH confidence)

- **Executed on this machine** — Python 3.14.2, libsqlite 3.34.1, this project's `.venv` via `uv run`: nine experiment scripts covering `autocommit` + DDL + `PRAGMA` atomicity, WAL ordering, `close()` under the connection CM, nested connection CMs, the full five-shape migration ladder, nine-case `prune()` equivalence, `EXPLAIN QUERY PLAN`, `rowcount`, `json_each`, `ParamSpec`/PEP 695 decorator typing, `sqlite3.Row` type strictness, `functools.wraps` marker propagation, and the 200-round stress test under the project's own `pytest` configuration.
- **Python 3.14 `sqlite3` documentation** — https://docs.python.org/3.14/library/sqlite3.html — `Connection.autocommit`, `LEGACY_TRANSACTION_CONTROL`, `isolation_level`, the connection context manager ("does nothing" when no transaction is open), `check_same_thread` ("write operations may need to be serialized by the user to avoid data corruption"), `row_factory`, `sqlite3.Row`, `Cursor.rowcount`, `executescript` implicit commit.
- **Context7 `/python/cpython`** — `Doc/library/sqlite3.rst` — corroborated the same statements independently of the rendered docs.
- **Repository, read directly** — `src/saneless/job.py`, `src/saneless/vocabulary.py`, `src/saneless/exceptions.py`, `src/saneless/pipeline.py` (`ScanResult`, `PipelineEvent`), `src/saneless/worker.py`, `src/saneless/web/routes.py`, `tests/test_job.py`, `pyproject.toml`, `docs/`.
- **Planning artefacts** — `.planning/phases/22-job-store-hardening/22-CONTEXT.md`, `.planning/REQUIREMENTS.md`, `.planning/ROADMAP.md`, `.planning/reviews/2026-09-09-code-review.md` §§ C-07, M-03, N-13, N-14, N-17.

### Secondary (MEDIUM confidence)

- **SQLite isolation documentation** — https://www.sqlite.org/isolation.html — used for the *negative* result: SQLite does not guarantee what a statement sees of concurrent same-connection modification. This is why Mechanic 5's snapshot behaviour is reported as observed-not-guaranteed.

### Tertiary (LOW confidence)

- None. Every claim above is either executed here or cited to an official source; items that are neither are listed in § Assumptions Log.

## Metadata

**Confidence breakdown:**

- Standard stack: **HIGH** — entirely stdlib, all already in use, no new packages.
- Migration ladder: **HIGH** — the full ladder was built and run against five database shapes with the observed output reproduced above.
- Transaction control (D-13): **HIGH** — executed and independently cited to the Python 3.14 docs.
- WAL ordering (Pitfall 1): **HIGH** — executed, and the `:memory:` blind spot reproduced directly.
- `@_locked` + type checkers: **HIGH** — the exact code passed `ruff`, `ruff format`, `ty` and `pyrefly` inside `src/saneless/`.
- `close()` collision (Pitfall 2): **HIGH** — reproduced four ways including a hand-rolled context manager.
- Nested connection CM (Pitfall 3): **HIGH** — executed on a file database; the surviving rows are shown.
- `prune()` equivalence and `rowcount`: **HIGH** for the result, **MEDIUM** for the durability of the query plan across SQLite versions (A1).
- `sqlite3.Row` typing: **HIGH** — probed against both checkers.
- `S608` friction: **HIGH** for the observed behaviour, **MEDIUM** for stability across ruff versions (A4).
- D-11 sufficiency sweep: **MEDIUM-HIGH** — the `ScanResult` field-for-field match is strong evidence; APPL-03's pass-B clause rests on a reading of "during pass B" that the planner should confirm (A3).
- Documentation targets: **HIGH** — grepped exhaustively; the absence of a storage page is itself the finding.

**Research date:** 2026-09-10
**Valid until:** 2026-10-10 (30 days — stdlib mechanics are stable; the ruff `S608` heuristic and the SQLite query plan are the two items that could drift)
