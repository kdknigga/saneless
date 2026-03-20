---
phase: 2
slug: adf-and-multi-page
status: draft
nyquist_compliant: false
wave_0_complete: false
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
| 02-01-01 | 01 | 1 | SCAN-04 | unit | `uv run pytest tests/test_scanner_adf.py -k multi_page` | ❌ W0 | ⬜ pending |
| 02-01-02 | 01 | 1 | SCAN-10 | unit | `uv run pytest tests/test_scanner_adf.py -k empty_feeder` | ❌ W0 | ⬜ pending |
| 02-01-03 | 01 | 1 | SCAN-05 | unit | `uv run pytest tests/test_scanner_adf.py -k hardware_duplex` | ❌ W0 | ⬜ pending |
| 02-02-01 | 02 | 1 | SCAN-08 | unit | `uv run pytest tests/test_empty_page.py` | ❌ W0 | ⬜ pending |
| 02-02-02 | 02 | 1 | SCAN-09 | unit | `uv run pytest tests/test_thumbnail.py` | ❌ W0 | ⬜ pending |
| 02-03-01 | 03 | 2 | SCAN-06, SCAN-07 | unit+integration | `uv run pytest tests/test_manual_duplex.py` | ❌ W0 | ⬜ pending |
| 02-03-02 | 03 | 2 | SCAN-11, SCAN-12 | integration | `uv run pytest tests/test_pipeline_adf.py` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_scanner_adf.py` — stubs for SCAN-04, SCAN-05, SCAN-10
- [ ] `tests/test_empty_page.py` — stubs for SCAN-08
- [ ] `tests/test_thumbnail.py` — stubs for SCAN-09
- [ ] `tests/test_manual_duplex.py` — stubs for SCAN-06, SCAN-07
- [ ] `tests/test_pipeline_adf.py` — stubs for SCAN-11, SCAN-12

*Existing infrastructure covers pytest framework and conftest.py.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real ADF hardware feed | SCAN-04 | Requires physical scanner with ADF | Load 3 pages, run `saneless scan --profile adf`, verify 3-page PDF |
| Real empty feeder detection | SCAN-10 | Requires physical scanner with empty ADF | Run ADF scan with empty tray, verify error message |

*All other behaviors can be verified with mock scanner backend.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
