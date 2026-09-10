# Phase 22: Job Store Hardening - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-09-10
**Phase:** 22-Job Store Hardening
**Areas discussed:** Migration ladder baseline, The six speculative columns, Lock discipline + proof, prune()/row mapping/db path

**Area selection:** the user selected **all four** offered gray areas.

---

## Migration ladder baseline

### Q1 — How should step 0 tell a fresh DB from a legacy DB when both report `user_version = 0`?

| Option | Description | Selected |
|--------|-------------|----------|
| Probe for the jobs table (Recommended) | No `jobs` table → fresh, create + stamp head. Table present → legacy, enter the ladder at step 1. Cheap, no new state, honest about the one thing that distinguishes them. | ✓ |
| Reconcile via PRAGMA table_info | Ignore shape history; diff the live column set against the target and ADD what's missing. Idempotent, but schema reconciliation rather than the ladder STOR-02 asks for. | |
| Stamp application_id too | Probe-for-table plus `PRAGMA application_id` so a future version can refuse to migrate a foreign file. More machinery for a guarantee nothing needs. | |
| You decide | | |

**User's choice:** Probe for the jobs table → **D-03**

### Q2 — Which legacy shapes must the ladder actually migrate?

Context presented: three schemas exist in git history — S1 `b95f88f` (no `thumbnail`, no `error_category`), S2 `5bd6158` (+`thumbnail`, no migration ever written), S3 `dc9b8af` (+`error_category` via the bare ALTER). All three report `user_version = 0`. Also presented: `git tag` returns zero tags, `release.yml` fires only on `v*`, and Phase 31 still has to prove the release workflow end to end.

| Option | Description | Selected |
|--------|-------------|----------|
| S3 only — nobody else runs it | If nothing was ever published, the only databases in existence are the user's, and they're S3. S1/S2 steps would be dead code — Phase 21 D-06 logic. | ✓ |
| All three — S1, S2 and S3 | Full ladder; three small steps and three fixture tests. N-13 explicitly asks for a test that opens a first-release schema. | |
| S2 and S3, skip S1 | Covers the shape N-13 verified broken without inventing history for a shape that existed only between two dev phases. | |
| You decide | | |

**User's choice:** S3 only → **D-04**
**Notes:** this makes N-13's "first-release schema" test an S3 fixture, not S1. Recorded explicitly in D-04 so a planner doesn't write S1/S2 fixtures.

### Q3 — Who owns table creation once the ladder exists?

| Option | Description | Selected |
|--------|-------------|----------|
| Ladder owns it (Recommended) | Delete `CREATE TABLE IF NOT EXISTS` from `__init__`; step 1 IS the CREATE TABLE. One code path, ladder self-evidently complete. | ✓ |
| Keep CREATE TABLE IF NOT EXISTS | `__init__` creates fresh schemas, ladder handles only legacy. Fewer moving parts, but the schema is spelled twice — how N-13 happened. | |
| You decide | | |

**User's choice:** Ladder owns it → **D-01**

### Q4 — Where does the ladder live, and how are steps expressed?

| Option | Description | Selected |
|--------|-------------|----------|
| In-module tuple of callables (Recommended) | `_MIGRATIONS` in `job.py`, index+1 is the version. Type-checked, no file I/O, schema history readable top-to-bottom. | ✓ |
| SQL files in a migrations/ package dir | Conventional and DDL-native, but adds package-data plumbing and hides schema logic from the type checkers. | |
| Adopt a library | CLAUDE.md prefers external packages — but ~30 lines against one table, and the dependency buys features this project won't use. | |
| You decide | | |

**User's choice:** In-module tuple of callables → **D-02**

### Q5 (follow-up, raised by Claude) — Should step 2 verify the S3 column set before adding the v2 columns?

Raised because "S3 only" means step 2 assumes any pre-existing `jobs` table has all ten columns; if it doesn't, step 2 hands back a database still missing `thumbnail` — the exact silent breakage N-13 verified.

| Option | Description | Selected |
|--------|-------------|----------|
| Assert then fail loudly (Recommended) | Read `PRAGMA table_info`, raise naming the file path and missing columns rather than migrating. ~5 lines. | ✓ |
| No guard — trust S3 | Simplest; if nobody else runs saneless the assertion can never fire. But it surfaces later as a confusing `create_job` crash. | |
| Guard and self-heal | Detect and add missing S3 columns. Smuggles the S1/S2 ladder back in under another name, un-testable for the same reason. | |
| You decide | | |

**User's choice:** Assert then fail loudly → **D-05**

**Gate:** "Next area."

---

## The six speculative columns

Context presented: `owner_token`'s semantics are already fixed by REQUIREMENTS.md § Out of Scope (*"a footgun guard for the flip prompt, not an auth mechanism"*) and APPL-09. A sweep of OUTC/DPLX/APPL found `FALLBACK` is a JobState not a column, the preserved-PDF path goes in `error`, and queue position comes from `list_pending()` — leaving APPL-03's duplex front/back counts as the one unresolved candidate.

### Q1 — Do the six also become `Job` dataclass fields in this phase?

| Option | Description | Selected |
|--------|-------------|----------|
| Columns + fields, no writers (Recommended) | All six on `Job` with defaults, read through the single row mapping; nothing writes them. A column the mapping ignores means Phase 23 edits the mapping anyway. | ✓ |
| DB columns only | Keeps the dataclass fully reachable, but keeps the "one migration" promise while breaking the "one row mapping" promise. | |
| Columns + fields + write paths | Also wire `set_outcome`, `set_page_counts`, `set_owner_token`. Half-satisfies Phase 23/30 criteria in the wrong phase. | |
| You decide | | |

**User's choice:** Columns + fields, no writers → **D-07**

### Q2 — How should the three page-count columns be typed?

| Option | Description | Selected |
|--------|-------------|----------|
| Nullable INTEGER, default NULL (Recommended) | `int \| None = None`. NULL = "never recorded", true of every existing row and every job that dies pre-scan. Phase 30 renders "—" not a false "0 pages". | ✓ |
| NOT NULL DEFAULT 0 | Simpler readers, no `is None` branches — but asserts a measured zero, and `ADD COLUMN NOT NULL DEFAULT 0` backfills historical rows with that lie. | |
| You decide | | |

**User's choice:** Nullable INTEGER → **D-08**

### Q3 — How is `outcome` stored and read back, given `ScanOutcome` lacks `FAILED` until Phase 23?

| Option | Description | Selected |
|--------|-------------|----------|
| TEXT, parsed to ScanOutcome \| None (Recommended) | Mirrors how `state` and `error_category` already round-trip. Unknown value raises on read — consistent with today and Phase 21 D-10. | ✓ |
| TEXT, kept as a raw str | Avoids a downgrade crashing on a newer `FAILED` — but downgrades aren't supported, and it reintroduces the stringly-typed protocol N-38 and Phase 21 removed. | |
| You decide | | |

**User's choice:** TEXT parsed to `ScanOutcome | None` → **D-09**

### Q4 — Should the six-column list be verified sufficient before locking?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — make the planner prove it (Recommended) | CONTEXT.md instructs the planner to re-read OUTC/DPLX/APPL before writing the migration, calling out APPL-03 specifically. Anything missing goes in THIS migration. | ✓ |
| Add a seventh now for duplex counts | Cheap insurance, but guesses a shape Phase 25 hasn't designed. | |
| Lock the six as written | Treat STOR-03 as authoritative; a later phase adds a step 3, which is now cheap and honest. | |
| You decide | | |

**User's choice:** Yes — make the planner prove it → **D-11**

**Gate:** "Next area."

---

## Lock discipline + proof

Context presented: the stdlib docs' `check_same_thread` note (*"write operations may need to be serialized by the user"*), `with conn:` being a no-op when no transaction is open under `LEGACY_TRANSACTION_CONTROL`, and `Connection.autocommit` being available on 3.12+. Framing applied from the user's standing verification-rigor rule: a passing 200-round stress test proves `threading.RLock` works, which is Python's contract; the genuinely uncertain claim is "every public method takes the lock, including the ones added next year."

### Q1 — How is "the lock is taken in every public method" applied and kept true?

| Option | Description | Selected |
|--------|-------------|----------|
| @_locked decorator + reflective test (Recommended) | `inspect`-based test enumerates public callables and asserts each carries the marker. Phase 21 D-09's pattern moved from enum members to methods. | ✓ |
| Inline `with self._lock, self._conn:` | C-07's literal prescription, verified 0/10 failures — but nothing detects the eighth method someone forgets. | |
| Public wrapper / private _impl split | Satisfies STOR-01's "never call other public methods" structurally, at the cost of doubling the method count. | |
| You decide | | |

**User's choice:** `@_locked` decorator + reflective test → **D-12**

### Q2 — What should the 200-round test assert, given "no interleaved-transaction corruption"?

| Option | Description | Selected |
|--------|-------------|----------|
| Zero exceptions + a data invariant (Recommended) | Assert every created job is readable at its last-set state and the row count reconciles. Asserts our serialization produced correct data, not that RLock is reentrant. | ✓ |
| Zero exceptions only | Mirrors the review's own stress script (10/10 → 0/10). Cheapest, honest as a regression test for a bug with a crisp signature. | |
| Add a deliberate no-lock control | Prove the test can fail. Strongest evidence, but pins an implementation detail — a future build that doesn't raise turns the suite red for nothing. | |
| You decide | | |

**User's choice:** Zero exceptions + a data invariant → **D-14**

### Q3 — Which transaction-control mode on Python 3.14?

| Option | Description | Selected |
|--------|-------------|----------|
| Set conn.autocommit = False (Recommended) | PEP 249 behaviour; a step's DDL and its `PRAGMA user_version = N` commit or roll back together. Removes the legacy-mode trap. | ✓ |
| Keep legacy mode + `with self._conn:` | C-07's prescribed fix, minimal change — but `with conn:` is a no-op for the PRAGMA and DDL the ladder runs. | |
| You decide | | |

**User's choice:** `conn.autocommit = False` → **D-13**

### Q4 — Does `JobStore` get a use-after-close defence (M-03)?

| Option | Description | Selected |
|--------|-------------|----------|
| Locked close() + a _closed flag (Recommended) | Clear saneless-owned error after close, a few lines in a phase already rewriting every method. | |
| Locked close(), no flag | Lock only; let sqlite3's own `ProgrammingError` surface. | |
| Leave close() entirely to Phase 26 | Strict scope: STOR-01..05 say nothing about close(). | ✓ |
| You decide | | |

**User's choice:** Leave close() entirely to Phase 26 — **diverges from the recommendation** → **D-15**

### Q5 (follow-up, raised by Claude) — Then is `close()` decorated or exempted from the reflective test?

Raised because the user's Q4 answer left the interaction with Q1's exemption-free reflective test unresolved.

| Option | Description | Selected |
|--------|-------------|----------|
| Decorate it, nothing more | `@_locked` like every other method — no flag, no custom error. Reflective test stays exemption-free. | ✓ |
| Exempt it, documented in the test | `_EXEMPT = {"close"}` pointing at Phase 26. Truest to "leave it entirely" — but an exemption set is the hand-written list Phase 21 D-09 warned about. | |
| You decide | | |

**User's choice:** Decorate it, nothing more → **D-15**

**Gate:** "Next area."

---

## prune(), row mapping, db path

Context presented: two findings worked out rather than assumed. **(1)** The combined `DELETE` was initially going to be offered as a semantic trade-off; working the cases showed it is not — both predicates order by `created_at`, so the expired set is always a prefix of the oldest rows and the union equals the sequential composition (600/200/500 → 200 both ways; 600/50/500 → 100 both ways). **(2)** STOR-05 and roadmap criterion 4 both say "FAILED", but `JobState` has `ERROR` — a planner reading literally could invent a `JobState.FAILED`.

### Q1 — Does "FAILED" in STOR-05 mean the existing `JobState.ERROR`?

| Option | Description | Selected |
|--------|-------------|----------|
| Yes — ERROR, and say so explicitly (Recommended) | Record that "FAILED" throughout STOR-05/OUTC-01/the criteria is prose for `JobState.ERROR`, and this phase adds no member. | ✓ |
| Rename ERROR to FAILED here | Makes code match requirement prose — but the value is persisted TEXT, so it needs a data migration, and Phase 21 just locked this vocabulary. | |
| You decide | | |

**User's choice:** Yes — ERROR, explicitly → **D-19**

### Q2 — How should `prune()` produce its count?

| Option | Description | Selected |
|--------|-------------|----------|
| One combined DELETE + cursor.rowcount (Recommended) | Matches criterion 5 literally, exact count immune to the race that forced -1, and deletes the same rows per the equivalence check. | ✓ |
| Two DELETEs, sum the rowcounts | Predicates stay visually separate and the race is still killed — but it's two statements, and the verifier reads criteria literally. | |
| You decide | | |

**User's choice:** One combined DELETE → **D-16**

### Q3 — How should the single row mapping and column list be expressed?

| Option | Description | Selected |
|--------|-------------|----------|
| _COLUMNS tuple + sqlite3.Row + _row_to_job (Recommended) | Column list once (SELECT + INSERT), name-based access, one conversion helper. Positional indexing over 16 columns is how N-13's thumbnail bug happened. | ✓ |
| _COLUMNS tuple + positional _row_to_job | Same single list and helper, smaller diff — but `row[11]` across 16 columns stays fragile. | |
| sqlite3.Row alone, no _COLUMNS | Shortest, but the INSERT still spells the columns out, so "exactly once" wouldn't be true. | |
| You decide | | |

**User's choice:** `_COLUMNS` + `sqlite3.Row` + `_row_to_job` → **D-17**

### Q4 — Is the duplicated `tmp_dir / "saneless.db"` path (N-17) in or out?

| Option | Description | Selected |
|--------|-------------|----------|
| Centralize now, don't move it (Recommended) | One `default_db_path(settings)` used by both call sites; location unchanged. Four lines, part of STOR-04's cited finding. | |
| Leave it entirely to Phase 23 | STOR-04's requirement text covers only the mapping and column list; the path dedup is N-17 prose. Phase 23 touches both call sites anyway. | ✓ |
| You decide | | |

**User's choice:** Leave it entirely to Phase 23 — **diverges from the recommendation** → **D-18**

**Final gate:** "I'm ready for context."

---

## Claude's Discretion

The user answered every question rather than delegating any, so no decision was handed to Claude wholesale. The following were explicitly left open to the planner in CONTEXT.md:

- `fail_active_jobs()`'s signature — M-03 prescribes `(reason)`, STOR-05 implies a fixed "server restarted"; and whether it sets `error_category`.
- `list_pending()`'s exact predicate and ordering (derive from `ACTIVE_STATES`, not a hand-written list).
- Whether the ladder runs inside `__init__` or an explicit `migrate()` called from it.
- Whether `PRAGMA journal_mode=WAL` survives, given it is inert for the `:memory:` databases every test uses.
- Task and commit breakdown.
- The exact prose of the storage documentation update.

## Deferred Ideas

No scope creep was raised by the user during discussion. The deferrals recorded in CONTEXT.md all arose from scope-boundary questions Claude posed:

- Calling `fail_active_jobs()` at startup — Phase 26 (ROBU-05).
- Writing `outcome` / `warning` — Phase 23; writing and rendering page counts and `owner_token` — Phase 30.
- Moving the database onto `output.data_dir`, and centralizing the duplicated path — Phase 23 (OUTC-09, D-18).
- `close()` hardening and shutdown ordering — Phase 26 (M-03, D-15).
- Worker resilience, 429 backpressure, `def` routes — Phase 26 (C-07's other half).
- Giving `error_category` a consumer or removing it — Phase 30 (N-14).
- `PRAGMA application_id` — no current need.
- A duplex front/back count column — **not deferred**; D-11 obliges the planner to resolve it inside this phase's migration if APPL-03 needs persistence.
- Renaming `JobState.ERROR` to `FAILED` — rejected outright (D-19), not deferred.
