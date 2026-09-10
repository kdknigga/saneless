---
phase: 21-vocabulary-and-contracts
plan: 01
subsystem: domain-vocabulary
tags: [enums, exhaustiveness, assert_never, refactor, leaf-module]
requires: []
provides:
  - saneless.vocabulary.JobState
  - saneless.vocabulary.ErrorCategory
  - saneless.vocabulary.ScanOutcome
  - saneless.vocabulary.ACTIVE_STATES
  - saneless.vocabulary.TERMINAL_STATES
  - saneless.vocabulary.BUSY_STATES
  - saneless.vocabulary.state_label
  - saneless.vocabulary.progress_label
  - saneless.vocabulary.error_message
  - saneless.vocabulary.classify_error
  - saneless.job.Job.is_active
  - saneless.job.Job.is_busy
affects:
  - src/saneless/job.py
tech-stack:
  added: []
  patterns:
    - "match over an enum with bare member patterns, assign-in-arm, one trailing return, and case _: assert_never(x)"
    - "frozenset classification constants with a derived subset, documented by an attached string literal"
    - "runtime re-export import kept alive by an existing __all__ entry (no # noqa, no import X as X)"
key-files:
  created:
    - src/saneless/vocabulary.py
    - tests/test_vocabulary.py
  modified:
    - src/saneless/job.py
decisions:
  - "BUSY_STATES is written without a type annotation so the derivation `ACTIVE_STATES - {JobState.AWAITING_FLIP}` is the whole right-hand side; ACTIVE_STATES and TERMINAL_STATES keep explicit frozenset[JobState] annotations because they are hand-written sources of truth"
  - "The @dataclass decorator on Job was left bare -- no frozen=, slots=, kw_only= or eq= -- because no dataclass in this codebase uses any of them and Job.state is mutated in place by tests and callers"
  - "classify_error stays an isinstance chain, not a match, because it dispatches on exception type; worker._categorize_error is left in place for a later plan to collapse"
requirements: [CTR-01, CTR-05]
metrics:
  duration: 16m
  completed: 2026-09-10
---

# Phase 21 Plan 01: Vocabulary and Contracts Summary

Created `saneless.vocabulary` as the leaf module owning `JobState`, `ErrorCategory`,
`ScanOutcome`, the three state classifications, and four total lookup functions whose
exhaustiveness both `ty` and `pyrefly` enforce; `job.py` now defines no enum but still
exports both, and `Job` answers `is_active` / `is_busy` from the derived sets.

## What Shipped

**`src/saneless/vocabulary.py`** — leaf module, imports only `enum`, `typing`, and
`saneless.exceptions`.

- `JobState` (7 members), `ErrorCategory` (5), `ScanOutcome` (SUCCESS, FALLBACK) — all
  copied verbatim in style from `job.py`, values identical to names, lifecycle order.
- `ACTIVE_STATES` / `TERMINAL_STATES` partition `JobState`; `BUSY_STATES` is derived.
- `state_label`, `progress_label`, `error_message` — `match` + bare member patterns +
  assign-in-arm + one trailing `return` + `case _: assert_never(x)`.
- `classify_error` — moved from `worker.py:130-149`, `self` dropped, check order preserved.

**`tests/test_vocabulary.py`** — 90 tests, 4 `@pytest.mark.parametrize` sites, all
parametrised over `list(JobState)` / `list(ErrorCategory)` / `list(ScanOutcome)`, never
over a hand-written name list.

**`src/saneless/job.py`** — enums deleted, `from enum import StrEnum` removed, single
first-party import line added, `__all__` at line 19 untouched, `Job.is_active` /
`Job.is_busy` added between the field block and `class JobStore`.

## Exact User-Facing Strings Shipped

`state_label` (verified byte-identical to `web/app.py` `_STATE_LABELS` by a script that
parses that dict out of the live source and compares all seven entries):

| JobState | Label |
|---|---|
| PENDING | `Pending` |
| SCANNING | `Scanning` |
| AWAITING_FLIP | `Waiting for flip` |
| ASSEMBLING | `Assembling` |
| UPLOADING | `Uploading` |
| DONE | `Complete` |
| ERROR | `Failed` |

`progress_label` (the five in-flight strings verified byte-identical against
`web/templates/partials/status.html` and `cli.py` `_event_labels`, including the literal
three-ASCII-period ellipsis):

| JobState | Prose |
|---|---|
| PENDING | `Starting scan...` |
| SCANNING | `Scanning...` |
| AWAITING_FLIP | `Awaiting flip...` |
| ASSEMBLING | `Assembling PDF...` |
| UPLOADING | `Uploading to paperless-ngx...` |
| DONE | `Complete` (totality only, no production caller this phase) |
| ERROR | `Failed` (totality only, no production caller this phase) |

`error_message` — five developer-authored sentences, no exception text interpolated, not
wired to any template, route, or CLI path in this phase (D-12).

## Exhaustiveness Spot-Check Result

Deleted the `case JobState.ERROR: label = "Failed"` arm from `state_label`, ran both
checkers, restored the arm.

- `uv run ty check` → exit 1:
  `error[type-assertion-failure]: Argument does not have asserted type 'Never' ... Inferred type of argument is 'Literal[JobState.ERROR]'` at `vocabulary.py:132:13`
- `uv run pyrefly check` → exit 1:
  `ERROR Argument 'Literal[JobState.ERROR]' is not assignable to parameter 'arg' with type 'Never' in function 'typing.assert_never' [bad-argument-type]` at `vocabulary.py:132:26`

Both name the dropped member. After restoring the arm both exit 0. The enforcement is real.

## `typing.cast` in the D-10 Negative Test

`bad = cast("JobState", "UNKNOWN")` and `bad = cast("ErrorCategory", "UNRECOGNISED")` were
accepted by **both** checkers with no suppression comment:

- `uv run ty check` → `All checks passed!`
- `uv run pyrefly check tests/test_vocabulary.py` → `0 errors`

`pytest.raises(AssertionError)` is the assertion, not `ValueError` — `typing.assert_never`
raises `AssertionError` from a real `raise` statement, so `python -O` does not strip it.
The test docstring records why raising is safe (the `JobState(row[3])` guard at
`job.py:187` / `:262`) and what the failure mode would be if it were not (Jinja render
raising → bare HTTP 500 → htmx 2 does not swap non-2xx → status area freezes silently
while the 1-second poll keeps firing).

## `frozen=` / `slots=` Decision Context

The `@dataclass` decorator on `Job` was left exactly as it was — bare. Reasons:

1. No dataclass anywhere in this codebase uses `frozen=`, `slots=`, `kw_only=`, or `eq=`.
2. `Job.state` is reassigned in place by callers and tests (`j.state = JobState.DONE` is
   in this plan's own acceptance criteria), so `frozen=True` would be a breaking change.
3. `@property` on a dataclass is not a field: `is_active` / `is_busy` do not appear in
   `__init__`, `__eq__`, or `dataclasses.fields(Job)`. A test asserts that last point
   directly, so the decorator needed no change at all.

`slots=True` would additionally conflict with `@property` on a dataclass without care and
buys nothing for an object created a handful of times per scan.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] pyrefly project mode cannot see a worktree nested under a dot-directory**

- **Found during:** Task 1, at the first commit
- **Issue:** This plan executes in a git worktree at
  `.claude/worktrees/agent-a236b096ca6eae625/`. The repository `.gitignore` contains
  `.claude/worktrees/`, and pyrefly's project-mode include resolution consequently skipped
  every file: `No Python files matched patterns ...**/*.py*`. `uv run pyrefly check`
  exited 1, which failed the `pyrefly-checker` prek hook on every commit — for a reason
  with nothing to do with the code. `ty`, `ruff`, and `pytest` were all unaffected.
- **Fix:** Added an **untracked, worktree-local** `pyrefly.toml` at the worktree root
  naming the include roots explicitly (`src/**/*.py`, `tests/*.py`). `pyrefly.toml` takes
  precedence over `[tool.pyrefly]` in `pyproject.toml`. This is deliberately NOT committed:
  the problem exists only inside the worktree, and the shared `pyproject.toml` is byte
  identical to HEAD.
- **Files modified:** none tracked. `pyrefly.toml` is untracked and dies with the worktree.
- **Verification that this is not a paper-over:** appended `BAD_PROBE: int = "not an int"`
  to `vocabulary.py` and re-ran `uv run pyrefly check` — it reported the error, proving the
  configured run genuinely type-checks `src/`. The probe was then deleted.
- **Residual gap:** even with the explicit includes, pyrefly's project mode still skips the
  `tests` tree in this worktree. Every commit was therefore additionally gated on an
  explicit `uv run pyrefly check tests/test_vocabulary.py tests/test_job.py`, which
  reported `0 errors`. In the main repository (not under a dot-directory) the stock
  `uv run pyrefly check` covers both trees, so the orchestrator's post-merge verification
  is unaffected.
- **Commit:** n/a (no tracked file changed)

**2. [Rule 1 - Lint] ruff SIM300 / D213 / FBT001 on first-draft test code**

- **Found during:** Tasks 1-3
- **Issue:** `assert ACTIVE_STATES == frozenset(...)` reads as a Yoda condition to
  `SIM300`; the D-10 test's multi-line docstring started on the first line (`D213`);
  and parametrising `active: bool, busy: bool` as separate positional params tripped
  `FBT001`.
- **Fix:** operands swapped (`ruff --fix`), docstring summary moved to the second line,
  and the two booleans folded into a single `expected: tuple[bool, bool]` parameter
  compared as `(job.is_active, job.is_busy) == expected`. No rule was disabled and no
  suppression comment was added.
- **Files modified:** `tests/test_vocabulary.py`
- **Commits:** `46a1f80`, `643fa99`, `4b16de2`

### Notes, not deviations

- `worker._categorize_error` was **not** deleted. Task 2's instruction was to move the
  function into `vocabulary.py`; `worker.py` is outside this plan's `files_modified` and
  belongs to a later plan in the phase. The two implementations are byte-identical in
  behaviour and check order, so nothing diverges in the meantime.
- No package was installed. `pyproject.toml` and `uv.lock` are untouched (T-21-SC).

## Threat Model Dispositions Honoured

| Threat ID | How |
|---|---|
| T-21-01 | `case _: assert_never(state)` in all three label/message functions; no `raise ValueError`, no `pass`, no raw-string or `.value` patterns, no guarded arms |
| T-21-02 | `error_message` returns only developer-authored constants; no exception text interpolated; not wired to any template, route, or CLI path |
| T-21-03 | Labels remain developer-authored constants; no `\| safe` filter introduced anywhere |
| T-21-SC | Zero packages installed; dependency files untouched |

## Verification Results

```
uv run ruff check .                              -> All checks passed!
uv run ruff format --check .                     -> 39 files already formatted
uv run ty check                                  -> All checks passed!
uv run pyrefly check                             -> 0 errors  (src; see deviation 1)
uv run pyrefly check tests/test_vocabulary.py \
                     tests/test_job.py           -> 0 errors
uv run pytest -m "not browser"                   -> 422 passed, 8 deselected
```

Baseline was 332 passed; this plan adds 90 tests in `tests/test_vocabulary.py`.

Structural assertions, all passing:

```
command grep -c 'BUSY_STATES = ACTIVE_STATES - {JobState.AWAITING_FLIP}' src/saneless/vocabulary.py  -> 1
command grep -c 'assert_never' src/saneless/vocabulary.py                                            -> 5
command grep -nE 'case _: *(raise|pass)' src/saneless/vocabulary.py                                  -> no match
command grep -nE 'case "[A-Z_]+"|case [A-Za-z]+\.[A-Z_]+\.value' src/saneless/vocabulary.py          -> no match
command grep -nE 'case [A-Za-z]+\.[A-Z_]+ if ' src/saneless/vocabulary.py                            -> no match
command grep -nE '^from (saneless\.)?(job|pipeline|worker|cli|web)' src/saneless/vocabulary.py       -> no match
command grep -n 'noqa\|type: ignore' src/saneless/vocabulary.py tests/test_vocabulary.py             -> no match
command grep -nE '^class (JobState|ErrorCategory)\(StrEnum\)' src/saneless/job.py                    -> no match
command grep -c '__all__ = ["ErrorCategory", "Job", "JobState", "JobStore"]' src/saneless/job.py     -> 1
command grep -n 'noqa' src/saneless/job.py                                                           -> no match
command grep -c 'parametrize' tests/test_vocabulary.py                                               -> 4
```

## Known Stubs

None. `error_message` has no production caller yet, but that is D-12's explicit decision
rather than an unfinished wire-up: the status partial keeps rendering `job.error` verbatim
because the specific text is more truthful today than a generic category sentence, and the
plain-language display is scheduled for the error-message rework. The reasoning is recorded
in the function's own docstring so a future reader does not "finish" it prematurely.

## Commits

| Task | Commit | Description |
|---|---|---|
| 1 | `46a1f80` | vocabulary.py with the three enums and three classifications |
| 2 | `643fa99` | four total lookup functions behind match + assert_never |
| 3 | `4b16de2` | job.py re-export plus Job.is_active / Job.is_busy |

## What the Next Plan Can Rely On

- `from saneless.vocabulary import JobState, ACTIVE_STATES, BUSY_STATES, state_label, progress_label`
  works from anywhere without an import cycle.
- `from saneless.job import JobState, ErrorCategory` still resolves to the *same* objects,
  so no consumer needs editing to keep working.
- `job.is_active` / `job.is_busy` are the seam the templates should use in 21-03; do not
  push the frozensets into the template namespace.
- Adding an eighth `JobState` member without a label arm fails both type checkers — proven,
  not assumed.
