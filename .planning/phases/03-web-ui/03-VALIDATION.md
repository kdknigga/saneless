---
phase: 03
slug: web-ui
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 03 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest tests/ -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 03-01-01 | 01 | 1 | UI-01 | e2e (Playwright) | `uv run pytest tests/test_web.py::test_page_loads -x` | No -- Wave 0 | ⬜ pending |
| 03-01-02 | 01 | 1 | UI-02 | unit | `uv run pytest tests/test_web.py::test_status_polling -x` | No -- Wave 0 | ⬜ pending |
| 03-01-03 | 01 | 1 | UI-03 | unit | `uv run pytest tests/test_web.py::test_flip_prompt -x` | No -- Wave 0 | ⬜ pending |
| 03-01-04 | 01 | 1 | UI-04 | unit | `uv run pytest tests/test_web.py::test_thumbnail_display -x` | No -- Wave 0 | ⬜ pending |
| 03-01-05 | 01 | 1 | UI-05 | unit | `uv run pytest tests/test_web.py::test_job_history -x` | No -- Wave 0 | ⬜ pending |
| 03-01-06 | 01 | 1 | UI-06 | unit | `uv run pytest tests/test_job.py::test_prune -x` | No -- Wave 0 | ⬜ pending |
| 03-01-07 | 01 | 1 | UI-07 | e2e (Playwright) | `uv run pytest tests/test_web.py::test_scan_button_disabled -x` | No -- Wave 0 | ⬜ pending |
| 03-01-08 | 01 | 1 | UI-08 | unit | `uv run pytest tests/test_web.py::test_cache_invalidate -x` | No -- Wave 0 | ⬜ pending |
| 03-01-09 | 01 | 1 | PROF-03 | unit | `uv run pytest tests/test_web.py::test_profile_dropdown -x` | No -- Wave 0 | ⬜ pending |
| 03-01-10 | 01 | 1 | PLSS-04 | unit | `uv run pytest tests/test_web.py::test_scan_form_submit -x` | No -- Wave 0 | ⬜ pending |
| 03-01-11 | 01 | 1 | PLSS-05 | unit | `uv run pytest tests/test_cache.py -x` | No -- Wave 0 | ⬜ pending |
| 03-01-12 | 01 | 1 | HLTH-01 | unit | `uv run pytest tests/test_web.py::test_health_endpoint -x` | No -- Wave 0 | ⬜ pending |
| 03-01-13 | 01 | 1 | HLTH-02 | unit | `uv run pytest tests/test_web.py::test_health_no_auth -x` | No -- Wave 0 | ⬜ pending |
| 03-01-14 | 01 | 1 | LOG-03 | unit | `uv run pytest tests/test_web.py::test_error_display -x` | No -- Wave 0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_web.py` — stubs for UI-01 through UI-08, PROF-03, PLSS-04, HLTH-01, HLTH-02, LOG-03
- [ ] `tests/test_cache.py` — stubs for PLSS-05 (TTL cache with invalidation)
- [ ] `tests/test_job.py::test_prune` / `tests/test_job.py::test_list_recent` — stubs for UI-05, UI-06
- [ ] Dev dependency: `uv add --dev httpx` (for FastAPI TestClient)

---

## Manual-Only Verifications

*All phase behaviors have automated verification via Playwright MCP and pytest.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
