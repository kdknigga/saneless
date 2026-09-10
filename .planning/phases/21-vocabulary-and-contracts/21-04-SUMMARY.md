---
phase: 21-vocabulary-and-contracts
plan: 04
subsystem: pipeline-worker-cli
tags: [enums, exhaustiveness, assert_never, refactor, state-machine, cli]
requires:
  - saneless.vocabulary.JobState
  - saneless.vocabulary.ACTIVE_STATES
  - saneless.vocabulary.BUSY_STATES
  - saneless.vocabulary.classify_error
  - saneless.vocabulary.progress_label
provides:
  - saneless.pipeline.PipelineEvent.job_state
affects:
  - src/saneless/worker.py
  - src/saneless/cli.py
tech-stack:
  added: []
  patterns:
    - "property on a StrEnum implemented as match over bare member patterns, assign-in-arm, one trailing return, case _: assert_never(self)"
    - "status callback driven by a derived classification set (ACTIVE_STATES) instead of a hand-written event list"
    - "branch on `state is None` rather than on an enum member name, so the type checker narrows the non-None arm"
key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - src/saneless/worker.py
    - src/saneless/cli.py
    - tests/test_pipeline.py
    - tests/test_worker.py
decisions:
  - "`saneless jobs` keeps printing RAW state values at both cli.py:238 (--json) and cli.py:262 (human table) -- a deliberate decision, not an omission"
  - "PipelineEvent.SCANNING is not special-cased by name in _status_cb, accepting one redundant idempotent SQLite UPDATE rather than reintroducing a hand-written event list"
  - "worker.py now imports JobState from .vocabulary (its definition site) rather than from .job, since a .vocabulary import was being added anyway"
  - "_categorize_error deleted outright with no compatibility shim or delegate method -- nothing in tests/ referenced it"
  - "logger.error(...category.value.lower()...) left byte-identical; error_message() deliberately not wired (D-12)"
requirements: [CTR-01, CTR-05]
metrics:
  duration: 12m
  completed: 2026-09-10
---

# Phase 21 Plan 04: Event-to-State Projection Summary

`PipelineEvent.job_state` is now the single machine-checked projection from pipeline events
to persisted job states; the worker's four-arm `if/elif` chain and the CLI's private
progress-prose dict are both gone, replaced by reads of `ACTIVE_STATES`/`BUSY_STATES` and
`progress_label`, with every user-visible string byte-identical to before.

## What Shipped

**`src/saneless/pipeline.py`** — `PipelineEvent` gains a `job_state` property returning
`JobState | None`, implemented as a `match self:` over all six members with bare member
patterns, assign-in-arm, one trailing `return`, and `case _: assert_never(self)`.
`SCANNING_REVERSE` maps explicitly to `None` ("this event changes no persisted state") with
a comment on its arm and the meaning documented in the property docstring. The docstring
also warns that a non-`None` result is *not* an instruction to write that state — `DONE` is
terminal and the caller decides which states it applies. Imports added:
`typing.assert_never` and `from saneless.vocabulary import JobState` (runtime, absolute
form, matching the file's existing style).

**`src/saneless/worker.py`** — `_status_cb` names no `PipelineEvent` member at all:

```
state = event.job_state
if state is None or state not in ACTIVE_STATES:   -> _transition_event.set(); return
elif state in BUSY_STATES:                        -> update_state(state); set()
else:                                             -> clear(); update_state(state)
```

`ScanWorker._categorize_error` deleted; the single call site now reads
`category = classify_error(exc)`. The runtime `from .exceptions import ...` and
`from .job import ErrorCategory, JobState` lines are gone (ruff `F401` confirmed both fully
unused); `JobState` and the classifications come from a single
`from .vocabulary import ACTIVE_STATES, BUSY_STATES, JobState, classify_error`.

**`src/saneless/cli.py`** — `_event_labels` deleted; `status_callback` branches
`DONE` -> `state is None` -> general, echoing `progress_label(state)` in the last arm.

## Ordering Preserved (T-21-08)

| Event | Before | After |
|---|---|---|
| `AWAITING_FLIP` | `clear()` then `update_state(AWAITING_FLIP)` | `clear()` then `update_state(state)` |
| `ASSEMBLING` | `update_state(ASSEMBLING)` then `set()` | `update_state(state)` then `set()` |
| `UPLOADING` | `update_state(UPLOADING)` then `set()` | `update_state(state)` then `set()` |
| `SCANNING_REVERSE` | `set()` only | `set()` only |

`BUSY_STATES == ACTIVE_STATES - {AWAITING_FLIP}` turned out to be *exactly* the "set after"
set, which is an independent confirmation that the three-way classification from plan 21-01
has the right shape. The existing `tests/test_worker.py` transition tests
(`test_worker_assembling_state`, `test_worker_uploading_state`, and the flip tests) pass
unedited.

## The Two Benign Deltas

Both are deliberate consequences of removing the hand-written event list, recorded here and
in the commit message so a reader does not mistake them for accidents.

1. **`PipelineEvent.DONE` now reaches `_transition_event.set()`** where it previously matched
   no branch at all. The event is always already set at that moment — the immediately
   preceding `UPLOADING` set it — and `threading.Event.set()` on an already-set event is a
   no-op. No state is written: `JobState.DONE` is not in `ACTIVE_STATES`, so `DONE` takes the
   early-return branch.

2. **`PipelineEvent.SCANNING` now produces a second, idempotent `update_state(SCANNING)`.**
   The worker already writes `SCANNING` at `worker.py:181` before calling `run_pipeline`, and
   `update_state` is a plain `UPDATE`, so the only cost is one redundant SQLite write per job.
   `SCANNING` is deliberately **not** special-cased by name to avoid it — doing so would
   reintroduce precisely the hand-written event list this plan deletes.

## The DONE-From-Inside-The-Pipeline Trap (T-21-07)

`run_pipeline` does emit both `SCANNING` and `DONE`; the old `if/elif` chain silently ignored
both. A rewrite that applied every non-`None` `job_state` would write `JobState.DONE` from
*inside* the pipeline's `with tempfile.TemporaryDirectory(...)` block — before cleanup and
before `run_pipeline` returns. The web UI polls once a second, so that is user-observable:
the "Done" panel and the history-refresh `hx-get` fire early, and any exception during
cleanup then produces a visible DONE -> ERROR flicker.

The fix is `state not in ACTIVE_STATES`, which is derived vocabulary rather than a new
hand-written ignore list, and which naturally excludes `DONE` (terminal) while including
`SCANNING`.

`TestWorkerEnumDispatch::test_worker_status_cb_dispatches_on_pipeline_event` was rewritten to
close the gap. The fake pipeline now emits **every** member of `PipelineEvent` (iterating
`PipelineEvent`, not a hand-written list, so a seventh member is exercised automatically) and
snapshots `states_seen[start:]` so that only what the *callback* wrote is measured, separate
from the worker's own pre-pipeline `SCANNING` and post-pipeline `DONE` writes. It asserts:

- `JobState.DONE not in from_callback`
- `from_callback == [SCANNING, AWAITING_FLIP, ASSEMBLING, UPLOADING]` (emission order)
- `states_seen[-1] is JobState.DONE` (still written, by the worker, after the pipeline returned)

**Verified to actually catch the regression**, not merely asserted: relaxing the guard to
`if state is None:` and re-running produced

```
assert JobState.DONE not in from_callback
E   AssertionError: assert <JobState.DONE: 'DONE'> not in [<JobState.SCANNING: 'SCANNING'>, ...
```

The guard was then restored and the test re-passed. The previous version of this test
(`assert JobState.ASSEMBLING in states_seen`, single event emitted) would not have caught it.

## Exhaustiveness Spot-Check Result

Deleted the `case PipelineEvent.SCANNING_REVERSE: state = None` arm from
`PipelineEvent.job_state`, ran both checkers, restored the arm.

- `uv run ty check src/saneless/pipeline.py` → exit 1:
  `error[type-assertion-failure]: Argument does not have asserted type 'Never'` —
  `Inferred type of argument is 'Self@job_state & ~Literal[PipelineEvent.SCANNING] & ~Literal[PipelineEvent.AWAITING_FLIP] & ~Literal[PipelineEvent.ASSEMBLING] & ~Literal[PipelineEvent.UPLOADING] & ~Literal[PipelineEvent.DONE]'`
- `uv run pyrefly check src/saneless/pipeline.py` → exit 1:
  `ERROR Argument 'Literal[PipelineEvent.SCANNING_REVERSE]' is not assignable to parameter 'arg' with type 'Never' in function 'typing.assert_never' [bad-argument-type]` at line 83

Note the shape difference from plan 21-01: because this is a `@property` on the enum rather
than a module-level function, `ty` reports the residue as `Self@job_state` minus the five
handled literals rather than naming `SCANNING_REVERSE` directly. `pyrefly` names the missing
member outright. Both fail; both exit 0 after the arm is restored. The enforcement is real,
and adding a seventh `PipelineEvent` without deciding what it persists breaks the build.

## The `saneless jobs` Raw-Value Decision

**`saneless jobs` still prints RAW state values at both `cli.py:238` and `cli.py:262`, by
decision. Neither line was edited.** Reasoning:

- **`:238`** is inside `--json` output. That is a **machine contract**; friendly labels there
  would break any consumer parsing it.
- **`:262`** is the human table. The phase goal's "no behaviour change" is unconditional with
  exactly one authorised exception (D-11, which belongs to plan 21-02). Relabelling this table
  would be a second, unauthorised user-visible change smuggled in under a refactor.
- Success criterion 1's "the worker, web templates, and CLI all read them from the same
  module" is satisfied by the CLI's **progress prose**, which this plan moved into
  `vocabulary.py`. That is the vocabulary the criterion is about.
- Phase 23's criterion ("`saneless jobs` renders `FALLBACK` distinctly") is already satisfied
  by raw values, and human-friendly CLI output belongs with the later appliance layer.

`command grep -c 'j.state.value' src/saneless/cli.py` returns 2, confirming both lines survive.

## CLI Strings — Byte Identity

`tests/test_cli.py` was **not edited** (`git diff --name-only 08adfad HEAD` lists only
`src/saneless/{cli,pipeline,worker}.py` and `tests/test_{pipeline,worker}.py`), and all 37 CLI
tests pass. Additionally verified programmatically that each event still yields exactly its
old string with three ASCII periods and no U+2026:

| Event | String | Source |
|---|---|---|
| `SCANNING` | `Scanning...` | `progress_label(JobState.SCANNING)` |
| `AWAITING_FLIP` | `Awaiting flip...` | `progress_label(JobState.AWAITING_FLIP)` |
| `SCANNING_REVERSE` | `Scanning reverse sides...` | the one explicit CLI case |
| `ASSEMBLING` | `Assembling PDF...` | `progress_label(JobState.ASSEMBLING)` |
| `UPLOADING` | `Uploading to paperless-ngx...` | `progress_label(JobState.UPLOADING)` |
| `DONE` | `Done: {title}` | the bespoke `DONE` branch, unchanged |

The dropped `_event_labels.get(event, str(event))` fallback had no reachable input — all five
non-`DONE` events were keys — so its removal changes nothing; the new form is total by
construction.

Why the middle branch tests `state is None` rather than `event is PipelineEvent.SCANNING_REVERSE`:
`SCANNING_REVERSE` is the only event whose `job_state` is `None`, so the two are equivalent
today, but only the `None` test narrows `state` to `JobState` for the `progress_label(state)`
call in the final branch. When Phase 25 gives the reverse pass a `JobState` twin, this branch
collapses into the general one — recorded in a comment at the branch.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] pyrefly project mode skips the `tests` tree inside the worktree**

- **Found during:** Task 1, before the first commit
- **Issue:** Identical to plan 21-01's deviation 1. The worktree lives under
  `.claude/worktrees/`, which repo `.gitignore` excludes, so pyrefly's project-mode include
  resolution matched nothing and `uv run pyrefly check` exited 1 for reasons unrelated to the
  code, failing the `pyrefly-checker` prek hook.
- **Fix:** Recreated the same **untracked, worktree-local** `pyrefly.toml`
  (`project-includes = ["src/**/*.py", "tests/*.py"]`). Every commit was additionally gated
  on explicitly-named paths (`uv run pyrefly check src/saneless/worker.py tests/test_worker.py`
  and equivalents), which report `0 errors`. Even with the explicit includes, pyrefly still
  reports `WARN Skipping include pattern .../tests/*.py because it is matched by ...
  ignore files`, so the explicit-path runs are what actually cover the test tree.
- **Files modified:** none tracked. `pyrefly.toml` was **deleted before returning**, leaving
  the worktree with no untracked files.
- **Commit:** n/a

No other deviations. No Rule 1, 2, or 4 situations arose; the plan's task list was executed
exactly as written.

### Notes, not deviations

- `_is_manual_duplex` (`pipeline.py:136`) and the duplicated inline
  `"manual" in source and "duplex" in source` in `worker.py` are **untouched** (constraint 5).
- No `JobState.SCANNING_REVERSE` was added — `command grep -c 'SCANNING_REVERSE'
  src/saneless/vocabulary.py` returns 0 (constraint 6).
- `error_message()` is still not wired to any CLI or template path (D-12 / T-21-02).
- `logger.error("Job %s failed (%s): %s", ...)` is byte-identical to before.
- No package was installed; `pyproject.toml` and `uv.lock` are untouched (T-21-SC).

## Threat Model Dispositions Honoured

| Threat ID | Disposition | How |
|---|---|---|
| T-21-07 | mitigate | Only `ACTIVE_STATES` are applied, so `DONE` cannot be written from the callback; enforced by a `TestWorkerEnumDispatch` assertion that was proven to fail when the guard is relaxed |
| T-21-08 | mitigate | clear-before-`AWAITING_FLIP` / set-after-`ASSEMBLING`/`UPLOADING` preserved verbatim; the existing transition tests pass unedited |
| T-21-02 | accept | `error_message()` deliberately not wired; `classify_error` returns the same categories, and both `job.error` and the log line are unchanged |
| T-21-SC | mitigate | Zero packages installed; only `typing.assert_never` and existing first-party imports added |

## Verification Results

```
uv run ruff check .                                   -> All checks passed!
uv run ruff format --check .                          -> 39 files already formatted
uv run ty check                                       -> All checks passed!
uv run pyrefly check                                  -> 0 errors  (src; see deviation 1)
uv run pyrefly check src/saneless/pipeline.py \
        src/saneless/worker.py src/saneless/cli.py \
        tests/test_pipeline.py tests/test_worker.py \
        tests/test_cli.py                             -> 0 errors
uv run pytest -m "not browser"                        -> 453 passed, 8 deselected
```

Baseline entering this plan was 445 passed; the 8 new tests are the parametrised totality
test over `list(PipelineEvent)` (6 cases) plus the explicit mapping and `SCANNING_REVERSE`
tests. Every one of the three commits was individually gated on the full five-check suite.

Structural assertions, all passing:

```
command grep -rn '_event_labels\|_categorize_error' src/ tests/                -> no match
command grep -rnE 'elif event is PipelineEvent' src/                           -> no match
command grep -c 'assert_never' src/saneless/pipeline.py                        -> 2
command grep -nE 'case _: *(raise|pass)' src/saneless/pipeline.py              -> no match
command grep -c 'classify_error(exc)' src/saneless/worker.py                   -> 1
command grep -c 'event.job_state' src/saneless/worker.py                       -> 1
command grep -c 'ACTIVE_STATES' src/saneless/worker.py                         -> 2
command grep -c 'progress_label' src/saneless/cli.py                           -> 4
command grep -cF 'Scanning reverse sides...' src/saneless/cli.py               -> 1
command grep -cE 'Assembling PDF\.\.\.|Uploading to paperless-ngx\.\.\.' cli.py -> 0
command grep -c 'j.state.value' src/saneless/cli.py                            -> 2
command grep -rn 'SCANNING_REVERSE' src/saneless/vocabulary.py src/saneless/job.py -> no match
command grep -n 'noqa\|type: ignore' src/saneless/{pipeline,worker,cli}.py \
        tests/test_{pipeline,worker}.py                                        -> no match
git diff --name-only 08adfad HEAD                                              -> the 5 planned files only
```

## Known Stubs

None.

## Commits

| Task | Commit | Description |
|---|---|---|
| 1 | `973b9ba` | `PipelineEvent.job_state` total projection + totality tests |
| 2 | `a70a00c` | worker `_status_cb` driven by `ACTIVE_STATES`/`BUSY_STATES`; `classify_error` in use |
| 3 | `3aa9e9f` | CLI `_event_labels` deleted, prose sourced from `progress_label` |

## What the Next Plan Can Rely On

- `PipelineEvent.job_state` is total and type-checked; adding a seventh event without a
  mapping arm fails `ty`, `pyrefly`, **and** the parametrised suite.
- The worker holds no event-name list. When Phase 25 adds `JobState.SCANNING_REVERSE`, the
  only edits needed are the one `job_state` arm and the CLI's `state is None` branch, which
  then collapses into the general one.
- `worker.py` no longer imports `saneless.exceptions` at all; error classification lives in
  exactly one place.
- The DONE-from-inside-the-pipeline regression is now covered by a test that has been
  demonstrated to fail when the guard is removed.

## Self-Check: PASSED

All five modified files exist on disk, and all three claimed commits (`973b9ba`, `a70a00c`,
`3aa9e9f`) are present in `git log` on `worktree-agent-a138ea4dea61584e5` atop base `08adfad`.
