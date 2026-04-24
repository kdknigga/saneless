---
phase: 2
slug: adf-and-multi-page
status: draft
nyquist_compliant: true
wave_0_complete: true
created: 2026-03-20
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/ -x -q --tb=short` |
| **Full suite command** | `uv run pytest tests/ -v` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q --tb=short`
- **After every plan wave:** Run `uv run pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 02-01-01 | 01 | 1 | SCAN-08, SCAN-09 | unit | `uv run pytest tests/test_pages.py -x -v` | Created by Plan 01 Task 2 | pending |
| 02-01-02 | 01 | 1 | SCAN-08, SCAN-09 | unit | `uv run pytest tests/test_config.py tests/test_worker.py -x -q` | Existing files | pending |
| 02-02-01 | 02 | 2 | SCAN-04, SCAN-05, SCAN-10 | unit | `uv run pytest tests/test_scanner.py -x -v` | Extended by Plan 02 Task 1 | pending |
| 02-03-01 | 03 | 3 | SCAN-06, SCAN-07 | unit+integration | `uv run pytest tests/test_pipeline.py -x -v` | Extended by Plan 03 Task 1 | pending |
| 02-03-02 | 03 | 3 | SCAN-11, SCAN-12 | integration | `uv run pytest tests/test_worker.py -x -v` | Extended by Plan 03 Task 2 | pending |

*Status: pending -- green -- red -- flaky*

---

## Wave 0 Requirements

- [x] `tests/test_pages.py` -- created by Plan 01 Task 2 (SCAN-08: empty page detection, SCAN-09: thumbnail generation)
- [x] `tests/test_scanner.py` -- extended by Plan 02 Task 1 (SCAN-04: ADF multi-page, SCAN-05: duplex, SCAN-10: empty feeder)
- [x] `tests/test_pipeline.py` -- extended by Plan 03 Task 1 (SCAN-06: manual duplex, SCAN-07: page count validation)
- [x] `tests/test_worker.py` -- extended by Plan 03 Task 2 (SCAN-11: queuing, SCAN-12: background worker)
- [x] `tests/conftest.py` -- extended by Plan 01 Task 1 (multi-page/empty/content image fixtures)

*Each plan creates its own test files/classes as TDD tasks. No separate Wave 0 stub generation needed -- plans are type: execute with tdd="true" on each task, so tests are written before implementation within each task.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real ADF hardware feed | SCAN-04 | Requires physical scanner with ADF | Load 3 pages, run `saneless scan --profile adf`, verify 3-page PDF |
| Real empty feeder detection | SCAN-10 | Requires physical scanner with empty ADF | Run ADF scan with empty tray, verify error message |

*All other behaviors can be verified with mock scanner backend.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 10s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved
