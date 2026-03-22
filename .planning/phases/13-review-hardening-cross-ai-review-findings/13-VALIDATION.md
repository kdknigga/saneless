---
phase: 13
slug: review-hardening-cross-ai-review-findings
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-22
---

# Phase 13 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 |
| **Config file** | `pyproject.toml` [tool.pytest.ini_options] |
| **Quick run command** | `uv run pytest tests/ -x --ignore=tests/test_browser.py` |
| **Full suite command** | `uv run pytest tests/ --ignore=tests/test_browser.py` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x --ignore=tests/test_browser.py`
- **After every plan wave:** Run `uv run pytest tests/ --ignore=tests/test_browser.py && uv run ruff check . && uv run ty check`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 13-01-01 | 01 | 1 | RH-01 | unit | `uv run pytest tests/test_pipeline.py -x -k disk_space` | ❌ W0 | ⬜ pending |
| 13-01-02 | 01 | 1 | RH-02 | unit | `uv run pytest tests/test_pipeline.py -x -k duplex_mismatch` | ❌ W0 | ⬜ pending |
| 13-01-03 | 01 | 1 | RH-03 | unit | `uv run pytest tests/test_worker.py -x -k pipeline_event` | ❌ W0 | ⬜ pending |
| 13-02-01 | 02 | 1 | RH-04 | unit | `uv run pytest tests/test_web.py -x -k sanitize` | ❌ W0 | ⬜ pending |
| 13-02-02 | 02 | 1 | RH-05 | unit | `uv run pytest tests/test_config.py -x -k writable` | ❌ W0 | ⬜ pending |
| 13-02-03 | 02 | 1 | RH-06 | unit | `uv run pytest tests/test_worker.py -x -k prune` | ❌ W0 | ⬜ pending |
| 13-02-04 | 02 | 1 | RH-07 | unit | `uv run pytest tests/test_pipeline.py -x -k empty_page_toggle` | ❌ W0 | ⬜ pending |
| 13-02-05 | 02 | 1 | RH-08 | integration | `uv run pytest tests/test_browser.py -x -k page_loads` | ✅ existing | ⬜ pending |
| 13-02-06 | 02 | 1 | RH-09 | manual-only | Visual inspection of YAML comments | N/A | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_pipeline.py` — new test functions for RH-01 (disk_space), RH-02 (duplex_mismatch), RH-07 (empty_page_toggle)
- [ ] `tests/test_worker.py` — new test functions for RH-03 (pipeline_event), RH-06 (prune)
- [ ] `tests/test_web.py` — new test function for RH-04 (sanitize)
- [ ] `tests/test_config.py` — new test function for RH-05 (writable)

*No new test files needed — all fit in existing modules.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| docker-compose.yml config.toml warning comment | RH-09 | YAML comment — no runtime behavior to test | Verify `docker-compose.yml` contains a comment mentioning `config.toml must exist on host` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
