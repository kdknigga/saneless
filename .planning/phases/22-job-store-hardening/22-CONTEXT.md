# Phase 22: Job Store Hardening - Context

**Gathered:** 2026-09-10
**Status:** Ready for planning

<domain>
## Phase Boundary

This phase delivers a `JobStore` that is **safe under concurrency** and can **evolve its schema honestly**. Everything it does lives in `src/saneless/job.py` plus its tests and the storage documentation.

**In scope:**
- An `RLock` taken by every public method of `JobStore` (STOR-01, C-07).
- A `PRAGMA user_version` migration ladder that replaces the bare `ALTER TABLE ... except: pass` (STOR-02, N-13).
- Six new columns — `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning`, `owner_token` — added in **one** migration step (STOR-03).
- The row-to-`Job` mapping and the column list reduced to exactly one occurrence each; `prune()` counting from one statement (STOR-04, N-17).
- Two new query methods: `fail_active_jobs()` and `list_pending()` (STOR-05, M-03, U-06).
- The storage docs corrected in-phase.

**Explicitly NOT in scope — these belong to later phases and must not be pre-empted here:**
- *Calling* `fail_active_jobs()` at startup — ROBU-05, **Phase 26**. This phase ships the method unwired.
- *Writing* any of the six new columns — **Phase 23** (`outcome`, `warning`) and **Phase 30** (page counts, `owner_token`). This phase ships them unwritten.
- *Rendering* page counts, queue position, or the owner-only flip prompt — APPL-03/APPL-08/APPL-09, **Phase 30**.
- Moving the database off `tmp_dir` onto `output.data_dir` — OUTC-09, **Phase 23**. See D-18.
- Hardening `close()` against use-after-close, and the shutdown ordering that causes it — M-03, **Phase 26**. See D-15.
- Worker resilience, 429 backpressure, sync routes — C-07's *other half*, **Phase 26**.
- Any new `JobState` member. `FALLBACK` is **Phase 23's**; `SCANNING_REVERSE` is **Phase 25's**. See D-19.

**Behaviour change:** none intended. `prune()` is refactored to a single statement, and D-16 records the equivalence argument plus the test obligation that proves it.

</domain>

<decisions>
## Implementation Decisions

The user selected all four gray areas and answered every question. Where an answer diverges from the option flagged "Recommended" it is called out explicitly below (D-15 and D-18 both do). These decisions are **locked for planning**: a planner may implement them, not relitigate them. A planner who believes one is wrong should raise it rather than silently implement the other branch.

### The migration ladder

- **D-01: the ladder owns table creation.** `CREATE TABLE IF NOT EXISTS` is deleted from `__init__`. Migration **step 1 is the `CREATE TABLE`**, at the current (S3) ten-column shape. A fresh database runs the whole ladder from 0 to head; a legacy database joins partway. Rationale: with a `CREATE TABLE` in `__init__` *and* an accumulating list of `ALTER`s, the current schema is spelled in two places — which is exactly how N-13 happened (the `thumbnail` column was added to `CREATE TABLE` in `5bd6158` and never got a migration at all). One code path means the ladder is self-evidently complete, because it can build the schema from nothing.

- **D-02: the ladder is a module-level tuple of callables in `job.py`.** `_MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...]`, where index + 1 is the version. Rejected: numbered `.sql` files under a package directory (adds package-data plumbing to `pyproject.toml` and puts schema logic where `ty` and `pyrefly` cannot see it) and adopting a migration library (CLAUDE.md prefers external packages over reimplementation, but a two-step stdlib-`sqlite3` ladder against one table is roughly thirty lines, and a dependency here buys rollback and branching this project will never use — REQUIREMENTS.md "Out of Scope" already rules out new runtime dependencies on the same grounds).

- **D-03: fresh vs. legacy is decided by probing for the `jobs` table.** Every database that has ever existed reports `PRAGMA user_version = 0`, because nothing has ever set it. So step 0 is:
  - `user_version = 0` **and no `jobs` table** → fresh. Run the full ladder, stamp the head version.
  - `user_version = 0` **and `jobs` exists** → legacy. Enter the ladder after step 1.
  - `user_version = N > 0` → run steps `N+1`..head.

  Rejected: reconciling purely via `PRAGMA table_info` (idempotent and immune to shape branching, but that is schema reconciliation, not the ladder STOR-02 asks for) and stamping `PRAGMA application_id` (a guarantee nothing currently needs).

- **D-04: only the S3 shape is supported as a legacy input, and S1/S2 ladder steps are deliberately NOT written.** Measured during discussion: three schemas have existed in git history —

  | Shape | Commit | Columns |
  |---|---|---|
  | S1 | `b95f88f` (Phase 01) | no `thumbnail`, no `error_category` |
  | S2 | `5bd6158` (Phase 02) | + `thumbnail`, **no migration was ever written for it** |
  | S3 | `dc9b8af` (Phase 07) | + `error_category`, via the bare `ALTER ... except: pass` |

  S2 is the shape N-13 verified fails on every `create_job` with *"table jobs has no column named thumbnail"*. But **nothing has ever been published**: `git tag` returns zero tags, `.github/workflows/release.yml` triggers only on `push: tags: ["v*"]`, and Phase 31's success criterion 2 still requires the release workflow to be *"proven end to end … verified by an actual `pip install` and `docker pull` from a clean machine"*. So the only databases in existence are the user's own, and they are S3. S1/S2 steps would be untestable-in-anger dead code — the same "do not ship what nothing can reach" logic as Phase 21's D-06.

  **Verification note for the planner:** N-13 asks for "a test that opens a first-release schema". Under D-04 that test opens an **S3** database (fixture-constructed at the `dc9b8af` shape), not S1. Do not write S1/S2 fixtures.

- **D-05: step 2 asserts before it acts, and fails loudly.** Before adding the six v2 columns to a legacy database, read `PRAGMA table_info(jobs)` and assert the ten S3 columns are present. If any are missing, **raise**, naming the database file path and the missing column names. Rationale: D-04's assumption ("any pre-existing `jobs` table is S3") is an assumption, and if it is ever wrong the alternative is a database that silently keeps working until the next `create_job` crashes — the exact failure mode N-13 documents. A loud failure at open beats a silent runtime crash. Rejected: self-healing by adding whatever is missing, which smuggles the S1/S2 ladder back in under another name and is un-testable for the same reason those steps were ruled out.

- **D-06: physical column order is not a contract.** An S2→S3 database migrated by the old bare `ALTER` has `error_category` **last**; a freshly created S3 database has it **sixth**. This is harmless today only because every `SELECT` and `INSERT` names its columns. The ladder and the row mapping must never assume ordinal position. D-17's `sqlite3.Row` requirement makes this structural rather than a matter of care.

### The six speculative columns

- **D-07: columns AND `Job` dataclass fields, but no writers.** All six become columns *and* fields on `Job` with defaults, read back through the single row mapping. No production code writes them in this phase. Rationale: STOR-04 requires the row mapping to exist exactly once — a column the mapping ignores means Phase 23 has to edit the mapping anyway, which is precisely the ad-hoc schema change this phase exists to prevent. Rejected: DB columns only (keeps the "one migration" promise while breaking the "one row mapping" promise) and adding write paths (`set_outcome`, `set_page_counts`, …) — writes belong to the phases that have something true to write, and shipping them here would half-satisfy Phase 23's and Phase 30's criteria in the wrong phase.

  This is a **deliberate, bounded exemption** from Phase 21's D-06 ("no dead vocabulary"). It applies to *columns and the fields that mirror them*, and stops there. It does not extend to enum members — see D-17.

- **D-08: the three page-count columns are nullable `INTEGER`, defaulting to `NULL`.** `pages_scanned`, `pages_removed`, `pages_uploaded` map to `int | None = None`. `NULL` means "never recorded", which is true of every row that exists today and of every job that fails before the scanner opens; `0` would assert a measured zero. Phase 30 (APPL-03) can then render "—" rather than a false "0 pages". Note also that `ALTER TABLE ... ADD COLUMN ... NOT NULL DEFAULT 0` would backfill every historical row with that lie.

- **D-09: `outcome` is `TEXT`, read back as `ScanOutcome | None`.** This mirrors exactly how `state` and `error_category` already round-trip (`JobState(row[3])`, `ErrorCategory(row[5]) if row[5] else None`). `NULL` until Phase 23 writes it. A stored value the current enum does not know raises on read — consistent with today's behaviour and with Phase 21's D-10. Rejected: keeping it a raw `str`, which reintroduces exactly the stringly-typed protocol N-38 and Phase 21 spent a phase removing.

  `warning` and `owner_token` are `TEXT` nullable → `str | None = None`.

- **D-10: `owner_token`'s semantics are already decided elsewhere; this phase only picks the column's shape.** REQUIREMENTS.md § "Out of Scope" states the trusted-LAN decision stands and *"the APPL-09 owner token is a footgun guard for the flip prompt, not an auth mechanism"*; APPL-09 pins it to an HttpOnly, `SameSite=Lax` cookie set at submit. Nothing about authentication, rotation, or expiry is this phase's to decide.

- **D-11 (MANDATORY planner obligation): prove the six-column list is sufficient before writing the migration.** The entire value of "one migration" is that no later phase reaches for another `ALTER`. Before writing step 2, re-read the OUTC, DPLX and APPL requirement blocks in `.planning/REQUIREMENTS.md` and confirm nothing else needs persisting. **Call out APPL-03 specifically** — *"manual duplex shows front and back counts during pass B"* — and determine whether that is live worker state or a persisted per-job fact. If anything is missing, it goes in **this** migration step, not a later one.

  Swept during discussion and found NOT to need columns: `FALLBACK` is a `JobState`, not a column (OUTC-02); the preserved-PDF path goes in the existing `error` field (OUTC-04, *"the job's error names that path"*); queue position comes from `list_pending()` (APPL-08). APPL-03's duplex split is the one that was not resolved.

### Lock discipline and what the proof proves

- **D-12: a `@_locked` decorator on every public method, enforced by a reflective test.** A small `@_locked` decorator takes `self._lock` (and the connection's transaction context) around each public method. A test enumerates `JobStore`'s public callables via `inspect` and asserts every one carries the marker.

  This is **Phase 21's D-09 pattern moved from enum members to methods**: parametrise over the real thing, never over a hand-written list, so the thing you forgot fails the suite by itself. A 200-round stress test cannot fail when someone adds an unlocked method next year; a test that enumerates the class can. Rejected: inline `with self._lock, self._conn:` at each site (exactly C-07's prescription and verified 0/10 failures, but nothing detects the eighth method someone forgets) and a public-wrapper / private-`_impl` split (satisfies STOR-01's "public methods never call other public methods" structurally, at the cost of doubling the method count in a 300-line module — `RLock` reentrancy makes the failure mode it guards against non-fatal anyway).

  **The reflective test carries no exemption list.** See D-14.

- **D-13: `conn.autocommit = False`.** Python 3.12+ supports PEP 249 transaction control, and this project is on 3.14. Under the default `LEGACY_TRANSACTION_CONTROL` with `isolation_level="DEFERRED"`, `with conn:` **does nothing when no transaction is open** — which is the case for the `PRAGMA` and DDL the ladder runs, so a version stamp could land outside the transaction and a half-applied step be recorded as complete. With `autocommit = False`, a transaction opens before *any* statement, so a step's DDL and its `PRAGMA user_version = N` commit or roll back together. Rejected: keeping legacy mode plus `with self._conn:` (C-07's literal prescription, minimal diff, but it would force the planner to open transactions explicitly for every migration step anyway).

- **D-14: the 200-round concurrency test asserts zero exceptions AND a data invariant.** Two threads run a mix of writes and reads for 200 rounds; afterwards, assert every job the test created is readable with the state it was last set to, and that the row count reconciles with what was created minus what was pruned.

  Rationale, and the reason this is not just "no exceptions": zero exceptions demonstrates that `threading.RLock` is reentrant, which is Python's contract, not this project's. Criterion 1 also demands *"no interleaved-transaction corruption"*, and the data invariant is what actually speaks to that. Rejected: a deliberate no-lock control run asserting the unlocked store *does* raise — strongest evidence, but it pins a CPython/SQLite implementation detail, and a future build that happens not to raise would turn the suite red for no real reason.

  The review's own evidence for C-07 (10/10 failures unlocked, 0/10 locked, both in-memory and file-backed) is the baseline this test regresses against; `.planning/reviews/2026-09-09-code-review.md` § C-07 describes the stress shape.

- **D-15 (DIVERGES from the recommendation, by explicit user choice): `close()` gets `@_locked` and nothing else.** No `_closed` flag, no saneless-owned use-after-close error. M-03's verified failure — shutdown closing the store while the worker is mid-scan, producing `Cannot operate on a closed database` — is **Phase 26's** to fix, along with the shutdown ordering that causes it. The decorator is applied purely so that `close()` cannot land mid-transaction and so the reflective test in D-12 needs no exemption list. Rejected in the same breath: an `_EXEMPT = {"close"}` set in the test, because an exemption set is the hand-written list Phase 21's D-09 warned about, and it is where the next exemption gets quietly added.

### `prune()`, the row mapping, and the db path

- **D-16: `prune()` becomes one combined `DELETE` and reports `cursor.rowcount`.**

  ```sql
  DELETE FROM jobs
   WHERE created_at < ?
      OR id NOT IN (SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)
  ```

  This matches criterion 5 literally (*"`prune()` reports its count from one statement"*) and removes the race N-17 documents, where a concurrent `create_job` between the two `COUNT(*)` statements made the result wrong — a scratch test forced it to return `-1`.

  **The equivalence claim, worked through during discussion:** both predicates order by `created_at`, so the age-expired set is always a *prefix* of the oldest rows. Therefore the union of the two predicates equals the sequential composition in every case. Checked: 600 rows / 200 expired / `max_rows=500` → 200 deleted, 400 remain, both ways; 600 / 50 / 500 → 100 deleted, 500 remain, both ways; 600 / 0 / 500 → 100 both ways. This is why the single statement is *not* a behaviour trade-off.

  **Test obligation:** the existing `test_prune_by_age`, `test_prune_by_count` and `test_prune_no_deletions` in `tests/test_job.py` must pass **unchanged**. That is the evidence for the equivalence argument. Add a test that a concurrent insert during prune cannot corrupt the returned count.

  **Latent assumption, worth a comment in the code:** `created_at` is stored as an ISO-8601 string via `datetime.now(tz=UTC).isoformat()`, and both the `<` comparison and the `ORDER BY` are lexicographic. That is correct only because every row is UTC with a `+00:00` offset. If a non-UTC timestamp ever reaches this column, both the cutoff and the ordering silently become wrong.

- **D-17: `_COLUMNS` tuple + `sqlite3.Row` + a single `_row_to_job`.** A module-level `_COLUMNS: tuple[str, ...]` builds both the `SELECT` list and the `INSERT` placeholders — that is STOR-04's "the column list exists once". `self._conn.row_factory = sqlite3.Row` gives name-based access, and one `_row_to_job(row) -> Job` performs the conversion — that is "the row mapping exists once". Positional indexing across sixteen columns is exactly how the `thumbnail` bug in N-13 became possible; `JobStore` owns its connection exclusively (nothing else ever touches it), so the `row_factory` change has no blast radius. Rejected: a positional `_row_to_job` without `sqlite3.Row` (smaller diff, but `row[11]` across sixteen columns stays as fragile as it reads) and `SELECT *` with `sqlite3.Row` alone (the `INSERT` still has to spell the columns out, so "the column list appears exactly once" would not actually be true).

- **D-18 (DIVERGES from the recommendation, by explicit user choice): the duplicated database path is left entirely to Phase 23.** N-17's closing sentence asks to *"expose the database path once instead of computing `tmp_dir / \"saneless.db\"` in both `cli.py` and `app.py`"*, and N-17 is STOR-04's cited finding — so this was arguably in scope. It is deliberately out. STOR-04's requirement text covers only the row mapping and the column list; the path dedup lives in N-17's prose. Phase 23 is touching both call sites regardless when OUTC-09 moves the database onto `output.data_dir`, so it dedups and moves in one step. **Do not centralize the path in this phase.**

  Current duplication, for Phase 23's benefit: `src/saneless/cli.py:227` and `src/saneless/web/app.py:59`.

### Vocabulary

- **D-19: "FAILED" throughout STOR-05, OUTC-01 and the roadmap success criteria means the existing `JobState.ERROR`.** `JobState` has seven members — `PENDING`, `SCANNING`, `AWAITING_FLIP`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR` (`src/saneless/vocabulary.py:36-45`). There is no `FAILED`. M-03's own prescription confirms the reading: *"sets every active row to `ERROR` with `error=\"Interrupted by restart\"`"*.

  **This phase adds no `JobState` member and renames none.** Phase 21's D-06 still holds; `FALLBACK` is Phase 23's and `SCANNING_REVERSE` is Phase 25's. Renaming `ERROR` to `FAILED` was considered and rejected: the value is persisted as SQLite `TEXT` and read back through `JobState(row[3])`, so a rename means a *data* migration for the `state` column on top of everything else here, and Phase 21 has just finished locking this vocabulary.

  Recorded explicitly because a planner reading STOR-05 literally could reasonably invent a `JobState.FAILED`.

### Post-research amendments (2026-09-10)

These were added after `22-RESEARCH.md` settled the mechanics by execution on Python 3.14.2 / libsqlite 3.34.1. They are **locked on the same footing as D-01..D-19**. Where one corrects an earlier decision, the correction is a measured constraint, not a change of preference.

- **D-20 (CORRECTS D-12): `@_locked` takes the lock ONLY. The transaction context moves into method bodies.** D-12 as written said the decorator takes `self._lock` *"(and the connection's transaction context)"*. That parenthetical is **impossible**, verified four ways: `with conn: conn.close()` raises `sqlite3.ProgrammingError: Cannot operate on a closed database` from `Connection.__exit__`, because `__exit__` must commit or roll back a connection the body has already closed. A hand-rolled context manager that checks `conn.in_transaction` first fails identically, because `in_transaction` itself raises on a closed connection. C-07's own prescription (`with self._lock, self._conn:` inline) has the same defect; the review simply never showed `close()`.
  - **Resolution:** `@_locked` wraps `with self._lock:` only. Each method that executes SQL opens `with self._conn:` in its own body. `close()` is then `@_locked def close(self): self._conn.close()` — which satisfies **D-15 literally** and **D-12's purpose** (the reflective test still enumerates every public method and needs no exemption list). The rejected alternative, a parameterised `@_locked(transaction=False)` used only on `close()`, costs a decorator factory for no gain now that the transaction boundary is visible in the body.
  - Verified type-clean shape (zero diagnostics from ruff, ruff format, `ty` and `pyrefly`): PEP 695 `def _locked[**P, R]` — ruff's `UP047` *requires* it over `ParamSpec`/`TypeVar` objects — with `Concatenate[JobStore, P]` so `self` is typed, `functools.wraps`, and the marker set via `setattr(wrapper, _LOCKED_MARKER, True)` where `_LOCKED_MARKER` is a module constant (`B010` fires on a literal attribute name; a direct `wrapper.__saneless_locked__ = True` makes both checkers complain).
  - **Marker detection in the reflective test:** `getattr(member, _LOCKED_MARKER, False)` over `inspect.getmembers(JobStore, inspect.isfunction)`. Rejected: `hasattr(member, "__wrapped__")` (any `functools.wraps` decorator sets it) and `__qualname__` (`functools.wraps` copies the wrapped function's, so it is identical either way). Note the test is blind to a public `@property` — worth one comment line.

- **D-21 (RESOLVES the WAL discretion item): keep `PRAGMA journal_mode=WAL`, set it BEFORE `conn.autocommit = False`, and pin the ordering with a file-backed test.** `conn.autocommit = False` opens a transaction immediately (`in_transaction` is `True` before any statement), and SQLite refuses a WAL switch inside a transaction — *on a file database*. An in-memory database has no journal to change, so it silently returns `'memory'` and no error. **Every test in `tests/test_job.py` constructs `JobStore()` with the default `":memory:"`**, so the wrong ordering passes 332/332 tests and raises `OperationalError` on the operator's first `saneless serve`. The pinning test must construct a real `JobStore(str(tmp_path / "x.db"))` and assert `PRAGMA journal_mode` reads back `'wal'`; without it, a future refactor reverses the ordering undetected.

- **D-22: ruff `S608` is resolved with module-level constants whose leading token is a Name.** STOR-04 requires the column list to exist exactly once, SQL cannot parameterise identifiers, so some interpolation is unavoidable — and `S608` flags every direct form (f-string, concatenation, `.format`, `%`). CLAUDE.md forbids `# noqa`, `# type: ignore` *and* rule-disabling, and SWP-08 is separately committed to removing the tree's eight existing suppressions, so there is no `per-file-ignores` escape that respects the project rule. **Resolution:** `_SELECT_JOBS = "SELECT"`, `_INSERT_JOBS = "INSERT INTO jobs"` etc., then `f"{_SELECT_JOBS} {_COLUMN_LIST} FROM jobs"` — verified clean, because the rule's heuristic keys on the string *beginning* with a SQL verb literal. **Each such statement constant MUST carry a comment stating why the interpolation is safe** (every interpolated value is a module-level `tuple[str, ...]` literal that no user input can reach), so a reader is not left to infer safety from the naming. Building the statements once at import is also better than rebuilding them per call. Rejected: `json_each(?)` (fully static, verified working, but it covers only the `IN` clauses and adds a soft JSON1 dependency) and a `pyproject.toml` carve-out (that is the rule-disabling CLAUDE.md prohibits, and would be the tree's first).

- **D-23: add `StorageError(SanelessError)` to `src/saneless/exceptions.py`; the D-05 guard raises it.** None of the existing types fits a wrong-shaped database — it is not a config, scanner, feeder or Paperless error — and EXC-01 forbids third-party exception types escaping a module boundary, ruling out `sqlite3.OperationalError`; a bare `RuntimeError` gives the CLI nothing to catch. `exceptions.py` is a leaf (`vocabulary.py` already imports it), so `job.py` may import from it with no inversion. This *feeds* Phase 28 rather than pre-empting it: EXC-02's CLI catch set (`ConfigError`, `ScanError`, `PaperlessError`, `PdfError`) gaining a fifth member is exactly what that phase exists to reconcile. This is the one place this phase adds vocabulary, and it is deliberate.

- **D-24: add an `ast`-walking test asserting no public `JobStore` method calls another public method.** Verified: `sqlite3` connection context managers **do not nest** — an inner `with conn:` commits the *outer* transaction. In the executed case, rows 1 and 2 survived a rollback that should have discarded all three. With the transaction context now in method bodies (D-20), a public method calling another public method silently commits half its work. `RLock` re-entrancy prevents the *deadlock*; nothing prevents the *incorrectness*. STOR-01's "public methods never call other public methods" is therefore a correctness rule, and ruff cannot see it. Shared logic goes in **private, undecorated** helpers (`_row_to_job`, `_migrate`). Same family as D-12's reflective test: convert a silent future failure into an immediate loud one.

- **D-25: the ladder commits per step.** `for index in range(version, len(_MIGRATIONS)): _MIGRATIONS[index](conn); conn.execute(f"PRAGMA user_version = {index + 1}"); conn.commit()`. A step that fails leaves a *valid* lower-version database rather than nothing. Two further verified constraints: **`PRAGMA user_version = ?` cannot be parameterised** (`OperationalError`) — it must be interpolated, and `S608` does not cover `PRAGMA`; and **`__init__` must `rollback()` and `close()` before re-raising** on the D-05 guard's failure path, or a failed `JobStore(path)` leaks an open connection holding a `BEGIN DEFERRED`. The fresh-vs-legacy probe of D-03 is `SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'`.

- **D-26: `sorted()` every `frozenset` before binding it into SQL parameters.** `ACTIVE_STATES` is a `frozenset[JobState]`; iteration order varies per process with `PYTHONHASHSEED`. The result is correct either way, but an unsorted bind makes a failing test's parameter dump irreproducible across runs.

- **D-27 (RESOLVES D-11): the six columns are sufficient. No seventh column.** The mandated sweep was performed. Five of the six map field-for-field onto Phase 21's `ScanResult` (`pipeline.py:108-115`). APPL-03's *"manual duplex shows front and back counts during pass B"* is **transient progress feedback, not a persisted fact**: `PipelineEvent` is a payload-free `StrEnum`, nothing writes per page, and `pages_scanned` recorded at the end of pass A carries the front count. D-11's obligation is discharged; the planner does not need to repeat it.

- **D-28: `tests/test_job.py:134` `test_jobstore_migration_adds_column` must be rewritten, not restyled.** It constructs an **S2** database (no `error_category`). Under D-05 that database now correctly *raises*. The replacement builds an **S3** fixture at the `dc9b8af` shape and asserts it migrates to `user_version = 2` with sixteen columns. A second test should assert an S2 fixture raises `StorageError` naming the missing column, leaving `user_version = 0` and the original columns untouched — verified to be the actual behaviour.

- **D-29: the concurrency test uses `ThreadPoolExecutor` + `Future.result()`, never raw `threading.Thread`.** Raw threads swallow exceptions into `threading.excepthook`, so the test would pass green while the store was broken; `Future.result()` re-raises in the calling thread. Start both workers on a `threading.Barrier(2)` with a timeout — no sleeping, no polling, cannot hang. Measured cost of the full 200-round run: **0.28 s** against Phase 20's 60 s timeout, zero warnings under `filterwarnings = ["error"]`. **Do not add an unlocked control run** (already rejected in D-14, now with a second reason): on Python 3.14 with `autocommit = False` the unlocked failure signature is unstable — observed `TypeError: 'NoneType' object is not subscriptable` on `:memory:` and `InterfaceError: bad parameter or other API misuse` on a file.

- **D-30: pin `prune()`'s subquery behaviour with a shuffled-insert-order regression test.** The equivalence argument in D-16 was verified across nine cases with `cursor.rowcount` exact in all of them, but the `LIST SUBQUERY` materialisation it depends on is **not a documented SQLite guarantee**. A test that inserts rows in shuffled `created_at` order and asserts the retained set is the newest `max_rows` is what stops a future SQLite version changing the plan underneath us. Note also that this machine ships libsqlite **3.34.1** (RHEL 9) while CI may ship newer — the test is what makes the difference visible.

- **D-31: the "storage documentation" the phase goal promises does not exist as a page.** `docs/` has no job-store or persistence page. What exists is four scattered sentences plus a structural gap in `architecture.md`; `docs/reference/configuration.md:48-49`'s *"completed job history"* is the one sentence that is actually wrong today. The planner decides whether to correct the sentences in place or add a short persistence section — but must not claim any of the six new columns has a consumer yet.

### Claude's Discretion

Left genuinely open to the planner:

- **`fail_active_jobs()`'s signature.** M-03 prescribes `fail_active_jobs(reason)`; STOR-05 implies a fixed *"server restarted"* reason. Either is acceptable. Whether it also sets `error_category` is open — note that N-14 observes `error_category` is currently written but never read, and Phase 21's D-12 left it deliberately unwired.
- **`list_pending()`'s exact predicate and ordering.** STOR-05 says "queued jobs ordered by creation time". `PENDING` is the only pre-scan state, and `ORDER BY created_at ASC` is the obvious reading; confirm against `ACTIVE_STATES` rather than hand-listing states.
- **Whether the ladder runs inside `__init__` or an explicit `migrate()` called from it.** Note that every test constructs a `JobStore` directly, so whichever is chosen must work with no extra call.
- **Whether `PRAGMA journal_mode=WAL` survives.** It is inert for the `:memory:` databases every test uses. Keep it, drop it, or make it conditional — but do not let it become the reason a migration step behaves differently in tests than in production.
- **Task and commit breakdown**, provided the ladder and `_COLUMNS`/`_row_to_job` land before anything that depends on them.
- **The exact prose of the storage documentation update**, subject to the constraint that it must not describe any of the six new columns as having a consumer yet.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### The findings this phase resolves
- `.planning/reviews/2026-09-09-code-review.md` § C-07 (~line 380) — the finding behind STOR-01. Contains the verified stress-test result (10/10 failures unlocked, 0/10 locked, both in-memory and file-backed) and the prescribed `with self._lock, self._conn:` shape. **Its "make the worker resilient" and "make `submit()` use `put_nowait` and return 429" halves are Phase 26's, not this phase's.**
- `.planning/reviews/2026-09-09-code-review.md` § N-13 (~line 865) — the finding behind STOR-02. Documents that the bare `except` also swallows "database is locked", "readonly database" and "no such table", and that the `thumbnail` column never got a migration at all. **Its "a test that opens a first-release schema" means an S3 fixture under D-04, not S1.**
- `.planning/reviews/2026-09-09-code-review.md` § N-17 (~line 873) — the finding behind STOR-04. Two copies of the row mapping, three of the column list, and the `prune()` two-`COUNT(*)` race that a scratch test forced to `-1`. **Its closing db-path sentence is deliberately deferred — see D-18.**
- `.planning/reviews/2026-09-09-code-review.md` § M-03 (~line 407) — the finding behind STOR-05's `fail_active_jobs()`. Read it for the exact prescription (`ERROR`, `error="Interrupted by restart"`, called in `lifespan` before `worker.start()`) and note that **the calling is ROBU-05 / Phase 26; only the method is this phase's.** Also the source of the `tmp_dir` durability observation that becomes OUTC-09.
- `.planning/reviews/2026-09-09-code-review.md` § N-14 (~line 867) — `error_category` written but never read; relevant to the `fail_active_jobs()` discretion item.
- `.planning/REQUIREMENTS.md` lines 26-30 — STOR-01..STOR-05 verbatim.
- `.planning/REQUIREMENTS.md` § "Out of Scope" (~line 201) — **the `owner_token` semantics (D-10) and the no-new-runtime-dependency constraint (D-02) both come from here.**

### What later phases will do with this work — read to see what NOT to build
- `.planning/ROADMAP.md` § "Phase 22: Job Store Hardening" — the goal and the five success criteria this phase is verified against.
- `.planning/ROADMAP.md` § Overview, ordering point 2 — why the lock and the migration ladder must land before typed results, and the explicit instruction that "all result columns are added in one migration so no later phase reaches for another bare `ALTER TABLE ... except: pass`".
- `.planning/ROADMAP.md` § "Phase 23: Honest Outcomes and Never Lose a Scan" — what writes `outcome` and `warning`, and where the database path moves (D-18).
- `.planning/ROADMAP.md` § "Phase 26: Worker and Web Robustness" — success criterion 4 is what calls `fail_active_jobs()`; it also owns `close()` and shutdown ordering (D-15).
- `.planning/ROADMAP.md` § "Phase 30: Appliance Layer" — success criteria 3 and 5 are what read the page counts and `owner_token`.
- `.planning/REQUIREMENTS.md` § OUTC-01..OUTC-10, § DPLX-01..DPLX-07, § APPL-01..APPL-12 — **the sweep D-11 obliges the planner to redo. APPL-03 is the open one.**

### Standing constraints from earlier phases
- `.planning/phases/21-vocabulary-and-contracts/21-CONTEXT.md` § D-02 — `vocabulary.py` is a leaf module; `job.py` re-exports `JobState` and `ErrorCategory` and every existing `from saneless.job import JobState` must keep resolving. The dependency runs one way and this phase must not reverse it.
- `.planning/phases/21-vocabulary-and-contracts/21-CONTEXT.md` § D-06, D-07 — why no `JobState` member and no `ScanOutcome.FAILED` land here (D-19).
- `.planning/phases/21-vocabulary-and-contracts/21-CONTEXT.md` § D-09 — the parametrise-over-the-real-thing test pattern that D-12 extends from enum members to methods.
- `.planning/phases/20-ci-gate/20-CONTEXT.md` § "Toolchain and supply chain" (D-16) — `ty` and `pyrefly` are both mandatory and equal; findings are fixed, never suppressed with `# type: ignore` or `# noqa`.
- `.planning/phases/20-ci-gate/20-CONTEXT.md` § "Hang guard" — `timeout = 60` with `timeout_method = signal`, `filterwarnings = ["error"]`, `--strict-config` and `--strict-markers` are live in `pyproject.toml`. **The 200-round concurrency test of D-14 must finish well inside 60 seconds, and must not emit a warning.**
- `CONTRIBUTING.md` — the five checks every change must pass.
- `CLAUDE.md` — Python 3.14, `uv`, `prek`; ruff + ruff format + `ty` + `pyrefly` all clean, no suppressions.

### Library documentation
- Python 3.14 `sqlite3` docs — `Connection.autocommit` and `LEGACY_TRANSACTION_CONTROL` (D-13), the `check_same_thread` note that *"write operations may need to be serialized by the user to avoid data corruption"* (D-12), `Connection.row_factory` and `sqlite3.Row` (D-17), and the connection context manager's "does nothing if no transaction is open" behaviour. Fetch via Context7 rather than from memory.

</canonical_refs>

<code_context>
## Existing Code Insights

### Verified baseline (measured during discussion, 2026-09-10)
- Working tree clean on branch `autodev`. Phase 21 complete through `231d48a`.
- `src/saneless/job.py` is **301 lines**: one `Job` dataclass (10 fields) and one `JobStore` (7 public methods).
- Phase 20 baseline stands: 332 tests pass in ~27s with `-m "not browser"`.
- `git tag` returns **zero tags**; `pyproject.toml` version is `0.1.0`; `release.yml` triggers only on `push: tags: ["v*"]`. This is the evidence for D-04.

### The defects, as measured
- **No lock.** `job.py:78` — `sqlite3.connect(db_path, check_same_thread=False)`, one connection, seven public methods, no serialization. Every method ends in an explicit `self._conn.commit()`.
- **The bare migration.** `job.py:96-100` — `try: ALTER TABLE jobs ADD COLUMN error_category TEXT / except sqlite3.OperationalError: pass`.
- **Row mapping written twice.** `job.py:172-183` (`get_job`) and `job.py:247-258` (`list_recent`) — identical ten-field positional constructions.
- **Column list written three times.** `job.py:81-92` (`CREATE TABLE`), `job.py:133-135` (`INSERT`), and the two `SELECT` lists at `:165-168` and `:241-244`.
- **`prune()`'s two-`COUNT(*)` race.** `job.py:277` and `job.py:292`, straddling two `DELETE`s at `:281` and `:286`.
- **Db path computed twice.** `src/saneless/cli.py:227` and `src/saneless/web/app.py:59`, both `str(Path(settings.output.tmp_dir) / "saneless.db")`. **Deferred — D-18.**

### Established patterns this phase must follow
- **Enum round-tripping.** `state` and `error_category` are stored as `TEXT` and reconstructed through the enum constructor (`JobState(row[3])`, `ErrorCategory(row[5]) if row[5] else None`). D-09 applies the identical shape to `outcome`.
- **`Job.is_active` / `Job.is_busy`** (`job.py:55-64`) read `ACTIVE_STATES` / `BUSY_STATES` from `vocabulary.py`. `fail_active_jobs()` and `list_pending()` should derive their predicates from the same frozensets, never from hand-written state lists — Phase 21's D-04 and D-09.
- **`job.py`'s `__all__`** (`job.py:20`) re-exports `ErrorCategory` and `JobState`. It must keep doing so.
- **Docstring style.** Ruff `D` rules are on; every public module, class and function needs a docstring, `D203`/`D212` ignored.
- **TDD is enabled** (`.planning/config.json` → `workflow.tdd_mode: true`). Tests come first.

### Integration points
- `src/saneless/job.py` — the phase's entire production surface.
- `tests/test_job.py` (162 lines) — gains the concurrency test (D-14), the migration tests (D-04/D-05), the reflective lock-coverage test (D-12), and the prune-equivalence assertions (D-16). `test_jobstore_migration_adds_column` at `:134` is written against the bare `ALTER` and will need rewriting against the ladder.
- **Callers that must keep working unchanged** — nothing in this phase changes their behaviour, but the suite covers them: `src/saneless/worker.py:181,197,223,230,250,254,265`; `src/saneless/web/routes.py:83,85,88,164,189,191,280,300,319`; `src/saneless/web/app.py:58,69,77`; `src/saneless/cli.py:228,230,266`.
- `docs/` — the storage documentation corrected in-phase. Locate the pages that describe job history and persistence.

### Constraints the architecture imposes
- **`vocabulary.py` must stay a leaf.** `job.py` imports from it; never the reverse.
- **`:memory:` is the default and the test path.** Every test constructs `JobStore()` with no path. The ladder, the `autocommit` change and any `PRAGMA` must behave correctly for an in-memory database, and the migration must run without an explicit caller.
- **`sqlite3.Row` is safe here** because `JobStore` owns its connection exclusively — no other module holds a reference.

</code_context>

<specifics>
## Specific Ideas

- **The user chose the strict-scope branch twice, against the recommendation, and both times in the same direction** — `close()` hardening (D-15) and the db-path dedup (D-18) were both pushed to the phases that already own the surrounding change. Read this as a standing preference for this milestone: when a fix could plausibly live in this phase or the next, and the next phase is touching that code anyway, it belongs to the next phase. A planner should not "helpfully" fold either back in.

- **Two decisions exist specifically to make a future omission fail the suite rather than pass it**, and both are direct descendants of Phase 21's D-09: the reflective lock-coverage test (D-12) and the `PRAGMA table_info` assertion in step 2 (D-05). Neither is defensive coding for its own sake — each converts a silent future failure into an immediate loud one. If a planner finds them awkward, the correct response is a better mechanism with the same property, not removal.

- **One recommendation was withdrawn during the discussion after being checked.** The `prune()` question was initially framed as a trade-off between an exact rowcount and a subtle change in keep-semantics. Working the cases showed the two forms are equivalent, because age-expiry is always a `created_at` prefix — so the trade-off was imaginary and the single statement wins outright. The worked cases are in D-16 so nobody re-derives the false trade-off from the requirement text.

- **The "v1.0 database" in success criterion 2 is a database this project's own git history produced at commit `dc9b8af`, not an artifact any user has.** The criterion is still worth satisfying — it is what makes the ladder real — but the planner should know it is testing a fixture, not a field upgrade, and should not spend effort on shapes D-04 ruled out.

</specifics>

<deferred>
## Deferred Ideas

- **Calling `fail_active_jobs()` at startup, before `worker.start()`** — ROBU-05, **Phase 26**. This phase ships the method with no production caller; its tests are its consumer.
- **Writing `outcome` and `warning`** — **Phase 23** (OUTC-01, OUTC-02, OUTC-03). The columns and fields exist here; nothing sets them.
- **Writing and rendering the three page counts, and `owner_token`** — **Phase 30** (APPL-03, APPL-08, APPL-09).
- **Moving the database off `tmp_dir` onto `output.data_dir`** — OUTC-09 / N-39, **Phase 23**.
- **Centralizing the duplicated `tmp_dir / "saneless.db"` computation** (`cli.py:227`, `web/app.py:59`) — **Phase 23**, folded into the OUTC-09 move. D-18.
- **`close()` hardening: a `_closed` flag, a saneless-owned use-after-close error, and the shutdown ordering that makes `Cannot operate on a closed database` reachable** — M-03, **Phase 26**. D-15. Phase 28 (exception translation) may also want a say in the error type.
- **Worker resilience around store failures, `put_nowait` + 429 backpressure, and `def` routes** — the other half of C-07, **Phase 26**.
- **Giving `error_category` a consumer, or removing it** — N-14, **Phase 30** (APPL-04) per Phase 21's D-12.
- **A `pages_back` / duplex front-back split column, if APPL-03 turns out to need one** — resolved by D-11 *in this phase* if the sweep says persistence is required; otherwise Phase 25/30 keeps it in live worker state. This is the one item that must NOT be deferred silently.
- **`PRAGMA application_id`** — considered for proving a database file is saneless's own before migrating it. No current need; revisit only if a real cross-application collision appears.
- **Renaming `JobState.ERROR` to `FAILED` to match the requirement prose** — rejected outright (D-19), not deferred. It would require a data migration of the persisted `state` column and reopens vocabulary Phase 21 just closed.

</deferred>

---

*Phase: 22-Job Store Hardening*
*Context gathered: 2026-09-10*
