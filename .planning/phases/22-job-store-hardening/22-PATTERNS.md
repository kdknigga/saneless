# Phase 22: Job Store Hardening - Pattern Map

**Mapped:** 2026-09-10
**Files analyzed:** 4 (2 production, 1 test, 2 doc pages — `job.py` counted once)
**Analogs found:** 9 exact/role-match / 12 mappable units — **3 units have NO analog in this tree**

This phase's production surface is one module, so this map is unit-level rather than
file-level: `src/saneless/job.py` is decomposed into the eight things it gains, and each
is matched separately. Three of them (`_locked`, the `_MIGRATIONS` ladder, the
`ThreadPoolExecutor` stress test) have **no precedent anywhere in this repository** — see
§ No Analog Found. For those the planner must treat `22-RESEARCH.md`'s verified prototypes
as the source, not invent a local convention.

---

## File Classification

| New/Modified unit | File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|---|
| `_locked[**P, R]` decorator | `src/saneless/job.py` (new) | utility / cross-cutting wrapper | request-response wrapper | **none** | **no analog** |
| `_MIGRATIONS` ladder + `_migrate` | `src/saneless/job.py` (new) | migration | batch / DDL | **none** (the bare `ALTER` at `job.py:96-100` is the thing being deleted) | **no analog** |
| `_COLUMNS` / `_SELECT_*` module constants | `src/saneless/job.py` (new) | config / constants | — | `src/saneless/config.py:159`; `src/saneless/vocabulary.py:68-99` | role-match |
| `_S3_COLUMNS` frozenset | `src/saneless/job.py` (new) | config / constants | — | `src/saneless/vocabulary.py:83` | exact |
| `_row_to_job` | `src/saneless/job.py` (new) | transform | transform | `src/saneless/job.py:172-183` (the mapping being deduplicated) | exact |
| Six new `Job` fields | `src/saneless/job.py` (modified) | model | — | `src/saneless/job.py:44-53` + `src/saneless/pipeline.py:107-115` (`ScanResult`) | exact |
| `fail_active_jobs()` | `src/saneless/job.py` (new) | service / repository write | CRUD | `src/saneless/job.py:185-212` (`update_state`) | exact |
| `list_pending()` | `src/saneless/job.py` (new) | service / repository read | CRUD | `src/saneless/job.py:229-260` (`list_recent`) | exact |
| `prune()` rewrite | `src/saneless/job.py:262-297` (modified) | service / repository write | CRUD | itself + `22-RESEARCH.md` § Code Examples | exact |
| `StorageError` | `src/saneless/exceptions.py` (modified) | model / vocabulary | — | `src/saneless/exceptions.py:33-34` (`PaperlessError`) | exact |
| Migration tests (S3 / S2 / idempotent / journal_mode) | `tests/test_job.py` (rewrite `:134`) | test | file-I/O | `tests/test_job.py:134-162` (construction idiom) + `tests/test_worker.py:112-127` (file-backed reopen) | exact |
| Reflective `@_locked` coverage test | `tests/test_job.py` (new) | test | — | `tests/test_vocabulary.py:41-54, 103-115` (parametrise-over-the-real-thing) | role-match |
| `ast`-walk no-public-self-call test | `tests/test_job.py` (new) | test | — | **none** — no `ast` or `inspect` import exists in `tests/` | **no analog** |
| 200-round concurrency test | `tests/test_job.py` (new) | test | event-driven / concurrency | **none** — no `RLock`, `Barrier` or `ThreadPoolExecutor` anywhere in the repo | **no analog** |
| Column round-trip tests | `tests/test_job.py` (new) | test | CRUD | `tests/test_job.py:117-132` (`test_jobstore_persists_error_category`) | exact |
| Prune equivalence / shuffled / concurrent tests | `tests/test_job.py` (new + existing) | test | CRUD | `tests/test_job.py:49-98` | exact |
| Storage prose correction | `docs/reference/configuration.md:48-49` | config docs | — | itself (same table, `:43`) | exact |
| Persistence section (optional) | `docs/explanation/architecture.md` | explanation docs | — | `architecture.md:45-51` (§ Worker Thread Model) | role-match |

---

## Pattern Assignments

### `src/saneless/job.py` — module header and imports

**Analog:** `src/saneless/job.py:1-22` (current header) and `src/saneless/vocabulary.py:1-33`

Current header, which the new imports extend:

```python
"""
Job model with state machine and SQLite persistence.
...
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from saneless.vocabulary import ACTIVE_STATES, BUSY_STATES, ErrorCategory, JobState

__all__ = ["ErrorCategory", "Job", "JobState", "JobStore"]

logger = logging.getLogger(__name__)
```

Conventions this establishes, all of which the new code must keep:

- `from __future__ import annotations` first (every module in `src/saneless/` has it except `exceptions.py` — see that section).
- stdlib `import x` block, then `from x import y` block, then first-party `from saneless.…`.
- `__all__` is a **sorted list literal** (ruff `RUF022`); `job.py` re-exports `ErrorCategory` and `JobState` from `vocabulary.py` and **must keep doing so** (Phase 21 D-02).
- `logger = logging.getLogger(__name__)` immediately after `__all__`.

**New imports required** (from `22-RESEARCH.md` § Pattern 2 / § Pitfall 4):
`functools`, `threading`, `typing.Concatenate`, and `from saneless.exceptions import StorageError`.
`ScanOutcome` joins the `saneless.vocabulary` import; whether it also joins `__all__` is the
planner's call (`ErrorCategory`/`JobState` precedent argues yes if `job.py` returns it).
Ruff `TC003` will demand `from collections.abc import Callable` sit inside
`if TYPE_CHECKING:` — the tree already does this in `tests/test_job.py:16-17` and
`tests/test_worker.py:22-28`.

---

### `src/saneless/job.py` — the enum round-trip (D-09)

**Analog:** `src/saneless/job.py` — read side at `:176-178`, write side at `:140-142`.

Read side, which `_row_to_job` must reproduce name-based:

```python
            state=JobState(row[3]),
            error=row[4],
            error_category=ErrorCategory(row[5]) if row[5] else None,
```

Write side, in `create_job`:

```python
                job.state.value,
                job.error,
                job.error_category.value if job.error_category else None,
```

**The exact shape D-09 mandates for `outcome`** — non-optional enum is bare
`Enum(value)`, optional enum is `Enum(value) if value else None`, and the write side is
`.value if x else None`. Applied:

```python
            outcome=ScanOutcome(row["outcome"]) if row["outcome"] else None,
```

Note the `if row[...] else None` guard, not `is not None` — the existing code treats the
empty string and `NULL` identically. Keep that; changing it is a behaviour change this
phase does not want.

The three page counts and the two `str | None` columns are plain passthroughs
(`pages_scanned=row["pages_scanned"]`), matching how `correspondent` and `thumbnail` are
handled at `:180-181`. Non-enum, non-JSON columns get no conversion.

Also note the two non-enum conversions the mapping already performs and must keep:
`tags=json.loads(row[6])` and `created_at=datetime.fromisoformat(row[9])`.

---

### `src/saneless/job.py` — module-level constants (`_COLUMNS`, `_S3_COLUMNS`, `_V2_COLUMNS`)

**Analog A (private tuple of strings):** `src/saneless/config.py:159`

```python
_VALID_SECTIONS = ("scanner", "paperless", "output", "profiles")
```

Single-underscore prefix, module scope, tuple literal, no type annotation when the
inference is obvious. `_COLUMNS` should carry the annotation anyway because
`22-RESEARCH.md` § Pattern 3 spells it `_COLUMNS: tuple[str, ...]` and both checkers were
verified clean on that form.

**Analog B (annotated frozenset constant with an attached docstring):** `src/saneless/vocabulary.py:83-88`

```python
TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.DONE, JobState.ERROR})
"""Job states where the job has reached its final outcome.

Together with ``ACTIVE_STATES`` this partitions ``JobState``: every member is in
exactly one of the two sets.
"""
```

This is the house style for a constant that needs justifying: annotated assignment
followed by a **triple-quoted string literal as a module-level docstring**, using
double-backtick reST for identifiers. D-22 requires each interpolated SQL constant to
carry a comment stating why the interpolation is safe — this docstring form is the
established mechanism, and it reads better than a `#` comment above the assignment.

`_S3_COLUMNS: frozenset[str]` should copy this exactly, and the `_SELECT_JOBS` /
`_INSERT_JOBS` name-prefix constants (the `S608` resolution) should each carry a
docstring saying: every interpolated value is a module-level `tuple[str, ...]` literal
that no user input can reach.

**Derived-constant precedent:** `vocabulary.py:90` — `BUSY_STATES: frozenset[JobState] = ACTIVE_STATES - {JobState.AWAITING_FLIP}`,
with the docstring explaining *"Derived from `ACTIVE_STATES` so the two can never drift apart."*
That is precisely the argument for `_COLUMN_LIST = ", ".join(_COLUMNS)` and
`_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)`.

---

### `src/saneless/job.py` — `Job` dataclass gains six fields

**Analog:** `src/saneless/job.py:25-53` (the dataclass itself) and `src/saneless/pipeline.py:107-115` (`ScanResult`, which five of the six mirror)

```python
@dataclass
class Job:
    """
    A scan job with metadata and state tracking.

    Attributes:
        id: Unique job identifier (UUID).
        ...
        thumbnail: Optional base64-encoded JPEG thumbnail string.

    """

    id: str
    profile: str
    title: str
    state: JobState = JobState.PENDING
    error: str | None = None
    error_category: ErrorCategory | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    tags: list[int] = field(default_factory=list)
    correspondent: int | None = None
    thumbnail: str | None = None
```

Conventions to copy:
- Bare `@dataclass` (no `frozen=`, no `slots=`) — matches `ScanResult` and `PipelineRequest` too.
- Required fields first, defaulted fields after; new columns are all defaulted so they append at the end.
- Mutable defaults use `field(default_factory=...)`.
- **Every field gets a line in the class docstring's `Attributes:` block.** The docstring block is closed by a blank line before the trailing `"""` — reproduce that.
- The six new `Attributes:` lines must not claim a consumer. Phrase them as recorded facts
  (`outcome: How the scan resolved, once a scan has recorded one.`) rather than as
  behaviour, matching D-07 and the ROADMAP constraint.

`ScanResult` field names to mirror **exactly** (`pipeline.py:110-115`):

```python
    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    warning: str | None = None
```

On `Job` these become `ScanOutcome | None = None`, `int | None = None` ×3, `str | None = None`,
plus `owner_token: str | None = None`.

**Precedent for a deliberately-unwired member's docstring** — `vocabulary.py:139-151`
(`progress_label`'s *"They have no production caller in this phase"*) and
`vocabulary.py:63-65` (the `ScanOutcome.FALLBACK` comment). Copy that register when
documenting the six unwritten columns.

---

### `src/saneless/job.py` — `fail_active_jobs()` and `list_pending()`

**Analog (write):** `src/saneless/job.py:185-212` (`update_state`)

```python
    def update_state(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """
        Update the state (and optionally error) of a job.

        Args:
            job_id: The UUID string of the job.
            state: New job state.
            error: Optional error message (typically set with ERROR state).
            error_category: Optional error category for programmatic handling.

        """
        self._conn.execute(
            "UPDATE jobs SET state = ?, error = ?, error_category = ? WHERE id = ?",
            (
                state.value,
                error,
                error_category.value if error_category else None,
            job_id,
            ),
        )
        self._conn.commit()
        logger.debug("Job %s -> %s", job_id, state.value)
```

**Analog (read + list comprehension):** `src/saneless/job.py:229-260` (`list_recent`)

```python
    def list_recent(self, limit: int = 50) -> list[Job]:
        """
        Fetch the most recent jobs ordered newest-first.

        Args:
            limit: Maximum number of jobs to return.

        Returns:
            List of Job instances ordered by creation time descending.

        """
        rows = self._conn.execute(
            "SELECT ... FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [ Job(...) for row in rows ]
```

Docstring conventions both establish, enforced by ruff `D`:
- Summary line on the line **after** the opening `"""` (`D212` is ignored, `D213`-ish style).
- `Args:` block with one line per parameter.
- `Returns:` block only when the method returns something.
- A **blank line before the closing `"""`** (this is consistent across every docstring in the tree).
- `logger.debug` with `%s` lazy formatting, never f-strings (ruff `G`).

`list_pending()` becomes `list_recent` with the ordering flipped to `ASC` and a
`WHERE state = ?` predicate; the `_SELECT_ALL` constant plus `_row_to_job` collapses the
body to three lines. `fail_active_jobs()` becomes `update_state` with an `IN` predicate
built from `tuple(sorted(s.value for s in ACTIVE_STATES))` (D-26) and returning
`cursor.rowcount`.

**Predicate derivation precedent:** `src/saneless/job.py:55-63`

```python
    @property
    def is_active(self) -> bool:
        """Whether this job is still in flight (not DONE or ERROR)."""
        return self.state in ACTIVE_STATES
```

`ACTIVE_STATES` and `BUSY_STATES` are already imported at `job.py:18`. Adding the SQL
predicate on top of the same frozensets is a one-line import change, not a new dependency.

---

### `src/saneless/job.py` — raising `StorageError` from the D-05 guard

**Analog:** `src/saneless/config.py:221-229`

```python
    tmp = Path(settings.output.tmp_dir)
    if tmp.exists() and not os.access(tmp, os.W_OK):
        msg = f"tmp_dir is not writable: {tmp}"
        raise ConfigError(msg)
```

and the multi-line form, `src/saneless/config.py:196-201`:

```python
        msg = (
            f"Unknown config section {names}. "
            f"Valid top-level sections: {valid}. {hint_text}"
        )
        raise ConfigError(msg) from exc
```

This is the ruff `EM`-compliant idiom used at all eight raise sites in the tree: bind
`msg` first, then `raise Type(msg)`. Never a literal inside `raise`. Use `from exc` when
re-raising inside an `except`. The message names the offending value — the D-05 guard's
message must name the database path and the sorted missing column names, matching
`f"tmp_dir is not writable: {tmp}"`'s "what, then which".

---

### `src/saneless/exceptions.py` — adding `StorageError`

**Analog:** the file itself, `src/saneless/exceptions.py:1-34` (34 lines, read in full)

```python
"""
Custom exception hierarchy for saneless.

All saneless-specific exceptions inherit from SanelessError,
allowing callers to catch broad or narrow exception types.
"""

__all__ = [
    "ConfigError",
    "FeederEmptyError",
    "PaperlessError",
    "SanelessError",
    "ScanError",
]


class SanelessError(Exception):
    """Base exception for all saneless errors."""


class ConfigError(SanelessError):
    """Configuration loading or validation failure."""


class ScanError(SanelessError):
    """Scanner operation failure."""


class FeederEmptyError(ScanError):
    """ADF feeder is empty -- no paper detected."""


class PaperlessError(SanelessError):
    """Paperless-ngx API operation failure."""
```

Conventions to copy exactly so `StorageError` looks native:

| Convention | Detail |
|---|---|
| **No `from __future__ import annotations`** | This is the *only* module in `src/saneless/` without it. Do not add it — the file has no annotations at all. |
| `__all__` placement | Immediately after the module docstring, **before** any class. Multi-line list literal, one name per line, trailing comma, ASCII-sorted (ruff `RUF022`). `"StorageError"` sorts **last** (after `"ScanError"`). |
| Class body | A single one-line docstring and **nothing else** — no `pass`, no `...`, no `__init__`. |
| Docstring form | One sentence, no trailing period style variance: every existing one ends with a period. Noun-phrase, names the failure: *"Configuration loading or validation failure."* |
| Blank lines | Two blank lines between classes; two between `__all__` and the first class. |
| Dash style | `--` (two ASCII hyphens), never an em dash — see `FeederEmptyError`'s docstring. This holds across the whole tree. |
| Ordering | Base class first, then subclasses in the order they were introduced, with a narrower subclass directly after its parent (`FeederEmptyError` after `ScanError`). `StorageError(SanelessError)` therefore appends at the end, after `PaperlessError`. |

The concrete addition:

```python
class StorageError(SanelessError):
    """Job database schema or persistence failure."""
```

---

### `tests/test_job.py` — module header

**Analog:** `tests/test_job.py:1-17` (current header)

```python
"""
JobStore list, prune, and error category tests.

Covers requirements: UI-05, UI-06, PKG-01.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from saneless.job import ErrorCategory, Job, JobState, JobStore

if TYPE_CHECKING:
    from pathlib import Path
```

Three things this already gives the planner for free:

1. **`sqlite3` is already imported** — the raw-connection schema fixtures need no new import.
2. **`Path` is already under `TYPE_CHECKING`** — the `tmp_path: Path` annotation works today.
   (Contrary to the scope note: `test_jobstore_migration_adds_column` at `:134` **does**
   take `tmp_path` and does build a file-backed database. The file is not `:memory:`-only;
   it is one test away from being so. See § Findings.)
3. **`Covers requirements:` line in the module docstring** — this is the house convention
   (`tests/test_vocabulary.py:4` reads `Covers requirements: CTR-01, CTR-02, CTR-05.`).
   It must be updated to add `STOR-01, STOR-02, STOR-03, STOR-04, STOR-05`.

Test-file conventions across the suite:
- `from __future__ import annotations` first.
- Module-level `def test_*` functions **and** `class Test*:` groupings coexist in the same
  file (`test_job.py:20-98` are functions, `:101` is `class TestErrorCategory`). Class
  docstring is a one-liner: `"""ErrorCategory enum and JobStore integration tests."""`.
- Every test has a one-line docstring ending in the requirement ID in parentheses:
  `"""List recent jobs returns entries in reverse chronological order (UI-05)."""`
- Return annotation `-> None` on every test (ruff `ANN`).
- Store lifecycle is **always** `store = JobStore()` / `try: … finally: store.close()`.

---

### `tests/test_job.py` — file-backed schema fixture (S3 / S2 / journal_mode)

**Analog A (raw-`sqlite3` legacy schema construction):** `tests/test_job.py:134-149` — the test being rewritten. Its *construction idiom* is correct and should be kept:

```python
    def test_jobstore_migration_adds_column(self, tmp_path: Path) -> None:
        """Opening a pre-existing DB without error_category column succeeds."""
        db_path = str(tmp_path / "migrate.db")
        # Create old-schema DB
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE jobs (
            id TEXT PRIMARY KEY, profile TEXT NOT NULL, title TEXT NOT NULL,
            state TEXT NOT NULL, error TEXT, tags TEXT NOT NULL,
            correspondent INTEGER, thumbnail TEXT, created_at TEXT NOT NULL
        )"""
        )
        conn.commit()
        conn.close()
        # Open with new JobStore -- should add error_category column
        store = JobStore(db_path=db_path)
```

**This exact body is the S2 fixture** (nine columns, `thumbnail` present, `error_category`
absent). Under D-05 it now raises `StorageError`, so per D-28 it is rewritten — but the
S2 *shape* to feed the guard test is already written here verbatim. The S3 fixture is the
same block plus `error_category TEXT` after `error TEXT` (the `dc9b8af` shape, matching
`job.py:81-92`'s current `CREATE TABLE` minus nothing).

Note `db_path = str(tmp_path / "migrate.db")` — **`str()`, not `Path`**; `JobStore.__init__`
takes `db_path: str`. Every call site in the tree does this.

**Analog B (file-backed `JobStore` reopen):** `tests/test_worker.py:112-127`

```python
    def test_job_store_sqlite_persistence(self, tmp_path: Path) -> None:
        """Jobs survive JobStore close/reopen cycle."""
        db_path = str(tmp_path / "jobs.db")

        store = JobStore(db_path=db_path)
        job = store.create_job("default", "Persistent Doc")
        job_id = job.id
        store.close()

        store2 = JobStore(db_path=db_path)
        try:
            fetched = store2.get_job(job_id)
            assert fetched is not None
            assert fetched.title == "Persistent Doc"
        finally:
            store2.close()
```

This is the exact shape for the **idempotent-reopen** migration test (D-25 / VALIDATION
row `migration_idempotent`): open, close, reopen, assert. It is also the shape the
**journal_mode** test (D-21) needs — construct `JobStore(str(tmp_path / "x.db"))` and read
`PRAGMA journal_mode` back.

**Analog C (helper method inside a test class):** `tests/test_cli.py:390-398`

```python
    def _populate_store(self, db_path: str, count: int = 2) -> None:
        """Populate a JobStore at db_path with test jobs."""
        store = JobStore(db_path=db_path)
        for i in range(count):
            store.create_job(
                profile="default" if i % 2 == 0 else "photo",
                title=f"Test Document {i + 1}",
            )
        store.close()
```

Underscore-prefixed helper as a **method on the test class**, with a docstring, typed
params, `-> None`. The Wave 0 S3/S2 schema builders should follow this (or the
module-level `def _get(store, job_id) -> Job` form at `tests/test_worker.py:31-35`, which
is the module-level variant of the same convention).

---

### `tests/test_job.py` — the `StorageError` guard test

**Analog:** `tests/test_config.py:207-215` and `tests/test_paperless.py:191`

```python
        with pytest.raises(ConfigError, match=r"profiles\.default"):
            ...
        with pytest.raises(ConfigError, match="bogus"):
            ...
        with pytest.raises(PaperlessError, match="rejected"):
            ...
```

The house idiom is `pytest.raises(SanelessSubclass, match="<a substring of the message>")`,
raw-string when the pattern contains a regex metacharacter. For the D-05 guard the match
should be the missing column name (`match="error_category"`), which is what D-28 requires
the message to name. `pytest` is **not** currently imported in `tests/test_job.py` — it
will need `import pytest` in the third-party block (see `tests/test_vocabulary.py:12`).

---

### `tests/test_job.py` — the reflective lock-coverage test

**Analog:** `tests/test_vocabulary.py:41-50` and `:103-115`

```python
    def test_job_state_has_exactly_seven_members(self) -> None:
        """
        JobState declares exactly seven lifecycle members (CTR-01).

        A count guard, not a name list: adding a member should fail the
        parametrised completeness tests below -- which force a label and a
        classification decision -- rather than a hand-written roster that only
        records what the enum happened to contain when it was written.
        """
        assert len(list(JobState)) == 7

    @pytest.mark.parametrize("state", list(JobState))
    def test_every_state_is_active_xor_terminal(self, state: JobState) -> None:
        """Each JobState is either active or terminal, never both (CTR-01)."""
        assert (state in ACTIVE_STATES) ^ (state in TERMINAL_STATES)
```

**This is Phase 21's D-09 pattern in its only existing incarnation, and it is the register
the D-12 test must adopt**: enumerate the real thing (`list(JobState)` → here,
`inspect.getmembers(JobStore, inspect.isfunction)`), never a hand-written roster, and
put the *rationale* in a multi-line docstring so the next reader does not "simplify" it
into a list. The D-20 note about the test being blind to a public `@property` belongs in
that same docstring block — the precedent for documenting a known blind spot inline is
`vocabulary.py:145-151`.

`@pytest.mark.parametrize` over the enumerated members is the tree's idiom; the
`22-RESEARCH.md` § Pattern 4 form (one test, list-comprehension, `assert unlocked == []`)
is a legitimate variant and gives a better failure message. Either matches the spirit;
the parametrised form matches the letter of the existing file.

**Caveat:** `inspect` and `ast` are imported **nowhere in `tests/`** (verified: zero
matches). There is no local precedent for a source-introspecting test. See § No Analog.

---

### `tests/test_job.py` — column round-trip tests

**Analog:** `tests/test_job.py:117-132`

```python
    def test_jobstore_persists_error_category(self) -> None:
        """JobStore persists and retrieves error_category from SQLite."""
        store = JobStore()
        try:
            job = store.create_job("default", "Cat Test")
            store.update_state(
                job.id,
                JobState.ERROR,
                error="boom",
                error_category=ErrorCategory.SCANNER,
            )
            fetched = store.get_job(job.id)
            assert fetched is not None
            assert fetched.error_category == ErrorCategory.SCANNER
        finally:
            store.close()
```

Note `assert fetched is not None` before the field assertion — this is how the suite
narrows `Job | None`, and `tests/test_worker.py:31-35` factors it into a `_get()` helper.
Either is acceptable; `test_job.py` uses the inline form.

The six new columns have **no writer** this phase (D-07), so the round-trip test cannot go
through a public setter. It must write via the raw connection (the `store._conn.execute`
idiom already used at `tests/test_job.py:56-60`) or via a directly-constructed row, then
assert `get_job()` returns the right value **and the right Python type** — the type
assertion is load-bearing because `sqlite3.Row.__getitem__` returns `Any` and neither
checker can catch a wrong-typed column (`22-RESEARCH.md` § Mechanic 6).

---

### `tests/test_job.py` — prune tests (must pass unchanged)

**Analog:** `tests/test_job.py:49-98`, verbatim. D-16 makes these three the *evidence* for
the equivalence argument, so they are read-only for this phase.

The one line the planner must verify still works under `conn.autocommit = False`
(`tests/test_job.py:56-60`):

```python
        store._conn.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (old_date, old_job.id),
        )
        store._conn.commit()
```

This reaches into the private connection and commits outside the lock. It should still
work (an explicit `commit()` on an always-open transaction), but "the existing prune tests
pass unchanged" is a D-16 obligation and this is the only line in them that touches
internals. Worth an explicit task-level check.

---

### `docs/reference/configuration.md` — the one wrong sentence

**Analog:** the same table, `docs/reference/configuration.md:43-49`

```markdown
| `tmp_dir` | string | `"/tmp/saneless"` | Temporary directory for scan files and SQLite database |
...
| `history_retention_days` | int | `7` | Days to keep completed job history |
| `history_max_rows` | int | `500` | Maximum job history entries in SQLite |
```

D-31 names `:48-49` as the sentence that is actually wrong today: `prune()` deletes by
`created_at` regardless of terminal state, so it does not keep *completed* history — it
keeps *all* history. The correction is one cell, in the same
`| field | type | default | description |` shape, sentence-case, no trailing period
(consistent across every row in the file).

Related sentences the planner may want to touch, all confirmed present:
- `docs/reference/docker.md:30` — `| /tmp/saneless | Scan temp files and SQLite database | No (ephemeral OK) |`
- `docs/reference/cli-commands.md:85` — *"List recent scan job history from the SQLite database."*
- `docs/getting-started/first-web-ui-scan.md:46-48` — § Check job history
- `docs/explanation/architecture.md:49` — the single persistence sentence

---

### `docs/explanation/architecture.md` — the structural gap

**Analog:** `docs/explanation/architecture.md:45-51` (§ Worker Thread Model) and `:53-62` (§ Configuration Layer)

```markdown
## Worker Thread Model

The web server runs a background worker thread that processes one scan job at a time. Jobs are submitted to a `queue.Queue` and executed sequentially -- scanning is inherently serial because a physical scanner can only handle one job at a time.

The worker communicates progress back to the web layer via state transitions on the `Job` object stored in SQLite. The UI polls the job status endpoint, and HTMX updates the status indicator when the state changes. This design keeps the web layer fully responsive during long-running scans.
```

D-31 confirmed: **there is no § Job Storage / § Persistence section**, and `architecture.md:49`
is the whole of what the page says about SQLite. The top-level `##` sections are
Scan Pipeline / Worker Thread Model / Configuration Layer / Web Layer — a persistence
section would slot between Worker Thread Model and Configuration Layer.

House style for this page, if the planner adds one:
- `##` for a subsystem, `###` for a component inside it.
- Prose paragraphs, no bullet lists except where enumerating steps.
- ` -- ` (space, two hyphens, space) for a parenthetical dash; **no em dashes anywhere in `docs/`**.
- Backticked identifiers (`` `run_pipeline()` ``, `` `PipelineEvent` ``, `` `queue.Queue` ``).
- Explains *why*, not *what* (`:5` — *"This page explains how saneless is structured and why it works the way it does."*).
- Cross-links as relative markdown paths (`:28` — `[Set Up ADF Duplex Scanning](../how-to/set-up-adf-duplex.md)`).

**Constraint:** whatever prose lands must not describe any of the six new columns as having
a consumer (CONTEXT.md § Claude's Discretion, last bullet).

---

## Shared Patterns

### Docstrings (ruff `D`, `D203`/`D212` ignored)

**Source:** `src/saneless/vocabulary.py:102-118`, `src/saneless/job.py:110-123`
**Apply to:** every module, class, function and method added by this phase

```python
def state_label(state: JobState) -> str:
    """
    Return the short human label for a job state.

    These are the labels the history table has always rendered; they are part
    of the user-visible surface and must not be reworded casually.

    Args:
        state: The job state to label.

    Returns:
        The user-facing label, e.g. ``"Waiting for flip"``.

    Raises:
        AssertionError: If the value is not a JobState member.

    """
```

The exact shape: opening `"""` alone on its line, summary sentence on the next,
blank line, optional rationale prose, then `Args:` / `Returns:` / `Raises:` blocks in that
order, then **a blank line before the closing `"""`**. Single-line docstrings collapse to
`"""One sentence."""` (see `job.py:57`). `Raises:` is used and must be present on the
D-05 guard and on `__init__` if it can raise `StorageError`.

### Typing

**Source:** `src/saneless/job.py:9`, `src/saneless/pipeline.py`, `tests/test_worker.py:22-28`
**Apply to:** all new code

- `from __future__ import annotations` in every file except `exceptions.py`.
- PEP 604 unions (`str | None`), never `Optional[...]`.
- Type-checking-only imports go inside `if TYPE_CHECKING:` (ruff `TC003` will require this
  for `collections.abc.Callable` in `job.py` and for `pathlib.Path` in tests).
- **Zero suppressions.** The tree has 7 `# noqa` today (`config.py:124,125`,
  `scanner/__init__.py:23`, `scanner/sane_backend.py:48,50`, `test_cli.py:553`,
  `test_web.py:397`) and SWP-08 is committed to removing them. Adding a suppression here
  would move that number the wrong way.
- Ruff `UP047` forces PEP 695 `def _locked[**P, R]` over `ParamSpec` objects; ruff `B010`
  forces `setattr(wrapper, _LOCKED_MARKER, True)` over a literal attribute name. Both
  verified in `22-RESEARCH.md` § Mechanic 2.

### Logging

**Source:** `src/saneless/job.py:150, 212, 296`
**Apply to:** any new log statement

```python
        logger.debug("Created job %s: %s", job.id, job.title)
        logger.debug("Job %s -> %s", job_id, state.value)
        logger.debug("Pruned %d old jobs", deleted)
```

`%s`/`%d` lazy interpolation, never f-strings (ruff `G004`). `logger` is the
module-level `logging.getLogger(__name__)`.

### Prose punctuation

**Source:** `src/saneless/exceptions.py:30`, `src/saneless/vocabulary.py:2`, `docs/explanation/architecture.md:3`
**Apply to:** every docstring, comment and doc sentence added

The entire tree uses ` -- ` (two ASCII hyphens) where an em dash would go, and `` `` ``
double backticks for reST identifiers inside module docstrings. There is not one em dash
in `src/` or `docs/`. Do not introduce the first.

---

## No Analog Found

Three units have **no precedent anywhere in this repository**. For these the planner should
lift the verified prototypes from `22-RESEARCH.md` directly rather than looking for a local
convention that does not exist.

| Unit | Role | Data Flow | Evidence of absence | Use instead |
|---|---|---|---|---|
| `_locked[**P, R]` decorator | utility / wrapper | request-response | Zero matches in `src/` for `functools.wraps`, `ParamSpec`, `Concatenate`, `@wraps`. Every decorator in the tree is third-party: `@click.*` (`cli.py`, 23 sites), `@field_validator` (`config.py:146`), `@router.*` (`routes.py`, 10 sites), `@contextlib.asynccontextmanager` (`app.py:64`), plus stdlib `@dataclass`/`@property`. **This project has never written a decorator.** | `22-RESEARCH.md` § Pattern 2 — verified clean under ruff + `ty` + `pyrefly` |
| `_MIGRATIONS` ladder / `_migrate` | migration | batch / DDL | The only migration code in the tree is the bare `try: ALTER … except sqlite3.OperationalError: pass` at `job.py:96-100`, which this phase deletes. No `PRAGMA user_version` anywhere. No `alembic`, no `.sql` files, no `migrations/` directory. | `22-RESEARCH.md` § Pattern 1 + § Mechanic 3 — verified against five database shapes |
| 200-round concurrency test | test | concurrency | Zero matches in `src/` **and** `tests/` for `RLock`, `threading.Lock`, `Barrier`, `ThreadPoolExecutor`, `concurrent.futures`. The closest existing construct is `threading.Event` (`test_pipeline.py:702,734-735`, `test_scanner.py:897`, `worker.py:62-64`) used for flip/abort signalling — a different problem. | `22-RESEARCH.md` § Mechanic 4 + § Code Examples — measured 0.28 s, 0 warnings |
| `ast`-walk no-public-self-call test (D-24) | test | — | Zero `import ast` and zero `import inspect` in `tests/`. No source-introspecting test exists. | `22-RESEARCH.md` § Pitfall 3; nearest *spiritual* analog is `tests/test_vocabulary.py`'s enumerate-the-real-thing register |

---

## Findings (not file-map items)

Raised per the scope note's instruction to report rather than fold in.

1. **`tests/test_job.py` is not `:memory:`-only, and neither is the suite's file-backed
   coverage.** The scope note and `22-RESEARCH.md` § Pitfall 1 both state that every test
   constructs `JobStore()` with the default. In fact `tests/test_job.py:134` already takes
   `tmp_path` and builds a file-backed store, and **five further file-backed `JobStore`
   constructions exist outside the file**: `tests/test_cli.py:392, 411, 522, 812` and
   `tests/test_worker.py:116`. Consequence for the planner: the WAL-after-autocommit
   ordering error would *not* pass 332/332 — it would fail those five tests with
   `OperationalError` from an unrelated file, which is a confusing signature rather than an
   invisible one. **D-21's dedicated `journal_mode` test is still right and still needed**
   (it names the failure), but the plan should not repeat the "passes the entire suite,
   crashes in production" claim as fact.

2. **`pyproject.toml` already contains two `src/` per-file-ignores.** D-22 rejects a
   `per-file-ignores` carve-out partly on the grounds that it "would be the tree's first".
   It would not: `[tool.ruff.lint.per-file-ignores]` already carries
   `"src/saneless/__init__.py" = ["PLC0415"]` and `"src/saneless/config.py" = ["S104"]`.
   D-22's resolution (module-level name-prefixed constants) is unaffected and remains
   locked — but the plan should not restate the "tree's first" justification.

3. **Suppression count is 7, not 8.** Verified: `config.py:124,125`,
   `scanner/__init__.py:23`, `scanner/sane_backend.py:48,50`, `test_cli.py:553`,
   `test_web.py:397`. Cosmetic, but SWP-08's scope is quoted in `22-RESEARCH.md`.

4. **`PdfError` does not exist.** `22-RESEARCH.md` § Mechanic 3 and D-23 both quote EXC-02's
   CLI catch set as `ConfigError, ScanError, PaperlessError, PdfError`.
   `src/saneless/exceptions.py` defines only `SanelessError`, `ConfigError`, `ScanError`,
   `FeederEmptyError`, `PaperlessError`. `StorageError` will be the **sixth** type in the
   hierarchy, not the fifth, and Phase 28 will have to reconcile a name that has not been
   written yet. Not this phase's problem; flagged so the plan does not assert otherwise.

5. **No caller constructs `Job` positionally.** Verified across `src/` and `tests/`: the
   only `Job(...)` sites are `job.py:124, 172, 247` (all keyword) and
   `test_job.py:114`, `test_vocabulary.py:340, 345, 351`, `test_worker.py:144` (all
   keyword, all using at most `id`/`profile`/`title`/`state`). Appending six defaulted
   fields breaks nothing. **No caller would break** — the four deferred modules
   (`worker.py`, `web/app.py`, `web/routes.py`, `cli.py`) call only existing `JobStore`
   methods with unchanged signatures.

6. **`tests/test_job.py:27, 79` use `time.sleep(0.01)`** inside two of the three prune
   tests that D-16 requires to pass unchanged. `22-RESEARCH.md` notes TEST-02/Phase 32 ban
   `time.sleep` in tests. These are pre-existing and out of scope here, but a planner
   adding a "no sleeps" gate to this phase would break its own D-16 obligation. Leave them.

---

## Metadata

**Analog search scope:** `src/saneless/` (all 14 modules + `web/` + `scanner/`),
`tests/` (all 19 files), `docs/` (all 19 markdown pages), `pyproject.toml`
**Files read in full:** `src/saneless/job.py`, `src/saneless/exceptions.py`,
`src/saneless/vocabulary.py`, `src/saneless/web/cache.py`, `tests/test_job.py`,
`tests/conftest.py`, `docs/explanation/architecture.md`
**Files read in part:** `src/saneless/pipeline.py`, `src/saneless/cli.py`,
`src/saneless/web/app.py`, `tests/test_vocabulary.py`, `tests/test_worker.py`,
`tests/test_cli.py`, `docs/reference/configuration.md`, `pyproject.toml`
**Pattern extraction date:** 2026-09-10
