---
phase: 21-vocabulary-and-contracts
plan: 05
subsystem: pipeline-contracts
tags: [dataclass, typed-result, magic-string-removal, docs-accuracy, refactor]
requires:
  - saneless.vocabulary.ScanOutcome
provides:
  - saneless.paperless.UploadResult
  - saneless.pipeline.ScanResult
affects:
  - src/saneless/paperless.py
  - src/saneless/pipeline.py
  - tests/conftest.py
  - tests/test_paperless.py
  - tests/test_pipeline.py
  - tests/test_cli.py
  - tests/test_worker.py
  - docs/explanation/consume-directory-fallback.md
tech-stack:
  added: []
  patterns:
    - "result dataclass lives with its producer, the outcome enum lives in vocabulary.py"
    - "branch on a plain bool field plus an explicit `is not None` so `str | None` narrows structurally, without cast/assert/suppression"
    - "test doubles return the real result type, so a stub that drifts from the contract fails loudly instead of silently taking the wrong branch"
key-files:
  created: []
  modified:
    - src/saneless/paperless.py
    - src/saneless/pipeline.py
    - tests/conftest.py
    - tests/test_paperless.py
    - tests/test_pipeline.py
    - tests/test_cli.py
    - tests/test_worker.py
    - docs/explanation/consume-directory-fallback.md
decisions:
  - "UploadResult and ScanResult are plain @dataclass -- no frozen=, slots=, kw_only= or eq= -- because no dataclass in this codebase uses any of them and the job columns that will mirror these fields would force dataclasses.replace churn with no local precedent"
  - "run_pipeline branches on `upload_result.delivered_to_api and task_uuid is not None` rather than on an `is`-identity comparison against an enum member, so a stub returning the wrong shape raises on attribute access instead of silently selecting a branch"
  - "ScanResult deliberately does NOT carry poll_task's dict: threading an untyped dict through a typed result is the exact smell this phase removes, and no caller of run_pipeline reads its return value today"
  - "The empty-page filter step was extracted into _drop_empty_pages() because the added ScanResult construction pushed run_pipeline to 51 statements and tripped ruff PLR0915; the rule was fixed structurally rather than suppressed"
  - "tests/test_worker.py was swept even though it is outside the plan's files_modified list, because its eleven run_pipeline doubles returned bare status dicts that no longer resemble the contract"
requirements: [CTR-02, CTR-03]
metrics:
  duration: 34m
  completed: 2026-09-10
---

# Phase 21 Plan 05: Typed Upload and Scan Results Summary

`upload_document` returns an `UploadResult` carrying `delivered_to_api`, `task_uuid` and
the consume-directory path instead of a task-uuid-or-`"fallback"` string; `run_pipeline`
returns a `ScanResult` with a `ScanOutcome` and three page counts instead of an untyped
`dict` nobody read; and the one documentation sentence claiming a `FALLBACK` job status
that has never existed now describes what actually happens.

## What Shipped

### `UploadResult` — `src/saneless/paperless.py`

```python
@dataclass
class UploadResult:
    """Where a document ended up when upload_document returned."""

    delivered_to_api: bool
    task_uuid: str | None = None
    consume_dir_path: Path | None = None
```

Exported as `__all__ = ["PaperlessClient", "UploadResult"]`.
`upload_document` is now `-> UploadResult`:

| Path | Return |
|---|---|
| 200 from `post_document/` | `UploadResult(delivered_to_api=True, task_uuid=str(task_id))` |
| retries exhausted, `consume_dir` configured | `UploadResult(delivered_to_api=False, consume_dir_path=dest)` |
| retries exhausted, no `consume_dir` | `raise PaperlessError` — unchanged |
| 4xx | `raise PaperlessError` immediately — unchanged |

`consume_dir_path` reuses the `dest` the method already computed for `shutil.copy2`, so
the result names the exact file the PDF landed in — information the sentinel could not
carry. The stale `Returns:` docstring block that documented `"fallback"` as a return value
was rewritten.

### `ScanResult` — `src/saneless/pipeline.py`

```python
@dataclass
class ScanResult:
    """How a scan pipeline run resolved, and how many pages it moved."""

    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    warning: str | None = None
```

Placed beside `PipelineRequest` as its symmetric result pair; `__all__` is the sorted
`["PipelineEvent", "PipelineRequest", "ScanResult", "run_pipeline"]`.

Field sources inside `run_pipeline`:

| Field | Normal path | Duplex-mismatch early return |
|---|---|---|
| `outcome` | `FALLBACK` when `delivered_to_api` is False, else `SUCCESS` | `SUCCESS` (matches the old `{"status": "DONE"}`) |
| `pages_scanned` | `len(images)` before filtering | `len(fronts) + len(backs)` |
| `pages_removed` | `len(images) - len(filtered)` (0 when the profile toggle is off) | `0` |
| `pages_uploaded` | `len(filtered)` | `len(fronts) + len(backs)` |
| `warning` | `None` | `_handle_duplex_mismatch`'s message |

All three former `dict` return paths are gone: `poll_task`'s task dict, the synthesised
FALLBACK dict, and the DONE-plus-warning dict.

### The narrowing, without a suppression

`poll_task` takes a `str`; `upload_result.task_uuid` is `str | None`. Resolved
structurally:

```python
task_uuid = upload_result.task_uuid
if upload_result.delivered_to_api and task_uuid is not None:
    paperless.poll_task(task_uuid, timeout=settings.output.paperless_task_timeout)
    outcome = ScanOutcome.SUCCESS
else:
    outcome = ScanOutcome.FALLBACK
```

No `cast`, no `assert`, no `# type: ignore`. Both checkers accept it.

### The atomicity moment

The signature change and all five stub sites landed in commit `a0ee3ee`:

| # | Site | Change |
|---|---|---|
| 1 | `tests/conftest.py` `mock_paperless` | `return_value` → `UploadResult(delivered_to_api=True, task_uuid="mock-task-uuid")` |
| 2 | `tests/test_pipeline.py` `test_run_pipeline_calls_poll` | local override → `UploadResult` carrying `"task-uuid-123"`; the `poll_task` call-arg assertion still holds |
| 3 | `tests/test_cli.py` `MockPaperlessClient` | annotation **and** return value |
| 4 | `tests/test_cli.py` `FailPaperless` | annotation only (it raises) |
| 5 | `tests/test_paperless.py` | 3 `== "fallback"` assertions → `delivered_to_api is False` **plus** `consume_dir_path == <the file the PDF was copied to>`; the 2 success-path `== <uuid>` assertions also moved |

`tests/test_web.py`'s unrelated `mock_paperless` fixture (it patches `get_tags` /
`get_correspondents`) was not touched.

### Documentation

`docs/explanation/consume-directory-fallback.md:66` claimed:

> **The job status shows FALLBACK.** ... the scan job's final status is `FALLBACK` rather
> than `DONE`, so users can identify which documents may need metadata corrections ...

`JobState` has no `FALLBACK` member and `worker.py:236` writes `JobState.DONE` after
`run_pipeline` returns regardless of delivery path, so a reader looking for that row would
never find one. Replaced with a bolded lead-in plus one paragraph in the file's existing
voice, stating that both paths end in `DONE`, that the job history does not distinguish
them, and pointing at the paperless-ngx side where the missing metadata is actually
visible. No future state is promised and no planning artefact is referenced.

The feature's English name is intact everywhere: the filename, `docs/index.md`,
`docs/explanation/architecture.md` and `docs/reference/docker.md` are unchanged.

## Grep Gate Output

```
command grep -rn '"fallback"' src/ tests/            -> 0 matches   (exit 1)
command grep -rn 'FALLBACK' docs/ README.md          -> 0 matches   (exit 1)
command grep -rn 'JobState.FALLBACK|ScanOutcome.FAILED' src/ tests/ docs/
                                                     -> 0 matches   (exit 1)

command grep -rn 'FALLBACK' src/                     -> exactly 2 hits, both legitimate:
  src/saneless/pipeline.py:521:            outcome = ScanOutcome.FALLBACK
  src/saneless/vocabulary.py:62:    FALLBACK = "FALLBACK"

ls docs/explanation/consume-directory-fallback.md    -> exists
command grep -c 'fallback' docs/index.md             -> 1  (feature name intact)
command grep -c 'job status shows FALLBACK' docs/explanation/consume-directory-fallback.md -> 0
```

Structural criteria:

```
command grep -c 'class UploadResult' src/saneless/paperless.py   -> 1
command grep -c '\-> UploadResult' src/saneless/paperless.py     -> 1
command grep -c 'class ScanResult'  src/saneless/pipeline.py     -> 1
command grep -c '\-> ScanResult'    src/saneless/pipeline.py     -> 1
command grep -nE 'def run_pipeline.*-> dict' src/saneless/pipeline.py -> no match
command grep -n '{"status"' src/saneless/pipeline.py             -> no match
command grep -nE '@dataclass\((frozen|slots|kw_only|eq)' src/saneless/paperless.py src/saneless/pipeline.py -> no match
command grep -nE 'def poll_task.*-> dict\[str, object\]' src/saneless/paperless.py -> 1 (deliberately untouched)
command grep -c 'delivered_to_api' src/saneless/pipeline.py      -> 1
command grep -c 'UploadResult' tests/conftest.py                 -> 2
```

## Deferred to a Later Phase — Confirmed Absent

| Item | Status |
|---|---|
| `poll_task` raising on FAILURE / timeout | Untouched. Same signature `-> dict[str, object]`, same body, same return values. Verified by grep above. |
| `ScanOutcome.FAILED` | Not added. `ScanOutcome` still has exactly `SUCCESS` and `FALLBACK`. |
| `JobState.FALLBACK` | Not added. Zero hits anywhere in `src/`, `tests/`, `docs/`. |
| Rendering a FALLBACK state in the UI | Not done. No template touched. |

## Threat Model Dispositions Honoured

| Threat ID | How |
|---|---|
| T-21-09 (mitigate) | The stringly-typed protocol is gone. `delivered_to_api` is a declared `bool` on a declared dataclass; a mismatch is now a type error, and a test double returning the wrong shape raises on attribute access instead of silently taking a branch. |
| T-21-10 (accept) | `consume_dir_path` is the same `dest` already logged by `logger.warning("All retries exhausted. Copied PDF to %s", dest)`. No new disclosure surface; not rendered anywhere. |
| T-21-11 (accept) | Unchanged by design — a consume-directory delivery is still recorded as `JobState.DONE`. Task 3 makes the documentation honest about it rather than papering over it. |
| T-21-12 (transfer) | `poll_task` untouched, verified by grep. |
| T-21-SC (mitigate) | Zero packages installed. `pyproject.toml` and `uv.lock` byte-identical to base — `git diff 6eba02e..HEAD --name-only` lists neither. Only stdlib `dataclasses` was newly imported. |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] ruff PLR0915: run_pipeline hit 51 statements**

- **Found during:** Task 2, after adding the `ScanResult` construction
- **Issue:** `src/saneless/pipeline.py:358 PLR0915 Too many statements (51 > 50)`. The
  ScanResult construction added one statement past the configured limit. Suppression is
  forbidden by CLAUDE.md, so the rule had to be satisfied structurally.
- **Fix:** Extracted the Step-2 empty-page block into a module-level
  `_drop_empty_pages(images, profile) -> list[Image.Image]` with a full docstring
  (including its `Raises: ScanError`), reducing `run_pipeline` by seven statements. The
  helper lives in the same module, so `test_custom_thresholds_from_profile`, which patches
  `saneless.pipeline.filter_empty_pages`, still intercepts the call — verified green.
  `ProfileConfig` was added to the existing `TYPE_CHECKING` import from `saneless.config`.
- **Files modified:** `src/saneless/pipeline.py`
- **Commit:** `d1c9277`

**2. [Rule 3 - Blocking] pyrefly project mode cannot see a worktree under a dot-directory**

- **Found during:** setup, before Task 1
- **Issue:** Identical to plans 21-01 and 21-04. The worktree lives under
  `.claude/worktrees/`, which repo `.gitignore` excludes, so pyrefly's project-mode
  include resolution matches nothing and `uv run pyrefly check` exits 1 for reasons
  unrelated to the code, failing the `pyrefly-checker` prek hook on every commit.
- **Fix:** Recreated the same **untracked, worktree-local** `pyrefly.toml`
  (`project-includes = ["src/**/*.py", "tests/*.py"]`). Even with the explicit includes
  pyrefly still emits `WARN Skipping include pattern .../tests/*.py because it is matched
  by ... ignore files`, so every commit was additionally gated on explicitly-named paths:
  `uv run pyrefly check src/saneless/paperless.py src/saneless/pipeline.py tests/conftest.py
  tests/test_paperless.py tests/test_pipeline.py tests/test_cli.py tests/test_worker.py`
  → `0 errors`. That explicit run is what actually covers the test tree here.
- **Files modified:** none tracked. `pyrefly.toml` was **deleted before returning**.
- **Commit:** n/a

### Plan-text discrepancies, reported not silently followed

**A. Task 2 `<files>` and the frontmatter `files_modified` omit `tests/test_worker.py`,
but Task 2's `<action>` explicitly instructs sweeping it.**

The `<action>` was followed. Eleven `run_pipeline` doubles in `tests/test_worker.py` either
returned a bare `{"status": "SUCCESS"}` dict or fell off the end returning `None` while
standing in for `run_pipeline`. They now return a shared `_success_result() -> ScanResult`,
the two `-> dict[str, str]` helpers are `-> ScanResult`, and the six stubs that raise are
annotated with the contract they replace. This is the same faithful-double argument that
justified moving the five `upload_document` stubs in Task 1: nothing type-checks a
`monkeypatch.setattr("saneless.worker.run_pipeline", ...)` target, so an inaccurate
annotation is exactly the invisible drift this phase exists to remove.

**B. Task 1's acceptance criterion `command grep -n 'noqa|type: ignore' ... tests/test_cli.py`
returns one hit that predates this plan.**

`tests/test_cli.py:553` carries `# noqa: PLC0415` on a function-scoped `from fastapi import
FastAPI`, introduced in commit `6546c3d` well before this phase. It is untouched and out of
this plan's scope (it is not on any line this plan modified). No new suppression was added
anywhere: `command grep -n 'noqa|type: ignore'` over `src/saneless/paperless.py`,
`src/saneless/pipeline.py`, `tests/conftest.py`, `tests/test_paperless.py`,
`tests/test_pipeline.py` and `tests/test_worker.py` returns nothing.

**C. A draft comment tripped the plan's own gate.**

The strengthened consume-dir assertion in `tests/test_paperless.py` was first written with
a comment reading `# the old "fallback" sentinel could not carry`. That is prose, not a
protocol, but it contains the literal `"fallback"` and so failed
`command grep -rn '"fallback"' src/ tests/`. Reworded to "the old magic-string sentinel"
before the Task 1 commit. Noted because it is the kind of hit a reviewer running the gate
would otherwise have to re-derive.

No Rule 1, 2, or 4 situations arose. No authentication gates. No checkpoints.

## Verification Results

```
uv run ruff check .                                -> All checks passed!
uv run ruff format --check .                       -> 40 files already formatted
uv run ty check                                    -> All checks passed!
uv run pyrefly check                               -> 0 errors  (src; see deviation 2)
uv run pyrefly check <7 explicit paths>            -> 0 errors
uv run pytest -m "not browser"                     -> 498 passed, 8 deselected
uv run pytest -m browser                           ->   8 passed, 498 deselected
```

Baseline after Wave 2 was **495 passed**; this plan adds three tests
(`TestScanResultContract`: page counts, counts-with-detection-off, and the FALLBACK
outcome) for a floor of 498. No test was deleted and no dip occurred — the suite was green
at all three commit boundaries, each gated on the full five-check CONTRIBUTING set by the
prek hooks.

Per-task green checks:

```
uv run pytest -q tests/test_paperless.py -k "fallback or consume_dir"  ->  5 passed
uv run pytest -q tests/test_pipeline.py::TestRunPipeline
              tests/test_pipeline.py::TestManualDuplex
              tests/test_pipeline.py::TestScanResultContract           -> 17 passed
```

## Known Stubs

None. Every field on both new dataclasses is populated from a real value at every
construction site in `src/`. `ScanResult.warning` is `None` on the normal path because
there is genuinely no warning there, not because a data source is missing.

`run_pipeline`'s return value still has no reader in production (`worker.py:230` and
`cli.py:133` both discard it), which is unchanged from before this plan — the point of
CTR-03 was to make the value typed and truthful so a future reader can exist, not to add
one. `ScanOutcome.FALLBACK` is now produced by `pipeline.py:521` but not yet rendered
anywhere, which is D-06's explicit deferral rather than an unfinished wire-up.

## Commits

| Task | Commit | Description |
|---|---|---|
| 1 | `a0ee3ee` | `UploadResult` + consumer rewire + all five stub sites, atomically |
| 2 | `d1c9277` | `ScanResult`, no dict return path left, test_worker double sweep |
| 3 | `6f57c9d` | the false FALLBACK job-status claim corrected |

## What the Next Plan Can Rely On

- `from saneless.paperless import UploadResult` and `from saneless.pipeline import ScanResult`
  both resolve without an import cycle; `ScanOutcome` stays in `vocabulary.py`.
- `run_pipeline` already computes `pages_scanned` / `pages_removed` / `pages_uploaded`, so
  the job columns that mirror them have a live source — no pipeline change needed to fill
  them.
- `ScanOutcome.FALLBACK` is produced at exactly one site
  (`src/saneless/pipeline.py:521`); adding a `JobState.FALLBACK` and rendering it is a
  worker-and-template change, not a pipeline change.
- `poll_task` is byte-identical to its pre-plan form, so the work that makes it raise
  starts from an unmoved baseline.
- Every `run_pipeline` and `upload_document` test double in the suite now returns the real
  result type, so changing either signature again will fail loudly at the double rather
  than silently mis-branch.

## Self-Check: PASSED

Files claimed and verified present on disk:

```
FOUND: src/saneless/paperless.py
FOUND: src/saneless/pipeline.py
FOUND: tests/conftest.py
FOUND: tests/test_paperless.py
FOUND: tests/test_pipeline.py
FOUND: tests/test_cli.py
FOUND: tests/test_worker.py
FOUND: docs/explanation/consume-directory-fallback.md
FOUND: .planning/phases/21-vocabulary-and-contracts/21-05-SUMMARY.md
```

Commits claimed and verified in `git log` on `worktree-agent-a6b7e09f0c62a090e` atop base
`6eba02e`:

```
FOUND: a0ee3ee
FOUND: d1c9277
FOUND: 6f57c9d
```

No `STATE.md` or `ROADMAP.md` modification: `git diff 6eba02e..HEAD --name-only` lists
neither.
