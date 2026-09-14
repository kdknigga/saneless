---
phase: 25-manual-duplex
plan: 06
subsystem: scanner
tags: [manual-duplex, sane, source-resolution, c-01, tdd]
requires:
  - phase: 25-04
    provides: "profile.duplex as the single strategy reader in run_pipeline"
  - phase: 24
    provides: "classify_source / SourceKind.uses_feeder as the only classification rule"
provides:
  - "ScanSettings.resolve_feeder_source: bool = False"
  - "_resolve_source(raw_options, requested, *, resolve_feeder=False) with a disjoint feeder branch"
  - "_resolve_feeder_source(available_sources, requested) and the no-feeder refusal"
affects: [25-09 docs (refusal message), 25-05 worker/web (none directly)]
tech-stack:
  added: []
  patterns:
    - "Scan-strategy flag carried as a plain value on ScanSettings (auto_source_mode precedent)"
    - "Disjoint early-return branch makes a dangerous fallback structurally unreachable"
key-files:
  created: []
  modified:
    - src/saneless/scanner/base.py
    - src/saneless/scanner/sane_backend.py
    - src/saneless/pipeline.py
    - tests/test_scanner.py
    - tests/test_pipeline.py
key-decisions:
  - "ScanSettings carries resolve_feeder_source: bool, not a scanner-side DuplexMode enum (NONE and HARDWARE would behave identically in the scanner)"
  - "A device with no feeder source (or no source option at all) refuses with ScanError before any page; Auto is never substituted for manual duplex"
  - "The operator's source wins when the device reports it and it feeds; device resolution is the fallback, not the override"
  - "run_pipeline binds manual_duplex = profile.duplex == 'manual' once and feeds both _flip_context and resolve_feeder_source, keeping 25-04's single-profile.duplex-read gate intact"
requirements-completed: [DPLX-01, DPLX-02, DPLX-04]
duration: ~25min
completed: 2026-09-14
---

# Phase 25 Plan 06: Manual-Duplex Feeder Resolution Summary

**Manual duplex now scans through a feeder the device actually reports, picked by `classify_source`. A device with no feeder is refused before pass A, and the `Auto` substitution that caused C-01 cannot be reached from this path.**

## Performance

- **Duration:** ~25 min
- **Completed:** 2026-09-14
- **Tasks:** 2 (TDD RED + GREEN)
- **Files modified:** 5

## Accomplishments

- `ScanSettings.resolve_feeder_source: bool = False` sits beside `auto_source_mode`. A comment records why it is a bool and not an enum.
- `_resolve_source` gained a keyword-only `resolve_feeder`. When it is set, the function returns early into `_resolve_feeder_source`, before the `Auto` fallback code, so that fallback cannot run for manual duplex.
- Feeder selection runs in this order:
  1. The requested source, if the device reports it and it feeds.
  2. Otherwise the first reported source that `classify_source(...).uses_feeder` accepts.
  3. Otherwise a `ScanError`.
- `run_pipeline` is the one place the config `Literal` becomes the scanner flag.
- The integration test runs both passes through the real `SaneBackend` against a device reporting `["Flatbed", "Automatic Document Feeder"]`, with a profile of `source = "ADF"` and `duplex = "manual"`. A second test drives `run_pipeline` against a `Flatbed` + `Auto` device. Before the fix that run took platen snapshots and reported success. Now it refuses with no device calls, no flip prompt and no upload.

## Final signatures

```python
def _resolve_feeder_source(available_sources: list[str], requested: str) -> str: ...

def _resolve_source(
    raw_options: list[tuple], requested: str, *, resolve_feeder: bool = False
) -> tuple[str, bool]: ...
```

## No-feeder refusal message (for plan 25-09)

```
Manual duplex needs a document feeder, and the device reports none. Available: ['Flatbed', 'Auto']
```

It uses the same `Available: [...]` shape as the existing `Device does not support source 'X'. Available: [...]` message. A device with no `source` option renders `Available: []`.

## Task Commits

1. **Task 1 (RED): pin feeder selection, the no-feeder refusal, and a realistic integration device**: `275608c` (test)
2. **Task 2 (GREEN): carry the signal and make the feeder branch disjoint from the Auto path**: `5b184bb` (feat)

## Files Created/Modified

- `src/saneless/scanner/base.py`: the `resolve_feeder_source` field and the comment explaining the choice
- `src/saneless/scanner/sane_backend.py`: `_resolve_feeder_source`, the early branch in `_resolve_source`, and the flag passed through from `scan_pages`
- `src/saneless/pipeline.py`: `manual_duplex` binding, `resolve_feeder_source=manual_duplex` with a single-conversion-point comment
- `tests/test_scanner.py`: `TestResolveSourceForManualDuplex` (12 tests: direct `_resolve_source` cases, the `ScanSettings` default, and two `scan_pages` cases)
- `tests/test_pipeline.py`: the integration device now uses a realistic feeder name, a new no-feeder refusal test through `run_pipeline`, and the `"ADF Manual Duplex"` pseudo-source is gone from this file

## Verification

- `uv run pytest -q`: 1033 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean
- RED run: the scanner cases failed with `TypeError: _resolve_source() got an unexpected keyword argument 'resolve_feeder'`, and the `ScanSettings` cases with a `TypeError` or `AttributeError`. The integration test failed with `Device does not support source 'ADF'`. The pipeline refusal test failed with `DID NOT RAISE`, which was C-01 happening in the test.
- `grep -rn "vocabulary\|JobState\|ScanOutcome" src/saneless/scanner/` (on `.py` files): empty
- `grep -rn '"ADF"' src/saneless/scanner/ --include='*.py'`: empty
- `grep -c "resolve_feeder_source" src/saneless/pipeline.py`: 1
- `grep -v '^#' src/saneless/scanner/sane_backend.py | grep -c classify_source`: 5 (the new branch calls the classifier and does no substring test)
- `profile.duplex` is still read once in `pipeline.py` (plus the existing comment), so 25-04's DPLX-03 gate still holds
- `tests/fake_sane.py` unmodified

## Decisions Made

- **Pipeline binding:** the plan wrote `resolve_feeder_source=profile.duplex == "manual"`. 25-04's gate requires `profile.duplex` to be read exactly once in `pipeline.py`. I bound `manual_duplex = profile.duplex == "manual"` once and used it both for `_flip_context` and for the new keyword. Both plans' gates hold, and the conversion still happens in one place.
- **`has_source_option` on the feeder branch:** it passes through unchanged rather than being hardcoded `True`. Any non-raising return has a non-empty reported list, so the value is `True` anyway.

## Deviations from Plan

### Acceptance-criterion scope

**1. `grep -rn "ADF Manual Duplex" tests/` is not empty, and cannot be from this plan's files**
- **Found during:** Task 2 verification
- **Issue:** The criterion is broader than this plan's files. The remaining hits are:
  - `tests/test_config.py:369,370,401`, `tests/test_auto_profiles.py:700` and `tests/test_browser.py:140`: tests of the legacy DPLX-02 translation, which must keep using the legacy string.
  - `tests/test_worker.py`: owned by the concurrent plan 25-05.
  - `tests/fake_sane.py:792`: a docstring. The plan says not to edit this file.
  - `tests/test_scanner.py:175`: a classifier table case, which is a fact about `classify_source`, not a fictional device.
- **Fix:** All 23 uses in `tests/test_pipeline.py` are gone. The integration test's `report_sources` now uses a realistic list, the `MagicMock`-backed profiles use `source = "ADF"`, and the `duplex="none"` test uses the canonical legacy form `"Manual Duplex"`. No other file was touched.
- **Files modified:** tests/test_pipeline.py
- **Commit:** 275608c

**2. Comment wording changed to pass the plan's own grep gates**
- The first draft of the `ScanSettings` comment contained the word "vocabulary", and the `_resolve_feeder_source` docstring quoted `"ADF"`. Both tripped the D-03 and no-hardcoded-name gates, so both comments were reworded before the GREEN commit.

**Total deviations:** 2, both about test scope or comment wording. No behaviour change from the plan.

## Issues Encountered

- `grep` over `src/saneless/scanner/` can match stale `__pycache__` bytecode. Limit the gate to `*.py` files.

## TDD Gate Compliance

RED `test(25-06)` commit `275608c`, then GREEN `feat(25-06)` commit `5b184bb`. No refactor commit was needed.

## Threat Flags

None. T-25-25 and T-25-28 are handled by the disjoint branch and the "requested source wins" rule. T-25-26 holds because device names are only matched against the device's own list, classified, and rendered into the error text.

## Self-Check: PASSED

- FOUND: src/saneless/scanner/base.py, src/saneless/scanner/sane_backend.py, src/saneless/pipeline.py, tests/test_scanner.py, tests/test_pipeline.py
- FOUND: 275608c, 5b184bb
