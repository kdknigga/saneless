---
phase: 25-manual-duplex
plan: 04
subsystem: pipeline
tags: [manual-duplex, strategy, refusal-guard, match-assert-never, tdd]
requires:
  - "ProfileConfig.duplex (plan 25-01)"
  - "PipelineRequest.flip_coordinator, _FlipContext, _flip_context (plan 25-03)"
provides:
  - "run_pipeline: the single strategy read `profile.duplex == \"manual\"`, placed before _resolve_device"
  - "_flip_context(request, settings): the refusal, now reached only on that read and before any SANE contact"
  - "_finish_duplex_mismatch(mismatch, tmp_path, paperless, request, settings) -> ScanResult"
  - "worker._process_job builds WorkerFlipCoordinator from profile.duplex"
removed:
  - "pipeline._is_manual_duplex"
affects: [25-05, 25-06, 25-09 (documents the refusal message below)]
tech-stack:
  added: []
  patterns:
    - "total match + assert_never over ScanBatch | _DuplexMismatch"
    - "one strategy read that binds an Optional context; later dispatch branches on that context, not on the field again"
key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - src/saneless/worker.py
    - tests/test_pipeline.py
    - tests/test_worker.py
decisions:
  - "The strategy read binds `flip = _flip_context(...) if profile.duplex == \"manual\" else None` before _resolve_device. Later dispatch branches on `flip is None`, so profile.duplex appears once in pipeline.py"
  - "The mismatch arm moved into _finish_duplex_mismatch because run_pipeline reached 57 statements (PLR0915 limit is 50). The behaviour is unchanged. The D-08 comment sits beside `pages_removed=0` in the helper, with a pointer at the match arm"
  - "21 existing pipeline tests set `.source = \"ADF Manual Duplex\"` after construction, which bypasses the before-validator. They now also set `.duplex = \"manual\"`. Without it they would silently run simplex"
metrics:
  duration: "~30 min"
  completed: 2026-09-14
  tasks: 2
  files: 4
---

# Phase 25 Plan 04: One Strategy Reader, Refused Up Front Summary

`profile.duplex` is now the only thing that decides whether a scan is manual duplex. It is read once
in `run_pipeline`, before `_resolve_device`, so a manual-duplex request without a flip coordinator
fails with `ConfigError` before the scanner is ever contacted. The worker's copy of the
`"manual" in source and "duplex" in source` rule and `pipeline._is_manual_duplex` are both deleted.
The duplex result is now handled by a `match` that must cover every result type (`assert_never`).

## Commits

| Gate | Task | Commit | Message |
|------|------|--------|---------|
| RED | 1 | `6d170b3` | test(25-04): add failing tests for the up-front refusal and profile.duplex as sole strategy reader |
| GREEN | 2 | `7c6e077` | feat(25-04): profile.duplex as the single strategy reader, refused up front, dispatched by a total match |

No REFACTOR commit was needed.

At RED, exactly the 5 new tests failed, and all 123 existing pipeline and worker tests still passed.
The 5 failures:
- refusal test: `DID NOT RAISE ConfigError`. The run went simplex, because `source` was a plain `ADF Front`.
- legacy-looking source with `duplex = "none"`: `ConfigError` from 25-03's `_flip_context`. The old code still went two-pass because of the source name.
- `duplex = "manual"` on a plain source: `assert 1 == 2` scan calls.
- worker, plain source: `isinstance(None, WorkerFlipCoordinator)` was False.
- worker, legacy-looking source with `duplex = "none"`: a coordinator was built.

## Refusal message (25-09 documents this)

`Profile '<name>' is manual duplex, which needs a flip coordinator to tell saneless when the stack has been turned over, and none was supplied`
(`ConfigError`). This replaces the 25-03 placeholder wording.

## DPLX-03 grep gate (verbatim)

```
$ grep -rn '"manual" in\|"duplex" in\|_is_manual_duplex' src/
src/saneless/scanner/base.py:90:    if "duplex" in lower:
src/saneless/config.py:64:    to choose a scanning strategy: ``pipeline._is_manual_duplex`` is deleted in
src/saneless/config.py:75:    return "manual" in lowered and "duplex" in lowered
```

The `scanner/base.py:90` hit already existed at base `cbddb70`, and it is not a strategy read. It is
in `classify_source`, which sorts a SANE source name into a `SourceKind` (`FEEDER_DUPLEX` for a
hardware duplex source). It never checks for "manual" and never chooses the two-pass flow. Nothing in
`pipeline.py` or `worker.py` matches. (Without `-I`, grep also reports a match in a binary
`__pycache__` `.pyc` file, which is not source.)

Other gates:
- `grep -rn "profile.duplex" src/`: one code read in `pipeline.py:1049` and one in `worker.py:272`.
  The other matches are comments, `config.py`'s legacy-source warning, or `auto_profiles.py`'s display
  table. None of those choose a strategy.
- `grep -rn "isinstance(duplex_result" src/`: nothing.
- `grep -v '^#' src/saneless/pipeline.py | grep -c "assert_never(duplex_result)"`: `1`.
- `grep -c "N-07" src/saneless/pipeline.py`: `2`. `_drop_empty_pages` is named in the D-08 comment in
  `_finish_duplex_mismatch` and in the pointer at the `_DuplexMismatch()` arm.

To confirm the `match` really covers every type, the `_DuplexMismatch()` arm was removed from a scratch copy.
Both checkers flagged it: ty with `type-assertion-failure` and pyrefly with `bad-argument-type` on
`assert_never(duplex_result)`. The file was then restored.

## Verification

- `uv run pytest -q`: 1020 passed. That is the 1015 baseline from 25-03 plus 5 new tests.
- `uv run ruff check .`, `uv run ruff format --check .`: clean. PLR0913 and PLR0915 are both clean.
- `uv run ty check`: all checks passed. `uv run pyrefly check src tests`: 0 errors. The 4 warnings
  were already there.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] The mismatch arm was moved into a helper to satisfy PLR0915**
- **Found during:** Task 2
- **Issue:** With the guard and the full mismatch arm both written inline, `run_pipeline` had 57
  statements, over ruff's limit of 50. CLAUDE.md forbids suppressing the rule.
- **Fix:** Two changes:
  - The refusal stays in 25-03's `_flip_context` helper. It is now called only from the single
    `profile.duplex == "manual"` read, before `_resolve_device`, and the placement comment is at that
    call site. As a result, the plan's key_link pattern `flip_coordinator is None` is found in
    `_flip_context` instead of in the body of `run_pipeline`. The guard still runs first, before any
    device contact, and the zero-contact test proves it.
  - The mismatch arm moved into `_finish_duplex_mismatch`, with no change in behaviour: it still skips
    `_drop_empty_pages`, still returns `pages_removed=0`, and the SUCCESS/FALLBACK and warning logic
    are the same. All mismatch tests pass unchanged.
- **Files modified:** src/saneless/pipeline.py
- **Commit:** `7c6e077`

**2. [Rule 1 - Bug] Existing duplex tests would have silently become simplex tests**
- **Found during:** Task 1
- **Issue:** 21 tests in `tests/test_pipeline.py` set `profile.source = "ADF Manual Duplex"` after the
  profile was built. The legacy-translation validator only runs at construction, so these profiles
  kept `duplex = "none"`. Once `source` stopped choosing the strategy, those tests would have run
  simplex and failed.
- **Fix:** Each assignment now has a matching `.duplex = "manual"` line. The source strings were left
  as they are, because changing source names belongs to 25-06.
- **Commit:** `6d170b3`

Commits used `/usr/bin/git` and all hooks ran. No Serena editing tools were used.

## Threat surface

T-25-14, T-25-15, T-25-16 and T-25-17 are mitigated as the plan specified. T-25-18 (the deliberate
mismatch bypass) is accepted and documented in the code. No new endpoints or trust boundaries were
added.

## Known Stubs

None. The 25-03 placeholder in `_flip_context` is now the real guard, called up front.

## Self-Check: PASSED

- FOUND: src/saneless/pipeline.py, src/saneless/worker.py, tests/test_pipeline.py, tests/test_worker.py
- FOUND: 6d170b3 (RED), 7c6e077 (GREEN)
